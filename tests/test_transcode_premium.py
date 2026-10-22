#!/usr/bin/env python3
"""Generated-media processing regressions; needs rex, FFmpeg with loudnorm,
 dialoguenhance, zscale/tonemap, libx264, and ffprobe. No production services.
 Run alongside test_native_transcode.py (which covers subtitle/codec/seek regressions).
"""
import array
import json
import math
import os
import socketserver
import subprocess
import tempfile
import threading
import time

import test_native_transcode as t


class AuthenticatedSource(t.Zurg):
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer transcode-source-secret":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()


def run(*args):
    return subprocess.run([t.FFMPEG, "-v", "error", "-nostdin", *args],
                          check=True, capture_output=True, timeout=90).stdout


def media(container):
    return container["Metadata"][0]["Media"][0]


def rms(path):
    raw = run("-i", path, "-map", "0:a:0", "-ar", "48000", "-ac", "2",
              "-f", "f32le", "-")
    samples = array.array("f", raw)
    return math.sqrt(sum(x*x for x in samples) / len(samples))


def capture(port, scratch, label, key="1", **options):
    query = t.plex_web_query(key, label, **options)
    status, _, body = t.get(port, "/video/:/transcode/universal/decision", query)
    assert status == 200 and json.loads(body)["MediaContainer"].get("transcodeDecisionCode") == 1001, (status, body)
    status, _, body = t.get(port, "/video/:/transcode/universal/start.m3u8", query)
    assert status == 200, (status, body)
    payload = bytearray()
    for index in range(2):
        status, _, segment = t.get(port, "/video/:/transcode/universal/session/%s/base/%05d.ts" % (label, index))
        assert status == 200 and segment, (status, segment[:200])
        payload.extend(segment)
    path = os.path.join(scratch, label + ".ts")
    with open(path, "wb") as out:
        out.write(payload)
    status, _, body = t.get(port, "/transcode/sessions")
    rows = json.loads(body)["MediaContainer"].get("TranscodeSession", [])
    session = next(row for row in rows if row["key"] == label)
    t.get(port, "/video/:/transcode/universal/stop", [("session", label)])
    return path, session


def main():
    assert t.FFMPEG and t.FFPROBE, "FFmpeg and ffprobe are required"
    with tempfile.TemporaryDirectory(prefix="rex-premium-") as scratch:
        source = os.path.join(scratch, "quiet.mkv")
        # Correlated stereo is a measurable dialogue/centre signal, quiet enough
        # that dynamic normalization has substantial headroom without clipping.
        run("-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24:d=12",
            "-f", "lavfi", "-i", "aevalsrc=0.008*sin(2*PI*440*t)|0.008*sin(2*PI*440*t):s=48000:d=12",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "24", "-c:a", "aac", "-b:a", "192k", source)
        t.MEDIA["quiet.mkv"] = source
        probe = t.probe(source)
        item = t.row("1", "Quiet", "quiet.mkv", probe["streams"], os.path.getsize(source), 12)
        hdr = os.path.join(scratch, "hdr.mkv")
        run("-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=24:d=12",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-vf", "format=yuv420p10le",
            "-c:v", "ffv1", "-color_trc", "smpte2084", "-color_primaries", "bt2020",
            "-colorspace", "bt2020nc", "-c:a", "aac", hdr)
        t.MEDIA["hdr.mkv"] = hdr
        hdr_item = t.row("2", "HDR", "hdr.mkv", t.probe(hdr)["streams"], os.path.getsize(hdr), 12)
        stream = hdr_item["Media"][0]["Part"][0]["Stream"][0]
        stream.update(bitDepth=10, colorTrc="smpte2084", colorPrimaries="bt2020", colorSpace="bt2020nc")
        catalog = os.path.join(scratch, "catalog.json")
        with open(catalog, "w") as out:
            json.dump({"machineIdentifier": "0000000000000000000000000000000000000007",
                       "sections": [{"key": "1", "type": "movie", "title": "Clips"}],
                       "items": [item, hdr_item]}, out)
        with socketserver.ThreadingTCPServer(("127.0.0.1", t.free_port()), AuthenticatedSource) as upstream:
            upstream.daemon_threads = True
            threading.Thread(target=upstream.serve_forever, daemon=True).start()
            port, process, _ = t.start_server(catalog, upstream.server_address[1], True, scratch,
                                             REX_PLEXTV_URL="http://127.0.0.1:%d" % t.free_port())
            try:
                t.register_source(port, "zurg", {"url": "http://127.0.0.1:%d/dav" % upstream.server_address[1],
                                                "mount": "/zurg", "bearer": "transcode-source-secret"})
                t.get(port, "/:/prefs", [("HardwareAcceleratedCodecs", "0")], method="PUT")
                # Async capability probing must finish before exercising processing.
                deadline = time.monotonic() + 45
                while True:
                    _, decision = t.decision(port, "1", "ready", normalizeLoudness="1")
                    if decision.get("transcodeDecisionCode") == 1001:
                        break
                    assert time.monotonic() < deadline, "loudnorm processing unavailable"
                    time.sleep(.1)
                _, plain = t.decision(port, "1", "direct", directPlay="1")
                assert plain.get("mdeDecisionCode") == 1000
                baseline, session = capture(port, scratch, "plain", boostDialog="0", normalizeLoudness="0")
                assert session["audioDecision"] == "copy"
                base_rms = rms(baseline)
                for option in ("normalizeLoudness", "boostDialog"):
                    options = {option: "1", "maxVideoBitrate": "12000", "videoResolution": "1920x1080"}
                    _, decision = t.decision(port, "1", option, directPlay="1", **options)
                    streams = media(decision)["Part"][0]["Stream"]
                    audio = next(row for row in streams if row["streamType"] == 2)
                    assert audio["decision"] == "transcode", (option, audio)
                    path, session = capture(port, scratch, option, directPlay="1", **options)
                    assert session["audioDecision"] == "transcode" and session[option] is True
                    assert session["videoDecision"] == "copy", session
                    assert rms(path) > base_rms * 1.15, (option, rms(path), base_rms)
                    output_audio = next(s for s in t.probe(path)["streams"] if s["codec_type"] == "audio")
                    assert output_audio["sample_rate"] == "48000"
                # Low caps formerly silently became 300 kbps video + 192 kbps audio.
                for cap in (128, 320, 720, 2000):
                    _, decision = t.decision(port, "1", "cap%d" % cap, maxVideoBitrate=str(cap))
                    output = media(decision)
                    assert 0 < output["bitrate"] <= cap, (cap, output)
                    assert next(s for s in output["Part"][0]["Stream"] if s["streamType"] == 1)["decision"] == "transcode"
                for cap in (1, 32, 100):
                    _, decision = t.decision(port, "1", "too-small%d" % cap, maxVideoBitrate=str(cap))
                    assert decision.get("transcodeDecisionCode") != 1001, (cap, decision)
                # A trusted loopback/LAN connection cannot be forced into WAN policy by a query.
                t.get(port, "/:/prefs", [("WanPerStreamMaxUploadRate", "320")], method="PUT")
                _, decision = t.decision(port, "1", "lan", location="wan")
                assert media(decision)["bitrate"] > 320
                t.get(port, "/:/prefs", [("WanPerStreamMaxUploadRate", "0")], method="PUT")
                pixels = {}
                for algorithm in ("off", "linear", "gamma", "clip", "hable", "reinhard", "mobius"):
                    t.get(port, "/:/prefs", [("TranscoderToneMapping", "0" if algorithm == "off" else "1"),
                                              ("TranscoderToneMappingAgorithm", "hable" if algorithm == "off" else algorithm)], method="PUT")
                    path, session = capture(port, scratch, "hdr-" + algorithm, key="2", directStream="0")
                    assert session["toneMapping"] is (algorithm != "off"), session
                    pixels[algorithm] = run("-i", path, "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-")
                    assert not session.get("transcodeHwEncoding"), session
                assert pixels["off"] != pixels["hable"] != pixels["reinhard"]
            finally:
                process.terminate()
                process.wait(timeout=15)
                upstream.shutdown()
    print("premium generated-media regressions passed")


if __name__ == "__main__":
    main()
