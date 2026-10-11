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
    /// `children`'s rows by parent key, built on first use and dropped by every mutable
    /// path. Without it a listing scanned all rows once per show and per season: 9 s for
    /// a page of fun's 1,500 movies and over 30 s for its 13,500 episodes (2026-10-10).
    children_index: std::sync::OnceLock<HashMap<String, Vec<usize>>>,
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
            children_index: std::sync::OnceLock::new(),
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
        self.children_index.take();
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
        self.children_index.take();
        self.items.get_mut(index)
    }

    pub fn items(&self) -> &Vec<Value> {
        &self.items
    }

    /// Every row, mutably: a save then looks at them all.
    pub fn items_mut(&mut self) -> &mut Vec<Value> {
        self.changes.all = true;
        self.children_index.take();
        &mut self.items
    }

    pub fn take_changes(&mut self) -> Changes {
        std::mem::take(&mut self.changes)
    }

    pub fn children(&self, parent_key: &str) -> Vec<Value> {
        self.children_refs(parent_key).into_iter().cloned().collect()
    }

    /// `children` without the copies, for callers that only read or count.
    pub fn children_refs(&self, parent_key: &str) -> Vec<&Value> {
        let index = self.children_index.get_or_init(|| {
            let mut index: HashMap<String, Vec<usize>> = HashMap::new();
            for (position, item) in self.items.iter().enumerate() {
                let parent = match item.get("parentRatingKey") {
                    Some(Value::String(value)) => value.clone(),
                    Some(Value::Number(value)) => value.to_string(),
                    _ => continue,
                };
                index.entry(parent).or_default().push(position);
            }
            index
        });
        index
            .get(parent_key)
            .into_iter()
            .flatten()
            .filter_map(|position| self.items.get(*position))
            .collect()
    }

    /// Append a row. A row with a key is recorded by its key, so a save writes that one
    /// row (`store.rs` appends it) rather than looking at every row: a scan adds rows to a
    /// live library one release at a time.
    pub fn add(&mut self, item: Value) {
        self.children_index.take();
        let key = key_value(&item);
        self.items.push(item);
        match key {
            Some(key) => {
                self.by_id.insert(key.clone(), self.items.len() - 1);
                if !self.changes.all {
                    self.changes.keys.insert(key);
                }
            }
            None => self.changes.all = true,
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
    /// `childCount`), on fun's library as it stood on 2026-10-10: 361 shows, 698
    /// seasons, 13,509 episodes and 1,565 movies.
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
