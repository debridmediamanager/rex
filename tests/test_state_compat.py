#!/usr/bin/env python3
"""A state database is the same file whichever release wrote it, rows on disk or not.

Since rows live in the state database rather than in memory (`rust/src/rows.rs`), a start reads
`state:items` row by row and keeps few of them; the file's shape did not change. So an install
needs no migration either way: this release reads what an older one wrote, and the older one
reads what this one wrote -- the way back after an upgrade. Proved with a real older release
(`REX_OLD_SERVER`, e.g. v0.3.0's binary) and this build (`REX_SERVER`, or the default), on one
state file: each restart answers every read as the run before it left it, and this release's
own writes reach the older one. An uploaded poster is served byte for byte by the other
release too: this one keeps none of the bytes the file has (`artwork::Uploads`), and writes an
upload as the older one did, `[base64, type]` by key.

    REX_OLD_SERVER=/path/to/rex-v0.3.0/rust/target/release/rex python3 tests/test_state_compat.py
"""
import hashlib
import http.client
import json
import os
import shutil
import signal
import sys
import tempfile
import urllib.parse

import test_persist
from test_persist import HERE, LIBRARY, Instance, check, differing, first, results, snapshot, write_everything


def run(binary, catalog, state):
    """A server of `binary` on `catalog` keeping `state` (`server_command` reads `REX_SERVER`)."""
    previous = os.environ.get("REX_SERVER")
    if binary:
        os.environ["REX_SERVER"] = binary
    else:
        os.environ.pop("REX_SERVER", None)
    try:
        return Instance(catalog, state)
    finally:
        if previous is None:
            os.environ.pop("REX_SERVER", None)
        else:
            os.environ["REX_SERVER"] = previous


def exchange(instance, method, path, body=None):
    """(status, raw body): `Instance.request` decodes a body as text."""
    connection = http.client.HTTPConnection("127.0.0.1", instance.port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=test_persist.HEADERS)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def upload(instance, body):
    """Upload `body` as row 1007's poster: the path its candidate is served from, or None."""
    status = exchange(instance, "POST", "/library/metadata/1007/posters", body)[0]
    key = "upload://posters/" + hashlib.sha1(body).hexdigest()
    return "/library/metadata/1007/file?url=" + urllib.parse.quote(key, safe="") if status == 200 else None


def main():
    old = os.environ.get("REX_OLD_SERVER")
    if not old:
        print("SKIP  REX_OLD_SERVER names no older release binary")
        sys.exit(0)
    new = os.environ.get("REX_SERVER")
    work = tempfile.mkdtemp(prefix="rex-state-compat-")
    try:
        catalog = os.path.join(work, "catalog.json")
        shutil.copy(os.path.join(HERE, "catalog_ui.json"), catalog)
        machine = json.load(open(catalog))["machineIdentifier"]
        state = os.path.join(work, "rex.db")

        before = run(old, catalog, state)
        ok, statuses = write_everything(before, machine)
        check("the older release accepts every write", ok, "statuses %s" % statuses)
        old_poster = b"\x89PNG\r\n\x1a\n" + b"uploaded to the older release" * 64
        old_upload = upload(before, old_poster)
        written = snapshot(before)
        before.stop(signal.SIGTERM)

        upgraded = run(new, catalog, state)
        read = snapshot(upgraded)
        check("this release reads the older one's file as it left it", not differing(written, read),
              "differ: %s" % differing(written, read))
        statuses = [upgraded.request(method, path)[0] for method, path in (
            ("GET", "/:/scrobble?key=1009&identifier=" + LIBRARY),
            ("GET", "/:/rate?key=1007&identifier=%s&rating=6" % LIBRARY),
            ("PUT", "/library/metadata/1007?title.value=Written%20After%20The%20Upgrade&title.locked=1"))]
        check("this release accepts its writes", all(200 <= status < 300 for status in statuses), statuses)
        check("this release serves the older one's upload as sent",
              old_upload and exchange(upgraded, "GET", old_upload) == (200, old_poster), old_upload)
        new_poster = b"\x89PNG\r\n\x1a\n" + b"uploaded after the upgrade" * 64
        new_upload = upload(upgraded, new_poster)
        # Once a save reached the file the rows are let go of: these reads come from the file.
        upgraded.request("GET", "/library/sections/1/all")
        after = snapshot(upgraded)
        check("its writes show", first(after["/library/metadata/1009"][1]).get("viewCount") == 1
              and first(after["/library/metadata/1007"][1]).get("title") == "Written After The Upgrade",
              str(after["/library/metadata/1007"])[:300])
        upgraded.stop(signal.SIGTERM)

        downgraded = run(old, catalog, state)
        back = snapshot(downgraded)
        check("the older release serves both releases' uploads as sent",
              new_upload and exchange(downgraded, "GET", new_upload) == (200, new_poster)
              and exchange(downgraded, "GET", old_upload) == (200, old_poster), new_upload)
        check("the older release reads this one's file as it left it", not differing(after, back),
              "differ: %s" % differing(after, back))
        downgraded.stop(signal.SIGTERM)

        again = run(new, catalog, state)
        check("and this release reads it once more", not differing(after, snapshot(again)))
        again.stop(signal.SIGTERM)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("%d/%d checks passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    test_persist.results = results
    main()
