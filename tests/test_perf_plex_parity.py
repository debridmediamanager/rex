#!/usr/bin/env python3
"""The requests a real PMS answered faster than rex on the same library answer at its speed (2026-10-17).

fun's library in a real Plex Media Server 1.42.2 beside rex on the same host: Plex answered Continue
Watching in 6 ms (rex 0.29 s), a guid lookup in 5 ms (rex 35-390 ms), a show library's Recently
Released page in 64 ms (rex 0.61 s), its Recently Added page in 64 ms (rex 0.12 s), Home in 53 ms
(rex 0.11 s) and a two-word search in 0.19 s (rex 0.44 s). Each was rex rebuilding from the whole
library what changes only with it: every Continue Watching row projected to show six, every
episode sorted to answer one guid or one page, every title and tag folded again per search.

The library is `test_perf_production_walk.py`'s. Bounds are multiples of this client parsing an
unpaged movie listing (`client_unit`). Every request is read once before it is timed, as a client
reads Home and its pages again and again; the first read after a library change still costs the
pass the kept answers save. Measured on the Mac over two runs (unfixed -> fixed, units): Continue
Watching 6.7-7.0 -> 0.10-0.11, Home 0.40-0.46 -> 0.21, the guid lookups 0.24-0.35 -> 0.015-0.021,
Recently Added 0.21-0.27 -> 0.11, Recently Released 0.35-0.37 -> 0.11, the search 1.0-1.1 -> 0.08.

    python3 tests/test_perf_plex_parity.py
"""
import shutil
import sys
import tempfile

from test_perf_listing_window import check, client_unit, container, median, request, results, start
from test_perf_production_walk import catalog

GUID = "guid=plex%3A%2F%2F{}%2F{:024x}"
BOUNDS = [
    ("/hubs/continueWatching/items?X-Plex-Container-Start=0&X-Plex-Container-Size=6",
     "Continue Watching's six", 1.0),
    ("/hubs?count=12&includeMeta=1&excludeFields=summary", "Home", 0.3),
    ("/library/all?type=4&" + GUID.format("episode", 300003), "an episode guid lookup", 0.08),
    ("/library/all?" + GUID.format("show", 300001) + "&includeGuids=1", "an untyped guid lookup", 0.08),
    ("/library/sections/2/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=50",
     "a show library's Recently Added page", 0.16),
    ("/library/sections/2/newest?X-Plex-Container-Start=0&X-Plex-Container-Size=50",
     "a show library's Recently Released page", 0.2),
    ("/hubs/search?query=show%20001&limit=30", "a two-word search", 0.4),
]


def run(port):
    _, body, _ = median(port, "/library/sections/1/all")
    unit = client_unit(body)
    print("unit (parsing the unpaged movie listing here): %.4fs" % unit, flush=True)
    for path, name, bound in BOUNDS:
        request(port, path)
        status, body, took = median(port, path)
        answer = container(body)
        filled = any(answer.get(field) for field in ("Metadata", "Hub"))
        check("%s is under %.2f units (%.4fs, %.3f)" % (name, bound, took, took / unit),
              status == 200 and filled and took < bound * unit, (status, filled, took))
    # A page kept from the library still counts all of it.
    _, page, _ = median(port, "/library/sections/2/newest?X-Plex-Container-Start=0&X-Plex-Container-Size=50", 1)
    _, whole, _ = median(port, "/library/sections/2/newest?X-Plex-Container-Start=0&X-Plex-Container-Size=0", 1)
    check("a Recently Released page reports the whole list's totalSize",
          container(page).get("totalSize") == container(whole).get("totalSize") and container(page).get("totalSize", 0) > 50,
          (container(page).get("totalSize"), container(whole).get("totalSize")))


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-plex-")
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
