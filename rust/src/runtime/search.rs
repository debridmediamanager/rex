//! Universal search: the title matcher, the score ladder, the tag matcher, the
//! spellfix fallback and the two result shapes (`/hubs/search`, `/library/search`).
//! A port of `server.py`'s `Catalog.search`, `search_tier`, `search_matches`,
//! `search`, `library_search` and their helpers; the measurements are in their
//! docstrings (cycles 12-59).

use super::*;
use super::resume::int_param;
use crate::hierarchy::{fold_query, fold_title};

/// The seventeen hubs `/hubs/search` answers, in the reference's base order.
const SEARCH_HUBS: &[(&str, &str)] = &[
    ("show", "Shows"), ("artist", "Artists"), ("album", "Albums"), ("episode", "Episodes"),
    ("movie", "Movies"), ("track", "Tracks"), ("photoalbum", "Photo Albums"),
    ("autotag", "Automatic Tags"), ("photo", "Photos"), ("tag", "Tags"), ("actor", "Actors"),
    ("director", "Directors"), ("genre", "Genres"), ("collection", "Collections"),
    ("playlist", "Playlists"), ("shared", "Shared"), ("place", "Places"),
];
/// The tag hubs, and which of a row's tag lists each searches.
const SEARCH_TAG_HUBS: &[(&str, &str)] = &[("actor", "Role"), ("director", "Director"), ("genre", "Genre")];

fn tier_drop(tier: &str) -> f64 {
    match tier {
        "exact" => 0.0,
        "anchored" => 0.20,
        "prefix" => 0.40,
        _ => 0.60,
    }
}

fn tier_class(tier: &str) -> i64 {
    match tier {
        "exact" => 0,
        "anchored" => 1,
        "prefix" => 2,
        _ => 3,
    }
}

fn score_text(rank: f64) -> String {
    format!("{:.5}", rank.clamp(0.0, 1.0))
}

fn text_of<'a>(row: &'a Value, field: &str) -> &'a str {
    row.get(field).and_then(Value::as_str).unwrap_or("")
}

fn words(text: &str) -> Vec<String> {
    text.split_whitespace().map(str::to_string).collect()
}

/// The partial/first-token score pass's token view (cycle 39): marks dropped, the
/// title table applied, ASCII case folded.
fn tier_words(text: &str) -> Vec<String> {
    words(&fold_title(text).chars().map(|c| c.to_ascii_lowercase()).collect::<String>())
}

/// Cycle 39's whole-title equality pass.
fn whole_title_equal(title: &str, query: &str) -> bool {
    let raw_terms = words(query);
    if raw_terms.iter().any(|word| word == "ōf" || word == "Ōf") {
        return false;
    }
    if raw_terms.iter().any(|word| word.chars().skip(1).any(|c| c == 'ſ')) {
        return false;
    }
    let left = words(&fold_query(&title.to_lowercase()));
    let right = words(&fold_query(&query.to_lowercase()));
    !left.is_empty() && left == right
}

/// Which of the four score tiers a row's title earns (cycle 36, 50).
pub(super) fn search_tier(row: &Value, query: &str) -> &'static str {
    let terms = tier_words(query);
    if terms.is_empty() {
        return "fuzzy";
    }
    for field in ["title", "titleSort", "originalTitle"] {
        if whole_title_equal(text_of(row, field), query) {
            return "exact";
        }
    }
    let title_words = tier_words(text_of(row, "title"));
    if title_words.is_empty() {
        return "fuzzy";
    }
    let sort = row.get("titleSort").and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or(text_of(row, "title"));
    let anchored = tier_words(sort);
    if anchored.first() == terms.first() && !anchored.is_empty() {
        return "anchored";
    }
    if terms.len() <= title_words.len()
        && title_words[0].starts_with(terms[0].as_str())
        && (1..terms.len()).all(|index| title_words[index].starts_with(terms[index].as_str()))
    {
        return "prefix";
    }
    "fuzzy"
}

/// The tier one tag NAME earns, or None when the query does not reach it.
pub(super) fn tag_tier(name: &str, terms: &[String]) -> Option<&'static str> {
    let name_words = words(&fold_query(name));
    if name_words.is_empty() || terms.is_empty() {
        return None;
    }
    if name_words == terms {
        return Some("exact");
    }
    if terms.len() <= name_words.len()
        && name_words[0].starts_with(terms[0].as_str())
        && (1..terms.len()).all(|index| name_words[index].starts_with(terms[index].as_str()))
    {
        return Some("prefix");
    }
    if terms.len() == 1 && name_words[1..].iter().any(|word| word.starts_with(terms[0].as_str())) {
        return Some("fuzzy");
    }
    None
}

fn tag_score(tier: &str) -> String {
    score_text(match tier {
        "exact" => 0.92,
        "fuzzy" => 0.32,
        _ => 0.52,
    })
}

/// The full-precision score PMS compares (cycle 36/38): the type's ladder base, the
/// tier drop, plus `audienceRating / 10000` capped at 10.
pub(super) fn search_score_value(row: &Value, needle: &str) -> f64 {
    let base = match row.get("type").and_then(Value::as_str) {
        Some("episode") => 0.91,
        Some("playlist") => 0.92,
        _ => 0.93,
    };
    let tier = search_tier(row, needle);
    let tail = row
        .get("audienceRating")
        .and_then(|value| value.as_f64().or_else(|| value.as_str().and_then(|s| s.parse::<f64>().ok())))
        .map_or(0.0, |rating| rating.clamp(0.0, 10.0) / 10000.0);
    (base - tier_drop(tier) + tail).clamp(0.0, 1.0)
}

/// The integer hub rank (cycle 41).
fn search_hub_rank(kind: &str, classes: &[i64]) -> i64 {
    let class = classes.iter().copied().min().unwrap_or(3);
    let bonus = match class {
        0 => 50,
        1 | 2 => 25,
        _ => 0,
    };
    let bias = match kind {
        "show" | "artist" => 10,
        "season" | "album" => 5,
        _ => 0,
    };
    bonus + bias
}

/// PMS's recovered unstable descending comparison sort (libc++ introsort), in place.
pub(super) fn comparison_sort<T>(rows: &mut Vec<T>, score: &dyn Fn(&T) -> f64) {
    fn before<T>(rows: &[T], score: &dyn Fn(&T) -> f64, left: usize, right: usize) -> bool {
        score(&rows[left]) > score(&rows[right])
    }
    fn small<T>(rows: &mut [T], score: &dyn Fn(&T) -> f64, indices: &[usize]) -> usize {
        let mut changes = 0;
        for position in 1..indices.len() {
            let mut cursor = position;
            while cursor > 0 && before(rows, score, indices[cursor], indices[cursor - 1]) {
                rows.swap(indices[cursor], indices[cursor - 1]);
                changes += 1;
                cursor -= 1;
            }
        }
        changes
    }
    fn insertion<T>(rows: &mut [T], score: &dyn Fn(&T) -> f64, first: usize, last: usize, incomplete: bool) -> bool {
        if last - first <= 5 {
            let indices = (first..last).collect::<Vec<_>>();
            small(rows, score, &indices);
            return true;
        }
        small(rows, score, &[first, first + 1, first + 2]);
        let mut changes = 0;
        for position in first + 3..last {
            if before(rows, score, position, position - 1) {
                let mut cursor = position;
                // Rotate the value down: equivalent to the moving hole.
                while cursor > first && score(&rows[cursor]) > score(&rows[cursor - 1]) {
                    rows.swap(cursor, cursor - 1);
                    cursor -= 1;
                }
                changes += 1;
                if incomplete && changes == 8 {
                    return position + 1 == last;
                }
            }
        }
        true
    }
    fn run<T>(rows: &mut [T], score: &dyn Fn(&T) -> f64, mut first: usize, mut last: usize) {
        loop {
            let count = last - first;
            if count <= 6 {
                insertion(rows, score, first, last, false);
                return;
            }
            let mut middle = first + count / 2;
            let last_index = last - 1;
            let points = if count < 1000 {
                vec![first, middle, last_index]
            } else {
                vec![first, first + count / 4, middle, middle + count / 4, last_index]
            };
            let mut swaps = small(rows, score, &points);
            let mut left = first;
            let mut right = last_index;
            let mut restart = false;
            if !before(rows, score, first, middle) {
                loop {
                    right -= 1;
                    if left == right {
                        left += 1;
                        right = last_index;
                        if !before(rows, score, first, right) {
                            loop {
                                if left == right {
                                    return;
                                }
                                if before(rows, score, first, left) {
                                    rows.swap(left, right);
                                    left += 1;
                                    break;
                                }
                                left += 1;
                            }
                        }
                        if left == right {
                            return;
                        }
                        loop {
                            while !before(rows, score, first, left) {
                                left += 1;
                            }
                            right -= 1;
                            while before(rows, score, first, right) {
                                right -= 1;
                            }
                            if left >= right {
                                break;
                            }
                            rows.swap(left, right);
                            left += 1;
                        }
                        first = left;
                        restart = true;
                        break;
                    }
                    if before(rows, score, right, middle) {
                        rows.swap(first, right);
                        swaps += 1;
                        break;
                    }
                }
                if restart {
                    continue;
                }
            }
            left = first + 1;
            if left < right {
                loop {
                    while before(rows, score, left, middle) {
                        left += 1;
                    }
                    right -= 1;
                    while !before(rows, score, right, middle) {
                        right -= 1;
                    }
                    if left > right {
                        break;
                    }
                    rows.swap(left, right);
                    swaps += 1;
                    if middle == left {
                        middle = right;
                    }
                    left += 1;
                }
            }
            if left != middle && before(rows, score, middle, left) {
                rows.swap(left, middle);
                swaps += 1;
            }
            if swaps == 0 {
                let left_done = insertion(rows, score, first, left, true);
                let right_done = insertion(rows, score, left + 1, last, true);
                if left_done && right_done {
                    return;
                }
                if left_done {
                    first = left + 1;
                    continue;
                }
                if right_done {
                    last = left;
                    continue;
                }
            }
            if left - first < last - left {
                run(rows, score, first, left);
                first = left + 1;
            } else {
                run(rows, score, left + 1, last);
                last = left;
            }
        }
    }
    let length = rows.len();
    run(rows.as_mut_slice(), score, 0, length);
}

/// One tag hit, grouped per (name, library).
#[derive(Clone)]
pub(super) struct TagEntry {
    tag: Value,
    rows: Vec<Value>,
    row: Value,
    tier: &'static str,
    count: usize,
}

type Typed = HashMap<String, Vec<Value>>;
type Tags = HashMap<String, Vec<TagEntry>>;

impl Runtime {
    /// The normal title pass (`Catalog.search`): word PREFIX, tokenised on whitespace,
    /// a one-character query equal to a title, a multi-word query in order or not.
    fn catalog_search(&self, query: &str) -> Vec<Value> {
        let needle = fold_query(query).trim().to_string();
        if needle.is_empty() {
            return Vec::new();
        }
        let searchable = |row: &&Value| {
            matches!(row.get("type").and_then(Value::as_str), Some("movie" | "show" | "season" | "episode"))
        };
        if needle.chars().count() == 1 {
            return self
                .catalog
                .items
                .iter()
                .filter(searchable)
                .filter(|row| fold_query(text_of(row, "title")).trim() == needle)
                .cloned()
                .collect();
        }
        let hit = |text: &str| -> bool {
            let title_words = words(&fold_query(text));
            if title_words.iter().any(|word| word.starts_with(needle.as_str())) || title_words.join(" ").starts_with(needle.as_str()) {
                return true;
            }
            let terms = needle.split(' ').filter(|t| !t.is_empty()).collect::<Vec<_>>();
            if terms.len() < 2 {
                return false;
            }
            let mut position = 0;
            let mut in_order = true;
            for term in &terms {
                while position < title_words.len() && !title_words[position].starts_with(term) {
                    position += 1;
                }
                if position == title_words.len() {
                    in_order = false;
                    break;
                }
                position += 1;
            }
            if in_order {
                return true;
            }
            terms.iter().all(|term| title_words.iter().any(|word| word.starts_with(term)))
        };
        self.catalog
            .items
            .iter()
            .filter(searchable)
            .filter(|row| hit(text_of(row, "title")) || hit(text_of(row, "originalTitle")))
            .cloned()
            .collect()
    }

    /// Every playlist row, smart first, then by sort title (`library.playlists`).
    fn playlist_rows(&self) -> Vec<Value> {
        let mut rows = self.playlists.values().map(|p| self.playlist_row(p, false, true)).collect::<Vec<_>>();
        rows.sort_by_key(|row| {
            (
                !row.get("smart").and_then(Value::as_bool).unwrap_or(false),
                row.get("titleSort").and_then(Value::as_str).filter(|s| !s.is_empty()).unwrap_or(text_of(row, "title")).to_string(),
            )
        });
        rows
    }

    fn search_row_sections(&self, kind: &str, row: &Value) -> HashSet<String> {
        if kind == "playlist" {
            let id = row.get("ratingKey").map(string_value).unwrap_or_default();
            return self
                .playlists
                .get(&id)
                .map(|playlist| {
                    self.playlist_keys(playlist)
                        .iter()
                        .filter_map(|key| self.catalog.get(key))
                        .map(|member| string_value(member.get("librarySectionID").unwrap_or(&Value::Null)))
                        .collect()
                })
                .unwrap_or_default();
        }
        HashSet::from([string_value(row.get("librarySectionID").unwrap_or(&Value::Null))])
    }

    fn spelling_index(&self) -> crate::runtime::spellfix::SearchIndex {
        self.spelling.clone().unwrap_or_default()
    }

    /// (typed row lists, tag hits) for one query, both ranked (`search_matches`).
    pub(super) fn search_matches(&self, text: &str, limit: Option<usize>, tagged_items: bool) -> (Typed, Tags) {
        let mut rows = self.catalog_search(text);
        let score_query = text.trim().to_string();
        let needle = score_query.to_lowercase();
        let needle_terms = words(&fold_query(text));
        let mut playlist_rows = Vec::new();
        if needle.chars().count() >= 2 {
            let first = needle.split_whitespace().next().unwrap_or("").to_string();
            for row in self.playlist_rows() {
                let title = text_of(&row, "title").to_lowercase();
                let hit = title
                    .split(|c: char| !(c.is_ascii_digit() || c.is_ascii_lowercase()))
                    .filter(|word| !word.is_empty())
                    .any(|word| word.starts_with(first.as_str()));
                if hit {
                    playlist_rows.push(row);
                }
            }
        }
        // (hub, row, tag, name, tier)
        let mut direct_tags: Vec<(&str, Value, Value, String, &'static str)> = Vec::new();
        for (hub, field) in SEARCH_TAG_HUBS {
            for row in &self.catalog.items {
                for tag in row.get(*field).and_then(Value::as_array).into_iter().flatten() {
                    let name = text_of(tag, "tag").to_string();
                    if let Some(tier) = tag_tier(&name, &needle_terms) {
                        if needle.chars().count() >= 2 {
                            direct_tags.push((hub, row.clone(), tag.clone(), name, tier));
                        }
                    }
                }
            }
        }
        let collection_hit = needle.chars().count() >= 2
            && self.catalog.items.iter().any(|row| {
                row.get("Collection")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .any(|tag| tag_tier(text_of(tag, "tag"), &needle_terms).is_some())
            });
        let normal_gate = rows.iter().any(|row| matches!(row.get("type").and_then(Value::as_str), Some("movie" | "show")))
            || !playlist_rows.is_empty()
            || collection_hit
            || !direct_tags.is_empty();
        let spelling = if normal_gate { None } else { Some(self.spelling_index()) };
        if let Some(spelling) = &spelling {
            let mut seen = rows.iter().filter_map(key_of).collect::<HashSet<_>>();
            for row in &self.catalog.items {
                if !matches!(row.get("type").and_then(Value::as_str), Some("movie" | "show")) {
                    continue;
                }
                let key = key_of(row).unwrap_or_default();
                if seen.contains(&key) {
                    continue;
                }
                if spelling.metadata_matches(row, text) {
                    rows.push(row.clone());
                    seen.insert(key);
                }
            }
        }
        let mut tag_matches = direct_tags.clone();
        if let Some(spelling) = &spelling {
            if needle.chars().count() >= 2 {
                let direct_keys = direct_tags
                    .iter()
                    .map(|(hub, row, _, name, _)| {
                        (hub.to_string(), name.clone(), string_value(row.get("librarySectionID").unwrap_or(&Value::Null)))
                    })
                    .collect::<HashSet<_>>();
                for (hub, field) in SEARCH_TAG_HUBS.iter().filter(|(hub, _)| matches!(*hub, "actor" | "director")) {
                    for row in &self.catalog.items {
                        for tag in row.get(*field).and_then(Value::as_array).into_iter().flatten() {
                            let name = text_of(tag, "tag").to_string();
                            let key = (hub.to_string(), name.clone(), string_value(row.get("librarySectionID").unwrap_or(&Value::Null)));
                            if direct_keys.contains(&key) {
                                continue;
                            }
                            if spelling.tag_matches(&name, text) {
                                tag_matches.push((hub, row.clone(), tag.clone(), name, "fuzzy"));
                            }
                        }
                    }
                }
            }
        }
        let mut tags: Tags = HashMap::new();
        for (hub, _field) in SEARCH_TAG_HUBS {
            let mut seen: BTreeMapLike = BTreeMapLike::default();
            for (matched, row, tag, name, tier) in &tag_matches {
                if matched != hub {
                    continue;
                }
                let key = (name.clone(), string_value(row.get("librarySectionID").unwrap_or(&Value::Null)));
                let entry = seen.entry(key, || TagEntry { tag: tag.clone(), rows: Vec::new(), row: row.clone(), tier, count: 0 });
                entry.rows.push(row.clone());
            }
            let mut kept = Vec::new();
            for (_key, mut entry) in seen.into_sorted() {
                let section_type = self
                    .section(&string_value(entry.row.get("librarySectionID").unwrap_or(&Value::Null)))
                    .and_then(|section| section.get("type"))
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                if !entry.rows.iter().any(|row| row.get("type").and_then(Value::as_str) == Some(section_type.as_str())) {
                    continue;
                }
                entry.count = entry.rows.len();
                kept.push(entry);
            }
            if let Some(limit) = limit {
                kept.truncate(limit);
            }
            if !kept.is_empty() {
                tags.insert(hub.to_string(), kept);
            }
        }
        let mut typed: Typed = HashMap::new();
        for row in &rows {
            typed.entry(row.get("type").and_then(Value::as_str).unwrap_or("").to_string()).or_default().push(row.clone());
        }
        if !playlist_rows.is_empty() {
            typed.insert("playlist".to_string(), playlist_rows);
        }
        if tagged_items {
            let mut seen = rows.iter().filter_map(key_of).collect::<HashSet<_>>();
            for (hub, row, tag, _name, _tier) in &tag_matches {
                let key = key_of(row).unwrap_or_default();
                if seen.contains(&key) {
                    continue;
                }
                let mut reasoned = row.clone();
                if let Some(object) = reasoned.as_object_mut() {
                    object.insert("_searchReason".to_string(), json!([hub, tag]));
                }
                typed.entry(row.get("type").and_then(Value::as_str).unwrap_or("").to_string()).or_default().push(reasoned);
                seen.insert(key);
            }
        }
        for (kind, candidates) in typed.iter_mut() {
            if matches!(kind.as_str(), "movie" | "show" | "episode") {
                let mut title_hits = candidates.iter().filter(|r| r.get("_searchReason").is_none()).cloned().collect::<Vec<_>>();
                let associated = candidates.iter().filter(|r| r.get("_searchReason").is_some()).cloned().collect::<Vec<_>>();
                title_hits.sort_by(|a, b| {
                    let key = |row: &Value| {
                        (text_of(row, "title").as_bytes().to_vec(), key_of(row).and_then(|k| k.parse::<i64>().ok()).unwrap_or(0))
                    };
                    key(b).cmp(&key(a))
                });
                let (exact, rest): (Vec<_>, Vec<_>) =
                    title_hits.into_iter().partition(|row| search_tier(row, &score_query) == "exact");
                *candidates = exact.into_iter().chain(rest).chain(associated).collect();
            }
            let query = score_query.clone();
            comparison_sort(candidates, &|row: &Value| search_score_value(row, &query));
            if let Some(limit) = limit {
                candidates.truncate(limit);
            }
        }
        (typed, tags)
    }

    /// Drop the libraries Visibility excludes from global search (`hidden=2`).
    fn searchable(&self, typed: Typed, tags: Tags) -> (Typed, Tags) {
        let hidden = self
            .catalog
            .sections
            .iter()
            .map(|s| string_value(s.get("key").unwrap_or(&Value::Null)))
            .filter(|key| self.section_visibility(key) >= 2)
            .collect::<HashSet<_>>();
        if hidden.is_empty() {
            return (typed, tags);
        }
        let typed = typed
            .into_iter()
            .map(|(kind, rows)| {
                let kept = rows
                    .into_iter()
                    .filter(|row| {
                        let sections = self.search_row_sections(&kind, row);
                        !(!sections.is_empty() && sections.is_subset(&hidden))
                    })
                    .collect();
                (kind, kept)
            })
            .collect();
        let tags = tags
            .into_iter()
            .map(|(hub, rows)| {
                let kept = rows
                    .into_iter()
                    .filter(|entry| !hidden.contains(&string_value(entry.row.get("librarySectionID").unwrap_or(&Value::Null))))
                    .collect();
                (hub, kept)
            })
            .collect();
        (typed, tags)
    }

    fn tag_result(&self, entry: &TagEntry, hub: &str) -> Map<String, Value> {
        let section_id = entry.row.get("librarySectionID").cloned().unwrap_or(Value::Null);
        let section_key = string_value(&section_id);
        let section = self.section(&section_key).cloned().unwrap_or(Value::Null);
        let id = entry.tag.get("id").cloned().unwrap_or(Value::Null);
        let id_text = string_value(&id);
        let section_number = section_key.parse::<i64>().map(Value::from).unwrap_or(section_id.clone());
        let mut result = Map::new();
        result.insert("key".to_string(), json!(format!("/library/sections/{section_key}/all?{hub}={id_text}")));
        result.insert("librarySectionID".to_string(), section_id.clone());
        result.insert("librarySectionKey".to_string(), json!(format!("/library/sections/{section_key}")));
        result.insert("librarySectionTitle".to_string(), section.get("title").cloned().unwrap_or(Value::Null));
        result.insert(
            "librarySectionType".to_string(),
            json!(if section.get("type").and_then(Value::as_str) == Some("show") { 2 } else { 1 }),
        );
        result.insert("reason".to_string(), json!("section"));
        result.insert("reasonID".to_string(), section_number);
        result.insert("reasonTitle".to_string(), section.get("title").cloned().unwrap_or(Value::Null));
        result.insert("score".to_string(), json!(tag_score(entry.tier)));
        result.insert("type".to_string(), json!("tag"));
        result.insert("id".to_string(), id);
        result.insert("filter".to_string(), json!(format!("{hub}={id_text}")));
        result.insert("tag".to_string(), entry.tag.get("tag").cloned().unwrap_or(Value::Null));
        result.insert(
            "tagType".to_string(),
            json!(match hub {
                "actor" => 6,
                "director" => 4,
                _ => 1,
            }),
        );
        if let Some(key) = entry.tag.get("tagKey").filter(|v| !v.is_null() && *v != "") {
            result.insert("tagKey".to_string(), key.clone());
        } else if hub == "actor" || hub == "director" {
            result.insert("tagKey".to_string(), json!(crate::hierarchy::person_tag_key(text_of(&entry.tag, "tag"))));
        }
        if let Some(thumb) = entry.tag.get("thumb").filter(|v| !v.is_null() && *v != "") {
            result.insert("thumb".to_string(), thumb.clone());
        }
        result.insert("count".to_string(), json!(entry.count));
        result
    }

    /// The collections a query matches, as `tagType: 2` tag rows (cycles 15, 28, 30, 57).
    fn collection_matches(&self, needle: &str, section_filter: Option<&str>) -> Vec<Map<String, Value>> {
        let terms = words(&fold_query(needle));
        let mut found = Vec::new();
        for section in &self.catalog.sections {
            let section_key = string_value(section.get("key").unwrap_or(&Value::Null));
            if section_filter.is_some_and(|wanted| wanted != section_key) {
                continue;
            }
            // name -> (tag, rows, tier)
            let mut seen: BTreeMap<String, (Value, Vec<String>, &'static str)> = BTreeMap::new();
            if needle.chars().count() >= 2 {
                for row in &self.catalog.items {
                    if string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) != section_key {
                        continue;
                    }
                    for tag in row.get("Collection").and_then(Value::as_array).into_iter().flatten() {
                        let name = text_of(tag, "tag").to_string();
                        let Some(tier) = tag_tier(&name, &terms) else { continue };
                        let entry = seen.entry(name).or_insert_with(|| (tag.clone(), Vec::new(), tier));
                        let key = key_of(row).unwrap_or_default();
                        if !entry.1.contains(&key) {
                            entry.1.push(key);
                        }
                    }
                }
                for collection in self.collections.values().filter(|c| c.section == section_key) {
                    let Some(tier) = tag_tier(&collection.title, &terms) else { continue };
                    let row = self.dynamic_collection_row(collection);
                    let entry = seen.entry(collection.title.clone()).or_insert_with(|| {
                        (json!({"tag": collection.title, "id": row.get("index").cloned().unwrap_or(Value::Null)}), Vec::new(), tier)
                    });
                    for key in &collection.items {
                        if !entry.1.contains(key) {
                            entry.1.push(key.clone());
                        }
                    }
                }
            }
            for (name, (tag, rows, tier)) in seen {
                let id = tag.get("id").cloned().unwrap_or(Value::Null);
                let id_text = string_value(&id);
                let section_number = section_key.parse::<i64>().map(Value::from).unwrap_or(json!(section_key));
                let mut result = Map::new();
                result.insert("key".to_string(), json!(format!("/library/sections/{section_key}/all?collection={id_text}")));
                result.insert("librarySectionID".to_string(), section_number.clone());
                result.insert("librarySectionKey".to_string(), json!(format!("/library/sections/{section_key}")));
                result.insert("librarySectionTitle".to_string(), section.get("title").cloned().unwrap_or(Value::Null));
                result.insert(
                    "librarySectionType".to_string(),
                    json!(if section.get("type").and_then(Value::as_str) == Some("show") { 2 } else { 1 }),
                );
                result.insert("reason".to_string(), json!("section"));
                result.insert("reasonID".to_string(), section_number);
                result.insert("reasonTitle".to_string(), section.get("title").cloned().unwrap_or(Value::Null));
                result.insert("score".to_string(), json!(tag_score(tier)));
                result.insert("type".to_string(), json!("tag"));
                result.insert("id".to_string(), id);
                result.insert("filter".to_string(), json!(format!("collection={id_text}")));
                result.insert("tag".to_string(), json!(name));
                result.insert("tagType".to_string(), json!(2));
                result.insert("count".to_string(), json!(rows.len()));
                if let Some(collection) = self.collections.values().find(|c| c.section == section_key && c.title == name) {
                    if let Some(guid) = self.dynamic_collection_row(collection).get("guid") {
                        result.insert("guid".to_string(), guid.clone());
                    }
                }
                found.push(result);
            }
        }
        found
    }

    /// A search hit row, led by `librarySectionTitle` and then its `score` (cycle 25).
    fn search_hit_row(&self, row: &Value, query: &HashMap<String, String>, score: Option<String>, flat: bool) -> Value {
        let is_show = row.get("type").and_then(Value::as_str) == Some("show");
        let mut shown = self.grid_row(row, true, !flat, is_show);
        if !flat {
            shown = guid_gate(shown, query);
        }
        let Some(object) = shown.as_object_mut() else { return shown };
        if flat && !is_show {
            object.shift_remove("librarySectionID");
            object.shift_remove("librarySectionKey");
        }
        let mut ordered = Map::new();
        // The marker the XML/JSON orderer leads the hit by; never serialised.
        ordered.insert("_searchhit".to_string(), json!(true));
        if let Some(title) = object.shift_remove("librarySectionTitle") {
            ordered.insert("librarySectionTitle".to_string(), title);
        }
        if let Some(score) = score {
            ordered.insert("score".to_string(), json!(score));
        }
        for (key, value) in object.iter() {
            ordered.insert(key.clone(), value.clone());
        }
        Value::Object(ordered)
    }

    /// `/hubs/search` -- seventeen hubs every time.
    pub(super) fn hubs_search(&self, query: &HashMap<String, String>) -> Response {
        let Some(text) = query.get("query").filter(|t| !t.is_empty()) else {
            return Response::error(400, "plexsim: bad request");
        };
        let wanted_section = match query.get("contentDirectoryID").filter(|v| !v.is_empty()) {
            Some(raw) => {
                let normal = if int_param(raw, None) {
                    raw.trim_start_matches('+').parse::<i64>().map(|n| n.to_string()).unwrap_or(raw.clone())
                } else {
                    raw.clone()
                };
                if self.section(&normal).is_none() || self.section_visibility(&normal) >= 2 {
                    return Response::error(404, "plexsim: not found");
                }
                Some(normal)
            }
            None => None,
        };
        if let Some(raw) = query.get("limit") {
            if !int_param(raw, None) {
                return Response::error(400, "plexsim: bad request");
            }
            let value = raw.trim_start_matches('+').parse::<i128>().unwrap_or(0);
            if value <= 0 || !int_param(raw, Some(64)) {
                return Response::error(500, "plexsim: internal error");
            }
        }
        let limit = query
            .get("limit")
            .and_then(|raw| raw.trim_start_matches('+').parse::<usize>().ok())
            .unwrap_or(3);
        let (matched, found_tags) = self.search_matches(text, Some(limit), true);
        let (mut typed, mut tags) = self.searchable(matched, found_tags);
        if let Some(section) = &wanted_section {
            for (kind, rows) in typed.iter_mut() {
                rows.retain(|row| self.search_row_sections(kind, row).contains(section));
            }
            for entries in tags.values_mut() {
                entries.retain(|entry| &string_value(entry.row.get("librarySectionID").unwrap_or(&Value::Null)) == section);
            }
        }
        let score_query = text.trim().to_string();
        let needle = score_query.to_lowercase();
        let mut order = SEARCH_HUBS.to_vec();
        if query.get("includeCollections").map(String::as_str) == Some("1") {
            let position = order.iter().position(|(kind, _)| *kind == "collection").unwrap_or(0);
            let collection = order.remove(position);
            let track = order.iter().position(|(kind, _)| *kind == "track").unwrap_or(0);
            order.insert(track + 1, collection);
        }
        let mut hubs: Vec<(i64, Value)> = Vec::new();
        for (kind, title) in order {
            let mut rows = Vec::new();
            let mut classes = Vec::new();
            for row in typed.get(kind).cloned().unwrap_or_default() {
                let tier = search_tier(&row, &score_query);
                classes.push(tier_class(tier));
                let mut entry = self.search_hit_row(&row, query, Some(score_text(search_score_value(&row, &score_query))), false);
                if tier == "fuzzy" {
                    if let Some(object) = entry.as_object_mut() {
                        let reason = row.get("_searchReason").and_then(Value::as_array).cloned();
                        if let Some(reason) = reason {
                            object.insert("reason".to_string(), reason[0].clone());
                            object.insert("reasonID".to_string(), reason[1].get("id").cloned().unwrap_or(Value::Null));
                            object.insert("reasonTitle".to_string(), reason[1].get("tag").cloned().unwrap_or(Value::Null));
                        } else {
                            let key = key_of(&row).unwrap_or_default();
                            'outer: for (hub, _) in SEARCH_TAG_HUBS {
                                for entry_tag in tags.get(*hub).into_iter().flatten() {
                                    if entry_tag.rows.iter().any(|tagged| key_of(tagged).as_deref() == Some(key.as_str())) {
                                        object.insert("reason".to_string(), json!(hub));
                                        object.insert("reasonID".to_string(), entry_tag.tag.get("id").cloned().unwrap_or(Value::Null));
                                        object.insert("reasonTitle".to_string(), entry_tag.tag.get("tag").cloned().unwrap_or(Value::Null));
                                        break 'outer;
                                    }
                                }
                            }
                        }
                    }
                }
                rows.push(entry);
            }
            let mut hub = Map::new();
            hub.insert("title".to_string(), json!(title));
            hub.insert("type".to_string(), json!(kind));
            hub.insert("hubIdentifier".to_string(), json!(kind));
            hub.insert("context".to_string(), json!(""));
            hub.insert("size".to_string(), json!(rows.len()));
            hub.insert("more".to_string(), json!(false));
            hub.insert("style".to_string(), json!("shelf"));
            if !rows.is_empty() {
                hub.insert("Metadata".to_string(), Value::Array(rows));
            }
            if kind == "collection" {
                let found = self.collection_matches(&needle, wanted_section.as_deref());
                if !found.is_empty() {
                    for entry in &found {
                        let score = entry.get("score").and_then(Value::as_str).and_then(|s| s.parse::<f64>().ok()).unwrap_or(0.0);
                        classes.push(if score >= 0.9 { 0 } else if score >= 0.7 { 1 } else if score >= 0.5 { 2 } else { 3 });
                    }
                    hub.insert("size".to_string(), json!(found.len()));
                    hub.insert("Directory".to_string(), Value::Array(found.into_iter().map(Value::Object).collect()));
                }
            } else if let Some(entries) = tags.get(kind) {
                let directory = entries.iter().map(|entry| Value::Object(self.tag_result(entry, kind))).collect::<Vec<_>>();
                classes.extend(entries.iter().map(|entry| tier_class(entry.tier)));
                hub.insert("size".to_string(), json!(directory.len()));
                hub.insert("Directory".to_string(), Value::Array(directory));
            }
            hubs.push((search_hub_rank(kind, &classes), Value::Object(hub)));
        }
        comparison_sort(&mut hubs, &|hub: &(i64, Value)| hub.0 as f64);
        let hubs = hubs.into_iter().map(|(_, hub)| hub).collect::<Vec<_>>();
        Response::json(200, json!({"MediaContainer": {"size": hubs.len(), "Hub": hubs}}))
    }

    /// Whether one `searchTypes=` value selects this hit (cycle 28).
    fn search_type_selects(&self, tokens: &Option<HashSet<String>>, kind: &str, row: &Value) -> bool {
        let Some(tokens) = tokens else { return true };
        if matches!(kind, "actor" | "director") {
            return tokens.contains("people");
        }
        if kind == "movie" {
            return tokens.contains("movies");
        }
        if matches!(kind, "show" | "season" | "episode") {
            return tokens.contains("tv");
        }
        let mut families = HashSet::new();
        for section_id in self.search_row_sections(kind, row) {
            let is_show = self.section(&section_id).and_then(|s| s.get("type")).and_then(Value::as_str) == Some("show");
            if is_show {
                families.insert("tv");
            } else {
                families.insert("movies");
                families.insert("otherVideos");
            }
        }
        families.iter().any(|family| tokens.contains(*family))
    }

    /// `/library/search` -- the same matches as one flat `SearchResult` list.
    pub(super) fn flat_search(&self, query: &HashMap<String, String>) -> Response {
        let Some(text) = query.get("query") else {
            return Response::error(400, "plexsim: bad request");
        };
        if text.is_empty() {
            return Response::json(200, json!({"MediaContainer": {"size": 0}}));
        }
        if let Some(raw) = query.get("limit") {
            if !int_param(raw, None) {
                return Response::error(400, "plexsim: bad request");
            }
            let value = raw.trim_start_matches('+').parse::<i128>().unwrap_or(0);
            if value <= 0 || !int_param(raw, Some(64)) {
                return Response::error(500, "plexsim: internal error");
            }
        }
        let limit = query
            .get("limit")
            .and_then(|raw| raw.trim_start_matches('+').parse::<usize>().ok())
            .unwrap_or(10);
        let tokens = query
            .get("searchTypes")
            .filter(|v| !v.is_empty())
            .map(|value| value.split(',').map(str::to_string).collect::<HashSet<_>>());
        let (matched, found_tags) = self.search_matches(text, Some(limit), false);
        let (typed, tags) = self.searchable(matched, found_tags);
        let score_query = text.trim().to_string();
        let needle = score_query.to_lowercase();
        let mut results: Vec<Value> = Vec::new();
        let metadata = |results: &mut Vec<Value>, kind: &str| {
            for row in typed.get(kind).into_iter().flatten() {
                if !self.search_type_selects(&tokens, kind, row) {
                    continue;
                }
                let score = score_text(search_score_value(row, &score_query)).parse::<f64>().unwrap_or(0.0);
                results.push(json!({"score": score, "Metadata": self.search_hit_row(row, query, None, true)}));
            }
        };
        let flat_tag = |mut shown: Map<String, Value>| {
            // `shift_remove`: `Map::remove` is a SWAP remove under preserve_order, which
            // moved the row's last attribute (`count`) into the removed slot.
            let score = shown
                .shift_remove("score")
                .and_then(|value| value.as_str().and_then(|s| s.parse::<f64>().ok()))
                .unwrap_or(0.32);
            for name in ["reason", "reasonID", "reasonTitle"] {
                shown.shift_remove(name);
            }
            json!({"score": score, "Directory": shown})
        };
        let tag_rows = |results: &mut Vec<Value>, kind: &str| {
            for entry in tags.get(kind).into_iter().flatten() {
                if !self.search_type_selects(&tokens, kind, &entry.row) {
                    continue;
                }
                results.push(flat_tag(self.tag_result(entry, kind)));
            }
        };
        for kind in ["movie", "show", "episode", "artist", "album", "track", "collection", "photoalbum"] {
            metadata(&mut results, kind);
        }
        tag_rows(&mut results, "autotag");
        metadata(&mut results, "photo");
        for kind in ["tag", "actor", "director", "genre"] {
            tag_rows(&mut results, kind);
        }
        for row in self.collection_matches(&needle, None) {
            if !self.search_type_selects(&tokens, "collection", &Value::Object(row.clone())) {
                continue;
            }
            results.push(flat_tag(row));
        }
        metadata(&mut results, "playlist");
        for kind in ["shared", "place"] {
            tag_rows(&mut results, kind);
            metadata(&mut results, kind);
        }
        results.truncate(limit);
        Response::json(200, json!({"MediaContainer": {"size": results.len(), "SearchResult": results}}))
    }
}

/// A tiny ordered map for the tag grouping: `sorted(seen)` in Python.
#[derive(Default)]
struct BTreeMapLike {
    entries: BTreeMap<(String, String), TagEntry>,
}

impl BTreeMapLike {
    fn entry(&mut self, key: (String, String), make: impl FnOnce() -> TagEntry) -> &mut TagEntry {
        self.entries.entry(key).or_insert_with(make)
    }

    fn into_sorted(self) -> Vec<((String, String), TagEntry)> {
        self.entries.into_iter().collect()
    }
}

use std::collections::BTreeMap;
