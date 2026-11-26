#!/usr/bin/env python3
"""A title's ratings follow Plex's agent as plexlab's do: on Refresh Metadata, through a Fix Match and a start.

plexlab took the agent's new scores on its own passes over the library, the item request a refresh
sends (`GET /library/metadata/<id>?...&updating=1`): on 2026-11-12 Big Buck Bunny's Rotten Tomatoes
audience score went from 5.4 to 5.3, Firefly's TMDB score from 8.348 (shown 8.3) to 8.351 (8.4), and
Tears of Steel gained a TMDB entry, and plexlab shows them (`fixtures/ratings_s1434.json`, recorded
by `probes/record_ratings.py`: plexlab's answers now and the agent's answers to those passes; the
older shows are `fixtures/agent_description_s1434.json`). rex kept the scores the agent gave the
day a title was matched. The agent gives Tears of Steel `rating` 6 with no `ratingImage`, and
plexlab shows no `rating`. Here, Plex's agent answering from those recordings:

  (a) Tears of Steel, Fix Matched from the agent's own search answer: no `rating`, as plexlab;
  (b) Refresh Metadata of the film, of Tears of Steel and of the show takes the agent's scores of
      that pass: each detail and grid row is plexlab's now, and the episode keeps its own; an
      episode's refresh takes its own from the agent's answer to rex's bare GET (read 2026-11-26);
  (c) a Fix Match again keeps the newer scores (the search answer is kept for good), and so does a start;
  (d) a score a person locked stays theirs through a refresh, (e) and through the start that gives
      rows an older rex described their ratings.

    python3 tests/test_refresh_ratings.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse

import test_agent_description as described
from test_agent_description import ITEMS, MOUNT, REFERENCE, WEB, catalog, ratings
from test_feed import Bytes, Server, check, results, serve

FIXTURE = json.load(open(os.path.join(described.FIXTURES, "ratings_s1434.json")))
PLEXLAB = FIXTURE["plexlab"]
GRID = described.GRID
FILM, TEARS, SHOW = "5d7768352e80df001ebde4c9", "5d77699b96b655001fdd3b87", "5d9c086c7d06d9001ffd27aa"
TEARS_PATH = "movies/Tears.of.Steel.2012.1080p/Tears.of.Steel.2012.1080p.mkv"


class Agent(http.server.ThreadingHTTPServer):
    """Plex's agent (and plex.tv) behind `REX_PLEXTV_URL`: an item as the agent answered it on
    `pass_day`'s pass, nothing before one is set, and its title search for Tears of Steel."""
    daemon_threads = True
    pass_day = None
    asked = []


class Handler(http.server.BaseHTTPRequestHandler):
    def answer(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:  # plex.tv's form posts
            body = {}
        path = urllib.parse.urlsplit(self.path).path
        found = {"MediaContainer": {"size": 0}}
        if path.startswith("/metadata.provider.plex.tv/library/metadata/"):
            item = path.rsplit("/", 1)[1]
            day = self.server.pass_day
            self.server.asked.append((self.command, item))
            if self.command == "GET" and day and day in FIXTURE["agent"].get(item, {}):
                found = FIXTURE["agent"][item][day]
            elif self.command == "POST" and item == "matches" and body.get("title") == "Tears of Steel":
                found = FIXTURE["search"]["response"]
        out = json.dumps(found).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_DELETE = answer

    def log_message(self, *args):
        pass


def plexlab(path, key):
    return next(row for row in PLEXLAB[path]["MediaContainer"]["Metadata"] if row["ratingKey"] == key)


def fix_match(server, key):
    """Plex Web's Fix Match: its search, then the candidate it picks."""
    status, body = server.request("/library/metadata/%s/matches?manual=1&title=Tears%%20of%%20Steel&year=2012" % key)
    found = ((body or {}).get("MediaContainer") or {}).get("SearchResult") or [] if status == 200 else []
    candidate = next((row for row in found if row.get("guid") == "plex://movie/" + TEARS), {})
    status, _ = server.request("/library/metadata/%s/match?guid=%s&name=%s&year=%s"
                               % (key, urllib.parse.quote(candidate.get("guid", ""), safe=""),
                                  urllib.parse.quote(candidate.get("name", "")), candidate.get("year", "")), "PUT")
    return status == 200 and bool(candidate)


def detail(server, key):
    return (server.rows("/library/metadata/%s?%s" % (key, WEB)) or [{}])[0]


def wait(test, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if test():
            return True
        time.sleep(0.25)
    return test()


def main():
    agent = Agent(("127.0.0.1", 0), Handler)
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    keys = {"REX_PLEXTV_URL": "http://127.0.0.1:%d" % agent.server_address[1]}
    bytes_port = serve(Bytes().handler())
    work = tempfile.mkdtemp(prefix="rex-ratings-")
    catalog(work)
    tears = dict(ITEMS[0], id=TEARS_PATH, path=TEARS_PATH, file="%s/%s" % (MOUNT, TEARS_PATH), name="Tears.of.Steel.2012.1080p",
                 release="movies/Tears.of.Steel.2012.1080p", hash="7e4c1f0a" * 5)
    for field in ("ids", "kind"):
        tears.pop(field)
    server = Server(work, bytes_port, keys=keys)
    try:
        status, body = server.feed("POST", "items", {"items": ITEMS + [tears]})
        pushed = server.keys()
        film, tear, episode = pushed.get(ITEMS[0]["id"]), pushed.get(TEARS_PATH), pushed.get(ITEMS[2]["id"])
        show = server.row(episode).get("grandparentRatingKey")
        check("the film, the episode and an unmatched Tears of Steel are pushed", status == 200 and film and tear and show, body)

        # -- (a) --------------------------------------------------------------------------
        want = ratings(plexlab("/library/metadata/4?" + WEB, "4"))
        got = ratings(detail(server, tear)) if fix_match(server, tear) else {}
        check("(a) Tears of Steel Fix Matched: the agent's rating 6 with no image is no rating, as plexlab shows none",
              {k: v for k, v in got.items() if k != "Rating"} == {k: v for k, v in want.items() if k != "Rating"}, (got, want))

        # -- (b) --------------------------------------------------------------------------
        agent.pass_day = "2026-11-12"
        for key in (film, tear, show):
            server.request("/library/metadata/%s/refresh" % key, "PUT")
        for name, key, path, ref in (("Big Buck Bunny", film, "/library/metadata/1?" + WEB, "1"),
                                     ("Tears of Steel", tear, "/library/metadata/4?" + WEB, "4"),
                                     ("Firefly", show, "/library/metadata/6?" + WEB, "6")):
            want = ratings(plexlab(path, ref))
            wait(lambda: ratings(detail(server, key)) == want)
            check("(b) a refresh gives %s plexlab's ratings now" % name, ratings(detail(server, key)) == want,
                  (ratings(detail(server, key)), want))
        for section, pairs in (("1", ((film, "1"), (tear, "4"))), ("2", ((show, "6"),))):
            rows = {row.get("ratingKey"): row for row in server.rows("/library/sections/%s/all?%s" % (section, GRID))}
            for key, ref in pairs:
                want = ratings(plexlab("/library/sections/%s/all?%s" % (section, GRID), ref))
                check("(b) and its grid row plexlab's: %s" % want.get("audienceRating"), ratings(rows.get(key, {})) == want,
                      (ratings(rows.get(key, {})), want))
        want = ratings(described.ref_row("/library/metadata/8?" + WEB))
        check("(b) the episode keeps its own score, plexlab's (unchanged there since)", ratings(detail(server, episode)) == want,
              (ratings(detail(server, episode)), want))
        # No pass of plexlab's asked an episode alone: the agent's answer to rex's own bare GET, read 2026-11-26.
        agent.pass_day = "2026-11-26 bare"
        asked = len(agent.asked)
        server.request("/library/metadata/%s/refresh" % episode, "PUT")
        wait(lambda: len(agent.asked) > asked)
        time.sleep(1)
        check("(b) an episode's refresh takes its own scores from the agent's answer for it, plexlab's",
              ratings(detail(server, episode)) == want, (ratings(detail(server, episode)), want))
        agent.pass_day = "2026-11-12"

        # -- (c) --------------------------------------------------------------------------
        want = ratings(plexlab("/library/metadata/4?" + WEB, "4"))
        check("(c) a Fix Match again from the kept search answer keeps the newer ratings",
              fix_match(server, tear) and ratings(detail(server, tear)) == want, (ratings(detail(server, tear)), want))
    finally:
        server.stop()
    server = Server(work, bytes_port, keys=keys)
    try:
        for name, key, path, ref in (("Big Buck Bunny", film, "/library/metadata/1?" + WEB, "1"),
                                     ("Firefly", show, "/library/metadata/6?" + WEB, "6")):
            want = ratings(plexlab(path, ref))
            check("(c) after a start %s keeps them" % name, ratings(detail(server, key)) == want, (ratings(detail(server, key)), want))

        # -- (d) --------------------------------------------------------------------------
        status, _ = server.request("/library/sections/1/all?type=1&id=%s&audienceRating.value=9.9&audienceRating.locked=1" % film, "PUT")
        agent.pass_day = "2026-10-13"
        server.request("/library/metadata/%s/refresh" % film, "PUT")
        older = ratings(described.ref_row("/library/metadata/1?" + WEB))
        wait(lambda: ratings(detail(server, film))["Rating"] == older["Rating"])
        got = ratings(detail(server, film))
        check("(d) a refresh keeps the audience score a person locked, and takes the rest",
              status == 200 and got["audienceRating"] == 9.9 and got["Rating"] == older["Rating"], (status, got, older))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    # -- (e) a start on rows an older rex described ------------------------------------------
    work = tempfile.mkdtemp(prefix="rex-ratings-held-")
    rows = json.loads(json.dumps(described.HELD["rows"]))
    film = next(row for row in rows if row["title"] == "Big Buck Bunny")
    film.update(audienceRating=9.9, Field=[{"locked": True, "name": "audienceRating"}])
    catalog(work, rows)
    server = Server(work, bytes_port, keys=keys)
    try:
        wait(lambda: ratings(detail(server, film["ratingKey"]))["Rating"])
        got = ratings(detail(server, film["ratingKey"]))
        check("(e) a start giving older rows their ratings keeps the audience score a person locked",
              got["audienceRating"] == 9.9 and got["Rating"], got)
    finally:
        server.stop()
        agent.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
