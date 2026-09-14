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
YELLOW = "\033[33m"
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
        # Only provable on a show with more than one season -- against a
        # single-season catalog this check asserted seasons[1] and failed for
        # want of data, which is noise rather than a regression.
        if len(seasons) < 2:
            print("  %sSKIP%s  %-46s %s" % (YELLOW, OFF, "second page is honoured",
                                            "needs a show with two or more seasons"))
        else:
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


    # Cycle 16, measured on kids (season 7, which owns no art) and zen (season 11114,
    # which owns its own, and its episode 11115): a row that BORROWS its background
    # carries the owner's path verbatim -- kids' season 7 and episode 8 both carry
    # `/library/metadata/6/art/...` -- and its own `/art` is a 404. plexsim's
    # `_stamp_artwork` re-keyed every artwork URL to the row holding it, so a season
    # and its show claimed to own the same image under two paths and
    # `/library/metadata/<season>/art` answered 200 where a real PMS answers 404 --
    # fix 4's trap, a 200 reading to the client as "this row has a background of its
    # own".
    base_c16f = args.base
    sections_c16f = get(base_c16f, "/library/sections")[1]
    show_section_c16f = next(d_c16f["key"] for d_c16f in sections_c16f.get("Directory") or []
                             if d_c16f.get("type") == "show")
    show_c16f = (get(base_c16f, "/library/sections/%s/all?type=2" % show_section_c16f)[1]
                 .get("Metadata") or [])[0]["ratingKey"]
    detail_c16f = get(base_c16f, "/library/metadata/%s" % show_c16f)[1]["Metadata"][0]
    season_c16f = (get(base_c16f, "/library/metadata/%s/children?excludeAllLeaves=1" % show_c16f)[1]
                   .get("Metadata") or [])[0]["ratingKey"]
    episode_c16f = (get(base_c16f, "/library/metadata/%s/children" % season_c16f)[1]
                    .get("Metadata") or [])[0]
    def art_status_c16f(key_c16f):
        # the suite's own get() parses JSON, so a 200 carrying image bytes reads as
        # None; this one wants the status and nothing else
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    base_c16f + "/library/metadata/%s/art" % key_c16f,
                    headers={"User-Agent": UA}), timeout=30) as response_c16f:
                return response_c16f.status
        except urllib.error.HTTPError as error_c16f:
            return error_c16f.code

    status_c16f = tuple(art_status_c16f(key_c16f)
                        for key_c16f in (show_c16f, season_c16f, episode_c16f["ratingKey"]))
    season_row_c16f = get(base_c16f, "/library/metadata/%s" % season_c16f)[1]["Metadata"][0]
    check("a borrowed background keeps its owner's path and 404s on the borrower",
          bool(detail_c16f.get("art"))
          and season_row_c16f.get("art") == detail_c16f.get("art")
          and episode_c16f.get("art") == detail_c16f.get("art")
          and status_c16f == (200, 404, 404),
          "show=%r season=%r episode=%r statuses=%s"
          % (detail_c16f.get("art"), season_row_c16f.get("art"),
             episode_c16f.get("art"), status_c16f))

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
