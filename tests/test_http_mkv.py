#!/usr/bin/env python3
"""Every client profile's streaming protocol converts, and Plex Desktop's Matroska stream plays.

Plex for Mac 1.115 (profile `Plex Desktop`) asks for a conversion with `protocol=http` and plays
what `/video/:/transcode/universal/start` (no extension) answers: one Matroska stream, chunked as
it is made. rex had no such protocol and refused every conversion it asked for -- "Cannot convert
this item. No conversion profile found for protocol http." -- so a live channel, which only a
conversion reads, never played (rex-fun, 2026-10-26). plexlab's answers are
`tests/fixtures/transcode/http_mkv_s1434.json`.

    guard   every streaming target of every profile in data/client_profiles.json: the decision
            for an item that must be converted (a live HLS channel, a file over the quality cap)
            is Conversion OK, and start serves media FFmpeg decodes, for each distinct target.
            A profile naming a protocol rex does not serve fails here, by name. A live channel
            over DASH is a dynamic MPD a reader goes on reading.
    http    live channels (H.264/AAC, 1080i, HEVC/MP2, AC3 5.1, audio only), files (mkv, mp4,
            HDR10 tonemapped, a text and an image subtitle, a second audio track), a seek either
            clock, a stop and a disconnect ending FFmpeg, the session's listing and ping.

    python3 tests/test_http_mkv.py      # REX_TRANSCODER=/path/to/ffmpeg optional
"""
import calendar
import http.client
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

import test_native_transcode as nt
from test_native_transcode import FFMPEG, FFPROBE, MEDIA, check, get, make_clip, probe, row

ROOT = nt.ROOT
DURATION = 30
PROFILES = json.load(open(os.path.join(ROOT, "data/client_profiles.json")))
RECORDED = json.load(open(os.path.join(ROOT, "tests/fixtures/transcode/http_mkv_s1434.json")))
# The profiles whose streaming targets name no protocol (an MPEG-TS or ASF file, no `protocol`
# attribute): PMS's DLNA renderer profiles, which its DLNA server streams to. rex runs no DLNA
# server, so no request of theirs reaches the universal transcoder. Listed by name so that a new
# one is a failure to look at, not a silent gap.
NO_PROTOCOL = {
    "Asus Transformer Prime TF201", "Panasonic Viera 3D TV", "PlayStation 3", "Roku-5000X-6.0",
    "RokuDLNA-3.1", "RokuDLNA-4400X-7.x", "RokuDLNA-5.1", "RokuDLNA-5000X-6.x", "RokuDLNA-5000X-7.x",
    "RokuDLNA-6.x", "RokuDLNA-6000X-7.x", "Samsung SmartTV", "Sony Blu-ray", "Sony Blu-ray 2013",
    "Sony Bravia 2010", "Sony Bravia 2011", "Sony Bravia 2012", "Sony Bravia 2013", "Sony Streaming Player",
    "Western Digital TV Live", "Xbox 360",
}
SERVED = {"hls", "dash", "http"}

# Each live channel's segments, by its playlist's name; each moves on in real time from its
# first read, as nt.live_playlist's does.
CHANNELS = {}


class Quiet(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # FFmpeg hangs up mid-read whenever a stream ends; that is no failure


# A `stall` file's source sends this much of it, then nothing, holding the connection open, as a
# debrid CDN that stops mid-file does.
STALL_AFTER = 400_000


class Source(nt.Zurg):
    def do_GET(self):
        name = urllib.parse.unquote(self.path).rsplit("/", 1)[-1]
        if name.startswith("stall") and name in MEDIA:
            size = os.path.getsize(MEDIA[name])
            self.send_response(200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(MEDIA[name], "rb") as handle:
                self.wfile.write(handle.read(STALL_AFTER))
            self.wfile.flush()
            time.sleep(600)
            return
        channel = CHANNELS.get(name)
        if channel is None:
            return super().do_GET()
        channel["since"] = channel["since"] or time.time()
        # Three segments on the air at the first read, then each as its own length passes.
        lengths = channel["lengths"]
        made, clock = 3, time.time() - channel["since"]
        # A channel that stops publishing for a while, then is back at its live edge.
        frozen = channel.get("freeze")
        if frozen and frozen[0] <= clock < frozen[1]:
            clock = frozen[0]
        while made < len(lengths) and clock >= sum(lengths[3:made + 1]):
            made += 1
        first = max(0, made - 5)
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:%d" % -(-max(lengths) // 1),
                 "#EXT-X-MEDIA-SEQUENCE:%d" % first]
        for segment, length in zip(channel["segments"][first:made], lengths[first:made]):
            lines += ["#EXTINF:%.6f," % length, segment]
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def desktop_query(key, session, **extra):
    """Plex for Mac's own request, as recorded (`http_mkv_s1434.json`), for item `key`."""
    pairs = []
    for name, value in RECORDED["exchanges"][0]["query"]:
        if name in ("maxVideoBitrate", "videoResolution"):
            continue
        value = {"path": "/library/metadata/%s" % key, "session": session,
                 "X-Plex-Session-Identifier": "playback-" + session}.get(name, value)
        pairs.append((name, extra.pop(name, value)))
    pairs.extend(extra.items())
    return pairs


def profile_query(profile, protocol, key, session, **extra):
    """Plex Web's recorded conversion request, as `profile` asking over `protocol`."""
    return nt.plex_web_query(key, session, protocol=protocol, **{"X-Plex-Client-Profile-Name": profile}, **extra)


def decide(port, query):
    status, _, body = get(port, "/video/:/transcode/universal/decision", query)
    return json.loads(body)["MediaContainer"] if status == 200 else {"status": status}


def read_stream(port, query, limit, seconds, path, silence=15):
    """GET start, keep at most `limit` bytes or `seconds` of them, and hang up. Answers the
    status, headers and the bytes written to `path`. A live source that stops publishing (the
    fixture channels end after 60 s) stops the stream, as a real one does: 15 s of silence ends it."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=silence)
    connection.request("GET", "/video/:/transcode/universal/start?" + urllib.parse.urlencode(query))
    response = connection.getresponse()
    headers = {name.lower(): value for name, value in response.getheaders()}
    data = b""
    deadline = time.time() + seconds
    while len(data) < limit and time.time() < deadline:
        try:
            chunk = response.read1(65536) if response.status == 200 else response.read()
        except TimeoutError:
            break
        if not chunk:
            break
        data += chunk
    connection.close()
    open(path, "wb").write(data)
    return response.status, headers, data


def streams_of(path):
    try:
        return probe(path)
    except subprocess.CalledProcessError:
        return {"streams": [], "format": {}}


def decodes(path, kind="v"):
    """Whether FFmpeg decodes a frame of `kind` out of `path`: a 200 of an error page is no media."""
    done = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-map", "0:%s:0" % kind, "-frames:%s" % kind, "1" if kind == "v" else "10",
                           "-f", "null", "-"], capture_output=True, text=True)
    return done.returncode == 0


def last_pts(path):
    """A streamed Matroska has no duration in its header (plexlab's neither): its last packet's time."""
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split()
    times = [float(value) for value in out if value not in ("", "N/A")]
    return max(times) if times else 0.0


def first_pts(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                          "-read_intervals", "%+#1", "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split()
    return float(out[0]) if out else None


def processes():
    """Every process's id and command line. `tasklist` shows no command lines, so Windows asks CIM."""
    if os.name == "nt":
        listed = subprocess.run(["powershell", "-NoProfile", "-Command",
                                 "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
                                 "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"],
                                capture_output=True, text=True).stdout
    else:
        listed = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    return [line.strip() for line in listed.splitlines() if line.strip()]


def ffmpegs_reading(name):
    return [line.split()[0] for line in processes() if "ffmpeg" in line.lower() and "/" + name in line and "-f matroska" in line]


def gone(name, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not ffmpegs_reading(name):
            return True
        time.sleep(0.3)
    return False


def segment_bytes(port, protocol, query, session, scratch, label):
    """The first media a protocol's start leads to, as one decodable file."""
    path = os.path.join(scratch, "%s.bin" % label)
    if protocol == "http":
        status, headers, data = read_stream(port, query, 1_500_000, 20, path)
        return status == 200 and headers.get("content-type") == "video/x-matroska", path
    if protocol == "hls":
        status, _, master = get(port, "/video/:/transcode/universal/start.m3u8", query)
        base = "/video/:/transcode/universal/session/%s/base/" % session
        status_i, _, index = get(port, base + "index.m3u8")
        header = get(port, base + "header")[2] if b'EXT-X-MAP' in index else b""
        status_s, _, first = get(port, base + "00000.ts")
        open(path, "wb").write(header + first)
        return status == status_i == status_s == 200, path
    status, _, mpd = get(port, "/video/:/transcode/universal/start.mpd", query)
    base = "/video/:/transcode/universal/session/%s/0/" % session
    status_h, _, header = get(port, base + "header")
    status_s, _, first = get(port, base + "%d.m4s" % (first_segment(mpd.decode("utf-8", "replace")) if status == 200 else 0))
    open(path, "wb").write(header + first)
    return status == status_h == status_s == 200, path


# beIN Sports 7 (MENA)'s keyframe times over a minute, then on every 5 s.
BEIN_KEYFRAMES = [0, 5, 10, 15, 20, 22.88, 25.28] + [25.28 + 5 * n for n in range(1, 20)]


def make_channel(scratch, name, video, audio, size="320x180", rate=24, extra=(), seconds=120, maps=()):
    """A clip cut into a live channel's 2 s MPEG-TS segments, served as `<name>.m3u8`."""
    clip = os.path.join(scratch, name + ".ts")
    inputs = ["-f", "lavfi", "-i", "testsrc2=s=%s:r=%d:d=%d" % (size, rate, seconds)] if video else []
    inputs += ["-f", "lavfi", "-i", "sine=f=440:d=%d" % seconds]
    subprocess.run([FFMPEG, "-v", "error", "-y", *inputs, *maps, *video, *audio, *extra, "-shortest", "-f", "mpegts", clip], check=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", clip, "-map", "0", "-c", "copy", "-f", "hls", "-hls_time", "2",
                    "-hls_list_size", "0", "-hls_segment_filename", os.path.join(scratch, name + "%03d.ts"),
                    os.path.join(scratch, name + "-vod.m3u8")], check=True)
    segments = sorted(entry for entry in os.listdir(scratch) if re.fullmatch(re.escape(name) + r"\d{3}\.ts", entry))
    for segment in segments:
        MEDIA[segment] = os.path.join(scratch, segment)
    # Each segment's own length: a source cut on its keyframes has segments as long as its GOP.
    vod = open(os.path.join(scratch, name + "-vod.m3u8")).read()
    lengths = [float(value) for value in re.findall(r"#EXTINF:([\d.]+),", vod)]
    CHANNELS[name + ".m3u8"] = {"segments": segments, "lengths": lengths, "since": None}
    # The streams as read off the frames, as rex-live-sports probes a channel since 6758680: a
    # playlist's own probe leaves an interlaced picture's field_order unknown.
    return probe(clip)["streams"]


def live_row(key, name, streams):
    if any(stream["codec_type"] == "video" for stream in streams):
        item = row(key, name.title(), name + ".m3u8", streams, 0, 0)
    else:
        # A radio channel: a row with its one audio stream.
        item = row(key, name.title(), name + ".m3u8", [{"codec_type": "video", "codec_name": "h264", "index": -1}] + streams, 0, 0)
        part = item["Media"][0]["Part"][0]
        part["Stream"] = [stream for stream in part["Stream"] if stream["streamType"] != 1]
        for field in ("videoCodec", "width", "height", "videoResolution", "videoFrameRate", "aspectRatio"):
            item["Media"][0].pop(field, None)
    video = next((stream for stream in streams if stream["codec_type"] == "video"), None)
    if video:
        stream = item["Media"][0]["Part"][0]["Stream"][0]
        stream.update({"width": video["width"], "height": video["height"]})
        item["Media"][0].update({"width": video["width"], "height": video["height"]})
        if video.get("field_order") not in (None, "progressive", "unknown"):
            stream["scanType"] = "interlaced"
    item["Media"][0]["container"] = item["Media"][0]["Part"][0]["container"] = "hls"
    return item


def file_row(key, name, streams, container="mkv", **extra):
    item = row(key, name.split(".")[0].title(), name, streams, os.path.getsize(MEDIA[name]), DURATION, **extra)
    item["Media"][0]["container"] = item["Media"][0]["Part"][0]["container"] = container
    return item


def build(scratch):
    h264 = ["-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-pix_fmt", "yuv420p"]
    aac = ["-c:a", "aac", "-ac", "2"]
    items, channels = [], {}
    # The guard's channels, read by every profile in turn: on the air for its whole run. One is
    # field-coded 576i25, as many broadcast channels are.
    streams = make_channel(scratch, "live-guard", h264, aac, seconds=1200)
    items.append(live_row("100", "live-guard", streams))
    channels["live-guard"] = "100"
    streams = make_channel(scratch, "live-guard-i", ["-vf", "setfield=tff", "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
                                                     "-pix_fmt", "yuv420p", "-flags", "+ilme+ildct", "-x264-params", "tff=1"],
                           aac, "720x576", 25, ("-field_order", "tt"), seconds=1200)
    items.append(live_row("99", "live-guard-i", streams))
    channels["live-guard-i"] = "99"
    # Live channels, each a different broadcast shape, each read once.
    for key, name, video, audio, size, rate, extra in (
            ("101", "live-h264", h264, aac, "320x180", 24, ()),
            ("102", "live-1080i", ["-vf", "setfield=tff", "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p",
                                   "-flags", "+ilme+ildct", "-x264-params", "tff=1"], ["-c:a", "mp2", "-ac", "2"], "1920x1080", 25,
             ("-field_order", "tt")),
            ("103", "live-hevc", ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none:keyint=48",
                                  "-pix_fmt", "yuv420p"], ["-c:a", "mp2", "-ac", "1"], "320x180", 24, ()),
            ("104", "live-ac3", h264, ["-c:a", "ac3", "-ac", "6"], "320x180", 24, ()),
            ("105", "live-radio", [], aac, None, None, ()),
            # beIN Sports 7 (MENA)'s GOP, read off the real channel (2026-10-27): keyframes every
            # 5 s with two short gaps (20 -> 22.88 -> 25.28), so its source segments are cut there.
            ("106", "live-gop5", ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                                  "-x264-params", "keyint=1000:min-keyint=1000:scenecut=0",
                                  "-force_key_frames", ",".join("%.2f" % t for t in BEIN_KEYFRAMES)],
             ["-c:a", "aac", "-ac", "2", "-ar", "44100"], "320x180", 50, ())):
        streams = make_channel(scratch, name, video, audio, size or "320x180", rate or 24, extra)
        items.append(live_row(key, name, streams))
        channels[name] = key
    # Live channels whose streams changed under their probe (rex-fun, 2026-10-27): the row is the
    # day-old probe, the channel what it serves now.
    streams = make_channel(scratch, "live-aacnow", h264, aac)
    item = live_row("107", "live-aacnow", streams)
    audio = next(stream for stream in item["Media"][0]["Part"][0]["Stream"] if stream["streamType"] == 2)
    audio["codec"] = item["Media"][0]["audioCodec"] = "mp2"  # Sport 5 Gold's probe said MP2; it serves AAC
    items.append(item)
    channels["live-aacnow"] = "107"
    streams = make_channel(scratch, "live-audiofirst", h264, aac, maps=("-map", "1:a", "-map", "0:v"))
    item = live_row("108", "live-audiofirst", streams)
    part = item["Media"][0]["Part"][0]
    # Movistar Deportes' probe: one video stream at index 0, no audio; it now serves audio as 0.
    part["Stream"] = [dict(stream, index=0) for stream in part["Stream"] if stream["streamType"] == 1]
    item["Media"][0].pop("audioCodec", None)
    items.append(item)
    channels["live-audiofirst"] = "108"
    streams = make_channel(scratch, "live-hiccup", h264, aac, seconds=240)
    # Digi Sport 4 stopped for longer than a player waits on a segment (45 s); the channel did not.
    CHANNELS["live-hiccup.m3u8"]["freeze"] = (14, 74)
    items.append(live_row("109", "live-hiccup", streams))
    channels["live-hiccup"] = "109"
    # Digi Sport 4's GOP: keyframes 10 s apart (measured over 30 s, 2026-10-27), so its source
    # segments are 10 s long too; MP2 audio as its.
    streams = make_channel(scratch, "live-gop10", ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                                                   "-x264-params", "keyint=250:min-keyint=250:scenecut=0"],
                           ["-c:a", "mp2", "-ac", "2"], "320x180", 25, seconds=1200)
    items.append(live_row("112", "live-gop10", streams))
    channels["live-gop10"] = "112"
    # A channel with no audio at open while its probe promised AAC (Sport 5 Gold, 11:30 UTC).
    streams = make_channel(scratch, "live-noaudio", h264, ["-an"])
    item = live_row("110", "live-noaudio", [dict(streams[0], index=0), {"codec_type": "audio", "codec_name": "aac", "index": 1, "channels": 2}])
    items.append(item)
    channels["live-noaudio"] = "110"
    # ... and one whose audio comes back: 20 s with none, then audio and video. It holds on its
    # first segments with audio (as a source re-resolving does), so the run is started again.
    quiet = make_channel(scratch, "live-audioback-a", h264, ["-an"], seconds=20)
    loud = make_channel(scratch, "live-audioback-b", h264, aac, seconds=200)
    first, second = CHANNELS.pop("live-audioback-a.m3u8"), CHANNELS.pop("live-audioback-b.m3u8")
    CHANNELS["live-audioback.m3u8"] = {"segments": first["segments"] + second["segments"], "lengths": first["lengths"] + second["lengths"],
                                       "since": None, "freeze": (sum(first["lengths"]) + 6, sum(first["lengths"]) + 36)}
    items.append(live_row("111", "live-audioback", loud))
    channels["live-audioback"] = "111"
    # Files.
    files = {}
    for key, name, video, audio in (
            ("201", "h264.mkv", h264, aac),
            ("202", "slowstop.mkv", h264, aac),
            ("203", "plainmp4.mp4", h264, aac),
            ("208", "stall.mkv", h264, aac)):
        MEDIA[name] = os.path.join(scratch, name)
        make_clip(MEDIA[name], video, audio, 90 if name == "slowstop.mkv" else DURATION)
        items.append(file_row(key, name, probe(MEDIA[name])["streams"], container="mp4" if name.endswith(".mp4") else "mkv"))
        files[name] = key
    # The slow file is served at nt.SLOW_RATE only when its name starts with "slow".
    # HDR10: PQ-tagged 10-bit HEVC, tonemapped when converted.
    MEDIA["hdr10.mkv"] = os.path.join(scratch, "hdr10.mkv")
    make_clip(MEDIA["hdr10.mkv"], ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-x265-params",
                                   "log-level=none:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:hdr10=1:max-cll=1000,400"],
              aac, DURATION)
    item = file_row("204", "hdr10.mkv", probe(MEDIA["hdr10.mkv"])["streams"])
    item["Media"][0]["Part"][0]["Stream"][0].update({"bitDepth": 10, "colorTrc": "smpte2084"})
    items.append(item)
    files["hdr10.mkv"] = "204"
    # Subtitles: an embedded SRT (carried into the Matroska) and a PGS box (burned).
    plain = MEDIA["h264.mkv"]
    srt = os.path.join(scratch, "line.srt")
    open(srt, "w").write("1\n00:00:00,500 --> 00:00:29,000\nHello from the SRT\n")
    sup = os.path.join(scratch, "box.sup")
    nt.make_pgs(sup, 1280, 720, DURATION)
    for key, name, source, codec, flags in (("205", "srt.mkv", srt, "srt", ["-c:s", "srt"]), ("206", "pgs.mkv", sup, "pgs", [])):
        MEDIA[name] = os.path.join(scratch, name)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", plain, "-i", source, "-map", "0", "-map", "1", "-c", "copy", *flags,
                        MEDIA[name]], check=True)
        streams = probe(MEDIA[name])["streams"]
        items.append(file_row(key, name, streams, subtitle=next(s for s in streams if s["codec_type"] == "subtitle"),
                              subtitle_codec=codec))
        files[name] = key
    # An interlaced 1080i25 file (x264 field coding), its row's video `scanType` interlaced as
    # rex's mediainfo names a probe's `field_order` tt.
    MEDIA["interlaced.mkv"] = os.path.join(scratch, "interlaced.mkv")
    make_clip(MEDIA["interlaced.mkv"], ["-vf", "setfield=tff", "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p",
                                        "-flags", "+ilme+ildct", "-x264-params", "tff=1"], aac, DURATION, "1920x1080")
    item = file_row("209", "interlaced.mkv", probe(MEDIA["interlaced.mkv"])["streams"])
    item["Media"][0]["Part"][0]["Stream"][0].update({"scanType": "interlaced", "width": 1920, "height": 1080})
    item["Media"][0].update({"width": 1920, "height": 1080})
    items.append(item)
    files["interlaced.mkv"] = "209"
    # Recorded files whose keyframes are further apart than a DASH remux's 5 s segment: 12 of 33
    # files sampled on rex-fun have a gap over 5 s, most of them x265 encodes (2026-10-27).
    # And one whose 2 s keyframes fit them, which keeps the copy. `slowgop8.mkv` is served at
    # nt.SLOW_RATE, as a debrid source is read: a player has the first segment before the second
    # is cut.
    for key, name, gop in (("210", "gop8.mkv", 8), ("211", "gop12.mkv", 12), ("212", "gop2.mkv", 2), ("213", "slowgop8.mkv", 8)):
        MEDIA[name] = os.path.join(scratch, name)
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=%d" % DURATION, "-f", "lavfi", "-i", "sine=d=%d" % DURATION,
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-x264-params", "keyint=%d:min-keyint=%d:scenecut=0" % (gop * 25, gop * 25), "-c:a", "aac", "-shortest", MEDIA[name]], check=True)
        items.append(file_row(key, name, probe(MEDIA[name])["streams"]))
        files[name] = key
    # Two audio tracks, the second (AC3 5.1) selected.
    MEDIA["twoaudio.mkv"] = os.path.join(scratch, "twoaudio.mkv")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", plain, "-f", "lavfi", "-i", "sine=f=880:d=%d" % DURATION,
                    "-map", "0:v", "-map", "0:a", "-map", "1:a", "-c:v", "copy", "-c:a:0", "copy", "-c:a:1", "ac3", "-ac:a:1", "6",
                    MEDIA["twoaudio.mkv"]], check=True)
    streams = probe(MEDIA["twoaudio.mkv"])["streams"]
    item = file_row("207", "twoaudio.mkv", streams)
    rows = item["Media"][0]["Part"][0]["Stream"]
    second = next(s for s in streams if s["codec_type"] == "audio" and s["codec_name"] == "ac3")
    rows[1]["selected"] = False
    rows.append({"id": 207003, "streamType": 2, "codec": "ac3", "index": second["index"], "channels": 6, "bitrate": 448,
                 "selected": True, "displayTitle": "Second (AC3 5.1)"})
    items.append(item)
    files["twoaudio.mkv"] = "207"
    return items, channels, files


# The profiles whose browsers cannot decode field-coded (PAFF) H.264: Chrome 156 (macOS, Windows) and
# Edge 156 failed a real 1080i50 recording with PIPELINE_ERROR_DECODE, Firefox 153 and WebKit 26.4
# played it (findings, 2026-10-27); Opera is Chromium too. rex converts interlaced H.264 for them.
PAFF_BLIND = {"Chrome", "Microsoft Edge", "Opera"}


def field_order(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=field_order",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.split()
    # MPEG-TS lists the stream under its program too: the first line is enough.
    return out[0] if out else "none"


def guard(port, scratch, live_key, file_key, interlaced_live, interlaced_file, long_gop_live, long_gop_file):
    print("every profile's streaming protocol")
    targets = {}
    for name, profile in sorted(PROFILES.items()):
        for target in profile.get("targets", []):
            if target.get("tag") != "VideoProfile" or target.get("context") == "static":
                continue
            protocol = target.get("protocol")
            if protocol is None:
                continue
            targets.setdefault((name, protocol), target)
    no_protocol = {name for name, profile in PROFILES.items()
                   if any(t.get("tag") == "VideoProfile" and t.get("context") != "static" and t.get("protocol") is None
                          for t in profile.get("targets", []))
                   and not any(t.get("tag") == "VideoProfile" and t.get("protocol") for t in profile.get("targets", []))}
    check("the protocol-less profiles are the known DLNA/pre-universal list", no_protocol == NO_PROTOCOL,
          "new: %s, gone: %s" % (sorted(no_protocol - NO_PROTOCOL), sorted(NO_PROTOCOL - no_protocol)))
    unknown = sorted({protocol for _, protocol in targets} - SERVED)
    check("every profile's streaming protocol is one rex serves (%s)" % ", ".join(sorted(SERVED)), not unknown, str(unknown))
    started = set()
    for (name, protocol), target in sorted(targets.items()):
        signature = (protocol, target.get("container"), target.get("videoCodec") or target.get("codec"), target.get("audioCodec"))
        for kind, key, extra in (("live", live_key, {}), ("capped file", file_key, {"maxVideoBitrate": "1000", "videoResolution": "640x360"}),
                                 ("interlaced live", interlaced_live, {}), ("interlaced file", interlaced_file, {}),
                                 ("long-GOP live", long_gop_live, {}),
                                 ("long-GOP file", long_gop_file, {"directStream": "1", "directStreamAudio": "1"})):
            session = "g-%s-%s-%s" % (re.sub(r"\W", "", name), protocol, re.sub(r"\W", "", kind))
            container = decide(port, profile_query(name, protocol, key, session, **extra))
            text = " ".join(str(container.get(field, "")) for field in ("generalDecisionText", "transcodeDecisionText"))
            check("%s / %s: a %s converts" % (name, protocol, kind),
                  container.get("generalDecisionCode") == 1001 and "No conversion profile" not in text,
                  "%s %s" % (container.get("generalDecisionCode", container.get("status")), text.strip()[:80]))
            blind = kind.startswith("interlaced") and name in PAFF_BLIND
            if blind:
                streams = container.get("Metadata", [{}])[0].get("Media", [{}])[0].get("Part", [{}])[0].get("Stream", [])
                video = next((stream.get("decision") for stream in streams if stream.get("streamType") == 1), None)
                check("%s / %s: an %s's H.264 is converted, not copied" % (name, protocol, kind), video == "transcode", str(video))
            if (signature, kind, blind) in started or container.get("generalDecisionCode") != 1001:
                continue
            started.add((signature, kind, blind))
            ok, path = segment_bytes(port, protocol, profile_query(name, protocol, key, session, **extra), session, scratch,
                                     "guard-%s" % session)
            check("%s / %s: start's %s media decodes (%s)" % (name, protocol, kind, "/".join(str(v) for v in signature[1:])),
                  ok and decodes(path), "%d bytes" % os.path.getsize(path))
            if blind:
                check("%s / %s: the %s's picture comes out deinterlaced" % (name, protocol, kind),
                      field_order(path) == "progressive", field_order(path))
            get(port, "/video/:/transcode/universal/stop", [("session", session)])
    # Plex Web's own requests: its Chrome profile names no streaming target, which the request
    # adds (`X-Plex-Client-Profile-Extra`), so no profile above is Plex Web on Chrome.
    for label, query_of, protocol in (("Plex Web 4.160 (Chrome)", nt.plex_web_query, "hls"),
                                      ("Plex Web 4.147.1 (Chrome)", nt.dash_query, "dash")):
        for kind, key in (("interlaced live", interlaced_live), ("interlaced file", interlaced_file)):
            session = "w-%s-%s" % (protocol, re.sub(r"\W", "", kind))
            container = decide(port, query_of(key, session))
            streams = container.get("Metadata", [{}])[0].get("Media", [{}])[0].get("Part", [{}])[0].get("Stream", [])
            video = next((stream.get("decision") for stream in streams if stream.get("streamType") == 1), None)
            check("%s / %s: an %s's H.264 is converted, not copied" % (label, protocol, kind),
                  container.get("generalDecisionCode") == 1001 and video == "transcode", "%s %s" % (container.get("generalDecisionCode"), video))
            ok, path = segment_bytes(port, protocol, query_of(key, session), session, scratch, "guard-%s" % session)
            check("%s / %s: the %s's picture decodes, deinterlaced" % (label, protocol, kind),
                  ok and decodes(path) and field_order(path) == "progressive", field_order(path))
            get(port, "/video/:/transcode/universal/stop", [("session", session)])


def first_segment(mpd):
    """The segment Shaka 3.3.1 asks for first: 0 for a static MPD; for a dynamic one the live edge
    less the max segment duration and the delay, and one before that (probes/shaka_live_dash.py
    logs its requests)."""
    clock = re.search(r'availabilityStartTime="([^"]+)"', mpd)
    if not clock:
        return 0
    def seconds(name):
        found = re.search(r'%s="PT([\d.]+)S"' % name, mpd)
        return float(found.group(1)) if found else 0.0
    since = time.time() - calendar.timegm(time.strptime(clock.group(1), "%Y-%m-%dT%H:%M:%SZ"))
    length = float(re.search(r'SegmentTemplate timescale="1" duration="(\d+)"', mpd).group(1))
    return max(0, int((since - seconds("maxSegmentDuration") - seconds("suggestedPresentationDelay")) // length) - 1)


def live_dash(port, key):
    """A live channel over DASH, as Plex Web 4.147.1 asks for it: the MPD is dynamic (a static one
    said 0 s, and Shaka 3.3.1 played nothing: probes/shaka_live_dash.py), and a DASH reader goes on
    reading it past the first segments."""
    session = "dash-live"
    query = nt.dash_query(key, session)
    decide(port, query)
    status, _, body = get(port, "/video/:/transcode/universal/start.mpd", query)
    mpd = body.decode("utf-8", "replace")
    check("live over DASH: the MPD is dynamic, clocked, with no duration",
          status == 200 and 'type="dynamic"' in mpd and "availabilityStartTime=" in mpd and "mediaPresentationDuration" not in mpd,
          re.sub(r"\s+", " ", mpd[mpd.find("<MPD"):mpd.find("<Period")])[-260:])
    # The first video segment as Shaka 3.3.1 asks it, straight after the MPD: the live edge less
    # the max segment duration and the delay, and a segment before that (probes/shaka_live_dash.py
    # logs its requests). A live DASH segment was served only once the next one was done, and in
    # 5 s cuts, 6.2 s after start.mpd where HLS's first came in 0.3 s (Shaka's first frame 9.7 s).
    index = first_segment(mpd)
    base = "/video/:/transcode/universal/session/%s/0/" % session
    asked = time.time()
    status_h, _, header = get(port, base + "header")
    status_s, _, first = get(port, base + "%d.m4s" % index)
    waited = time.time() - asked
    check("live over DASH: the first video segment is served within 1.5 s of start.mpd",
          status_h == status_s == 200 and first and waited < 1.5, "segment %d: %s/%s in %.2fs" % (index, status_h, status_s, waited))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])
    session = "dash-live-read"
    query = nt.dash_query(key, session)
    decide(port, query)
    url = "http://127.0.0.1:%d/video/:/transcode/universal/start.mpd?%s" % (port, urllib.parse.urlencode(query))
    started = time.time()
    done = subprocess.run([FFMPEG, "-v", "verbose", "-i", url, "-t", "16", "-map", "0:v:0", "-c", "copy", "-f", "null", "-",
                           "-progress", "pipe:1"], capture_output=True, text=True, timeout=120)
    times = [int(value) for value in re.findall(r"^out_time_us=(\d+)", done.stdout, re.M)]
    read = max(times, default=0) / 1e6
    check("live over DASH: a DASH reader goes on past the first segments (16 s)", read >= 15,
          "%.1f s read in %.0f s %s" % (read, time.time() - started, done.stderr.strip()[-120:]))
    # FFmpeg 8.1's reader logs each segment it opens twice. Under the window rex once wrote, its
    # unsigned arithmetic took every segment for an old one and opened segment 0 thousands of times
    # in a second (rex-fun 2026-10-27: 280 times; dashdec.c `calc_min_seg_no`).
    opened = {}
    for segment in re.findall(r"session/%s/(\d+/\d+\.m4s)" % session, done.stderr):
        opened[segment] = opened.get(segment, 0) + 1
    worst = max(opened.items(), key=lambda item: item[1], default=("none", 0))
    check("live over DASH: the DASH reader opens each segment once", opened and worst[1] <= 2,
          "%s opened %d times (logged)" % worst)
    get(port, "/video/:/transcode/universal/stop", [("session", session)])


APPLE_TV = json.load(open(os.path.join(ROOT, "tests/fixtures/transcode/appletv_hls_mkv_query.json")))


def apple_tv_query(key, session):
    """Plex for Apple TV's own request (`appletv_hls_mkv_query.json`): HLS in Matroska segments."""
    return [(name, {"/library/metadata/KEY": "/library/metadata/%s" % key, "SESSION": session}.get(value, value))
            for name, value in APPLE_TV["query"]]


def hls_segments(port, query, session, count, scratch, label):
    """start.m3u8, then `count` segments in order, as a player asks them: each one's status, and
    the first ones as one decodable file (behind the Matroska header where there is one)."""
    get(port, "/video/:/transcode/universal/start.m3u8", query)
    base = "/video/:/transcode/universal/session/%s/base/" % session
    status, _, playlist = get(port, base + "index.m3u8")
    header = get(port, base + "header")[2] if b"EXT-X-MAP" in playlist else b""
    statuses, data = [], header
    for index in range(count):
        status, _, body = get(port, base + "%05d.ts" % index, timeout=60)
        statuses.append(status)
        if index < 2 and status == 200:
            data += body
    path = os.path.join(scratch, "%s.bin" % label)
    open(path, "wb").write(data)
    return statuses, path


def dash_segments(port, query, session, count, scratch, label):
    status, _, mpd = get(port, "/video/:/transcode/universal/start.mpd", query)
    mpd = mpd.decode("utf-8", "replace")
    first = first_segment(mpd) if status == 200 else 0
    sets = len(re.findall(r"<AdaptationSet", mpd))
    statuses, data = [], b""
    for rep in range(sets):
        statuses.append(get(port, "/video/:/transcode/universal/session/%s/%d/header" % (session, rep))[0])
    for index in range(first, first + count):
        for rep in range(sets):
            status, _, body = get(port, "/video/:/transcode/universal/session/%s/%d/%d.m4s" % (session, rep, index), timeout=60)
            statuses.append(status)
            # rex serves a DASH segment as its init and its fragment: the first is a whole file.
            if rep == 0 and not data and status == 200:
                data = body
    path = os.path.join(scratch, "%s.mp4" % label)
    open(path, "wb").write(data)
    return statuses, path, mpd


def stale(port, scratch, channels, log):
    """Live channels whose streams changed under their probe, as rex-fun's every-app matrix met
    them on 01846cb7 (2026-10-27). rex never probes; a source's probe can be a day old."""
    print("live sources that changed under their probe")
    # 1. Sport 5 Gold: the probe said MP2, the channel serves AAC, which no Matroska header takes
    #    copied: "failed on every path". A failed copy converts that stream and runs again.
    key = channels["live-aacnow"]
    session = "st-aacnow-tv"
    container = decide(port, apple_tv_query(key, session))
    statuses, path = hls_segments(port, apple_tv_query(key, session), session, 3, scratch, session)
    found = streams_of(path)["streams"]
    check("a codec changed under the probe: Apple TV's Matroska HLS plays, the audio converted",
          container.get("generalDecisionCode") == 1001 and statuses == [200] * 3 and decodes(path, "a")
          and [s["codec_name"] for s in found if s["codec_type"] == "audio"] == ["aac"], "%s %s" % (statuses, [s.get("codec_name") for s in found]))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])
    session = "st-aacnow-desktop"
    decide(port, desktop_query(key, session))
    status, headers, data = read_stream(port, desktop_query(key, session), 600_000, 25, os.path.join(scratch, session + ".mkv"))
    check("a codec changed under the probe: Plex Desktop's Matroska stream plays, the audio converted",
          status == 200 and decodes(os.path.join(scratch, session + ".mkv"), "a"), "%s %d bytes" % (status, len(data)))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])
    check("the log says which copy failed and that it is converted",
          "failed on every path copying its audio: converting it" in open(log).read(), "")
    # 2. Movistar Deportes: the probe had video at 0 and no audio; the channel serves audio as 0.
    #    A live source's streams are mapped by kind, and only what the plan has is mapped.
    key = channels["live-audiofirst"]
    for label, run in (("Plex Web DASH", lambda session: dash_segments(port, nt.dash_query(key, session), session, 2, scratch, session)),
                       ("Plex Web HLS", lambda session: hls_segments(port, nt.plex_web_query(key, session), session, 2, scratch, session))):
        session = "st-audiofirst-" + label.split()[-1].lower()
        decide(port, (nt.dash_query if "DASH" in label else nt.plex_web_query)(key, session))
        result = run(session)
        statuses, path = result[0], result[1]
        found = streams_of(path)["streams"]
        sets = len(re.findall(r"<AdaptationSet", result[2])) if len(result) > 2 else None
        check("indexes moved under the probe: %s plays the video, and only it" % label,
              all(status == 200 for status in statuses) and decodes(path) and sets in (None, 1)
              and [s["codec_type"] for s in found] == ["video"], "%s %s sets=%s" % (statuses, [s.get("codec_type") for s in found], sets))
        get(port, "/video/:/transcode/universal/stop", [("session", session)])
    session = "st-audiofirst-desktop"
    decide(port, desktop_query(key, session))
    status, headers, data = read_stream(port, desktop_query(key, session), 600_000, 25, os.path.join(scratch, session + ".mkv"))
    found = streams_of(os.path.join(scratch, session + ".mkv"))["streams"]
    check("indexes moved under the probe: Plex Desktop's stream is the video, and only it",
          status == 200 and decodes(os.path.join(scratch, session + ".mkv")) and [s["codec_type"] for s in found] == ["video"],
          str([s.get("codec_type") for s in found]))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])
    # 3. Digi Sport 4: the source stops publishing for 60 s. FFmpeg waits on its playlist with
    #    nothing out and the player's next segment waited 45 s and failed; now the run starts
    #    again at the live edge, so it comes.
    key = channels["live-hiccup"]
    for label, query_of, segments in (("Plex Web HLS", nt.plex_web_query, hls_segments), ("Plex Web DASH", nt.dash_query, dash_segments)):
        session = "st-hiccup-" + label.split()[-1].lower()
        decide(port, query_of(key, session))
        began = time.time()
        result = segments(port, query_of(key, session), session, 18, scratch, session)
        if "HLS" in label:
            check("a live source that stops for 60 s: %s's every segment comes" % label,
                  all(status == 200 for status in result[0]), "%s in %.0f s" % (result[0], time.time() - began))
        else:
            # Read in order, not by the clock: what the run skipped to reach the clock is gone at
            # once (404), and none waits out the 45 s.
            check("a live source that stops for 60 s: %s's every segment comes or is gone at once" % label,
                  200 in result[0][2:] and all(status in (200, 404) for status in result[0]),
                  "%s in %.0f s" % (result[0], time.time() - began))
        get(port, "/video/:/transcode/universal/stop", [("session", session)])
        CHANNELS["live-hiccup.m3u8"]["since"] = None  # the next client meets the stop again
    # A DASH player follows its MPD's clock, not what was made: after the stop it asks for the
    # segment the clock says, and the run was numbered on from where it stopped, far behind it
    # (Star Sports Select 2: requests jumped 4, 15, 29, 40, each waited 20-30 s).
    session = "st-hiccup-clock"
    query = nt.dash_query(key, session)
    decide(port, query)
    status, _, mpd = get(port, "/video/:/transcode/universal/start.mpd", query)
    mpd = mpd.decode("utf-8", "replace")
    get(port, "/video/:/transcode/universal/session/%s/0/header" % session)
    time.sleep(80)
    index = first_segment(mpd) if status == 200 else 0
    asked = time.time()
    status, _, body = get(port, "/video/:/transcode/universal/session/%s/0/%d.m4s" % (session, index), timeout=60)
    took = time.time() - asked
    check("after a stop, the segment a DASH player's clock asks for comes within 15 s",
          status == 200 and took < 15, "segment %d: %s in %.1f s" % (index, status, took))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])
    CHANNELS["live-hiccup.m3u8"]["since"] = None
    check("the log says why the run started again", "starting again at the live edge" in open(log).read(),
          re.findall(r"st-hiccup[^\n]*(?:starting again|giving up)[^\n]*", open(log).read())[:2])
    session = "st-hiccup-desktop"
    decide(port, desktop_query(key, session))
    began = time.time()
    # A player waits out a silent source longer than rex does (`LIVE_STALL`, 20 s).
    status, headers, data = read_stream(port, desktop_query(key, session), 100_000_000, 90, os.path.join(scratch, session + ".mkv"), silence=40)
    check("a live source that stops: Plex Desktop's Matroska stream ends, saying why",
          status == 200 and decodes(os.path.join(scratch, session + ".mkv")) and time.time() - began < 80
          and re.search(r"st-hiccup-desktop stream(: the live source sent nothing| ended: FFmpeg exited)", open(log).read()),
          "%d bytes in %.0f s; %s" % (len(data), time.time() - began, re.findall(r"st-hiccup-desktop stream[^\n]*", open(log).read())[-1:]))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])


def loudness(path):
    """The mean volume of a file's audio in dB (-91 for silence), or None without audio."""
    done = subprocess.run([FFMPEG, "-v", "info", "-i", path, "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True)
    found = re.search(r"mean_volume: (-?[\d.]+) dB", done.stderr)
    return float(found.group(1)) if found else None


def long_gop_files(port, scratch, files, log):
    """A recorded file remuxed over DASH whose keyframes are further apart than the 5 s template:
    one gap spanning two template slots made one chunk of them, and every later segment held the
    film of the one after its number. Each chunk's own `sidx` shows it; the video is converted from
    there, so every segment starts where its number says. A file whose keyframes fit keeps the copy."""
    print("recorded files with keyframes further apart than a DASH segment")
    for name in ("gop8.mkv", "gop12.mkv", "gop2.mkv", "slowgop8.mkv"):
        key = files[name]
        session = "vod-" + name.split(".")[0]
        query = nt.dash_query(key, session, directStream="1", directStreamAudio="1")
        decide(port, query)
        status, _, mpd = get(port, "/video/:/transcode/universal/start.mpd", query)
        length = int(re.search(r'duration="(\d+)"', mpd.decode()).group(1)) if status == 200 else 5
        placed = []
        for index in range(DURATION // 5):
            status, _, body = get(port, "/video/:/transcode/universal/session/%s/0/%d.m4s" % (session, index), timeout=90)
            kinds = nt.boxes(body) if status == 200 else []
            scale = next((extra["timescale"] for kind, extra in kinds if kind == "moov"), 0)
            tfdt = next((extra.get("tfdt") for kind, extra in kinds if kind == "moof"), None)
            placed.append(round(tfdt / scale - index * length, 1) if scale and tfdt is not None else None)
            path = os.path.join(scratch, "%s-%d.mp4" % (session, index))
            open(path, "wb").write(body)
        # A copied chunk starts on the first keyframe at or after its slot, so within the slot;
        # one a whole slot late holds the next segment's film.
        check("%s keyframes %s s apart: every DASH segment starts in its own slot" % (name, re.search(r"gop(\d+)", name).group(1)),
              all(off is not None and -length < off < length - 0.05 for off in placed) and decodes(path), "offsets %s" % placed)
        get(port, "/video/:/transcode/universal/stop", [("session", session)])
        if name == "gop2.mkv":
            # FFmpeg's `-use_timeline 1` cut each chunk 5 s after the last: 6 s chunks drifted a
            # second a segment, and this file was converted at its fifth.
            switched = re.findall(r"TRANSCODE %s run [^\n]*the copied video cuts only on keyframes" % session, open(log).read())
            check("gop2.mkv keyframes 2 s apart: the remux keeps the copy", not switched, switched[:1])
        session += "-read"
        query = nt.dash_query(key, session, directStream="1", directStreamAudio="1")
        decide(port, query)
        url = "http://127.0.0.1:%d/video/:/transcode/universal/start.mpd?%s" % (port, urllib.parse.urlencode(query))
        try:
            done = subprocess.run([FFMPEG, "-v", "error", "-i", url, "-map", "0:v:0", "-c", "copy", "-f", "null", "-", "-progress", "pipe:1"],
                                  capture_output=True, text=True, timeout=180)
            out = done.stdout
        except subprocess.TimeoutExpired as expired:
            out = (expired.stdout or b"").decode("utf-8", "replace") if isinstance(expired.stdout, bytes) else (expired.stdout or "")
        read = max([int(value) for value in re.findall(r"^out_time_us=(\d+)", out, re.M)] or [0]) / 1e6
        check("%s: a DASH reader reads the whole %d s" % (name, DURATION), read >= DURATION - 3, "%.1f s" % read)
        get(port, "/video/:/transcode/universal/stop", [("session", session)])
    check("the log says where the copied video was converted from", "runs a segment past its own); converting it from segment" in open(log).read(), "")


def long_gop(port, scratch, channels, log):
    """Live DASH of a channel whose keyframes are 10 s apart: copied video cuts only there, so a
    4 s template ran out ahead of it and the player's segment failed (Digi Sport 4, rex-fun
    2026-10-27, 3/3). The video is converted once that shows; the other protocols keep the copy."""
    print("a live source with keyframes 10 s apart")
    key = channels["live-gop10"]
    session = "gop-dash"
    decide(port, nt.dash_query(key, session))
    began = time.time()
    statuses, path, mpd = dash_segments(port, nt.dash_query(key, session), session, 16, scratch, session)
    # As fast as they play: before, each waited on the video's next cut, 10 s for 4 s of film
    # (16 segments in 141 s), and the player ran dry.
    took = time.time() - began
    check("10 s keyframes: live DASH's 16 segments come as fast as they play (64 s of film)",
          all(status == 200 for status in statuses) and decodes(path) and took < 64 + 16, "%s in %.0f s" % (statuses, took))
    check("the log says the copied video is converted, and from where",
          "the copied video cuts only on keyframes further apart" in open(log).read(), "")
    get(port, "/video/:/transcode/universal/stop", [("session", session)])
    session = "gop-dash-read"
    decide(port, nt.dash_query(key, session))
    url = "http://127.0.0.1:%d/video/:/transcode/universal/start.mpd?%s" % (port, urllib.parse.urlencode(nt.dash_query(key, session)))
    try:
        done = subprocess.run([FFMPEG, "-v", "error", "-i", url, "-t", "16", "-map", "0:v:0", "-c", "copy", "-f", "null", "-", "-progress", "pipe:1"],
                              capture_output=True, text=True, timeout=180)
        out = done.stdout
    except subprocess.TimeoutExpired as expired:
        out = (expired.stdout or b"").decode("utf-8", "replace") if isinstance(expired.stdout, bytes) else (expired.stdout or "")
    read = max([int(value) for value in re.findall(r"^out_time_us=(\d+)", out, re.M)] or [0]) / 1e6
    check("10 s keyframes: a DASH reader reads 16 s", read >= 15, "%.1f s" % read)
    get(port, "/video/:/transcode/universal/stop", [("session", session)])
    for label, query_of in (("Plex Web HLS", nt.plex_web_query), ("Apple TV Matroska HLS", apple_tv_query)):
        session = "gop-" + re.sub(r"\W", "", label).lower()
        decide(port, query_of(key, session))
        statuses, path = hls_segments(port, query_of(key, session), session, 4, scratch, session)
        check("10 s keyframes: %s plays" % label, all(status == 200 for status in statuses) and decodes(path), str(statuses))
        get(port, "/video/:/transcode/universal/stop", [("session", session)])
    session = "gop-desktop"
    decide(port, desktop_query(key, session))
    path = os.path.join(scratch, session + ".mkv")
    status, _, data = read_stream(port, desktop_query(key, session), 1_000_000, 40, path, silence=40)
    check("10 s keyframes: Plex Desktop's Matroska stream plays", status == 200 and decodes(path), "%s %d bytes" % (status, len(data)))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])


def missing_audio(port, scratch, channels, log):
    """A live source with no audio at open, where its probe promised some: every run died on
    `-map 0:a:0`, and the ladder converted the audio and then the video for nothing (rex-fun
    2026-10-27, Sport 5 Gold). Silence stands in for it, and a restart takes the audio back."""
    print("a live source without its audio")
    key = channels["live-noaudio"]
    for label, query_of, segments in (("Plex Web DASH", nt.dash_query, dash_segments), ("Plex Web HLS", nt.plex_web_query, hls_segments),
                                      ("Apple TV Matroska HLS", apple_tv_query, hls_segments)):
        session = "na-" + re.sub(r"\W", "", label).lower()
        decide(port, query_of(key, session))
        result = segments(port, query_of(key, session), session, 2, scratch, session)
        sets = len(re.findall(r"<AdaptationSet", result[2])) if len(result) > 2 else None
        sound = result[1]
        if sets:
            # DASH keeps the audio in its own representation: its first segment.
            sound = os.path.join(scratch, session + "-audio.mp4")
            open(sound, "wb").write(get(port, "/video/:/transcode/universal/session/%s/1/%d.m4s" % (session, first_segment(result[2])))[2])
        found = sorted({s["codec_type"] for path in {result[1], sound} for s in streams_of(path)["streams"]})
        check("no audio at open: %s plays, a silent track where the audio was promised" % label,
              all(status == 200 for status in result[0]) and decodes(result[1]) and found == ["audio", "video"]
              and (loudness(sound) or 0) < -80 and sets in (None, 2),
              "%s %s %s sets=%s" % (result[0], found, loudness(sound), sets))
        get(port, "/video/:/transcode/universal/stop", [("session", session)])
    session = "na-desktop"
    decide(port, desktop_query(key, session))
    path = os.path.join(scratch, session + ".mkv")
    status, _, data = read_stream(port, desktop_query(key, session), 600_000, 25, path)
    check("no audio at open: Plex Desktop's Matroska stream plays, a silent track in it",
          status == 200 and decodes(path) and "audio" in [s["codec_type"] for s in streams_of(path)["streams"]], "%s %d bytes" % (status, len(data)))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])
    text = open(log).read()
    check("the log says silence stands in, and no copy was converted for it",
          "has no audio now; silence stands in" in text and not re.search(r"na-\w+ (stream )?failed on every path copying", text), "")
    # The audio comes back: the restart after the source held reads it again.
    key = channels["live-audioback"]
    session = "na-back"
    decide(port, nt.plex_web_query(key, session))
    get(port, "/video/:/transcode/universal/start.m3u8", nt.plex_web_query(key, session))
    base = "/video/:/transcode/universal/session/%s/base/" % session
    statuses, last = [], b""
    for index in range(22):
        status, _, body = get(port, base + "%05d.ts" % index, timeout=60)
        statuses.append(status)
        last = body if status == 200 else last
    path = os.path.join(scratch, session + ".ts")
    open(path, "wb").write(last)
    check("the audio back after a restart: the player's last segment has the source's own sound",
          all(status == 200 for status in statuses) and (loudness(path) or -91) > -60, "%s %s dB" % (statuses, loudness(path)))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])


def http_matrix(port, scratch, channels, files, log):
    print("Plex Desktop's Matroska stream")
    expect = {
        "live-guard": ("h264", "aac", 2), "live-guard-i": ("h264", "aac", 2), "live-h264": ("h264", "aac", 2), "live-1080i": ("h264", "mp2", 2), "live-hevc": ("hevc", "mp2", 1),
        "live-ac3": ("h264", "ac3", 6), "live-radio": (None, "aac", 2), "live-gop5": ("h264", "aac", 2),
    }
    for name, key in channels.items():
        if name not in expect:
            continue
        session = "h-" + name
        container = decide(port, desktop_query(key, session))
        media = container.get("Metadata", [{}])[0].get("Media", [{}])[0]
        part = media.get("Part", [{}])[0]
        check("%s: decision is Conversion OK as mkv, streams direct" % name,
              container.get("generalDecisionCode") == 1001 and media.get("container") == "mkv" and "protocol" not in media
              and all(stream.get("location") == "direct" for stream in part.get("Stream", [])),
              json.dumps({k: media.get(k) for k in ("container", "protocol", "videoCodec", "audioCodec")}))
        path = os.path.join(scratch, "%s.mkv" % name)
        status, headers, data = read_stream(port, desktop_query(key, session), 1_500_000, 25, path)
        check("%s: start is plexlab's chunked video/x-matroska" % name,
              status == 200 and headers.get("content-type") == "video/x-matroska" and headers.get("transfer-encoding") == "chunked"
              and headers.get("accept-ranges") == "none" and data[:4] == b"\x1a\x45\xdf\xa3", "%s %d bytes" % (status, len(data)))
        found = streams_of(path)["streams"]
        video = next((s["codec_name"] for s in found if s["codec_type"] == "video"), None)
        audio = next((s for s in found if s["codec_type"] == "audio"), {})
        want_video, want_audio, channels_n = expect[name]
        check("%s: the stream is %s/%s %dch and decodes" % (name, want_video, want_audio, channels_n),
              video == want_video and audio.get("codec_name") == want_audio and audio.get("channels") == channels_n
              and decodes(path, "v" if want_video else "a"), "%s/%s %sch" % (video, audio.get("codec_name"), audio.get("channels")))
        check("%s: hanging up ends its FFmpeg" % name, gone(name + ".m3u8"), str(ffmpegs_reading(name + ".m3u8")))
        get(port, "/video/:/transcode/universal/stop", [("session", session)])

    capped = {"maxVideoBitrate": "1500", "videoResolution": "1280x720"}
    for name, extra, want in (
            ("h264.mkv", {}, ("h264", "copy")),
            ("h264.mkv", capped, ("h264", "convert")),
            ("plainmp4.mp4", capped, ("h264", "convert")),
            ("hdr10.mkv", {}, ("hevc", "copy")),
            ("hdr10.mkv", capped, ("h264", "convert")),
            ("srt.mkv", {}, ("h264", "copy")),
            ("pgs.mkv", capped, ("h264", "convert")),
            ("twoaudio.mkv", {}, ("h264", "copy"))):
        key = files[name]
        label = "%s%s" % (name, " capped" if extra else "")
        session = "f-" + re.sub(r"\W", "", label)
        container = decide(port, desktop_query(key, session, **extra))
        if name == "hdr10.mkv" and extra and "no HDR is (no zscale)" in open(log, errors="replace").read():
            # rex never sends HDR untonemapped: without zscale (Homebrew's FFmpeg) it refuses.
            check("hdr10 capped: without zscale the HDR conversion is refused", container.get("generalDecisionCode") != 1001,
                  str(container.get("generalDecisionCode")))
            continue
        streams = {s.get("streamType"): s for s in container.get("Metadata", [{}])[0].get("Media", [{}])[0].get("Part", [{}])[0].get("Stream", [])}
        decision = streams.get(1, {}).get("decision")
        check("%s: decision 1001, video %s" % (label, "transcode" if want[1] == "convert" else "copy"),
              container.get("generalDecisionCode") == 1001 and decision == ("transcode" if want[1] == "convert" else "copy"), str(decision))
        path = os.path.join(scratch, "%s.mkv" % session)
        status, headers, data = read_stream(port, desktop_query(key, session, **extra), 50_000_000, 60, path)
        found = streams_of(path)
        video = next((s for s in found["streams"] if s["codec_type"] == "video"), {})
        audio = next((s for s in found["streams"] if s["codec_type"] == "audio"), {})
        duration = last_pts(path)
        check("%s: the whole title streams as %s and decodes" % (label, want[0]),
              status == 200 and video.get("codec_name") == want[0] and decodes(path) and abs(duration - DURATION) < 2.5,
              "%s %.1fs %d bytes" % (video.get("codec_name"), duration, len(data)))
        if name == "hdr10.mkv" and extra:
            tags = "%s,%s" % (video.get("color_transfer"), video.get("color_primaries"))
            check("hdr10 capped: the picture is tonemapped to BT.709", tags == "bt709,bt709", tags)
        if name == "srt.mkv":
            subtitle = next((s for s in found["streams"] if s["codec_type"] == "subtitle"), {})
            check("srt.mkv: the selected SRT is carried in the Matroska", subtitle.get("codec_name") == "subrip", str(subtitle.get("codec_name")))
        if name == "pgs.mkv":
            bright = nt.luma(path, 2, nt.BOX)
            check("pgs.mkv: the selected PGS box is burned in", bright > 200, "%.0f" % bright)
        if name == "twoaudio.mkv":
            check("twoaudio.mkv: the selected second track (AC3 5.1) is the one streamed",
                  audio.get("codec_name") == "ac3" and audio.get("channels") == 6, "%s %sch" % (audio.get("codec_name"), audio.get("channels")))
        if name == "h264.mkv" and not extra:
            status, _, _ = get(port, "/video/:/transcode/universal/ping", [("session", session)])
            _, _, body = get(port, "/transcode/sessions")
            rows = [r for r in json.loads(body)["MediaContainer"].get("TranscodeSession", []) if r.get("key") == session]
            check("an ended stream's session is kept, as plexlab's: ping 200, listed complete http/mkv",
                  status == 200 and rows and rows[0].get("protocol") == "http" and rows[0].get("container") == "mkv"
                  and rows[0].get("complete") is True, json.dumps(rows[0] if rows else {})[:160])
        status = get(port, "/video/:/transcode/universal/stop", [("session", session)])[0]
        if name == "h264.mkv" and not extra:
            check("its stop is 200, and ping after it 404", status == 200
                  and get(port, "/video/:/transcode/universal/ping", [("session", session)])[0] == 404, str(status))

    # A seek: plexlab's `offset=4` starts at 4.0 s with copyts=1 and at 0 with copyts=0.
    for copyts, low, high in (("1", 9.5, 12.5), ("0", -0.2, 1.0)):
        session = "seek-" + copyts
        query = desktop_query(files["h264.mkv"], session, offset="10", copyts=copyts, **capped)
        decide(port, query)
        path = os.path.join(scratch, "seek-%s.mkv" % copyts)
        read_stream(port, query, 400_000, 20, path)
        pts = first_pts(path)
        check("a seek to 10 s with copyts=%s starts at %s" % (copyts, "10 s" if copyts == "1" else "0"),
              pts is not None and low <= pts <= high, str(pts))
        get(port, "/video/:/transcode/universal/stop", [("session", session)])

    # A source that stops sending: FFmpeg writes nothing more, so only the socket says the client
    # hung up. Before, FFmpeg ran on until the idle reaper (120 s).
    session = "stalled"
    query = desktop_query(files["stall.mkv"], session, **capped)
    decide(port, query)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    connection.request("GET", "/video/:/transcode/universal/start?" + urllib.parse.urlencode(query))
    response = connection.getresponse()
    received = 0
    try:
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            received += len(chunk)
    except (TimeoutError, OSError):
        pass  # the source has gone quiet
    stalled = bool(ffmpegs_reading("stall.mkv"))
    # The response holds the socket's file: closing only the connection sends no FIN.
    response.close()
    connection.close()
    hung_up = time.time()
    ended = gone("stall.mkv", timeout=8)
    check("a hang-up while the source sends nothing ends FFmpeg within seconds",
          received and stalled and ended, "%d bytes, FFmpeg %s, gone %s in %.1fs"
          % (received, "running" if stalled else "not running", ended, time.time() - hung_up))
    get(port, "/video/:/transcode/universal/stop", [("session", session)])

    # A stop mid-stream ends the stream and its FFmpeg; the source is read slowly, so it runs on.
    session = "stopme"
    query = desktop_query(files["slowstop.mkv"], session, **capped)
    decide(port, query)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.request("GET", "/video/:/transcode/universal/start?" + urllib.parse.urlencode(query))
    response = connection.getresponse()
    first = response.read1(65536)
    running = bool(ffmpegs_reading("slowstop.mkv"))
    status = get(port, "/video/:/transcode/universal/stop", [("session", session)])[0]
    ended = False
    deadline = time.time() + 15
    try:
        while time.time() < deadline:
            if not response.read1(65536):
                ended = True
                break
    except (http.client.IncompleteRead, OSError):
        ended = True
    connection.close()
    check("a stop mid-stream is 200, ends the stream and its FFmpeg",
          first and running and status == 200 and ended and gone("slowstop.mkv"), "running=%s stop=%s ended=%s" % (running, status, ended))


def main():
    if not FFMPEG or not FFPROBE:
        print("SKIP: no ffmpeg (set REX_TRANSCODER)")
        return 0
    scratch = tempfile.mkdtemp(prefix="rex-http-mkv-test-")
    try:
        return run(scratch)
    finally:
        # A run leaves ~1 GB of generated channels and segments: 208 left behind filled a disk.
        if os.environ.get("REX_KEEP_TMP"):
            print("scratch kept: %s" % scratch)
        else:
            shutil.rmtree(scratch, ignore_errors=True)


def run(scratch):
    items, channels, files = build(scratch)
    catalog = os.path.join(scratch, "catalog.json")
    json.dump({"machineIdentifier": "0000000000000000000000000000000000000008", "friendlyName": "rex-http-mkv",
               "sections": [{"key": "1", "type": "movie", "title": "Clips", "uuid": "0" * 39 + "8"}],
               "items": items}, open(catalog, "w"))
    source_port = nt.free_port()
    httpd = Quiet(("127.0.0.1", source_port), Source)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port, process, log = nt.start_server(catalog, source_port, True, scratch)
    try:
        if "guard" in sys.argv[1:] or len(sys.argv) == 1:
            guard(port, scratch, channels["live-guard"], files["h264.mkv"], channels["live-guard-i"], files["interlaced.mkv"], channels["live-gop10"], files["gop12.mkv"])
            live_dash(port, channels["live-guard"])
        if "stale" in sys.argv[1:] or len(sys.argv) == 1:
            stale(port, scratch, channels, log)
            missing_audio(port, scratch, channels, log)
        if any(name in sys.argv[1:] for name in ("stale", "gop")) or len(sys.argv) == 1:
            long_gop(port, scratch, channels, log)
            long_gop_files(port, scratch, files, log)
        if "http" in sys.argv[1:] or len(sys.argv) == 1:
            http_matrix(port, scratch, channels, files, log)
    finally:
        process.terminate()
        process.wait()
        httpd.shutdown()
    results = nt.results
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
