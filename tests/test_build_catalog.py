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


work = tempfile.mkdtemp(prefix="plexsim-build-")
try:
    document = json.load(open(os.path.join(HERE, "catalog_ui.json")))
    machine = document["machineIdentifier"]
    as_json = os.path.join(work, "catalog.json")
    json.dump(document, open(as_json, "w"))
    as_db = os.path.join(work, "plexsim.db")
    catalogdb.write(as_db, document)
    check("a JSON catalog's machine is kept", build_catalog.existing_machine(as_json) == machine)
    check("a database catalog's machine is kept", build_catalog.existing_machine(as_db) == machine)
    check("no catalog yet: none", build_catalog.existing_machine(os.path.join(work, "new.db")) is None)
    empty = os.path.join(work, "empty.db")
    open(empty, "wb").close()
    check("an empty file: none", build_catalog.existing_machine(empty) is None)
finally:
    shutil.rmtree(work, ignore_errors=True)
failed = results.count(False)
print("\n%d/%d passed" % (len(results) - failed, len(results)))
sys.exit(1 if failed else 0)
