#!/usr/bin/env python3
"""A playback progress beat costs what it changes, not the library (the production server, 2026-10-16).

The production rex: 40,197 rows, and a state file holding 22,432 `deleted` and 22,380
`retiredKeys` entries. Every `/:/timeline` beat a playing client sends (one per ~10 s) took
the runtime lock and, before answering, walked the whole library twice: an episode's beat
scanned every row for its show's siblings to un-hide from Continue Watching, and the save it
dirtied serialised, hashed and re-keyed every element of every state field to find that none
had changed. 69 ms a beat on a fast host; on the production server, where part of the library
sat in swap, every beat paged it back in and the requests queued behind it timed out at 60 s.

The library is `test_perf_listing_window.py`'s (the production server's first 120 shows, every
episode one real row), and the state file is seeded with the production server's counts of
deleted and retired keys. The unit is one unpaged episode listing measured first in the same
run. Measured on the test host (arm64 Docker, 4,582 episodes): unfixed a beat cost 0.043 units;
fixed, 0.008.

    python3 tests/test_perf_progress_beat.py
"""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

from test_perf_listing_window import ENV, HEADERS, ROOT, catalog, check, container, free_port, median, request, results
from server_command import server_command
import http.client

DELETED = 22432
RETIRED = 22380
BEATS = 30


def start(path, state):
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", path, "--state", state, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(1800):
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
    process.wait(timeout=120)


def seed(state):
    """The production server's deleted and retired keys, none of them a row of this library."""
    db = sqlite3.connect(state)
    for name, kind, values in (
        ("state:deleted", "array", ['"%d"' % (900000 + n) for n in range(DELETED)]),
        ("state:retiredKeys", "array", ["%d" % (700000 + n) for n in range(RETIRED)]),
    ):
        db.execute("INSERT OR REPLACE INTO doc(name, kind, body) VALUES (?, ?, NULL)", (name, kind))
        db.execute("DELETE FROM row WHERE coll = ?", (name,))
        db.executemany("INSERT INTO row(coll, key, pos, body) VALUES (?, ?, ?, ?)",
                       [(name, "seed.%d" % n, float(n), body) for n, body in enumerate(values)])
    db.commit()
    db.close()


def beat(port, key, offset):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
    try:
        began = time.time()
        connection.request("GET", "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s"
                                  "&state=playing&time=%d&duration=2400000" % (key, key, offset),
                           headers=dict(HEADERS, Connection="close",
                                        **{"X-Plex-Session-Identifier": "perf-beat"}))
        response = connection.getresponse()
        response.read()
        return response.status, time.time() - began
    finally:
        connection.close()


def run(port):
    _, body, unit = median(port, "/library/sections/2/all?type=4")
    episodes = container(body).get("Metadata", [])
    print("unit (unpaged episode listing, %d rows): %.3fs" % (len(episodes), unit), flush=True)
    key = episodes[len(episodes) // 2]["ratingKey"]
    # The first beats write what the seed left keyed differently; the steady state follows.
    for offset in range(5):
        beat(port, key, 120000 + offset * 1000)
    runs = [beat(port, key, 200000 + n * 10000) for n in range(BEATS)]
    took = sorted(run[1] for run in runs)[BEATS // 2]
    check("every beat answers 200", all(status == 200 for status, _ in runs), runs[:3])
    check("a progress beat is under 0.02 units (%.4fs, %.4f)" % (took, took / unit),
          took < 0.02 * unit, (took, unit))
    _, body, _ = median(port, "/library/metadata/%s" % key, 1)
    check("the beat's offset is kept", container(body)["Metadata"][0].get("viewOffset") == 200000 + (BEATS - 1) * 10000)


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-beat-")
    try:
        path, _ = catalog(directory)
        state = os.path.join(directory, "rex.db")
        process, port = start(path, state)
        stop(process)
        seed(state)
        process, port = start(path, state)
        try:
            run(port)
        finally:
            process.kill()
            process.wait()
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    print("%d/%d checks passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
