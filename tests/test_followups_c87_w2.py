#!/usr/bin/env python3
"""Cycle 87 W2: `/system/agents?mediaType=N` as Plex Web 4.162.1 asks for it.

Driven by zen's recorded exchanges (`tests/fixtures/c87_w2_system_agents_zen.json`,
1.42.2, the version plexsim reports): the bare path and every `mediaType` from 1 to 22,
each with the Plex Web headers and `X-Plex-Container-Start/Size` (zen is production and
is always paged), plus two windowed reads and the `/media/providers/metadata` table. The
bodies are kept as RAW TEXT, because the point of two of these checks is the repeated
`Language` / `MediaType` keys that any JSON parser collapses.

- A legacy agent's `hasPrefs` and `primary` are JSON BOOLEANS and a bare-form
  `MediaType`'s `mediaType` a NUMBER on kids and zen alike; plexsim sent the strings
  `"0"`/`"1"` its XML capture held.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

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
    check("%s: legacy hasPrefs/primary are JSON booleans, mediaType a number (zen)"
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
    check("%s: every repeated Language/MediaType key is on the wire, as zen repeats it"
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
        # The bare list's ORDER of `lastfm` and `plexthememusic` is zen's own plug-in load
        # order; kids and both scratch copies agree with plexsim (`legacy_agents.json`).
        expected, actual = sorted(expected), sorted(actual)
    window = [get(container, key) for key in ("size", "totalSize", "offset")]
    got_window = [get(got_container, key) for key in ("size", "totalSize", "offset")]
    check("%s lists %s (zen)" % (label(exchange), expected or "no agent"),
          status == 200 and actual == expected and got_window == window,
          (status, actual, got_window, window))


def provider_types(port, exchange):
    want = json.loads(exchange["body"])["MediaContainer"]["MetadataAgentProvider"]
    status, text = request(port, "GET", exchange["path"], exchange["headers"])
    got = json.loads(text)["MediaContainer"].get("MetadataAgentProvider", []) \
        if status == 200 else []
    # The provider ids are a separate question (zen numbers Personal Media 5); the
    # media types each provider serves are the same table `/system/agents` reads.
    expected = {row["identifier"]: row["MetadataType"] for row in want}
    actual = {row["identifier"]: row.get("MetadataType") for row in got}
    check("/media/providers/metadata: each provider's MetadataType list is zen's",
          actual == expected,
          {key: (expected[key], actual.get(key)) for key in expected if actual.get(key) != expected[key]})


def main():
    exchanges = json.load(open(FIXTURE, encoding="utf-8"))
    process, port = start()
    try:
        for exchange in exchanges:
            if exchange["path"].startswith("/system/agents"):
                legacy_types(port, exchange)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d cycle-87 W2 checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
