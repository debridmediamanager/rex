#!/usr/bin/env python3
"""A collection made Visible on Library, Home and Friends' Home, as a real PMS keeps and serves it.

`fixtures/collection_hub_pins_s1435.json` is Plex Web 4.162.1 driving a real, online PMS 1.43.5
(harness clips: Big Buck Bunny 1, Elephants Dream 2, Sintel 3, Tears of Steel 4; collections
"Blender Films" 15 = 1,2,3 and "Open Movies" 16 = 3,4): a collection's "Visible on" menu
(`GET`/`POST /hubs/sections/1/manage?metadataItemId=`), Manage Recommendations' ticks
(`PUT manage/{id}`), Remove Hub (`DELETE manage/{id}`), a drag (`PUT manage/{id}/move?after=`),
Reset Customization (`DELETE manage`), and between the steps the library's Recommended page
(`/hubs/sections/1`), Home (`/hubs/promoted`, `/hubs`) and the list itself.

This replays every exchange in order against rex and compares what is the list's: each managed
row's flags and visibilities, the statuses, the collection shelf's fields and members, and where
the shelf sits among the hubs both servers serve. Then the list survives a restart under --state.

    python3 tests/test_collection_hub_pins.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from server_command import server_command
from startup_wait import poll_until
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "collection_hub_pins_s1435.json")))["exchanges"]
TOKEN = "pins-token"
MACHINE = "57e41332f5b99adb07cfdf57505cfdc563327e45"
MOVIES = [("1", "Big Buck Bunny", "2008-04-10"), ("2", "Elephants Dream", "2006-03-24"),
          ("3", "Sintel", "2010-09-27"), ("4", "Tears of Steel", "2012-09-26")]
COLLECTIONS = [("15", "Blender Films", "1,2,3"), ("16", "Open Movies", "3,4")]

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:900]))


def catalog():
    items = [{"librarySectionID": 1, "librarySectionKey": "/library/sections/1", "librarySectionTitle": "Movies",
              "ratingKey": key, "key": "/library/metadata/" + key, "type": "movie", "title": title,
              "year": int(date[:4]), "originallyAvailableAt": date, "guid": "plex://movie/%024d" % int(key),
              "addedAt": 1793296900 + int(key)} for key, title, date in MOVIES]
    items.append({"librarySectionID": 2, "librarySectionKey": "/library/sections/2", "librarySectionTitle": "TV Shows",
                  "ratingKey": "6", "key": "/library/metadata/6/children", "type": "show", "title": "Firefly",
                  "year": 2002, "guid": "plex://show/%024d" % 6, "addedAt": 1793296906})
    return {"machineIdentifier": MACHINE, "friendlyName": "pins",
            "sections": [
                {"key": "1", "type": "movie", "title": "Movies", "agent": "tv.plex.agents.movie",
                 "scanner": "Plex Movie", "language": "en-US", "Location": [{"id": 1, "path": "/data/movies"}]},
                {"key": "2", "type": "show", "title": "TV Shows", "agent": "tv.plex.agents.series",
                 "scanner": "Plex TV Series", "language": "en-US", "Location": [{"id": 2, "path": "/data/shows"}]}],
            "items": items}


def ask(port, method, path, query=(), accept="application/json"):
    pairs = [(k, v) for k, v in query if k != "X-Plex-Token"] + [("X-Plex-Token", TOKEN)]
    url = "http://127.0.0.1:%d%s?%s" % (port, path, urllib.parse.urlencode(pairs))
    request = urllib.request.Request(url, method=method, headers={"Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, ConnectionError):
        return 0, ""


def container(body):
    try:
        return json.loads(body).get("MediaContainer", {})
    except ValueError:
        return {}


def stem(identifier):
    """`movie.genre.1.6` -> `movie.genre`; Home's Recently Added under either of its names."""
    identifier = re.sub(r"(\.\d+)+$", "", identifier or "")
    return "recentlyadded" if identifier in ("movie.recentlyadded", "home.movies.recent") else identifier


class Server:
    def __init__(self, work, state=None):
        self.port = free_port()
        args = ["--port", self.port, "--catalog", os.path.join(work, "catalog.json"), "--gdm", "off",
                "--token", TOKEN, "--assert-claimed"]
        if state:
            args += ["--state", state]
        env = dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:9")
        for name in ("REX_TOKEN", "PLEX_TOKEN", "REX_STATE"):
            env.pop(name, None)
        self.log = open(os.path.join(work, "server.log"), "ab")
        self.proc = subprocess.Popen(server_command(*args, root=ROOT), cwd=ROOT, env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        poll_until(lambda: ask(self.port, "GET", "/library/sections")[0] == 200, 40)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=30)
        self.log.close()


def make_collections(port):
    """The reference's collections, made as it made them; reference id -> rex's."""
    ids = {}
    for reference, title, members in COLLECTIONS:
        uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (MACHINE, members)
        status, body = ask(port, "POST", "/library/collections",
                           [("type", "1"), ("title", title), ("smart", "0"), ("sectionId", "1"), ("uri", uri)])
        made = (container(body).get("Metadata") or [{}])[0]
        check("collection %r made" % title, status == 200 and made.get("ratingKey"), (status, body[:200]))
        ids[reference] = str(made.get("ratingKey"))
    return ids


def mapper(ids):
    def apply(text):
        for reference, mine in ids.items():
            text = re.sub(r"(custom\.collection\.\d+\.)%s\b" % reference, r"\g<1>%s" % mine, text)
            text = re.sub(r"(custom\.collection\.\d+\.%s\.)%s\b" % (mine, reference), r"\g<1>%s" % mine, text)
            text = text.replace("/library/collections/%s/" % reference, "/library/collections/%s/" % mine)
        return text
    return apply


def managed_rows(body, xml=False):
    if xml:
        return [dict(re.findall(r'(\w+)="([^"]*)"', row)) for row in re.findall(r"<Hub ([^>]*?)/?>", body or "")]
    rows = container(body).get("Hub") or []
    return [{k: (("1" if v else "0") if isinstance(v, bool) else str(v)) for k, v in row.items()} for row in rows]


def shelf_fields(hub):
    return {k: hub.get(k) for k in ("hubIdentifier", "title", "type", "context", "style", "key", "promoted",
                                    "size", "more", "hubKey")}


def compare_served(label, reference_body, my_body, apply):
    theirs = [h for h in container(reference_body).get("Hub") or []]
    mine = [h for h in container(my_body).get("Hub") or []]
    their_stems = [stem(apply(h.get("hubIdentifier"))) for h in theirs]
    my_stems = [stem(h.get("hubIdentifier")) for h in mine]
    common = set(their_stems) & set(my_stems)
    check(label + ": the collection shelf sits where the reference's does",
          [s for s in their_stems if s in common] == [s for s in my_stems if s in common]
          and sum(s.startswith("custom.collection") for s in their_stems)
          == sum(s.startswith("custom.collection") for s in my_stems),
          ([s for s in their_stems if s in common], [s for s in my_stems if s in common]))
    for hub in theirs:
        if not str(hub.get("hubIdentifier")).startswith("custom.collection"):
            continue
        want = {k: (apply(v) if isinstance(v, str) else v) for k, v in shelf_fields(hub).items()}
        got = next((shelf_fields(h) for h in mine if h.get("hubIdentifier") == want["hubIdentifier"]), None)
        check(label + ": %s carries the reference's fields" % want["hubIdentifier"], got == want, (got, want))
        rows = next((h.get("Metadata") for h in mine if h.get("hubIdentifier") == want["hubIdentifier"]), None) or []
        check(label + ": %s lists the members in the reference's order" % want["hubIdentifier"],
              [str(r.get("ratingKey")) for r in rows] == [str(r.get("ratingKey")) for r in hub.get("Metadata") or []],
              ([r.get("ratingKey") for r in rows], [r.get("ratingKey") for r in hub.get("Metadata") or []]))


def replay(port, ids):
    apply = mapper(ids)
    for exchange in FIXTURE:
        method, path = exchange["method"], apply(exchange["path"])
        query = [(k, apply(v)) for k, v in exchange["query"] if not k.startswith("X-Plex-")]
        if exchange["status"] is None:
            continue
        # The reference's own collection ids appear in metadataItemId.
        query = [(k, ids.get(v, v) if k == "metadataItemId" else v) for k, v in query]
        xml = "xml" in (exchange.get("accept") or "")
        status, body = ask(port, method, path, query, "application/xml" if xml else "application/json")
        label = "#%d %s %s %s %s" % (exchange["index"], exchange["step"], method, path,
                                     urllib.parse.urlencode([q for q in query if q[0] != "count"])[:90])
        check(label + ": %s as the reference's" % exchange["status"], status == exchange["status"], (status, body[:300]))
        reference = apply(exchange["res_body"] or "")
        if status != 200 or exchange["status"] != 200:
            continue
        if "/manage" in path and (method != "PUT" or path.endswith("/move")) and method != "DELETE":
            theirs, mine = managed_rows(reference, xml), managed_rows(body, xml)
            check(label + ": the list's rows as the reference's", mine == theirs, (mine, theirs))
            if xml:
                rows = re.findall(r"<Hub ([^>]*?)/?>", body)
                check(label + ": XML rows in the reference's attribute order",
                      [re.findall(r'(\w+)="', r) for r in rows]
                      == [re.findall(r'(\w+)="', r) for r in re.findall(r"<Hub ([^>]*?)/?>", reference)], rows[:2])
        elif method == "GET" and (path.startswith("/hubs/sections/") or path in ("/hubs/promoted", "/hubs")):
            if not exchange["owner_plex_pass"]:
                # The same PMS owned by an account without Plex Pass served no collection
                # shelf at all; rex claims Plex Pass, so only the Plex Pass owner's pages count.
                continue
            compare_served(label, reference, body, apply)
        elif method in ("PUT", "DELETE"):
            check(label + ": an empty answer", body == "", body[:100])


def main():
    work = tempfile.mkdtemp(prefix="rex-pins-")
    json.dump(catalog(), open(os.path.join(work, "catalog.json"), "w"))
    server = Server(work)
    try:
        ids = make_collections(server.port)
        replay(server.port, ids)
    finally:
        server.stop()

    # The list is the server's: a restart under --state keeps the order and the ticks.
    state = os.path.join(work, "state.db")
    server = Server(work, state)
    try:
        ids = make_collections(server.port)
        mine = ids["15"]
        ask(server.port, "POST", "/hubs/sections/1/manage", [("metadataItemId", mine), ("promotedToRecommended", "1"),
                                                             ("promotedToOwnHome", "1"), ("promotedToSharedHome", "0")])
        ask(server.port, "PUT", "/hubs/sections/1/manage/custom.collection.1.%s/move" % mine,
            [("after", "movie.recentlyadded")])
        before = managed_rows(ask(server.port, "GET", "/hubs/sections/1/manage")[1])
    finally:
        server.stop()
    server = Server(work, state)
    try:
        after = managed_rows(ask(server.port, "GET", "/hubs/sections/1/manage")[1])
        check("Manage Recommendations is kept across a restart", after == before and any(
            r["identifier"] == "custom.collection.1.%s" % mine and r["promotedToOwnHome"] == "1" for r in after)
              and [r["identifier"] for r in after][2] == "custom.collection.1.%s" % mine, (after, before))
        promoted = container(ask(server.port, "GET", "/hubs/promoted", [("contentDirectoryID", "1")])[1])
        check("a restarted server still serves the pinned collection on Home",
              any(str(h.get("hubIdentifier")).startswith("custom.collection.1.%s." % mine)
                  for h in promoted.get("Hub") or []), [h.get("hubIdentifier") for h in promoted.get("Hub") or []])
    finally:
        server.stop()

    print("%d/%d passed" % (sum(results), len(results)))
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
