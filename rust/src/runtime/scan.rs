//! The live library: rows a source's items become, on keys that never change.
//!
//! The catalog file used to be the whole library, loaded once at start. A library a source
//! feeds (`feed.rs`) changes while the server runs, and this module is what a real PMS's
//! scanner does with what it finds: each new file becomes a row (identified, its Media
//! from the source's ffprobe), an episode is placed under its show and season, a row
//! added before its file was probed gets its streams later, and a file that is gone is
//! trashed as measured on plexscratch (`tests/fixtures/missing_file_scan_s43.json`). zurg
//! keeps the ratingKey it matched each release to and drops the release's IMDb id when
//! that key disappears, so a row's key is minted once and kept across pushes and restarts.

use std::sync::LazyLock;

use regex::Regex;

use super::*;
use crate::identify::{self, Identifier, PlexSource, TmdbSource};
use crate::{hierarchy, mediainfo};

/// Where minted keys start: above the catalog's own ranges (movies and episodes 1000+,
/// shows 20000+, seasons 30000+), playlists (40000+) and collections (50000+).
const SCAN_KEY_BASE: i64 = 100_000;

/// A media file to become a row: its Part `file` and size.
#[derive(Debug, Clone)]
pub(super) struct Found {
    pub file: String,
    pub size: u64,
}

/// The library rows are placed in, and the providers that identify them.
pub(super) struct Library {
    pub key: String,
    pub kind: String,
    pub title: String,
    pub tmdb: Option<Arc<tmdb::Tmdb>>,
    pub plex: Option<Arc<plexmeta::PlexMeta>>,
}

/// A new file and what its source says about its release, ready to become a row.
pub(super) struct Arrival {
    pub found: Found,
    /// The release's name, and the folder its files sit in (a show's `Location`).
    pub release: String,
    pub release_path: String,
    /// `hash`, `imdb_id`, `size` and `media_info`, as zurg's `/api/library/entry` named them.
    pub entry: Value,
    /// Fields the row carries for its source (`_source`, `_sid`, `_path`, `_ids`).
    pub extra: Map<String, Value>,
    /// What identification gave this file's row: TMDB's fields (`metadata::enrich`),
    /// Plex's guids, and how sure (`identify.rs`).
    pub meta: Option<Map<String, Value>>,
    /// The season and episode TMDB knows the file by, once its show is identified (`on_tmdb`).
    pub numbers: Option<(i64, i64)>,
}

fn extension(file: &str) -> String {
    file.rsplit_once('.')
        .map(|(_, extension)| extension.to_ascii_lowercase())
        .filter(|extension| (2..=5).contains(&extension.len()))
        .unwrap_or_else(|| "mkv".to_string())
}

fn stem(file: &str) -> &str {
    let name = file.rsplit('/').next().unwrap_or(file);
    name.rsplit_once('.').map_or(name, |(stem, _)| stem)
}

/// Plex's scanners skip a release's sample clip; a separate row titled as the film
/// would be the result here.
pub(super) fn is_sample(file: &str) -> bool {
    stem(file)
        .to_ascii_lowercase()
        .split(|c: char| !c.is_ascii_alphanumeric())
        .any(|word| word == "sample")
}

/// (season, episode) of a show's file: the file's own numbers, else its release's; an
/// episode with no season (absolute numbering) is season 1. `folder` is where the source says
/// the provider lists the file inside its release (`folder_of`), `None` when it says nothing.
pub(super) fn episode_of(release: &str, folder: Option<&str>, file_name: &str) -> Option<(i64, i64)> {
    let (season, episode) = numbers_of(release, folder, file_name)?;
    // An anime cour on TMDB's numbering (`anime.rs`).
    if let Some(numbers) = super::anime::renumber(release, file_name, season, episode) {
        return Some(numbers);
    }
    Some((season.unwrap_or(1), episode))
}

/// Whether a file's numbers are its name's own, as scene releases number them -- not ones the
/// anime mapping already put on TMDB's numbering (`anime::renumber`).
pub(super) fn scene_numbered(release: &str, folder: Option<&str>, file_name: &str) -> bool {
    let Some((season, episode)) = numbers_of(release, folder, file_name) else {
        return false;
    };
    super::anime::renumber(release, file_name, season, episode).is_none()
}

/// The season (if named) and episode a show's file is numbered by: the file's own name, else
/// its release's, else the shapes only a show's file can be read by (`numbered_in_release`).
fn numbers_of(release: &str, folder: Option<&str>, file_name: &str) -> Option<(Option<i64>, i64)> {
    let parsed = (identify::parse_name(release), identify::parse_name(file_name));
    // `[CyC] Natsume Yuujinchou Shi 01`, `[tlacatlc6] Natsume Yuujinchou San 01v2`: the season as a
    // Japanese numeral (四 shi, 三 san) right before the episode. The second read as season 1.
    let stem = file_name.rsplit_once('.').map_or(file_name, |(stem, _)| stem);
    if parsed.1.season.is_none() {
        if let Some(found) = JAPANESE_SEASON.captures(stem) {
            let season = JAPANESE_NUMERALS.iter().position(|word| *word == &found[1]).map(|index| index as i64 + 2);
            if let (Some(season), Ok(episode)) = (season, found[2].parse::<i64>()) {
                return (episode > 0).then_some((Some(season), episode));
            }
        }
    }
    // `aaf-wilty.302.avi` in `Would.I.Lie.To.You.S03E01.PROPER…`: a scene file numbering its season
    // and episode as one number, which the release's own `S03E01` overruled. That NZB holds two
    // episodes, and the production server put the second on the first (2026-10-16). Only the
    // release's season, so a `720` or a film's `300` is never read as one.
    if parsed.1.episode.is_none() {
        if let (Some(season), Some(found)) = (parsed.0.season.filter(|season| (1..=9).contains(season)), SCENE_SHORTHAND.captures(stem)) {
            if found[1].parse::<i64>().ok() == Some(season) {
                if let Some(episode) = found[2].parse::<i64>().ok().filter(|episode| *episode > 0) {
                    return Some((Some(season), episode));
                }
            }
        }
    }
    if let Some(episode) = parsed.1.episode.or(parsed.0.episode) {
        return Some((parsed.1.season.or(parsed.0.season), episode));
    }
    numbered_in_release(&parsed.0, release, folder, file_name)
}

static SCENE_SHORTHAND: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?:^|[\s._-])(\d)(\d{2})(?:[\s._-]|$)").unwrap());

// Two to ten, as anime titles write a season: `Ni`, `San`, `Shi`, `Go`. Capitalised only, so the
// particle in `Mahou Shoujo ni Akogarete 10` is no season.
const JAPANESE_NUMERALS: [&str; 9] = ["Ni", "San", "Shi", "Go", "Roku", "Nana", "Hachi", "Kyuu", "Juu"];
static JAPANESE_SEASON: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"[\s._](Ni|San|Shi|Go|Roku|Nana|Hachi|Kyuu|Juu)[\s._]+(\d{2,3})(?:v\d)?(?:[\s._(\[]|$)").unwrap()
});

/// The provider's folder a row's or an arrival's file sits in (`_folder`, as the source said it).
pub(super) fn folder_of(fields: &Map<String, Value>) -> Option<&str> {
    fields.get("_folder").and_then(Value::as_str)
}

// Names that carry an episode number only beside their release, each measured on the production
// server's feed (2026-10-15, 4,405 files unplaced): a film called `Apollo 13` reads the same,
// so they are read for a show's files and never by `parse_name`.
// `35. Returning Memories` in `Sailor.Moon.S01…`, `02 - Who Mourns for Adonais` in `Star Trek … Season 2`.
// `-01 Are You Afraid of the Dark - The Tale of the Phantom Cab` in its pack's `Season 1` folder.
static LEADING: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^-?(\d{1,3})(?:\.\s*|\s+-\s+|\s+)[^\d\s]").unwrap());
// The season's number before the episode's, in a folder of that season: `601 - The Tale of the
// Zombie Dice` (Are You Afraid of the Dark's `Season 6`), `05.01 - Steve Martin, Blondie` (Saturday
// Night Live's), `1.01. A Night At The Katz Motel` (Courage's), and Phineas and Ferb's `S1 (2007-9)`
// with two segments or two episodes on one file: `101a-b - Rollercoaster; …`, `237-8 - Summer Belongs to You!`.
static SEASON_LEADING: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(\d{1,2})\.?(\d{2})(?:[a-z](?:-[a-z])?|-\d{1,2})?(?:\.\s*|\s+-\s+|\s+)[^\d\s]").unwrap()
});
// A folder of one season, as packs on fun's providers name them (2026-10-15): `Stagione 6`,
// `Season 13`, `Are You Afraid Of The Dark Season 4`, `S2 (2009-11)`, `Saturday Night Live -  Season 05`,
// `Season 1 (1999–2001) (Classics Remastered)`. `Season 2006` (by year) and `Season TV` name none.
static FOLDER_SEASON: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(?:^|[\s._-])(?:season|stagione|staffel|saison|temporada|s)[\s._-]*(\d{1,2})(?:$|[^\d\-–+&,e])").unwrap()
});
// `011 - Terror Of The Ancient Noh Mask (v2)` (InuYasha), `063 - Пещера Али-Баббы` (DuckTales): an
// absolute number zero-padded to three digits. `608 - The Tale of the Vampire Town` is season 6's
// eighth in `Are You Afraid Of The Dark Complete Series`, so only 000-199 are read so.
static LEADING_ABSOLUTE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^([01]\d{2})(?:-[01]\d{2})?(?:\.\s*|\s+-\s+)\S").unwrap());
static UNDERSCORE_SEASON_EP: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)(?:^|[\s._-])S(\d{1,2})[._-]?EP?(\d{1,4})(?:[^\da-z]|$)").unwrap());
static SEASON_DASH_EP: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)(?:^|[\s._-])Season[\s._]*(\d{1,2})[\s._]*-[\s._]*(\d{1,4})(?:v\d)?(?:[^\da-z]|$)").unwrap());
static PAIRED_CODES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?:^|\s-\s)(\d{3}),\s*(\d{3})\s-\s").unwrap());
static DOUBLE_NX: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)(?:^|[\s._-])(\d{1,2})x(\d{2,3})x\d{2,3}(?:[\s._-]|$)").unwrap());
static EP_ANY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)(?:^|[\s._-])ep(\d{1,4})(?:[^\da-z]|$)").unwrap());
static PRIMED: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:\[[^\]]*\]\s*)?([^\[\]]+?)\s-\s(\d{2,3})'(?:[\s\[(]|$)").unwrap());
// What a show's one-folder release holds besides its episodes, named like them: `To LOVE-Ru OVA 04`,
// `Mawaru_Penguindrum_NCED_07`, `Uchuu Kyoudai - NCOP04`.
static NOT_EPISODE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b(?:nc(?:op|ed)\w*|ova|oad|op|ed|sp|pv|cm|special|specials|movie|menu|preview|trailer|recap|extra|extras|bonus|featurette|interview|behind|making|creditless|clean|teaser|promo|omake)\b").unwrap()
});
// `Carl.Sagans.Cosmos.10of13.The.Edge.of.Forever`.
static OF_COUNT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)(?:^|[\s._-])(\d{1,3})of(\d{1,3})(?:[\s._-]|$)").unwrap());
// `[Coalgirls]_Hunter_X_Hunter_001_(1920x1080…)`, `Lovely Runner 14`, `House M.D. 15 - Black Hole`,
// `[Zurako]_Mawaru_Penguindrum_08_Even_if…`: the show's own name, then the number.
static TITLE_NUMBER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(.*?\S)\s+(\d{2,4})(?:v\d)?(?:\s.*)?$").unwrap());
// A release that names more than one season: `Season 1 2 3 4`, `S01-S07`, `Seasons 1-15`, `Complete Series`.
static SEASONS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)seasons|complete|\bs\d{1,2}\s*[-+&]\s*s?\d|season[\s._]*\d{1,2}[\s._,&+-]+\d").unwrap()
});

/// `numbers_of` for a file whose name has no number `parse_name` reads. zurg lists a pack's
/// files in one folder, so Smallville's and Saturday Night Live's `01`s of every season would
/// fold onto one episode: two-digit leading numbers in a release of several seasons are read
/// only under the season folder the provider lists them in (`folder`, which zurg sends since
/// the 2026-10-15 feed), and a release whose files the provider lists at its top level
/// (`Some("")`) was never flattened.
fn numbered_in_release(parsed: &identify::Parsed, release: &str, folder: Option<&str>, file_name: &str) -> Option<(Option<i64>, i64)> {
    let release = release.rsplit('/').next().unwrap_or(release);
    let stem = file_name.rsplit_once('.').map_or(file_name, |(stem, _)| stem);
    // Plex's local-extra suffixes: `002 - Deleted Scene Episodes 2-4 [iKaos]-deleted` is no episode 2.
    if EXTRA.is_match(stem) {
        return None;
    }
    let positive = |number: &str| number.parse::<i64>().ok().filter(|number| *number > 0);
    // The folder right above the file: Smallville's `Stagione 6/Extra` holds no episodes.
    let folder_season = folder
        .and_then(|folder| folder.rsplit('/').next())
        .filter(|folder| !SEASONS.is_match(folder))
        .and_then(|folder| FOLDER_SEASON.captures(folder))
        .and_then(|found| positive(&found[1]));
    let several = folder_season.is_none() && (SEASONS.is_match(release) || parsed.pack && parsed.season.is_none());
    let one_season = folder_season.or(parsed.season.filter(|_| !several));
    // Season and episode in shapes `parse_name` leaves to a show's files (fun's feed, 2026-10-15):
    // `MLB_S6EP01_Climatiqueen` (no word boundary before an underscore's `S`), `Grand.Blue.Season.3-09`.
    if let Some(found) = UNDERSCORE_SEASON_EP.captures(stem).or_else(|| SEASON_DASH_EP.captures(stem)) {
        return Some((positive(&found[1]), positive(&found[2])?));
    }
    // Two episodes in one file go on the first (rex keeps a file on one row):
    // `The Amazing World of Gumball - 207, 208 - The Phone - The Job` (season 2's 7th and 8th),
    // `Animaniacs - 1x60x61 - Testimonials & Babblin' Bijou`.
    if let Some(found) = PAIRED_CODES.captures(stem) {
        let (first, second) = (positive(&found[1])?, positive(&found[2])?);
        if first / 100 == second / 100 && second == first + 1 && first % 100 > 0 {
            return Some((Some(first / 100), first % 100));
        }
    }
    if let Some(found) = DOUBLE_NX.captures(stem) {
        return Some((positive(&found[1]), positive(&found[2])?));
    }
    // `meteor_garden_ep27#[jem]`: a stray character where `parse_name` wants a separator.
    if let Some(found) = EP_ANY.captures(stem) {
        return Some((one_season, positive(&found[1])?));
    }
    // `[Sephirotic] Evangelion - 21'`: the director's cut of episode 21, another version of it
    // (TMDB lists no special for it). `Evangelion - The End of Evangelion - 25'` is the film, in parts.
    if let Some(found) = PRIMED.captures(stem).filter(|found| !found[1].contains(" - ")) {
        return Some((one_season, positive(&found[2])?));
    }
    if let Some(season) = folder_season {
        if let Some(found) = SEASON_LEADING.captures(stem).filter(|found| positive(&found[1]) == Some(season)) {
            return Some((Some(season), positive(&found[2])?));
        }
    }
    if let Some(season) = one_season {
        if let Some(found) = LEADING.captures(stem) {
            return Some((Some(season), positive(&found[1])?));
        }
    }
    if parsed.season.is_none() && !several {
        // `147-148 - The Tragic Love Song Of Destiny` (InuYasha): two episodes, on the first.
        if let Some(found) = LEADING_ABSOLUTE.captures(stem) {
            return Some((None, positive(&found[1])?));
        }
        // `01 - Guilmon Is Born! The Digimon That I Created!`, at the top of `[DmonHiro] Digimon
        // Tamers (BD, 1080p)`: listed so by the provider, so no pack's seasons were folded into it.
        if folder == Some("") {
            if let Some(found) = LEADING.captures(stem) {
                return Some((None, positive(&found[1])?));
            }
        }
    }
    if let Some(found) = OF_COUNT.captures(stem) {
        let (episode, count) = (positive(&found[1])?, positive(&found[2])?);
        return (episode <= count).then_some((one_season, episode));
    }
    // The name before the number must be the release's (`Magic Knight Rayearth 09` in
    // `Magic Knight Rayearth TV`), so `To LOVE-Ru OVA 04` or `Mawaru_Penguindrum_NCED_07` is no
    // episode 4 or 7, and a show named with a number (`Room 104`) never reads it as one.
    // Dots too: `BBC.Frozen.Planet.01.To.the.Ends.of.the.Earth`.
    let spaced = stem.replace(['_', '.'], " ");
    let bare = BRACKETS.replace_all(&spaced, " ");
    let found = TITLE_NUMBER.captures(bare.trim())?;
    let number = positive(&found[2])?;
    // Four digits for a long run (`[HatSubs] One Piece 1106`), never a year or a resolution.
    if found[2].len() == 4 && (1900..=2099).contains(&number) || matches!(number, 480 | 576 | 720 | 1080 | 2160) {
        return None;
    }
    let fold = |text: &str| text.chars().filter(|c| c.is_alphanumeric()).collect::<String>().to_lowercase();
    let (head, show) = (fold(&found[1]), fold(&parsed.title));
    // The release may say more than the file (`… TV`, `… BD`), never less: a file's
    // `OVA 04`, `NCED 07` or `207, 208` (two episodes) is not the show's name.
    // A release of one episode is named as its file (`[HatSubs] One Piece 1107 (WEB 1080p)`), so the
    // release's own title carries the number; four digits is a long run, never `Room 104`.
    let alone = found[2].len() == 4 && show == format!("{head}{}", &found[2]);
    let named = head.len() >= 4
        && (alone || show == head || show.starts_with(&head) && !show[head.len()..].starts_with(|c: char| c.is_ascii_digit()));
    // A file the provider lists at the top of a one-folder release is an episode of it whatever
    // the file calls the show: `[Silver_zero_Subs]Alps_no_Shoujo_Heidi_01` in `Heidi blu-ray
    // complete`, `[EG]Cowbop_Bebop_01` (sic) in `Cowboy Bebop [EG]` -- unless its name says it is
    // something else.
    let top_level = folder == Some("") && head.len() >= 4 && !NOT_EPISODE.is_match(&found[1]);
    (named || top_level).then_some(())?;
    Some((one_season, number))
}

static BRACKETS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[\[(][^\])]*[\])]").unwrap());
static EXTRA: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)-(?:behindthescenes|deleted|featurette|interview|scene|short|trailer|other)$").unwrap()
});

/// The season and episode TMDB knows a file of `show` by, when TMDB has no season by the
/// file's own number: the production server held about 30 such seasons on `local://` (2026-10-13).
/// Scene releases of anime number seasons as TVDB does (`Jujutsu.Kaisen.S03E03`, TMDB's
/// S1E50), which anime-lists converts for a known show (`anime::tvdb_to_tmdb`); MythBusters'
/// files number them by year (`MythBusters.2004x01`), which TMDB's season of that year is.
/// Only an answer TMDB has, season and episode, is taken; a season TMDB has is never moved.
#[cfg(test)]
pub(super) fn on_tmdb(tmdb: Option<&dyn TmdbSource>, show: i64, numbers: (i64, i64)) -> (i64, i64) {
    let seasons = tmdb.and_then(|t| identify::tmdb_details(t, "tv", show)).map(|details| numbered_seasons(tmdb, show, &details)).unwrap_or_default();
    on_seasons(&seasons, show, numbers, true)
}

/// A TMDB season: its number, how many episodes it has, its first air date, and the number of
/// its first episode (`numbered_seasons`).
pub(super) type Season = (i64, i64, String, i64);

/// A show's TMDB seasons, from its details, each taken to number its episodes from 1.
pub(super) fn tmdb_seasons(details: &Value) -> Vec<Season> {
    details
        .get("seasons")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|row| {
            Some((
                row.get("season_number")?.as_i64()?,
                row.get("episode_count").and_then(Value::as_i64).unwrap_or(0),
                row.get("air_date").and_then(Value::as_str).unwrap_or_default().to_string(),
                1,
            ))
        })
        .collect()
}

/// `tmdb_seasons`, with the number TMDB gives each season's first episode for a show
/// anime-lists files as anime: TMDB numbers One Piece's on from the show's first episode
/// (its S22 is 1089-1155) and Fairy Tail's and My Hero Academia's from 1, while anime-lists
/// marks One Piece and Fairy Tail alike (`tmdbseason="a"`). Read off the runtime lock: a
/// season list TMDB has not answered before goes to the network.
pub(super) fn numbered_seasons(tmdb: Option<&dyn TmdbSource>, show: i64, details: &Value) -> Vec<Season> {
    let mut seasons = tmdb_seasons(details);
    let Some(t) = tmdb.filter(|_| super::anime::is_anime(show)) else {
        return seasons;
    };
    for season in seasons.iter_mut().filter(|season| season.0 > 0) {
        let listed = t.get(&format!("tv/{show}/season/{}", season.0), &[]);
        if let Some(first) = listed
            .as_ref()
            .and_then(|listed| listed.get("episodes")?.as_array()?.first()?.get("episode_number")?.as_i64())
        {
            season.3 = first;
        }
    }
    seasons
}

/// `on_tmdb` for seasons already read. `scene`: the numbers are the name's own
/// (`scene_numbered`), which for a known anime show are TVDB's even where TMDB has a season
/// by the same number: TMDB's Ascendance of a Bookworm S2 is the 2026 cour, and the BD
/// remux's `S02E01-Apprentice Priestess` TVDB's second season, TMDB's S1E15 (the production
/// server held 12 of them as versions of the 2026 cour's episodes, 2026-10-14).
pub(super) fn on_seasons(seasons: &[Season], show: i64, (season, episode): (i64, i64), scene: bool) -> (i64, i64) {
    let fits = |(season, episode): (i64, i64)| seasons.iter().any(|row| row.0 == season && episode >= 1 && episode <= row.1);
    if scene && season > 0 {
        if let Some(mapped) = super::anime::tvdb_to_tmdb(show, season, episode).filter(|mapped| fits(*mapped)) {
            return mapped;
        }
    }
    // An anime numbered from its first episode on (`[SubsPlease] One Piece - 1130`, `[Anime Time]
    // My Hero Academia - 089`, `Fairy.Tail.E233.Song.of.the.Faries`) reads as one long season 1,
    // which TMDB splits: counted through TMDB's seasons in order it is TMDB's episode (Fairy
    // Tail's E233 and E202 land on the TMDB episodes their titles name, My Hero Academia's 150 on
    // S7E12). The production server held 2,042 One Piece, 347 My Hero Academia and 178 Fairy Tail
    // episodes on `local://` so (2026-10-16), as fun's real Plex holds them. Only a show
    // anime-lists files as anime: Sesame Street's `Episode 1840` is a production number.
    if season == 1 && episode > 1 && !fits((1, episode)) && super::anime::is_anime(show) {
        if let Some(counted) = counted_through(seasons, episode) {
            return counted;
        }
    }
    if seasons.is_empty() || seasons.iter().any(|row| row.0 == season) {
        return (season, episode);
    }
    if let Some(mapped) = super::anime::tvdb_to_tmdb(show, season, episode).filter(|mapped| fits(*mapped)) {
        return mapped;
    }
    if season >= 1900 {
        let aired: Vec<i64> =
            seasons.iter().filter(|row| row.0 > 0 && row.2.starts_with(&season.to_string())).map(|row| row.0).collect();
        if let [only] = aired[..] {
            if fits((only, episode)) {
                return (only, episode);
            }
        }
    }
    (season, episode)
}

/// The TMDB season and episode an absolute episode number is, its seasons counted in order and
/// the episode numbered as TMDB numbers that season's; None past the last episode TMDB lists.
fn counted_through(seasons: &[Season], episode: i64) -> Option<(i64, i64)> {
    let mut regular: Vec<&Season> = seasons.iter().filter(|row| row.0 > 0).collect();
    regular.sort_by_key(|row| row.0);
    let mut left = episode;
    for row in regular {
        if left <= row.1 {
            return Some((row.0, row.3 + left - 1));
        }
        left -= row.1;
    }
    None
}

/// TMDB's numbers for a file whose season TMDB lacks, through IMDb's numbering of the show
/// (`imdb.rs`): the IMDb id of IMDb's episode by those numbers, and the TMDB episode TMDB's
/// `/find` names for it, if it is the identified show's. A release named as the show plus a
/// subtitle is another series of it -- `BLEACH.Thousand-Year.Blood.War.S04E01` identified as
/// Bleach would be IMDb's Bleach S4, the Bount arc of the original series -- and is left.
pub(super) fn by_imdb(
    tmdb: &dyn TmdbSource,
    show: i64,
    show_imdb: &str,
    release_title: &str,
    show_title: &str,
    (season, episode): (i64, i64),
) -> Option<(i64, i64)> {
    let (release, title) = (super::anime::normalize(release_title), super::anime::normalize(show_title));
    if title.is_empty() || (release != title && release.starts_with(&format!("{title} "))) {
        return None;
    }
    let tconst = super::imdb::episode(show_imdb, season, episode)?;
    let found = tmdb.get(&format!("find/{tconst}"), &[("external_source", "imdb_id".to_string())])?;
    let hit = found.get("tv_episode_results")?.as_array()?.first()?;
    if hit.get("show_id")?.as_i64()? != show {
        return None;
    }
    Some((hit.get("season_number")?.as_i64()?, hit.get("episode_number")?.as_i64()?))
}

/// `on_seasons`, then IMDb's numbering where TMDB still has no season by the file's number.
pub(super) fn on_tmdb_or_imdb(
    tmdb: Option<&dyn TmdbSource>,
    seasons: &[Season],
    show: i64,
    show_imdb: Option<&str>,
    release_title: &str,
    show_title: &str,
    numbers: (i64, i64),
    scene: bool,
) -> (i64, i64) {
    let placed = on_seasons(seasons, show, numbers, scene);
    if seasons.is_empty() || seasons.iter().any(|row| row.0 == placed.0) {
        return placed;
    }
    match (tmdb, show_imdb) {
        (Some(t), Some(imdb)) => by_imdb(t, show, imdb, release_title, show_title, numbers).unwrap_or(placed),
        _ => placed,
    }
}

// What a pack's file name says before its title: `14. `, `1.01. `, `101a-b - `, `237-8 - `, `-01 `.
static TITLE_LEAD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^-?\d+(?:\.\d+)?(?:[a-z](?:-[a-z])?|-\d{1,2})?\.?\s*(?:-\s+)?").unwrap());

/// The TMDB episode a pack's file is titled as, in the season its numbers name, else the
/// numbers. TMDB files Courage the Cowardly Dog and Phineas and Ferb by their 11-minute
/// segments (26 and 47 a season), and their packs by the half hour: `Season 2/14. The Magic
/// Tree Of Nowhere. Robot Randy` is TMDB's S2E1 "The Magic Tree of Nowhere", not S2E14
/// (measured 2026-10-15; IMDb numbers them by the half hour too, but TMDB's /find answers
/// half of IMDb's ids with one segment and the rest with nothing). The file's title must
/// start with one episode's whole name, the longest when several do, and only a name with
/// no number of its own (`numbered_in_release`) is read so: a scene release's S02E05 is its own.
pub(super) fn by_title(
    tmdb: Option<&dyn TmdbSource>,
    show: i64,
    release: &str,
    file_name: &str,
    (season, episode): (i64, i64),
) -> (i64, i64) {
    let parsed = (identify::parse_name(release), identify::parse_name(file_name));
    let (Some(t), None) = (tmdb, parsed.1.episode.or(parsed.0.episode)) else {
        return (season, episode);
    };
    let fold = |text: &str| text.chars().filter(|c| c.is_alphanumeric()).collect::<String>().to_lowercase();
    let stem = file_name.rsplit_once('.').map_or(file_name, |(stem, _)| stem);
    let title = fold(&BRACKETS.replace_all(&TITLE_LEAD.replace(stem, ""), " "));
    let Some(listed) = t.get(&format!("tv/{show}/season/{season}"), &[]) else {
        return (season, episode);
    };
    let mut named: Vec<(usize, i64)> = listed
        .get("episodes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|row| {
            let name = fold(row.get("name")?.as_str()?);
            (name.len() >= 6 && title.starts_with(&name)).then_some((name.len(), row.get("episode_number")?.as_i64()?))
        })
        .collect();
    named.sort_unstable_by(|a, b| b.0.cmp(&a.0));
    match named.as_slice() {
        [(longest, number), rest @ ..] if rest.first().is_none_or(|next| next.0 < *longest) => (season, *number),
        _ => (season, episode),
    }
}

/// Identification, off the lock, once per release: who it is (`identify.rs` -- an IMDb
/// id the source pushed is exact; otherwise its name to Plex's matcher and TMDB, their agreement
/// the confidence), then for each new file the fields TMDB gives its row and Plex's own
/// guids for it. A release named as the other kind of title than its library's, or not
/// at all, stays unmatched for Fix Match.
pub(super) fn name_release(plan: &Library, arrivals: &mut [Arrival], files: usize) {
    let (tmdb, plex) = (plan.tmdb.as_deref(), plan.plex.as_deref());
    let Some(first) = arrivals.first() else {
        return;
    };
    if tmdb.is_none() && plex.is_none() {
        return;
    }
    let hash = first
        .entry
        .get("hash")
        .and_then(Value::as_str)
        .filter(|hash| !hash.is_empty())
        .unwrap_or("zurg")
        .to_ascii_lowercase();
    let mut known = HashMap::new();
    if let Some(imdb) = first.entry.get("imdb_id").and_then(Value::as_str).filter(|id| !id.is_empty()) {
        known.insert(hash.clone(), imdb.to_string());
    }
    let release = first.release.clone();
    let first_file = first.found.file.rsplit('/').next().unwrap_or_default().to_string();
    // A TMDB id with no IMDb id is read the way a `{tmdb-…}` folder tag is: exact. So is the
    // show an anime release's cour belongs to (`anime.rs`), which DMM's id for its hash and
    // its title both miss.
    let anime = (plan.kind == "show").then(|| super::anime::lookup(&release, &first_file)).flatten();
    if anime.is_some() {
        known.clear();
    }
    let named = match (first.entry.get("tmdb_id").and_then(Value::as_str), anime) {
        (_, Some((target, _))) => format!("{release} {{tmdb-{}}}", target.tmdb),
        (Some(id), None) if known.is_empty() && !id.is_empty() => format!("{release} {{tmdb-{id}}}"),
        _ => release.clone(),
    };
    let identifier = Identifier {
        tmdb: tmdb.map(|t| t as &dyn TmdbSource),
        plex: plex.map(|p| p as &dyn PlexSource),
        known,
    };
    let mut identity = identifier.identify_as(&named, Some(&hash), files, Some(&plan.kind));
    // A release named in another language than its files (`Хватай Сон Чжэ и беги Lovely Runner`
    // holding `Lovely.Runner.2024.S01E01`, `Détective.Conan.-.Inédits.VF` holding `Détective
    // Conan S11E01`) is the title its files name: the production server held both on `local://`, and
    // Lovely Runner beside the show of that name (2026-10-13).
    let named_nothing = |identity: &identify::Identity| {
        identity.kind != plan.kind || (identity.tmdb.is_none() && identity.plex.is_none() && identity.imdb.is_none())
    };
    if named_nothing(&identity) && !first_file.is_empty() && first_file != release {
        let by_file = identifier.identify_as(stem(&first_file), None, 1, Some(&plan.kind));
        if !named_nothing(&by_file) {
            identity = by_file;
        }
    }
    let report = json!({
        "confidence": identity.confidence, "source": identity.source, "imdb": identity.imdb,
        "tmdb": identity.tmdb, "plex": identity.plex,
        "disputed": identity.disputed.as_ref().map(|d| json!({"tmdb": d.tmdb, "imdb": d.imdb, "kind": d.kind})),
    });
    let matched = identity.kind == plan.kind
        && (identity.tmdb.is_some() || identity.plex.is_some() || identity.imdb.is_some());
    info!(
        "FEED identified {release:?} -> {} {} ({})",
        identity.confidence,
        identity.imdb.as_deref().unwrap_or("?"),
        identity.source.as_deref().unwrap_or("-")
    );
    if !matched {
        for arrival in arrivals.iter_mut() {
            arrival.meta = Some(Map::from_iter([("_identify".to_string(), report.clone())]));
        }
        return;
    }
    let show = plan.kind == "show";
    let tmdb_kind = if show { "tv" } else { "movie" };
    let details = tmdb.zip(identity.tmdb).and_then(|(t, id)| identify::tmdb_details(t, tmdb_kind, id));
    let externals: Vec<Value> = identity
        .imdb
        .iter()
        .map(|imdb| json!({"id": format!("imdb://{imdb}")}))
        .chain(identity.tmdb.iter().map(|id| json!({"id": format!("tmdb://{id}")})))
        .collect();
    let seasons = match (show, plex, identity.plex.as_deref()) {
        (true, Some(p), Some(guid)) => identify::plex_children(p, guid),
        _ => HashMap::new(),
    };
    let show_seasons = details
        .as_ref()
        .filter(|_| show)
        .map(|details| numbered_seasons(tmdb.map(|t| t as &dyn TmdbSource), identity.tmdb.unwrap_or_default(), details))
        .unwrap_or_default();
    let mut episodes: HashMap<String, HashMap<String, String>> = HashMap::new();
    for arrival in arrivals.iter_mut() {
        let file_name = arrival.found.file.rsplit('/').next().unwrap_or_default().to_string();
        let folder = folder_of(&arrival.extra).map(str::to_string);
        let numbers = if show { episode_of(&release, folder.as_deref(), &file_name) } else { None };
        let numbers = match (numbers, identity.tmdb.filter(|_| identity.kind == "show")) {
            (Some(numbers), Some(id)) => Some(on_tmdb_or_imdb(
                tmdb.map(|t| t as &dyn TmdbSource),
                &show_seasons,
                id,
                // A show named by its TMDB id (an anime cour's) carries no IMDb id of its own.
                identity.imdb.as_deref().or_else(|| details.as_ref()?.pointer("/external_ids/imdb_id")?.as_str()),
                &identify::parse_name(&release).title,
                details.as_ref().and_then(|d| d.get("name")).and_then(Value::as_str).unwrap_or_default(),
                numbers,
                scene_numbered(&release, folder.as_deref(), &file_name),
            ))
            .map(|numbers| by_title(tmdb.map(|t| t as &dyn TmdbSource), id, &release, &file_name, numbers)),
            (numbers, _) => numbers,
        };
        arrival.numbers = numbers;
        let mut meta = Map::new();
        meta.insert("title".to_string(), json!(identity.parsed.title));
        meta.insert("summary".to_string(), json!(""));
        if let (Some(details), Some(id), Some(t)) = (&details, identity.tmdb, plan.tmdb.as_deref()) {
            let parsed = Map::from_iter([
                ("title".to_string(), json!(identity.parsed.title)),
                ("year".to_string(), json!(identity.parsed.year)),
                ("season".to_string(), json!(numbers.map(|n| n.0))),
                ("episode".to_string(), json!(numbers.map(|n| n.1))),
            ]);
            let (episode, season) = match numbers {
                Some((season, episode)) => (
                    t.episode(&json!(id), &json!(season), &json!(episode)),
                    t.season(&json!(id), &json!(season)),
                ),
                None => (None, None),
            };
            metadata::enrich(
                &mut meta,
                details,
                &parsed,
                episode.as_ref(),
                Some(season.as_ref().unwrap_or(&json!({}))),
                tmdb_kind,
                id,
            );
        } else if !externals.is_empty() {
            // Named, but no TMDB to describe it: the ids alone.
            let field = if show { "_show_external_guids" } else { "Guid" };
            meta.insert(field.to_string(), Value::Array(externals.clone()));
        }
        // TMDB has no poster for this title, or does not know it, and Plex does: its own
        // artwork draws the tile (the production server, 2026-10-12: the Monogatari seasons Plex keeps as shows
        // of their own, Wallace & Gromit, Tears of Steel).
        // ponytail: Plex serves its artwork at full size (about 2 MB a poster), and
        // `fetch_remote_image` keeps 512 images in memory; fine for the few titles only Plex
        // draws, worth a size cap if that set grows.
        let (poster, art) = if show { ("_show_poster", "_show_art") } else { ("_poster_url", "_art_url") };
        for (field, plex) in [(poster, &identity.thumb), (art, &identity.art)] {
            if let Some(url) = plex.as_ref().filter(|_| meta.get(field).is_none_or(Value::is_null)) {
                meta.insert(field.to_string(), json!(url));
            }
        }
        if let Some(year) = identity.parsed.year.filter(|_| !meta.contains_key("year") && !show) {
            meta.insert("year".to_string(), json!(year));
        }
        let identity_key = identity
            .tmdb
            .map(|id| format!("tmdb:{id}"))
            .or_else(|| identity.imdb.as_ref().map(|imdb| format!("imdb:{imdb}")))
            .or_else(|| identity.plex.clone())
            .unwrap_or_default();
        meta.insert("_identity".to_string(), json!(identity_key));
        if show {
            meta.insert("_tmdb_id".to_string(), json!(identity_key));
        }
        // Plex's own guids, which a real PMS's rows carry and plex.tv knows (cycle 70).
        if let Some(guid) = &identity.plex {
            if !show {
                meta.insert("guid".to_string(), json!(guid));
            } else {
                meta.insert("_show_guid".to_string(), json!(guid));
                if let Some((season, episode)) = numbers {
                    if let Some(season_guid) = seasons.get(&season.to_string()) {
                        meta.insert("_season_guid".to_string(), json!(season_guid));
                        let children = episodes.entry(season_guid.clone()).or_insert_with(|| {
                            plex.map(|p| identify::plex_children(p, season_guid)).unwrap_or_default()
                        });
                        if let Some(episode_guid) = children.get(&episode.to_string()) {
                            meta.insert("guid".to_string(), json!(episode_guid));
                        }
                    }
                }
            }
        }
        meta.insert("_identify".to_string(), report.clone());
        arrival.meta = Some(meta);
    }
}

pub(super) fn under(file: &str, folder: &str) -> bool {
    let folder = folder.trim_end_matches('/');
    file == folder || file.starts_with(&format!("{folder}/"))
}

impl Runtime {
    /// After the state is restored: the next minted key sits above every key in use or
    /// retired, whatever the saved counter says.
    pub(super) fn settle_scan_key(&mut self) {
        let highest = self
            .catalog
            .items()
            .iter()
            .filter_map(|row| key_of(row).and_then(|key| key.parse::<i64>().ok()))
            .chain(self.retired_keys.iter().copied())
            .max()
            .unwrap_or(0);
        self.next_scan_key = self.next_scan_key.max(highest + 1).max(SCAN_KEY_BASE);
    }

    /// A ratingKey no row has ever had.
    pub(super) fn mint_key(&mut self) -> String {
        let key = self.next_scan_key;
        self.next_scan_key += 1;
        self.dirty = true;
        key.to_string()
    }

    /// A library's folders, as its `Location` rows name them.
    pub(super) fn locations(&self, section: &Value) -> Vec<String> {
        section
            .get("Location")
            .and_then(Value::as_array)
            .map(|rows| {
                rows.iter()
                    .map(|row| string_value(row.get("path").unwrap_or(&Value::Null)))
                    .filter(|path| !path.is_empty())
                    .collect()
            })
            .unwrap_or_default()
    }

    /// `autoEmptyTrash`, as `/:/prefs` reports it: a write, else the captured value.
    fn auto_empty_trash(&self) -> bool {
        let value = self.prefs.get("autoEmptyTrash").cloned().or_else(|| {
            load_json_sidecar(&self.catalog_path, "server_prefs.json")
                .and_then(|rows| rows.as_array().cloned())
                .and_then(|rows| {
                    rows.into_iter()
                        .find(|row| row.get("id").and_then(Value::as_str) == Some("autoEmptyTrash"))
                })
                .and_then(|row| row.get("value").cloned())
        });
        match value {
            Some(Value::Bool(on)) => on,
            Some(Value::String(text)) => matches!(text.as_str(), "1" | "true"),
            Some(Value::Number(number)) => number.as_i64() == Some(1),
            _ => false,
        }
    }

    /// The scan's opening frames, as `notify_section_scan` writes them.
    pub(super) fn scan_started(&mut self, section: &str, title: &str) -> Map<String, Value> {
        let mut activity = butler::new_activity("library.update.section", "Scanning", false);
        self.notify_activity(&activity, "started");
        self.notify_status(&format!("Scanning the \"{title}\" section"), "");
        activity.insert("title".to_string(), json!(format!("Scanning {title}")));
        self.notify_activity(&activity, "updated");
        activity.insert("Context".to_string(), json!({"librarySectionID": section}));
        activity
    }

    /// Measured (6a2): a folder scan's steps name the release folder, at 0 then 99.
    pub(super) fn scan_progress(&mut self, activity: &mut Map<String, Value>, section: &str, subtitle: &str, progress: i64) {
        activity.insert("subtitle".to_string(), json!(subtitle));
        activity.insert("progress".to_string(), json!(progress));
        activity.insert("Context".to_string(), json!({"librarySectionID": section}));
        self.notify_activity(activity, "updated");
    }

    pub(super) fn scan_finished(&mut self, activity: &mut Map<String, Value>, section: &str) {
        activity.insert("progress".to_string(), json!(100));
        activity.insert("Context".to_string(), json!({"librarySectionID": section}));
        self.notify_activity(activity, "updated");
        let mut outer = butler::new_activity(
            "provider.subscriptions.process",
            "Processing subscriptions",
            true,
        );
        self.notify_activity(&outer, "started");
        self.notify_activity(activity, "ended");
        self.notify_status(
            "Library scan complete",
            "Extra information may still be downloading from the Internet",
        );
        let mut inner = outer.clone();
        inner.insert("uuid".to_string(), json!(butler::activity_uuid()));
        self.notify_activity(&inner, "started");
        inner.insert("progress".to_string(), json!(100));
        self.notify_activity(&inner, "ended");
        outer.insert("progress".to_string(), json!(100));
        self.notify_activity(&outer, "ended");
    }

    /// New files become rows: keys minted, Media built from the source's probe, episodes
    /// placed under the show and season they belong to (existing ones kept, by identity).
    /// Answers the keys of every row created, parents first.
    pub(super) fn place_arrivals(&mut self, plan: &Library, arrivals: Vec<Arrival>) -> Vec<String> {
        let taken: HashSet<String> = self.catalog.items().iter().flat_map(row_files).collect();
        let mut leaves = Vec::new();
        for arrival in arrivals {
            // A file another row already reads.
            if taken.contains(&arrival.found.file) {
                continue;
            }
            match self.leaf_for(plan, &arrival, None) {
                Some(leaf) => leaves.push(leaf),
                None => warning!("FEED {} has no episode number; not placed", arrival.found.file),
            }
        }
        // Announced by the caller once versions are folded (`push_created_frames`).
        self.place_leaves(plan, leaves, &HashSet::new(), false)
    }

    /// Leaf rows into the library: a show's episode under the show of its identity and the
    /// season of its index, each found or made. `quiet` names leaves that are not new
    /// (a re-identified row keeps its key): they get no creation frames.
    pub(super) fn place_leaves(&mut self, plan: &Library, leaves: Vec<Value>, quiet: &HashSet<String>, announce: bool) -> Vec<String> {
        if leaves.is_empty() {
            return Vec::new();
        }
        let mut rows = hierarchy::build(leaves);
        // `build` numbers new shows and seasons from its own bases; each becomes the
        // library's existing show (same identity) or season (same show and index), or
        // takes a fresh key.
        let mut rekeyed: HashMap<String, String> = HashMap::new();
        let mut existing: HashSet<String> = HashSet::new();
        let identity_of = |rows: &[Value], show: &str| {
            rows.iter()
                .find(|row| row.get("grandparentRatingKey").map(string_value).as_deref() == Some(show))
                .and_then(|row| row.get("_identity").cloned())
        };
        for index in 0..rows.len() {
            if rows[index].get("type").and_then(Value::as_str) != Some("show") {
                continue;
            }
            let temporary = key_of(&rows[index]).unwrap_or_default();
            let identity = identity_of(&rows, &temporary).unwrap_or(Value::Null);
            let found = self.catalog.items().iter().find(|row| {
                row.get("type").and_then(Value::as_str) == Some("show")
                    && string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == plan.key
                    && row.get("_identity") == Some(&identity)
            });
            let key = match found.and_then(key_of) {
                Some(key) => {
                    existing.insert(key.clone());
                    key
                }
                None => self.mint_key(),
            };
            rows[index]["_identity"] = identity;
            rekeyed.insert(temporary, key);
        }
        for index in 0..rows.len() {
            if rows[index].get("type").and_then(Value::as_str) != Some("season") {
                continue;
            }
            let temporary = key_of(&rows[index]).unwrap_or_default();
            let show = rows[index].get("parentRatingKey").map(string_value).unwrap_or_default();
            let show = rekeyed.get(&show).cloned().unwrap_or(show);
            let season_index = rows[index].get("index").and_then(Value::as_i64);
            let found = self.catalog.items().iter().find(|row| {
                row.get("type").and_then(Value::as_str) == Some("season")
                    && row.get("parentRatingKey").map(string_value).as_deref() == Some(show.as_str())
                    && row.get("index").and_then(Value::as_i64) == season_index
            });
            let key = match found.and_then(key_of) {
                Some(key) => {
                    existing.insert(key.clone());
                    key
                }
                None => self.mint_key(),
            };
            rekeyed.insert(temporary, key);
        }
        rekey(&mut rows, &rekeyed);
        let section_id = plan.key.parse::<i64>().map(Value::from).unwrap_or_else(|_| json!(plan.key));
        let mut created = Vec::new();
        let mut episodes = Vec::new();
        for mut row in rows {
            let key = key_of(&row).unwrap_or_default();
            let kind = row.get("type").and_then(Value::as_str).unwrap_or_default().to_string();
            if let Some(object) = row.as_object_mut() {
                object.insert("librarySectionID".to_string(), section_id.clone());
                object.insert("librarySectionTitle".to_string(), json!(plan.title));
                object.insert(
                    "librarySectionKey".to_string(),
                    json!(format!("/library/sections/{}", plan.key)),
                );
                object.insert("_scanned".to_string(), json!(true));
                // A show and season nobody identified: the local agent's guid, on their
                // own keys (the episode's was copied up by `build`).
                if matches!(kind.as_str(), "show" | "season")
                    && object.get("guid").and_then(Value::as_str).map_or(true, |guid| guid.starts_with("local://"))
                {
                    object.insert("guid".to_string(), json!(format!("local://{key}")));
                }
                if kind == "show" {
                    // A real PMS's show Location is the show's folder; here, the
                    // release folder each episode came from.
                    object.insert("Location".to_string(), json!([]));
                }
            }
            if existing.contains(&key) {
                match kind.as_str() {
                    "season" => {
                        self.refresh_season(&key, &row);
                    }
                    "show" => {
                        self.fill_artwork(&key, &row);
                    }
                    _ => {}
                }
                continue;
            }
            if kind == "episode" {
                episodes.push(key.clone());
            }
            prepare_rows(std::slice::from_mut(&mut row));
            self.catalog.add(row);
            created.push(key);
        }
        for key in &episodes {
            self.inherit_parents(key);
        }
        for key in created.iter().filter(|key| announce && !quiet.contains(*key)) {
            self.push_created_frames(key);
        }
        created
    }

    /// A season already here, and the season a leaf of it builds now: the leaf's
    /// identification of it when it has one, else its show's artwork where the season has
    /// none, and its show's guid and title as they stand. A season was only ever made, so
    /// one placed before its show was identified kept the local agent's guid, no ids and the
    /// blank tile after the show was re-identified in place (the production server,
    /// 2026-10-12: 186 seasons of 131 shows, Black Mirror's Season 6 among them). What a
    /// person locked stays. Answers whether anything changed.
    pub(super) fn refresh_season(&mut self, key: &str, built: &Value) -> bool {
        let Some(row) = self.catalog.get(key).and_then(Value::as_object).cloned() else {
            return false;
        };
        let present = |value: &&Value| !value.is_null() && value.as_str() != Some("");
        let named = |guid: Option<&Value>| {
            guid.and_then(Value::as_str).is_some_and(|guid| !guid.is_empty() && !guid.starts_with("local://"))
        };
        let tmdb_season = |ids: Option<&Value>| {
            ids.and_then(Value::as_array)?
                .iter()
                .find_map(|id| id.get("id")?.as_str()?.strip_prefix("tmdb://").map(str::to_string))
        };
        // The leaf's identity replaces one the season lacks, or another TMDB season's (its
        // show re-identified as another title); a later release's lookup of the same season
        // does not move it (one Plex missed would trade Plex's guid for a minted one).
        let identified = named(built.get("guid"))
            && (!named(row.get("guid")) || tmdb_season(built.get("Guid")) != tmdb_season(row.get("Guid")));
        let mut changes = Vec::new();
        for (field, lock) in [
            ("guid", None),
            ("Guid", None),
            ("summary", Some("summary")),
            ("year", Some("year")),
            ("_poster_url", Some("thumb")),
            ("_art_url", Some("art")),
        ] {
            let Some(value) = built.get(field).filter(present) else {
                continue;
            };
            let artwork = matches!(field, "_poster_url" | "_art_url") && row.get(field).filter(present).is_none();
            if (identified || artwork)
                && !lock.is_some_and(|lock| super::edit::field_locked(&row, lock))
                && row.get(field) != Some(value)
            {
                changes.push((field, value.clone()));
            }
        }
        let show = row.get("parentRatingKey").map(string_value).and_then(|show| self.catalog.get(&show));
        for (field, from) in [("parentGuid", "guid"), ("parentTitle", "title")] {
            if let Some(value) = show.and_then(|show| show.get(from)).filter(present) {
                if row.get(field) != Some(value) {
                    changes.push((field, value.clone()));
                }
            }
        }
        if changes.is_empty() {
            return false;
        }
        let guid = changes.iter().find(|(field, _)| *field == "guid").map(|(_, guid)| guid.clone());
        if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            for (field, value) in changes {
                object.insert(field.to_string(), value);
            }
        }
        if let Some(guid) = guid {
            // An episode's `parentGuid` is its season's own guid (hierarchy.rs).
            let children: Vec<String> = self.catalog.children_refs(key).into_iter().filter_map(key_of).collect();
            for child in children {
                if let Some(object) = self.catalog.get_mut(&child).and_then(Value::as_object_mut) {
                    object.insert("parentGuid".to_string(), guid.clone());
                }
            }
        }
        self.dirty = true;
        if let Some(row) = self.catalog.get(key).cloned() {
            self.push_timeline_entry(&row, 5, None, None, None, None);
        }
        true
    }

    /// Artwork a show here lacks, from the show a later leaf builds: one named before TMDB
    /// had its poster (the production server, 2026-10-12: Camp CrunchLabs), or known only to Plex, whose own
    /// poster `name_release` falls back to. What a person picked or locked stays.
    pub(super) fn fill_artwork(&mut self, key: &str, built: &Value) -> bool {
        let Some(row) = self.catalog.get(key).and_then(Value::as_object).cloned() else {
            return false;
        };
        let present = |value: &&Value| !value.is_null() && value.as_str() != Some("");
        let changes: Vec<(&str, Value)> = [("_poster_url", "thumb"), ("_art_url", "art")]
            .into_iter()
            .filter(|(field, lock)| row.get(*field).filter(present).is_none() && !super::edit::field_locked(&row, lock))
            .filter_map(|(field, _)| Some((field, built.get(field).filter(present)?.clone())))
            .collect();
        if changes.is_empty() {
            return false;
        }
        if let Some(object) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            for (field, value) in changes {
                object.insert(field.to_string(), value);
            }
        }
        self.dirty = true;
        if let Some(row) = self.catalog.get(key).cloned() {
            self.push_timeline_entry(&row, 5, None, None, None, None);
        }
        true
    }

    /// The leaf row a new file becomes, on `key` or a minted one; `None` for a show's file
    /// with no episode.
    pub(super) fn leaf_for(&mut self, plan: &Library, arrival: &Arrival, key: Option<String>) -> Option<Value> {
        let file_name = arrival.found.file.rsplit('/').next().unwrap_or_default().to_string();
        let release = identify::parse_name(&arrival.release);
        let (season, episode) = if plan.kind == "show" {
            arrival.numbers.or_else(|| episode_of(&arrival.release, folder_of(&arrival.extra), &file_name))?
        } else {
            (0, 0)
        };
        let key = key.unwrap_or_else(|| self.mint_key());
        let rating = key.parse::<i64>().unwrap_or(0);
        let stamp = now();
        let extension = extension(&file_name);
        let part_id = rating * 10;
        let probe = arrival
            .entry
            .get("media_info")
            .filter(|probe| probe.is_object())
            .cloned();
        let size = arrival
            .entry
            .get("size")
            .and_then(Value::as_u64)
            .filter(|size| *size > 0)
            .unwrap_or(arrival.found.size);
        let mut media = mediainfo::build_media(
            probe.as_ref().unwrap_or(&json!({"format": {}, "streams": []})),
            rating * 100,
            part_id,
            &format!("/library/parts/{part_id}/{stamp}/file.{extension}"),
            &arrival.found.file,
            size as i64,
            Some(&extension),
        );
        if let Some(prefs) = &self.track_prefs {
            mediainfo::select_media_tracks(&mut media, prefs);
        }
        let title = if release.title.is_empty() { stem(&file_name).to_string() } else { release.title.clone() };
        let mut leaf = json!({
            "ratingKey": key,
            "key": format!("/library/metadata/{key}"),
            "guid": format!("local://{key}"),
            "type": plan.kind.clone(),
            "title": title,
            "summary": "",
            "duration": media.get("duration").cloned().unwrap_or(json!(0)),
            "addedAt": stamp,
            "updatedAt": stamp,
            "thumb": format!("/library/metadata/{key}/thumb/{stamp}"),
            "art": format!("/library/metadata/{key}/art/{stamp}"),
            "Media": [media],
            "_identity": format!("title:{}", hierarchy::fold_title(&release.title).to_lowercase()),
            "_release": arrival.release.clone(),
        });
        if let Some(year) = release.year {
            leaf["year"] = json!(year);
        }
        if probe.is_none() {
            leaf["_probePending"] = json!(true);
        }
        if let Some(hash) = arrival.entry.get("hash").and_then(Value::as_str) {
            leaf["_hash"] = json!(hash);
        }
        for (field, value) in &arrival.extra {
            leaf[field.as_str()] = value.clone();
        }
        if plan.kind == "show" {
            leaf["_kind"] = json!("tv");
            leaf["_tmdb_id"] = leaf["_identity"].clone();
            leaf["_season"] = json!(season);
            leaf["_episode"] = json!(episode);
            leaf["_show_title"] = json!(leaf["title"].clone());
            leaf["_show_summary"] = json!("");
            if let Some(year) = release.year {
                leaf["_show_year"] = json!(year);
            }
            // The local agent's episode: its generated title, marked as generated so an
            // index edit re-titles it (`unmatch_row`).
            leaf["title"] = json!(format!("Episode {episode}"));
            leaf["_generatedTitle"] = json!(true);
            leaf.as_object_mut()?.shift_remove("year");
            leaf["_release_path"] = json!(arrival.release_path.clone());
        }
        if let Some(meta) = &arrival.meta {
            for (field, value) in meta {
                leaf[field.as_str()] = value.clone();
            }
            // TMDB named the show but not this episode: `enrich` left the SHOW's title
            // on it, so it keeps its generated one.
            if plan.kind == "show" && !meta.contains_key("index") {
                leaf["title"] = json!(format!("Episode {episode}"));
            } else if let Some(object) = leaf.as_object_mut() {
                object.shift_remove("_generatedTitle");
            }
        }
        Some(leaf)
    }

    /// An episode's parent fields, read off the rows it now sits under.
    pub(super) fn inherit_parents(&mut self, key: &str) {
        let Some(episode) = self.catalog.get(key).cloned() else {
            return;
        };
        let season_key = episode.get("parentRatingKey").map(string_value).unwrap_or_default();
        let Some(season) = self.catalog.get(&season_key).cloned() else {
            return;
        };
        let show_key = season.get("parentRatingKey").map(string_value).unwrap_or_default();
        let Some(show) = self.catalog.get(&show_key).cloned() else {
            return;
        };
        let release_path = episode.get("_release_path").cloned();
        if let Some(row) = self.catalog.get_mut(key).and_then(Value::as_object_mut) {
            for (field, value) in [
                ("parentRatingKey", json!(season_key)),
                ("parentKey", json!(format!("/library/metadata/{season_key}"))),
                ("parentTitle", season.get("title").cloned().unwrap_or(Value::Null)),
                ("parentIndex", season.get("index").cloned().unwrap_or(Value::Null)),
                ("parentGuid", season.get("guid").cloned().unwrap_or(Value::Null)),
                ("parentThumb", season.get("thumb").cloned().unwrap_or(Value::Null)),
                ("grandparentRatingKey", json!(show_key)),
                ("grandparentKey", json!(format!("/library/metadata/{show_key}"))),
                ("grandparentTitle", show.get("title").cloned().unwrap_or(Value::Null)),
                ("grandparentGuid", show.get("guid").cloned().unwrap_or(Value::Null)),
                ("grandparentThumb", show.get("thumb").cloned().unwrap_or(Value::Null)),
                ("grandparentArt", show.get("art").cloned().unwrap_or(Value::Null)),
                ("grandparentSlug", show.get("slug").cloned().unwrap_or(Value::Null)),
            ] {
                if value.is_null() {
                    row.shift_remove(field);
                } else {
                    row.insert(field.to_string(), value);
                }
            }
            row.shift_remove("_release_path");
        }
        if let (Some(path), Some(show)) = (release_path, self.catalog.get_mut(&show_key)) {
            let locations = show
                .as_object_mut()
                .map(|object| object.entry("Location").or_insert_with(|| json!([])));
            if let Some(Value::Array(rows)) = locations {
                if !rows.iter().any(|row| row.get("path") == Some(&path)) {
                    rows.push(json!({"path": path}));
                }
            }
        }
    }

    /// Measured (4a/4b): a row a scan creates is announced `state 0` with an empty title,
    /// then `state 1` created, analysing and thumbnailing, then `state 1` again; an
    /// episode's first frame also names its season and show.
    pub(super) fn push_created_frames(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let parents = (row.get("type").and_then(Value::as_str) == Some("episode")).then(|| {
            (
                row.get("parentRatingKey").map(string_value).unwrap_or_default(),
                row.get("grandparentRatingKey").map(string_value).unwrap_or_default(),
            )
        });
        let episode = parents.is_some();
        self.push_timeline_entry_with(&row, 0, Some("created"), None, None, Some(""), parents);
        let media_states: &[Option<&str>] = if episode {
            &[None, Some("analyzing"), None]
        } else {
            &[None, Some("analyzing"), Some("thumbnailing"), None]
        };
        for media_state in media_states {
            self.push_timeline_entry(&row, 1, Some("created"), *media_state, None, None);
        }
    }

    /// Measured (4a): after the scan, a new row's metadata pass -- the `Updating
    /// Metadata` activity around processing, matching, queued and loading frames.
    pub(super) fn created_metadata_frames(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        if !matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode")) {
            return;
        }
        let mut activity = self.start_metadata_activity(&self.frame_title(&row));
        activity.insert("progress".to_string(), json!(49));
        self.notify_activity(&activity, "updated");
        self.push_timeline_entry(&row, 1, Some("processing"), None, None, None);
        self.push_timeline_entry(&row, 2, Some("matching"), None, None, None);
        self.push_timeline_entry(&row, 5, None, None, None, None);
        self.push_timeline_entry(&row, 3, Some("queued"), None, None, None);
        self.push_timeline_entry(&row, 4, Some("loading"), None, None, None);
        activity.insert("progress".to_string(), json!(100));
        self.notify_activity(&activity, "ended");
        self.push_timeline_entry(&row, 5, None, None, None, None);
    }

    /// Rows added before their file was probed get their Media from the probe, keeping
    /// their ids.
    pub(super) fn fill_probes(&mut self, probed: Vec<(String, Value)>) -> usize {
        let mut filled = 0;
        for (key, probe) in probed {
            let Some(row) = self.catalog.get(&key).cloned() else {
                continue;
            };
            let Some(old) = row.get("Media").and_then(Value::as_array).and_then(|media| media.first()).cloned() else {
                continue;
            };
            let part = old.get("Part").and_then(Value::as_array).and_then(|parts| parts.first()).cloned().unwrap_or_default();
            let file = part.get("file").and_then(Value::as_str).unwrap_or_default().to_string();
            let mut media = mediainfo::build_media(
                &probe,
                old.get("id").and_then(Value::as_i64).unwrap_or(0),
                part.get("id").and_then(Value::as_i64).unwrap_or(0),
                part.get("key").and_then(Value::as_str).unwrap_or_default(),
                &file,
                part.get("size").and_then(Value::as_i64).unwrap_or(0),
                Some(&extension(&file)),
            );
            if let Some(prefs) = &self.track_prefs {
                mediainfo::select_media_tracks(&mut media, prefs);
            }
            if let Some(object) = self.catalog.get_mut(&key).and_then(Value::as_object_mut) {
                object.insert("duration".to_string(), media.get("duration").cloned().unwrap_or(json!(0)));
                object.insert("Media".to_string(), json!([media]));
                object.shift_remove("_probePending");
            }
            if let Some(row) = self.catalog.get(&key).cloned() {
                self.push_timeline_entry(&row, 5, None, None, None, None);
            }
            filled += 1;
        }
        filled
    }

    /// A file the source no longer has: to the trash, or deleted at once under
    /// `autoEmptyTrash` (measured, 5a: one `state 9` frame and nothing else). Answers
    /// whether it was deleted rather than trashed; a row already in the trash is left.
    pub(super) fn remove_missing(&mut self, key: &str) -> Option<bool> {
        if self.catalog.get(key)?.get("deletedAt").is_some() {
            return None;
        }
        if self.auto_empty_trash() {
            self.purge_row(key);
            Some(true)
        } else {
            self.trash_row(key);
            Some(false)
        }
    }

    /// Measured (1, 2): a missing file soft-deletes its row -- `deletedAt` on the row and
    /// its Media (never the Part), and on an episode's season and show too, counts
    /// unchanged -- each announced as a `type -1` state 3 then state 5 pair.
    pub(super) fn trash_row(&mut self, key: &str) {
        let stamp = now();
        let mut chain = vec![key.to_string()];
        if let Some(row) = self.catalog.get(key) {
            if row.get("type").and_then(Value::as_str) == Some("episode") {
                chain.extend(row.get("parentRatingKey").map(string_value));
                chain.extend(row.get("grandparentRatingKey").map(string_value));
            }
        }
        for (index, member) in chain.iter().enumerate() {
            let Some(object) = self.catalog.get_mut(member).and_then(Value::as_object_mut) else {
                continue;
            };
            object.insert("deletedAt".to_string(), json!(stamp));
            if index == 0 {
                for media in object.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
                    if let Some(media) = media.as_object_mut() {
                        media.insert("deletedAt".to_string(), json!(stamp));
                    }
                }
            }
            self.deletion_pair(member);
        }
        info!("FEED trashed {key}");
    }

    /// Measured (6a2): the file back before the trash was emptied restores the SAME row
    /// -- key, Media and guid -- with the same `type -1` pair. Its season and show leave
    /// the trash once none of their episodes is in it (inferred: not measured).
    pub(super) fn untrash_row(&mut self, key: &str) {
        let mut chain = vec![key.to_string()];
        if let Some(row) = self.catalog.get(key) {
            if row.get("type").and_then(Value::as_str) == Some("episode") {
                chain.extend(row.get("parentRatingKey").map(string_value));
                chain.extend(row.get("grandparentRatingKey").map(string_value));
            }
        }
        for (index, member) in chain.iter().enumerate() {
            if index > 0 && self.leaves(member).iter().any(|leaf| leaf.get("deletedAt").is_some() && key_of(leaf).as_deref() != Some(key)) {
                continue;
            }
            let Some(object) = self.catalog.get_mut(member).and_then(Value::as_object_mut) else {
                continue;
            };
            if object.shift_remove("deletedAt").is_none() {
                continue;
            }
            for media in object.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
                if let Some(media) = media.as_object_mut() {
                    media.shift_remove("deletedAt");
                }
            }
            self.deletion_pair(member);
        }
        info!("FEED restored {key}");
    }

    fn deletion_pair(&mut self, key: &str) {
        if let Some(row) = self.catalog.get(key).cloned() {
            self.push_timeline_entry(&row, 3, None, None, Some(-1), None);
            self.push_timeline_entry(&row, 5, None, None, Some(-1), None);
        }
    }

    /// A trashed leaf deleted for good: the row goes (and an episode's season with its
    /// last episode, as a DELETE takes it; a show with its last season, inferred), its key
    /// retired, announced as the row's own type in `state 9`.
    pub(super) fn purge_row(&mut self, key: &str) {
        let Some(row) = self.catalog.get(key).cloned() else {
            return;
        };
        let parents = (row.get("type").and_then(Value::as_str) == Some("episode")).then(|| {
            (
                row.get("parentRatingKey").map(string_value).unwrap_or_default(),
                row.get("grandparentRatingKey").map(string_value).unwrap_or_default(),
            )
        });
        let mut doomed = vec![key.to_string()];
        if let Some((season, show)) = &parents {
            if self.children(season).len() <= 1 {
                doomed.push(season.clone());
                if self.children(show).len() <= 1 {
                    doomed.push(show.clone());
                }
            }
        }
        for gone in &doomed {
            if self.catalog.remove(gone).is_some() {
                self.deleted.insert(gone.clone());
                if let Ok(number) = gone.parse::<i64>() {
                    self.retired_keys.push(number);
                }
            }
        }
        self.drop_from_queues(&doomed);
        self.push_timeline_entry_with(&row, 9, Some("deleted"), None, None, None, parents);
        info!("FEED deleted {key}");
    }

    /// `PUT /library/sections/{id}/emptyTrash`: every trashed leaf deleted. Measured
    /// (3a/3b): a show's season and show leave the trash first (their `type -1` pairs),
    /// then each episode's `state 9` names its season and show.
    pub(super) fn empty_section_trash(&mut self, section: &str) -> usize {
        let trashed: Vec<Value> = self
            .catalog
            .items()
            .iter()
            .filter(|row| {
                string_value(row.get("librarySectionID").unwrap_or(&Value::Null)) == section
                    && matches!(row.get("type").and_then(Value::as_str), Some("movie" | "episode"))
                    && row.get("deletedAt").is_some()
            })
            .cloned()
            .collect();
        for row in &trashed {
            for parent in ["parentRatingKey", "grandparentRatingKey"] {
                let Some(key) = row.get(parent).map(string_value) else {
                    continue;
                };
                let cleared = self
                    .catalog
                    .get_mut(&key)
                    .and_then(Value::as_object_mut)
                    .and_then(|object| object.shift_remove("deletedAt"))
                    .is_some();
                if cleared {
                    self.deletion_pair(&key);
                }
            }
        }
        for row in &trashed {
            if let Some(key) = key_of(row) {
                self.purge_row(&key);
            }
        }
        if !trashed.is_empty() {
            self.dirty = true;
        }
        trashed.len()
    }
}

/// Every file a row's Parts read.
pub(super) fn row_files(row: &Value) -> Vec<String> {
    row.get("Media")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .flat_map(|media| media.get("Part").and_then(Value::as_array).into_iter().flatten())
        .filter_map(|part| part.get("file").and_then(Value::as_str).map(str::to_string))
        .collect()
}

/// `build`'s temporary show and season keys replaced, in the key fields and in every
/// `/library/metadata/<key>` path built from them.
fn rekey(rows: &mut [Value], keys: &HashMap<String, String>) {
    for row in rows {
        let Some(object) = row.as_object_mut() else {
            continue;
        };
        for field in ["ratingKey", "parentRatingKey", "grandparentRatingKey"] {
            if let Some(new) = object.get(field).map(string_value).and_then(|old| keys.get(&old)) {
                object.insert(field.to_string(), json!(new));
            }
        }
        for value in object.values_mut() {
            let Value::String(text) = value else {
                continue;
            };
            let Some(rest) = text.strip_prefix("/library/metadata/") else {
                continue;
            };
            let (head, tail) = rest.split_once('/').unwrap_or((rest, ""));
            if let Some(new) = keys.get(head) {
                *text = if tail.is_empty() {
                    format!("/library/metadata/{new}")
                } else {
                    format!("/library/metadata/{new}/{tail}")
                };
            }
        }
    }
}

/// The load-time corrections every catalog row gets (`Runtime::load`), for rows a scan
/// adds: tag ids, no show-level fields on an episode, no stored file-check pair.
fn prepare_rows(rows: &mut [Value]) {
    number_catalog_tags(rows);
    for row in rows.iter_mut() {
        let episode = row.get("type").and_then(Value::as_str) == Some("episode");
        let Some(object) = row.as_object_mut() else {
            continue;
        };
        if episode {
            for field in ["Genre", "Country", "studio", "tagline", "originalTitle", "rating", "Label"] {
                object.shift_remove(field);
            }
        }
        for media in object.get_mut("Media").and_then(Value::as_array_mut).into_iter().flatten() {
            for part in media.get_mut("Part").and_then(Value::as_array_mut).into_iter().flatten() {
                if let Some(part) = part.as_object_mut() {
                    part.shift_remove("accessible");
                    part.shift_remove("exists");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `tests/fixtures/identify/unplaced_episodes.json`: files the production rex's feed
    /// dropped as "no episode number".
    /// MythBusters' seasons as TMDB lists them (tv/1428, 2026-10-13): numbered 1-16, each
    /// aired in one year from 2003; the production server's files number them by year (`2004x01`).
    struct MythBusters;
    impl TmdbSource for MythBusters {
        fn get(&self, path: &str, _params: &[(&str, String)]) -> Option<Value> {
            (path == "tv/1428").then(|| {
                let mut seasons = vec![json!({"season_number": 0, "episode_count": 25, "air_date": "2004-01-11"})];
                for (number, (count, year)) in [(11, 2003), (17, 2004), (20, 2005), (28, 2006), (25, 2007), (19, 2008), (23, 2009),
                    (23, 2010), (21, 2011), (20, 2012), (11, 2013), (15, 2014), (14, 2015), (11, 2016), (6, 2017), (8, 2018)]
                    .into_iter()
                    .enumerate()
                {
                    seasons.push(json!({"season_number": number + 1, "episode_count": count, "air_date": format!("{year}-01-01")}));
                }
                json!({"id": 1428, "seasons": seasons})
            })
        }
    }

    /// `BLEACH.Thousand-Year.Blood.War.S04E01`, identified as Bleach (TMDB files the sequel series under
    /// it), is no S4 of Bleach on IMDb's numbering: that is the original series' Bount arc.
    #[test]
    fn a_release_named_as_the_show_and_a_subtitle_is_not_looked_up_on_imdb() {
        let tmdb: &dyn TmdbSource = &MythBusters;
        assert_eq!(by_imdb(tmdb, 30984, "tt0434665", "BLEACH Thousand-Year Blood War", "Bleach", (4, 1)), None);
    }

    #[test]
    fn a_year_numbered_season_is_tmdbs_season_of_that_year() {
        let tmdb: Option<&dyn TmdbSource> = Some(&MythBusters);
        assert_eq!(on_tmdb(tmdb, 1428, (2004, 1)), (2, 1), "MythBusters.2004x01");
        assert_eq!(on_tmdb(tmdb, 1428, (2003, 11)), (1, 11), "MythBusters.2003x11");
        assert_eq!(on_tmdb(tmdb, 1428, (2018, 8)), (16, 8), "MythBusters.S2018E08");
        assert_eq!(on_tmdb(tmdb, 1428, (2003, 12)), (2003, 12), "past the season's episodes: left as named");
        assert_eq!(on_tmdb(tmdb, 1428, (5, 3)), (5, 3), "a season TMDB has is never moved");
    }

    #[test]
    fn episode_of_reads_the_names_the_feed_dropped() {
        let record: Value =
            serde_json::from_str(include_str!("../../../tests/fixtures/identify/unplaced_episodes.json")).unwrap();
        let mut wrong = Vec::new();
        for case in record["files"].as_array().unwrap() {
            let got = episode_of(case["release"].as_str().unwrap(), case["folder"].as_str(), case["file"].as_str().unwrap());
            let want = case["episode"].as_i64().map(|episode| (case["season"].as_i64().unwrap(), episode));
            if got != want {
                wrong.push(format!("{}: got {got:?}, want {want:?}", case["file"]));
            }
        }
        assert!(wrong.is_empty(), "{wrong:#?}");
    }

    /// One Piece's TMDB seasons as `tests/fixtures/feed/blank_1016.json` recorded them
    /// (2026-10-16): an absolute episode is counted through them, the first season's own
    /// numbers stay, and a number past TMDB's last episode is none.
    #[test]
    fn an_absolute_episode_is_counted_through_tmdbs_seasons() {
        let record: Value = serde_json::from_str(include_str!("../../../tests/fixtures/feed/blank_1016.json")).unwrap();
        let details = record["tmdb"]
            .as_object()
            .unwrap()
            .iter()
            .find(|(key, _)| key.contains("/tv/37854?"))
            .map(|(_, details)| details.clone())
            .unwrap();
        let mut seasons = tmdb_seasons(&details);
        assert_eq!(counted_through(&seasons, 1106), Some((22, 18)), "a show TMDB numbers from 1 each season");
        // One Piece's own: each season numbered on from the show's first episode.
        let mut first = 1;
        for season in seasons.iter_mut().filter(|season| season.0 > 0) {
            season.3 = first;
            first += season.1;
        }
        assert_eq!(counted_through(&seasons, 1106), Some((22, 1106)), "[HatSubs] One Piece 1106");
        assert_eq!(counted_through(&seasons, 61), Some((1, 61)), "East Blue's last");
        assert_eq!(counted_through(&seasons, 62), Some((2, 62)));
        let total: i64 = seasons.iter().filter(|row| row.0 > 0).map(|row| row.1).sum();
        assert_eq!(counted_through(&seasons, total + 1), None);
    }

    /// TMDB's season lists as `tests/fixtures/identify/segment_titles.json` recorded them.
    struct Seasons(Value);
    impl TmdbSource for Seasons {
        fn get(&self, path: &str, _params: &[(&str, String)]) -> Option<Value> {
            let key = path.strip_prefix("tv/")?.replace("/season/", "/");
            Some(json!({"episodes": self.0["seasons"].get(&key)?.clone()}))
        }
    }

    /// `tests/fixtures/identify/segment_titles.json`: Courage's and Phineas and Ferb's packs,
    /// numbered by the half hour, on TMDB's segments by their titles; SNL's as they were.
    #[test]
    fn by_title_places_a_pack_on_the_tmdb_episode_its_title_names() {
        let record: Value =
            serde_json::from_str(include_str!("../../../tests/fixtures/identify/segment_titles.json")).unwrap();
        let tmdb = Seasons(record.clone());
        let mut wrong = Vec::new();
        for case in record["files"].as_array().unwrap() {
            let (release, folder, file) = (case["release"].as_str().unwrap(), case["folder"].as_str(), case["file"].as_str().unwrap());
            let numbers = episode_of(release, folder, file).unwrap();
            let got = by_title(Some(&tmdb), case["show"].as_i64().unwrap(), release, file, numbers);
            let want = (case["season"].as_i64().unwrap(), case["episode"].as_i64().unwrap());
            if got != want {
                wrong.push(format!("{file}: got {got:?}, want {want:?}"));
            }
        }
        assert!(wrong.is_empty(), "{} wrong: {wrong:#?}", wrong.len());
        // A scene release's own numbers stand, whatever its title says.
        assert_eq!(
            by_title(Some(&tmdb), 2085, "Courage.the.Cowardly.Dog.S02.1080p", "Courage.the.Cowardly.Dog.S02E14.The.Magic.Tree.of.Nowhere.mkv", (2, 14)),
            (2, 14)
        );
    }

    /// `tests/fixtures/identify/pack_folders.json`: every file of eight packs on fun, Smallville's
    /// and Saturday Night Live's among them, under the folder its provider lists it in.
    #[test]
    fn episode_of_reads_a_packs_season_folders() {
        let record: Value =
            serde_json::from_str(include_str!("../../../tests/fixtures/identify/pack_folders.json")).unwrap();
        let mut wrong = Vec::new();
        for case in record["files"].as_array().unwrap() {
            let (release, folder, file) = (case["release"].as_str().unwrap(), case["folder"].as_str(), case["file"].as_str().unwrap());
            let got = episode_of(release, folder, file);
            let want = case["episode"].as_i64().map(|episode| (case["season"].as_i64().unwrap(), episode));
            if got != want {
                wrong.push(format!("{folder:?}/{file}: got {got:?}, want {want:?}"));
            }
        }
        assert!(wrong.is_empty(), "{} wrong: {wrong:#?}", wrong.len());
    }

    fn options(dir: &Path, catalog: &Path) -> RuntimeOptions {
        RuntimeOptions {
            catalog: catalog.to_path_buf(),
            state: Some(dir.join("rex.db")),
            ..RuntimeOptions::default()
        }
    }

    fn seed(name: &str) -> Value {
        json!({"machineIdentifier": "0000000000000000000000000000000000000077",
               "friendlyName": name,
               "sections": [{"key": "1", "type": "movie", "title": "Movies",
                             "uuid": "00000000000000000000000000000000000000aa"}],
               "items": [{"ratingKey": "1001", "key": "/library/metadata/1001",
                          "type": "movie", "title": "Seeded", "librarySectionID": 1,
                          "guid": "plex://movie/000000000000000000000001",
                          "addedAt": 1700000000, "updatedAt": 1700000000}]})
    }

    fn scanned_row(key: &str) -> Value {
        json!({"ratingKey": key, "key": format!("/library/metadata/{key}"), "type": "movie",
               "title": "Scanned", "librarySectionID": 1, "_scanned": true,
               "guid": "plex://movie/000000000000000000000002",
               "addedAt": 1700000100, "updatedAt": 1700000100})
    }

    /// One test, because the store's write queue is process-wide.
    #[test]
    fn a_snapshot_after_one_held_change_does_not_reserialise_every_held_item() {
        // the production server, 2026-10-10: 28,859 held items, every one serialised, hashed
        // and compared on each save -- two seconds apart while any client polled -- under the
        // runtime lock every request waits on. One real held record stands in for all of
        // them.
        let fixture: Value =
            serde_json::from_str(include_str!("../../tests/fixtures/feed_held_record.json")).unwrap();
        let record = fixture["record"].clone();
        let dir = std::env::temp_dir().join(format!("rex-held-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let catalog = dir.join("catalog.json");
        fs::write(&catalog, seed("held").to_string()).unwrap();

        let mut runtime = Runtime::load(options(&dir, &catalog)).unwrap();
        for n in 0..28_859 {
            runtime.hold(format!("rd:held/{n}"), record.clone());
        }
        runtime.save_state().unwrap();
        let mut slowest = std::time::Duration::ZERO;
        for n in 0..10 {
            runtime.hold(format!("rd:new/{n}"), record.clone());
            runtime.release(&format!("rd:held/{n}"));
            let started = std::time::Instant::now();
            let snapshot = runtime.snapshot_state().unwrap();
            slowest = slowest.max(started.elapsed());
            write_snapshot(&snapshot.expect("a held change is a change")).unwrap();
        }
        // The unfixed pass took 1.2 s here (a debug build); the keyed one about 11 ms.
        assert!(slowest < std::time::Duration::from_millis(100), "a one-item snapshot took {slowest:?}");
        drop(runtime);

        let runtime = Runtime::load(options(&dir, &catalog)).unwrap();
        assert_eq!(runtime.feed_held.len(), 28_859);
        assert_eq!(runtime.feed_held.get("rd:new/9"), Some(&record));
        assert!(!runtime.feed_held.contains_key("rd:held/0"));
        drop(runtime);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_runtime_loaded_after_another_has_saved_keeps_its_own_writes() {
        // The save serials were per runtime and the written mark per process: a second
        // runtime's first saves counted as written and were dropped. The test suite runs
        // several runtimes in one process; this one failed whenever another saved first.
        let base = std::env::temp_dir().join(format!("rex-two-{}", std::process::id()));
        let _ = fs::remove_dir_all(&base);
        let (first_dir, second_dir) = (base.join("first"), base.join("second"));
        for dir in [&first_dir, &second_dir] {
            fs::create_dir_all(dir).unwrap();
            fs::write(dir.join("catalog.json"), seed("two").to_string()).unwrap();
        }
        let mut first = Runtime::load(options(&first_dir, &first_dir.join("catalog.json"))).unwrap();
        for _ in 0..3 {
            let key = first.mint_key();
            first.catalog.add(scanned_row(&key));
            first.save_state().unwrap();
        }
        drop(first);

        let mut second = Runtime::load(options(&second_dir, &second_dir.join("catalog.json"))).unwrap();
        let key = second.mint_key();
        second.catalog.add(scanned_row(&key));
        second.save_state().unwrap();
        drop(second);
        let second = Runtime::load(options(&second_dir, &second_dir.join("catalog.json"))).unwrap();
        assert!(second.catalog.get(&key).is_some(), "the second runtime's save was dropped");
        drop(second);
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn two_runtimes_saving_at_once_each_write_their_own_file() {
        // The save queue is the process's: one runtime's flush drained another's queued
        // delta into its own file and marked it done, so the other's flush wrote nothing.
        // The unit tests run runtimes on parallel threads; Windows CI lost a row this way.
        let base = std::env::temp_dir().join(format!("rex-both-{}", std::process::id()));
        let _ = fs::remove_dir_all(&base);
        let dirs = [base.join("a"), base.join("b")];
        for dir in &dirs {
            fs::create_dir_all(dir).unwrap();
            fs::write(dir.join("catalog.json"), seed("both").to_string()).unwrap();
        }
        let mut a = Runtime::load(options(&dirs[0], &dirs[0].join("catalog.json"))).unwrap();
        let mut b = Runtime::load(options(&dirs[1], &dirs[1].join("catalog.json"))).unwrap();
        let (key_a, key_b) = (a.mint_key(), b.mint_key());
        a.catalog.add(scanned_row(&key_a));
        b.catalog.add(scanned_row(&key_b));
        let saved_a = a.snapshot_state().unwrap().expect("a change");
        let saved_b = b.snapshot_state().unwrap().expect("a change");
        write_snapshot(&saved_a).unwrap();
        write_snapshot(&saved_b).unwrap();
        drop((a, b));
        let a = Runtime::load(options(&dirs[0], &dirs[0].join("catalog.json"))).unwrap();
        let b = Runtime::load(options(&dirs[1], &dirs[1].join("catalog.json"))).unwrap();
        assert!(a.catalog.get(&key_a).is_some(), "a's save is missing from a's file");
        assert!(b.catalog.get(&key_b).is_some(), "b's save is missing from b's file");
        drop((a, b));
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn scanned_rows_keep_their_keys_and_state_across_restarts_and_catalog_edits() {
        let dir = std::env::temp_dir().join(format!("rex-scan-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let catalog = dir.join("catalog.json");
        fs::write(&catalog, seed("one").to_string()).unwrap();

        let mut runtime = Runtime::load(options(&dir, &catalog)).unwrap();
        let first = runtime.mint_key();
        assert!(first.parse::<i64>().unwrap() >= SCAN_KEY_BASE);
        runtime.catalog.add(scanned_row(&first));
        let second = runtime.mint_key();
        runtime.catalog.add(scanned_row(&second));
        if let Some(row) = runtime.catalog.get_mut(&first).and_then(Value::as_object_mut) {
            row.insert("viewCount".to_string(), json!(2));
        }
        // A row removed by a scan: its key is never minted again.
        runtime.catalog.remove(&second);
        runtime.save_state().unwrap();
        drop(runtime);

        let mut runtime = Runtime::load(options(&dir, &catalog)).unwrap();
        let row = runtime.catalog.get(&first).expect("the scanned row survives a restart");
        assert_eq!(row["viewCount"], 2);
        assert!(runtime.catalog.get(&second).is_none());
        let third = runtime.mint_key();
        assert!(third.parse::<i64>().unwrap() > second.parse::<i64>().unwrap());
        runtime.save_state().unwrap();
        drop(runtime);

        // The catalog FILE changes (a new seeded row, a rename): a scanned library is
        // kept by row, not set aside as a rebuilt catalog's state is.
        let mut edited = seed("two");
        edited["items"]
            .as_array_mut()
            .unwrap()
            .push(json!({"ratingKey": "1002", "type": "movie", "title": "Also seeded",
                         "librarySectionID": 1, "addedAt": 1700000200}));
        fs::write(&catalog, edited.to_string()).unwrap();
        let runtime = Runtime::load(options(&dir, &catalog)).unwrap();
        assert_eq!(runtime.catalog.get(&first).unwrap()["viewCount"], 2);
        assert!(runtime.catalog.get("1001").is_some());
        assert!(runtime.catalog.get("1002").is_some(), "the file's new row joins");
        assert!(!dir.join("rex.db.stale").exists());
        drop(runtime);
        let _ = fs::remove_dir_all(&dir);
    }

    /// The identification pass on zurg's two real entries (`fixtures/zurg/entries.json`),
    /// offline: TMDB's and Plex's answers sit in their on-disk caches, which is where a
    /// live lookup would have put them. The answers are written for the test.
    #[test]
    fn an_imdb_id_from_zurg_names_the_release_and_plex_names_its_guids() {
        let dir = std::env::temp_dir().join(format!("rex-name-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let api = "https://api.themoviedb.org/3";
        let details = "append_to_response=credits%2Cexternal_ids%2Ccontent_ratings%2Crelease_dates";
        let tmdb_cache = json!({
            format!("{api}/find/tt0103827?external_source=imdb_id&api_key=KEY"):
                {"movie_results": [{"id": 31639}], "tv_results": []},
            format!("{api}/movie/31639?{details}&api_key=KEY"):
                {"title": "Blame It on the Bellboy", "release_date": "1992-03-06",
                 "overview": "A hotel mix-up.", "external_ids": {"imdb_id": "tt0103827"}},
            format!("{api}/find/tt0397306?external_source=imdb_id&api_key=KEY"):
                {"movie_results": [], "tv_results": [{"id": 1433}]},
            format!("{api}/tv/1433?{details}&api_key=KEY"):
                {"name": "American Dad!", "first_air_date": "2005-02-06",
                 "external_ids": {"imdb_id": "tt0397306"}},
            format!("{api}/tv/1433/season/5?api_key=KEY"): {"id": 3626, "air_date": "2009-09-27"},
            format!("{api}/tv/1433/season/5/episode/7?append_to_response=external_ids&api_key=KEY"):
                {"id": 512000, "name": "Phantom of the Telethon", "air_date": "2009-11-15",
                 "external_ids": {"imdb_id": "tt1508003"}},
        });
        fs::write(dir.join("tmdb_cache.json"), tmdb_cache.to_string()).unwrap();
        let plex_cache = json!({
            r#"POST /library/metadata/matches {"guid": "tmdb://31639", "type": 1}"#:
                {"MediaContainer": {"Metadata": [{"guid": "plex://movie/5d7768279ab5440020ad6e0e"}]}},
            r#"POST /library/metadata/matches {"guid": "tmdb://1433", "type": 2}"#:
                {"MediaContainer": {"Metadata": [{"guid": "plex://show/5d9c086c46115600200aa2fe"}]}},
            "GET /library/metadata/5d9c086c46115600200aa2fe/children ":
                {"MediaContainer": {"Metadata": [{"index": 5, "guid": "plex://season/602e67d31d3358002c411c37"}]}},
            "GET /library/metadata/602e67d31d3358002c411c37/children ":
                {"MediaContainer": {"Metadata": [{"index": 7, "guid": "plex://episode/5d9c0ab4705e7a001e6d4a2b"}]}},
        });
        fs::write(dir.join("plex_cache.json"), plex_cache.to_string()).unwrap();
        let entries: Value = serde_json::from_str(
            // The build's own path, else the tree the test runs in: a test binary built in a
            // container and run on another machine (Windows from a cross build) has only the latter.
            &fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../tests/fixtures/zurg/entries.json"))
                .or_else(|_| fs::read_to_string("../tests/fixtures/zurg/entries.json"))
                .unwrap(),
        )
        .unwrap();
        let arrival = |kind: &str| {
            let fixture = &entries["entries"][kind];
            let release = fixture["release"].as_str().unwrap().to_string();
            let file = format!("/zurg/x/{release}/{}", fixture["file"].as_str().unwrap());
            Arrival {
                found: Found { file, size: 1 },
                release: release.clone(),
                release_path: format!("/zurg/x/{release}"),
                entry: fixture["entry"].clone(),
                extra: Map::new(),
                meta: None,
                numbers: None,
            }
        };
        let mut claim = plextv::Claim::load(&"f".repeat(40), dir.join("claim_state.json"), "x", VERSION);
        claim.token = Some("token".to_string());
        let claim = Arc::new(Mutex::new(claim));
        let plan = |kind: &str| Library {
            key: "1".to_string(),
            kind: kind.to_string(),
            title: "x".to_string(),
            tmdb: Some(Arc::new(tmdb::Tmdb::new("testkey".to_string(), dir.join("tmdb_cache.json")))),
            plex: Some(Arc::new(plexmeta::PlexMeta::new(Arc::clone(&claim), dir.join("plex_cache.json"), "f".repeat(40)))),
        };
        let mut movie = vec![arrival("movie")];
        name_release(&plan("movie"), &mut movie, 1);
        let meta = movie[0].meta.clone().unwrap();
        assert_eq!(meta["title"], "Blame It on the Bellboy");
        assert_eq!(meta["year"], 1992);
        assert_eq!(meta["guid"], "plex://movie/5d7768279ab5440020ad6e0e");
        assert_eq!(meta["Guid"], json!([{"id": "imdb://tt0103827"}, {"id": "tmdb://31639"}]));
        assert_eq!(meta["_identify"]["confidence"], "exact");
        let mut episode = vec![arrival("episode")];
        name_release(&plan("show"), &mut episode, 1);
        let meta = episode[0].meta.clone().unwrap();
        assert_eq!(meta["title"], "Phantom of the Telethon");
        assert_eq!(meta["_show_title"], "American Dad!");
        assert_eq!(meta["_show_guid"], "plex://show/5d9c086c46115600200aa2fe");
        assert_eq!(meta["_season_guid"], "plex://season/602e67d31d3358002c411c37");
        assert_eq!(meta["guid"], "plex://episode/5d9c0ab4705e7a001e6d4a2b");
        assert_eq!((meta["_season"].clone(), meta["_episode"].clone()), (json!(5), json!(7)));
        // A movie library's release that is a show is not forced into a movie.
        let mut wrong = vec![arrival("episode")];
        name_release(&plan("movie"), &mut wrong, 1);
        assert!(!wrong[0].meta.clone().unwrap().contains_key("guid"));
        let _ = fs::remove_dir_all(&dir);
    }

    /// Black Mirror's Season 6 as the production rex held it, refreshed by the seasons leaves
    /// build.
    #[test]
    fn a_season_takes_an_identity_once_and_moves_only_to_another_tmdb_season() {
        let fixture: Value =
            serde_json::from_str(include_str!("../../../tests/fixtures/feed/season_art.json")).unwrap();
        let dir = std::env::temp_dir().join(format!("rex-season-art-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let catalog = dir.join("catalog.json");
        let document = json!({"machineIdentifier": "0000000000000000000000000000000000000078", "friendlyName": "seasons",
                              "sections": [{"key": "2", "type": "show", "title": "TV Shows",
                                            "uuid": "00000000000000000000000000000000000000ab"}],
                              "items": fixture["rows"]});
        fs::write(&catalog, document.to_string()).unwrap();
        let mut runtime = Runtime::load(RuntimeOptions { catalog, state: None, ..RuntimeOptions::default() }).unwrap();
        let built = |guid: &str, tmdb: i64, poster: &str| {
            json!({"type": "season", "guid": guid, "Guid": [{"id": format!("tmdb://{tmdb}")}],
                   "summary": "Twisted tales", "_poster_url": poster})
        };
        let field = |runtime: &Runtime, key: &str, name: &str| runtime.catalog.get(key).unwrap()[name].clone();
        let plex = "plex://season/640b5417e96cd974790c094f";
        let poster = "https://image.tmdb.org/t/p/w342/kwATNh8JC1IJclnVwk8x7wZb2b4.jpg";

        assert!(runtime.refresh_season("112196", &built(plex, 331809, poster)));
        assert_eq!(field(&runtime, "112196", "guid"), plex);
        assert_eq!(field(&runtime, "112196", "_poster_url"), poster);
        assert_eq!(field(&runtime, "112196", "parentGuid"), "plex://show/5d9c0863ef619b002047f234");
        assert_eq!(field(&runtime, "112158", "parentGuid"), plex);
        // The same TMDB season looked up again without Plex: a minted guid moves nothing.
        assert!(!runtime.refresh_season("112196", &built("plex://season/0123456789abcdef01234567", 331809, poster)));
        // A season that is not identified gives only artwork a season lacks.
        assert!(!runtime.refresh_season("112196", &built("local://112196", 331809, "https://image.tmdb.org/t/p/w342/x.jpg")));
        // Another TMDB season (the show re-identified as another title) replaces it, but not
        // a poster a person locked.
        if let Some(row) = runtime.catalog.get_mut("112196").and_then(Value::as_object_mut) {
            row.insert("Field".to_string(), json!([{"locked": true, "name": "thumb"}]));
        }
        assert!(runtime.refresh_season("112196", &built("plex://season/76543210fedcba9876543210", 4242, "https://image.tmdb.org/t/p/w342/y.jpg")));
        assert_eq!(field(&runtime, "112196", "guid"), "plex://season/76543210fedcba9876543210");
        assert_eq!(field(&runtime, "112196", "_poster_url"), poster);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn samples_and_folders() {
        assert!(is_sample("/x/Film.2020.Sample.mkv") && !is_sample("/x/Samples.of.Life.mkv"));
        assert!(under("/mnt/zurg/movies/A/b.mkv", "/mnt/zurg/movies/") && !under("/mnt/zurg/moviesX/b.mkv", "/mnt/zurg/movies"));
    }
}
