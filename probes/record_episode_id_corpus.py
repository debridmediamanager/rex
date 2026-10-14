#!/usr/bin/env python3
"""Record the corpus of shows DMM knows by an episode's IMDb id, replayed offline by
`rust/tests/identify_corpus.rs`.

    TMDB_KEY=... PLEX_TOKEN=... python3 probes/record_episode_id_corpus.py RELEASES.json OUT.json
    TMDB_KEY=... PLEX_TOKEN=... python3 probes/record_episode_id_corpus.py --top-up CORPUS.json

RELEASES.json is `[{name, hash, imdb, files}]`: one release of each show a rex library
left unmatched although its source pushed an IMDb id (the production server, 2026-10-11: 285
shows, the id DMM maps each hash to). Each is identified by that id alone, as the feed does, by
the native identifier (`probes/identify_replay.py`), and every provider answer the identifier
read is kept, trimmed to the fields it reads.
`truth` is the show TMDB's /find names for the id: an episode's or a season's show_id.
A release with no `imdb` is identified by its name, and its `truth` (a TMDB id) is
taken from RELEASES.json. A top-up asks only what CORPUS.json lacks, as the other recorders' do.
"""
import json
import sys

from identify_replay import Live, recorded_tmdb, replay, top_up, write

FIND_KINDS = ("movie_results", "tv_results", "tv_episode_results", "tv_season_results")


def trim_tmdb(path, answer):
    answer = answer or {}
    if path.startswith("search/"):
        return {"results": [{"id": r["id"]} for r in (answer.get("results") or [])[:5]]}
    if path.startswith("find/"):
        return {kind: [{k: r[k] for k in ("id", "show_id") if k in r}
                       for r in answer.get(kind) or []][:3] for kind in FIND_KINDS}
    trimmed = {"imdb_id": answer.get("imdb_id"),
               "external_ids": {"imdb_id": (answer.get("external_ids") or {}).get("imdb_id")}}
    if "number_of_seasons" in answer:
        trimmed["number_of_seasons"] = answer["number_of_seasons"]
    return trimmed


def trim_plex(path, answer):
    rows = ((answer or {}).get("MediaContainer") or {}).get("Metadata") or []
    if path.endswith("/children"):
        # A show's seasons, or a season's episodes: every row's index and guid.
        return {"MediaContainer": {"Metadata": [
            {"index": row.get("index"), "guid": row.get("guid")} for row in rows]}}
    # The provider's artwork too: the identifier reads it for a title TMDB gives none, and the
    # corpus's later answers (topped up with record_unidentified_corpus.py's rules) carry it.
    return {"MediaContainer": {"Metadata": [
        {"guid": row.get("guid"), "title": row.get("title"), "year": row.get("year"),
         "Guid": [{"id": g.get("id")} for g in row.get("Guid") or []],
         "thumb": row.get("thumb"), "art": row.get("art")}
        for row in rows[:1]]}}


def main():
    if sys.argv[1] == "--top-up":
        return top_up(sys.argv[2], "hash", trim_tmdb, trim_plex)
    releases_path, out_path = sys.argv[1:3]
    releases = json.load(open(releases_path))
    live = Live()
    corpus = {"source": "The production rex's unmatched shows and the IMDb ids DMM gave them, 2026-10-11",
              "torrents": [], "tmdb": {}, "plex": {}}
    for release in releases:
        found = recorded_tmdb(corpus, live, trim_tmdb, "find/" + release["imdb"],
                              {"external_source": "imdb_id"}) if release.get("imdb") else {}
        truth = release.get("truth")
        for kind in FIND_KINDS:
            if found.get(kind):
                row = found[kind][0]
                truth = row.get("show_id", row.get("id")) if kind != "movie_results" else None
                break
        corpus["torrents"].append({"name": release["name"], "hash": release["hash"].lower(),
                                   "known": release.get("imdb"), "files": release["files"],
                                   "truth": truth})
    identities, _, _ = replay("hash", corpus, live, trim_tmdb, trim_plex)
    for torrent, identity in zip(corpus["torrents"], identities):
        torrent["recorded"] = identity["tmdb"]
    write(corpus, out_path)
    named = sum(1 for t in corpus["torrents"] if t["truth"])
    right = sum(1 for t in corpus["torrents"] if t["truth"] and t["recorded"] == t["truth"])
    print("recorded %d: %d name a show, %d identified as it" % (len(corpus["torrents"]), named, right))


if __name__ == "__main__":
    main()
