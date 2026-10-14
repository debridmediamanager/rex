//! IMDb's episode numbering, to find the TMDB episode a file is when TMDB has no season by
//! the file's number. Release groups number long anime and reality shows as IMDb and TVDB do
//! (`Bungo.Stray.Dogs.S04E01`, `Detective.Conan.S33E01`); TMDB files many of them as one
//! long season, and Plex's metadata follows TMDB. IMDb's `title.episode.tsv.gz` (about ten
//! million episodes) names each episode's own IMDb id, and TMDB's `/find` answers which of its
//! episodes that id is. `REX_IMDB_EPISODES=on` keeps the file beside the catalog, fetched again
//! when a week old, and an indexed copy of it (`imdb-episodes.db`); without it nothing here is used.

use std::io::{BufRead, BufReader};
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, SystemTime};

use rusqlite::{params, Connection, OpenFlags};

use crate::runtime::{info, warning};

const URL: &str = "https://datasets.imdbws.com/title.episode.tsv.gz";
const DATASET: &str = "title.episode.tsv.gz";
const INDEX: &str = "imdb-episodes.db";
const FRESH: Duration = Duration::from_secs(7 * 24 * 3600);

static DB: Mutex<Option<Connection>> = Mutex::new(None);

fn age(path: &Path) -> Option<Duration> {
    let modified = std::fs::metadata(path).and_then(|meta| meta.modified()).ok()?;
    Some(SystemTime::now().duration_since(modified).unwrap_or_default())
}

fn number(tconst: &str) -> Option<i64> {
    tconst.strip_prefix("tt")?.parse().ok()
}

/// The dataset's rows as `(parent, season, episode, tconst)` numbers, in an indexed table.
pub fn build(dataset: &Path, out: &Path) -> Result<usize, String> {
    let partial = out.with_extension("part");
    let _ = std::fs::remove_file(&partial);
    let mut db = Connection::open(&partial).map_err(|e| e.to_string())?;
    db.execute_batch("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; CREATE TABLE e(parent INTEGER, season INTEGER, episode INTEGER, tconst INTEGER);")
        .map_err(|e| e.to_string())?;
    let file = std::fs::File::open(dataset).map_err(|e| e.to_string())?;
    let reader = BufReader::new(flate2::read::GzDecoder::new(file));
    let mut rows = 0;
    let transaction = db.transaction().map_err(|e| e.to_string())?;
    {
        let mut insert = transaction.prepare("INSERT INTO e VALUES (?1, ?2, ?3, ?4)").map_err(|e| e.to_string())?;
        for line in reader.lines() {
            let line = line.map_err(|e| e.to_string())?;
            let fields: Vec<&str> = line.split('\t').collect();
            let [tconst, parent, season, episode] = fields[..] else {
                continue;
            };
            let (Some(tconst), Some(parent), Ok(season), Ok(episode)) =
                (number(tconst), number(parent), season.parse::<i64>(), episode.parse::<i64>())
            else {
                continue;
            };
            insert.execute(params![parent, season, episode, tconst]).map_err(|e| e.to_string())?;
            rows += 1;
        }
    }
    transaction.commit().map_err(|e| e.to_string())?;
    db.execute_batch("CREATE INDEX i ON e(parent, season, episode);").map_err(|e| e.to_string())?;
    drop(db);
    std::fs::rename(&partial, out).map_err(|e| e.to_string())?;
    Ok(rows)
}

/// Fetch and index what is missing or stale beside the catalog, then open it. Answers whether
/// episodes can be looked up.
pub fn load(dir: &Path) -> bool {
    if std::env::var("REX_IMDB_EPISODES").map(|value| value != "on").unwrap_or(true) {
        return false;
    }
    let (dataset, index) = (dir.join(DATASET), dir.join(INDEX));
    if age(&dataset).is_none_or(|age| age > FRESH) {
        match fetch() {
            Ok(body) => {
                let partial = dataset.with_extension("part");
                if std::fs::write(&partial, &body).and_then(|_| std::fs::rename(&partial, &dataset)).is_err() {
                    warning!("IMDB could not keep {}", dataset.display());
                }
            }
            Err(error) => warning!("IMDB fetch {URL}: {error}"),
        }
    }
    let stale = match (age(&dataset), age(&index)) {
        (Some(dataset), Some(index)) => dataset < index,
        (Some(_), None) => true,
        (None, _) => false,
    };
    if stale {
        let began = std::time::Instant::now();
        match build(&dataset, &index) {
            Ok(rows) => info!("IMDB indexed {rows} episodes in {:.0} s", began.elapsed().as_secs_f64()),
            Err(error) => warning!("IMDB index {}: {error}", index.display()),
        }
    }
    match Connection::open_with_flags(&index, OpenFlags::SQLITE_OPEN_READ_ONLY) {
        Ok(db) => {
            if let Ok(mut slot) = DB.lock() {
                *slot = Some(db);
            }
            true
        }
        Err(error) => {
            warning!("IMDB {} unreadable: {error}; seasons TMDB lacks stay as named", index.display());
            false
        }
    }
}

/// Open an index for tests and the start (`load`).
pub fn open(index: &Path) -> bool {
    match Connection::open_with_flags(index, OpenFlags::SQLITE_OPEN_READ_ONLY) {
        Ok(db) => DB.lock().map(|mut slot| *slot = Some(db)).is_ok(),
        Err(_) => false,
    }
}

/// The IMDb id of `show`'s episode `episode` of season `season`, as IMDb numbers them.
pub fn episode(show: &str, season: i64, episode: i64) -> Option<String> {
    let parent = number(show)?;
    let guard = DB.lock().ok()?;
    let db = guard.as_ref()?;
    let tconst: i64 = db
        .query_row(
            "SELECT tconst FROM e WHERE parent = ?1 AND season = ?2 AND episode = ?3 LIMIT 1",
            params![parent, season, episode],
            |row| row.get(0),
        )
        .ok()?;
    Some(format!("tt{tconst:07}"))
}

fn fetch() -> Result<Vec<u8>, String> {
    let mut response =
        super::upstream::open("GET", URL, &[("User-Agent", "rex".to_string())], b"", Duration::from_secs(300))?;
    if response.status != 200 {
        return Err(format!("HTTP {}", response.status));
    }
    response.read_body(256 * 1024 * 1024)
}
