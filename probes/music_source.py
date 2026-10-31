#!/usr/bin/env python3
"""A music source for trying rex's music library by hand: serves audio files, registers, pushes.

It is a source as `docs/feed-api.md` describes one, nothing rex-specific: a Range server over a
folder, `PUT /rex/sources/<name>`, then `POST /rex/feed/<name>/items` with each file as a
`kind: "track"` and its ffprobe JSON. What each file is comes from this script's arguments, never
from tags or names (rex reads neither):

* `--plexav` makes plexav's four albums in `--dir` (its `make-music.sh`, synthetic 45 s tones)
  and pushes each album by its MusicBrainz release id, as `tests/music_rig.py` does;
* otherwise every audio file under `--dir/<artist>/<album>/` is pushed, its album named by
  `--album "<artist>/<album>=<mbid or plex://album/...>"` when given, else shown by `display`
  (artist and album as the folders say, the track as its file, numbered in file order).

The music library must exist first: `--make-library` adds one (`POST /library/sections?type=artist`,
which needs `--token` for a claimed rex) on `/mnt/rex/music`. The script keeps serving until ^C.

    python3 probes/music_source.py --rex http://127.0.0.1:32500 --dir /tmp/music --plexav --make-library --token <rex token>
"""
import argparse
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
AUDIO = (".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav")


def serve(root, port):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            path = os.path.join(root, urllib.parse.unquote(urllib.parse.urlparse(self.path).path).lstrip("/"))
            if not os.path.isfile(path):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            size = os.path.getsize(path)
            first, last, status = 0, size - 1, 200
            match = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
            if match:
                status = 206
                first = int(match.group(1) or 0)
                last = min(int(match.group(2)) if match.group(2) else size - 1, size - 1)
            with open(path, "rb") as handle:
                handle.seek(first)
                body = handle.read(last - first + 1)
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (first, last, size))
            self.end_headers()
            self.wfile.write(body)
    server = socketserver.ThreadingTCPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def call(rex, path, method="GET", body=None, token=None):
    request = urllib.request.Request(rex + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers=dict({"Content-Type": "application/json", "Accept": "application/json"},
                                                  **({"X-Plex-Token": token} if token else {})))
    with urllib.request.urlopen(request, timeout=600) as response:
        raw = response.read()
        return json.loads(raw) if raw[:1] == b"{" else raw


def probe(path):
    return json.loads(subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
                                     check=True, capture_output=True).stdout)


def folder_items(root, albums):
    items = []
    for artist in sorted(os.listdir(root)):
        for album in sorted(os.listdir(os.path.join(root, artist))) if os.path.isdir(os.path.join(root, artist)) else []:
            folder = os.path.join(root, artist, album)
            files = sorted(name for name in os.listdir(folder) if name.lower().endswith(AUDIO))
            named = albums.get("%s/%s" % (artist, album))
            for number, name in enumerate(files, 1):
                relative = "/".join([artist, album, name])
                item = {"id": relative, "path": relative, "kind": "track", "track": number,
                        "media_info": probe(os.path.join(folder, name))}
                if named:
                    item["ids"] = {"plex": named} if named.startswith("plex://") else {"mbid": named}
                item["display"] = {"title": os.path.splitext(name)[0], "artist": {"title": artist}, "album": {"title": album}}
                items.append(item)
    return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rex", default="http://127.0.0.1:32400")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--name", default="music")
    parser.add_argument("--port", type=int, default=9988)
    parser.add_argument("--host", default="127.0.0.1", help="this machine's address as rex reaches it")
    parser.add_argument("--plexav", action="store_true")
    parser.add_argument("--album", action="append", default=[])
    parser.add_argument("--make-library", action="store_true")
    parser.add_argument("--token")
    args = parser.parse_args()
    os.makedirs(args.dir, exist_ok=True)
    if args.plexav:
        import music_rig
        items = music_rig.make_library(args.dir)
        # What this source knows the files are, for a rex Plex's agent cannot be asked from.
        for item in items:
            artist, album = item["path"].split("/")[1:3]
            title = next(titles[item["track"] - 1] for name, record, _, _, _, _, titles in music_rig.ALBUMS if name == artist and album.startswith(record))
            item["display"] = {"title": title, "artist": {"title": artist}, "album": {"title": album.rsplit(" (", 1)[0]}}
        root = args.dir
    else:
        items = folder_items(args.dir, dict(entry.split("=", 1) for entry in args.album))
        root = args.dir
    serve(root, args.port)
    if args.make_library:
        query = urllib.parse.urlencode({"name": "Music", "type": "artist", "agent": "tv.plex.agents.music",
                                        "scanner": "Plex Music", "language": "en-US", "location": "/mnt/rex/music"})
        call(args.rex, "/library/sections?" + query, "POST", token=args.token)
    print(call(args.rex, "/rex/sources/%s" % args.name, "PUT", {"url": "http://%s:%d" % (args.host, args.port)}))
    for start in range(0, len(items), 200):
        print(call(args.rex, "/rex/feed/%s/items" % args.name, "POST", {"items": items[start:start + 200]}))
    print("serving %d tracks from %s on :%d; ^C to stop" % (len(items), root, args.port))
    threading.Event().wait()


if __name__ == "__main__":
    sys.exit(main())
