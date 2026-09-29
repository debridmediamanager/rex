//! Item artwork: picks, uploads, clears and the cascade that re-derives every
//! borrowed field.  Ported from `server.py`'s `set_artwork`, `upload_artwork`,
//! `clear_artwork`, `after_artwork_edit`, `cascade_artwork` and `artwork_list`,
//! whose docstrings hold the measurements (cycles 14-31, 59, 61).

use super::*;

/// Whether this row's copy of `field` is its OWN rather than one it borrows: a
/// borrower carries the owner's path verbatim, so the stored string names the owner.
pub(super) fn owns_field(row: Option<&Value>, field: &str) -> bool {
    let Some(row) = row else {
        return false;
    };
    let key = row.get("ratingKey").map(string_value).unwrap_or_default();
    row.get(field)
        .and_then(Value::as_str)
        .is_some_and(|value| value.starts_with(&format!("/library/metadata/{key}/")))
}

/// Does this row OWN the image behind `/library/metadata/{id}/{leaf}`? (cycle 16)
pub(super) fn owns_artwork(row: &Value, leaf: &str) -> bool {
    let stored = row
        .get(if leaf == "art" { "art" } else { "thumb" })
        .and_then(Value::as_str)
        .unwrap_or("");
    if stored.is_empty() {
        return false;
    }
    let parts = stored.split('/').collect::<Vec<_>>();
    if parts.len() > 3 && parts[1] == "library" && parts[2] == "metadata" {
        return parts[3] == row.get("ratingKey").map(string_value).unwrap_or_default();
    }
    true
}

/// Which ancestor's copy of `field` a row under `season` carries: the NEAREST
/// ancestor that OWNS one (cycle 29).
fn handed_down_from(field: &str, season: Option<&Value>, show: Option<&Value>) -> Option<Value> {
    if owns_field(season, field) {
        season.cloned()
    } else {
        show.cloned()
    }
}

/// Give a row the ancestor's copy of a handed-down field, or take it away.
fn hand_down(child: &mut Map<String, Value>, field: &str, owner: Option<&Value>) {
    match owner.and_then(|owner| owner.get(field)).filter(|value| !value.is_null()) {
        Some(value) => {
            child.insert(field.to_string(), value.clone());
        }
        None => {
            child.remove(field);
        }
    }
}

/// Re-derive one borrowed field from the row it is borrowed from; only a child whose
/// copy names `owner` follows.
fn follow_artwork(child: &mut Map<String, Value>, field: &str, owner: &Value) {
    let owner_key = owner.get("ratingKey").map(string_value).unwrap_or_default();
    let borrows = child
        .get(field)
        .and_then(Value::as_str)
        .is_some_and(|value| value.starts_with(&format!("/library/metadata/{owner_key}/")));
    if !borrows {
        return;
    }
    hand_down(child, field, Some(owner));
}

/// The `key`/`thumb` a real PMS gives a local-uri artwork candidate: `/library/
/// metadata/{id}/file?url=<uri>`, percent-encoded with `.` escaped as well.
pub(super) fn candidate_path(rating_key: &str, uri: &str) -> String {
    let mut encoded = String::new();
    for byte in uri.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'~') {
            encoded.push(byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    format!("/library/metadata/{rating_key}/file?url={encoded}")
}

/// The content type of an uploaded image, from its magic bytes.
pub(super) fn image_type(body: &[u8]) -> &'static str {
    if body.starts_with(b"\x89PNG\r\n\x1a\n") {
        "image/png"
    } else if body.starts_with(b"\xff\xd8\xff") {
        "image/jpeg"
    } else if body.len() >= 12 && &body[..4] == b"RIFF" && &body[8..12] == b"WEBP" {
        "image/webp"
    } else {
        "application/octet-stream"
    }
}

fn sha1_hex(body: &[u8]) -> String {
    let mut hasher = Sha1::new();
    hasher.update(body);
    hasher.finalize().iter().map(|byte| format!("{byte:02x}")).collect()
}

impl Runtime {
    fn parent_of(&self, row: &Value, field: &str) -> Option<Value> {
        row.get(field)
            .map(string_value)
            .and_then(|key| self.catalog.get(&key).cloned())
    }

    fn store_row(&mut self, key: &str, row: Value) {
        if let Some(slot) = self.catalog.get_mut(key) {
            *slot = row;
        }
    }

    /// Re-derive every borrowed artwork field under a row whose artwork changed
    /// (cycles 25, 27, 29, 61).
    pub(super) fn cascade_artwork_from(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let kind = row.get("type").and_then(Value::as_str).unwrap_or("").to_string();
        if kind != "show" && kind != "season" {
            return;
        }
        let show = if kind == "show" {
            Some(row.clone())
        } else {
            self.parent_of(&row, "parentRatingKey")
        };
        for child in self.children(key) {
            let child_key = key_of(&child).unwrap_or_default();
            let mut child = child;
            if let Some(object) = child.as_object_mut() {
                if kind == "show" {
                    hand_down(object, "parentThumb", None);
                    if let Some(thumb) = row.get("thumb").filter(|v| !v.is_null()) {
                        object.insert("parentThumb".to_string(), thumb.clone());
                    }
                    let snapshot = Value::Object(object.clone());
                    let source = handed_down_from("art", Some(&snapshot), Some(&row));
                    hand_down(object, "art", source.as_ref());
                    object.remove("theme");
                    object.remove("parentTheme");
                    if let Some(theme) = row.get("theme").filter(|v| !v.is_null()) {
                        object.insert("parentTheme".to_string(), theme.clone());
                    }
                } else {
                    if owns_field(Some(&row), "thumb") {
                        object.insert("parentThumb".to_string(), row["thumb"].clone());
                    } else {
                        object.remove("parentThumb");
                    }
                    let source = handed_down_from("art", Some(&row), show.as_ref());
                    hand_down(object, "art", source.as_ref());
                }
                follow_artwork(object, "thumb", &row);
            }
            self.store_row(&child_key, child.clone());
            if kind == "show" {
                for leaf in self.leaves(&child_key) {
                    let leaf_key = key_of(&leaf).unwrap_or_default();
                    let mut leaf = leaf;
                    if let Some(object) = leaf.as_object_mut() {
                        for name in ["thumb", "art", "theme"] {
                            let target = format!("grandparent{}", capitalize_ascii(name));
                            match row.get(name).filter(|v| !v.is_null()) {
                                Some(value) => {
                                    object.insert(target, value.clone());
                                }
                                None => {
                                    object.remove(&target);
                                }
                            }
                        }
                        let source = handed_down_from("art", Some(&child), Some(&row));
                        hand_down(object, "art", source.as_ref());
                    }
                    self.store_row(&leaf_key, leaf);
                }
            }
        }
    }

    /// `POST /library/metadata/{id}/{posters,arts,themes,clearLogos}` with the image as
    /// the body (cycles 15, 17, 29, 59, 61).
    pub(super) fn upload_artwork(&mut self, key: &str, kind: &str, body: &[u8]) -> Response {
        if body.is_empty() {
            return Response::error(400, "plexsim: bad request");
        }
        let Some(mut row) = self.catalog.get(key).cloned() else {
            return Response::error(404, "plexsim: not found");
        };
        let (field, mut prefix, served) = match kind {
            "posters" => ("thumb", "upload://posters".to_string(), None),
            "themes" => ("theme", "upload://themes".to_string(), Some("audio/mpeg3")),
            "clearLogos" => ("clearLogo", "upload://clearLogos".to_string(), Some("image/jpeg")),
            _ => ("art", "upload://art".to_string(), None),
        };
        match row.get("type").and_then(Value::as_str) {
            Some("season") => {
                prefix += &format!("/seasons/{}", row.get("index").map(string_value).unwrap_or_default());
            }
            Some("episode") => {
                prefix += &format!(
                    "/seasons/{}/episodes/{}",
                    row.get("parentIndex").map(string_value).unwrap_or_default(),
                    row.get("index").map(string_value).unwrap_or_default()
                );
            }
            _ => {}
        }
        let upload = format!("{prefix}/{}", sha1_hex(body));
        self.uploads.insert(
            upload.clone(),
            (body.to_vec(), served.unwrap_or_else(|| image_type(body)).to_string()),
        );
        let stamp = now();
        let rating_key = key.to_string();
        let Some(object) = row.as_object_mut() else {
            return Response::error(500, "plexsim: internal error");
        };
        {
            let uploads = object
                .entry("_uploads".to_string())
                .or_insert_with(|| json!({}));
            if let Some(uploads) = uploads.as_object_mut() {
                let mine = uploads.entry(field.to_string()).or_insert_with(|| json!([]));
                if let Some(mine) = mine.as_array_mut() {
                    if !mine.iter().any(|value| value.as_str() == Some(upload.as_str())) {
                        mine.push(json!(upload));
                    }
                }
            }
        }
        let private = format!("_{}_url", if field == "thumb" { "poster" } else { field });
        let agent = private.replace("_url", "_agent");
        if let Some(previous) = object.get(&private).and_then(Value::as_str).map(str::to_string) {
            if !previous.starts_with("upload://") && !object.contains_key(&agent) {
                object.insert(agent, json!(previous));
            }
        }
        object.insert(private, json!(upload));
        let snapshot = Value::Object(object.clone());
        for name in ["thumb", "art", "theme"] {
            let present = object.get(name).is_some_and(|value| !value.is_null() && value != "");
            if present && (name == field || owns_field(Some(&snapshot), name)) {
                object.insert(name.to_string(), json!(format!("/library/metadata/{rating_key}/{name}/{stamp}")));
            }
        }
        if field == "clearLogo" || object.contains_key("_clearLogo") {
            object.insert(
                "_clearLogo".to_string(),
                json!(format!("/library/metadata/{rating_key}/clearLogo/{stamp}")),
            );
        }
        if field != "clearLogo" && !object.contains_key(field) {
            object.insert(field.to_string(), json!(format!("/library/metadata/{rating_key}/{field}/{stamp}")));
        }
        object.insert("updatedAt".to_string(), json!(stamp));
        self.store_row(key, row);
        self.cascade_artwork_from(key);
        self.push_timeline(key, 5, None);
        self.dirty = true;
        Response::empty(200)
    }

    /// `PUT /library/metadata/{id}/{poster,art}?url=` -- pick one (cycles 14, 27).  The
    /// response body is the row's new versioned path.
    pub(super) fn set_artwork(&mut self, key: &str, kind: &str, query: &HashMap<String, String>) -> Response {
        let chosen = query.get("url").map(|value| percent_decode(value)).unwrap_or_default();
        if chosen.is_empty() {
            return Response::error(400, "plexsim: no url");
        }
        let Some(mut row) = self.catalog.get(key).cloned() else {
            return Response::error(404, "plexsim: not found");
        };
        let field = if kind == "posters" { "thumb" } else { "art" };
        let private = if kind == "posters" { "_poster_url" } else { "_art_url" };
        let placeholder = format!("/library/metadata/{key}/{field}/1");
        let stamp = now();
        let Some(object) = row.as_object_mut() else {
            return Response::error(500, "plexsim: internal error");
        };
        if chosen == placeholder {
            object.remove(private);
        } else {
            object.insert(private.to_string(), json!(chosen));
        }
        for name in ["thumb", "art", "theme"] {
            let value = object.get(name).and_then(Value::as_str).unwrap_or("").to_string();
            let owned = value.starts_with(&format!("/library/metadata/{key}/"));
            if !value.is_empty() && (name == field || owned) {
                object.insert(name.to_string(), json!(format!("/library/metadata/{key}/{name}/{stamp}")));
            }
        }
        if !object.contains_key(field) || object.get(field).is_some_and(Value::is_null) {
            object.insert(field.to_string(), json!(format!("/library/metadata/{key}/{field}/{stamp}")));
        }
        object.insert("updatedAt".to_string(), json!(stamp));
        let mut fields = object
            .get("Field")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        fields.retain(|entry| entry.get("name").and_then(Value::as_str) != Some(field));
        fields.push(json!({"locked": true, "name": field}));
        object.insert("Field".to_string(), Value::Array(fields));
        let body = object.get(field).and_then(Value::as_str).unwrap_or("").to_string();
        self.store_row(key, row);
        self.cascade_artwork_from(key);
        self.push_timeline(key, 5, None);
        self.dirty = true;
        Response::plain(200, body.into_bytes())
    }

    /// The fallback and the cascade an EMPTY `thumb.value=`/`art.value=` owes (cycle 31):
    /// the row falls back to its nearest ancestor that OWNS one and every borrower below
    /// is re-derived.
    pub(super) fn after_artwork_edit(&mut self, key: &str, query: &HashMap<String, String>) {
        for field in ["thumb", "art"] {
            let name = format!("{field}.value");
            let cleared = query.get(&name).is_some_and(|v| v.is_empty())
                || query.get(field).is_some_and(|v| v.is_empty());
            if !cleared {
                continue;
            }
            let Some(mut row) = self.catalog.get(key).cloned() else {
                return;
            };
            if row.get(field).is_some_and(|value| !value.is_null() && value != "") {
                continue;
            }
            let kind = row.get("type").and_then(Value::as_str).unwrap_or("").to_string();
            let season = self.parent_of(&row, "parentRatingKey");
            let show = self.parent_of(&row, "grandparentRatingKey");
            if let Some(object) = row.as_object_mut() {
                object.remove(if field == "thumb" { "_poster_url" } else { "_art_url" });
                if field == "thumb" && object.get("parentThumb").is_some_and(|v| !v.is_null()) {
                    let parent = object["parentThumb"].clone();
                    object.insert(field.to_string(), parent);
                } else if field == "art" && (kind == "season" || kind == "episode") {
                    let source = if kind == "episode" {
                        handed_down_from(field, season.as_ref(), show.as_ref())
                    } else {
                        handed_down_from(field, None, season.as_ref())
                    };
                    if let Some(value) = source.as_ref().and_then(|s| s.get(field)).filter(|v| !v.is_null()) {
                        object.insert(field.to_string(), value.clone());
                    }
                }
            }
            self.store_row(key, row);
            self.cascade_artwork_from(key);
        }
    }

    /// `/library/metadata/{id}/{posters,arts}` -- the Change Artwork picker.
    pub(super) fn artwork_list(&self, row: &Value, kind: &str) -> Response {
        // Measured: an artwork list renders as <Photo> in XML while its JSON still
        // says `Metadata` (the private `_element` never reaches the JSON writer).
        let container = |mut entries: Vec<Value>| {
            for entry in &mut entries {
                if let Some(object) = entry.as_object_mut() {
                    object.insert("_element".to_string(), json!("Photo"));
                }
            }
            Response::json(
                200,
                json!({"MediaContainer": {
                    "size": entries.len(),
                    "identifier": "com.plexapp.plugins.library",
                    "mediaTagPrefix": MEDIA_TAG_PREFIX,
                    "mediaTagVersion": MEDIA_TAG_VERSION,
                    "Metadata": entries,
                }}),
            )
        };
        let kind_type = row.get("type").and_then(Value::as_str).unwrap_or("");
        if kind == "arts" && (kind_type == "season" || kind_type == "episode") {
            return container(Vec::new());
        }
        let rating_key = row.get("ratingKey").map(string_value).unwrap_or_default();
        let field = if kind == "posters" { "thumb" } else { "art" };
        let mut stored = row
            .get(if kind == "posters" { "_poster_url" } else { "_art_url" })
            .and_then(Value::as_str)
            .map(str::to_string);
        let uploads = row
            .get("_uploads")
            .and_then(|uploads| uploads.get(field))
            .and_then(Value::as_array)
            .map(|list| list.iter().filter_map(Value::as_str).map(str::to_string).collect::<Vec<_>>())
            .unwrap_or_default();
        let mut entries = uploads
            .iter()
            .map(|upload| {
                json!({"key": candidate_path(&rating_key, upload), "ratingKey": upload,
                       "thumb": candidate_path(&rating_key, upload),
                       "selected": stored.as_deref() == Some(upload.as_str())})
            })
            .collect::<Vec<_>>();
        let agent = row
            .get(if kind == "posters" { "_poster_agent" } else { "_art_agent" })
            .and_then(Value::as_str)
            .map(str::to_string);
        let chose_upload = stored.as_ref().is_some_and(|value| uploads.contains(value));
        let picked = stored.clone();
        if chose_upload || stored.is_none() {
            stored = agent;
        }
        let current = row.get(field).is_some_and(|value| !value.is_null() && value != "");
        let chose_remote = stored.is_some() && picked == stored;
        let placeholder = format!("/library/metadata/{rating_key}/{field}/1");
        if let Some(stored) = stored {
            entries.insert(
                0,
                json!({"key": stored, "ratingKey": stored, "thumb": stored,
                       "selected": chose_remote, "provider": "tmdb"}),
            );
        }
        entries.push(json!({"key": placeholder, "ratingKey": placeholder, "thumb": placeholder,
            "selected": !chose_remote && !chose_upload && current, "provider": "local"}));
        container(entries)
    }
}
