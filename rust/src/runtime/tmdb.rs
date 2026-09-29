//! `metadata.Tmdb`: the live agent behind Fix Match, present only with `TMDB_KEY`.
//!
//! Responses are cached on disk in `tmdb_cache.json` beside the catalog, keyed by the
//! request url with the key replaced by `KEY` -- the same file and the same keys the
//! Python server reads and writes, so either server can pick up the other's cache.

use super::upstream;
use super::*;
use std::time::Duration;

const API: &str = "https://api.themoviedb.org/3";

pub(super) struct Tmdb {
    key: String,
    cache_path: PathBuf,
    cache: Mutex<Map<String, Value>>,
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
        // A cache that does not parse is started afresh, as `json.load`'s ValueError is.
        let cache = fs::read(&cache_path)
            .ok()
            .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
            .and_then(|value| value.as_object().cloned())
            .unwrap_or_default();
        Self {
            key,
            cache_path,
            cache: Mutex::new(cache),
        }
    }

    fn save(&self, cache: &Map<String, Value>) {
        if let Ok(text) = serde_json::to_vec(cache) {
            let _ = fs::write(&self.cache_path, text);
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
        let url = format!("{API}/{}?{}", path.trim_start_matches('/'), query.join("&"));
        let cache_key = url.replace(&self.key, "KEY");
        if let Some(hit) = self.cache.lock().ok()?.get(&cache_key).cloned() {
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
        let mut cache = self.cache.lock().ok()?;
        cache.insert(cache_key, payload.clone());
        self.save(&cache);
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
