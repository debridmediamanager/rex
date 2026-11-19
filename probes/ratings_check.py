#!/usr/bin/env python3
"""Check a running rex's ratings against what Plex's agent says of the same titles.

Read-only. Takes a sample of a rex's films, shows and their first episode, asks each detail as
Plex Web does, asks Plex's agent (metadata.provider.plex.tv) for the same guid, and checks the
rating fields against the rule measured on real PMSes (findings, "Ratings"): a film's `rating`,
`audienceRating`, their images and `Rating` as the agent gives them; a show's and an episode's
`audienceRating` its own TMDB score, with no `rating`; every value to one decimal as `%.1f`.

A row that breaks the rule in itself is a FAIL: a score with no image (the form rex answered
before 2026-11-19), a show's or episode's critic score or non-TMDB audience score, a score that is
not its source's entry in `Rating`. A row that keeps the rule but differs from what the agent says
NOW is STALE, not failed: rex describes a title from the answer it kept when it named it, and the
agent's scores and sources move since (rex never asks again).

`--reference` adds a real PMS holding the same titles (a library shared with you is enough): each
title is looked up there by guid and its fields shown beside rex's.

    T=$(ssh old-mac-mini 'docker exec rex cat /config/claim_state.json' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
    python3 probes/ratings_check.py --rex https://old-mac-mini.taile98a97.ts.net:32400 --token "$T" --sample 40
"""

import argparse
import json
import random
import sys
import urllib.parse
import urllib.request

AGENT = "https://metadata.provider.plex.tv"
FIELDS = ("rating", "ratingImage", "audienceRating", "audienceRatingImage", "Rating")
DETAIL = ("includeConcerts=1&includeExtras=1&includeOnDeck=1&includePopularLeaves=1&includePreferences=1&includeReviews=1"
          "&includeChapters=1&includeStations=1&includeExternalMedia=1&asyncAugmentMetadata=1&asyncCheckFiles=1"
          "&asyncRefreshAnalysis=1&asyncRefreshLocalMediaAgent=1")


def get(base, path, token):
    url = base + path + ("&" if "?" in path else "?") + "X-Plex-Token=" + urllib.parse.quote(token)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "X-Plex-Client-Identifier": "rex-ratings-check",
                                                   "X-Plex-Product": "Plex Web", "X-Plex-Language": "en"})
    try:
        container = json.load(urllib.request.urlopen(request, timeout=60)).get("MediaContainer")
        return container if isinstance(container, dict) else {}
    except Exception as error:  # a title the agent or a reference does not know is a miss, not a stop
        print("  ! %s: %s" % (path.split("?")[0], error), file=sys.stderr)
        return {}


def one(value):
    return float("%.1f" % value) if isinstance(value, (int, float)) else None


def expected(agent, kind):
    """The rule rex answers by, from the agent's own row."""
    rating = [dict(entry, value=one(entry.get("value"))) for entry in agent.get("Rating") or []]
    if kind == "movie":
        out = {"rating": one(agent.get("rating")), "ratingImage": agent.get("ratingImage"),
               "audienceRating": one(agent.get("audienceRating")), "audienceRatingImage": agent.get("audienceRatingImage")}
    else:
        tmdb = next((entry for entry in rating if str(entry.get("image", "")).startswith("themoviedb://")), {})
        out = {"rating": None, "ratingImage": None, "audienceRating": tmdb.get("value"), "audienceRatingImage": tmdb.get("image")}
    out["Rating"] = rating or None
    return out


def ratings(row):
    return {field: row.get(field) for field in FIELDS}


def shape(values):
    """What must match exactly: which fields there are, and each score's source."""
    out = {field: values[field] if field.endswith("Image") else values[field] is not None for field in FIELDS}
    out["Rating"] = sorted((entry.get("image"), entry.get("type")) for entry in values["Rating"] or [])
    return out


def broken(values, kind):
    """Why a row breaks the rule on its own, or None."""
    listed = {entry.get("image"): entry.get("value") for entry in values["Rating"] or []}
    scores = [values["rating"], values["audienceRating"], *(entry.get("value") for entry in values["Rating"] or [])]
    if any(isinstance(score, (int, float)) and score != one(score) for score in scores):
        return "a score with more than one decimal"
    for score, image in (("rating", "ratingImage"), ("audienceRating", "audienceRatingImage")):
        if (values[score] is None) != (values[image] is None):
            return "%s without %s" % ((score, image) if values[score] is not None else (image, score))
        if values[image] is not None and listed.get(values[image]) != values[score]:
            return "%s is not its source's entry in Rating" % score
    if kind != "movie" and values["rating"] is not None:
        return "a %s with a critic score" % kind
    if kind != "movie" and values["audienceRatingImage"] not in (None, "themoviedb://image.rating"):
        return "a %s's audience score is not TMDB's" % kind
    return None


def numbers(values):
    return {"rating": values["rating"], "audienceRating": values["audienceRating"],
            "Rating": sorted((entry.get("image"), entry.get("value")) for entry in values["Rating"] or [])}


def titles(args):
    sections = get(args.rex, "/library/sections", args.token).get("Directory") or []
    rows = []
    for section in sections:
        if section.get("type") not in ("movie", "show"):
            continue
        listed = get(args.rex, "/library/sections/%s/all?type=%d" % (section["key"], 1 if section["type"] == "movie" else 2), args.token)
        rows += [row for row in listed.get("Metadata") or [] if str(row.get("guid", "")).startswith("plex://")]
    random.Random(args.seed).shuffle(rows)
    films = [row for row in rows if row["type"] == "movie"][:args.sample]
    shows = [row for row in rows if row["type"] == "show"][:max(1, args.sample // 2)]
    out = [(row["ratingKey"], row["title"]) for row in films]
    for show in shows:
        out.append((show["ratingKey"], show["title"]))
        season = next((row for row in get(args.rex, "/library/metadata/%s/children" % show["ratingKey"], args.token).get("Metadata") or []
                       if row.get("index") != 0), None)
        episode = season and next(iter(get(args.rex, "/library/metadata/%s/children" % season["ratingKey"], args.token).get("Metadata") or []), None)
        if episode:
            out.append((episode["ratingKey"], "%s S%sE%s" % (show["title"], season.get("index"), episode.get("index"))))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rex", required=True, help="rex's address, e.g. https://host:32400")
    parser.add_argument("--token", required=True, help="a token rex takes (its claim's server token)")
    parser.add_argument("--sample", type=int, default=30, help="films to check; half as many shows, each with one episode")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--reference", help="a real PMS holding the same titles, shown beside rex")
    parser.add_argument("--reference-token")
    args = parser.parse_args()

    failed = stale = checked = 0
    for key, title in titles(args):
        row = (get(args.rex, "/library/metadata/%s?%s" % (key, DETAIL), args.token).get("Metadata") or [{}])[0]
        kind, guid = row.get("type"), row.get("guid", "")
        agent = (get(AGENT, "/library/metadata/%s" % guid.rsplit("/", 1)[-1], args.token).get("Metadata") or [None])[0]
        checked += 1
        ours = ratings(row)
        why = broken(ours, kind)
        want = expected(agent, kind) if agent else None
        if why:
            failed += 1
            print("FAIL   %-8s %s: %s\n         rex   %s\n         agent %s" % (kind, title, why, ours, want))
        elif not agent:
            print("OK     %-8s %s: the agent has no row for %s to compare with" % (kind, title, guid))
        elif shape(ours) != shape(want) or numbers(ours) != numbers(want):
            stale += 1
            print("STALE  %-8s %s: rex %s\n         the agent now %s" % (kind, title, ours, want))
        else:
            print("OK     %-8s %s: %s %s" % (kind, title, ours["audienceRating"], ours["audienceRatingImage"]))
        if args.reference:
            found = get(args.reference, "/library/all?guid=%s" % urllib.parse.quote(guid, safe=""), args.reference_token or "")
            ref = (found.get("Metadata") or [None])[0]
            if ref:
                ref = (get(args.reference, "/library/metadata/%s?%s" % (ref["ratingKey"], DETAIL), args.reference_token or "").get("Metadata") or [{}])[0]
                same = "same" if shape(ratings(ref)) == shape(ours) else "DIFFERS"
                print("         reference (%s) %s" % (same, ratings(ref)))
    print("\n%d checked: %d ok, %d stale, %d failed" % (checked, checked - failed - stale, stale, failed))
    sys.exit(1 if failed or not checked else 0)


if __name__ == "__main__":
    main()
