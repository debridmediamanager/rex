#!/usr/bin/env python3
"""Focused regressions for catalog shapes absent from the ordinary UI fixture."""

import os
import re
import sys
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

    if failures:
        print("FAIL %d catalog feature check(s)" % len(failures))
        return 1
    print("PASS all catalog feature checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
