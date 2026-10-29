"""`POST /rex/import-watched`: an account's watch state read off its old Plex server and kept
here as that account's own.

The old server is a stub answering what plexscratch (PMS 1.43.4) answered rex's exact requests
(`fixtures/watch_import_s143.json`, `probes/record_watch_import.py`): a film part-watched, an
episode watched, one watched and started again, rated rows and a watched `local://` episode.
A third library of 1,001 episodes is paged as the recorded pages of two were (checked here),
so a page boundary is crossed twice. rex is claimed against a stub plex.tv listing plexlab's
shared user (`fixtures/plextv_access_tokens_shared_s1434.json`), and its rows carry the old
server's guids under other keys: rows are matched by guid, never by key.

    python3 tests/test_watch_import.py
"""
import copy
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

from server_command import server_command
from startup_wait import poll_until
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures")
RECORDED = json.load(open(os.path.join(FIXTURES, "watch_import_s143.json")))["exchanges"]
MACHINE = "15fad815c00463b33e2e644245763ee42ef84757"
OWNER = "DEVICE-TOKEN-1"
SHARED = "SHARED-TOKEN"
OLD_TOKEN = "old-server-token-0123456789"
RESTRICTED = json.load(open(os.path.join(FIXTURES, "plextv_access_tokens_shared_s1434.json")))["body"]
# The same share given every library and no filter; the recorded one (Movies, G and PG) last.
LIST = RESTRICTED.replace('filterMovies="contentRating=G%2CPG"', 'filterMovies=""').replace(
    '<section key="1" createdAt="2026-10-17 15:35:28 UTC"/>',
    '<section key="1" createdAt="2026-10-17 15:35:28 UTC"/>\n    <section key="2" createdAt="2026-10-17 15:35:28 UTC"/>')
BIG = 1001

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


def recorded(path, query):
    for exchange in RECORDED:
        if exchange["path"] == path and [tuple(pair) for pair in exchange["query"]] == list(query):
            return exchange
    return None


def full_listing(section):
    return recorded("/library/sections/%s/all" % section,
                    [("type", "1" if section == "1" else "4"), ("X-Plex-Container-Start", "0"),
                     ("X-Plex-Container-Size", "500")])["body"]


def big_listing():
    body = copy.deepcopy(full_listing("2"))
    template = body["MediaContainer"]["Metadata"][0]
    rows = []
    for index in range(BIG):
        row = {name: template[name] for name in ("type", "title", "duration") if name in template}
        row.update({"ratingKey": str(5000 + index), "guid": "plex://episode/big%021d" % index, "index": index + 1})
        if index == BIG - 1:
            row.update({"viewCount": 1, "lastViewedAt": 1793312968})
        rows.append(row)
    body["MediaContainer"]["Metadata"] = rows
    return body


def page(body, start, size):
    """A listing as PMS pages it: the recorded pages of two are this, checked in `main`."""
    out = copy.deepcopy(body)
    rows = out["MediaContainer"].get("Metadata", [])
    container = out["MediaContainer"]
    container["Metadata"] = rows[start:start + size]
    container.update({"size": len(container["Metadata"]), "totalSize": len(rows), "offset": start})
    if not container["Metadata"]:
        del container["Metadata"]
    return out


class OldServer(http.server.BaseHTTPRequestHandler):
    asked = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        path, _, query = self.path.partition("?")
        pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
        OldServer.asked.append((path, pairs, self.headers.get("X-Plex-Token"), self.headers.get("Accept")))
        if self.headers.get("X-Plex-Token") != OLD_TOKEN:
            return self.send(401, b"<html><head><title>Unauthorized</title></head></html>", "text/html")
        exchange = recorded(path, pairs)
        if path == "/library/sections":
            body = copy.deepcopy(exchange["body"])
            body["MediaContainer"]["Directory"] += [
                {"key": "3", "type": "show", "title": "Big"}, {"key": "4", "type": "artist", "title": "Music"}]
        elif exchange:
            body = exchange["body"]
        elif path in ("/library/sections/2/all", "/library/sections/3/all"):
            query = dict(pairs)
            listing = full_listing("2") if path.endswith("/2/all") else big_listing()
            body = page(listing, int(query["X-Plex-Container-Start"]), int(query["X-Plex-Container-Size"]))
        else:
            return self.send(404, b"", "text/html")
        self.send(200, json.dumps(body).encode(), "application/json")

    def send(self, status, raw, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class PlexTv(http.server.BaseHTTPRequestHandler):
    tokens = LIST

    def log_message(self, *args):
        pass

    def answer(self):
        path = self.path.split("?")[0]
        if path == "/plex.tv/api/v2/server/access_tokens":
            body = PlexTv.tokens
        elif path == "/servers.plex.tv/api/v2/user":
            body = '<?xml version="1.0" encoding="UTF-8"?>\n<user id="316687974" username="owner"/>\n'
        else:
            body = '<?xml version="1.0" encoding="UTF-8"?>\n<MediaContainer size="0"/>\n'
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_PUT = do_POST = answer


def section(key, kind, title):
    return {"librarySectionID": int(key), "librarySectionTitle": title, "librarySectionKey": "/library/sections/" + key}


def catalog():
    """rex's rows for the old server's titles: their guids under keys of rex's own (2000 + the
    old key). Season 2's episodes are other titles here, as the old `local://` ones are."""
    items = []
    for row in full_listing("1")["MediaContainer"]["Metadata"]:
        key = str(2000 + int(row["ratingKey"]))
        items.append(dict({"ratingKey": key, "key": "/library/metadata/" + key, "guid": row["guid"], "type": "movie",
                           "title": row["title"], "year": 2010, "duration": row["duration"], "addedAt": 1789187071},
                          **section("1", "movie", "Movies")))
    items.append(dict({"ratingKey": "2006", "key": "/library/metadata/2006/children", "guid": "plex://show/" + "6" * 24,
                       "type": "show", "title": "Firefly", "addedAt": 1789187257}, **section("2", "show", "TV Shows")))
    for season, index in (("2007", 1), ("2011", 2)):
        items.append(dict({"ratingKey": season, "key": "/library/metadata/%s/children" % season, "type": "season",
                           "title": "Season %d" % index, "index": index, "parentRatingKey": "2006",
                           "guid": "plex://season/" + season * 6, "addedAt": 1789187257}, **section("2", "show", "TV Shows")))
    episodes = [(row["ratingKey"], row["guid"]) for row in full_listing("2")["MediaContainer"]["Metadata"]]
    episodes = [(key, guid if not guid.startswith("local://") else "plex://episode/%024d" % int(key)) for key, guid in episodes]
    episodes.append(("1000", "plex://episode/big%021d" % (BIG - 1)))
    for number, (old, guid) in enumerate(episodes):
        key = str(2000 + int(old))
        season = "2007" if number < 3 else "2011"
        items.append(dict({"ratingKey": key, "key": "/library/metadata/" + key, "guid": guid, "type": "episode",
                           "title": "Episode %d" % (number + 1), "index": number + 1, "parentIndex": 1 if number < 3 else 2,
                           "parentRatingKey": season, "grandparentRatingKey": "2006", "grandparentTitle": "Firefly",
                           "duration": 10031, "addedAt": 1789187257}, **section("2", "show", "TV Shows")))
    return {"machineIdentifier": MACHINE, "friendlyName": "rex", "sections": [
        {"key": "1", "type": "movie", "title": "Movies", "agent": "tv.plex.agents.movie", "scanner": "Plex Movie",
         "language": "en-US", "uuid": "e46d000f-bcc4-4b5b-8a7b-1a9d721346ec", "Location": [{"id": 1, "path": "/data/movies"}]},
        {"key": "2", "type": "show", "title": "TV Shows", "agent": "tv.plex.agents.series", "scanner": "Plex TV Series",
         "language": "en-US", "uuid": "d962847e-2c7c-4d45-8355-e45e16287846", "Location": [{"id": 2, "path": "/data/shows"}]}],
        "items": items}


class Server:
    def __init__(self, work, plextv):
        self.port = free_port()
        env = dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:%d" % plextv)
        for name in ("REX_STATE", "REX_TOKEN", "PLEX_TOKEN"):
            env.pop(name, None)
        self.log = open(os.path.join(work, "server.log"), "ab")
        self.process = subprocess.Popen(
            server_command("--port", self.port, "--catalog", os.path.join(work, "catalog.json"),
                           "--state", os.path.join(work, "rex.db"), "--gdm", "off", root=ROOT),
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        poll_until(lambda: self.ask(OWNER, "GET", "/library/sections")[0] == 200
                   and self.ask(SHARED, "GET", "/library/sections")[0] == 200, 40)

    def ask(self, token, method, path, query=(), body=None):
        pairs = list(query) + [("X-Plex-Token", token)]
        url = "http://127.0.0.1:%d%s?%s" % (self.port, path, urllib.parse.urlencode(pairs))
        request = urllib.request.Request(url, method=method, data=body, headers={
            "Accept": "application/json", "X-Plex-Client-Identifier": "rex-import-probe"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw[:1] == b"{" else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, json.loads(raw) if raw[:1] == b"{" else None
        except (urllib.error.URLError, ConnectionError):
            return 0, None

    def import_from(self, token, address):
        return self.ask(token, "POST", "/rex/import-watched", body=address.encode())

    def row(self, token, key):
        status, document = self.ask(token, "GET", "/library/metadata/" + key)
        return (document or {}).get("MediaContainer", {}).get("Metadata", [{}])[0] if status == 200 else {}

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=20)
        self.log.close()


def state(row):
    return {name: row[name] for name in ("viewCount", "viewOffset", "lastViewedAt", "userRating", "lastRatedAt") if name in row}


def start(plextv, tokens):
    PlexTv.tokens = tokens
    work = tempfile.mkdtemp(prefix="rex-watch-import-")
    json.dump(catalog(), open(os.path.join(work, "catalog.json"), "w"))
    json.dump({"machine": MACHINE, "token": "SERVER-TOKEN-IMPORT", "owner": "owner", "email": "owner@example.com",
               "feature_uuids": [], "account_id": 316687974, "view_state_cursor": None, "access_tokens": []},
              open(os.path.join(work, "claim_state.json"), "w"))
    return work, Server(work, plextv.server_address[1])


def main():
    # The stub pages as PMS paged: every recorded page of two is the slice of the full answer.
    for exchange in RECORDED:
        query = dict(exchange["query"])
        if query.get("X-Plex-Container-Size") == "2":
            sliced = page(full_listing("2"), int(query["X-Plex-Container-Start"]), 2)
            check("stub pages as PMS: start %s" % query["X-Plex-Container-Start"], sliced == exchange["body"],
                  (sliced["MediaContainer"], exchange["body"]["MediaContainer"]))

    old = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), OldServer)
    plextv = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), PlexTv)
    for stub in (old, plextv):
        threading.Thread(target=stub.serve_forever, daemon=True).start()
    origin = "http://127.0.0.1:%d" % old.server_address[1]
    # What Plex Web's View XML opens: the item's address, its query and the token last.
    address = origin + "/library/metadata/18?checkFiles=1&includeAllConcerts=1&X-Plex-Token=" + OLD_TOKEN
    work, server = start(plextv, LIST)
    try:
        # Refusals, before anything is read.
        check("GET is refused", server.ask(SHARED, "GET", "/rex/import-watched")[0] == 405)
        check("no token of rex's is refused", server.ask("NOT-A-TOKEN", "POST", "/rex/import-watched",
                                                         body=address.encode())[0] == 401)
        status, answer = server.import_from(SHARED, "hello")
        check("a body that is no address is a 400 that says so", status == 400 and "http" in answer["error"], (status, answer))
        status, answer = server.import_from(SHARED, origin + "/library/metadata/18")
        check("an address without its token is a 400 that says so", status == 400 and "X-Plex-Token" in answer["error"],
              (status, answer))
        status, answer = server.import_from(SHARED, address.replace(OLD_TOKEN, "wrong"))
        check("a token the old server refuses is a 502 that says so", status == 502 and "refused the token" in answer["error"],
              (status, answer))
        status, answer = server.import_from(SHARED, "http://127.0.0.1:%d/?X-Plex-Token=x" % free_port())
        check("an old server that is not there is a 502", status == 502 and "could not reach" in answer["error"], (status, answer))
        check("nothing written by a refusal", state(server.row(SHARED, "2018")) == {}, server.row(SHARED, "2018"))

        # The owner already watched Big Buck Bunny here; the share has nothing yet.
        server.ask(OWNER, "GET", "/:/scrobble", [("key", "2001"), ("identifier", "com.plexapp.plugins.library")])
        owner_bunny = state(server.row(OWNER, "2001"))

        OldServer.asked = []
        status, answer = server.import_from(SHARED, address)
        expected = {"watched": 4, "inProgress": 1, "rated": 3, "alreadyHere": 0, "notInRex": 1}
        check("the share's import counts what it kept", status == 200 and answer == expected, (status, answer))
        asked = [(path, pairs) for path, pairs, _, _ in OldServer.asked]
        sent = [("/library/sections", [])] + [(e["path"], [tuple(p) for p in e["query"]]) for e in RECORDED[1:3]] + [
            ("/library/sections/3/all", [("type", "4"), ("X-Plex-Container-Start", str(start)), ("X-Plex-Container-Size", "500")])
            for start in (0, 500, 1000)]
        check("rex asks the recorded requests, every page, no music library", asked == sent, asked)
        check("every request carries the old token as a header and asks for JSON",
              all(token == OLD_TOKEN and accept == "application/json" for _, _, token, accept in OldServer.asked),
              OldServer.asked[:2])

        film = full_listing("1")["MediaContainer"]["Metadata"]
        show = full_listing("2")["MediaContainer"]["Metadata"]
        by_key = {row["ratingKey"]: row for row in film + show}
        for old_key, fields in (("1", ("viewOffset", "lastViewedAt")),
                                ("18", ("viewCount", "lastViewedAt", "userRating", "lastRatedAt")),
                                ("4", ("userRating", "lastRatedAt")), ("8", ("viewCount", "lastViewedAt")),
                                ("9", ("userRating", "lastRatedAt")), ("10", ("viewCount", "viewOffset", "lastViewedAt"))):
            mine = state(server.row(SHARED, str(2000 + int(old_key))))
            theirs = {name: by_key[old_key][name] for name in fields}
            check("share holds the old server's %s for %s" % ("/".join(fields), by_key[old_key]["title"]),
                  {name: float(mine.get(name, 0)) for name in fields} == {name: float(value) for name, value in theirs.items()}
                  and set(mine) == set(fields), (mine, theirs))
        check("the 1,001st episode, past two page ends, is watched", state(server.row(SHARED, "3000")).get("viewCount") == 1,
              server.row(SHARED, "3000"))
        check("an episode only the old server's local:// row was is left alone", state(server.row(SHARED, "2012")) == {},
              server.row(SHARED, "2012"))
        season = server.row(SHARED, "2007")
        check("the share's season counts its two watched episodes", season.get("viewedLeafCount") == 2, season)
        status, deck = server.ask(SHARED, "GET", "/hubs/home/continueWatching")
        listed = [str(row.get("ratingKey")) for row in (deck or {}).get("MediaContainer", {}).get("Metadata", [])]
        check("the share's Continue Watching lists the film it was part-way through", "2001" in listed, (status, listed))
        check("the owner's rows are untouched", state(server.row(OWNER, "2018")) == {} and state(server.row(OWNER, "2001")) == owner_bunny,
              (server.row(OWNER, "2018"), server.row(OWNER, "2001")))

        status, answer = server.import_from(SHARED, address)
        check("a second import changes nothing", status == 200 and answer == {
            "watched": 0, "inProgress": 0, "rated": 0, "alreadyHere": 5, "notInRex": 1}, (status, answer))

        server.stop()
        server = Server(work, plextv.server_address[1])
        check("the share's import survives a restart", state(server.row(SHARED, "2018")).get("viewCount") == 1,
              server.row(SHARED, "2018"))

        status, answer = server.import_from(OWNER, address)
        check("the owner's import keeps what the owner watched here", status == 200 and answer["alreadyHere"] == 1
              and state(server.row(OWNER, "2001")) == owner_bunny, (answer, server.row(OWNER, "2001")))
        check("the owner's import is the owner's", state(server.row(OWNER, "2018")).get("viewCount") == 1, server.row(OWNER, "2018"))
        check("and leaves the share's as it was", state(server.row(SHARED, "2001")).get("viewOffset") == 600000,
              server.row(SHARED, "2001"))

        # The share plexlab recorded sees Movies rated G or PG only, and none of these is.
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
        work, server = start(plextv, RESTRICTED)
        status, answer = server.import_from(SHARED, address)
        check("a share's import writes no row the share cannot see", status == 200 and answer["notInRex"] == 8
              and answer["watched"] == answer["rated"] == 0 and state(server.row(OWNER, "2018")) == {}, (status, answer))
    finally:
        server.stop()
        old.shutdown()
        plextv.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
