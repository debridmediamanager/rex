#!/usr/bin/env python3
"""Focused regressions for catalog shapes absent from the ordinary UI fixture."""

import os
import re
import sys
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("PLEXSIM_TEST_ROOT", os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import build_catalog  # noqa: E402
import server  # noqa: E402
import xmlout  # noqa: E402


class Resolver:
    def resolve(self, link):
        return link, 0


def main():
    failures = []

    def check(name, condition, detail=""):
        print(("PASS" if condition else "FAIL"), name, detail)
        if not condition:
            failures.append(name)

    fixture = os.environ.get("PLEXSIM_CATALOG_FEATURES",
                             os.path.join(HERE, "catalog_features.json"))
    catalog = server.Catalog(fixture)
    sim = server.PlexSim(catalog, Resolver(), assert_claimed=True)

    def matches(query, tagged=False):
        typed, tags = sim.search_matches(query, 100, tagged_items=tagged)
        titles = {kind: [row.get("title") for row in rows]
                  for kind, rows in typed.items()}
        tag_names = {kind: [entry["tag"].get("tag") for entry in rows]
                     for kind, rows in tags.items()}
        return titles, tag_names

    # Cycle 41 completed the title-mark gap left by Cycle 32. Across every
    # assigned punctuation, symbol, separator, mark and format scalar in the
    # previously unswept ranges, both PMS versions remove exactly U+0300..U+036F
    # between ASCII sentinels. A generic combining-class test wrongly removed
    # another 699 Mn/Mc scalars and preserved U+034F.
    mark_domain = [
        codepoint
        for start, end in ((0x0300, 0x1fff), (0x3000, 0x10ffff))
        for codepoint in range(start, end + 1)
        if unicodedata.category(chr(codepoint)).startswith(("P", "S", "Z", "M"))
        or unicodedata.category(chr(codepoint)) == "Cf"
    ]
    removed_marks = [
        codepoint for codepoint in mark_domain
        if server.hierarchy_module.fold_title("A" + chr(codepoint) + "B") == "AB"
    ]
    check("title folding removes exactly the measured combining-diacritics block",
          len(mark_domain) == 7531
          and removed_marks == list(range(0x0300, 0x0370)),
          "%d-domain %d-removed first=%s last=%s"
          % (len(mark_domain), len(removed_marks),
             "U+%04X" % removed_marks[0] if removed_marks else "none",
             "U+%04X" % removed_marks[-1] if removed_marks else "none"))

    # Cycle 37 reconstructed both PMS spellfix tables, including the exact
    # editdist1/rank score, top-ten cap, <=200 cutoff and fallback gate. These
    # titles are the tiny measured kids corpus so `zzz` has the same two words.
    spelling = getattr(catalog, "spelling", None)
    zzz = spelling.metadata.candidates("zzz") if spelling is not None else []
    check("spellfix candidates use the measured cost and bounded result set",
          [(row["word"], row["distance"], row["score"]) for row in zzz]
          == [("job", 110, 140), ("steel", 137, 167)]
          and len(zzz) <= 10 and all(row["score"] <= 200 for row in zzz))

    # Cycle 39 reconstructed the tag vocabulary exactly. Most inputs were already
    # on API rows but are not shaped like Catalog.TAG_FIELDS; the only remainder is
    # this versioned 28-string server-owned fixture.
    source_row = {
        "title": "Vocabulary Sources",
        "Genre": [{"tag": "Measured Genre"}],
        "Chapter": [{"tag": "The Grey Havens"}],
        "Review": [{"tag": "Reviewer One"}],
        "Marker": [{"tag": "Intro Marker"}],
        "CommonSenseMedia": [{"tag": "Age Twelve"}],
        "Guid": [{"id": "imdb://tt1234567"}],
        "Rating": [{"image": "rottentomatoes://image.rating.upright"}],
        "studio": "Studio Canal",
        "Network": [{"tag": "HBO Max"}],
        "thumb": "/thumb/path",
        "art": "/art/path",
        "theme": "/theme/path",
        "Image": [{"type": "clearLogo", "url": "/logo/path"}],
    }
    source_index = server.spellfix_module.SearchIndex([source_row], ("Genre",))
    source_words = {entry["word"]: entry["rank"] for entry in source_index.tags.rows}
    expected_source_words = {
        "measured", "grey", "reviewer", "marker", "twelve", "imdb",
        "rottentomatoes", "studio", "canal", "hbo", "max", "thumb",
        "art", "theme", "clearlogo",
    }
    check("tag spellfix rebuild reads every measured row-backed source",
          expected_source_words <= set(source_words)
          and all(source_words[word] == 1 for word in expected_source_words))

    expected_static_tags = [
        "Optimized for Mobile", "Optimized for TV", "Original Quality",
        "The Movie Database (Aired)", "TheTVDB (Absolute)",
        "TheTVDB (Aired)", "TheTVDB (Alternate Aired)",
        "TheTVDB (Alternate)", "TheTVDB (Amazon)", "TheTVDB (Arc)",
        "TheTVDB (Blu-ray)", "TheTVDB (BluRay)", "TheTVDB (Combined)",
        "TheTVDB (Disney+)", "TheTVDB (DVD)", "TheTVDB (France)",
        "TheTVDB (International)", "TheTVDB (Legacy)",
        "TheTVDB (Netflix)", "TheTVDB (Ocean Dub)",
        "TheTVDB (Parts (Netflix))", "TheTVDB (Production)",
        "TheTVDB (Sagas)", "TheTVDB (Seasons)",
        "TheTVDB (SkyShowtime)", "TheTVDB (Story)",
        "TheTVDB (Streaming)", "TheTVDB (Superfan)",
    ]
    check("tag spellfix includes the exact measured 1.42.2 static vocabulary",
          getattr(server, "SPELLFIX_STATIC_TAGS", []) == expected_static_tags)
    check("static tag inputs participate in the bounded candidate vectors",
          [entry["word"] for entry in catalog.spelling.tags.candidates("optmized")]
          == ["optimized"]
          and "dvd" in [entry["word"]
                        for entry in catalog.spelling.tags.candidates("tft")]
          and "bluray" in [entry["word"]
                           for entry in catalog.spelling.tags.candidates("flr")])

    def repeated_tag(word, count):
        return [{"tag": word + (" " * index)} for index in range(count)]

    # A compact reconstruction of the live `emtz` witness. The 5,785 distinct
    # imdb GUID strings give that token its measured rank and keep Imtiaz out of
    # the ten-candidate vector, exactly as zen's visible people hubs do.
    emtz_ranks = {
        "ents": 2, "hintze": 1, "ends": 2, "entre": 1, "hunts": 2,
        "enter": 4, "amter": 1, "ent": 3, "emde": 1, "imtiaz": 1,
    }
    emtz_row = {
        "title": "Vocabulary Witness",
        "Role": [tag for word, count in emtz_ranks.items()
                 for tag in repeated_tag(word, count)],
        "Guid": [{"id": "imdb://tt%07d" % index} for index in range(5785)],
    }
    emtz_index = server.spellfix_module.SearchIndex([emtz_row], ("Role",))
    emtz_words = [entry["word"] for entry in emtz_index.tags.candidates("emtz")]
    check("GUID rank keeps the measured emtz people candidate outside the cap",
          emtz_words[-1:] == ["imdb"] and "imtiaz" not in emtz_words
          and not emtz_index.tag_matches("Imtiaz Ali", "emtz"))
    check("misspelled title words reach the measured movie rows",
          matches("teers")[0].get("movie") == ["Tears of Steel"]
          and matches("steal")[0].get("movie") == ["Tears of Steel"]
          and matches("buny")[0].get("movie") == ["Big Buck Bunny"]
          and matches("elefants")[0].get("movie") == ["Elephants Dream"])
    check("fallback ANDs retained tokens and discards only sub-three-byte tokens",
          matches("Tears zzz")[0].get("movie") == ["Tears of Steel"]
          and matches("Tears xy")[0].get("movie") == ["Tears of Steel"]
          and "Tears of Steel" not in matches("Tears xyz")[0].get("movie", []))
    train_titles, _ = matches("train")
    check("an episode-only normal hit leaves the global spelling gate open",
          train_titles.get("episode") == ["The Train Job"]
          and train_titles.get("movie") == ["Elephants Dream"])
    # Make the later episode bucket score above the movie.  The recovered flat
    # comparator still preserves bucket assembly instead of globally re-sorting it.
    catalog.items[109]["titleSort"] = "train"
    train_flat = sim.library_search({"query": "train", "limit": "100"})["MediaContainer"]
    catalog.items[109].pop("titleSort", None)
    train_rows = train_flat.get("SearchResult") or []
    check("flat search preserves its measured movie-before-episode buckets",
          [(row.get("Metadata") or {}).get("ratingKey") for row in train_rows[:2]]
          == ["105", "109"]
          and train_rows[0].get("score", 0) < train_rows[1].get("score", 0))

    # Cycle 38 recovered the final comparator from both PMS binaries.  The score is
    # compared at full precision and only formatted to five decimals afterwards.
    # These two rows deliberately serialize to the same value; the higher hidden
    # audience component still wins even though its title sorts later.
    catalog.items[101]["audienceRating"] = 8.400
    catalog.items[102]["audienceRating"] = 8.372
    feature_hubs = sim.search({"query": "feature", "limit": "100"})["MediaContainer"]
    feature_movies = next(hub for hub in feature_hubs["Hub"]
                          if hub["hubIdentifier"] == "movie").get("Metadata") or []
    check("hub search sorts the unrounded score before formatting it",
          [row.get("ratingKey") for row in feature_movies[:2]] == ["101", "102"]
          and [row.get("score") for row in feature_movies[:2]]
          == ["0.33084", "0.33084"])
    catalog.items[101].pop("audienceRating", None)
    catalog.items[102].pop("audienceRating", None)

    # Cycle 39's complete 659-cell score-fold sweep found that membership and
    # score tiers are separate passes. Partial tiering applies the measured title
    # table before ASCII case folding; whole-title equality is broader, with the
    # measured long-s position and two-letter macron-o exceptions.
    tears = next(row for row in catalog.items.values()
                 if row.get("title") == "Tears of Steel")
    tier_cases = {
        "TĒars": "fuzzy",
        "TĒar": "fuzzy",
        "Tears Ōf Steel": "anchored",
        "Tears ōf Steel": "anchored",
        "Tearſ of Steel": "fuzzy",
        "TĒars of Steel": "exact",
        "Tears of StĒel": "exact",
        "Tears of ſteel": "exact",
    }
    tier_scores = {
        "TĒars": "0.33055",
        "TĒar": "0.33055",
        "Tears Ōf Steel": "0.73055",
        "Tears ōf Steel": "0.73055",
        "Tearſ of Steel": "0.33055",
        "TĒars of Steel": "0.93055",
        "Tears of StĒel": "0.93055",
        "Tears of ſteel": "0.93055",
    }

    def tears_hub_score(query):
        document = sim.search({"query": query, "limit": "100"})["MediaContainer"]
        return next((row.get("score") for hub in document.get("Hub") or []
                     for row in hub.get("Metadata") or []
                     if row.get("ratingKey") == str(tears.get("ratingKey"))), None)

    check("hub score folding keeps its measured partial and whole-title passes",
          {query: catalog.search_tier(tears, query) for query in tier_cases}
          == tier_cases
          and {query: tears_hub_score(query) for query in tier_scores}
          == tier_scores
          and catalog.search_tier({"title": "Dream"}, "Ðre") == "prefix")

    check("the anchored search tier reads titleSort",
          catalog.search_tier({"title": "The Train Job",
                               "titleSort": "Train Job"}, "train") == "anchored")

    counted_tag = {"id": 777, "tag": "Gina Torres", "filter": "actor=777"}
    counted_rows = (catalog.items[108], catalog.items[109])
    old_roles = [row.get("Role") for row in counted_rows]
    for row in counted_rows:
        row["Role"] = [dict(counted_tag)]
    counted_search = sim.library_search({"query": "Gina", "limit": "100"})[
        "MediaContainer"]
    counted_hit = next((result.get("Directory") for result in
                        counted_search.get("SearchResult") or []
                        if (result.get("Directory") or {}).get("tag") == "Gina Torres"), {})
    for row, roles in zip(counted_rows, old_roles):
        if roles is None:
            row.pop("Role", None)
        else:
            row["Role"] = roles
    check("a search tag count spans every row type in its tag table",
          counted_hit.get("count") == 2, str(counted_hit))

    profile_rows = (sim.handle("GET", "/clients/profiles", b"", {})
                    ["MediaContainer"].get("Profile") or [])
    profile_titles = [row.get("title") for row in profile_rows]
    check("client profiles preserve deterministic resource-file order",
          profile_titles.index("Opera TV App") < profile_titles.index("Opera")
          and profile_titles.index("Roku-3.1") < profile_titles.index("Roku")
          and profile_titles.index("Samsung SmartTV") < profile_titles.index("Samsung")
          and profile_titles.index("Sony Blu-ray 2013") < profile_titles.index("Sony Blu-ray")
          and profile_titles.index("Plex Desktop")
          < profile_titles.index("Plex Home Theater-RaspberryPi")
          < profile_titles.index("Plex Home Theater"))
    check("Traditional Chinese request tags select the measured English catalogue",
          server.request_language({"X-Plex-Language": "zh-HK"}, {}) == "en"
          and server.request_language({}, {"X-Plex-Language": "zh-TW"}) == "en")

    # The real spellfix tables are stale between rebuilds.  Both binaries call the
    # rebuild from explicit optimization, the Butler task, and section-deletion
    # completion; ordinary title edits do not maintain it row by row.
    rebuild_catalog = server.Catalog(fixture)
    rebuild_sim = server.PlexSim(rebuild_catalog, Resolver(), assert_claimed=True)

    def rebuilt_movies(query):
        typed, _tags = rebuild_sim.search_matches(query, 100)
        return [row.get("ratingKey") for row in typed.get("movie", [])]

    rebuild_catalog.items[101]["title"] = "Zebracorn"
    stale_explicit = rebuilt_movies("zebracrn")
    rebuild_sim.handle("PUT", "/library/optimize", b"", {})
    fresh_explicit = rebuilt_movies("zebracrn")
    rebuild_catalog.items[101]["title"] = "Quokkahedron"
    stale_butler = rebuilt_movies("quokkahedrn")
    rebuild_sim.handle("POST", "/butler/OptimizeDatabase", b"", {})
    fresh_butler = rebuilt_movies("quokkahedrn")
    rebuild_catalog.items[108]["title"] = "Xylophonic"
    stale_delete = [row.get("ratingKey") for row in
                    rebuild_sim.search_matches("xylophnic", 100)[0].get("show", [])]
    rebuild_sim.handle("DELETE", "/library/sections/1", b"", {})
    fresh_delete = [row.get("ratingKey") for row in
                    rebuild_sim.search_matches("xylophnic", 100)[0].get("show", [])]
    check("spellfix rebuilds only at the measured optimization call sites",
          stale_explicit == [] and fresh_explicit == ["101"]
          and stale_butler == [] and fresh_butler == ["101"]
          and stale_delete == [] and fresh_delete == ["108"])

    tudik_titles, tudik_tags = matches("Tudik", tagged=True)
    tudik_hubs = sim.search({"query": "Tudik", "limit": "100"})["MediaContainer"]
    tudik_movies = next(hub for hub in tudik_hubs["Hub"]
                        if hub["hubIdentifier"] == "movie").get("Metadata") or []
    check("the measured people spelling branch emits its tag and associated item",
          tudik_tags.get("actor") == ["Alan Tudyk"]
          and tudik_titles.get("movie") == ["Stacked Feature"]
          and tudik_movies[0].get("reason") == "actor"
          and tudik_movies[0].get("reasonTitle") == "Alan Tudyk")

    stacked = getattr(build_catalog, "stacked_name", lambda _name: None)
    check("the builder recognises only the measured - cdN stack spelling",
          stacked("Feature - cd2.mkv") == ("feature", 2, "Feature.mkv")
          and stacked("Feature part2.mkv") is None)
    check("a stacked catalog Media carries the summed duration",
          catalog.items[101]["Media"][0]["duration"] == 333000
          and [p["duration"] for p in catalog.items[101]["Media"][0]["Part"]]
          == [111000, 222000])
    check("each Part owns its own restricted link",
          sim.resolve_part(1010)[0] == "fixture://stacked-cd1"
          and sim.resolve_part(1011)[0] == "fixture://stacked-cd2")

    def decision(part_index, rating_key=101, extra=None):
        query = {
            "hasMDE": "1",
            "path": "/library/metadata/%s" % rating_key,
            "mediaIndex": "0",
            "partIndex": str(part_index),
            "X-Plex-Platform": "Chrome",
        }
        query.update(extra or {})
        return sim.handle("GET", "/video/:/transcode/universal/decision?"
                          + urllib.parse.urlencode(query), b"", {})

    first = decision(0)["MediaContainer"]
    second = decision(1)["MediaContainer"]
    invalid = decision(2)
    all_parts = decision(-1)["MediaContainer"]
    first_parts = [part["id"] for row in first.get("Metadata") or []
                   for media in row.get("Media") or [] for part in media.get("Part") or []]
    second_parts = [part["id"] for row in second.get("Metadata") or []
                    for media in row.get("Media") or [] for part in media.get("Part") or []]
    check("partIndex selects one Part and its own streams",
          first_parts == ["1010"] and second_parts == ["1011"])
    check("an out-of-range multi-part partIndex is a 400",
          isinstance(invalid, tuple) and invalid[:2] == ("error", 400))
    check("partIndex -1 returns the measured all-parts conversion refusal",
          all_parts.get("size") == 0 and not all_parts.get("Metadata")
          and all_parts.get("directPlayDecisionCode") == 3000
          and all_parts.get("generalDecisionCode") == 2000
          and all_parts.get("transcodeDecisionCode") == 4005
          and all_parts.get("directPlayDecisionText")
          == "App cannot direct play this item. Multi-part media requires a conversion when all parts are selected.")

    # Cycle 41 created the same two-Media movie on writable copies of both PMS
    # versions. Unlike a single-Media row, its decision has no unambiguous fallback:
    # missing, empty, nonnumeric, negative and out-of-range mediaIndex values are 400.
    # Explicit 0/1 each return exactly the selected Media. A play queue deliberately
    # ignores the selector and carries both Media, in detail order, with neither marked
    # selected.
    two_media_row_c41g = catalog.items[101]
    original_media_c41g = two_media_row_c41g.get("Media")
    two_media_c41g = [catalog.items[101]["Media"][0],
                      catalog.items[102]["Media"][0]]
    two_media_row_c41g["Media"] = two_media_c41g
    media_ids_c41g = [str(media_c41g.get("id")) for media_c41g in two_media_c41g]

    def multi_decision_c41g(value_c41g=None, present_c41g=True):
        query_c41g = {
            "hasMDE": "1",
            "path": "/library/metadata/101",
            "partIndex": "0",
            "X-Plex-Platform": "Chrome",
        }
        if present_c41g:
            query_c41g["mediaIndex"] = value_c41g
        return sim.handle(
            "GET", "/video/:/transcode/universal/decision?"
            + urllib.parse.urlencode(query_c41g), b"", {})

    valid_media_c41g = {}
    for index_c41g in ("0", "1"):
        answer_c41g = multi_decision_c41g(index_c41g)
        body_c41g = answer_c41g.get("MediaContainer", {})
        valid_media_c41g[index_c41g] = [
            media_c41g for row_c41g in body_c41g.get("Metadata") or []
            for media_c41g in row_c41g.get("Media") or []]
    invalid_media_c41g = [multi_decision_c41g(None, False),
                          multi_decision_c41g(""),
                          multi_decision_c41g("abc"),
                          multi_decision_c41g("-1"),
                          multi_decision_c41g("2")]
    check("a multi-Media decision requires one explicit in-range mediaIndex",
          all(isinstance(answer_c41g, tuple)
              and answer_c41g[:2] == ("error", 400)
              for answer_c41g in invalid_media_c41g)
          and all(len(valid_media_c41g[index_c41g]) == 1
                  and str(valid_media_c41g[index_c41g][0].get("id"))
                      == media_ids_c41g[int(index_c41g)]
                  and valid_media_c41g[index_c41g][0].get("selected") is True
                  for index_c41g in ("0", "1")),
          "valid=%s invalid=%s"
          % ({key_c41g: [row_c41g.get("id") for row_c41g in rows_c41g]
              for key_c41g, rows_c41g in valid_media_c41g.items()},
             [answer_c41g[:2] if isinstance(answer_c41g, tuple) else "200"
              for answer_c41g in invalid_media_c41g]))

    queue_media_c41g = []
    queue_uri_c41g = ("server://%s/com.plexapp.plugins.library/library/metadata/101"
                      % catalog.machine)
    for value_c41g, present_c41g in ((None, False), ("0", True), ("1", True),
                                     ("abc", True), ("-1", True), ("2", True)):
        params_c41g = {"type": "video", "uri": queue_uri_c41g}
        if present_c41g:
            params_c41g["mediaIndex"] = value_c41g
        answer_c41g = sim.handle(
            "POST", "/playQueues?" + urllib.parse.urlencode(params_c41g), b"", {})
        body_c41g = answer_c41g.get("MediaContainer", {})
        rows_c41g = body_c41g.get("Metadata") or []
        queue_media_c41g.append([
            (str(media_c41g.get("id")), media_c41g.get("selected"))
            for row_c41g in rows_c41g for media_c41g in row_c41g.get("Media") or []])
    if original_media_c41g is None:
        two_media_row_c41g.pop("Media", None)
    else:
        two_media_row_c41g["Media"] = original_media_c41g
    check("a multi-Media queue ignores mediaIndex and keeps every version",
          all(rows_c41g == [(media_ids_c41g[0], None),
                            (media_ids_c41g[1], None)]
              for rows_c41g in queue_media_c41g),
          str(queue_media_c41g))

    dovi = decision(0, 102, {"directPlay": "0"})["MediaContainer"]
    check("the catalog can carry a profile-5 Dolby Vision row to its measured refusal",
          dovi.get("size") == 0 and dovi.get("transcodeDecisionCode") == 2003
          and dovi.get("transcodeDecisionText")
          == "File is unplayable. DoVi (Profile 5) color space is not supported.")

    def get(path):
        try:
            answer = sim.handle("GET", path, b"", {})
        except Exception as error:  # Parent-proof runs should fail, not abort.
            return ("exception", type(error).__name__)
        return answer.get("MediaContainer") if isinstance(answer, dict) else answer

    # Cycle 50 measured the season form on both supported PMS versions.  Its OnDeck
    # row is selected for the whole show, then exposed only by the season that owns
    # that episode; it is not each season's first unwatched episode.
    ondeck_rows = [
        {"ratingKey": "45101", "key": "/library/metadata/45101/children",
         "type": "show", "title": "On Deck Show", "librarySectionID": 2},
        {"ratingKey": "45102", "key": "/library/metadata/45102/children",
         "type": "season", "title": "Season 1", "index": 1,
         "parentRatingKey": "45101", "librarySectionID": 2},
        {"ratingKey": "45103", "key": "/library/metadata/45103/children",
         "type": "season", "title": "Season 2", "index": 2,
         "parentRatingKey": "45101", "librarySectionID": 2},
        {"ratingKey": "45104", "key": "/library/metadata/45104", "type": "episode",
         "title": "Watched", "index": 1, "parentIndex": 1,
         "parentRatingKey": "45102", "grandparentRatingKey": "45101",
         "librarySectionID": 2, "viewCount": 1, "lastViewedAt": 100},
        {"ratingKey": "45105", "key": "/library/metadata/45105", "type": "episode",
         "title": "Next", "index": 2, "parentIndex": 1,
         "parentRatingKey": "45102", "grandparentRatingKey": "45101",
         "librarySectionID": 2},
        {"ratingKey": "45106", "key": "/library/metadata/45106", "type": "episode",
         "title": "Later Season", "index": 1, "parentIndex": 2,
         "parentRatingKey": "45103", "grandparentRatingKey": "45101",
         "librarySectionID": 2},
    ]
    for synthetic in ondeck_rows:
        catalog.add(synthetic)
    season_one = get("/library/metadata/45102?includeOnDeck=1")
    season_two = get("/library/metadata/45103?includeOnDeck=1")
    season_one_row = (season_one.get("Metadata") or [{}])[0]
    season_two_row = (season_two.get("Metadata") or [{}])[0]
    catalog.remove(45101)
    check("a season exposes only the show's own On Deck episode",
          ((season_one_row.get("OnDeck") or {}).get("Metadata") or {}).get("ratingKey")
          == "45105" and "OnDeck" not in season_two_row,
          "season1=%s season2=%s"
          % (((season_one_row.get("OnDeck") or {}).get("Metadata") or {}).get("ratingKey"),
             ((season_two_row.get("OnDeck") or {}).get("Metadata") or {}).get("ratingKey")))

    common_sense_row = catalog.items[108]
    common_sense_row["CommonSenseMedia"] = [{"tag": "Age Twelve"}]
    common_sense_detail = (get("/library/metadata/108").get("Metadata") or [{}])[0]
    common_sense_grid = next(
        row for row in get("/library/sections/2/all").get("Metadata") or []
        if row.get("ratingKey") == "108")
    common_sense_row.pop("CommonSenseMedia", None)
    check("CommonSenseMedia is detail-only and absent from a light row",
          common_sense_detail.get("CommonSenseMedia") == [{"tag": "Age Twelve"}]
          and "CommonSenseMedia" not in common_sense_grid,
          "detail=%s grid=%s"
          % (common_sense_detail.get("CommonSenseMedia"),
             common_sense_grid.get("CommonSenseMedia")))

    recent_show = catalog.items[108]
    recent_leaf = catalog.items[109]
    old_show_added = recent_show.get("addedAt")
    old_leaf_added = recent_leaf.get("addedAt")
    recent_show["addedAt"] = 300
    recent_leaf["addedAt"] = 200
    section_hubs = sim.section_hubs("2")["MediaContainer"].get("Hub") or []
    section_recent = next(h for h in section_hubs
                          if h.get("hubIdentifier") == "tv.recentlyadded.2")
    promoted_hubs = sim.promoted_hubs({"contentDirectoryID": "2"})["MediaContainer"].get("Hub") or []
    promoted_recent = next(h for h in promoted_hubs
                           if h.get("hubIdentifier") == "tv.recentlyadded.2")
    section_stamp = next(r.get("addedAt") for r in section_recent.get("Metadata") or []
                         if r.get("ratingKey") == "108")
    promoted_stamp = next(r.get("addedAt") for r in promoted_recent.get("Metadata") or []
                          if r.get("ratingKey") == "108")
    if old_show_added is None:
        recent_show.pop("addedAt", None)
    else:
        recent_show["addedAt"] = old_show_added
    if old_leaf_added is None:
        recent_leaf.pop("addedAt", None)
    else:
        recent_leaf["addedAt"] = old_leaf_added
    check("the TV Recently Added shelves date a show by its newest leaf",
          section_stamp == 200 and promoted_stamp == 200,
          "section=%s promoted=%s" % (section_stamp, promoted_stamp))

    thumb_rows = [
        {"ratingKey": "45201", "key": "/library/metadata/45201/children",
         "type": "show", "title": "Thumb Show", "librarySectionID": 2,
         "thumb": "/library/metadata/45201/thumb/20"},
        {"ratingKey": "45202", "key": "/library/metadata/45202/children",
         "type": "season", "title": "Season 1", "index": 1,
         "parentRatingKey": "45201", "librarySectionID": 2,
         "thumb": "/library/metadata/45201/thumb/20"},
        {"ratingKey": "45203", "key": "/library/metadata/45203", "type": "episode",
         "title": "Episode", "index": 1, "parentRatingKey": "45202",
         "grandparentRatingKey": "45201", "librarySectionID": 2},
    ]
    for synthetic in thumb_rows:
        catalog.add(synthetic)
    borrowed_thumb = get("/library/metadata/45202/children").get("thumb")
    catalog.items[45202]["thumb"] = "/library/metadata/45202/thumb/21"
    owned_thumb = get("/library/metadata/45202/children").get("thumb")
    catalog.remove(45201)
    check("a children container carries only artwork its parent owns",
          borrowed_thumb is None and owned_thumb == "/library/metadata/45202/thumb/21",
          "borrowed=%r owned=%r" % (borrowed_thumb, owned_thumb))

    restamp_rows = [
        {"ratingKey": "45301", "key": "/library/metadata/45301/children",
         "type": "show", "title": "Restamp Show", "librarySectionID": 2,
         "thumb": "/library/metadata/45301/thumb/999",
         "art": "/library/metadata/45301/art/999"},
        {"ratingKey": "45302", "key": "/library/metadata/45302/children",
         "type": "season", "title": "Season 1", "index": 1,
         "parentRatingKey": "45301", "librarySectionID": 2,
         "thumb": "/library/metadata/45301/thumb/999",
         "art": "/library/metadata/45301/art/999"},
        {"ratingKey": "45303", "key": "/library/metadata/45303", "type": "episode",
         "title": "Restamped Episode", "index": 1, "parentRatingKey": "45302",
         "grandparentRatingKey": "45301", "librarySectionID": 2,
         "addedAt": 123, "thumb": "/library/metadata/45303/thumb/456",
         "grandparentThumb": "/library/metadata/45301/thumb/999",
         "art": "/library/metadata/45301/art/999"},
    ]
    for synthetic in restamp_rows:
        catalog.add(synthetic)
    restamped_leaf = (get("/library/metadata/45301/allLeaves").get("Metadata") or [{}])[0]
    restamped_images = {image.get("type"): image.get("url")
                        for image in restamped_leaf.get("Image") or []}
    catalog.remove(45301)
    check("allLeaves restamps borrowed fields and their derived Image rows together",
          restamped_leaf.get("grandparentThumb") == "/library/metadata/45301/thumb/123"
          and restamped_images.get("coverPoster") == "/library/metadata/45301/thumb/123"
          and restamped_images.get("background") == "/library/metadata/45301/art/123"
          and restamped_images.get("snapshot") == "/library/metadata/45303/thumb/456",
          "fields=%r images=%s"
          % (restamped_leaf.get("grandparentThumb"), restamped_images))

    inline_deck_leaf = catalog.items[109]
    old_offset = inline_deck_leaf.get("viewOffset")
    old_viewed = inline_deck_leaf.get("lastViewedAt")
    inline_deck_leaf["viewOffset"] = 100
    inline_deck_leaf["lastViewedAt"] = 100
    inline_deck_show = (get("/library/metadata/108?includeOnDeck=1&includeExtras=1")
                        .get("Metadata") or [{}])[0]
    inline_deck_row = ((inline_deck_show.get("OnDeck") or {}).get("Metadata") or {})
    if old_offset is None:
        inline_deck_leaf.pop("viewOffset", None)
    else:
        inline_deck_leaf["viewOffset"] = old_offset
    if old_viewed is None:
        inline_deck_leaf.pop("lastViewedAt", None)
    else:
        inline_deck_leaf["lastViewedAt"] = old_viewed
    check("includeExtras reaches the inline OnDeck row",
          (inline_deck_row.get("Extras") or {}).get("size") == 0,
          str(inline_deck_row.get("Extras")))

    parent = get("/library/metadata/101?includeExtras=1")
    parent_row = (parent.get("Metadata") or [{}])[0]
    inline = (parent_row.get("Extras") or {}).get("Metadata") or []
    check("the parent advertises its lowest-index trailer and an uncapped inline block",
          parent_row.get("primaryExtraKey") == "/library/metadata/201"
          and (parent_row.get("Extras") or {}).get("size") == 1
          and [row.get("ratingKey") for row in inline] == ["201"]
          and "art" not in inline[0])

    item = get("/library/metadata/101/extras/all")
    section = get("/library/sections/1/extras/all?extraType=1")
    section_miss = get("/library/sections/1/extras/all?extraType=3")
    item_row = (item.get("Metadata") or [{}])[0]
    section_row = (section.get("Metadata") or [{}])[0]
    forbidden = {"librarySectionID", "librarySectionTitle", "librarySectionKey"}
    check("item and section extras expose the measured full and light projections",
          item.get("size") == 1 and item.get("allowSync") is False
          and not (forbidden & set(item))
          and str(item_row.get("art") or "").startswith("/library/metadata/101/art/")
          and section.get("size") == 1 and not (forbidden & set(section_row))
          and all("Stream" not in part for media in section_row.get("Media") or []
          for part in media.get("Part") or [])
          and section_miss.get("size") == 0)
    check("a clip carries only its own coverPoster Image on both extras lists",
          item_row.get("Image")
          == [{"alt": "Stacked Feature Trailer", "type": "coverPoster",
               "url": catalog.items[201].get("thumb")}]
          and section_row.get("Image") == item_row.get("Image"),
          "item=%s section=%s" % (item_row.get("Image"), section_row.get("Image")))
    untyped_library_keys = [row.get("ratingKey") for row in
                            get("/library/all").get("Metadata") or []]
    check("the untyped whole-library listing omits clips",
          "201" not in untyped_library_keys, str(untyped_library_keys))
    stored_duration_row = catalog.items[101]
    stored_duration_row["_libraryAllDuration"] = 888084
    untyped_duration_rows = {row.get("ratingKey"): row for row in
                             get("/library/all").get("Metadata") or []}
    typed_duration_rows = {row.get("ratingKey"): row for row in
                           get("/library/all?type=1").get("Metadata") or []}
    stored_duration_row.pop("_libraryAllDuration", None)
    check("untyped library durations use their stored metadata column",
          untyped_duration_rows["101"].get("duration") == 888084
          and "duration" not in untyped_duration_rows["102"]
          and typed_duration_rows["101"].get("duration") == 333000
          and typed_duration_rows["102"].get("duration") == 10000,
          "untyped=%s typed=%s"
          % ({key: row.get("duration") for key, row in untyped_duration_rows.items()
              if key in ("101", "102")},
             {key: row.get("duration") for key, row in typed_duration_rows.items()
              if key in ("101", "102")}))

    clip = get("/library/metadata/201")
    clip_row = ((clip.get("Metadata") or [{}])[0]
                if isinstance(clip, dict) else {})
    check("a faithful clip detail needs no invented section fields",
          isinstance(clip, dict) and clip.get("size") == 1
          and clip.get("allowSync") is False
          and not (forbidden & set(clip)) and not (forbidden & set(clip_row))
          and "parentRatingKey" not in clip_row and "_parentRatingKey" not in clip_row)
    check("a clip borrows the art attribute but owns only thumb and poster reads",
          get("/library/metadata/201/art")[:2] == ("error", 404)
          and get("/library/metadata/201/thumb")[0] in ("image-key", "png")
          and get("/library/metadata/201/poster")[0] in ("image-key", "png"))

    xml = xmlout.to_xml({"MediaContainer": item}).decode("utf-8")
    match = re.search(r"<Video\b[^>]*", xml)
    attributes = re.findall(r"([A-Za-z0-9_:]+)=", match.group(0)) if match else []
    check("clip XML keeps extraType as its final attribute",
          attributes[-1:] == ["extraType"], str(attributes))

    # Cycle 41, measured on a level containing both a direct file and a subfolder:
    # JSON carries an untyped directory row followed by the movie row, while XML
    # names those same rows `<Directory>` then `<Video>`. A container-wide Metadata
    # override handled folder-only levels but mislabeled both rows as Video here.
    mixed_a = catalog.items[103]
    mixed_b = catalog.items[104]
    original_a = mixed_a.get("Media")
    original_b = mixed_b.get("Media")
    mixed_a["Media"] = [{"id": 4103, "Part": [{"id": 4103,
                                                "file": "/plexsim/1/A/direct.mkv"}]}]
    mixed_b["Media"] = [{"id": 4104, "Part": [{"id": 4104,
                                                "file": "/plexsim/1/A/B/deep.mkv"}]}]
    mixed_folder = sim.folder_view("1", catalog.section("1"), {"parent": "1"})
    mixed_body = mixed_folder["MediaContainer"]
    mixed_elements = mixed_body.pop("_xml", None)
    mixed_xml = ET.fromstring(xmlout.to_xml(mixed_folder, mixed_elements))
    mixed_tags = [child.tag for child in mixed_xml]
    if original_a is None:
        mixed_a.pop("Media", None)
    else:
        mixed_a["Media"] = original_a
    if original_b is None:
        mixed_b.pop("Media", None)
    else:
        mixed_b["Media"] = original_b
    check("a mixed folder level renders Directory then Video",
          mixed_tags == ["Directory", "Video"], str(mixed_tags))

    # Cycle 41 adds the differentiating fixture cycle 19 lacked: row duration and
    # Media.duration deliberately disagree, and one Media omits it. Both the filter
    # and the ascending sort read the Media column on a real PMS.
    duration_rows = [catalog.items[key] for key in (103, 104, 105)]
    duration_before = [(row.get("duration"), row.get("Media")) for row in duration_rows]
    for row, row_duration, media_duration in zip(
            duration_rows, (300, 100, 50), (100, 300, None)):
        row["duration"] = row_duration
        row["Media"] = [{"id": 5000 + int(row["ratingKey"]),
                         "duration": media_duration}]
    duration_values = [sim.filter_value("duration", row) for row in duration_rows]
    duration_order = [row["ratingKey"] for row in
                      sorted(duration_rows, key=sim._sort_key("duration"))]
    for row, (old_duration, old_media) in zip(duration_rows, duration_before):
        if old_duration is None:
            row.pop("duration", None)
        else:
            row["duration"] = old_duration
        if old_media is None:
            row.pop("Media", None)
        else:
            row["Media"] = old_media
    check("duration filter and sort read Media.duration",
          duration_values == [[100], [300], [None]]
          and duration_order == ["105", "103", "104"],
          "values %s order %s" % (duration_values, duration_order))

    # Cycle 41 measured this exact plus-named download on disposable copies of both
    # supported PMS versions. The legacy filename substitutes an underscore, while
    # filename* preserves the name and percent-encodes the plus and final dot.
    disposition = getattr(server, "_download_disposition", lambda _name: None)(
        "C41C Plus+Name (2202).mkv")
    check("download disposition maps plus only in the legacy filename",
          disposition == ('attachment; filename="C41C Plus_Name (2202).mkv"; '
                          "filename*=UTF-8''C41C%20Plus%2BName%20%282202%29%2Emkv"),
          str(disposition))

    # Cycle 41 measured show merge independently on both disposable PMS versions.
    # Unlike a season merge, it is not a no-op merely because the parent has no
    # Media: matching episodes combine versions and the absorbed hierarchy vanishes.
    target_show = {
        "ratingKey": "24101", "key": "/library/metadata/24101",
        "guid": "local://24101", "type": "show", "title": "Merge Target",
        "librarySectionID": 2, "librarySectionTitle": "Feature Shows",
        "librarySectionKey": "/library/sections/2", "childCount": 1,
        "leafCount": 1, "viewedLeafCount": 0,
        "Location": [{"id": 1, "path": "/target"}],
    }
    source_show = dict(target_show, ratingKey="24102", key="/library/metadata/24102",
                       guid="local://24102", title="Merge Source",
                       Location=[{"id": 2, "path": "/source"}])
    target_season = {
        "ratingKey": "34101", "key": "/library/metadata/34101",
        "type": "season", "title": "Season 1", "index": 1,
        "parentRatingKey": "24101", "parentKey": "/library/metadata/24101",
        "librarySectionID": 2, "leafCount": 1, "viewedLeafCount": 0,
    }
    source_season = dict(target_season, ratingKey="34102",
                         key="/library/metadata/34102", parentRatingKey="24102",
                         parentKey="/library/metadata/24102")
    target_episode = {
        "ratingKey": "14101", "key": "/library/metadata/14101",
        "type": "episode", "title": "Target Episode", "index": 1,
        "parentIndex": 1, "parentRatingKey": "34101",
        "parentKey": "/library/metadata/34101", "grandparentRatingKey": "24101",
        "grandparentKey": "/library/metadata/24101", "librarySectionID": 2,
        "Media": [{"id": 141010, "Part": [{"id": 141010}]}],
    }
    source_episode = dict(target_episode, ratingKey="14102",
                          key="/library/metadata/14102", title="Source Episode",
                          parentRatingKey="34102", parentKey="/library/metadata/34102",
                          grandparentRatingKey="24102", grandparentKey="/library/metadata/24102",
                          Media=[{"id": 141020, "Part": [{"id": 141020}]}])
    for synthetic in (target_show, source_show, target_season, source_season,
                      target_episode, source_episode):
        catalog.add(synthetic)
    merge_frames = []
    sim.add_socket(lambda kind, element, rows: merge_frames.extend(rows)
                   if kind == "timeline" else None)
    merge_answer = sim.handle("PUT", "/library/metadata/24101/merge?ids=24102", b"", {})
    merge_media = [media.get("id") for media in target_episode.get("Media") or []]
    deleted_frames = merge_frames[:3]
    survivor_frames = merge_frames[3:]
    check("show merge absorbs its hierarchy and combines matching episode Media",
          merge_answer == ("empty", 200)
          and not ({24102, 34102, 14102} & set(catalog.items))
          and merge_media == [141020, 141010]
          and target_show.get("Location") == [
              {"id": 1, "path": "/target"}, {"id": 2, "path": "/source"}]
          and (target_show.get("childCount"), target_show.get("leafCount")) == (1, 1),
          "answer %r media %r locations %r" %
          (merge_answer, merge_media, target_show.get("Location")))
    check("show merge sends exact delete and survivor timeline sequence",
          [(frame.get("itemID"), frame.get("type"), frame.get("state"),
            frame.get("title"), frame.get("metadataState"))
           for frame in deleted_frames]
          == [("24102", 2, 9, "", "deleted"),
              ("34102", 3, 9, "", "deleted"),
              ("14102", 4, 9, "", "deleted")]
          and [(frame.get("itemID"), frame.get("type"), frame.get("state"),
                "metadataState" in frame)
               for frame in survivor_frames]
          == [("14101", -1, 3, False), ("14101", -1, 5, False)]
          and all(frame.get("sectionID") == "2" for frame in merge_frames),
          str(merge_frames))

    # Cycle 41 supplied the second large-library witness for Cycle 28's fitted
    # 50-row TV cap. Both PMS versions answer all 50 before paging, including when
    # legacy limit/count parameters are present; the shared 12-row shelf cap does
    # not apply to this See All endpoint.
    cap_catalog = server.Catalog(fixture)
    cap_sim = server.PlexSim(cap_catalog, Resolver(), assert_claimed=True)
    for number in range(55):
        show_key = str(25000 + number)
        season_key = str(35000 + number)
        episode_key = str(15000 + number)
        cap_catalog.add({
            "ratingKey": show_key, "key": "/library/metadata/" + show_key,
            "guid": "local://" + show_key, "type": "show",
            "title": "Cap Show %02d" % number, "librarySectionID": 99,
            "childCount": 1, "leafCount": 1, "viewedLeafCount": 0,
        })
        cap_catalog.add({
            "ratingKey": season_key, "key": "/library/metadata/" + season_key,
            "type": "season", "title": "Season 1", "index": 1,
            "parentRatingKey": show_key, "librarySectionID": 99,
            "leafCount": 1, "viewedLeafCount": 0,
        })
        cap_catalog.add({
            "ratingKey": episode_key, "key": "/library/metadata/" + episode_key,
            "type": "episode", "title": "Cap Episode %02d" % number,
            "index": 1, "parentIndex": 1, "parentRatingKey": season_key,
            "grandparentRatingKey": show_key, "librarySectionID": 99,
            "addedAt": 1789700000 + number,
        })

    def recent_cap(path, headers=None):
        return cap_sim.handle("GET", path, b"", headers or {})["MediaContainer"]

    cap_first = recent_cap("/hubs/home/recentlyAdded?type=2&sectionID=99",
                           {"X-Plex-Container-Start": "0",
                            "X-Plex-Container-Size": "100"})
    cap_after = recent_cap("/hubs/home/recentlyAdded?type=2&sectionID=99",
                           {"X-Plex-Container-Start": "50",
                            "X-Plex-Container-Size": "100"})
    cap_limit = recent_cap("/hubs/home/recentlyAdded?type=2&sectionID=99&limit=100")
    cap_count = recent_cap("/hubs/home/recentlyAdded?type=2&sectionID=99&count=100")
    check("Recently Added TV exposes and pages its measured 50-row cap",
          [(body.get("size"), body.get("totalSize"), body.get("offset"),
            len(body.get("Metadata") or []))
           for body in (cap_first, cap_after, cap_limit, cap_count)]
          == [(50, 50, 0, 50), (0, 50, 50, 0),
              (50, 50, 0, 50), (50, 50, 0, 50)],
          str([(body.get("size"), body.get("totalSize"), body.get("offset"))
               for body in (cap_first, cap_after, cap_limit, cap_count)]))

    # Cycle 41 turned Cycle 28's fitted roll-up threshold into a controlled
    # add-history measurement on both writable copies: one new episode remains an
    # episode; adding a sibling under the same season replaces it with that season.
    roll_catalog = server.Catalog(fixture)
    roll_sim = server.PlexSim(roll_catalog, Resolver(), assert_claimed=True)
    roll_show = {
        "ratingKey": "24901", "key": "/library/metadata/24901",
        "guid": "local://24901", "type": "show", "title": "Rollup Show",
        "librarySectionID": 98, "childCount": 1, "leafCount": 1,
    }
    roll_season = {
        "ratingKey": "34901", "key": "/library/metadata/34901",
        "type": "season", "title": "Season 1", "index": 1,
        "parentTitle": "Rollup Show", "parentRatingKey": "24901",
        "librarySectionID": 98, "leafCount": 1,
    }
    roll_first_episode = {
        "ratingKey": "14901", "key": "/library/metadata/14901",
        "type": "episode", "title": "Episode 1", "index": 1,
        "parentIndex": 1, "parentTitle": "Season 1",
        "grandparentTitle": "Rollup Show", "parentRatingKey": "34901",
        "grandparentRatingKey": "24901", "librarySectionID": 98,
        "addedAt": 1789800001,
    }
    for synthetic in (roll_show, roll_season, roll_first_episode):
        roll_catalog.add(synthetic)
    one_sibling = roll_sim.recently_added_tv("98")
    roll_catalog.add(dict(roll_first_episode, ratingKey="14902",
                          key="/library/metadata/14902", title="Episode 2",
                          index=2, addedAt=1789800002))
    two_siblings = roll_sim.recently_added_tv("98")
    check("recent TV rolls one new episode to itself and two siblings to the season",
          [(row.get("type"), row.get("title"), row.get("parentTitle"),
            row.get("grandparentTitle"), row.get("addedAt"))
           for row in one_sibling]
          == [("episode", "Episode 1", "Season 1", "Rollup Show", 1789800001)]
          and [(row.get("type"), row.get("title"), row.get("parentTitle"),
                row.get("addedAt")) for row in two_siblings]
          == [("season", "Season 1", "Rollup Show", 1789800002)],
          "one %r two %r" % (one_sibling, two_siblings))

    # Cycle 44: the TMDB pipeline's own output, driven through `metadata.identify` with
    # a stubbed TMDB client. Two things it got wrong, both measured on the references:
    #
    #  * a SEASON was given TMDB's season air-date year as its own `year`. 689 of zen's
    #    742 seasons carry `parentYear` and exactly ONE carries a `year` of its own, so
    #    a Plex series agent does not produce one -- and carrying it takes `parentYear`
    #    away, because the year rule reads "has a year of its own" off that column;
    #  * a SEASON and an EPISODE carried no `Guid` at all. The ids were fetched and
    #    discarded on a note claiming a real PMS leaves those arrays empty. kids' season
    #    7 answers `tmdb://3846` and `tvdb://15791` and its episode 8 answers three ids;
    #    58 of 60 zen episodes surveyed carry a block.
    import metadata as metadata_module

    class _Tmdb:
        def identify(self, parsed):
            return "tv", 1396

        def details(self, kind, tmdb_id):
            return {"id": 1396, "name": "Stub Show", "first_air_date": "2008-09-23",
                    "external_ids": {"imdb_id": "tt0903747"}, "genres": [],
                    "credits": {"cast": [], "crew": []}}

        def season(self, tmdb_id, number):
            return {"id": 3572, "air_date": "2009-03-08",
                    "external_ids": {"tvdb_id": 15791},
                    "overview": "Stub season."}

        def episode(self, tmdb_id, season, number):
            return {"id": 62085, "name": "Stub Episode", "air_date": "2009-03-08",
                    "external_ids": {"imdb_id": "tt1234567", "tvdb_id": 297989}}

    probe_row = {"title": "Stub Show S01E07", "type": "episode"}
    parsed = {"title": "Stub Show", "year": None, "season": 1, "episode": 7}
    identified = False
    try:
        identified = metadata_module.enrich(probe_row, _Tmdb(), parsed)
    except Exception as error:                       # pragma: no cover - reported below
        check("the pipeline identifies a stub episode", False, repr(error))
    if identified:
        check("an identified episode carries its own Guid",
              [g["id"] for g in probe_row.get("Guid") or []]
              == ["imdb://tt1234567", "tmdb://62085", "tvdb://297989"],
              str(probe_row.get("Guid")))
        check("the pipeline stores no season year of its own",
              "_season_year" not in probe_row, str(probe_row.get("_season_year")))
        check("the pipeline stores the season's external ids",
              [g["id"] for g in probe_row.get("_season_guids") or []]
              == ["tmdb://3572", "tvdb://15791"], str(probe_row.get("_season_guids")))
    # And the LOAD-time correction, for a season already written with its own year.
    built = [
        {"ratingKey": "9001", "type": "show", "title": "Load Show", "year": 2008,
         "guid": "plex://show/aaaaaaaaaaaaaaaaaaaaaaaa",
         "key": "/library/metadata/9001/children", "librarySectionID": 2},
        {"ratingKey": "9002", "type": "season", "title": "Season 1", "index": 1,
         "year": 2009, "parentRatingKey": "9001", "parentTitle": "Load Show",
         "guid": "plex://season/bbbbbbbbbbbbbbbbbbbbbbbb",
         "key": "/library/metadata/9002/children", "librarySectionID": 2},
    ]
    server.hierarchy_module.normalize_rows(built)
    check("a season written with its own year loads with parentYear instead",
          built[1].get("year") is None and built[1].get("parentYear") == 2008,
          "year=%r parentYear=%r" % (built[1].get("year"), built[1].get("parentYear")))

    if failures:
        print("FAIL %d catalog feature check(s)" % len(failures))
        return 1
    print("PASS all catalog feature checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
