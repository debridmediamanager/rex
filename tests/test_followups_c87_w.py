#!/usr/bin/env python3
"""Cycle 87 W: the request families Plex Web 4.162.1 builds that 4.147.1 did not.

- `GET /media/providers/metadata/{id}` and `/media/providers/metadata/group/{id}` answer
  the listing's own row as a one-row container, paged like the listing; a non-numeric id
  is a 400 and an unknown one a 404. Driven by zen's recorded exchanges
  (`tests/fixtures/c87_w_provider_ids.json`). The oracle had this since cycle 44; the
  native port 404'd all of it.
- 4.162.1's section editor sends `action=create|update` to `/system/agents?mediaType=N`
  and `/system/scanners/{N}`. 1.42.2 (zen, the version plexsim reports) ignores it; only
  1.43.4 switches to a different list. Guards, measured on zen 2026-10-01.
- The clearLogo/squareArt artwork panes are gated on a server `shortVersion` of 1.43.0,
  so a 1.42.2 server is never asked; its answers are pinned anyway (zen: an empty
  `clearLogos` container, a 404 `squareArts`).

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_c87_w.py
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "c87_w_provider_ids.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "followups-c87-w",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.162.1",
           "X-Plex-Platform": "Chrome"}

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = dict(HEADERS, Connection="close")
    sent.update(headers or {})
    try:
        connection.request(method, path, headers=sent)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    try:
        return response.status, json.loads(raw) if raw else None
    except ValueError:
        return response.status, raw


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            if request(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def provider_ids(port):
    for exchange in json.load(open(FIXTURE)):
        status, body = request(port, exchange["method"], exchange["path"], exchange["headers"])
        want = exchange["body"]
        if want is None:
            check("%s is a %d (zen)" % (exchange["path"], exchange["status"]),
                  status == exchange["status"], status)
            continue
        # The rows are the four built-in modern agents, which plexsim holds under zen's
        # ids for movie/series/music, so the whole one-row container is comparable.
        check("%s is zen's one-row container" % exchange["path"],
              status == exchange["status"] and body == want, (status, body))


def action_grammar(port):
    _, bare = request(port, "GET", "/system/agents?mediaType=1")
    for action in ("create", "update"):
        status, body = request(port, "GET", "/system/agents?mediaType=1&action=" + action)
        rows = ((body or {}).get("MediaContainer") or {}).get("Agent") or []
        check("/system/agents?mediaType=1&action=%s ignores action on 1.42.2 (zen: 5 rows)"
              % action, status == 200 and body == bare and len(rows) == 5, (status, len(rows)))
    _, bare = request(port, "GET", "/system/scanners/1")
    status, body = request(port, "GET", "/system/scanners/1?action=create")
    # The list is XML whatever Accept says (cycle 87, W6): read the rows out of it.
    names = re.findall(r'<Scanner name="([^"]*)"/>', (body or b"").decode("utf-8")) \
        if isinstance(body, bytes) else []
    check("/system/scanners/1?action=create ignores action on 1.42.2 (zen: 4 scanners)",
          status == 200 and body == bare and names == [
              "Plex Movie", "Plex Movie Scanner", "Plex Video Files Scanner", "Plex Video Files"],
          (status, names))


def artwork_kinds(port):
    status, body = request(port, "GET", "/library/metadata/1007/clearLogos")
    container = (body or {}).get("MediaContainer") or {} if isinstance(body, dict) else {}
    check("clearLogos on 1.42.2 is an empty 200 container (zen)",
          status == 200 and container.get("size") == 0 and "Metadata" not in container,
          (status, body))
    status, _ = request(port, "GET", "/library/metadata/1007/squareArts")
    check("squareArts on 1.42.2 is a 404 (zen)", status == 404, status)


def main():
    process, port = start()
    try:
        provider_ids(port)
        action_grammar(port)
        artwork_kinds(port)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d cycle-87 W checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
