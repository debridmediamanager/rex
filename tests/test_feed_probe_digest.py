#!/usr/bin/env python3
"""An item sent by its probe's digest (`probe`) instead of its `media_info` (docs/feed-api.md,
"Probe digests").

A source's snapshot sent every file's whole ffprobe output every six hours for rows that already
had it: the production server's 41,549 items, all but 2% unchanged (2026-10-24). rex keeps each
version's `probe` and applies an item that brings only its digest where that is enough; where it
is not -- a new item, a digest that changed, a version from before digests -- it asks for the item
whole (`wants`) and changes nothing of it, and a snapshot still counts it as named.

    python3 tests/test_feed_probe_digest.py
"""
import copy
import os
import shutil
import sys
import tempfile

from test_feed import Bytes, Server, catalog, check, item, register, results, serve


def light(full, probe):
    """`full` as a source sends it once rex has its probe: the digest, no media_info."""
    out = copy.deepcopy(full)
    out.pop("media_info", None)
    out["probe"] = probe
    return out


def main():
    source = Bytes()
    bytes_port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-probe-digest-")
    catalog(work)
    server = Server(work, bytes_port)
    try:
        status, answer = register(server)
        check("registering answers the probe_digest feature",
              status == 200 and "probe_digest" in (answer or {}).get("features", []), (status, answer))

        movie = dict(item("movie"), probe="digest-1")
        status, body = server.feed("POST", "items", {"items": [movie]})
        check("a whole item with its digest is created", status == 200 and body.get("created") == 1, (status, body))
        key = server.keys()[movie["id"]]
        streams = server.row(key).get("Media")

        status, body = server.feed("PUT", "snapshot", {"items": [light(movie, "digest-1")]})
        check("a snapshot naming it by the same digest needs nothing more",
              status == 200 and "wants" not in body and body.get("unchanged") == 1, (status, body))
        check("...and removes nothing", (body.get("removed") or {}).get("trashed") == 0 and movie["id"] in server.keys(),
              (body.get("removed"), list(server.keys())))
        check("...and the row keeps its streams", server.row(key).get("Media") == streams)

        renamed = dict(light(movie, "digest-1"), name=movie["name"] + " (Director's Cut)")
        status, body = server.feed("POST", "items", {"items": [renamed]})
        check("a digest-only push still applies its other fields", status == 200 and "wants" not in body, (status, body))

        status, body = server.feed("PUT", "snapshot", {"items": [light(movie, "digest-2")]})
        check("a changed digest is asked for whole", status == 200 and body.get("wants") == [movie["id"]], (status, body))
        check("...counted as named, so nothing is removed",
              (body.get("removed") or {}).get("trashed") == 0 and movie["id"] in server.keys(), body.get("removed"))
        check("...and the row is left as it was", server.row(key).get("Media") == streams)

        status, body = server.feed("POST", "items", {"items": [dict(item("movie"), probe="digest-2")]})
        check("the whole item it asked for is taken", status == 200 and "wants" not in body, (status, body))
        status, body = server.feed("PUT", "snapshot", {"items": [light(movie, "digest-2")]})
        check("...after which the new digest is enough", status == 200 and "wants" not in body, (status, body))

        new = dict(item("new"), probe="digest-new")
        status, body = server.feed("POST", "items", {"items": [light(new, "digest-new")]})
        check("a new item sent by its digest alone is asked for, not held",
              status == 200 and body.get("wants") == [new["id"]] and body.get("held") == 0 and body.get("created") == 0,
              (status, body))
        status, held = server.feed("GET", "held")
        check("...and nothing of it is kept", new["id"] not in [entry["id"] for entry in (held or {}).get("held", [])], held)
        status, body = server.feed("POST", "items", {"items": [new]})
        check("...until it comes whole", status == 200 and body.get("created") == 1, (status, body))

        episode = item("episode")
        status, body = server.feed("POST", "items", {"items": [episode]})
        check("an item pushed with no digest, as before digests", status == 200 and body.get("created") == 1, (status, body))
        status, body = server.feed("POST", "items", {"items": [light(episode, "digest-ep")]})
        check("...is asked for whole the first time its digest comes alone",
              status == 200 and body.get("wants") == [episode["id"]], (status, body))

        bare = item("episode", probe=False)
        status, body = server.feed("POST", "items", {"items": [dict(bare, id=bare["id"] + ".other", path=bare["path"] + ".other.mkv",
                                                                   file=bare["file"] + ".other.mkv")]})
        check("a source that sends no digest is held without media_info, as before",
              status == 200 and body.get("held") == 1 and "wants" not in body, (status, body))

        server.stop()
        server = Server(work, bytes_port)
        status, body = server.feed("PUT", "snapshot", {"items": [light(movie, "digest-2"), light(new, "digest-new"),
                                                                 dict(item("episode"), probe="digest-ep")]})
        check("a version's digest is kept across a restart", status == 200 and "wants" not in body, (status, body))
    finally:
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    print("%d/%d probe digest checks passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
