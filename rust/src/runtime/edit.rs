//! The item and section edit writers: `PUT /library/metadata/{id}?...` and the Edit
//! dialog's `PUT /library/sections/{id}/all?type=&id=...`. Ported from `library.py`'s
//! `edit`, `_edit`, `_set_field`, `_set_tags`, `_merge_tags`, `_set_lock`,
//! `_follow_parent`, `_empty_title` and `_derive_title_sort`, whose docstrings hold
//! the measurements.

use super::*;

const INTEGER_FIELDS: &[&str] = &[
    "year", "index", "absoluteIndex", "parentIndex", "duration", "viewOffset", "viewCount",
    "leafCount", "childCount",
];

const EDITABLE_FIELDS: &[&str] = &[
    "title", "titleSort", "originalTitle", "studio", "tagline", "summary", "isAdult",
    "contentRating", "originallyAvailableAt", "year", "index", "editionTitle",
    "userRating", "audienceRating", "rating", "duration", "updatedAt", "createdAt",
    "skipCount", "viewOffset", "ratingCount", "type", "viewedLeafCount",
    "contentRatingAge", "mediaCount", "extraType", "absoluteIndex",
];

const IGNORED_EDIT_FIELDS: &[&str] = &["userRating"];

const IGNORED_VALUE_FIELDS: &[&str] = &[
    "addedAt", "guid", "lastViewedAt", "viewCount", "remote", "resourcesChangedAt",
    "userFields", "lastSkippedAt", "mediaAnalysisVersion", "beginsAt", "endsAt", "agent",
    "source", "parentIndex", "updatedAt", "createdAt", "skipCount", "ratingCount", "type",
    "viewedLeafCount", "contentRatingAge", "mediaCount", "extraType", "viewOffset",
    "duration",
];

const PARSED_NOT_STORED: &[&str] = &["absoluteIndex"];

const IGNORED_TAG_TYPES: &[&str] = &["network", "mood"];

const NO_OP_FIELDS: &[&str] = &["hdr10plus"];

const SECTION_INTEGER_FIELDS: &[&str] = &[
    "addedAt", "duration", "parentIndex", "ratingCount", "updatedAt", "createdAt",
    "contentRatingAge",
];
const SECTION_STORED_FIELDS: &[&str] = &["addedAt", "ratingCount", "updatedAt", "contentRatingAge"];
const SECTION_REFUSED_FIELDS: &[&str] = &[
    "viewCount", "lastViewedAt", "skipCount", "viewOffset", "type", "lastSkippedAt",
    "mediaAnalysisVersion", "beginsAt", "endsAt", "agent", "source", "viewedLeafCount",
    "mediaCount", "extraType", "lastRatedAt",
];

const FLOAT_FIELDS: &[&str] = &["audienceRating", "rating", "userRating"];

const KEEP_EMPTY_FIELDS: &[&str] = &["title", "summary"];

const FIELD_ORDER: &[&str] = &[
    "title", "titleSort", "originalTitle", "studio", "rating", "tagline", "summary",
    "contentRating", "thumb", "art", "banner", "theme", "originallyAvailableAt", "year",
    "genre", "collection", "director", "writer", "actor", "duration", "producer", "country",
    "index", "label", "audienceRating", "guid", "editionTitle", "slug", "addedAt", "network",
    "viewCount", "userRating", "parentIndex",
];

const ARTWORK_VALUE_FIELDS: &[&str] = &["theme", "banner", "thumb", "art"];

const LOCK_ONLY_FIELDS: &[&str] = &[
    "thumb", "art", "theme", "banner", "guid", "slug", "addedAt", "remote",
    "resourcesChangedAt", "userFields", "lastSkippedAt", "mediaAnalysisVersion", "beginsAt",
    "endsAt", "agent", "viewCount", "network", "parentIndex", "lastViewedAt",
    "audioLanguage", "dovi", "duplicate", "file", "format", "hdr", "id", "lastRatedAt",
    "make", "mediaBitrate", "mediaSize", "model", "parent", "place", "resolution", "source",
    "subformat", "subtitleLanguage", "tag", "trash", "unmatched", "unviewedLeafCount",
    "unwatched", "viewedOrInProgressLeafCount",
];

/// The edit grammar's tag types and the element each one writes.
pub(super) const TAG_KEYS: &[(&str, &str)] = &[
    ("collection", "Collection"),
    ("genre", "Genre"),
    ("director", "Director"),
    ("writer", "Writer"),
    ("producer", "Producer"),
    ("actor", "Role"),
    ("country", "Country"),
    ("label", "Label"),
    ("similar", "Similar"),
    ("mood", "Mood"),
    ("style", "Style"),
];

pub(super) fn tag_key(tag_type: &str) -> Option<&'static str> {
    TAG_KEYS
        .iter()
        .find(|(name, _)| *name == tag_type)
        .map(|(_, key)| *key)
}

fn has(set: &[&str], name: &str) -> bool {
    set.contains(&name)
}

/// A generated season label: `Season N`, `Specials` or `[Unknown Season]`.
pub(super) fn generated_season_title(title: &str) -> bool {
    title == "Specials"
        || title == "[Unknown Season]"
        || title
            .strip_prefix("Season ")
            .is_some_and(|rest| !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()))
}

fn full_digits(text: &str) -> bool {
    !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit())
}

fn signed_integer(text: &str) -> Option<i64> {
    let body = text.strip_prefix(['+', '-']).unwrap_or(text);
    if !full_digits(body) {
        return None;
    }
    text.trim_start_matches('+').parse::<i64>().ok().or_else(|| {
        // Out of i64 range: still an integer to the grammar.
        Some(if text.starts_with('-') { i64::MIN } else { i64::MAX })
    })
}

fn float_text(text: &str) -> bool {
    let body = text.strip_prefix(['+', '-']).unwrap_or(text);
    let (whole, fraction) = match body.split_once('.') {
        Some((whole, fraction)) => (whole, Some(fraction)),
        None => (body, None),
    };
    full_digits(whole) && fraction.is_none_or(full_digits)
}

/// Days from 0001-01-01 (proleptic Gregorian) to a date, and back.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

fn iso_date(days: i64) -> Option<String> {
    let (year, month, day) = civil_from_days(days);
    if !(1..=9999).contains(&year) {
        return None;
    }
    Some(format!("{year:04}-{month:02}-{day:02}"))
}

/// An `originallyAvailableAt` as a real PMS reads it, or None when it cannot:
/// `2007-2-3` is `2007-02-03`, a day past the month's end ROLLS OVER, a trailing time
/// is dropped, a two-digit year is that year, and a bare INTEGER is epoch seconds.
pub(super) fn parse_date(text: &str) -> Option<String> {
    let bare = text.strip_prefix('-').unwrap_or(text);
    if full_digits(bare) {
        let seconds = text.parse::<i64>().ok()?;
        return iso_date(days_from_civil(1970, 1, 1) + seconds.div_euclid(86400));
    }
    let (date, time) = match text.find(['T', ' ']) {
        Some(index) => (&text[..index], Some(&text[index + 1..])),
        None => (text, None),
    };
    let parts = date.split('-').collect::<Vec<_>>();
    if parts.len() != 3
        || !(1..=4).contains(&parts[0].len())
        || !(1..=2).contains(&parts[1].len())
        || !(1..=2).contains(&parts[2].len())
        || !parts.iter().all(|part| full_digits(part))
    {
        return None;
    }
    if let Some(time) = time {
        let pattern = regex::Regex::new(
            r"^\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$",
        )
        .ok()?;
        if !pattern.is_match(time) {
            return None;
        }
    }
    let year = parts[0].parse::<i64>().ok()?;
    let month = parts[1].parse::<i64>().ok()?;
    let day = parts[2].parse::<i64>().ok()?;
    if year < 1 || !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    iso_date(days_from_civil(year, month, 1) + day - 1)
}

/// A NEW tag's spelling: its first character upper-cased when it is an ASCII letter
/// (cycle 61); `élan`, `ßeta` and `1abc` keep their spelling.
pub(super) fn capitalised(value: &str) -> String {
    let mut chars = value.chars();
    match chars.next() {
        Some(first) if first.is_ascii_lowercase() => {
            first.to_ascii_uppercase().to_string() + chars.as_str()
        }
        _ => value.to_string(),
    }
}

/// Python's `urllib.parse.unquote` for a tag-removal list entry.
fn unquote(text: &str) -> String {
    percent_decode(text)
}

fn locked_field(row: &Map<String, Value>, name: &str) -> bool {
    row.get("Field")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .any(|field| {
            field.get("name").and_then(Value::as_str) == Some(name)
                && field.get("locked").is_some_and(truthy)
        })
}

/// A real PMS reports locks as a `Field` array on the row, in COLUMN order.
pub(super) fn set_lock(row: &mut Map<String, Value>, field: &str, locked: bool) {
    let mut fields = row
        .get("Field")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    fields.retain(|entry| entry.get("name").and_then(Value::as_str) != Some(field));
    if locked {
        fields.push(json!({"locked": true, "name": field}));
    }
    fields.sort_by(|left, right| {
        let rank = |entry: &Value| {
            let name = entry.get("name").and_then(Value::as_str).unwrap_or_default();
            (
                FIELD_ORDER.iter().position(|known| *known == name).unwrap_or(FIELD_ORDER.len()),
                name.to_string(),
            )
        };
        rank(left).cmp(&rank(right))
    });
    if fields.is_empty() {
        row.remove("Field");
    } else {
        row.insert("Field".to_string(), Value::Array(fields));
    }
}

/// Measured on a writable copy: a Sort Title that is not LOCKED does not stick -- a
/// title write, a sort-title write and an unlock all re-derive it from the title,
/// article stripped; an EMPTY title leaves it; the SECTION path stores a typed one
/// until the next title write (cycle 16); a generated season title stores none.
pub(super) fn derive_title_sort(row: &mut Map<String, Value>) {
    if locked_field(row, "titleSort") {
        return;
    }
    if row.remove("_section_title_sort").is_some_and(|value| truthy(&value)) {
        return;
    }
    let Some(title) = row.get("title").and_then(Value::as_str).map(str::to_string) else {
        return;
    };
    if title.is_empty() {
        return;
    }
    if row.get("type").and_then(Value::as_str) == Some("season") && generated_season_title(&title) {
        row.remove("titleSort");
        return;
    }
    let derived = crate::hierarchy::sort_title(&title);
    if derived != title {
        row.insert("titleSort".to_string(), json!(derived));
    } else {
        row.remove("titleSort");
    }
}

/// An EMPTIED title: a season becomes `Season N` and an episode `Episode N`, a
/// movie, show or list keeps "", and every one keeps its old title's sort form unless
/// the sort title is locked.
pub(super) fn empty_title(row: &mut Map<String, Value>, old_title: Option<&str>) {
    if let Some(old) = old_title.filter(|old| !old.is_empty()) {
        if !locked_field(row, "titleSort") {
            row.insert("titleSort".to_string(), json!(crate::hierarchy::sort_title(old)));
        }
    }
    let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
    if kind == "season" || kind == "episode" {
        row.insert("_generatedTitle".to_string(), json!(true));
        let index = row.get("index").map(py_str).unwrap_or_else(|| "None".to_string());
        row.insert(
            "title".to_string(),
            json!(format!("{} {index}", if kind == "season" { "Season" } else { "Episode" })),
        );
    }
}

impl Runtime {
    /// `PUT /library/metadata/{id}?title.value=X&title.locked=1` and the section save.
    ///
    /// Returns whether anything was written, or the status a real PMS refuses with. A
    /// refused save writes NOTHING (cycle 17). The ORDERED pairs matter: `genre[0]`,
    /// `genre[1]` build one desired list.
    pub(super) fn library_edit(
        &mut self,
        key: &str,
        pairs: &[(String, String)],
        section: bool,
    ) -> Result<bool, u16> {
        let Some(original) = self.catalog.get(key).and_then(Value::as_object).cloned() else {
            return Err(404);
        };
        let mut row = original.clone();
        if row.get("type").and_then(Value::as_str) == Some("episode") {
            let written = pairs
                .iter()
                .filter(|(name, _)| {
                    name.split('.').next() == Some("contentRating") && !name.ends_with(".locked")
                })
                .map(|(_, value)| value.clone())
                .collect::<Vec<_>>();
            if written.iter().any(|value| !value.trim().is_empty()) {
                // an explicit write makes the rating the EPISODE's own (cycle 16)
                row.insert("_own_content_rating".to_string(), json!(true));
            } else if !written.is_empty() {
                row.remove("_own_content_rating");
            }
        }
        if section && pairs.iter().any(|(name, _)| name == "titleSort.value") {
            row.insert("_section_title_sort".to_string(), json!(true));
        }
        let mut items = pairs
            .iter()
            .filter(|(name, _)| {
                !name.starts_with("X-Plex-")
                    && !name.starts_with("include")
                    && !name.starts_with("exclude")
                    && name != "_pairs"
                    && name != "id"
                    && !(section && name == "type")
            })
            .cloned()
            .collect::<Vec<_>>();
        if section {
            // On the SECTION path a bare `field=value` is a FILTER that scopes the edit.
            items.retain(|(name, _)| {
                name.ends_with(".value") || name.ends_with(".locked") || name.contains(".tag.tag")
            });
        }
        if section && items.is_empty() {
            // Cycle 17: on the SECTION path a form that names nothing to write is a
            // 200 that edits nothing.
            return Ok(false);
        }
        if items.is_empty() || (!section && !Self::edit_recognised(&items)) {
            // Cycle 17: on the ITEM path the 400 means "this request edits NOTHING".
            return Err(400);
        }
        let old_title = row.get("title").and_then(Value::as_str).map(str::to_string);
        let sort_touched = items.iter().any(|(name, _)| {
            matches!(
                name.as_str(),
                "title" | "title.value" | "titleSort" | "titleSort.value" | "titleSort.locked"
            )
        });
        let mut changed = false;
        let mut adding: Vec<(String, Vec<(i64, usize, String)>)> = Vec::new();
        let mut removing: Vec<(String, Vec<String>)> = Vec::new();
        fn entry<'a, T: Default>(list: &'a mut Vec<(String, T)>, name: &str) -> &'a mut T {
            if let Some(index) = list.iter().position(|(known, _)| known == name) {
                return &mut list[index].1;
            }
            list.push((name.to_string(), T::default()));
            &mut list.last_mut().expect("just pushed").1
        }
        let role_pattern = regex::Regex::new(r"^actor\[\d*\]\.tagRole$").expect("valid regex");
        for (position, (name, value)) in items.iter().enumerate() {
            let tag_type = name.split('[').next().unwrap_or_default().to_lowercase();
            if name.contains(".tag.tag-") {
                if tag_key(&tag_type).is_none() {
                    if !section {
                        continue;
                    }
                    return Err(400);
                }
                entry(&mut removing, &tag_type).extend(
                    value.split(',').filter(|part| !part.is_empty()).map(unquote),
                );
                changed = true;
            } else if has(IGNORED_TAG_TYPES, &tag_type) || role_pattern.is_match(name) {
                // Measured: accepted and discarded.
                changed = true;
            } else if name.contains(".tag.tag") {
                if tag_key(&tag_type).is_none() {
                    if !section {
                        continue;
                    }
                    return Err(400);
                }
                // Measured: the INDEX orders the list, not the parameter's position.
                let index = name
                    .split_once('[')
                    .and_then(|(_, rest)| rest.split_once(']'))
                    .map(|(inside, _)| inside.to_string())
                    .unwrap_or_default();
                let order = if full_digits(&index) {
                    index.parse::<i64>().unwrap_or(position as i64)
                } else {
                    position as i64
                };
                entry(&mut adding, &tag_type).push((order, position, value.clone()));
                changed = true;
            } else if let Some(field) = name.strip_suffix(".locked") {
                if !has(EDITABLE_FIELDS, field)
                    && tag_key(field).is_none()
                    && !has(LOCK_ONLY_FIELDS, field)
                    && !has(NO_OP_FIELDS, field)
                {
                    if !section {
                        continue;
                    }
                    return Err(400);
                }
                if has(NO_OP_FIELDS, field) {
                    changed = true;
                    continue;
                }
                if section {
                    // Measured: the section path PARSES the lock as an integer 0 or 1.
                    let significant = value.trim_start_matches('0');
                    if !full_digits(value) || !matches!(significant, "" | "1") {
                        return Err(400);
                    }
                    set_lock(&mut row, field, significant == "1");
                } else {
                    // Measured: on the item path `1` is the only value that locks.
                    set_lock(&mut row, field, value == "1");
                }
                changed = true;
            } else {
                let field = name.strip_suffix(".value").unwrap_or(name);
                if section && has(SECTION_REFUSED_FIELDS, field) {
                    return Err(500);
                }
                if section && has(SECTION_INTEGER_FIELDS, field) {
                    Self::edit_set_field(&mut row, field, value, section)?;
                    changed = true;
                    continue;
                }
                let is_movie = row.get("type").and_then(Value::as_str) == Some("movie");
                if has(IGNORED_VALUE_FIELDS, field)
                    || has(NO_OP_FIELDS, field)
                    || (matches!(field, "duration" | "index") && is_movie)
                    || (tag_key(field).is_some() && !name.ends_with(".value"))
                {
                    changed = true;
                    continue;
                }
                if has(ARTWORK_VALUE_FIELDS, field) || (section && field == "slug") {
                    if has(ARTWORK_VALUE_FIELDS, field) {
                        if value.is_empty() {
                            // Cycle 31: an EMPTY artwork value REMOVES the column.
                            row.remove(field);
                        } else {
                            let stamp = row
                                .get("updatedAt")
                                .filter(|value| truthy(value))
                                .map(py_str)
                                .unwrap_or_else(|| now().to_string());
                            row.insert(
                                field.to_string(),
                                json!(format!("/library/metadata/{key}/{field}/{stamp}")),
                            );
                        }
                    }
                    changed = true;
                    continue;
                }
                if !has(EDITABLE_FIELDS, field) {
                    if !section {
                        continue;
                    }
                    return Err(400);
                }
                Self::edit_set_field(&mut row, field, value, section)?;
                changed = true;
            }
        }
        let written = items
            .iter()
            .map(|(name, _)| name.strip_suffix(".value").unwrap_or(name).to_string())
            .collect::<HashSet<_>>();
        if section && written.contains("originallyAvailableAt") && !written.contains("year") {
            // Measured: on the SECTION path the release date sets the YEAR.
            match row
                .get("originallyAvailableAt")
                .and_then(Value::as_str)
                .filter(|date| !date.is_empty())
                .and_then(|date| date.get(..4))
                .and_then(|year| year.parse::<i64>().ok())
            {
                Some(year) => {
                    row.insert("year".to_string(), json!(year));
                }
                None => {
                    row.remove("year");
                }
            }
        }
        // Nothing below refuses: commit the row, then the writes that reach others.
        if let Some(target) = self.catalog.get_mut(key) {
            *target = Value::Object(row);
        }
        let mut tag_types = adding.iter().map(|(name, _)| name.clone()).collect::<Vec<_>>();
        for (name, _) in &removing {
            if !tag_types.contains(name) {
                tag_types.push(name.clone());
            }
        }
        for tag_type in tag_types {
            let mut wanted = adding
                .iter()
                .find(|(name, _)| *name == tag_type)
                .map(|(_, list)| list.clone())
                .unwrap_or_default();
            wanted.sort_by(|left, right| (left.0, left.1).cmp(&(right.0, right.1)));
            let wanted = wanted
                .into_iter()
                .map(|(_, _, value)| value)
                .filter(|value| !value.is_empty())
                .collect::<Vec<_>>();
            let removed = removing
                .iter()
                .find(|(name, _)| *name == tag_type)
                .map(|(_, list)| list.clone())
                .unwrap_or_default();
            if section {
                self.merge_tags(key, &tag_type, &wanted, &removed);
            } else {
                // Measured on the item path: ANY `tag-` removal clears the whole list
                // and the indexed list rebuilds it; an exact repeat is one tag.
                let mut unique = Vec::new();
                for value in wanted {
                    if !unique.contains(&value) {
                        unique.push(value);
                    }
                }
                self.set_tags(key, &tag_type, &unique);
            }
        }
        let mut written = written;
        {
            let Some(row) = self.catalog.get_mut(key).and_then(Value::as_object_mut) else {
                return Ok(changed);
            };
            if written.contains("title") {
                row.remove("_generatedTitle");
            }
            if written.contains("title") && row.get("title").and_then(Value::as_str) == Some("") {
                empty_title(row, old_title.as_deref());
            } else if sort_touched {
                derive_title_sort(row);
            }
            let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
            if written.contains("index")
                && kind == "season"
                && !written.contains("title")
                && generated_season_title(old_title.as_deref().unwrap_or(""))
            {
                // Cycle 55: a season's generated title follows an index edit.
                let index = row.get("index").cloned();
                let title = match &index {
                    Some(value) if py_str(value) == "0" => "Specials".to_string(),
                    Some(value) if !value.is_null() => format!("Season {}", py_str(value)),
                    _ => "[Unknown Season]".to_string(),
                };
                row.insert("title".to_string(), json!(title));
                written.insert("title".to_string());
            } else if written.contains("index") && row.get("_generatedTitle").is_some_and(truthy) {
                let index = row.get("index").map(py_str).unwrap_or_else(|| "None".to_string());
                row.insert(
                    "title".to_string(),
                    json!(format!("{} {index}", if kind == "season" { "Season" } else { "Episode" })),
                );
                written.insert("title".to_string());
            }
        }
        if ["title", "studio", "index", "contentRating"]
            .iter()
            .any(|name| written.contains(*name))
        {
            self.follow_parent(key, &written);
        }
        let row = self.catalog.get(key).cloned().unwrap_or(Value::Null);
        let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
        if kind == "episode" && written.contains("contentRating") && !row.get("contentRating").is_some_and(truthy) {
            // Cycle 20: clearing an episode's own rating makes it read the show's.
            let show_rating = row
                .get("grandparentRatingKey")
                .map(string_value)
                .and_then(|show| self.catalog.get(&show).cloned())
                .and_then(|show| show.get("contentRating").filter(|value| truthy(value)).cloned());
            if let Some(rating) = show_rating {
                if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                    object.insert("contentRating".to_string(), rating);
                }
            }
        }
        if kind == "season" && written.contains("year") {
            // Cycle 16: a season with no year of its own carries the show's as
            // `parentYear`, and one that LOSES its year gains it in the same read.
            let parent_year = row
                .get("parentRatingKey")
                .map(string_value)
                .and_then(|parent| self.catalog.get(&parent).cloned())
                .and_then(|parent| parent.get("year").filter(|value| truthy(value)).cloned());
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                match parent_year {
                    Some(year) if !object.get("year").is_some_and(truthy) => {
                        object.insert("parentYear".to_string(), year);
                    }
                    _ => {
                        object.remove("parentYear");
                    }
                }
            }
        }
        // Record the diff for `--state`, a removal as null.
        if let Some(after) = self.catalog.get(key).and_then(Value::as_object).cloned() {
            let edits = self.edits.entry(key.to_string()).or_default();
            for (name, value) in &after {
                if original.get(name) != Some(value) {
                    edits.insert(name.clone(), value.clone());
                }
            }
            for name in original.keys() {
                if !after.contains_key(name) {
                    edits.insert(name.clone(), Value::Null);
                }
            }
        }
        self.dirty = true;
        Ok(changed)
    }

    /// One edit against one or more rows, with the refusals a real PMS makes, and the
    /// frames it pushes. Cycle 16: the SECTION path's `state 3` frame carries the row's
    /// title from BEFORE the write, frames its rows in LISTING order and sends every
    /// row's state 3 before any row's state 5; the ITEM path sends its pair per row.
    pub(super) fn apply_edit(
        &mut self,
        keys: &[String],
        pairs: &[(String, String)],
        section: bool,
        metadata_state: &str,
    ) -> Response {
        let query = pairs.iter().cloned().collect::<HashMap<_, _>>();
        if section {
            let was = keys
                .iter()
                .filter_map(|key| self.catalog.get(key).cloned().map(|row| (key.clone(), row)))
                .collect::<HashMap<_, _>>();
            for key in keys {
                if let Err(status) = self.library_edit(key, pairs, true) {
                    self.save_if_dirty();
                    return Response::error(status, "plexsim: edit refused");
                }
                self.after_artwork_edit(key, &query);
            }
            let rows = keys
                .iter()
                .filter_map(|key| self.catalog.get(key).cloned())
                .collect::<Vec<_>>();
            let ordered = self.sort_rows(rows.clone(), Some("titleSort")).unwrap_or(rows);
            for row in &ordered {
                let key = key_of(row).unwrap_or_default();
                let before = was.get(&key).cloned().unwrap_or_else(|| row.clone());
                self.notify_timeline(&before, 3, Some(metadata_state));
            }
            for row in &ordered {
                self.notify_timeline(row, 5, None);
            }
        } else {
            for key in keys {
                if let Err(status) = self.library_edit(key, pairs, false) {
                    self.save_if_dirty();
                    return Response::error(status, "plexsim: edit refused");
                }
                self.after_artwork_edit(key, &query);
                if let Some(row) = self.catalog.get(key).cloned() {
                    self.notify_edited(&row, metadata_state);
                }
            }
        }
        self.save_if_dirty();
        Response::empty(200)
    }

    /// Does this form name anything the ITEM edit path can write? (cycle 17)
    fn edit_recognised(items: &[(String, String)]) -> bool {
        let role_pattern = regex::Regex::new(r"^actor\[\d*\]\.tagRole$").expect("valid regex");
        for (name, _) in items {
            if name.contains(".tag.tag") || role_pattern.is_match(name) {
                let tag_type = name.split('[').next().unwrap_or_default().to_lowercase();
                if tag_key(&tag_type).is_some() || has(IGNORED_TAG_TYPES, &tag_type) {
                    return true;
                }
                continue;
            }
            let field = name.strip_suffix(".locked").unwrap_or(name);
            let field = field.strip_suffix(".value").unwrap_or(field);
            if has(EDITABLE_FIELDS, field)
                || tag_key(field).is_some()
                || has(LOCK_ONLY_FIELDS, field)
                || has(IGNORED_VALUE_FIELDS, field)
                || has(NO_OP_FIELDS, field)
            {
                return true;
            }
        }
        false
    }

    /// One scalar field, with the coercions a real PMS applies.
    fn edit_set_field(
        row: &mut Map<String, Value>,
        field: &str,
        value: &str,
        section: bool,
    ) -> Result<(), u16> {
        if field == "isAdult" {
            if value == "1" {
                row.insert("isAdult".to_string(), json!(true));
            } else {
                row.remove("isAdult");
            }
            return Ok(());
        }
        if section && has(SECTION_INTEGER_FIELDS, field) {
            // Cycle 20: parsed as an integer -- a word is a 500 -- and stored only for
            // the four columns that really store.
            let text = value.trim();
            let Some(number) = signed_integer(text) else {
                return Err(500);
            };
            if has(SECTION_STORED_FIELDS, field) {
                row.insert(field.to_string(), json!(number));
                if field == "updatedAt" {
                    let key = row.get("ratingKey").map(string_value).unwrap_or_default();
                    for artwork in ["thumb", "art"] {
                        let current = row.get(artwork).map(string_value).unwrap_or_default();
                        if let Some((head, _)) = current.rsplit_once('/') {
                            if head.starts_with(&format!("/library/metadata/{key}/")) {
                                row.insert(artwork.to_string(), json!(format!("{head}/{number}")));
                            }
                        }
                    }
                }
            }
            return Ok(());
        }
        if has(IGNORED_EDIT_FIELDS, field) && !section {
            // Measured: accepted, value discarded -- the star rating is /:/rate.
            return Ok(());
        }
        if has(FLOAT_FIELDS, field) {
            let text = value.trim();
            if (text.is_empty() && !section) || (!text.is_empty() && !float_text(text)) {
                return Err(500);
            }
            let number = if text.is_empty() {
                None
            } else {
                text.trim_start_matches('+').parse::<f64>().ok()
            };
            match number {
                Some(number) if !(number < 0.0) => {
                    row.insert(field.to_string(), json!(number));
                    if field == "userRating" {
                        row.insert("lastRatedAt".to_string(), json!(now()));
                    }
                }
                _ => {
                    row.remove(field);
                    if field == "userRating" {
                        row.remove("lastRatedAt");
                    }
                }
            }
            return Ok(());
        }
        if value.is_empty() {
            if field == "year" && section {
                row.remove(field);
                return Ok(());
            }
            if has(INTEGER_FIELDS, field) {
                return Err(500);
            }
            if has(KEEP_EMPTY_FIELDS, field) {
                row.insert(field.to_string(), json!(""));
            } else {
                row.remove(field);
            }
            return Ok(());
        }
        if has(INTEGER_FIELDS, field) {
            let text = value.trim();
            let Some(number) = signed_integer(text) else {
                return Err(500);
            };
            if field == "year" && !(-2147483648..=2147483647).contains(&number) {
                return Err(500);
            }
            if has(PARSED_NOT_STORED, field) {
                row.remove(field);
            } else if number < 0 || (field == "year" && number == 0) {
                row.remove(field);
            } else {
                row.insert(field.to_string(), json!(number));
            }
            return Ok(());
        }
        if field == "originallyAvailableAt" {
            match parse_date(value) {
                Some(parsed) => {
                    row.insert(field.to_string(), json!(parsed));
                }
                None if section => return Err(500),
                None => {
                    row.remove(field);
                }
            }
            return Ok(());
        }
        row.insert(field.to_string(), json!(value));
        Ok(())
    }

    /// Replace a tag list, which is what an indexed write means. A tag's canonical
    /// spelling and id survive -- a real PMS keeps a tag TABLE -- and a NEW one is
    /// minted from its text.
    pub(super) fn set_tags(&mut self, key: &str, tag_type: &str, values: &[String]) {
        let Some(element) = tag_key(tag_type) else { return };
        let Some(row) = self.catalog.get(key).cloned() else { return };
        let before = row
            .get(element)
            .and_then(Value::as_array)
            .map(|tags| tags.iter().filter_map(|tag| tag.get("tag")).map(py_str).collect::<Vec<_>>())
            .unwrap_or_default();
        let mut existing: HashMap<String, Value> = HashMap::new();
        for tag in row.get(element).and_then(Value::as_array).into_iter().flatten() {
            existing.insert(tag.get("tag").map(py_str).unwrap_or_default().to_lowercase(), tag.clone());
        }
        for other in &self.catalog.items {
            for tag in other.get(element).and_then(Value::as_array).into_iter().flatten() {
                existing
                    .entry(tag.get("tag").map(py_str).unwrap_or_default().to_lowercase())
                    .or_insert_with(|| tag.clone());
            }
        }
        for ((table, lowered), tag) in &self.tag_table {
            if table == element {
                existing.entry(lowered.clone()).or_insert_with(|| tag.clone());
            }
        }
        let mut fresh = Vec::new();
        for value in values {
            if let Some(known) = existing.get(&value.to_lowercase()) {
                fresh.push(known.clone());
                continue;
            }
            let spelled = capitalised(value);
            let id = tag_id(element, &spelled);
            let mut minted = json!({"id": id, "tag": spelled, "filter": format!("{tag_type}={id}")});
            if crate::hierarchy::PERSON_TAG_ELEMENTS.contains(&element) {
                minted["tagKey"] = json!(crate::hierarchy::person_tag_key(&spelled));
            }
            if element == "Role" {
                minted["role"] = json!("");
            }
            self.tag_table
                .insert((element.to_string(), spelled.to_lowercase()), minted.clone());
            existing.insert(spelled.to_lowercase(), minted.clone());
            fresh.push(minted);
        }
        let section = row.get("librarySectionID").map(string_value).unwrap_or_default();
        if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            if fresh.is_empty() {
                object.remove(element);
            } else {
                object.insert(element.to_string(), Value::Array(fresh.clone()));
            }
            if element == "Collection" {
                object.remove("_collectionLight");
            }
        }
        if element == "Collection" {
            let names = fresh.iter().map(|tag| tag.get("tag").map(py_str).unwrap_or_default()).collect::<Vec<_>>();
            for name in &names {
                self.sync_collection_tag(key, &section, name, true);
            }
            for gone in before.iter().filter(|name| !names.contains(name)) {
                self.sync_collection_tag(key, &section, gone, false);
            }
        }
    }

    /// Editing a `collection[0].tag.tag` on an item makes a TAG, not a collection
    /// (cycle 30): it joins a collection only when one of that name already exists,
    /// and leaving the tag leaves the collection (cycle 49), which itself survives.
    fn sync_collection_tag(&mut self, key: &str, section: &str, title: &str, join: bool) {
        let lowered = title.to_lowercase();
        for collection in self.collections.values_mut() {
            let name = if collection.tag.is_empty() { &collection.title } else { &collection.tag };
            if name.to_lowercase() != lowered || collection.section != section {
                continue;
            }
            if join {
                if !collection.items.iter().any(|item| item == key) {
                    collection.items.push(key.to_string());
                }
            } else {
                collection.items.retain(|item| item != key);
            }
            break;
        }
    }

    /// The SECTION path's tag write, which merges where the item path replaces: the
    /// indexed list goes FIRST, deduplicated case-insensitively, the row's other tags
    /// follow, and a removal removes exactly the names it lists.
    fn merge_tags(&mut self, key: &str, tag_type: &str, wanted: &[String], removed: &[String]) {
        let Some(element) = tag_key(tag_type) else { return };
        let gone = removed.iter().map(|name| name.to_lowercase()).collect::<HashSet<_>>();
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
        let kept = self
            .catalog
            .get(key)
            .and_then(|row| row.get(element))
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|tag| tag.get("tag").map(py_str).unwrap_or_default())
            .filter(|name| !seen.contains(&name.to_lowercase()) && !gone.contains(&name.to_lowercase()))
            .collect::<Vec<_>>();
        first.extend(kept);
        self.set_tags(key, tag_type, &first);
    }

    /// Copy a show's or a season's title, studio, index and content rating onto the
    /// rows beneath it. Measured on a writable copy: a season's title and studio land
    /// on its episodes' `parentTitle`/`parentStudio`, a show's on its seasons' and
    /// every episode's `grandparentTitle`; an EMPTY title removes them. A show's
    /// `contentRating` moves onto the EPISODES that carry no rating of their own
    /// (cycle 16) -- a season carries none.
    pub(super) fn follow_parent(&mut self, key: &str, written: &HashSet<String>) {
        let Some(row) = self.catalog.get(key).cloned() else { return };
        let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
        if kind != "show" && kind != "season" {
            return;
        }
        fn carry(child: &mut Map<String, Value>, name: &str, value: Option<&Value>) {
            match value.filter(|value| truthy(value)) {
                Some(value) => {
                    child.insert(name.to_string(), value.clone());
                }
                None => {
                    child.remove(name);
                }
            }
        }
        for child in self.catalog.items.iter_mut() {
            let parent = child.get("parentRatingKey").map(string_value);
            let grandparent = child.get("grandparentRatingKey").map(string_value);
            let Some(object) = child.as_object_mut() else { continue };
            if parent.as_deref() == Some(key) {
                if written.contains("title") {
                    carry(object, "parentTitle", row.get("title"));
                }
                if written.contains("studio") {
                    carry(object, "parentStudio", row.get("studio"));
                }
                if written.contains("index") {
                    object.insert(
                        "parentIndex".to_string(),
                        row.get("index").cloned().unwrap_or(Value::Null),
                    );
                }
            } else if kind == "show" && grandparent.as_deref() == Some(key) && written.contains("title") {
                carry(object, "grandparentTitle", row.get("title"));
            }
            if kind == "show" && written.contains("contentRating") && grandparent.as_deref() == Some(key)
                && !object.get("_own_content_rating").is_some_and(truthy)
            {
                carry(object, "contentRating", row.get("contentRating"));
            }
        }
    }
}
