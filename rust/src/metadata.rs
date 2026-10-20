//! Pure metadata identity and artwork helpers.

use std::collections::HashMap;

use serde_json::{json, Map, Value};

fn put(row: &mut Map<String, Value>, key: &str, value: impl Into<Value>) {
    row.insert(key.to_string(), value.into());
}

/// Provider calls required by the GUID relinker.  The core transformation
/// does not know about HTTP, caches, or fixture exchanges; production callers
/// inject a real provider implementation.
pub trait PlexProvider {
    fn match_item(
        &mut self,
        kind: &str,
        external_ids: &[String],
    ) -> Option<(String, Option<String>)>;
    fn children(&mut self, guid: &str) -> HashMap<String, String>;
}

fn value_key(value: Option<&Value>) -> Option<String> {
    match value? {
        Value::String(value) => Some(value.clone()),
        Value::Number(value) => Some(value.to_string()),
        _ => None,
    }
}

fn guid_tail(guid: &str) -> &str {
    guid.rsplit('/').next().unwrap_or("")
}

/// Pure hierarchy relinking against an injected provider.
pub fn apply_plex_guids_with_provider(
    items: &mut [Value],
    provider: &mut dyn PlexProvider,
) -> usize {
    let mut relinked = 0;
    let mut matched_shows = Vec::<(Value, String, Option<String>)>::new();
    for item in items.iter_mut() {
        let snapshot = item.as_object().cloned().unwrap_or_default();
        let kind = snapshot.get("type").and_then(Value::as_str).unwrap_or("");
        if kind != "movie" && kind != "show" {
            continue;
        }
        let ids = snapshot
            .get("Guid")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|entry| value_key(entry.get("id")))
            .collect::<Vec<_>>();
        let Some((guid, slug)) = provider.match_item(kind, &ids) else {
            continue;
        };
        if let Some(row) = item.as_object_mut() {
            put(row, "guid", guid.clone());
            if let Some(slug) = slug.as_deref().filter(|slug| !slug.is_empty()) {
                put(row, "slug", slug);
            }
        }
        relinked += 1;
        if kind == "show" {
            matched_shows.push((
                snapshot.get("ratingKey").cloned().unwrap_or(Value::Null),
                guid,
                slug,
            ));
        }
    }

    for (show_key, show_guid, slug) in matched_shows {
        let seasons = provider.children(&show_guid);
        for season_index in 0..items.len() {
            let season_snapshot = items[season_index].as_object().cloned().unwrap_or_default();
            if season_snapshot.get("type").and_then(Value::as_str) != Some("season")
                || season_snapshot.get("parentRatingKey") != Some(&show_key)
            {
                continue;
            }
            // Parent pointers are updated even when the provider has no
            // matching season.  This is what lets later children retain the
            // catalog's season identity while inheriting the matched show.
            if let Some(row) = items[season_index].as_object_mut() {
                put(row, "parentGuid", show_guid.clone());
                if let Some(slug) = slug.as_deref().filter(|slug| !slug.is_empty()) {
                    put(row, "parentSlug", slug);
                }
            }
            let provider_season_guid = seasons
                .get(&value_key(season_snapshot.get("index")).unwrap_or_default())
                .cloned();
            if let Some(season_guid) = provider_season_guid.as_deref() {
                if let Some(row) = items[season_index].as_object_mut() {
                    put(row, "guid", season_guid);
                }
                relinked += 1;
            }
            let inherited_season_guid = provider_season_guid
                .clone()
                .or_else(|| value_key(season_snapshot.get("guid")));
            let episodes = provider_season_guid
                .as_deref()
                .map(|guid| provider.children(guid))
                .unwrap_or_default();
            #[allow(clippy::needless_range_loop)]
            for episode_index in 0..items.len() {
                let episode_snapshot = items[episode_index]
                    .as_object()
                    .cloned()
                    .unwrap_or_default();
                if episode_snapshot.get("type").and_then(Value::as_str) != Some("episode")
                    || episode_snapshot.get("parentRatingKey") != season_snapshot.get("ratingKey")
                {
                    continue;
                }
                if let Some(row) = items[episode_index].as_object_mut() {
                    put(row, "grandparentGuid", show_guid.clone());
                    put(
                        row,
                        "parentGuid",
                        inherited_season_guid
                            .clone()
                            .map(Value::String)
                            .unwrap_or(Value::Null),
                    );
                    if let Some(slug) = slug.as_deref().filter(|slug| !slug.is_empty()) {
                        put(row, "grandparentSlug", slug);
                    }
                    if let Some(guid) =
                        episodes.get(&value_key(episode_snapshot.get("index")).unwrap_or_default())
                    {
                        put(row, "guid", guid.as_str());
                        relinked += 1;
                    }
                }
            }
        }
    }
    relinked
}

/// Recorded exchanges are a test/CLI adapter only.  The adapter indexes
/// request fields rather than serialised JSON, so provider body key ordering
/// cannot alter a match.
pub struct RecordedProvider {
    matches: HashMap<(i64, String), Value>,
    children: HashMap<String, HashMap<String, String>>,
}

impl RecordedProvider {
    pub fn from_value(recorded: &Value) -> Self {
        let mut provider = Self {
            matches: HashMap::new(),
            children: HashMap::new(),
        };
        for entry in recorded.as_array().into_iter().flatten() {
            let Some(entry) = entry.as_object() else {
                continue;
            };
            let method = entry.get("method").and_then(Value::as_str).unwrap_or("");
            let path = entry.get("path").and_then(Value::as_str).unwrap_or("");
            let response = entry.get("response").cloned().unwrap_or(Value::Null);
            if method == "POST" && path == "/library/metadata/matches" {
                let Some(body) = entry.get("body").and_then(Value::as_object) else {
                    continue;
                };
                let Some(kind) = body.get("type").and_then(Value::as_i64) else {
                    continue;
                };
                let Some(guid) = body.get("guid").and_then(Value::as_str) else {
                    continue;
                };
                provider
                    .matches
                    .entry((kind, guid.to_string()))
                    .or_insert(response);
            } else if method == "GET" {
                let mut rows = HashMap::new();
                for row in response
                    .get("MediaContainer")
                    .and_then(|container| container.get("Metadata"))
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    if let (Some(index), Some(guid)) = (
                        value_key(row.get("index")),
                        row.get("guid").and_then(Value::as_str),
                    ) {
                        rows.insert(index, guid.to_string());
                    }
                }
                provider.children.insert(path.to_string(), rows);
            }
        }
        provider
    }
}

impl PlexProvider for RecordedProvider {
    fn match_item(
        &mut self,
        kind: &str,
        external_ids: &[String],
    ) -> Option<(String, Option<String>)> {
        let kind_number = if kind == "movie" { 1 } else { 2 };
        let mut ids = external_ids.to_vec();
        ids.sort_by_key(|id| !id.starts_with("tmdb://"));
        for id in ids {
            let Some(response) = self.matches.get(&(kind_number, id)) else {
                continue;
            };
            let Some(row) = response
                .get("MediaContainer")
                .and_then(|container| container.get("Metadata"))
                .and_then(Value::as_array)
                .and_then(|rows| rows.first())
            else {
                continue;
            };
            let Some(guid) = row.get("guid").and_then(Value::as_str) else {
                continue;
            };
            if !guid.starts_with(&format!("plex://{}/", kind)) {
                continue;
            }
            let slug = row.get("slug").and_then(Value::as_str).map(str::to_string);
            return Some((guid.to_string(), slug));
        }
        None
    }

    fn children(&mut self, guid: &str) -> HashMap<String, String> {
        self.children
            .get(&format!("/library/metadata/{}/children", guid_tail(guid)))
            .cloned()
            .unwrap_or_default()
    }
}

/// Compatibility wrapper for the test/CLI adapter.  Runtime callers should
/// use [`apply_plex_guids_with_provider`] with an actual provider.
pub fn apply_plex_guids_recorded(items: &mut [Value], recorded: &Value) -> usize {
    let mut provider = RecordedProvider::from_value(recorded);
    apply_plex_guids_with_provider(items, &mut provider)
}

/// Python's truthiness for a JSON value: null, false, 0, "" and empty containers are false.
/// The oracle read most optional fields this way; every module reads them through this.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// A provider field that says something: a non-empty string or a number.
fn said<'a>(row: &'a Value, field: &str) -> Option<&'a Value> {
    row.get(field).filter(|value| truthy(value))
}

/// The provider sends `year` as a number or a string (tests/fixtures/provider/matches.json).
fn year_of(row: &Value) -> Option<i64> {
    let year = said(row, "year")?;
    year.as_i64().or_else(|| year.as_str()?.parse().ok())
}

/// A row's external ids (`Guid`), as the provider lists them.
fn external_ids(row: &Value) -> Vec<Value> {
    row.get("Guid")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|entry| Some(json!({"id": entry.get("id")?.as_str().filter(|id| !id.is_empty())?})))
        .collect()
}

/// A tag list (`Genre`, `Role`, ...) in the shape a row carries it: the provider's own entries,
/// down to the fields rows keep, at most `limit` of them.
fn tags(row: &Value, field: &str, limit: usize) -> Option<Vec<Value>> {
    let tags: Vec<Value> = row
        .get(field)?
        .as_array()?
        .iter()
        .filter_map(|entry| {
            let entry = entry.as_object()?;
            let mut tag = Map::new();
            for key in ["tag", "role", "thumb"] {
                if let Some(value) = entry.get(key).filter(|value| truthy(value)) {
                    tag.insert(key.to_string(), value.clone());
                }
            }
            tag.contains_key("tag").then_some(Value::Object(tag))
        })
        .take(limit)
        .collect();
    (!tags.is_empty()).then_some(tags)
}

/// What the provider's season row gives the season a leaf builds (`hierarchy::build`): its guid
/// and ids, poster, air date and summary.
pub fn season_hints(row: &mut Map<String, Value>, season: &Value) {
    if let Some(guid) = said(season, "guid") {
        put(row, "_season_guid", guid.clone());
    }
    let ids = external_ids(season);
    if !ids.is_empty() {
        put(row, "_season_guids", ids);
    }
    for (from, to) in [("thumb", "_season_poster"), ("originallyAvailableAt", "_season_air"), ("summary", "_season_summary")] {
        if let Some(value) = said(season, from) {
            put(row, to, value.clone());
        }
    }
}

/// The collections Plex's agent files a movie under (its match's `Collection`: 1,152 of the
/// 4,109 movie matches the production rex kept, 2026-10-17), as a library's automatic
/// collections group them: the `plex://collection/` guid, its name and summary.
pub fn collections_of(title: &Value) -> Value {
    let entries = title.get("Collection").and_then(Value::as_array).into_iter().flatten();
    Value::Array(
        entries
            .filter_map(|entry| {
                let guid = entry.get("guid")?.as_str().filter(|guid| !guid.is_empty())?;
                let tag = entry.get("tag")?.as_str().filter(|tag| !tag.is_empty())?;
                Some(json!({"guid": guid, "tag": tag, "summary": entry.get("summary").cloned().unwrap_or(json!(""))}))
            })
            .collect(),
    )
}

/// What Plex's metadata agent says a title is, onto a row: rex reads no TMDB and describes a
/// title only by its `plex://` guid (Ben, 2026-10-17). `title` is the provider's movie or show
/// row (its match), `season` and `episode` the rows its `children` list for the numbers the
/// source placed the file on; the underscored fields are what `hierarchy::build` makes the show
/// and season of. An episode the provider has no row for keeps the guid it had.
pub fn describe(
    row: &mut Map<String, Value>,
    title: &Value,
    season: Option<&Value>,
    episode: Option<&Value>,
    show: bool,
    numbers: Option<(i64, i64)>,
) {
    let name = said(title, "title")
        .and_then(Value::as_str)
        .map(str::to_string)
        .or_else(|| row.get("title").and_then(Value::as_str).map(str::to_string))
        .unwrap_or_default();
    put(row, "title", name.clone());
    // The load-time rule (`hierarchy.sort_title`): titleSort only when it differs from the title.
    let derived = crate::hierarchy::sort_title(&name);
    if !derived.is_empty() && derived != name {
        put(row, "titleSort", derived);
    } else {
        row.shift_remove("titleSort");
    }
    let summary = said(title, "summary").cloned().unwrap_or_else(|| json!(""));
    put(row, "summary", summary.clone());
    if let Some(year) = year_of(title) {
        put(row, "year", year);
    }
    for field in ["originallyAvailableAt", "tagline", "contentRating", "studio", "audienceRating"] {
        if let Some(value) = said(title, field) {
            put(row, field, value.clone());
        }
    }
    for (field, limit) in [("Genre", usize::MAX), ("Country", 2), ("Role", 6), ("Director", 6), ("Writer", 6)] {
        if let Some(tags) = tags(title, field, limit) {
            put(row, field, tags);
        }
    }
    let externals = external_ids(title);
    let guid = said(title, "guid").cloned();
    let thumb = said(title, "thumb").cloned();
    let art = said(title, "art").cloned();
    if let Some(thumb) = &thumb {
        put(row, "_poster_url", thumb.clone());
    }
    if let Some(art) = &art {
        put(row, "_art_url", art.clone());
    }
    put(row, "_show_external_guids", externals.clone());
    if !show {
        put(row, "_kind", "movie");
        put(row, "_collections", collections_of(title));
        if let Some(guid) = guid {
            put(row, "guid", guid);
        }
        put(row, "Guid", externals);
        return;
    }
    put(row, "_kind", "tv");
    put(row, "_show_title", name.clone());
    put(row, "_show_summary", summary);
    put(row, "_show_year", year_of(title));
    put(row, "_show_poster", thumb.unwrap_or(Value::Null));
    put(row, "_show_art", art.unwrap_or(Value::Null));
    if let Some(guid) = guid {
        put(row, "_show_guid", guid);
    }
    put(row, "_season", numbers.map(|n| n.0));
    put(row, "_episode", numbers.map(|n| n.1));
    if let Some(season) = season {
        season_hints(row, season);
    }
    let (Some(episode), Some((season_index, episode_index))) = (episode, numbers) else {
        return;
    };
    put(row, "index", episode_index);
    put(row, "parentIndex", season_index);
    put(row, "grandparentTitle", name);
    for field in ["title", "summary", "originallyAvailableAt"] {
        if let Some(value) = said(episode, field) {
            put(row, field, value.clone());
        }
    }
    if let Some(year) = year_of(episode) {
        put(row, "year", year);
    }
    // An episode's still draws its background, as a real PMS's episode rows do.
    if let Some(still) = said(episode, "thumb") {
        put(row, "_art_url", still.clone());
    }
    if let Some(guid) = said(episode, "guid") {
        put(row, "guid", guid.clone());
    }
    let ids = external_ids(episode);
    if ids.is_empty() {
        row.shift_remove("Guid");
    } else {
        put(row, "Guid", ids);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The provider's own rows, recorded from metadata.provider.plex.tv
    /// (tests/fixtures/provider/matches.json): Big Buck Bunny's match.
    fn recorded(path: &str) -> Value {
        // Compiled in: CI's Windows test binary is cross-built, so the manifest path is a Linux one.
        let rows: Value = serde_json::from_str(include_str!("../../tests/fixtures/provider/matches.json")).unwrap();
        rows.as_array().unwrap().iter().find(|row| row["path"].as_str().unwrap().contains(path)).unwrap()["response"].clone()
    }

    #[test]
    fn a_title_is_described_by_plex_alone() {
        let show = recorded("/library/metadata/matches")["MediaContainer"]["Metadata"][0].clone();
        assert_eq!(show["title"], "Big Buck Bunny");
        let mut movie = Map::new();
        describe(&mut movie, &show, None, None, false, None);
        assert_eq!(movie["title"], show["title"]);
        assert_eq!(movie["summary"], show["summary"]);
        assert_eq!(movie["_poster_url"], show["thumb"]);
        assert_eq!(movie["guid"], show["guid"]);
        assert!(movie["Guid"].as_array().is_some_and(|ids| !ids.is_empty()));
        assert!(!movie.contains_key("_show_title"));
        // An episode: the show's row, its season's and its episode's, all the provider's.
        let episode = json!({"title": "Pilot", "summary": "s", "thumb": "https://x/still.jpg",
                             "guid": "plex://episode/5d9c0fd6e264b7001fc8c9d3", "Guid": [{"id": "tmdb://62"}], "year": 2008});
        let season = json!({"guid": "plex://season/5d9c0996e264b7001fc52f30", "thumb": "https://x/s1.jpg", "summary": "first"});
        let mut row = Map::from_iter([("guid".to_string(), json!("local://100001"))]);
        describe(&mut row, &show, Some(&season), Some(&episode), true, Some((1, 1)));
        assert_eq!((&row["title"], &row["index"], &row["parentIndex"]), (&json!("Pilot"), &json!(1), &json!(1)));
        assert_eq!(row["guid"], "plex://episode/5d9c0fd6e264b7001fc8c9d3");
        assert_eq!(row["_show_title"], show["title"]);
        assert_eq!(row["_season_guid"], season["guid"]);
        assert_eq!((&row["_season_poster"], &row["_art_url"]), (&season["thumb"], &episode["thumb"]));
        // An episode carries its own ids, and its season none of a year (cycle 44: 689 of the 1.42.2
        // reference's 742 seasons carry `parentYear` and one a `year` of its own).
        assert_eq!(row["Guid"], json!([{"id": "tmdb://62"}]));
        assert!(!row.contains_key("_season_year"));
        // No episode row: the show's description, and the guid the row had.
        let mut unknown = Map::from_iter([("guid".to_string(), json!("local://100002"))]);
        describe(&mut unknown, &show, None, None, true, Some((9, 99)));
        assert_eq!((&unknown["guid"], &unknown["_show_title"]), (&json!("local://100002"), &show["title"]));
    }

}
