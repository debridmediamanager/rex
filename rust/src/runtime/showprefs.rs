//! A show's per-item settings and their row effects: the `/prefs` writer, the
//! EFFECTIVE value an item inherits from its library, and the `flattenSeasons` and
//! `episodeSort` projections. Ported from `server.py`'s item `/prefs` leaf and the
//! catalog's `show_pref` and `apply_show_prefs`, whose docstrings hold the
//! measurements (cycles 14, 15 and 36).

use super::*;

fn unmatched_guid(row: &Value) -> bool {
    row.get("guid").and_then(Value::as_str).is_some_and(|guid| {
        guid.starts_with("local://")
            || guid.starts_with("tv.plex.agents.none://")
            || guid.starts_with("none://")
    })
}

impl Runtime {
    /// The setting list an item of this type advertises, from the captured sidecar.
    fn item_pref_settings(&self, kind: &str) -> Vec<Value> {
        load_json_sidecar(&self.catalog_path, "item_prefs.json")
            .and_then(|value| value.get(kind).cloned())
            .and_then(|value| value.as_array().cloned())
            .unwrap_or_default()
    }

    /// The EFFECTIVE value of a show setting: the item's unless it inherits (`-1`),
    /// then the library's, then the library default. Measured on the copy across all
    /// four combinations for `flattenSeasons` and `episodeSort` (cycle 15).
    pub(super) fn show_pref(&self, show: &Value, name: &str) -> String {
        let mut stored = show
            .get("_prefs")
            .and_then(|prefs| prefs.get(name))
            .map(string_value);
        if stored.is_none() {
            stored = self
                .item_pref_settings(show.get("type").and_then(Value::as_str).unwrap_or(""))
                .iter()
                .find(|setting| setting.get("id").and_then(Value::as_str) == Some(name))
                .and_then(|setting| setting.get("value"))
                .map(string_value);
        }
        if let Some(stored) = stored.filter(|value| value != "-1") {
            return stored;
        }
        let section = show.get("librarySectionID").map(string_value).unwrap_or_default();
        if let Some(value) = self.section_prefs.get(&(section, name.to_string())) {
            return value.clone();
        }
        load_json_sidecar(&self.catalog_path, "section_prefs.json")
            .and_then(|value| value.get("show").cloned())
            .and_then(|value| value.as_array().cloned())
            .unwrap_or_default()
            .iter()
            .find(|setting| setting.get("id").and_then(Value::as_str) == Some(name))
            .and_then(|setting| setting.get("value"))
            .map(string_value)
            .unwrap_or_else(|| "-1".to_string())
    }

    /// Whether a show's effective `flattenSeasons` skips the season level: `1`, or
    /// `2` ("hide for single-season series") on a show with at most one season.
    pub(super) fn show_flattened(&self, show: &Value) -> bool {
        let value = self.show_pref(show, "flattenSeasons");
        value == "1"
            || (value == "2"
                && self.children(&key_of(show).unwrap_or_default()).len() <= 1)
    }

    /// The row effects of `flattenSeasons=1`, applied where a row is projected: the
    /// show gains `skipChildren: true` and every episode under it `skipParent: true`
    /// and LOSES `parentRatingKey` and `parentKey`, keeping `parentIndex` and
    /// `parentTitle`. `=0` and `=-1` put all of it back.
    pub(super) fn apply_flatten_projection(&self, object: &mut Map<String, Value>) {
        match object.get("type").and_then(Value::as_str) {
            Some("show") => {
                let row = Value::Object(object.clone());
                if self.show_flattened(&row) {
                    object.insert("skipChildren".to_string(), json!(true));
                } else {
                    object.remove("skipChildren");
                }
            }
            Some("episode") => {
                let show = object
                    .get("grandparentRatingKey")
                    .map(string_value)
                    .and_then(|key| self.catalog.get(&key).cloned());
                if show.as_ref().is_some_and(|show| self.show_flattened(show)) {
                    object.insert("skipParent".to_string(), json!(true));
                    object.remove("parentRatingKey");
                    object.remove("parentKey");
                } else {
                    object.remove("skipParent");
                }
            }
            _ => {}
        }
    }

    /// Cycle 14: the SHOW's effective `episodeSort=1` puts a season's `/children`
    /// NEWEST FIRST; `0` and `-1` put it back.
    pub(super) fn season_children_newest_first(&self, season: &Value) -> bool {
        season.get("type").and_then(Value::as_str) == Some("season")
            && season
                .get("parentRatingKey")
                .map(string_value)
                .and_then(|key| self.catalog.get(&key).cloned())
                .is_some_and(|show| self.show_pref(&show, "episodeSort") == "1")
    }

    /// `PUT /library/metadata/{id}/prefs` -- the item's Advanced settings.
    ///
    /// Measured: the GET is a 404 and only the PUT exists; a setting the ROW does not
    /// advertise is a 400 and nothing is applied (cycle 36: an unmatched show drops
    /// `languageOverride` and `useOriginalTitle`, an unmatched movie has none); an EMPTY
    /// value clears the setting. Cycle 14: the cascade belongs to `showOrdering` ALONE
    /// -- every other write pushes zero frames -- and on an unmatched show that write is
    /// a 500 whose value is nevertheless stored (cycle 36).
    pub(super) fn write_item_prefs(
        &mut self,
        key: &str,
        row: &Value,
        pairs: &[(String, String)],
        request: &Request,
    ) -> Response {
        if request.method != "PUT" {
            return Response::error(404, "plexsim: not found");
        }
        let kind = row.get("type").and_then(Value::as_str).unwrap_or("").to_string();
        let unmatched = unmatched_guid(row);
        let mut settings = self
            .item_pref_settings(&kind)
            .iter()
            .filter_map(|setting| setting.get("id").and_then(Value::as_str).map(str::to_string))
            .collect::<Vec<_>>();
        if unmatched {
            if kind == "movie" {
                settings.clear();
            } else {
                settings.retain(|name| name != "languageOverride" && name != "useOriginalTitle");
            }
        }
        let mut writes: Vec<(String, String)> = Vec::new();
        for (name, value) in pairs {
            if name.starts_with("X-Plex-") {
                continue;
            }
            // The last spelling of a repeated name wins, as the query dict reads it.
            writes.retain(|(known, _)| known != name);
            writes.push((name.clone(), value.clone()));
        }
        if writes.iter().any(|(name, _)| !settings.contains(name)) {
            return Response::error(400, "plexsim: bad request");
        }
        if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            let stored = object
                .entry("_prefs".to_string())
                .or_insert_with(|| Value::Object(Map::new()));
            if let Some(stored) = stored.as_object_mut() {
                for (name, value) in &writes {
                    if value.is_empty() {
                        stored.remove(name);
                    } else {
                        stored.insert(name.clone(), json!(value));
                    }
                }
            }
        }
        self.dirty = true;
        if !writes.is_empty() && kind == "show" && writes.iter().any(|(name, _)| name == "showOrdering") {
            if unmatched {
                self.save_if_dirty();
                return Response::error(500, "plexsim: internal error");
            }
            self.notify_show_cascade(key);
        }
        self.save_if_dirty();
        Response::empty(200)
    }
}
