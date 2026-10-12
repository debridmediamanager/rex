#!/usr/bin/env python3
"""Cycle 87 follow-ups (agent e3), each measured on plexscratch42 (PMS 1.42.2) and recorded
in `tests/fixtures/timeline_play_latch_s42.json`, in Plex Web 4.160's beat shape and
python-plexapi's `Playable.updateTimeline` shape:

- the `progress` answer belongs to the session's current PLAY -- (playQueueItemID, item) --
  and outlives a stop: a stop past the minute keeps the next play at `progress`, one at or
  under it does not, and another item's beat resets it; the resume-point deletion follows
  the same latch and never happens on a `stopped` beat;
- a playlist's own beats (`Playlist.updateTimeline`) keep their crossing position and
  latch on a track of their own, and their completion writes no history row; a queue BUILT
  from a playlist follows the item rules unchanged;
- a beat with NO `playQueueItemID` moves its client's own queue to the first row of its
  `ratingKey`; an EMPTY one (or `0`, `abc`, an unknown id) moves nothing.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `REX_SERVER=python`.

    python3 tests/test_followups_c87_e3.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import uuid

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "timeline_play_latch_s42.json")))
# Three movies of the fixture catalog standing in for plexscratch42's A, B and C, and one
# no queue holds for X.
ROWS = {"A": "1007", "B": "1008", "C": "1009", "X": "1012"}
WEB_CLIENT = "c87e3-web"
WEB = [("X-Plex-Product", "Plex Web"), ("X-Plex-Version", "4.160.0"),
       ("X-Plex-Client-Identifier", WEB_CLIENT), ("X-Plex-Platform", "Chrome"),
       ("X-Plex-Platform-Version", "151.0"),
       ("X-Plex-Features", "external-media,indirect-media,hub-style-list"),
       ("X-Plex-Model", "standalone"), ("X-Plex-Device", "Linux"),
       ("X-Plex-Device-Name", "Chrome"), ("X-Plex-Language", "en")]
PLEXAPI = {"X-Plex-Product": "PlexAPI", "X-Plex-Version": "4.18.2", "X-Plex-Platform": "Darwin",
           "X-Plex-Platform-Version": "24.6.0", "X-Plex-Device": "macOS-15.6-arm64-arm-64bit",
           "X-Plex-Device-Name": "probe-mac", "X-Plex-Language": "en", "X-Plex-Provides": "controller",
           "X-Plex-Sync-Version": "2"}
FLAGS = [("repeat", 0), ("own", 1), ("includeChapters", 1), ("includeMarkers", 1),
         ("includeGeolocation", 1), ("includeExternalMedia", 1)]
IIDS = {"none": "", "abc": "abc", "zero": "0", "bogus": "999999"}

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


def first(port, path):
    return (request(port, "GET", path)[1].get("Metadata") or [{}])[0]


def state(port, label):
    row = first(port, "/library/metadata/%s" % ROWS[label])
    return {"viewCount": row.get("viewCount"), "viewOffset": row.get("viewOffset")}


def history(port):
    body = request(port, "GET", "/status/sessions/history/all",
                   [("sort", "viewedAt:desc"), ("X-Plex-Container-Start", 0),
                    ("X-Plex-Container-Size", 100)])[1]
    return {str(r.get("historyKey")): str(r.get("ratingKey")) for r in body.get("Metadata", [])}


def web(client):
    return [(k, client if k == "X-Plex-Client-Identifier" else v) for k, v in WEB]


def make_queue(port, machine, labels, playlist=None, client=WEB_CLIENT):
    """Plex Web's queue create, over items or a playlist; returns (id, {label: rowID})."""
    if playlist:
        query = [("type", "video"), ("playlistID", playlist)] + FLAGS
    else:
        uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (
            machine, ",".join(ROWS[label] for label in labels))
        query = [("type", "video"), ("extrasPrefixCount", 0), ("uri", uri)] + FLAGS
    body = request(port, "POST", "/playQueues", query + web(client))[1]
    labels_by_key = {v: k for k, v in ROWS.items()}
    ids = {}
    for index, row in enumerate(body.get("Metadata", [])):
        label = labels_by_key.get(str(row["ratingKey"]), str(row["ratingKey"]))
        ids[label if label not in ids else label + str(index)] = row["playQueueItemID"]
    return body.get("playQueueID"), ids


def make_playlist(port, machine):
    uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s,%s" % (
        machine, ROWS["A"], ROWS["B"])
    body = request(port, "POST", "/playlists", [("type", "video"), ("title", "c87e3-tmp"),
                                                ("smart", 0), ("uri", uri)])[1]
    return str(body["Metadata"][0]["ratingKey"])


def selected(port, queue, ids):
    body = request(port, "GET", "/playQueues/%s" % queue, [("own", 1)] + WEB)[1]
    chosen = body.get("playQueueSelectedItemID")
    return {v: k for k, v in ids.items()}.get(chosen, chosen)


def beat(port, shape, session, state_, position, duration, iid, key, client=None, path=None):
    """One beat in the recorded shape: Plex Web 4.160's query order as plexlab's c45 capture
    holds it, or python-plexapi 4.18's `Playable.updateTimeline` (no session identifier)."""
    path = path or "/library/metadata/%s" % key
    if shape == "pa":
        query = [("ratingKey", key), ("key", path), ("identifier", "com.plexapp.plugins.library"),
                 ("time", position), ("state", state_), ("duration", duration)]
        if iid is not None:
            query.append(("playQueueItemID", iid))
        headers = dict(PLEXAPI, **{"X-Plex-Client-Identifier": client or session})
        return request(port, "GET", "/:/timeline", query, headers)
    query = [("ratingKey", key), ("key", path), ("playbackTime", position)]
    if iid is not None:
        query.append(("playQueueItemID", iid))
    query += [("state", state_), ("hasMDE", 1), ("time", position), ("duration", duration)]
    query += web(client or WEB_CLIENT) + [
        ("X-Plex-Playback-Session-Id", session + "-ps"), ("X-Plex-Playback-Id", session + "-pi"),
        ("X-Plex-Drm", "none"), ("X-Plex-Text-Format", "plain"),
        ("X-Plex-Provider-Version", "7.2"), ("X-Plex-Session-Identifier", session)]
    return request(port, "GET", "/:/timeline", query)


def sequences(port, machine):
    duration = int(first(port, "/library/metadata/%s" % ROWS["A"])["duration"])
    for name, sequence in sorted(FIXTURE["sequences"].items()):
        if sequence.get("skip_in_test"):
            continue
        shape, scenario = name.split("/")
        playlist = make_playlist(port, machine) if scenario.startswith("pl") else None
        keys = dict(ROWS, P=playlist)
        queue, ids = make_queue(port, machine, "ABC",
                                playlist if scenario.startswith("pl_") else None)
        before = history(port)
        sessions = {}
        answers, measured = [], []
        for step in sequence["beats"]:
            tag, state_, at, label, iid_label = step["spec"][:5]
            extra = step["spec"][5] if len(step["spec"]) > 5 else {}
            session = sessions.setdefault(tag, "c87e3-%s-%s-%s" % (name, tag, uuid.uuid4().hex[:6]))
            length = duration
            if label == "P":
                length = int(first(port, "/playlists/%s" % playlist)["duration"])
            if isinstance(at, str):
                position = int(length * float(at[1:]))
            else:
                position = int(length * at) if isinstance(at, float) else at
            iid = None if iid_label == "omit" else IIDS.get(iid_label, ids.get(iid_label))
            status, body = beat(port, shape, session, state_, position, length, iid, keys[label],
                                client=extra.get("client"),
                                path="/playlists/%s" % playlist if label == "P" else None)
            got = [status, body.get("playbackState"), body.get("viewCount"), body.get("viewOffset")]
            answer = step["answer"]
            want = [answer["status"], answer["playbackState"], answer["viewCount"], answer["viewOffset"]]
            if "selected" in step:
                got.append(selected(port, queue, ids))
                want.append(step["selected"])
            answers.append(got)
            measured.append(want)
        check("%s: every beat's status, playbackState, viewCount, viewOffset" % name,
              answers == measured, {"got": answers, "measured": measured})
        after = {label: state(port, label) for label in "ABC"}
        check("%s: A, B and C afterwards" % name, after == sequence["after"],
              {"got": after, "measured": sequence["after"]})
        labels = {v: k for k, v in keys.items() if v}
        new = sorted(labels.get(v, v) for k, v in history(port).items() if k not in before)
        check("%s: the history rows written" % name, new == sequence["history_new"],
              {"got": new, "measured": sequence["history_new"]})
        for label in "ABC":
            request(port, "GET", "/:/unscrobble",
                    [("key", ROWS[label]), ("identifier", "com.plexapp.plugins.library")])
        if playlist:
            request(port, "DELETE", "/playlists/%s" % playlist)


def queue_selection(port, machine):
    """The omitted-id queue moves (`queue_selection` in the fixture)."""
    measured = FIXTURE["queue_selection"]
    duration = int(first(port, "/library/metadata/%s" % ROWS["A"])["duration"])
    x_length = int(first(port, "/library/metadata/%s" % ROWS["X"])["duration"])

    def go(session, label, state_="playing", position=3000, length=duration):
        return beat(port, "web", session, state_, position, length, None, ROWS[label])[1].get(
            "playbackState")

    got = {}
    q1, i1 = make_queue(port, machine, "ABC")
    q2, i2 = make_queue(port, machine, "ABC")
    session = "c87e3-q-" + uuid.uuid4().hex[:6]
    got["two_queues"] = [go(session, "B"), {"q1": selected(port, q1, i1), "q2": selected(port, q2, i2)}]
    go(session, "B", "stopped", 4000)

    queue, ids = make_queue(port, machine, "ABCB")
    session = "c87e3-q-" + uuid.uuid4().hex[:6]
    row = [sorted(ids)]
    for label in "BCB":
        row += [go(session, label), selected(port, queue, ids)]
    got["dupes"] = row
    go(session, "B", "stopped", 4000)

    queue, ids = make_queue(port, machine, "ABC")
    session = "c87e3-q-" + uuid.uuid4().hex[:6]
    row = [go(session, "B"), selected(port, queue, ids)]
    row += [go(session, "X", position=1000, length=x_length), selected(port, queue, ids)]
    go(session, "X", "stopped", 1000, x_length)
    got["not_in_queue"] = row + [selected(port, queue, ids)]

    row = []
    for state_ in ("paused", "stopped", "buffering"):
        queue, ids = make_queue(port, machine, "ABC")
        session = "c87e3-q-" + uuid.uuid4().hex[:6]
        row.append([state_, go(session, "B", state_), selected(port, queue, ids)])
        go(session, "B", "stopped", 4000)
    got["states"] = row

    queue, ids = make_queue(port, machine, "ABC")
    session = "c87e3-q-" + uuid.uuid4().hex[:6]
    bare = [("ratingKey", ROWS["B"]), ("key", "/library/metadata/" + ROWS["B"])]
    status, body = request(port, "GET", "/:/timeline", bare + [("state", "playing")] + WEB
                           + [("X-Plex-Session-Identifier", session)])
    got["notime"] = [status, body.get("playbackState"), selected(port, queue, ids)]
    request(port, "GET", "/:/timeline", bare + [("state", "stopped")] + WEB
            + [("X-Plex-Session-Identifier", session)])

    playlist = make_playlist(port, machine)
    queue, ids = make_queue(port, machine, "", playlist)
    session = "c87e3-q-" + uuid.uuid4().hex[:6]
    got["playlist_queue"] = [sorted(ids), go(session, "B"), selected(port, queue, ids)]
    go(session, "B", "stopped", 4000)
    request(port, "DELETE", "/playlists/%s" % playlist)

    for case, value in sorted(got.items()):
        check("queue selection %s" % case, value == measured[case],
              {"got": value, "measured": measured[case]})


UA = "Plex Web/4.147.1 (rex-test)"
LIBRARY = "identifier=com.plexapp.plugins.library"


def ui_surface_replay(port):
    """test_ui_surface's c58t and c70 sequences as the suite sends them (`ui_surface_replay`)."""
    measured = FIXTURE["ui_surface_replay"]
    movie, spare = ROWS["A"], ROWS["B"]

    def ui_beat(key, state_, at, duration, session):
        path = ("/:/timeline?ratingKey=%s&key=%s&%s&state=%s&time=%d&duration=%d"
                % (key, urllib.parse.quote("/library/metadata/%s" % key), LIBRARY, state_, at, duration))
        return request(port, "GET", path, headers={
            "User-Agent": UA, "X-Plex-Session-Identifier": session,
            "X-Plex-Client-Identifier": "rex-ui-test", "X-Plex-Product": "Plex Web",
            "X-Plex-Platform": "Chrome"})[1]

    def app_beat(key, duration, position, state_, session, extra=()):
        query = urllib.parse.urlencode([
            ("key", "/library/metadata/%s" % key), ("ratingKey", key), ("playQueueItemID", "1"),
            ("duration", duration), ("time", position), ("playbackTime", position),
            ("hasMDE", "1"), ("context", "null:null"), ("state", state_),
            ("X-Plex-Playback-Session-Id", session), ("X-Plex-Playback-Id", "p70")] + list(extra))
        return request(port, "GET", "/:/timeline?" + query, headers={
            "User-Agent": UA, "X-Plex-Product": "Plex for Android (Mobile)",
            "X-Plex-Platform": "Android", "X-Plex-Client-Identifier": "c70-app",
            "X-Plex-Session-Id": session})[1]

    def offset(key):
        return first(port, "/library/metadata/%s" % key).get("viewOffset")

    request(port, "GET", "/:/unscrobble?key=%s&%s" % (movie, LIBRARY))
    answers = [ui_beat(movie, "playing", 200000, 1000000, "c58t-a"),
               ui_beat(movie, "stopped", 300000, 1000000, "c58t-a")]
    stored = offset(movie)
    answers += [ui_beat(movie, "playing", 30000, 1000000, "c58t-b"),
                ui_beat(movie, "stopped", 30000, 1000000, "c58t-b")]
    kept = offset(movie)
    answers.append(ui_beat(movie, "playing", 110000, 1000000, "c58t-c"))
    middle = offset(movie)
    answers.append(ui_beat(movie, "stopped", 30000, 1000000, "c58t-c"))
    got = {"answers": [[a.get("playbackState"), a.get("viewOffset")] for a in answers],
           "stored": stored, "kept": kept, "after_c_playing": middle, "dropped": offset(movie)}
    want = dict(measured["c58t"], answers=[list(a) for a in measured["c58t"]["answers"]])
    check("ui replay c58t: a STOPPED sub-minute beat keeps the resume point", got == want,
          {"got": got, "measured": want})
    request(port, "GET", "/:/unscrobble?key=%s&%s" % (movie, LIBRARY))

    duration = int(first(port, "/library/metadata/%s" % spare)["duration"])
    request(port, "PUT", "/:/unscrobble?%s&key=%s" % (LIBRARY, spare))
    near = duration * 95 // 100
    hist = [app_beat(spare, duration, 60000, "playing", "c70-hist"),
            app_beat(spare, duration, near, "playing", "c70-hist"),
            app_beat(spare, duration, near, "stopped", "c70-hist")]
    request(port, "PUT", "/:/scrobble?%s&key=%s" % (LIBRARY, spare))
    request(port, "PUT", "/:/unscrobble?%s&key=%s" % (LIBRARY, spare))
    cross = [app_beat(spare, duration, 0, "playing", "c70-zero"),
             app_beat(spare, duration, near, "playing", "c70-zero"),
             app_beat(spare, duration, near, "stopped", "c70-zero"),
             app_beat(spare, duration, 1, "playing", "c70-one"),
             app_beat(spare, duration, near, "playing", "c70-one"),
             app_beat(spare, duration, near, "stopped", "c70-one")]
    request(port, "PUT", "/:/unscrobble?%s&key=%s" % (LIBRARY, spare))
    offline = []
    for count in (1, 1, 0, None, "abc"):
        extra = [("offline", "1"), ("updated", str(int(time.time()) - 600))]
        if count is not None:
            extra.append(("viewCount", str(count)))
        answer = app_beat(spare, duration, 0, "stopped", "c70-off", extra)
        offline.append([answer.get("playbackState"), answer.get("viewCount"),
                        first(port, "/library/metadata/%s" % spare).get("viewCount")])
    for name, rows in (("history", hist), ("crossing", cross)):
        got = [[a.get("playbackState"), a.get("viewCount")] for a in rows]
        want = [list(a) for a in measured["c70"]["hist" if name == "history" else "cross"]]
        check("ui replay c70 %s block in suite order" % name, got == want,
              {"got": got, "measured": want})
    want = [list(a) for a in measured["c70"]["offline"]]
    check("ui replay c70 offline sync after a play stopped at 95%", offline == want,
          {"got": offline, "measured": want})
    request(port, "GET", "/:/unscrobble?key=%s&%s" % (spare, LIBRARY))


def near_end(port):
    """Near-end beats inside a play that passed the minute (`near_end`)."""
    duration = int(first(port, "/library/metadata/%s" % ROWS["A"])["duration"])
    for name, sequence in sorted(FIXTURE["near_end"].items()):
        shape = name.split("/")[0]
        if sequence["pre_scrobbled"]:
            request(port, "GET", "/:/scrobble?key=%s&%s" % (ROWS["A"], LIBRARY))
        session = "c87e3b-%s-%s" % (name, uuid.uuid4().hex[:6])
        got, want = [], []
        for state_, at, answer, count, offset_ in sequence["beats"]:
            position = int(duration * at) if isinstance(at, float) else at
            body = beat(port, shape, session, state_, position, duration, None, ROWS["A"])[1]
            got.append([body.get("playbackState"), body.get("viewCount"), body.get("viewOffset")])
            want.append([answer, count, offset_])
        check("near end %s" % name, got == want, {"got": got, "measured": want})
        request(port, "GET", "/:/unscrobble?key=%s&%s" % (ROWS["A"], LIBRARY))


def run(port):
    machine = request(port, "GET", "/identity")[1].get("machineIdentifier")
    sequences(port, machine)
    queue_selection(port, machine)
    ui_surface_replay(port)
    near_end(port)


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
    print("PASS %d/%d cycle 87 e3 follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
