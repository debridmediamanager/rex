#!/usr/bin/env python3
"""A source declares a music library for one of its directories (`directories`: `music`).

As `shows` and `movies` titles do (docs/feed-api.md "Libraries a source declares"), a `music` title
makes an artist library on `<mount>/<name>` when the source registers, unless one is there already;
it is the music library Add Library's Music makes, which `fixtures/music/sections.json` records as
plexav's PMS lists it (`GET /library/sections`). The server here starts with a movie and a show
library only, as rex seeds one, so tracks have nowhere to go until the source declares it.

    python3 tests/test_music_library_declared.py
"""
import json
import os
import shutil
import sys
import tempfile

import music_rig as M
from music_rig import check, results

PLEXAV = json.load(open(os.path.join(M.FIXTURES, "sections.json")))["body"]["MediaContainer"]["Directory"]
SEEDED = [{"key": "1", "type": "movie", "title": "Movies", "uuid": "0" * 39 + "1", "Location": [{"id": 1, "path": "/data/movies"}]},
          {"key": "2", "type": "show", "title": "TV Shows", "uuid": "0" * 39 + "2", "Location": [{"id": 2, "path": "/data/shows"}]}]


def main():
    work = tempfile.mkdtemp(prefix="rex-music-declared-")
    items = M.make_library(os.path.join(work, "media"))[:3]
    bytes_port, _ = M.range_server(os.path.join(work, "media"))
    agent_url, _ = M.agent_server()
    rex = M.Rex(work, bytes_port, agent_url, extra_sections=SEEDED, music=False,
                register={"directories": [{"name": "music", "music": "Music"}]})
    try:
        status, body = rex.request("/library/sections")
        music = [row for row in body["MediaContainer"]["Directory"] if row.get("type") == "artist"]
        check("registering a source that declares `music` makes one music library", len(music) == 1 and music[0]["title"] == "Music", music)
        theirs = next(row for row in PLEXAV if row["type"] == "artist")
        differences = M.compare(json.dumps(theirs), json.dumps(music[0]) if music else "{}",
                                ignore=("scannedAt", "contentChangedAt", "updatedAt", "createdAt", "path"))
        check("it is listed as plexav lists its music library (agent, scanner, language, group)", music and not differences, differences)
        check("on <mount>/music", music and music[0]["Location"][0]["path"] == "/data/music", music and music[0]["Location"])
        status, body = rex.request("/rex/sources/plexav", "PUT", {"url": "http://127.0.0.1:%d" % bytes_port, "mount": "/data",
                                                                  "directories": [{"name": "music", "music": "Music"}]},
                                   {"Content-Type": "application/json"})
        status, body = rex.request("/library/sections")
        check("registering again makes no second one", len([r for r in body["MediaContainer"]["Directory"] if r.get("type") == "artist"]) == 1)
        status, body = rex.push(items)
        tracks = rex.rows("/library/sections/%s/all?type=10" % music[0]["key"]) if music else []
        check("the source's tracks go to it", body.get("created") == 3 and len(tracks) == 3, (body, len(tracks)))
    finally:
        rex.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("SKIP: needs ffmpeg and ffprobe to make plexav's files")
        sys.exit(0)
    sys.exit(main())
