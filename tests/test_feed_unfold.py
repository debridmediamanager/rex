#!/usr/bin/env python3
"""Each file of a season batch is its own episode, and rows that held several are unfolded.

The production rex held 232 episodes whose versions were different episodes (2026-10-14):
a batch named for its show's first season ([Reza] Food Wars, [IK] Toaru Kagaku no Railgun)
put every file's season on the anime-lists entry of season 1, so S01-S05 E01 were one
episode; and a pack identified through its IMDb id as one title gave all its episodes one
guid, which the fold took for versions of one episode (The Boys S03E01-E08). This pushes
zurg's entries for those files (`fixtures/feed/unfold.json`) and checks each lands on its
own row and numbers; then folds them as the production server held them (an episode
Merge, which keeps each release's source) and checks a start unfolds them again.

Record the answers again: `REX_RECORD_ANSWERS=1 TMDB_KEY=... python3 tests/test_feed_unfold.py`.

    python3 tests/test_feed_unfold.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

from test_feed import Bytes, check, results, serve
import test_feed_anime as anime

HERE = os.path.dirname(os.path.abspath(__file__))
anime.FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "unfold.json")
anime.FIXTURE = FIXTURE = json.load(open(anime.FIXTURE_PATH))


def placed_all(server, keys, names):
    return {name: anime.wait_for(server, keys[FIXTURE["pushes"][name]["path"]], FIXTURE["pushes"][name]["want"], 30)
            for name in names}


def check_placed(label, got, keys, names):
    for name in names:
        want = FIXTURE["pushes"][name]["want"]
        ok = got[name][1:] == want[1:] and (want[0] is None or got[name][0] == want[0])
        check("%s: %s is season %s episode %s" % (label, name, want[1], want[2]), ok, got[name])
    rows = [keys[FIXTURE["pushes"][name]["path"]] for name in names]
    check("%s: every file is a row of its own" % label, len(set(rows)) == len(rows), rows)


def main():
    source = Bytes()
    for pushed in FIXTURE["pushes"].values():
        source.files.add("/dav/" + pushed["path"])
    bytes_port = serve(source.handler())
    names = sorted(FIXTURE["pushes"])

    # -- live: each file of a batch is its own episode -------------------------------------
    work = tempfile.mkdtemp(prefix="rex-unfold-")
    anime.catalog(work)
    server = anime.start(work, bytes_port, True)
    try:
        status, body = server.feed("POST", "items", {"items": [anime.item(name) for name in names]})
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
              len(folded) == len(names) and len(set(folded.values())) == len(FIXTURE["folds"]), folded)
    finally:
        anime.stop(server, work)

    # -- a start unfolds them and places each on its numbers -------------------------------
    server = anime.start(work, bytes_port, True)
    try:
        deadline = time.time() + 90
        keys = server.keys()
        while len(set(keys.values())) < len(names) and time.time() < deadline:
            time.sleep(1)
            keys = server.keys()
        got = placed_all(server, keys, names)
        check_placed("after a start", got, keys, names)
        for group, survivor in sorted(FIXTURE["folds"].items()):
            path = FIXTURE["pushes"][survivor]["path"]
            check("%s's survivor keeps its key" % group, keys.get(path) == folded.get(path), (folded.get(path), keys.get(path)))
    finally:
        anime.stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    if os.environ.get("REX_RECORD_ANSWERS") == "1":
        for key, value in list(FIXTURE["tmdb"].items()):
            if "/season/" in key and "/episode/" not in key and isinstance(value, dict):
                FIXTURE["tmdb"][key] = {field: v for field, v in value.items() if field != "episodes"}
        json.dump(FIXTURE, open(anime.FIXTURE_PATH, "w"), indent=1, sort_keys=True, ensure_ascii=False)
        print("recorded %d TMDB and %d Plex answers" % (len(FIXTURE["tmdb"]), len(FIXTURE["plex"])))
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if os.environ.get("REX_SERVER") == "python":
        print("the feed is rex's own API; the Python oracle has none, so this suite is native only")
        sys.exit(0)
    sys.exit(main())
