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

- A write on a BUILT-IN provider or group id, measured on plexscratch42 (a fresh 1.42.2,
  2026-10-01; `tests/fixtures/c87_w3_builtin_provider_writes_s42.json`, nothing changed on
  any of them): `PUT /media/providers/metadata/1` is a 400 with no or an empty `uri` and a
  500 with an unreachable one, `POST` on the id a 404; `PUT /group/3?title=` (its own title)
  answers the row, `POST /group/3` is a 404 and `PUT /group/3/items/5` a 400. The oracle
  answered the provider row to every verb and the native server 404'd all of them.
- A built-in group's rename is REAL (plexscratch42): `PUT /group/3?title=Plex%20Music%20W3`
  answers the row with the new title, the group listing and `/group/3` keep it, the
  provider row's title does not change, and renaming back restores it. Both servers
  answered the row as held (`tests/fixtures/c87_w3_builtin_group_rename_s42.json`).
- DELETE on a BUILT-IN id (plexscratch42, destructive, restored from a snapshot after;
  `tests/fixtures/c87_w3_builtin_deletes_s42.json`): provider 3 is a 400 and stays,
  before and after its group goes; group 3 is an empty 200 -- gone from the group
  listing, `/group/3` (404, a second DELETE too) and `/system/agents`' bare list and
  `mediaType=8`/`9` -- and Add Library then refuses its agent with a 400 either way.

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
WRITE_FIXTURE = os.path.join(HERE, "fixtures", "c87_w3_builtin_provider_writes_s42.json")
RENAME_FIXTURE = os.path.join(HERE, "fixtures", "c87_w3_builtin_group_rename_s42.json")
DELETE_FIXTURE = os.path.join(HERE, "fixtures", "c87_w3_builtin_deletes_s42.json")
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


def builtin_writes(port):
    snapshot = ("/media/providers/metadata", "/media/providers/metadata/group",
                "/media/providers/metadata/1", "/media/providers/metadata/group/3")
    for exchange in json.load(open(WRITE_FIXTURE, encoding="utf-8")):
        before = [request(port, "GET", path, exchange["headers"]) for path in snapshot]
        status, text = request(port, exchange["method"], exchange["path"], exchange["headers"])
        after = [request(port, "GET", path, exchange["headers"]) for path in snapshot]
        try:
            body = json.loads(text) if exchange["body"] is not None else None
        except ValueError:
            body = text
        check("%s %s on a BUILT-IN id is a %d%s, and changes nothing (plexscratch42)"
              % (exchange["method"], exchange["path"], exchange["status"],
                 " with the row" if exchange["body"] is not None else ""),
              status == exchange["status"] and body == exchange["body"] and before == after,
              (status, text[:200], before == after))


def builtin_rename(port):
    """Rename built-in group 3 and rename it back, reading what plexscratch42 read."""
    provider_before = request(port, "GET", "/media/providers/metadata/3")
    for exchange in json.load(open(RENAME_FIXTURE, encoding="utf-8")):
        status, text = request(port, exchange["method"], exchange["path"], exchange["headers"])
        try:
            body = json.loads(text)
        except ValueError:
            body = text
        title = exchange["body"]["MediaContainer"]["MetadataAgentProviderGroup"][0]["title"]
        check("%s %s answers group 3 titled %r (plexscratch42)"
              % (exchange["method"], exchange["path"], title),
              status == exchange["status"] and body == exchange["body"], (status, text[:300]))
        for path, want in exchange["readsAfter"].items():
            status, text = request(port, "GET", path, exchange["headers"])
            try:
                got = json.loads(text)
            except ValueError:
                got = text
            check("  ...and %s then reads %r" % (path, title), status == 200 and got == want,
                  (status, text[:300]))
    check("the rename leaves the PROVIDER row's title alone (plexscratch42)",
          request(port, "GET", "/media/providers/metadata/3") == provider_before)


def _parsed(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _compare_reads(port, headers, label, reads):
    for path, want in reads.items():
        status, text = request(port, "GET", path, headers)
        body = _parsed(text)
        if want["body"] is None:
            ok, shown = status == want["status"], status
        elif path.startswith("/system/agents"):
            got = [row["identifier"] for row in body["MediaContainer"].get("Agent", [])] \
                if status == 200 else None
            want_ids = want["body"]["identifiers"]
            if path == "/system/agents":
                # zen's / the copy's own plug-in load order aside (W2).
                got, want_ids = sorted(got or []), sorted(want_ids)
            ok, shown = got == want_ids, got
        elif path == "/media/providers/metadata":
            # plexsim carries zen's `uri`/`online`; the ids are the question here.
            got = [row["id"] for row in body["MediaContainer"]["MetadataAgentProvider"]] \
                if status == 200 else None
            ok = got == [row["id"] for row in want["body"]["MediaContainer"]["MetadataAgentProvider"]]
            shown = got
        elif path == "/media/providers/metadata/3":
            ok, shown = status == 200, status
        else:
            ok, shown = status == want["status"] and body == want["body"], (status, text[:200])
        check("  ...%s, then %s reads as plexscratch42's" % (label, path), ok, shown)


def builtin_deletes(port, fixture):
    """DELETE on built-in provider 3 and built-in group 3, in plexscratch42's order. The
    two music-library creates are not replayed: plexsim serves no music library at all
    (its Add Library refuses `type=artist`), so `derived_creates` asks the same question
    of a movie library."""
    headers = fixture["headers"]
    for step in fixture["steps"]:
        if step["method"] == "POST":
            continue
        status, text = request(port, step["method"], step["path"], headers)
        empty = step["status"] == 200 and step["body"] == ""
        check("%s %s on a BUILT-IN id is a %d%s (plexscratch42)"
              % (step["method"], step["path"], step["status"], " with no body" if empty else ""),
              status == step["status"] and (not empty or text == ""), (status, text[:200]))
        _compare_reads(port, headers, "%s %s" % (step["method"], step["path"]), step["after"])


def derived_creates(port, fixture):
    """Add Library after a built-in group is gone. Measured with a music library:
    `metadataAgentProviderGroupId=3` (what 4.162.1 sends, the agent's `/system/agents` id)
    is a 400 "unable to find entry for provided 'metadataAgentProviderGroupId'", and no
    parameter a 400 "unable to find built-in agent group for provided 'agent'". The same
    lookups, asked of the movie library plexsim does serve."""
    headers = fixture["headers"]
    form = ("/library/sections?name=W3%%20%s&type=movie&agent=tv.plex.agents.movie"
            "&scanner=Plex%%20Movie&language=en-US%s&location=%%2Fdata%%2Fw3-none")
    status, _ = request(port, "POST", form % ("held", "&metadataAgentProviderGroupId=1"), headers)
    check("Add Library naming a HELD group (1) is still a 201", status == 201, status)
    status, _ = request(port, "POST", form % ("gone", "&metadataAgentProviderGroupId=3"), headers)
    check("Add Library naming the DELETED group 3 is a 400 (plexscratch42's lookup)",
          status == 400, status)
    status, _ = request(port, "DELETE", "/media/providers/metadata/group/1", headers)
    check("DELETE /media/providers/metadata/group/1 is the same empty 200", status == 200, status)
    for label, extra in (("with", "&metadataAgentProviderGroupId=1"), ("without", "")):
        status, _ = request(port, "POST", form % (label, extra), headers)
        check("Add Library for the movie agent %s its deleted group's id is a 400" % label,
              status == 400, status)


def main():
    process, port = start()
    try:
        legacy_order(port)
        provider_ids(port)
        builtin_writes(port)
        builtin_rename(port)
    finally:
        stop(process)
    # A fresh server: the deletes leave state behind.
    fixture = json.load(open(DELETE_FIXTURE, encoding="utf-8"))
    process, port = start()
    try:
        builtin_deletes(port, fixture)
        derived_creates(port, fixture)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d cycle-87 W3 checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
