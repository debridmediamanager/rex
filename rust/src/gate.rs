//! Client-side direct-play gate measured from the official profile behavior.

use serde_json::{json, Map, Value};

pub const PROFILES: &[(&str, i64, i64, bool, bool)] = &[
    ("TV / 4K HEVC-capable", 2160, 80000, true, true),
    ("TV / 1080p h264-only", 1088, 20000, false, false),
    ("mobile over cellular", 720, 4000, true, false),
    ("kodi default (allow_hevc off)", 1088, 20000, false, false),
];
fn int(row: &Map<String, Value>, key: &str) -> i64 {
    row.get(key)
        .and_then(|v| v.as_i64().or_else(|| v.as_u64().map(|x| x as i64)))
        .unwrap_or(0)
}
fn numeric_frame_rate(row: &Map<String, Value>) -> f64 {
    let token = row
        .get("videoFrameRate")
        .map(|v| v.to_string().trim_matches('"').to_string())
        .unwrap_or_default();
    match token.as_str() {
        "24p" => 23.976,
        "PAL" => 25.0,
        "NTSC" => 29.97,
        "30p" => 30.0,
        "50p" => 50.0,
        "60p" => 59.94,
        _ => token.trim_end_matches('p').parse().unwrap_or(0.0),
    }
}
pub fn verdict(
    media: &Map<String, Value>,
    max_height: i64,
    max_bitrate: i64,
    allow_hevc: bool,
    supports_1080p60: bool,
) -> (bool, String) {
    let height = int(media, "height");
    let bitrate = int(media, "bitrate");
    let codec = media
        .get("videoCodec")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    let fps = numeric_frame_rate(media);
    if height > max_height {
        return (false, format!("height {} > max {}", height, max_height));
    }
    if bitrate > max_bitrate {
        return (
            false,
            format!("bitrate {} > max {} kbps", bitrate, max_bitrate),
        );
    }
    if !supports_1080p60 && fps > 30.0 && height >= 1080 {
        return (
            false,
            format!("{:.0} fps at {}p needs 1080p60 support", fps, height),
        );
    }
    if codec == "hevc" && !allow_hevc {
        return (false, "hevc disabled on this client".into());
    }
    if matches!(codec.as_str(), "vp8" | "vp9" | "av1") {
        return (
            false,
            format!("{} is not broadly direct-playable on Plex clients", codec),
        );
    }
    (true, "direct play".into())
}
pub fn verdict_value(
    media: &Value,
    max_height: i64,
    max_bitrate: i64,
    allow_hevc: bool,
    supports_1080p60: bool,
) -> Value {
    let Some(media) = media.as_object() else {
        return json!({"ok":false,"text":"invalid media"});
    };
    let (ok, text) = verdict(media, max_height, max_bitrate, allow_hevc, supports_1080p60);
    json!({"ok":ok,"text":text})
}
