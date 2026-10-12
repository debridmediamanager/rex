//! Plex query/filter grammar.
//!
//! Query operators are encoded in parameter names.  The parser therefore keeps
//! the ordered `(key, value)` pairs supplied by the caller and never collapses
//! duplicate keys into a map.

use std::collections::HashMap;
use std::sync::OnceLock;
use std::time::UNIX_EPOCH;

use serde_json::{json, Map, Value};

use crate::hierarchy::{fold_case, fold_query};

fn put(row: &mut Map<String, Value>, key: &str, value: impl Into<Value>) {
    row.insert(key.to_string(), value.into());
}

pub const OPERATOR_SUFFIXES: &[(&str, &str)] = &[
    ("!=", "!=="),
    (">>", ">>="),
    ("<<", "<<="),
    ("!", "!="),
    ("=", "=="),
    (">", ">="),
    ("<", "<="),
    ("", "="),
];
pub const TAG_TABLE_FIELDS: &[&str] = &[
    "genre",
    "collection",
    "director",
    "writer",
    "producer",
    "actor",
    "country",
    "label",
    "network",
    "similar",
    "mood",
    "style",
    "format",
    "subformat",
    "tag",
    "make",
    "model",
    "lens",
    "aperture",
    "exposure",
    "iso",
    "device",
    "autotag",
    "place",
    "channel",
    "mediaProcessingTarget",
    "sharedWith",
];
pub const UNLISTED_STRING_FIELDS: &[&str] = &[
    "titleSort",
    "summary",
    "tagline",
    "originalTitle",
    "theme",
    "banner",
    "art",
    "thumb",
    "slug",
];
pub const RESOLUTIONS: &[&str] = &[
    "sd", "480", "576", "720", "1080", "2k", "2.7k", "4k", "8k", "16k",
];
const FRACTIONAL_REFUSED: &[&str] = &["viewCount", "viewOffset"];
const STRICT_INT32_FIELDS: &[&str] = &["decade", "viewCount", "viewOffset"];
const NULL_IS_ZERO_FIELDS: &[&str] = &["viewCount", "viewOffset"];
const NULL_IS_ZERO_DATES: &[&str] = &["lastViewedAt", "lastRatedAt", "lastSkippedAt"];
const NULL_SENTINEL_FIELDS: &[&str] = &[
    "rating",
    "audienceRating",
    "userRating",
    "absoluteIndex",
    "parent",
];
const OPERATOR_BLIND_BOOLEANS: &[&str] = &["unmatched"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FilterError {
    pub status: u16,
    pub detail: String,
}

pub type ResolveFn<'a> = &'a dyn Fn(&str, &Value) -> Vec<Value>;
impl FilterError {
    fn new(detail: impl Into<String>) -> Self {
        refusal(500, detail)
    }
}

thread_local! {
    static REFUSAL: std::cell::RefCell<Option<(u16, String)>> = const { std::cell::RefCell::new(None) };
}

/// A `FilterError` raised on this thread, noted for the request that raised it: the
/// oracle's `handle()` catches the exception, logs `REFUSED <route>: <detail>` at
/// WARNING and answers `rex: <detail>`, where the native routes carry only the
/// status back up. The runtime clears the note before a request and reads it after.
pub fn refusal(status: u16, detail: impl Into<String>) -> FilterError {
    let detail = detail.into();
    REFUSAL.with(|note| *note.borrow_mut() = Some((status, detail.clone())));
    FilterError { status, detail }
}

pub fn clear_refusal() {
    REFUSAL.with(|note| note.borrow_mut().take());
}

pub fn take_refusal() -> Option<(u16, String)> {
    REFUSAL.with(|note| note.borrow_mut().take())
}

/// Python's `repr()` of a `str`: single quotes unless the text holds one and no
/// double quote, with the escapes `%r` writes into every refusal detail.
pub fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => out.push_str(&format!("\\x{:02x}", c as u32)),
            c if (0x80..0xa0).contains(&(c as u32)) => out.push_str(&format!("\\x{:02x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `repr()` of a catalog value the way the oracle holds it: a str, a number or None.
pub fn py_repr_value(value: &Value) -> String {
    match value {
        Value::String(text) => py_repr(text),
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        other => other.to_string(),
    }
}

fn is_tag_field(field: &str) -> bool {
    TAG_TABLE_FIELDS.contains(&field)
}
pub fn language_code(value: &str) -> String {
    static TAGS: OnceLock<HashMap<String, String>> = OnceLock::new();
    let tags = TAGS.get_or_init(|| {
        let mut tags = HashMap::new();
        if let Ok(document) = serde_json::from_str::<HashMap<String, Vec<String>>>(include_str!(
            "../../languages.json"
        )) {
            for (code, values) in document {
                if let Some(tag) = values.get(1) {
                    tags.insert(code, tag.clone());
                }
            }
        }
        tags
    });
    tags.get(value)
        .cloned()
        .unwrap_or_else(|| value.to_string())
}

/// Parse the date forms accepted by the measured PMS filter surface.
///
/// Relative values use the server's fixed units (`mon` is thirty days), ISO
/// values are interpreted as local midnight/seconds just like Python's
/// `time.mktime`, and surrounding whitespace is refused. `now` is injectable
/// for deterministic tests and callers that already have a request clock.
pub fn as_epoch(value: &str, now: Option<f64>) -> Result<f64, FilterError> {
    if value != value.trim() {
        return Err(FilterError::new(format!("unparseable date {}", py_repr(value))));
    }
    let is_integer = !value.is_empty()
        && value
            .strip_prefix(['+', '-'])
            .unwrap_or(value)
            .chars()
            .all(|c| c.is_ascii_digit());
    if is_integer {
        let parsed = value
            .parse::<i64>()
            .map_err(|_| FilterError::new(format!("date out of range {}", py_repr(value))))?;
        return Ok(parsed as f64);
    }
    if value.contains('.') {
        let unsigned = value.strip_prefix(['+', '-']).unwrap_or(value);
        if unsigned
            .chars()
            .filter(|c| *c != '.')
            .all(|c| c.is_ascii_digit())
            && unsigned.matches('.').count() == 1
        {
            return Err(FilterError::new(format!("fractional date {}", py_repr(value))));
        }
    }
    let (sign, rest) = match value.as_bytes().first() {
        Some(b'+') => (1.0, &value[1..]),
        Some(b'-') => (-1.0, &value[1..]),
        _ => (-1.0, value),
    };
    let units = [
        ("weeks", 604_800.0),
        ("days", 86_400.0),
        ("mon", 2_592_000.0),
        ("week", 604_800.0),
        ("day", 86_400.0),
        ("year", 31_536_000.0),
        ("years", 31_536_000.0),
        ("y", 31_536_000.0),
        ("w", 604_800.0),
        ("d", 86_400.0),
        ("h", 3_600.0),
        ("m", 60.0),
        ("s", 1.0),
    ];
    for (unit, seconds) in units {
        if let Some(amount) = rest.strip_suffix(unit) {
            if !amount.is_empty() && amount.chars().all(|c| c.is_ascii_digit()) {
                let amount = amount
                    .parse::<f64>()
                    .map_err(|_| FilterError::new(format!("unparseable date {}", py_repr(value))))?;
                let base = now.unwrap_or_else(|| {
                    UNIX_EPOCH
                        .elapsed()
                        .map(|duration| duration.as_secs_f64())
                        .unwrap_or(0.0)
                });
                return Ok(base + sign * amount * seconds);
            }
        }
    }
    if let Some(epoch) = parse_iso_epoch(value) {
        return Ok(epoch);
    }
    value
        .parse::<f64>()
        .map_err(|_| FilterError::new(format!("unparseable date {}", py_repr(value))))
}

pub fn as_number(value: &str) -> Result<f64, FilterError> {
    value
        .trim()
        .parse::<f64>()
        .map_err(|_| FilterError::new(format!("unparseable number {}", py_repr(value))))
}

fn parse_iso_epoch(value: &str) -> Option<f64> {
    let bytes = value.as_bytes();
    let date = |start: usize| -> Option<i64> {
        let text = value.get(start..start + 4)?;
        text.parse().ok()
    };
    if bytes.len() < 10
        || bytes.get(4) != Some(&b'-')
        || bytes.get(7) != Some(&b'-')
        || !bytes[0..4].iter().all(u8::is_ascii_digit)
        || !bytes[5..7].iter().all(u8::is_ascii_digit)
        || !bytes[8..10].iter().all(u8::is_ascii_digit)
    {
        return None;
    }
    let year = date(0)?;
    let month: u32 = value.get(5..7)?.parse().ok()?;
    let day: u32 = value.get(8..10)?.parse().ok()?;
    let (hour, minute, second) = if bytes.len() == 10 {
        (0, 0, 0)
    } else {
        if bytes.len() < 19
            || !matches!(bytes[10], b'T' | b' ')
            || bytes[13] != b':'
            || bytes[16] != b':'
            || !bytes[11..13].iter().all(u8::is_ascii_digit)
            || !bytes[14..16].iter().all(u8::is_ascii_digit)
            || !bytes[17..19].iter().all(u8::is_ascii_digit)
        {
            return None;
        }
        (
            value.get(11..13)?.parse().ok()?,
            value.get(14..16)?.parse().ok()?,
            value.get(17..19)?.parse().ok()?,
        )
    };
    if !(1..=12).contains(&month)
        || day == 0
        || day > days_in_month(year, month)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return None;
    }
    Some(local_epoch(year, month, day, hour, minute, second))
}

// Python's `time.mktime` interprets naive ISO values in the process-local
// timezone and applies its DST rules, as the C library's own `mktime` does.
fn local_epoch(year: i64, month: u32, day: u32, hour: u32, minute: u32, second: u32) -> f64 {
    let mut value: libc::tm = unsafe { std::mem::zeroed() };
    value.tm_year = (year - 1900) as libc::c_int;
    value.tm_mon = (month - 1) as libc::c_int;
    value.tm_mday = day as libc::c_int;
    value.tm_hour = hour as libc::c_int;
    value.tm_min = minute as libc::c_int;
    value.tm_sec = second as libc::c_int;
    value.tm_isdst = -1;
    crate::platform::local_mktime(&mut value) as f64
}

fn days_in_month(year: i64, month: u32) -> u32 {
    match month {
        2 if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

pub fn split_key(key: &str) -> (String, String) {
    for (suffix, operator) in OPERATOR_SUFFIXES {
        if !suffix.is_empty() && key.ends_with(suffix) {
            let name = &key[..key.len() - suffix.len()];
            if name.chars().any(|c| matches!(c, '<' | '>' | '!' | '=')) {
                return (key.into(), "=".into());
            }
            return (name.into(), (*operator).into());
        }
    }
    (key.into(), "=".into())
}
pub fn split_scope(name: &str) -> (String, String) {
    if let Some((scope, bare)) = name.rsplit_once('.') {
        (scope.into(), bare.into())
    } else {
        ("".into(), name.into())
    }
}
pub fn operator_allowed(kind: &str, field: &str, operator: &str) -> bool {
    if is_tag_field(field) {
        return operator == "=" || operator == "!=";
    }
    if field == "guid" {
        return operator == "=" || operator == "<=";
    }
    if kind == "tagId" {
        return operator == "=";
    }
    if kind == "string" {
        return UNLISTED_STRING_FIELDS.contains(&field)
            || ["=", "!=", "==", "!==", "<=", ">="].contains(&operator);
    }
    true
}

fn str_actual(value: &Value) -> Option<String> {
    match value {
        Value::String(v) => Some(v.clone()),
        Value::Number(v) => Some(v.to_string()),
        Value::Bool(v) => Some(v.to_string()),
        _ => None,
    }
}
fn values(actual: &Value) -> Vec<String> {
    fn nonempty(value: Option<String>) -> Vec<String> {
        value
            .into_iter()
            .filter(|value| !value.is_empty())
            .collect()
    }
    match actual {
        Value::Array(items) => items
            .iter()
            .flat_map(|v| {
                if let Some(map) = v.as_object() {
                    vec![
                        map.get("id").and_then(str_actual),
                        map.get("tag").and_then(str_actual),
                    ]
                    .into_iter()
                    .flatten()
                    .filter(|value| !value.is_empty())
                    .collect::<Vec<_>>()
                } else {
                    nonempty(str_actual(v))
                }
            })
            .collect(),
        Value::Null => vec![],
        v => nonempty(str_actual(v)),
    }
}
fn num(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}
fn actual_num(value: &Value) -> Result<f64, FilterError> {
    num(value).ok_or_else(|| FilterError::new(format!("unparseable number {}", py_repr_value(value))))
}
fn wanted_num(wanted: &str) -> Result<f64, FilterError> {
    wanted
        .trim()
        .parse()
        .map_err(|_| FilterError::new(format!("unparseable number {}", py_repr(wanted))))
}
fn date_actual(value: &Value) -> Result<Option<f64>, FilterError> {
    match value {
        Value::Number(value) => Ok(value.as_f64()),
        Value::String(value) => {
            let numeric = !value.is_empty()
                && value
                    .strip_prefix(['+', '-'])
                    .unwrap_or(value)
                    .chars()
                    .all(|c| c.is_ascii_digit() || c == '.');
            if numeric {
                value
                    .parse::<f64>()
                    .map(Some)
                    .map_err(|_| FilterError::new(format!("unparseable date {}", py_repr(value))))
            } else {
                as_epoch(value, None).map(Some)
            }
        }
        Value::Null => Ok(None),
        _ => Err(FilterError::new(format!("unparseable date {}", py_repr_value(value)))),
    }
}
fn bool_actual(value: &Value) -> bool {
    match value {
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_i64().unwrap_or(0) != 0,
        Value::String(v) => !v.is_empty(),
        _ => false,
    }
}

fn title_words(raw: &str) -> Vec<String> {
    raw.to_lowercase()
        .split_whitespace()
        .map(str::to_string)
        .collect()
}
fn raw_word_prefixes(wanted: &str, text: &str) -> bool {
    let terms = wanted
        .to_lowercase()
        .split(' ')
        .map(str::to_string)
        .collect::<Vec<_>>();
    let words = title_words(text);
    let mut pos = 0;
    for term in terms {
        while pos < words.len() && !words[pos].starts_with(&term) {
            pos += 1;
        }
        if pos == words.len() {
            return false;
        }
        pos += 1;
    }
    true
}
fn folded_word_run(wanted: &str, text: &str) -> bool {
    let terms = fold_query(wanted)
        .split(' ')
        .map(str::to_string)
        .collect::<Vec<_>>();
    if terms.iter().any(String::is_empty) {
        return false;
    }
    let words = fold_case(text)
        .split_whitespace()
        .map(str::to_string)
        .collect::<Vec<_>>();
    let mut pos = 0;
    for (i, term) in terms.iter().enumerate() {
        while pos < words.len()
            && !if i == terms.len() - 1 {
                words[pos].starts_with(term)
            } else {
                words[pos] == *term
            }
        {
            pos += 1;
        }
        if pos == words.len() {
            return false;
        }
        pos += 1;
    }
    true
}
fn original_phrase(wanted: &str, text: &str) -> bool {
    let wanted_lower = wanted.to_lowercase();
    let text_lower = text.to_lowercase();
    let terms = wanted_lower
        .split(' ')
        .filter(|x| !x.is_empty())
        .collect::<Vec<_>>();
    let words = text_lower.split_whitespace().collect::<Vec<_>>();
    if terms.is_empty() || words.len() < terms.len() {
        return false;
    }
    for start in 0..=words.len() - terms.len() {
        if terms.iter().enumerate().all(|(i, t)| {
            if i + 1 == terms.len() {
                words[start + i].starts_with(t)
            } else {
                words[start + i] == *t
            }
        }) {
            return true;
        }
    }
    false
}
fn title_match(wanted: &str, text: &str, sort: Option<&str>, original: Option<&str>) -> bool {
    if wanted.is_empty() {
        return true;
    }
    wanted.split(',').any(|part| {
        raw_word_prefixes(part, text)
            || folded_word_run(part, sort.unwrap_or(text))
            || original.map(|x| original_phrase(part, x)).unwrap_or(false)
    })
}

fn exact(operator: &str, actual: &Value, wanted: &str, field: &str) -> bool {
    let vals = values(actual);
    let wanted = if field == "contentRating" && wanted == "None" {
        ""
    } else {
        wanted
    };
    let hit = if wanted.is_empty() {
        if is_tag_field(field) {
            false
        } else if ["studio", "editionTitle"].contains(&field) {
            true
        } else {
            vals.is_empty()
        }
    } else if is_tag_field(field) {
        // Cycle 88, measured on kids and zen: a tag NAME matches ignoring the case of its
        // ASCII letters only (SQLite's NOCASE) -- `genre=ANIMATION` is `Animation`, and
        // `actor=ARINZé KENE` is `Arinzé Kene` while `ARINZÉ KENE` matches nothing.
        vals.iter().any(|x| x.eq_ignore_ascii_case(wanted))
    } else {
        vals.iter().any(|x| x == wanted)
    };
    if operator == "!=" {
        !hit
    } else if operator == "=" {
        hit
    } else {
        false
    }
}
fn tag_column(operator: &str, actual: &Value, wanted: &str, field: &str) -> bool {
    let text = values(actual).first().cloned().unwrap_or_default();
    let wanted = if field == "contentRating" && wanted == "None" {
        ""
    } else {
        wanted
    };
    match operator {
        "==" => text.to_lowercase() == wanted.to_lowercase(),
        "!==" => text.to_lowercase() != wanted.to_lowercase(),
        "<=" => text.as_str() <= wanted,
        "<<=" => text.as_str() < wanted,
        ">=" => text.as_str() >= wanted,
        _ => text.as_str() > wanted,
    }
}

/// Compare one typed field using the measured operator semantics.
pub fn compare(
    kind: &str,
    operator: &str,
    actual: &Value,
    wanted: &str,
    field: &str,
) -> Result<bool, FilterError> {
    let mut operator = operator.to_string();
    if field == "decade" {
        operator = "=".into();
    }
    // `location` is advertised as a tag by Meta, but its stored value is the
    // integer section id.  Shadow the type once; recursively calling compare
    // with the same field used to recurse until the process stack overflowed.
    let kind = if field == "location" { "integer" } else { kind };
    if kind == "resolution" {
        let wanted = wanted.trim();
        if !RESOLUTIONS.contains(&wanted) {
            return Err(FilterError::new(format!("unknown resolution {}", py_repr(wanted))));
        }
        return Ok(exact("=", actual, wanted, field));
    }
    if kind == "tag" {
        return Ok(
            if !is_tag_field(field) && !["=", "!="].contains(&operator.as_str()) {
                tag_column(&operator, actual, wanted, field)
            } else {
                exact(&operator, actual, wanted, field)
            },
        );
    }
    if kind == "audioLanguage" || kind == "subtitleLanguage" {
        let vals = values(actual)
            .into_iter()
            .map(|x| language_code(&x))
            .collect::<Vec<_>>();
        let wanted = language_code(wanted);
        if operator == "=" {
            return Ok(vals.iter().any(|x| x == &wanted));
        }
        let holds = |one: &String| match operator.as_str() {
            "!=" | "==" => one == &wanted,
            "!==" => one != &wanted,
            "<=" => one <= &wanted,
            "<<=" => one < &wanted,
            ">=" => one >= &wanted,
            _ => one > &wanted,
        };
        return Ok(!vals.iter().any(holds));
    }
    if kind == "media_string" {
        let vals = if actual.is_array() {
            values(actual)
        } else {
            vec![str_actual(actual).unwrap_or_default()]
        };
        return Ok(vals.iter().any(|v| match operator.as_str() {
            "=" | "==" => v.as_str() == wanted,
            "!=" | "!==" => v.as_str() != wanted,
            "<=" => v.as_str() <= wanted,
            "<<=" => v.as_str() < wanted,
            ">=" => v.as_str() >= wanted,
            _ => v.as_str() > wanted,
        }));
    }
    if kind == "tagId" {
        let wanted = wanted.trim();
        if wanted.is_empty() || !wanted.chars().all(|c| c.is_ascii_digit()) {
            return Err(FilterError::new(format!("bad tagId {}", py_repr(wanted))));
        }
        // Python parses arbitrary-size decimal input, then compares its
        // canonical decimal spelling.  Keep this string based so a huge valid
        // id is simply a non-match instead of a Rust integer panic.
        let canonical = wanted.trim_start_matches('0');
        let canonical = if canonical.is_empty() { "0" } else { canonical };
        return Ok(values(actual).iter().any(|v| v == canonical));
    }
    if kind == "boolean" {
        if actual.is_array() {
            let mut matched = false;
            for value in actual.as_array().into_iter().flatten() {
                if value.is_null() {
                    continue;
                }
                if compare(kind, &operator, value, wanted, field)? {
                    matched = true;
                    break;
                }
            }
            return Ok(matched);
        }
        let state = bool_actual(actual);
        if OPERATOR_BLIND_BOOLEANS.contains(&field) {
            return Ok(state == (wanted == "1"));
        }
        return Ok(
            if (operator == "=" && wanted == "1") || (operator == "!=" && wanted == "0") {
                state
            } else {
                !state
            },
        );
    }
    if kind == "id" {
        let target = wanted_num(wanted);
        if target.is_err() {
            return Ok(if operator == "==" {
                false
            } else if operator == "!==" {
                true
            } else {
                ["!=", "<<=", "<="].contains(&operator.as_str())
            });
        }
        let target = target.unwrap();
        let actual = if actual.is_null() {
            0.0
        } else {
            actual_num(actual)?
        };
        return Ok(match operator.as_str() {
            "==" => actual == target,
            "!==" => actual != target,
            ">>=" => actual > target,
            ">=" => actual >= target,
            "<<=" => actual < target,
            "<=" => actual <= target,
            "!=" => actual != target,
            _ => actual == target,
        });
    }
    if kind == "integer" {
        if STRICT_INT32_FIELDS.contains(&field)
            && (wanted.is_empty()
                || !wanted
                    .strip_prefix(['+', '-'])
                    .unwrap_or(wanted)
                    .chars()
                    .all(|c| c.is_ascii_digit())
                || wanted.parse::<i64>().map_or(true, |value| {
                    !(-2_147_483_648..2_147_483_648).contains(&value)
                }))
        {
            return Err(FilterError::new(format!("bad {} {}", field, py_repr(wanted))));
        }
        if FRACTIONAL_REFUSED.contains(&field) && wanted.contains('.') {
            return Err(FilterError::new(format!(
                "fractional value {} on {}",
                py_repr(wanted), field
            )));
        }
        let target = wanted_num(wanted)?;
        if NULL_SENTINEL_FIELDS.contains(&field)
            && operator == "="
            && wanted.starts_with('-')
            && wanted[1..].chars().all(|c| c == '0' || c == '1')
            && wanted[1..].trim_start_matches('0') == "1"
        {
            return Ok(actual.is_null() || actual == &Value::String(String::new()));
        }
        if actual.is_null() {
            return Ok(if NULL_IS_ZERO_FIELDS.contains(&field) {
                operator == "=" && target == 0.0 || ["<=", "<<="].contains(&operator.as_str())
            } else {
                false
            });
        }
        if actual.is_array() {
            let mut matched = false;
            for value in actual.as_array().into_iter().flatten() {
                if value.is_null() {
                    continue;
                }
                if compare(kind, &operator, value, wanted, field)? {
                    matched = true;
                    break;
                }
            }
            return Ok(matched);
        }
        if operator == "==" || operator == "!==" {
            let hit = str_actual(actual).unwrap_or_default() == wanted;
            return Ok(if operator == "==" { hit } else { !hit });
        }
        let actual_num = actual_num(actual)?;
        return Ok(match operator.as_str() {
            ">>=" => actual_num > target,
            ">=" => actual_num >= target,
            "<<=" => actual_num < target,
            "<=" => actual_num <= target,
            "!=" => actual_num != target,
            _ => actual_num == target,
        });
    }
    if kind == "date" {
        let moment = as_epoch(wanted, None)?;
        if NULL_IS_ZERO_DATES.contains(&field)
            && operator == "="
            && !(wanted
                .strip_prefix(['+', '-'])
                .unwrap_or(wanted)
                .chars()
                .all(|c| c.is_ascii_digit())
                && wanted
                    .parse::<i64>()
                    .is_ok_and(|value| (-2_147_483_648..2_147_483_648).contains(&value)))
        {
            return Err(FilterError::new(format!("bad {} {}", field, py_repr(wanted))));
        }
        let Some(actual) = date_actual(actual)? else {
            if NULL_IS_ZERO_DATES.contains(&field) {
                return Ok(["<=", "<<="].contains(&operator.as_str())
                    || (operator == "=" && moment == 0.0));
            }
            return Ok(false);
        };
        return Ok(match operator.as_str() {
            ">>=" => actual > moment,
            ">=" => actual >= moment,
            "<<=" => actual < moment,
            "<=" => actual <= moment,
            "!=" | "!==" => (actual - moment).abs() >= 1.0,
            _ => (actual - moment).abs() < 1.0,
        });
    }
    if actual.is_array() {
        return Ok(exact(&operator, actual, wanted, field));
    }
    let text = if actual.is_null() {
        String::new()
    } else {
        str_actual(actual).unwrap_or_default()
    };
    if field == "title" && operator == "=" {
        return Ok(title_match(wanted, &text, None, None));
    }
    if UNLISTED_STRING_FIELDS.contains(&field)
        && ["<=", "<<=", ">=", ">>="].contains(&operator.as_str())
    {
        let (a, b) = if field == "titleSort" {
            (text.to_lowercase(), wanted.to_lowercase())
        } else {
            (text, wanted.into())
        };
        return Ok(if ["<=", "<<="].contains(&operator.as_str()) {
            a < b
        } else {
            a > b
        });
    }
    if ["titleSort", "summary", "tagline"].contains(&field)
        && ["=", "!="].contains(&operator.as_str())
    {
        let hit = text.to_lowercase() == wanted.to_lowercase();
        return Ok(if operator == "=" { hit } else { !hit });
    }
    Ok(match operator.as_str() {
        "==" => text.to_lowercase() == wanted.to_lowercase(),
        "!==" => text.to_lowercase() != wanted.to_lowercase(),
        // The guid prefix is CASE-SENSITIVE (kids: `guid=plex` every matched row,
        // `guid=PLEX` none); every other string's begins-with folds case.
        "<=" if field == "guid" => text.starts_with(wanted),
        "<=" => text.to_lowercase().starts_with(&wanted.to_lowercase()),
        ">=" => text.to_lowercase().ends_with(&wanted.to_lowercase()),
        "!=" => !text.to_lowercase().contains(&wanted.to_lowercase()),
        _ => text.to_lowercase().contains(&wanted.to_lowercase()),
    })
}

fn place(groups: &mut Vec<Vec<Value>>, pending: Option<&str>, node: Value) {
    if pending == Some("or") {
        groups.push(vec![node]);
    } else {
        groups.last_mut().unwrap().push(node);
    }
}
fn collapse(groups: Vec<Vec<Value>>) -> Option<Value> {
    let rules: Vec<Value> = groups
        .into_iter()
        .filter(|g| !g.is_empty())
        .map(|g| {
            if g.len() == 1 {
                g.into_iter().next().unwrap()
            } else {
                json!({"op":"and","rules":g})
            }
        })
        .collect();
    if rules.len() == 1 {
        Some(rules.into_iter().next().unwrap())
    } else if rules.is_empty() {
        None
    } else {
        Some(json!({"op":"or","rules":rules}))
    }
}

/// Parse ordered query parameters. `field_types` maps bare fields to the
/// advertised Plex field type; unknown fields are reported and ignored.
pub fn parse(
    pairs: &[(String, String)],
    field_types: &HashMap<String, String>,
    errors: &mut Vec<Value>,
) -> Option<Value> {
    let mut stack: Vec<Vec<Vec<Value>>> = vec![vec![vec![]]];
    let mut pending: Option<String> = None;
    let mut last: Option<Value> = None;
    let mut stray = false;
    for (k, v) in pairs {
        if k.starts_with("X-Plex-") || k.starts_with("include") || k.starts_with("exclude") {
            continue;
        }
        if k == "push" {
            stack.push(vec![vec![]]);
            pending = None;
            continue;
        }
        if k == "pop" {
            if stack.len() > 1 {
                let group = collapse(stack.pop().unwrap());
                if let Some(group) = group {
                    place(stack.last_mut().unwrap(), pending.as_deref(), group.clone());
                    last = Some(group);
                }
            } else {
                stray = true;
                errors.push(json!({"type":"field","value":k}));
            }
            pending = None;
            continue;
        }
        if k == "and" || k == "or" {
            if last.is_none() {
                errors.push(json!({"type":"field","value":k}));
                return None;
            }
            pending = Some(k.clone());
            continue;
        }
        if ["sort", "type", "group", "limit", "sourceType"].contains(&k.as_str()) {
            continue;
        }
        let (name, mut op) = split_key(k);
        let (scope, bare) = split_scope(&name);
        let Some(kind) = field_types.get(&bare) else {
            errors.push(json!({"type":"field","value":k}));
            continue;
        };
        if !operator_allowed(kind, &bare, &op) {
            errors.push(json!({"type":"field","value":k}));
            continue;
        }
        if bare == "guid" && op == "=" {
            op = "<=".into();
        }
        let alternatives = if bare == "title" && op == "=" && v.contains(',') {
            v.split(',').map(str::to_string).collect()
        } else {
            let a = v
                .split(',')
                .filter(|x| !x.is_empty())
                .map(str::to_string)
                .collect::<Vec<_>>();
            if a.is_empty() {
                vec![v.clone()]
            } else {
                a
            }
        };
        let leaves = alternatives
            .into_iter()
            .map(
                |value| json!({"field":bare,"scope":scope,"operator":op,"value":value,"type":kind}),
            )
            .collect::<Vec<_>>();
        let node = if leaves.len() == 1 {
            leaves[0].clone()
        } else {
            json!({"op":if op=="!="||op=="!=="{"and"}else{"or"},"rules":leaves})
        };
        place(stack.last_mut().unwrap(), pending.as_deref(), node.clone());
        last = Some(node);
        pending = None;
    }
    if pending.is_some() && last.is_some() {
        place(
            stack.last_mut().unwrap(),
            pending.as_deref(),
            last.clone().unwrap(),
        );
        errors.push(json!({"type":"field","value":"eof"}));
    }
    while stack.len() > 1 {
        errors.push(json!({"type":"mismatch","value":"eof","expected":"pop"}));
        let group = collapse(stack.pop().unwrap());
        if let Some(group) = group {
            place(stack.last_mut().unwrap(), None, group);
        }
    }
    let tree = collapse(stack.pop().unwrap());
    if stray && tree.as_ref().is_some_and(|v| v.get("op").is_none()) {
        return Some(json!({"op":"and","rules":[tree.unwrap()]}));
    }
    tree
}

pub fn evaluate(
    tree: &Value,
    row: &Value,
    value_of: &dyn Fn(&str, &Value) -> Value,
) -> Result<bool, FilterError> {
    evaluate_with(tree, row, value_of, None)
}

/// Evaluate a parsed tree while preserving filter failures and scoped row
/// resolution.  `resolve` is the injectable hierarchy lookup used by the
/// server layer; a scoped leaf succeeds when any resolved row satisfies it.
pub fn evaluate_with(
    tree: &Value,
    row: &Value,
    value_of: &dyn Fn(&str, &Value) -> Value,
    resolve: Option<ResolveFn<'_>>,
) -> Result<bool, FilterError> {
    if tree.is_null() {
        return Ok(true);
    }
    if let Some(op) = tree.get("op").and_then(Value::as_str) {
        // Borrowed, as is an unscoped candidate below: copying the rules and the whole
        // row for every rule of every row was most of a filtered listing (fun, 2026-10-11).
        let rules: &[Value] = tree.get("rules").and_then(Value::as_array).map_or(&[], Vec::as_slice);
        if op == "and" {
            for rule in rules {
                if !evaluate_with(rule, row, value_of, resolve)? {
                    return Ok(false);
                }
            }
            return Ok(true);
        }
        for rule in rules {
            if evaluate_with(rule, row, value_of, resolve)? {
                return Ok(true);
            }
        }
        return Ok(false);
    }
    let field = tree.get("field").and_then(Value::as_str).unwrap_or("");
    let kind = tree.get("type").and_then(Value::as_str).unwrap_or("string");
    let operator = tree.get("operator").and_then(Value::as_str).unwrap_or("=");
    let wanted = tree.get("value").and_then(str_actual).unwrap_or_default();
    let scope = tree.get("scope").and_then(Value::as_str).filter(|scope| !scope.is_empty());
    let candidates: Vec<std::borrow::Cow<'_, Value>> = match (resolve, scope) {
        (Some(resolve), Some(scope)) => resolve(scope, row)
            .into_iter()
            .map(std::borrow::Cow::Owned)
            .collect(),
        _ => vec![std::borrow::Cow::Borrowed(row)],
    };
    for candidate in candidates {
        if field == "title" && operator == "=" {
            // Cycle 18, measured on zen: the bare `title=` matcher -- and ONLY it --
            // also reads the row's ORIGINAL title, and (cycle 32) its folded pass
            // reads the stored SORT title; both travel with the value in Python.
            if let Some(object) = candidate.as_object() {
                let text = value_of(field, &candidate);
                let text = str_actual(&text).unwrap_or_default();
                let sort = crate::hierarchy::stored_sort_title(object);
                let original = object
                    .get("originalTitle")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                if title_match(&wanted, &text, Some(&sort), Some(&original)) {
                    return Ok(true);
                }
                continue;
            }
        }
        if compare(kind, operator, &value_of(field, &candidate), &wanted, field)? {
            return Ok(true);
        }
    }
    Ok(false)
}

pub fn sort_terms(spec: &str) -> Vec<(String, bool, String)> {
    spec.split(',')
        .filter_map(|term| {
            let parts = term
                .trim()
                .split(':')
                .map(str::to_string)
                .collect::<Vec<_>>();
            if parts.first().is_none_or(String::is_empty) {
                None
            } else {
                let desc = parts.iter().skip(1).any(|x| x == "desc");
                let seed = parts
                    .iter()
                    .skip(1)
                    .find(|x| !matches!(x.as_str(), "asc" | "desc" | "nocollate"))
                    .cloned()
                    .unwrap_or_default();
                Some((parts[0].clone(), desc, seed))
            }
        })
        .collect()
}
pub fn orders(spec: &str, supported: &dyn Fn(&str) -> bool) -> Result<Vec<Value>, FilterError> {
    sort_terms(spec)
        .into_iter()
        .map(|(field, desc, _)| {
            if !supported(&field) {
                return Err(FilterError::new(format!("unsupported sort key {}", field)));
            }
            Ok(json!({"field":field,"ascending":!desc}))
        })
        .collect()
}
pub fn source_order(value: Option<&str>) -> Result<Option<Vec<Value>>, FilterError> {
    let Some(value) = value else { return Ok(None) };
    let digits = value.strip_prefix(['+', '-']).unwrap_or(value);
    if digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
        return Err(FilterError::new(format!("bad sourceType {}", py_repr(value))));
    }
    let n: i64 = value
        .parse()
        .map_err(|_| FilterError::new(format!("bad sourceType {}", py_repr(value))))?;
    if !(-2_147_483_648..2_147_483_648).contains(&n) {
        return Err(FilterError::new(format!("bad sourceType {}", py_repr(value))));
    }
    Ok((n != 1).then(|| vec![json!({"field":"id","ascending":true})]))
}
pub fn describe(tree: &Value) -> Option<Value> {
    describe_with(tree, &|_field, _scope| String::new(), &|_field, _value| {
        None
    })
}

/// Render a parsed tree with injectable field and tag value titles.  Keeping
/// these lookups outside the parser preserves the pure filter core while
/// retaining the `/query/parse` descriptor shape.
pub fn describe_with(
    tree: &Value,
    title_of: &dyn Fn(&str, &str) -> String,
    value_title_of: &dyn Fn(&str, &str) -> Option<String>,
) -> Option<Value> {
    if tree.is_null() {
        return None;
    }
    if let Some(op) = tree.get("op") {
        let rules = tree
            .get("rules")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        return Some(
            json!({"type":"composite","operator":op,"Rule":rules.iter().filter_map(|rule| describe_with(rule, title_of, value_title_of)).collect::<Vec<_>>() }),
        );
    }
    let mut out = Map::new();
    if tree
        .get("scope")
        .and_then(Value::as_str)
        .is_some_and(|v| !v.is_empty())
    {
        out.insert(
            "type".into(),
            tree.get("scope").cloned().unwrap_or(Value::Null),
        );
    }
    if let Some(field) = tree.get("field") {
        out.insert("field".into(), field.clone());
        if let Some(operator) = tree.get("operator") {
            out.insert("operator".into(), operator.clone());
        }
        if field != "type" {
            out.insert(
                "fieldTitle".into(),
                Value::String(title_of(
                    field.as_str().unwrap_or_default(),
                    tree.get("scope")
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                )),
            );
            if let (Some(name), Some(value)) =
                (field.as_str(), tree.get("value").and_then(str_actual))
            {
                if is_tag_field(name) {
                    if let Some(title) = value_title_of(name, &value) {
                        out.insert("valueTitle".into(), Value::String(title));
                    }
                }
            }
        }
        if let Some(value) = tree.get("value") {
            out.insert("value".into(), value.clone());
        }
    }
    Some(Value::Object(out))
}
pub fn document(
    type_number: i64,
    tree: &Value,
    order: Option<Vec<Value>>,
    limit: Option<i64>,
    group: Option<Value>,
    errors: &[Value],
) -> Value {
    let descriptor = Descriptor {
        title_of: &|_field, _scope| String::new(),
        value_title_of: &|_field, _value| None,
    };
    document_with(type_number, tree, order, limit, group, errors, &descriptor)
}

pub struct Descriptor<'a> {
    pub title_of: &'a dyn Fn(&str, &str) -> String,
    pub value_title_of: &'a dyn Fn(&str, &str) -> Option<String>,
}

pub fn document_with(
    type_number: i64,
    tree: &Value,
    order: Option<Vec<Value>>,
    limit: Option<i64>,
    group: Option<Value>,
    errors: &[Value],
    descriptor: &Descriptor<'_>,
) -> Value {
    let mut body = Map::new();
    put(&mut body, "type", type_number);
    if let Some(rule) = describe_with(tree, descriptor.title_of, descriptor.value_title_of) {
        put(&mut body, "Rule", rule);
    }
    if let Some(order) = order.filter(|x| !x.is_empty()) {
        put(&mut body, "Order", order);
    }
    if let Some(limit) = limit {
        put(&mut body, "Limit", json!([{"limit":limit}]));
    }
    if let Some(group) = group {
        put(&mut body, "Group", group);
    }
    if !errors.is_empty() {
        put(&mut body, "Error", errors.to_vec());
    }
    json!({"Query":body})
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn operators() {
        assert_eq!(split_key("year>>"), ("year".into(), ">>=".into()));
        assert_eq!(split_key("genre"), ("genre".into(), "=".into()));
    }
    #[test]
    fn title() {
        assert!(!title_match(
            "Th\u{e9} Zarquon",
            "The Zarquon Machine",
            Some("Zarquon Machine"),
            Some("Le Salaire de la Peur")
        ));
        assert!(title_match(
            "The Zarquon",
            "The Zarquon Machine",
            Some("Zarquon Machine"),
            Some("")
        ));
        assert!(title_match(
            "Z\u{e1}rquon Machine",
            "The Zarquon Machine",
            Some("Zarquon Machine"),
            Some("")
        ));
        assert!(title_match("Fire", "Firefly", None, None));
        assert!(!title_match("ire", "Firefly", None, None));
    }
    #[test]
    fn parse_duplicate() {
        let mut types = HashMap::new();
        types.insert("year".into(), "integer".into());
        let mut errors = vec![];
        let tree = parse(
            &[
                ("year".into(), "2020".into()),
                ("year".into(), "2021".into()),
            ],
            &types,
            &mut errors,
        );
        assert!(tree.is_some());
    }

    #[test]
    fn date_parser_keeps_measured_forms_strict() {
        let now = as_epoch("-1d", Some(1_000.0)).expect("relative date");
        assert_eq!(now, -86_400.0 + 1_000.0);
        assert!(as_epoch("2020-01-01", Some(1_000.0)).is_ok());
        assert!(as_epoch("2020-01-01T00:00:00", Some(1_000.0)).is_ok());
        assert!(as_epoch("-1mo", Some(1_000.0)).is_err());
        assert!(as_epoch(" 1", Some(1_000.0)).is_err());
        assert!(as_epoch("1.5", Some(1_000.0)).is_err());
    }

    #[test]
    fn measured_location_and_huge_tag_id_do_not_recurse_or_panic() {
        assert!(compare("tag", "=", &json!(1), "1", "location").unwrap());
        assert!(!compare("tagId", "=", &json!([7]), &"9".repeat(100), "tagId").unwrap());
        assert!(compare("string", "=", &json!("Éclair"), "éclair", "titleSort").unwrap());
    }
}
