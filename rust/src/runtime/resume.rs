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
    pub(super) fn on_home<R: Row>(&self, rows: Vec<R>) -> Vec<R> {
        // The hidden libraries once, not a prefs lookup with three allocations per row:
        // Home ran this over every movie on each read (2026-10-17). Usually none is hidden.
        let hidden = self
            .section_prefs
            .keys()
            .filter(|(_, name)| name == "hidden")
            .map(|(section, _)| section.as_str())
            .filter(|section| self.section_visibility(section) >= 1)
            .collect::<HashSet<&str>>();
        if hidden.is_empty() {
            return rows;
        }
        rows.into_iter()
            .filter(|row| {
                !hidden.contains(
                    string_value(row.get("librarySectionID").unwrap_or(&Value::Null)).as_str(),
                )
            })
            .collect()
    }

    /// `rows` narrowed to the libraries `contentDirectoryID` named, off Home's hidden.
    pub(super) fn in_directories<R: Row>(
        &self,
        rows: Vec<R>,
        wanted: &Option<HashSet<String>>,
    ) -> Vec<R> {
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

    fn hidden_from_continue(&self, row: &impl Row) -> bool {
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
        self.continue_watching_shared(include_hidden).as_ref().clone()
    }

    /// `continue_watching` without a copy, for the readers that only borrow its rows.
    pub(super) fn continue_watching_shared(&self, include_hidden: bool) -> std::sync::Arc<Vec<Value>> {
        // Home, its hubs and every item page read this list; it changes only with the rows,
        // the removed-from-Continue-Watching set and the plays of this session.
        let key = (self.catalog.generation(), include_hidden);
        if let Some((_, rows)) = self.library_memo.borrow().continue_watching.iter().find(|(memo, _)| {
            (memo.0, memo.1) == key && memo.2 == self.hidden_continue && memo.3 == self.viewed_in_session
        }) {
            return std::sync::Arc::clone(rows);
        }
        let rows = std::sync::Arc::new(self.continue_watching_uncached(include_hidden));
        let mut memo = self.library_memo.borrow_mut();
        memo.continue_watching.retain(|(memo, _)| memo.0 == key.0 && memo.1 != include_hidden);
        memo.continue_watching.push((
            (key.0, include_hidden, self.hidden_continue.clone(), self.viewed_in_session.clone()),
            std::sync::Arc::clone(&rows),
        ));
        rows
    }

    pub(super) fn cached_subset(&self, key: &SubsetKey) -> Option<(Vec<usize>, Option<usize>)> {
        let memo = self.library_memo.borrow();
        memo.subsets.iter().find(|(stored, _)| stored == key).map(|(_, found)| found.clone())
    }

    /// Keep a subset's ordered rows; a row without a catalog position (none should lack
    /// one) keeps the subset out of the memo.
    pub(super) fn store_subset(&self, key: SubsetKey, rows: &[RowRef<'_>], untrimmed: Option<usize>) {
        let positions = rows.iter().map(|row| row.hot.position).collect::<Vec<usize>>();
        let mut memo = self.library_memo.borrow_mut();
        memo.subsets.retain(|(stored, _)| stored.0 == key.0);
        if memo.subsets.len() >= 64 {
            memo.subsets.remove(0);
        }
        memo.subsets.push((key, (positions, untrimmed)));
    }

    /// The positions of a section's rows of one type, in library order (`choose_refs`).
    pub(super) fn typed_section_positions(&self, kind: &str, section: &str) -> Vec<usize> {
        let generation = self.catalog.generation();
        let mut memo = self.library_memo.borrow_mut();
        if memo.typed_sections.as_ref().is_none_or(|(built, _)| *built != generation) {
            let mut index: HashMap<(String, String), Vec<usize>> = HashMap::new();
            for row in self.catalog.hot().rows() {
                let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
                let section = string_value(row.get("librarySectionID").unwrap_or(&Value::Null));
                index.entry((kind, section)).or_default().push(row.position);
            }
            memo.typed_sections = Some((generation, index));
        }
        let (_, index) = memo.typed_sections.as_ref().expect("built above");
        index.get(&(kind.to_string(), section.to_string())).cloned().unwrap_or_default()
    }

    /// The rows whose `guid` starts with any of `prefixes`, each once, in library order: a
    /// guid lookup read every row of the library to answer one (2026-10-17).
    pub(super) fn guid_prefix_rows(&self, prefixes: &[&str]) -> Vec<&Value> {
        let generation = self.catalog.generation();
        let mut memo = self.library_memo.borrow_mut();
        if memo.guid_index.as_ref().is_none_or(|(built, _)| *built != generation) {
            let mut index: std::collections::BTreeMap<String, Vec<usize>> = Default::default();
            for row in self.catalog.hot().rows() {
                if let Some(guid) = row.get("guid").and_then(Value::as_str) {
                    index.entry(guid.to_string()).or_default().push(row.position);
                }
            }
            memo.guid_index = Some((generation, index));
        }
        let (_, index) = memo.guid_index.as_ref().expect("built above");
        let mut positions = prefixes
            .iter()
            .flat_map(|prefix| {
                index
                    .range::<str, _>((std::ops::Bound::Included(*prefix), std::ops::Bound::Unbounded))
                    .take_while(move |(guid, _)| guid.starts_with(prefix))
                    .flat_map(|(_, positions)| positions.iter().copied())
            })
            .collect::<Vec<_>>();
        positions.sort_unstable();
        positions.dedup();
        positions.into_iter().filter_map(|position| self.catalog.row_at(position)).collect()
    }

    fn continue_watching_uncached(&self, include_hidden: bool) -> Vec<Value> {
        fn truthy(row: &impl Row, name: &str) -> bool {
            match row.get(name) {
                None | Some(Value::Null) => false,
                Some(Value::Number(number)) => number.as_f64().unwrap_or(0.0) != 0.0,
                Some(Value::String(text)) => !text.is_empty(),
                Some(Value::Bool(flag)) => *flag,
                Some(_) => true,
            }
        }
        fn stamp_of(row: &impl Row) -> i64 {
            row.get("lastViewedAt").and_then(Value::as_i64).unwrap_or(0)
        }
        // References: copying every episode of the library here, once per call and three
        // calls per `/hubs`, was most of the home screen's 1.9 GB (the production server,
        // 2026-10-10). Only a show with a play or a resume point can have a row, so only
        // those shows' episodes are grouped (2026-10-11). One pass reads each row's type
        // once: three passes over the 42k rows were most of a Home read (2026-10-17). The
        // pass reads the hot fields (`hotrow.rs`), and copies only the rows it answers.
        fn show_of<'a>(row: Lazy<'a, 'a>) -> std::borrow::Cow<'a, str> {
            match row.get("grandparentRatingKey") {
                Some(Value::String(text)) => std::borrow::Cow::Borrowed(text.as_str()),
                other => std::borrow::Cow::Owned(string_value(other.unwrap_or(&Value::Null))),
            }
        }
        let hot = self.catalog.hot();
        let mut rows: Vec<(i64, Value)> = Vec::new();
        let mut episodes_of_library: Vec<(std::borrow::Cow<'_, str>, Lazy<'_, '_>)> = Vec::new();
        let mut active: HashSet<String> = HashSet::new();
        let mut played_libraries: HashSet<String> = HashSet::new();
        for row in hot.lazy() {
            match row.get("type").and_then(Value::as_str) {
                Some("movie") => {
                    if truthy(&row, "viewOffset") && (include_hidden || !self.hidden_from_continue(&row)) {
                        rows.push((stamp_of(&row), row.body().clone()));
                    }
                }
                Some("episode") => {
                    let show = show_of(row);
                    if truthy(&row, "viewOffset") || truthy(&row, "viewCount") {
                        active.insert(show.to_string());
                    }
                    if !self.viewed_in_session.is_empty()
                        && key_of(&row).is_some_and(|key| self.viewed_in_session.contains(&key))
                    {
                        played_libraries.insert(string_value(row.get("librarySectionID").unwrap_or(&Value::Null)));
                    }
                    episodes_of_library.push((show, row));
                }
                _ => {}
            }
        }
        let mut order: Vec<String> = Vec::new();
        let mut shows: HashMap<String, Vec<Lazy<'_, '_>>> = HashMap::new();
        if !active.is_empty() {
            for (show, row) in episodes_of_library {
                if !active.contains(show.as_ref()) {
                    continue;
                }
                if !shows.contains_key(show.as_ref()) {
                    order.push(show.to_string());
                }
                shows.entry(show.into_owned()).or_default().push(row);
            }
        }
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
            let mut chosen: Option<Lazy<'_, '_>> = None;
            for row in episodes.iter().copied().filter(|row| truthy(row, "viewOffset")) {
                if chosen.map_or(true, |best| stamp_of(&row) > stamp_of(&best)) {
                    chosen = Some(row);
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
                    let anchor_row = episodes[anchor];
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
                        .copied();
                }
            }
            if let Some(chosen) = chosen {
                let mut chosen = chosen.body().clone();
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
        self.continue_watching(false).into_iter().filter(|row| resume_point(row)).collect()
    }

    /// The MERGED hub, `/hubs/continueWatching` and its `/items` leaf (cycle 57):
    /// the in-progress rows and On Deck's next-up rows, newest view first.
    pub(super) fn merged_continue_watching(&self) -> Vec<Value> {
        let mut rows = self.continue_watching(false);
        rows.sort_by_key(|row| -row.get("lastViewedAt").and_then(Value::as_i64).unwrap_or(0));
        rows
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

/// A shelf with no `count=` carries at most SIX rows (measured on the 1.42.2 reference).
const SHELF_COUNT: i64 = 6;
/// `/hubs/home/recentlyAdded?type=2` caps at 50 (cycle 28, the 1.42.2 reference).
const TV_RECENT_CAP: usize = 50;

/// (generation, section, leaf, `unwatched`, page end): what a section subset depends on.
pub(super) type SubsetKey = (u64, String, String, Option<String>, Option<usize>);

/// Answers read off the whole library, each with what it was computed from: the catalog's
/// `generation` and the request's other inputs. A stale entry is never served, because a
/// row cannot change without the generation changing.
#[derive(Debug, Default)]
pub(super) struct LibraryMemo {
    continue_watching: Vec<((u64, bool, HashSet<String>, HashSet<String>), std::sync::Arc<Vec<Value>>)>,
    recently_added_tv: Vec<((u64, Option<String>), std::sync::Arc<Vec<Value>>)>,
    /// Every row's position by (type, `librarySectionID` as `string_value` reads it).
    typed_sections: Option<(u64, HashMap<(String, String), Vec<usize>>)>,
    /// A section subset's ordered rows by request (`cached_subset`): positions and the
    /// count `newest` had before keeping only what its page can hold.
    subsets: Vec<(SubsetKey, (Vec<usize>, Option<usize>))>,
    /// The newest-first movie list's positions, by section (`recent_movie_refs`).
    recent_movies: Vec<((u64, Option<String>), Vec<usize>)>,
    /// Every row's position by its `guid`, sorted so a prefix is a range (`guid_prefix_rows`).
    guid_index: Option<(u64, std::collections::BTreeMap<String, Vec<usize>>)>,
}

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
    pub(super) fn hub<R: Row>(
        &self,
        identifier: &str,
        title: &str,
        rows: &[R],
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
                let row = row.whole();
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
    pub(super) fn home_hub<R: Row>(
        &self,
        identifier: &str,
        title: &str,
        key: &str,
        rows: &[R],
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
    fn newest_leaf_stamp(&self, row: &(impl Row + ?Sized)) -> i64 {
        let key = key_of(row).unwrap_or_default();
        self.leaves_refs(&key)
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
        // Home, the TV hubs and the promoted shelves each roll up the whole library; the
        // unfiltered roll-up changes only with the rows.
        if leaves_in.is_some() {
            return self.recently_added_tv_uncached(section_id, leaves_in);
        }
        self.recently_added_tv_shared(section_id).as_ref().clone()
    }

    /// The unfiltered roll-up without a copy (`recently_added_tv`).
    pub(super) fn recently_added_tv_shared(&self, section_id: Option<&String>) -> std::sync::Arc<Vec<Value>> {
        let key = (self.catalog.generation(), section_id.cloned());
        if let Some((_, rows)) = self.library_memo.borrow().recently_added_tv.iter().find(|(memo, _)| *memo == key) {
            return std::sync::Arc::clone(rows);
        }
        let rows = std::sync::Arc::new(self.recently_added_tv_uncached(section_id, None));
        let mut memo = self.library_memo.borrow_mut();
        memo.recently_added_tv.retain(|(memo, _)| memo.0 == key.0);
        memo.recently_added_tv.push((key, std::sync::Arc::clone(&rows)));
        rows
    }

    fn recently_added_tv_uncached(
        &self,
        section_id: Option<&String>,
        leaves_in: Option<Vec<Value>>,
    ) -> Vec<Value> {
        // References into the catalog (or the caller's filtered leaves): every episode
        // was copied three times here -- the list, then by season and by show -- and
        // only the capped rows it answers are copied now. The catalog's are read through
        // their hot fields (`hotrow.rs`).
        match &leaves_in {
            Some(rows) => self.roll_up_tv(section_id, rows.iter().collect(), |row| (*row).clone()),
            None => {
                let hot = self.catalog.hot();
                let leaves = hot
                    .lazy()
                    .filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"))
                    .collect::<Vec<_>>();
                self.roll_up_tv(section_id, leaves, |row| row.body().clone())
            }
        }
    }

    fn roll_up_tv<R: Row>(&self, section_id: Option<&String>, mut leaves: Vec<R>, own: impl Fn(&R) -> Value) -> Vec<Value> {
        /// A row of the roll-up: a leaf, or the show or season it rolled up into.
        enum Pick<'r, R> {
            Leaf(&'r R),
            Parent(RowRef<'r>),
        }
        impl<R: Row> Pick<'_, R> {
            fn get(&self, name: &str) -> Option<&Value> {
                match self {
                    Pick::Leaf(row) => row.get(name),
                    Pick::Parent(row) => row.get(name),
                }
            }
        }
        if let Some(section) = section_number(section_id) {
            let section = section.to_string();
            leaves.retain(|row| {
                string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section
            });
        }
        // Measured on plexscratch (1.43.4): a show whose effective `flattenSeasons` skips
        // the season level rolls up exactly as any other -- Firefly's six episodes across
        // two seasons stay ONE show row on `home.television.recent`, `tv.recentlyadded`
        // and `/hubs/home/recentlyAdded?type=2`. The setting only changes what an episode
        // ROW says (`apply_flatten_projection`); the stored row keeps its season, and this
        // roll-up reads the stored rows. The oracle once took the key off the stored row,
        // and this grouped a flattened show's episodes under no season to match it.
        // Each leaf's key, season and show, read once and borrowed below: allocating them
        // again for every map and lookup was most of this roll-up over the production
        // server's 27k episodes.
        let named = leaves
            .iter()
            .map(|leaf| {
                (
                    leaf,
                    key_of(leaf).unwrap_or_default(),
                    string_value(leaf.get("parentRatingKey").unwrap_or(&Value::Null)),
                    string_value(leaf.get("grandparentRatingKey").unwrap_or(&Value::Null)),
                )
            })
            .collect::<Vec<_>>();
        let mut by_season: HashMap<&str, Vec<(&R, &str)>> = HashMap::new();
        let mut show_order: Vec<&str> = Vec::new();
        let mut by_show: HashMap<&str, Vec<&str>> = HashMap::new();
        let mut leaf_season: HashMap<&str, &str> = HashMap::new();
        for (leaf, key, season, show) in &named {
            leaf_season.insert(key.as_str(), season.as_str());
            by_season.entry(season.as_str()).or_default().push((*leaf, key.as_str()));
            if !by_show.contains_key(show.as_str()) {
                show_order.push(show.as_str());
            }
            by_show.entry(show.as_str()).or_default().push(key.as_str());
        }
        // An episode row carries its key for the claimed-season pass at the end.
        let mut rows: Vec<(Pick<'_, R>, Option<&str>)> = Vec::new();
        let mut claimed: HashSet<&str> = HashSet::new();
        for show_key in &show_order {
            let show = self.catalog.lazy_get(show_key);
            let mut seasons: Vec<&str> = Vec::new();
            for key in &by_show[show_key] {
                let season = leaf_season.get(key).copied().unwrap_or_default();
                if !seasons.contains(&season) {
                    seasons.push(season);
                }
            }
            if let Some(show) = show {
                if seasons.len() > 1 {
                    rows.push((Pick::Parent(show), None));
                    claimed.extend(seasons);
                    continue;
                }
            }
            for season_key in seasons {
                let members = by_season.get(season_key).map(Vec::as_slice).unwrap_or_default();
                match self.catalog.lazy_get(season_key) {
                    Some(season) if members.len() > 1 => {
                        rows.push((Pick::Parent(season), None));
                        claimed.insert(season_key);
                    }
                    _ => rows.extend(members.iter().map(|(leaf, key)| (Pick::Leaf(*leaf), Some(*key)))),
                }
            }
        }
        rows.retain(|(row, key)| {
            !(row.get("type").and_then(Value::as_str) == Some("episode")
                && key
                    .and_then(|key| leaf_season.get(key))
                    .is_some_and(|season| claimed.contains(season)))
        });
        let rows = rows.into_iter().map(|(row, _)| row).collect::<Vec<_>>();
        let mut dated = rows
            .into_iter()
            .map(|row| {
                // An episode is its own only leaf: looking up its (absent) children for
                // each of the library's episodes was most of this roll-up.
                if row.get("type").and_then(Value::as_str) == Some("episode") {
                    (row.get("addedAt").and_then(Value::as_i64).unwrap_or(0), row)
                } else {
                    let stamp = match &row {
                        Pick::Leaf(leaf) => self.newest_leaf_stamp(&own(leaf)),
                        Pick::Parent(parent) => self.newest_leaf_stamp(&parent),
                    };
                    (stamp, row)
                }
            })
            .collect::<Vec<_>>();
        dated.sort_by_key(|(stamp, _)| -stamp);
        dated
            .into_iter()
            .take(TV_RECENT_CAP)
            .map(|(stamp, row)| {
                let mut row = match row {
                    Pick::Leaf(leaf) => own(leaf),
                    Pick::Parent(parent) => parent.body().clone(),
                };
                if row.get("addedAt").and_then(Value::as_i64) != Some(stamp) {
                    if let Some(object) = row.as_object_mut() {
                        object.insert("addedAt".to_string(), json!(stamp));
                    }
                }
                row
            })
            .collect()
    }

    /// Cycle 54, measured on plexlab and the 1.42.2 reference: the request's listing
    /// FILTERS run over the EPISODES before the TV roll-up -- `title=Train` answers the one
    /// episode, `title=Firefly` nothing, `show.title=Firefly` the show again.  `Ok(None)`
    /// when the request carries no filter; a filter that does not parse is its status.  A
    /// hub PAGE drops an empty-valued pair first (`drop_empty`, the oracle's
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
        // The refused columns refuse before anything parses: plexlab's TV Recently Added
        // list answers `art=` (EMPTY, which that list parses) with a 500, where the
        // parse below finds no field in it and would answer every row.
        if pairs.iter().any(|(name, _)| {
            let (field, _) = crate::filters::split_key(name);
            REFUSED_FILTER_FIELDS.contains(&crate::filters::split_scope(&field).1.as_str())
        }) {
            return Err(500);
        }
        let types = self.filter_field_types("show");
        let mut errors = Vec::new();
        if crate::filters::parse(&pairs, &types, &mut errors).is_none() {
            return Ok(None);
        }
        // Filtered through their hot fields; only the episodes it keeps are copied.
        let hot = self.catalog.hot();
        let leaves = hot.lazy().filter(|row| row.get("type").and_then(Value::as_str) == Some("episode")).collect::<Vec<_>>();
        self.filter_rows(leaves, &pairs, "show").map(|rows| Some(rows.into_iter().map(|row| row.body().clone()).collect()))
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
            Some("1") => Some(self.recent_movie_refs(section_id).into_iter().map(|row| row.body().clone()).collect()),
            _ => None,
        }
    }

    /// The movie half of `recently_added_rows` by reference: Home's shelf and the
    /// movie list copied every movie to show twelve (the production server, 2026-10-11).
    pub(super) fn recent_movie_refs(&self, section_id: Option<&String>) -> Vec<RowRef<'_>> {
        // Home, its hubs and the promoted shelves read this list on every request; it
        // changes only with the rows (59% of a Home read, 2026-10-17).
        let key = (self.catalog.generation(), section_id.cloned());
        if let Some((_, positions)) = self.library_memo.borrow().recent_movies.iter().find(|(memo, _)| *memo == key) {
            return positions.iter().filter_map(|position| self.catalog.lazy_at(*position)).collect();
        }
        let hot = self.catalog.hot();
        let mut movies = hot.rows().filter(|row| row.get("type").and_then(Value::as_str) == Some("movie")).collect::<Vec<_>>();
        if let Some(section) = section_number(section_id) {
            let section = section.to_string();
            movies.retain(|row| string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section);
        }
        movies.sort_by_key(|row| -row.get("addedAt").and_then(Value::as_i64).unwrap_or(0));
        let positions = movies.iter().map(|row| row.position).collect::<Vec<_>>();
        drop(movies);
        let rows = positions.iter().filter_map(|&position| self.catalog.lazy_at(position)).collect();
        let mut memo = self.library_memo.borrow_mut();
        memo.recent_movies.retain(|(memo, _)| memo.0 == key.0);
        memo.recent_movies.push((key, positions));
        rows
    }

    /// The rows of a library's `tv/movie.recentlyadded` shelf: a show library's is its
    /// own mixed roll-up (cycle 55), a movie library's its rows newest first.
    pub(super) fn section_recently_added_rows(&self, section: &Value) -> Vec<Value> {
        let key = string_value(section.get("key").unwrap_or(&Value::Null));
        if section.get("type").and_then(Value::as_str) == Some("show") {
            return self.recently_added_tv(Some(&key), None);
        }
        self.section_recent_movie_refs(&key).into_iter().map(|row| row.body().clone()).collect()
    }

    /// A movie library's rows newest first, borrowed.
    pub(super) fn section_recent_movie_refs(&self, key: &str) -> Vec<RowRef<'_>> {
        let mut rows = self.section_row_refs(key);
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
        // One pass for every shelf that reads it: three per Home read were most of Home
        // (2026-10-17). `resuming` and `ondeck_shelf_hidden` are this list, filtered.
        let all = self.continue_watching_shared(false);
        let watching = self.in_directories(all.iter().collect::<Vec<_>>(), &directories);
        // Borrowed by the TV shelf below when it has no filter.
        let tv_recent = self.recently_added_tv_shared(None);
        let excluded = exclude_continue_watching(query);
        let hidden = ondeck_hidden_in(&all);
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
            let owned = |rows: Vec<Value>| rows.into_iter().map(AnyRow::Owned).collect::<Vec<_>>();
            let rows: Vec<AnyRow<'_>> = match *identifier {
                "home.continue" => self
                    .in_directories(all.iter().filter(|row| resume_point(row)).collect::<Vec<_>>(), &directories)
                    .into_iter()
                    .map(AnyRow::Borrowed)
                    .collect(),
                "home.ondeck" => watching
                    .iter()
                    .filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"))
                    .map(|row| AnyRow::Borrowed(*row))
                    .collect(),
                // Read through their hot fields: only the shelf's twelve are read whole.
                "home.movies.recent" => self
                    .in_directories(self.recent_movie_refs(None), &directories)
                    .into_iter()
                    .map(AnyRow::Ref)
                    .collect(),
                "home.television.recent" => match self.filtered_tv_leaves(query, true).ok().flatten() {
                    Some(leaves) => owned(self.in_directories(self.recently_added_tv(None, Some(leaves)), &directories)),
                    None => self
                        .in_directories(tv_recent.iter().collect::<Vec<_>>(), &directories)
                        .into_iter()
                        .map(AnyRow::Borrowed)
                        .collect(),
                },
                "home.playlists" => owned(self.recent_playlists()),
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
                        // Cycle 83: this pass gave a `local://` row a one-entry list
                        // and kept an unmatched row's stored ids.
                        apply_listing_guids(projected, stored);
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
        let all = if excluded { std::sync::Arc::new(Vec::new()) } else { self.continue_watching_shared(false) };
        if !excluded {
            let rows = self.on_home(all.iter().filter(|row| resume_point(row)).collect::<Vec<_>>());
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
        if !excluded && !ondeck_hidden_in(&all) {
            let rows = self
                .on_home(all.iter().collect::<Vec<_>>())
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
            // Cycle 54: with a filter in the request the library's TV shelf is the
            // roll-up of the episodes that pass it.
            let filtered = if is_show {
                self.filtered_tv_leaves(query, true).ok().flatten()
            } else {
                None
            };
            // Only the shelf's own rows are built, and movies stay borrowed: the
            // library's list was copied and then replaced under a pin, and a movie
            // shelf copied every movie twice (+320 MB a request, 2026-10-17).
            let owned = |rows: Vec<Value>| rows.into_iter().map(AnyRow::Owned).collect::<Vec<_>>();
            let tv_all = if is_show && filtered.is_none() { Some(self.recently_added_tv_shared(None)) } else { None };
            let tv_section = if is_show && filtered.is_none() && pinned.is_none() {
                Some(self.recently_added_tv_shared(Some(&section_key)))
            } else {
                None
            };
            let mut rows: Vec<AnyRow<'_>> = if pinned.is_some() {
                let merged = if is_show {
                    match (&tv_all, filtered) {
                        (Some(shared), _) => shared.iter().map(AnyRow::Borrowed).collect(),
                        (None, filtered) => owned(self.recently_added_tv(None, filtered)),
                    }
                } else {
                    self.recent_movie_refs(None).into_iter().map(AnyRow::Ref).collect()
                };
                self.in_directories(merged, &Some(same_type.iter().cloned().collect()))
            } else if let Some(leaves) = filtered {
                owned(self.recently_added_tv(Some(&section_key), Some(leaves)))
            } else if let Some(shared) = &tv_section {
                shared.iter().map(AnyRow::Borrowed).collect()
            } else {
                self.section_recent_movie_refs(&section_key).into_iter().map(AnyRow::Ref).collect()
            };
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
            let mut kind = if is_show { "mixed" } else { "movie" };
            if section_type == "artist" {
                // `server.py`: measured on plexscratch42 for an empty music library --
                // `music.recent.added.<id>`, typed `album`, keyed on the library's albums
                // by date added; under `pinnedContentDirectoryID` the 1.42.2 reference's
                // swap onto the Home shelf's own identity (`home.music.recent`, "Recently
                // Added Music", `type=8`). rex holds no music rows.
                rows = Vec::new();
                kind = "album";
                identifier = format!("music.recent.added.{section_key}");
                key = format!("/library/sections/{section_key}/all?type=9&sort=addedAt:desc");
                if pinned.is_some() {
                    identifier = format!("home.music.recent.{section_key}");
                    shelf_title = "Recently Added Music".to_string();
                    key = format!(
                        "/hubs/home/recentlyAdded?type=8&pinnedContentDirectoryID={}",
                        query.get("pinnedContentDirectoryID").cloned().unwrap_or_default()
                    );
                }
            }
            let mut shelf = self.home_hub(
                &identifier,
                &shelf_title,
                &key,
                &rows,
                query,
                kind,
                "shelf",
                true,
            );
            // Cycle 83, measured on plexlab (Firefly) and recorded on the 1.42.2 reference
            // (cycle 55): the TV shelf's FULL show rows take the `include*` blocks here as
            // on a library hub page; a light row never does.
            if is_show {
                for row in shelf
                    .get_mut("Metadata")
                    .and_then(Value::as_array_mut)
                    .into_iter()
                    .flatten()
                {
                    if row.get("type").and_then(Value::as_str) == Some("show") {
                        self.expand_full_rows(std::slice::from_mut(row), query);
                    }
                }
            }
            hubs.push(shelf);
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

/// The columns a LISTING refuses -- a 500 wherever the filter grammar runs (`filters.py`'s
/// `REFUSED_FILTER_FIELDS`, and `filter_rows`).
pub(super) const REFUSED_FILTER_FIELDS: &[&str] = &[
    "parentIndex",
    "slug",
    "extraType",
    "theme",
    "banner",
    "art",
    "thumb",
    "userFields",
];

/// The fifteen fields a hub request refuses with a value (a 500). Cycle 83, measured on
/// plexlab and the 1.42.2 reference: the listing's refusals (`REFUSED_FILTER_FIELDS`) are
/// refused here on the same terms, so `hub_refusal` checks both lists.
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
        if (HUB_REFUSED_FIELDS.contains(&bare) || REFUSED_FILTER_FIELDS.contains(&bare))
            && !value.trim().is_empty()
        {
            return Some(Response::error(
                500,
                &format!("rex: cannot filter a hub on {key}"),
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
    let total = rows.len();
    let Some((start, size)) = paging_values(query)? else {
        return Ok((rows, false, 0, total));
    };
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

/// `(start, size)` when both paging parameters are sent, `None` otherwise, and `Err(500)`
/// for a value that does not parse (`server.py` `paging_values`).
///
/// Cycle 83, measured on plexlab and the 1.42.2 reference over the grid, `/folder`,
/// `/recentlyViewedShows`, `/children`, `/library/sections`, `/identity`, `/:/prefs`, a hub
/// page and `/hubs/search`: a SIZE is parsed whenever it is sent, alone included (a lone
/// `X-Plex-Container-Size=abc` is the 500), a START only beside a Size. The grammar is a
/// 32-bit signed integer -- an optional `+`/`-` and ASCII digits; an empty value, `1.5`,
/// a space either side and `2147483648` are 500s. `trim().parse::<i64>()` took spaces and
/// 64-bit values, and a lone Size was never read.
pub(super) fn paging_values(query: &HashMap<String, String>) -> Result<Option<(i64, i64)>, u16> {
    fn parse(text: &str) -> Result<i64, u16> {
        let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return Err(500);
        }
        let value = text.parse::<i32>().map_err(|_| 500_u16)?;
        Ok(i64::from(value))
    }
    let Some(size) = query.get("X-Plex-Container-Size") else {
        return Ok(None);
    };
    let size = parse(size)?;
    let Some(start) = query.get("X-Plex-Container-Start") else {
        return Ok(None);
    };
    Ok(Some((parse(start)?, size)))
}

/// A row the Guid gate strips: an UNMATCHED guid -- `local://` (never matched, a season or
/// episode unmatch) or `tv.plex.agents.none://` (a movie or show unmatch, or the none agent) --
/// or the LEGACY Personal Media agent's `com.plexapp.agents.none://`. Cycle 84, measured on
/// plexscratch42 (1.42.2) with a movie library on that agent (`tests/fixtures/cycle84_c.json`):
/// no `Guid` key in any `includeGuids=1` listing or the detail. Such a row is NOT unmatched --
/// `unmatched=1` finds none of them -- so only the Guid gates read this.
pub(super) fn unmatched_guid(row: &Value) -> bool {
    row.get("guid").and_then(Value::as_str).is_some_and(|guid| {
        guid.starts_with("local://")
            || guid.starts_with("tv.plex.agents.none://")
            || guid.starts_with("com.plexapp.agents.none://")
    })
}

/// The `Guid` list a LISTING row carries under `includeGuids=1`, or None for no key --
/// the oracle's `_listing_guids`, one gate for every listing and hub.
///
/// Measured on plexscratch (1.43.4) after unmatching a movie and an episode, and on
/// plexscratch42 (1.42.2) on a `tv.plex.agents.none://` movie and `local://` rows: an
/// unmatched row carries NO `Guid` key in any listing, even where its DETAIL still
/// answers the imdb/tmdb/tvdb list the unmatch kept. A matched row keeps its list, and
/// one with no external ids gets a one-entry list naming its own guid.
pub(super) fn listing_guids(row: &Value) -> Option<Value> {
    if unmatched_guid(row) {
        return None;
    }
    if row.get("type").and_then(Value::as_str) == Some("collection") {
        // Cycle 88, measured on plexscratch42: a collection row carries no `Guid` under
        // `includeGuids=1` -- `all?type=18` and section `/collections` alike.
        return None;
    }
    match row.get("Guid") {
        Some(Value::Array(list)) if !list.is_empty() => Some(Value::Array(list.clone())),
        _ => row
            .get("guid")
            .filter(|guid| !guid.is_null() && !string_value(guid).is_empty())
            .map(|guid| json!([{"id": guid}])),
    }
}

/// Put `stored`'s listing `Guid` on a projected row under `includeGuids=1`, keeping a
/// list the projection already carries, or take the key off an unmatched row.
pub(super) fn apply_listing_guids(shown: &mut Value, stored: &Value) {
    let guids = listing_guids(stored);
    let Some(object) = shown.as_object_mut() else {
        return;
    };
    match guids {
        None => {
            object.shift_remove("Guid");
        }
        Some(guids) => {
            let empty = object.get("Guid").map_or(true, |existing| {
                existing.is_null() || existing.as_array().is_some_and(Vec::is_empty)
            });
            if empty {
                object.insert("Guid".to_string(), guids);
            }
        }
    }
}

/// Strip `Guid` from a listing row unless the client asked for it; under
/// `includeGuids=1` it is `listing_guids`.
pub(super) fn guid_gate(mut row: Value, query: &HashMap<String, String>) -> Value {
    let wanted = query.get("includeGuids").map(String::as_str) == Some("1");
    if wanted {
        let stored = row.clone();
        apply_listing_guids(&mut row, &stored);
    } else if let Some(object) = row.as_object_mut() {
        object.shift_remove("Guid");
    }
    row
}

/// A row with a stored resume point: Continue Watching rather than On Deck (cycle 25).
fn resume_point(row: &Value) -> bool {
    row.get("viewOffset").and_then(Value::as_f64).unwrap_or(0.0) != 0.0
}

/// Whether `/hubs` and `/hubs/promoted` drop the On Deck shelf: exactly when every
/// episode on deck is also in Continue Watching (cycle 25), over one `continue_watching`.
fn ondeck_hidden_in(watching: &[Value]) -> bool {
    let deck: HashSet<String> = watching
        .iter()
        .filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"))
        .filter_map(key_of)
        .collect();
    let resuming: HashSet<String> = watching.iter().filter(|row| resume_point(row)).filter_map(key_of).collect();
    !deck.is_empty() && deck == resuming
}
