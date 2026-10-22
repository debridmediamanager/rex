#!/usr/bin/env python3
"""Subtitle search, apply and automatic subtitles, against a claimed rex and local stubs.

What Plex Web sends and what a real PMS answers are recorded in
`fixtures/subtitles/plex_search_apply_s143.json` (plexlab, 1.43.4): the Search Subtitles request
and its Stream rows, the apply `PUT .../subtitles?key=/library/streams/{id}&...` answering 200
with `X-Plex-Activity`, the `subtitle.download` activity started and ended, the applied Stream
row, its bytes, its delete, and the PMS's own calls to Plex's provider. Here the provider is a
stub behind `REX_PLEXTV_URL` (its `streams/matches` and its download's 302), beside a stub
source serving two small real clips FFmpeg makes: a film with independent embedded text
tracks and voiced audio, and an episode with no text track whose audio is tone bursts on
its dialogue's timing, which rex reads with `REX_TRANSCODER`.

The candidates are made from each clip's own timing, as the corpus found them
(`fixtures/subtitles/sync_corpus.json.gz`): one in sync, one 2.4 s late, one timed for 25 fps
on a 23.976 release, one of another title, Windows-1252 bytes and UTF-8 mangled as
Windows-1252 (`â™ª` for `♪`). rex must keep the one in sync over the provider's first result,
put a late one right, and store UTF-8.

    python3 tests/test_subtitles.py
"""
import gzip
import http.server
import json
import math
import os
import random
import re
import shutil
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from server_command import server_command, test_ffmpeg
from startup_wait import poll_until
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
REFERENCE = json.load(open(os.path.join(FIXTURES, "subtitles", "plex_search_apply_s143.json")))
SHARED_TOKENS = json.load(open(os.path.join(FIXTURES, "plextv_access_tokens_shared_s1434.json")))["body"]
CACHE = json.load(open(os.path.join(FIXTURES, "feed", "provider_cache.json")))
LEGACY = json.load(open(os.path.join(FIXTURES, "feed", "legacy_scan_rows.json")))
ENTRIES = json.load(open(os.path.join(FIXTURES, "zurg", "entries.json")))["entries"]
DECISION = [tuple(pair) for pair in json.load(open(os.path.join(FIXTURES, "decision_streams_selected.json")))["request_query"]]
FFMPEG = test_ffmpeg()
FFPROBE = shutil.which("ffprobe") or (FFMPEG and os.path.join(os.path.dirname(FFMPEG), "ffprobe"))
MACHINE = "00000000000000000000000000000000000000e5"
TOKEN = "subtitles-server-token"
DURATION = 300
# The owner's plex.tv settings: subtitles always, Brazilian Portuguese.
PROFILE = ('<profile autoSelectAudio="1" defaultAudioAccessibility="0" defaultAudioLanguage="en" '
           'defaultAudioLanguages="" defaultSubtitleLanguage="pt-BR" defaultSubtitleLanguages="" '
           'autoSelectSubtitle="2" defaultSubtitleAccessibility="0" defaultSubtitleForced="0" '
           'watchedIndicator="1" mediaReviewsVisibility="0" mediaReviewsLanguages="" mediaPostsVisibility="1"/>')
FILM, SHOW = ENTRIES["movie"], ENTRIES["episode"]
FILM_STEM = FILM["file"][:-4]

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


# -- the clips and their subtitles -----------------------------------------------------------

def timeline(seed):
    """Dialogue timing: lines of 1-3.5 s with 1.5-5 s between them, about 40% of the runtime."""
    rng, at, out = random.Random(seed), 4.0, []
    while at < DURATION - 6:
        length = rng.uniform(1.0, 3.5)
        out.append((round(at, 3), round(at + length, 3)))
        at += length + rng.uniform(1.5, 5.0)
    return out


def stamp(seconds):
    ms = int(round(seconds * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def srt(cues, line):
    return "".join("%d\n%s --> %s\n%s\n\n" % (n + 1, stamp(a), stamp(b), line % (n + 1)) for n, (a, b) in enumerate(cues))


def read_cues(text):
    out = []
    for m in re.finditer(r"(\d+):(\d\d):(\d\d)[,.](\d{3}) --> (\d+):(\d\d):(\d\d)[,.](\d{3})", text):
        g = [int(x) for x in m.groups()]
        out.append((g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000, g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000))
    return out


def write_dialogue_pcm(path, cues):
    """Real decoder input with a 1 kHz tone on dialogue timing, silence between."""
    rate, tone = 16000, [int(12000 * math.sin(2 * math.pi * 1000 * n / 16000)) for n in range(16)]
    samples = bytearray(DURATION * rate * 2)
    for a, b in cues:
        for n in range(int(a * rate), min(int(b * rate), DURATION * rate)):
            struct.pack_into("<h", samples, 2 * n, tone[n % 16])
    open(path, "wb").write(samples)


def make_clips(work):
    film_cues, show_cues = timeline(11), timeline(23)
    # Exact FFmpeg-generated input from the late HTTP-seek JPEG failure. Keep
    # its limited-range frame at 290 s; regenerating it can change negotiation.
    film = os.path.join(work, "film.mkv")
    fixture = os.path.join(FIXTURES, "subtitles", "limited_range_late_frame.mkv.gz")
    with gzip.open(fixture, "rb") as source, open(film, "wb") as target:
        shutil.copyfileobj(source, target)
    pcm = os.path.join(work, "show.pcm")
    write_dialogue_pcm(pcm, show_cues)
    show = os.path.join(work, "show.mkv")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1:d=%d" % DURATION,
                    "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", pcm, "-t", str(DURATION),
                    "-map", "0:v", "-map", "1:a", "-c:v", "mpeg4", "-g", "10", "-c:a", "flac", show], check=True)
    return (film, film_cues), (show, show_cues)


def probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def os_hash(path):
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        head = handle.read(65536)
        handle.seek(max(0, size - 65536))
        tail = handle.read(65536)
    total = size
    for chunk in (head, tail):
        for (word,) in struct.iter_unpack("<Q", chunk[:len(chunk) // 8 * 8]):
            total = (total + word) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % total


# -- the stubs ---------------------------------------------------------------------------------

class Stub:
    """plex.tv and Plex's provider (`REX_PLEXTV_URL` sends `/<host><path>`), the provider's
    download target and the source's bytes, on one port, every request kept."""

    def __init__(self):
        self.seen = []
        self.files = {}       # /dav/<path> -> bytes of a clip
        self.subtitles = {}   # /files/<name> -> bytes of a subtitle
        self.matches = {}     # (file name, lang) -> provider rows
        self.port = free_port()
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", kind="application/json", extra=()):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                # FFmpeg can reuse a range connection unless the response announces
                # its closure. BaseHTTPRequestHandler honors the request's close
                # header, so advertise that decision instead of leaving a dead
                # HTTP/1.1 connection looking persistent after the first range.
                if self.close_connection:
                    self.send_header("Connection", "close")
                for name, value in extra:
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_GET(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                path, _, query = self.path.partition("?")
                stub.seen.append((self.command, path, urllib.parse.parse_qsl(query, keep_blank_values=True),
                                  {name.lower(): value for name, value in self.headers.items()}, self.path))
                if path.startswith("/dav/"):
                    body = stub.files.get(urllib.parse.unquote(path))
                    if body is None:
                        return self.reply(404, b"")
                    spec = self.headers.get("Range")
                    if spec and spec.startswith("bytes="):
                        first, _, last = spec[6:].partition("-")
                        first, last = int(first), min(int(last) if last else len(body) - 1, len(body) - 1)
                        return self.reply(206, body[first:last + 1], "video/x-matroska",
                                          [("Content-Range", "bytes %d-%d/%d" % (first, last, len(body))),
                                           ("Accept-Ranges", "bytes")])
                    return self.reply(200, body, "video/x-matroska", [("Accept-Ranges", "bytes")])
                if path == "/plex.tv/api/v2/server/access_tokens":
                    return self.reply(200, SHARED_TOKENS.encode(), "application/xml")
                if path == "/servers.plex.tv/api/v2/user":
                    body = '<?xml version="1.0" encoding="UTF-8"?>\n<user id="1" username="owner">%s</user>\n' % PROFILE
                    return self.reply(200, body.encode(), "application/xml")
                if path.startswith("/metadata.provider.plex.tv/library/streams/"):
                    # The provider's own refusals, measured 2026-10-20 with a claimed server's token:
                    # it is an X-Plex-Token (header or query); as X-Plex-Api-Token it is refused.
                    asked = dict(urllib.parse.parse_qsl(query))
                    token = self.headers.get("X-Plex-Token") or asked.get("X-Plex-Token")
                    if token != TOKEN:
                        message = "Invalid api token" if self.headers.get("X-Plex-Api-Token") or asked.get("X-Plex-Api-Token") else "You must provide a token!"
                        return self.reply(401, json.dumps({"Error": {"error": "Unauthorized", "message": message, "statusCode": 401}}).encode(),
                                          "application/json; charset=utf-8")
                if path == "/metadata.provider.plex.tv/library/streams/matches":
                    q = dict(urllib.parse.parse_qsl(query))
                    rows = stub.matches.get((q.get("filename"), q.get("lang")), [])
                    body = json.dumps({"MediaContainer": {"librarySectionID": "home", "librarySectionTitle": "Metadata",
                                                          "offset": 0, "totalSize": len(rows), "identifier": "tv.plex.provider.metadata",
                                                          "size": len(rows), "Stream": rows}}).encode()
                    return self.reply(200, body, "application/json; charset=utf-8")
                found = re.fullmatch(r"/metadata\.provider\.plex\.tv/library/streams/(\d+)", path)
                if found:
                    # The provider's 302 to the subtitle's own link.
                    target = "http://127.0.0.1:%d/files/%s.srt" % (stub.port, found.group(1))
                    return self.reply(302, b"Found. Redirecting to " + target.encode(), "text/plain; charset=utf-8",
                                      [("Location", target)])
                if path.startswith("/files/"):
                    body = stub.subtitles.get(path)
                    return self.reply(200 if body is not None else 404, body or b"", "application/x-subrip")
                if path.startswith("/metadata.provider.plex.tv/"):
                    return self.reply(200, b'{"MediaContainer": {"size": 0, "Metadata": []}}')
                body = b'<?xml version="1.0" encoding="UTF-8"?>\n<MediaContainer size="0"/>\n'
                self.reply(200, body, "application/xml")

            do_HEAD = do_POST = do_PUT = do_DELETE = do_GET

        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", self.port), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def subtitle_calls(self):
        """What reached Plex's subtitle search and its downloads."""
        return [(path, query, headers) for _, path, query, headers, _ in self.seen
                if "/library/streams/" in path or path.startswith("/files/")]


# -- the server --------------------------------------------------------------------------------

class Server:
    def __init__(self, work, stub, claimed=True, transcoder=True):
        self.port = free_port()
        env = dict(os.environ, REX_PLEXTV_URL=stub.base())
        for name in ("REX_STATE", "REX_TOKEN", "PLEX_TOKEN", "TMDB_KEY"):
            env.pop(name, None)
        env["REX_TRANSCODER"] = "off"
        if transcoder:
            env["REX_TRANSCODER"] = FFMPEG
            env["REX_TRANSCODE_DIR"] = os.path.join(work, "transcode")
        json.dump({"machineIdentifier": MACHINE, "friendlyName": "rex-subtitles", "sections": LEGACY["sections"],
                   "items": []}, open(os.path.join(work, "catalog.json"), "w"))
        json.dump(CACHE["plex"], open(os.path.join(work, "plex_cache.json"), "w"))
        flags = []
        if claimed:
            json.dump({"machine": MACHINE, "token": TOKEN, "owner": "owner", "email": "owner@example.com",
                       "feature_uuids": [], "account_id": 1, "view_state_cursor": None, "access_tokens": []},
                      open(os.path.join(work, "claim_state.json"), "w"))
        else:
            flags = ["--assert-claimed"]
        self.log = open(os.path.join(work, "server.log"), "ab")
        self.process = subprocess.Popen(
            server_command("--port", self.port, "--catalog", os.path.join(work, "catalog.json"), "--gdm", "off",
                           *flags, root=ROOT),
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        poll_until(lambda: self.request("/identity")[0] == 200, 40)
        self.request("/rex/sources/zurg", "PUT", {"url": stub.base() + "/dav", "mount": "/zurg"})

    def request(self, path, method="GET", body=None, headers=None, raw=False):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                         headers=dict({"Accept": "application/json", "X-Plex-Token": TOKEN,
                                                       "Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                if raw:
                    return response.status, dict(response.headers), payload
                return response.status, json.loads(payload) if payload[:1] == b"{" else payload
        except urllib.error.HTTPError as error:
            payload = error.read()
            return (error.code, dict(error.headers), payload) if raw else (error.code, None)
        except (urllib.error.URLError, ConnectionError):
            return (0, {}, b"") if raw else (0, None)

    def push(self, kind, path, clip, ids, content_hash="c" * 40, **placed):
        directory, release, name = path.split("/")
        item = {"id": path, "path": path, "file": "/zurg/" + path, "name": name, "release": directory + "/" + release,
                "size": os.path.getsize(clip), "hash": content_hash, "ids": ids, "kind": kind, "media_info": probe(clip)}
        item.update(placed)
        status, body = self.request("/rex/feed/zurg/items", "POST", {"items": [item]})
        status, listed = self.request("/rex/feed/zurg/items")
        return {row["id"]: row["ratingKey"] for row in (listed or {}).get("items", [])}.get(path)

    def detail(self, key):
        status, body = self.request("/library/metadata/%s?includeGuids=1" % key)
        return (((body or {}).get("MediaContainer") or {}).get("Metadata") or [{}])[0] if status == 200 else {}

    def streams(self, key):
        row = self.detail(key)
        return ((row.get("Media") or [{}])[0].get("Part") or [{}])[0].get("Stream") or []

    def analysis_state(self, key):
        status, body = self.request("/rex/analysis")
        return next((job for job in (body or {}).get("jobs", [])
                     if str(job.get("ratingKey")) == str(key)), {}) if status == 200 else {}

    def upload(self, key, title, text):
        before = {stream["id"] for stream in self.streams(key)}
        path = "/library/metadata/%s/subtitles?%s" % (key, urllib.parse.urlencode({"title": title, "format": "srt"}))
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=text.encode(), method="POST",
                                         headers={"X-Plex-Token": TOKEN, "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(request, timeout=60) as response:
            assert response.status == 200
        return next(stream for stream in self.streams(key) if stream["id"] not in before)

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.log.close()


class Events:
    """The activity frames off the eventsource channel, as the reference's were recorded."""

    def __init__(self, port):
        self.frames = []
        response = urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:%d/:/eventsource/notifications?filters=activity" % port,
            headers={"Accept": "text/event-stream", "X-Plex-Token": TOKEN}), timeout=3600)

        def read():
            for line in response:
                text = line.decode("utf-8", "replace").strip()
                if text.startswith("data:"):
                    try:
                        self.frames.append(json.loads(text.split(":", 1)[1]))
                    except ValueError:
                        pass
        threading.Thread(target=read, daemon=True).start()

    def activity(self, uuid):
        return [frame["ActivityNotification"] for frame in self.frames
                if isinstance(frame.get("ActivityNotification"), dict) and frame["ActivityNotification"].get("uuid") == uuid]


def provider_row(number, title, language, score):
    """A row as Plex's provider answers one (the fixture's outbound answer)."""
    return {"codec": "srt", "id": str(number), "key": "/library/streams/%d" % number, "language": "English" if language == "en" else "Portuguese",
            "languageCode": language, "providerTitle": "OpenSubtitles", "score": score, "title": title,
            "index": "0", "streamType": 3}


def main():
    if not FFMPEG or not FFPROBE:
        print("SKIP: no ffmpeg/ffprobe (set REX_TRANSCODER)")
        return
    work = tempfile.mkdtemp(prefix="rex-subtitles-")
    stub = Stub()
    (film, film_cues), (show, show_cues) = make_clips(work)
    film_path = "movies/%s/%s" % (FILM["release"], FILM["file"])
    show_path = "shows/%s/%s" % (SHOW["release"], SHOW["file"])
    stub.files["/dav/" + film_path] = open(film, "rb").read()
    stub.files["/dav/" + show_path] = open(show, "rb").read()
    # Independent late-seek transport regression: the declared video really has a
    # frame at 290 s. A closed HTTP/1.1 socket masquerading as persistent used to
    # drop FFmpeg's repeated Range request and return exit 0 with no image.
    late_frame = os.path.join(work, "http-late-frame.jpg")
    extracted = subprocess.run(
        [FFMPEG, "-v", "error", "-nostdin", "-n", "-rw_timeout", "15000000",
         "-ss", "290.000", "-i", stub.base() + "/dav/" + urllib.parse.quote(film_path, safe="/"),
         "-map", "0:0", "-an", "-sn", "-dn", "-frames:v", "1",
         "-vf", "scale=320:-2:out_range=full,setsar=1,format=yuvj420p",
         "-color_range", "pc", "-threads", "1", "-f", "image2", late_frame],
        capture_output=True, timeout=45)
    jpeg = open(late_frame, "rb").read() if os.path.isfile(late_frame) else b""
    check("real FFmpeg late HTTP seek returns a complete JPEG from the retained source",
          extracted.returncode == 0 and jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9"),
          (extracted.returncode, len(jpeg), extracted.stderr.decode(errors="replace")[-1000:]))


    # The film, in Brazilian Portuguese: the provider's first result is another title's timing
    # under this very release's name; its second is in sync, in Windows-1252. Plex would show
    # the first.
    other = timeline(97)
    stub.matches[(FILM["file"], "pt-BR")] = [provider_row(81001, FILM_STEM, "pt-BR", 9100),
                                             provider_row(81002, "Blame It on the Bellboy (1992)", "pt-BR", 1200)]
    stub.subtitles["/files/81001.srt"] = srt(other, "Outro filme %d").encode("cp1252")
    stub.subtitles["/files/81002.srt"] = srt(film_cues, "Não é possível %d").encode("cp1252")

    # The episode, in English: the provider's first is another title's, its second 2.4 s late and
    # mangled (`â™ª`).
    show_stem = SHOW["file"][:-4]
    stub.matches[(SHOW["file"], "en")] = [provider_row(82001, show_stem, "en", 8000),
                                          provider_row(82002, "American Dad! S05E07", "en", 1500)]
    stub.subtitles["/files/82001.srt"] = srt(other, "Another show %d").encode()
    late = [(a + 2.4, b + 2.4) for a, b in show_cues]
    stub.subtitles["/files/82002.srt"] = srt(late, "♪ Line %d ♪").encode("utf-8").decode("cp1252").encode("utf-8")

    os.makedirs(os.path.join(work, "claimed"))
    server = Server(os.path.join(work, "claimed"), stub)
    try:
        film_key = server.push("movie", film_path, film, {"tmdb": 31639, "imdb": "tt0103827"})
        show_key = server.push("episode", show_path, show, {"tmdb": 1433, "imdb": "tt0397306"}, season=5, episode=7)
        check("both clips are pushed", film_key and show_key, (film_key, show_key))
        # A listing, not the detail request: that one starts the job.
        status, listing = server.request("/library/sections/1/all?includeGuids=1")
        film_row = next((row for row in ((listing or {}).get("MediaContainer") or {}).get("Metadata") or []
                         if str(row.get("ratingKey")) == str(film_key)), {})
        check("the film is the title its ids name, its IMDb id among its Guids",
              {"id": "imdb://tt0103827"} in (film_row.get("Guid") or []), film_row.get("Guid"))
        poll_until(lambda: any(path == "/servers.plex.tv/api/v2/user" for _, path, *_ in stub.seen), 30)
        time.sleep(1.0)

        # -- automatic subtitles: found and kept before the playback decision -----------------
        events = Events(server.port)
        stub.seen.clear()
        before = server.streams(film_key)
        query = [(k, "/library/metadata/%s" % film_key if k == "path" else v) for k, v in DECISION]
        status, decided = server.request("/video/:/transcode/universal/decision?" + urllib.parse.urlencode(query))
        part = ((((decided or {}).get("MediaContainer") or {}).get("Metadata") or [{}])[0].get("Media") or [{}])[0].get("Part") or [{}]
        listed = [s for s in part[0].get("Stream") or [] if s.get("streamType") == 3]
        check("the detail request finds no Portuguese subtitle and starts the job",
              not any(s.get("languageCode") == "por" for s in before), before)
        check("the decision sent right after it waits for the job and lists the Portuguese subtitle, selected",
              status == 200 and any(s.get("languageCode") == "por" and s.get("selected") for s in listed),
              (status, listed))
        streams = server.streams(film_key)
        kept = [s for s in streams if s.get("streamType") == 3 and s.get("languageCode") == "por"]
        check("one Portuguese subtitle is kept on the film", len(kept) == 1, kept)
        kept = kept[0] if kept else {}
        reference = REFERENCE["client"]["appliedStreams"][0]
        check("it is the reference's stored Stream row, the owner's, selected by the owner's settings",
              list(kept) == list(reference) and kept.get("transient") == "1" and kept.get("userID") == "1"
              and kept.get("selected") is True, (list(kept), list(reference)))
        check("rex kept the one in sync, not the provider's first result (another title's timing)",
              kept.get("title") == "Blame It on the Bellboy (1992)" and kept.get("sourceKey") == "/library/streams/81002"
              and kept.get("providerTitle") == "OpenSubtitles" and kept.get("score") == "1200", kept)
        status, headers, body = server.request(kept.get("key", "/none"), raw=True)
        text = body.decode("utf-8", "replace")
        check("its bytes are UTF-8 from the provider's Windows-1252, served as text/html",
              status == 200 and headers.get("Content-Type") == "text/html" and "Não é possível 1" in text,
              (status, headers.get("Content-Type"), text[:120]))
        check("and in sync, so its timing is untouched", read_cues(text)[:5] == [(a, b) for a, b in film_cues[:5]],
              (read_cues(text)[:3], film_cues[:3]))
        asked = [query for path, query, _ in stub.subtitle_calls() if path.endswith("/streams/matches")]
        check("Plex's provider was asked in pt-BR, as the measured request", asked and dict(asked[0]).get("lang") == "pt-BR", asked)

        # -- the episode: no text track, measured against its audio ------------------------------
        stub.seen.clear()
        server.detail(show_key)
        poll_until(lambda: any(path.endswith("/streams/matches") for path, _, _ in stub.subtitle_calls()), 20)
        time.sleep(1.5)
        server.detail(show_key)
        time.sleep(0.5)
        pt_asks = [query for path, query, _ in stub.subtitle_calls() if path.endswith("/streams/matches")]
        check("a search that found nothing is not run again for a day",
              len(pt_asks) == 1 and dict(pt_asks[0]).get("lang") == "pt-BR", pt_asks)

        stub.seen.clear()
        search = "/library/metadata/%s/subtitles?language=en" % show_key
        status, first = server.request(search)
        container = (first or {}).get("MediaContainer") or {}
        rows = container.get("Stream") or []
        expected_keys = list(REFERENCE["client"]["search"]["body"]["MediaContainer"])
        check("the search answers the reference's container", status == 200 and list(container) == expected_keys
              and container.get("size") == 2, (status, list(container), container.get("size")))
        shape = list(REFERENCE["client"]["search"]["body"]["MediaContainer"]["Stream"][0])
        check("every row is the reference's search row", rows and all(list(row) == shape for row in rows),
              [list(row) for row in rows])
        provider = [row for row in rows if row.get("providerTitle") == "OpenSubtitles"]
        check("Plex's rows keep the provider's id as sourceKey and its score as a string",
              sorted(row.get("sourceKey") for row in provider) == ["/library/streams/82001", "/library/streams/82002"]
              and all(isinstance(row.get("score"), str) for row in provider), provider)
        calls = [(path, query, headers) for path, query, headers in stub.subtitle_calls() if path.endswith("/streams/matches")]
        measured = REFERENCE["outbound"][0]
        if calls:
            path, query, headers = calls[0]
            names = [name for name, _ in query]
            check("the provider is asked as the reference asks it, without plexHash",
                  names == [name for name, _ in measured["query"] if name != "plexHash"], names)
            check("with the release's own OpenSubtitles hash, its guid and its file name",
                  dict(query).get("openSubtitlesHash") == os_hash(show) and dict(query).get("filename") == SHOW["file"]
                  and dict(query).get("type") == "4" and dict(query).get("guid", "").startswith("plex://"), query)
            check("with the claim's server token and the reference's headers",
                  headers.get("x-plex-token") == TOKEN and "x-plex-api-token" not in headers and headers.get("x-plex-client-identifier") == MACHINE
                  and headers.get("x-plex-provides") == "server" and headers.get("user-agent", "").startswith("PlexMediaServer/"),
                  headers)
        else:
            check("the provider was asked", False, stub.subtitle_calls())
        status, again = server.request(search)
        ids = {row["id"] for row in rows}
        again_ids = {row["id"] for row in ((again or {}).get("MediaContainer") or {}).get("Stream") or []}
        check("a search again mints new local ids", again_ids and not (ids & again_ids), (ids, again_ids))

        late_row = next((row for row in ((again or {}).get("MediaContainer") or {}).get("Stream") or []
                         if row.get("sourceKey") == "/library/streams/82002"), {})
        media_id = (server.detail(show_key).get("Media") or [{}])[0].get("id")
        apply = REFERENCE["client"]["apply"]["request"]
        query = [(name, late_row.get("key") if name == "key" else str(media_id) if name == "mediaItemID" else value)
                 for name, value in apply["query"]]
        status, headers, body = server.request("/library/metadata/%s/subtitles?%s" % (show_key, urllib.parse.urlencode(query)),
                                               "PUT", raw=True)
        uuid = headers.get("X-Plex-Activity")
        check("the apply answers 200, empty, with its activity in X-Plex-Activity",
              status == 200 and body == b"" and uuid and headers.get("Access-Control-Expose-Headers") == "X-Plex-Activity",
              (status, headers, body[:80]))
        poll_until(lambda: [note["event"] for note in events.activity(uuid)] == ["started", "ended"], 60)
        notes = events.activity(uuid)
        recorded = REFERENCE["client"]["events"]
        check("its subtitle.download activity starts and ends, as the reference's",
              [(n["event"], n["Activity"]["type"], n["Activity"]["title"], n["Activity"]["progress"]) for n in notes]
              == [(r["ActivityNotification"]["event"], r["ActivityNotification"]["Activity"]["type"],
                   r["ActivityNotification"]["Activity"]["title"], r["ActivityNotification"]["Activity"]["progress"])
                  for r in recorded]
              and all(list(n["Activity"]) == list(recorded[0]["ActivityNotification"]["Activity"]) for n in notes),
              notes)
        streams = server.streams(show_key)
        applied = [s for s in streams if s.get("sourceKey") == "/library/streams/82002"]
        applied = applied[0] if applied else {}
        check("the applied subtitle is the reference's applied row, selected, the owner's",
              list(applied) == list(reference) and applied.get("selected") is True and applied.get("transient") == "1"
              and applied.get("userID") == "1" and applied.get("languageCode") == "eng"
              and applied.get("extendedDisplayTitle") == "American Dad! S05E07 (English SRT OpenSubtitles)",
              applied)
        check("and the only subtitle selected",
              [s.get("id") for s in streams if s.get("streamType") == 3 and s.get("selected")] == [applied.get("id")],
              [(s.get("id"), s.get("selected")) for s in streams if s.get("streamType") == 3])
        status, headers, body = server.request(applied.get("key", "/none"), raw=True)
        text = body.decode("utf-8", "replace")
        got = read_cues(text)
        off = [abs(a - x) for (a, _), (x, _) in zip(got, show_cues)]
        check("its bytes are UTF-8 with the mangled notes put back", status == 200 and "♪ Line 1 ♪" in text
              and "â\u0099" not in text and "â™" not in text, text[:120])
        check("and 2.4 s earlier: measured against the audio and moved onto it",
              len(got) == len(show_cues) and off and max(off) <= 0.15, (max(off) if off else None, got[:3], show_cues[:3]))
        status, _ = server.request(applied.get("key", "/none"), "DELETE")
        check("the applied subtitle deletes as the reference's did",
              status == 200 and not any(s.get("id") == applied.get("id") for s in server.streams(show_key)), status)
        status, _ = server.request("/library/metadata/%s/subtitles?key=/library/streams/999999&codec=srt&language=eng"
                                   % show_key, "PUT")
        check("an apply of a stream no search answered is refused", status == 400, status)

        # -- a detail request reads no audio; the play checks its pick and puts it right --------
        # Six 90 s windows are a few hundred MB of a real file: only a play reads them (Ben,
        # 2026-10-20). The best-named candidate here is 2.4 s late; the other is in sync.
        next_file = SHOW["file"].replace("S05E07", "S05E08")
        next_path = "shows/%s/%s" % (SHOW["release"], next_file)
        stub.files["/dav/" + next_path] = open(show, "rb").read()
        stub.matches[(next_file, "pt-BR")] = [provider_row(83001, next_file[:-4], "pt-BR", 900),
                                              provider_row(83002, "American Dad! S05E08", "pt-BR", 100)]
        stub.subtitles["/files/83001.srt"] = srt(late, "Linha atrasada %d").encode()
        stub.subtitles["/files/83002.srt"] = srt(show_cues, "Linha certa %d").encode()
        next_key = server.push("episode", next_path, show, {"tmdb": 1433, "imdb": "tt0397306"}, season=5, episode=8)
        stub.seen.clear()
        server.detail(next_key)
        kept_now = lambda: [s for s in server.streams(next_key) if s.get("streamType") == 3 and s.get("languageCode") == "por"]
        poll_until(lambda: kept_now(), 30)
        first_kept = (kept_now() or [{}])[0]
        audio_reads = [headers.get("range") for method, path, _, headers, _ in stub.seen
                       if urllib.parse.unquote(path) == "/dav/" + next_path and headers.get("range", "").endswith("-")]
        check("a detail request keeps the best-named pick without reading the audio",
              first_kept.get("sourceKey") == "/library/streams/83001" and audio_reads == [], (first_kept, audio_reads))
        stub.seen.clear()
        server.request("/playQueues?type=video&uri=%s&key=%s" % (
            urllib.parse.quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (MACHINE, next_key)),
            urllib.parse.quote("/library/metadata/%s" % next_key)), "POST")
        poll_until(lambda: (kept_now() or [{}])[0].get("sourceKey") == "/library/streams/83002", 60)
        checked = kept_now()
        audio_reads = [path for method, path, _, headers, _ in stub.seen
                       if urllib.parse.unquote(path) == "/dav/" + next_path and headers.get("range", "").endswith("-")]
        status, headers, body = server.request((checked or [{}])[0].get("key", "/none"), raw=True)
        got = read_cues(body.decode("utf-8", "replace"))
        off = [abs(a - x) for (a, _), (x, _) in zip(got, show_cues)]
        check("the play reads the audio, then keeps the one in sync in the same stream",
              len(checked) == 1 and checked[0].get("id") == first_kept.get("id") and audio_reads
              and checked[0].get("sourceKey") == "/library/streams/83002" and off and max(off) <= 0.15,
              (checked, len(audio_reads), max(off) if off else None))
        # -- official per-read autoAdjustSubtitle, not a destructive edit of the upload ------
        uploaded_text = srt([(a + 2.4, b + 2.4) for a, b in film_cues], "Uploaded late line %d")
        uploaded = server.upload(film_key, "late.srt", uploaded_text)
        stream_path = uploaded["key"]
        selections = [(stream["id"], stream.get("selected")) for stream in server.streams(film_key)]
        status, _, original = server.request(stream_path + "?autoAdjustSubtitle=0", raw=True)
        check("disabled official adjustment returns the exact uploaded text", status == 200 and original == uploaded_text.encode())
        status, _, corrected = server.request(stream_path + "?autoAdjustSubtitle=1", raw=True)
        corrected_cues = read_cues(corrected.decode())
        errors = [abs(a - b) for (a, _), (b, _) in zip(corrected_cues, film_cues)]
        check("official adjustment measures and corrects the late upload",
              status == 200 and len(corrected_cues) == len(film_cues) and errors and max(errors) < .15,
              (status, errors[:4]))
        source_reads = len([path for _, path, *_ in stub.seen if path.startswith("/dav/")])
        status, _, repeated = server.request(stream_path + "?autoAdjustSubtitle=1", raw=True)
        check("repeat adjustment is stable and reuses cached original/reference",
              status == 200 and repeated == corrected
              and source_reads == len([path for _, path, *_ in stub.seen if path.startswith("/dav/")]))
        status, _, disabled = server.request(stream_path + "?autoAdjustSubtitle=0", raw=True)
        check("adjustment does not overwrite original or account selections",
              status == 200 and disabled == original
              and selections == [(stream["id"], stream.get("selected")) for stream in server.streams(film_key)])
        media = server.detail(film_key)["Media"][0]
        check("text reference is not advertised as voice analysis", media.get("hasVoiceActivity") is False, media.get("hasVoiceActivity"))
        check("internal subtitle text and analysis cache never appear in metadata",
              "_subtitleSync" not in json.dumps(media) and "_content" not in json.dumps(media))

        uncertain_text = srt(timeline(97), "Unrelated line %d")
        uncertain = server.upload(film_key, "uncertain.srt", uncertain_text)
        status, _, unchanged = server.request(uncertain["key"] + "?autoAdjustSubtitle=1", raw=True)
        check("ambiguous/unrelated subtitle is not silently retimed", status == 200 and unchanged == uncertain_text.encode())

        embedded = next(stream for stream in server.streams(film_key)
                        if stream.get("streamType") == 3 and stream.get("index") == 3)
        embedded_path = "/library/streams/%s.srt" % embedded["id"]
        status, _, embedded_original = server.request(embedded_path + "?autoAdjustSubtitle=0", raw=True)
        check("supported embedded text is readable without adjustment",
              status == 200 and abs(read_cues(embedded_original.decode())[0][0] - film_cues[0][0] - 2.4) < .01)
        status, _, embedded_fixed = server.request(embedded_path + "?autoAdjustSubtitle=1", raw=True)
        embedded_cues = read_cues(embedded_fixed.decode())
        errors = [abs(a - b) for (a, _), (b, _) in zip(embedded_cues, film_cues)]
        check("supported embedded text is measured against an independent track",
              status == 200 and len(embedded_cues) == len(film_cues) and errors and max(errors) < .15, errors[:4])
        # A prior successful text reference coexists with the later real audio analysis.
        # A new text-based adjustment must not overwrite that audio reference.
        before_hybrid_selections = [(stream["id"], stream.get("selected")) for stream in server.streams(film_key)]
        status, _ = server.request("/library/metadata/%s/analyze" % film_key, "PUT")
        check("Analyze accepts a movie with independent embedded text and actual audio", status == 200)
        poll_until(lambda: server.analysis_state(film_key).get("state") in ("completed", "failed", "stale"), 120)
        hybrid = server.detail(film_key)["Media"][0]
        hybrid_upload = next(stream for stream in hybrid["Part"][0]["Stream"] if stream["id"] == uploaded["id"])
        check("hybrid Analyze establishes the actual client voice-and-stream gate before adjustment",
              hybrid.get("hasVoiceActivity") is True and hybrid_upload.get("canAutoSync") is True,
              server.analysis_state(film_key))
        check("hybrid voice analysis leaves selected account tracks unchanged",
              before_hybrid_selections == [(stream["id"], stream.get("selected")) for stream in server.streams(film_key)])
        second_text = server.upload(film_key, "late-after-analysis.srt", uploaded_text)
        before_text_adjustment = [(stream["id"], stream.get("selected")) for stream in server.streams(film_key)]
        status, _, after_voice = server.request(second_text["key"] + "?autoAdjustSubtitle=1", raw=True)
        check("text-reference adjustment preserves previously analyzed voice metadata",
              status == 200 and after_voice == corrected
              and server.detail(film_key)["Media"][0].get("hasVoiceActivity") is True
              and before_text_adjustment == [(stream["id"], stream.get("selected")) for stream in server.streams(film_key)],
              server.analysis_state(film_key))

        audio_upload = server.upload(show_key, "audio-late.srt", srt(late, "Audio late line %d"))
        check("voice analysis is not advertised before actual Analyze",
              server.detail(show_key)["Media"][0].get("hasVoiceActivity") is False)
        before_analysis_selections = [(stream["id"], stream.get("selected")) for stream in server.streams(show_key)]
        status, _ = server.request("/library/metadata/%s/analyze" % show_key, "PUT")
        check("manual Analyze starts actual voice processing", status == 200, status)
        poll_until(lambda: server.analysis_state(show_key).get("state") in ("completed", "failed", "stale"), 120)
        analyzed = server.detail(show_key)["Media"][0]
        analyzed_upload = next(stream for stream in analyzed["Part"][0]["Stream"] if stream["id"] == audio_upload["id"])
        check("Analyze enables the actual shipped-client voice-and-stream gate before first adjust",
              analyzed.get("hasVoiceActivity") is True and analyzed_upload.get("canAutoSync") is True,
              server.analysis_state(show_key))
        check("voice analysis leaves selected account tracks unchanged",
              before_analysis_selections == [(stream["id"], stream.get("selected")) for stream in server.streams(show_key)])
        before_adjustment_reads = len([path for _, path, *_ in stub.seen if path.startswith("/dav/")])
        status, _, audio_fixed = server.request(audio_upload["key"] + "?autoAdjustSubtitle=1", raw=True)
        audio_cues = read_cues(audio_fixed.decode())
        errors = [abs(a - b) for (a, _), (b, _) in zip(audio_cues, show_cues)]
        check("official adjustment uses actual FFmpeg audio-reference measurements",
              status == 200 and len(audio_cues) == len(show_cues) and errors and max(errors) < .15, errors[:4])
        check("first official adjustment reuses the precomputed audio reference",
              before_adjustment_reads == len([path for _, path, *_ in stub.seen if path.startswith("/dav/")]))
        check("successful measured audio reference establishes voice-analysis metadata",
              server.detail(show_key)["Media"][0].get("hasVoiceActivity") is True)
        for path in (stream_path + "?autoAdjustSubtitle=1", "/library/metadata/%s" % film_key):
            status, _, denied = server.request(path, headers={"X-Plex-Token": "not-an-authorized-account"}, raw=True)
            check("unauthorized token gains neither subtitle nor analysis metadata: " + path,
                  status in (401, 403) and b"Uploaded late line" not in denied and b"hasVoiceActivity" not in denied,
                  (status, denied[:100]))
        # This measured access-list fixture is a real Movies-only account, not an unknown
        # token. It cannot see the episode even after the owner cached its audio analysis.
        for path in (audio_upload["key"] + "?autoAdjustSubtitle=1", "/library/metadata/%s" % show_key):
            status, _, denied = server.request(path, headers={"X-Plex-Token": "SHARED-TOKEN"}, raw=True)
            check("recognized restricted account cannot read episode subtitles or analysis: " + path,
                  status in (403, 404) and b"Audio late line" not in denied and b"hasVoiceActivity" not in denied,
                  (status, denied[:100]))

        # A source-supplied new content version must invalidate references even when every
        # transport attribute remains the same: URL, byte size, duration and stream indices.
        replaced_key = server.push("episode", show_path, show, {"tmdb": 1433, "imdb": "tt0397306"},
                                   content_hash="d" * 40, season=5, episode=7)
        check("same-path source version remains the same library item", str(replaced_key) == str(show_key))
        check("changed owning Media content hash invalidates prior voice analysis",
              server.detail(show_key)["Media"][0].get("hasVoiceActivity") is False)
        before_replacement_reads = len([path for _, path, *_ in stub.seen if path.startswith("/dav/")])
        status, _, replaced_adjusted = server.request(audio_upload["key"] + "?autoAdjustSubtitle=1", raw=True)
        check("changed content hash invalidates adjusted cache and forces fresh source measurement",
              status == 200 and replaced_adjusted == audio_fixed
              and len([path for _, path, *_ in stub.seen if path.startswith("/dav/")]) > before_replacement_reads)
    finally:
        server.stop()
        # What the meter read of each candidate, for whoever reads a failure.
        for line in open(os.path.join(work, "claimed", "server.log"), errors="replace"):
            if "SUBTITLE" in line:
                print("   ", line.rstrip()[-220:])

    # -- unclaimed: Plex's provider is never asked ---------------------------------------------------
    directory = os.path.join(work, "unclaimed")
    os.makedirs(directory)
    server = Server(directory, stub, claimed=False, transcoder=False)
    try:
        key = server.push("movie", film_path, film, {"tmdb": 31639, "imdb": "tt0103827"})
        stub.seen.clear()
        server.detail(key)
        server.request("/playQueues?type=video&uri=%s&key=%s" % (
            urllib.parse.quote("server://%s/com.plexapp.plugins.library/library/metadata/%s" % (MACHINE, key)),
            urllib.parse.quote("/library/metadata/%s" % key)), "POST")
        status, found = server.request("/library/metadata/%s/subtitles?language=pt-BR" % key)
        time.sleep(1.0)
        calls = [path for path, _, _ in stub.subtitle_calls()]
        check("unclaimed: the search answers its empty container and nothing is asked",
              status == 200 and list((found or {}).get("MediaContainer") or {}) == ["size", "identifier", "mediaTagPrefix",
                                                                                    "mediaTagVersion"]
              and calls == [], (status, found, calls))
    finally:
        server.stop()
    stub.httpd.shutdown()
    failed = results.count(False)
    if failed:
        print("Failed subtitle test media and server logs retained at " + work)
    else:
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
