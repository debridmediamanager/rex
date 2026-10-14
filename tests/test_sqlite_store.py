#!/usr/bin/env python3
"""The SQLite store: the state and the catalog in one database, written by the row.

The state file used to be one JSON document (every catalog row, every list, every
history entry) rewritten whole on every save, and a client write is saved before it
is answered -- so a single rating on a large library rewrote the whole library. This
suite holds what replaced it:

* the default state is `rex.db` beside a JSON catalog, a SQLite database whose
  collections are stored a row per element;
* one client write on a 20,000-row catalog touches a handful of rows, counted by
  SQLite triggers installed in the server's own database, and is saved without
  serialising the other rows (catalog row changes are tracked, `catalog.rs`);
* a legacy `state.json` (recorded from the native server before the change,
  `fixtures/legacy_state_native.json`) is imported once and kept aside;
* `--catalog` takes a database written by `rex catalog --convert`: it serves
  exactly what the JSON it came from serves, and the state then lives in the same file;
* a catalog rebuilt inside that database keeps a `.stale` copy and carries view
  state, as a rebuilt JSON catalog does.

    python3 tests/test_sqlite_store.py
"""
import copy
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

from test_persist import Instance, LIBRARY, check, first, results


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MAGIC = b"SQLite format 3\x00"


def is_sqlite(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == MAGIC
    except OSError:
        return False


def workdir(name="catalog.json", document=None):
    work = tempfile.mkdtemp(prefix="rex-sqlite-")
    catalog = os.path.join(work, name)
    if document is None:
        shutil.copy(os.path.join(HERE, "catalog_ui.json"), catalog)
    else:
        with open(catalog, "w") as handle:
            json.dump(document, handle, indent=2)
    return work, catalog


def convert(source, target):
    rex = os.path.join(ROOT, "rust", "target", "release", "rex.exe" if os.name == "nt" else "rex")
    run = subprocess.run([rex, "catalog", "--convert", source, "--out", target],
                         capture_output=True, text=True)
    return run.returncode == 0 and is_sqlite(target), (run.stdout + run.stderr)[-600:]


def watched(instance, key="1007"):
    return first(instance.request("GET", "/library/metadata/%s" % key)[1]).get("viewCount")


def scrobble(instance, key="1007"):
    return instance.request("GET", "/:/scrobble?key=%s&identifier=%s" % (key, LIBRARY))[0]


def default_database():
    work, catalog = workdir()
    try:
        server = Instance(catalog, default=True)
        scrobble(server)
        server.stop(signal.SIGKILL)
        database = os.path.join(work, "rex.db")
        check("the default state is rex.db beside the catalog, a SQLite database",
              is_sqlite(database), "files %s" % sorted(os.listdir(work)))
        if not is_sqlite(database):
            return
        with sqlite3.connect(database) as db:
            items = db.execute("SELECT count(*) FROM row WHERE coll='state:items'").fetchone()[0]
            kind = db.execute("SELECT kind FROM doc WHERE name='state:items'").fetchone()
        check("the catalog rows are stored one database row each",
              kind == ("array",) and items == 19, "kind %s, %d rows" % (kind, items))
        restarted = Instance(catalog, default=True)
        check("a write answered before a hard kill is restored from the database",
              watched(restarted) == 1, "viewCount %s" % watched(restarted))
        restarted.stop()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def large_catalog(count):
    document = json.load(open(os.path.join(HERE, "catalog_ui.json")))
    model = next(row for row in document["items"] if row["ratingKey"] == "1007")
    for index in range(count):
        row = copy.deepcopy(model)
        key = str(600000 + index)
        row["ratingKey"] = key
        row["key"] = "/library/metadata/" + key
        row["title"] = "Copy %d" % index
        row.pop("guid", None)
        for media_index, media in enumerate(row.get("Media") or []):
            media["id"] = 900000 + index * 4 + media_index
            for part_index, part in enumerate(media.get("Part") or []):
                part_id = 2000000 + index * 4 + part_index
                part["id"] = part_id
                part["key"] = "/library/parts/%d/file.mkv" % part_id
        document["items"].append(row)
    return document


def one_write_touches_few_rows():
    name = "one rating on a 20,000-row catalog writes a handful of rows"
    work, catalog = workdir(document=large_catalog(20000))
    try:
        state = os.path.join(work, "rex.db")
        server = Instance(catalog, state=state, wait=180)
        scrobble(server)
        server.stop()
        if not is_sqlite(state):
            check(name, False, "no database: files %s" % sorted(os.listdir(work)))
            return
        # Count every row the server inserts, updates or deletes from here on.
        with sqlite3.connect(state) as db:
            db.executescript("""
                CREATE TABLE audit(op TEXT, coll TEXT);
                CREATE TRIGGER audit_i AFTER INSERT ON row BEGIN
                    INSERT INTO audit VALUES ('insert', NEW.coll); END;
                CREATE TRIGGER audit_u AFTER UPDATE ON row BEGIN
                    INSERT INTO audit VALUES ('update', NEW.coll); END;
                CREATE TRIGGER audit_d AFTER DELETE ON row BEGIN
                    INSERT INTO audit VALUES ('delete', OLD.coll); END;
            """)
        server = Instance(catalog, state=state, wait=180)
        # A write is saved before it is answered; finding what changed must not cost
        # the whole library. With every row serialised to diff it, each of these took
        # 2.7 s on the test host against 2.2 ms without a state file.
        took = []
        for index in range(5):
            started = time.monotonic()
            server.request("GET", "/:/rate?key=%d&identifier=%s&rating=3" % (
                600200 + index, LIBRARY))
            took.append(time.monotonic() - started)
        took.sort()
        check("a write on a 20,000-row catalog is saved without serialising every row",
              took[2] < 0.5, "median %.3f s of %s" % (took[2], ["%.3f" % t for t in took]))
        server.stop()
        with sqlite3.connect(state) as db:
            db.execute("DELETE FROM audit")
        server = Instance(catalog, state=state, wait=180)
        status = server.request(
            "GET", "/:/rate?key=600123&identifier=%s&rating=8" % LIBRARY)[0]
        rated = first(server.request("GET", "/library/metadata/600123")[1]).get("userRating")
        server.stop(signal.SIGKILL)
        with sqlite3.connect(state) as db:
            touched = db.execute("SELECT op, coll FROM audit").fetchall()
        check(name, status == 200 and rated == 8 and 0 < len(touched) <= 10,
              "status %s, userRating %s, %d rows: %s" % (status, rated, len(touched), touched[:12]))
        server = Instance(catalog, state=state, wait=180)
        kept = first(server.request("GET", "/library/metadata/600123")[1]).get("userRating")
        check("that rating is restored after a hard kill", kept == 8, "userRating %s" % kept)
        server.stop()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def legacy_migration():
    name = "a legacy state.json is imported into rex.db and kept aside"
    work, catalog = workdir()
    try:
        legacy = os.path.join(work, "state.json")
        shutil.copy(os.path.join(HERE, "fixtures", "legacy_state_native.json"), legacy)
        server = Instance(catalog, default=True)
        restored = (watched(server),
                    first(server.request("GET", "/library/metadata/1008")[1]).get("title"))
        server.stop()
        database = os.path.join(work, "rex.db")
        check("the legacy state is served after the import",
              restored == (1, "Legacy Title"), "got %s" % (restored,))
        check(name, is_sqlite(database) and os.path.exists(legacy + ".migrated")
              and not os.path.exists(legacy), "files %s" % sorted(os.listdir(work)))
        server = Instance(catalog, default=True)
        again = (watched(server),
                 first(server.request("GET", "/library/metadata/1008")[1]).get("title"))
        server.stop()
        check("the imported state survives the next restart from the database alone",
              again == (1, "Legacy Title"), "got %s" % (again,))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# Reads whose answers depend on the catalog alone, on any fresh server.
SAME = ("/library/sections", "/library/sections/1/all", "/library/sections/2/all",
        "/library/metadata/1007", "/library/metadata/20001/children",
        "/library/metadata/30001/children", "/library/metadata/1001",
        "/library/sections/1/all?sort=titleSort", "/hubs/search?query=the")


def database_catalog():
    work, source = workdir()
    try:
        database = os.path.join(work, "library.db")
        converted, detail = convert(source, database)
        check("rex catalog --convert writes the catalog into a database", converted, detail)
        if not converted:
            return
        plain = Instance(source)
        from_json = {path: plain.request("GET", path) for path in SAME}
        plain.stop()
        served = Instance(database)
        from_db = {path: served.request("GET", path) for path in SAME}
        served.stop()
        differ = [path for path in SAME if from_json[path] != from_db[path]]
        check("a database catalog serves exactly what its JSON source serves",
              not differ and all(from_db[path][0] == 200 for path in SAME),
              "differ %s; statuses %s" % (differ, [from_db[p][0] for p in SAME]))
        server = Instance(database, default=True)
        scrobble(server)
        server.stop()
        server = Instance(database, default=True)
        kept = watched(server)
        server.stop()
        extra = sorted(name for name in os.listdir(work)
                       if name not in ("catalog.json", "library.db", "library.db.lock")
                       and not name.startswith("library.db-"))
        check("with a database catalog the state lives in the same file",
              kept == 1 and not extra, "viewCount %s, other files %s" % (kept, extra))

        # Rebuild the catalog inside the same database: a new row, new bytes.
        document = json.load(open(source))
        document["items"].append(dict(copy.deepcopy(document["items"][0]),
                                      ratingKey="1999", key="/library/metadata/1999",
                                      title="Rebuilt Row"))
        with open(source, "w") as handle:
            json.dump(document, handle, indent=2)
        converted, detail = convert(source, database)
        server = Instance(database, default=True)
        carried = watched(server)
        added = first(server.request("GET", "/library/metadata/1999")[1]).get("title")
        server.stop()
        check("a catalog rebuilt in the database carries view state and keeps a .stale copy",
              converted and carried == 1 and added == "Rebuilt Row"
              and is_sqlite(database + ".stale"),
              "viewCount %s, new row %s, files %s %s" % (
                  carried, added, sorted(os.listdir(work)), detail))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    default_database()
    one_write_touches_few_rows()
    legacy_migration()
    database_catalog()
    failed = results.count(False)
    print("\n%d/%d passed" % (len(results) - failed, len(results)))
    sys.exit(1 if failed else 0)
