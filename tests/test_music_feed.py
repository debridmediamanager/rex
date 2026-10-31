#!/usr/bin/env python3
"""Tracks a source pushes (`kind: "track"`, docs/feed-api.md "Tracks") become plexav's music library.

`music_rig.py` makes plexav's 46 files with plexav's own FFmpeg commands, serves them from a plain
Range server and answers Plex's agent from what plexav's PMS 1.43.4 asked it
(`fixtures/music/agent.json`). Every album is placed by MusicBrainz's release id except The Slip,
placed by Plex's album guid; rex reads no tag and no file name:

* each album is Plex's album for its id and each track Plex's track on its disc and number (the
  agent's `Jigsaw Falling Into Place`, not the file's `into`), under Plex's artist;
* a track's Media is plexav's for the same file (`FLAC (Stereo)`, no video fields);
* a track nothing names is shown by its `display`, grouped by those names; with none, PMS's
  `[Unknown Artist]` and `[Unknown Album]`, titled as its file;
* held without a probe, moved on its key when placed again, trashed when removed; a file that is
  not audio refused as a track, an audio file refused as anything else.

    python3 tests/test_music_feed.py
"""
import json
import os
import shutil
import subprocess
import sys

import music_rig as M
from music_rig import check, results

SLIP = "plex://album/5d07c2fb403c640290929d98"


def reference_track(title):
    for record in M.load("web-music-play"):
        if record["path"].startswith("/library/metadata/") and record["res_body"]:
            rows = json.loads(record["res_body"]).get("MediaContainer", {}).get("Metadata") or []
            if rows and rows[0].get("type") == "track" and rows[0].get("title") == title:
                return record
    return None


def main():
    work, items, rex, handler, asked = M.start()
    try:
        for item in items:
            if item["path"].startswith("music/Nine Inch Nails/"):
                item["ids"] = {"plex": SLIP}
        status, body = rex.push(items)
        check("46 tracks pushed with their albums' ids are created", status == 200 and body.get("created") == 46
              and not body.get("rejected"), (status, body))
        artists = rex.rows("/library/sections/1/all?type=8")
        albums = rex.rows("/library/sections/1/all?type=9")
        tracks = rex.rows("/library/sections/1/all?type=10")
        check("three artists, four albums, 46 tracks, as plexav's library",
              (len(artists), len(albums), len(tracks)) == (3, 4, 46), (len(artists), len(albums), len(tracks)))
        by_title = {row["title"]: row for row in albums}
        check("In Rainbows is Plex's album for its MusicBrainz release id",
              by_title.get("In Rainbows", {}).get("guid") == "plex://album/5d07c2b1403c6402908fddb6", by_title.keys())
        matched = [query for path, query in asked if path == "/library/metadata/matches"]
        check("the release id is asked as plexav's PMS asked it (`guid=mbid://...&type=9`)",
              any(("guid", "mbid://3b408cb5-7d51-4188-b07c-fabcf308cda3") in query and ("type", "9") in query for query in matched),
              matched[:1])
        check("The Slip, pushed by Plex's album guid, is that album with no match asked",
              by_title.get("The Slip", {}).get("guid") == SLIP
              and not any(("guid", "mbid://12b57d46-a192-499e-a91f-7da66790a1c1") in query for query in matched),
              by_title.get("The Slip"))
        jigsaw = next((row for row in tracks if row["title"].lower().startswith("jigsaw")), {})
        check("a track is Plex's track on its number: the agent's title, not the file's",
              jigsaw.get("title") == "Jigsaw Falling Into Place" and jigsaw.get("guid", "").startswith("plex://track/")
              and jigsaw.get("index") == 9 and jigsaw.get("parentTitle") == "In Rainbows", jigsaw)
        radiohead = next((row for row in artists if row["title"] == "Radiohead"), {})
        check("the artist is the album's parent at Plex's agent",
              radiohead.get("guid") == "plex://artist/5d07bbfc403c6402904a5ecc", radiohead)

        # The Media of the same file, against plexav's own analysis of it.
        step = next(row for row in tracks if row["title"] == "15 Step")
        mine = rex.rows("/library/metadata/%s" % step["ratingKey"])[0]
        theirs = json.loads(reference_track("15 Step")["res_body"])["MediaContainer"]["Metadata"][0]
        differences = M.compare(json.dumps({"Media": theirs["Media"]}), json.dumps({"Media": mine["Media"]}),
                                ignore=("accessible", "exists"))
        check("a FLAC track's Media, Part and Stream are plexav's for the same file", not differences, differences)
        # plexav's bitrate rule (fixtures/music/media.json): a FLAC's Media counts its audio frames, from the
        # first packet its source's probe names; its Stream is the file's.
        probe = next(item for item in items if item["path"].endswith("01 - 15 Step.flac"))["media_info"]
        size = int(probe["format"]["size"])
        frames = round((size - int(probe["packets"][0]["pos"])) * 8 / mine["Media"][0]["duration"])
        check("a FLAC track's bitrate is its audio frames' over its length, its stream's the file's, as plexav's",
              (mine["Media"][0].get("bitrate"), mine["Media"][0]["Part"][0]["Stream"][0].get("bitrate"))
              == (frames, round(int(probe["format"]["bit_rate"]) / 1000)),
              (mine["Media"][0].get("bitrate"), frames))
        stream = mine["Media"][0]["Part"][0]["Stream"][0]
        check("its stream is titled as a track's: FLAC (Stereo)", stream.get("displayTitle") == "FLAC (Stereo)", stream)

        # Nothing names it: what its source displays, grouped by those names.
        loose = os.path.join(work, "media", "loose")
        os.makedirs(loose, exist_ok=True)
        source = os.path.join(work, "media", items[0]["path"])
        for name in ("a.mp3", "b.mp3", "c.mp3", "d.mp3"):
            shutil.copy(source, os.path.join(loose, name))
        probe = items[0]["media_info"]
        display = {"title": "Opening", "artist": {"title": "Home Band"}, "album": {"title": "Garage Tapes", "date": "2019-05-01"}}
        status, body = rex.push([
            {"id": "loose/a.mp3", "path": "loose/a.mp3", "kind": "track", "track": 1, "media_info": probe, "display": display},
            {"id": "loose/b.mp3", "path": "loose/b.mp3", "kind": "track", "track": 2, "media_info": probe,
             "ids": {"mbid": "00000000-0000-0000-0000-000000000000"},
             "display": dict(display, title="Closing")},
            {"id": "loose/c.mp3", "path": "loose/c.mp3", "kind": "track", "media_info": probe},
            {"id": "loose/d.mp3", "path": "loose/d.mp3", "kind": "track", "media_info": None},
            {"id": "loose/e.mkv", "path": "loose/e.mkv", "kind": "track", "media_info": probe},
            {"id": "loose/f.mp3", "path": "loose/f.mp3", "media_info": probe},
            {"id": "loose/g.mp3", "path": "loose/g.mp3", "kind": "track", "probe": "digest-1"},
        ])
        rejected = {entry["id"]: entry["reason"] for entry in body.get("rejected") or []}
        check("tracks nothing or something unknown names are created, one held without its probe",
              body.get("created") == 3 and body.get("held") == 1, body)
        check("a video file pushed as a track is refused", rejected.get("loose/e.mkv") == "not an audio file", rejected)
        check("a new track named by its probe's digest alone is asked for whole", body.get("wants") == ["loose/g.mp3"], body)
        check("an audio file pushed as anything else is refused", rejected.get("loose/f.mp3") == "not a video file", rejected)
        band = next((row for row in rex.rows("/library/sections/1/all?type=8") if row["title"] == "Home Band"), None)
        check("the display's artist is an artist of its own, no agent guid", band and band["guid"].startswith("local://"), band)
        garage = rex.rows("/library/metadata/%s/children" % band["ratingKey"]) if band else []
        check("its album holds both tracks, the unknown release id's too",
              [row["title"] for row in garage] == ["Garage Tapes"] and garage[0].get("year") == 2019, garage)
        songs = rex.rows("/library/metadata/%s/children" % garage[0]["ratingKey"]) if garage else []
        check("each track is titled and numbered as displayed",
              [(row["title"], row["index"]) for row in songs] == [("Opening", 1), ("Closing", 2)], songs)
        unknown = next((row for row in rex.rows("/library/sections/1/all?type=10") if row["title"] == "c"), {})
        check("with no display it is titled as its file under PMS's unknown artist and album",
              (unknown.get("grandparentTitle"), unknown.get("parentTitle")) == ("[Unknown Artist]", "[Unknown Album]"), unknown)
        keys = rex.request("/rex/feed/plexav/held")[1]
        check("the held track is listed held", "loose/d.mp3" in json.dumps(keys), keys)
        status, body = rex.push([{"id": "loose/d.mp3", "path": "loose/d.mp3", "kind": "track", "media_info": probe,
                                  "display": dict(display, title="Encore")}])
        check("its probe shows it", body.get("created") == 1, body)

        # Placed again: same key, another track of the album.
        key = step["ratingKey"]
        moved = dict(next(item for item in items if item["path"].endswith("01 - 15 Step.flac")), track=2)
        rex.request("/:/scrobble?identifier=com.plexapp.plugins.library&key=%s" % key)
        status, body = rex.push([moved])
        row = rex.rows("/library/metadata/%s" % key)
        check("a track placed on another number moves there on its key",
              body.get("updated") == 1 and row and row[0]["title"] == "Bodysnatchers" and row[0]["index"] == 2, (body, row[:1]))
        check("and keeps the play it had", row and row[0].get("viewCount") == 1, row[:1] and row[0].get("viewCount"))
        status, body = rex.request("/rex/feed/plexav/items", "DELETE", {"ids": [moved["id"]]}, {"Content-Type": "application/json"})
        row = rex.rows("/library/metadata/%s" % key)
        check("a removed track goes to the trash", body.get("trashed") == 1 and row and row[0].get("deletedAt"), (body, row[:1]))
        album = rex.rows("/library/metadata/%s" % row[0]["parentRatingKey"]) if row else []
        check("its album is marked with it, as a trashed episode marks its season (inferred, not measured for music)",
              album and album[0].get("deletedAt"), album[:1])

        # A library: `library` naming no music library refuses the track.
        status, body = rex.push([dict(items[1], id="other", path=items[1]["path"], library="7")])
        check("a track no music library holds is refused, named",
              (body.get("rejected") or [{}])[0].get("reason", "").startswith("no music library holds"), body)
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
