//! Pure metadata identity and artwork helpers.

use std::collections::HashMap;

use serde_json::{json, Map, Value};

use crate::hotrow::Field;

fn put(row: &mut Map<String, Value>, key: &str, value: impl Into<Value>) {
    row.insert(key.to_string(), value.into());
}

/// `key` set to what the agent says, or gone when it says nothing: a title described again (Fix
/// Match) keeps nothing of the one it was before.
fn put_or_drop(row: &mut Map<String, Value>, key: &str, value: Option<Value>) {
    match value {
        Some(value) => put(row, key, value),
        None => {
            row.shift_remove(key);
        }
    }
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

    // Each row's children by `parentRatingKey`, compared as JSON as before (`"6"` is not `6`), in
    // item order: a pass over every item per show and per season was 450M copies for a
    // 2,000-show library.
    let mut children: HashMap<Option<String>, Vec<usize>> = HashMap::new();
    for (index, item) in items.iter().enumerate() {
        children.entry(item.get("parentRatingKey").map(Value::to_string)).or_default().push(index);
    }
    let of_type = |items: &[Value], index: usize, kind: &str| items[index].get("type").and_then(Value::as_str) == Some(kind);
    for (show_key, show_guid, slug) in matched_shows {
        let seasons = provider.children(&show_guid);
        for &season_index in children.get(&Some(show_key.to_string())).into_iter().flatten() {
            if !of_type(items, season_index, "season") {
                continue;
            }
            let season_key = items[season_index].get("ratingKey").map(Value::to_string);
            let season_index_text = value_key(items[season_index].get("index")).unwrap_or_default();
            let season_guid_before = value_key(items[season_index].get("guid"));
            // Parent pointers are updated even when the provider has no
            // matching season.  This is what lets later children retain the
            // catalog's season identity while inheriting the matched show.
            if let Some(row) = items[season_index].as_object_mut() {
                put(row, "parentGuid", show_guid.clone());
                if let Some(slug) = slug.as_deref().filter(|slug| !slug.is_empty()) {
                    put(row, "parentSlug", slug);
                }
            }
            let provider_season_guid = seasons.get(&season_index_text).cloned();
            if let Some(season_guid) = provider_season_guid.as_deref() {
                if let Some(row) = items[season_index].as_object_mut() {
                    put(row, "guid", season_guid);
                }
                relinked += 1;
            }
            let inherited_season_guid = provider_season_guid.clone().or(season_guid_before);
            let episodes = provider_season_guid
                .as_deref()
                .map(|guid| provider.children(guid))
                .unwrap_or_default();
            for &episode_index in children.get(&season_key).into_iter().flatten() {
                if !of_type(items, episode_index, "episode") {
                    continue;
                }
                let episode_index_text = value_key(items[episode_index].get("index")).unwrap_or_default();
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
                    if let Some(guid) = episodes.get(&episode_index_text) {
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
pub fn truthy<'a>(value: impl Into<Field<'a>>) -> bool {
    match value.into() {
        Field::Null => false,
        Field::Bool(flag) => flag,
        Field::Int(number) => number != 0,
        Field::Float(number) => number != 0.0,
        Field::Str(text) => !text.is_empty(),
        field => match field.value().as_ref() {
            Value::Null => false,
            Value::Bool(flag) => *flag,
            Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
            Value::String(text) => !text.is_empty(),
            Value::Array(items) => !items.is_empty(),
            Value::Object(map) => !map.is_empty(),
        },
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
/// down to the fields rows keep, at most `limit` of them. A person's `tagKey` is the agent's own id
/// for them, as plexlab's (1.43.4) Firefly answers it (`5d7768286f4521001ea9945c`, Nathan Fillion).
fn tags(row: &Value, field: &str, limit: usize) -> Option<Vec<Value>> {
    let person = crate::hierarchy::PERSON_TAG_ELEMENTS.contains(&field);
    let tags: Vec<Value> = row
        .get(field)?
        .as_array()?
        .iter()
        .filter_map(|entry| {
            let entry = entry.as_object()?;
            let mut tag = Map::new();
            for (key, from) in [("tag", "tag"), ("tagKey", "id"), ("role", "role"), ("thumb", "thumb")] {
                if key == "tagKey" && !person {
                    continue;
                }
                if let Some(value) = entry.get(from).filter(|value| truthy(value)) {
                    tag.insert(key.to_string(), value.clone());
                }
            }
            tag.contains_key("tag").then_some(Value::Object(tag))
        })
        .take(limit)
        .collect();
    (!tags.is_empty()).then_some(tags)
}

/// The url of the agent's first image of `kind` (`clearLogo`, `backgroundSquare`) in its `Image` list.
fn image(row: &Value, kind: &str) -> Option<Value> {
    row.get("Image")?
        .as_array()?
        .iter()
        .find(|image| image.get("type").and_then(Value::as_str) == Some(kind))
        .and_then(|image| said(image, "url"))
        .cloned()
}

/// The people Plex lists on a title: every one the agent names. The production rex kept six of
/// each (a median show's cast is 39, 2026-11-04) where plexlab shows Firefly's 112, and no producer.
const PEOPLE: [&str; 4] = ["Role", "Director", "Writer", "Producer"];

/// The `_described` a row described from all of these carries (`agent_fields`); a row without
/// it was described before they were kept, and `feed::describe_again` reads them back for it.
pub const DESCRIBED: i64 = 3;

/// What a start fetches for rows an older `DESCRIBED` left without it, as its log names it: say
/// what the new one adds when raising `DESCRIBED`.
pub const DESCRIBED_ADDS: &str = "ratings (IMDb, Rotten Tomatoes, TMDB)";

/// The rating fields `ratings` sets; `feed::describe_again` drops one the agent no longer gives.
pub const RATINGS: [&str; 5] = ["rating", "ratingImage", "audienceRating", "audienceRatingImage", "Rating"];

/// The lock a person's edit of a rating field sets (`audienceRating.locked=1` locks
/// `audienceRating`), an image going with its score; the `Rating` list has none.
pub fn rating_lock(field: &str) -> Option<&'static str> {
    match field {
        "rating" | "ratingImage" => Some("rating"),
        "audienceRating" | "audienceRatingImage" => Some("audienceRating"),
        _ => None,
    }
}

/// `ratings` onto a row: each field set or, when the agent gives none, dropped, unless a person
/// locked it. Whether anything changed.
pub fn put_ratings(row: &mut Map<String, Value>, ratings: [(&'static str, Option<Value>); 5], locked: impl Fn(&Map<String, Value>, &str) -> bool) -> bool {
    let mut changed = false;
    for (field, value) in ratings {
        if rating_lock(field).is_some_and(|lock| locked(row, lock)) {
            continue;
        }
        changed |= match value {
            Some(value) if row.get(field) == Some(&value) => false,
            Some(value) => {
                row.insert(field.to_string(), value);
                true
            }
            None => row.shift_remove(field).is_some(),
        };
    }
    changed
}

/// A title's ratings as a real PMS shows them from its agent's answer (plexlab's Firefly, The Train
/// Job and Big Buck Bunny, `agent_description_s1434.json`; 25 films, 12 shows and 12 episodes of a
/// 1.43.5 PMS against the agent, 2026-11-19): a film's as the agent gives them, Rotten Tomatoes'
/// critic and audience scores; a show's and an episode's audience score is its TMDB one, with no
/// critic score, and none when TMDB gives none. A film's `rating` only beside its `ratingImage`:
/// the agent gives Tears of Steel `rating` 6 with no image, and plexlab shows none
/// (`ratings_s1434.json`). Every value to one decimal (Firefly's TMDB 8.348 was plexlab's 8.3,
/// 8.351 its 8.4; a value exactly halfway is unmeasured).
// shortcut: a movie library's `ratingsSource` imdb or themoviedb is not measured; the default (Rotten Tomatoes) is answered.
pub fn ratings(title: &Value, kind: &str) -> [(&'static str, Option<Value>); 5] {
    let round = |value: &Value| value.as_f64().and_then(|value| format!("{value:.1}").parse::<f64>().ok()).map(Value::from);
    let list: Vec<Value> = title.get("Rating").and_then(Value::as_array).into_iter().flatten()
        .filter_map(|entry| {
            let mut entry = entry.as_object()?.clone();
            let value = round(entry.get("value")?)?;
            entry.insert("value".to_string(), value);
            Some(Value::Object(entry))
        })
        .collect();
    let (rating, image, audience, audience_image) = if kind == "movie" {
        let image = said(title, "ratingImage").cloned();
        (said(title, "rating").filter(|_| image.is_some()).and_then(round), image,
         said(title, "audienceRating").and_then(round), said(title, "audienceRatingImage").cloned())
    } else {
        let tmdb = list.iter().find(|entry| entry["image"].as_str().is_some_and(|image| image.starts_with("themoviedb://")));
        (None, None, tmdb.map(|entry| entry["value"].clone()), tmdb.map(|entry| entry["image"].clone()))
    };
    let list = Some(Value::Array(list)).filter(|list| list.as_array().is_some_and(|list| !list.is_empty()));
    [("rating", rating), ("ratingImage", image), ("audienceRating", audience), ("audienceRatingImage", audience_image), ("Rating", list)]
}

/// What Plex's agent says of a title beside its name, summary and artwork, which rex dropped until
/// 2026-11-04: a film's and a show's theme music, clear logo and square art (served at the row's
/// own `theme`, `clearLogo` and `squareArt` paths) and original title; a film's and an episode's
/// every credited person; a show's cast and networks, which plexlab files as tags it never
/// projects onto a row (a filter and an axis only). Each with what the agent says, or None.
/// plexlab (1.43.4) shows Firefly without the directors, writers and producers its agent names.
pub fn agent_fields(title: &Value, kind: &str) -> Vec<(&'static str, Option<Value>)> {
    let mut fields = Vec::new();
    if kind != "episode" {
        fields.extend([
            ("_theme_url", said(title, "theme").cloned()),
            ("_clearLogo_url", image(title, "clearLogo")),
            ("_squareArt_url", image(title, "backgroundSquare")),
            ("originalTitle", said(title, "originalTitle").cloned()),
        ]);
    }
    let people: &[&'static str] = if kind == "show" { &["Network", "Role"] } else { &PEOPLE };
    for field in people {
        fields.push((*field, tags(title, field, usize::MAX).map(Value::from)));
    }
    fields.extend(ratings(title, kind));
    fields
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
    for field in ["originallyAvailableAt", "tagline", "contentRating", "studio"] {
        if let Some(value) = said(title, field) {
            put(row, field, value.clone());
        }
    }
    for (field, limit) in [("Genre", usize::MAX), ("Country", 2)] {
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
    put(row, "_described", DESCRIBED);
    if !show {
        put(row, "_kind", "movie");
        put(row, "_collections", collections_of(title));
        for (field, value) in agent_fields(title, "movie") {
            put_or_drop(row, field, value);
        }
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
    // The show's own fields ride on its episode's leaf for `hierarchy::build` (`SHOW_HINTS`); each
    // episode carries its own credits, below, and none of its show's.
    put(row, "_show_described", DESCRIBED);
    for (field, value) in agent_fields(title, "show") {
        if let Some((_, hint)) = crate::hierarchy::SHOW_HINTS.iter().find(|(name, _)| *name == field) {
            put(row, hint, value.unwrap_or(Value::Null));
        }
    }
    // An episode's ratings are its own (below), never its show's: rex once showed every episode
    // its show's score under a TMDB badge.
    for field in PEOPLE.iter().chain(&RATINGS) {
        row.shift_remove(*field);
    }
    // A show Fix Matched is described onto its own row, which takes them as a built show does.
    if row.get("type").and_then(Value::as_str) == Some("show") {
        for (field, hint) in crate::hierarchy::SHOW_HINTS {
            match row.shift_remove(*hint).filter(|value| !value.is_null()) {
                Some(value) => put(row, field, value),
                None => {
                    row.shift_remove(*field);
                }
            }
        }
    }
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
    // plexlab's The Train Job: the episode's 24 credited actors, its director, writers and producers.
    for (field, value) in agent_fields(episode, "episode") {
        put_or_drop(row, field, value);
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

    /// Plex's agent's Firefly (TMDB 1437), its season 1 and The Train Job, as `specials.json`
    /// recorded them: what plexlab shows of the same title is in `agent_description_s1434.json`.
    fn firefly() -> (Value, Value, Value) {
        let plex: Value = serde_json::from_str(include_str!("../../tests/fixtures/feed/specials.json")).unwrap();
        let rows = |key: &str| plex["plex"][key]["MediaContainer"]["Metadata"].as_array().unwrap().clone();
        let show = rows("POST /library/metadata/matches {\"guid\": \"tmdb://1437\", \"type\": 2}")[0].clone();
        let all: Vec<Value> = plex["plex"].as_object().unwrap().values().flat_map(|answer| answer["MediaContainer"]["Metadata"].as_array().cloned().unwrap_or_default()).collect();
        let season = all.iter().find(|row| row["type"] == "season" && row["index"] == 1).unwrap().clone();
        let episode = all.iter().find(|row| row["guid"] == "plex://episode/5d9c127e4eefaa001f6449c2").unwrap().clone();
        (show, season, episode)
    }

    #[test]
    fn a_film_keeps_its_logo_square_art_original_title_and_every_person() {
        let bbb = recorded("/library/metadata/matches")["MediaContainer"]["Metadata"][0].clone();
        let mut movie = Map::new();
        describe(&mut movie, &bbb, None, None, false, None);
        assert!(movie["_clearLogo_url"].as_str().unwrap().ends_with(".png"));
        assert!(movie["_squareArt_url"].as_str().unwrap().contains("/gracenote/"));
        assert!(!movie.contains_key("_theme_url"), "the movie agent gives no theme");
        // plexlab's Big Buck Bunny: producer Ton Roosendaal, by the agent's person id.
        assert_eq!(movie["Producer"][0]["tag"], "Ton Roosendaal");
        assert_eq!(movie["Producer"][0]["tagKey"], "5d776834103a2d001f567902");
        assert_eq!(movie["_described"], DESCRIBED);
        let mut serbian = json!({"title": "A Serbian Film", "originalTitle": "Srpski film", "Role": (0..25).map(|n| json!({"tag": format!("Actor {n}"), "id": format!("{n:024}")})).collect::<Vec<_>>()});
        describe(&mut movie, &serbian, None, None, false, None);
        assert_eq!((movie["originalTitle"].as_str(), movie["Role"].as_array().map(Vec::len)), (Some("Srpski film"), Some(25)));
        // Described again as a title that says none of it: nothing of the first lingers.
        serbian = json!({"title": "Other"});
        describe(&mut movie, &serbian, None, None, false, None);
        assert!(["originalTitle", "Role", "Producer", "_clearLogo_url", "_squareArt_url"].iter().all(|field| !movie.contains_key(*field)));
    }

    #[test]
    fn a_show_keeps_its_theme_logo_networks_and_cast_and_an_episode_its_own_credits() {
        let (show, season, episode) = firefly();
        let mut leaf = Map::new();
        describe(&mut leaf, &show, Some(&season), Some(&episode), true, Some((1, 1)));
        assert_eq!(leaf["_show_theme"], "https://tvthemes.plexapp.com/78874.mp3");
        assert!(leaf["_show_clearLogo"].as_str().unwrap().ends_with(".png"));
        assert_eq!(leaf["_show_Network"], json!([{"tag": "FOX"}]));
        // plexlab shows Firefly's 112, The Train Job its own 24, its director, writers and producers.
        assert_eq!(leaf["_show_Role"].as_array().unwrap().len(), 112);
        assert_eq!(leaf["_show_Role"][0], json!({"tag": "Nathan Fillion", "tagKey": "5d7768286f4521001ea9945c", "role": "Mal Reynolds",
                                                "thumb": "https://metadata-static.plex.tv/8/people/8e60c5a6412557c52b32ab5e85a2ce34.jpg"}));
        assert_eq!(leaf["Role"].as_array().unwrap().len(), 24);
        assert_eq!(leaf["Director"][0]["tag"], "Joss Whedon");
        assert_eq!(leaf["Producer"].as_array().unwrap().len(), 2);
        assert!(!leaf.contains_key("_theme_url") && !leaf.contains_key("Network"), "the show's own are hints only");
        // Built, the show takes them, and the episode keeps its own credits.
        let rows = crate::hierarchy::build(vec![Value::Object(leaf)]);
        let built = rows.iter().find(|row| row["type"] == "show").unwrap();
        assert_eq!((built["_theme_url"].as_str(), built["Role"].as_array().map(Vec::len)), (Some("https://tvthemes.plexapp.com/78874.mp3"), Some(112)));
        assert_eq!(built["Network"], json!([{"tag": "FOX"}]));
        assert_eq!(built["_described"], DESCRIBED);
        assert!(!built.as_object().unwrap().keys().any(|key| key.starts_with("_show_")));
        let ep = rows.iter().find(|row| row["type"] == "episode").unwrap();
        assert_eq!(ep["Role"].as_array().map(Vec::len), Some(24));
        // A show row Fix Matched onto the title is described as its own row.
        let mut matched = Map::from_iter([("type".to_string(), json!("show")), ("Role".to_string(), json!([{"tag": "old"}]))]);
        describe(&mut matched, &show, None, None, true, None);
        assert_eq!(matched["Role"].as_array().map(Vec::len), Some(112));
        assert_eq!(matched["_theme_url"], "https://tvthemes.plexapp.com/78874.mp3");
        assert!(!matched.keys().any(|key| crate::hierarchy::SHOW_HINTS.iter().any(|(_, hint)| hint == key)));
    }

}
