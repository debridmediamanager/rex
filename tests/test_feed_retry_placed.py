#!/usr/bin/env python3
"""An episode its source placed that Plex's agent had not named is named again by the retry, not only by a push.

`fixtures/feed/retry_placed_episodes_1023.json` holds the production rex's Animaniacs show, two seasons
and three episodes as they were before 43c6fd5c went live on 2026-10-23 (placed by zurg with TMDB's
numbers and episode ids, still `local://`), and Plex's metadata provider's answers for the show as rex
asks them (`probes/record_provider_pages.py`). Once rex read those answers whole, the rows stayed
unmatched until zurg's next snapshot, six hours on: the retry every ten minutes took only titles on the
blank tile, and Animaniacs has its poster. A stub answers as the provider did; plex.tv answers nothing.

    python3 tests/test_feed_retry_placed.py
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time

from test_feed import LEGACY, Bytes, Server, check, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "feed", "retry_placed_episodes_1023.json")))
PREFIX = "/metadata.provider.plex.tv"
EPISODES = ("104945", "104946", "129641")


def body_of(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return text


class Provider(http.server.BaseHTTPRequestHandler):
    """Plex's provider as recorded, paged by `X-Plex-Container-Start`; plex.tv is a 404."""

    def answer(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = body_of(self.rfile.read(size).decode()) if size else None
        path = self.path[len(PREFIX):] if self.path.startswith(PREFIX + "/") else None
        start = int(self.headers.get("X-Plex-Container-Start") or 0)
        found = next((a["response"] for a in FIXTURE["answers"] if path and a["method"] == self.command
                      and a["path"] == path and a["start"] == start and body_of(a["body"]) == body), None)
        raw = json.dumps(found).encode() if found is not None else b""
        self.send_response(200 if found is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = answer

    def log_message(self, *args):
        pass


def main():
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    source = Bytes()
    for row in FIXTURE["rows"]:
        if row.get("_sid"):
            source.files.add("/dav/" + row["_sid"])
    work = tempfile.mkdtemp(prefix="rex-retry-placed-")
    json.dump({"machineIdentifier": "00000000000000000000000000000000000000fe", "friendlyName": "rex-retry-placed",
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
        for key, row in rows.items():
            check("placed episode %s (S%sE%s) is named by the retry without a push" % (key, row.get("parentIndex"), row.get("index")),
                  str(row.get("guid", "")).startswith("plex://episode/") and row.get("title") not in (None, "", "Episode %s" % row.get("index")),
                  (row.get("guid"), row.get("title")))
        before = {row["ratingKey"]: row for row in FIXTURE["rows"]}
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
