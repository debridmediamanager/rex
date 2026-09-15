"""ffprobe JSON -> Plex Media / Part / Stream elements.

Why this exists rather than hand-written attributes: every official Plex client
runs its OWN direct-play gate on these numbers BEFORE the server is ever asked
for a decision.  plex-for-kodi's `MediaDecisionEngine.canDirectPlay` rejects on
height over the client max, bitrate over the client max, frame rate above 30 at
1080p+, and HEVC when the client has it disabled -- all read straight off the
`Media` element.  A plausible-looking guess therefore does not fail loudly, it
makes a client silently ask for a transcode plexsim cannot serve.  So the
numbers come from a real probe of the real byte stream.
"""

import json
import os
import re
from fractions import Fraction

# Plex writes the frame rate as a broadcast-ish token, not a float.
_FRAME_RATE_TOKENS = [
    (Fraction(24000, 1001), "24p"),
    (Fraction(24, 1), "24p"),
    (Fraction(25, 1), "PAL"),
    (Fraction(30000, 1001), "NTSC"),
    (Fraction(30, 1), "30p"),
    (Fraction(50, 1), "50p"),
    (Fraction(60000, 1001), "60p"),
    (Fraction(60, 1), "60p"),
]

# A track's language as a real PMS writes it, measured on every track of 381 rows on fun:
# `language` is the language's name IN that language (`日本語`, `Español`, `polski`),
# `languageCode` the ISO 639-2 terminology code (ffprobe's `fre`, `ger`, `cze` and `chi` read
# back `fra`, `deu`, `ces` and `zho`), and `languageTag` the two-letter ISO 639-1 code, or the
# three-letter code where a language has none (`fil`, `mul`). plexsim printed English names
# for 15 languages and the raw code for the rest, kept bibliographic codes, and cut the tag
# to two letters -- `jp` for Japanese, `sp` for Spanish, `fi` (Finnish) for Filipino -- and a
# client picks a track by the tag. languages.json holds every name and tag fun wrote.
LANGUAGES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "languages.json"),
                           encoding="utf-8"))

# ISO 639-2's bibliographic codes, which Matroska files carry and ffprobe passes through,
# against the terminology codes a real PMS writes. The first fifteen were each measured on
# fun; the last four complete the standard's list.
_TERMINOLOGY = {"alb": "sqi", "baq": "eus", "chi": "zho", "cze": "ces", "dut": "nld",
                "fre": "fra", "ger": "deu", "gre": "ell", "ice": "isl", "mac": "mkd",
                "may": "msa", "per": "fas", "rum": "ron", "slo": "slk", "wel": "cym",
                "arm": "hye", "bur": "mya", "geo": "kat", "tib": "bod"}

_CONTAINER_ALIASES = {
    "matroska,webm": "mkv",
    "matroska": "mkv",
    "mov,mp4,m4a,3gp,3g2,mj2": "mp4",
    "mpegts": "mpegts",
    "webm": "webm",
    "avi": "avi",
}


def _frame_rate_token(rate):
    try:
        value = Fraction(rate)
    except (ValueError, ZeroDivisionError, TypeError):
        return None
    if value == 0:
        return None
    best = min(_FRAME_RATE_TOKENS, key=lambda pair: abs(pair[0] - value))
    if abs(best[0] - value) <= Fraction(1, 100):
        return best[1]
    return "%gp" % round(float(value))


# The resolution label, measured on all 4947 Media of fun's movie libraries with no
# exception: the larger of a WIDTH bucket and a HEIGHT bucket, with `2k` and `2.7k` between
# 1080 and 4k. plexsim read the height alone, so a cropped 1080p release (1920x800) was
# `720`, and both a client's quality gate and the Resolution filter read the label. Each
# threshold is the smallest dimension fun has in its bucket; the largest it has in the
# bucket below is in the comment, and nothing between the two has been seen.
_WIDTH_BUCKETS = ((2876, "4k"),    # 2560 is 2k
                  (2048, "2k"),    # 1998 is 1080
                  (1908, "1080"),  # 1424 is 720
                  (1276, "720"),   # 1024 is 576
                  (1024, "576"))   # 768 is below
_HEIGHT_BUCKETS = ((1600, "2.7k"),  # 1376 is 1080
                   (1072, "1080"),  # 960 is 720
                   (714, "720"),    # 648 is 576
                   (572, "576"),    # 552 is 480
                   (480, "480"))    # 432 is below
_RESOLUTION_ORDER = ("sd", "480", "576", "720", "1080", "2k", "2.7k", "4k")


def _video_resolution(height, width):
    if not height:
        return None
    by_width = next((label for cutoff, label in _WIDTH_BUCKETS if (width or 0) >= cutoff), "sd")
    by_height = next((label for cutoff, label in _HEIGHT_BUCKETS if height >= cutoff), "sd")
    return max(by_width, by_height, key=_RESOLUTION_ORDER.index)


# The aspect ratio a real PMS writes is one of six values -- 1.33, 1.66, 1.78, 1.85, 2.2 and
# 2.35 -- on every Media of fun's movie libraries: the DISPLAY aspect (width times the sample
# aspect ratio, over height) snapped to the nearest. plexsim wrote the raw quotient and
# ignored the sample aspect ratio, so a 1920x800 release read 2.4 where a real server says
# 2.35, and an anamorphic 3840x2160 (SAR 333:320) 1.78 where it says 1.85.
_ASPECT_RATIOS = (1.33, 1.66, 1.78, 1.85, 2.2, 2.35)


def _aspect_ratio(stream):
    width, height = stream.get("width"), stream.get("height")
    if not width or not height:
        return None
    try:
        sar = Fraction((stream.get("sample_aspect_ratio") or "1:1").replace(":", "/"))
    except (ValueError, ZeroDivisionError):
        sar = Fraction(1)
    if sar <= 0:
        sar = Fraction(1)
    shape = float(width * sar / height)
    return min(_ASPECT_RATIOS, key=lambda value: abs(value - shape))


def _language_fields(tags):
    raw = tags.get("language") or ""
    code = _TERMINOLOGY.get(raw.lower(), raw.lower())
    if code in ("", "und"):
        return {}
    if code in LANGUAGES:
        name, tag = LANGUAGES[code]
        return {"language": name, "languageCode": code, "languageTag": tag}
    if len(code) == 3 and code.isalpha():
        return {"language": code, "languageCode": code, "languageTag": code}
    # Not an ISO code at all (`english`, `russian`): measured on three tracks, the text is
    # both the name and the tag, and the code is empty.
    return {"language": raw, "languageCode": "", "languageTag": raw}


# The track titles a real PMS writes, measured on every stream of 35 files on fun:
#
#   video     displayTitle  <resolution>[ <range>]              4K DoVi/HDR10
#             extended      <displayTitle> (<CODEC>[ <Profile>]) 4K DoVi/HDR10 (HEVC Main 10)
#   audio     displayTitle  <language> (<CODEC> <channels>)     English (DTS-HD MA 7.1)
#   subtitle  displayTitle  <language>[ Forced][ SDH]           English SDH
#             extended      <displayTitle> (<CODEC>)            English SDH (SRT)
#
# A track TITLE never enters displayTitle. It leads the extended title, followed in brackets
# by the label parts it does not already carry -- `BTM TrueHD Atmos 7.1 (English)`,
# `SDH (English SRT)` -- and by nothing when it carries every one (`English DTS Express 5.1`).
# plexsim printed the track title as an audio title, `sdp` for SD video, `Dolby Digital Plus`
# and `3ch`, and never named a profile.

# Resolution buckets as a title prints them: five labels, so a 576 or 2k stream reads as its
# neighbour (1024x436 is `480p`, 1920x1600 `1080p`).
_TITLE_RESOLUTIONS = {"4k": "4K", "2.7k": "1080", "2k": "1080", "1080": "1080", "720": "720",
                      "576": "480", "480": "480", "sd": "SD"}
_VIDEO_CODEC_LABELS = {"h264": "H.264", "hevc": "HEVC"}
_DTS_LABELS = {"ma": "DTS-HD MA", "es": "DTS-ES", "hra": "DTS-HD HRA", "express": "DTS Express"}

# How a track title is split into words, and how a word is compared: the separators are
# whitespace and `- / ( ) [ ] + , | : ; &` but not the dot (`Baby.2004.1080p` carries no
# `1080p`), and only ASCII letters fold case (`Қазақ` does not carry `қазақ`).
_TITLE_SEPARATORS = re.compile(r"[\s\-/()\[\]+,|:;&]+")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _video_resolution_label(row):
    label = _TITLE_RESOLUTIONS.get(_video_resolution(row.get("height"), row.get("width")))
    if label and label not in ("4K", "SD"):
        label += "i" if row.get("scanType") == "interlaced" else "p"
    return label


def _dynamic_range(row):
    if row.get("DOVIPresent"):
        if not row.get("DOVIBLPresent"):
            return "DoVi Enhancement Layer"
        return "DoVi/HDR10" if row.get("DOVIBLCompatID") in (1, 6) else "DoVi"
    return "HDR10" if row.get("colorTrc") == "smpte2084" else None


def _video_codec_label(row):
    """HEVC always names its profile, H.264 any profile but High and Main, and every other
    codec none (`MPEG4`, `AV1`, `VP8`)."""
    codec = str(row.get("codec") or "").lower()
    if not codec:
        return None
    label = _VIDEO_CODEC_LABELS.get(codec, codec.upper())
    profile = row.get("profile")
    if profile and (codec == "hevc" or (codec == "h264" and profile not in ("high", "main"))):
        label += " " + profile.title()
    return label


def _audio_codec_label(row):
    codec = str(row.get("codec") or "").lower()
    if codec == "dca":
        return _DTS_LABELS.get(row.get("profile"), "DTS")
    if codec == "aac" and row.get("profile") == "he-aac":
        return "HE-AAC"
    return codec.upper()


# Cycle 24: the dispositions a VIDEO stream carries, measured on zen.
_VIDEO_FLAGS = ("default", "forced", "original", "dub")


def _channel_label(row):
    """The layout's own name (`5.1(side)` prints `5.1`, `2.1` stays `2.1`), else the count."""
    layout = row.get("audioChannelLayout")
    if layout:
        # Cycle 24, measured on zen: a layout ffprobe cannot name reads
        # `8 channels (FL+FR+FC+LFE+SL+SR+TFL+TFR)`, and splitting on the bracket left the
        # SPACE behind -- `8 channels ` composed `(EAC3 8 channels )` with a double space.
        base = layout.split("(")[0].strip()
        return {"mono": "Mono", "stereo": "Stereo"}.get(base, base)
    channels = row.get("channels")
    if not channels:
        return None
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(channels, "%dch" % channels)


def _alnum(text):
    """A token folded to ASCII lower-case letters and digits, for the raw-name fallback."""
    return re.sub(r"[^0-9a-z]", "", str(text or "").translate(_ASCII_LOWER))


def _carried(title, part, stand_ins=(), raw=()):
    """Whether the title already carries this part.

    Cycle 24, fitted on zen's 105,558 real Streams and held out on the 46 recorded probes:
    a part is carried when its FIRST word, or one of its stand-ins, is one of the title's
    words -- `2.0` for Stereo and `1.0` for Mono, `AVC` for H.264, `Signs`/`Songs` for
    Forced, `CC`/`HI` for SDH, `HDR`/`Dolby` for the dynamic range -- OR when the part or a
    `raw` alias matches a title word with every non-alphanumeric character folded away,
    which is what makes `H264` carry `H.264` and `aac` carry `HE-AAC`.
    """
    words = {word.translate(_ASCII_LOWER) for word in _TITLE_SEPARATORS.split(title) if word}
    folded = {_alnum(word) for word in _TITLE_SEPARATORS.split(title) if word}
    first = next((word for word in _TITLE_SEPARATORS.split(part or "") if word), "")
    if any(word and word.translate(_ASCII_LOWER) in words
           for word in (first,) + tuple(stand_ins)):
        return True
    return any(candidate and candidate in folded
               for candidate in [_alnum(part)] + [_alnum(one) for one in raw])


def _language_carried(title, language, tag):
    """Whether the title already carries the LANGUAGE part, which has its own rule.

    Cycle 24, and it is the rule two earlier attempts could not find. Cycle 22 fitted "the
    whole name, not the first word" to zen and it broke the recorded corpus; the first-word
    rule alone fits the corpus and misses 179 of zen's rows. Both are explained by: the name
    is carried only when it is a SINGLE token that is one of the title's words, with any
    TRAILING PARENTHESISED GROUP stripped from the name first -- or when its tag is.
    So `Русский` in `Русский (Full) - Кубик в Кубе` and `English (Weeb)` in
    `English (Some-Stuffs)` are carried (single token after the strip), while `norsk bokmål`
    and `Tiếng Việt` in their own titles are NOT (two tokens, and no single word can match).
    """
    words = {word.translate(_ASCII_LOWER) for word in _TITLE_SEPARATORS.split(title) if word}
    base = re.sub(r"\s*\([^)]*\)\s*$", "", str(language or "")).strip()
    if base and len([w for w in _TITLE_SEPARATORS.split(base) if w]) == 1 \
            and base.translate(_ASCII_LOWER) in words:
        return True
    return bool(tag) and str(tag).translate(_ASCII_LOWER) in words


def _extended_title(title, parts, keep=()):
    """`title (parts it does not carry)`, each part tested by `_carried`.

    `keep` names parts already decided -- the LANGUAGE, whose rule is `_language_carried`
    and NOT the generic first-word one. Routing it through `_carried` again drops every
    multi-token name whose first word is a title word, which is the case `_language_carried`
    exists to get right.
    """
    left = [part for part, stand_ins, raw in parts
            if part and (part in keep or not _carried(title, part, stand_ins, raw))]
    return "%s (%s)" % (title, " ".join(left)) if left else title


def _subtitle_codec_label(row):
    """The codec part of a subtitle's titles.

    Measured on a writable copy of the reference: a SIDECAR subtitle -- one uploaded
    through the client's Upload Subtitle modal, which is the only kind plexsim can
    hold -- labels itself `<CODEC> External`, so its plain title reads
    `Unknown (SRT External)` and its extended one `<name> (SRT External)`. An embedded
    track keeps the bare codec. The tell is the sidecar `key`: an embedded stream has
    none. One witness only (the copy is a real PMS 1.43.4; no sidecar subtitle was
    found on the 1.42.2 server to confirm it against).
    """
    # Cycle 24, measured on zen (107 streams): `eia_608` prints as `Closed Captions`, and
    # it is the plain CODEC label, so it reaches the plain display title too.
    codec = str(row.get("codec") or "").upper()
    if codec == "EIA_608":
        codec = "Closed Captions"
    if codec and str(row.get("key") or "").startswith("/library/streams/"):
        # Cycle 24: the suffix is the stream's own `providerTitle`, and `External` is the
        # fallback -- zen's 11 sidecars carry none and read `(SRT External)`, while the
        # recorded corpus has one carrying `OpenSubtitles` and reading `(English SRT
        # OpenSubtitles)`. Cycle 17 measured the fallback alone, on one server.
        return "%s %s" % (codec, row.get("providerTitle") or "External")
    return codec


def stream_titles(row, stand_in=None):
    """Set `displayTitle` and `extendedDisplayTitle` on one Stream row, from its own fields.

    Called when a stream is built and again when a catalog loads, so a catalog built before
    a rule was measured is corrected rather than needing a rebuild.

    `stand_in` is the row's ENGLISH language name when the row carries a localised one.
    Measured in cycle 17: the reference decides whether the extended title CARRIES the
    language part on the ENGLISH name and then renders the localised one, so a track
    titled "English" keeps `English (SRT)` in German while its plain title moves.
    """
    kind = row.get("streamType")
    title = row.get("title")
    if kind == 1:
        resolution = _video_resolution_label(row)
        if not resolution:
            return row
        dynamic = _dynamic_range(row)
        codec = _video_codec_label(row)
        plain = " ".join(part for part in (resolution, dynamic) if part)
        row["displayTitle"] = plain
        if title is not None:
            avc = ("AVC",) if str(row.get("codec") or "").lower() == "h264" else ()
            # Cycle 24: `HDR`/`Dolby` stand in for the dynamic range, and the codec is also
            # matched against the RAW ffprobe name with punctuation folded (`H264`/`H.264`).
            row["extendedDisplayTitle"] = _extended_title(
                title, [(resolution, (), ()), (dynamic, ("HDR", "Dolby"), ()),
                        (codec, avc, (row.get("codec") or "",))])
        else:
            row["extendedDisplayTitle"] = "%s (%s)" % (plain, codec) if codec else plain
    elif kind == 2:
        language = row.get("language")
        codec = _audio_codec_label(row)
        channels = _channel_label(row)
        row["displayTitle"] = "%s (%s)" % (language or "Unknown",
                                           " ".join(part for part in (codec, channels) if part))
        if title is not None:
            # Cycle 24: `1.0` stands in for Mono as `2.0` does for Stereo, and the audio
            # codec is matched against the raw ffprobe name too (`aac` carries `HE-AAC`).
            stereo = ("2.0",) if channels == "Stereo" else \
                ("1.0",) if channels == "Mono" else ()
            parts = [(codec, (), (row.get("codec") or "",)), (channels, stereo, ())]
            language_part = ()
            if language and not _language_carried(title, language,
                                                  row.get("languageTag") or ""):
                language_part = (stand_in or language,)
                parts.insert(0, (language_part[0], (), ()))
            row["extendedDisplayTitle"] = _extended_title(title, parts, keep=language_part)
        else:
            row["extendedDisplayTitle"] = row["displayTitle"]
    elif kind == 3:
        language = row.get("language")
        forced = "Forced" if row.get("forced") else None
        sdh = "SDH" if row.get("hearingImpaired") else None
        codec = _subtitle_codec_label(row)
        plain = " ".join(part for part in (language or "Unknown", forced, sdh) if part)
        row["displayTitle"] = plain
        if title is not None:
            # Cycle 24: `Signs`/`Songs` stand in for Forced and `CC`/`HI` for SDH.
            parts = [(forced, ("Signs", "Songs"), ()), (sdh, ("CC", "HI"), ()),
                     (codec, (), (row.get("codec") or "",))]
            language_part = ()
            if language and not _language_carried(title, language,
                                                  row.get("languageTag") or ""):
                language_part = (stand_in or language,)
                parts.insert(0, (language_part[0], (), ()))
            row["extendedDisplayTitle"] = _extended_title(title, parts, keep=language_part)
        else:
            row["extendedDisplayTitle"] = "%s (%s)" % (plain, codec) if codec else plain
    return row


# The products whose `displayTitle` carries the codec, measured on both servers against 38
# product names, in the header and in the query alike: a video's and a subtitle's plain
# title gains ` (<CODEC>...)` -- `1080p (H.264)`, `English SDH (SRT)` -- for exactly these,
# case-sensitively, and for no other (`Apple TV`, `Plex for Android`, `Plex for Xbox One`,
# `Plex Desktop`, Roku, HTPC, Kodi, PlexAPI and no product at all get the plain title). An
# audio title and every extended title are the same for everyone. The recorded rule, a
# video `displayTitle` of `1080p`, was the no-product answer, and plexsim sent it to Plex
# Web itself.
CODEC_TITLE_PRODUCTS = frozenset((
    "Plex Web", "Plex for iOS", "Plex for Apple TV", "Plex for Android (TV)",
    "Plex for Android (Mobile)", "Plex for LG", "Plex for Samsung", "Plex for Windows",
    "Plex for Mac", "Plex for Xbox", "Plex for PlayStation 4", "Plex for PlayStation 5",
    "Plex for Vizio"))


def client_display_title(row, product):
    """The `displayTitle` one client is sent for a Stream row."""
    title = row.get("displayTitle")
    if product not in CODEC_TITLE_PRODUCTS or not title:
        return title
    kind = row.get("streamType")
    if kind == 1:
        codec = _video_codec_label(row)
    elif kind == 3:
        codec = _subtitle_codec_label(row)
    else:
        return title
    return "%s (%s)" % (title, codec) if codec else title


# ffprobe's disposition keys against a real PMS's attribute names. Measured on every
# probed track on fun: each flag is written only when TRUE, on audio, subtitles and video
# alike, and a real PMS never sends `default: false` or `selected: false`.
_DISPOSITIONS = {"default": "default", "forced": "forced", "hearingImpaired": "hearing_impaired",
                 "original": "original", "dub": "dub", "comment": "comment"}
TRACK_FLAGS = ("default", "forced", "hearingImpaired", "original", "dub", "comment")


def _disposition(stream, fields):
    flags = stream.get("disposition") or {}
    return {field: True for field in fields if flags.get(_DISPOSITIONS[field])}


def _video_stream(stream, stream_id):
    width = stream.get("width")
    height = stream.get("height")
    row = {
        "id": stream_id,
        "streamType": 1,
        "codec": stream.get("codec_name"),
        "index": stream.get("index"),
        "streamIdentifier": _stream_identifier(stream),
        "width": width,
        "height": height,
        "profile": _video_profile(stream.get("profile")),
        "level": None if stream.get("level") in (None, -99) else stream.get("level"),
        "refFrames": stream.get("refs"),
        # ffprobe reports this as a STRING; a real PMS sends an integer, and a
        # typed client decoding `"8"` into an int field fails the whole row.
        "bitDepth": _bit_depth(stream),
        "chromaSubsampling": stream.get("pix_fmt") and _chroma(stream["pix_fmt"]),
        # ffprobe has all three of these and a real PMS carries them; Get Info
        # prints them. `hasScalingMatrix` is NOT here on purpose: a real PMS
        # reads it out of the H.264 bitstream and ffprobe does not report it, so
        # there is nothing to derive it from.
        "chromaLocation": stream.get("chroma_location"),
        "codedHeight": _coded_height(stream),
        "codedWidth": stream.get("coded_width"),
        "codecID": _codec_id(stream),
        "colorPrimaries": stream.get("color_primaries"),
        "colorTrc": stream.get("color_transfer"),
        "colorSpace": stream.get("color_space"),
        # Measured: ffprobe's `color_range` as it is, and absent where it has none.
        "colorRange": stream.get("color_range"),
        "frameRate": _float_or_none(_frame_rate(stream)),
        "scanType": _scan_type(stream.get("field_order")),
    }
    row.update(_pixel_aspect(stream))
    # Measured on the six Dolby Vision files probed on fun (profiles 5, 7, 8 and 10): the
    # eight DOVI attributes are ffprobe's DOVI configuration record, field for field. They
    # are what tells a client a Dolby Vision stream it can play from one it cannot, and
    # plexsim sent none of them.
    dovi = next((data for data in stream.get("side_data_list") or []
                 if data.get("side_data_type") == "DOVI configuration record"), None)
    if dovi:
        row.update({
            "DOVIPresent": True,
            "DOVIProfile": dovi.get("dv_profile"),
            "DOVILevel": dovi.get("dv_level"),
            "DOVIBLCompatID": dovi.get("dv_bl_signal_compatibility_id"),
            "DOVIBLPresent": bool(dovi.get("bl_present_flag")),
            "DOVIELPresent": bool(dovi.get("el_present_flag")),
            "DOVIRPUPresent": bool(dovi.get("rpu_present_flag")),
            "DOVIVersion": "%s.%s" % (dovi.get("dv_version_major", 1), dovi.get("dv_version_minor", 0)),
        })
    # Cycle 24, measured on zen's 13,681 video Streams: 88 carry `dub` and none carries
    # `comment` or `hearingImpaired`, so the video set is these four.
    row.update(_disposition(stream, _VIDEO_FLAGS))
    # A video stream carries its own language and title from its tags, measured on 17 and
    # 10 streams on fun; plexsim set neither.
    tags = stream.get("tags") or {}
    row.update(_language_fields(tags))
    if tags.get("title"):
        row["title"] = tags["title"]
    bitrate = stream.get("bit_rate")
    if bitrate:
        row["bitrate"] = round(int(bitrate) / 1000)
    return stream_titles(row)


def _pixel_aspect(stream):
    """`pixelAspectRatio` and `anamorphic`, which a real PMS sends as a PAIR.

    Cycle 25, measured on 1,688 of zen's video Streams: 358 carry both and 1,330 carry
    neither -- not one carries only one of the two -- and the value is ffprobe's
    `sample_aspect_ratio` verbatim, present exactly when the sample aspect is not square.

    `anamorphic` is the one Stream boolean a real PMS sends when it is FALSE, so it goes
    out either way. Its rule is the pixel aspect rounded to ONE decimal place: true when
    that is not 1.0. Sixteen distinct ratios witness it and every one agrees --
    `99:100`, `172:173`, `799:800`, `816:817`, `872:873`, `959:960`, `10439:10440`,
    `481:480`, `255:254` and `643:640` (303 streams) are all false, and `1:2`, `10:11`,
    `16:15`, `495:464`, `721:640` and `2:1` are all true -- together with the two in the
    recorded corpus, `333:320` false and `32:27` true. That puts the boundary inside
    (0.0406, 0.0667] and 0.05 is the only round number in it, so the CONSTANT is still
    inferred where the two sides of it are measured; a file with a ratio in that gap
    would settle it. A pixel-difference model was tried first and is refuted: the 3840
    -wide `333:320` stream displaces 156 pixels and is false where a 716-wide `16:15`
    displaces 48 and is true.
    """
    raw = stream.get("sample_aspect_ratio")
    if not raw or ":" not in raw:
        return {}
    numerator, _, denominator = raw.partition(":")
    try:
        ratio = int(numerator) / int(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return {}
    if not ratio or ratio == 1:
        # ffprobe writes `0:1` for a sample aspect it could not read, and `1:1` for a
        # square one; a real PMS sends neither field for either.
        return {}
    return {"pixelAspectRatio": raw, "anamorphic": abs(ratio - 1) >= 0.05}


def _int_or(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bit_depth(stream):
    """A video stream's bit depth.

    ffprobe reports `bits_per_raw_sample` for H.264 and leaves it out for HEVC and AV1,
    where the depth is only in the pixel format. Measured on fun: all 16 ten-bit streams
    probed without it (`yuv420p10le`, `yuv444p10le`) read `bitDepth: 10`. plexsim defaulted
    every one to 8, and ten-bit is a direct-play gate on many clients.
    """
    raw = _int_or(stream.get("bits_per_raw_sample"), None)
    if raw:
        return raw
    match = re.search(r"p(\d+)(?:le|be)$", stream.get("pix_fmt") or "")
    return int(match.group(1)) if match else 8


def _frame_rate(stream):
    """The AVERAGE frame rate, falling back to the nominal one. Measured: the reference's mp4
    clip whose nominal rate is 48 reads `frameRate: 23.947` and `24p`, which is ffprobe's
    `avg_frame_rate`; plexsim read `r_frame_rate` and said 48p."""
    average = stream.get("avg_frame_rate") or ""
    if average and not average.startswith("0/"):
        return average
    return stream.get("r_frame_rate")


def _video_profile(profile):
    """ffprobe's video profile as a real PMS writes it: lower case, without a trailing
    ` Profile` (`Simple Profile` is `simple`), and absent where ffprobe has only its unknown
    marker `0` (which comes with level -99). Measured on fun; plexsim wrote `0` and -99."""
    text = (profile or "").strip().lower()
    if text.endswith(" profile"):
        text = text[:-len(" profile")]
    return None if text in ("", "0") else text


def _coded_height(stream):
    """An H.264 stream's coded height is whole macroblocks: 1080 is 1088, 436 is 448 and 536
    is 544, measured on 12 streams on fun and 11 on the reference, while ffprobe 8.1 reports
    the cropped height. HEVC and the rest match ffprobe as it is."""
    height = stream.get("coded_height")
    if height and stream.get("codec_name") == "h264":
        return (height + 15) // 16 * 16
    return height


def _codec_id(stream):
    """A video stream's codec tag, measured on 8 of 8 streams that have a real one: `hev1`,
    `hvc1` and `dvhe` in an mp4, `XVID` and `DX50` in an AVI, `HDMV` in MPEG-TS -- but not
    `avc1`, and never on an audio or subtitle stream. plexsim sent none."""
    tag = stream.get("codec_tag_string") or ""
    if not tag or "[" in tag or tag == "avc1":
        return None
    return tag


def _stream_identifier(stream):
    """The container's own track id in decimal, measured on 79 streams: an mp4 track number
    (`0x2` is `"2"`), an MPEG-TS PID (`0x1011` is `"4113"`), and absent in Matroska, where
    ffprobe reports no id. A string, as a real PMS sends it. plexsim sent none."""
    try:
        return str(int(stream["id"], 16))
    except (KeyError, TypeError, ValueError):
        return None


def _audio_bit_depth(stream):
    """Measured on 42 lossless and DTS tracks on fun: ffprobe's `bits_per_raw_sample`, and 24
    for a DTS track without it (DTS Express excepted); a lossy codec carries none. plexsim
    never sent an audio bit depth."""
    raw = _int_or(stream.get("bits_per_raw_sample"), None)
    if raw:
        return raw
    if stream.get("codec_name") == "dts" and stream.get("profile") != "DTS Express":
        return 24
    return None


def _scan_type(field_order):
    """Measured on 47 video streams: absent where ffprobe reports no field order (19),
    `interlaced` for a field-ordered stream (`tt`), `progressive` otherwise. plexsim wrote
    `progressive` on every one."""
    if not field_order:
        return None
    return "progressive" if field_order == "progressive" else "interlaced"


def _chroma(pix_fmt):
    if "444" in pix_fmt:
        return "4:4:4"
    if "422" in pix_fmt:
        return "4:2:2"
    return "4:2:0"


def _float_or_none(rate):
    try:
        return round(float(Fraction(rate)), 3)
    except (ValueError, ZeroDivisionError, TypeError):
        return None


# ffprobe's audio codec and profile against the ones a real PMS writes, measured on 141
# tracks on fun and the reference. Every DTS variant is `dca`, with the profile naming the
# variant; PCM in Matroska is `pcm` with ffprobe's sample format as its profile, while
# Blu-ray PCM keeps its own name; and the `+ Dolby Atmos` profile ffprobe gives TrueHD and
# E-AC-3 is not written at all. plexsim passed ffprobe's names through, and a client's
# direct-play profile names `dca`: a `dts` track matched nothing in it.
DTS_PROFILES = {"dts": "dts", "dts-hd ma": "ma", "dts-es": "es", "dts-hd hra": "hra",
                "dts express": "express"}


def audio_codec(codec, profile):
    """(codec, profile) as a real PMS names them, from ffprobe's or from a stored row's."""
    codec = (codec or "").lower()
    profile = (profile or "").lower() or None
    if codec in ("dts", "dca"):
        return "dca", DTS_PROFILES.get(profile, profile)
    if codec.startswith("pcm_") and codec != "pcm_bluray":
        return "pcm", codec
    if profile and "atmos" in profile:
        return codec, None
    return codec, profile


def _audio_stream(stream, stream_id, selected):
    tags = stream.get("tags") or {}
    channels = stream.get("channels") or 2
    codec = stream.get("codec_name") or ""
    plex_codec, plex_profile = audio_codec(codec, stream.get("profile"))
    row = {
        "id": stream_id,
        "streamType": 2,
        "codec": plex_codec,
        "index": stream.get("index"),
        "streamIdentifier": _stream_identifier(stream),
        "channels": channels,
        # ffprobe's layout verbatim, measured on 126 of 130 tracks on fun, and absent where
        # it has none. plexsim derived it from the channel count, so a `5.1` track was
        # `5.1(side)` and a 3-, 4-, 5- or 7-channel one had no layout.
        "audioChannelLayout": stream.get("channel_layout") or None,
        "samplingRate": stream.get("sample_rate") and int(stream["sample_rate"]),
        "bitDepth": _audio_bit_depth(stream),
        "profile": plex_profile,
    }
    # Measured on 130 audio tracks on fun: every flag is ffprobe's disposition and appears
    # only when true. plexsim marked the FIRST track default, sent `default: false` and
    # `selected: false` on every other one, and none of the other five flags. Which track
    # is selected depends on the account's language settings, which plexsim does not have,
    # so the first one still is.
    if selected:
        row["selected"] = True
    row.update(_disposition(stream, TRACK_FLAGS))
    row.update(_language_fields(tags))
    bitrate = stream.get("bit_rate")
    if bitrate:
        row["bitrate"] = round(int(bitrate) / 1000)
    if tags.get("title"):
        row["title"] = tags["title"]
    return stream_titles(row)


# ffprobe's subtitle decoder names against the codec a real PMS writes, measured on every
# embedded track of 35 files on fun: SubRip is `srt`, a Blu-ray bitmap track `pgs` and a DVD
# one `vobsub`. plexsim passed the last two through as `hdmv_pgs_subtitle` and
# `dvd_subtitle`, which no client's burn-in rule names.
SUBTITLE_CODECS = {"subrip": "srt", "hdmv_pgs_subtitle": "pgs", "dvd_subtitle": "vobsub"}


def _subtitle_stream(stream, stream_id):
    tags = stream.get("tags") or {}
    codec = stream.get("codec_name") or ""
    codec = SUBTITLE_CODECS.get(codec, codec)
    # Measured on 432 embedded subtitle tracks on fun: `default` and `forced`
    # appear only when TRUE, every track says `canAutoSync: false`, and none
    # carries `embeddedInVideo` -- which marks captions carried INSIDE the video
    # stream, not a subtitle track muxed beside it. plexsim sent all three on
    # every track, false flags included.
    row = {
        "id": stream_id,
        "streamType": 3,
        "canAutoSync": False,
        "codec": codec,
        "index": stream.get("index"),
        "streamIdentifier": _stream_identifier(stream),
    }
    row.update(_disposition(stream, TRACK_FLAGS))
    bitrate = stream.get("bit_rate")
    if bitrate:
        row["bitrate"] = round(int(bitrate) / 1000)
    row.update(_language_fields(tags))
    if tags.get("title"):
        row["title"] = tags["title"]
    return stream_titles(row)


def build_media(probe, *, media_id, part_id, part_key, file_path, size, container_hint=None):
    """One `Media` element (with its single `Part` and that part's `Stream`s).

    `probe` is the parsed output of
    `ffprobe -print_format json -show_format -show_streams`.

    `container_hint` is the source file's extension.  It is needed because
    ffprobe reports `matroska,webm` for BOTH a .mkv and a .webm -- the two share
    a demuxer -- while Plex clients treat them as different containers, and a
    webm announced as mkv is a direct-play offer the client cannot honour.
    """
    fmt = probe.get("format") or {}
    streams = probe.get("streams") or []

    # Every video stream, not only the first -- a dual-layer Dolby Vision file carries its
    # enhancement layer as a second one, and a real PMS lists it -- except an attached
    # picture: ffprobe lists a Matroska file's cover art as a video stream, and a real PMS
    # lists none (7 covers in 4 files on fun).
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")]
    video = videos[0] if videos else None
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]

    duration_ms = int(float(fmt.get("duration") or 0) * 1000)
    # Rounded, not truncated: measured on 33 of 35 files on fun, where plexsim read one short
    # on 28 of them. Stream bitrates round the same way.
    bitrate_kbps = round(int(fmt.get("bit_rate") or 0) / 1000) or None

    rows = []
    next_id = part_id * 100

    for each in videos:
        next_id += 1
        rows.append(_video_stream(each, next_id))
    for position, audio in enumerate(audios):
        next_id += 1
        rows.append(_audio_stream(audio, next_id, selected=(position == 0)))
    for subtitle in subtitles:
        next_id += 1
        rows.append(_subtitle_stream(subtitle, next_id))

    width = video.get("width") if video else None
    height = video.get("height") if video else None
    # The Media's audio fields name the track with the MOST channels, the first of them on a
    # tie -- measured on 35 of 35 files on fun, where the first track was the wrong one in 9
    # -- and a DTS-HD MA track is `dca-ma` there while every other DTS variant stays `dca`.
    # plexsim named the first track, and a client gates direct play on these fields.
    main_audio = max(audios, key=lambda s: s.get("channels") or 0) if audios else {}
    main_codec, main_profile = audio_codec(main_audio.get("codec_name"), main_audio.get("profile"))
    container = _CONTAINER_ALIASES.get(
        fmt.get("format_name"), (fmt.get("format_name") or "").split(",")[0]
    )
    # The shared matroska/webm demuxer cannot tell the two apart; the extension
    # can, and it is what a real PMS reports.
    if container == "mkv" and (container_hint or "").lower() == "webm":
        container = "webm"
    video_profile = _video_profile(video.get("profile")) if video else None

    part = {
        "id": part_id,
        "key": part_key,
        "duration": duration_ms,
        "file": file_path,
        "size": size,
        "container": container,
        "videoProfile": video_profile,
        "audioProfile": main_profile,
        # A real PMS reports these only under `?checkFiles=1` -- measured: a plain
        # detail read carries neither. plexsim sends them ALWAYS on purpose,
        # because plezy re-fetches metadata in a loop until every part carries all
        # three of Stream / exists / accessible and never passes checkFiles. Two
        # extra attributes cost a client nothing; a client that loops forever is
        # the worse failure. The deviation is recorded in docs/findings.md.
        "exists": True,
        "accessible": True,
        "Stream": rows,
    }

    media = {
        "id": media_id,
        "duration": duration_ms,
        "bitrate": bitrate_kbps,
        "width": width,
        "height": height,
        "aspectRatio": _aspect_ratio(video) if video else None,
        "audioChannels": main_audio.get("channels"),
        "audioCodec": "dca-ma" if (main_codec, main_profile) == ("dca", "ma") else (main_codec or None),
        "videoCodec": video.get("codec_name") if video else None,
        "videoResolution": _video_resolution(height, width),
        "container": container,
        "videoFrameRate": _frame_rate_token(_frame_rate(video)) if video else None,
        "videoProfile": video_profile,
        "audioProfile": main_profile,
        "Part": [part],
    }
    return normalize_media(media)


# Containers that HAVE a moov atom, which is what the two streaming flags describe.
MP4_FAMILY = ("mp4", "m4v", "mov")


def normalize_media(media):
    """The per-container flag rules a real PMS applies, on a built Media element.

    Applied at build time and again when a catalog loads, because catalogs built
    before these were measured carry the old flags baked in.

    Measured on fun across 2,558 rows:
    * `optimizedForStreaming` and `has64bitOffsets` exist ONLY for an mp4/mov --
      absent on every mkv, mpegts and avi. On an mp4 the Media's
      `optimizedForStreaming` is an INTEGER and the Part's a boolean, and both
      flags appear on both. plexsim sent `false` on every Media of every container.
    * `hasVoiceActivity: false` is on EVERY Media row of both real servers; the
      player's Auto-Sync Subtitles control reads it.
    * `Part.hasThumbnail` appears only as the string `"1"`, and only when a
      thumbnail exists. plexsim sent `false` everywhere.

    `optimizedForStreaming` cannot be read from ffprobe (it is where the moov atom
    sits), so it stays 0. `has64bitOffsets` can be: an mp4 larger than 4 GiB cannot
    address its data with 32-bit chunk offsets, so it uses co64.
    """
    container = str(media.get("container") or "").lower()
    parts = media.get("Part") or []
    media.pop("optimizedForStreaming", None)
    media.pop("has64bitOffsets", None)
    if container in MP4_FAMILY:
        large = any(int(p.get("size") or 0) > 2 ** 32 for p in parts)
        media["optimizedForStreaming"] = 0
        media["has64bitOffsets"] = large
    media["hasVoiceActivity"] = bool(media.get("hasVoiceActivity"))
    if media.get("audioCodec") == "dts":
        _, profile = audio_codec("dts", media.get("audioProfile"))
        media["audioCodec"] = "dca-ma" if profile == "ma" else "dca"
        media["audioProfile"] = profile
    # A catalog built before cycle 10 stores the old resolution label, aspect ratio and
    # profile markers, and each is derivable from the row. The aspect ratio is snapped only
    # when it is not already one of the six: a stored row has no sample aspect ratio, and a
    # value built from one is already right.
    if media.get("width") and media.get("height"):
        media["videoResolution"] = _video_resolution(media["height"], media["width"])
        if media.get("aspectRatio") not in _ASPECT_RATIOS:
            media["aspectRatio"] = _aspect_ratio(media)
    for holder in [media] + parts:
        if "videoProfile" in holder:
            holder["videoProfile"] = _video_profile(holder["videoProfile"])
    for part in parts:
        part.pop("optimizedForStreaming", None)
        part.pop("has64bitOffsets", None)
        if container in MP4_FAMILY:
            part["optimizedForStreaming"] = False
            part["has64bitOffsets"] = int(part.get("size") or 0) > 2 ** 32
        if part.get("hasThumbnail") not in ("1", 1, True):
            part.pop("hasThumbnail", None)
        for stream in part.get("Stream") or []:
            for flag in TRACK_FLAGS + ("selected",):
                if stream.get(flag) is False:
                    stream.pop(flag)
            if stream.get("languageCode"):
                stream.update(_language_fields({"language": stream["languageCode"]}))
            if "bitDepth" in stream:
                stream["bitDepth"] = _int_or(stream["bitDepth"], stream["bitDepth"])
            if stream.get("streamType") == 1:
                stream["profile"] = _video_profile(stream.get("profile"))
                if stream.get("level") == -99:
                    stream.pop("level")
            if stream.get("streamType") == 2 and stream.get("codec"):
                stream["codec"], profile = audio_codec(stream["codec"], stream.get("profile"))
                if profile:
                    stream["profile"] = profile
                else:
                    stream.pop("profile", None)
            if stream.get("streamType") == 3:
                # The subtitle flag rules, for catalogs built before they were
                # measured (see _subtitle_stream).
                stream.pop("embeddedInVideo", None)
                if stream.get("codec") in SUBTITLE_CODECS:
                    stream["codec"] = SUBTITLE_CODECS[stream["codec"]]
                for flag in ("default", "forced"):
                    if not stream.get(flag):
                        stream.pop(flag, None)
                stream.setdefault("canAutoSync", False)
    return media
