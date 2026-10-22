#!/usr/bin/env python3
"""A file its source placed is where the source says, and stays there.

zurg asks DMM what each file is and pushes the answer with it (`kind`, the movie's or show's
`ids`, TMDB's `season`/`episode`; docs/feed-api.md). rex reads nothing off a name: a file pushed
without a placement is season 1 of a show named after its release, in file order. These are
real releases rex misplaced by name before 2026-10-17, as zurg pushed them
(`fixtures/feed/anime.json`, `blank_tiles.json`, `blank_1016.json`, with Plex's answers where
recorded), pushed with the placement TMDB numbers them by: fansub cours TMDB numbers as
a season or as episodes further on (`Hibike Euphonium 3 - 01` is S3E1, Uma Musume's `Part 2 - 05`
S1E18), scene numbers TMDB does not have (`Detective.Conan.S33E01`, TMDB's S1E1103 in the
placement contract; `Bungo.Stray.Dogs.S04E01`, TMDB's S1E37), and films zurg files under anime.

Each is checked placed as pushed, and still there after a start, after the
blank tile retry, and after a later push that places it elsewhere
(same key, watched). A person's Fix
Match wins over a placement, and a file pushed without one is numbered in file order. Episodes placed
on season 0 (`specials.json`, Firefly's) are Plex's specials by name, like any other episode.

    python3 tests/test_feed_placement.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse

from test_feed import ENTRIES, LEGACY, MOUNT, Bytes, Server, check, results, serve
import test_feed_blank_tiles as blank

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
ANIME = json.load(open(os.path.join(FIXTURES, "feed", "anime.json")))
TILES = json.load(open(os.path.join(FIXTURES, "feed", "blank_tiles.json")))
EVA = json.load(open(os.path.join(FIXTURES, "feed", "blank_1016.json")))
SPECIALS = json.load(open(os.path.join(FIXTURES, "feed", "specials.json")))

# TMDB's numbers for each, as `anime.json` recorded TMDB's answers for them; Case Closed's from
# the placement contract (TMDB lists one season of 1,217 episodes, rex's name read keeps S33).
# Left out: `bleach_tybw` (its `want` is a one-episode TMDB stub) and `kingdom_scene`, pushed
# without a placement.
PLACED = {name: pushed["want"] for name, pushed in ANIME["pushes"].items() if pushed["want"][0] and name != "bleach_tybw"}
PLACED["case_closed"] = [30983, 1, 1103]


def item(fixture, name, ids=None, **placement):
    pushed = fixture["pushes"][name]
    path = pushed["path"]
    probe = ENTRIES["movie" if placement.get("kind") == "movie" else "episode"]["entry"]["media_info"]
    out = {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": pushed["name"],
           "release": "/".join(path.split("/")[:2]), "size": pushed["size"], "hash": pushed["hash"],
           "ids": pushed["ids"] if ids is None else ids, "media_info": probe}
    out.update(placement)
    return out


def episode(name, numbers=None):
    """`anime.json`'s push of `name`, placed on `numbers` (TMDB show, season, episode)."""
    show, season, number = numbers or PLACED[name]
    return item(ANIME, name, dict(ANIME["pushes"][name]["ids"], tmdb=show), kind="episode", season=season, episode=number)


def catalog(work, *fixtures, rows=(), anime=True):
    sections = json.loads(json.dumps(LEGACY["sections"]))
    if anime:
        next(section for section in sections if section["type"] == "show")["Location"].append({"id": 3, "path": "/zurg/anime"})
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000f9", "friendlyName": "rex-placed",
               "sections": sections, "items": list(rows)}, open(os.path.join(work, "catalog.json"), "w"))
    merged = {}
    for fixture in fixtures:
        merged.update(fixture.get("plex", {}))
    json.dump(merged, open(os.path.join(work, "plex_cache.json"), "w"))


def start(work, bytes_port):
    return Server(work, bytes_port)


def stop(server, work):
    server.stop()


def placed(server, key):
    """(the show's TMDB id, season, episode) an episode row sits on."""
    row = server.row(key)
    show = server.row(row.get("grandparentRatingKey")) if row.get("grandparentRatingKey") else {}
    tmdb = next((int(guid["id"][7:]) for guid in show.get("Guid") or [] if guid.get("id", "").startswith("tmdb://")), None)
    return [tmdb, row.get("parentIndex"), row.get("index")]


def log_count(work, text):
    return open(os.path.join(work, "server.log"), errors="replace").read().count(text)


def check_placed(label, server, names, keys):
    for name in names:
        got = placed(server, keys.get(item(ANIME, name)["id"]))
        check("%s: %s is TMDB %s's S%sE%s" % (label, name, *PLACED[name]), got == PLACED[name], got)


def live(bytes_port):
    work = tempfile.mkdtemp(prefix="rex-placed-")
    catalog(work, ANIME)
    server = start(work, bytes_port)
    try:
        # -- a row nothing placed moves once its source places it, on its key -----------------
        status, body = server.feed("POST", "items", {"items": [item(ANIME, "bungo_scene")]})
        bungo = server.keys().get(item(ANIME, "bungo_scene")["id"])
        check("pushed with its IMDb id and no placement, Bungo.Stray.Dogs.S04E01 is unmatched, its release's first file",
              status == 200 and placed(server, bungo) == [None, 1, 1], (body, placed(server, bungo)))
        server.request("/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % bungo)
        server.feed("POST", "items", {"items": [episode("bungo_scene")]})
        check("placed, it is TMDB's S1E37, on the same key", server.keys().get(item(ANIME, "bungo_scene")["id"]) == bungo
              and placed(server, bungo) == PLACED["bungo_scene"], (server.keys(), placed(server, bungo)))
        check("and what was watched stays watched", server.row(bungo).get("viewCount") == 1, server.row(bungo).get("viewCount"))

        # -- placed again elsewhere: moved on its key -----------------------------------------
        # The Ascendance BD's `S02E01-Apprentice Priestess` as named (TMDB has a season 2), then
        # as TMDB numbers that cour: S1E15.
        server.feed("POST", "items", {"items": [episode("ascendance_bd_s02e01", [91768, 2, 1])]})
        bd = server.keys().get(item(ANIME, "ascendance_bd_s02e01")["id"])
        check("placed on S2E1, it is there", placed(server, bd) == [91768, 2, 1], placed(server, bd))
        server.request("/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % bd)
        server.feed("POST", "items", {"items": [episode("ascendance_bd_s02e01")]})
        check("a later push placing it on S1E15 moves it there, on the same key, watched",
              server.keys().get(item(ANIME, "ascendance_bd_s02e01")["id"]) == bd and placed(server, bd) == [91768, 1, 15]
              and server.row(bd).get("viewCount") == 1, (placed(server, bd), server.row(bd).get("viewCount")))

        # -- every placement as pushed; a file without one in file order ------------------------
        rest = sorted(set(PLACED) - {"bungo_scene", "ascendance_bd_s02e01"})
        status, body = server.feed("POST", "items", {"items": [episode(name) for name in rest] + [item(ANIME, "kingdom_scene")]})
        check("every file is taken", status == 200 and not body.get("rejected"), (status, body))
        keys = server.keys()
        check_placed("pushed", server, sorted(PLACED), keys)
        check("the files of one episode are one row (Ascendance's two S4 - 01)",
              keys[item(ANIME, "ascendance_scene_s04e01")["id"]] == keys[item(ANIME, "ascendance_erai_s4_01")["id"]], keys)
        kingdom = placed(server, keys[item(ANIME, "kingdom_scene")["id"]])
        check("a file pushed without a placement is its release's first, not its name's S1E3 (Kingdom.S01E03)",
              kingdom == [None, 1, 1], kingdom)
    finally:
        stop(server, work)

    # -- a start leaves each where it was placed -----------------------------------------------
    label = "after a start"
    before = log_count(work, "FEED re-identified")
    server = start(work, bytes_port)
    try:
        time.sleep(20)
        check_placed(label, server, sorted(PLACED), keys)
        check("%s: every key kept, nothing identified again" % label,
              server.keys() == keys and log_count(work, "FEED re-identified") == before,
              (before, log_count(work, "FEED re-identified")))
        check("%s: what was watched stays watched" % label, server.row(bungo).get("viewCount") == 1 and server.row(bd).get("viewCount") == 1)
    finally:
        stop(server, work)
    shutil.rmtree(work, ignore_errors=True)


def retried(bytes_port):
    """With Plex's agent knowing none of them no show has a poster: every row is on the blank tile, and the start
    pushes each again as its row says its source pushed it (`retry_blank`)."""
    work = tempfile.mkdtemp(prefix="rex-placed-retry-")
    catalog(work, ANIME)
    server = start(work, bytes_port)
    try:
        server.feed("POST", "items", {"items": [episode(name) for name in sorted(PLACED)]})
        keys = server.keys()
        check_placed("with no poster", server, sorted(PLACED), keys)
    finally:
        stop(server, work)
    retries = log_count(work, "FEED retrying")
    server = start(work, bytes_port)
    try:
        deadline = time.time() + 120
        text = lambda: open(os.path.join(work, "server.log"), errors="replace").read()
        while time.time() < deadline and not (log_count(work, "FEED retrying") > retries
                                               and "FEED zurg upsert" in text().rsplit("FEED retrying", 1)[-1]):
            time.sleep(0.2)
        check("the start retried the blank rows", log_count(work, "FEED retrying") > retries, log_count(work, "FEED retrying"))
        check_placed("after the blank-tile retry", server, sorted(PLACED), keys)
        check("after the blank-tile retry: every key kept", server.keys() == keys, server.keys())
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)


def films(bytes_port):
    """`kind` decides the library, and a person's Fix Match wins over any placement."""
    work = tempfile.mkdtemp(prefix="rex-placed-films-")
    catalog(work, TILES, EVA)
    server = start(work, bytes_port)
    conan_item = item(TILES, "conan", {"tmdb": 30983}, kind="episode", season=1, episode=221)
    serbian_item = lambda tmdb: item(TILES, "serbian", {"tmdb": tmdb}, kind="movie")
    eva_item = item(EVA, "eva_111", {"imdb": "tt0923811", "tmdb": 15137}, kind="movie", folder="")
    try:
        status, body = server.feed("POST", "items", {"items": [conan_item, serbian_item(73861), eva_item]})
        keys = server.keys()
        conan, serbian, eva = (server.row(keys.get(one["id"])) for one in (conan_item, serbian_item(0), eva_item))
        show = server.row(conan.get("grandparentRatingKey")) if conan.get("grandparentRatingKey") else {}
        check("Detektiv.Conan.E0221, filed under movies and placed as an episode, is TMDB 30983's S1E221",
              status == 200 and conan.get("type") == "episode" and str(conan.get("librarySectionID")) == "2"
              and [conan.get("parentIndex"), conan.get("index")] == [1, 221] and "tmdb://30983" in blank.guids(show),
              (body, conan.get("type"), conan.get("librarySectionID"), conan.get("parentIndex"), conan.get("index"), blank.guids(show)))
        check("A.Serbian.Film, placed as TMDB 73861, is A Serbian Film",
              serbian.get("type") == "movie" and "tmdb://73861" in blank.guids(serbian), (serbian.get("title"), blank.guids(serbian)))
        check("[Baws] Evangelion 1.11, filed under anime and placed as a film, is a film in Movies",
              eva.get("type") == "movie" and str(eva.get("librarySectionID")) == "1" and "tmdb://15137" in blank.guids(eva),
              (eva.get("type"), eva.get("librarySectionID"), blank.guids(eva)))

        key = serbian.get("ratingKey")
        status, chosen = blank.match(server, key, "Elementos", 2023)
        check("a person Fix Matches A.Serbian.Film to another film", status == 200 and chosen.get("guid")
              and server.row(key).get("guid") == chosen["guid"], (status, chosen, server.row(key).get("guid")))
        server.feed("POST", "items", {"items": [serbian_item(631842)]})
        check("a later push placing it as another film leaves the match as the person made it",
              server.row(key).get("guid") == chosen.get("guid") and server.keys().get(serbian_item(0)["id"]) == key,
              (server.row(key).get("guid"), chosen.get("guid")))
    finally:
        stop(server, work)
    server = start(work, bytes_port)
    try:
        time.sleep(15)
        check("after a start the match is still the person's", server.row(key).get("guid") == chosen.get("guid"), server.row(key).get("guid"))
        conan = server.row(keys.get(conan_item["id"]))
        check("after a start the episode is still S1E221 and the film still in Movies",
              [conan.get("parentIndex"), conan.get("index")] == [1, 221] and server.row(keys.get(eva_item["id"])).get("type") == "movie",
              (conan.get("parentIndex"), conan.get("index")))
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)


def declare(server, **titles):
    body = {"url": "http://127.0.0.1:%d/dav" % server.bytes_port, "mount": MOUNT,
            "bearer": "bytes-secret-for-tests", "directories": [dict({"name": "anime"}, **titles)]}
    return server.request("/rex/sources/zurg", "PUT", body, {"Content-Type": "application/json"})


def section_of(server, key):
    return str(server.row(key).get("librarySectionID"))


def declared_moves(bytes_port):
    """zurg's `anime` declared a library of its own after these sat in TV Shows and Movies: each
    row moves there on its key as it is (`relocate`). A film a person Fix Matched keeps their
    match; a show's edits and picked poster go with it to the show made for it there; an episode
    held as two releases, one in `anime` and one in `shows` (Ascendance's two S4 - 01), stays put
    and is said once; two releases both in `anime` move together. Re-identifying a moved row lost
    the hand match, never moved a row of two releases, and dropped its show's edits."""
    work = tempfile.mkdtemp(prefix="rex-placed-declared-")
    catalog(work, ANIME, TILES, EVA, anime=False)
    server = start(work, bytes_port)
    eva_item = item(EVA, "eva_111", {"imdb": "tt0923811", "tmdb": 15137}, kind="movie", folder="")
    pushed = [episode("hibike"), episode("jjk_lostyears"), episode("ascendance_erai_s4_01"),
              episode("ascendance_scene_s04e01"), eva_item]
    try:
        server.feed("POST", "items", {"items": pushed})
        keys = server.keys()
        hibike, eva, pair = (keys.get(item(fixture, name)["id"]) for fixture, name in
                             ((ANIME, "hibike"), (EVA, "eva_111"), (ANIME, "ascendance_erai_s4_01")))
        check("before: the anime episodes are in TV Shows, the film in Movies, Ascendance's two S4 - 01 one row",
              (section_of(server, hibike), section_of(server, eva), section_of(server, pair)) == ("2", "1", "2")
              and keys.get(item(ANIME, "ascendance_scene_s04e01")["id"]) == pair, (keys, section_of(server, hibike)))
        status, chosen = blank.match(server, eva, "Elementos", 2023)
        show = server.row(hibike).get("grandparentRatingKey")
        season = server.row(hibike).get("parentRatingKey")
        server.request("/library/metadata/%s?title.value=%s&title.locked=1&titleSort.value=%s&titleSort.locked=1"
                       "&summary.value=%s&summary.locked=1&label[0].tag.tag=Kyoto" %
                       (show, urllib.parse.quote("Hibike! (ours)"), urllib.parse.quote("Euphonium, Sound"),
                        urllib.parse.quote("The brass band, as we tell it.")), "PUT")
        server.request("/library/metadata/%s?summary.value=%s&summary.locked=1" % (season, urllib.parse.quote("Third year.")), "PUT")
        picked = "https://image.tmdb.org/t/p/original/ours.jpg"
        server.request("/library/metadata/%s/poster?url=%s" % (show, urllib.parse.quote(picked, safe="")), "PUT")
        server.request("/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % hibike)
        check("a person Fix Matched the film and edited the show", status == 200 and chosen.get("guid")
              and server.row(eva).get("guid") == chosen["guid"] and server.row(show).get("title") == "Hibike! (ours)",
              (status, chosen, server.row(show).get("title")))

        declare(server, shows="Anime", movies="Anime Movies")
        _, listing = server.request("/library/sections")
        made = {row["title"]: row["key"] for row in listing["MediaContainer"]["Directory"]}
        for _ in range(2):
            server.feed("POST", "items", {"items": pushed})
        film = server.row(eva)
        check("the Fix Matched film moves to Anime Movies on its key, matched as the person matched it",
              section_of(server, eva) == made.get("Anime Movies") and film.get("guid") == chosen.get("guid")
              and server.keys().get(eva_item["id"]) == eva, (section_of(server, eva), film.get("guid"), chosen.get("guid")))
        moved = server.row(hibike)
        shown = server.row(moved.get("grandparentRatingKey"))
        check("the episode moves to Anime on its key, watched", section_of(server, hibike) == made.get("Anime")
              and moved.get("viewCount") == 1 and server.keys().get(episode("hibike")["id"]) == hibike,
              (section_of(server, hibike), moved.get("viewCount")))
        check("under a show there that keeps the show's edits: title, sort title, summary and label",
              moved.get("grandparentRatingKey") != show and shown.get("title") == "Hibike! (ours)"
              and shown.get("titleSort") == "Euphonium, Sound" and shown.get("summary") == "The brass band, as we tell it."
              and [tag.get("tag") for tag in shown.get("Label") or []] == ["Kyoto"]
              and moved.get("grandparentTitle") == "Hibike! (ours)",
              {name: shown.get(name) for name in ("title", "titleSort", "summary", "Label")})
        posters = server.request("/library/metadata/%s/posters" % moved.get("grandparentRatingKey"))[1]
        selected = [row.get("key") for row in (posters or {}).get("MediaContainer", {}).get("Metadata") or [] if row.get("selected")]
        check("its picked poster, and its season's summary",
              selected == [picked] and server.row(moved.get("parentRatingKey")).get("summary") == "Third year.",
              (selected, server.row(moved.get("parentRatingKey")).get("summary")))
        check("and the show and season it left are gone from TV Shows",
              server.request("/library/metadata/%s" % show)[0] == 404 and server.request("/library/metadata/%s" % season)[0] == 404,
              (server.request("/library/metadata/%s" % show)[0], server.request("/library/metadata/%s" % season)[0]))
        check("an episode whose two releases sit in anime and shows stays in TV Shows, said once",
              section_of(server, pair) == "2" and log_count(work, "FEED %s stays in TV Shows" % pair) == 1,
              (section_of(server, pair), log_count(work, "stays in TV Shows")))
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    work = tempfile.mkdtemp(prefix="rex-placed-declared-both-")
    catalog(work, ANIME, anime=False)
    server = start(work, bytes_port)
    # The scene release refiled under anime beside Erai's: both releases in the declared folder.
    scene = episode("ascendance_scene_s04e01")
    for field in ("id", "path", "file", "release"):
        scene[field] = scene[field].replace("shows/", "anime/", 1)
    try:
        server.feed("POST", "items", {"items": [episode("ascendance_erai_s4_01"), scene]})
        keys = server.keys()
        pair = keys.get(episode("ascendance_erai_s4_01")["id"])
        declare(server, shows="Anime")
        server.feed("POST", "items", {"items": [episode("ascendance_erai_s4_01"), scene]})
        _, listing = server.request("/library/sections")
        anime = {row["title"]: row["key"] for row in listing["MediaContainer"]["Directory"]}.get("Anime")
        check("an episode whose two releases are both in anime moves there, both with it",
              keys.get(scene["id"]) == pair and section_of(server, pair) == anime
              and len(server.row(pair).get("Media") or []) == 2, (keys, section_of(server, pair), anime))
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)


# Plex's rows for Firefly's seasons and their episodes, by (season, episode); a season by (season, None).
SPECIAL_PLEX = {}
for _key, _answer in SPECIALS["plex"].items():
    for _row in (_answer.get("MediaContainer") or {}).get("Metadata") or []:
        if _row.get("type") == "season":
            SPECIAL_PLEX[(_row["index"], None)] = _row
        elif _row.get("type") == "episode":
            SPECIAL_PLEX[(_row["parentIndex"], _row["index"])] = _row


def special(name):
    return item(SPECIALS, name, folder=SPECIALS["pushes"][name]["folder"], **SPECIALS["pushes"][name]["placed"])


def check_specials(label, server, held=False):
    """Each special is Plex's episode by title, summary and ids, in a season a real PMS titles
    `Specials` (findings, "Season 0"), with Plex's season-0 poster. Rows held from before rex read
    no TMDB keep what they were named then: only the blank ones (`Episode N`) are named again,
    and their season keeps its own ids."""
    names = sorted(SPECIALS["pushes"])
    checked = [name for name in names if SPECIALS["pushes"][name]["placed"]["season"] == 0] if held else names
    keys = server.keys()
    rows = {name: server.row(keys.get(special(name)["id"])) for name in names}
    guids = set()
    for name in checked:
        row, placed = rows[name], SPECIALS["pushes"][name]["placed"]
        answer = SPECIAL_PLEX[(placed["season"], placed["episode"])]
        check("%s: %s is Plex's S%dE%d %r, by its summary and ids too" % (label, name, placed["season"], placed["episode"], answer["title"]),
              [row.get("parentIndex"), row.get("index"), row.get("title"), row.get("summary")]
              == [placed["season"], placed["episode"], answer["title"], answer["summary"]]
              and {guid["id"] for guid in answer["Guid"]} <= set(blank.guids(row)) and row.get("guid") == answer["guid"],
              (row.get("parentIndex"), row.get("index"), row.get("title"), row.get("guid"), blank.guids(row)))
        guids.add(row.get("guid"))
    check("%s: each episode has a guid of its own" % label, len(guids) == len(checked), guids)
    season = server.row(rows["s01e11_trash"].get("parentRatingKey"))
    check("%s: their season is Specials, Plex's season 0, with Plex's poster" % label,
          season.get("index") == 0 and season.get("title") == "Specials"
          and (held or {guid["id"] for guid in SPECIAL_PLEX[(0, None)].get("Guid") or []} <= set(blank.guids(season)))
          and blank.poster(server, season.get("ratingKey")) is not None,
          (season.get("index"), season.get("title"), blank.guids(season), blank.poster(server, season.get("ratingKey"))))
    check("%s: under the same show as season 1" % label,
          len({row.get("grandparentRatingKey") for row in rows.values()}) == 1, [row.get("grandparentRatingKey") for row in rows.values()])
    return keys


def specials(bytes_port):
    """Firefly's Trash, The Message and Heart of Gold, which TMDB and Plex keep as season 0 and zurg
    placed there (`fixtures/feed/specials.json`, zen's end-to-end rig). rex drew them `Episode 1-3`
    under the show's movie guid; the rows it held so (`rows`) are named by the retry a start runs."""
    work = tempfile.mkdtemp(prefix="rex-placed-specials-")
    catalog(work, SPECIALS)
    server = start(work, bytes_port)
    try:
        status, body = server.feed("POST", "items", {"items": [special(name) for name in sorted(SPECIALS["pushes"])]})
        check("every Firefly file is taken", status == 200 and not body.get("rejected"), (status, body))
        check_specials("pushed", server)
    finally:
        stop(server, work)
    server = start(work, bytes_port)
    try:
        time.sleep(15)
        check_specials("after a start", server)
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)

    # -- held so: the same push changes nothing, so a start's retry names them ------------------
    work = tempfile.mkdtemp(prefix="rex-placed-specials-held-")
    catalog(work, SPECIALS, rows=[dict(row, _source="zurg") if row.get("_source") else row for row in SPECIALS["rows"]])
    held = {row["ratingKey"] for row in SPECIALS["rows"] if row.get("type") == "episode"}
    server = start(work, bytes_port)
    try:
        trash = next(row["ratingKey"] for row in SPECIALS["rows"] if row.get("_sid") == special("s01e11_trash")["id"])
        blank.wait_for(server, trash, lambda row: str(row.get("guid", "")).startswith("plex://episode/"), 60)
        keys = check_specials("rows held as `Episode N`, after a start", server, held=True)
        check("rows held as `Episode N`: each is named on its own key", set(keys.values()) == held, (keys, held))
        status, body = server.feed("POST", "items", {"items": [special(name) for name in sorted(SPECIALS["pushes"])]})
        check("zurg's next push of them is taken", status == 200 and not body.get("rejected"), (status, body))
        check("and keeps every key", check_specials("rows held as `Episode N`, after zurg's next push", server, held=True) == keys)
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)


def main():
    source = Bytes()
    for fixture in (ANIME, TILES, EVA, SPECIALS):
        for pushed in fixture["pushes"].values():
            source.files.add("/dav/" + pushed["path"])
    bytes_port = serve(source.handler())
    live(bytes_port)
    retried(bytes_port)
    films(bytes_port)
    specials(bytes_port)
    declared_moves(bytes_port)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
