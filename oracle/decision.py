"""The decision a real PMS makes for a client that does not send `hasMDE=1`.

Cycle 32. A plain-form decision runs the CLIENT's own profile against the item, and the
profile is a file the reference ships: its `Resources/Profiles/` was read out of the lab
image read-only (78 `<Client>` files, parsed twice by two different parsers with identical
results) into `client_profiles.json`. The rules below were fitted to the decision endpoint
itself and scored cell by cell: plexlab (1.43.4) 78 profiles x 8 rows, the 1.42.2 reference
78 x 16 rows chosen for their containers, codecs, channel counts, bit depths and subtitle
kinds, and cycle 34's 78 cells on a row MADE to carry two audio tracks with no language at
all -- 1,950 cells, exact in outcome and refusal TEXT on 1,906.

All 44 it misses are the cells of the 1.42.2 reference's row 19813, and they are a state
rather than a shape: a row whose tracks name no language answers differently before any
client has chosen a track (41 profiles refuse it as "not the first audio stream") and after
one has, with the item's `Stream` rows byte-identical either way. rex carries a chosen
track, so the model is the after; the before is measured in docs/findings.md and not
emulated.

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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_ROOT, "data", "client_profiles.json"), encoding="utf-8") as _handle:
    PROFILES = json.load(_handle)

# The subtitle codecs a `SubtitleProfile` target can convert. An image subtitle cannot be
# burned into a direct play: Samsung declares an srt target and still refuses the 1.42.2
# reference's row 1062 for its selected PGS track, where it plays the same file's text
# tracks.
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
    """The audio stream the server would PLAY, or None.

    Cycle 32 re-picked the widest DEFAULT track here when the selected one carried no
    language, fitted to the 1.42.2 reference's row 19813 -- the one measured row whose two
    audio tracks name none. Cycle 34 made that row's shape on plexscratch and found the
    re-pick is not a property of the row at all: the same bytes answer 41 of 78 profiles
    "not the first audio stream" before any client has chosen a track and answer every one
    of the 78 from the visible row afterwards, with `Stream` byte-identical either way. See
    the module docstring; the selection rex holds is the one a client has made.
    """
    audio = [s for s in _streams(media) if s.get("streamType") == 2]
    if not audio:
        return None
    selected = [s for s in audio if s.get("selected")]
    return selected[0] if selected else audio[0]


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
            # four 6-channel aac rows on the 1.42.2 reference; tvOS carries the same bound
            # without it and refuses the same four.
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
            # Cycle 35, measured on the 1.42.2 reference's row 3831 against Android: a
            # limitation whose comparison came from `list=` rather than `value=` names the
            # list and then appends ` [list]` -- `high 10 != baseline|main|high [list]`.
            # The corpus's other 1,949 cells are all `value=` comparisons, which carry no
            # suffix; no measured row trips a `substring=` one, so that form is left alone.
            wanted = limit.get("value") or limit.get("list") or limit.get("substring")
            if limit.get("value") is None and limit.get("list") is not None:
                wanted = "%s [list]" % wanted
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
    1,950 measured (profile, row) cells and exact on 1,906 of them -- every miss being
    the 1.42.2 reference's row 19813 read in its pre-selection state, which the row does
    not carry.
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
    # The SELECTED stream is refused before anything is matched: the 1.42.2 reference's
    # rows 2925, 2157 and 4969 select their second track and 66 profiles each answer this,
    # never a profile text.
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
    # EVERY audio stream has to be direct-playable, not only the selected one, and the
    # first in INDEX order that is not is the codec the refusal names. A profile that
    # declares `StreamUnselectedIncompatibleAudioStreams` skips the UNSELECTED ones --
    # the server streams those separately -- which is the whole difference between
    # Windows Phone (aac,mp3: direct plays the made row) and Universal TV (aac,ac3:
    # refuses it naming the mp3 it does not list), the two being otherwise alike.
    skip_unselected = any(setting.get("name")
                          == "StreamUnselectedIncompatibleAudioStreams"
                          and setting.get("value") == "true"
                          for setting in profile.get("settings") or [])
    audio_codec = selected_audio.get("codec") if selected_audio is not None else None
    for stream in audio_streams:
        if skip_unselected and stream is not selected_audio:
            continue
        codec = stream.get("codec")
        if not [entry for entry in candidates
                if not entry.get("audioCodec") or codec is None
                or codec in _names(entry.get("audioCodec"))]:
            return False, (NO_PROFILE + ", video codec %s, and audio codec %s.") % (
                container, video_codec, codec)
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
    # Cycle 44: the SUBTITLE twin of the audio rule above,
    # and it runs after the audio-codec
    # pass AND after the subtitle-CODEC pass:, and it was missing entirely.
    # Measured on the 1.42.2 reference over 21 rows whose selected EMBEDDED subtitle is not
    # the first embedded one -- 917, 4315, 10442, 2721, 2124, 4583, 4641, 4853, 10279 and
    # eleven Media of 1637 -- and on a file built for it on plexscratch (1.43.4): exactly
    # three of the 78 profiles refuse, `Samsung`, `RokuDLNA-5000X-7.x` and
    # `RokuDLNA-6000X-7.x`, with
    #
    #     App cannot direct play this item. Selected embedded subtitle stream is not the
    #     first embedded subtitle stream.
    #
    # The gate is the same `DirectPlayStreamSelection` as the audio rule: on the 1.42.2
    # reference's row 917 only four profiles reach this stage at all, and the one that
    # declares it (`Windows 10 Desktop`) direct-plays the row while the three that do not
    # refuse it.
    #
    # The 2,106-cell corpus could not see this. In every corpus row whose selected
    # subtitle is not first (the 1.42.2 reference's rows 11901, 2157, 2925, 4969) the
    # selected AUDIO is not first either, so the audio refusal fires above and masks it --
    # a sample that cannot disagree. It is reachable the moment a client uses its subtitle
    # picker: rex told all three profiles "Direct play OK." for a row they will not render,
    # which is cycle 26's failure mode from the other side.
    if subtitle is not None and not subtitle.get("key") and not can_select:
        embedded = [s for s in streams
                    if s.get("streamType") == 3 and not s.get("key")]
        if embedded and subtitle is not embedded[0]:
            return False, ("App cannot direct play this item. Selected embedded subtitle "
                           "stream is not the first embedded subtitle stream.")
    values = _values(media, selected_audio)
    # Limitations: the CONTAINER's first, then a codec profile that names the codec, then
    # one that says `*`. The 1.42.2 reference's row 848 on Android reports
    # `part.optimizedForStreaming` before an audio channel bound it also fails, and its row
    # 19754 on Sony Bravia 2011 reports its h264 `video.level` bound before the `*`
    # profile's `video.width` bound that comes first in the file.
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


def _dolby_vision_profile(media):
    """The DoVi profile of the media's first video stream, or None."""
    video = next((s for s in _streams(media) if s.get("streamType") == 1), {})
    return video.get("DOVIProfile") if video.get("DOVIPresent") else None


def conversion(profile, protocol, media=None):
    """"convert", "neither" or "refuse" for a request whose direct play was refused."""
    source_codec = None
    if media is not None and _number(_dolby_vision_profile(media)) == 5:
        # Cycle 36, measured on the 1.42.2 reference over three Dolby Vision PROFILE 5 rows
        # (967 and 1120, mkv/hevc/1080; 4813, mp4/hevc/4k) against all 78 profiles: the
        # server refuses to convert a profile-5 source at all. `transcodeDecisionCode
        # 2003`, "File is unplayable. DoVi (Profile 5) color space is not supported." --
        # where the same 78 profiles against a profile 7 or 8 row, and against the two
        # non-DV hevc controls already in the corpus, answer the ordinary 4002. So it is
        # keyed on the DV profile and not on hevc, not on the container and not on server
        # load.
        #
        # It is not a blanket veto: ONE of the 38 profiles that declare a matching http
        # target still converts it, and that one is the only one whose target names the
        # SOURCE's own video codec (`Plex Desktop`, `videoCodec="h264,hevc,*"`). Every
        # other target would re-encode to h264 and lose the colour space. Exact on all 78
        # cells in both directions.
        #
        # Left open: `h264,hevc,*` is the only positive
        # witness on either reference, so "the target names the source codec" and "the
        # target declares `*`" fit the same 78 cells. The narrower of the two is built.
        video = next((s for s in _streams(media) if s.get("streamType") == 1), {})
        source_codec = str(video.get("codec") or "").lower()
    for target in profile.get("targets") or []:
        if target.get("tag") != "VideoProfile" or target.get("context") == "static":
            # Universal Recording's only http target says `context="static"`, and both
            # references refuse it.
            continue
        names = [piece.strip().lower() for piece
                 in str(target.get("videoCodec") or "").split(",") if piece.strip()]
        blocked = source_codec is not None and source_codec not in names
        declared = target.get("protocol")
        if declared == protocol:
            if blocked:
                continue
            return "convert"
        if declared is None and protocol == "http":
            # Xbox 360's one video target is an asf with no protocol: a 400 on both
            # references, for every row it cannot direct play. One profile. The refusal
            # is the ROUTE's, so the Dolby Vision veto above does not reach it -- Xbox
            # 360 answers its 400 on a profile-5 row as on every other.
            if target.get("container") == "asf":
                return "refuse"
            if blocked:
                continue
            return "convert"
    return "neither"


def dolby_vision_blocked(name, media, protocol="http"):
    """Whether the server would refuse to CONVERT this media on its DoVi profile.

    True only when the source is Dolby Vision profile 5 and the client's profile has a
    matching http target that does NOT name the source's own video codec -- which is the
    case the reference answers `transcodeDecisionCode 2003` rather than 4005. A client
    with no target at all gets the ordinary 4005, and the one profile whose target names
    hevc converts the row.
    """
    profile = resolve(name)
    if profile is None or media is None:
        return False
    if _number(_dolby_vision_profile(media)) != 5:
        return False
    return conversion(profile, protocol, None) == "convert" \
        and conversion(profile, protocol, media) != "convert"


def decide(name, media, protocol="http", direct=True):
    profile = resolve(name)
    if profile is None:
        return "refuse", None
    text = "App cannot direct play this item. Direct play is disabled."
    if direct:
        ok, text = direct_play(profile, media)
        if ok:
            return "directplay", text
    return conversion(profile, protocol or "http", media), text
