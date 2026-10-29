#!/usr/bin/env python3
"""Record what a real PMS answers the requests `POST /rex/import-watched` sends it
(`tests/fixtures/watch_import_s143.json`): its libraries, then each film and TV library's
leaves page by page, as the token's account sees them.

    PLEX_TOKEN=... python3 probes/record_watch_import.py --base URL --out FILE.json [--seed]

`--seed` first writes the watch state the fixture needs -- a film part-watched, an episode
watched, one watched and then started again, a rated one and a watched `local://` one --
through the same `/:/scrobble`, `/:/progress` and `/:/rate` a client sends. Only ever
against a disposable copy (plexscratch). The token is written as `<TOKEN>`.
"""
import argparse
import json
import os
import urllib.parse
import urllib.request

# rex's page size (`watch_import::PAGE`), and a small one to show how PMS pages.
PAGE = 500
SMALL = 2
LIBRARY = "com.plexapp.plugins.library"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    token = os.environ["PLEX_TOKEN"]
    exchanges = []

    def ask(path, query=(), record=True):
        url = args.base + path + ("?" + urllib.parse.urlencode(list(query)) if query else "")
        request = urllib.request.Request(url, headers={"Accept": "application/json", "X-Plex-Token": token})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            if record:
                exchanges.append({
                    "method": "GET",
                    "path": path,
                    "query": [list(pair) for pair in query],
                    "headers": {"Accept": "application/json", "X-Plex-Token": "<TOKEN>"},
                    "status": response.status,
                    "content_type": response.headers.get("Content-Type"),
                    "body": json.loads(body),
                })
            return body

    if args.seed:
        leaves = {}
        for section, kind in (("1", "1"), ("2", "4")):
            listed = json.loads(ask("/library/sections/%s/all" % section, [("type", kind)], record=False))
            for row in listed["MediaContainer"].get("Metadata", []):
                leaves[row["title"]] = row["ratingKey"]
        for title, path, query in (
            ("Big Buck Bunny", "/:/progress", [("time", "600000"), ("state", "stopped")]),
            ("The Train Job", "/:/scrobble", []),
            ("Our Mrs. Reynolds", "/:/scrobble", []),
            ("Our Mrs. Reynolds", "/:/progress", [("time", "300000"), ("state", "stopped")]),
            ("Bushwhacked", "/:/rate", [("rating", "8")]),
            ("Episode 1", "/:/scrobble", []),
        ):
            ask(path, [("key", leaves[title]), ("identifier", LIBRARY)] + query, record=False)

    sections = json.loads(ask("/library/sections"))
    for directory in sections["MediaContainer"]["Directory"]:
        kind = {"movie": "1", "show": "4"}.get(directory["type"])
        if kind:
            path = "/library/sections/%s/all" % directory["key"]
            ask(path, [("type", kind), ("X-Plex-Container-Start", "0"), ("X-Plex-Container-Size", str(PAGE))])
    # The same show library in pages of two: where a page ends and what the last one says.
    start = 0
    while True:
        body = json.loads(ask("/library/sections/2/all",
                              [("type", "4"), ("X-Plex-Container-Start", str(start)), ("X-Plex-Container-Size", str(SMALL))]))
        start += SMALL
        if len(body["MediaContainer"].get("Metadata", [])) < SMALL:
            break
    version = json.loads(ask("/identity", record=False))["MediaContainer"]["version"]
    with open(args.out, "w") as out:
        json.dump({"recorded_from": "plexscratch, PMS " + version,
                   "exchanges": exchanges}, out, indent=1)
        out.write("\n")
    print("%d exchanges -> %s" % (len(exchanges), args.out))


if __name__ == "__main__":
    main()
