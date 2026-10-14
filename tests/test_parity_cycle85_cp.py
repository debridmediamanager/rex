#!/usr/bin/env python3
"""Cycle 85, territories C and P: what a play queue reports once rows stop existing.

Measured on plexscratch42 (PMS 1.42.2) with two throwaway TV libraries, and the window
half repeated on plexscratch (PMS 1.43.4); recorded in
`tests/fixtures/queue_section_delete_s42.jsonl` and re-read here, so each rule below is
checked against the reference's own exchange before it is applied to rex:

- `window` and `center` choose which rows are listed and never move the reported
  selection: `playQueueSelectedItemOffset` is the selection's index in the WHOLE queue
  (not its place in the slice, not the centre's) and `playQueueSelectedMetadataItemID`
  names the selected row. Both servers rebased the offset into the window; the oracle also
  read the metadata id at the rebased position.
- `DELETE /library/sections/{id}` leaves the section's rows QUEUED: no version step, the
  selection stays where it was. A read lists only the rows whose item still exists, while
  `playQueueTotalCount` counts every row, the offset indexes all of them, and
  `playQueueSelectedMetadataItemID` is absent once the selected row's item is gone. A
  queue with no live row still names its selection. A window slices the whole queue
  before the dead rows drop. Both servers counted and indexed only the live rows.

Drives a real server process (`tests/server_command.py`).

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
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "Accept": "application/json"}
# The fixture's reads after each delete, as (step, query) -- the same reads are sent to
# rex's mirrored queue.
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
    deletes = [entry["path"] for entry in lines if entry["method"] == "DELETE"]
    check("reference: two section DELETEs, each answered 200",
          deletes == ["/library/sections/10", "/library/sections/11"]
          and all(entry["status"] == 200 for entry in lines if entry["method"] == "DELETE"),
          deletes)

    def after(queue, prefix, query):
        for step, path, state in by_queue[queue]:
            if step.startswith(prefix) and path == "/playQueues/%s?own=1%s" % (queue, query):
                return state
        return None

    measured = 0
    for queue, deleted_section, prefix in (("2503", 10, "read-late"), ("2504", 10, "read-late"),
                                           ("2505", 10, "read-late"), ("2506", 11, "read0"),
                                           ("2507", 11, "read0")):
        # the last listing before the delete is the last write's answer (add/move/create)
        before = [state for step, path, state in by_queue[queue] if not step.startswith("read")][-1]
        gone = {row[0] for row in before["rows"] if row[2] == deleted_section}
        for query in WINDOW_READS + ("&center=8479",):
            got = after(queue, prefix, query)
            if got is None:
                continue
            measured += 1
            compare("reference: queue %s%s after DELETE /library/sections/%s follows the rule"
                    % (queue, query or " (plain)", deleted_section), got, expected(before, gone, query))
    check("reference: the rule was applied to every recorded post-delete read", measured == 9, measured)
    # The concrete facts the rule rests on, so a re-recorded fixture cannot drift silently.
    a = after("2503", "read-late", "")
    check("reference: a dead selected row keeps playQueueSelectedItemID, drops the metadata id, "
          "and the total still counts the dead rows",
          a["selected"] == 8464 and a["selectedKey"] is None and a["total"] == 3
          and a["version"] == 3 and a["size"] == 1, a)
    c = after("2505", "read-late", "")
    check("reference: a queue with no live row still names its selection and offset",
          c["size"] == 0 and c["selected"] == 8471 and c["offset"] == 1 and c["total"] == 3
          and c["version"] == 1, c)
    window_reads = [(step, path, state) for step, path, state in
                    sum((by_queue[q] for q in by_queue), [])
                    if step in ("readFw", "readFc", "readFcw", "rw", "rcw")]
    check("reference: window/center never move the offset (both versions: 2, the selection)",
          len(window_reads) == 5 and all(state["offset"] == 2 and state["selectedKey"] in ("2", "17")
                                         for _, _, state in window_reads),
          [(step, state["offset"], state["selectedKey"], len(state["rows"]))
           for step, _, state in window_reads])


# -- rex ---------------------------------------------------------------------------

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


def rex_checks(port):
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
    # A-E: the section-delete queues, mirrored on the TV library (section 2) beside movies.
    a = make(port, 1002, "A")
    add(port, a, 1003, "A")
    add(port, a, 1007, "A")
    b = make(port, 1007, "B")
    add(port, b, 1002, "B")
    add(port, b, 1003, "B")
    c = make(port, 30004, "C", 1006)
    d = make(port, 1007, "D")
    for key in (1002, 1003, 1008):
        add(port, d, key, "D")
    d_ids = [row[0] for row in read(port, d, "D")["rows"]]
    request(port, "PUT", "/playQueues/%s/items/%s/move?after=%s" % (d, d_ids[0], d_ids[2]), "D")
    e = make(port, 1002, "E")
    add(port, e, 1003, "E")
    add(port, e, 1007, "E")
    queues = [(a, "A"), (b, "B"), (c, "C"), (d, "D"), (e, "E")]
    before = {q: read(port, q, client) for q, client in queues}
    status = request(port, "DELETE", "/library/sections/2")[0]
    check("DELETE /library/sections/2 answers 200", status == 200, status)
    for q, client in queues:
        gone = {row[0] for row in before[q]["rows"] if row[2] == 2}
        queries = WINDOW_READS + (("&center=%s" % before[q]["rows"][-1][0],) if client == "E" else ())
        for query in queries:
            compare("queue %s%s after the section delete follows the rule" % (client, query or " (plain)"),
                    read(port, q, client, query), expected(before[q], gone, query))


def main():
    reference_checks()
    process, port = start()
    try:
        rex_checks(port)
    finally:
        stop(process)
    passed = sum(results)
    print("%s %d/%d cycle 85 CP queue checks" % ("PASS" if passed == len(results) else "FAIL",
                                                passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
