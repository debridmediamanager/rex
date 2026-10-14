#!/usr/bin/env python3
"""Releases named in a way identification read wrong, each a real one the production rex left on
`local://` (2026-10-13): a film named twice with a dotted `a.K.A.`, a `THEATRiCAL.CUT` whose
`CUT` stayed in the title, and a show whose release is named in Russian while its files say
`Lovely.Runner.2024.S01E01` -- named by its files, it joins the show of that name instead of
standing beside it. zurg's real entries are `fixtures/feed/names.json`.

Record the answers again: `REX_RECORD_ANSWERS=1 TMDB_KEY=... python3 tests/test_feed_names.py`.

    python3 tests/test_feed_names.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile

from test_feed import ENTRIES, LEGACY, MOUNT, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "names.json")
FIXTURE = json.load(open(FIXTURE_PATH))
RECORD = os.environ.get("REX_RECORD_ANSWERS") == "1"
PROBE = ENTRIES["episode"]["entry"]["media_info"]


def item(name):
    pushed = FIXTURE["pushes"][name]
    path = pushed["path"]
    return {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": pushed["name"],
            "release": "/".join(path.split("/")[:2]), "size": pushed["size"], "hash": pushed["hash"],
            "ids": pushed["ids"], "media_info": PROBE}


def main():
    source = Bytes()
    for name in FIXTURE["pushes"]:
        source.files.add("/dav/" + FIXTURE["pushes"][name]["path"])
    bytes_port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-names-")
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000f9", "friendlyName": "rex-names",
               "sections": LEGACY["sections"], "items": []}, open(os.path.join(work, "catalog.json"), "w"))
    for name in ("tmdb", "plex"):
        json.dump({} if RECORD else FIXTURE[name], open(os.path.join(work, name + "_cache.json"), "w"))
    keys = {"TMDB_KEY": os.environ["TMDB_KEY"], "PLEX_TOKEN": os.environ.get("PLEX_TOKEN", "fake")} if RECORD else None
    server = Server(work, bytes_port, keys=keys)
    try:
        server.feed("POST", "items", {"items": [item(name) for name in sorted(FIXTURE["pushes"])]})
        placed = server.keys()
        for name in sorted(FIXTURE["pushes"]):
            row = server.row(placed[FIXTURE["pushes"][name]["path"]])
            named = server.row(row["grandparentRatingKey"]) if row.get("grandparentRatingKey") else row
            ids = [guid.get("id") for guid in named.get("Guid") or []]
            want = "tmdb://%d" % FIXTURE["pushes"][name]["want"]
            check("%s is TMDB's %s (%s)" % (name, want, named.get("title")), want in ids, (named.get("title"), ids))
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
