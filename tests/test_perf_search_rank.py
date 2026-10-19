#!/usr/bin/env python3
"""A search for a common word ranks its hits without redoing each row's work (2026-10-19).

The web client's search for "the" on rex-fun's library (4,014 movies, 34,258 episodes) reaches
10,331 episodes and 1,344 movies. Every hit is ranked before the first `limit` are kept, and that
ranking built each row's sort key again on every comparison and worked out each title's tier
twice, once to put exact titles first and again for its score; `/library/search` then projected
rows past its `limit` that it cut. On a copy of that library the client's own request took
0.48 s on the Windows bench box and 0.12 s on a Mac; keyed once a row, 0.21 s and 0.04 s.

The library is `test_perf_listing_window.py`'s (the production server's first 120 shows, about
4,500 episodes), each episode titled with one of the real titles "the" reaches on rex-fun
(`fixtures/fun_the_episode_titles.json`, evenly sampled from that search's ranked answer), so
every episode is a hit as it is there. The unit is one unpaged episode listing parsed here.

    python3 tests/test_perf_search_rank.py
"""
import json
import os
import shutil
import sys
import tempfile

from test_perf_listing_window import HERE, catalog, check, client_unit, container, median, results, start

# The web client 4.147.1's own requests as it sends them, typing "the" (rex-fun's log, 2026-10-19).
FLAT = "/library/search?query=the&limit=100&searchTypes=movies%2Cpeople%2Ctv&includeCollections=1"
HUBS = "/hubs/search?query=the&limit=30&includeCollections=1&includeExternalMedia=1"


def retitle(path):
    titles = json.load(open(os.path.join(HERE, "fixtures", "fun_the_episode_titles.json")))
    data = json.load(open(path))
    episodes = [row for row in data["items"] if row.get("type") == "episode" and str(row.get("librarySectionID")) == "2"]
    for number, row in enumerate(episodes):
        row["title"] = titles[number % len(titles)]
    with open(path, "w") as handle:
        json.dump(data, handle)
    return len(episodes)


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-rank-")
    process = None
    try:
        path, _ = catalog(directory)
        episodes = retitle(path)
        process, port = start(path)
        _, body, _ = median(port, "/library/sections/2/all?type=4")
        unit = client_unit(body)
        print("unit (parsing the unpaged episode listing here, %d rows): %.4fs"
              % (len(container(body).get("Metadata", [])), unit), flush=True)

        status, body, took = median(port, HUBS.replace("limit=30", "limit=100000"))
        hits = sum(hub.get("size", 0) for hub in container(body).get("Hub", []) if hub.get("type") == "episode")
        # A few real titles hold "the" only after punctuation, which starts no word.
        check("nearly every episode is a hit (%d of %d)" % (hits, episodes),
              status == 200 and hits >= 0.98 * episodes, hits)

        status, body, took = median(port, FLAT, times=5)
        found = container(body).get("SearchResult", []) if status == 200 else []
        check("the web client's search answers its 100 (%d)" % len(found), len(found) == 100, (status, body[:200]))
        check("the web client's search is under 0.5 units (%.4fs, %.2f)" % (took, took / unit),
              took < 0.5 * unit, (took, unit))

        status, body, took = median(port, HUBS, times=5)
        check("the hubs search is under 0.5 units (%.4fs, %.2f)" % (took, took / unit),
              status == 200 and took < 0.5 * unit, (status, took, unit))
    finally:
        if process is not None:
            process.kill()
            process.wait()
        shutil.rmtree(directory, ignore_errors=True)
    print("%d/%d checks passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
