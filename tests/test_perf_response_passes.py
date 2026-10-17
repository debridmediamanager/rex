#!/usr/bin/env python3
"""A large answer is parsed once after its route, and its rows ranked once each (2026-10-17).

On a copy of the production server's library an unpaged episode listing (96 MB) took 5.7 s on
the Mac, and 60% of it came after the route: the paging, paging-header, XML and key-order
passes each parsed the whole body again (three to five parses), and the key-order pass ranked a
row's attributes inside every comparison, formatting the row's bucket name each time.

The library is `test_perf_listing_window.py`'s. The unit is this client parsing the answer once
(Python's `json.loads`). Measured on the Mac over three runs (unfixed -> fixed, units): JSON
12.2-15.5 -> 6.6-7.1, XML 12.1-15.1 -> 6.3-6.7; with the document handed over unserialised
(`Response::json_deferred`) both 4.2-4.5.

    python3 tests/test_perf_response_passes.py
"""
import json
import shutil
import sys
import tempfile
import time

from test_perf_listing_window import HEADERS, catalog, check, median, results, start

LISTING = "/library/sections/2/all?type=4"
BOUND = 5.5


def run(port, episodes):
    status, body, took = median(port, LISTING)
    # The unit is this client parsing the same answer once: it scales with the answer and
    # the host, and nothing the server does moves it.
    began = time.perf_counter()
    rows = len(json.loads(body)["MediaContainer"].get("Metadata", []))
    unit = time.perf_counter() - began
    print("unit (parsing the %d-byte answer here once): %.3fs" % (len(body), unit), flush=True)
    check("an unpaged episode listing is under %.1f units (%.3fs, %.1f)" % (BOUND, took, took / unit),
          status == 200 and rows == episodes and took < BOUND * unit, (status, rows, took))
    HEADERS["Accept"] = "application/xml"
    try:
        status, xml, took = median(port, LISTING)
    finally:
        HEADERS["Accept"] = "application/json"
    check("and as XML under %.1f units (%.3fs, %.1f)" % (BOUND, took, took / unit),
          status == 200 and xml.count(b"<Video ") == episodes and took < BOUND * unit, (status, took))


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-passes-")
    process = None
    try:
        path, episodes = catalog(directory)
        process, port = start(path)
        run(port, episodes)
    finally:
        if process is not None:
            process.kill()
            process.wait()
        shutil.rmtree(directory, ignore_errors=True)
    print("%d/%d passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
