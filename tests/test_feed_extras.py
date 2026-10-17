#!/usr/bin/env python3
"""A disc's extras are never episodes when they are pushed, and the rows a start finds stay as held.

`fixtures/feed/extras_1017.json` is what the production rex held on 2026-10-17 and zurg's pushes
of the same release. `[KH] Motto To Love-Ru (BD …)` lists its menus and creditless openings and
endings under `Extras/Menu` and `Extras/NC`; `Menu - 02` … `Menu - 06` and `NCED - 01` were read as
episodes 1-6 of a show of their own, with the blank tile. To Love Ru's first three episodes held
the [KH] Darkness discs' `NCED - 01`, commercials and OVAs as versions beside each episode's own
file, which a client could play for the episode.

An extra of a type Plex serves -- the creditless opening, a film's `Featurettes/` or `-trailer`
file -- is a clip of its release's title instead (`extraType`, the shape cycle 29 measured on the
1.42.2 reference); a disc's menus are no extra Plex has a type for and stay unplaced. Rows already
held are a person's to delete: no pass at start reads a row's name (2026-10-17), so a start keeps
them as they are.

Record the answers again: `REX_RECORD_ANSWERS=1 TMDB_KEY=... PLEX_TOKEN=... python3 tests/test_feed_extras.py`.

    python3 tests/test_feed_extras.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

import urllib.request

from test_feed import BODY, ENTRIES, LEGACY, MOUNT, Bytes, Server, check, item as feed_item, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "extras_1017.json")
FIXTURE = json.load(open(FIXTURE_PATH))
RECORD = os.environ.get("REX_RECORD_ANSWERS") == "1"
PROBE = ENTRIES["episode"]["entry"]["media_info"]
MOTTO = ["169239", "169246", "169187", "169188", "169189", "169190", "169191"]
TO_LOVE_RU = {"159014": "[Holomux] To LOVE-Ru 01 ", "159015": "[Holomux] To LOVE-Ru 02 ", "159016": "[Holomux] To LOVE-Ru 03 "}


def catalog(work, rows=()):
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000f7", "friendlyName": "rex-extras",
               "sections": LEGACY["sections"], "items": list(rows)}, open(os.path.join(work, "catalog.json"), "w"))
    for name in ("tmdb", "plex"):
        json.dump({} if RECORD else FIXTURE[name], open(os.path.join(work, name + "_cache.json"), "w"))


def start(work, bytes_port):
    keys = {"TMDB_KEY": os.environ["TMDB_KEY"], "PLEX_TOKEN": os.environ["PLEX_TOKEN"]} if RECORD else None
    return Server(work, bytes_port, keys=keys)


def stop(server, work):
    server.stop()
    if RECORD:
        for name in ("tmdb", "plex"):
            path = os.path.join(work, name + "_cache.db")
            if os.path.exists(path):
                with sqlite3.connect(path) as db:
                    FIXTURE[name].update({key: json.loads(value) for key, value in db.execute("SELECT key, value FROM cache")})


def item(name):
    pushed = FIXTURE["pushes"][name]
    out = {field: value for field, value in pushed.items() if field != "want"}
    out.update(file="%s/%s" % (MOUNT, pushed["path"]), media_info=PROBE)
    return out


def files(row):
    return [part.get("file", "").rsplit("/", 1)[-1] for media in row.get("Media") or [] for part in media.get("Part") or []]


def check_rows(label, server):
    kept = [key for key in MOTTO if server.row(key)]
    check("%s: Motto To Love-Ru's rows are as they were held" % label, kept == MOTTO, kept)
    for key in sorted(TO_LOVE_RU):
        held = next(row for row in FIXTURE["rows"] if row["ratingKey"] == key)
        check("%s: To Love Ru %s keeps every release it held" % (label, key),
              sorted(files(server.row(key))) == sorted(files(held)), files(server.row(key)))


def check_clip(server, parent, key, extra_type, subtype, title):
    clip = server.row(key) if key else {}
    check("%s is a clip of %s, extraType %d" % (title, parent, extra_type),
          [clip.get("type"), clip.get("extraType"), clip.get("subtype"), clip.get("title")] == ["clip", extra_type, subtype, title]
          and "librarySectionID" not in clip, (key, {k: clip.get(k) for k in ("type", "extraType", "subtype", "title", "librarySectionID")}))
    status, body = server.request("/library/metadata/%s?includeExtras=1" % parent)
    inline = ((((body or {}).get("MediaContainer") or {}).get("Metadata") or [{}])[0].get("Extras") or {}).get("Metadata") or []
    check("%s is in its parent's Extras" % title, key and key in [row.get("ratingKey") for row in inline], inline)
    check("%s is in its parent's extras leaf" % title,
          key and key in [row.get("ratingKey") for row in server.rows("/library/metadata/%s/extras" % parent)])
    part = ((clip.get("Media") or [{}])[0].get("Part") or [{}])[0].get("key")
    read = urllib.request.Request("http://127.0.0.1:%d%s" % (server.port, part), headers={"Range": "bytes=0-15"})
    try:
        with urllib.request.urlopen(read, timeout=30) as response:
            got = response.status, response.read()
    except Exception as error:
        got = getattr(error, "code", None), b""
    check("%s plays: its part reads its file's bytes" % title, got == (206, BODY[:16]), (part, got[0]))


def main():
    source = Bytes()
    for pushed in FIXTURE["pushes"].values():
        source.files.add("/dav/" + pushed["path"])
    bytes_port = serve(source.handler())

    # -- rows held so: a start leaves them as they are -----------------------------------------
    work = tempfile.mkdtemp(prefix="rex-extras-")
    catalog(work, FIXTURE["rows"])
    server = start(work, bytes_port)
    try:
        time.sleep(10)
        check_rows("after a start", server)
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    # -- pushed: the episode is placed, the menu and the creditless opening are not -----------
    work = tempfile.mkdtemp(prefix="rex-extras-")
    catalog(work)
    server = start(work, bytes_port)
    try:
        names = sorted(FIXTURE["pushes"])
        status, body = server.feed("POST", "items", {"items": [item(name) for name in names]})
        check("every file is taken", status == 200 and not (body or {}).get("rejected"), (status, body))
        keys = server.keys()
        for name in names:
            want, key = FIXTURE["pushes"][name]["want"], keys.get(FIXTURE["pushes"][name]["id"])
            if want is None:
                row = server.row(key) if key else {}
                check("%s is no episode" % name, row.get("type") != "episode", (key, row.get("type"), row.get("index")))
                continue
            row = server.row(key) if key else {}
            check("%s is episode %s of season %s" % (name, want[1], want[0]),
                  [row.get("parentIndex"), row.get("index")] == want, (key, row.get("parentIndex"), row.get("index")))
        show = server.row(keys[FIXTURE["pushes"]["motto_01"]["id"]]).get("grandparentRatingKey")
        check("the menu is no clip either: Plex has no type for a disc's menus",
              keys.get(FIXTURE["pushes"]["motto_menu_01"]["id"]) is None, keys.get(FIXTURE["pushes"]["motto_menu_01"]["id"]))
        check_clip(server, show, keys.get(FIXTURE["pushes"]["motto_ncop"]["id"]), 13, "opening", "NCOP")
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    # -- a film's extras: Plex's folder and suffix names, each a clip of the film ---------------
    work = tempfile.mkdtemp(prefix="rex-extras-")
    catalog(work)
    server = start(work, bytes_port)
    try:
        film = feed_item("movie")
        directory = film["path"].rsplit("/", 1)[0]
        extras = []
        for folder, name in (("Featurettes", "Making Of.mkv"), ("", "Feature-trailer.mkv")):
            extra = dict(film, id="%s/%s" % (directory, name), path="%s/%s" % (directory, name),
                         file="%s/%s/%s" % (MOUNT, directory, name), folder=folder, ids={})
            source.files.add("/dav/" + extra["path"])
            extras.append(extra)
        status, body = server.feed("POST", "items", {"items": extras + [film]})
        check("the film and its extras are taken in one push", status == 200 and not (body or {}).get("rejected"), (status, body))
        keys = server.keys()
        parent = keys.get(film["id"])
        check("the film is a movie", server.row(parent).get("type") == "movie", server.row(parent).get("type"))
        featurette, trailer = (keys.get(extra["id"]) for extra in extras)
        check_clip(server, parent, featurette, 10, "featurette", "Making Of")
        check_clip(server, parent, trailer, 1, "trailer", "Feature-trailer")
        check("the film's primaryExtraKey is its trailer", server.row(parent).get("primaryExtraKey") == "/library/metadata/%s" % trailer,
              server.row(parent).get("primaryExtraKey"))
        listed = [row.get("ratingKey") for row in server.rows("/library/sections/%s/all" % server.row(parent).get("librarySectionID"))]
        check("no clip is in the library's listing", parent in listed and featurette not in listed and trailer not in listed, listed)
        status, again = server.feed("POST", "items", {"items": extras + [film]})
        check("a second push of the same files changes nothing", status == 200 and server.keys() == keys
              and len(server.rows("/library/metadata/%s/extras" % parent)) == 2, (status, again))
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    if RECORD:
        json.dump(FIXTURE, open(FIXTURE_PATH, "w"), indent=1, sort_keys=True, ensure_ascii=False)
        open(FIXTURE_PATH, "a").write("\n")
        print("recorded %d TMDB and %d Plex answers" % (len(FIXTURE["tmdb"]), len(FIXTURE["plex"])))
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
