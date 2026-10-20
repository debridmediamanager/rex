#!/usr/bin/env python3
"""The leftovers: rules cycle 83's agents measured beside their own and did not fix,
re-measured on a real PMS and checked here against the measured values.

- container paging over its two transports: a lone header or query parameter never
  pages, and header and query merge PER PARAMETER, the header winning
  (`tests/fixtures/paging_grammar.json`, plexlab);
- the grammar of the two paging values: a Size is parsed whenever sent, a lone one
  included, a Start only beside a Size, as a 32-bit signed integer (the same fixture);
- `/folder?parent=` that is not all digits (`1,2`, `-1`, `+7`) is the top folder, an empty
  or unknown numeric one the empty list, and a REPEATED `parent` takes its last value (plexlab);
- `/library/all` reads a repeated parameter's LAST value where the section grid ANDs it
  (plexlab and the 1.42.2 reference);
- an EMPTY collection carries no `childCount` on any row
  (`tests/fixtures/collection_empty_childcount.json`, plexscratch42);
- a CLIP's `/allLeaves` and `/grandchildren` are 404s beside `/children`'s 400 (plexlab's clip
  5; driven on `catalog_features.json`'s trailer 201);
- a poster upload is served `image/jpeg` whatever its bytes, and the transcoder still refuses
  one that is no image (`tests/fixtures/upload_non_image.json`, plexscratch42);
- `DELETE /library/metadata/{a},{b}` is a 404 that deletes nothing
  (`tests/fixtures/delete_key_list.json`, plexscratch42);

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_parity_leftovers.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import hashlib
import struct
import time
import urllib.parse
import zlib

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FEATURES = os.path.join("tests", "catalog_features.json")
FIXTURES = os.path.join(HERE, "fixtures")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-leftovers",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome"}
# The fixture's reference ids against `catalog_ui.json`'s.
SHOW = {"kids": "6", "ui": "20001"}

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:900]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def exchange(port, method, path, headers=None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = dict(HEADERS, Connection="close")
    sent.update(headers or {})
    try:
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    return response.status, dict(response.getheaders()), raw


def request(port, method, path, headers=None, body=None):
    status, _, raw = exchange(port, method, path, headers, body)
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw


def mc(port, path, method="GET", headers=None):
    status, body = request(port, method, path, headers)
    return (body or {}).get("MediaContainer", {}) if isinstance(body, dict) else {"_status": status}


def start(catalog=CATALOG):
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", catalog, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            if request(port, "GET", "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def paging_cells(port, cells):
    """Replay the recorded paging cells; the mismatches per family."""
    wrong = {}
    for cell in cells:
        path = cell["family"].replace("{show}", SHOW["ui"])
        if cell["query"]:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(
                [tuple(pair) for pair in cell["query"]], quote_via=urllib.parse.quote)
        status, body = request(port, "GET", path, headers=dict(cell["headers"]))
        want = cell["answer"]
        got = {"status": status}
        if status == 200 and isinstance(body, dict):
            body = body.get("MediaContainer", {})
            got.update({"offset": body.get("offset"), "paged": "totalSize" in body})
        expected = {"status": want["status"]}
        if want["status"] == 200:
            expected.update({"offset": want["offset"], "paged": want["totalSize"] is not None})
        if got != expected:
            wrong.setdefault(cell["family"], []).append((cell["case"], expected, got))
    return wrong


def run(port):
    with open(os.path.join(FIXTURES, "paging_grammar.json")) as handle:
        fixture = json.load(handle)
    transport = [cell for cell in fixture["cells"] if "'" not in cell["case"]]
    grammar = [cell for cell in fixture["cells"] if "'" in cell["case"]]

    # 1. The transports: a lone header or query parameter never pages; the header wins
    #    per parameter over the query, so a header Size beside a query Start pages.
    wrong = paging_cells(port, transport)
    for family in fixture["families"]:
        check("paging transport on %s follows the measured header/query merge" % family,
              family not in wrong, wrong.get(family))

    # 2. The grammar: a lone Size is parsed (`abc` is the 500), a lone Start is not, and
    #    both are 32-bit signed integers with no space, point, exponent or underscore.
    wrong = paging_cells(port, grammar)
    for family in fixture["families"]:
        check("paging values on %s follow the measured int32 grammar" % family,
              family not in wrong, wrong.get(family))

    # 3. `/folder?parent=`: plexlab answers `1,2` with the library's top folder (the earlier
    #    "empty 200" was a numeric parent naming no directory), and the last `parent` wins.
    top = [row.get("key") for row in mc(port, "/library/sections/2/folder").get("Metadata") or []]
    answers = {}
    for query in ("parent=1,2", "parent=1%2C2", "parent=-1", "parent=%2B7", "parent=",
                  "parent=99999", "parent=abc&parent=99999", "parent=99999&parent=abc"):
        body = mc(port, "/library/sections/2/folder?" + query)
        answers[query] = (body.get("_status", 200),
                          [row.get("key") for row in body.get("Metadata") or []])
    expected = {query: (200, top) for query in
                ("parent=1,2", "parent=1%2C2", "parent=-1", "parent=%2B7", "parent=99999&parent=abc")}
    expected.update({query: (200, []) for query in
                     ("parent=", "parent=99999", "parent=abc&parent=99999")})
    check("a non-digit folder parent is the top folder and the last repeated parent wins",
          top and answers == expected, {"top": top, "answers": answers})

    # 4. `/library/all` takes a repeated key's LAST value; the section grid ANDs it.
    trust = urllib.parse.quote("plex://movie/614ee97b618cd92600cef3e1", safe="")
    tuesday = urllib.parse.quote("plex://movie/60a69a74ba9733002ddf78f6", safe="")

    def keys(path):
        body = mc(port, path)
        return body.get("_status", 200), [row.get("ratingKey") for row in body.get("Metadata") or []]
    movies = keys("/library/sections/1/all")[1]
    answers = {query: keys("/library/all?" + query) for query in (
        "type=1&guid=%s&guid=%s" % (trust, tuesday), "type=1&guid=%s&guid=%s" % (tuesday, trust),
        "guid=%s&guid=%s" % (trust, tuesday), "type=1&year=2022&year=2024",
        "year=2024&type=1&year=2022", "type=2&type=1", "type=abc&type=1")}
    answers["type=1&type=abc"] = (mc(port, "/library/all?type=1&type=abc").get("_status"), [])
    answers["grid"] = keys("/library/sections/1/all?guid=%s&guid=%s" % (trust, tuesday))
    expected = {
        "type=1&guid=%s&guid=%s" % (trust, tuesday): (200, ["1013"]),
        "type=1&guid=%s&guid=%s" % (tuesday, trust): (200, ["1012"]),
        "guid=%s&guid=%s" % (trust, tuesday): (200, ["1013"]),
        "type=1&year=2022&year=2024": (200, ["1013"]),
        "year=2024&type=1&year=2022": (200, ["1012"]),
        "type=2&type=1": (200, movies),
        "type=abc&type=1": (200, movies),
        "type=1&type=abc": (400, []),
        "grid": (200, []),
    }
    check("/library/all takes a repeated key's last value and the grid ANDs it",
          len(movies) == 6 and answers == expected, answers)

    # 5. An empty collection: no `childCount` on any of its rows; a member's collection
    #    keeps its count. Built as the fixture's create, over a show's key at type 4.
    with open(os.path.join(FIXTURES, "collection_empty_childcount.json")) as handle:
        recorded = json.load(handle)
    machine = mc(port, "/identity").get("machineIdentifier")

    def create(title, kind, section, key):
        uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, key)
        body = mc(port, "/library/collections?type=%s&title=%s&smart=0&sectionId=%s&uri=%s"
                  % (kind, title, section, urllib.parse.quote(uri, safe="")), method="POST")
        row = (body.get("Metadata") or [{}])[0]
        return row.get("ratingKey"), "childCount" in row
    empty, created_count = create("LB83Empty", 4, 2, SHOW["ui"])
    full, _ = create("LB83One", 1, 1, "1007")
    seen = {"create": created_count}
    for path in recorded["childCount_absent_on"][:-1]:
        path = path.replace("742", str(empty))
        rows = [row for row in mc(port, path).get("Metadata") or [] if row.get("ratingKey") == empty]
        seen[path] = [("childCount" in row, row.get("childCount")) for row in rows]
    member = [row.get("childCount") for row in mc(port, "/library/sections/1/collections")
              .get("Metadata") or [] if row.get("ratingKey") == full]
    check("an empty collection carries no childCount on any row; a member's collection 1",
          empty and not created_count and member == [1]
          and all(value == [(False, None)] for name, value in seen.items() if name != "create"),
          {"empty": empty, "seen": seen, "member": member})
    for key in (empty, full):
        request(port, "DELETE", "/library/collections/%s" % key)

    # 7. Uploads: served `image/jpeg` whatever the bytes; the transcoder decodes them.
    with open(os.path.join(FIXTURES, "upload_non_image.json")) as handle:
        recorded = json.load(handle)
    text = recorded["body"].encode("ascii")

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\0\0\0\0")) + chunk(b"IEND", b""))
    seen = {}
    for key, body in (("1007", text), ("1012", png)):
        status, _, _ = exchange(port, "POST", "/library/metadata/%s/posters" % key, body=body,
                                headers={"Content-Type": "application/octet-stream"})
        thumb = (mc(port, "/library/metadata/%s" % key).get("Metadata") or [{}])[0].get("thumb")
        candidate = "/library/metadata/%s/file?url=%s" % (
            key, urllib.parse.quote("upload://posters/" + hashlib.sha1(body).hexdigest(), safe=""))
        row = {"upload": status}
        for name, path in (("direct", thumb), ("candidate", candidate),
                           ("transcode", "/photo/:/transcode?width=100&height=100&url="
                            + urllib.parse.quote(thumb or "", safe="")),
                           ("ultrablur", "/services/ultrablur/colors?url="
                            + urllib.parse.quote(thumb or "", safe=""))):
            got, headers, raw = exchange(port, "GET", path, headers={"Accept": "*/*"})
            row[name] = (got, headers.get("Content-Type") if name != "ultrablur" else None,
                         len(raw) if name in ("direct", "candidate") else None)
        seen[key] = row
    want = recorded["answers"]
    expected = {
        "1007": {"upload": want["upload"], "direct": (200, "image/jpeg", len(text)),
                 "candidate": (200, "image/jpeg", len(text)),
                 "transcode": (want["transcode"][0], want["transcode"][1], None),
                 "ultrablur": (want["ultrablur"], None, None)},
        "1012": {"upload": 200, "direct": (200, "image/jpeg", len(png)),
                 "candidate": (200, "image/jpeg", len(png)),
                 "transcode": (200, "image/jpeg", None), "ultrablur": (200, None, None)},
    }
    check("an upload is served image/jpeg whatever its bytes; the transcoder refuses a non-image",
          seen == expected, seen)
    # Cycle 17: `/file?url=` serves one of THIS row's candidates and is a 404 otherwise. The
    # catalog row's route served any row's upload.
    other = "/library/metadata/1008/file?url=%s" % urllib.parse.quote(
        "upload://posters/" + hashlib.sha1(png).hexdigest(), safe="")
    status = exchange(port, "GET", other, headers={"Accept": "*/*"})[0]
    check("a row's /file?url= of another row's upload is a 404", status == 404, status)

    # 8. A key-list DELETE: the 404, neither row removed; the key-list GET still lists both.
    with open(os.path.join(FIXTURES, "delete_key_list.json")) as handle:
        recorded = {cell["request"]: cell for cell in json.load(handle)["cells"]}
    want = recorded["DELETE /library/metadata/743,744"]
    status, headers, raw = exchange(port, "DELETE", "/library/metadata/1008,1009")
    after = [row.get("ratingKey") for row in mc(port, "/library/metadata/1008,1009")
             .get("Metadata") or []]
    check("a key-list DELETE is the 404 and deletes nothing",
          (status, len(raw), after) == (want["status"], want["length"], ["1008", "1009"]),
          (status, len(raw), after))


def run_features(port):
    # 6. A clip (plexlab's 5, a movie's trailer): `/children` the 89-byte 400, `/allLeaves` and
    #    `/grandchildren` the 85-byte 404, paged or not. A movie's stay empty 200s.
    paging = "X-Plex-Container-Start=0&X-Plex-Container-Size=5"
    seen = {}
    for key in ("201", "101"):
        for leaf in ("children", "allLeaves", "grandchildren"):
            for suffix in ("", "?" + paging):
                status, _, raw = exchange(port, "GET", "/library/metadata/%s/%s%s" % (key, leaf, suffix))
                seen[(key, leaf, bool(suffix))] = (status, len(raw) if status >= 400 else None)
    expected = {}
    for paged in (False, True):
        expected[("201", "children", paged)] = (400, 89)
        expected[("201", "allLeaves", paged)] = (404, 85)
        expected[("201", "grandchildren", paged)] = (404, 85)
        expected[("101", "children", paged)] = (400, 89)
        expected[("101", "allLeaves", paged)] = (200, None)
        expected[("101", "grandchildren", paged)] = (200, None)
    types = [mc(port, "/library/metadata/%s" % key).get("Metadata", [{}])[0].get("type")
             for key in ("201", "101")]
    check("a clip's allLeaves and grandchildren are 404s, a movie's empty 200s",
          types == ["clip", "movie"] and seen == expected,
          {"types": types, "wrong": {k: v for k, v in seen.items() if expected.get(k) != v}})


def main():
    process, port = start()
    try:
        run(port)
    finally:
        stop(process)
    process, port = start(FEATURES)
    try:
        run_features(port)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d leftover parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
