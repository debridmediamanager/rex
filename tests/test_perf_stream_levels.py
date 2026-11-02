#!/usr/bin/env python3
"""A track's levels (`/library/streams/{id}/levels`) are found without reading the library.

Plexamp asks for the levels of every track it starts (its waveform), and of the next one. rex
looked for the stream in every row, each read from the state file: on rex-fun (43,506 rows,
2026-11-02) 10-16 s a request, and every other request waited behind it, so a track a person
tapped started 15 s later, and again for each track after (rex.log: `rows: 43506 read from the
file in 13.2s` right before each `404 GET /library/streams/<id>/levels`). A track's streams are
numbered from its own key, so its row is asked for first.

The library is `test_perf_listing_window.py`'s (thousands of episode rows) with a music library
beside it holding three of plexav's tracks, pushed by a source to a server keeping its state in a
file, then served by a second server started on that file, as the production server restarts.
What that server reads from its file to answer is what `--verbose` logs (`rows: N read`).

    python3 tests/test_perf_stream_levels.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import music_rig as M
from music_rig import check, results
from server_command import server_command
from test_perf_listing_window import ROOT, catalog
from test_ports import free_port

TOKEN = M.TOKEN


def serve(path, state, log, agent_url):
    port = free_port()
    env = dict(os.environ, REX_PLEXTV_URL=agent_url)
    for name in ("REX_STATE", "REX_TOKEN", "PLEX_TOKEN"):
        env.pop(name, None)
    process = subprocess.Popen(server_command("--port", port, "--catalog", path, "--assert-claimed", "--gdm", "off",
                                              "--state", state, "--verbose", root=ROOT),
                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(600):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/identity" % port, timeout=2)
            return process, port
        except Exception:
            time.sleep(0.1)
    raise SystemExit("the server did not start")


def request(port, path, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data, method=method,
                                 headers={"Accept": "application/json", "X-Plex-Token": TOKEN, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            out = response.read()
            return response.status, json.loads(out) if out[:1] == b"{" else out
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def stop(process):
    process.terminate()
    try:
        process.wait(60)
    except subprocess.TimeoutExpired:
        process.kill()


def main():
    work = tempfile.mkdtemp(prefix="rex-levels-")
    path, episodes = catalog(work)
    data = json.load(open(path))
    data["sections"].append({"key": "9", "type": "artist", "title": "Music", "agent": "tv.plex.agents.music",
                             "scanner": "Plex Music", "language": "en-US", "uuid": "0" * 39 + "9",
                             "Location": [{"id": 9, "path": "/data/music"}]})
    json.dump(data, open(path, "w"))
    items = M.make_library(os.path.join(work, "media"))[:3]
    bytes_port, _ = M.range_server(os.path.join(work, "media"))
    agent_url, _ = M.agent_server()
    state = os.path.join(work, "state.db")
    log_path = os.path.join(work, "server.log")
    try:
        with open(log_path, "ab") as log:
            first, port = serve(path, state, log, agent_url)
            request(port, "/rex/sources/plexav", "PUT", {"url": "http://127.0.0.1:%d" % bytes_port, "mount": "/data"})
            status, body = request(port, "/rex/feed/plexav/items", "POST", {"items": items})
            check("three tracks pushed", status == 200 and body.get("created") == 3, (status, body))
            stop(first)
            second, port = serve(path, state, log, agent_url)
        tracks = request(port, "/library/sections/9/all?type=10")[1]["MediaContainer"].get("Metadata") or []
        row = request(port, "/library/metadata/%s" % tracks[0]["ratingKey"])[1]["MediaContainer"]["Metadata"][0] if tracks else {}
        stream = (row.get("Media") or [{}])[0].get("Part", [{}])[0].get("Stream", [{}])[0].get("id") if row else None
        check("the restored server lists the tracks among %d episodes" % episodes, len(tracks) == 3 and stream, (len(tracks), stream))
        before = os.path.getsize(log_path)
        started = time.time()
        status, _ = request(port, "/library/streams/%s/levels?subsample=128" % stream)
        took = time.time() - started
        time.sleep(0.5)  # the read count is logged as the request ends
        with open(log_path, "rb") as handle:
            handle.seek(before)
            logged = handle.read().decode(errors="replace")
        read = sum(int(count) for count in re.findall(r"rows: (\d+) read from the file", logged))
        check("a track with no levels answers 404, as an unanalysed PMS does", status == 404, status)
        check("finding its stream read %d of the library's rows from the file (%.3fs); at most its own" % (read, took),
              read <= 5, read)
        status, _ = request(port, "/library/streams/987654321/levels")
        check("a stream no row has is still a 404", status == 404, status)
        stop(second)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("SKIP: needs ffmpeg and ffprobe to make plexav's files")
        sys.exit(0)
    sys.exit(main())
