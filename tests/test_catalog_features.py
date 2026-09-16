#!/usr/bin/env python3
"""Focused regressions for catalog shapes absent from the ordinary UI fixture."""

import os
import re
import sys
import urllib.parse

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

    if failures:
        print("FAIL %d catalog feature check(s)" % len(failures))
        return 1
    print("PASS all catalog feature checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
