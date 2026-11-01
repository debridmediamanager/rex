#!/usr/bin/env python3
"""A file its source now says nothing places leaves the title it was placed as.

`fixtures/feed/unplaced_1101.json` is four of the eleven rows the production server held on
2026-11-01 for `Second.Chance.1995.dvdrip-xpost`, a Japanese series of eleven 46-minute
episodes: zurg-usenet had sent each as the 1996 film Second Chance, the id it found for the
release, which DMM placed the whole release by. DMM no longer places a film on a release of
numbered episodes, and zurg then sends each file with `kind: "unplaced"` (docs/feed-api.md):
the source looked and nothing places it. An absent `kind` still leaves a row where the last
placement put it.

    python3 tests/test_feed_unplaced.py
"""
import json
import os
import shutil
import tempfile

from test_feed import ENTRIES, LEGACY, MOUNT, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "feed", "unplaced_1101.json")))
PROBE = ENTRIES["episode"]["entry"]["media_info"]
FILM = "plex://movie/5d776bf7ad5437001f7af8f1"


def pushed(name):
    out = {field: value for field, value in FIXTURE["pushes"][name].items() if field != "row"}
    out.update(file="%s/%s" % (MOUNT, out["path"]), media_info=PROBE)
    return out


def main():
    source = Bytes()
    for push in FIXTURE["pushes"].values():
        source.files.add("/dav/" + push["path"])
    bytes_port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-unplaced-")
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000f8", "friendlyName": "rex-unplaced",
               "sections": LEGACY["sections"], "items": FIXTURE["rows"]}, open(os.path.join(work, "catalog.json"), "w"))
    for name in ("tmdb", "plex"):
        json.dump({}, open(os.path.join(work, name + "_cache.json"), "w"))
    server = Server(work, bytes_port)
    try:
        pushes = FIXTURE["pushes"]
        for name, push in sorted(pushes.items()):
            check("%s is the film before" % name, server.row(push["row"]).get("guid") == FILM, server.row(push["row"]).get("guid"))
        status, body = server.feed("POST", "items", {"items": [pushed(name) for name in sorted(pushes)]})
        check("every item is taken", status == 200 and not (body or {}).get("rejected"), (status, body))
        keys = server.keys()
        for name in ("Second.Chance.01", "Second.Chance.02", "Second.Chance.03"):
            push = pushes[name]
            check("%s: the film's row it was is gone" % name, not server.row(push["row"]), server.row(push["row"]).get("guid"))
            row = server.row(keys.get(push["id"])) if keys.get(push["id"]) else {}
            check("%s is shown unmatched" % name, str(row.get("guid", "")).startswith("local://"), (keys.get(push["id"]), row.get("guid")))
        control = pushes["Second.Chance.04"]
        check("pushed with no kind, Second.Chance.04 stays where its placement put it",
              server.row(control["row"]).get("guid") == FILM and keys.get(control["id"]) == control["row"],
              (keys.get(control["id"]), server.row(control["row"]).get("guid")))
        status, _ = server.feed("POST", "items", {"items": [pushed(name) for name in sorted(pushes)]})
        check("pushed again, nothing changes", status == 200 and server.keys() == keys, server.keys())
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
