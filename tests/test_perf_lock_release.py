#!/usr/bin/env python3
"""A whole-library answer does not hold the other clients' requests (2026-10-17).

On fun, while rex answered every episode at once (10 s), a Home request sent alongside waited 5.9 s,
where a real Plex Media Server on the same host and library held one for 1.6 s and then answered
every other in 30 ms: rex projected all 34k rows under its one runtime lock. A large owner's
listing is now projected a chunk at a time with the lock released between chunks
(`ProjectionPlan`), and the passes after its route run once its rows are in.

The library is `test_perf_listing_window.py`'s (4,582 episodes). While an unpaged episode listing
runs, `/library/sections` is asked every 20 ms; the worst wait is bounded as a fraction of the
listing's own time, measured in the same run (best of three). Measured on the Mac over two runs
(unfixed -> fixed): 0.25-0.48 of the listing -> 0.01-0.02.

    python3 tests/test_perf_lock_release.py
"""
import shutil
import sys
import tempfile
import threading
import time

from test_perf_listing_window import catalog, check, container, request, results, start

LISTINGS = ["/library/sections/2/allLeaves", "/library/sections/2/all?type=4", "/library/sections/2/newest"]


def worst_wait(port, listing):
    took = {}

    def big():
        began = time.perf_counter()
        request(port, listing)
        took["listing"] = time.perf_counter() - began

    thread = threading.Thread(target=big)
    thread.start()
    waits = []
    while thread.is_alive():
        began = time.perf_counter()
        request(port, "/library/sections")
        waits.append(time.perf_counter() - began)
        time.sleep(0.02)
    thread.join()
    return max(waits), took["listing"]


def same_rows(port, listing):
    """The chunked answer is the one the route projects itself: its pages, below the plan's size."""
    whole = container(request(port, listing)[1])
    rows, start = [], 0
    while True:
        joiner = "&" if "?" in listing else "?"
        page = container(request(port, "%s%sX-Plex-Container-Start=%d&X-Plex-Container-Size=1000" % (listing, joiner, start))[1])
        rows += page.get("Metadata", [])
        start += 1000
        if start >= page.get("totalSize", 0):
            break
    check("%s answers the rows its pages do (%d rows)" % (listing, len(rows)),
          whole.get("Metadata") == rows and whole.get("size") == len(rows), (whole.get("size"), len(rows)))


def run(port):
    for listing in LISTINGS:
        same_rows(port, listing)
        request(port, listing)
        worst, total = min((worst_wait(port, listing) for _ in range(3)), key=lambda pair: pair[0] / pair[1])
        check("during %s a request waits under 10%% of it (%.3fs of %.3fs, %.2f)" % (listing, worst, total, worst / total),
              worst < 0.1 * total, (worst, total))


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-lock-")
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
