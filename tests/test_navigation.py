#!/usr/bin/env python3
"""Replay the request sequence a client makes browsing a TV library.

Each step is the exact request Plex for Mac / Plex Web was observed making, and
each response is checked against the shape a real PMS returns for the same step
(captured from fun). Key-level comparison is not enough: the `viewMode` bug was
invisible to it, because both sides HAD the field and only the values were
swapped -- a season list is 65593 and an episode list 65592, and plexsim had
them inverted, so the client was handed the episode layout for a season list and
drew no seasons.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GREEN = "\033[32m"
RED = "\033[31m"
OFF = "\033[0m"

UA = "Plex/1.115.0 (Macintosh; plexsim-test)"

# The params the real clients were logged sending.
SHOW_DETAIL = ("includeConcerts=1&includeExtras=1&includeOnDeck=1&includePopularLeaves=1"
               "&includePreferences=1&includeReviews=1&includeChapters=1&includeStations=1"
               "&includeExternalMedia=1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print("  %s  %-46s %s" % (GREEN + "PASS" + OFF if ok else RED + "FAIL" + OFF, label, detail))


def get(base, path):
    url = base + path
    request = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response).get("MediaContainer", {})
    except urllib.error.HTTPError as error:
        return error.code, {}
    except Exception:
        return None, {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:32400")
    parser.add_argument("--catalog", default=os.path.join(ROOT, "catalog.json"))
    args = parser.parse_args()

    document = json.load(open(args.catalog))
    shows = [r for r in document["items"] if r.get("type") == "show"]
    if not shows:
        print("no shows in the catalog")
        return 1

    for show in shows:
        show_key = show["ratingKey"]
        print("\n=== %s (rk=%s) ===" % (show["title"], show_key))

        # 1. the show detail page
        status, detail = get(args.base, "/library/metadata/%s?%s" % (show_key, SHOW_DETAIL))
        row = (detail.get("Metadata") or [{}])[0]
        check("show detail 200", status == 200, "HTTP %s" % status)
        check("show row type=show", row.get("type") == "show", str(row.get("type")))
        check("childCount matches seasons", row.get("childCount") == show.get("childCount"),
              "childCount=%s" % row.get("childCount"))
        check("leafCount matches episodes", row.get("leafCount") == show.get("leafCount"),
              "leafCount=%s" % row.get("leafCount"))
        on_deck = (row.get("OnDeck") or {}).get("Metadata")
        check("OnDeck present", bool(on_deck),
              "S%02dE%02d %s" % (on_deck.get("parentIndex") or 0, on_deck.get("index") or 0,
                                 on_deck.get("title")) if on_deck else "missing")

        # 2. the season list -- the step that was rendering empty
        status, children = get(args.base,
                               "/library/metadata/%s/children?excludeAllLeaves=1" % show_key)
        seasons = children.get("Metadata") or []
        check("children 200", status == 200, "HTTP %s" % status)
        check("viewGroup=season", children.get("viewGroup") == "season",
              str(children.get("viewGroup")))
        # The bug: fun returns 65593 for a season list. 65592 is the episode layout.
        check("viewMode=65593 (season layout)", children.get("viewMode") == 65593,
              str(children.get("viewMode")))
        check("season count matches childCount", len(seasons) == (show.get("childCount") or 0),
              "%d season(s)" % len(seasons))
        check("container parentTitle is the show", children.get("parentTitle") == show["title"],
              str(children.get("parentTitle")))
        check("every season has an index", all(s.get("index") for s in seasons),
              ", ".join("S%s" % s.get("index") for s in seasons))
        check("every season has leafCount", all(s.get("leafCount") for s in seasons),
              ", ".join(str(s.get("leafCount")) for s in seasons))
        check("every season has a thumb", all(s.get("thumb") for s in seasons))
        check("season thumbs are versioned (not /1)",
              all(not str(s.get("thumb", "")).endswith("/1") for s in seasons),
              "stale-cache guard")

        # 2b. the SAME request the web client actually sends, with paging.
        # Plex Web asks with X-Plex-Container-Start/Size, and its pre-play
        # descendant list renders `null` unless the container carries a truthy
        # `totalSize` (chunk-4304: `s && c && d && d.totalSize ? <list> : null`,
        # fed by `null == e.totalSize ? void 0 : parseInteger(e.totalSize)`).
        # Without these two fields every season row above is correct and the
        # Seasons block still does not appear.
        status, paged = get(args.base,
                            "/library/metadata/%s/children?excludeAllLeaves=1"
                            "&X-Plex-Container-Start=0&X-Plex-Container-Size=20" % show_key)
        check("paged children 200", status == 200, "HTTP %s" % status)
        check("paged children carry totalSize",
              paged.get("totalSize") == (show.get("childCount") or 0),
              "totalSize=%s" % paged.get("totalSize"))
        check("paged children carry offset", paged.get("offset") == 0,
              "offset=%s" % paged.get("offset"))
        check("unpaged children omit totalSize/offset",
              "totalSize" not in children and "offset" not in children,
              "a real PMS adds them only when the client pages")

        # A second page must actually move the window, not resend page one.
        status, page2 = get(args.base,
                            "/library/metadata/%s/children?excludeAllLeaves=1"
                            "&X-Plex-Container-Start=1&X-Plex-Container-Size=1" % show_key)
        rows2 = page2.get("Metadata") or []
        check("second page is honoured",
              status == 200 and page2.get("offset") == 1 and page2.get("size") == 1
              and len(rows2) == 1
              and rows2[0].get("ratingKey") == seasons[1].get("ratingKey")
              and page2.get("totalSize") == (show.get("childCount") or 0),
              "offset=%s size=%s totalSize=%s" % (page2.get("offset"), page2.get("size"),
                                                  page2.get("totalSize")))

        # 3. each season's episodes
        total_episodes = 0
        for season in seasons:
            status, eps = get(args.base, "/library/metadata/%s/children" % season["ratingKey"])
            rows = eps.get("Metadata") or []
            total_episodes += len(rows)
            label = "%s -> %d episode(s)" % (season["title"], len(rows))
            ok = (status == 200
                  and eps.get("viewGroup") == "episode"
                  and eps.get("viewMode") == 65592
                  and len(rows) == season.get("leafCount")
                  and all(e.get("type") == "episode" for e in rows)
                  and all(e.get("index") for e in rows)
                  and all(e.get("Media") for e in rows)
                  and all(e.get("grandparentTitle") == show["title"] for e in rows))
            check(label, ok, "viewMode=%s %s" % (
                eps.get("viewMode"),
                ", ".join("S%02dE%02d" % (e.get("parentIndex") or 0, e.get("index") or 0)
                          for e in rows)))

        check("episodes across seasons == leafCount",
              total_episodes == (show.get("leafCount") or 0),
              "%d total" % total_episodes)

        # 4. allLeaves, which some clients use instead of per-season calls
        status, leaves = get(args.base, "/library/metadata/%s/allLeaves" % show_key)
        check("allLeaves returns every episode",
              status == 200 and len(leaves.get("Metadata") or []) == (show.get("leafCount") or 0),
              "%d" % len(leaves.get("Metadata") or []))

        # 5. endpoints the client calls that must not 500
        for path, expect in (("/library/metadata/%s/related?includeAugmentations=1" % show_key, 200),
                             ("/library/metadata/%s/userState" % show_key, 404)):
            status, _ = get(args.base, path)
            check("%s -> %s" % (path.split("?")[0].split("/")[-1], expect),
                  status == expect, "HTTP %s" % status)

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("\n%s  %d/%d checks passed" % (GREEN + "PASS" + OFF if passed == total
                                         else RED + "FAIL" + OFF, passed, total))
    for label, ok in results:
        if not ok:
            print("   failed: %s" % label)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
