//! Subtitle search and automatic subtitles: Plex's own provider and Stremio subtitle addons,
//! each candidate measured against the release and corrected before it is kept.
//!
//! Measured 2026-10-20 (`probes/subsbench/README.md`, `tests/fixtures/subtitles/`): Plex's first
//! result is in sync for about half the releases that have a readable candidate, and every
//! such release has a candidate in sync or fixed by a constant shift or a frame-rate ratio.
//! Coverage is no lever (the Stremio OpenSubtitles addon found no title Plex's provider
//! lacked), measuring is. So rex ranks the candidates by their release names, measures the
//! top few against the release's own embedded text track or its audio, keeps one in sync or
//! corrects one that is off, and stores it as UTF-8.

use super::upstream;
use super::*;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

// -- the meter (probes/subsbench/measure.py) ---------------------------------------------

/// One frame of the speech and cue masks, seconds.
pub(crate) const FRAME: f64 = 0.032;
/// One reference window, seconds.
pub(crate) const WIN: f64 = 90.0;
/// Where the windows sit, as fractions of the runtime.
pub(crate) const FRACTIONS: [f64; 6] = [0.1, 0.25, 0.4, 0.55, 0.7, 0.85];
const LAG: f64 = 30.0;
/// A joint peak this many deviations above the curve's mean is a reading.
const Z: f64 = 3.0;
/// A window agrees with the joint lag within this many seconds.
const AGREE: f64 = 0.5;
/// The frame-rate ratios a candidate is also tried under: a subtitle timed for 25 fps
/// against a 23.976 release drifts by 4% across the film.
pub(crate) const RATIOS: [(&str, f64); 9] = [
    ("1", 1.0),
    ("25/23.976", 25.0 / 23.976),
    ("23.976/25", 23.976 / 25.0),
    ("24/23.976", 24.0 / 23.976),
    ("23.976/24", 23.976 / 24.0),
    ("25/24", 25.0 / 24.0),
    ("24/25", 24.0 / 25.0),
    ("30/29.97", 30.0 / 29.97),
    ("29.97/30", 29.97 / 30.0),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Verdict {
    InSync,
    Shift,
    Fps,
    Broken,
    Unknown,
}

impl Verdict {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Verdict::InSync => "in_sync",
            Verdict::Shift => "shift",
            Verdict::Fps => "fps",
            Verdict::Broken => "broken",
            Verdict::Unknown => "unknown",
        }
    }
}

/// What the meter read off one candidate: its verdict, the frame-rate ratio and the lag
/// (positive: the subtitle is late) at that ratio.
#[derive(Clone, Debug)]
pub(crate) struct Reading {
    pub verdict: Verdict,
    pub ratio: &'static str,
    pub factor: f64,
    pub offset: f64,
    pub z: f64,
}

impl Reading {
    fn unknown() -> Self {
        Reading { verdict: Verdict::Unknown, ratio: "1", factor: 1.0, offset: 0.0, z: 0.0 }
    }
}

/// Python's `round(x, digits)` for the meter's values, which never sit on a tie.
fn rounded(value: f64, digits: i32) -> f64 {
    let scale = 10f64.powi(digits);
    (value * scale).round() / scale
}

/// The correlation of `cues`' mask against one window's speech for every lag within LAG
/// (index k is a lag of k - pad frames), or None when the window holds too few cues or too
/// little speech to say anything. The mask and the speech are +-1 encoded, as ffsubsync's are.
/// Computed exactly from the cue mask's runs and the speech's prefix sums: measure.py's FFT
/// gives the same integers to float noise, and rex keeps its crate set.
fn lag_curve(speech: &[u8], cues: &[(f64, f64)], start: f64) -> Option<Vec<f64>> {
    let n = speech.len();
    let spoken = speech.iter().filter(|frame| **frame != 0).count();
    if n == 0 || (spoken as f64 / n as f64) < 0.02 {
        return None;
    }
    let pad = (LAG / FRAME) as usize;
    let total = n + 2 * pad;
    let mut mask = vec![false; total];
    let lo = start - LAG;
    let mut covered = 0;
    for &(a, b) in cues {
        if b < lo || a > start + WIN + LAG {
            continue;
        }
        let i = (((a - lo) / FRAME) as i64).max(0) as usize;
        let j = (((b - lo) / FRAME) as i64).clamp(0, total as i64) as usize;
        if i < j {
            mask[i..j].iter_mut().for_each(|frame| *frame = true);
        }
        if a < start + WIN && b > start {
            covered += 1;
        }
    }
    if covered < 3 {
        return None;
    }
    let mut prefix = Vec::with_capacity(n + 1);
    prefix.push(0_i64);
    for frame in speech {
        prefix.push(prefix.last().copied().unwrap_or(0) + i64::from(*frame != 0));
    }
    let mut runs = Vec::new();
    let mut at = 0;
    while at < total {
        if mask[at] {
            let begin = at;
            while at < total && mask[at] {
                at += 1;
            }
            runs.push((begin as i64, at as i64));
        } else {
            at += 1;
        }
    }
    // r[k] = sum_t c[t+k] s[t] = 4 A(k) - 2 M(k) - 2 V + n, with A the speech the mask covers
    // at lag k and M the mask's frames inside the window.
    let lags = (2 * pad + 1) as i64;
    let (n_i, voiced) = (n as i64, prefix[n]);
    let mut curve = vec![n_i - 2 * voiced; lags as usize];
    for (begin, end) in runs {
        // The run reaches the window only for lags in (begin - n, end).
        for k in (begin - n_i + 1).max(0)..end.min(lags) {
            let from = if k <= begin { begin - k } else { 0 };
            let to = if k >= end - n_i { end - k } else { n_i };
            curve[k as usize] += 4 * (prefix[to as usize] - prefix[from as usize]) - 2 * (to - from);
        }
    }
    Some(curve.into_iter().map(|value| value as f64).collect())
}

/// The curve's peak: its lag in seconds and how many deviations it stands above the mean.
fn peak(curve: &[f64]) -> (f64, f64) {
    let pad = (curve.len() - 1) / 2;
    let mut best = 0;
    for (index, value) in curve.iter().enumerate() {
        if *value > curve[best] {
            best = index;
        }
    }
    let mean = curve.iter().sum::<f64>() / curve.len() as f64;
    let deviation = (curve.iter().map(|value| (value - mean).powi(2)).sum::<f64>() / curve.len() as f64).sqrt();
    let deviation = if deviation == 0.0 { 1.0 } else { deviation };
    (rounded((best as f64 - pad as f64) * FRAME, 2), rounded((curve[best] - mean) / deviation, 1))
}

/// measure.py's `measure`: each window's lag curve summed into one joint curve per frame-rate
/// ratio. The ratio whose joint peak stands highest wins (1, the true rate, by a 10% margin);
/// a window agrees when its own peak sits within AGREE of the joint one. A reading needs a
/// joint peak Z deviations up and three agreeing windows, at least half of those that say
/// anything; otherwise the windows disagree (`broken`, another cut) or nothing is read.
pub(crate) fn measure(windows: &[(f64, Vec<u8>)], cues: &[(f64, f64)]) -> Reading {
    let mut best: Option<(Reading, usize, usize)> = None;
    for (name, factor) in RATIOS {
        let scaled = cues.iter().map(|(a, b)| (a * factor, b * factor)).collect::<Vec<_>>();
        let curves = windows.iter().map(|(start, speech)| lag_curve(speech, &scaled, *start)).collect::<Vec<_>>();
        let used = curves.iter().flatten().collect::<Vec<_>>();
        let Some(first) = used.first() else { continue };
        let mut joint = vec![0.0; first.len()];
        for curve in &used {
            joint.iter_mut().zip(curve.iter()).for_each(|(sum, value)| *sum += value);
        }
        let (lag, z) = peak(&joint);
        let agree = used.iter().filter(|curve| (peak(curve).0 - lag).abs() <= AGREE).count();
        let better = match &best {
            None => true,
            Some((reading, _, _)) => z > reading.z * if reading.ratio == "1" { 1.1 } else { 1.0 },
        };
        if better {
            best = Some((Reading { verdict: Verdict::Unknown, ratio: name, factor, offset: lag, z }, used.len(), agree));
        }
    }
    let Some((mut reading, windows, agree)) = best else {
        return Reading::unknown();
    };
    reading.verdict = if reading.z < Z || agree < 3 {
        Verdict::Unknown
    } else if agree * 2 < windows {
        Verdict::Broken
    } else if reading.ratio != "1" {
        Verdict::Fps
    } else if reading.offset.abs() <= 0.5 {
        Verdict::InSync
    } else {
        Verdict::Shift
    };
    reading
}

/// variant.py's `energy_mask`, the detector rex can run with no model: 16 kHz mono PCM that
/// FFmpeg band-passed to 300-3400 Hz, RMS per 512 samples (32 ms) in dB, speech where it is
/// above the window's median. It decides about half as often as Silero and agrees with it
/// when it does (README, "What it found").
pub(crate) fn energy_mask(pcm: &[i16]) -> Vec<u8> {
    let decibels = pcm
        .chunks_exact(512)
        .map(|frame| {
            let power = frame.iter().map(|sample| f64::from(*sample).powi(2)).sum::<f64>() / 512.0;
            20.0 * (power.sqrt() + 1.0).log10()
        })
        .collect::<Vec<_>>();
    if decibels.is_empty() {
        return Vec::new();
    }
    // numpy's percentile: linear between the two closest ranks.
    let mut sorted = decibels.clone();
    sorted.sort_by(|a, b| a.total_cmp(b));
    let rank = 0.5 * (sorted.len() - 1) as f64;
    let (low, high) = (rank.floor() as usize, rank.ceil() as usize);
    let median = sorted[low] + (sorted[high] - sorted[low]) * (rank - low as f64);
    decibels.iter().map(|value| u8::from(*value > median)).collect()
}

/// A text track's cues as the same mask over a window: what the meter reads the candidates
/// against when the release carries a text subtitle of its own. Aligning against it agreed
/// with the audio meter on 20 of 20 in sync, 4 of 4 shifts and 4 of 6 frame-rate errors.
pub(crate) fn cue_mask(cues: &[(f64, f64)], start: f64) -> Vec<u8> {
    let frames = (WIN / FRAME) as usize;
    let mut mask = vec![0_u8; frames];
    for &(a, b) in cues {
        if b <= start || a >= start + WIN {
            continue;
        }
        let i = (((a - start) / FRAME) as i64).max(0) as usize;
        let j = (((b - start) / FRAME) as i64).clamp(0, frames as i64) as usize;
        if i < j {
            mask[i..j].iter_mut().for_each(|frame| *frame = 1);
        }
    }
    mask
}

/// Where the six windows start for a runtime of `duration_ms`: measure.py's
/// `round(duration * fraction, 1)`.
pub(crate) fn window_starts(duration_ms: i64) -> Vec<f64> {
    FRACTIONS.iter().map(|fraction| rounded(duration_ms as f64 / 1000.0 * fraction, 1)).collect()
}

// -- text ------------------------------------------------------------------------------

/// Windows-1252's 0x80-0x9F; the five holes keep their C1 code points, as Windows maps them.
const CP1252_HIGH: [char; 32] = [
    '\u{20AC}', '\u{81}', '\u{201A}', '\u{192}', '\u{201E}', '\u{2026}', '\u{2020}', '\u{2021}',
    '\u{2C6}', '\u{2030}', '\u{160}', '\u{2039}', '\u{152}', '\u{8D}', '\u{17D}', '\u{8F}',
    '\u{90}', '\u{2018}', '\u{2019}', '\u{201C}', '\u{201D}', '\u{2022}', '\u{2013}', '\u{2014}',
    '\u{2DC}', '\u{2122}', '\u{161}', '\u{203A}', '\u{153}', '\u{9D}', '\u{17E}', '\u{178}',
];

fn cp1252_char(byte: u8) -> char {
    match byte {
        0x80..=0x9F => CP1252_HIGH[(byte - 0x80) as usize],
        other => other as char,
    }
}

fn cp1252_byte(character: char) -> Option<u8> {
    match character as u32 {
        code @ (0..=0x7F | 0xA0..=0xFF) => Some(code as u8),
        _ => CP1252_HIGH.iter().position(|high| *high == character).map(|at| 0x80 + at as u8),
    }
}

/// measure.py's count of `Ã[\x80-\xbf]|â€|â™`: UTF-8 read once more as Windows-1252.
pub(crate) fn mojibake(text: &str) -> usize {
    let characters = text.chars().collect::<Vec<_>>();
    let (mut count, mut at) = (0, 0);
    while at + 1 < characters.len() {
        let next = characters[at + 1];
        let hit = match characters[at] {
            'Ã' => ('\u{80}'..='\u{BF}').contains(&next),
            'â' => next == '€' || next == '™',
            _ => false,
        };
        if hit {
            count += 1;
            at += 2;
        } else {
            at += 1;
        }
    }
    count
}

/// Each run of characters that is UTF-8 read as Windows-1252 read back as UTF-8: a lead byte's
/// character followed by its continuation bytes' characters. Text in any other script, and a
/// real `Ã` before an ordinary letter, are left as they are.
fn unmangle(text: &str) -> String {
    let characters = text.chars().collect::<Vec<_>>();
    let mut out = String::with_capacity(text.len());
    let mut at = 0;
    while at < characters.len() {
        let follow = match cp1252_byte(characters[at]) {
            Some(0xC2..=0xDF) => 1,
            Some(0xE0..=0xEF) => 2,
            Some(0xF0..=0xF4) => 3,
            _ => 0,
        };
        let bytes = characters
            .get(at..=at + follow)
            .filter(|_| follow > 0)
            .and_then(|run| run.iter().map(|character| cp1252_byte(*character)).collect::<Option<Vec<_>>>())
            .filter(|bytes| bytes[1..].iter().all(|byte| (0x80..=0xBF).contains(byte)));
        match bytes.as_deref().and_then(|bytes| std::str::from_utf8(bytes).ok()) {
            Some(decoded) => {
                out.push_str(decoded);
                at += follow + 1;
            }
            None => {
                out.push(characters[at]);
                at += 1;
            }
        }
    }
    out
}

/// A downloaded subtitle as UTF-8 text, and how much mojibake it arrived with. A BOM is
/// dropped; bytes that are not UTF-8 are Windows-1252; provider files that are UTF-8 read
/// once as Windows-1252 and saved again (`â™ª` for `♪`, measured 2026-10-20) are put back
/// when that leaves less of it.
pub(crate) fn decode_text(raw: &[u8]) -> (String, usize) {
    let raw = raw.strip_prefix(b"\xEF\xBB\xBF").unwrap_or(raw);
    let mut text = match std::str::from_utf8(raw) {
        Ok(text) => text.to_string(),
        Err(_) => raw.iter().map(|byte| cp1252_char(*byte)).collect(),
    };
    let found = mojibake(&text);
    // Twice: a file mangled twice over reads right after the second pass.
    for _ in 0..2 {
        let repaired = unmangle(&text);
        if mojibake(&repaired) >= mojibake(&text) {
            break;
        }
        text = repaired;
    }
    (text, found)
}

fn time_pattern() -> &'static regex::Regex {
    static PATTERN: OnceLock<regex::Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        regex::Regex::new(r"(?:(\d+):)?(\d{1,2}):(\d{2})([,.])(\d{1,3})(\s*-->\s*)(?:(\d+):)?(\d{1,2}):(\d{2})([,.])(\d{1,3})")
            .expect("the timing pattern compiles")
    })
}

/// `h:mm:ss,fff` to seconds. The fraction is read as written (`,05` is 50 ms): measure.py's
/// `int(str(int(g)).ljust(3, '0'))` read `,050` as 500 ms.
fn seconds(hours: Option<&str>, minutes: &str, secs: &str, fraction: &str) -> f64 {
    let number = |text: &str| text.parse::<f64>().unwrap_or(0.0);
    let millis = format!("{fraction:0<3}");
    hours.map_or(0.0, number) * 3600.0 + number(minutes) * 60.0 + number(secs) + number(&millis[..3]) / 1000.0
}

fn is_ass(text: &str) -> bool {
    !text.contains("-->") && text.lines().any(|line| line.trim_start().starts_with("Dialogue:"))
}

/// An ASS `Dialogue:` line's start and end fields, with where they are.
fn ass_times(line: &str) -> Option<(f64, f64)> {
    let rest = line.trim_start().strip_prefix("Dialogue:")?;
    let mut fields = rest.splitn(4, ',');
    let (_, start, end) = (fields.next()?, fields.next()?, fields.next()?);
    let parse = |stamp: &str| {
        let mut parts = stamp.trim().split(':');
        let (hours, minutes, rest) = (parts.next()?, parts.next()?, parts.next()?);
        let (secs, fraction) = rest.split_once('.')?;
        Some(seconds(Some(hours), minutes, secs, &format!("{fraction:0<2}")[..2]) + 0.0)
    };
    Some((parse(start)?, parse(end)?))
}

/// The cues of an SRT, WebVTT or ASS file, in seconds.
pub(crate) fn cues(text: &str) -> Vec<(f64, f64)> {
    if is_ass(text) {
        return text.lines().filter_map(ass_times).filter(|(a, b)| b > a).collect();
    }
    time_pattern()
        .captures_iter(text)
        .filter_map(|caps| {
            let a = seconds(caps.get(1).map(|m| m.as_str()), &caps[2], &caps[3], &caps[5]);
            let b = seconds(caps.get(7).map(|m| m.as_str()), &caps[8], &caps[9], &caps[11]);
            (b > a).then_some((a, b))
        })
        .collect()
}

/// Every timestamp of the file put right: scaled by the frame-rate ratio, then moved back by
/// the lag the meter read at that ratio. A time before the start is the start.
pub(crate) fn retime(text: &str, factor: f64, offset: f64) -> String {
    let moved = |time: f64| ((time * factor - offset).max(0.0) * 1000.0).round() as i64;
    if is_ass(text) {
        return text
            .split_inclusive('\n')
            .map(|line| {
                let Some((start, end)) = ass_times(line) else { return line.to_string() };
                let stamp = |ms: i64| format!("{}:{:02}:{:02}.{:02}", ms / 3_600_000, ms / 60_000 % 60, ms / 1000 % 60, ms % 1000 / 10);
                let (head, rest) = line.split_once("Dialogue:").unwrap_or(("", line));
                let mut fields = rest.splitn(4, ',');
                let layer = fields.next().unwrap_or_default();
                let (_, _, tail) = (fields.next(), fields.next(), fields.next().unwrap_or_default());
                format!("{head}Dialogue:{layer},{},{},{tail}", stamp(moved(start)), stamp(moved(end)))
            })
            .collect();
    }
    time_pattern()
        .replace_all(text, |caps: &regex::Captures| {
            let stamp = |ms: i64, separator: &str| {
                format!("{:02}:{:02}:{:02}{separator}{:03}", ms / 3_600_000, ms / 60_000 % 60, ms / 1000 % 60, ms % 1000)
            };
            let a = seconds(caps.get(1).map(|m| m.as_str()), &caps[2], &caps[3], &caps[5]);
            let b = seconds(caps.get(7).map(|m| m.as_str()), &caps[8], &caps[9], &caps[11]);
            format!("{}{}{}", stamp(moved(a), &caps[4]), &caps[6], stamp(moved(b), &caps[10]))
        })
        .into_owned()
}

// -- the OpenSubtitles hash ---------------------------------------------------------------

/// The hash Plex's provider and the addons match a release by: its size plus the 64-bit
/// little-endian words of its first and last 64 KiB, wrapping (candidates.py, which agreed
/// with the PMS's own `openSubtitlesHash` for plexlab's Sintel).
pub(crate) fn opensubtitles_hash(size: u64, head: &[u8], tail: &[u8]) -> String {
    let mut hash = size;
    for chunk in [head, tail] {
        for word in chunk[..chunk.len().min(65_536)].chunks_exact(8) {
            hash = hash.wrapping_add(u64::from_le_bytes(word.try_into().unwrap_or([0; 8])));
        }
    }
    format!("{hash:016x}")
}

// -- languages -----------------------------------------------------------------------------

/// The `lang` Plex's provider is asked with for a client's `language`: measured, `en` and
/// `pt-BR` answer (and `pt` 500'd once), so Brazilian Portuguese keeps its region and every
/// other language is its two letters.
pub(crate) fn provider_language(language: &str) -> String {
    let primary = language.split(['-', '_']).next().unwrap_or_default().to_ascii_lowercase();
    if primary == "pt" && language.to_ascii_lowercase().ends_with("br") {
        return "pt-BR".to_string();
    }
    primary
}

/// The ISO 639-2 code a Stremio addon lists a language under: `pob` for Brazilian Portuguese
/// (the OpenSubtitles addon's own code), else the three letters of `languages.json`.
pub(crate) fn addon_language(language: &str) -> String {
    let language = provider_language(language);
    if language == "pt-BR" {
        return "pob".to_string();
    }
    crate::mediainfo::language_code_of_tag(&language).unwrap_or(language)
}

/// Whether an addon's `lang` is the one wanted (`addon_language`'s), in either of ISO 639-2's
/// spellings (`ger` and `deu`).
pub(crate) fn addon_language_matches(listed: &str, wanted: &str) -> bool {
    let listed = listed.to_ascii_lowercase();
    listed == wanted || crate::mediainfo::terminology(&listed) == wanted
}

/// A Stream row's language fields for a provider's language code. Measured on plexlab: the
/// provider's `en` is the row's `languageCode` and `languageTag` `eng`, `language` English.
pub(crate) fn row_language(language: &str) -> (String, String) {
    let primary = provider_language(language);
    let primary = primary.split('-').next().unwrap_or_default();
    let code = crate::mediainfo::language_code_of_tag(primary).unwrap_or_else(|| primary.to_string());
    let name = crate::mediainfo::language_name(&code).unwrap_or_else(|| code.clone());
    (code, name)
}

// -- ranking (probes/subsbench/analyze.py) -------------------------------------------------

/// One subtitle a source offered for a release.
#[derive(Clone, Debug, Default)]
pub(crate) struct Candidate {
    /// `plex`, or the addon's base url.
    pub source: String,
    /// What Plex Web shows as its provider: `OpenSubtitles` for Plex's, an addon's own name.
    pub provider: String,
    /// Where it is: Plex's `/library/streams/{id}`, an addon's download url.
    pub locator: String,
    pub release: String,
    pub file: String,
    pub title: String,
    pub score: Option<Value>,
    pub hash_match: bool,
    pub fps_milli: Option<i64>,
    pub hearing_impaired: bool,
    pub codec: String,
    /// The provider's language code (`en`, `pt-BR`).
    pub language: String,
}

const NOISE: [&str; 16] = ["mkv", "mp4", "srt", "avi", "the", "and", "of", "a", "eng", "en", "por", "pt", "br", "pob", "subs", "sub"];
const SOURCES: [&str; 10] = ["bluray", "bdrip", "brrip", "web", "webrip", "webdl", "hdtv", "dvdrip", "remux", "hdrip"];
const SERVICES: [&str; 10] = ["amzn", "nf", "dsnp", "hmax", "atvp", "pcok", "hulu", "max", "pmtp", "cr"];

fn tokens(text: &str) -> HashSet<String> {
    let lower = text.to_lowercase().replace("web-dl", "webdl").replace("web dl", "webdl");
    lower
        .split(|character: char| !(character.is_ascii_lowercase() || character.is_ascii_digit()))
        .filter(|token| !token.is_empty() && !NOISE.contains(token))
        .map(str::to_string)
        .collect()
}

/// The release group: what follows the last `-` before the extension.
fn group(text: &str) -> Option<String> {
    static TRAILER: OnceLock<regex::Regex> = OnceLock::new();
    static GROUP: OnceLock<regex::Regex> = OnceLock::new();
    let trailer = TRAILER.get_or_init(|| regex::Regex::new(r"\.(srt|mkv|mp4|ass)$").expect("compiles"));
    let pattern = GROUP.get_or_init(|| regex::Regex::new(r"-([A-Za-z0-9]+)(?:\[[^\]]*\])?(?:\.[a-z]{2,4})?$").expect("compiles"));
    let stem = trailer.replace(text.trim(), "");
    pattern.captures(&stem).map(|caps| caps[1].to_lowercase())
}

fn similarity(file: &str, release: &str) -> f64 {
    let (a, b) = (tokens(file), tokens(release));
    let union = a.union(&b).count().max(1);
    let mut score = a.intersection(&b).count() as f64 / union as f64;
    if group(file).is_some_and(|mine| Some(mine) == group(release)) {
        score += 1.0;
    }
    let set = |tokens: &HashSet<String>, of: &[&str]| tokens.iter().filter(|token| of.contains(&token.as_str())).cloned().collect::<HashSet<_>>();
    for (of, weight) in [(&SOURCES[..], 0.5), (&SERVICES[..], 0.3)] {
        let (sa, sb) = (set(&a, of), set(&b, of));
        if !sa.is_empty() && !sb.is_empty() {
            score += if sa.intersection(&sb).next().is_some() { weight } else { -weight };
        }
    }
    score
}

/// analyze.py's `rex_rank` key: how alike the names are, a hash match, a frame rate that is
/// not the release's, mojibake and hearing-impaired lines.
pub(crate) fn rank_score(file: &str, frame_rate: Option<&str>, candidate: &Candidate, mojibake: usize) -> f64 {
    let mut score = similarity(file, &candidate.release);
    if !candidate.file.is_empty() {
        score = score.max(similarity(file, &candidate.file));
    }
    if candidate.hash_match {
        score += 3.0;
    }
    let want = match frame_rate {
        Some("24p") => Some(23_976),
        Some("PAL") => Some(25_000),
        Some("NTSC") => Some(29_970),
        _ => None,
    };
    if let (Some(want), Some(fps)) = (want, candidate.fps_milli.filter(|fps| *fps != 0)) {
        if (fps - want).abs() > 100 {
            score -= 2.0;
        }
    }
    if mojibake > 3 {
        score -= 1.0;
    }
    if candidate.hearing_impaired {
        score -= 0.1;
    }
    score
}

/// The candidates' indexes, best first; ties keep the sources' order.
pub(crate) fn rank(file: &str, frame_rate: Option<&str>, candidates: &[Candidate], mojibake: &dyn Fn(usize) -> usize) -> Vec<usize> {
    let scores = (0..candidates.len()).map(|at| rank_score(file, frame_rate, &candidates[at], mojibake(at))).collect::<Vec<_>>();
    let mut order = (0..candidates.len()).collect::<Vec<_>>();
    order.sort_by(|a, b| scores[*b].total_cmp(&scores[*a]));
    order
}

/// How many of the best-ranked candidates are downloaded and measured: five found 56 of 75
/// English and 32 of 47 Brazilian releases a good subtitle, eight 57 and 34 (the corpus).
pub(crate) const MEASURED: usize = 5;

/// A correction: scale by the ratio, then take the lag off.
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Fix {
    pub ratio: &'static str,
    pub factor: f64,
    pub offset: f64,
}

impl Fix {
    fn of(reading: &Reading) -> Option<Fix> {
        matches!(reading.verdict, Verdict::Shift | Verdict::Fps).then(|| Fix { ratio: reading.ratio, factor: reading.factor, offset: reading.offset })
    }
}

/// rex's pick: the best-ranked candidates measured one at a time (`read` downloads and
/// measures one, answering its reading and mojibake, None when it cannot be had), ranked again
/// now their mojibake is known; the first in sync, else the first a shift or a frame-rate
/// ratio fixes, corrected, else the first. `top` is 1 when there is nothing to measure against.
pub(crate) fn pick(
    file: &str,
    frame_rate: Option<&str>,
    candidates: &[Candidate],
    top: usize,
    mut read: impl FnMut(usize) -> Option<(Reading, usize)>,
) -> Option<(usize, Option<Fix>)> {
    let mut measured = Vec::new();
    for at in rank(file, frame_rate, candidates, &|_| 0) {
        if measured.len() == top {
            break;
        }
        if let Some((reading, garbled)) = read(at) {
            measured.push((at, reading, garbled));
        }
    }
    let garbled = measured.iter().map(|(at, _, garbled)| (*at, *garbled)).collect::<HashMap<_, _>>();
    let order = rank(file, frame_rate, candidates, &|at| garbled.get(&at).copied().unwrap_or(0));
    let measured = order.iter().filter_map(|at| measured.iter().find(|entry| entry.0 == *at)).collect::<Vec<_>>();
    if let Some((at, _, _)) = measured.iter().find(|entry| entry.1.verdict == Verdict::InSync) {
        return Some((*at, None));
    }
    if let Some((at, reading, _)) = measured.iter().find(|entry| Fix::of(&entry.1).is_some()) {
        return Some((*at, Fix::of(reading)));
    }
    measured.first().map(|(at, _, _)| (*at, None))
}

// -- the sources ---------------------------------------------------------------------------

const PROVIDER: &str = "https://metadata.provider.plex.tv";
/// A search that found nothing is not run again for an item for a day.
const MISS_FOR: Duration = Duration::from_secs(86_400);
/// How long a playback decision waits for its item's subtitle job.
const DECISION_WAIT: Duration = Duration::from_secs(3);
/// The largest subtitle file kept.
const MAX_FILE: usize = 16 * 1024 * 1024;

/// The Stremio subtitle addons `REX_SUBTITLE_ADDONS` names: comma-separated base urls, an
/// addon's manifest url with or without its `/manifest.json`.
pub(crate) fn addons() -> Vec<String> {
    std::env::var("REX_SUBTITLE_ADDONS")
        .unwrap_or_default()
        .split(',')
        .map(|base| base.trim().trim_end_matches("/manifest.json").trim_end_matches('/').to_string())
        .filter(|base| base.starts_with("http://") || base.starts_with("https://"))
        .collect()
}

/// Who asks Plex's provider: this server, with the claim's server token.
#[derive(Clone)]
pub(crate) struct Agent {
    token: String,
    machine: String,
    name: String,
    version: String,
}

impl std::fmt::Debug for Agent {
    // The token is an account credential; it never reaches a debug print.
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Agent").field("machine", &self.machine).finish()
    }
}

impl Agent {
    /// The headers a claimed PMS asks the provider with, in its order (plexlab's outbound
    /// capture in `plex_search_apply_s143.json`), as rex names itself to plex.tv elsewhere.
    fn headers(&self) -> Vec<(&'static str, String)> {
        vec![
            ("User-Agent", format!("PlexMediaServer/{}", self.version)),
            ("Accept-Encoding", "gzip".to_string()),
            ("Accept", "application/json".to_string()),
            ("X-Plex-Client-Identifier", self.machine.clone()),
            ("X-Plex-Version", self.version.clone()),
            ("X-Plex-Provides", "server".to_string()),
            ("X-Plex-Platform", "Linux".to_string()),
            ("X-Plex-Product", "Plex Media Server".to_string()),
            ("X-Plex-Platform-Version", "6.1".to_string()),
            ("X-Plex-Device-Name", self.name.clone()),
            ("X-Plex-Device", "PC".to_string()),
            ("X-Plex-Model", "x86_64".to_string()),
            ("X-Plex-Device-Vendor", "Docker".to_string()),
            // The recorded PMS sent `X-Plex-Api-Token`, a credential rex does not hold: the claim's
            // server token is refused there ("Invalid api token", 401) and taken as `X-Plex-Token`
            // (measured 2026-10-20 on the search and the download alike).
            ("X-Plex-Token", self.token.clone()),
        ]
    }
}

/// Where a Part's bytes are, and what the searches and the meter need to know of it.
#[derive(Clone, Debug, Default)]
pub(crate) struct PartInfo {
    pub id: String,
    url: String,
    auth: Option<String>,
    size: u64,
    /// The file's name, as the provider and the addons are told it.
    pub file: String,
    duration: i64,
    pub frame_rate: Option<String>,
    video_codec: String,
    resolution: String,
    /// The ffprobe indexes of the embedded text subtitles the meter can read the timing of.
    text_tracks: Vec<i64>,
    /// The OpenSubtitles hash kept on the Part (`_osHash`).
    hash: Option<String>,
}

/// What a search asks about one release.
#[derive(Clone, Debug)]
pub(crate) struct Search {
    language: String,
    agent: Option<Agent>,
    addons: Vec<String>,
    guid: Option<String>,
    episode: bool,
    year: String,
    /// The addons' type and id: `movie`/`tt...`, or `series`/`tt...:season:episode`.
    stremio: Option<(String, String)>,
    pub part: PartInfo,
}

impl Candidate {
    fn to_value(&self) -> Value {
        json!({"source": self.source, "provider": self.provider, "locator": self.locator, "release": self.release,
               "file": self.file, "title": self.title, "score": self.score, "hashMatch": self.hash_match,
               "fpsMilli": self.fps_milli, "hearingImpaired": self.hearing_impaired, "codec": self.codec,
               "language": self.language})
    }

    fn from_value(value: &Value) -> Candidate {
        let text = |name: &str| value.get(name).and_then(Value::as_str).unwrap_or_default().to_string();
        Candidate {
            source: text("source"),
            provider: text("provider"),
            locator: text("locator"),
            release: text("release"),
            file: text("file"),
            title: text("title"),
            score: value.get("score").filter(|score| !score.is_null()).cloned(),
            hash_match: value.get("hashMatch").and_then(Value::as_bool).unwrap_or(false),
            fps_milli: value.get("fpsMilli").and_then(Value::as_i64),
            hearing_impaired: value.get("hearingImpaired").and_then(Value::as_bool).unwrap_or(false),
            codec: text("codec"),
            language: text("language"),
        }
    }
}

fn encode(pairs: &[(&str, String)]) -> String {
    pairs.iter().map(|(name, value)| format!("{name}={}", upstream::quote_plus(value))).collect::<Vec<_>>().join("&")
}

/// A GET one at a time per host, a 429's `Retry-After` waited out: Plex's provider downloads and
/// the Stremio addon both answered 429 to parallel requests (measured 2026-10-20).
fn polite(url: &str, headers: &[(&str, String)]) -> Result<(u16, Vec<u8>), String> {
    static HOSTS: OnceLock<Mutex<HashMap<String, Arc<Mutex<()>>>>> = OnceLock::new();
    let host = url.split("://").nth(1).unwrap_or(url).split('/').next().unwrap_or_default().to_string();
    let lane = HOSTS.get_or_init(Default::default).lock().unwrap_or_else(PoisonError::into_inner).entry(host).or_default().clone();
    let _turn = lane.lock().unwrap_or_else(PoisonError::into_inner);
    for attempt in 0..4_u32 {
        let (status, answer, body) = upstream::fetch("GET", url, headers, b"", Duration::from_secs(30))?;
        if status != 429 {
            let gzipped = answer.iter().any(|(name, value)| name.eq_ignore_ascii_case("content-encoding") && value.contains("gzip"));
            return Ok((status, if gzipped || body.starts_with(&[0x1F, 0x8B]) { gunzip(&body)? } else { body }));
        }
        let retry = answer.iter().find(|(name, _)| name.eq_ignore_ascii_case("retry-after")).and_then(|(_, value)| value.trim().parse::<u64>().ok());
        thread::sleep(Duration::from_secs(retry.unwrap_or(5 << attempt).min(60)));
    }
    Err("429 after four tries".to_string())
}

fn gunzip(body: &[u8]) -> Result<Vec<u8>, String> {
    use std::io::Read as _;
    let mut out = Vec::new();
    flate2::read::GzDecoder::new(body).take(MAX_FILE as u64 + 1).read_to_end(&mut out).map_err(|error| error.to_string())?;
    Ok(out)
}

fn hash_of(part: &PartInfo) -> Option<String> {
    if part.hash.is_some() {
        return part.hash.clone();
    }
    let length = part.size.min(65_536) as usize;
    if length == 0 {
        return None;
    }
    let read = |offset| super::universal::read_range(&part.url, part.auth.as_deref(), offset, length).ok();
    Some(opensubtitles_hash(part.size, &read(0)?, &read(part.size - length as u64)?))
}

/// Plex's own search, the request a claimed PMS makes (`plex_search_apply_s143.json`'s
/// outbound): `plexHash` left out, which still answers (measured 2026-10-20).
fn provider_candidates(search: &Search, hash: Option<&str>) -> Vec<Candidate> {
    let Some(agent) = &search.agent else { return Vec::new() };
    let mut query = vec![("filename", search.part.file.clone())];
    query.extend(search.guid.clone().map(|guid| ("guid", guid)));
    query.push(("lang", provider_language(&search.language)));
    query.extend(hash.map(|hash| ("openSubtitlesHash", hash.to_string())));
    query.push(("type", if search.episode { "4" } else { "1" }.to_string()));
    query.push(("videoCodec", search.part.video_codec.clone()));
    let resolution = &search.part.resolution;
    let numeric = !resolution.is_empty() && resolution.bytes().all(|byte| byte.is_ascii_digit());
    query.push(("videoQuality", if numeric { format!("{resolution}p") } else { resolution.clone() }));
    query.push(("year", search.year.clone()));
    let url = super::plextv::plextv_url(&format!("{PROVIDER}/library/streams/matches?{}", encode(&query)));
    let answer = match polite(&url, &agent.headers()) {
        Ok((200, body)) => serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null),
        Ok((status, _)) => {
            warning!("SUBTITLE Plex's provider answered {status} for {} ({})", search.part.file, search.language);
            return Vec::new();
        }
        Err(error) => {
            warning!("SUBTITLE Plex's provider unreachable: {error}");
            return Vec::new();
        }
    };
    answer
        .pointer("/MediaContainer/Stream")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|row| provider_candidate(row, &search.language))
        .collect()
}

/// One of the provider's rows: `{codec, id, key, language, languageCode, providerTitle, score,
/// title, index, streamType}` (the fixture's outbound answer).
fn provider_candidate(row: &Value, language: &str) -> Option<Candidate> {
    let text = |name: &str| row.get(name).map(string_value).unwrap_or_default();
    let title = text("title");
    Some(Candidate {
        source: "plex".to_string(),
        provider: Some(text("providerTitle")).filter(|name| !name.is_empty()).unwrap_or_else(|| "OpenSubtitles".to_string()),
        locator: row.get("key").and_then(Value::as_str)?.to_string(),
        release: title.clone(),
        title,
        score: row.get("score").cloned(),
        hearing_impaired: row.get("hearingImpaired").is_some_and(truthy),
        codec: Some(text("codec")).filter(|codec| !codec.is_empty()).unwrap_or_else(|| "srt".to_string()),
        language: Some(text("languageCode")).filter(|code| !code.is_empty()).unwrap_or_else(|| provider_language(language)),
        ..Candidate::default()
    })
}

/// An addon's own name, from its manifest; its host when it has none.
fn addon_name(base: &str) -> String {
    static NAMES: OnceLock<Mutex<HashMap<String, String>>> = OnceLock::new();
    let names = NAMES.get_or_init(Default::default);
    if let Some(name) = names.lock().unwrap_or_else(PoisonError::into_inner).get(base) {
        return name.clone();
    }
    let read = polite(&format!("{base}/manifest.json"), &[("User-Agent", upstream::UPSTREAM_UA.to_string())])
        .ok()
        .filter(|(status, _)| *status == 200)
        .and_then(|(_, body)| serde_json::from_slice::<Value>(&body).ok())
        .and_then(|manifest| manifest.get("name").and_then(Value::as_str).map(str::to_string));
    let name = read.clone().unwrap_or_else(|| base.split("://").nth(1).unwrap_or(base).split('/').next().unwrap_or(base).to_string());
    if read.is_some() {
        names.lock().unwrap_or_else(PoisonError::into_inner).insert(base.to_string(), name.clone());
    }
    name
}

/// Each addon's list, as the Stremio protocol asks it (candidates.py): the release's hash,
/// size and file name, in the language's ISO 639-2 code.
fn addon_candidates(search: &Search, hash: Option<&str>) -> Vec<Candidate> {
    let Some((kind, id)) = &search.stremio else { return Vec::new() };
    let wanted = addon_language(&search.language);
    let mut extra = Vec::new();
    extra.extend(hash.map(|hash| ("videoHash", hash.to_string())));
    extra.push(("videoSize", search.part.size.to_string()));
    extra.push(("filename", search.part.file.clone()));
    let mut out = Vec::new();
    for base in &search.addons {
        let url = format!("{base}/subtitles/{kind}/{id}/{}.json", encode(&extra));
        let listed = match polite(&url, &[("User-Agent", upstream::UPSTREAM_UA.to_string())]) {
            Ok((200, body)) => serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null),
            Ok((status, _)) => {
                warning!("SUBTITLE addon {base} answered {status}");
                continue;
            }
            Err(error) => {
                warning!("SUBTITLE addon {base} unreachable: {error}");
                continue;
            }
        };
        let name = addon_name(base);
        for entry in listed.get("subtitles").and_then(Value::as_array).into_iter().flatten() {
            let text = |field: &str| entry.get(field).map(string_value).unwrap_or_default();
            let (Some(url), lang) = (entry.get("url").and_then(Value::as_str), text("lang")) else { continue };
            if !addon_language_matches(&lang, &wanted) {
                continue;
            }
            let file = text("subtitleFileName");
            let release = Some(text("movieReleaseName")).filter(|name| !name.is_empty()).unwrap_or_else(|| file.clone());
            let title = file.strip_suffix(".srt").unwrap_or(&file).to_string();
            out.push(Candidate {
                source: base.clone(),
                provider: name.clone(),
                locator: url.to_string(),
                title: if title.is_empty() { Some(release.clone()).filter(|r| !r.is_empty()).unwrap_or_else(|| text("id")) } else { title },
                release,
                hash_match: text("m") == "h",
                fps_milli: entry.get("fpsMilli").and_then(|fps| fps.as_i64().or_else(|| fps.as_str()?.parse().ok())),
                hearing_impaired: file.contains("-HI") || file.contains(".HI."),
                file,
                codec: "srt".to_string(),
                language: provider_language(&search.language),
                ..Candidate::default()
            });
        }
    }
    out
}

fn search_all(search: &Search, hash: Option<&str>) -> Vec<Candidate> {
    let mut found = provider_candidates(search, hash);
    found.extend(addon_candidates(search, hash));
    found
}

fn download(candidate: &Candidate, agent: Option<&Agent>) -> Option<Vec<u8>> {
    let (url, headers) = if candidate.source == "plex" {
        // `GET /library/streams/{id}`, a 302 to the provider's own link. The token rides in the
        // header, as on the search, so no logged url carries it; the redirect drops it.
        let agent = agent?;
        let url = format!("{PROVIDER}{}", candidate.locator);
        let headers = vec![
            ("User-Agent", format!("PlexMediaServer/{}", agent.version)),
            ("Accept", "*/*".to_string()),
            ("Accept-Encoding", "gzip".to_string()),
            ("X-Plex-Token", agent.token.clone()),
        ];
        (super::plextv::plextv_url(&url), headers)
    } else {
        (candidate.locator.clone(), vec![("User-Agent", upstream::UPSTREAM_UA.to_string())])
    };
    match polite(&url, &headers) {
        Ok((200, body)) if body.len() <= MAX_FILE => Some(body),
        Ok((status, _)) => {
            warning!("SUBTITLE download of {} from {} answered {status}", candidate.title, candidate.provider);
            None
        }
        Err(error) => {
            warning!("SUBTITLE download of {} from {} failed: {error}", candidate.title, candidate.provider);
            None
        }
    }
}

// -- the reference -------------------------------------------------------------------------

/// The cues of an embedded text track, in seconds, from its index alone: a Matroska track's
/// `CueDuration`s, an MP4 track's sample times, a cue with no length lasting to the next
/// (`mkvsubs::Index::render`'s rule). An MP4's empty samples are its gaps.
fn track_cues(cues: &[super::mkvsubs::Cue]) -> Vec<(f64, f64)> {
    let kept = cues
        .iter()
        .filter(|cue| !matches!(cue.place, super::mkvsubs::Place::Sample { size, .. } if size <= 2))
        .collect::<Vec<_>>();
    kept.iter()
        .enumerate()
        .map(|(at, cue)| {
            let end = match cue.duration.filter(|duration| *duration > 0) {
                Some(duration) => cue.start + duration,
                None => kept.get(at + 1).map_or(cue.start + 4000, |next| next.start.min(cue.start + 8000)),
            };
            (cue.start as f64 / 1000.0, end as f64 / 1000.0)
        })
        .collect()
}

/// One window of the release's first audio track: 16 kHz mono, band-passed, read by Range.
fn audio_window(ffmpeg: &Path, part: &PartInfo, start: f64) -> Option<Vec<i16>> {
    use std::io::Read as _;
    let mut command = std::process::Command::new(ffmpeg);
    command.args(["-v", "error", "-nostdin", "-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "4"]);
    command.args(["-rw_timeout", "30000000"]);
    if let Some(auth) = &part.auth {
        command.args(["-headers", &format!("Authorization: {auth}\r\n")]);
    }
    command.args(["-user_agent", upstream::UPSTREAM_UA, "-ss", &format!("{start:.3}"), "-i", &part.url, "-t", "90"]);
    command.args(["-map", "0:a:0", "-ac", "1", "-ar", "16000", "-af", "highpass=f=300,lowpass=f=3400", "-f", "s16le", "-"]);
    command.stdin(std::process::Stdio::null()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::null());
    let mut child = super::universal::spawn_tied(command).ok()?;
    let mut raw = Vec::new();
    let read = child.stdout.take()?.read_to_end(&mut raw);
    let status = child.wait().ok()?;
    (read.is_ok() && status.success() && raw.len() >= 32_000).then(|| raw.chunks_exact(2).map(|pair| i16::from_le_bytes([pair[0], pair[1]])).collect())
}

/// What the candidates are measured against: the release's own embedded text track, read
/// through its index for a few hundred bytes a block, else (with `REX_TRANSCODER`) six 90 s
/// windows of its audio, else nothing.
fn reference(part: &PartInfo, ffmpeg: Option<&Path>) -> Option<Vec<(f64, Vec<u8>)>> {
    if part.duration <= 0 {
        return None;
    }
    let starts = window_starts(part.duration);
    for stream in &part.text_tracks {
        let read = |offset: u64, length: usize| super::universal::read_range(&part.url, part.auth.as_deref(), offset, length);
        match super::mkvsubs::index(&read, *stream) {
            Ok(index) => {
                let cues = track_cues(&index.cues);
                if spans(&cues, &starts) {
                    return Some(starts.iter().map(|start| (*start, cue_mask(&cues, *start))).collect());
                }
                info!("SUBTITLE text stream {stream} of {} is too sparse to measure against ({} cues)", part.file, cues.len());
            }
            Err(error) => info!("SUBTITLE cannot read text stream {stream} of {}: {error}", part.file),
        }
    }
    let ffmpeg = ffmpeg?;
    let windows = starts
        .iter()
        .filter_map(|start| audio_window(ffmpeg, part, *start).map(|pcm| (*start, energy_mask(&pcm))))
        .collect::<Vec<_>>();
    (!windows.is_empty()).then_some(windows)
}

/// A track the meter can stand on: three cues or more in at least half the windows. Against a
/// forced track (a real WEB-DL's: 12 lines, all within its first six minutes, measured 2026-10-20)
/// every candidate reads nothing. Tracks flagged forced are passed over already; this catches
/// one whose flag is missing (ponytail: not yet seen unflagged in a real library).
fn spans(cues: &[(f64, f64)], starts: &[f64]) -> bool {
    let full = starts.iter().filter(|start| cues.iter().filter(|(a, b)| *b > **start && *a < **start + WIN).count() >= 3).count();
    full * 2 >= starts.len() && !starts.is_empty()
}

// -- the jobs --------------------------------------------------------------------------------

/// The subtitle jobs running, by item, and the automatic searches that found nothing.
#[derive(Debug, Default)]
pub(crate) struct Jobs {
    running: Mutex<HashMap<String, usize>>,
    misses: Mutex<HashMap<String, Instant>>,
    /// Automatic subtitles kept unchecked by a detail request (no text track to check against,
    /// and audio is read only for a play), by item and language: the stream a play checks.
    unchecked: Mutex<HashMap<String, i64>>,
}

impl Jobs {
    fn start(&self, key: &str) {
        *self.running.lock().unwrap_or_else(PoisonError::into_inner).entry(key.to_string()).or_insert(0) += 1;
    }

    fn finish(&self, key: &str) {
        let mut running = self.running.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(count) = running.get_mut(key) {
            *count -= 1;
            if *count == 0 {
                running.remove(key);
            }
        }
    }

    fn running(&self, key: &str) -> bool {
        self.running.lock().unwrap_or_else(PoisonError::into_inner).contains_key(key)
    }

    fn missed(&self, what: &str) -> bool {
        self.misses.lock().unwrap_or_else(PoisonError::into_inner).get(what).is_some_and(|at| at.elapsed() < MISS_FOR)
    }

    fn miss(&self, what: String) {
        let mut misses = self.misses.lock().unwrap_or_else(PoisonError::into_inner);
        misses.retain(|_, at| at.elapsed() < MISS_FOR);
        misses.insert(what, Instant::now());
    }

    fn unchecked(&self, what: &str) -> Option<i64> {
        self.unchecked.lock().unwrap_or_else(PoisonError::into_inner).get(what).copied()
    }

    fn set_unchecked(&self, what: String, stream: Option<i64>) {
        let mut unchecked = self.unchecked.lock().unwrap_or_else(PoisonError::into_inner);
        match stream {
            Some(stream) => unchecked.insert(what, stream),
            None => unchecked.remove(&what),
        };
    }

    fn wait(&self, key: &str, limit: Duration) {
        let deadline = Instant::now() + limit;
        while self.running(key) && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(50));
        }
    }
}

#[derive(Debug)]
pub(crate) enum Task {
    /// Plex Web's apply of a search row: that candidate, measured and put right.
    Apply { offer: Candidate, activity: Map<String, Value> },
    /// The account's settings want a subtitle the Part lacks: rex's pick. `replaces` is the
    /// stream a detail request kept unchecked, which a play's job checks against the audio.
    Automatic { replaces: Option<i64> },
}

#[derive(Debug)]
pub(crate) struct Job {
    key: String,
    account: i64,
    search: Search,
    ffmpeg: Option<PathBuf>,
    /// An FFmpeg this job does not read audio with (a detail request's): its pick is kept
    /// unchecked for a play to check.
    audio_later: bool,
    task: Task,
    jobs: Arc<Jobs>,
}

struct Finish(Arc<Jobs>, String);

impl Drop for Finish {
    fn drop(&mut self) {
        self.0.finish(&self.1);
    }
}

/// Run a job on a thread of its own: everything but storing its answer is done off the lock.
pub(super) fn spawn(runtime: &SharedRuntime, job: Job) {
    let shared = Arc::clone(runtime);
    let (jobs, key) = (Arc::clone(&job.jobs), job.key.clone());
    if thread::Builder::new().name("subtitles".to_string()).spawn(move || run(&shared, job)).is_err() {
        jobs.finish(&key);
    }
}

fn run(shared: &SharedRuntime, job: Job) {
    let _finish = Finish(Arc::clone(&job.jobs), job.key.clone());
    let Job { key, account, search, ffmpeg, audio_later, task, jobs } = job;
    let begun = Instant::now();
    let (hash, candidates) = match &task {
        Task::Apply { offer, .. } => (None, vec![offer.clone()]),
        Task::Automatic { .. } => {
            let hash = hash_of(&search.part);
            let found = search_all(&search, hash.as_deref());
            (hash.filter(|_| search.part.hash.is_none()), found)
        }
    };
    let windows = if candidates.is_empty() { None } else { reference(&search.part, ffmpeg.as_deref()) };
    let mut texts = HashMap::new();
    let top = if windows.is_some() { MEASURED } else { 1 };
    let picked = pick(&search.part.file, search.part.frame_rate.as_deref(), &candidates, top, |at| {
        let raw = download(&candidates[at], search.agent.as_ref())?;
        let (text, garbled) = decode_text(&raw);
        let reading = windows.as_ref().map_or_else(Reading::unknown, |windows| measure(windows, &cues(&text)));
        info!(
            "SUBTITLE measured {key}: {} ({}) {} ratio {} lag {:.2}s z {}",
            candidates[at].title, candidates[at].provider, reading.verdict.name(), reading.ratio, reading.offset, reading.z
        );
        texts.insert(at, text);
        Some((reading, garbled))
    });
    let chosen = picked.and_then(|(at, fix)| {
        let text = texts.remove(&at)?;
        let text = match &fix {
            Some(fix) => retime(&text, fix.factor, fix.offset),
            None => text,
        };
        Some((at, fix, text))
    });
    let mut guard = shared.lock().unwrap_or_else(PoisonError::into_inner);
    if let Some(hash) = &hash {
        guard.keep_hash(&key, &search.part.id, hash);
    }
    let applied = matches!(task, Task::Apply { .. });
    let replaces = match task {
        Task::Automatic { replaces } => replaces,
        Task::Apply { .. } => None,
    };
    let unchecked = format!("{key}\u{1f}{}", provider_language(&search.language));
    match chosen {
        Some((at, fix, text)) => {
            let candidate = &candidates[at];
            let stored = match replaces {
                Some(stream) => guard.replace_subtitle(&key, stream, account, candidate, text),
                None => guard.store_subtitle(&key, account, &search, candidate, text, applied),
            };
            // Checked now, or never to be: a play's job read the audio, and without an FFmpeg
            // nothing will. Only a detail request's pick waits for a play.
            jobs.set_unchecked(unchecked.clone(), stored.filter(|_| !applied && windows.is_none() && audio_later));
            info!(
                "SUBTITLE {} {key} -> stream {} {} ({}){} in {:.1}s",
                if applied { "applied" } else { "automatic" },
                stored.map_or_else(|| "none".to_string(), |id| id.to_string()),
                candidate.title,
                candidate.provider,
                fix.map_or_else(String::new, |fix| format!(", corrected by ratio {} and {:.2}s", fix.ratio, fix.offset)),
                begun.elapsed().as_secs_f64()
            );
        }
        None if applied => warning!("SUBTITLE apply {key}: the subtitle could not be downloaded"),
        // The kept one stays as it was; it is not checked again.
        None if replaces.is_some() => jobs.set_unchecked(unchecked.clone(), None),
        None => {
            info!("SUBTITLE automatic {key}: nothing found in {}, not searched again for a day", search.language);
            jobs.miss(format!("{key}\u{1f}{}", provider_language(&search.language)));
        }
    }
    if let Task::Apply { mut activity, .. } = task {
        activity.insert("progress".to_string(), json!(100));
        guard.notify_activity(&activity, "ended");
    }
    guard.catalog.pack_pending();
    let snapshot = guard.flush_state();
    drop(guard);
    write_or_log(snapshot);
}

/// A search row's fields, and a stored one's, in the order the reference writes them
/// (`plex_search_apply_s143.json`): the stored one adds `selected` (an apply), `transient` and
/// the `userID` that added it. `id` and `key` are left for the caller.
fn offer_fields(candidate: &Candidate, stored: Option<(bool, i64)>, codec: &str) -> Map<String, Value> {
    let (code, name) = row_language(&candidate.language);
    let mut fields = Map::new();
    fields.insert("id".to_string(), Value::Null);
    fields.insert("key".to_string(), Value::Null);
    fields.insert("streamType".to_string(), json!(3));
    fields.insert("canAutoSync".to_string(), json!(false));
    if stored.is_some_and(|(selected, _)| selected) {
        fields.insert("selected".to_string(), json!(true));
    }
    fields.insert("codec".to_string(), json!(codec));
    fields.insert("language".to_string(), json!(name));
    fields.insert("languageTag".to_string(), json!(code));
    fields.insert("languageCode".to_string(), json!(code));
    fields.insert("format".to_string(), json!(codec));
    fields.insert("providerTitle".to_string(), json!(candidate.provider));
    // The provider's number, which the reference answers as a string.
    if let Some(score) = &candidate.score {
        fields.insert("score".to_string(), json!(string_value(score)));
    }
    fields.insert("sourceKey".to_string(), json!(candidate.locator));
    fields.insert("title".to_string(), json!(candidate.title));
    if let Some((_, user)) = stored {
        fields.insert("transient".to_string(), json!("1"));
        fields.insert("userID".to_string(), json!(user.to_string()));
    }
    fields
}

/// What a request's account wants subtitled: its subtitle language when its settings show
/// subtitles always (2), or with audio in another language (1) and the Part's audio is.
fn wanted_language(streams: &[Value], prefs: &crate::mediainfo::TrackPrefs) -> Option<String> {
    let language = prefs.subtitle.clone().filter(|language| !language.is_empty())?;
    match prefs.subtitles {
        2 => Some(language),
        1 => {
            let (audio, _) = crate::mediainfo::chosen_tracks(streams, prefs);
            let spoken = streams
                .iter()
                .find(|stream| audio.is_some() && stream.get("id").and_then(Value::as_i64) == audio)
                .and_then(crate::mediainfo::stream_language);
            (spoken != Some(crate::mediainfo::setting_language(&language))).then_some(language)
        }
        _ => None,
    }
}

/// Where the ids of search rows and the subtitles kept from them start. A Part's own streams are
/// `part * 100 + n` with parts `key * 10` (`scan::leaf_for`), so this is a range they reach only
/// past two million rows; and it needs no pass over the library, which an upload's id does.
const SUBTITLE_IDS: i64 = 2_000_000_000;

impl Runtime {
    /// The stream id an upload takes: the next after the highest the server holds (measured on
    /// the copy), and after every id a subtitle search handed out.
    pub(super) fn next_stream_id(&self) -> i64 {
        let held = self
            .catalog
            .hot()
            .lazy()
            .flat_map(|item| item.get("Media").and_then(Value::as_array).cloned().unwrap_or_default())
            .flat_map(|media| media.get("Part").and_then(Value::as_array).cloned().unwrap_or_default())
            .filter_map(|part| crate::catalog::max_stream_id(&part))
            .max()
            .unwrap_or(0);
        (held + 1).max(self.subtitle_serial)
    }

    /// `count` ids for search rows or a kept subtitle, each search's new (the reference's 386-389,
    /// then 390-393, its applied stream 394). The counter is kept in the state file.
    fn mint_subtitle_ids(&mut self, count: usize) -> i64 {
        let id = self.subtitle_serial.max(SUBTITLE_IDS);
        self.subtitle_serial = id + count as i64;
        id
    }

    /// Stream `id`, a subtitle with its text (`_content`), on `key`'s row: in the Part `part`
    /// names, else the first. `fields` holds the row in its order, `id` and `key` left to fill.
    /// `select` deselects every other subtitle of every Part of the row and selects it. Answers
    /// None when the row has no Part.
    pub(super) fn add_subtitle_stream(&mut self, key: &str, part: Option<&str>, id: i64, mut fields: Map<String, Value>, content: String, select: bool) -> Option<i64> {
        fields.insert("id".to_string(), json!(id));
        fields.insert("key".to_string(), json!(format!("/library/streams/{id}")));
        crate::mediainfo::stream_titles(&mut fields, None, None);
        fields.insert("_content".to_string(), json!(content));
        let row = self.catalog.get_mut(key)?;
        // Packed again with the request's other writes (`pack_pending`).
        crate::catalog::unpack_streams(row);
        let mut parts = row
            .get_mut("Media")
            .and_then(Value::as_array_mut)
            .into_iter()
            .flatten()
            .filter_map(|media| media.get_mut("Part").and_then(Value::as_array_mut))
            .flatten()
            .filter_map(Value::as_object_mut)
            .collect::<Vec<_>>();
        let target = part.and_then(|part| parts.iter().position(|row| row.get("id").map(string_value).as_deref() == Some(part))).unwrap_or(0);
        if parts.is_empty() {
            return None;
        }
        if select {
            for streams in parts.iter_mut().filter_map(|part| part.get_mut("Stream").and_then(Value::as_array_mut)) {
                for existing in streams.iter_mut().filter(|stream| stream.get("streamType").and_then(Value::as_i64) == Some(3)) {
                    if let Some(existing) = existing.as_object_mut() {
                        existing.shift_remove("selected");
                    }
                }
            }
        }
        if let Some(streams) = parts[target].entry("Stream".to_string()).or_insert_with(|| Value::Array(Vec::new())).as_array_mut() {
            streams.push(Value::Object(fields));
        }
        self.subtitle_serial = self.subtitle_serial.max(id + 1);
        self.dirty = true;
        self.save_if_dirty();
        Some(id)
    }

    /// The OpenSubtitles hash kept on the Part it was read for.
    fn keep_hash(&mut self, key: &str, part: &str, hash: &str) {
        let Some(row) = self.catalog.get_mut(key) else { return };
        for media in row.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
            for found in media.get_mut("Part").and_then(Value::as_array_mut).into_iter().flatten() {
                if found.get("id").map(string_value).as_deref() == Some(part) {
                    if let Some(object) = found.as_object_mut() {
                        object.insert("_osHash".to_string(), json!(hash));
                        self.dirty = true;
                    }
                }
            }
        }
    }

    /// A downloaded subtitle kept on the item, as the reference keeps an applied one, and for
    /// an automatic one the owner's tracks selected again by their settings.
    fn store_subtitle(&mut self, key: &str, account: i64, search: &Search, candidate: &Candidate, text: String, applied: bool) -> Option<i64> {
        let codec = if is_ass(&text) { "ass" } else { "srt" };
        let fields = offer_fields(candidate, Some((applied, account)), codec);
        let id = self.mint_subtitle_ids(1);
        self.add_subtitle_stream(key, Some(&search.part.id), id, fields, text, applied)?;
        if !applied {
            if let Some(prefs) = self.track_prefs.clone() {
                if let Some(media) = self.catalog.get_mut(key).and_then(|row| row.get_mut("Media")) {
                    crate::mediainfo::select_media_tracks(media, &prefs);
                }
            }
        }
        Some(id)
    }

    /// A kept automatic subtitle put right in place: same id and key, the play's pick and text.
    fn replace_subtitle(&mut self, key: &str, stream: i64, account: i64, candidate: &Candidate, text: String) -> Option<i64> {
        let codec = if is_ass(&text) { "ass" } else { "srt" };
        let row = self.catalog.get_mut(key)?;
        crate::catalog::unpack_streams(row);
        let found = row
            .get_mut("Media")
            .and_then(Value::as_array_mut)
            .into_iter()
            .flatten()
            .filter_map(|media| media.get_mut("Part").and_then(Value::as_array_mut))
            .flatten()
            .filter_map(|part| part.get_mut("Stream").and_then(Value::as_array_mut))
            .flatten()
            .filter_map(Value::as_object_mut)
            .find(|existing| existing.get("id").and_then(Value::as_i64) == Some(stream))?;
        let selected = found.get("selected").is_some_and(truthy);
        let mut fields = offer_fields(candidate, Some((selected, account)), codec);
        fields.insert("id".to_string(), json!(stream));
        fields.insert("key".to_string(), json!(format!("/library/streams/{stream}")));
        crate::mediainfo::stream_titles(&mut fields, None, None);
        fields.insert("_content".to_string(), json!(text));
        *found = fields;
        self.dirty = true;
        self.save_if_dirty();
        Some(stream)
    }

    fn subtitle_agent(&self) -> Option<Agent> {
        let claim = self.plextv.claim.lock().unwrap_or_else(PoisonError::into_inner);
        let token = claim.token.clone().filter(|token| !token.is_empty())?;
        Some(Agent { token, machine: claim.machine.clone(), name: claim.name.clone(), version: claim.version.clone() })
    }

    /// What a search of `key` in `language` asks, None when there is nobody to ask (no claim
    /// and no addons) or no source holds the Part. `media` is the client's `mediaItemID`.
    fn subtitle_search_spec(&self, row: &Value, language: &str, media: Option<&str>) -> Option<Search> {
        let agent = self.subtitle_agent();
        let addons = addons();
        if agent.is_none() && addons.is_empty() {
            return None;
        }
        let row = crate::catalog::unpacked(row);
        let rows = row.get("Media").and_then(Value::as_array)?;
        let chosen = media
            .and_then(|wanted| rows.iter().find(|media| media.get("id").map(string_value).as_deref() == Some(wanted)))
            .or_else(|| rows.first())?;
        let part = chosen.get("Part").and_then(Value::as_array)?.first()?;
        let file = part.get("file").and_then(Value::as_str)?;
        let (source, path) = self.part_source(&row, file)?;
        let (url, auth) = source.ffmpeg_input(&path);
        let streams = part.get("Stream").and_then(Value::as_array).cloned().unwrap_or_default();
        let text = |value: &Value, name: &str| value.get(name).map(string_value).filter(|text| !text.is_empty());
        let readable = matches!(chosen.get("container").and_then(Value::as_str), Some("mkv" | "mp4" | "mov"));
        let text_tracks = streams
            .iter()
            .filter(|_| readable)
            .filter(|stream| {
                stream.get("streamType").and_then(Value::as_i64) == Some(3)
                    && stream.get("key").is_none()
                    && !stream.get("forced").is_some_and(truthy)
                    && matches!(text(stream, "codec").unwrap_or_default().to_ascii_lowercase().as_str(), "srt" | "ass" | "ssa" | "mov_text")
            })
            .filter_map(|stream| stream.get("index").and_then(Value::as_i64))
            .filter(|index| *index >= 0)
            .collect();
        // rex identifies nothing: the IMDb id is the one the rows hold, a show's for an episode.
        let imdb = |row: &Value| {
            row.get("Guid").and_then(Value::as_array).into_iter().flatten().find_map(|guid| {
                guid.get("id").and_then(Value::as_str)?.strip_prefix("imdb://").map(str::to_string)
            })
        };
        let episode = row.get("type").and_then(Value::as_str) == Some("episode");
        let stremio = if episode {
            let show = row.get("grandparentRatingKey").map(string_value).and_then(|show| self.catalog.get(&show).cloned());
            let (season, number) = (text(&row, "parentIndex"), text(&row, "index"));
            match (show.as_ref().and_then(imdb), season, number) {
                (Some(id), Some(season), Some(number)) => Some(("series".to_string(), format!("{id}:{season}:{number}"))),
                _ => None,
            }
        } else {
            imdb(&row).map(|id| ("movie".to_string(), id))
        };
        Some(Search {
            language: language.to_string(),
            agent,
            addons,
            guid: text(&row, "guid").filter(|guid| guid.starts_with("plex://")),
            episode,
            year: text(&row, "year").unwrap_or_default(),
            stremio,
            part: PartInfo {
                id: text(part, "id").unwrap_or_default(),
                url,
                auth,
                size: part.get("size").and_then(Value::as_u64).unwrap_or(0),
                file: file.rsplit('/').next().unwrap_or(file).to_string(),
                duration: chosen.get("duration").or_else(|| part.get("duration")).and_then(Value::as_i64).unwrap_or(0),
                frame_rate: text(chosen, "videoFrameRate"),
                video_codec: text(chosen, "videoCodec").unwrap_or_default(),
                resolution: text(chosen, "videoResolution").unwrap_or_default(),
                text_tracks,
                hash: text(part, "_osHash"),
            },
        })
    }

    /// `GET /library/metadata/{id}/subtitles?language=`: Plex's provider and the addons asked
    /// with the lock released (`agent_answer`), their candidates ranked by rex and answered as
    /// the reference's Stream rows, each search minting new local ids (the reference's 386-389,
    /// then 390-393) that keep the candidate for the apply. With nobody to ask, the empty
    /// container it always answered.
    pub(super) fn subtitle_search(&mut self, key: &str, row: &Value, query: &HashMap<String, String>, language: &str) -> Response {
        let answer = |runtime: &Self, rows: Vec<Value>| {
            let mut fields = Map::from_iter([("size".to_string(), json!(rows.len()))]);
            if !rows.is_empty() {
                fields.insert("Stream".to_string(), Value::Array(rows));
            }
            // The oracle's `tag_container(size=0, Stream=[])`: the library identity without
            // `allowSync`, as the reference's answer with rows is.
            let mut body = runtime.container(fields, true);
            if let Some(object) = body.get_mut("MediaContainer").and_then(Value::as_object_mut) {
                object.shift_remove("allowSync");
            }
            Response::json(200, body)
        };
        let Some(search) = self.subtitle_search_spec(row, language, query.get("mediaItemID").map(String::as_str)) else {
            return answer(self, Vec::new());
        };
        let asked = search.clone();
        let found = self.agent_answer(format!("subtitles\u{1f}{key}\u{1f}{language}"), move || {
            let hash = hash_of(&asked.part);
            let candidates = search_all(&asked, hash.as_deref());
            json!({"hash": hash, "candidates": candidates.iter().map(Candidate::to_value).collect::<Vec<_>>()})
        });
        let Some(found) = found else {
            return Response::empty(200);
        };
        if let Some(hash) = found.get("hash").and_then(Value::as_str).filter(|_| search.part.hash.is_none()) {
            self.keep_hash(key, &search.part.id, hash);
        }
        let candidates = found.get("candidates").and_then(Value::as_array).into_iter().flatten().map(Candidate::from_value).collect::<Vec<_>>();
        let base = self.mint_subtitle_ids(candidates.len());
        let mut rows = Vec::new();
        for (number, at) in rank(&search.part.file, search.part.frame_rate.as_deref(), &candidates, &|_| 0).into_iter().enumerate() {
            let id = base + number as i64;
            let candidate = &candidates[at];
            let mut fields = offer_fields(candidate, None, &candidate.codec);
            fields.insert("id".to_string(), json!(id));
            fields.insert("key".to_string(), json!(format!("/library/streams/{id}")));
            crate::mediainfo::stream_titles(&mut fields, None, None);
            rows.push(Value::Object(fields));
            self.subtitle_offers.insert(id, (key.to_string(), candidate.clone()));
        }
        // Plenty for the dialogs open at once; an older row's apply is refused.
        self.subtitle_offers.retain(|id, _| *id >= base - 4096);
        info!("SUBTITLE search {key} {language}: {} candidate(s)", rows.len());
        answer(self, rows)
    }

    /// `PUT /library/metadata/{id}/subtitles?key=/library/streams/{id}&...` (Plex Web's apply):
    /// 200 at once with the activity in `X-Plex-Activity`, its `subtitle.download` started now
    /// and ended when the subtitle is kept, as the reference answered (about 0.5 s later there).
    pub(super) fn subtitle_apply(&mut self, key: &str, row: &Value, query: &HashMap<String, String>) -> Response {
        let offer = query
            .get("key")
            .and_then(|stream| stream.strip_prefix("/library/streams/"))
            .and_then(|id| id.parse::<i64>().ok())
            .and_then(|id| self.subtitle_offers.get(&id).cloned());
        // ponytail: unmeasured -- a stream no search of this server answered for this item.
        let Some((_, offer)) = offer.filter(|(owner, _)| owner == key) else {
            return Response::error(400, "rex: bad request");
        };
        let Some(search) = self.subtitle_search_spec(row, &offer.language, query.get("mediaItemID").map(String::as_str)) else {
            return Response::error(400, "rex: bad request");
        };
        let activity = super::butler::new_activity("subtitle.download", "Downloading Subtitle", false);
        self.notify_activity(&activity, "started");
        let uuid = activity.get("uuid").map(string_value).unwrap_or_default();
        info!("SUBTITLE apply {key} <- {} ({})", offer.title, offer.provider);
        self.subtitle_jobs.start(key);
        self.pending_subtitle = Some(Job {
            key: key.to_string(),
            account: self.viewer.id,
            search,
            ffmpeg: self.transcoder.as_ref().map(|transcoder| transcoder.ffmpeg().to_path_buf()),
            audio_later: false,
            task: Task::Apply { offer, activity },
            jobs: Arc::clone(&self.subtitle_jobs),
        });
        let mut response = Response::empty(200);
        response.headers.push(("X-Plex-Activity".to_string(), uuid));
        response.headers.push(("Access-Control-Expose-Headers".to_string(), "X-Plex-Activity".to_string()));
        response
    }

    /// Automatic subtitles, on an item's detail request and a play queue's: when the requesting
    /// account's settings want subtitles in a language its Part has none in, a job finds,
    /// measures and keeps one before the playback decision picks the tracks. Only a play's job
    /// reads audio (`playing`): six 90 s windows are a few hundred MB, too much for browsing
    /// (Ben, 2026-10-20). A detail request's pick with nothing else to check it against is kept
    /// unchecked, and the play's job checks it and puts it right in place.
    pub(super) fn subtitles_wanted(&mut self, key: &str, row: &Value, playing: bool) {
        if !matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode")) || self.pending_subtitle.is_some() {
            return;
        }
        let prefs = if self.viewer.is_owner() { self.track_prefs.clone() } else { self.viewer.prefs.clone() };
        let Some(prefs) = prefs else { return };
        let row = crate::catalog::unpacked(row);
        let streams = row
            .pointer("/Media/0/Part/0/Stream")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let Some(language) = wanted_language(&streams, &prefs) else { return };
        let have = crate::mediainfo::setting_language(&language);
        let miss = format!("{key}\u{1f}{}", provider_language(&language));
        let ffmpeg = self.transcoder.as_ref().map(|transcoder| transcoder.ffmpeg().to_path_buf());
        let present = streams.iter().any(|stream| stream.get("streamType").and_then(Value::as_i64) == Some(3) && crate::mediainfo::stream_language(stream).as_deref() == Some(have.as_str()));
        let replaces = match (present, self.subtitle_jobs.unchecked(&miss)) {
            (false, _) => None,
            (true, Some(stream)) if playing && ffmpeg.is_some() => Some(stream),
            (true, _) => return,
        };
        if self.subtitle_jobs.running(key) || (replaces.is_none() && self.subtitle_jobs.missed(&miss)) {
            return;
        }
        let Some(search) = self.subtitle_search_spec(&row, &language, None) else { return };
        info!(
            "SUBTITLE automatic {key}: account {} wants {language}{}",
            self.viewer.id,
            if replaces.is_some() { ", checking the kept one against the audio" } else { "" }
        );
        self.subtitle_jobs.start(key);
        self.pending_subtitle = Some(Job {
            key: key.to_string(),
            account: self.viewer.id,
            search,
            audio_later: !playing && ffmpeg.is_some(),
            ffmpeg: ffmpeg.filter(|_| playing),
            task: Task::Automatic { replaces },
            jobs: Arc::clone(&self.subtitle_jobs),
        });
    }

    /// A playback decision for an item whose subtitle job is running waits for it, up to
    /// DECISION_WAIT, with the lock released (`agent_answer`): true while it must answer
    /// nothing yet and be routed again.
    pub(super) fn subtitle_wait(&mut self, key: &str) -> bool {
        let waited = format!("subtitles-wait\u{1f}{key}");
        if !self.subtitle_jobs.running(key) && !self.agent_answers.contains_key(&waited) {
            return false;
        }
        let (jobs, item) = (Arc::clone(&self.subtitle_jobs), key.to_string());
        self.agent_answer(waited, move || {
            jobs.wait(&item, DECISION_WAIT);
            Value::Null
        })
        .is_none()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opensubtitles_hash_is_size_plus_head_and_tail_words() {
        // Two words of 1 and 2 in the head, one of 3 in the tail, on a 0x10000-byte file.
        let mut head = vec![0_u8; 65_536];
        head[0] = 1;
        head[8] = 2;
        let mut tail = vec![0_u8; 65_536];
        tail[65_528] = 3;
        assert_eq!(opensubtitles_hash(0x10000, &head, &tail), "0000000000010006");
        // Wrapping, as the reference's `& 0xFFFFFFFFFFFFFFFF` does.
        let ones = vec![0xFF_u8; 8];
        assert_eq!(opensubtitles_hash(2, &ones, &[]), "0000000000000001");
    }

    #[test]
    fn mojibake_from_the_provider_is_put_back() {
        // `♪ Love ♪` saved as UTF-8 after a read as Windows-1252: the bytes of `â™ª Love â™ª`.
        let raw = b"1\r\n00:00:01,000 --> 00:00:02,000\r\n\xC3\xA2\xE2\x84\xA2\xC2\xAA Love \xC3\xA2\xE2\x84\xA2\xC2\xAA\r\n";
        let (text, found) = decode_text(raw);
        assert_eq!(found, 2);
        assert!(text.contains("\u{266A} Love \u{266A}"), "{text}");
        // Portuguese the same way, and a real `Ã` before a letter kept.
        let (text, found) = decode_text("NÃ£o Ã© SÃO PAULO".as_bytes());
        assert_eq!((text.as_str(), found), ("Não é SÃO PAULO", 2));
        // Windows-1252 bytes are not UTF-8: read as Windows-1252, the BOM dropped.
        let (text, found) = decode_text(b"\xEF\xBB\xBFa\x93b\x94 \xE9");
        assert_eq!((text.as_str(), found), ("a\u{201C}b\u{201D} é", 0));
        let (text, _) = decode_text(b"Ol\xE1, voc\xEA");
        assert_eq!(text, "Olá, você");
        // Clean UTF-8 stays as it is.
        assert_eq!(decode_text("日本語 ♪ çà".as_bytes()).0, "日本語 ♪ çà");
    }

    #[test]
    fn cues_read_every_timing_form() {
        let srt = "1\n00:01:47,250 --> 00:01:50,500\nThis blade has a dark past.\n\n2\n00:01:51,05 --> 00:01:52,050\nx\n";
        assert_eq!(cues(srt), vec![(107.25, 110.5), (111.05, 112.05)]);
        let vtt = "WEBVTT\n\n01:02.500 --> 01:04.000 align:start\nx\n";
        assert_eq!(cues(vtt), vec![(62.5, 64.0)]);
        let ass = "[Events]\nFormat: Layer, Start, End, Style, Text\nDialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,Hi\n";
        assert_eq!(cues(ass), vec![(1.5, 3.0)]);
    }

    #[test]
    fn retiming_scales_then_shifts_every_form() {
        let srt = "1\n00:01:40,000 --> 00:01:42,000\nx\n";
        // 2 s late: moved back.
        assert_eq!(retime(srt, 1.0, 2.0), "1\n00:01:38,000 --> 00:01:40,000\nx\n");
        // Timed for 25 fps on a 23.976 release: scaled by 25/23.976 first.
        let fixed = retime(srt, 25.0 / 23.976, 0.0);
        assert_eq!(cues(&fixed), vec![(104.271, 106.356)]);
        let vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000 line:0\nx\n";
        assert_eq!(retime(vtt, 1.0, 1.5), "WEBVTT\n\n00:00:00.000 --> 00:00:01.500 line:0\nx\n");
        let ass = "[Events]\nDialogue: 0,0:00:10.00,0:00:12.50,Default,,0,0,0,,Hi, there\n";
        assert_eq!(retime(ass, 1.0, -1.0), "[Events]\nDialogue: 0,0:00:11.00,0:00:13.50,Default,,0,0,0,,Hi, there\n");
    }

    #[test]
    fn languages_map_between_plex_and_the_addons() {
        assert_eq!(provider_language("en"), "en");
        assert_eq!(provider_language("EN-us"), "en");
        assert_eq!(provider_language("pt-BR"), "pt-BR");
        assert_eq!(provider_language("pt-br"), "pt-BR");
        assert_eq!(provider_language("pt"), "pt");
        assert_eq!(addon_language("en"), "eng");
        assert_eq!(addon_language("pt-BR"), "pob");
        assert_eq!(addon_language("pt"), "por");
        assert_eq!(addon_language("de"), "deu");
        assert!(addon_language_matches("ger", "deu") && addon_language_matches("pob", "pob") && !addon_language_matches("por", "pob"));
        assert_eq!(row_language("en"), ("eng".to_string(), "English".to_string()));
        assert_eq!(row_language("pt-BR").0, "por");
    }

    /// The provider's recorded answer, made into rows the way the search and the apply make
    /// them, is the reference's search rows and its applied row, field for field and in order.
    #[test]
    fn rows_are_the_references() {
        let fixture: Value = serde_json::from_str(include_str!("../../../tests/fixtures/subtitles/plex_search_apply_s143.json")).unwrap();
        let provider: Value = serde_json::from_str(fixture["outbound"][0]["res_body"].as_str().unwrap()).unwrap();
        let expected = fixture["client"]["search"]["body"]["MediaContainer"]["Stream"].as_array().unwrap();
        let offered = provider["MediaContainer"]["Stream"].as_array().unwrap();
        assert_eq!(offered.len(), expected.len());
        let row = |candidate: &Candidate, id: i64, stored: Option<(bool, i64)>| {
            let mut fields = offer_fields(candidate, stored, &candidate.codec);
            fields.insert("id".to_string(), json!(id));
            fields.insert("key".to_string(), json!(format!("/library/streams/{id}")));
            crate::mediainfo::stream_titles(&mut fields, None, None);
            fields
        };
        for (offer, reference) in offered.iter().zip(expected) {
            let candidate = provider_candidate(offer, "en").unwrap();
            let built = row(&candidate, reference["id"].as_i64().unwrap(), None);
            assert_eq!(Value::Object(built.clone()), *reference);
            assert_eq!(built.keys().collect::<Vec<_>>(), reference.as_object().unwrap().keys().collect::<Vec<_>>());
        }
        let applied = &fixture["client"]["appliedStreams"][0];
        let built = row(&provider_candidate(&offered[0], "en").unwrap(), 394, Some((true, 1)));
        assert_eq!(Value::Object(built.clone()), *applied);
        assert_eq!(built.keys().collect::<Vec<_>>(), applied.as_object().unwrap().keys().collect::<Vec<_>>());
        // The account's stream reads as the language its settings name.
        assert_eq!(crate::mediainfo::stream_language(applied).as_deref(), Some("en"));
    }

    #[test]
    fn ranking_prefers_the_release_and_its_hash() {
        let file = "Love.At.First.Sight.2023.1080p.NF.WEB-DL.DDP5.1.Atmos.x264-TFA.mkv";
        let candidate = |release: &str| Candidate { release: release.to_string(), ..Candidate::default() };
        let near = candidate("Love.at.First.Sight.2023.720p.NF.WEB-DL.DDP5.1.H.264-TFA");
        let far = candidate("Love.at.First.Sight.2023.BluRay.x264-OTHER");
        assert!(rank_score(file, Some("24p"), &near, 0) > rank_score(file, Some("24p"), &far, 0));
        let hashed = Candidate { hash_match: true, ..far.clone() };
        assert!(rank_score(file, None, &hashed, 0) > rank_score(file, None, &near, 0));
        let pal = Candidate { fps_milli: Some(25_000), ..near.clone() };
        assert!((rank_score(file, Some("24p"), &near, 0) - rank_score(file, Some("24p"), &pal, 0) - 2.0).abs() < 1e-9);
        assert!((rank_score(file, None, &near, 0) - rank_score(file, None, &near, 4) - 1.0).abs() < 1e-9);
        assert_eq!(group("Some.Release-GROUP[rarbg].srt").as_deref(), Some("group"));
    }

    // -- the measured corpus -----------------------------------------------------------

    struct Corpus {
        items: Vec<Value>,
    }

    fn corpus() -> Corpus {
        use std::io::Read as _;
        let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../tests/fixtures/subtitles/sync_corpus.json.gz");
        let mut text = String::new();
        flate2::read::GzDecoder::new(fs::File::open(path).expect("the corpus is checked in")).read_to_string(&mut text).expect("gzip");
        let document: Value = serde_json::from_str(&text).expect("json");
        Corpus { items: document["items"].as_array().cloned().unwrap_or_default() }
    }

    /// The fixture's energy masks: numpy `packbits`, most significant bit first.
    fn windows_of(item: &Value) -> Vec<(f64, Vec<u8>)> {
        item["windows"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|window| {
                let bytes = base64_decode_bytes(window["energy"].as_str().unwrap_or_default()).unwrap_or_default();
                let frames = window["frames"].as_u64().unwrap_or(0) as usize;
                let mask = (0..frames).map(|at| (bytes[at / 8] >> (7 - at % 8)) & 1).collect();
                (window["start"].as_f64().unwrap_or(0.0), mask)
            })
            .collect()
    }

    /// `cueEncoding`: flat centiseconds, each the difference from the one before.
    fn cues_of(candidate: &Value) -> Vec<(f64, f64)> {
        let mut time = 0_i64;
        let flat = candidate["cues"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|delta| {
                time += delta.as_i64().unwrap_or(0);
                time as f64 / 100.0
            })
            .collect::<Vec<_>>();
        flat.chunks_exact(2).map(|pair| (pair[0], pair[1])).collect()
    }

    fn candidate_of(row: &Value) -> Candidate {
        let text = |name: &str| row[name].as_str().unwrap_or_default().to_string();
        let file = text("file");
        Candidate {
            source: text("source"),
            release: text("release"),
            hash_match: row["hashMatch"].as_bool().unwrap_or(false),
            fps_milli: row["fpsMilli"].as_i64().or_else(|| row["fpsMilli"].as_str().and_then(|fps| fps.parse().ok())),
            // analyze.py: Plex's flag, an addon's file name.
            hearing_impaired: row["hearingImpaired"].as_bool().unwrap_or(false) || file.contains("-HI") || file.contains(".HI."),
            file,
            ..Candidate::default()
        }
    }

    /// The corpus run through rex's own pick, on the fixture's energy masks and cue timings,
    /// against the Silero meter's verdicts (`truth`). A pick is right when its truth is in sync
    /// and rex left it as it was, or rex corrected it by the truth's ratio and an offset within
    /// 0.5 s of the truth's. Plex's pick is its provider's first result, as Plex Web lists it.
    #[test]
    fn rex_picks_in_sync_subtitles_far_more_often_than_plex() {
        let corpus = corpus();
        let mut tally: std::collections::BTreeMap<&str, [usize; 3]> = Default::default();
        let (mut both, mut agree, mut false_sync, mut measured_total, mut overall) = (0, 0, 0, 0, 0);
        for item in &corpus.items {
            let windows = windows_of(item);
            let file = item["file"].as_str().unwrap_or_default();
            let frame_rate = item["videoFrameRate"].as_str();
            for language in ["en", "pt-BR"] {
                let rows = item["candidates"].as_array().into_iter().flatten().filter(|row| row["lang"] == language).collect::<Vec<_>>();
                let truth = |row: &Value| row["truth"]["verdict"].as_str().unwrap_or("unknown").to_string();
                if !rows.iter().any(|row| truth(row) != "unknown") {
                    continue;
                }
                let candidates = rows.iter().map(|row| candidate_of(row)).collect::<Vec<_>>();
                let picked = pick(file, frame_rate, &candidates, MEASURED, |at| {
                    let reading = measure(&windows, &cues_of(rows[at]));
                    let expected = truth(rows[at]);
                    measured_total += 1;
                    overall += usize::from(reading.verdict.name() == expected);
                    if reading.verdict != Verdict::Unknown && expected != "unknown" {
                        both += 1;
                        agree += usize::from(reading.verdict.name() == expected);
                    }
                    false_sync += usize::from(reading.verdict == Verdict::InSync && matches!(expected.as_str(), "shift" | "fps" | "broken"));
                    Some((reading, rows[at]["mojibake"].as_u64().unwrap_or(0) as usize))
                });
                let entry = tally.entry(language).or_default();
                entry[0] += 1;
                let plex_first = rows.iter().find(|row| row["source"] == "plex");
                entry[1] += usize::from(plex_first.is_some_and(|row| truth(row) == "in_sync"));
                let right = match picked {
                    Some((at, None)) => truth(rows[at]) == "in_sync",
                    Some((at, Some(fix))) => {
                        let expected = &rows[at]["truth"];
                        matches!(truth(rows[at]).as_str(), "in_sync" | "shift" | "fps")
                            && expected["ratio"].as_str() == Some(fix.ratio)
                            && expected["offset"].as_f64().is_some_and(|offset| (offset - fix.offset).abs() <= 0.5)
                    }
                    None => false,
                };
                entry[2] += usize::from(right);
            }
        }
        for (language, [items, plex, rex]) in &tally {
            println!(
                "{language}: {items} releases with a readable candidate; Plex's first result in sync {plex} ({:.0}%), rex's pick right {rex} ({:.0}%)",
                100.0 * *plex as f64 / *items as f64,
                100.0 * *rex as f64 / *items as f64
            );
        }
        println!(
            "meter: {measured_total} candidates measured, {overall} verdicts as the truth's ({:.0}%); where both read one, {agree} of {both} agree ({:.0}%); {false_sync} called in sync that the truth calls shift/fps/broken",
            100.0 * overall as f64 / measured_total.max(1) as f64,
            100.0 * agree as f64 / both.max(1) as f64
        );
        assert_eq!(tally["en"][0], 77, "the README's 77 English releases");
        assert_eq!(tally["pt-BR"][0], 49, "the README's 49 Brazilian releases");
        assert_eq!((tally["en"][1], tally["pt-BR"][1]), (41, 22), "Plex's first result, as the README measured it");
        for (language, [items, plex, rex]) in &tally {
            // At least fifteen points better in each language.
            assert!(
                (*rex as f64 - *plex as f64) / *items as f64 >= 0.15,
                "{language}: rex {rex} against Plex {plex} of {items}"
            );
        }
        assert!(agree as f64 >= 0.9 * both as f64, "the meter port agrees with the truth where both read: {agree} of {both}");
        assert!(overall * 2 > measured_total, "the meter port's verdicts are the truth's for most candidates: {overall} of {measured_total}");
        assert!(false_sync <= 3, "{false_sync} candidates called in sync that are off");
    }

    #[test]
    fn a_sparse_track_is_no_reference() {
        // Lucky Strike 2026's forced English track (iT WEB-DL, read through a claimed rex,
        // 2026-10-20): twelve lines between 4:33 and 6:00 of a 103-minute film.
        let starts = window_starts(6_211_136);
        let forced = (0..12).map(|at| (273.9 + at as f64 * 7.5, 275.0 + at as f64 * 7.5)).collect::<Vec<_>>();
        assert!(!spans(&forced, &starts));
        let full = (0..1500).map(|at| (at as f64 * 4.0, at as f64 * 4.0 + 2.5)).collect::<Vec<_>>();
        assert!(spans(&full, &starts));
        // Three of six windows with three lines each is enough; two is not.
        let half = |windows: usize| starts.iter().take(windows).flat_map(|start| (0..3).map(move |at| (start + 10.0 * at as f64, start + 10.0 * at as f64 + 2.0))).collect::<Vec<_>>();
        assert!(spans(&half(3), &starts));
        assert!(!spans(&half(2), &starts));
    }

    /// The whole job against the live services for one release read through a claimed rex:
    /// Plex's provider and the Stremio OpenSubtitles addon searched, the top five downloaded and
    /// measured against the release (its embedded text tracks when `text_tracks` names any, else
    /// its audio through the FFmpeg on PATH). `REX_LIVE_TOKEN`/`REX_LIVE_MACHINE` are a claim's;
    /// `REX_LIVE_ITEM` is the release: `{url, size, file, duration, fps, codec, resolution,
    /// text_tracks, guid, year, imdb, hash}`, the url a Part's with its token.
    #[test]
    #[ignore]
    fn live_job_end_to_end() {
        let env = |name: &str| std::env::var(name).unwrap_or_else(|_| panic!("{name}"));
        let agent = Agent { token: env("REX_LIVE_TOKEN"), machine: env("REX_LIVE_MACHINE"), name: "rex".into(), version: "1.42.2.10156-f737b826c".into() };
        let item: Value = serde_json::from_str(&env("REX_LIVE_ITEM")).expect("REX_LIVE_ITEM");
        let field = |name: &str| item[name].as_str().unwrap_or_default().to_string();
        let part = PartInfo {
            id: "live".into(),
            url: field("url"),
            auth: None,
            size: item["size"].as_u64().unwrap_or_default(),
            file: field("file"),
            duration: item["duration"].as_i64().unwrap_or_default(),
            frame_rate: Some(field("fps")),
            video_codec: field("codec"),
            resolution: field("resolution"),
            text_tracks: item["text_tracks"].as_array().into_iter().flatten().filter_map(Value::as_i64).collect(),
            hash: None,
        };
        let hash = hash_of(&part);
        assert_eq!(hash.as_deref(), Some(field("hash").as_str()), "the hash the study read through rex");
        for language in ["en", "pt-BR"] {
            let search = Search {
                language: language.into(),
                agent: Some(agent.clone()),
                addons: vec!["https://opensubtitles-v3.strem.io".into()],
                guid: Some(field("guid")),
                episode: false,
                year: field("year"),
                stremio: Some(("movie".into(), field("imdb"))),
                part: part.clone(),
            };
            let found = search_all(&search, hash.as_deref());
            let plex = found.iter().filter(|candidate| candidate.source == "plex").count();
            println!("{language}: {plex} from Plex's provider, {} from the addon", found.len() - plex);
            assert!(plex > 0 && found.len() > plex, "both sources answer");
            {
                let label = if part.text_tracks.is_empty() { "audio" } else { "embedded track" };
                let windows = reference(&part, Some(Path::new("ffmpeg"))).expect("a reference");
                let mut texts = HashMap::new();
                let picked = pick(&part.file, part.frame_rate.as_deref(), &found, MEASURED, |at| {
                    let raw = download(&found[at], Some(&agent))?;
                    let (text, garbled) = decode_text(&raw);
                    let reading = measure(&windows, &cues(&text));
                    println!("  {label}: {} [{}] {} ratio {} lag {:.2} z {} garbled {garbled} left {}", found[at].title, found[at].source, reading.verdict.name(), reading.ratio, reading.offset, reading.z, mojibake(&text));
                    texts.insert(at, text);
                    Some((reading, garbled))
                });
                let (at, fix) = picked.expect("a pick");
                println!("  {label} picks {} {:?}", found[at].title, fix);
                assert_eq!(mojibake(&texts[&at]), 0, "the kept text is clean UTF-8");
            }
        }
    }
}
