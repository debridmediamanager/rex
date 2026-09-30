#!/usr/bin/env python3
"""The leftovers: rules cycle 83's agents measured beside their own and did not fix,
re-measured on a real PMS and checked here against the measured values.

- container paging over its two transports: a lone header or query parameter never
  pages, and header and query merge PER PARAMETER, the header winning
  (`tests/fixtures/paging_grammar.json`, kids);
- the grammar of the two paging values: a Size is parsed whenever sent, a lone one
  included, a Start only beside a Size, as a 32-bit signed integer (the same fixture);
- `/folder?parent=` that is not all digits (`1,2`, `-1`, `+7`) is the top folder, an empty
  or unknown numeric one the empty list, and a REPEATED `parent` takes its last value (kids);

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_leftovers.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FEATURES = os.path.join("tests", "catalog_features.json")
FIXTURES = os.path.join(HERE, "fixtures")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
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

    # 3. `/folder?parent=`: kids answers `1,2` with the library's top folder (the handover's
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


def main():
    process, port = start()
    try:
        run(port)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d leftover parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
