#!/usr/bin/env python3
"""A pack of several seasons whose files are numbered only within their season folder, which
zurg's mount flattens: Saturday Night Live Seasons 1-15 has an `01 ` file in each of fifteen
`Season N` folders, Smallville's Italian BD remux in each of ten `Stagione N`. zurg now pushes
the folder (`folder`), and each file is its own season's episode; pushed without it, the file
is left unplaced rather than folded onto another season's. A pack listed at its top level
(Digimon Tamers, `folder: ""`) was never flattened, so its leading numbers read too. zurg's real
entries are `fixtures/feed/pack_folders.json`.

Record the answers again: `REX_RECORD_ANSWERS=1 TMDB_KEY=... python3 tests/test_feed_pack_folders.py`.

    python3 tests/test_feed_pack_folders.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

from test_feed import ENTRIES, LEGACY, MOUNT, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "pack_folders.json")
FIXTURE = json.load(open(FIXTURE_PATH))
RECORD = os.environ.get("REX_RECORD_ANSWERS") == "1"
PROBE = ENTRIES["episode"]["entry"]["media_info"]


def item(name):
    pushed = FIXTURE["pushes"][name]
    path = pushed["path"]
    out = {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": pushed["name"],
           "release": "/".join(path.split("/")[:2]), "size": pushed["size"], "hash": pushed["hash"],
           "ids": pushed["ids"], "media_info": PROBE}
    if "folder" in pushed:
        out["folder"] = pushed["folder"]
    return out


def main():
    source = Bytes()
    for name in FIXTURE["pushes"]:
        source.files.add("/dav/" + FIXTURE["pushes"][name]["path"])
    bytes_port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-packs-")
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000f8", "friendlyName": "rex-packs",
               "sections": LEGACY["sections"], "items": []}, open(os.path.join(work, "catalog.json"), "w"))
    for name in ("tmdb", "plex"):
        json.dump({} if RECORD else FIXTURE[name], open(os.path.join(work, name + "_cache.json"), "w"))
    keys = {"TMDB_KEY": os.environ["TMDB_KEY"], "PLEX_TOKEN": os.environ.get("PLEX_TOKEN", "fake")} if RECORD else None
    server = Server(work, bytes_port, keys=keys)
    try:
        server.feed("POST", "items", {"items": [item(name) for name in sorted(FIXTURE["pushes"])]})
        placed = server.keys()
        for name in sorted(FIXTURE["pushes"]):
            pushed = FIXTURE["pushes"][name]
            key = placed.get(pushed["path"])
            if pushed["want"] is None:
                check("%s is not placed" % name, key is None, key and server.row(key).get("title"))
                continue
            row = server.row(key) if key else {}
            got = [row.get("parentIndex"), row.get("index")]
            check("%s is S%02dE%02d" % (name, *pushed["want"]), got == pushed["want"], (got, row.get("grandparentTitle")))
        snl = {server.row(placed[FIXTURE["pushes"][name]["path"]]).get("grandparentRatingKey")
               for name in ("snl-s13-01", "snl-s14-01") if FIXTURE["pushes"][name]["path"] in placed}
        check("both SNL episodes are under one show", len(snl) == 1, snl)
    finally:
        server.stop()
        if RECORD:
            for name in ("tmdb", "plex"):
                path = os.path.join(work, name + "_cache.db")
                if os.path.exists(path):
                    with sqlite3.connect(path) as db:
                        FIXTURE[name].update({key: json.loads(value) for key, value in db.execute("SELECT key, value FROM cache")})
            for key, value in list(FIXTURE["tmdb"].items()):
                if "/season/" in key and "/episode/" not in key and isinstance(value, dict):
                    FIXTURE["tmdb"][key] = {field: v for field, v in value.items() if field != "episodes"}
            json.dump(FIXTURE, open(FIXTURE_PATH, "w"), indent=1, sort_keys=True, ensure_ascii=False)
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
