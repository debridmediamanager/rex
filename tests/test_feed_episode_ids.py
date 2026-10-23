#!/usr/bin/env python3
"""A placed episode is Plex's episode its source's TMDB episode id names, and every page of a season is read.

`fixtures/provider/feed_episodes_1023.json` is Plex's metadata provider's own answers, page by page as
rex asks them (50 a page), recorded with `probes/record_provider_pages.py` on 2026-10-23;
`fixtures/feed/feed_episodes_1023.json` is the production rex's rows for two of the releases. A stub
provider answers from the recording and plex.tv answers nothing, so no claimed test server reaches it.

* Animaniacs' season 1 is 160 episodes (Detective Conan's 1,218): rex read the first 50, so the
  production rex's S1E109, which zurg placed so, stayed unmatched, and the 50 rows were kept in the
  cache for good;
* Re:ZERO's S1E84 (TMDB 7130181) sits on the second page of the season Plex lists it in;
* Ben 10: Omniverse's `S02E01 - Showdown Part 1` is TMDB's and Plex's S3E1 (TMDB 1107002): a push that
  names TMDB's episode lands there whatever numbers it carries; one with none, at its numbers;
* SpongeBob's Frys S10 pack, which zurg placed as S9E15 for its S10E24, was a version of a row holding
  three other releases' S10E24 and never left it.

    python3 tests/test_feed_episode_ids.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading

from test_feed import ENTRIES, LEGACY, MOUNT, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
ANSWERS = json.load(open(os.path.join(HERE, "fixtures", "provider", "feed_episodes_1023.json")))["answers"]
ROWS = json.load(open(os.path.join(HERE, "fixtures", "feed", "feed_episodes_1023.json")))["rows"]
PROBE = ENTRIES["episode"]["entry"]["media_info"]
PREFIX = "/metadata.provider.plex.tv"
SEASON_1 = "/library/metadata/65dd8dbef97685e8c5506592/children"
asked = []


def body_of(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


class Provider(http.server.BaseHTTPRequestHandler):
    """Plex's provider as recorded, paged by `X-Plex-Container-Start`; plex.tv is a 404."""

    def answer(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = body_of(self.rfile.read(size).decode()) if size else None
        path = self.path[len(PREFIX):] if self.path.startswith(PREFIX + "/") else None
        start = int(self.headers.get("X-Plex-Container-Start") or 0)
        found = next((a["response"] for a in ANSWERS if path and a["method"] == self.command and a["path"] == path
                      and a["start"] == start and body_of(a["body"]) == body), None)
        asked.append((self.command, path, start))
        raw = json.dumps(found).encode() if found is not None else b""
        self.send_response(200 if found is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = answer

    def log_message(self, *args):
        pass


def provider():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % server.server_port


def item(path, release, ids, season, episode, tmdb_episode=None):
    pushed = {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": release.split("/")[-1],
              "release": release, "size": 1 << 30, "hash": "", "ids": ids, "media_info": PROBE,
              "kind": "episode", "season": season, "episode": episode}
    if tmdb_episode:
        pushed["tmdb_episode"] = tmdb_episode
    return pushed


def first_page():
    return next(a["response"] for a in ANSWERS if a["path"] == SEASON_1 and a["start"] == 0)


def main():
    kenny = next(row for row in ROWS if row["ratingKey"] == "141597")
    animaniacs = next(row for row in ROWS if row["ratingKey"] == "118183")
    frys = next(media for media in kenny["Media"] if "Frys" in media["_sid"])
    showdown = "shows/Ben 10 Omniverse (2012) Season 1-4 S01-04 (1080p HMAX.WEBDL x265 10bit AAC 2.0 EDGE2020)/Season 2/Ben 10 Omniverse - S02E01 - Showdown Part 1 (1080p x265 EDGE2020).mkv"
    tummy = "shows/Ben 10 Omniverse (2012) Season 1-4 S01-04 (1080p HMAX.WEBDL x265 10bit AAC 2.0 EDGE2020)/Season 2/Ben 10 Omniverse - S02E03 - Tummy Trouble (1080p x265 EDGE2020).mkv"
    rezero = "shows/Re ZERO Starting Life in Another World S04E18 MULTi 1080p CR WEB-DL AAC2.0 x264-Tsundere-Raws/Re ZERO Starting Life in Another World S04E18 MULTi 1080p CR WEB-DL AAC2.0 x264-Tsundere-Raws.mkv"
    release = lambda path: "/".join(path.split("/")[:2])
    source = Bytes()
    for path in (frys["_sid"], animaniacs["_sid"], showdown, tummy, rezero):
        source.files.add("/dav/" + path)
    work = tempfile.mkdtemp(prefix="rex-epids-")
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fd", "friendlyName": "rex-epids",
               "sections": LEGACY["sections"], "items": ROWS}, open(os.path.join(work, "catalog.json"), "w"))
    # What the production rex kept of Animaniacs' season 1: its first page, for good.
    json.dump({"GET %s " % SEASON_1: first_page()}, open(os.path.join(work, "plex_cache.json"), "w"))
    server = Server(work, serve(source.handler()), keys={"REX_PLEXTV_URL": provider()})
    try:
        status, body = server.feed("POST", "items", {"items": [
            item(animaniacs["_sid"], release(animaniacs["_sid"]), {"tmdb": 82, "imdb": "tt0105941"}, 1, 109),
            item(rezero, release(rezero), {"tmdb": 65942}, 1, 84, 7130181),
            item(showdown, release(showdown), {"tmdb": 46922}, 2, 1, 1107002),
            item(tummy, release(tummy), {"tmdb": 46922}, 2, 3),
            item(frys["_sid"], release(frys["_sid"]), {"tmdb": 387, "imdb": "tt0206512"}, 9, 15),
        ]})
        check("every push is taken", status == 200 and not (body or {}).get("rejected"), (status, body))
        keys = server.keys()

        row = server.row(animaniacs["ratingKey"])
        check("Animaniacs' S1E109, past the 50 rows kept of its season, is Plex's episode on its own key",
              str(row.get("guid", "")).startswith("plex://episode/") and (row.get("parentIndex"), row.get("index")) == (1, 109),
              (row.get("guid"), row.get("parentIndex"), row.get("index")))
        pages = sorted(start for method, path, start in asked if path == SEASON_1)
        check("the season is read page by page to its totalSize", pages == list(range(0, 160, 50)), pages[:4])

        row = server.row(keys.get(rezero))
        check("Re:ZERO's S1E84 on the season's second page is Plex's episode naming TMDB 7130181",
              (row.get("parentIndex"), row.get("index")) == (1, 84) and "tmdb://7130181" in [g.get("id") for g in row.get("Guid") or []],
              (row.get("parentIndex"), row.get("index"), row.get("Guid")))

        row = server.row(keys.get(showdown))
        check("a push naming TMDB's episode 1107002 lands on Plex's S3E1 'Showdown (1)', not its numbers' S2E1",
              (row.get("parentIndex"), row.get("index"), row.get("title")) == (3, 1, "Showdown (1)"),
              (row.get("parentIndex"), row.get("index"), row.get("title")))
        row = server.row(keys.get(tummy))
        check("a push naming no TMDB episode is Plex's at its numbers",
              (row.get("parentIndex"), row.get("index")) == (2, 3) and str(row.get("guid", "")).startswith("plex://episode/"),
              (row.get("parentIndex"), row.get("index"), row.get("guid")))

        moved = keys.get(frys["_sid"])
        row = server.row(moved) if moved else {}
        check("the Frys S10E24, placed as S9E15, leaves the row of four releases for S9E15",
              moved != kenny["ratingKey"] and (row.get("parentIndex"), row.get("index")) == (9, 15),
              (moved, row.get("parentIndex"), row.get("index")))
        left = server.row(kenny["ratingKey"])
        sids = [media.get("_sid") for media in left.get("Media") or []]
        check("the other three releases stay on S10E24", (left.get("parentIndex"), left.get("index")) == (10, 24)
              and len(sids) == 3 and frys["_sid"] not in sids, (left.get("parentIndex"), left.get("index"), len(sids)))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
