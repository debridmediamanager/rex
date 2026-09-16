"""The decision a real PMS makes for a client that does not send `hasMDE=1`.

Cycle 32. A plain-form decision runs the CLIENT's own profile against the item, and the
profile is a file the reference ships: its `Resources/Profiles/` was read out of the lab
image read-only (78 `<Client>` files, parsed twice by two different parsers with identical
results) into `client_profiles.json`. The rules below were fitted to the decision endpoint
itself and scored cell by cell: kids (1.43.4) 78 profiles x 8 rows, zen (1.42.2) 78 x 16
rows chosen for their containers, codecs, channel counts, bit depths and subtitle kinds --
1,872 cells, of which the outcome is exact on 1,871 and the refusal TEXT on 1,864. The
eight it misses are all zen 19813, the one row whose audio tracks carry no language; they
are recorded as open (HANDOVER 3.3) rather than fitted to a single witness.

In order, each refusal naming itself in the reference's own words:

  1. the requested profile FILE is resolved through its `redirect=` chain;
  2. a SELECTED audio stream that is not the first one refuses, unless the profile
     declares `DirectPlayStreamSelection` -- before any profile is matched;
  3. a `<DirectPlayProfiles><VideoProfile>` must name the container and video codec;
  4. the stream the server would PLAY (the selected one, or with no language the default
     with the most channels) that is not the first refuses the same way;
  5. a selected SIDECAR subtitle needs a `SubtitleProfile` somewhere in the profile;
  6. the entry must name the selected audio codec, then a selected embedded subtitle's
     codec -- and the refusal text says how far the match got;
  7. every `<Limitations>` that applies: the container's first, then a codec profile that
     names the codec, then one that says `*`; `onlyTranscodes="true"` is skipped;
  8. with direct play refused, a `<TranscodeTargets><VideoProfile>` for the request's
     protocol decides between "convert" and "neither"; `context="static"` never serves,
     and Xbox 360's asf-only target is a 400.

The answer is `(outcome, text)`: outcome `directplay`, `convert`, `neither` or `refuse`,
and `text` the whole `directPlayDecisionText`.
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "client_profiles.json"), encoding="utf-8") as _handle:
    PROFILES = json.load(_handle)

# The subtitle codecs a `SubtitleProfile` target can convert. An image subtitle cannot be
# burned into a direct play: Samsung declares an srt target and still refuses zen 1062's
# selected PGS track, where it plays the same file's text tracks.
IMAGE_SUBTITLES = frozenset(["pgs", "hdmv_pgs_subtitle", "dvd_subtitle", "vobsub",
                             "dvb_subtitle", "dvbsub", "xsub"])

NO_PROFILE = "App cannot direct play this item. No direct play video profile exists for " \
             "protocol http, with container %s"


def resolve(name):
    """The profile a FILE name loads, following `redirect=` to the one that declares."""
    profile = PROFILES.get(name)
    seen = set()
    while profile is not None and profile.get("redirect") \
            and profile["redirect"] not in seen:
        seen.add(profile["redirect"])
        profile = PROFILES.get(profile["redirect"])
    return profile


def _names(value):
    return [entry.strip() for entry in (value or "").split(",") if entry.strip()]


def _number(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _shown(value):
    number = _number(value)
    if number is not None and number == int(number):
        return "%d" % number
    return str(value)


def _streams(media):
    parts = media.get("Part") or [{}]
    return list(parts[0].get("Stream") or [])


def evaluated_audio(media):
    """The audio stream the server would PLAY, or None."""
    audio = [s for s in _streams(media) if s.get("streamType") == 2]
    if not audio:
        return None
    selected = [s for s in audio if s.get("selected")]
    chosen = selected[0] if selected else audio[0]
    if not chosen.get("language") and not chosen.get("languageCode"):
        # zen 19813 stores its 2-channel mp3 as selected and names no language on either
        # track, and 41 profiles refuse it as "not the first audio stream" -- which only
        # the 6-channel ac3 can be. One witness, and see HANDOVER 3.3.
        defaults = [s for s in audio if s.get("default")] or audio
        chosen = max(defaults, key=lambda s: (s.get("channels") or 0,
                                              -audio.index(s)))
    return chosen


def _values(media, audio):
    video = next((s for s in _streams(media) if s.get("streamType") == 1), {})
    part = (media.get("Part") or [{}])[0]
    optimized = part.get("optimizedForStreaming", media.get("optimizedForStreaming"))
    values = {
        "video.width": video.get("width") or media.get("width"),
        "video.height": video.get("height") or media.get("height"),
        "video.bitDepth": video.get("bitDepth"),
        "video.level": video.get("level"),
        "video.profile": video.get("profile"),
        "video.frameRate": video.get("frameRate"),
        "video.bitrate": video.get("bitrate"),
        "video.refFrames": video.get("refFrames"),
        "video.anamorphic": 1 if video.get("anamorphic") else 0,
        "video.hasScalingMatrix": 1 if video.get("hasScalingMatrix") else 0,
        "video.scanType": video.get("scanType"),
        "video.separateFields": 0,
        "part.optimizedForStreaming": 1 if optimized in (1, True, "1") else 0,
        "part.has64bitOffsets": 1 if part.get("has64bitOffsets") else 0,
    }
    if audio is not None:
        values.update({
            "audio.channels": audio.get("channels"),
            "audio.samplingRate": audio.get("samplingRate"),
            "audio.bitrate": audio.get("bitrate"),
            "audio.profile": audio.get("profile"),
            "audio.bitDepth": audio.get("bitDepth"),
        })
    return values


def _limitation(limitations, values):
    """The first limitation the item fails, in the reference's words, or None."""
    for limit in limitations:
        if limit.get("onlyTranscodes") == "true":
            # iOS.xml bounds `audio.channels` at 2 with this attribute and direct plays
            # four 6-channel aac rows on zen; tvOS carries the same bound without it and
            # refuses the same four.
            continue
        name, tag = limit.get("name"), limit.get("tag")
        actual = values.get(name)
        if tag in ("UpperBound", "LowerBound"):
            have, bound = _number(actual), _number(limit.get("value"))
            if have is None or bound is None:
                continue
            if tag == "UpperBound" and have > bound:
                return "%s limitation applies: %s > %s" % (name, _shown(actual),
                                                          _shown(limit["value"]))
            if tag == "LowerBound" and have < bound:
                return "%s limitation applies: %s < %s" % (name, _shown(actual),
                                                          _shown(limit["value"]))
        elif tag in ("Match", "NotMatch"):
            if actual is None:
                continue
            text = str(actual).lower()
            wanted = limit.get("value") or limit.get("list") or limit.get("substring")
            if limit.get("substring") is not None:
                hit = limit["substring"].lower() in text
            elif limit.get("list") is not None:
                hit = text in [entry.lower() for entry in limit["list"].split("|")]
            else:
                expected = str(limit.get("value")).lower()
                hit = text == expected or (
                    _number(actual) is not None and _number(expected) is not None
                    and _number(actual) == _number(expected))
            if tag == "Match" and not hit:
                return "%s limitation applies: %s != %s" % (name, _shown(actual), wanted)
            if tag == "NotMatch" and hit:
                return "%s limitation applies: %s == %s" % (name, _shown(actual), wanted)
    return None


def _has_subtitle_target(profile):
    return any(entry.get("tag") == "SubtitleProfile"
               for block in ("targets", "directplay")
               for entry in profile.get(block) or [])


def direct_play(profile, media):
    """(True, "Direct play OK.") or (False, the directPlayDecisionText refusing it).

    The checks run in the order the reference's refusal texts put them, fitted over all
    1,872 measured (profile, row) cells and exact on 1,864 of them. The eight it misses
    are all zen 19813, the one row whose two audio tracks carry no language, and are
    recorded as open in HANDOVER 3.3 rather than fitted to a single witness.
    """
    streams = _streams(media)
    audio_streams = [s for s in streams if s.get("streamType") == 2]
    selected_audio = next((s for s in audio_streams if s.get("selected")),
                          audio_streams[0] if audio_streams else None)
    audio = evaluated_audio(media)
    can_select = any(setting.get("name") == "DirectPlayStreamSelection"
                     and setting.get("value") == "true"
                     for setting in profile.get("settings") or [])
    not_first = ("App cannot direct play this item. Selected audio stream is not the first "
                 "audio stream and app cannot select audio streams in direct play.")
    # The SELECTED stream is refused before anything is matched: zen 2925, 2157 and 4969
    # select their second track and 66 profiles each answer this, never a profile text.
    if selected_audio is not None and selected_audio is not audio_streams[0] \
            and not can_select:
        return False, not_first
    container = media.get("container")
    video_codec = media.get("videoCodec")
    candidates = [entry for entry in profile.get("directplay") or []
                  if entry.get("tag") == "VideoProfile" and not entry.get("protocol")
                  and container in _names(entry.get("container"))
                  and (not entry.get("codec") or video_codec in _names(entry.get("codec")))]
    if not candidates:
        return False, (NO_PROFILE + ", and video codec %s.") % (container, video_codec)
    # The stream the server would PLAY is refused next, once the container matched.
    if audio is not None and audio is not audio_streams[0] and not can_select:
        return False, not_first
    subtitle = next((s for s in streams if s.get("streamType") == 3 and s.get("selected")),
                    None)
    if subtitle is not None and subtitle.get("key") and not _has_subtitle_target(profile):
        return False, ("App cannot direct play this item. Selected sidecar subtitle stream "
                       "cannot be direct played.")
    # The audio codec a profile entry is matched on is the SELECTED track's: zen 19813's
    # refusals name its selected mp3 on Android, Popcorn Hour and the Universal profiles.
    audio_codec = selected_audio.get("codec") if selected_audio is not None else None
    candidates = [entry for entry in candidates
                  if not entry.get("audioCodec") or audio_codec is None
                  or audio_codec in _names(entry.get("audioCodec"))]
    if not candidates:
        return False, (NO_PROFILE + ", video codec %s, and audio codec %s.") % (
            container, video_codec, audio_codec)
    if subtitle is not None and not subtitle.get("key"):
        convertible = _has_subtitle_target(profile) \
            and subtitle.get("codec") not in IMAGE_SUBTITLES
        candidates = [entry for entry in candidates
                      if convertible or subtitle.get("codec") in
                      _names(entry.get("subtitleCodec")) + _names(entry.get("subtitleFormat"))]
        if not candidates:
            return False, (NO_PROFILE + ", using video codec %s, audio codec %s, and "
                                        "subtitle codec %s.") % (
                container, video_codec, audio_codec, subtitle.get("codec"))
    values = _values(media, selected_audio)
    # Limitations: the CONTAINER's first, then a codec profile that names the codec, then
    # one that says `*`. zen 848 on Android reports `part.optimizedForStreaming` before an
    # audio channel bound it also fails, and zen 19754 on Sony Bravia 2011 reports its h264
    # `video.level` bound before the `*` profile's `video.width` bound that comes first in
    # the file.
    groups = []
    for holder in profile.get("containerprofiles") or []:
        names = _names(holder.get("name"))
        if holder.get("tag") == "VideoContainer" and ("*" in names or container in names):
            groups.append((0, holder))
    for codec in profile.get("codecprofiles") or []:
        names = _names(codec.get("name"))
        if codec.get("tag") == "VideoCodec" and ("*" in names or video_codec in names) \
                or codec.get("tag") == "VideoAudioCodec" and selected_audio is not None \
                and ("*" in names or audio_codec in names):
            groups.append((2 if "*" in names else 1, codec))
    for _rank, group in sorted(groups, key=lambda pair: pair[0]):
        reason = _limitation(group.get("limitations") or [], values)
        if reason:
            return False, "This app cannot play this item. The reason is: %s." % reason
    return True, "Direct play OK."


def conversion(profile, protocol):
    """"convert", "neither" or "refuse" for a request whose direct play was refused."""
    for target in profile.get("targets") or []:
        if target.get("tag") != "VideoProfile" or target.get("context") == "static":
            # Universal Recording's only http target says `context="static"`, and both
            # references refuse it.
            continue
        declared = target.get("protocol")
        if declared == protocol:
            return "convert"
        if declared is None and protocol == "http":
            # Xbox 360's one video target is an asf with no protocol: a 400 on both
            # references, for every row it cannot direct play. One profile.
            return "refuse" if target.get("container") == "asf" else "convert"
    return "neither"


def decide(name, media, protocol="http", direct=True):
    profile = resolve(name)
    if profile is None:
        return "refuse", None
    text = "App cannot direct play this item. Direct play is disabled."
    if direct:
        ok, text = direct_play(profile, media)
        if ok:
            return "directplay", text
    return conversion(profile, protocol or "http"), text
