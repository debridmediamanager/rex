#!/usr/bin/env python3
"""Record what a real PMS makes of an episode split into two files (`- pt1`/`- pt2`).

Run against plexscratch, the disposable write copy, with a probe library under its writable
`/config` holding `Lost (2004)/Season 04/Lost - s04e13 - pt1.mkv` and `- pt2.mkv` (each: h264,
two AAC tracks, one SRT `eng`; pt1 10 s at about 2 Mbps with `eng` then `fre`, pt2 12 s at about
300 kbps with `fre` then `eng`, so a pick carried to the other Part shows whether it goes by index
or by language, and the Media's `bitrate` whether it is the first Part's, the last's or the most):

    PLEX_URL=http://<lab ip>:32410 PLEX_TOKEN=$(./scratch.sh token) \\
        python3 probes/record_parts_stack.py tests/fixtures/parts_stack_s1434.json

It makes the library, scans it, records the episode's Media and Parts, two audio picks with
`allParts=1` on part 1 as Plex Web sends them before play, then a subtitle and an audio pick on
part 1 without it, as its player sends them while part 1 plays, and deletes the library again. No
token is written.
The decision's `partIndex` grammar on a stacked pair is cycle 37's (findings, "partIndex on a
MULTI-PART Media").
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

URL, TOKEN = os.environ["PLEX_URL"], os.environ["PLEX_TOKEN"]
HEADERS = {"Accept": "application/json", "X-Plex-Token": TOKEN, "X-Plex-Client-Identifier": "rex-parts-probe",
           "X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome"}
exchanges = []


def ask(method, path, params=None, record=True):
    query = "?" + urllib.parse.urlencode(params) if params else ""
    request = urllib.request.Request(URL + path + query, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = raw.decode("utf-8", "replace")[:400]
    if record:
        exchanges.append({"method": method, "path": path, "params": params or {}, "status": status, "body": body})
    return status, body


def episode():
    status, body = ask("GET", "/library/sections/%s/all" % section, {"type": 4}, record=False)
    rows = (body or {}).get("MediaContainer", {}).get("Metadata", [])
    return rows[0]["ratingKey"] if rows else None


def detail(key):
    return ask("GET", "/library/metadata/%s" % key)[1]["MediaContainer"]["Metadata"][0]


status, body = ask("POST", "/library/sections", {
    "name": "Parts Probe", "type": "show", "agent": "tv.plex.agents.none", "scanner": "Plex TV Series",
    "language": "xn", "location": "/config/parts-probe"}, record=False)
section = body["MediaContainer"]["Directory"][0]["key"]
try:
    ask("GET", "/library/sections/%s/refresh" % section, record=False)
    for _ in range(60):
        key = episode()
        if key and len(detail(key).get("Media", [{}])[0].get("Part", [])) == 2 and all(
                part.get("Stream") for part in detail(key)["Media"][0]["Part"]):
            break
        time.sleep(2)
    exchanges.clear()
    row = detail(key)
    parts = row["Media"][0]["Part"]
    # Each pick, French then English, carried to the other Part: by index or by language.
    for language in ("fra", "eng"):
        stream = next(s["id"] for s in parts[0]["Stream"] if s.get("streamType") == 2 and s.get("languageCode") == language)
        ask("PUT", "/library/parts/%s" % parts[0]["id"], {"audioStreamID": stream, "allParts": 1})
        detail(key)
    # Plex Web's player sends `allParts` only while a later Part plays (`partIndex > 0`).
    french = next(s["id"] for s in parts[0]["Stream"] if s.get("streamType") == 2 and s.get("languageCode") == "fra")
    for pick in ({"subtitleStreamID": 0}, {"audioStreamID": french}):
        ask("PUT", "/library/parts/%s" % parts[0]["id"], pick)
        detail(key)
finally:
    ask("DELETE", "/library/sections/%s" % section, record=False)

json.dump({"about": __doc__.strip().splitlines()[0], "exchanges": exchanges},
          open(sys.argv[1], "w"), indent=1, sort_keys=True)
open(sys.argv[1], "a").write("\n")
print("recorded %d exchanges" % len(exchanges))
