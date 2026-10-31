#!/usr/bin/env python3
"""A listing of hundreds of albums stays fast whether their artwork is analysed yet or not.

`thumbBlurHash` and `UltraBlurColors` come from the artwork (`test_artwork_blur.py`), which a
source may serve slowly: here each of 400 covers takes 40 ms, 16 s for all of them. rex reads them
on a thread of its own, outside the runtime lock, so neither the first listing (nothing analysed)
nor a request beside it waits for one, and once all are kept the listing is as cheap as before:
both cost a few times what this client takes to parse the answer (`client_unit`).

    python3 tests/test_perf_blur.py
"""
import http.server
import json
import os
import shutil
import socketserver
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
import zlib

# This suite measures the analysis every other suite's server leaves off (`server_command.py`).
os.environ["REX_ARTWORK_ANALYSIS"] = "on"

import music_rig as M
from music_rig import check, results
from startup_wait import poll_until
from test_perf_listing_window import client_unit
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = json.load(open(os.path.join(HERE, "fixtures", "music", "media.json")))["tracks"][0]["probe"]
ALBUMS = 400
DELAY = 0.04
LISTING = "/library/sections/1/all?type=9&includeFields=thumbBlurHash"


def png(color, size=64):
    """A solid PNG, written here so no picture is checked in for it."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    rows = b"".join(b"\x00" + bytes(color) * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def slow_server(images):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            time.sleep(DELAY)
            data = images.get(urllib.parse.urlparse(self.path).path.lstrip("/"), b"")
            self.send_response(200 if data else 404)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    port = free_port()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


def timed(rex, path):
    began = time.perf_counter()
    status, headers, body = rex.request(path, raw=True)
    return time.perf_counter() - began, status, body


def main():
    work = tempfile.mkdtemp(prefix="rex-perf-blur-")
    images = {"c%03d.png" % n: png((n % 256, (n * 7) % 256, (n * 13) % 256)) for n in range(ALBUMS)}
    port = slow_server(images)
    agent_url, _ = M.agent_server([])
    rex = M.Rex(work, port, agent_url)
    try:
        source = "http://127.0.0.1:%d" % port
        tracks = [{"id": "m/%03d.flac" % n, "path": "music/A%03d/B/01.flac" % n, "kind": "track", "track": 1,
                   "media_info": PROBE, "display": {"title": "T%03d" % n, "artist": {"title": "Artist %03d" % n},
                                                    "album": {"title": "Album %03d" % n, "poster": "%s/c%03d.png" % (source, n)}}}
                  for n in range(ALBUMS)]
        status, body = rex.push(tracks)
        check("%d albums are created" % ALBUMS, status == 200 and body.get("created") == ALBUMS, (status, body))

        took, status, body = timed(rex, LISTING)
        unit = client_unit(body)
        rows = json.loads(body)["MediaContainer"].get("Metadata") or []
        hashed = sum("thumbBlurHash" in row for row in rows)
        print("unit (parsing the %d-album listing here): %.4fs; first listing %.3fs, %d hashed" % (len(rows), unit, took, hashed))
        check("the first listing answers every album at once, almost none analysed yet, under 40 units (%.3fs, %.1f)" % (took, took / unit),
              status == 200 and len(rows) == ALBUMS and hashed < ALBUMS / 4 and took < 40 * unit, (took, unit, hashed))

        # While the covers are still being read: listings and a request beside them do not wait.
        runs, waits = [], []
        for _ in range(5):
            timing = {}
            thread = threading.Thread(target=lambda: timing.update(listing=timed(rex, LISTING)[0]))
            thread.start()
            waits.append(timed(rex, "/library/sections")[0])
            thread.join()
            runs.append(timing["listing"])
        took, waited = sorted(runs)[2], sorted(waits)[2]
        check("while covers are read a listing costs under 20 units (%.3fs, %.1f)" % (took, took / unit), took < 20 * unit, (took, unit))
        check("and a request beside it waits under 0.25 s (%.3fs)" % waited, waited < 0.25, waited)

        def all_hashed():
            return all("thumbBlurHash" in row for row in json.loads(timed(rex, LISTING)[2])["MediaContainer"]["Metadata"])
        began = time.time()
        poll_until(all_hashed, ALBUMS * DELAY * 3 + 30)
        check("every cover is analysed in the background (%.1fs)" % (time.time() - began), all_hashed())
        took = sorted(timed(rex, LISTING)[0] for _ in range(5))[2]
        check("analysed, the listing costs under 20 units (%.3fs, %.1f)" % (took, took / unit), took < 20 * unit, (took, unit))
    finally:
        rex.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
