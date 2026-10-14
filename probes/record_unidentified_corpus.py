#!/usr/bin/env python3
"""Record the provider answers the identifier reads for releases a rex library left on the
blank tile, or top up a corpus with the answers it lacks; both replayed offline by
`rust/tests/identify_corpus.rs`.

    TMDB_KEY=... PLEX_TOKEN=... python3 probes/record_unidentified_corpus.py RELEASES.json OUT.json
    TMDB_KEY=... PLEX_TOKEN=... python3 probes/record_unidentified_corpus.py --top-up CORPUS.json

RELEASES.json is `[{name, hash, known, tmdb, files, kind, truth}]`: a release as its source
pushed it -- the IMDb id (`known`) or TMDB id (`tmdb`) zurg sent, and the library `kind` the
feed files it in -- with `truth`, the TMDB id it is, set by hand. Each is identified by the
native identifier as the feed identifies it (`name_release`; `probes/identify_replay.py`).

A top-up replays CORPUS.json the way its test does and asks the providers only for what the
recording lacks, so every answer recorded before stays as it was: a change that asks one
more question records that answer without re-recording the corpus around it. A top-up that
asks nothing leaves the file untouched.
"""
import json
import sys

from identify_replay import Live, replay, write

FIND_KINDS = ("movie_results", "tv_results", "tv_episode_results", "tv_season_results")


def trim_tmdb(path, answer):
    """The fields the identifier reads. A lookup that failed or found nothing is `{}`."""
    if not answer:
        return {}
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
        return {"MediaContainer": {"Metadata": [
            {"index": row.get("index"), "guid": row.get("guid")} for row in rows]}}
    return {"MediaContainer": {"Metadata": [
        {"guid": row.get("guid"), "title": row.get("title"), "year": row.get("year"),
         "Guid": [{"id": g.get("id")} for g in row.get("Guid") or []],
         "thumb": row.get("thumb"), "art": row.get("art")}
        for row in rows[:1]]}}


def main():
    args = sys.argv[1:]
    out = args[1]
    if args[0] == "--top-up":
        corpus = json.load(open(out))
    else:
        corpus = {"source": "the production rex's movies and shows on the blank tile, 2026-10-12",
                  "torrents": json.load(open(args[0])), "tmdb": {}, "plex": {}}
    identities, tmdb_asked, plex_asked = replay("release", corpus, Live(), trim_tmdb, trim_plex)
    if args[0] != "--top-up" or tmdb_asked or plex_asked:
        write(corpus, out)
    right = sum(1 for t, i in zip(corpus["torrents"], identities)
                if t.get("truth") is not None and i["tmdb"] == t["truth"])
    print("%s: asked TMDB %d and Plex %d more; %d of %d labelled named right"
          % (out, tmdb_asked, plex_asked, right,
             sum(1 for t in corpus["torrents"] if t.get("truth") is not None)))
    for t, i in zip(corpus["torrents"], identities):
        print("  %-8s %-70s -> %s %s %s" % (t.get("truth"), t["name"][:70], i["kind"], i["tmdb"], i["source"]))


if __name__ == "__main__":
    main()
