#!/usr/bin/env python3
"""A lineup of 300 channels a week deep answers the guide fast and holds no other request (2026-10-31).

rex read the guide off the channel rows for every Live TV request: each grid, lineup and What's
On answer cloned every channel row and its week of programmes and built every airing again, and
What's On looked for each channel's airing now through every airing of the lineup. With 300
channels of a programme every half hour for a week (100,800 airings) that is what a source with
an IPTV provider's EPG pushes. The guide is now kept until a row changes (`livetv::guide`) with
each channel's airings indexed by start.

    python3 tests/test_perf_livetv.py      # REX_PERF_LIVETV_CHANNELS=300
"""
import http.client
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server_command import register_source, server_command  # noqa: E402
from startup_wait import poll_until  # noqa: E402
from test_ports import free_port  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNELS = int(os.environ.get("REX_PERF_LIVETV_CHANNELS", "300"))
PREFIX = "/tv.plex.providers.epg.xmltv:2"
# A live channel's probe: an HLS playlist with no duration.
PROBE = {"format": {"format_name": "hls", "nb_streams": 2},
         "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "profile": "High", "width": 1280, "height": 720,
                      "r_frame_rate": "30/1", "avg_frame_rate": "30/1", "pix_fmt": "yuv420p", "level": 31},
                     {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "48000",
                      "channel_layout": "stereo"}]}
# Bounds, generous for a slow test host: the unfixed server took several seconds for each.
GRID_MS = 100
WHATS_ON_MS = 1500
BESIDE_MS = 250

results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print("  %s  %-64s %s" % ("PASS" if ok else "FAIL", label, detail), flush=True)


def ask(port, method, path, body=None, timeout=600):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        began = time.perf_counter()
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers={"Accept": "application/json", "Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read()
        return response.status, data, time.perf_counter() - began
    finally:
        connection.close()


def lineup(start):
    """Each channel's week, a programme every half hour from six hours back."""
    first = (start // 1800) * 1800 - 6 * 3600
    for number in range(1, CHANNELS + 1):
        guide = []
        for slot in range(7 * 48):
            begins = first + slot * 1800
            if slot % 2:
                guide.append({"start": begins, "end": begins + 1800, "title": "Episode %d" % (slot % 20 + 1), "type": "episode",
                              "show": "Show %d" % number, "season": 1, "episode": slot % 20 + 1, "categories": ["Series"]})
            else:
                guide.append({"start": begins, "end": begins + 1800, "title": "Programme %d.%d" % (number, slot % 12),
                              "summary": "A programme.", "categories": ["News" if number % 3 else "Sports"]})
        yield {"id": "channels/%d.m3u8" % number, "path": "channels/%d.m3u8" % number, "media_info": PROBE,
               "display": {"title": "Channel %d" % number},
               "channel": {"number": "%d.1" % number, "title": "Channel %d" % number, "guide": guide}}


def main():
    scratch = tempfile.mkdtemp(prefix="rex-perf-livetv-")
    port = free_port()
    catalog = os.path.join(scratch, "catalog.json")
    json.dump({"machineIdentifier": "0" * 39 + "9", "friendlyName": "rex-perf-livetv",
               "sections": [{"key": "1", "type": "movie", "title": "Movies", "uuid": "0" * 39 + "1"}], "items": []},
              open(catalog, "w"))
    env = dict(os.environ, TZ="UTC", REX_TRANSCODER="off", REX_PLEXTV_URL="http://127.0.0.1:%d" % free_port())
    log = open(os.path.join(scratch, "rex.log"), "w")
    process = subprocess.Popen(server_command("--port", port, "--catalog", catalog, "--assert-claimed", root=ROOT),
                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        poll_until(lambda: ask(port, "GET", "/identity")[0] == 200 if _up(port) else False, 40)
        register_source(port, "perf", {"url": "http://127.0.0.1:9"})
        items = list(lineup(int(time.time())))
        began = time.perf_counter()
        for at in range(0, len(items), 30):
            status, body, _ = ask(port, "POST", "/rex/feed/perf/items", {"items": items[at:at + 30]})
            if status != 200 or json.loads(body).get("rejected"):
                raise RuntimeError("push refused: %s %s" % (status, body[:300]))
        print("pushed %d channels, %d airings in %.1fs" % (CHANNELS, CHANNELS * 7 * 48, time.perf_counter() - began), flush=True)
        # Tomorrow, whole in the week pushed: today keeps only the airings that ended under three
        # hours ago, 8 rows after 22:30 UTC, which failed `rows >= 10` every night.
        today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400))
        status, _, first = ask(port, "GET", "%s/grid?channelGridKey=150.1&date=%s" % (PREFIX, today))
        grids = []
        for number in range(1, 41):
            status, body, took = ask(port, "GET", "%s/grid?channelGridKey=%d.1&date=%s" % (PREFIX, number, today))
            grids.append(took)
        rows = len(json.loads(body)["MediaContainer"].get("Metadata", []))
        median = statistics.median(grids) * 1000
        check("a channel's day in the grid answers in under %d ms" % GRID_MS, status == 200 and rows >= 10 and median < GRID_MS,
              "first %.0f ms (reads the guide), then median %.1f ms, %d rows" % (first * 1000, median, rows))
        status, body, took = ask(port, "GET", PREFIX + "/lineups/dvr/channels")
        check("the lineup of %d channels answers in under %d ms" % (CHANNELS, GRID_MS),
              status == 200 and json.loads(body)["MediaContainer"]["size"] == CHANNELS and took * 1000 < GRID_MS, "%.1f ms" % (took * 1000))
        status, body, took = ask(port, "GET", PREFIX + "/hubs/discover?count=12&includeRecentChannels=1&includeMeta=1&excludeFields=summary")
        check("What's On answers in under %d ms" % WHATS_ON_MS, status == 200 and took * 1000 < WHATS_ON_MS, "%.0f ms" % (took * 1000))
        # A Plex request beside a stream of grid requests waits for at most one of them.
        stop, beside = threading.Event(), []

        def hammer(offset):
            number = offset
            while not stop.is_set():
                ask(port, "GET", "%s/grid?channelGridKey=%d.1&date=%s" % (PREFIX, number % CHANNELS + 1, today))
                number += 4

        threads = [threading.Thread(target=hammer, args=(offset,), daemon=True) for offset in range(4)]
        for thread in threads:
            thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            beside.append(ask(port, "GET", "/identity")[2])
            time.sleep(0.05)
        stop.set()
        for thread in threads:
            thread.join(30)
        worst = max(beside) * 1000
        check("/identity beside four streams of grid requests waits under %d ms" % BESIDE_MS, worst < BESIDE_MS,
              "worst of %d: %.0f ms, median %.1f ms" % (len(beside), worst, statistics.median(beside) * 1000))
    finally:
        process.kill()
        process.wait()
        shutil.rmtree(scratch, ignore_errors=True)
    failed = [label for label, ok in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


def _up(port):
    try:
        return ask(port, "GET", "/identity", timeout=2)[0] == 200
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
