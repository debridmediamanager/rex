//! The listing query's shared grammar -- `type=`, the refused filter fields,
//! `limit=`, `group=` -- and `/library/all`, ported from `server.py`.

use super::resume::int_param;
use super::*;

/// Filter names every listing refuses with a 500 (cycles 15, 18, 45, 53).
const REFUSED_FILTER_FIELDS: &[&str] = &[
    "parentIndex",
    "theme",
    "banner",
    "art",
    "thumb",
    "slug",
    "extraType",
    "userFields",
];
const RESOLUTIONS: &[&str] = &[
    "sd", "480", "576", "720", "1080", "2k", "2.7k", "4k", "8k", "16k",
];
/// The fields `group=` accepts, probed one at a time against the reference.
const GROUPABLE: &[&str] = &[
    "title",
    "titleSort",
    "studio",
    "contentRating",
    "year",
    "decade",
    "addedAt",
    "updatedAt",
    "originallyAvailableAt",
    "audienceRating",
    "rating",
    "editionTitle",
    "index",
    "summary",
    "tagline",
    "contentRatingAge",
    "type",
    "guid",
    "id",
    "originalTitle",
    "createdAt",
    "mediaCount",
    "ratingCount",
    "absoluteIndex",
    "remote",
    "isAdult",
    "resourcesChangedAt",
];

fn bare_key(key: &str) -> &str {
    key.trim_end_matches(['<', '>', '=', '!'])
}

/// `type=` has to PARSE as a signed 32-bit whole number; an empty one is always a 500
/// unless `empty_status` says otherwise.
pub(super) fn parsed_type(
    query: &HashMap<String, String>,
    status: u16,
    empty_status: Option<u16>,
) -> Result<Option<i64>, u16> {
    let Some(raw) = query.get("type") else {
        return Ok(None);
    };
    if raw.trim().is_empty() {
        return Err(empty_status.unwrap_or(500));
    }
    if !int_param(raw, Some(32)) {
        return Err(status);
    }
    Ok(raw.trim_start_matches('+').parse::<i64>().ok())
}

/// The refusals a listing answers wherever it consumes a query (cycle 18); `allow` is
/// the section extras leaf's exemption for `extraType` (cycle 45).
pub(super) fn listing_refusals(query: &HashMap<String, String>, allow: &[&str]) -> Option<u16> {
    if query.keys().any(|key| {
        REFUSED_FILTER_FIELDS.contains(&bare_key(key)) && !allow.contains(&bare_key(key))
    }) {
        return Some(500);
    }
    for (key, value) in query {
        if bare_key(key) == "resolution" && !RESOLUTIONS.contains(&value.trim()) {
            return Some(500);
        }
    }
    if let Some(source) = query.get("sourceType") {
        if !int_param(source, Some(32)) {
            return Some(500);
        }
    }
    None
}

/// Cycle 45: `unmatched=` is a 500 on `/library/all`.
pub(super) fn whole_library_refusal(query: &HashMap<String, String>) -> Option<u16> {
    query
        .keys()
        .any(|key| bare_key(key) == "unmatched")
        .then_some(500)
}

/// Cycle 45: the 500 `file=` gets, decided by the QUERY's type.
pub(super) fn file_filter_refusal(
    query: &HashMap<String, String>,
    whole_library: bool,
    default_type: Option<&str>,
) -> Option<u16> {
    if !query.keys().any(|key| bare_key(key) == "file") {
        return None;
    }
    let mut kind = query
        .get("type")
        .map(|value| value.trim().to_string())
        .unwrap_or_default();
    if kind.is_empty() && !whole_library {
        kind = default_type.unwrap_or("").trim().to_string();
    }
    let refused: &[&str] = if whole_library { &["2", "3"] } else { &["2"] };
    refused.contains(&kind.as_str()).then_some(500)
}

/// `?limit=N` caps the rows the QUERY returns: `0` is one row and a negative one
/// every row; anything but a signed 32-bit integer is a 500 (cycle 55).
pub(super) fn limited(
    rows: Vec<Value>,
    query: &HashMap<String, String>,
) -> Result<Vec<Value>, u16> {
    let Some(raw) = query.get("limit") else {
        return Ok(rows);
    };
    if !int_param(raw, Some(32)) {
        return Err(500);
    }
    let count = raw.trim_start_matches('+').parse::<i64>().unwrap_or(0);
    if count < 0 {
        return Ok(rows);
    }
    Ok(rows.into_iter().take((count.max(1)) as usize).collect())
}

impl Runtime {
    /// `group=<field>` -- one row per distinct value, the survivor being the group's
    /// minimum `addedAt` (then ratingKey), chosen BEFORE the sort (cycle 14).
    pub(super) fn grouped(&self, rows: Vec<Value>, field: &str) -> Result<Vec<Value>, u16> {
        let bare = field.split_once('.').map_or(field, |(_, bare)| bare);
        if bare.is_empty() || !GROUPABLE.contains(&bare) {
            return Err(500);
        }
        let mut best: HashMap<String, ((i64, i64), usize)> = HashMap::new();
        for (index, row) in rows.iter().enumerate() {
            let marker = string_value(&self.filter_value(row, field));
            let rank = (
                row.get("addedAt").and_then(Value::as_i64).unwrap_or(0),
                key_of(row)
                    .and_then(|key| key.parse::<i64>().ok())
                    .unwrap_or(0),
            );
            match best.get(&marker) {
                Some((current, _)) if *current <= rank => {}
                _ => {
                    best.insert(marker, (rank, index));
                }
            }
        }
        let chosen = best
            .values()
            .map(|(_, index)| *index)
            .collect::<HashSet<_>>();
        Ok(rows
            .into_iter()
            .enumerate()
            .filter(|(index, _)| chosen.contains(index))
            .map(|(_, row)| row)
            .collect())
    }

    /// `/library/all` (`server.py`'s handler of the same route).
    pub(super) fn library_all(&self, query: &HashMap<String, String>) -> Response {
        let pairs = query
            .iter()
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect::<Vec<_>>();
        self.library_all_pairs(query, &pairs)
    }

    /// `/library/all` with the request's ORDERED pairs: a filter query may repeat a
    /// key, and the scoped-guid conjunction below reads every `type=` it names.
    pub(super) fn library_all_pairs(
        &self,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Response {
        // Measured: an unparseable `type=` is a 400 here and an empty one a 500.
        let type_number = match parsed_type(query, 400, None) {
            Ok(number) => number,
            Err(status) => return Response::error(status, "plexsim: bad type"),
        };
        if let Some(status) = listing_refusals(query, &[])
            .or_else(|| whole_library_refusal(query))
            .or_else(|| file_filter_refusal(query, true, None))
        {
            return Response::error(status, "plexsim: refused");
        }
        if type_number == Some(18) {
            let mut rows = self
                .collections
                .values()
                .map(|collection| self.dynamic_collection_row(collection))
                .collect::<Vec<_>>();
            if let Some(section) = query.get("sectionId") {
                rows.retain(|row| {
                    string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == *section
                });
            }
            let rows = match self.sort_rows(
                rows,
                Some(query.get("sort").map(String::as_str).unwrap_or("titleSort")),
            ) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: unsupported sort"),
            };
            let (page, paged, start, total) = match page_window(rows, query) {
                Ok(window) => window,
                Err(status) => return Response::error(status, "plexsim: bad container parameter"),
            };
            let mut fields = Map::from_iter([("Metadata".to_string(), Value::Array(page))]);
            if paged {
                fields.insert("offset".to_string(), json!(start));
                fields.insert("totalSize".to_string(), json!(total));
            }
            return Response::json(200, self.container(fields, true));
        }
        let mut source = self
            .catalog
            .items
            .iter()
            .filter(|row| {
                type_number.is_some() || row.get("type").and_then(Value::as_str) != Some("clip")
            })
            .cloned()
            .collect::<Vec<_>>();
        let scoped = |scope: &str, field: &str| {
            pairs.iter().any(|(name, _)| {
                let (key, _) = crate::filters::split_key(name);
                let (found_scope, found_field) = crate::filters::split_scope(&key);
                found_scope == scope && found_field == field
            })
        };
        let scoped_guid = scoped("show", "guid");
        // Cycle 41, measured with this exact client conjunction on both PMS versions.
        // A scoped show guid supplies the row type when `/library/all` is untyped, so
        // `show.guid=...&season.index=1` returns the SHOW alone.  With an explicit type
        // the plan is asymmetric: type 3 returns its season, type 4 its episodes, and
        // `type=2&season.index=1` refuses with 500 even without the guid clause.
        if type_number.is_none() && scoped_guid {
            source.retain(|row| row.get("type").and_then(Value::as_str) == Some("show"));
        }
        if type_number == Some(2) && scoped("season", "index") {
            return Response::error(500, "plexsim: invalid scoped conjunction");
        }
        if type_number.is_none() {
            // Cycle 25: the UNTYPED query lists collections as well, sorted into the
            // rest by ratingKey.
            source.extend(self.collections.values().map(|collection| {
                let mut row = self.dynamic_collection_row(collection);
                if let Some(object) = row.as_object_mut() {
                    object.insert("_collectionRow".to_string(), json!(true));
                }
                row
            }));
        }
        source.sort_by_key(|row| {
            key_of(row)
                .and_then(|key| key.parse::<i64>().ok())
                .unwrap_or(i64::MAX)
        });
        if type_number.is_some() && query.get("sort").map_or(true, |sort| sort.is_empty()) {
            source = self
                .sort_rows(source, Some("titleSort"))
                .unwrap_or_default();
        }
        if let Some(number) = type_number {
            let wanted = match number {
                1 => "movie",
                2 => "show",
                3 => "season",
                4 => "episode",
                _ => "",
            };
            source.retain(|row| {
                !wanted.is_empty() && row.get("type").and_then(Value::as_str) == Some(wanted)
            });
        }
        let section_type = if type_number == Some(2) {
            "show"
        } else {
            "movie"
        };
        // A plain `guid=` is the listing's own prefix match below; the grammar reads
        // `guid` only when a scoped guid asks for it (`parse_only`).
        let filter_pairs = pairs
            .iter()
            .filter(|(key, _)| scoped_guid || key.as_str() != "guid")
            .cloned()
            .collect::<Vec<_>>();
        let mut rows = match self.filter_rows(source, &filter_pairs, section_type) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad request"),
        };
        // `guid=` is a case-sensitive PREFIX of the row's guid (measured).
        let prefixes = pairs
            .iter()
            .filter(|(name, _)| name == "guid")
            .map(|(_, value)| value.as_str())
            .collect::<Vec<_>>();
        if !prefixes.is_empty() {
            rows.retain(|row| {
                let guid = row.get("guid").and_then(Value::as_str).unwrap_or("");
                prefixes.iter().all(|prefix| guid.starts_with(prefix))
            });
        }
        if let Some(group) = query.get("group") {
            // Cycle 29: `group=` is honoured here too, and refused as on a listing.
            rows = match self.grouped(rows, group) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: cannot group"),
            };
        }
        if let Some(sort) = query.get("sort").filter(|sort| !sort.is_empty()) {
            rows = match self.sort_rows(rows, Some(sort)) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: unsupported sort key"),
            };
        }
        let rows = match limited(rows, query) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad limit"),
        };
        let keep_guid = query.get("includeGuids").map(String::as_str) == Some("1");
        let rows = rows
            .iter()
            .map(|row| {
                if row.get("_collectionRow").is_some() {
                    return strip_private_value(row);
                }
                let mut shown = self.grid_row(row, true, keep_guid, false);
                if type_number.is_none() {
                    if let Some(object) = shown.as_object_mut() {
                        object.shift_remove("Media");
                        match row.get("_libraryAllDuration") {
                            Some(duration) if !duration.is_null() => {
                                object.insert("duration".to_string(), duration.clone());
                            }
                            _ => {
                                object.shift_remove("duration");
                            }
                        }
                    }
                }
                shown
            })
            .collect::<Vec<_>>();
        Response::json(
            200,
            self.container(
                Map::from_iter([("Metadata".to_string(), Value::Array(rows))]),
                true,
            ),
        )
    }
}

impl Runtime {
    /// `/library/sections/{id}/extras` and its one view, `/extras/all` (cycles 15, 29,
    /// 45): the leaf runs the LISTING query and refuses what a listing refuses, plus
    /// the four media keys, while `extraType=` is a real column here.
    pub(super) fn section_extras(
        &self,
        section_key: &str,
        rest: &[&str],
        query: &HashMap<String, String>,
    ) -> Response {
        let Some(section) = self.section(section_key) else {
            return Response::error(404, "plexsim: not found");
        };
        if !(rest.is_empty() || rest == ["all"]) {
            return Response::error(404, "plexsim: not found");
        }
        if rest == ["all"] {
            if let Some(status) = listing_refusals(query, &["extraType"]) {
                return Response::error(status, "plexsim: refused");
            }
            if query.get("limit").is_some_and(|raw| {
                let digits = raw.trim_start_matches('-');
                digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit())
            }) {
                return Response::error(500, "plexsim: server error");
            }
            if let Some(sort) = query.get("sort").filter(|sort| !sort.is_empty()) {
                if self.sort_rows(Vec::new(), Some(sort)).is_err() {
                    return Response::error(500, "plexsim: unsupported sort key");
                }
            }
            if let Err(status) = parsed_type(query, 500, None) {
                return Response::error(status, "plexsim: bad type");
            }
            if query.contains_key("group") {
                return Response::error(500, "plexsim: server error");
            }
            if query.get("parent").is_some_and(|raw| {
                let digits = raw.trim_start_matches('-');
                digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit())
            }) {
                return Response::error(500, "plexsim: server error");
            }
            if query.keys().any(|key| {
                matches!(
                    bare_key(key).trim(),
                    "duration" | "resolution" | "mediaSize" | "file"
                )
            }) {
                return Response::error(500, "plexsim: server error");
            }
            if query.get("index").is_some_and(|raw| !int_param(raw, None)) {
                return Response::error(500, "plexsim: bad index");
            }
            if query
                .get("sourceType")
                .is_some_and(|raw| !int_param(raw, Some(32)))
            {
                return Response::error(500, "plexsim: bad sourceType");
            }
        }
        let is_show = section.get("type").and_then(Value::as_str) == Some("show");
        let title = section
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let mut fields = Map::new();
        fields.insert("size".to_string(), json!(0));
        fields.insert(
            "art".to_string(),
            json!(if is_show {
                "/:/resources/show-fanart.jpg"
            } else {
                "/:/resources/movie-fanart.jpg"
            }),
        );
        fields.insert(
            "identifier".to_string(),
            json!("com.plexapp.plugins.library"),
        );
        fields.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
        fields.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
        fields.insert(
            "thumb".to_string(),
            json!(if is_show {
                "/:/resources/show.png"
            } else {
                "/:/resources/movie.png"
            }),
        );
        fields.insert("title1".to_string(), json!(title));
        if is_show {
            fields.insert("nocache".to_string(), json!(true));
        }
        if rest.is_empty() {
            fields.insert("size".to_string(), json!(1));
            fields.insert(
                "Directory".to_string(),
                json!([{"key": "all", "title": format!("All {title}")}]),
            );
            return Response::json(
                200,
                Value::Object(Map::from_iter([(
                    "MediaContainer".to_string(),
                    Value::Object(fields),
                )])),
            );
        }
        // The library's clips: every clip owned by a parent in this section.
        let mut rows = self
            .catalog
            .items
            .iter()
            .filter(|row| {
                row.get("type").and_then(Value::as_str) == Some("clip")
                    && string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                        == section_key
            })
            .cloned()
            .collect::<Vec<_>>();
        if let Ok(Some(number)) = parsed_type(query, 500, None) {
            if number != 12 {
                rows.clear();
            }
        }
        if let Some(wanted) = query.get("extraType") {
            rows.retain(|row| {
                row.get("extraType").map(string_value).as_deref() == Some(wanted.as_str())
            });
        }
        let pairs = query
            .iter()
            .filter(|(key, _)| key.as_str() != "extraType")
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect::<Vec<_>>();
        let rows = match self.filter_rows(rows, &pairs, if is_show { "show" } else { "movie" }) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad request"),
        };
        let rows = match self.sort_rows(
            rows,
            Some(
                query
                    .get("sort")
                    .map(String::as_str)
                    .filter(|s| !s.is_empty())
                    .unwrap_or("index"),
            ),
        ) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: unsupported sort key"),
        };
        let rows = match limited(rows, query) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad limit"),
        };
        let (page, paged, start, total) = match page_window(rows, query) {
            Ok(window) => window,
            Err(status) => return Response::error(status, "plexsim: bad container parameter"),
        };
        fields.insert("size".to_string(), json!(page.len()));
        if paged {
            fields.insert("totalSize".to_string(), json!(total));
            fields.insert("offset".to_string(), json!(start));
        }
        if !page.is_empty() {
            fields.insert(
                "Metadata".to_string(),
                Value::Array(
                    page.iter()
                        .map(|row| self.grid_row(row, false, false, false))
                        .collect(),
                ),
            );
        }
        Response::json(
            200,
            Value::Object(Map::from_iter([(
                "MediaContainer".to_string(),
                Value::Object(fields),
            )])),
        )
    }
}
