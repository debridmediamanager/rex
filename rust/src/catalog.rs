//! Native catalog model shared by the future library and HTTP layers.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::Path;

use serde_json::{Map, Value};

use crate::{hierarchy, mediainfo};

#[derive(Debug, Clone)]
pub struct Catalog {
    /// Untyped top-level document, retained so future routes can preserve
    /// arbitrary server properties rather than narrowing the catalog schema.
    pub document: Map<String, Value>,
    pub sections: Vec<Value>,
    /// Private so that every mutable path is seen: `get_mut` records its row and
    /// `items_mut` marks them all, which is how a save writes only what changed
    /// (`store.rs`) instead of serialising every row to find out.
    items: Vec<Value>,
    pub by_id: HashMap<String, usize>,
    changes: Changes,
    /// The rows written since the last `take_touched`, kept apart from `changes` because a
    /// save takes those: the runtime re-stamps the artwork paths of these (`restamp_artwork`).
    touched: Changes,
    /// `children`'s rows by parent key and each section's movies and shows, built on first
    /// use. Without it a listing scanned all rows once per show and per season: 9 s for a
    /// page of the production server's 1,500 movies and over 30 s for its 13,500 episodes
    /// (2026-10-10). A row written in place re-files only itself: dropping the index on
    /// every `get_mut` made the first listing after each progress beat rescan the library,
    /// which on the production server (2026-10-16) paged all of it back in from swap.
    index: RowIndex,
}

/// `Catalog::index`: positions by parent and by section, and the positions written since.
#[derive(Default)]
struct RowIndex(std::sync::Mutex<IndexState>);

#[derive(Default)]
struct IndexState {
    built: Option<Positions>,
    stale: Vec<usize>,
}

#[derive(Default)]
struct Positions {
    parent_of: Vec<Option<String>>,
    children: HashMap<String, Vec<usize>>,
    section_of: Vec<Option<String>>,
    sections: HashMap<String, Vec<usize>>,
}

/// A clone builds its own index when it is first read.
impl Clone for RowIndex {
    fn clone(&self) -> Self {
        Self::default()
    }
}

impl std::fmt::Debug for RowIndex {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("RowIndex")
    }
}

fn key_text(value: Option<&Value>) -> Option<String> {
    match value {
        Some(Value::String(value)) => Some(value.clone()),
        Some(Value::Number(value)) => Some(value.to_string()),
        _ => None,
    }
}

/// The section a movie or show row is listed in; other rows are listed under their parents.
fn section_of(item: &Value) -> Option<String> {
    matches!(item.get("type").and_then(Value::as_str), Some("movie" | "show"))
        .then(|| match item.get("librarySectionID") {
            Some(Value::Bool(value)) => value.to_string(),
            other => key_text(other).unwrap_or_default(),
        })
}

fn file(map: &mut HashMap<String, Vec<usize>>, key: Option<&String>, position: usize, add: bool) {
    let Some(key) = key else {
        return;
    };
    let list = map.entry(key.clone()).or_default();
    match (list.binary_search(&position), add) {
        (Err(at), true) => list.insert(at, position),
        (Ok(at), false) => {
            list.remove(at);
        }
        _ => {}
    }
}

impl RowIndex {
    fn drop_all(&self) {
        let mut state = self.0.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        state.built = None;
        state.stale.clear();
    }

    fn mark(&self, position: usize) {
        let mut state = self.0.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.built.is_some() {
            state.stale.push(position);
        }
    }

    /// The positions `pick` reads out of the index, brought up to date with `items` first.
    fn read<T>(&self, items: &[Value], pick: impl FnOnce(&Positions) -> T) -> T {
        let mut state = self.0.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let stale = std::mem::take(&mut state.stale);
        let positions = state.built.get_or_insert_with(|| {
            let mut positions = Positions::default();
            for (position, item) in items.iter().enumerate() {
                let parent = key_text(item.get("parentRatingKey"));
                let section = section_of(item);
                if let Some(parent) = &parent {
                    positions.children.entry(parent.clone()).or_default().push(position);
                }
                if let Some(section) = &section {
                    positions.sections.entry(section.clone()).or_default().push(position);
                }
                positions.parent_of.push(parent);
                positions.section_of.push(section);
            }
            positions
        });
        for position in stale {
            let Some(item) = items.get(position) else {
                continue;
            };
            if positions.parent_of.len() <= position {
                positions.parent_of.resize(position + 1, None);
                positions.section_of.resize(position + 1, None);
            }
            let parent = key_text(item.get("parentRatingKey"));
            if parent != positions.parent_of[position] {
                file(&mut positions.children, positions.parent_of[position].as_ref(), position, false);
                file(&mut positions.children, parent.as_ref(), position, true);
                positions.parent_of[position] = parent;
            }
            let section = section_of(item);
            if section != positions.section_of[position] {
                file(&mut positions.sections, positions.section_of[position].as_ref(), position, false);
                file(&mut positions.sections, section.as_ref(), position, true);
                positions.section_of[position] = section;
            }
        }
        pick(positions)
    }
}

/// The rows changed since the last `take_changes`: by key, or all of them.
#[derive(Debug, Clone, Default)]
pub struct Changes {
    pub all: bool,
    pub keys: HashSet<String>,
}

impl Catalog {
    pub fn from_value(value: Value) -> Result<Self, String> {
        let Value::Object(mut document) = value else {
            return Err("catalog must be a JSON object".to_string());
        };
        // Taken out of the document, which keeps the keys in place (`to_value` writes them
        // back): copying them held every row twice for the life of the server.
        let mut take_array = |name: &str| match document.get_mut(name).map(Value::take) {
            Some(Value::Array(rows)) => rows,
            _ => Vec::new(),
        };
        let sections = take_array("sections");
        let items = take_array("items");
        let mut catalog = Self {
            document,
            sections,
            items,
            by_id: HashMap::new(),
            changes: Changes { all: true, keys: HashSet::new() },
            touched: Changes { all: true, keys: HashSet::new() },
            index: RowIndex::default(),
        };
        catalog.rebuild_index();
        Ok(catalog)
    }

    pub fn load(path: impl AsRef<Path>) -> Result<Self, String> {
        let text =
            fs::read_to_string(path.as_ref()).map_err(|e| format!("cannot read catalog: {e}"))?;
        let value =
            serde_json::from_str(&text).map_err(|e| format!("cannot parse catalog: {e}"))?;
        Self::from_value(value)
    }

    pub fn to_value(&self) -> Value {
        let mut document = self.document.clone();
        document.insert("sections".to_string(), Value::Array(self.sections.clone()));
        document.insert("items".to_string(), Value::Array(self.items.clone()));
        Value::Object(document)
    }

    pub fn rebuild_index(&mut self) {
        self.index.drop_all();
        self.by_id.clear();
        for (index, item) in self.items.iter().enumerate() {
            if let Some(key) = key_value(item) {
                self.by_id.insert(key, index);
            }
        }
    }

    pub fn get(&self, rating_key: &str) -> Option<&Value> {
        self.by_id
            .get(rating_key)
            .and_then(|index| self.items.get(*index))
    }
    pub fn get_mut(&mut self, rating_key: &str) -> Option<&mut Value> {
        let index = *self.by_id.get(rating_key)?;
        if !self.changes.all {
            self.changes.keys.insert(rating_key.to_string());
        }
        if !self.touched.all {
            self.touched.keys.insert(rating_key.to_string());
        }
        self.index.mark(index);
        self.items.get_mut(index)
    }

    pub fn items(&self) -> &Vec<Value> {
        &self.items
    }

    /// Every row, mutably: a save then looks at them all.
    pub fn items_mut(&mut self) -> &mut Vec<Value> {
        self.changes.all = true;
        self.touched.all = true;
        self.index.drop_all();
        &mut self.items
    }

    pub fn take_changes(&mut self) -> Changes {
        std::mem::take(&mut self.changes)
    }

    pub fn take_touched(&mut self) -> Changes {
        std::mem::take(&mut self.touched)
    }

    pub fn children(&self, parent_key: &str) -> Vec<Value> {
        self.children_refs(parent_key).into_iter().cloned().collect()
    }

    /// `children` without the copies, for callers that only read or count.
    pub fn children_refs(&self, parent_key: &str) -> Vec<&Value> {
        let positions = self.index.read(&self.items, |index| index.children.get(parent_key).cloned());
        positions.into_iter().flatten().filter_map(|position| self.items.get(position)).collect()
    }

    /// A section's movies and shows, in row order.
    pub fn section_refs(&self, section_key: &str) -> Vec<&Value> {
        let positions = self.index.read(&self.items, |index| index.sections.get(section_key).cloned());
        positions.into_iter().flatten().filter_map(|position| self.items.get(position)).collect()
    }

    /// Append a row. A row with a key is recorded by its key, so a save writes that one
    /// row (`store.rs` appends it) rather than looking at every row: a scan adds rows to a
    /// live library one release at a time.
    pub fn add(&mut self, item: Value) {
        let key = key_value(&item);
        self.items.push(item);
        self.index.mark(self.items.len() - 1);
        match key {
            Some(key) => {
                self.by_id.insert(key.clone(), self.items.len() - 1);
                if !self.changes.all {
                    self.changes.keys.insert(key.clone());
                }
                if !self.touched.all {
                    self.touched.keys.insert(key);
                }
            }
            None => {
                self.changes.all = true;
                self.touched.all = true;
            }
        }
    }
    /// Remove a row; the others keep their order, so a save deletes the one row.
    pub fn remove(&mut self, rating_key: &str) -> Option<Value> {
        let index = *self.by_id.get(rating_key)?;
        if !self.changes.all {
            self.changes.keys.insert(rating_key.to_string());
        }
        let removed = self.items.remove(index);
        self.rebuild_index();
        Some(removed)
    }

    /// Remove several rows with one reindex: `remove` reindexes every row each time.
    pub fn remove_many(&mut self, rating_keys: &HashSet<String>) {
        if rating_keys.is_empty() {
            return;
        }
        if !self.changes.all {
            self.changes.keys.extend(rating_keys.iter().cloned());
        }
        self.items.retain(|item| key_value(item).is_none_or(|key| !rating_keys.contains(&key)));
        self.rebuild_index();
    }

    /// Where a row sits in `items`, which is the order a save stores rows in.
    pub fn position(&self, rating_key: &str) -> Option<usize> {
        self.by_id.get(rating_key).copied()
    }

    /// Apply load-time corrections to row hierarchy, media fields, and stream
    /// projections. This is deliberately explicit: callers can load raw rows
    /// for diagnostics without silently changing the evidence document.
    pub fn normalize(&mut self) {
        self.changes.all = true;
        for item in self.items.iter_mut() {
            if let Some(media) = item.get_mut("Media").and_then(Value::as_array_mut) {
                for media in media.iter_mut() {
                    mediainfo::normalize_media(media);
                }
            }
        }
        hierarchy::normalize_rows(&mut self.items);
        self.rebuild_index();
    }

    /// `server.py` `_retitle_streams`: every Stream's display titles recomputed from
    /// the row's own fields, so a catalog built before a title rule was measured is
    /// corrected rather than needing a rebuild. A server-load step, not part of
    /// `normalize`: Python runs it in `Catalog.__init__` after `normalize_media` and
    /// `normalize_rows`, and the `catalog-normalize` parity command mirrors only those
    /// two, which leave the stored titles alone.
    pub fn retitle_streams(&mut self) {
        self.changes.all = true;
        for item in self.items.iter_mut() {
            for media in item
                .get_mut("Media")
                .and_then(Value::as_array_mut)
                .into_iter()
                .flatten()
            {
                for part in media
                    .get_mut("Part")
                    .and_then(Value::as_array_mut)
                    .into_iter()
                    .flatten()
                {
                    for stream in part
                        .get_mut("Stream")
                        .and_then(Value::as_array_mut)
                        .into_iter()
                        .flatten()
                    {
                        if let Some(stream) = stream.as_object_mut() {
                            mediainfo::stream_titles(stream, None, None);
                        }
                    }
                }
            }
        }
    }
}

fn key_value(item: &Value) -> Option<String> {
    item.get("ratingKey").and_then(|v| match v {
        Value::String(v) => Some(v.clone()),
        Value::Number(v) => Some(v.to_string()),
        _ => None,
    })
}

impl TryFrom<Value> for Catalog {
    type Error = String;
    fn try_from(value: Value) -> Result<Self, Self::Error> {
        Self::from_value(value)
    }
}

#[cfg(test)]
mod tests {
    /// What a show listing asks for every show and every season (`leafCount`,
    /// `childCount`), on the production server's library as it stood on 2026-10-10: 361
    /// shows, 698 seasons, 13,509 episodes and 1,565 movies.
    #[test]
    fn children_of_every_show_and_season_does_not_rescan_the_library_each_time() {
        let shape: Vec<Vec<usize>> =
            serde_json::from_str(include_str!("../tests/fixtures/fun_show_shape.json")).unwrap();
        let mut items = Vec::new();
        let mut next = 100_000u64;
        let mut key = || {
            next += 1;
            next.to_string()
        };
        for _ in 0..1565 {
            items.push(serde_json::json!({"ratingKey": key(), "type": "movie"}));
        }
        let mut parents = Vec::new();
        for seasons in &shape {
            let show = key();
            items.push(serde_json::json!({"ratingKey": show, "type": "show"}));
            parents.push((show.clone(), seasons.len()));
            for (index, episodes) in seasons.iter().enumerate() {
                let season = key();
                items.push(serde_json::json!({"ratingKey": season, "type": "season",
                                              "parentRatingKey": show, "index": index + 1}));
                parents.push((season.clone(), *episodes));
                for episode in 0..*episodes {
                    // Numeric parents, as rows a feed placed sometimes carry.
                    items.push(serde_json::json!({"ratingKey": key(), "type": "episode",
                                                  "parentRatingKey": season.parse::<u64>().unwrap(),
                                                  "index": episode + 1}));
                }
            }
        }
        let catalog = Catalog::from_value(serde_json::json!({"items": items})).unwrap();
        let started = std::time::Instant::now();
        for (parent, count) in &parents {
            assert_eq!(catalog.children(parent).len(), *count, "children of {parent}");
        }
        let elapsed = started.elapsed();
        assert!(elapsed < std::time::Duration::from_millis(500), "1,059 lookups took {elapsed:?}");
    }

    #[test]
    fn children_follow_a_row_added_or_reparented_after_a_lookup() {
        let mut catalog = Catalog::from_value(serde_json::json!({"items": [
            {"ratingKey": "1", "type": "season"},
            {"ratingKey": "2", "type": "episode", "parentRatingKey": "1"}]})).unwrap();
        assert_eq!(catalog.children("1").len(), 1);
        catalog.add(serde_json::json!({"ratingKey": "3", "type": "episode", "parentRatingKey": "1"}));
        assert_eq!(catalog.children("1").len(), 2);
        catalog.get_mut("2").unwrap()["parentRatingKey"] = serde_json::json!("9");
        assert_eq!(catalog.children("1").len(), 1);
        assert_eq!(catalog.children("9").len(), 1);
        catalog.remove("3");
        assert!(catalog.children("1").is_empty());
    }

    #[test]
    fn a_section_lists_its_movies_and_shows_through_adds_moves_and_removes() {
        let mut catalog = Catalog::from_value(serde_json::json!({"items": [
            {"ratingKey": "1", "type": "movie", "librarySectionID": 1},
            {"ratingKey": "2", "type": "show", "librarySectionID": "2"},
            {"ratingKey": "3", "type": "season", "librarySectionID": 2, "parentRatingKey": "2"},
            {"ratingKey": "4", "type": "movie", "librarySectionID": "1"}]})).unwrap();
        let keys = |catalog: &Catalog, section: &str| -> Vec<String> {
            catalog.section_refs(section).iter().filter_map(|row| key_value(row)).collect()
        };
        assert_eq!(keys(&catalog, "1"), ["1", "4"]);
        assert_eq!(keys(&catalog, "2"), ["2"]);
        catalog.add(serde_json::json!({"ratingKey": "5", "type": "movie", "librarySectionID": 1}));
        assert_eq!(keys(&catalog, "1"), ["1", "4", "5"]);
        // A film moved among the shows keeps its place in row order there.
        catalog.get_mut("1").unwrap()["librarySectionID"] = serde_json::json!(2);
        assert_eq!(keys(&catalog, "1"), ["4", "5"]);
        assert_eq!(keys(&catalog, "2"), ["1", "2"]);
        catalog.get_mut("4").unwrap()["type"] = serde_json::json!("episode");
        assert_eq!(keys(&catalog, "1"), ["5"]);
        catalog.remove("2");
        assert_eq!(keys(&catalog, "2"), ["1"]);
    }

    /// A progress beat writes one row in place; the listings after it read the index. On
    /// the production server (2026-10-16) every write dropped the index, so each listing after
    /// a beat scanned all 40,197 rows again: 2,000 alternations took 4 s here, unfixed.
    #[test]
    fn a_row_written_in_place_does_not_rescan_the_library() {
        let mut items = Vec::new();
        for show in 0..400u64 {
            let show_key = (100_000 + show * 100).to_string();
            items.push(serde_json::json!({"ratingKey": show_key, "type": "show", "librarySectionID": 2}));
            for episode in 1..100u64 {
                items.push(serde_json::json!({"ratingKey": (100_000 + show * 100 + episode).to_string(),
                                              "type": "episode", "parentRatingKey": show_key}));
            }
        }
        let mut catalog = Catalog::from_value(serde_json::json!({"items": items})).unwrap();
        assert_eq!(catalog.children("100000").len(), 99);
        let started = std::time::Instant::now();
        for beat in 0..2000u64 {
            let key = (100_000 + (beat % 400) * 100 + 1 + beat % 99).to_string();
            catalog.get_mut(&key).unwrap()["viewOffset"] = serde_json::json!(beat * 1000);
            assert_eq!(catalog.children(&(100_000 + (beat % 400) * 100).to_string()).len(), 99);
            assert_eq!(catalog.section_refs("2").len(), 400);
        }
        let elapsed = started.elapsed();
        assert!(elapsed < std::time::Duration::from_millis(500), "2,000 beats took {elapsed:?}");
    }

    use super::*;
    #[test]
    fn preserves_unknown_document_properties() {
        let catalog = Catalog::from_value(
            serde_json::json!({"custom":{"x":1},"sections":[],"items":[{"ratingKey":"1"}]}),
        )
        .unwrap();
        assert_eq!(catalog.to_value()["custom"]["x"], 1);
        assert_eq!(catalog.get("1").unwrap()["ratingKey"], "1");
    }

    #[test]
    fn add_and_remove_record_only_their_own_key() {
        let mut catalog = Catalog::from_value(serde_json::json!({"sections":[],"items":[
            {"ratingKey":"1"},{"ratingKey":"2"},{"ratingKey":"3"}]}))
        .unwrap();
        catalog.take_changes();
        catalog.add(serde_json::json!({"ratingKey":"9"}));
        catalog.remove("2");
        let changes = catalog.take_changes();
        assert!(!changes.all);
        assert_eq!(changes.keys, HashSet::from(["9".to_string(), "2".to_string()]));
        assert_eq!(catalog.position("9"), Some(2));
        assert_eq!(catalog.get("3").unwrap()["ratingKey"], "3");
        catalog.add(serde_json::json!({"title":"keyless"}));
        assert!(catalog.take_changes().all, "a row without a key is found by a full pass");
    }
}
