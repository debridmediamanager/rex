#!/usr/bin/env python3
"""Record what a real PMS with a DVR answers a shared user's Live TV requests, with and without
the share's Live TV permission (`allowTuners`), next to its owner
(`tests/fixtures/livetv/shared_tuners_s1434.json`).

    OWNER_TOKEN=... SHARED_TOKEN=... python3 probes/record_livetv_shared.py \
        --base http://127.0.0.1:32420 --allow-tuners 1 --out FILE.json [--merge]

`SHARED_TOKEN` is the `accessToken` plex.tv gives the shared user for this server. Run it once
per share setting (change it at plex.tv between runs), `--merge` adding to the same file. Only
reads and one tune, whose session is stopped at the end. Tokens are written as `<OWNER>` and
`<SHARED>`; bodies are kept only as `/media/providers`' identifiers and What's On's hubs, the rest by status alone.
"""
import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request

PROVIDER = "/tv.plex.providers.epg.xmltv:2"
CLIENT = {
    "X-Plex-Client-Identifier": "rex-livetv-shared-probe",
    "X-Plex-Product": "Plex Web",
    "X-Plex-Version": "4.163.0",
    "X-Plex-Platform": "Chrome",
    "Accept": "application/json",
}
# The Live TV reads Plex Web and Plex for Android made in tests/fixtures/livetv/*_s1434, and
# the tune that starts watching.
REQUESTS = [
    ("GET", "/media/providers", ()),
    ("GET", "/livetv/dvrs", ()),
    ("GET", "/livetv/dvrs/2", ()),
    ("GET", "/livetv/sessions", ()),
    ("GET", PROVIDER + "/sections", ()),
    ("GET", PROVIDER + "/lineups/dvr/channels", ()),
    ("GET", PROVIDER + "/grid", (("type", "1"),)),
    ("GET", PROVIDER + "/hubs/discover", ()),
    ("GET", PROVIDER + "/watchnow", ()),
    ("GET", PROVIDER + "/hubs/search", (("query", "Firefly"),)),
    ("GET", PROVIDER + "/library/search", (("query", "Firefly"), ("searchTypes", "livetv"))),
    ("POST", "/livetv/dvrs/2/channels/2.1/tune", ()),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--allow-tuners", required=True, choices=["0", "1"])
    parser.add_argument("--out", required=True)
    parser.add_argument("--merge", action="store_true")
    args = parser.parse_args()
    tokens = {"owner": os.environ["OWNER_TOKEN"], "shared": os.environ["SHARED_TOKEN"]}
    record = json.load(open(args.out)) if args.merge and os.path.exists(args.out) else {
        "_provenance": "plexav (PMS 1.43.4, an HDHomeRun and an XMLTV guide, no Plex Pass), 2026-11-01: "
                       "a managed Home user of its owner, shared every library, asked each request "
                       "with its share's allowTuners 0 and 1 (plex.tv shared_servers sharing_settings).",
        "runs": {}}
    run = []
    for who in ("owner", "shared"):
        for method, path, query in REQUESTS:
            pairs = list(query) + [("X-Plex-Token", tokens[who])]
            request = urllib.request.Request(args.base + path + "?" + urllib.parse.urlencode(pairs),
                                             method=method, headers=CLIENT)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    status, body = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, body = error.code, error.read()
            entry = {"who": who, "method": method, "path": path, "query": [list(p) for p in query], "status": status}
            if path == "/media/providers" and status == 200:
                providers = json.loads(body)["MediaContainer"].get("MediaProvider", [])
                entry["providers"] = [provider.get("identifier") for provider in providers]
            if path.endswith("/hubs/discover") and status == 200:
                hubs = json.loads(body)["MediaContainer"].get("Hub", [])
                entry["hubs"] = [[hub.get("hubIdentifier"), hub.get("size")] for hub in hubs]
            if path.endswith("/tune") and status == 200:
                session = json.loads(body)["MediaContainer"]["MediaSubscription"][0]["MediaGrabOperation"][0]["Metadata"]
                stop = urllib.request.Request(args.base + "/livetv/sessions/" + urllib.parse.quote(str(session.get("key", "")).rsplit("/", 1)[-1])
                                              + "?X-Plex-Token=" + tokens["owner"], method="DELETE", headers=CLIENT)
                try:
                    urllib.request.urlopen(stop, timeout=30).read()
                except urllib.error.HTTPError:
                    pass
            run.append(entry)
            print(who, method, path, status, entry.get("providers", ""))
    record["runs"]["allowTuners=" + args.allow_tuners] = run
    with open(args.out, "w") as handle:
        json.dump(record, handle, indent=1)
        handle.write("\n")


if __name__ == "__main__":
    main()
