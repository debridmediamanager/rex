#!/usr/bin/env python3
"""Focused regressions for catalog shapes absent from the ordinary UI fixture."""

import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("PLEXSIM_TEST_ROOT", os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import build_catalog  # noqa: E402
import server  # noqa: E402


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

    if failures:
        print("FAIL %d catalog feature check(s)" % len(failures))
        return 1
    print("PASS all catalog feature checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
