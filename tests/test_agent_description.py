#!/usr/bin/env python3
"""What Plex's agent says of a title is shown as a real PMS shows it: theme music, clear logos,
square art, every person it credits, a film's original title and a show's networks.

rex kept six people of each kind and dropped the rest of what Plex's agent answers for a title:
on the production server 232 shows had theme music and 2,531 films a clear logo, and none was
served (2026-11-04). `fixtures/agent_description_s1434.json` is plexlab (PMS 1.43.4, its harness
clips Big Buck Bunny and Firefly) answering Plex Web's and Plex for Android's own detail, grid and
children requests, and the theme, logo and square-art reads (bytes as length and SHA-256,
`probes/record_agent_description.py`). Plex's agent answers for the same titles come from
`fixtures/feed/specials.json` (Firefly), `fixtures/provider/matches.json` (Big Buck Bunny) and
`fixtures/feed/blank_tiles.json` (A Serbian Film, whose original title is Srpski film; the 1.42.2
reference stores the agent's original title for 474 of its 1,541 matched films).

The first leg pushes the three titles as their source placed them. The second starts on rows an
older rex described (`fixtures/agent_description_rows.json`, read from its state file after the
same push), which a start gives what it dropped from the answers it kept. The theme, logo and
square-art bytes are fetched from Plex's hosts; offline those checks skip.

    python3 tests/test_agent_description.py
"""
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

from test_feed import ENTRIES, LEGACY, MOUNT, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
REFERENCE = json.load(open(os.path.join(FIXTURES, "agent_description_s1434.json")))
HELD = json.load(open(os.path.join(FIXTURES, "agent_description_rows.json")))
SPECIALS = json.load(open(os.path.join(FIXTURES, "feed", "specials.json")))
TILES = json.load(open(os.path.join(FIXTURES, "feed", "blank_tiles.json")))
MATCHES = json.load(open(os.path.join(FIXTURES, "provider", "matches.json")))
SERBIAN = 'POST /library/metadata/matches {"guid": "tmdb://73861", "type": 1}'


def python_key(entry):
    """`plexmeta.rs`'s cache key for a recorded agent exchange: Python's sorted `json.dumps`."""
    return "%s %s %s" % (entry["method"], entry["path"], json.dumps(entry["body"], sort_keys=True) if entry.get("body") else "")


def answers():
    out = dict(SPECIALS["plex"])
    out[SERBIAN] = TILES["plex"][SERBIAN]
    for entry in MATCHES:
        if (entry.get("body") or {}).get("guid") == "tmdb://10378":
            out[python_key(entry)] = entry["response"]
    return out


def movie_item(release, tmdb):
    path = "movies/%s/%s.mkv" % (release, release)
    return {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": release, "release": "movies/" + release,
            "size": 1234567, "hash": hashlib.sha1(release.encode()).hexdigest(), "ids": {"tmdb": tmdb}, "kind": "movie",
            "media_info": ENTRIES["movie"]["entry"]["media_info"]}


def episode_item():
    pushed = SPECIALS["pushes"]["s01e02_the_train_job"]
    path = pushed["path"]
    out = {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": pushed["name"],
           "release": "/".join(path.split("/")[:2]), "size": pushed["size"], "hash": pushed["hash"],
           "ids": pushed["ids"], "folder": pushed["folder"], "media_info": ENTRIES["episode"]["entry"]["media_info"]}
    out.update(pushed["placed"])
    return out


ITEMS = [movie_item("Big.Buck.Bunny.2008.1080p.BluRay.x264", 10378), movie_item("A.Serbian.Film.2010.1080p.BluRay.x264", 73861),
         episode_item()]


def catalog(work, rows=(), plex=None):
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000f3", "friendlyName": "rex-described",
               "sections": LEGACY["sections"], "items": list(rows)}, open(os.path.join(work, "catalog.json"), "w"))
    json.dump(plex or answers(), open(os.path.join(work, "plex_cache.json"), "w"))


def reference(method, path, headers=None):
    return next(entry for entry in REFERENCE["exchanges"]
                if entry["method"] == method and entry["path"] == path and entry["headers"] == (headers or {}))


def ref_row(path, index=0):
    return reference("GET", path)["body"]["MediaContainer"]["Metadata"][index]


VERSIONS = REFERENCE["versions"]
WEB = reference("GET", "/library/metadata/6?" + REFERENCE["exchanges"][0]["path"].split("?", 1)[1])["path"].split("?", 1)[1]
DROID = REFERENCE["exchanges"][4]["path"].split("?", 1)[1]
GRID = REFERENCE["exchanges"][6]["path"].split("?", 1)[1]
CHILDREN = REFERENCE["exchanges"][8]["path"].split("?", 1)[1]


def shape(value, keys):
    """A path or row with each server's own keys and artwork versions put aside: plexlab's 6, 7, 8
    and 1 are rex's show, season, episode and film."""
    text = json.dumps(value)
    for theirs, ours in keys.items():
        text = re.sub(r"/library/metadata/%s/" % theirs, "/library/metadata/{%s}/" % ours, text)
    return json.loads(re.sub(r"(/library/metadata/\{\w+\}/(?:thumb|art|theme|clearLogo|squareArt))/\d+", r"\1/V", text))


def images(row):
    return [(image["type"], image["url"]) for image in row.get("Image") or []]


def ratings(row):
    return {field: row.get(field) for field in ("rating", "ratingImage", "audienceRating", "audienceRatingImage", "Rating")}


def people(row, field):
    return [(tag.get("tag"), tag.get("tagKey")) for tag in row.get(field) or []]


def agent_image(answers, alt, kind):
    """The URL of the image of `kind` Plex's agent answers for `alt`."""
    stack = [answers]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if value.get("type") == kind and value.get("alt") == alt and "url" in value:
                return value["url"]
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    raise LookupError("no %s for %s" % (kind, alt))


def fetched(url):
    """What Plex's host serves for `url` to this machine now."""
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def online():
    try:
        socket.create_connection(("metadata-static.plex.tv", 443), timeout=5).close()
        return True
    except OSError:
        return False


def raw(server, method, path, headers=None):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (server.port, path), method=method,
                                     headers=dict({"X-Plex-Token": server.token}, **(headers or {})))
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def rex_keys(server):
    keys = server.keys()
    film = keys[ITEMS[0]["id"]]
    episode = keys[ITEMS[2]["id"]]
    row = server.row(episode)
    return {"1": "film", "6": "show", "7": "season", "8": "episode"}, {
        "film": film, "serbian": keys[ITEMS[1]["id"]], "episode": episode,
        "season": row.get("parentRatingKey"), "show": row.get("grandparentRatingKey")}


def ours(rex, value):
    text = json.dumps(value)
    for name, key in rex.items():
        text = text.replace("/library/metadata/%s/" % key, "/library/metadata/{%s}/" % name)
    return shape(json.loads(text), {})


def described(label, server, bytes_too=True):
    names, rex = rex_keys(server)
    ref = lambda value: shape(value, names)
    get = lambda key, query: (server.rows("/library/metadata/%s?%s" % (rex[key], query)) or [{}])[0]

    # -- the detail pages, as Plex Web and Plex for Android ask for them -----------------------
    show, want = get("show", WEB), ref_row("/library/metadata/6?" + WEB)
    check("%s: the show names its theme at its own path, as plexlab's Firefly does" % label,
          ours(rex, show.get("theme")) == ref(want["theme"]), (show.get("theme"), want["theme"]))
    check("%s: the show's Images are plexlab's four: poster, background, clear logo, square art" % label,
          ours(rex, images(show)) == ref(images(want)), (images(show), images(want)))
    check("%s: the show lists its whole cast, each by the agent's own person id" % label,
          people(show, "Role") == people(want, "Role") and len(want["Role"]) == 112, (len(show.get("Role") or []), people(show, "Role")[:3]))
    check("%s: and no network, director, writer or producer, which plexlab's show has none of" % label,
          all(field not in show and field not in want for field in ("Network", "Director", "Writer", "Producer")),
          [field for field in ("Network", "Director", "Writer", "Producer") if field in show])
    check("%s: the show's ratings are plexlab's: its TMDB score, and every source's in Rating" % label,
          ratings(show) == ratings(want), (ratings(show), ratings(want)))
    season, want = get("season", WEB), ref_row("/library/metadata/7?" + WEB)
    check("%s: the season's parentTheme is its show's theme" % label,
          ours(rex, season.get("parentTheme")) == ref(want["parentTheme"]), (season.get("parentTheme"), want["parentTheme"]))
    check("%s: the season's clear logo and square art are its show's" % label,
          ours(rex, images(season))[2:] == ref(images(want))[2:], (images(season), images(want)))
    for query, client in ((WEB, "Plex Web"), (DROID, "Plex for Android")):
        episode, want = get("episode", query), ref_row("/library/metadata/8?" + query)
        check("%s: %s's episode: grandparentTheme its show's" % (label, client),
              ours(rex, episode.get("grandparentTheme")) == ref(want["grandparentTheme"]), (episode.get("grandparentTheme"), want["grandparentTheme"]))
        check("%s: %s's episode: Images poster, snapshot, background, the show's clear logo and square art" % (label, client),
              [kind for kind, _ in images(episode)] == [kind for kind, _ in images(want)]
              and ours(rex, images(episode))[3:] == ref(images(want))[3:], (images(episode), images(want)))
        check("%s: %s's episode: its own 24 actors, director, writers and producers" % (label, client),
              people(episode, "Role") == people(want, "Role")
              and all(sorted(people(episode, field)) == sorted(people(want, field)) for field in ("Director", "Writer", "Producer")),
              {field: (people(episode, field)[:3], people(want, field)[:3]) for field in ("Role", "Director", "Writer", "Producer")})
        check("%s: %s's episode: its own ratings, not its show's" % (label, client),
              ratings(episode) == ratings(want), (ratings(episode), ratings(want)))
    for query, client in ((WEB, "Plex Web"), (DROID, "Plex for Android")):
        film, want = get("film", query), ref_row("/library/metadata/1?" + query)
        check("%s: %s's film: Images poster, background, clear logo, square art" % (label, client),
              ours(rex, images(film)) == ref(images(want)), (images(film), images(want)))
        check("%s: %s's film: its producer, director and writer, by the agent's person ids" % (label, client),
              all(people(film, field) == people(want, field) for field in ("Producer", "Director", "Writer"))
              and "Role" not in film and "theme" not in film,
              {field: people(film, field) for field in ("Producer", "Director", "Writer", "Role")})
        check("%s: %s's film: the agent's ratings, Rotten Tomatoes' and every source's in Rating" % (label, client),
              ratings(film) == ratings(want), (ratings(film), ratings(want)))
    serbian = get("serbian", WEB)
    agent = TILES["plex"][SERBIAN]["MediaContainer"]["Metadata"][0]
    check("%s: a film shows the original title Plex's agent gives it" % label,
          serbian.get("originalTitle") == agent["originalTitle"] == "Srpski film", serbian.get("originalTitle"))

    # -- the grid and the children lists ------------------------------------------------------
    grid = {row.get("type"): row for row in server.rows("/library/sections/2/all?" + GRID)}
    want = ref_row("/library/sections/2/all?" + GRID)
    row = grid.get("show", {})
    check("%s: the show's grid row names its theme and the four Images, three of its cast" % label,
          ours(rex, row.get("theme")) == ref(want["theme"]) and ours(rex, images(row)) == ref(images(want))
          and [tag.get("tag") for tag in row.get("Role") or []] == [tag["tag"] for tag in want["Role"]],
          (row.get("theme"), images(row), row.get("Role")))
    check("%s: the show's grid row carries its TMDB score and no Rating list" % label, ratings(row) == ratings(want), (ratings(row), ratings(want)))
    films = {row.get("ratingKey"): row for row in server.rows("/library/sections/1/all?" + GRID)}
    want = next(row for row in reference("GET", "/library/sections/1/all?" + GRID)["body"]["MediaContainer"]["Metadata"] if row["ratingKey"] == "1")
    row = films.get(rex["film"], {})
    check("%s: the film's grid row carries the four Images" % label, ours(rex, images(row)) == ref(images(want)), images(row))
    check("%s: the film's grid row carries its ratings and no Rating list" % label, ratings(row) == ratings(want), (ratings(row), ratings(want)))
    seasons = server.rows("/library/metadata/%s/children?%s" % (rex["show"], CHILDREN))
    want = ref_row("/library/metadata/6/children?" + CHILDREN)
    row = next((row for row in seasons if row.get("ratingKey") == rex["season"]), {})
    check("%s: the season's children row: parentTheme and the show's logo and square art" % label,
          ours(rex, row.get("parentTheme")) == ref(want["parentTheme"]) and ours(rex, images(row))[2:] == ref(images(want))[2:],
          (row.get("parentTheme"), images(row)))
    leaves = server.rows("/library/metadata/%s/children?%s" % (rex["season"], CHILDREN))
    want = ref_row("/library/metadata/7/children?" + CHILDREN)
    row = next((row for row in leaves if row.get("ratingKey") == rex["episode"]), {})
    check("%s: the episode's children row: grandparentTheme and the five Images" % label,
          ours(rex, row.get("grandparentTheme")) == ref(want["grandparentTheme"])
          and [kind for kind, _ in images(row)] == [kind for kind, _ in images(want)], (row.get("grandparentTheme"), images(row)))
    check("%s: the episode's children row carries its own score" % label, ratings(row) == ratings(want), (ratings(row), ratings(want)))
    axis = server.request("/library/sections/2/network")[1]["MediaContainer"]
    want = reference("GET", "/library/sections/2/network")["body"]["MediaContainer"]
    check("%s: the show is filed under its network, By Network, as plexlab files Firefly" % label,
          [entry["title"] for entry in axis.get("Directory") or []] == [entry["title"] for entry in want["Directory"]]
          and axis.get("title2") == want["title2"], axis)

    # -- the candidate list Plezy and python-plexapi pick square art from ---------------------
    for key, ref_key in (("show", "6"), ("film", "1"), ("season", "7")):
        status, body = server.request("/library/metadata/%s/squareArts" % rex[key])
        listed = (body or {}).get("MediaContainer", {}) if status == 200 else {}
        want = reference("GET", "/library/metadata/%s/squareArts" % ref_key)["body"]["MediaContainer"]
        rows = listed.get("Metadata") or []
        check("%s: the %s's squareArts answers %s" % (label, key, "its square art, selected" if want["size"] else "none"),
              status == 200 and bool(rows) == bool(want["size"]) and (not rows or rows[0].get("selected") is True),
              (status, listed))

    # -- the reads: what a borrower and a film without a theme answer, then the bytes ----------
    for path, name in ((("season", "theme"), "a season's theme"), (("season", "clearLogo"), "a season's clear logo"),
                       (("season", "squareArt"), "a season's square art"), (("film", "theme"), "a film's theme")):
        key, leaf = path
        status = raw(server, "GET", "/library/metadata/%s/%s/1" % (rex[key], leaf))[0]
        want = reference("GET", "/library/metadata/%s/%s/%s" % ({"season": "7", "film": "1"}[key], leaf,
                                                                  VERSIONS["show" if key == "season" else "movie"]))["status"]
        check("%s: %s is plexlab's %d" % (label, name, want), status == want, status)
    if not bytes_too:
        return
    if not online():
        print("SKIP  %s: theme, logo and square-art bytes (Plex's hosts unreachable)" % label)
        return
    theme = "/library/metadata/%s/theme/%s" % (rex["show"], show.get("theme", "/1").rsplit("/", 1)[1])
    for method, path, headers, ref_path in (
            ("GET", theme, {"Range": "bytes=0-"}, "/library/metadata/6/theme/" + VERSIONS["show"]),
            ("GET", theme, {}, "/library/metadata/6/theme/" + VERSIONS["show"]),
            ("HEAD", theme, {}, "/library/metadata/6/theme/" + VERSIONS["show"]),
            ("GET", "/library/metadata/%s/theme" % rex["show"], {}, "/library/metadata/6/theme"),
            ("GET", "/library/metadata/%s/clearLogo/1" % rex["show"], {}, "/library/metadata/6/clearLogo/" + VERSIONS["show"]),
            ("GET", "/library/metadata/%s/squareArt/1" % rex["show"], {}, "/library/metadata/6/squareArt/" + VERSIONS["show"]),
            ("GET", "/library/metadata/%s/squareArt" % rex["film"], {}, "/library/metadata/1/squareArt"),
            ("GET", "/library/metadata/%s/clearLogo/1" % rex["film"], {"Range": "bytes=0-99"}, "/library/metadata/1/clearLogo/%s" % VERSIONS["movie"])):
        status, got, body = raw(server, method, path, headers)
        want = reference(method, ref_path, headers)
        heads = {name.lower(): value for name, value in want["response_headers"]}
        same = {name: got.get(name.title() if name != "x-plex-local-asset" else "X-Plex-Local-Asset") for name in heads}
        # The film's logo is the agent's pick when each was asked, which has changed since plexlab's.
        logo = "/clearLogo/" in ref_path and ref_path.startswith("/library/metadata/1/")
        if logo:
            heads.pop("content-range"), same.pop("content-range")
        # The show's logo is plexlab's file as Plex's CDN serves it here: one URL and ETag, but
        # Frankfurt's copy is 11,818 bytes and Berlin's the recorded 11,816 (2026-11-05).
        if ref_path == "/library/metadata/6/clearLogo/" + VERSIONS["show"]:
            served = fetched(agent_image(SPECIALS["plex"], "Firefly", "clearLogo"))
            want = dict(want, length=len(served), sha256=hashlib.sha256(served).hexdigest())
            heads["content-length"] = str(len(served))
        check("%s: %s %s%s: plexlab's %d, %s%s" % (label, method, path.split("/", 4)[-1], " " + headers["Range"] if headers else "",
                                                     want["status"], heads.get("content-type"), "" if logo else " and its bytes"),
              status == want["status"] and same == heads
              and (logo or (len(body), hashlib.sha256(body).hexdigest()) == (want["length"], want["sha256"])),
              (status, got, len(body), want))


def pushed(bytes_port):
    work = tempfile.mkdtemp(prefix="rex-described-")
    catalog(work)
    server = Server(work, bytes_port)
    try:
        status, body = server.feed("POST", "items", {"items": ITEMS})
        check("the three titles are taken", status == 200 and not body.get("rejected"), (status, body))
        described("pushed", server)
    finally:
        server.stop()
    # A start keeps all of it.
    server = Server(work, bytes_port)
    try:
        described("after a start", server, bytes_too=False)
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)


def held(bytes_port):
    """The rows an older rex made of the same push, which carried six people and none of the rest."""
    work = tempfile.mkdtemp(prefix="rex-described-held-")
    catalog(work, HELD["rows"])
    show = next(row for row in HELD["rows"] if row["type"] == "show")
    check("held: the older rex's show had six of the cast and no theme",
          len(show.get("Role") or []) == 6 and "_theme_url" not in show and "_described" not in show, show.get("Role"))
    server = Server(work, bytes_port)
    try:
        deadline = time.time() + 30
        while time.time() < deadline and "theme" not in server.row(show["ratingKey"]):
            time.sleep(0.5)
        described("held, after a start", server)
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)


def held_matched(bytes_port):
    """The same older rows, the film matched by hand and the episode one Plex's agent rates nowhere
    (as a real library's Long Way Down S1E1): the film takes its ratings, its match kept, and the
    episode drops the show's score the older rex gave it."""
    work = tempfile.mkdtemp(prefix="rex-described-matched-")
    rows = json.loads(json.dumps(HELD["rows"]))
    film = next(row for row in rows if row["title"] == "Big Buck Bunny")
    film["_matched"] = True
    episode = next(row for row in rows if row["type"] == "episode")
    plex = answers()
    for answer in plex.values():
        for row in (answer.get("MediaContainer") or {}).get("Metadata") or []:
            if row.get("guid") == episode["guid"]:
                for field in ("audienceRating", "audienceRatingImage", "Rating"):
                    row.pop(field, None)
    catalog(work, rows, plex)
    server = Server(work, bytes_port)
    try:
        deadline = time.time() + 30
        while time.time() < deadline and "audienceRatingImage" not in server.row(film["ratingKey"]):
            time.sleep(0.5)
        got, want = server.rows("/library/metadata/%s?%s" % (film["ratingKey"], WEB))[0], ref_row("/library/metadata/1?" + WEB)
        check("held, matched by hand: the film takes plexlab's ratings", ratings(got) == ratings(want), (ratings(got), ratings(want)))
        kept = server.row(film["ratingKey"])
        check("held, matched by hand: and keeps its match", (kept.get("guid"), kept.get("title")) == (film["guid"], film["title"]),
              (kept.get("guid"), kept.get("title")))
        got = server.rows("/library/metadata/%s?%s" % (episode["ratingKey"], WEB))[0]
        check("held: an episode Plex's agent rates nowhere drops its show's score", not any(ratings(got).values()), ratings(got))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)


def main():
    source = Bytes()
    bytes_port = serve(source.handler())
    pushed(bytes_port)
    held(bytes_port)
    held_matched(bytes_port)
    print("\n%d/%d passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
