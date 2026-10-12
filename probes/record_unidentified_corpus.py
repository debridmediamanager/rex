#!/usr/bin/env python3
"""Record the provider answers the identifier reads for releases a rex library left on the
blank tile, or top up a corpus with the answers it lacks; both replayed offline by
`tests/test_identify.py` and `rust/tests/identify_corpus.rs`.

    TMDB_KEY=... PLEX_TOKEN=... python3 probes/record_unidentified_corpus.py RELEASES.json OUT.json
    TMDB_KEY=... PLEX_TOKEN=... python3 probes/record_unidentified_corpus.py --top-up CORPUS.json

RELEASES.json is `[{name, hash, known, tmdb, files, kind, truth}]`: a release as its source
pushed it -- the IMDb id (`known`) or TMDB id (`tmdb`) zurg sent, and the library `kind` the
feed files it in -- with `truth`, the TMDB id it is, set by hand. Each is identified as
`identify.identify_release` does for the feed.

A top-up replays CORPUS.json the way its test does and asks the providers only for what the
recording lacks, so every answer recorded before stays as it was: a change that asks one
more question records that answer without re-recording the corpus around it.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "oracle"))
import identify  # noqa: E402
import metadata  # noqa: E402

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


class Tmdb(metadata.Tmdb):
    """The recording first, TMDB for what it lacks."""

    def __init__(self, record, *args):
        super().__init__(*args)
        self.record, self.asked = record, 0

    def _get(self, path, **params):
        key = json.dumps([path, sorted(params.items())])
        if key not in self.record:
            self.asked += 1
            self.record[key] = trim_tmdb(path, super()._get(path, **params))
        return self.record[key]


def plex_provider(record, token, cache):
    plex = metadata.PlexProvider(token, cache)
    live = plex._call
    plex.asked = 0

    def call(method, path, body=None):
        key = json.dumps([method, path, body], sort_keys=True)
        if key not in record:
            plex.asked += 1
            record[key] = trim_plex(path, live(method, path, body))
        return record[key]

    plex._call = call
    return plex


def identify_all(corpus, tmdb, plex):
    torrents = corpus["torrents"]
    known = {t["hash"]: t["known"] for t in torrents if t.get("known")}
    identifier = identify.Identifier(tmdb=tmdb, plex=plex, known=known)
    return [(t, identify.identify_release(identifier, t)) for t in torrents]


def main():
    args = sys.argv[1:]
    cache = os.environ.get("RECORD_CACHE") or tempfile.mkdtemp(prefix="identify-record-")
    os.makedirs(cache, exist_ok=True)
    if args[0] == "--top-up":
        out = args[1]
        corpus = json.load(open(out))
    else:
        out = args[1]
        corpus = {"source": "the production rex's movies and shows on the blank tile, 2026-10-12",
                  "torrents": json.load(open(args[0])), "tmdb": {}, "plex": {}}
    tmdb = Tmdb(corpus["tmdb"], os.environ["TMDB_KEY"], os.path.join(cache, "tmdb.json"))
    plex = plex_provider(corpus["plex"], os.environ["PLEX_TOKEN"], os.path.join(cache, "plex.json"))
    identities = identify_all(corpus, tmdb, plex)
    json.dump(corpus, open(out, "w"), indent=1, sort_keys=True)
    right = sum(1 for t, i in identities if t.get("truth") is not None and i["tmdb"] == t["truth"])
    print("%s: asked TMDB %d and Plex %d more; %d of %d labelled named right"
          % (out, tmdb.asked, plex.asked, right, sum(1 for t in corpus["torrents"] if t.get("truth") is not None)))
    for t, i in identities:
        print("  %-8s %-70s -> %s %s %s" % (t.get("truth"), t["name"][:70], i["kind"], i["tmdb"], i["source"]))


if __name__ == "__main__":
    main()
