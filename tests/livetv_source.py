#!/usr/bin/env python3
"""A source of live channels with a guide, for Live TV (docs/feed-api.md, "Live TV").

Each channel is a real live HLS stream: one FFmpeg per channel writes 2 s segments of a test
picture and a tone (H.264 720p30 High, AC3 mono 48 kHz, as plexav's HDHomeRun sends) in real time,
keeping the newest six, with no end. The source serves them over HTTP, registers with a rex and
pushes each channel as a feed item with its probe and a `channel`: its number, name and guide.

Run it by hand against a rex for an end-to-end client test (Plex Web's Live TV, Plex for
Android's Live TV tab):

    python3 tests/livetv_source.py --rex http://127.0.0.1:32400

It pushes three channels with a week's guide starting two hours ago, as plexav's guide.xml was
made (news on the hour, Firefly on the half hour, films every other hour, football hourly), and
serves until interrupted. `tests/test_livetv.py` imports it and pushes a recorded guide instead.
"""
import argparse
import functools
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time
import urllib.request

CHANNELS = [("2.1", "Rex News", "red"), ("4.1", "Rex Movies", "blue"), ("7.1", "Rex Sports", "green")]


class Quiet(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # a reader hanging up mid-segment is no failure


class Files(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        # A live playlist changes every segment: never cached.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


class LiveSource:
    """Live HLS channels under `<url>/channels/<number>.m3u8`."""

    def __init__(self, ffmpeg, ffprobe, port, channels=CHANNELS, root=None):
        self.ffmpeg, self.ffprobe, self.channels = ffmpeg, ffprobe, channels
        self.root = root or tempfile.mkdtemp(prefix="rex-livetv-source-")
        self.dir = os.path.join(self.root, "channels")
        os.makedirs(self.dir, exist_ok=True)
        self.port = port
        self.url = "http://127.0.0.1:%d" % port
        self.processes = []
        self.httpd = Quiet(("127.0.0.1", port), functools.partial(Files, directory=self.root))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        for number, title, colour in channels:
            self.processes.append(subprocess.Popen(
                [ffmpeg, "-v", "error", "-re",
                 "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30,drawbox=c=%s@0.4:t=fill" % colour,
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                 "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-g", "60", "-b:v", "2M", "-pix_fmt", "yuv420p",
                 "-c:a", "ac3", "-ac", "1", "-b:a", "192k",
                 "-f", "hls", "-hls_time", "2", "-hls_list_size", "6", "-hls_flags", "delete_segments+omit_endlist",
                 "-hls_segment_filename", os.path.join(self.dir, number + "-%05d.ts"), os.path.join(self.dir, number + ".m3u8")],
                stdin=subprocess.DEVNULL))

    def wait(self, segments=3, timeout=60):
        """Until every channel's playlist lists `segments` segments."""
        deadline = time.time() + timeout
        for number, _, _ in self.channels:
            playlist = os.path.join(self.dir, number + ".m3u8")
            while time.time() < deadline:
                if os.path.exists(playlist) and open(playlist).read().count("#EXTINF") >= segments:
                    break
                time.sleep(0.3)
            else:
                raise RuntimeError("channel %s made no playlist" % number)

    def probe(self, number):
        """ffprobe's JSON of a channel's live playlist: `hls`, and no duration."""
        result = subprocess.run([self.ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json",
                                 "%s/channels/%s.m3u8" % (self.url, number)], capture_output=True, text=True, timeout=60)
        return json.loads(result.stdout)

    def item(self, number, title, channel):
        item = {"id": "channels/%s.m3u8" % number, "path": "channels/%s.m3u8" % number,
                "display": {"title": title}, "media_info": self.probe(number)}
        if channel is not None:
            item["channel"] = channel
        return item

    def close(self):
        for process in self.processes:
            process.kill()
            process.wait()
        self.httpd.shutdown()
        shutil.rmtree(self.root, ignore_errors=True)


def call(rex, method, path, body):
    request = urllib.request.Request(rex + path, data=json.dumps(body).encode(), method=method,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read() or b"{}")


def push(rex, source, items, name="livetv"):
    """Register the source with rex and push `items` (`docs/feed-api.md`)."""
    call(rex, "PUT", "/rex/sources/" + name, {"url": source.url})
    return call(rex, "POST", "/rex/feed/%s/items" % name, {"items": items})


def week(start=None):
    """A week of guide per channel from two hours ago, in plexav's guide.xml pattern."""
    hour = (int(start or time.time()) // 3600 - 2) * 3600
    guide = {number: [] for number, _, _ in CHANNELS}
    for h in range(24 * 7):
        at = hour + h * 3600
        guide["2.1"].append({"start": at, "end": at + 1800, "title": "Rex Evening News", "summary": "The day's headlines.",
                             "type": "movie", "categories": ["News"], "new": True})
        episode = h % 14 + 1
        guide["2.1"].append({"start": at + 1800, "end": at + 3600, "title": "Episode %d" % episode, "summary": "Episode %d." % episode,
                             "type": "episode", "show": "Firefly", "season": 1, "episode": episode,
                             "categories": ["Series", "Drama"], "new": h % 3 == 0})
        if h % 2 == 0:
            guide["4.1"].append({"start": at, "end": at + 7200, "title": ["Sintel", "Big Buck Bunny", "Tears of Steel"][h // 2 % 3],
                                 "summary": "A Blender open movie.", "type": "movie", "categories": ["Movie"]})
        guide["7.1"].append({"start": at, "end": at + 3600, "title": "Rex Football Live", "summary": "Live match coverage.",
                             "type": "movie", "categories": ["Sports", "Football"], "new": True})
    return guide


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rex", required=True, help="rex's address, e.g. http://192.168.1.20:32400")
    parser.add_argument("--port", type=int, default=18080, help="the port this source serves its channels on")
    parser.add_argument("--host", default="127.0.0.1", help="the address rex reaches this source at")
    args = parser.parse_args()
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    source = LiveSource(ffmpeg, ffprobe, args.port)
    source.url = "http://%s:%d" % (args.host, args.port)
    try:
        source.wait()
        guide = week()
        items = [source.item(number, title, {"number": number, "title": title, "call_sign": title, "guide": guide[number]})
                 for number, title, _ in CHANNELS]
        print(push(args.rex.rstrip("/"), source, items))
        print("serving %d live channels at %s; Ctrl-C ends them" % (len(CHANNELS), source.url))
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        source.close()


if __name__ == "__main__":
    main()
