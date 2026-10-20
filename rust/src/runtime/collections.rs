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
pub(super) const LIST_STATE: &[&str] = &[
    "userRating",
    "viewOffset",
    "viewCount",
    "lastViewedAt",
    "lastRatedAt",
];

/// The settings `PUT /library/metadata/{collection}/prefs` stores.
const COLLECTION_PREFS: [&str; 3] = ["collectionMode", "collectionSort", "collectionFilterBasedOnUser"];

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

    /// The collection's guid. Cycle 89: it is its TAG's, so a collection created under a
    /// deleted one's name carries the deleted one's; one stored before cycle 89 has none
    /// of its own and was always minted from its id.
    pub(super) fn guid(&self) -> String {
        self.extra
            .get("guid")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| format!("collection://rex-{}", self.id))
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
            // Read at the COLLECTION's level, as a create reads its uri: plexscratch42
            // (1.42.2) answers a smart show collection over
            // `all?type=2&episode.unwatched=1&limit=3` with its SHOWS, and the uri's
            // own `type=` is the collection's business, not the player's.
            return self.collection_uri_rows(
                &collection.uri,
                &collection.section,
                member_type_number(&collection.subtype),
                &collection.subtype,
                false,
            );
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

    /// How many rows a collection lists and their years, sorted: a plain collection's read off
    /// its members' hot fields, where copying every member row was a whole-library collection's
    /// 40,000 rows for each Collections tab row and each member's preplay.
    fn member_counts(&self, collection: &Collection) -> (usize, Vec<String>) {
        let mut years = Vec::new();
        let mut count = 0;
        let mut take = |year: Option<&Value>| {
            count += 1;
            if let Some(year) = year.filter(|value| truthy(value)) {
                years.push(py_str(year));
            }
        };
        if collection.smart {
            for row in self.collection_members(collection) {
                take(row.get("year"));
            }
        } else {
            for row in collection.items.iter().filter_map(|key| self.catalog.lazy_get(key)) {
                take(row.get("year"));
            }
        }
        years.sort();
        (count, years)
    }

    /// The row a real PMS answers for a collection (`_collection_row`).
    pub(super) fn dynamic_collection_row(&self, collection: &Collection) -> Value {
        let (count, mut years) = self.member_counts(collection);
        // Cycle 87: a merge's stale counts first (`freeze_merged_collections`), then an
        // emptied collection's last ones (`keep_emptied_counts`).
        let emptied = collection
            .extra
            .get("_frozenCounts")
            .cloned()
            .or_else(|| {
                (count == 0)
                    .then(|| collection.extra.get("_emptiedCounts").cloned())
                    .flatten()
            });
        if let Some(emptied) = &emptied {
            years = emptied
                .get("years")
                .and_then(Value::as_array)
                .map(|years| years.iter().map(string_value).collect())
                .unwrap_or_default();
        }
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
                collection.guid()
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
        // Cycle 88, measured on plexscratch42: once a poster is UPLOADED (or picked) the
        // collection's `thumb` is its own versioned `/library/metadata/{id}/thumb/{v}` on
        // every read, the composite only before that.
        let own_thumb = collection
            .extra
            .get("thumb")
            .map(string_value)
            .filter(|thumb| thumb.starts_with(&format!("/library/metadata/{}/thumb/", collection.id)));
        row.insert(
            "thumb".to_string(),
            json!(own_thumb.clone().unwrap_or_else(|| format!(
                "/library/collections/{}/composite/{stamp}?width=400&height=600",
                collection.id
            ))),
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
        // Cycle 83, plexscratch42 (1.42.2): an EMPTY collection carries no `childCount`
        // on any of its rows (a one-member one says 1); this sent 0.
        if let Some(count) = emptied
            .as_ref()
            .and_then(|emptied| emptied.get("childCount"))
        {
            row.insert("childCount".to_string(), count.clone());
        } else if count > 0 {
            row.insert("childCount".to_string(), json!(count));
        }
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
        if let Some(thumb) = &own_thumb {
            // Cycle 88 (plexscratch42): an own poster is also the row's `coverPoster`
            // `Image`, on the detail reads and the listing alike, ahead of its tags.
            row.insert(
                "Image".to_string(),
                json!([{"alt": collection.title, "type": "coverPoster", "url": thumb}]),
            );
        }
        // Cycle 88: the detail reads carry `Label` then `Field`.
        for name in ["Label", "Field"] {
            if let Some(value) = collection.extra.get(name).filter(|value| !value.is_null()) {
                row.insert(name.to_string(), value.clone());
            }
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
            .or_insert_with(("Collection".to_string(), lowered), || minted.clone());
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

    /// Remember the counts a plain collection had when its LAST member leaves.
    ///
    /// Cycle 87, measured on plexscratch42 (1.42.2;
    /// `tests/fixtures/collection_emptied_s42.json`): emptied one member at a time --
    /// `DELETE .../items/{id}` or the member's `collection[].tag.tag-` edit -- a
    /// collection keeps the `childCount`, `minYear` and `maxYear` it had with one member
    /// left, on the DELETE's answer and every read, while `/children` lists nothing. The
    /// reference stops recomputing the aggregate at zero; one CREATED empty never had one
    /// (cycle 83). This dropped all three.
    pub(super) fn keep_emptied_counts(&mut self, id: &str, members: &[Value]) {
        let Some(collection) = self.collections.get(id).cloned() else {
            return;
        };
        if collection.smart {
            return;
        }
        let after = self.member_counts(&collection).0;
        if after == members.len() {
            return;
        }
        // Cycle 87 B2 (plexscratch 1.43.4, `tests/fixtures/collection_merged_s43.json`): a
        // removal after a merge recounts from the real members -- unless it empties the
        // collection, which then keeps the stale counts the merge left (2, 2006-2015 over
        // what had become one 2015 member), not the one member's.
        let Some(stored) = self.collections.get_mut(id) else {
            return;
        };
        let frozen = stored.extra.shift_remove("_frozenCounts");
        if !members.is_empty() && after == 0 {
            stored.extra.insert(
                "_emptiedCounts".to_string(),
                frozen.unwrap_or_else(|| collection_counts(members)),
            );
        }
    }

    /// A merge does not recount the collections it takes a member from.
    ///
    /// Cycle 87 B2, measured on plexscratch (1.43.4; `tests/fixtures/collection_merged_s43.json`,
    /// E4's `merge_playlist_s43.json` beside it): `PUT /library/metadata/18/merge?ids=4` on a
    /// collection whose only member was 4 left it reading `childCount` 1 and 4's year, at once
    /// and 20 s later, while `/children` listed nothing; `merge?ids=17` on a collection of 17
    /// and 18 listed 18 once and still read 2 and both years. The aggregate stays as it was
    /// until the next membership write recounts it. This pruned the key and recounted live,
    /// so the first lost its `childCount` and the second read 1.
    pub(super) fn freeze_merged_collections(&mut self, key: &str) {
        let held = self
            .collections
            .values()
            .filter(|collection| {
                !collection.smart
                    && !collection.extra.contains_key("_frozenCounts")
                    && collection.items.iter().any(|item| item == key)
            })
            .cloned()
            .collect::<Vec<_>>();
        for collection in held {
            let (count, years) = self.member_counts(&collection);
            let counts = json!({"childCount": count, "years": years});
            if let Some(stored) = self.collections.get_mut(&collection.id) {
                stored.extra.insert("_frozenCounts".to_string(), counts);
            }
        }
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
                // Cycle 87 B2: the add after a merge recounts (1, 2010 for one 2010 movie).
                stored.extra.shift_remove("_frozenCounts");
            }
        }
        added
    }

    /// `(type number, subtype)` of a collection created with `type=raw`, or None for
    /// the 400. Measured on plexscratch42 (1.42.2): `type` is an INTEGER -- `abc`,
    /// `2.0` and an empty value are each a 400, `02` and `+2` are 2 -- and it NAMES THE
    /// MEMBERS' TYPE whatever the uri holds. With no `type`, or a negative one, the
    /// collection takes its LIBRARY's type: `show` in a show library, with or without
    /// a uri. The subtype used to be read off the first row the uri resolved to, with
    /// `movie` as the fallback, so an empty collection in a show library was a movie
    /// one. The names are measured over 0-21, 42, 99-101 and 1000 (a number not listed
    /// was `unknown`); only 1-4 hold rows.
    fn collection_level(&self, section: &str, raw: Option<&str>) -> Option<(i64, String)> {
        let number = match raw {
            None => -1,
            Some(raw) => {
                let digits = raw.strip_prefix(['+', '-']).unwrap_or(raw);
                if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
                    return None;
                }
                // Past i64 the value is still a number; no library holds its type.
                raw.trim_start_matches('+').parse::<i64>().unwrap_or(i64::MAX)
            }
        };
        if number < 0 {
            let kind = self
                .section(section)
                .and_then(|value| value.get("type"))
                .and_then(Value::as_str)
                .unwrap_or("movie")
                .to_string();
            let number = match kind.as_str() {
                "show" => 2,
                "season" => 3,
                "episode" => 4,
                "artist" => 8,
                "photo" => 13,
                _ => 1,
            };
            return Some((number, kind));
        }
        let subtype = match number {
            1 | 101 => "movie",
            2 => "show",
            3 => "season",
            4 => "episode",
            5 => "trailer",
            8 => "artist",
            9 => "album",
            10 => "track",
            12 => "clip",
            13 | 14 => "photo",
            15 | 42 => "playlist",
            16 => "directory",
            18 => "collection",
            20 => "author",
            21 => "audiobook",
            _ => "unknown",
        };
        Some((number, subtype.to_string()))
    }

    /// The rows a collection of `subtype` members in `section` takes from a uri.
    /// Measured on plexscratch42 (`tests/fixtures/collection_section_uri.json`). A
    /// SECTION uri is read at the COLLECTION's level, not the uri's and not the
    /// player's: `type=2` over `/library/sections/3/all` holds the library's two shows
    /// (`subtype: show`, `childCount: 2`), `type=3` its four seasons and `type=4` its
    /// twelve episodes, and the uri's own `type=` is ignored -- `type=2` over
    /// `all?type=4` is still the two shows -- while its filters apply. The native
    /// server listed the shows whatever the collection's type, and the oracle expanded
    /// them into episodes as a play queue does.
    ///
    /// A create keeps only rows of the collection's type in the collection's OWN
    /// library: `type=4` over a show's key and `type=2` over an episode's are empty
    /// collections of the named type, and a key or a section of another library adds
    /// nothing.
    fn collection_uri_rows(
        &self,
        uri: &str,
        section: &str,
        number: i64,
        subtype: &str,
        create: bool,
    ) -> Vec<Value> {
        self.collection_uri_refs(uri, section, number, subtype, create).into_iter().map(AnyRow::into_value).collect()
    }

    /// `collection_uri_rows`, the catalog's rows read in place (`uri_rows_at`).
    fn collection_uri_refs(
        &self,
        uri: &str,
        section: &str,
        number: i64,
        subtype: &str,
        create: bool,
    ) -> Vec<AnyRow<'_>> {
        let decoded = percent_decode(uri);
        let named = decoded.find("/library/sections/").and_then(|start| {
            let tail = &decoded[start + "/library/sections/".len()..];
            let path = tail.split('?').next().unwrap_or("");
            let mut parts = path.split('/');
            let id = parts.next().filter(|id| !id.is_empty())?;
            (parts.next() == Some("all")).then(|| id.to_string())
        });
        // `rows_for_uri` reads a metadata key before a section, so a uri is a section
        // one here only when it names no key.
        if let Some(named) = named.filter(|_| metadata_keys_from_uri(uri).is_empty()) {
            if create && named != section {
                return Vec::new();
            }
            return self.uri_rows_at(uri, Some(number));
        }
        let rows = self.uri_rows_at(uri, None);
        if !create {
            return rows;
        }
        rows.into_iter()
            .filter(|row| {
                row.get("type").and_then(Value::as_str) == Some(subtype)
                    && row.get("librarySectionID").map(string_value).as_deref() == Some(section)
            })
            .collect()
    }

    /// `POST /library/collections?type=&title=&smart=&sectionId=&uri=`. Measured: a
    /// `title` and a `sectionId` are required (400 each); a collection over a show
    /// holds the SHOW; the members' tag is the title with its first ASCII letter
    /// capitalised unless the tag TABLE already spells it (cycle 61); a create pushes
    /// one type-18 `created` frame, and its RESPONSE carries no librarySection fields.
    pub(super) fn create_collection(&mut self, query: &HashMap<String, String>) -> Response {
        let Some(title) = query.get("title") else {
            return Response::error(400, "rex: bad request");
        };
        let Some(section) = query.get("sectionId").filter(|value| !value.is_empty()) else {
            return Response::error(400, "rex: bad request");
        };
        let uri = query.get("uri").cloned().unwrap_or_default();
        let Some((number, subtype)) =
            self.collection_level(section, query.get("type").map(String::as_str))
        else {
            return Response::error(400, "rex: bad request");
        };
        let (count, keys) = {
            let rows = self.collection_uri_refs(&uri, section, number, &subtype, true);
            (rows.len(), rows.iter().filter_map(key_of).collect::<Vec<_>>())
        };
        let smart = query.get("smart").map(String::as_str) == Some("1");
        let id = self.next_collection.to_string();
        self.next_collection += 1;
        let mut title = if title.is_empty() {
            "Untitled".to_string()
        } else {
            title.clone()
        };
        // Cycle 89, measured on plexscratch42 (1.42.2),
        // `tests/fixtures/collection_recreate_s42.json`: a collection's guid lives on its
        // TAG and the rating and prefs are stored against the guid, so one created under
        // a DELETED collection's name -- ignoring case, whatever its library -- takes that
        // name's spelling as its title, its guid, and the userRating and lastRatedAt from
        // the first read; its old prefs come back with the next prefs write. Both servers
        // minted a new guid and brought nothing back.
        let previous = self.collection_guids.get(&title.to_lowercase()).cloned();
        if let Some(spelling) = previous
            .as_ref()
            .and_then(|previous| previous.get("tag"))
            .and_then(Value::as_str)
        {
            title = spelling.to_string();
        }
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
        match &previous {
            Some(previous) => {
                extra.insert(
                    "guid".to_string(),
                    previous.get("guid").cloned().unwrap_or(Value::Null),
                );
                for (name, value) in previous
                    .get("state")
                    .and_then(Value::as_object)
                    .into_iter()
                    .flatten()
                {
                    extra.insert(name.clone(), value.clone());
                }
                if let Some(prefs) = previous
                    .get("prefs")
                    .and_then(Value::as_object)
                    .filter(|prefs| !prefs.is_empty())
                {
                    extra.insert("_inheritedPrefs".to_string(), Value::Object(prefs.clone()));
                }
            }
            None => {
                extra.insert(
                    "guid".to_string(),
                    json!(format!("collection://rex-{id}")),
                );
            }
        }
        let mut stored = Collection {
            id: id.clone(),
            title: title.clone(),
            items: Members::default(),
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
            self.add_collection_items(&id, &keys);
        }
        let collection = self.collections.get(&id).cloned().expect("just inserted");
        info!(
            "COLLECTION create {} smart={} ({} item(s))",
            collection.title,
            if smart { "True" } else { "False" },
            count
        );
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
    pub(super) fn push_timeline_collection(&mut self, row: &Value, state: i64, metadata_state: Option<&str>) {
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
        let key = row.get("ratingKey").map(string_value).unwrap_or_default();
        let at = entry.get("updatedAt").and_then(Value::as_i64).unwrap_or(0);
        self.record_section_timeline(row, &key, state, at);
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
            Err(error) => Some(Response::error(
                error.status,
                &format!("rex: {}", error.detail),
            )),
            Ok(_) => None,
        }
    }

    /// The collection half of an edit: title, summary, the sort title and content
    /// rating, a sort-title lock and Labels (`library_list_item` PUT).
    fn write_collection_edit(&mut self, id: &str, pairs: &[(String, String)], section: bool) {
        let query = pairs.iter().cloned().collect::<HashMap<_, _>>();
        let title = query
            .get("title.value")
            .or_else(|| query.get("title"))
            .cloned();
        let summary = query
            .get("summary.value")
            .or_else(|| query.get("summary"))
            .cloned();
        // Cycle 88, measured on plexscratch42 through all three save paths
        // (`tests/fixtures/collection_edit_grammar_s42.json`): a collection's labels take
        // the ITEM grammar -- on the SECTION path the indexed list MERGES (deduplicated
        // ignoring case, the collection's other labels after it) and `label[].tag.tag-`
        // removes what it names; on the item and collections paths the list REPLACES and
        // any removal clears it. rex replaced on every path and ignored removals.
        let mut adding: Vec<(i64, usize, String)> = Vec::new();
        let mut removing: Vec<String> = Vec::new();
        for (position, (name, value)) in pairs.iter().enumerate() {
            if name.split('[').next().unwrap_or_default().to_lowercase() != "label"
                || name.ends_with(".locked")
            {
                continue;
            }
            if name.contains(".tag.tag-") {
                removing.extend(
                    value
                        .split(',')
                        .filter(|part| !part.is_empty())
                        .map(percent_decode),
                );
            } else if name.contains(".tag.tag") {
                adding.push((super::edit::tag_order(name, position), position, value.clone()));
            }
        }
        adding.sort_by(|a, b| (a.0, a.1).cmp(&(b.0, b.1)));
        let wanted = adding
            .into_iter()
            .map(|(_, _, value)| value)
            .filter(|value| !value.is_empty())
            .collect::<Vec<_>>();
        let labels: Option<Vec<String>> = if wanted.is_empty() && removing.is_empty() {
            None
        } else if section {
            let gone = removing
                .iter()
                .map(|name| name.to_lowercase())
                .collect::<HashSet<_>>();
            let mut first = Vec::new();
            let mut seen = HashSet::new();
            for value in &wanted {
                if seen.insert(value.to_lowercase()) {
                    first.push(value.clone());
                }
            }
            if first.is_empty() && gone.is_empty() {
                None
            } else {
                let kept = self
                    .collections
                    .get(id)
                    .and_then(|collection| collection.extra.get("Label"))
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(|tag| tag.get("tag").and_then(Value::as_str))
                    .filter(|tag| {
                        let lowered = tag.to_lowercase();
                        !seen.contains(&lowered) && !gone.contains(&lowered)
                    })
                    .map(str::to_string)
                    .collect::<Vec<_>>();
                first.extend(kept);
                Some(first)
            }
        } else {
            let mut exact = Vec::new();
            for value in wanted {
                if !exact.contains(&value) {
                    exact.push(value);
                }
            }
            Some(exact)
        };
        let label_tags = labels.map(|labels| {
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
                    .or_insert_with(("Label".to_string(), lowered), || tag.clone());
                minted.push(tag);
            }
            minted
        });
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
        // Cycle 88: every `.locked` column lands in `Field` (rex kept a `titleSort`
        // lock alone), parsed as the path parses it.
        let locks = pairs
            .iter()
            .filter_map(|(name, value)| {
                let field = name.strip_suffix(".locked")?;
                if field == "titleSort" || !edit::records_lock(field) {
                    return None;
                }
                let locked = if section {
                    !value.is_empty()
                        && value.bytes().all(|b| b.is_ascii_digit())
                        && value.trim_start_matches('0') == "1"
                } else {
                    value == "1"
                };
                Some((field.to_string(), locked))
            })
            .collect::<Vec<_>>();
        if query.contains_key("titleSort.locked") || !locks.is_empty() {
            let mut map = collection.extra.clone();
            if let Some(locked) = query.get("titleSort.locked") {
                edit::set_lock(&mut map, "titleSort", locked == "1");
            }
            for (field, locked) in &locks {
                edit::set_lock(&mut map, field, *locked);
            }
            match map.get("Field") {
                Some(fields) => {
                    collection.extra.insert("Field".to_string(), fields.clone());
                }
                None => {
                    collection.extra.shift_remove("Field");
                }
            }
        }
        match label_tags {
            Some(tags) if !tags.is_empty() => {
                collection
                    .extra
                    .insert("Label".to_string(), Value::Array(tags));
            }
            Some(_) => {
                collection.extra.shift_remove("Label");
            }
            None => {}
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
            .or_insert_with(("Collection".to_string(), title.to_lowercase()), || {
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
                .hot()
                .lazy()
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
                return Response::error(409, "rex: a collection has that name");
            }
        }
        if let Some(title) = &wanted {
            self.rename_collection_tag(id, title);
        }
        let was = self.dynamic_collection_row(&collection);
        if let Some(refusal) = self.collection_edit_refusal(&collection, pairs) {
            return refusal;
        }
        self.write_collection_edit(id, pairs, true);
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
            return Response::error(404, "rex: not found");
        };
        let members = if collection.smart {
            Vec::new()
        } else {
            self.collection_members(&collection)
        };
        self.collections.remove(id);
        // Cycle 89: the tag outlives the collection, and with it the guid and what is
        // stored against the guid -- a later create under this name takes them back.
        let state = ["userRating", "lastRatedAt"]
            .iter()
            .filter_map(|name| {
                collection
                    .extra
                    .get(*name)
                    .filter(|value| !value.is_null())
                    .map(|value| (name.to_string(), value.clone()))
            })
            .collect::<Map<String, Value>>();
        let prefs = COLLECTION_PREFS
            .iter()
            .filter_map(|name| {
                collection
                    .prefs
                    .get(*name)
                    .map(|value| (name.to_string(), json!(value)))
            })
            .collect::<Map<String, Value>>();
        self.collection_guids.insert(
            collection.tag_name().to_lowercase(),
            json!({"tag": collection.tag_name(), "guid": collection.guid(),
                "state": state, "prefs": prefs}),
        );
        // Cycle 88 B3, measured alike on plexscratch42 (1.42.2) and plexscratch (1.43.4),
        // `tests/fixtures/collection_delete_by_title_s4{2,3}.json`: the delete untags the
        // members by the collection's CURRENT title, ignoring case -- not by the tag they
        // carry. An item-path or collections-path rename leaves that tag under the old
        // name, so the delete leaves it on them for good; renamed onto ANOTHER
        // collection's title, the delete strips that one's tag, and so its membership.
        let name = if collection.title.is_empty() {
            collection.tag_name()
        } else {
            collection.title.clone()
        };
        let lowered = name.to_lowercase();
        let other = self
            .collections
            .values()
            .find(|other| {
                other.section == collection.section && other.tag_name().to_lowercase() == lowered
            })
            .map(|other| other.id.clone());
        for key in &collection.items {
            self.untag_collection_member(key, &name);
            // One member at a time, as the item edit drops one: the emptied counts are
            // the last member's (`keep_emptied_counts`).
            let Some(other) = other.as_deref() else {
                continue;
            };
            let members = match self.collections.get(other) {
                Some(stored) if stored.items.contains(key) => self.collection_members(stored),
                _ => continue,
            };
            if let Some(stored) = self.collections.get_mut(other) {
                stored.items.retain(|item| item != key);
            }
            self.keep_emptied_counts(other, &members);
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
            "subtype": collection.subtype, "librarySectionID": collection.section});
        self.push_timeline_collection(&gone, 9, Some("deleted"));
        self.dirty = true;
        self.save_if_dirty();
        Response::empty(200)
    }

    /// The bare `{size, Metadata: [row]}` a collection write answers with.
    fn collection_row_container(&self, id: &str) -> Response {
        let Some(collection) = self.collections.get(id) else {
            return Response::error(404, "rex: not found");
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
            return Response::error(404, "rex: not found");
        }
        let id = segments[0];
        let Some(collection) = self.collections.get(id).cloned() else {
            return Response::error(404, "rex: no such collection");
        };
        if !matches!(method, "GET" | "HEAD" | "PUT" | "DELETE") {
            // Cycle 21: POST, PATCH and a bare OPTIONS are 404s here and below.
            return Response::error(404, "rex: not found");
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
                    self.write_collection_edit(id, &pairs, false);
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
                    return Response::error(404, "rex: not found");
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
                .map(|row| {
                    Self::without_written_prefs(row, self.grid_row(row, true, false, false))
                })
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
                return Response::error(404, "rex: not found");
            }
            let name = collection.tag_name();
            let removed = self
                .collections
                .get(id)
                .is_some_and(|c| c.items.contains(member));
            if removed {
                let members = self.collection_members(&collection);
                if let Some(stored) = self.collections.get_mut(id) {
                    stored.items.retain(|key| key != member);
                    stored.touch();
                }
                self.keep_emptied_counts(id, &members);
                self.untag_collection_member(member, &name);
                self.dirty = true;
            }
            return self.collection_row_container(id);
        }
        if matches!(leaf, "items" | "children") && segments.len() == 4 && segments[3] == "move" {
            if method != "PUT" {
                return Response::error(404, "rex: not found");
            }
            let member = segments[2];
            let after = query.get("after");
            if let Some(after) = after {
                let digits = after.strip_prefix('-').unwrap_or(after);
                if after == member
                    || digits.is_empty()
                    || !digits.bytes().all(|b| b.is_ascii_digit())
                {
                    return Response::error(400, "rex: bad request");
                }
            }
            if !collection.items.contains(member) {
                return Response::error(400, "rex: bad request");
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
                stored.items = items.into();
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
                    return Response::error(400, "rex: bad request");
                };
                if let Some(stored) = self.collections.get_mut(id) {
                    stored.uri = uri.clone();
                    stored.touch();
                }
                info!(
                    "COLLECTION {id} query <- {}",
                    uri.chars().take(120).collect::<String>()
                );
                self.dirty = true;
                return self.collection_row_container(id);
            }
            // Cycle 17: a comma-joined uri tolerates members it cannot use; only a
            // single-key uri of the wrong type (or none at all) is a 400.
            // Measured on plexscratch42: an add reads a SECTION uri at the collection's
            // level as the create does -- `/library/sections/3/all` into a show
            // collection adds the library's other show, and `all?type=2` into an
            // episode collection adds every episode.
            let level = match collection.subtype.as_str() {
                "movie" => 1,
                "show" => 2,
                "season" => 3,
                "episode" => 4,
                _ => 0,
            };
            let rows = self.collection_uri_refs(
                query.get("uri").map(String::as_str).unwrap_or(""),
                &collection.section,
                level,
                &collection.subtype,
                false,
            );
            let usable = rows
                .iter()
                .filter(|row| {
                    row.get("type").and_then(Value::as_str) == Some(collection.subtype.as_str())
                })
                .filter_map(key_of)
                .collect::<Vec<_>>();
            if rows.is_empty() || (rows.len() == 1 && usable.is_empty()) {
                return Response::error(400, "rex: bad request");
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
            return Response::error(404, "rex: not found");
        }
        let pairs = Self::request_pairs(request);
        if tail.len() == 1 && tail[0] == "prefs" {
            if method != "PUT" {
                return Response::error(404, "rex: not found");
            }
            if collection.smart && pairs.iter().any(|(name, _)| name == "collectionSort") {
                // Cycle 15: a SMART collection refuses `collectionSort` with a 400.
                return Response::error(400, "rex: bad request");
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
                return Response::error(400, "rex: bad request");
            }
            if let Some(stored) = self.collections.get_mut(&id) {
                // Cycle 89 (plexscratch42): a re-created collection reads none of its old
                // prefs until a prefs write is ACCEPTED -- then every old one it does not
                // write comes back with it. A refused write (above) brings nothing.
                if let Some(Value::Object(inherited)) = stored.extra.shift_remove("_inheritedPrefs") {
                    for (name, value) in inherited {
                        if !writes.iter().any(|(written, _)| *written == name) {
                            stored.prefs.insert(name, string_value(&value));
                        }
                    }
                }
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
            // The oracle joins its query dict's names, and that dict carries its own
            // `_pairs` entry last -- so the line names it too.
            let names = edit::query_names(&pairs);
            info!(
                "COLLECTION PREF {id} {}",
                if names.is_empty() { "_pairs".to_string() } else { format!("{names},_pairs") }
            );
            return Response::empty(200);
        }
        if tail.len() == 1 && matches!(tail[0], "thumbs" | "banners") {
            if !matches!(method, "GET" | "HEAD") {
                return Response::error(404, "rex: not found");
            }
            return Response::json(200, self.tag_container(Map::new()));
        }
        if tail.len() == 1 && tail[0] == "tree" {
            if !matches!(method, "GET" | "HEAD") {
                return Response::error(404, "rex: not found");
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
            return self.list_artwork_response(&id, true, tail[0], request);
        }
        if tail.len() == 1 && tail[0] == "children" {
            // Cycle 14/17: a full secondary container with LIGHT grid rows, the
            // librarySection triple on the CONTAINER, `allowSync: true`, no thumb.
            if !matches!(method, "GET" | "HEAD") {
                return Response::error(404, "rex: not found");
            }
            let section = self
                .section(&collection.section)
                .cloned()
                .unwrap_or(Value::Null);
            let rows = self
                .collection_members(collection)
                .iter()
                .map(|row| {
                    Self::without_written_prefs(row, self.grid_row(row, false, false, false))
                })
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
                // A NUMBER, as on every other collection container: measured 2026-09-30
                // on plexscratch42 (1.42.2) over four movie collections, paged and
                // unpaged. This one had copied the oracle's stored `sectionId` string.
                body.extend(self.collection_section_fields(collection));
            }
            return Response::json(200, container);
        }
        if tail.len() == 1 && matches!(tail[0], "clearLogos" | "themes") {
            return Response::json(200, self.tag_container(Map::new()));
        }
        if tail.len() == 1 && tail[0] == "composite" || tail.first() == Some(&"composite") {
            if !matches!(method, "GET" | "HEAD") {
                return Response::error(404, "rex: not found");
            }
            let rows = self.collection_members(collection);
            return self.composite_image(&rows, query);
        }
        if let Some(response) = self.list_artwork_read(&id, true, tail, query, method) {
            return response;
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
                return Response::error(404, "rex: not found");
            }
            if !matches!(method, "GET" | "HEAD" | "PUT") {
                return Response::error(404, "rex: not found");
            }
            if leaf == "match" || leaf == "extras" {
                return Response::error(400, "rex: bad request");
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
            return Response::error(404, "rex: not found");
        }
        if method == "PUT" {
            if let Some(refusal) = self.collection_edit_refusal(collection, &pairs) {
                return refusal;
            }
            self.write_collection_edit(&id, &pairs, false);
            if let Some(after) = self.collections.get(&id).cloned() {
                let row = self.dynamic_collection_row(&after);
                self.push_timeline_collection(&row, 3, Some("loading"));
                self.push_timeline_collection(&row, 5, None);
            }
            self.save_if_dirty();
            info!("EDIT list {id} <- {}", edit::query_names(&pairs));
            return Response::empty(200);
        }
        if method == "DELETE" {
            return self.delete_collection(&id);
        }
        // Measured on the production server: the library container any item's detail
        // carries.
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
        let rows = self.filter_rows(rows, pairs)?;
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
                    && collection.items.contains(key)
            }) {
                tag_object.insert(
                    "guid".to_string(),
                    json!(collection.guid()),
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
            return Response::error(400, "rex: no url");
        }
        let field = if leaf == "poster" { "thumb" } else { "art" };
        let stamp = now();
        let path = format!("/library/metadata/{id}/{field}/{stamp}");
        let chosen = query.get("url").map(|value| percent_decode(value)).unwrap_or_default();
        let Some(collection) = self.collections.get_mut(id) else {
            return Response::error(404, "rex: not found");
        };
        // The oracle's `set_artwork` keeps what was picked as the row's private url --
        // the candidate `/posters` then lists as selected -- except the placeholder.
        let private = if leaf == "poster" { "_poster_url" } else { "_art_url" };
        if chosen == format!("/library/metadata/{id}/{field}/1") {
            collection.extra.shift_remove(private);
        } else {
            collection.extra.insert(private.to_string(), json!(chosen));
        }
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

/// `{"childCount", "years"}`: the aggregate a plain collection's row reads, stored when the
/// reference stops recounting it (an emptying removal, a merge).
fn collection_counts(members: &[Value]) -> Value {
    let mut years = members
        .iter()
        .filter_map(|row| row.get("year").filter(|value| truthy(value)).map(py_str))
        .collect::<Vec<_>>();
    years.sort();
    json!({"childCount": members.len(), "years": years})
}

/// One automatic-collection pass at a time: two at once would each create the same collection.
static AUTO_PASS: Mutex<()> = Mutex::new(());

/// The movie libraries whose "Minimum automatic collection size" (`autoCollectionThreshold`,
/// 0 Disabled, 1-4) is set, with it.
fn auto_thresholds(runtime: &Runtime) -> HashMap<String, usize> {
    runtime
        .section_prefs
        .iter()
        .filter(|((_, name), _)| name == "autoCollectionThreshold")
        .filter_map(|((section, _), value)| Some((section.clone(), value.parse::<usize>().ok().filter(|n| *n > 0)?)))
        .filter(|(section, _)| {
            runtime.section(section).and_then(|row| row.get("type")).and_then(Value::as_str) == Some("movie")
        })
        .collect()
}

/// A library's automatic collections, as a real PMS makes them from its agent: every
/// collection Plex's agent files at least the threshold's number of the library's movies
/// under becomes one (its guid, name and summary the agent's), and one that exists already --
/// by that guid, or a collection of that name -- takes the movies it lacks. Nothing is ever
/// removed: the setting's own summary says a change "will have no effect on any existing
/// collections". rex has no metadata refresh to make them on, so a pass runs when the
/// threshold is set, when the library is refreshed, after a Fix Match, and after each feed
/// push (`backfill` false: what a push matched carries its collections already). A movie
/// described before rex kept them is asked of the agent again (`backfill`), off the lock;
/// its answer is almost always the one the agent's cache already holds.
pub(super) fn auto_collect(shared: &SharedRuntime, backfill: bool) {
    let _pass = AUTO_PASS.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let lock = || shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let (plex, unread) = {
        let guard = lock();
        let thresholds = auto_thresholds(&guard);
        if thresholds.is_empty() {
            return;
        }
        let unread = guard
            .catalog
            .hot()
            .lazy()
            .filter(|row| backfill && row.get("type").and_then(Value::as_str) == Some("movie"))
            .filter(|row| row.get("_collections").is_none() && row.get("deletedAt").is_none())
            .filter(|row| row.get("librarySectionID").is_some_and(|id| thresholds.contains_key(&string_value(id))))
            .filter_map(|row| {
                let externals = row
                    .get("Guid")
                    .and_then(Value::as_array)?
                    .iter()
                    .filter_map(|entry| entry.get("id")?.as_str().map(str::to_string))
                    .collect::<Vec<_>>();
                Some((key_of(&row)?, externals)).filter(|(_, externals)| !externals.is_empty())
            })
            .collect::<Vec<_>>();
        (guard.plex.clone(), unread)
    };
    let read = plex
        .map(|plex| {
            unread
                .into_iter()
                .filter_map(|(key, externals)| {
                    let answer = crate::identify::plex_match(plex.as_ref(), "movie", &externals)?;
                    Some((key, metadata::collections_of(&answer.row)))
                })
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    let mut guard = lock();
    for (key, collections) in read {
        if let Some(row) = guard.catalog.get_mut(&key).and_then(Value::as_object_mut) {
            row.insert("_collections".to_string(), collections);
            guard.dirty = true;
        }
    }
    let thresholds = auto_thresholds(&guard);
    for (section, threshold) in thresholds {
        guard.group_auto_collections(&section, threshold, backfill);
    }
    let snapshot = guard.flush_state();
    drop(guard);
    write_or_log(snapshot);
}

impl Runtime {
    /// One library's automatic collections (`auto_collect`). A collection a person deleted
    /// (`collection_guids` keeps its guid) comes back on an asked-for pass -- a refresh, the
    /// threshold set -- and never on a feed push, which would bring it back within hours.
    fn group_auto_collections(&mut self, section: &str, threshold: usize, asked: bool) {
        let mut groups: std::collections::BTreeMap<String, (Value, Vec<String>)> = std::collections::BTreeMap::new();
        for row in self.catalog.hot().lazy() {
            if row.get("type").and_then(Value::as_str) != Some("movie")
                || row.get("deletedAt").is_some()
                || row.get("librarySectionID").map(string_value).as_deref() != Some(section)
            {
                continue;
            }
            let Some(key) = key_of(&row) else {
                continue;
            };
            for entry in row.get("_collections").and_then(Value::as_array).into_iter().flatten() {
                let Some(guid) = entry.get("guid").and_then(Value::as_str) else {
                    continue;
                };
                groups.entry(guid.to_string()).or_insert_with(|| (entry.clone(), Vec::new())).1.push(key.clone());
            }
        }
        for (guid, (entry, keys)) in groups {
            let name = entry.get("tag").map(string_value).unwrap_or_default();
            let existing = self
                .collections
                .values()
                .find(|collection| {
                    collection.section == section
                        && !collection.smart
                        && (collection.guid() == guid || collection.tag_name().eq_ignore_ascii_case(&name))
                })
                .map(|collection| collection.id.clone());
            let id = match existing {
                Some(id) => id,
                None if keys.len() >= threshold
                    && (asked || !self.collection_guids.values().any(|gone| gone.get("guid").and_then(Value::as_str) == Some(guid.as_str()))) =>
                {
                    let query = HashMap::from([
                        ("title".to_string(), name.clone()),
                        ("sectionId".to_string(), section.to_string()),
                        ("type".to_string(), "1".to_string()),
                    ]);
                    if self.create_collection(&query).status != 200 {
                        continue;
                    }
                    let id = (self.next_collection - 1).to_string();
                    if let Some(created) = self.collections.get_mut(&id) {
                        created.extra.insert("guid".to_string(), json!(guid));
                        created.extra.insert("summary".to_string(), entry.get("summary").cloned().unwrap_or(json!("")));
                    }
                    id
                }
                None => continue,
            };
            let unlocked = keys
                .iter()
                .filter(|key| !self.catalog.get(key.as_str()).and_then(Value::as_object).is_some_and(|row| edit::field_locked(row, "collection")))
                .cloned()
                .collect::<Vec<_>>();
            if self.add_collection_items(&id, &keys) > 0 {
                // The agent's collection, not one a person typed: the members' field stays unlocked.
                for key in unlocked {
                    if let Some(row) = self.catalog.get_mut(&key).and_then(Value::as_object_mut) {
                        edit::set_lock(row, "collection", false);
                    }
                }
                info!("COLLECTION auto {name} in section {section}: {} member(s)", self.collections.get(&id).map_or(0, |c| c.items.len()));
                self.dirty = true;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The Matrix, Reloaded and Revolutions as Plex's agent matched them on the production rex
    /// (tests/fixtures/provider/collections.json, out of its plex_cache.db, 2026-10-17): each filed
    /// under "The Matrix Collection".
    fn library(name: &str, threshold: Option<&str>) -> (SharedRuntime, PathBuf) {
        // The build's own path, else the tree the test runs in (Windows runs a cross-built binary).
        let built = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let crate_dir = if built.exists() { built } else { PathBuf::from(".") };
        let file = crate_dir.join("../tests/fixtures/provider/collections.json");
        let recorded: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(file).unwrap()).unwrap();
        let mut items = Vec::new();
        for (index, answer) in recorded.iter().enumerate() {
            let mut row = Map::new();
            row.insert("ratingKey".to_string(), json!((100001 + index).to_string()));
            row.insert("type".to_string(), json!("movie"));
            row.insert("librarySectionID".to_string(), json!(1));
            metadata::describe(&mut row, &answer["response"]["MediaContainer"]["Metadata"][0], None, None, false, None);
            items.push(Value::Object(row));
        }
        // Big Buck Bunny is in no collection.
        items.push(json!({"ratingKey": "100009", "type": "movie", "librarySectionID": 1, "title": "Big Buck Bunny", "_collections": []}));
        // One directory under the repository, whose data/ holds the library settings (`sidecar_roots`).
        let dir = crate_dir
            .join("..")
            .join(format!(".auto-collections-{name}-{}-{}", std::process::id(), threshold.unwrap_or("none")));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let catalog = dir.join("catalog.json");
        let document = json!({"format": 1, "machineIdentifier": "auto-collections", "friendlyName": "auto",
            "sections": [{"key": "1", "type": "movie", "title": "Movies", "Location": [{"path": "/movies"}]}], "items": items});
        std::fs::write(&catalog, serde_json::to_vec(&document).unwrap()).unwrap();
        let mut runtime = Runtime::load(RuntimeOptions { catalog, state: Some(dir.join("no-state")), ..RuntimeOptions::default() }).unwrap();
        if let Some(threshold) = threshold {
            runtime.write_section_prefs("1", "movie", &[("autoCollectionThreshold".to_string(), threshold.to_string())]).unwrap();
            assert_eq!(runtime.pending_auto_collections, threshold != "0", "setting the threshold leaves the pass due");
        }
        (Arc::new(Mutex::new(runtime)), dir)
    }

    fn collections(shared: &SharedRuntime) -> Vec<(String, String, Vec<String>)> {
        let guard = shared.lock().unwrap();
        guard.collections.values().map(|c| (c.title.clone(), c.guid(), c.items.to_vec())).collect()
    }

    /// wason92 in #rex-support, 2026-10-16: a movie library's automatic collections never came,
    /// set before or after its movies arrived, because rex runs no metadata refresh to make them.
    #[test]
    fn a_threshold_makes_the_agents_collections() {
        let (shared, dir) = library("made", Some("2"));
        auto_collect(&shared, true);
        let made = collections(&shared);
        assert_eq!(made.len(), 1, "one collection, none for Big Buck Bunny: {made:?}");
        let (title, guid, members) = &made[0];
        assert_eq!(title, "The Matrix Collection");
        assert_eq!(guid, "plex://collection/5ec2fd508509540041a16274", "the agent's guid");
        assert_eq!(members, &["100001", "100002", "100003"]);
        {
            let guard = shared.lock().unwrap();
            let matrix = guard.catalog.get("100001").unwrap();
            assert_eq!(matrix["Collection"][0]["tag"], "The Matrix Collection", "a member carries its tag");
            assert!(!edit::field_locked(matrix.as_object().unwrap(), "collection"), "the agent's collection locks nothing");
            let summary = guard.collections.values().next().unwrap().extra["summary"].as_str().unwrap().to_string();
            assert!(!summary.is_empty(), "the agent's summary");
        }
        // Another pass, or a refresh, makes no second one.
        auto_collect(&shared, true);
        assert_eq!(collections(&shared).len(), 1);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn below_the_threshold_or_disabled_nothing_is_made() {
        for threshold in [Some("4"), Some("0"), None] {
            let (shared, dir) = library("none", threshold);
            auto_collect(&shared, true);
            assert!(collections(&shared).is_empty(), "{threshold:?}");
            let _ = std::fs::remove_dir_all(dir);
        }
    }

    /// A movie that arrives later joins the collection it is filed under, and one of that name a
    /// person made already takes the agent's members rather than a second collection being made.
    #[test]
    fn later_arrivals_join_and_a_same_named_collection_is_reused() {
        let (shared, dir) = library("later", Some("2"));
        {
            let mut guard = shared.lock().unwrap();
            let query = HashMap::from([
                ("title".to_string(), "The Matrix Collection".to_string()),
                ("sectionId".to_string(), "1".to_string()),
                ("type".to_string(), "1".to_string()),
            ]);
            assert_eq!(guard.create_collection(&query).status, 200);
            // Revolutions has not arrived yet.
            guard.catalog.get_mut("100003").unwrap().as_object_mut().unwrap().insert("deletedAt".to_string(), json!(1));
        }
        auto_collect(&shared, false);
        let made = collections(&shared);
        assert_eq!(made.len(), 1, "{made:?}");
        assert_eq!(made[0].2, ["100001", "100002"]);
        shared.lock().unwrap().catalog.get_mut("100003").unwrap().as_object_mut().unwrap().shift_remove("deletedAt");
        auto_collect(&shared, false);
        assert_eq!(collections(&shared)[0].2, ["100001", "100002", "100003"]);
        let _ = std::fs::remove_dir_all(dir);
    }

    /// A collection a person deleted stays deleted through feed pushes; a refresh makes it again.
    #[test]
    fn a_deleted_collection_waits_for_a_refresh() {
        let (shared, dir) = library("deleted", Some("2"));
        auto_collect(&shared, true);
        let id = shared.lock().unwrap().collections.keys().next().unwrap().clone();
        shared.lock().unwrap().delete_collection(&id);
        auto_collect(&shared, false);
        assert!(collections(&shared).is_empty(), "a push brought it back");
        auto_collect(&shared, true);
        assert_eq!(collections(&shared).len(), 1, "a refresh makes it again");
        let _ = std::fs::remove_dir_all(dir);
    }
}
