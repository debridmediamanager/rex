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

ROOT = os.environ.get("REX_TEST_ROOT",
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FFMPEG = os.environ.get("REX_TRANSCODER") or shutil.which("ffmpeg")
FFPROBE = os.path.join(os.path.dirname(FFMPEG), "ffprobe") if FFMPEG else None
if FFPROBE and not os.path.exists(FFPROBE):
    FFPROBE = shutil.which("ffprobe")
DURATION = 30

results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print("  %s  %-62s %s" % ("PASS" if ok else "FAIL", label, detail))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


MEDIA = {}
# A clip zurg serves at this many bytes a second: the run outlasts the first segment.
SLOW_RATE = 2_000_000


class Zurg(http.server.BaseHTTPRequestHandler):
    """zurg's WebDAV file read over the generated clips."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        name = urllib.parse.unquote(self.path).rsplit("/", 1)[-1]
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


def make_clip(path, video, audio, duration, size="1280x720"):
    subprocess.run([FFMPEG, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=%s:r=24:d=%d" % (size, duration),
                    "-f", "lavfi", "-i", "sine=f=440:d=%d" % duration,
                    *video, *audio, "-shortest", path], check=True)


def probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def row(key, title, file, streams, size, duration):
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
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
                             ]}]}],
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
    environment = dict(os.environ, ZURG_URL="http://127.0.0.1:%d" % zurg_port,
                       REX_TRANSCODE_HW=os.environ.get("REX_TRANSCODE_HW", "none"),
                       REX_TRANSCODE_DIR=os.path.join(scratch, "segments-%d" % port), **env)
    environment.pop("REX_TRANSCODER", None)
    if transcoder:
        environment["REX_TRANSCODER"] = FFMPEG
    log = open(os.path.join(scratch, "rex-%d.log" % port), "w")
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", catalog, "--assert-claimed", "--zurg-mount", "/zurg", root=ROOT),
        cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(160):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=2)
            break
        except Exception:
            time.sleep(0.25)
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


def decision(port, key, session, **extra):
    status, _, body = get(port, "/video/:/transcode/universal/decision", plex_web_query(key, session, **extra))
    return status, json.loads(body)["MediaContainer"] if status == 200 else {}


def walk(port, scratch, label, key, expect_video, expect_audio, size, duration=DURATION, **extra):
    session = "s-" + label
    status, container = decision(port, key, session, **extra)
    check("%s: decision is Conversion OK (1001)" % label,
          status == 200 and container.get("generalDecisionCode") == 1001
          and container.get("transcodeDecisionCode") == 1001
          and container.get("directPlayDecisionCode") == 3000,
          "%s %s" % (status, container.get("generalDecisionCode")))
    media = container.get("Metadata", [{}])[0].get("Media", [{}])[0]
    part = media.get("Part", [{}])[0]
    streams = {s.get("streamType"): s for s in part.get("Stream", [])}
    check("%s: decision Media is the mpegts/hls output" % label,
          media.get("container") == "mpegts" and media.get("protocol") == "hls"
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
          and names[0] == "00000.ts", "%d segments" % len(names))
    length = int(re.search(r"TARGETDURATION:(\d+)", playlist).group(1)) if status == 200 else 4
    base = "/video/:/transcode/universal/session/%s/base/" % session
    status, headers, first = get(port, base + "00000.ts")
    ttfs = time.time() - started
    check("%s: first segment is MPEG-TS" % label,
          status == 200 and headers.get("Content-Type") == "video/MP2T" and first[:1] == b"\x47",
          "%s %d bytes, %.2fs from start.m3u8" % (status, len(first), ttfs))
    path = os.path.join(scratch, "%s-0.ts" % label)
    open(path, "wb").write(first)
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
    open(path1, "wb").write(second)
    start1 = float(probe(path1)["format"].get("start_time", -1)) if status == 200 else -1
    check("%s: segment 1 starts where segment 0 ends" % label,
          status == 200 and abs(start1 - start0 - length) < 1.6, "offset %.3f, length %d" % (start1 - start0, length))
    far = len(names) - 2
    status, _, tail = get(port, base + "%05d.ts" % far)
    path_far = os.path.join(scratch, "%s-far.ts" % label)
    open(path_far, "wb").write(tail)
    start_far = float(probe(path_far)["format"].get("start_time", -1)) if status == 200 else -1
    check("%s: a far seek (segment %d) restarts at its own time" % (label, far),
          status == 200 and tail[:1] == b"\x47" and abs(start_far - start0 - far * length) < 1.6,
          "offset %.3f, want %d" % (start_far - start0, far * length))

    status, _, body = get(port, "/transcode/sessions")
    rows = json.loads(body)["MediaContainer"].get("TranscodeSession", [])
    mine = [r for r in rows if r.get("key") == session]
    check("%s: /transcode/sessions lists it" % label,
          len(mine) == 1 and mine[0].get("videoDecision") == expect_video
          and mine[0].get("protocol") == "hls" and mine[0].get("container") == "mpegts",
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
          and row_.get("Media", [{}])[0].get("container") == "mpegts", "")
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


def main():
    if not FFMPEG or not FFPROBE:
        print("SKIP: no ffmpeg (set REX_TRANSCODER)")
        return 0
    scratch = tempfile.mkdtemp(prefix="rex-transcode-test-")
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
    }
    items = []
    for number, (name, (video, audio, duration)) in enumerate(clips.items(), start=1):
        path = os.path.join(scratch, name)
        make_clip(path, video, audio, duration, "320x180" if name == "long.mkv" else "1280x720")
        MEDIA[name] = path
        items.append(row(str(number), name.split(".")[0].title(), name, probe(path)["streams"], os.path.getsize(path), duration))
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
        # A segment is served as soon as FFmpeg moves on to the next one, not when the
        # run ends: FFmpeg buffers its own segment list until then, which made the
        # first segment of a slow source wait for the whole file.
        whole = os.path.getsize(MEDIA["slow.mkv"]) / SLOW_RATE
        ttfs = walk(port, scratch, "slow", "2", "copy", "copy", None, duration=90)
        check("slow: the first segment comes while the run is still reading",
              ttfs < whole * 0.6, "%.2fs, the whole file takes %.1fs" % (ttfs, whole))
        walk(port, scratch, "convert", "3", "transcode", "transcode", (1280, 720))
        walk(port, scratch, "capped", "1", "transcode", "copy", (640, 360),
             videoResolution="640x360", maxVideoBitrate="1000", directStream="1")
        # The bundled /web client converts everything over DASH (directStream=0).
        walk_dash(port, "dash-convert", "3", "transcode", "transcode", (1280, 720))
        walk_dash(port, "dash-remux", "1", "copy", "copy", None, directStream="1", directStreamAudio="1")
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
        session = "s-race"
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
        check("race: the newest request is served after at most two restarts",
              status == 200 and seeked[:1] == b"\x47" and restarts <= 2,
              "%s, %d restarts, the old request answered %s" % (status, restarts, answers.get("old")))
        get(port, "/video/:/transcode/universal/stop", [("session", session)])
        status, _, body = get(port, "/transcode/sessions")
        check("every session is gone after stop", json.loads(body)["MediaContainer"].get("size") == 0, "")
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
    finally:
        process.terminate()
        process.wait()
    failed = [label for label, ok in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    if failed:
        print("rex log: %s" % log)
        # A CI runner's scratch directory is gone when the job ends: show what rex said.
        if os.environ.get("CI"):
            with open(log, encoding="utf-8", errors="replace") as handle:
                print("".join(handle.readlines()[-150:]))
        return 1
    shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
