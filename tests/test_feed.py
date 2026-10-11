#!/usr/bin/env python3
"""The feed: a source pushes its items, simplex lists and queries nothing.

`docs/feed-api.md` is the contract. The source here is zurg as its producer describes
itself: items whose id and path are `<directory>/<release>/<file>`, whose `file` is that
under zurg's mount, carrying zurg's two real entries (`fixtures/zurg/entries.json`: the
release, hash, IMDb id, size and stored ffprobe) and a release zurg has neither
identified nor probed. Its bytes are a plain Range server behind a bearer token, not
WebDAV, because a source is any HTTP server that honours Range.

TMDB and Plex answer from `fixtures/feed/provider_cache.json` (real ids) written beside
the catalog; a lookup they miss goes out with a fake key and is refused, so a release
pushed without ids stays unmatched until a later push names it.

The migration leg starts from rows the old WebDAV scanner really made
(`fixtures/feed/legacy_scan_rows.json`, recorded by `probes/legacy_scan_fixture.py`).

Native only: the feed is simplex's own API, and the Python oracle has none.

    python3 tests/test_feed.py
"""
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
ENTRIES = json.load(open(os.path.join(FIXTURES, "zurg", "entries.json")))["entries"]
CACHE = json.load(open(os.path.join(FIXTURES, "feed", "provider_cache.json")))
LEGACY = json.load(open(os.path.join(FIXTURES, "feed", "legacy_scan_rows.json")))
FEED_TOKEN = "feed-secret-for-tests"
BYTES_TOKEN = "bytes-secret-for-tests"
BODY = bytes((i * 7 + 13) % 251 for i in range(65536))
NEW = "Qzxv.Wmplk.2024.1080p.WEB-DL"
MOUNT = "/zurg"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def item(kind, ids=True, probe=True):
    """A zurg-shaped item for one of the fixture's entries, or `new` (unidentified)."""
    if kind == "new":
        release, filename, entry = NEW, NEW + ".mkv", {"hash": "a" * 40, "name": NEW, "size": 12345}
        entry["media_info"] = ENTRIES["movie"]["entry"]["media_info"] if probe else None
        directory = "movies"
    else:
        fixture = ENTRIES[kind]
        release, filename, entry = fixture["release"], fixture["file"], dict(fixture["entry"])
        directory = "movies" if kind == "movie" else "shows"
        if not probe:
            entry["media_info"] = None
    path = "%s/%s/%s" % (directory, release, filename)
    out = {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": entry["name"],
           "release": "%s/%s" % (directory, release), "size": entry["size"], "hash": entry["hash"],
           "media_info": entry["media_info"]}
    if ids and entry.get("imdb_id"):
        out["ids"] = {"imdb": entry["imdb_id"]}
    return out


class Bytes:
    """A source's byte server: Range reads of `files` behind a bearer token."""

    def __init__(self):
        self.files = set()
        self.seen = []

    def handler(source):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", extra=()):
                self.send_response(status)
                self.send_header("Content-Type", "video/x-matroska")
                self.send_header("Content-Length", str(len(body)))
                for name, value in extra:
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):
                path = urllib.parse.unquote(self.path)
                source.seen.append((self.command, path, self.headers.get("Authorization"),
                                    self.headers.get("Range")))
                if self.headers.get("Authorization") != "Bearer " + BYTES_TOKEN:
                    return self.reply(401)
                if path not in source.files:
                    return self.reply(404)
                spec = self.headers.get("Range")
                if spec and spec.startswith("bytes="):
                    first, _, last = spec[6:].partition("-")
                    first, last = int(first), min(int(last) if last else len(BODY) - 1, len(BODY) - 1)
                    return self.reply(206, BODY[first:last + 1],
                                      [("Content-Range", "bytes %d-%d/%d" % (first, last, len(BODY)))])
                self.reply(200, BODY)

            do_HEAD = do_GET
        return Handler


def serve(handler):
    port = free_port()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port


class Events:
    """Every notification frame off the eventsource channel, in arrival order."""

    def __init__(self, port):
        self.frames = []
        response = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:%d/:/eventsource/notifications" % port,
            headers={"Accept": "text/event-stream"}), timeout=3600)

        def read():
            name = None
            for line in response:
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("event:"):
                    name = text.split(":", 1)[1].strip()
                elif text.startswith("data:") and name:
                    try:
                        self.frames.append((name, json.loads(text.split(":", 1)[1])))
                    except ValueError:
                        pass
                    name = None
        threading.Thread(target=read, daemon=True).start()

    def mark(self):
        time.sleep(0.5)  # frames of the request before land first
        return len(self.frames)

    def timeline(self, mark, key):
        time.sleep(0.5)
        out = []
        for name, frame in self.frames[mark:]:
            if name == "timeline":
                entry = frame.get("TimelineEntry")
                out.extend(row for row in (entry if isinstance(entry, list) else [entry])
                           if row.get("itemID") == key)
        return [(row.get("type"), row.get("state"), row.get("metadataState")) for row in out]


class Server:
    def __init__(self, work, bytes_port, catalog="catalog.json"):
        self.port = free_port()
        sources = {"zurg": {"url": "http://127.0.0.1:%d/dav" % bytes_port, "bearer": BYTES_TOKEN,
                            "token": FEED_TOKEN, "mount": MOUNT}}
        json.dump(sources, open(os.path.join(work, "sources.json"), "w"))
        env = dict(os.environ, TMDB_KEY="fake", PLEX_TOKEN="fake", SIMPLEX_PLEXTV_URL="http://127.0.0.1:9")
        for name in ("SIMPLEX_STATE", "SIMPLEX_TOKEN", "ZURG_URL", "SIMPLEX_SOURCES", "SIMPLEX_FEED_TOKEN"):
            env.pop(name, None)
        self.log = open(os.path.join(work, "server.log"), "ab")
        self.process = subprocess.Popen(
            server_command("--port", self.port, "--catalog", os.path.join(work, catalog),
                           "--state", os.path.join(work, "simplex.db"), "--sources",
                           os.path.join(work, "sources.json"), "--assert-claimed", "--gdm", "off", root=ROOT),
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(160):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/identity" % self.port, timeout=2)
                break
            except Exception:
                time.sleep(0.25)
        self.events = Events(self.port)

    def request(self, path, method="GET", body=None, headers=None):
        data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                         headers=dict({"Accept": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw[:1] == b"{" else raw
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw[:1] == b"{" else raw

    def feed(self, method, leaf, body=None, token=FEED_TOKEN, source="zurg"):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        return self.request("/simplex/feed/%s/%s" % (source, leaf), method, body, headers)

    def rows(self, path):
        status, body = self.request(path)
        return ((body or {}).get("MediaContainer") or {}).get("Metadata") or [] if status == 200 else []

    def keys(self, source="zurg"):
        status, body = self.feed("GET", "items")
        return {row["id"]: row["ratingKey"] for row in body.get("items", [])} if status == 200 else {}

    def row(self, key):
        return (self.rows("/library/metadata/%s?includeGuids=1" % key) or [{}])[0]

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.log.close()


def catalog(work, items=(), name="catalog.json", next_key=None):
    document = {"machineIdentifier": "00000000000000000000000000000000000000fe",
                "friendlyName": "simplex-feed", "sections": LEGACY["sections"], "items": list(items)}
    json.dump(document, open(os.path.join(work, name), "w"))
    for cache, rows in (("tmdb_cache.json", CACHE["tmdb"]), ("plex_cache.json", CACHE["plex"])):
        json.dump(rows, open(os.path.join(work, cache), "w"))


def main():
    work = tempfile.mkdtemp(prefix="simplex-feed-")
    source = Bytes()
    for kind in ("movie", "episode", "new"):
        source.files.add("/dav/" + item(kind)["path"])
    bytes_port = serve(source.handler())
    catalog(work)
    server = Server(work, bytes_port)
    try:
        # -- refusals: the feed takes its source's token and nothing else -----------------
        movie = item("movie", ids=False)
        status, _ = server.feed("POST", "items", {"items": [movie]}, token=None)
        check("a push without a token is refused 401", status == 401, status)
        status, _ = server.feed("POST", "items", {"items": [movie]}, token="wrong")
        check("a push with another token is refused 401", status == 401, status)
        status, _ = server.request("/simplex/feed/zurg/items", "POST", {"items": [movie]},
                                   {"X-Plex-Token": FEED_TOKEN, "Content-Type": "application/json"})
        check("the feed token as a Plex token is refused 401", status == 401, status)
        status, _ = server.feed("POST", "items", {"items": [movie]}, source="nobody")
        check("an unknown source is a 404", status == 404, status)
        status, _ = server.feed("POST", "items", b"not json")
        check("a body that is not a JSON object is a 400", status == 400, status)
        check("nothing was added by a refused push", server.keys() == {} and
              server.rows("/library/sections/1/all") == [], server.keys())

        # -- upsert: a release pushed without ids is shown unmatched ---------------------
        status, body = server.feed("POST", "items", {"items": [movie]})
        check("a probed item is created", status == 200 and body.get("created") == 1, body)
        movie_key = server.keys().get(movie["id"])
        row = server.row(movie_key)
        part = ((row.get("Media") or [{}])[0].get("Part") or [{}])[0]
        check("its ratingKey is minted above every catalog range", movie_key and int(movie_key) >= 100000, movie_key)
        check("its Part file is the item's file, the path zurg's matcher reads back",
              part.get("file") == movie["file"], part.get("file"))
        check("its Media and Streams are the pushed ffprobe",
              (row.get("Media") or [{}])[0].get("videoCodec") == "h264" and len(part.get("Stream") or []) == 4,
              row.get("Media"))
        check("with no ids and no provider answer it is unmatched: titled from its name, local guid",
              row.get("guid") == "local://%s" % movie_key and row.get("title") == "Blame It on the Bellboy"
              and not row.get("Guid"), (row.get("guid"), row.get("title"), row.get("Guid")))
        status, body = server.feed("POST", "items", {"items": [movie]})
        check("the same push again changes nothing",
              status == 200 and body.get("unchanged") == 1 and body.get("created") == 0, body)

        # -- part bytes come from the source's url, Range forwarded, its own credential --
        source.seen.clear()
        status, raw = server.request(part.get("key", "/none"), headers={"Range": "bytes=100-199"})
        hit = [seen for seen in source.seen if seen[0] == "GET"]
        check("a part read streams the source's bytes, Range forwarded", status == 206 and raw == BODY[100:200],
              status)
        check("read at <url>/<path> with the source's bearer, not the feed token",
              hit and hit[-1][1] == "/dav/" + movie["path"] and hit[-1][2] == "Bearer " + BYTES_TOKEN
              and hit[-1][3] == "bytes=100-199", hit[-1:])

        # -- late ids: the same key, identified, its watch state kept ---------------------
        server.request("/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % movie_key)
        status, body = server.feed("POST", "items", {"items": [item("movie")]})
        row = server.row(movie_key)
        check("a push that adds an IMDb id re-identifies the row", status == 200 and body.get("updated") == 1
              and row.get("title") == "Blame It on the Bellboy" and row.get("year") == 1992, (body, row.get("title")))
        check("on the same ratingKey, with Plex's guid and both external ids",
              server.keys().get(movie["id"]) == movie_key
              and row.get("guid") == "plex://movie/5d7768279ab5440020ad6e0e"
              and {"id": "imdb://tt0103827"} in (row.get("Guid") or []), (row.get("guid"), row.get("Guid")))
        check("and keeps its watch state", row.get("viewCount") == 1, row.get("viewCount"))
        status, body = server.feed("POST", "items", {"items": [item("movie")]})
        check("the same ids again re-identify nothing", body.get("unchanged") == 1, body)

        # -- an episode: unmatched by name, then its id: the same episode, show, season ----
        episode = item("episode")
        status, body = server.feed("POST", "items", {"items": [item("episode", ids=False)]})
        episode_key = server.keys().get(episode["id"])
        before = server.row(episode_key)
        local_show, local_season = before.get("grandparentRatingKey"), before.get("parentRatingKey")
        # The same push again, with an IMDb id TMDB and Plex do not know: the show moves to
        # that identity, still unidentified, on its own key and its own local guid.
        unknown = dict(item("episode"), ids={"imdb": "tt0000001"})
        server.feed("POST", "items", {"items": [unknown]})
        unnamed = (server.rows("/library/sections/2/all") or [{}])[0]
        check("an id no provider knows keeps the show's key and its own local guid",
              unnamed.get("ratingKey") == local_show and unnamed.get("guid") == "local://%s" % local_show,
              (unnamed.get("ratingKey"), unnamed.get("guid"), local_show))
        check("an episode with no ids is placed under a local show and season of its name",
              body.get("created") == 1 and before.get("index") == 7 and before.get("parentIndex") == 5
              and before.get("grandparentGuid") == "local://%s" % local_show, (body, before))
        status, body = server.feed("POST", "items", {"items": [episode]})
        ep = server.row(episode_key)
        shows = server.rows("/library/sections/2/all?includeGuids=1")
        check("its id re-identifies the episode on its key: TMDB's title, Plex's guid",
              body.get("updated") == 1 and server.keys().get(episode["id"]) == episode_key
              and ep.get("title") == "Phantom of the Telethon"
              and ep.get("guid") == "plex://episode/5d9c0ab4705e7a001e6d4a2b", (body, ep.get("title"), ep.get("guid")))
        check("and its show, all of whose episodes moved, is re-identified on ITS key too",
              ep.get("grandparentRatingKey") == local_show and ep.get("parentRatingKey") == local_season
              and [row.get("ratingKey") for row in shows] == [local_show]
              and shows[0].get("guid") == "plex://show/5d9c086c46115600200aa2fe"
              and shows[0].get("title") == "American Dad!" and ep.get("grandparentTitle") == "American Dad!",
              (ep.get("grandparentRatingKey"), local_show, [(r.get("ratingKey"), r.get("title"), r.get("guid"))
                                                             for r in shows]))

        # -- held: no media_info, no row ------------------------------------------------
        held = item("new", probe=False)
        status, body = server.feed("POST", "items", {"items": [held]})
        status_h, listing = server.feed("GET", "held")
        titles = [row.get("title") for row in server.rows("/library/sections/1/all")]
        search = server.rows("/hubs/search?query=Qzxv")
        found = [hub for hub in (server.request("/hubs/search?query=Qzxv")[1] or {}).get("MediaContainer", {})
                 .get("Hub", []) if hub.get("size")]
        check("an item with no media_info is held, not created", body.get("held") == 1 and body.get("created") == 0,
              body)
        check("it is in no listing and no search", "Qzxv Wmplk" not in titles and not search and not found,
              (titles, found))
        check("the held listing names it", status_h == 200 and [row.get("id") for row in listing.get("held", [])]
              == [held["id"]], listing)
        server.stop()
        server = Server(work, bytes_port)
        check("held items survive a restart", [row.get("id") for row in server.feed("GET", "held")[1].get("held", [])]
              == [held["id"]] and server.keys().get(movie["id"]) == movie_key, server.feed("GET", "held"))
        before = int(time.time())
        mark = server.events.mark()
        status, body = server.feed("POST", "items", {"items": [item("new")]})
        new_key = server.keys().get(held["id"])
        new = server.row(new_key)
        recent = [row.get("ratingKey") for row in server.rows("/library/recentlyAdded")]
        check("the push that brings media_info shows it", body.get("created") == 1 and new.get("title") == "Qzxv Wmplk"
              and server.feed("GET", "held")[1].get("size") == 0, (body, new.get("title")))
        check("stamped as added when it became playable, first in Recently Added",
              (new.get("addedAt") or 0) >= before and recent[:1] == [new_key], (new.get("addedAt"), before, recent))
        created = server.events.timeline(mark, new_key)
        check("announced with the measured creation frames", created[:2] == [(1, 0, "created"), (1, 1, "created")],
              created)

        # -- delete: the trash, a return, the trash emptied --------------------------------
        mark = server.events.mark()
        status, body = server.feed("DELETE", "items", {"ids": [held["id"]]})
        trashed = server.row(new_key)
        check("a removed id is trashed, not deleted (autoEmptyTrash off)",
              body.get("trashed") == 1 and trashed.get("deletedAt")
              and (trashed.get("Media") or [{}])[0].get("deletedAt")
              and "deletedAt" not in ((trashed.get("Media") or [{}])[0].get("Part") or [{}])[0], (body, trashed))
        check("announced as the measured type -1 state 3/5 pair",
              server.events.timeline(mark, new_key) == [(-1, 3, None), (-1, 5, None)],
              server.events.timeline(mark, new_key))
        server.feed("POST", "items", {"items": [item("new")]})
        back = server.row(new_key)
        check("the id pushed again before emptyTrash is the same row and key",
              server.keys().get(held["id"]) == new_key and "deletedAt" not in back, back.get("deletedAt"))
        server.feed("DELETE", "items", {"ids": [held["id"]]})
        mark = server.events.mark()
        status, _ = server.request("/library/sections/1/emptyTrash", "PUT")
        check("emptyTrash deletes it with one state 9 frame",
              server.request("/library/metadata/%s" % new_key)[0] == 404
              and server.events.timeline(mark, new_key) == [(1, 9, "deleted")], server.events.timeline(mark, new_key))
        server.feed("POST", "items", {"items": [item("new")]})
        check("pushed after the trash was emptied, it is a new row on a new key",
              server.keys().get(held["id"]) not in (None, new_key), server.keys().get(held["id"]))
        status, body = server.feed("DELETE", "items", {"ids": ["never/pushed.mkv"]})
        check("an unknown id is counted, not an error", status == 200 and body.get("unknown") == 1, body)

        # -- refresh and /activities: the measured skeleton, nothing scanned -------------
        keys = server.keys()
        status, _ = server.request("/library/sections/1/refresh?path=" + urllib.parse.quote(MOUNT + "/movies/" + NEW))
        time.sleep(0.5)
        status_a, activities = server.request("/activities")
        check("an old zurg's refresh still answers 200 and changes nothing",
              status == 200 and server.keys() == keys, status)
        check("/activities lists nothing between requests",
              status_a == 200 and activities.get("MediaContainer", {}).get("size") == 0, activities)

        # -- snapshot: chunks, reconcile, the guards ---------------------------------------
        everything = [item("movie"), item("episode"), item("new")]
        status, body = server.feed("PUT", "snapshot", {"session": "s1", "part": 1, "items": everything[:1]})
        check("a chunk of a session simplex does not hold is refused 409", status == 409, (status, body))
        status, body = server.feed("PUT", "snapshot", {"session": "s1", "part": 0, "items": everything[:1]})
        status2, body2 = server.feed("PUT", "snapshot", {"session": "s1", "part": 1, "complete": True,
                                                         "items": everything[1:]})
        check("a complete snapshot of everything removes nothing",
              status == 200 and status2 == 200 and body2.get("seen") == 3
              and body2.get("removed") == {"trashed": 0, "deleted": 0, "released": 0}, (body, body2))
        status, body = server.feed("PUT", "snapshot", {"items": []})
        check("an empty snapshot removes nothing", status == 200 and body.get("removed", {}).get("trashed") == 0
              and len([k for k, v in server.keys().items()]) == 3, body)
        server.feed("POST", "items", {"items": [dict(item("new", probe=False), id="held/only.mkv",
                                                     path="movies/Held.Only/Held.Only.mkv",
                                                     file=MOUNT + "/movies/Held.Only/Held.Only.mkv")]})
        status, body = server.feed("PUT", "snapshot", {"items": [item("movie")]})
        check("a snapshot that would remove more than half is refused 409 and removes nothing",
              status == 409 and body.get("would_remove") == 3 and body.get("of") == 4
              and not any(row.get("deleted") for row in server.feed("GET", "items")[1]["items"]), body)
        status, body = server.feed("PUT", "snapshot", {"items": everything})
        check("a snapshot missing one held item releases it",
              status == 200 and body.get("removed", {}).get("released") == 1
              and server.feed("GET", "held")[1].get("size") == 0, body)
        status, body = server.feed("PUT", "snapshot", {"items": everything[:2], "force": False})
        check("removing one of three is within the guard: trashed",
              status == 200 and body.get("removed", {}).get("trashed") == 1, body)
        server.feed("PUT", "snapshot", {"items": everything})
        status, body = server.feed("PUT", "snapshot", {"items": everything[:1], "force": True})
        check("with force, a large removal is accepted", status == 200 and body.get("removed", {}).get("trashed") == 2,
              body)
        server.feed("PUT", "snapshot", {"items": everything})
        check("and the trashed rows come back on their keys when pushed again",
              all(not row.get("deleted") for row in server.feed("GET", "items")[1]["items"])
              and server.keys().get(movie["id"]) == movie_key and server.keys().get(episode["id"]) == episode_key,
              server.feed("GET", "items")[1])

        # -- autoEmptyTrash: an episode gone is deleted at once, parents with it -----------
        server.request("/:/prefs?autoEmptyTrash=1", "PUT")
        mark = server.events.mark()
        status, body = server.feed("DELETE", "items", {"ids": [episode["id"]]})
        check("with autoEmptyTrash on, a removed episode is deleted in the request (5a)",
              body.get("deleted") == 1 and server.request("/library/metadata/%s" % episode_key)[0] == 404
              and server.events.timeline(mark, episode_key) == [(4, 9, "deleted")],
              (body, server.events.timeline(mark, episode_key)))
        check("and its now-empty season and show go with it", server.rows("/library/sections/2/all") == [])
        server.stop()

        # -- migration: rows the old scanner made keep their keys -------------------------
        legacy_work = tempfile.mkdtemp(prefix="simplex-feed-legacy-")
        catalog(legacy_work, LEGACY["items"])
        server = Server(legacy_work, bytes_port)
        legacy_keys = {row["ratingKey"]: row for row in LEGACY["items"]}
        by_file = {((row.get("Media") or [{}])[0].get("Part") or [{}])[0].get("file"): row["ratingKey"]
                   for row in LEGACY["items"] if row["type"] in ("movie", "episode")}
        pushed = [item("movie"), item("episode"), item("new")]
        qzxv_key = by_file[item("new")["file"]]
        status, body = server.feed("PUT", "snapshot", {"items": pushed[:2]})
        check("a first snapshot over the scanner's rows creates nothing",
              status == 200 and body.get("created") == 0 and body.get("unchanged", 0) + body.get("updated", 0) == 2,
              body)
        check("and trashes the scanner's row it did not name, as a whole-library scan did",
              body.get("removed", {}).get("trashed") == 1 and server.row(qzxv_key).get("deletedAt"), body)
        status, body = server.feed("PUT", "snapshot", {"items": pushed})
        keys = server.keys()
        check("named again, that row is adopted out of the trash on its key",
              status == 200 and body.get("created") == 0 and keys.get(item("new")["id"]) == qzxv_key
              and not server.row(qzxv_key).get("deletedAt"), (body, keys))
        check("every file keeps the ratingKey the scanner minted",
              all(keys.get(entry["id"]) == by_file.get(entry["file"]) for entry in pushed), (keys, by_file))
        bellboy = server.row(by_file[item("movie")["file"]])
        check("adopted with its watch state and identification untouched",
              bellboy.get("viewCount") == 1 and bellboy.get("guid") == legacy_keys[bellboy.get("ratingKey")]["guid"],
              (bellboy.get("viewCount"), bellboy.get("guid")))
        shows = server.rows("/library/sections/2/all")
        check("the show and season the scanner made stay on their keys",
              [row.get("ratingKey") for row in shows] == [row["ratingKey"] for row in LEGACY["items"]
                                                          if row["type"] == "show"], shows)
        filled = server.row(qzxv_key)
        check("the scanner's unprobed row gets its streams from the push",
              len(((filled.get("Media") or [{}])[0].get("Part") or [{}])[0].get("Stream") or []) == 4,
              filled.get("Media"))
        status, body = server.feed("POST", "items", {"items": pushed})
        check("pushing the same again changes nothing", body.get("unchanged") == 3, body)
        named = dict(item("new"), ids={"imdb": "tt0103827"})
        server.feed("POST", "items", {"items": [named]})
        bellboy_key = by_file[item("movie")["file"]]
        merged = server.row(bellboy_key)
        # Re-identified as a title already here, it joins that row as a version, as a real
        # PMS merges a file matched to a movie it holds (test_feed_versions.py).
        check("an id the scanner's row was not identified by re-identifies it, into the row of that title",
              server.request("/library/metadata/%s" % qzxv_key)[0] == 404 and merged.get("viewCount") == 1
              and [((media.get("Part") or [{}])[0]).get("file") for media in merged.get("Media") or []]
              == [item("movie")["file"], item("new")["file"]], merged.get("Media"))
        keys = dict(keys, **{item("new")["id"]: bellboy_key})
        server.stop()
        server = Server(legacy_work, bytes_port)
        check("a restart keeps the adopted keys", server.keys() == keys, server.keys())
        shutil.rmtree(legacy_work, ignore_errors=True)
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    failed = results.count(False)
    print("\n%d/%d feed checks passed" % (len(results) - failed, len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    if os.environ.get("SIMPLEX_SERVER") == "python":
        print("the feed is simplex's own API; the Python oracle has none, so this suite is native only")
        sys.exit(0)
    sys.exit(main())
