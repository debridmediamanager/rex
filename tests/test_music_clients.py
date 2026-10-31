#!/usr/bin/env python3
"""Plex Web, Plex for Android and Plexamp against rex's music library, request for request.

plexav's PMS 1.43.4 answered these clients on 2026-10-31 (`fixtures/music/<client>.jsonl`,
`probes/record_music_fixtures.py`); `music_rig.py` pushes the same 46 files to rex placed by their
albums' MusicBrainz ids. Each recorded request is reissued with plexav's keys swapped for rex's and
its answer compared as `harness/scripts/replay.py` compares: element structure, attribute presence
and values, the per-server fields left out.

What is left out, by design and named: no `Stations` (rex runs no sonic analysis, so a library
of music has no Stations shelf and an artist none), no `UltraBlurColors`/`thumbBlurHash` (no
artwork analysis), no `requiredBandwidths`/`deepAnalysisVersion` (no loudness analysis); a
library's `Meta` and settings' help text (rex's own wording); the Top Albums shelf's period
(drawn); an album's `Mood` list (plexav's merges its tracks' in an order not worked out); view
state where the recording had plays rex's fresh library has not (`viewCount`, `skipCount`,
`lastViewedAt`, `userRating`, which the playback legs below drive and check themselves).

    python3 tests/test_music_clients.py
"""
import json
import os
import re
import shutil
import sys
import urllib.parse

import music_rig as M
from music_rig import check, results

STATE = ("viewCount", "skipCount", "lastViewedAt", "viewedLeafCount", "userRating", "lastRatedAt", "viewOffset",
         "thumbBlurHash")
SKIP_PATHS = re.compile(r"^/(library/sections/1/prefs|playlists|:/timeline|playQueues|library/parts|music/|:/rate"
                        r"|status/sessions|library/metadata/augmentations|library/streams)")


def without_stations(body):
    """A recorded answer without what an analysed library adds (`Stations`) and with the drawn
    Top Albums period blanked; both sides go through it."""
    try:
        document = json.loads(body)
    except (TypeError, ValueError):
        return body
    container = document.get("MediaContainer") or {}
    hubs = container.get("Hub")
    if isinstance(hubs, list):
        hubs[:] = [hub for hub in hubs if hub.get("context") != "hub.music.stations"]
        for hub in hubs:
            # Drawn per process, on both servers: the period of these shelves.
            if hub.get("context") in ("hub.music.top.period", "hub.music.vault"):
                hub["title"] = hub["key"] = ""
        container["size"] = len(hubs)
    for row in container.get("Metadata") or []:
        row.pop("Stations", None)
        if row.get("type") == "album":
            row.pop("Mood", None)
    return json.dumps(document)


def replay(rex, name, mapping, parts, ignore=(), only=None):
    """Every GET of a recording, compared; answers the count of differing requests."""
    bad = 0
    for record in M.load(name):
        if record["method"] != "GET" or SKIP_PATHS.match(record["path"]) or (only and not only(record)):
            continue
        status, _, body = M.send(rex, record, mapping, parts)
        query = "&".join("%s=%s" % (k, v) for k, v in record["query"] if not k.startswith("X-Plex"))
        label = "%s %s?%s" % (name, record["path"], query[:90])
        if status != record["status"]:
            check(label, False, "status %s vs %s" % (record["status"], status))
            bad += 1
            continue
        differences = M.compare(without_stations(record["res_body"]), without_stations(body.decode()),
                                ignore=("Meta", "Similar", "Setting@summary") + tuple(ignore))
        bad += not check(label, not differences, differences[:8])
    return bad


def item_ids(rex, path):
    status, body = rex.request(path)
    return [row["playQueueItemID"] for row in (body.get("MediaContainer") or {}).get("Metadata") or []]


def main():
    work, items, rex, handler, asked = M.start()
    try:
        status, body = rex.push(items)
        assert status == 200 and body.get("created") == 46, body
        mapping, parts = M.key_map(rex)
        check("every plexav row has rex's row of the same title", len(mapping) == 53 and len(parts) == 46,
              (len(mapping), len(parts)))
        key = lambda reference: mapping[reference]

        # -- browsing, as each client did it, before anything played -----------------------------
        replay(rex, "web-music-browse", mapping, parts)
        # Recorded after plays, ratings and Plexamp's smart lists: view state left out, and the
        # shelves that follow plays checked by the playback leg below.
        replay(rex, "web-music-views", mapping, parts, ignore=STATE)
        replay(rex, "droid-music-browse", mapping, parts, ignore=STATE, only=lambda record: not record["path"].startswith("/hubs/"))
        replay(rex, "plexamp-browse", mapping, parts, ignore=STATE + ("totalSize", "size", "Metadata"),
               only=lambda record: "/library/metadata/" in record["path"] and "history" not in record["path"])

        # -- Plex Web plays In Rainbows -------------------------------------------------------------
        web = {r["path"] + "?" + "&".join(k for k, _ in r["query"]): r for r in M.load("web-music-play")}
        create = next(r for r in M.load("web-music-play") if r["method"] == "POST" and r["path"] == "/playQueues")
        status, _, body = M.send(rex, create, mapping, parts)
        queue = json.loads(body)["MediaContainer"]
        differences = M.compare(create["res_body"], body)
        check("Web's album queue (type=audio) is plexav's: ten tracks, the last named", status == 200 and not differences
              and queue.get("playQueueLastAddedItemID") == queue["Metadata"][-1]["playQueueItemID"], differences[:6])
        decision = next(r for r in M.load("web-music-play") if r["path"] == "/music/:/transcode/universal/decision")
        status, headers, body = M.send(rex, decision, mapping, parts)
        differences = M.compare(decision["res_body"], body, ignore=STATE)
        check("Web's music decision is plexav's direct play, as XML", status == 200 and body.startswith(b"<?xml")
              and not differences and b'mdeDecisionCode="1000"' in body and b'decision="directplay"' in body
              and b'location="direct"' in body, (status, differences[:6], body[:200]))
        read = next(r for r in M.load("web-music-play") if r["path"].startswith("/library/parts/"))
        status, headers, body = M.send(rex, read, mapping, parts)
        reference = dict((k.lower(), v) for k, v in read["res_headers"])
        on_disk = open(os.path.join(work, "media", items[10]["path"]), "rb").read()
        check("its part read is the file's bytes, with plexav's status and type", status == read["status"]
              and headers.get("Content-Type") == reference.get("content-type") and on_disk.startswith(body[:4096]),
              (status, headers.get("Content-Type"), reference.get("content-type")))
        track = key("32")
        # The client's own session, as Plex Web names it on its decision and its timelines.
        extra_session = "&X-Plex-Session-Identifier=" + dict(decision["query"])["X-Plex-Session-Identifier"]
        timeline = lambda state, time, extra="": rex.request(
            "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s&playbackTime=%d&playQueueItemID=%d&state=%s&hasMDE=1&time=%d&duration=45000%s"
            % (track, track, time, queue["Metadata"][0]["playQueueItemID"], state, time, extra + extra_session),
            headers={"X-Plex-Client-Identifier": "thuj6pv24kf5wr6qaqla8flz", "X-Plex-Product": "Plex Web"})
        status, playing = timeline("playing", 8000)
        first = json.loads(next(r for r in M.load("web-music-play") if r["path"] == "/:/timeline")["res_body"])
        check("a playing track's timeline is plexav's: ignore, no counts, its Bandwidths",
              status == 200 and not M.compare(json.dumps(first), json.dumps(playing), ignore=("bandwidth",)), playing)
        status, stopped = timeline("stopped", 25000, "&continuing=1")
        container = stopped.get("MediaContainer", {})
        check("left at 25 of 45 s for the next track it counts a play and a skip, as plexav's did",
              (container.get("playbackState"), container.get("viewCount"), container.get("skipCount"), container.get("viewOffset"))
              == ("complete", 1, 1, -1), container)
        status, early = rex.request(
            "/:/timeline?ratingKey=%s&key=%%2Flibrary%%2Fmetadata%%2F%s&playbackTime=34&playQueueItemID=%d&state=stopped&hasMDE=1&continuing=1&time=34&duration=45000"
            % (key("33"), key("33"), queue["Metadata"][1]["playQueueItemID"]))
        check("left after 34 ms it counts no play", early.get("MediaContainer", {}).get("viewCount", 0) == 0
              and early["MediaContainer"].get("playbackState") == "ignore", early)
        row = rex.rows("/library/metadata/%s" % track)[0]
        album = rex.rows("/library/metadata/%s" % key("31"))[0]
        check("the track, its album and its artist carry the play", (row.get("viewCount"), row.get("skipCount")) == (1, 1)
              and album.get("viewCount") == 1 and album.get("viewedLeafCount") == 1
              and rex.rows("/library/metadata/%s" % key("17"))[0].get("viewCount") == 1, (row.get("viewCount"), album))
        status, hubs = rex.request("/hubs/sections/1?count=12&includeLibraryPlaylists=1&includeStations=1")
        by_context = {hub["context"]: hub for hub in hubs["MediaContainer"]["Hub"]}
        check("Recently Played Music names the artist, More by its albums, no Stations shelf",
              [m["title"] for m in by_context.get("hub.music.recent.played", {}).get("Metadata", [])] == ["Radiohead"]
              and [m["title"] for m in by_context.get("hub.music.recent.artist", {}).get("Metadata", [])] == ["OK Computer", "In Rainbows"]
              and "hub.music.stations" not in by_context, list(by_context))
        status, history = rex.request("/status/sessions/history/all?metadataItemID=%s&sort=viewedAt:desc&accountID=1" % key("17"))
        check("the artist's history lists the play", status == 200 and (history.get("MediaContainer") or {}).get("size", 0) >= 1, history)

        # -- Plex for Android: an album's children queued as `type=video` ---------------------------
        record = next(r for r in M.load("droid-music-play") if r["method"] == "POST" and r["path"] == "/playQueues")
        status, _, body = M.send(rex, record, mapping, parts)
        differences = M.compare(record["res_body"], body, ignore=STATE)
        check("Android's `type=video` queue of an album's children is plexav's ten tracks", status == 200 and not differences,
              differences[:6])
        record = next(r for r in M.load("droid-music-play") if r["path"] == "/music/:/transcode/universal/decision")
        status, _, body = M.send(rex, record, mapping, parts)
        differences = M.compare(record["res_body"], body, ignore=STATE)
        check("Android's music decision (protocol=hls) is plexav's, as JSON", status == 200 and not differences, differences[:6])

        # -- Plexamp: queue with loudness ramps, windows, whole-file reads ---------------------------
        recorded = [r for r in M.load("plexamp-play")]
        create = next(r for r in recorded if r["method"] == "POST" and r["path"] == "/playQueues")
        status, _, body = M.send(rex, create, mapping, parts)
        queue = json.loads(body)["MediaContainer"]
        check("Plexamp's queue (includeLoudnessRamps=1) is plexav's", status == 200
              and not M.compare(create["res_body"], body, ignore=STATE), M.compare(create["res_body"], body, ignore=STATE)[:6])
        pid = str(queue["playQueueID"])
        windowed = next(r for r in recorded if r["path"].startswith("/playQueues/") and ("center", "40") in [tuple(q) for q in r["query"]])
        status, _, body = M.send(rex, windowed, mapping, parts, path="/playQueues/" + pid,
                                 extra={"center=40": "center=%d" % queue["Metadata"][1]["playQueueItemID"]})
        window_query = [(k, v) for k, v in windowed["query"] if not k.startswith("X-Plex")]
        center = queue["Metadata"][1]["playQueueItemID"]
        status, _, body = rex.request("/playQueues/%s?%s" % (pid, urllib.parse.urlencode(
            [(k, str(center) if k == "center" else v) for k, v in window_query])), raw=True)
        check("a windowed read (`window=30&center=`) answers the whole queue, as plexav's ten",
              status == 200 and len(json.loads(body)["MediaContainer"]["Metadata"]) == 10, status)
        download = next(r for r in recorded if r["path"].startswith("/library/parts/"))
        status, headers, body = M.send(rex, download, mapping, parts)
        recorded_headers = dict((k.lower(), v) for k, v in download["res_headers"])
        check("a whole-file read (`download=1`) is the file, as an attachment named as plexav named it",
              status == download["status"] and body == on_disk
              and headers.get("Content-Type") == recorded_headers.get("content-type")
              and headers.get("Content-Disposition") == recorded_headers.get("content-disposition"),
              (status, len(body), headers.get("Content-Type"), headers.get("Content-Disposition")))
        levels = next(r for r in recorded if r["path"].endswith("/levels"))
        status, _, _ = M.send(rex, levels, mapping, parts)
        check("a stream's loudness levels are plexav's 404 (no analysis)", status == 404, status)
        status, detail = rex.request("/library/metadata/%s?asyncAugmentMetadata=1" % key("17"))
        augmentation = detail["MediaContainer"].get("augmentationKey", "")
        status, answer = rex.request(augmentation + "?wait=1")
        check("an augmentation a detail named answers plexav's empty container", status == 200 and answer == {"MediaContainer": {"size": 0}},
              (augmentation, status, answer))
        status, _ = rex.request("/library/metadata/augmentations/99999?wait=1")
        check("one never named stays a 404", status == 404, status)

        # -- Plexamp's first run: five smart lists over the library, evaluated when read ------------
        setup = [r for r in M.load("plexamp-setup")]
        made = {}
        for record in (r for r in setup if r["method"] == "POST" and r["path"] == "/playlists"):
            status, _, body = M.send(rex, record, mapping, parts)
            row = json.loads(body)["MediaContainer"]["Metadata"][0]
            made[dict(record["query"])["title"]] = row["ratingKey"]
            check("Plexamp's smart list %r is created" % dict(record["query"])["title"], status == 200 and row.get("smart") is True, (status, row))
        # Then Plexamp PUTs each list's summary with `includeFields=thumbBlurHash` beside it, which
        # plexav answered 200; rex refused the unknown name with a 400 until the request was
        # replayed as Plexamp sends it (a hand-made PUT without includeFields passed).
        theirs = {json.loads(r["res_body"])["MediaContainer"]["Metadata"][0]["ratingKey"]: dict(r["query"])["title"]
                  for r in setup if r["method"] == "POST" and r["path"] == "/playlists"}
        for record in (r for r in setup if r["method"] == "PUT" and r["path"].startswith("/playlists/")):
            title = theirs[record["path"].rsplit("/", 1)[1]]
            status, _, _ = M.send(rex, record, mapping, parts, path="/playlists/%s" % made[title])
            listed = rex.rows("/playlists/%s" % made[title])
            summary = dict(record["query"])["summary"]
            check("Plexamp's summary PUT on %r answers 200 and is kept, as plexav's" % title,
                  status == record["status"] == 200 and listed and listed[0].get("summary") == summary,
                  (status, listed[:1]))
        check("All Music holds every track", len(rex.rows("/playlists/%s/items" % made["All Music"])) == 46)
        played = [row["title"] for row in rex.rows("/playlists/%s/items" % made["Recently Played"])]
        check("Recently Played is read when asked: the track played above", played == ["15 Step"], played)

        # -- Plex Web's actions: an audio list of an album, a rating ----------------------------------
        actions = M.load("web-music-actions")
        record = next(r for r in actions if r["method"] == "POST" and r["path"] == "/playlists")
        status, _, body = M.send(rex, record, mapping, parts)
        row = json.loads(body)["MediaContainer"]["Metadata"][0]
        check("Web's audio playlist of The Slip holds its ten tracks", status == 200 and row.get("playlistType") == "audio"
              and row.get("leafCount") == 10 and len(rex.rows("/playlists/%s/items" % row["ratingKey"])) == 10, row)
        record = next(r for r in actions if r["method"] == "PUT" and r["path"] == "/:/rate")
        status, _, _ = M.send(rex, record, mapping, parts)
        rated = rex.rows("/library/metadata/%s" % key("43"))[0]
        check("Web's rating of an album is kept on it", status == 200 and rated.get("userRating") == 5.0, rated.get("userRating"))

        # -- Search ---------------------------------------------------------------------------------
        record = next(r for r in M.load("web-music-search") if r["path"] == "/library/search")
        status, _, body = M.send(rex, record, mapping, parts)
        theirs = [(r["Metadata"]["type"], r["Metadata"]["title"]) for r in json.loads(record["res_body"])["MediaContainer"]["SearchResult"] if "Metadata" in r]
        ours = [(r["Metadata"]["type"], r["Metadata"]["title"]) for r in json.loads(body)["MediaContainer"].get("SearchResult", []) if "Metadata" in r]
        check("searching Radiohead finds plexav's artist and albums", status == 200 and set(theirs) <= set(ours), (theirs, ours))
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
