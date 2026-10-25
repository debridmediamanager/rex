#!/usr/bin/env python3
"""A source puts its titles in collections: `collections` on a feed item (docs/feed-api.md).

`fixtures/feed/collections_stremio_1025.json` is rex-stremio's push with catalogs as
collections: Tenzing is in Cinemeta's Popular and Featured films, sent once naming both, and
a series' episodes name the collection their show is in. Each name is applied as a person's
`Collection` tag write is (`test_tag_collections.py`): a tag naming no collection makes one.

    python3 tests/test_feed_collections.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile

from test_feed import Bytes, Server, catalog, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
ITEMS = json.load(open(os.path.join(HERE, "fixtures", "feed", "collections_stremio_1025.json")))["items"]
SHARED = "stc-stremio-films-tt6262770"


def collections(server, section):
    """Each collection of a library and the titles in it."""
    return {row["title"]: sorted(member["title"] for member in server.rows("/library/collections/%s/children" % row["ratingKey"]))
            for row in server.rows("/library/sections/%s/collections" % section)}


def again(**changes):
    item = copy.deepcopy(next(item for item in ITEMS if item["id"] == SHARED))
    item.update(changes)
    if item.get("collections") is None:
        item.pop("collections", None)
    return item


def main():
    source = Bytes()
    bytes_port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-collections-")
    catalog(work)
    server = Server(work, bytes_port)
    try:
        status, body = server.feed("POST", "items", {"items": ITEMS})
        check("the push is taken", status == 200 and not (body or {}).get("rejected"), (status, body))
        keys = server.keys()
        film = server.row(keys[SHARED])
        others = {item["id"]: server.row(keys[item["id"]])["title"] for item in ITEMS if item.get("kind") == "movie"}
        movies, shows = film["librarySectionID"], server.row(keys["stc-stremio-shows-tt11540908-1-1"])["librarySectionID"]
        show = server.row(server.row(keys["stc-stremio-shows-tt11540908-1-1"])["grandparentRatingKey"])["title"]
        expected = {"DMM Cast": [others["stc-stremio-films-tt30144839"]],
                    "Top films": sorted([others["stc-stremio-films-tt34384900"], film["title"]]),
                    "Featured films": [film["title"]]}
        check("each named collection is made in the film's library, holding what names it",
              collections(server, movies) == expected, collections(server, movies))
        check("an episode's collection holds its show", collections(server, shows) == {"Top shows": [show]},
              collections(server, shows))
        check("the film carries both collections as tags",
              sorted(tag["tag"] for tag in film.get("Collection", [])) == ["Featured films", "Top films"], film.get("Collection"))

        # A person takes the film out of a collection: the same list pushed again leaves it out.
        featured = next(row for row in server.rows("/library/sections/%s/collections" % movies) if row["title"] == "Featured films")
        status, _ = server.request("/library/collections/%s/items/%s" % (featured["ratingKey"], film["ratingKey"]), "DELETE")
        check("a person takes the film out of Featured films", status == 200, status)
        status, body = server.feed("POST", "items", {"items": [again()]})
        check("the same list again changes nothing", status == 200 and (body or {}).get("unchanged") == 1, (status, body))
        check("and the film stays out", film["title"] not in collections(server, movies).get("Featured films", []),
              collections(server, movies))

        # A collection a person made stays the film's whatever the source sends.
        status, _ = server.request("/library/metadata/%s?collection[0].tag.tag=Watch%%20soon&collection[1].tag.tag=Top%%20films"
                                   "&collection[2].tag.tag=Featured%%20films" % film["ratingKey"], "PUT")
        check("a person adds the film to a collection of their own", status == 200, status)
        status, body = server.feed("POST", "items", {"items": [again(collections=["Top films"])]})
        check("a list without Featured films updates the film", status == 200 and (body or {}).get("updated") == 1, (status, body))
        now = collections(server, movies)
        check("it leaves the collection the source dropped and keeps the rest",
              film["title"] not in now.get("Featured films", []) and film["title"] in now["Top films"]
              and now.get("Watch soon") == [film["title"]], now)

        status, body = server.feed("POST", "items", {"items": [again(collections=None)]})
        now = collections(server, movies)
        check("an item sent without collections keeps the film's", status == 200 and film["title"] in now["Top films"], (status, now))
        status, body = server.feed("POST", "items", {"items": [again(collections=[])]})
        now = collections(server, movies)
        check("an empty list takes it out of the source's collections only",
              film["title"] not in now["Top films"] and now.get("Watch soon") == [film["title"]], now)

        status, body = server.feed("POST", "items", {"items": [again(collections="Top films")]})
        check("collections that are no list of names are refused",
              status == 200 and (body or {}).get("rejected") == [{"id": SHARED, "reason": "collections must be a list of names"}],
              (status, body))
    finally:
        server.stop()
    shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
