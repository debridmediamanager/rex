#!/usr/bin/env python3
"""The source decides what a file is; rex shows what it pushed.

zurg asks DMM what each file is and pushes the answer (`kind`, the movie's or show's ids, TMDB's
season and episode), or a TMDB id a person set by hand in zurg; anything else arrives with
neither, 0.9% of the production server's episodes and 1.1% of its movies (2026-10-17). rex read
names until then: it searched them, read ids out of them, asked TMDB's /find for an IMDb id, and
numbered and moved episodes by them, at every push and every start. These are real pushes and
rows from the feed fixtures, against a server whose Plex agent (`REX_PLEXTV_URL`) is a local
recorder, so every question rex asks is seen; rex reads no TMDB (2026-10-17):

(a) Pushed unplaced -- `Kingdom.S01E03` (`anime.json`), Bungo Stray Dogs' S04E01 with only its IMDb
    id (`anime.json`), Lovely Runner with only a TVDB id (`names.json`), and Saturday Night Live's
    three `01`s of one pack (`pack_folders.json`): each is unmatched, in a show named after its
    release, season 1, numbered in its release's file order, and nobody is asked anything.
(b) Placed -- Case Closed on TMDB's S1E1103 with only its IMDb id: exactly there, and Plex's
    agent is asked only by its external id.
(c) A TMDB id set by hand, no `kind` -- the same Bungo Stray Dogs file, and A Serbian Film
    (`blank_tiles.json`), with Plex's answers recorded for them: that title, in its library's
    kind, the episode still on its file-order number. The id is trusted as pushed, checked
    against nothing (Pawn Stars' episode id 1559521 too, which zurg's own Plex once pushed as a
    show's): a title Plex does not know keeps its release's name, and nothing is searched.
(d) Rows held -- the production server's rows of 2026-10-16 (`blank_1016.json`), a show named after
    an archive, one after an episode number, a film among the shows: through two starts nothing
    is renumbered, unfolded, moved or identified again, and nobody is asked anything.

    python3 tests/test_feed_source_decides.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import time

from test_feed import ENTRIES, LEGACY, MOUNT, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(HERE, "fixtures", "feed")
FIXTURES = {name: json.load(open(os.path.join(FEED, name + ".json")))
            for name in ("anime", "names", "pack_folders", "blank_tiles", "blank_1016")}
PROBE = ENTRIES["episode"]["entry"]["media_info"]
SNL = ["snl-s07-01-no-folder", "snl-s13-01", "snl-s14-01"]


def item(fixture, name, **override):
    """zurg's push of `name`, as recorded, with `override` (ids, kind, ...) on top."""
    pushed = FIXTURES[fixture]["pushes"][name]
    path = pushed["path"]
    out = {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": pushed["name"],
           "release": "/".join(path.split("/")[:2]), "size": pushed["size"], "hash": pushed["hash"],
           "ids": pushed["ids"], "media_info": PROBE}
    if "folder" in pushed:
        out["folder"] = pushed["folder"]
    out.update(override)
    return out


class Recorder:
    """Plex's agent: every request kept, nothing known."""

    def __init__(self):
        self.seen = []

    def handler(recorder):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                recorder.seen.append((self.path, body.decode("utf-8", "replace")))
                # The agent's no match.
                status, answer = 200, b'{"MediaContainer": {"size": 0, "Metadata": []}}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            do_POST = do_PUT = do_GET
        return Handler

    def asked(self):
        """What identification asked of Plex's agent."""
        return [(path, body) for path, body in self.seen if path.startswith("/metadata.provider.plex.tv/")]


def catalog(work, rows=(), answers=None):
    """The catalog, and the TMDB and Plex answers recorded in a fixture whose keys hold one of
    `answers[fixture]`, as the caches a lookup reads first: a request they hold never reaches the
    recorder, so only the titles under test are answered."""
    sections = json.loads(json.dumps(LEGACY["sections"]))
    next(section for section in sections if section["type"] == "show")["Location"].append({"id": 3, "path": "/zurg/anime"})
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000f6", "friendlyName": "rex-source-decides",
               "sections": sections, "items": list(rows)}, open(os.path.join(work, "catalog.json"), "w"))
    kept = {key: value for fixture, wanted in (answers or {}).items() for key, value in FIXTURES[fixture].get("plex", {}).items()
            if any(part in key for part in wanted)}
    json.dump(kept, open(os.path.join(work, "plex_cache.json"), "w"))


def start(work, bytes_port, recorder_port):
    base = "http://127.0.0.1:%d" % recorder_port
    return Server(work, bytes_port, keys={"REX_PLEXTV_URL": base})


def where(server, pushed):
    """(show title, show guid, the show's external ids, season, episode) an episode row sits on."""
    row = server.row(server.keys().get(pushed["id"]))
    show = server.row(row.get("grandparentRatingKey")) if row.get("grandparentRatingKey") else {}
    ids = [guid.get("id") for guid in show.get("Guid") or []]
    return show.get("title"), str(show.get("guid", "")), ids, row.get("parentIndex"), row.get("index")


def pushes(bytes_port, recorder, recorder_port):
    work = tempfile.mkdtemp(prefix="rex-source-decides-")
    catalog(work, answers={"anime": ["/tv/65931?"], "blank_tiles": ["/movie/73861?", "tmdb://73861"]})
    server = start(work, bytes_port, recorder_port)
    try:
        # -- (a) unplaced: unmatched, named after its release, in file order, nobody asked ----------
        unplaced = [item("anime", "kingdom_scene"), item("anime", "bungo_scene"), item("names", "lovely_runner")]
        snl = [item("pack_folders", name) for name in SNL]
        recorder.seen.clear()
        status, body = server.feed("POST", "items", {"items": unplaced + snl})
        check("every unplaced file is taken", status == 200 and body.get("created") == 6 and not body.get("rejected"), (status, body))
        for pushed in unplaced:
            title, guid, ids, season, episode = where(server, pushed)
            check("%s: unmatched, the first of season 1 of a show named after its release, not its name's numbers"
                  % pushed["name"][:40],
                  title == pushed["name"] and guid.startswith("local://") and not ids and (season, episode) == (1, 1),
                  (title, guid, ids, season, episode))
        got = [where(server, pushed) for pushed in snl]
        by_path = sorted(snl, key=lambda pushed: pushed["path"])
        check("Saturday Night Live's three `01`s are one show named after their pack, numbered in file order",
              len({row[1] for row in got}) == 1 and all(row[0] == snl[0]["name"] for row in got)
              and [where(server, pushed)[3:] for pushed in by_path] == [(1, 1), (1, 2), (1, 3)],
              [(pushed["path"].rsplit("/", 1)[1], where(server, pushed)[3:]) for pushed in by_path])
        check("nobody was asked about an unplaced file: no metadata provider, no Plex agent",
              recorder.asked() == [], recorder.asked()[:4])
        keys = server.keys()
        status, body = server.feed("POST", "items", {"items": unplaced + snl})
        check("pushed again, nothing is numbered or named again", body.get("unchanged") == 6 and server.keys() == keys, body)

        # -- (b) placed: exactly where pushed, Plex asked only by its external id ---------------------
        recorder.seen.clear()
        conan = item("anime", "case_closed", kind="episode", season=1, episode=1103)
        server.feed("POST", "items", {"items": [conan]})
        title, guid, ids, season, episode = where(server, conan)
        check("Case Closed, placed on TMDB's S1E1103, is there and known by its id, whatever its name numbers",
              (season, episode) == (1, 1103) and "imdb://tt0131179" in ids, (title, ids, season, episode))
        asked = recorder.asked()
        check("Plex's agent is asked only by its external id, and nothing else is asked",
              all(path.startswith("/metadata.provider") for path, _ in asked)
              and all('"guid"' in body and '"title"' not in body for path, body in asked if path.endswith("/matches")),
              asked)

        # -- (c) a TMDB id set by hand: that title in its library's kind ---------------------------
        recorder.seen.clear()
        bungo = item("anime", "bungo_scene", ids={"tmdb": 65931})
        serbian = item("blank_tiles", "serbian", ids={"tmdb": 73861})
        status, body = server.feed("POST", "items", {"items": [bungo, serbian]})
        title, guid, ids, season, episode = where(server, bungo)
        check("Bungo Stray Dogs, given TMDB 65931 by hand, is that show, its episode still its file-order first",
              "tmdb://65931" in ids and (season, episode) == (1, 1) and server.keys().get(bungo["id"]) == keys.get(bungo["id"]),
              (body, title, ids, season, episode))
        film = server.row(server.keys().get(serbian["id"]))
        check("A Serbian Film, given TMDB 73861 by hand, is that film",
              film.get("type") == "movie" and film.get("title") == "A Serbian Film" and {"id": "tmdb://73861"} in (film.get("Guid") or []),
              (film.get("type"), film.get("title"), film.get("Guid")))
        runner = item("names", "lovely_runner", ids={"tmdb": 1559521})
        server.feed("POST", "items", {"items": [runner]})
        title, guid, ids, season, episode = where(server, runner)
        check("Lovely Runner given 1559521 by hand is trusted as that id: Plex knows none, so it keeps its release's name, its first file",
              title == runner["name"] and guid.startswith("local://") and ids == ["tmdb://1559521"] and (season, episode) == (1, 1),
              (title, guid, ids, season, episode))
        check("and no title was searched for, by any of them", not any("/search" in path for path, _ in recorder.asked()), recorder.asked())
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)


def held(bytes_port, recorder, recorder_port):
    """(d): rows a start must leave as they are."""
    rows = FIXTURES["blank_1016"]["rows"]
    placement = ("type", "librarySectionID", "parentIndex", "index", "parentRatingKey", "grandparentRatingKey")
    fields = lambda row: ("title", "guid") + (placement if row.get("type") == "episode" else ())
    want = {row["ratingKey"]: {field: row.get(field) for field in fields(row)} for row in rows}
    work = tempfile.mkdtemp(prefix="rex-source-decides-held-")
    catalog(work, rows)
    try:
        for label in ("a start", "the next start"):
            recorder.seen.clear()
            server = start(work, bytes_port, recorder_port)
            try:
                time.sleep(15)
                got = {key: {field: server.row(key).get(field) for field in want[key]} for key in want}
                for key in sorted(want):
                    check("after %s, row %s (%s) is as it was held" % (label, key, want[key]["title"]), got[key] == want[key],
                          got[key])
                check("after %s, nobody was asked about them" % label, recorder.asked() == [], recorder.asked()[:4])
            finally:
                server.stop()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    source = Bytes()
    for fixture in FIXTURES.values():
        for pushed in fixture.get("pushes", {}).values():
            source.files.add("/dav/" + pushed["path"])
    bytes_port = serve(source.handler())
    recorder = Recorder()
    recorder_port = serve(recorder.handler())
    pushes(bytes_port, recorder, recorder_port)
    held(bytes_port, recorder, recorder_port)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
