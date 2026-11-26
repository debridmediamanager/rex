#!/usr/bin/env python3
"""Record the ratings plexlab shows and the agent answers they came from, for tests/test_refresh_ratings.py.

Run on the lab host against plexlab (Big Buck Bunny 1, Tears of Steel 4, Firefly 6), which an
unclaimed PMS answers on loopback with no token, and its outbound capture:

    python3 probes/record_ratings.py http://127.0.0.1:32400 capture/outbound.jsonl > ratings_s1434.json

`plexlab` is Plex Web's detail and grid requests answered now, read only (GETs). `agent` is what
Plex's agent answered plexlab for each title's guid on two of its passes over the library, the
request a refresh sends (`GET /library/metadata/<id>?...&updating=1`; a show's with
`includeChildren=1`), and `search` the agent's answer to plexlab's own title search for Tears of
Steel when it matched it. With `AGENT_TOKEN` (any Plex account's token) set, the agent's answer to
the bare `GET /library/metadata/<id>` rex sends for a refresh is read now as well, for the three and
The Train Job (`<date> bare`); no pass of plexlab's asked an episode alone. Only the agent's JSON
bodies are kept: the capture's headers carry the server's token, and no artwork bytes are in it.
"""
import datetime
import json
import os
import sys
import urllib.request

WEB = ("includeConcerts=1&includeExtras=1&includeOnDeck=1&includePopularLeaves=1&includePreferences=1"
       "&includeReviews=1&includeChapters=1&includeStations=1&includeExternalMedia=1&asyncAugmentMetadata=1"
       "&asyncCheckFiles=1&asyncRefreshAnalysis=1&asyncRefreshLocalMediaAgent=1")
GRID = "includeCollections=1&includeExternalMedia=1&includeAdvanced=1&includeMeta=1&X-Plex-Container-Start=0&X-Plex-Container-Size=50"
READS = ["/library/metadata/1?" + WEB, "/library/metadata/4?" + WEB, "/library/metadata/6?" + WEB,
         "/library/sections/1/all?" + GRID, "/library/sections/2/all?" + GRID]
# Big Buck Bunny, Tears of Steel, Firefly.
GUIDS = ["5d7768352e80df001ebde4c9", "5d77699b96b655001fdd3b87", "5d9c086c7d06d9001ffd27aa"]
PASSES = ["2026-10-13", "2026-11-12"]
EPISODE = "5d9c127e4eefaa001f6449c2"  # The Train Job, plexlab's 8
SEARCH = ("2026-09-12", "Tears of Steel")


def read(base, path):
    request = urllib.request.Request(base + path, headers={"Accept": "application/json"})
    return json.load(urllib.request.urlopen(request, timeout=30))


def main():
    base, capture = sys.argv[1].rstrip("/"), sys.argv[2]
    agent, search = {guid: {} for guid in GUIDS}, None
    for line in open(capture, errors="replace"):
        if "metadata.provider.plex.tv" not in line:
            continue
        exchange = json.loads(line)
        if exchange.get("host") != "metadata.provider.plex.tv" or exchange.get("status") != 200:
            continue
        day, path = exchange["iso"][:10], exchange["path"]
        item = path.rsplit("/", 1)[-1]
        if day in PASSES and exchange["method"] == "GET" and path == "/library/metadata/" + item and item in GUIDS:
            agent[item][day] = json.loads(exchange["res_body"])
        if (day, exchange["method"], path) == (SEARCH[0], "POST", "/library/metadata/matches") and search is None:
            body = json.loads(exchange.get("req_body") or "{}")
            if body.get("title") == SEARCH[1]:
                search = {"request": body, "response": json.loads(exchange["res_body"])}
    token = os.environ.get("AGENT_TOKEN")
    if token:
        day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d") + " bare"
        for guid in GUIDS + [EPISODE]:
            request = urllib.request.Request("https://metadata.provider.plex.tv/library/metadata/%s?X-Plex-Country=US&X-Plex-Language=en-US" % guid,
                                             headers={"Accept": "application/json", "X-Plex-Token": token, "X-Plex-Language": "en-US"})
            agent.setdefault(guid, {})[day] = json.load(urllib.request.urlopen(request, timeout=30))
    json.dump({"reference": "plexlab, PMS 1.43.4.10903 (unclaimed since 2026-10-22; its library matched while claimed)",
               "plexlab": {path: read(base, path) for path in READS}, "agent": agent, "search": search},
              sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
