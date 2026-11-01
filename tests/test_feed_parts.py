#!/usr/bin/env python3
"""The parts of one episode are one row, one Media of several Parts, as a real PMS stacks them.

TMDB and Plex's agent list Lost's S4E13 as one 85-minute episode where TVDB and the release have two
files, S04E13 and S04E14. A source that knows says so with `part` (`docs/feed-api.md`): both files
on S4E13, parts 1 and 2. rex had no field for it, so the second file was another row on the same
number, as files of one release without a part still are (a rex user's Lost S01E24 and its renamed
`S01E24.Part.2`, two "Exodus (2)" tiles, 2026-11-01). A real PMS makes a stacked pair one Media
with two Parts, its duration the sum (findings, "partIndex on a MULTI-PART Media"), and Plex Web
plays the Parts back to back, its timeline reporting the time across the whole Media.

`fixtures/feed/parts_lost_1101.json` is what a source pushes for both finales;
`fixtures/parts_stack_s1434.json` is a real PMS's stacked `- pt1`/`- pt2` pair and the track picks
Plex Web sends for it with `allParts=1`, which the last leg replays.

    python3 tests/test_feed_parts.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse

from test_feed import ENTRIES, MOUNT, Bytes, Server, catalog, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "feed", "parts_lost_1101.json")))
# A real PMS's stacked `- pt1`/`- pt2` pair and two `allParts=1` audio picks on it
# (`probes/record_parts_stack.py` on plexscratch 1.43.4).
STACK = json.load(open(os.path.join(HERE, "fixtures", "parts_stack_s1434.json")))["exchanges"]
PROBE = ENTRIES["episode"]["entry"]["media_info"]


def item(entry, **changes):
    """The fixture's entry as a source pushes it: its file under the mount, its probe its own length."""
    pushed = {key: value for key, value in entry.items() if key not in ("duration", "about")}
    probe = copy.deepcopy(PROBE)
    probe["format"]["duration"] = entry["duration"]
    probe["format"]["size"] = str(entry["size"])
    pushed.update(file="%s/%s" % (MOUNT, entry["path"]), media_info=probe)
    pushed.update(changes)
    return {key: value for key, value in pushed.items() if value is not None}


PART1, PART2 = (item(entry) for entry in FIXTURE["parts"])
WHOLE13 = item(FIXTURE["parts"][0], part=None)
OTHER = item(FIXTURE["other_release"])
HIQVE = [item(entry) for entry in FIXTURE["s01e24_no_part"]]
MS = [int(float(entry["duration"]) * 1000) for entry in FIXTURE["parts"]]


class Source(Bytes):
    """The byte stub, deleting a file as a source answers a WebDAV DELETE."""

    def __init__(self):
        super().__init__()
        self.deleted = []

    def handler(source):
        class Handler(Bytes.handler(source)):
            def do_DELETE(self):
                path = urllib.parse.unquote(self.path)
                source.deleted.append(path)
                source.files.discard(path)
                self.reply(204)
        return Handler


def media(row):
    return [[part.get("file") for part in m.get("Part") or []] for m in row.get("Media") or []]


def episodes(server, season_of, number):
    """Every row on the season's number."""
    season = server.row(season_of).get("parentRatingKey")
    return [row for row in server.rows("/library/metadata/%s/children" % season) if row.get("index") == number]


def read(server, source, part):
    """A part read: whether it streamed, and the path the source was asked for."""
    source.seen.clear()
    status, _ = server.request(part["key"], headers={"Range": "bytes=0-9"})
    return status == 206, [seen[1] for seen in source.seen if seen[0] == "GET"][-1:]


def decision(server, key, part_index):
    query = urllib.parse.urlencode({"hasMDE": "1", "path": "/library/metadata/%s" % key, "mediaIndex": "0",
                                    "partIndex": str(part_index), "X-Plex-Platform": "Chrome"})
    status, body = server.request("/video/:/transcode/universal/decision?" + query)
    container = (body or {}).get("MediaContainer") or {} if isinstance(body, dict) else {}
    parts = [str(part.get("id")) for row in container.get("Metadata") or [] for m in row.get("Media") or []
             for part in m.get("Part") or []]
    return status, parts, container


def start(work, port, items=()):
    catalog(work, items)
    return Server(work, port)


def main():
    source = Source()
    for pushed in (PART1, PART2, OTHER, *HIQVE):
        source.files.add("/dav/" + pushed["path"])
    port = serve(source.handler())
    files = [PART1["file"], PART2["file"]]

    # -- both parts in one push: one row, one Media of two Parts ---------------------------------
    work = tempfile.mkdtemp(prefix="rex-parts-")
    server = start(work, port)
    try:
        status, body = server.feed("POST", "items", {"items": [PART2, PART1]})
        keys = server.keys()
        key = keys.get(PART1["id"])
        check("both parts are taken, one row created", status == 200 and body.get("created") == 1 and not body.get("rejected"),
              (status, body))
        check("both ids are on one key", key and keys.get(PART2["id"]) == key, keys)
        row = server.row(key)
        check("one row on S4E13, no second Episode 13", len(episodes(server, key, 13)) == 1,
              [(r.get("ratingKey"), r.get("title")) for r in episodes(server, key, 13)])
        check("one Media holding both files as Parts, in part order", media(row) == [files], media(row))
        held = (row.get("Media") or [{}])[0]
        check("the Media's duration is the sum of its Parts', each Part keeping its own",
              held.get("duration") == sum(MS) and [p.get("duration") for p in held.get("Part") or []] == MS
              and row.get("duration") == sum(MS), (held.get("duration"), [p.get("duration") for p in held.get("Part") or []], row.get("duration")))
        check("each Part keeps its own streams and size",
              all(len(p.get("Stream") or []) == 3 for p in held.get("Part") or [])
              and [p.get("size") for p in held.get("Part") or []] == [PART1["size"], PART2["size"]],
              [(len(p.get("Stream") or []), p.get("size")) for p in held.get("Part") or []])
        for part, pushed in zip(held.get("Part") or [], (PART1, PART2)):
            ok, hit = read(server, source, part)
            check("part %d streams from its own path" % pushed["part"], ok and hit == ["/dav/" + pushed["path"]], (ok, hit))
        ids = [str(p.get("id")) for p in held.get("Part") or []]
        answers = [decision(server, key, index) for index in (0, 1, 2)]
        check("a decision's partIndex picks that Part, and one past the last is a 400",
              [a[1] for a in answers[:2]] == [ids[:1], ids[1:]] and answers[2][0] == 400,
              [(a[0], a[1]) for a in answers])
        status, _, every = decision(server, key, -1)
        check("partIndex -1 is the measured all-parts refusal", status == 200 and every.get("directPlayDecisionCode") == 3000
              and every.get("transcodeDecisionCode") == 4005 and not every.get("Metadata"), (status, every))
        status, body = server.feed("POST", "items", {"items": [PART1, PART2]})
        check("both pushed again change nothing", body.get("unchanged") == 2 and body.get("updated") == 0, body)
        server.feed("POST", "items", {"items": [dict(PART1, probe="digest-e13"), dict(PART2, probe="digest-e14")]})
        light = [dict({k: v for k, v in pushed.items() if k != "media_info"}, probe=digest)
                 for pushed, digest in ((PART1, "digest-e13"), (PART2, "digest-e14"))]
        status, body = server.feed("POST", "items", {"items": light})
        check("each Part keeps its own probe digest: a digest-only push of both needs no probe",
              status == 200 and "wants" not in body and media(server.row(key)) == [files], (status, body))

        # -- a Part leaves, and comes back --------------------------------------------------------
        status, body = server.feed("DELETE", "items", {"ids": [PART2["id"]]})
        row = server.row(key)
        check("removing part 2 leaves the row with part 1, out of the trash",
              status == 200 and body.get("trashed") == 0 and media(row) == [files[:1]] and not row.get("deletedAt")
              and server.keys() == {PART1["id"]: key}, (body, media(row), row.get("deletedAt")))
        check("and its duration is part 1's", row.get("duration") == MS[0], row.get("duration"))
        server.feed("PUT", "snapshot", {"items": [PART1, PART2]})
        row = server.row(key)
        check("a snapshot naming it again joins it as part 2 on the same row", media(row) == [files]
              and server.keys() == {PART1["id"]: key, PART2["id"]: key}, (media(row), server.keys()))

        # -- another release is another version ----------------------------------------------------
        server.feed("POST", "items", {"items": [OTHER]})
        row = server.row(key)
        check("another release's whole S4E13 is a second Media beside the parts",
              media(row) == [files, [OTHER["file"]]] and server.keys().get(OTHER["id"]) == key, media(row))
        server.feed("DELETE", "items", {"ids": [OTHER["id"]]})
    finally:
        server.stop()

    # -- a restart keeps it as it is --------------------------------------------------------------
    log = os.path.join(work, "server.log")
    folds = open(log, errors="replace").read().count("FEED folded")
    server = Server(work, port)
    try:
        time.sleep(2)
        row = server.row(key)
        check("after a restart: one Media of both Parts on the same key, nothing folded again",
              media(row) == [files] and server.keys() == {PART1["id"]: key, PART2["id"]: key}
              and open(log, errors="replace").read().count("FEED folded") == folds, (media(row), server.keys()))

        # -- deleting the episode deletes every Part at the source -------------------------------
        source.deleted.clear()
        status, _ = server.request("/library/metadata/%s" % key, "DELETE")
        check("deleting the episode deletes both parts' files at the source",
              status == 200 and sorted(source.deleted) == sorted("/dav/" + p["path"] for p in (PART1, PART2)),
              (status, source.deleted))
        check("and the row is gone", server.request("/library/metadata/%s" % key)[0] == 404, server.row(key))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    for pushed in (PART1, PART2):
        source.files.add("/dav/" + pushed["path"])

    # -- an episode already here takes its second part, its key and watch state kept ------------
    work = tempfile.mkdtemp(prefix="rex-parts-")
    server = start(work, port)
    try:
        server.feed("POST", "items", {"items": [WHOLE13]})
        key = server.keys().get(WHOLE13["id"])
        server.request("/:/scrobble?key=%s&identifier=com.plexapp.plugins.library" % key)
        server.request("/:/progress?key=%s&identifier=com.plexapp.plugins.library&time=2700000&state=stopped" % key)
        before = server.row(key)
        status, body = server.feed("POST", "items", {"items": [PART1, PART2]})
        row = server.row(key)
        check("the S04E13 row a whole-episode push made keeps its key when the parts arrive",
              server.keys() == {PART1["id"]: key, PART2["id"]: key} and len(episodes(server, key, 13)) == 1,
              (server.keys(), key))
        check("its first file is part 1, the new file part 2, of its one Media", media(row) == [files], media(row))
        check("its watch state stays", before.get("viewCount") == 1 and row.get("viewCount") == 1
              and row.get("viewOffset") == before.get("viewOffset") == 2700000,
              (before.get("viewCount"), row.get("viewCount"), before.get("viewOffset"), row.get("viewOffset")))
        # The source numbers part 2 a whole episode again: it leaves the Media for a row of its own,
        # as a file of the same release without a part is.
        server.feed("POST", "items", {"items": [item(FIXTURE["parts"][1], part=None)]})
        row = server.row(key)
        split = server.keys().get(PART2["id"])
        check("a part pushed again without `part` leaves the Media, the row keeping part 1",
              media(row) == [files[:1]] and split and split != key, (media(row), split))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    # -- part 2 first, part 1 in a later push -------------------------------------------------
    work = tempfile.mkdtemp(prefix="rex-parts-")
    server = start(work, port)
    try:
        server.feed("POST", "items", {"items": [PART2]})
        key = server.keys().get(PART2["id"])
        server.feed("POST", "items", {"items": [PART1]})
        row = server.row(key)
        check("part 1 arriving after part 2 joins its row, the Parts in part order",
              server.keys() == {PART1["id"]: key, PART2["id"]: key} and media(row) == [files], (server.keys(), media(row)))
        ok, hit = read(server, source, ((row.get("Media") or [{}])[0].get("Part") or [{}])[0])
        check("and the first Part streams part 1's path", ok and hit == ["/dav/" + PART1["path"]], (ok, hit))
        # Placed again elsewhere, both parts move: the row is re-identified on its key and the parts
        # stay one Media.
        moved = [dict(pushed, episode=12, tmdb_episode=334004) for pushed in (PART1, PART2)]
        server.feed("POST", "items", {"items": moved})
        row = server.row(key)
        check("both parts placed on S4E12 move there as one Media on the same key",
              (row.get("parentIndex"), row.get("index")) == (4, 12) and media(row) == [files]
              and server.keys() == {PART1["id"]: key, PART2["id"]: key} and not episodes(server, key, 13),
              ((row.get("parentIndex"), row.get("index")), media(row), server.keys()))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    # -- files of one release with no part stay rows of their own --------------------------------
    # docs/feed-api.md "Versions": other files of the same release on one number are no versions of
    # each other (a collection numbering several shows alike, a batch's specials). Without `part`,
    # Lost's S01E24 and its renamed second half stay two rows, as a rex user's library showed them.
    work = tempfile.mkdtemp(prefix="rex-parts-")
    server = start(work, port)
    try:
        status, body = server.feed("POST", "items", {"items": HIQVE})
        keys = server.keys()
        rows = episodes(server, keys.get(HIQVE[0]["id"]), 24)
        check("two files of one release on S1E24 without a part are two rows, as before",
              status == 200 and len(set(keys.values())) == 2 and len(rows) == 2
              and all(len(media(r)) == 1 for r in rows), (keys, [media(r) for r in rows]))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    # -- a track picked for all Parts, as the reference carries it ---------------------------------
    # Plex Web sends `allParts=1` with every pick before play. A real PMS carries it to the other
    # Parts of the Media by stream index; rex matched stream ids, which no other Part has, and so
    # took the second Part's selected track away.
    recorded = lambda at: STACK[at]["body"]["MediaContainer"]["Metadata"][0]
    audio = lambda row: [[(s.get("id"), bool(s.get("selected"))) for s in p.get("Stream") or [] if s.get("streamType") == 2]
                         for m in row.get("Media") or [] for p in m.get("Part") or []]
    work = tempfile.mkdtemp(prefix="rex-parts-")
    stacked = dict({k: v for k, v in recorded(0).items() if k in ("title", "duration", "Media")},
                   ratingKey="500", key="/library/metadata/500", guid="local://500", type="movie", librarySectionID=1)
    server = start(work, port, [stacked])
    try:
        check("the reference's stacked pair is one Media of two Parts, its duration their sum",
              len(recorded(0)["Media"]) == 1 and recorded(0)["Media"][0]["duration"]
              == sum(p["duration"] for p in recorded(0)["Media"][0]["Part"]), recorded(0)["Media"][0]["duration"])
        for at in (1, 3):
            pick = STACK[at]
            server.request("%s?%s" % (pick["path"], urllib.parse.urlencode(pick["params"])), "PUT")
            check("an allParts=1 pick of stream %s selects on each Part what the reference selected"
                  % pick["params"]["audioStreamID"], audio(server.row("500")) == audio(recorded(at + 1)),
                  (audio(server.row("500")), audio(recorded(at + 1))))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)

    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
