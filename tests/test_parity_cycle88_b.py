#!/usr/bin/env python3
"""Cycle 88, territory B: a collection's locks and labels take the ITEM edit grammar.

Confirming cycle 87's emptied-collection counts on plexscratch42 (PMS 1.42.2) with the
python-plexapi walk's exact plain-collection sequence showed the counts right and the rest
of the collection wrong. Re-measured through all three save paths and recorded in
`tests/fixtures/collection_edit_grammar_s42.json`:

  * every `X.locked=1` lands in the collection's `Field` list -- title, summary,
    contentRating, thumb, label -- and `X.locked=0` takes it out (both servers kept a
    `titleSort` lock alone, so the Edit dialog's lock icons and plexapi's `fields` never
    showed one);
  * on the SECTION save (`all?type=18&id=`) an indexed `label[N]` MERGES -- label B after
    label A reads [B, A] -- and `label[].tag.tag-=a` removes A whatever its case; on the
    item and collections paths the list REPLACES and any removal clears it (both servers
    replaced on every path and ignored every removal, so plexapi's `removeLabel` and the
    dialog's "remove the last label" changed nothing; the oracle's collections path dropped
    labels altogether);
  * a LISTED collection -- `all?type=18`, `/collections`, `/library/all?type=18` -- carries
    no `Label`, no `Field` and, under `includeGuids=1`, no `Guid`; the detail reads carry
    `Label` then `Field`.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle88_b.py
"""
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse

from server_command import server_command

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join("tests", "catalog_ui.json")
FIXTURE = os.path.join(HERE, "fixtures", "collection_edit_grammar_s42.json")
ENV = dict(os.environ, RD_TOKEN="dummy", PYTHONDONTWRITEBYTECODE="1",
           PLEXSIM_PLEXTV_URL=os.environ.get("PLEXSIM_PLEXTV_URL", "http://127.0.0.1:9"))
for name in ("PLEXSIM_STATE", "PLEXSIM_TOKEN", "TMDB_KEY"):
    ENV.pop(name, None)
HEADERS = {"X-Plex-Product": "Plex Web", "X-Plex-Platform": "Chrome",
           "X-Plex-Client-Identifier": "parity-cycle88-b", "Accept": "application/json"}
SECTION, MEMBER = "2", "20001"     # catalog_ui's show library and its show
REF_SECTION = "3"                  # the fixture's section on plexscratch42

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)[:700]))


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, headers=dict(HEADERS, Connection="close"))
        response = connection.getresponse()
        body = response.read()
        try:
            return response.status, json.loads(body) if body.strip() else None
        except ValueError:
            return response.status, None
    finally:
        connection.close()


def start():
    port = free_port()
    args = ["--port", port, "--catalog", CATALOG, "--assert-claimed", "--gdm", "off"]
    process = subprocess.Popen(server_command(*args, root=ROOT), cwd=ROOT, env=ENV,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
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
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()


def view(row):
    """What the rule is about: the lock names, the label tags, and whether a Guid rides."""
    return {"Field": [f.get("name") for f in row.get("Field") or []],
            "Label": [t.get("tag") for t in row.get("Label") or []],
            "Guid": bool(row.get("Guid")),
            "summary": row.get("summary"), "contentRating": row.get("contentRating")}


def path_kind(path):
    if "/sections/" in path:
        return "section"
    return "item" if path.startswith("/library/metadata/") else "collections"


def read_kind(path):
    return ("metadata" if path.startswith("/library/metadata/") else
            "collection" if path.startswith("/library/collections/") else "listing")


def reference_edits(fixture):
    """[(path kind, query, status, {read kind: view})] for every PUT in the fixture."""
    steps, current = [], None
    for exchange in fixture["edits"]:
        if exchange["method"] == "PUT":
            path, _, query = exchange["path"].partition("?")
            if path_kind(path) == "section":
                query = "&".join(p for p in query.split("&")
                                 if not p.startswith(("type=", "id=")))
            current = (path_kind(path), query, exchange["status"], {})
            steps.append(current)
        elif exchange["method"] == "GET" and current is not None:
            rows = [m for m in exchange["body"]["MediaContainer"].get("Metadata") or []
                    if m.get("type") == "collection" and "c88b1" in str(m.get("title"))]
            if rows:
                current[3][read_kind(exchange["path"])] = view(rows[0])
    return steps


def reference_checks(fixture, steps):
    """The fixture says what this file claims it says."""
    by = {}
    for kind, query, _status, views in steps:
        by.setdefault(kind, []).append((query, views["metadata"]))
    section = dict(by["section"])
    item = dict(by["item"])
    first = by["section"][0][1]
    check("reference: title.locked=1 puts title in Field", first["Field"] == ["title"], first)
    last = by["section"][-1][1]
    check("reference: title and label unlocked leave contentRating and thumb",
          last["Field"] == ["contentRating", "thumb"], last)
    merged = [v for q, v in by["section"] if q.startswith("label[0].tag.tag=c88b1LabB")][0]
    check("reference: the section save MERGES label B before label A",
          merged["Label"] == ["C88b1LabB", "C88b1LabA"], merged)
    removed = [v for q, v in by["section"] if "tag.tag-=" in q][0]
    check("reference: the section save's lower-case removal takes A alone",
          removed["Label"] == ["C88b1LabB"], removed)
    replaced = [v for q, v in by["item"] if q.startswith("label[0].tag.tag=c88b1LabB")][0]
    cleared = [v for q, v in by["item"] if "tag.tag-=" in q][0]
    check("reference: the item path REPLACES and any removal clears",
          replaced["Label"] == ["C88b1LabB"] and cleared["Label"] == [], (replaced, cleared))
    listing = [views.get("listing") for _k, _q, _s, views in steps]
    check("reference: a listed collection carries no Field, Label or Guid",
          all(v == dict(v, Field=[], Label=[], Guid=False) for v in listing if v), listing[:3])
    del section, item


def server_edit_checks(port, steps):
    status, body = request(port, "GET", "/identity")
    machine = body["MediaContainer"]["machineIdentifier"]
    uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, MEMBER)
    for kind in ("section", "item", "collections"):
        status, body = request(port, "POST", "/library/collections?" + urllib.parse.urlencode(
            dict(type=2, title="C88B c88b1 %s" % kind, smart=0, sectionId=SECTION, uri=uri)))
        key = body["MediaContainer"]["Metadata"][0]["ratingKey"]
        for step_kind, query, ref_status, ref_views in steps:
            if step_kind != kind:
                continue
            path = {"section": "/library/sections/%s/all?type=18&id=%s&%s" % (SECTION, key, query),
                    "item": "/library/metadata/%s?%s" % (key, query),
                    "collections": "/library/collections/%s?%s" % (key, query)}[kind]
            got_status, _ = request(port, "PUT", path)
            got = {}
            status, body = request(port, "GET", "/library/metadata/%s" % key)
            got["metadata"] = view(body["MediaContainer"]["Metadata"][0])
            status, body = request(port, "GET", "/library/sections/%s/all?includeGuids=1&type=18"
                                   % SECTION)
            rows = [m for m in body["MediaContainer"].get("Metadata") or []
                    if str(m.get("ratingKey")) == str(key)]
            got["listing"] = view(rows[0]) if rows else None
            status, body = request(port, "GET", "/library/collections/%s" % key)
            got["collection"] = view(body["MediaContainer"]["Metadata"][0])
            check("%s path %s: status %s and %s" % (kind, query, ref_status, ref_views["metadata"]),
                  got_status == ref_status and got == ref_views, (got_status, got))
        request(port, "DELETE", "/library/collections/%s" % key)


def server_read_checks(port, fixture):
    status, body = request(port, "GET", "/identity")
    machine = body["MediaContainer"]["machineIdentifier"]
    uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (machine, MEMBER)
    status, body = request(port, "POST", "/library/collections?" + urllib.parse.urlencode(
        dict(type=2, title="C88B Reads", smart=0, sectionId=SECTION, uri=uri)))
    key = body["MediaContainer"]["Metadata"][0]["ratingKey"]
    ref_key = None
    for exchange in fixture["reads"]:
        if exchange["method"] == "POST":
            ref_key = exchange["body"]["MediaContainer"]["Metadata"][0]["ratingKey"]
            continue
        path = (exchange["path"].replace("/%s" % ref_key, "/%s" % key)
                .replace("id=%s" % ref_key, "id=%s" % key)
                .replace("/sections/%s/" % REF_SECTION, "/sections/%s/" % SECTION))
        if exchange["method"] == "PUT":
            request(port, "PUT", path)
            continue
        if exchange["method"] != "GET" or exchange.get("accept") != "application/json":
            continue
        ref = [m for m in exchange["body"]["MediaContainer"].get("Metadata") or []
               if str(m.get("ratingKey")) == str(ref_key)]
        status, body = request(port, "GET", path)
        got = [m for m in ((body or {}).get("MediaContainer") or {}).get("Metadata") or []
               if str(m.get("ratingKey")) == str(key)]
        want = [[k for k in m if k in ("Label", "Field", "Guid")] for m in ref]
        have = [[k for k in m if k in ("Label", "Field", "Guid")] for m in got]
        check("read %s carries %s" % (exchange["path"].split("?")[0] + (
            "?" + exchange["path"].split("?")[1] if "?" in exchange["path"] else ""), want),
            have == want, have)
    for path in ("/library/all?type=18", "/library/all?type=18&includeGuids=1", "/library/all"):
        # Measured on plexscratch42 with the same collection: `/library/all?type=18` lists it
        # with neither tag list; the untyped query lists the same row.
        status, body = request(port, "GET", path)
        got = [m for m in ((body or {}).get("MediaContainer") or {}).get("Metadata") or []
               if str(m.get("ratingKey")) == str(key)]
        check("read %s carries no Label, Field or Guid" % path,
              got and not any(k in m for m in got for k in ("Label", "Field", "Guid")), got)
    request(port, "DELETE", "/library/collections/%s" % key)


def main():
    fixture = json.load(open(FIXTURE))
    steps = reference_edits(fixture)
    reference_checks(fixture, steps)
    process, port = start()
    try:
        server_edit_checks(port, steps)
        server_read_checks(port, fixture)
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
