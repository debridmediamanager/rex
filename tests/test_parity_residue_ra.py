#!/usr/bin/env python3
"""The UI suite's residue, exhausted: what was left after replaying the whole suite's
request stream (the oracle's, then the native server's own) against both servers in
lockstep and neutralising what is random or per-run by measurement.

Each check asserts the MEASURED answer, not the other server's:

- a refusal at `/system/agents` orders its length before its type (kids, 1.43.4:
  `?mediaType=abc` is the 89-byte 400), although the container there is type-first;
- an embedded stream's 501 carries the reason phrase `Not Implemented` (kids), and an
  uploaded sidecar's 200 orders its length before its type (plexscratch42, cycle 74);
- the photo transcoder (and ultrablur through it) 500s an uploaded poster whose bytes are
  not an image, the 200-that-does-not-decode rule of cycle 52;
- a `random="1"` hub shelf is a random sample of its whole set in random order, held per
  (shelf, count) for the life of the process (zen, cycles 28 and 30).

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `SIMPLEX_SERVER=python`.

    python3 tests/test_parity_residue_ra.py
"""
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import zlib

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           SIMPLEX_PLEXTV_URL=os.environ.get("SIMPLEX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("SIMPLEX_STATE", "SIMPLEX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = [("Accept", "application/json"), ("X-Plex-Client-Identifier", "parity-residue-ra"),
           ("X-Plex-Product", "Plex Web"), ("X-Plex-Version", "4.147.1"),
           ("X-Plex-Platform", "Chrome")]

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def raw(port, method, path, body=None, headers=()):
    """One request on its own socket: (status, reason, [header names in order], headers, body)."""
    lines = ["%s %s HTTP/1.1" % (method, path), "Host: 127.0.0.1:%d" % port]
    lines += ["%s: %s" % pair for pair in HEADERS + list(headers)]
    if body is not None:
        lines.append("Content-Length: %d" % len(body))
    lines.append("Connection: close")
    sock = socket.create_connection(("127.0.0.1", port), 30)
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b""))
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    parts = status_line.split(" ", 2)
    names, values = [], {}
    for line in header_lines:
        name, _, value = line.partition(":")
        names.append(name.strip())
        values[name.strip().lower()] = value.strip()
    return int(parts[1]), parts[2] if len(parts) > 2 else "", names, values, payload


def order(names):
    """The response's own headers, less the ones whose place nothing here is about."""
    return [name for name in names if name.lower() not in ("keep-alive",)]


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if process.poll() is not None:
            break
        try:
            if raw(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


# Measured 2026-09-30 on kids (1.43.4), raw socket, Connection: close.
ERROR_ORDER = ["X-Plex-Protocol", "Content-Length", "Content-Type", "Connection",
               "Cache-Control", "Date"]


def run(port):
    status, reason, names, _, body = raw(port, "GET", "/system/agents?mediaType=abc")
    check("a /system/agents refusal is the 400 with its length before its type",
          status == 400 and order(names) == ERROR_ORDER and len(body) == 89,
          (status, names, len(body)))
    status, reason, names, _, _ = raw(port, "GET", "/system/agents")
    check("... while the /system/agents container stays type-first",
          status == 200 and order(names)[:3] == ["X-Plex-Protocol", "Content-Type",
                                                 "Content-Length"], (status, names))

    # An EMBEDDED stream (1007's first) is the 501, measured on kids for the bare id,
    # `.srt` and `.vtt` alike: `HTTP/1.1 501 Not Implemented`, 97 bytes, length first.
    for suffix in ("", ".srt", ".vtt"):
        status, reason, names, _, body = raw(port, "GET", "/library/streams/1007001" + suffix)
        check("an embedded stream%s is `501 Not Implemented`, length before type" % suffix,
              status == 501 and reason == "Not Implemented" and order(names) == ERROR_ORDER
              and len(body) == 97, (status, reason, names, len(body)))
    # An uploaded sidecar's key serves its bytes as a 200 `text/html` with its length
    # before its type (cycle 74, plexscratch42), `.srt` and `autoAdjustSubtitle=` alike.
    srt = b"1\n00:00:00,000 --> 00:00:02,000\nresidue\n\n"
    status, _, _, _, _ = raw(port, "POST", "/library/metadata/1007/subtitles?title=ra.srt&format=srt",
                             body=srt)
    detail = json.loads(raw(port, "GET", "/library/metadata/1007")[4])
    sidecar = [stream for media in detail["MediaContainer"]["Metadata"][0].get("Media", [])
               for part in media.get("Part", []) for stream in part.get("Stream", [])
               if stream.get("key") and stream.get("streamType") == 3]
    check("the subtitle upload lands a sidecar with a key", status == 200 and sidecar,
          (status, sidecar))
    if sidecar:
        key = sidecar[-1]["key"]
        for tail in ("", "?autoAdjustSubtitle=1"):
            status, reason, names, values, body = raw(port, "GET", key + tail)
            check("a sidecar read%s is the 200 text/html with its length before its type" % tail,
                  status == 200 and reason == "OK" and body == srt
                  and values.get("content-type") == "text/html" and order(names) == ERROR_ORDER,
                  (status, reason, names, body[:40]))

    # The photo transcoder DECODES what the path serves (cycle 52, kids and zen): only a
    # 200 whose bytes are not an image is its 500, and ultrablur resolves its url the same
    # way (cycle 70; kids answers `url=/identity` with that 109-byte 500). An uploaded
    # poster whose bytes are no image is such a 200 on the direct path.
    def thumb():
        rows = json.loads(raw(port, "GET", "/library/metadata/1007")[4])["MediaContainer"]
        return rows["Metadata"][0]["thumb"]

    status = raw(port, "POST", "/library/metadata/1007/posters", body=b"<binary 32 bytes>")[0]
    art = thumb()
    status_direct, _, _, values_direct, _ = raw(port, "GET", art)
    status, _, names, values, body = raw(
        port, "GET", "/photo/:/transcode?width=200&height=300&url=%s" % urllib.parse.quote(art, safe=""))
    # Cycle 83, plexscratch42: an upload reads back `image/jpeg` whatever its bytes
    # (`tests/fixtures/upload_non_image.json`), text and PNG alike, directly and through
    # the transcoder -- the octet-stream and image/png this checked were simplex's sniff.
    check("a non-image upload's direct read is a 200 image/jpeg, and the transcoder 500s it",
          status_direct == 200 and values_direct.get("content-type") == "image/jpeg"
          and status == 500 and len(body) == 109 and order(names) == ERROR_ORDER,
          (status_direct, values_direct.get("content-type"), status, names, body[:60]))
    for leaf in ("/service/ultrablur/colors", "/services/ultrablur/colors"):
        status, _, names, _, body = raw(port, "GET", leaf + "?url=%2Flibrary%2Fmetadata%2F1007%2Fthumb")
        check("%s of that non-image thumb is the transcoder's 500" % leaf,
              status == 500 and len(body) == 109 and order(names) == ERROR_ORDER,
              (status, names, body[:60]))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30")) + chunk(b"IEND", b""))
    raw(port, "POST", "/library/metadata/1007/posters", body=png)
    art = thumb()
    status, _, names, values, body = raw(
        port, "GET", "/photo/:/transcode?width=200&height=300&url=%s" % urllib.parse.quote(art, safe=""))
    check("... while an uploaded PNG transcodes to itself, cached three days",
          status == 200 and body == png and values.get("content-type") == "image/jpeg"
          and values.get("cache-control") == "max-age=259200", (status, values, body[:20]))
    status, _, names, _, body = raw(port, "GET", "/service/ultrablur/colors?url=%2Flibrary%2Fmetadata%2F1007%2Fthumb")
    check("... and ultrablur answers its container, type first",
          status == 200 and b"UltraBlurColors" in body
          and order(names)[:3] == ["X-Plex-Protocol", "Content-Type", "Content-Length"],
          (status, names, body[:60]))


def chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def shelf(port, count):
    hubs = json.loads(raw(port, "GET", "/hubs/sections/1?count=%d" % count)[4])
    for hub in hubs["MediaContainer"].get("Hub", []):
        if hub["hubIdentifier"] == "movie.topunwatched.1":
            return [row["ratingKey"] for row in hub.get("Metadata", [])], hub.get("random")
    return [], None


def run_random_shelves(starts=6):
    """Cycles 28 and 30 (zen): a `random="1"` shelf is a random SAMPLE of its whole set in
    random order, stable per (shelf, `count=`) for the life of the process. Over six fresh
    processes a real sampler repeats one six-row order every time with p = 720**-5 and one
    two-row sample with p = 15**-5; a sorted head does both every time."""
    orders, pairs, stable, flagged = set(), set(), True, True
    for _ in range(starts):
        process, port = start()
        try:
            # The fixture's movies rate below the shelf's 7.0 floor; lift all six over it.
            for key in (1007, 1008, 1009, 1012, 1013, 1014):
                raw(port, "PUT", "/library/metadata/%d?audienceRating.value=8.0" % key)
            first, flag = shelf(port, 40)
            again, _ = shelf(port, 40)
            head, _ = shelf(port, 2)
            head_again, _ = shelf(port, 2)
        finally:
            stop(process)
        stable = stable and first == again and head == head_again and len(first) > 2
        flagged = flagged and flag is True
        orders.add(tuple(first))
        pairs.add(frozenset(head))
    check("Top Unwatched is stable per (shelf, count) within a process, and flagged random",
          stable and flagged, (orders, pairs))
    check("... and its order differs between processes (a random sample, not a sorted head)",
          len(orders) > 1, orders)
    check("... and a count=2 shelf samples the whole set, not its first two rows",
          len(set().union(*pairs)) > 2, pairs)


def main():
    process, port = start()
    try:
        run(port)
    finally:
        stop(process)
    run_random_shelves()
    passed = sum(results)
    print("PASS %d/%d residue checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
