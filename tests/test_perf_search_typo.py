#!/usr/bin/env python3
"""A misspelt search answers without holding the server (a Plezy user's report, 2026-10-18).

"west wing" answered and "west wign" stalled the server until `/identity` stopped answering and
the container went unhealthy. A query nothing matches directly falls back on the spellfix
vocabularies (`spellfix.rs`), and that pass scanned the vocabulary for the query's candidates
again for every title and every cast name of the library, and read every row whole, all under
the runtime lock. On a copy of the production server's library (42,339 rows, its vocabularies
rebuilt by `PUT /library/optimize`) one misspelt search took 137-178 s; scanned once, 0.03 s.

The library is `test_perf_listing_window.py`'s, every episode given ids of its own (they are
copies of one real row, so they shared its imdb/tmdb/tvdb ids), which is what fills a real
library's tag vocabulary. The unit is one unpaged episode listing parsed here, as there.

A restart leaves the vocabularies in the state file until a search falls back on them (they held
16 MB for good on the production server): the first misspelt search after it reads them back
within the same budget, and answers as the server that built them did.

    python3 tests/test_perf_search_typo.py
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse

from test_perf_listing_window import HEADERS, catalog, check, client_unit, container, median, request, results, start


def own_ids(path):
    """Each episode its own Guid ids, as a real library's are."""
    data = json.load(open(path))
    number = 0
    for row in data["items"]:
        if row.get("type") == "episode" and row.get("Guid"):
            number += 1
            row["Guid"] = [{"id": "imdb://tt%08d" % (20000000 + number)},
                           {"id": "tmdb://%d" % (6000000 + number)},
                           {"id": "tvdb://%d" % (11000000 + number)}]
    with open(path, "w") as handle:
        json.dump(data, handle)


def optimize(port):
    """Database optimization, the measured point a PMS (and rex) rebuilds its spellfix tables."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
    try:
        connection.request("PUT", "/library/optimize", headers=dict(HEADERS, Connection="close"))
        return connection.getresponse().status
    finally:
        connection.close()


def search(port, query):
    return request(port, "/hubs/search?query=%s&limit=30" % urllib.parse.quote(query))


def shows(body):
    hubs = container(body).get("Hub", [])
    return {row.get("title") for hub in hubs if hub.get("type") == "show" for row in hub.get("Metadata", [])}


def typo_beside_identity(port, unit, label):
    """A misspelt search, and /identity asked while it runs: a container's health check."""
    during = {}
    typo = threading.Thread(target=lambda: during.update(search=search(port, "Simpsns")))
    typo.start()
    time.sleep(0.05)
    during["identity"] = request(port, "/identity")
    typo.join()
    status, body, took = during["search"]
    check("%s: the misspelt title is found by its spelling" % label, status == 200
          and shows(body) == {"The Simpsons"}, (status, shows(body) if status == 200 else body[:200]))
    check("%s: a misspelt search is under 0.5 units (%.4fs, %.2f)" % (label, took, took / unit),
          took < 0.5 * unit, (took, unit))
    identity = during["identity"]
    check("%s: /identity answers beside it, under 0.5 units (%.4fs)" % (label, identity[2]),
          identity[0] == 200 and identity[2] < 0.5 * unit, (identity[0], identity[2], unit))


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-typo-")
    try:
        path, _ = catalog(directory)
        own_ids(path)
        process, port = start(path)
        try:
            check("the vocabularies are rebuilt", optimize(port) == 200)
            _, body, _ = median(port, "/library/sections/2/all?type=4")
            unit = client_unit(body)
            print("unit (parsing the unpaged episode listing here, %d rows): %.4fs"
                  % (len(container(body).get("Metadata", [])), unit), flush=True)
            typo_beside_identity(port, unit, "built")
        finally:
            process.kill()
            process.wait()
        state = os.path.join(directory, "rex.db")
        process, port = start(path, "--state", state)
        try:
            # Saved before it is answered, as every write is.
            check("the vocabularies are rebuilt and saved", optimize(port) == 200)
        finally:
            process.kill()
            process.wait()
        process, port = start(path, "--state", state)
        try:
            typo_beside_identity(port, unit, "read back after a restart")
        finally:
            process.kill()
            process.wait()
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    print("%d/%d checks passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
