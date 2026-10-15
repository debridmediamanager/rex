#!/usr/bin/env python3
"""Record how a real PMS gates a conversion session's own requests sent WITHOUT a token,
from a source its token gate refuses (`tests/fixtures/transcode/session_gate_*.json`).

Plex Web asks for `start.m3u8` with its token, then for the playlist and segments that
playlist names relative to it, and a relative URL loses the query the token rode in.

    python3 probes/record_session_gate.py --base URL --label NAME --out FILE.json
    PLEX_TOKEN=... python3 probes/record_session_gate.py --base URL --key RATINGKEY ...

Without a token it only asks what needs none -- the control (`/library/sections`) and a
session that does not exist -- which is safe against a production server. With one and a
remuxable movie's `--key` it starts one HLS and one DASH conversion (the fixtures'
recorded Plex Web requests, remuxed: `directStream=1`), asks for their playlist,
segments, headers, ping and stop without a token and with a wrong one, and stops both.
The token is written as `<TOKEN>`, the wrong one as `<WRONG>`.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "..", "tests", "fixtures", "transcode")
UNIVERSAL = "/video/:/transcode/universal"
WRONG = "WRONG-TOKEN-0000000000"


def recorded_query(fixture, key, session):
    pairs = []
    for name, value in json.load(open(os.path.join(FIXTURES, fixture)))["exchanges"][0]["query"]:
        if name == "X-Plex-Token":
            continue
        value = {"path": "/library/metadata/%s" % key, "session": session,
                 "X-Plex-Session-Identifier": "playback-" + session}.get(name, value)
        pairs.append((name, value))
    return pairs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--key")
    args = parser.parse_args()
    token = os.environ.get("PLEX_TOKEN", "")
    exchanges = []

    def ask(path, query=(), sent="none", note="", method="GET"):
        pairs = list(query)
        if sent == "valid":
            pairs.append(("X-Plex-Token", token))
        elif sent == "wrong":
            pairs.append(("X-Plex-Token", WRONG))
        url = args.base + path + ("?" + urllib.parse.urlencode(pairs) if pairs else "")
        request = urllib.request.Request(url, method=method, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, headers, body = response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            status, headers, body = error.code, error.headers, error.read()
        shown = [(k, {"valid": "<TOKEN>", "wrong": "<WRONG>"}[sent] if k == "X-Plex-Token" else v)
                 for k, v in pairs]
        exchanges.append({"method": method, "path": path, "query": shown, "token": sent,
                          "note": note, "status": status,
                          "content_type": headers.get("Content-Type"), "res_len": len(body)})
        print("  %3d  %-6s %-4s %s  %s" % (status, sent, method, path, note))
        return status, body

    ask("/library/sections", note="control: the gate refuses this source")
    ask(UNIVERSAL + "/session/nosuchsession0001/base/index.m3u8", note="HEAD", method="HEAD")
    for path in ("/session/nosuchsession0001/base/index.m3u8", "/session/nosuchsession0001/base/00000.ts",
                 "/session/nosuchsession0001/0/header", "/session/nosuchsession0001/0/0.m4s"):
        ask(UNIVERSAL + path, note="a session that does not exist")
    if token and args.key:
        ask("/library/sections", sent="valid", note="control: the token opens it")
        hls, dash = "gateprobe-hls-%s" % args.label, "gateprobe-dash-%s" % args.label
        query = recorded_query("scratch_copy_mkv.json", args.key, hls)
        ask(UNIVERSAL + "/decision", query, "valid")
        ask(UNIVERSAL + "/start.m3u8", query, note="start without a token")
        ask(UNIVERSAL + "/start.m3u8", query, "valid", note="start with the token")
        base = "%s/session/%s/base/" % (UNIVERSAL, hls)
        incomplete = [("X-Plex-Incomplete-Segments", "1")]
        ask(base + "index.m3u8", incomplete, note="the playlist start.m3u8 names, as Plex Web asks for it")
        ask(base + "00000.ts", note="a segment")
        ask(base + "index.m3u8", incomplete, "wrong", note="the playlist with a wrong token")
        ask(base + "00001.ts", (), "wrong", note="a segment with a wrong token")
        ask(base + "index.m3u8", incomplete, note="HEAD", method="HEAD")
        for method in ("POST", "PUT", "DELETE"):
            ask(base + "index.m3u8", incomplete, note="another verb", method=method)
        ask(UNIVERSAL + "/ping", [("session", hls)], note="ping a live session")
        ask("/transcode/sessions", note="the session listing")
        query = recorded_query("scratch_dash_copy.json", args.key, dash)
        ask(UNIVERSAL + "/decision", query, "valid")
        ask(UNIVERSAL + "/start.mpd", query, "valid", note="start with the token")
        base = "%s/session/%s/" % (UNIVERSAL, dash)
        for path in ("0/header", "0/0.m4s", "1/header", "1/0.m4s"):
            ask(base + path, note="DASH")
        ask(UNIVERSAL + "/stop", [("session", hls)], note="stop without a token")
        ask(UNIVERSAL + "/ping", [("session", hls)], "valid", note="did the tokenless stop stop it?")
        ask(UNIVERSAL + "/stop", [("session", hls)], "valid")
        ask("%s/session/%s/base/00002.ts" % (UNIVERSAL, hls), note="a segment of a stopped session")
        ask(UNIVERSAL + "/stop", [("session", dash)], "valid")
    identity = urllib.request.urlopen(args.base + "/identity", timeout=10).read().decode()
    container = identity.split("<MediaContainer", 1)[-1].split(">", 1)[0]
    reference = " ".join(part for part in container.split() if part.startswith(("claimed=", "version=")))
    json.dump({"label": args.label, "reference": reference, "exchanges": exchanges},
              open(args.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
