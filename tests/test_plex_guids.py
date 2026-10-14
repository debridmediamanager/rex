#!/usr/bin/env python3
"""The catalog's guids against Plex's own metadata agent. No server, no network.

Cycle 70: the native Plex for Android app never loads a detail page's actions or a
show's Seasons for an item whose `plex://` guid plex.tv does not know -- and rex
minted every guid from a hash. `apply_plex_guids` (`rust/src/metadata.rs`) relinks a
built catalog to the agent's guids; this drives it, through the native CLI's
`provider-guids` test adapter, from `tests/fixtures/provider/matches.json`, the agent's
real answers (per-account `userState` stripped) for a movie match, a show match, the
show's seasons and its first season's episodes. The rows are the live catalog's own
shape: Big Buck Bunny, and The Mentalist S1E5 and S1E7.

    REX_NATIVE_BIN=rust/target/release/rex python3 tests/test_plex_guids.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.environ.get("REX_NATIVE_BIN",
                     os.path.join(os.path.dirname(HERE), "rust", "target", "release", "rex"))

RECORDED = json.load(open(os.path.join(HERE, "fixtures", "provider", "matches.json")))


def apply_plex_guids(items, recorded):
    """The native relink of `items` against the agent answers in `recorded`: (count, rows).

    A request `recorded` does not hold fails as an unreachable agent does."""
    with tempfile.TemporaryDirectory(prefix="rex-plex-guids-") as directory:
        paths = []
        for name, value in (("items", items), ("recorded", recorded)):
            paths.append(os.path.join(directory, name + ".json"))
            with open(paths[-1], "w", encoding="utf-8") as handle:
                json.dump(value, handle)
        result = subprocess.run([BIN, "provider-guids", "--items", paths[0], "--recorded", paths[1]],
                                capture_output=True, text=True, check=True)
    answer = json.loads(result.stdout)
    return answer["count"], answer["items"]


def rows():
    minted_show = "plex://show/4de6e0f217f5cdfb7eb89788"
    minted_season = "plex://season/92c7f339fb234cb5398ab286"
    return [
        {"ratingKey": "1001", "type": "movie", "title": "Big Buck Bunny", "slug": "big-buck-bunny",
         "guid": "plex://movie/c18b41265ec9c09e0042e95d",
         "Guid": [{"id": "imdb://tt1254207"}, {"id": "tmdb://10378"}]},
        {"ratingKey": "20001", "type": "show", "title": "The Mentalist", "slug": "the-mentalist",
         "guid": minted_show, "Guid": [{"id": "imdb://tt1196946"}, {"id": "tmdb://5920"}]},
        {"ratingKey": "30001", "type": "season", "index": 1, "parentRatingKey": "20001",
         "guid": minted_season, "parentGuid": minted_show, "parentSlug": "the-mentalist"},
        {"ratingKey": "1004", "type": "episode", "index": 5, "parentRatingKey": "30001",
         "grandparentRatingKey": "20001", "guid": "plex://episode/ccc53c23a5a47bbd876264ab",
         "parentGuid": minted_season, "grandparentGuid": minted_show,
         "grandparentSlug": "the-mentalist"},
        {"ratingKey": "1003", "type": "episode", "index": 7, "parentRatingKey": "30001",
         "grandparentRatingKey": "20001", "guid": "plex://episode/c3a3b9f8290fbeeefb6fc84d",
         "parentGuid": minted_season, "grandparentGuid": minted_show,
         "grandparentSlug": "the-mentalist"},
    ]


def main():
    checks = []

    def check(name, got, want):
        checks.append((name, got == want, got, want))

    count, items = apply_plex_guids(rows(), RECORDED)
    movie, show, season, ep5, ep7 = items

    # The agent's answers, read straight out of the fixture rather than restated.
    by_path = {(e["method"], e["path"], json.dumps(e["body"])): e["response"] for e in RECORDED}
    real_movie = by_path[("POST", "/library/metadata/matches", json.dumps({"type": 1, "guid": "tmdb://10378"}))]["MediaContainer"]["Metadata"][0]
    real_show = by_path[("POST", "/library/metadata/matches", json.dumps({"type": 2, "guid": "tmdb://5920"}))]["MediaContainer"]["Metadata"][0]
    show_id = real_show["guid"].rsplit("/", 1)[-1]
    seasons = {r["index"]: r for r in by_path[("GET", "/library/metadata/%s/children" % show_id, "null")]["MediaContainer"]["Metadata"]}
    season_id = seasons[1]["guid"].rsplit("/", 1)[-1]
    episodes = {r["index"]: r for r in by_path[("GET", "/library/metadata/%s/children" % season_id, "null")]["MediaContainer"]["Metadata"]}

    check("movie guid", movie["guid"], real_movie["guid"])
    check("movie slug", movie["slug"], real_movie["slug"])
    check("show guid", show["guid"], real_show["guid"])
    check("show slug", show["slug"], real_show["slug"])
    check("season guid (index 1)", season["guid"], seasons[1]["guid"])
    check("season parentGuid", season["parentGuid"], real_show["guid"])
    check("episode 5 guid", ep5["guid"], episodes[5]["guid"])
    check("episode 7 guid", ep7["guid"], episodes[7]["guid"])
    check("episode parentGuid", ep5["parentGuid"], seasons[1]["guid"])
    check("episode grandparentGuid", ep7["grandparentGuid"], real_show["guid"])
    check("rows relinked", count, 5)

    # Offline, or an id the agent does not know, leaves the minted guid alone.
    _count, offline = apply_plex_guids(rows(), [])
    check("offline keeps the minted guid", offline[1]["guid"], rows()[1]["guid"])

    for name, ok, got, want in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else "  got %r want %r" % (got, want)))
    passed = sum(1 for c in checks if c[1])
    print("%d/%d checks passed" % (passed, len(checks)))
    sys.exit(0 if passed == len(checks) else 1)


if __name__ == "__main__":
    main()
