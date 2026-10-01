#!/usr/bin/env python3
"""Cycle 87, agent E4: three follow-ups E2 left measured-not-fixed or unmeasured.

1. A SEASON refresh's frame ORDER. Measured on plexscratch (PMS 1.43.4) over three
   refreshes of Firefly's two three-episode seasons, frame-identical
   (`tests/fixtures/season_cascade_order_s43.json`): the season's `queued` frame comes
   BEFORE `activity started`, and the activity ENDS between the last episode's second
   `loading` and that episode's bare 5. The copy's seasons are `local://` and open with
   a `matching`/5 pair a refresh of its matched show does not push; that opener is the
   unmatched row's and is dropped before the comparison. The fixture is reduced to roles
   and checked against a model of the rule, and the model is applied to plexsim's
   one- and four-episode seasons.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_c87_e4.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
SEASON_FIXTURE = os.path.join(HERE, "fixtures", "season_cascade_order_s43.json")
TITLE_FIXTURE = os.path.join(HERE, "fixtures", "collection_title_filter.json")
MERGE_FIXTURE = os.path.join(HERE, "fixtures", "merge_playlist_s43.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "followups-c87-e4", "Accept": "application/json"}

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


def container(port, path):
    status, body = request(port, "GET", path)
    try:
        return status, json.loads(body).get("MediaContainer") or {}
    except ValueError:
        return status, {}


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(600):
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


def machine(port):
    return container(port, "/identity")[1].get("machineIdentifier")


def item_uri(port, keys):
    return ("server://%s/com.plexapp.plugins.library/library/metadata/%s"
            % (machine(port), ",".join(str(k) for k in keys)))


# -- 1. the season cascade's frame order ------------------------------------------------

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


def capture(port, method, path, seconds=2.0):
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


def abstract(frames, season_key):
    """Frames -> role tokens: `S` the season, `E<n>` its n-th episode by first appearance;
    the activity as (event, progress, subtitle-is-the-season's-frame-title)."""
    roles, tokens, season_title = {str(season_key): "S"}, [], None
    for frame in frames:
        if frame["event"] == "timeline":
            for entry in entries(frame, "TimelineEntry"):
                item = str(entry.get("itemID"))
                if item not in roles:
                    roles[item] = "E%d" % len(roles)
                if roles[item] == "S":
                    season_title = entry.get("title")
                tokens.append(("T", roles[item], entry.get("state"),
                               entry.get("metadataState") or "-"))
        else:
            for note in entries(frame, "ActivityNotification"):
                activity = note.get("Activity") or {}
                tokens.append(("A", note.get("event"), activity.get("progress"),
                               activity.get("subtitle") == season_title))
    return tokens


def model(episodes):
    """The measured rule for a season of `episodes` episodes."""
    out = [("T", "S", 3, "queued"), ("A", "started", -1, True),
           ("T", "S", 3, "processing"), ("T", "S", 4, "loading")]
    for n in range(1, episodes + 1):
        out += [("T", "E%d" % n, 5, "processing"), ("T", "E%d" % n, 4, "loading"),
                ("T", "E%d" % n, 4, "loading")]
        if n < episodes:
            out.append(("T", "E%d" % n, 5, "-"))
    out.append(("A", "ended", 100, True))
    if episodes:
        out.append(("T", "E%d" % episodes, 5, "-"))
    out.append(("T", "S", 5, "-"))
    return out


def season_checks(port):
    fixture = json.load(open(SEASON_FIXTURE))
    for number, cascade in enumerate(fixture["cascades"], 1):
        season = cascade["request"]["path"].split("/")[3]
        tokens = abstract(cascade["frames"], season)
        # The `local://` opener: the season's `state 2 matching` and the bare 5 after it.
        if tokens[:2] == [("T", "S", 2, "matching"), ("T", "S", 5, "-")]:
            tokens = tokens[2:]
        check("the measured rule reproduces plexscratch's season refresh #%d" % number,
              tokens == model(3), "\n%s\n%s" % (tokens, model(3)))
    for season, count in (("30004", 4), ("30001", 1)):
        status, frames = capture(port, "PUT", "/library/metadata/%s/refresh" % season)
        tokens = abstract(frames, season)
        check("a %d-episode season refresh pushes the measured order (queued first, "
              "ended before the last episode's 5)" % count,
              status == 200 and tokens == model(count),
              "status %s\n got %s\nwant %s" % (status, tokens, model(count)))


def main():
    process, port = start()
    try:
        season_checks(port)
    finally:
        stop(process)
    passed = sum(results)
    print("%s %d/%d cycle 87 E4 follow-up checks"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
