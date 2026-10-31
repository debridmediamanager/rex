"""A music library pushed to rex as plexav's PMS holds it, and the replay of plexav's clients against it.

plexav (`probes/record_music_fixtures.py`) is a real PMS 1.43.4 whose one music library is four albums
of synthetic 45 s tones tagged as Radiohead's In Rainbows (FLAC) and OK Computer, Nine Inch Nails'
The Slip and Daft Punk's Discovery (MP3), made by its `make-music.sh`. This rig makes the same files
with the same FFmpeg commands in a temporary directory (no Plex bytes are checked in), serves them from
a plain Range server registered as a source, answers Plex's agent from what plexav's PMS asked it
(`fixtures/music/agent.json`) and pushes the 46 tracks placed as their source would place them: each
album by its MusicBrainz release id, each track by its number. Section 1 is the music library, as on
plexav, so the recorded paths name it as they are.

`replay` reissues a recorded client request with plexav's keys swapped for rex's (by title: both
came from Plex's agent) and compares the answers as `harness/scripts/replay.py` does: element
structure, attribute presence and values, the volatile fields left out.
"""
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

from server_command import server_command
from startup_wait import poll_until
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures", "music")
AGENT = json.load(open(os.path.join(FIXTURES, "agent.json")))["answers"]
MACHINE = "00000000000000000000000000000000000000a1"
REFERENCE_MACHINE = "8b9a5a573725219f3a79001e70fcd435b9600995"
TOKEN = "fake"

# plexav's make-music.sh: artist, album, year, extension, genre tag, tracks. The release ids are
# the ones plexav's PMS matched each album by (its outbound capture).
ALBUMS = [
    ("Nine Inch Nails", "The Slip", 2008, "mp3", "Industrial", "12b57d46-a192-499e-a91f-7da66790a1c1",
     ["999,999", "1,000,000", "Letting You", "Discipline", "Echoplex", "Head Down", "Lights in the Sky",
      "Corona Radiata", "The Four of Us Are Dying", "Demon Seed"]),
    ("Radiohead", "In Rainbows", 2007, "flac", "Alternative", "3b408cb5-7d51-4188-b07c-fabcf308cda3",
     ["15 Step", "Bodysnatchers", "Nude", "Weird Fishes/Arpeggi", "All I Need", "Faust Arp", "Reckoner",
      "House of Cards", "Jigsaw Falling into Place", "Videotape"]),
    ("Radiohead", "OK Computer", 1997, "mp3", "Alternative", "425d8db9-88a3-31e2-bd23-b1a967626dd5",
     ["Airbag", "Paranoid Android", "Subterranean Homesick Alien", "Exit Music (For a Film)", "Let Down",
      "Karma Police", "Fitter Happier", "Electioneering", "Climbing Up the Walls", "No Surprises", "Lucky",
      "The Tourist"]),
    ("Daft Punk", "Discovery", 2001, "mp3", "Electronic", "098060d4-ba3c-469d-88b0-08642261c7ab",
     ["One More Time", "Aerodynamic", "Digital Love", "Harder, Better, Faster, Stronger", "Crescendolls",
      "Nightvision", "Superheroes", "High Life", "Something About Us", "Voyager", "Veridis Quo",
      "Short Circuit", "Face to Face", "Too Long"]),
]

# Differ per server by construction (replay.py's), and what rex does not have by design: no
# sonic or loudness analysis (`requiredBandwidths`, `deepAnalysisVersion`), no artwork colours
# (`UltraBlurColors`), its own session ids and media ids.
VOLATILE = {
    "machineIdentifier", "ratingKey", "parentRatingKey", "grandparentRatingKey", "key", "parentKey",
    "grandparentKey", "guid", "parentGuid", "grandparentGuid", "addedAt", "updatedAt", "createdAt",
    "scannedAt", "lastViewedAt", "thumb", "art", "parentThumb", "grandparentThumb", "grandparentArt",
    "banner", "theme", "token", "accessToken", "sessionKey", "uuid", "librarySectionUUID",
    "contentChangedAt", "refreshing", "hubKey", "id", "file", "duration", "bitrate", "size",
    "totalFileSize", "mediaTagVersion", "resourceSession", "playQueueID", "playQueueItemID",
    "playQueueSelectedItemID", "playQueueLastAddedItemID", "playQueueSelectedMetadataItemID",
    "playQueueSourceURI", "url", "filter", "historyKey", "viewedAt", "deviceID", "augmentationKey",
}
UNANALYSED = {"UltraBlurColors", "requiredBandwidths", "deepAnalysisVersion"}

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:900]))
    return ok


def tool(name):
    return shutil.which(name) or (os.environ.get("REX_TRANSCODER") if name == "ffmpeg" and os.environ.get("REX_TRANSCODER", "off") != "off" else None)


def make_library(root):
    """plexav's files, made as its make-music.sh makes them; answers each track's push."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    items = []
    for artist, album, year, ext, genre, mbid, titles in ALBUMS:
        folder = os.path.join(root, "music", artist, "%s (%d)" % (album, year))
        os.makedirs(folder, exist_ok=True)
        for number, title in enumerate(titles, 1):
            name = "%02d - %s.%s" % (number, title.replace("/", "-"), ext)
            path = os.path.join(folder, name)
            codec = ["-c:a", "libmp3lame", "-b:a", "192k"] if ext == "mp3" else ["-c:a", "flac"]
            subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "sine=frequency=%d:duration=45" % (220 + number * 40), "-ac", "2",
                            "-metadata", "title=" + title, "-metadata", "artist=" + artist,
                            "-metadata", "album_artist=" + artist, "-metadata", "album=" + album,
                            "-metadata", "date=%d" % year, "-metadata", "track=%d/%d" % (number, len(titles)),
                            "-metadata", "genre=" + genre] + codec + [path], check=True)
            probe = json.loads(subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json", "-show_format",
                                               "-show_streams", path], check=True, capture_output=True).stdout)
            relative = "/".join(["music", artist, "%s (%d)" % (album, year), name])
            items.append({"id": relative, "path": relative, "kind": "track", "ids": {"mbid": mbid},
                          "disc": 1, "track": number, "media_info": probe,
                          "release": "music/%s/%s (%d)" % (artist, album, year)})
    return items


def range_server(root):
    """A plain byte server over `root`: GET with Range, nothing else (a source, docs/feed-api.md)."""
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        reads = []

        def log_message(self, *args):
            pass

        def do_GET(self):
            path = os.path.join(root, urllib.parse.unquote(urllib.parse.urlparse(self.path).path).lstrip("/"))
            Handler.reads.append((self.path, self.headers.get("Range")))
            if not os.path.isfile(path):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = open(path, "rb").read()
            status, first, last = 200, 0, len(data) - 1
            spec = self.headers.get("Range") or ""
            match = re.match(r"bytes=(\d*)-(\d*)$", spec)
            if match:
                status = 206
                first = int(match.group(1) or 0)
                last = min(int(match.group(2)) if match.group(2) else len(data) - 1, len(data) - 1)
            body = data[first:last + 1]
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (first, last, len(data)))
            self.end_headers()
            self.wfile.write(body)
    port = free_port()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port, Handler


def agent_server(answers=AGENT):
    """Plex's agent as plexav's PMS heard it; plex.tv and anything unrecorded answer 404."""
    asked = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            prefix = "/metadata.provider.plex.tv"
            path = url.path[len(prefix):] if url.path.startswith(prefix + "/") else None
            query = sorted(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
            asked.append((path, query))
            found = next((a["response"] for a in answers if path == a["path"]
                          and sorted(tuple(pair) for pair in a["query"]) == query), None)
            raw = json.dumps(found).encode() if found is not None else b""
            self.send_response(200 if found is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_POST = do_PUT = do_GET
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % server.server_port, asked


class Rex:
    """A claimed (`--assert-claimed`) rex whose section 1 is a music library."""

    def __init__(self, work, bytes_port, agent_url, extra_sections=()):
        self.port = free_port()
        self.work = work
        catalog = {"machineIdentifier": MACHINE, "friendlyName": "rex-music",
                   "sections": [{"key": "1", "type": "artist", "title": "Music", "agent": "tv.plex.agents.music",
                                 "scanner": "Plex Music", "language": "en-US",
                                 "uuid": "0000000000000000000000000000000000000a01",
                                 "Location": [{"id": 1, "path": "/data/music"}]}] + list(extra_sections),
                   "items": []}
        path = os.path.join(work, "catalog.json")
        json.dump(catalog, open(path, "w"))
        json.dump({"machine": MACHINE, "token": TOKEN}, open(os.path.join(work, "claim_state.json"), "w"))
        env = dict(os.environ, REX_PLEXTV_URL=agent_url)
        for name in ("REX_STATE", "REX_TOKEN", "PLEX_TOKEN"):
            env.pop(name, None)
        self.log = open(os.path.join(work, "server.log"), "ab")
        self.process = subprocess.Popen(
            server_command("--port", self.port, "--catalog", path, "--assert-claimed", "--gdm", "off", root=ROOT),
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)

        def ready():
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/identity" % self.port, timeout=2)
                return True
            except Exception:
                return False
        poll_until(ready, 40)
        status, _ = self.request("/rex/sources/plexav", "PUT",
                                 {"url": "http://127.0.0.1:%d" % bytes_port, "mount": "/data"},
                                 {"Content-Type": "application/json"})
        assert status == 200, status

    def request(self, path, method="GET", body=None, headers=None, raw=False):
        data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
                                         headers=dict({"Accept": "application/json", "X-Plex-Token": TOKEN},
                                                      **(headers or {})))
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                out = response.read()
                if raw:
                    return response.status, dict(response.headers), out
                return response.status, json.loads(out) if out[:1] == b"{" else out
        except urllib.error.HTTPError as error:
            out = error.read()
            if raw:
                return error.code, dict(error.headers), out
            return error.code, json.loads(out) if out[:1] == b"{" else out

    def push(self, items):
        return self.request("/rex/feed/plexav/items", "POST", {"items": items}, {"Content-Type": "application/json"})

    def rows(self, path):
        status, body = self.request(path)
        return ((body or {}).get("MediaContainer") or {}).get("Metadata") or [] if status == 200 else []

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.log.close()


def load(name):
    return [json.loads(line) for line in open(os.path.join(FIXTURES, name + ".jsonl"))]


def reference_keys():
    """plexav's keys by (type, title, parent title): its listings, as the fixtures recorded them."""
    keys = {}
    for record in load("web-music-views"):
        if record["path"] != "/library/sections/1/all":
            continue
        for row in json.loads(record["res_body"]).get("MediaContainer", {}).get("Metadata") or []:
            keys[row["ratingKey"]] = (row["type"], row["title"], row.get("parentTitle"))
    return keys


def key_map(rex):
    """plexav's ratingKey -> rex's, and part id -> part id, by title."""
    ours = {}
    for kind in (8, 9, 10):
        for row in rex.rows("/library/sections/1/all?type=%d" % kind):
            ours[(row["type"], row["title"], row.get("parentTitle"))] = row
    mapping, parts = {}, {}
    for key, signature in reference_keys().items():
        row = ours.get(signature)
        if row:
            mapping[key] = row["ratingKey"]
    for record in load("web-music-views"):
        if record["path"] == "/library/sections/1/all" and ("type", "10") in [tuple(q) for q in record["query"]]:
            for row in json.loads(record["res_body"])["MediaContainer"]["Metadata"]:
                if row["ratingKey"] in mapping:
                    theirs = row["Media"][0]["Part"][0]["id"]
                    mine = ours[(row["type"], row["title"], row.get("parentTitle"))]["Media"][0]["Part"][0]["id"]
                    parts[str(theirs)] = str(mine)
    return mapping, parts


def rewrite(text, mapping, parts=None, extra=None):
    """A recorded path or value with plexav's keys swapped for rex's."""
    if text is None:
        return text
    text = text.replace(REFERENCE_MACHINE, MACHINE)
    def metadata(match):
        keys = ",".join(mapping.get(key, key) for key in match.group(2).split(","))
        return match.group(1) + keys
    text = re.sub(r"(/library/metadata/)([\d,]+)", metadata, text)
    text = re.sub(r"(%2Flibrary%2Fmetadata%2F)(\d+)", lambda m: m.group(1) + mapping.get(m.group(2), m.group(2)), text)
    if parts:
        text = re.sub(r"(/library/parts/)(\d+)/\d+", lambda m: "%s%s/1" % (m.group(1), parts.get(m.group(2), m.group(2))), text)
    for name, value in (extra or {}).items():
        text = text.replace(name, value)
    return text


def query_of(record, mapping, extra=None):
    out = []
    for name, value in record.get("query") or []:
        if name == "X-Plex-Token":
            continue
        if name in ("ratingKey", "metadataItemID", "artist.id", "key", "id", "id!") and value != "-1":
            value = ",".join(mapping.get(part, part) for part in value.split(","))
        out.append((name, rewrite(value, mapping, extra=extra)))
    return out


def send(rex, record, mapping, parts=None, extra=None, path=None):
    path = path or rewrite(record["path"], mapping, parts, extra)
    query = query_of(record, mapping, extra)
    headers = {}
    for name, value in record.get("req_headers") or []:
        if name.lower() in ("accept", "x-plex-product", "x-plex-client-identifier", "x-plex-platform",
                            "x-plex-version", "x-plex-device", "x-plex-device-name", "x-plex-session-id",
                            "x-plex-language", "x-plex-container-start", "x-plex-container-size",
                            "x-plex-provides", "content-type", "range"):
            headers[name] = value
    url = path + ("?" + urllib.parse.urlencode(query) if query else "")
    return rex.request(url, record["method"], record.get("req_body").encode() if record.get("req_body") else None,
                       headers, raw=True)


def tree(body, content_type):
    nodes = defaultdict(list)

    def walk(node, path):
        if isinstance(node, dict):
            nodes[path].append({k: str(v) for k, v in node.items() if not isinstance(v, (dict, list))})
            for name, value in node.items():
                if isinstance(value, (dict, list)):
                    walk(value, "%s/%s" % (path, name))
        elif isinstance(node, list):
            for item in node:
                walk(item, path)

    def walk_xml(element, path):
        here = "%s/%s" % (path, element.tag)
        nodes[here].append(dict(element.attrib))
        for child in element:
            walk_xml(child, here)
    text = (body or b"").decode("utf-8", "replace").strip() if isinstance(body, bytes) else (body or "").strip()
    if not text:
        return nodes
    if text.startswith("{"):
        walk(json.loads(text), "")
    elif text.startswith("<"):
        import xml.etree.ElementTree as ET
        walk_xml(ET.fromstring(text), "")
    return nodes


def compare(reference, ours, ignore=(), values=True):
    """The differences replay.py reports: missing elements, element counts, missing attributes and
    differing values, minus volatile fields and what `ignore` names (an element or attribute)."""
    skip = set(ignore) | UNANALYSED
    a, b = tree(reference, ""), tree(ours, "")
    out = []
    for path, ref_nodes in a.items():
        if any(part in skip for part in path.split("/")):
            continue
        got = b.get(path)
        if got is None:
            out.append("missing %s" % path)
            continue
        if len(ref_nodes) != len(got):
            out.append("%s count %d vs %d" % (path, len(ref_nodes), len(got)))
        for ref, mine in zip(ref_nodes, got):
            element = path.rsplit("/", 1)[-1]
            for name, value in ref.items():
                if name in skip or "%s@%s" % (element, name) in skip:
                    continue
                if name not in mine:
                    out.append("%s missing @%s" % (path, name))
                elif values and name not in VOLATILE and mine[name] != value:
                    out.append("%s @%s %r vs %r" % (path, name, value[:60], mine[name][:60]))
    return sorted(set(out))


def start(extra_sections=()):
    """The rig up: files made, both stubs serving, rex started and every track pushed."""
    work = tempfile.mkdtemp(prefix="rex-music-")
    items = make_library(os.path.join(work, "media"))
    bytes_port, handler = range_server(os.path.join(work, "media"))
    agent_url, asked = agent_server()
    rex = Rex(work, bytes_port, agent_url, extra_sections)
    return work, items, rex, handler, asked
