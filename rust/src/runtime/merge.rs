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
            .items()
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
        self.catalog.remove(key);
        self.forget_row(key);
    }

    /// What retiring a row leaves behind besides the row itself: its key retired, and its
    /// playlist entries and collection memberships gone.
    pub(super) fn forget_row(&mut self, key: &str) {
        if let Ok(number) = key.parse::<i64>() {
            self.retired_keys.push(number);
        }
        self.deleted.insert(key.to_string());
        // Cycle 87, plexscratch (1.43.4, `tests/fixtures/merge_playlist_s43.json`): the
        // absorbed row's playlist ENTRY goes with it -- its playlistItemID is a 404 to a
        // DELETE and a move, and a `move?after=` it goes to the front like any unknown
        // `after`. We hid the entry at render and kept its slot addressable.
        for playlist in self.playlists.values_mut() {
            while let Some(index) = playlist.items.iter().position(|item| item == key) {
                playlist.items.remove(index);
                if index < playlist.item_ids.len() {
                    playlist.item_ids.remove(index);
                }
            }
        }
        for collection in self.collections.values_mut() {
            collection.items.retain(|item| item != key);
        }
    }

    /// `PUT /library/metadata/{id}/split`: one row per Media again, each new row on a
    /// fresh key carrying the source's metadata, its guid and locks included.
    pub(super) fn route_split(&mut self, key: &str) -> Response {
        let Some(original) = self.catalog.get(key).cloned() else {
            return Response::error(404, "simplex: not found");
        };
        let media = original
            .get("Media")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut created = Vec::new();
        if media.len() >= 2 {
            let kind = original
                .get("type")
                .and_then(Value::as_str)
                .unwrap_or("movie")
                .to_string();
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                // Neither the merge nor the split moved the source's `updatedAt` (or its
                // artwork version) on the reference.
                object.insert("Media".to_string(), json!([media[0].clone()]));
            }
            for source in &media[1..] {
                let new_key = self.next_row_key(&kind).to_string();
                let mut clone = original.clone();
                if let Some(object) = clone.as_object_mut() {
                    object.insert("Media".to_string(), json!([source.clone()]));
                    object.insert("ratingKey".to_string(), json!(new_key));
                    object.insert(
                        "key".to_string(),
                        json!(format!("/library/metadata/{new_key}")),
                    );
                    // Cycle 83, measured on plexscratch (merge 3 into 17, split 17): the
                    // new row is 17 in every field but `ratingKey`, `key` and the artwork
                    // URLs built from them -- the first item's title, `plex://` guid,
                    // whole `Guid` list and `Field` locks included -- holding the absorbed
                    // Media. A minted `local://<new key>` lost its identity in every
                    // `includeGuids` listing.
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
                object.shift_remove("year");
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
        info!("SPLIT {key} -> {} new row(s)", created.len());
        Response::empty(200)
    }
}
