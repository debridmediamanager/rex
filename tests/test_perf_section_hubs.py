#!/usr/bin/env python3
"""A library's Recommended page projects only the rows its shelves show (fun, 2026-10-11).

fun-simplex, fed by zurg: 591 shows, 27,040 episodes and 3,346 movies. Plex for Mac's
Recommended tab (`/hubs/sections/{id}?count=12`) took 12-37 s and the Library tab queued
behind it on the runtime lock: every shelf copied the whole section and projected every
row of it -- Recently Released all 27k episodes -- before keeping 12. A filtered shelf
listing (`unwatched=1&audienceRating>=7`) copied the whole row once per filter rule.

The library is fun's first 120 shows (`rust/tests/fixtures/fun_show_shape.json`, every
episode a copy of one real episode row, `fixtures/fun_episode_row.json`) and 1,500 copies
of one real movie row (`fixtures/fun_movie_row.json`). Bounds are fractions of an unpaged
listing of the same section measured first in the same run, so a slower host moves them.
Measured on zen: unfixed, TV hubs cost 1.71 units, movie hubs 6.34 and the filtered page
0.52; fixed, 0.02, 0.06 and 0.02.

    python3 tests/test_perf_section_hubs.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile

from test_perf_listing_window import catalog as show_catalog, check, container, median, results, start

HERE = os.path.dirname(os.path.abspath(__file__))
MOVIES = 1500
CLIENT = ("&includeLibraryPlaylists=1&includeStations=1&includeRecentChannels=1&includeMeta=1"
          "&includeExternalMetadata=1&excludeFields=summary&X-Plex-Product=Plex%20for%20Mac")


def catalog(directory):
    """fun's shows, plus fun's movie row repeated with spread dates, ratings and genres."""
    path, episodes = show_catalog(directory)
    data = json.load(open(path))
    movie = json.load(open(os.path.join(HERE, "fixtures", "fun_movie_row.json")))
    genres = movie["Genre"]
    for number in range(MOVIES):
        row = copy.deepcopy(movie)
        own = str(500000 + number)
        row.update({"ratingKey": own, "key": "/library/metadata/%s" % own,
                    "guid": "plex://movie/%024x" % int(own), "title": "Movie %04d" % number,
                    "titleSort": "Movie %04d" % number, "librarySectionID": 1,
                    "addedAt": movie["addedAt"] - number * 3600,
                    "originallyAvailableAt": "%04d-06-29" % (2026 - number % 40),
                    "year": 2026 - number % 40, "audienceRating": 5.0 + (number % 50) / 10,
                    "Genre": genres[: 1 + number % len(genres)]})
        data["items"].append(row)
    with open(path, "w") as handle:
        json.dump(data, handle)
    return path, episodes


def run(port):
    _, body, unit_tv = median(port, "/library/sections/2/all?type=4")
    print("TV unit (unpaged episode listing, %d rows): %.3fs"
          % (len(container(body).get("Metadata", [])), unit_tv), flush=True)
    _, body, unit_movies = median(port, "/library/sections/1/all")
    print("movie unit (unpaged movie listing, %d rows): %.3fs"
          % (len(container(body).get("Metadata", [])), unit_movies), flush=True)

    status, body, took = median(port, "/hubs/sections/2?count=12" + CLIENT)
    hubs = container(body).get("Hub", [])
    aired = next((hub for hub in hubs if hub["hubIdentifier"] == "tv.recentlyaired.2"), {})
    check("TV Recommended is under 0.25 units (%.3fs, %.2f)" % (took, took / unit_tv),
          status == 200 and len(aired.get("Metadata", [])) == 12 and aired.get("more") is True
          and took < 0.25 * unit_tv, (status, len(aired.get("Metadata", [])), took))

    status, body, took = median(port, "/hubs/sections/1?count=12" + CLIENT)
    hubs = container(body).get("Hub", [])
    added = next((hub for hub in hubs if hub["hubIdentifier"] == "movie.recentlyadded.1"), {})
    check("Movie Recommended is under 0.25 units (%.3fs, %.2f)" % (took, took / unit_movies),
          status == 200 and len(added.get("Metadata", [])) == 12 and took < 0.25 * unit_movies,
          (status, len(added.get("Metadata", [])), took))
    # The shelf is the head of its own key's listing, newest first.
    _, listing, _ = median(port, "/library/sections/1/all?sort=addedAt:desc"
                                 "&X-Plex-Container-Start=0&X-Plex-Container-Size=12", 1)
    expected = [row["ratingKey"] for row in container(listing).get("Metadata", [])]
    got = [row["ratingKey"] for row in added.get("Metadata", [])]
    check("Recently Added is its listing's first 12 rows", got == expected, (got[:3], expected[:3]))

    page = ("/library/sections/1/all?unwatched=1&audienceRating%3E=7.0&sort=audienceRating:desc"
            "&X-Plex-Container-Start=12&X-Plex-Container-Size=24")
    status, body, took = median(port, page)
    rows = container(body).get("Metadata", [])
    check("a filtered shelf page is under 0.2 units (%.3fs, %.2f)" % (took, took / unit_movies),
          status == 200 and len(rows) == 24 and took < 0.2 * unit_movies
          and all(row.get("audienceRating", 0) >= 7.0 for row in rows), (status, len(rows), took))


def main():
    directory = tempfile.mkdtemp(prefix="simplex-perf-hubs-")
    process = None
    try:
        path, _ = catalog(directory)
        process, port = start(path)
        run(port)
    finally:
        if process is not None:
            process.kill()
            process.wait()
        shutil.rmtree(directory, ignore_errors=True)
    print("%d/%d passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
