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

# displayTitle for an audio track is what the client's track picker shows.
_AUDIO_CODEC_LABELS = {
    "ac3": "Dolby Digital",
    "eac3": "Dolby Digital Plus",
    "truehd": "Dolby TrueHD",
    "dts": "DTS",
    "aac": "AAC",
    "opus": "Opus",
    "flac": "FLAC",
    "mp3": "MP3",
}

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


def _channel_label(channels):
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(channels, "%dch" % channels)


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


# Codec names as Plex labels them in a track title.
_CODEC_LABELS = {"h264": "H.264", "hevc": "HEVC", "mpeg4": "MPEG-4", "vp8": "VP8",
                 "vp9": "VP9", "av1": "AV1", "eac3": "EAC3", "ac3": "AC3",
                 "aac": "AAC", "dts": "DTS", "opus": "OPUS", "flac": "FLAC",
                 "truehd": "TRUEHD", "mp3": "MP3", "vorbis": "VORBIS",
                 "srt": "SRT", "ass": "ASS", "subrip": "SRT", "pgs": "PGS"}


def stream_titles(row):
    """Set `displayTitle` and `extendedDisplayTitle` on one built Stream row.

    These two are what the client's quality and track pickers print, and the
    split between them is not the obvious one. Measured on two real servers:

      video     displayTitle `1080p`              extended `1080p (H.264)`
      audio     displayTitle `日本語 (OPUS 5.1)`   extended the same
      subtitle  displayTitle `English`            extended `English (ASS)`
      subtitle  displayTitle `English (Weeb)`     extended `Honorifics (English (Weeb) ASS)`
                with a track title

    So the codec belongs in the EXTENDED title, not the plain one -- plexsim had
    it the other way round for video -- and a subtitle with a track title shows
    its LANGUAGE as the plain title, with the track name only in the extended
    one. plexsim showed the track name, which is a different string in the
    picker from what every real client shows.

    One measured difference is deliberately not reproduced: a 10-bit HEVC track
    on fun extends to `1080p (HEVC Main 10)` while an 8-bit H.264 one on the
    reference extends to `1080p (H.264)` with no profile at all. Two samples do
    not give the rule for when the profile is appended, so it is left off.
    """
    codec = str(row.get("codec") or "")
    label = _CODEC_LABELS.get(codec.lower(), codec.upper())
    kind = row.get("streamType")
    language = row.get("language")
    title = row.get("title")
    if kind == 1:
        resolution = row.get("displayTitle")
        # A previously built row may already carry the codec; take the plain
        # resolution back off it so this stays idempotent.
        if resolution and " (" in resolution:
            resolution = resolution.split(" (")[0]
        if not resolution:
            return row
        row["displayTitle"] = resolution
        row["extendedDisplayTitle"] = ("%s (%s)" % (resolution, label) if label
                                       else resolution)
    elif kind == 2:
        if row.get("displayTitle"):
            row["extendedDisplayTitle"] = row["displayTitle"]
    elif kind == 3:
        plain = language or title or "Unknown"
        row["displayTitle"] = plain
        if title and title != plain:
            row["extendedDisplayTitle"] = "%s (%s %s)" % (title, plain, label)
        else:
            row["extendedDisplayTitle"] = ("%s (%s)" % (plain, label) if label
                                           else plain)
    return row


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
        "width": width,
        "height": height,
        "profile": (stream.get("profile") or "").lower() or None,
        "level": stream.get("level"),
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
        "codedHeight": stream.get("coded_height"),
        "codedWidth": stream.get("coded_width"),
        "colorPrimaries": stream.get("color_primaries"),
        "colorTrc": stream.get("color_transfer"),
        "colorSpace": stream.get("color_space"),
        # Measured: ffprobe's `color_range` as it is, and absent where it has none.
        "colorRange": stream.get("color_range"),
        "frameRate": _float_or_none(stream.get("r_frame_rate")),
        "scanType": _scan_type(stream.get("field_order")),
    }
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
    row.update(_disposition(stream, ("default", "forced", "original")))
    codec = (stream.get("codec_name") or "").upper()
    resolution = _video_resolution(height, width)
    if resolution and codec:
        label = "4K" if resolution == "4k" else resolution + "p"
        row["displayTitle"] = "%s (%s)" % (label, codec)
    bitrate = stream.get("bit_rate")
    if bitrate:
        row["bitrate"] = int(int(bitrate) / 1000)
    return stream_titles(row)


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
        "channels": channels,
        # ffprobe's layout verbatim, measured on 126 of 130 tracks on fun, and absent where
        # it has none. plexsim derived it from the channel count, so a `5.1` track was
        # `5.1(side)` and a 3-, 4-, 5- or 7-channel one had no layout.
        "audioChannelLayout": stream.get("channel_layout") or None,
        "samplingRate": stream.get("sample_rate") and int(stream["sample_rate"]),
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
        row["bitrate"] = int(int(bitrate) / 1000)
    # Prefer the container's own track title; it is what a real PMS surfaces.
    label = _AUDIO_CODEC_LABELS.get(codec, codec.upper())
    row["displayTitle"] = tags.get("title") or "%s (%s %s)" % (
        row.get("language", "Unknown"),
        label,
        _channel_label(channels),
    )
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
    }
    row.update(_disposition(stream, TRACK_FLAGS))
    bitrate = stream.get("bit_rate")
    if bitrate:
        row["bitrate"] = int(int(bitrate) / 1000)
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
    bitrate_kbps = int(int(fmt.get("bit_rate") or 0) / 1000) or None

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
    first_audio = audios[0] if audios else {}
    container = _CONTAINER_ALIASES.get(
        fmt.get("format_name"), (fmt.get("format_name") or "").split(",")[0]
    )
    # The shared matroska/webm demuxer cannot tell the two apart; the extension
    # can, and it is what a real PMS reports.
    if container == "mkv" and (container_hint or "").lower() == "webm":
        container = "webm"
    video_profile = (video.get("profile") or "").lower() if video else None

    part = {
        "id": part_id,
        "key": part_key,
        "duration": duration_ms,
        "file": file_path,
        "size": size,
        "container": container,
        "videoProfile": video_profile,
        "audioProfile": (first_audio.get("profile") or "").lower() or None,
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
        "audioChannels": first_audio.get("channels"),
        "audioCodec": first_audio.get("codec_name"),
        "videoCodec": video.get("codec_name") if video else None,
        "videoResolution": _video_resolution(height, width),
        "container": container,
        "videoFrameRate": _frame_rate_token(video.get("r_frame_rate")) if video else None,
        "videoProfile": video_profile,
        "audioProfile": (first_audio.get("profile") or "").lower() or None,
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
