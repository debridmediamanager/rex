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
2. Section `search?type=18&query=` (cycle 83: "finds an empty collection on plexscratch42;
   both servers do not"). Measured on plexscratch and plexscratch42
   (`tests/fixtures/section_search_collections.json`): `type=18` searches the library's
   COLLECTIONS -- empty or not -- by title substring, case-blind, in a container whose
   viewGroup is `collection` with no viewMode, each row the `/all?type=18` row. Neither
   server searched collections at all. Beside it, kids: the title2 keeps the query
   verbatim (`'TeArS'`, `' TEARS '`) and a padded query is not trimmed.
3. The oracle's unlocked list writes (cycle 83): a row DELETE (and a merge) rewrites every
   playlist's item list -- read, filter, assign -- outside the lock the list writes have
   held since cycle 83. A playlist CLEAR landing between that read and assign is undone,
   and the cleared playlist comes back whole. The window is a list comprehension, so it is
   widened the only way a black-box test can: a generated catalog of 40,000 movies, two
   whole-library playlists ahead of the target (so the delete is still sweeping when a
   waiting thread's 5 ms GIL request lands) and the clear sent 0, 0.5 or 1 ms behind the
   delete. The unfixed oracle failed 9 and 10 of 300 pairs; the check asserts none.
   `--skip-race` leaves it out.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_c87_e2.py
"""
import http.client
import json
import os
import shutil
import tempfile
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
ORDER_FIXTURE = os.path.join(HERE, "fixtures", "show_cascade_order_s43.json")
SEARCH_FIXTURE = os.path.join(HERE, "fixtures", "section_search_collections.json")
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


def start(catalog=CATALOG):
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", catalog, "--assert-claimed",
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


# -- 2. the section search over collections --------------------------------------------

def container_of(body):
    return json.loads(body)["MediaContainer"]


def shape(mc):
    """What the fixture pins about a search container, minus per-server values."""
    return {"viewGroup": mc.get("viewGroup"), "viewMode": mc.get("viewMode"),
            "nocache": mc.get("nocache"), "title2": mc.get("title2"),
            "content": mc.get("content"),
            "titles": [row.get("title") for row in mc.get("Metadata") or []]}


def search_checks(port):
    fixture = json.load(open(SEARCH_FIXTURE))
    reference = {entry["request"][4:]: entry for entry in fixture["s43"] + fixture["s42"]}
    # The reference's own rule: every search row IS that collection's /all?type=18 row,
    # empty collections included.
    for server in ("s43", "s42"):
        listed = {row["ratingKey"]: row for entry in fixture[server] if "/all?type=18" in entry["request"]
                  for row in entry["body"]["MediaContainer"].get("Metadata") or []}
        rows = [(entry["request"], row) for entry in fixture[server] if "/search?" in entry["request"]
                for row in entry["body"]["MediaContainer"].get("Metadata") or []]
        check("reference %s: every type=18 search row is the collection's /all?type=18 row" % server,
              rows and all(listed.get(row["ratingKey"]) == row for _, row in rows),
              [path for path, row in rows if listed.get(row["ratingKey"]) != row])
    reference = {entry["request"][4:]: entry for entry in fixture["s43"] + fixture["s42"]
                 if "/search?" in entry["request"]}
    empty = reference["/library/sections/2/search?type=18&query=E2"]["body"]["MediaContainer"]
    check("reference: an EMPTY collection is found, in a `collection` container with no viewMode",
          [r.get("title") for r in empty.get("Metadata") or []] == ["E2Empty87"]
          and "childCount" not in empty["Metadata"][0]
          and empty.get("viewGroup") == "collection" and "viewMode" not in empty, empty)

    machine = container_of(request(port, "GET", "/identity")[1])["machineIdentifier"]
    base = "server://%s/com.plexapp.plugins.library/library/metadata/" % machine
    quote = urllib.parse.quote
    # The fixture's two collections, made the same way: an episode collection over the
    # SHOW's key holds nothing; a movie collection over one movie holds one.
    made = []
    for kind, title, section, key in ((4, "E2Empty87", 2, SHOW), (1, "E2One87", 1, "1007")):
        status, body = request(port, "POST", "/library/collections?type=%d&title=%s&smart=0"
                               "&sectionId=%d&uri=%s" % (kind, title, section, quote(base + key, safe="")))
        made.append(container_of(body)["Metadata"][0]["ratingKey"] if status == 200 else None)
    check("the two collections are created (one empty, one with a member)", all(made), made)
    ours_listed = {}
    for section in (1, 2):
        for row in container_of(request(port, "GET", "/library/sections/%d/all?type=18"
                                        % section)[1]).get("Metadata") or []:
            ours_listed[row["ratingKey"]] = row
    for path, entry in sorted(reference.items()):
        if "/search?" not in path or "/sections/" not in path:
            continue
        if not any(name in path for name in ("E2", "e2", "mpty8")):
            continue
        status, body = request(port, "GET", path)
        mc = container_of(body)
        want = shape(entry["body"]["MediaContainer"])
        check("%s answers the reference's container and rows" % path,
              status == entry["status"] and shape(mc) == want
              and all(ours_listed.get(row.get("ratingKey")) == row for row in mc.get("Metadata") or []),
              "status %s, got %s, want %s" % (status, shape(mc), want))
    # A query the fixture's rows rule out finds nothing, and the box keeps its spaces.
    for path, entry in ((p, e) for p, e in reference.items() if p.endswith("query=%20C49%20")
                        or p.endswith("query=zzz")):
        mc = container_of(request(port, "GET", path.replace("C49", "E2One"))[1])
        check("%s finds nothing and is titled verbatim" % path.replace("C49", "E2One"),
              not mc.get("Metadata") and mc.get("title2") == entry["body"]["MediaContainer"]["title2"]
              .replace("C49", "E2One"), shape(mc))
    for entry in fixture["kids"]:
        path = entry["request"][4:]
        want = entry["body"]["MediaContainer"]
        mc = container_of(request(port, "GET", path)[1])
        check("%s is titled %r and finds %s" % (path, want["title2"],
                                               "a row" if want.get("Metadata") else "nothing"),
              mc.get("title2") == want["title2"]
              and bool(mc.get("Metadata")) == bool(want.get("Metadata")), shape(mc))
    for key in made:
        if key:
            request(port, "DELETE", "/library/collections/%s" % key)


# -- 3. a row delete against a playlist clear ---------------------------------------------

RACE_ROWS, RACE_PAIRS = 40000, 300


def race_catalog(directory):
    """catalog_ui.json plus RACE_ROWS lean movies in section 1."""
    catalog = json.load(open(os.path.join(HERE, "catalog_ui.json")))
    for number in range(RACE_ROWS):
        key = 200000 + number
        catalog["items"].append({
            "ratingKey": str(key), "key": "/library/metadata/%d" % key,
            "guid": "plex://movie/%024x" % key, "type": "movie",
            "title": "Race %05d" % number, "year": 2020, "duration": 60000,
            "addedAt": 1789180050, "updatedAt": 1789180050,
            "librarySectionID": 1})
    path = os.path.join(directory, "catalog.json")
    with open(path, "w") as handle:
        json.dump(catalog, handle)
    return path


def race_checks():
    directory = tempfile.mkdtemp(prefix="plexsim-c87-e2-")
    try:
        process, port = start(race_catalog(directory))
        try:
            delete_clear_race(port)
        finally:
            stop(process)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def delete_clear_race(port):
    machine = container_of(request(port, "GET", "/identity")[1])["machineIdentifier"]
    library = urllib.parse.quote("server://%s/com.plexapp.plugins.library/library/sections/1/all"
                                 "?type=1" % machine, safe="")
    # Dict order is creation order: the two padding lists are swept BEFORE the target.
    for name in ("Pad%20A", "Pad%20B", "Target"):
        status, body = request(port, "POST", "/playlists?type=video&title=%s&smart=0&uri=%s"
                               % (name, library))
    target = container_of(body)["Metadata"][0]
    check("the target playlist holds the whole library",
          status == 200 and target.get("leafCount", 0) >= RACE_ROWS, (status, target.get("leafCount")))
    victims = ["%d" % (200000 + number) for number in range(RACE_PAIRS)]
    back, refused = [], []
    for number, victim in enumerate(victims):
        if number:
            request(port, "PUT", "/playlists/%s/items?uri=%s" % (target["ratingKey"], library))
        connections = [http.client.HTTPConnection("127.0.0.1", port, timeout=60) for _ in range(2)]
        for connection in connections:
            connection.connect()
        barrier, out = threading.Barrier(2), [None, None]

        def send(index, method, path):
            barrier.wait()
            if index:
                time.sleep((number % 3) * 0.0005)
            connection = connections[index]
            connection.request(method, path, headers=HEADERS)
            response = connection.getresponse()
            response.read()
            out[index] = response.status

        threads = [threading.Thread(target=send, args=(0, "DELETE", "/library/metadata/%s" % victim)),
                   threading.Thread(target=send, args=(1, "DELETE", "/playlists/%s/items"
                                                       % target["ratingKey"]))]
        [thread.start() for thread in threads]
        [thread.join() for thread in threads]
        [connection.close() for connection in connections]
        if out != [200, 200]:
            refused.append((victim, out))
        left = container_of(request(port, "GET", "/playlists/%s" % target["ratingKey"])[1])
        count = (left.get("Metadata") or [{}])[0].get("leafCount")
        if count:
            back.append((victim, count))
    check("a row delete and a playlist clear released together both answer 200",
          not refused, refused[:5])
    check("a cleared playlist stays cleared when a row delete races it (%d pairs)" % RACE_PAIRS,
          not back, "%d of %d came back, e.g. %s" % (len(back), RACE_PAIRS, back[:4]))


def main():
    process, port = start()
    try:
        order_checks(port)
        search_checks(port)
    finally:
        stop(process)
    if "--skip-race" not in sys.argv:
        race_checks()
    passed = sum(results)
    print("%s %d/%d cycle 87 E2 follow-up checks"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
