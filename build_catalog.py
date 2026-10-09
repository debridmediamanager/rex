#!/usr/bin/env python3
"""Write plexsim's catalog: the server's identity and its libraries.

The library itself is zurg's. The native server lists each library's folder on zurg's
WebDAV and adds what it finds -- probed by zurg, identified, placed -- whenever zurg
asks it to scan (zurg's `plex_server_url` pointed at plexsim), and at start. So a new
server's catalog is a seed: a machineIdentifier, a name, and a movie and a show
library whose locations are folders under zurg's mount:

    ./build_catalog.py --seed --name den --zurg-mount /mnt/zurg --out plexsim.db

An `--out` ending in `.db` writes the catalog into a SQLite database instead (see
catalogdb.py), which the server takes as `--catalog` and keeps its state in too. An
existing JSON catalog is moved into one with

    ./build_catalog.py --convert catalog.json --out plexsim.db
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
    parser.add_argument("--name", default="plexsim", help="friendlyName the clients show")
    parser.add_argument("--seed", action="store_true",
                        help="write an empty library for a zurg-backed server")
    parser.add_argument("--zurg-mount", default="/mnt/zurg", help="with --seed: zurg's mount_path")
    parser.add_argument("--movies", default="movies",
                        help="with --seed: the zurg directory the movie library holds")
    parser.add_argument("--shows", default="shows",
                        help="with --seed: the zurg directory the show library holds")
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
    machine = existing_machine(args.out) or hashlib.sha1(
        ("plexsim/" + args.name).encode()).hexdigest()[:40]
    sections = hierarchy_module.sections(args.name)
    mount = args.zurg_mount.rstrip("/")
    for section, folder in zip(sections, (args.movies, args.shows)):
        # zurg's own Plex integration finds a library by its location, and sends
        # `<mount>/<directory>/<release>` as the scan's path.
        section["Location"] = [{"id": int(section["key"]), "path": "%s/%s" % (mount, folder)}]
    save_catalog(args.out, {"machineIdentifier": machine, "friendlyName": args.name,
                            "sections": sections, "items": []})
    print("wrote %s: an empty library, machine=%s" % (args.out, machine))


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
