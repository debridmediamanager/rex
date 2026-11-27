//! The measured plain-form Plex decision engine.
//!
//! `client_profiles.json` is the profile resource shipped by the reference
//! PMS.  The functions below preserve the profile's arbitrary JSON fields and
//! apply the measured check order from the Python oracle.  The fixture corpus
//! tests every selected outcome and the exact text recorded for status-200
//! decisions; status-400 refusal cells intentionally assert only the outcome,
//! matching the Python oracle's corpus contract.

use std::collections::{HashMap, HashSet};

use serde_json::{Map, Value};

pub type Profile = Map<String, Value>;
pub type Profiles = HashMap<String, Profile>;

pub(crate) const IMAGE_SUBTITLES: &[&str] = &[
    "pgs",
    "hdmv_pgs_subtitle",
    "dvd_subtitle",
    "vobsub",
    "dvb_subtitle",
    "dvbsub",
    "xsub",
];

const NO_PROFILE: &str =
    "App cannot direct play this item. No direct play video profile exists for protocol http, with container ";

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    DirectPlay,
    Convert,
    Neither,
    Refuse,
}

impl Outcome {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::DirectPlay => "directplay",
            Self::Convert => "convert",
            Self::Neither => "neither",
            Self::Refuse => "refuse",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Decision {
    pub outcome: Outcome,
    pub text: Option<String>,
}

pub fn parse_profiles(source: &str) -> Result<Profiles, serde_json::Error> {
    serde_json::from_str(source)
}

fn array(value: Option<&Value>) -> Option<&Vec<Value>> {
    value.and_then(Value::as_array)
}

fn text(value: Option<&Value>) -> Option<&str> {
    value.and_then(Value::as_str)
}

/// Python's truth test is used by the reference for optional profile fields.
fn truthy(value: Option<&Value>) -> bool {
    value.is_some_and(crate::metadata::truthy)
}

fn number(value: Option<&Value>) -> Option<f64> {
    match value {
        Some(Value::Bool(value)) => Some(if *value { 1.0 } else { 0.0 }),
        Some(Value::Number(value)) => value.as_f64(),
        Some(Value::String(value)) => value.trim().parse::<f64>().ok(),
        _ => None,
    }
}

fn python_string(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::Bool(value)) => {
            if *value {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Some(Value::Number(value)) => value.to_string(),
        Some(Value::String(value)) => value.clone(),
        Some(Value::Array(value)) => format!("{value:?}"),
        Some(Value::Object(value)) => format!("{value:?}"),
    }
}

fn python_string_text(value: Option<&str>) -> String {
    value.unwrap_or("None").to_string()
}

fn shown(value: Option<&Value>) -> String {
    let rendered = python_string(value);
    match number(value) {
        Some(number) if number == number.trunc() => format!("{number:.0}"),
        _ => rendered,
    }
}

fn names(value: Option<&Value>) -> Vec<&str> {
    text(value)
        .unwrap_or("")
        .split(',')
        .map(str::trim)
        .filter(|entry| !entry.is_empty())
        .collect()
}

fn entries<'a>(object: &'a Map<String, Value>, key: &str) -> Vec<&'a Map<String, Value>> {
    array(object.get(key))
        .into_iter()
        .flat_map(|values| values.iter().filter_map(Value::as_object))
        .collect()
}

fn stream_type(stream: &Map<String, Value>) -> Option<f64> {
    match stream.get("streamType") {
        Some(Value::Number(value)) => value.as_f64(),
        _ => None,
    }
}

fn streams(media: &Value) -> Vec<&Map<String, Value>> {
    let Some(media) = media.as_object() else {
        return Vec::new();
    };
    let Some(parts) = array(media.get("Part")) else {
        return Vec::new();
    };
    let Some(part) = parts.first().and_then(Value::as_object) else {
        return Vec::new();
    };
    array(part.get("Stream"))
        .into_iter()
        .flat_map(|values| values.iter().filter_map(Value::as_object))
        .collect()
}

fn first_audio(streams: &[&Map<String, Value>]) -> Option<usize> {
    streams
        .iter()
        .position(|stream| stream_type(stream) == Some(2.0))
}

fn audio_indices(streams: &[&Map<String, Value>]) -> Vec<usize> {
    streams
        .iter()
        .enumerate()
        .filter_map(|(index, stream)| (stream_type(stream) == Some(2.0)).then_some(index))
        .collect()
}

fn selected_audio(streams: &[&Map<String, Value>]) -> Option<usize> {
    let audio = audio_indices(streams);
    audio
        .iter()
        .copied()
        .find(|index| truthy(streams[*index].get("selected")))
        .or_else(|| audio.first().copied())
}

/// Return the index in the first Part's stream list that the server would play.
pub fn evaluated_audio(media: &Value) -> Option<&Map<String, Value>> {
    let stream_list = streams(media);
    selected_audio(&stream_list).map(|index| stream_list[index])
}

fn value_or<'a>(object: &'a Map<String, Value>, key: &str, fallback: Option<&'a Value>) -> Value {
    object
        .get(key)
        .filter(|value| truthy(Some(value)))
        .cloned()
        .or_else(|| fallback.filter(|value| truthy(Some(value))).cloned())
        .unwrap_or(Value::Null)
}

fn one_if_truthy(value: Option<&Value>) -> Value {
    Value::from(if truthy(value) { 1 } else { 0 })
}

fn is_one(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Bool(true)) => true,
        Some(Value::Number(value)) => number(Some(&Value::Number(value.clone()))) == Some(1.0),
        Some(Value::String(value)) => value == "1",
        _ => false,
    }
}

fn measured_values(
    media: &Value,
    streams: &[&Map<String, Value>],
    selected_audio_index: Option<usize>,
) -> HashMap<String, Value> {
    let media_object = media.as_object().cloned().unwrap_or_default();
    let video = streams
        .iter()
        .find(|stream| stream_type(stream) == Some(1.0))
        .copied()
        .cloned()
        .unwrap_or_default();
    let part = media_object
        .get("Part")
        .and_then(Value::as_array)
        .and_then(|parts| parts.first())
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let optimized = part
        .get("optimizedForStreaming")
        .or_else(|| media_object.get("optimizedForStreaming"));
    let mut values = HashMap::from([
        (
            "video.width".to_string(),
            value_or(&video, "width", media_object.get("width")),
        ),
        (
            "video.height".to_string(),
            value_or(&video, "height", media_object.get("height")),
        ),
        (
            "video.bitDepth".to_string(),
            video.get("bitDepth").cloned().unwrap_or(Value::Null),
        ),
        (
            "video.level".to_string(),
            video.get("level").cloned().unwrap_or(Value::Null),
        ),
        (
            "video.profile".to_string(),
            video.get("profile").cloned().unwrap_or(Value::Null),
        ),
        (
            "video.frameRate".to_string(),
            video.get("frameRate").cloned().unwrap_or(Value::Null),
        ),
        (
            "video.bitrate".to_string(),
            video.get("bitrate").cloned().unwrap_or(Value::Null),
        ),
        (
            "video.refFrames".to_string(),
            video.get("refFrames").cloned().unwrap_or(Value::Null),
        ),
        (
            "video.anamorphic".to_string(),
            one_if_truthy(video.get("anamorphic")),
        ),
        (
            "video.hasScalingMatrix".to_string(),
            one_if_truthy(video.get("hasScalingMatrix")),
        ),
        (
            "video.scanType".to_string(),
            video.get("scanType").cloned().unwrap_or(Value::Null),
        ),
        ("video.separateFields".to_string(), Value::from(0)),
        (
            "video.DOVIProfile".to_string(),
            video.get("DOVIProfile").cloned().unwrap_or(Value::Null),
        ),
        (
            "part.optimizedForStreaming".to_string(),
            Value::from(if is_one(optimized) { 1 } else { 0 }),
        ),
        (
            "part.has64bitOffsets".to_string(),
            one_if_truthy(part.get("has64bitOffsets")),
        ),
    ]);
    if let Some(index) = selected_audio_index {
        let audio = streams[index];
        for (name, key) in [
            ("audio.channels", "channels"),
            ("audio.samplingRate", "samplingRate"),
            ("audio.bitrate", "bitrate"),
            ("audio.profile", "profile"),
            ("audio.bitDepth", "bitDepth"),
        ] {
            values.insert(
                name.to_string(),
                audio.get(key).cloned().unwrap_or(Value::Null),
            );
        }
    }
    values
}

fn limitation(limits: &[&Map<String, Value>], values: &HashMap<String, Value>) -> Option<String> {
    for limit in limits {
        if text(limit.get("onlyTranscodes")) == Some("true") {
            continue;
        }
        let name = text(limit.get("name"));
        let tag = text(limit.get("tag"));
        let Some(name) = name else {
            continue;
        };
        let Some(actual) = values.get(name).filter(|value| !value.is_null()) else {
            continue;
        };
        if matches!(tag, Some("UpperBound") | Some("LowerBound")) {
            let Some(have) = number(Some(actual)) else {
                continue;
            };
            let Some(bound) = number(limit.get("value")) else {
                continue;
            };
            if tag == Some("UpperBound") && have > bound {
                return Some(format!(
                    "{name} limitation applies: {} > {}",
                    shown(Some(actual)),
                    shown(limit.get("value"))
                ));
            }
            if tag == Some("LowerBound") && have < bound {
                return Some(format!(
                    "{name} limitation applies: {} < {}",
                    shown(Some(actual)),
                    shown(limit.get("value"))
                ));
            }
        } else if matches!(tag, Some("Match") | Some("NotMatch")) {
            let actual_text = python_string(Some(actual)).to_lowercase();
            let value = limit.get("value");
            let list = limit.get("list");
            let substring = limit.get("substring");
            let wanted = if truthy(value) {
                python_string(value)
            } else if truthy(list) {
                python_string(list)
            } else if truthy(substring) {
                python_string(substring)
            } else {
                "None".to_string()
            };
            let wanted = if value.is_none_or(Value::is_null) && list.is_some() {
                format!("{wanted} [list]")
            } else {
                wanted
            };
            let hit = if let Some(substring) = text(substring) {
                actual_text.contains(&substring.to_lowercase())
            } else if let Some(list) = text(list) {
                list.split('|')
                    .map(str::to_lowercase)
                    .any(|entry| entry == actual_text)
            } else {
                let expected = python_string(value).to_lowercase();
                actual_text == expected
                    || number(Some(actual))
                        .zip(expected.parse::<f64>().ok())
                        .is_some_and(|(actual, expected)| actual == expected)
            };
            if tag == Some("Match") && !hit {
                return Some(format!(
                    "{name} limitation applies: {} != {wanted}",
                    shown(Some(actual))
                ));
            }
            if tag == Some("NotMatch") && hit {
                return Some(format!(
                    "{name} limitation applies: {} == {wanted}",
                    shown(Some(actual))
                ));
            }
        }
    }
    None
}

/// The first codec-profile limitation `codec` breaks in `scope` (`VideoCodec`,
/// `VideoAudioCodec`), with the `onlyTranscodes` ones included: those are exactly the
/// rules for a stream a conversion copies rather than one the app plays directly.
/// The video bitrate a conversion is held to by the profile: an `upperBound` on `video.bitrate` for
/// any codec (`*`, which an extra's limitation arrives as an empty name) or H.264, which rex converts
/// to. Plezy brings its quality as one in its extra (`add-limitation(scope=videoCodec&scopeName=*&
/// type=upperBound&name=video.bitrate&value=2000)`) with no `maxVideoBitrate`, and a real 1.42.2
/// converts at 2000 (`tests/fixtures/decisions/plezy_quality_limit_s1422.json`).
pub fn video_bitrate_cap(profile: &Profile) -> Option<i64> {
    entries(profile, "codecprofiles")
        .into_iter()
        .filter(|entry| text(entry.get("tag")) == Some("VideoCodec"))
        .filter(|entry| {
            let codecs = names(entry.get("name"));
            codecs.is_empty() || codecs.iter().any(|name| *name == "*" || name.eq_ignore_ascii_case("h264"))
        })
        .flat_map(|entry| entries(entry, "limitations"))
        .filter(|limit| text(limit.get("name")) == Some("video.bitrate") && text(limit.get("tag")).is_some_and(|tag| tag.eq_ignore_ascii_case("UpperBound")))
        .filter_map(|limit| number(limit.get("value")))
        .filter(|value| *value >= 1.0)
        .map(|value| value as i64)
        .min()
}

pub fn codec_limitation(profile: &Profile, scope: &str, codec: &str, media: &Value) -> Option<String> {
    let stream_list = streams(media);
    let values = measured_values(media, &stream_list, selected_audio(&stream_list));
    let limits = entries(profile, "codecprofiles")
        .into_iter()
        .filter(|entry| text(entry.get("tag")) == Some(scope))
        .filter(|entry| names(entry.get("name")).iter().any(|name| *name == "*" || name.eq_ignore_ascii_case(codec)))
        .flat_map(|entry| entries(entry, "limitations"))
        .map(|limit| {
            let mut limit = limit.clone();
            limit.remove("onlyTranscodes");
            limit
        })
        .collect::<Vec<_>>();
    limitation(&limits.iter().collect::<Vec<_>>(), &values)
}

fn has_subtitle_target(profile: &Profile) -> bool {
    ["targets", "directplay"].iter().any(|key| {
        entries(profile, key)
            .iter()
            .any(|entry| text(entry.get("tag")) == Some("SubtitleProfile"))
    })
}

pub fn direct_play(profile: &Profile, media: &Value) -> (bool, String) {
    let stream_list = streams(media);
    let audio = audio_indices(&stream_list);
    let selected_audio_index = selected_audio(&stream_list);
    let first_audio_index = first_audio(&stream_list);
    let can_select = entries(profile, "settings").iter().any(|setting| {
        text(setting.get("name")) == Some("DirectPlayStreamSelection")
            && text(setting.get("value")) == Some("true")
    });
    let not_first = "App cannot direct play this item. Selected audio stream is not the first audio stream and app cannot select audio streams in direct play.";
    if selected_audio_index.is_some() && first_audio_index != selected_audio_index && !can_select {
        return (false, not_first.to_string());
    }

    let media_object = media.as_object();
    let container = media_object.and_then(|value| text(value.get("container")));
    let video_codec = media_object.and_then(|value| text(value.get("videoCodec")));
    let candidates = entries(profile, "directplay")
        .into_iter()
        .filter(|entry| {
            text(entry.get("tag")) == Some("VideoProfile")
                && !truthy(entry.get("protocol"))
                && names(entry.get("container")).contains(&container.unwrap_or(""))
                && (!truthy(entry.get("codec"))
                    || names(entry.get("codec")).contains(&video_codec.unwrap_or("")))
        })
        .collect::<Vec<_>>();
    if candidates.is_empty() {
        return (
            false,
            format!(
                "{NO_PROFILE}{}, and video codec {}.",
                python_string_text(container),
                python_string_text(video_codec)
            ),
        );
    }

    let subtitle_index = stream_list
        .iter()
        .position(|stream| stream_type(stream) == Some(3.0) && truthy(stream.get("selected")));
    if let Some(index) = subtitle_index {
        if truthy(stream_list[index].get("key")) && !has_subtitle_target(profile) {
            return (
                false,
                "App cannot direct play this item. Selected sidecar subtitle stream cannot be direct played."
                    .to_string(),
            );
        }
    }

    let skip_unselected = entries(profile, "settings").iter().any(|setting| {
        text(setting.get("name")) == Some("StreamUnselectedIncompatibleAudioStreams")
            && text(setting.get("value")) == Some("true")
    });
    let audio_codec = selected_audio_index.and_then(|index| text(stream_list[index].get("codec")));
    for index in &audio {
        if skip_unselected && Some(*index) != selected_audio_index {
            continue;
        }
        let codec = text(stream_list[*index].get("codec"));
        let compatible = candidates.iter().any(|entry| {
            !truthy(entry.get("audioCodec"))
                || codec.is_none()
                || names(entry.get("audioCodec")).contains(&codec.unwrap_or(""))
        });
        if !compatible {
            return (
                false,
                format!(
                    "{NO_PROFILE}{}, video codec {}, and audio codec {}.",
                    python_string_text(container),
                    python_string_text(video_codec),
                    python_string_text(codec)
                ),
            );
        }
    }
    let candidates = candidates
        .into_iter()
        .filter(|entry| {
            !truthy(entry.get("audioCodec"))
                || audio_codec.is_none()
                || names(entry.get("audioCodec")).contains(&audio_codec.unwrap_or(""))
        })
        .collect::<Vec<_>>();
    if candidates.is_empty() {
        return (
            false,
            format!(
                "{NO_PROFILE}{}, video codec {}, and audio codec {}.",
                python_string_text(container),
                python_string_text(video_codec),
                python_string_text(audio_codec)
            ),
        );
    }

    if let Some(index) = subtitle_index {
        let subtitle = stream_list[index];
        if !truthy(subtitle.get("key")) {
            let subtitle_codec = text(subtitle.get("codec"));
            let convertible = has_subtitle_target(profile)
                && subtitle_codec.is_none_or(|codec| !IMAGE_SUBTITLES.contains(&codec));
            let candidates = candidates
                .iter()
                .filter(|entry| {
                    convertible
                        || names(entry.get("subtitleCodec")).contains(&subtitle_codec.unwrap_or(""))
                        || names(entry.get("subtitleFormat"))
                            .contains(&subtitle_codec.unwrap_or(""))
                })
                .collect::<Vec<_>>();
            if candidates.is_empty() {
                return (
                    false,
                    format!(
                        "{NO_PROFILE}{}, using video codec {}, audio codec {}, and subtitle codec {}.",
                        python_string_text(container),
                        python_string_text(video_codec),
                        python_string_text(audio_codec),
                        python_string_text(subtitle_codec)
                    ),
                );
            }
        }
    }

    if let Some(index) = subtitle_index {
        let subtitle = stream_list[index];
        if !truthy(subtitle.get("key")) && !can_select {
            let embedded = stream_list
                .iter()
                .enumerate()
                .filter_map(|(index, stream)| {
                    (stream_type(stream) == Some(3.0) && !truthy(stream.get("key")))
                        .then_some(index)
                })
                .collect::<Vec<_>>();
            if !embedded.is_empty() && embedded[0] != index {
                return (
                    false,
                    "App cannot direct play this item. Selected embedded subtitle stream is not the first embedded subtitle stream."
                        .to_string(),
                );
            }
        }
    }

    let values = measured_values(media, &stream_list, selected_audio_index);
    let mut groups = Vec::new();
    for holder in entries(profile, "containerprofiles") {
        let holder_names = names(holder.get("name"));
        if text(holder.get("tag")) == Some("VideoContainer")
            && (holder_names.contains(&"*") || holder_names.contains(&container.unwrap_or("")))
        {
            groups.push((0_u8, holder));
        }
    }
    for codec in entries(profile, "codecprofiles") {
        let codec_names = names(codec.get("name"));
        let is_video = text(codec.get("tag")) == Some("VideoCodec")
            && (codec_names.contains(&"*") || codec_names.contains(&video_codec.unwrap_or("")));
        let is_audio = text(codec.get("tag")) == Some("VideoAudioCodec")
            && selected_audio_index.is_some()
            && (codec_names.contains(&"*") || codec_names.contains(&audio_codec.unwrap_or("")));
        if is_video || is_audio {
            groups.push((if codec_names.contains(&"*") { 2 } else { 1 }, codec));
        }
    }
    groups.sort_by_key(|(rank, _)| *rank);
    for (_, group) in groups {
        let limits = entries(group, "limitations");
        if let Some(reason) = limitation(&limits, &values) {
            return (
                false,
                format!("This app cannot play this item. The reason is: {reason}."),
            );
        }
    }
    (true, "Direct play OK.".to_string())
}

fn dolby_vision_profile(media: &Value) -> Option<&Value> {
    streams(media)
        .into_iter()
        .find(|stream| stream_type(stream) == Some(1.0))
        .and_then(|stream| truthy(stream.get("DOVIPresent")).then(|| stream.get("DOVIProfile")))
        .flatten()
}

/// True when a conversion target can serve the item, beyond declaring the right protocol: it
/// must also be able to produce what the source is.
///
/// An HLS source (Part container `hls`, a playlist rather than a file) is served by rex as
/// MPEG-TS segments. Only a target that streams HLS/DASH, or whose container is `mpegts`/`hls`,
/// can carry that. A target declaring only `container=mkv` (or mp4, ...) expects a single file
/// as output and cannot.
///
/// That is the cast case. The Plex Cast / Chromecast receiver sends an Extra whose only
/// transcode target is `protocol=http&container=mkv`. Matching on protocol alone planned an MKV
/// conversion of an HLS source and refused with 4005 "No conversion profile found for protocol
/// http". Such a target must not match an HLS source, so the profile falls through to the
/// default HLS target (see `effective_profile`).
///
/// A file source (mkv, mp4, ...) is matched on protocol alone, exactly as before.
fn target_serves_source(target: &Map<String, Value>, media: Option<&Value>) -> bool {
    if !is_hls_source(media) {
        return true;
    }
    matches!(text(target.get("protocol")), Some("hls" | "dash"))
        || matches!(text(target.get("container")), Some("mpegts" | "hls"))
}

/// The protocols a profile declares on its conversion targets, in declaration order
/// (the same targets `conversion` walks: `VideoProfile`, not `static`).
fn declared_protocols<'a>(profile: &'a Profile, media: Option<&Value>) -> Vec<&'a str> {
    entries(profile, "targets")
        .into_iter()
        .filter(|target| {
            text(target.get("tag")) == Some("VideoProfile")
                && text(target.get("context")) != Some("static")
                // an HLS source can only be offered a protocol whose target can carry it
                && target_serves_source(target, media)
        })
        .filter_map(|target| text(target.get("protocol")))
        .collect()
}

/// A client may send `protocol=*` (Plex for Android opening a live HLS channel from a library
/// row) or a list (`*,hls`, `hls,http`), where `conversion` compares the string against a
/// target's one declared protocol and so finds none: 4005 "No conversion profile found for
/// protocol *". Resolve such a value to one protocol: the first listed entry the profile
/// declares, else the profile's first declared protocol, else `hls` for an HLS-container
/// source, else `http`. A single concrete value (`http`, `hls`, `dash`, ...) is returned
/// untouched, so every client that works today decides exactly as before, with ONE exception:
///
/// Casting. When a Plex app casts to another Plex client the receiving device becomes the
/// client, and a TV asks for `protocol=http` (or sends none, which defaults to `http`) for an
/// HLS-container item (a playlist source, not a file). Generic, Android, tvOS, Samsung and
/// Universal TV declare only an HLS streaming target, so `conversion` finds no `http` target
/// and refuses every item ("No conversion profile found for protocol http"). For exactly that
/// case, `http` on an `hls` Part container resolves to `hls` when the profile does NOT convert
/// on `http` today but DOES convert on `hls`. Profiles that already convert on `http` (Roku,
/// Chromecast, Plex Desktop, an asf-only target's Refuse) are detected by asking `conversion`
/// itself, so "has an http target" can never drift from what `conversion` really does; they
/// keep exactly today's path. File containers (mkv, mp4, ...) are never touched.
pub fn effective_protocol<'a>(
    profile: Option<&'a Profile>,
    requested: &'a str,
    media: Option<&Value>,
) -> &'a str {
    if !requested.contains(['*', ',']) && !requested.contains(char::is_whitespace) {
        if requested == "http"
            && is_hls_source(media)
            && profile.is_some_and(|profile| {
                conversion(profile, "http", media) == Outcome::Neither
                    && conversion(profile, "hls", media) == Outcome::Convert
            })
        {
            return "hls";
        }
        return requested;
    }
    let declared = profile.map(|profile| declared_protocols(profile, media)).unwrap_or_default();
    if let Some(listed) = requested
        .split(',')
        .map(str::trim)
        .find(|entry| !entry.is_empty() && *entry != "*" && declared.contains(entry))
    {
        return listed;
    }
    if let Some(first) = declared.first() {
        return first;
    }
    if is_hls_source(media) {
        "hls"
    } else {
        "http"
    }
}

/// The standard HLS streaming target given to a profile that declares none (see
/// `effective_profile`). The single source of truth for that default.
pub const DEFAULT_HLS_EXTRA: &str = "add-transcode-target(type=videoProfile&context=streaming&protocol=hls&container=mpegts&videoCodec=h264&audioCodec=aac)";

/// True when some streaming target of the profile can serve an HLS source (see
/// `target_serves_source`): the capability test, not "declares any protocol".
fn serves_hls(profile: &Profile) -> bool {
    let hls_source = serde_json::json!({"Part": [{"container": "hls"}]});
    entries(profile, "targets").into_iter().any(|target| {
        text(target.get("tag")) == Some("VideoProfile")
            && text(target.get("context")) != Some("static")
            && target_serves_source(target, Some(&hls_source))
    })
}

/// The profile a decision and its transcode plan both use: the profile with the client's
/// `X-Plex-Client-Profile-Extra` applied. When the item is an HLS source and, once the Extra is
/// applied, no streaming target of the profile can serve an HLS source, `DEFAULT_HLS_EXTRA`
/// stands in so the profile can convert. That holds whether or not the client sent an Extra:
/// Generic has no targets, Universal TV only static ones, and the Plex Cast / Chromecast
/// receiver sends an Extra whose only target is `protocol=http&container=mkv`. A client cast to
/// would otherwise refuse every HLS item (4005, protocol http). A target that does serve HLS,
/// and any file-backed item, leave the profile exactly as the Extra made it.
pub fn effective_profile(profile: &Profile, extra: &str, media: Option<&Value>) -> Profile {
    let applied = if extra.is_empty() {
        profile.clone()
    } else {
        with_extra(profile, extra)
    };
    if is_hls_source(media) && !serves_hls(&applied) {
        return with_extra(&applied, DEFAULT_HLS_EXTRA);
    }
    applied
}

/// True when the item's first Part is an HLS playlist rather than a file.
pub fn is_hls_source(media: Option<&Value>) -> bool {
    media
        .and_then(|media| media.get("Part"))
        .and_then(Value::as_array)
        .and_then(|parts| parts.first())
        .and_then(|part| text(part.get("container")))
        == Some("hls")
}

pub fn conversion(profile: &Profile, protocol: &str, media: Option<&Value>) -> Outcome {
    let source_codec = if media
        .and_then(dolby_vision_profile)
        .and_then(|profile| number(Some(profile)))
        == Some(5.0)
    {
        Some(
            streams(media.expect("DoVi profile came from media"))
                .into_iter()
                .find(|stream| stream_type(stream) == Some(1.0))
                .and_then(|stream| stream.get("codec").filter(|value| truthy(Some(value))))
                .map(|value| python_string(Some(value)))
                .unwrap_or_default()
                .to_lowercase(),
        )
    } else {
        None
    };
    for target in entries(profile, "targets") {
        if text(target.get("tag")) != Some("VideoProfile")
            || text(target.get("context")) == Some("static")
        {
            continue;
        }
        // Protocol alone is not enough: an HLS source needs a target that can carry HLS (the
        // Chromecast's mkv-over-http target cannot). See `target_serves_source`.
        if !target_serves_source(target, media) {
            continue;
        }
        let target_codec_text = target
            .get("videoCodec")
            .filter(|value| truthy(Some(value)))
            .map(|value| python_string(Some(value)))
            .unwrap_or_default();
        let target_codecs = target_codec_text
            .split(',')
            .map(str::trim)
            .filter(|piece| !piece.is_empty())
            .map(str::to_lowercase)
            .collect::<Vec<_>>();
        let blocked = source_codec
            .as_ref()
            .is_some_and(|source| !target_codecs.iter().any(|codec| codec == source));
        let declared = target.get("protocol").filter(|value| !value.is_null());
        if text(declared) == Some(protocol) {
            if !blocked {
                return Outcome::Convert;
            }
            continue;
        }
        if declared.is_none() && protocol == "http" {
            if text(target.get("container")) == Some("asf") {
                // Xbox 360's asf-only target. Its 400 is the audio's: a Media with no audio
                // stream (a raw .avc, .m2v, .fli or .vp3 on plexscratch42, 2026-11-04) is
                // 3000/2000/4007, "Implementation for video encoder 'wmv2' not found."
                if media.is_some_and(|media| !streams(media).iter().any(|stream| stream_type(stream) == Some(2.0))) {
                    return Outcome::Neither;
                }
                return Outcome::Refuse;
            }
            if !blocked {
                return Outcome::Convert;
            }
        }
    }
    Outcome::Neither
}

pub fn dolby_vision_blocked(
    profiles: &Profiles,
    name: &str,
    media: Option<&Value>,
    protocol: &str,
) -> bool {
    let Some(profile) = resolve(profiles, name) else {
        return false;
    };
    let Some(media) = media else {
        return false;
    };
    if dolby_vision_profile(media).and_then(|profile| number(Some(profile))) != Some(5.0) {
        return false;
    }
    conversion(profile, protocol, None) == Outcome::Convert
        && conversion(profile, protocol, Some(media)) != Outcome::Convert
}

pub fn resolve<'a>(profiles: &'a Profiles, name: &str) -> Option<&'a Profile> {
    let mut profile = profiles.get(name);
    let mut seen = HashSet::new();
    while let Some(current) = profile {
        let Some(redirect_value) = current.get("redirect") else {
            break;
        };
        if !truthy(Some(redirect_value)) {
            break;
        }
        let Some(redirect) = text(Some(redirect_value)) else {
            break;
        };
        if !seen.insert(redirect.to_string()) {
            break;
        }
        profile = profiles.get(redirect);
    }
    profile
}

/// The profile with what `X-Plex-Client-Profile-Extra` adds to it: `add-direct-play-profile`
/// and `add-limitation`. Plex for Android adds `type=notMatch&name=video.DOVIProfile&value=5`
/// for HEVC on a device without Dolby Vision profile 5, and a real 1.42.2 then refuses that
/// direct play ("video.DOVIProfile limitation applies: 5 == 5") while a non-DV HEVC file still
/// plays (`tests/fixtures/decisions/dovi_profile5_limitation_s1422.json`). Plex for iOS names
/// the empty `Generic` profile and brings its whole HLS target in an `add-transcode-target`,
/// which a real 1.42.2 converts to (`tests/fixtures/decisions/generic_transcode_target_s1422.json`).
// ponytail: the other directives (settings) are not applied to the decision.
pub fn with_extra(profile: &Profile, extra: &str) -> Profile {
    let mut profile = profile.clone();
    let capital = |word: &str| {
        let mut chars = word.chars();
        chars.next().map(|first| first.to_ascii_uppercase().to_string() + chars.as_str()).unwrap_or_default()
    };
    // Directives are joined by `+`. A client that sends the Extra as a query parameter has that
    // `+` form-decoded to a space on the way in (the Plex Cast / Chromecast receiver does), so
    // either separates them. A directive body never contains a space: its values are `&`-joined
    // and percent-encoded.
    for directive in extra.split(|c: char| c == '+' || c.is_whitespace()).filter(|part| !part.is_empty()) {
        let Some((kind, rest)) = directive.split_once('(') else {
            continue;
        };
        // Plezy sends `audioCodec=aac%2Cac3%2Ceac3%2Cmp3`, which a real 1.42.2 reads as the list
        // Plex's apps send with plain commas (`tests/fixtures/decisions/plezy_codec_lists_s1422.json`).
        let pairs: HashMap<&str, String> = rest
            .trim_end_matches(')')
            .split('&')
            .filter_map(|pair| pair.split_once('='))
            .map(|(key, value)| (key, crate::runtime::percent_decode(value)))
            .collect();
        let named = |key: &str| pairs.get(key).map(String::as_str).unwrap_or("");
        // The profile resource says "any" with an empty field; a client's extra says `*`.
        let field = |key: &str| Value::from(Some(named(key)).filter(|value| *value != "*").unwrap_or(""));
        let (list, entry) = match kind.trim() {
            "add-direct-play-profile" if named("type") == "videoProfile" => (
                "directplay",
                serde_json::json!({"tag": "VideoProfile", "container": field("container"),
                    "codec": field("videoCodec"), "audioCodec": field("audioCodec")}),
            ),
            "add-transcode-target" if named("type") == "videoProfile" => (
                "targets",
                serde_json::json!({"tag": "VideoProfile", "context": field("context"), "protocol": field("protocol"),
                    "container": field("container"), "codec": field("videoCodec"), "audioCodec": field("audioCodec"),
                    "subtitleCodec": field("subtitleCodec")}),
            ),
            "add-limitation" => (
                "codecprofiles",
                serde_json::json!({"tag": capital(named("scope")),
                    "name": field("scopeName"), "limitations": [{"name": field("name"),
                    "tag": capital(named("type")), "value": field("value")}]}),
            ),
            _ => continue,
        };
        if let Some(Value::Array(entries)) = profile.get_mut(list) {
            entries.push(entry);
        } else {
            profile.insert(list.to_string(), Value::Array(vec![entry]));
        }
    }
    profile
}

pub fn decide(
    profiles: &Profiles,
    name: &str,
    media: &Value,
    protocol: &str,
    direct: bool,
) -> Decision {
    let Some(profile) = resolve(profiles, name) else {
        return Decision {
            outcome: Outcome::Refuse,
            text: None,
        };
    };
    decide_profile(profile, media, protocol, direct)
}

pub fn decide_profile(profile: &Profile, media: &Value, protocol: &str, direct: bool) -> Decision {
    let mut text = "App cannot direct play this item. Direct play is disabled.".to_string();
    if direct {
        let (ok, reason) = direct_play(profile, media);
        text = reason;
        if ok {
            return Decision {
                outcome: Outcome::DirectPlay,
                text: Some(text),
            };
        }
    }
    Decision {
        outcome: conversion(
            profile,
            if protocol.is_empty() {
                "http"
            } else {
                protocol
            },
            Some(media),
        ),
        text: Some(text),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    const PROFILES: &str = include_str!("../../data/client_profiles.json");
    const ROWS: &str = include_str!("../../tests/fixtures/decisions/rows.json");
    const CELLS: &str = include_str!("../../tests/fixtures/decisions/cells.jsonl");

    fn integer(value: Option<&Value>) -> Option<i64> {
        value.and_then(Value::as_i64)
    }

    fn expected_outcome(cell: &Map<String, Value>) -> Outcome {
        if integer(cell.get("status")) != Some(200) {
            return Outcome::Refuse;
        }
        if integer(cell.get("directPlayDecisionCode")) == Some(1000) {
            return Outcome::DirectPlay;
        }
        if integer(cell.get("generalDecisionCode")) == Some(1001)
            || integer(cell.get("transcodeDecisionCode")) == Some(4002)
        {
            return Outcome::Convert;
        }
        if integer(cell.get("generalDecisionCode")) == Some(2000) {
            return Outcome::Neither;
        }
        panic!("unrecognised measured decision cell: {cell:?}");
    }

    #[test]
    fn measured_corpus_matches_outcomes_and_status_200_texts() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        let rows: Value = serde_json::from_str(ROWS).expect("row fixture parses");
        let row_map = rows["rows"].as_object().expect("rows object");
        let texts = rows["texts"].as_array().expect("texts array");
        let mut measured = 0;
        let mut outcome_matches = 0;
        let mut exact_matches = 0;
        let mut recorded = 0;
        let mut recorded_matches = 0;
        for line in CELLS.lines().filter(|line| !line.trim().is_empty()) {
            let cell: Value = serde_json::from_str(line).expect("decision cell parses");
            let cell = cell.as_object().expect("decision cell object");
            let row = row_map
                .get(cell["row"].as_str().expect("row name"))
                .expect("row exists");
            let media = row.get("Media").expect("row media");
            let answer = decide(
                &profiles,
                cell["profile"].as_str().expect("profile name"),
                media,
                "http",
                true,
            );
            let wanted = expected_outcome(cell);
            let exact = answer.outcome == wanted
                && (wanted == Outcome::Refuse
                    || answer.text.as_deref()
                        == cell
                            .get("text")
                            .and_then(Value::as_u64)
                            .and_then(|index| texts.get(index as usize))
                            .and_then(Value::as_str));
            if cell.get("state").and_then(Value::as_str) == Some("pre-selection") {
                recorded += 1;
                recorded_matches += usize::from(exact);
            } else {
                measured += 1;
                outcome_matches += usize::from(answer.outcome == wanted);
                exact_matches += usize::from(exact);
                assert!(exact, "decision mismatch for {cell:?}: got {answer:?}");
            }
        }
        assert_eq!(profiles.len(), 78);
        assert_eq!(row_map.len(), 57);
        assert_eq!(measured, 4368);
        assert_eq!(outcome_matches, measured);
        assert_eq!(exact_matches, measured);
        assert_eq!(recorded, 156);
        assert_eq!(recorded_matches, 67);
    }

    #[test]
    fn redirects_and_missing_profiles_follow_reference_resolution() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        assert_eq!(resolve(&profiles, "Chrome"), resolve(&profiles, "Web"));
        assert!(resolve(&profiles, "Raspberry PI").is_none());
    }

    #[test]
    fn numeric_helpers_match_python_json_semantics() {
        assert_eq!(number(Some(&Value::String(" 2.0 ".to_string()))), Some(2.0));

        let numeric = serde_json::json!({
            "Part": [{"Stream": [{"streamType": 2, "selected": true}]}]
        });
        let textual = serde_json::json!({
            "Part": [{"Stream": [{"streamType": "2", "selected": true}]}]
        });
        assert_eq!(
            evaluated_audio(&numeric).and_then(|stream| stream.get("streamType")),
            Some(&Value::from(2))
        );
        assert!(evaluated_audio(&textual).is_none());
    }

    const DOVI: &str = include_str!("../../tests/fixtures/decisions/dovi_profile5_limitation_s1422.json");

    #[test]
    fn a_client_extra_limitation_refuses_dolby_vision_profile_5_as_a_real_pms_does() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        let fixture: Value = serde_json::from_str(DOVI).expect("dovi fixture parses");
        let samsung = resolve(&profiles, "Samsung").expect("Samsung profile");
        let mut checked = 0;
        for row in fixture["decisions"].as_array().expect("decisions") {
            if !row["label"].as_str().unwrap_or("").contains("-samsung-") {
                continue;
            }
            let media = &fixture["items"][row["item"].as_str().expect("item")]["Media"][0];
            let extra = row["req_headers"]["X-Plex-Client-Profile-Extra"].as_str().unwrap_or("");
            let answer = decide_profile(&with_extra(samsung, extra), media, "hls", true);
            let reference = &row["response"];
            let direct = reference["directPlayDecisionCode"].as_i64() == Some(1000);
            assert_eq!(answer.outcome == Outcome::DirectPlay, direct, "{}", row["label"]);
            assert_eq!(answer.text.as_deref(), reference["directPlayDecisionText"].as_str(), "{}", row["label"]);
            if !direct {
                assert_eq!(reference["transcodeDecisionCode"].as_i64(), Some(2003), "{}", row["label"]);
                assert!(dolby_vision_blocked(&profiles, "Samsung", Some(media), "hls"), "{}", row["label"]);
            }
            checked += 1;
        }
        assert_eq!(checked, 6);
    }

    const GENERIC: &str = include_str!("../../tests/fixtures/decisions/generic_transcode_target_s1422.json");

    #[test]
    fn a_client_extra_transcode_target_lets_the_generic_profile_convert_as_a_real_pms_does() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        let fixture: Value = serde_json::from_str(GENERIC).expect("generic fixture parses");
        let media = &fixture["items"]["1"]["Media"][0];
        let rows = fixture["decisions"].as_array().expect("decisions");
        for row in rows {
            let name = row["req_headers"]["X-Plex-Client-Profile-Name"].as_str().expect("profile name");
            let extra = row["req_headers"]["X-Plex-Client-Profile-Extra"].as_str().unwrap_or("");
            let profile = with_extra(resolve(&profiles, name).expect("profile"), extra);
            let answer = decide_profile(&profile, media, "hls", false);
            let converts = row["response"]["generalDecisionCode"].as_i64() == Some(1001);
            assert_eq!(answer.outcome == Outcome::Convert, converts, "{}", row["label"]);
        }
        assert_eq!(rows.len(), 4);
    }

    const PLEZY: &str = include_str!("../../tests/fixtures/decisions/plezy_codec_lists_s1422.json");

    #[test]
    fn a_client_extra_codec_list_joined_with_an_encoded_comma_is_the_same_list_as_a_real_pms_reads_it() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        let fixture: Value = serde_json::from_str(PLEZY).expect("plezy fixture parses");
        let media = &fixture["items"]["1"]["Media"][0];
        let rows = fixture["decisions"].as_array().expect("decisions");
        for row in rows {
            let label = row["label"].as_str().expect("label");
            let asked = |name: &str| {
                row["query"].as_array().expect("query").iter().find(|pair| pair[0] == name).and_then(|pair| pair[1].as_str()).expect(name)
            };
            let profile = with_extra(resolve(&profiles, asked("X-Plex-Client-Profile-Name")).expect("profile"), asked("X-Plex-Client-Profile-Extra"));
            let converts = row["response"]["generalDecisionCode"].as_i64() == Some(1001);
            assert_eq!(decide_profile(&profile, media, "hls", false).outcome == Outcome::Convert, converts, "{label}");
            let target = profile["targets"].as_array().expect("targets").iter().find(|target| target["protocol"] == "hls").expect("hls target");
            assert_eq!(target["audioCodec"], "aac,ac3,eac3,mp3", "{label}");
            let video = if target["container"] == "mp4" { "hevc,h264" } else { "h264" };
            assert_eq!(target["codec"], video, "{label}");
        }
        assert_eq!(rows.len(), 6);
    }

    /// `protocol=*` and lists must resolve to a protocol the profile converts to; a single
    /// concrete value must come back untouched (Plex for Android's live HLS channel, 4005).
    #[test]
    fn an_unrecognised_protocol_falls_back_and_a_concrete_one_is_untouched() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        let android = resolve(&profiles, "Android").expect("Android profile");
        let hls_media = serde_json::json!({"Part": [{"container": "hls"}]});
        let file_media = serde_json::json!({"Part": [{"container": "mkv"}]});
        // single recognised values, whatever the profile or media: exactly as before. The one
        // deliberate exception is `http` on an HLS item for a profile with no http target (a cast
        // to a TV): Android declares only hls, so that resolves to hls; see
        // `cast_http_on_an_hls_item_resolves_to_hls_when_the_profile_has_no_http_target`.
        for value in ["http", "hls", "dash", "mp4"] {
            let expected = if value == "http" { "hls" } else { value };
            assert_eq!(effective_protocol(Some(android), value, Some(&hls_media)), expected);
            assert_eq!(effective_protocol(Some(android), value, Some(&file_media)), value);
            assert_eq!(effective_protocol(None, value, None), value);
            assert_eq!(effective_protocol(None, value, Some(&hls_media)), value);
        }
        // `*` and lists: the profile's own declared protocol.
        for value in ["*", "*,*", " * ", "*,hls", "hls,http", "http,hls", "dash,*"] {
            assert_eq!(effective_protocol(Some(android), value, Some(&file_media)), "hls", "{value}");
        }
        // no profile: hls for an hls source, else the old default.
        assert_eq!(effective_protocol(None, "*", Some(&hls_media)), "hls");
        assert_eq!(effective_protocol(None, "*", Some(&file_media)), "http");
        assert_eq!(effective_protocol(None, "*", None), "http");
        // and conversion now succeeds for `*` where it refused before.
        let wide = effective_protocol(Some(android), "*", Some(&hls_media));
        assert_eq!(conversion(android, "*", Some(&hls_media)), Outcome::Neither);
        assert_eq!(conversion(android, wide, Some(&hls_media)), Outcome::Convert);
    }

    /// Review (PR 21, CodeRabbit): the protocol must come from the profile the decision is made
    /// with, i.e. WITH the client's `X-Plex-Client-Profile-Extra` applied. The empty Generic
    /// profile declares no protocol, so a file-backed item with `protocol=*` used to resolve to
    /// `http` and then match no target of the extra's HLS transcode.
    #[test]
    fn protocol_star_on_a_file_item_uses_the_extra_applied_profile() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        let generic = resolve(&profiles, "Generic").expect("Generic profile");
        let extra = "add-transcode-target(type=videoProfile&context=streaming&protocol=hls&container=mpegts&videoCodec=h264&audioCodec=aac)";
        for container in ["mkv", "mp4"] {
            let file_media = serde_json::json!({"Part": [{"container": container}]});
            // before the fix: base profile -> http -> nothing to convert to.
            let base = effective_protocol(Some(generic), "*", Some(&file_media));
            assert_eq!(base, "http", "{container}");
            assert_eq!(decide_profile(&with_extra(generic, extra), &file_media, base, false).outcome, Outcome::Neither, "{container}");
            // after: one extra-applied profile picks the protocol and decides.
            let profile = with_extra(generic, extra);
            let protocol = effective_protocol(Some(&profile), "*", Some(&file_media));
            assert_eq!(protocol, "hls", "{container}");
            assert_eq!(decide_profile(&profile, &file_media, protocol, false).outcome, Outcome::Convert, "{container}");
            // a single concrete protocol is untouched, extra or not.
            assert_eq!(effective_protocol(Some(&profile), "http", Some(&file_media)), "http", "{container}");
            assert_eq!(effective_protocol(Some(&profile), "hls", Some(&file_media)), "hls", "{container}");
        }
    }

    /// Cast playback: a receiving TV asks for `protocol=http` on an HLS-container item under a
    /// profile that declares only an HLS target. That used to refuse ("No conversion profile
    /// found for protocol http"); it must now resolve to `hls` and convert. Profiles that
    /// already convert on `http`, and file containers, must be left exactly as they were.
    #[test]
    fn cast_http_on_an_hls_item_resolves_to_hls_when_the_profile_has_no_http_target() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        let hls_media = serde_json::json!({"Part": [{"container": "hls"}]});
        let profile_of = |value: Value| -> Profile { value.as_object().cloned().expect("profile object") };
        let hls_only = profile_of(serde_json::json!({"targets": [
            {"tag": "VideoProfile", "context": "streaming", "protocol": "hls", "container": "mpegts", "videoCodec": "h264", "audioCodec": "aac"}
        ]}));
        // (a) an hls-only streaming profile: refused on http before, converts on hls now.
        assert_eq!(conversion(&hls_only, "http", Some(&hls_media)), Outcome::Neither);
        let protocol = effective_protocol(Some(&hls_only), "http", Some(&hls_media));
        assert_eq!(protocol, "hls");
        assert_eq!(conversion(&hls_only, protocol, Some(&hls_media)), Outcome::Convert);
        // the extra-applied Generic profile (an hls transcode target) behaves the same way.
        let generic = resolve(&profiles, "Generic").expect("Generic profile");
        let extra = "add-transcode-target(type=videoProfile&context=streaming&protocol=hls&container=mpegts&videoCodec=h264&audioCodec=aac)";
        let with_hls = with_extra(generic, extra);
        let protocol = effective_protocol(Some(&with_hls), "http", Some(&hls_media));
        assert_eq!(protocol, "hls");
        assert_eq!(decide_profile(&with_hls, &hls_media, protocol, false).outcome, Outcome::Convert);
        // (b) a Roku-like profile that declares an mkv/http target AND an hls target: on a FILE
        // item http keeps converting on its own target; on an HLS item the mkv target cannot
        // carry the playlist, so http resolves to the hls target it also declares.
        let roku_like = profile_of(serde_json::json!({"targets": [
            {"tag": "VideoProfile", "context": "streaming", "protocol": "http", "container": "mkv", "videoCodec": "h264", "audioCodec": "aac"},
            {"tag": "VideoProfile", "context": "streaming", "protocol": "hls", "container": "mpegts", "videoCodec": "h264", "audioCodec": "aac"}
        ]}));
        let mkv_file = serde_json::json!({"Part": [{"container": "mkv"}]});
        assert_eq!(effective_protocol(Some(&roku_like), "http", Some(&mkv_file)), "http");
        assert_eq!(conversion(&roku_like, "http", Some(&mkv_file)), Outcome::Convert);
        assert_eq!(conversion(&roku_like, "http", Some(&hls_media)), Outcome::Neither);
        assert_eq!(effective_protocol(Some(&roku_like), "http", Some(&hls_media)), "hls");
        // a target with no protocol at all also answers http in `conversion` for a file item;
        // for an HLS item its mkv container cannot carry the playlist (same rule).
        let undeclared = profile_of(serde_json::json!({"targets": [
            {"tag": "VideoProfile", "context": "streaming", "container": "mkv", "videoCodec": "h264", "audioCodec": "aac"}
        ]}));
        assert_eq!(effective_protocol(Some(&undeclared), "http", Some(&hls_media)), "http");
        // the shipped profiles whose only streaming target is a file container over http
        // (mkv) cannot carry an HLS playlist: conversion no longer matches them on protocol
        // alone, so on an HLS item they resolve to hls like the five that refused. On a FILE
        // item they convert on http exactly as before.
        for name in ["Chromecast", "Plex Desktop"] {
            let profile = resolve(&profiles, name).unwrap_or_else(|| panic!("{name} profile"));
            assert_eq!(conversion(profile, "http", Some(&hls_media)), Outcome::Neither, "{name}");
            let effective = effective_profile(profile, "", Some(&hls_media));
            assert_eq!(effective_protocol(Some(&effective), "http", Some(&hls_media)), "hls", "{name}");
            assert_eq!(conversion(&effective, "hls", Some(&hls_media)), Outcome::Convert, "{name}");
            let mkv = serde_json::json!({"Part": [{"container": "mkv"}]});
            assert_eq!(conversion(profile, "http", Some(&mkv)), Outcome::Convert, "{name}");
            assert_eq!(effective_profile(profile, "", Some(&mkv)), *profile, "{name}");
        }
        for name in ["Generic", "Android", "tvOS", "Samsung", "Universal TV"] {
            let profile = resolve(&profiles, name).unwrap_or_else(|| panic!("{name} profile"));
            let profile = with_extra(profile, extra);
            let protocol = effective_protocol(Some(&profile), "http", Some(&hls_media));
            assert_eq!(protocol, "hls", "{name}");
            assert_eq!(conversion(&profile, protocol, Some(&hls_media)), Outcome::Convert, "{name}");
        }
        // (c) a non-HLS item (mkv) asking for http is unchanged, whatever the profile.
        let mkv_media = serde_json::json!({"Part": [{"container": "mkv"}]});
        for profile in [&hls_only, &roku_like, &undeclared, &with_hls] {
            assert_eq!(effective_protocol(Some(profile), "http", Some(&mkv_media)), "http");
        }
        assert_eq!(effective_protocol(Some(&hls_only), "http", None), "http");
        assert_eq!(effective_protocol(None, "http", Some(&hls_media)), "http");
        // (d) other single concrete values stay untouched on an HLS item.
        for value in ["hls", "dash", "mp4"] {
            assert_eq!(effective_protocol(Some(&hls_only), value, Some(&hls_media)), value);
        }
    }

    /// Generic (no targets) and Universal TV (static targets only) declare no streaming target.
    /// With no Extra on an HLS item they get the default hls target and convert; everything
    /// else is unchanged.
    #[test]
    fn a_profile_with_no_streaming_target_gets_a_default_hls_target_for_hls_items() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        let hls_media = serde_json::json!({"Part": [{"container": "hls"}]});
        let mkv_media = serde_json::json!({"Part": [{"container": "mkv"}]});
        let profile_of = |value: Value| -> Profile { value.as_object().cloned().expect("profile object") };
        let universal_like = profile_of(serde_json::json!({"targets": [
            {"tag": "VideoProfile", "context": "static", "protocol": "http", "container": "mp4", "videoCodec": "h264", "audioCodec": "aac"},
            {"tag": "SubtitleProfile", "context": "streaming", "protocol": "http", "container": "srt"}
        ]}));
        let mut declaring_none = vec![universal_like];
        for name in ["Generic", "Universal TV"] {
            declaring_none.push(resolve(&profiles, name).unwrap_or_else(|| panic!("{name} profile")).clone());
        }
        for raw in &declaring_none {
            assert!(declared_protocols(raw, None).is_empty());
            let profile = effective_profile(raw, "", Some(&hls_media));
            for requested in ["http", "hls"] {
                let protocol = effective_protocol(Some(&profile), requested, Some(&hls_media));
                assert_eq!(protocol, "hls");
                assert_eq!(decide_profile(&profile, &hls_media, protocol, false).outcome, Outcome::Convert);
            }
            // a file-backed item gets no default: unchanged.
            assert_eq!(effective_profile(raw, "", Some(&mkv_media)), *raw);
            assert_eq!(effective_profile(raw, "", None), *raw);
            assert_eq!(effective_protocol(Some(raw), "http", Some(&mkv_media)), "http");
            // the client's own Extra wins; the default is not added on top.
            let own = effective_profile(raw, DEFAULT_HLS_EXTRA, Some(&hls_media));
            assert_eq!(own, with_extra(raw, DEFAULT_HLS_EXTRA));
            let other = "add-transcode-target(type=videoProfile&context=streaming&protocol=hls&container=mpegts&videoCodec=hevc&audioCodec=aac)";
            assert_eq!(effective_profile(raw, other, Some(&hls_media)), with_extra(raw, other));
        }
        // profiles whose streaming target already serves hls are unchanged without an Extra.
        for name in ["Android", "tvOS", "Samsung"] {
            let raw = resolve(&profiles, name).unwrap_or_else(|| panic!("{name} profile"));
            assert!(serves_hls(raw), "{name}");
            assert_eq!(effective_profile(raw, "", Some(&hls_media)), *raw, "{name}");
        }
        // profiles whose only streaming target is a file container over http get the default
        // hls target on an HLS item (and nothing on a file item).
        for name in ["Chromecast", "Plex Desktop"] {
            let raw = resolve(&profiles, name).unwrap_or_else(|| panic!("{name} profile"));
            assert!(!serves_hls(raw), "{name}");
            assert_eq!(effective_profile(raw, "", Some(&hls_media)), with_extra(raw, DEFAULT_HLS_EXTRA), "{name}");
            assert_eq!(effective_profile(raw, "", Some(&mkv_media)), *raw, "{name}");
        }
    }

    /// What the Plex Cast / Chromecast receiver (product "Plex Cast", platform "Chromecast",
    /// device "Android", version "5.0.0") really sends as `X-Plex-Client-Profile-Extra`, captured
    /// from a refused cast. Its full length was 2759 characters; the capture kept the first 1500,
    /// which holds every target (one mkv-over-http transcode target, no hls target) and the
    /// start of the limitations.
    const CHROMECAST_EXTRA: &str = "add-direct-play-profile(type=videoProfile&context=all&protocol=http&container=mkv&videoCodec=hevc,h264&audioCodec=aac,vorbis,opus,ac3,eac3,mp3,flac) add-transcode-target(type=videoProfile&context=all&protocol=http&container=mkv&videoCodec=hevc,h264&audioCodec=aac,vorbis,opus,ac3,eac3,mp3,flac) add-direct-play-profile(type=subtitleProfile&context=all&protocol=http&subtitleContainer=webvtt&subtitleCodec=webvtt) add-transcode-target(type=subtitleProfile&context=all&protocol=http&container=webvtt&subtitleCodec=webvtt) add-limitation(scope=videoCodec&scopeName=h264&scopeType=videoCodec&context=all&protocol=http&type=upperBound&name=video.level&value=42) add-limitation(scope=videoCodec&scopeName=h264&scopeType=videoCodec&context=all&protocol=http&type=upperBound&name=video.width&value=4096&replace=true) add-limitation(scope=videoCodec&scopeName=h264&scopeType=videoCodec&context=all&protocol=http&type=upperBound&name=video.height&value=2160&replace=true) add-limitation(scope=videoCodec&scopeName=hevc&scopeType=videoCodec&context=all&protocol=http&type=upperBound&name=video.level&value=153) add-limitation(scope=videoCodec&scopeName=hevc&scopeType=videoCodec&context=all&protocol=http&type=upperBound&name=video.bitDepth&value=8&replace=true) add-limitation(scope=videoCodec&scopeName=hevc&scopeType=videoCodec&context=all&protocol=http&type=upperBound&name=video.width&value=4096&replace=true) add-limitation(scope=videoCodec&scopeName=hevc&scopeType=videoCodec&context=all&protocol=http&ty";

    /// The Chromecast Extra declares `protocol=http&container=mkv` and no hls target. Matching on
    /// protocol alone planned an MKV conversion of an HLS source (4005, protocol http). The
    /// profile must now fall through to the default hls target for an HLS item and convert there.
    #[test]
    fn chromecast_extra_with_only_an_mkv_http_target_converts_an_hls_item_through_the_default_hls_target() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        let hls_media = serde_json::json!({"Part": [{"container": "hls"}]});
        // the fixture really is the shape described: an http/mkv target, nothing that serves hls.
        let raw = resolve(&profiles, "Generic").expect("Generic profile");
        let applied = with_extra(raw, CHROMECAST_EXTRA);
        assert_eq!(declared_protocols(&applied, None), vec!["http"]);
        assert!(declared_protocols(&applied, Some(&hls_media)).is_empty());
        assert!(!serves_hls(&applied));
        // the old protocol-only match: http "converts" on the applied profile for a FILE item ...
        let mkv_media = serde_json::json!({"Part": [{"container": "mkv"}]});
        assert_eq!(conversion(&applied, "http", Some(&mkv_media)), Outcome::Convert);
        // ... but it must not for an HLS item: the mkv target cannot serve it.
        assert_eq!(conversion(&applied, "http", Some(&hls_media)), Outcome::Neither);
        // every profile name the cast may arrive under resolves through the default target.
        for name in ["Generic", "Android", "Chromecast", "Roku"] {
            let raw = resolve(&profiles, name).unwrap_or_else(|| panic!("{name} profile"));
            let effective = effective_profile(raw, CHROMECAST_EXTRA, Some(&hls_media));
            assert!(serves_hls(&effective), "{name}");
            let protocol = effective_protocol(Some(&effective), "http", Some(&hls_media));
            assert_eq!(protocol, "hls", "{name}");
            assert_eq!(conversion(&effective, protocol, Some(&hls_media)), Outcome::Convert, "{name}");
            assert_eq!(decide_profile(&effective, &hls_media, protocol, true).outcome, Outcome::Convert, "{name}");
            // a list / wildcard request resolves to the same protocol.
            for requested in ["*", "http,hls", "*,hls"] {
                assert_eq!(effective_protocol(Some(&effective), requested, Some(&hls_media)), "hls", "{name} {requested}");
            }
        }
    }

    /// The same Extra on a file-backed item is exactly what it was: no hls target is added, and
    /// it converts on its own mkv/http target.
    #[test]
    fn chromecast_extra_on_a_file_item_is_unchanged() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        for container in ["mkv", "mp4", "avi"] {
            let media = serde_json::json!({"Part": [{"container": container}]});
            for name in ["Generic", "Android", "Chromecast", "Roku"] {
                let raw = resolve(&profiles, name).unwrap_or_else(|| panic!("{name} profile"));
                let effective = effective_profile(raw, CHROMECAST_EXTRA, Some(&media));
                assert_eq!(effective, with_extra(raw, CHROMECAST_EXTRA), "{name} {container}");
                assert!(effective.get("targets").and_then(Value::as_array).is_some_and(|t| t.len() >= 1));
                assert_eq!(effective_protocol(Some(&effective), "http", Some(&media)), "http");
                assert_eq!(conversion(&effective, "http", Some(&media)), Outcome::Convert, "{name} {container}");
            }
        }
        // and with no media at all.
        let generic = resolve(&profiles, "Generic").expect("Generic profile");
        assert_eq!(effective_profile(generic, CHROMECAST_EXTRA, None), with_extra(generic, CHROMECAST_EXTRA));
    }

    /// A target that genuinely serves HLS behaves as before, in either declaration style:
    /// `protocol=hls`, or an `mpegts` container. Android / iOS (Generic named, hls target in the
    /// Extra) keep their own target; the default is never stacked on top.
    #[test]
    fn a_target_that_serves_hls_is_left_exactly_as_it_was() {
        let profiles = parse_profiles(PROFILES).expect("profile fixture parses");
        let hls_media = serde_json::json!({"Part": [{"container": "hls"}]});
        let android_extra = "add-transcode-target(type=videoProfile&context=streaming&protocol=hls&container=mpegts&videoCodec=h264&audioCodec=aac)+add-limitation(scope=videoCodec&scopeName=hevc&scopeType=videoCodec&context=all&protocol=hls&type=notMatch&name=video.DOVIProfile&value=5)";
        let ios_extra = "add-transcode-target(type=videoProfile&context=streaming&protocol=hls&container=mpegts&videoCodec=h264,hevc&audioCodec=aac,ac3,eac3)";
        let mpegts_over_http = "add-transcode-target(type=videoProfile&context=streaming&protocol=http&container=mpegts&videoCodec=h264&audioCodec=aac)";
        let mixed = format!("{CHROMECAST_EXTRA} {ios_extra}");
        for extra in [android_extra, ios_extra, mpegts_over_http, mixed.as_str()] {
            for name in ["Generic", "Android", "Samsung", "tvOS"] {
                let raw = resolve(&profiles, name).unwrap_or_else(|| panic!("{name} profile"));
                // no default is added: the profile is exactly what the Extra made it.
                let effective = effective_profile(raw, extra, Some(&hls_media));
                assert_eq!(effective, with_extra(raw, extra), "{name} {extra:.60}");
                assert!(serves_hls(&effective), "{name}");
                let protocol = effective_protocol(Some(&effective), "http", Some(&hls_media));
                assert_eq!(conversion(&effective, protocol, Some(&hls_media)), Outcome::Convert, "{name}");
            }
        }
        // a target the profile declares as hls/mpegts is not "an http mkv" target: serves_hls is
        // true for exactly the targets that can carry the playlist.
        let hls = serde_json::json!({"tag": "VideoProfile", "context": "streaming", "protocol": "hls", "container": "mpegts"});
        let dash = serde_json::json!({"tag": "VideoProfile", "context": "streaming", "protocol": "dash", "container": "mp4"});
        let mpegts = serde_json::json!({"tag": "VideoProfile", "context": "streaming", "container": "mpegts"});
        let mkv = serde_json::json!({"tag": "VideoProfile", "context": "streaming", "protocol": "http", "container": "mkv"});
        let bare = serde_json::json!({"tag": "VideoProfile", "context": "streaming"});
        for (target, expected) in [(&hls, true), (&dash, true), (&mpegts, true), (&mkv, false), (&bare, false)] {
            assert_eq!(target_serves_source(target.as_object().expect("target object"), Some(&hls_media)), expected, "{target}");
            // a file source is matched on protocol alone, whatever the target.
            assert!(target_serves_source(target.as_object().expect("target object"), Some(&serde_json::json!({"Part": [{"container": "mkv"}]}))));
            assert!(target_serves_source(target.as_object().expect("target object"), None));
        }
    }
}
