//! `metadata.PlexProvider`: Plex's own metadata agent, present once the server is claimed or
//! has `PLEX_TOKEN`, and asked with `plextv::server_token`: the claim's token, else that one.
//!
//! What a real PMS asks when it matches a file (plexlab's outbound capture): `POST
//! /library/metadata/matches` with the title, year and file name, or with an external id,
//! and a matched show's and season's `children` for their `plex://` guids. Cycle 70: a
//! row whose guid plex.tv does not know draws no detail page in the Android app.
//! Answers are cached beside the catalog under the keys the Python module writes: a row
//! each in `plex_cache.db` (diskcache.rs), which imports `plex_cache.json` -- what
//! `build_catalog.py --relink` writes -- once.

use super::diskcache::DiskCache;
use super::upstream;
use super::*;
use std::time::Duration;

const PROVIDER: &str = "https://metadata.provider.plex.tv";
/// How long an answer the provider may complete later is kept before it is asked again.
const INCOMPLETE_FOR: i64 = 86_400;

/// A kept answer that is incomplete and older than `INCOMPLETE_FOR` (or kept before
/// answers were stamped).
fn expired(incomplete: bool, answer: &Value) -> bool {
    incomplete && answer.get("_rexCachedAt").and_then(Value::as_i64).is_none_or(|at| now() - at >= INCOMPLETE_FOR)
}

/// Stamp an incomplete answer with when it was kept.
fn stamped(incomplete: bool, mut answer: Value) -> Value {
    if let (true, Some(object)) = (incomplete, answer.as_object_mut()) {
        object.insert("_rexCachedAt".to_string(), json!(now()));
    }
    answer
}

pub(crate) struct PlexMeta {
    /// Read per call, so a claim made at runtime is asked with at once.
    claim: super::plextv::SharedClaim,
    /// This server's machineIdentifier: what a real PMS sends as `X-Plex-Client-Identifier`
    /// on every agent call (plexlab's outbound capture, tests/fixtures/plexmeta_matches_s143.json).
    client: String,
    cache_path: PathBuf,
    cache: DiskCache,
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
    pub(super) fn new(claim: super::plextv::SharedClaim, cache_path: PathBuf, client: String) -> Self {
        Self { claim, client, cache: DiskCache::open(&cache_path), cache_path }
    }
}

impl crate::identify::PlexSource for PlexMeta {
    fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Option<Value> {
        let key = format!("{method} {path} {}", body.map(python_json).unwrap_or_default());
        // A match or children list with no rows may be filled later: kept a day, then asked
        // again (the production rex drew Camp CrunchLabs on the blank tile after its poster was
        // added, 2026-10-12, when every answer was kept for good).
        let incomplete = |answer: &Value| {
            answer.pointer("/MediaContainer/Metadata").and_then(Value::as_array).is_none_or(Vec::is_empty)
        };
        let cached = self.cache.get(&key);
        if let Some(hit) = cached.as_ref().filter(|hit| !expired(incomplete(hit), hit)) {
            return Some(hit.clone());
        }
        let Some(token) = super::plextv::server_token(&self.claim) else {
            return cached;
        };
        let mut headers = vec![
            ("X-Plex-Token", token),
            ("Accept", "application/json".to_string()),
            ("Content-Type", "application/json".to_string()),
            ("X-Plex-Product", "Plex Media Server".to_string()),
            ("X-Plex-Provider-Version", "6.5.0".to_string()),
            ("X-Plex-Client-Identifier", self.client.clone()),
        ];
        if method == "GET" {
            // The agent pages at 50 (the lab capture); 500 is refused with a 400.
            headers.push(("X-Plex-Container-Start", "0".to_string()));
            headers.push(("X-Plex-Container-Size", "50".to_string()));
        }
        let payload = body.map(Value::to_string).unwrap_or_default();
        let fetched = upstream::fetch(
            method,
            &super::plextv::plextv_url(&format!("{PROVIDER}{path}")),
            &headers,
            payload.as_bytes(),
            Duration::from_secs(25),
        )
        .ok()
        .filter(|(status, _, _)| (200..300).contains(status))
        .and_then(|(_, _, raw)| serde_json::from_slice::<Value>(&raw).ok());
        // The provider unreachable: the answer it gave before, however empty.
        let Some(answer) = fetched else {
            return cached;
        };
        let answer = stamped(incomplete(&answer), answer);
        self.cache.put(&key, &answer);
        Some(answer)
    }
}

/// `build_catalog.relink_guids` (`rex catalog --relink`): the rows' guids relinked to the
/// agent's, asked as `client` (the catalog's machineIdentifier) through the server's own cache,
/// with the token of the claim beside `catalog`, else `PLEX_TOKEN`. None with neither.
pub(crate) fn relink(items: &mut [Value], catalog: &Path, client: String) -> Option<usize> {
    let claim_file = super::plextv::claim_path(catalog);
    let claim = super::plextv::Claim::load(&client, claim_file.clone(), "", super::plextv::PMS_VERSION_FALLBACK);
    let claim = Arc::new(Mutex::new(claim));
    super::plextv::server_token(&claim)?;
    let agent = PlexMeta::new(claim, claim_file.with_file_name("plex_cache.json"), client);
    struct Agent(PlexMeta);
    impl crate::metadata::PlexProvider for Agent {
        /// `PlexProvider.match`: the first external id, `tmdb://` ones first, whose match is
        /// a `plex://` item of the kind asked for.
        fn match_item(&mut self, kind: &str, external_ids: &[String]) -> Option<(String, Option<String>)> {
            use crate::identify::PlexSource;
            let mut ids = external_ids.iter().collect::<Vec<_>>();
            ids.sort_by_key(|id| !id.starts_with("tmdb://"));
            ids.into_iter().find_map(|external| {
                let body = json!({"type": if kind == "movie" { 1 } else { 2 }, "guid": external});
                let answer = self.0.call("POST", "/library/metadata/matches", Some(&body))?;
                let row = answer.pointer("/MediaContainer/Metadata/0")?;
                let guid = row.get("guid")?.as_str().filter(|guid| guid.starts_with(&format!("plex://{kind}/")))?;
                Some((guid.to_string(), row.get("slug").and_then(Value::as_str).map(str::to_string)))
            })
        }
        fn children(&mut self, guid: &str) -> HashMap<String, String> {
            crate::identify::plex_children(&self.0, guid)
        }
    }
    Some(crate::metadata::apply_plex_guids_with_provider(items, &mut Agent(agent)))
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
