//! The on-disk answer cache behind `Tmdb` and `PlexMeta`: one SQLite row per answer.
//!
//! Both caches used to live in memory as one JSON object, rewritten whole to
//! `tmdb_cache.json` / `plex_cache.json` on every miss. Measured on fun (2026-10-10, 10,056
//! TMDB and 2,458 Plex answers): 230 MB serialized per identified release, about every
//! 1.3 s while zurg's feed filled the library -- one core pinned in `serde_json`, 100 MB/s
//! of disk writes and a 3-4 GB heap. A row per answer makes a miss cost its own bytes.
//!
//! The database sits beside the JSON file it replaces (`tmdb_cache.db` for
//! `tmdb_cache.json`). A legacy JSON cache is imported once, when the database does not
//! exist yet, and left in place; the Python oracle still reads and writes the JSON file,
//! so the two servers no longer share answers learned after the import.

use super::logging::{info, warning};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{Map, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub(super) struct DiskCache {
    path: PathBuf,
    connection: Option<Mutex<Connection>>,
}

impl DiskCache {
    /// The cache that replaces `json_path`. A database that cannot be opened leaves the
    /// cache empty and every lookup a miss, as an unreadable JSON cache did.
    pub(super) fn open(json_path: &Path) -> Self {
        let path = json_path.with_extension("db");
        let fresh = !path.exists();
        let connection = Connection::open(&path)
            .and_then(|connection| {
                // A lost answer is fetched again, so a crash may drop the last commits.
                connection.execute_batch(
                    "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=10000;
                     CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
                )?;
                Ok(connection)
            })
            .ok();
        let cache = Self { path, connection: connection.map(Mutex::new) };
        if fresh {
            cache.import(json_path);
        }
        cache
    }

    fn import(&self, json_path: &Path) {
        let Some(legacy) = fs::read(json_path)
            .ok()
            .and_then(|raw| serde_json::from_slice::<Map<String, Value>>(&raw).ok())
        else {
            return;
        };
        let Some(connection) = &self.connection else { return };
        let Ok(mut connection) = connection.lock() else { return };
        let imported = connection.transaction().and_then(|transaction| {
            {
                let mut insert =
                    transaction.prepare("INSERT OR REPLACE INTO cache (key, value) VALUES (?1, ?2)")?;
                for (key, value) in &legacy {
                    insert.execute(params![key, value.to_string()])?;
                }
            }
            transaction.commit()
        });
        match imported {
            Ok(()) => info!("imported {} cached answer(s) from {}", legacy.len(), json_path.display()),
            Err(error) => warning!("{}: import of {} failed: {error}", self.path.display(), json_path.display()),
        }
    }

    pub(super) fn get(&self, key: &str) -> Option<Value> {
        let connection = self.connection.as_ref()?.lock().ok()?;
        let text: Option<String> = connection
            .query_row("SELECT value FROM cache WHERE key = ?1", params![key], |row| row.get(0))
            .optional()
            .ok()?;
        serde_json::from_str(&text?).ok()
    }

    pub(super) fn put(&self, key: &str, value: &Value) {
        let Some(connection) = &self.connection else { return };
        let Ok(connection) = connection.lock() else { return };
        if let Err(error) = connection.execute(
            "INSERT OR REPLACE INTO cache (key, value) VALUES (?1, ?2)",
            params![key, value.to_string()],
        ) {
            warning!("{}: {error}", self.path.display());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> (String, Value) {
        let raw = include_str!("../../tests/fixtures/tmdb_movie_details.json");
        let map: Map<String, Value> = serde_json::from_str(raw).unwrap();
        map.into_iter().next().unwrap()
    }

    #[test]
    fn a_legacy_json_cache_is_imported_once_and_answers_without_the_network() {
        let dir = tempfile_dir("import");
        let (key, value) = fixture();
        fs::write(dir.join("tmdb_cache.json"), json_object(&[(key.clone(), value.clone())])).unwrap();
        let cache = DiskCache::open(&dir.join("tmdb_cache.json"));
        assert_eq!(cache.get(&key), Some(value.clone()));
        assert_eq!(cache.get("missing"), None);
        drop(cache);
        // A second open must not re-import: the JSON file is no longer the cache.
        fs::write(dir.join("tmdb_cache.json"), "{}").unwrap();
        assert_eq!(DiskCache::open(&dir.join("tmdb_cache.json")).get(&key), Some(value));
    }

    #[test]
    fn an_answer_put_survives_a_reopen() {
        let dir = tempfile_dir("reopen");
        let (key, value) = fixture();
        DiskCache::open(&dir.join("plex_cache.json")).put(&key, &value);
        assert_eq!(DiskCache::open(&dir.join("plex_cache.json")).get(&key), Some(value));
    }

    fn json_object(entries: &[(String, Value)]) -> String {
        Value::Object(entries.iter().cloned().collect()).to_string()
    }

    pub(crate) fn tempfile_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("simplex-diskcache-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }
}
