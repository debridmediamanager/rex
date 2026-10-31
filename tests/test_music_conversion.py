#!/usr/bin/env python3
"""A track converted for a client that turned direct play off, as plexav's PMS converts one.

`fixtures/music/conversion.json` (`probes/record_music_conversion.py`) is plexav's answers to Plex
Web's and Plex for Android's own recorded music decisions with the client's `directPlay=0` -- what
each sends below original music quality -- and the start, playlists and segments that answer
names: Web's DASH (`mp4`/AAC, `segments-audio`), Android's HLS in Matroska (MP3, `segments-av`).
Its `rules` are the same decision for five codecs and four `musicBitrate`s: every codec direct
plays with direct play on (FLAC, MP3, Opus, Vorbis, ALAC, PCM), and a conversion is 95% of the
asked bitrate, an AAC at most six bits a sample. `music_rig.py` pushes plexav's own 46 files; rex
converts with its FFmpeg (`test_ffmpeg()`), and the segments must decode.

    python3 tests/test_music_conversion.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

import music_rig as M
from music_rig import check, results
from server_command import test_ffmpeg

RECORDED = json.load(open(os.path.join(M.FIXTURES, "conversion.json")))
NOT_ANALYSED = ("deepAnalysisVersion", "requiredBandwidths")


def exchange(client, suffix):
    return next(e for e in RECORDED["exchanges"] if e["client"] == client and e["path"].endswith(suffix))


def decode(ffprobe, data):
    with tempfile.NamedTemporaryFile(suffix=".bin") as handle:
        handle.write(data)
        handle.flush()
        out = subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", handle.name],
                             capture_output=True, text=True).stdout
    streams = (json.loads(out or "{}").get("streams") or [])
    return [(s.get("codec_name"), s.get("sample_rate"), s.get("channels")) for s in streams]


def main():
    ffmpeg = test_ffmpeg()
    ffprobe = shutil.which("ffprobe")
    os.environ["REX_TRANSCODER"] = ffmpeg
    work, items, rex, handler, asked = M.start()
    try:
        status, body = rex.push(items)
        assert status == 200 and body.get("created") == 46, body
        mapping, parts = M.key_map(rex)

        # -- the rules plexav followed -----------------------------------------------------------
        for client in ("web-music-play", "droid-music-play"):
            record = next(r for r in M.load(client) if r["path"] == "/music/:/transcode/universal/decision")
            for rule in (r for r in RECORDED["rules"] if r["client"] == client and r["title"] == "15 Step"):
                query = [(k, "/library/metadata/%s" % mapping["32"] if k == "path" else rule["directPlay"] if k == "directPlay"
                          else rule["musicBitrate"] if k == "musicBitrate" else v) for k, v in record["query"]]
                status, _, answer = rex.request("/music/:/transcode/universal/decision?" + urllib.parse.urlencode(
                    [(k, v) for k, v in query if "token" not in k.lower()]), raw=True, headers={"Accept": "application/json"})
                media = (json.loads(answer).get("MediaContainer", {}).get("Metadata") or [{}])[0].get("Media", [{}])[0]
                check("%s, directPlay=%s, musicBitrate=%s: %s %s at %s kbit/s, as plexav" % (
                      client.split("-")[0], rule["directPlay"], rule["musicBitrate"], rule["codec"], rule["container"], rule["bitrate"]),
                      (media.get("audioCodec"), media.get("container"), media.get("bitrate")) == (rule["codec"], rule["container"], rule["bitrate"]),
                      (media.get("audioCodec"), media.get("container"), media.get("bitrate")))

        # -- Plex Web: DASH ----------------------------------------------------------------------
        recorded = exchange("web-music-play", "/decision")
        query = [(k, "/library/metadata/%s" % mapping["32"] if k == "path" else v) for k, v in recorded["query"]]
        status, headers, body = rex.request(recorded["path"] + "?" + urllib.parse.urlencode(query), raw=True,
                                            headers={"Accept": "text/plain, */*; q=0.01"})
        differences = M.compare(recorded["body"], body, ignore=NOT_ANALYSED + ("viewCount", "skipCount", "lastViewedAt"))
        check("Web's decision with direct play off is plexav's conversion (3000/1001/1001, AAC in mp4, segments-audio)",
              status == 200 and not differences and b'generalDecisionCode="1001"' in body, (status, differences[:8]))
        recorded = exchange("web-music-play", "start.mpd")
        status, headers, mpd = rex.request(recorded["path"] + "?" + urllib.parse.urlencode(query), raw=True)
        theirs = recorded["body"]
        pick = lambda text, pattern: re.findall(pattern, text if isinstance(text, str) else text.decode())
        same = all(pick(mpd, pattern) == pick(theirs, pattern) for pattern in (
            r'mimeType="([^"]+)"', r'codecs="([^"]+)"', r'bandwidth="(\d+)"', r'audioSamplingRate="(\d+)"',
            r'mediaPresentationDuration="([^"]+)"', r'maxSegmentDuration="([^"]+)"', r'<SegmentTemplate timescale="1" duration="(\d+)"'))
        check("its start.mpd is plexav's: one AAC representation, one-second segments", status == 200
              and headers.get("Content-Type") == recorded["headers"]["Content-Type"] and same, (status, mpd[:600]))
        session = dict(query)["session"]
        base = "/music/:/transcode/universal/session/%s/0/" % session
        status, head_headers, header = rex.request(base + "header", raw=True)
        status0, seg_headers, segment = rex.request(base + "0.m4s", raw=True)
        check("its header and first segment are served as plexav's (application/octet-stream)",
              (status, status0) == (200, 200) and head_headers.get("Content-Type") == seg_headers.get("Content-Type")
              == exchange("web-music-play", "/0/header")["headers"]["Content-Type"], (status, status0, head_headers.get("Content-Type")))
        check("and they decode: AAC at 44.1 kHz, two channels", decode(ffprobe, header + segment) == [("aac", "44100", 2)],
              decode(ffprobe, header + segment))
        rex.request("/music/:/transcode/universal/stop?session=%s" % session)

        # -- Plex for Android: HLS in Matroska ---------------------------------------------------
        recorded = exchange("droid-music-play", "/decision")
        query = [(k, "/library/metadata/%s" % mapping["32"] if k == "path" else v) for k, v in recorded["query"]]
        status, _, body = rex.request(recorded["path"] + "?" + urllib.parse.urlencode(query), raw=True, headers={"Accept": "application/json"})
        differences = M.compare(recorded["body"], body, ignore=NOT_ANALYSED + ("viewCount", "skipCount", "lastViewedAt"))
        check("Android's decision with direct play off is plexav's: MP3 in mkv over HLS, segments-av",
              status == 200 and not differences, (status, differences[:8]))
        session = dict(query)["session"]
        recorded = exchange("droid-music-play", "start.m3u8")
        status, headers, master = rex.request(recorded["path"] + "?" + urllib.parse.urlencode(query), raw=True)
        check("its start.m3u8 is plexav's, byte for byte", status == 200 and master.decode() == recorded["body"].replace("rexprobe-droid", session),
              (status, master[:200], recorded["body"][:200]))
        recorded = exchange("droid-music-play", "index.m3u8")
        status, headers, index = rex.request("/music/:/transcode/universal/session/%s/base/index.m3u8" % session, raw=True)
        check("its media playlist is plexav's: Matroska's header map and 45 one-second segments", status == 200 and index.decode() == recorded["body"],
              (status, index[:300]))
        status, head_headers, header = rex.request("/music/:/transcode/universal/session/%s/base/header" % session, raw=True)
        status0, seg_headers, segment = rex.request("/music/:/transcode/universal/session/%s/base/00000.ts" % session, raw=True)
        check("its header and first segment are plexav's types (octet-stream, video/MP2T)",
              (head_headers.get("Content-Type"), seg_headers.get("Content-Type"))
              == (exchange("droid-music-play", "base/header")["headers"]["Content-Type"], exchange("droid-music-play", "00000.ts")["headers"]["Content-Type"]),
              (status, status0, head_headers.get("Content-Type"), seg_headers.get("Content-Type")))
        check("and they decode: MP3, two channels", [s[0::2] for s in decode(ffprobe, header + segment)] == [("mp3", 2)], decode(ffprobe, header + segment))
        rex.request("/music/:/transcode/universal/stop?session=%s" % session)
    finally:
        rex.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if not (test_ffmpeg() and shutil.which("ffprobe")):
        print("SKIP: needs ffmpeg and ffprobe")
        sys.exit(0)
    sys.exit(main())
