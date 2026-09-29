//! The notification half of the item context menu and the library scans: the
//! timeline frames, activities and status rows a real PMS pushes, and the unmatch
//! cascade they announce. Ported from `server.py`'s `notify_*` family and
//! `library.py`'s `unmatch`, whose docstrings hold the measurements.

use super::*;

impl Runtime {
    /// A fresh activity uuid; the reference mints a random v4 per activity.
    pub(super) fn activity_uuid(&mut self) -> String {
        self.activity_serial += 1;
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default();
        let hex = bytes_digest(format!("{nanos}/{}", self.activity_serial).as_bytes());
        format!(
            "{}-{}-4{}-a{}-{}",
            &hex[0..8],
            &hex[8..12],
            &hex[13..16],
            &hex[17..20],
            &hex[20..32]
        )
    }

    pub(super) fn push_activity(&mut self, activity: &Value, event: &str) {
        let uuid = activity.get("uuid").cloned().unwrap_or(Value::Null);
        self.push_event(
            "activity",
            json!({"ActivityNotification": [{"event": event, "uuid": uuid, "Activity": activity}]}),
        );
    }

    /// One activity from `started` to `ended`, with the measured `updated` steps --
    /// (progress, subtitle) pairs -- between; the end keeps the last subtitle.
    pub(super) fn push_activity_run(&mut self, kind: &str, title: &str, steps: &[(i64, &str)]) {
        let uuid = self.activity_uuid();
        let mut activity = json!({"uuid": uuid, "type": kind, "cancellable": false,
            "userID": 1, "title": title, "subtitle": "", "progress": 0});
        self.push_activity(&activity, "started");
        for (progress, subtitle) in steps {
            activity["progress"] = json!(progress);
            activity["subtitle"] = json!(subtitle);
            self.push_activity(&activity, "updated");
        }
        activity["progress"] = json!(100);
        self.push_activity(&activity, "ended");
    }

    // The timeline's state alphabet, read off the reference and pinned against the
    // client's own reducer: 0 created, 3 queued/processing/loading an edit, 4 loading,
    // 5 DONE (and it carries NO metadataState), 9 deleted. `5` is what makes the
    // client refetch the row.

    /// Plex's numeric type code as a TimelineEntry carries it. A playlist is 15 and a
    /// collection 18 on the wire, and the client builds the URL of the row it
    /// updates from this number.
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


    pub(super) fn notify_timeline(&mut self, row: &Value, state: i64, metadata_state: Option<&str>) {
        self.notify_timeline_full(row, state, metadata_state, None, None, None);
    }

    /// A TimelineEntry for one row, with the field set measured off a real refresh:
    /// identifier, sectionID and itemID as STRINGS, the numeric type, the title,
    /// `state` and `metadataState` (omitted when None, which is what state 5 does).
    pub(super) fn notify_timeline_full(
        &mut self,
        row: &Value,
        state: i64,
        metadata_state: Option<&str>,
        media_state: Option<&str>,
        type_number: Option<i64>,
        title_override: Option<&str>,
    ) {
        let title = match title_override {
            Some(title) => title.to_string(),
            None => self.frame_title(row),
        };
        let item_id = row.get("ratingKey").map(string_value).unwrap_or_default();
        let mut kind = row
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let mut section_id = row
            .get("librarySectionID")
            .map(string_value)
            .unwrap_or_default();
        if kind.is_empty() || kind == "playlist" || kind == "collection" {
            if self.playlists.contains_key(&item_id) && self.catalog.get(&item_id).is_none() {
                kind = "playlist".to_string();
                section_id = "-1".to_string();
            } else if self.collections.contains_key(&item_id) && self.catalog.get(&item_id).is_none()
            {
                kind = "collection".to_string();
            }
        }
        let mut entry = Map::new();
        entry.insert("identifier".to_string(), json!("com.plexapp.plugins.library"));
        entry.insert("sectionID".to_string(), json!(section_id));
        entry.insert("itemID".to_string(), json!(item_id));
        entry.insert(
            "type".to_string(),
            json!(type_number.unwrap_or_else(|| Self::timeline_type(&kind))),
        );
        entry.insert("title".to_string(), json!(title));
        entry.insert("state".to_string(), json!(state));
        entry.insert("updatedAt".to_string(), json!(now()));
        if let Some(metadata_state) = metadata_state {
            entry.insert("metadataState".to_string(), json!(metadata_state));
        }
        if let Some(media_state) = media_state {
            entry.insert("mediaState".to_string(), json!(media_state));
        }
        self.push_event("timeline", json!({"TimelineEntry": Value::Object(entry)}));
    }



    /// Shape measured on fun mid-refresh: uuid, type, cancellable, userID, title,
    /// subtitle, progress. Pushes the `started` frame.
    pub(super) fn add_activity(&mut self, subtitle: &str, title: &str, kind: &str, progress: i64) -> Value {
        let uuid = self.activity_uuid();
        let activity = json!({"uuid": uuid, "type": kind, "cancellable": false,
            "userID": 1, "title": title, "subtitle": subtitle, "progress": progress});
        self.push_activity(&activity, "started");
        activity
    }

    pub(super) fn add_metadata_activity(&mut self, subtitle: &str) -> Value {
        self.add_activity(subtitle, "Updating Metadata", "library.update.item.metadata", -1)
    }

    /// End an activity the way a real refresh does: an `updated` at 49 unless the
    /// sequence already pushed it, then `ended` at 100.
    pub(super) fn finish_activity(&mut self, activity: &mut Value) {
        if activity.get("progress").and_then(Value::as_i64) != Some(49) {
            activity["progress"] = json!(49);
            self.push_activity(activity, "updated");
        }
        activity["progress"] = json!(100);
        self.push_activity(activity, "ended");
    }

    /// started -> ended with NO `updated` between (the chapter-thumbs activity, the
    /// season cascade).
    pub(super) fn end_activity_quietly(&mut self, activity: &mut Value) {
        self.push_activity(activity, "ended");
    }

    /// The seven-frame sequence a real PMS pushes for a metadata refresh: timeline(3,
    /// queued), activity started, activity updated 49, timeline(3, processing),
    /// timeline(4, loading), activity ended, timeline(5). Cycle 33: a never-matched
    /// row (`local://` guid) opens with `state 2 matching` and a bare `state 5`.
    pub(super) fn notify_refreshed(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        if row
            .get("guid")
            .and_then(Value::as_str)
            .is_some_and(|guid| guid.starts_with("local://"))
        {
            self.notify_timeline(&row, 2, Some("matching"));
            self.notify_timeline(&row, 5, None);
        }
        self.notify_timeline(&row, 3, Some("queued"));
        let subtitle = self.frame_title(&row);
        let mut activity = self.add_metadata_activity(&subtitle);
        activity["progress"] = json!(49);
        self.push_activity(&activity, "updated");
        self.notify_timeline(&row, 3, Some("processing"));
        self.notify_timeline(&row, 4, Some("loading"));
        self.finish_activity(&mut activity);
        self.notify_timeline(&row, 5, None);
    }

    /// The frames a show-wide re-read pushes: the show queued -> processing -> loading
    /// inside one `Updating Metadata` activity, then each season with its episodes,
    /// the media pass per episode, and the show's own state 5.
    pub(super) fn notify_show_cascade(&mut self, key: &str) {
        let Some(show) = self.catalog.get(key).cloned() else {
            return;
        };
        let subtitle = self.frame_title(&show);
        let mut activity = self.add_metadata_activity(&subtitle);
        self.notify_timeline(&show, 3, Some("queued"));
        self.notify_timeline(&show, 3, Some("processing"));
        self.notify_timeline(&show, 4, Some("loading"));
        let seasons = self.children(key);
        let count = seasons.len().max(1) as f64;
        let show_title = show.get("title").and_then(Value::as_str).unwrap_or_default().to_string();
        for (number, season) in seasons.iter().enumerate() {
            let index = season.get("index").and_then(value_as_i64).unwrap_or(0);
            activity["subtitle"] = json!(format!("{show_title} S{index:02}"));
            activity["progress"] = json!(py_round((number + 1) as f64 * 89.0 / count));
            self.push_activity(&activity, "updated");
            self.notify_timeline(season, 5, Some("processing"));
            self.notify_timeline(season, 4, Some("loading"));
            let episodes = self.children(&key_of(season).unwrap_or_default());
            for episode in &episodes {
                self.notify_timeline(episode, 5, Some("processing"));
                self.notify_timeline(episode, 4, Some("loading"));
                self.notify_timeline(episode, 4, Some("loading"));
                self.notify_timeline(episode, 5, None);
            }
            for episode in &episodes {
                self.notify_timeline_full(episode, 5, None, Some("analyzing"), None, None);
                self.notify_timeline_full(episode, 5, None, Some("thumbnailing"), None, None);
                self.notify_timeline(episode, 5, None);
            }
            self.notify_timeline(season, 5, None);
        }
        self.finish_activity(&mut activity);
        self.notify_timeline(&show, 5, None);
    }

    /// The eighteen frames a SEASON refresh pushes. Cycle 15/25: started at -1 and
    /// ended at 100 with nothing between.
    pub(super) fn notify_season_cascade(&mut self, key: &str) {
        let Some(season) = self.catalog.get(key).cloned() else {
            return;
        };
        let subtitle = self.frame_title(&season);
        let mut activity = self.add_metadata_activity(&subtitle);
        self.notify_timeline(&season, 3, Some("queued"));
        self.notify_timeline(&season, 3, Some("processing"));
        self.notify_timeline(&season, 4, Some("loading"));
        for episode in self.children(key) {
            self.notify_timeline(&episode, 5, Some("processing"));
            self.notify_timeline(&episode, 4, Some("loading"));
            self.notify_timeline(&episode, 4, Some("loading"));
            self.notify_timeline(&episode, 5, None);
        }
        activity["progress"] = json!(100);
        self.end_activity_quietly(&mut activity);
        self.notify_timeline(&season, 5, None);
    }

    /// The 46 frames a SHOW's unmatch pushes. Cycle 25: the opener and the activity
    /// progression are the MOVIE unmatch's, and the activity opens with the show's
    /// bare TITLE -- the one place the subtitle is not the frame title.
    pub(super) fn notify_show_unmatch_cascade(&mut self, key: &str) {
        let Some(show) = self.catalog.get(key).cloned() else {
            return;
        };
        self.notify_timeline(&show, 2, Some("matching"));
        self.notify_timeline(&show, 3, Some("matching"));
        self.notify_timeline(&show, 3, Some("queued"));
        let bare = show.get("title").and_then(Value::as_str).unwrap_or_default().to_string();
        let mut activity = self.add_metadata_activity(&bare);
        for (title, progress) in [
            ("Analyzing Media", -1),
            ("Analyzing Media", 25),
            ("Updating Metadata", 25),
            ("Updating Metadata", 50),
        ] {
            activity["title"] = json!(title);
            activity["progress"] = json!(progress);
            self.push_activity(&activity, "updated");
        }
        self.notify_timeline(&show, 3, Some("processing"));
        self.notify_timeline(&show, 4, Some("loading"));
        let seasons = self.children(key);
        let total = seasons.len() as i64;
        for (number, season) in seasons.iter().enumerate() {
            activity["subtitle"] = json!(self.frame_title(season));
            self.push_activity(&activity, "updated");
            activity["progress"] = json!(100 - 5 * (total - (number as i64 + 1)));
            self.push_activity(&activity, "updated");
            self.notify_timeline(season, 5, Some("processing"));
            self.notify_timeline(season, 4, Some("loading"));
            for episode in self.children(&key_of(season).unwrap_or_default()) {
                self.notify_timeline(&episode, 5, Some("processing"));
                self.notify_timeline(&episode, 4, Some("loading"));
                self.notify_timeline(&episode, 4, Some("loading"));
                self.notify_timeline(&episode, 5, None);
            }
            self.notify_timeline(season, 5, None);
        }
        activity["progress"] = json!(100);
        self.end_activity_quietly(&mut activity);
        self.notify_timeline(&show, 5, None);
    }

    /// The `backgroundProcessingQueue` frame that follows a queue-touching write
    /// (cycle 15). The measured delay is not reproduced.
    pub(super) fn notify_queue_regenerated(&mut self) {
        self.push_event(
            "backgroundProcessingQueue",
            json!({"BackgroundProcessingQueueEventNotification":
                [{"queueID": 1, "event": "queueRegenerated"}]}),
        );
    }

    /// The two frames an EDIT pushes: state 3, then done. `loading` for an edit to
    /// the item's own path, `queued` for one saved through the section path.
    pub(super) fn notify_edited(&mut self, row: &Value, metadata_state: &str) {
        self.notify_timeline(row, 3, Some(metadata_state));
        self.notify_timeline(row, 5, None);
    }




    /// Strip the agent's identity, the way `PUT .../unmatch` does. Measured on the
    /// reference by diffing the row's keys across the call: a movie's or a show's guid
    /// becomes `tv.plex.agents.none://<ratingKey>`, its title the FILE NAME without
    /// its extension, `summary` emptied but kept, and the artwork, Image, Guid, Rating,
    /// year and date all KEPT. An EPISODE's and a SEASON's guid becomes `local://<id>`
    /// and its title the generated `Episode N` / `Season N`; a SEASON cascades to every
    /// episode under it, and a SHOW to every season and episode.
    pub(super) fn unmatch_row(&mut self, key: &str) {
        let Some(original) = self.catalog.get(key).cloned() else {
            return;
        };
        let kind = original
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if kind == "episode" || kind == "season" {
            let index = original.get("index").map(py_str).unwrap_or_else(|| "None".to_string());
            let title = format!(
                "{} {index}",
                if kind == "season" { "Season" } else { "Episode" }
            );
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.insert("guid".to_string(), json!(format!("local://{key}")));
                object.insert("_generatedTitle".to_string(), json!(true));
                object.insert("title".to_string(), json!(title.clone()));
                if kind == "season" {
                    object.insert("titleSort".to_string(), json!(title));
                } else {
                    object.remove("titleSort");
                }
                object.insert("updatedAt".to_string(), json!(now()));
                if kind == "episode" {
                    // Re-measured on the copy against a DIRECT episode unmatch: the
                    // four people lists, `audienceRating` and `year` go, and `summary`
                    // is emptied -- the same field set a season's cascade drops.
                    for field in ["Director", "Writer", "Producer", "Role", "audienceRating", "year"] {
                        object.remove(field);
                    }
                    object.insert("summary".to_string(), json!(""));
                }
            }
            self.follow_parent_title(key);
            if kind == "season" {
                self.unmatch_season_children(key);
            }
            return;
        }
        let guid = format!("tv.plex.agents.none://{key}");
        if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            for field in [
                "Genre",
                "Country",
                "Role",
                "Director",
                "Writer",
                "Producer",
                "studio",
                "contentRating",
                "contentRatingAge",
                "tagline",
                "audienceRating",
                "rating",
            ] {
                object.remove(field);
            }
            object.insert("summary".to_string(), json!(""));
            object.insert("guid".to_string(), json!(guid.clone()));
        }
        if kind == "show" {
            // Re-measured on the copy: a SHOW unmatch cascades the whole way down --
            // every season and episode becomes `local://`, each season's `parentGuid`
            // and each episode's `grandparentGuid` follow the show's new guid, and the
            // artwork the children borrowed from the show goes with it, as does the
            // `contentRating` they inherited.
            for season in self.children(key) {
                if season.get("type").and_then(Value::as_str) != Some("season") {
                    continue;
                }
                let season_key = key_of(&season).unwrap_or_default();
                self.unmatch_row(&season_key);
                if let Some(object) = self.catalog.get_mut(&season_key).and_then(Value::as_object_mut) {
                    object.insert("parentGuid".to_string(), json!(guid.clone()));
                    object.remove("parentThumb");
                    object.remove("parentStudio");
                }
            }
            for leaf in self.leaves(key) {
                let leaf_key = key_of(&leaf).unwrap_or_default();
                if let Some(object) = self.catalog.get_mut(&leaf_key).and_then(Value::as_object_mut) {
                    object.insert("grandparentGuid".to_string(), json!(guid.clone()));
                    object.remove("grandparentThumb");
                    object.remove("grandparentArt");
                    object.remove("contentRating");
                    object.remove("_inheritedContentRating");
                }
            }
            if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
                object.remove("art");
            }
        }
        // The FILE NAME is the only title left that an agent did not supply.
        let file_title = original
            .get("Media")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .flat_map(|media| media.get("Part").and_then(Value::as_array).into_iter().flatten())
            .find_map(|part| {
                let name = part
                    .get("file")
                    .map(string_value)
                    .unwrap_or_default()
                    .rsplit('/')
                    .next()
                    .unwrap_or_default()
                    .to_string();
                (!name.is_empty()).then(|| {
                    name.rsplit_once('.')
                        .map(|(stem, _)| stem.to_string())
                        .unwrap_or(name.clone())
                })
            });
        if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            if let Some(title) = file_title {
                object.insert("title".to_string(), json!(title));
            }
            object.insert("updatedAt".to_string(), json!(now()));
        }
    }

    /// The season half of an unmatch: the season loses its `year` (the show's then
    /// shows through as `parentYear`), its `thumb` falls back to the SHOW's, and every
    /// episode under it is unmatched with its `parentGuid` following the season.
    pub(super) fn unmatch_season_children(&mut self, season_key: &str) {
        let Some(season) = self.catalog.get(season_key).cloned() else {
            return;
        };
        let parent = season
            .get("parentRatingKey")
            .map(string_value)
            .and_then(|parent| self.catalog.get(&parent).cloned());
        if let Some(object) = self.catalog.get_mut(season_key).and_then(Value::as_object_mut) {
            object.remove("year");
            if let Some(parent) = &parent {
                if let Some(year) = parent.get("year").filter(|value| !value.is_null()) {
                    object.insert("parentYear".to_string(), year.clone());
                }
                if let Some(thumb) = parent.get("thumb").filter(|value| truthy(value)) {
                    object.insert("thumb".to_string(), thumb.clone());
                }
            }
        }
        let season_guid = self
            .catalog
            .get(season_key)
            .and_then(|row| row.get("guid"))
            .cloned()
            .unwrap_or(Value::Null);
        let episodes = self
            .catalog
            .items
            .iter()
            .filter(|leaf| {
                leaf.get("type").and_then(Value::as_str) == Some("episode")
                    && leaf.get("parentRatingKey").map(string_value).as_deref() == Some(season_key)
            })
            .filter_map(key_of)
            .collect::<Vec<_>>();
        for leaf_key in episodes {
            self.unmatch_row(&leaf_key);
            if let Some(object) = self.catalog.get_mut(&leaf_key).and_then(Value::as_object_mut) {
                object.insert("parentGuid".to_string(), season_guid.clone());
                object.remove("parentThumb");
            }
        }
    }

    /// Copy a show's or a season's title onto the rows beneath it (`parentTitle`,
    /// and a show's `grandparentTitle`); an EMPTY title removes them.
    pub(super) fn follow_parent_title(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let kind = row.get("type").and_then(Value::as_str).unwrap_or_default();
        if kind != "show" && kind != "season" {
            return;
        }
        let title = row.get("title").filter(|value| truthy(value)).cloned();
        for child in self.catalog.items.iter_mut() {
            let parent = child.get("parentRatingKey").map(string_value);
            let grandparent = child.get("grandparentRatingKey").map(string_value);
            let Some(object) = child.as_object_mut() else { continue };
            let name = if parent.as_deref() == Some(key) {
                "parentTitle"
            } else if kind == "show" && grandparent.as_deref() == Some(key) {
                "grandparentTitle"
            } else {
                continue;
            };
            match &title {
                Some(title) => {
                    object.insert(name.to_string(), title.clone());
                }
                None => {
                    object.remove(name);
                }
            }
        }
    }

    /// The frames half of the item context menu's refresh, analyze and unmatch.
    pub(super) fn item_action(&mut self, key: &str, action: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
        match action {
            "refresh" => {
                // Cycle 15, re-measured on the copy: a refresh CASCADES. A SEASON runs
                // its episodes inside its own activity and a SHOW nests a season's
                // whole block under each season; the episode form is seven frames.
                if kind == "show" {
                    self.notify_show_cascade(key);
                } else if kind == "season" {
                    self.notify_season_cascade(key);
                } else {
                    self.notify_refreshed(key);
                }
            }
            "analyze" => self.analyze_item(key, &row),
            "unmatch" => {
                self.unmatch_row(key);
                let Some(row) = self.catalog.get(key).cloned() else {
                    return;
                };
                match kind.as_str() {
                    "episode" => {
                        self.notify_timeline(&row, 2, Some("matching"));
                        self.notify_timeline(&row, 5, None);
                    }
                    "season" => {
                        // Cycle 25: ONE `matching` frame and then the SEASON cascade.
                        self.notify_timeline(&row, 2, Some("matching"));
                        self.notify_season_cascade(key);
                    }
                    "show" => self.notify_show_unmatch_cascade(key),
                    "movie" => {
                        // Cycle 23, measured on plexscratch twice: TWELVE frames, the
                        // opener a `matching` pair and the activity stepping through a
                        // second TITLE, `Analyzing Media`.
                        self.notify_timeline(&row, 2, Some("matching"));
                        self.notify_timeline(&row, 3, Some("matching"));
                        self.notify_timeline(&row, 3, Some("queued"));
                        let subtitle = self.frame_title(&row);
                        let mut activity = self.add_metadata_activity(&subtitle);
                        for (title, progress) in [
                            ("Analyzing Media", -1),
                            ("Analyzing Media", 25),
                            ("Updating Metadata", 25),
                            ("Updating Metadata", 50),
                        ] {
                            activity["title"] = json!(title);
                            activity["progress"] = json!(progress);
                            self.push_activity(&activity, "updated");
                        }
                        self.notify_timeline(&row, 3, Some("processing"));
                        self.notify_timeline(&row, 4, Some("loading"));
                        activity["title"] = json!("Updating Metadata");
                        activity["progress"] = json!(100);
                        self.end_activity_quietly(&mut activity);
                        self.notify_timeline(&row, 5, None);
                    }
                    _ => {}
                }
            }
            _ => {}
        }
    }

}
