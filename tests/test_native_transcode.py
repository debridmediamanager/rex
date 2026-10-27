#!/usr/bin/env python3
"""The universal transcoder (`REX_TRANSCODER`) end to end, against real media.

Stands up a local zurg serving three generated clips with ordinary byte-range
semantics, starts rex with an FFmpeg, and walks Plex Web 4.160's own conversion
request (`tests/fixtures/transcode/`, recorded from plexscratch) through decision,
start.m3u8, the media playlist, segments, a far seek, the session listings, the
timeline and stop -- decoding what comes back rather than trusting a 200.

    remux   h264/aac mkv              video and audio copied into MPEG-TS
    convert hevc/ac3 5.1 mkv          video converted to h264, audio to aac 5.1
    capped  the remux at 640x360/1 Mbps  video converted and scaled down

And the encoder-less server is unchanged without it: the same request is the
"neither" decision and `start.m3u8` is the 404 it always was.

A claimed rex replays what a real PMS answers a conversion's session requests sent
without a token (`session_gate_s143.json`): keyed to the session, not the token.

    python3 tests/test_native_transcode.py      # REX_TRANSCODER=/path/to/ffmpeg optional
"""
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from server_command import register_source, server_command, test_ffmpeg
from startup_wait import poll_until
from test_ports import free_port

ROOT = os.environ.get("REX_TEST_ROOT",
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FFMPEG = test_ffmpeg()
FFPROBE = os.path.join(os.path.dirname(FFMPEG), "ffprobe") if FFMPEG else None
if FFPROBE and not os.path.exists(FFPROBE):
    FFPROBE = shutil.which("ffprobe")
DURATION = 30

results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print("  %s  %-62s %s" % ("PASS" if ok else "FAIL", label, detail))


MEDIA = {}
# A clip zurg serves at this many bytes a second: the run outlasts the first segment.
SLOW_RATE = 2_000_000
# The times of each file's small range reads (a subtitle line's), by file name.
SMALL_READS = {}


class Zurg(http.server.BaseHTTPRequestHandler):
    """zurg's WebDAV file read over the generated clips."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        name = urllib.parse.unquote(self.path).rsplit("/", 1)[-1]
        if name in ("live.m3u8", "livesubs.m3u8", "livesubs-text") or name.startswith("cue-"):
            body = {"live.m3u8": live_playlist, "livesubs.m3u8": live_master, "livesubs-text": live_cues}.get(name, lambda: "WEBVTT\n\n")().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        path = MEDIA.get(name)
        if not path:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = os.path.getsize(path)
        first, last, partial = 0, size - 1, False
        match = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if match and match.group(1):
            first = int(match.group(1))
            last = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            partial = True
        # A `lag` file answers small reads slowly, as a debrid source paced to its read budget
        # does: its subtitle is read slower than a conversion runs.
        if partial and last - first < 8192:
            SMALL_READS.setdefault(name, []).append(time.time())
        if name.startswith("lag") and partial and last - first < 8192:
            time.sleep(0.25)
        self.send_response(206 if partial else 200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(last - first + 1))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (first, last, size))
        self.end_headers()
        with open(path, "rb") as handle:
            handle.seek(first)
            remaining = last - first + 1
            slow = name.startswith("slow")
            try:
                while remaining > 0:
                    chunk = handle.read(min(remaining, 1 << 16))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                    if slow:
                        time.sleep(len(chunk) / SLOW_RATE)
            except (BrokenPipeError, ConnectionResetError):
                pass


# A live channel's HLS, as a broadcast serves it: a window of its newest 2 s segments that moves
# on in real time from the first read, and no ENDLIST (usatv's channels, rex-stremio's tv type).
LIVE = {"segments": [], "since": None}


def live_playlist():
    LIVE["since"] = LIVE["since"] or time.time()
    made = min(len(LIVE["segments"]), 3 + int((time.time() - LIVE["since"]) / 2))
    first = max(0, made - 5)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:%d" % first]
    for name in LIVE["segments"][first:made]:
        lines += ["#EXTINF:2.000000,", name]
    return "\n".join(lines) + "\n"


def live_master():
    """PenguPlay's proxied channels' master playlist (2026-10-24, URLs redacted): a WebVTT rendition
    whose playlist and cues have no extension, which FFmpeg 7.1+ refuses as a whole unless told
    `-extension_picky 0`."""
    return ('#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="rus t1",AUTOSELECT=YES,LANGUAGE="rus",URI="livesubs-text"\n'
            '#EXT-X-STREAM-INF:AVERAGE-BANDWIDTH=6070000,BANDWIDTH=7590000,SUBTITLES="subs",RESOLUTION=320x180,FRAME-RATE=24.000,CODECS="avc1.640028"\n'
            'live.m3u8\n')


def live_cues():
    return re.sub(r"live(\d{3})\.ts", r"cue-\1", live_playlist())


def make_clip(path, video, audio, duration, size="1280x720"):
    subprocess.run([FFMPEG, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=%s:r=24:d=%d" % (size, duration),
                    "-f", "lavfi", "-i", "sine=f=440:d=%d" % duration,
                    *video, *audio, "-shortest", path], check=True)


# A white box of a PGS subtitle, shown for the whole clip: FFmpeg can encode no bitmap
# subtitle from text, so the .sup is written by hand (PCS, WDS, PDS, ODS, END segments).
BOX = (540, 500, 200, 60)


def make_pgs(path, width, height, duration):
    import struct
    x, y, w, h = BOX

    def segment(pts, kind, body):
        return b"PG" + struct.pack(">IIBH", pts, pts, kind, len(body)) + body

    def show(pts, objects):
        pcs = struct.pack(">HHBHBBBB", width, height, 0x10, 0 if objects else 1, 0x80 if objects else 0, 0, 0, len(objects))
        for _ in objects:
            pcs += struct.pack(">HBBHH", 0, 0, 0, x, y)
        return segment(pts, 0x16, pcs) + segment(pts, 0x17, struct.pack(">BBHHHH", 1, 0, x, y, w, h))

    line = (bytes([0, 0x80 | w, 1]) if w < 64 else bytes([0, 0xC0 | (w >> 8), w & 0xFF, 1])) + b"\0\0"
    rle = line * h
    ods = struct.pack(">HBB", 0, 0, 0xC0) + (len(rle) + 4).to_bytes(3, "big") + struct.pack(">HH", w, h) + rle
    data = (show(0, [0]) + segment(0, 0x14, bytes([0, 0, 1, 235, 128, 128, 255])) + segment(0, 0x15, ods)
            + segment(0, 0x80, b"") + show(duration * 90000, []) + segment(duration * 90000, 0x80, b""))
    open(path, "wb").write(data)


def box_script(lines=((0, 30),)):
    """An ASS script drawing a white box over BOX for each (start, end) in seconds, which
    libass renders without a font."""
    x, y, w, h = BOX
    stamp = lambda t: "%d:%02d:%02d.%02d" % (t // 3600, t // 60 % 60, t % 60, round(t * 100) % 100)
    return ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1280\nPlayResY: 720\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\nStyle: Default,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,0,0,0,0,"
            "100,100,0,0,1,0,0,7,0,0,0,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n" + "".join(
                "Dialogue: 0,%s,%s,Default,,0,0,0,,{\\pos(%d,%d)\\p1}m 0 0 l %d 0 %d %d 0 %d{\\p0}\n"
                % (stamp(start), stamp(end), x, y, w, w, h, h) for start, end in lines))


def luma(path, at, crop):
    """The mean luma of a crop of the frame at `at` seconds."""
    x, y, w, h = crop
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", str(at), "-i", path, "-frames:v", "1",
                          "-vf", "crop=%d:%d:%d:%d,format=gray" % (w, h, x, y), "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return sum(raw) / max(len(raw), 1)


def frame_lumas(path, crop, count):
    """Decode sequentially from the first video frame, ignoring the muxer's timestamp delay."""
    x, y, w, h = crop
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-map", "0:v:0",
                          "-vf", "crop=%d:%d:%d:%d,format=gray" % (w, h, x, y),
                          "-frames:v", str(count), "-vsync", "0", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    frame_size = w * h
    if len(raw) != count * frame_size:
        raise AssertionError("expected %d decoded crop frames, got %d bytes" % (count, len(raw)))
    return [sum(raw[offset:offset + frame_size]) / frame_size
            for offset in range(0, len(raw), frame_size)]


def probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def row(key, title, file, streams, size, duration, subtitle=None, subtitle_codec="pgs"):
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    # A real PMS names DTS `dca`, as rex's mediainfo does.
    audio = dict(audio, codec_name={"dts": "dca"}.get(audio["codec_name"], audio["codec_name"]))
    return {
        "ratingKey": key, "key": "/library/metadata/%s" % key,
        "librarySectionID": 1, "librarySectionTitle": "Clips",
        "librarySectionKey": "/library/sections/1",
        "type": "movie", "title": title, "year": 2026,
        "guid": "plex://movie/00000000000000000000000%s" % key,
        "duration": duration * 1000, "addedAt": 1700000000, "updatedAt": 1700000000,
        "Media": [{"id": int(key) * 100, "container": "mkv", "videoCodec": video["codec_name"],
                   "audioCodec": audio["codec_name"], "width": 1280, "height": 720,
                   "duration": duration * 1000, "videoResolution": "720", "bitrate": 2500,
                   "videoFrameRate": "24p", "aspectRatio": 1.78,
                   "audioChannels": int(audio["channels"]),
                   "Part": [{"id": int(key) * 10, "container": "mkv", "size": size,
                             "duration": duration * 1000,
                             "key": "/library/parts/%d/1700000000/file.mkv" % (int(key) * 10),
                             "file": "/zurg/movies/%s/%s" % (title, file),
                             "Stream": [
                                 {"id": int(key) * 1000 + 1, "streamType": 1, "codec": video["codec_name"],
                                  "index": video["index"], "width": 1280, "height": 720, "bitDepth": 8,
                                  "frameRate": 24.0, "bitrate": 2300, "displayTitle": "720p"},
                                 {"id": int(key) * 1000 + 2, "streamType": 2, "codec": audio["codec_name"],
                                  "index": audio["index"], "channels": int(audio["channels"]),
                                  "bitrate": 192, "selected": True, "displayTitle": "Unknown"},
                             ] + ([{"id": int(key) * 1000 + 3, "streamType": 3, "codec": subtitle_codec,
                                    "index": subtitle["index"], "selected": True,
                                    "displayTitle": "English (%s)" % subtitle_codec.upper()}]
                                  if subtitle else [])}]}],
    }


# Plex Web 4.160's conversion request (c45-play-mp4), as the fixture recorded it.
def plex_web_query(key, session, **extra):
    recorded = json.load(open(os.path.join(ROOT, "tests/fixtures/transcode/scratch_copy_mkv.json")))
    pairs = []
    for name, value in recorded["exchanges"][0]["query"]:
        if name == "path":
            value = "/library/metadata/%s" % key
        elif name == "session":
            value = session
        elif name == "X-Plex-Session-Identifier":
            value = "playback-" + session
        elif name == "X-Plex-Token":
            continue
        pairs.append((name, extra.pop(name, value)))
    pairs.extend(extra.items())
    return pairs


# Plex Web 4.147.1's (the bundled /web client's) DASH request, as the fixture recorded it.
def dash_query(key, session, **extra):
    recorded = json.load(open(os.path.join(ROOT, "tests/fixtures/transcode/scratch_dash_copy.json")))
    pairs = []
    for name, value in recorded["exchanges"][0]["query"]:
        if name == "path":
            value = "/library/metadata/%s" % key
        elif name == "session":
            value = session
        elif name == "X-Plex-Session-Identifier":
            value = "playback-" + session
        elif name == "X-Plex-Token":
            continue
        pairs.append((name, extra.pop(name, value)))
    pairs.extend(extra.items())
    return pairs


def boxes(data):
    """Top-level MP4 boxes, with a fragment's tfdt and an init's media timescale."""
    import struct
    out, at = [], 0
    while at + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[at:at + 8])
        if size < 8:
            break
        kind = kind.decode("latin-1")
        extra = {}
        body = data[at:at + size]
        if kind == "moof":
            found = body.find(b"tfdt")
            if found > 0:
                version = body[found + 4]
                extra["tfdt"] = struct.unpack(">Q", body[found + 8:found + 16])[0] if version == 1 else struct.unpack(">I", body[found + 8:found + 12])[0]
        if kind == "moov":
            found = body.find(b"mdhd")
            if found > 0:
                version = body[found + 4]
                extra["timescale"] = struct.unpack(">I", body[found + 24:found + 28] if version == 1 else body[found + 16:found + 20])[0]
        out.append((kind, extra))
        at += size
    return out


def walk_dash(port, label, key, expect_video, expect_audio, size, duration=DURATION, **extra):
    session = "d-" + label
    status, body = get(port, "/video/:/transcode/universal/decision", dash_query(key, session, **extra))[0::2]
    container = json.loads(body)["MediaContainer"] if status == 200 else {}
    media = container.get("Metadata", [{}])[0].get("Media", [{}])[0]
    part = media.get("Part", [{}])[0]
    streams = {s.get("streamType"): s for s in part.get("Stream", [])}
    check("%s: DASH decision is Conversion OK as mp4/dash" % label,
          container.get("generalDecisionCode") == 1001 and media.get("container") == "mp4"
          and media.get("protocol") == "dash" and media.get("optimizedForStreaming") is True,
          json.dumps({k: media.get(k) for k in ("container", "protocol", "width", "height")}))
    check("%s: DASH streams %s/%s on segments-video/-audio" % (label, expect_video, expect_audio),
          streams.get(1, {}).get("decision") == expect_video and streams.get(2, {}).get("decision") == expect_audio
          and streams.get(1, {}).get("location") == "segments-video"
          and streams.get(2, {}).get("location") == "segments-audio",
          "%s/%s" % (streams.get(1, {}).get("location"), streams.get(2, {}).get("location")))
    started = time.time()
    status, headers, body = get(port, "/video/:/transcode/universal/start.mpd", dash_query(key, session, **extra))
    mpd = body.decode("utf-8", "replace")
    check("%s: start.mpd is the reference's static MPD" % label,
          status == 200 and headers.get("Content-Type") == "application/dash+xml"
          and 'initialization="session/%s/$RepresentationID$/header"' % session in mpd
          and 'media="session/%s/$RepresentationID$/$Number$.m4s" startNumber="0"' % session in mpd
          and 'mimeType="video/mp4"' in mpd and 'mimeType="audio/mp4"' in mpd, str(status))
    base = "/video/:/transcode/universal/session/%s/" % session
    length = int(re.search(r'SegmentTemplate timescale="1" duration="(\d+)"', mpd).group(1)) if status == 200 else 5
    check("%s: DASH segments are %d s" % (label, 5 if expect_video == "copy" else 2),
          length == (5 if expect_video == "copy" else 2) and 'minBufferTime="PT%dS"' % length in mpd, "%d" % length)
    # A remux cuts on the source's own keyframes (the clips have a 2 s GOP); a
    # conversion places one on every boundary.
    slack = 2.1 if expect_video == "copy" else 0.6
    scales = {}
    for rep_id in (0, 1):
        status, headers, init = get(port, base + "%d/header" % rep_id)
        kinds = boxes(init)
        scales[rep_id] = next((extra_["timescale"] for kind, extra_ in kinds if kind == "moov"), 0)
        check("%s: rep %d header is an init (ftyp+moov)" % (label, rep_id),
              status == 200 and [kind for kind, _ in kinds] == ["ftyp", "moov"]
              and headers.get("Content-Type") == "application/octet-stream", " ".join(k for k, _ in kinds))
    if size:
        width = re.search(r'mimeType="video/mp4"[^>]*width="(\d+)" height="(\d+)"', mpd)
        check("%s: MPD video is %dx%d" % (label, size[0], size[1]),
              width and (int(width.group(1)), int(width.group(2))) == size, width.group(0)[-30:] if width else "none")
    for rep_id in (0, 1):
        for index in (0, 1):
            status, _, chunk = get(port, base + "%d/%d.m4s" % (rep_id, index))
            kinds = boxes(chunk)
            names = [kind for kind, _ in kinds]
            tfdt = next((extra_.get("tfdt") for kind, extra_ in kinds if kind == "moof"), None)
            seconds = tfdt / scales[rep_id] if tfdt is not None and scales[rep_id] else -1
            check("%s: rep %d segment %d is init+fragment at %ds" % (label, rep_id, index, index * length),
                  status == 200 and names[:2] == ["ftyp", "moov"] and "moof" in names and "mdat" in names
                  and abs(seconds - index * length) < slack, "%s tfdt %.3fs" % (" ".join(names), seconds))
    if rep_id == 1:
        print("        first DASH segments in %.2fs from start.mpd" % (time.time() - started))
    far = (duration // length) - 2
    for rep_id in (0, 1):
        status, _, chunk = get(port, base + "%d/%d.m4s" % (rep_id, far))
        kinds = boxes(chunk)
        tfdt = next((extra_.get("tfdt") for kind, extra_ in kinds if kind == "moof"), None)
        seconds = tfdt / scales[rep_id] if tfdt is not None and scales[rep_id] else -1
        check("%s: rep %d far seek (segment %d) carries the title's clock" % (label, rep_id, far),
              status == 200 and abs(seconds - far * length) < slack, "tfdt %.3fs, want %d" % (seconds, far * length))
    status, _, body = get(port, "/transcode/sessions")
    rows = [r for r in json.loads(body)["MediaContainer"].get("TranscodeSession", []) if r.get("key") == session]
    check("%s: /transcode/sessions says dash/mp4" % label,
          rows and rows[0].get("protocol") == "dash" and rows[0].get("container") == "mp4", "")
    status, _, _ = get(port, "/video/:/transcode/universal/stop", [("session", session)])
    check("%s: stop is 200" % label, status == 200, str(status))


def get(port, path, params=(), accept="application/json", timeout=60, method="GET"):
    url = "http://127.0.0.1:%d%s" % (port, path)
    if params:
        url += "?" + urllib.parse.urlencode(list(params))
    request = urllib.request.Request(url, method=method, headers={"Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()
    except OSError:
        # A request that never answers is a failure to report, not a crash.
        return 0, {}, b""


def start_server(catalog, zurg_port, transcoder, scratch, **env):
    port = free_port()
    # Software encoding unless the caller names a device (`REX_TRANSCODE_HW=vaapi` with an Intel
    # iGPU).
    environment = dict(os.environ, REX_TRANSCODE_HW=os.environ.get("REX_TRANSCODE_HW", "none"),
                       REX_TRANSCODE_DIR=os.path.join(scratch, "segments-%d" % port), **env)
    environment["REX_TRANSCODER"] = "off"
    if transcoder:
        environment["REX_TRANSCODER"] = FFMPEG
    log = open(os.path.join(scratch, "rex-%d.log" % port), "w")
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", catalog, "--assert-claimed", root=ROOT),
        cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
    def ready():
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=2)
            return True
        except Exception:
            return False
    poll_until(ready, 40)
    register_source(port, "zurg", {"url": "http://127.0.0.1:%d/dav" % zurg_port, "mount": "/zurg"})
    return port, process, log.name


def replay_session_gate(port, token):
    """A real PMS keys a conversion's own requests to the session, not to the token: Plex Web
    asks for the playlist start.m3u8 names relative to it, which drops the token, and a claimed
    rex answered it 401 (Plex Web 4.147.1, 2026-10-15: "Playback Error, h4"). Replays what
    plexscratch answered a source its gate refuses, which a claimed server's gate refuses too."""
    recorded = json.load(open(os.path.join(ROOT, "tests/fixtures/transcode/session_gate_s143.json")))
    tokens = {"valid": token, "wrong": "WRONG-TOKEN-0000000000", "none": None}
    for exchange in recorded["exchanges"]:
        if exchange["path"].endswith("/start.m3u8") and exchange["token"] == "none":
            continue  # the reference refuses it 403 where rex's refusal is 401; not this gate
        params = [(name, "/library/metadata/1" if name == "path" else value)
                  for name, value in exchange["query"] if name != "X-Plex-Token"]
        if tokens[exchange["token"]]:
            params.append(("X-Plex-Token", tokens[exchange["token"]]))
        status = get(port, exchange["path"], params, method=exchange["method"])[0]
        check("claimed: %s %s, %s token" % (exchange["method"], exchange["path"].replace("/video/:/transcode/universal", "")[:44], exchange["token"]),
              status == exchange["status"], "%s, the reference %s" % (status, exchange["status"]))


def wait_for_log(path, pattern, timeout=60):
    """The first group of `pattern` once the server log has it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = re.search(pattern, open(path).read())
        if found:
            return found.group(1)
        time.sleep(0.2)
    return None


def decision(port, key, session, **extra):
    status, _, body = get(port, "/video/:/transcode/universal/decision", plex_web_query(key, session, **extra))
    return status, json.loads(body)["MediaContainer"] if status == 200 else {}


def walk(port, scratch, label, key, expect_video, expect_audio, size, duration=DURATION, muxer="mpegts", **extra):
    session = "s-" + label
    mkv = muxer == "mkv"
    status, container = decision(port, key, session, **extra)
    check("%s: decision is Conversion OK (1001)" % label,
          status == 200 and container.get("generalDecisionCode") == 1001
          and container.get("transcodeDecisionCode") == 1001
          and container.get("directPlayDecisionCode") == 3000,
          "%s %s" % (status, container.get("generalDecisionCode")))
    media = container.get("Metadata", [{}])[0].get("Media", [{}])[0]
    part = media.get("Part", [{}])[0]
    streams = {s.get("streamType"): s for s in part.get("Stream", [])}
    check("%s: decision Media is the %s/hls output" % (label, muxer),
          media.get("container") == muxer and media.get("protocol") == "hls"
          and part.get("decision") == "transcode" and "key" not in part and "file" not in part,
          json.dumps({k: media.get(k) for k in ("container", "protocol", "width", "height", "videoCodec", "audioCodec")}))
    check("%s: stream decisions %s/%s" % (label, expect_video, expect_audio),
          streams.get(1, {}).get("decision") == expect_video and streams.get(2, {}).get("decision") == expect_audio
          and streams.get(1, {}).get("location") == "segments-av",
          "%s/%s" % (streams.get(1, {}).get("decision"), streams.get(2, {}).get("decision")))
    if size:
        check("%s: output size %dx%d" % (label, size[0], size[1]),
              (media.get("width"), media.get("height")) == size, "%sx%s" % (media.get("width"), media.get("height")))

    started = time.time()
    status, headers, body = get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query(key, session, **extra))
    master = body.decode("utf-8", "replace")
    check("%s: start.m3u8 names the session's media playlist" % label,
          status == 200 and headers.get("Content-Type") == "application/vnd.apple.mpegurl"
          and "session/%s/base/index.m3u8?X-Plex-Incomplete-Segments=1" % session in master,
          "%s %r" % (status, master[-90:]))
    status, _, body = get(port, "/video/:/transcode/universal/session/%s/base/index.m3u8" % session)
    playlist = body.decode()
    names = [line for line in playlist.splitlines() if line and not line.startswith("#")]
    check("%s: media playlist lists the whole title, then ENDLIST" % label,
          status == 200 and playlist.rstrip().endswith("#EXT-X-ENDLIST") and len(names) >= 3
          and names[0] == "00000.ts" and ('#EXT-X-MAP:URI="header"' in playlist) == mkv, "%d segments" % len(names))
    length = int(re.search(r"TARGETDURATION:(\d+)", playlist).group(1)) if status == 200 else 4
    base = "/video/:/transcode/universal/session/%s/base/" % session
    # Matroska clusters only parse behind the run's header, the playlist's EXT-X-MAP.
    header = b""
    status, headers, first = get(port, base + "00000.ts")
    ttfs = time.time() - started
    if mkv:
        status_h, headers_h, header = get(port, base + "header")
        check("%s: the header is Matroska's, as application/octet-stream" % label,
              status_h == 200 and headers_h.get("Content-Type") == "application/octet-stream"
              and header[:4] == b"\x1a\x45\xdf\xa3", "%s %s" % (status_h, headers_h.get("Content-Type")))
    check("%s: first segment is %s" % (label, "a Matroska cluster" if mkv else "MPEG-TS"),
          status == 200 and headers.get("Content-Type") == "video/MP2T" and (first[:4] == b"\x1f\x43\xb6\x75" if mkv else first[:1] == b"\x47"),
          "%s %d bytes, %.2fs from start.m3u8" % (status, len(first), ttfs))
    path = os.path.join(scratch, "%s-0.ts" % label)
    open(path, "wb").write(header + first)
    info = probe(path) if status == 200 else {"streams": [], "format": {}}
    # The MPEG-TS muxer offsets every timestamp by the same constant (`-max_delay`, as
    # Plex's own segments are); a player anchors on the first segment, so offsets are
    # measured from it.
    start0 = float(info["format"].get("start_time", 0))
    codecs = sorted(s["codec_name"] for s in info["streams"])
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), {})
    audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), {})
    check("%s: segment decodes as h264 + %s" % (label, streams.get(2, {}).get("codec")),
          video.get("codec_name") == "h264" and audio.get("codec_name") == streams.get(2, {}).get("codec"),
          " ".join(codecs))
    if size:
        check("%s: segment is %dx%d" % (label, size[0], size[1]),
              (video.get("width"), video.get("height")) == size, "%sx%s" % (video.get("width"), video.get("height")))
    status, _, second = get(port, base + "00001.ts")
    path1 = os.path.join(scratch, "%s-1.ts" % label)
    open(path1, "wb").write(header + second)
    start1 = float(probe(path1)["format"].get("start_time", -1)) if status == 200 else -1
    check("%s: segment 1 starts where segment 0 ends" % label,
          status == 200 and abs(start1 - start0 - length) < 1.6, "offset %.3f, length %d" % (start1 - start0, length))
    far = len(names) - 2
    status, _, tail = get(port, base + "%05d.ts" % far)
    path_far = os.path.join(scratch, "%s-far.ts" % label)
    open(path_far, "wb").write(header + tail)
    start_far = float(probe(path_far)["format"].get("start_time", -1)) if status == 200 else -1
    check("%s: a far seek (segment %d) restarts at its own time" % (label, far),
          status == 200 and tail[:1] == (b"\x1f" if mkv else b"\x47") and abs(start_far - start0 - far * length) < 1.6,
          "offset %.3f, want %d" % (start_far - start0, far * length))

    status, _, body = get(port, "/transcode/sessions")
    rows = json.loads(body)["MediaContainer"].get("TranscodeSession", [])
    mine = [r for r in rows if r.get("key") == session]
    check("%s: /transcode/sessions lists it" % label,
          len(mine) == 1 and mine[0].get("videoDecision") == expect_video
          and mine[0].get("protocol") == "hls" and mine[0].get("container") == muxer,
          json.dumps({k: mine[0].get(k) for k in ("videoDecision", "audioDecision", "speed", "maxOffsetAvailable")}) if mine else "none")
    beat = [("ratingKey", key), ("key", "/library/metadata/%s" % key), ("state", "playing"),
            ("time", str(length * 1000)), ("duration", str(duration * 1000)), ("hasMDE", "1"),
            ("X-Plex-Session-Identifier", "playback-" + session),
            ("X-Plex-Client-Identifier", "thuj6pv24kf5wr6qaqla8flz"),
            ("X-Plex-Product", "Plex Web"), ("X-Plex-Platform", "Chrome")]
    status, _, body = get(port, "/:/timeline", beat)
    timeline = json.loads(body)["MediaContainer"]
    nested = timeline.get("TranscodeSession", [{}])[0]
    check("%s: the timeline carries the TranscodeSession" % label,
          nested.get("key") == "/transcode/sessions/%s" % session and timeline.get("size") == 2
          and "Bandwidths" in timeline, json.dumps({k: timeline.get(k) for k in ("size", "playbackState")}))
    status, _, body = get(port, "/status/sessions")
    listing = json.loads(body)["MediaContainer"].get("Metadata", [])
    row_ = next((r for r in listing if r.get("ratingKey") == key), {})
    check("%s: Now Playing shows the converted Media and TranscodeSession" % label,
          row_.get("TranscodeSession", {}).get("key") == "/transcode/sessions/%s" % session
          and row_.get("Media", [{}])[0].get("container") == muxer, "")
    output = row_.get("Media", [{}])[0].get("bitrate", 0)
    check("%s: its Session bandwidth is ~1.05x the output" % label,
          row_.get("Session", {}).get("bandwidth") == round(output * 1.05), "%s for %s kbps" % (row_.get("Session", {}).get("bandwidth"), output))
    status, _, _ = get(port, "/video/:/transcode/universal/ping", [("session", session)])
    check("%s: ping a live session is 200" % label, status == 200, str(status))
    status, _, _ = get(port, "/video/:/transcode/universal/stop", [("session", session)])
    check("%s: stop is 200" % label, status == 200, str(status))
    status, _, _ = get(port, "/video/:/transcode/universal/ping", [("session", session)])
    check("%s: a stopped session's ping is 404" % label, status == 404, str(status))
    beat[2] = ("state", "stopped")
    get(port, "/:/timeline", beat)
    return ttfs


def live_source(port, scratch, android):
    """A live channel plays on. Its conversion was a title of no duration: one segment and
    ENDLIST, so Plex for Android stopped after ~10 s and Plezy too (rex live test, 2026-10-24)."""
    session = "s-live"
    status, container = decision(port, "18", session, **{name: android[name] for name in ("X-Plex-Client-Profile-Name", "X-Plex-Client-Profile-Extra")})
    check("live: decision is a conversion", status == 200 and container.get("generalDecisionCode") == 1001, str(container.get("generalDecisionCode")))
    query = plex_web_query("18", session, **{name: android[name] for name in ("X-Plex-Client-Profile-Name", "X-Plex-Client-Profile-Extra")})
    status, _, _ = get(port, "/video/:/transcode/universal/start.m3u8", query)
    base = "/video/:/transcode/universal/session/%s/base/" % session
    segments, ended = [], True
    deadline = time.time() + 16
    while time.time() < deadline:
        status, _, body = get(port, base + "index.m3u8")
        playlist = body.decode()
        ended = "#EXT-X-ENDLIST" in playlist
        segments = [line for line in playlist.splitlines() if line and not line.startswith("#")]
        if ended or (segments and int(segments[-1][:5]) >= 3):
            break
        time.sleep(1)
    check("live: the media playlist grows and has no ENDLIST", status == 200 and not ended and segments and int(segments[-1][:5]) >= 3,
          "%s ended=%s %s" % (status, ended, segments[-3:]))
    status_h, _, header = get(port, base + "header")
    served = [get(port, base + "%05d.ts" % index)[0] for index in range(4)]
    check("live: segments 0-3 are served in turn", status_h == 200 and served == [200] * 4, "%s %s" % (status_h, served))
    # Plezy's beats give its player's live window as the duration: past a minute, then at its
    # edge. A channel keeps no resume point and is never watched.
    for at in (61000, 119000):
        get(port, "/:/timeline", [("ratingKey", "18"), ("key", "/library/metadata/18"), ("state", "playing"), ("time", str(at)),
                                  ("duration", "120000"), ("X-Plex-Session-Identifier", "plezy-live"),
                                  ("X-Plex-Client-Identifier", "plezy-live"), ("X-Plex-Product", "Plezy")])
    status, _, body = get(port, "/library/metadata/18")
    row_ = json.loads(body)["MediaContainer"]["Metadata"][0] if status == 200 else {}
    check("live: a channel keeps no resume point and is never watched",
          status == 200 and "viewOffset" not in row_ and not row_.get("viewCount"),
          json.dumps({k: row_.get(k) for k in ("viewOffset", "viewCount")}))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])
    # Plezy reads the Part with mpv, which a conversion's playlist answers: it reads on, too.
    played = os.path.join(scratch, "live-part.ts")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", "http://127.0.0.1:%d/library/parts/180/1700000000/file.m3u8" % port,
                    "-t", "20", "-c", "copy", played], check=False, timeout=120)
    length = float(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", played],
                                  capture_output=True, text=True).stdout.strip() or 0) if os.path.exists(played) else 0
    check("live: a player reading its Part plays on past the first segments", length > 15, "%.2fs" % length)
    played = os.path.join(scratch, "livesubs-part.ts")
    try:
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", "http://127.0.0.1:%d/library/parts/190/1700000000/file.m3u8" % port,
                        "-t", "10", "-c", "copy", played], check=False, timeout=120)
    except subprocess.TimeoutExpired:
        pass  # a conversion that never starts holds its reader: what it wrote is measured below
    length = float(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", played],
                                  capture_output=True, text=True).stdout.strip() or 0) if os.path.exists(played) else 0
    check("live: a channel with an extensionless WebVTT rendition plays", length > 8, "%.2fs" % length)
    status, _, body = get(port, "/transcode/sessions")
    for listed in json.loads(body)["MediaContainer"].get("TranscodeSession", []) if status == 200 else []:
        if str(listed.get("key", "")).startswith(("part-180-", "part-190-")):
            get(port, "/video/:/transcode/universal/stop", [("session", listed["key"])])


def profile_audio_encoders(port, scratch):
    """Custom client capabilities, not untouched Desktop/Roku defaults.

    Those real profiles advertise these codecs in Matroska HTTP targets. Generic's
    supported add-transcode-target API lets a client request one advertised codec
    in HLS Matroska segments, without AAC/the first codec masking the other encoders.
    """
    import array

    profiles = json.load(open(os.path.join(ROOT, "data/client_profiles.json")))
    for name, codec, expected in (("Plex Desktop", "opus", "opus"),
                                  ("Plex Desktop", "vorbis", "vorbis"),
                                  ("Roku-5.x", "pcm", "pcm_s16le")):
        target = next(target for target in profiles[name]["targets"]
                      if target.get("context") == "streaming"
                      and codec in target.get("audioCodec", "").split(","))
        container = target["container"]
        video = (target.get("videoCodec") or target["codec"]).split(",")[0]
        session = "s-profile-" + codec
        extra = {"directStream": "1", "directStreamAudio": "0",
                 "X-Plex-Client-Profile-Name": "Generic",
                 "X-Plex-Client-Profile-Extra":
                     "add-transcode-target(type=videoProfile&context=streaming&protocol=hls"
                     "&container=%s&videoCodec=%s&audioCodec=%s)" % (container, video, codec)}
        try:
            status, result = decision(port, "1", session, **extra)
            media = result.get("Metadata", [{}])[0].get("Media", [{}])[0]
            audio = next((stream for stream in media.get("Part", [{}])[0].get("Stream", [])
                          if stream.get("streamType") == 2), {})
            check("%s: custom %s capabilities negotiate audio conversion" % (codec, name),
                  status == 200 and audio.get("decision") == "transcode"
                  and audio.get("codec") == codec and media.get("container") == container,
                  json.dumps(audio))
            get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("1", session, **extra))
            base = "/video/:/transcode/universal/session/%s/base/" % session
            status, _, segment = get(port, base + "00000.ts")
            header_status, _, header = get(port, base + "header")
            available = status == 200 and header_status == 200
            check("%s: negotiated Matroska audio segment is available" % codec, available,
                  "segment %s, header %s" % (status, header_status))
            if not available:
                continue
            path = os.path.join(scratch, "profile-%s-0.mkv" % codec)
            open(path, "wb").write(header + segment)
            actual = [stream["codec_name"] for stream in probe(path)["streams"]
                      if stream["codec_type"] == "audio"]
            check("%s: ffprobe identifies the requested encoded audio" % codec,
                  actual == [expected], repr(actual))
            # Decode the served audio, not the source or an encoder-name echo. The
            # fixture's 440 Hz tone must survive in a quarter second of real PCM.
            decoded = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-map", "0:a:0",
                                      "-t", "0.25", "-ac", "1", "-ar", "48000",
                                      "-f", "s16le", "-"], capture_output=True, check=True).stdout
            samples = array.array("h")
            samples.frombytes(decoded)
            if sys.byteorder != "little":
                samples.byteswap()
            crossings = sum((left < 0) != (right < 0) for left, right in zip(samples, samples[1:]))
            check("%s: produced audio decodes to the source's 440 Hz tone" % codec,
                  len(samples) == 12000 and max(map(abs, samples), default=0) > 1000
                  and 190 <= crossings <= 250,
                  "%d samples, %d zero crossings" % (len(samples), crossings))
        finally:
            get(port, "/video/:/transcode/universal/stop", [("session", session)])


def main():
    if not FFMPEG or not FFPROBE:
        print("SKIP: no ffmpeg (set REX_TRANSCODER)")
        return 0
    scratch = tempfile.mkdtemp(prefix="rex-transcode-test-")
    try:
        return run(scratch)
    finally:
        # A run leaves its clips and segments (~1 GB); left behind, they filled a disk.
        if os.environ.get("REX_KEEP_TMP"):
            print("scratch kept: %s" % scratch)
        else:
            shutil.rmtree(scratch, ignore_errors=True)


def run(scratch):
    clips = {
        "remux.mkv": (["-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-pix_fmt", "yuv420p"], ["-c:a", "aac", "-ac", "2"], DURATION),
        # Long enough that its far seek lands beyond what the slow read has produced,
        # so a REMUX restarts mid-title (a 2 s GOP, so the cut is near the boundary).
        "slow.mkv": (["-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-pix_fmt", "yuv420p"], ["-c:a", "aac", "-ac", "2"], 90),
        "convert.mkv": (["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none", "-pix_fmt", "yuv420p"],
                        ["-c:a", "ac3", "-ac", "6"], DURATION),
        # Five minutes, tiny: long enough to play past the 120 s kept behind the player.
        "long.mkv": (["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none", "-pix_fmt", "yuv420p"],
                     ["-c:a", "aac", "-ac", "2"], 300),
        # DTS 5.1 beside h264: a client that plays the video but not the audio.
        "dts.mkv": (["-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-pix_fmt", "yuv420p"],
                    ["-c:a", "dca", "-strict", "-2", "-ac", "6", "-ar", "48000"], DURATION),
    }
    items = []
    for number, (name, (video, audio, duration)) in enumerate(clips.items(), start=1):
        path = os.path.join(scratch, name)
        make_clip(path, video, audio, duration, "320x180" if name == "long.mkv" else "1280x720")
        MEDIA[name] = path
        items.append(row(str(number), name.split(".")[0].title(), name, probe(path)["streams"], os.path.getsize(path), duration))
    # The remux's h264/aac with a selected PGS subtitle, which only a conversion can show.
    plain = os.path.join(scratch, "plain.mkv")
    make_clip(plain, ["-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-pix_fmt", "yuv420p"], ["-c:a", "aac", "-ac", "2"], DURATION)
    sup = os.path.join(scratch, "box.sup")
    make_pgs(sup, 1280, 720, DURATION)
    MEDIA["subs.mkv"] = os.path.join(scratch, "subs.mkv")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", plain, "-i", sup, "-map", "0", "-map", "1", "-c", "copy",
                    MEDIA["subs.mkv"]], check=True)
    streams = probe(MEDIA["subs.mkv"])["streams"]
    items.append(row("6", "Subs", "subs.mkv", streams, os.path.getsize(MEDIA["subs.mkv"]), DURATION,
                     subtitle=next(s for s in streams if s["codec_type"] == "subtitle")))
    # The same with a selected embedded SRT, which a Matroska target carries in its segments.
    srt = os.path.join(scratch, "line.srt")
    open(srt, "w").write("1\n00:00:00,500 --> 00:00:29,000\nHello from the SRT\n")
    MEDIA["srt.mkv"] = os.path.join(scratch, "srt.mkv")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", plain, "-i", srt, "-map", "0", "-map", "1", "-c", "copy",
                    "-c:s", "srt", MEDIA["srt.mkv"]], check=True)
    streams = probe(MEDIA["srt.mkv"])["streams"]
    items.append(row("9", "Srt", "srt.mkv", streams, os.path.getsize(MEDIA["srt.mkv"]), DURATION,
                     subtitle=next(s for s in streams if s["codec_type"] == "subtitle"), subtitle_codec="srt"))
    # The same box drawn by an embedded ASS track, which an MPEG-TS client gets burned.
    script = os.path.join(scratch, "box.ass")
    open(script, "w").write(box_script())
    MEDIA["boxass.mkv"] = os.path.join(scratch, "boxass.mkv")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", plain, "-i", script, "-map", "0", "-map", "1", "-c", "copy",
                    MEDIA["boxass.mkv"]], check=True)
    streams = probe(MEDIA["boxass.mkv"])["streams"]
    items.append(row("11", "Boxass", "boxass.mkv", streams, os.path.getsize(MEDIA["boxass.mkv"]), DURATION,
                     subtitle=next(s for s in streams if s["codec_type"] == "subtitle"), subtitle_codec="ass"))
    # The SRT as an MP4's timed text (mov_text), which Matroska cannot hold.
    MEDIA["movtext.mp4"] = os.path.join(scratch, "movtext.mp4")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", plain, "-i", srt, "-map", "0", "-map", "1", "-c", "copy",
                    "-c:s", "mov_text", MEDIA["movtext.mp4"]], check=True)
    streams = probe(MEDIA["movtext.mp4"])["streams"]
    item = row("12", "Movtext", "movtext.mp4", streams, os.path.getsize(MEDIA["movtext.mp4"]), DURATION,
               subtitle=next(s for s in streams if s["codec_type"] == "subtitle"), subtitle_codec="mov_text")
    item["Media"][0]["container"] = item["Media"][0]["Part"][0]["container"] = "mp4"
    items.append(item)
    # Five minutes with a box every 2 s, read 4 lines a second (`lag`): its run converts faster.
    MEDIA["lagsubs.mkv"] = os.path.join(scratch, "lagsubs.mkv")
    lagass = os.path.join(scratch, "lag.ass")
    open(lagass, "w").write(box_script([(start, start + 2) for start in range(0, 300, 2)]))
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", MEDIA["long.mkv"], "-i", lagass, "-map", "0", "-map", "1", "-c", "copy",
                    MEDIA["lagsubs.mkv"]], check=True)
    streams = probe(MEDIA["lagsubs.mkv"])["streams"]
    items.append(row("13", "Lagsubs", "lagsubs.mkv", streams, os.path.getsize(MEDIA["lagsubs.mkv"]), 300,
                     subtitle=next(s for s in streams if s["codec_type"] == "subtitle"), subtitle_codec="ass"))
    # The slow file with 120 lines packed into 6 s at 1:40, as an opening song's karaoke is.
    MEDIA["lagdense.mkv"] = os.path.join(scratch, "lagdense.mkv")
    dense = os.path.join(scratch, "dense.ass")
    open(dense, "w").write(box_script(sorted([(start, start + 2) for start in range(0, 300, 2)]
                                             + [(100 + n * 0.05, 100.05 + n * 0.05) for n in range(120)])))
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", MEDIA["long.mkv"], "-i", dense, "-map", "0", "-map", "1", "-c", "copy",
                    MEDIA["lagdense.mkv"]], check=True)
    dense_streams = probe(MEDIA["lagdense.mkv"])["streams"]
    items.append(row("15", "Lagdense", "lagdense.mkv", dense_streams, os.path.getsize(MEDIA["lagdense.mkv"]), 300,
                     subtitle=next(s for s in dense_streams if s["codec_type"] == "subtitle"), subtitle_codec="ass"))
    # The same file answered at full speed, for the read budget two sessions share.
    MEDIA["burstsubs.mkv"] = MEDIA["lagsubs.mkv"]
    items.append(row("14", "Burstsubs", "burstsubs.mkv", streams, os.path.getsize(MEDIA["lagsubs.mkv"]), 300,
                     subtitle=next(s for s in streams if s["codec_type"] == "subtitle"), subtitle_codec="ass"))
    # The plain clip on its own, for a subtitle uploaded to rex.
    MEDIA["plain.mkv"] = plain
    items.append(row("10", "Plain", "plain.mkv", probe(plain)["streams"], os.path.getsize(plain), DURATION))
    # The plain clip as an HLS playlist and its MPEG-TS segments, as a relay serves YouTube's
    # (format_name "hls"): there is no file to direct play, only a conversion reads it.
    hls = os.path.join(scratch, "plainhls.m3u8")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", plain, "-map", "0", "-c", "copy", "-f", "hls", "-hls_time", "4",
                    "-hls_playlist_type", "vod", "-hls_segment_filename", os.path.join(scratch, "plainhls%03d.ts"), hls], check=True)
    for name in os.listdir(scratch):
        if name.startswith("plainhls"):
            MEDIA[name] = os.path.join(scratch, name)
    item = row("17", "Plainhls", "plainhls.m3u8", probe(hls)["streams"], os.path.getsize(hls), DURATION)
    item["Media"][0]["container"] = item["Media"][0]["Part"][0]["container"] = "hls"
    items.append(item)
    # A 90 s clip as a live channel: an HLS probe with no duration (the real one is
    # tests/fixtures/transcode/live_hls_probe.json), so the row's duration is 0.
    channel = os.path.join(scratch, "channel.mkv")
    make_clip(channel, ["-c:v", "libx264", "-preset", "ultrafast", "-g", "48"], ["-c:a", "aac", "-ac", "2"], 90, "320x180")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", channel, "-map", "0", "-c", "copy",
                    "-f", "hls", "-hls_time", "2", "-hls_list_size", "0", "-hls_segment_filename", os.path.join(scratch, "live%03d.ts"),
                    os.path.join(scratch, "livevod.m3u8")], check=True)
    LIVE["segments"] = sorted(name for name in os.listdir(scratch) if re.fullmatch(r"live\d{3}\.ts", name))
    for name in LIVE["segments"]:
        MEDIA[name] = os.path.join(scratch, name)
    recorded = json.load(open(os.path.join(ROOT, "tests/fixtures/transcode/live_hls_probe.json")))["probe"]
    assert "duration" not in recorded["format"]
    item = row("18", "Live", "live.m3u8", probe(os.path.join(scratch, "livevod.m3u8"))["streams"], 0, 0)
    item["Media"][0]["container"] = item["Media"][0]["Part"][0]["container"] = "hls"
    items.append(item)
    item = row("19", "Livesubs", "livesubs.m3u8", probe(os.path.join(scratch, "livevod.m3u8"))["streams"], 0, 0)
    item["Media"][0]["container"] = item["Media"][0]["Part"][0]["container"] = "hls"
    items.append(item)
    # The plain clip as MPEG-TS, as YouTube's HLS serves its h264/aac (stream types 0x1b/0x0f,
    # which a copy into MP4 kept as its codec tags until 2026-10-22).
    MEDIA["plain.ts"] = os.path.join(scratch, "plain.ts")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", plain, "-map", "0", "-c", "copy", MEDIA["plain.ts"]], check=True)
    item = row("16", "Plaints", "plain.ts", probe(MEDIA["plain.ts"])["streams"], os.path.getsize(MEDIA["plain.ts"]), DURATION)
    item["Media"][0]["container"] = item["Media"][0]["Part"][0]["container"] = "mpegts"
    items.append(item)
    # HDR: a PQ-tagged 10-bit HEVC (HDR10), and a stand-in for a Dolby Vision profile 5
    # release carrying the real P5 clip's probe fields (tests/fixtures/transcode/hdr_probes.json):
    # P5 signals no transfer at all, so only its DOVI fields say it needs tonemapping.
    hdr_probes = json.load(open(os.path.join(ROOT, "tests/fixtures/transcode/hdr_probes.json")))["probes"]
    hevc10 = ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-x265-params"]
    pq = "log-level=none:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:hdr10=1:max-cll=1000,400"
    for key, name, params in (("7", "hdr10.mkv", pq), ("8", "dvp5.mkv", "log-level=none")):
        MEDIA[name] = os.path.join(scratch, name)
        make_clip(MEDIA[name], hevc10 + [params], ["-c:a", "aac", "-ac", "2"], DURATION)
        item = row(key, name.split(".")[0].title(), name, probe(MEDIA[name])["streams"], os.path.getsize(MEDIA[name]), DURATION)
        video = item["Media"][0]["Part"][0]["Stream"][0]
        video["bitDepth"] = 10
        if key == "7":
            video["colorTrc"] = "smpte2084"
        else:
            dovi = next(d for d in hdr_probes["dv-p5"]["streams"][0]["side_data_list"] if "dv_profile" in d)
            video.update({"DOVIPresent": True, "DOVIProfile": dovi["dv_profile"], "DOVILevel": dovi["dv_level"],
                          "DOVIBLCompatID": dovi["dv_bl_signal_compatibility_id"], "DOVIBLPresent": True,
                          "DOVIRPUPresent": True, "DOVIELPresent": False})
        items.append(item)
    # The HDR10 clip with the PGS box selected: a 4K HDR title's burned image subtitle, which on
    # fun's J4105 dropped the conversion to 0.25x until it stayed on the device (2026-10-27).
    MEDIA["hdrsubs.mkv"] = os.path.join(scratch, "hdrsubs.mkv")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", MEDIA["hdr10.mkv"], "-i", sup, "-map", "0", "-map", "1", "-c", "copy",
                    MEDIA["hdrsubs.mkv"]], check=True)
    streams = probe(MEDIA["hdrsubs.mkv"])["streams"]
    item = row("20", "Hdrsubs", "hdrsubs.mkv", streams, os.path.getsize(MEDIA["hdrsubs.mkv"]), DURATION,
               subtitle=next(s for s in streams if s["codec_type"] == "subtitle"))
    item["Media"][0]["Part"][0]["Stream"][0].update({"bitDepth": 10, "colorTrc": "smpte2084"})
    items.append(item)
    catalog = os.path.join(scratch, "catalog.json")
    json.dump({"machineIdentifier": "0000000000000000000000000000000000000007", "friendlyName": "rex-transcode",
               "sections": [{"key": "1", "type": "movie", "title": "Clips", "uuid": "0" * 39 + "7"}],
               "items": items}, open(catalog, "w"))
    zurg_port = free_port()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", zurg_port), Zurg)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    print("encoder-less (no REX_TRANSCODER)")
    port, process, log = start_server(catalog, zurg_port, False, scratch)
    try:
        status, container = decision(port, "1", "plain")
        check("without an encoder the conversion request is 'neither'",
              container.get("generalDecisionCode") == 2000 and container.get("transcodeDecisionCode") == 4005,
              str(container.get("generalDecisionCode")))
        status, _, _ = get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("1", "plain"))
        # Refused, so never decided: the undecided session's 400, as before.
        check("without an encoder start.m3u8 is still the undecided 400", status == 400, str(status))
        status, _, body = get(port, "/")
        check("without an encoder / claims no transcoder",
              json.loads(body)["MediaContainer"].get("transcoderVideo") is False, "")
    finally:
        process.terminate()
        process.wait()

    print("claimed, with REX_TRANSCODER (plex.tv on a closed port)")
    claimed = os.path.join(scratch, "claimed")
    os.makedirs(claimed)
    shutil.copy(catalog, claimed)
    json.dump({"machine": "0000000000000000000000000000000000000007", "token": "SERVER-TOKEN-STUB"},
              open(os.path.join(claimed, "claim_state.json"), "w"))
    port, process, log = start_server(os.path.join(claimed, "catalog.json"), zurg_port, True, scratch,
                                      REX_PLEXTV_URL="http://127.0.0.1:%d" % free_port())
    try:
        replay_session_gate(port, "SERVER-TOKEN-STUB")
    finally:
        process.terminate()
        process.wait()

    print("with REX_TRANSCODER=%s" % FFMPEG)
    port, process, log = start_server(catalog, zurg_port, True, scratch)
    try:
        status, _, body = get(port, "/")
        check("/ claims transcoderVideo", json.loads(body)["MediaContainer"].get("transcoderVideo") is True, "")
        status, container = decision(port, "1", "direct", directPlay="1")
        check("a direct-playable request is still MDE direct play",
              container.get("mdeDecisionCode") == 1000 and "generalDecisionCode" not in container, "")
        walk(port, scratch, "remux", "1", "copy", "copy", None)
        # Plex for Android at Original quality decides direct play itself (hasMDE) from the codecs
        # alone, and played an HLS-backed Part's playlist bytes as the video (rex-fun, 2026-10-23).
        android = dict(json.load(open(os.path.join(ROOT, "tests/fixtures/decisions/generic_transcode_target_s1422.json")))["decisions"][-1]["query"])
        # Original quality, as the app sent it: no bitrate cap and an EMPTY videoResolution.
        android.pop("maxVideoBitrate")
        android.update(directPlay="1", directStream="1", directStreamAudio="1", videoQuality="100", videoResolution="")
        for key, label in (("17", "hls"), ("10", "plain")):
            status, _, body = get(port, "/video/:/transcode/universal/decision",
                                  list(dict(android, path="/library/metadata/" + key, session="mde-" + label).items()))
            answer = json.loads(body)["MediaContainer"] if status == 200 else {}
            if label == "hls":
                check("an HLS-backed Part is never direct play, even when the app decides (hasMDE)",
                      answer.get("generalDecisionCode") == 1001 and answer.get("directPlayDecisionCode") == 3000
                      and "mdeDecisionCode" not in answer, json.dumps({k: v for k, v in answer.items() if "Decision" in k}))
            else:
                check("a file the app decides on is still its direct play",
                      answer.get("mdeDecisionCode") == 1000 and "generalDecisionCode" not in answer,
                      json.dumps({k: v for k, v in answer.items() if "Decision" in k}))
            get(port, "/video/:/transcode/universal/stop", [("session", "mde-" + label)])
        walk(port, scratch, "hls-source", "17", "copy", "copy", None)
        # Plezy plays every Original-quality item from its Part URL with mpv and asks for no
        # decision; an HLS-backed Part handed it the source's own playlist, which played as a few
        # seconds (rex-fun, 2026-10-23). The read is a conversion now: one title-long timeline.
        status, headers, body = get(port, "/library/parts/170/1700000000/file.m3u8")
        check("an HLS-backed Part read is sent to a conversion's start.m3u8",
              status == 200 and body.startswith(b"#EXTM3U") and b"session/part-170-" in body, "%s %r" % (status, body[:80]))
        played = os.path.join(scratch, "part-read.ts")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", "http://127.0.0.1:%d/library/parts/170/1700000000/file.m3u8" % port,
                        "-c", "copy", played], check=False, timeout=120)
        length = float(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", played],
                                      capture_output=True, text=True).stdout.strip() or 0) if os.path.exists(played) else 0
        check("a player reading that Part plays the whole title, not one segment",
              abs(length - DURATION) < 1.5, "%.2fs of %ds" % (length, DURATION))
        # A player that read to the end stops nothing; the idle reaper would. Stop them here.
        status, _, body = get(port, "/transcode/sessions")
        for listed in json.loads(body)["MediaContainer"].get("TranscodeSession", []) if status == 200 else []:
            if str(listed.get("key", "")).startswith("part-170-"):
                get(port, "/video/:/transcode/universal/stop", [("session", listed["key"])])
        live_source(port, scratch, android)
        status, _, body = get(port, "/library/parts/100/1700000000/file.mkv")
        check("a file's Part read is still its bytes", status == 200 and body[:4] == b"\x1a\x45\xdf\xa3", str(status))
        # Plex for Android's own target is Matroska, which takes no ADTS AAC as copied out of
        # MPEG-TS: "Error parsing AAC extradata, unable to determine samplerate" (rex-fun, 2026-10-23).
        # The picture is still copied; the AAC is converted.
        walk(port, scratch, "hls-source-android", "17", "copy", "transcode", None, muxer="mkv",
             **{name: android[name] for name in ("X-Plex-Client-Profile-Name", "X-Plex-Client-Profile-Extra")})
        # A segment is served as soon as FFmpeg moves on to the next one, not when the
        # run ends: FFmpeg buffers its own segment list until then, which made the
        # first segment of a slow source wait for the whole file.
        whole = os.path.getsize(MEDIA["slow.mkv"]) / SLOW_RATE
        ttfs = walk(port, scratch, "slow", "2", "copy", "copy", None, duration=90)
        check("slow: the first segment comes while the run is still reading",
              ttfs < whole * 0.6, "%.2fs, the whole file takes %.1fs" % (ttfs, whole))
        walk(port, scratch, "convert", "3", "transcode", "transcode", (1280, 720))
        # Plex for iOS and Plex for Android (Mobile) name the empty Generic profile and bring
        # their whole HLS target in X-Plex-Client-Profile-Extra, which a real 1.42.2 converts to (rex#5).
        # Their targets take AC3, so convert.mkv's 5.1 AC3 is copied beside a converted picture, and
        # x265's ~10 s GOP put the copied audio of a far seek's restart 5 s early until `-ss 0`.
        recorded = json.load(open(os.path.join(ROOT, "tests/fixtures/decisions/generic_transcode_target_s1422.json")))
        for asked in recorded["decisions"]:
            if asked["label"].endswith("generic-target"):
                walk(port, scratch, asked["label"], "3", "transcode", "copy", (1280, 720), directStream="0", muxer="mkv",
                     **{name: asked["req_headers"][name] for name in ("X-Plex-Client-Profile-Name", "X-Plex-Client-Profile-Extra")})
        # A selected text subtitle was dropped from every conversion. The reference copies an
        # SRT into these apps' Matroska segments (`subtitles_s1422.json`).
        android = next(asked for asked in recorded["decisions"] if asked["label"] == "android-generic-target")
        profile = {name: android["req_headers"][name] for name in ("X-Plex-Client-Profile-Name", "X-Plex-Client-Profile-Extra")}
        status, container = decision(port, "9", "s-srt", directStream="0", subtitles="auto", **profile)
        part = container.get("Metadata", [{}])[0].get("Media", [{}])[0].get("Part", [{}])[0]
        text = next((s for s in part.get("Stream", []) if s.get("streamType") == 3), {})
        check("srt: the selected SRT is copied into the segments, as the reference's row says",
              (text.get("decision"), text.get("format"), text.get("location")) == ("copy", "srt", "segments-av"),
              json.dumps(text))
        get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("9", "s-srt", directStream="0", subtitles="auto", **profile))
        base = "/video/:/transcode/universal/session/s-srt/base/"
        _, _, header = get(port, base + "header")
        status, _, first = get(port, base + "00000.ts")
        joined = os.path.join(scratch, "srt-0.mkv")
        open(joined, "wb").write(header + first)
        lines = subprocess.run([FFMPEG, "-v", "error", "-i", joined, "-map", "0:s:0", "-f", "srt", "-"],
                               capture_output=True, text=True).stdout
        check("srt: the first segment carries the subtitle's text", "Hello from the SRT" in lines, repr(lines[:120]))
        get(port, "/video/:/transcode/universal/stop", [("session", "s-srt")])
        # An MP4's timed text was dropped for them too: Matroska cannot hold mov_text, so it is
        # converted to SRT in the pass.
        status, container = decision(port, "12", "s-mov", directStream="0", subtitles="auto", **profile)
        part = container.get("Metadata", [{}])[0].get("Media", [{}])[0].get("Part", [{}])[0]
        text = next((s for s in part.get("Stream", []) if s.get("streamType") == 3), {})
        check("mov_text: the apps get the MP4's timed text converted to SRT",
              (text.get("decision"), text.get("format"), text.get("location")) == ("transcode", "srt", "segments-av"),
              json.dumps(text))
        get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("12", "s-mov", directStream="0", subtitles="auto", **profile))
        base = "/video/:/transcode/universal/session/s-mov/base/"
        _, _, header = get(port, base + "header")
        status, _, first = get(port, base + "00000.ts")
        joined = os.path.join(scratch, "mov-0.mkv")
        open(joined, "wb").write(header + first)
        lines = subprocess.run([FFMPEG, "-v", "error", "-i", joined, "-map", "0:s:0", "-f", "srt", "-"],
                               capture_output=True, text=True).stdout
        check("mov_text: the first segment carries the subtitle's text", "Hello from the SRT" in lines, repr(lines[:120]))
        get(port, "/video/:/transcode/universal/stop", [("session", "s-mov")])
        # A subtitle uploaded to rex was dropped too: copied into the apps' Matroska segments,
        # burned into an MPEG-TS client's picture. Its drawing is a white box over BOX, which
        # libass renders without a font.
        x, y, w, h = BOX
        box = box_script()
        upload = urllib.request.Request("http://127.0.0.1:%d/library/metadata/10/subtitles?title=box.ass" % port,
                                        data=box.encode(), method="POST")
        status = urllib.request.urlopen(upload, timeout=30).status
        check("sidecar: the upload is accepted", status == 200, str(status))
        status, container = decision(port, "10", "s-side-mkv", directStream="0", subtitles="auto", **profile)
        part = container.get("Metadata", [{}])[0].get("Media", [{}])[0].get("Part", [{}])[0]
        text = next((s for s in part.get("Stream", []) if s.get("streamType") == 3), {})
        check("sidecar: the apps get the upload copied, as the reference copies its sidecar",
              (text.get("decision"), text.get("format"), text.get("location")) == ("copy", "ass", "segments-av")
              and text.get("key", "").startswith("/library/streams/"), json.dumps(text))
        get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("10", "s-side-mkv", directStream="0", subtitles="auto", **profile))
        base = "/video/:/transcode/universal/session/s-side-mkv/base/"
        _, _, header = get(port, base + "header")
        status, _, first = get(port, base + "00000.ts")
        joined = os.path.join(scratch, "side-0.mkv")
        open(joined, "wb").write(header + first)
        kinds = [s["codec_name"] for s in probe(joined)["streams"]] if status == 200 else []
        check("sidecar: the Matroska segment carries it as a subtitle stream", "ass" in kinds, " ".join(kinds))
        get(port, "/video/:/transcode/universal/stop", [("session", "s-side-mkv")])
        status, container = decision(port, "10", "s-side-ts", directStream="1")
        part = container.get("Metadata", [{}])[0].get("Media", [{}])[0].get("Part", [{}])[0]
        kinds = {s.get("streamType"): s for s in part.get("Stream", [])}
        check("sidecar: an MPEG-TS client gets it burned, the picture converted for it",
              kinds.get(3, {}).get("decision") == "burn" and kinds.get(3, {}).get("burn") == "1"
              and kinds.get(1, {}).get("decision") == "transcode",
              "/".join(str(kinds.get(k, {}).get("decision")) for k in (1, 2, 3)))
        get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("10", "s-side-ts", directStream="1"))
        status, _, first = get(port, "/video/:/transcode/universal/session/s-side-ts/base/00000.ts")
        segment = os.path.join(scratch, "side-ts-0.ts")
        open(segment, "wb").write(first)
        centre = (x + w // 2 - 10, y + h // 2 - 10, 20, 20)
        burned = luma(segment, 0, centre) if status == 200 and first[:1] == b"\x47" else -1
        source = luma(plain, 0, centre)
        check("sidecar: its white box is in the converted picture",
              burned > 200 and abs(burned - source) > 30, "luma %.0f, the source's %.0f" % (burned, source))
        get(port, "/video/:/transcode/universal/stop", [("session", "s-side-ts")])
        # An embedded text subtitle was dropped for every client not given Matroska. The
        # reference burns an SRT for the iOS profile's MPEG-TS (`subtitles_s1422.json`); rex
        # burns it for Plex Web too, reading the track through the release's Cues.
        for key, label in (("9", "s-srt-ts"), ("11", "s-ass-ts"), ("12", "s-mov-ts")):
            status, container = decision(port, key, label, directStream="1", subtitles="auto")
            part = container.get("Metadata", [{}])[0].get("Media", [{}])[0].get("Part", [{}])[0]
            kinds = {s.get("streamType"): s for s in part.get("Stream", [])}
            check("%s: Plex Web gets the embedded text subtitle burned, the picture converted for it" % label,
                  kinds.get(3, {}).get("decision") == "burn" and kinds.get(3, {}).get("burn") == "1"
                  and kinds.get(1, {}).get("decision") == "transcode",
                  "/".join(str(kinds.get(k, {}).get("decision")) for k in (1, 2, 3)))
        get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("11", "s-ass-ts", directStream="1", subtitles="auto"))
        status, _, first = get(port, "/video/:/transcode/universal/session/s-ass-ts/base/00000.ts")
        segment = os.path.join(scratch, "ass-ts-0.ts")
        open(segment, "wb").write(first)
        burned = luma(segment, 0, centre) if status == 200 and first[:1] == b"\x47" else -1
        source = luma(plain, 0, centre)
        check("s-ass-ts: the embedded track's white box is in the converted picture",
              burned > 200 and abs(burned - source) > 30, "luma %.0f, the source's %.0f" % (burned, source))
        check("s-ass-ts: the track was read through its Cues",
              wait_for_log(log, r"s-ass-ts subtitle stream \d+: (1) cues read") == "1", "")
        # One stage read is one run: the reader restarted the run once more after its last stage.
        get(port, "/video/:/transcode/universal/stop", [("session", "s-ass-ts")])
        runs = re.findall(r"TRANSCODE s-ass-ts run (\d+) from", open(log).read())
        check("s-ass-ts: a track read in one stage starts one run", runs == ["1"], str(runs))
        # An MP4's timed text is read through its sample tables the same way.
        get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("12", "s-mov-ts", directStream="1", subtitles="auto"))
        status, _, first = get(port, "/video/:/transcode/universal/session/s-mov-ts/base/00000.ts")
        check("s-mov-ts: the MP4's timed text was read through its sample tables",
              status == 200 and wait_for_log(log, r"s-mov-ts subtitle stream \d+: (1) of 1 cues") == "1", str(status))
        get(port, "/video/:/transcode/universal/stop", [("session", "s-mov-ts")])
        # A seek past what had been read: the run from there converted the rest of the title
        # before its lines were read and never burned them (Laid-Back Camp on rex-fun). The run
        # now holds where its file stops knowing the lines, and the reader reads from the seek.
        decision(port, "13", "s-lag", directStream="1", subtitles="auto")
        get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("13", "s-lag", directStream="1", subtitles="auto"))
        base = "/video/:/transcode/universal/session/s-lag/base/"
        status, _, first = get(port, base + "00000.ts")
        check("s-lag: the first segment comes once the first minute is read", status == 200, str(status))
        status, _, late = get(port, base + "00142.ts")
        segment = os.path.join(scratch, "lag-142.ts")
        open(segment, "wb").write(late)
        centre = (x + w // 2 - 10, y + h // 2 - 10, 20, 20)
        burned = luma(segment, 0, centre) if status == 200 and late[:1] == b"\x47" else -1
        check("s-lag: a segment past what was read when it was asked for carries its line",
              burned > 230, "luma %.0f (%s)" % (burned, status))
        get(port, "/video/:/transcode/universal/stop", [("session", "s-lag")])
        # A player waiting on lines that read slower than they play is kept waiting a few
        # seconds, not until they are all read: an opening song's karaoke held the player 51 s
        # and its request timed out (Laid-Back Camp on the zen rig).
        decision(port, "15", "s-dense", directStream="1", subtitles="auto")
        get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("15", "s-dense", directStream="1", subtitles="auto"))
        waits = []
        for index in range(60):
            begun = time.time()
            status, _, _ = get(port, "/video/:/transcode/universal/session/s-dense/base/%05d.ts" % index)
            waits.append((round(time.time() - begun, 1), index, status))
        worst = max(waits)
        check("s-dense: a player is not held long on lines read slower than they play",
              worst[0] < 20 and all(status == 200 for _, _, status in waits), "longest wait %ss at segment %d (%s)" % worst)
        get(port, "/video/:/transcode/universal/stop", [("session", "s-dense")])
        # Two sessions of one title read its lines out of one budget: each with its own burst,
        # a Real-Debrid file's reads were reset and the file held off (the zen rig, 2026-10-19).
        for label in ("s-burst-a", "s-burst-b"):
            decision(port, "14", label, directStream="1", subtitles="auto")
            get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("14", label, directStream="1", subtitles="auto"))
        time.sleep(3)
        reads = len(SMALL_READS.get("burstsubs.mkv", []))
        check("s-burst: two sessions of one title share its read budget", reads <= 110, "%d small reads in 3 s" % reads)
        for label in ("s-burst-a", "s-burst-b"):
            get(port, "/video/:/transcode/universal/stop", [("session", label)])
        walk(port, scratch, "capped", "1", "transcode", "copy", (640, 360),
             videoResolution="640x360", maxVideoBitrate="1000", directStream="1")
        # Audio only: the video is copied and the DTS the client refuses becomes AAC.
        walk(port, scratch, "audio-only", "5", "copy", "transcode", None, directStream="1", directStreamAudio="1")
        # Exercise the profile-supported non-AAC encoders through real client target negotiation.
        profile_audio_encoders(port, scratch)
        # A selected image subtitle cannot reach the client inside a conversion's
        # MPEG-TS: it is burned into the picture, which converts the video even where the
        # codec alone would be copied. The decision's shape is not measured (no recording).
        status, container = decision(port, "6", "s-subs")
        part = container.get("Metadata", [{}])[0].get("Media", [{}])[0].get("Part", [{}])[0]
        kinds = {s.get("streamType"): s for s in part.get("Stream", [])}
        check("subs: the selected PGS subtitle is burned, the video converted for it",
              kinds.get(3, {}).get("decision") == "burn" and kinds.get(1, {}).get("decision") == "transcode"
              and kinds.get(2, {}).get("decision") == "copy",
              "/".join(str(kinds.get(k, {}).get("decision")) for k in (1, 2, 3)))
        get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("6", "s-subs"))
        status, _, first = get(port, "/video/:/transcode/universal/session/s-subs/base/00000.ts")
        segment = os.path.join(scratch, "subs-0.ts")
        open(segment, "wb").write(first)
        x, y, w, h = BOX
        centre = (x + w // 2 - 10, y + h // 2 - 10, 20, 20)
        # FFmpeg 8.1.2 can composite the PTS-0 cue one frame after the PTS-0 video,
        # unlike 4.4.2 (fixtures/transcode/pgs_onset_ffmpeg.json: 47/48 burned frames).
        # Require every frame in a bounded stable interval (0.25-1 s of the 0-30 s cue),
        # not the scheduling-dependent first frame, nor merely some nonempty output.
        burned = frame_lumas(segment, centre, 24) if status == 200 and first[:1] == b"\x47" else []
        source = frame_lumas(plain, centre, 24)
        stable = list(zip(burned[6:24], source[6:24]))
        check("subs: the white box is visible throughout the stable active-cue interval",
              len(stable) == 18 and all(value > 230 and value - original > 30 for value, original in stable),
              "burned %s, source %s" % ([round(v, 1) for v in burned], [round(v, 1) for v in source]))
        get(port, "/video/:/transcode/universal/stop", [("session", "s-subs")])
        # The same box over HDR10 (tonemapped): where the device overlays with alpha (the
        # probe's alpha_subtitles, VAAPI), the run stays on it, decode to encode.
        device = wait_for_log(log, r"hardware (\S+) ") or "none"
        # ... and decodes the clip's 10-bit HEVC there (zen's Skylake does not, fun's J4105 does).
        overlays = device == "vaapi" and wait_for_log(log, r"alpha_subtitles=(true|false)") == "true" \
            and wait_for_log(log, r"opencl_tonemap=(true|false)") == "true" \
            and "hevc10" in (wait_for_log(log, r"decode=\[([^\]]*)\]") or "").split(",")
        status, container = decision(port, "20", "s-hdrsubs")
        if container.get("generalDecisionCode") == 1001:
            get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("20", "s-hdrsubs"))
            status, _, first = get(port, "/video/:/transcode/universal/session/s-hdrsubs/base/00000.ts")
            segment = os.path.join(scratch, "hdrsubs-0.ts")
            open(segment, "wb").write(first)
            burned = frame_lumas(segment, centre, 24) if status == 200 and first[:1] == b"\x47" else []
            check("hdrsubs: the box is burned into the tonemapped HDR picture",
                  len(burned) == 24 and all(value > 200 for value in burned[6:24]), [round(v, 1) for v in burned])
            ran = wait_for_log(log, r"TRANSCODE s-hdrsubs run 1 from segment 0 on (\w+)")
            check("hdrsubs: it runs on the full device path where the device overlays (%s)" % device,
                  ran == "Full" if overlays else ran in ("EncodeOnly", "Software"), str(ran))
            get(port, "/video/:/transcode/universal/stop", [("session", "s-hdrsubs")])
        else:
            tonemaps = wait_for_log(log, r"tonemapx=(true|false)") == "true" or "no HDR is (no zscale)" not in open(log).read()
            check("hdrsubs: without tonemapping the HDR conversion is refused", not tonemaps and not overlays,
                  str(container.get("generalDecisionCode")))
        # HDR is tonemapped to SDR BT.709: the output segment says so. Dolby Vision profile
        # 5 (no HDR10 base; a real PMS refuses it, 2003) converts too when tonemapx can apply
        # its RPU, and keeps the refusal when it cannot: untonemapped it is green.
        probed = wait_for_log(log, r"tonemapx=(true|false)")
        tonemapx = probed == "true"
        for key, label in (("7", "hdr10"), ("8", "dvp5")):
            status, container = decision(port, key, "s-" + label)
            if label == "dvp5" and not tonemapx:
                check("dvp5: without tonemapx profile 5 keeps the reference's refusal",
                      container.get("transcodeDecisionCode") == 2003, str(container.get("transcodeDecisionCode")))
                continue
            check("%s: decision converts the video" % label,
                  container.get("generalDecisionCode") == 1001, "%s %s" % (status, container.get("transcodeDecisionText")))
            get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query(key, "s-" + label))
            status, _, first = get(port, "/video/:/transcode/universal/session/s-%s/base/00000.ts" % label)
            segment = os.path.join(scratch, "%s-0.ts" % label)
            open(segment, "wb").write(first)
            tags = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                   "stream=color_transfer,color_primaries", "-of", "csv=p=0", segment],
                                  capture_output=True, text=True).stdout.split("\n")[0].strip() if status == 200 else str(status)
            check("%s: the converted picture is %s" % (label, "tonemapped to BT.709" if tonemapx else "converted untonemapped"),
                  tags == ("bt709,bt709" if tonemapx else "smpte2084,bt2020"), tags)
            get(port, "/video/:/transcode/universal/stop", [("session", "s-" + label)])
        # The bundled /web client converts everything over DASH (directStream=0).
        walk_dash(port, "dash-convert", "3", "transcode", "transcode", (1280, 720))
        walk_dash(port, "dash-remux", "1", "copy", "copy", None, directStream="1", directStreamAudio="1")
        # MPEG-TS h264/aac copied into DASH's MP4: FFmpeg refused the TS codec tags
        # ("Tag [27][0][0][0] incompatible"), so every segment was a 500 (rex-fun, YouTube HLS).
        walk_dash(port, "dash-remux-ts", "16", "copy", "copy", None, directStream="1", directStreamAudio="1")
        # Plex Web re-fetches both headers on every seek. Once playback had passed the
        # 120 s kept behind the player, the run's first fragment was gone and the header
        # waited on it forever: the seek hung with the buffer empty.
        session = "d-long"
        get(port, "/video/:/transcode/universal/decision", dash_query("4", session))
        get(port, "/video/:/transcode/universal/start.mpd", dash_query("4", session))
        base = "/video/:/transcode/universal/session/%s/" % session
        # 70 segments of 2 s: 140 s, past the 120 s kept behind the player.
        for index in range(0, 70):
            for rep_id in (0, 1):
                get(port, base + "%d/%d.m4s" % (rep_id, index))
        began = time.time()
        status, _, init = get(port, base + "0/header", timeout=20)
        check("dash-long: the header still answers after playing past the kept window",
              status == 200 and [kind for kind, _ in boxes(init)] == ["ftyp", "moov"] and time.time() - began < 3,
              "%s in %.2fs" % (status, time.time() - began))
        get(port, "/video/:/transcode/universal/stop", [("session", session)])
        # Right after a seek the player still has a request out for the OLD position.
        # Each restarting FFmpeg at its own segment in turn was a loop (3 531 restarts in
        # seconds against Plex Web on the 1.42.2 reference); the newest request wins, the older
        # gives up.
        # Five times: on the Windows runner the older seek's launch landed after the newer's and
        # replaced it, leaving the newest request waiting on a run that was gone, in one run of
        # two (CI 2026-10-18).
        for attempt in range(5):
            session = "s-race-%d" % attempt
            get(port, "/video/:/transcode/universal/decision", plex_web_query("4", session))
            get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("4", session))
            base = "/video/:/transcode/universal/session/%s/base/" % session
            get(port, base + "00000.ts")
            answers = {}
            old = threading.Thread(target=lambda: answers.__setitem__("old", get(port, base + "00140.ts", timeout=30)[0]))
            old.start()
            time.sleep(0.05)
            status, _, seeked = get(port, base + "00060.ts", timeout=30)
            old.join()
            restarts = sum(1 for line in open(log) if "TRANSCODE %s seek to" % session in line)
            check("race %d: the newest request is served after at most two restarts" % attempt,
                  status == 200 and seeked[:1] == b"\x47" and restarts <= 2,
                  "%s, %d restarts, the old request answered %s" % (status, restarts, answers.get("old")))
            get(port, "/video/:/transcode/universal/stop", [("session", session)])
        status, _, body = get(port, "/transcode/sessions")
        check("every session is gone after stop", json.loads(body)["MediaContainer"].get("size") == 0, "")
        # "Maximum simultaneous CPU transcodes": past the cap a second video encode is refused,
        # a remux is not counted, and the slot comes back when the first conversion stops.
        get(port, "/:/prefs", [("_CPU-TranscodeCountLimit", "1")], method="PUT")
        _, first = decision(port, "3", "s-cap-a")
        status, _, _ = get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("3", "s-cap-a"))
        _, second = decision(port, "3", "s-cap-b")
        check("cap 1: a second video conversion is refused while one runs",
              first.get("generalDecisionCode") == 1001 and status == 200
              and second.get("generalDecisionCode") == 2000 and second.get("transcodeDecisionCode") == 4005,
              "%s %s %s" % (first.get("generalDecisionCode"), status, second.get("generalDecisionCode")))
        _, again = decision(port, "3", "s-cap-a")
        check("cap 1: the running conversion's own session still converts (a seek)",
              again.get("generalDecisionCode") == 1001, str(again.get("generalDecisionCode")))
        _, remux = decision(port, "1", "s-cap-r", directStream="1")
        check("cap 1: a remux is not counted against the cap",
              remux.get("generalDecisionCode") == 1001, str(remux.get("generalDecisionCode")))
        get(port, "/video/:/transcode/universal/stop", [("session", "s-cap-a")])
        _, freed = decision(port, "3", "s-cap-b")
        check("cap 1: once the first stops, the next conversion starts",
              freed.get("generalDecisionCode") == 1001, str(freed.get("generalDecisionCode")))
        get(port, "/:/prefs", [("_CPU-TranscodeCountLimit", "0")], method="PUT")
        if sys.platform.startswith("linux"):
            # A conversion must not outlive the server: a killed rex once left FFmpeg
            # reading from zurg for an hour into a deleted directory.
            get(port, "/video/:/transcode/universal/decision", plex_web_query("4", "s-orphan"))
            get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("4", "s-orphan"))
            get(port, "/video/:/transcode/universal/session/s-orphan/base/00000.ts")
            marker = os.path.join(scratch, "segments-%d" % port)
            def ffmpegs():
                found = []
                for pid in os.listdir("/proc"):
                    try:
                        line = open("/proc/%s/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode("utf-8", "replace")
                    except OSError:
                        continue
                    if "ffmpeg" in line and marker in line:
                        found.append(pid)
                return found
            before = ffmpegs()
            process.kill()
            process.wait()
            time.sleep(1.5)
            check("a killed server takes its FFmpeg with it", before and not ffmpegs(), "%s -> %s" % (before, ffmpegs()))
        elif sys.platform == "darwin":
            # No parent-death signal here: a stopped rex stops its FFmpegs itself, or they run on
            # into the deleted directory (docs/setup.md, "Transcoding").
            get(port, "/video/:/transcode/universal/decision", plex_web_query("4", "s-orphan"))
            get(port, "/video/:/transcode/universal/start.m3u8", plex_web_query("4", "s-orphan"))
            get(port, "/video/:/transcode/universal/session/s-orphan/base/00000.ts")
            marker = os.path.join(scratch, "segments-%d" % port)
            def ffmpegs():
                listed = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
                return [line.split()[0] for line in listed.splitlines() if "ffmpeg" in line and marker in line]
            before = ffmpegs()
            process.terminate()
            process.wait()
            time.sleep(1.5)
            check("a stopped server stops its FFmpeg", before and not ffmpegs(), "%s -> %s" % (before, ffmpegs()))
    finally:
        process.terminate()
        process.wait()
    failed = [label for label, ok in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    if failed:
        # The scratch directory goes with the run (REX_KEEP_TMP=1 keeps it): show what rex said.
        print("rex log (last 150 lines; REX_KEEP_TMP=1 keeps %s):" % log)
        with open(log, encoding="utf-8", errors="replace") as handle:
            print("".join(handle.readlines()[-150:]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
