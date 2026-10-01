#!/usr/bin/env python3
"""Cycle 83, territory B: what the native server got wrong that B's regressions caught.

- An item's `/themes` and `/clearLogos` candidate rows are `Track` and `Photo` elements
  (`tests/fixtures/candidate_rows_kids.json`, recorded on kids). python-plexapi's
  `themes()` and `logos()` keep only rows of their own tag, so the native server's default
  `Video` element turned an uploaded theme or logo into an empty list
  (`plexapi/mixins/resources.py`, `fetchItems(..., cls=media.Theme|media.Logo)`).
- A play queue belongs to the client the QUERY names as well as the header: Plex Web sends
  every X-Plex-* in the query string, and a real PMS retires that client's older queue on
  its next create and moves ownership on an `own=1` read by that form
  (`tests/fixtures/playqueue_query_client_s42.json`). The native server read the header
  alone, so every Plex Web queue stayed alive and an `own=1` read took nothing.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle83_xb.py
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
# python-plexapi's own header set: no Accept, so the answer is XML.
HEADERS = {"X-Plex-Client-Identifier": "parity-cycle83-xb", "X-Plex-Product": "PlexAPI",
           "X-Plex-Version": "4.18.3", "X-Plex-Platform": "Darwin"}
MOVIE = "1008"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
       b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")
MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + b"\x00" * 413

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


def request(port, method, path, body=None, query_only=False):
    """`query_only`: no X-Plex-* header at all, Plex Web's shape (they ride in the query)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Connection": "close"} if query_only else dict(HEADERS, Connection="close")
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


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


def measured_elements():
    """The candidate row element of each list, read from the recorded exchange."""
    recorded = json.load(open(os.path.join(HERE, "fixtures", "candidate_rows_kids.json")))
    out = {}
    for exchange in recorded["exchanges"]:
        leaf = exchange["path"].rsplit("/", 1)[1]
        rows = list(ET.fromstring(exchange["body"].encode()))
        out[leaf] = {row.tag for row in rows}
    return out


def run(port):
    measured = measured_elements()
    check("fixture: kids answers Track rows on /themes and Photo rows on /clearLogos",
          measured == {"themes": {"Track"}, "clearLogos": {"Photo"}}, measured)
    for leaf, body in (("themes", MP3), ("clearLogos", PNG)):
        status, _ = request(port, "POST", "/library/metadata/%s/%s" % (MOVIE, leaf), body)
        check("POST /%s with a file is 200" % leaf, status == 200, status)
        status, raw = request(port, "GET", "/library/metadata/%s/%s" % (MOVIE, leaf))
        rows = list(ET.fromstring(raw)) if status == 200 else []
        uploads = [row for row in rows if (row.get("ratingKey") or "").startswith("upload://%s/" % leaf)]
        check("GET /%s lists the upload as one candidate" % leaf, len(uploads) == 1,
              raw[:300])
        tags = sorted({row.tag for row in uploads})
        check("the uploaded %s candidate is a %s element, as kids' rows are"
              % (leaf, "/".join(sorted(measured[leaf]))),
              uploads and set(tags) == measured[leaf], tags)
        check("the uploaded %s candidate is selected" % leaf,
              uploads and uploads[0].get("selected") == "1", uploads and uploads[0].attrib)
    # JSON carries no element name: the private key must not leak into it.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.request("GET", "/library/metadata/%s/themes" % MOVIE,
                       headers=dict(HEADERS, Accept="application/json", Connection="close"))
    raw = connection.getresponse().read()
    connection.close()
    check("the JSON listing carries no private key", not re.search(rb'"_', raw), raw[:300])


def queue_checks(port):
    measured = json.load(open(os.path.join(HERE, "fixtures", "playqueue_query_client_s42.json")))
    retire = [step["status"] for step in measured["retire"]]
    own = measured["own"]
    check("fixture: the first of two query-named queues is a 404, the second a 200",
          retire == [200, 200, 404, 200], retire)
    check("fixture: an own=1 read by a query-named client steps the version and retires its queue",
          own[2]["playQueueVersion"] == own[0]["playQueueVersion"] + 1 and own[3]["status"] == 404, own)
    status, raw = request(port, "GET", "/identity")
    machine = ET.fromstring(raw).get("machineIdentifier")
    uri = "server%%3A%%2F%%2F%s%%2Fcom.plexapp.plugins.library%%2Flibrary%%2Fmetadata%%2F1002" % machine

    def create(client):
        status, raw = request(port, "POST", "/playQueues?type=video&uri=%s&X-Plex-Client-Identifier=%s"
                              "&X-Plex-Product=Plex%%20Web" % (uri, client), query_only=True)
        root = ET.fromstring(raw)
        return root.get("playQueueID"), root.get("playQueueVersion")

    first, _ = create("xb-query-x")
    second, _ = create("xb-query-x")
    statuses = [request(port, "GET", "/playQueues/%s" % first, query_only=True)[0],
                request(port, "GET", "/playQueues/%s" % second, query_only=True)[0]]
    check("a query-named client's second queue retires its first (404, 200)",
          statuses == [404, 200], statuses)
    taken, version = create("xb-query-x")
    other, _ = create("xb-query-y")
    status, raw = request(port, "GET", "/playQueues/%s?own=1&X-Plex-Client-Identifier=xb-query-y" % taken,
                          query_only=True)
    after = ET.fromstring(raw).get("playQueueVersion") if status == 200 else None
    check("an own=1 read by a query-named client steps the version by one",
          after is not None and int(after) == int(version) + 1, (version, after))
    check("... and retires that client's other queue",
          request(port, "GET", "/playQueues/%s" % other, query_only=True)[0] == 404)


def main():
    process, port = start()
    try:
        run(port)
        queue_checks(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d cycle-83 B parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
