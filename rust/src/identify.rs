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
static RANGE: LazyLock<Regex> = LazyLock::new(|| re(r"\s[-–]\s(\d{1,4})\s?-\s?(\d{1,4})\b.*(\n?)\z"));
// A site is the last word of its run: `Modern.Family.S03E14.Me.Jealous` and
// `Crazy.Ex-Girlfriend.S03E02.To.Josh` are no sites, and reading them as `E14.Me` and
// `E02.To` took the episode number with them (the production server's feed, 2026-10-13).
// Python's lookahead `\.(?!(?:mkv|…)\b)\w` is the two patterns below.
static SITE_RUN_ON: LazyLock<Regex> = LazyLock::new(|| re(r"^\.\w"));
static SITE_EXTENSION: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)^\.(?:mkv|mp4|webm|avi|ts|m4v)\b"));
// `Avatar - The Last Airbender (2005 - 2008) [1080p]`: the years a series ran.
static YEARS_RAN: LazyLock<Regex> =
    LazyLock::new(|| re(r"[(\[]\s*((?:19|20)\d{2})\s*[-–]\s*(?:(?:19|20)\d{2})?\s*[)\]]"));
static BRACKET_YEAR: LazyLock<Regex> = LazyLock::new(|| re(r"[(\[]((?:19|20)\d{2})[)\]]"));
static EDITION: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"(?i)\b(?:remastered|extended(?: edition)?|director'?s cut|unrated|theatrical|4kremux\d*|uhd|imax)\b",
    )
});
// An episode named without `SxxEyy`: a bare number (`Mysterious Love E06 1080p`), the NxNN
// form (`Some Day or One Day - 1x02`), or only inside the brackets `parse_release` drops (a
// disc rip's `00000 (E01-The.Arrangement).m2ts`, Suits.S03 on the production server). zurg
// files these as shows since 2026-10-11, and with no number here the feed placed none of
// them. The production server's Usenet library (2026-10-12) adds four digits and an `EP` or
// `Ep.` (`Detektiv.Conan.E0221`, `One.Piece.EP1161`, `Gintama.Ep.14`), each filed under movies.
// The production server's feed (2026-10-13) adds three digits and a year for the NxNN season
// (`Animaniacs - 1x123`, `MythBusters - 2004x09`; never a bare 0, which is the hex of a group
// tag such as `[0x539] Seishun Buta Yarou …`), `Episode` spelled out
// (`Digimon Adventure Zero Two Episode 12 - …`) and a version alone (`[CyC] Mushishi 01v2`).
static LOOSE_NX: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)(?:^|[\s._\-\[(])(0[1-9]|[1-9]\d?|(?:19|20)\d{2})x(\d{2,3})(?:[\s._\-\])]|$)"));
static LOOSE_E: LazyLock<Regex> = LazyLock::new(|| {
    re(r"(?i)(?:^|[\s._\-\[(])(?:E(?:P[.\s]?)?(\d{2,4})|Episode[\s._]?(\d{1,4})|(\d{2,4})v\d)(?:[\s._\-\])]|$)")
});
// An absolute episode glued to its title by a dash (`[Moozzi2] Eighty-Six-15 [BD …]`,
// `[HorribleSubs].Gintama-340.[1080p]`, the production server's Usenet movies): two or three
// digits and no year, so `Blade-Runner-2049` stays a film; a version, a last episode's `END`
// and a reposter's tag may follow (`Shichi-12 END`, `Gintama-364.[1080p]-RakuvFIN`). Spaced,
// it needs the version, since `parse_release` already reads ` - 12`: `Uso - 01v2 - Monotone`.
static DASH_EPISODE: LazyLock<Regex> = LazyLock::new(|| {
    re(r"^(.*\S)(?:-(\d{2,3})(?:v\d)?(?:\s+END)?(?:\s+-\S+)?|\s+-\s+(\d{2,3})v\d(?:\s+-\s.*)?)$")
});
// `[philosophy-raws][Samurai Champloo][13][BDRIP]…`: every part bracketed, which
// `parse_release` drops whole, the title second and the episode third.
static BRACKETED: LazyLock<Regex> =
    LazyLock::new(|| re(r"^\s*\[[^\]]*\]\s*\[([^\]]+)\]\s*\[(\d{1,4})(?:v\d)?\]"));
// `Der.mude.Tod.AKA.Destiny.1921`, `El.laberinto.del.fauno.aka.Pans.Labyrinth`: a film named
// twice, its original name first. TMDB finds all nine such names on the production server by
// that first name, and misses `La Haine AKA Hate` by the second.
static AKA: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)\s+aka\s+"));
// `[Erai-raws].Kingdom.4th.Season-21.[1080p]`, `Gintama. Shirogane no Tamashii-hen 2nd Season - 14 END`:
// a season written as an ordinal, the episode after it. Read as a pack, Kingdom's 21st
// episode was a season 21 film on the production server (2026-10-13).
static ORDINAL_SEASON: LazyLock<Regex> = LazyLock::new(|| re(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)[\s._-]+Season\b"));
static ORDINAL_SEASON_EP: LazyLock<Regex> =
    LazyLock::new(|| re(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)[\s._-]+Season[\s._]*-[\s._]*(\d{1,4})(?:v\d)?\b"));
// `[Sakurato] Bleach： Sennen Kessen Hen - Kashin Tan [07][AVC-8bit 1080P AAC][CHS]`: the episode
// bracketed right after the title, which `parse_release` drops with the other brackets.
// A resolution is never one (`[720]`).
static BRACKET_EPISODE: LazyLock<Regex> =
    LazyLock::new(|| re(r"^\s*(?:\[[^\]]*\][\s.]*)?[^\[\]]*[^\[\]\s.]\s*\.?\[(\d{2,3})(?:v\d)?\]"));
// A season pack names its season without an episode: `Planet.Earth.II.S01.1080p…`,
// `The Sopranos S01 …`, `Show Season 2 …`, `S01-S03`, and on the production server `Family.Matters.seasons.01-09`
// and `South Park - Seasons 1 to 25`. Python ends it with
// `(?![\s._-]?E\d)`, which the regex crate lacks. The tail below consumes exactly
// what that lookahead does not match, so the same backtracking path wins and the
// start and groups are Python's; only the end differs, and only the start is used.
static SEASON_PACK: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"\b(?i:S(\d{1,2})(?:\s?-\s?S?\d{1,2})?|Seasons?[\s._-]?(\d{1,2}))\b(?:\z|[^Ee\s._-]|[Ee](?:\z|\D)|[\s._-](?:\z|[^Ee]|[Ee](?:\z|\D)))",
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
    let stem = SITE_ANY.replace_all(&stem, |found: &regex::Captures| {
        let rest = &stem[found.get(0).map_or(0, |m| m.end())..];
        if SITE_RUN_ON.is_match(rest) && !SITE_EXTENSION.is_match(rest) {
            found[0].to_string()
        } else {
            " ".to_string()
        }
    });
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
    // Only a rising one: `One Piece - 0160 - 10 Survival Rate!` is episode 160 and its title.
    let run = RANGE.captures(&stem).filter(|run| py_int(&run[2]) > py_int(&run[1]));
    let ranged = run.is_some();
    let stem = match run {
        Some(run) => {
            let whole = run.get(0).expect("group 0");
            format!("{}{}{}", &stem[..whole.start()], &run[3], &stem[whole.end()..])
        }
        None => stem.to_string(),
    };
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
        // A year is a season with two digits after it (`2004x09`); `1920x816` is a resolution.
        let loose = LOOSE_NX.captures(&stem)
            .filter(|found| py_int(&found[1]).is_none_or(|season| season < 1900) || found[2].len() == 2)
            .map(|found| (&*LOOSE_NX, py_int(&found[1]), py_int(&found[2])))
            .or_else(|| LOOSE_E.captures(&stem).map(|found| {
                let number = found.get(1).or(found.get(2)).or(found.get(3)).map_or("", |m| m.as_str());
                (&*LOOSE_E, None, py_int(number))
            }));
        if let Some((pattern, season, episode)) = loose {
            parsed.season = season;
            parsed.episode = episode;
            if let Some(cut) = pattern.find(&parsed.title) {
                parsed.title = parsed.title[..cut.start()].to_string();
            }
        }
    }
    // `[SubsPlease] Tower of God S2 - 07v2 (1080p)`: an absolute number after the season it is
    // in. Read as season 1 it was placed on that season's episode 7 as another version of it.
    // `Shiguang Dailiren Season 3 - 01` and `… 2nd Season - 14 END` the same way.
    if parsed.season.is_none() && parsed.episode.is_some() {
        let pack = SEASON_PACK.captures(&parsed.title).and_then(|found| {
            let season = found.get(1).or(found.get(2))?;
            Some((py_int(season.as_str()), found.get(0).expect("group 0").start()))
        });
        let ordinal = || {
            ORDINAL_SEASON
                .captures(&parsed.title)
                .map(|found| (py_int(&found[1]), found.get(0).expect("group 0").start()))
        };
        if let Some((season, start)) = pack.or_else(ordinal) {
            parsed.season = season;
            parsed.title = parsed.title[..start].to_string();
        }
    }
    if parsed.season.is_none() && parsed.episode.is_none() {
        if let Some(found) = ORDINAL_SEASON_EP.captures(&stem) {
            parsed.season = py_int(&found[1]);
            parsed.episode = py_int(&found[2]);
            if let Some(cut) = ORDINAL_SEASON.find(&parsed.title) {
                parsed.title = parsed.title[..cut.start()].to_string();
            }
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
    parsed.title = AKA.splitn(&parsed.title, 2).next().unwrap_or_default().to_string();
    if parsed.season.is_none() && parsed.episode.is_none() && parsed.year.unwrap_or(0) == 0 && !parsed.pack {
        if let Some(dashed) = DASH_EPISODE.captures(&parsed.title) {
            parsed.episode = dashed.get(2).or(dashed.get(3)).and_then(|number| py_int(number.as_str()));
            parsed.title = dashed[1].trim_matches([' ', '-', '–', '—']).to_string();
        }
    }
    if let Some(numbered) = BRACKET_EPISODE.captures(name) {
        let number = py_int(&numbered[1]);
        if parsed.season.is_none() && parsed.episode.is_none() && parsed.year.unwrap_or(0) == 0 && !parsed.pack
            && !matches!(number, Some(480 | 576 | 720))
        {
            parsed.episode = number;
        }
    }
    if let Some(bracketed) = BRACKETED.captures(name) {
        if parsed.title.is_empty() && parsed.season.is_none() && parsed.episode.is_none() {
            parsed.title = squeeze(&bracketed[1]);
            parsed.episode = py_int(&bracketed[2]);
        }
    }
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
/// (271 of the 285 shows the production rex left unmatched on 2026-10-11, Arrow's
/// tt2340185 among them, were `tv_episode_results`).
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
/// `Planet.Earth.II.S01` found a 2019 documentary film of that name. A kind the caller
/// `fixed` (its library's) is the only one asked: the other places nothing there.
fn tmdb_identify(t: &dyn TmdbSource, parsed: &Parsed, kind: &str, fixed: bool) -> (Option<String>, Option<i64>) {
    let order = if kind == "show" {
        ["tv", "movie"]
    } else {
        ["movie", "tv"]
    };
    for tmdb_kind in &order[..if fixed { 1 } else { 2 }] {
        let tmdb_kind = *tmdb_kind;
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

/// What `PlexProvider.answer` reads off a matched row. The provider's own artwork rides
/// along: a title only Plex knows has no TMDB poster (the production server, 2026-10-12: the Monogatari
/// seasons Plex keeps as shows of their own drew the blank tile).
#[derive(Debug, Clone, PartialEq)]
pub struct PlexAnswer {
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
            guid,
            kind: kind.to_string(),
            title: row.get("title").and_then(Value::as_str).map(str::to_string),
            year: row.get("year").and_then(Value::as_i64),
            imdb: tail("imdb://"),
            tmdb: tmdb.and_then(|id| py_int(&id)),
            thumb: text("thumb"),
            art: text("art"),
        }
    }
}

/// The provider's own match for a release name, as a real PMS asks for it. plexlab's
/// outbound capture (plexlab, 1.43.4) holds the body a scan sends, `type`, `title`,
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
    Some(PlexAnswer::of(&row, guid, kind))
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
    /// The provider's artwork for `plex`, for a title TMDB gives none.
    pub thumb: Option<String>,
    pub art: Option<String>,
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
    /// `The.Tom.and.Jerry.Show.S02E05` (the test host, 2026-10-10) the 1975 series, one
    /// season long, where TMDB's first hit is the 2014 one with five; every episode then
    /// sat under a show with no such season, with no episode metadata.
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
    /// section's type, "movie" or "show") instead of the name heuristic: an answer of the
    /// other kind places nothing there, so the name is asked as that kind.
    pub fn identify_as(
        &self,
        name: &str,
        info_hash: Option<&str>,
        files: usize,
        kind: Option<&str>,
    ) -> Identity {
        let parsed = parse_name(name);
        let fixed = kind;
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
            thumb: None,
            art: None,
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
        if imdb.is_none() && name_tmdb.is_none() {
            return self.by_name(identity, fixed.is_some());
        }
        let blank = identity.clone();
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
        } else if let Some(id) = name_tmdb.as_deref().and_then(py_int) {
            // zurg hands over the TMDB id of whatever its own Plex matched, an episode's
            // among them: the production server's Pawn Stars came as 1559521, which no show has (2026-10-12).
            let found = |t: &dyn TmdbSource| {
                tmdb_details(t, if kind == "show" { "tv" } else { "movie" }, id)
                    .is_some_and(|details| details.as_object().is_some_and(|details| !details.is_empty()))
            };
            if self.tmdb.is_none_or(found) {
                identity.tmdb = Some(id);
            }
        }
        if let Some(p) = self.plex {
            let mut externals = Vec::new();
            if let Some(tmdb) = identity.tmdb.filter(|tmdb| *tmdb != 0) {
                externals.push(format!("tmdb://{tmdb}"));
            }
            if let Some(imdb) = identity.imdb.as_deref().filter(|imdb| !imdb.is_empty()) {
                externals.push(format!("imdb://{imdb}"));
            }
            if let Some(answer) = plex_match(p, &identity.kind, &externals) {
                identity.plex = Some(answer.guid);
                (identity.thumb, identity.art) = (answer.thumb, answer.art);
                // TMDB's /find does not know every IMDb id Plex does: DMM gives Knock at the
                // Cabin as tt15679400, which Plex lists beside TMDB's 631842 (the production server, 2026-10-12).
                // Plex's own TMDB ids are not all TMDB's: Digimon Adventure 02's 229048 is none.
                let details = match (self.tmdb, answer.tmdb) {
                    (Some(t), Some(id)) if identity.tmdb.is_none() => {
                        tmdb_details(t, if identity.kind == "show" { "tv" } else { "movie" }, id)
                            .filter(|details| details.as_object().is_some_and(|details| !details.is_empty()))
                    }
                    _ => None,
                };
                if let Some(details) = details {
                    identity.tmdb = answer.tmdb;
                    let own = details
                        .pointer("/external_ids/imdb_id")
                        .and_then(Value::as_str)
                        .filter(|imdb| !imdb.is_empty())
                        .or_else(|| details.get("imdb_id").and_then(Value::as_str).filter(|imdb| !imdb.is_empty()));
                    if let Some(own) = own {
                        identity.imdb = Some(own.to_string());
                    }
                }
            }
        }
        if identity.tmdb.is_some() && fixed.is_none_or(|fixed| identity.kind == fixed) {
            return identity;
        }
        // The id names no title TMDB has, or one of the other kind than the library's: on
        // the production server (2026-10-12) DMM's episode ids for Boy Meets World and Courage the Cowardly Dog,
        // Bill Nye's id for a one-episode film, and zurg's ids for episodes left 45 shows and
        // 12 films on the blank tile. The name decides then, as it does with no id at all:
        // an id TMDB does not know is as likely DMM's mistake as Plex's -- its tt6257382 for
        // A Serbian Film is Plex's "Film Postcards: Serbia".
        let by_name = self.by_name(blank, fixed.is_some());
        if by_name.tmdb.is_some() || (by_name.plex.is_some() && identity.tmdb.is_none()) {
            by_name
        } else {
            identity
        }
    }

    /// The cleaned name to Plex's matcher and TMDB, their agreement the confidence.
    fn by_name(&self, mut identity: Identity, fixed: bool) -> Identity {
        if identity.parsed.title.is_empty() {
            return identity;
        }
        let name = identity.name.clone();
        let parsed = &identity.parsed;
        let mut plex_answer = self.plex.and_then(|p| {
            plex_match_title(p, &identity.kind, &parsed.title, parsed.year, Some(&name))
        });
        let (mut tmdb_kind, mut tmdb_id, mut tmdb_imdb_id) = (None, None, None);
        if let Some(t) = self.tmdb {
            (tmdb_kind, tmdb_id) = tmdb_identify(t, parsed, &identity.kind, fixed);
            if let (Some(kind), Some(id)) = (&tmdb_kind, tmdb_id) {
                tmdb_imdb_id = tmdb_imdb(t, kind, id);
            }
        }
        // A film Plex knows by no TMDB id is a record of its own, and the one DMM's map was
        // fed (the production server, 2026-10-12): Plex answered `A.Serbian.Film.2010` with "Film Postcards:
        // Serbia" and `Mar.Adentro.2004` with a yearless stub, where TMDB's search was right.
        let stub_film = |answer: &PlexAnswer| {
            answer.kind == "movie" && answer.tmdb.is_none() && tmdb_kind.as_deref() == Some("movie")
        };
        let overruled = match (&plex_answer, tmdb_id) {
            (Some(answer), Some(id))
                if stub_film(answer)
                    || self.season_only_tmdb_has(answer, tmdb_kind.as_deref(), id, tmdb_imdb_id.as_deref(), parsed) =>
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
            (identity.thumb, identity.art) = (answer.thumb, answer.art);
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
            if let Some(answer) = self.plex.and_then(|p| plex_match(p, &identity.kind, &[format!("tmdb://{id}")])) {
                identity.plex = Some(answer.guid);
                (identity.thumb, identity.art) = (answer.thumb, answer.art);
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
    /// Names zurg filed under movies on the test host and in production until 2026-10-11,
    /// each a series.
    #[test]
    fn identify_loose_episode_numbers() {
        for (name, title, season, episode) in [
            ("Mysterious Love E06 1080p WEB.DL AAC H.264-Luvmichelle.mp4", "Mysterious Love", None, Some(6)),
            ("Some Day or One Day - 1x02 (1080p VIKI WEB.DL Marshall).mkv", "Some Day or One Day", Some(1), Some(2)),
            ("00000 (E01-The.Arrangement).m2ts", "00000 m2ts", None, Some(1)),
            ("Suits.S03.1080p.BDRemux.2xRus.Eng.HDCLUB", "Suits", Some(3), None),
            ("Movie.2020.1080p.DDP5.1.x264-GRP.mkv", "Movie", None, None),
            // Filed under movies by the production server's Usenet zurg (2026-10-12), each an episode.
            ("Detektiv.Conan.E0221.Nichts.als.Luegen.2.German.1996.ANiME.FS.DL.1080p.BluRay.x264-STARS", "Detektiv Conan", None, Some(221)),
            ("One.Piece.EP1161.A.Dangerous.Deal.Loki.of.the.Underworld.and.Luffy.2160p.BILI.WEB-DL", "One Piece", None, Some(1161)),
            ("Gintama.Ep.14.German.DTS.5.1.DL.1080p.BluRay.x264-AST4u", "Gintama", None, Some(14)),
            ("[Moozzi2] Eighty-Six-15 [BD 3840x2160 x265-10Bit FLACx2]", "Eighty-Six", None, Some(15)),
            ("[HorribleSubs].Gintama-340.[1080p]", "Gintama", None, Some(340)),
            ("[philosophy-raws][Samurai Champloo][13][BDRIP][Hi10P FLAC][1920X1080]", "Samurai Champloo", None, Some(13)),
            ("[Moozzi2] Natsume Yuujinchou Shichi-12 END [BD 1920x1080 x265-10Bit Flac]", "Natsume Yuujinchou Shichi", None, Some(12)),
            ("[HorribleSubs].Gintama-364.[1080p]-RakuvFIN", "Gintama", None, Some(364)),
            ("[RH] Shigatsu wa Kimi no Uso (Your Lie in April) - 01v2 - Monotone-Colorful [Dual Audio] [BDRip]", "Shigatsu wa Kimi no Uso", None, Some(1)),
            ("Family.Matters.seasons.01-09.AMZN.1080p.10bit.hevc.x265-bearfish", "Family Matters", Some(1), None),
            ("South Park - Seasons 1 to 25 (S01-S25) Master Collection, The Movie & Extras [1080p-2160p]", "South Park", Some(1), None),
            // What stays a film: a year beside the dash, one digit after it, an `AKA`'s first name.
            ("Blade-Runner-2049", "Blade-Runner", None, None),
            ("Spider-Man-2", "Spider-Man-2", None, None),
            ("A Trip to the Moon (1902) Arrow (1080p BluRay x265 HEVC 10bit AAC 2.0 afm72)", "A Trip to the Moon", None, None),
            ("Der.mude.Tod.AKA.Destiny.1921.576p.BluRay.AAC.x264-HANDJOB", "Der mude Tod", None, None),
            ("Mr.Nice.Guy.aka.Yat.goh.ho.yan.1997.Extended.Original.Cut.2160p.UHD.Blu-ray", "Mr Nice Guy", None, None),
        ] {
            let parsed = parse_name(name);
            assert_eq!((parsed.title.as_str(), parsed.season, parsed.episode), (title, season, episode), "{name}");
        }
    }

    #[test]
    fn identify_season_pack_lookahead() {
        let pack = |name: &str| {
            let parsed = parse_name(name);
            (parsed.title, parsed.season, parsed.pack)
        };
        // Five digits are no episode (`SxxEyy` reads four at most), and still refuse the pack.
        assert_eq!(
            pack("Show S01 E02345"),
            ("Show S01 E02345".to_string(), None, false)
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
