//! Which title a release is, from the most exact evidence to the least:
//! the port of `identify.py`, with the request shaping of `metadata.Tmdb` and
//! `metadata.PlexProvider` it reads through. No HTTP and no cache live here; the
//! providers arrive as [`TmdbSource`] and [`PlexSource`].
//!
//! Measured on the user's own library on 2026-10-06 (`tests/fixtures/identify/`,
//! DMM's labels as the answer key): the file name to TMDB's first search hit named
//! 167 of 200 right. Plex's own matcher, sent what a real PMS sends, and TMDB together
//! name 193, and where the two agree they are right 169 of 173 times.
//!
//! So, in order: an id written into the name, the hash's known IMDb id (`known`: zurg
//! asks DMM and hands it over), then the cleaned name to Plex's matcher and TMDB, with
//! their agreement as the confidence. Nothing is guessed: a release no source names
//! stays unidentified, for Fix Match.
//!
//! The patterns are `identify.py`'s, kept Unicode-aware as Python's `re` is on str.
//! ponytail: the regex crate's `\w` also counts combining marks and connector
//! punctuation (Python's does not, and counts superscript digits such as `²`), its `\s` lacks
//! `\x1c`-`\x1f`, and `int()` here reads ASCII digits only. A 150,200-name fuzz against
//! `identify.parse_name` parted on those characters alone; no release in the library
//! carries one.

use std::collections::HashMap;
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{json, Value};

use crate::metadata::parse_release;

// Confidence, most to least certain. `exact` came from an id, not a name.
pub const EXACT: &str = "exact";
pub const AGREED: &str = "agreed";
pub const SINGLE: &str = "single";
pub const NONE: &str = "none";

fn re(pattern: &str) -> Regex {
    Regex::new(pattern).unwrap()
}

// Ids a release names outright: Radarr/Sonarr folder tags (`{imdb-tt4357198}`,
// `[imdbid-tt…]`, `{tmdb-12345}`) and a bare IMDb id. Seen in the library as
// `How to Make a Killing (2026) {imdb-tt4357198}`.
static IMDB_TAG: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)(?:imdb(?:id)?[-_ =:]?)?\b(tt\d{7,9})\b"));
static TMDB_TAG: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)[\[{(]tmdb(?:id)?[-_ =:](\d{1,8})[\]})]"));

// Noise the release-name parser does not know, each seen in the library:
// `www.UIndex.org    -    The Matrix 1999 …` (a site ahead of the title),
// `Matrix 4Kremux2160.pctreload1.com.mkv` (a site after it),
// `[Lia] One Piece - 0001-1000 [1080p]` (an episode range),
// `The.Matrix.Remastered.1999 …` (an edition word ahead of the year).
static SITE_PREFIX: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?i)^\s*(?:www\.)?[\w-]+\.(?:org|com|net|to|me|cc|io|ws|xyz|tv|lol|ru|li|se|nl)\s*[-–—_:|]+\s*",
    )
});
static ID_BRACKET: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)[{\[(](?:imdb|tmdb|tvdb)(?:id)?[-_ =:][^}\])]*[}\])]"));
static SITE_ANY: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)\b(?:www\.)?[\w-]+\.(?:org|com|net|to|me|cc|io|ws|xyz|lol|ru)\b"));
// Python's `$` also matches before a final newline; `(\n?)\z` is that, and the
// substitution puts the newline back.
static RANGE: LazyLock<Regex> = LazyLock::new(|| re(r"\s[-–]\s\d{1,4}\s?-\s?\d{1,4}\b.*(\n?)\z"));
// `Avatar - The Last Airbender (2005 - 2008) [1080p]`: the years a series ran.
static YEARS_RAN: LazyLock<Regex> =
    LazyLock::new(|| re(r"[(\[]\s*((?:19|20)\d{2})\s*[-–]\s*(?:(?:19|20)\d{2})?\s*[)\]]"));
static BRACKET_YEAR: LazyLock<Regex> = LazyLock::new(|| re(r"[(\[]((?:19|20)\d{2})[)\]]"));
static EDITION: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?i)\b(?:remastered|extended(?: edition)?|director'?s cut|unrated|theatrical|4kremux\d*|uhd|imax)\b",
    )
});
// A season pack names its season without an episode: `Planet.Earth.II.S01.1080p…`,
// `The Sopranos S01 …`, `Show Season 2 …`, `S01-S03`. Python ends it with
// `(?![\s._-]?E\d)`, which the regex crate lacks. The tail below consumes exactly
// what that lookahead does not match, so the same backtracking path wins and the
// start and groups are Python's; only the end differs, and only the start is used.
static SEASON_PACK: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"\b(?i:S(\d{1,2})(?:\s?-\s?S?\d{1,2})?|Season[\s._-]?(\d{1,2}))\b(?:\z|[^Ee\s._-]|[Ee](?:\z|\D)|[\s._-](?:\z|[^Ee]|[Ee](?:\z|\D)))",
    )
});

/// `metadata.parse_release`'s {title, year, season, episode}, plus `pack`: a season
/// pack, an episode range or the years a series ran.
#[derive(Debug, Clone, PartialEq)]
pub struct Parsed {
    pub title: String,
    pub year: Option<i64>,
    pub season: Option<i64>,
    pub episode: Option<i64>,
    pub pack: bool,
}

/// Python `int()` of a captured digit run.
fn py_int(digits: &str) -> Option<i64> {
    digits.parse().ok()
}

/// `re.sub(r"\s+", " ", text).strip(" -–—")`.
fn squeeze(text: &str) -> String {
    text.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim_matches([' ', '-', '–', '—'])
        .to_string()
}

/// (imdb `tt…`, tmdb digits) written into the release name itself.
pub fn explicit_ids(name: &str) -> (Option<String>, Option<String>) {
    let group = |pattern: &Regex| pattern.captures(name).map(|found| found[1].to_string());
    (group(&IMDB_TAG), group(&TMDB_TAG))
}

/// `metadata.parse_release` after removing the noise it does not know.
pub fn parse_name(name: &str) -> Parsed {
    let stem = SITE_PREFIX.replace(name, "");
    let stem = ID_BRACKET.replace_all(&stem, " ");
    let stem = SITE_ANY.replace_all(&stem, " ");
    let stem = EDITION.replace_all(&stem, " ");
    // `parse_release` drops every bracketed group as release-team noise, and with it
    // the year of `For the Birds (2000).mkv` and `How to Make a Killing (2026) {…}`.
    // The first year a series ran is the one Plex and TMDB know it by: without it,
    // `Avatar - The Last Airbender (2005 - 2008)` matched the 2024 remake.
    let ran = YEARS_RAN.captures(&stem).map(|found| py_int(&found[1]));
    let stem = YEARS_RAN.replace_all(&stem, " ");
    let stem = BRACKET_YEAR.replace_all(&stem, " ${1} ");
    // An episode range is a show's batch, never a film: `One Piece - 0001-1000` was
    // matched to a 2026 movie called One Piece before this.
    let ranged = RANGE.is_match(&stem);
    let stem = RANGE.replace_all(&stem, "${1}");
    let release = parse_release(&stem);
    let mut parsed = Parsed {
        title: release["title"].as_str().unwrap_or_default().to_string(),
        year: release["year"].as_i64(),
        season: release["season"].as_i64(),
        episode: release["episode"].as_i64(),
        pack: ranged || ran.is_some(),
    };
    if parsed.year.unwrap_or(0) == 0 {
        if let Some(Some(year)) = ran {
            parsed.year = Some(year);
        }
    }
    if parsed.season.is_none() && parsed.episode.is_none() {
        let spaced = stem.replace('.', " ");
        let season = SEASON_PACK
            .captures(&parsed.title)
            .or_else(|| SEASON_PACK.captures(&spaced))
            .map(|found| py_int(found.get(1).or(found.get(2)).map_or("", |m| m.as_str())));
        if let Some(season) = season {
            parsed.season = season;
            parsed.pack = true;
            let title = parsed.title.replace('.', " ");
            let head = SEASON_PACK
                .find(&title)
                .map_or(&title[..], |m| &title[..m.start()]);
            parsed.title = head.to_string();
        }
    }
    parsed.title = squeeze(&parsed.title);
    parsed
}

pub fn is_show(parsed: &Parsed) -> bool {
    parsed.episode.is_some() || parsed.season.is_some() || parsed.pack
}

/// TMDB as identify reads it: Python `Tmdb._get(path, **params)`, params in the
/// order given, api key NOT included.
pub trait TmdbSource {
    fn get(&self, path: &str, params: &[(&str, String)]) -> Option<Value>;
}

/// Plex's metadata provider as identify reads it: Python
/// `PlexProvider._call(method, path, body)`.
pub trait PlexSource {
    fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Option<Value>;
}

fn results(found: Option<Value>) -> Vec<Value> {
    found
        .and_then(|found| found.get("results").and_then(Value::as_array).cloned())
        .unwrap_or_default()
}

/// `Tmdb.search`: every candidate for a title. A year narrows films only.
pub fn tmdb_search(t: &dyn TmdbSource, kind: &str, title: &str, year: Option<i64>) -> Vec<Value> {
    if title.is_empty() {
        return Vec::new();
    }
    let mut params = vec![("query", title.to_string())];
    if let Some(year) = year.filter(|year| *year != 0 && kind == "movie") {
        params.push(("year", year.to_string()));
    }
    results(t.get(&format!("search/{kind}"), &params))
}

/// `Tmdb.details`.
pub fn tmdb_details(t: &dyn TmdbSource, kind: &str, id: i64) -> Option<Value> {
    t.get(
        &format!("{kind}/{id}"),
        &[(
            "append_to_response",
            "credits,external_ids,content_ratings,release_dates".to_string(),
        )],
    )
}

/// (kind, tmdb id) of an IMDb id, from TMDB's /find. An episode's or a season's id
/// names its show: the id DMM knows a season pack by is usually its first episode's
/// (271 of the 285 shows fun's rex left unmatched on 2026-10-11, Arrow's tt2340185
/// among them, were `tv_episode_results`).
pub fn tmdb_find_imdb(t: &dyn TmdbSource, imdb: &str) -> (Option<String>, Option<i64>) {
    let found = t.get(
        &format!("find/{imdb}"),
        &[("external_source", "imdb_id".to_string())],
    );
    for (key, kind, id) in [
        ("movie_results", "movie", "id"),
        ("tv_results", "show", "id"),
        ("tv_episode_results", "show", "show_id"),
        ("tv_season_results", "show", "show_id"),
    ] {
        let first = found
            .as_ref()
            .and_then(|found| found.get(key)?.as_array()?.first().cloned());
        if let Some(first) = first {
            return (Some(kind.to_string()), first.get(id).and_then(Value::as_i64));
        }
    }
    (None, None)
}

fn tmdb_imdb(t: &dyn TmdbSource, kind: &str, id: i64) -> Option<String> {
    let details = tmdb_details(t, if kind == "show" { "tv" } else { "movie" }, id)?;
    let external = details
        .pointer("/external_ids/imdb_id")
        .and_then(Value::as_str);
    external
        .filter(|imdb| !imdb.is_empty())
        .or_else(|| details.get("imdb_id").and_then(Value::as_str))
        .map(str::to_string)
}

/// `Tmdb.identify`, but asked in the order the name implies: a season pack has no
/// episode number, and `Tmdb.identify` searched it as a film first --
/// `Planet.Earth.II.S01` found a 2019 documentary film of that name.
fn tmdb_identify(t: &dyn TmdbSource, parsed: &Parsed, kind: &str) -> (Option<String>, Option<i64>) {
    let order = if kind == "show" {
        ["tv", "movie"]
    } else {
        ["movie", "tv"]
    };
    for tmdb_kind in order {
        let found = match parsed.year.filter(|year| *year != 0) {
            // `Tmdb.search` applies a year to films only; a series is found by the
            // year it first aired, and by its title alone when that finds nothing.
            Some(year) if tmdb_kind == "tv" => {
                let params = [
                    ("query", parsed.title.clone()),
                    ("first_air_date_year", year.to_string()),
                ];
                let dated = results(t.get("search/tv", &params));
                if dated.is_empty() {
                    tmdb_search(t, "tv", &parsed.title, None)
                } else {
                    dated
                }
            }
            _ => tmdb_search(t, tmdb_kind, &parsed.title, parsed.year),
        };
        if let Some(first) = found.first() {
            let kind = if tmdb_kind == "tv" { "show" } else { "movie" };
            let id = first
                .get("id")
                .and_then(Value::as_i64)
                .filter(|id| *id != 0);
            return (Some(kind.to_string()), id);
        }
    }
    (None, None)
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

/// (guid, slug) of the provider's item for a movie or show, `tmdb://` ids asked first.
pub fn plex_match(
    p: &dyn PlexSource,
    kind: &str,
    external_ids: &[String],
) -> Option<(String, Option<String>)> {
    let mut ids = external_ids.iter().collect::<Vec<_>>();
    ids.sort_by_key(|id| !id.starts_with("tmdb://"));
    for external in ids {
        let body = json!({"type": kind_type(kind)?, "guid": external});
        if let Some((row, guid)) = matched_row(
            p.call("POST", "/library/metadata/matches", Some(&body)),
            kind,
        ) {
            return Some((
                guid,
                row.get("slug").and_then(Value::as_str).map(str::to_string),
            ));
        }
    }
    None
}

/// What `PlexProvider.match_title` answers.
#[derive(Debug, Clone, PartialEq)]
pub struct PlexAnswer {
    pub guid: String,
    pub kind: String,
    pub title: Option<String>,
    pub year: Option<i64>,
    pub imdb: Option<String>,
    pub tmdb: Option<i64>,
}

/// The provider's own match for a release name, as a real PMS asks for it. plexlab's
/// outbound capture (kids, 1.43.4) holds the body a scan sends, `type`, `title`,
/// `year`, `filename` and the server's own file `hash`, which rex, reading no
/// file itself, cannot compute; without it 184 of 198 labelled movies still matched right.
pub fn plex_match_title(
    p: &dyn PlexSource,
    kind: &str,
    title: &str,
    year: Option<i64>,
    filename: Option<&str>,
) -> Option<PlexAnswer> {
    let mut body = json!({"excludeElements": "Media", "type": kind_type(kind)?, "title": title});
    if let Some(year) = year.filter(|year| *year != 0) {
        body["year"] = json!(year);
    }
    if let Some(filename) = filename.filter(|filename| !filename.is_empty()) {
        body["filename"] = json!(filename);
    }
    let (row, guid) = matched_row(
        p.call("POST", "/library/metadata/matches", Some(&body)),
        kind,
    )?;
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
    Some(PlexAnswer {
        guid,
        kind: kind.to_string(),
        title: row.get("title").and_then(Value::as_str).map(str::to_string),
        year: row.get("year").and_then(Value::as_i64),
        imdb: tail("imdb://"),
        tmdb: tmdb.and_then(|id| py_int(&id)),
    })
}

/// {index: guid} of a show's seasons or a season's episodes.
pub fn plex_children(p: &dyn PlexSource, guid: &str) -> HashMap<String, String> {
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
            let guid = row.get("guid")?.as_str().filter(|guid| !guid.is_empty())?;
            Some((index, guid.to_string()))
        })
        .collect()
}

/// The match TMDB named when Plex named another.
#[derive(Debug, Clone, PartialEq)]
pub struct Disputed {
    pub tmdb: Option<i64>,
    pub imdb: Option<String>,
    pub kind: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Identity {
    pub name: String,
    pub parsed: Parsed,
    pub kind: String,
    pub imdb: Option<String>,
    pub tmdb: Option<i64>,
    pub plex: Option<String>,
    pub confidence: &'static str,
    pub source: Option<String>,
    pub disputed: Option<Disputed>,
}

/// Runs the steps in order for one torrent; either provider may be absent.
pub struct Identifier<'a> {
    pub tmdb: Option<&'a dyn TmdbSource>,
    pub plex: Option<&'a dyn PlexSource>,
    /// Lowercase info hash -> IMDb id, as DMM already knows them.
    pub known: HashMap<String, String>,
}

impl Identifier<'_> {
    /// Plex and TMDB named different shows, and the release's season is one only TMDB's
    /// has. Plex's matcher answers one show for a bare title: for
    /// `The.Tom.and.Jerry.Show.S02E05` (zen, 2026-10-10) the 1975 series, one season
    /// long, where TMDB's first hit is the 2014 one with five; every episode then sat
    /// under a show with no such season, with no episode metadata.
    fn season_only_tmdb_has(
        &self,
        answer: &PlexAnswer,
        tmdb_kind: Option<&str>,
        tmdb_id: i64,
        tmdb_imdb_id: Option<&str>,
        parsed: &Parsed,
    ) -> bool {
        let (Some(t), Some(p), Some(season)) = (self.tmdb, self.plex, parsed.season.filter(|season| *season != 0)) else {
            return false;
        };
        if answer.kind != "show" || tmdb_kind != Some("show") {
            return false;
        }
        if answer.tmdb == Some(tmdb_id) || tmdb_imdb_id.is_some_and(|imdb| !imdb.is_empty() && answer.imdb.as_deref() == Some(imdb)) {
            return false;
        }
        let seasons = tmdb_details(t, "tv", tmdb_id)
            .and_then(|details| details.get("number_of_seasons").and_then(Value::as_i64))
            .unwrap_or(0);
        if season > seasons {
            return false;
        }
        let plex_seasons = plex_children(p, &answer.guid);
        !plex_seasons.is_empty() && !plex_seasons.contains_key(&season.to_string())
    }

    /// `files`: how many video files the torrent holds. Several with no episode
    /// numbers and no single year are a series more often than not, so they are
    /// tried as a show first.
    pub fn identify(&self, name: &str, info_hash: Option<&str>, files: usize) -> Identity {
        self.identify_as(name, info_hash, files, None)
    }

    /// The same, with the movie/show decision taken from the caller (a library
    /// section's type, "movie" or "show") instead of the name heuristic.
    pub fn identify_as(
        &self,
        name: &str,
        info_hash: Option<&str>,
        files: usize,
        kind: Option<&str>,
    ) -> Identity {
        let parsed = parse_name(name);
        let kind = kind.unwrap_or(
            if is_show(&parsed) || (files > 2 && parsed.year.unwrap_or(0) == 0) {
                "show"
            } else {
                "movie"
            },
        );
        let mut identity = Identity {
            name: name.to_string(),
            parsed,
            kind: kind.to_string(),
            imdb: None,
            tmdb: None,
            plex: None,
            confidence: NONE,
            source: None,
            disputed: None,
        };

        let (name_imdb, name_tmdb) = explicit_ids(name);
        let named = name_imdb.is_some() || name_tmdb.is_some();
        let imdb = name_imdb
            .or_else(|| {
                self.known
                    .get(&info_hash.filter(|hash| !hash.is_empty())?.to_lowercase())
                    .cloned()
            })
            .filter(|imdb| !imdb.is_empty());
        if imdb.is_some() || name_tmdb.is_some() {
            identity.confidence = EXACT;
            identity.source = Some(if named { "name id" } else { "dmm hash" }.to_string());
            if let Some(imdb) = imdb {
                if let Some(t) = self.tmdb {
                    if let (Some(found_kind), tmdb_id) = tmdb_find_imdb(t, &imdb) {
                        identity.kind = found_kind;
                        identity.tmdb = tmdb_id;
                    }
                }
                // An episode's id found its show: the show is known by its own id.
                let series = match (self.tmdb, identity.tmdb) {
                    (Some(t), Some(id)) if identity.kind == "show" => tmdb_imdb(t, "show", id),
                    _ => None,
                };
                identity.imdb = Some(series.filter(|series| !series.is_empty()).unwrap_or(imdb));
            } else {
                identity.tmdb = name_tmdb.as_deref().and_then(py_int);
            }
            if let Some(p) = self.plex {
                let mut externals = Vec::new();
                if let Some(tmdb) = identity.tmdb.filter(|tmdb| *tmdb != 0) {
                    externals.push(format!("tmdb://{tmdb}"));
                }
                if let Some(imdb) = identity.imdb.as_deref().filter(|imdb| !imdb.is_empty()) {
                    externals.push(format!("imdb://{imdb}"));
                }
                identity.plex = plex_match(p, &identity.kind, &externals).map(|(guid, _)| guid);
            }
            return identity;
        }

        if identity.parsed.title.is_empty() {
            return identity;
        }
        let parsed = &identity.parsed;
        let mut plex_answer = self.plex.and_then(|p| {
            plex_match_title(p, &identity.kind, &parsed.title, parsed.year, Some(name))
        });
        let (mut tmdb_kind, mut tmdb_id, mut tmdb_imdb_id) = (None, None, None);
        if let Some(t) = self.tmdb {
            (tmdb_kind, tmdb_id) = tmdb_identify(t, parsed, &identity.kind);
            if let (Some(kind), Some(id)) = (&tmdb_kind, tmdb_id) {
                tmdb_imdb_id = tmdb_imdb(t, kind, id);
            }
        }
        let overruled = match (&plex_answer, tmdb_id) {
            (Some(answer), Some(id))
                if self.season_only_tmdb_has(answer, tmdb_kind.as_deref(), id, tmdb_imdb_id.as_deref(), parsed) =>
            {
                plex_answer.take()
            }
            _ => None,
        };

        if let Some(answer) = plex_answer {
            let agree = (tmdb_id.is_some() && answer.tmdb == tmdb_id)
                || (tmdb_imdb_id.as_deref().is_some_and(|imdb| !imdb.is_empty())
                    && answer.imdb == tmdb_imdb_id);
            identity.plex = Some(answer.guid);
            identity.imdb = answer.imdb;
            identity.tmdb = answer.tmdb;
            identity.kind = answer.kind;
            identity.confidence = if agree { AGREED } else { SINGLE };
            identity.source = Some(if agree { "plex+tmdb" } else { "plex" }.to_string());
            if tmdb_id.is_some() && !agree {
                identity.disputed = Some(Disputed {
                    tmdb: tmdb_id,
                    imdb: tmdb_imdb_id,
                    kind: tmdb_kind,
                });
            }
        } else if let (Some(kind), Some(id)) = (tmdb_kind, tmdb_id) {
            identity.kind = kind;
            identity.tmdb = Some(id);
            identity.imdb = tmdb_imdb_id;
            identity.confidence = SINGLE;
            identity.source = Some("tmdb".to_string());
            identity.disputed = overruled.map(|answer| Disputed {
                tmdb: answer.tmdb,
                imdb: answer.imdb,
                kind: Some(answer.kind),
            });
            // Plex did not match the name, but it knows the title by its id: without a
            // real `plex://` guid the Android app's detail page never loads (cycle 70).
            if let Some(p) = self.plex {
                identity.plex =
                    plex_match(p, &identity.kind, &[format!("tmdb://{id}")]).map(|(guid, _)| guid);
            }
        }
        identity
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Each one a release in the library that the old parser got wrong.
    #[test]
    fn identify_parsing() {
        let cases: [(&str, &str, Option<i64>, bool); 10] = [
            (
                "www.UIndex.org    -    The Matrix 1999 1080p Bluray OPUS 7 1 AV1-WhiskeyJack",
                "The Matrix",
                Some(1999),
                false,
            ),
            (
                "The.Matrix.Remastered.1999.2160p.UHD.BluRay.10bit.HDR.x265-HazMatt.mkv",
                "The Matrix",
                Some(1999),
                false,
            ),
            (
                "Matrix 4Kremux2160.pctreload1.com.mkv",
                "Matrix",
                None,
                false,
            ),
            (
                "The.Sopranos.S01.1080p.BluRay.Rus.Eng.HDCLUB",
                "The Sopranos",
                None,
                true,
            ),
            (
                "Planet.Earth.II.S01.1080p.BluRay.HEVC-tlacatlc6",
                "Planet Earth II",
                None,
                true,
            ),
            (
                "[Lia] One Piece - 0001-1000 [1080p]",
                "One Piece",
                None,
                true,
            ),
            (
                "For the Birds (2000).mkv",
                "For the Birds",
                Some(2000),
                false,
            ),
            (
                "How to Make a Killing (2026) {imdb-tt4357198}",
                "How to Make a Killing",
                Some(2026),
                false,
            ),
            (
                "Lanterns.2026.S01E08.1080p.x265-ELiTE.mkv",
                "Lanterns",
                Some(2026),
                true,
            ),
            (
                "Avatar - The Last Airbender (2005 - 2008) [1080p]",
                "Avatar - The Last Airbender",
                Some(2005),
                true,
            ),
        ];
        for (name, title, year, show) in cases {
            let parsed = parse_name(name);
            assert_eq!(
                (parsed.title.as_str(), parsed.year, is_show(&parsed)),
                (title, year, show),
                "{name}"
            );
        }
        assert_eq!(
            explicit_ids("How to Make a Killing (2026) {imdb-tt4357198}"),
            (Some("tt4357198".to_string()), None)
        );
        assert_eq!(
            explicit_ids("Film (2020) {tmdb-12345}"),
            (None, Some("12345".to_string()))
        );
    }

    // Python's `(?![\s._-]?E\d)` refuses `S01` followed by an episode and backtracks;
    // answers taken from `identify.parse_name`.
    #[test]
    fn identify_season_pack_lookahead() {
        let pack = |name: &str| {
            let parsed = parse_name(name);
            (parsed.title, parsed.season, parsed.pack)
        };
        assert_eq!(
            pack("Show S01 E02x"),
            ("Show S01 E02x".to_string(), None, false)
        );
        assert_eq!(pack("Show S01 Extras"), ("Show".to_string(), Some(1), true));
        assert_eq!(
            pack("Show.S01-S03.1080p"),
            ("Show".to_string(), Some(1), true)
        );
        assert_eq!(pack("Show Season.2"), ("Show".to_string(), Some(2), true));
    }

    struct FindTmdb;
    impl TmdbSource for FindTmdb {
        fn get(&self, path: &str, _params: &[(&str, String)]) -> Option<Value> {
            Some(if path == "find/tt0133093" {
                json!({"movie_results": [{"id": 603}], "tv_results": []})
            } else {
                json!({})
            })
        }
    }

    struct GuidPlex;
    impl PlexSource for GuidPlex {
        fn call(&self, _method: &str, _path: &str, body: Option<&Value>) -> Option<Value> {
            Some(if body == Some(&json!({"type": 1, "guid": "tmdb://603"})) {
                json!({"MediaContainer": {"Metadata": [{"guid": "plex://movie/5d776825880197001ec967c8"}]}})
            } else {
                json!({})
            })
        }
    }

    #[test]
    fn identify_exact() {
        let identifier = Identifier {
            tmdb: Some(&FindTmdb),
            plex: Some(&GuidPlex),
            known: HashMap::from([("a".repeat(40), "tt0133093".to_string())]),
        };
        // A hash DMM knows is exact, whatever the name says.
        let by_hash = identifier.identify("Some.Unparseable.Thing.mkv", Some(&"A".repeat(40)), 1);
        assert_eq!(by_hash.confidence, EXACT);
        assert_eq!(by_hash.imdb.as_deref(), Some("tt0133093"));
        assert_eq!(by_hash.tmdb, Some(603));
        assert_eq!(
            by_hash.plex.as_deref(),
            Some("plex://movie/5d776825880197001ec967c8")
        );
        assert_eq!(by_hash.source.as_deref(), Some("dmm hash"));
        // An id in the name is exact.
        let tagged = identifier.identify("Matrix {imdb-tt0133093}", None, 1);
        assert_eq!(
            (tagged.confidence, tagged.source.as_deref()),
            (EXACT, Some("name id"))
        );
        // With no source, nothing is guessed.
        let bare = Identifier {
            tmdb: None,
            plex: None,
            known: HashMap::new(),
        };
        let nothing = bare.identify("Unknown.Release.2020.mkv", None, 1);
        assert_eq!((nothing.confidence, nothing.imdb), (NONE, None));
    }
}
