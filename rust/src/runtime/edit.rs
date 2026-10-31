//! The metadata edit grammar, ported from `library.py`'s `Library.edit`.
//!
//! Every rule here was measured parameter by parameter against a writable copy of
//! the reference; the comments carry the measurement the Python module records.
//! The engine works on a COPY of the row, so a save a real PMS refuses writes
//! nothing (cycle 17: `title.value=PARTIAL&year.value=abc` is a 500 and the title is
//! untouched), and only then writes the row back and applies the cross-row effects.

use super::*;
use regex::Regex;
use std::sync::OnceLock;

pub(super) struct EditError {
    pub status: u16,
    pub detail: String,
}

fn refuse(status: u16, detail: impl Into<String>) -> EditError {
    EditError {
        status,
        detail: detail.into(),
    }
}

// Fields a row carries as a number.  Cycle 20: the ITEM path really reads three of
// them; the SECTION path reads far more (SECTION_INTEGER_FIELDS).
const INTEGER_FIELDS: &[&str] = &[
    "year",
    "index",
    "absoluteIndex",
    "parentIndex",
    "duration",
    "viewOffset",
    "viewCount",
    "leafCount",
    "childCount",
];

// Fields the Edit dialog may write.  Measured: anything else is a 400, including
// `sortTitle` (the field is `titleSort`).
const EDITABLE_FIELDS: &[&str] = &[
    "title",
    "titleSort",
    "originalTitle",
    "studio",
    "tagline",
    "summary",
    "isAdult",
    "contentRating",
    "originallyAvailableAt",
    "year",
    "index",
    "editionTitle",
    "userRating",
    "audienceRating",
    "rating",
    "duration",
    "updatedAt",
    "createdAt",
    "skipCount",
    "viewOffset",
    "ratingCount",
    "type",
    "viewedLeafCount",
    "contentRatingAge",
    "mediaCount",
    "extraType",
    "absoluteIndex",
];

// Accepted, locked, and DISCARDED on the ITEM path: the star rating is /:/rate there.
const IGNORED_EDIT_FIELDS: &[&str] = &["userRating"];

// Accepted with a 200 and DISCARDED as values on the ITEM path.
const IGNORED_VALUE_FIELDS: &[&str] = &[
    "addedAt",
    "guid",
    "lastViewedAt",
    "viewCount",
    "remote",
    "resourcesChangedAt",
    "userFields",
    "lastSkippedAt",
    "mediaAnalysisVersion",
    "beginsAt",
    "endsAt",
    "agent",
    "source",
    "parentIndex",
    "updatedAt",
    "createdAt",
    "skipCount",
    "ratingCount",
    "type",
    "viewedLeafCount",
    "contentRatingAge",
    "mediaCount",
    "extraType",
    "viewOffset",
    "duration",
];

// Cycle 31: PARSED and then dropped -- `absoluteIndex.value=7` stores nothing while a
// word is a 500.
const PARSED_NOT_STORED: &[&str] = &["absoluteIndex"];

// A show's `network[0].tag.tag=` and a movie's `mood[0].tag.tag=` are 200s that store
// nothing visible.
const IGNORED_TAG_TYPES: &[&str] = &["network", "mood"];

// Cycle 31: `hdr10plus` is accepted on both halves of the grammar and stores neither.
const NO_OP_FIELDS: &[&str] = &["hdr10plus"];

// Cycle 20: the SECTION save path parses these as integers (a word is a 500) and
// stores four of them; the refused set is a 500 there for ANY value.
const SECTION_INTEGER_FIELDS: &[&str] = &[
    "addedAt",
    "duration",
    "parentIndex",
    "ratingCount",
    "updatedAt",
    "createdAt",
    "contentRatingAge",
];
const SECTION_STORED_FIELDS: &[&str] = &["addedAt", "ratingCount", "updatedAt", "contentRatingAge"];
const SECTION_REFUSED_FIELDS: &[&str] = &[
    "viewCount",
    "lastViewedAt",
    "skipCount",
    "viewOffset",
    "type",
    "lastSkippedAt",
    "mediaAnalysisVersion",
    "beginsAt",
    "endsAt",
    "agent",
    "source",
    "viewedLeafCount",
    "mediaCount",
    "extraType",
    "lastRatedAt",
];

// Stored as NUMBERS: `audienceRating.value=5.5` reads back 5.5, not "5.5".
const FLOAT_FIELDS: &[&str] = &["audienceRating", "rating", "userRating"];

// The only two fields an empty value STORES as "" rather than removing.
const KEEP_EMPTY_FIELDS: &[&str] = &["title", "summary"];

// The order a real PMS lists locks in -- the metadata table's columns, not the order
// the client wrote them.
const FIELD_ORDER: &[&str] = &[
    "title",
    "titleSort",
    "originalTitle",
    "studio",
    "rating",
    "tagline",
    "summary",
    "contentRating",
    "thumb",
    "art",
    "banner",
    "theme",
    "originallyAvailableAt",
    "year",
    "genre",
    "collection",
    "director",
    "writer",
    "actor",
    "duration",
    "producer",
    "country",
    "index",
    "label",
    "audienceRating",
    "guid",
    "editionTitle",
    "slug",
    "addedAt",
    "network",
    "viewCount",
    "userRating",
    "parentIndex",
];

// Cycle 20/31: the artwork columns take a value on both paths; the url is discarded
// and the column becomes the row's own versioned path.
const ARTWORK_VALUE_FIELDS: &[&str] = &["theme", "banner", "thumb", "art"];

// Columns that take a LOCK without being editable fields (cycles 20, 21, 30, 53).
const LOCK_ONLY_FIELDS: &[&str] = &[
    "thumb",
    "art",
    "theme",
    "banner",
    "guid",
    "slug",
    "addedAt",
    "remote",
    "resourcesChangedAt",
    "userFields",
    "lastSkippedAt",
    "mediaAnalysisVersion",
    "beginsAt",
    "endsAt",
    "agent",
    "viewCount",
    "network",
    "parentIndex",
    "lastViewedAt",
    "audioLanguage",
    "dovi",
    "duplicate",
    "file",
    "format",
    "hdr",
    "id",
    "lastRatedAt",
    "make",
    "mediaBitrate",
    "mediaSize",
    "model",
    "parent",
    "place",
    "resolution",
    "source",
    "subformat",
    "subtitleLanguage",
    "tag",
    "trash",
    "unmatched",
    "unviewedLeafCount",
    "unwatched",
    "viewedOrInProgressLeafCount",
];

/// Tag-type parameter name -> the row key that holds it.  The client builds edit
/// params from the LOWERCASED tag type, and `actor` writes `Role`.
pub(super) fn tag_key_for(tag_type: &str) -> Option<&'static str> {
    Some(match tag_type {
        "collection" => "Collection",
        "genre" => "Genre",
        "director" => "Director",
        "writer" => "Writer",
        "producer" => "Producer",
        "actor" => "Role",
        "country" => "Country",
        "label" => "Label",
        "similar" => "Similar",
        "mood" => "Mood",
        "style" => "Style",
        _ => return None,
    })
}

fn is_tag_type(name: &str) -> bool {
    tag_key_for(name).is_some()
}

/// A column a `.locked` write records in `Field` (cycle 88: the collection writer's
/// test; `hdr10plus` is accepted and recorded nowhere).
pub(super) fn records_lock(field: &str) -> bool {
    (has(EDITABLE_FIELDS, field) || is_tag_type(field) || has(LOCK_ONLY_FIELDS, field))
        && !has(NO_OP_FIELDS, field)
}

fn has(list: &[&str], name: &str) -> bool {
    list.contains(&name)
}

fn tag_role_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^actor\[\d*\]\.tagRole$").unwrap())
}

fn generated_season_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^(Season \d+|Specials|\[Unknown Season\])$").unwrap())
}

fn signed_int_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^[+-]?\d+$").unwrap())
}

fn float_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^[+-]?\d+(\.\d+)?$").unwrap())
}

/// Python's `str.strip()` for the ASCII whitespace a query value can carry.
fn py_strip(value: &str) -> &str {
    value.trim_matches(|c: char| c.is_whitespace())
}

/// An `originallyAvailableAt` as a real PMS reads it, or None when it cannot.
///
/// Measured value by value on a writable copy of the reference: `2007-2-3` is
/// `2007-02-03`, a day past the month's end ROLLS OVER (`2007-02-30` is `2007-03-02`)
/// while a day outside 1-31 or a month outside 1-12 is refused, a trailing time is
/// dropped, a two-digit year is that year (`99-01-02` is `0099-01-02`), and a bare
/// INTEGER is epoch seconds (`20070203` is `1970-08-21`, `-5` is `1969-12-31`).
pub(super) fn parse_edit_date(text: &str) -> Option<String> {
    static EPOCH: OnceLock<Regex> = OnceLock::new();
    static DATE: OnceLock<Regex> = OnceLock::new();
    let epoch = EPOCH.get_or_init(|| Regex::new(r"^-?\d+$").unwrap());
    let date = DATE.get_or_init(|| {
        Regex::new(
            r"^(\d{1,4})-(\d{1,2})-(\d{1,2})(?:[T ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$",
        )
        .unwrap()
    });
    if epoch.is_match(text) {
        let seconds: i64 = text.parse().ok()?;
        let days = seconds.div_euclid(86_400);
        return Some(civil_from_days(days));
    }
    let captures = date.captures(text)?;
    let year: i64 = captures[1].parse().ok()?;
    let month: i64 = captures[2].parse().ok()?;
    let day: i64 = captures[3].parse().ok()?;
    if year < 1 || !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    let days = days_from_civil(year, month, 1) + day - 1;
    let text = civil_from_days(days);
    if text.len() > 10 {
        return None;
    }
    Some(text)
}

pub(super) fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

pub(super) fn civil_from_days(days: i64) -> String {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    let y = y + if m <= 2 { 1 } else { 0 };
    format!("{y:04}-{m:02}-{d:02}")
}

/// A NEW tag's spelling: its first character upper-cased when it is an ASCII letter.
/// Cycle 61: `c61c lowgenre` is stored `C61c lowgenre`, while `élan`, `ßeta` and
/// `1abc` keep their spelling and an EXISTING tag keeps its canonical one.
pub(super) fn capitalised(value: &str) -> String {
    let mut chars = value.chars();
    match chars.next() {
        Some(first) if first.is_ascii_lowercase() => {
            first.to_ascii_uppercase().to_string() + chars.as_str()
        }
        _ => value.to_string(),
    }
}

/// The name part of a pair, lowercased, before any `[`.
fn tag_type_of(name: &str) -> String {
    name.split('[').next().unwrap_or(name).to_lowercase()
}

fn strip_value_suffix(name: &str) -> &str {
    name.strip_suffix(".value").unwrap_or(name)
}

pub(super) fn field_locked(row: &Map<String, Value>, field: &str) -> bool {
    row.get("Field")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .any(|entry| {
            entry.get("name").and_then(Value::as_str) == Some(field)
                && entry
                    .get("locked")
                    .and_then(Value::as_bool)
                    .unwrap_or(false)
        })
}

/// A real PMS reports locks as a `Field` array on the row, in COLUMN order.
pub(super) fn set_lock(row: &mut Map<String, Value>, field: &str, locked: bool) {
    let mut fields = row
        .get("Field")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter(|entry| entry.get("name").and_then(Value::as_str) != Some(field))
        .collect::<Vec<_>>();
    if locked {
        fields.push(json!({"locked": true, "name": field}));
    }
    fields.sort_by_key(|entry| {
        let name = entry
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        (
            FIELD_ORDER
                .iter()
                .position(|known| *known == name)
                .unwrap_or(FIELD_ORDER.len()),
            name,
        )
    });
    if fields.is_empty() {
        row.shift_remove("Field");
    } else {
        row.insert("Field".to_string(), Value::Array(fields));
    }
}

/// A Sort Title that is not LOCKED does not stick: a title write, a sort-title write
/// and an unlock all re-derive it from the title, article stripped.  An EMPTY title
/// leaves it as it was.  Cycle 16: the SECTION save path stores an unlocked
/// `titleSort.value=` until the next title write.
pub(super) fn derive_title_sort(row: &mut Map<String, Value>) {
    if field_locked(row, "titleSort") {
        return;
    }
    if row.shift_remove("_section_title_sort").is_some() {
        return;
    }
    let title = row
        .get("title")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if title.is_empty() {
        return;
    }
    if row.get("type").and_then(Value::as_str) == Some("season")
        && generated_season_re().is_match(&title)
    {
        // Cycle 20: a title the SERVER generated stores no sort title at all.
        row.shift_remove("titleSort");
        return;
    }
    let derived = crate::hierarchy::sort_title(&title);
    if derived != title {
        row.insert("titleSort".to_string(), json!(derived));
    } else {
        row.shift_remove("titleSort");
    }
}

/// An EMPTIED title: a season becomes `Season N` and an episode `Episode N` -- a
/// title GENERATED from the index -- while a movie, a show and a list keep "".  Every
/// one keeps its old title's sort form as `titleSort` unless the sort title is locked.
fn empty_title(row: &mut Map<String, Value>, old_title: &str) {
    if !old_title.is_empty() && !field_locked(row, "titleSort") {
        row.insert(
            "titleSort".to_string(),
            json!(crate::hierarchy::sort_title(old_title)),
        );
    }
    let kind = row
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if kind == "season" || kind == "episode" {
        row.insert("_generatedTitle".to_string(), json!(true));
        let index = row.get("index").map(string_value).unwrap_or_default();
        let label = if kind == "season" {
            "Season"
        } else {
            "Episode"
        };
        let index = if index.is_empty() {
            "None".to_string()
        } else {
            index
        };
        row.insert("title".to_string(), json!(format!("{label} {index}")));
    }
}

/// What one `_edit` pass leaves for the caller to apply across rows.
#[derive(Default)]
pub(super) struct EditOutcome {
    pub written: HashSet<String>,
    pub minted: Vec<(String, String, Value)>,
    pub collection_added: Vec<String>,
    pub collection_removed: Vec<String>,
}

impl Runtime {
    /// The ordered query pairs of a request, which a `HashMap` collapses: the
    /// `genre[0]`/`genre[1]` entries build one list and a repeat matters.
    pub(super) fn request_pairs(request: &Request) -> Vec<(String, String)> {
        let (_, raw) = split_target(&request.target);
        parse_query(&raw)
            .into_iter()
            .map(|(key, value)| {
                // `parse_query` reads a bare name as " "; Python keeps it blank.
                if value == " " && !raw.contains(&format!("{key}=")) {
                    (key, String::new())
                } else {
                    (key, value)
                }
            })
            .collect()
    }

    /// Every tag this server knows, keyed by (row key, lowercased tag): each row's
    /// own, then the tag TABLE -- which outlives a tag being removed from every row.
    fn known_tag(&self, row: &Map<String, Value>, key: &str, lowered: &str) -> Option<Value> {
        let matches = |tag: &&Value| {
            tag.get("tag")
                .map(string_value)
                .is_some_and(|text| text.to_lowercase() == lowered)
        };
        if let Some(found) = row
            .get(key)
            .and_then(Value::as_array)
            .and_then(|tags| tags.iter().find(matches))
        {
            return Some(found.clone());
        }
        if let Some(found) = tag_in_catalog(&self.catalog, key, lowered) {
            return Some(found);
        }
        self.tag_table
            .get(&(key.to_string(), lowered.to_string()))
            .cloned()
    }

    /// Replace a tag list, which is what an indexed write means.  A tag's canonical
    /// spelling and id survive a case-insensitive match.
    fn set_tags(
        &self,
        row: &mut Map<String, Value>,
        tag_type: &str,
        values: &[String],
        outcome: &mut EditOutcome,
    ) {
        let Some(key) = tag_key_for(tag_type) else {
            return;
        };
        let before = row
            .get(key)
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|tag| tag.get("tag").map(string_value).unwrap_or_default())
            .collect::<HashSet<_>>();
        let mut fresh = Vec::new();
        let mut minted_here: HashMap<String, Value> = HashMap::new();
        for value in values {
            let lowered = value.to_lowercase();
            if let Some(known) = minted_here
                .get(&lowered)
                .cloned()
                .or_else(|| self.known_tag(row, key, &lowered))
                .or_else(|| {
                    outcome
                        .minted
                        .iter()
                        .find(|(k, l, _)| k == key && *l == lowered)
                        .map(|(_, _, tag)| tag.clone())
                })
            {
                fresh.push(known);
                continue;
            }
            let spelled = capitalised(value);
            let id = tag_id(key, &spelled);
            let mut minted = Map::new();
            minted.insert("id".to_string(), json!(id));
            minted.insert("tag".to_string(), json!(spelled));
            minted.insert("filter".to_string(), json!(format!("{tag_type}={id}")));
            if crate::hierarchy::PERSON_TAG_ELEMENTS.contains(&key) {
                minted.insert(
                    "tagKey".to_string(),
                    json!(crate::hierarchy::person_tag_key(&spelled)),
                );
            }
            if key == "Role" {
                // Measured: an actor added through an edit carries `role: ""`.
                minted.insert("role".to_string(), json!(""));
            }
            let minted = Value::Object(minted);
            outcome
                .minted
                .push((key.to_string(), lowered.clone(), minted.clone()));
            minted_here.insert(lowered, minted.clone());
            fresh.push(minted);
        }
        let after = fresh
            .iter()
            .map(|tag| tag.get("tag").map(string_value).unwrap_or_default())
            .collect::<Vec<_>>();
        if fresh.is_empty() {
            row.shift_remove(key);
        } else {
            row.insert(key.to_string(), Value::Array(fresh));
        }
        if key == "Collection" {
            row.shift_remove("_collectionLight");
            outcome.collection_added.extend(after.iter().cloned());
            for gone in before {
                if !after.contains(&gone) {
                    outcome.collection_removed.push(gone);
                }
            }
        }
    }

    /// The SECTION path's tag write, which MERGES where the item path replaces: the
    /// indexed list goes first, deduplicated case-insensitively, the row's other tags
    /// follow in their order, and a `tag-` removal removes exactly what it names.
    fn merge_tags(
        &self,
        row: &mut Map<String, Value>,
        tag_type: &str,
        wanted: &[String],
        removed: &[String],
        outcome: &mut EditOutcome,
    ) {
        let Some(key) = tag_key_for(tag_type) else {
            return;
        };
        let gone = removed
            .iter()
            .map(|name| name.to_lowercase())
            .collect::<HashSet<_>>();
        let mut first = Vec::new();
        let mut seen = HashSet::new();
        for value in wanted {
            if seen.insert(value.to_lowercase()) {
                first.push(value.clone());
            }
        }
        if first.is_empty() && gone.is_empty() {
            return;
        }
        let kept = row
            .get(key)
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|tag| tag.get("tag").map(string_value).unwrap_or_default())
            .filter(|tag| {
                let lowered = tag.to_lowercase();
                !seen.contains(&lowered) && !gone.contains(&lowered)
            })
            .collect::<Vec<_>>();
        first.extend(kept);
        self.set_tags(row, tag_type, &first, outcome);
    }

    /// One scalar field, with the coercions a real PMS applies.
    fn set_field(
        row: &mut Map<String, Value>,
        field: &str,
        value: &str,
        section: bool,
    ) -> Result<(), EditError> {
        if field == "isAdult" {
            if value == "1" {
                row.insert("isAdult".to_string(), json!(true));
            } else {
                row.shift_remove("isAdult");
            }
            return Ok(());
        }
        if section && has(SECTION_INTEGER_FIELDS, field) {
            let text = py_strip(value);
            if !signed_int_re().is_match(text) {
                return Err(refuse(500, format!("unparseable {field} {}", crate::filters::py_repr(value))));
            }
            if has(SECTION_STORED_FIELDS, field) {
                let number: i64 = text.trim_start_matches('+').parse().unwrap_or(0);
                row.insert(field.to_string(), json!(number));
                if field == "updatedAt" {
                    // The artwork version IS this column, so it moves with it.
                    let own = format!(
                        "/library/metadata/{}/",
                        row.get("ratingKey").map(string_value).unwrap_or_default()
                    );
                    for artwork in ["thumb", "art"] {
                        let current = row.get(artwork).map(string_value).unwrap_or_default();
                        if let Some((head, _)) = current.rsplit_once('/') {
                            if head.starts_with(&own) {
                                row.insert(artwork.to_string(), json!(format!("{head}/{number}")));
                            }
                        }
                    }
                }
            }
            return Ok(());
        }
        if has(IGNORED_EDIT_FIELDS, field) && !section {
            return Ok(());
        }
        if has(FLOAT_FIELDS, field) {
            // A rating is a number (`abc` is a 500 on both paths); an EMPTY one is a
            // 500 on the item path and removes the field on the section path.  The
            // section path STORES a userRating, stamping lastRatedAt.
            let text = py_strip(value);
            if (text.is_empty() && !section) || (!text.is_empty() && !float_re().is_match(text)) {
                return Err(refuse(500, format!("unparseable {field} {}", crate::filters::py_repr(value))));
            }
            let number = if text.is_empty() {
                None
            } else {
                text.parse::<f64>().ok()
            };
            if field == "userRating" {
                // Cycle 90: the section path's rating IS a rating write -- the same
                // unchanged-is-nothing rule as `/:/rate`, a change stamps
                // `viewUpdatedAt`, and `-5` stamps `lastRatedAt` (plexscratch42; it
                // cleared both here).  The row is the catalog row's copy the caller
                // writes back.
                let stored = row.get("userRating").and_then(Value::as_f64);
                if let Some((stars, rated_at)) = rating_write(stored, number.unwrap_or(-1.0)) {
                    for (name, value) in [("userRating", stars), ("lastRatedAt", rated_at)] {
                        match value {
                            Some(value) => {
                                row.insert(name.to_string(), value);
                            }
                            None => {
                                row.shift_remove(name);
                            }
                        }
                    }
                    row.insert("_viewUpdatedAt".to_string(), json!(now()));
                }
                return Ok(());
            }
            match number {
                Some(number) if number >= 0.0 => {
                    row.insert(field.to_string(), json!(number));
                }
                _ => {
                    // Cycle 20: a negative rating REMOVES the field, the way a
                    // negative year does.
                    row.shift_remove(field);
                }
            }
            return Ok(());
        }
        if value.is_empty() {
            if field == "year" && section {
                row.shift_remove(field);
                return Ok(());
            }
            if has(INTEGER_FIELDS, field) {
                return Err(refuse(500, format!("unparseable {field} {}", crate::filters::py_repr(value))));
            }
            if has(KEEP_EMPTY_FIELDS, field) {
                row.insert(field.to_string(), json!(""));
            } else {
                row.shift_remove(field);
            }
            return Ok(());
        }
        if has(INTEGER_FIELDS, field) {
            let text = py_strip(value);
            if !signed_int_re().is_match(text) {
                return Err(refuse(500, format!("unparseable {field} {}", crate::filters::py_repr(value))));
            }
            let digits = text.trim_start_matches('+');
            let number = digits.parse::<i128>().unwrap_or(i128::MAX);
            if field == "year" && !(-2_147_483_648..=2_147_483_647).contains(&number) {
                return Err(refuse(500, format!("year out of range {}", crate::filters::py_repr(value))));
            }
            if has(PARSED_NOT_STORED, field) || number < 0 || (field == "year" && number == 0) {
                row.shift_remove(field);
            } else {
                // Past i64 it is the largest, not a wrapped negative index or duration.
                row.insert(field.to_string(), json!(number.min(i64::MAX as i128) as i64));
            }
            return Ok(());
        }
        if field == "originallyAvailableAt" {
            match parse_edit_date(value) {
                Some(date) => {
                    row.insert(field.to_string(), json!(date));
                }
                None if section => {
                    return Err(refuse(500, format!("unparseable {field} {}", crate::filters::py_repr(value))));
                }
                None => {
                    row.shift_remove(field);
                }
            }
            return Ok(());
        }
        row.insert(field.to_string(), json!(value));
        Ok(())
    }

    /// Does this form name anything the ITEM edit path can write?  Cycle 17: the item
    /// path's 400 is "nothing here is an edit".
    fn edit_recognised(items: &[(String, String)]) -> bool {
        for (name, _) in items {
            if name.contains(".tag.tag") || tag_role_re().is_match(name) {
                let tag_type = tag_type_of(name);
                if is_tag_type(&tag_type) || has(IGNORED_TAG_TYPES, &tag_type) {
                    return true;
                }
                continue;
            }
            let field = name.strip_suffix(".locked").unwrap_or(name);
            let field = strip_value_suffix(field);
            if has(EDITABLE_FIELDS, field)
                || is_tag_type(field)
                || has(LOCK_ONLY_FIELDS, field)
                || has(IGNORED_VALUE_FIELDS, field)
                || has(NO_OP_FIELDS, field)
            {
                return true;
            }
        }
        false
    }

    /// `Library.edit` + `Library._edit` over a copy of one row.  Returns the edited
    /// row and what the caller must apply to OTHER rows.
    pub(super) fn edit_row_copy(
        &self,
        original: &Map<String, Value>,
        pairs: &[(String, String)],
        section: bool,
    ) -> Result<Option<(Map<String, Value>, EditOutcome)>, EditError> {
        let mut row = original.clone();
        let kind = row
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if kind == "episode" {
            let written = pairs
                .iter()
                .filter(|(name, _)| {
                    name.split('.').next() == Some("contentRating") && !name.ends_with(".locked")
                })
                .map(|(_, value)| value.clone())
                .collect::<Vec<_>>();
            if written.iter().any(|value| !py_strip(value).is_empty()) {
                // an explicit write makes the rating the EPISODE's own -- cycle 16
                row.insert("_own_content_rating".to_string(), json!(true));
            } else if !written.is_empty() {
                // Cycle 20: an EMPTY write gives the row back to the show.
                row.shift_remove("_own_content_rating");
            }
        }
        if section && pairs.iter().any(|(name, _)| name == "titleSort.value") {
            // The section path keeps what was typed until the next title write.
            row.insert("_section_title_sort".to_string(), json!(true));
        }
        let mut items = pairs
            .iter()
            .filter(|(name, _)| {
                !name.starts_with("X-Plex-")
                    && !name.starts_with("include")
                    && !name.starts_with("exclude")
                    && name != "id"
                    // Cycle 22: `type` is the SECTION path's scoping parameter and
                    // nothing else there; on the item path it is an accepted no-op.
                    && !(section && name == "type")
            })
            .cloned()
            .collect::<Vec<_>>();
        if section {
            // On the SECTION path a bare `field=value` is a FILTER that scopes the
            // edit, not a field to write.
            items.retain(|(name, _)| {
                name.ends_with(".value") || name.ends_with(".locked") || name.contains(".tag.tag")
            });
            if items.is_empty() {
                // Cycle 17: a section-path form that names nothing to write is a 200
                // that edits nothing.
                return Ok(None);
            }
        }
        if items.is_empty() || (!section && !Self::edit_recognised(&items)) {
            return Err(refuse(400, "bad request"));
        }
        let mut outcome = EditOutcome::default();
        let old_title = row.get("title").map(string_value).unwrap_or_default();
        let sort_touched = items.iter().any(|(name, _)| {
            matches!(
                name.as_str(),
                "title" | "title.value" | "titleSort" | "titleSort.value" | "titleSort.locked"
            )
        });
        // tag type -> (index, position, value) per indexed entry
        let mut adding: Vec<(String, Vec<(i64, usize, String)>)> = Vec::new();
        let mut removing: Vec<(String, Vec<String>)> = Vec::new();
        fn slot<'a, T>(list: &'a mut Vec<(String, Vec<T>)>, key: &str) -> &'a mut Vec<T> {
            if let Some(index) = list.iter().position(|(name, _)| name == key) {
                &mut list[index].1
            } else {
                list.push((key.to_string(), Vec::new()));
                &mut list.last_mut().unwrap().1
            }
        }
        for (position, (name, value)) in items.iter().enumerate() {
            if name.contains(".tag.tag-") {
                let tag_type = tag_type_of(name);
                if !is_tag_type(&tag_type) {
                    if !section {
                        continue;
                    }
                    return Err(refuse(400, format!("unknown tag field {}", crate::filters::py_repr(&tag_type))));
                }
                // The client double-encodes the list: each name is quoted, then the
                // comma-joined list is quoted again.
                slot(&mut removing, &tag_type).extend(
                    value
                        .split(',')
                        .filter(|part| !part.is_empty())
                        .map(|part| percent_decode(part)),
                );
            } else if has(IGNORED_TAG_TYPES, &tag_type_of(name)) || tag_role_re().is_match(name) {
                // Measured: accepted and discarded.
            } else if name.contains(".tag.tag") {
                let tag_type = tag_type_of(name);
                if !is_tag_type(&tag_type) {
                    if !section {
                        continue;
                    }
                    return Err(refuse(400, format!("unknown tag field {}", crate::filters::py_repr(&tag_type))));
                }
                slot(&mut adding, &tag_type).push((tag_order(name, position), position, value.clone()));
            } else if let Some(field) = name.strip_suffix(".locked") {
                if !has(EDITABLE_FIELDS, field)
                    && !is_tag_type(field)
                    && !has(LOCK_ONLY_FIELDS, field)
                    && !has(NO_OP_FIELDS, field)
                {
                    if !section {
                        continue;
                    }
                    return Err(refuse(400, format!("unknown field {}", crate::filters::py_repr(field))));
                }
                if has(NO_OP_FIELDS, field) {
                    continue;
                }
                if section {
                    // Measured: the section path PARSES the lock as an integer that
                    // must be 0 or 1 -- `01` and `001` lock, `00` unlocks, and `true`,
                    // `2`, `-1`, ` 1` and an empty value are all a 400.
                    if value.is_empty()
                        || !value.bytes().all(|b| b.is_ascii_digit())
                        || value.trim_start_matches('0').len() > 1
                        || value.trim_start_matches('0') > "1"
                    {
                        return Err(refuse(400, format!("bad lock {}", crate::filters::py_repr(value))));
                    }
                    set_lock(&mut row, field, value.trim_start_matches('0') == "1");
                } else {
                    // Measured: on the item path `1` is the only value that locks.
                    set_lock(&mut row, field, value == "1");
                }
            } else {
                let field = strip_value_suffix(name);
                if section && has(SECTION_REFUSED_FIELDS, field) {
                    return Err(refuse(500, format!("refused {field} on the section path")));
                }
                if section && has(SECTION_INTEGER_FIELDS, field) {
                    Self::set_field(&mut row, field, value, section)?;
                    continue;
                }
                if has(IGNORED_VALUE_FIELDS, field)
                    || has(NO_OP_FIELDS, field)
                    || (matches!(field, "duration" | "index") && kind == "movie")
                    || (is_tag_type(field) && !name.ends_with(".value"))
                {
                    continue;
                }
                if has(ARTWORK_VALUE_FIELDS, field) || (section && field == "slug") {
                    if has(ARTWORK_VALUE_FIELDS, field) {
                        if value.is_empty() {
                            // Cycle 31: an EMPTY artwork value REMOVES the column.
                            row.shift_remove(field);
                        } else {
                            let stamp = row
                                .get("updatedAt")
                                .map(string_value)
                                .filter(|stamp| !stamp.is_empty() && stamp != "0")
                                .unwrap_or_else(|| now().to_string());
                            let own = row.get("ratingKey").map(string_value).unwrap_or_default();
                            row.insert(
                                field.to_string(),
                                json!(format!("/library/metadata/{own}/{field}/{stamp}")),
                            );
                        }
                    }
                    continue;
                }
                if !has(EDITABLE_FIELDS, field) {
                    if !section {
                        continue;
                    }
                    return Err(refuse(400, format!("unknown field {}", crate::filters::py_repr(field))));
                }
                Self::set_field(&mut row, field, value, section)?;
            }
        }
        let written = items
            .iter()
            .map(|(name, _)| strip_value_suffix(name).to_string())
            .collect::<HashSet<_>>();
        let mut written = written;
        if section && written.contains("originallyAvailableAt") && !written.contains("year") {
            // Measured: on the SECTION path the release date sets the YEAR -- and
            // clearing it removes both -- unless the same save writes a year.
            let date = row
                .get("originallyAvailableAt")
                .map(string_value)
                .unwrap_or_default();
            if let Some(year) = date.get(..4).and_then(|year| year.parse::<i64>().ok()) {
                row.insert("year".to_string(), json!(year));
            } else {
                row.shift_remove("year");
            }
        }
        let mut tag_types = adding
            .iter()
            .map(|(name, _)| name.clone())
            .collect::<Vec<_>>();
        for (name, _) in &removing {
            if !tag_types.contains(name) {
                tag_types.push(name.clone());
            }
        }
        for tag_type in tag_types {
            let mut entries = adding
                .iter()
                .find(|(name, _)| *name == tag_type)
                .map(|(_, entries)| entries.clone())
                .unwrap_or_default();
            entries.sort_by(|left, right| (left.0, left.1).cmp(&(right.0, right.1)));
            let wanted = entries
                .into_iter()
                .map(|(_, _, value)| value)
                .filter(|value| !value.is_empty())
                .collect::<Vec<_>>();
            let removed = removing
                .iter()
                .find(|(name, _)| *name == tag_type)
                .map(|(_, names)| names.clone())
                .unwrap_or_default();
            if section {
                self.merge_tags(&mut row, &tag_type, &wanted, &removed, &mut outcome);
            } else {
                // Measured on the item path: ANY `tag-` removal clears the whole list,
                // and the indexed list in the same request rebuilds it.  An exact
                // repeat is one tag.
                let mut unique = Vec::new();
                for value in wanted {
                    if !unique.contains(&value) {
                        unique.push(value);
                    }
                }
                self.set_tags(&mut row, &tag_type, &unique, &mut outcome);
            }
        }
        if written.contains("title") {
            row.shift_remove("_generatedTitle");
        }
        let title_now = row.get("title").map(string_value).unwrap_or_default();
        if written.contains("title") && title_now.is_empty() && row.get("title").is_some() {
            empty_title(&mut row, &old_title);
        } else if sort_touched {
            derive_title_sort(&mut row);
        }
        if written.contains("index")
            && kind == "season"
            && !written.contains("title")
            && generated_season_re().is_match(&old_title)
        {
            // Cycle 55: a season's title is GENERATED unless the user wrote one, so it
            // follows an index edit -- `index.value=0` is `Specials`.
            let index = row.get("index").map(string_value);
            let title = match index.as_deref() {
                Some("0") => "Specials".to_string(),
                Some(index) if !index.is_empty() => format!("Season {index}"),
                _ => "[Unknown Season]".to_string(),
            };
            row.insert("title".to_string(), json!(title));
            written.insert("title".to_string());
        } else if written.contains("index") && row.get("_generatedTitle").is_some() {
            // Measured: a generated title follows the index.
            let label = if kind == "season" {
                "Season"
            } else {
                "Episode"
            };
            let index = row.get("index").map(string_value).unwrap_or_default();
            row.insert("title".to_string(), json!(format!("{label} {index}")));
            written.insert("title".to_string());
        }
        outcome.written = written;
        Ok(Some((row, outcome)))
    }

    /// Apply the effects an edit owes OTHER rows, after the row itself is stored.
    fn after_edit(&mut self, key: &str, outcome: &EditOutcome) {
        for (table_key, lowered, tag) in &outcome.minted {
            self.tag_table
                .or_insert_with((table_key.clone(), lowered.clone()), || tag.clone());
        }
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let written = &outcome.written;
        let kind = row
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if written.contains("title")
            || written.contains("studio")
            || written.contains("index")
            || written.contains("contentRating")
        {
            self.follow_parent(&row, written);
        }
        if kind == "episode" && written.contains("contentRating") {
            let empty = row
                .get("contentRating")
                .map(string_value)
                .unwrap_or_default()
                .is_empty();
            if empty {
                // Cycle 20: the inheritance is a READ-TIME fallback -- clearing an
                // episode's own rating makes it read the show's AT ONCE.
                let show_key = row
                    .get("grandparentRatingKey")
                    .map(string_value)
                    .unwrap_or_default();
                let inherited = self
                    .catalog
                    .get(&show_key)
                    .and_then(|show| show.get("contentRating"))
                    .filter(|value| !string_value(value).is_empty())
                    .cloned();
                if let (Some(value), Some(object)) = (
                    inherited,
                    self.catalog.get_mut(key).and_then(Value::as_object_mut),
                ) {
                    object.insert("contentRating".to_string(), value);
                }
            }
        }
        if kind == "season" && written.contains("year") {
            // Cycle 16: a season with no year of its own carries the show's as
            // `parentYear`, and one that LOSES its year gains it in the same read.
            let parent_key = row
                .get("parentRatingKey")
                .map(string_value)
                .unwrap_or_default();
            let parent_year = self
                .catalog
                .get(&parent_key)
                .and_then(|parent| parent.get("year"))
                .filter(|year| !year.is_null() && string_value(year) != "0")
                .cloned();
            let own_year = row
                .get("year")
                .filter(|year| !year.is_null() && string_value(year) != "0")
                .is_some();
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                match parent_year {
                    Some(year) if !own_year => {
                        object.insert("parentYear".to_string(), year);
                    }
                    _ => {
                        object.shift_remove("parentYear");
                    }
                }
            }
        }
        let section = row
            .get("librarySectionID")
            .map(string_value)
            .unwrap_or_default();
        for title in &outcome.collection_added {
            self.join_tag_collection(key, &row, &section, title);
        }
        for title in &outcome.collection_removed {
            self.leave_tag_collection(key, &section, title);
        }
    }

    /// Copy a show's or a season's title and studio onto the rows beneath it.
    /// Measured: a season's title and studio land on its episodes' `parentTitle` and
    /// `parentStudio`, and a show's on its seasons' and every episode's
    /// `grandparentTitle`.  An EMPTY show title removes them rather than sending "".
    pub(super) fn follow_parent(&mut self, row: &Value, written: &HashSet<String>) {
        let kind = row
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if kind != "show" && kind != "season" {
            return;
        }
        let key = key_of(row).unwrap_or_default();
        let value_of = |name: &str| {
            row.get(name)
                .filter(|value| !value.is_null() && !string_value(value).is_empty())
                .cloned()
        };
        let title = value_of("title");
        let studio = value_of("studio");
        let rating = value_of("contentRating");
        let index = row.get("index").cloned().unwrap_or(Value::Null);
        fn carry(child: &mut Map<String, Value>, name: &str, value: &Option<Value>) {
            match value {
                Some(value) => {
                    child.insert(name.to_string(), value.clone());
                }
                None => {
                    child.shift_remove(name);
                }
            }
        }
        // Only the rows beneath, by key: `items_mut` marks every row changed, and the
        // save then re-diffs the whole library under the lock for one show edit.
        let beneath = self
            .catalog
            .hot()
            .lazy()
            .filter(|child| {
                ["parentRatingKey", "grandparentRatingKey"]
                    .iter()
                    .any(|name| child.get(*name).map(string_value).as_deref() == Some(key.as_str()))
            })
            .filter_map(|row| key_of(&row))
            .collect::<Vec<_>>();
        for child_key in &beneath {
            let Some(child) = self.catalog.get_mut(child_key).and_then(Value::as_object_mut) else {
                continue;
            };
            let parent = child
                .get("parentRatingKey")
                .map(string_value)
                .unwrap_or_default();
            let grandparent = child
                .get("grandparentRatingKey")
                .map(string_value)
                .unwrap_or_default();
            if parent == key {
                if written.contains("title") {
                    carry(child, "parentTitle", &title);
                }
                if written.contains("studio") {
                    carry(child, "parentStudio", &studio);
                }
                if written.contains("index") {
                    child.insert("parentIndex".to_string(), index.clone());
                }
            } else if kind == "show" && grandparent == key && written.contains("title") {
                carry(child, "grandparentTitle", &title);
            }
            if kind == "show" && written.contains("contentRating") && grandparent == key {
                // Cycle 16: the cascade reaches only the episodes that carry NO rating
                // of their own.
                if child.get("_own_content_rating").is_none() {
                    carry(child, "contentRating", &rating);
                }
            }
        }
    }

    /// One edit against one or more rows, with the refusals a real PMS makes.  The
    /// item path frames each row as it goes; the section path frames its rows in
    /// LISTING order -- the sort title -- and sends every row's state 3 (with the
    /// title from BEFORE the write) before any row's state 5.
    pub(super) fn apply_edit(
        &mut self,
        keys: &[String],
        pairs: &[(String, String)],
        section: bool,
        metadata_state: &str,
    ) -> Response {
        let mut before = Vec::new();
        let mut done = Vec::new();
        for key in keys {
            let Some(original) = self.catalog.get(key).and_then(Value::as_object).cloned() else {
                continue;
            };
            let edited = match self.edit_row_copy(&original, pairs, section) {
                Ok(edited) => edited,
                Err(error) => {
                    self.save_if_dirty();
                    warning!("EDIT REFUSED: {}", error.detail);
                    return Response::error(error.status, &format!("rex: {}", error.detail));
                }
            };
            let Some((row, outcome)) = edited else {
                if section {
                    before.push((key.clone(), Value::Object(original.clone())));
                    done.push(key.clone());
                }
                continue;
            };
            // The persisted edit record: every public field the write changed.
            let mut changed = Map::new();
            for (name, value) in &row {
                if original.get(name) != Some(value) {
                    changed.insert(name.clone(), value.clone());
                }
            }
            for name in original.keys() {
                if !row.contains_key(name) {
                    changed.insert(name.clone(), Value::Null);
                }
            }
            if let Some(slot) = self.catalog.get_mut(key) {
                *slot = Value::Object(row.clone());
            }
            self.after_edit(key, &outcome);
            if outcome.written.contains("summary") {
                let mut names = HashSet::new();
                names.insert("summary".to_string());
                self.propagate_metadata_change(key, &names);
            }
            // Cycle 31: an emptied artwork column falls back to its nearest owning
            // ancestor's and re-derives every borrower below it.
            let artwork_query = pairs.iter().cloned().collect::<HashMap<_, _>>();
            self.after_artwork_edit(key, &artwork_query);
            self.edits.entry(key.clone()).or_default().extend(changed);
            if section {
                before.push((key.clone(), Value::Object(original)));
                done.push(key.clone());
            } else {
                self.push_edit_events_named(key, metadata_state);
            }
        }
        if section && !done.is_empty() {
            let mut ordered = done
                .iter()
                .filter_map(|key| self.catalog.get(key).cloned())
                .collect::<Vec<_>>();
            ordered.sort_by_key(|row| {
                crate::hierarchy::sort_title(
                    &row.get("titleSort")
                        .or_else(|| row.get("title"))
                        .map(string_value)
                        .unwrap_or_default(),
                )
                .to_lowercase()
            });
            for row in &ordered {
                let key = key_of(row).unwrap_or_default();
                let was = before
                    .iter()
                    .find(|(candidate, _)| *candidate == key)
                    .map(|(_, row)| row.clone())
                    .unwrap_or_else(|| row.clone());
                self.push_timeline_row(&was, 3, Some(metadata_state), None);
            }
            for row in &ordered {
                self.push_timeline_row(row, 5, None, None);
            }
        }
        self.dirty = true;
        self.save_if_dirty();
        info!(
            "EDIT {} <- {}",
            keys.iter()
                .filter(|key| self.catalog.get(key).is_some())
                .cloned()
                .collect::<Vec<_>>()
                .join(","),
            query_names(pairs)
        );
        Response::empty(200)
    }
}

/// The oracle's query DICT from the request's ordered pairs: each name once, at its
/// first position, holding its last value.
pub(crate) fn dict_pairs(pairs: &[(String, String)]) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for (name, value) in pairs {
        match out.iter_mut().find(|(known, _)| known == name) {
            Some(slot) => slot.1 = value.clone(),
            None => out.push((name.clone(), value.clone())),
        }
    }
    out
}

/// `",".join(k for k in query if not k.startswith("X-Plex-") and k != "_pairs")`: the
/// request's parameter NAMES, once each in first-seen order, as the oracle's dict holds them.
pub(super) fn query_names(pairs: &[(String, String)]) -> String {
    let mut seen = Vec::<&str>::new();
    for (name, _) in pairs {
        if !name.starts_with("X-Plex-") && name != "_pairs" && !seen.contains(&name.as_str()) {
            seen.push(name);
        }
    }
    seen.join(",")
}

/// Where a `field[N].tag.tag` value goes in its list: measured, the INDEX orders the list, not
/// the parameter's position; one with no numeric index keeps its position.
pub(super) fn tag_order(name: &str, position: usize) -> i64 {
    let index = name
        .split_once('[')
        .and_then(|(_, rest)| rest.split_once(']'))
        .map(|(index, _)| index)
        .unwrap_or("");
    if !index.is_empty() && index.bytes().all(|b| b.is_ascii_digit()) {
        index.parse::<i64>().unwrap_or(position as i64)
    } else {
        position as i64
    }
}

/// The first tag of `key` any row holds, in row order, whose name lowercased is `lowered`. The
/// search corpus already names every row's Role, Director, Genre and Collection tags in memory,
/// so only the row holding the match is read; the other fields are read off every row, which
/// is what every one cost before (each row from disk, an edit's new tag at a time).
fn tag_in_catalog(catalog: &crate::catalog::Catalog, key: &str, lowered: &str) -> Option<Value> {
    let matches = |tag: &&Value| tag.get("tag").map(string_value).is_some_and(|text| text.to_lowercase() == lowered);
    let hot = catalog.hot();
    let Some(slot) = crate::corpus::CORPUS_TAG_FIELDS.iter().position(|field| *field == key) else {
        return hot.lazy().find_map(|row| row.get(key).and_then(Value::as_array).and_then(|tags| tags.iter().find(matches)).cloned());
    };
    let (rows, names) = hot.corpus();
    let wanted = names.each().filter(|(name, _)| name.to_lowercase() == lowered).map(|(_, id)| id).collect::<HashSet<_>>();
    if wanted.is_empty() {
        return None;
    }
    rows.iter().enumerate().find_map(|(position, entry)| {
        let (index, _) = names.tags(entry, slot).iter().find(|(_, id)| wanted.contains(id))?;
        hot.at(position)?.get(key)?.as_array()?.get(*index as usize).filter(matches).cloned()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_known_genre_is_found_reading_only_the_row_that_holds_it() {
        let directory = std::env::temp_dir().join(format!("rex-known-tag-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&directory);
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("rex.db");
        let mut items = (0..40)
            .map(|n| json!({"ratingKey": (100 + n).to_string(), "type": "movie", "title": format!("M{n}"), "summary": "x".repeat(300),
                            "Genre": [{"tag": "Drama", "id": 1}]}))
            .collect::<Vec<_>>();
        items[25]["Genre"] = json!([{"tag": "Drama", "id": 1}, {"tag": "Film-Noir", "id": 77, "filter": "genre=77"}]);
        items[30]["Genre"] = json!([{"tag": "film-noir", "id": 78}]);
        let mut catalog = crate::catalog::Catalog::from_value(json!({"items": items})).unwrap();
        let mut cache = crate::store::Cache::default();
        catalog.attach(&path, crate::rows::test_support::key_of, false);
        crate::rows::test_support::save(&mut catalog, &mut cache, &path, true);
        catalog.pack_pending();
        let before = crate::rows::test_support::DISK_READS.with(std::cell::Cell::get);
        let found = tag_in_catalog(&catalog, "Genre", "film-noir");
        let read = crate::rows::test_support::DISK_READS.with(std::cell::Cell::get) - before;
        let _ = std::fs::remove_dir_all(&directory);
        // The first row holding it, in row order, with its own spelling and id.
        assert_eq!(found, Some(json!({"tag": "Film-Noir", "id": 77, "filter": "genre=77"})));
        assert!(read <= 1, "{read} rows read from disk");
    }

    #[test]
    fn an_integer_past_i64_is_stored_as_the_largest_not_wrapped() {
        let mut row = Map::new();
        assert!(Runtime::set_field(&mut row, "index", "9223372036854775808", false).is_ok());
        assert_eq!(row.get("index"), Some(&json!(i64::MAX)));
    }
}
