#!/usr/bin/env python3
"""Cycle 83, territories C and P on the native server: the differences that were the native's.

Each check replays a reference exchange recorded in `tests/fixtures/cycle83_cp.json` (zen and
plexscratch42, both PMS 1.42.2) and asserts the measured value, not agreement between the two
servers.

Starts its own server (native by default, `PLEXSIM_SERVER=python` the oracle,
`PLEXSIM_SERVER=/path/to/binary` any build) on `tests/catalog_ui.json`, without `--state`.
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from server_command import server_command  # noqa: E402

ROOT = os.path.dirname(HERE)
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "cycle83_cp.json")))
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self):
        self.port = free_port()
        env = dict(os.environ, RD_TOKEN="dummy", PLEXSIM_PLEXTV_URL="http://127.0.0.1:9")
        for name in ("TMDB_KEY", "PLEXSIM_STATE"):
            env.pop(name, None)
        self.proc = subprocess.Popen(
            server_command("--port", str(self.port), "--catalog", "tests/catalog_ui.json",
                           "--assert-claimed", "--gdm", "off", root=ROOT),
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.3).close()
                return
            except OSError:
                time.sleep(0.2)
        raise SystemExit("server never listened")

    def req(self, method, path, headers=(), client="c83cp-check"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.putrequest(method, path, skip_accept_encoding=True)
        sent = dict(headers)
        sent.setdefault("X-Plex-Client-Identifier", client)
        for k, v in sent.items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheaders(), body

    def json(self, method, path, client="c83cp-check"):
        status, _, body = self.req(method, path, {"Accept": "application/json"}, client)
        return status, (json.loads(body) if body.startswith(b"{") else None)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def agents_paged(srv):
    """zen, 1.42.2: `GET /system/agents?mediaType=1` with the X-Plex-Container pair as request
    headers and no Accept (python-plexapi's `PlexServer.agents()`) is the XML page -- `size 2
    totalSize 5 offset 1`, the second and third agents, and the two paging headers. The native
    server rendered this XML by hand past the pager, so it answered all five agents with no
    `totalSize`, and plexapi's `agents().totalSize` and `.offset` read None."""
    fx = FIXTURE["agents_paged"]
    status, headers, body = srv.req("GET", fx["request"]["path"], fx["request"]["headers"])
    text = body.decode("utf-8", "replace")
    top = re.search(r"<MediaContainer([^>]*)>", text)
    attrs = dict(re.findall(r'(\w+)="([^"]*)"', top.group(1))) if top else {}
    ids = re.findall(r'<Agent [^>]*identifier="([^"]*)"', text)
    check("agents XML page: status", status == fx["status"], status)
    check("agents XML page: container size/totalSize/offset",
          {k: attrs.get(k) for k in fx["container"]} == fx["container"], attrs)
    check("agents XML page: the paged rows", ids == fx["identifiers"], ids)
    want = {k: v for k, v in fx["headers"] if k.startswith("X-Plex-Container")}
    got = {k: v for k, v in headers if k in want}
    check("agents XML page: the X-Plex-Container headers", got == want, got)
    status, headers, body = srv.req("GET", fx["request"]["path"],
                                    [("X-Plex-Container-Start", "0"), ("X-Plex-Container-Size", "100")])
    ids = re.findall(r'<Agent [^>]*identifier="([^"]*)"', body.decode("utf-8", "replace"))
    check("agents XML, one page of 100: every agent", ids == fx["unpaged_identifiers"], ids)


PQX = "repeat=0&own=1&includeChapters=1&includeMarkers=1&includeGeolocation=1&includeExternalMedia=1"
SHOW_URI = ("server%3A%2F%2Ff1x7u4e0000000000000000000000000000000f1%2Fcom.plexapp.plugins.library"
            "%2Flibrary%2Fmetadata%2F20001")


def new_queue(srv, client):
    status, doc = srv.json("POST", "/playQueues?type=video&extrasPrefixCount=0&uri=%s&continuous=1&%s"
                           % (SHOW_URI, PQX), client)
    mc = doc["MediaContainer"]
    return mc["playQueueID"], [row["playQueueItemID"] for row in mc["Metadata"]], mc["playQueueVersion"]


def queue_take(srv):
    """plexscratch42, 1.42.2: only a READ with `own=1` takes a queue another client made. A
    creator's own write with `own=1` (P's kill matrix sends the move, shuffle and unshuffle that
    way, the Plex Web shape) steps the version once and leaves the taker the owner. The native
    server took the queue back on any method: the move stepped the version twice, the taker's
    next read stepped it again, and the creator's next new queue retired the queue the other
    player was still playing (404 where the reference answers 200)."""
    fx = FIXTURE["play_queue"]
    pq, rows, version = new_queue(srv, "c83cp-creator")
    got = [("create", version)]
    read = lambda client: srv.json("GET", "/playQueues/%s?%s" % (pq, PQX), client)[1]["MediaContainer"]["playQueueVersion"]
    got.append(("other GET own=1", read("c83cp-other")))
    status, doc = srv.json("PUT", "/playQueues/%s/items/%s/move?after=%s&%s" % (pq, rows[-1], rows[0], PQX),
                           "c83cp-creator")
    got.append(("creator PUT move own=1", doc["MediaContainer"]["playQueueVersion"]))
    got.append(("other GET own=1", read("c83cp-other")))
    got.append(("other GET own=1", read("c83cp-other")))
    got.append(("creator GET own=1", read("c83cp-creator")))
    check("queue: version through a take and the creator's move", got == [tuple(x) for x in fx["versions"]], got)

    pq, rows, _ = new_queue(srv, "c83cp-creator2")
    read = lambda client: srv.json("GET", "/playQueues/%s?%s" % (pq, PQX), client)
    read("c83cp-other2")
    srv.json("PUT", "/playQueues/%s/items/%s/move?after=%s&%s" % (pq, rows[-1], rows[0], PQX), "c83cp-creator2")
    new_queue(srv, "c83cp-creator2")
    status = read("c83cp-other2")[0]
    check("queue: the creator's next queue leaves the one another client took",
          status == fx["creator_new_queue_then_other_reads_old"], status)

    pq, _, _ = new_queue(srv, "c83cp-creator3")
    srv.req("HEAD", "/playQueues/%s?own=1" % pq, {"Accept": "application/json"}, "c83cp-other3")
    version = srv.json("GET", "/playQueues/%s" % pq, "c83cp-third")[1]["MediaContainer"]["playQueueVersion"]
    check("queue: a HEAD with own=1 takes it too", version == fx["head_own_by_other_then_version"], version)


def queue_add(srv):
    """plexscratch42, 1.42.2: adding a SHOW or a SEASON to a queue adds its episodes -- a movie
    queue plus a six-episode show is seven rows, all leaves, and a season with `next=1` adds that
    season's three. The native server resolved the add's uri without the leaf expansion its
    create uses, so the queue got the show row itself, which has no Media to play."""
    fx = FIXTURE["queue_add"]
    for uri_key, label, nxt, case in (("20001", "show", "0", "add_show_next0"),
                                      ("30004", "season", "1", "add_season_next1")):
        status, doc = srv.json("POST", "/playQueues?type=video&extrasPrefixCount=0&uri=%s&%s"
                               % (SHOW_URI.replace("20001", "1007"), PQX), "c83cp-add-" + label)
        pq = doc["MediaContainer"]["playQueueID"]
        leaves = [row["ratingKey"] for row in
                  srv.json("GET", "/library/metadata/%s/%s" % (uri_key, "allLeaves" if label == "show" else "children"))
                  [1]["MediaContainer"]["Metadata"]]
        status, doc = srv.json("PUT", "/playQueues/%s?uri=%s&next=%s&%s"
                               % (pq, SHOW_URI.replace("20001", uri_key), nxt, PQX), "c83cp-add-" + label)
        rows = [(row["ratingKey"], row["type"]) for row in doc["MediaContainer"]["Metadata"]]
        want = [("1007", "movie")] + [(key, "episode") for key in leaves]
        check("queue add of a %s: the movie then its %d episodes (reference: 1 + %d)"
              % (label, len(leaves), len(fx[case]["after"]) - 1),
              status == 200 and rows == want and len(leaves) > 1, rows)


def main():
    srv = Server()
    try:
        agents_paged(srv)
        queue_take(srv)
        queue_add(srv)
    finally:
        srv.stop()
    print("PASS %d/%d" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
