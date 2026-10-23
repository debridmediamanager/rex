#!/usr/bin/env python3
"""Record Plex's metadata provider's answers, every page of each, for a test's stub provider.

    PLEX_TOKEN=<a claimed server's token> PLEX_CLIENT=<its machineIdentifier> \\
        python3 probes/record_provider_pages.py OUT.json 'POST /library/metadata/matches {"guid":"tmdb://65942","type":2}' \\
        'GET /library/metadata/<id>/children' ... [--seasons-of <show id>]

Each answer is kept as `{method, path, body, start, response}`, one per page of 50 (the size rex
asks; 500 is refused with a 400), and each row is cut to the fields rex reads of it. A GET of a
show's children with `--seasons-of` also records each season's children. No token is written.
"""
import json
import os
import sys
import urllib.request

PROVIDER = "https://metadata.provider.plex.tv"
KEEP = ("guid", "Guid", "index", "parentIndex", "title", "type", "summary", "originallyAvailableAt", "year", "thumb", "art",
        "leafCount", "ratingKey", "key", "contentRating", "studio")


def ask(method, path, body, start):
    headers = {"X-Plex-Token": os.environ["PLEX_TOKEN"], "Accept": "application/json", "Content-Type": "application/json",
               "X-Plex-Provider-Version": "6.5.0", "X-Plex-Client-Identifier": os.environ["PLEX_CLIENT"],
               "X-Plex-Product": "Plex Media Server"}
    if method == "GET":
        headers.update({"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": "50"})
    request = urllib.request.Request(PROVIDER + path, data=body.encode() if body else None, method=method, headers=headers)
    return json.load(urllib.request.urlopen(request, timeout=30))


def cut(answer):
    container = answer.get("MediaContainer", {})
    rows = [{key: row[key] for key in KEEP if key in row} for row in container.get("Metadata", [])]
    return {"MediaContainer": dict({key: value for key, value in container.items() if key != "Metadata"}, Metadata=rows)}


def record(method, path, body, out):
    start, rows = 0, []
    while True:
        answer = cut(ask(method, path, body, start))
        out.append({"method": method, "path": path, "body": body, "start": start, "response": answer})
        page = answer["MediaContainer"]["Metadata"]
        rows.extend(page)
        total = answer["MediaContainer"].get("totalSize", 0)
        if method != "GET" or not page or len(rows) >= total:
            return rows
        start = len(rows)


def main():
    target, specs = sys.argv[1], sys.argv[2:]
    out = []
    seasons_of = None
    if "--seasons-of" in specs:
        at = specs.index("--seasons-of")
        seasons_of, specs = specs[at + 1], specs[:at] + specs[at + 2:]
    for spec in specs:
        method, rest = spec.split(" ", 1)
        path, _, body = rest.partition(" ")
        rows = record(method, path, body, out)
        if seasons_of and path == "/library/metadata/%s/children" % seasons_of:
            for season in rows:
                record("GET", "/library/metadata/%s/children" % season["guid"].rsplit("/", 1)[1], "", out)
    json.dump({"answers": out}, open(target, "w"), indent=1, sort_keys=True)
    print("recorded %d pages" % len(out))


if __name__ == "__main__":
    main()
