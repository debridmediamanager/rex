#!/usr/bin/env python3
"""Cycle 87 W3: what W and W2 left on `/system/agents` and `/media/providers/metadata`.

- A LEGACY plug-in agent row writes `hasPrefs, identifier, primary` (then `name` in the
  typed form), in JSON and XML alike; plexsim put `identifier` first, the modern rows'
  order. An empty legacy row is a PAIRED `<Agent ...>\\n</Agent>`; plexsim self-closed
  it. Driven by zen's raw bodies: the JSON ones W2 recorded
  (`tests/fixtures/c87_w2_system_agents_zen.json`) and the XML ones recorded here
  (`tests/fixtures/c87_w3_system_agents_xml_zen.json`). After the fix every typed body
  is byte-identical to zen's; the bare form is compared with its rows sorted, because
  where `lastfm` and `plexthememusic` fall is zen's own plug-in load order (W2).

- The provider table numbers Personal Media **5** (`/system/agents` numbers it 4), its
  built-in group is 4 with `metadataAgentProviderId` 5, and provider 4 is the unlisted
  MusicBrainz intermediary that only its per-id GET answers. Measured identically on kids,
  zen, plexscratch and a fresh 1.42.2 install (plexscratch42), so a rule and not one
  database's history; plexsim numbered providers by agent position. Driven by zen's
  exchanges (`tests/fixtures/c87_w3_provider_ids_zen.json`).

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_c87_w3.py
"""
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
JSON_FIXTURE = os.path.join(HERE, "fixtures", "c87_w2_system_agents_zen.json")
XML_FIXTURE = os.path.join(HERE, "fixtures", "c87_w3_system_agents_xml_zen.json")
PROVIDER_FIXTURE = os.path.join(HERE, "fixtures", "c87_w3_provider_ids_zen.json")
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


def request(port, method, path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=dict(headers or {}, Connection="close"))
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


def label(exchange):
    return "%s [Start %s Size %s]" % (exchange["path"],
                                       exchange["headers"].get("X-Plex-Container-Start"),
                                       exchange["headers"].get("X-Plex-Container-Size"))


def json_rows_sorted(text):
    """The body as (key, value) pairs -- repeated keys and key ORDER kept -- with the
    top-level Agent rows sorted by identifier (zen's own plug-in load order aside)."""
    body = json.loads(text, object_pairs_hook=list)
    container = body[0][1]
    agents = sorted((row for key, value in container if key == "Agent" for row in value),
                    key=lambda row: [value for key, value in row if key == "identifier"])
    return [(key, value) for key, value in container if key != "Agent"], agents


XML_AGENT = re.compile(r"<Agent [^>]*/>|<Agent [^>]*>.*?</Agent>", re.S)


def xml_rows_sorted(text):
    return XML_AGENT.sub("", text), sorted(XML_AGENT.findall(text))


def legacy_order(port):
    for fixture, kind in ((JSON_FIXTURE, "JSON"), (XML_FIXTURE, "XML")):
        for exchange in json.load(open(fixture, encoding="utf-8")):
            if not exchange["path"].startswith("/system/agents"):
                continue
            headers = dict(exchange["headers"])
            if kind == "XML":
                headers.pop("Accept", None)
            status, text = request(port, "GET", exchange["path"], headers)
            want = exchange["body"]
            if exchange["path"] == "/system/agents" and \
                    exchange["headers"].get("X-Plex-Container-Start") == "0":
                sort = json_rows_sorted if kind == "JSON" else xml_rows_sorted
                try:
                    same = sort(text) == sort(want)
                except (ValueError, IndexError):
                    same = False
                check("%s %s: zen's rows, byte for byte, legacy keys `hasPrefs, identifier, "
                      "primary` and empty rows paired (row order aside)" % (kind, label(exchange)),
                      status == 200 and same, (status, text[:300]))
                continue
            first = next((index for index, (a, b) in enumerate(zip(text, want)) if a != b),
                         min(len(text), len(want)))
            check("%s %s: byte-identical to zen (legacy `hasPrefs, identifier, primary, name`)"
                  % (kind, label(exchange)), status == 200 and text == want,
                  (status, "first difference at %d: got %r want %r"
                   % (first, text[first - 60:first + 60], want[first - 60:first + 60])))


def provider_ids(port):
    for exchange in json.load(open(PROVIDER_FIXTURE, encoding="utf-8")):
        status, text = request(port, exchange["method"], exchange["path"], exchange["headers"])
        want = exchange["body"]
        if want is None:
            check("%s is a %d (zen)" % (exchange["path"], exchange["status"]),
                  status == exchange["status"], status)
            continue
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        container = want["MediaContainer"]
        rows = container.get("MetadataAgentProvider") or container.get("MetadataAgentProviderGroup")
        ids = [(row["id"], [item["metadataAgentProviderId"]
                            for item in row.get("MetadataAgentProviderGroupItem", [])])
               for row in rows]
        check("%s is zen's container, ids %s" % (exchange["path"], ids),
              status == exchange["status"] and body == want, (status, body))


def main():
    process, port = start()
    try:
        legacy_order(port)
        provider_ids(port)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d cycle-87 W3 checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
