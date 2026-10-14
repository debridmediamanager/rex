#!/usr/bin/env python3
"""The native mediainfo (`rust/src/mediainfo.rs`) against a real PMS's own analysis of the same files.

Every fixture in tests/fixtures/media/ is one real file: the ffprobe probe
build_catalog.py takes of it, and the Media element a real PMS wrote for it --
a production server (1.42.2) for real releases, the plexlab reference (1.43.4) for its
synthetic clips -- both recorded on 2026-09-13, the probe over the server's own part
URL. `webDisplayTitles` is the same row read with `X-Plex-Product: Plex Web`.

The `s42-*` fixtures are SHALLOW: cycle 54 made one small file per container on the
1.42.2 disposable copy (plexscratch42), because no real library holds a `.ts`, `.m4v`,
`.wmv`, `.mpg`, `.flv`, `.ogv`, `.3gp` or `.divx` part. That copy's analysis is the
scanner's quick pass -- even an explicit analyze leaves its streams without `bitDepth`,
colour or `codecID`, all of which the 1.42.2 reference writes for the same containers -- so its
STREAM fields are not a witness. Only the checks registered `shallow=True`, which read what the
container decides, run on them.

Each check converts every probe with the native CLI's `mediainfo` test adapter (`build_media`,
as the catalog is built) and compares the attributes one rule governs with the real
server's, presence and value; the rules below a single row are driven through the same
adapter with a one-stream probe, or through `stream-titles`. No server and no network.

    python3 tests/test_mediainfo.py [--bin REX] [--only CHECK[,CHECK]] [--list]

`--bin` checks another native binary (default `$REX_NATIVE_BIN`, else
`rust/target/release/rex`): that is how a check is proven to fail against the unfixed code.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "media")


class Native:
    """The native binary's mediainfo test adapters."""

    def __init__(self, binary):
        self.binary = binary

    def _run(self, document, *args):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(document, handle)
        try:
            result = subprocess.run([self.binary, *args, "--input", handle.name],
                                    capture_output=True, text=True, check=False)
        finally:
            os.unlink(handle.name)
        if result.returncode:
            raise RuntimeError("%s exited %d: %s" % (args[0], result.returncode, result.stderr.strip()))
        return json.loads(result.stdout)

    def build_media(self, probe, media_id, part_id, part_key, file_path, size, container_hint=None):
        args = ["mediainfo", "--media-id", str(media_id), "--part-id", str(part_id),
                "--part-key", part_key, "--file", file_path, "--size", str(size)]
        if container_hint:
            args += ["--container-hint", container_hint]
        return self._run(probe, *args)

    def built(self, *streams):
        """The Media `build_media` makes of a probe holding only these ffprobe streams."""
        return self.build_media({"streams": list(streams)}, 1, 1, "/library/parts/1/1/file.mkv",
                                "file.mkv", 0)

    def built_stream(self, stream):
        return self.built(stream)["Part"][0]["Stream"][0]

    def stream_titles(self, row):
        """`stream_titles` on one Stream row, which comes back with its titles."""
        return self._run([row], "stream-titles")[0]

    def client_display_titles(self, rows, product):
        """Each Stream row's `displayTitle` as `product` is sent it."""
        return [row.get("displayTitle") for row in self._run(rows, "stream-titles", "--product", product)]


CHECKS = []


def check(name, once=False, shallow=False):
    """Register a check. A `once` check is called once, not per fixture.

    `shallow` checks also run on the SHALLOW fixtures (see the module docstring)."""
    def register(function):
        function.shallow = shallow
        CHECKS.append((name, function, once))
        return function
    return register


def streams(media, kind=None):
    return [s for part in media.get("Part") or [] for s in part.get("Stream") or []
            if kind is None or s.get("streamType") == kind]


def paired(fixture, kind):
    """(real stream, rex stream) for every real stream that has a container index.

    An external subtitle a real PMS found beside the file has no index; rex has no
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
    # The reference's AAC clips: the production server writes `lc` for the same profile.
    ("kids-1-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-12-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-13-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-14-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-17-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-18-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-3-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-4-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-8-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    # The reference's untagged clips: the production server leaves an untagged track without a
    # language.
    ("kids-1-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-1-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-1-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-10-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-10-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-10-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-12-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-12-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-12-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-13-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-13-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-13-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-14-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-14-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-14-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-17-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-17-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-17-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-3-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-3-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-3-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-4-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-4-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-4-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-8-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-8-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-8-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-9-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-9-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-9-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    # Layouts the probe does not explain.
    ("fun-1051324-2", 2, "audioChannelLayout"): "E-AC-3 JOC in mp4",
    ("fun-1051324-2", 4, "audioChannelLayout"): "E-AC-3 JOC in mp4",
    ("fun-1061845-0", 1, "audioChannelLayout"): "DTS Express: the production server writes no layout; one track",
    ("kids-1-0", 1, "audioChannelLayout"): "the 1.43.4 reference leaves most mono AAC clips without a layout",
    ("kids-12-0", 1, "audioChannelLayout"): "the 1.43.4 reference leaves most mono AAC clips without a layout",
    ("kids-13-0", 1, "audioChannelLayout"): "the 1.43.4 reference leaves most mono AAC clips without a layout",
    ("kids-14-0", 1, "audioChannelLayout"): "the 1.43.4 reference leaves most mono AAC clips without a layout",
    ("kids-17-0", 1, "audioChannelLayout"): "the 1.43.4 reference leaves most mono AAC clips without a layout",
    ("kids-18-0", 1, "audioChannelLayout"): "the 1.43.4 reference leaves most mono AAC clips without a layout",
    ("kids-3-0", 1, "audioChannelLayout"): "the 1.43.4 reference leaves most mono AAC clips without a layout",
    ("kids-4-0", 1, "audioChannelLayout"): "the 1.43.4 reference leaves most mono AAC clips without a layout",
    ("kids-8-0", 1, "audioChannelLayout"): "the 1.43.4 reference leaves most mono AAC clips without a layout",
    # TrueHD in an mp4.
    ("fun-1051324-2", 5, "bitDepth"): "TrueHD in mp4: the production server writes no bit depth",
    # AVI tracks the probe gives no id.
    ("fun-1057221-0", 1, "streamIdentifier"): "an AVI track the production server numbers with no id in the probe",
    ("fun-1150826-0", 1, "streamIdentifier"): "an AVI track the production server numbers with no id in the probe",
    # The reference's untagged video tracks.
    ("kids-1-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-1-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-1-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-10-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-10-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-10-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-12-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-12-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-12-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-13-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-13-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-13-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-14-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-14-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-14-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-17-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-17-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-17-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-3-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-3-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-3-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-4-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-4-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-4-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-8-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-8-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-8-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-9-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-9-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-9-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    # Media bitrates the probe does not give.
    ("fun-1054688-1", None, "bitrate"): "neither rounded nor truncated: the production server's own measure",
    ("fun-1057221-0", None, "bitrate"): "neither rounded nor truncated: the production server's own measure",
    ("kids-1-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    ("kids-10-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    ("kids-12-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    ("kids-13-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    ("kids-14-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    ("kids-17-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    ("kids-18-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    ("kids-3-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    ("kids-4-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    ("kids-8-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    ("kids-9-0", None, "bitrate"): "the 1.43.4 reference's ten-second clips",
    # The Media's audio fields.
    ("fun-1051324-2", None, "audioCodec"): "E-AC-3 JOC in mp4: the probe's 7.1 outranks the DTS-HD MA track",
    ("fun-1051324-2", None, "audioChannels"): "E-AC-3 JOC in mp4: the probe's 7.1 outranks the DTS-HD MA track",
    ("fun-1051324-2", None, "audioProfile"): "E-AC-3 JOC in mp4: the probe's 7.1 outranks the DTS-HD MA track",
    ("kids-1-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-12-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-13-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-14-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-17-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-18-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-3-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-4-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    ("kids-8-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; the production server writes lc",
    # Titles built from a field the probe does not give.
    ("fun-1051324-2", 2, "displayTitle"): "E-AC-3 JOC in mp4: the production server prints its 5.1 core",
    ("fun-1051324-2", 2, "extendedDisplayTitle"): "E-AC-3 JOC in mp4: the production server prints its 5.1 core",
    ("fun-1051324-2", 4, "displayTitle"): "E-AC-3 JOC in mp4: the production server prints its 5.1 core",
    ("fun-1051324-2", 4, "extendedDisplayTitle"): "E-AC-3 JOC in mp4: the production server prints its 5.1 core",
    ("fun-1061845-0", 3, "extendedDisplayTitle"): "a forced track with no linguistic content leaves Forced out; one track",
    ("kids-1-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-1-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-10-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-10-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-12-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-12-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-13-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-13-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-14-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-14-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-17-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-17-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-3-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-3-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-4-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-4-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-8-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-8-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-9-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
    ("kids-9-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; the production server writes nothing",
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


@check("container", shallow=True)
def _container(fixture):
    """The Media's and the Part's `container`, against what the real PMS wrote.

    Cycle 44: this suite compared every stream field and the Media's geometry and never
    the container, so the corpus carried a refutation for thirty-four cycles without
    anyone reading it. `fun-1052407-0.json` is a **`.webm`** file whose recorded Media --
    a real 1.42.2 PMS's own answer for it -- says `mkv`, while rex's mediainfo overrode
    the demuxer's answer with the extension and produced `webm`. Confirmed live on the
    identical 571,346,576-byte file: the 1.42.2 reference answers `mkv`, and no row of its 4,508
    movie Media carries `webm` at all.
    """
    out = per_media(("container",))(fixture)
    real_part = ((fixture["Media"].get("Part") or [{}])[0])
    sim_part = ((fixture["sim"].get("Part") or [{}])[0])
    out += [("%s Part container" % fixture["name"],
             real_part.get("container"), sim_part.get("container"))]
    return out


@check("packet-length", shallow=True)
def _packet_length(fixture):
    """Cycle 54: an MPEG-TS Part carries `packetLength: 188` and no other Part does.

    Measured on all seventeen containers on plexscratch42, on the 1.42.2 reference's fourteen real
    `.m2ts` parts, and in `fun-1073414-1` -- which has recorded it since cycle 10 without any check
    reading it. It is 188 on a Blu-ray `.m2ts` too, whose packets are really 192 bytes, so
    it is a constant of the container and not a measurement of the file.
    """
    real = (fixture["Media"].get("Part") or [{}])[0]
    sim = (fixture["sim"].get("Part") or [{}])[0]
    return [("%s Part packetLength" % fixture["name"], real.get("packetLength"),
             sim.get("packetLength"))]


@check("subtitle-codec-names")
def _subtitle_codec_names(fixture):
    return per_stream(3, ("codec",))(fixture)


@check("audio-codec-names")
def _audio_codec_names(fixture):
    return per_stream(2, ("codec", "profile"))(fixture)


@check("language-fields")
def _language_field_values(fixture):
    fields = ("language", "languageCode", "languageTag")
    return per_stream(2, fields)(fixture) + per_stream(3, fields)(fixture)


DOVI_FIELDS = ("DOVIPresent", "DOVIProfile", "DOVILevel", "DOVIBLCompatID", "DOVIBLPresent",
               "DOVIELPresent", "DOVIRPUPresent", "DOVIVersion")


@check("video-streams")
def _video_stream_indexes(fixture):
    return [("%s video stream indexes" % fixture["name"],
             sorted(s["index"] for s in streams(fixture["Media"], 1) if "index" in s),
             sorted(s.get("index") for s in streams(fixture["sim"], 1)))]


@check("dovi")
def _dovi(fixture):
    return per_stream(1, DOVI_FIELDS)(fixture)


@check("video-bitdepth")
def _video_bit_depth(fixture):
    return per_stream(1, ("bitDepth",))(fixture)


@check("video-resolution")
def _video_resolution_label(fixture):
    return per_media(("videoResolution",))(fixture)


def video_resolution(native, height, width):
    """The `videoResolution` of a Media whose one video stream is `width`x`height`."""
    return native.built({"codec_type": "video", "codec_name": "h264", "index": 0,
                         "width": width, "height": height}).get("videoResolution")


@check("video-resolution-survey", once=True)
def _video_resolution_survey(native):
    survey = json.load(open(os.path.join(HERE, "fixtures", "media-resolutions.json")))
    return [("%dx%d (%d Media in production)" % (width, height, count), label,
             video_resolution(native, height, width))
            for width, height, label, count in survey["rows"]]


@check("cycle 41: the short-wide 480 resolution boundary", once=True)
def _short_wide_480_boundary(native):
    """Both disposable reference versions switch from SD to 480 at stored width 848.

    The boundary sweep used a fixed 360-pixel height and observed stored widths 840,
    842, 844 and 846 as ``sd`` and 848 as ``480``. Odd source widths were normalised
    down by analysis, so this tests the values PMS actually stores and rex reads.
    """
    return [("%dx360" % width, expected, video_resolution(native, 360, width))
            for width, expected in ((840, "sd"), (842, "sd"), (844, "sd"),
                                    (846, "sd"), (848, "480"))]


@check("pixel-aspect")
def _pixel_aspect_pair(fixture):
    """Cycle 25: `pixelAspectRatio` and `anamorphic` come as a PAIR, or not at all.

    The corpus holds two streams that carry them -- `333:320` with `anamorphic` false and
    `32:27` with it true -- and fifty-four that carry neither. rex sent neither field
    on any stream, so Get Info printed no pixel aspect and a client deciding whether it
    had to correct the aspect on playback was told nothing.
    """
    return per_stream(1, ("pixelAspectRatio", "anamorphic"), built_only=True)(fixture)


@check("pixel-aspect-threshold", once=True)
def _pixel_aspect_threshold(native):
    """The sixteen ratios the 1.42.2 reference witnesses, the two in the corpus, and the exact boundary.

    Measured on 1,688 of the 1.42.2 reference's video Streams (358 carrying the pair, 1,330 carrying
    neither, none carrying one). Cycle 41 then measured both disposable reference
    versions at the boundary: `21:20` is false and `20:19` is true.
    """
    rows = []
    for ratio, expected in (("1:1", None), ("0:1", None),
                            ("99:100", False), ("172:173", False), ("799:800", False),
                            ("816:817", False), ("872:873", False), ("959:960", False),
                            ("10439:10440", False), ("481:480", False), ("255:254", False),
                            ("643:640", False), ("333:320", False), ("21:20", False),
                            ("1:2", True), ("10:11", True), ("16:15", True),
                            ("20:19", True), ("495:464", True), ("721:640", True),
                            ("32:27", True),
                            ("2:1", True)):
        built = native.built_stream(
            {"codec_type": "video", "codec_name": "h264", "index": 0, "width": 1920,
             "height": 1080, "sample_aspect_ratio": ratio})
        rows.append(("sample_aspect_ratio %s" % ratio,
                     (ratio, expected) if expected is not None else (None, None),
                     (built.get("pixelAspectRatio"), built.get("anamorphic"))))
    return rows


@check("aspect-ratio")
def _aspect_ratio_value(fixture):
    return per_media(("aspectRatio",))(fixture)


@check("disposition-flags")
def _disposition_flags(fixture):
    flags = ("default", "forced", "hearingImpaired", "original", "dub", "comment")
    out = per_stream(1, ("default", "forced", "original"))(fixture)
    out += per_stream(2, flags)(fixture) + per_stream(3, flags)(fixture)
    for real, sim in paired(fixture, 2):
        if sim.get("selected") is False:
            out.append(("%s stream %s selected" % (fixture["name"], real["index"]), None, False))
    return out


@check("scan-type")
def _scan_type_value(fixture):
    return per_stream(1, ("scanType",))(fixture)


@check("color-range")
def _color_range(fixture):
    return per_stream(1, ("colorRange",))(fixture)


@check("channel-layout")
def _channel_layout(fixture):
    return per_stream(2, ("audioChannelLayout",))(fixture)


@check("audio-bitdepth")
def _audio_bit_depth(fixture):
    return per_stream(2, ("bitDepth",))(fixture)


@check("stream-identifier")
def _stream_identifier(fixture):
    return sum((per_stream(kind, ("streamIdentifier",))(fixture) for kind in (1, 2, 3)), [])


@check("codec-id")
def _codec_id(fixture):
    return sum((per_stream(kind, ("codecID",))(fixture) for kind in (1, 2, 3)), [])


@check("coded-height")
def _coded_height(fixture):
    return per_stream(1, ("codedHeight",))(fixture)


@check("video-language-title")
def _video_language_title(fixture):
    return per_stream(1, ("language", "languageCode", "languageTag", "title"))(fixture)


@check("unknown-markers")
def _unknown_markers(fixture):
    out = per_stream(1, ("profile", "level"))(fixture) + per_media(("videoProfile",))(fixture)
    real_part = fixture["Media"]["Part"][0]
    sim_part = (fixture["sim"].get("Part") or [{}])[0]
    return out + compare(fixture, "Part", real_part, sim_part, ("videoProfile",))


@check("bitrate-rounding")
def _bitrate_rounding(fixture):
    out = per_media(("bitrate",))(fixture)
    probe = {s["index"]: s for s in fixture["probe"]["streams"]}
    for kind in (1, 2, 3):
        for real, sim in paired(fixture, kind):
            raw = (probe.get(real["index"]) or {}).get("bit_rate")
            # Only where the probe's rate is the one a real PMS wrote, rounded or truncated;
            # the rest are the server's own measure.
            if raw and real.get("bitrate") in (int(int(raw) / 1000), round(int(raw) / 1000)):
                out += compare(fixture, "stream %s" % real["index"], real, sim, ("bitrate",))
    return out


@check("frame-rate")
def _frame_rate(fixture):
    return per_stream(1, ("frameRate",))(fixture) + per_media(("videoFrameRate",))(fixture)


@check("media-audio-track")
def _media_audio_track(fixture):
    out = per_media(("audioCodec", "audioChannels", "audioProfile"))(fixture)
    real_part = fixture["Media"]["Part"][0]
    sim_part = (fixture["sim"].get("Part") or [{}])[0]
    return out + compare(fixture, "Part", real_part, sim_part, ("audioProfile",))


@check("video-titles")
def _video_titles(fixture):
    return per_stream(1, ("displayTitle", "extendedDisplayTitle"))(fixture)


@check("audio-titles")
def _audio_titles(fixture):
    return per_stream(2, ("displayTitle", "extendedDisplayTitle"))(fixture)


@check("subtitle-titles")
def _subtitle_titles(fixture):
    return per_stream(3, ("displayTitle", "extendedDisplayTitle"))(fixture)


@check("client-titles")
def _client_titles(fixture, native):
    """`webDisplayTitles` is each stream's displayTitle as read with `X-Plex-Product: Plex Web`."""
    pairs = [(real, sim) for real, sim in paired(fixture, None)
             if not skipped(fixture, real["index"], "displayTitle")]
    built = [sim for _real, sim in pairs if sim]
    titles = {product: iter(native.client_display_titles(built, product) if built else [])
              for product in ("Plex Web", "Plex for Roku")}
    out = []
    for real, sim in pairs:
        web, roku = (next(titles["Plex Web"]), next(titles["Plex for Roku"])) if sim else (None, None)
        out.append(("%s stream %s Plex Web displayTitle" % (fixture["name"], real["index"]),
                    fixture["webDisplayTitles"].get(str(real["index"])), web))
        out.append(("%s stream %s Plex for Roku displayTitle" % (fixture["name"], real["index"]),
                    real.get("displayTitle"), roku))
    return out


def fixtures(native):
    out = []
    for path in sorted(glob.glob(os.path.join(FIXTURES, "*.json"))):
        fixture = json.load(open(path))
        fixture["name"] = os.path.basename(path)[:-5]
        probe = fixture["probe"]
        extension = fixture["extension"]
        fixture["sim"] = native.build_media(
            probe, media_id=1, part_id=1,
            part_key="/library/parts/1/1/file.%s" % extension,
            file_path="file.%s" % extension,
            size=int((probe.get("format") or {}).get("size") or 0),
            container_hint=extension)
        out.append(fixture)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", default=os.environ.get("REX_NATIVE_BIN", os.path.join(
        os.path.dirname(HERE), "rust", "target", "release", "rex")), help="the native binary to check")
    parser.add_argument("--only", help="comma-separated check names")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--examples", type=int, default=6)
    args = parser.parse_args()
    if args.list:
        print("\n".join(name for name, _, _ in CHECKS))
        return 0
    if not os.path.exists(args.bin):
        print("native binary not found: %s" % args.bin)
        return 1
    native = Native(args.bin)
    corpus = fixtures(native)
    wanted = set(args.only.split(",")) if args.only else None
    passed = total = 0
    for name, function, once in CHECKS:
        if wanted and name not in wanted:
            continue
        total += 1
        code = function.__code__
        extra = {"native": native} if "native" in code.co_varnames[:code.co_argcount] else {}
        try:
            if once:
                rows = function(**extra)
            else:
                rows = [row for fixture in corpus
                        if getattr(function, "shallow", False) or not fixture.get("shallow")
                        for row in function(fixture, **extra)]
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
            print("     %s: real %r, rex %r" % (label, real, sim))
    print("%d/%d checks passed" % (passed, total))
    return 0 if passed == total and total else 1


@check("cycle 24: the extended title's measured stand-ins and language rule", once=True)
def extended_title_model(native):
    """Cycle 24, fitted on the 1.42.2 reference's 105,558 real Streams and held out on the 46 recorded probes.

    The LANGUAGE part has its own rule and it is the one two earlier attempts could not
    find: the name is carried only when it is a SINGLE token that is one of the title's
    words, with any TRAILING PARENTHESISED GROUP stripped from the name first -- so
    `Русский` in `Русский (Full) - ...` and `English (Weeb)` in `English (Some-Stuffs)` are
    carried, while `norsk bokmål` and `Tiếng Việt` in their own titles are not. Cycle 22
    fitted "the whole name" to the 1.42.2 reference and it broke the recorded corpus; the first-word
    rule alone fits the corpus and missed 179 of the 1.42.2 reference's rows. Beside it, five more
    stand-ins: `Signs`/`Songs` for Forced, `CC`/`HI` for SDH, `1.0` for Mono, `HDR`/`Dolby` for the
    dynamic range, and the raw ffprobe codec name (so `aac` carries `HE-AAC` and `H264`
    carries `H.264`). `eia_608` prints `Closed Captions` in BOTH titles, and a sidecar's
    codec suffix is its `providerTitle`, with `External` the fallback.

    Scored end to end this moves the 1.42.2 reference's misses from 613 of 105,558 to 174, and the
    recorded corpus from 3 of 642 to 0.
    """
    rows = []

    def built(row):
        return native.stream_titles(row)

    # the language rule, in both directions
    for title, language, code, want in (
            ("norsk bokmål", "norsk bokmål", "nor", "norsk bokmål (norsk bokmål SRT)"),
            ("Tiếng Việt", "Tiếng Việt", "vie", "Tiếng Việt (Tiếng Việt SRT)"),
            ("dansk", "Dansk", "dan", "dansk (SRT)"),
            ("English (Some-Stuffs)", "English (Weeb)", "eng", "English (Some-Stuffs) (SRT)"),
            ("Русский (Full) - Кубик в Кубе", "Русский", "rus",
             "Русский (Full) - Кубик в Кубе (SRT)")):
        out = built({"streamType": 3, "codec": "srt", "language": language,
                     "languageCode": code, "title": title})
        rows.append((title, want, out.get("extendedDisplayTitle")))

    # the five stand-ins, and eia_608 in both titles
    out = built({"streamType": 3, "codec": "srt", "language": "English", "title": "Signs & Songs",
                 "forced": True})
    # The FORCED part is carried by `Signs`; the language is not, so it stays. 120 rows on
    # the 1.42.2 reference are titled `Signs & Songs...` with `forced` set and no "forced" in the
    # title.
    rows.append(("Forced <- Signs", "Signs & Songs (English SRT)",
                 out.get("extendedDisplayTitle")))
    out = built({"streamType": 3, "codec": "srt", "language": "English",
                 "title": "English [CC]", "hearingImpaired": True})
    rows.append(("SDH <- CC", "English [CC] (SRT)", out.get("extendedDisplayTitle")))
    out = built({"streamType": 2, "codec": "flac", "language": "English",
                 "audioChannelLayout": "mono", "title": "Commentary 1.0"})
    rows.append(("Mono <- 1.0", "Commentary 1.0 (English FLAC)",
                 out.get("extendedDisplayTitle")))
    out = built({"streamType": 3, "codec": "eia_608", "language": "English"})
    # `Closed Captions` is the CODEC LABEL, so it lands wherever the codec does. (the 1.42.2
    # reference's stored `displayTitle` carries the codec because those rows were read as Plex Web
    # -- the recorded client-title rule -- so the catalog form to compare is the extended one.)
    rows.append(("eia_608", "English (Closed Captions)", out.get("extendedDisplayTitle")))
    out = built({"streamType": 3, "codec": "srt", "language": "English",
                 "key": "/library/streams/9", "providerTitle": "OpenSubtitles"})
    rows.append(("sidecar providerTitle", "English (SRT OpenSubtitles)",
                 out.get("extendedDisplayTitle")))
    out = built({"streamType": 3, "codec": "srt", "language": "English",
                 "key": "/library/streams/9"})
    rows.append(("sidecar fallback", "English (SRT External)", out.get("extendedDisplayTitle")))
    return rows


@check("cycle 24: the language table, the video dub flag and an unnamed channel layout",
       once=True)
def measured_smalls(native):
    """Cycle 24, measured on the 1.42.2 reference's 105,558 real Streams.

    `languages.json` lacked THIRTEEN codes the 1.42.2 reference's streams actually use, each named
    in its own language with its 639-1 tag. A VIDEO stream carries `dub` (88 of 13,681) and never
    `comment` or `hearingImpaired`, so its disposition set is default/forced/original/dub.
    And a layout ffprobe cannot name -- `8 channels (FL+FR+FC+LFE+SL+SR+TFL+TFR)` -- left a
    trailing SPACE behind when the bracket was split off, composing `(EAC3 8 channels )`.
    """
    rows = []
    for code, name, tag in (("zul", "isiZulu", "zu"), ("kat", "\u10e5\u10d0\u10e0\u10d7\u10e3\u10da\u10d8", "ka"),
                            ("nno", "norsk nynorsk", "nn"), ("mdr", "Mandar", "mdr"),
                            ("cha", "Chamorro", "ch")):
        # The table's row, as a track tagged with the code is written.
        got = native.built_stream({"codec_type": "audio", "codec_name": "aac", "index": 0,
                                   "tags": {"language": code}})
        rows.append(("languages.json %s" % code, [name, tag],
                     [got.get("language"), got.get("languageTag")]))
    built = native.stream_titles({"streamType": 2, "codec": "eac3", "language": "English",
                                  "audioChannelLayout": "8 channels (FL+FR+FC+LFE+SL+SR+TFL+TFR)"})
    rows.append(("unnamed layout", "English (EAC3 8 channels)",
                 built.get("extendedDisplayTitle")))
    # A video track's `dub` disposition is carried: it is one of the video flags.
    video = native.built_stream({"codec_type": "video", "codec_name": "h264", "index": 0,
                                 "width": 1920, "height": 1080, "disposition": {"dub": 1}})
    rows.append(("video dub is a disposition", True, video.get("dub") is True))
    return rows


@check("cycle 41: carried codec punctuation discriminator", once=True)
def carried_codec_punctuation(native):
    """Cycle 24's three exact discriminator families, retained together.

    An AC-3 stored title carries the AC3 codec component, but VC-1 does not carry
    VC1 and E-AC-3 does not carry EAC3. A global punctuation substitution therefore
    cannot model the reference even though the generic carried-title model does.
    """
    cases = [
        ("AC-3", {"streamType": 2, "codec": "ac3", "language": "English",
                  "channels": 2, "title": "AC-3"}, "AC-3 (English AC3 Stereo)"),
        ("VC-1", {"streamType": 1, "codec": "vc1", "width": 1920, "height": 1080,
                  "title": "VC-1"}, "VC-1 (1080p VC1)"),
        ("E-AC-3", {"streamType": 2, "codec": "eac3", "language": "English",
                    "channels": 2, "title": "E-AC-3"},
         "E-AC-3 (English EAC3 Stereo)"),
    ]
    rows = []
    for label, stream, want in cases:
        rows.append((label, want, native.stream_titles(stream).get("extendedDisplayTitle")))
    return rows


@check("cycle 41: a visually impaired subtitle disposition is projected only when true",
       once=True)
def visual_impaired_subtitle(native):
    """Cycle 41, measured on the 1.42.2 reference's stored stream 93535 and metadata item 13374.

    The PGS subtitle's persisted `ma:visualImpaired:\"1\"` becomes
    `visualImpaired: true` on the Stream row. As with the other dispositions, a
    false or absent ffprobe flag is omitted rather than serialized as false.
    """
    base = {
        "codec_type": "subtitle",
        "codec_name": "hdmv_pgs_subtitle",
        "index": 0,
        "tags": {"language": "eng", "title": "SDH"},
    }
    positive = dict(base, disposition={"visual_impaired": 1})
    negative = dict(base, disposition={"visual_impaired": 0})
    missing = dict(base)
    return [
        ("visual impaired true", True,
         native.built_stream(positive).get("visualImpaired")),
        ("visual impaired false omitted", False,
         "visualImpaired" in native.built_stream(negative)),
        ("visual impaired absent omitted", False,
         "visualImpaired" in native.built_stream(missing)),
    ]


if __name__ == "__main__":
    sys.exit(main())
