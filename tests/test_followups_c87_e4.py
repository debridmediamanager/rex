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
2. `GET /library/sections/{id}/all?type=18&title=` -- python-plexapi's
   `LibrarySection.collections(title=...)` and `collection(title)`. Measured on
   plexscratch (three probe collections) and plexscratch42 (its standing three, read
   only; `tests/fixtures/collection_title_filter.json`): `title=` is the listing's word
   prefix with the fold, `!=` an unfolded substring, `<=`/`>=` begins/ends, `==` finds
   nothing, empty collections included. Both servers already answered every one of the
   57 exchanges as the references do, so E2's "0 rows where ours find substrings" is not
   reproduced; the check is kept as the record.
3. A plain MERGE takes the absorbed row out of every playlist. Measured on plexscratch
   (`tests/fixtures/merge_playlist_s43.json`): after `PUT /library/metadata/A/merge?ids=B`
   the playlist answers without B, and B's playlistItemID is GONE -- a `move?after=` it
   sends the item to the front (an unknown `after`), a DELETE of it or a move of it is a
   404, and the split that follows does not bring it back. Both servers kept B's entry
   in the stored list and hid it at render, so the stale id still addressed a slot:
   the move landed after it and the DELETE answered 200.

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


# -- 2. a section's collections by title ----------------------------------------------

def make_collection(port, title, members):
    fields = [("type", "1"), ("title", title), ("smart", "0"), ("sectionId", "1")]
    if members:
        fields.append(("uri", item_uri(port, members)))
    query = urllib.parse.urlencode(fields)
    status, body = request(port, "POST", "/library/collections?" + query)
    key = (json.loads(body)["MediaContainer"]["Metadata"][0]["ratingKey"]
           if status == 200 else None)
    return key


def run_queries(port, exchanges):
    wrong = []
    for exchange in exchanges:
        path = exchange["request"].split(" ", 1)[1]
        status, mc = container(port, path)
        got = [(row.get("title"), row.get("childCount")) for row in mc.get("Metadata") or []]
        want = [(row["title"], row["childCount"]) for row in exchange["rows"]]
        if status != exchange["status"] or got != want:
            wrong.append((path.split("type=18", 1)[1], status, got, want))
    return wrong


def title_checks(port):
    fixture = json.load(open(TITLE_FIXTURE))
    member = "1007"
    made = [make_collection(port, title, [member]) for title in fixture["s43_collections"]]
    check("the plexscratch probe collections are created", all(made), made)
    wrong = run_queries(port, fixture["s43"])
    check("`all?type=18&title=` answers plexscratch's %d exchanges (word prefix, folded; "
          "`!=` substring)" % len(fixture["s43"]), not wrong, wrong[:4])
    for key in made:
        if key:
            request(port, "DELETE", "/library/collections/%s" % key)
    made = []
    for title, count in fixture["s42_collections"].items():
        # C52Amulti and C52Averify read NO childCount. Emptying a one-member collection
        # cannot make that row: plexscratch42 keeps `childCount` 1 through every removal
        # path a client sends (`/children/{id}` and `/items/{id}` DELETEs, the tag edit;
        # cycle 87 B2b, `tests/fixtures/collection_removal_paths_s42.json`), so this check
        # broke when the servers learned that rule. A collection that never held a member
        # -- created with no `uri`, measured there too -- carries none; that is the state
        # the two standing collections are in, however they got there.
        key = make_collection(port, title, [member] if count else [])
        made.append(key)
    wrong = run_queries(port, fixture["s42"])
    check("`all?type=18&title=` answers plexscratch42's %d exchanges, empty collections "
          "included" % len(fixture["s42"]), not wrong, wrong[:4])
    for key in made:
        if key:
            request(port, "DELETE", "/library/collections/%s" % key)


# -- 3. a merge and the playlists -----------------------------------------------------

def playlist_items(port, key):
    status, mc = container(port, "/playlists/%s/items" % key)
    return [(row.get("ratingKey"), row.get("playlistItemID"))
            for row in mc.get("Metadata") or []], mc.get("leafCount")


def merge_checks(port):
    fixture = json.load(open(MERGE_FIXTURE))
    # The reference's roles: the list was [C, B, A], B merged into A.
    before = fixture["playlist_before"]["GET /playlists/63/items"]["body"]["MediaContainer"]
    ref_c, ref_b, ref_a = [row["ratingKey"] for row in before["Metadata"]]

    def roles(rows, mapping):
        return [mapping.get(key, key) for key, _ in rows]

    ref_names = {ref_c: "C", ref_b: "B", ref_a: "A"}
    after = fixture["playlist_after_merge"]["GET /playlists/63/items"]["body"]["MediaContainer"]
    want_merged = [ref_names[row["ratingKey"]] for row in after["Metadata"]]
    split = fixture["playlist_after_split"]["GET /playlists/63/items"]["body"]["MediaContainer"]
    want_split = [ref_names[row["ratingKey"]] for row in split["Metadata"]]
    moved = fixture["stale_item"][0]["then"]["body"]["MediaContainer"]
    want_moved = [ref_names[row["ratingKey"]] for row in moved["Metadata"]]
    stale_delete, stale_move = (fixture["stale_item"][1]["status"],
                                fixture["stale_item"][2]["status"])

    c, b, a = "1013", "1008", "1009"
    names = {c: "C", b: "B", a: "A"}
    query = urllib.parse.urlencode([("type", "video"), ("title", "E4 merge"), ("smart", "0"),
                                    ("uri", item_uri(port, [c, b, a]))])
    status, body = request(port, "POST", "/playlists?" + query)
    playlist = json.loads(body)["MediaContainer"]["Metadata"][0]["ratingKey"]
    rows, _ = playlist_items(port, playlist)
    ids = dict(rows)
    status, _ = request(port, "PUT", "/library/metadata/%s/merge?ids=%s" % (a, b))
    rows, count = playlist_items(port, playlist)
    check("a merged-away row leaves the playlist (%s, leafCount %d)"
          % (want_merged, len(want_merged)),
          status == 200 and roles(rows, names) == want_merged and count == len(want_merged),
          (status, roles(rows, names), count))
    request(port, "PUT", "/library/metadata/%s/split" % a)
    rows, _ = playlist_items(port, playlist)
    check("the split that follows does not bring it back (%s)" % want_split,
          roles(rows, names) == want_split, roles(rows, names))
    status, _ = request(port, "PUT", "/playlists/%s/items/%s/move?after=%s"
                        % (playlist, ids[a], ids[b]))
    rows, _ = playlist_items(port, playlist)
    check("a move AFTER the absorbed row's playlistItemID goes to the front (%s)" % want_moved,
          status == 200 and roles(rows, names) == want_moved, (status, roles(rows, names)))
    status, _ = request(port, "PUT", "/playlists/%s/items/%s/move" % (playlist, ids[b]))
    check("a move OF the absorbed row's playlistItemID is a %d" % stale_move,
          status == stale_move, status)
    status, _ = request(port, "DELETE", "/playlists/%s/items/%s" % (playlist, ids[b]))
    check("a DELETE of the absorbed row's playlistItemID is a %d" % stale_delete,
          status == stale_delete, status)
    request(port, "DELETE", "/playlists/%s" % playlist)


def main():
    process, port = start()
    try:
        season_checks(port)
        title_checks(port)
        merge_checks(port)
    finally:
        stop(process)
    passed = sum(results)
    print("%s %d/%d cycle 87 E4 follow-up checks"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
