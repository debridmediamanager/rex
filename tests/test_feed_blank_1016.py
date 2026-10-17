#!/usr/bin/env python3
"""Rows the production rex drew the blank tile for on 2026-10-16 get named, and stay named.

`fixtures/feed/blank_1016.json` is zurg's entries for them, with the TMDB and Plex answers the
native server asked for them:

* Would I Lie to You? S03 releases whose files sit inside archives
  (`…S03E01.PROPER…/aaf-wilty.301.rar/aaf-wilty.301.avi`). A push folded each onto its episode
  as another version; the next start read the archive as the version's release, numbered it
  episode 301 and split it off into a show of that name with no ids. The `S03E01.PROPER` NZB
  holds two episodes, and its `aaf-wilty.302` is S03E02.
* `[HatSubs] One Piece 1106 (WEB 1080p) [97566ECF]`: the episode number spaced after the title
  was read as part of it, and each episode became a show of its own. It is an episode of One
  Piece; pushed with no id, it is left on that show for Fix Match, never named by its name.
* `Fairy.Tail.E233.Song.of.the.Faries`, an episode of Fairy Tail (TMDB's S6E7, likewise placed).
* `[Zoku] One Piece - Season 19 (Whole Cake) (783-842)/One Piece - 783 - Sanji's Homecoming!` and
  `[Koten_Gars] One Piece - Season 04-09/… One Piece - 0207`: the pack names a season and the file
  numbers from the show's first episode; episodes of One Piece (TMDB's S18E783 and S7E207, as placed).
* The three Evangelion Rebuild films, which zurg files under anime and so under TV Shows: their
  exact ids are films, refused as shows. They go to Movies.

The rows the production server already held that way (`rows`) are placed again by the retry a
start runs; held with no id, they stay unmatched.

Record the answers again: `REX_RECORD_ANSWERS=1 TMDB_KEY=... PLEX_TOKEN=... python3 tests/test_feed_blank_1016.py`.

    python3 tests/test_feed_blank_1016.py
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time

from test_feed import Bytes, Server, check, results, serve
import test_feed_blank_tiles as blank

HERE = os.path.dirname(os.path.abspath(__file__))
blank.FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "blank_1016.json")
blank.FIXTURE = FIXTURE = json.load(open(blank.FIXTURE_PATH))
NAMES = sorted(FIXTURE["pushes"])
catalog = blank.catalog


def start(work, bytes_port):
    keys = None
    if blank.RECORD:
        keys = {"TMDB_KEY": os.environ["TMDB_KEY"], "PLEX_TOKEN": os.environ["PLEX_TOKEN"]}
    return Server(work, bytes_port, keys=keys)


def item(name):
    """zurg's entry, with the provider `folder` zurg always sends ("" at a release's top level):
    the production server's rows came with it, and it is what reads a file's leading number."""
    return dict(blank.item(name), folder=FIXTURE["pushes"][name].get("folder", ""))


def tmdb_of(row):
    return next((int(guid[7:]) for guid in blank.guids(row) if guid.startswith("tmdb://")), None)


def check_named(label, server):
    keys = server.keys()
    key = lambda name: keys.get(item(name)["id"])
    for name in NAMES:
        want = FIXTURE["pushes"][name]["want"]
        row = server.row(key(name))
        if "with" in want:
            check("%s: %s is another version of %s" % (label, name, want["with"]),
                  key(name) is not None and key(name) == key(want["with"]), (key(name), key(want["with"])))
            continue
        if want.get("film"):
            check("%s: %s is a film in Movies, named" % (label, name),
                  row.get("type") == "movie" and str(row.get("librarySectionID")) == "1"
                  and not str(row.get("guid", "")).startswith(blank.LOCAL),
                  (row.get("type"), row.get("librarySectionID"), row.get("guid"), row.get("title")))
            continue
        show = server.row(row.get("grandparentRatingKey")) if row.get("grandparentRatingKey") else {}
        named = not str(show.get("guid", "local://")).startswith(blank.LOCAL)
        if "show_tmdb" in want:
            check("%s: %s is TMDB %s's S%02dE%02d, with TMDB's title" % (label, name, want["show_tmdb"], *want["numbers"]),
                  named and tmdb_of(show) == want["show_tmdb"] and [row.get("parentIndex"), row.get("index")] == want["numbers"]
                  and not re.fullmatch(r"Episode \d+", row.get("title") or ""),
                  (show.get("title"), tmdb_of(show), row.get("parentIndex"), row.get("index"), row.get("title")))
        else:
            pushed_ids = bool(FIXTURE["pushes"][name].get("ids"))
            check("%s: %s is an episode of %s%s" % (label, name, want["show_title"], "" if pushed_ids else ", left for Fix Match"),
                  named == pushed_ids and show.get("title") == want["show_title"], (show.get("title"), show.get("guid")))
    shows = [server.row(key(name)).get("grandparentTitle") or "" for name in NAMES]
    check("%s: no show is named after an archive or an episode number" % label,
          not any(title.startswith(("aaf-wilty", "One Piece 11")) for title in shows), shows)
    return keys


def main():
    source = Bytes()
    for pushed in FIXTURE["pushes"].values():
        source.files.add("/dav/" + pushed["path"])
    bytes_port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-blank1016-")
    catalog(work)
    server = start(work, bytes_port)
    try:
        status, body = server.feed("POST", "items", {"items": [item(name) for name in NAMES]})
        check("every file is taken", status == 200 and not (body or {}).get("rejected"), (status, body))
        keys = check_named("pushed", server)
    finally:
        blank.stop(server, work)

    # -- a start keeps every version on its episode and every film in Movies -----------------
    log = os.path.join(work, "server.log")
    unfolded = open(log, errors="replace").read().count("FEED unfolded")
    server = start(work, bytes_port)
    try:
        time.sleep(20)
        check_named("after a start", server)
        check("the start unfolds nothing and keeps every key",
              open(log, errors="replace").read().count("FEED unfolded") == unfolded and server.keys() == keys,
              {path: (keys.get(path), key) for path, key in server.keys().items() if keys.get(path) != key})
    finally:
        blank.stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    # -- rows held that way: a start's retry names them --------------------------------------
    work = tempfile.mkdtemp(prefix="rex-blank1016-")
    catalog(work, FIXTURE["rows"])
    server = start(work, bytes_port)
    try:
        episode = lambda title: next(row["ratingKey"] for row in FIXTURE["rows"] if row.get("_release", "").startswith(title))
        named = lambda row: not str(row.get("guid", "local://")).startswith(blank.LOCAL)
        wilty = blank.wait_for(server, episode("aaf-wilty"), lambda row: [row.get("parentIndex"), row.get("index")] == [3, 1])
        show = server.row(wilty.get("grandparentRatingKey")) if wilty.get("grandparentRatingKey") else {}
        check("the row held as the show `aaf-wilty 301 rar`, which has no id, is S03E01 again, left for Fix Match",
              show.get("title") == "Would I Lie To You" and tmdb_of(show) is None and [wilty.get("parentIndex"), wilty.get("index")] == [3, 1],
              (show.get("title"), tmdb_of(show), wilty.get("parentIndex"), wilty.get("index")))
        piece = blank.wait_for(server, episode("[HatSubs] One Piece"), lambda row: row.get("grandparentTitle") == "One Piece")
        check("the row held as the show `One Piece 1106` is an episode of One Piece",
              piece.get("grandparentTitle") == "One Piece", (piece.get("grandparentTitle"), piece.get("grandparentGuid")))
        film = blank.wait_for(server, episode("[Baws] Evangelion"), lambda row: row.get("type") == "movie")
        check("the row held as the show `Evangelion 1 11` is a film in Movies, named",
              film.get("type") == "movie" and str(film.get("librarySectionID")) == "1" and named(film),
              (film.get("type"), film.get("librarySectionID"), film.get("guid"), film.get("title")))
    finally:
        blank.stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    if blank.RECORD:
        json.dump(FIXTURE, open(blank.FIXTURE_PATH, "w"), indent=1, sort_keys=True, ensure_ascii=False)
        print("recorded %d TMDB and %d Plex answers" % (len(FIXTURE["tmdb"]), len(FIXTURE["plex"])))
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
