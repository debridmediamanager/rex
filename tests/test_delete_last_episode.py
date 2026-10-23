#!/usr/bin/env python3
"""Deleting a show's last episode removes its season and the show, as a real PMS does.

`fixtures/delete_last_episode_s43.json` is plexscratch 1.43.4: `DELETE /library/metadata/{episode}`
of a show's only episode makes the show and its season 404s at once, drops the show from the
library's listing and sends three `deleted` frames -- the show's, the season's naming the show, the
episode's under its own title naming both; deleting one of two sends the episode's alone and the
show stays. rex removed the season and kept the show, a title of no episodes in the library (the
production server's Manny_Pacquiao_2, Pokémon, Girl-Boy, Yu-Gi-Oh!, 2026-10-23). The rows here are
the production server's own: a show holding one episode and one holding four.

    python3 tests/test_delete_last_episode.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

from test_feed import LEGACY, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
MEASURED = json.load(open(os.path.join(HERE, "fixtures", "delete_last_episode_s43.json")))
ROWS = json.load(open(os.path.join(HERE, "fixtures", "feed", "delete_last_episode_rows_1023.json")))["rows"]
LONE_SHOW, LONE_SEASON, LONE_EPISODE = "170712", "170713", "170711"
PAIR_SHOW, PAIR_SEASON, PAIR_EPISODE = "172588", "172589", "172584"


def frames_since(server, mark):
    time.sleep(0.7)
    out = []
    for name, frame in server.events.frames[mark:]:
        if name == "timeline":
            entry = frame.get("TimelineEntry")
            out.extend(row for row in (entry if isinstance(entry, list) else [entry]) if row.get("state") == 9)
    return out


def shape(frame):
    return (frame.get("type"), frame.get("state"), frame.get("metadataState"), sorted(key for key in frame if key not in ("updatedAt",)))


def main():
    lone, pair = MEASURED["cases"]
    work = tempfile.mkdtemp(prefix="rex-delete-last-")
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000ff", "friendlyName": "rex-delete-last",
               "sections": LEGACY["sections"], "items": ROWS}, open(os.path.join(work, "catalog.json"), "w"))
    server = Server(work, serve(Bytes().handler()))
    try:
        mark = server.events.mark()
        status, _ = server.request("/library/metadata/%s" % LONE_EPISODE, "DELETE")
        check("the show's last episode is deleted", status == 200, status)
        gone = {key: server.request("/library/metadata/%s" % key)[0] for key in (LONE_SHOW, LONE_SEASON, LONE_EPISODE)}
        check("its season and its show are 404s at once, as on the reference",
              list(gone.values()) == [lone["GET after"][name] for name in ("show", "season", "episode")], gone)
        listed = [row.get("ratingKey") for row in server.rows("/library/sections/%s/all" % next(
            section["key"] for section in LEGACY["sections"] if section["type"] == "show"))]
        check("the show leaves the library's listing", LONE_SHOW not in listed and PAIR_SHOW in listed, listed)
        frames = frames_since(server, mark)
        check("three frames: the show's, the season's, the episode's, as the reference sends them",
              [shape(frame) for frame in frames] == [shape(frame) for frame in lone["timeline"]],
              ([shape(frame) for frame in frames], [shape(frame) for frame in lone["timeline"]]))
        by_type = {frame.get("type"): frame for frame in frames}
        show = next(row for row in ROWS if row["ratingKey"] == LONE_SHOW)
        check("the season's frame names the show, the episode's both, under the episode's own title",
              by_type.get(3, {}).get("parentItemID") == LONE_SHOW
              and (by_type.get(4, {}).get("parentItemID"), by_type.get(4, {}).get("rootItemID")) == (LONE_SEASON, LONE_SHOW)
              and by_type.get(4, {}).get("title") == "Episode 1"
              and by_type.get(3, {}).get("title") == "%s S01" % show["title"],
              [(frame.get("type"), frame.get("title"), frame.get("parentItemID"), frame.get("rootItemID")) for frame in frames])

        mark = server.events.mark()
        status, _ = server.request("/library/metadata/%s" % PAIR_EPISODE, "DELETE")
        check("one of a show's episodes is deleted", status == 200, status)
        left = {key: server.request("/library/metadata/%s" % key)[0] for key in (PAIR_SHOW, PAIR_SEASON, PAIR_EPISODE)}
        check("its season and show stay", list(left.values()) == [pair["GET after"][name] for name in ("show", "season", "episode")], left)
        check("the show counts the episodes left", server.row(PAIR_SHOW).get("leafCount") == 3, server.row(PAIR_SHOW).get("leafCount"))
        frames = frames_since(server, mark)
        check("one frame, the episode's, naming its season and show",
              [shape(frame) for frame in frames] == [shape(frame) for frame in pair["timeline"]]
              and (frames[0].get("parentItemID"), frames[0].get("rootItemID")) == (PAIR_SEASON, PAIR_SHOW),
              [(shape(frame), frame.get("parentItemID"), frame.get("rootItemID")) for frame in frames])
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
