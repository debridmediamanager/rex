#!/usr/bin/env python3
"""Cycle 84, territory B: what the native server got wrong that B's write chain caught.

- A media item's DELETE (`DELETE /library/metadata/{id}`, python-plexapi's `item.delete()`,
  drive59's `movie.delete()` and `episode.delete()`) pushes ONE `state 9` TimelineEntry for
  that row: the row's own type number, its `sectionID` and `metadataState: "deleted"` -- the
  shape every measured state-9 frame carries (a playlist's and a collection's delete, a
  merge's absorbed row, a library delete's rows; docs/findings.md, cycles 6, 18 and 22). The
  native server looked the row up AFTER removing it, so its frame was `type 1`,
  `sectionID ""` and no `metadataState` for every kind of row. Plex Web's timeline handler
  repopulates `/library/sections/{sectionID}` on a state-9 frame only when the sectionID is
  positive, so another open client's library never refetched after a delete.
- The same lookup kept the delete out of `/library/sections/{id}/timeline`, which lists the
  rows the server recently removed as `state 9` stubs (cycle 9; python-plexapi's
  `LibrarySection.timeline()` reads it).

- A deleted row leaves the play queues with the queue's VERSION stepped and the selection
  moved to a row that still exists (docs/findings.md, "A removed row leaves the play
  queues"; B's rlwrite76 restart leg read the shuffled show queue at version 1 on the
  native server and 2 on the oracle). The native server only filtered the row out at read
  time: the version never moved and a deleted selected item stayed selected.

A media-item DELETE itself is a 400 on both disposable copies (read-only media), so the
frame's TITLE for a media row is unmeasured and not asserted here.

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_parity_cycle84_b.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Client-Identifier": "parity-cycle84-b", "X-Plex-Product": "PlexAPI",
           "X-Plex-Version": "4.18.3", "X-Plex-Platform": "Darwin", "Accept": "application/json"}
# (ratingKey, kind, the TimelineEntry type number, section) in tests/catalog_ui.json
ROWS = (("1010", "episode", 4, "2"), ("1009", "movie", 1, "1"))

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


def request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=dict(HEADERS, Connection="close"))
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


def eventsource(port, trigger, seconds=3):
    """The SSE timeline frames pushed while `trigger` runs."""
    response = urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:%d/:/eventsource/notifications" % port,
        headers={"Accept": "text/event-stream"}), timeout=seconds + 5)
    threading.Timer(0.5, trigger).start()
    frames, name, deadline = [], None, time.time() + seconds
    try:
        while time.time() < deadline:
            try:
                line = response.readline()
            except Exception:
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("event:"):
                name = text.split(":", 1)[1].strip()
            elif text.startswith("data:") and name:
                frames.append((name, text.split(":", 1)[1].strip()))
                name = None
    finally:
        response.close()
    return frames


def run(port):
    for key, kind, number, section in ROWS:
        status, _ = request(port, "GET", "/library/metadata/%s" % key)
        check("the %s %s exists before the delete" % (kind, key), status == 200, status)
        outcome = {}

        def delete():
            outcome["status"] = request(port, "DELETE", "/library/metadata/%s" % key)[0]

        frames = eventsource(port, delete)
        check("DELETE /library/metadata/%s is 200" % key, outcome.get("status") == 200, outcome)
        entries = []
        for name, data in frames:
            if name != "timeline":
                continue
            entry = json.loads(data).get("TimelineEntry") or {}
            if str(entry.get("itemID")) == key and entry.get("state") == 9:
                entries.append(entry)
        check("the %s's delete pushes one state-9 frame" % kind, len(entries) == 1, frames)
        entry = entries[0] if entries else {}
        check("the %s's state-9 frame says metadataState deleted" % kind,
              entry.get("metadataState") == "deleted", entry)
        check("the %s's state-9 frame carries its own type number %d" % (kind, number),
              entry.get("type") == number, entry)
        check("the %s's state-9 frame names its section %s (Plex Web repopulates "
              "/library/sections/{sectionID} only for a positive id)" % (kind, section),
              str(entry.get("sectionID")) == section, entry)
        status, raw = request(port, "GET", "/library/sections/%s/timeline" % section)
        rows = (json.loads(raw).get("MediaContainer", {}).get("Metadata") or []) if status == 200 else []
        stub = [row for row in rows if str(row.get("ratingKey")) == key]
        check("/library/sections/%s/timeline lists the deleted %s as a state-9 stub" % (section, kind),
              len(stub) == 1 and stub[0].get("state") == 9
              and stub[0].get("metadataState") == "deleted", (status, raw[:400]))


def queue_checks(port):
    """A removed row leaves the play queues: the queue shrinks, its version steps and the
    selection moves to a row that still exists (docs/findings.md, measured on the reference
    with the delete cycle's season-removal pair)."""
    status, raw = request(port, "GET", "/identity")
    machine = json.loads(raw)["MediaContainer"]["machineIdentifier"]
    uri = ("server%%3A%%2F%%2F%s%%2Fcom.plexapp.plugins.library%%2Flibrary%%2Fmetadata%%2F30004"
           % machine)
    status, raw = request(port, "POST", "/playQueues?type=video&key=%%2Flibrary%%2Fmetadata%%2F1006&uri=%s"
                          % uri)
    queue = json.loads(raw)["MediaContainer"] if status == 200 else {}
    keys = [row.get("ratingKey") for row in queue.get("Metadata") or []]
    check("a season queue holds its four episodes, 1006 selected",
          keys == ["1005", "1006", "1010", "1011"]
          and str(queue.get("playQueueSelectedMetadataItemID")) == "1006", (status, keys, queue.get("playQueueSelectedMetadataItemID")))
    qid, version = queue.get("playQueueID"), queue.get("playQueueVersion")
    for gone, rest in (("1011", ["1005", "1006", "1010"]), ("1006", ["1005", "1010"])):
        request(port, "DELETE", "/library/metadata/%s" % gone)
        status, raw = request(port, "GET", "/playQueues/%s" % qid)
        after = json.loads(raw)["MediaContainer"] if status == 200 else {}
        keys = [row.get("ratingKey") for row in after.get("Metadata") or []]
        check("deleting queued %s takes it out of the queue" % gone,
              keys == rest and after.get("playQueueTotalCount") == len(rest), (keys, after.get("playQueueTotalCount")))
        check("deleting queued %s steps the queue's version (%s -> %s)" % (gone, version, (version or 0) + 1),
              after.get("playQueueVersion") == (version or 0) + 1, after.get("playQueueVersion"))
        version = after.get("playQueueVersion")
        selected = str(after.get("playQueueSelectedMetadataItemID"))
        item_ids = [row.get("playQueueItemID") for row in after.get("Metadata") or []]
        check("after deleting %s the selection is a row the queue still lists" % gone,
              selected in rest and after.get("playQueueSelectedItemID") in item_ids,
              (selected, after.get("playQueueSelectedItemID"), item_ids))


def main():
    process, port = start()
    try:
        queue_checks(port)
        run(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d cycle-84 B parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
