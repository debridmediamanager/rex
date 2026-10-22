#!/usr/bin/env python3
"""`--state`, kind by kind: every piece of mutable state the state file keeps survives.

`test_persist.py` proves the store works for a dozen writes at once. This suite
walks the whole list the state file holds -- the catalog's rows and sections, the
library's lists, tag table, history and counters, the server's preferences, section
and agent settings, provider groups, uploads (and their bytes), minted theme rows,
play and download queues, and the session module's device and statistics tables --
and checks each KIND on its own, so a server that drops one of them names it.

For each kind: the write the client sends, then every surface that shows it, read
before a SIGTERM and again after a restart on the same state file. A kind passes when
the reads answer identically, and only counts when they differ from a fresh instance's
(otherwise the write never landed and the comparison proves nothing). An id-bearing
kind also creates one more of its thing after the restart, and that id must be the one
a server that never restarted hands out: the counter continues rather than restarts.

The catalog-rebuild rule (`restore_state`, `rust/src/runtime.rs`) is checked last: over a
changed catalog only the view state -- matched by guid, then by part file -- and the
server-level settings carry over, and the old file is kept as `<file>.stale`.

    python3 tests/test_parity_persist.py
"""
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

from server_command import server_command


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LIBRARY = "com.plexapp.plugins.library"
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-persist",
           "X-Plex-Device-Name": "Parity Persist", "X-Plex-Platform": "Chrome",
           "X-Plex-Product": "Plex Web"}
# Two distinct payloads per upload kind: the candidate key is the SHA-1 of the bytes.
POSTER = b"\x89PNG\r\n\x1a\n" + b"persisted poster bytes" * 8
ART = b"\x89PNG\r\n\x1a\n" + b"persisted art bytes" * 8
THEME = b"ID3" + b"persisted theme bytes" * 8
LOGO = b"\x89PNG\r\n\x1a\n" + b"persisted logo bytes" * 8
SUBTITLE = b"1\n00:00:01,000 --> 00:00:02,000\nKept across a restart\n"

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + detail))


def free_port():
    # `REX_PORT_RANGE=LOW-HIGH` confines the servers to a range shared agents
    # have split between them; otherwise the kernel picks.
    bounds = os.environ.get("REX_PORT_RANGE")
    candidates = range(int(bounds.split("-")[0]), int(bounds.split("-")[1]) + 1) \
        if bounds else [0]
    for candidate in candidates:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", candidate))
            return probe.getsockname()[1]
        except OSError:
            continue
        finally:
            probe.close()
    raise SystemExit("no free port in %s" % bounds)


class Instance:
    def __init__(self, catalog, state=None):
        self.port = free_port()
        command = server_command("--port", self.port, "--catalog", catalog,
                                 "--gdm", "off", "--assert-claimed", root=ROOT)
        if state:
            command += ["--state", state]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   REX_PLEXTV_URL="http://127.0.0.1:9")
        for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
            env.pop(name, None)
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(command, cwd=ROOT, env=env,
                                     stdout=subprocess.DEVNULL, stderr=self.log)
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.1)
        self.log.seek(0)
        raise SystemExit("rex did not start:\n" + self.log.read().decode(errors="replace"))

    def request(self, method, path, headers=None, body=None):
        """(status, body): JSON parsed, anything else as its length and SHA-1."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=body, headers=dict(HEADERS, **(headers or {})))
            response = conn.getresponse()
            raw = response.read()
            kind = response.getheader("Content-Type") or ""
        finally:
            conn.close()
        if "json" in kind:
            try:
                return response.status, json.loads(raw) if raw else None
            except ValueError:
                pass
        return response.status, {"type": kind.split(";")[0], "length": len(raw),
                                  "sha1": hashlib.sha1(raw).hexdigest()}

    def stop(self, sig=signal.SIGTERM):
        self.proc.send_signal(sig)
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def output(self):
        self.log.seek(0)
        return self.log.read().decode(errors="replace")


def differences(before, after, where="", out=None):
    """The first few leaf paths where two answers differ, for a readable failure."""
    out = [] if out is None else out
    if len(out) >= 6:
        return out
    if isinstance(before, dict) and isinstance(after, dict):
        for key in list(before) + [k for k in after if k not in before]:
            if before.get(key, "<absent>") != after.get(key, "<absent>"):
                differences(before.get(key, "<absent>"), after.get(key, "<absent>"),
                            "%s.%s" % (where, key), out)
    elif isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)) \
            and len(before) == len(after):
        for index, (one, two) in enumerate(zip(before, after)):
            if one != two:
                differences(one, two, "%s[%d]" % (where, index), out)
    else:
        out.append("%s: %s -> %s" % (where or "body", json.dumps(before)[:160],
                                      json.dumps(after)[:160]))
    return out


def container(body):
    return (body or {}).get("MediaContainer") or {} if isinstance(body, dict) else {}


def rows(body):
    return container(body).get("Metadata") or []


def first(body):
    return (rows(body) or [{}])[0]


def uri(machine, key):
    return urllib.parse.quote("server://%s/%s/library/metadata/%s" % (machine, LIBRARY, key),
                              safe="")


def q(text):
    return urllib.parse.quote(str(text), safe="")


def timeline(instance, key, state, at, session, duration=600000):
    return instance.request(
        "GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s&state=%s&time=%d"
               "&duration=%d" % (key, key, state, at, duration),
        {"X-Plex-Session-Identifier": session, "X-Plex-Client-Identifier": "persist-player",
         "X-Plex-Device-Name": "Persist Player", "X-Plex-Product": "Plex Web",
         "X-Plex-Platform": "Chrome"})


# -- the kinds ------------------------------------------------------------------
#
# Each kind: `write(instance, ctx)` performs the client's write and records what it
# created in `ctx`; `reads(ctx)` the surfaces that show it; `again(instance, ctx)`, for
# an id-bearing kind, creates one more and returns the id it was given.
#
# The rows are shared out so each kind writes rows of its own: 1007 is uploads, 1008
# edits, 1009 Continue Watching, 1012/1014 a merge, 1013 subtitles and a resume point,
# 1001-1006 the show's view state, 1010 a delete. The one effect that crosses kinds is
# section 2's `flattenSeasons`, projected onto every episode read, so losing it fails
# every kind that reads an episode as well as its own.

def w_server_prefs(inst, ctx):
    # The Settings page, Scheduled Tasks included: the butler's window is a server pref.
    return [inst.request("PUT", "/:/prefs?FriendlyName=Persisted%20Server"
                                "&ButlerStartHour=3&ButlerEndHour=6")[0]]


def w_section_prefs(inst, ctx):
    return [inst.request("PUT", "/library/sections/2/prefs?flattenSeasons=1&episodeSort=1")[0],
            inst.request("PUT", "/library/sections/1/prefs?enableCinemaTrailers=0"
                                "&autoCollectionThreshold=3")[0]]


def w_item_prefs(inst, ctx):
    return [inst.request("PUT", "/library/metadata/20001/prefs?episodeSort=0"
                                "&showOrdering=aired")[0]]


def w_agent_order(inst, ctx):
    return [inst.request("PUT", "/system/agents/com.plexapp.agents.imdb/config/1?order="
                                "com.plexapp.agents.imdb%2Ccom.plexapp.agents.localmedia")[0]]


def w_provider_group(inst, ctx):
    status, body = inst.request("POST", "/media/providers/metadata/group?title=Kept%20Group"
                                        "&primaryIdentifier=tv.plex.agents.movie")
    group = (container(body).get("MetadataAgentProviderGroup") or [{}])[0]
    ctx["group"] = group.get("id")
    renamed = inst.request("PUT", "/media/providers/metadata/group/%s?title=Kept%%20Group%%20Renamed"
                           % ctx["group"])[0]
    return [status, renamed]


def again_provider_group(inst, ctx):
    body = inst.request("POST", "/media/providers/metadata/group?title=After"
                                "&primaryIdentifier=tv.plex.agents.movie")[1]
    return (container(body).get("MetadataAgentProviderGroup") or [{}])[0].get("id")


def w_uploads(inst, ctx):
    statuses = [inst.request("POST", "/library/metadata/1007/%s" % kind, body=body)[0]
                for kind, body in (("posters", POSTER), ("arts", ART), ("themes", THEME),
                                   ("clearLogos", LOGO))]
    keys = []
    for kind in ("posters", "arts", "themes", "clearLogos"):
        for row in rows(inst.request("GET", "/library/metadata/1007/%s" % kind)[1]):
            # The candidate's `key` is where its bytes are served from.
            if str(row.get("ratingKey") or "").startswith("upload://"):
                keys.append(row["key"])
    ctx["upload_keys"] = keys
    return statuses + [200 if len(keys) == 4 else 0]


def r_uploads(ctx):
    return (["/library/metadata/1007"]
            + ["/library/metadata/1007/%s" % kind
               for kind in ("posters", "arts", "themes", "clearLogos")]
            + list(ctx.get("upload_keys") or []))


def w_subtitles(inst, ctx):
    status = inst.request("POST", "/library/metadata/1013/subtitles?title=kept.srt&format=srt",
                          body=SUBTITLE)[0]
    ctx["stream"] = _last_subtitle(inst, "1013")
    return [status]


def _last_subtitle(inst, key):
    streams = [stream for media in first(inst.request("GET", "/library/metadata/%s" % key)[1])
               .get("Media") or [] for part in media.get("Part") or []
               for stream in part.get("Stream") or [] if stream.get("streamType") == 3]
    return max((int(stream["id"]) for stream in streams), default=None)


def again_subtitles(inst, ctx):
    inst.request("POST", "/library/metadata/1013/subtitles?title=after.srt&format=srt",
                 body=SUBTITLE + b"again\n")
    return _last_subtitle(inst, "1013")


def w_watch_history(inst, ctx):
    # 1001 was completed in a playback session (the kind before this one); these are
    # Mark Played and a movie's resume point, so the show has no episode in progress.
    statuses = [inst.request("GET", path)[0] for path in (
        "/:/scrobble?key=1002&identifier=" + LIBRARY,
        "/:/scrobble?key=1003&identifier=" + LIBRARY,
        "/:/progress?key=1013&time=65000&identifier=" + LIBRARY)]
    history = rows(inst.request("GET", "/status/sessions/history/all?type=0,1,4")[1])
    # Delete the row holding the HIGHEST key: the counter must still not hand it out
    # again, which a counter re-derived from the surviving rows would.
    keys = [row.get("historyKey") for row in history if row.get("historyKey")]
    if keys:
        newest = max(keys, key=lambda key: int(key.rsplit("/", 1)[-1]))
        statuses.append(inst.request("DELETE", newest)[0])
    return statuses


def again_watch_history(inst, ctx):
    inst.request("GET", "/:/scrobble?key=1005&identifier=" + LIBRARY)
    history = rows(inst.request("GET", "/status/sessions/history/all?type=0,1,4")[1])
    return sorted(row.get("historyKey") for row in history)


def w_edits(inst, ctx):
    return [inst.request(method, path)[0] for method, path in (
        ("GET", "/:/rate?key=1008&identifier=%s&rating=8" % LIBRARY),
        ("PUT", "/library/metadata/1008?title.value=Persisted%20Title&title.locked=1"),
        ("PUT", "/library/metadata/1008?summary.value=Kept%20across%20a%20restart"),
        ("PUT", "/library/metadata/1008?type=1&genre%5B0%5D.tag.tag=Kept%20Genre"
                "&genre.locked=1"),
        # Added and then removed: the TAG TABLE keeps it, so a re-add reuses its id.
        ("PUT", "/library/metadata/1008?type=1&label%5B0%5D.tag.tag=Transient%20Label"),
        ("PUT", "/library/metadata/1008?type=1&label%5B%5D.tag.tag-=Transient%20Label"),
    )]


def again_edits(inst, ctx):
    inst.request("PUT", "/library/metadata/1009?type=1&label%5B0%5D.tag.tag=Transient%20Label")
    return [(tag.get("tag"), tag.get("id")) for tag in
            first(inst.request("GET", "/library/metadata/1009")[1]).get("Label") or []]


def w_merge(inst, ctx):
    return [inst.request("PUT", "/library/metadata/1012/merge?ids=1014")[0],
            inst.request("DELETE", "/library/metadata/1010")[0]]


def again_merge(inst, ctx):
    # A split mints keys for the parts it takes out; a key a merge or delete freed
    # is never reused (`_retired`), restart or not.
    inst.request("PUT", "/library/metadata/1012/split")
    return sorted(str(row.get("ratingKey")) for row in
                  rows(inst.request("GET", "/library/sections/1/all")[1]))


def w_playlists(inst, ctx):
    machine = ctx["machine"]
    status, body = inst.request("POST", "/playlists?type=video&title=Kept%%20List&smart=0"
                                        "&uri=%s" % uri(machine, 1007))
    ctx["playlist"] = first(body).get("ratingKey")
    add = inst.request("PUT", "/playlists/%s/items?uri=%s" % (ctx["playlist"],
                                                             uri(machine, 1013)))[0]
    smart_uri = q("server://%s/%s/library/sections/1/all?type=1&sort=titleSort"
                  % (machine, LIBRARY))
    smart_status, body = inst.request("POST", "/playlists?type=video&title=Kept%%20Smart"
                                              "&smart=1&uri=%s" % smart_uri)
    ctx["smart_playlist"] = first(body).get("ratingKey")
    # A playlist is a leaf of its own: its view state and rating live on the list.
    rated = inst.request("GET", "/:/rate?key=%s&identifier=%s&rating=6"
                         % (ctx["playlist"], LIBRARY))[0]
    return [status, add, smart_status, rated]


def r_playlists(ctx):
    return ["/playlists", "/playlists/%s" % ctx.get("playlist"),
            "/playlists/%s/items" % ctx.get("playlist"),
            "/playlists/%s" % ctx.get("smart_playlist"),
            "/playlists/%s/items" % ctx.get("smart_playlist")]


def again_playlists(inst, ctx):
    body = inst.request("POST", "/playlists?type=video&title=After&smart=0&uri=%s"
                        % uri(ctx["machine"], 1009))[1]
    key = first(body).get("ratingKey")
    items = rows(inst.request("GET", "/playlists/%s/items" % key)[1])
    return key, [row.get("playlistItemID") for row in items]


def w_collections(inst, ctx):
    machine = ctx["machine"]
    status, body = inst.request("POST", "/library/collections?type=1&title=Kept%%20Collection"
                                        "&smart=0&sectionId=1&uri=%s" % uri(machine, 1013))
    ctx["collection"] = first(body).get("ratingKey")
    smart_uri = q("server://%s/%s/library/sections/1/all?type=1&title=Tears"
                  % (machine, LIBRARY))
    smart_status, body = inst.request("POST", "/library/collections?type=1&title=Kept%%20Smart"
                                              "&smart=1&sectionId=1&uri=%s" % smart_uri)
    ctx["smart_collection"] = first(body).get("ratingKey")
    edited = inst.request("PUT", "/library/sections/1/all?type=18&id=%s"
                                 "&summary.value=Collected%%20summary" % ctx["collection"])[0]
    mode = inst.request("PUT", "/library/metadata/%s/prefs?collectionMode=1"
                        % ctx["collection"])[0]
    return [status, smart_status, edited, mode]


def r_collections(ctx):
    return ["/library/sections/1/collections", "/library/metadata/1013",
            "/library/metadata/%s" % ctx.get("collection"),
            "/library/collections/%s/children" % ctx.get("collection"),
            "/library/metadata/%s/prefs" % ctx.get("collection"),
            "/library/metadata/%s" % ctx.get("smart_collection"),
            "/library/collections/%s/children" % ctx.get("smart_collection")]


def again_collections(inst, ctx):
    body = inst.request("POST", "/library/collections?type=1&title=After&smart=0&sectionId=1"
                                "&uri=%s" % uri(ctx["machine"], 1009))[1]
    return first(body).get("ratingKey")


def w_play_queues(inst, ctx):
    status, body = inst.request("POST", "/playQueues?type=video&own=1&uri=%s"
                                % uri(ctx["machine"], 20001))
    ctx["queue"] = container(body).get("playQueueID")
    return [status]


def again_play_queues(inst, ctx):
    body = inst.request("POST", "/playQueues?type=video&own=1&uri=%s"
                        % uri(ctx["machine"], 1009),
                        {"X-Plex-Client-Identifier": "persist-other-player"})[1]
    return (container(body).get("playQueueID"),
            [row.get("playQueueItemID") for row in rows(body)])


def w_download_queues(inst, ctx):
    status, body = inst.request("POST", "/downloadQueue",
                                {"X-Plex-Client-Identifier": "persist-downloader"})
    ctx["download"] = (container(body).get("DownloadQueue") or [{}])[0].get("id")
    return [status]


def again_download_queues(inst, ctx):
    same = inst.request("POST", "/downloadQueue",
                        {"X-Plex-Client-Identifier": "persist-downloader"})[1]
    other = inst.request("POST", "/downloadQueue",
                         {"X-Plex-Client-Identifier": "persist-second-downloader"})[1]
    return [(container(body).get("DownloadQueue") or [{}])[0].get("id")
            for body in (same, other)]


def w_devices_statistics(inst, ctx):
    statuses = [inst.request("GET", "/identity", {
        "X-Plex-Client-Identifier": "persist-second-device",
        "X-Plex-Device-Name": "Second Device"})[0]]
    # A play the session module counts: playing, then stopped 40 s in.
    statuses.append(timeline(inst, "1006", "playing", 1000, "persist-stats")[0])
    statuses.append(timeline(inst, "1006", "stopped", 41000, "persist-stats")[0])
    return statuses


def w_continue_watching(inst, ctx):
    # A movie resumed and then hidden: the offset stays, the shelf drops it.
    statuses = [inst.request("GET", "/:/progress?key=1009&time=95000&identifier=" + LIBRARY)[0],
                inst.request("PUT", "/actions/removeFromContinueWatching?ratingKey=1009")[0]]
    return statuses


def w_session_view(inst, ctx):
    # An episode COMPLETED in a playback session: history, not view state, and what
    # lets a library's Continue Watching list a scrobble-only next-up (cycles 57-59):
    # the next kind's Mark Played on 1002 and 1003 puts 1004 on that shelf only
    # because of this.
    statuses = [timeline(inst, "1001", "playing", 30000, "persist-complete", 60000)[0],
                timeline(inst, "1001", "playing", 59000, "persist-complete", 60000)[0],
                timeline(inst, "1001", "stopped", 59000, "persist-complete", 60000)[0]]
    # The next-up follows the most recently viewed episode, to the second.
    time.sleep(1.1)
    return statuses


def w_libraries(inst, ctx):
    created = []
    for name in ("Added%20Library", "Doomed%20Library"):
        status, body = inst.request("POST", "/library/sections?name=%s&type=movie"
                                    "&agent=tv.plex.agents.movie&scanner=Plex%%20Movie"
                                    "&language=en-US&location=%%2F%s" % (name, name[:5]))
        created.append(status)
    sections = container(inst.request("GET", "/library/sections")[1]).get("Directory") or []
    keys = sorted((int(row["key"]) for row in sections if int(row["key"]) > 2))
    ctx["sections"] = keys
    if len(keys) >= 2:
        created.append(inst.request("PUT", "/library/sections/%s?name=Renamed%%20Library"
                                           "&agent=tv.plex.agents.movie&language=en-US"
                                    % keys[0])[0])
        created.append(inst.request("DELETE", "/library/sections/%s" % keys[1])[0])
    return created


def again_libraries(inst, ctx):
    inst.request("POST", "/library/sections?name=After&type=movie&agent=tv.plex.agents.movie"
                         "&scanner=Plex%20Movie&language=en-US&location=%2Fafter")
    sections = container(inst.request("GET", "/library/sections")[1]).get("Directory") or []
    return sorted(int(row["key"]) for row in sections)


def w_theme_rows(inst, ctx):
    status, body = inst.request("GET", "/library/metadata/1012/theme/1700000000/metadata")
    return [status]


def again_theme_rows(inst, ctx):
    body = inst.request("GET", "/library/metadata/1013/theme/1700000001/metadata")[1]
    return first(body).get("ratingKey")


KINDS = [
    ("server prefs (butler window, name)", w_server_prefs, lambda c: ["/:/prefs", "/"], None),
    ("section prefs and the library-wide show settings", w_section_prefs,
     lambda c: ["/library/sections/2/prefs", "/library/sections/1/prefs",
                "/library/metadata/20001/children", "/library/metadata/1005"], None),
    ("item prefs", w_item_prefs,
     lambda c: ["/library/metadata/20001/prefs", "/library/metadata/30004/children"], None),
    ("legacy agent order", w_agent_order,
     lambda c: ["/system/agents/com.plexapp.agents.imdb/config/1"], None),
    ("custom metadata provider groups", w_provider_group,
     lambda c: ["/media/providers/metadata/group", "/media/providers/metadata",
                "/media/providers/metadata/group/%s" % c.get("group")],
     again_provider_group),
    ("uploaded posters, art, themes and logos, with their bytes", w_uploads, r_uploads, None),
    ("uploaded sidecar subtitles, with their bytes", w_subtitles,
     lambda c: ["/library/metadata/1013", "/library/streams/%s" % c.get("stream")],
     again_subtitles),
    ("views completed in a playback session", w_session_view,
     lambda c: ["/hubs/sections/2/continueWatching/items", "/hubs/sections/2",
                "/library/sections/2/onDeck"], None),
    ("watch state and history", w_watch_history,
     lambda c: ["/status/sessions/history/all?type=0,1,4", "/library/metadata/1002",
                "/library/metadata/1013", "/library/metadata/30002", "/library/metadata/20001"],
     again_watch_history),
    ("ratings, edits, locks and the tag table", w_edits,
     lambda c: ["/library/metadata/1008", "/library/sections/1/genre"], again_edits),
    ("merges and deletes (and the retired keys)", w_merge,
     lambda c: ["/library/metadata/1012", "/library/metadata/1014", "/library/metadata/1010",
                "/library/sections/1/all", "/library/metadata/30004/children"], again_merge),
    ("playlists, smart ones and their view state", w_playlists, r_playlists, again_playlists),
    ("collections, smart ones and their settings", w_collections, r_collections,
     again_collections),
    ("play queues", w_play_queues, lambda c: ["/playQueues/%s" % c.get("queue")],
     again_play_queues),
    ("download queues", w_download_queues,
     lambda c: ["/downloadQueue/%s" % c.get("download")], again_download_queues),
    ("devices and statistics", w_devices_statistics,
     lambda c: ["/devices", "/statistics/media?timespan=4"], None),
    ("hidden from Continue Watching", w_continue_watching,
     lambda c: ["/hubs/continueWatching", "/library/onDeck", "/library/metadata/1009"], None),
    ("library create, rename and delete", w_libraries,
     lambda c: ["/library/sections"] + ["/library/sections/%s" % k
                                        for k in c.get("sections") or []],
     again_libraries),
    ("minted theme rows", w_theme_rows,
     lambda c: ["/library/metadata/1012/theme/1700000000/metadata"], again_theme_rows),
]


def write_all(inst, ctx):
    statuses = {}
    for name, write, _reads, _again in KINDS:
        statuses[name] = write(inst, ctx)
    return statuses


def read_all(inst, ctx):
    return {name: {path: inst.request("GET", path) for path in reads(ctx)}
            for name, _write, reads, _again in KINDS}


def again_all(inst, ctx):
    return {name: again(inst, ctx) for name, _w, _r, again in KINDS if again}


def main():
    work = tempfile.mkdtemp(prefix="rex-parity-persist-")
    try:
        catalog = os.path.join(work, "catalog.json")
        shutil.copy(os.path.join(HERE, "catalog_ui.json"), catalog)
        machine = json.load(open(catalog))["machineIdentifier"]
        state = os.path.join(work, "state")

        # -- the server that never restarts: what "continues" means --------------
        control = Instance(catalog, os.path.join(work, "control-state"))
        control_ctx = {"machine": machine}
        write_all(control, control_ctx)
        continued = again_all(control, control_ctx)
        control.stop()

        # -- write everything, read everything, restart, read again ----------------
        first_run = Instance(catalog, state)
        ctx = {"machine": machine}
        statuses = write_all(first_run, ctx)
        written = read_all(first_run, ctx)
        first_run.stop(signal.SIGTERM)
        # Past the second the writes were stamped in, so a row minted again on read
        # (with a new `addedAt`) cannot pass for the stored one.
        time.sleep(1.1)
        fresh_instance = Instance(catalog)
        fresh = read_all(fresh_instance, ctx)
        fresh_instance.stop()
        second_run = Instance(catalog, state)
        restored = read_all(second_run, ctx)
        after = again_all(second_run, ctx)
        second_run.stop()

        for name, _write, _reads, again in KINDS:
            accepted = all(200 <= status < 300 for status in statuses[name])
            landed = [path for path in written[name]
                      if written[name][path] != fresh[name][path]]
            lost = [path for path in written[name]
                    if written[name][path] != restored[name][path]]
            check("%s: the write lands" % name, bool(accepted and landed),
                  "statuses %s, no read differs from a fresh server" % statuses[name]
                  if accepted else "statuses %s" % statuses[name])
            check("%s: every read answers the same after a restart" % name, not lost,
                  "; ".join("%s: %s" % (path, differences(written[name][path],
                                                          restored[name][path]))
                            for path in lost))
            if again:
                check("%s: the next id continues across the restart" % name,
                      after[name] == continued[name] and after[name] not in (None, [], ""),
                      "after a restart %s, without one %s" % (after[name], continued[name]))

        rebuild(work, catalog, machine)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\nPASS %d/%d" % (sum(results), len(results)))
    return 0 if all(results) else 1


def rebuild(work, catalog, machine):
    """The state restore over a changed catalog: view state by guid, then by part
    file; the server-level settings; nothing else; the old file kept as `.stale`."""
    state = os.path.join(work, "rebuild-state")
    shutil.copy(os.path.join(HERE, "catalog_ui.json"), catalog)
    inst = Instance(catalog, state)
    for path in ("/:/scrobble?key=1007&identifier=" + LIBRARY,
                 "/:/rate?key=1013&identifier=%s&rating=6" % LIBRARY,
                 "/:/progress?key=1001&time=70000&identifier=" + LIBRARY,
                 "/:/scrobble?key=1002&identifier=" + LIBRARY):
        inst.request("GET", path)
    inst.request("PUT", "/:/prefs?FriendlyName=Rebuilt%20Server&ButlerStartHour=4")
    inst.request("PUT", "/system/agents/com.plexapp.agents.imdb/config/1?order="
                        "com.plexapp.agents.imdb%2Ccom.plexapp.agents.localmedia")
    group = (container(inst.request("POST", "/media/providers/metadata/group?title=Kept"
                                            "&primaryIdentifier=tv.plex.agents.movie")[1])
             .get("MetadataAgentProviderGroup") or [{}])[0].get("id")
    inst.request("PUT", "/library/sections/2/prefs?flattenSeasons=1")
    inst.request("PUT", "/library/metadata/1008?title.value=Dropped%20Title&title.locked=1")
    inst.request("POST", "/playlists?type=video&title=Dropped&smart=0&uri=%s"
                 % uri(machine, 1007))
    agents_before = inst.request("GET", "/system/agents/com.plexapp.agents.imdb/config/1")
    groups_before = inst.request("GET", "/media/providers/metadata/group")
    devices_before = inst.request("GET", "/devices")
    inst.stop()

    # The rebuild: new ratingKeys for every row (so nothing can be matched by key),
    # and 1013's guid dropped (so it can only be matched by its part file).
    document = json.load(open(catalog))
    def moved(value):
        # ratingKeys are strings on some rows and numbers on others; keep each's type.
        return type(value)(int(value) + 500000)

    olds = {str(row["ratingKey"]) for row in document["items"]}
    for row in document["items"]:
        if str(row["ratingKey"]) == "1013":
            row.pop("guid", None)
        row["ratingKey"] = moved(row["ratingKey"])
        row["key"] = "/library/metadata/%s" % row["ratingKey"]
        for name in ("parentRatingKey", "grandparentRatingKey"):
            if str(row.get(name)) in olds:
                row[name] = moved(row[name])
        for name in ("parentKey", "grandparentKey"):
            tail = str(row.get(name) or "").rsplit("/", 1)[-1]
            if tail in olds:
                row[name] = "/library/metadata/%d" % (int(tail) + 500000)
    json.dump(document, open(catalog, "w"))

    inst = Instance(catalog, state)
    view = {old: first(inst.request("GET", "/library/metadata/%d" % (old + 500000))[1])
            for old in (1007, 1013, 1001, 1002, 1008)}
    check("rebuild: view state follows the guid, not the old ratingKey",
          view[1007].get("viewCount") == 1 and view[1002].get("viewCount") == 1
          and view[1001].get("viewOffset") == 70000,
          "1007 viewCount=%r 1002 viewCount=%r 1001 viewOffset=%r" % (
              view[1007].get("viewCount"), view[1002].get("viewCount"),
              view[1001].get("viewOffset")))
    check("rebuild: a row without a guid is matched by its part file",
          view[1013].get("userRating") == 6.0, "1013 userRating=%r" % view[1013].get("userRating"))
    check("rebuild: the server settings carry over",
          container(inst.request("GET", "/")[1]).get("friendlyName") == "Rebuilt Server"
          and any(s.get("id") == "ButlerStartHour" and str(s.get("value")) == "4"
                  for s in container(inst.request("GET", "/:/prefs")[1]).get("Setting") or [])
          and inst.request("GET", "/system/agents/com.plexapp.agents.imdb/config/1")
          == agents_before
          and inst.request("GET", "/media/providers/metadata/group") == groups_before
          and group is not None,
          "friendlyName, ButlerStartHour, agent order or provider groups not carried")
    check("rebuild: the device table carries over",
          inst.request("GET", "/devices") == devices_before)
    playlists = rows(inst.request("GET", "/playlists")[1])
    history = rows(inst.request("GET", "/status/sessions/history/all?type=0,1,4")[1])
    flattened = container(inst.request("GET", "/library/sections/2/prefs")[1]).get("Setting")
    flat = [s.get("value") for s in flattened or [] if s.get("id") == "flattenSeasons"]
    check("rebuild: edits, lists, history and section settings stay behind",
          view[1008].get("title") != "Dropped Title" and not playlists and not history
          and flat != ["1"],
          "title=%r playlists=%d history=%d flattenSeasons=%s" % (
              view[1008].get("title"), len(playlists), len(history), flat))
    check("rebuild: the superseded state file is kept as .stale",
          os.path.exists(state + ".stale"))
    inst.stop()


if __name__ == "__main__":
    sys.exit(main())
