#!/usr/bin/env python3
"""The rest of a Plex client's walk answers without copying the library (the production server, 2026-10-11).

A replay of the requests Plex for Mac sends, against the production rex after the Recommended
fix: search took 4.6 s for "the", Home 1.0-1.4 s, Home's movie Recently Added 1.3-1.7 s,
every movie's Related shelf 0.6-1.1 s, `/library/sections` 0.65 s (the apps poll it), and
a movie library's `firstCharacter` and genre lists 0.6-0.9 s. Each copied every row it
read -- and Continue Watching, which Home runs three times and every item page once,
grouped every episode of the library -- or projected rows it then dropped.

The library is `test_perf_section_hubs.py`'s: the production server's first 120 shows, every episode
one real row, and 1,500 copies of one real movie row. Bounds are fractions of an unpaged
movie listing measured first in the same run. Measured on the 1.42.2 reference (unfixed -> fixed,
units): sections 0.59 -> 0.01, Home 0.35 -> 0.04, movie Recently Added 0.78 -> 0.01, Related
0.99 -> 0.09, firstCharacter 0.58 -> 0.01, genres 0.33 -> 0.01, search 1.83 -> 0.12.

    python3 tests/test_perf_client_walk.py
"""
import os
import shutil
import sys
import tempfile

from test_perf_listing_window import check, container, median, results, start
from test_perf_section_hubs import catalog

BOUNDS = [
    ("/library/sections", "the library list", 0.15),
    ("/hubs?count=12&includeLibraryPlaylists=1&includeStations=1&includeRecentChannels=1"
     "&includeMeta=1&excludeFields=summary", "Home", 0.2),
    ("/hubs/home/recentlyAdded?type=1&count=12", "Home's movie Recently Added", 0.15),
    ("/library/metadata/500007/related?includeAugmentations=1&includeExternalMetadata=1&includeMeta=1",
     "a movie's Related shelf", 0.4),
    ("/library/sections/1/firstCharacter?sort=titleSort&includeCollections=1&includeExternalMedia=1",
     "the movie library's A-Z bar", 0.15),
    ("/library/sections/1/genre?type=1", "the movie library's genre list", 0.15),
    ("/hubs/search?query=movie&limit=30&includeCollections=1&includeExternalMedia=1",
     "a search matching every movie", 0.5),
]


def run(port):
    _, body, unit = median(port, "/library/sections/1/all")
    print("unit (unpaged movie listing, %d rows): %.3fs"
          % (len(container(body).get("Metadata", [])), unit), flush=True)
    for path, name, bound in BOUNDS:
        status, body, took = median(port, path)
        answer = container(body)
        filled = any(answer.get(field) for field in ("Directory", "Metadata", "Hub"))
        check("%s is under %.2f units (%.3fs, %.2f)" % (name, bound, took, took / unit),
              status == 200 and filled and took < bound * unit, (status, filled, took))


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-walk-")
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
