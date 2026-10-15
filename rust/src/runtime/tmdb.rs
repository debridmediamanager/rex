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
    /// DMM's sponsor metadata API (`REX_DMM_API_KEY`): its base url and the key. Asked first;
    /// TMDB itself, with `key`, only when DMM does not answer.
    dmm: Option<(String, String)>,
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
            dmm: None,
            cache: DiskCache::open(&cache_path),
            cache_path,
        }
    }

    /// Reads through DMM first: `{base}/api/metadata/tmdb/{path}` answers TMDB's own JSON
    /// from DMM's cache, so a library's seasons cost one TMDB call across every DMM sponsor.
    pub(super) fn with_dmm(mut self, base: String, key: String) -> Self {
        self.dmm = Some((base.trim_end_matches('/').to_string(), key));
        self
    }

    /// DMM's answer: `Ok(Some)` TMDB's JSON, `Ok(None)` TMDB has no such title (its own 404,
    /// which carries TMDB's `status_code`), `Err` DMM did not answer for TMDB -- a lapsed key,
    /// its rate limit, a path it does not serve -- so TMDB is asked directly.
    fn get_dmm(base: &str, key: &str, path: &str, query: &str) -> Result<Option<Value>, ()> {
        let url = format!("{base}/api/metadata/tmdb/{path}{}{query}", if query.is_empty() { "" } else { "?" });
        match upstream::fetch("GET", &url, &[("x-api-key", key.to_string())], b"", Duration::from_secs(25)) {
            Ok((200..=299, _, body)) => Ok(serde_json::from_slice::<Value>(&body).ok()),
            Ok((404, _, body))
                if serde_json::from_slice::<Value>(&body).is_ok_and(|answer| answer.get("status_code").is_some()) =>
            {
                Ok(None)
            }
            _ => Err(()),
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
        let path = path.trim_start_matches('/');
        let mut query = params
            .iter()
            .map(|(name, value)| format!("{name}={}", upstream::quote_plus(value)))
            .collect::<Vec<_>>();
        let own = query.join("&");
        query.push(format!("api_key={}", upstream::quote_plus(&self.key)));
        let url = format!("{}/{path}?{}", self.api, query.join("&"));
        // Keyed by the real API's url, whatever `api` points at, and whoever answers it.
        let cache_key = format!("{API}/{path}?{}", query.join("&"))
            .replace(&format!("api_key={}", upstream::quote_plus(&self.key)), "api_key=KEY");
        let cached = self.cache.get(&cache_key);
        if let Some(hit) = cached.as_ref().filter(|hit| !expired(incomplete(path, hit), hit)) {
            return Some(hit.clone());
        }
        let from_dmm = self.dmm.as_ref().map(|(base, key)| Self::get_dmm(base, key, path, &own));
        let fetched = match from_dmm {
            Some(Ok(payload)) => payload,
            // Without a TMDB key of its own rex has no one else to ask.
            _ if self.key.is_empty() => None,
            _ => match Self::get_url(&url) {
                Ok(payload) => payload,
                // Rate limited: back off once, then give up.
                Err(429) => {
                    thread::sleep(Duration::from_secs(2));
                    Self::get_url(&url).ok().flatten()
                }
                Err(_) => None,
            },
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

    /// A server answering `status` and `body`, keeping every request line and `x-api-key`.
    fn answering(status: u16, body: String) -> (String, std::sync::Arc<Mutex<Vec<String>>>) {
        let seen = std::sync::Arc::new(Mutex::new(Vec::new()));
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let log = seen.clone();
        thread::spawn(move || {
            for mut stream in listener.incoming().flatten() {
                let mut request = [0u8; 8192];
                let read = stream.read(&mut request).unwrap_or(0);
                let text = String::from_utf8_lossy(&request[..read]).to_string();
                let key = text.lines().find_map(|line| line.strip_prefix("x-api-key: ")).unwrap_or("-");
                log.lock().unwrap().push(format!("{} {key}", text.lines().next().unwrap_or_default()));
                let _ = write!(
                    stream,
                    "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        (format!("http://{address}"), seen)
    }

    /// DMM's sponsor metadata API is asked first and TMDB only when DMM does not answer for
    /// it. Its answers are DMM's measured ones (2026-10-15, production): TMDB's JSON, TMDB's own
    /// 404 with `status_code`, 404 `{"error": ...}` for a read it does not serve, and 401 for a
    /// lapsed key.
    #[test]
    fn dmm_answers_first_and_tmdb_only_when_dmm_does_not() {
        let dir = std::env::temp_dir().join(format!("rex-tmdb-dmm-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let season = json!({"season_number": 1, "episodes": [{"episode_number": 1, "name": "Rollercoaster"}]});
        let (tmdb_url, tmdb_seen) = answering(200, json!({"season_number": 1, "episodes": []}).to_string());

        let (dmm_url, dmm_seen) = answering(200, season.to_string());
        let mut tmdb = Tmdb::new("testkey".to_string(), dir.join("a.json")).with_dmm(dmm_url, "sponsorkey".to_string());
        tmdb.api = tmdb_url.clone();
        assert_eq!(tmdb.season(&json!(1877), &json!(1)), Some(season.clone()));
        assert_eq!(*dmm_seen.lock().unwrap(), ["GET /api/metadata/tmdb/tv/1877/season/1 HTTP/1.1 sponsorkey"]);
        assert!(tmdb_seen.lock().unwrap().is_empty());
        // Kept under TMDB's own url, so a server that drops DMM keeps what it read.
        assert_eq!(
            tmdb.cache.get(&format!("{API}/tv/1877/season/1?api_key=KEY")),
            Some(season.clone())
        );

        // TMDB has no such show: DMM says so in TMDB's words, and TMDB is not asked again.
        let (dmm_url, _) = answering(404, json!({"status_code": 34, "success": false}).to_string());
        let mut tmdb = Tmdb::new("testkey".to_string(), dir.join("b.json")).with_dmm(dmm_url, "sponsorkey".to_string());
        tmdb.api = tmdb_url.clone();
        assert_eq!(tmdb.season(&json!(999999999), &json!(1)), None);
        assert!(tmdb_seen.lock().unwrap().is_empty());

        // A lapsed key, or a read DMM does not serve: TMDB directly, with rex's own key.
        for (status, body) in [(401, json!({"error": "Sponsorship is no longer active"})), (404, json!({"error": "Not a TMDB read this API serves"}))] {
            let (dmm_url, _) = answering(status, body.to_string());
            let mut tmdb = Tmdb::new("testkey".to_string(), dir.join(format!("c{status}.json"))).with_dmm(dmm_url, "sponsorkey".to_string());
            tmdb.api = tmdb_url.clone();
            assert!(tmdb.season(&json!(1877), &json!(1)).is_some(), "{status}");
        }
        assert_eq!(tmdb_seen.lock().unwrap().len(), 2);
        assert!(tmdb_seen.lock().unwrap()[0].starts_with("GET /tv/1877/season/1?api_key=testkey "));

        // DMM alone, no TMDB key: DMM's refusal is no answer, and TMDB is never asked keyless.
        let (dmm_url, _) = answering(429, json!({"error": "Rate limit exceeded"}).to_string());
        let mut tmdb = Tmdb::new(String::new(), dir.join("d.json")).with_dmm(dmm_url, "sponsorkey".to_string());
        tmdb.api = tmdb_url;
        assert_eq!(tmdb.season(&json!(1877), &json!(1)), None);
        assert_eq!(tmdb_seen.lock().unwrap().len(), 2);
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
