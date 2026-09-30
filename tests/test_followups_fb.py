#!/usr/bin/env python3
"""Follow-ups from HANDOVER §2a that were filed as "oracle bugs the native server does
not copy", each re-measured on a real PMS before either server changed:

- an EMPTY body is never gzipped, whatever the client offers -- driven from the official
  client's own `PUT /updater/check` in `tests/fixtures/reference-walk.jsonl`;
- `/photo/:/transcode` output caches (`max-age=259200`) whatever it transcodes, a
  bundled resource and an uploaded poster included, where the same files read directly
  are `no-cache`;
- a collection's `/children` container carries `librarySectionID` as a NUMBER;
- a bare `OPTIONS` on a multi-segment path reaches only the handlers that take any
  verb, and is the ordinary 404 on every GET-only read route;
- `GET /services/codecs` is a claimed server's 403 (never ported to the native server).

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_fb.py
"""
import http.client
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
FIXTURE = os.path.join(HERE, "fixtures", "reference-walk.jsonl")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "followups-fb",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome"}

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    sent = dict(HEADERS, Connection="close")
    sent.update(headers or {})
    try:
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    return response.status, response.getheaders(), raw


def mc(port, path, method="GET"):
    status, _, raw = request(port, method, path)
    try:
        return json.loads(raw).get("MediaContainer", {}) if raw else {}
    except ValueError:
        return {"_status": status}


def raw_exchange(port, data, wait=5.0):
    """Send raw bytes; return (bytes received, how the socket ended, seconds taken)."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    started = time.time()
    received, ended = b"", "open"
    try:
        sock.sendall(data)
        sock.settimeout(wait)
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                ended = "EOF"
                break
            received += chunk
    except socket.timeout:
        pass
    except ConnectionResetError:
        ended = "RST"
    finally:
        sock.close()
    return received, ended, time.time() - started


def head_names(received):
    head = received.partition(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
    return head[0], [line.split(":", 1)[0] for line in head[1:] if line], \
        {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip() for line in head[1:] if ":" in line}


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            if request(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def png(width, height):
    """A real (grey) PNG, so nothing downstream can refuse it as not-an-image."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    rows = b"".join(b"\0" + b"\x80\x80\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


# --- an empty body is never gzipped -------------------------------------------------

def empty_body(port):
    # The official client's `PUT /updater/check`, exactly as kids received it: the query
    # in order (token dropped) and every header but `Host`. kids answered it with
    # `Content-Length: 0`, `text/html`, no encoding pair, the length before the type.
    exchange = None
    with open(FIXTURE) as corpus:
        for line in corpus:
            row = json.loads(line)
            if row["method"] == "PUT" and row["path"] == "/updater/check":
                exchange = row
                break
    query = urllib.parse.urlencode([(k, v) for k, v in exchange["query"] if k != "X-Plex-Token"])
    head = "%s %s?%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % (
        exchange["method"], exchange["path"], query, port)
    for name, value in exchange["req_headers"]:
        if name.lower() in ("host", "connection"):
            continue
        head += "%s: %s\r\n" % (name, value)
    head += "Connection: close\r\n\r\n"
    received, _, _ = raw_exchange(port, head.encode("latin-1"))
    status, names, values = head_names(received)
    body = received.partition(b"\r\n\r\n")[2]
    # The reference's own order, with its keep-alive pair read as the close this request
    # asked for.
    want = []
    for name, _value in exchange["res_headers"]:
        if name == "Keep-Alive":
            continue
        want.append(name)
    check("the client's PUT /updater/check answers an EMPTY, UNCOMPRESSED body",
          status == "HTTP/1.1 %d %s" % (exchange["status"], exchange["reason"])
          and body == b"" and values.get("content-length") == "0"
          and values.get("content-type") == "text/html"
          and "content-encoding" not in values
          and "x-plex-content-original-length" not in values
          and "x-plex-content-compressed-length" not in values,
          "%s %s body=%r" % (status, values, body[:40]))
    check("and in the reference's header order, the length before the type",
          names == want, "got %s want %s" % (names, want))

    # A 204 is an empty body too: the playlist delete, with gzip offered.
    identity = mc(port, "/identity")
    uri = "server://%s/com.plexapp.plugins.library/library/metadata/1007" % identity.get(
        "machineIdentifier")
    made = mc(port, "/playlists?type=video&title=fb204&smart=0&uri=%s"
              % urllib.parse.quote(uri, safe=""), "POST")
    key = (made.get("Metadata") or [{}])[0].get("ratingKey")
    received, _, _ = raw_exchange(port, (
        "DELETE /playlists/%s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nAccept: application/json\r\n"
        "Accept-Encoding: gzip, deflate, br, zstd\r\nConnection: close\r\n\r\n"
        % (key, port)).encode())
    status, names, values = head_names(received)
    check("a 204 with gzip offered carries no body and no encoding pair",
          key and status.startswith("HTTP/1.1 204") and received.partition(b"\r\n\r\n")[2] == b""
          and "content-encoding" not in values
          and "x-plex-content-original-length" not in values,
          "%s %s" % (status, names))


# --- transcoder output caches ----------------------------------------------------------

def transcoder_cache(port):
    def cache(path):
        status, headers, _ = request(port, "GET", path)
        return status, dict((k.lower(), v) for k, v in headers).get("cache-control")
    # kids, 2026-09-30, repeated: 200 `max-age=259200` through the transcoder, 200
    # `no-cache` read directly.
    through = cache("/photo/:/transcode?width=100&height=100&url=%s"
                    % urllib.parse.quote("/:/resources/movie.png", safe=""))
    direct = cache("/:/resources/movie.png")
    check("a bundled resource through /photo/:/transcode caches for three days",
          through == (200, "max-age=259200"), through)
    check("and the same resource read directly is no-cache", direct == (200, "no-cache"), direct)

    status, _, _ = request(port, "POST", "/library/metadata/1007/posters", body=png(20, 30),
                           headers={"Content-Type": "image/png"})
    thumb = (mc(port, "/library/metadata/1007").get("Metadata") or [{}])[0].get("thumb")
    through = cache("/photo/:/transcode?width=100&height=150&url=%s"
                    % urllib.parse.quote(thumb or "", safe=""))
    direct = cache(thumb or "/")
    check("an UPLOADED poster through /photo/:/transcode caches for three days",
          status == 200 and thumb and through == (200, "max-age=259200"),
          "upload %s thumb %s -> %s" % (status, thumb, through))
    check("and read directly it is no-cache", direct == (200, "no-cache"), direct)


# --- a collection's /children names its section by number ------------------------------

def collection_section_id(port):
    identity = mc(port, "/identity")
    uri = "server://%s/com.plexapp.plugins.library/library/metadata/1007" % identity.get(
        "machineIdentifier")
    made = mc(port, "/library/collections?type=1&title=fbSection&smart=0&sectionId=1&uri=%s"
              % urllib.parse.quote(uri, safe=""), "POST")
    key = (made.get("Metadata") or [{}])[0].get("ratingKey")
    # plexscratch42 (1.42.2), 2026-09-30, a movie collection holding one movie, paged and
    # unpaged: `librarySectionID: 1` on the container, and on each row of
    # `/library/collections/{id}/children`.
    seen = {}
    for suffix in ("", "?X-Plex-Container-Start=0&X-Plex-Container-Size=20"):
        container = mc(port, "/library/metadata/%s/children%s" % (key, suffix))
        seen["metadata" + suffix[:1]] = container.get("librarySectionID")
        rows = mc(port, "/library/collections/%s/children%s" % (key, suffix)).get("Metadata") or []
        seen["collections" + suffix[:1]] = [row.get("librarySectionID") for row in rows]
    check("a collection's /children names its section as a NUMBER",
          key and seen == {"metadata": 1, "metadata?": 1, "collections": [1], "collections?": [1]},
          seen)


# --- a bare OPTIONS on a multi-segment path --------------------------------------------

# Measured 2026-09-30 on kids (1.43.4) and, for the writers kids must not be sent,
# plexscratch42 (1.42.2): (status, content type) for a bare OPTIONS with no parameters.
OPTIONS_MEASURED = {
    "/hubs/home/onDeck": (404, "text/html"),
    "/hubs/home/continueWatching": (404, "text/html"),
    "/hubs/continueWatching/items": (404, "text/html"),
    "/hubs/search/voice": (404, "text/html"),
    "/accounts/1": (404, "text/html"),
    "/myplex/account": (404, "text/html"),
    "/channels/recentlyViewed": (404, "text/html"),
    "/library/search": (404, "text/html"),
    "/library/all/top": (404, "text/html"),
    "/livetv/sessions": (404, "text/html"),
    "/media/providers/metadata/group": (404, "text/html"),
    "/status/sessions/background": (404, "text/html"),
    "/:/websockets/notifications": (404, "text/html"),
    "/:/eventsource/notifications": (404, "text/html"),
    "/actions/removeFromContinueWatching": (404, "text/html"),
    "/diagnostics/logs": (404, "text/html"),
    "/library/sections": (404, "text/html"),
    "/library/metadata/1007": (404, "text/html"),
    "/:/resources/movie.png": (200, "image/png"),
    "/photo/:/transcode": (400, "text/html"),
    "/video/:/transcode/universal/decision": (400, "text/html"),
    "/video/:/transcode/universal/start": (400, "text/html"),
    "/subtitles/:/transcode/universal/start": (400, "text/html"),
    "/:/scrobble": (400, "text/html"),
    "/:/timeline": (400, "text/html"),
    "/:/progress": (400, "text/html"),
    "/security/resources": (400, "text/html"),
    "/security/token": (400, "text/html"),
    "/status/sessions/terminate": (400, "text/html"),
    "/services/codecs": (403, "text/html"),
    "/log/networked": (200, "text/html"),
}


def options(port):
    wrong = {}
    for path, want in OPTIONS_MEASURED.items():
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("OPTIONS", path, headers={"Connection": "close"})
            response = connection.getresponse()
            response.read()
            got = (response.status, (response.getheader("Content-Type") or "").split(";")[0])
        except (OSError, http.client.HTTPException) as error:
            got = type(error).__name__
        finally:
            connection.close()
        if got != want:
            wrong[path] = (got, want)
    check("a bare OPTIONS answers what a real PMS does on %d multi-segment paths"
          % len(OPTIONS_MEASURED), not wrong, wrong)


# --- /services/codecs ---------------------------------------------------------------------

def services_codecs(port):
    # kids (1.43.4), claimed like this server, 2026-09-30: GET is the 403 (cycle 45).
    status = request(port, "GET", "/services/codecs")[0]
    check("GET /services/codecs is the claimed server's 403", status == 403, status)


def run(port):
    empty_body(port)
    transcoder_cache(port)
    collection_section_id(port)
    options(port)
    services_codecs(port)


def main():
    process, port = start()
    try:
        run(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
