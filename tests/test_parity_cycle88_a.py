#!/usr/bin/env python3
"""Cycle 88, territory A (localisation): a modern agent's `Language` rows in the locales
Plex Web ships that `agent_languages.json` did not hold.

`tests/fixtures/c88_a_agent_languages_zen.json` was read off zen (1.42.2) as XML, the only
form that carries every row, and kids (1.43.4) answers the same lists. Plex Web 4.162.1 ships
43 locales and sends its own as `X-Plex-Language`; the table held 29 of them, so in af, bg,
el, en-GB, es-419, fa, fr-CA, is, my, no, pt-BR, sl, sr, vi and zh-TW both servers answered
the NO-HEADER list -- every language named in itself, `en-US` preselected -- where a real PMS
names them in the client's language and preselects `bg-BG`, `pt-BR`, `vi-VN`... Plex Web's Add
Library and Fix Match language selects label each option with the row's `title` and preselect
the row marked `default`.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "c88_a_agent_languages_zen.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)

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


def start():
    port = free_port()
    process = subprocess.Popen(
        server_command("--port", port, "--catalog", CATALOG, "--assert-claimed",
                       "--gdm", "off", root=ROOT),
        cwd=ROOT, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if process.poll() is not None:
            raise SystemExit("server exited at start")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return process, port
        except OSError:
            time.sleep(0.1)
    process.kill()
    raise SystemExit("server did not start")


def ask(port, path, headers):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def rows(agent):
    return [[x.get("code"), x.get("title")] + (["1"] if x.get("default") == "1" else [])
            for x in agent.findall("Language")]


def main():
    fixture = json.load(open(FIXTURE, encoding="utf-8"))
    process, port = start()
    try:
        for language, documents in sorted(fixture["documents"].items()):
            headers = dict(fixture["request_headers"], **{"X-Plex-Language": language,
                                                          "X-Plex-Token": "plexsimtok"})
            wrong = []
            for media_type in ("1", "2", "8", None):
                path = "/system/agents" + ("?mediaType=" + media_type if media_type else "")
                status, body = ask(port, path, headers)
                if status != 200:
                    wrong.append((path, status)); continue
                modern = [a for a in ET.fromstring(body).findall("Agent")
                          if a.get("identifier", "").startswith("tv.plex.agents")]
                for agent in modern:
                    key = "mediaType=%s %s" % (media_type or "1", agent.get("identifier"))
                    # every modern agent shares one list (cycle 25); the bare form's rows
                    # are compared with the movie agent's list
                    want = documents.get(key) or documents["mediaType=1 tv.plex.agents.movie"]
                    got = rows(agent)
                    if got != want:
                        first = next((g, w) for g, w in zip(got + [None] * 45, want) if g != w)
                        wrong.append((path, agent.get("identifier"), first))
                if not modern:
                    wrong.append((path, "no modern agent"))
            default = next(r for r in documents["mediaType=1 tv.plex.agents.movie"] if len(r) > 2)
            check("%s: every modern agent's Language rows are titled, ordered and preselected "
                  "as zen's (%s %s)" % (language, default[0], default[1]), not wrong, wrong[:3])
    finally:
        process.terminate()
        process.wait(timeout=10)
    print("PASS %d/%d" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
