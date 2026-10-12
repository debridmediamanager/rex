#!/usr/bin/env python3
"""Write rex's catalog: the server's identity and its libraries.

The library itself is what rex's sources push (docs/feed-api.md). A pushed item lands in
the library holding the directory its path starts with (`movies/<release>/<file>`), so a
new server's catalog is a seed: a machineIdentifier, a name, and a movie and a show
library naming those directories:

    ./build_catalog.py --seed --name den --out rex.db
    ./build_catalog.py --seed --name den --shows shows --shows anime --out rex.db

`--mount` (repeatable) is the prefix the folders are shown with. zurg's own Plex matcher
compares full paths, so it is zurg's `mount_path` when zurg's `plex_server_url` is rex.

An `--out` ending in `.db` writes the catalog into a SQLite database instead (see
catalogdb.py), which the server takes as `--catalog` and keeps its state in too. An
existing JSON catalog is moved into one with

    ./build_catalog.py --convert catalog.json --out rex.db
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import catalogdb  # noqa: E402
import hierarchy as hierarchy_module  # noqa: E402
import metadata as metadata_module  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "catalog.json"))
    parser.add_argument("--name", default="rex", help="friendlyName the clients show")
    parser.add_argument("--seed", action="store_true",
                        help="write an empty library for a zurg-backed server")
    parser.add_argument("--mount", "--zurg-mount", dest="mounts", action="append",
                        help="with --seed: the prefix the library folders are shown with (zurg's "
                             "mount_path, for zurg's matcher); repeat for several. Default /mnt/zurg")
    parser.add_argument("--movies", action="append",
                        help="with --seed: a directory the movie library holds under each "
                             "mount; repeat for several. Default movies")
    parser.add_argument("--shows", action="append",
                        help="with --seed: a directory the show library holds under each "
                             "mount; repeat for several. Default shows")
    parser.add_argument(
        "--convert",
        metavar="JSON",
        help="write an existing JSON catalog into the --out database and stop",
    )
    parser.add_argument(
        "--relink",
        action="store_true",
        help="only relink the guids of the existing --out catalog to Plex's (needs PLEX_TOKEN)",
    )
    args = parser.parse_args()

    if args.convert:
        with open(args.convert, "rb") as handle:
            source = handle.read()
        document = json.loads(source)
        catalogdb.write(args.out, document, source_bytes=source)
        print("wrote %s: %d item(s) from %s" % (args.out, len(document.get("items") or []),
                                                 args.convert))
        return

    if args.relink:
        document = load_catalog(args.out)
        count = relink_guids(document["items"], args.out)
        save_catalog(args.out, document)
        print("relinked %d row(s) in %s" % (count, args.out))
        return

    if not args.seed:
        parser.error("nothing to do: --seed, --convert or --relink")
    # machineIdentifier must be stable across restarts: plex.tv keys the registered
    # device on it, and a client that saw the old one treats a new value as a different
    # server -- so a re-seed keeps the one already there.
    # "plexsim/" is the pre-rename namespace, frozen: changing it re-mints every id it seeds.
    machine = existing_machine(args.out) or hashlib.sha1(
        ("plexsim/" + args.name).encode()).hexdigest()[:40]
    sections = hierarchy_module.sections(args.name)
    seed_locations(sections, args.mounts or ["/mnt/zurg"], args.movies or ["movies"],
                   args.shows or ["shows"])
    save_catalog(args.out, {"machineIdentifier": machine, "friendlyName": args.name,
                            "sections": sections, "items": []})
    print("wrote %s: an empty library, machine=%s" % (args.out, machine))


def seed_locations(sections, mounts, movies, shows):
    """Each library holds every one of its folders under every mount. zurg's own Plex
    integration finds a library by its location, and the feed places an item by its
    `<mount>/<directory>/<release>/<file>`. Location ids are numbered across libraries,
    so one mount and one folder each keeps the ids the seed always wrote (1 and 2)."""
    next_id = 1
    for section, folders in zip(sections, (movies, shows)):
        section["Location"] = []
        for mount in mounts:
            for folder in folders:
                section["Location"].append({"id": next_id,
                                            "path": "%s/%s" % (mount.rstrip("/"), folder.strip("/"))})
                next_id += 1


def existing_machine(out):
    """The machineIdentifier of the catalog already at `out`, if any. A re-seed (the
    manager's `build`) must not change it: clients and plex.tv know the server by it,
    and a new one is a different server to them."""
    try:
        return (load_catalog(out) or {}).get("machineIdentifier") if os.path.exists(out) else None
    except (OSError, ValueError, sqlite3.Error):
        return None


def is_database(path):
    return path.endswith(".db") or catalogdb.is_sqlite(path)


def load_catalog(path):
    if catalogdb.is_sqlite(path):
        return catalogdb.read(path)
    with open(path) as handle:
        return json.load(handle)


def save_catalog(path, document):
    """`catalog.json`, or the catalog half of a database (its state is left alone)."""
    if is_database(path):
        catalogdb.write(path, document)
        return
    with open(path, "w") as handle:
        json.dump(document, handle, indent=2)


def relink_guids(items, out):
    """Give the rows Plex's own guids (metadata.PlexProvider); a no-op without PLEX_TOKEN."""
    token = os.environ.get("PLEX_TOKEN")
    if not token:
        print("no PLEX_TOKEN: guids stay minted, and the Android app's detail pages stay empty")
        return 0
    provider = metadata_module.PlexProvider(
        token, os.path.join(os.path.dirname(os.path.abspath(out)), "plex_cache.json"))
    return metadata_module.apply_plex_guids(items, provider)


if __name__ == "__main__":
    main()
