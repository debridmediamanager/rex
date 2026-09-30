#!/usr/bin/env python3
"""Library listing parity: the section containers, leaves and rows as `server.py` builds them.

Each check asserts what the Python oracle answers (itself measured against a real PMS)
for one request of the library-listing family -- `/library/sections`, a section's
leaves (`sorts`, `filters`, `folder`, `{tag}/top`, `arts`, `allLeaves`, `albums`,
`moment`, `computePath`, `onDeck`, ...), `/library/all`, `/library/shared/*` and the
double-slash pass-through routes -- found by diffing the native server's answers
against the oracle's over the whole UI suite.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_listings.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)

HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-listings",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome"}
MOVIE_ART, MOVIE_THUMB = "/:/resources/movie-fanart.jpg", "/:/resources/movie.png"
SHOW_ART, SHOW_THUMB = "/:/resources/show-fanart.jpg", "/:/resources/show.png"
LIBRARY_TRIPLE = {"identifier": "com.plexapp.plugins.library",
                  "mediaTagPrefix": "/system/bundle/media/flags/",
                  "mediaTagVersion": 1655122614}

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


def request(port, method, path, headers=None, body=None, keep_alive=False):
    """(status, {lower header: value}, parsed JSON or raw bytes)."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = dict(HEADERS)
    sent.update(headers or {})
    if not keep_alive:
        sent["Connection"] = "close"
    try:
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        raw = response.read()
        head = {name.lower(): value for name, value in response.getheaders()}
    finally:
        connection.close()
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw
    return response.status, head, parsed


def mc(port, path, method="GET"):
    status, _, body = request(port, method, path)
    return (body or {}).get("MediaContainer", {}) if status == 200 and isinstance(body, dict) \
        else {"_status": status}


def has(container, **expected):
    wrong = {k: (container.get(k, "<absent>"), v) for k, v in expected.items()
             if container.get(k, "<absent>") != v}
    return not wrong, wrong


def lacks(container, *names):
    present = [name for name in names if name in container]
    return not present, present


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
    print("PASS %d/%d listing parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


def run(port):
    # -- containers --------------------------------------------------------------
    sections = mc(port, "/library/sections")
    ok, bad = lacks(sections, "identifier", "mediaTagPrefix", "mediaTagVersion")
    check("/library/sections carries title1 and allowSync only, no library triple",
          ok and sections.get("allowSync") is False and sections.get("title1") == "Plex Library",
          bad or sections)
    ok, bad = lacks(mc(port, "/library/sections/all"), "identifier", "mediaTagPrefix")
    check("/library/sections/all is the same bare container", ok, bad)

    root = mc(port, "/library")
    ok, bad = has(root, size=3, allowSync=False, art="/:/resources/library-art.png", content="",
                  title1="Plex Library", title2="", **LIBRARY_TRIPLE)
    check("/library carries the library-art secondary pair and the library triple", ok, bad)
    for path in ("/library//", "/library/sections/1//"):
        doubled = mc(port, path)
        parent = mc(port, path[:-2])
        check("%s answers its parent's own container" % path,
              doubled == parent and doubled.get("size"), (doubled, parent))

    for leaf in ("sorts", "filters"):
        for section, art, thumb, title in (("1", MOVIE_ART, MOVIE_THUMB, "Debrid Movies"),
                                           ("2", SHOW_ART, SHOW_THUMB, "Debrid TV")):
            found = mc(port, "/library/sections/%s/%s" % (section, leaf))
            expected = dict(allowSync=False, art=art, thumb=thumb, content="secondary",
                            title1=title, viewGroup="secondary", viewMode=65592,
                            **LIBRARY_TRIPLE)
            if section == "2":
                expected["nocache"] = True
            ok, bad = has(found, **expected)
            check("/library/sections/%s/%s is the secondary container" % (section, leaf), ok, bad)

    folder = mc(port, "/library/sections/1/folder")
    ok, bad = has(folder, allowSync=True, art=MOVIE_ART, thumb=MOVIE_THUMB, content="secondary",
                  title1="Debrid Movies", title2="By Folder", viewGroup="secondary",
                  viewMode=65592, librarySectionID=1, **LIBRARY_TRIPLE)
    check("/folder is the secondary container titled By Folder", ok, bad)

    arts = mc(port, "/library/sections/1/arts")
    ok, bad = has(arts, allowSync=False, art=MOVIE_ART, thumb=MOVIE_THUMB,
                  title1="Debrid Movies", viewGroup="movie", viewMode=65592, **LIBRARY_TRIPLE)
    check("/arts is the library's container", ok and arts.get("size") == 6, bad or arts)
    show_arts = mc(port, "/library/sections/2/arts")
    ok, bad = has(show_arts, viewGroup="episode", nocache=True, art=SHOW_ART)
    check("a show library's /arts says viewGroup episode and nocache", ok, bad)

    for leaf in ("albums", "moment", "allLeaves"):
        found = mc(port, "/library/sections/1/%s" % leaf)
        ok, bad = has(found, art=MOVIE_ART, thumb=MOVIE_THUMB, viewMode=65592, **LIBRARY_TRIPLE)
        check("/%s carries the resource art and thumb" % leaf, ok, bad)
    path = mc(port, "/library/sections/1/computePath?startID=1007&endID=1007")
    ok, bad = has(path, size=0, art=MOVIE_ART, thumb=MOVIE_THUMB, title1="Debrid Movies")
    ok2, bad2 = lacks(path, "viewMode", "allowSync", "nocache")
    check("/computePath carries art and thumb and neither viewMode nor allowSync",
          ok and ok2, (bad, bad2))

    for section, view_group, art in (("1", "movie", MOVIE_ART), ("2", "episode", SHOW_ART)):
        deck = mc(port, "/library/sections/%s/onDeck" % section)
        expected = dict(art=art, librarySectionID=int(section), title2="On Deck",
                        viewGroup=view_group, viewMode=65592, allowSync=True, **LIBRARY_TRIPLE)
        if section == "2":
            expected.update(mixedParents=True, nocache=True)
        ok, bad = has(deck, **expected)
        check("/library/sections/%s/onDeck is the listing container" % section, ok, bad)

    # -- the tag tables ------------------------------------------------------------
    genre = mc(port, "/library/sections/1/genre/top")
    rows = genre.get("Directory") or []
    ok, bad = has(genre, art=MOVIE_ART, thumb=MOVIE_THUMB, **LIBRARY_TRIPLE)
    check("/genre/top is the library container", ok, bad)
    check("/genre/top rows keep the tag's integer id and carry no title",
          rows and all(isinstance(r.get("id"), int) and "title" not in r for r in rows),
          rows[:2])
    check("/genre/top rows are ascending by id",
          [r.get("id") for r in rows] == sorted(r.get("id") for r in rows), rows)
    label = mc(port, "/library/sections/1/label/top")
    ok, bad = has(label, size=0, **LIBRARY_TRIPLE)
    check("an untyped /top (label) is an empty library container", ok, bad)
    for name, status in (("decade", 200), ("network", 200), ("unwatched", 200),
                         ("audioLanguage", 200), ("platform", 404), ("zzz", 404),
                         ("thumb", 404)):
        got = request(port, "GET", "/library/sections/1/%s/top" % name)[0]
        check("/%s/top answers %d" % (name, status), got == status, got)
    resolution = mc(port, "/library/sections/1/resolution/top")
    check("/resolution/top is the resolution bucket, the whole library",
          len(resolution.get("Metadata") or []) == 6, resolution.get("size"))

    # -- empty write answers ---------------------------------------------------------
    for method, path in (("PUT", "/library/sections/1/analyze"),
                         ("PUT", "/library/sections/1/emptyTrash"),
                         ("DELETE", "/library/sections/1/indexes")):
        status, head, _ = request(port, method, path)
        check("%s %s answers an empty text/html 200" % (method, path),
              status == 200 and head.get("content-type") == "text/html"
              and head.get("content-length") == "0", (status, head))

    # -- an unrecognised method honours the request's connection state -------------
    for method in ("PATCH", "FROB"):
        status, head, _ = request(port, method, "/library/sections")
        check("%s with Connection: close answers 500 and closes" % method,
              status == 500 and head.get("connection") == "close"
              and "keep-alive" not in head, (status, head))


if __name__ == "__main__":
    sys.exit(main())
