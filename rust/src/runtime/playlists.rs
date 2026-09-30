//! Playlists, ported from `library.py`'s playlist model and `server.py`'s
//! `playlists_route`.
//!
//! A playlist's own columns (title, summary, playlistType, addedAt, updatedAt, the
//! sort title, the stored edit fields and its per-user state) live in the playlist's
//! `edits` record, keyed by its ratingKey; its members and their server-wide
//! `playlistItemID`s live on the `Playlist` itself.

use super::*;

/// `PLAYLIST_BASE`: playlists take 40000+ so a ratingKey names its own kind on sight.
pub(super) const PLAYLIST_BASE: i64 = 40000;

/// A list's per-user state, spliced in after `composite` (or `playlistType`).
const LIST_STATE: &[&str] = &[
    "userRating",
    "viewOffset",
    "viewCount",
    "lastViewedAt",
    "lastRatedAt",
];

impl Runtime {
    pub(super) fn playlist_columns(&self, id: &str) -> Map<String, Value> {
        self.edits.get(id).cloned().unwrap_or_default()
    }

    /// Field set measured on a real PMS's POST /playlists response.  `content` is the
    /// single read's (`/playlists/{id}`, `/library/metadata/{id}`) and `state` the
    /// per-user block the listing and the metadata alias carry (cycle 21).
    pub(super) fn playlist_row(&self, playlist: &Playlist, content: bool, state: bool) -> Value {
        let columns = self.playlist_columns(&playlist.id);
        let rows = self
            .playlist_keys(playlist)
            .iter()
            .filter_map(|key| self.catalog.get(key).cloned())
            .collect::<Vec<_>>();
        // Cycle 55: `sum(round(duration/1000, 0))` over each item's longest Media, in
        // integer seconds.
        let duration = rows
            .iter()
            .map(|row| {
                let longest = row
                    .get("Media")
                    .and_then(Value::as_array)
                    .map(|media| {
                        media
                            .iter()
                            .filter_map(|m| m.get("duration").and_then(Value::as_i64))
                            .max()
                            .unwrap_or(0)
                    })
                    .filter(|value| *value > 0)
                    .unwrap_or_else(|| row.get("duration").and_then(Value::as_i64).unwrap_or(0));
                longest / 1000
            })
            .sum::<i64>()
            * 1000;
        let stamp = columns
            .get("updatedAt")
            .and_then(Value::as_i64)
            .unwrap_or_else(now);
        let unmatched = columns.get("_unmatched").is_some();
        let mut row = Map::new();
        row.insert("ratingKey".to_string(), json!(playlist.id));
        row.insert(
            "key".to_string(),
            json!(format!("/playlists/{}/items", playlist.id)),
        );
        row.insert(
            "guid".to_string(),
            json!(if unmatched {
                format!("local://{}", playlist.id)
            } else {
                format!("com.plexapp.agents.none://plexsim-playlist-{}", playlist.id)
            }),
        );
        row.insert("type".to_string(), json!("playlist"));
        row.insert("title".to_string(), json!(playlist.title));
        row.insert(
            "summary".to_string(),
            json!(columns.get("summary").map(string_value).unwrap_or_default()),
        );
        row.insert("smart".to_string(), json!(playlist.smart));
        row.insert(
            "playlistType".to_string(),
            json!(columns
                .get("playlistType")
                .map(string_value)
                .filter(|kind| !kind.is_empty())
                .unwrap_or_else(|| "video".to_string())),
        );
        row.insert("leafCount".to_string(), json!(rows.len()));
        row.insert(
            "addedAt".to_string(),
            columns.get("addedAt").cloned().unwrap_or(json!(stamp)),
        );
        row.insert("updatedAt".to_string(), json!(stamp));
        if let Some(sort) = columns
            .get("titleSort")
            .filter(|sort| !string_value(sort).is_empty())
        {
            row.insert("titleSort".to_string(), sort.clone());
        }
        // Cycle 16: `PUT /playlists/{id}` takes the item edit grammar, so the row
        // carries back what it stored.
        for stored in [
            "year",
            "studio",
            "originallyAvailableAt",
            "thumb",
            "art",
            "Field",
        ] {
            if let Some(value) = columns.get(stored).filter(|value| {
                !value.is_null()
                    && !(value.is_string() && string_value(value).is_empty())
                    && !value.as_array().is_some_and(Vec::is_empty)
            }) {
                row.insert(stored.to_string(), value.clone());
            }
        }
        let images = [("thumb", "coverPoster"), ("art", "background")]
            .iter()
            .filter_map(|(field, kind)| {
                columns
                    .get(*field)
                    .filter(|value| !string_value(value).is_empty())
                    .map(|url| json!({"alt": playlist.title, "type": kind, "url": url}))
            })
            .collect::<Vec<_>>();
        if !images.is_empty() {
            row.insert("Image".to_string(), Value::Array(images));
        }
        if !rows.is_empty() {
            // Measured: an EMPTY playlist carries neither a composite nor a duration.
            row.insert(
                "composite".to_string(),
                json!(format!("/playlists/{}/composite/{stamp}", playlist.id)),
            );
            if !unmatched {
                row.insert("duration".to_string(), json!(duration));
            }
        }
        if playlist.smart {
            // The only thing that marks a smart playlist in the list: the client
            // draws its own icon from it.
            row.insert("icon".to_string(), json!("playlist://image.smart"));
            if content && !playlist.uri.is_empty() {
                row.insert("content".to_string(), json!(source_uri_for(&playlist.uri)));
            }
        }
        if state {
            let after = if row.contains_key("composite") {
                "composite"
            } else {
                "playlistType"
            };
            let spliced_state = LIST_STATE
                .iter()
                .filter_map(|name| {
                    columns
                        .get(*name)
                        .filter(|value| !value.is_null())
                        .map(|value| (name.to_string(), value.clone()))
                })
                .collect::<Vec<_>>();
            if !spliced_state.is_empty() {
                let mut spliced = Map::new();
                for (key, value) in row {
                    let marker = key == after;
                    spliced.insert(key, value);
                    if marker {
                        for (name, value) in &spliced_state {
                            spliced.insert(name.clone(), value.clone());
                        }
                    }
                }
                row = spliced;
            }
        }
        Value::Object(row)
    }

    /// The next server-wide `playlistItemID`.  Cycle 16: a SERVER-WIDE sequence --
    /// three lists built in a row took 40/41, 42/43, 44/45 -- not one per list.
    pub(super) fn next_playlist_item_id(&mut self) -> i64 {
        self.next_playlist_item += 1;
        self.next_playlist_item
    }

    /// `POST /playlists?type=&title=&smart=&uri=`.
    pub(super) fn create_playlist_ported(&mut self, query: &HashMap<String, String>) -> Response {
        // Measured on a writable copy of the reference: a create needs a `title`, a
        // `type` and a `uri` (each missing one is a 400), a type outside
        // video/audio/photo is a 500.
        if ["title", "type", "uri"]
            .iter()
            .any(|name| query.get(*name).is_none())
        {
            return Response::error(400, "plexsim: bad request");
        }
        let kind = query.get("type").cloned().unwrap_or_default();
        if !matches!(kind.as_str(), "video" | "audio" | "photo") {
            return Response::error(500, "plexsim: bad playlist type");
        }
        let uri = query.get("uri").cloned().unwrap_or_default();
        let mut rows = self.playable_rows_for_uri(&uri);
        // Cycle 41: a type/content mismatch returns 400, but first creates an EMPTY
        // playlist of that type and pushes its ordinary type-15 `created` frame.
        let mismatched = kind != "video" && !rows.is_empty();
        if mismatched {
            rows.clear();
        }
        let smart = query.get("smart").is_some_and(|value| value == "1");
        let key = self.next_playlist.max(PLAYLIST_BASE + 1);
        self.next_playlist = key + 1;
        let id = key.to_string();
        let title = query.get("title").cloned().unwrap_or_default();
        let stamp = now();
        let mut playlist = Playlist {
            id: id.clone(),
            title: title.clone(),
            items: Vec::new(),
            item_ids: Vec::new(),
            smart,
            uri: if smart { uri.clone() } else { String::new() },
        };
        if !smart {
            for row in &rows {
                let Some(row_key) = key_of(row) else { continue };
                if playlist.items.contains(&row_key) {
                    continue;
                }
                playlist.items.push(row_key);
                let item_id = self.next_playlist_item_id();
                playlist.item_ids.push(item_id);
            }
        }
        let mut columns = Map::new();
        columns.insert("title".to_string(), json!(title));
        columns.insert("playlistType".to_string(), json!(kind));
        columns.insert("summary".to_string(), json!(""));
        columns.insert("addedAt".to_string(), json!(stamp));
        columns.insert("updatedAt".to_string(), json!(stamp));
        // Cycle 16/21: a playlist is born `viewCount: 1` with `lastViewedAt` equal to
        // its `addedAt`, on the STORED row.
        columns.insert("viewCount".to_string(), json!(1));
        columns.insert("lastViewedAt".to_string(), json!(stamp));
        // Measured: a list created as `The Fresh List` already sorts as `Fresh List`.
        edit::derive_title_sort(&mut columns);
        self.edits.insert(id.clone(), columns);
        self.playlists.insert(id.clone(), playlist.clone());
        info!(
            "PLAYLIST create {title} smart={} ({} item(s))",
            if smart { "True" } else { "False" },
            rows.len()
        );
        self.push_list_timeline(&id, 15, 0, "created");
        self.dirty = true;
        self.save_if_dirty();
        if mismatched {
            return Response::error(400, "plexsim: bad request");
        }
        // The CREATE response omits `content` and the per-user-state block.
        let row = self.playlist_row(&playlist, false, false);
        Response::json(
            200,
            self.container(
                Map::from_iter([
                    ("size".to_string(), json!(1)),
                    ("Metadata".to_string(), Value::Array(vec![row])),
                ]),
                false,
            ),
        )
    }

    /// `PUT /playlists/{id}` (and the `/library/metadata/{id}` alias): the item edit
    /// grammar over the list's own columns.  Cycle 16: `year`,
    /// `originallyAvailableAt` and `studio` are stored, `thumb=`/`art=` mint artwork
    /// under `/library/metadata/{ratingKey}/`, every `.locked` locks; `userRating`,
    /// `guid`, `duration` and the timestamps are accepted and DISCARDED; any other name
    /// and a bare PUT are 400s.
    pub(super) fn edit_playlist_ported(
        &mut self,
        id: &str,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Response {
        self.edit_playlist_on(id, query, pairs, false)
    }

    /// `PUT /library/metadata/{playlist}`: cycle 20, measured on plexscratch over 57
    /// columns -- the ITEM path's whole validation (an unknown name a 400, an
    /// unparseable number a 500), applied to a COPY, then only the list's own title,
    /// summary and sort title are written.
    pub(super) fn edit_playlist_alias(
        &mut self,
        id: &str,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Response {
        self.edit_playlist_on(id, query, pairs, true)
    }

    fn edit_playlist_on(
        &mut self,
        id: &str,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
        alias: bool,
    ) -> Response {
        const STORED: &[&str] = &[
            "title",
            "summary",
            "titleSort",
            "year",
            "studio",
            "originallyAvailableAt",
            "thumb",
            "art",
        ];
        const DISCARDED: &[&str] = &[
            "userRating",
            "guid",
            "duration",
            "addedAt",
            "updatedAt",
            "viewCount",
            "lastViewedAt",
            "type",
        ];
        let Some(playlist) = self.playlists.get(id).cloned() else {
            return Response::error(404, "plexsim: not found");
        };
        let field_of = |name: &str| {
            name.strip_suffix(".value")
                .or_else(|| name.strip_suffix(".locked"))
                .unwrap_or(name)
                .to_string()
        };
        let named = pairs
            .iter()
            .filter(|(name, _)| !name.starts_with("X-Plex-"))
            .cloned()
            .collect::<Vec<_>>();
        if alias {
            let mut probe = self.playlist_columns(id);
            probe.insert("ratingKey".to_string(), json!(id));
            probe.insert("title".to_string(), json!(playlist.title));
            if let Err(error) = self.edit_row_copy(&probe, pairs, false) {
                return Response::error(error.status, &format!("plexsim: {}", error.detail));
            }
        }
        if named.is_empty() {
            return Response::error(400, "plexsim: bad request");
        }
        for (name, _) in named.iter().filter(|_| !alias) {
            let field = field_of(name);
            if !STORED.contains(&field.as_str()) && !DISCARDED.contains(&field.as_str()) {
                return Response::error(400, "plexsim: bad request");
            }
        }
        let mut columns = self.playlist_columns(id);
        columns.insert("ratingKey".to_string(), json!(id));
        columns.insert("title".to_string(), json!(playlist.title));
        let stored_pairs = named
            .iter()
            .filter(|(name, _)| {
                let field = field_of(name);
                if alias {
                    matches!(field.as_str(), "title" | "summary" | "titleSort")
                } else {
                    STORED.contains(&field.as_str())
                }
            })
            .cloned()
            .collect::<Vec<_>>();
        if !stored_pairs.is_empty() {
            match self.edit_row_copy(&columns, &stored_pairs, false) {
                Ok(Some((edited, _))) => columns = edited,
                Ok(None) => {}
                Err(error) => {
                    return Response::error(error.status, &format!("plexsim: {}", error.detail))
                }
            }
        }
        let stamp_before = columns
            .get("updatedAt")
            .map(string_value)
            .unwrap_or_default();
        for name in ["thumb", "art"].into_iter().filter(|_| !alias) {
            if query
                .get(name)
                .or_else(|| query.get(&format!("{name}.value")))
                .is_some()
            {
                // the path a real PMS mints is under the library metadata tree, keyed by
                // the playlist's own ratingKey -- measured
                columns.insert(
                    name.to_string(),
                    json!(format!("/library/metadata/{id}/{name}/{stamp_before}")),
                );
            }
        }
        // `edit_playlist`: `title=` stores the EMPTY string; an emptied list keeps its
        // old title's sort form, a new title re-derives it.
        let title = query
            .get("title.value")
            .or_else(|| query.get("title"))
            .cloned();
        let old_title = playlist.title.clone();
        if let Some(title) = &title {
            columns.insert("title".to_string(), json!(title));
            if title.is_empty() {
                if !old_title.is_empty() {
                    let locked =
                        columns
                            .get("Field")
                            .and_then(Value::as_array)
                            .is_some_and(|fields| {
                                fields.iter().any(|field| {
                                    field.get("name").and_then(Value::as_str) == Some("titleSort")
                                })
                            });
                    if !locked {
                        columns.insert(
                            "titleSort".to_string(),
                            json!(crate::hierarchy::sort_title(&old_title)),
                        );
                    }
                }
            } else {
                edit::derive_title_sort(&mut columns);
            }
        }
        if let Some(summary) = query.get("summary.value").or_else(|| query.get("summary")) {
            columns.insert("summary".to_string(), json!(summary));
        }
        columns.insert("updatedAt".to_string(), json!(now()));
        columns.shift_remove("ratingKey");
        columns.shift_remove("_generatedTitle");
        let new_title = columns.get("title").map(string_value).unwrap_or_default();
        if let Some(stored) = self.playlists.get_mut(id) {
            stored.title = new_title.clone();
        }
        self.edits.insert(id.to_string(), columns);
        // A playlist's own edit pair: state 3 `loading` then 5, type 15, section -1.
        let frame_row = json!({"ratingKey": id, "type": "playlist", "title": new_title,
                               "librarySectionID": -1});
        self.push_timeline_entry(
            &frame_row,
            3,
            Some("loading"),
            None,
            Some(15),
            Some(&new_title),
        );
        self.push_timeline_entry(&frame_row, 5, None, None, Some(15), Some(&new_title));
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    /// `PUT {playlist}/items?uri=` on a smart list: the Edit dialog's Save.  The stored
    /// query is REPLACED by the uri (cycle 31: an item uri too, on 1.42.2) and the row
    /// answers re-evaluated; no uri is a 400.
    pub(super) fn save_smart_playlist(
        &mut self,
        id: &str,
        query: &HashMap<String, String>,
    ) -> Response {
        let uri = query.get("uri").cloned().unwrap_or_default();
        if uri.is_empty() {
            return Response::error(400, "plexsim: bad request");
        }
        info!(
            "PLAYLIST {id} query <- {}",
            uri.chars().take(120).collect::<String>()
        );
        if let Some(stored) = self.playlists.get_mut(id) {
            stored.uri = uri;
        }
        self.edits
            .entry(id.to_string())
            .or_default()
            .insert("updatedAt".to_string(), json!(now()));
        self.dirty = true;
        self.save_if_dirty();
        let Some(playlist) = self.playlists.get(id).cloned() else {
            return Response::error(404, "plexsim: not found");
        };
        let row = self.playlist_row(&playlist, false, true);
        Response::json(
            200,
            self.container(
                Map::from_iter([
                    ("size".to_string(), json!(1)),
                    ("Metadata".to_string(), Value::Array(vec![row])),
                ]),
                false,
            ),
        )
    }
}

impl Runtime {
    /// A type-15 frame for a playlist, `sectionID "-1"` (`notify_timeline` on the
    /// stored list row, which carries no `type` of its own).
    fn push_timeline_playlist(
        &mut self,
        playlist: &Playlist,
        state: i64,
        metadata_state: Option<&str>,
    ) {
        let row = json!({"ratingKey": playlist.id, "title": playlist.title});
        self.push_timeline_entry(
            &row,
            state,
            metadata_state,
            None,
            Some(15),
            Some(&playlist.title),
        );
    }

    /// `/library/metadata/{playlist}/{leaf}` for the ITEM action leaves
    /// (`library_list_item`). Cycle 21, measured on plexscratch over a fresh playlist:
    /// it answers them the way a collection does except where the row type splits
    /// them -- `/analyze` is a 400 here, `/allLeaves` and `/grandchildren` are 404s, and
    /// `/related` is an empty container with neither the librarySection triple nor
    /// `allowSync`. plexsim 404'd every one, so Change Poster, Refresh Metadata and Fix
    /// Match on a playlist page did nothing at all. `None` leaves a GET of the artwork
    /// to the ordinary path.
    pub(super) fn playlist_action_leaf(
        &mut self,
        playlist: &Playlist,
        leaf: &str,
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Option<Response> {
        let method = request.method.as_str();
        if !matches!(method, "GET" | "HEAD" | "PUT" | "DELETE") {
            return Some(Response::error(404, "plexsim: not found"));
        }
        if matches!(leaf, "poster" | "art" | "thumb") {
            return match method {
                // `set_artwork(row, leaf + "s")`: only `posters` is the thumb, so a PUT
                // on `/thumb` picks the ART, exactly as the Python path does.
                "PUT" => Some(self.set_playlist_artwork(playlist, leaf == "poster", query)),
                // Cycle 31: a 404 on 1.42.2, like the item path.
                "DELETE" => Some(Response::error(404, "plexsim: not found")),
                _ => None,
            };
        }
        if !matches!(method, "GET" | "HEAD" | "PUT") {
            return Some(Response::error(404, "plexsim: not found"));
        }
        Some(match leaf {
            // The same no-parameters 400 the item path answers, and a playlist's
            // analyze splits from a collection's 200.
            "match" | "extras" | "analyze" => Response::error(400, "plexsim: bad request"),
            "related" => Response::json(
                200,
                json!({"MediaContainer": {"size": 0, "identifier": "com.plexapp.plugins.library",
                    "mediaTagPrefix": MEDIA_TAG_PREFIX, "mediaTagVersion": MEDIA_TAG_VERSION}}),
            ),
            "refresh" | "unmatch" => {
                // Cycle 23, measured twice on plexscratch: a playlist never runs the
                // activity block. Its refresh is four frames -- 2/matching, 5,
                // 3/queued, 5 -- and its unmatch two, 2/matching and 5. Cycle 26: the
                // unmatch moves `guid` to `local://{rk}` and drops `duration`.
                self.push_timeline_playlist(playlist, 2, Some("matching"));
                if leaf == "refresh" {
                    self.push_timeline_playlist(playlist, 5, None);
                    self.push_timeline_playlist(playlist, 3, Some("queued"));
                }
                self.push_timeline_playlist(playlist, 5, None);
                if leaf == "unmatch" {
                    self.edits
                        .entry(playlist.id.clone())
                        .or_default()
                        .insert("_unmatched".to_string(), json!(true));
                    self.dirty = true;
                    self.save_if_dirty();
                }
                Response::empty(200)
            }
            // A playlist has no leaf view of its own.
            _ => Response::error(404, "plexsim: not found"),
        })
    }

    /// `PUT /library/metadata/{playlist}/{poster,art}?url=` (`set_artwork` on the
    /// stored list row): the owned artwork re-versions onto one new stamp, the picked
    /// field is locked, `updatedAt` moves, one `state 5` frame goes out as type 15 with
    /// `sectionID "-1"` (cycle 22), and the body is the new path.
    fn set_playlist_artwork(
        &mut self,
        playlist: &Playlist,
        poster: bool,
        query: &HashMap<String, String>,
    ) -> Response {
        let chosen = query
            .get("url")
            .map(|value| percent_decode(value))
            .unwrap_or_default();
        if chosen.is_empty() {
            return Response::error(400, "plexsim: no url");
        }
        let field = if poster { "thumb" } else { "art" };
        let private = if poster { "_poster_url" } else { "_art_url" };
        let key = playlist.id.clone();
        let placeholder = format!("/library/metadata/{key}/{field}/1");
        let stamp = now();
        let columns = self.edits.entry(key.clone()).or_default();
        if chosen == placeholder {
            columns.shift_remove(private);
        } else {
            columns.insert(private.to_string(), json!(chosen));
        }
        for name in ["thumb", "art", "theme"] {
            let value = columns.get(name).map(string_value).unwrap_or_default();
            let owned = value.starts_with(&format!("/library/metadata/{key}/"));
            if !value.is_empty() && (name == field || owned) {
                columns.insert(
                    name.to_string(),
                    json!(format!("/library/metadata/{key}/{name}/{stamp}")),
                );
            }
        }
        if columns.get(field).is_none_or(Value::is_null) {
            columns.insert(
                field.to_string(),
                json!(format!("/library/metadata/{key}/{field}/{stamp}")),
            );
        }
        columns.insert("updatedAt".to_string(), json!(stamp));
        let mut fields = columns
            .get("Field")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        fields.retain(|entry| entry.get("name").and_then(Value::as_str) != Some(field));
        fields.push(json!({"locked": true, "name": field}));
        columns.insert("Field".to_string(), Value::Array(fields));
        let body = columns.get(field).map(string_value).unwrap_or_default();
        self.push_timeline_playlist(playlist, 5, None);
        self.dirty = true;
        self.save_if_dirty();
        // The oracle's `("text", 200, path)`: the new artwork path as a `text/html`
        // body, which the client assigns straight onto the row.
        let mut response = Response::bytes(200, "text/html");
        response.body = body.into_bytes();
        response
    }
}
