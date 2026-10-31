#!/usr/bin/env python3
"""Record what plexav's PMS answers for a track's conversion, from its clients' own requests.

Plex Web's and Plex for Android's recorded music decisions (`tests/fixtures/music/web-music-play.jsonl`,
`droid-music-play.jsonl`) are replayed at plexav with the client's own `directPlay=0` -- what each
sends when its music quality is set below original -- and then the start, playlists and the first
segments the answer names, the way the client follows them. Also replayed as recorded (direct play
on) for the extra codecs added to plexav for this (`Rex Test/Codec Test (2026)`: Opus, Vorbis, ALAC,
PCM) and with lower `musicBitrate` values, which is how the codec and bitrate rules were found.
Bodies of media are kept by length and first bytes only.

    python3 probes/record_music_conversion.py --base http://127.0.0.1:32499 --token-command "ssh <lab host> './token.sh'"
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

KEEP = ("accept", "x-plex-product", "x-plex-client-identifier", "x-plex-platform", "x-plex-version", "x-plex-device",
        "x-plex-device-name", "x-plex-language", "x-plex-client-profile-extra", "x-plex-client-profile-name")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--token-command", required=True)
    parser.add_argument("--out", default=os.path.join(M.FIXTURES, "conversion.json"))
    args = parser.parse_args()
    token = subprocess.run(args.token_command, shell=True, capture_output=True, text=True).stdout.strip()
    if not token:
        sys.exit("no token from --token-command")

    def send(record, path, query, accept=None):
        query = [(k, v) for k, v in query if "token" not in k.lower()]
        headers = {k: v for k, v in record.get("req_headers") or [] if k.lower() in KEEP}
        if accept:
            headers["Accept"] = accept
        headers["X-Plex-Token"] = token
        request = urllib.request.Request(args.base + path + "?" + urllib.parse.urlencode(query), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def kept(status, headers, body, text=True):
        entry = {"status": status, "headers": {k: v for k, v in headers.items() if k.lower() in ("content-type", "cache-control")}}
        if text:
            entry["body"] = body.decode("utf-8", "replace").replace(token, "REDACTED")
        else:
            entry["length"] = len(body)
            entry["head"] = body[:8].hex()
        return entry

    out = {"recorded": "plexav PMS 1.43.4, 2026-10-31", "exchanges": []}
    for client, accept, session in (("web-music-play", None, "rexprobe-web"), ("droid-music-play", "application/json", "rexprobe-droid")):
        record = next(r for r in M.load(client) if r["path"] == "/music/:/transcode/universal/decision")
        query = [(k, "0" if k == "directPlay" else session if k == "session" else v) for k, v in record["query"]]
        protocol = dict(query)["protocol"]
        entry = {"client": client, "path": record["path"], "query": query}
        entry.update(kept(*send(record, record["path"], query, accept)))
        out["exchanges"].append(entry)
        start = "/music/:/transcode/universal/start." + ("mpd" if protocol == "dash" else "m3u8")
        entry = {"client": client, "path": start, "query": query}
        entry.update(kept(*send(record, start, query)))
        out["exchanges"].append(entry)
        xplex = [(k, v) for k, v in record["query"] if k.startswith("X-Plex")]
        base = "/music/:/transcode/universal/session/%s/" % session
        follow = ["0/header", "0/0.m4s", "0/1.m4s"] if protocol == "dash" else ["base/index.m3u8", "base/header", "base/00000.ts"]
        for leaf in follow:
            status, headers, body = send(record, base + leaf, xplex)
            entry = {"client": client, "path": base + leaf, "query": xplex}
            entry.update(kept(status, headers, body, text=leaf.endswith(".m3u8")))
            out["exchanges"].append(entry)
        send(record, "/music/:/transcode/universal/stop", [("session", session)] + xplex)
    # The rules: every codec direct plays as recorded; musicBitrate under directPlay=0 sets the bitrate.
    for client in ("web-music-play", "droid-music-play"):
        record = next(r for r in M.load(client) if r["path"] == "/music/:/transcode/universal/decision")
        for key, title in (("32", "15 Step"), ("68", "Opus Tone"), ("69", "Vorbis Tone"), ("70", "ALAC Tone"), ("71", "WAV Tone")):
            for direct, rate in (("1", None), ("0", "320"), ("0", "192"), ("0", "128"), ("0", "64")):
                query = [(k, "/library/metadata/" + key if k == "path" else direct if k == "directPlay"
                          else (rate or v) if k == "musicBitrate" else v) for k, v in record["query"]]
                status, headers, body = send(record, record["path"], query, "application/json")
                if status != 200:
                    out.setdefault("rules", []).append({"client": client, "title": title, "directPlay": direct, "status": status})
                    continue
                container = json.loads(body)["MediaContainer"]
                media = container["Metadata"][0]["Media"][0]
                out.setdefault("rules", []).append({
                    "client": client, "title": title, "directPlay": direct, "musicBitrate": dict(query)["musicBitrate"],
                    "general": container.get("generalDecisionCode") or container.get("mdeDecisionCode"),
                    "codec": media.get("audioCodec"), "container": media.get("container"), "bitrate": media.get("bitrate"),
                    "source": {"samplingRate": next((s.get("samplingRate") for s in media["Part"][0]["Stream"]), None)}})
    json.dump(out, open(args.out, "w"), indent=1)
    print("%d exchanges, %d rules -> %s" % (len(out["exchanges"]), len(out["rules"]), args.out))


if __name__ == "__main__":
    sys.exit(main())
