#!/usr/bin/env python3
"""Cycle 83, territory A: what the native server answered differently from the oracle in
A's localisation and item-settings sweeps, checked against the 1.42.2 reference's own answers.

- an item's `languageOverride` picker under `X-Plex-Language: zh-TW` and `es-419` is the
  translation table's list for that exact language (the 1.42.2 reference leads zh-TW with
  `tr-TR:土耳其文` and names `en-GB` `Inglés (R. U.)` in es-419); the native server cut the language to
  its base and drew `zh`'s simplified names and `es`'s list;
- `/query/parse` titles each Rule by the advertised Meta Field for (scope, field):
  `episode.addedAt` is "Date Episode Added", `hdr` "HDR", `show.unwatchedLeaves`
  "Unwatched Episodes"; the native server's hand table spelled the first wrong and left
  the other two empty.

Both expectations are the 1.42.2 reference's recorded exchanges
(`tests/fixtures/item_language_override_zen.json`,
`tests/fixtures/query_parse_field_titles_zen.json`). Drives a real server process
(`tests/server_command.py`).

    python3 tests/test_parity_cycle83_xa.py
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
FIXTURES = os.path.join(HERE, "fixtures")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"Accept": "application/json", "X-Plex-Client-Identifier": "parity-cycle83-xa",
           "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.147.1",
           "X-Plex-Platform": "Chrome"}
# The fixture catalog's movie and show, which carry the same item settings as the 1.42.2
# reference's.
ITEMS = ("1007", "20001")

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:600]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    sent = dict(HEADERS, Connection="close")
    sent.update(headers or {})
    try:
        connection.request("GET", path, headers=sent)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    try:
        return response.status, json.loads(raw) if raw else None
    except ValueError:
        return response.status, raw


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            break
        try:
            if request(port, "/identity")[0] == 200:
                return process, port
        except OSError:
            pass
        time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def language_override(port):
    with open(os.path.join(FIXTURES, "item_language_override_zen.json"), encoding="utf-8") as handle:
        measured = json.load(handle)["Setting"]
    for item in ITEMS:
        for language, row in measured.items():
            status, body = request(port, "/library/metadata/%s?includePreferences=1" % item,
                                   {"X-Plex-Language": language})
            settings = ((body or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0] \
                .get("Preferences", {}).get("Setting", []) if isinstance(body, dict) else []
            got = next((s for s in settings if s.get("id") == "languageOverride"), {})
            check("item %s languageOverride enumValues under %s is the 1.42.2 reference's" % (item, language),
                  status == 200 and got.get("enumValues") == row["enumValues"],
                  "%s %r" % (status, (got.get("enumValues") or "")[:160]))


def field_titles(port):
    with open(os.path.join(FIXTURES, "query_parse_field_titles_zen.json"), encoding="utf-8") as handle:
        exchanges = json.load(handle)["exchanges"]
    for exchange in exchanges:
        route, _, raw = exchange["path"].partition("?")
        # The recorded query is sent as the client sends it: `>>=` percent-encoded.
        path = route + "?" + "&".join(
            urllib.parse.quote(key, safe=".") + "=" + urllib.parse.quote(value, safe="")
            for key, value in urllib.parse.parse_qsl(raw, keep_blank_values=True))
        status, body = request(port, path, {"X-Plex-Language": "en"})
        rule = (body or {}).get("Query", {}).get("Rule", {}) if isinstance(body, dict) else {}
        rules = rule.get("Rule", [rule]) if rule.get("type") == "composite" else [rule]
        got = [{k: r[k] for k in ("type", "field", "fieldTitle") if k in r} for r in rules]
        check("query/parse fieldTitles %s" % exchange["path"][:90],
              status == exchange["status"] and got == exchange["Rule"],
              [(g, w) for g, w in zip(got, exchange["Rule"]) if g != w] or (status, len(got)))


def main():
    process, port = start()
    try:
        language_override(port)
        field_titles(port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d cycle 83 territory A parity checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
