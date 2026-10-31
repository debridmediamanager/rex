//! `metadata.PlexProvider`: Plex's own metadata agent, present once the server is claimed, and
//! asked with the claim's server token (`plextv::claim_token`).
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
    pub(super) fn new(claim: super::plextv::SharedClaim, cache_path: PathBuf) -> Self {
        Self { claim, cache: DiskCache::open(&cache_path), cache_path }
    }
}

impl crate::identify::PlexSource for PlexMeta {
    fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Option<Value> {
        let key = format!("{method} {path} {}", body.map(python_json).unwrap_or_default());
        // A match or children list with no rows may be filled later: kept a day, then asked
        // again (the production rex drew Camp CrunchLabs on the blank tile after its poster was
        // added, 2026-10-12, when every answer was kept for good).
        // So is one holding fewer rows than its `totalSize`: before every page was read, a list
        // was kept as its first 50 rows.
        let incomplete = |answer: &Value| {
            let rows = answer.pointer("/MediaContainer/Metadata").and_then(Value::as_array);
            let total = answer.pointer("/MediaContainer/totalSize").and_then(Value::as_u64).unwrap_or(0) as usize;
            rows.is_none_or(|rows| rows.is_empty() || rows.len() < total)
        };
        let cached = self.cache.get(&key);
        if let Some(hit) = cached.as_ref().filter(|hit| !expired(incomplete(hit), hit)) {
            return Some(hit.clone());
        }
        // Read token and identity together on every network call: the shared claim's daily
        // version refresh must reach an already-created metadata transport too.
        let headers = {
            let claim = self.claim.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            let Some(token) = claim.token.as_ref().filter(|token| !token.is_empty()) else {
                return cached;
            };
            let mut headers = claim.pms_headers("PlexMediaServer", false);
            for (name, value) in &mut headers {
                if *name == "Accept" {
                    *value = "application/json".to_string();
                }
            }
            headers.extend([
                ("X-Plex-Token", token.clone()),
                ("Content-Type", "application/json".to_string()),
                ("X-Plex-Provider-Version", "6.5.0".to_string()),
            ]);
            headers
        };
        let payload = body.map(Value::to_string).unwrap_or_default();
        let page = |start: usize| {
            let mut headers = headers.clone();
            // A path naming its own query (a music match, an album with its children) is asked as
            // plexav's PMS asked it, unpaged (its outbound capture, 2026-10-31).
            if method == "GET" && !path.contains('?') {
                // The agent pages at 50 (the lab capture); 500 is refused with a 400.
                headers.push(("X-Plex-Container-Start", start.to_string()));
                headers.push(("X-Plex-Container-Size", "50".to_string()));
            }
            upstream::fetch(
                method,
                &super::plextv::plextv_url(&format!("{PROVIDER}{path}")),
                &headers,
                payload.as_bytes(),
                Duration::from_secs(25),
            )
            .ok()
            .filter(|(status, _, _)| (200..300).contains(status))
            .and_then(|(_, _, raw)| serde_json::from_slice::<Value>(&raw).ok())
        };
        let mut fetched = page(0);
        // A list longer than a page is read to its `totalSize` (Detective Conan's season 1 is 1,218
        // episodes in pages of 50, Animaniacs' 160, 2026-10-23, `tests/fixtures/provider/feed_episodes_1023.json`):
        // one page left every episode past the 50th unmatched. A page that cannot be read leaves
        // the answer as the earlier ones made it.
        while method == "GET" {
            let Some(container) = fetched.as_mut().and_then(|answer| answer.get_mut("MediaContainer")) else {
                break;
            };
            let total = container.get("totalSize").and_then(Value::as_u64).unwrap_or(0) as usize;
            let have = container.get("Metadata").and_then(Value::as_array).map_or(0, Vec::len);
            if have == 0 || have >= total {
                break;
            }
            let more = page(have).and_then(|next| next.pointer("/MediaContainer/Metadata").and_then(Value::as_array).cloned()).unwrap_or_default();
            if more.is_empty() {
                break;
            }
            if let Some(Value::Array(rows)) = container.get_mut("Metadata") {
                rows.extend(more);
                let size = rows.len();
                container["size"] = json!(size);
            }
        }
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
/// with the token of the claim beside `catalog`. None without one.
pub(crate) fn relink(items: &mut [Value], catalog: &Path, client: String, name: &str) -> Option<usize> {
    let claim_file = super::plextv::claim_path(catalog);
    let claim = super::plextv::Claim::load(&client, claim_file.clone(), name, super::plextv::PMS_VERSION_FALLBACK);
    let claim = Arc::new(Mutex::new(claim));
    super::plextv::claim_token(&claim)?;
    // A one-shot catalog command needs only the release list, not cloud account startup
    // or a version watcher. Offline it retains the packaged/persisted current identity.
    super::plextv::refresh_version(&claim);
    let agent = PlexMeta::new(claim, claim_file.with_file_name("plex_cache.json"));
    struct Agent(PlexMeta);
    impl crate::metadata::PlexProvider for Agent {
        /// `PlexProvider.match`: the first external id, `tmdb://` ones first, whose match is
        /// a `plex://` item of the kind asked for.
        fn match_item(&mut self, kind: &str, external_ids: &[String]) -> Option<(String, Option<String>)> {
            let answer = crate::identify::plex_match(&self.0, kind, external_ids)?;
            let slug = answer.row.get("slug").and_then(Value::as_str).map(str::to_string);
            Some((answer.guid, slug))
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
