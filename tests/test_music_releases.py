#!/usr/bin/env python3
"""Two releases of one album are two album rows of its guid, each with its own tracks, as plexav shows them.

plexav's PMS 1.43.4 held Radiohead's In Rainbows twice on 2026-11-01: `In Rainbows (2007)/`, ten FLAC
tones, and `In Rainbows (2007) [AAC]/`, the same ten as AAC in m4a, both matched to Plex's album
`plex://album/5d07c2b1403c6402908fddb6`. It listed one artist and two album rows of that guid, 31 and 95,
each with its own ten tracks of one Media (`fixtures/music/album-releases.json`, recorded by
`probes/record_album_releases.py`). rex put both releases' tracks on one album of twenty. A release is the
track's source and its `release`, by default the folder above its path (docs/feed-api.md "Tracks"):

* the AAC folder pushed beside the FLAC one is a second album row of the guid: its own ten tracks, the
  album listing, the artist's albums, search and the hubs list both, as plexav's;
* the same folder pushed by a second source is an album of its own too;
* a re-push changes no key, and a played track keeps its play;
* with `REX_OLD_SERVER` (a rex that merged them, f2f14ec4 or older) its state's merged album splits on the
  next push: the first release keeps the album's key, the second's tracks theirs and their plays.

    python3 tests/test_music_releases.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import music_rig as M
from music_rig import check, results

FIXTURE = json.load(open(os.path.join(M.FIXTURES, "album-releases.json")))
GUID = "plex://album/5d07c2b1403c6402908fddb6"
FLAC, AAC = "music/Radiohead/In Rainbows (2007)", "music/Radiohead/In Rainbows (2007) [AAC]"
WEB = "includeCollections=1&includeExternalMedia=1&includeAdvanced=1&includeMeta=1"
DETAIL = ("includeConcerts=1&includeExtras=1&includeOnDeck=1&includePopularLeaves=1&includePreferences=1"
          "&includeReviews=1&includeChapters=1&includeStations=1&includeExternalMedia=1")
SEARCH = "/library/search?query=In%20Rainbows&limit=100&searchTypes=movies,music,people,tv&includeCollections=1&includeExternalMedia=1"


def theirs(path, query=""):
    """plexav's answer to the request recorded at `path`, its query starting with `query`."""
    return next(answer["body"]["MediaContainer"] for answer in FIXTURE["answers"]
                if answer["path"] == path and answer["query"].startswith(query))


def theirs_album(folder):
    return next(key for key, name in FIXTURE["albums"].items() if name == folder.rsplit("/", 1)[1])


def aac_release(work, items):
    """plexav's AAC copy: the FLAC release's tones as AAC in m4a, pushed with no `release` (its folder)."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    out = []
    for item in items:
        if not item["path"].startswith(FLAC + "/"):
            continue
        relative = "%s/%s.m4a" % (AAC, item["path"].rsplit("/", 1)[1].rsplit(".", 1)[0])
        path = os.path.join(work, "media", relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-i", os.path.join(work, "media", item["path"]),
                        "-c:a", "aac", "-b:a", "192k", path], check=True)
        probe = json.loads(subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams",
                                           path], check=True, capture_output=True).stdout)
        moved = dict(item, id=relative, path=relative, media_info=probe)
        moved.pop("release")
        out.append(moved)
    return out


def in_rainbows(rex):
    return [row for row in rex.rows("/library/sections/1/all?type=9&" + WEB) if row["title"] == "In Rainbows"]


def keys(rex):
    """Every music row's key by (type, folder of its first Part or its own title, parent): what a re-push must keep."""
    out = {}
    for kind in (8, 9, 10):
        for row in rex.rows("/library/sections/1/all?type=%d" % kind):
            part = (row.get("Media") or [{}])[0].get("Part", [{}])[0].get("file", "")
            out[(row["type"], row["title"], part)] = row["ratingKey"]
    return out


def album_of(rex, folder):
    """rex's album row whose tracks are `folder`'s files."""
    for row in in_rainbows(rex):
        tracks = rex.rows("/library/metadata/%s/children?excludeAllLeaves=1" % row["ratingKey"])
        if tracks and all(("/%s/" % folder) in track["Media"][0]["Part"][0]["file"] for track in tracks):
            return row["ratingKey"]
    return None


def releases(rex, items, aac):
    status, body = rex.push(aac)
    check("plexav's AAC copy of In Rainbows, pushed beside its FLAC release, is created",
          status == 200 and body.get("created") == 10 and not body.get("rejected"), (status, body))

    # The album listing: two rows of the guid, as plexav's.
    rows, reference = in_rainbows(rex), [row for row in theirs("/library/sections/1/all", "type=9")["Metadata"]
                                         if row["guid"] == GUID]
    check("the album listing has two In Rainbows rows, one per release, as plexav's (31 and 95)",
          len(rows) == len(reference) == 2 and len({row["ratingKey"] for row in rows}) == 2,
          [(row["ratingKey"], row.get("guid")) for row in rows])
    fields = ("type", "title", "guid", "index", "parentTitle", "parentGuid", "year", "originallyAvailableAt")
    check("both are Plex's album of that guid under the one artist, as plexav lists them",
          sorted(tuple(row.get(field) for field in fields) for row in rows)
          == sorted(tuple(row.get(field) for field in fields) for row in reference),
          ([tuple(row.get(field) for field in fields) for row in rows], [tuple(row.get(field) for field in fields) for row in reference]))
    check("the library holds five albums: plexav's four and the second release",
          len(rex.rows("/library/sections/1/all?type=9&" + WEB)) == 5, len(rex.rows("/library/sections/1/all?type=9")))

    # Each album: its own ten tracks of one Media, the same tracks of Plex's agent.
    flac, aac_album = album_of(rex, FLAC.rsplit("/", 1)[1]), album_of(rex, AAC.rsplit("/", 1)[1])
    check("each release's tracks are one album of their own", flac and aac_album and flac != aac_album, (flac, aac_album))
    # Two albums found as one release each: the checks below need both.
    found_both = bool(flac and aac_album)
    guids = {}
    for folder, mine, codec in ((FLAC, flac, "flac"), (AAC, aac_album, "aac")):
        reference_key = theirs_album(folder)
        detail = (rex.rows("/library/metadata/%s?%s" % (mine, DETAIL)) or [{}])[0]
        expected = theirs("/library/metadata/%s" % reference_key)["Metadata"][0]
        check("%s's album counts its own ten tracks (leafCount), as plexav's %s" % (codec, reference_key),
              detail.get("leafCount") == expected["leafCount"] == 10, (detail.get("leafCount"), expected["leafCount"]))
        status, body = rex.request("/library/metadata/%s/children?excludeAllLeaves=1" % mine)
        container = (body or {}).get("MediaContainer") or {} if status == 200 else {}
        tracks = container.get("Metadata") or []
        recorded = theirs("/library/metadata/%s/children" % reference_key)
        check("%s's album lists ten tracks, each one %s Media, numbered as plexav's %s" % (codec, codec, reference_key),
              [(track["index"], len(track["Media"]), track["Media"][0].get("audioCodec")) for track in tracks]
              == [(track["index"], len(track["Media"]), track["Media"][0].get("audioCodec")) for track in recorded["Metadata"]],
              [(track["index"], len(track["Media"]), track["Media"][0].get("audioCodec")) for track in tracks])
        same = ("size", "parentIndex", "parentTitle", "parentYear", "title1", "title2", "viewGroup", "grandparentTitle")
        check("%s's children container is plexav's %s" % (codec, reference_key),
              {field: container.get(field) for field in same} == {field: recorded.get(field) for field in same},
              ({field: container.get(field) for field in same}, {field: recorded.get(field) for field in same}))
        guids[codec] = sorted(track["guid"] for track in tracks)
    check("both albums hold Plex's same ten tracks (guids), as plexav's two do",
          guids.get("flac") == guids.get("aac") and len(set(guids.get("flac") or [])) == 10, guids)

    # The artist: one row, three albums.
    artists = [row for row in rex.rows("/library/sections/1/all?type=8&" + WEB) if row["title"] == "Radiohead"]
    check("Radiohead is one artist, as plexav's", len(artists) == 1, artists)
    artist = artists[0]["ratingKey"] if artists else ""
    mine = sorted(row["title"] for row in rex.rows("/library/metadata/%s/children?excludeAllLeaves=1" % artist))
    recorded = sorted(row["title"] for row in theirs("/library/metadata/%s/children" % reference[0]["parentRatingKey"])["Metadata"])
    check("its albums are both releases and OK Computer, as plexav's", mine == recorded, (mine, recorded))

    # The artist page's popular tracks (`group=title`): each track once, from the first release, as plexav's.
    status, body = rex.request("/library/sections/1/all?album.subformat!=Compilation,Live&artist.id=%s&group=title&limit=100"
                               "&ratingCount>=1&resolveTags=1&sort=ratingCount:desc&type=10" % artist)
    popular = [(row["title"], row["parentRatingKey"]) for row in ((body or {}).get("MediaContainer") or {}).get("Metadata") or []
               if row.get("parentTitle") == "In Rainbows"]
    recorded = [(row["title"], row["parentRatingKey"]) for row in theirs("/library/sections/1/all", "album.subformat")["Metadata"]
                if row.get("parentTitle") == "In Rainbows"]
    check("the artist's popular tracks list each In Rainbows track once, from the first release's album, as plexav's (31)",
          found_both and len(popular) == len(recorded) == 10 and {album for _, album in popular} == {flac}
          and {album for _, album in recorded} == {theirs_album(FLAC)}, (popular[:3], recorded[:3]))

    # Search and the hubs find both.
    status, body = rex.request(SEARCH)
    found = sorted(hit["Metadata"]["ratingKey"] for hit in ((body or {}).get("MediaContainer") or {}).get("SearchResult") or []
                   if hit.get("Metadata", {}).get("type") == "album")
    recorded = [hit["Metadata"]["ratingKey"] for hit in theirs("/library/search")["SearchResult"] if hit["Metadata"]["type"] == "album"]
    check("a search for In Rainbows finds both albums, as plexav's (31, 95)", found_both and found == sorted([flac, aac_album]) and len(recorded) == 2,
          (found, recorded))
    status, body = rex.request("/hubs/sections/1?count=12&includeLibraryPlaylists=1&includeStations=1&includeRecentChannels=1"
                               "&includeMeta=1&includeExternalMetadata=1&excludeFields=summary")
    added = next((hub for hub in ((body or {}).get("MediaContainer") or {}).get("Hub") or []
                  if hub.get("hubIdentifier") == "music.recent.added.1"), {})
    shown = sorted(row["ratingKey"] for row in added.get("Metadata") or [] if row.get("guid") == GUID)
    recorded = [row["ratingKey"] for hub in theirs("/hubs/sections/1")["Hub"] for row in hub.get("Metadata") or []
                if hub["hubIdentifier"] == "music.recent.artist.1" and row.get("guid") == GUID]
    check("Recently Added shows both albums, as plexav's album shelves show 31 and 95",
          found_both and shown == sorted([flac, aac_album]) and recorded == ["31", "95"], (shown, recorded))
    return flac, aac_album


def kept(rex, items, aac, flac):
    before = keys(rex)
    status, body = rex.push(items + aac)
    check("a re-push of both releases changes nothing", body.get("unchanged") == len(items) + len(aac) and not body.get("updated"),
          body)
    check("and keeps every key: artists, both albums, every track", keys(rex) == before,
          sorted(set(keys(rex).items()) ^ set(before.items()))[:6])
    track = (rex.rows("/library/metadata/%s/children" % flac) or [{}])[0].get("ratingKey")
    rex.request("/:/scrobble?identifier=com.plexapp.plugins.library&key=%s" % track)
    rex.push([item for item in items if item["path"].startswith(FLAC + "/")])
    row = (rex.rows("/library/metadata/%s" % track) or [{}])[0]
    check("a played track of the first release keeps its album and its play through a re-push",
          row.get("parentRatingKey") == flac and row.get("viewCount") == 1, (row.get("parentRatingKey"), row.get("viewCount")))


def second_source(rex, items, bytes_port):
    status, _ = rex.request("/rex/sources/second", "PUT", {"url": "http://127.0.0.1:%d" % bytes_port, "mount": "/data"},
                            {"Content-Type": "application/json"})
    flac = [item for item in items if item["path"].startswith(FLAC + "/")]
    status, body = rex.request("/rex/feed/second/items", "POST", {"items": flac}, {"Content-Type": "application/json"})
    check("the same release pushed by a second source is created", body.get("created") == 10, body)
    rows = in_rainbows(rex)
    counts = sorted((rex.rows("/library/metadata/%s?%s" % (row["ratingKey"], DETAIL)) or [{}])[0].get("leafCount") for row in rows)
    check("and is an album of its own: three In Rainbows of ten tracks each", counts == [10, 10, 10], counts)


def legacy(work, items, aac, bytes_port, agent_url):
    """A state an older rex wrote, both releases on one album, read by this one."""
    old = os.environ.get("REX_OLD_SERVER")
    if not old:
        print("SKIP  a merged album from an older rex's state (set REX_OLD_SERVER)")
        return
    state = os.path.join(work, "legacy.db")
    saved = os.environ.get("REX_SERVER")
    os.environ["REX_SERVER"] = old
    try:
        rex = M.Rex(work, bytes_port, agent_url, args=("--state", state))
    finally:
        if saved is None:
            os.environ.pop("REX_SERVER")
        else:
            os.environ["REX_SERVER"] = saved
    try:
        rex.push(items)
        rex.push(aac)
        merged = in_rainbows(rex)
        tracks = rex.rows("/library/metadata/%s/children" % merged[0]["ratingKey"]) if merged else []
        played = next((track["ratingKey"] for track in tracks if "[AAC]" in track["Media"][0]["Part"][0]["file"]), None)
        rex.request("/:/scrobble?identifier=com.plexapp.plugins.library&key=%s" % played)
        aac_keys = sorted(track["ratingKey"] for track in tracks if "[AAC]" in track["Media"][0]["Part"][0]["file"])
    finally:
        rex.stop()
    if len(merged) != 1 or len(tracks) != 20:
        print("SKIP  REX_OLD_SERVER kept the releases apart already (%d albums, %d tracks)" % (len(merged), len(tracks)))
        return
    rex = M.Rex(work, bytes_port, agent_url, args=("--state", state))
    try:
        status, body = rex.push(items + aac)
        check("an older rex's merged album: the next push moves the second release's tracks", body.get("updated") == 10, body)
        flac, split = album_of(rex, FLAC.rsplit("/", 1)[1]), album_of(rex, AAC.rsplit("/", 1)[1])
        check("the first release keeps the merged album's key", flac == merged[0]["ratingKey"], (flac, merged[0]["ratingKey"]))
        moved = sorted(track["ratingKey"] for track in rex.rows("/library/metadata/%s/children" % split)) if split else []
        check("the second release is an album of its own, its tracks on their keys", split and moved == aac_keys, (split, moved, aac_keys))
        row = (rex.rows("/library/metadata/%s" % played) or [{}])[0]
        check("and a played one keeps its play", row.get("viewCount") == 1, row.get("viewCount"))
    finally:
        rex.stop()


def main():
    work = tempfile.mkdtemp(prefix="rex-music-")
    items = M.make_library(os.path.join(work, "media"))
    bytes_port, _ = M.range_server(os.path.join(work, "media"))
    agent_url, _ = M.agent_server()
    rex, aac = M.Rex(work, bytes_port, agent_url), []
    try:
        status, body = rex.push(items)
        check("plexav's 46 tracks are created", body.get("created") == 46, body)
        aac = aac_release(work, items)
        flac, _ = releases(rex, items, aac)
        kept(rex, items, aac, flac)
        second_source(rex, items, bytes_port)
    finally:
        rex.stop()
    try:
        legacy(work, items, aac, bytes_port, agent_url)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        print("SKIP: needs ffmpeg and ffprobe to make plexav's files")
        sys.exit(0)
    sys.exit(main())
