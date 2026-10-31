#!/usr/bin/env python3
"""Record what plexav's PMS answers about a track's loudness once its analysis has run.

plexav analysed the loudness of the files added to it on 2026-10-31 (`Rex Test/Codec Test (2026)`:
four 20 s tones and `05 - Ramp Tone.flac`, a tone whose level rises from 2% to full over 20 s) as
it scanned them; its original 46 tracks have none (`/levels` is a 404 for them). For each analysed
track this keeps its audio Stream as a detail read names it, `/library/streams/{id}/levels` at the
`subsample` values a client may send (Plexamp sends 128), and Plexamp's own recorded play-queue
request (`fixtures/music/plexamp-play.jsonl`) for that album with and without its
`includeLoudnessRamps=1`.

    python3 probes/record_music_loudness.py --base http://127.0.0.1:32499 --token-command "ssh <lab host> './token.sh'"
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
import music_rig as M  # noqa: E402

SUBSAMPLES = [None, "0", "1", "10", "64", "128", "199", "200", "300"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--token-command", required=True)
    args = parser.parse_args()
    token = subprocess.run(args.token_command, shell=True, capture_output=True, text=True).stdout.strip()
    if not token:
        sys.exit("no token from --token-command")

    def call(path, query=(), method="GET", headers=()):
        url = args.base + path + ("?" + urllib.parse.urlencode(list(query)) if query else "")
        sent = dict(headers)
        sent.update({"X-Plex-Token": token, "Accept": "application/json"})
        request = urllib.request.Request(url, method=method, headers=sent)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    _, body = call("/library/sections/1/all", [("type", "10")])
    rows = [row for row in json.loads(body)["MediaContainer"]["Metadata"] if row.get("grandparentTitle") == "Rex Test"]
    out = {"recorded": "plexav PMS 1.43.4, 2026-10-31", "tracks": []}
    for row in rows:
        _, body = call("/library/metadata/%s" % row["ratingKey"])
        stream = next(s for s in json.loads(body)["MediaContainer"]["Metadata"][0]["Media"][0]["Part"][0]["Stream"] if s["streamType"] == 2)
        levels = []
        for subsample in SUBSAMPLES:
            status, body = call("/library/streams/%s/levels" % stream["id"], [("subsample", subsample)] if subsample is not None else [])
            levels.append({"subsample": subsample, "status": status, "body": json.loads(body) if status == 200 else None})
        out["tracks"].append({"title": row["title"], "album": row["parentRatingKey"], "stream": stream, "levels": levels})
    record = next(r for r in M.load("plexamp-play") if r["method"] == "POST" and r["path"] == "/playQueues")
    album = out["tracks"][0]["album"]
    query = [(k, v.replace("/library/metadata/31/children", "/library/metadata/%s/children" % album) if k == "uri" else v)
             for k, v in record["query"] if "token" not in k.lower()]
    for ramps in (True, False):
        asked = [(k, v) for k, v in query if ramps or k != "includeLoudnessRamps"]
        # Plexamp names itself in headers, which a queue needs (a 400 without its client id).
        status, body = call("/playQueues", asked, "POST", [(k, v) for k, v in record["req_headers"]
                                                           if k.startswith("X-Plex") and "token" not in k.lower()])
        if status != 200:
            sys.exit("playQueues answered %s: %s" % (status, body[:200]))
        out.setdefault("queues", []).append({"query": asked, "status": status, "body": json.loads(body)})
    json.dump(out, open(os.path.join(M.FIXTURES, "loudness.json"), "w"), indent=1)
    print("%d analysed tracks -> %s" % (len(out["tracks"]), os.path.join(M.FIXTURES, "loudness.json")))


if __name__ == "__main__":
    sys.exit(main())
