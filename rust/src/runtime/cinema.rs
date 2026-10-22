//! Cinema queues use only clips explicitly supplied by a source and registered media.
//! Pre-roll separators follow https://support.plex.tv/articles/202920803-extras/.
use super::*;

#[derive(Default)]
pub(super) struct CinemaPrefix {
    pub rows: Vec<Value>,
    /// The requested feature, not the prefix's selected playback item.
    pub main_key: Option<String>,
}

impl Runtime {
    /// Called once on POST, after ordinary shuffle and before minting QueueItems.
    /// Insert `rows` immediately before main_key, selecting the first inserted
    /// QueueItem regardless of query.key. Retain main_key as the metadata identity
    /// while a prefix is selected; preserve each prefix row's Media projection.
    pub(super) fn cinema_prefix(
        &self,
        query: &HashMap<String, String>,
        rows: &[Value],
        entropy: u64,
    ) -> Result<CinemaPrefix, Response> {
        let Some(count) = query.get("extrasPrefixCount") else {
            return Ok(CinemaPrefix::default());
        };
        let count = count.parse::<i32>().map_err(|_| Response::error(400, "rex: invalid extrasPrefixCount"))?;
        // Official clients also send -1 for ordinary continuous playback.
        if count <= 0 || query.get("type").map(String::as_str) != Some("video") {
            return Ok(CinemaPrefix::default());
        }
        let wanted = query.get("key").map(|key| extract_key(key));
        let Some(main) = wanted.as_ref().and_then(|key| rows.iter().find(|row| key_of(*row).as_ref() == Some(key)))
            .or_else(|| rows.first()) else {
            return Ok(CinemaPrefix::default());
        };
        if main.get("type").and_then(Value::as_str) != Some("movie") || !self.visible(main) {
            return Ok(CinemaPrefix::default());
        }
        let section = main.get("librarySectionID").map(string_value).unwrap_or_default();
        if self.section_prefs.get(&(section, "enableCinemaTrailers".into()))
            .is_some_and(|value| !is_true_option(value)) {
            return Ok(CinemaPrefix::default());
        }
        let mut prefix = Vec::new();
        let mut seen_files = rows.iter().flat_map(scan::row_files).collect::<HashSet<_>>();
        if self.prefs.get("CinemaTrailersFromLibrary").map_or(true, truthy) {
            let unwatched = self.prefs.get("CinemaTrailersType").map(string_value).as_deref() != Some("0");
            let mut movies = self.catalog.hot().lazy()
                .filter(|row| row.get("type").and_then(Value::as_str) == Some("movie"))
                .filter_map(|row| key_of(&row))
                .collect::<Vec<_>>();
            movies.sort();
            let mut seen_movies = HashSet::new();
            // No queued feature (or another version of it) advertises itself.
            for row in rows { seen_movies.insert(cinema_identity(row)); }
            for key in movies {
                let Some(movie) = self.catalog.get(&key) else { continue };
                if movie.get("deletedAt").is_some() || !self.visible(movie)
                    || (unwatched && watched_leaf(movie))
                    || !seen_movies.insert(cinema_identity(movie)) { continue; }
                // One trailer per movie, in the source's explicit extra index order.
                if let Some(clip) = self.extras_of(&key).into_iter().find(|clip| {
                    clip.get("extraType").map(string_value).as_deref() == Some("1")
                        && self.cinema_visible(clip) && self.cinema_playable(clip)
                        && !scan::row_files(clip).iter().any(|file| seen_files.contains(file))
                }) {
                    seen_files.extend(scan::row_files(clip));
                    prefix.push(clip.clone());
                    if prefix.len() >= count as usize { break; }
                }
            }
        }
        let configured = self.prefs.get("CinemaTrailersPrerollID").map(string_value).unwrap_or_default();
        for path in preroll_paths(&configured, entropy).map_err(|message| Response::error(400, message))? {
            // Exact existing Part.file only. Never open arbitrary local files, identify
            // names, manufacture clip rows, or widen an account's source permissions.
            let row = self.catalog.hot().lazy().find_map(|row| {
                let key = key_of(&row)?;
                let row = self.catalog.get(&key)?;
                if !self.cinema_visible(row) || row.get("deletedAt").is_some() { return None; }
                let media = row.get("Media")?.as_array()?.iter().find_map(|media| {
                    if media.get("deletedAt").is_some() { return None; }
                    let part = media.get("Part")?.as_array()?.iter().find(|part| {
                        part.get("file").and_then(Value::as_str) == Some(path)
                            && self.part_source(row, path).is_some()
                    })?;
                    let mut selected = media.clone();
                    selected["Part"] = json!([part]);
                    Some(selected)
                })?;
                let mut selected = row.clone();
                selected["Media"] = json!([media]);
                Some(selected)
            }).ok_or_else(|| Response::error(409, "rex: configured cinema pre-roll is missing or inaccessible"))?;
            if rows.iter().any(|main| cinema_identity(main) == cinema_identity(&row)) {
                return Err(Response::error(409, "rex: cinema pre-roll cannot be the queued feature"));
            }
            prefix.push(row);
        }
        let main_key = if prefix.is_empty() { None } else { key_of(main) };
        Ok(CinemaPrefix { rows: prefix, main_key })
    }

    fn cinema_visible(&self, row: &Value) -> bool {
        if !self.visible(row) { return false; }
        if row.get("type").and_then(Value::as_str) == Some("clip") {
            // Clips have an internal section/owner, not public librarySectionID.
            return row.get("_parentRatingKey").map(string_value)
                .and_then(|key| self.catalog.get(&key))
                .is_some_and(|parent| self.visible(parent) && parent.get("deletedAt").is_none());
        }
        matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode"))
    }

    fn cinema_playable(&self, row: &Value) -> bool {
        row.get("deletedAt").is_none() && row.get("Media").and_then(Value::as_array)
            .into_iter().flatten().any(|media| media.get("deletedAt").is_none()
                && media.get("Part").and_then(Value::as_array).into_iter().flatten().any(|part| {
                    part.get("file").and_then(Value::as_str).is_some_and(|file| self.part_source(row, file).is_some())
                }))
    }
}

fn cinema_identity(row: &Value) -> String {
    row.get("guid").and_then(Value::as_str).filter(|guid| !guid.is_empty())
        .map(str::to_string).or_else(|| key_of(row)).unwrap_or_default()
}

fn preroll_paths(configured: &str, entropy: u64) -> Result<Vec<&str>, &'static str> {
    if configured.is_empty() { return Ok(Vec::new()); }
    if configured.contains(';') && configured.contains(',') {
        return Err("rex: cinema pre-roll cannot mix comma and semicolon separators");
    }
    let paths = configured.split(if configured.contains(';') { ';' } else { ',' }).collect::<Vec<_>>();
    if paths.iter().any(|path| path.is_empty()) {
        return Err("rex: cinema pre-roll contains an empty path");
    }
    if configured.contains(';') {
        Ok(vec![paths[(entropy % paths.len() as u64) as usize]])
    } else { Ok(paths) }
}

#[cfg(test)]
#[path = "cinema_tests.rs"]
mod tests;
