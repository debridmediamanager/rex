#!/usr/bin/env python3
"""Cycle 87 W2: `/system/agents?mediaType=N` as Plex Web 4.162.1 asks for it.

Driven by the 1.42.2 reference's recorded exchanges
(`tests/fixtures/c87_w2_system_agents_zen.json`, 1.42.2, the version rex reports): the bare path
and every `mediaType` from 1 to 22, each with the Plex Web headers and
`X-Plex-Container-Start/Size` (the 1.42.2 reference is production and is always paged), plus two
windowed reads and the `/media/providers/metadata` table. The bodies are kept as RAW TEXT, because
the point of two of these checks is the repeated `Language` / `MediaType` keys that any JSON parser
collapses.

- A legacy agent's `hasPrefs` and `primary` are JSON BOOLEANS and a bare-form
  `MediaType`'s `mediaType` a NUMBER on plexlab and the 1.42.2 reference alike; rex sent the
  strings `"0"`/`"1"` its XML capture held.
- Under paging the native server re-serialised the route's body and wrote each repeated
  `Language` key once (the last one); the oracle and both references repeat it.
- `mediaType` 9, 10, 14, 20, 21 and 22 list the modern agents the 1.42.2 reference lists (Plex
  Music and
  Plex Personal Media for album/track, Personal Media for the photo and 20-22 types);
  rex listed none of them, and the provider table's `MetadataType` lists followed.

Drives a real server process (`tests/server_command.py`).

    python3 tests/test_followups_c87_w2.py
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "c87_w2_system_agents_zen.json")
ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
           REX_PLEXTV_URL=os.environ.get("REX_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("REX_STATE", "REX_TOKEN", "TMDB_KEY"):
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


def request(port, method, path, headers):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=dict(headers, Connection="close"))
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    return response.status, raw.decode("utf-8", "replace")


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
            if request(port, "GET", "/identity", {})[0] == 200:
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


def pairs(text):
    """The body with every repeated key kept: objects become lists of (key, value)."""
    return json.loads(text, object_pairs_hook=list)


def get(node, key):
    return [value for name, value in node if name == key]


def agents(text):
    container = get(pairs(text), "MediaContainer")[0]
    rows = get(container, "Agent")
    return container, (rows[0] if rows else [])


def label(exchange):
    window = exchange["headers"].get("X-Plex-Container-Start"), \
        exchange["headers"].get("X-Plex-Container-Size")
    return "%s [Start %s Size %s]" % (exchange["path"], window[0], window[1])


def typed(value):
    # `True == 1` in Python; the JSON TYPE is the finding, so compare it explicitly.
    return (type(value).__name__, value)


def legacy_types(port, exchange):
    _, want = agents(exchange["body"])
    status, text = request(port, "GET", exchange["path"], exchange["headers"])
    _, got = agents(text) if status == 200 else ([], [])

    def table(rows):
        out = {}
        for row in rows:
            identifier = get(row, "identifier")[0]
            if not identifier.startswith("com.plexapp.agents."):
                continue
            out[identifier] = (
                [typed(value) for value in get(row, "hasPrefs") + get(row, "primary")],
                # The TYPE alone: how many `MediaType` keys survive is `repeated_keys`'.
                sorted({type(value).__name__ for media in get(row, "MediaType")
                        for value in get(media, "mediaType")}))
        return out

    expected, actual = table(want), table(got)
    if not expected:
        return
    check("%s: legacy hasPrefs/primary are JSON booleans, mediaType a number (the 1.42.2 reference)"
          % label(exchange), status == 200 and actual == expected,
          {key: (expected[key], actual.get(key)) for key in expected if actual.get(key) != expected[key]})


def repeated_keys(port, exchange):
    _, want = agents(exchange["body"])
    status, text = request(port, "GET", exchange["path"], exchange["headers"])
    _, got = agents(text) if status == 200 else ([], [])

    def counts(rows):
        out = {}
        for row in rows:
            medias = get(row, "MediaType")
            out[get(row, "identifier")[0]] = (
                len(get(row, "Language")), len(medias),
                [len(get(media, "Language")) for media in medias])
        return out

    expected = counts(want)
    if not any(row[0] > 1 or row[1] > 1 for row in expected.values()):
        return
    actual = counts(got)
    # Membership is `agent_lists`' question; this one is the count of each repeated key
    # on every row both sides list.
    shared = [key for key in expected if key in actual]
    check("%s: every repeated Language/MediaType key is on the wire, as the 1.42.2 reference repeats it"
          % label(exchange), status == 200 and (shared or not actual) and
          all(actual[key] == expected[key] for key in shared),
          [(key, expected[key], actual[key]) for key in shared
           if actual[key] != expected[key]][:3] or status)


def agent_lists(port, exchange):
    container, want = agents(exchange["body"])
    status, text = request(port, "GET", exchange["path"], exchange["headers"])
    got_container, got = agents(text) if status == 200 else ([], [])
    expected = [get(row, "identifier")[0] for row in want]
    actual = [get(row, "identifier")[0] for row in got]
    if exchange["path"] == "/system/agents":
        # The bare list's ORDER of `lastfm` and `plexthememusic` is the 1.42.2 reference's own
        # plug-in load order; plexlab and both scratch copies agree with rex
        # (`legacy_agents.json`).
        expected, actual = sorted(expected), sorted(actual)
    window = [get(container, key) for key in ("size", "totalSize", "offset")]
    got_window = [get(got_container, key) for key in ("size", "totalSize", "offset")]
    check("%s lists %s (the 1.42.2 reference)" % (label(exchange), expected or "no agent"),
          status == 200 and actual == expected and got_window == window,
          (status, actual, got_window, window))


def provider_types(port, exchange):
    want = json.loads(exchange["body"])["MediaContainer"]["MetadataAgentProvider"]
    status, text = request(port, "GET", exchange["path"], exchange["headers"])
    got = json.loads(text)["MediaContainer"].get("MetadataAgentProvider", []) \
        if status == 200 else []
    # The provider ids are a separate question (the 1.42.2 reference numbers Personal Media 5); the
    # media types each provider serves are the same table `/system/agents` reads.
    expected = {row["identifier"]: row["MetadataType"] for row in want}
    actual = {row["identifier"]: row.get("MetadataType") for row in got}
    check("/media/providers/metadata: each provider's MetadataType list is the 1.42.2 reference's",
          actual == expected,
          {key: (expected[key], actual.get(key)) for key in expected if actual.get(key) != expected[key]})


def main():
    exchanges = json.load(open(FIXTURE, encoding="utf-8"))
    process, port = start()
    try:
        for exchange in exchanges:
            if exchange["path"].startswith("/system/agents"):
                legacy_types(port, exchange)
                repeated_keys(port, exchange)
                agent_lists(port, exchange)
            elif exchange["path"] == "/media/providers/metadata":
                provider_types(port, exchange)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d cycle-87 W2 checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
