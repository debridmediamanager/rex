#!/usr/bin/env python3
"""rex answers the same with or without a `data/` beside it, black-box.

A sponsor ran the binary from `rust/target/release` with `--catalog rex.db` (2026-10-20): no
`data/` beside the catalog or in the working directory, so no `client_profiles.json`, and every
play on every client was a 400 from `/video/:/transcode/universal/decision`, logged as nothing
more than "rex: bad request". The sidecars are compiled into the binary now. This starts one
server from an empty directory holding only a copy of the catalog and one from the checkout,
replays the sponsor's own SHIELD request (`fixtures/decisions/shield_android_tv_decision.json`)
and the routes each sidecar answers, and wants the same bytes from both.

    python3 tests/test_sidecars_embedded.py
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET

from server_command import server_command
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join(HERE, "catalog_ui.json")
MOVIE = "1007"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- %r" % (detail,)))


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def start(port, catalog, cwd):
    log = tempfile.TemporaryFile()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", REX_PLEXTV_URL="http://127.0.0.1:9")
    for name in ("REX_TOKEN", "PLEX_TOKEN", "REX_STATE"):
        env.pop(name, None)
    proc = subprocess.Popen(server_command("--port", port, "--catalog", catalog, "--gdm", "off",
                                           "--assert-claimed", root=ROOT),
                            cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(150):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return proc, log
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
    log.seek(0)
    raise SystemExit("rex did not start:\n" + log.read().decode(errors="replace")[-3000:])


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def decision_path():
    with open(os.path.join(HERE, "fixtures", "decisions", "shield_android_tv_decision.json")) as handle:
        recorded = json.load(handle)
    query = [(key, "/library/metadata/%s" % MOVIE if key == "path" else value)
             for key, value in recorded["query"]]
    return recorded["path"] + "?" + urllib.parse.urlencode(query)


# One route at least for each sidecar the server reads at request time.
ROUTES = (
    decision_path(),                                    # client_profiles.json
    "/crossdomain.xml", "/clientaccesspolicy.xml", "/favicon.ico",
    "/:/prefs",                                         # server_prefs.json, regional_server_prefs.json
    "/:/prefs?X-Plex-Language=de",                      # translations.json
    "/library/sections/1/prefs",                        # section_prefs.json
    "/library/sections/1/prefs?X-Plex-Language=fr",
    "/library/metadata/%s/prefs" % MOVIE,               # item_prefs.json
    "/library/metadata/%s?X-Plex-Language=de" % MOVIE,  # stream_words.json, item_language_names.json
    "/library/sections/1/filters", "/library/sections/1/sorts",
    "/library/sections/2/filters",                      # section_meta*.json
    "/system/agents",                                   # system_agents.json, legacy_agents.json
    "/system/agents/tv.plex.agents.movie/config/1",
    "/system/agents/tv.plex.agents.movie/languages",    # agent_languages.json
    "/:/plugins/com.plexapp.system/prefs",              # plugin_prefs.json
    "/library/sections/1/all?X-Plex-Language=de",       # title_templates.json
)


def main():
    bare = tempfile.mkdtemp(prefix="rex-no-data-")
    try:
        catalog = os.path.join(bare, "catalog.json")
        shutil.copy(CATALOG, catalog)
        check("the bare directory has no data/", not os.path.exists(os.path.join(bare, "data")))
        ports = free_port(), free_port()
        alone, alone_log = start(ports[0], catalog, bare)
        beside, _ = start(ports[1], CATALOG, ROOT)
        try:
            status, body = get(ports[0], ROUTES[0])
            check("the SHIELD's decision is answered with no data/ beside rex", status == 200, status)
            check("the recorded SHIELD request permits direct play without an encoder",
                  ET.fromstring(body).get("mdeDecisionCode") == "1000", body[:300])
            for path in ROUTES:
                got, want = get(ports[0], path), get(ports[1], path)
                check("%s matches a server beside data/" % path.split("?")[0][:60], got == want,
                      (got[0], want[0], got[1][:120], want[1][:120]))
        finally:
            stop(alone)
            stop(beside)
    finally:
        shutil.rmtree(bare, ignore_errors=True)
    passed = sum(results)
    print("%d/%d checks passed" % (passed, len(results)))
    if passed != len(results):
        alone_log.seek(0)
        print(alone_log.read().decode(errors="replace")[-3000:])
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
