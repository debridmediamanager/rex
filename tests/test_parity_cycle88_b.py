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

And the plexapi walk's `coll.uploadPoster` step, re-measured in
`tests/fixtures/collection_poster_upload_s42.json` (a playlist takes the same rule,
`playlist_poster_upload_s42.json`): a poster UPLOAD makes the collection's
`thumb` its own `/library/metadata/{id}/thumb/{v}` (the composite before), adds a
`coverPoster` `Image`, locks `thumb` and moves `updatedAt`; `/posters` lists the upload first
under a `/library/metadata/{id}/file?url=upload%3A...` key and the composite last, each with
an explicit `selected`; and the upload is the image at `thumb` (bare, versioned, `thumb/1`),
`poster`, the candidate key and the transcoder, while `art` stays a 404. Both servers kept
the composite `thumb` (the uploaded poster never showed), listed the bare `upload://` uri as
the candidate, and served a grey placeholder or a 404 at those paths.

And a tag NAME in a section filter (`tests/fixtures/tag_name_case_kids_zen.json`, kids and
zen): ASCII letters match ignoring case -- `genre=ANIMATION` is `Animation` -- and a
non-ASCII letter only in its own case -- `actor=ARINZé KENE` finds Arinzé Kene,
`ARINZÉ KENE` nothing (SQLite's NOCASE). Both servers compared names exactly.

Drives a real server process (`tests/server_command.py`): the native binary by default,
the oracle with `PLEXSIM_SERVER=python`.

    python3 tests/test_parity_cycle88_b.py
"""
import base64
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
CASE_FIXTURE = os.path.join(HERE, "fixtures", "tag_name_case_kids_zen.json")
POSTER_FIXTURES = [os.path.join(HERE, "fixtures", name) for name in
                   ("collection_poster_upload_s42.json", "playlist_poster_upload_s42.json")]
MOVIE = "1012"                     # catalog_ui's movie, the playlist member
# The 206-byte PNG the fixture's upload was made of; the recorded upload is these bytes
# plus the fixture's `tag`, which keeps its sha1 (and so its candidate) fresh on the copy.
POSTER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABgCAIAAAAip+O/AAAACXBIWXMAAAABAAAAAQBPJcTWAAAAgElE"
    "QVR4nO3PwQkAIBDAMAX3H/lwCB9BaCZo96y/HR3wqgGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0B"
    "rQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0BrQGtAa0B"
    "rQGtAe0CHaQCfFnVhS0AAAAASUVORK5CYII=")
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


def request(port, method, path, body=None, raw=False):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(method, path, body=body, headers=dict(HEADERS, Connection="close"))
        response = connection.getresponse()
        data = response.read()
        if raw:
            return response.status, data, dict(response.getheaders())
        try:
            return response.status, json.loads(data) if data.strip() else None
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


def poster_view(path, body, key):
    """A read's collection, reduced to what the upload rule is about, ids and stamps cut."""
    rows = ((body or {}).get("MediaContainer") or {}).get("Metadata") or []
    if path.split("?")[0].endswith("/posters"):
        return [{"ratingKey": "upload" if str(e.get("ratingKey")).startswith("upload://")
                 else e.get("ratingKey"),
                 "key": ("file" if "/file?url=upload%3A%2F%2Fposters%2F" in e.get("key", "")
                         else "composite" if "/composite/" in e.get("key", "") else e.get("key")),
                 "thumb_is_key": e.get("thumb") == e.get("key") if "/file?" in e.get("key", "")
                 else e.get("thumb", "").endswith("width=200&height=300"),
                 "selected": e.get("selected")} for e in rows]
    out = []
    for row in rows:
        if str(row.get("ratingKey")) != str(key):
            continue
        thumb = str(row.get("thumb") or "")
        own = "/library/metadata/%s/thumb/" % key
        out.append({
            "thumb": "own" if thumb.startswith(own) else
                     "composite" if "/composite/" in thumb else thumb,
            "versioned_at_updatedAt": thumb.startswith(own)
                                      and thumb[len(own):] == str(row.get("updatedAt")),
            "Image": [(i.get("type"), i.get("alt") == row.get("title"), i.get("url") == thumb)
                      for i in row.get("Image") or []],
            "Field": [f.get("name") for f in row.get("Field") or []]})
    return out


def server_poster_checks(port, fixture):
    kind = fixture.get("kind", "collection")
    png = POSTER_PNG + fixture["tag"].encode()
    if len(png) != fixture["upload_bytes"]:
        raise SystemExit("the fixture's upload was %d bytes" % fixture["upload_bytes"])
    status, body = request(port, "GET", "/identity")
    machine = body["MediaContainer"]["machineIdentifier"]
    uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (
        machine, MEMBER if kind == "collection" else MOVIE)
    exchanges = fixture["exchanges"]
    ref_key = str(exchanges[0]["body"]["MediaContainer"]["Metadata"][0]["ratingKey"])
    if kind == "collection":
        status, body = request(port, "POST", "/library/collections?" + urllib.parse.urlencode(
            dict(type=2, title="C88B Poster " + fixture["tag"], smart=0, sectionId=SECTION,
                 uri=uri)))
    else:
        status, body = request(port, "POST", "/playlists?" + urllib.parse.urlencode(
            dict(type="video", title="C88B Poster " + fixture["tag"], smart=0, uri=uri)))
    key = str(body["MediaContainer"]["Metadata"][0]["ratingKey"])
    thumb = candidate = None
    for exchange in exchanges[1:]:
        method, path = exchange["method"], exchange["path"]
        if path.startswith(("/library/collections?", "/playlists?")):
            continue
        if method == "DELETE":
            break
        if "/thumb/" in path and not path.endswith("/thumb/1") and thumb:
            path = thumb
        elif "/file?url=upload%3A%2F%2Fposters%2F" in path and "%2F0000" not in path \
                and candidate:
            path = candidate
        elif path.startswith("/photo/:/transcode") and thumb:
            path = "/photo/:/transcode?width=100&height=100&url=" + urllib.parse.quote(
                thumb, safe="")
        path = (path.replace("/%s/" % ref_key, "/%s/" % key).replace("/%s?" % ref_key, "/%s?" % key)
                .replace("/sections/%s/" % REF_SECTION, "/sections/%s/" % SECTION))
        if path.endswith("/%s" % ref_key):
            path = path[: -len(ref_key)] + key
        ref = exchange["body"]
        if method == "POST":
            got_status, _, _ = request(port, "POST", path, body=png, raw=True)
            check("%s upload answers %s" % (kind, exchange["status"]),
                  got_status == exchange["status"], got_status)
            continue
        if isinstance(ref, dict) and "MediaContainer" in ref:
            got_status, got = request(port, method, path)
            want = poster_view(exchange["path"], ref, ref_key)
            have = poster_view(path, got, key)
            check("%s %s %s reads %s" % (kind, method, exchange["path"].replace(ref_key, "{id}"),
                                          want),
                  got_status == exchange["status"] and have == want, (got_status, have))
            if path.split("?")[0] == "/library/metadata/%s" % key:
                thumb = ((got or {}).get("MediaContainer") or {}).get("Metadata", [{}])[0].get("thumb")
            if path.endswith("/posters"):
                rows = ((got or {}).get("MediaContainer") or {}).get("Metadata") or []
                candidate = next((e.get("key") for e in rows
                                  if str(e.get("ratingKey")).startswith("upload://")), None)
            continue
        got_status, data, headers = request(port, method, path, raw=True)
        transcoded = path.startswith("/photo/")
        # The local-asset mark is compared on the images only: the copy's `art` 404 carries
        # one too, a header on an error page no client reads (recorded, not emulated).
        ok = exchange["status"] == 200
        want = (exchange["status"], exchange["headers"].get("Content-Type"),
                None if transcoded or not ok else exchange["headers"].get("X-Plex-Local-Asset"),
                None if transcoded or method == "HEAD" or not isinstance(ref, dict)
                else ref.get("is_upload"))
        have = (got_status, headers.get("Content-Type"),
                None if transcoded or not ok else headers.get("X-Plex-Local-Asset"),
                None if transcoded or method == "HEAD" or not isinstance(ref, dict)
                else data == png)
        check("%s %s %s is %s" % (kind, method, exchange["path"].replace(ref_key, "{id}")[:70],
                                  want),
              have == want, have)
    request(port, "DELETE", ("/library/collections/%s" if kind == "collection"
                             else "/playlists/%s") % key)


def server_case_checks(port, fixture):
    """The reference's relation, replayed on catalog_ui's own spellings of the same tags."""
    by_path = {}
    for exchange in fixture["exchanges"]:
        query = urllib.parse.unquote(exchange["path"].split("&", 1)[1])
        by_path[query] = exchange["titles"]
    canonical = {"genre": "Animation", "country": "Netherlands", "actor": "Arinzé Kene"}
    for query, titles in by_path.items():
        name, _, value = query.partition("=")
        negated = name.endswith("!")
        field = name.rstrip("!")
        if field not in canonical or value.lower() not in (canonical[field].lower(),) \
                and value.upper() != canonical[field].upper():
            continue
        base = [t for q, t in by_path.items() if q == "%s=%s" % (name, canonical[field])]
        if not base and canonical[field] in ("Animation", "Netherlands") and negated:
            base = None
        same = (titles == base[0]) if base else bool(titles)
        status, body = request(port, "GET", "/library/sections/1/all?type=1&%s%s=%s" % (
            field, "!" if negated else "", urllib.parse.quote(value)))
        status, exact = request(port, "GET", "/library/sections/1/all?type=1&%s%s=%s" % (
            field, "!" if negated else "", urllib.parse.quote(canonical[field])))
        got = sorted(m.get("title") for m in body["MediaContainer"].get("Metadata") or [])
        want = sorted(m.get("title") for m in exact["MediaContainer"].get("Metadata") or [])
        folds = value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                                              "abcdefghijklmnopqrstuvwxyz")) \
            == canonical[field].translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                                                        "abcdefghijklmnopqrstuvwxyz"))
        if not negated:
            check("reference: %s %s the canonical spelling (an ASCII-only case fold)" % (
                query, "matches as" if same else "does NOT match as"),
                  same == folds and bool(want), (same, folds, want))
        check("%s answers %s" % (query, "the canonical rows" if same else "no row"),
              got == (want if same else []) and want, (got, want))


def main():
    fixture = json.load(open(FIXTURE))
    steps = reference_edits(fixture)
    reference_checks(fixture, steps)
    process, port = start()
    try:
        server_edit_checks(port, steps)
        server_read_checks(port, fixture)
        for path in POSTER_FIXTURES:
            server_poster_checks(port, json.load(open(path)))
        server_case_checks(port, json.load(open(CASE_FIXTURE)))
    finally:
        stop(process)
    passed = sum(results)
    print("PASS %d/%d" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
