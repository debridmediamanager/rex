#!/usr/bin/env python3
"""The server's memory stays near its library's, at the start and under the home screen.

The production rex (2026-10-10), 19,600 rows: 3.5 GB resident and 1.5 GB swapped. heaptrack and
`/proc` on a copy of its database found the start holding every row twice (the saved
document beside the restored catalog, 1.58 GB) and requests copying whole libraries --
a page of 50 shows +650 MB, an episode count +1.4 GB at peak, `/hubs` +1.9 GB -- kept
afterwards by the allocator.

Its shape (`rust/tests/fixtures/fun_show_shape.json`, first 120 shows) with every
episode a copy of one real row (`fixtures/fun_episode_row.json`) is saved to a state
database by one server and restored by a second, the path the production server takes at every
start. The bounds are ratios to the rows' own JSON size and to the resident size after the start,
so they hold on any host. Linux only: it reads `/proc/<pid>/status`. Measured on the test host:
unfixed, the start peaked at 16.6x the rows' JSON and the requests added 1,440 MB; fixed,
8.2x and 6 MB. Each stream was then a map of its own, half of the production server's 2.6 GB on
2026-10-16; held as text, the restored library is 4.0x its JSON (8.1x before, arm64 Docker).

    python3 tests/test_perf_memory.py
"""
import copy
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY", "PLEX_TOKEN"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "perf-memory", "Accept": "application/json"}
SHOWS = 120
# Each Part's streams are held as their JSON text (`catalog::PACKED_STREAMS`).
RESIDENT = 6
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
        connection.request("GET", path, headers=dict(HEADERS, Connection="close"))
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def memory(pid):
    """(resident, peak resident) in MB."""
    fields = {}
    with open("/proc/%d/status" % pid) as handle:
        for line in handle:
            name, _, value = line.partition(":")
            fields[name] = value
    return int(fields["VmRSS"].split()[0]) / 1024, int(fields["VmHWM"].split()[0]) / 1024


def catalog(directory):
    """catalog_ui.json plus the production server's first SHOWS shows; and the JSON size of every row."""
    data = json.load(open(os.path.join(HERE, "catalog_ui.json")))
    shape = json.load(open(os.path.join(ROOT, "rust", "tests", "fixtures",
                                        "fun_show_shape.json")))[:SHOWS]
    episode = json.load(open(os.path.join(HERE, "fixtures", "fun_episode_row.json")))
    key = 400000
    for number, seasons in enumerate(shape):
        key += 1
        show = str(key)
        data["items"].append({"ratingKey": show, "key": "/library/metadata/%s/children" % show,
                              "type": "show", "title": "Show %03d" % number, "librarySectionID": 2,
                              "addedAt": 1789180050, "updatedAt": 1789180050, "_scanned": True})
        for season_index, count in enumerate(seasons, 1):
            key += 1
            season = str(key)
            data["items"].append({"ratingKey": season, "type": "season", "index": season_index,
                                  "title": "Season %d" % season_index, "parentRatingKey": show,
                                  "librarySectionID": 2, "addedAt": 1789180050, "_scanned": True})
            for index in range(1, count + 1):
                key += 1
                row = copy.deepcopy(episode)
                row.update({"ratingKey": str(key), "key": "/library/metadata/%d" % key,
                            "index": index, "parentIndex": season_index, "parentRatingKey": season,
                            "grandparentRatingKey": show, "librarySectionID": 2,
                            "addedAt": 1789180050 + key, "viewOffset": 1000 if key % 97 == 0 else None,
                            "_scanned": True})
                if row["viewOffset"] is None:
                    del row["viewOffset"]
                data["items"].append(row)
    path = os.path.join(directory, "catalog.json")
    with open(path, "w") as handle:
        json.dump(data, handle)
    rows_mb = sum(len(json.dumps(row)) for row in data["items"]) / 1e6
    # the production server's catalog file is a seed with no rows: the library is the state's own.
    seed = os.path.join(directory, "seed.json")
    shutil.copy(os.path.join(HERE, "catalog_ui.json"), seed)
    return path, seed, rows_mb


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
            if request(port, "/identity") == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def stop(process):
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    if not sys.platform.startswith("linux"):
        print("SKIP  reads /proc/<pid>/status (Linux only)")
        return
    directory = tempfile.mkdtemp(prefix="rex-perf-memory-")
    process = None
    try:
        path, seed, rows_mb = catalog(directory)
        state = os.path.join(directory, "state.db")
        # The first server writes the rows into the state database...
        process, port = start(path, state)
        request(port, "/library/sections/2/all?X-Plex-Container-Start=0&X-Plex-Container-Size=1")
        time.sleep(3)
        stop(process)
        # ...and the second restores them from it over the seed, as the production server does at
        # every start.
        process, port = start(seed, state)
        time.sleep(35)  # past one trim interval
        loaded, load_peak = memory(process.pid)
        print("rows %.0f MB of JSON; after the start %.0f MB resident, %.0f MB peak"
              % (rows_mb, loaded, load_peak), flush=True)
        check("the start peaks under 12x the rows' JSON (%.1fx)" % (load_peak / rows_mb),
              load_peak < 12 * rows_mb, (load_peak, rows_mb))
        check("the restored library holds under %dx its rows' JSON (%%.1fx)" % RESIDENT % (loaded / rows_mb),
              loaded < RESIDENT * rows_mb, (loaded, rows_mb))

        for path_ in ("/hubs", "/library/sections/2/all?X-Plex-Container-Start=0&X-Plex-Container-Size=50",
                      "/library/sections/2/all?type=4&X-Plex-Container-Start=0&X-Plex-Container-Size=0",
                      "/library/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=50"):
            if request(port, path_) != 200:
                check("%s answers" % path_, False)
        _, peak = memory(process.pid)
        print("after /hubs, a show page, an episode count and recently added: %.0f MB peak" % peak,
              flush=True)
        check("those requests raise the peak by under a third of the started size (+%.0f MB)"
              % (peak - load_peak), peak - load_peak < loaded / 3, (peak, load_peak, loaded))
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        shutil.rmtree(directory, ignore_errors=True)
    print("%d/%d passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
