#!/usr/bin/env python3
"""A show matched by hand takes the picked show's episodes its rows' TMDB episode ids name.

`fixtures/feed/show_match_1023.json` holds the production rex's Ben 10: Omniverse show, its four
seasons and 80 episodes as they were on 2026-10-22: an old Fix Match had put the show on a guid
Plex's agent does not know, and its rows on Ben and Kate's (plex://show/5d9c08202192ba001f30832c),
67 episodes unmatched. Fix Matching the show to Plex's Ben 10: Omniverse
(plex://show/5d9c0853e98e47001eb0ce95) rewrote the show row alone: rex never asked for the picked
show's children, so every season and episode kept the earlier identity's guids. Plex's agent's
answers for the match are `plex` in the fixture: it lists the show as eight seasons of ten, the
release as four of twenty, so by number the release's S2E1 would be Plex's S2E1, another episode.
An episode whose source pushed TMDB's episode id is Plex's episode listing it; one with none is left
as it was unless every season holds as many episodes as Plex's.

Record the answers again with the claim's server token, through a relay at REX_RECORD_PLEXTV_URL
that reaches Plex's metadata provider (`<relay>/metadata.provider.plex.tv/...`) and nothing on
plex.tv, so the claimed test server announces nowhere (no key is written, its cache keys hold none):
`REX_RECORD_ANSWERS=1 PLEX_TOKEN=... REX_RECORD_PLEXTV_URL=http://127.0.0.1:PORT python3 tests/test_match_show_children.py`.

    python3 tests/test_match_show_children.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse

from test_feed import ENTRIES, LEGACY, MOUNT, Bytes, Server, check, results, serve

PROBE = ENTRIES["episode"]["entry"]["media_info"]

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "show_match_1023.json")
FIXTURE = json.load(open(FIXTURE_PATH))
RECORD = os.environ.get("REX_RECORD_ANSWERS") == "1"
SHOW = "136566"
PICKED = "plex://show/5d9c0853e98e47001eb0ce95"
OLD_SHOW = "plex://show/5d9c08202192ba001f30832c"
SHOWDOWN = "136506"
ROW = next(row for row in FIXTURE["rows"] if row["ratingKey"] == SHOWDOWN)
SHOWDOWN_ITEM = {"id": ROW["_sid"], "path": ROW["_sid"], "file": MOUNT + "/" + ROW["_sid"], "name": ROW["_release"],
                 "release": "/".join(ROW["_sid"].split("/")[:2]), "size": 1 << 30, "hash": ROW.get("_hash", ""),
                 "ids": {"tmdb": 46922}, "kind": "episode", "season": 2, "episode": 1, "tmdb_episode": 1107002}


def main():
    work = tempfile.mkdtemp(prefix="rex-showmatch-")
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fc", "friendlyName": "rex-showmatch",
               "sections": LEGACY["sections"], "items": FIXTURE["rows"]}, open(os.path.join(work, "catalog.json"), "w"))
    json.dump({} if RECORD else FIXTURE["plex"], open(os.path.join(work, "plex_cache.json"), "w"))
    server = Server(work, serve(Bytes().handler()), keys={"PLEX_TOKEN": os.environ["PLEX_TOKEN"], "REX_PLEXTV_URL": os.environ["REX_RECORD_PLEXTV_URL"]} if RECORD else None)
    try:
        watched = {row["ratingKey"] for row in FIXTURE["rows"] if row.get("viewCount")}
        # zurg's push of the Showdown file with the TMDB episode DMM places it on: stored on the row,
        # which a show matched by hand keeps where it is until the show is matched
        status, _ = server.feed("POST", "items", {"items": [dict(SHOWDOWN_ITEM, media_info=PROBE)]})
        check("the push is taken", status == 200, status)
        status, body = server.request("/library/metadata/%s/matches?manual=1&title=%s&year=2012"
                                      % (SHOW, urllib.parse.quote("Ben 10: Omniverse")))
        found = ((body or {}).get("MediaContainer") or {}).get("SearchResult") or [] if status == 200 else []
        check("Plex's agent offers Ben 10: Omniverse (2012) for the show", any(row.get("guid") == PICKED for row in found),
              [(row.get("guid"), row.get("name")) for row in found])
        status, _ = server.request("/library/metadata/%s/match?guid=%s&name=%s&year=2012"
                                   % (SHOW, urllib.parse.quote(PICKED, safe=""), urllib.parse.quote("Ben 10: Omniverse")), "PUT")
        check("the match is taken", status == 200, status)

        show = server.row(SHOW)
        episodes = server.rows("/library/metadata/%s/allLeaves" % SHOW)
        check("the show is Plex's Ben 10: Omniverse", show.get("guid") == PICKED, show.get("guid"))
        before = {row["ratingKey"]: row for row in FIXTURE["rows"] if row["type"] == "episode"}
        moved = server.row(SHOWDOWN)
        check("the episode its source named TMDB's 1107002 is Plex's S3E1 'Showdown (1)', under season 3",
              (moved.get("parentIndex"), moved.get("index"), moved.get("title")) == (3, 1, "Showdown (1)")
              and str(moved.get("guid", "")).startswith("plex://episode/") and moved.get("grandparentGuid") == PICKED,
              (moved.get("parentIndex"), moved.get("index"), moved.get("title"), moved.get("grandparentGuid")))
        season_3 = next((row for row in server.rows("/library/metadata/%s/children" % SHOW) if row.get("index") == 3), {})
        check("its season 3 is Plex's", str(season_3.get("guid", "")).startswith("plex://season/"), season_3.get("guid"))
        same = [row["ratingKey"] for row in episodes if row["ratingKey"] != SHOWDOWN
                and (row.get("guid"), row.get("parentIndex"), row.get("index")) ==
                (before[row["ratingKey"]]["guid"], before[row["ratingKey"]]["parentIndex"], before[row["ratingKey"]]["index"])]
        check("the 79 episodes with no id stay as they were: the seasons do not hold Plex's counts", len(same) == 79,
              [(row["ratingKey"], row.get("parentIndex"), row.get("index"), row.get("guid")) for row in episodes
               if row["ratingKey"] not in same and row["ratingKey"] != SHOWDOWN][:5])
        part_1 = next(row for row in episodes if (row.get("parentIndex"), row.get("index")) == (2, 2))
        check("the release's S2E2 is not Plex's S2E2", part_1.get("guid") == before[part_1["ratingKey"]]["guid"], part_1.get("guid"))
        kept = [key for key in watched if not server.row(key).get("viewCount")]
        check("watch state stays on its rows", not kept, kept)
    finally:
        server.stop()
        if RECORD:
            with sqlite3.connect(os.path.join(work, "plex_cache.db")) as db:
                FIXTURE["plex"].update({key: json.loads(value) for key, value in db.execute("SELECT key, value FROM cache")})
            json.dump(FIXTURE, open(FIXTURE_PATH, "w"), indent=1, sort_keys=True)
            print("recorded %d Plex answers" % len(FIXTURE["plex"]))
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
