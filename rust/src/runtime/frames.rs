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
    /// The string a row's frames -- and its activity's subtitle -- carry.
    ///
    /// Measured on a writable copy of the reference: a movie's and a show's frames
    /// title them with the year (`Elephants Dream (2006)`, `Firefly (2002)`, and an
    /// emptied title still gets it: ` (2010)`), while a SEASON's say `Firefly S01` and
    /// an EPISODE's `Firefly S01 E07` -- from the SHOW's title and the current indexes.
    pub(super) fn frame_title(&self, row: &Value) -> String {
        let title = row.get("title").map(string_value).unwrap_or_default();
        let kind = row.get("type").and_then(Value::as_str).unwrap_or_default();
        let year = row
            .get("year")
            .filter(|year| !year.is_null() && string_value(year) != "0")
            .map(string_value)
            .filter(|year| !year.is_empty());
        if matches!(kind, "movie" | "show") {
            if let Some(year) = year {
                return format!("{title} ({year})");
            }
            return title;
        }
        if matches!(kind, "season" | "episode") {
            let show_key = row
                .get(if kind == "season" {
                    "parentRatingKey"
                } else {
                    "grandparentRatingKey"
                })
                .map(string_value)
                .unwrap_or_default();
            let show_title = self
                .catalog
                .get(&show_key)
                .and_then(|show| show.get("title"))
                .map(string_value)
                .unwrap_or_default();
            let index = row
                .get(if kind == "season" { "index" } else { "parentIndex" })
                .and_then(|value| value.as_i64().or_else(|| string_value(value).parse().ok()))
                .unwrap_or(0);
            let mut out = format!("{show_title} S{index:02}");
            if kind == "episode" {
                let own = row
                    .get("index")
                    .and_then(|value| value.as_i64().or_else(|| string_value(value).parse().ok()))
                    .unwrap_or(0);
                out.push_str(&format!(" E{own:02}"));
            }
            return out;
        }
        title
    }

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
        entry.insert("identifier".to_string(), json!("com.plexapp.plugins.library"));
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
        let section_key = row
            .get("librarySectionID")
            .map(string_value)
            .unwrap_or_default();
        if (state == 5 || state == 9)
            && !section_key.is_empty()
            && section_key.bytes().all(|b| b.is_ascii_digit())
        {
            // What a library's /timeline lists: the last finished or removed state
            // of each item, keyed by ratingKey.
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
                            .filter(|value| !value.is_null())
                            .cloned()
                            .unwrap_or(json!(0));
                        stub.insert(count.to_string(), value);
                    }
                }
                record.insert("stub".to_string(), Value::Object(stub));
            }
            let records = self.section_timeline.entry(section_key).or_default();
            // Re-inserting moves the key to the end, which is insertion order.
            records.retain(|(existing, _)| *existing != key);
            records.push((key.clone(), Value::Object(record)));
        }
        self.push_event("timeline", json!({"TimelineEntry": Value::Object(entry)}));
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
                    for tag in ["Genre", "Country", "Director", "Writer", "Role", "Collection"] {
                        object.remove(tag);
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
            json!(if show {
                "/:/resources/show-fanart.jpg"
            } else {
                "/:/resources/movie-fanart.jpg"
            }),
        );
        body.insert("content".to_string(), json!("secondary"));
        body.insert("identifier".to_string(), json!("com.plexapp.plugins.library"));
        body.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
        body.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
        body.insert(
            "thumb".to_string(),
            json!(if show {
                "/:/resources/show.png"
            } else {
                "/:/resources/movie.png"
            }),
        );
        body.insert(
            "title1".to_string(),
            section.get("title").cloned().unwrap_or(Value::Null),
        );
        body.insert("viewGroup".to_string(), json!("secondary"));
        body.insert("viewMode".to_string(), json!(65592));
        if show {
            body.insert("nocache".to_string(), json!(true));
        }
        body.insert("latestEntryTime".to_string(), json!(latest));
        body.insert("updateQueueSize".to_string(), json!(0));
        body.insert("Metadata".to_string(), Value::Array(rows));
        Response::json(200, json!({"MediaContainer": Value::Object(body)}))
    }
}
