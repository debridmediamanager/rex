#!/usr/bin/env python3
"""Cycle 84, follow-ups F: what leaves a play queue when rows are removed.

Measured on plexscratch42 (PMS 1.42.2) with a throwaway TV library, recorded in
`tests/fixtures/queue_removal_s42.jsonl` and re-read here, so each rule below is checked
against the reference's own exchange before it is applied to rex:

- EVERY row a write removes leaves every play queue -- a season's `DELETE` takes its
  episodes out, and an episode or show `PUT .../merge` takes out the rows it absorbs. The
  oracle dropped only the key the DELETE named and nothing on a merge.
- The queue's VERSION steps once PER ROW that leaves (a season of three: 1 -> 4), not once
  per write.
- A selection that leaves moves to the FOLLOWING surviving row -- not the queue's head --
  and with no row after it the queue has NO selection: `size 0`, no rows, no selected item
  id, `playQueueSelectedItemOffset` 0 and `playQueueTotalCount` still counting the rows.
- A Fix Match that merges is a removal like any other (the native server left it queued).

And a request header's value is what follows the colon less ONE optional space, for every
header (cycle 50's rule): `X-Plex-Platform:  Roku` is not Roku and `X-Plex-Version:  9.7.1`
does not parse, measured over raw sockets on plexlab and plexscratch42
(`tests/fixtures/header_ows_roku.json`) through `/media/providers`' Roku pivot strip. The
oracle's parser stripped every leading space.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `REX_SERVER=python`.

    python3 tests/test_parity_cycle84_f.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "queue_removal_s42.jsonl")
OWS_FIXTURE = os.path.join(HERE, "fixtures", "header_ows_roku.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "Accept": "application/json"}

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


def request(port, method, path, client="parity-cycle84-f", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        sent = dict(HEADERS, Connection="close")
        sent["X-Plex-Client-Identifier"] = client
        sent.update(headers or {})
        connection.request(method, path, headers=sent)
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


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def view(container):
    """The fields the rules talk about, from one queue MediaContainer."""
    rows = container.get("Metadata") or []
    return {"version": container.get("playQueueVersion"),
            "total": container.get("playQueueTotalCount"),
            "selected": container.get("playQueueSelectedItemID"),
            "selectedKey": container.get("playQueueSelectedMetadataItemID"),
            "offset": container.get("playQueueSelectedItemOffset"),
            "size": container.get("size"),
            "rows": [(row.get("playQueueItemID"), str(row.get("ratingKey"))) for row in rows],
            "fields": sorted(key for key in container if key != "Metadata")}


# -- the reference ---------------------------------------------------------------------

def reference():
    """Replay the fixture's exchanges into per-queue histories."""
    lines = [json.loads(line) for line in open(FIXTURE)]
    queues = {}          # queue id -> list of (write that preceded it, view)
    last_write = None
    for entry in lines[1:]:
        body = entry.get("body")
        if entry["method"] in ("DELETE", "PUT"):
            last_write = "%s %s" % (entry["method"], entry["path"])
            continue
        if not entry["path"].startswith("/playQueues") or not isinstance(body, dict):
            continue
        mc = body["MediaContainer"]
        queues.setdefault(mc["playQueueID"], []).append(
            (None if entry["method"] == "POST" else last_write, view(mc)))
    return queues


def reference_checks(queues):
    """What the fixture says, asserted so a re-recorded fixture cannot drift silently."""
    season = queues[2408]            # a queue over season 802 (803, 804, 805)
    check("reference: a season DELETE empties a queue over that season's episodes",
          season[0][1]["rows"] and season[1][0] == "DELETE /library/metadata/802"
          and season[1][1]["rows"] == [] and season[1][1]["total"] is None, season)
    check("reference: the version steps once per row that left (3 rows: 1 -> 4)",
          season[0][1]["version"] == 1 and season[1][1]["version"] == 4, season)
    show = queues[2409]              # the whole show, 804 selected
    check("reference: a show queue loses the deleted season's episodes and keeps the rest",
          [k for _, k in show[1][1]["rows"]] == ["807", "808", "809"]
          and show[1][1]["version"] == 4, show[1])
    check("reference: the selection (804) moves to the FOLLOWING survivor 807",
          show[0][1]["selectedKey"] == "804" and show[1][1]["selectedKey"] == "807", show)
    middle = queues[2411]            # 807, 808, 809 with 808 selected; 808 deleted
    check("reference: a deleted selected episode hands the selection to the next row, "
          "not the head", middle[1][1]["selectedKey"] == "809"
          and middle[1][1]["rows"][0][1] == "807" and middle[1][1]["version"] == 2, middle)
    last = queues[2412]              # 807, 809 with 809 selected; 809 deleted
    check("reference: a deleted LAST selected row leaves no selection and no rows listed",
          last[1][1]["selected"] is None and last[1][1]["rows"] == []
          and last[1][1]["total"] == 1 and last[1][1]["offset"] == 0
          and last[1][1]["size"] == 0, last[1])
    merged_show = queues[2413]       # Gamma's episodes; Gamma merged into Beta
    check("reference: a show /merge takes the absorbed show's episodes out (+2 versions)",
          merged_show[1][0] == "PUT /library/metadata/810/merge?ids=814"
          and merged_show[1][1]["rows"] == [] and merged_show[1][1]["version"] == 3,
          merged_show)
    survivor = queues[2414]          # Beta's own queue: untouched by the show merge
    check("reference: the surviving show's queue is untouched by a show merge",
          survivor[1][1]["version"] == 1 and len(survivor[1][1]["rows"]) == 2, survivor)
    episode = queues[2415]           # 812, 813; 813 merged into 812
    check("reference: an episode /merge takes the absorbed episode out (+1 version)",
          [k for _, k in episode[1][1]["rows"]] == ["812"]
          and episode[1][1]["version"] == 2, episode)
    return last[1][1]["fields"]


# -- rex ---------------------------------------------------------------------------

def machine(port):
    return json.loads(request(port, "GET", "/identity")[1])["MediaContainer"]["machineIdentifier"]


def make(port, key, client, selected=None):
    uri = ("server%%3A%%2F%%2F%s%%2Fcom.plexapp.plugins.library%%2Flibrary%%2Fmetadata%%2F%s"
           % (machine(port), key))
    extra = "&key=%%2Flibrary%%2Fmetadata%%2F%s" % selected if selected else ""
    status, raw = request(port, "POST", "/playQueues?type=video&uri=%s&shuffle=0&repeat=0"
                          "&continuous=0%s" % (uri, extra), client)
    return json.loads(raw)["MediaContainer"]["playQueueID"] if status == 200 else None


def read(port, queue_id, client):
    status, raw = request(port, "GET", "/playQueues/%s?own=1" % queue_id, client)
    return view(json.loads(raw)["MediaContainer"]) if status == 200 else {"status": status}


def keys(state):
    return [key for _, key in state.get("rows") or []]


def deletes(no_selection_fields):
    """The Simpsons in catalog_ui: 1001 (s19), 1002-1003 (s20 = 30002), 1004 (s21),
    1005, 1006, 1010, 1011 (s22 = 30004)."""
    process, port = start()
    try:
        show = make(port, 20001, "f-show", 1002)
        season = make(port, 30004, "f-season")
        tail = make(port, 20001, "f-tail", 1011)
        request(port, "DELETE", "/library/metadata/30002")
        state = read(port, show, "f-show")
        check("DELETE season 30002 takes its episodes 1002 and 1003 out of a show queue",
              keys(state) == ["1001", "1004", "1005", "1006", "1010", "1011"]
              and state.get("total") == 6, state)
        check("the show queue's version steps once per row that left (1 -> 3)",
              state.get("version") == 3, state.get("version"))
        check("the selection (1002) moves to the FOLLOWING survivor 1004, not the head",
              state.get("selectedKey") == "1004"
              and state.get("selected") in [i for i, k in state.get("rows", []) if k == "1004"],
              state)
        state = read(port, season, "f-season")
        check("a queue holding none of the removed rows keeps its version (1)",
              state.get("version") == 1 and len(keys(state)) == 4, state)

        request(port, "DELETE", "/library/metadata/1011")
        state = read(port, tail, "f-tail")
        check("deleting the LAST, selected row leaves no selection: no selected item id",
              state.get("selected") is None and state.get("selectedKey") is None, state)
        check("... no rows listed and size 0, as the reference answers",
              state.get("rows") == [] and state.get("size") == 0, state)
        check("... playQueueTotalCount still counts the 5 rows and the offset is 0",
              state.get("total") == 5 and state.get("offset") == 0, state)
        check("... and the container carries exactly the reference's fields",
              state.get("fields") == no_selection_fields,
              (state.get("fields"), no_selection_fields))
        check("... with the version stepped for each removal (1 -> 3 -> 4)",
              state.get("version") == 4, state.get("version"))

        request(port, "DELETE", "/library/metadata/30004")
        state = read(port, season, "f-season")
        check("DELETE season 30004 empties the queue over it (three rows left: 2 -> 5)",
              state.get("rows") == [] and state.get("total") is None
              and state.get("version") == 5, state)
        state = read(port, show, "f-show")
        check("the show queue keeps 1001 and 1004, still selected, at version 7",
              keys(state) == ["1001", "1004"] and state.get("selectedKey") == "1004"
              and state.get("version") == 7, state)
    finally:
        stop(process)


def merges():
    process, port = start()
    try:
        on_absorbed = make(port, 30004, "f-merge-a", 1006)
        elsewhere = make(port, 30004, "f-merge-b", 1010)
        status, _ = request(port, "PUT", "/library/metadata/1005/merge?ids=1006")
        check("PUT /library/metadata/1005/merge?ids=1006 is 200", status == 200, status)
        state = read(port, on_absorbed, "f-merge-a")
        check("an episode merge takes the absorbed 1006 out of the queue",
              keys(state) == ["1005", "1010", "1011"] and state.get("total") == 3, state)
        check("... steps the version (1 -> 2)", state.get("version") == 2, state)
        check("... and moves the selection to the following row 1010",
              state.get("selectedKey") == "1010", state)
        state = read(port, elsewhere, "f-merge-b")
        check("another queue holding 1006 loses it too and keeps its selection 1010",
              keys(state) == ["1005", "1010", "1011"] and state.get("version") == 2
              and state.get("selectedKey") == "1010", state)

        # Fix Match onto a guid another row of the library already holds MERGES the two
        # (recorded: the matched row is deleted, the survivor keeps both Media), so the
        # matched row is a removed row like any other. The native server retired it and
        # left it queued: the version stood still and the selection named a 404.
        status, raw = request(port, "GET", "/library/metadata/1011")
        guid = json.loads(raw)["MediaContainer"]["Metadata"][0]["guid"]
        status, _ = request(port, "PUT", "/library/metadata/1010/match?guid=%s&name=Match"
                            % urllib.parse.quote(guid, safe=""))
        check("PUT /library/metadata/1010/match onto 1011's guid is 200", status == 200, status)
        check("... and the matched row 1010 is gone (merged into 1011)",
              request(port, "GET", "/library/metadata/1010")[0] == 404)
        for queue_id, client in ((on_absorbed, "f-merge-a"), (elsewhere, "f-merge-b")):
            state = read(port, queue_id, client)
            check("a Fix Match merge takes 1010 out of queue %s (rows 1005, 1011)" % client,
                  keys(state) == ["1005", "1011"] and state.get("total") == 2, state)
            check("... steps its version (2 -> 3)", state.get("version") == 3, state)
            check("... and moves its selection 1010 to the following row 1011",
                  state.get("selectedKey") == "1011"
                  and state.get("selected") in [i for i, k in state.get("rows", []) if k == "1011"],
                  state)
    finally:
        stop(process)


def raw_providers(port, lines):
    """`GET /media/providers` over a raw socket: http.client would normalise the very
    whitespace being measured."""
    head = [b"GET /media/providers HTTP/1.1", b"Host: 127.0.0.1:%d" % port,
            b"Accept: application/json", b"X-Plex-Client-Identifier: c84f-ows",
            b"X-Plex-Product: Plex for Roku", b"Connection: close"]
    head += [line.encode("latin-1") for line in lines]
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    try:
        sock.sendall(b"\r\n".join(head) + b"\r\n\r\n")
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    header, _, body = data.partition(b"\r\n\r\n")
    status = int(header.split(b" ", 2)[1])
    if b"transfer-encoding: chunked" in header.lower():
        out, rest = b"", body
        while rest:
            size, _, rest = rest.partition(b"\r\n")
            count = int(size, 16)
            if count == 0:
                break
            out, rest = out + rest[:count], rest[count + 2:]
        body = out
    container = json.loads(body)["MediaContainer"] if status == 200 else {}
    pivots = sum(len(row.get("Pivot") or [])
                 for provider in container.get("MediaProvider") or []
                 for feature in provider.get("Feature") or []
                 for row in feature.get("Directory") or []
                 if str(row.get("key") or "").startswith("/library/sections/"))
    return status, pivots


def header_whitespace():
    cells = json.load(open(OWS_FIXTURE))["cells"]
    process, port = start()
    try:
        for cell in cells:
            answers = list(cell["answers"].values())
            stripped = {answer["libraryPivots"] == 0 for answer in answers}
            check("reference: both servers agree on %s" % cell["name"], len(stripped) == 1,
                  cell["answers"])
            want = stripped.pop()
            status, pivots = raw_providers(port, cell["lines"])
            check("%r: library pivots %s, as both references answer"
                  % (cell["lines"], "withheld" if want else "kept"),
                  status == 200 and (pivots == 0) == want and (want or pivots > 0),
                  (status, pivots))
    finally:
        stop(process)


def main():
    fields = reference_checks(reference())
    deletes(fields)
    merges()
    header_whitespace()
    passed = sum(results)
    print("PASS %d/%d cycle-84 F parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
