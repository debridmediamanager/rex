#!/usr/bin/env python3
"""What rex drew the blank tile for gets named, and what a person matched stays theirs.

the production rex held 56 films and 45 shows with no poster on 2026-10-12, every one unidentified.
Their releases, as zurg pushed them, are `fixtures/feed/blank_tiles.json` with the TMDB and
Plex answers the native server asked for them:

* zurg's Usenet library filed anime episodes under movies (`Detektiv.Conan.E0221…`), each a
  film nothing matched: it is an episode of a show now;
* DMM's id for Knock at the Cabin (tt15679400) is one TMDB's /find does not know and Plex
  lists beside TMDB's 631842; DMM's for A Serbian Film is Plex's "Film Postcards: Serbia";
* rows already held that way -- the Conan episode as a film, Boy Meets World on DMM's id for
  an episode, Zoku Owarimonogatari and Wallace & Gromit known only to Plex -- are named again
  by the retry a start runs, on their own keys, and one nothing names waits for its next try;
* a Fix Match was undone by the next push that named other ids, and a match that merged two
  rows lost the absorbed release, which came back as a row of its own.

Record the answers again (asks TMDB and Plex; no key is written, TMDB's cache keys hold
`KEY` and Plex's none): `REX_RECORD_ANSWERS=1 TMDB_KEY=... PLEX_TOKEN=... python3 tests/test_feed_blank_tiles.py`.

    python3 tests/test_feed_blank_tiles.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse

from test_feed import ENTRIES, LEGACY, MOUNT, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_PATH = os.path.join(HERE, "fixtures", "feed", "blank_tiles.json")
FIXTURE = json.load(open(FIXTURE_PATH))
RECORD = os.environ.get("REX_RECORD_ANSWERS") == "1"
# A real file's ffprobe: the names are what is under test, and a row needs a probe to show.
PROBE = ENTRIES["movie"]["entry"]["media_info"]
LOCAL = ("local://", "tv.plex.agents.none://")


def item(name, ids=None):
    pushed = FIXTURE["pushes"][name]
    path = pushed["path"]
    return {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": pushed["name"],
            "release": "/".join(path.split("/")[:2]), "size": pushed["size"], "hash": pushed["hash"],
            "ids": pushed["ids"] if ids is None else ids, "media_info": PROBE}


def catalog(work, rows=()):
    # the production server's show library also holds zurg's `anime` directory, where Zoku Owarimonogatari is.
    sections = json.loads(json.dumps(LEGACY["sections"]))
    next(section for section in sections if section["type"] == "show")["Location"].append({"id": 3, "path": "/zurg/anime"})
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fb", "friendlyName": "rex-blank",
               "sections": sections, "items": list(rows)}, open(os.path.join(work, "catalog.json"), "w"))
    for name in ("tmdb", "plex"):
        json.dump({} if RECORD else FIXTURE[name], open(os.path.join(work, name + "_cache.json"), "w"))


def start(work, bytes_port):
    keys = {"TMDB_KEY": os.environ["TMDB_KEY"], "PLEX_TOKEN": os.environ["PLEX_TOKEN"]} if RECORD else None
    return Server(work, bytes_port, keys=keys)


def trimmed(key, answer):
    """An answer kept to what rex reads of it: a TMDB season's own fields, not its episodes
    (Case Closed's first season lists over a thousand), and a Plex children list's indexes
    and guids."""
    if "/season/" in key and "/episode/" not in key:
        return {field: value for field, value in answer.items() if field != "episodes"}
    if key.startswith("GET ") and "/children" in key:
        rows = (answer.get("MediaContainer") or {}).get("Metadata") or []
        return dict(answer, MediaContainer={"Metadata": [{"index": row.get("index"), "guid": row.get("guid")}
                                                          for row in rows]})
    return answer


def stop(server, work):
    server.stop()
    if RECORD:
        for name in ("tmdb", "plex"):
            with sqlite3.connect(os.path.join(work, name + "_cache.db")) as db:
                FIXTURE[name].update({key: trimmed(key, json.loads(value))
                                      for key, value in db.execute("SELECT key, value FROM cache")})


def poster(server, key):
    """The image a row's poster reads, as `/posters` lists the stored one."""
    listed = server.rows("/library/metadata/%s/posters" % key)
    return next((row.get("key") for row in listed if row.get("provider") == "tmdb"), None)


def guids(row):
    return [guid.get("id") for guid in row.get("Guid") or []]


def wait_for(server, key, until, seconds=90):
    """A start retries the blank rows off the request path: the row once it shows."""
    deadline = time.time() + seconds
    row = server.row(key)
    while not until(row) and time.time() < deadline:
        time.sleep(0.1)
        row = server.row(key)
    return row


def state_row(work, key):
    """A row as the state file keeps it, private fields and all."""
    with sqlite3.connect(os.path.join(work, "rex.db")) as db:
        found = db.execute("SELECT body FROM row WHERE coll = 'state:items' AND key = ?", (key,)).fetchone()
    return json.loads(found[0]) if found else {}


def match(server, key, title, year):
    status, body = server.request("/library/metadata/%s/matches?manual=1&title=%s&year=%s"
                                  % (key, urllib.parse.quote(title), year))
    found = ((body or {}).get("MediaContainer") or {}).get("SearchResult") or [] if status == 200 else []
    candidate = next((row for row in found if row.get("type") == "movie"), {})
    status, _ = server.request("/library/metadata/%s/match?guid=%s&name=%s&year=%s"
                               % (key, urllib.parse.quote(candidate.get("guid", ""), safe=""),
                                  urllib.parse.quote(candidate.get("name", "")), candidate.get("year", "")), "PUT")
    return status, candidate


def pushes(bytes_port):
    work = tempfile.mkdtemp(prefix="rex-blank-")
    catalog(work)
    server = start(work, bytes_port)
    try:
        # -- an episode zurg filed under movies is an episode ------------------------------
        status, body = server.feed("POST", "items", {"items": [item("conan")]})
        row = server.row(server.keys()[item("conan")["id"]])
        show = server.row(row.get("grandparentRatingKey")) if row.get("grandparentRatingKey") else {}
        check("Detektiv.Conan.E0221, filed under movies, is episode 221 in the show library",
              status == 200 and row.get("type") == "episode" and str(row.get("librarySectionID")) == "2"
              and row.get("index") == 221, (body, row.get("type"), row.get("librarySectionID"), row.get("index")))
        check("of the show TMDB and Plex both name (Case Closed, 30983), with its poster",
              "tmdb://30983" in guids(show) and poster(server, show.get("ratingKey")) is not None,
              (show.get("title"), guids(show), poster(server, show.get("ratingKey"))))

        # -- ids TMDB does not know ----------------------------------------------------------
        server.feed("POST", "items", {"items": [item("knock"), item("serbian")]})
        keys = server.keys()
        knock, serbian = server.row(keys[item("knock")["id"]]), server.row(keys[item("serbian")["id"]])
        check("DMM's tt15679400 names Knock at the Cabin through Plex's TMDB id, with TMDB's poster",
              "tmdb://631842" in guids(knock) and poster(server, knock.get("ratingKey")) is not None,
              (knock.get("title"), guids(knock), poster(server, knock.get("ratingKey"))))
        check("DMM's tt6257382 (Plex's \"Film Postcards: Serbia\") gives way to the name: A Serbian Film",
              "tmdb://73861" in guids(serbian) and serbian.get("title") == "A Serbian Film",
              (serbian.get("title"), guids(serbian)))

        # -- a person's match is theirs ------------------------------------------------------
        key = serbian.get("ratingKey")
        status, chosen = match(server, key, "Elementos", 2023)
        matched = server.row(key)
        check("a person Fix Matches it to another film", status == 200 and chosen.get("guid")
              and matched.get("guid") == chosen["guid"], (status, chosen, matched.get("guid")))
        server.feed("POST", "items", {"items": [item("serbian", {"imdb": "tt0133093"})]})
        after = server.row(key)
        check("a later push naming other ids leaves the match as the person made it",
              after.get("guid") == chosen.get("guid") and after.get("title") == matched.get("title"),
              (after.get("guid"), after.get("title"), chosen.get("guid")))

        server.feed("POST", "items", {"items": [item("elementos")]})
        other = server.keys()[item("elementos")["id"]]
        status, _ = match(server, other, "Elementos", 2023)
        merged = server.keys()
        check("matching a second row to the same title merges it into the first",
              status == 200 and merged.get(item("elementos")["id"]) == key and server.row(other) == {},
              (status, merged.get(item("elementos")["id"]), key))
        before = set(merged.values())
        server.feed("POST", "items", {"items": [item("elementos")]})
        check("and the absorbed release's next push finds it there, not a row of its own",
              server.keys().get(item("elementos")["id"]) == key and set(server.keys().values()) == before,
              (server.keys().get(item("elementos")["id"]), set(server.keys().values()) - before))

        # -- Unmatch hands the row back ------------------------------------------------------
        knock_key = knock.get("ratingKey")
        match(server, knock_key, "Knock at the Cabin", 2023)
        status, _ = server.request("/library/metadata/%s/unmatch" % knock_key, "PUT")
        server.feed("POST", "items", {"items": [item("knock", {"imdb": "tt8272064"})]})
        check("after Unmatch a push naming other ids identifies the row again",
              status == 200 and "tmdb://631842" in guids(server.row(knock_key)),
              (status, server.row(knock_key).get("guid"), guids(server.row(knock_key))))
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)


def retry(bytes_port):
    rows = [dict(row, _source="zurg") if row.get("_source") else row for row in FIXTURE["rows"]]
    by_key = {row["ratingKey"]: row for row in rows}
    conan = next(row for row in rows if row["type"] == "movie")["ratingKey"]
    boy = next(row for row in rows if row.get("title") == "Boy Meets World")["ratingKey"]
    zoku = next(row for row in rows if row.get("title") == "Zoku Owarimonogatari")["ratingKey"]
    sweeney = item("sweeney")
    rows.append(dict(by_key[conan], ratingKey="199999", key="/library/metadata/199999", guid="local://199999",
                     title=FIXTURE["pushes"]["sweeney"]["name"], _sid=sweeney["id"], _path=sweeney["path"],
                     _release=sweeney["name"], _hash=sweeney["hash"], _ids={}))

    work = tempfile.mkdtemp(prefix="rex-blank-")
    catalog(work, rows)
    server = start(work, bytes_port)
    try:
        row = wait_for(server, conan, lambda row: row.get("type") == "episode")
        show = server.row(row.get("grandparentRatingKey")) if row.get("grandparentRatingKey") else {}
        check("the production rex's Conan film is retried at start into an episode, on its own key",
              row.get("type") == "episode" and str(row.get("librarySectionID")) == "2"
              and poster(server, show.get("ratingKey")) is not None,
              (row.get("type"), row.get("librarySectionID"), show.get("title")))
        row = wait_for(server, boy, lambda row: not str(row.get("guid", "")).startswith(LOCAL))
        check("Boy Meets World, held on DMM's id for an episode, is named in place with its poster",
              "tmdb://1777" in guids(row) and poster(server, boy) is not None,
              (row.get("guid"), guids(row), poster(server, boy)))
        row = wait_for(server, zoku, lambda row: poster(server, zoku) is not None)
        plex_thumb = next(answer["MediaContainer"]["Metadata"][0].get("thumb") for key, answer in FIXTURE["plex"].items()
                          if "Zoku Owarimonogatari" in key and answer.get("MediaContainer", {}).get("Metadata"))
        check("Zoku Owarimonogatari, known only to Plex, draws Plex's own poster",
              plex_thumb and poster(server, zoku) == plex_thumb, (poster(server, zoku), plex_thumb))
        # Each season of a show draws its poster too, whichever season's episodes gave the show its own
        # (a rehearsal on the production rex's rows left Wallace & Gromit's Specials blank).
        wallace = next(row for row in rows if row.get("title") == "Wallace & Gromit")["ratingKey"]
        wait_for(server, wallace, lambda row: poster(server, wallace) is not None)
        seasons = server.rows("/library/metadata/%s/children" % wallace)
        check("Wallace & Gromit, known only to Plex, draws its poster on the show and both its seasons",
              poster(server, wallace) is not None and len(seasons) == 2
              and all(poster(server, season["ratingKey"]) is not None for season in seasons),
              (poster(server, wallace), [(season.get("title"), poster(server, season["ratingKey"])) for season in seasons]))
    finally:
        stop(server, work)
    retried = state_row(work, "199999")
    check("a release nothing names waits for its next try, an hour on",
          retried.get("_retry", {}).get("n") == 1 and retried["_retry"]["at"] - time.time() > 3000,
          retried.get("_retry"))
    shutil.rmtree(work, ignore_errors=True)

    # -- what a person matched is not retried ----------------------------------------------------
    work = tempfile.mkdtemp(prefix="rex-blank-")
    catalog(work, [dict(row, _matched=True) if row["ratingKey"] == boy else row for row in rows])
    server = start(work, bytes_port)
    try:
        wait_for(server, zoku, lambda row: poster(server, zoku) is not None)
        check("a show a person matched keeps what they matched it to through the retry",
              server.row(boy).get("guid") == by_key[boy]["guid"], server.row(boy).get("guid"))
        # And through a push naming its episode by other ids: the episode was identified again
        # and left the person's show, or took the show's identity with it (`reidentify_show`).
        episode = next(row for row in rows if row.get("grandparentRatingKey") == boy and row["type"] == "episode")
        server.feed("POST", "items", {"items": [{
            "id": episode["_sid"], "path": episode["_path"], "file": episode["Media"][0]["Part"][0]["file"],
            "name": episode["_release"], "size": episode["Media"][0]["Part"][0]["size"], "hash": episode.get("_hash"),
            "ids": {"imdb": "tt0105958"}, "media_info": PROBE}]})
        after = server.row(episode["ratingKey"])
        check("an episode of a show a person matched stays under it when a push names other ids",
              after.get("grandparentRatingKey") == boy and server.row(boy).get("guid") == by_key[boy]["guid"],
              (after.get("grandparentRatingKey"), server.row(boy).get("guid")))
    finally:
        stop(server, work)
        shutil.rmtree(work, ignore_errors=True)


def main():
    source = Bytes()
    for name in FIXTURE["pushes"]:
        source.files.add("/dav/" + FIXTURE["pushes"][name]["path"])
    bytes_port = serve(source.handler())
    pushes(bytes_port)
    retry(bytes_port)
    if RECORD:
        json.dump(FIXTURE, open(FIXTURE_PATH, "w"), indent=1, sort_keys=True)
        print("recorded %d TMDB and %d Plex answers" % (len(FIXTURE["tmdb"]), len(FIXTURE["plex"])))
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
