//! Listing helpers ported from `server.py`: `grouped`, `_limited`, `_guid_prefix` and
//! the `/library/all` query.

use super::*;

/// The fields `group=` accepts.  Cycle 53: three of the ten database columns group and
/// seven are a 500.
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

/// Cycle 55: a signed 32-bit integer with an optional sign and nothing else.
pub(super) fn int_param(text: &str) -> Option<i64> {
    let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let value: i128 = text.trim_start_matches('+').parse().ok()?;
    (-(1i128 << 31)..(1i128 << 31))
        .contains(&value)
        .then_some(value as i64)
}

impl Runtime {
    /// `group=<field>` -- one row per distinct value of that field.
    ///
    /// Cycle 14 measured WHICH row survives: the group's **minimum `addedAt`**,
    /// chosen before the sort, so the surviving SET is the same under every `sort=`
    /// and only the order changes (lowest ratingKey breaks a tie).  An empty, unknown
    /// or non-scalar field is a 500.
    pub(super) fn grouped(&self, rows: Vec<Value>, field: &str) -> Result<Vec<Value>, u16> {
        let bare = field.split_once('.').map(|(_, bare)| bare).unwrap_or(field);
        if bare.is_empty() || !GROUPABLE.contains(&bare) {
            return Err(500);
        }
        let mut best: Vec<(String, (i64, i64), usize)> = Vec::new();
        for (position, row) in rows.iter().enumerate() {
            let marker = self.filter_value(row, field).to_string();
            let rank = (
                row.get("addedAt").and_then(Value::as_i64).unwrap_or(0),
                row.get("ratingKey")
                    .map(string_value)
                    .and_then(|key| key.parse::<i64>().ok())
                    .unwrap_or(0),
            );
            match best.iter_mut().find(|(known, _, _)| *known == marker) {
                Some(entry) => {
                    if rank < entry.1 {
                        entry.1 = rank;
                        entry.2 = position;
                    }
                }
                None => best.push((marker, rank, position)),
            }
        }
        let chosen = best.iter().map(|(_, _, position)| *position).collect::<HashSet<_>>();
        Ok(rows
            .into_iter()
            .enumerate()
            .filter(|(position, _)| chosen.contains(position))
            .map(|(_, row)| row)
            .collect())
    }

    /// `?limit=N` on a listing: it caps the rows the QUERY returns -- `limit=0`
    /// answers ONE row and a negative one every row; a value that is not a signed
    /// 32-bit integer is a 500.
    pub(super) fn limited(rows: Vec<Value>, query: &HashMap<String, String>) -> Result<Vec<Value>, u16> {
        let Some(raw) = query.get("limit") else {
            return Ok(rows);
        };
        let Some(count) = int_param(raw) else {
            return Err(500);
        };
        if count < 0 {
            return Ok(rows);
        }
        Ok(rows.into_iter().take(count.max(1) as usize).collect())
    }

    /// `/library/all` -- a real query endpoint, not a dump.  `type=` selects the rows,
    /// filters and `sort=` apply as on a section, and a row carries its Media only
    /// when a `type` was given.
    pub(super) fn route_library_all(
        &self,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Response {
        // Measured on both servers: `/library/all` refuses an unparseable `type=` with
        // 400 where a section listing answers 500, and both answer 500 to an empty one.
        if let Some(raw) = query.get("type") {
            if raw.trim().is_empty() {
                return Response::error(500, "plexsim: bad request");
            }
            if int_param(raw).is_none() {
                return Response::error(400, "plexsim: bad request");
            }
        }
        let kinds = pairs
            .iter()
            .filter(|(name, _)| name == "type")
            .map(|(_, value)| value.clone())
            .collect::<Vec<_>>();
        if kinds.iter().all(|kind| int_param(kind) == Some(18)) && !kinds.is_empty() {
            return self.route_all(query);
        }
        let scoped = |scope: &str, field: &str| {
            pairs.iter().any(|(name, _)| {
                let (key, _) = crate::filters::split_key(name);
                let (found_scope, found_field) = crate::filters::split_scope(&key);
                found_scope == scope && found_field == field
            })
        };
        let scoped_guid = scoped("show", "guid");
        let scoped_season_index = scoped("season", "index");
        let mut source = self.catalog.items.clone();
        // Cycle 41: a scoped show guid supplies the row type when `/library/all` is
        // untyped, so `show.guid=...&season.index=1` returns the SHOW alone.  With an
        // explicit type the plan is asymmetric: type 3 returns its season, type 4 its
        // episodes, and `type=2&season.index=1` refuses with 500.
        let kind_set = kinds.iter().collect::<HashSet<_>>();
        if kind_set.is_empty() && scoped_guid {
            source.retain(|row| row.get("type").and_then(Value::as_str) == Some("show"));
        }
        if kind_set.len() == 1 && kinds[0] == "2" && scoped_season_index {
            return Response::error(500, "plexsim: invalid scoped conjunction");
        }
        if kind_set.is_empty() {
            // Cycle 25: the UNTYPED query lists collections as well; cycle 50: clips
            // are the opposite exception.
            source.retain(|row| row.get("type").and_then(Value::as_str) != Some("clip"));
            source.extend(
                self.collections
                    .values()
                    .map(|collection| self.dynamic_collection_row(collection)),
            );
        }
        let rating_key = |row: &Value| {
            row.get("ratingKey")
                .map(string_value)
                .and_then(|key| key.parse::<i64>().ok())
                .unwrap_or(0)
        };
        source.sort_by_key(rating_key);
        // Cycle 50: only the UNTYPED form defaults to the ratingKey.  A TYPED
        // `/library/all?type=N` uses that type's own default listing order.
        if !kind_set.is_empty() && !query.contains_key("sort") {
            source = match self.sort_rows(source, Some("titleSort")) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: bad request"),
            };
        }
        if !kind_set.is_empty() {
            let wanted = if kind_set.len() == 1 {
                match int_param(&kinds[0]) {
                    Some(1) => Some("movie"),
                    Some(2) => Some("show"),
                    Some(3) => Some("season"),
                    Some(4) => Some("episode"),
                    _ => None,
                }
            } else {
                None
            };
            source.retain(|row| {
                wanted.is_some() && row.get("type").and_then(Value::as_str) == wanted
            });
        }
        let filter_pairs = pairs
            .iter()
            .filter(|(name, _)| {
                // A plain `guid=` is the listing's own prefix match below; the grammar
                // reads `guid` only when a scoped guid asks for it.
                scoped_guid || name != "guid"
            })
            .cloned()
            .collect::<Vec<_>>();
        let section_type = match kinds.first().and_then(|kind| int_param(kind)) {
            Some(2..=4) => "show",
            _ => "movie",
        };
        source = match self.filter_rows(source, &filter_pairs, section_type) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad request"),
        };
        // `guid=` on a listing: a case-sensitive PREFIX of the row's guid.
        let prefixes = pairs
            .iter()
            .filter(|(name, _)| name == "guid")
            .map(|(_, value)| value.clone())
            .collect::<Vec<_>>();
        if !prefixes.is_empty() {
            source.retain(|row| {
                let guid = row.get("guid").map(string_value).unwrap_or_default();
                prefixes.iter().all(|prefix| guid.starts_with(prefix.as_str()))
            });
        }
        if let Some(group) = query.get("group") {
            source = match self.grouped(source, group) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: bad request"),
            };
        }
        if let Some(sort) = query.get("sort") {
            source = match self.sort_rows(source, Some(sort)) {
                Ok(rows) => rows,
                Err(status) => return Response::error(status, "plexsim: bad request"),
            };
        }
        let source = match Self::limited(source, query) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad request"),
        };
        let keep_guid = query.get("includeGuids").is_some_and(|value| value == "1");
        let rows = source
            .iter()
            .map(|row| {
                if self.collections.contains_key(&key_of(row).unwrap_or_default()) {
                    return row.clone();
                }
                let mut shown = self.grid_row(row, true, keep_guid, false);
                if kind_set.is_empty() {
                    if let Some(object) = shown.as_object_mut() {
                        object.remove("Media");
                        // Cycle 50: this legacy untyped query reads the stored runtime,
                        // not Media.duration; a null stored value omits the field.
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
        Response::json(
            200,
            self.container(
                Map::from_iter([("Metadata".to_string(), Value::Array(rows))]),
                true,
            ),
        )
    }
}
