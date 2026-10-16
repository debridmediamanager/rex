#!/usr/bin/env python3
"""Each file of a season batch is its own episode, and rows that held several are unfolded.

The production rex held 232 episodes whose versions were different episodes (2026-10-14):
a batch named for its show's first season ([Reza] Food Wars, [IK] Toaru Kagaku no Railgun)
put every file's season on season 1, so S01-S05 E01 were one episode; and a pack identified
through its IMDb id as one title gave all its episodes one guid, which the fold took for
versions of one episode (The Boys S03E01-E08). This pushes zurg's entries for those files
(`fixtures/feed/unfold.json`) and checks each lands on its own row and numbers; then folds
them as the production server held them (an episode Merge, which keeps each release's
source) and checks a start unfolds them again, while two releases of one Re:Zero episode
named apart (`- 71`, `S04E05`), which zurg pushes placed on TMDB's S1E71 (`PLACED`), stay
one row on every start.

Record the answers again: `REX_RECORD_ANSWERS=1 TMDB_KEY=... python3 tests/test_feed_unfold.py`.

    python3 tests/test_feed_unfold.py
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
FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "unfold.json")
FIXTURE = json.load(open(FIXTURE_PATH))
RECORD = os.environ.get("REX_RECORD_ANSWERS") == "1"
PROBE = ENTRIES["episode"]["entry"]["media_info"]
# Files only their source can number as TMDB does: TMDB keeps Re:Zero as one season, and the
# scene release names its fourth (TVDB's) season.
PLACED = {"rezero_feibanyama_s04e05", "rezero_subsplease_71"}


def item(name):
    pushed = FIXTURE["pushes"][name]
    path = pushed["path"]
    out = {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": pushed["name"],
           "release": "/".join(path.split("/")[:2]), "size": pushed["size"], "hash": pushed["hash"],
           "ids": pushed["ids"], "media_info": PROBE}
    if name in PLACED:
        show, season, episode = pushed["want"]
        out.update(ids=dict(pushed["ids"], tmdb=show), kind="episode", season=season, episode=episode)
    return out


def catalog(work):
    sections = json.loads(json.dumps(LEGACY["sections"]))
    next(section for section in sections if section["type"] == "show")["Location"].append({"id": 3, "path": "/zurg/anime"})
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fa", "friendlyName": "rex-unfold",
               "sections": sections, "items": []}, open(os.path.join(work, "catalog.json"), "w"))
    for name in ("tmdb", "plex"):
        json.dump({} if RECORD else FIXTURE[name], open(os.path.join(work, name + "_cache.json"), "w"))


def start(work, bytes_port):
    keys = None
    if RECORD:
        keys = {"TMDB_KEY": os.environ["TMDB_KEY"], "PLEX_TOKEN": os.environ.get("PLEX_TOKEN", "fake")}
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
        time.sleep(0.1)
        got = placed(server, key)
    return got


def placed_all(server, keys, names):
    return {name: wait_for(server, keys[FIXTURE["pushes"][name]["path"]], FIXTURE["pushes"][name]["want"], 30)
            for name in names}


def check_placed(label, got, keys, names):
    for name in names:
        want = FIXTURE["pushes"][name]["want"]
        ok = got[name][1:] == want[1:] and (want[0] is None or got[name][0] == want[0])
        check("%s: %s is season %s episode %s" % (label, name, want[1], want[2]), ok, got[name])
    key = lambda name: keys[FIXTURE["pushes"][name]["path"]]
    rows = [key(name) for name in names if "with" not in FIXTURE["pushes"][name]]
    check("%s: every episode is a row of its own" % label, len(set(rows)) == len(rows), rows)
    for name in names:
        if "with" in FIXTURE["pushes"][name]:
            check("%s: %s is a version of %s" % (label, name, FIXTURE["pushes"][name]["with"]),
                  key(name) == key(FIXTURE["pushes"][name]["with"]), (key(name), key(FIXTURE["pushes"][name]["with"])))


def main():
    source = Bytes()
    for pushed in FIXTURE["pushes"].values():
        source.files.add("/dav/" + pushed["path"])
    bytes_port = serve(source.handler())
    names = sorted(FIXTURE["pushes"])
    rows_wanted = [name for name in names if "with" not in FIXTURE["pushes"][name]]

    # -- live: each file of a batch is its own episode -------------------------------------
    work = tempfile.mkdtemp(prefix="rex-unfold-")
    catalog(work)
    server = start(work, bytes_port)
    try:
        status, body = server.feed("POST", "items", {"items": [item(name) for name in names]})
        check("every file is taken", status == 200, (status, body))
        keys = server.keys()
        check_placed("pushed", placed_all(server, keys, names), keys, names)

        # -- folded before, as the production server held them ------------------------------
        for group, survivor in sorted(FIXTURE["folds"].items()):
            target = keys[FIXTURE["pushes"][survivor]["path"]]
            others = sorted({keys[FIXTURE["pushes"][name]["path"]] for name in names
                             if name.startswith(group + "_") and name != survivor} - {target})
            status, _ = server.request("/library/metadata/%s/merge?ids=%s" % (target, ",".join(others)), "PUT")
            check("%s folded onto %s (%d releases)" % (group, target, len(others) + 1), status == 200, status)
        folded = server.keys()
        check("a merge keeps each release's source: the feed lists every file on its survivor",
              len(folded) == len(names) and len(set(folded.values())) == len(FIXTURE["folds"]) + 1, folded)
    finally:
        stop(server, work)

    # -- a start unfolds them and places each on its numbers -------------------------------
    server = start(work, bytes_port)
    try:
        deadline = time.time() + 90
        keys = server.keys()
        while len(set(keys.values())) < len(rows_wanted) and time.time() < deadline:
            time.sleep(0.1)
            keys = server.keys()
        got = placed_all(server, keys, names)
        check_placed("after a start", got, keys, names)
        for group, survivor in sorted(FIXTURE["folds"].items()):
            path = FIXTURE["pushes"][survivor]["path"]
            check("%s's survivor keeps its key" % group, keys.get(path) == folded.get(path), (folded.get(path), keys.get(path)))
    finally:
        stop(server, work)

    # -- and the start after that leaves every row as it is ----------------------------------
    log = os.path.join(work, "server.log")
    unfolded = open(log, errors="replace").read().count("FEED unfolded")
    server = start(work, bytes_port)
    try:
        time.sleep(20)
        check("the next start unfolds nothing (versions of one episode stay one row)",
              open(log, errors="replace").read().count("FEED unfolded") == unfolded and server.keys() == keys,
              open(log, errors="replace").read().count("FEED unfolded") - unfolded)
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
