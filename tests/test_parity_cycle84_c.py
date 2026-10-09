#!/usr/bin/env python3
"""Cycle 84, territory C: a LEGACY-agent row (`com.plexapp.agents.none://`) against the reference.

Replays the exchanges recorded in `tests/fixtures/cycle84_c.json` -- plexscratch42 (PMS 1.42.2), a
movie library created on the legacy Personal Media agent over the reference media -- on a copy of
`tests/catalog_ui.json` whose movie 1007 carries the recorded legacy guid and no Guid list, and
asserts the measured values: no `Guid` key on that row in any `includeGuids=1` listing, JSON or
XML, and the row NOT counted by the `unmatched=1` filter.

Starts its own server (native by default, `PLEXSIM_SERVER=python` the oracle,
`PLEXSIM_SERVER=/path/to/binary` any build), without `--state`.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from server_command import server_command  # noqa: E402

ROOT = os.path.dirname(HERE)
FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "cycle84_c.json")))
PROBE = "1007"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def legacy_guid():
    guids = {row["guid"] for e in FIXTURE["exchanges"] for row in e.get("rows", [])}
    assert len(guids) == 1, guids
    return guids.pop()


def legacy_catalog():
    catalog = json.load(open(os.path.join(HERE, "catalog_ui.json")))
    for row in catalog["items"]:
        if row["ratingKey"] == PROBE:
            row["guid"] = legacy_guid()
            row.pop("Guid", None)
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(catalog, handle)
    handle.close()
    return handle.name


class Server:
    def __init__(self, catalog):
        self.port = free_port()
        env = dict(os.environ, PLEXSIM_PLEXTV_URL="http://127.0.0.1:9")
        for name in ("TMDB_KEY", "PLEXSIM_STATE"):
            env.pop(name, None)
        self.proc = subprocess.Popen(
            server_command("--port", str(self.port), "--catalog", catalog,
                           "--assert-claimed", "--gdm", "off", root=ROOT),
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.3).close()
                return
            except OSError:
                time.sleep(0.2)
        raise SystemExit("server never listened")

    def get(self, path, accept):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request("GET", path, headers={"Accept": accept,
                                        "X-Plex-Client-Identifier": "c84c-check"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def probe_rows(body, accept):
    """(guid, Guid) for every element/object naming the probe row."""
    rows = []
    if accept == "application/json":
        def walk(node):
            if isinstance(node, dict):
                if node.get("ratingKey") == PROBE:
                    rows.append((node.get("guid"), node.get("Guid", "<absent>")))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(json.loads(body))
    else:
        for el in ET.fromstring(body).iter():
            if el.get("ratingKey") == PROBE:
                rows.append((el.get("guid"),
                             [g.get("id") for g in el.findall("Guid")] or "<absent>"))
    return rows


def main():
    catalog = legacy_catalog()
    server = Server(catalog)
    guid = legacy_guid()
    try:
        for e in FIXTURE["exchanges"]:
            path = e["path"].replace("{S}", "1").replace("{M}", PROBE)
            status, body = server.get(path, e["accept"])
            fmt = "JSON" if e["accept"] == "application/json" else "XML"
            if "rows" in e:
                assert all(r["Guid"] == "<absent>" for r in e["rows"]), e
                got = probe_rows(body, e["accept"]) if status == 200 else []
                check("%s %s: the legacy row carries no Guid, as measured" % (fmt, path),
                      status == e["status"] and got and all(g == (guid, "<absent>") for g in got),
                      (status, got[:2]))
            else:
                ids = [r.get("ratingKey") for r in
                       json.loads(body)["MediaContainer"].get("Metadata", [])] if status == 200 else []
                check("%s %s: the legacy row is %scounted (measured)" % (
                          fmt, path, "" if e["contains_probe"] else "NOT "),
                      status == e["status"] and (PROBE in ids) == e["contains_probe"],
                      (status, ids))
    finally:
        server.stop()
        os.unlink(catalog)
    print("PASS %d/%d" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
