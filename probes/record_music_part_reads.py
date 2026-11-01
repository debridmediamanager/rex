#!/usr/bin/env python3
"""Record how plexav's PMS answers a read of each audio format's Part, every header in order.

plexav's music section holds MP3 and FLAC albums and `Rex Test/Codec Test (2026)` (Opus `.opus`,
Vorbis `.ogg`, ALAC `.m4a`, PCM `.wav` and a FLAC ramp). For the first track of each file extension
this reads `/library/parts/{id}/{ts}/file.{ext}` the ways clients do -- whole, `Range: bytes=0-99`,
an open `bytes=100-`, a HEAD, and Plexamp's `download=1` whole and ranged -- and keeps the status,
every response header name in order with its value (`Date`'s left out) and the body's length, with
the row's `addedAt` and `updatedAt`.
Read-only: nothing is written to the PMS.

    python3 probes/record_music_part_reads.py --base http://127.0.0.1:32499 --token-command "ssh <lab host> './token.sh'"
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "tests", "fixtures", "music", "part-reads.json")
READS = [
    ("GET", "", None), ("GET", "", "bytes=0-99"), ("GET", "", "bytes=100-"), ("HEAD", "", None),
    ("GET", "download=1", None), ("GET", "download=1", "bytes=0-99"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--token-command", required=True)
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args()
    token = subprocess.run(args.token_command, shell=True, capture_output=True, text=True).stdout.strip()
    if not token:
        sys.exit("no token from --token-command")
    base = urllib.parse.urlparse(args.base)

    def request(method, path, headers):
        connection = http.client.HTTPConnection(base.hostname, base.port, timeout=60)
        connection.request(method, path, headers=dict({"X-Plex-Token": token}, **headers))
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, response.getheaders(), body

    _, _, body = request("GET", "/library/sections/1/all?type=10", {"Accept": "application/json"})
    seen, out = set(), {"recorded": "plexav PMS 1.43.4, 2026-11-01", "parts": []}
    for row in json.loads(body)["MediaContainer"]["Metadata"]:
        media = row["Media"][0]
        part = media["Part"][0]
        extension = part["file"].rsplit(".", 1)[-1].lower()
        if extension in seen:
            continue
        seen.add(extension)
        # The row's stamps: `Last-Modified` is its `updatedAt`, and a row with none (one Plex's agent
        # never matched) answers no `Last-Modified`.
        entry = {"file": part["file"].rsplit("/", 1)[-1], "container": media.get("container"),
                 "audioCodec": media.get("audioCodec"), "size": part["size"], "guid": row.get("guid"),
                 "addedAt": row.get("addedAt"), "updatedAt": row.get("updatedAt"), "reads": []}
        for method, query, spec in READS:
            status, headers, data = request(method, part["key"] + ("?" + query if query else ""),
                                            {"Range": spec} if spec else {})
            entry["reads"].append({"method": method, "query": query, "range": spec, "status": status,
                                   "headers": [[k, "" if k == "Date" else v] for k, v in headers],
                                   "length": len(data)})
        out["parts"].append(entry)
        print("%s: %s" % (entry["file"], [(r["status"], dict(r["headers"]).get("Content-Type")) for r in entry["reads"]]))
    # A video Part too, ranged only (plexav's one episode is a 433 MB recording): its `Last-Modified` is its row's
    # `updatedAt` as well, which is not its `addedAt`.
    _, _, body = request("GET", "/library/sections/2/all?type=4", {"Accept": "application/json"})
    for row in json.loads(body)["MediaContainer"].get("Metadata", [])[:1]:
        part = row["Media"][0]["Part"][0]
        status, headers, data = request("GET", part["key"], {"Range": "bytes=0-99"})
        out["video"] = {"file": part["file"].rsplit("/", 1)[-1], "addedAt": row.get("addedAt"),
                        "updatedAt": row.get("updatedAt"), "status": status,
                        "headers": [[k, "" if k == "Date" else v] for k, v in headers]}
    json.dump(out, open(args.out, "w"), indent=1, ensure_ascii=False)
    open(args.out, "a").write("\n")


if __name__ == "__main__":
    sys.exit(main())
