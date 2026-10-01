#!/usr/bin/env python3
"""Cycle 87 follow-up W5: an EMPTY music library, read the way Plex Web reads it.

Add Library now creates a `type=artist` library on both servers (W4), and plexsim
holds no music, so the library a user adds is empty. Plex Web 4.162.1 and python-plexapi
4.18.2 then read its row, its `/media/providers` directory, its `Meta`, its filter and
sort descriptors, its preferences and its hubs. `tests/fixtures/c87_w5_music_library_s42.json`
is every one of those reads, replayed on plexscratch42 (1.42.2) against the same empty
music library made with the same form, and each check below asserts the measured value.

Drives a real server process (`tests/server_command.py`): the native binary by
default, the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_followups_c87_w5.py
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
FIXTURE = os.path.join(HERE, "fixtures", "c87_w5_music_library_s42.json")
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


class Client:
    def __init__(self, port, fixture):
        self.port = port
        self.headers = dict(fixture["request_headers"], **{
            "X-Plex-Client-Identifier": "followups-c87-w5", "Accept": "application/json"})

    def get(self, method, path, paging=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = dict(self.headers, **(paging or {}))
        try:
            connection.request(method, path, headers=sent)
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        try:
            body = json.loads(raw) if raw.strip().startswith(b"{") else raw
        except ValueError:
            body = raw
        return response.status, body


def retarget(value, theirs, ours):
    """The reference's section key rewritten to ours, everywhere it names the library."""
    text = json.dumps(value)
    for pattern in ("/sections/%s/", "/sections/%s\"", "/sections/%s?", "sectionID=%s",
                    "contentDirectoryID=%s", "%%2C%s%%2C"):
        text = text.replace(pattern % theirs, pattern % ours)
    return json.loads(text)


def exchange(fixture, path):
    return next(row for row in fixture["exchanges"] if row["path"] == path)


def run(port, fixture):
    client = Client(port, fixture)
    theirs = fixture["section"]
    status, created = client.get("POST", fixture["created_with"])
    ours = str(created["MediaContainer"]["Directory"][0]["key"]) if status == 201 else "0"
    check("Add Library creates the music library", status == 201, status)
    echo = fixture["created_body"]["MediaContainer"]["Directory"][0]
    got = created["MediaContainer"]["Directory"][0] if status == 201 else {}
    check("the 201 echoes the measured attributes in the measured order",
          list(got) == list(echo), list(got))
    check("the 201 echoes the form's agent, scanner, group and folder",
          [got.get(k) for k in ("agent", "scanner", "metadataAgentProviderGroupId")]
          == [echo[k] for k in ("agent", "scanner", "metadataAgentProviderGroupId")]
          and got.get("Location", [{}])[0].get("path") == echo["Location"][0]["path"], got)

    def measured(path):
        row = exchange(fixture, path)
        return row, retarget(row["body"], theirs, ours)

    def ask(path, paging=None):
        return client.get("GET", path.replace("{S}", ours), paging)

    # -- the library row keeps the form ----------------------------------------
    _row, want = measured("/library/sections")
    want = want["MediaContainer"]["Directory"][0]
    _status, body = ask("/library/sections")
    got = next((d for d in body["MediaContainer"]["Directory"] if str(d["key"]) == ours), {})
    for name in ("type", "agent", "scanner", "language", "metadataAgentProviderGroupId",
                 "contentChangedAt"):
        check("the library row's %s is the form's (%r)" % (name, want[name]),
              got.get(name) == want[name], got.get(name))
    check("the library row's folder is the one the form named",
          [loc.get("path") for loc in got.get("Location") or []]
          == [loc["path"] for loc in want["Location"]], got.get("Location"))
    check("the row's three create stamps are one moment, not 0",
          got.get("createdAt") and got.get("createdAt") == got.get("updatedAt")
          == got.get("scannedAt"), got)
    _row, want = measured("/media/providers")
    want = want["MediaContainer"]["Directory"][0]
    _status, body = ask("/media/providers")
    got = next((d for provider in body["MediaContainer"].get("MediaProvider", [])
                for feature in provider.get("Feature", []) if feature.get("type") == "content"
                for d in feature.get("Directory", []) if d.get("id") == ours), {})
    check("/media/providers names the music agent and scanner",
          (got.get("agent"), got.get("scanner")) == (want["agent"], want["scanner"]),
          (got.get("agent"), got.get("scanner")))
    _row, want = measured("/library/sections/{S}/location")
    _status, body = ask("/library/sections/{S}/location")
    check("the folder axis lists the folder the form named",
          [d.get("title") for d in body["MediaContainer"].get("Directory", [])]
          == [d["title"] for d in want["MediaContainer"]["Directory"]], body)

    # -- the library browses as artists, albums and tracks ----------------------
    def meta_of(body):
        return (body.get("MediaContainer") or {}).get("Meta") or {} if isinstance(body, dict) else {}

    def without_source(meta):
        # The one claim-gated row: zen (claimed) lists `source` for albums and tracks
        # and the unclaimed copy does not (see section_meta_artist.json's provenance).
        meta = json.loads(json.dumps(meta))
        for entry in meta.get("Type") or []:
            if "Filter" in entry:
                entry["Filter"] = [f for f in entry["Filter"] if f.get("filter") != "source"]
        return meta

    for path, label in (
            ("/library/sections/{S}/all?includeMeta=1&X-Plex-Container-Start=0&X-Plex-Container-Size=0",
             "the library's Meta"),
            ("/library/sections/{S}/all?includeMeta=1&includeAdvanced=1&X-Plex-Container-Start=0"
             "&X-Plex-Container-Size=0", "the library's advanced Meta"),
            ("/library/sections/{S}/all?type=9&includeCollections=1&includeExternalMedia=1"
             "&includeAdvanced=1&includeMeta=1", "the Albums grid's Meta")):
        row, want = measured(path)
        _status, body = ask(path, row["paging"])
        got = meta_of(body)
        want = want["MediaContainer"]["Meta"]
        check("%s lists the measured Types, titles and active mark" % label,
              [(t.get("type"), t.get("title"), t.get("active"), t.get("key")) for t in got.get("Type") or []]
              == [(t["type"], t["title"], t["active"], t["key"]) for t in want["Type"]],
              [(t.get("type"), t.get("title"), t.get("active")) for t in got.get("Type") or []])
        for name in ("Sort", "Field"):
            check("%s: every Type's %s list is the measured one" % (label, name),
                  [t.get(name) for t in got.get("Type") or []] == [t.get(name) for t in want["Type"]],
                  [[x.get("key") for x in t.get(name) or []] for t in got.get("Type") or []])
        check("%s: every Type's Filter list is the measured one" % label,
              [t.get("Filter") for t in without_source(got).get("Type") or []]
              == [t.get("Filter") for t in want["Type"]],
              [[x.get("filter") for x in t.get("Filter") or []] for t in got.get("Type") or []])
        check("%s: the eight FieldType rows" % label,
              got.get("FieldType") == want["FieldType"], got.get("FieldType"))
    _row, want = measured("/media/providers")
    want = [p["key"] for p in want["MediaContainer"]["Directory"][0]["Pivot"] if p["id"] == "library"]
    _status, body = ask("/media/providers")
    got = [p.get("key") for provider in body["MediaContainer"].get("MediaProvider", [])
           for feature in provider.get("Feature", []) if feature.get("type") == "content"
           for d in feature.get("Directory", []) if d.get("id") == ours
           for p in d.get("Pivot", []) if p.get("id") == "library"]
    check("the Library pivot opens the artists (%s)" % want, got == want, got)
    for path in ("/library/sections/{S}/sorts", "/library/sections/{S}/sorts?type=8",
                 "/library/sections/{S}/sorts?type=9", "/library/sections/{S}/sorts?type=10",
                 "/library/sections/{S}/sorts?type=8&includeAdvanced=1",
                 "/library/sections/{S}/filters", "/library/sections/{S}/filters?type=8"):
        _row, want = measured(path)
        _status, body = ask(path)
        got = body["MediaContainer"].get("Directory") if isinstance(body, dict) else body
        check("%s is the measured list" % path,
              got == want["MediaContainer"]["Directory"], [x.get("key") for x in got or []])
    _row, want = measured("/library/sections/{S}?includeDetails=1")
    _status, body = ask("/library/sections/{S}?includeDetails=1")
    check("the section detail's Type rows are the music ones",
          without_source(body.get("MediaContainer") or {}).get("Type")
          == want["MediaContainer"]["Type"],
          [t.get("type") for t in (body.get("MediaContainer") or {}).get("Type") or []])

    # -- the library's own settings ---------------------------------------------
    _row, want = measured("/library/sections/{S}/prefs")
    _row, form = measured("/library/sections/prefs?type=8&agent=tv.plex.agents.music")
    check("(reference) an empty music library's prefs ARE the music form's",
          want == form, "fixture")
    claimed = fixture["witnesses"]["zen_music_form"]["body"]["MediaContainer"]["Setting"]
    _status, body = ask("/library/sections/{S}/prefs")
    got = body["MediaContainer"].get("Setting") if isinstance(body, dict) else None
    check("the music library's prefs are the claimed form's %d rows" % len(claimed),
          got == claimed, [r.get("id") for r in got or []])
    unclaimed = {r["id"]: r for r in want["MediaContainer"]["Setting"]}
    check("every row the unclaimed copy answers is there, attribute for attribute",
          all(unclaimed[r["id"]] == r for r in got or [] if r.get("id") in unclaimed)
          and set(unclaimed) <= {r.get("id") for r in got or []}, got)
    _status, body = ask("/library/sections?includePreferences=1")
    row = next((d for d in body["MediaContainer"]["Directory"] if str(d["key"]) == ours), {})
    check("the includePreferences block carries the same rows",
          (row.get("Preferences") or {}).get("Setting") == claimed,
          [r.get("id") for r in (row.get("Preferences") or {}).get("Setting") or []])
    write = fixture["witnesses"]["prefs_write"]
    status, _body = client.get("PUT", write["path"].replace("{S}", ours))
    _s, body = ask("/library/sections/{S}/prefs")
    values = {r.get("id"): r.get("value") for r in body["MediaContainer"].get("Setting") or []}
    check("a music setting write is a %d and reads back as measured" % write["status"],
          status == write["status"]
          and all(values.get(k) == v for k, v in write["read_back"].items()), (status, values))

    # -- the Recommended tab and Home ---------------------------------------------
    randoms = fixture["witnesses"]["random_music_hubs"]
    periods = {period: title for period, title in randoms["vault_periods"]}

    def shelf_identity(hub):
        return {k: hub.get(k) for k in ("hubIdentifier", "title", "type", "context", "size",
                                         "more", "key", "attribution")
                if k in hub}

    for path in ("/hubs/sections/{S}?count=12&includeLibraryPlaylists=1&includeStations=1"
                 "&includeRecentChannels=1&includeMeta=1&includeExternalMetadata=1"
                 "&excludeFields=summary",
                 "/hubs/sections/{S}?includeGuids=1&includeStations=1",
                 "/hubs/sections/{S}", "/hubs/sections/{S}?includeStations=1",
                 "/hubs/sections/{S}?includeExternalMetadata=1",
                 "/hubs/sections/{S}?includeLibraryPlaylists=1"):
        row, want = measured(path)
        _status, body = ask(path, row["paging"])
        got = (body.get("MediaContainer") or {}).get("Hub") or [] if isinstance(body, dict) else []
        want = want["MediaContainer"]["Hub"]
        short = path.split("?")[1][:40] if "?" in path else "bare"
        check("hubs (%s): the measured %d music shelves in order" % (short, len(want)),
              [h.get("hubIdentifier") for h in got]
              == [h["hubIdentifier"].replace(".%s" % theirs, ".%s" % ours) for h in want],
              [h.get("hubIdentifier") for h in got])
        fixed_ok = True
        for mine, theirs_hub in zip(got, want):
            if theirs_hub.get("random"):
                continue
            expected = shelf_identity(retarget(theirs_hub, theirs, ours))
            expected["hubIdentifier"] = theirs_hub["hubIdentifier"].replace(
                ".%s" % theirs, ".%s" % ours)
            if shelf_identity(mine) != expected:
                fixed_ok = False
                print("   ", shelf_identity(mine), expected)
        check("hubs (%s): every non-random shelf's key, title, type and context" % short,
              fixed_ok and got, "see above")
        vault = next((h for h in got if str(h.get("hubIdentifier")).startswith("music.vault.")), {})
        period = str(vault.get("key", "")).partition("lastViewedAt<=")[2].partition("&")[0]
        check("hubs (%s): the vault shelf draws a measured period and names it" % short,
              periods.get(period) == vault.get("title") and vault.get("random") is True, vault)
        tour = next((h for h in got if str(h.get("hubIdentifier")).startswith("music.touring.")), {})
        cutoff = str(tour.get("key", "")).partition("taggingCreatedAt>=")[2]
        check("hubs (%s): the touring cutoff lies within the last day" % short,
              cutoff.isdigit() and 0 <= time.time() - int(cutoff) <= 86400
              and tour.get("random") is True, tour)
    path = "/hubs/promoted?contentDirectoryID={S}&includeMeta=1&count=12"
    row, want = measured(path)
    _status, body = ask(path)
    got = [h for h in body["MediaContainer"].get("Hub") or []
           if str(h.get("hubIdentifier", "")).endswith(".%s" % ours)]
    want = [retarget(h, theirs, ours) for h in want["MediaContainer"]["Hub"]
            if h["hubIdentifier"].endswith(".%s" % theirs)]
    check("Home's promoted call gives the music library its Recently Added albums shelf",
          [(h.get("hubIdentifier"), h.get("title"), h.get("type"), h.get("key")) for h in got]
          == [(h["hubIdentifier"].replace(".%s" % theirs, ".%s" % ours), h["title"], h["type"],
               h["key"]) for h in want], got)


def main():
    fixture = json.load(open(FIXTURE))
    process, port = start()
    try:
        run(port, fixture)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    passed = sum(results)
    print("PASS %d/%d c87 W5 follow-up checks" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
