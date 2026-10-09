#!/usr/bin/env python3
"""The native server's library scan against a local zurg: zurg is the library.

zurg's Plex integration (its `plex_server_url` pointed at plexsim) sends
`GET /library/sections/{id}/refresh?path=<mount>/<dir>/<release>` when a release
arrives, changes or goes. plexsim lists the folder on zurg's WebDAV, asks
`/api/library/entry` about each new file and adds it, trashes what is gone the way a
real PMS does (`fixtures/missing_file_scan_s43.json`, measured on plexscratch), and keeps
every row's ratingKey across scans and restarts -- zurg drops a release's IMDb id when
the key it matched the release to disappears.

The zurg here is a stub: PROPFIND listings in zurg's own shape, entries carrying two
real stored probes (`fixtures/zurg/entries.json`), file bytes behind basic auth, and a
503 while "loading". With no TMDB or Plex key what a scan finds stays unidentified; with
`TMDB_KEY` and `PLEX_TOKEN` in the environment the two real releases are identified
live (their IMDb ids are zurg's) and must come back with TMDB's titles and Plex's guids.

Native only: the Python oracle serves a fixed catalog and does not scan.

    python3 tests/test_native_zurg_scan.py
"""
import base64
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
import urllib.parse
import urllib.request

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "zurg", "entries.json")))["entries"]
AUTH = "Basic " + base64.b64encode(b"zuser:zpass").decode()
BODY = bytes(range(256)) * 64
KEYED = bool(os.environ.get("TMDB_KEY") and os.environ.get("PLEX_TOKEN"))
NEW = "Qzxv.Wmplk.2024.1080p.WEB-DL"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Zurg:
    """The library: `files` maps a decoded `/dav/...` file path to its entry."""

    def __init__(self):
        self.files = {}
        self.loading = False
        self.entry_delay = 0.0
        self.requests = []
        self.lock = threading.Lock()

    def add(self, directory, release, filename, entry):
        self.files["/dav/%s/%s/%s" % (directory, release, filename)] = entry

    def remove_release(self, directory, release):
        prefix = "/dav/%s/%s/" % (directory, release)
        for path in [p for p in self.files if p.startswith(prefix)]:
            del self.files[path]

    def listing(self, folder):
        """zurg's multistatus for a folder: itself, then one level below it."""
        folder = folder.rstrip("/") + "/"
        children = {}
        for path, entry in self.files.items():
            if not path.startswith(folder):
                continue
            rest = path[len(folder):].split("/")
            if len(rest) == 1:
                children[folder + rest[0]] = ("file", entry.get("size") or len(BODY))
            else:
                children[folder + rest[0] + "/"] = ("dir", 0)
        top = folder.count("/") <= 3  # /dav/<dir>/ always exists; a release only with files
        if not children and not top:
            return None
        quote = lambda p: "/".join(urllib.parse.quote(s, safe="") for s in p.split("/"))
        rows = ['<d:response>\n\t<d:href>%s</d:href>\n\t<d:propstat>\n\t\t<d:prop>\n\t\t\t'
                '<d:resourcetype>\n\t\t\t\t<d:collection/>\n\t\t\t</d:resourcetype>\n\t\t\t'
                '<d:getlastmodified>Mon, 02 Jan 2026 15:04:05 GMT</d:getlastmodified>\n\t\t'
                '</d:prop>\n\t\t<d:status>HTTP/1.1 200 OK</d:status>\n\t</d:propstat>\n'
                '</d:response>' % quote(folder)]
        for href, (kind, size) in sorted(children.items()):
            if kind == "dir":
                rows.append(rows[0].replace(quote(folder), quote(href), 1))
            else:
                rows.append('<d:response>\n\t<d:href>%s</d:href>\n\t<d:propstat>\n\t\t<d:prop>'
                            '\n\t\t\t<d:getcontentlength>%d</d:getcontentlength>\n\t\t\t'
                            '<d:getlastmodified>Mon, 02 Jan 2026 15:04:05 GMT</d:getlastmodified>'
                            '\n\t\t\t<d:resourcetype></d:resourcetype>\n\t\t</d:prop>\n\t\t'
                            '<d:status>HTTP/1.1 200 OK</d:status>\n\t</d:propstat>\n</d:response>'
                            % (quote(href), size))
        return ('<?xml version="1.0" encoding="utf-8"?>\n<d:multistatus xmlns:d="DAV:">'
                + "".join(rows) + "</d:multistatus>").encode()

    def handler(zurg):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", content_type="application/json", extra=()):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for name, value in extra:
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def gate(self):
                with zurg.lock:
                    zurg.requests.append((self.command, self.path))
                if self.headers.get("Authorization") != AUTH:
                    self.reply(401, b"Unauthorized", "text/plain")
                    return False
                return True

            def do_PROPFIND(self):
                if not self.gate():
                    return
                if zurg.loading:
                    self.reply(503, b"Library is still loading", "text/plain", [("Retry-After", "5")])
                    return
                body = zurg.listing(urllib.parse.unquote(self.path))
                if body is None:
                    self.reply(404, b"Not Found", "text/plain")
                else:
                    self.reply(207, body, "application/xml; charset=utf-8")

            def do_GET(self):
                if not self.gate():
                    return
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/api/library/entry":
                    path = urllib.parse.parse_qs(parsed.query)["path"][0]
                    entry = zurg.files.get(urllib.parse.unquote(path))
                    time.sleep(zurg.entry_delay)
                    if entry is None:
                        self.reply(404, b"404 page not found\n", "text/plain")
                    else:
                        self.reply(200, json.dumps(entry).encode())
                    return
                path = urllib.parse.unquote(parsed.path)
                if path not in zurg.files:
                    self.reply(404, b"Not Found", "text/plain")
                    return
                spec = self.headers.get("Range")
                if spec and spec.startswith("bytes="):
                    first, _, last = spec[6:].partition("-")
                    first = int(first)
                    last = min(int(last) if last else len(BODY) - 1, len(BODY) - 1)
                    self.reply(206, BODY[first:last + 1], "video/x-matroska",
                               [("Content-Range", "bytes %d-%d/%d" % (first, last, len(BODY)))])
                else:
                    self.reply(200, BODY, "video/x-matroska")

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
        return len(self.frames)

    def since(self, mark):
        return self.frames[mark:]

    def scan_ended(self, mark, timeout=30):
        """Wait for a library scan that started after `mark` to end."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for name, frame in self.since(mark):
                body = frame.get("ActivityNotification") or {}
                rows = body if isinstance(body, list) else [body]
                if any(row.get("event") == "ended"
                       and (row.get("Activity") or {}).get("type") == "library.update.section"
                       for row in rows):
                    return True
            time.sleep(0.1)
        return False


def timeline(frames):
    out = []
    for name, frame in frames:
        if name == "timeline":
            entry = frame.get("TimelineEntry")
            out.extend(entry if isinstance(entry, list) else [entry])
    return out


class Server:
    def __init__(self, work, zurg_port):
        self.port = free_port()
        env = dict(os.environ, ZURG_URL="http://127.0.0.1:%d" % zurg_port, ZURG_USER="zuser",
                   ZURG_PASSWORD="zpass", PLEXSIM_PLEXTV_URL="http://127.0.0.1:9")
        for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN") + (() if KEYED else ("TMDB_KEY", "PLEX_TOKEN")):
            env.pop(name, None)
        self.log = open(os.path.join(work, "server.log"), "ab")
        self.process = subprocess.Popen(
            server_command("--port", self.port, "--catalog", os.path.join(work, "catalog.json"),
                           "--state", os.path.join(work, "plexsim.db"), "--zurg-mount", "/zurg",
                           "--assert-claimed", "--gdm", "off", root=ROOT),
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(160):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/identity" % self.port, timeout=2)
                break
            except Exception:
                time.sleep(0.25)
        self.events = Events(self.port)

    def get(self, path, method="GET", headers=None):
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method,
                                         headers=dict({"Accept": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw[:1] == b"{" else raw
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def rows(self, path):
        status, body = self.get(path)
        return ((body or {}).get("MediaContainer") or {}).get("Metadata") or [] if status == 200 else []

    def refresh(self, section, path=None, wait=True):
        mark = self.events.mark()
        query = "?path=" + urllib.parse.quote(path) if path else ""
        status, _ = self.get("/library/sections/%s/refresh%s" % (section, query))
        ended = self.events.scan_ended(mark) if wait else None
        return status, ended, mark

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.log.close()


def main():
    work = tempfile.mkdtemp(prefix="plexsim-zurg-scan-")
    zurg = Zurg()
    movie = FIXTURE["movie"]
    episode = FIXTURE["episode"]
    zurg.add("movies", movie["release"], movie["file"], movie["entry"])
    zurg.add("shows", episode["release"], episode["file"], episode["entry"])
    zurg_port = serve(zurg.handler())
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000zz",
               "friendlyName": "plexsim-zurg-scan",
               "sections": [{"key": "1", "type": "movie", "title": "Movies",
                             "uuid": "0000000000000000000000000000000000000001",
                             "Location": [{"id": 1, "path": "/zurg/movies"}]},
                            {"key": "2", "type": "show", "title": "TV Shows",
                             "uuid": "0000000000000000000000000000000000000002",
                             "Location": [{"id": 2, "path": "/zurg/shows"}]}],
               "items": []}, open(os.path.join(work, "catalog.json"), "w"))
    zurg.loading = True
    server = Server(work, zurg_port)
    try:
        # -- the start: zurg still loading, then ready --------------------------------
        time.sleep(2)
        check("while zurg answers 503 the start's scan adds nothing and waits",
              server.rows("/library/sections/1/all") == [])
        mark = server.events.mark()
        zurg.loading = False
        check("once zurg is ready the start's scan runs", server.events.scan_ended(mark, 30),
              server.events.since(mark)[-3:])
        deadline = time.time() + 30
        while time.time() < deadline and not (server.rows("/library/sections/1/all")
                                               and server.rows("/library/sections/2/all")):
            time.sleep(0.25)
        time.sleep(0.5)
        movies = server.rows("/library/sections/1/all?includeGuids=1")
        found = movies[0] if movies else {}
        part = ((found.get("Media") or [{}])[0].get("Part") or [{}])[0]
        check("the movie is in its library, titled from its release name",
              len(movies) == 1 and found.get("title") == "Blame It on the Bellboy"
              and found.get("year") == 1992, movies)
        check("its ratingKey is a minted string above every catalog range",
              isinstance(found.get("ratingKey"), str) and int(found["ratingKey"]) >= 100000,
              found.get("ratingKey"))
        check("its Part's file is zurg's mount path, the path zurg's matcher reads back",
              part.get("file") == "/zurg/movies/%s/%s" % (movie["release"], movie["file"]),
              part.get("file"))
        detail = (server.rows("/library/metadata/%s" % found.get("ratingKey")) or [{}])[0]
        media = (detail.get("Media") or [{}])[0]
        streams = ((media.get("Part") or [{}])[0].get("Stream") or [])
        check("its Media and Streams are zurg's stored probe",
              media.get("videoCodec") == "h264" and media.get("container") == "mkv"
              and media.get("duration") == 4707078 and len(streams) == 4,
              {k: media.get(k) for k in ("videoCodec", "container", "duration")})
        if KEYED:
            check("zurg's IMDb id names it exactly: Plex's own guid and both external ids",
                  (found.get("guid") or "").startswith("plex://movie/")
                  and {"id": "imdb://tt0103827"} in (found.get("Guid") or [])
                  and any(g.get("id", "").startswith("tmdb://") for g in found.get("Guid") or []),
                  (found.get("guid"), found.get("Guid")))
        else:
            check("unidentified, it carries the local agent's guid and no private field",
                  found.get("guid") == "local://%s" % found.get("ratingKey")
                  and not any(k.startswith("_") for k in found), found.get("guid"))
        episodes = server.rows("/library/sections/2/all?type=4&includeGuids=1")
        shows = server.rows("/library/sections/2/all?includeGuids=1")
        show_episode = episodes[0] if episodes else {}
        check("the episode is placed under a show and a season of its own",
              len(episodes) == 1 and len(shows) == 1
              and show_episode.get("index") == 7 and show_episode.get("parentIndex") == 5
              and show_episode.get("grandparentTitle") == "American Dad!"
              and show_episode.get("grandparentRatingKey") == shows[0].get("ratingKey"),
              (episodes, shows))
        if KEYED:
            show = shows[0] if shows else {}
            check("the show and the episode carry Plex's guids, the episode TMDB's title",
                  (show.get("guid") or "").startswith("plex://show/")
                  and {"id": "imdb://tt0397306"} in (show.get("Guid") or [])
                  and (show_episode.get("guid") or "").startswith("plex://episode/")
                  and show_episode.get("title") not in ("Episode 7", "American Dad!"),
                  (show.get("guid"), show.get("Guid"), show_episode.get("guid"),
                   show_episode.get("title")))
        keys = {row["ratingKey"] for row in movies + episodes + shows}

        # -- playback reads zurg's WebDAV with zurg's credential ----------------------
        status, body = server.get(part.get("key", "/none"), headers={"Range": "bytes=100-199"})
        check("a part read streams the file's bytes from zurg, Range forwarded",
              status == 206 and body == BODY[100:200], status)

        # -- a new release, then its probe arriving later -------------------------------
        zurg.add("movies", NEW, NEW + ".mkv",
                 {"hash": "a" * 40, "name": NEW, "size": 12345,
                  "signed_path": "/strm/e/x", "media_info": None})
        zurg.entry_delay = 1.5
        mark = server.events.mark()
        server.get("/library/sections/1/refresh?path=" + urllib.parse.quote(
            "/zurg/movies/" + NEW))
        time.sleep(0.6)
        status, activities = server.get("/activities")
        listed = ((activities or {}).get("MediaContainer") or {}).get("Activity") or []
        status, sections = server.get("/library/sections")
        refreshing = [row.get("refreshing") for row in
                      ((sections or {}).get("MediaContainer") or {}).get("Directory") or []]
        check("while it scans, /activities lists a library.update.section activity",
              [row.get("type") for row in listed] == ["library.update.section"]
              and (listed[0].get("Context") or {}).get("librarySectionID") == "1", activities)
        check("and its library reports refreshing", refreshing == [True, False], refreshing)
        server.events.scan_ended(mark)
        zurg.entry_delay = 0
        time.sleep(1)  # a new row's metadata frames follow the scan's end
        frames = server.events.since(mark)
        new = [row for row in server.rows("/library/sections/1/all") if row.get("title") == "Qzxv Wmplk"]
        new_key = new[0]["ratingKey"] if new else None
        created = [entry for entry in timeline(frames) if entry.get("itemID") == new_key]
        check("a path refresh adds the new release's file",
              len(new) == 1 and new[0].get("year") == 2024, new)
        check("the measured creation frames: state 0 untitled, then state 1 created",
              [(e.get("state"), e.get("metadataState"), e.get("title") == "") for e in created][:2]
              == [(0, "created", True), (1, "created", False)]
              and created[-1].get("state") == 5, created)
        new_detail = (server.rows("/library/metadata/%s" % new_key) or [{}])[0]
        new_part = ((new_detail.get("Media") or [{}])[0].get("Part") or [{}])[0]
        check("with no probe yet it has a Part and no Stream",
              new_part.get("file", "").endswith("/%s.mkv" % NEW)
              and not new_part.get("Stream"), new_part)
        zurg.files["/dav/movies/%s/%s.mkv" % (NEW, NEW)][
            "media_info"] = movie["entry"]["media_info"]
        server.refresh(1, "/zurg/movies/" + NEW)
        filled = (server.rows("/library/metadata/%s" % new_key) or [{}])[0]
        check("a later scan fills its streams in, on the same key",
              len(((filled.get("Media") or [{}])[0].get("Part") or [{}])[0].get("Stream") or []) == 4,
              filled.get("Media"))

        # -- keys never change: rescans, a restart, a restart's own scan ---------------
        server.refresh(1)
        server.refresh(2)
        after = {row["ratingKey"] for row in server.rows("/library/sections/1/all")
                 + server.rows("/library/sections/2/all?type=4") + server.rows("/library/sections/2/all")}
        check("a whole-library rescan changes no key and adds nothing", after == keys | {new_key}, after)
        server.stop()
        server = Server(work, zurg_port)
        time.sleep(4)
        restarted = {row["ratingKey"] for row in server.rows("/library/sections/1/all")
                     + server.rows("/library/sections/2/all?type=4") + server.rows("/library/sections/2/all")}
        check("a restart serves the same library on the same keys", restarted == after, restarted)

        # -- a release gone: trashed, back, gone again, emptied ------------------------
        zurg.remove_release("movies", NEW)
        status, ended, mark = server.refresh(1, "/zurg/movies/" + NEW)
        trashed = (server.rows("/library/metadata/%s" % new_key) or [{}])[0]
        pair = [(e.get("type"), e.get("state")) for e in timeline(server.events.since(mark))
                if e.get("itemID") == new_key]
        check("a refresh of a release zurg no longer has answers 200 and scans", status == 200 and ended)
        check("its row stays, soft-deleted: deletedAt on the row and its Media, not the Part",
              trashed.get("deletedAt") and (trashed.get("Media") or [{}])[0].get("deletedAt")
              and "deletedAt" not in ((trashed.get("Media") or [{}])[0].get("Part") or [{}])[0], trashed)
        check("announced as the measured type -1 state 3 / state 5 pair", pair == [(-1, 3), (-1, 5)], pair)
        listed = [row.get("ratingKey") for row in server.rows("/library/sections/1/all?trash=1")]
        check("trash=1 lists the trashed movie alone", listed == [new_key], listed)
        zurg.add("movies", NEW, NEW + ".mkv",
                 {"hash": "a" * 40, "name": NEW, "size": 12345,
                  "signed_path": "/strm/e/x", "media_info": movie["entry"]["media_info"]})
        server.refresh(1, "/zurg/movies/" + NEW)
        back = (server.rows("/library/metadata/%s" % new_key) or [{}])[0]
        check("the file back before the trash is emptied restores the same row",
              back.get("ratingKey") == new_key and "deletedAt" not in back, back.get("deletedAt"))
        zurg.remove_release("movies", NEW)
        server.refresh(1, "/zurg/movies/" + NEW)
        mark = server.events.mark()
        status, _ = server.get("/library/sections/1/emptyTrash", method="PUT")
        time.sleep(0.5)
        deleted = [(e.get("type"), e.get("state"), e.get("metadataState"))
                   for e in timeline(server.events.since(mark)) if e.get("itemID") == new_key]
        check("emptying the trash deletes the row with one state 9 frame",
              status == 200 and server.get("/library/metadata/%s" % new_key)[0] == 404
              and deleted == [(1, 9, "deleted")], (status, deleted))

        # -- what is never "everything gone" -------------------------------------------
        before = {row["ratingKey"] for row in server.rows("/library/sections/1/all")}
        zurg.loading = True
        server.refresh(1, wait=False)
        time.sleep(3)
        check("a scan while zurg is loading changes nothing",
              {row["ratingKey"] for row in server.rows("/library/sections/1/all")} == before
              and not server.rows("/library/sections/1/all?trash=1"))
        zurg.loading = False
        time.sleep(7)
        saved = dict(zurg.files)
        zurg.files = {path: entry for path, entry in saved.items() if not path.startswith("/dav/movies/")}
        server.refresh(1)
        check("an empty library folder trashes nothing (measured: 6b3)",
              not server.rows("/library/sections/1/all?trash=1")
              and {row["ratingKey"] for row in server.rows("/library/sections/1/all")} == before)
        zurg.files = saved

        # -- an episode gone with autoEmptyTrash on: deleted in the scan, parents too ---
        server.get("/:/prefs?autoEmptyTrash=1", method="PUT")
        episode_key = show_episode.get("ratingKey")
        zurg.remove_release("shows", episode["release"])
        status, ended, mark = server.refresh(2, "/zurg/shows/" + episode["release"])
        gone = [(e.get("type"), e.get("state"), e.get("metadataState"))
                for e in timeline(server.events.since(mark)) if e.get("itemID") == episode_key]
        check("with autoEmptyTrash on, a missing episode is deleted during the scan (5a)",
              server.get("/library/metadata/%s" % episode_key)[0] == 404
              and gone == [(4, 9, "deleted")], gone)
        check("and its now-empty season and show go with it",
              server.rows("/library/sections/2/all") == [], server.rows("/library/sections/2/all"))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    failed = results.count(False)
    print("\n%d/%d zurg scan checks passed" % (len(results) - failed, len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    if os.environ.get("PLEXSIM_SERVER") == "python":
        print("the Python oracle does not scan; this suite is native only")
        sys.exit(0)
    sys.exit(main())
