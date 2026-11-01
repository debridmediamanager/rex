#!/usr/bin/env python3
"""Record what plexav answers for two releases of one album (`tests/fixtures/music/album-releases.json`).

plexav is a real PMS 1.43.4 on kids (`~/plexav`, API on 127.0.0.1:32420 there) whose music library
held, on 2026-11-01, Radiohead's In Rainbows twice: the FLAC folder `In Rainbows (2007)/` of
`make-music.sh` and `In Rainbows (2007) [AAC]/`, the same ten tones as AAC in m4a. Its agent matched
both to Plex's album `plex://album/5d07c2b1403c6402908fddb6`, and it lists two album rows of that guid,
each with its own ten tracks.

Read-only: every request is a GET Plex Web sent on plexav (`fixtures/music/web-music-*.jsonl`) with
its `async*` refreshes left off, run on kids over ssh with the server's own token (read there, never
printed), the answers redacted as `harness/scripts/redact.py` redacts and kept without their `summary`
texts. `albums` names each In Rainbows row's folder.

    python3 probes/record_album_releases.py [--host ben@kids] [--out tests/fixtures/music/album-releases.json]
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "harness", "scripts"))
from redact import scrub_body  # noqa: E402

GUID = "plex://album/5d07c2b1403c6402908fddb6"
WEB = "includeCollections=1&includeExternalMedia=1&includeAdvanced=1&includeMeta=1"
DETAIL = ("includeConcerts=1&includeExtras=1&includeOnDeck=1&includePopularLeaves=1&includePreferences=1"
          "&includeReviews=1&includeChapters=1&includeStations=1&includeExternalMedia=1")
HUBS = "count=12&includeLibraryPlaylists=1&includeStations=1&includeRecentChannels=1&includeMeta=1&includeExternalMetadata=1&excludeFields=summary"

# Runs on the PMS's host: reads the server's token from its Preferences.xml and answers
# {"path?query": [status, body]} for the requests in PATHS.
REMOTE = r'''
import json, subprocess, urllib.request, urllib.error
token = subprocess.run(["docker", "exec", "plexav-plex", "sh", "-c",
    "grep -o 'PlexOnlineToken=\"[^\"]*' '/config/Library/Application Support/Plex Media Server/Preferences.xml'"],
    capture_output=True, text=True, check=True).stdout.split('"')[1].strip()
out = {}
for path in PATHS:
    request = urllib.request.Request("http://127.0.0.1:32420" + path, headers={
        "Accept": "application/json", "X-Plex-Token": token, "X-Plex-Product": "Plex Web",
        "X-Plex-Client-Identifier": "rex-album-releases-probe", "X-Plex-Platform": "Chrome"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            out[path] = [response.status, response.read().decode()]
    except urllib.error.HTTPError as error:
        out[path] = [error.code, error.read().decode()]
print(json.dumps(out))
'''


def strip(value):
    """Without the agent's biographies and reviews (`summary`): kilobytes nothing here reads."""
    if isinstance(value, dict):
        return {key: strip(inner) for key, inner in value.items() if key != "summary"}
    if isinstance(value, list):
        return [strip(inner) for inner in value]
    return value


def remote(host, paths):
    program = "PATHS = %s\n%s" % (json.dumps(paths), REMOTE)
    done = subprocess.run(["ssh", host, "python3", "-"], input=program, capture_output=True, text=True, check=True)
    return {path: (status, scrub_body(body)) for path, (status, body) in json.loads(done.stdout).items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="ben@kids")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(HERE), "tests", "fixtures", "music", "album-releases.json"))
    args = parser.parse_args()
    listing = "/library/sections/1/all?type=9&" + WEB
    _, body = remote(args.host, [listing])[listing]
    albums = [row for row in json.loads(body)["MediaContainer"]["Metadata"] if row.get("guid") == GUID]
    if len(albums) != 2:
        sys.exit("expected two album rows of %s, plexav lists %d" % (GUID, len(albums)))
    artist = albums[0]["parentRatingKey"]
    paths = [listing, "/library/sections/1/all?type=8&" + WEB, "/library/metadata/%s/children?excludeAllLeaves=1" % artist,
             "/library/metadata/%s?%s" % (artist, DETAIL), "/hubs/sections/1?" + HUBS,
             "/library/metadata/%s/related?includeAugmentations=1&includeExternalMetadata=1&includeMeta=1" % artist,
             "/library/sections/1/all?album.subformat!=Compilation,Live&artist.id=%s&group=title&limit=100&ratingCount>=1"
             "&resolveTags=1&sort=ratingCount:desc&type=10" % artist,
             "/library/search?query=In%20Rainbows&limit=100&searchTypes=movies,music,people,tv&includeCollections=1&includeExternalMedia=1"]
    for album in albums:
        paths += ["/library/metadata/%s?%s" % (album["ratingKey"], DETAIL),
                  "/library/metadata/%s/children?excludeAllLeaves=1" % album["ratingKey"]]
    answers = {path: (status, strip(json.loads(body))) for path, (status, body) in remote(args.host, paths).items()}
    # Each album row's folder, from its first track's Part.
    folders = {}
    for album in albums:
        tracks = answers["/library/metadata/%s/children?excludeAllLeaves=1" % album["ratingKey"]][1]["MediaContainer"]["Metadata"]
        folders[album["ratingKey"]] = tracks[0]["Media"][0]["Part"][0]["file"].rsplit("/", 2)[1]
    record = {
        "recorded": "2026-11-01, plexav (PMS 1.43.4) with In Rainbows as FLAC and as AAC in two folders",
        "albums": folders,
        "answers": [{"path": path.split("?")[0], "query": path.split("?", 1)[1] if "?" in path else "",
                     "status": status, "body": body} for path, (status, body) in answers.items()],
    }
    with open(args.out, "w") as out:
        json.dump(record, out, sort_keys=True)
        out.write("\n")
    print("%s: %d answers, %d bytes" % (args.out, len(answers), os.path.getsize(args.out)))


if __name__ == "__main__":
    main()
