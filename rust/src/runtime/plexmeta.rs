//! `metadata.PlexProvider`: Plex's own metadata agent, present only with `PLEX_TOKEN`.
//!
//! What a real PMS asks when it matches a file (plexlab's outbound capture): `POST
//! /library/metadata/matches` with the title, year and file name, or with an external id,
//! and a matched show's and season's `children` for their `plex://` guids. Cycle 70: a
//! row whose guid plex.tv does not know draws no detail page in the Android app.
//! Answers are cached in `plex_cache.json` beside the catalog under the keys the Python
//! module writes, so `build_catalog.py --relink` and this server share the file.

use super::upstream;
use super::*;
use std::time::Duration;

const PROVIDER: &str = "https://metadata.provider.plex.tv";

pub(crate) struct PlexMeta {
    token: String,
    cache_path: PathBuf,
    cache: Mutex<Map<String, Value>>,
}

impl std::fmt::Debug for PlexMeta {
    // The token is an account credential; it never reaches a debug print.
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("PlexMeta").field("cache_path", &self.cache_path).finish()
    }
}

/// `json.dumps(value, sort_keys=True)`: Python's separators and ASCII escapes.
fn python_json(value: &Value) -> String {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let fields = keys
                .into_iter()
                .map(|key| format!("{}: {}", python_json(&json!(key)), python_json(&map[key])))
                .collect::<Vec<_>>();
            format!("{{{}}}", fields.join(", "))
        }
        Value::Array(rows) => format!("[{}]", rows.iter().map(python_json).collect::<Vec<_>>().join(", ")),
        other => {
            let mut out = String::new();
            for unit in other.to_string().encode_utf16() {
                if unit < 0x80 {
                    out.push(unit as u8 as char);
                } else {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            out
        }
    }
}

impl PlexMeta {
    pub(crate) fn new(token: String, cache_path: PathBuf) -> Self {
        let cache = fs::read(&cache_path)
            .ok()
            .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
            .and_then(|value| value.as_object().cloned())
            .unwrap_or_default();
        Self { token, cache_path, cache: Mutex::new(cache) }
    }
}

impl crate::identify::PlexSource for PlexMeta {
    fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Option<Value> {
        let key = format!("{method} {path} {}", body.map(python_json).unwrap_or_default());
        if let Some(hit) = self.cache.lock().ok()?.get(&key).cloned() {
            return Some(hit);
        }
        let mut headers = vec![
            ("X-Plex-Token", self.token.clone()),
            ("Accept", "application/json".to_string()),
            ("Content-Type", "application/json".to_string()),
            ("X-Plex-Product", "Plex Media Server".to_string()),
            ("X-Plex-Provider-Version", "6.5.0".to_string()),
            ("X-Plex-Client-Identifier", "simplex".to_string()),
        ];
        if method == "GET" {
            // The agent pages at 50 (the lab capture); 500 is refused with a 400.
            headers.push(("X-Plex-Container-Start", "0".to_string()));
            headers.push(("X-Plex-Container-Size", "50".to_string()));
        }
        let payload = body.map(Value::to_string).unwrap_or_default();
        let (status, _, raw) = upstream::fetch(
            method,
            &format!("{PROVIDER}{path}"),
            &headers,
            payload.as_bytes(),
            Duration::from_secs(25),
        )
        .ok()?;
        if !(200..300).contains(&status) {
            return None;
        }
        let answer = serde_json::from_slice::<Value>(&raw).ok()?;
        let mut cache = self.cache.lock().ok()?;
        cache.insert(key, answer.clone());
        if let Ok(text) = serde_json::to_vec(&*cache) {
            let _ = fs::write(&self.cache_path, text);
        }
        Some(answer)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cache_keys_are_the_python_modules() {
        assert_eq!(
            python_json(&json!({"type": 1, "title": "Amélie", "excludeElements": "Media", "year": 2001})),
            "{\"excludeElements\": \"Media\", \"title\": \"Am\\u00e9lie\", \"type\": 1, \"year\": 2001}"
        );
    }
}
