#!/usr/bin/env python3
"""Every release of one episode is one row, whatever guid identification gave it, the trash's too.

`fixtures/feed/versions_1017.json` is what the production rex held on 2026-10-17:

* SpongeBob's S10E64, which neither TMDB nor Plex lists: five `local://` rows of four releases on
  one season, so the fold, which went by the agent's guid, folded none; one of them in the trash.
* Freakazoid's S1E13: the Dooky release, and beside it the AndreMor one in the trash, which kept
  the show's trash icon on; and S1E2, watched, in the trash since zurg's 04:21:34 snapshot stopped
  naming it. The other AndreMor NZB of S01E02, pushed as zurg pushes it once it lists its archive,
  brings that row back rather than making one beside it.
* Beavis and Butt-Head's S1E4, three files of one Mike Judge collection (Daria's, Tales From the
  Tour Bus's and Beavis and Butt-Head's own S01E04): other files of one release are no versions
  of each other, and stay three rows.

Record the answers again: `REX_RECORD_ANSWERS=1 TMDB_KEY=... PLEX_TOKEN=... python3 tests/test_feed_versions_1017.py`.

    python3 tests/test_feed_versions_1017.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

from test_feed import ENTRIES, MOUNT, Bytes, check, results, serve
import test_feed_extras as extras

HERE = os.path.dirname(os.path.abspath(__file__))
extras.FIXTURE_PATH = FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "versions_1017.json")
extras.FIXTURE = FIXTURE = json.load(open(FIXTURE_PATH))
PROBE = ENTRIES["episode"]["entry"]["media_info"]
SPONGEBOB = ("141637", ["144800", "155103", "161428", "161429"])
FREAKAZOID = ("150532", ["148968"])
BEAVIS = ["156638", "156648", "156653"]


def files(row):
    return [part.get("file", "").rsplit("/", 1)[-1] for media in row.get("Media") or [] for part in media.get("Part") or []]


def own_files(key):
    row = next(row for row in FIXTURE["rows"] if row["ratingKey"] == key)
    return files(row) if "deletedAt" not in row else []


def check_rows(label, server):
    for name, (survivor, others) in (("SpongeBob S10E64", SPONGEBOB), ("Freakazoid S1E13", FREAKAZOID)):
        want = sorted(own_files(survivor) + [file for key in others for file in own_files(key)])
        left = [key for key in others if server.row(key)]
        check("%s: one row, %s, holding every release not in the trash" % (label, survivor),
              not left and sorted(files(server.row(survivor))) == want, (left, sorted(files(server.row(survivor))), want))
    rows = {key: files(server.row(key)) for key in BEAVIS}
    check("%s: Beavis and Butt-Head S1E4's three files of one collection stay three rows" % label,
          all(len(held) == 1 for held in rows.values()), rows)
    show = server.row("118624")
    check("%s: SpongeBob is out of the trash with its trashed episode gone" % label, not show.get("deletedAt"), show.get("deletedAt"))


def main():
    source = Bytes()
    for pushed in FIXTURE["pushes"].values():
        source.files.add("/dav/" + pushed["path"])
    bytes_port = serve(source.handler())

    work = tempfile.mkdtemp(prefix="rex-versions1017-")
    extras.catalog(work, FIXTURE["rows"])
    server = extras.start(work, bytes_port)
    try:
        check_rows("after a start", server)

        # -- a release of a trashed episode brings it back --------------------------------------
        pushed = FIXTURE["pushes"]["freakazoid_bang_s01e02"]
        item = dict(pushed, file="%s/%s" % (MOUNT, pushed["path"]), media_info=PROBE)
        status, body = server.feed("POST", "items", {"items": [item]})
        check("the other S01E02 NZB is taken", status == 200 and not (body or {}).get("rejected"), (status, body))
        key = server.keys().get(pushed["id"])
        row = server.row("166114")
        check("it is S1E2's row, 166114, out of the trash with the new file as its Media",
              key == "166114" and not row.get("deletedAt") and files(row) == [pushed["path"].rsplit("/", 1)[-1]],
              (key, row.get("deletedAt"), files(row)))
        check("which keeps its watch state", row.get("viewCount") == 1, row.get("viewCount"))
        show, season = server.row("135397"), server.row("135398")
        check("and Freakazoid! and its Season 1 are out of the trash", not show.get("deletedAt") and not season.get("deletedAt"),
              (show.get("deletedAt"), season.get("deletedAt")))
        keys = server.keys()
    finally:
        extras.stop(server, work)

    # -- the next start leaves every row as it is -----------------------------------------------
    log = os.path.join(work, "server.log")
    folds = open(log, errors="replace").read().count("FEED folded")
    server = extras.start(work, bytes_port)
    try:
        time.sleep(10)
        check_rows("after the next start", server)
        check("the next start folds nothing and keeps every key",
              open(log, errors="replace").read().count("FEED folded") == folds and server.keys() == keys,
              (open(log, errors="replace").read().count("FEED folded") - folds,
               {path: (keys.get(path), key) for path, key in server.keys().items() if keys.get(path) != key}))
    finally:
        extras.stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    if extras.RECORD:
        json.dump(FIXTURE, open(FIXTURE_PATH, "w"), indent=1, sort_keys=True, ensure_ascii=False)
        open(FIXTURE_PATH, "a").write("\n")
        print("recorded %d TMDB and %d Plex answers" % (len(FIXTURE["tmdb"]), len(FIXTURE["plex"])))
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
