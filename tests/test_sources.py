#!/usr/bin/env python3
"""Sources are byte servers, and rex assumes nothing of one beyond HTTP.

`docs/feed-api.md`: a source is any HTTP server that answers a GET for a file. This
suite runs two at once, neither of them zurg, each pushing under its own name and token
into the same libraries (an item's `file` places it; its source only says where the bytes are):

- `disk`: Python's own `http.server.SimpleHTTPRequestHandler` over a directory -- the
  real implementation, not a stub of it. It ignores `Range` and answers every GET 200
  with the whole file, as plenty of plain file servers do.
- `front`: basic auth, and every file a 302 to `cdn` on another host, the way a
  storage front hands out presigned links. `cdn` refuses any request carrying an
  `Authorization` header, as S3 does for a presigned URL ("Only one auth mechanism
  allowed"), and honours `Range`.

Native only: the feed is rex's own API, and the Python oracle has none.

    python3 tests/test_sources.py
"""
import base64
import functools
import http.client
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from server_command import server_command
from test_feed import BODY, MOUNT, ROOT, check, results, free_port, item, catalog, serve

FRONT_USER, FRONT_PASSWORD = "front-user", "front-password"


def cdn_handler(seen):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, status, body=b"", extra=()):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Range")))
            if self.headers.get("Authorization"):
                return self.reply(400, b"Only one auth mechanism allowed")
            spec = self.headers.get("Range")
            if spec and spec.startswith("bytes="):
                first, _, last = spec[6:].partition("-")
                first, last = int(first), min(int(last) if last else len(BODY) - 1, len(BODY) - 1)
                return self.reply(206, BODY[first:last + 1],
                                  [("Content-Range", "bytes %d-%d/%d" % (first, last, len(BODY)))])
            self.reply(200, BODY)

        do_HEAD = do_GET
    return Handler


def front_handler(cdn_port, seen):
    expected = "Basic " + base64.b64encode(("%s:%s" % (FRONT_USER, FRONT_PASSWORD)).encode()).decode()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            status = 302 if self.headers.get("Authorization") == expected else 401
            self.send_response(status)
            if status == 302:
                # Another host: `localhost`, where the source's url names 127.0.0.1.
                self.send_header("Location", "http://localhost:%d%s?X-Amz-Signature=x" % (cdn_port, self.path))
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_HEAD = do_GET
    return Handler


def start(work, sources):
    port = free_port()
    json.dump(sources, open(os.path.join(work, "sources.json"), "w"))
    env = dict(os.environ, TMDB_KEY="fake", PLEX_TOKEN="fake", REX_PLEXTV_URL="http://127.0.0.1:9")
    for name in ("REX_STATE", "REX_TOKEN", "ZURG_URL", "REX_SOURCES", "REX_FEED_TOKEN"):
        env.pop(name, None)
    log = open(os.path.join(work, "server.log"), "ab")
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", os.path.join(work, "catalog.json"), "--sources",
                       os.path.join(work, "sources.json"), "--assert-claimed", "--gdm", "off", root=ROOT),
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(160):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=2)
            break
        except Exception:
            time.sleep(0.25)
    return port, process, log


def call(port, path, method="GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method,
                                     headers=dict({"Accept": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            try:
                return response.status, response.read()
            except http.client.IncompleteRead as short:
                return response.status, short.partial
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def push(port, source, token, entry):
    status, raw = call(port, "/rex/feed/%s/items" % source, "POST", {"items": [entry]},
                       {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    status, raw = call(port, "/rex/feed/%s/items" % source, headers={"Authorization": "Bearer " + token})
    key = {row["id"]: row["ratingKey"] for row in json.loads(raw).get("items", [])}.get(entry["id"])
    status, raw = call(port, "/library/metadata/%s" % key)
    row = (json.loads(raw)["MediaContainer"].get("Metadata") or [{}])[0] if status == 200 else {}
    return ((row.get("Media") or [{}])[0].get("Part") or [{}])[0].get("key")


def main():
    work = tempfile.mkdtemp(prefix="rex-sources-")
    disk_root = os.path.join(work, "disk")
    # The size each source pushes is its file's, as zurg reports a release's real size.
    movie, episode = (dict(item(kind, ids=False), size=len(BODY)) for kind in ("movie", "episode"))
    os.makedirs(os.path.join(disk_root, os.path.dirname(movie["path"])))
    open(os.path.join(disk_root, movie["path"]), "wb").write(BODY)
    os.makedirs(os.path.join(disk_root, "movies", "Plain.Release.2021"))
    open(os.path.join(disk_root, "movies", "Plain.Release.2021", "Plain.Release.2021.mkv"), "wb").write(BODY)
    disk_port = serve(functools.partial(http.server.SimpleHTTPRequestHandler, directory=disk_root))
    cdn_seen, front_seen = [], []
    cdn_port = serve(cdn_handler(cdn_seen))
    front_port = serve(front_handler(cdn_port, front_seen))
    catalog(work)
    port, process, log = start(work, {
        "disk": {"url": "http://127.0.0.1:%d" % disk_port, "token": "disk-feed"},
        "front": {"url": "http://127.0.0.1:%d/files" % front_port, "user": FRONT_USER,
                  "password": FRONT_PASSWORD, "token": "front-feed"},
        "plain": {"url": "http://127.0.0.1:%d" % disk_port, "token": "plain-feed"},
    })
    try:
        disk_part = push(port, "disk", "disk-feed", movie)
        front_part = push(port, "front", "front-feed", episode)
        check("each source's push is a row with a part", disk_part and front_part, (disk_part, front_part))

        # -- no prefix to line up: an item lands by its directory -----------------------------
        # zurg's mount_path defaults to empty, and then it pushes no `file`: rex made it
        # `/movies/...`, which the seeded `/zurg/movies` did not hold, so every item was refused.
        plain_movie = {"id": "movies/Plain.Release.2021/Plain.Release.2021.mkv",
                       "path": "movies/Plain.Release.2021/Plain.Release.2021.mkv", "size": len(BODY),
                       "media_info": movie["media_info"]}
        plain_episode = {"id": "shows/Plain.Show.S01.1080p/Plain.Show.S01E01.1080p.mkv",
                         "path": "shows/Plain.Show.S01.1080p/Plain.Show.S01E01.1080p.mkv", "size": len(BODY),
                         "media_info": episode["media_info"]}
        status, raw = call(port, "/rex/feed/plain/items", "POST", {"items": [plain_movie, plain_episode]},
                           {"Authorization": "Bearer plain-feed", "Content-Type": "application/json"})
        body = json.loads(raw)
        check("a source with no mount pushing no file: both items placed, none refused",
              status == 200 and body.get("created") == 2 and not body.get("rejected"), body)
        status, raw = call(port, "/library/sections/1/all")
        titles = [row.get("title") for row in json.loads(raw)["MediaContainer"].get("Metadata") or []]
        check("the movies directory's item is in the movie library", "Plain Release" in titles, titles)

        # -- a server that ignores Range: the client still gets the bytes it asked for ----
        status, raw = call(port, disk_part, headers={"Range": "bytes=100-199"})
        check("a range read from a server that answers 200 is the range's bytes, not the file's first",
              status == 206 and raw == BODY[100:200], (status, raw[:8].hex(), BODY[100:108].hex()))
        status, raw = call(port, disk_part, headers={"Range": "bytes=65000-"})
        check("an open-ended range from it is the file's tail", status == 206 and raw == BODY[65000:],
              (status, len(raw)))
        status, raw = call(port, disk_part)
        check("a whole read from it is the whole file", status == 200 and raw == BODY, (status, len(raw)))

        # -- a redirect to another host: the source's credential stays with the source ----
        status, raw = call(port, front_part, headers={"Range": "bytes=100-199"})
        check("a part the source redirects elsewhere streams from there", status == 206 and raw == BODY[100:200],
              (status, raw[:60]))
        check("the source itself got its credential", front_seen and front_seen[-1][1], front_seen[-1:])
        check("the other host never sees the source's credential", cdn_seen and not any(a for _, a, _ in cdn_seen),
              cdn_seen)
        check("and gets the Range", cdn_seen and cdn_seen[-1][2] == "bytes=100-199", cdn_seen[-1:])
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)
    failed = results.count(False)
    print("\n%d/%d source checks passed" % (len(results) - failed, len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    if os.environ.get("REX_SERVER") == "python":
        print("the feed is rex's own API; the Python oracle has none, so this suite is native only")
        sys.exit(0)
    sys.exit(main())
