//! Merge, split and match, ported from `library.py`'s `merge`/`split`/`apply_match`
//! and `server.py`'s `item_action`.

use super::*;

fn key_base(kind: &str) -> i64 {
    match kind {
        "show" => 20000,
        "season" => 30000,
        _ => 1000,
    }
}

impl Runtime {
    /// A split's new row takes a key NEVER used before (measured: 67 after 57), not
    /// the one a merge just freed: the highest used or retired key in the type's
    /// range, plus one.
    fn next_row_key(&self, kind: &str) -> i64 {
        let base = key_base(kind);
        let in_range = |key: i64| (base..base + 10000).contains(&key);
        self.catalog
            .items
            .iter()
            .filter_map(|row| key_of(row).and_then(|key| key.parse::<i64>().ok()))
            .chain(self.retired_keys.iter().copied())
            .filter(|key| in_range(*key))
            .max()
            .map_or(base + 1, |key| key + 1)
    }

    /// Remove a row and everything beneath it, retiring every key.
    pub(super) fn retire_row(&mut self, key: &str) {
        let children = self
            .children(key)
            .iter()
            .filter_map(key_of)
            .collect::<Vec<_>>();
        for child in children {
            self.retire_row(&child);
        }
        if let Ok(number) = key.parse::<i64>() {
            self.retired_keys.push(number);
        }
        self.catalog.remove(key);
        self.deleted.insert(key.to_string());
    }

    /// `library.merge`: one row, every Media.  Cycle 14: a target with no Media of
    /// its OWN (a season) absorbs nothing.
    fn merge_rows(&mut self, key: &str, others: &[String]) -> Vec<Value> {
        let Some(row) = self.catalog.get(key).cloned() else {
            return Vec::new();
        };
        if row.get("Media").and_then(Value::as_array).is_none_or(Vec::is_empty) {
            return Vec::new();
        }
        let mut media = row.get("Media").and_then(Value::as_array).cloned().unwrap_or_default();
        let mut merged = Vec::new();
        for other_key in others {
            if other_key == key {
                continue;
            }
            let Some(other) = self.catalog.get(other_key).cloned() else {
                continue;
            };
            media.extend(other.get("Media").and_then(Value::as_array).cloned().unwrap_or_default());
            self.retire_row(other_key);
            merged.push(other);
        }
        if !merged.is_empty() {
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.insert("Media".to_string(), Value::Array(media));
                object.insert("updatedAt".to_string(), json!(now()));
            }
        }
        merged
    }

    /// `PUT /library/metadata/{id}/merge?ids=`.
    pub(super) fn route_merge(&mut self, key: &str, row: &Value, query: &HashMap<String, String>) -> Response {
        // Measured: no `ids` is a 400, an unknown id a 404, a row of another type a 400.
        let ids = query
            .get("ids")
            .map(|ids| ids.split(',').filter(|id| !id.is_empty()).map(str::to_string).collect::<Vec<_>>())
            .unwrap_or_default();
        if ids.is_empty() {
            return Response::error(400, "plexsim: bad request");
        }
        let others = ids.iter().map(|id| self.catalog.get(id).cloned()).collect::<Vec<_>>();
        if others.iter().any(Option::is_none) {
            return Response::error(404, "plexsim: no such item");
        }
        if others.iter().flatten().any(|other| other.get("type") != row.get("type")) {
            return Response::error(400, "plexsim: bad request");
        }
        let merged = self.merge_rows(key, &ids);
        // Measured: the absorbed row is deleted (state 9, untitled) and the survivor
        // refreshed under type -1, state 3 then 5.
        for other in &merged {
            self.push_timeline_entry(other, 9, Some("deleted"), None, None, Some(""));
        }
        if !merged.is_empty() {
            if let Some(survivor) = self.catalog.get(key).cloned() {
                self.push_timeline_entry(&survivor, 3, None, None, Some(-1), None);
                self.push_timeline_entry(&survivor, 5, None, None, Some(-1), None);
            }
        }
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    /// `PUT /library/metadata/{id}/split`: one row per Media again, each new row on a
    /// fresh key, `local://` guid, no locks.
    pub(super) fn route_split(&mut self, key: &str) -> Response {
        let Some(original) = self.catalog.get(key).cloned() else {
            return Response::error(404, "plexsim: not found");
        };
        let media = original.get("Media").and_then(Value::as_array).cloned().unwrap_or_default();
        let mut created = Vec::new();
        if media.len() >= 2 {
            let kind = original.get("type").and_then(Value::as_str).unwrap_or("movie").to_string();
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.insert("Media".to_string(), json!([media[0].clone()]));
                object.insert("updatedAt".to_string(), json!(now()));
            }
            for source in &media[1..] {
                let new_key = self.next_row_key(&kind).to_string();
                let mut clone = original.clone();
                if let Some(object) = clone.as_object_mut() {
                    object.insert("Media".to_string(), json!([source.clone()]));
                    object.remove("Field");
                    object.insert("ratingKey".to_string(), json!(new_key));
                    object.insert("key".to_string(), json!(format!("/library/metadata/{new_key}")));
                    object.insert("guid".to_string(), json!(format!("local://{new_key}")));
                }
                self.catalog.add(clone.clone());
                self.deleted.remove(&new_key);
                created.push(clone);
            }
        }
        // Measured: each new row is created -- state 0 untitled, state 0 titled, state
        // 1 -- and the source then refreshed, state 3 then 5, with no metadataState.
        for clone in &created {
            let mut untitled = clone.clone();
            if let Some(object) = untitled.as_object_mut() {
                object.insert("title".to_string(), json!(""));
                object.remove("year");
            }
            self.push_timeline_entry(&untitled, 0, Some("created"), None, None, None);
            self.push_timeline_entry(clone, 0, Some("created"), None, None, None);
            self.push_timeline_entry(clone, 1, Some("created"), None, None, None);
        }
        if !created.is_empty() {
            if let Some(source) = self.catalog.get(key).cloned() {
                self.push_timeline_entry(&source, 3, None, None, None, None);
                self.push_timeline_entry(&source, 5, None, None, None, None);
            }
        }
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    /// `PUT /library/metadata/{id}/match?guid=&name=&year=`.
    pub(super) fn route_match(&mut self, key: &str, row: &Value, query: &HashMap<String, String>) -> Response {
        // Measured: the NAME is what this endpoint requires -- a guid with no name is a
        // 400, and an empty guid with a name is a 200 that renames and refreshes.
        let Some(name) = query.get("name").cloned() else {
            return Response::error(400, "plexsim: match needs a name");
        };
        let guid = query.get("guid").cloned().unwrap_or_default();
        let section = row.get("librarySectionID").map(string_value).unwrap_or_default();
        // Matching to a guid another row in the same library already carries MERGES
        // this row into that one.
        let existing = if guid.is_empty() {
            None
        } else {
            self.catalog
                .items
                .iter()
                .find(|other| {
                    key_of(other).as_deref() != Some(key)
                        && other.get("guid").map(string_value).as_deref() == Some(guid.as_str())
                        && other.get("librarySectionID").map(string_value).unwrap_or_default() == section
                })
                .cloned()
        };
        self.push_timeline_entry(row, 3, None, None, None, None);
        if let Some(existing) = existing {
            let survivor = key_of(&existing).unwrap_or_default();
            let merged = self.merge_rows(&survivor, &[key.to_string()]);
            for other in &merged {
                let mut untitled = other.clone();
                if let Some(object) = untitled.as_object_mut() {
                    object.insert("title".to_string(), json!(""));
                    object.remove("year");
                }
                self.push_timeline_entry(&untitled, 9, Some("deleted"), None, None, None);
            }
            if let Some(current) = self.catalog.get(&survivor).cloned() {
                self.push_timeline_entry(&current, 3, None, None, Some(-1), None);
                self.push_timeline_entry(&current, 5, None, None, Some(-1), None);
            }
            self.notify_refreshed(&survivor);
        } else {
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                if !guid.is_empty() {
                    object.insert("guid".to_string(), json!(guid));
                }
                if !name.is_empty() {
                    object.insert("title".to_string(), json!(name));
                }
                if let Some(year) = query.get("year").and_then(|year| year.parse::<i64>().ok()) {
                    object.insert("year".to_string(), json!(year));
                }
                object.insert("updatedAt".to_string(), json!(now()));
            }
            // Cycle 59: a rename here reaches the children as an edit's does.
            if let Some(current) = self.catalog.get(key).cloned() {
                let written = ["title".to_string(), "studio".to_string()]
                    .into_iter()
                    .collect::<HashSet<_>>();
                self.follow_parent(&current, &written);
            }
            self.notify_refreshed(key);
        }
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }
}
