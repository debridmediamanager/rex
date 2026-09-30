//! A library's secondary axes -- By Genre, By Year, By First Letter, the media axes,
//! the fixed rating scale, the other library types' axes -- and the bucket listing
//! behind each entry. Ported from `server.py`'s `secondary_directory`, `media_axis`,
//! `secondary_container`, `axis_bucket`, `typed_rows` and the `firstCharacter` leaf,
//! whose docstrings hold the measurements.

use super::*;

/// Tag axes and the element each reads.
const SECONDARY_TAGS: &[(&str, &str)] = &[
    ("genre", "Genre"),
    ("collection", "Collection"),
    ("director", "Director"),
    ("writer", "Writer"),
    ("producer", "Producer"),
    ("actor", "Role"),
    ("country", "Country"),
    ("label", "Label"),
    ("network", "Network"),
];

/// Field axes and the column each reads.
const SECONDARY_FIELDS: &[(&str, &str)] = &[
    ("year", "year"),
    ("decade", "year"),
    ("contentRating", "contentRating"),
    ("studio", "studio"),
    ("editionTitle", "editionTitle"),
];

const SECONDARY_TITLES: &[(&str, &str)] = &[
    ("genre", "By Genre"),
    ("year", "By Year"),
    ("decade", "By Decade"),
    ("contentRating", "By Content Rating"),
    ("collection", "By Collection"),
    ("director", "By Director"),
    ("writer", "By Writer"),
    ("producer", "By Producer"),
    // Measured: "By Starring Actor", not "By Actor".
    ("actor", "By Starring Actor"),
    ("country", "By Country"),
    ("studio", "By Studio"),
    ("label", "By Label"),
    ("network", "By Network"),
    ("editionTitle", "By Edition"),
    ("resolution", "By Resolution"),
    ("videoCodec", "By Video Codec"),
    ("audioCodec", "By Audio Codec"),
    ("subtitleCodec", "By Subtitle Codec"),
    ("audioLayout", "By Audio Layout"),
    ("audioLanguage", "By Audio Language"),
    ("subtitleLanguage", "By Subtitle Language"),
    ("location", "By Folder Location"),
    ("userRating", "By Rating"),
];

/// Cycle 44: 1.43.4 answers these four and 1.42.2 -- the build plexsim reports -- 404s.
const SECONDARY_MEDIA_1_43: &[&str] = &["videoCodec", "audioCodec", "subtitleCodec", "audioLayout"];
const SECONDARY_MEDIA: &[&str] = &["resolution", "audioLanguage", "subtitleLanguage"];

/// `/library/sections/{id}/userRating` -- a FIXED scale on kids and zen alike.
const USER_RATINGS: &[(&str, &str)] = &[
    ("10", "5 stars"),
    ("8", "4 stars"),
    ("6", "3 stars"),
    ("4", "2 stars"),
    ("2", "1 star"),
    ("-1", "Not rated"),
];

/// The axes of OTHER library types: an EMPTY secondary container with its own title2.
const CROSS_TYPE_AXES: &[(&str, &str)] = &[
    ("platform", ""),
    ("mood", "By Mood"),
    ("style", "By Style"),
    ("format", "By Format"),
    ("subformat", "By Type"),
    ("source", "By Source"),
    ("make", "By Camera Make"),
    ("model", "By Camera Model"),
    ("place", "By Place"),
    ("tag", "By Tag"),
    ("autotag", "By Autotag"),
    ("aperture", "By Aperture"),
    ("exposure", "By Shutter Speed"),
    ("iso", "By ISO"),
    ("lens", "By Lens"),
    ("sport", "By Sport"),
    ("team", "By Team"),
    ("channel", "By Channel"),
    ("publisher", "By Publisher"),
    ("narrator", "By Narrator"),
];

/// Cycle 45: the one axis that answers 200 to a `type=` it cannot parse.
const TYPE_BLIND_AXES: &[&str] = &["narrator"];

/// The media axes' label per resolution key, best first -- the reference's order.
const RESOLUTION_TITLES: &[(&str, &str)] = &[
    ("sd", "SD"),
    ("480", "480p"),
    ("576", "576p"),
    ("720", "720p"),
    ("1080", "1080p"),
    ("2k", "2K"),
    ("2.7k", "2.7K"),
    ("4k", "4K"),
    ("8k", "8K"),
    ("16k", "16K"),
];

/// Which bucket kind each axis's `/{axis}/{key}` path reads (cycles 16, 44, 45).
const AXIS_BUCKETS: &[(&str, &str)] = &[
    ("genre", "tag"),
    ("director", "tag"),
    ("writer", "tag"),
    ("producer", "tag"),
    ("actor", "tag"),
    ("country", "tag"),
    ("network", "tag"),
    ("collection", "tag"),
    ("label", "tag"),
    ("mood", "tag"),
    ("style", "tag"),
    ("format", "tag"),
    ("subformat", "tag"),
    ("source", "tag"),
    ("make", "tag"),
    ("model", "tag"),
    ("place", "tag"),
    ("tag", "tag"),
    ("similar", "tag"),
    ("year", "year"),
    ("decade", "decade"),
    ("contentRating", "contentRating"),
    ("resolution", "resolution"),
    ("userRating", "userRating"),
    ("rating", "userRating"),
];

/// Every tag column a bucket lookup reads (the tag TABLE is global, cycle 45).
const TAG_FIELDS: &[&str] = &[
    "Genre",
    "Collection",
    "Director",
    "Writer",
    "Producer",
    "Role",
    "Country",
    "Label",
    "Similar",
    "Network",
    "Mood",
    "Style",
];

fn lookup<'a>(table: &'a [(&'a str, &'a str)], name: &str) -> Option<&'a str> {
    table
        .iter()
        .find(|(key, _)| *key == name)
        .map(|(_, value)| *value)
}

/// Python's `urllib.parse.quote(text)` (`/` kept).
pub(super) fn py_quote(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'~' | b'/') {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// The first character of a sort title as the axis counts it: upper-cased, `#` for
/// an empty one.
fn first_upper(title: &str) -> String {
    match title.chars().next() {
        Some(first) => first.to_uppercase().collect(),
        None => "#".to_string(),
    }
}

fn ascii_non_letter(text: &str) -> bool {
    text.is_ascii() && !text.chars().all(|c| c.is_alphabetic())
}

impl Runtime {
    /// The rows a listing or a secondary axis is over, scoped by `?type=`. A type the
    /// library holds no rows of answers nothing; repeated `type=` parameters AND.
    pub(super) fn axis_typed_rows(
        &self,
        section_id: &str,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Vec<Value> {
        let types = pairs
            .iter()
            .filter(|(name, _)| name == "type")
            .map(|(_, value)| value.clone())
            .collect::<HashSet<_>>();
        if types.len() > 1 {
            return Vec::new();
        }
        let raw = query.get("type").map(String::as_str).unwrap_or("");
        let normal = raw
            .trim_start_matches('+')
            .parse::<i64>()
            .map(|number| number.to_string())
            .unwrap_or_else(|_| raw.to_string());
        let wanted = match normal.as_str() {
            "1" => Some("movie"),
            "2" => Some("show"),
            "3" => Some("season"),
            "4" => Some("episode"),
            _ => None,
        };
        let Some(wanted) = wanted else {
            if !raw.is_empty() {
                return Vec::new();
            }
            return self.section_rows(section_id);
        };
        let mut rows = self
            .catalog
            .items
            .iter()
            .filter(|row| {
                row.get("type").and_then(Value::as_str) == Some(wanted)
                    && string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                        == section_id
            })
            .cloned()
            .collect::<Vec<_>>();
        if wanted == "season" || wanted == "episode" {
            let show_title = |row: &Value| -> String {
                row.get("grandparentTitle")
                    .filter(|value| truthy(value))
                    .or_else(|| row.get("parentTitle").filter(|value| truthy(value)))
                    .map(string_value)
                    .unwrap_or_default()
            };
            rows.sort_by(|left, right| {
                let key = |row: &Value| {
                    (
                        show_title(row),
                        row.get("parentIndex").and_then(value_as_i64).unwrap_or(0),
                        row.get("index").and_then(value_as_i64).unwrap_or(0),
                    )
                };
                key(left).cmp(&key(right))
            });
        }
        rows
    }

    /// The listing grammar's filters over rows, or the status a real PMS refuses with.
    pub(super) fn axis_filters(
        &self,
        rows: Vec<Value>,
        pairs: &[(String, String)],
        section: &Value,
    ) -> Result<Vec<Value>, u16> {
        let section_type = section
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("movie");
        let kept = pairs
            .iter()
            .filter(|(name, _)| {
                !matches!(
                    name.as_str(),
                    "X-Plex-Container-Start" | "X-Plex-Container-Size" | "X-Plex-Language"
                )
            })
            .cloned()
            .collect::<Vec<_>>();
        self.filter_rows(rows, &kept, section_type)
    }

    /// A secondary list (By Genre, By Year, sorts, filters). Measured:
    /// `content=secondary`, `viewGroup=secondary`, no librarySection* pair, and a
    /// show library's carries `nocache`.
    pub(super) fn axis_container(
        &self,
        section: &Value,
        title2: Option<&str>,
        fields: Map<String, Value>,
    ) -> Value {
        let show = section.get("type").and_then(Value::as_str) == Some("show");
        let mut body = Map::new();
        body.insert("size".to_string(), json!(0));
        body.insert("allowSync".to_string(), json!(false));
        body.insert(
            "art".to_string(),
            json!(if show {
                "/:/resources/show-fanart.jpg"
            } else {
                "/:/resources/movie-fanart.jpg"
            }),
        );
        body.insert("content".to_string(), json!("secondary"));
        body.insert(
            "identifier".to_string(),
            json!("com.plexapp.plugins.library"),
        );
        body.insert("mediaTagPrefix".to_string(), json!(MEDIA_TAG_PREFIX));
        body.insert("mediaTagVersion".to_string(), json!(MEDIA_TAG_VERSION));
        body.insert(
            "thumb".to_string(),
            json!(if show {
                "/:/resources/show.png"
            } else {
                "/:/resources/movie.png"
            }),
        );
        body.insert(
            "title1".to_string(),
            section.get("title").cloned().unwrap_or(Value::Null),
        );
        body.insert("viewGroup".to_string(), json!("secondary"));
        body.insert("viewMode".to_string(), json!(65592));
        if show {
            body.insert("nocache".to_string(), json!(true));
        }
        if let Some(title2) = title2 {
            body.insert("title2".to_string(), json!(title2));
        }
        for (key, value) in fields {
            body.insert(key, value);
        }
        for key in ["Directory", "Metadata"] {
            if let Some(list) = body.get(key).and_then(Value::as_array) {
                let size = list.len();
                body.insert("size".to_string(), json!(size));
            }
        }
        json!({"MediaContainer": body})
    }

    /// The container a section bucket listing answers (`listing_container`): the
    /// library's triple, `title1`/`title2`, the grid projection and the paging.
    pub(super) fn axis_listing(
        &self,
        section_id: &str,
        section: &Value,
        rows: Vec<Value>,
        query: &HashMap<String, String>,
        title2: &str,
        show_nocache: bool,
    ) -> Response {
        self.section_listing_response(
            section_id,
            section,
            rows,
            query,
            title2,
            show_nocache,
            None,
            false,
        )
    }

    /// `listing_container` with the knobs its callers use: a `viewGroup` override and
    /// the unwatched subset's `?unwatched=1` on a row with children (cycle 50).
    #[allow(clippy::too_many_arguments)]
    pub(super) fn section_listing_response(
        &self,
        section_id: &str,
        section: &Value,
        rows: Vec<Value>,
        query: &HashMap<String, String>,
        title2: &str,
        show_nocache: bool,
        view_group: Option<&str>,
        unwatched_keys: bool,
    ) -> Response {
        let rows = rows
            .into_iter()
            .map(|row| {
                let mut projected = guid_gate(self.section_listing_row(&row), query);
                if unwatched_keys {
                    if let Some(key) = projected
                        .get("key")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                    {
                        if key.ends_with("/children") {
                            projected["key"] = json!(format!("{key}?unwatched=1"));
                        }
                    }
                }
                projected
            })
            .collect::<Vec<_>>();
        let (page, paged, start, total) = match page_window(rows, query) {
            Ok(window) => window,
            Err(status) => return Response::error(status, "plexsim: bad container parameter"),
        };
        let show = section.get("type").and_then(Value::as_str) == Some("show");
        let mut fields = Map::new();
        fields.insert(
            "librarySectionID".to_string(),
            section_id
                .parse::<i64>()
                .map(|id| json!(id))
                .unwrap_or_else(|_| json!(section_id)),
        );
        fields.insert(
            "librarySectionTitle".to_string(),
            section.get("title").cloned().unwrap_or(Value::Null),
        );
        fields.insert(
            "librarySectionUUID".to_string(),
            section.get("uuid").cloned().unwrap_or(Value::Null),
        );
        fields.insert(
            "title1".to_string(),
            section.get("title").cloned().unwrap_or(Value::Null),
        );
        fields.insert("title2".to_string(), json!(title2));
        fields.insert(
            "viewGroup".to_string(),
            view_group.map(|group| json!(group)).unwrap_or_else(|| {
                section
                    .get("type")
                    .cloned()
                    .unwrap_or_else(|| json!("movie"))
            }),
        );
        fields.insert("viewMode".to_string(), json!(65592));
        fields.insert(
            "art".to_string(),
            json!(if show {
                "/:/resources/show-fanart.jpg"
            } else {
                "/:/resources/movie-fanart.jpg"
            }),
        );
        fields.insert(
            "thumb".to_string(),
            json!(if show {
                "/:/resources/show.png"
            } else {
                "/:/resources/movie.png"
            }),
        );
        fields.insert("allowSync".to_string(), json!(true));
        if paged {
            fields.insert("offset".to_string(), json!(start));
            fields.insert("totalSize".to_string(), json!(total));
        }
        if show && show_nocache {
            fields.insert("nocache".to_string(), json!(true));
        }
        fields.insert("Metadata".to_string(), Value::Array(page));
        Response::json(200, self.container(fields, true))
    }

    /// `type=` on an axis: a whole number, `status` for one that does not parse and
    /// `empty` for an empty one.
    fn axis_type_refusal(query: &HashMap<String, String>, status: u16, empty: u16) -> Option<u16> {
        let raw = query.get("type")?;
        if raw.trim().is_empty() {
            return Some(empty);
        }
        if !int_param(raw, Some(32)) {
            return Some(status);
        }
        None
    }

    /// `/library/sections/{id}/{leaf}` for every axis, or None when `leaf` is not one.
    pub(super) fn route_section_axis(
        &self,
        section_id: &str,
        section: &Value,
        leaf: &str,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Option<Response> {
        let is_tag = lookup(SECONDARY_TAGS, leaf).is_some();
        let is_field = lookup(SECONDARY_FIELDS, leaf).is_some();
        if leaf == "firstCharacter" {
            if let Some(status) = Self::axis_type_refusal(query, 500, 500) {
                return Some(Response::error(status, "plexsim: bad type"));
            }
            let rows = self.axis_typed_rows(section_id, query, pairs);
            let rows = match self.axis_filters(rows, pairs, section) {
                Ok(rows) => rows,
                Err(status) => return Some(Response::error(status, "plexsim: bad filter")),
            };
            // Cycle 16: the AXIS counts a season or an episode under its SHOW's sort
            // title; cycle 41: every ASCII non-letter is counted under `#`, while
            // non-ASCII punctuation keeps its own key.
            let mut counts: Vec<(String, i64)> = Vec::new();
            for row in rows {
                let basis = self.first_character_basis(&row);
                let title = basis
                    .get("titleSort")
                    .filter(|value| truthy(value))
                    .or_else(|| basis.get("title").filter(|value| truthy(value)))
                    .map(string_value)
                    .unwrap_or_default();
                let mut first = first_upper(&title);
                if ascii_non_letter(&first) {
                    first = "#".to_string();
                }
                match counts.iter_mut().find(|(key, _)| *key == first) {
                    Some((_, count)) => *count += 1,
                    None => counts.push((first, 1)),
                }
            }
            counts.sort_by(|left, right| left.0.cmp(&right.0));
            let entries = counts
                .into_iter()
                .map(|(key, size)| json!({"size": size, "key": py_quote(&key), "title": key}))
                .collect::<Vec<_>>();
            return Some(Response::json(
                200,
                self.axis_container(
                    section,
                    Some("By First Letter"),
                    Map::from_iter([("Directory".to_string(), Value::Array(entries))]),
                ),
            ));
        }
        if is_tag || is_field {
            // Measured on both servers: an axis over the rows refuses a `type=` it
            // cannot parse -- 500 everywhere, and 400 on `year` alone.
            let refusal = if leaf == "year" { 400 } else { 500 };
            if let Some(status) = Self::axis_type_refusal(query, refusal, refusal) {
                return Some(Response::error(status, "plexsim: bad type"));
            }
            return Some(self.secondary_directory(section_id, section, leaf, query, pairs));
        }
        if SECONDARY_MEDIA_1_43.contains(&leaf) {
            return Some(Response::error(404, "plexsim: not found"));
        }
        if SECONDARY_MEDIA.contains(&leaf) {
            return Some(self.media_axis(section_id, section, leaf, pairs));
        }
        if leaf == "userRating" || leaf == "rating" {
            // Cycle 14: a real axis and a FIXED one -- the same six rows whatever
            // ratings the library holds; it ignores `type=` the way a media axis does.
            let entries = USER_RATINGS
                .iter()
                .map(|(key, title)| json!({"key": key, "title": title}))
                .collect::<Vec<_>>();
            return Some(Response::json(
                200,
                self.axis_container(
                    section,
                    lookup(SECONDARY_TITLES, "userRating"),
                    Map::from_iter([("Directory".to_string(), Value::Array(entries))]),
                ),
            ));
        }
        if let Some(title) = lookup(CROSS_TYPE_AXES, leaf) {
            // Measured on both references: an EMPTY secondary container, and a
            // `type=` that does not parse is a 500 -- except on `narrator`.
            if !TYPE_BLIND_AXES.contains(&leaf) {
                if let Some(status) = Self::axis_type_refusal(query, 500, 500) {
                    return Some(Response::error(status, "plexsim: bad type"));
                }
            }
            return Some(Response::json(
                200,
                self.axis_container(
                    section,
                    Some(title),
                    Map::from_iter([("Directory".to_string(), Value::Array(Vec::new()))]),
                ),
            ));
        }
        None
    }

    /// The row whose sort title the `firstCharacter` AXIS counts a row under: a
    /// season and an episode count under their SHOW (cycle 16).
    fn first_character_basis(&self, row: &Value) -> Value {
        let key = match row.get("type").and_then(Value::as_str) {
            Some("season") => row.get("parentRatingKey"),
            Some("episode") => row.get("grandparentRatingKey"),
            _ => return row.clone(),
        };
        key.map(string_value)
            .and_then(|key| self.catalog.get(&key).cloned())
            .unwrap_or_else(|| row.clone())
    }

    /// The bucket a row falls in when the LISTING filters on it: the row's OWN
    /// STORED sort title (cycle 18: empty for a generated season title), first
    /// character upper-cased, `#` for none.
    fn first_character_bucket(row: &Value) -> String {
        let stored = row
            .as_object()
            .map(|object| crate::hierarchy::stored_sort_title(object))
            .unwrap_or_default();
        first_upper(&stored)
    }

    /// `/library/sections/{id}/{genre,year,director,...}`. A tag axis keys its entries
    /// by TAG ID and a field axis by the value itself; only a `genre` entry carries a
    /// `type`; an actor entry carries a thumb (a placeholder when the tag has none,
    /// cycle 52); the ACTOR axis is the union of each item's FIRST THREE roles
    /// (cycle 14); a `studio` entry leaves its fastKey raw and double-encodes its key;
    /// the contentRating axis alone ends with a `None` bucket.
    fn secondary_directory(
        &self,
        section_id: &str,
        section: &Value,
        leaf: &str,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Response {
        let rows = self.axis_typed_rows(section_id, query, pairs);
        let mut rows = match self.axis_filters(rows, pairs, section) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad filter"),
        };
        if leaf == "label" {
            // Cycle 15: a label written onto a COLLECTION is in this axis too.
            rows.extend(self.collection_rows_for_axis(section_id));
        }
        if matches!(leaf, "director" | "writer" | "producer")
            && section.get("type").and_then(Value::as_str) == Some("show")
        {
            // Cycle 50: a SHOW library's person axes are built from the EPISODES.
            rows = rows
                .into_iter()
                .flat_map(|row| {
                    if matches!(
                        row.get("type").and_then(Value::as_str),
                        Some("show" | "season")
                    ) {
                        self.leaves(&key_of(&row).unwrap_or_default())
                    } else {
                        vec![row]
                    }
                })
                .collect();
        }
        let mut entries = Vec::new();
        if let Some(element) = lookup(SECONDARY_TAGS, leaf) {
            let mut seen: Vec<(String, Value)> = Vec::new();
            for row in &rows {
                let tags = row
                    .get(element)
                    .and_then(Value::as_array)
                    .cloned()
                    .unwrap_or_default();
                let tags = if leaf == "actor" {
                    tags.into_iter().take(3).collect::<Vec<_>>()
                } else {
                    tags
                };
                for tag in tags {
                    let name = tag.get("tag").map(string_value).unwrap_or_default();
                    if name.is_empty() || seen.iter().any(|(known, _)| *known == name) {
                        continue;
                    }
                    seen.push((name, tag));
                }
            }
            seen.sort_by(|left, right| left.0.cmp(&right.0));
            for (name, tag) in seen {
                let id = tag
                    .get("id")
                    .map(py_str)
                    .unwrap_or_else(|| "None".to_string());
                let mut entry = Map::new();
                entry.insert(
                    "fastKey".to_string(),
                    json!(format!("/library/sections/{section_id}/all?{leaf}={id}")),
                );
                if let Some(thumb) = tag.get("thumb").filter(|value| truthy(value)) {
                    entry.insert("thumb".to_string(), thumb.clone());
                } else if leaf == "actor" {
                    entry.insert(
                        "thumb".to_string(),
                        json!("/:/resources/actor-icon.png?t=-1"),
                    );
                }
                entry.insert("key".to_string(), json!(id));
                entry.insert("title".to_string(), json!(name));
                if leaf == "genre" {
                    entry.insert("type".to_string(), json!(leaf));
                }
                entries.push(Value::Object(entry));
            }
        } else if let Some(field) = lookup(SECONDARY_FIELDS, leaf) {
            let mut seen: Vec<(i64, String)> = Vec::new();
            for row in &rows {
                let Some(value) = row
                    .get(field)
                    .filter(|value| !value.is_null() && *value != &json!(""))
                else {
                    continue;
                };
                let text = if leaf == "decade" {
                    match value_as_i64(value) {
                        Some(year) => (year.div_euclid(10) * 10).to_string(),
                        None => continue,
                    }
                } else {
                    py_str(value)
                };
                if !seen.iter().any(|(_, known)| *known == text) {
                    let number = text.parse::<i64>().unwrap_or(0);
                    seen.push((number, text));
                }
            }
            if matches!(leaf, "year" | "decade") {
                // Python sorts the STRINGS, newest first.
                seen.sort_by(|left, right| right.1.cmp(&left.1));
            } else {
                seen.sort_by(|left, right| left.1.cmp(&right.1));
            }
            for (_, value) in seen {
                let once = py_quote(&value);
                entries.push(json!({
                    "fastKey": format!(
                        "/library/sections/{section_id}/all?{leaf}={}",
                        if leaf == "studio" { value.clone() } else { once.clone() }
                    ),
                    "key": if leaf == "studio" { py_quote(&once) } else { once.clone() },
                    "title": if leaf == "decade" { format!("{value}s") } else { value.clone() },
                }));
            }
        }
        if leaf == "contentRating"
            && rows
                .iter()
                .any(|row| !row.get("contentRating").is_some_and(truthy))
        {
            // Measured on both servers: the contentRating axis -- and only that axis --
            // ends with a `None` bucket whose fastKey is `?contentRating=None`.
            entries.push(json!({
                "fastKey": format!("/library/sections/{section_id}/all?contentRating=None"),
                "key": "None",
                "title": "None",
            }));
        }
        let mut container = self.axis_container(
            section,
            lookup(SECONDARY_TITLES, leaf),
            Map::from_iter([("Directory".to_string(), Value::Array(entries))]),
        );
        if leaf == "actor" {
            // Measured across all 26 axes: every one carries `content: "secondary"`
            // EXCEPT `actor`.
            if let Some(body) = container
                .get_mut("MediaContainer")
                .and_then(Value::as_object_mut)
            {
                body.shift_remove("content");
            }
        }
        Response::json(200, container)
    }

    /// The collections of one library as rows a tag axis can read.
    fn collection_rows_for_axis(&self, section_id: &str) -> Vec<Value> {
        self.collections
            .values()
            .filter(|collection| collection.section == section_id)
            .map(|collection| self.dynamic_collection_row(collection))
            .collect()
    }

    /// `/library/sections/{id}/{resolution,audioLanguage,subtitleLanguage}`: the axes
    /// read off the Media and its Streams. Their entries carry `key` and a LABEL
    /// `title` and NO `fastKey`; the rows are type-blind (cycle 13) but run the
    /// listing's filters (cycle 54); a show library reads its LEAVES (cycle 20).
    fn media_axis(
        &self,
        section_id: &str,
        section: &Value,
        leaf: &str,
        pairs: &[(String, String)],
    ) -> Response {
        let rows = self.section_rows(section_id);
        let without_type = pairs
            .iter()
            .filter(|(name, _)| name != "type")
            .cloned()
            .collect::<Vec<_>>();
        let rows = match self.axis_filters(rows, &without_type, section) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad filter"),
        };
        let mut seen: Vec<(String, Option<String>)> = Vec::new();
        for row in &rows {
            let mut media_rows = row
                .get("Media")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            if media_rows.is_empty()
                && matches!(
                    row.get("type").and_then(Value::as_str),
                    Some("show" | "season")
                )
            {
                media_rows = self
                    .leaves(&key_of(row).unwrap_or_default())
                    .iter()
                    .flat_map(|leaf| {
                        leaf.get("Media")
                            .and_then(Value::as_array)
                            .cloned()
                            .unwrap_or_default()
                    })
                    .collect();
            }
            for media in &media_rows {
                let mut found: Vec<(Option<String>, Option<String>)> = Vec::new();
                if leaf == "resolution" {
                    found.push((media.get("videoResolution").map(py_str), None));
                } else {
                    let wanted = if leaf == "audioLanguage" { 2 } else { 3 };
                    for part in media
                        .get("Part")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                    {
                        for stream in part
                            .get("Stream")
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                        {
                            if stream.get("streamType").and_then(Value::as_i64) != Some(wanted) {
                                continue;
                            }
                            found.push((
                                stream
                                    .get("languageTag")
                                    .filter(|v| !v.is_null())
                                    .map(py_str),
                                stream.get("language").filter(|v| !v.is_null()).map(py_str),
                            ));
                        }
                    }
                }
                for (value, label) in found {
                    let Some(value) = value.filter(|value| !value.is_empty() && value != "None")
                    else {
                        continue;
                    };
                    if !seen.iter().any(|(known, _)| *known == value) {
                        seen.push((value, label));
                    }
                }
            }
        }
        let entries = if leaf == "resolution" {
            let rank = |value: &str| {
                RESOLUTION_TITLES
                    .iter()
                    .position(|(key, _)| *key == value)
                    .map(|index| -(index as i64))
                    .unwrap_or(1)
            };
            let mut keys = seen.iter().map(|(key, _)| key.clone()).collect::<Vec<_>>();
            keys.sort_by_key(|key| rank(key));
            keys.into_iter()
                .map(|key| {
                    let title = lookup(RESOLUTION_TITLES, &key)
                        .map(str::to_string)
                        .unwrap_or(key.clone());
                    json!({"key": key, "title": title})
                })
                .collect::<Vec<_>>()
        } else {
            seen.sort_by(|left, right| left.0.cmp(&right.0));
            seen.into_iter()
                .map(|(key, label)| {
                    let title = label
                        .filter(|label| !label.is_empty())
                        .unwrap_or(key.clone());
                    json!({"key": key, "title": title})
                })
                .collect::<Vec<_>>()
        };
        Response::json(
            200,
            self.axis_container(
                section,
                lookup(SECONDARY_TITLES, leaf),
                Map::from_iter([("Directory".to_string(), Value::Array(entries))]),
            ),
        )
    }

    /// `/library/sections/{id}/firstCharacter/{key}` -- a real LISTING, not the axis:
    /// `title2` reads "Start with '<key>'", the rows filter on the row's OWN stored
    /// sort title, and the listing grammar applies on top (cycle 16).
    pub(super) fn first_character_listing(
        &self,
        section_id: &str,
        section: &Value,
        raw_key: &str,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Response {
        let wanted = percent_decode(raw_key);
        if let Some(status) = Self::axis_type_refusal(query, 500, 500) {
            return Response::error(status, "plexsim: bad type");
        }
        let rows = self
            .axis_typed_rows(section_id, query, pairs)
            .into_iter()
            .filter(|row| {
                let bucket = Self::first_character_bucket(row);
                bucket == wanted || (wanted == "#" && ascii_non_letter(&bucket))
            })
            .collect::<Vec<_>>();
        let rows = match self.axis_filters(rows, pairs, section) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad filter"),
        };
        let sort = query
            .get("sort")
            .filter(|sort| !sort.is_empty())
            .map(String::as_str)
            .unwrap_or("titleSort");
        let rows = match self.sort_rows(rows, Some(sort)) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: unsupported sort"),
        };
        self.axis_listing(
            section_id,
            section,
            rows,
            query,
            &format!("Start with '{wanted}'"),
            true,
        )
    }

    /// `/library/sections/{id}/{axis}/{key}` -- the rows behind one axis entry
    /// (cycle 16). A tag axis keys on the TAG ID across every tag column and 404s an
    /// id the library does not hold; `year`/`decade`/`userRating` take a non-negative
    /// integer; `contentRating` and `resolution` take anything.
    pub(super) fn axis_bucket(
        &self,
        section_id: &str,
        section: &Value,
        leaf: &str,
        raw_key: &str,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Option<Response> {
        let kind = lookup(AXIS_BUCKETS, leaf)?;
        let wanted = percent_decode(raw_key);
        let mut rows = self.axis_typed_rows(section_id, query, pairs);
        let title2;
        match kind {
            "tag" => {
                rows.extend(self.collection_rows_for_axis(section_id));
                let title = rows.iter().find_map(|row| {
                    TAG_FIELDS.iter().find_map(|column| {
                        row.get(*column)
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                            .find(|tag| {
                                tag.get("id").map(py_str).as_deref() == Some(wanted.as_str())
                            })
                            .and_then(|tag| tag.get("tag").map(py_str))
                    })
                });
                let Some(title) = title else {
                    return Some(Response::error(404, "plexsim: not found"));
                };
                rows.retain(|row| {
                    TAG_FIELDS.iter().any(|column| {
                        row.get(*column)
                            .and_then(Value::as_array)
                            .into_iter()
                            .flatten()
                            .any(|tag| {
                                tag.get("id").map(py_str).as_deref() == Some(wanted.as_str())
                            })
                    })
                });
                title2 = title;
            }
            "year" | "decade" | "userRating" => {
                if !wanted.bytes().all(|b| b.is_ascii_digit()) || wanted.is_empty() {
                    return Some(Response::error(404, "plexsim: not found"));
                }
                let number = wanted.parse::<i64>().unwrap_or(0);
                if kind == "year" {
                    rows.retain(|row| {
                        row.get("year").filter(|v| truthy(v)).map(py_str).as_deref()
                            == Some(wanted.as_str())
                    });
                    title2 = wanted.clone();
                } else if kind == "decade" {
                    rows.retain(|row| {
                        row.get("year")
                            .and_then(value_as_i64)
                            .filter(|year| *year != 0)
                            .is_some_and(|year| number <= year && year < number + 10)
                    });
                    title2 = wanted.clone();
                } else {
                    let column = if leaf == "rating" {
                        "rating"
                    } else {
                        "userRating"
                    };
                    if number == 0 {
                        rows.retain(|row| row.get(column).is_none_or(Value::is_null));
                    } else {
                        let low = (number - number % 2) - 1;
                        rows.retain(|row| {
                            row.get(column)
                                .and_then(Value::as_f64)
                                .is_some_and(|value| {
                                    low as f64 <= value && value < (low + 2) as f64
                                })
                        });
                    }
                    title2 = format!("{wanted} Star");
                }
            }
            "contentRating" => {
                if wanted == "None" {
                    rows.retain(|row| !row.get("contentRating").is_some_and(truthy));
                } else {
                    rows.retain(|row| {
                        row.get("contentRating")
                            .filter(|v| truthy(v))
                            .map(py_str)
                            .as_deref()
                            == Some(wanted.as_str())
                    });
                }
                title2 = format!("Rated {wanted}");
            }
            _ => {
                let known = ["4k", "1080", "720", "576", "480", "sd"];
                let media_of = |row: &Value| {
                    row.get("Media")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default()
                };
                if known.contains(&wanted.as_str()) {
                    rows.retain(|row| {
                        media_of(row).iter().any(|media| {
                            media
                                .get("videoResolution")
                                .filter(|v| truthy(v))
                                .map(py_str)
                                .as_deref()
                                == Some(wanted.as_str())
                        })
                    });
                } else {
                    rows.retain(|row| {
                        media_of(row)
                            .iter()
                            .any(|media| media.get("videoResolution").is_some_and(truthy))
                    });
                }
                title2 = wanted.clone();
            }
        }
        let rows = match self.axis_filters(rows, pairs, section) {
            Ok(rows) => rows,
            Err(status) => return Some(Response::error(status, "plexsim: bad filter")),
        };
        let sort = query
            .get("sort")
            .filter(|sort| !sort.is_empty())
            .map(String::as_str)
            .unwrap_or("titleSort");
        let rows = match self.sort_rows(rows, Some(sort)) {
            Ok(rows) => rows,
            Err(status) => return Some(Response::error(status, "plexsim: unsupported sort")),
        };
        Some(self.axis_listing(section_id, section, rows, query, &title2, true))
    }

    /// `/library/sections/{id}/unwatched` -- a LISTING titled "Unplayed", typed like a
    /// listing and answering ONLY the library's own top-level type (cycle 20), running
    /// the listing's filters (cycle 54), ordered by titleSort whatever `sort=` says once
    /// it validates (cycles 50, 55), and carrying `nocache` on a show library (cycle 16).
    pub(super) fn unwatched_listing(
        &self,
        section_id: &str,
        section: &Value,
        query: &HashMap<String, String>,
        pairs: &[(String, String)],
    ) -> Response {
        if let Some(status) = Self::axis_type_refusal(query, 500, 500) {
            return Response::error(status, "plexsim: bad type");
        }
        let own = if section.get("type").and_then(Value::as_str) == Some("show") {
            2
        } else {
            1
        };
        let asked = query
            .get("type")
            .and_then(|value| value.trim_start_matches('+').parse::<i64>().ok())
            .unwrap_or(own);
        let rows = if asked != own {
            Vec::new()
        } else {
            self.axis_typed_rows(section_id, query, pairs)
                .into_iter()
                // Cycle 83, measured on zen: a SHOW is on this list by its LEAVES (507
                // of 509, exactly `unwatchedLeaves=1`), not by its own `viewCount`.
                .filter(|row| self.has_unwatched(row))
                .collect()
        };
        let rows = match self.axis_filters(rows, pairs, section) {
            Ok(rows) => rows,
            Err(status) => return Response::error(status, "plexsim: bad filter"),
        };
        if let Some(sort) = query.get("sort").filter(|sort| !sort.is_empty()) {
            if let Err(status) = self.sort_rows(rows.clone(), Some(sort)) {
                return Response::error(status, "plexsim: unsupported sort");
            }
        }
        let rows = self
            .sort_rows(rows.clone(), Some("titleSort"))
            .unwrap_or(rows);
        let show = section.get("type").and_then(Value::as_str) == Some("show");
        let view_group = (show && asked != own).then_some("episode");
        self.section_listing_response(
            section_id, section, rows, query, "Unplayed", true, view_group, true,
        )
    }
}
