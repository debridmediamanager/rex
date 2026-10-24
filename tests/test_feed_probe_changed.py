#!/usr/bin/env python3
"""A source's new probe of a changed file replaces the row's streams.

A Stremio addon's live channel comes back from another mirror with another stream layout
(rex-stremio re-probes it then, 2026-10-24), and the conversion maps tracks by the probe's
indexes: rex kept the first probe's streams, so the channel failed every play after its first
mirror was gone. `fixtures/feed/live_mirror_probes_1024.json` holds two real layouts.

    python3 tests/test_feed_probe_changed.py   # test_feed_display.py runs it too, as CI does
"""
import json
import os
import sys
import tempfile

from test_feed import MOUNT, Bytes, Server, catalog, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
PROBES = json.load(open(os.path.join(HERE, "fixtures", "feed", "live_mirror_probes_1024.json")))


def item(probe):
    path = "live-tv/channels/channels-red-hd.m3u8"
    return {"id": "st-channels-red-hd", "path": path, "file": "%s/%s" % (MOUNT, path), "name": ".red HD",
            "media_info": probe, "display": {"title": ".red HD"}}


def streams(server, key):
    media = server.row(key).get("Media") or [{}]
    return [(s.get("index"), s.get("codec")) for s in (media[0].get("Part") or [{}])[0].get("Stream") or [] if "index" in s]


def layout(probe):
    """Its video then its audio streams, as rex lists a Part's: index and codec."""
    return [(s["index"], s["codec_name"]) for kind in ("video", "audio") for s in probe["streams"] if s["codec_type"] == kind]


def run():
    bytes_port = serve(Bytes().handler())
    work = tempfile.mkdtemp(prefix="rex-probe-changed-")
    catalog(work)
    server = Server(work, bytes_port)
    try:
        status, body = server.feed("POST", "items", {"items": [item(PROBES["first"])]})
        check("the channel is created", status == 200 and (body or {}).get("created") == 1, (status, body))
        key = server.keys()["st-channels-red-hd"]
        first = streams(server, key)
        check("its streams are the first mirror's", [s for s in first if s[1] in ("aac", "h264")] == layout(PROBES["first"]), first[:4])
        status, body = server.feed("POST", "items", {"items": [item(PROBES["second"])]})
        check("a probe with another layout updates the row", status == 200 and (body or {}).get("updated") == 1, (status, body))
        second = streams(server, key)
        check("its streams are the second mirror's, on the same row",
              server.keys()["st-channels-red-hd"] == key and [s for s in second if s[1] in ("aac", "h264")] == layout(PROBES["second"]),
              second[:4])
        status, body = server.feed("POST", "items", {"items": [item(PROBES["second"])]})
        check("the same probe again changes nothing", status == 200 and (body or {}).get("unchanged") == 1, (status, body))
    finally:
        server.stop()


def main():
    run()
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
