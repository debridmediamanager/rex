#!/usr/bin/env python3
"""A source puts its files in playlists: `playlists` on a feed item (docs/feed-api.md).

Tracks are `music_rig.py`'s plexav library; films are rex-stremio's push of
`fixtures/feed/collections_stremio_1025.json`. Each title an item names is an ordinary playlist of
the owner's, its members in the positions the source gave, and only what changed since the
source's last list is applied, as its `collections` are:

* a member a person took out stays out, a member a person added keeps its place;
* absent keeps, `{}` takes the item out of every playlist the source named;
* a playlist the source empties is deleted, unless a person's own members are left in it.

    python3 tests/test_feed_playlists.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile

import music_rig as M
from music_rig import check, results

HERE = os.path.dirname(os.path.abspath(__file__))


def playlists(rex):
    return {row["title"]: row for row in rex.rows("/playlists")}


def members(rex, title):
    playlist = playlists(rex).get(title)
    return [row["title"] for row in rex.rows("/playlists/%s/items" % playlist["ratingKey"])] if playlist else None


def tracks(rex):
    rex_items = rex.request("/rex/feed/plexav/items")[1]["items"]
    return {row["id"]: row["ratingKey"] for row in rex_items}


def music():
    work, items, rex, _, _ = M.start()
    try:
        status, body = rex.push(items)
        check("the plexav tracks are taken", status == 200 and body.get("created") == 46, (status, body))
        rows = {row["ratingKey"]: row["title"] for row in rex.rows("/library/sections/1/all?type=10")}
        keys = tracks(rex)
        a, b, c, d = sorted(keys)[:4]
        name = {item: rows[keys[item]] for item in (a, b, c, d)}
        by_id = {item["id"]: item for item in items}

        def push(changes):
            sent = []
            for item, value in changes.items():
                pushed = copy.deepcopy(by_id[item])
                if value is not None:
                    pushed["playlists"] = value
                sent.append(pushed)
            return rex.push(sent)

        status, body = push({a: {"Mix": 3}, b: {"Mix": 1}, c: {"Mix": 2}})
        check("three tracks naming Mix are updated", status == 200 and body.get("updated") == 3, (status, body))
        mix = playlists(rex).get("Mix", {})
        check("Mix is an audio playlist of three, not smart",
              mix.get("playlistType") == "audio" and mix.get("leafCount") == 3 and mix.get("smart") is False, mix)
        check("its members are in the source's order", members(rex, "Mix") == [name[b], name[c], name[a]], members(rex, "Mix"))
        row = rex.rows("/library/metadata/%s" % keys[a])[0]
        check("the row answers no field of rex's own", not [field for field in row if field.startswith("_")], sorted(row))

        status, body = push({a: {"Mix": 3}, b: {"Mix": 1}, c: {"Mix": 2}})
        check("the same lists again change nothing", status == 200 and body.get("unchanged") == 3, (status, body))

        # A person takes one out and adds one of their own.
        entries = rex.rows("/playlists/%s/items" % mix["ratingKey"])
        first = next(row for row in entries if row["title"] == name[b])
        status, _ = rex.request("/playlists/%s/items/%s" % (mix["ratingKey"], first["playlistItemID"]), "DELETE")
        check("a person takes the first track out", status == 200, status)
        uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (M.MACHINE, keys[d])
        status, _ = rex.request("/playlists/%s/items?uri=%s" % (mix["ratingKey"], uri), "PUT")
        check("and adds a track of their own", status == 200 and members(rex, "Mix")[-1] == name[d], (status, members(rex, "Mix")))
        status, body = push({a: {"Mix": 1}, b: {"Mix": 3}, c: {"Mix": 2}})
        check("the source moves two tracks", status == 200 and body.get("updated") == 2, (status, body))
        check("the moved track takes its place, the one taken out stays out, the person's stays last",
              members(rex, "Mix") == [name[a], name[c], name[d]], members(rex, "Mix"))

        status, body = push({a: None})
        check("an item sent without playlists keeps its place", members(rex, "Mix") == [name[a], name[c], name[d]],
              (status, members(rex, "Mix")))
        push({a: {}, b: {}, c: {}})
        check("{} takes the source's tracks out; the person's own keeps the playlist",
              members(rex, "Mix") == [name[d]], members(rex, "Mix"))

        push({a: {"Gone": 1}})
        check("a second title is a playlist of its own", members(rex, "Gone") == [name[a]], members(rex, "Gone"))
        push({a: {}})
        check("a playlist the source empties is deleted", "Gone" not in playlists(rex), sorted(playlists(rex)))

        status, body = push({a: {"Mix": 0}})
        check("a position below 1 is refused",
              status == 200 and body.get("rejected") == [{"id": a, "reason": "playlists must map each title to its position, from 1"}],
              (status, body))
        status, body = push({a: ["Mix"]})
        check("a list of titles is refused", status == 200 and len(body.get("rejected") or []) == 1, (status, body))
    finally:
        rex.stop()
        shutil.rmtree(work, ignore_errors=True)


def films():
    from test_feed import Bytes, Server, catalog, serve
    items = json.load(open(os.path.join(HERE, "fixtures", "feed", "collections_stremio_1025.json")))["items"]
    source = Bytes()
    bytes_port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-playlists-")
    catalog(work)
    server = Server(work, bytes_port)
    try:
        movies = [copy.deepcopy(item) for item in items if item.get("kind") == "movie"]
        for position, item in enumerate(reversed(movies), start=1):
            item["playlists"] = {"Tonight": position}
        status, body = server.feed("POST", "items", {"items": movies})
        check("films naming Tonight are taken", status == 200 and not body.get("rejected"), (status, body))
        listed = {row["title"]: row for row in server.rows("/playlists")}
        tonight = listed.get("Tonight", {})
        keys = server.keys()
        expected = [server.row(keys[item["id"]])["title"] for item in reversed(movies)]
        check("Tonight is a video playlist", tonight.get("playlistType") == "video", tonight)
        check("its films are in the source's order",
              [row["title"] for row in server.rows("/playlists/%s/items" % tonight.get("ratingKey"))] == expected, expected)

        server.stop()
        server = Server(work, bytes_port)
        listed = {row["title"]: row for row in server.rows("/playlists")}
        check("Tonight and its order survive a restart",
              [row["title"] for row in server.rows("/playlists/%s/items" % listed.get("Tonight", {}).get("ratingKey"))] == expected,
              sorted(listed))
        for item in movies:
            item["playlists"] = {}
        server.feed("POST", "items", {"items": movies})
        check("and stay the source's: emptied after the restart, it is deleted",
              "Tonight" not in {row["title"] for row in server.rows("/playlists")}, server.rows("/playlists"))
    finally:
        server.stop()
    shutil.rmtree(work, ignore_errors=True)


def main():
    music()
    films()
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
