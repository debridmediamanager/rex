#!/usr/bin/env python3
"""`continuous=1` on an episode, and a PUT add of a season or show, on a play queue.

Cycle 83's N territory replayed axis 11's write transcript against the native server:
Plex for Android's Play on an episode (`tests/fixtures/playqueue_continuous.json`,
`app_request`, captured through the pinning proxy) sends `POST /playQueues?...continuous=1`,
and the native server queued that one episode where the oracle queued it and the rest of
its season -- so the app had nothing to play next. Measured on plexscratch42 (1.42.2), the
fixture's `create` and `add`: `continuous=1` queues the episode and the episodes that
FOLLOW it in its own season, and stops at the season's end; a PUT add honours it the same
way; and a season or show uri added to a queue adds its episodes. The native PUT added the
bare episode, season or show row.

Starts its own server (native by default, `PLEXSIM_SERVER=python` the oracle) on
`tests/catalog_ui.json`, whose show 20001 has seasons 30002 [1002, 1003] and
30004 [1005, 1006, 1010, 1011].
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from server_command import server_command  # noqa: E402

ROOT = os.path.dirname(HERE)
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s  %s" % ("PASS" if ok else "FAIL", name, detail))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def send(port, method, path, headers):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request(method, path, headers=dict(headers, Accept="application/json"))
    r = c.getresponse()
    body = r.read()
    c.close()
    container = json.loads(body).get("MediaContainer", {}) if r.status == 200 else {}
    return r.status, container


def keys(container):
    return [row["ratingKey"] for row in container.get("Metadata", [])]


def main():
    fixture = json.load(open(os.path.join(HERE, "fixtures", "playqueue_continuous.json")))
    # The reference's own answers, re-read from the fixture so a re-measurement moves them.
    create = {row["path"].split("metadata%2F")[1].split("&")[0]: [r["ratingKey"] for r in row["rows"]]
              for row in fixture["create"]}
    assert create == {"387": ["387", "388"], "388": ["388"], "23": ["23"]}, create
    added = [[r["ratingKey"] for r in row["rows"]] for row in fixture["add"]]
    assert added[1] == ["21", "387", "388"] and added[2][:4] == ["21", "25", "26", "27"], added
    assert len(added[3]) == 9 and len(added[4]) == 18, added
    port = free_port()
    env = dict(os.environ, PLEXSIM_PLEXTV_URL="http://127.0.0.1:9")
    env.pop("TMDB_KEY", None)
    env.pop("PLEXSIM_STATE", None)
    proc = subprocess.Popen(
        server_command("--port", str(port), "--catalog", "tests/catalog_ui.json",
                       "--assert-claimed", "--gdm", "off", root=ROOT),
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.3).close()
                break
            except OSError:
                time.sleep(0.2)
        _, ident = send(port, "GET", "/identity", {})
        machine = ident["machineIdentifier"]
        app = fixture["app_request"]
        headers = {k: v for k, v in app["req_headers"] if k.lower() not in ("accept-encoding",)}

        def uri(rk):
            return "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, rk)

        def app_play(rk, client, continuous="1"):
            query = [(k, uri(rk) if k == "uri" else client if k == "X-Plex-Client-Identifier"
                      else continuous if k == "continuous" else v) for k, v in app["query"]]
            hdr = dict(headers, **{"X-Plex-Client-Identifier": client})
            return send(port, "POST", app["path"] + "?" + urllib.parse.urlencode(query), hdr)

        # create: the app's own request, one client per queue
        for rk, want in (("1002", ["1002", "1003"]),
                         ("1005", ["1005", "1006", "1010", "1011"]),
                         ("1006", ["1006", "1010", "1011"]),
                         ("1003", ["1003"])):
            status, c = app_play(rk, "c83xn-%s" % rk)
            check("the app's Play on %s (continuous=1) queues the rest of its season" % rk,
                  status == 200 and keys(c) == want and c.get("playQueueTotalCount") == len(want),
                  "%s %s total=%s" % (status, keys(c), c.get("playQueueTotalCount")))
        status, c = app_play("1002", "c83xn-off", continuous="0")
        check("continuous=0 queues the one episode", status == 200 and keys(c) == ["1002"],
              "%s %s" % (status, keys(c)))

        # add: the measured PUT sequence, on a queue of one episode
        client = {"X-Plex-Client-Identifier": "c83xn-put"}
        status, c = app_play("1004", "c83xn-put", continuous="0")
        qid = c.get("playQueueID")
        steps = (
            ("a PUT add of an episode with continuous=1 adds the rest of its season",
             [("uri", uri("1005")), ("continuous", "1")],
             ["1004", "1005", "1006", "1010", "1011"]),
            ("next=1 with continuous=1 inserts the run after the selected item",
             [("uri", uri("1002")), ("continuous", "1"), ("next", "1")],
             ["1004", "1002", "1003", "1005", "1006", "1010", "1011"]),
            ("a PUT add of a season adds its episodes",
             [("uri", uri("30002"))],
             ["1004", "1002", "1003", "1005", "1006", "1010", "1011", "1002", "1003"]),
            ("a PUT add of a show adds every episode",
             [("uri", uri("20001"))],
             ["1004", "1002", "1003", "1005", "1006", "1010", "1011", "1002", "1003",
              "1001", "1002", "1003", "1004", "1005", "1006", "1010", "1011"]),
        )
        for name, query, want in steps:
            status, c = send(port, "PUT", "/playQueues/%s?%s" % (qid, urllib.parse.urlencode(query)),
                             client)
            check(name, status == 200 and keys(c) == want and c.get("playQueueTotalCount") == len(want),
                  "%s %s" % (status, keys(c)))
        status, c = send(port, "GET", "/playQueues/%s" % qid, client)
        check("the queue reads back with no parent row in it",
              status == 200 and all(row.get("type") == "episode" for row in c.get("Metadata", []))
              and len(keys(c)) == 17, "%s %s" % (status, [row.get("type") for row in c.get("Metadata", [])][-3:]))
    finally:
        proc.terminate()
        proc.wait(10)
    print("\nPASS %d/%d play-queue continuation checks" % (sum(results), len(results)) if all(results)
          else "\nFAIL %d/%d play-queue continuation checks" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
