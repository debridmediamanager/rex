#!/usr/bin/env python3
"""Offline Python-oracle parity checks for the native phase 2 core.

The driver invokes only the native CLI.  Python is used here as the checked-in
behavior oracle, while the Rust production library has no Python runtime
dependency and no service/socket is started.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import filters  # noqa: E402
import gate  # noqa: E402
import hierarchy  # noqa: E402
import mediainfo  # noqa: E402
import metadata  # noqa: E402
import xmlout  # noqa: E402


BIN = os.environ.get("REX_NATIVE_BIN",
                     os.path.join(ROOT, "rust", "target", "debug", "rex"))


def equivalent(left, right):
    """Compare JSON while preserving presence, type, and list order."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            equivalent(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            equivalent(a, b) for a, b in zip(left, right)
        )
    return left == right


def ordered_equivalent(left, right):
    """Compare JSON recursively, including object insertion order."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return list(left) == list(right) and all(
            ordered_equivalent(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            ordered_equivalent(a, b) for a, b in zip(left, right)
        )
    return left == right


def invoke(command, *args, document=None):
    """Run one CLI operation, writing JSON through a task-local temp file."""
    temp = None
    try:
        if document is not None:
            temp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
            json.dump(document, temp)
            temp.close()
            args = (*args, "--input", temp.name)
        result = subprocess.run(
            [BIN, command, *args],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise AssertionError(
                f"{command} exited {result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout
    finally:
        if temp is not None:
            os.unlink(temp.name)


def native_json(command, *args, document=None):
    return json.loads(invoke(command, *args, document=document))


def check_mediainfo():
    fixture_dir = os.path.join(HERE, "fixtures", "media")
    checked = 0
    fields = 0
    for name in sorted(os.listdir(fixture_dir)):
        if not name.endswith(".json"):
            continue
        probe = json.load(open(os.path.join(fixture_dir, name)))
        _, media_name, part_name = name[:-5].rsplit("-", 2)
        media_id = int(media_name)
        part_id = int(part_name)
        part_key = f"/library/parts/{media_id}/{part_id}/file.mkv"
        file_path = probe.get("format", {}).get("filename") or "file.mkv"
        size = int(float(probe.get("format", {}).get("size") or 0))
        expected = mediainfo.build_media(
            probe,
            media_id=media_id,
            part_id=part_id,
            part_key=part_key,
            file_path=file_path,
            size=size,
        )
        got = native_json(
            "mediainfo",
            "--media-id", str(media_id),
            "--part-id", str(part_id),
            "--part-key", part_key,
            "--file", file_path,
            "--size", str(size),
            document=probe,
        )
        if not equivalent(expected, got):
            raise AssertionError(f"mediainfo parity failed for {name}")
        checked += 1

        def count_values(value):
            if isinstance(value, dict):
                return sum(count_values(v) for v in value.values())
            if isinstance(value, list):
                return sum(count_values(v) for v in value)
            return 1

        fields += count_values(expected)
    # Midpoint probes exercise Python's ties-to-even round for Media/stream
    # bitrates and the fallback frame-rate token.  The checked-in corpus has
    # no exact midpoint, so this is a deliberate measured-shape mutation, not
    # a replacement fixture.
    midpoint_probe = {
        "format": {"duration": "1.5", "bit_rate": "2500", "format_name": "matroska"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920,
             "height": 1080, "profile": "High", "level": 40, "refs": 1,
             "pix_fmt": "yuv420p", "avg_frame_rate": "5/2",
             "r_frame_rate": "5/2", "index": 0, "coded_height": 1080,
             "coded_width": 1920},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2,
             "sample_rate": "48000", "index": 1, "bit_rate": "3500"},
        ],
    }
    expected = mediainfo.build_media(
        midpoint_probe, media_id=7, part_id=9, part_key="/x",
        file_path="x.mkv", size=1,
    )
    got = native_json(
        "mediainfo", "--media-id", "7", "--part-id", "9", "--part-key", "/x",
        "--file", "x.mkv", "--size", "1", document=midpoint_probe,
    )
    if not equivalent(expected, got):
        raise AssertionError("mediainfo midpoint rounding parity failed")
    checked += 1

    normalization_cases = [
        {"container": "mp4", "size": 2 ** 32 + 1,
         "Part": [{"file": "x.mp4", "container": "mp4", "size": 2 ** 32 + 1}]},
        {"container": "webm", "Part": [{"file": "x.webm", "container": "webm"}]},
        {"container": "mkv", "optimizedForStreaming": False,
         "has64bitOffsets": True, "Part": [{"file": "x.mkv", "container": "mkv",
                                              "hasThumbnail": False}]},
    ]
    for media in normalization_cases:
        expected = copy.deepcopy(media)
        mediainfo.normalize_media(expected)
        got = native_json("normalize-media", document=media)
        if not ordered_equivalent(expected, got):
            raise AssertionError("normalize_media parity failed")
        checked += 1
    return checked, fields


def check_hierarchy():
    checked = 0
    for filename in ("catalog_ui.json", "catalog_features.json"):
        document = json.load(open(os.path.join(HERE, filename)))
        leaves = [row for row in document["items"]
                  if row.get("type") in ("movie", "episode")]
        expected = hierarchy.build(copy.deepcopy(leaves))
        got = native_json("hierarchy-build", document=leaves)
        if not equivalent(expected, got):
            raise AssertionError(f"hierarchy build parity failed for {filename}")
        rows = copy.deepcopy(document["items"])
        hierarchy.normalize_rows(rows)
        got = native_json("hierarchy-normalize", document=document["items"])
        if not equivalent(rows, got):
            raise AssertionError(f"hierarchy normalize parity failed for {filename}")
        checked += len(leaves) + len(rows)
    # The measured title table and generated-season storage rules are exercised
    # independently of catalog fixture titles.
    rows = [
        {"ratingKey": "1", "type": "season", "title": "Season 4",
         "titleSort": "Season 4", "index": 4},
        {"ratingKey": "2", "type": "show", "title": "The Éxample",
         "childCount": 1},
        {"ratingKey": "3", "type": "episode", "title": "Episode 1",
         "parentRatingKey": "1", "parentTitle": "Season 4",
         "grandparentRatingKey": "2"},
    ]
    expected = copy.deepcopy(rows)
    hierarchy.normalize_rows(expected)
    got = native_json("hierarchy-normalize", document=rows)
    if not equivalent(expected, got):
        raise AssertionError("hierarchy generated-title parity failed")
    # Preserve the exact measured Unicode sweep from test_catalog_features.py,
    # rather than reducing title folding to a representative accented sample.
    mark_domain = [
        codepoint
        for start, end in ((0x0300, 0x1FFF), (0x3000, 0x10FFFF))
        for codepoint in range(start, end + 1)
        if unicodedata.category(chr(codepoint)).startswith(("P", "S", "Z", "M"))
        or unicodedata.category(chr(codepoint)) == "Cf"
    ]
    fold_input = ["A" + chr(codepoint) + "B" for codepoint in mark_domain]
    expected_fold = [hierarchy.fold_title(value) for value in fold_input]
    got_fold = native_json("hierarchy-fold", document=fold_input)
    if expected_fold != got_fold:
        raise AssertionError("hierarchy title-fold Unicode sweep parity failed")
    case_input = ["Éclair", "İstanbul", "ß", "ſ", "K", "ẞ"]
    expected_case = [hierarchy.fold_case(value) for value in case_input]
    got_case = native_json("hierarchy-case", document=case_input)
    if expected_case != got_case:
        raise AssertionError("hierarchy case-fold Unicode parity failed")

    year_rows = [
        {"ratingKey": "9001", "type": "show", "title": "Load Show",
         "year": 2008, "guid": "plex://show/aaaaaaaaaaaaaaaaaaaaaaaa",
         "key": "/library/metadata/9001/children", "librarySectionID": 2},
        {"ratingKey": "9002", "type": "season", "title": "Season 1",
         "index": 1, "year": 2009, "parentRatingKey": "9001",
         "parentTitle": "Load Show",
         "guid": "plex://season/bbbbbbbbbbbbbbbbbbbbbbbb",
         "key": "/library/metadata/9002/children", "librarySectionID": 2},
    ]
    expected_year = copy.deepcopy(year_rows)
    hierarchy.normalize_rows(expected_year)
    got_year = native_json("hierarchy-normalize", document=year_rows)
    if not equivalent(expected_year, got_year):
        raise AssertionError("hierarchy season-year parity failed")

    specials = json.load(open(os.path.join(HERE, "fixtures", "specials_zen.json")))
    leaves = []
    for number, show in enumerate(specials["shows"]):
        for season in show["seasons"]:
            leaves.append({
                "ratingKey": str(46000 + len(leaves)),
                "title": "Leaf %d" % len(leaves),
                "_kind": "tv",
                "_tmdb_id": 79000 + number,
                "_show_title": show["title"],
                "_season": season["index"],
                "_episode": 1,
                "addedAt": 1,
                "updatedAt": 1,
            })
    expected_specials = hierarchy.build(copy.deepcopy(leaves))
    got_specials = native_json("hierarchy-build", document=leaves)
    if not equivalent(expected_specials, got_specials):
        raise AssertionError("hierarchy Specials build parity failed")
    legacy_specials = copy.deepcopy(expected_specials)
    for row in legacy_specials:
        if row.get("type") == "season" and row.get("index") == 0:
            row["title"] = "Season 0"
        if row.get("type") == "episode" and row.get("parentIndex") == 0:
            row["parentTitle"] = "Season 0"
    hierarchy.normalize_rows(legacy_specials)
    got_legacy_specials = native_json(
        "hierarchy-normalize", document=legacy_specials
    )
    if not equivalent(legacy_specials, got_legacy_specials):
        raise AssertionError("hierarchy legacy Specials parity failed")
    return checked + len(rows) + 5


def check_xml():
    checked = 0
    for filename in ("catalog_ui.json", "catalog_features.json"):
        document = json.load(open(os.path.join(HERE, filename)))
        for row in document["items"]:
            value = {"MediaContainer": {"size": 1, "Metadata": [row]}}
            expected = xmlout.to_xml(value).decode()
            got = invoke("xml", document=value)
            if expected != got:
                raise AssertionError(
                    f"XML parity failed for {filename} ratingKey={row.get('ratingKey')}"
                )
            expected_ordered = xmlout.ordered(value)
            got_ordered = native_json("ordered", document=value)
            if not ordered_equivalent(expected_ordered, got_ordered):
                raise AssertionError(
                    f"ordered parity failed for {filename} ratingKey={row.get('ratingKey')}"
                )
            checked += 2
    system_values = [
        {"MediaContainer": {}},
        {"MediaContainer": {
            "size": 2,
            "totalSize": 2,
            "offset": 0,
            "Metadata": [
                {"type": "movie", "ratingKey": "1", "title": "A&B"},
                {"type": "episode", "ratingKey": "2", "title": "É"},
            ],
        }},
        {"Query": {"type": 1, "Rule": {
            "field": "title", "operator": "=", "value": "x"
        }}},
    ]
    for value in system_values:
        expected = xmlout.to_system_xml(value).decode()
        got = invoke("system-xml", document=value)
        if expected != got:
            raise AssertionError("system XML parity failed")
        checked += 1
    return checked


def check_metadata():
    names = [
        "The.Mandalorian.S01E05.1080p.WEB-DL.mkv",
        "Detective Conan - 1194.mkv",
        "Big.Buck.Bunny.2008.4K.webm",
        "Show.Name.S02E03.2024.mkv",
        "foo [1080p] bar.mkv",
        "The.Movie.2023.1080p.mkv",
        "Maxwell.mkv",
        "Constantine.mkv",
        "Film.1899",
        "MovieS01E01.mkv",
        "Detective Conan - 1194 1080p.mkv",
    ]
    for name in names:
        expected = metadata.parse_release(name)
        got = native_json("metadata-parse", "--filename", name)
        if expected != got:
            raise AssertionError(f"release parse parity failed for {name}")

    class FakeTmdb:
        def __init__(self, details, episode=None, season=None):
            self.details_value = details
            self.episode_value = episode
            self.season_value = season

        def identify(self, _parsed):
            return "tv", 123

        def details(self, _kind, _tmdb_id):
            return self.details_value

        def episode(self, *_args):
            return self.episode_value

        def season(self, *_args):
            return self.season_value

    details = {
        "name": "The Example",
        "first_air_date": "2020-02-03",
        "overview": "show summary",
        "tagline": "line",
        "vote_average": 8.26,
        "external_ids": {"imdb_id": "tt123"},
        "poster_path": "/poster.jpg",
        "backdrop_path": "/back.jpg",
        "content_ratings": {"results": [
            {"iso_3166_1": "US", "rating": "TV-14"}
        ]},
        "networks": [{"name": "Net"}],
        "genres": [{"name": "Drama"}],
        "production_countries": [
            {"name": "USA"}, {"name": "Canada"}, {"name": "Third"}
        ],
        "credits": {
            "cast": [{"name": "Actor", "character": "Self",
                      "profile_path": "/a.jpg"}, {"name": "Actor"}],
            "crew": [{"name": "Dir", "job": "Director"},
                     {"name": "Writer", "department": "Writing"}],
        },
    }
    episode = {
        "name": "Ep title",
        "overview": "ep summary",
        "air_date": "2021-03-04",
        "still_path": "/still.jpg",
        "id": 987,
        "external_ids": {"imdb_id": "tt987", "tvdb_id": "tv987"},
    }
    season = {
        "id": 456,
        "poster_path": "/season.jpg",
        "air_date": "2020-02-03",
        "overview": "season summary",
        "external_ids": {"tvdb_id": "tv456"},
    }
    parsed = {"title": "The Example", "year": None, "season": 1, "episode": 2}
    expected = {"summary": "old"}
    python_row = copy.deepcopy(expected)
    metadata.enrich(
        python_row,
        FakeTmdb(details, episode, season),
        parsed,
        chosen=("tv", 123),
    )
    got = native_json(
        "metadata-enrich",
        document={
            "row": expected,
            "details": details,
            "parsed": parsed,
            "episode": episode,
            "season": season,
            "kind": "tv",
            "tmdbId": 123,
        },
    )
    if not got["ok"] or not equivalent(python_row, got["row"]):
        raise AssertionError("metadata enrichment parity failed")

    extra_enrichments = [
        {
            "row": {"summary": "kept"},
            "details": {
                "title": "", "name": "", "release_date": "",
                "first_air_date": "2022-04-05", "overview": "",
                "imdb_id": "tt-top", "vote_average": 8.25,
                "production_companies": [], "networks": [{"name": "Net"}],
                "genres": [{"name": "Drama"}],
                "production_countries": [{"name": "USA"}],
                "credits": {"cast": [{"name": None}, {"name": None}]},
            },
            "parsed": {"title": "Parsed fallback", "season": None, "episode": None},
            "episode": None, "season": None, "kind": "movie", "tmdbId": 321,
        },
        {
            "row": {"summary": "old"},
            "details": {
                "name": "Special Show", "first_air_date": "İ2020",
                "external_ids": {"tvdb_id": 123}, "genres": [],
                "overview": 0,
            },
            # Season zero is a Specials season. Python does the season lookup
            # because the value is present, but does not request an episode
            # because zero is falsey.
            "parsed": {"title": "Special Show", "season": 0, "episode": 1},
            "episode": {"name": "Should not be used", "id": 99},
            "season": {"id": 456, "external_ids": {"tvdb_id": 789},
                       "air_date": "2020-01-01", "overview": "Specials"},
            "kind": "tv", "tmdbId": 654,
        },
    ]
    for case in extra_enrichments:
        python_row = copy.deepcopy(case["row"])
        class StubTmdb:
            def details(self, *_args):
                return case["details"]

            def episode(self, *_args):
                return case["episode"]

            def season(self, *_args):
                return case["season"]

        parsed_case = case["parsed"]
        metadata.enrich(
            python_row, StubTmdb(), parsed_case,
            chosen=(case["kind"], case["tmdbId"]),
        )
        got = native_json("metadata-enrich", document=case)
        if not got["ok"] or not equivalent(python_row, got["row"]):
            raise AssertionError("metadata edge-case parity failed")

    empty_case = {
        "row": {"title": "untouched"}, "details": {},
        "parsed": {"title": "x", "season": None, "episode": None},
        "episode": None, "season": None, "kind": "movie", "tmdbId": 1,
    }
    python_row = copy.deepcopy(empty_case["row"])
    class EmptyTmdb:
        def details(self, *_args):
            return {}
    if metadata.enrich(python_row, EmptyTmdb(), empty_case["parsed"],
                       chosen=("movie", 1)):
        raise AssertionError("empty metadata oracle unexpectedly identified")
    got = native_json("metadata-enrich", document=empty_case)
    if got["ok"] or got["row"] != python_row:
        raise AssertionError("empty metadata parity failed")

    spec = importlib.util.spec_from_file_location(
        "test_plex_guids", os.path.join(HERE, "test_plex_guids.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = module.rows()
    expected_rows = copy.deepcopy(rows)
    provider = metadata.PlexProvider("unused", None, opener=module.opener)
    expected_count = metadata.apply_plex_guids(expected_rows, provider)
    got = native_json(
        "provider-guids",
        "--items", _json_temp(rows),
        "--recorded", _json_temp(module.RECORDED),
    )
    # The temporary files are removed by the caller below; this is a short-lived
    # offline fixture exchange, never a provider/network request.
    if got["count"] != expected_count or not equivalent(expected_rows, got["items"]):
        raise AssertionError("provider guid parity failed")

    partial_items = [
        {"type": "show", "ratingKey": "s", "Guid": [
            {"id": "imdb://tt-partial"}, {"id": "tmdb://900"}
        ], "guid": "old-show"},
        {"type": "season", "ratingKey": "s1", "parentRatingKey": "s",
         "index": 1, "guid": "old-season-1"},
        {"type": "season", "ratingKey": "s2", "parentRatingKey": "s",
         "index": 2, "guid": "old-season-2"},
        {"type": "episode", "ratingKey": "e1", "parentRatingKey": "s1",
         "index": 1, "guid": "old-episode-1"},
        {"type": "episode", "ratingKey": "e2", "parentRatingKey": "s2",
         "index": 1, "guid": "old-episode-2"},
    ]
    # Deliberately reverse the match body key order. Child paths use distinct
    # provider guid tails, and season 2 has no provider match.
    partial_recorded = [
        {"method": "POST", "path": "/library/metadata/matches",
         "body": {"guid": "tmdb://900", "type": 2},
         "response": {"MediaContainer": {"Metadata": [
             {"guid": "plex://show/partial", "slug": "partial-slug"}
         ]}}},
        {"method": "GET", "path": "/library/metadata/partial/children",
         "response": {"MediaContainer": {"Metadata": [
             {"index": 1, "guid": "plex://season/partial-season"}
         ]}}},
        {"method": "GET", "path": "/library/metadata/partial-season/children",
         "response": {"MediaContainer": {"Metadata": [
             {"index": 1, "guid": "plex://episode/partial-episode"}
         ]}}},
    ]
    class PartialProvider:
        def match(self, kind, external_ids):
            return ("plex://show/partial", "partial-slug") if kind == "show" else None

        def children(self, guid):
            if guid == "plex://show/partial":
                return {1: "plex://season/partial-season"}
            if guid == "plex://season/partial-season":
                return {1: "plex://episode/partial-episode"}
            return {}
    expected_partial = copy.deepcopy(partial_items)
    partial_count = metadata.apply_plex_guids(expected_partial, PartialProvider())
    got_partial = native_json(
        "provider-guids", "--items", _json_temp(partial_items),
        "--recorded", _json_temp(partial_recorded),
    )
    if (got_partial["count"] != partial_count
            or not equivalent(expected_partial, got_partial["items"])
            or expected_partial[2].get("parentGuid") != "plex://show/partial"):
        raise AssertionError("partial provider parity failed")
    return len(names) + 1 + len(extra_enrichments) + 1 + len(rows) + 1


def _json_temp(value):
    temp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(value, temp)
    temp.close()
    # Keep the path in a process-local list so the parity run can clean up after
    # invoking the CLI. This avoids putting fixture data in the workspace.
    _TEMP_FILES.append(temp.name)
    return temp.name


_TEMP_FILES = []


def check_filters():
    cases = [
        ("string", op, "The Éclair of Steel", wanted, "title")
        for op, wanted in (
            [("=", "éclair"), ("!=", "Éclair"), ("==", "THE ÉCLAIR OF STEEL"),
             ("!==", "other"), ("<=", "the"), (">=", "steel")]
        )
    ]
    cases += [
        ("string", op, "Éclair", wanted, "titleSort")
        for op, wanted in [("=", "éCLAIR"), ("!=", "other"), ("==", "éclair"),
                           ("!==", "other"), ("<=", "éz"), (">=", "éa")]
    ]
    cases += [
        ("integer", op, actual, wanted, field)
        for op, actual, wanted, field in (
            [("=", 2020, "2020", "year"), ("!=", 2020, "2021", "year"),
             ("==", 2020, "2020", "year"), ("!==", 2020, "2021", "year"),
             ("<<=", 2020, "2021", "year"), ("<=", 2020, "2020", "year"),
             (">=", 2020, "2020", "year"), (">=", 2020, "2019", "year"),
             ("=", None, "-1", "rating"), ("=", None, "0", "viewCount"),
             ("=", [None, 3], "3", "year"), ("=", "abc", "3", "year"),
             ("=", 2000, "9223372036854775808", "year"),
             ("=", None, "1.5", "viewCount"),
             ("=", None, "2147483648", "decade")]
        )
    ]
    cases += [
        ("boolean", op, actual, wanted, field)
        for op, actual, wanted, field in (
            [("=", True, "1", "unwatched"), ("!=", False, "0", "unwatched"),
             ("==", False, "1", "unwatched"), ("!==", True, "1", "unwatched"),
             ("<=", False, "1", "unwatched"), (">=", False, "1", "unwatched"),
             ("<<=", True, "0", "unwatched"), (">=", True, "1", "unmatched"),
             ("=", [False, True], "1", "unwatched"), ("=", [], "1", "unwatched")]
        )
    ]
    cases += [
        ("id", op, actual, wanted, "id")
        for op, actual, wanted in (
            [("=", 7, "07"), ("!=", 7, "8"), ("==", 7, "7"),
             ("!==", 7, "8"), ("<<=", 7, "8"), ("<=", 7, "7"),
             (">=", 7, "6"), ("=", "abc", "1"), ("=", {}, "1"),
             ("=", None, "0")]
        )
    ]
    cases += [
        ("tag", op, actual, wanted, field)
        for op, actual, wanted, field in (
            [("=", [{"id": 7, "tag": "Animation"}], "Animation", "genre"),
             ("!=", [{"id": 7, "tag": "Animation"}], "Comedy", "genre"),
             ("=", None, "", "genre"), ("=", "PG", "PG", "contentRating"),
             ("!=", "PG", "pg", "contentRating"), ("==", "PG", "pg", "contentRating"),
             ("!==", "PG", "pg", "contentRating"), ("<=", "PG", "PG", "contentRating"),
             ("<<=", "PG", "PH", "contentRating"), (">=", "PG", "PA", "contentRating"),
             (">=", "PG", "PZ", "contentRating")]
        )
    ]
    cases += [("resolution", op, "1080", "1080", "resolution")
              for op in ("=", "!=", "==", "!==", "<=", ">=", "<<=", ">>=")]
    cases += [("resolution", "=", "1080", wanted, "resolution")
              for wanted in ("2160", "", "HD")]
    cases += [
        ("audioLanguage", op, ["eng", "fra"], wanted, "audioLanguage")
        for op, wanted in (("=", "en"), ("!=", "de"), ("==", "en"),
                           ("!==", "en"), ("<=", "zz"), (">=", "aa"),
                           ("<<=", "zz"), (">=", "zz"))
    ]
    cases += [
        ("media_string", op, ["h264", None, "hevc"], wanted, "mediaVideoCodec")
        for op, wanted in (("=", "h264"), ("!=", "h264"), ("==", "H264"),
                           ("!==", "H264"), ("<=", "h264"), (">=", "g"),
                           ("<<=", "z"), (">=", "h"))
    ]
    cases += [
        ("tagId", "=", actual, wanted, "tagId")
        for actual, wanted in (([7, 8], "7"), ([0], "000"), ([7], "9"),
                               ([7], "9" * 100), ([7], "abc"), ([7], ""))
    ]
    cases += [
        ("date", op, actual, wanted, field)
        for op, actual, wanted, field in (
            [("=", "2020-01-01", "2020-01-01", "originallyAvailableAt"),
             ("!=", "2020-01-01", "2021-01-01", "originallyAvailableAt"),
             ("<<=", "2020-01-01", "2021-01-01", "originallyAvailableAt"),
             ("<=", "2020-01-01", "2020-01-01", "originallyAvailableAt"),
             (">=", "2020-01-01", "2019-01-01", "originallyAvailableAt"),
             (">>=", "2020-01-01", "2020-01-01", "originallyAvailableAt"),
             ("=", None, "0", "lastViewedAt"), ("<<=", None, "2020-01-01", "lastViewedAt"),
             ("=", None, "bad", "lastViewedAt"), ("=", "bad", "2020-01-01", "createdAt"),
             ("=", "2020-01-01", "1.5", "createdAt")]
        )
    ]
    cases += [("tag", op, 1, wanted, "location")
              for op, wanted in (("=", "1"), ("!=", "2"), ("<=", "1"),
                                 (">=", "0"), ("<<=", "2"), (">>", "0"))]

    checked = 0
    for kind, operator, actual, wanted, field in cases:
        try:
            expected = ("ok", bool(filters.compare(
                kind, operator, actual, wanted, field
            )))
        except filters.FilterError as error:
            expected = ("error", error.status)
        result = subprocess.run(
            [BIN, "filter-compare", "--kind", kind, "--operator", operator,
             "--field", field, "--actual", _json_temp(actual), "--wanted", wanted],
            text=True, capture_output=True, check=False,
        )
        if expected[0] == "error":
            if result.returncode != 2 or f"filter {expected[1]}:" not in result.stderr:
                raise AssertionError(
                    f"filter error parity failed for {kind} {operator} {field}={wanted}: "
                    f"expected status {expected[1]}, rc={result.returncode}, stderr={result.stderr!r}"
                )
            if any(marker in result.stderr.lower()
                   for marker in ("panicked at", "stack overflow", "fatal runtime")):
                raise AssertionError("native filter error panicked")
            got = ("error", expected[1])
        else:
            if result.returncode != 0:
                raise AssertionError(
                    f"native filter unexpectedly failed for {kind} {operator} {field}: "
                    f"{result.stderr.strip()}"
                )
            payload = json.loads(result.stdout)
            if list(payload) != ["match"] or type(payload["match"]) is not bool:
                raise AssertionError("native filter result shape changed")
            got = ("ok", payload["match"])
        if expected != got:
            raise AssertionError(
                f"filter parity failed for {kind} {operator} {field}={wanted}: "
                f"{expected} != {got}"
            )
        checked += 1

    field_types = {
        "year": "integer", "title": "string", "genre": "tag", "episode": "integer",
        "index": "integer", "type": "integer",
    }
    pair_cases = [
        [("year", "2020")], [("year", "2020"), ("year", "2021")],
        [("year", "2020"), ("or", "1"), ("title", "foo")],
        [("push", "1"), ("year", "2020"), ("push", "1"), ("title", "foo"),
         ("pop", "1"), ("pop", "1")],
        [("unknown", "x"), ("year", "2020")], [("genre", "1,2")],
        [("genre!=", "1,2")], [("title", "a,")], [("year", "2020"), ("and", "1")],
        [("pop", "1"), ("year", "2020")],
        [("X-Plex-Token", "secret"), ("includeGuids", "1"), ("year", "2020")],
        [("sort", "titleSort"), ("type", "1"), ("group", "1"), ("limit", "2"),
         ("sourceType", "2"), ("year", "2020")], [("and", "1")],
        [("push", "1"), ("year", "2020")],
    ]
    for pairs in pair_cases:
        errors = []
        expected_tree = filters.parse(pairs, lambda field: field_types.get(field),
                                      errors=errors)
        got = native_json(
            "filter-parse",
            document={"pairs": [list(pair) for pair in pairs],
                      "fieldTypes": field_types},
        )
        if expected_tree != got["tree"] or errors != got["errors"]:
            raise AssertionError(f"filter parse parity failed for {pairs}")
        checked += 1

    eval_cases = [
        ({"field": "year", "scope": "", "operator": "=", "value": "2020",
          "type": "integer"}, {"year": 2020}),
        ({"field": "title", "scope": "episode", "operator": "=", "value": "Fire",
          "type": "string"}, {"episode": [{"title": "Firefly"}, {"title": "Other"}]}),
        ({"field": "year", "scope": "episode", "operator": "=", "value": "bad",
          "type": "integer"}, {"episode": [{"year": 2020}]}),
    ]
    for tree, row in eval_cases:
        try:
            expected = ("ok", bool(filters.evaluate(
                tree, row, lambda field, value: value.get(field),
                resolve=lambda scope, value: value.get(scope, []),
            )))
        except filters.FilterError as error:
            expected = ("error", error.status)
        result = subprocess.run(
            [BIN, "filter-evaluate", "--input", _json_temp({
                "tree": tree, "row": row, "resolve": True,
            })], text=True, capture_output=True, check=False,
        )
        if expected[0] == "error":
            if result.returncode != 2 or f"filter {expected[1]}:" not in result.stderr:
                raise AssertionError("scoped filter error status parity failed")
        elif result.returncode != 0 or json.loads(result.stdout) != {"match": expected[1]}:
            raise AssertionError("scoped filter evaluation parity failed")
        checked += 1

    pairs = [("genre", "6"), ("episode.title", "Fire")]
    parse_errors = []
    tree = filters.parse(pairs, lambda field: {"genre": "tag", "title": "string"}.get(field),
                         errors=parse_errors)
    titles = {"genre": "Genre", "episode.title": "Episode title", "title": "Title"}
    value_titles = {"genre": {"6": "Animation"}}
    expected_description = filters.describe(
        tree, lambda field, scope: titles.get(f"{scope}.{field}", titles.get(field, "")),
        lambda field, value: value_titles.get(field, {}).get(value),
    )
    got_description = native_json(
        "filter-describe", document={"tree": tree, "titles": titles,
                                     "valueTitles": value_titles},
    )
    if not ordered_equivalent(expected_description, got_description):
        raise AssertionError("filter descriptor parity failed")
    checked += 1

    order_spec = "titleSort:desc:nocollate,random:seed"
    expected_order = filters.orders(order_spec, lambda field: field in {"titleSort", "random"})
    got_order = native_json("filter-orders", document={
        "spec": order_spec, "supported": ["titleSort", "random"],
    })
    if not ordered_equivalent(expected_order, got_order):
        raise AssertionError("filter order parity failed")
    checked += 1
    for value in (None, "1", "0", "+2", "-1", "01", "1.5", "", " 1", "9" * 40):
        try:
            expected = filters.source_order(value)
        except filters.FilterError as error:
            expected = ("error", error.status)
        result = subprocess.run(
            [BIN, "filter-source-order", "--input", _json_temp(
                {} if value is None else {"value": value})],
            text=True, capture_output=True, check=False,
        )
        if isinstance(expected, tuple):
            if result.returncode != 2 or f"filter {expected[1]}:" not in result.stderr:
                raise AssertionError("source order error parity failed")
        elif not ordered_equivalent(expected, json.loads(result.stdout)):
            raise AssertionError("source order parity failed")
        checked += 1

    document = {
        "type": 4, "tree": tree, "order": expected_order, "limit": 10,
        "group": {"field": "year"}, "errors": parse_errors,
        "titles": titles, "valueTitles": value_titles,
    }
    expected_document = filters.document(
        4, tree, expected_order, 10, {"field": "year"}, parse_errors,
        lambda field, scope: titles.get(f"{scope}.{field}", titles.get(field, "")),
        lambda field, value: value_titles.get(field, {}).get(value),
    )
    got_document = native_json("filter-document", document=document)
    if not ordered_equivalent(expected_document, got_document):
        raise AssertionError("filter document parity failed")
    checked += 1
    return checked

def check_gate_and_catalog():
    media_cases = [
        {"height": 1080, "bitrate": 1000, "videoCodec": "h264",
         "videoFrameRate": "24p"},
        {"height": 2160, "bitrate": 1000, "videoCodec": "h264",
         "videoFrameRate": "24p"},
        {"height": 1080, "bitrate": 1000, "videoCodec": "hevc",
         "videoFrameRate": "60p"},
        {"height": 720, "bitrate": 1000, "videoCodec": "vp9",
         "videoFrameRate": "30p"},
    ]
    checked = 0
    for media in media_cases:
        expected = gate.verdict(media, 1088, 20_000, False, False)
        got = native_json(
            "gate", "--max-height", "1088", "--max-bitrate", "20000",
            document=media,
        )
        if (got["ok"], got["text"]) != expected:
            raise AssertionError("gate parity failed")
        checked += 1

    for filename in ("catalog_ui.json", "catalog_features.json"):
        document = json.load(open(os.path.join(HERE, filename)))
        expected = copy.deepcopy(document)
        for row in expected["items"]:
            for media in row.get("Media") or []:
                mediainfo.normalize_media(media)
        hierarchy.normalize_rows(expected["items"])
        got = native_json("catalog-normalize", document=document)
        if not equivalent(expected, got):
            raise AssertionError(f"catalog normalization parity failed for {filename}")
        if got.get("_provenance") != document.get("_provenance"):
            raise AssertionError(f"catalog provenance was lost for {filename}")
        checked += len(document["items"])
    return checked


def main():
    if not os.path.exists(BIN):
        raise SystemExit(f"native binary not found: {BIN}")
    results = [
        ("mediainfo fixtures", check_mediainfo()),
        ("hierarchy rows", (check_hierarchy(), 0)),
        ("XML documents", (check_xml(), 0)),
        ("metadata/provider rows", (check_metadata(), 0)),
        ("filter cases", (check_filters(), 0)),
        ("gate/catalog rows", (check_gate_and_catalog(), 0)),
    ]
    print("native phase2 parity")
    for name, (checks, fields) in results:
        suffix = f", {fields} scalar fields" if fields else ""
        print(f"PASS {name}: {checks} checks{suffix}")
    print(f"PASS total: {sum(checks for _, (checks, _) in results)} checks")
    for path in _TEMP_FILES:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    try:
        main()
    finally:
        for path in _TEMP_FILES:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
