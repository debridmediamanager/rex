#!/usr/bin/env python3
"""A byte read finds its Part without reading the library (fun's library copy, 2026-10-18).

Every `/library/parts/{id}/...` read -- each Range request a playing client sends -- walked every
row's Media and Parts to find the one Part, and a shared account's read walked them twice more
(`named_rows`). On a copy of the production server's library (42,806 rows) that was 60-66 ms of
each read before a byte moved; filed by Part id, 3-4 ms, the HTTP round trip.

The library is `test_perf_listing_window.py`'s, every episode given Parts of its own (they are
copies of one real row, so they shared its Part id). The unit is one unpaged episode listing
parsed here, as there. The reads go to a Part in the middle of the library and to one that does
not exist; with no source registered the first is the routed 502, the second the 404 -- both
answered once the Part is looked up. Measured in the bench container on the Mac (4,582 episodes):
unfixed a read cost 0.10-0.11 units, filed 0.004-0.005; the bound is 0.03.

    python3 tests/test_perf_part_read.py
"""
import json
import os
import shutil
import sys
import tempfile

from test_perf_listing_window import catalog, check, client_unit, container, median, request, results, start

READS = 21


def own_parts(path):
    """Each row's Parts their own ids, as a real library's are."""
    data = json.load(open(path))
    next_id = 5000000
    for row in data["items"]:
        for media in row.get("Media") or []:
            for part in media.get("Part") or []:
                next_id += 1
                part["id"] = next_id
                part["key"] = "/library/parts/%d/1/file.mkv" % next_id
    with open(path, "w") as handle:
        json.dump(data, handle)
    return next_id


def read(port, part):
    runs = sorted(request(port, "/library/parts/%d/1/file.mkv" % part) for _ in range(READS))
    took = sorted(run[2] for run in runs)[READS // 2]
    return {run[0] for run in runs}, took


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-part-")
    try:
        path, _ = catalog(directory)
        last = own_parts(path)
        process, port = start(path)
        try:
            _, body, server_unit = median(port, "/library/sections/2/all?type=4")
            unit = client_unit(body)
            print("unit (parsing the unpaged episode listing here, %d rows): %.4fs; the server's: %.3fs"
                  % (len(container(body).get("Metadata", [])), unit, server_unit), flush=True)
            middle = 5000000 + (last - 5000000) // 2
            for name, part, status in (("a Part of the library", middle, 502), ("no Part", last + 1000, 404)):
                statuses, took = read(port, part)
                check("%s answers %d" % (name, status), statuses == {status}, statuses)
                check("a read of %s is under 0.03 units (%.4fs, %.4f)" % (name, took, took / unit),
                      took < 0.03 * unit, (took, unit))
        finally:
            process.kill()
            process.wait()
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    print("%d/%d checks passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
