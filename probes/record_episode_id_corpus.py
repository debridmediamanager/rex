#!/usr/bin/env python3
"""Record the corpus of shows DMM knows by an episode's IMDb id, replayed offline by
`tests/test_identify.py` and `rust/tests/identify_corpus.rs`.

    TMDB_KEY=... PLEX_TOKEN=... python3 probes/record_episode_id_corpus.py RELEASES.json OUT.json

RELEASES.json is `[{name, hash, imdb, files}]`: one release of each show a rex library
left unmatched although its source pushed an IMDb id (fun, 2026-10-11: 285 shows, the
id DMM maps each hash to). Each is identified by that id alone, as the feed does, and
every provider answer the identifier read is kept, trimmed to the fields it reads.
`truth` is the show TMDB's /find names for the id: an episode's or a season's show_id.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import identify  # noqa: E402
import metadata  # noqa: E402

FIND_KINDS = ("movie_results", "tv_results", "tv_episode_results", "tv_season_results")


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
            trimmed = {kind: [{k: r[k] for k in ("id", "show_id") if k in r}
                              for r in answer.get(kind) or []][:3] for kind in FIND_KINDS}
        else:
            trimmed = {"imdb_id": answer.get("imdb_id"),
                       "external_ids": {"imdb_id": (answer.get("external_ids") or {}).get("imdb_id")}}
        self.record[key(path, params)] = trimmed
        return trimmed


def main():
    releases_path, out_path = sys.argv[1:3]
    releases = json.load(open(releases_path))
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
    known = {r["hash"].lower(): r["imdb"] for r in releases}
    identifier = identify.Identifier(tmdb=tmdb, plex=plex, known=known)
    corpus = []
    for release in releases:
        found = tmdb._get("find/" + release["imdb"], external_source="imdb_id")
        truth = None
        for kind in FIND_KINDS:
            if found.get(kind):
                row = found[kind][0]
                truth = row.get("show_id", row.get("id")) if kind != "movie_results" else None
                break
        identity = identifier.identify(release["name"], info_hash=release["hash"],
                                       files=release["files"])
        corpus.append({"name": release["name"], "hash": release["hash"].lower(),
                       "known": release["imdb"], "files": release["files"],
                       "truth": truth, "recorded": identity.get("tmdb")})
    json.dump({"source": "fun rex's unmatched shows and the IMDb ids DMM gave them, 2026-10-11",
               "torrents": corpus, "tmdb": tmdb.record, "plex": plex_record},
              open(out_path, "w"), indent=1, sort_keys=True)
    named = sum(1 for t in corpus if t["truth"])
    right = sum(1 for t in corpus if t["truth"] and t["recorded"] == t["truth"])
    print("recorded %d: %d name a show, %d identified as it" % (len(corpus), named, right))


if __name__ == "__main__":
    main()
