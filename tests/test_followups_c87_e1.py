#!/usr/bin/env python3
"""Cycle 87 follow-ups (agent e1), each measured on plexscratch42 (PMS 1.42.2):

- which item a `/:/timeline` beat is for, where a session's crossing position comes from,
  and how long a completion keeps answering `complete`, driven by the 30 beat sequences
  recorded in `tests/fixtures/timeline_queue_item_s42.json` (Plex Web's beat shape and
  python-plexapi's `updateTimeline`);
- a play queue that LOSES its selection (the selected last row removed with
  `DELETE /playQueues/{id}/items/{itemID}`), and the DELETE's own answer, read the way
  python-plexapi's `PlayQueue.get` reads it, with and without `center=`
  (`tests/fixtures/queue_lost_selection_s42.json`);
- the reads that list no rows at all: `window=0`, and a `center=` naming no row of the queue.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `REX_SERVER=python`.

    python3 tests/test_followups_c87_e1.py
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
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
# Plex Web 4.147's identity: every X-Plex-* rides in the QUERY.
WEB = {"X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1", "X-Plex-Platform": "Chrome",
       "X-Plex-Platform-Version": "140.0", "X-Plex-Device": "OSX", "X-Plex-Device-Name": "Chrome",
       "X-Plex-Model": "hosted", "X-Plex-Features": "external-media,indirect-media,hub-style-list",
       "X-Plex-Client-Identifier": "c87e1-web-chrome", "X-Plex-Language": "en",
       "X-Plex-Text-Format": "plain", "X-Plex-Provider-Version": "7.2", "X-Plex-Drm": "widevine"}
PLEXAPI = {"X-Plex-Product": "PlexAPI", "X-Plex-Version": "4.18.2", "X-Plex-Platform": "Darwin",
           "X-Plex-Platform-Version": "24.6.0", "X-Plex-Device": "macOS-15.6-arm64-arm-64bit",
           "X-Plex-Device-Name": "probe-mac", "X-Plex-Language": "en", "X-Plex-Provides": "controller",
           "X-Plex-Sync-Version": "2"}
QUEUE_FLAGS = [("repeat", 0), ("own", 1), ("includeChapters", 1), ("includeMarkers", 1),
               ("includeGeolocation", 1), ("includeExternalMedia", 1)]
# Three movies of the fixture catalog, standing in for plexscratch42's A, B and C.
ROWS = {"A": "1007", "B": "1008", "C": "1009"}

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


def request(port, method, path, query=(), headers=None):
    if query:
        path += "?" + urllib.parse.urlencode(list(query))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = {"Accept": "application/json", "Connection": "close"}
    sent.update(headers or {})
    try:
        connection.request(method, path, headers=sent)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    return response.status, (body or {}).get("MediaContainer", {}) if isinstance(body, dict) else {}


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


def row(port, key):
    return (request(port, "GET", "/library/metadata/%s" % key)[1].get("Metadata") or [{}])[0]


def web_queue(port, machine, keys):
    """Plex Web's queue create; returns {label: playQueueItemID}."""
    uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, ",".join(keys))
    status, body = request(port, "POST", "/playQueues",
                           [("type", "video"), ("extrasPrefixCount", 0), ("uri", uri)]
                           + QUEUE_FLAGS + list(WEB.items()))
    return status, body


def timeline_sequences(port, machine):
    fixture = json.load(open(os.path.join(HERE, "fixtures", "timeline_queue_item_s42.json")))
    duration = int(row(port, ROWS["A"])["duration"])
    for number, (name, sequence) in enumerate(sorted(fixture["sequences"].items())):
        status, body = web_queue(port, machine, [ROWS["A"], ROWS["B"], ROWS["C"]])
        by_key = {str(r["ratingKey"]): r["playQueueItemID"] for r in body.get("Metadata", [])}
        ids = {label: by_key.get(key) for label, key in ROWS.items()}
        if status != 200 or None in ids.values():
            check("timeline %s: the queue" % name, False, (status, ids))
            continue
        session = "c87e1-%s-%d" % (name, number)
        answers = []
        for state, at, label, sent_key, suffix, _, _ in sequence["beats"]:
            position = int(duration * at) if isinstance(at, float) else at
            key = ROWS[sent_key]
            item_id = {"omit": None, "none": "", "bogus": "999999"}.get(label, ids.get(label))
            if name.startswith("pa_"):
                # python-plexapi 4.18 `Video.updateTimeline`: its own query, its headers, no
                # session identifier (the client identifier is the session).
                query = [("ratingKey", key), ("key", "/library/metadata/%s" % key),
                         ("identifier", "com.plexapp.plugins.library"), ("time", position),
                         ("state", state), ("duration", duration)]
                if item_id is not None:
                    query.append(("playQueueItemID", item_id))
                headers = dict(PLEXAPI, **{"X-Plex-Client-Identifier": session})
            else:
                query = [("key", "/library/metadata/%s" % key), ("ratingKey", key)]
                if item_id is not None:
                    query.append(("playQueueItemID", item_id))
                query += [("duration", duration), ("time", position), ("playbackTime", position),
                          ("hasMDE", 1), ("context", "null:null"), ("state", state),
                          ("X-Plex-Playback-Session-Id", session + suffix),
                          ("X-Plex-Playback-Id", session + suffix + "p")] + list(WEB.items())
                headers = {"X-Plex-Session-Identifier": session + suffix}
            status, body = request(port, "GET", "/:/timeline", query, headers)
            answers.append([status, body.get("playbackState"), body.get("viewCount")])
        expected = [[200, beat[5], beat[6]] for beat in sequence["beats"]]
        check("timeline %s: every beat's playbackState and viewCount" % name,
              answers == expected, {"got": answers, "measured": expected})
        after = {label: row(port, ROWS[label]).get("viewCount") for label in ("A", "B")}
        check("timeline %s: the items watched afterwards" % name,
              after == sequence["after"], {"got": after, "measured": sequence["after"]})
        for label in ("A", "B"):
            request(port, "GET", "/:/unscrobble",
                    [("key", ROWS[label]), ("identifier", "com.plexapp.plugins.library")])


# Four movies of the fixture catalog for plexscratch42's A-D.
QUEUE_ROWS = {"A": "1007", "B": "1008", "C": "1009", "D": "1012"}


def queue_summary(status, body, labels, version):
    """The fields the fixture records, ids given as their row labels."""
    keys = {value: label for label, value in QUEUE_ROWS.items()}
    return {
        "status": status, "size": body.get("size"),
        "rows": [labels.get(r.get("playQueueItemID"), r.get("playQueueItemID"))
                 for r in body.get("Metadata", [])],
        "selectedItemID": labels.get(body.get("playQueueSelectedItemID"),
                                     body.get("playQueueSelectedItemID")),
        "selectedOffset": body.get("playQueueSelectedItemOffset"),
        "selectedMetadataItemID": keys.get(str(body.get("playQueueSelectedMetadataItemID")),
                                           body.get("playQueueSelectedMetadataItemID")),
        "totalCount": body.get("playQueueTotalCount"),
        "versionDelta": (body.get("playQueueVersion") or 0) - version,
        "keys": sorted(k for k in body if k.startswith("playQueue")),
    }


def read_query(step, ids):
    """The query of one recorded read, by its step name (see the fixture's provenance)."""
    def centre(label):
        return 999999 if label == "bogus" else ids[label]
    plexapi = {"own": 0, "window": 50, "includeBefore": 1, "includeAfter": 1}
    name = step.split("_")
    if step in ("plain_own1", "plain_after"):
        return [("own", 1)]
    if step == "plexapi_default":
        return list(plexapi.items())
    if step == "w0_only":
        return [("window", 0)]
    if step in ("center_only_B", "center_bogus_only"):
        return [("center", centre("B" if step == "center_only_B" else "bogus"))]
    if step == "center_bogus":
        return list(plexapi.items()) + [("center", 999999)]
    query = dict(plexapi)
    if name[0] in ("w0", "w1"):
        query["window"] = int(name[0][1])
    elif name[0] == "before0":
        query["includeBefore"] = 0
    elif name[0] == "after0":
        query["includeAfter"] = 0
    if name[-1] != "nocenter":
        query["center"] = centre(name[-1])
    return list(query.items())


def lists_nothing_by_rule(query, ids, removed):
    """`window=0`, or a `center=` naming no row the queue still holds."""
    query = dict(query)
    live = {ids[label] for label in ids if label not in removed}
    return query.get("window") == 0 or ("center" in query and query["center"] not in live)


def queue_reads(port, machine, empty_rule):
    """Replay every recorded case; `empty_rule` picks the reads the window=0 / unknown
    centre rule decides (True) or every other step (False)."""
    fixture = json.load(open(os.path.join(HERE, "fixtures", "queue_lost_selection_s42.json")))
    headers = dict(PLEXAPI, **{"X-Plex-Client-Identifier": "c87e1-plexapi-q"})
    for case, recorded in fixture["cases"].items():
        order, removed = recorded["order"], recorded["removed"]
        uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (
            machine, ",".join(QUEUE_ROWS[label] for label in order))
        status, body = request(port, "POST", "/playQueues",
                               [("type", "video"), ("uri", uri), ("shuffle", 0), ("repeat", 0),
                                ("continuous", 0),
                                ("key", "/library/metadata/%s" % QUEUE_ROWS[recorded["selected"]])],
                               headers)
        labels = {r["playQueueItemID"]: label
                  for r, label in zip(body.get("Metadata", []), order)}
        ids = {label: item for item, label in labels.items()}
        version = body.get("playQueueVersion") or 0
        queue = body.get("playQueueID")
        got = {"create": queue_summary(status, body, labels, version)}
        for label in removed:
            status, body = request(port, "DELETE", "/playQueues/%s/items/%s" % (queue, ids[label]),
                                   (), headers)
            got["delete_" + label] = queue_summary(status, body, labels, version)
        for step in recorded["steps"]:
            if step == "create" or step.startswith("delete_"):
                continue
            query = read_query(step, ids)
            status, body = request(port, "GET", "/playQueues/%s" % queue, query, headers)
            got[step] = queue_summary(status, body, labels, version)
        for step, measured in recorded["steps"].items():
            ruled = step not in ("create",) and not step.startswith("delete_") \
                and lists_nothing_by_rule(read_query(step, ids), ids, removed)
            if ruled != empty_rule:
                continue
            check("queue %s: %s" % (case, step), got.get(step) == measured,
                  {"got": got.get(step), "measured": measured})


def run(port):
    machine = request(port, "GET", "/identity")[1].get("machineIdentifier")
    timeline_sequences(port, machine)
    queue_reads(port, machine, empty_rule=False)
    queue_reads(port, machine, empty_rule=True)


def main():
    process, port = start()
    try:
        run(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d cycle 87 e1 follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
