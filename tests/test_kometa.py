#!/usr/bin/env python3
"""Kometa's collections and collection posters, answered as a real PMS answers them.

`fixtures/kometa_collections_s1435.json` is Kometa 2.5.3 run at a real, online PMS 1.43.5
(harness clips: Big Buck Bunny 1, Sintel 3, Firefly 6). Kometa makes a static collection
with nothing but a tag write (`PUT /library/sections/{s}/all?collection[0].tag.tag=`), then
looks the collection up by title (`/all?type=18&title=`) and gives up when it is not
there. That PMS made a collection item from the write; the offline copies rex had measured
before (plexscratch, plexscratch42) made only the tag. It sets a collection's poster by
`POST .../posters?url=` with no body: the PMS fetched the url and kept it as an upload,
and refused an unreachable or loopback url with 502 and a page that is no image with 406.

This replays Kometa's own requests in order against rex and compares what is per
collection. The url poster's 200 and 406 fetch the recorded GitHub urls and skip offline.

    python3 tests/test_kometa.py
"""
import json
import os
import re
import socket
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
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "kometa_collections_s1435.json")))["exchanges"]
TOKEN = "kometa-token"
# The titles Kometa made by tag write; its smart collections and separators are POSTs not replayed.
TAGGED = ("Blender Films", "TMDb Sintel", "Space Westerns")
MOVIES = [("1", "Big Buck Bunny", 2008), ("2", "Elephants Dream", 2006), ("3", "Sintel", 2010),
          ("4", "Tears of Steel", 2012)]

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def catalog():
    def section(key, kind):
        return {"librarySectionID": int(key), "librarySectionKey": "/library/sections/" + key,
                "librarySectionTitle": "Movies" if kind == "movie" else "TV Shows"}
    items = [dict(section("1", "movie"), ratingKey=key, key="/library/metadata/" + key, type="movie", title=title,
                  year=year, guid="plex://movie/%024d" % int(key), addedAt=1792789000 + int(key))
             for key, title, year in MOVIES]
    items.append(dict(section("2", "show"), ratingKey="6", key="/library/metadata/6/children", type="show",
                      title="Firefly", year=2002, guid="plex://show/%024d" % 6, addedAt=1792789006))
    return {"machineIdentifier": "78d14afa2dc0be73d1e03434a4a71a331ede0631", "friendlyName": "pmskometa",
            "sections": [
                {"key": "1", "type": "movie", "title": "Movies", "agent": "tv.plex.agents.movie",
                 "scanner": "Plex Movie", "language": "en-US", "Location": [{"id": 1, "path": "/data/movies"}]},
                {"key": "2", "type": "show", "title": "TV Shows", "agent": "tv.plex.agents.series",
                 "scanner": "Plex TV Series", "language": "en-US", "Location": [{"id": 2, "path": "/data/shows"}]}],
            "items": items}


def ask(port, method, path, query=()):
    pairs = [(k, v) for k, v in query if k != "X-Plex-Token"] + [("X-Plex-Token", TOKEN)]
    url = "http://127.0.0.1:%d%s?%s" % (port, path, urllib.parse.urlencode(pairs))
    request = urllib.request.Request(url, method=method, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw[:1] == b"{" else None
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, ConnectionError):
        return 0, None


def directories(body):
    """A recorded XML answer's collection rows."""
    return [dict(re.findall(r'(\w+)="([^"]*)"', row)) for row in re.findall(r"<Directory [^>]*>", body or "")]


def reachable(host):
    try:
        socket.create_connection((host, 443), timeout=5).close()
        return True
    except OSError:
        return False


def main():
    work = tempfile.mkdtemp(prefix="rex-kometa-")
    json.dump(catalog(), open(os.path.join(work, "catalog.json"), "w"))
    port = free_port()
    env = dict(os.environ, REX_PLEXTV_URL="http://127.0.0.1:9")
    for name in ("REX_TOKEN", "PLEX_TOKEN", "REX_STATE"):
        env.pop(name, None)
    log = open(os.path.join(work, "server.log"), "ab")
    proc = subprocess.Popen(server_command("--port", port, "--catalog", os.path.join(work, "catalog.json"),
                                           "--gdm", "off", "--token", TOKEN, "--assert-claimed", root=ROOT),
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    ids = {}  # the reference's collection ratingKey -> rex's, matched by title
    try:
        poll_until(lambda: ask(port, "GET", "/library/sections")[0] == 200, 40)
        for exchange in FIXTURE:
            method, path, query = exchange["method"], exchange["path"], [tuple(p) for p in exchange["query"]]
            named = dict(query)
            title = named.get("title") or named.get("collection[0].tag.tag")
            label = "#%d %s %s %s" % (exchange["index"], method, path, title or "")
            if method == "PUT" and "collection[0].tag.tag" in named:
                status, _ = ask(port, method, path, query)
                check(label + ": status as the reference's", status == exchange["status"], status)
            elif method == "GET" and named.get("type") == "18" and title in TAGGED:
                status, document = ask(port, method, path, query)
                theirs = directories(exchange["body"] if "body" in exchange else exchange["res_body"])
                mine = (document or {}).get("MediaContainer", {}).get("Metadata", [])
                check(label + ": found as the reference found it", [m.get("title") for m in mine]
                      == [t["title"] for t in theirs], ([m.get("title") for m in mine], [t["title"] for t in theirs]))
                for want, got in zip(theirs, mine):
                    ids[want["ratingKey"]] = str(got.get("ratingKey"))
                    for field in ("type", "subtype", "childCount"):
                        check(label + ": %s as the reference's" % field,
                              str(got.get(field)) == want.get(field), (got.get(field), want.get(field)))
                    check(label + ": a collection:// guid", str(got.get("guid", "")).startswith("collection://"),
                          got.get("guid"))
            elif method == "GET" and path.endswith("/children") and path.split("/")[3] in ids:
                status, document = ask(port, method, path.replace(path.split("/")[3], ids[path.split("/")[3]]), query)
                theirs = sorted(re.findall(r'<Video ratingKey="(\d+)"', exchange["res_body"]))
                mine = sorted(str(m.get("ratingKey")) for m in (document or {}).get("MediaContainer", {}).get("Metadata", []))
                check(label + ": the members the reference lists", mine == theirs, (mine, theirs))
        check("every tag-written collection was found", sorted(ids) and len(ids) == len(TAGGED), ids)

        # Posters by url, on a collection Kometa made: the reference's answers, its 69th and 864-866th.
        target = "/library/metadata/%s/posters" % next(iter(ids.values()), "0")
        github = reachable("raw.githubusercontent.com")
        for exchange in FIXTURE:
            if exchange["method"] != "POST" or not exchange["path"].endswith("/posters"):
                continue
            if exchange["index"] not in (69, 864, 865, 866):
                continue
            url = dict(exchange["query"])["url"]
            label = "#%d POST posters?url=%s" % (exchange["index"], url[:60])
            if "githubusercontent" in url and not github:
                print("SKIP  %s: raw.githubusercontent.com unreachable" % label)
                continue
            status, _ = ask(port, "POST", target, [("url", url)])
            check(label + ": %d as the reference's" % exchange["status"], status == exchange["status"], status)
            if exchange["status"] == 200:
                _status, document = ask(port, "GET", target)
                photos = (document or {}).get("MediaContainer", {}).get("Metadata", [])
                picked = [p for p in photos if p.get("selected")]
                check(label + ": the fetched image is an upload://posters candidate, selected",
                      len(picked) == 1 and str(picked[0].get("ratingKey", "")).startswith("upload://posters/"), photos)
    finally:
        proc.terminate()
        proc.wait(timeout=20)
        log.close()
    print("%d/%d passed" % (sum(results), len(results)))
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
