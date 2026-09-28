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

const IMAGE_SUBTITLES: &[&str] = &[
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
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_f64().is_some_and(|number| number != 0.0),
        Some(Value::String(value)) => !value.is_empty(),
        Some(Value::Array(value)) => !value.is_empty(),
        Some(Value::Object(value)) => !value.is_empty(),
    }
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

    if selected_audio_index.is_some() && first_audio_index != selected_audio_index && !can_select {
        return (false, not_first.to_string());
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

    const PROFILES: &str = include_str!("../../client_profiles.json");
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
        assert_eq!(row_map.len(), 31);
        assert_eq!(measured, 2340);
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
        assert!(evaluated_audio(&numeric).is_some());
        assert!(evaluated_audio(&textual).is_none());
    }
}
