#!/usr/bin/env python3
"""Record the identification corpus `rust/tests/identify_corpus.rs` replays offline.

    TMDB_KEY=... PLEX_TOKEN=... python3 probes/record_identify_corpus.py \\
        RD_TORRENTS.json DMM_LABELS.tsv OUT.json [N]

RD_TORRENTS.json is Real-Debrid's `GET /torrents` (name and hash of every torrent);
DMM_LABELS.tsv is `hash<TAB>imdbId[,imdbId]` from DMM's hash tables, the answer key.
N labelled torrents (seed 7, one-label hashes only) are identified by name alone, by the
native identifier (`probes/identify_replay.py`) -- the DMM step is left out, since it is
where the labels come from -- and every provider answer the identifier read is kept, trimmed
to the fields it reads, together with the old builder's answer (file name to TMDB's first
hit) for comparison.

    TMDB_KEY=... PLEX_TOKEN=... python3 probes/record_identify_corpus.py --top-up CORPUS.json

replays CORPUS.json and asks the providers only what the recording lacks, so a parser
change that asks one more question records that answer and leaves the rest as recorded;
a top-up that adds nothing leaves the file untouched.
"""
import json
import random
import sys

from identify_replay import Live, native, recorded_tmdb, replay, top_up, write


def trim_tmdb(path, answer):
    answer = answer or {}
    if path.startswith("search/"):
        return {"results": [{"id": r["id"]} for r in (answer.get("results") or [])[:5]]}
    if path.startswith("find/"):
        return {kind: [{"id": r["id"]} for r in answer.get(kind) or []][:3]
                for kind in ("movie_results", "tv_results")}
    return {"imdb_id": answer.get("imdb_id"),
            "external_ids": {"imdb_id": (answer.get("external_ids") or {}).get("imdb_id")}}


def trim_plex(path, answer):
    rows = ((answer or {}).get("MediaContainer") or {}).get("Metadata") or []
    return {"MediaContainer": {"Metadata": [
        {"guid": row.get("guid"), "title": row.get("title"), "year": row.get("year"),
         "Guid": [{"id": g.get("id")} for g in row.get("Guid") or []]}
        for row in rows[:1]]}}


def old_builder(corpus, live, filename):
    """The old builder: the release parsed as `metadata.parse_release` does, TMDB's first hit."""
    parsed = native("metadata-parse", "--filename", filename)
    if not parsed["title"]:
        return None
    for kind in (("tv", "movie") if parsed["episode"] is not None else ("movie", "tv")):
        params = {"query": parsed["title"]}
        if parsed["year"] and kind == "movie":
            params["year"] = parsed["year"]
        results = recorded_tmdb(corpus, live, trim_tmdb, "search/" + kind, params).get("results") or []
        if results:
            details = recorded_tmdb(corpus, live, trim_tmdb, "%s/%s" % (kind, results[0]["id"]), {
                "append_to_response": "credits,external_ids,content_ratings,release_dates"})
            return (details.get("external_ids") or {}).get("imdb_id") or details.get("imdb_id")
    return None


def main():
    if sys.argv[1] == "--top-up":
        return top_up(sys.argv[2], "name", trim_tmdb, trim_plex)
    torrents_path, labels_path, out_path = sys.argv[1:4]
    count = int(sys.argv[4]) if len(sys.argv) > 4 else 200
    torrents = {t["hash"].lower(): t for t in json.load(open(torrents_path))}
    labels = {}
    for line in open(labels_path):
        h, ids = line.rstrip("\n").split("\t")
        if "," not in ids:
            labels[h.lower()] = ids
    known = sorted(h for h in labels if h in torrents)
    random.Random(7).shuffle(known)

    corpus = {"source": "Real-Debrid library and DMM labels, 2026-10-06",
              "torrents": [{"name": torrents[h]["filename"], "hash": h, "truth": labels[h],
                            "files": len(torrents[h].get("links") or [])} for h in known[:count]],
              "tmdb": {}, "plex": {}}
    live = Live()
    identities, _, _ = replay("name", corpus, live, trim_tmdb, trim_plex)
    for torrent, identity in zip(corpus["torrents"], identities):
        torrent["old"] = old_builder(corpus, live, torrent["name"])
        torrent["recorded"] = identity["imdb"]
    write(corpus, out_path)
    right = sum(1 for t in corpus["torrents"] if t["recorded"] == t["truth"])
    old = sum(1 for t in corpus["torrents"] if t["old"] == t["truth"])
    print("recorded %d: new right %d, old right %d" % (len(corpus["torrents"]), right, old))


if __name__ == "__main__":
    main()
