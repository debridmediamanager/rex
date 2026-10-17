#!/usr/bin/env python3
"""Two releases of one title are one row with two Media, as a real PMS makes them.

The production rex listed 127 Hours twice on 2026-10-11: two releases in one zurg, each pushed
as its own item, identified to the same Plex guid, made two movies (407 movies and
~1,370 episodes doubled across the library). A real PMS's movie agent makes every file
of one title one item and offers them as versions. This drives the feed with zurg's
real entries for both releases (`fixtures/feed/duplicate_releases.json`), then starts
on the two rows the production rex actually held, which a restart must fold.

    python3 tests/test_feed_versions.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

from test_feed import BYTES_TOKEN, BODY, MOUNT, LEGACY, Bytes, Events, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "feed", "duplicate_releases.json")))
GUID = "plex://movie/5d77687d7e5fa10020bf080d"


def item(index, path=None):
    """zurg's entry, pushed as zurg pushes what DMM placed: a film, by its IMDb id and the TMDB id
    the fixture's answers name (44115)."""
    entry = FIXTURE["entries"][index]
    path = path or entry["path"]
    data = entry["entry"]
    return {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": data["name"],
            "release": path.rsplit("/", 1)[0], "size": data["size"], "hash": data["hash"],
            "media_info": data["media_info"], "ids": {"imdb": data["imdb_id"], "tmdb": 44115}, "kind": "movie"}


def catalog(work, items=()):
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fd", "friendlyName": "rex-versions",
               "sections": LEGACY["sections"], "items": list(items)}, open(os.path.join(work, "catalog.json"), "w"))
    json.dump(FIXTURE["tmdb"], open(os.path.join(work, "tmdb_cache.json"), "w"))
    json.dump(FIXTURE["plex"], open(os.path.join(work, "plex_cache.json"), "w"))


def movies(server):
    return [row for row in server.rows("/library/sections/1/all?includeGuids=1") if row.get("guid") == GUID]


def files(row):
    return [part.get("file") for media in row.get("Media") or [] for part in media.get("Part") or []]


def read(server, row, file):
    """A part read of the Part reading `file`: the path the byte source was asked for."""
    source.seen.clear()
    part = [part for media in row.get("Media") or [] for part in media.get("Part") or [] if part.get("file") == file]
    if not part:
        return False, []
    status, raw = server.request(part[0]["key"], headers={"Range": "bytes=0-9"})
    hits = [seen[1] for seen in source.seen if seen[0] == "GET"]
    return status == 206 and raw == BODY[:10], hits[-1:]


source = Bytes()


def main():
    one, two = item(0), item(1)
    for pushed in (one, two):
        source.files.add("/dav/" + pushed["path"])
    bytes_port = serve(source.handler())

    # -- live: the second release joins the first ------------------------------------
    work = tempfile.mkdtemp(prefix="rex-versions-")
    catalog(work)
    server = Server(work, bytes_port)
    try:
        server.feed("POST", "items", {"items": [one]})
        key = server.keys().get(one["id"])
        server.request("/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % key)
        events = Events(server.port)
        mark = events.mark()
        status, body = server.feed("POST", "items", {"items": [two]})
        # Plex for Mac drew a release announced as a new row and then folded away in Recently
        # Added with the trash icon (the production server, 2026-10-13: The Darjeeling Limited).
        time.sleep(0.5)
        others = [(entry.get("itemID"), entry.get("state")) for name, frame in events.frames[mark:] if name == "timeline"
                  for entry in (frame.get("TimelineEntry") if isinstance(frame.get("TimelineEntry"), list)
                                else [frame.get("TimelineEntry")])
                  if entry and entry.get("itemID") != key and entry.get("state") in (0, 9)]
        check("the second release is never announced as a row of its own, created or deleted", others == [], others)
        listed = movies(server)
        row = server.row(key)
        check("the second release of a title is no new movie", status == 200 and body.get("created") == 0
              and [r.get("ratingKey") for r in listed] == [key], (body, [(r.get("ratingKey"), r.get("title")) for r in listed]))
        check("it is a second Media of the first release's row, as a real PMS's versions",
              files(row) == [one["file"], two["file"]] and row.get("viewCount") == 1, (files(row), row.get("viewCount")))
        check("the feed lists both ids on that one key", server.keys() == {one["id"]: key, two["id"]: key}, server.keys())
        for pushed in (one, two):
            ok, hit = read(server, row, pushed["file"])
            check("each version streams from its own release's path (%s)" % pushed["release"][:24],
                  ok and hit == ["/dav/" + pushed["path"]], (ok, hit))
        status, body = server.feed("POST", "items", {"items": [one, two]})
        check("both pushed again change nothing", body.get("unchanged") == 2 and body.get("created") == 0, body)

        moved = item(1, "movies/127 Hours moved/" + two["path"].rsplit("/", 1)[1])
        moved["id"] = two["id"]
        source.files.add("/dav/" + moved["path"])
        server.feed("POST", "items", {"items": [moved]})
        row = server.row(key)
        ok, hit = read(server, row, moved["file"])
        check("a moved second release moves its own Part, not the first's",
              files(row) == [one["file"], moved["file"]] and ok and hit == ["/dav/" + moved["path"]], (files(row), hit))

        status, body = server.feed("DELETE", "items", {"ids": [one["id"], one["id"]]})
        row = server.row(key)
        ok, hit = read(server, row, moved["file"])
        check("repeating one release id drops its Media once; the row stays, out of the trash",
              status == 200 and body.get("trashed") == 0 and files(row) == [moved["file"]] and not row.get("deletedAt")
              and server.keys() == {two["id"]: key} and body.get("deleted") == 1 and body.get("unknown") == 0,
              (body, files(row), row.get("deletedAt")))
        check("and the release left streams from its own path", ok and hit == ["/dav/" + moved["path"]], hit)
        status, body = server.feed("PUT", "snapshot", {"items": [moved]})
        check("a snapshot naming the release left removes nothing", status == 200 and server.keys() == {two["id"]: key},
              (status, body, server.keys()))
        server.feed("DELETE", "items", {"ids": [two["id"]]})
        check("removing the last release trashes the row as before", server.row(key).get("deletedAt"), server.row(key))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    # -- restart: the two rows the production rex held are folded ----------------------------
    work = tempfile.mkdtemp(prefix="rex-versions-")
    # They were the production rex's `rd` source's; the test's source is `zurg`.
    catalog(work, [dict(row, _source="zurg") for row in FIXTURE["rows"]])
    server = Server(work, bytes_port)
    try:
        old = [row["ratingKey"] for row in FIXTURE["rows"]]
        listed = movies(server)
        row = server.row(old[0])
        check("the production rex's two 127 Hours rows start as one movie on the older key",
              [r.get("ratingKey") for r in listed] == [old[0]] and len(row.get("Media") or []) == 2
              and server.request("/library/metadata/%s" % old[1])[0] == 404,
              ([(r.get("ratingKey"), len(r.get("Media") or [])) for r in listed], old))
        check("the feed finds both of its releases on that key",
              server.keys() == {r["_sid"]: old[0] for r in FIXTURE["rows"]}, server.keys())
        status, body = server.feed("POST", "items", {"items": [one, two]})
        check("their next push is that row, not two new ones", body.get("created") == 0 and len(movies(server)) == 1,
              (body, len(movies(server))))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    # -- a save cut short: the survivor written, the absorbed row not yet deleted ------
    work = tempfile.mkdtemp(prefix="rex-versions-")
    survivor, absorbed = [dict(row, _source="zurg") for row in FIXTURE["rows"]]
    tag = lambda row: dict(row["Media"][0], _source="zurg", _sid=row["_sid"], _path=row["_path"])
    catalog(work, [dict(survivor, Media=[tag(survivor), tag(absorbed)]), absorbed])
    server = Server(work, bytes_port)
    try:
        row = server.row(survivor["ratingKey"])
        check("an absorbed row back after a cut-short save folds without a second copy of its Media",
              len(movies(server)) == 1 and files(row) == [m["Part"][0]["file"] for m in (survivor["Media"][0], absorbed["Media"][0])],
              files(row))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
