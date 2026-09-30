//! Continue Watching and On Deck: which rows are resuming, which are next up, and
//! the shelves and lists that read them.  Ported from `server.py`'s
//! `continue_watching`, `resuming`, `merged_continue_watching`, `on_home` and
//! `in_directories`, whose docstrings hold the measurements.

use super::*;

impl Runtime {
    /// The library's Visibility setting: 0 everywhere, 1 off Home, 2 off search too.
    pub(super) fn section_visibility(&self, section_id: &str) -> i64 {
        self.section_prefs
            .get(&(section_id.to_string(), "hidden".to_string()))
            .and_then(|raw| raw.trim().parse::<i64>().ok())
            .unwrap_or(0)
    }

    /// `rows` without the libraries Edit Library > Visibility keeps off Home
    /// (cycle 58: `hidden=1` and `2` empty every Home shelf of that library).
    pub(super) fn on_home(&self, rows: Vec<Value>) -> Vec<Value> {
        rows.into_iter()
            .filter(|row| {
                self.section_visibility(&string_value(
                    row.get("librarySectionID").unwrap_or(&Value::Null),
                )) < 1
            })
            .collect()
    }

    /// `rows` narrowed to the libraries `contentDirectoryID` named, off Home's hidden.
    pub(super) fn in_directories(
        &self,
        rows: Vec<Value>,
        wanted: &Option<HashSet<String>>,
    ) -> Vec<Value> {
        let rows = self.on_home(rows);
        match wanted {
            None => rows,
            Some(wanted) => rows
                .into_iter()
                .filter(|row| {
                    wanted.contains(&string_value(
                        row.get("librarySectionID").unwrap_or(&Value::Null),
                    ))
                })
                .collect(),
        }
    }

    fn hidden_from_continue(&self, row: &Value) -> bool {
        key_of(row).is_some_and(|key| self.hidden_continue.contains(&key))
    }

    /// Rows for Continue Watching and On Deck, newest first.
    ///
    /// A movie is there while it has a resume point.  A SHOW contributes one row --
    /// its most recently viewed in-progress episode (cycle 14), or with none in
    /// progress the episode after the most recently viewed watched one (cycle 55,
    /// from the binary's SQL).  Removing any of its episodes from Continue Watching
    /// hides the show's row.  A next-up whose anchor was scrobbled rather than
    /// played, in a library with no playback completion, is marked
    /// `_nextUpFromScrobble` (cycles 57-59) for the section shelves to drop.
    pub(super) fn continue_watching(&self, include_hidden: bool) -> Vec<Value> {
        let truthy = |row: &Value, name: &str| match row.get(name) {
            None | Some(Value::Null) => false,
            Some(Value::Number(number)) => number.as_f64().unwrap_or(0.0) != 0.0,
            Some(Value::String(text)) => !text.is_empty(),
            Some(Value::Bool(flag)) => *flag,
            Some(_) => true,
        };
        let stamp_of = |row: &Value| row.get("lastViewedAt").and_then(Value::as_i64).unwrap_or(0);
        let mut rows: Vec<(i64, Value)> = self
            .catalog
            .items
            .iter()
            .filter(|row| {
                truthy(row, "viewOffset")
                    && row.get("type").and_then(Value::as_str) == Some("movie")
                    && (include_hidden || !self.hidden_from_continue(row))
            })
            .map(|row| (stamp_of(row), row.clone()))
            .collect();
        let mut order: Vec<String> = Vec::new();
        let mut shows: HashMap<String, Vec<Value>> = HashMap::new();
        for row in &self.catalog.items {
            if row.get("type").and_then(Value::as_str) == Some("episode") {
                let show = string_value(row.get("grandparentRatingKey").unwrap_or(&Value::Null));
                if !shows.contains_key(&show) {
                    order.push(show.clone());
                }
                shows.entry(show).or_default().push(row.clone());
            }
        }
        let played_libraries: HashSet<String> = shows
            .values()
            .flatten()
            .filter(|row| key_of(row).is_some_and(|key| self.viewed_in_session.contains(&key)))
            .map(|row| string_value(row.get("librarySectionID").unwrap_or(&Value::Null)))
            .collect();
        for show in order {
            let mut episodes = shows.remove(&show).unwrap_or_default();
            if !include_hidden && episodes.iter().any(|row| self.hidden_from_continue(row)) {
                continue;
            }
            episodes.sort_by_key(|row| {
                (
                    row.get("parentIndex").and_then(Value::as_i64).unwrap_or(0),
                    row.get("index").and_then(Value::as_i64).unwrap_or(0),
                )
            });
            // `max` keeps the FIRST maximal element in Python; iterate in order and
            // replace only on a strictly greater stamp.
            let mut chosen: Option<Value> = None;
            for row in episodes.iter().filter(|row| truthy(row, "viewOffset")) {
                if chosen
                    .as_ref()
                    .map_or(true, |best| stamp_of(row) > stamp_of(best))
                {
                    chosen = Some(row.clone());
                }
            }
            let mut anchor_stamp: Option<i64> = None;
            let mut scrobbled = false;
            if chosen.is_none() {
                let mut anchor: Option<usize> = None;
                for (index, row) in episodes.iter().enumerate() {
                    if truthy(row, "viewCount")
                        && anchor.map_or(true, |best| stamp_of(row) > stamp_of(&episodes[best]))
                    {
                        anchor = Some(index);
                    }
                }
                if let Some(anchor) = anchor {
                    let anchor_row = &episodes[anchor];
                    anchor_stamp = anchor_row.get("lastViewedAt").and_then(Value::as_i64);
                    scrobbled = !played_libraries.contains(&string_value(
                        anchor_row.get("librarySectionID").unwrap_or(&Value::Null),
                    ));
                    let parent_index = anchor_row.get("parentIndex").cloned();
                    chosen = episodes[anchor + 1..]
                        .iter()
                        .find(|row| {
                            !truthy(row, "viewCount")
                                && !truthy(row, "viewOffset")
                                && (row.get("parentIndex").cloned() == parent_index
                                    || row.get("index").and_then(Value::as_i64).unwrap_or(0) > 0)
                        })
                        .cloned();
                }
            }
            if let Some(mut chosen) = chosen {
                if let (Some(stamp), Some(object)) = (anchor_stamp, chosen.as_object_mut()) {
                    if stamp != 0 {
                        object.insert("lastViewedAt".to_string(), json!(stamp));
                    }
                }
                if scrobbled && chosen.get("viewOffset").is_none() {
                    if let Some(object) = chosen.as_object_mut() {
                        object.insert("_nextUpFromScrobble".to_string(), json!(true));
                    }
                }
                let stamp = episodes.iter().map(stamp_of).max().unwrap_or(0);
                rows.push((stamp, chosen));
            }
        }
        // Python's sort is stable, and so is `sort_by_key`.
        rows.sort_by_key(|(stamp, _)| -stamp);
        rows.into_iter().map(|(_, row)| row).collect()
    }

    /// Rows for CONTINUE WATCHING, which is not On Deck: only those with a stored
    /// `viewOffset` (cycle 25).
    pub(super) fn resuming(&self) -> Vec<Value> {
        self.continue_watching(false)
            .into_iter()
            .filter(|row| row.get("viewOffset").and_then(Value::as_f64).unwrap_or(0.0) != 0.0)
            .collect()
    }

    /// The MERGED hub, `/hubs/continueWatching` and its `/items` leaf (cycle 57):
    /// the in-progress rows and On Deck's next-up rows, newest view first.
    pub(super) fn merged_continue_watching(&self) -> Vec<Value> {
        let mut rows = self.continue_watching(false);
        rows.sort_by_key(|row| -row.get("lastViewedAt").and_then(Value::as_i64).unwrap_or(0));
        rows
    }

    /// Whether `/hubs` and `/hubs/promoted` drop the On Deck shelf: exactly when
    /// every episode on deck is also in Continue Watching (cycle 25).
    pub(super) fn ondeck_shelf_hidden(&self) -> bool {
        let deck: HashSet<String> = self
            .continue_watching(false)
            .iter()
            .filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"))
            .filter_map(key_of)
            .collect();
        let resuming: HashSet<String> = self.resuming().iter().filter_map(key_of).collect();
        !deck.is_empty() && deck == resuming
    }

    /// Cycle 53: `/hubs/continueWatching` and its `/items` leaf carry `includedAt`
    /// directly after `lastViewedAt`, equal to it.
    pub(super) fn with_included_at(row: Value) -> Value {
        let Value::Object(object) = row else {
            return row;
        };
        if !object.contains_key("lastViewedAt") {
            return Value::Object(object);
        }
        let mut out = Map::new();
        for (key, value) in object {
            let stamp = (key == "lastViewedAt").then(|| value.clone());
            out.insert(key, value);
            if let Some(stamp) = stamp {
                out.insert("includedAt".to_string(), stamp);
            }
        }
        Value::Object(out)
    }

    /// A hub response: `size`, `allowSync` (TRUE, cycle 14) and `identifier` only.
    pub(super) fn hub_container(&self, fields: Map<String, Value>) -> Value {
        let mut body = Map::new();
        body.insert("size".to_string(), json!(0));
        body.insert("allowSync".to_string(), json!(true));
        body.insert(
            "identifier".to_string(),
            json!("com.plexapp.plugins.library"),
        );
        let hub_count = fields.get("Hub").and_then(Value::as_array).map(Vec::len);
        for (key, value) in fields {
            body.insert(key, value);
        }
        if let Some(count) = hub_count {
            body.insert("size".to_string(), json!(count));
        }
        Value::Object(Map::from_iter([(
            "MediaContainer".to_string(),
            Value::Object(body),
        )]))
    }
}

/// The library ids `contentDirectoryID=` scopes a hub request to, or None: an EMPTY
/// value scopes nothing, the match is an exact string, and a comma list selects
/// several (cycle 28).
pub(super) fn content_directories(query: &HashMap<String, String>) -> Option<HashSet<String>> {
    let raw = query.get("contentDirectoryID")?;
    if raw.is_empty() {
        return None;
    }
    Some(
        raw.split(',')
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .collect(),
    )
}

/// Cycle 54: an integer query parameter as a real PMS parses it -- an optional sign
/// and digits, nothing else, inside the signed range of `bits`; `None` takes any size.
pub(super) fn int_param(value: &str, bits: Option<u32>) -> bool {
    let digits = value.strip_prefix(['+', '-']).unwrap_or(value);
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return false;
    }
    let Some(bits) = bits else {
        return true;
    };
    match value.trim_start_matches('+').parse::<i128>() {
        Ok(number) => {
            let bound = 1_i128 << (bits - 1);
            -bound <= number && number < bound
        }
        Err(_) => false,
    }
}

/// Whether a real PMS reads this value as one of its 0/1 options: `+?0*[01]`
/// (cycle 29).
pub(super) fn bool_option(value: &str) -> bool {
    let text = value.strip_prefix('+').unwrap_or(value);
    !text.is_empty()
        && text.bytes().all(|byte| byte.is_ascii_digit())
        && matches!(text.trim_start_matches('0'), "" | "1")
}

/// Whether `excludeContinueWatching=` drops the two resume shelves: the value parses
/// as the integer 1 (cycle 28; `+1` too, cycle 55).
pub(super) fn exclude_continue_watching(query: &HashMap<String, String>) -> bool {
    let raw = query
        .get("excludeContinueWatching")
        .map(String::as_str)
        .unwrap_or("");
    int_param(raw, None)
        && !raw.starts_with('-')
        && raw.trim_start_matches('+').trim_start_matches('0') == "1"
}

/// A shelf with no `count=` carries at most SIX rows (measured on zen).
const SHELF_COUNT: i64 = 6;
/// `/hubs/home/recentlyAdded?type=2` caps at 50 (cycle 28, zen).
const TV_RECENT_CAP: usize = 50;

/// The global hub list, measured identically on both real servers: eight shelves in
/// this order, all promoted. (identifier, title, key, kind, style)
const GLOBAL_HUBS: &[(&str, &str, &str, &str, &str)] = &[
    (
        "home.continue",
        "Continue Watching",
        "/hubs/home/continueWatching",
        "mixed",
        "hero",
    ),
    (
        "home.ondeck",
        "On Deck",
        "/hubs/home/onDeck",
        "episode",
        "shelf",
    ),
    (
        "home.movies.recent",
        "Recently Added Movies",
        "/hubs/home/recentlyAdded?type=1",
        "movie",
        "shelf",
    ),
    (
        "home.television.recent",
        "Recently Added TV",
        "/hubs/home/recentlyAdded?type=2",
        "mixed",
        "shelf",
    ),
    (
        "home.music.recent",
        "Recently Added Music",
        "/hubs/home/recentlyAdded?type=8",
        "album",
        "shelf",
    ),
    (
        "home.photos.recent",
        "Recently Added Photos",
        "/hubs/home/recentlyAdded?type=13",
        "photo",
        "shelf",
    ),
    (
        "home.videos.recent",
        "Recently Added Videos",
        "/hubs/home/recentlyAdded?type=1&personal=1",
        "clip",
        "shelf",
    ),
    (
        "home.playlists",
        "Recent Playlists",
        "/playlists/all?type=15&sort=lastViewedAt:desc&playlistType=video,audio",
        "playlist",
        "shelf",
    ),
];

/// Cycle 54: `sectionID` on the Recently Added list is a NUMBER -- `01` is section 1
/// and `0` selects nothing, while a value that is not an int32, and a negative one,
/// is ignored.
pub(super) fn section_number(value: Option<&String>) -> Option<i64> {
    let value = value?;
    if !int_param(value, Some(32)) {
        return None;
    }
    let number = value.trim_start_matches('+').parse::<i64>().ok()?;
    (number >= 0).then_some(number)
}

impl Runtime {
    /// One shelf, capped at `count` with `more` saying whether it was cut.  `rows`
    /// are CATALOG rows; a show on a `mixed` shelf (or every row when `full`) is the
    /// full row and every other row the light grid one.
    pub(super) fn hub(
        &self,
        identifier: &str,
        title: &str,
        rows: &[Value],
        count: Option<&String>,
        kind: &str,
        full: bool,
        extra: Vec<(&str, Value)>,
    ) -> Map<String, Value> {
        let mut limit = SHELF_COUNT;
        let mut exhausted = false;
        // Cycle 55: `count` is a SIGNED 32-BIT integer; cycle 28: a negative count is
        // `len(rows) + count` clamped at zero and always reports the shelf exhausted.
        if let Some(count) = count.filter(|count| int_param(count, Some(32))) {
            limit = count
                .trim_start_matches('+')
                .parse::<i64>()
                .unwrap_or(SHELF_COUNT);
            if limit < 0 {
                limit = (rows.len() as i64 + limit).max(0);
                exhausted = true;
            }
        }
        let full_shows = kind == "mixed";
        let page = rows
            .iter()
            .take(limit.max(0) as usize)
            .map(|row| {
                if full || (full_shows && row.get("type").and_then(Value::as_str) == Some("show")) {
                    self.detail_row(row)
                } else {
                    self.grid_row(row, true, false, false)
                }
            })
            .collect::<Vec<_>>();
        let mut body = Map::new();
        body.insert("hubIdentifier".to_string(), json!(identifier));
        body.insert("title".to_string(), json!(title));
        body.insert("type".to_string(), json!(kind));
        body.insert("hubKey".to_string(), json!("/library/all"));
        body.insert("size".to_string(), json!(page.len()));
        body.insert(
            "more".to_string(),
            json!(!exhausted && rows.len() > page.len()),
        );
        body.insert("Metadata".to_string(), Value::Array(page));
        for (name, value) in extra {
            body.insert(name.to_string(), value);
        }
        body
    }

    /// One Home-screen shelf: `key`, `context` (the identifier with a trailing section
    /// id stripped), `style`, and `hubKey` naming the rows THIS shelf holds.
    pub(super) fn home_hub(
        &self,
        identifier: &str,
        title: &str,
        key: &str,
        rows: &[Value],
        query: &HashMap<String, String>,
        kind: &str,
        style: &str,
        promoted: bool,
    ) -> Value {
        let stem = match identifier.rsplit_once('.') {
            Some((head, tail)) if !tail.is_empty() && tail.bytes().all(|b| b.is_ascii_digit()) => {
                head
            }
            _ => identifier,
        };
        let mut extra = vec![
            ("key", json!(key)),
            ("context", json!(format!("hub.{stem}"))),
            ("style", json!(style)),
        ];
        if promoted {
            extra.push(("promoted", json!(true)));
        }
        let mut built = self.hub(
            identifier,
            title,
            rows,
            query.get("count"),
            kind,
            false,
            extra,
        );
        let keys = built
            .get("Metadata")
            .and_then(Value::as_array)
            .map(|rows| rows.iter().filter_map(key_of).collect::<Vec<_>>())
            .unwrap_or_default();
        if keys.is_empty() {
            built.shift_remove("hubKey");
        } else {
            built.insert(
                "hubKey".to_string(),
                json!(format!("/library/metadata/{}", keys.join(","))),
            );
        }
        Value::Object(built)
    }

    /// The newest leaf's `addedAt`, or the row's own with no leaves.
    fn newest_leaf_stamp(&self, row: &Value) -> i64 {
        let key = key_of(row).unwrap_or_default();
        self.leaves(&key)
            .iter()
            .map(|leaf| leaf.get("addedAt").and_then(Value::as_i64).unwrap_or(0))
            .max()
            .unwrap_or_else(|| row.get("addedAt").and_then(Value::as_i64).unwrap_or(0))
    }

    /// `/hubs/home/recentlyAdded?type=2` -- a MIXED, rolled-up list (cycle 28): two or
    /// more recently-added siblings collapse into their parent, recursively, and a
    /// parent row is dated by its NEWEST LEAF.
    pub(super) fn recently_added_tv(
        &self,
        section_id: Option<&String>,
        leaves_in: Option<Vec<Value>>,
    ) -> Vec<Value> {
        let mut leaves = leaves_in.unwrap_or_else(|| {
            self.catalog
                .items
                .iter()
                .filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"))
                .cloned()
                .collect()
        });
        if let Some(section) = section_number(section_id) {
            leaves.retain(|row| {
                string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                    == section.to_string()
            });
        }
        // `apply_show_prefs` takes `parentRatingKey` off the STORED episodes of a show
        // whose effective `flattenSeasons` skips the season level, and this roll-up reads
        // the stored rows: such a show's episodes share no season, so none of them rolls
        // up into a season or the show and each stays an episode.
        let mut flattened: HashMap<String, bool> = HashMap::new();
        let mut season_of = |leaf: &Value| -> String {
            let show = string_value(leaf.get("grandparentRatingKey").unwrap_or(&Value::Null));
            let skip = *flattened.entry(show.clone()).or_insert_with(|| {
                self.catalog
                    .get(&show)
                    .is_some_and(|show| self.show_flattened(show))
            });
            if skip {
                String::new()
            } else {
                string_value(leaf.get("parentRatingKey").unwrap_or(&Value::Null))
            }
        };
        let seasons_of = leaves.iter().map(&mut season_of).collect::<Vec<_>>();
        let mut season_order: Vec<String> = Vec::new();
        let mut by_season: HashMap<String, Vec<Value>> = HashMap::new();
        let mut show_order: Vec<String> = Vec::new();
        let mut by_show: HashMap<String, Vec<Value>> = HashMap::new();
        let mut leaf_season: HashMap<String, String> = HashMap::new();
        for (leaf, season) in leaves.iter().zip(seasons_of) {
            leaf_season.insert(key_of(leaf).unwrap_or_default(), season.clone());
            let show = string_value(leaf.get("grandparentRatingKey").unwrap_or(&Value::Null));
            if !by_season.contains_key(&season) {
                season_order.push(season.clone());
            }
            by_season.entry(season).or_default().push(leaf.clone());
            if !by_show.contains_key(&show) {
                show_order.push(show.clone());
            }
            by_show.entry(show).or_default().push(leaf.clone());
        }
        let mut rows: Vec<Value> = Vec::new();
        let mut claimed: HashSet<String> = HashSet::new();
        for show_key in &show_order {
            let show_leaves = &by_show[show_key];
            let show = self.catalog.get(show_key);
            let mut seasons: Vec<String> = Vec::new();
            for leaf in show_leaves {
                let season = leaf_season
                    .get(&key_of(leaf).unwrap_or_default())
                    .cloned()
                    .unwrap_or_default();
                if !seasons.contains(&season) {
                    seasons.push(season);
                }
            }
            if let Some(show) = show {
                if seasons.len() > 1 {
                    rows.push(show.clone());
                    claimed.extend(seasons);
                    continue;
                }
            }
            for season_key in seasons {
                let members = by_season.get(&season_key).cloned().unwrap_or_default();
                match self.catalog.get(&season_key) {
                    Some(season) if members.len() > 1 => {
                        rows.push(season.clone());
                        claimed.insert(season_key);
                    }
                    _ => rows.extend(members),
                }
            }
        }
        rows.retain(|row| {
            !(row.get("type").and_then(Value::as_str) == Some("episode")
                && leaf_season
                    .get(&key_of(row).unwrap_or_default())
                    .is_some_and(|season| claimed.contains(season)))
        });
        let mut dated = rows
            .into_iter()
            .map(|row| (self.newest_leaf_stamp(&row), row))
            .collect::<Vec<_>>();
        dated.sort_by_key(|(stamp, _)| -stamp);
        dated
            .into_iter()
            .take(TV_RECENT_CAP)
            .map(|(stamp, mut row)| {
                if row.get("addedAt").and_then(Value::as_i64) != Some(stamp) {
                    if let Some(object) = row.as_object_mut() {
                        object.insert("addedAt".to_string(), json!(stamp));
                    }
                }
                row
            })
            .collect()
    }

    /// Cycle 54, measured on kids and zen: the request's listing FILTERS run over the
    /// EPISODES before the TV roll-up -- `title=Train` answers the one episode,
    /// `title=Firefly` nothing, `show.title=Firefly` the show again.  `Ok(None)` when the
    /// request carries no filter; a filter that does not parse is its status.  A hub
    /// PAGE drops an empty-valued pair first (`drop_empty`, the oracle's
    /// `_hub_page_filters`), where `/hubs/home/recentlyAdded` parses it.
    pub(super) fn filtered_tv_leaves(
        &self,
        query: &HashMap<String, String>,
        drop_empty: bool,
    ) -> Result<Option<Vec<Value>>, u16> {
        let mut pairs = query
            .iter()
            .filter(|(_, value)| !drop_empty || !value.is_empty())
            .map(|(name, value)| (name.clone(), value.clone()))
            .collect::<Vec<_>>();
        pairs.sort();
        let types = self.filter_field_types("show");
        let mut errors = Vec::new();
        if crate::filters::parse(&pairs, &types, &mut errors).is_none() {
            return Ok(None);
        }
        let leaves = self
            .catalog
            .items
            .iter()
            .filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"))
            .cloned()
            .collect::<Vec<_>>();
        self.filter_rows(leaves, &pairs, "show").map(Some)
    }

    /// Newest first, by TYPE (1 movies, 2 the TV roll-up); None for every other type
    /// (cycle 21).
    pub(super) fn recently_added_rows(
        &self,
        type_id: Option<&str>,
        section_id: Option<&String>,
    ) -> Option<Vec<Value>> {
        match type_id {
            Some("2") => Some(self.recently_added_tv(section_id, None)),
            Some("1") => {
                let mut rows = self
                    .catalog
                    .items
                    .iter()
                    .filter(|row| row.get("type").and_then(Value::as_str) == Some("movie"))
                    .cloned()
                    .collect::<Vec<_>>();
                if let Some(section) = section_number(section_id) {
                    rows.retain(|row| {
                        string_value(row.get("librarySectionID").unwrap_or(&Value::Null))
                            == section.to_string()
                    });
                }
                rows.sort_by_key(|row| -row.get("addedAt").and_then(Value::as_i64).unwrap_or(0));
                Some(rows)
            }
            _ => None,
        }
    }

    /// The rows of a library's `tv/movie.recentlyadded` shelf: a show library's is its
    /// own mixed roll-up (cycle 55), a movie library's its rows newest first.
    pub(super) fn section_recently_added_rows(&self, section: &Value) -> Vec<Value> {
        let key = string_value(section.get("key").unwrap_or(&Value::Null));
        if section.get("type").and_then(Value::as_str) == Some("show") {
            return self.recently_added_tv(Some(&key), None);
        }
        let mut rows = self.section_rows(&key);
        rows.sort_by_key(|row| -row.get("addedAt").and_then(Value::as_i64).unwrap_or(0));
        rows
    }

    /// The `home.playlists` shelf's rows: the `/playlists` rows, newest VIEWED first.
    /// A stable sort over `library.playlists()`'s order, so lists viewed or added in the
    /// same second keep the smart-then-title order rather than the map's.
    pub(super) fn recent_playlists(&self) -> Vec<Value> {
        let mut rows = self.playlist_rows();
        rows.sort_by_key(|row| {
            -row.get("lastViewedAt")
                .and_then(Value::as_i64)
                .or_else(|| row.get("addedAt").and_then(Value::as_i64))
                .unwrap_or(0)
        });
        rows
    }

    /// `/hubs` -- the global shelves (`server.py` `hubs`).
    pub(super) fn global_hubs(&self, query: &HashMap<String, String>) -> Value {
        let wanted = query
            .get("identifier")
            .map(|value| {
                value
                    .split(',')
                    .filter(|v| !v.is_empty())
                    .map(str::to_string)
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        let directories = content_directories(query);
        let watching = self.in_directories(self.continue_watching(false), &directories);
        let excluded = exclude_continue_watching(query);
        let hidden = self.ondeck_shelf_hidden();
        let mut hubs = Vec::new();
        for (identifier, title, key, kind, style) in GLOBAL_HUBS {
            if !wanted.is_empty() && !wanted.iter().any(|w| w == identifier) {
                continue;
            }
            if *identifier == "home.ondeck" && hidden {
                continue;
            }
            if matches!(*identifier, "home.continue" | "home.ondeck") && excluded {
                continue;
            }
            if *identifier == "home.playlists" && directories.is_some() {
                continue;
            }
            let rows: Vec<Value> = match *identifier {
                "home.continue" => self.in_directories(self.resuming(), &directories),
                "home.ondeck" => watching
                    .iter()
                    .filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"))
                    .cloned()
                    .collect(),
                "home.movies.recent" => self.in_directories(
                    self.recently_added_rows(Some("1"), None)
                        .unwrap_or_default(),
                    &directories,
                ),
                "home.television.recent" => {
                    let leaves = self.filtered_tv_leaves(query, true).ok().flatten();
                    self.in_directories(self.recently_added_tv(None, leaves), &directories)
                }
                "home.playlists" => self.recent_playlists(),
                _ => Vec::new(),
            };
            hubs.push(self.home_hub(identifier, title, key, &rows, query, kind, style, true));
        }
        self.expand_global_hub_rows(&mut hubs, query);
        if query.get("includeGuids").map(String::as_str) == Some("1") {
            for hub in &mut hubs {
                if let Some(rows) = hub.get_mut("Metadata").and_then(Value::as_array_mut) {
                    for projected in rows {
                        let key = key_of(projected).unwrap_or_default();
                        let Some(stored) = self.catalog.get(&key) else {
                            continue;
                        };
                        if projected
                            .get("Guid")
                            .is_some_and(|g| g.as_array().map_or(true, |a| !a.is_empty()))
                        {
                            continue;
                        }
                        let guids = match stored.get("Guid") {
                            Some(Value::Array(list)) if !list.is_empty() => {
                                Some(Value::Array(list.clone()))
                            }
                            _ => stored
                                .get("guid")
                                .and_then(Value::as_str)
                                .filter(|g| !g.is_empty())
                                .map(|g| json!([{"id": g}])),
                        };
                        if let (Some(guids), Some(object)) = (guids, projected.as_object_mut()) {
                            object.insert("Guid".to_string(), guids);
                        }
                    }
                }
            }
        }
        self.hub_container(Map::from_iter([("Hub".to_string(), Value::Array(hubs))]))
    }

    /// `/hubs/promoted` -- the Home screen, one call per pinned library
    /// (`server.py` `promoted_hubs`).
    pub(super) fn promoted_hubs(&self, query: &HashMap<String, String>) -> Value {
        let mut hubs = Vec::new();
        let excluded = exclude_continue_watching(query);
        if !excluded {
            let rows = self.on_home(self.resuming());
            hubs.push(self.home_hub(
                "home.continue",
                "Continue Watching",
                "/hubs/home/continueWatching",
                &rows,
                query,
                "mixed",
                "hero",
                true,
            ));
        }
        if !excluded && !self.ondeck_shelf_hidden() {
            let rows = self
                .on_home(self.continue_watching(false))
                .into_iter()
                .filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"))
                .collect::<Vec<_>>();
            hubs.push(self.home_hub(
                "home.ondeck",
                "On Deck",
                "/hubs/home/onDeck",
                &rows,
                query,
                "episode",
                "shelf",
                true,
            ));
        }
        let wanted = content_directories(query);
        let pinned = query.get("pinnedContentDirectoryID").map(|value| {
            value
                .split(',')
                .filter(|v| !v.is_empty())
                .map(str::to_string)
                .collect::<Vec<_>>()
        });
        let kinds = self
            .catalog
            .sections
            .iter()
            .map(|s| {
                (
                    string_value(s.get("key").unwrap_or(&Value::Null)),
                    s.get("type")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string(),
                )
            })
            .collect::<HashMap<_, _>>();
        for section in &self.catalog.sections {
            let section_key = string_value(section.get("key").unwrap_or(&Value::Null));
            let section_type = section.get("type").and_then(Value::as_str).unwrap_or("");
            if wanted.as_ref().is_some_and(|w| !w.contains(&section_key)) {
                continue;
            }
            let mut same_type: Vec<String> = Vec::new();
            if let Some(pinned) = &pinned {
                same_type = pinned
                    .iter()
                    .filter(|value| kinds.get(*value).map(String::as_str) == Some(section_type))
                    .cloned()
                    .collect();
                if same_type.first() != Some(&section_key) {
                    continue;
                }
            }
            if self.section_visibility(&section_key) >= 1 {
                continue;
            }
            let is_show = section_type == "show";
            let mut rows = self.section_recently_added_rows(section);
            // Cycle 54: with a filter in the request the library's TV shelf is the
            // roll-up of the episodes that pass it.
            let filtered = if is_show {
                self.filtered_tv_leaves(query, true).ok().flatten()
            } else {
                None
            };
            if let Some(leaves) = filtered.clone() {
                rows = self.recently_added_tv(Some(&section_key), Some(leaves));
            }
            if pinned.is_some() {
                let merged = if is_show {
                    self.recently_added_tv(None, filtered)
                } else {
                    self.recently_added_rows(Some("1"), None)
                        .unwrap_or_default()
                };
                rows = self.in_directories(merged, &Some(same_type.iter().cloned().collect()));
            }
            let mut key = if is_show {
                format!("/hubs/home/recentlyAdded?type=2&sectionID={section_key}")
            } else {
                format!("/library/sections/{section_key}/all?sort=addedAt:desc")
            };
            let mut identifier = format!(
                "{}.recentlyadded.{section_key}",
                if is_show { "tv" } else { "movie" }
            );
            let mut shelf_title = format!(
                "Recently Added in {}",
                section.get("title").and_then(Value::as_str).unwrap_or("")
            );
            if pinned.is_some() {
                identifier = format!(
                    "home.{}.recent.{section_key}",
                    if is_show { "television" } else { "movies" }
                );
                shelf_title = format!("Recently Added {}", if is_show { "TV" } else { "Movies" });
                key = format!(
                    "/hubs/home/recentlyAdded?type={}&pinnedContentDirectoryID={}",
                    if is_show { 2 } else { 1 },
                    query
                        .get("pinnedContentDirectoryID")
                        .cloned()
                        .unwrap_or_default()
                );
            }
            hubs.push(self.home_hub(
                &identifier,
                &shelf_title,
                &key,
                &rows,
                query,
                if is_show { "mixed" } else { "movie" },
                "shelf",
                true,
            ));
        }
        if wanted
            .as_ref()
            .map_or(true, |w| w.is_empty() || w.contains("playlists"))
        {
            let rows = self.recent_playlists();
            hubs.push(self.home_hub(
                "home.playlists",
                "Recent Playlists",
                "/playlists/all?type=15&sort=lastViewedAt:desc&playlistType=video,audio",
                &rows,
                query,
                "playlist",
                "shelf",
                true,
            ));
        }
        self.hub_container(Map::from_iter([("Hub".to_string(), Value::Array(hubs))]))
    }
}

/// The fifteen fields a hub request refuses with a value (a 500).
const HUB_REFUSED_FIELDS: &[&str] = &[
    "duration",
    "lastViewedAt",
    "lastRatedAt",
    "viewCount",
    "skipCount",
    "viewOffset",
    "userRating",
    "unwatched",
    "resolution",
    "hdr",
    "mediaSize",
    "file",
    "parentIndex",
    "slug",
    "group",
];

/// A 500 if this hub request names one of the fifteen with a value.
pub(super) fn hub_refusal(query: &HashMap<String, String>) -> Option<Response> {
    for (key, value) in query {
        let bare = key.trim_end_matches(['<', '>', '=', '!']);
        if HUB_REFUSED_FIELDS.contains(&bare) && !value.trim().is_empty() {
            return Some(Response::error(
                500,
                &format!("plexsim: cannot filter a hub on {key}"),
            ));
        }
    }
    None
}

/// (page, paged, start, total) for a listing, by the measured rule: a real PMS pages
/// only when `X-Plex-Container-Start` AND `-Size` are both present; an EMPTY size is a
/// 500 even alone; a negative value drops paging altogether.
pub(super) fn page_window(
    rows: Vec<Value>,
    query: &HashMap<String, String>,
) -> Result<(Vec<Value>, bool, usize, usize), u16> {
    let start_raw = query.get("X-Plex-Container-Start");
    let size_raw = query.get("X-Plex-Container-Size");
    if size_raw.is_some_and(|size| size.is_empty()) {
        return Err(500);
    }
    let total = rows.len();
    let (Some(start_raw), Some(size_raw)) = (start_raw, size_raw) else {
        return Ok((rows, false, 0, total));
    };
    let parse = |text: &str| text.trim().parse::<i64>().map_err(|_| 500_u16);
    let start = parse(start_raw)?;
    let size = parse(size_raw)?;
    if size < 0 || start < 0 {
        return Ok((rows, false, 0, total));
    }
    let page = rows
        .into_iter()
        .skip(start as usize)
        .take(size as usize)
        .collect();
    Ok((page, true, start as usize, total))
}

/// Strip `Guid` from a listing row unless the client asked for it; under
/// `includeGuids=1` a row with only its primary `guid` gets a one-entry list.
pub(super) fn guid_gate(mut row: Value, query: &HashMap<String, String>) -> Value {
    let wanted = query.get("includeGuids").map(String::as_str) == Some("1");
    if let Some(object) = row.as_object_mut() {
        if wanted {
            let empty = object.get("Guid").map_or(true, |guids| {
                guids.is_null() || guids.as_array().is_some_and(Vec::is_empty)
            });
            if empty {
                if let Some(guid) = object
                    .get("guid")
                    .and_then(Value::as_str)
                    .filter(|g| !g.is_empty())
                {
                    let guid = guid.to_string();
                    object.insert("Guid".to_string(), json!([{"id": guid}]));
                }
            }
        } else {
            object.shift_remove("Guid");
        }
    }
    row
}
