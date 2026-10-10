#!/usr/bin/env python3
"""simplex's MEDIA BYTE path against what a real PMS answers. No credential needed.

For the whole programme the part path could not be checked: every provider link in
the checked-in fixture is account-scoped and dead, so `tests/test_all.py` reported
`0/11 served` for fifteen cycles and every rule about a media body was measured on a
REFERENCE and implemented on faith. Cycle 44 found thirteen of twenty `Range` cells
wrong on that path, including two that break every seek a real player makes.

So this suite stands up the upstream instead of asking for one:

    local zurg  -- WebDAV file reads with ordinary byte-range semantics, behind basic
                   auth, answering 404 for a file it no longer has
    simplex     -- started with ZURG_URL pointed at the stub, on a generated catalog
                   whose Part files sit under `--zurg-mount`

and then drives simplex's real handler. The expectations are the values kids (1.43.4)
and zen (1.42.2) answer for the same header against their own parts, recorded in
`EXPECTED` below with the measurement beside each one.

    python3 tests/test_part_bytes.py
"""
import http.server
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from server_command import server_command

ROOT = os.environ.get("SIMPLEX_TEST_ROOT",
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIZE = 100000
BODY = bytes(((i * 7 + 13) % 251) for i in range(SIZE))

results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print("  %s  %-58s %s" % ("PASS" if ok else "FAIL", label, detail))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Quiet(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass


UNAVAILABLE = {"on": False}
AUTH = "Basic " + __import__("base64").b64encode(b"zurg-user:zurg-pass").decode()
SEEN = {"paths": []}


class Zurg(_Quiet):
    """zurg's WebDAV file read: what simplex streams a Part from."""

    def _gate(self):
        SEEN["paths"].append(self.path)
        if self.headers.get("Authorization") != AUTH:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        if UNAVAILABLE["on"] and "gone" in self.path:
            # What zurg answers for a file that is no longer in the library.
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def do_HEAD(self):
        if not self._gate():
            return
        self.send_response(200)
        self.send_header("Content-Length", str(SIZE))
        self.end_headers()

    def do_GET(self):
        if not self._gate():
            return
        spec = self.headers.get("Range")
        first, last = 0, SIZE - 1
        partial = False
        if spec and spec.startswith("bytes="):
            match = re.match(r"bytes=(\d*)-(\d*)$", spec)
            if match:
                lo, hi = match.group(1), match.group(2)
                if lo == "" and hi:
                    first, last, partial = max(0, SIZE - int(hi)), SIZE - 1, True
                elif lo:
                    first = int(lo)
                    last = min(int(hi), SIZE - 1) if hi else SIZE - 1
                    partial = True
                if first >= SIZE or last < first:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % SIZE)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
        chunk = BODY[first:last + 1]
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "application/force-download")
        self.send_header("Content-Length", str(len(chunk)))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (first, last, SIZE))
        self.end_headers()
        self.wfile.write(chunk)


def serve(handler):
    port = free_port()
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port, httpd


CATALOG = {
    "machineIdentifier": "0000000000000000000000000000000000000001",
    "friendlyName": "simplex-part-bytes",
    "sections": [{"key": "1", "type": "movie", "title": "Bytes",
                  "uuid": "00000000000000000000000000000000000000ff"}],
    "items": [{
        "ratingKey": "1", "key": "/library/metadata/1",
        "librarySectionID": 1, "librarySectionTitle": "Bytes",
        "librarySectionKey": "/library/sections/1",
        "type": "movie", "title": "Byte Movie", "year": 2026,
        "guid": "plex://movie/000000000000000000000001", "duration": 60000,
        "addedAt": 1700000000, "updatedAt": 1700000000,
        "Media": [{"id": 100, "container": "mp4", "videoCodec": "h264",
                   "audioCodec": "aac", "width": 1280, "height": 720,
                   "duration": 60000, "videoResolution": "720", "bitrate": 1000,
                   "aspectRatio": 1.78, "audioChannels": 2,
                   "Part": [{"id": 10, "container": "mp4", "size": SIZE,
                             "duration": 60000,
                             "key": "/library/parts/10/1700000000/file.mp4",
                             "file": "/zurg/movies/Byte Movie (2026) +1/byte movie.mp4",
                             "Stream": [
                                 {"id": 1001, "streamType": 1, "codec": "h264",
                                  "index": 0, "width": 1280, "height": 720},
                                 {"id": 1002, "streamType": 2, "codec": "aac",
                                  "index": 1, "channels": 2, "selected": True},
                             ]}]}],
    }, {
        # A second row: the file zurg stops serving.
        "ratingKey": "2", "key": "/library/metadata/2",
        "librarySectionID": 1, "librarySectionTitle": "Bytes",
        "librarySectionKey": "/library/sections/1",
        "type": "movie", "title": "Gone Movie", "year": 2026,
        "guid": "plex://movie/000000000000000000000002", "duration": 60000,
        "addedAt": 1700000000, "updatedAt": 1700000000,
        "Media": [{"id": 200, "container": "mp4", "videoCodec": "h264",
                   "audioCodec": "aac", "width": 1280, "height": 720,
                   "duration": 60000, "videoResolution": "720", "bitrate": 1000,
                   "aspectRatio": 1.78, "audioChannels": 2,
                   "Part": [{"id": 20, "container": "mp4", "size": SIZE,
                             "duration": 60000,
                             "key": "/library/parts/20/1700000000/file.mp4",
                             "file": "/zurg/movies/Gone Movie (2026)/gone movie.mp4",
                             "Stream": [
                                 {"id": 2001, "streamType": 1, "codec": "h264",
                                  "index": 0, "width": 1280, "height": 720},
                                 {"id": 2002, "streamType": 2, "codec": "aac",
                                  "index": 1, "channels": 2, "selected": True},
                             ]}]}],
    }],
}

# Every cell below is what BOTH references answer for that header against their own
# part, measured cycle 44 on kids 1.43.4 (part 1) and zen 1.42.2 (part 4075), and
# expressed against this suite's own body size. `None` for a header means "send none".
#   (label, Range header or None, expected status, expected Content-Range,
#    expected Content-Length, expected first byte offset or None for "whole body")
N = SIZE
EXPECTED = [
    ("no Range", None, 200, None, N, 0),
    ("Range: (empty)", "", 200, None, N, 0),
    ("bytes=0-", "bytes=0-", 206, "bytes 0-%d/%d" % (N - 1, N), N, 0),
    ("bytes=0-0", "bytes=0-0", 206, "bytes 0-0/%d" % N, 1, 0),
    ("bytes=5-9", "bytes=5-9", 206, "bytes 5-9/%d" % N, 5, 5),
    ("bytes=-1 (the LAST byte)", "bytes=-1", 206,
     "bytes %d-%d/%d" % (N - 1, N - 1, N), 1, N - 1),
    ("bytes=-0", "bytes=-0", 206, "bytes 0-0/%d" % N, N, 0),
    ("suffix longer than the body", "bytes=-%d" % (N + 1000), 206,
     "bytes 0-%d/%d" % (N - 1, N), N, 0),
    ("unparseable first", "bytes=abc-100", 206, "bytes 0-%d/%d" % (N - 1, N), N, 0),
    ("unparseable last", "bytes=0-abc", 206, "bytes 0-0/%d" % N, 1, 0),
    ("a second dash", "bytes=0-1-2", 206, "bytes 0-%d/%d" % (N - 1, N), N, 0),
    ("reversed range is a 416", "bytes=100-50", 416, None, 129, None),
    ("first at the end is a 416", "bytes=%d-" % N, 416, None, 129, None),
    ("first past the end is a 416", "bytes=%d-" % (N + 10), 416, None, 129, None),
    ("no '=' is not a Range", "bytes 0-10", 200, None, N, 0),
    ("bytes= is not a Range", "bytes=", 200, None, N, 0),
    ("bytes=- is not a Range", "bytes=-", 200, None, N, 0),
    ("a comma list is the whole body", "bytes=0-1,5-6", 206,
     "bytes 0-%d/%d" % (N - 1, N), N, 0),
    ("the unit is not read", "items=0-10", 206, "bytes 0-10/%d" % N, 11, 0),
    ("a last past the end is clamped", "bytes=0-%d" % (N + 1000), 206,
     "bytes 0-%d/%d" % (N - 1, N), N, 0),
]


def raw(port, path, headers=(), method="GET"):
    sock = socket.create_connection(("127.0.0.1", port), timeout=30)
    request = "%s %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % (method, path, port)
    for key, value in headers:
        request += "%s: %s\r\n" % (key, value)
    request += "Connection: close\r\n\r\n"
    sock.sendall(request.encode())
    buffer = b""
    while True:
        piece = sock.recv(65536)
        if not piece:
            break
        buffer += piece
    sock.close()
    head, _, body = buffer.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    names = [line.split(":", 1)[0] for line in lines[1:] if ":" in line]
    values = {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
              for line in lines[1:] if ":" in line}
    return int(lines[0].split()[1]), names, values, body


def main():
    zurg_port, _zurg = serve(Zurg)
    catalog = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(CATALOG, catalog)
    catalog.close()
    port = free_port()
    environment = dict(os.environ, ZURG_URL="http://127.0.0.1:%d" % zurg_port,
                       ZURG_USER="zurg-user", ZURG_PASSWORD="zurg-pass")
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", catalog.name, "--assert-claimed",
                       "--zurg-mount", "/zurg", root=ROOT),
        cwd=ROOT, env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(160):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=2)
                break
            except Exception:
                time.sleep(0.25)
        # Read the Part's own `key` back rather than composing it: the version stamp
        # is the row's, and a check that guesses it measures a 404.
        with urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:%d/library/metadata/1" % port,
                headers={"Accept": "application/json"}), timeout=15) as response:
            row = json.load(response)["MediaContainer"]["Metadata"][0]
        key = row["Media"][0]["Part"][0]["key"]
        status, _n, _v, body = raw(port, key)
        check("the local upstream serves the part at all",
              status == 200 and len(body) == SIZE, "%s %d bytes" % (status, len(body)))
        check("the part is read from zurg's WebDAV path for its file, with zurg's credential",
              "/dav/movies/Byte%20Movie%20%282026%29%20%2B1/byte%20movie.mp4" in SEEN["paths"],
              SEEN["paths"][-3:])

        # -- the measured Range grammar, cell by cell ------------------------------
        wrong = []
        for label, spec, want_status, want_range, want_length, want_first in EXPECTED:
            headers = [("Accept", "*/*")]
            if spec is not None:
                headers.append(("Range", spec))
            status, _names, values, body = raw(port, key, headers)
            got_length = values.get("content-length")
            got_range = values.get("content-range")
            ok = (status == want_status
                  and got_range == want_range
                  and got_length == str(want_length)
                  and (want_first is None or body[:1] == BODY[want_first:want_first + 1])
                  and len(body) == (want_length if status != 416 else len(body)))
            if not ok:
                wrong.append("%s -> %s cr=%s cl=%s (want %s cr=%s cl=%s)"
                             % (label, status, got_range, got_length,
                                want_status, want_range, want_length))
        check("every measured Range cell on a media body",
              not wrong, "%d/%d wrong%s" % (len(wrong), len(EXPECTED),
                                            ": " + "; ".join(wrong[:3]) if wrong else ""))

        # -- the envelope ----------------------------------------------------------
        status, names, values, _body = raw(port, key, [("Accept", "*/*"),
                                                       ("Range", "bytes=0-9")])
        check("a part read leads with X-Plex-Protocol",
              names[:1] == ["X-Plex-Protocol"], str(names[:4]))
        check("the part header order is the measured one",
              [n for n in names if n.lower() not in ("access-control-allow-origin",)]
              == ["X-Plex-Protocol", "ETag", "Last-Modified", "Accept-Ranges",
                  "Content-Range", "Connection", "Content-Length", "Content-Type",
                  "Date"], str(names))
        status, names, values, _body = raw(port, key, [("Accept", "*/*"),
                                                       ("Origin", "https://app.plex.tv"),
                                                       ("Range", "bytes=0-9")])
        check("with an Origin the CORS pair follows X-Plex-Protocol",
              names[:3] == ["X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary"]
              and names[-2:] == ["Access-Control-Allow-Origin", "Date"], str(names[:4]))

        # -- the Content-Type is the stored FILE's extension ------------------------
        check("the Content-Type follows the file's extension",
              values.get("content-type") == "video/mp4", values.get("content-type"))

        # Cycle 54: every extension a real PMS names, measured on plexscratch42 (1.42.2)
        # by reading one part of each container, and the four it leaves as a byte stream.
        sys.path.insert(0, ROOT)
        import server as server_module_c54
        measured_c54 = {"mkv": "video/x-matroska", "mp4": "video/mp4", "m4v": "video/mp4",
                        "webm": "video/webm", "avi": "video/x-msvideo",
                        "divx": "video/x-msvideo", "mov": "video/quicktime",
                        "ts": "video/MP2T", "mpg": "video/mpeg", "mpeg": "video/mpeg",
                        "flv": "video/x-flv", "wmv": "video/x-ms-wmv",
                        "asf": "video/x-ms-asf", "mts": "application/octet-stream",
                        "m2ts": "application/octet-stream",
                        "ogv": "application/octet-stream",
                        "3gp": "application/octet-stream",
                        "mpegts": "application/octet-stream"}
        wrong_c54 = ["%s -> %s" % (ext, server_module_c54._part_content_type(
            {"file": "/x/movie.%s" % ext}, None)) for ext, want in measured_c54.items()
            if server_module_c54._part_content_type({"file": "/x/movie.%s" % ext}, None)
            != want]
        check("the part Content-Type of every measured extension", not wrong_c54,
              "; ".join(wrong_c54))

        # -- the 3-segment media path is READ-ONLY ---------------------------------
        codes = {}
        for method in ("GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"):
            codes[method] = raw(port, key + "?audioStreamID=1002", method=method)[0]
        check("the 3-segment media path answers 404 to a write",
              codes["POST"] == 404 and codes["PUT"] == 404
              and codes["GET"] == 200 and codes["HEAD"] == 200, str(codes))

        # -- a 416 on a media body carries no connection state and no local-asset --
        status, names, values, _body = raw(port, key, [("Accept", "*/*"),
                                                       ("Range", "bytes=999999-")])
        check("a media 416 is the reference's header set",
              status == 416
              and [n for n in names if n != "Access-Control-Allow-Origin"]
              == ["X-Plex-Protocol", "Content-Length", "Content-Type",
                  "Cache-Control", "Date"]
              and values.get("cache-control") == "no-cache", "%s %s" % (status, names))

        # -- the byte counter counts what was WRITTEN ------------------------------
        def bandwidth():
            with urllib.request.urlopen(urllib.request.Request(
                    "http://127.0.0.1:%d/statistics/bandwidth?timespan=6" % port,
                    headers={"Accept": "application/json"}), timeout=10) as response:
                raw_body = response.read()
            payload = json.loads(raw_body) if raw_body[:1] == b"{" else {}
            rows = (payload.get("MediaContainer") or {}).get("StatisticsBandwidth") or []
            return sum(int(r.get("bytes") or 0) for r in rows)

        before = bandwidth()
        raw(port, key, [("Accept", "*/*"), ("Range", "bytes=0-999")])
        after = bandwidth()
        check("a ranged read counts its own bytes, not the whole file",
              0 < after - before <= 2000, "delta=%d (a 1000-byte read)" % (after - before))

        # -- an UNAVAILABLE file is a 404, not a 502 -------------------------------
        # Measured on zen part 23595, whose `checkFiles=1` reports
        # `accessible=false`/`exists=false`: the byte read is a plain 404 with the
        # binary refusal header set. simplex answered 502 to a dead link and to a
        # network failure alike.
        UNAVAILABLE["on"] = True
        try:
            status, names, values, _body = raw(
                port, "/library/parts/20/1700000000/file.mp4", [("Accept", "*/*")])
        finally:
            UNAVAILABLE["on"] = False
        check("an unavailable file answers 404, not 502",
              status == 404 and values.get("content-type") == "text/html",
              "%s %s" % (status, values.get("content-type")))

        # -- checkFiles reflects the same provider result -------------------------
        # Cycle 46's first real comparison of this metadata claim with simplex's own
        # byte path found five false positives: every stale private-catalog link said
        # accessible=true/exists=true and immediately answered unavailable_file/404.
        # Both references expose false/false for a missing file, and the live/dead
        # pair below drives zurg's real answer rather than a catalog flag.
        def part_state(rating_key, option):
            with urllib.request.urlopen(urllib.request.Request(
                    "http://127.0.0.1:%d/library/metadata/%s?%s=1"
                    % (port, rating_key, option),
                    headers={"Accept": "application/json"}), timeout=30) as response:
                item = json.load(response)["MediaContainer"]["Metadata"][0]
            item_part = item["Media"][0]["Part"][0]
            return item_part.get("accessible"), item_part.get("exists")

        live_states = {option: part_state("1", option) for option in
                       ("checkFiles", "asyncCheckFiles", "checkFileAvailability")}
        UNAVAILABLE["on"] = True
        try:
            dead_states = {option: part_state("2", option) for option in
                           ("checkFiles", "asyncCheckFiles", "checkFileAvailability")}
        finally:
            UNAVAILABLE["on"] = False
        with urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:%d/library/metadata/2" % port,
                headers={"Accept": "application/json"}), timeout=30) as response:
            unchecked = json.load(response)["MediaContainer"]["Metadata"][0]
        unchecked_part = unchecked["Media"][0]["Part"][0]
        check("file-check options report a live and an unavailable file",
              set(live_states.values()) == {(True, True)}
              and set(dead_states.values()) == {(False, False)}
              and "accessible" not in unchecked_part and "exists" not in unchecked_part,
              "live=%r dead=%r unchecked=%r"
              % (live_states, dead_states,
                 (unchecked_part.get("accessible"), unchecked_part.get("exists"))))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()
        os.unlink(catalog.name)

    passed = sum(1 for _, ok in results if ok)
    print("%d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
