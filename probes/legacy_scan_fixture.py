#!/usr/bin/env python3
"""Record the rows the old WebDAV scanner made, for the feed's migration test.

The feed adopts a row the scanner made for the same file (`docs/feed-api.md`,
"Migration"). That test has to start from what the scanner really wrote, not from a
row shaped by hand, so this drives the LAST scanning build (acaa3cc, before the feed)
against a stub zurg serving zurg's two real entries (`tests/fixtures/zurg/entries.json`)
and a third release zurg had neither identified nor probed, scrobbles one row, and dumps
the state database's rows to `tests/fixtures/feed/legacy_scan_rows.json`.

TMDB and Plex answer from the same on-disk caches `rust/src/runtime/scan.rs`'s unit test
writes (real ids); a lookup they miss goes out with a fake key and is refused, so the
unidentified release stays unmatched.

    python3 probes/legacy_scan_fixture.py --bin <acaa3cc build>/rex --root <acaa3cc tree>
"""
import argparse
import base64
import http.server
import json
import os
import socket
import socketserver
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRIES = json.load(open(os.path.join(HERE, "tests", "fixtures", "zurg", "entries.json")))["entries"]
NEW = "Qzxv.Wmplk.2024.1080p.WEB-DL"
CACHE = json.load(open(os.path.join(HERE, "tests", "fixtures", "feed", "provider_cache.json")))
TMDB_CACHE, PLEX_CACHE = CACHE["tmdb"], CACHE["plex"]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def zurg_stub(files):
    """PROPFIND listings in zurg's shape and `/api/library/entry`, no auth."""
    def listing(folder):
        folder = folder.rstrip("/") + "/"
        children = {}
        for path, entry in files.items():
            if path.startswith(folder):
                rest = path[len(folder):].split("/")
                children[folder + rest[0] + ("/" if len(rest) > 1 else "")] = entry.get("size") or 1
        quote = lambda p: "/".join(urllib.parse.quote(s, safe="") for s in p.split("/"))
        row = ('<d:response><d:href>%s</d:href><d:propstat><d:prop>%s</d:prop>'
               '<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')
        rows = [row % (quote(folder), "<d:resourcetype><d:collection/></d:resourcetype>")]
        for href, size in sorted(children.items()):
            rows.append(row % (quote(href), "<d:resourcetype><d:collection/></d:resourcetype>"
                               if href.endswith("/") else
                               "<d:getcontentlength>%d</d:getcontentlength><d:resourcetype></d:resourcetype>" % size))
        return ('<?xml version="1.0" encoding="utf-8"?>\n<d:multistatus xmlns:d="DAV:">'
                + "".join(rows) + "</d:multistatus>").encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, status, body, kind):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PROPFIND(self):
            self.reply(207, listing(urllib.parse.unquote(self.path)), "application/xml")

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(urllib.parse.parse_qs(parsed.query).get("path", [""])[0])
            entry = files.get(path)
            if parsed.path != "/api/library/entry" or entry is None:
                self.reply(404, b"404 page not found\n", "text/plain")
            else:
                self.reply(200, json.dumps(entry).encode(), "application/json")

    port = free_port()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", required=True, help="the acaa3cc build of rust/target/release/rex")
    parser.add_argument("--root", required=True, help="the acaa3cc tree (its sidecars)")
    parser.add_argument("--out", default=os.path.join(HERE, "tests", "fixtures", "feed", "legacy_scan_rows.json"))
    args = parser.parse_args()
    work = tempfile.mkdtemp(prefix="rex-legacy-")
    movie, episode = ENTRIES["movie"], ENTRIES["episode"]
    files = {
        "/dav/movies/%s/%s" % (movie["release"], movie["file"]): movie["entry"],
        "/dav/shows/%s/%s" % (episode["release"], episode["file"]): episode["entry"],
        "/dav/movies/%s/%s.mkv" % (NEW, NEW): {"hash": "a" * 40, "name": NEW, "size": 12345,
                                               "signed_path": "/strm/e/x", "media_info": None},
    }
    zurg_port = zurg_stub(files)
    sections = [{"key": "1", "type": "movie", "title": "Movies",
                 "uuid": "0000000000000000000000000000000000000001", "Location": [{"id": 1, "path": "/zurg/movies"}]},
                {"key": "2", "type": "show", "title": "TV Shows",
                 "uuid": "0000000000000000000000000000000000000002", "Location": [{"id": 2, "path": "/zurg/shows"}]}]
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000ff", "friendlyName": "legacy",
               "sections": sections, "items": []}, open(os.path.join(work, "catalog.json"), "w"))
    json.dump(TMDB_CACHE, open(os.path.join(work, "tmdb_cache.json"), "w"))
    json.dump(PLEX_CACHE, open(os.path.join(work, "plex_cache.json"), "w"))
    port = free_port()
    env = dict(os.environ, ZURG_URL="http://127.0.0.1:%d" % zurg_port, TMDB_KEY="fake", PLEX_TOKEN="fake",
               REX_PLEXTV_URL="http://127.0.0.1:9")
    for name in ("REX_STATE", "REX_TOKEN", "ZURG_USER", "ZURG_PASSWORD"):
        env.pop(name, None)
    log = open(os.path.join(work, "server.log"), "wb")
    process = subprocess.Popen([args.bin, "--port", str(port), "--catalog", os.path.join(work, "catalog.json"),
                                "--state", os.path.join(work, "rex.db"), "--zurg-mount", "/zurg",
                                "--assert-claimed", "--gdm", "off"], cwd=args.root, env=env, stdout=log,
                               stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % port
    get = lambda path, method="GET": urllib.request.urlopen(urllib.request.Request(
        base + path, method=method, headers={"Accept": "application/json"}), timeout=30).read()
    try:
        deadline = time.time() + 120
        rows = []
        while time.time() < deadline and len(rows) < 2:
            time.sleep(1)
            try:
                rows = json.loads(get("/library/sections/1/all"))["MediaContainer"].get("Metadata") or []
            except Exception:
                rows = []
        time.sleep(3)  # the show library's scan follows the movie library's
        bellboy = [row for row in rows if row.get("title") == "Blame It on the Bellboy"]
        get("/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % bellboy[0]["ratingKey"])
        time.sleep(1)
    finally:
        process.terminate()
        process.wait(timeout=30)
        log.close()
    db = sqlite3.connect(os.path.join(work, "rex.db"))
    items = [json.loads(body) for (body,) in db.execute(
        "SELECT body FROM row WHERE coll = 'state:items' ORDER BY pos")]
    (next_key,) = db.execute("SELECT body FROM doc WHERE name = 'state:nextScanKey'").fetchone()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump({"_about": "Rows the last WebDAV-scanning build (acaa3cc) made from zurg's two real entries "
                         "(tests/fixtures/zurg/entries.json) and an unidentified, unprobed release, with the "
                         "movie scrobbled once; dumped from its state database by probes/legacy_scan_fixture.py. "
                         "The feed's migration test adopts them.",
               "sections": sections, "nextScanKey": json.loads(next_key), "items": items},
              open(args.out, "w"), indent=1, ensure_ascii=False)
    print("%d rows -> %s" % (len(items), args.out))


if __name__ == "__main__":
    main()
