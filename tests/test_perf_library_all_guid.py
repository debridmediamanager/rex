#!/usr/bin/env python3
"""A `/library/all` guid lookup copies no rows it does not answer (the production server, 2026-10-17).

Plex clients ask `/library/all?type=2&guid=plex://show/...` (and type 4, type 1 and untyped) to
learn whether the server holds a title. On the production rex (42,806 rows) it answered 212
bytes in 67 s, and every other request queued behind it on the runtime lock: the query cloned every row of the library, sorted them all
by key and then by titleSort, and only then dropped the rows of other types and filtered the guid.

The library is `test_perf_section_hubs.py`'s. Bounds are multiples of this client parsing an
unpaged movie listing (`client_unit`). Measured on the Mac in the server's own listing time, which
got cheaper since: unfixed every lookup cost 0.34-0.38 units, fixed 0.01-0.03; the bound is 0.1 of
that unit times 10.5, the two units' ratio on the unfixed build.

    python3 tests/test_perf_library_all_guid.py
"""
import shutil
import sys
import tempfile

from test_perf_listing_window import check, client_unit, container, median, results, start
from test_perf_section_hubs import catalog

# The four shapes Plex clients sent the production server: typed show, episode and movie lookups,
# and an untyped one with includeGuids.
GUID = "guid=plex%3A%2F%2F{}%2F{:024x}"
LOOKUPS = [
    ("/library/all?type=2&" + GUID.format("show", 300001), "a show guid lookup", "300001"),
    ("/library/all?type=4&" + GUID.format("episode", 300003), "an episode guid lookup", "300003"),
    ("/library/all?type=1&" + GUID.format("movie", 500007), "a movie guid lookup", "500007"),
    ("/library/all?" + GUID.format("show", 300001) + "&includeGuids=1", "an untyped guid lookup", "300001"),
]


def run(port):
    _, body, server_unit = median(port, "/library/sections/1/all")
    unit = client_unit(body)
    print("unit (parsing the unpaged movie listing here, %d rows): %.4fs; the server's: %.3fs"
          % (len(container(body).get("Metadata", [])), unit, server_unit), flush=True)
    for path, name, key in LOOKUPS:
        status, body, took = median(port, path)
        keys = [row.get("ratingKey") for row in container(body).get("Metadata", [])]
        check("%s finds its one row under 1.1 units (%.3fs, %.2f)" % (name, took, took / unit),
              status == 200 and keys == [key] and took < 1.1 * unit, (status, keys, took))


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-library-all-")
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
