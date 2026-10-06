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
}

/// The rows changed since the last `take_changes`: by key, or all of them.
#[derive(Debug, Clone, Default)]
pub struct Changes {
    pub all: bool,
    pub keys: HashSet<String>,
}

impl Catalog {
    pub fn from_value(value: Value) -> Result<Self, String> {
        let document = value
            .as_object()
            .cloned()
            .ok_or_else(|| "catalog must be a JSON object".to_string())?;
        let sections = document
            .get("sections")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let items = document
            .get("items")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut catalog = Self {
            document,
            sections,
            items,
            by_id: HashMap::new(),
            changes: Changes { all: true, keys: HashSet::new() },
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
        self.by_id.clear();
        for (index, item) in self.items.iter().enumerate() {
            if let Some(key) = item.get("ratingKey").and_then(|v| match v {
                Value::String(v) => Some(v.clone()),
                Value::Number(v) => Some(v.to_string()),
                _ => None,
            }) {
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
        self.items.get_mut(index)
    }

    pub fn items(&self) -> &Vec<Value> {
        &self.items
    }

    /// Every row, mutably: a save then looks at them all.
    pub fn items_mut(&mut self) -> &mut Vec<Value> {
        self.changes.all = true;
        &mut self.items
    }

    pub fn take_changes(&mut self) -> Changes {
        std::mem::take(&mut self.changes)
    }

    pub fn children(&self, parent_key: &str) -> Vec<Value> {
        self.items
            .iter()
            .filter(|item| {
                item.get("parentRatingKey").and_then(|value| match value {
                    Value::String(value) => Some(value == parent_key),
                    Value::Number(value) => Some(value.to_string() == parent_key),
                    _ => None,
                }) == Some(true)
            })
            .cloned()
            .collect()
    }

    pub fn add(&mut self, item: Value) {
        self.changes.all = true;
        self.items.push(item);
        self.rebuild_index();
    }
    pub fn remove(&mut self, rating_key: &str) -> Option<Value> {
        let index = *self.by_id.get(rating_key)?;
        self.changes.all = true;
        let removed = self.items.remove(index);
        self.rebuild_index();
        Some(removed)
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

impl TryFrom<Value> for Catalog {
    type Error = String;
    fn try_from(value: Value) -> Result<Self, Self::Error> {
        Self::from_value(value)
    }
}

#[cfg(test)]
mod tests {
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
}
