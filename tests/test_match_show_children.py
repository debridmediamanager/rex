#!/usr/bin/env python3
"""A show matched by hand takes the picked show's seasons and episodes, by their numbers.

`fixtures/feed/show_match_1023.json` holds the production rex's Ben 10: Omniverse show, its four
seasons and 80 episodes as they were on 2026-10-22: an old Fix Match had put the show on a guid
Plex's agent does not know, and its rows on Ben and Kate's (plex://show/5d9c08202192ba001f30832c),
67 episodes unmatched. Fix Matching the show to Plex's Ben 10: Omniverse
(plex://show/5d9c0853e98e47001eb0ce95) rewrote the show row alone: rex never asked for the picked
show's children, so every season and episode kept the earlier identity's guids. Plex's agent's
answers for the match are `plex` in the fixture.

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

from test_feed import LEGACY, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "show_match_1023.json")
FIXTURE = json.load(open(FIXTURE_PATH))
RECORD = os.environ.get("REX_RECORD_ANSWERS") == "1"
SHOW = "136566"
PICKED = "plex://show/5d9c0853e98e47001eb0ce95"
OLD_SHOW = "plex://show/5d9c08202192ba001f30832c"


def main():
    work = tempfile.mkdtemp(prefix="rex-showmatch-")
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fc", "friendlyName": "rex-showmatch",
               "sections": LEGACY["sections"], "items": FIXTURE["rows"]}, open(os.path.join(work, "catalog.json"), "w"))
    json.dump({} if RECORD else FIXTURE["plex"], open(os.path.join(work, "plex_cache.json"), "w"))
    server = Server(work, serve(Bytes().handler()), keys={"PLEX_TOKEN": os.environ["PLEX_TOKEN"], "REX_PLEXTV_URL": os.environ["REX_RECORD_PLEXTV_URL"]} if RECORD else None)
    try:
        watched = {row["ratingKey"] for row in FIXTURE["rows"] if row.get("viewCount")}
        status, body = server.request("/library/metadata/%s/matches?manual=1&title=%s&year=2012"
                                      % (SHOW, urllib.parse.quote("Ben 10: Omniverse")))
        found = ((body or {}).get("MediaContainer") or {}).get("SearchResult") or [] if status == 200 else []
        check("Plex's agent offers Ben 10: Omniverse (2012) for the show", any(row.get("guid") == PICKED for row in found),
              [(row.get("guid"), row.get("name")) for row in found])
        status, _ = server.request("/library/metadata/%s/match?guid=%s&name=%s&year=2012"
                                   % (SHOW, urllib.parse.quote(PICKED, safe=""), urllib.parse.quote("Ben 10: Omniverse")), "PUT")
        check("the match is taken", status == 200, status)

        show = server.row(SHOW)
        seasons = server.rows("/library/metadata/%s/children" % SHOW)
        episodes = server.rows("/library/metadata/%s/allLeaves" % SHOW)
        check("the show is Plex's Ben 10: Omniverse", show.get("guid") == PICKED, show.get("guid"))
        check("every season is one of the picked show's",
              len(seasons) == 4 and all(str(row.get("guid", "")).startswith("plex://season/") and row.get("parentGuid") == PICKED
                                        for row in seasons), [(row.get("index"), row.get("guid"), row.get("parentGuid")) for row in seasons])
        stale = [row.get("ratingKey") for row in episodes if OLD_SHOW in (row.get("grandparentGuid"), row.get("parentGuid"))]
        check("no episode is left on Ben and Kate's show", len(episodes) == 80 and not stale, (len(episodes), stale[:5]))
        # Plex's agent lists the show as eight seasons of ten episodes, the release as four of
        # twenty: the numbers Plex lists are its episodes, the rest unmatched (rex reads no name)
        listed = {(row.get("parentIndex"), row.get("index")) for row in episodes if row.get("index", 99) <= 10}
        matched = {(row.get("parentIndex"), row.get("index")) for row in episodes if str(row.get("guid", "")).startswith("plex://episode/")}
        local = [row for row in episodes if (row.get("parentIndex"), row.get("index")) not in listed]
        check("each episode Plex lists for its numbers is matched (S1-S4 E1-E10, 40 rows)", matched == listed and len(matched) == 40,
              sorted(listed ^ matched)[:10])
        check("an episode Plex lists none for is unmatched, not on the old show's",
              all(str(row.get("guid", "")).startswith("local://") for row in local) and len(local) == 40,
              [(row.get("parentIndex"), row.get("index"), row.get("guid")) for row in local if not str(row.get("guid", "")).startswith("local://")][:5])
        sample = next((row for row in episodes if (row.get("parentIndex"), row.get("index")) == (1, 5)), {})
        check("S01E05 takes its title from the picked show", sample.get("title") not in (None, "", "Episode 5"), sample.get("title"))
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
