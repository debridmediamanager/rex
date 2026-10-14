#!/usr/bin/env python3
"""An anime cour is placed on TMDB's show, season and episode, and rows placed before move there.

Named by title, the production rex left 12 anime shows unidentified and about 50 seasons on `local://`
(2026-10-13): fansubs name a cour as AniDB does (`Hibike Euphonium 3 - 01`, `Uma Musume -
Cinderella Gray Part 2 - 05`, `Bleach - Sennen Kessen Hen - Kashin Tan - 01`) and TMDB numbers it
as a season, or as episodes further on in one. `REX_ANIME_LISTS=on` reads anime-lists and the
anime-offline-database (`fixtures/anime/`, real entries cut to these titles, kept beside the
catalog so nothing is fetched). This pushes zurg's real entries for those files
(`fixtures/feed/anime.json`) and a scene release of the Korean `Kingdom`, which must keep its
own numbers; then starts with the mapping on over the rows the same files made without it.

Record the answers again: `REX_RECORD_ANSWERS=1 TMDB_KEY=... python3 tests/test_feed_anime.py`.

    python3 tests/test_feed_anime.py
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
FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "anime.json")
FIXTURE = json.load(open(FIXTURE_PATH))
LISTS = os.path.join(HERE, "fixtures", "anime")
RECORD = os.environ.get("REX_RECORD_ANSWERS") == "1"
PROBE = ENTRIES["episode"]["entry"]["media_info"]


def item(name):
    pushed = FIXTURE["pushes"][name]
    path = pushed["path"]
    return {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": pushed["name"],
            "release": "/".join(path.split("/")[:2]), "size": pushed["size"], "hash": pushed["hash"],
            "ids": pushed["ids"], "media_info": PROBE}


def catalog(work):
    sections = json.loads(json.dumps(LEGACY["sections"]))
    next(section for section in sections if section["type"] == "show")["Location"].append({"id": 3, "path": "/zurg/anime"})
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fa", "friendlyName": "rex-anime",
               "sections": sections, "items": []}, open(os.path.join(work, "catalog.json"), "w"))
    for name in ("tmdb", "plex"):
        json.dump({} if RECORD else FIXTURE[name], open(os.path.join(work, name + "_cache.json"), "w"))
    for name in ("anime-list-master.xml", "anime-offline-database.jsonl"):
        shutil.copy(os.path.join(LISTS, name), os.path.join(work, name))
    # IMDb's rows for these shows, kept where rex keeps the dataset, so nothing is fetched.
    shutil.copy(os.path.join(HERE, "fixtures", "imdb", "title.episode.tsv.gz"), os.path.join(work, "title.episode.tsv.gz"))


def start(work, bytes_port, anime):
    keys = {"REX_ANIME_LISTS": "on" if anime else "off", "REX_IMDB_EPISODES": "on" if anime else "off"}
    if RECORD:
        keys.update(TMDB_KEY=os.environ["TMDB_KEY"], PLEX_TOKEN=os.environ.get("PLEX_TOKEN", "fake"))
    return Server(work, bytes_port, keys=keys)


def stop(server, work):
    server.stop()
    if RECORD:
        for name in ("tmdb", "plex"):
            path = os.path.join(work, name + "_cache.db")
            if os.path.exists(path):
                with sqlite3.connect(path) as db:
                    FIXTURE[name].update({key: json.loads(value) for key, value in db.execute("SELECT key, value FROM cache")})


def placed(server, key):
    """(the show's TMDB id, season, episode) an episode row sits on."""
    row = server.row(key)
    show = server.row(row.get("grandparentRatingKey")) if row.get("grandparentRatingKey") else {}
    tmdb = next((int(guid["id"][7:]) for guid in show.get("Guid") or [] if guid.get("id", "").startswith("tmdb://")), None)
    return [tmdb, row.get("parentIndex"), row.get("index")]


def wait_for(server, key, want, seconds=60):
    deadline = time.time() + seconds
    got = placed(server, key)
    while got[1:] != want[1:] and time.time() < deadline:
        time.sleep(1)
        got = placed(server, key)
    return got


def main():
    source = Bytes()
    for name in FIXTURE["pushes"]:
        source.files.add("/dav/" + FIXTURE["pushes"][name]["path"])
    bytes_port = serve(source.handler())
    names = sorted(FIXTURE["pushes"])

    # -- live: each cour lands on TMDB's numbers ------------------------------------------
    work = tempfile.mkdtemp(prefix="rex-anime-")
    catalog(work)
    server = start(work, bytes_port, True)
    try:
        status, body = server.feed("POST", "items", {"items": [item(name) for name in names]})
        keys = server.keys()
        check("every file is placed", status == 200 and len(keys) == len(names), (status, body))
        for name in names:
            want = FIXTURE["pushes"][name]["want"]
            got = placed(server, keys[FIXTURE["pushes"][name]["path"]])
            if want[0] is None:
                check("the scene release of the Korean Kingdom keeps its own numbers", got[1:] == want[1:], got)
            else:
                check("%s is TMDB's %s season %s episode %s" % (name, *want), got == want, got)
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    # -- placed before: a start moves each onto TMDB's numbers, on its own key -------------
    work = tempfile.mkdtemp(prefix="rex-anime-")
    catalog(work)
    server = start(work, bytes_port, False)
    try:
        server.feed("POST", "items", {"items": [item(name) for name in names]})
        keys = server.keys()
        hibike = keys[FIXTURE["pushes"]["hibike"]["path"]]
        server.request("/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % hibike)
        before = {name: placed(server, keys[FIXTURE["pushes"][name]["path"]]) for name in names}
        check("without the lists, Hibike Euphonium 3's first episode sits on season 1",
              before["hibike"][1:] == [1, 1], before["hibike"])
    finally:
        stop(server, work)
    server = start(work, bytes_port, True)
    try:
        for name in names:
            want = FIXTURE["pushes"][name]["want"]
            got = wait_for(server, keys[FIXTURE["pushes"][name]["path"]], want)
            if want[0] is None:
                check("a start leaves the Korean Kingdom where it was", got[1:] == before[name][1:], (before[name], got))
            else:
                check("a start places %s on TMDB's season %s episode %s, on its key" % (name, want[1], want[2]),
                      got == want, (before[name], got))
        check("and what was watched stays watched", server.row(hibike).get("viewCount") == 1, server.row(hibike).get("viewCount"))
        check("no file is left on a row of its own", server.keys() == keys, server.keys())
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    if RECORD:
        for key, value in list(FIXTURE["tmdb"].items()):
            if "/season/" in key and "/episode/" not in key and isinstance(value, dict):
                FIXTURE["tmdb"][key] = {field: v for field, v in value.items() if field != "episodes"}
        json.dump(FIXTURE, open(FIXTURE_PATH, "w"), indent=1, sort_keys=True, ensure_ascii=False)
        print("recorded %d TMDB and %d Plex answers" % (len(FIXTURE["tmdb"]), len(FIXTURE["plex"])))
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
