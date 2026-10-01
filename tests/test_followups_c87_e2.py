#!/usr/bin/env python3
"""Cycle 87, agent E2: three follow-ups the cycle 83 and 86 reports left measured-not-fixed.

1. A show cascade's FRAME ORDER (cycle 86, "Measured and not acted on"). Measured on
   plexscratch (PMS 1.43.4) with the copy's one matched show, two-season Firefly, over a
   `showOrdering=dvd` write and its undo -- seven captures, every one the same 40 frames
   (`tests/fixtures/show_cascade_order_s43.json`). The fixture is reduced to roles (the
   show, the n-th season, the m-th episode of it) and checked against a model of the rule;
   the model is then applied to plexsim's four-season show, whose seasons hold 1, 2, 1
   and 4 episodes, and compared with what the server pushes. The media pass is left out
   of the comparison: it is state-dependent work on real files, none ran on the warm copy.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_c87_e2.py
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
ORDER_FIXTURE = os.path.join(HERE, "fixtures", "show_cascade_order_s43.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "followups-c87-e2", "Accept": "application/json"}
SHOW = "20001"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name,
                        "" if ok else "  -- " + str(detail)[:900]), flush=True)


def free_port():
    """A free port; from `PLEXSIM_TEST_PORTS=LO-HI` when it is set."""
    configured = os.environ.get("PLEXSIM_TEST_PORTS")
    candidates = range(*(int(p) + n for n, p in enumerate(configured.split("-")))) \
        if configured else [0]
    for candidate in candidates:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", candidate))
            return probe.getsockname()[1]
        except OSError:
            continue
        finally:
            probe.close()
    raise SystemExit("no free port in PLEXSIM_TEST_PORTS=%s" % configured)


def request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=dict(HEADERS, Connection="close", **(headers or {})))
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
    for _ in range(150):
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


# -- 1. the show cascade's frame order --------------------------------------------------

def listen(port, frames, ready, connections):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connections.append(connection)
    connection.request("GET", "/:/eventsource/notifications",
                       headers=dict(HEADERS, Accept="text/event-stream"))
    response = connection.getresponse()
    ready.set()
    event = None
    while True:
        try:
            line = response.readline()
        except (OSError, ValueError):
            break
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip()
        if text.startswith("event:"):
            event = text[6:].strip()
        elif text.startswith("data:") and event in ("timeline", "activity"):
            frames.append({"event": event, "data": json.loads(text[5:])})


def capture(port, method, path, seconds=3.0):
    frames, ready, connections = [], threading.Event(), []
    thread = threading.Thread(target=listen, args=(port, frames, ready, connections),
                              daemon=True)
    thread.start()
    ready.wait(10)
    time.sleep(0.3)
    status = request(port, method, path)[0]
    time.sleep(seconds)
    try:
        connections[0].sock.shutdown(socket.SHUT_RDWR)
    except (OSError, AttributeError, IndexError):
        pass
    thread.join(5)
    return status, list(frames)


def entries(frame, key):
    value = frame["data"].get(key)
    if value is None:
        value = (frame["data"].get("NotificationContainer") or {}).get(key)
    return value if isinstance(value, list) else [value] if value else []


def abstract(frames):
    """Frames -> role tokens: rows and subtitles named by order of first appearance."""
    roles, subtitles, tokens = {}, {}, []
    seasons, episodes = [0], {}
    # The media pass is a triple per leaf -- analyzing, thumbnailing, then a bare 5 -- and
    # its bare 5 is the frame right after the thumbnailing one.
    thumbnailed = None

    def role(entry):
        item = str(entry.get("itemID"))
        if item not in roles:
            kind = entry.get("type")
            if kind == 2:
                roles[item] = "show"
            elif kind == 3:
                seasons[0] += 1
                roles[item] = "S%d" % seasons[0]
            else:
                episodes[seasons[0]] = episodes.get(seasons[0], 0) + 1
                roles[item] = "S%dE%d" % (seasons[0], episodes[seasons[0]])
        return roles[item]

    for frame in frames:
        if frame["event"] == "timeline":
            for entry in entries(frame, "TimelineEntry"):
                media, thumbnailed = thumbnailed, None
                if entry.get("mediaState"):
                    if entry.get("mediaState") == "thumbnailing":
                        thumbnailed = entry.get("itemID")
                    continue
                if media is not None and media == entry.get("itemID") \
                        and entry.get("state") == 5 and not entry.get("metadataState"):
                    continue
                tokens.append(("T", role(entry), entry.get("state"), entry.get("metadataState")))
        else:
            thumbnailed = None
            for note in entries(frame, "ActivityNotification"):
                activity = note.get("Activity") or {}
                subtitle = activity.get("subtitle")
                if subtitle not in subtitles:
                    subtitles[subtitle] = "show" if not subtitles else "S%d" % len(subtitles)
                tokens.append(("A", note.get("event"), subtitles[subtitle], activity.get("progress")))
    return tokens


def model(counts):
    """The measured rule for a show whose seasons hold `counts` episodes."""
    out = [("T", "show", 3, "queued"), ("A", "started", "show", -1),
           ("T", "show", 3, "processing"), ("T", "show", 4, "loading")]
    progress = -1
    for number, count in enumerate(counts, 1):
        season = "S%d" % number
        out.append(("A", "updated", season, progress))
        out += [("T", season, 5, "processing"), ("T", season, 4, "loading")]
        for episode in range(1, count + 1):
            name = "%sE%d" % (season, episode)
            out += [("T", name, 5, "processing"), ("T", name, 4, "loading"),
                    ("T", name, 4, "loading")]
            if episode < count:
                out.append(("T", name, 5, None))
        progress = int(round(number * 89.0 / len(counts)))
        out.append(("A", "updated", season, progress))
        if number == len(counts):
            out.append(("A", "ended", season, 100))
        if count:
            out.append(("T", "%sE%d" % (season, count), 5, None))
        out.append(("T", season, 5, None))
    out.append(("T", "show", 5, None))
    return out


def order_checks(port):
    fixture = json.load(open(ORDER_FIXTURE))
    for cascade in fixture["cascades"]:
        tokens = abstract(cascade["frames"])
        check("reference: %s is the modelled 40-frame order (two seasons of three)"
              % cascade["request"]["path"],
              cascade["status"] == 200 and len(cascade["frames"]) == 40
              and tokens == model([3, 3]), tokens)
        activity = [t for t in tokens if t[0] == "A"]
        check("reference: the activity steps -1, -1, 44, 44, 89 and ends at 100",
              [t[3] for t in activity] == [-1, -1, 44, 44, 89, 100]
              and [t[2] for t in activity] == ["show", "S1", "S1", "S2", "S2", "S2"],
              activity)
    status, body = request(port, "GET", "/library/metadata/%s/children" % SHOW)
    seasons = json.loads(body)["MediaContainer"]["Metadata"]
    counts = []
    for season in seasons:
        children = json.loads(request(port, "GET", "/library/metadata/%s/children"
                                      % season["ratingKey"])[1])["MediaContainer"]
        counts.append(len(children.get("Metadata") or []))
    check("plexsim's show has four seasons of 1, 2, 1, 4 episodes", counts == [1, 2, 1, 4], counts)
    for value in ("dvd", ""):
        status, frames = capture(port, "PUT", "/library/metadata/%s/prefs?showOrdering=%s"
                                 % (SHOW, value))
        tokens = abstract(frames)
        want = model(counts)
        first = next((n for n, (a, b) in enumerate(zip(tokens, want)) if a != b),
                     min(len(tokens), len(want)))
        check("showOrdering=%s pushes the measured order (activity stepped at each season's "
              "end, no trailing 49)" % value,
              status == 200 and tokens == want,
              "status %s, %d vs %d tokens, first difference at %d: got %s want %s"
              % (status, len(tokens), len(want), first, tokens[first:first + 4],
                 want[first:first + 4]))


def main():
    process, port = start()
    try:
        order_checks(port)
    finally:
        stop(process)
    passed = sum(results)
    print("%s %d/%d cycle 87 E2 follow-up checks"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
