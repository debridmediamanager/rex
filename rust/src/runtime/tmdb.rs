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
/// How long an answer TMDB may complete later is kept before it is asked again.
pub(super) const INCOMPLETE_FOR: i64 = 86_400;

/// An answer TMDB may complete later: a search or an id lookup that found nothing, or a
/// title with no poster yet. Every answer used to be kept for good, so the production rex drew Camp
/// CrunchLabs on the blank tile after TMDB had added its poster (2026-10-12), and a release
/// TMDB did not know on the day it arrived was never named.
fn incomplete(path: &str, answer: &Value) -> bool {
    let empty = |field: &str| answer.get(field).and_then(Value::as_array).is_none_or(Vec::is_empty);
    if path.starts_with("search/") {
        return empty("results");
    }
    if path.starts_with("find/") {
        return ["movie_results", "tv_results", "tv_episode_results", "tv_season_results"].into_iter().all(empty);
    }
    let segments: Vec<&str> = path.split('/').collect();
    matches!(segments.as_slice(), ["movie" | "tv", id] if id.bytes().all(|byte| byte.is_ascii_digit()))
        && answer.get("poster_path").is_none_or(Value::is_null)
}

/// A kept answer that is incomplete and older than `INCOMPLETE_FOR` (or kept before
/// answers were stamped).
pub(super) fn expired(incomplete: bool, answer: &Value) -> bool {
    incomplete && answer.get("_rexCachedAt").and_then(Value::as_i64).is_none_or(|at| now() - at >= INCOMPLETE_FOR)
}

/// Stamp an incomplete answer with when it was kept.
pub(super) fn stamped(incomplete: bool, mut answer: Value) -> Value {
    if let (true, Some(object)) = (incomplete, answer.as_object_mut()) {
        object.insert("_rexCachedAt".to_string(), json!(now()));
    }
    answer
}

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
        let cached = self.cache.get(&cache_key);
        if let Some(hit) = cached.as_ref().filter(|hit| !expired(incomplete(path, hit), hit)) {
            return Some(hit.clone());
        }
        let fetched = match Self::get_url(&url) {
            Ok(payload) => payload,
            // Rate limited: back off once, then give up.
            Err(429) => {
                thread::sleep(Duration::from_secs(2));
                Self::get_url(&url).ok().flatten()
            }
            Err(_) => None,
        };
        // TMDB unreachable: the answer it gave before, however incomplete.
        let Some(payload) = fetched else {
            return cached;
        };
        let payload = stamped(incomplete(path, &payload), payload);
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
    fn an_answer_without_a_poster_is_asked_again_a_day_later() {
        // the production rex, 2026-10-12: Camp CrunchLabs' details were kept with `poster_path: null`,
        // and its show drew the blank tile after TMDB had the poster.
        let dir = std::env::temp_dir().join(format!("rex-tmdb-incomplete-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let without = json!({"id": 328629, "name": "Camp CrunchLabs", "poster_path": null});
        let mut tmdb = Tmdb::new("testkey".to_string(), dir.join("tmdb_cache.json"));
        tmdb.api = stub(json!({"id": 328629, "name": "Camp CrunchLabs", "poster_path": "/3QYB.jpg"}).to_string());
        let key = |id: i64| {
            format!("{API}/tv/{id}?append_to_response=credits%2Cexternal_ids%2Ccontent_ratings%2Crelease_dates&api_key=KEY")
        };
        // Kept before answers were stamped, as the production rex's was: asked again now.
        tmdb.cache.put(&key(328629), &without);
        assert_eq!(tmdb.details("tv", &json!(328629)).unwrap()["poster_path"], "/3QYB.jpg");
        // Kept an hour ago: still the answer.
        tmdb.cache.put(&key(1), &stamped(true, without.clone()));
        assert!(tmdb.details("tv", &json!(1)).unwrap()["poster_path"].is_null());
        // Kept two days ago, and TMDB unreachable: the old answer, not nothing.
        let mut old = without.clone();
        old["_rexCachedAt"] = json!(now() - 2 * INCOMPLETE_FOR);
        tmdb.cache.put(&key(2), &old);
        tmdb.api = "http://127.0.0.1:9".to_string();
        assert_eq!(tmdb.details("tv", &json!(2)), Some(old));
        // A complete answer is never asked again.
        assert!(!expired(incomplete("tv/328629", &json!({"poster_path": "/x.jpg"})), &json!({})));
        assert!(incomplete("search/tv", &json!({"results": []})) && !incomplete("search/tv", &json!({"results": [{"id": 1}]})));
        assert!(incomplete("find/tt15679400", &json!({"movie_results": [], "tv_results": []})));
        assert!(!incomplete("tv/1/season/2", &json!({"poster_path": null})));
    }

    #[test]
    fn a_miss_writes_its_own_answer_not_the_whole_cache() {
        // the production server, 2026-10-10: 10,056 answers averaging 15 KB, and every miss
        // rewrote all of them. 1,500 copies of one real answer make a 24 MB cache.
        let raw = include_str!("../../tests/fixtures/tmdb_movie_details.json");
        let (key, value) = serde_json::from_str::<Map<String, Value>>(raw).unwrap().into_iter().next().unwrap();
        let legacy: Map<String, Value> = (0..1500)
            .map(|n| (key.replace("/movie/15173?", &format!("/movie/{}?", 900_000 + n)), value.clone()))
            .collect();
        let dir = std::env::temp_dir().join(format!("rex-tmdb-miss-{}", std::process::id()));
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
