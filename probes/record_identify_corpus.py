#!/usr/bin/env python3
"""Record the identification corpus `tests/test_identify.py` replays offline.

    TMDB_KEY=... PLEX_TOKEN=... python3 probes/record_identify_corpus.py \\
        RD_TORRENTS.json DMM_LABELS.tsv OUT.json [N]

RD_TORRENTS.json is Real-Debrid's `GET /torrents` (name and hash of every torrent);
DMM_LABELS.tsv is `hash<TAB>imdbId[,imdbId]` from DMM's hash tables, the answer key.
N labelled torrents (seed 7, one-label hashes only) are identified by name alone --
the DMM step is left out, since it is where the labels come from -- and every provider
answer the identifier read is kept, trimmed to the fields it reads, together with the
old builder's answer (file name to TMDB's first hit) for comparison.
"""
import json
import os
import random
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "oracle"))
import identify  # noqa: E402
import metadata  # noqa: E402


def key(path, params):
    return json.dumps([path, sorted(params.items())])


class RecordingTmdb(metadata.Tmdb):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.record = {}

    def _get(self, path, **params):
        answer = super()._get(path, **params) or {}
        if path.startswith("search/"):
            trimmed = {"results": [{"id": r["id"]} for r in (answer.get("results") or [])[:5]]}
        elif path.startswith("find/"):
            trimmed = {kind: [{"id": r["id"]} for r in answer.get(kind) or []][:3]
                       for kind in ("movie_results", "tv_results")}
        else:
            trimmed = {"imdb_id": answer.get("imdb_id"),
                       "external_ids": {"imdb_id": (answer.get("external_ids") or {}).get("imdb_id")}}
        self.record[key(path, params)] = trimmed
        return trimmed


def main():
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

    # The full provider answers stay outside the repository; only the trimmed ones
    # the identifier read are written to OUT.
    cache = os.environ.get("RECORD_CACHE") or tempfile.mkdtemp(prefix="identify-record-")
    os.makedirs(cache, exist_ok=True)
    tmdb = RecordingTmdb(os.environ["TMDB_KEY"], os.path.join(cache, "tmdb.json"))
    plex_record = {}
    plex = metadata.PlexProvider(os.environ["PLEX_TOKEN"], os.path.join(cache, "plex.json"))
    real_call = plex._call

    def recording_call(method, path, body=None):
        answer = real_call(method, path, body) or {}
        rows = (answer.get("MediaContainer") or {}).get("Metadata") or []
        trimmed = {"MediaContainer": {"Metadata": [
            {"guid": row.get("guid"), "title": row.get("title"), "year": row.get("year"),
             "Guid": [{"id": g.get("id")} for g in row.get("Guid") or []]}
            for row in rows[:1]]}}
        plex_record[json.dumps([method, path, body], sort_keys=True)] = trimmed
        return trimmed

    plex._call = recording_call
    identifier = identify.Identifier(tmdb=tmdb, plex=plex)
    corpus = []
    for h in known[:count]:
        torrent = torrents[h]
        files = len(torrent.get("links") or [])
        identity = identifier.identify(torrent["filename"], files=files)
        # The old builder: metadata.parse_release on the name, TMDB's first hit.
        old_kind, old_id = tmdb.identify(metadata.parse_release(torrent["filename"]))
        old_imdb = None
        if old_id:
            details = tmdb.details(old_kind, old_id) or {}
            old_imdb = (details.get("external_ids") or {}).get("imdb_id") or details.get("imdb_id")
        corpus.append({"name": torrent["filename"], "hash": h, "truth": labels[h], "files": files,
                       "old": old_imdb, "recorded": identity.get("imdb")})
    json.dump({"source": "Real-Debrid library and DMM labels, 2026-10-06",
               "torrents": corpus, "tmdb": tmdb.record, "plex": plex_record},
              open(out_path, "w"), indent=1, sort_keys=True)
    right = sum(1 for t in corpus if t["recorded"] == t["truth"])
    old = sum(1 for t in corpus if t["old"] == t["truth"])
    print("recorded %d: new right %d, old right %d" % (len(corpus), right, old))


if __name__ == "__main__":
    main()
