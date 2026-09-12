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

_LANGUAGE_NAMES = {
    "eng": "English",
    "chi": "Chinese",
    "zho": "Chinese",
    "spa": "Spanish",
    "fra": "French",
    "fre": "French",
    "deu": "German",
    "ger": "German",
    "jpn": "Japanese",
    "por": "Portuguese",
    "ita": "Italian",
    "rus": "Russian",
    "kor": "Korean",
    "nld": "Dutch",
    "dut": "Dutch",
}

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


def _video_resolution(height, width):
    if not height:
        return None
    if width and width >= 3000:
        return "4k"
    for cutoff, label in ((1080, "1080"), (720, "720"), (576, "576"), (480, "480")):
        if height >= cutoff - 8:
            return label
    return "sd"


def _channel_layout(channels):
    return {1: "mono", 2: "stereo", 6: "5.1(side)", 8: "7.1"}.get(channels)


def _channel_label(channels):
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(channels, "%dch" % channels)


def _language_fields(tags):
    code = (tags.get("language") or "").lower() or None
    if code in (None, "und"):
        return {}
    return {
        "language": _LANGUAGE_NAMES.get(code, code),
        "languageCode": code,
        "languageTag": code[:2],
    }


def _video_stream(stream, stream_id):
    width = stream.get("width")
    height = stream.get("height")
    row = {
        "id": stream_id,
        "streamType": 1,
        "default": True,
        "codec": stream.get("codec_name"),
        "index": stream.get("index"),
        "width": width,
        "height": height,
        "profile": (stream.get("profile") or "").lower() or None,
        "level": stream.get("level"),
        "refFrames": stream.get("refs"),
        "bitDepth": stream.get("bits_per_raw_sample") or 8,
        "chromaSubsampling": stream.get("pix_fmt") and _chroma(stream["pix_fmt"]),
        "colorPrimaries": stream.get("color_primaries"),
        "colorTrc": stream.get("color_transfer"),
        "colorSpace": stream.get("color_space"),
        "frameRate": _float_or_none(stream.get("r_frame_rate")),
        "scanType": "progressive",
    }
    codec = (stream.get("codec_name") or "").upper()
    resolution = _video_resolution(height, width)
    if resolution and codec:
        label = "4K" if resolution == "4k" else resolution + "p"
        row["displayTitle"] = "%s (%s)" % (label, codec)
    bitrate = stream.get("bit_rate")
    if bitrate:
        row["bitrate"] = int(int(bitrate) / 1000)
    return row


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


def _audio_stream(stream, stream_id, selected):
    tags = stream.get("tags") or {}
    channels = stream.get("channels") or 2
    codec = stream.get("codec_name") or ""
    row = {
        "id": stream_id,
        "streamType": 2,
        "selected": selected,
        "default": selected,
        "codec": codec,
        "index": stream.get("index"),
        "channels": channels,
        "audioChannelLayout": _channel_layout(channels),
        "samplingRate": stream.get("sample_rate") and int(stream["sample_rate"]),
        "profile": (stream.get("profile") or "").lower() or None,
    }
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
    return row


def _subtitle_stream(stream, stream_id):
    tags = stream.get("tags") or {}
    disposition = stream.get("disposition") or {}
    codec = stream.get("codec_name") or ""
    # Plex names the SRT codec "srt"; ffprobe calls it subrip.
    codec = {"subrip": "srt", "mov_text": "mov_text"}.get(codec, codec)
    row = {
        "id": stream_id,
        "streamType": 3,
        "codec": codec,
        "index": stream.get("index"),
        "default": bool(disposition.get("default")),
        "forced": bool(disposition.get("forced")),
        # Embedded tracks live inside the part; a sidecar would set `key`.
        "embeddedInVideo": True,
    }
    row.update(_language_fields(tags))
    if tags.get("title"):
        row["title"] = tags["title"]
    row["displayTitle"] = tags.get("title") or row.get("language", "Unknown")
    return row


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

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]

    duration_ms = int(float(fmt.get("duration") or 0) * 1000)
    bitrate_kbps = int(int(fmt.get("bit_rate") or 0) / 1000) or None

    rows = []
    next_id = part_id * 100
    if video is not None:
        next_id += 1
        rows.append(_video_stream(video, next_id))
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
        "hasThumbnail": False,
        # A real PMS reports these after its own accessibility check; plezy
        # re-fetches metadata in a loop until every part carries all three of
        # Stream / exists / accessible, so they are not optional.
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
        "aspectRatio": round(width / height, 2) if width and height else None,
        "audioChannels": first_audio.get("channels"),
        "audioCodec": first_audio.get("codec_name"),
        "videoCodec": video.get("codec_name") if video else None,
        "videoResolution": _video_resolution(height, width),
        "container": container,
        "videoFrameRate": _frame_rate_token(video.get("r_frame_rate")) if video else None,
        "videoProfile": video_profile,
        "audioProfile": (first_audio.get("profile") or "").lower() or None,
        # plexsim never repackages, so the stream is exactly what the source is.
        "optimizedForStreaming": False,
        "has64bitOffsets": False,
        "Part": [part],
    }
    return media
