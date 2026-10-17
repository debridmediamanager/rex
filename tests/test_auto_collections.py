#!/usr/bin/env python3
"""A movie library's "Minimum automatic collection size" makes the collections Plex's agent files its movies under.

wason92 in #rex-support, 2026-10-16: a movie library created with automatic collections off and
switched on later never built any, because a real PMS makes them during a metadata refresh and rex
runs none. The library here is the production rex's (2026-10-17): its movies were described before
rex kept their collections, so turning the setting on must ask Plex's agent again. The agent's
answers are its own, recorded out of that server's cache (`fixtures/provider/collections.json`):
The Matrix, Reloaded and Revolutions, each filed under "The Matrix Collection".

    python3 tests/test_auto_collections.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

from test_feed import LEGACY, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
ANSWERS = json.load(open(os.path.join(HERE, "fixtures", "provider", "collections.json")))
MATRIX_GUID = "plex://collection/5ec2fd508509540041a16274"


def library(work):
    """The three films as rows a source pushed before this change: described, but with no collections."""
    template = next(row for row in LEGACY["items"] if row["type"] == "movie")
    rows, cache = [], {}
    for index, answer in enumerate(ANSWERS):
        film = answer["response"]["MediaContainer"]["Metadata"][0]
        key = str(100000 + index)
        row = json.loads(json.dumps(template))
        row.update({"ratingKey": key, "key": "/library/metadata/%s" % key, "guid": film["guid"], "title": film["title"],
                    "year": film["year"], "originallyAvailableAt": film.get("originallyAvailableAt"),
                    "Guid": [{"id": entry["id"]} for entry in film["Guid"]], "librarySectionID": 1})
        row["Media"][0]["Part"][0]["file"] = "/zurg/movies/%s/%s.mkv" % (film["title"], film["title"])
        rows.append(row)
        body = json.dumps(answer["body"], sort_keys=True)
        cache["%s %s %s" % (answer["method"], answer["path"], body)] = answer["response"]
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fc", "friendlyName": "rex-auto",
               "sections": LEGACY["sections"], "items": rows}, open(os.path.join(work, "catalog.json"), "w"))
    json.dump(cache, open(os.path.join(work, "plex_cache.json"), "w"))


def collections(server):
    return server.rows("/library/sections/1/collections")


def wait_for(server, count, seconds=20):
    deadline = time.time() + seconds
    while time.time() < deadline:
        found = collections(server)
        if len(found) >= count:
            return found
        time.sleep(0.25)
    return collections(server)


def main():
    work = tempfile.mkdtemp(prefix="rex-auto-collections-")
    bytes_port = serve(Bytes().handler())
    library(work)
    server = Server(work, bytes_port)
    try:
        check("no collection while the setting is off", collections(server) == [], collections(server))
        # Plex Web's Edit Library dialog, Advanced tab: the whole form with the setting in `prefs[...]`.
        status, _ = server.request("/library/sections/1?agent=tv.plex.agents.movie&type=movie&scanner=Plex%20Movie"
                                   "&language=en-US&name=Movies&prefs%5BautoCollectionThreshold%5D=2", "PUT")
        check("the edit is accepted", status == 200, status)
        found = wait_for(server, 1)
        check("turned on later, the setting makes the agent's collection", len(found) == 1, found)
        matrix = found[0] if found else {}
        check("named and identified as the agent's", (matrix.get("title"), matrix.get("guid"))
              == ("The Matrix Collection", MATRIX_GUID), matrix)
        check("the agent's summary", matrix.get("summary", "").startswith("The Matrix collection"), matrix.get("summary"))
        members = server.rows("/library/collections/%s/children" % matrix.get("ratingKey"))
        check("all three films, in release order", [row["title"] for row in members]
              == ["The Matrix", "The Matrix Reloaded", "The Matrix Revolutions"], [row["title"] for row in members])
        film = server.row("100000")
        check("a member carries the collection's tag",
              [tag.get("tag") for tag in film.get("Collection") or []] == ["The Matrix Collection"], film.get("Collection"))
        check("and no field of rex's own", "_collections" not in film, sorted(film))
        server.request("/library/sections/1/refresh")
        time.sleep(2)
        check("a refresh makes no second one", len(collections(server)) == 1, collections(server))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
