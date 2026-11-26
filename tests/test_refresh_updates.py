#!/usr/bin/env python3
"""What a Refresh Metadata asks Plex's agent and what it moves, as plexlab's refreshes did.

`fixtures/refresh_s1434.json` is plexlab (PMS 1.43.4, Plex's agent reachable) refreshed three times
on 2026-11-26 (`probes/record_refresh.py`): Big Buck Bunny, Firefly's Season 1, then Firefly. Each
asked the agent's item once with a query of its own (a film's and a show's beside their `/extras`;
a show's seasons then each asked with their episodes inside, no episode on its own), and saved
(moved `updatedAt` to the PUT's second, and every artwork path of the row with it) only a title
whose answer was not the last one it was given: Big Buck Bunny's was, and nothing moved; Season 1
and its episodes, last asked in September, moved; asked again 52 s later inside the show's
refresh, they did not, and the show did. Its last frame came after the agent's answer. rex asked
the bare item, a show's seasons alike, took a season's episodes from nothing, and never moved
`updatedAt`. Here, Plex's agent answering with plexlab's recorded answers:

  (a) a film's refresh asks one request, plexlab's query; its first moves `updatedAt` and its
      artwork paths to the PUT's second, its frames, plexlab's seven, once the answer is in;
  (b) the same answer again moves nothing (plexlab's Big Buck Bunny);
  (c) a season's refresh asks the season alone, plexlab's query, and takes its episodes' stills and
      scores from that answer, plexlab's for The Train Job; both move, the show does not;
  (d) a show's refresh asks the show, then its season, nothing else; the show moves, its season
      (the same answer as just before) does not, and both show the show's new paths where they
      borrow them; the library's own stamps and Recently Added stay as they were;
  (e) an agent that gives no answer moves nothing, asks once, and the frames still come;
  (f) a poster a person uploaded is served at the new path and at the old one, as plexlab serves
      its current picture at an old version;
  (g) an episode's refresh asks its bare item, as before (not measured on plexlab).

    python3 tests/test_refresh_updates.py
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
from test_agent_description import ITEMS, catalog
from test_feed import Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "refresh_s1434.json")))
RATINGS = json.load(open(os.path.join(HERE, "fixtures", "ratings_s1434.json")))
FILM_R, SEASON_R, SHOW_R = FIXTURE["refreshes"]
FILM, SHOW, SEASON = "5d7768352e80df001ebde4c9", "5d9c086c7d06d9001ffd27aa", "602e67e766dfdb002c0a1b5b"
# The season's guid in the older answers this library was described from (`feed/specials.json`,
# 2026-10-16); the agent names Firefly's Season 1 602e67e7... now, and answers for either here.
OLD_SEASON = "5d9c09de08fddd001f2afb4c"
TRAIN_JOB = "plex://episode/5d9c127e4eefaa001f6449c2"


def recorded(refresh, item):
    """plexlab's request for `item` in a refresh and the agent's answer to it."""
    return next(entry for entry in refresh["outbound"] if entry["path"] == "/library/metadata/" + item)


ANSWERS = {FILM: recorded(FILM_R, FILM)["response"], SHOW: recorded(SHOW_R, SHOW)["response"],
           SEASON: recorded(SHOW_R, SEASON)["response"]}


class Agent(http.server.ThreadingHTTPServer):
    daemon_threads = True
    asked = []
    down = False
    delay = 0.0
    answers = ANSWERS


class Handler(http.server.BaseHTTPRequestHandler):
    def answer(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        url = urllib.parse.urlsplit(self.path)
        found = {"MediaContainer": {"size": 0}}
        status = 200
        if url.path.startswith("/metadata.provider.plex.tv/library/metadata/"):
            item = url.path.rsplit("/", 1)[1]
            self.server.asked.append((self.command, url.path.split("/metadata.provider.plex.tv", 1)[1],
                                      [list(pair) for pair in urllib.parse.parse_qsl(url.query, keep_blank_values=True)]))
            time.sleep(self.server.delay)
            if self.server.down:
                status = 503
            elif self.command == "GET" and item in (SEASON, OLD_SEASON):
                found = json.loads(json.dumps(self.server.answers[SEASON]).replace(SEASON, item))
            elif self.command == "GET" and item in self.server.answers:
                found = self.server.answers[item]
        out = json.dumps(found).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_DELETE = answer

    def log_message(self, *args):
        pass


def wait(test, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if test():
            return True
        time.sleep(0.2)
    return test()


def row(server, key):
    return (server.rows("/library/metadata/%s" % key) or [{}])[0]


def paths(item):
    return {field: item.get(field) for field in ("thumb", "art", "theme", "parentThumb", "parentTheme", "grandparentThumb",
                                                "grandparentArt", "grandparentTheme") if item.get(field)}


def version(path):
    return int(str(path).rsplit("/", 1)[-1]) if path else None


def frames(server, mark):
    """The refresh's frames as plexlab's are compared: each timeline entry's type, state and
    metadataState, each activity's event."""
    out = []
    for name, frame in server.events.frames[mark:]:
        if name == "timeline":
            entries = frame.get("TimelineEntry")
            for entry in entries if isinstance(entries, list) else [entries]:
                out.append(("timeline", entry.get("type"), entry.get("state"), entry.get("metadataState")))
        elif name == "activity":
            notes = frame.get("ActivityNotification")
            for note in notes if isinstance(notes, list) else [notes]:
                out.append(("activity", (note or {}).get("event")))
    return out


def plexlab_frames(refresh):
    out = []
    for frame in refresh["frames"]:
        for entry in frame.get("TimelineEntry") or []:
            out.append(("timeline", entry.get("type"), entry.get("state"), entry.get("metadataState")))
        for entry in frame.get("ActivityNotification") or []:
            out.append(("activity", entry.get("event")))
    return out


def refresh(server, agent, key):
    """A refresh and what it asked; its second, as the row's stamp is compared."""
    asked, at = len(agent.asked), int(time.time())
    status, _ = server.request("/library/metadata/%s/refresh" % key, "PUT")
    return status, asked, at


def upload(server, key, body):
    return server.request("/library/metadata/%s/posters" % key, "POST", body, {"Content-Type": "image/jpeg"})[0]


def picture(server, path):
    status, body = server.request(path)
    return status, body if isinstance(body, bytes) else None


def main():
    agent = Agent(("127.0.0.1", 0), Handler)
    threading.Thread(target=agent.serve_forever, daemon=True).start()
    keys = {"REX_PLEXTV_URL": "http://127.0.0.1:%d" % agent.server_address[1]}
    work = tempfile.mkdtemp(prefix="rex-refresh-updates-")
    catalog(work)
    server = Server(work, serve(Bytes().handler()), keys=keys)
    try:
        status, body = server.feed("POST", "items", {"items": ITEMS})
        pushed = server.keys()
        film, episode = pushed.get(ITEMS[0]["id"]), pushed.get(ITEMS[2]["id"])
        show, season = row(server, episode).get("grandparentRatingKey"), row(server, episode).get("parentRatingKey")
        check("the film and The Train Job are pushed and named", status == 200 and film and episode and show and season, body)
        section = lambda: [(d.get("key"), d.get("updatedAt"), d.get("contentChangedAt")) for d in
                           (server.request("/library/sections")[1].get("MediaContainer") or {}).get("Directory", [])]
        recent = lambda: [r.get("ratingKey") for r in server.rows("/library/recentlyAdded")]
        sections_before, recent_before = section(), recent()

        # -- (a) a film's refresh ---------------------------------------------------------------
        time.sleep(1.1)  # a second past the push's stamp
        before, mark = row(server, film), server.events.mark()
        agent.delay = 1.5
        status, asked, at = refresh(server, agent, film)
        time.sleep(0.8)
        early = frames(server, mark)
        wait(lambda: len(frames(server, mark)) >= 7, 10)
        agent.delay = 0
        want = recorded(FILM_R, FILM)
        sent = agent.asked[asked:]
        check("(a) a film's refresh asks the agent's item once, with plexlab's query (its /extras rex takes nothing from)",
              status == 200 and sent == [("GET", want["path"], want["query"])], (sent, want["path"], want["query"]))
        after = row(server, film)
        check("(a) its first moves updatedAt to the PUT's second, as plexlab saved Big Buck Bunny on 2026-11-25",
              at <= int(after.get("updatedAt") or 0) <= at + 1 and after.get("updatedAt") != before.get("updatedAt"),
              (before.get("updatedAt"), after.get("updatedAt"), at))
        check("(a) and every artwork path it serves to that version",
              all(version(path) >= after["updatedAt"] for path in paths(after).values()) and paths(after) != paths(before),
              (paths(before), paths(after)))
        check("(a) its frames are plexlab's seven", frames(server, mark) == plexlab_frames(FILM_R), (frames(server, mark), plexlab_frames(FILM_R)))
        check("(a) and come once the agent has answered, so a client reading the row on the last reads the new one",
              early == [], early)

        # -- (b) the same answer again ----------------------------------------------------------
        before, mark = row(server, film), server.events.mark()
        status, asked, at = refresh(server, agent, film)
        wait(lambda: len(frames(server, mark)) >= 7, 10)
        after = row(server, film)
        check("(b) the same answer again moves nothing, as Big Buck Bunny's on 2026-11-26 23:19",
              len(agent.asked) == asked + 1 and after.get("updatedAt") == before.get("updatedAt") and paths(after) == paths(before),
              (before.get("updatedAt"), after.get("updatedAt"), paths(before), paths(after)))
        check("(b) and still sends its frames", frames(server, mark) == plexlab_frames(FILM_R), frames(server, mark))

        # -- (c) a season's refresh -------------------------------------------------------------
        rows_before = {key: row(server, key) for key in (show, season, episode)}
        mark = server.events.mark()
        status, asked, at = refresh(server, agent, season)
        wait(lambda: len(frames(server, mark)) >= 6, 10)
        time.sleep(0.5)
        want = recorded(SEASON_R, SEASON)
        sent = agent.asked[asked:]
        check("(c) a season's refresh asks the season alone, with plexlab's query, no episode",
              [(method, path.replace(OLD_SEASON, SEASON), query) for method, path, query in sent] == [("GET", want["path"], want["query"])],
              (sent, want["query"]))
        rows_after = {key: row(server, key) for key in (show, season, episode)}
        answer = next(e for e in want["response"]["MediaContainer"]["Metadata"][0]["Children"]["Metadata"] if e["guid"] == TRAIN_JOB)
        status, body = server.request("/library/metadata/%s/posters" % episode)
        still = next((r.get("key") for r in (body.get("MediaContainer") or {}).get("Metadata", []) if r.get("provider") == "tmdb"), None)
        check("(c) and takes The Train Job's still from that answer", still == answer["thumb"], (still, answer["thumb"]))
        plexlab_after = SEASON_R["rows"]["8"]["after"]
        check("(c) and its scores, plexlab's after the same refresh",
              described.ratings(rows_after[episode])["Rating"] == plexlab_after["Rating"]
              and rows_after[episode].get("audienceRating") == plexlab_after["audienceRating"],
              (described.ratings(rows_after[episode]), plexlab_after["Rating"], plexlab_after["audienceRating"]))
        moved = {key: rows_after[key].get("updatedAt") != rows_before[key].get("updatedAt") for key in rows_before}
        check("(c) the season and its episode move to the PUT's second, the show does not, as plexlab's 7 and 8 against 6",
              moved == {show: False, season: True, episode: True}
              and all(at <= int(rows_after[key]["updatedAt"]) <= at + 1 for key in (season, episode)), (moved, at, rows_after[season].get("updatedAt")))
        check("(c) its frames are plexlab's shape, one episode here",
              [f for f in frames(server, mark) if f[0] == "activity" or f[1] == 3] == [f for f in plexlab_frames(SEASON_R) if f[0] == "activity" or f[1] == 3],
              frames(server, mark))

        # -- (d) a show's refresh ---------------------------------------------------------------
        rows_before = {key: row(server, key) for key in (show, season, episode)}
        status, asked, at = refresh(server, agent, show)
        wait(lambda: len(agent.asked) >= asked + 2, 10)
        time.sleep(1.5)
        sent = [(method, path.replace(OLD_SEASON, SEASON), query) for method, path, query in agent.asked[asked:]]
        want = [("GET", e["path"], e["query"]) for e in SHOW_R["outbound"] if not e["path"].endswith("/extras")]
        check("(d) a show's refresh asks the show, then its season, plexlab's queries, nothing else", sent == want, (sent, want))
        rows_after = {key: row(server, key) for key in (show, season, episode)}
        moved = {key: rows_after[key].get("updatedAt") != rows_before[key].get("updatedAt") for key in rows_before}
        check("(d) the show moves, its season and episode (the answer just given) do not, as plexlab's 6, 7 and 8",
              moved == {show: True, season: False, episode: False} and at <= int(rows_after[show]["updatedAt"]) <= at + 1, moved)
        new = rows_after[show]
        check("(d) the show's poster, background and theme take its new version",
              all(version(new.get(field)) >= new["updatedAt"] for field in ("thumb", "art", "theme")), paths(new))
        check("(d) and its season and episode show them where they borrow them",
              rows_after[season].get("parentThumb") == new["thumb"] and rows_after[season].get("art") == new["art"]
              and rows_after[episode].get("grandparentThumb") == new["thumb"] and rows_after[episode].get("grandparentArt") == new["art"]
              and rows_after[episode].get("thumb") == rows_before[episode].get("thumb"),
              (paths(rows_after[season]), paths(rows_after[episode])))
        check("(d) the libraries' own stamps and Recently Added stay as they were, as plexlab's did",
              section() == sections_before and recent() == recent_before, (section(), sections_before, recent(), recent_before))

        # -- (e) no answer ----------------------------------------------------------------------
        agent.down = True
        before, mark = row(server, film), server.events.mark()
        status, asked, at = refresh(server, agent, film)
        wait(lambda: len(frames(server, mark)) >= 7, 10)
        time.sleep(1)
        after = row(server, film)
        check("(e) an agent that gives no answer moves nothing, as plexscratch offline, and is asked once",
              len(agent.asked) == asked + 1 and after.get("updatedAt") == before.get("updatedAt") and paths(after) == paths(before),
              (len(agent.asked) - asked, before.get("updatedAt"), after.get("updatedAt")))
        check("(e) and the frames still come", frames(server, mark) == plexlab_frames(FILM_R), frames(server, mark))
        agent.down = False

        # -- (f) a person's upload --------------------------------------------------------------
        mine = b"\xff\xd8\xff\xe0" + b"rex-refresh-updates" * 8
        status = upload(server, film, mine)
        old = row(server, film).get("thumb")
        time.sleep(1.1)
        # Another answer than the last: Big Buck Bunny's of plexlab's 2026-11-12 pass.
        agent.answers = dict(ANSWERS, **{FILM: RATINGS["agent"][FILM]["2026-11-12"]})
        refresh(server, agent, film)
        wait(lambda: row(server, film).get("thumb") != old, 10)
        new = row(server, film).get("thumb")
        check("(f) an uploaded poster is kept through a refresh that moves its path",
              status == 200 and new != old and picture(server, new) == (200, mine), (status, old, new, picture(server, new)[0]))
        check("(f) and the old path serves it too, as plexlab serves its current picture there", picture(server, old) == (200, mine),
              picture(server, old)[0])
        agent.answers = ANSWERS

        # -- (g) an episode -------------------------------------------------------------------
        status, asked, at = refresh(server, agent, episode)
        wait(lambda: len(agent.asked) > asked, 10)
        time.sleep(1)
        sent = agent.asked[asked:]
        check("(g) an episode's refresh asks its bare item, as before", [(m, p) for m, p, _ in sent] == [("GET", "/library/metadata/" + TRAIN_JOB.rsplit("/", 1)[1])],
              sent)
    finally:
        server.stop()
        agent.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
