#!/usr/bin/env python3
"""The full client path: plex.tv account -> resource discovery -> connect -> play.

conformance.py points a client straight at plexsim's URL, which proves the REST
contract but skips the half that makes the whole idea work: a real Plex client
never takes a URL, it asks plex.tv which servers the account has and dials what
it is told.  This drives that path with python-plexapi's MyPlexAccount, the same
discovery every official client performs.

    PLEX_TOKEN=... .venv/bin/python plexsim/discover.py
"""

import argparse
import os
import sys
import time

from plexapi.myplex import MyPlexAccount

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results = []


def check(label, condition, detail=""):
    results.append(bool(condition))
    print("  %s  %-50s %s" % (PASS if condition else FAIL, label, detail))
    return bool(condition)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="plexsim-spike", help="resource name to dial")
    args = parser.parse_args()

    token = os.environ.get("PLEX_TOKEN")
    if not token:
        print("PLEX_TOKEN must be set")
        return 2

    print("\n=== 1. plex.tv account ===")
    account = MyPlexAccount(token=token)
    check("signed in", bool(account.username), account.username)
    check("plex pass", "plexpass" in (account.roles or []), str(account.subscriptionPlan))

    print("\n=== 2. resource discovery (what every client does) ===")
    resources = [r for r in account.resources() if "server" in (r.provides or "")]
    check("server resources", len(resources) >= 1, "%d found" % len(resources))
    for resource in resources:
        print("        - %-18s product=%-20s" % (resource.name, resource.product))

    target = next((r for r in resources if r.name == args.name), None)
    if not check("plexsim is discoverable", target is not None, args.name):
        return 1

    print("\n=== 3. connect via the advertised connection ===")
    started = time.time()
    server = target.connect(timeout=30)
    check("connected", True, "%s in %.2fs" % (server.friendlyName, time.time() - started))
    check("machineIdentifier matches", bool(server.machineIdentifier), server.machineIdentifier)
    check(
        "connection was the advertised URI",
        server._baseurl.startswith("https://"),
        server._baseurl,
    )

    print("\n=== 4. browse the discovered server ===")
    sections = server.library.sections()
    check("sections", len(sections) >= 1, ", ".join(s.title for s in sections))
    items = sections[0].all()
    check("items", len(items) >= 1, "%d item(s)" % len(items))

    print("\n=== 5. play from the discovered server ===")
    target_item = None
    for item in items:
        full = server.fetchItem(int(item.ratingKey))
        if full.media and full.media[0].videoCodec == "h264":
            target_item = full
            break
    if not check("a direct-playable item exists", target_item is not None):
        return 1

    part = target_item.media[0].parts[0]
    stream_url = server.url(part.key, includeToken=True)
    check("stream URL built by the client library", stream_url.startswith("https://"),
          stream_url.split("?")[0])

    import requests

    response = requests.get(stream_url, headers={"Range": "bytes=0-99"}, timeout=60)
    check("206 over the discovered connection", response.status_code == 206,
          str(response.status_code))
    check("true total size", (response.headers.get("Content-Range") or "").endswith(
        "/%d" % part.size), response.headers.get("Content-Range"))
    check("matroska magic", response.content[:4] == b"\x1a\x45\xdf\xa3",
          response.content[:4].hex())

    total = len(results)
    passed = sum(results)
    print("\n%s  %d/%d checks passed" % (PASS if passed == total else FAIL, passed, total))
    print("\nstream URL for a player:\n  %s\n" % stream_url)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
