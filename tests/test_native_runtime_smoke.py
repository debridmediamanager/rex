#!/usr/bin/env python3
"""Small native-only route contract smoke test.

The large UI suite owns the exhaustive measured surface.  This driver keeps a
short, repeatable denominator for the native routes added during phase 3, so a
build check cannot accidentally become the only runtime evidence.  It is run
on zen with ``PLEXSIM_SERVER`` pointing at the Rust binary; the default Python
selection remains unchanged for the historical suites.
"""

import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from server_command import server_command  # noqa: E402


def request(base, path, method="GET"):
    req = urllib.request.Request(
        base + path,
        method=method,
        headers={"Accept": "application/json", "X-Plex-Client-Identifier": "native-smoke"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read()
            return response.status, json.loads(body or b"{}"), response.headers
    except urllib.error.HTTPError as error:
        return error.code, {}, error.headers
    except urllib.error.URLError:
        return 0, {}, {}


def check(results, label, condition, detail=""):
    results.append(bool(condition))
    print(("PASS" if condition else "FAIL"), label, detail)


def main():
    if not os.environ.get("PLEXSIM_SERVER"):
        print("PLEXSIM_SERVER must select the native executable", file=sys.stderr)
        return 2
    port = 32620
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            print("port %d is busy" % port, file=sys.stderr)
            return 2
    command = server_command(
        "--port", port, "--catalog", "tests/catalog_ui.json", "--gdm", "off",
        "--assert-claimed", root=ROOT
    )
    # Like `server.py`, the server refuses to start without a Real-Debrid key.
    env = dict(os.environ, RD_TOKEN=os.environ.get("RD_TOKEN") or "dummy")
    process = subprocess.Popen(command, cwd=ROOT, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    results = []
    try:
        for _ in range(40):
            status, _, _ = request(base, "/identity")
            if status == 200:
                break
            time.sleep(0.1)
        status, identity, _ = request(base, "/identity")
        identity_container = identity.get("MediaContainer") or {}
        check(
            results,
            "identity is native and explicitly claimed",
            status == 200 and identity_container and identity_container.get("claimed") == 1,
        )

        status, sections, _ = request(base, "/library/sections")
        directories = sections.get("MediaContainer", {}).get("Directory", [])
        check(results, "library sections are listed", status == 200 and len(directories) >= 2)
        section = directories[0].get("key", "").rsplit("/", 1)[-1]
        status, listing, _ = request(base, "/library/sections/%s/all" % section)
        rows = listing.get("MediaContainer", {}).get("Metadata", [])
        check(results, "section listing has fixture rows", status == 200 and bool(rows))
        key = rows[0].get("ratingKey")
        status, detail, _ = request(base, "/library/metadata/%s" % key)
        detail_rows = detail.get("MediaContainer", {}).get("Metadata", [])
        check(results, "metadata detail preserves Media", status == 200 and bool(detail_rows) and "Media" in detail_rows[0])

        status, tags, _ = request(base, "/library/tags?type=6")
        tag_rows = tags.get("MediaContainer", {}).get("Directory", [])
        check(results, "person tag table is populated", status == 200 and bool(tag_rows))
        if tag_rows:
            person_id = tag_rows[0].get("id")
            status, person, _ = request(base, "/library/people/%s" % person_id)
            person_rows = person.get("MediaContainer", {}).get("Directory", [])
            check(results, "person lookup resolves numeric tag id", status == 200 and bool(person_rows))
            status, person_media, _ = request(base, "/library/people/%s/media" % person_id)
            check(results, "person media is a native listing", status == 200 and "Metadata" in person_media.get("MediaContainer", {}))
        else:
            check(results, "person lookup resolves numeric tag id", False, "no tag rows")
            check(results, "person media is a native listing", False, "no tag rows")

        status, arts, _ = request(base, "/library/arts")
        check(results, "owned artwork listing is routed", status == 200 and "Metadata" in arts.get("MediaContainer", {}))
        status, matches, _ = request(base, "/library/matches?type=1&title=Tears%20of%20Steel")
        check(results, "library matches returns exact title rows", status == 200 and bool(matches.get("MediaContainer", {}).get("Metadata")))
        status, sync, _ = request(base, "/sync")
        check(results, "bare sync uses the plugin container", status == 200 and sync.get("MediaContainer", {}).get("content") == "plugins")
        status, _, _ = request(base, "/myplex/claim", method="POST")
        check(results, "claim without token follows Python's 400 contract", status == 400)

        native = shlex.split(os.environ["PLEXSIM_SERVER"])[0]
        malformed = subprocess.run([native, "--unknown-native-option"], cwd=ROOT, capture_output=True)
        check(results, "native CLI rejects an unconsumed option", malformed.returncode != 0)
        # `main()`'s argparse refuses these before serving anything; a CLI that
        # accepted them would start a server here, which the timeout catches.
        for label, extra, wanted in (
            ("an unknown --part-mode", ["--part-mode", "bogus"], b"invalid choice"),
            ("an unknown --gdm", ["--gdm", "sometimes"], b"invalid choice"),
            ("--tls-key without --tls-cert", ["--tls-key", "key.pem"], b"--tls-key needs --tls-cert"),
            ("to serve without RD_TOKEN", [], b"RD_TOKEN must be set"),
        ):
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            spare = probe.getsockname()[1]
            probe.close()
            try:
                refused = subprocess.run(
                    [native, "--port", str(spare), "--catalog", "tests/catalog_ui.json",
                     "--gdm", "off"] + extra,
                    cwd=ROOT, env=env if extra else dict(env, RD_TOKEN=""),
                    capture_output=True, timeout=5)
                ok = refused.returncode == 2 and wanted in refused.stderr
            except subprocess.TimeoutExpired:
                ok = False
            check(results, "native CLI refuses " + label, ok)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    passed = sum(results)
    print("PASS %d/%d native runtime smoke checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
