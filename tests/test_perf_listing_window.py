#!/usr/bin/env python3
"""A paged section listing projects only its page, on fun's library (2026-10-10).

fun-simplex, fed by zurg: 361 shows, 698 seasons and 13,509 episodes. A page of 50 movies
took 9 s and a `X-Plex-Container-Size=0` episode count over 30 s: the section listing
projected EVERY row of the library and `page_fields` sliced the page out afterwards.
The shape is fun's (`rust/tests/fixtures/fun_show_shape.json`); every episode is a copy of one real
episode row from it (`fixtures/fun_episode_row.json`, Media and cast included).

The library is fun's first 120 shows (about 4,500 episodes): an unpaged listing of all
13,509 projected rows held 5 GB, and two of them at once were killed by zen's OOM killer.
The bounds are multiples of one unpaged SHOW listing measured first in the same run, so a
slower host moves them with it. Measured on zen: unfixed, a count and the last page cost
1.9 units each; windowed, 0.36.

    python3 tests/test_perf_listing_window.py
"""
import copy
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           SIMPLEX_PLEXTV_URL=os.environ.get("SIMPLEX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("SIMPLEX_STATE", "SIMPLEX_TOKEN", "TMDB_KEY", "PLEX_TOKEN"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "perf-listing-window", "Accept": "application/json"}

SHOWS = 120
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]),
          flush=True)


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
    try:
        began = time.time()
        connection.request("GET", path, headers=dict(HEADERS, Connection="close"))
        response = connection.getresponse()
        body = response.read()
        return response.status, body, time.time() - began
    finally:
        connection.close()


def catalog(directory):
    """catalog_ui.json plus fun's shows, seasons and episodes in section 2."""
    data = json.load(open(os.path.join(HERE, "catalog_ui.json")))
    shape = json.load(open(os.path.join(ROOT, "rust", "tests", "fixtures", "fun_show_shape.json")))[:SHOWS]
    episode = json.load(open(os.path.join(HERE, "fixtures", "fun_episode_row.json")))
    key = 300000

    def mint():
        nonlocal key
        key += 1
        return str(key)

    for number, seasons in enumerate(shape):
        show = mint()
        data["items"].append({"ratingKey": show, "key": "/library/metadata/%s/children" % show,
                              "guid": "plex://show/%024x" % int(show), "type": "show",
                              "title": "Show %03d" % number, "librarySectionID": 2,
                              "addedAt": 1789180050, "updatedAt": 1789180050})
        for season_index, count in enumerate(seasons, 1):
            season = mint()
            data["items"].append({"ratingKey": season, "key": "/library/metadata/%s/children" % season,
                                  "type": "season", "title": "Season %d" % season_index,
                                  "index": season_index, "parentRatingKey": show,
                                  "parentTitle": "Show %03d" % number, "librarySectionID": 2,
                                  "addedAt": 1789180050, "updatedAt": 1789180050})
            for index in range(1, count + 1):
                row = copy.deepcopy(episode)
                own = mint()
                row.update({"ratingKey": own, "key": "/library/metadata/%s" % own,
                            "guid": "plex://episode/%024x" % int(own), "index": index,
                            "parentIndex": season_index, "parentRatingKey": season,
                            "grandparentRatingKey": show, "grandparentTitle": "Show %03d" % number,
                            "librarySectionID": 2})
                data["items"].append(row)
    # catalog_ui.json's own show library has episodes too.
    episodes = sum(1 for row in data["items"]
                   if row.get("type") == "episode" and str(row.get("librarySectionID")) == "2")
    path = os.path.join(directory, "catalog.json")
    with open(path, "w") as handle:
        json.dump(data, handle)
    return path, episodes


def start(path):
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", path, "--assert-claimed", "--gdm", "off",
                       root=ROOT),
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


def median(port, path, times=3):
    runs = [request(port, path) for _ in range(times)]
    runs.sort(key=lambda run: run[2])
    return runs[times // 2]


def container(body):
    return json.loads(body)["MediaContainer"]


def run(port, episodes):
    _, body, unit = median(port, "/library/sections/2/all")
    print("unit (unpaged show listing, %d rows): %.3fs" % (len(container(body).get("Metadata", [])), unit),
          flush=True)

    status, body, took = median(port, "/library/sections/2/all?type=4"
                                      "&X-Plex-Container-Start=0&X-Plex-Container-Size=0")
    answer = container(body)
    check("an episode count (Size=0) is under 1 unit (%.3fs)" % took,
          status == 200 and answer.get("totalSize") == episodes and not answer.get("Metadata")
          and took < 1.0 * unit, (status, answer.get("totalSize"), took))

    start = episodes - 50
    status, body, took = median(port, "/library/sections/2/all?type=4"
                                      "&X-Plex-Container-Start=%d&X-Plex-Container-Size=50" % start)
    answer = container(body)
    check("the last page of episodes is under 1 unit (%.3fs)" % took,
          status == 200 and answer.get("totalSize") == episodes and answer.get("offset") == start
          and len(answer.get("Metadata", [])) == 50 and took < 1.0 * unit,
          (status, answer.get("totalSize"), answer.get("offset"), len(answer.get("Metadata", [])), took))

    # The window is the one the unwindowed listing would have sliced.
    _, full, _ = request(port, "/library/sections/2/all?type=4")
    expected = [row["ratingKey"] for row in container(full)["Metadata"][start:start + 50]]
    got = [row["ratingKey"] for row in answer.get("Metadata", [])]
    check("the page is the same rows, in the same order, as the full listing's slice",
          got == expected, (got[:3], expected[:3]))

    status, body, took = median(port, "/library/sections/2/all?X-Plex-Container-Start=0"
                                      "&X-Plex-Container-Size=50")
    answer = container(body)
    # A ceiling, not the window's regression: measured 0.61 units unfixed, 0.46 fixed.
    check("a page of 50 shows is under 0.75 units (%.3fs)" % took,
          status == 200 and len(answer.get("Metadata", [])) == 50 and took < 0.75 * unit,
          (status, len(answer.get("Metadata", [])), took))


def main():
    directory = tempfile.mkdtemp(prefix="simplex-perf-window-")
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
