#!/usr/bin/env python3
"""An episode held as several releases its source placed alike is named by the retry, as one release is.

`fixtures/feed/retry_placed_versions_1024.json` holds the production rex's rows on 2026-10-24 for Re:ZERO's
S1E79 (three releases of `S04E13`), S1E80 (two of `S04E14`) and The Apothecary Diaries' S1E52 (two of
`S03E04`), each version placed by zurg on the same TMDB numbers and episode id, all still `local://`
under shows Plex's agent named, and Plex's metadata provider's answers for both shows as rex asks them
(`probes/record_provider_pages.py`): the provider lists every one of those episode ids. A row of several
releases was never retried nor re-identified by a push, so they stayed unmatched while every
single-release episode beside them was named. A stub answers as the provider did; plex.tv answers nothing.

    python3 tests/test_feed_retry_versions.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time

import test_feed_retry_placed
from test_feed import LEGACY, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "feed", "retry_placed_versions_1024.json")))
EPISODES = {"144954": 3, "150040": 2, "177347": 2}


def main():
    # the Animaniacs suite's stub provider, answering from this fixture
    test_feed_retry_placed.FIXTURE = FIXTURE
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), test_feed_retry_placed.Provider)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    source = Bytes()
    for row in FIXTURE["rows"]:
        for media in row.get("Media", []):
            if media.get("_sid") or row.get("_sid"):
                source.files.add("/dav/" + (media.get("_sid") or row["_sid"]))
    work = tempfile.mkdtemp(prefix="rex-retry-versions-")
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fd", "friendlyName": "rex-retry-versions",
               "sections": LEGACY["sections"], "items": FIXTURE["rows"]}, open(os.path.join(work, "catalog.json"), "w"))
    server = Server(work, serve(source.handler()), keys={"REX_PLEXTV_URL": "http://127.0.0.1:%d" % stub.server_port})
    try:
        deadline = time.time() + 90
        rows = {}
        while time.time() < deadline:
            rows = {key: server.row(key) for key in EPISODES}
            if all(str(row.get("guid", "")).startswith("plex://episode/") for row in rows.values()):
                break
            time.sleep(0.5)
        before = {row["ratingKey"]: row for row in FIXTURE["rows"]}
        for key, row in rows.items():
            check("episode %s, %d releases placed alike (S%sE%s), is named by the retry without a push"
                  % (key, EPISODES[key], row.get("parentIndex"), row.get("index")),
                  str(row.get("guid", "")).startswith("plex://episode/") and row.get("title") not in (None, "", "Episode %s" % row.get("index")),
                  (row.get("guid"), row.get("title")))
            check("episode %s keeps all %d of its releases" % (key, EPISODES[key]),
                  len(row.get("Media") or []) == EPISODES[key], len(row.get("Media") or []))
        check("each stays on the numbers its source placed it on",
              all((row.get("parentIndex"), row.get("index")) == (before[key]["parentIndex"], before[key]["index"]) for key, row in rows.items()),
              [(key, row.get("parentIndex"), row.get("index")) for key, row in rows.items()])
    finally:
        server.stop()
        stub.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
