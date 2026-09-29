//! Catalog hierarchy and the measured Plex row normalisation rules.

use std::collections::HashMap;

use serde_json::{json, Map, Value};
use sha1::{Digest, Sha1};

type Row = Map<String, Value>;

pub const SHOW_BASE: i64 = 20_000;
pub const SEASON_BASE: i64 = 30_000;
pub const MOVIE_SECTION: &str = "1";
pub const SHOW_SECTION: &str = "2";
pub const SPECIALS_TITLE: &str = "Specials";
pub const PERSON_TAG_ELEMENTS: &[&str] = &["Director", "Writer", "Role", "Producer"];

fn str_value(row: &Row, key: &str) -> Option<String> {
    row.get(key).and_then(|value| match value {
        Value::String(value) => Some(value.clone()),
        Value::Number(value) => Some(value.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        _ => None,
    })
}
fn int_value(row: &Row, key: &str) -> Option<i64> {
    row.get(key).and_then(|value| match value {
        Value::Number(n) => n.as_i64().or_else(|| n.as_u64().map(|n| n as i64)),
        Value::String(s) => s.parse().ok(),
        _ => None,
    })
}
fn bool_value(row: &Row, key: &str) -> bool {
    row.get(key)
        .map(|v| match v {
            Value::Bool(v) => *v,
            Value::Number(v) => v.as_i64().unwrap_or(0) != 0,
            Value::String(v) => v == "1" || v == "true",
            _ => false,
        })
        .unwrap_or(false)
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
fn obj() -> Row {
    Map::new()
}

fn sha1_hex(seed: &str) -> String {
    let mut hasher = Sha1::new();
    hasher.update(seed.as_bytes());
    format!("{:x}", hasher.finalize())
}
pub fn uuid(seed: &str) -> String {
    sha1_hex(&format!("plexsim/{}", seed))
}
pub fn plex_guid(kind: &str, seed: &str) -> String {
    format!(
        "plex://{}/{}",
        kind,
        &sha1_hex(&format!("plexsim/{}/{}", kind, seed))[..24]
    )
}

pub fn sections(name: &str) -> Vec<Value> {
    vec![
        json!({"key":MOVIE_SECTION,"type":"movie","title":"Debrid Movies","uuid":uuid(&format!("section/1/{}",name))}),
        json!({"key":SHOW_SECTION,"type":"show","title":"Debrid TV","uuid":uuid(&format!("section/2/{}",name))}),
    ]
}

const TITLE_FOLDS: &[(char, char)] = &[
    ('÷', '/'),
    ('Ø', '0'),
    ('ø', '0'),
    ('À', 'A'),
    ('Á', 'A'),
    ('Â', 'A'),
    ('Ã', 'A'),
    ('Ä', 'A'),
    ('Å', 'A'),
    ('Ǎ', 'A'),
    ('à', 'a'),
    ('á', 'a'),
    ('â', 'a'),
    ('ã', 'a'),
    ('ä', 'a'),
    ('å', 'a'),
    ('ā', 'a'),
    ('ǎ', 'a'),
    ('Ç', 'C'),
    ('Č', 'C'),
    ('ç', 'c'),
    ('č', 'c'),
    ('Ð', 'D'),
    ('Ď', 'D'),
    ('ď', 'd'),
    ('Æ', 'E'),
    ('È', 'E'),
    ('É', 'E'),
    ('Ê', 'E'),
    ('Ë', 'E'),
    ('Ě', 'E'),
    ('æ', 'e'),
    ('è', 'e'),
    ('é', 'e'),
    ('ê', 'e'),
    ('ë', 'e'),
    ('ē', 'e'),
    ('ě', 'e'),
    ('Ǧ', 'G'),
    ('ǧ', 'g'),
    ('Ì', 'I'),
    ('Í', 'I'),
    ('Î', 'I'),
    ('Ï', 'I'),
    ('Ǐ', 'I'),
    ('ì', 'i'),
    ('í', 'i'),
    ('î', 'i'),
    ('ï', 'i'),
    ('ī', 'i'),
    ('ǐ', 'i'),
    ('ǰ', 'j'),
    ('Ǩ', 'K'),
    ('ǩ', 'k'),
    ('Ľ', 'L'),
    ('ľ', 'l'),
    ('Ñ', 'N'),
    ('Ň', 'N'),
    ('ñ', 'n'),
    ('ň', 'n'),
    ('Ò', 'O'),
    ('Ó', 'O'),
    ('Ô', 'O'),
    ('Õ', 'O'),
    ('Ö', 'O'),
    ('ð', 'O'),
    ('Ǒ', 'O'),
    ('ò', 'o'),
    ('ó', 'o'),
    ('ô', 'o'),
    ('õ', 'o'),
    ('ö', 'o'),
    ('ō', 'o'),
    ('ǒ', 'o'),
    ('Þ', 'P'),
    ('þ', 'p'),
    ('Ř', 'R'),
    ('ř', 'r'),
    ('Š', 'S'),
    ('ß', 's'),
    ('š', 's'),
    ('Ť', 'T'),
    ('ť', 't'),
    ('Ù', 'U'),
    ('Ú', 'U'),
    ('Û', 'U'),
    ('Ü', 'U'),
    ('Ǔ', 'U'),
    ('ù', 'u'),
    ('ú', 'u'),
    ('û', 'u'),
    ('ü', 'u'),
    ('ū', 'u'),
    ('ǔ', 'u'),
    ('×', 'x'),
    ('Ý', 'Y'),
    ('ý', 'y'),
    ('ÿ', 'y'),
    ('Ž', 'Z'),
    ('ž', 'z'),
];

pub fn fold_title(text: &str) -> String {
    text.chars()
        .filter(|c| !('̀'..='ͯ').contains(c))
        .map(|c| {
            TITLE_FOLDS
                .iter()
                .find(|(from, _)| *from == c)
                .map(|(_, to)| *to)
                .unwrap_or(c)
        })
        .collect()
}
pub fn fold_case(text: &str) -> String {
    // Python's measured matcher uses simple casefolding: keep a one-codepoint
    // casefold expansion, but fall back to the character's lower form when a
    // fold expands (for example, `ß` stays `ß` while `İ` becomes `i\u{307}`).
    // Taking only the first Rust lowercase scalar silently dropped the dot in
    // the latter case and changed a title query's word boundaries.
    text.chars()
        .flat_map(|character| {
            let folded: String = match character {
                'ſ' => "s".to_string(),
                'K' => "k".to_string(),
                _ => character.to_lowercase().collect(),
            };
            if folded.chars().count() == 1 {
                folded
            } else {
                character.to_lowercase().collect()
            }
            .chars()
            .collect::<Vec<_>>()
        })
        .collect()
}
pub fn fold_query(text: &str) -> String {
    fold_case(&fold_title(text))
}

fn strip_leading(text: &str) -> String {
    text.trim_start_matches(|c: char| {
        matches!(
            c,
            '"' | '\''
                | '('
                | '-'
                | '.'
                | '['
                | '{'
                | '~'
                | ' '
                | '\t'
                | '\r'
                | '\n'
                | '\u{c}'
                | '\u{b}'
        ) || (c as u32 >= 0x80 && c as u32 <= 0xbf)
            || (c as u32 >= 0x2000 && c as u32 <= 0x2fff)
    })
    .to_string()
}
pub fn sort_title(title: &str) -> String {
    let folded = fold_title(title);
    let trimmed = strip_leading(&folded);
    // A title made of nothing but strippable characters keeps its own text: measured,
    // a title of apostrophes or of opening brackets carries no titleSort at all.
    let base = if trimmed.is_empty() { folded } else { trimmed };
    // `(?i)^(the|a|an)\s+`: the article and every whitespace character after it.
    let lower = base.to_ascii_lowercase();
    for article in ["the", "an", "a"] {
        if lower.starts_with(article) {
            let rest = &base[article.len()..];
            let after = rest.trim_start_matches(char::is_whitespace);
            if after.len() < rest.len() {
                return after.to_string();
            }
        }
    }
    base
}
fn generated_title(title: &str) -> bool {
    title == "Specials"
        || title == "[Unknown Season]"
        || (title.starts_with("Season ") && title[7..].chars().all(|c| c.is_ascii_digit()))
}
pub fn stored_title(row: &Row) -> String {
    let title = str_value(row, "title").unwrap_or_default();
    if str_value(row, "type").as_deref() == Some("season") && generated_title(&title) {
        String::new()
    } else {
        title
    }
}
pub fn stored_sort_title(row: &Row) -> String {
    let title = str_value(row, "title").unwrap_or_default();
    // Python's `if not row.get("titleSort")`: a stored sort title -- even one
    // equal to the generated title (kids' season 7, cycle 31) -- counts; only a
    // row storing none (or an empty one) stores the empty string.
    if str_value(row, "type").as_deref() == Some("season")
        && generated_title(&title)
        && str_value(row, "titleSort").filter(|sort| !sort.is_empty()).is_none()
    {
        String::new()
    } else {
        str_value(row, "titleSort")
            .filter(|sort| !sort.is_empty())
            .unwrap_or(title)
    }
}
pub fn person_tag_key(name: &str) -> String {
    sha1_hex(&format!("plexsim-person:{}", name))[..24].to_string()
}
pub const DEFAULT_ULTRABLUR: &[(&str, &str)] = &[
    ("topLeft", "1a1a1e"),
    ("topRight", "24242a"),
    ("bottomRight", "15151a"),
    ("bottomLeft", "1f1f26"),
];
fn slug(title: &str) -> String {
    let mut out = String::new();
    let mut dash = false;
    for c in title.to_ascii_lowercase().chars() {
        if c.is_ascii_alphanumeric() {
            out.push(c);
            dash = false;
        } else if !dash && !out.is_empty() {
            out.push('-');
            dash = true;
        }
    }
    while out.ends_with('-') {
        out.pop();
    }
    if out.is_empty() {
        "item".into()
    } else {
        out
    }
}

fn stamp_path(path: &str, stamps: &HashMap<String, String>) -> String {
    let parts: Vec<&str> = path.split('/').collect();
    if path.starts_with("/library/metadata/") && parts.len() == 6 {
        if let Some(stamp) = stamps.get(parts[3]) {
            return format!("/library/metadata/{}/{}/{}", parts[3], parts[4], stamp);
        }
    }
    path.into()
}
fn artwork_fields() -> &'static [&'static str] {
    &[
        "thumb",
        "art",
        "banner",
        "theme",
        "composite",
        "parentThumb",
        "parentArt",
        "parentTheme",
        "grandparentThumb",
        "grandparentArt",
        "grandparentTheme",
    ]
}

pub fn set_season_count(show: &mut Row, seasons: &[Row]) {
    show.shift_remove("seasonCount");
    let specials = seasons
        .iter()
        .filter(|row| int_value(row, "index") == Some(0))
        .count();
    if specials == 0 {
        return;
    }
    let count = (seasons.len() - specials) as i64;
    let mut ordered = Map::new();
    for (key, value) in show.clone() {
        ordered.insert(key.clone(), value);
        if key == "childCount" {
            ordered.insert("seasonCount".into(), json!(count));
        }
    }
    if !ordered.contains_key("seasonCount") {
        ordered.insert("seasonCount".into(), json!(count));
    }
    *show = ordered;
}

pub fn normalize_rows(rows: &mut [Value]) {
    let mut by_key: HashMap<String, Row> = HashMap::new();
    for value in rows.iter() {
        if let Some(row) = value.as_object() {
            if let Some(key) = str_value(row, "ratingKey") {
                by_key.insert(key, row.clone());
            }
        }
    }
    // `titleSort` is a stored-value marker. It is usually a string, so parsing
    // it as a boolean loses the distinction between an agent-supplied sort title
    // and a generated one (Python's `row.get(...) is not None` semantics).
    let stored: HashMap<String, bool> = rows
        .iter()
        .filter_map(|v| v.as_object())
        .filter_map(|r| {
            let present = r.get("titleSort").is_some_and(|value| {
                !value.is_null()
                    && match value {
                        Value::String(text) => !text.is_empty(),
                        _ => true,
                    }
            });
            str_value(r, "ratingKey").map(|k| (k, present))
        })
        .collect();
    for value in rows.iter_mut() {
        let Some(row) = value.as_object_mut() else {
            continue;
        };
        let kind = str_value(row, "type").unwrap_or_default();
        if kind == "season" {
            if let Some(parent) = by_key.get(&str_value(row, "parentRatingKey").unwrap_or_default())
            {
                let art = str_value(row, "art").unwrap_or_default();
                let parent_art = str_value(parent, "art").unwrap_or_default();
                if !parent_art.is_empty() && art.rsplit('/').next() == parent_art.rsplit('/').next()
                {
                    put(row, "art", parent_art);
                }
            }
        }
    }
    // The Python implementation indexes mutable row objects. Refresh this
    // snapshot after the artwork rewrite so episode inheritance observes the
    // corrected season path rather than the pre-rewrite value.
    by_key = rows
        .iter()
        .filter_map(|v| v.as_object())
        .filter_map(|r| str_value(r, "ratingKey").map(|k| (k, r.clone())))
        .collect();
    for value in rows.iter_mut() {
        let Some(row) = value.as_object_mut() else {
            continue;
        };
        for element in PERSON_TAG_ELEMENTS {
            if let Some(tags) = row.get_mut(*element).and_then(Value::as_array_mut) {
                for tag in tags {
                    if let Some(tag) = tag.as_object_mut() {
                        if let Some(name) = str_value(tag, "tag") {
                            if !tag.contains_key("tagKey") {
                                put(tag, "tagKey", person_tag_key(&name));
                            }
                        }
                    }
                }
            }
        }
        if str_value(row, "type").as_deref() == Some("episode") {
            if let Some(season) = by_key.get(&str_value(row, "parentRatingKey").unwrap_or_default())
            {
                if let Some(art) = str_value(season, "art") {
                    put(row, "art", art);
                }
                let thumb = str_value(season, "thumb").unwrap_or_default();
                if !thumb.starts_with(&format!(
                    "/library/metadata/{}/",
                    str_value(season, "ratingKey").unwrap_or_default()
                )) {
                    row.shift_remove("parentThumb");
                }
            }
        }
        if matches!(
            str_value(row, "type").as_deref(),
            Some("show") | Some("season")
        ) {
            if let Some(key) = str_value(row, "key") {
                if !key.ends_with("/children") {
                    put(
                        row,
                        "key",
                        format!("{}/children", key.trim_end_matches('/')),
                    );
                }
            }
        }
    }
    let mut seasons_of: HashMap<String, Vec<Row>> = HashMap::new();
    for value in rows.iter_mut() {
        if let Some(row) = value.as_object_mut() {
            if str_value(row, "type").as_deref() == Some("season") {
                if int_value(row, "index") == Some(0)
                    && matches!(
                        str_value(row, "title").as_deref(),
                        None | Some("") | Some("Season 0")
                    )
                {
                    put(row, "title", SPECIALS_TITLE);
                }
                seasons_of
                    .entry(str_value(row, "parentRatingKey").unwrap_or_default())
                    .or_default()
                    .push(row.clone());
            }
        }
    }
    // A season-0 title correction is also visible through the same mutable
    // index when its episode rows are fixed in the next pass.
    by_key = rows
        .iter()
        .filter_map(|v| v.as_object())
        .filter_map(|r| str_value(r, "ratingKey").map(|k| (k, r.clone())))
        .collect();
    for value in rows.iter_mut() {
        let Some(row) = value.as_object_mut() else {
            continue;
        };
        if str_value(row, "type").as_deref() == Some("episode")
            && str_value(row, "parentTitle").as_deref() == Some("Season 0")
        {
            if let Some(season) = by_key.get(&str_value(row, "parentRatingKey").unwrap_or_default())
            {
                if int_value(season, "index") == Some(0) {
                    put(
                        row,
                        "parentTitle",
                        str_value(season, "title").unwrap_or_else(|| SPECIALS_TITLE.into()),
                    );
                }
            }
        } else if str_value(row, "type").as_deref() == Some("show") {
            let seasons = seasons_of
                .get(&str_value(row, "ratingKey").unwrap_or_default())
                .cloned()
                .unwrap_or_default();
            set_season_count(row, &seasons);
        }
    }
    for value in rows.iter_mut() {
        let Some(row) = value.as_object_mut() else {
            continue;
        };
        let kind = str_value(row, "type").unwrap_or_default();
        let title = str_value(row, "title").unwrap_or_default();
        let derived = sort_title(&title);
        if !derived.is_empty() && derived != title {
            put(row, "titleSort", derived);
        } else {
            row.shift_remove("titleSort");
        }
        if let Some(original) = str_value(row, "originalTitle") {
            let files: Vec<String> = row
                .get("Media")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_object)
                .flat_map(|m| {
                    m.get("Part")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                })
                .filter_map(Value::as_object)
                .filter_map(|p| str_value(p, "file"))
                .map(|f| f.rsplit('/').next().unwrap_or("").to_string())
                .collect();
            if files.contains(&original)
                || original == title
                || [
                    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts", ".wmv",
                    ".flv", ".mpg", ".mpeg",
                ]
                .iter()
                .any(|x| original.to_ascii_lowercase().ends_with(x))
            {
                row.shift_remove("originalTitle");
            }
        }
        if row.get("Field") == Some(&json!([{"locked":true,"name":"thumb"}])) {
            row.shift_remove("Field");
        }
        if row.get("Label") == Some(&json!([])) {
            row.shift_remove("Label");
        }
        if kind == "movie" {
            row.shift_remove("Location");
        }
        if matches!(kind.as_str(), "movie" | "show" | "episode") {
            row.shift_remove("rating");
        }
        if kind == "episode" && str_value(row, "contentRating").is_none() {
            if let Some(parent) =
                by_key.get(&str_value(row, "grandparentRatingKey").unwrap_or_default())
            {
                if let Some(rating) = str_value(parent, "contentRating") {
                    put(row, "contentRating", rating);
                }
            }
        }
        if kind == "episode" && row.get("audienceRating").is_some_and(|v| !v.is_null()) {
            if !row.contains_key("Rating") {
                put(
                    row,
                    "Rating",
                    json!([{"image":"themoviedb://image.rating","value":row.get("audienceRating").cloned().unwrap_or(Value::Null),"type":"audience"}]),
                );
            }
            if !row.contains_key("audienceRatingImage") {
                put(row, "audienceRatingImage", "themoviedb://image.rating");
            }
        }
        if kind == "show" {
            row.entry("index").or_insert(json!(1));
        }
        for key in ["theme", "parentTheme", "grandparentTheme"] {
            row.shift_remove(key);
        }
        if kind == "season" {
            if !bool_value(row, "skipCount") {
                row.shift_remove("skipCount");
            }
            if row.get("year").is_some()
                && !stored
                    .get(&str_value(row, "ratingKey").unwrap_or_default())
                    .copied()
                    .unwrap_or(false)
            {
                let parent = by_key.get(&str_value(row, "parentRatingKey").unwrap_or_default());
                row.shift_remove("year");
                if let Some(year) = parent.and_then(|p| p.get("year")).cloned() {
                    put(row, "parentYear", year);
                }
            }
            if row.get("year").is_some_and(|v| !v.is_null()) {
                row.shift_remove("parentYear");
            } else {
                row.shift_remove("year");
            }
        }
        if kind == "show" {
            if let Some(locations) = row.get_mut("Location").and_then(Value::as_array_mut) {
                for loc in locations {
                    if let Some(loc) = loc.as_object_mut() {
                        loc.shift_remove("id");
                    }
                }
            }
        }
        if kind == "episode" && str_value(row, "parentGuid") == str_value(row, "grandparentGuid") {
            if let Some(season) = by_key.get(&str_value(row, "parentRatingKey").unwrap_or_default())
            {
                if let Some(guid) = str_value(season, "guid") {
                    put(row, "parentGuid", guid);
                }
            }
        }
        if kind == "episode" && row.get("Chapter").is_none() {
            row.shift_remove("chapterSource");
        }
    }
    let stamps: HashMap<String, String> = rows
        .iter()
        .filter_map(|v| v.as_object())
        .filter_map(|r| {
            let key = str_value(r, "ratingKey")?;
            let stamp = int_value(r, "updatedAt")
                .or_else(|| int_value(r, "addedAt"))
                .unwrap_or(1)
                .to_string();
            Some((key, stamp))
        })
        .collect();
    for value in rows.iter_mut() {
        if let Some(row) = value.as_object_mut() {
            for field in artwork_fields() {
                if let Some(path) = str_value(row, field) {
                    let stamped = stamp_path(&path, &stamps);
                    if stamped != path {
                        put(row, field, stamped);
                    }
                }
            }
        }
    }
}

fn fill_real(rows: &mut [Value]) {
    for value in rows {
        let Some(row) = value.as_object_mut() else {
            continue;
        };
        let kind = str_value(row, "type").unwrap_or_default();
        let rk = str_value(row, "ratingKey").unwrap_or_default();
        if kind == "show" {
            let section = str_value(row, "librarySectionID").unwrap_or_default();
            row.entry("Location")
                .or_insert(json!([{"path":format!("/plexsim/{}/{}",section,rk)}]));
        }
        if kind == "show" || kind == "movie" {
            let title = str_value(row, "title").unwrap_or_default();
            row.entry("slug").or_insert(json!(slug(&title)));
            if let Some(rating) = row.get("rating").cloned().filter(|v| !v.is_null()) {
                row.entry("Rating").or_insert(
                    json!([{"image":"themoviedb://image.rating","value":rating,"type":"audience"}]),
                );
                row.entry("audienceRatingImage")
                    .or_insert(json!("themoviedb://image.rating"));
            }
        }
        if matches!(kind.as_str(), "show" | "movie" | "season" | "episode")
            && !row.contains_key("UltraBlurColors")
        {
            let mut colors = Map::new();
            for (k, v) in DEFAULT_ULTRABLUR {
                put(&mut colors, k, *v);
            }
            put(row, "UltraBlurColors", colors);
        }
        if kind == "season" {
            let title = str_value(row, "parentTitle").unwrap_or_default();
            row.entry("parentSlug").or_insert(json!(slug(&title)));
        }
        if kind == "episode" {
            let title = str_value(row, "grandparentTitle").unwrap_or_default();
            row.entry("grandparentSlug").or_insert(json!(slug(&title)));
        }
    }
}
fn stamp_artwork(rows: &mut [Value]) {
    for value in rows.iter_mut() {
        if let Some(row) = value.as_object_mut() {
            for field in ["thumb", "art"] {
                if row
                    .get(field)
                    .is_some_and(|v| !v.is_null() && !v.as_str().unwrap_or("").is_empty())
                {
                    let rk = str_value(row, "ratingKey").unwrap_or_default();
                    let version = int_value(row, "updatedAt")
                        .or_else(|| int_value(row, "addedAt"))
                        .unwrap_or(1);
                    put(
                        row,
                        field,
                        format!("/library/metadata/{}/{}/{}", rk, field, version),
                    );
                }
            }
        }
    }
    let by: HashMap<String, Row> = rows
        .iter()
        .filter_map(|v| v.as_object())
        .filter_map(|r| str_value(r, "ratingKey").map(|k| (k, r.clone())))
        .collect();
    for value in rows.iter_mut() {
        let Some(row) = value.as_object_mut() else {
            continue;
        };
        if str_value(row, "type").as_deref() == Some("season") {
            if let Some(show) = by.get(&str_value(row, "parentRatingKey").unwrap_or_default()) {
                if let Some(art) = str_value(show, "art") {
                    if row.get("_art_url").is_none() || row.get("_art_url") == show.get("_art_url")
                    {
                        put(row, "art", art);
                    }
                }
            }
        }
        if let Some(parent) = by.get(&str_value(row, "parentRatingKey").unwrap_or_default()) {
            if let Some(thumb) = str_value(parent, "thumb") {
                put(row, "parentThumb", thumb);
            }
        }
        if let Some(grand) = by.get(&str_value(row, "grandparentRatingKey").unwrap_or_default()) {
            if let Some(thumb) = str_value(grand, "thumb") {
                put(row, "grandparentThumb", thumb);
            }
            if let Some(art) = str_value(grand, "art") {
                put(row, "grandparentArt", art);
            }
        }
        if str_value(row, "type").as_deref() == Some("episode") {
            if let Some(parent) = by.get(&str_value(row, "parentRatingKey").unwrap_or_default()) {
                if let Some(art) = str_value(parent, "art") {
                    put(row, "art", art);
                }
            }
        }
    }
}

/// Build movie/show/season/episode rows from the metadata-enriched leaves.
pub fn build(mut leaves: Vec<Value>) -> Vec<Value> {
    let mut rows = Vec::new();
    let mut shows: HashMap<String, usize> = HashMap::new();
    let mut seasons: HashMap<(String, i64), usize> = HashMap::new();
    let mut next_show = SHOW_BASE;
    let mut next_season = SEASON_BASE;
    for leaf_value in leaves.iter_mut() {
        let Some(leaf) = leaf_value.as_object_mut() else {
            continue;
        };
        let is_episode = str_value(leaf, "_kind").as_deref() == Some("tv")
            && int_value(leaf, "_season").is_some()
            && int_value(leaf, "_episode").is_some();
        if !is_episode {
            put(leaf, "type", "movie");
            put(leaf, "librarySectionID", 1);
            put(leaf, "librarySectionTitle", "Debrid Movies");
            put(leaf, "librarySectionKey", "/library/sections/1");
            for key in ["index", "parentIndex", "grandparentTitle"] {
                leaf.shift_remove(key);
            }
            rows.push(leaf_value.clone());
            continue;
        }
        let tmdb = str_value(leaf, "_tmdb_id").unwrap_or_default();
        let season_number = int_value(leaf, "_season").unwrap();
        let show_index = if let Some(index) = shows.get(&tmdb) {
            *index
        } else {
            next_show += 1;
            let rk = next_show.to_string();
            let mut show = obj();
            put(&mut show, "ratingKey", rk.clone());
            put(
                &mut show,
                "key",
                format!("/library/metadata/{}/children", rk),
            );
            put_opt(
                &mut show,
                "guid",
                str_value(leaf, "_show_guid").or_else(|| str_value(leaf, "guid")),
            );
            put(&mut show, "type", "show");
            put_opt(
                &mut show,
                "title",
                str_value(leaf, "_show_title").or_else(|| str_value(leaf, "title")),
            );
            put(
                &mut show,
                "summary",
                str_value(leaf, "_show_summary").unwrap_or_default(),
            );
            put_opt(&mut show, "year", int_value(leaf, "_show_year"));
            put(
                &mut show,
                "thumb",
                format!("/library/metadata/{}/thumb/1", rk),
            );
            put(&mut show, "art", format!("/library/metadata/{}/art/1", rk));
            put(&mut show, "librarySectionID", 2);
            put(&mut show, "librarySectionTitle", "Debrid TV");
            put(&mut show, "librarySectionKey", "/library/sections/2");
            for key in ["addedAt", "updatedAt"] {
                put_opt(&mut show, key, int_value(leaf, key));
            }
            put(&mut show, "childCount", 0);
            put(&mut show, "leafCount", 0);
            put(&mut show, "viewedLeafCount", 0);
            for key in [
                "contentRating",
                "studio",
                "Genre",
                "Country",
                "Role",
                "rating",
                "audienceRating",
                "tagline",
            ] {
                if let Some(v) = leaf.get(key).filter(|v| !v.is_null()) {
                    show.insert(key.into(), v.clone());
                }
            }
            if let Some(v) = leaf.get("_show_external_guids") {
                show.insert("Guid".into(), v.clone());
            }
            put_opt(&mut show, "_poster_url", leaf.get("_show_poster").cloned());
            put_opt(&mut show, "_art_url", leaf.get("_show_art").cloned());
            rows.push(Value::Object(show));
            let idx = rows.len() - 1;
            shows.insert(tmdb.clone(), idx);
            idx
        };
        let show = rows[show_index].as_object().cloned().unwrap();
        let season_key = (tmdb.clone(), season_number);
        let season_index = if let Some(index) = seasons.get(&season_key) {
            *index
        } else {
            next_season += 1;
            let rk = next_season.to_string();
            let mut season = obj();
            let season_title = if season_number == 0 {
                SPECIALS_TITLE.to_string()
            } else {
                format!("Season {}", season_number)
            };
            put(&mut season, "ratingKey", rk.clone());
            put(
                &mut season,
                "key",
                format!("/library/metadata/{}/children", rk),
            );
            put(&mut season, "type", "season");
            put_opt(&mut season, "guid", str_value(leaf, "_season_guid"));
            put(&mut season, "title", season_title);
            put(&mut season, "index", season_number);
            put(&mut season, "parentIndex", 1);
            put_opt(&mut season, "year", int_value(leaf, "_season_year"));
            put_opt(&mut season, "Guid", leaf.get("_season_guids").cloned());
            put(
                &mut season,
                "summary",
                str_value(leaf, "_season_summary").unwrap_or_default(),
            );
            put_opt(
                &mut season,
                "parentRatingKey",
                str_value(&show, "ratingKey"),
            );
            put_opt(
                &mut season,
                "parentKey",
                str_value(&show, "key").map(|x| x.trim_end_matches("/children").to_string()),
            );
            put_opt(&mut season, "parentTitle", str_value(&show, "title"));
            put_opt(&mut season, "parentThumb", str_value(&show, "thumb"));
            put_opt(&mut season, "parentGuid", str_value(&show, "guid"));
            put(
                &mut season,
                "thumb",
                str_value(&show, "thumb").unwrap_or_default(),
            );
            put(
                &mut season,
                "art",
                str_value(&show, "art").unwrap_or_default(),
            );
            put(&mut season, "librarySectionID", 2);
            put(&mut season, "librarySectionTitle", "Debrid TV");
            put(&mut season, "librarySectionKey", "/library/sections/2");
            for key in ["addedAt", "updatedAt"] {
                put_opt(&mut season, key, int_value(leaf, key));
            }
            put(&mut season, "leafCount", 0);
            put(&mut season, "viewedLeafCount", 0);
            put(&mut season, "parentStudio", str_value(&show, "studio"));
            put_opt(&mut season, "parentYear", int_value(&show, "year"));
            put_opt(
                &mut season,
                "_poster_url",
                leaf.get("_season_poster")
                    .cloned()
                    .or_else(|| leaf.get("_show_poster").cloned()),
            );
            put_opt(&mut season, "_art_url", leaf.get("_show_art").cloned());
            rows.push(Value::Object(season));
            let idx = rows.len() - 1;
            seasons.insert(season_key, idx);
            if let Some(show) = rows[show_index].as_object_mut() {
                let count = int_value(show, "childCount").unwrap_or(0);
                put(show, "childCount", count + 1);
            }
            idx
        };
        let season = rows[season_index].as_object().cloned().unwrap();
        // The leaf's own art URL becomes its poster. The show art remains the
        // row's background URL; the consumed source marker is removed first just
        // as the Python builder does.
        let episode_still = leaf.get("_art_url").cloned();
        leaf.shift_remove("_art_url");
        put(leaf, "type", "episode");
        let episode = int_value(leaf, "_episode").unwrap();
        put(leaf, "index", episode);
        put(leaf, "parentIndex", season_number);
        put(leaf, "librarySectionID", 2);
        put(leaf, "librarySectionTitle", "Debrid TV");
        put(leaf, "librarySectionKey", "/library/sections/2");
        put_opt(leaf, "parentRatingKey", str_value(&season, "ratingKey"));
        put_opt(
            leaf,
            "parentKey",
            str_value(&season, "key").map(|x| x.trim_end_matches("/children").to_string()),
        );
        put_opt(leaf, "parentTitle", str_value(&season, "title"));
        put_opt(leaf, "parentThumb", str_value(&season, "thumb"));
        put_opt(leaf, "grandparentRatingKey", str_value(&show, "ratingKey"));
        put_opt(
            leaf,
            "grandparentKey",
            str_value(&show, "key").map(|x| x.trim_end_matches("/children").to_string()),
        );
        put_opt(leaf, "grandparentTitle", str_value(&show, "title"));
        put_opt(leaf, "grandparentThumb", str_value(&show, "thumb"));
        put_opt(leaf, "grandparentArt", str_value(&show, "art"));
        put_opt(leaf, "grandparentGuid", str_value(&show, "guid"));
        put_opt(
            leaf,
            "parentGuid",
            str_value(&season, "guid").or_else(|| str_value(&season, "parentGuid")),
        );
        put_opt(leaf, "art", str_value(&season, "art"));
        if str_value(leaf, "contentRating").is_some_and(|rating| !rating.is_empty()) {
            // the leaf's OWN rating, which a later show edit must not overwrite
            put(leaf, "_own_content_rating", true);
        } else if let Some(rating) = str_value(&show, "contentRating") {
            put(leaf, "contentRating", rating);
        }
        put_opt(
            leaf,
            "_poster_url",
            episode_still.or_else(|| leaf.get("_show_poster").cloned()),
        );
        put_opt(leaf, "_art_url", leaf.get("_show_art").cloned());
        if let Some(show) = rows[show_index].as_object_mut() {
            let count = int_value(show, "leafCount").unwrap_or(0);
            put(show, "leafCount", count + 1);
        }
        if let Some(season) = rows[season_index].as_object_mut() {
            let count = int_value(season, "leafCount").unwrap_or(0);
            put(season, "leafCount", count + 1);
        }
        rows.push(leaf_value.clone());
    }
    fill_real(&mut rows);
    stamp_artwork(&mut rows);
    normalize_rows(&mut rows);
    for value in rows.iter_mut() {
        if let Some(row) = value.as_object_mut() {
            for key in [
                "_kind",
                "_tmdb_id",
                "_show_title",
                "_show_summary",
                "_show_year",
                "_show_poster",
                "_show_art",
                "_show_guid",
                "_season",
                "_episode",
                "_season_guid",
                "_season_guids",
                "_season_poster",
                "_show_external_guids",
                "_season_year",
                "_season_air",
                "_season_summary",
            ] {
                row.shift_remove(key);
            }
        }
    }
    rows
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn folding() {
        assert_eq!(fold_title("A\u{301}B"), "AB");
        assert_eq!(sort_title("The Zeta"), "Zeta");
        assert_eq!(fold_case("İßſK"), "i\u{307}ßsk");
    }
    #[test]
    fn person() {
        assert_eq!(person_tag_key("Alan Tudyk").len(), 24);
    }
}
