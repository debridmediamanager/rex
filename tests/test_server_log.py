#!/usr/bin/env python3
"""The server's own log, as the Console and Download Logs hand it to a client.

Every route that logs (as the Python oracle did) writes one line with its own text and level, on
the thread that served the request, and that line reaches Download Logs
(`/diagnostics/logs`, `rex.log`) and every event channel opened with
`filters=log` as a `LogSentNotification`. A server that answers the same bytes but
logs nothing leaves the Troubleshooting page and the Console empty, which is exactly
what the Console exists to show.

Also here, because they are the channels those lines travel on: `filters=` is an
exact whitelist on the SSE channel as on the websocket, the SSE channel STREAMS past
its first ping, an edit pushes its real `EDIT` line and nothing invented, and a
websocket CLOSE is echoed with 1000.

Drives real server processes (`tests/server_command.py`). The source registered first is a
zurg on a closed port: its part read expects the transport failure's 502.

    python3 tests/test_server_log.py
"""
import base64
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for _name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(_name, None)
HEADERS = {"X-Plex-Client-Identifier": "logcheck", "X-Plex-Product": "Plex Web",
           "X-Plex-Version": "4.147.1", "X-Plex-Platform": "Chrome",
           "Accept": "application/json"}
STAMP = re.compile(r"^[A-Z][a-z]{2} \d\d, \d{4} \d\d:\d\d:\d\d\.\d{3} ")
REQUEST_THREAD = re.compile(r"^\[Thread-(\d+) \(process_request_thread\)\] ")

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


class Server:
    def __init__(self):
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.connections = 0
        self.process = subprocess.Popen(
            server_command("--port", self.port, "--catalog", "tests/catalog_ui.json",
                           "--assert-claimed", "--gdm", "off", root=ROOT),
            cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(120):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.25)
        # The probe above was a connection too: the server numbers its request
        # threads per connection, the way `socketserver.ThreadingMixIn` does.
        self.connections = 1

    def request(self, method, path, body=None, headers=None):
        """One request on its own connection; returns (status, body, thread ordinal)."""
        self.connections += 1
        ordinal = self.connections
        request = urllib.request.Request(self.base + path, method=method, data=body,
                                         headers=dict(HEADERS, **(headers or {})))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read(), ordinal
        except urllib.error.HTTPError as error:
            return error.code, error.read(), ordinal
        except Exception as error:
            return None, repr(error).encode(), ordinal

    def log_lines(self):
        status, body, _ = self.request("GET", "/diagnostics/logs")
        if status != 200:
            return []
        text = zipfile.ZipFile(io.BytesIO(body)).read("rex.log").decode("utf-8", "replace")
        return [STAMP.sub("", line) for line in text.splitlines() if line]

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)


def container(body):
    try:
        return json.loads(body).get("MediaContainer", {})
    except ValueError:
        return {}


def expect(server, label, method, path, wanted, body=None, headers=None, status=None):
    """Run one request; `wanted` is [(LEVEL, message regex)] the request must log, in
    order, on its own request thread."""
    got_status, got_body, ordinal = server.request(method, path, body, headers)
    return {"label": label, "status": got_status, "wanted_status": status,
            "wanted": wanted, "thread": ordinal, "body": got_body}


def verify(runs, lines):
    """Match each run's wanted lines, in order, among the lines of its own thread."""
    by_thread = {}
    for line in lines:
        match = REQUEST_THREAD.match(line)
        if match:
            by_thread.setdefault(int(match.group(1)), []).append(line[match.end():])
    for run in runs:
        own = by_thread.get(run["thread"], [])
        position, missing = 0, []
        for level, pattern in run["wanted"]:
            regex = re.compile("^%s - %s$" % (level, pattern))
            while position < len(own) and not regex.match(own[position]):
                position += 1
            if position == len(own):
                missing.append("%s - %s" % (level, pattern))
                position = 0
            else:
                position += 1
        ok = not missing and (run["wanted_status"] is None or run["status"] == run["wanted_status"])
        check("%s logs its line%s on Thread-%d" % (run["label"], "s" if len(run["wanted"]) > 1 else "",
                                                   run["thread"]),
              ok, "status %s; missing %s; thread %d logged %s"
              % (run["status"], missing, run["thread"], own))


def sse(server, spec, seconds, trigger=None, trigger_after=0.5):
    """Hold `/:/eventsource/notifications<spec>` open for `seconds`, running `trigger`
    after `trigger_after` seconds. Returns [(event, data)] in arrival order."""
    events = []
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=seconds + 5)
    server.connections += 1
    sock.sendall(("GET /:/eventsource/notifications%s HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                  "Accept: text/event-stream\r\n\r\n" % spec).encode())
    if trigger is not None:
        threading.Timer(trigger_after, trigger).start()
    buffer = b""
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            sock.settimeout(max(deadline - time.time(), 0.05))
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buffer += chunk
    finally:
        sock.close()
    body = buffer.partition(b"\r\n\r\n")[2].decode("utf-8", "replace")
    name = None
    for line in body.split("\n"):
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and name:
            events.append((name, line.split(":", 1)[1].strip()))
            name = None
    return events


def main():
    server = Server()
    try:
        machine = container(server.request("GET", "/identity")[1]).get("machineIdentifier")
        item = container(server.request("GET", "/library/metadata/1007")[1]).get("Metadata", [{}])[0]
        part = item.get("Media", [{}])[0].get("Part", [{}])[0]
        part_id = part.get("id")
        audio = [stream["id"] for stream in part.get("Stream", []) if stream.get("streamType") == 2]
        uri = urllib.parse.quote(
            "server://%s/com.plexapp.plugins.library/library/metadata/1007" % machine)
        uri2 = urllib.parse.quote(
            "server://%s/com.plexapp.plugins.library/library/metadata/1008" % machine)
        M = "/library/metadata/"
        runs = []

        def run(label, method, path, wanted, **kwargs):
            runs.append(expect(server, label, method, path, wanted, **kwargs))
            return runs[-1]

        E = re.escape
        # zurg on a closed port: every part read is a transport failure, and its log lines are.
        run("a source registration", "PUT", "/rex/sources/zurg",
            [("INFO", E("SOURCES zurg registered at http://127.0.0.1:9/dav")),
             ("INFO", E("SOURCES PUT /rex/sources/zurg -> 200"))],
            body=json.dumps({"url": "http://127.0.0.1:9/dav", "mount": "/rex"}).encode(),
            headers={"Content-Type": "application/json"}, status=200)
        run("a refused filter", "GET", "/library/sections/1/all?year%3E%3E=abc",
            [("WARNING", E("REFUSED /library/sections/1/all: unparseable number 'abc'")),
             ("INFO", E("500 GET /library/sections/1/all -- rex: unparseable number 'abc'"))],
            status=500)
        run("an unsupported sort", "GET", "/library/sections/1/all?sort=bogusKey",
            [("WARNING", E("UNSUPPORTED sort key 'bogusKey'")),
             ("INFO", E("500 GET /library/sections/1/all -- rex: unsupported sort key bogusKey"))],
            status=500)
        run("an updater apply", "PUT", "/updater/apply",
            [("WARNING", E("UPDATER apply requested -- nothing downloaded")),
             ("INFO", E("400 PUT /updater/apply -- rex: bad request"))], status=400)
        run("a client log GET", "GET", "/log?level=2&source=web&message=hello%20there",
            [("INFO", E("CLIENT LOG [2] web: hello there"))], status=200)
        run("a client log POST", "POST", "/log?level=3&message=posted",
            [("INFO", E("CLIENT LOG [3] client: posted"))], status=200)
        run("a client log with a bad level", "POST", "/log?level=x&message=posted",
            [("INFO", E("400 POST /log -- rex: bad request"))], status=400)
        run("a database optimize", "PUT", "/library/optimize",
            [("INFO", E("OPTIMIZE database -- rebuilt the search vocabulary"))], status=200)
        run("an unknown single-segment path", "GET", "/foo",
            [("INFO", E("PLUGIN PATH GET /foo -- legacy dispatcher"))], status=200)
        run("a named single-segment plug-in path", "GET", "/prefs",
            [("INFO", E("PLUGIN PATH GET /prefs -- legacy dispatcher"))], status=200)
        run("the bare sync path", "GET", "/sync",
            [("INFO", E("PLUGIN PATH GET /sync -- legacy dispatcher"))], status=200)
        run("an unknown path", "GET", "/nothing/here",
            [("WARNING", E("UNKNOWN GET /nothing/here")),
             ("INFO", E("404 GET /nothing/here -- rex: not found"))], status=404)
        run("a section edit", "PUT",
            "/library/sections/1?name=Renamed&agent=tv.plex.agents.movie&language=en-US",
            [("INFO", E("SECTION edit 1 -> Renamed"))], status=200)
        run("a section scan", "GET", "/library/sections/1/refresh",
            [("INFO", E("SECTION refresh 1 force=False -> 6 row(s)"))], status=200)
        run("a forced section scan", "GET", "/library/sections/1/refresh?force=1",
            [("INFO", E("SECTION refresh 1 force=True -> 6 row(s)"))], status=200)
        run("a scan of a folder the section lacks", "GET",
            "/library/sections/1/refresh?path=/nowhere",
            [("INFO", E("SECTION refresh 1 path='/nowhere' -> no folder"))], status=200)
        run("a scan cancel", "DELETE", "/library/sections/1/refresh",
            [("INFO", E("SECTION refresh cancelled 1"))], status=200)
        run("a section analyze", "PUT", "/library/sections/1/analyze",
            [("INFO", E("SECTION analyze 1 -- nothing to analyze, rex owns no files"))],
            status=200)
        run("a section empty-trash", "PUT", "/library/sections/1/emptyTrash",
            [("INFO", E("SECTION emptyTrash 1 -- nothing to emptyTrash, rex owns no files"))],
            status=200)
        run("a section preference", "PUT", "/library/sections/1/prefs?enableCinemaTrailers=0",
            [("INFO", E("SECTION PREF 1 enableCinemaTrailers = false"))], status=200)
        run("an unknown section leaf", "GET", "/library/sections/1/bogusleaf",
            [("WARNING", E("UNKNOWN section leaf bogusleaf on section 1")),
             ("INFO", E("404 GET /library/sections/1/bogusleaf -- rex: not found"))],
            status=404)
        run("an item edit", "PUT", M + "1007?title.value=Edited&title.locked=1",
            [("INFO", E("EDIT 1007 <- title.value,title.locked"))], status=200)
        run("a refused item edit", "PUT", M + "1007?year.value=abc",
            [("WARNING", E("EDIT REFUSED: unparseable year 'abc'")),
             ("INFO", E("500 PUT /library/metadata/1007 -- rex: unparseable year 'abc'"))],
            status=500)
        run("an item preference", "PUT", M + "20001/prefs?episodeSort=1",
            [("INFO", E("ITEM PREF 20001 episodeSort = 1"))], status=200)
        run("an unknown item leaf", "GET", M + "1007/bogusleaf",
            [("WARNING", E("UNKNOWN item leaf bogusleaf on 1007")),
             ("INFO", E("404 GET /library/metadata/1007/bogusleaf -- rex: not found"))],
            status=404)
        run("an item refresh", "PUT", M + "1007/refresh",
            [("INFO", E("REFRESH 1007 (Edited)"))], status=200)
        run("a show refresh", "PUT", M + "20001/refresh",
            [("INFO", E("REFRESH 20001 (The Simpsons) -- cascaded"))], status=200)
        run("an item analyze", "PUT", M + "1007/analyze",
            [("INFO", E("ANALYZE 1007 -- 1 leaf frame set(s)"))], status=200)
        run("a match search with no Plex agent", "GET", M + "1008/matches?manual=1",
            [("INFO", E("MATCHES 1008 -- no Plex agent (not claimed)"))], status=200)
        run("an unmatch", "PUT", M + "1009/unmatch",
            [("INFO", E("UNMATCH 1009 -> guid=tv.plex.agents.none://1009"))], status=200)
        run("a match", "PUT", M + "1009/match?guid=plex%3A%2F%2Fmovie%2F5d7768265af944001f1f6a4e"
            "&name=Tears%20of%20Steel",
            [("INFO", E("MATCH 1009 -> plex://movie/5d7768265af944001f1f6a4e"))], status=200)
        run("a split", "PUT", M + "1009/split", [("INFO", E("SPLIT 1009 -> 0 new row(s)"))],
            status=200)
        run("a merge", "PUT", M + "1008/merge?ids=1009",
            [("INFO", E("MERGE 1008 <- ['1009'] (1 row(s) absorbed)"))], status=200)
        # The delete goes to the file's source first (media_delete_s43.json), and this zurg is
        # on a closed port: the row stays, the refusal is logged and the answer is the 400 a
        # real PMS gives a file it cannot remove.
        run("an item delete its source cannot reach", "DELETE", M + "1014",
            [("WARNING", r"DELETE refused by the source, row kept: .*Vinaipayan.* at zurg: .*")],
            status=400)
        run("a play queue", "POST", "/playQueues?type=video&uri=%s&shuffle=0&repeat=0&own=1" % uri,
            [("INFO", r"PLAYQUEUE \d+ -> 1 item\(s\) from uri="
              + E(urllib.parse.unquote(uri)[:80]))], status=200)
        run("an MDE decision", "GET", "/video/:/transcode/universal/decision?path=%2Flibrary"
            "%2Fmetadata%2F1007&session=lp1&directPlay=1&directStream=1&protocol=http"
            "&mediaIndex=0&partIndex=0&hasMDE=1",
            [("INFO", E("DECISION direct play for path=/library/metadata/1007"))], status=200)
        run("a refused decision", "GET", "/video/:/transcode/universal/decision?path=%2Flibrary"
            "%2Fmetadata%2F1007&session=lp3&directPlay=0&directStream=0&protocol=dash"
            "&mediaIndex=0&partIndex=0&hasMDE=1",
            [("INFO", E("DECISION refused for path=/library/metadata/1007"))], status=200)
        run("a transcode start after a decision", "GET", "/video/:/transcode/universal/"
            "start.m3u8?path=%2Flibrary%2Fmetadata%2F1007&session=lp3&protocol=hls",
            [("WARNING", E("TRANSCODE REQUESTED -- client refused direct play: "
                           "/library/metadata/1007")),
             ("INFO", E("404 GET /video/:/transcode/universal/start.m3u8 -- rex: no transcoder"))],
            status=404)
        run("a timeline beat", "GET", "/:/timeline?ratingKey=1007&key=%2Flibrary%2Fmetadata%2F1007"
            "&state=playing&time=1000&duration=10000",
            [("INFO", r"TIMELINE 1007 state=playing t=1000ms session=\S+")], status=200,
            headers={"X-Plex-Session-Identifier": "lp1"})
        # A playing client beats every 10 s: the same item and state again is DEBUG,
        # so it leaves no line here; a new state is INFO again.
        repeat_beat = run("a repeated timeline beat", "GET", "/:/timeline?ratingKey=1007"
            "&key=%2Flibrary%2Fmetadata%2F1007&state=playing&time=3000&duration=10000",
            [], status=200, headers={"X-Plex-Session-Identifier": "lp1"})
        run("a timeline pause", "GET", "/:/timeline?ratingKey=1007&key=%2Flibrary%2Fmetadata%2F1007"
            "&state=paused&time=4000&duration=10000",
            [("INFO", r"TIMELINE 1007 state=paused t=4000ms session=\S+")], status=200,
            headers={"X-Plex-Session-Identifier": "lp1"})
        run("a terminate of no session", "GET", "/status/sessions/terminate?sessionId=zzz&reason=bye",
            [("INFO", E("404 GET /status/sessions/terminate -- rex: no such session"))],
            status=404)
        run("a progress write", "GET", "/:/progress?key=1007&identifier=com.plexapp.plugins.library"
            "&time=2000&state=paused",
            [("INFO", E("PROGRESS 1007 t=2000ms state=paused"))], status=200)
        run("a show scrobble", "GET", "/:/scrobble?key=20001&identifier=com.plexapp.plugins.library",
            [("INFO", E("SCROBBLE 20001 -> 8 leaf row(s)"))], status=200)
        run("an unscrobble", "GET", "/:/unscrobble?key=1007&identifier=com.plexapp.plugins.library",
            [("INFO", E("UNSCROBBLE 1007 -> 1 leaf row(s)"))], status=200)
        run("a rating", "PUT", "/:/rate?key=1007&identifier=com.plexapp.plugins.library&rating=8",
            [("INFO", E("RATE 1007 -> 8"))], status=200)
        if audio:
            run("a track selection", "PUT", "/library/parts/%s?audioStreamID=%s&allParts=1"
                % (part_id, audio[-1]),
                [("INFO", E("STREAM SELECT part=%s item=1007 {2: '%s'}" % (part_id, audio[-1])))],
                status=200)
        run("a refused track selection", "PUT", "/library/parts/%s?audioStreamID=999999&allParts=1"
            % part_id,
            [("WARNING", E("STREAM SELECT refused: 999999 is not a type-2 stream of 1007")),
             ("INFO", E("400 PUT /library/parts/%s -- rex: bad request" % part_id))],
            status=400)
        run("a subtitle upload with no extension", "POST",
            M + "1007/subtitles?title=t&language=en&format=srt",
            [("INFO", E("500 POST /library/metadata/1007/subtitles -- rex: internal error"))],
            body=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n", status=500)
        run("a subtitle upload", "POST", M + "1007/subtitles?title=t.srt&language=en&format=srt",
            [("INFO", r"SUBTITLE upload 1007 -> stream \d+ \(srt\)")],
            body=b"1\n00:00:01,000 --> 00:00:02,000\nhi\n", status=200)
        run("a poster upload", "POST", M + "1007/posters",
            [("INFO", E("ARTWORK upload 1007 posters 72 bytes -> "
                        "upload://posters/55effc5a6ff3e3c16520f3a325e90fe569b2e66e"))],
            body=b"\x89PNG\r\n\x1a\n" + b"\0" * 64, status=200)
        run("a poster pick", "PUT", M + "1007/poster?url=%s"
            % urllib.parse.quote("upload://posters/55effc5a6ff3e3c16520f3a325e90fe569b2e66e"),
            [("INFO", E("ARTWORK 1007 posters -> "
                        "upload://posters/55effc5a6ff3e3c16520f3a325e90fe569b2e66e"))], status=200)
        created = run("a collection create", "POST",
                      "/library/collections?type=1&title=LogColl&smart=0&sectionId=1&uri=%s" % uri,
                      [("INFO", E("COLLECTION create LogColl smart=False (1 item(s))"))], status=200)
        collection = (container(created["body"]).get("Metadata") or [{}])[0].get("ratingKey")
        run("a collection preference", "PUT", M + "%s/prefs?collectionMode=1" % collection,
            [("INFO", E("COLLECTION PREF %s collectionMode,_pairs" % collection))], status=200)
        run("a collection edit", "PUT", M + "%s?title.value=LogColl2&title.locked=1" % collection,
            [("INFO", E("EDIT list %s <- title.value,title.locked" % collection))], status=200)
        created = run("a playlist create", "POST",
                      "/playlists?type=video&title=LogPl&smart=0&uri=%s" % uri,
                      [("INFO", E("PLAYLIST create LogPl smart=False (1 item(s))"))], status=200)
        playlist = (container(created["body"]).get("Metadata") or [{}])[0].get("ratingKey")
        run("a playlist add", "PUT", "/playlists/%s/items?uri=%s" % (playlist, uri2),
            [("INFO", E("PLAYLIST %s += 1 item(s)" % playlist))], status=200)
        smart_uri = "server://%s/com.plexapp.plugins.library/library/sections/1/all?type=1" % machine
        created = run("a smart playlist create", "POST", "/playlists?type=video&title=LogSmart"
                      "&smart=1&uri=%s" % urllib.parse.quote(smart_uri),
                      [("INFO", r"PLAYLIST create LogSmart smart=True \(\d+ item\(s\)\)")],
                      status=200)
        smart = (container(created["body"]).get("Metadata") or [{}])[0].get("ratingKey")
        saved = smart_uri + "&year=2012"
        run("a smart playlist save", "PUT", "/playlists/%s/items?uri=%s"
            % (smart, urllib.parse.quote(saved)),
            [("INFO", E("PLAYLIST %s query <- %s" % (smart, saved[:120])))], status=200)
        queue = container(server.request("POST", "/playQueues?type=video&uri=%s" % uri)[1]) \
            .get("playQueueID")
        run("a play queue add", "PUT", "/playQueues/%s?uri=%s" % (queue, uri2),
            [("INFO", E("PLAYQUEUE %s append 1 item(s)" % queue))], status=200)
        run("a play queue Play Next", "PUT", "/playQueues/%s?uri=%s&next=1" % (queue, uri2),
            [("INFO", E("PLAYQUEUE %s next 1 item(s)" % queue))], status=200)
        run("a server preference", "PUT", "/:/prefs?FriendlyName=LogName",
            [("INFO", E("PREF FriendlyName = LogName"))], status=200)
        run("an empty maintenance window", "PUT", "/:/prefs?ButlerStartHour=4&ButlerEndHour=4",
            [("INFO", E("PREF ButlerStartHour = 4")), ("INFO", E("PREF ButlerEndHour = 4")),
             ("INFO", E("PREF ButlerEndHour = 5 (an empty window)"))], status=200)
        run("a butler task", "POST", "/butler/OptimizeDatabase",
            [("INFO", E("BUTLER ran OptimizeDatabase"))], status=200)
        history = container(server.request("GET", "/status/sessions/history/all?type=1")[1]) \
            .get("Metadata") or []
        if history:
            key = str(history[0].get("historyKey", "")).rsplit("/", 1)[-1]
            run("a history delete", "DELETE", "/status/sessions/history/%s" % key,
                [("INFO", E("HISTORY delete %s" % key))], status=200)
        else:
            check("the scrobble left a history row to delete", False, "no history")
        run("a library create", "POST", "/library/sections?name=LogLib&type=movie"
            "&agent=tv.plex.agents.movie&scanner=Plex%20Movie&language=en-US&location=%2Fdata%2Flog",
            [("INFO", E("SECTION create LogLib (movie)"))], status=201)
        sections = container(server.request("GET", "/library/sections")[1]).get("Directory") or []
        added = [row.get("key") for row in sections if row.get("title") == "LogLib"]
        if added:
            run("a library delete", "DELETE", "/library/sections/%s" % added[0],
                [("INFO", E("SECTION delete %s (LogLib)" % added[0]))], status=200)
        run("a part read zurg cannot answer", "GET", "/library/parts/%s/0/file.mkv" % part_id,
            [("ERROR", E("PART %s upstream failed: <urlopen error " % part_id) + ".*>"),
             ("INFO", E("502 GET /library/parts/%s/0/file.mkv -- rex: upstream failed"
                        % part_id))], headers={"Range": "bytes=0-99"}, status=502)
        run("a remote artwork read", "GET", "/photo/:/transcode?width=100&height=100&url=%s"
            % urllib.parse.quote("http://127.0.0.1:9/x.jpg"),
            [("INFO", E("ARTWORK fetch http://127.0.0.1:9/x.jpg")),
             ("INFO", E("404 GET /photo/:/transcode -- rex: artwork unavailable"))],
            status=404)

        lines = server.log_lines()
        startup = [line for line in lines if line.startswith("[MainThread] ")]
        check("the startup lines are the main thread's",
              any(line == "[MainThread] INFO - claim state: unclaimed" for line in startup)
              and any(line.startswith("[MainThread] INFO - token gate: OFF") for line in startup),
              startup[:4])
        verify(runs, lines)
        repeat_lines = [line for line in lines
                        if line.startswith("[Thread-%d " % repeat_beat["thread"]) and "TIMELINE" in line]
        check("a repeated timeline beat logs no INFO line", not repeat_lines, repeat_lines)

        # An edit pushes its own EDIT line to a `filters=log` channel -- and no invented
        # "metadata updated" row beside it.
        frames = sse(server, "?filters=log", 4, lambda: server.request(
            "PUT", M + "1007?tagline.value=%s" % urllib.parse.quote("logged")))
        rows = []
        for name, data in frames:
            try:
                rows.append((name, json.loads(data).get("LogSentNotification") or {}))
            except ValueError:
                rows.append((name, {}))
        messages = [row.get("message") for name, row in rows if name == "log"]
        check("a filters=log channel carries the edit's own EDIT line",
              "EDIT 1007 <- tagline.value" in messages
              and all(row.get("level") in (0, 1, 2, 3, 4) for name, row in rows if name == "log"),
              messages)
        check("an edit invents no 'metadata updated' log row",
              "metadata updated" not in messages, messages)

        # `filters=` is a whitelist: a scan pushes activity, status and progress frames
        # and an edit timeline ones, and a `filters=timeline` channel carries the
        # timeline ones alone.
        def scan_and_edit():
            server.request("GET", "/library/sections/1/refresh")
            server.request("PUT", M + "1007?tagline.value=%s" % urllib.parse.quote("both"))
        frames = sse(server, "?filters=timeline", 4, scan_and_edit)
        kinds = sorted({name for name, _data in frames if name != "ping"})
        check("filters=timeline is an exact whitelist on the SSE channel",
              kinds == ["timeline"], kinds)

        # The channel STREAMS: a write eleven seconds in, after the first ping, still
        # arrives.
        frames = sse(server, "?filters=timeline", 14,
                     lambda: server.request("PUT", M + "1007?tagline.value=late"), 11.5)
        names = [name for name, _data in frames]
        check("the SSE channel keeps streaming past its first ping",
              "ping" in names and "timeline" in names[names.index("ping"):] if "ping" in names
              else False, names)

        # A websocket names its filters when it opens and answers a CLOSE with 1000.
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=10)
        server.connections += 1
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(("GET /:/websockets/notifications?filters=log HTTP/1.1\r\n"
                      "Host: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        time.sleep(0.5)
        mask = os.urandom(4)
        payload = struct.pack("!H", 1000)
        sock.sendall(struct.pack("!BB", 0x88, 0x80 | len(payload)) + mask
                     + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        answer = b""
        deadline = time.time() + 5
        sock.settimeout(1)
        while time.time() < deadline and b"\x88\x02\x03\xe8" not in answer:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            answer += chunk
        sock.close()
        time.sleep(0.5)
        check("a websocket CLOSE is answered with status 1000",
              b"\x88\x02\x03\xe8" in answer, answer[-40:])
        lines = server.log_lines()
        check("the websocket logs its filters as it opens",
              any(re.match(r"^\[Thread-\d+ \(process_request_thread\)\] INFO - websocket event "
                           r"channel opened filters=\['log'\]$", line) for line in lines),
              [line for line in lines if "websocket" in line])
        check("the websocket logs the client's close",
              any(line.endswith("INFO - websocket closed by client") for line in lines),
              [line for line in lines if "websocket" in line])
    finally:
        server.stop()

    passed = sum(results)
    print("PASS %d/%d server-log checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
