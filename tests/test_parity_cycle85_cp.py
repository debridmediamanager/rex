#!/usr/bin/env python3
"""Cycle 85, territories C and P: what a play queue reports about its selection.

Measured on plexscratch42 (PMS 1.42.2) with two throwaway TV libraries, and the window
half repeated on plexscratch (PMS 1.43.4); recorded in
`tests/fixtures/queue_section_delete_s42.jsonl` and re-read here, so each rule below is
checked against the reference's own exchange before it is applied to plexsim:

- `window` and `center` choose which rows are listed and never move the reported
  selection: `playQueueSelectedItemOffset` is the selection's index in the WHOLE queue
  (not its place in the slice, not the centre's) and `playQueueSelectedMetadataItemID`
  names the selected row. Both servers rebased the offset into the window; the oracle also
  read the metadata id at the rebased position.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle85_cp.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "queue_section_delete_s42.jsonl")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "Accept": "application/json"}
# The fixture's reads after each delete, as (step, query) -- the same reads are sent to
# plexsim's mirrored queue.
WINDOW_READS = ("", "&window=1", "&includeBefore=0&includeAfter=1")

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, method, path, client="parity-cycle85-cp"):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        sent = dict(HEADERS, Connection="close")
        sent["X-Plex-Client-Identifier"] = client
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
    rows = container.get("Metadata") or []
    return {"id": str(container.get("playQueueID")),
            "version": container.get("playQueueVersion"),
            "total": container.get("playQueueTotalCount"),
            "selected": container.get("playQueueSelectedItemID"),
            "selectedKey": container.get("playQueueSelectedMetadataItemID"),
            "offset": container.get("playQueueSelectedItemOffset"),
            "size": container.get("size"),
            "rows": [(row.get("playQueueItemID"), str(row.get("ratingKey")),
                      row.get("librarySectionID")) for row in rows]}


def expected(before, gone, query=""):
    """The rule: what a read answers after the rows in `gone` (item ids) stopped existing.

    `before` is the full read just before; `query` the read's window parameters."""
    params = dict(part.split("=", 1) for part in query.strip("&").split("&") if part)
    ids = [row[0] for row in before["rows"]]
    selected_at = ids.index(before["selected"])
    centre = params.get("center")
    at = ids.index(int(centre)) if centre and int(centre) in ids else selected_at
    rows = before["rows"]
    if "window" in params or "includeBefore" in params or "includeAfter" in params:
        span = int(params.get("window", len(rows)))
        low = max(0, at - span)
        first = params.get("includeBefore") != "0"
        last = params.get("includeAfter") != "0"
        rows = ((rows[low:at] if first else []) + (rows[at:at + 1] if first and last else [])
                + (rows[at + 1:at + span + 1] if last else []))
    alive = before["selected"] not in gone
    return {"version": before["version"], "total": len(before["rows"]),
            "selected": before["selected"], "offset": selected_at,
            "selectedKey": before["selectedKey"] if alive else None,
            "rows": [row for row in rows if row[0] not in gone]}


def compare(name, got, want):
    got_cut = {key: got.get(key) for key in want}
    got_cut["rows"] = [tuple(row[:2]) for row in got.get("rows") or []]
    want_cut = dict(want, rows=[tuple(row[:2]) for row in want["rows"]])
    check(name, got_cut == want_cut, "got %s want %s" % (got_cut, want_cut))


# -- the reference ---------------------------------------------------------------------

def reference():
    lines = [json.loads(line) for line in open(FIXTURE)][1:]
    reads = []
    for entry in lines:
        body = entry.get("body")
        if (entry["path"].startswith("/playQueues") and isinstance(body, dict)
                and "playQueueID" in body.get("MediaContainer", {})):
            reads.append((entry["step"], entry["method"], entry["path"], view(body["MediaContainer"])))
    return lines, reads


def reference_checks():
    lines, reads = reference()
    by_queue = {}
    for step, method, path, state in reads:
        by_queue.setdefault(state["id"], []).append((step, path, state))
    window_reads = [(step, path, state) for step, path, state in
                    sum((by_queue[q] for q in by_queue), [])
                    if step in ("readFw", "readFc", "readFcw", "rw", "rcw")]
    check("reference: window/center never move the offset (both versions: 2, the selection)",
          len(window_reads) == 5 and all(state["offset"] == 2 and state["selectedKey"] in ("2", "17")
                                         for _, _, state in window_reads),
          [(step, state["offset"], state["selectedKey"], len(state["rows"]))
           for step, _, state in window_reads])


# -- plexsim ---------------------------------------------------------------------------

def machine(port):
    return json.loads(request(port, "GET", "/identity")[1])["MediaContainer"]["machineIdentifier"]


def uri(port, key):
    return ("server%%3A%%2F%%2F%s%%2Fcom.plexapp.plugins.library%%2Flibrary%%2Fmetadata%%2F%s"
            % (machine(port), key))


def make(port, key, client, selected=None):
    extra = "&key=%%2Flibrary%%2Fmetadata%%2F%s" % selected if selected else ""
    status, raw = request(port, "POST", "/playQueues?type=video&uri=%s&shuffle=0&repeat=0"
                          "&continuous=0&own=1%s" % (uri(port, key), extra), client)
    return json.loads(raw)["MediaContainer"]["playQueueID"]


def add(port, queue_id, key, client):
    return request(port, "PUT", "/playQueues/%s?uri=%s&next=0" % (queue_id, uri(port, key)), client)[0]


def read(port, queue_id, client, query=""):
    status, raw = request(port, "GET", "/playQueues/%s?own=1%s" % (queue_id, query), client)
    return view(json.loads(raw)["MediaContainer"]) if status == 200 else {"status": status}


def plexsim_checks(port):
    # F: the 1.42.2 control, mirrored on movies (2, 297, 299, 300 -> 1007, 1008, 1009, 1012).
    f = make(port, 1007, "F")
    for key in (1008, 1009, 1012):
        add(port, f, key, "F")
    ids = [row[0] for row in read(port, f, "F")["rows"]]
    request(port, "PUT", "/playQueues/%s/items/%s/move?after=%s" % (f, ids[0], ids[2]), "F")
    whole = read(port, f, "F")
    for query in ("&window=1", "&center=%s" % ids[3], "&center=%s&window=1" % ids[3]):
        compare("window/center list a slice and keep the offset on the selection (%s)" % query,
                read(port, f, "F", query), expected(whole, set(), query))


def main():
    reference_checks()
    process, port = start()
    try:
        plexsim_checks(port)
    finally:
        stop(process)
    passed = sum(results)
    print("%s %d/%d cycle 85 CP queue checks" % ("PASS" if passed == len(results) else "FAIL",
                                                passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
