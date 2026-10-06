#!/usr/bin/env python3
"""Build plexsim's catalog.json from real Real-Debrid content.

For each restricted `/d/` link: unrestrict it, ffprobe the CDN URL over HTTP
(Range reads only -- nothing is downloaded), and emit a Plex metadata row whose
Media/Part/Stream attributes come from that probe rather than from a guess.
See mediainfo.py for why the numbers have to be real.

    RD_TOKEN=... ./build_catalog.py --link RESTRICTED_LINK ...

An `--out` ending in `.db` writes the catalog into a SQLite database instead (see
catalogdb.py), which the server takes as `--catalog` and keeps its state in too. An
existing JSON catalog is moved into one with

    ./build_catalog.py --convert catalog.json --out plexsim.db
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import catalogdb  # noqa: E402
import hierarchy as hierarchy_module  # noqa: E402
import metadata as metadata_module  # noqa: E402
from debrid import Resolver  # noqa: E402
from mediainfo import build_media  # noqa: E402

# Release-name noise stripped when deriving a display title.
_NOISE = re.compile(
    r"(?i)\b(1080p|2160p|720p|480p|4k|web[\s._-]?dl|webrip|bluray|blu[\s._-]?ray|remux|"
    r"hdtv|amzn|nf|hmax|glbo|dsnp|atvp|max|hevc|x264|x265|h[\s._-]?264|h[\s._-]?265|av1|"
    r"ddp?5[\s._-]?1|dd5[\s._-]?1|aac2[\s._-]?0|aac|ac3|eac3|truehd|atmos|opus|dts(?:[\s._-]?hd)?|"
    r"dv|hdr10\+?|hdr|proper|repack|imax|extended|dual|multi|subsplease|nanakoraws)\b"
)

# Plex's measured stacked-file convention. Cycle 36 made `Title - cd1.ext` and
# `Title - cd2.ext` on both writable references: they became one Media with two
# Parts in numeric order, and the Media/item duration became the Parts' sum.
_STACKED = re.compile(r"(?i)^(?P<base>.+?)\s*-\s*cd(?P<number>[1-9][0-9]*)"
                      r"(?P<extension>\.[A-Za-z0-9]{2,5})$")


def stacked_name(filename):
    """Return `(stable group key, part number, display filename)`, or None."""
    match = _STACKED.match(filename)
    if not match:
        return None
    base = match.group("base").strip()
    extension = match.group("extension")
    return base.casefold(), int(match.group("number")), base + extension


def append_stacked_part(first, later):
    """Merge a later measured stack member into the first row."""
    target = (first.get("Media") or [None])[0]
    source = (later.get("Media") or [None])[0]
    if not target or not source or not source.get("Part"):
        return False
    target.setdefault("Part", []).extend(source["Part"])
    target["duration"] = sum(int(part.get("duration") or 0)
                             for part in target["Part"])
    first["duration"] = target["duration"]
    return True


def probe(url):
    """ffprobe a remote URL. Reads only what the demuxer asks for."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            url,
        ],
        capture_output=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError("ffprobe failed: %s" % result.stderr.decode()[:400])
    return json.loads(result.stdout)


def pretty_title(filename):
    """Release filename -> something a library grid can show.

    Deliberately crude: plexsim is a protocol harness, and a real deployment
    would take the title from the catalog that produced the link (dmm already
    has the IMDb/TMDB identity) rather than re-parsing a release name here.
    """
    stem = re.sub(r"\.(mkv|mp4|webm|avi|ts)$", "", filename, flags=re.I)
    stem = stem.replace(".", " ").replace("_", " ")
    stem = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", stem)
    stem = _NOISE.sub(" ", stem)
    stem = re.sub(r"-\s*\w+$", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -")

    year = None
    match = re.search(r"\b(19\d{2}|20\d{2})\b", stem)
    if match:
        year = int(match.group(1))
        stem = stem.replace(match.group(1), " ")

    episode = re.search(r"(?i)\bS(\d{1,2})[\s._-]?E(\d{1,3})\b", stem)
    if episode:
        stem = stem[: episode.start()].strip(" -")
    stem = re.sub(r"\s+", " ", stem).strip(" -")

    return stem or filename, year, episode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--link", action="append", default=[], help="restricted RD /d/ link")
    parser.add_argument("--torrent", action="append", default=[], help="RD torrent id")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "catalog.json"))
    parser.add_argument("--name", default="plexsim", help="friendlyName the clients show")
    parser.add_argument(
        "--tmdb-key",
        default=os.environ.get("TMDB_KEY"),
        help="TMDB api key; without it rows keep their probe-derived titles only",
    )
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

    token = os.environ.get("RD_TOKEN")
    if not token:
        parser.error("RD_TOKEN must be set")

    links = list(args.link)
    for torrent_id in args.torrent:
        request = urllib.request.Request(
            "https://api.real-debrid.com/rest/1.0/torrents/info/" + torrent_id,
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            info = json.load(response)
        links.extend(info.get("links") or [])

    if not links:
        parser.error("no links to build from")

    resolver = Resolver(token)
    tmdb = None
    if args.tmdb_key:
        tmdb = metadata_module.Tmdb(
            args.tmdb_key,
            os.path.join(os.path.dirname(os.path.abspath(args.out)), "tmdb_cache.json"),
        )
    else:
        print("no TMDB key: rows will carry filename-derived titles and no artwork")

    # machineIdentifier must be stable across restarts: plex.tv keys the
    # registered device on it, and a client that saw the old one treats a new
    # value as a different server.
    machine = hashlib.sha1(("plexsim/" + args.name).encode()).hexdigest()[:40]

    sections = hierarchy_module.sections(args.name)

    items = []
    entries = []
    stacks = {}
    rating_key = 1000
    for link in links:
        print("resolving %s" % link, flush=True)
        try:
            url, size = resolver.resolve(link)
        except Exception as error:
            print("  SKIP unrestrict failed: %s" % error, flush=True)
            continue

        filename = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        stacked = stacked_name(filename)
        display_filename = stacked[2] if stacked else filename
        if filename.lower().endswith(".rar"):
            # RD serves some torrents only as an archive; the bytes are a RAR
            # stream, not a media container, so a player cannot direct-play it.
            # Virtualizing those is zurg's job, not something to fake here.
            print("  SKIP archive-backed (%s)" % filename, flush=True)
            continue

        print("  probing %s" % filename, flush=True)
        try:
            probed = probe(url)
        except Exception as error:
            print("  SKIP probe failed: %s" % error, flush=True)
            continue

        if not any(s.get("codec_type") == "video" for s in probed.get("streams") or []):
            print("  SKIP no video stream", flush=True)
            continue

        rating_key += 1
        part_id = rating_key * 10
        parsed = metadata_module.parse_release(display_filename)
        title = parsed["title"] or display_filename
        year = parsed["year"]
        if parsed["season"] and parsed["episode"]:
            title = "%s (S%02dE%02d)" % (title, parsed["season"], parsed["episode"])
        elif parsed["episode"]:
            title = "%s (%d)" % (title, parsed["episode"])

        media = build_media(
            probed,
            media_id=rating_key * 100,
            part_id=part_id,
            part_key="/library/parts/%d/%d/file.%s"
            % (part_id, int(time.time()), _extension(filename)),
            file_path="/plexsim/1/" + filename,
            size=int(probed.get("format", {}).get("size") or size),
            container_hint=_extension(filename),
        )
        media["Part"][0]["_debrid"] = link

        row = {
                "ratingKey": str(rating_key),
                "key": "/library/metadata/%d" % rating_key,
                "guid": "plexsim://movie/%d" % rating_key,
                "type": "movie",
                "title": title,
                # No titleSort and no originalTitle here: a real PMS sends the first
                # only when it differs from the title, and the second is the
                # original-language title, never the file name. The normalizer in
                # hierarchy.py derives titleSort once the title is final.
                "year": year,
                "summary": "Served by plexsim from a debrid link. Source file: %s" % filename,
                "duration": media["duration"],
                "addedAt": int(time.time()),
                "updatedAt": int(time.time()),

                "thumb": "/library/metadata/%d/thumb/1" % rating_key,
                "art": "/library/metadata/%d/art/1" % rating_key,
                "contentRating": "NR",
                "Media": [media],
            }

        identified = False
        if tmdb is not None:
            try:
                identified = metadata_module.enrich(row, tmdb, parsed)
            except Exception as error:
                print("  metadata lookup failed: %s" % error, flush=True)
        if stacked:
            group, part_number, _display = stacked
            if group not in stacks:
                stacks[group] = []
                entries.append(("stack", group))
            stacks[group].append((part_number, row))
        else:
            entries.append(("row", row))
        stream_summary = "%s %s %dx%d %s" % (
            media["container"],
            media["videoCodec"],
            media["width"] or 0,
            media["height"] or 0,
            media["audioCodec"],
        )
        mark = "identified as %s" % row.get("guid") if identified else "NOT identified"
        print("  OK  %-30s [%s, %s kbps] %s"
              % (row.get("title", title)[:30], stream_summary, media["bitrate"], mark), flush=True)

    # A torrent's link order is not part of the measured convention; numeric cdN
    # order is. Pick cd1's row as the Media template and append every later Part.
    for kind, value in entries:
        if kind == "row":
            items.append(value)
            continue
        members = sorted(stacks[value], key=lambda pair: pair[0])
        row = members[0][1]
        for _number, later in members[1:]:
            if not append_stacked_part(row, later):
                print("  SKIP invalid stack member: %s" % later.get("title"), flush=True)
        items.append(row)

    # Flat leaves -> movies + show/season/episode rows.
    items = hierarchy_module.build(items)
    relink_guids(items, args.out)
    kinds = {}
    for row in items:
        kinds[row["type"]] = kinds.get(row["type"], 0) + 1
    print("\nlibrary shape: " + ", ".join("%d %s" % (v, k) for k, v in sorted(kinds.items())))

    document = {
        "machineIdentifier": machine,
        "friendlyName": args.name,
        "sections": sections,
        "items": items,
    }
    save_catalog(args.out, document)

    print("\nwrote %s: %d item(s), machine=%s" % (args.out, len(items), machine))


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


def _extension(filename):
    match = re.search(r"\.([A-Za-z0-9]{2,5})$", filename)
    return (match.group(1) if match else "mkv").lower()


if __name__ == "__main__":
    main()
