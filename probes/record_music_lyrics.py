#!/usr/bin/env python3
"""Record how plexav's PMS shows and serves a track's sidecar lyrics.

On 2026-10-31 two files written for this sat beside plexav's `Rex Test/Codec Test (2026)` tracks:
`01 - Opus Tone.lrc` (timed, with `[ti:]` and `[ar:]` tags) and `02 - Vorbis Tone.txt` (plain);
their text is synthetic and kept here whole. The music section was refreshed, and this asks each
track's detail (its Part's Streams) and each lyrics stream under the Accept headers and `format=`
values clients send: none, `application/json`, Plex Web's `text/plain, */*; q=0.01`, each with no
`format`, `format=xml` and `format=json`.

    python3 probes/record_music_lyrics.py --base http://127.0.0.1:32499 --token-command "ssh <lab host> './token.sh'"
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
OUT = os.path.join(os.path.dirname(HERE), "tests", "fixtures", "music", "lyrics.json")
TEXTS = {
    "01 - Opus Tone.lrc": "[ti:Opus Tone]\n[ar:Rex Test]\n[00:00.00]First synthetic line for rex\n"
                          "[00:05.50]Second synthetic line for rex\n[00:12.25]Third synthetic line for rex\n",
    "02 - Vorbis Tone.txt": "First plain synthetic line for rex\nSecond plain synthetic line for rex\n",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--token-command", required=True)
    args = parser.parse_args()
    token = subprocess.run(args.token_command, shell=True, capture_output=True, text=True).stdout.strip()
    if not token:
        sys.exit("no token from --token-command")

    def get(path, query=(), accept=None):
        headers = {"X-Plex-Token": token}
        if accept:
            headers["Accept"] = accept
        url = args.base + path + ("?" + urllib.parse.urlencode(list(query)) if query else "")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                return response.status, response.headers.get("Content-Type"), response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type"), error.read().decode()

    _, _, body = get("/library/sections/1/all", [("type", "10")], "application/json")
    rows = [row for row in json.loads(body)["MediaContainer"]["Metadata"] if row.get("grandparentTitle") == "Rex Test"]
    out = {"recorded": "plexav PMS 1.43.4, 2026-10-31", "texts": TEXTS, "tracks": []}
    for row in rows:
        _, _, body = get("/library/metadata/%s" % row["ratingKey"], (), "application/json")
        part = json.loads(body)["MediaContainer"]["Metadata"][0]["Media"][0]["Part"][0]
        lyrics = [stream for stream in part["Stream"] if stream.get("streamType") == 4]
        if not lyrics:
            continue
        entry = {"title": row["title"], "file": part["file"], "streams": part["Stream"], "answers": []}
        for accept in (None, "application/json", "text/plain, */*; q=0.01"):
            for query in ([], [("format", "xml")], [("format", "json")]):
                status, content_type, body = get(lyrics[0]["key"], query, accept)
                entry["answers"].append({"accept": accept, "query": query, "status": status,
                                         "contentType": content_type, "body": body})
        out["tracks"].append(entry)
    json.dump(out, open(OUT, "w"), indent=1, ensure_ascii=False)
    print("%d tracks with lyrics -> %s" % (len(out["tracks"]), OUT))


if __name__ == "__main__":
    sys.exit(main())
