#!/usr/bin/env python3
"""Cycle 84 (territories N and P): the two measured-not-fixed items cycle 83 left.

1. A decision lists only the streams that PLAY. Plex Web's exact MDE request
   (`tests/fixtures/decision_streams_selected.json`, `request_query`) on zen (1.42.2) lists the
   first video, the selected audio and the selected subtitle -- `decision: ignore` with no
   location under `subtitles=none`, `location: sidecar-subs` under `subtitles=sidecar` -- and
   on plexscratch42 (1.42.2) a row whose subtitles are all unselected lists none of them, under
   every `subtitles` value. Both servers listed every stream of the file with
   `location: direct`.
2. A merge leaves the survivor's `updatedAt` alone (`tests/fixtures/merge_updated_at_s42.json`,
   plexscratch42; plexscratch 1.43.4 agrees). Both servers stamped the merge time.

Starts its own server (native by default, `REX_SERVER=python` the oracle) on
`tests/catalog_ui.json`: movie 1012 holds 32 unselected srt subtitles beside a selected eac3,
episode 1011 (part 10110) two unselected srt subtitles, movies 1008 and 1009 one Media each.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from server_command import server_command  # noqa: E402

ROOT = os.path.dirname(HERE)
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s  %s" % ("PASS" if ok else "FAIL", name, detail))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def send(port, method, path, accept="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    c.request(method, path, headers={"Accept": accept, "X-Plex-Client-Identifier": "c84np-check"})
    r = c.getresponse()
    body = r.read().decode("utf-8", "replace")
    c.close()
    return r.status, body


def container(body):
    try:
        return json.loads(body).get("MediaContainer", {})
    except ValueError:
        return {}


def decision_streams(port, query, rating_key, subtitles):
    q = [(k, ("/library/metadata/%s" % rating_key if k == "path"
              else subtitles if k == "subtitles" else v)) for k, v in query]
    st, body = send(port, "GET", "/video/:/transcode/universal/decision?" + urllib.parse.urlencode(q))
    mc = container(body)
    rows = []
    for meta in mc.get("Metadata", []):
        for media in meta.get("Media", []):
            for part in media.get("Part", []):
                rows.extend(part.get("Stream", []))
    return st, rows


def shape(rows):
    """What the fixture compares: per stream, its type, whether it is selected, and the
    decision/location it carries."""
    return [(s.get("streamType"), bool(s.get("selected")), s.get("decision"), s.get("location"))
            for s in rows]


def main():
    fixture = json.load(open(os.path.join(HERE, "fixtures", "decision_streams_selected.json")))
    query = [tuple(pair) for pair in fixture["request_query"]]
    unselected = fixture["plexscratch42"]["272"]
    # The reference's own answers, re-read so a re-measurement moves the expectation.
    assert sum(1 for s in unselected["item"] if s["streamType"] == 3) == 2
    expect_unselected = {mode: shape(answer["Stream"]) for mode, answer in unselected["decision"].items()}
    assert set(expect_unselected) == {"none", "sidecar", "burn", "auto"}
    assert all(v == [(1, False, None, "direct"), (2, True, None, "direct")]
               for v in expect_unselected.values()), expect_unselected
    zen = fixture["zen"]["2925"]
    assert sum(1 for s in zen["item"] if s["streamType"] == 3) == 31
    expect_selected = {mode: shape(zen["decision"][mode]["Stream"]) for mode in ("none", "sidecar")}
    assert expect_selected == {"none": [(1, False, None, "direct"), (2, True, None, "direct"),
                                        (3, True, "ignore", None)],
                               "sidecar": [(1, False, None, "direct"), (2, True, None, "direct"),
                                           (3, True, None, "sidecar-subs")]}, expect_selected
    many_video = fixture["zen"]["1098"]
    assert [s["streamType"] for s in many_video["item"]].count(1) == 2
    assert [s["streamType"] for s in many_video["decision"]["none"]["Stream"]] == [1, 2, 3]
    merge = json.load(open(os.path.join(HERE, "fixtures", "merge_updated_at_s42.json")))
    before = merge["reads"][0]["768"]["updatedAt"]
    assert all(read["768"]["updatedAt"] == before for read in merge["reads"]), merge["reads"]

    port = free_port()
    env = dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:9")
    env.pop("TMDB_KEY", None)
    env.pop("REX_STATE", None)
    proc = subprocess.Popen(
        server_command("--port", str(port), "--catalog", "tests/catalog_ui.json",
                       "--assert-claimed", "--gdm", "off", root=ROOT),
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if send(port, "GET", "/identity")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)

        # 1a. No subtitle selected: none is listed, whatever `subtitles` says.
        for mode, expected in sorted(expect_unselected.items()):
            for rk in ("1012", "1011"):
                st, rows = decision_streams(port, query, rk, mode)
                check("decision subtitles=%s on %s lists only the video and the selected audio" % (mode, rk),
                      st == 200 and shape(rows) == expected,
                      "status=%s streams=%s" % (st, shape(rows)))

        # 1b. A selected subtitle is listed alone, its fate set by `subtitles`.
        st, _ = send(port, "PUT", "/library/parts/10110?subtitleStreamID=1002004&allParts=1")
        check("selecting subtitle 1002004 on part 10110 answers 200", st == 200, "status=%s" % st)
        for mode, expected in sorted(expect_selected.items()):
            st, rows = decision_streams(port, query, "1011", mode)
            ids = [s.get("id") for s in rows if s.get("streamType") == 3]
            check("decision subtitles=%s lists the selected subtitle alone as zen does" % mode,
                  st == 200 and shape(rows) == expected and ids == ["1002004"],
                  "status=%s streams=%s subtitle ids=%s" % (st, shape(rows), ids))
        xq = [(k, "/library/metadata/1011" if k == "path" else "none" if k == "subtitles" else v)
              for k, v in query]
        st, xml = send(port, "GET", "/video/:/transcode/universal/decision?" + urllib.parse.urlencode(xq),
                       accept="application/xml")
        streams = [line for line in xml.split("\n") if "<Stream " in line]
        check("the XML decision carries the same three Streams, the subtitle decision=\"ignore\"",
              st == 200 and len(streams) == 3 and 'decision="ignore"' in streams[-1]
              and 'location=' not in streams[-1],
              "status=%s streams=%d last=%s" % (st, len(streams), streams[-1][-120:] if streams else None))
        send(port, "PUT", "/library/parts/10110?subtitleStreamID=0&allParts=1")

        # 2. A merge leaves the survivor's updatedAt alone.
        st, body = send(port, "GET", "/library/metadata/1008")
        survivor_before = container(body)["Metadata"][0].get("updatedAt")
        time.sleep(1.2)
        st, _ = send(port, "PUT", "/library/metadata/1008/merge?ids=1009")
        st_read, body = send(port, "GET", "/library/metadata/1008")
        row = container(body)["Metadata"][0]
        check("merge 1009 into 1008 answers 200 and moves the Media", st == 200 and len(row.get("Media", [])) == 2,
              "status=%s media=%d" % (st, len(row.get("Media", []))))
        check("the survivor keeps its updatedAt across the merge (plexscratch42: %s before and after)" % before,
              row.get("updatedAt") == survivor_before,
              "before=%s after=%s" % (survivor_before, row.get("updatedAt")))
        st, body = send(port, "GET", "/library/sections/1/all")
        listed = {m["ratingKey"]: m.get("updatedAt") for m in container(body).get("Metadata", [])}
        check("the section listing agrees", listed.get("1008") == survivor_before,
              "listed=%s" % listed.get("1008"))
        st, _ = send(port, "PUT", "/library/metadata/1008/split")
        st, body = send(port, "GET", "/library/sections/1/all")
        after_split = {m["ratingKey"]: m.get("updatedAt") for m in container(body).get("Metadata", [])
                       if m.get("title", "").startswith("Tears of Steel (fixture copy 2)")}
        check("the split leaves both rows on the survivor's updatedAt (plexscratch42: 768 and 770 alike)",
              len(after_split) == 2 and set(after_split.values()) == {survivor_before},
              "rows=%s" % after_split)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("PASS %d/%d" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
