//! Which TMDB season and episode an anime release is. Fansubs name a cour the way AniDB
//! does -- `Hibike Euphonium 3 - 01`, `Uma Musume - Cinderella Gray Part 2 - 05`, `Bleach -
//! Sennen Kessen Hen - Kashin Tan - 01` -- while TMDB files Hibike 3 as Sound! Euphonium's
//! third season, Cinderella Gray's second cour as episodes 14-23 of its one season and Kashin
//! Tan as Bleach's season 2 from episode 41. Named by title, 12 shows on the production server
//! stayed unidentified and about 50 seasons sat on `local://` (2026-10-13).
//!
//! Two community datasets answer it: anime-lists (`anime-list-master.xml`, an AniDB id to
//! TMDB's show, season and episode offset) and manami's anime-offline-database (titles and
//! synonyms to AniDB ids). `REX_ANIME_LISTS=on` keeps both beside the catalog, fetched when
//! missing or a week old; without it nothing here is used. Only an anime-shaped release (a
//! fansub group or CRC in brackets) is looked up: `Kingdom.S01E01.NF` is the Korean series,
//! not the anime of that name.

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, LazyLock, RwLock};
use std::time::{Duration, SystemTime};

use regex::Regex;
use serde_json::Value;

use crate::identify;
use crate::runtime::{info, warning};

const LIST_URL: &str = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml";
const TITLES_URL: &str =
    "https://github.com/manami-project/anime-offline-database/releases/latest/download/anime-offline-database.jsonl";
const LIST_FILE: &str = "anime-list-master.xml";
const TITLES_FILE: &str = "anime-offline-database.jsonl";
const FRESH: Duration = Duration::from_secs(7 * 24 * 3600);

/// One AniDB entry as TMDB numbers it.
#[derive(Debug, Clone, PartialEq)]
pub struct Target {
    pub anidb: u32,
    pub tmdb: i64,
    pub season: i64,
    pub offset: i64,
    pub tvdb_season: Option<i64>,
    pub tvdb_offset: i64,
}

#[derive(Default)]
pub struct Index {
    titles: HashMap<String, Vec<u32>>,
    entries: HashMap<u32, Target>,
}

static INDEX: RwLock<Option<Arc<Index>>> = RwLock::new(None);

static ENTRY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"<anime ([^>]*)>\s*<name>([^<]*)</name>").unwrap());
static ATTR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r#"(\w+)="([^"]*)""#).unwrap());
// `[Erai-raws] …`, `[SubsPlease] …`, `… [8FAE9300].mkv`: what fansubs name and scene releases do not.
static SHAPED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^\s*\[[^\]]+\]|[\[(][0-9A-F]{8}[\])]").unwrap());

/// Lowercase letters and digits, words split on everything else: `Hibike! Euphonium 3` and
/// `Hibike Euphonium 3` are one title.
pub fn normalize(title: &str) -> String {
    title
        .to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

fn unescape(text: &str) -> String {
    text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&apos;", "'")
}

impl Index {
    /// The two datasets as they are published: the XML list and the JSON-lines database.
    pub fn parse(list: &str, titles: &str) -> Index {
        let mut index = Index::default();
        let mut names: Vec<(u32, String)> = Vec::new();
        for found in ENTRY.captures_iter(list) {
            let attrs: HashMap<&str, &str> =
                ATTR.captures_iter(&found[1]).map(|a| (a.get(1).unwrap().as_str(), a.get(2).unwrap().as_str())).collect();
            let number = |name: &str| attrs.get(name).and_then(|value| value.parse::<i64>().ok());
            let (Some(anidb), Some(tmdb), Some(season)) =
                (attrs.get("anidbid").and_then(|id| id.parse::<u32>().ok()), number("tmdbtv"), number("tmdbseason"))
            else {
                continue;
            };
            index.entries.insert(anidb, Target {
                anidb,
                tmdb,
                season,
                offset: number("tmdboffset").unwrap_or(0),
                tvdb_season: number("defaulttvdbseason"),
                tvdb_offset: number("episodeoffset").unwrap_or(0),
            });
            names.push((anidb, unescape(&found[2])));
        }
        for (anidb, name) in names {
            index.add(&name, anidb);
        }
        for line in titles.lines() {
            let Ok(row) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let ids: Vec<u32> = row
                .get("sources")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .filter(|source| source.contains("anidb.net/anime/"))
                .filter_map(|source| source.rsplit('/').next()?.parse().ok())
                .filter(|id| index.entries.contains_key(id))
                .collect();
            if ids.is_empty() {
                continue;
            }
            let title = row.get("title").and_then(Value::as_str).into_iter();
            let synonyms = row.get("synonyms").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_str);
            for name in title.chain(synonyms) {
                for id in &ids {
                    index.add(name, *id);
                }
            }
        }
        for ids in index.titles.values_mut() {
            ids.sort_unstable();
            ids.dedup();
        }
        index
    }

    fn add(&mut self, name: &str, anidb: u32) {
        let key = normalize(name);
        if !key.is_empty() {
            self.titles.entry(key).or_default().push(anidb);
        }
    }

    /// The entry a title names, the lowest AniDB id when several share it (a show's first
    /// cour before its later ones).
    fn find(&self, title: &str) -> Option<&Target> {
        self.titles.get(&normalize(title))?.iter().find_map(|id| self.entries.get(id))
    }

    /// The entry a release is, and whether its numbers are this entry's: a bare title with
    /// a later season (`Oshi no Ko S2 - 05`) that only the first season's entry answers names
    /// the show, not the season.
    pub fn lookup(&self, name: &str) -> Option<(Target, bool)> {
        let parsed = identify::parse_name(name);
        if parsed.title.is_empty() {
            return None;
        }
        // AniDB names a sequel by its year: `Mairimashita! Iruma-kun (2026)`.
        if let Some(year) = parsed.year {
            if let Some(target) = self.find(&format!("{} ({year})", parsed.title)) {
                return Some((target.clone(), true));
            }
        }
        if let Some(season) = parsed.season.filter(|season| *season > 1) {
            let ordinal = match season % 10 {
                1 if season % 100 != 11 => "st",
                2 if season % 100 != 12 => "nd",
                3 if season % 100 != 13 => "rd",
                _ => "th",
            };
            for candidate in [
                format!("{} {season}{ordinal} Season", parsed.title),
                format!("{} Season {season}", parsed.title),
                format!("{} {season}", parsed.title),
            ] {
                if let Some(target) = self.find(&candidate) {
                    return Some((target.clone(), true));
                }
            }
            let target = self.find(&parsed.title)?;
            // TVDB's numbering of the show (`Bleach-Thousand-Year.Blood.War-S17E23`) is the entry's.
            let numbered = target.tvdb_season == Some(season);
            return Some((target.clone(), numbered));
        }
        self.find(&parsed.title).map(|target| (target.clone(), true)).or_else(|| {
            // `Bleach 2 Sennen Kessen-hen - 46`: a series number between the title's words, which
            // AniDB's names leave out (`Bleach: Sennen Kessen Hen`). The production server held it alone on
            // `local://` (2026-10-13).
            let words: Vec<&str> = parsed.title.split_whitespace().collect();
            let lone = words.iter().enumerate().position(|(at, word)| {
                at > 0 && at + 1 < words.len() && word.len() <= 2 && word.bytes().all(|b| b.is_ascii_digit())
            })?;
            let without: Vec<&str> = words.iter().enumerate().filter(|(at, _)| *at != lone).map(|(_, word)| *word).collect();
            self.find(&without.join(" ")).map(|target| (target.clone(), true))
        })
    }
}

/// TMDB's season and episode for a release's `episode`, numbered within its cour (`Kashin
/// Tan - 01` is Bleach S2E41), already counted from the show's first cour (`Bleach 2 Sennen
/// Kessen-hen - 46`), or in TVDB's season (`S17E23`).
/// ponytail: a number past the offset is read as already absolute only when the offset is
/// 10 or more, a whole cour; an entry that starts two episodes in is always counted from its
/// own first. A cour longer than its offset would need AniDB's episode counts.
pub fn numbers(target: &Target, season: Option<i64>, episode: i64) -> (i64, i64) {
    let episode = if season.is_some_and(|season| Some(season) == target.tvdb_season && season > 1) {
        episode - target.tvdb_offset + target.offset
    } else if target.offset >= 10 && episode > target.offset {
        episode
    } else {
        episode + target.offset
    };
    (target.season, episode.max(1))
}

pub fn shaped(name: &str) -> bool {
    SHAPED.is_match(name)
}

pub fn current() -> Option<Arc<Index>> {
    INDEX.read().ok()?.clone()
}

pub fn install(index: Index) {
    if let Ok(mut slot) = INDEX.write() {
        *slot = Some(Arc::new(index));
    }
}

/// The entry an anime-shaped release (or its file) is, with whether its numbers are the entry's.
pub fn lookup(release: &str, file: &str) -> Option<(Target, bool)> {
    let index = current()?;
    if !shaped(release) && !shaped(file) {
        return None;
    }
    index.lookup(release).or_else(|| index.lookup(file))
}

/// TMDB's numbers for an anime release's parsed ones; `None` when it is no mapped anime.
pub fn renumber(release: &str, file: &str, season: Option<i64>, episode: i64) -> Option<(i64, i64)> {
    let (target, numbered) = lookup(release, file)?;
    numbered.then(|| numbers(&target, season, episode))
}

/// Load the datasets kept beside the catalog, fetching what is missing or a week old.
/// Answers whether an index is installed.
pub fn load(dir: &Path) -> bool {
    if std::env::var("REX_ANIME_LISTS").map(|value| value != "on").unwrap_or(true) {
        return false;
    }
    let mut texts = Vec::new();
    for (file, url) in [(LIST_FILE, LIST_URL), (TITLES_FILE, TITLES_URL)] {
        let path = dir.join(file);
        let stale = std::fs::metadata(&path)
            .and_then(|meta| meta.modified())
            .map(|modified| SystemTime::now().duration_since(modified).unwrap_or_default() > FRESH)
            .unwrap_or(true);
        if stale {
            match fetch(url) {
                Ok(body) => {
                    let partial = path.with_extension("part");
                    if std::fs::write(&partial, &body).and_then(|_| std::fs::rename(&partial, &path)).is_err() {
                        warning!("ANIME could not keep {}", path.display());
                    }
                }
                Err(error) => warning!("ANIME fetch {url}: {error}"),
            }
        }
        match std::fs::read_to_string(&path) {
            Ok(text) => texts.push(text),
            Err(error) => {
                warning!("ANIME {} unreadable: {error}; anime releases are named by title", path.display());
                return false;
            }
        }
    }
    let index = Index::parse(&texts[0], &texts[1]);
    info!("ANIME {} entries, {} titles", index.entries.len(), index.titles.len());
    install(index);
    true
}

fn fetch(url: &str) -> Result<Vec<u8>, String> {
    let mut response = super::upstream::open("GET", url, &[("User-Agent", "rex".to_string())], b"", Duration::from_secs(120))?;
    if response.status != 200 {
        return Err(format!("HTTP {}", response.status));
    }
    response.read_body(256 * 1024 * 1024)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Real entries of both datasets (`tests/fixtures/anime/`), cut to the titles the
    /// production server held.
    fn index() -> Index {
        Index::parse(
            include_str!("../../../tests/fixtures/anime/anime-list-master.xml"),
            include_str!("../../../tests/fixtures/anime/anime-offline-database.jsonl"),
        )
    }

    #[test]
    fn anime_releases_take_tmdbs_season_and_episode() {
        let index = index();
        let place = |name: &str| {
            let parsed = identify::parse_name(name);
            let (target, numbered) = index.lookup(name)?;
            let episode = parsed.episode?;
            Some((target.tmdb, numbered.then(|| numbers(&target, parsed.season, episode))))
        };
        let record: Value =
            serde_json::from_str(include_str!("../../../tests/fixtures/anime/releases.json")).unwrap();
        let mut wrong = Vec::new();
        for case in record["releases"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let want = case["tmdb"].as_i64().map(|tmdb| {
                (tmdb, case["season"].as_i64().map(|season| (season, case["episode"].as_i64().unwrap())))
            });
            let got = place(name);
            if got != want {
                wrong.push(format!("{name}: got {got:?}, want {want:?}"));
            }
        }
        assert!(wrong.is_empty(), "{wrong:#?}");
    }

    #[test]
    fn only_fansub_shaped_names_are_anime() {
        assert!(shaped("[Erai-raws] Kingdom 4th Season - 21 [1080p]"));
        assert!(shaped("Kingdom - 21 [ABCD1234].mkv"));
        assert!(!shaped("Kingdom.S01E01.1080p.NF.WEB-DL.DDP5.1.x264"));
    }
}
