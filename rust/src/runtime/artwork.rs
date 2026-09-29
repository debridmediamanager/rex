//! Change Artwork: the candidate lists, a pick, an upload, and the cascade an
//! artwork write owes the rows that borrow it. Ported from `server.py`'s
//! `artwork_list`, `set_artwork`, `upload_artwork`, `after_artwork_edit` and
//! `cascade_artwork`, whose docstrings hold the measurements.

use super::*;

/// The `key`/`thumb` a real PMS gives an artwork candidate that is a local uri.
///
/// Measured on kids and zen: `/library/metadata/{id}/file?url=<uri>`, where the uri is
/// percent-encoded with `.` escaped as well.
pub(super) fn candidate_path(rating_key: &str, uri: &str) -> String {
    let mut encoded = String::new();
    for byte in uri.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'~') {
            encoded.push(byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    format!("/library/metadata/{rating_key}/file?url={encoded}")
}

/// The content type of an uploaded image, from its magic bytes.
fn image_type(body: &[u8]) -> &'static str {
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

/// Whether this row's copy of `field` is its OWN rather than one it borrows: a
/// borrowing row carries the OWNER's path verbatim, so the stored string's ratingKey
/// is the whole test.
pub(super) fn owns_field(row: Option<&Value>, field: &str) -> bool {
    let Some(row) = row else { return false };
    let key = row.get("ratingKey").map(string_value).unwrap_or_default();
    row.get(field)
        .map(string_value)
        .is_some_and(|value| value.starts_with(&format!("/library/metadata/{key}/")))
}

/// Does this row OWN the image behind `/library/metadata/{id}/{leaf}`? Measured cycle
/// 16 on kids and zen: only the owner answers 200. A row with no artwork owns nothing,
/// and a path that is not a library path (a remote url) is the row's own.
pub(super) fn owns_artwork(row: &Value, leaf: &str) -> bool {
    let stored = row
        .get(if leaf == "art" { "art" } else { "thumb" })
        .map(string_value)
        .unwrap_or_default();
    if stored.is_empty() {
        return false;
    }
    let parts = stored.split('/').collect::<Vec<_>>();
    if parts.len() > 3 && parts[1] == "library" && parts[2] == "metadata" {
        return parts[3] == row.get("ratingKey").map(string_value).unwrap_or_default();
    }
    true
}

impl Runtime {
    fn artwork_parent(&self, row: &Value, field: &str) -> Option<Value> {
        let key = row.get(field).map(string_value)?;
        self.catalog.get(&key).cloned()
    }

    /// `/library/metadata/{id}/{posters,arts}` -- the Change Artwork picker.
    ///
    /// A real PMS lists every candidate the agent found, each with `selected` on the
    /// current one; plexsim has what TMDB gave it, the uploads, and its own generated
    /// placeholder. Measured on plexscratch after an upload: the agent's row first,
    /// then the uploads, then the local one.
    pub(super) fn artwork_candidates(&self, row: &Value, kind: &str) -> Response {
        let rating_key = row.get("ratingKey").map(string_value).unwrap_or_default();
        if kind == "arts"
            && matches!(
                row.get("type").and_then(Value::as_str),
                Some("season" | "episode")
            )
        {
            // Measured: a season's and an episode's background candidates are their
            // OWN -- never the show's.
            return Response::json(200, self.tag_container(Map::new()));
        }
        let field = if kind == "posters" { "thumb" } else { "art" };
        let private = if kind == "posters" { "_poster_url" } else { "_art_url" };
        let agent_name = if kind == "posters" { "_poster_agent" } else { "_art_agent" };
        let mut stored = row
            .get(private)
            .filter(|value| truthy(value))
            .map(string_value);
        let uploads = row
            .get("_uploads")
            .and_then(|uploads| uploads.get(field))
            .and_then(Value::as_array)
            .map(|items| items.iter().map(string_value).collect::<Vec<_>>())
            .unwrap_or_default();
        let mut entries = Vec::new();
        for upload in &uploads {
            let path = candidate_path(&rating_key, upload);
            entries.push(json!({
                "_element": "Photo",
                "key": path,
                "ratingKey": upload,
                "thumb": path,
                "selected": stored.as_deref() == Some(upload.as_str()),
            }));
        }
        let agent = row.get(agent_name).filter(|value| truthy(value)).map(string_value);
        let chose_upload = stored.as_ref().is_some_and(|value| uploads.contains(value));
        let picked = stored.clone();
        if chose_upload || stored.is_none() {
            stored = agent;
        }
        let current = row.get(field).is_some_and(truthy);
        // `selected` is whichever candidate the row's artwork actually POINTS AT.
        let chose_remote = stored.is_some() && picked == stored;
        let placeholder = format!("/library/metadata/{rating_key}/{field}/1");
        if let Some(stored) = &stored {
            entries.insert(
                0,
                json!({
                    "_element": "Photo",
                    "key": stored,
                    "ratingKey": stored,
                    "thumb": stored,
                    "selected": chose_remote,
                    "provider": "tmdb",
                }),
            );
        }
        entries.push(json!({
            "_element": "Photo",
            "key": placeholder,
            "ratingKey": placeholder,
            "thumb": placeholder,
            "selected": !chose_remote && !chose_upload && current,
            "provider": "local",
        }));
        Response::json(
            200,
            self.tag_container(Map::from_iter([
                ("size".to_string(), json!(entries.len())),
                ("Metadata".to_string(), Value::Array(entries)),
            ])),
        )
    }

    /// `PUT /library/metadata/{id}/{poster,art}?url=` -- pick one.
    ///
    /// Measured on a writable copy: whatever is picked the row's artwork becomes a NEW
    /// versioned path of its own and the response body is that path. Cycle 14: a pick
    /// re-versions `thumb`, `art` AND `theme` onto ONE new stamp -- only the ones the
    /// row OWNS (cycle 27), the picked field exempt -- moves `updatedAt`, LOCKS the
    /// picked field, cascades, and pushes exactly one `state 5` frame.
    pub(super) fn set_artwork(
        &mut self,
        key: &str,
        kind: &str,
        query: &HashMap<String, String>,
    ) -> Response {
        let chosen = percent_decode(query.get("url").map(String::as_str).unwrap_or(""));
        if chosen.is_empty() {
            return Response::error(400, "plexsim: no url");
        }
        let field = if kind == "posters" { "thumb" } else { "art" };
        let private = if kind == "posters" { "_poster_url" } else { "_art_url" };
        let placeholder = format!("/library/metadata/{key}/{field}/1");
        let stamp = now();
        let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) else {
            return Response::error(404, "plexsim: not found");
        };
        if chosen == placeholder {
            object.remove(private);
        } else {
            object.insert(private.to_string(), json!(chosen));
        }
        let own_prefix = format!("/library/metadata/{key}/");
        for name in ["thumb", "art", "theme"] {
            let value = object.get(name).filter(|value| truthy(value)).map(string_value);
            if let Some(value) = value {
                if name == field || value.starts_with(&own_prefix) {
                    object.insert(
                        name.to_string(),
                        json!(format!("/library/metadata/{key}/{name}/{stamp}")),
                    );
                }
            }
        }
        object
            .entry(field.to_string())
            .or_insert_with(|| json!(format!("/library/metadata/{key}/{field}/{stamp}")));
        object.insert("updatedAt".to_string(), json!(stamp));
        let mut fields = object
            .get("Field")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        fields.retain(|entry| entry.get("name").and_then(Value::as_str) != Some(field));
        fields.push(json!({"locked": true, "name": field}));
        object.insert("Field".to_string(), Value::Array(fields));
        let answer = object.get(field).map(string_value).unwrap_or_default();
        self.cascade_artwork_rows(key);
        if let Some(row) = self.catalog.get(key).cloned() {
            self.notify_timeline(&row, 5, None);
        }
        self.dirty = true;
        self.save_if_dirty();
        // Measured: the body is the NEW artwork path, bare, because the client assigns
        // the response straight onto the model's `thumb`.
        let mut response = Response::bytes(200, "text/html");
        response.body = answer.into_bytes();
        response
    }

    /// `POST /library/metadata/{id}/{posters,arts,themes,clearLogos}` with the image as
    /// the body. Measured on a writable copy: an EMPTY 200, the image becomes an
    /// `upload://posters/<sha1 of the bytes>` candidate of this row alone (a season's and
    /// an episode's carry their place in the path) and is selected at once; only the
    /// field the upload WROTE becomes the row's own (cycle 29), everything it owns
    /// re-versions onto one stamp with `updatedAt`, the write cascades, and one
    /// `state 5` frame is pushed.
    pub(super) fn upload_artwork(&mut self, key: &str, kind: &str, body: &[u8]) -> Response {
        if body.is_empty() {
            return Response::error(400, "plexsim: bad request");
        }
        let Some(row) = self.catalog.get(key).cloned() else {
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
                prefix.push_str(&format!(
                    "/seasons/{}",
                    row.get("index").map(py_str).unwrap_or_else(|| "None".to_string())
                ));
            }
            Some("episode") => {
                prefix.push_str(&format!(
                    "/seasons/{}/episodes/{}",
                    row.get("parentIndex").map(py_str).unwrap_or_else(|| "None".to_string()),
                    row.get("index").map(py_str).unwrap_or_else(|| "None".to_string())
                ));
            }
            _ => {}
        }
        let upload_key = format!("{prefix}/{}", bytes_digest(body));
        self.uploads.insert(
            upload_key.clone(),
            (body.to_vec(), served.unwrap_or_else(|| image_type(body)).to_string()),
        );
        let private = match field {
            "thumb" => "_poster_url".to_string(),
            other => format!("_{other}_url"),
        };
        let agent = private.replace("_url", "_agent");
        let stamp = now();
        let own_prefix = format!("/library/metadata/{key}/");
        let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) else {
            return Response::error(404, "plexsim: not found");
        };
        {
            let uploads = object
                .entry("_uploads".to_string())
                .or_insert_with(|| json!({}));
            if let Some(uploads) = uploads.as_object_mut() {
                let mine = uploads
                    .entry(field.to_string())
                    .or_insert_with(|| json!([]));
                if let Some(mine) = mine.as_array_mut() {
                    if !mine.iter().any(|value| value.as_str() == Some(upload_key.as_str())) {
                        mine.push(json!(upload_key));
                    }
                }
            }
        }
        // Cycle 59: the agent's candidate stays in the picker, unselected.
        if let Some(previous) = object.get(&private).filter(|value| truthy(value)).map(string_value) {
            if !previous.starts_with("upload://") && !object.contains_key(&agent) {
                object.insert(agent.clone(), json!(previous));
            }
        }
        object.insert(private.clone(), json!(upload_key));
        for name in ["thumb", "art", "theme"] {
            let value = object.get(name).filter(|value| truthy(value)).map(string_value);
            if let Some(value) = value {
                if name == field || value.starts_with(&own_prefix) {
                    object.insert(
                        name.to_string(),
                        json!(format!("/library/metadata/{key}/{name}/{stamp}")),
                    );
                }
            }
        }
        if field == "clearLogo" || object.get("_clearLogo").is_some_and(truthy) {
            object.insert(
                "_clearLogo".to_string(),
                json!(format!("/library/metadata/{key}/clearLogo/{stamp}")),
            );
        }
        if field != "clearLogo" {
            object
                .entry(field.to_string())
                .or_insert_with(|| json!(format!("/library/metadata/{key}/{field}/{stamp}")));
        }
        object.insert("updatedAt".to_string(), json!(stamp));
        self.cascade_artwork_rows(key);
        if let Some(row) = self.catalog.get(key).cloned() {
            self.notify_timeline(&row, 5, None);
        }
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    /// Cycle 31: an EMPTY `thumb.value=` or `art.value=` removes the column, and the
    /// row then falls back to its nearest ancestor that OWNS one, every borrower below
    /// re-derived -- everything the 1.43.4-only DELETE does after dropping the field.
    pub(super) fn after_artwork_edit(&mut self, key: &str, query: &HashMap<String, String>) {
        for field in ["thumb", "art"] {
            let cleared = query.get(&format!("{field}.value")).is_some_and(String::is_empty)
                || query.get(field).is_some_and(String::is_empty);
            if !cleared {
                continue;
            }
            let Some(row) = self.catalog.get(key).cloned() else {
                return;
            };
            if row.get(field).is_some_and(truthy) {
                continue;
            }
            let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
            let source = if field == "art" && (kind == "season" || kind == "episode") {
                let owner = if kind == "episode" {
                    let season = self.artwork_parent(&row, "parentRatingKey");
                    let show = self.artwork_parent(&row, "grandparentRatingKey");
                    if owns_field(season.as_ref(), field) { season } else { show }
                } else {
                    self.artwork_parent(&row, "parentRatingKey")
                };
                owner.and_then(|owner| owner.get(field).filter(|value| truthy(value)).cloned())
            } else {
                None
            };
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.remove(if field == "thumb" { "_poster_url" } else { "_art_url" });
                if field == "thumb" {
                    if let Some(parent) = object.get("parentThumb").filter(|value| truthy(value)).cloned() {
                        object.insert(field.to_string(), parent);
                    }
                } else if let Some(source) = source {
                    object.insert(field.to_string(), source);
                }
            }
            self.cascade_artwork_rows(key);
        }
    }

    /// Re-derive every borrowed artwork field under a row whose artwork changed.
    ///
    /// Cycle 27, measured on the copy over three writes: a borrowed artwork field is
    /// DERIVED from its owner, not stored, so a write to the owner re-derives it on
    /// every borrower -- including CREATING it. A season that OWNS an `art` keeps it
    /// through a show pick (cycle 29); a season carries the show's theme as
    /// `parentTheme` (cycle 61).
    pub(super) fn cascade_artwork_rows(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
        if kind != "show" && kind != "season" {
            return;
        }
        let show = if kind == "show" {
            Some(row.clone())
        } else {
            self.artwork_parent(&row, "parentRatingKey")
        };
        let own_thumb = owns_field(Some(&row), "thumb");
        for child in self.children(key) {
            let child_key = key_of(&child).unwrap_or_default();
            let art_owner = if kind == "show" {
                if owns_field(Some(&child), "art") { Some(child.clone()) } else { Some(row.clone()) }
            } else if owns_field(Some(&row), "art") {
                Some(row.clone())
            } else {
                show.clone()
            };
            if let Some(object) = self.catalog.get_mut(&child_key).and_then(Value::as_object_mut) {
                if kind == "show" {
                    set_or_remove(object, "parentThumb", row.get("thumb"));
                    set_or_remove(object, "art", art_owner.as_ref().and_then(|owner| owner.get("art")));
                    object.remove("theme");
                    set_or_remove(object, "parentTheme", row.get("theme"));
                } else {
                    if own_thumb {
                        set_or_remove(object, "parentThumb", row.get("thumb"));
                    } else {
                        object.remove("parentThumb");
                    }
                    set_or_remove(object, "art", art_owner.as_ref().and_then(|owner| owner.get("art")));
                }
                // A row that borrows THIS row's poster shows it, so its own `thumb`
                // follows the new stamp -- and goes when the owner's does.
                if object
                    .get("thumb")
                    .map(string_value)
                    .is_some_and(|value| value.starts_with(&format!("/library/metadata/{key}/")))
                {
                    set_or_remove(object, "thumb", row.get("thumb"));
                }
            }
            if kind == "show" {
                let child_now = self.catalog.get(&child_key).cloned();
                let leaf_art_owner = if owns_field(child_now.as_ref(), "art") {
                    child_now
                } else {
                    Some(row.clone())
                };
                for leaf in self.leaves(&child_key) {
                    let leaf_key = key_of(&leaf).unwrap_or_default();
                    if let Some(object) = self.catalog.get_mut(&leaf_key).and_then(Value::as_object_mut) {
                        for (name, target) in [
                            ("thumb", "grandparentThumb"),
                            ("art", "grandparentArt"),
                            ("theme", "grandparentTheme"),
                        ] {
                            set_or_remove(object, target, row.get(name));
                        }
                        set_or_remove(
                            object,
                            "art",
                            leaf_art_owner.as_ref().and_then(|owner| owner.get("art")),
                        );
                    }
                }
            }
        }
    }

    /// An uploaded candidate's bytes, served by `/library/metadata/{id}/file?url=`.
    pub(super) fn serve_upload(&self, url: &str) -> Response {
        let wanted = percent_decode(&url.replace("%2E", "."));
        match self.uploads.get(&wanted) {
            Some((body, content_type)) => {
                let mut response = Response::bytes(200, content_type.clone());
                response.body = body.clone();
                response
            }
            None => Response::error(404, "plexsim: not found"),
        }
    }
}

/// Give a row an owner's copy of a field, or take it away when the owner has none.
fn set_or_remove(object: &mut Map<String, Value>, name: &str, value: Option<&Value>) {
    match value.filter(|value| !value.is_null()) {
        Some(value) => {
            object.insert(name.to_string(), value.clone());
        }
        None => {
            object.remove(name);
        }
    }
}
