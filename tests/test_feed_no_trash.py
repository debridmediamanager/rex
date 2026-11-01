#!/usr/bin/env python3
"""A source registered `"trash": false` keeps no trash: what it removes is deleted.

rex-live-sports pushes live channels and drops each from its next snapshot once it stops playing;
rex trashed every one, as it does a missing file that may come back, and the production server's
Live Sports library held 2,115 trashed channels beside 853 live ones on 2026-11-01 (`ls-espn-usa-2`
… `ls-espn-usa-2-2-2-…` among them; their ids below). A channel that ends never comes back as the
same file, so such a source registers `"trash": false` (docs/feed-api.md). A source that does not
still has its removals trashed (autoEmptyTrash off). That the option outlives a restart is
`source.rs`'s unit test.

    python3 tests/test_feed_no_trash.py
"""
import os
import shutil
import tempfile

from test_feed import ENTRIES, MOUNT, Bytes, Server, catalog, check, register, results, serve

# rex takes a `.m3u8` as a video by its probe's format alone, as live-sports' relay probes it.
_movie = ENTRIES["movie"]["entry"]["media_info"]
PROBE = dict(_movie, format=dict(_movie["format"], format_name="hls"))
CHANNELS = ["ls-espn-usa-2", "ls-espn-usa-2-2"]


def channel(sid):
    path = "live-sports/%s.m3u8" % sid[len("ls-"):]
    return {"id": sid, "path": path, "file": "%s/%s" % (MOUNT, path), "name": "ESPN+ USA", "release": sid,
            "size": 1, "media_info": PROBE, "display": {"title": "ESPN+ USA"}}


def removed_after_snapshot(server):
    """Both channels pushed, then a snapshot of the first alone: the second's row as it is after."""
    status, _ = server.feed("POST", "items", {"items": [channel(sid) for sid in CHANNELS]})
    keys = server.keys()
    gone = keys.get(CHANNELS[1])
    status, body = server.feed("PUT", "snapshot", {"session": "s1", "part": 0, "complete": True, "items": [channel(CHANNELS[0])]})
    return status, body, gone


def main():
    source = Bytes()
    for sid in CHANNELS:
        source.files.add("/dav/" + channel(sid)["path"])
    bytes_port = serve(source.handler())
    for keeps_trash in (False, True):
        work = tempfile.mkdtemp(prefix="rex-no-trash-")
        catalog(work)
        server = Server(work, bytes_port)
        try:
            if not keeps_trash:
                status, body = register(server, trash=False)
                check("registered with trash false", status == 200 and body.get("trash") is False, (status, body))
            status, body, gone = removed_after_snapshot(server)
            row = server.row(gone)
            if keeps_trash:
                check("by default the dropped channel is trashed",
                      body.get("removed", {}).get("trashed") == 1 and row.get("deletedAt"), (body, row.get("deletedAt")))
            else:
                check("the dropped channel is deleted, not trashed",
                      body.get("removed", {}).get("deleted") == 1 and body.get("removed", {}).get("trashed") == 0 and not row,
                      (body, row.get("deletedAt")))
        finally:
            server.stop()
            shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
