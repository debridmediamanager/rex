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


def check(name, once=False):
    """Register a check. A `once` check is called once, not per fixture."""
    def register(function):
        CHECKS.append((name, function, once))
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
    # The reference's AAC clips: fun writes `lc` for the same profile.
    ("kids-1-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-12-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-13-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-14-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-17-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-18-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-3-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-4-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-8-0", 1, "profile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    # The reference's untagged clips: fun leaves an untagged track without a language.
    ("kids-1-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-1-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-1-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-10-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-10-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-10-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-12-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-12-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-12-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-13-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-13-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-13-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-14-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-14-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-14-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-17-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-17-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-17-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-3-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-3-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-3-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-4-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-4-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-4-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-8-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-8-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-8-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-9-0", 1, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-9-0", 1, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-9-0", 1, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    # Layouts the probe does not explain.
    ("fun-1051324-2", 2, "audioChannelLayout"): "E-AC-3 JOC in mp4",
    ("fun-1051324-2", 4, "audioChannelLayout"): "E-AC-3 JOC in mp4",
    ("fun-1061845-0", 1, "audioChannelLayout"): "DTS Express: fun writes no layout; one track",
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
    ("fun-1051324-2", 5, "bitDepth"): "TrueHD in mp4: fun writes no bit depth",
    # AVI tracks the probe gives no id.
    ("fun-1057221-0", 1, "streamIdentifier"): "an AVI track fun numbers with no id in the probe",
    ("fun-1150826-0", 1, "streamIdentifier"): "an AVI track fun numbers with no id in the probe",
    # The reference's untagged video tracks.
    ("kids-1-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-1-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-1-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-10-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-10-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-10-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-12-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-12-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-12-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-13-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-13-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-13-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-14-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-14-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-14-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-17-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-17-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-17-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-3-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-3-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-3-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-4-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-4-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-4-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-8-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-8-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-8-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-9-0", 0, "language"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-9-0", 0, "languageCode"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-9-0", 0, "languageTag"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    # Media bitrates the probe does not give.
    ("fun-1054688-1", None, "bitrate"): "neither rounded nor truncated: fun's own measure",
    ("fun-1057221-0", None, "bitrate"): "neither rounded nor truncated: fun's own measure",
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
    ("kids-1-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-12-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-13-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-14-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-17-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-18-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-3-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-4-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    ("kids-8-0", None, "audioProfile"): "the 1.43.4 reference omits most of its AAC clips' profile; fun writes lc",
    # Titles built from a field the probe does not give.
    ("fun-1051324-2", 2, "displayTitle"): "E-AC-3 JOC in mp4: fun prints its 5.1 core",
    ("fun-1051324-2", 2, "extendedDisplayTitle"): "E-AC-3 JOC in mp4: fun prints its 5.1 core",
    ("fun-1051324-2", 4, "displayTitle"): "E-AC-3 JOC in mp4: fun prints its 5.1 core",
    ("fun-1051324-2", 4, "extendedDisplayTitle"): "E-AC-3 JOC in mp4: fun prints its 5.1 core",
    ("fun-1061845-0", 3, "extendedDisplayTitle"): "a forced track with no linguistic content leaves Forced out; one track",
    ("kids-1-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-1-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-10-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-10-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-12-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-12-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-13-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-13-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-14-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-14-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-17-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-17-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-3-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-3-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-4-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-4-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-8-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-8-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-9-0", 1, "displayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
    ("kids-9-0", 1, "extendedDisplayTitle"): "the 1.43.4 reference labels an untagged track English; fun writes nothing",
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


@check("video-resolution-survey", once=True)
def _video_resolution_survey(mediainfo):
    survey = json.load(open(os.path.join(HERE, "fixtures", "media-resolutions.json")))
    return [("%dx%d (%d Media on fun)" % (width, height, count), label,
             mediainfo._video_resolution(height, width))
            for width, height, label, count in survey["rows"]]


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
def _client_titles(fixture, mediainfo):
    """`webDisplayTitles` is each stream's displayTitle as read with `X-Plex-Product: Plex Web`."""
    out = []
    for real, sim in paired(fixture, None):
        if skipped(fixture, real["index"], "displayTitle"):
            continue
        out.append(("%s stream %s Plex Web displayTitle" % (fixture["name"], real["index"]),
                    fixture["webDisplayTitles"].get(str(real["index"])),
                    mediainfo.client_display_title(sim, "Plex Web") if sim else None))
        out.append(("%s stream %s Plex for Roku displayTitle" % (fixture["name"], real["index"]),
                    real.get("displayTitle"),
                    mediainfo.client_display_title(sim, "Plex for Roku") if sim else None))
    return out


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
        print("\n".join(name for name, _, _ in CHECKS))
        return 0
    mediainfo = load(args.root)
    corpus = fixtures(mediainfo)
    wanted = set(args.only.split(",")) if args.only else None
    passed = total = 0
    for name, function, once in CHECKS:
        if wanted and name not in wanted:
            continue
        total += 1
        code = function.__code__
        extra = {"mediainfo": mediainfo} if "mediainfo" in code.co_varnames[:code.co_argcount] else {}
        try:
            if once:
                rows = function(**extra)
            else:
                rows = [row for fixture in corpus for row in function(fixture, **extra)]
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


@check("cycle 24: the extended title's measured stand-ins and language rule", once=True)
def extended_title_model(mediainfo):
    """Cycle 24, fitted on zen's 105,558 real Streams and held out on the 46 recorded probes.

    The LANGUAGE part has its own rule and it is the one two earlier attempts could not
    find: the name is carried only when it is a SINGLE token that is one of the title's
    words, with any TRAILING PARENTHESISED GROUP stripped from the name first -- so
    `Русский` in `Русский (Full) - ...` and `English (Weeb)` in `English (Some-Stuffs)` are
    carried, while `norsk bokmål` and `Tiếng Việt` in their own titles are not. Cycle 22
    fitted "the whole name" to zen and it broke the recorded corpus; the first-word rule
    alone fits the corpus and missed 179 of zen's rows. Beside it, five more stand-ins:
    `Signs`/`Songs` for Forced, `CC`/`HI` for SDH, `1.0` for Mono, `HDR`/`Dolby` for the
    dynamic range, and the raw ffprobe codec name (so `aac` carries `HE-AAC` and `H264`
    carries `H.264`). `eia_608` prints `Closed Captions` in BOTH titles, and a sidecar's
    codec suffix is its `providerTitle`, with `External` the fallback.

    Scored end to end this moves zen's misses from 613 of 105,558 to 174, and the recorded
    corpus from 3 of 642 to 0.
    """
    rows = []

    def built(row):
        out = dict(row)
        mediainfo.stream_titles(out)
        return out

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
    # zen are titled `Signs & Songs...` with `forced` set and no "forced" in the title.
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
    # `Closed Captions` is the CODEC LABEL, so it lands wherever the codec does. (zen's
    # stored `displayTitle` carries the codec because those rows were read as Plex Web --
    # the recorded client-title rule -- so the catalog form to compare is the extended one.)
    rows.append(("eia_608", "English (Closed Captions)", out.get("extendedDisplayTitle")))
    out = built({"streamType": 3, "codec": "srt", "language": "English",
                 "key": "/library/streams/9", "providerTitle": "OpenSubtitles"})
    rows.append(("sidecar providerTitle", "English (SRT OpenSubtitles)",
                 out.get("extendedDisplayTitle")))
    out = built({"streamType": 3, "codec": "srt", "language": "English",
                 "key": "/library/streams/9"})
    rows.append(("sidecar fallback", "English (SRT External)", out.get("extendedDisplayTitle")))
    return rows


if __name__ == "__main__":
    sys.exit(main())
