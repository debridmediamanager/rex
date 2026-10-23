#!/usr/bin/env python3
"""A source's listing and a snapshot's end read no row from disk, and hold no Plex request (2026-10-18).

On the production server (rex-fun, 40,335 rows from one zurg) `GET /rex/feed/rd/items` read
every row of the library from the state file to find the source's releases in their Media:
26.4 s, and AD's 39,368 another 22.4 s. Every Plex request waited behind it -- a show page on
Plex for Android spun for a minute and its `/children` was answered two seconds after the AD
listing ended. A snapshot's final chunk walked the rows the same way (`feed_ids`), so zurg's
snapshot every six hours stalled the server once per source.

The library here is pushed through the feed with a state file and the server restarted, so its
rows are on disk as on the production server, with a 1 MB row cache. The unit is this client
parsing the listing's answer once (`test_perf_listing_window.client_unit`): it scales with the
library and the host and no server change moves it. Measured on the Mac (6,000 movies): unfixed
the listing cost 45 units, a Plex request beside it 38 and the snapshot's end 42; reading the
releases kept beside each row's hot fields, 3.6, 0.8 and 1.3. The bounds are 10, 1 and 3.

That listing still built and wrote its answer holding the runtime lock: on rex-fun the RD listing
(41,419 items, 13 MB) took 0.46-0.6 s alone and a 50 ms Plex request sent beside it 0.6-2.0 s.
Here, with 100,000 movies, a Plex request sent halfway through waited 40% of the listing; taking
only the releases under the lock, 8%. The bound is a fifth.

    python3 tests/test_perf_feed_listing.py
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from server_command import server_command
from test_perf_listing_window import ENV, check, client_unit, free_port, request, results

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROBE = json.load(open(os.path.join(HERE, "fixtures", "zurg", "entries.json")))["entries"]["movie"]["entry"]["media_info"]
MOVIES = int(os.environ.get("REX_PERF_FEED_MOVIES", "100000"))
CHUNK = 500
MOUNT = "/zurg"


def item(number):
    release = "Feed.Perf.Movie.%05d.2020.1080p.WEB-DL" % number
    path = "movies/%s/%s.mkv" % (release, release)
    return {"id": path, "path": path, "file": "%s/%s" % (MOUNT, path), "name": release,
            "release": "movies/" + release, "size": 1000000 + number, "hash": "%040x" % number,
            "media_info": PROBE}


def send(port, method, path, body):
    import http.client
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=900)
    try:
        began = time.time()
        connection.request(method, path, body=json.dumps(body),
                           headers={"Content-Type": "application/json", "Accept": "application/json"})
        response = connection.getresponse()
        return response.status, response.read(), time.time() - began
    finally:
        connection.close()


def start(catalog, state, cache_mb):
    port = free_port()
    env = dict(ENV, REX_ROW_CACHE_MB=str(cache_mb))
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", catalog, "--state", state, "--mount", MOUNT,
                       "--assert-claimed", "--gdm", "off", root=ROOT),
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(3000):
        if process.poll() is not None:
            break
        try:
            if request(port, "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def stop(process):
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=300)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-feed-")
    catalog, state = os.path.join(directory, "catalog.json"), os.path.join(directory, "rex.db")
    try:
        process, port = start(catalog, state, 64)
        try:
            status, _, _ = send(port, "PUT", "/rex/sources/test", {"url": "http://127.0.0.1:9", "mount": MOUNT})
            check("the source registers", status == 200, status)
            created = 0
            for first in range(0, MOVIES, CHUNK):
                status, body, _ = send(port, "POST", "/rex/feed/test/items",
                                       {"items": [item(n) for n in range(first, min(first + CHUNK, MOVIES))]})
                created += json.loads(body).get("created", 0) if status == 200 else 0
            check("%d movies are created" % MOVIES, created == MOVIES, created)
        finally:
            stop(process)

        # Restarted: the rows are in the state file now, as the production server's are.
        process, port = start(catalog, state, 1)
        try:
            status, body, _ = request(port, "/rex/feed/test/items")
            unit = client_unit(body)
            listed = json.loads(body).get("size")
            check("the listing names every movie", status == 200 and listed == MOVIES, (status, listed))

            alone = sorted(request(port, "/rex/feed/test/items")[2] for _ in range(3))[1]
            runs, waits = [], []
            for _ in range(7):
                timing = {}

                def listing():
                    timing["listing"] = request(port, "/rex/feed/test/items")[2]

                thread = threading.Thread(target=listing)
                thread.start()
                # Halfway through, when a listing that held the server would still hold it.
                time.sleep(alone / 2)
                waits.append(request(port, "/library/sections")[2])
                thread.join()
                runs.append(timing["listing"])
            took, waited = sorted(runs)[3], sorted(waits)[3]
            print("unit (parsing the %d-item listing here): %.4fs; listing %.3fs; a Plex request beside it %.3fs"
                  % (listed, unit, took, waited), flush=True)
            check("the listing costs under 10 units (%.3fs, %.1f)" % (took, took / unit), took < 10 * unit, (took, unit))
            check("a Plex request beside it waits under 1 unit (%.3fs, %.1f)" % (waited, waited / unit),
                  waited < unit, (waited, unit))
            check("a Plex request sent halfway waits under a fifth of the listing (%.3fs of %.3fs)" % (waited, took),
                  waited < took / 5, (waited, took))

            # A snapshot's final chunk works out what the session left out: here it would remove
            # all but one movie, so it is refused, after the same pass a real removal makes.
            session = {"session": "perf", "part": 0, "complete": False, "items": [item(0)]}
            send(port, "PUT", "/rex/feed/test/snapshot", session)
            status, body, ended = send(port, "PUT", "/rex/feed/test/snapshot",
                                       {"session": "perf", "part": 1, "complete": True, "items": []})
            check("the snapshot that would remove nearly everything is refused", status == 409, (status, body[:200]))
            check("the snapshot's end costs under 3 units (%.3fs, %.1f)" % (ended, ended / unit),
                  ended < 3 * unit, (ended, unit))
        finally:
            stop(process)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    print("%d/%d checks passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
