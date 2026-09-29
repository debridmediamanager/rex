//! Pure metadata identity, release parsing, and artwork helpers.

use std::collections::HashMap;
use std::sync::OnceLock;

use regex::Regex;
use serde_json::{json, Map, Value};
use sha1::{Digest, Sha1};

pub const IMAGE_BASE: &str = "https://image.tmdb.org/t/p/";

fn put(row: &mut Map<String, Value>, key: &str, value: impl Into<Value>) {
    row.insert(key.to_string(), value.into());
}
fn sha1_hex(text: &str) -> String {
    let mut h = Sha1::new();
    h.update(text.as_bytes());
    format!("{:x}", h.finalize())
}

pub fn plex_guid(kind: &str, seed: &str) -> String {
    format!(
        "plex://{}/{}",
        kind,
        &sha1_hex(&format!("plexsim/{}/{}", kind, seed))[..24]
    )
}

fn release_regexes() -> (
    &'static Regex,
    &'static Regex,
    &'static Regex,
    &'static Regex,
    &'static Regex,
    &'static Regex,
) {
    static EXT: OnceLock<Regex> = OnceLock::new();
    static BRACKET: OnceLock<Regex> = OnceLock::new();
    static SEASON_EP: OnceLock<Regex> = OnceLock::new();
    static LOOSE_EP: OnceLock<Regex> = OnceLock::new();
    static YEAR: OnceLock<Regex> = OnceLock::new();
    static CUT: OnceLock<Regex> = OnceLock::new();
    // Keep all patterns equivalent to metadata.py.  In particular, the word
    // boundaries prevent `S01E01` embedded in a movie title and prevent noise
    // tokens such as `stan` from cutting `Constantine`.
    (
        EXT.get_or_init(|| Regex::new(r"(?i)\.(mkv|mp4|webm|avi|ts|m4v)$").unwrap()),
        BRACKET.get_or_init(|| Regex::new(r"[\[\(][^\]\)]*[\]\)]").unwrap()),
        SEASON_EP.get_or_init(|| Regex::new(r"(?i)\bS(\d{1,2})[\s._-]?E(\d{1,3})\b").unwrap()),
        LOOSE_EP.get_or_init(|| Regex::new(r"(?i)(?:^|\s)-\s(\d{2,4})(?:\s|$)").unwrap()),
        YEAR.get_or_init(|| Regex::new(r"\b(19\d{2}|20\d{2})\b").unwrap()),
        CUT.get_or_init(|| Regex::new(
            r"(?i)\b(1080p|2160p|720p|480p|4k|web[\s._-]?dl|webrip|web|bluray|blu[\s._-]?ray|remux|hdtv|dvdrip|brrip|amzn|nf|hmax|glbo|dsnp|atvp|max|hulu|pcok|stan|hevc|x264|x265|h[\s._-]?264|h[\s._-]?265|av1|xvid|divx|ddp?5[\s._-]?1|dd5[\s._-]?1|ddp|dts|truehd|atmos|aac2[\s._-]?0|aac|ac3|eac3|opus|flac|dv|hdr10\+?|hdr|sdr|proper|repack|imax|extended|uncut|dual|multi|complete)\b"
        ).unwrap()),
    )
}

/// Parse the filename grammar used before TMDB lookup.
pub fn parse_release(filename: &str) -> Value {
    let (extension, bracket, season_ep, loose_ep, year_re, cut_re) = release_regexes();
    let mut stem = extension.replace(filename, "").to_string();
    stem = bracket.replace_all(&stem, " ").to_string();
    stem = stem.replace(['.', '_'], " ");
    let (mut season, mut episode): (Option<i64>, Option<i64>) = (None, None);
    if let Some(found) = season_ep.captures(&stem) {
        season = found.get(1).and_then(|value| value.as_str().parse().ok());
        episode = found.get(2).and_then(|value| value.as_str().parse().ok());
        if let Some(start) = found.get(0).map(|value| value.start()) {
            stem.truncate(start);
        }
    } else if let Some(found) = loose_ep.captures(&stem) {
        episode = found.get(1).and_then(|value| value.as_str().parse().ok());
        if let Some(start) = found.get(0).map(|value| value.start()) {
            stem.truncate(start);
        }
    }
    let mut year: Option<i64> = None;
    let mut last_year_start = None;
    for found in year_re.captures_iter(&stem) {
        if let Some(value) = found.get(1) {
            year = value.as_str().parse().ok();
            last_year_start = Some(value.start());
        }
    }
    if let Some(start) = last_year_start {
        stem.truncate(start);
    }
    if let Some(found) = cut_re.find(&stem) {
        stem.truncate(found.start());
    }
    let title = stem
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim_matches(|c: char| matches!(c, ' ' | '-' | '–' | '—'))
        .to_string();
    json!({"title": title, "year": year, "season": season, "episode": episode})
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

fn strip_article(title: &str) -> String {
    let lower = title.to_lowercase();
    for article in ["the ", "a ", "an "] {
        if lower.starts_with(article) {
            return title[article.len()..].to_string();
        }
    }
    title.to_string()
}

fn value_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn truthy_string(object: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| {
        object
            .get(*key)
            .filter(|value| value_truthy(value))
            .and_then(Value::as_str)
            .map(str::to_string)
    })
}

fn value_id(value: Option<&Value>) -> Option<String> {
    let value = value?;
    if !value_truthy(value) {
        return None;
    }
    match value {
        Value::String(value) => Some(value.clone()),
        Value::Number(value) => Some(value.to_string()),
        _ => None,
    }
}

fn year_prefix(value: Option<&Value>) -> Option<i64> {
    let text = value?.as_str()?;
    let prefix = text.chars().take(4).collect::<String>();
    if prefix.chars().count() == 4 && prefix.chars().all(|c| c.is_ascii_digit()) {
        prefix.parse().ok()
    } else {
        None
    }
}

fn round_ties_even(value: f64, digits: i32) -> f64 {
    if !value.is_finite() {
        return value;
    }
    // Scaling first can erase the binary-float side of a decimal midpoint:
    // Python round(8.65, 1) is 8.7, whereas round(86.5) is 86.
    format!("{:.*}", digits.max(0) as usize, value)
        .parse()
        .unwrap_or(value)
}

fn people(details: &Map<String, Value>, mode: &str) -> Vec<Value> {
    let credits = details.get("credits").and_then(Value::as_object);
    let mut rows = Vec::new();
    if mode == "cast" {
        for person in credits
            .and_then(|credits| credits.get("cast"))
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .take(6)
        {
            let Some(person) = person.as_object() else {
                continue;
            };
            let mut row = Map::new();
            put(
                &mut row,
                "tag",
                person.get("name").cloned().unwrap_or(Value::Null),
            );
            put(
                &mut row,
                "role",
                person.get("character").cloned().unwrap_or(Value::Null),
            );
            if let Some(path) = person
                .get("profile_path")
                .filter(|value| value_truthy(value))
                .and_then(Value::as_str)
            {
                put(&mut row, "thumb", format!("{}w185{}", IMAGE_BASE, path));
            }
            rows.push(Value::Object(row));
        }
    } else {
        for person in credits
            .and_then(|credits| credits.get("crew"))
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let Some(person) = person.as_object() else {
                continue;
            };
            let matches = match mode {
                "director" => person.get("job").and_then(Value::as_str) == Some("Director"),
                "writer" => person.get("department").and_then(Value::as_str) == Some("Writing"),
                _ => false,
            };
            if matches {
                let mut row = Map::new();
                put(
                    &mut row,
                    "tag",
                    person.get("name").cloned().unwrap_or(Value::Null),
                );
                rows.push(Value::Object(row));
            }
        }
    }
    let mut unique = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for row in rows.into_iter().take(6) {
        let key = serde_json::to_string(row.get("tag").unwrap_or(&Value::Null))
            .unwrap_or_else(|_| "null".to_string());
        if seen.insert(key) {
            unique.push(row);
        }
        if unique.len() == 6 {
            break;
        }
    }
    unique
}

fn content_rating(details: &Map<String, Value>, kind: &str) -> Option<String> {
    if kind == "tv" {
        for entry in details
            .get("content_ratings")
            .and_then(|v| v.get("results"))
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let Some(entry) = entry.as_object() else {
                continue;
            };
            if matches!(
                entry.get("iso_3166_1").and_then(Value::as_str),
                Some("US") | Some("GB")
            ) {
                if let Some(rating) = entry
                    .get("rating")
                    .and_then(Value::as_str)
                    .filter(|v| !v.is_empty())
                {
                    return Some(rating.to_string());
                }
            }
        }
    } else {
        for entry in details
            .get("release_dates")
            .and_then(|v| v.get("results"))
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let Some(entry) = entry.as_object() else {
                continue;
            };
            if entry.get("iso_3166_1").and_then(Value::as_str) != Some("US") {
                continue;
            }
            for release in entry
                .get("release_dates")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                if let Some(certification) = release
                    .get("certification")
                    .and_then(Value::as_str)
                    .filter(|v| !v.is_empty())
                {
                    return Some(certification.to_string());
                }
            }
        }
    }
    None
}

/// Add the measured subset of TMDB metadata to a row. `details`, episode, and
/// season payloads are supplied by an injected resolver.
pub fn enrich(
    row: &mut Map<String, Value>,
    details: &Value,
    parsed: &Map<String, Value>,
    episode: Option<&Value>,
    season: Option<&Value>,
    kind: &str,
    tmdb_id: i64,
) -> bool {
    let Some(details) = details.as_object() else {
        return false;
    };
    // Python treats an empty details mapping as an identification miss.
    if details.is_empty() {
        return false;
    }
    let title = truthy_string(details, &["title", "name"])
        .or_else(|| {
            parsed
                .get("title")
                .filter(|value| value_truthy(value))
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .unwrap_or_default();
    let date = truthy_string(details, &["release_date", "first_air_date"]).unwrap_or_default();
    put(row, "title", title.clone());
    put(row, "titleSort", strip_article(&title));
    let summary = details
        .get("overview")
        .filter(|value| value_truthy(value))
        .cloned()
        .or_else(|| {
            row.get("summary")
                .filter(|value| value_truthy(value))
                .cloned()
        })
        .unwrap_or_else(|| Value::String(String::new()));
    put(row, "summary", summary);
    if year_prefix(Some(&Value::String(date.clone()))).is_some() {
        put(row, "year", year_prefix(Some(&Value::String(date.clone()))));
        put(row, "originallyAvailableAt", date.clone());
    }
    if let Some(tagline) = details.get("tagline").filter(|value| value_truthy(value)) {
        put(row, "tagline", tagline.clone());
    }
    if let Some(vote) = details
        .get("vote_average")
        .filter(|value| value_truthy(value))
        .and_then(|value| match value {
            Value::Number(number) => number.as_f64(),
            Value::String(string) => string.parse().ok(),
            _ => None,
        })
    {
        let v = round_ties_even(vote, 1);
        put(row, "rating", v);
        put(row, "audienceRating", v);
    }
    if let Some(rating) = content_rating(details, kind) {
        put(row, "contentRating", rating);
    }
    let companies = details
        .get("production_companies")
        .filter(|value| value_truthy(value))
        .or_else(|| details.get("networks").filter(|value| value_truthy(value)))
        .and_then(Value::as_array);
    if let Some(companies) = companies.filter(|companies| !companies.is_empty()) {
        let name = companies
            .first()
            .and_then(Value::as_object)
            .and_then(|company| company.get("name"))
            .cloned()
            .unwrap_or(Value::Null);
        put(row, "studio", name);
    }
    if let Some(genres) = details
        .get("genres")
        .filter(|value| value_truthy(value))
        .and_then(Value::as_array)
    {
        let genres: Vec<Value> = genres
            .iter()
            .filter_map(|g| {
                g.as_object()
                    .map(|g| json!({"tag":g.get("name").cloned().unwrap_or(Value::Null)}))
            })
            .collect();
        if !genres.is_empty() {
            put(row, "Genre", genres);
        }
    }
    if let Some(countries) = details
        .get("production_countries")
        .and_then(Value::as_array)
    {
        let countries: Vec<Value> = countries
            .iter()
            .take(2)
            .filter_map(|country| {
                country.as_object().map(
                    |country| json!({"tag":country.get("name").cloned().unwrap_or(Value::Null)}),
                )
            })
            .collect();
        if !countries.is_empty() {
            put(row, "Country", countries);
        }
    }
    for (field, mode) in [
        ("Role", "cast"),
        ("Director", "director"),
        ("Writer", "writer"),
    ] {
        let people = people(details, mode);
        if !people.is_empty() {
            put(row, field, people);
        }
    }
    let imdb = details
        .get("external_ids")
        .and_then(|x| x.get("imdb_id"))
        .filter(|value| value_truthy(value))
        .and_then(|value| value_id(Some(value)))
        .or_else(|| value_id(details.get("imdb_id")));
    let externals = if let Some(imdb) = imdb.as_deref() {
        json!([{"id":format!("imdb://{}",imdb)},{"id":format!("tmdb://{}",tmdb_id)}])
    } else {
        json!([{"id":format!("tmdb://{}",tmdb_id)}])
    };
    let mut episode_imdb: Option<String> = None;
    let mut episode_tmdb: Option<String> = None;
    let mut episode_tvdb: Option<String> = None;
    let mut episode_found = false;
    let poster = details
        .get("poster_path")
        .filter(|value| value_truthy(value))
        .and_then(Value::as_str)
        .map(str::to_string);
    let mut backdrop = details
        .get("backdrop_path")
        .filter(|value| value_truthy(value))
        .and_then(Value::as_str)
        .map(str::to_string);
    let episode_requested = kind == "tv"
        && parsed.get("season").is_some_and(value_truthy)
        && parsed.get("episode").is_some_and(value_truthy);
    if episode_requested {
        if let Some(ep_value) = episode.filter(|value| value_truthy(value)) {
            let Some(ep) = ep_value.as_object() else {
                return false;
            };
            episode_found = true;
            if let Some(v) = ep.get("name").filter(|value| value_truthy(value)) {
                put(row, "title", v.clone());
            }
            if let Some(v) = ep.get("overview").filter(|value| value_truthy(value)) {
                put(row, "summary", v.clone());
            }
            if let Some(v) = parsed.get("episode") {
                put(row, "index", v.clone());
            }
            if let Some(v) = parsed.get("season") {
                put(row, "parentIndex", v.clone());
            }
            put(row, "grandparentTitle", title.clone());
            if let Some(air) = ep.get("air_date").filter(|value| value_truthy(value)) {
                put(row, "originallyAvailableAt", air.clone());
                if let Some(year) = year_prefix(Some(air)) {
                    put(row, "year", year);
                }
            }
            if let Some(v) = ep
                .get("still_path")
                .filter(|value| value_truthy(value))
                .and_then(Value::as_str)
            {
                backdrop = Some(v.to_string());
            }
            let ext = ep.get("external_ids").and_then(Value::as_object);
            episode_imdb = ext
                .and_then(|x| x.get("imdb_id"))
                .and_then(|value| value_id(Some(value)));
            episode_tmdb = value_id(ep.get("id"));
            episode_tvdb = ext
                .and_then(|x| x.get("tvdb_id"))
                .and_then(|value| value_id(Some(value)));
        }
    }
    // Replace the base identity only when the injected episode payload supplied
    // one; a missing payload follows the same identity path as Python.
    if episode_found && (episode_imdb.is_some() || episode_tmdb.is_some()) {
        let seed = episode_imdb
            .clone()
            .unwrap_or_else(|| format!("tmdb:{}", episode_tmdb.as_deref().unwrap_or_default()));
        put(row, "guid", plex_guid("episode", &seed));
        let mut ids = Vec::new();
        if let Some(v) = episode_imdb.as_deref() {
            ids.push(json!({"id":format!("imdb://{}",v)}));
        }
        if let Some(v) = episode_tmdb.as_deref() {
            ids.push(json!({"id":format!("tmdb://{}",v)}));
        }
        if let Some(v) = episode_tvdb {
            ids.push(json!({"id":format!("tvdb://{}",v)}));
        }
        if ids.is_empty() {
            row.shift_remove("Guid");
        } else {
            put(row, "Guid", ids);
        }
    } else {
        let seed = imdb.clone().unwrap_or_else(|| format!("tmdb:{}", tmdb_id));
        put(row, "guid", plex_guid("movie", &seed));
        put(row, "Guid", externals.clone());
    }
    put(row, "_show_external_guids", externals);
    if let Some(path) = poster.as_deref() {
        put(row, "_poster_url", format!("{}w342{}", IMAGE_BASE, path));
    }
    if let Some(path) = backdrop.as_deref() {
        put(row, "_art_url", format!("{}w1280{}", IMAGE_BASE, path));
    }
    put(row, "_kind", kind);
    put(row, "_tmdb_id", tmdb_id);
    if kind == "tv" {
        put(row, "_show_title", title.clone());
        put(
            row,
            "_show_summary",
            details
                .get("overview")
                .filter(|value| value_truthy(value))
                .cloned()
                .unwrap_or_else(|| Value::String(String::new())),
        );
        let show_year = year_prefix(Some(&Value::String(date.clone())));
        put(row, "_show_year", show_year);
        put(
            row,
            "_show_poster",
            details
                .get("poster_path")
                .filter(|value| value_truthy(value))
                .and_then(Value::as_str)
                .map(|v| format!("{}w342{}", IMAGE_BASE, v)),
        );
        put(
            row,
            "_show_art",
            details
                .get("backdrop_path")
                .filter(|value| value_truthy(value))
                .and_then(Value::as_str)
                .map(|v| format!("{}w1280{}", IMAGE_BASE, v)),
        );
        let show_seed = imdb.clone().unwrap_or_else(|| format!("tmdb:{}", tmdb_id));
        put(row, "_show_guid", plex_guid("show", &show_seed));
        put(
            row,
            "_season",
            parsed.get("season").cloned().unwrap_or(Value::Null),
        );
        put(
            row,
            "_episode",
            parsed.get("episode").cloned().unwrap_or(Value::Null),
        );
        if parsed.get("season").is_some() && !parsed.get("season").is_some_and(Value::is_null) {
            if let Some(season) = season
                .filter(|value| value_truthy(value))
                .and_then(Value::as_object)
            {
                if let Some(id) = value_id(season.get("id")) {
                    put(row, "_season_guid", plex_guid("season", &id));
                    let mut ids = vec![json!({"id":format!("tmdb://{}",id)})];
                    if let Some(tvdb) = season
                        .get("external_ids")
                        .and_then(|x| x.get("tvdb_id"))
                        .and_then(|value| value_id(Some(value)))
                    {
                        ids.push(json!({"id":format!("tvdb://{}",tvdb)}));
                    }
                    put(row, "_season_guids", ids);
                }
                if let Some(v) = season
                    .get("poster_path")
                    .filter(|value| value_truthy(value))
                    .and_then(Value::as_str)
                {
                    put(row, "_season_poster", format!("{}w342{}", IMAGE_BASE, v));
                }
                if let Some(v) = season.get("air_date").filter(|value| value_truthy(value)) {
                    if year_prefix(Some(v)).is_some() {
                        put(row, "_season_air", v.clone());
                    }
                }
                if let Some(v) = season.get("overview").filter(|value| value_truthy(value)) {
                    put(row, "_season_summary", v.clone());
                }
            }
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn enrichment_preserves_python_rounding_and_credit_limit() {
        assert_eq!(round_ties_even(8.65, 1), 8.7);
        assert_eq!(round_ties_even(8.55, 1), 8.6);
        let details = json!({"credits":{"crew":[
            {"name":"A","job":"Director"}, {"name":"A","job":"Director"},
            {"name":"B","job":"Director"}, {"name":"C","job":"Director"},
            {"name":"D","job":"Director"}, {"name":"E","job":"Director"},
            {"name":"F","job":"Director"}
        ]}});
        let result = people(details.as_object().unwrap(), "director");
        assert_eq!(
            result,
            vec![
                json!({"tag":"A"}),
                json!({"tag":"B"}),
                json!({"tag":"C"}),
                json!({"tag":"D"}),
                json!({"tag":"E"})
            ]
        );
    }
    #[test]
    fn guid() {
        assert_eq!(
            plex_guid("movie", "tt1254207"),
            "plex://movie/c18b41265ec9c09e0042e95d"
        );
    }
    #[test]
    fn release() {
        assert_eq!(
            parse_release("The.Movie.2023.1080p.mkv")["title"],
            "The Movie"
        );
    }

    #[test]
    fn release_regex_boundaries_match_measured_names() {
        assert_eq!(parse_release("Maxwell.mkv")["title"], "Maxwell");
        assert_eq!(parse_release("Constantine.mkv")["title"], "Constantine");
        assert_eq!(parse_release("Film.1899")["title"], "Film 1899");
        assert_eq!(parse_release("Film.1899")["year"], Value::Null);
        assert_eq!(parse_release("MovieS01E01.mkv")["title"], "MovieS01E01");
        assert_eq!(
            parse_release("Detective Conan - 1194 1080p.mkv")["episode"],
            1194
        );
    }
}
