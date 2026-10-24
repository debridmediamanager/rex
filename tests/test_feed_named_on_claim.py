#!/usr/bin/env python3
"""What a source placed before rex could ask Plex's agent is named once it can, without waiting a day.

Unclaimed, rex has no token for Plex's agent, so a title its source placed keeps the name it was
pushed with. A sponsor's rex on 2026-10-24 was given zurg's `rex_url` before its claim: 7,035
releases arrived on their release names, the ten-minute retry named 50 a pass (about a day for
the lot), and zurg's pushes passed them by, because a push asked again only for a row whose
identification held neither a TMDB id nor a Plex guid, and DMM's placement always gives the TMDB
id. Here:

  (a) titles pushed to an unclaimed rex are named the moment it is claimed, with no push;
  (b) a title placed while Plex's agent was unreachable is named by the source's next push.

Plex's metadata provider answers as recorded (`fixtures/provider/matches.json`: Big Buck Bunny
and The Mentalist's first season); plex.tv is test_plextv's stub.

    python3 tests/test_feed_named_on_claim.py
"""
import hashlib
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time

import test_plextv as plextv
from test_plextv import AGENT, CATALOG, CLAIM_TOKEN, MACHINE, MEDIA_INFO, SERVER_TOKEN, Instance, Stub, check, wait_for
from test_ports import free_port

MOVIE = ("movies/Big.Buck.Bunny.2008.1080p.BluRay.x264", "Big.Buck.Bunny.2008.1080p.BluRay.x264.mkv",
         {"kind": "movie", "ids": {"tmdb": 10378}}, "plex://movie/5d7768352e80df001ebde4c9")
EPISODE = ("shows/The.Mentalist.S01.1080p.WEB-DL", "The.Mentalist.S01E01.1080p.WEB-DL.mkv",
           {"kind": "episode", "ids": {"tmdb": 5920}, "season": 1, "episode": 1}, "plex://episode/5d9c0fd6e264b7001fc8c9d3")

_answer = plextv.StubHandler.answer


def answer(self, record):
    # Plex's agent unreachable, as a provider outage answers it.
    if record["host"] == AGENT and getattr(self.server, "agent_down", False):
        return 503, "text/plain", "stub: the provider is down"
    return _answer(self, record)


plextv.StubHandler.answer = answer


def call(instance, method, path, document=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", instance.port, timeout=60)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["X-Plex-Token"] = token
    try:
        conn.request(method, path, body=json.dumps(document) if document is not None else None, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, json.loads(raw) if raw[:1] == b"{" else raw
    finally:
        conn.close()


def push(instance):
    """A source's registration, then both titles probed and placed as DMM places them."""
    items = []
    for release, file, placed, _ in (MOVIE, EPISODE):
        path = "%s/%s" % (release, file)
        item = {"id": path, "path": path, "file": "/zurg/" + path, "name": file, "release": release, "size": 12345,
                "hash": hashlib.sha1(path.encode()).hexdigest(), "media_info": MEDIA_INFO}
        item.update(placed)
        items.append(item)
    call(instance, "PUT", "/rex/sources/zurg", {"url": "http://127.0.0.1:9/dav", "mount": "/zurg"})
    return call(instance, "POST", "/rex/feed/zurg/items", {"items": items})[0]


def rows(instance):
    """Each title's row as a claimed client sees it, by its path."""
    status, body = call(instance, "GET", "/rex/feed/zurg/items")
    keys = {item["id"]: item["ratingKey"] for item in body.get("items", [])} if status == 200 else {}
    found = {}
    for release, file, _, _ in (MOVIE, EPISODE):
        key = keys.get("%s/%s" % (release, file))
        status, body = call(instance, "GET", "/library/metadata/%s" % key, token=SERVER_TOKEN) if key else (0, None)
        found[release] = ((body or {}).get("MediaContainer") or {}).get("Metadata", [{}])[0] if status == 200 else {}
    return found


def named(instance):
    found = rows(instance)
    return all(found[release].get("guid") == guid for release, _, _, guid in (MOVIE, EPISODE)) and found


def fresh(claimed):
    directory = tempfile.mkdtemp(prefix="rex-named-on-claim-")
    shutil.copy(CATALOG, os.path.join(directory, "catalog.json"))
    if claimed:
        json.dump({"machine": MACHINE, "token": SERVER_TOKEN, "owner": plextv.OWNER, "email": plextv.EMAIL,
                   "feature_uuids": [], "account_id": plextv.ACCOUNT_ID, "view_state_cursor": None},
                  open(os.path.join(directory, "claim_state.json"), "w"))
    return directory


def main():
    stub = Stub(free_port())
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    directories = []
    try:
        # -- (a) pushed unclaimed, then claimed ------------------------------------------
        directories.append(fresh(False))
        waiting = Instance(directories[-1], free_port(), stub.server_address[1], plex_token=None, announce=None)
        try:
            check("(a) an unclaimed rex takes both placed titles", push(waiting) == 200)
            time.sleep(2)
            check("(a) and asks Plex's agent nothing for them", not stub.taken(AGENT),
                  [(r["method"], r["path"]) for r in stub.taken(AGENT)])
            status = call(waiting, "POST", "/myplex/claim?token=%s" % CLAIM_TOKEN)[0]
            found = wait_for(lambda: named(waiting), 20) or rows(waiting)
            check("(a) once claimed, both are named by Plex's agent with no push",
                  status == 200 and all(found[release].get("guid") == guid for release, _, _, guid in (MOVIE, EPISODE)),
                  (status, {release: found[release].get("guid") for release in found}))
            check("(a) as the titles Plex's agent knows them by",
                  found[MOVIE[0]].get("title") == "Big Buck Bunny" and found[EPISODE[0]].get("title") == "Pilot"
                  and found[EPISODE[0]].get("grandparentTitle") == "The Mentalist",
                  {release: (row.get("title"), row.get("grandparentTitle")) for release, row in found.items()})
        finally:
            waiting.stop()

        # -- (b) claimed, pushed while Plex's agent is down, pushed again once it is up ----
        stub.clear()
        stub.agent_down = True
        directories.append(fresh(True))
        claimed = Instance(directories[-1], free_port(), stub.server_address[1], plex_token=None, announce=None)
        try:
            check("(b) a claimed rex takes both titles while Plex's agent is down", push(claimed) == 200)
            asked = wait_for(lambda: stub.taken(AGENT), 10) or []
            found = rows(claimed)
            check("(b) it asked the agent, which answered nothing, so neither is named",
                  asked and not any(str(row.get("guid", "")).startswith("plex://") for row in found.values()),
                  {release: row.get("guid") for release, row in found.items()})
            stub.agent_down = False
            check("(b) the source's next push is taken", push(claimed) == 200)
            found = wait_for(lambda: named(claimed), 20) or rows(claimed)
            check("(b) and names both, without waiting for the hourly retry",
                  all(found[release].get("guid") == guid for release, _, _, guid in (MOVIE, EPISODE)),
                  {release: found[release].get("guid") for release in found})
            check("(b) the episode's show too", found[EPISODE[0]].get("grandparentTitle") == "The Mentalist",
                  found[EPISODE[0]].get("grandparentTitle"))
            status, body = call(claimed, "GET", "/library/sections/2/all", token=SERVER_TOKEN)
            shows = [row.get("title") for row in ((body or {}).get("MediaContainer") or {}).get("Metadata", [])]
            check("(b) and no show is left on the release's name", status == 200 and EPISODE[1] not in shows, shows)
        finally:
            claimed.stop()
    finally:
        stub.shutdown()
        for directory in directories:
            shutil.rmtree(directory, ignore_errors=True)
    print("\n%d/%d passed" % (sum(plextv.results), len(plextv.results)))
    return 0 if all(plextv.results) else 1


if __name__ == "__main__":
    sys.exit(main())
