#!/usr/bin/env python3
"""Deleting a row the feed made deletes its file at the source, as a real PMS deletes the file.

Measured on plexscratch (PMS 1.43.4, `fixtures/media_delete_s43.json`): `DELETE
/library/metadata/{id}` removes the file -- the row goes only once it has -- and answers an
empty 200; with `allowMediaDeletion` off it is a 403; a file that cannot be removed leaves
the row and answers 400. rex deleted the row alone, so zurg, which still had the file,
pushed it back with its next snapshot (the production server's Mandalorian featurette came
back 70 minutes after its delete, 2026-10-14). A media server on zurg's mount reaches zurg
with a WebDAV `DELETE` of the file's path; this source stub answers it as zurg does (204,
or 403 for a file zurg will not delete, a placed `__magic__` one).

    python3 tests/test_feed_delete.py
"""
import json
import os
import sys
import tempfile
import urllib.parse

from test_feed import BYTES_TOKEN, Bytes, Server, catalog, check, item, results, serve

HERE = os.path.dirname(os.path.abspath(__file__))
MEASURED = {case["request"].split("/")[-1]: case for case in
            json.load(open(os.path.join(HERE, "fixtures", "media_delete_s43.json")))["cases"]}
ALLOWED, OFF, KEPT = MEASURED["58"], MEASURED["59"], MEASURED["1"]


class Zurg(Bytes):
    """The byte stub, answering a mount's DELETE as zurg's `deleteFileHandler` does."""

    def __init__(self):
        super().__init__()
        self.refused = set()
        self.deleted = []

    def handler(source):
        base = Bytes.handler(source)

        class Handler(base):
            def do_DELETE(self):
                path = urllib.parse.unquote(self.path)
                if self.headers.get("Authorization") != "Bearer " + BYTES_TOKEN:
                    return self.reply(401)
                if path in source.refused:
                    return self.reply(403, b"placed from __magic__")
                source.deleted.append(path)
                source.files.discard(path)
                self.reply(204)
        return Handler


def main():
    source = Zurg()
    port = serve(source.handler())
    work = tempfile.mkdtemp(prefix="rex-feed-delete-")
    catalog(work)
    server = Server(work, port)
    try:
        movie, other, episode = item("movie"), item("new"), item("episode")
        for pushed in (movie, other, episode):
            source.files.add("/dav/" + pushed["path"])
        status, body = server.feed("POST", "items", {"items": [movie, other, episode]})
        keys = server.keys()
        check("three files pushed", status == 200 and len(keys) == 3, (status, body))

        # -- allowed: the file goes at the source, then the row -----------------------------
        status, raw = server.request("/library/metadata/%s" % keys[movie["id"]], "DELETE")
        check("a delete answers %d with an empty body" % ALLOWED["status"],
              status == ALLOWED["status"] and raw in (b"", None), (status, raw))
        check("its file is deleted at the source, on its path under the source",
              source.deleted == ["/dav/" + movie["path"]], source.deleted)
        status, _ = server.request("/library/metadata/%s" % keys[movie["id"]])
        check("and the row answers %d" % ALLOWED["item_after"], status == ALLOWED["item_after"], status)

        # -- the source keeps the file: the row stays ----------------------------------------
        source.refused.add("/dav/" + other["path"])
        status, _ = server.request("/library/metadata/%s" % keys[other["id"]], "DELETE")
        check("a file the source will not delete answers %d" % KEPT["status"], status == KEPT["status"], status)
        status, _ = server.request("/library/metadata/%s" % keys[other["id"]])
        check("and leaves the row (%d)" % KEPT["item_after"], status == KEPT["item_after"], status)

        # -- deletion off: nothing reaches the source ----------------------------------------
        server.request("/:/prefs?allowMediaDeletion=0", "PUT")
        before = list(source.deleted)
        status, _ = server.request("/library/metadata/%s" % keys[episode["id"]], "DELETE")
        check("with allowMediaDeletion off a delete answers %d" % OFF["status"], status == OFF["status"], status)
        check("and sends the source nothing", source.deleted == before, source.deleted)
        status, _ = server.request("/library/metadata/%s" % keys[episode["id"]])
        check("and leaves the row (%d)" % OFF["item_after"], status == OFF["item_after"], status)
        server.request("/:/prefs?allowMediaDeletion=1", "PUT")

        # -- a show takes its episodes' files ------------------------------------------------
        show = server.row(keys[episode["id"]]).get("grandparentRatingKey")
        status, _ = server.request("/library/metadata/%s" % show, "DELETE")
        check("deleting the show deletes its episode's file at the source",
              status == 200 and source.deleted[-1:] == ["/dav/" + episode["path"]], (status, source.deleted))
        check("and the next snapshot of what the source still has brings none of it back",
              server.feed("PUT", "snapshot", {"items": [other]})[0] == 200
              and set(server.keys()) == {other["id"]}, server.keys())
    finally:
        server.stop()
    print("\n%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
