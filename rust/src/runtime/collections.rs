//! Collections: the stored model, the row a real PMS projects from it, the members'
//! `Collection` tag, and the `/library/collections` and `/library/metadata/{id}`
//! paths. Ported from `library.py`'s collection half (`create_collection`,
//! `add_collection_items`, `_collection_row`, `_collection_order`,
//! `rename_collection_tag`, `stamp_collection_tags`, `move_collection_item`,
//! `delete_collection`) and `server.py`'s `collections_route` and the collection
//! branch of `library_list_item`, whose docstrings hold the measurements.

use super::*;

/// The Plex type number a collection's `ratingCount` carries for its members.
fn member_type_number(subtype: &str) -> i64 {
    match subtype {
        "movie" => 1,
        "show" => 2,
        "season" => 3,
        "episode" => 4,
        _ => 1,
    }
}

/// The per-user state a list row carries, in this order and spliced in after its
/// `ratingCount` (cycle 21).
const LIST_STATE: &[&str] = &[
    "userRating",
    "viewOffset",
    "viewCount",
    "lastViewedAt",
    "lastRatedAt",
];

impl Collection {
    /// The tag the collection's members carry -- the name it was created under, which
    /// an ITEM-path rename leaves alone (cycle 9).
    pub(super) fn tag_name(&self) -> String {
        if self.tag.is_empty() {
            self.title.clone()
        } else {
            self.tag.clone()
        }
    }

    pub(super) fn stamp(&self) -> i64 {
        self.extra
            .get("updatedAt")
            .and_then(Value::as_i64)
            .unwrap_or(1700000000)
    }

    fn touch(&mut self) {
        self.extra.insert("updatedAt".to_string(), json!(now()));
    }

    pub(super) fn unmatched(&self) -> bool {
        self.prefs.get("_unmatched").map(String::as_str) == Some("1")
    }
}

impl Runtime {
    /// A collection's members in the order its `collectionSort` names (cycle 15): no
    /// sort or `0` is RELEASE DATE, `2` the custom order a move writes, and any other
    /// plain integer -- `1`, `3`, `-1`, `01`, `+1` -- ALPHABETICAL; `1.5`, ` 1`, `abc`
    /// and an empty value fall back to release date. A smart collection's members are
    /// its query, re-run on every read, in the query's own order: `collectionSort`
    /// orders a PLAIN collection only (`collection_children`), so a smart one never
    /// went through the release-date sort that put an undated row first.
    pub(super) fn collection_members(&self, collection: &Collection) -> Vec<Value> {
        if collection.smart {
            return self.rows_for_uri(&collection.uri);
        }
        let rows: Vec<Value> = collection
            .items
            .iter()
            .filter_map(|key| self.catalog.get(key).cloned())
            .collect();
        let sort = collection
            .prefs
            .get("collectionSort")
            .cloned()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "0".to_string());
        if sort == "2" {
            return rows;
        }
        let digits = sort.trim_start_matches(['+', '-']);
        let alphabetical = !digits.is_empty()
            && digits.bytes().all(|b| b.is_ascii_digit())
            && !digits.trim_start_matches('0').is_empty();
        let mut rows = rows;
        if alphabetical {
            rows.sort_by_key(|row| {
                row.get("titleSort")
                    .filter(|value| truthy(value))
                    .or_else(|| row.get("title"))
                    .map(string_value)
                    .unwrap_or_default()
                    .to_lowercase()
            });
        } else {
            rows.sort_by_key(|row| {
                row.get("originallyAvailableAt")
                    .map(string_value)
                    .unwrap_or_default()
            });
        }
        rows
    }

    /// The tag id a collection's members carry, which is also its row's `index`
    /// (cycle 14); a rename keeps it (cycle 15).
    pub(super) fn collection_tag_id(&self, collection: &Collection) -> i64 {
        collection
            .extra
            .get("tagID")
            .and_then(Value::as_i64)
            .unwrap_or_else(|| tag_id("Collection", &collection.tag_name()))
    }

    /// The row a real PMS answers for a collection (`_collection_row`).
    pub(super) fn dynamic_collection_row(&self, collection: &Collection) -> Value {
        let rows = self.collection_members(collection);
        let mut years = rows
            .iter()
            .filter_map(|row| row.get("year").filter(|value| truthy(value)).map(py_str))
            .collect::<Vec<_>>();
        years.sort();
        let stamp = collection.stamp();
        let unmatched = collection.unmatched();
        let mut row = Map::new();
        row.insert("ratingKey".to_string(), json!(collection.id));
        row.insert(
            "key".to_string(),
            json!(format!("/library/collections/{}/children", collection.id)),
        );
        row.insert(
            "guid".to_string(),
            json!(if unmatched {
                format!("local://{}", collection.id)
            } else {
                format!("collection://plexsim-{}", collection.id)
            }),
        );
        row.insert("type".to_string(), json!("collection"));
        row.insert("title".to_string(), json!(collection.title));
        row.insert(
            "subtype".to_string(),
            json!(if unmatched {
                "unknown"
            } else {
                collection.subtype.as_str()
            }),
        );
        row.insert(
            "summary".to_string(),
            collection
                .extra
                .get("summary")
                .cloned()
                .unwrap_or_else(|| json!("")),
        );
        row.insert(
            "index".to_string(),
            json!(self.collection_tag_id(collection)),
        );
        if !unmatched {
            row.insert(
                "ratingCount".to_string(),
                json!(
                    member_type_number(&collection.subtype)
                        + if collection.smart { 100 } else { 0 }
                ),
            );
        }
        row.insert(
            "thumb".to_string(),
            json!(format!(
                "/library/collections/{}/composite/{stamp}?width=400&height=600",
                collection.id
            )),
        );
        row.insert(
            "addedAt".to_string(),
            collection
                .extra
                .get("addedAt")
                .cloned()
                .unwrap_or_else(|| json!(stamp)),
        );
        row.insert("updatedAt".to_string(), json!(stamp));
        row.insert("childCount".to_string(), json!(rows.len()));
        row.insert(
            "librarySectionID".to_string(),
            collection
                .section
                .parse::<i64>()
                .map(|id| json!(id))
                .unwrap_or_else(|_| json!(collection.section)),
        );
        if let Some(section) = self.section(&collection.section) {
            row.insert(
                "librarySectionTitle".to_string(),
                section.get("title").cloned().unwrap_or(Value::Null),
            );
            row.insert(
                "librarySectionKey".to_string(),
                json!(format!("/library/sections/{}", collection.section)),
            );
        }
        if collection.smart {
            // Measured twice over: a collection's `smart` is the STRING "1".
            row.insert("smart".to_string(), json!("1"));
            if !collection.uri.is_empty() {
                row.insert(
                    "content".to_string(),
                    json!(percent_decode(&collection.uri)),
                );
            }
        }
        for name in ["titleSort", "contentRating"] {
            if let Some(value) = collection.extra.get(name).filter(|value| !value.is_null()) {
                row.insert(name.to_string(), value.clone());
            }
        }
        for name in ["collectionSort", "collectionMode"] {
            if let Some(value) = collection.prefs.get(name) {
                row.insert(name.to_string(), json!(value));
            }
        }
        for name in ["Field", "Label"] {
            if let Some(value) = collection.extra.get(name).filter(|value| !value.is_null()) {
                row.insert(name.to_string(), value.clone());
            }
        }
        if let Some(value) = collection
            .prefs
            .get("collectionFilterBasedOnUser")
            .filter(|value| !value.is_empty())
        {
            row.insert("collectionFilterBasedOnUser".to_string(), json!(value));
        }
        if !years.is_empty() && !collection.smart {
            // Measured: a SMART collection carries no minYear/maxYear.
            row.insert("minYear".to_string(), json!(years[0]));
            row.insert("maxYear".to_string(), json!(years[years.len() - 1]));
        }
        // Cycle 21: the per-user state sits after `ratingCount` (or `index` once an
        // unmatch removed it) and before `thumb`.
        let after = if row.contains_key("ratingCount") {
            "ratingCount"
        } else {
            "index"
        };
        // A collection never carries `viewCount` at all, whatever is scrobbled at it.
        let stored_state = self.list_state.get(&collection.id);
        let state = LIST_STATE
            .iter()
            .filter(|name| **name != "viewCount")
            .filter_map(|name| {
                stored_state
                    .and_then(|state| state.get(*name))
                    .or_else(|| collection.extra.get(*name))
                    .filter(|value| !value.is_null())
                    .map(|value| (name.to_string(), value.clone()))
            })
            .collect::<Vec<_>>();
        if state.is_empty() {
            return Value::Object(row);
        }
        let mut spliced = Map::new();
        for (key, value) in row {
            let hit = key == after;
            spliced.insert(key, value);
            if hit {
                for (name, value) in &state {
                    spliced.insert(name.clone(), value.clone());
                }
            }
        }
        Value::Object(spliced)
    }

    /// The Preferences block `includePreferences=1` adds: `collectionMode` and, for a
    /// plain collection, `collectionSort` -- a smart one carries
    /// `collectionFilterBasedOnUser` in that slot (cycle 15).
    pub(super) fn collection_preferences(&self, collection: &Collection) -> Vec<Value> {
        let second = if collection.smart {
            "collectionFilterBasedOnUser"
        } else {
            "collectionSort"
        };
        ["collectionMode", second]
            .iter()
            .map(|name| {
                let mut setting = match *name {
                    "collectionMode" => json!({
                        "id": "collectionMode", "label": "Collection Mode",
                        "summary": "How the items in the collection are displayed on the Library tab.",
                        "type": "text", "default": "-1", "value": "-1", "hidden": false,
                        "advanced": false, "group": "",
                        "enumValues": "-1:Library default|1:Hide items in this collection|2:Show this collection and its items|0:Hide this collection but show its items"}),
                    "collectionSort" => json!({
                        "id": "collectionSort", "label": "Collection order",
                        "summary": "How to sort the items in the collection.",
                        "type": "text", "default": "0", "value": "0", "hidden": false,
                        "advanced": false, "group": "",
                        "enumValues": "0:Release date|1:Alphabetical"}),
                    _ => json!({
                        "id": "collectionFilterBasedOnUser",
                        "label": "Collection filtering based on",
                        "summary": "Which user's activity (e.g. user rating, play count, or watched status) is used for the collection filtering.",
                        "type": "text", "default": "0", "value": "0", "hidden": false,
                        "advanced": false, "group": "",
                        "enumValues": "0:Always the server admin user|1:User currently viewing the content"}),
                };
                if let Some(value) = collection.prefs.get(*name) {
                    setting["value"] = json!(value);
                }
                setting
            })
            .collect()
    }

    fn collection_detail(&self, collection: &Collection, query: &HashMap<String, String>) -> Value {
        let mut row = self.dynamic_collection_row(collection);
        if query.get("includePreferences").map(String::as_str) == Some("1") {
            row["Preferences"] = json!({"Setting": self.collection_preferences(collection)});
        }
        row
    }

    pub(super) fn collection_section_fields(&self, collection: &Collection) -> Map<String, Value> {
        let mut fields = Map::new();
        fields.insert(
            "librarySectionID".to_string(),
            collection
                .section
                .parse::<i64>()
                .map(|id| json!(id))
                .unwrap_or_else(|_| json!(collection.section)),
        );
        if let Some(section) = self.section(&collection.section) {
            fields.insert(
                "librarySectionTitle".to_string(),
                section.get("title").cloned().unwrap_or(Value::Null),
            );
            fields.insert(
                "librarySectionUUID".to_string(),
                section.get("uuid").cloned().unwrap_or(Value::Null),
            );
        }
        fields
    }

    /// Put a collection's tag on a member (minted the way the catalog mints tags, and
    /// kept in the tag TABLE, cycle 61) and LOCK the member's `collection` field
    /// (cycle 60).
    fn tag_collection_member(&mut self, key: &str, name: &str) {
        let lowered = name.to_lowercase();
        let known = self
            .tag_table
            .get(&("Collection".to_string(), lowered.clone()))
            .cloned();
        let minted = known.unwrap_or_else(|| {
            let id = tag_id("Collection", name);
            json!({"id": id, "tag": name, "filter": format!("collection={id}")})
        });
        self.tag_table
            .entry(("Collection".to_string(), lowered))
            .or_insert_with(|| minted.clone());
        let Some(row) = self.catalog.get_mut(key).and_then(Value::as_object_mut) else {
            return;
        };
        let tags = row
            .entry("Collection".to_string())
            .or_insert_with(|| json!([]));
        if let Some(tags) = tags.as_array_mut() {
            if !tags
                .iter()
                .any(|tag| tag.get("tag").and_then(Value::as_str) == Some(name))
            {
                tags.push(minted);
            }
        }
        edit::set_lock(row, "collection", true);
    }

    fn untag_collection_member(&mut self, key: &str, name: &str) {
        let Some(row) = self.catalog.get_mut(key).and_then(Value::as_object_mut) else {
            return;
        };
        let lowered = name.to_lowercase();
        let empty = if let Some(tags) = row.get_mut("Collection").and_then(Value::as_array_mut) {
            tags.retain(|tag| {
                tag.get("tag").map(string_value).map(|t| t.to_lowercase()) != Some(lowered.clone())
            });
            tags.is_empty()
        } else {
            false
        };
        if empty {
            row.shift_remove("Collection");
        }
        row.shift_remove("_collectionLight");
    }

    /// Add rows to a collection: each joins once, is tagged and locked.
    fn add_collection_items(&mut self, id: &str, keys: &[String]) -> usize {
        let Some(collection) = self.collections.get(id).cloned() else {
            return 0;
        };
        let name = collection.tag_name();
        let mut added = 0;
        for key in keys {
            if collection.items.contains(key)
                || self
                    .collections
                    .get(id)
                    .is_some_and(|c| c.items.contains(key))
            {
                continue;
            }
            if let Some(stored) = self.collections.get_mut(id) {
                stored.items.push(key.clone());
            }
            self.tag_collection_member(key, &name);
            added += 1;
        }
        if added > 0 {
            if let Some(stored) = self.collections.get_mut(id) {
                stored.touch();
            }
        }
        added
    }

    /// `POST /library/collections?type=&title=&smart=&sectionId=&uri=`. Measured: a
    /// `title` and a `sectionId` are required (400 each); a collection over a show
    /// holds the SHOW; the members' tag is the title with its first ASCII letter
    /// capitalised unless the tag TABLE already spells it (cycle 61); a create pushes
    /// one type-18 `created` frame, and its RESPONSE carries no librarySection fields.
    pub(super) fn create_collection(&mut self, query: &HashMap<String, String>) -> Response {
        let Some(title) = query.get("title") else {
            return Response::error(400, "plexsim: bad request");
        };
        let Some(section) = query.get("sectionId").filter(|value| !value.is_empty()) else {
            return Response::error(400, "plexsim: bad request");
        };
        let uri = query.get("uri").cloned().unwrap_or_default();
        let rows = self.rows_for_uri(&uri);
        let subtype = rows
            .first()
            .and_then(|row| row.get("type").and_then(Value::as_str))
            .unwrap_or("movie")
            .to_string();
        let smart = query.get("smart").map(String::as_str) == Some("1");
        let id = self.next_collection.to_string();
        self.next_collection += 1;
        let title = if title.is_empty() {
            "Untitled".to_string()
        } else {
            title.clone()
        };
        let known = self
            .tag_table
            .get(&("Collection".to_string(), title.to_lowercase()))
            .and_then(|tag| tag.get("tag"))
            .map(string_value);
        let tag = known.unwrap_or_else(|| edit::capitalised(&title));
        let stamp = now();
        let mut extra = Map::new();
        extra.insert("summary".to_string(), json!(""));
        extra.insert("addedAt".to_string(), json!(stamp));
        extra.insert("updatedAt".to_string(), json!(stamp));
        extra.insert("_names".to_string(), json!([tag.to_lowercase()]));
        let mut stored = Collection {
            id: id.clone(),
            title: title.clone(),
            items: Vec::new(),
            smart,
            section: section.clone(),
            subtype,
            prefs: HashMap::new(),
            uri: if smart { uri.clone() } else { String::new() },
            tag,
            extra,
        };
        {
            let mut map = Map::new();
            map.insert("title".to_string(), json!(stored.title));
            edit::derive_title_sort(&mut map);
            if let Some(sort) = map.get("titleSort") {
                stored.extra.insert("titleSort".to_string(), sort.clone());
            }
        }
        self.collections.insert(id.clone(), stored);
        if !smart {
            let keys = rows.iter().filter_map(key_of).collect::<Vec<_>>();
            self.add_collection_items(&id, &keys);
        }
        let collection = self.collections.get(&id).cloned().expect("just inserted");
        let frame_row = json!({"ratingKey": id, "type": "collection", "title": collection.title,
            "librarySectionID": collection.section});
        self.push_timeline_collection(&frame_row, 0, Some("created"));
        let mut row = self.dynamic_collection_row(&collection);
        if let Some(object) = row.as_object_mut() {
            for name in [
                "librarySectionID",
                "librarySectionKey",
                "librarySectionTitle",
            ] {
                object.shift_remove(name);
            }
        }
        self.dirty = true;
        self.save_if_dirty();
        Response::json(
            200,
            json!({"MediaContainer": {"size": 1, "Metadata": [row]}}),
        )
    }

    /// A type-18 frame for a collection row (`sectionID` its library).
    fn push_timeline_collection(&mut self, row: &Value, state: i64, metadata_state: Option<&str>) {
        let mut entry = Map::new();
        entry.insert(
            "identifier".to_string(),
            json!("com.plexapp.plugins.library"),
        );
        entry.insert(
            "sectionID".to_string(),
            json!(row
                .get("librarySectionID")
                .map(string_value)
                .unwrap_or_default()),
        );
        entry.insert(
            "itemID".to_string(),
            json!(row.get("ratingKey").map(string_value).unwrap_or_default()),
        );
        entry.insert("type".to_string(), json!(18));
        entry.insert(
            "title".to_string(),
            json!(row.get("title").map(string_value).unwrap_or_default()),
        );
        entry.insert("state".to_string(), json!(state));
        entry.insert("updatedAt".to_string(), json!(now()));
        if let Some(metadata_state) = metadata_state {
            entry.insert("metadataState".to_string(), json!(metadata_state));
        }
        self.push_event("timeline", json!({"TimelineEntry": Value::Object(entry)}));
    }

    /// Run the ITEM edit grammar over a collection's row as a copy (cycle 20/21):
    /// its refusals are the collection's, and the collection writers below stay in
    /// charge of what is stored.
    fn collection_edit_refusal(
        &self,
        collection: &Collection,
        pairs: &[(String, String)],
    ) -> Option<Response> {
        let row = self.dynamic_collection_row(collection);
        let map = row.as_object().cloned().unwrap_or_default();
        match self.edit_row_copy(&map, pairs, false) {
            Err(error) => Some(Response::error(error.status, "plexsim: edit refused")),
            Ok(_) => None,
        }
    }

    /// The collection half of an edit: title, summary, the sort title and content
    /// rating, a sort-title lock and Labels (`library_list_item` PUT).
    fn write_collection_edit(&mut self, id: &str, pairs: &[(String, String)]) {
        let query = pairs.iter().cloned().collect::<HashMap<_, _>>();
        let title = query
            .get("title.value")
            .or_else(|| query.get("title"))
            .cloned();
        let summary = query
            .get("summary.value")
            .or_else(|| query.get("summary"))
            .cloned();
        let labels = {
            let pattern = regex::Regex::new(r"^label\[(\d+)\]\.tag\.tag$").expect("valid regex");
            let mut labels = pairs
                .iter()
                .filter_map(|(name, value)| {
                    pattern
                        .captures(name)
                        .and_then(|caps| caps.get(1))
                        .and_then(|index| index.as_str().parse::<i64>().ok())
                        .map(|index| (index, value.clone()))
                })
                .collect::<Vec<_>>();
            labels.sort_by_key(|(index, _)| *index);
            labels
                .into_iter()
                .map(|(_, value)| value)
                .filter(|value| !value.is_empty())
                .collect::<Vec<_>>()
        };
        let label_tags = if labels.is_empty() {
            None
        } else {
            let mut minted = Vec::new();
            for value in &labels {
                let lowered = value.to_lowercase();
                let tag = self
                    .tag_table
                    .get(&("Label".to_string(), lowered.clone()))
                    .cloned()
                    .unwrap_or_else(|| {
                        let spelled = edit::capitalised(value);
                        let id = tag_id("Label", &spelled);
                        json!({"id": id, "tag": spelled, "filter": format!("label={id}")})
                    });
                self.tag_table
                    .entry(("Label".to_string(), lowered))
                    .or_insert_with(|| tag.clone());
                minted.push(tag);
            }
            Some(minted)
        };
        let Some(collection) = self.collections.get_mut(id) else {
            return;
        };
        // edit_collection: an emptied title keeps its old sort form; a rename keeps
        // the tag the collection was created under.
        if let Some(title) = &title {
            if title.is_empty() && !collection.title.is_empty() {
                let old = collection.title.clone();
                collection.extra.insert(
                    "titleSort".to_string(),
                    json!(crate::hierarchy::sort_title(&old)),
                );
                collection.title = String::new();
            } else if !title.is_empty() && *title != collection.title {
                if collection.tag.is_empty() {
                    collection.tag = collection.title.clone();
                }
                collection.title = title.clone();
            }
        }
        if let Some(summary) = summary {
            collection
                .extra
                .insert("summary".to_string(), json!(summary));
        }
        collection.touch();
        for name in ["titleSort", "contentRating"] {
            if let Some(value) = query.get(&format!("{name}.value")) {
                collection.extra.insert(name.to_string(), json!(value));
            }
        }
        if let Some(locked) = query.get("titleSort.locked") {
            let mut map = collection.extra.clone();
            edit::set_lock(&mut map, "titleSort", locked == "1");
            match map.get("Field") {
                Some(fields) => {
                    collection.extra.insert("Field".to_string(), fields.clone());
                }
                None => {
                    collection.extra.shift_remove("Field");
                }
            }
        }
        if let Some(tags) = label_tags {
            collection
                .extra
                .insert("Label".to_string(), Value::Array(tags));
        }
        if [
            "title",
            "title.value",
            "titleSort.value",
            "titleSort.locked",
        ]
        .iter()
        .any(|name| query.contains_key(*name))
        {
            // Measured: a list's Sort Title follows an item's rule.
            let mut map = Map::new();
            map.insert("title".to_string(), json!(collection.title));
            if let Some(sort) = collection.extra.get("titleSort") {
                map.insert("titleSort".to_string(), sort.clone());
            }
            if let Some(fields) = collection.extra.get("Field") {
                map.insert("Field".to_string(), fields.clone());
            }
            edit::derive_title_sort(&mut map);
            match map.get("titleSort") {
                Some(sort) => {
                    collection
                        .extra
                        .insert("titleSort".to_string(), sort.clone());
                }
                None => {
                    collection.extra.shift_remove("titleSort");
                }
            }
        }
        self.dirty = true;
    }

    /// Cycle 15: the SECTION path's type=18 save renames the members' `Collection`
    /// TAG as well; the tag keeps its ID across the rename, and the new name joins
    /// the tag TABLE with the old one kept in it (cycle 61). A member's LIGHT row keeps
    /// the FIRST name it had (cycle 62).
    fn rename_collection_tag(&mut self, id: &str, title: &str) {
        let Some(collection) = self.collections.get(id).cloned() else {
            return;
        };
        if title.is_empty() {
            return;
        }
        let old = collection.tag_name();
        if title == old {
            return;
        }
        let tag_id_value = self.collection_tag_id(&collection);
        for member in &collection.items {
            let Some(row) = self.catalog.get_mut(member).and_then(Value::as_object_mut) else {
                continue;
            };
            let mut renamed = false;
            if let Some(tags) = row.get_mut("Collection").and_then(Value::as_array_mut) {
                for tag in tags.iter_mut() {
                    if tag.get("tag").and_then(Value::as_str) == Some(old.as_str()) {
                        tag["tag"] = json!(title);
                        renamed = true;
                    }
                }
            }
            if renamed {
                let light = row
                    .entry("_collectionLight".to_string())
                    .or_insert_with(|| json!({}));
                if let Some(light) = light.as_object_mut() {
                    let first = light
                        .shift_remove(&old.to_lowercase())
                        .unwrap_or_else(|| json!(old));
                    light.insert(title.to_lowercase(), first);
                }
            }
        }
        self.tag_table
            .entry(("Collection".to_string(), title.to_lowercase()))
            .or_insert_with(|| {
                json!({"id": tag_id_value, "tag": title, "filter": format!("collection={tag_id_value}")})
            });
        if let Some(stored) = self.collections.get_mut(id) {
            stored
                .extra
                .insert("tagID".to_string(), json!(tag_id_value));
            stored.tag = title.to_string();
            let mut names = stored
                .extra
                .get("_names")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            if !names
                .iter()
                .any(|name| name.as_str() == Some(&title.to_lowercase()))
            {
                names.push(json!(title.to_lowercase()));
            }
            stored
                .extra
                .insert("_names".to_string(), Value::Array(names));
        }
    }

    /// The SECTION path's type=18 save for one collection: the 409 a taken name gets
    /// (cycles 58, 61), the tag rename, the edit, and the `queued`/done frame pair
    /// with the title from BEFORE the write (cycle 36).
    pub(super) fn edit_collection(
        &mut self,
        id: &str,
        edits: &HashMap<String, String>,
    ) -> Response {
        let pairs = edits
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect::<Vec<_>>();
        self.section_collection_save(id, &pairs)
    }

    pub(super) fn section_collection_save(
        &mut self,
        id: &str,
        pairs: &[(String, String)],
    ) -> Response {
        let Some(collection) = self.collections.get(id).cloned() else {
            return Response::empty(200);
        };
        let query = pairs.iter().cloned().collect::<HashMap<_, _>>();
        let wanted = query
            .get("title.value")
            .or_else(|| query.get("title"))
            .cloned();
        if let Some(wanted) = wanted.as_ref().filter(|title| !title.is_empty()) {
            let lowered = wanted.to_lowercase();
            let mut taken = self
                .catalog
                .items
                .iter()
                .filter(|row| {
                    string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                        == collection.section
                })
                .flat_map(|row| {
                    row.get("Collection")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default()
                })
                .filter_map(|tag| tag.get("tag").map(string_value).map(|t| t.to_lowercase()))
                .collect::<HashSet<_>>();
            taken.extend(
                self.tag_table
                    .keys()
                    .filter(|(table, _)| table == "Collection")
                    .map(|(_, lowered)| lowered.clone()),
            );
            let mut own = collection
                .extra
                .get("_names")
                .and_then(Value::as_array)
                .map(|names| {
                    names
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::to_string)
                        .collect::<HashSet<_>>()
                })
                .unwrap_or_default();
            own.insert(collection.title.to_lowercase());
            if taken.contains(&lowered) && !own.contains(&lowered) {
                return Response::error(409, "plexsim: a collection has that name");
            }
        }
        if let Some(title) = &wanted {
            self.rename_collection_tag(id, title);
        }
        let was = self.dynamic_collection_row(&collection);
        if let Some(refusal) = self.collection_edit_refusal(&collection, pairs) {
            return refusal;
        }
        self.write_collection_edit(id, pairs);
        if let Some(after) = self.collections.get(id).cloned() {
            let row = self.dynamic_collection_row(&after);
            self.push_timeline_collection(&was, 3, Some("queued"));
            self.push_timeline_collection(&row, 5, None);
        }
        self.save_if_dirty();
        Response::empty(200)
    }

    /// `DELETE /library/collections/{id}`: every MEMBER queued, every member done,
    /// then the collection's own state 9 with an EMPTY title; the members lose the tag
    /// and the collection goes.
    fn delete_collection(&mut self, id: &str) -> Response {
        let Some(collection) = self.collections.get(id).cloned() else {
            return Response::error(404, "plexsim: not found");
        };
        let members = if collection.smart {
            Vec::new()
        } else {
            self.collection_members(&collection)
        };
        self.collections.remove(id);
        let name = collection.tag_name();
        for key in &collection.items {
            self.untag_collection_member(key, &name);
        }
        for member in &members {
            if let Some(row) = key_of(member).and_then(|key| self.catalog.get(&key).cloned()) {
                self.notify_timeline(&row, 3, Some("queued"), None, None);
            }
        }
        for member in &members {
            if let Some(row) = key_of(member).and_then(|key| self.catalog.get(&key).cloned()) {
                self.notify_timeline(&row, 5, None, None, None);
            }
        }
        let gone = json!({"ratingKey": id, "type": "collection", "title": "",
            "librarySectionID": collection.section});
        self.push_timeline_collection(&gone, 9, Some("deleted"));
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    /// The bare `{size, Metadata: [row]}` a collection write answers with.
    fn collection_row_container(&self, id: &str) -> Response {
        let Some(collection) = self.collections.get(id) else {
            return Response::error(404, "plexsim: not found");
        };
        Response::json(
            200,
            json!({"MediaContainer": {"size": 1, "Metadata": [self.dynamic_collection_row(collection)]}}),
        )
    }

    /// `/library/collections[/{id}[/...]]` (`collections_route`).
    pub(super) fn route_collection_path(
        &mut self,
        segments: &[&str],
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        let method = request.method.as_str();
        if segments.is_empty() {
            if method == "POST" {
                return self.create_collection(query);
            }
            // A real PMS 404s the bare GET: collections are listed per section.
            return Response::error(404, "plexsim: not found");
        }
        let id = segments[0];
        let Some(collection) = self.collections.get(id).cloned() else {
            return Response::error(404, "plexsim: no such collection");
        };
        if !matches!(method, "GET" | "HEAD" | "PUT" | "DELETE") {
            // Cycle 21: POST, PATCH and a bare OPTIONS are 404s here and below.
            return Response::error(404, "plexsim: not found");
        }
        let pairs = Self::request_pairs(request);
        if segments.len() == 1 {
            return match method {
                "DELETE" => self.delete_collection(id),
                "PUT" => {
                    // Cycle 21: this path is the ITEM edit path, with the grammar.
                    if let Some(refusal) = self.collection_edit_refusal(&collection, &pairs) {
                        return refusal;
                    }
                    self.write_collection_edit(id, &pairs);
                    if let Some(after) = self.collections.get(id).cloned() {
                        let row = self.dynamic_collection_row(&after);
                        self.push_timeline_collection(&row, 3, Some("loading"));
                        self.push_timeline_collection(&row, 5, None);
                    }
                    self.save_if_dirty();
                    Response::empty(200)
                }
                _ => {
                    // Measured: this container carries `allowSync` TRUE, the identifier,
                    // the media-tag pair, and a NUMBER librarySectionID.
                    let mut body = Map::new();
                    body.insert("size".to_string(), json!(1));
                    body.insert("allowSync".to_string(), json!(true));
                    body.insert(
                        "identifier".to_string(),
                        json!("com.plexapp.plugins.library"),
                    );
                    body.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
                    body.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
                    body.extend(self.collection_section_fields(&collection));
                    body.insert(
                        "Metadata".to_string(),
                        json!([self.collection_detail(&collection, query)]),
                    );
                    Response::json(200, json!({"MediaContainer": body}))
                }
            };
        }
        let leaf = segments[1];
        match leaf {
            "refresh" | "unmatch" | "analyze" | "prefs" => {
                // Delegated to the metadata path, where the sequences were measured.
                if method != "PUT" {
                    return Response::error(404, "plexsim: not found");
                }
                return self.route_collection_metadata(&collection, &segments[1..], query, request);
            }
            "posters" | "arts" if matches!(method, "GET" | "HEAD") => {
                // Cycle 15: `/posters` lists exactly one candidate -- the collection's
                // own composite, `default://`, selected, no `provider`; `/arts` is empty.
                let mut entries = Vec::new();
                if leaf == "posters" {
                    let composite = format!(
                        "/library/collections/{}/composite/{}",
                        collection.id,
                        collection.stamp()
                    );
                    entries.push(json!({"_element": "Photo", "key": format!("{composite}?width=400&height=600"),
                        "ratingKey": "default://", "thumb": format!("{composite}?width=200&height=300"),
                        "selected": true}));
                }
                return Response::json(
                    200,
                    json!({"MediaContainer": {"size": entries.len(), "identifier": "com.plexapp.plugins.library",
                        "mediaTagPrefix": MEDIA_TAG_PREFIX, "mediaTagVersion": MEDIA_TAG_VERSION,
                        "Metadata": entries}}),
                );
            }
            _ => {}
        }
        if matches!(leaf, "children" | "items") && segments.len() == 2 && method == "GET" {
            // Measured: the members as the LIGHT grid row, in a bare container.
            let rows = self
                .collection_members(&collection)
                .iter()
                .map(|row| self.grid_row(row, true, false, false))
                .collect::<Vec<_>>();
            return Response::json(
                200,
                json!({"MediaContainer": {"size": rows.len(), "Metadata": rows}}),
            );
        }
        if matches!(leaf, "items" | "children") && segments.len() == 3 && method == "DELETE" {
            let member = segments[2];
            if member.is_empty() || !member.bytes().all(|b| b.is_ascii_digit()) {
                // Cycle 17: a ratingKey that is not a NUMBER is a 404.
                return Response::error(404, "plexsim: not found");
            }
            let name = collection.tag_name();
            let removed = self
                .collections
                .get(id)
                .is_some_and(|c| c.items.iter().any(|k| k == member));
            if removed {
                if let Some(stored) = self.collections.get_mut(id) {
                    stored.items.retain(|key| key != member);
                    stored.touch();
                }
                self.untag_collection_member(member, &name);
                self.dirty = true;
            }
            return self.collection_row_container(id);
        }
        if matches!(leaf, "items" | "children") && segments.len() == 4 && segments[3] == "move" {
            if method != "PUT" {
                return Response::error(404, "plexsim: not found");
            }
            let member = segments[2];
            let after = query.get("after");
            if let Some(after) = after {
                let digits = after.strip_prefix('-').unwrap_or(after);
                if after == member
                    || digits.is_empty()
                    || !digits.bytes().all(|b| b.is_ascii_digit())
                {
                    return Response::error(400, "plexsim: bad request");
                }
            }
            if !collection.items.iter().any(|key| key == member) {
                return Response::error(400, "plexsim: bad request");
            }
            // Measured: a move starts from the order SHOWN and switches the collection
            // to its custom order, `collectionSort` 2; an `after=` naming no member
            // moves the row to the FRONT.
            let shown = self
                .collection_members(&collection)
                .iter()
                .filter_map(key_of)
                .collect::<Vec<_>>();
            let mut items = shown.clone();
            items.extend(
                collection
                    .items
                    .iter()
                    .filter(|key| !shown.contains(key))
                    .cloned(),
            );
            items.retain(|key| key != member);
            let position = after
                .and_then(|after| items.iter().position(|key| key == after))
                .map(|index| index + 1)
                .unwrap_or(0);
            items.insert(position, member.to_string());
            if let Some(stored) = self.collections.get_mut(id) {
                stored.items = items;
                stored
                    .prefs
                    .insert("collectionSort".to_string(), "2".to_string());
                stored.touch();
            }
            self.dirty = true;
            return self.collection_row_container(id);
        }
        if leaf == "items" && segments.len() == 2 && method == "PUT" {
            if collection.smart {
                // The Edit-smart-collection Save: the stored query is REPLACED.
                let Some(uri) = query.get("uri").filter(|uri| !uri.is_empty()) else {
                    return Response::error(400, "plexsim: bad request");
                };
                if let Some(stored) = self.collections.get_mut(id) {
                    stored.uri = uri.clone();
                    stored.touch();
                }
                self.dirty = true;
                return self.collection_row_container(id);
            }
            // Cycle 17: a comma-joined uri tolerates members it cannot use; only a
            // single-key uri of the wrong type (or none at all) is a 400.
            let rows = self.rows_for_uri(query.get("uri").map(String::as_str).unwrap_or(""));
            let usable = rows
                .iter()
                .filter(|row| {
                    row.get("type").and_then(Value::as_str) == Some(collection.subtype.as_str())
                })
                .filter_map(key_of)
                .collect::<Vec<_>>();
            if rows.is_empty() || (rows.len() == 1 && usable.is_empty()) {
                return Response::error(400, "plexsim: bad request");
            }
            self.add_collection_items(id, &usable);
            self.dirty = true;
            self.save_if_dirty();
            return self.collection_row_container(id);
        }
        if leaf == "composite" {
            return self.route_collection_metadata(&collection, &segments[1..], query, request);
        }
        Response::json(200, json!({"MediaContainer": {"size": 0}}))
    }

    /// `/library/metadata/{collection}[/...]` -- the collection branch of
    /// `library_list_item`.
    pub(super) fn route_collection_metadata(
        &mut self,
        collection: &Collection,
        tail: &[&str],
        query: &HashMap<String, String>,
        request: &Request,
    ) -> Response {
        let method = request.method.as_str();
        let id = collection.id.clone();
        if !matches!(method, "GET" | "HEAD" | "PUT" | "DELETE")
            && !(method == "POST" && tail.len() == 1 && matches!(tail[0], "posters" | "arts"))
        {
            return Response::error(404, "plexsim: not found");
        }
        let pairs = Self::request_pairs(request);
        if tail.len() == 1 && tail[0] == "prefs" {
            if method != "PUT" {
                return Response::error(404, "plexsim: not found");
            }
            if collection.smart && pairs.iter().any(|(name, _)| name == "collectionSort") {
                // Cycle 15: a SMART collection refuses `collectionSort` with a 400.
                return Response::error(400, "plexsim: bad request");
            }
            let mut accepted = vec!["collectionSort", "collectionMode"];
            if collection.smart {
                accepted.push("collectionFilterBasedOnUser");
            }
            let writes = pairs
                .iter()
                .filter(|(name, _)| !name.starts_with("X-Plex-"))
                .cloned()
                .collect::<Vec<_>>();
            if writes
                .iter()
                .any(|(name, _)| !accepted.contains(&name.as_str()))
            {
                return Response::error(400, "plexsim: bad request");
            }
            if let Some(stored) = self.collections.get_mut(&id) {
                for (name, value) in writes {
                    if name == "collectionFilterBasedOnUser" {
                        stored.prefs.insert(name, value);
                    } else if value.is_empty() {
                        stored.prefs.remove(&name);
                    } else {
                        stored.prefs.insert(name, value);
                    }
                }
            }
            self.dirty = true;
            self.save_if_dirty();
            return Response::empty(200);
        }
        if tail.len() == 1 && matches!(tail[0], "thumbs" | "banners") {
            if !matches!(method, "GET" | "HEAD") {
                return Response::error(404, "plexsim: not found");
            }
            return Response::json(200, self.tag_container(Map::new()));
        }
        if tail.len() == 1 && tail[0] == "tree" {
            if !matches!(method, "GET" | "HEAD") {
                return Response::error(404, "plexsim: not found");
            }
            let key = id
                .parse::<i64>()
                .map(|id| json!(id))
                .unwrap_or_else(|_| json!(id));
            return Response::json(
                200,
                json!({"MediaContainer": {"size": 1, "MetadataItem": [{"id": key}]}}),
            );
        }
        if tail.len() == 1 && matches!(tail[0], "posters" | "arts") {
            if method == "POST" {
                if request.body.is_empty() {
                    return Response::error(406, "plexsim: not acceptable");
                }
                return self.list_artwork_response(&id, true, tail[0], request);
            }
            if !matches!(method, "GET" | "HEAD") {
                return Response::error(404, "plexsim: not found");
            }
            let mut entries = Vec::new();
            if tail[0] == "posters" {
                let composite = format!(
                    "/library/collections/{}/composite/{}",
                    id,
                    collection.stamp()
                );
                entries.push(
                    json!({"_element": "Photo", "key": format!("{composite}?width=400&height=600"),
                    "ratingKey": "default://", "thumb": format!("{composite}?width=200&height=300"),
                    "selected": true}),
                );
            }
            if let Some((upload, _)) = self.list_artwork.get(&id) {
                for entry in &mut entries {
                    if let Some(object) = entry.as_object_mut() {
                        object.shift_remove("selected");
                    }
                }
                entries.push(
                    json!({"_element": "Photo", "key": upload, "ratingKey": upload,
                    "thumb": upload, "selected": true}),
                );
            }
            return Response::json(
                200,
                self.tag_container(Map::from_iter([(
                    "Metadata".to_string(),
                    Value::Array(entries),
                )])),
            );
        }
        if tail.len() == 1 && tail[0] == "children" {
            // Cycle 14/17: a full secondary container with LIGHT grid rows, the
            // librarySection triple on the CONTAINER, `allowSync: true`, no thumb.
            if !matches!(method, "GET" | "HEAD") {
                return Response::error(404, "plexsim: not found");
            }
            let section = self
                .section(&collection.section)
                .cloned()
                .unwrap_or(Value::Null);
            let rows = self
                .collection_members(collection)
                .iter()
                .map(|row| self.grid_row(row, false, false, false))
                .collect::<Vec<_>>();
            let view_group = if collection.subtype.is_empty() {
                section.get("type").cloned().unwrap_or(Value::Null)
            } else {
                json!(collection.subtype)
            };
            let mut container = self.axis_container(
                &section,
                Some(&collection.title),
                Map::from_iter([
                    ("Metadata".to_string(), Value::Array(rows)),
                    ("viewGroup".to_string(), view_group),
                ]),
            );
            if let Some(body) = container
                .get_mut("MediaContainer")
                .and_then(Value::as_object_mut)
            {
                body.shift_remove("content");
                body.shift_remove("thumb");
                body.insert("key".to_string(), json!(id));
                body.insert("nocache".to_string(), json!(true));
                body.insert(
                    "parentIndex".to_string(),
                    json!(self.collection_tag_id(collection)),
                );
                body.insert("parentTitle".to_string(), json!(collection.title));
                body.insert("allowSync".to_string(), json!(true));
                body.extend(self.collection_section_fields(collection));
            }
            return Response::json(200, container);
        }
        if tail.len() == 1 && matches!(tail[0], "clearLogos" | "themes") {
            return Response::json(200, self.tag_container(Map::new()));
        }
        if tail.len() == 1 && tail[0] == "composite" || tail.first() == Some(&"composite") {
            if !matches!(method, "GET" | "HEAD") {
                return Response::error(404, "plexsim: not found");
            }
            return Response::image(jpeg_placeholder());
        }
        if tail.len() == 1
            && matches!(tail[0], "poster" | "art" | "thumb")
            && matches!(method, "GET" | "HEAD")
        {
            // Cycle 52: a collection with no uploaded artwork owns none of these.
            return Response::error(404, "plexsim: not found");
        }
        if tail.len() == 1
            && matches!(
                tail[0],
                "analyze"
                    | "match"
                    | "extras"
                    | "related"
                    | "allLeaves"
                    | "grandchildren"
                    | "refresh"
                    | "unmatch"
                    | "poster"
                    | "art"
                    | "thumb"
            )
        {
            let leaf = tail[0];
            if matches!(leaf, "poster" | "art" | "thumb") {
                if method == "PUT" && leaf != "thumb" {
                    return self.set_collection_artwork(&id, leaf, query);
                }
                return Response::error(404, "plexsim: not found");
            }
            if !matches!(method, "GET" | "HEAD" | "PUT") {
                return Response::error(404, "plexsim: not found");
            }
            if leaf == "match" || leaf == "extras" {
                return Response::error(400, "plexsim: bad request");
            }
            if leaf == "analyze" {
                // Cycle 23: the activity PAIR and nothing else.
                let mut activity = self.add_activity(
                    &collection.title,
                    "Generating video chapter thumbnails",
                    "media.generate.chapter.thumbs",
                    -1,
                );
                activity["progress"] = json!(100);
                self.end_activity_quietly(&mut activity);
                return Response::empty(200);
            }
            if leaf == "refresh" || leaf == "unmatch" {
                // Cycle 23/26: three frames while it carries an agent guid, eight once
                // an unmatch gave it `local://`; an unmatch sets `local://`, drops
                // `ratingCount` and makes the subtype `unknown`.
                let row = self.dynamic_collection_row(collection);
                let local = row
                    .get("guid")
                    .and_then(Value::as_str)
                    .is_some_and(|guid| guid.starts_with("local://"));
                self.push_timeline_collection(&row, 2, Some("matching"));
                if leaf == "refresh" {
                    self.push_timeline_collection(&row, 5, None);
                }
                self.push_timeline_collection(&row, 3, Some("queued"));
                if local || leaf == "unmatch" {
                    let mut activity = self.add_metadata_activity(&collection.title);
                    activity["progress"] = json!(49);
                    self.push_activity(&activity, "updated");
                    self.push_timeline_collection(&row, 3, Some("processing"));
                    activity["progress"] = json!(100);
                    self.end_activity_quietly(&mut activity);
                    self.push_timeline_collection(&row, 5, None);
                }
                if leaf == "unmatch" {
                    if let Some(stored) = self.collections.get_mut(&id) {
                        stored
                            .prefs
                            .insert("_unmatched".to_string(), "1".to_string());
                    }
                    self.dirty = true;
                }
                return Response::empty(200);
            }
            if leaf == "related" {
                let mut body = Map::new();
                body.insert("size".to_string(), json!(0));
                body.insert("allowSync".to_string(), json!(true));
                body.insert(
                    "identifier".to_string(),
                    json!("com.plexapp.plugins.library"),
                );
                body.extend(self.collection_section_fields(collection));
                body.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
                body.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
                return Response::json(200, json!({"MediaContainer": body}));
            }
            // Cycle 41: allLeaves and grandchildren are real secondary containers but
            // neither is a member view; both stay empty.
            let section = self
                .section(&collection.section)
                .cloned()
                .unwrap_or(Value::Null);
            let mut body = Map::new();
            body.insert("size".to_string(), json!(0));
            body.insert("allowSync".to_string(), json!(true));
            body.insert("art".to_string(), json!("/:/resources/movie-fanart.jpg"));
            body.insert(
                "identifier".to_string(),
                json!("com.plexapp.plugins.library"),
            );
            body.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
            body.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
            body.insert(
                "title1".to_string(),
                section.get("title").cloned().unwrap_or(Value::Null),
            );
            body.insert("viewGroup".to_string(), json!("movie"));
            body.insert("viewMode".to_string(), json!(65592));
            if leaf == "allLeaves" {
                body.insert("key".to_string(), json!(id));
                body.extend(self.collection_section_fields(collection));
                body.insert("mixedParents".to_string(), json!(true));
                body.insert("nocache".to_string(), json!(true));
                body.insert(
                    "parentIndex".to_string(),
                    json!(self.collection_tag_id(collection)),
                );
                body.insert("parentTitle".to_string(), json!(collection.title));
                body.insert("title2".to_string(), json!(collection.title));
            } else {
                body.shift_remove("allowSync");
            }
            return Response::json(200, json!({"MediaContainer": body}));
        }
        if !tail.is_empty() {
            return Response::error(404, "plexsim: not found");
        }
        if method == "PUT" {
            if let Some(refusal) = self.collection_edit_refusal(collection, &pairs) {
                return refusal;
            }
            self.write_collection_edit(&id, &pairs);
            if let Some(after) = self.collections.get(&id).cloned() {
                let row = self.dynamic_collection_row(&after);
                self.push_timeline_collection(&row, 3, Some("loading"));
                self.push_timeline_collection(&row, 5, None);
            }
            self.save_if_dirty();
            return Response::empty(200);
        }
        if method == "DELETE" {
            return self.delete_collection(&id);
        }
        // Measured on fun: the library container any item's detail carries.
        let mut body = Map::new();
        body.insert("size".to_string(), json!(1));
        body.insert("allowSync".to_string(), json!(true));
        body.insert(
            "identifier".to_string(),
            json!("com.plexapp.plugins.library"),
        );
        body.extend(self.collection_section_fields(collection));
        body.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
        body.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
        body.insert(
            "Metadata".to_string(),
            json!([self.collection_detail(collection, query)]),
        );
        Response::json(200, json!({"MediaContainer": body}))
    }

    /// A library's collections as rows, sorted by titleSort and filtered by the
    /// listing grammar -- the `type=18` listing (cycle 14) and the `/collections`
    /// leaf (cycle 28).
    pub(super) fn section_collection_rows(
        &self,
        section_key: &str,
        pairs: &[(String, String)],
        sort: Option<&str>,
    ) -> Result<Vec<Value>, u16> {
        let rows = self
            .collections
            .values()
            .filter(|collection| collection.section == section_key)
            .map(|collection| self.dynamic_collection_row(collection))
            .collect::<Vec<_>>();
        let section_type = self
            .section(section_key)
            .and_then(|section| section.get("type").and_then(Value::as_str))
            .unwrap_or("movie")
            .to_string();
        let rows = self.filter_rows(rows, pairs, &section_type)?;
        self.sort_rows(
            rows,
            Some(sort.filter(|sort| !sort.is_empty()).unwrap_or("titleSort")),
        )
    }

    /// Stamp each member's `Collection` tag with the collection's own guid and drop
    /// any `count` (cycle 15), at projection time.
    pub(super) fn stamp_collection_tags(&self, key: &str, object: &mut Map<String, Value>) {
        let Some(tags) = object.get_mut("Collection").and_then(Value::as_array_mut) else {
            return;
        };
        for tag in tags.iter_mut() {
            let Some(tag_object) = tag.as_object_mut() else {
                continue;
            };
            tag_object.shift_remove("count");
            let name = tag_object.get("tag").map(string_value).unwrap_or_default();
            if let Some(collection) = self.collections.values().find(|collection| {
                !collection.smart
                    && collection.tag_name() == name
                    && collection.items.iter().any(|item| item == key)
            }) {
                tag_object.insert(
                    "guid".to_string(),
                    json!(format!("collection://plexsim-{}", collection.id)),
                );
            }
        }
    }

    /// `PUT /library/metadata/{collection}/{poster,art}?url=` -- the pick on a list.
    /// Measured: the answer is the new versioned path, the field is locked, the row's
    /// `updatedAt` moves, and ONE `state 5` frame goes out carrying the list's own type
    /// (cycle 22: 18 for a collection).
    fn set_collection_artwork(
        &mut self,
        id: &str,
        leaf: &str,
        query: &HashMap<String, String>,
    ) -> Response {
        if query.get("url").is_none_or(|url| url.is_empty()) {
            return Response::error(400, "plexsim: no url");
        }
        let field = if leaf == "poster" { "thumb" } else { "art" };
        let stamp = now();
        let path = format!("/library/metadata/{id}/{field}/{stamp}");
        let Some(collection) = self.collections.get_mut(id) else {
            return Response::error(404, "plexsim: not found");
        };
        collection.extra.insert(field.to_string(), json!(path));
        collection
            .extra
            .insert("updatedAt".to_string(), json!(stamp));
        let mut map = collection.extra.clone();
        edit::set_lock(&mut map, field, true);
        if let Some(fields) = map.get("Field") {
            collection.extra.insert("Field".to_string(), fields.clone());
        }
        if let Some(stored) = self.collections.get(id).cloned() {
            let row = self.dynamic_collection_row(&stored);
            self.push_timeline_collection(&row, 5, None);
        }
        self.dirty = true;
        self.save_if_dirty();
        let mut response = Response::bytes(200, "text/html");
        response.body = path.into_bytes();
        response
    }
}
