#!/usr/bin/env python3
"""A disc's extras are never episodes: not when they are pushed, and not as rows a start finds.

`fixtures/feed/extras_1017.json` is what the production rex held on 2026-10-17 and zurg's pushes
of the same release. `[KH] Motto To Love-Ru (BD …)` lists its menus and creditless openings and
endings under `Extras/Menu` and `Extras/NC`; `Menu - 02` … `Menu - 06` and `NCED - 01` were read as
episodes 1-6 of a show of their own, with the blank tile. To Love Ru's first three episodes held
the [KH] Darkness discs' `NCED - 01`, commercials and OVAs as versions beside each episode's own
file, which a client could play for the episode.

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

from test_feed import ENTRIES, LEGACY, MOUNT, Bytes, Server, check, results, serve

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
    gone = {key: server.row(key).get("title") for key in MOTTO if server.row(key)}
    check("%s: Motto To Love-Ru's menus and NCED are no show, no season and no episodes" % label, not gone, gone)
    for key, own in sorted(TO_LOVE_RU.items()):
        held = files(server.row(key))
        check("%s: To Love Ru %s keeps its own release and nothing else" % (label, key),
              len(held) == 1 and held[0].startswith(own), held)


def main():
    source = Bytes()
    for pushed in FIXTURE["pushes"].values():
        source.files.add("/dav/" + pushed["path"])
    bytes_port = serve(source.handler())

    # -- rows held so: a start drops what is no episode ----------------------------------------
    work = tempfile.mkdtemp(prefix="rex-extras-")
    catalog(work, FIXTURE["rows"])
    server = start(work, bytes_port)
    try:
        check_rows("after a start", server)
    finally:
        stop(server, work)
    log = os.path.join(work, "server.log")
    dropped = open(log, errors="replace").read().count("FEED dropped")
    server = start(work, bytes_port)
    try:
        time.sleep(10)
        check_rows("after the next start", server)
        check("the next start drops nothing more", open(log, errors="replace").read().count("FEED dropped") == dropped == 1,
              (dropped, open(log, errors="replace").read().count("FEED dropped")))
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
                check("%s is no episode" % name, key is None, (key, server.row(key).get("index") if key else None))
                continue
            row = server.row(key) if key else {}
            check("%s is episode %s of season %s" % (name, want[1], want[0]),
                  [row.get("parentIndex"), row.get("index")] == want, (key, row.get("parentIndex"), row.get("index")))
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
