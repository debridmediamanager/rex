#!/usr/bin/env python3
"""Every item the feed rejects is logged with its reason.

The production server's Usenet snapshot of 2026-10-17 04:21:34 rejected 579 items, and its log
counted them (`… 4 rejected`) and named none. `fixtures/feed/rejected_1017.json` is one of them
as zurg pushes it: a release's sample, an archive member.

    python3 tests/test_feed_rejected.py
"""
import json
import os
import shutil
import sys
import tempfile

from test_feed import ENTRIES, MOUNT, Bytes, Server, catalog, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "feed", "rejected_1017.json")))
# rex-fun 2026-10-23: a source's videos pushed again under new ids, with the files the old ids had.
SAME_FILE = json.load(open(os.path.join(HERE, "fixtures", "feed", "same_file_new_id_1023.json")))
PROBE = ENTRIES["episode"]["entry"]["media_info"]


def main():
    bytes_port = serve(Bytes().handler())
    work = tempfile.mkdtemp(prefix="rex-rejected-")
    catalog(work)
    server = Server(work, bytes_port)
    try:
        for name, pushed in sorted(FIXTURE["pushes"].items()):
            item = {field: value for field, value in pushed.items() if field != "want"}
            item.update(file="%s/%s" % (MOUNT, pushed["path"]), media_info=PROBE)
            status, body = server.feed("POST", "items", {"items": [item]})
            rejected = (body or {}).get("rejected") or []
            check("%s is rejected as %s" % (name, pushed["want"]),
                  status == 200 and rejected == [{"id": pushed["id"], "reason": pushed["want"]}], (status, body))
        first, again = (dict(SAME_FILE[name], file="%s/%s" % (MOUNT, SAME_FILE[name]["path"])) for name in ("first", "again"))
        status, body = server.feed("POST", "items", {"items": [first]})
        check("the first id of a file makes its row", status == 200 and (body or {}).get("created") == 1, (status, body))
        status, body = server.feed("POST", "items", {"items": [again]})
        want = "%s is already %s's file" % (again["file"], first["id"])
        check("a new id of a file another id has is rejected, naming that id (it was dropped unsaid)",
              status == 200 and (body or {}).get("rejected") == [{"id": again["id"], "reason": want}], (status, body))
        twins = [dict(first, id="twin-a", path="movies/Twins/twins.mp4", file=MOUNT + "/movies/Twins/twins.mp4", release="twins"),
                 dict(first, id="twin-b", path="movies/Twins/twins.mp4", file=MOUNT + "/movies/Twins/twins.mp4", release="twins")]
        status, body = server.feed("POST", "items", {"items": twins})
        check("two new ids of one file in one push: the second is rejected",
              status == 200 and (body or {}).get("created") == 1
              and (body or {}).get("rejected") == [{"id": "twin-b", "reason": "%s is already twin-a's file" % twins[0]["file"]}], (status, body))
        # The relay serves `videos/<id>.m3u8`: a video by its probe (ffprobe's `hls`), not its name.
        playlist = dict(first, id="playlist", path="movies/Playlist/1mjMdudwtRc.m3u8", file=MOUNT + "/movies/Playlist/1mjMdudwtRc.m3u8", release="playlist")
        status, body = server.feed("POST", "items", {"items": [playlist]})
        check("an .m3u8 file whose probe is HLS is a video", status == 200 and (body or {}).get("created") == 1, (status, body))
        named = dict(playlist, id="named", path="movies/Named/named.m3u8", file=MOUNT + "/movies/Named/named.m3u8", release="named", media_info=PROBE)
        status, body = server.feed("POST", "items", {"items": [named]})
        check("an .m3u8 file whose probe is a file's is still no video",
              status == 200 and (body or {}).get("rejected") == [{"id": "named", "reason": "not a video file"}], (status, body))
    finally:
        server.stop()
    log = open(os.path.join(work, "server.log"), errors="replace").read()
    for name, pushed in sorted(FIXTURE["pushes"].items()):
        line = "FEED zurg rejected %s: %s" % (pushed["id"], pushed["want"])
        check("the log says why %s was rejected" % name, line in log, line)
    line = "FEED zurg rejected hls-1mjMdudwtRc: "
    check("the log says why the new id was rejected", line in log, line)
    shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
