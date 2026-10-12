#!/usr/bin/env python3
"""build_catalog.py: a rebuild keeps the server's identity.

A catalog's `machineIdentifier` is how clients and plex.tv know the server; the builder
derived it from `--name` every time, so rebuilding a managed server's catalog (its
`build`) or an old catalog built under another name made it a different server.

    python3 tests/test_build_catalog.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import build_catalog  # noqa: E402
import catalogdb  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "  -- " + str(detail)))


work = tempfile.mkdtemp(prefix="rex-build-")
try:
    document = json.load(open(os.path.join(HERE, "catalog_ui.json")))
    machine = document["machineIdentifier"]
    as_json = os.path.join(work, "catalog.json")
    json.dump(document, open(as_json, "w"))
    as_db = os.path.join(work, "rex.db")
    catalogdb.write(as_db, document)
    check("a JSON catalog's machine is kept", build_catalog.existing_machine(as_json) == machine)
    check("a database catalog's machine is kept", build_catalog.existing_machine(as_db) == machine)
    check("no catalog yet: none", build_catalog.existing_machine(os.path.join(work, "new.db")) is None)
    empty = os.path.join(work, "empty.db")
    open(empty, "wb").close()
    check("an empty file: none", build_catalog.existing_machine(empty) is None)

    # -- a re-seed keeps the machine whatever the locale's encoding --------------------
    # Python opens text in the locale's encoding unless told (cp1252 on Windows): a JSON
    # catalog with a non-ASCII title failed to decode, existing_machine took that for "no
    # catalog", and the re-seed minted a new machineIdentifier, a different server to Plex.
    accented = os.path.join(work, "accented.json")
    json.dump({"machineIdentifier": "f" * 40, "friendlyName": "Café Ümlaut", "sections": [], "items": []},
              open(accented, "w", encoding="utf-8"), ensure_ascii=False)
    subprocess.run([sys.executable, "-X", "utf8=0", os.path.join(os.path.dirname(HERE), "build_catalog.py"),
                    "--seed", "--out", accented], check=True, stdout=subprocess.DEVNULL,
                   env=dict(os.environ, LC_ALL="C", PYTHONCOERCECLOCALE="0", PYTHONUTF8="0"))
    reseeded = json.load(open(accented, encoding="utf-8"))["machineIdentifier"]
    check("a re-seed of a non-ASCII JSON catalog keeps its machine in any locale", reseeded == "f" * 40, reseeded)

    # -- --seed: one library per type, holding every folder under every mount ------------
    def seed(*flags):
        out = os.path.join(work, "seed.json")
        if os.path.exists(out):
            os.remove(out)
        subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "build_catalog.py"),
                        "--seed", "--out", out] + list(flags), check=True, stdout=subprocess.DEVNULL)
        return {section["type"]: section["Location"] for section in json.load(open(out))["sections"]}

    check("one mount: the locations the seed always wrote",
          seed("--zurg-mount", "/mnt/zurg") == {"movie": [{"id": 1, "path": "/mnt/zurg/movies"}],
                                                 "show": [{"id": 2, "path": "/mnt/zurg/shows"}]},
          seed("--zurg-mount", "/mnt/zurg"))
    # fun's two libraries, read from its rex.db: three zurgs, anime beside shows.
    fun = seed("--mount", "/mnt/zurg", "--mount", "/mnt/zurg_ad/", "--mount", "/mnt/zurg_tb",
               "--shows", "shows", "--shows", "anime")
    check("several mounts and folders: each library holds every folder under every mount",
          [row["path"] for row in fun["movie"]] == ["/mnt/zurg/movies", "/mnt/zurg_ad/movies", "/mnt/zurg_tb/movies"]
          and [row["path"] for row in fun["show"]] == ["/mnt/zurg/shows", "/mnt/zurg/anime", "/mnt/zurg_ad/shows",
                                                       "/mnt/zurg_ad/anime", "/mnt/zurg_tb/shows", "/mnt/zurg_tb/anime"],
          fun)
    ids = [row["id"] for rows in fun.values() for row in rows]
    check("and every location has its own id", len(set(ids)) == len(ids) == 9, ids)
finally:
    shutil.rmtree(work, ignore_errors=True)
failed = results.count(False)
print("\n%d/%d passed" % (len(results) - failed, len(results)))
sys.exit(1 if failed else 0)
