//! ffprobe JSON to Plex `Media`, `Part`, and `Stream` elements.
//!
//! The Python implementation is deliberately data driven: the values here are
//! the measured PMS representation of ffprobe fields.  Keeping this module
//! independent of the transport makes it usable by the catalog builder and by
//! the later native HTTP server.

use std::collections::HashMap;
use std::sync::OnceLock;

use serde_json::{json, Map, Value};

type Row = Map<String, Value>;

const TRACK_FLAGS: &[(&str, &str)] = &[
    ("default", "default"),
    ("forced", "forced"),
    ("hearingImpaired", "hearing_impaired"),
    ("visualImpaired", "visual_impaired"),
    ("original", "original"),
    ("dub", "dub"),
    ("comment", "comment"),
];

const VIDEO_FLAGS: &[(&str, &str)] = &[
    ("default", "default"),
    ("forced", "forced"),
    ("original", "original"),
    ("dub", "dub"),
];

fn languages() -> &'static HashMap<String, (String, String)> {
    static LANGUAGES: OnceLock<HashMap<String, (String, String)>> = OnceLock::new();
    LANGUAGES.get_or_init(|| {
        let raw: Value = serde_json::from_str(include_str!("../../data/languages.json"))
            .expect("languages.json is valid JSON");
        raw.as_object()
            .expect("languages.json is an object")
            .iter()
            .filter_map(|(key, value)| {
                let values = value.as_array()?;
                Some((
                    key.clone(),
                    (
                        values.first()?.as_str()?.to_string(),
                        values.get(1)?.as_str()?.to_string(),
                    ),
                ))
            })
            .collect()
    })
}

fn map() -> Row {
    Map::new()
}

fn str_value(row: &Row, key: &str) -> Option<String> {
    row.get(key).and_then(|value| match value {
        Value::String(value) => Some(value.clone()),
        Value::Number(value) => Some(value.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        _ => None,
    })
}

fn bool_value(row: &Row, key: &str) -> bool {
    row.get(key)
        .map(|value| match value {
            Value::Bool(value) => *value,
            Value::Number(value) => value.as_i64().unwrap_or(0) != 0,
            Value::String(value) => matches!(value.as_str(), "1" | "true" | "True"),
            _ => false,
        })
        .unwrap_or(false)
}

fn int_value(row: &Row, key: &str) -> Option<i64> {
    row.get(key).and_then(int_any)
}

fn int_any(value: &Value) -> Option<i64> {
    match value {
        Value::Number(value) => value.as_i64().or_else(|| value.as_u64().map(|n| n as i64)),
        Value::String(value) => value.trim().parse().ok(),
        Value::Bool(value) => Some(i64::from(*value)),
        _ => None,
    }
}

fn float_any(value: &Value) -> Option<f64> {
    match value {
        Value::Number(value) => value.as_f64(),
        Value::String(value) => value.trim().parse().ok(),
        _ => None,
    }
}

fn put(row: &mut Row, key: &str, value: impl Into<Value>) {
    row.insert(key.to_string(), value.into());
}

fn put_opt(row: &mut Row, key: &str, value: Option<impl Into<Value>>) {
    row.insert(
        key.to_string(),
        value.map(Into::into).unwrap_or(Value::Null),
    );
}

fn parse_ratio(value: &str) -> Option<f64> {
    if let Some((a, b)) = value.split_once('/') {
        let n: f64 = a.trim().parse().ok()?;
        let d: f64 = b.trim().parse().ok()?;
        if d == 0.0 {
            None
        } else {
            Some(n / d)
        }
    } else {
        value.trim().parse().ok()
    }
}

fn round_python(value: f64) -> i64 {
    if !value.is_finite() {
        return 0;
    }
    value.round_ties_even() as i64
}

fn frame_rate_token(rate: Option<&str>) -> Option<String> {
    let value = parse_ratio(rate?)?;
    if value == 0.0 || !value.is_finite() {
        return None;
    }
    let candidates = [
        (24000.0 / 1001.0, "24p"),
        (24.0, "24p"),
        (25.0, "PAL"),
        (30000.0 / 1001.0, "NTSC"),
        (30.0, "30p"),
        (50.0, "50p"),
        (60000.0 / 1001.0, "60p"),
        (60.0, "60p"),
    ];
    let (best, label) = candidates.iter().min_by(|a, b| {
        (a.0 - value)
            .abs()
            .partial_cmp(&(b.0 - value).abs())
            .unwrap()
    })?;
    if (best - value).abs() <= 0.01 {
        Some((*label).to_string())
    } else {
        Some(format!("{}p", round_python(value)))
    }
}

fn container_for(container: &str, extension: Option<&str>) -> String {
    if container == "mp4"
        && extension
            .unwrap_or("")
            .trim_start_matches('.')
            .eq_ignore_ascii_case("mov")
    {
        "mov".to_string()
    } else {
        container.to_string()
    }
}

const RESOLUTION_ORDER: &[&str] = &["sd", "480", "576", "720", "1080", "2k", "2.7k", "4k"];

pub fn video_resolution(height: Option<i64>, width: Option<i64>) -> Option<String> {
    let height = height?;
    let by_width = match width.unwrap_or(0) {
        n if n >= 2876 => "4k",
        n if n >= 2048 => "2k",
        n if n >= 1908 => "1080",
        n if n >= 1276 => "720",
        n if n >= 1024 => "576",
        n if n >= 848 => "480",
        _ => "sd",
    };
    let by_height = match height {
        n if n >= 1600 => "2.7k",
        n if n >= 1072 => "1080",
        n if n >= 714 => "720",
        n if n >= 572 => "576",
        n if n >= 480 => "480",
        _ => "sd",
    };
    Some(
        if RESOLUTION_ORDER
            .iter()
            .position(|x| *x == by_width)
            .unwrap()
            >= RESOLUTION_ORDER
                .iter()
                .position(|x| *x == by_height)
                .unwrap()
        {
            by_width
        } else {
            by_height
        }
        .to_string(),
    )
}

fn aspect_ratio(stream: Option<&Row>) -> Option<f64> {
    let stream = stream?;
    let width = int_value(stream, "width")? as f64;
    let height = int_value(stream, "height")? as f64;
    if width == 0.0 || height == 0.0 {
        return None;
    }
    let sar = str_value(stream, "sample_aspect_ratio")
        .and_then(|value| {
            let (n, d) = value.split_once(':')?;
            let n: f64 = n.parse().ok()?;
            let d: f64 = d.parse().ok()?;
            (d != 0.0).then_some(n / d)
        })
        .filter(|value| *value > 0.0)
        .unwrap_or(1.0);
    [1.33, 1.66, 1.78, 1.85, 2.2, 2.35]
        .iter()
        .min_by(|a, b| {
            (**a - width * sar / height)
                .abs()
                .partial_cmp(&((**b - width * sar / height).abs()))
                .unwrap()
        })
        .copied()
}

/// `languages.json`'s name for an ISO 639-2 code (`eng` -> English).
pub(crate) fn language_name(code: &str) -> Option<String> {
    languages().get(code).map(|(name, _)| name.clone())
}

/// The ISO 639-2 code `languages.json` gives a language tag (`en` -> `eng`).
pub(crate) fn language_code_of_tag(tag: &str) -> Option<String> {
    languages().iter().find(|(_, (_, known))| known == tag).map(|(code, _)| code.clone())
}

pub(crate) fn terminology(raw: &str) -> &str {
    match raw.to_ascii_lowercase().as_str() {
        "alb" => "sqi",
        "baq" => "eus",
        "chi" => "zho",
        "cze" => "ces",
        "dut" => "nld",
        "fre" => "fra",
        "ger" => "deu",
        "gre" => "ell",
        "ice" => "isl",
        "mac" => "mkd",
        "may" => "msa",
        "per" => "fas",
        "rum" => "ron",
        "slo" => "slk",
        "wel" => "cym",
        "arm" => "hye",
        "bur" => "mya",
        "geo" => "kat",
        "tib" => "bod",
        _ => raw,
    }
}

fn language_fields(tags: Option<&Row>) -> Row {
    let raw = tags
        .and_then(|tags| str_value(tags, "language"))
        .unwrap_or_default();
    let code = terminology(&raw).to_ascii_lowercase();
    let mut out = map();
    if code.is_empty() || code == "und" {
        return out;
    }
    if let Some((name, tag)) = languages().get(&code) {
        put(&mut out, "language", name.clone());
        put(&mut out, "languageCode", code);
        put(&mut out, "languageTag", tag.clone());
    } else if code.len() == 3 && code.chars().all(|c| c.is_ascii_alphabetic()) {
        put(&mut out, "language", code.clone());
        put(&mut out, "languageCode", code.clone());
        put(&mut out, "languageTag", code);
    } else {
        put(&mut out, "language", raw.clone());
        put(&mut out, "languageCode", "");
        put(&mut out, "languageTag", raw);
    }
    out
}

fn title_words(text: &str) -> Vec<String> {
    text.split(|c: char| {
        c.is_whitespace()
            || matches!(
                c,
                '-' | '/' | '(' | ')' | '[' | ']' | '+' | ',' | '|' | ':' | ';' | '&'
            )
    })
    .filter(|word| !word.is_empty())
    .map(|word| word.to_ascii_lowercase())
    .collect()
}

fn alnum(text: &str) -> String {
    text.chars()
        .filter(|c| c.is_ascii_alphanumeric())
        .map(|c| c.to_ascii_lowercase())
        .collect()
}

fn carried(title: &str, part: &str, stand_ins: &[&str], raw: &[&str]) -> bool {
    let words = title_words(title);
    let folded: Vec<String> = title_words(title).iter().map(|word| alnum(word)).collect();
    let first = title_words(part).first().cloned().unwrap_or_default();
    stand_ins
        .iter()
        .any(|one| words.iter().any(|word| word == &one.to_ascii_lowercase()))
        || (!first.is_empty() && words.iter().any(|word| word == &first))
        || std::iter::once(part)
            .chain(raw.iter().copied())
            .map(alnum)
            .filter(|x| !x.is_empty())
            .any(|candidate| folded.iter().any(|word| word == &candidate))
}

fn language_carried(title: &str, language: &str, tag: &str) -> bool {
    let words = title_words(title);
    let base = language
        .rsplit_once('(')
        .map(|(x, _)| x.trim())
        .unwrap_or(language)
        .trim();
    let base_words = title_words(base);
    (base_words.len() == 1 && words.iter().any(|word| word == &base.to_ascii_lowercase()))
        || (!tag.is_empty() && words.iter().any(|word| word == &tag.to_ascii_lowercase()))
}

fn extended_title(
    title: &str,
    parts: Vec<(String, Vec<&str>, Vec<String>)>,
    keep: &[String],
) -> String {
    let left: Vec<String> = parts
        .into_iter()
        .filter_map(|(part, stand_ins, raw)| {
            if part.is_empty()
                || keep.iter().any(|item| item == &part)
                || !carried(
                    title,
                    &part,
                    &stand_ins,
                    &raw.iter().map(String::as_str).collect::<Vec<_>>(),
                )
            {
                Some(part)
            } else {
                None
            }
        })
        .collect();
    if left.is_empty() {
        title.to_string()
    } else {
        format!("{} ({})", title, left.join(" "))
    }
}

fn video_resolution_label(row: &Row) -> Option<String> {
    let raw = video_resolution(int_value(row, "height"), int_value(row, "width"))?;
    let mut label = match raw.as_str() {
        "4k" => "4K",
        "2.7k" | "2k" | "1080" => "1080",
        "720" => "720",
        "576" | "480" => "480",
        "sd" => "SD",
        _ => return None,
    }
    .to_string();
    if label != "4K" && label != "SD" {
        label.push_str(
            if str_value(row, "scanType").as_deref() == Some("interlaced") {
                "i"
            } else {
                "p"
            },
        );
    }
    Some(label)
}

fn dynamic_range(row: &Row) -> Option<String> {
    if bool_value(row, "DOVIPresent") {
        if !bool_value(row, "DOVIBLPresent") {
            Some("DoVi Enhancement Layer".to_string())
        } else if matches!(int_value(row, "DOVIBLCompatID"), Some(1 | 6)) {
            Some("DoVi/HDR10".to_string())
        } else {
            Some("DoVi".to_string())
        }
    } else if str_value(row, "colorTrc").as_deref() == Some("smpte2084") {
        Some("HDR10".to_string())
    } else {
        None
    }
}

pub fn video_codec_label(row: &Row) -> Option<String> {
    let codec = str_value(row, "codec")?.to_ascii_lowercase();
    let mut label = match codec.as_str() {
        "h264" => "H.264".to_string(),
        "hevc" => "HEVC".to_string(),
        _ => codec.to_ascii_uppercase(),
    };
    let profile = str_value(row, "profile");
    if let Some(profile) = profile.filter(|profile| {
        codec == "hevc" || (codec == "h264" && profile != "high" && profile != "main")
    }) {
        label.push(' ');
        label.push_str(
            &profile
                .split_whitespace()
                .map(|word| {
                    let mut chars = word.chars();
                    match chars.next() {
                        Some(first) => format!("{}{}", first.to_ascii_uppercase(), chars.as_str()),
                        None => String::new(),
                    }
                })
                .collect::<Vec<_>>()
                .join(" "),
        );
    }
    Some(label)
}

fn audio_codec_label(row: &Row) -> String {
    let codec = str_value(row, "codec")
        .unwrap_or_default()
        .to_ascii_lowercase();
    if codec == "dca" {
        return match str_value(row, "profile").as_deref() {
            Some("ma") => "DTS-HD MA",
            Some("es") => "DTS-ES",
            Some("hra") => "DTS-HD HRA",
            Some("express") => "DTS Express",
            _ => "DTS",
        }
        .to_string();
    }
    if codec == "aac" && str_value(row, "profile").as_deref() == Some("he-aac") {
        return "HE-AAC".to_string();
    }
    codec.to_ascii_uppercase()
}

fn channel_label(row: &Row) -> Option<String> {
    if let Some(layout) = str_value(row, "audioChannelLayout") {
        let base = layout.split('(').next().unwrap_or("").trim();
        return Some(
            match base {
                "mono" => "Mono",
                "stereo" => "Stereo",
                x => x,
            }
            .to_string(),
        );
    }
    let channels = int_value(row, "channels")?;
    Some(match channels {
        1 => "Mono".into(),
        2 => "Stereo".into(),
        6 => "5.1".into(),
        8 => "7.1".into(),
        n => format!("{}ch", n),
    })
}

pub fn subtitle_codec_label(row: &Row, external: &str) -> String {
    let codec = str_value(row, "codec")
        .unwrap_or_default()
        .to_ascii_uppercase();
    let codec = if codec == "EIA_608" {
        "Closed Captions".to_string()
    } else {
        codec
    };
    if !codec.is_empty()
        && str_value(row, "key")
            .unwrap_or_default()
            .starts_with("/library/streams/")
    {
        format!(
            "{} {}",
            codec,
            str_value(row, "providerTitle").unwrap_or_else(|| external.to_string())
        )
    } else {
        codec
    }
}

pub fn stream_titles(row: &mut Row, stand_in: Option<&str>, words: Option<&Row>) {
    let words = words.cloned().unwrap_or_default();
    let unknown = str_value(&words, "Unknown").unwrap_or_else(|| "Unknown".to_string());
    let kind = int_value(row, "streamType");
    let title = str_value(row, "title");
    match kind {
        Some(1) => {
            let Some(resolution) = video_resolution_label(row) else {
                return;
            };
            let dynamic = dynamic_range(row);
            let codec = video_codec_label(row);
            let plain = [Some(resolution.clone()), dynamic.clone()]
                .into_iter()
                .flatten()
                .collect::<Vec<_>>()
                .join(" ");
            put(row, "displayTitle", plain.clone());
            if let Some(title) = title {
                let avc = if str_value(row, "codec")
                    .unwrap_or_default()
                    .eq_ignore_ascii_case("h264")
                {
                    vec!["AVC"]
                } else {
                    vec![]
                };
                let mut parts = vec![
                    (resolution, vec![], vec![]),
                    (dynamic.unwrap_or_default(), vec!["HDR", "Dolby"], vec![]),
                    (
                        codec.unwrap_or_default(),
                        avc,
                        vec![str_value(row, "codec").unwrap_or_default()],
                    ),
                ];
                parts.retain(|(x, _, _)| !x.is_empty());
                put(
                    row,
                    "extendedDisplayTitle",
                    extended_title(&title, parts, &[]),
                );
            } else {
                put(
                    row,
                    "extendedDisplayTitle",
                    if let Some(codec) = codec {
                        format!("{} ({})", plain, codec)
                    } else {
                        plain
                    },
                );
            }
        }
        Some(2) => {
            let language = str_value(row, "language");
            let codec = audio_codec_label(row);
            let channels = channel_label(row);
            put(
                row,
                "displayTitle",
                format!(
                    "{} ({})",
                    language.clone().unwrap_or(unknown),
                    [Some(codec.clone()), channels.clone()]
                        .into_iter()
                        .flatten()
                        .collect::<Vec<_>>()
                        .join(" ")
                ),
            );
            if let Some(title) = title {
                let stereo = match channels.as_deref() {
                    Some("Stereo") => vec!["2.0"],
                    Some("Mono") => vec!["1.0"],
                    _ => vec![],
                };
                let mut parts = vec![
                    (
                        codec,
                        vec![],
                        vec![str_value(row, "codec").unwrap_or_default()],
                    ),
                    (channels.unwrap_or_default(), stereo, vec![]),
                ];
                let mut keep = vec![];
                if let Some(language) = language {
                    if !language_carried(
                        &title,
                        stand_in.unwrap_or(&language),
                        &str_value(row, "languageTag").unwrap_or_default(),
                    ) {
                        keep.push(language.clone());
                        parts.insert(0, (language, vec![], vec![]));
                    }
                }
                put(
                    row,
                    "extendedDisplayTitle",
                    extended_title(&title, parts, &keep),
                );
            } else {
                put(
                    row,
                    "extendedDisplayTitle",
                    row.get("displayTitle").cloned().unwrap_or(Value::Null),
                );
            }
        }
        Some(3) => {
            let language = str_value(row, "language");
            let forced = bool_value(row, "forced")
                .then(|| str_value(&words, "Forced").unwrap_or_else(|| "Forced".into()));
            let sdh = bool_value(row, "hearingImpaired")
                .then(|| str_value(&words, "SDH").unwrap_or_else(|| "SDH".into()));
            let external = str_value(&words, "External").unwrap_or_else(|| "External".to_string());
            let codec = subtitle_codec_label(row, &external);
            put(
                row,
                "displayTitle",
                [
                    language.clone().unwrap_or(unknown),
                    forced.clone().unwrap_or_default(),
                    sdh.clone().unwrap_or_default(),
                ]
                .into_iter()
                .filter(|x| !x.is_empty())
                .collect::<Vec<_>>()
                .join(" "),
            );
            if let Some(title) = title {
                let mut parts = vec![
                    (
                        forced.unwrap_or_default(),
                        vec!["Forced", "Signs", "Songs"],
                        vec![],
                    ),
                    (sdh.unwrap_or_default(), vec!["SDH", "CC", "HI"], vec![]),
                    (
                        codec,
                        vec![],
                        vec![str_value(row, "codec").unwrap_or_default()],
                    ),
                ];
                parts.retain(|(x, _, _)| !x.is_empty());
                let mut keep = vec![];
                if let Some(language) = language {
                    if !language_carried(
                        &title,
                        stand_in.unwrap_or(&language),
                        &str_value(row, "languageTag").unwrap_or_default(),
                    ) {
                        keep.push(language.clone());
                        parts.insert(0, (language, vec![], vec![]));
                    }
                }
                put(
                    row,
                    "extendedDisplayTitle",
                    extended_title(&title, parts, &keep),
                );
            } else {
                let plain = str_value(row, "displayTitle").unwrap_or_default();
                put(
                    row,
                    "extendedDisplayTitle",
                    if codec.is_empty() {
                        plain
                    } else {
                        format!("{} ({})", plain, codec)
                    },
                );
            }
        }
        _ => {}
    }
}

fn disposition(stream: &Row, fields: &[(&str, &str)]) -> Row {
    let mut out = map();
    let flags = stream.get("disposition").and_then(Value::as_object);
    for (field, source) in fields {
        if flags
            .and_then(|flags| flags.get(*source))
            .map(bool_value_value)
            .unwrap_or(false)
        {
            put(&mut out, field, true);
        }
    }
    out
}

fn bool_value_value(value: &Value) -> bool {
    int_any(value).unwrap_or(0) != 0 || value.as_bool().unwrap_or(false)
}

fn pixel_aspect(stream: &Row) -> Row {
    let Some(raw) = str_value(stream, "sample_aspect_ratio") else {
        return map();
    };
    let Some((n, d)) = raw.split_once(':') else {
        return map();
    };
    let Ok(n): Result<i64, _> = n.parse() else {
        return map();
    };
    let Ok(d): Result<i64, _> = d.parse() else {
        return map();
    };
    if d == 0 || n == d || n == 0 {
        return map();
    }
    let mut out = map();
    put(&mut out, "pixelAspectRatio", raw);
    // |n/d - 1| > 1/20, in integers: `21:20` sits exactly on the boundary, which a real
    // PMS calls square and a float reads as 0.050000000000000044 over it.
    put(
        &mut out,
        "anamorphic",
        (i128::from(n) - i128::from(d)).abs() * 20 > i128::from(d).abs(),
    );
    out
}

fn bit_depth(stream: &Row) -> i64 {
    if let Some(value) = int_value(stream, "bits_per_raw_sample") {
        if value != 0 {
            return value;
        }
    }
    let pix = str_value(stream, "pix_fmt").unwrap_or_default();
    if let Some(position) = pix.find('p') {
        let digits: String = pix[position + 1..]
            .chars()
            .take_while(|c| c.is_ascii_digit())
            .collect();
        let tail = &pix[position + 1 + digits.len()..];
        if (tail.starts_with("le") || tail.starts_with("be")) && !digits.is_empty() {
            if let Ok(value) = digits.parse() {
                return value;
            }
        }
    }
    8
}

fn video_profile(profile: Option<&str>) -> Option<String> {
    let mut text = profile.unwrap_or("").trim().to_ascii_lowercase();
    if text.ends_with(" profile") {
        text.truncate(text.len() - 8);
    }
    if text.is_empty() || text == "0" {
        None
    } else {
        Some(text)
    }
}

fn coded_height(stream: &Row) -> Option<i64> {
    let height = int_value(stream, "coded_height")?;
    Some(
        if str_value(stream, "codec_name").as_deref() == Some("h264") {
            (height + 15) / 16 * 16
        } else {
            height
        },
    )
}

fn codec_id(stream: &Row) -> Option<String> {
    let tag = str_value(stream, "codec_tag_string")?;
    if tag.is_empty() || tag.contains('[') || tag == "avc1" {
        None
    } else {
        Some(tag)
    }
}

fn stream_identifier(stream: &Row) -> Option<String> {
    let id = str_value(stream, "id")?;
    let id = id.strip_prefix("0x").unwrap_or(&id);
    i64::from_str_radix(id, 16).ok().map(|v| v.to_string())
}

fn audio_bit_depth(stream: &Row) -> Option<i64> {
    if let Some(value) = int_value(stream, "bits_per_raw_sample") {
        if value != 0 {
            return Some(value);
        }
    }
    if str_value(stream, "codec_name").as_deref() == Some("dts")
        && str_value(stream, "profile").as_deref() != Some("DTS Express")
    {
        Some(24)
    } else {
        None
    }
}

fn float_ratio(rate: Option<&str>) -> Option<f64> {
    let value = parse_ratio(rate?)?;
    Some((value * 1000.0).round() / 1000.0)
}

fn chroma(pix: &str) -> &'static str {
    if pix.contains("444") {
        "4:4:4"
    } else if pix.contains("422") {
        "4:2:2"
    } else {
        "4:2:0"
    }
}

fn scan_type(order: Option<&str>) -> Option<&'static str> {
    order.map(|value| {
        if value == "progressive" {
            "progressive"
        } else {
            "interlaced"
        }
    })
}

const SUBTITLE_CODECS: &[(&str, &str)] = &[
    ("subrip", "srt"),
    ("hdmv_pgs_subtitle", "pgs"),
    ("dvd_subtitle", "vobsub"),
];

fn audio_codec(codec: Option<&str>, profile: Option<&str>) -> (String, Option<String>) {
    let codec = codec.unwrap_or("").to_ascii_lowercase();
    let profile = profile
        .map(str::to_ascii_lowercase)
        .filter(|x| !x.is_empty());
    if codec == "dts" || codec == "dca" {
        let p = match profile.as_deref() {
            Some("dts") => Some("dts"),
            Some("dts-hd ma") => Some("ma"),
            Some("dts-es") => Some("es"),
            Some("dts-hd hra") => Some("hra"),
            Some("dts express") => Some("express"),
            x => x,
        };
        return ("dca".into(), p.map(str::to_string));
    }
    if codec.starts_with("pcm_") && codec != "pcm_bluray" {
        return ("pcm".into(), Some(codec));
    }
    if profile.as_deref().is_some_and(|x| x.contains("atmos")) {
        return (codec, None);
    }
    (codec, profile)
}

fn video_stream(stream: &Row, stream_id: i64) -> Row {
    let mut row = map();
    put(&mut row, "id", stream_id);
    put(&mut row, "streamType", 1);
    put_opt(&mut row, "codec", str_value(stream, "codec_name"));
    put_opt(&mut row, "index", int_value(stream, "index"));
    put_opt(&mut row, "streamIdentifier", stream_identifier(stream));
    put_opt(&mut row, "width", int_value(stream, "width"));
    put_opt(&mut row, "height", int_value(stream, "height"));
    put_opt(
        &mut row,
        "profile",
        video_profile(str_value(stream, "profile").as_deref()),
    );
    put_opt(
        &mut row,
        "level",
        int_value(stream, "level").filter(|v| *v != -99),
    );
    put_opt(&mut row, "refFrames", int_value(stream, "refs"));
    put(&mut row, "bitDepth", bit_depth(stream));
    put_opt(
        &mut row,
        "chromaSubsampling",
        str_value(stream, "pix_fmt").map(|x| chroma(&x)),
    );
    put_opt(
        &mut row,
        "chromaLocation",
        str_value(stream, "chroma_location"),
    );
    put_opt(&mut row, "codedHeight", coded_height(stream));
    put_opt(&mut row, "codedWidth", int_value(stream, "coded_width"));
    put_opt(&mut row, "codecID", codec_id(stream));
    put_opt(
        &mut row,
        "colorPrimaries",
        str_value(stream, "color_primaries"),
    );
    put_opt(&mut row, "colorTrc", str_value(stream, "color_transfer"));
    put_opt(&mut row, "colorSpace", str_value(stream, "color_space"));
    put_opt(&mut row, "colorRange", str_value(stream, "color_range"));
    let rate = str_value(stream, "avg_frame_rate")
        .filter(|x| !x.starts_with("0/"))
        .or_else(|| str_value(stream, "r_frame_rate"));
    put_opt(&mut row, "frameRate", float_ratio(rate.as_deref()));
    put_opt(
        &mut row,
        "scanType",
        scan_type(str_value(stream, "field_order").as_deref()),
    );
    let pa = pixel_aspect(stream);
    for (k, v) in pa {
        row.insert(k, v);
    }
    if let Some(dovi) = stream
        .get("side_data_list")
        .and_then(Value::as_array)
        .and_then(|list| {
            list.iter().find(|entry| {
                entry.get("side_data_type").and_then(Value::as_str)
                    == Some("DOVI configuration record")
            })
        })
        .and_then(Value::as_object)
    {
        put(&mut row, "DOVIPresent", true);
        put_opt(
            &mut row,
            "DOVIProfile",
            int_any(dovi.get("dv_profile").unwrap_or(&Value::Null)),
        );
        put_opt(
            &mut row,
            "DOVILevel",
            int_any(dovi.get("dv_level").unwrap_or(&Value::Null)),
        );
        put_opt(
            &mut row,
            "DOVIBLCompatID",
            int_any(
                dovi.get("dv_bl_signal_compatibility_id")
                    .unwrap_or(&Value::Null),
            ),
        );
        put(
            &mut row,
            "DOVIBLPresent",
            bool_value_value(dovi.get("bl_present_flag").unwrap_or(&Value::Null)),
        );
        put(
            &mut row,
            "DOVIELPresent",
            bool_value_value(dovi.get("el_present_flag").unwrap_or(&Value::Null)),
        );
        put(
            &mut row,
            "DOVIRPUPresent",
            bool_value_value(dovi.get("rpu_present_flag").unwrap_or(&Value::Null)),
        );
        put(
            &mut row,
            "DOVIVersion",
            format!(
                "{}.{}",
                int_any(dovi.get("dv_version_major").unwrap_or(&json!(1))).unwrap_or(1),
                int_any(dovi.get("dv_version_minor").unwrap_or(&json!(0))).unwrap_or(0)
            ),
        );
    }
    for (field, source) in VIDEO_FLAGS {
        if stream
            .get("disposition")
            .and_then(|d| d.get(*source))
            .map(bool_value_value)
            .unwrap_or(false)
        {
            put(&mut row, field, true);
        }
    }
    let tags = stream.get("tags").and_then(Value::as_object);
    for (k, v) in language_fields(tags).into_iter() {
        row.insert(k, v);
    }
    if let Some(title) = tags.and_then(|x| x.get("title")).cloned() {
        row.insert("title".into(), title);
    }
    if let Some(rate) = int_value(stream, "bit_rate") {
        put(&mut row, "bitrate", round_python(rate as f64 / 1000.0));
    }
    stream_titles(&mut row, None, None);
    row
}

fn audio_stream(stream: &Row, stream_id: i64, selected: bool) -> Row {
    let tags = stream.get("tags").and_then(Value::as_object);
    let (codec, profile) = audio_codec(
        str_value(stream, "codec_name").as_deref(),
        str_value(stream, "profile").as_deref(),
    );
    let mut row = map();
    put(&mut row, "id", stream_id);
    put(&mut row, "streamType", 2);
    put(&mut row, "codec", codec);
    put_opt(&mut row, "index", int_value(stream, "index"));
    put_opt(&mut row, "streamIdentifier", stream_identifier(stream));
    put(
        &mut row,
        "channels",
        int_value(stream, "channels").unwrap_or(2),
    );
    put_opt(
        &mut row,
        "audioChannelLayout",
        str_value(stream, "channel_layout"),
    );
    put_opt(&mut row, "samplingRate", int_value(stream, "sample_rate"));
    put_opt(&mut row, "bitDepth", audio_bit_depth(stream));
    put_opt(&mut row, "profile", profile);
    if selected {
        put(&mut row, "selected", true);
    }
    for (k, v) in disposition(stream, TRACK_FLAGS) {
        row.insert(k, v);
    }
    for (k, v) in language_fields(tags).into_iter() {
        row.insert(k, v);
    }
    if let Some(rate) = int_value(stream, "bit_rate") {
        put(&mut row, "bitrate", round_python(rate as f64 / 1000.0));
    }
    if let Some(title) = tags.and_then(|x| x.get("title")).cloned() {
        row.insert("title".into(), title);
    }
    stream_titles(&mut row, None, None);
    row
}

/// Text streams the measured subtitle synchronizer can parse and retime. Capability still
/// requires a resolvable source and an independent reference; the runtime projects that.
pub(crate) fn subtitle_sync_text(stream: &Value) -> bool {
    stream.get("streamType").and_then(Value::as_i64) == Some(3)
        && matches!(stream.get("codec").and_then(Value::as_str), Some("srt" | "subrip" | "ass" | "ssa" | "vtt" | "webvtt" | "mov_text" | "tx3g"))
        && (stream.get("_content").and_then(Value::as_str).is_some()
            || stream.get("index").and_then(Value::as_i64).is_some_and(|index| index >= 0))
}

fn subtitle_stream(stream: &Row, stream_id: i64) -> Row {
    let tags = stream.get("tags").and_then(Value::as_object);
    let raw = str_value(stream, "codec_name").unwrap_or_default();
    let codec = SUBTITLE_CODECS
        .iter()
        .find(|(a, _)| *a == raw)
        .map(|(_, b)| (*b).to_string())
        .unwrap_or(raw);
    let mut row = map();
    put(&mut row, "id", stream_id);
    put(&mut row, "streamType", 3);
    put(&mut row, "canAutoSync", false);
    put(&mut row, "codec", codec);
    put_opt(&mut row, "index", int_value(stream, "index"));
    put_opt(&mut row, "streamIdentifier", stream_identifier(stream));
    for (k, v) in disposition(stream, TRACK_FLAGS) {
        row.insert(k, v);
    }
    if let Some(rate) = int_value(stream, "bit_rate") {
        put(&mut row, "bitrate", round_python(rate as f64 / 1000.0));
    }
    for (k, v) in language_fields(tags).into_iter() {
        row.insert(k, v);
    }
    if let Some(title) = tags.and_then(|x| x.get("title")).cloned() {
        row.insert("title".into(), title);
    }
    stream_titles(&mut row, None, None);
    row
}

/// Embedded chapters measured against PMS (2 Fast 2 Furious, 2026-10-17):
/// one-based indexes, title tags, integer millisecond boundaries. The source's
/// ffprobe ids can be negative/huge; use stable part-scoped ids instead. No thumb
/// or thumb filter is advertised because rex has not generated chapter images.
pub fn build_chapters(probe: &Value, part_id: i64) -> Value {
    let offset = |chapter: &Value, name: &str| -> Option<i64> {
        let seconds = chapter.get(format!("{name}_time")).and_then(float_any).or_else(|| {
            let ticks = chapter.get(name).and_then(float_any)?;
            let base = chapter.get("time_base")?.as_str().and_then(parse_ratio)?;
            Some(ticks * base)
        })?;
        let millis = seconds * 1000.0;
        (millis.is_finite() && millis >= 0.0 && millis < i64::MAX as f64)
            .then(|| millis.round() as i64)
    };
    let mut chapters: Vec<(i64, i64, Option<&str>)> = probe.get("chapters")
        .and_then(Value::as_array).into_iter().flatten().filter_map(|chapter| {
            let start = offset(chapter, "start")?;
            let end = offset(chapter, "end")?;
            (end > start).then(|| (start, end, chapter.pointer("/tags/title")
                .and_then(Value::as_str).filter(|title| !title.is_empty())))
        }).collect();
    chapters.sort_by_key(|chapter| chapter.0);
    Value::Array(chapters.into_iter().enumerate().map(|(i, (start, end, title))| {
        let index = i as i64 + 1;
        let mut chapter = json!({
            "id": part_id.saturating_mul(10000).saturating_add(index),
            "index": index, "startTimeOffset": start, "endTimeOffset": end
        });
        if let Some(title) = title { chapter["tag"] = json!(title); }
        chapter
    }).collect())
}

/// Chapter timing belongs to a file version, not the shared movie/episode identity.
/// PMS exposes the first version's chapters on metadata and play queues; keep the
/// chapterSource flag on ordinary reads, and Chapter only when requested.
pub fn project_chapters(original: &Value, projected: &mut Value, include: bool) {
    let chapters = original.pointer("/Media/0/_chapters").or_else(|| original.get("Chapter"))
        .and_then(Value::as_array);
    let Some(object) = projected.as_object_mut() else { return; };
    object.shift_remove("Chapter");
    if let Some(chapters) = chapters {
        if chapters.is_empty() {
            object.shift_remove("chapterSource");
        } else {
            object.insert("chapterSource".to_string(), json!("media"));
            if include { object.insert("Chapter".to_string(), json!(chapters)); }
        }
    }
}

/// Build one Plex Media element from an ffprobe object.
pub fn build_media(
    probe: &Value,
    media_id: i64,
    part_id: i64,
    part_key: &str,
    file_path: &str,
    size: i64,
    container_hint: Option<&str>,
) -> Value {
    let fmt = probe.get("format").and_then(Value::as_object);
    let streams = probe
        .get("streams")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let videos: Vec<Row> = streams
        .iter()
        .filter_map(Value::as_object)
        .filter(|s| {
            s.get("codec_type").and_then(Value::as_str) == Some("video")
                && !s
                    .get("disposition")
                    .and_then(|d| d.get("attached_pic"))
                    .map(bool_value_value)
                    .unwrap_or(false)
        })
        .cloned()
        .collect();
    let audios: Vec<Row> = streams
        .iter()
        .filter_map(Value::as_object)
        .filter(|s| s.get("codec_type").and_then(Value::as_str) == Some("audio"))
        .cloned()
        .collect();
    let subtitles: Vec<Row> = streams
        .iter()
        .filter_map(Value::as_object)
        .filter(|s| s.get("codec_type").and_then(Value::as_str) == Some("subtitle"))
        .cloned()
        .collect();
    let video = videos.first();
    let duration_ms = fmt
        .and_then(|x| x.get("duration"))
        .and_then(float_any)
        .map(|x| (x * 1000.0).round() as i64)
        .unwrap_or(0);
    let bitrate = fmt
        .and_then(|x| x.get("bit_rate"))
        .and_then(int_any)
        .map(|x| round_python(x as f64 / 1000.0))
        .filter(|x| *x != 0);
    let mut rows = Vec::new();
    // Python integers do not overflow while assigning stream ids.  The
    // catalog contract still serialises these ids as signed JSON numbers, so
    // saturate at the representable edge instead of aborting a debug build.
    let mut next = part_id.saturating_mul(100);
    for s in &videos {
        next = next.saturating_add(1);
        rows.push(Value::Object(video_stream(s, next)));
    }
    for (i, s) in audios.iter().enumerate() {
        next = next.saturating_add(1);
        rows.push(Value::Object(audio_stream(s, next, i == 0)));
    }
    for s in &subtitles {
        next = next.saturating_add(1);
        rows.push(Value::Object(subtitle_stream(s, next)));
    }
    let width = video.and_then(|x| int_value(x, "width"));
    let height = video.and_then(|x| int_value(x, "height"));
    let main = audios.iter().fold(None, |best: Option<&Row>, candidate| {
        if best.is_none()
            || int_value(candidate, "channels").unwrap_or(0)
                > int_value(best.unwrap(), "channels").unwrap_or(0)
        {
            Some(candidate)
        } else {
            best
        }
    });
    let (main_codec, main_profile) = audio_codec(
        main.and_then(|x| str_value(x, "codec_name")).as_deref(),
        main.and_then(|x| str_value(x, "profile")).as_deref(),
    );
    let format_name = fmt
        .and_then(|x| str_value(x, "format_name"))
        .unwrap_or_default();
    let mut container = match format_name.as_str() {
        "matroska,webm" | "matroska" => "mkv",
        "mov,mp4,m4a,3gp,3g2,mj2" => "mp4",
        "mpegts" => "mpegts",
        "webm" => "webm",
        "avi" => "avi",
        x => x.split(',').next().unwrap_or(""),
    }
    .to_string();
    container = container_for(&container, container_hint);
    let profile = video.and_then(|x| video_profile(str_value(x, "profile").as_deref()));
    let mut part = map();
    put(&mut part, "id", part_id);
    put(&mut part, "key", part_key);
    put(&mut part, "duration", duration_ms);
    put(&mut part, "file", file_path);
    put(&mut part, "size", size);
    put(&mut part, "container", container.clone());
    put_opt(&mut part, "videoProfile", profile.clone());
    put_opt(&mut part, "audioProfile", main_profile.clone());
    put(&mut part, "Stream", Value::Array(rows));
    let mut media = map();
    put(&mut media, "id", media_id);
    put(&mut media, "duration", duration_ms);
    put_opt(&mut media, "bitrate", bitrate);
    put_opt(&mut media, "width", width);
    put_opt(&mut media, "height", height);
    put_opt(&mut media, "aspectRatio", aspect_ratio(video));
    put_opt(
        &mut media,
        "audioChannels",
        main.and_then(|x| int_value(x, "channels")),
    );
    let ac = if main_codec == "dca" && main_profile.as_deref() == Some("ma") {
        Some("dca-ma".into())
    } else if main_codec.is_empty() {
        None
    } else {
        Some(main_codec)
    };
    put_opt(&mut media, "audioCodec", ac);
    put_opt(
        &mut media,
        "videoCodec",
        video.and_then(|x| str_value(x, "codec_name")),
    );
    put_opt(
        &mut media,
        "videoResolution",
        video_resolution(height, width),
    );
    put(&mut media, "container", container);
    let video_rate = video
        .and_then(|x| str_value(x, "avg_frame_rate"))
        .filter(|x| !x.starts_with("0/"))
        .or_else(|| video.and_then(|x| str_value(x, "r_frame_rate")));
    put_opt(
        &mut media,
        "videoFrameRate",
        frame_rate_token(video_rate.as_deref()),
    );
    put_opt(&mut media, "videoProfile", profile);
    put_opt(&mut media, "audioProfile", main_profile);
    put(&mut media, "Part", Value::Array(vec![Value::Object(part)]));
    let mut media = Value::Object(media);
    normalize_media(&mut media);
    // An absent chapter list is not an authoritative empty one: older sources
    // may not request -show_chapters. Keep it distinct on subsequent feed pushes.
    if probe.get("chapters").is_some_and(Value::is_array) {
        media["_chapters"] = build_chapters(probe, part_id);
    }
    media
}

const MP4_FAMILY: &[&str] = &["mp4", "m4v", "mov"];

/// Apply the load-time corrections used for catalogs built by older versions.
pub fn normalize_media(media: &mut Value) {
    let Some(media) = media.as_object_mut() else {
        return;
    };
    let Some(mut parts) = media.get("Part").and_then(Value::as_array).cloned() else {
        return;
    };
    // Work on a cloned array so that updates to the parent map do not hold a
    // mutable borrow across the child pass. Replacing an existing Map entry
    // retains its insertion position with serde_json's preserve_order map.
    for part in parts.iter_mut() {
        let Some(p) = part.as_object_mut() else {
            continue;
        };
        let name = str_value(p, "file")
            .or_else(|| str_value(p, "key"))
            .unwrap_or_default();
        let ext = name.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
        if ext == "webm" {
            put(media, "container", "mkv");
            put(p, "container", "mkv");
        }
        if ext == "mov" && str_value(media, "container").as_deref() == Some("mp4") {
            put(media, "container", "mov");
            put(p, "container", "mov");
        }
    }
    let container = str_value(media, "container")
        .unwrap_or_default()
        .to_ascii_lowercase();
    media.shift_remove("optimizedForStreaming");
    media.shift_remove("has64bitOffsets");
    if MP4_FAMILY.contains(&container.as_str()) {
        let large = parts
            .iter()
            .any(|part| part.get("size").and_then(int_any).unwrap_or(0) > 2_i64.pow(32));
        put(media, "optimizedForStreaming", 0);
        put(media, "has64bitOffsets", large);
    }
    // Feed flags are not analysis. Only the runtime's matching measured reference may
    // project this back to true after normalization.
    put(media, "hasVoiceActivity", false);
    let old_audio = str_value(media, "audioCodec");
    let old_audio_profile = str_value(media, "audioProfile");
    if old_audio.as_deref() == Some("dts") {
        let (_, profile) = audio_codec(Some("dts"), old_audio_profile.as_deref());
        put(
            media,
            "audioCodec",
            if profile.as_deref() == Some("ma") {
                "dca-ma"
            } else {
                "dca"
            },
        );
        put_opt(media, "audioProfile", profile);
    }
    if let (Some(width), Some(height)) = (int_value(media, "width"), int_value(media, "height")) {
        put_opt(
            media,
            "videoResolution",
            video_resolution(Some(height), Some(width)),
        );
        let known_aspect = media
            .get("aspectRatio")
            .and_then(Value::as_f64)
            .is_some_and(|value| {
                [1.33, 1.66, 1.78, 1.85, 2.2, 2.35]
                    .iter()
                    .any(|known| (value - known).abs() < f64::EPSILON)
            });
        if !known_aspect {
            put_opt(media, "aspectRatio", aspect_ratio(Some(media)));
        }
    }
    if media.contains_key("videoProfile") {
        put_opt(
            media,
            "videoProfile",
            video_profile(str_value(media, "videoProfile").as_deref()),
        );
    }
    for part in parts.iter_mut() {
        let Some(p) = part.as_object_mut() else {
            continue;
        };
        if p.contains_key("videoProfile") {
            put_opt(
                p,
                "videoProfile",
                video_profile(str_value(p, "videoProfile").as_deref()),
            );
        }
        p.shift_remove("optimizedForStreaming");
        p.shift_remove("has64bitOffsets");
        if container == "mpegts" {
            put(p, "packetLength", 188)
        } else {
            p.shift_remove("packetLength");
        }
        if MP4_FAMILY.contains(&container.as_str()) {
            put(p, "optimizedForStreaming", false);
            put(
                p,
                "has64bitOffsets",
                int_value(p, "size").unwrap_or(0) > 2_i64.pow(32),
            );
        }
        if !matches!(p.get("hasThumbnail"),Some(Value::String(x))if x=="1")
            && !matches!(p.get("hasThumbnail"),Some(Value::Number(x))if x.as_i64()==Some(1))
            && !matches!(p.get("hasThumbnail"), Some(Value::Bool(true)))
        {
            p.shift_remove("hasThumbnail");
        }
        let packed = crate::catalog::unpack_part(p);
        if let Some(streams) = p.get_mut("Stream").and_then(Value::as_array_mut) {
            for stream in streams {
                let Some(s) = stream.as_object_mut() else {
                    continue;
                };
                for (flag, _) in TRACK_FLAGS
                    .iter()
                    .chain(std::iter::once(&("selected", "selected")))
                {
                    if s.get(*flag) == Some(&Value::Bool(false)) {
                        s.shift_remove(*flag);
                    }
                }
                if let Some(code) = str_value(s, "languageCode") {
                    for (k, v) in language_fields(Some(&Map::from_iter([(
                        String::from("language"),
                        Value::String(code),
                    )]))) {
                        s.insert(k, v);
                    }
                }
                if let Some(depth) = s.get("bitDepth").and_then(int_any) {
                    put(s, "bitDepth", depth);
                }
                if int_value(s, "streamType") == Some(1) {
                    // Python assigns the normalized value unconditionally for
                    // video streams, so an unknown profile remains an explicit
                    // JSON null rather than becoming an omitted attribute.
                    put(
                        s,
                        "profile",
                        video_profile(str_value(s, "profile").as_deref())
                            .map(Value::String)
                            .unwrap_or(Value::Null),
                    );
                    if int_value(s, "level") == Some(-99) {
                        s.shift_remove("level");
                    }
                }
                if int_value(s, "streamType") == Some(2) {
                    if let Some(codec) = str_value(s, "codec") {
                        let (codec, profile) =
                            audio_codec(Some(&codec), str_value(s, "profile").as_deref());
                        put(s, "codec", codec);
                        if let Some(profile) = profile {
                            put(s, "profile", profile);
                        } else {
                            s.shift_remove("profile");
                        }
                    }
                }
                if int_value(s, "streamType") == Some(3) {
                    s.shift_remove("embeddedInVideo");
                    if let Some(codec) = str_value(s, "codec") {
                        if let Some((_, mapped)) =
                            SUBTITLE_CODECS.iter().find(|(raw, _)| *raw == codec)
                        {
                            put(s, "codec", *mapped);
                        }
                    }
                    for flag in ["default", "forced"] {
                        if !bool_value(s, flag) {
                            s.shift_remove(flag);
                        }
                    }
                    put(s, "canAutoSync", false);
                }
            }
        }
        if packed {
            crate::catalog::repack_part(p);
        }
    }
    media.insert("Part".to_string(), Value::Array(parts));
}

/// The account's track settings (plex.tv `/api/v2/user` `profile`): what a real PMS
/// selects on every Part nobody chose tracks for.
#[derive(Clone, Debug, PartialEq)]
pub struct TrackPrefs {
    pub auto_audio: bool,
    pub audio: Option<String>,
    /// `autoSelectSubtitle`: 0 manually selected, 1 shown with foreign audio, 2 always.
    pub subtitles: i64,
    pub subtitle: Option<String>,
}

/// A stream's language as its tag's first subtag. A subtitle Plex's provider gave carries the
/// three letters as its tag (`eng`, measured on plexlab), read as `languages.json`'s `en`.
pub(crate) fn stream_language(stream: &Value) -> Option<String> {
    let tag = stream.get("languageTag").and_then(Value::as_str)?;
    let primary = tag.split('-').next().map(str::to_ascii_lowercase).filter(|code| !code.is_empty())?;
    match languages().get(&primary) {
        Some((_, short)) if primary.len() == 3 => Some(short.clone()),
        _ => Some(primary),
    }
}

/// An account's language setting as `stream_language` reads a stream's: `pt-BR` is `pt`.
pub(crate) fn setting_language(language: &str) -> String {
    language.split(['-', '_']).next().unwrap_or_default().to_ascii_lowercase()
}

/// Which audio and subtitle a real PMS selects for these streams. Measured on a real
/// 1.42.2 library under `autoSelectAudio` English and `autoSelectSubtitle` 2 English
/// (`tests/fixtures/track_selection/`): audio is the first track in the language, else
/// the first track -- not the one flagged default; the subtitle is the first one in the
/// language, else among all, that is neither flagged forced nor titled signs/songs,
/// else the first. A title saying "Forced" is not avoided; the forced flag is.
pub fn chosen_tracks(streams: &[Value], prefs: &TrackPrefs) -> (Option<i64>, Option<i64>) {
    let of = |kind: i64| {
        streams
            .iter()
            .filter(move |stream| stream.get("streamType").and_then(Value::as_i64) == Some(kind))
    };
    let id = |stream: &Value| stream.get("id").and_then(Value::as_i64);
    let wanted = |stream: &&Value, language: &Option<String>| {
        language.is_some() && stream_language(stream) == language.as_deref().map(setting_language)
    };
    let audio_track = prefs
        .auto_audio
        .then(|| of(2).find(|stream| wanted(stream, &prefs.audio)))
        .flatten()
        .or_else(|| of(2).next());
    let partial = |stream: &&Value| {
        stream.get("forced").and_then(Value::as_bool).unwrap_or(false)
            || stream.get("title").and_then(Value::as_str).is_some_and(|title| {
                let title = title.to_lowercase();
                title.contains("sign") || title.contains("song")
            })
    };
    let in_language: Vec<&Value> = of(3).filter(|stream| wanted(stream, &prefs.subtitle)).collect();
    // ponytail: modes 0 and 1 follow Plex's documented meaning, not a measurement (the
    // only account measured is on 2); measure them on plexscratch if anyone uses them.
    let pool: Vec<&Value> = match prefs.subtitles {
        2 if in_language.is_empty() => of(3).collect(),
        2 => in_language,
        1 if audio_track.and_then(stream_language) != prefs.subtitle.as_deref().map(setting_language) => in_language,
        _ => Vec::new(),
    };
    let subtitle = pool.iter().find(|stream| !partial(stream)).or(pool.first());
    (audio_track.and_then(id), subtitle.and_then(|stream| id(stream)))
}

/// Select the account's tracks on a Part nobody chose tracks for (`_trackChosen`, set by
/// `PUT /library/parts`). Answers whether anything changed.
pub fn select_tracks(part: &mut Value, prefs: &TrackPrefs) -> bool {
    if part.get("_trackChosen").and_then(Value::as_bool) == Some(true) {
        return false;
    }
    let packed = part.as_object_mut().is_some_and(crate::catalog::unpack_part);
    let changed = part.get_mut("Stream").and_then(Value::as_array_mut).is_some_and(|streams| select_in(streams, prefs));
    if packed {
        if let Some(object) = part.as_object_mut() {
            crate::catalog::repack_part(object);
        }
    }
    changed
}

fn select_in(streams: &mut [Value], prefs: &TrackPrefs) -> bool {
    let (audio, subtitle) = chosen_tracks(streams, prefs);
    let mut changed = false;
    for stream in streams.iter_mut() {
        let kind = stream.get("streamType").and_then(Value::as_i64);
        let want = match kind {
            Some(2) => audio,
            Some(3) => subtitle,
            _ => continue,
        };
        let is = stream.get("id").and_then(Value::as_i64) == want && want.is_some();
        let was = stream.get("selected").and_then(Value::as_bool) == Some(true);
        if is != was {
            changed = true;
            if let Some(object) = stream.as_object_mut() {
                if is {
                    object.insert("selected".into(), Value::Bool(true));
                } else {
                    object.shift_remove("selected");
                }
            }
        }
    }
    changed
}

/// `select_tracks` on every Part of a Media list (a row's `Media`, or `build_media`'s).
pub fn select_media_tracks(media: &mut Value, prefs: &TrackPrefs) -> bool {
    let mut changed = false;
    let rows = match media {
        Value::Array(rows) => rows.iter_mut().collect::<Vec<_>>(),
        other => vec![other],
    };
    for row in rows {
        for part in row.get_mut("Part").and_then(Value::as_array_mut).into_iter().flatten() {
            changed |= select_tracks(part, prefs);
        }
    }
    changed
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn chapters_match_measured_pms_boundaries_and_titles() {
        let fixture: Value = serde_json::from_str(include_str!("../../tests/fixtures/chapters/embedded.json")).unwrap();
        let mut actual = build_chapters(&fixture["probe"], 7);
        for (i, chapter) in actual.as_array_mut().unwrap().iter_mut().enumerate() {
            assert_eq!(chapter["id"], json!(70001 + i));
            chapter.as_object_mut().unwrap().shift_remove("id");
        }
        let rows = actual.as_array().unwrap();
        let expected = fixture["expected"].as_array().unwrap();
        assert_eq!(rows.len(), expected.len());
        for (i, (row, reference)) in rows.iter().zip(expected).enumerate() {
            assert_eq!(row["tag"], reference["tag"]);
            assert_eq!(row["index"], reference["index"]);
            // The real PMS boundaries differ
            // from this exact file's saved probe by up to 4ms. Preserve the
            // probe's millisecond precision rather than discard that precision.
            for (field, probe_field) in [("startTimeOffset", "start_time"), ("endTimeOffset", "end_time")] {
                let value = row[field].as_i64().unwrap();
                assert!((value - reference[field].as_i64().unwrap()).abs() <= 5);
                let seconds = fixture["probe"]["chapters"][i][probe_field].as_str().unwrap().parse::<f64>().unwrap();
                assert_eq!(value, (seconds * 1000.0).round() as i64);
            }
        }
    }
    #[test]
    fn chapters_validate_offsets_and_support_tick_timebases() {
        let probe = json!({"chapters": [
            {"start_time": "NaN", "end_time": "1"},
            {"start_time": "-1", "end_time": "1"},
            {"start_time": "2", "end_time": "2"},
            {"start_time": "0", "end_time": "inf"},
            {"start_time": "broken", "end_time": "1"},
            {"start": 90000, "end": 180000, "time_base": "1/90000"},
            {"start_time": "0", "end_time": "0.500", "tags": {"title": "Opening"}}
        ]});
        let chapters = build_chapters(&probe, 3);
        let rows = chapters.as_array().unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["index"], 1);
        assert_eq!(rows[0]["endTimeOffset"], 500);
        assert_eq!(rows[1]["startTimeOffset"], 1000);
        assert_eq!(rows[1]["endTimeOffset"], 2000);
        assert!(rows[1].get("tag").is_none());
        assert!(rows[0].get("thumb").is_none());
    }
    #[test]
    fn chapters_survive_media_build_and_normalization() {
        let probe = json!({"format": {}, "streams": [], "chapters": [
            {"start_time": "0", "end_time": "1"}]});
        let mut media = build_media(&probe, 1, 2, "/part", "/file.mkv", 0, Some("mkv"));
        assert_eq!(media["_chapters"][0]["endTimeOffset"], 1000);
        normalize_media(&mut media);
        assert_eq!(media["_chapters"][0]["id"], 20001);
        let missing = build_media(&json!({"format": {}, "streams": []}), 1, 2, "/part", "/file.mkv", 0, Some("mkv"));
        assert!(missing.get("_chapters").is_none());
    }
    #[test]
    fn chapters_projection_is_opt_in_and_stays_on_first_version() {
        let original = json!({"Media": [{"_chapters": [{"index": 1, "startTimeOffset": 0, "endTimeOffset": 100}]},
            {"_chapters": [{"index": 1, "startTimeOffset": 0, "endTimeOffset": 200}]}]});
        let mut shown = json!({});
        project_chapters(&original, &mut shown, false);
        assert_eq!(shown["chapterSource"], "media");
        assert!(shown.get("Chapter").is_none());
        project_chapters(&original, &mut shown, true);
        assert_eq!(shown["Chapter"][0]["endTimeOffset"], 100);
        project_chapters(&original, &mut shown, false);
        assert!(shown.get("Chapter").is_none());
        project_chapters(&json!({"Media": [{"_chapters": []}]}), &mut shown, true);
        assert!(shown.get("chapterSource").is_none());
    }
    #[test]
    fn resolution_boundaries() {
        assert_eq!(video_resolution(Some(360), Some(846)), Some("sd".into()));
        assert_eq!(video_resolution(Some(360), Some(848)), Some("480".into()));
    }
    #[test]
    fn frame_tokens() {
        assert_eq!(frame_rate_token(Some("24000/1001")), Some("24p".into()));
        assert_eq!(frame_rate_token(Some("30000/1001")), Some("NTSC".into()));
        assert_eq!(round_python(2.5), 2);
        assert_eq!(round_python(3.5), 4);
        assert_eq!(round_python(-2.5), -2);
    }
    #[test]
    fn language_is_measured() {
        let mut tags = map();
        put(&mut tags, "language", "fre");
        assert_eq!(
            str_value(&language_fields(Some(&tags)), "languageCode"),
            Some("fra".into())
        );
    }
}
