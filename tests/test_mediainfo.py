#!/usr/bin/env python3
"""mediainfo.py against a real PMS's own analysis of the same files.

Every fixture in tests/fixtures/media/ is one real file: the ffprobe probe
build_catalog.py takes of it, and the Media element a real PMS wrote for it --
production fun (1.42.2) for real releases, the plexlab reference (1.43.4) for its
synthetic clips -- both recorded on 2026-09-13, the probe over the server's own part
URL. `webDisplayTitles` is the same row read with `X-Plex-Product: Plex Web`.

Each check converts every probe with `mediainfo.build_media` exactly as
build_catalog.py does and compares the attributes one rule governs with the real
server's, presence and value. No server and no network.

    python3 tests/test_mediainfo.py [--root TREE] [--only CHECK[,CHECK]] [--list]

`--root` imports mediainfo from another tree: that is how a check is proven to fail
against the unfixed code.
"""

import argparse
import glob
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "media")

CHECKS = []


def check(name):
    def register(function):
        CHECKS.append((name, function))
        return function
    return register


def streams(media, kind=None):
    return [s for part in media.get("Part") or [] for s in part.get("Stream") or []
            if kind is None or s.get("streamType") == kind]


def paired(fixture, kind):
    """(real stream, plexsim stream) for every real stream that has a container index.

    An external subtitle a real PMS found beside the file has no index; plexsim has no
    sidecars, so it is not compared.
    """
    built = {s.get("index"): s for s in streams(fixture["sim"], kind)}
    for real in streams(fixture["Media"], kind):
        if real.get("index") is None:
            continue
        yield real, built.get(real["index"], {})


def compare(fixture, where, real, sim, fields):
    return [("%s %s %s" % (fixture["name"], where, field), real.get(field), sim.get(field))
            for field in fields if not skipped(fixture, real.get("index"), field)]


def per_stream(kind, fields, built_only=False):
    def rows(fixture):
        out = []
        for real, sim in paired(fixture, kind):
            if built_only and not sim:
                continue
            out += compare(fixture, "stream %s" % real["index"], real, sim, fields)
        return out
    return rows


def per_media(fields):
    def rows(fixture):
        return compare(fixture, "Media", fixture["Media"], fixture["sim"], fields)
    return rows


# Differences the probe cannot explain, each measured and each left alone. A rule's
# check skips exactly these (fixture, stream index, attribute) triples and no others.
SKIP = {
    # E-AC-3 with Atmos (JOC) in an mp4: ffprobe reports the 7.1 the decoder can
    # produce, the real server the 5.1 independent substream it indexed.
    ("fun-1051324-2", 2, "channels"): "E-AC-3 JOC in mp4",
    ("fun-1051324-2", 4, "channels"): "E-AC-3 JOC in mp4",
}


def skipped(fixture, index, field):
    return (fixture["name"], index, field) in SKIP


@check("baseline")
def _baseline(fixture):
    out = per_media(("width", "height", "videoCodec"))(fixture)
    out += per_stream(1, ("codec", "width", "height", "chromaSubsampling", "colorPrimaries",
                          "colorSpace", "colorTrc", "codedWidth"), built_only=True)(fixture)
    out += per_stream(2, ("channels", "samplingRate"))(fixture)
    return out


@check("subtitle-codec-names")
def _subtitle_codec_names(fixture):
    return per_stream(3, ("codec",))(fixture)


def load(root):
    if root:
        sys.path.insert(0, os.path.abspath(root))
    else:
        sys.path.insert(0, os.path.dirname(HERE))
    return importlib.import_module("mediainfo")


def fixtures(mediainfo):
    out = []
    for path in sorted(glob.glob(os.path.join(FIXTURES, "*.json"))):
        fixture = json.load(open(path))
        fixture["name"] = os.path.basename(path)[:-5]
        probe = fixture["probe"]
        extension = fixture["extension"]
        fixture["sim"] = mediainfo.build_media(
            probe, media_id=1, part_id=1,
            part_key="/library/parts/1/1/file.%s" % extension,
            file_path="file.%s" % extension,
            size=int((probe.get("format") or {}).get("size") or 0),
            container_hint=extension)
        out.append(fixture)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", help="import mediainfo from this tree")
    parser.add_argument("--only", help="comma-separated check names")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--examples", type=int, default=6)
    args = parser.parse_args()
    if args.list:
        print("\n".join(name for name, _ in CHECKS))
        return 0
    mediainfo = load(args.root)
    corpus = fixtures(mediainfo)
    wanted = set(args.only.split(",")) if args.only else None
    passed = total = 0
    for name, function in CHECKS:
        if wanted and name not in wanted:
            continue
        total += 1
        try:
            rows = [row for fixture in corpus for row in function(fixture, **(
                {"mediainfo": mediainfo} if "mediainfo" in function.__code__.co_varnames[
                    :function.__code__.co_argcount] else {}))]
        except Exception as error:  # a check that cannot run on a tree fails there
            print("FAIL %s: raised %s: %s" % (name, type(error).__name__, error))
            continue
        wrong = [row for row in rows if row[1] != row[2]]
        if rows and not wrong:
            passed += 1
            print("PASS %s (%d comparisons)" % (name, len(rows)))
            continue
        print("FAIL %s: %d of %d differ" % (name, len(wrong), len(rows)))
        for label, real, sim in wrong[:args.examples]:
            print("     %s: real %r, plexsim %r" % (label, real, sim))
    print("%d/%d checks passed" % (passed, total))
    return 0 if passed == total and total else 1


if __name__ == "__main__":
    sys.exit(main())
