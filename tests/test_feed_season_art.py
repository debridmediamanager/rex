#!/usr/bin/env python3
"""A season takes its identity and poster once one of its episodes is identified.

fun's rex drew the blank tile for 186 seasons of 131 shows on 2026-10-12, every one under
a show with a poster: Black Mirror's Season 6 sat on `local://112196` with no ids, no
summary and no poster. A season was made once, from its first episode. Its show was
re-identified in place when zurg's later push named the release (imdb:tt27731671 ->
tmdb:42009), but the season was found again by its index and left as it was made.

This drives the feed with the season's five real files and the TMDB and Plex answers
rex-fun had cached for them (`fixtures/feed/season_art.json`): pushed first with no ids,
so nothing names them, then with zurg's IMDb id, which re-identifies the show in place.
Then it starts on the rows rex-fun actually held, which a restart must repair, and on
the same rows with a poster a person picked, which stays.

    python3 tests/test_feed_season_art.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse

from test_feed import LEGACY, MOUNT, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "feed", "season_art.json")))
SHOW_GUID = "plex://show/5d9c0863ef619b002047f234"
# Plex's own guid for Season 6 (its answer for the show's children), and TMDB's season.
SEASON_GUID = "plex://season/640b5417e96cd974790c094f"
TMDB_SEASON = FIXTURE["tmdb"]["https://api.themoviedb.org/3/tv/42009/season/6?api_key=KEY"]
POSTER = "https://image.tmdb.org/t/p/w342" + TMDB_SEASON["poster_path"]
FUN_SEASON = "112196"


def item(entry, ids=True):
    path, data = entry["path"], entry["entry"]
    out = {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": data["name"],
           "release": path.rsplit("/", 1)[0], "size": data["size"], "hash": data["hash"],
           "media_info": data["media_info"]}
    if ids:
        out["ids"] = {"imdb": data["imdb_id"]}
    return out


def catalog(work, items=(), answers=True):
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fc", "friendlyName": "rex-seasons",
               "sections": LEGACY["sections"], "items": list(items)}, open(os.path.join(work, "catalog.json"), "w"))
    # The caches import their JSON once, into a database beside it.
    for name in ("tmdb_cache.db", "plex_cache.db"):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(os.path.join(work, name + suffix)):
                os.remove(os.path.join(work, name + suffix))
    json.dump(FIXTURE["tmdb"] if answers else {}, open(os.path.join(work, "tmdb_cache.json"), "w"))
    json.dump(FIXTURE["plex"] if answers else {}, open(os.path.join(work, "plex_cache.json"), "w"))


def season_of(server, show):
    seasons = server.rows("/library/metadata/%s/children?includeGuids=1" % show)
    return server.row(seasons[0]["ratingKey"]) if len(seasons) == 1 else {}


def poster(server, key):
    """The image the season's poster reads: the stored one `/posters` lists from TMDB."""
    listed = server.rows("/library/metadata/%s/posters" % key)
    return next((row.get("key") for row in listed if row.get("provider") == "tmdb"), None)


def episodes_parent(server, key):
    return {row.get("parentGuid") for row in server.rows("/library/metadata/%s/children" % key)}


def identified(server, season, show_guid, what):
    key = season.get("ratingKey")
    ids = [guid.get("id") for guid in season.get("Guid") or []]
    check("%s: the season takes Plex's guid for it" % what, season.get("guid") == SEASON_GUID,
          (season.get("guid"), key))
    check("%s: and TMDB's season id, and its summary" % what,
          "tmdb://%s" % TMDB_SEASON["id"] in ids and season.get("summary") == TMDB_SEASON["overview"],
          (ids, (season.get("summary") or "")[:60]))
    check("%s: and its parent is the show as it is now" % what, season.get("parentGuid") == show_guid,
          season.get("parentGuid"))
    check("%s: and its episodes name it as their parent" % what,
          episodes_parent(server, key) == {SEASON_GUID}, episodes_parent(server, key))


def wait_for(server, key, until, seconds=30):
    """A restart repairs seasons off the request path: the row once it shows."""
    deadline = time.time() + seconds
    row = server.row(key)
    while not until(row) and time.time() < deadline:
        time.sleep(0.5)
        row = server.row(key)
    return row


source = Bytes()


def main():
    pushed = [item(entry) for entry in FIXTURE["entries"]]
    for one in pushed:
        source.files.add("/dav/" + one["path"])
    bytes_port = serve(source.handler())

    # -- live: the release is named after its episodes were placed ----------------------------
    work = tempfile.mkdtemp(prefix="rex-seasons-")
    catalog(work)
    server = Server(work, bytes_port)
    try:
        status, body = server.feed("POST", "items", {"items": [item(entry, ids=False) for entry in FIXTURE["entries"]]})
        keys = server.keys()
        episode = server.row(keys[pushed[0]["id"]])
        show, season = episode.get("grandparentRatingKey"), episode.get("parentRatingKey")
        check("pushed with no ids, nothing names Black Mirror: its show and season are the local agent's",
              status == 200 and body.get("created") == 5 and str(server.row(show).get("guid")).startswith("local://")
              and str(server.row(season).get("guid")).startswith("local://"),
              (status, body, server.row(show).get("guid"), server.row(season).get("guid")))
        status, body = server.feed("POST", "items", {"items": pushed})
        check("zurg's IMDb id re-identifies the show in place, as it did on fun",
              status == 200 and body.get("updated") == 5 and server.row(show).get("guid") == SHOW_GUID
              and server.keys() == keys, (body, server.row(show).get("guid")))
        row = season_of(server, show)
        check("the season stays the one season, on its key", row.get("ratingKey") == season, (row.get("ratingKey"), season))
        identified(server, row, SHOW_GUID, "re-identified")
        check("re-identified: its poster is TMDB's for the season, not the blank tile", poster(server, season) == POSTER,
              poster(server, season))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    # -- restart: the rows rex-fun held are repaired --------------------------------------
    work = tempfile.mkdtemp(prefix="rex-seasons-")
    rows = [dict(row, _source="zurg") if row.get("_source") else row for row in FIXTURE["rows"]]
    catalog(work, rows)
    server = Server(work, bytes_port)
    try:
        row = wait_for(server, FUN_SEASON, lambda row: row.get("guid") == SEASON_GUID)
        identified(server, row, SHOW_GUID, "fun's Season 6 after a restart")
        check("fun's Season 6 after a restart: its poster is TMDB's for the season", poster(server, FUN_SEASON) == POSTER,
              poster(server, FUN_SEASON))
    finally:
        server.stop()
    # The repair was saved, not made again: no TMDB or Plex answers this time.
    catalog(work, rows, answers=False)
    server = Server(work, bytes_port)
    try:
        row = server.row(FUN_SEASON)
        check("the repaired season is kept across the next restart",
              row.get("guid") == SEASON_GUID and poster(server, FUN_SEASON) == POSTER,
              (row.get("guid"), poster(server, FUN_SEASON)))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    # -- a poster a person picked stays -------------------------------------------------------
    work = tempfile.mkdtemp(prefix="rex-seasons-")
    picked = "https://image.tmdb.org/t/p/w342/picked-by-hand.jpg"
    catalog(work, rows, answers=False)
    server = Server(work, bytes_port)
    try:
        status, _ = server.request("/library/metadata/%s/poster?url=%s" % (FUN_SEASON, urllib.parse.quote(picked, safe="")),
                                   "PUT")
        check("a person picks the unidentified season's poster", status == 200 and poster(server, FUN_SEASON) == picked,
              (status, poster(server, FUN_SEASON)))
    finally:
        server.stop()
    catalog(work, rows)
    server = Server(work, bytes_port)
    try:
        row = wait_for(server, FUN_SEASON, lambda row: row.get("guid") == SEASON_GUID)
        check("the season takes its guid on the next start and keeps the poster the person picked",
              row.get("guid") == SEASON_GUID and poster(server, FUN_SEASON) == picked,
              (row.get("guid"), poster(server, FUN_SEASON)))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if os.environ.get("REX_SERVER") == "python":
        print("the feed is rex's own API; the Python oracle has none, so this suite is native only")
        sys.exit(0)
    sys.exit(main())
