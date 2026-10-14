#!/usr/bin/env python3
"""rex catalog: a rebuild keeps the server's identity.

A catalog's `machineIdentifier` is how clients and plex.tv know the server; the builder
derived it from `--name` every time, so rebuilding a managed server's catalog (its
`build`) or an old catalog built under another name made it a different server.

Drives `rust/target/release/rex catalog` (build it first); the databases are read with
sqlite3 directly.

    python3 tests/test_build_catalog.py
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REX = os.path.join(ROOT, "rust", "target", "release", "rex.exe" if os.name == "nt" else "rex")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)))


def catalog(*flags, env=None):
    return subprocess.run([REX, "catalog"] + list(flags), check=True, stdout=subprocess.DEVNULL,
                          env=env)


def database_machine(path):
    db = sqlite3.connect(path)
    try:
        row = db.execute("SELECT body FROM doc WHERE name = 'catalog:machineIdentifier'").fetchone()
    finally:
        db.close()
    return json.loads(row[0])


def machine_of(path):
    with open(path, "rb") as handle:
        if handle.read(16) == b"SQLite format 3\x00":
            return database_machine(path)
    return json.load(open(path, encoding="utf-8"))["machineIdentifier"]


def placeholder(machine):
    return machine.strip("0") in ("", "1")


if not os.path.exists(REX):
    raise SystemExit("%s is not built: run `cargo build --release --manifest-path rust/Cargo.toml`" % REX)

work = tempfile.mkdtemp(prefix="rex-build-")
try:
    # -- a re-seed keeps the catalog's machine, JSON or database ---------------------------
    source = os.path.join(HERE, "catalog_ui.json")
    machine = json.load(open(source, encoding="utf-8"))["machineIdentifier"]
    as_json = os.path.join(work, "catalog.json")
    shutil.copy(source, as_json)
    as_db = os.path.join(work, "rex.db")
    catalog("--convert", source, "--out", as_db)
    db = sqlite3.connect(as_db)
    try:
        rows = db.execute("SELECT count(*) FROM row WHERE coll = 'catalog:items'").fetchone()[0]
        digest = db.execute("SELECT value FROM meta WHERE key = 'catalogDigest'").fetchone()[0]
    finally:
        db.close()
    raw = open(source, "rb").read()
    check("--convert writes a row per item, under the source's SHA-1",
          rows == len(json.loads(raw)["items"]) and digest == hashlib.sha1(raw).hexdigest(), (rows, digest))
    catalog("--seed", "--out", as_json)
    check("a JSON catalog's machine is kept", machine_of(as_json) == machine, machine_of(as_json))
    catalog("--seed", "--out", as_db)
    check("a database catalog's machine is kept", machine_of(as_db) == machine, machine_of(as_db))
    new = os.path.join(work, "new.db")
    catalog("--seed", "--out", new)
    check("no catalog yet: a machine of its own", machine_of(new) not in (None, machine)
          and len(machine_of(new)) == 40, machine_of(new))
    empty = os.path.join(work, "empty.db")
    open(empty, "wb").close()
    catalog("--seed", "--out", empty)
    check("an empty file: a machine of its own", len(machine_of(empty)) == 40
          and not placeholder(machine_of(empty)), machine_of(empty))

    # -- a re-seed keeps the machine whatever the locale's encoding --------------------
    # Python opened text in the locale's encoding unless told (cp1252 on Windows): a JSON
    # catalog with a non-ASCII title failed to decode, the builder took that for "no
    # catalog", and the re-seed minted a new machineIdentifier, a different server to Plex.
    accented = os.path.join(work, "accented.json")
    json.dump({"machineIdentifier": "f" * 40, "friendlyName": "Café Ümlaut", "sections": [], "items": []},
              open(accented, "w", encoding="utf-8"), ensure_ascii=False)
    catalog("--seed", "--out", accented, env=dict(os.environ, LC_ALL="C", LANG="C"))
    reseeded = json.load(open(accented, encoding="utf-8"))["machineIdentifier"]
    check("a re-seed of a non-ASCII JSON catalog keeps its machine in any locale", reseeded == "f" * 40, reseeded)

    # -- a new seed's machine is its own --------------------------------------------------
    # It was sha1("plexsim/" + --name): plex.tv could predict it, and two servers given the
    # same name shared one identity, so a claim or an announce on one wrote the other's row.
    def seeded_machine(out, *flags):
        catalog("--seed", "--name", "den", "--out", out, *flags)
        return json.load(open(out, encoding="utf-8"))["machineIdentifier"]
    first = seeded_machine(os.path.join(work, "den1.json"))
    second = seeded_machine(os.path.join(work, "den2.json"))
    check("two seeds with the same name get different machines, each 40 hex",
          first != second and all(len(m) == 40 and set(m) <= set("0123456789abcdef") for m in (first, second)),
          (first, second))
    check("and a re-seed keeps its own", seeded_machine(os.path.join(work, "den1.json")) == first)
    example = os.path.join(work, "example.json")
    shutil.copy(os.path.join(ROOT, "examples", "catalog.example.json"), example)
    replaced = seeded_machine(example)
    check("a re-seed over the example catalog's placeholder machine mints a real one",
          not placeholder(replaced) and len(replaced) == 40, replaced)

    # -- --seed: one library per type, holding every folder under every mount ------------
    def seed(*flags):
        out = os.path.join(work, "seed.json")
        if os.path.exists(out):
            os.remove(out)
        catalog("--seed", "--out", out, *flags)
        return {section["type"]: section["Location"] for section in json.load(open(out))["sections"]}

    check("one mount: the locations the seed always wrote",
          seed("--zurg-mount", "/mnt/zurg") == {"movie": [{"id": 1, "path": "/mnt/zurg/movies"}],
                                                 "show": [{"id": 2, "path": "/mnt/zurg/shows"}]},
          seed("--zurg-mount", "/mnt/zurg"))
    # A production server's two libraries, read from its rex.db: three zurgs, anime beside shows.
    prod = seed("--mount", "/mnt/zurg", "--mount", "/mnt/zurg_ad/", "--mount", "/mnt/zurg_tb",
               "--shows", "shows", "--shows", "anime")
    check("several mounts and folders: each library holds every folder under every mount",
          [row["path"] for row in prod["movie"]] == ["/mnt/zurg/movies", "/mnt/zurg_ad/movies", "/mnt/zurg_tb/movies"]
          and [row["path"] for row in prod["show"]] == ["/mnt/zurg/shows", "/mnt/zurg/anime", "/mnt/zurg_ad/shows",
                                                       "/mnt/zurg_ad/anime", "/mnt/zurg_tb/shows", "/mnt/zurg_tb/anime"],
          prod)
    ids = [row["id"] for rows in prod.values() for row in rows]
    check("and every location has its own id", len(set(ids)) == len(ids) == 9, ids)
finally:
    shutil.rmtree(work, ignore_errors=True)
failed = results.count(False)
print("\n%d/%d passed" % (len(results) - failed, len(results)))
sys.exit(1 if failed else 0)
