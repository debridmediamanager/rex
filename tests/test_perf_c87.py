#!/usr/bin/env python3
"""Cycle 87: whole-library writes and reads stay linear on a 40,000-row library.

`tests/test_followups_c87_e2.py`'s race leg builds a 40,000-movie section, makes
whole-library playlists, then per pair re-adds the whole library to one
(`PUT /playlists/{id}/items?uri=server://.../library/sections/1/all?type=1`), deletes a
row, clears the list and reads it. The native server took ~1.7 s a pair, the oracle
~0.04 s, and 300 pairs timed out in acceptance. Measured on the Mac with `sample`:

- the playlist create and items PUT checked each of 40,000 rows against the list with a
  scan of the list (1.7 s each; now ~0.06 s, the oracle 0.03);
- a playlist's items page looked each row's `playlistItemID` up with a scan (2 s a page);
- every projected row asked `stamp_collection_tags` whether it is a member of a
  whole-library collection with a scan of its members (a section page took 2 s, the
  oracle 0.15);
- deleting that collection recorded a section-timeline frame per member into a Vec it
  scanned per frame, and the event buffer moved all 8,192 retained events per push once
  full (3.5 s, the oracle 0.14);
- and the ORACLE added a section to a collection by rebuilding the member list per row,
  twice (53 s for the create, 43 s for an items PUT).

Every bound is a multiple of one linear request measured first in the same run -- the
unpaged `/library/sections/1/all` before any list exists -- so a slower host moves the
bound with it. The fixed servers sit at 0.1 to 2 of that unit; the unfixed ones at 2.4
(a create) to 48 (twenty pairs).

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_perf_c87.py
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "perf-c87", "Accept": "application/json"}
ROWS, PAIRS = 40000, 20
PAGE = "X-Plex-Container-Start=0&X-Plex-Container-Size=50"

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


def request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
    try:
        began = time.time()
        connection.request(method, path, headers=dict(HEADERS, Connection="close"))
        response = connection.getresponse()
        body = response.read()
        return response.status, body, time.time() - began
    finally:
        connection.close()


def catalog(directory):
    """catalog_ui.json plus ROWS lean movies in section 1 (the e2 race catalog)."""
    data = json.load(open(os.path.join(HERE, "catalog_ui.json")))
    for number in range(ROWS):
        key = 200000 + number
        data["items"].append({
            "ratingKey": str(key), "key": "/library/metadata/%d" % key,
            "guid": "plex://movie/%024x" % key, "type": "movie",
            "title": "Race %05d" % number, "year": 2020, "duration": 60000,
            "addedAt": 1789180050, "updatedAt": 1789180050, "librarySectionID": 1})
    path = os.path.join(directory, "catalog.json")
    with open(path, "w") as handle:
        json.dump(data, handle)
    return path


def start(path):
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", path, "--assert-claimed", "--gdm", "off",
                       root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(1200):
        if process.poll() is not None:
            break
        try:
            if request(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def median(port, method, path, times=3):
    return sorted(request(port, method, path)[2] for _ in range(times))[times // 2]


def container(body):
    return json.loads(body)["MediaContainer"]


def run(port):
    unit = median(port, "GET", "/library/sections/1/all")
    print("unit (unpaged /library/sections/1/all, no lists yet): %.3fs" % unit, flush=True)
    bare_page = median(port, "GET", "/library/sections/1/all?type=1&" + PAGE)
    machine = container(request(port, "GET", "/identity")[1])["machineIdentifier"]
    library = urllib.parse.quote("server://%s/com.plexapp.plugins.library/library/sections/1/all"
                                 "?type=1" % machine, safe="")

    status, body, took = request(port, "POST", "/playlists?type=video&title=Perf&smart=0&uri=%s"
                                 % library)
    playlist = container(body)["Metadata"][0]
    check("a whole-library playlist create is under 0.5 units (%.3fs)" % took,
          status == 200 and playlist.get("leafCount", 0) >= ROWS and took < 0.5 * unit,
          (status, playlist.get("leafCount"), took))

    key = playlist["ratingKey"]
    request(port, "DELETE", "/playlists/%s/items" % key)
    began, wrong = time.time(), []
    for number in range(PAIRS):
        status, body, _ = request(port, "PUT", "/playlists/%s/items?uri=%s" % (key, library))
        added = container(body)
        if status != 200 or added.get("leafCountAdded") != added.get("leafCountRequested") \
                or added.get("leafCountAdded", 0) < ROWS - PAIRS:
            wrong.append(("PUT", number, status, added.get("leafCountAdded")))
        if request(port, "DELETE", "/library/metadata/%d" % (200000 + number))[0] != 200:
            wrong.append(("DELETE row", number))
        if request(port, "DELETE", "/playlists/%s/items" % key)[0] != 200:
            wrong.append(("clear", number))
        left = container(request(port, "GET", "/playlists/%s" % key)[1])
        if (left.get("Metadata") or [{}])[0].get("leafCount"):
            wrong.append(("not cleared", number))
    took = time.time() - began
    check("%d pairs of re-add / row delete / clear / read are under 6 units (%.3fs, %.3fs a pair)"
          % (PAIRS, took, took / PAIRS), not wrong and took < 6 * unit, (wrong[:4], took))

    request(port, "PUT", "/playlists/%s/items?uri=%s" % (key, library))
    took = median(port, "GET", "/playlists/%s/items?%s" % (key, PAGE))
    check("a page of a 40,000-member playlist is under 0.75 units (%.3fs)" % took,
          took < 0.75 * unit, took)

    status, body, took = request(port, "POST", "/library/collections?type=1&title=Perf&smart=0"
                                 "&sectionId=1&uri=%s" % library)
    collection = container(body)["Metadata"][0]["ratingKey"] if status == 200 else None
    check("a whole-library collection create is under 0.75 units (%.3fs)" % took,
          collection and took < 0.75 * unit, (status, took))
    status, body, took = request(port, "PUT", "/library/collections/%s/items?uri=%s"
                                 % (collection, library))
    check("re-adding the library to it is under 0.75 units (%.3fs)" % took,
          status == 200 and took < 0.75 * unit, (status, took))

    took = median(port, "GET", "/library/sections/1/all?type=1&" + PAGE)
    check("a section page with every row in a collection costs what it did before the "
          "collection (%.3fs against %.3fs)" % (took, bare_page),
          took < max(3 * bare_page, 0.3 * unit), (took, bare_page))
    took = median(port, "GET", "/library/collections/%s/children?%s" % (collection, PAGE))
    check("a page of the collection's children is under 1.5 units (%.3fs)" % took,
          took < 1.5 * unit, took)

    status, _, took = request(port, "DELETE", "/library/collections/%s" % collection)
    check("deleting the 40,000-member collection is under 1 unit (%.3fs)" % took,
          status == 200 and took < unit, (status, took))


def main():
    directory = tempfile.mkdtemp(prefix="rex-perf-c87-")
    try:
        process, port = start(catalog(directory))
        try:
            run(port)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    passed = sum(results)
    print("%s %d/%d cycle 87 whole-library performance checks"
          % ("PASS" if passed == len(results) else "FAIL", passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
