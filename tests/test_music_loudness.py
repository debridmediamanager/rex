#!/usr/bin/env python3
"""A track's loudness a source pushes (`loudness`), answered as plexav's PMS answers its own analysis.

`fixtures/music/loudness.json` (`probes/record_music_loudness.py`) is plexav's analysed tracks: each
audio Stream's loudness fields, `/library/streams/{id}/levels` at every `subsample` a client may send,
and Plexamp's recorded play-queue request with and without `includeLoudnessRamps=1`. rex reads no
audio: three of `music_rig.py`'s tracks are pushed again with plexav's numbers for three of its own
(the ramp, an Opus and a PCM tone), and must show and serve them as plexav did. plexav works out a
smaller `subsample` from the audio itself; rex averages the pushed levels, which is checked against
plexav's own answers to within 0.2 dB at 64, Plexamp's 128 and 199 (0.8 at 10).

    python3 tests/test_music_loudness.py
"""
import json
import os
import shutil
import sys
import urllib.parse

import music_rig as M
from music_rig import check, results

RECORDED = json.load(open(os.path.join(M.FIXTURES, "loudness.json")))
FIELDS = ("albumGain", "albumPeak", "albumRange", "gain", "loudness", "lra", "peak")


def ramps(title):
    queue = next(q for q in RECORDED["queues"] if ("includeLoudnessRamps", "1") in [tuple(p) for p in q["query"]])
    row = next(m for m in queue["body"]["MediaContainer"]["Metadata"] if m["title"] == title)
    stream = row["Media"][0]["Part"][0]["Stream"][0]
    return {field: stream[field] for field in ("startRamp", "endRamp")}


def main():
    work, items, rex, handler, asked = M.start()
    try:
        status, body = rex.push(items)
        assert status == 200 and body.get("created") == 46, body
        chosen = [t for t in RECORDED["tracks"] if t["title"] in ("Ramp Tone", "Opus Tone", "WAV Tone")]
        album = [item for item in items if item["path"].startswith("music/Daft Punk/")][:len(chosen)]
        pushed = []
        for item, track in zip(album, chosen):
            full = next(level for level in track["levels"] if level["subsample"] is None)["body"]["MediaContainer"]["Level"]
            loudness = {field: track["stream"][field] for field in FIELDS}
            loudness.update(ramps(track["title"]))
            loudness["levels"] = [level["v"] for level in full]
            pushed.append(dict(item, loudness=loudness))
        status, body = rex.push(pushed)
        check("a push naming three tracks' loudness updates them", status == 200 and body.get("updated") == 3, body)
        keys = {row["id"]: row["ratingKey"] for row in rex.request("/rex/feed/plexav/items")[1]["items"]}
        for item, track in zip(pushed, chosen):
            stream = rex.rows("/library/metadata/%s" % keys[item["id"]])[0]["Media"][0]["Part"][0]["Stream"][0]
            check("%s: its stream shows plexav's loudness fields for %s" % (item["path"].rsplit("/", 1)[1], track["title"]),
                  {field: stream.get(field) for field in FIELDS} == {field: track["stream"][field] for field in FIELDS}
                  and not {"startRamp", "endRamp"} & set(stream), stream)
            for level in track["levels"]:
                query = "?subsample=%s" % level["subsample"] if level["subsample"] is not None else ""
                status, got = rex.request("/library/streams/%s/levels%s" % (stream["id"], query))
                theirs = level["body"]["MediaContainer"]
                mine = got.get("MediaContainer", {}) if isinstance(got, dict) else {}
                values = [v["v"] for v in mine.get("Level", [])]
                expected = [v["v"] for v in theirs.get("Level", [])]
                close = 0.8 if level["subsample"] == "10" else 0.2
                check("  levels, subsample %s: plexav's %d of %s" % (level["subsample"], theirs["size"], theirs["totalSamples"]),
                      status == 200 and mine.get("size") == theirs["size"] and mine.get("totalSamples") == theirs["totalSamples"]
                      and len(values) == len(expected) and all(abs(a - b) <= close + 1e-9 for a, b in zip(values, expected)),
                      (status, mine.get("size"), mine.get("totalSamples"), max([abs(a - b) for a, b in zip(values, expected)] or [0])))
        other = rex.rows("/library/metadata/%s" % keys[items[0]["id"]])[0]["Media"][0]["Part"][0]["Stream"][0]
        status, _ = rex.request("/library/streams/%s/levels?subsample=128" % other["id"])
        check("a track pushed with no loudness answers plexav's 404 before its analysis", status == 404, status)
        # Plexamp's own queue request for the album, with and without its loudness ramps.
        record = next(r for r in M.load("plexamp-play") if r["method"] == "POST" and r["path"] == "/playQueues")
        album_key = rex.rows("/library/metadata/%s" % keys[pushed[0]["id"]])[0]["parentRatingKey"]
        mapping = {"31": album_key}
        for with_ramps in (True, False):
            query = [(k, M.rewrite(v, mapping)) for k, v in record["query"] if "token" not in k.lower() and (with_ramps or k != "includeLoudnessRamps")]
            headers = {k: v for k, v in record["req_headers"] if k.startswith("X-Plex") and "token" not in k.lower()}
            status, queue = rex.request("/playQueues?" + urllib.parse.urlencode(query), "POST", headers=headers)
            rows = {m["title"]: m["Media"][0]["Part"][0]["Stream"][0] for m in queue["MediaContainer"]["Metadata"]}
            shown = [{field: rows[t].get(field) for field in ("startRamp", "endRamp")} for t in rows if t in [r["title"] for r in rex.rows("/library/metadata/%s/children" % album_key)][:3]]
            wanted = [ramps(track["title"]) if with_ramps else {"startRamp": None, "endRamp": None} for track in chosen]
            check("Plexamp's queue %s includeLoudnessRamps=1 %s each track's ramps" % ("with" if with_ramps else "without", "carries" if with_ramps else "leaves out"),
                  status == 200 and shown == wanted, (status, shown, wanted))
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
