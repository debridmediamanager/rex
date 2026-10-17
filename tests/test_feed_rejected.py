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
    finally:
        server.stop()
    log = open(os.path.join(work, "server.log"), errors="replace").read()
    for name, pushed in sorted(FIXTURE["pushes"].items()):
        line = "FEED zurg rejected %s: %s" % (pushed["id"], pushed["want"])
        check("the log says why %s was rejected" % name, line in log, line)
    shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
