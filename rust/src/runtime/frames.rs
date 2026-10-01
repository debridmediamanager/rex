//! Timeline frames, ported from `server.py`'s `notify_timeline` and `frame_title`,
//! and the per-library record `/library/sections/{id}/timeline` lists.

use super::*;

/// The numeric type a frame carries.  Cycle 22: a COLLECTION frames as type 18 and a
/// PLAYLIST as type 15 on every frame; the client builds the URL it updates from it.
pub(super) fn timeline_type(kind: &str) -> i64 {
    match kind {
        "movie" => 1,
        "show" => 2,
        "season" => 3,
        "episode" => 4,
        "playlist" => 15,
        "collection" => 18,
        _ => 1,
    }
}

impl Runtime {
    /// A TimelineEntry for one row, with the field set measured off a real refresh:
    /// identifier, sectionID and itemID as STRINGS, the numeric type, the title,
    /// `state` and `metadataState`.  `metadata_state=None` omits the field, which is
    /// what state 5 does.
    pub(super) fn push_timeline_row(
        &mut self,
        row: &Value,
        state: i64,
        metadata_state: Option<&str>,
        media_state: Option<&str>,
    ) {
        self.push_timeline_entry(row, state, metadata_state, media_state, None, None);
    }

    pub(super) fn push_timeline_entry(
        &mut self,
        row: &Value,
        state: i64,
        metadata_state: Option<&str>,
        media_state: Option<&str>,
        type_number: Option<i64>,
        title_override: Option<&str>,
    ) {
        let title = title_override
            .map(str::to_string)
            .unwrap_or_else(|| self.frame_title(row));
        let key = row.get("ratingKey").map(string_value).unwrap_or_default();
        let mut kind = row
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let mut section_id = row
            .get("librarySectionID")
            .map(string_value)
            .unwrap_or_default();
        if kind.is_empty() {
            if self.collections.contains_key(&key) {
                kind = "collection".to_string();
            } else if self.playlists.contains_key(&key) {
                kind = "playlist".to_string();
                section_id = "-1".to_string();
            }
        }
        let at = now();
        let mut entry = Map::new();
        entry.insert(
            "identifier".to_string(),
            json!("com.plexapp.plugins.library"),
        );
        entry.insert("sectionID".to_string(), json!(section_id));
        entry.insert("itemID".to_string(), json!(key));
        entry.insert(
            "type".to_string(),
            json!(type_number.unwrap_or_else(|| timeline_type(&kind))),
        );
        entry.insert("title".to_string(), json!(title));
        entry.insert("state".to_string(), json!(state));
        entry.insert("updatedAt".to_string(), json!(at));
        if let Some(metadata_state) = metadata_state {
            entry.insert("metadataState".to_string(), json!(metadata_state));
        }
        if let Some(media_state) = media_state.filter(|value| !value.is_empty()) {
            entry.insert("mediaState".to_string(), json!(media_state));
        }
        self.record_section_timeline(row, &key, state, at);
        self.push_event("timeline", json!({"TimelineEntry": Value::Object(entry)}));
    }

    /// `server.py` `notify_timeline`'s bookkeeping: what a library's /timeline lists --
    /// the last finished (5) or removed (9) state of each item, keyed by ratingKey, for
    /// every frame whose row names a numeric library, collections included.  A key
    /// seen before keeps its first position (a Python dict update), so equal stamps
    /// list in first-seen order.
    pub(super) fn record_section_timeline(&mut self, row: &Value, key: &str, state: i64, at: i64) {
        let section_key = row
            .get("librarySectionID")
            .map(string_value)
            .unwrap_or_default();
        if !(state == 5 || state == 9)
            || section_key.is_empty()
            || !section_key.bytes().all(|b| b.is_ascii_digit())
        {
            return;
        }
        let mut record = Map::new();
        record.insert("state".to_string(), json!(state));
        record.insert("at".to_string(), json!(at));
        if state == 9 {
            let mut stub = Map::new();
            stub.insert("ratingKey".to_string(), json!(key));
            stub.insert("key".to_string(), json!(format!("/library/metadata/{key}")));
            stub.insert("guid".to_string(), json!(""));
            stub.insert(
                "type".to_string(),
                row.get("subtype")
                    .filter(|value| truthy(value))
                    .or_else(|| row.get("type"))
                    .cloned()
                    .unwrap_or(Value::Null),
            );
            stub.insert("title".to_string(), json!(""));
            stub.insert("summary".to_string(), json!(""));
            stub.insert("state".to_string(), json!(state));
            stub.insert("metadataState".to_string(), json!("deleted"));
            if row.get("type").and_then(Value::as_str) == Some("show") {
                for count in ["childCount", "index", "leafCount", "viewedLeafCount"] {
                    let value = row
                        .get(count)
                        .filter(|value| truthy(value))
                        .cloned()
                        .unwrap_or(json!(0));
                    stub.insert(count.to_string(), value);
                }
            }
            record.insert("stub".to_string(), Value::Object(stub));
        }
        let records = self.section_timeline.entry(section_key).or_default();
        match records.iter_mut().find(|(existing, _)| existing == key) {
            Some(slot) => slot.1 = Value::Object(record),
            None => records.push((key.to_string(), Value::Object(record))),
        }
    }

    /// `/library/sections/{id}/timeline` -- what changed in a library lately.
    ///
    /// Measured on the reference: a secondary container with `latestEntryTime` and
    /// `updateQueueSize`, whose rows are the items the server recently finished
    /// (state 5: the row without its tags, plus `state`) and removed (state 9: a stub
    /// with an empty title and summary).
    pub(super) fn section_timeline_response(&self, section_key: &str, section: &Value) -> Response {
        let mut entries = self
            .section_timeline
            .get(section_key)
            .cloned()
            .unwrap_or_default();
        // Newest first; a stable sort keeps insertion order among equal stamps.
        entries.sort_by_key(|(_, record)| -record.get("at").and_then(Value::as_i64).unwrap_or(0));
        let mut rows = Vec::new();
        let mut latest = 0;
        for (key, record) in entries {
            let at = record.get("at").and_then(Value::as_i64).unwrap_or(0);
            latest = latest.max(at);
            let state = record.get("state").and_then(Value::as_i64).unwrap_or(0);
            if state == 5 {
                let Some(row) = self.catalog.get(&key).cloned() else {
                    continue;
                };
                let mut shown = self.grid_row(&row, false, false, false);
                if let Some(object) = shown.as_object_mut() {
                    for tag in [
                        "Genre",
                        "Country",
                        "Director",
                        "Writer",
                        "Role",
                        "Collection",
                    ] {
                        object.shift_remove(tag);
                    }
                    object.insert("state".to_string(), json!(state));
                }
                rows.push(shown);
            } else if let Some(stub) = record.get("stub") {
                rows.push(stub.clone());
            }
        }
        let show = section.get("type").and_then(Value::as_str) == Some("show");
        let mut body = Map::new();
        body.insert("size".to_string(), json!(rows.len()));
        body.insert("allowSync".to_string(), json!(false));
        body.insert(
            "art".to_string(),
            json!(resource_art(section.get("type").and_then(Value::as_str).unwrap_or("movie"))),
        );
        body.insert("content".to_string(), json!("secondary"));
        body.insert(
            "identifier".to_string(),
            json!("com.plexapp.plugins.library"),
        );
        body.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
        body.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
        body.insert(
            "thumb".to_string(),
            json!(resource_thumb(section.get("type").and_then(Value::as_str).unwrap_or("movie"))),
        );
        body.insert(
            "title1".to_string(),
            section.get("title").cloned().unwrap_or(Value::Null),
        );
        body.insert("viewGroup".to_string(), json!("secondary"));
        body.insert("viewMode".to_string(), json!(65592));
        if nocache_library(section.get("type").and_then(Value::as_str).unwrap_or("movie")) {
            body.insert("nocache".to_string(), json!(true));
        }
        body.insert("latestEntryTime".to_string(), json!(latest));
        body.insert("updateQueueSize".to_string(), json!(0));
        body.insert("Metadata".to_string(), Value::Array(rows));
        Response::json(200, json!({"MediaContainer": Value::Object(body)}))
    }
}
