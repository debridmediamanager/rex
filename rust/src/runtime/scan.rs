//! The live library: rows a scan adds to the running server, on keys that never change.
//!
//! The catalog file used to be the whole library, loaded once at start. A library fed by
//! zurg changes while the server runs, and zurg keeps the ratingKey it matched each
//! release to (and drops the release's IMDb id when that key disappears), so a row's key
//! is minted once and kept across scans and restarts.

use super::*;

/// Where minted keys start: above the catalog's own ranges (movies and episodes 1000+,
/// shows 20000+, seasons 30000+), playlists (40000+) and collections (50000+).
const SCAN_KEY_BASE: i64 = 100_000;

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
}

#[cfg(test)]
mod tests {
    use super::*;

    fn options(dir: &Path, catalog: &Path) -> RuntimeOptions {
        RuntimeOptions {
            catalog: catalog.to_path_buf(),
            state: Some(dir.join("plexsim.db")),
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
    fn scanned_rows_keep_their_keys_and_state_across_restarts_and_catalog_edits() {
        let dir = std::env::temp_dir().join(format!("plexsim-scan-{}", std::process::id()));
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
        assert!(!dir.join("plexsim.db.stale").exists());
        drop(runtime);
        let _ = fs::remove_dir_all(&dir);
    }
}
