#!/usr/bin/env python3
"""Cycle 86, territories C and P: a show cascade's activity progress on an exact half.

A show-wide re-read (the `showOrdering` write) steps its `Updating Metadata` activity per
season to `n * 89 / seasons`. Measured on plexscratch (PMS 1.43.4) with the copy's one
matched show, two-season Firefly, and recorded in
`tests/fixtures/show_cascade_progress_s43.json`: both the write and its undo step
-1 -> 44 -> 44 -> 89 -> 100, so the first season's 44.5 is reported as **44**. The library
scan's per-season step on the same show, measured beside it, reports 49.5 as **50**. The one
rule that gives both is round-half-to-even: truncation would have made the scan 49 and
round-half-up the cascade 45. The oracle (Python's `round`) already said 44; the native
server rounded half away from zero and said 45.

Re-read here from the fixture, then applied to rex's four-season show, whose second
season lands on the same 44.5.

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_parity_cycle86_cp.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "show_cascade_progress_s43.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "parity-cycle86-cp", "Accept": "application/json"}
SHOW = "20001"

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


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def updates(frames):
    """(subtitle, progress) of every activity `updated` frame, in order."""
    out = []
    for frame in frames:
        note = (frame.get("data") or {}).get("ActivityNotification") or {}
        if note.get("event") == "updated":
            activity = note.get("Activity") or {}
            out.append((activity.get("subtitle"), activity.get("progress")))
    return out


def reference_checks():
    """The fixture's own frames: the value a real PMS reports at one season of two."""
    fixture = json.load(open(FIXTURE))
    half = {}
    for cascade in fixture["cascades"]:
        steps = [p for _, p in updates(cascade["frames"]) if p is not None and p >= 0]
        check("reference: %s steps 44, 44, 89" % cascade["request"]["path"],
              steps == [44, 44, 89], steps)
        half["cascade"] = steps[0]
    scan = [p for _, p in updates(fixture["scan"]["frames"])
            if p is not None and 0 < p < 99]
    check("reference: a two-season scan reports its first season at 50", scan == [50], scan)
    half["scan"] = scan[0]
    # 44.5 -> 44 and 49.5 -> 50: only round-half-to-even gives both.
    check("reference: both halves round to even",
          half == {"cascade": round(89 / 2), "scan": round(99 / 2)}, half)
    return half


def listen(port, frames, ready, seconds):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    connection.request("GET", "/:/eventsource/notifications?filters=activity",
                       headers=dict(HEADERS, Accept="text/event-stream"))
    response = connection.getresponse()
    ready.set()
    event, until = None, time.time() + seconds
    while time.time() < until:
        try:
            line = response.readline()
        except OSError:
            # a quiet second between frames; the stream stays open until `seconds`
            continue
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip()
        if text.startswith("event:"):
            event = text[6:].strip()
        elif text.startswith("data:") and event == "activity":
            frames.append({"event": event, "data": json.loads(text[5:])})
    connection.close()


def capture(port, method, path, seconds=4.0):
    frames, ready = [], threading.Event()
    thread = threading.Thread(target=listen, args=(port, frames, ready, seconds), daemon=True)
    thread.start()
    ready.wait(10)
    time.sleep(0.3)
    status = request(port, method, path)[0]
    thread.join(seconds + 6)
    return status, frames


def rex_checks(port, half):
    status, body = request(port, "GET", "/library/metadata/%s/children" % SHOW)
    seasons = json.loads(body)["MediaContainer"]["Metadata"]
    check("the fixture show has four seasons (the second lands on n/len = 1/2)",
          status == 200 and len(seasons) == 4, (status, len(seasons)))
    status, frames = capture(port, "PUT", "/library/metadata/%s/prefs?showOrdering=dvd" % SHOW)
    steps = updates(frames)
    second = [p for subtitle, p in steps
              if subtitle and subtitle.endswith("S%02d" % int(seasons[1].get("index") or 0))]
    check("showOrdering=dvd answers 200", status == 200, status)
    # Cycle 87: a season's own step lands at its END (its opening `updated` carries the
    # previous season's progress), so the second season's step is its LAST update.
    check("the cascade reports two seasons of four at the reference's %d" % half["cascade"],
          second and second[-1] == half["cascade"], steps)
    status, frames = capture(port, "GET", "/library/sections/2/refresh")
    scan = [p for _, p in updates(frames) if p is not None and 0 < p < 99]
    check("the scan reports two seasons of four at the reference's %d" % half["scan"],
          status == 200 and len(scan) >= 2 and scan[1] == half["scan"], (status, scan))


def main():
    half = reference_checks()
    process, port = start()
    try:
        rex_checks(port, half)
    finally:
        stop(process)
    passed = sum(results)
    print("%s %d/%d cycle 86 CP cascade progress checks"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
