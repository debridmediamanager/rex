#!/usr/bin/env python3
"""A title's artwork Plex's agent replaced is asked for again: on Refresh Metadata, and when its fetch fails.

rex keeps the urls Plex's agent gave when it described a title, and the agent's answers in
`plex_cache.db` for good. Plex replaces a title's gracenote artwork and the old urls 403: Funny
Money's poster and background on 2026-11-17 drew the blank tile (`404 GET /photo/:/transcode --
rex: artwork unavailable`) while the agent named new ones, and Refresh Metadata changed nothing.
Here, against test_plextv's stub with Big Buck Bunny placed as zurg pushes it:

  (a) Refresh Metadata takes the poster and background the agent names now, past the cache;
  (b) a poster whose fetch fails asks the agent again by itself, once per start for the urls a row holds,
      and an agent that gave no answer only after a while (RETRY seconds here, ten minutes in a real rex);
  (c) a poster a person picked stays theirs, the background they did not pick still follows, and a
      background they uploaded stays theirs too (an upload sets no lock);
  (d) with The Mentalist's first episode: a show's refresh takes its poster, background and theme
      and its seasons' posters, never a background a season only borrows; an episode's its still;
  (e) a film that came with no poster, the agent naming none, takes the one it names later.

Not measured: a real PMS downloads the images when it matches, so it never meets a dead url, and
what it asks the agent on a refresh was not recorded (plexscratch has no route to plex.tv).

    python3 tests/test_refresh_artwork.py
"""
import http.client
import json
import shutil
import sys
import threading
import time
import urllib.parse

import test_feed_named_on_claim as named_on_claim
import test_plextv as plextv
from test_feed_named_on_claim import EPISODE, MOVIE, call, fresh, push, rows
from test_plextv import AGENT, SERVER_TOKEN, Instance, Stub, check, wait_for
from test_ports import free_port

ID = MOVIE[3].rsplit("/", 1)[1]
# How long a failed fetch waits to ask an agent that gave no answer again (REX_AGENT_ARTWORK_RETRY_S).
RETRY = 3
# Urls rex cannot fetch (a private address), as a 403'd gracenote url answers it.
DEAD = "http://127.0.0.1:9/%s.jpg"

_answer = plextv.StubHandler.answer


def answer(self, record):
    # The title as the agent describes it now (`GET /library/metadata/{id}`).
    if (record["method"], record["host"], record["path"]) == ("GET", AGENT, "/library/metadata/" + ID):
        if getattr(self.server, "item_down", False):
            self.server.item_refused = getattr(self.server, "item_refused", 0) + 1
            return 503, "text/plain", "stub: the provider is down"
        thumb, art = self.server.artwork
        title = {"guid": MOVIE[3], "type": "movie", "title": "Big Buck Bunny", "thumb": thumb, "art": art}
        if getattr(self.server, "theme", None):
            title["theme"] = self.server.theme
        return 200, "application/json", json.dumps({"MediaContainer": {"size": 1, "Metadata": [title]}})
    item = record["path"].rsplit("/", 1)[1]
    if (record["method"], record["host"]) == ("GET", AGENT) and item in getattr(self.server, "items", {}):
        return 200, "application/json", json.dumps({"MediaContainer": {"size": 1, "Metadata": [self.server.items[item]]}})
    status, kind, body = _answer(self, record)
    if getattr(self.server, "posterless", False) and (record["host"], record["path"]) == (AGENT, "/library/metadata/matches"):
        # A title the agent has no poster or background for yet (Baron Munchausen's, 2026-11-19).
        answer = json.loads(body)
        for row in (answer.get("MediaContainer") or {}).get("Metadata") or []:
            row.pop("thumb", None)
            row.pop("art", None)
        body = json.dumps(answer)
    return status, kind, body


plextv.StubHandler.answer = answer


def stored(instance, key, kind):
    """The agent url `/posters` or `/arts` lists for the row."""
    status, body = call(instance, "GET", "/library/metadata/%s/%s" % (key, kind), token=SERVER_TOKEN)
    listed = ((body or {}).get("MediaContainer") or {}).get("Metadata") or [] if status == 200 else []
    return next((row.get("key") for row in listed if row.get("provider") == "tmdb"), None)


def detail(instance, key):
    status, body = call(instance, "GET", "/library/metadata/%s" % key, token=SERVER_TOKEN)
    return ((body or {}).get("MediaContainer") or {}).get("Metadata", [{}])[0] if status == 200 else {}


def upload(instance, key, kind):
    """A person's upload (`POST .../arts`), as Plex Web sends a picture's bytes."""
    conn = http.client.HTTPConnection("127.0.0.1", instance.port, timeout=30)
    try:
        conn.request("POST", "/library/metadata/%s/%s" % (key, kind), body=b"\xff\xd8\xff\xe0" + b"x" * 64,
                     headers={"X-Plex-Token": SERVER_TOKEN, "Content-Type": "image/jpeg"})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def selected(instance, key, kind):
    status, body = call(instance, "GET", "/library/metadata/%s/%s" % (key, kind), token=SERVER_TOKEN)
    listed = ((body or {}).get("MediaContainer") or {}).get("Metadata") or [] if status == 200 else []
    return next((row.get("ratingKey") for row in listed if row.get("selected")), None)


def asked_for(stub, item):
    return len(stub.taken(AGENT, path="/library/metadata/" + item, method="GET"))


def asked(stub):
    return len(stub.taken(AGENT, path="/library/metadata/" + ID, method="GET"))


def main():
    stub = Stub(free_port())
    stub.artwork = ("", "")
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    directory = fresh(True)
    instance = Instance(directory, free_port(), stub.server_address[1], plex_token=None, announce=None,
                        env_extra={"REX_AGENT_ARTWORK_RETRY_S": str(RETRY)})
    try:
        check("Big Buck Bunny is pushed", push(instance) == 200)
        row = (wait_for(lambda: named_on_claim.named(instance), 20) or rows(instance))[MOVIE[0]]
        key = row.get("ratingKey")
        first = stored(instance, key, "posters")
        check("and named by Plex's agent, with the poster its match gave", row.get("guid") == MOVIE[3] and first, (row.get("guid"), first))

        # -- (a) Refresh Metadata ------------------------------------------------------------
        stub.artwork = (DEAD % "poster-b", DEAD % "art-b")
        status = call(instance, "PUT", "/library/metadata/%s/refresh" % key, token=SERVER_TOKEN)[0]
        poster = wait_for(lambda: stored(instance, key, "posters") == DEAD % "poster-b" and DEAD % "poster-b", 10)
        check("(a) Refresh Metadata takes the poster the agent names now", status == 200 and poster,
              (status, stored(instance, key, "posters")))
        check("(a) and the background", stored(instance, key, "arts") == DEAD % "art-b", stored(instance, key, "arts"))
        status, body = call(instance, "GET", "/library/metadata/%s" % key, token=SERVER_TOKEN)
        thumb = (body.get("MediaContainer") or {}).get("Metadata", [{}])[0].get("thumb") if status == 200 else None
        check("(a) at a new path, so clients drop the tile they cached", thumb and thumb != row.get("thumb"), (row.get("thumb"), thumb))

        # -- (b) a fetch that fails -----------------------------------------------------------
        stub.artwork = (DEAD % "poster-c", DEAD % "art-c")
        photo = "/photo/:/transcode?width=240&height=360&url=%s" % urllib.parse.quote("/library/metadata/%s/thumb/1" % key, safe="")
        stub.item_down, before, refused = True, asked(stub), getattr(stub, "item_refused", 0)
        call(instance, "GET", photo, token=SERVER_TOKEN)
        # Counted once the stub has answered 503: the request alone is recorded before that.
        down = wait_for(lambda: getattr(stub, "item_refused", 0) > refused, 10)
        check("(b) with the agent down a failed fetch asks it and changes nothing",
              down and stored(instance, key, "posters") == DEAD % "poster-b", (asked(stub) - before, stored(instance, key, "posters")))
        # Every view of a dead tile asked again while the agent answered 503 (2026-11-26): an outage
        # fails every fetch. The suite's rex waits RETRY seconds, a real one ten minutes.
        time.sleep(0.5)
        before = asked(stub)
        call(instance, "GET", photo, token=SERVER_TOKEN)
        check("(b) and the next failed fetch, the agent still down, does not ask again at once",
              not wait_for(lambda: asked(stub) > before, 2), asked(stub) - before)
        stub.item_down, before = False, asked(stub)
        status = call(instance, "GET", photo, token=SERVER_TOKEN)[0]
        # The failed ask can still hold its mark when the stub has answered, so a view finds the
        # row asked about and asks nothing: view again until one asks, never after.
        for _ in range(10):
            if wait_for(lambda: asked(stub) > before, 1):
                break
            call(instance, "GET", photo, token=SERVER_TOKEN)
        poster = wait_for(lambda: stored(instance, key, "posters") == DEAD % "poster-c" and DEAD % "poster-c", 10)
        check("(b) the next failed fetch, the agent back, is the 404 it was and asks again for the one it names now",
              status == 404 and poster and asked(stub) == before + 1, (status, stored(instance, key, "posters"), asked(stub) - before))
        # The agent still names the dead url: asked once for it, then not on every view.
        before = asked(stub)
        call(instance, "GET", photo, token=SERVER_TOKEN)
        wait_for(lambda: asked(stub) > before, 10)
        call(instance, "GET", photo, token=SERVER_TOKEN)
        call(instance, "GET", "/library/metadata/%s/thumb" % key, token=SERVER_TOKEN)
        check("(b) once for the urls a row holds: a url the agent still names is not asked for on every view",
              not wait_for(lambda: asked(stub) > before + 1, 3) and asked(stub) == before + 1
              and stored(instance, key, "posters") == DEAD % "poster-c", (asked(stub) - before, stored(instance, key, "posters")))
        stub.artwork = (DEAD % "poster-d", DEAD % "art-d")
        # A push that identifies the row again puts back the urls its kept match names: new urls, asked about again.
        call(instance, "PUT", "/library/metadata/%s/refresh" % key, token=SERVER_TOKEN)
        wait_for(lambda: stored(instance, key, "posters") == DEAD % "poster-d", 10)
        stub.artwork = (DEAD % "poster-f", DEAD % "art-f")
        call(instance, "GET", photo, token=SERVER_TOKEN)
        poster = wait_for(lambda: stored(instance, key, "posters") == DEAD % "poster-f" and DEAD % "poster-f", 10)
        check("(b) other urls, as a push identifying the row again puts back, ask again when their fetch fails", poster, stored(instance, key, "posters"))

        # -- (c) a person's pick ------------------------------------------------------------
        picked = DEAD % "picked"
        status = call(instance, "PUT", "/library/metadata/%s/poster?url=%s" % (key, urllib.parse.quote(picked, safe="")), token=SERVER_TOKEN)[0]
        stub.artwork = (DEAD % "poster-e", DEAD % "art-e")
        call(instance, "PUT", "/library/metadata/%s/refresh" % key, token=SERVER_TOKEN)
        art = wait_for(lambda: stored(instance, key, "arts") == DEAD % "art-e" and DEAD % "art-e", 10)
        check("(c) a refresh leaves the poster a person picked", status == 200 and stored(instance, key, "posters") == picked,
              (status, stored(instance, key, "posters")))
        check("(c) and takes the background they did not", art, stored(instance, key, "arts"))
        status = upload(instance, key, "arts")
        mine = selected(instance, key, "arts")
        stub.artwork = (DEAD % "poster-g", DEAD % "art-g")
        call(instance, "PUT", "/library/metadata/%s/refresh" % key, token=SERVER_TOKEN)
        wait_for(lambda: asked(stub) and stored(instance, key, "arts") == DEAD % "art-g", 4)
        check("(c) a refresh leaves a background a person uploaded",
              status == 200 and str(mine).startswith("upload://") and selected(instance, key, "arts") == mine,
              (status, mine, selected(instance, key, "arts")))

        # -- (d) a show, its season and episode ---------------------------------------------
        episode = (wait_for(lambda: rows(instance)[EPISODE[0]].get("grandparentRatingKey") and rows(instance), 20) or rows(instance))[EPISODE[0]]
        show, season, leaf = episode.get("grandparentRatingKey"), episode.get("parentRatingKey"), episode.get("ratingKey")
        before = {name: stored(instance, row, kind) for name, row, kind in
                  (("show", show, "posters"), ("show art", show, "arts"), ("season", season, "posters"),
                   ("season art", season, "arts"), ("episode", leaf, "posters"))}
        theme = detail(instance, show).get("theme")
        stub.items = {
            "5d9c08353c3f87001f34a531": {"guid": "plex://show/5d9c08353c3f87001f34a531", "type": "show", "thumb": DEAD % "show",
                                         "art": DEAD % "show-art", "theme": DEAD % "theme"},
            "5d9c0996e264b7001fc52f30": {"guid": "plex://season/5d9c0996e264b7001fc52f30", "type": "season", "thumb": DEAD % "season",
                                         "art": DEAD % "season-art"},
            EPISODE[3].rsplit("/", 1)[1]: {"guid": EPISODE[3], "type": "episode", "thumb": DEAD % "still"},
        }
        call(instance, "PUT", "/library/metadata/%s/refresh" % show, token=SERVER_TOKEN)
        done = wait_for(lambda: stored(instance, season, "posters") == DEAD % "season", 10)
        after = {name: stored(instance, row, kind) for name, row, kind in
                 (("show", show, "posters"), ("show art", show, "arts"), ("season", season, "posters"),
                  ("season art", season, "arts"), ("episode", leaf, "posters"))}
        check("(d) a show's refresh takes its poster and background, and its season's poster",
              done and after["show"] == DEAD % "show" and after["show art"] == DEAD % "show-art" and after["season"] == DEAD % "season",
              (before, after))
        check("(d) never a background its season only borrows, nor the episode's still",
              after["season art"] == before["season art"] and after["episode"] == before["episode"], (before, after))
        check("(d) and the show's theme, at a new path", theme and detail(instance, show).get("theme") not in (None, theme),
              (theme, detail(instance, show).get("theme")))
        # The season's poster as it is, its background another: the copy of the show's it keeps is not its own.
        stub.items["5d9c0996e264b7001fc52f30"]["art"] = DEAD % "season-art-2"
        call(instance, "PUT", "/library/metadata/%s/refresh" % season, token=SERVER_TOKEN)
        wait_for(lambda: asked_for(stub, "5d9c0996e264b7001fc52f30") >= 2, 10)
        time.sleep(1)
        check("(d) a season serving its show's background takes none of the agent's for it",
              "ARTWORK %s -- Plex's agent" % season not in instance.output().split("REFRESH %s" % season)[-1], season)
        call(instance, "PUT", "/library/metadata/%s/refresh" % leaf, token=SERVER_TOKEN)
        still = wait_for(lambda: stored(instance, leaf, "posters") == DEAD % "still" and DEAD % "still", 10)
        check("(d) an episode's refresh takes its still", before["episode"] and still, (before["episode"], stored(instance, leaf, "posters")))
    finally:
        instance.stop()
        shutil.rmtree(directory, ignore_errors=True)

    # -- (e) a film with no poster yet ----------------------------------------------------
    stub.posterless, stub.artwork = True, ("", "")
    directory = fresh(True)
    instance = Instance(directory, free_port(), stub.server_address[1], plex_token=None, announce=None,
                        env_extra={"REX_AGENT_ARTWORK_RETRY_S": str(RETRY)})
    try:
        push(instance)
        row = (wait_for(lambda: named_on_claim.named(instance), 20) or rows(instance))[MOVIE[0]]
        key = row.get("ratingKey")
        check("(e) a film the agent names no poster for draws the tile", row.get("guid") == MOVIE[3] and stored(instance, key, "posters") is None,
              (row.get("guid"), stored(instance, key, "posters")))
        theme = detail(instance, key).get("theme")
        stub.artwork, stub.theme = (DEAD % "late-poster", DEAD % "late-art"), DEAD % "late-theme"
        call(instance, "PUT", "/library/metadata/%s/refresh" % key, token=SERVER_TOKEN)
        poster = wait_for(lambda: stored(instance, key, "posters") == DEAD % "late-poster" and DEAD % "late-poster", 10)
        check("(e) and takes the poster and background the agent names later on Refresh Metadata",
              poster and stored(instance, key, "arts") == DEAD % "late-art", (stored(instance, key, "posters"), stored(instance, key, "arts")))
        late = detail(instance, key).get("theme")
        check("(e) and a theme it had none of, at a path of its own", theme is None and str(late).startswith("/library/metadata/%s/theme" % key),
              (theme, late))
    finally:
        instance.stop()
        stub.shutdown()
        shutil.rmtree(directory, ignore_errors=True)
    print("\n%d/%d passed" % (sum(plextv.results), len(plextv.results)))
    return 0 if all(plextv.results) else 1


if __name__ == "__main__":
    sys.exit(main())
