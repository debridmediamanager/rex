#!/usr/bin/env python3
"""The production server's slowest client requests answer without copying the library (2026-10-17).

A replay of every request shape fun's clients sent, against a copy of its library (42,806 rows),
found these copying or rescanning the whole library for a short answer (seconds on the Mac copy;
fun, swapping, took ten to forty times as long and every other request queued behind them):

- a show library's Recently Added and Recently Released pages of 50: every episode copied and
  sorted by titleSort before the page (1.0 s / 1.7 s, +1.5 / +1.9 GB);
- a show library's Unplayed list: every episode of every show copied to read its view count;
- the movie Recently Added shelf of `/hubs/promoted`: every movie copied twice and the first
  list thrown away under the pin every client sends (+320 MB);
- `/library/recentlyAdded`: every movie and season copied to list 50.

The library is `test_perf_section_hubs.py`'s, with distinct episode dates, a watched episode in
every fifth show and a resume point on every tenth movie. Bounds are fractions of an unpaged movie
listing measured first in the same run. Measured on the Mac (unfixed -> fixed, units): Recently
Added 0.28 -> 0.05, Recently Released 0.67 -> 0.06, Unplayed 0.21 -> 0.02, the promoted shelf
0.23 -> 0.01, the server's Recently Added 0.15 -> 0.05.

    python3 tests/test_perf_production_walk.py
"""
import json
import shutil
import sys
import tempfile

from test_perf_listing_window import check, container, median, results, start
from test_perf_section_hubs import catalog as hubs_catalog

PIN = "&pinnedContentDirectoryID=1%2C2&includeMeta=1&excludeFields=summary&count=12&excludeContinueWatching=1"
BOUNDS = [
    ("/library/sections/2/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=50",
     "a show library's Recently Added page", 0.15),
    ("/library/sections/2/newest?X-Plex-Container-Start=0&X-Plex-Container-Size=50",
     "a show library's Recently Released page", 0.25),
    ("/library/sections/2/unwatched?X-Plex-Container-Size=50", "a show library's Unplayed list", 0.1),
    ("/hubs/promoted?contentDirectoryID=1" + PIN, "the movie library's promoted shelf", 0.1),
    ("/library/recentlyAdded", "the server's Recently Added", 0.1),
]


def catalog(directory):
    path, episodes = hubs_catalog(directory)
    data = json.load(open(path))
    shows = [row for row in data["items"] if row.get("type") == "show"]
    watched = {row["ratingKey"] for number, row in enumerate(shows) if number % 5 == 0}
    first = set()
    for number, row in enumerate(data["items"]):
        if row.get("type") == "episode" and str(row.get("librarySectionID")) == "2":
            row["addedAt"] = 1789180050 - number * 60
            show = row.get("grandparentRatingKey")
            if show in watched and show not in first:
                first.add(show)
                row.update({"viewCount": 1, "lastViewedAt": 1792000000 - number})
        elif row.get("type") == "movie" and int(row["ratingKey"]) % 10 == 0:
            row.update({"viewOffset": 600000, "lastViewedAt": 1792000000 - number})
    with open(path, "w") as handle:
        json.dump(data, handle)
    return path, episodes


def run(port):
    _, body, unit = median(port, "/library/sections/1/all")
    print("unit (unpaged movie listing, %d rows): %.3fs"
          % (len(container(body).get("Metadata", [])), unit), flush=True)
    for path, name, bound in BOUNDS:
        status, body, took = median(port, path)
        answer = container(body)
        filled = any(answer.get(field) for field in ("Metadata", "Hub"))
        check("%s is under %.2f units (%.3fs, %.2f)" % (name, bound, took, took / unit),
              status == 200 and filled and took < bound * unit, (status, filled, took))


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-walk-1017-")
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
