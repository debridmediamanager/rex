//! Which title a release is: what its source says, and nothing rex reads off a name.
//!
//! zurg asks DMM what each file is and pushes the answer with it (`kind`, the movie's or show's
//! ids, TMDB's season and episode; docs/feed-api.md), or a TMDB id a person set by hand in zurg.
//! Those ids are the title. Plex's metadata agent is asked for its `plex://` guid by external id
//! ([`placed`]), and its answer is everything shown about the title: rex reads no TMDB (Ben,
//! 2026-10-17). A release pushed with neither stays unmatched, for Fix Match. No HTTP and no cache
//! live here; the agent arrives as a [`PlexSource`].
//!
//! rex identified by name until 2026-10-17 -- a cleaned name to Plex's matcher and TMDB's search,
//! which named one release in six as a title nobody chose on the user's library, an id written
//! into the name, TMDB's /find for an IMDb id -- and numbered episodes by their names. With DMM
//! placing them, 0.9% of the production server's episodes and 1.1% of its movies arrive unplaced
//! (findings, "The source decides what a file is").

use std::collections::HashMap;

use serde_json::{json, Value};

/// Plex's metadata provider as identify reads it: Python
/// `PlexProvider._call(method, path, body)`.
pub trait PlexSource {
    fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Option<Value>;
}

fn kind_type(kind: &str) -> Option<i64> {
    match kind {
        "movie" => Some(1),
        "show" => Some(2),
        _ => None,
    }
}

/// The first `MediaContainer.Metadata` row, when its guid is a `plex://<kind>/` one.
fn matched_row(payload: Option<Value>, kind: &str) -> Option<(Value, String)> {
    let row = payload?
        .get("MediaContainer")?
        .get("Metadata")?
        .as_array()?
        .first()?
        .clone();
    let guid = row.get("guid")?.as_str()?.to_string();
    guid.starts_with(&format!("plex://{kind}/"))
        .then_some((row, guid))
}

/// The provider's item for a movie or show named by an external id, `tmdb://` ids asked
/// first (`PlexProvider.match_row`, read as `PlexProvider.answer`).
pub fn plex_match(p: &dyn PlexSource, kind: &str, external_ids: &[String]) -> Option<PlexAnswer> {
    let mut ids = external_ids.iter().collect::<Vec<_>>();
    ids.sort_by_key(|id| !id.starts_with("tmdb://"));
    for external in ids {
        let body = json!({"type": kind_type(kind)?, "guid": external});
        if let Some((row, guid)) = matched_row(
            p.call("POST", "/library/metadata/matches", Some(&body)),
            kind,
        ) {
            return Some(PlexAnswer::of(&row, guid, kind));
        }
    }
    None
}

/// Fix Match's candidates for a title and year: the provider's rows for them. A claimed server's
/// token answered `{"type":1,"title":"The Matrix","year":1999}` with The Matrix's guid (findings,
/// "A claimed server needs no account token"); the body a real PMS sends for its own Fix Match
/// search is not recorded (plexmeta_matches_s143.json kept only its length, 84 bytes).
pub fn plex_search(p: &dyn PlexSource, kind: &str, title: &str, year: Option<i64>) -> Vec<Value> {
    let Some(kind_type) = kind_type(kind) else {
        return Vec::new();
    };
    let mut body = json!({"type": kind_type, "title": title});
    if let Some(year) = year {
        body["year"] = json!(year);
    }
    p.call("POST", "/library/metadata/matches", Some(&body))
        .and_then(|answer| answer.pointer("/MediaContainer/Metadata")?.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .filter(|row| row.get("guid").and_then(Value::as_str).is_some_and(|guid| guid.starts_with(&format!("plex://{kind}/"))))
        .collect()
}

/// What `PlexProvider.answer` reads off a matched row, and the row itself: the provider's match
/// carries the title's whole description (summary, artwork, genres, people; tests/fixtures/provider/
/// matches.json).
#[derive(Debug, Clone, PartialEq)]
pub struct PlexAnswer {
    pub row: Value,
    pub guid: String,
    pub kind: String,
    pub title: Option<String>,
    pub year: Option<i64>,
    pub imdb: Option<String>,
    pub tmdb: Option<i64>,
    pub thumb: Option<String>,
    pub art: Option<String>,
}

impl PlexAnswer {
    fn of(row: &Value, guid: String, kind: &str) -> Self {
        let ids = row
            .get("Guid")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|entry| entry.get("id").and_then(Value::as_str).unwrap_or_default())
            .collect::<Vec<_>>();
        let tail = |prefix: &str| {
            ids.iter()
                .find_map(|id| id.strip_prefix(prefix))
                .map(str::to_string)
        };
        let tmdb =
            tail("tmdb://").filter(|id| !id.is_empty() && id.chars().all(|c| c.is_ascii_digit()));
        let text = |field: &str| row.get(field).and_then(Value::as_str).filter(|value| !value.is_empty()).map(str::to_string);
        PlexAnswer {
            row: row.clone(),
            guid,
            kind: kind.to_string(),
            title: text("title"),
            year: row.get("year").and_then(Value::as_i64),
            imdb: tail("imdb://"),
            tmdb: tmdb.and_then(|id| id.parse().ok()),
            thumb: text("thumb"),
            art: text("art"),
        }
    }
}

/// {index: guid} of a show's seasons or a season's episodes.
pub fn plex_children(p: &dyn PlexSource, guid: &str) -> HashMap<String, String> {
    plex_children_rows(p, guid)
        .into_iter()
        .filter_map(|(index, row)| Some((index, row.get("guid")?.as_str().filter(|guid| !guid.is_empty())?.to_string())))
        .collect()
}

/// {index: row} of a show's seasons or a season's episodes, each the provider's whole row.
pub fn plex_children_rows(p: &dyn PlexSource, guid: &str) -> HashMap<String, Value> {
    let tail = guid.rsplit('/').next().unwrap_or_default();
    let payload = p.call("GET", &format!("/library/metadata/{tail}/children"), None);
    let rows = payload
        .as_ref()
        .and_then(|payload| {
            payload
                .get("MediaContainer")?
                .get("Metadata")?
                .as_array()
                .cloned()
        })
        .unwrap_or_default();
    rows.iter()
        .filter_map(|row| {
            let index = match row.get("index")? {
                Value::String(index) => index.clone(),
                Value::Number(index) => index.to_string(),
                _ => return None,
            };
            row.get("guid")?.as_str().filter(|guid| !guid.is_empty())?;
            Some((index, row.clone()))
        })
        .collect()
}

/// Plex's season and episode rows for a file of a show, and the numbers Plex files it under. By
/// the TMDB episode id its source pushed (`tmdb_episode`) when there is one: rex exchanges that
/// content id for the Plex episode whose `Guid` lists it, reading no name, so a file pushed on
/// other numbers than Plex's lands on Plex's own episode (Ben 10: Omniverse's `S02E01 - Showdown
/// Part 1` is TMDB's 1107002, Plex's S3E1). Plex's season at the pushed numbers is looked at first,
/// then every other season. With no id pushed, or one Plex lists nowhere, the row at the pushed
/// numbers, the latter only when that row lists no TMDB id of its own. `seasons` are the
/// show's `plex_children_rows`; `episodes` keeps each season's rows read, by its guid.
pub fn plex_episode(
    p: &dyn PlexSource,
    seasons: &HashMap<String, Value>,
    episodes: &mut HashMap<String, HashMap<String, Value>>,
    tmdb_episode: Option<i64>,
    numbers: Option<(i64, i64)>,
) -> Option<(Value, Value, (i64, i64))> {
    let mut rows_of = |season: &Value| -> HashMap<String, Value> {
        let Some(guid) = season.get("guid").and_then(Value::as_str) else {
            return HashMap::new();
        };
        episodes.entry(guid.to_string()).or_insert_with(|| plex_children_rows(p, guid)).clone()
    };
    let at = |season: i64, rows: &HashMap<String, Value>, episode: i64| rows.get(&episode.to_string()).cloned().map(|row| (row, (season, episode)));
    let Some(id) = tmdb_episode else {
        let (season, episode) = numbers?;
        let found = seasons.get(&season.to_string())?.clone();
        let (row, numbers) = at(season, &rows_of(&found), episode)?;
        return Some((found, row, numbers));
    };
    let wanted = format!("tmdb://{id}");
    let lists = |row: &Value| row.get("Guid").and_then(Value::as_array).is_some_and(|ids| ids.iter().any(|entry| entry.get("id").and_then(Value::as_str) == Some(wanted.as_str())));
    let mut order: Vec<(i64, &Value)> = seasons.iter().filter_map(|(index, season)| Some((index.parse().ok()?, season))).collect();
    order.sort_by_key(|(index, _)| (Some(*index) != numbers.map(|n| n.0), *index));
    for (index, season) in order {
        for (episode, row) in rows_of(season) {
            if lists(&row) {
                return Some((season.clone(), row.clone(), (index, episode.parse().ok()?)));
            }
        }
    }
    // Plex lists the id under no episode: Animaniacs' episodes from S1E2 on carry only imdb:// and
    // tvdb:// ids (rex-fun, 2026-10-23), and matched none. The episode at the pushed numbers stands
    // when it lists no TMDB id at all; one that lists another is another episode than the file's.
    let (season, episode) = numbers?;
    let found = seasons.get(&season.to_string())?.clone();
    let (row, numbers) = at(season, &rows_of(&found), episode)?;
    let tmdb_listed = row.get("Guid").and_then(Value::as_array).is_some_and(|ids| {
        ids.iter().any(|entry| entry.get("id").and_then(Value::as_str).is_some_and(|id| id.starts_with("tmdb://")))
    });
    (!tmdb_listed).then_some((found, row, numbers))
}

/// The title a release is, as its source named it, and what Plex's agent adds to that.
#[derive(Debug, Clone, PartialEq)]
pub struct Identity {
    /// "movie" or "show".
    pub kind: String,
    pub imdb: Option<String>,
    pub tmdb: Option<i64>,
    pub plex: Option<String>,
    /// The provider's row for `plex`: what the title is shown as.
    pub row: Option<Value>,
    pub title: Option<String>,
    pub year: Option<i64>,
    pub thumb: Option<String>,
    pub art: Option<String>,
}

/// A title its source named (`kind` and the movie's or show's ids, docs/feed-api.md): those ids
/// are the title, whatever its name reads and whether or not TMDB knows them, so nothing is
/// guessed. Plex's agent is asked for its guid and description by external id, and an id the push
/// left out is taken from its answer.
pub fn placed(plex: Option<&dyn PlexSource>, kind: &str, tmdb: Option<i64>, imdb: Option<&str>) -> Identity {
    let mut identity = Identity {
        kind: kind.to_string(),
        imdb: imdb.filter(|imdb| imdb.starts_with("tt")).map(str::to_string),
        tmdb: tmdb.filter(|id| *id > 0),
        plex: None,
        row: None,
        title: None,
        year: None,
        thumb: None,
        art: None,
    };
    if let Some(p) = plex {
        let externals: Vec<String> = identity
            .tmdb
            .map(|id| format!("tmdb://{id}"))
            .into_iter()
            .chain(identity.imdb.as_ref().map(|imdb| format!("imdb://{imdb}")))
            .collect();
        if let Some(answer) = plex_match(p, kind, &externals) {
            identity.plex = Some(answer.guid);
            identity.row = Some(answer.row);
            (identity.thumb, identity.art) = (answer.thumb, answer.art);
            (identity.title, identity.year) = (answer.title, answer.year);
            identity.tmdb = identity.tmdb.or(answer.tmdb);
            identity.imdb = identity.imdb.or(answer.imdb);
        }
    }
    identity
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    /// Plex's agent knowing The Matrix by its TMDB id and its IMDb id; every request kept.
    #[derive(Default)]
    struct MatrixPlex(RefCell<Vec<Value>>);
    impl PlexSource for MatrixPlex {
        fn call(&self, _method: &str, _path: &str, body: Option<&Value>) -> Option<Value> {
            self.0.borrow_mut().push(body.cloned().unwrap_or_default());
            let known = [json!({"type": 1, "guid": "tmdb://603"}), json!({"type": 1, "guid": "imdb://tt0133093"})];
            Some(if body.is_some_and(|body| known.contains(body)) {
                json!({"MediaContainer": {"Metadata": [{"guid": "plex://movie/5d776825880197001ec967c8", "title": "The Matrix",
                                                         "year": 1999, "Guid": [{"id": "imdb://tt0133093"}, {"id": "tmdb://603"}]}]}})
            } else {
                json!({})
            })
        }
    }

    #[test]
    fn a_placed_title_is_its_ids() {
        let plex = MatrixPlex::default();
        // The ids are the title, and Plex names its guid by them, TMDB's first.
        let both = placed(Some(&plex), "movie", Some(603), Some("tt0133093"));
        assert_eq!((both.tmdb, both.imdb.as_deref()), (Some(603), Some("tt0133093")));
        assert_eq!(both.plex.as_deref(), Some("plex://movie/5d776825880197001ec967c8"));
        assert_eq!((both.title.as_deref(), both.year), (Some("The Matrix"), Some(1999)));
        assert_eq!(plex.0.borrow()[0], json!({"type": 1, "guid": "tmdb://603"}));
        // An id the push left out comes from Plex's answer, never from a name or TMDB's /find.
        let imdb_only = placed(Some(&plex), "movie", None, Some("tt0133093"));
        assert_eq!(imdb_only.tmdb, Some(603));
        // An id nobody answers for is still the title.
        let unknown = placed(None, "show", Some(30983), None);
        assert_eq!((unknown.kind.as_str(), unknown.tmdb, unknown.imdb, unknown.plex), ("show", Some(30983), None, None));
        let unanswered = placed(Some(&plex), "show", Some(30983), Some("not-an-imdb-id"));
        assert_eq!((unanswered.tmdb, unanswered.imdb, unanswered.plex), (Some(30983), None, None));
    }

    /// Plex's Animaniacs, as its agent answered rex-fun on 2026-10-23: its episodes from S1E2 on
    /// list only `imdb://` and `tvdb://` ids, so a pushed `tmdb_episode` names none of them.
    struct AnimaniacsPlex;
    impl PlexSource for AnimaniacsPlex {
        fn call(&self, _method: &str, path: &str, _body: Option<&Value>) -> Option<Value> {
            (path == "/library/metadata/65dd8dbef97685e8c55065b5/children").then(|| json!({"MediaContainer": {"Metadata": [
                {"index": 8, "guid": "plex://episode/variety", "title": "Variety Speak", "Guid": [{"id": "imdb://tt1602152"}, {"id": "tvdb://102246"}]},
                {"index": 9, "guid": "plex://episode/tenors", "title": "Three Tenors and You're Out", "Guid": [{"id": "imdb://tt2385352"}, {"id": "tvdb://102247"}]},
                {"index": 10, "guid": "plex://episode/bingo", "title": "Bingo", "Guid": [{"id": "tmdb://999"}, {"id": "tvdb://102248"}]}
            ]}}))
        }
    }

    #[test]
    fn an_id_plex_lists_nowhere_falls_back_to_the_numbers_only_where_plex_lists_no_tmdb_id() {
        let seasons: HashMap<String, Value> = [("3".to_string(), json!({"index": 3, "guid": "plex://season/65dd8dbef97685e8c55065b5"}))].into();
        let mut episodes = HashMap::new();
        // `Animaniacs.S03E09.Three.Tenors.and.Youre.Out`: DMM's S3E9, TMDB episode 1906705, which
        // Plex lists under no episode; its S3E9 has no TMDB id of its own, so the numbers stand.
        let found = plex_episode(&AnimaniacsPlex, &seasons, &mut episodes, Some(1906705), Some((3, 9)));
        assert_eq!(found.map(|(_, row, numbers)| (row["title"].clone(), numbers)), Some((json!("Three Tenors and You're Out"), (3, 9))));
        // Plex's S3E10 lists another TMDB episode: those numbers are another episode than the file's.
        assert_eq!(plex_episode(&AnimaniacsPlex, &seasons, &mut episodes, Some(1906706), Some((3, 10))), None);
    }
}
