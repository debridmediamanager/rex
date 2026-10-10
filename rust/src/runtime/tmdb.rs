//! `metadata.Tmdb`: the live agent behind Fix Match, present only with `TMDB_KEY`.
//!
//! Responses are cached on disk beside the catalog, keyed by the request url with the key
//! replaced by `KEY` -- the Python server's keys. The native server keeps them a row each
//! in `tmdb_cache.db` (diskcache.rs), importing the oracle's `tmdb_cache.json` once.

use super::diskcache::DiskCache;
use super::upstream;
use super::*;
use std::time::Duration;

const API: &str = "https://api.themoviedb.org/3";

pub(super) struct Tmdb {
    key: String,
    api: String,
    cache_path: PathBuf,
    cache: DiskCache,
}

impl std::fmt::Debug for Tmdb {
    // The key is an account credential; it never reaches a debug print.
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("Tmdb")
            .field("cache_path", &self.cache_path)
            .finish()
    }
}

/// `str()` of a JSON scalar the way Python formats the same value in a query string.
fn python_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        other => other.to_string(),
    }
}

impl Tmdb {
    pub(super) fn new(key: String, cache_path: PathBuf) -> Self {
        Self {
            key,
            api: API.to_string(),
            cache: DiskCache::open(&cache_path),
            cache_path,
        }
    }

    fn get_url(url: &str) -> Result<Option<Value>, u16> {
        match upstream::fetch("GET", url, &[], b"", Duration::from_secs(25)) {
            Ok((200..=299, _, body)) => Ok(serde_json::from_slice::<Value>(&body).ok()),
            Ok((status, _, _)) => Err(status),
            Err(_) => Err(0),
        }
    }

    /// `_get(path, **params)`: the params in the order given, then `api_key`.
    fn get(&self, path: &str, params: &[(&str, String)]) -> Option<Value> {
        let mut query = params
            .iter()
            .map(|(name, value)| format!("{name}={}", upstream::quote_plus(value)))
            .collect::<Vec<_>>();
        query.push(format!("api_key={}", upstream::quote_plus(&self.key)));
        let url = format!("{}/{}?{}", self.api, path.trim_start_matches('/'), query.join("&"));
        // Keyed by the real API's url, whatever `api` points at.
        let cache_key = format!("{API}{}", &url[self.api.len()..]).replace(&self.key, "KEY");
        if let Some(hit) = self.cache.get(&cache_key) {
            return Some(hit);
        }
        let payload = match Self::get_url(&url) {
            Ok(payload) => payload?,
            // Rate limited: back off once, then give up.
            Err(429) => {
                thread::sleep(Duration::from_secs(2));
                Self::get_url(&url).ok()??
            }
            Err(_) => return None,
        };
        self.cache.put(&cache_key, &payload);
        Some(payload)
    }

    /// Every candidate for a title, not just the first: Fix Match is the user choosing.
    pub(super) fn search(&self, kind: &str, title: &str, year: Option<&Value>) -> Vec<Value> {
        if title.is_empty() {
            return Vec::new();
        }
        let mut params = vec![("query", title.to_string())];
        if let Some(year) = year.filter(|year| truthy(year)) {
            if kind == "movie" {
                params.push(("year", python_str(year)));
            }
        }
        self.get(&format!("search/{kind}"), &params)
            .and_then(|found| found.get("results").and_then(Value::as_array).cloned())
            .unwrap_or_default()
    }

    pub(super) fn details(&self, kind: &str, id: &Value) -> Option<Value> {
        self.get(
            &format!("{kind}/{}", python_str(id)),
            &[(
                "append_to_response",
                "credits,external_ids,content_ratings,release_dates".to_string(),
            )],
        )
    }

    pub(super) fn season(&self, id: &Value, season: &Value) -> Option<Value> {
        self.get(
            &format!("tv/{}/season/{}", python_str(id), python_str(season)),
            &[],
        )
    }

    pub(super) fn episode(&self, id: &Value, season: &Value, episode: &Value) -> Option<Value> {
        self.get(
            &format!(
                "tv/{}/season/{}/episode/{}",
                python_str(id),
                python_str(season),
                python_str(episode)
            ),
            &[("append_to_response", "external_ids".to_string())],
        )
    }
}

/// What `identify.rs` reads TMDB through: `_get`, cached like every other lookup.
impl crate::identify::TmdbSource for Tmdb {
    fn get(&self, path: &str, params: &[(&str, String)]) -> Option<Value> {
        Tmdb::get(self, path, params)
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    /// Bytes this process has passed to write(2): `wchar` in /proc/self/io.
    fn written() -> u64 {
        let io = fs::read_to_string("/proc/self/io").unwrap();
        io.lines().find_map(|line| line.strip_prefix("wchar: ")).unwrap().trim().parse().unwrap()
    }

    /// TMDB answering `body` to every request.
    fn stub(body: String) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        thread::spawn(move || {
            for mut stream in listener.incoming().flatten() {
                let _ = stream.read(&mut [0u8; 8192]);
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        format!("http://{address}")
    }

    #[test]
    fn a_miss_writes_its_own_answer_not_the_whole_cache() {
        // fun, 2026-10-10: 10,056 answers averaging 15 KB, and every miss rewrote all of them.
        // 1,500 copies of one real answer make a 24 MB cache.
        let raw = include_str!("../../tests/fixtures/tmdb_movie_details.json");
        let (key, value) = serde_json::from_str::<Map<String, Value>>(raw).unwrap().into_iter().next().unwrap();
        let legacy: Map<String, Value> = (0..1500)
            .map(|n| (key.replace("/movie/15173?", &format!("/movie/{}?", 900_000 + n)), value.clone()))
            .collect();
        let dir = std::env::temp_dir().join(format!("simplex-tmdb-miss-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("tmdb_cache.json"), Value::Object(legacy).to_string()).unwrap();

        let mut tmdb = Tmdb::new("testkey".to_string(), dir.join("tmdb_cache.json"));
        tmdb.api = stub(value.to_string());
        let before = written();
        for id in 1..=5 {
            assert!(tmdb.details("movie", &json!(id)).is_some());
        }
        let bytes = written() - before;
        assert!(bytes < 4_000_000, "five misses wrote {bytes} bytes");

        // Still answered after a restart, with TMDB gone.
        let mut reopened = Tmdb::new("testkey".to_string(), dir.join("tmdb_cache.json"));
        reopened.api = "http://127.0.0.1:9".to_string();
        assert_eq!(reopened.details("movie", &json!(3)), Some(value));
    }
}
