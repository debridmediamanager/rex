//! The listing query's shared grammar -- `type=`, the refused filter fields,
//! `limit=`, `group=` -- and `/library/all`, ported from `server.py`.

use super::*;
use super::resume::int_param;

/// Filter names every listing refuses with a 500 (cycles 15, 18, 45, 53).
const REFUSED_FILTER_FIELDS: &[&str] =
    &["parentIndex", "theme", "banner", "art", "thumb", "slug", "extraType", "userFields"];
const RESOLUTIONS: &[&str] = &["sd", "480", "576", "720", "1080", "2k", "2.7k", "4k", "8k", "16k"];
/// The fields `group=` accepts, probed one at a time against the reference.
const GROUPABLE: &[&str] = &[
    "title", "titleSort", "studio", "contentRating", "year", "decade", "addedAt", "updatedAt",
    "originallyAvailableAt", "audienceRating", "rating", "editionTitle", "index", "summary",
    "tagline", "contentRatingAge", "type", "guid", "id", "originalTitle", "createdAt",
    "mediaCount", "ratingCount", "absoluteIndex", "remote", "isAdult", "resourcesChangedAt",
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
    if query
        .keys()
        .any(|key| REFUSED_FILTER_FIELDS.contains(&bare_key(key)) && !allow.contains(&bare_key(key)))
    {
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
    query.keys().any(|key| bare_key(key) == "unmatched").then_some(500)
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
    let mut kind = query.get("type").map(|value| value.trim().to_string()).unwrap_or_default();
    if kind.is_empty() && !whole_library {
        kind = default_type.unwrap_or("").trim().to_string();
    }
    let refused: &[&str] = if whole_library { &["2", "3"] } else { &["2"] };
    refused.contains(&kind.as_str()).then_some(500)
}

/// `?limit=N` caps the rows the QUERY returns: `0` is one row and a negative one
/// every row; anything but a signed 32-bit integer is a 500 (cycle 55).
pub(super) fn limited(rows: Vec<Value>, query: &HashMap<String, String>) -> Result<Vec<Value>, u16> {
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
                key_of(row).and_then(|key| key.parse::<i64>().ok()).unwrap_or(0),
            );
            match best.get(&marker) {
                Some((current, _)) if *current <= rank => {}
                _ => {
                    best.insert(marker, (rank, index));
                }
            }
        }
        let chosen = best.values().map(|(_, index)| *index).collect::<HashSet<_>>();
        Ok(rows
            .into_iter()
            .enumerate()
            .filter(|(index, _)| chosen.contains(index))
            .map(|(_, row)| row)
            .collect())
    }

    /// `/library/all` (`server.py`'s handler of the same route).
    pub(super) fn library_all(&self, query: &HashMap<String, String>) -> Response {
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
            let rows = match self.sort_rows(rows, Some(query.get("sort").map(String::as_str).unwrap_or("titleSort"))) {
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
            .filter(|row| type_number.is_some() || row.get("type").and_then(Value::as_str) != Some("clip"))
            .cloned()
            .collect::<Vec<_>>();
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
        source.sort_by_key(|row| key_of(row).and_then(|key| key.parse::<i64>().ok()).unwrap_or(i64::MAX));
        if type_number.is_some() && query.get("sort").map_or(true, |sort| sort.is_empty()) {
            source = self.sort_rows(source, Some("titleSort")).unwrap_or_default();
        }
        if let Some(number) = type_number {
            let wanted = match number {
                1 => "movie",
                2 => "show",
                3 => "season",
                4 => "episode",
                _ => "",
            };
            source.retain(|row| !wanted.is_empty() && row.get("type").and_then(Value::as_str) == Some(wanted));
        }
        let section_type = if type_number == Some(2) { "show" } else { "movie" };
        let pairs = query
            .iter()
            .filter(|(key, _)| key.as_str() != "guid")
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect::<Vec<_>>();
        let mut rows = match self.filter_rows(source, &pairs, section_type) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad request"),
        };
        // `guid=` is a case-sensitive PREFIX of the row's guid (measured).
        if let Some(prefix) = query.get("guid") {
            rows.retain(|row| row.get("guid").and_then(Value::as_str).unwrap_or("").starts_with(prefix.as_str()));
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
                        object.remove("Media");
                        match row.get("_libraryAllDuration") {
                            Some(duration) if !duration.is_null() => {
                                object.insert("duration".to_string(), duration.clone());
                            }
                            _ => {
                                object.remove("duration");
                            }
                        }
                    }
                }
                shown
            })
            .collect::<Vec<_>>();
        Response::json(200, self.container(Map::from_iter([("Metadata".to_string(), Value::Array(rows))]), true))
    }
}
