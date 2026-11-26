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

/// The theme, clear logo and square art a row shows: where each is kept (`_theme_url`, an
/// upload's key or the agent's url), the field holding the path it is served at, and the mark
/// naming what that path was minted for (as `_thumbFor` does a poster's).
pub(super) const ASSETS: [(&str, &str, &str, &str); 3] = [
    ("_theme_url", "theme", "theme", "_themeFor"),
    ("_clearLogo_url", "_clearLogo", "clearLogo", "_clearLogoFor"),
    ("_squareArt_url", "_squareArt", "squareArt", "_squareArtFor"),
];

/// Whether the row serves its own `theme`, `clearLogo` or `squareArt`: a season borrowing its
/// show's answers 404 there (plexlab, Firefly's Season 1).
pub(super) fn owns_asset(row: &Value, leaf: &str) -> bool {
    let Some((_, field, _, _)) = ASSETS.iter().find(|asset| asset.2 == leaf) else {
        return false;
    };
    let key = row.get("ratingKey").map(string_value).unwrap_or_default();
    row.get(*field)
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
    match owner
        .and_then(|owner| owner.get(field))
        .filter(|value| !value.is_null())
    {
        Some(value) => {
            child.insert(field.to_string(), value.clone());
        }
        None => {
            child.shift_remove(field);
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

/// An artwork POST's bytes: its body, or with none what its `url` names. Measured on an online
/// 1.43.5 (`tests/fixtures/tag_collections_s1435.json`, a poster set by url): the fetched
/// image is kept as an upload is, and an unreachable or loopback url is a 502, a page that is
/// no image a 406. The offline copies could fetch nothing, which is why rex answered 406.
pub(super) fn posted_artwork(request: &Request) -> Result<Vec<u8>, Response> {
    if !request.body.is_empty() {
        return Ok(request.body.clone());
    }
    let (_, query) = split_target(&request.target);
    let Some(url) = parse_query(&query).into_iter().find(|(name, _)| name == "url").map(|(_, url)| url) else {
        return Err(Response::error(406, "rex: not acceptable"));
    };
    // Public addresses only, as `/photo/:/transcode?url=` (tests/test_photo_ssrf.py).
    match super::transcoder::fetch_remote_image(&url, None) {
        None => Err(Response::error(502, "rex: bad gateway")),
        Some((_, kind)) if !kind.to_ascii_lowercase().starts_with("image/") => {
            Err(Response::error(406, "rex: not acceptable"))
        }
        Some((bytes, _)) => Ok(bytes),
    }
}

/// A clear logo's or theme's upload, a POST's body or what its `?url=` answers. A real PMS 1.43.4
/// keeps any bytes the url answers (a page, its own `/identity`) and refuses a url it cannot fetch
/// with a 406, where a poster's is a 502 (`tests/fixtures/collection_logos_s1434.json`). rex still
/// fetches public addresses only.
pub(super) fn posted_any(request: &Request) -> Result<Vec<u8>, Response> {
    if !request.body.is_empty() {
        return Ok(request.body.clone());
    }
    let (_, query) = split_target(&request.target);
    parse_query(&query)
        .into_iter()
        .find(|(name, url)| name == "url" && !url.is_empty())
        .and_then(|(_, url)| super::transcoder::fetch_remote_image(&url, None))
        .map(|(bytes, _)| bytes)
        .ok_or_else(|| Response::error(406, "rex: not acceptable"))
}

pub(super) fn sha1_hex(body: &[u8]) -> String {
    let mut hasher = Sha1::new();
    hasher.update(body);
    hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
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

    /// Clients cache a row's artwork by its path, and a real PMS mints a new version of the
    /// path whenever the artwork changes (`set_artwork`, cycles 14, 27). The feed's own
    /// changes -- a show re-identified in place, a retry, a season repaired -- replaced
    /// `_poster_url` under the same path, so Plex for Mac kept drawing the blank tile it had
    /// cached for Ahsoka and other shows on the production server (2026-10-13) while rex
    /// served their posters. Every row written since the last pass whose artwork is not the
    /// one its path was minted for (`_thumbFor`, `_artFor`) gets a new version, handed down
    /// to its children. A row that never recorded one is stamped once where the library is
    /// kept (`--state`): its cached artwork predates this and may be stale.
    pub(super) fn restamp_artwork(&mut self) {
        // (`artwork_writes` decides it for each row; a start's restore asks it as it reads.)
        let touched = self.catalog.take_touched();
        let keys: Vec<String> = if touched.all {
            self.catalog.hot().lazy().filter_map(|row| key_of(&row)).collect()
        } else {
            touched.keys.into_iter().collect()
        };
        let kept = self.state_path.is_some();
        let stamp = now();
        let mut cascade = Vec::new();
        let mut wrote = false;
        for key in keys {
            let Some(row) = self.catalog.get(&key) else {
                continue;
            };
            let writes = artwork_writes(row, &key, kept);
            if writes.is_empty() {
                continue;
            }
            wrote = true;
            let shown = matches!(row.get("type").and_then(Value::as_str), Some("show" | "season"));
            let mut bumped = false;
            if let Some(object) = self.catalog.get_mut(&key).and_then(Value::as_object_mut) {
                for (field, mark, from, bump) in writes {
                    if from == "" && ASSETS.iter().any(|asset| asset.1 == field) {
                        bumped |= object.shift_remove(field).is_some();
                    } else if bump {
                        // Never the version it had: a poster found the second its row was made.
                        let old = object.get(field).and_then(Value::as_str).and_then(|path| path.rsplit('/').next())
                            .and_then(|version| version.parse::<i64>().ok()).unwrap_or(0);
                        let version = stamp.max(old + 1);
                        let leaf = ASSETS.iter().find(|asset| asset.1 == field).map_or(field, |asset| asset.2);
                        object.insert(field.to_string(), json!(format!("/library/metadata/{key}/{leaf}/{version}")));
                        bumped = true;
                    }
                    object.insert(mark.to_string(), from);
                }
            }
            if bumped && shown {
                cascade.push(key);
            }
        }
        for key in cascade {
            self.cascade_artwork_from(&key);
        }
        if wrote {
            self.dirty = true;
        }
    }

    /// Re-derive every borrowed artwork field under a row whose artwork changed
    /// (cycles 25, 27, 29, 61).
    pub(super) fn cascade_artwork_from(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let kind = row
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
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
                    object.shift_remove("theme");
                    object.shift_remove("parentTheme");
                    if let Some(theme) = row.get("theme").filter(|v| !v.is_null()) {
                        object.insert("parentTheme".to_string(), theme.clone());
                    }
                } else {
                    if owns_field(Some(&row), "thumb") {
                        object.insert("parentThumb".to_string(), row["thumb"].clone());
                    } else {
                        object.shift_remove("parentThumb");
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
                                    object.shift_remove(&target);
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
            return Response::error(400, "rex: bad request");
        }
        let Some(mut row) = self.catalog.get(key).cloned() else {
            return Response::error(404, "rex: not found");
        };
        // Cycle 83, plexscratch42 (1.42.2): a poster or backdrop upload is served
        // `image/jpeg` WHATEVER its bytes -- PNG and plain text alike, directly and
        // through the transcoder, which still decodes them (`transcoder.rs`).
        let (field, mut prefix, served) = match kind {
            "posters" => ("thumb", "upload://posters".to_string(), Some("image/jpeg")),
            "themes" => ("theme", "upload://themes".to_string(), Some("audio/mpeg3")),
            "clearLogos" => (
                "clearLogo",
                "upload://clearLogos".to_string(),
                Some("image/jpeg"),
            ),
            _ => ("art", "upload://art".to_string(), Some("image/jpeg")),
        };
        match row.get("type").and_then(Value::as_str) {
            Some("season") => {
                prefix += &format!(
                    "/seasons/{}",
                    row.get("index").map(string_value).unwrap_or_default()
                );
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
            body.to_vec(),
            served.unwrap_or_else(|| image_type(body)).to_string(),
        );
        let stamp = now();
        let rating_key = key.to_string();
        let Some(object) = row.as_object_mut() else {
            return Response::error(500, "rex: internal error");
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
                    if !mine
                        .iter()
                        .any(|value| value.as_str() == Some(upload.as_str()))
                    {
                        mine.push(json!(upload));
                    }
                }
            }
        }
        let private = format!("_{}_url", if field == "thumb" { "poster" } else { field });
        let agent = private.replace("_url", "_agent");
        if let Some(previous) = object
            .get(&private)
            .and_then(Value::as_str)
            .map(str::to_string)
        {
            if !previous.starts_with("upload://") && !object.contains_key(&agent) {
                object.insert(agent, json!(previous));
            }
        }
        object.insert(private, json!(upload));
        let snapshot = Value::Object(object.clone());
        for name in ["thumb", "art", "theme"] {
            let present = object
                .get(name)
                .is_some_and(|value| !value.is_null() && value != "");
            if present && (name == field || owns_field(Some(&snapshot), name)) {
                object.insert(
                    name.to_string(),
                    json!(format!("/library/metadata/{rating_key}/{name}/{stamp}")),
                );
            }
        }
        if field == "clearLogo" || object.contains_key("_clearLogo") {
            object.insert(
                "_clearLogo".to_string(),
                json!(format!("/library/metadata/{rating_key}/clearLogo/{stamp}")),
            );
        }
        if field != "clearLogo" && !object.contains_key(field) {
            object.insert(
                field.to_string(),
                json!(format!("/library/metadata/{rating_key}/{field}/{stamp}")),
            );
        }
        object.insert("updatedAt".to_string(), json!(stamp));
        minted_for_sources(object);
        self.store_row(key, row);
        self.cascade_artwork_from(key);
        info!("ARTWORK upload {key} {kind} {} bytes -> {upload}", body.len());
        self.push_timeline(key, 5, None);
        self.dirty = true;
        Response::empty(200)
    }

    /// `PUT /library/metadata/{id}/{poster,art}?url=` -- pick one (cycles 14, 27).  The
    /// response body is the row's new versioned path.
    pub(super) fn set_artwork(
        &mut self,
        key: &str,
        kind: &str,
        query: &HashMap<String, String>,
    ) -> Response {
        let chosen = query
            .get("url")
            .map(|value| percent_decode(value))
            .unwrap_or_default();
        if chosen.is_empty() {
            return Response::error(400, "rex: no url");
        }
        let Some(mut row) = self.catalog.get(key).cloned() else {
            return Response::error(404, "rex: not found");
        };
        let field = if kind == "posters" { "thumb" } else { "art" };
        let private = if kind == "posters" {
            "_poster_url"
        } else {
            "_art_url"
        };
        let placeholder = format!("/library/metadata/{key}/{field}/1");
        let stamp = now();
        let Some(object) = row.as_object_mut() else {
            return Response::error(500, "rex: internal error");
        };
        if chosen == placeholder {
            object.shift_remove(private);
        } else {
            object.insert(private.to_string(), json!(chosen));
        }
        for name in ["thumb", "art", "theme"] {
            let value = object
                .get(name)
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let owned = value.starts_with(&format!("/library/metadata/{key}/"));
            if !value.is_empty() && (name == field || owned) {
                object.insert(
                    name.to_string(),
                    json!(format!("/library/metadata/{key}/{name}/{stamp}")),
                );
            }
        }
        if !object.contains_key(field) || object.get(field).is_some_and(Value::is_null) {
            object.insert(
                field.to_string(),
                json!(format!("/library/metadata/{key}/{field}/{stamp}")),
            );
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
        minted_for_sources(object);
        let body = object
            .get(field)
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        self.store_row(key, row);
        self.cascade_artwork_from(key);
        info!(
            "ARTWORK {key} {kind} -> {}",
            chosen.chars().take(80).collect::<String>()
        );
        self.push_timeline(key, 5, None);
        self.dirty = true;
        // The oracle's `("text", 200, path)`: the new artwork path as a `text/html`
        // body, which the client assigns straight onto the row.
        let mut response = Response::bytes(200, "text/html");
        response.body = body.into_bytes();
        response
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
            if row
                .get(field)
                .is_some_and(|value| !value.is_null() && value != "")
            {
                continue;
            }
            let kind = row
                .get("type")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            let season = self.parent_of(&row, "parentRatingKey");
            let show = self.parent_of(&row, "grandparentRatingKey");
            if let Some(object) = row.as_object_mut() {
                object.shift_remove(if field == "thumb" {
                    "_poster_url"
                } else {
                    "_art_url"
                });
                if field == "thumb" && object.get("parentThumb").is_some_and(|v| !v.is_null()) {
                    let parent = object["parentThumb"].clone();
                    object.insert(field.to_string(), parent);
                } else if field == "art" && (kind == "season" || kind == "episode") {
                    let source = if kind == "episode" {
                        handed_down_from(field, season.as_ref(), show.as_ref())
                    } else {
                        handed_down_from(field, None, season.as_ref())
                    };
                    if let Some(value) = source
                        .as_ref()
                        .and_then(|s| s.get(field))
                        .filter(|v| !v.is_null())
                    {
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
            .get(if kind == "posters" {
                "_poster_url"
            } else {
                "_art_url"
            })
            .and_then(Value::as_str)
            .map(str::to_string);
        let uploads = row
            .get("_uploads")
            .and_then(|uploads| uploads.get(field))
            .and_then(Value::as_array)
            .map(|list| {
                list.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect::<Vec<_>>()
            })
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
            .get(if kind == "posters" {
                "_poster_agent"
            } else {
                "_art_agent"
            })
            .and_then(Value::as_str)
            .map(str::to_string);
        let chose_upload = stored.as_ref().is_some_and(|value| uploads.contains(value));
        let picked = stored.clone();
        if chose_upload || stored.is_none() {
            stored = agent;
        }
        let current = row
            .get(field)
            .is_some_and(|value| !value.is_null() && value != "");
        let chose_remote = stored.is_some() && picked == stored;
        let placeholder = format!("/library/metadata/{rating_key}/{field}/1");
        if let Some(stored) = stored {
            entries.insert(
                0,
                json!({"key": stored, "ratingKey": stored, "thumb": stored,
                       "selected": chose_remote, "provider": "tmdb"}),
            );
        }
        entries.push(
            json!({"key": placeholder, "ratingKey": placeholder, "thumb": placeholder,
            "selected": !chose_remote && !chose_upload && current, "provider": "local"}),
        );
        container(entries)
    }
}

/// A client's own pick or upload has just minted the row's paths for the artwork it now
/// names: record that (`_thumbFor`, `_artFor`), or `restamp_artwork` takes the change for
/// one the feed made and mints the path a second time, a second past `updatedAt` and past
/// the path the pick answered.
fn minted_for_sources(object: &mut serde_json::Map<String, Value>) {
    let assets = ASSETS.iter().map(|(source, _, _, mark)| (*source, *mark));
    for (source, mark) in [("_poster_url", "_thumbFor"), ("_art_url", "_artFor")].into_iter().chain(assets) {
        let from = object.get(source).filter(|value| !value.is_null()).cloned().unwrap_or(json!(""));
        object.insert(mark.to_string(), from);
    }
}

/// The artwork Plex's agent names a row by, each with the path the row serves it at and the lock
/// a person's pick sets: what `ask_agent_again` replaces.
const AGENT_ARTWORK: [(&str, &str, &str); 5] = [
    ("_poster_url", "thumb", "thumb"),
    ("_art_url", "art", "art"),
    ("_theme_url", "theme", "theme"),
    ("_clearLogo_url", "_clearLogo", "clearLogo"),
    ("_squareArt_url", "_squareArt", "squareArt"),
];

/// The agent's artwork urls `row` holds, one string: what a failed fetch was read from.
pub(super) fn agent_urls(row: &Value) -> String {
    AGENT_ARTWORK.map(|(field, _, _)| row.get(field).map(string_value).unwrap_or_default()).join(" ")
}

/// Plex's agent replaces a title's artwork, and the url rex kept from it stops answering
/// (Funny Money's gracenote poster and background 403'd on 2026-11-17 while the agent named
/// new ones): a row's artwork asked of the agent again by its guid, past `plex_cache.db`, for
/// a Refresh Metadata and for its artwork whose fetch failed (`dead`, the urls `agent_urls` read
/// then: once per start for the urls a row holds, so a url the agent still names is not asked for on
/// every view, and ten minutes on when the agent gave no answer). Only artwork the row serves itself, from a url the agent gave or from none, is set:
/// a season keeps a copy of its show's background (`hierarchy::fill_season`) and serves the
/// show's, and the agent's answer for a season or episode names a background anyway (a real
/// library's, 2026-11-19; replacing the copy rewrote 12 Monkeys' four seasons for nothing). What a person picked (locked) or uploaded (`upload://`) stays;
/// `restamp_artwork` gives a new url new paths. Each is asked as plexlab's refresh asks it
/// (`refresh_query`): a show and then each season it holds with a guid, a season's episodes
/// taken from its answer, never asked on their own. A real PMS keeps the images it downloaded when it matched, so
/// it never meets a dead url: the failed fetch's ask is rex's own.
pub(super) fn spawn_ask_agent_again(shared: &SharedRuntime, keys: Vec<String>, dead: Option<String>) {
    let shared = Arc::clone(shared);
    let _ = std::thread::Builder::new().name("agent-artwork".to_string()).spawn(move || ask_agent_again(&shared, keys, dead));
}

/// Seconds a failed fetch waits to ask an agent that gave no answer again: ten minutes, the feed's
/// blank-tile pass. `REX_AGENT_ARTWORK_RETRY_S` shortens it for the suite.
fn agent_retry_after() -> i64 {
    std::env::var("REX_AGENT_ARTWORK_RETRY_S").ok().and_then(|seconds| seconds.parse().ok()).filter(|seconds| *seconds > 0).unwrap_or(600)
}

/// The query plexlab's PMS asked the agent's item with on a `PUT .../refresh` (2026-11-26,
/// `tests/fixtures/refresh_s1434.json`): a film's beside its `/extras`, which rex takes nothing
/// from and does not ask; a show's beside its `/extras` too, its seasons inside; a season's with
/// its episodes inside. An episode's refresh was not measured: it is asked bare, as before.
fn refresh_query(kind: &str) -> &'static str {
    match kind {
        "movie" => "excludeElements=Collection&includeReviews=1&updating=1",
        "show" => "episodeOrder=tmdbAiring&includeChildren=1&updating=1",
        "season" => "includeChildren=1&updating=1",
        _ => "",
    }
}

/// The agent's id of a row it can be asked about: a film, show, season or episode with Plex's guid.
pub(super) fn agent_id(row: &Value) -> Option<(&str, &str)> {
    let kind = row.get("type").and_then(Value::as_str).filter(|kind| matches!(*kind, "movie" | "show" | "season" | "episode"))?;
    let id = row.get("guid").and_then(Value::as_str)?.strip_prefix("plex://")?.strip_prefix(kind)?.strip_prefix('/')?;
    Some((kind, id))
}

/// What a row takes from the agent's answer for it: the artwork urls it names, for artwork the row
/// serves itself, and its scores. Whether anything changed.
fn take_answer(object: &mut Map<String, Value>, key: &str, title: &Value) -> bool {
    let said: HashMap<&str, Value> = crate::metadata::agent_fields(title, "movie")
        .into_iter()
        .filter_map(|(field, value)| Some((field, value?)))
        .chain([("_poster_url", title.get("thumb").cloned()), ("_art_url", title.get("art").cloned())].into_iter().filter_map(|(field, value)| Some((field, value?))))
        .collect();
    let mut gave = false;
    let own = format!("/library/metadata/{key}/");
    for (field, path, lock) in AGENT_ARTWORK {
        let Some(url) = said.get(field).filter(|url| url.as_str().is_some_and(|url| !url.is_empty())) else {
            continue;
        };
        // The agent's url, or none yet (the tile a title with no poster draws): never an upload.
        let agents = object.get(field).and_then(Value::as_str).is_none_or(|stored| stored.is_empty() || stored.starts_with("http"));
        // A film or show with no theme, logo or square art yet has no path for one: it takes
        // its own when the url comes (`artwork_writes`). A borrowed path is another row's.
        let serves = match object.get(path).and_then(Value::as_str) {
            Some(served) => served.starts_with(&own),
            None => !matches!(path, "thumb" | "art") && matches!(object.get("type").and_then(Value::as_str), Some("movie" | "show")),
        };
        if agents && serves && !super::edit::field_locked(object, lock) && object.get(field) != Some(url) {
            object.insert(field.to_string(), url.clone());
            gave = true;
        }
    }
    if gave {
        info!("ARTWORK {key} -- Plex's agent names new artwork");
    }
    // plexlab's passes over its library (the item request a refresh sends) took the agent's new
    // scores: Big Buck Bunny's Rotten Tomatoes 5.4 became 5.3 and Firefly's TMDB 8.3 8.4 on
    // 2026-11-12 (findings, "Ratings"). A season shows none.
    let kind = object.get("type").and_then(Value::as_str).filter(|kind| matches!(*kind, "movie" | "show" | "episode")).map(str::to_string);
    if let Some(kind) = kind {
        if crate::metadata::put_ratings(object, crate::metadata::ratings(title, &kind), super::edit::field_locked) {
            gave = true;
            info!("REFRESH {key} -- Plex's agent gives new ratings");
        }
    }
    gave
}

/// A title plexlab's refresh saved: its `updatedAt` the second the refresh was sent, and every
/// artwork path it serves itself that version, changed or not (Firefly's poster, background,
/// theme, clear logo and square art all took 1795735386 with its cast alone changed, its locked
/// Big Buck Bunny poster too on 2026-11-25). Its seasons and episodes show its new paths where
/// they borrow them (`cascade_artwork_from`) and keep their own `updatedAt`.
fn stamp_refreshed(object: &mut Map<String, Value>, key: &str, at: i64) {
    let own = format!("/library/metadata/{key}/");
    for field in ["thumb", "art", "theme", "_clearLogo", "_squareArt"] {
        let Some((leaf, version)) = object.get(field).and_then(Value::as_str).filter(|path| path.starts_with(&own)).and_then(|path| path.rsplit_once('/')) else {
            continue;
        };
        // Never the version it had: a pick the same second.
        let version = at.max(version.parse::<i64>().unwrap_or(0) + 1);
        let path = format!("{leaf}/{version}");
        object.insert(field.to_string(), json!(path));
    }
    object.insert("updatedAt".to_string(), json!(at));
    // The paths are the artwork's now: `restamp_artwork` mints none again.
    minted_for_sources(object);
}

fn ask_agent_again(shared: &SharedRuntime, keys: Vec<String>, dead: Option<String>) {
    // plexlab stamps what a refresh saved with the second it was sent (Season 1 1795735334, Firefly
    // 1795735386: each its PUT's), however long the agent takes to answer.
    let at = now();
    let refresh = dead.is_none();
    // -- under the lock: which rows, by their guid ---------------------------------------------
    let (plex, asked) = {
        let mut guard = feed::lock(shared);
        let runtime = &mut *guard;
        let Some(plex) = runtime.plex.clone() else {
            drop(guard);
            return notify_refreshed(shared, &keys, refresh);
        };
        let mut asked = Vec::new();
        for key in &keys {
            let Some(row) = runtime.catalog.get(key) else {
                continue;
            };
            // A show's refresh asks the show, then each season it holds with a guid (plexlab's
            // Firefly: the show, its matched Season 1; Season 2, matched to nothing, not asked).
            let mut rows = vec![(key.clone(), row.clone())];
            if refresh && row.get("type").and_then(Value::as_str) == Some("show") {
                rows.extend(runtime.children(key).into_iter().filter_map(|season| Some((key_of(&season)?, season))));
            }
            for (key, row) in rows {
                let Some((kind, id)) = agent_id(&row) else {
                    continue;
                };
                let (kind, id, language) = (kind.to_string(), id.to_string(), runtime.row_language(&row));
                // Once for the urls the row holds: a push that identifies it again can put back older
                // ones, and those are asked about again.
                let urls = agent_urls(&row);
                // A refresh gave the row other urls since the fetch failed: those are not known dead.
                if dead.as_ref().is_some_and(|failed| *failed != urls) {
                    continue;
                }
                let mark = format!("{key} {urls}");
                if refresh {
                    asked.push((key, mark, kind, id, language));
                } else if runtime.agent_artwork_asked.get(&mark).is_none_or(|again| *again <= now()) {
                    runtime.agent_artwork_asked.insert(mark.clone(), i64::MAX);
                    asked.push((key, mark, kind, id, language));
                }
            }
        }
        (plex, asked)
    };
    // -- off the lock: the agent, now --------------------------------------------------------
    let (mut found, mut unanswered) = (Vec::new(), Vec::new());
    for (key, mark, kind, id, language) in asked {
        match plex.title_now(&language, &id, refresh_query(&kind)) {
            Some(Some((title, changed))) => found.push((key, format!("plex://{kind}/{id}"), title, changed)),
            // A season the agent names nothing for: a show's next is asked still.
            Some(None) if refresh => {}
            // A refresh asks nothing more of an agent that gave no answer (a show's next season).
            None if refresh => break,
            _ => unanswered.push(mark),
        }
    }
    // -- under the lock: apply ---------------------------------------------------------------
    let mut guard = feed::lock(shared);
    // No answer (offline, unclaimed): a failed fetch asks again ten minutes on, not on the next
    // view. Dropping the mark made every view of every dead tile ask while the agent answered 503
    // (five rows, three rounds of views: 15 asks, 2026-11-26), and an outage fails every fetch.
    let again = now() + agent_retry_after();
    for mark in unanswered {
        guard.agent_artwork_asked.insert(mark, again);
    }
    let (mut given, mut stamped) = (false, Vec::new());
    for (key, guid, title, changed) in found {
        // The row, and a season's episodes from the answer's own (`includeChildren=1`).
        let mut rows = vec![(key.clone(), guid, title.clone())];
        if guid_kind(&rows[0].1) == "season" {
            let held: HashMap<String, String> = guard.children(&key).iter().filter_map(|episode| Some((episode.get("guid")?.as_str()?.to_string(), key_of(episode)?))).collect();
            let children = title.pointer("/Children/Metadata").and_then(Value::as_array).cloned().unwrap_or_default();
            rows.extend(children.into_iter().filter_map(|episode| {
                let guid = episode.get("guid")?.as_str()?.to_string();
                Some((held.get(&guid)?.clone(), guid, episode))
            }));
        }
        for (key, guid, title) in rows {
            // A Fix Match while the agent was asked: the answer is the old title's.
            let Some(object) = guard.catalog.get_mut(&key).and_then(Value::as_object_mut).filter(|row| row.get("guid").and_then(Value::as_str) == Some(guid.as_str())) else {
                continue;
            };
            given |= take_answer(object, &key, &title);
            // plexlab's refresh saved a title whose answer was not the last one it was given, and
            // moved nothing for one whose was (Big Buck Bunny, 2026-11-26 23:19).
            if refresh && changed.contains(&guid) {
                stamp_refreshed(object, &key, at);
                stamped.push(key);
            }
        }
    }
    for key in &stamped {
        guard.cascade_artwork_from(key);
    }
    if !stamped.is_empty() {
        info!("REFRESH {} -- saved at {at}", stamped.join(", "));
    }
    // The refresh's frames once its answers are in: plexlab's last, the row's 5, comes after the
    // agent answered and the row was saved, and Plex Web reads the row again on it.
    if refresh {
        for key in &keys {
            guard.notify_refresh(key);
        }
    }
    if given || !stamped.is_empty() {
        guard.dirty = true;
        let snapshot = guard.flush_state();
        drop(guard);
        write_or_log(snapshot);
    }
}

fn guid_kind(guid: &str) -> &str {
    guid.strip_prefix("plex://").and_then(|rest| rest.split_once('/')).map_or("", |(kind, _)| kind)
}

/// A refresh's frames when no agent is there to ask.
fn notify_refreshed(shared: &SharedRuntime, keys: &[String], refresh: bool) {
    if refresh {
        let mut guard = feed::lock(shared);
        for key in keys {
            guard.notify_refresh(key);
        }
    }
}

/// What `restamp_artwork` writes to `key`'s row: each artwork field whose path was not minted
/// for its artwork, with the artwork and whether its path gets a new version. `kept`: the
/// library is kept (`--state`). Empty for a row whose artwork is as its paths say.
pub(super) fn artwork_writes(row: &Value, key: &str, kept: bool) -> Vec<(&'static str, &'static str, Value, bool)> {
    let mut writes = Vec::new();
    for (source, field, mark) in [("_poster_url", "thumb", "_thumbFor"), ("_art_url", "art", "_artFor")] {
        let owned = row
            .get(field)
            .and_then(Value::as_str)
            .is_some_and(|path| path.starts_with(&format!("/library/metadata/{key}/{field}/")));
        let from = row.get(source).filter(|value| !value.is_null()).cloned().unwrap_or(json!(""));
        match row.get(mark) {
            Some(minted) if *minted == from => {}
            None if !owned || !kept => writes.push((field, mark, from, false)),
            _ if owned => writes.push((field, mark, from, true)),
            _ => writes.push((field, mark, from, false)),
        }
    }
    // A theme, logo or square art gets its path when it comes and loses it when it goes, a new
    // version when it changes; a row that never had one is left as it is, unwritten.
    for (source, field, _, mark) in ASSETS {
        let from = row.get(source).filter(|value| value.as_str().is_some_and(|url| !url.is_empty())).cloned().unwrap_or(json!(""));
        let has_path = row.get(field).is_some_and(|value| !value.is_null());
        match row.get(mark) {
            Some(minted) if *minted == from && (has_path || from == "") => {}
            None if from == "" && !has_path => {}
            _ => writes.push((field, mark, from, true)),
        }
    }
    writes
}

/// Uploaded artwork by its `upload://` key: its type and length, and its bytes only until the
/// state file has the save that wrote them (`trim`); then the file's are read when a request
/// asks (`Runtime::upload`). heaptrack on the production server's `rex.db` (44,644 rows,
/// f0d4dc7a): 55 MB of a 285.5 MB idle heap was its 379 uploads, every one decoded at the start
/// and held, for a custom poster asked for now and then. With no state file every upload stays
/// here.
#[derive(Debug, Default)]
pub(super) struct Uploads {
    entries: HashMap<String, Upload>,
    /// Uploaded since a snapshot last took them (`take_changed`): the next one writes them.
    changed: HashSet<String>,
}

#[derive(Debug)]
struct Upload {
    kind: String,
    len: usize,
    bytes: Option<Vec<u8>>,
    /// The last save that wrote it, as `rows.rs` keeps a row's.
    serial: u64,
}

impl Uploads {
    pub(super) fn insert(&mut self, key: String, bytes: Vec<u8>, kind: String) {
        let serial = self.entries.get(&key).map_or(0, |upload| upload.serial);
        self.changed.insert(key.clone());
        self.entries.insert(key, Upload { kind, len: bytes.len(), bytes: Some(bytes), serial });
    }

    /// One the state file holds, restored: only its type and length are kept.
    pub(super) fn restore(&mut self, key: String, kind: String, len: usize) {
        self.entries.insert(key, Upload { kind, len, bytes: None, serial: 0 });
    }

    pub(super) fn len_of(&self, key: &str) -> Option<usize> {
        self.entries.get(key).map(|upload| upload.len)
    }

    /// The bytes and type of `key`: those held, else what `read` reads from the file.
    pub(super) fn get(&self, key: &str, read: impl FnOnce(&str) -> Option<Vec<u8>>) -> Option<(Vec<u8>, String)> {
        let upload = self.entries.get(key)?;
        let bytes = match &upload.bytes {
            Some(bytes) => bytes.clone(),
            None => read(key)?,
        };
        Some((bytes, upload.kind.clone()))
    }

    /// The uploads `keys` names, or every one, as the state file stores them: `[base64, type]`.
    pub(super) fn document(&self, keys: Option<&HashSet<String>>, read: impl Fn(&str) -> Option<Vec<u8>>) -> Map<String, Value> {
        let all: Vec<&String> = match keys {
            Some(keys) => keys.iter().collect(),
            None => self.entries.keys().collect(),
        };
        all.into_iter()
            .filter_map(|key| {
                let (bytes, kind) = self.get(key, &read)?;
                Some((key.clone(), json!([base64_encode(&bytes), kind])))
            })
            .collect()
    }

    pub(super) fn take_changed(&mut self) -> HashSet<String> {
        std::mem::take(&mut self.changed)
    }

    /// A save took `keys` and queued them as `serial`.
    pub(super) fn saved(&mut self, keys: &HashSet<String>, serial: u64) {
        for key in keys {
            if let Some(upload) = self.entries.get_mut(key) {
                upload.serial = upload.serial.max(serial);
            }
        }
    }

    /// Let go of the bytes the file has: each upload no snapshot waits to take whose save is
    /// durable (`store::written`).
    pub(super) fn trim(&mut self, durable: u64) {
        for (key, upload) in self.entries.iter_mut() {
            if upload.serial <= durable && !self.changed.contains(key) {
                upload.bytes = None;
            }
        }
    }
}

/// An upload's bytes as the state file stores them, read through the catalog's own reader.
pub(super) fn upload_from_file(catalog: &Catalog, key: &str) -> Option<Vec<u8>> {
    let body = catalog.state_row("state:uploads", key)?;
    let stored: Value = serde_json::from_str(&body).ok()?;
    base64_decode_bytes(stored.get(0)?.as_str()?)
}

#[cfg(test)]
mod asset_tests {
    use super::*;

    /// A theme, logo or square art gets its path when it comes, a new one when it changes and none
    /// when it goes; a row that never had one is not written (the production server's 45,328 rows
    /// would all be, at the first start).
    #[test]
    fn an_agent_asset_gets_its_own_path_and_loses_it_with_the_asset() {
        let mut row = json!({"ratingKey": "7", "_thumbFor": "", "_artFor": ""});
        assert!(artwork_writes(&row, "7", true).is_empty());
        row["_theme_url"] = json!("https://tvthemes.plexapp.com/78874.mp3");
        assert_eq!(artwork_writes(&row, "7", true), vec![("theme", "_themeFor", row["_theme_url"].clone(), true)]);
        row["theme"] = json!("/library/metadata/7/theme/5");
        row["_themeFor"] = row["_theme_url"].clone();
        assert!(artwork_writes(&row, "7", true).is_empty());
        assert!(owns_asset(&row, "theme") && !owns_asset(&row, "clearLogo"));
        row["_theme_url"] = json!("https://tvthemes.plexapp.com/1.mp3");
        assert_eq!(artwork_writes(&row, "7", true).len(), 1, "a changed theme is a new version");
        row.as_object_mut().unwrap().shift_remove("_theme_url");
        assert_eq!(artwork_writes(&row, "7", true), vec![("theme", "_themeFor", json!(""), true)]);
        // A season holding its show's path owns none.
        assert!(!owns_asset(&json!({"ratingKey": "8", "theme": "/library/metadata/7/theme/5"}), "theme"));
    }
}

#[cfg(test)]
mod upload_tests {
    use super::Uploads;

    const KEY: &str = "upload://posters/0a";

    fn unread(_: &str) -> Option<Vec<u8>> {
        panic!("read from the file while held")
    }

    #[test]
    fn an_upload_is_held_until_the_file_has_its_save_then_read_from_the_file() {
        let mut uploads = Uploads::default();
        uploads.insert(KEY.to_string(), b"poster".to_vec(), "image/jpeg".to_string());
        let held = Some((b"poster".to_vec(), "image/jpeg".to_string()));
        uploads.trim(u64::MAX);
        assert_eq!(uploads.get(KEY, unread), held, "no snapshot has taken it");
        let taken = uploads.take_changed();
        uploads.saved(&taken, 7);
        uploads.trim(6);
        assert_eq!(uploads.get(KEY, unread), held, "its save is not in the file yet");
        uploads.trim(7);
        assert!(uploads.entries[KEY].bytes.is_none());
        let mut asked = Vec::new();
        let read = uploads.get(KEY, |key| {
            asked.push(key.to_string());
            Some(b"the file's".to_vec())
        });
        assert_eq!(read, Some((b"the file's".to_vec(), "image/jpeg".to_string())));
        assert_eq!(asked, [KEY]);
        assert_eq!(uploads.len_of(KEY), Some(6));
        // Uploaded again before its save is written: held until that one is.
        uploads.insert(KEY.to_string(), b"poster".to_vec(), "image/jpeg".to_string());
        let taken = uploads.take_changed();
        uploads.trim(6);
        assert_eq!(uploads.get(KEY, unread), held, "the save that wrote it is serial 7");
        uploads.restore("upload://art/0b".to_string(), "image/jpeg".to_string(), 3);
        assert_eq!(uploads.get("upload://art/0b", |_| None), None, "a restored upload is the file's");
        assert_eq!(uploads.document(Some(&taken), unread)[KEY], serde_json::json!(["cG9zdGVy", "image/jpeg"]));
    }
}
