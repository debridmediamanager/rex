#!/usr/bin/env python3
"""Explicit source editions on recorded release inputs, not filename interpretation."""
import os
import shutil
import sys
import tempfile
import urllib.parse

from test_feed import Bytes, Server, check, results, serve
from test_feed_versions import catalog, files, item, movies


def main():
    source = Bytes()
    first, second = item(0), item(1)
    first["edition_title"] = "Director's Cut"
    second["edition_title"] = "Theatrical"
    for pushed in (first, second):
        source.files.add("/dav/" + pushed["path"])
    port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-editions-")
    catalog(work)
    server = Server(work, port)
    try:
        server.feed("POST", "items", {"items": [first, second]})
        keys = server.keys()
        director, theatrical = keys.get(first["id"]), keys.get(second["id"])
        check("different explicit editions of the same movie remain distinct",
              director != theatrical and len(movies(server)) == 2,
              [(row.get("ratingKey"), row.get("editionTitle")) for row in movies(server)])
        second["edition_title"] = "Director's Cut"
        server.feed("POST", "items", {"items": [second]})
        keys = server.keys()
        check("same-edition releases become versions of one movie",
              keys.get(second["id"]) == director and files(server.row(director)) == [first["file"], second["file"]], keys)
        omitted = dict(first)
        omitted.pop("edition_title")
        server.feed("POST", "items", {"items": [omitted]})
        check("an omitted edition preserves the source's last explicit edition",
              server.row(director).get("editionTitle") == "Director's Cut", server.row(director).get("editionTitle"))
        second["edition_title"] = "Extended"
        server.feed("POST", "items", {"items": [second]})
        keys = server.keys()
        extended = keys.get(second["id"])
        check("changing one folded release splits only that release into its own edition",
              extended != director and files(server.row(director)) == [first["file"]]
              and files(server.row(extended)) == [second["file"]]
              and server.row(extended).get("editionTitle") == "Extended", keys)
        query = urllib.parse.urlencode({"editionTitle": "My Edition", "editionTitle.locked": "1"})
        server.request("/library/metadata/%s?%s" % (director, query), "PUT")
        first["edition_title"] = "Source Changed"
        server.feed("POST", "items", {"items": [first]})
        check("a source update never overwrites a person's locked edition",
              server.row(director).get("editionTitle") == "My Edition", server.row(director).get("editionTitle"))
        second["edition_title"] = ""
        server.feed("POST", "items", {"items": [second]})
        check("an explicit empty edition clears an unlocked edition",
              server.row(extended).get("editionTitle", "") == "", server.row(extended).get("editionTitle"))
        before = server.keys()
        malformed = dict(second, edition_title=7)
        _, answer = server.feed("POST", "items", {"items": [malformed]})
        check("a malformed edition cannot mutate the existing release",
              bool(answer.get("rejected")) and server.keys() == before, answer)
        server.stop()
        server = Server(work, port)
        check("edition boundaries and locked edits survive restart",
              server.keys() == before and server.row(director).get("editionTitle") == "My Edition"
              and server.row(extended).get("editionTitle", "") == ""
              and files(server.row(extended)) == [second["file"]], server.keys())
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
