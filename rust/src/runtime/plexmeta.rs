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
//!
//! Every call is asked in its library's language ([`PlexMeta::in_language`]), as a real PMS asks:
//! rex sent none until 2026-11-04 (rex#9), so a de-DE library was described in English.

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

/// Whether an answer may be filled later, so is kept a day and then asked again: a match or
/// children list with no rows (the production rex drew Camp CrunchLabs on the blank tile after its
/// poster was added, 2026-10-12, when every answer was kept for good); one holding fewer rows than
/// its `totalSize` (before every page was read, a list was kept as its first 50 rows); and a match
/// whose title has no poster yet (The Adventures of Baron Munchausen on a real library, 2026-11-19:
/// the agent names neither poster nor background and lists no images, and the blank tile's retry
/// read the kept answer every time, five days apart).
fn incomplete(method: &str, answer: &Value) -> bool {
    let rows = answer.pointer("/MediaContainer/Metadata").and_then(Value::as_array);
    let total = answer.pointer("/MediaContainer/totalSize").and_then(Value::as_u64).unwrap_or(0) as usize;
    let posterless = |rows: &Vec<Value>| method == "POST" && rows[0].get("thumb").and_then(Value::as_str).is_none_or(str::is_empty);
    rows.is_none_or(|rows| rows.is_empty() || rows.len() < total || posterless(rows))
}

/// Stamp an incomplete answer with when it was kept.
fn stamped(incomplete: bool, mut answer: Value) -> Value {
    if let (true, Some(object)) = (incomplete, answer.as_object_mut()) {
        object.insert("_rexCachedAt".to_string(), json!(now()));
    }
    answer
}

/// A library's language as a PMS hands it to Plex's agent: the tag and the country its region names
/// (de-DE DE, pt-BR BR, as en-US is US in both captures). A library with none is en-US, as its
/// section answers it; a bare tag (`de`, set by hand: Add Library offers only regional ones,
/// `agent_languages.json`) names no country, so none is sent rather than one guessed.
fn locale(language: &str) -> (&str, Option<String>) {
    let language = if language.is_empty() { "en-US" } else { language };
    let country = language
        .rsplit_once('-')
        .map(|(_, region)| region)
        .filter(|region| region.len() == 2 && region.chars().all(|c| c.is_ascii_alphabetic()))
        .map(str::to_ascii_uppercase);
    (language, country)
}

/// Where a PMS puts the language, as plexlab's and plexav's PMS 1.43.4 sent it (outbound captures,
/// en-US libraries): `x-plex-language` and `x-plex-country` headers on `POST
/// /library/metadata/matches` (`plexmeta_matches_s143.json`), and `X-Plex-Country` then
/// `X-Plex-Language` last in the query of a GET of an item, its images and its extras
/// (`music/agent.json`), so on rex's `children` too, which neither PMS asked (it reads an item with
/// `includeChildren=1`); a music match by release id (`GET .../matches?guid=mbid://`) carries
/// neither. The request's path and its extra headers.
fn localised(method: &str, path: &str, language: &str) -> (String, Vec<(&'static str, String)>) {
    let (language, country) = locale(language);
    if method == "POST" {
        let headers = std::iter::once(("X-Plex-Language", language.to_string())).chain(country.map(|country| ("X-Plex-Country", country)));
        return (path.to_string(), headers.collect());
    }
    if !path.starts_with("/library/metadata/") || path.starts_with("/library/metadata/matches") {
        return (path.to_string(), Vec::new());
    }
    let country = country.map(|country| format!("X-Plex-Country={country}&")).unwrap_or_default();
    let joint = if path.contains('?') { '&' } else { '?' };
    // A refresh's ask (`updating=1`) names its text format last, as plexlab's do (2026-11-26,
    // `tests/fixtures/refresh_s1434.json`); its matches and children asks name none.
    let format = if path.contains("updating=1") { "&X-Plex-Text-Format=markdown" } else { "" };
    (format!("{path}{joint}{country}X-Plex-Language={language}{format}"), Vec::new())
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

    /// The agent as a library whose `language` is this asks it.
    pub(crate) fn in_language<'a>(&'a self, language: &'a str) -> InLanguage<'a> {
        InLanguage { agent: self, language }
    }
}

/// The cache key an answer is kept under: the Python module's.
fn cache_key(method: &str, path: &str, body: Option<&Value>) -> String {
    format!("{method} {path} {}", body.map(python_json).unwrap_or_default())
}

/// Plex's agent as the answers kept in `plex_cache.db` alone: asked nothing, so what a row was
/// described from is read again as it was, and a pass over the library waits on no network. Read
/// by the key every answer was kept under before rex asked in each library's language (en-US's).
pub(super) struct Kept<'a>(pub(super) &'a PlexMeta);

impl crate::identify::PlexSource for Kept<'_> {
    fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Option<Value> {
        self.0.cache.get(&cache_key(method, path, body))
    }
}

/// Plex's agent asked in one library's language.
pub(crate) struct InLanguage<'a> {
    agent: &'a PlexMeta,
    language: &'a str,
}

impl crate::identify::PlexSource for InLanguage<'_> {
    fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Option<Value> {
        self.agent.call(self.language, method, path, body, false, true)
    }
}

/// Each language its own answer, so a library whose language changes is asked again rather than
/// shown what another language was told. en-US keeps the key it was kept under while rex sent no
/// language, so English libraries keep what they hold.
fn keyed(language: &str, key: String) -> String {
    match locale(language).0 {
        "en-US" => key,
        other => format!("{other} {key}"),
    }
}

impl PlexMeta {
    /// A title described from a kept answer (a match, a season's children) takes the artwork and
    /// ratings `title_now` last read for its guid: those answers are kept for good, and a push that
    /// identifies a row again, or a Fix Match, would put back the urls the agent has since replaced
    /// and the scores a refresh replaced.
    pub(super) fn newer_description(&self, language: &str, title: &mut Value) {
        let id = title.get("guid").and_then(Value::as_str).and_then(|guid| guid.strip_prefix("plex://")?.split_once('/')).map(|(_, id)| id.to_string());
        let Some(id) = id else {
            return;
        };
        let Some(now) = self.cache.get(&keyed(language, cache_key("GET", &format!("/library/metadata/{id}"), None))) else {
            return;
        };
        let (Some(now), Some(title)) = (now.pointer("/MediaContainer/Metadata/0"), title.as_object_mut()) else {
            return;
        };
        for field in ["thumb", "art", "theme", "Image"] {
            if let Some(value) = now.get(field) {
                title.insert(field.to_string(), value.clone());
            }
        }
        // The scores as the newer answer gives them, none where it gives none.
        for field in crate::metadata::RATINGS {
            match now.get(field) {
                Some(value) => title.insert(field.to_string(), value.clone()),
                None => title.shift_remove(field),
            };
        }
    }

    /// A title as Plex's agent describes it now (`GET /library/metadata/{id}`, as plexav's PMS reads
    /// an item, `music/agent.json`; with `query`, as plexlab's refresh asks it), asked past
    /// `plex_cache.db`: the agent replaces a title's artwork and the urls it gave before stop
    /// answering (Funny Money's gracenote poster 403'd, 2026-11-17, while the agent named another).
    /// The title and the episodes a season's answer holds (`includeChildren=1`: no request of a
    /// refresh asks an episode on its own; a show's seasons are asked each) are kept under each
    /// title's own item key, which `newer_description` reads.
    pub(super) fn title_now(&self, language: &str, id: &str, query: &str) -> Option<Value> {
        let path = if query.is_empty() { format!("/library/metadata/{id}") } else { format!("/library/metadata/{id}?{query}") };
        let answer = self.call(language, "GET", &path, None, true, false)?;
        let title = answer.pointer("/MediaContainer/Metadata/0")?.clone();
        let mut own = title.clone();
        let held = own.as_object_mut().and_then(|own| own.shift_remove("Children"));
        let held = held.as_ref().and_then(|children| children.get("Metadata")).and_then(Value::as_array).cloned().unwrap_or_default();
        let held = held.into_iter().filter(|row| row.get("type").and_then(Value::as_str) == Some("episode"));
        for row in std::iter::once(own).chain(held) {
            let Some(id) = row.get("guid").and_then(Value::as_str).and_then(|guid| guid.strip_prefix("plex://")?.split_once('/')).map(|(_, id)| id.to_string()) else {
                continue;
            };
            self.cache.put(&keyed(language, cache_key("GET", &format!("/library/metadata/{id}"), None)), &json!({"MediaContainer": {"size": 1, "Metadata": [row]}}));
        }
        Some(title)
    }

    /// `keep`: the answer is kept under its request's key (`title_now` keeps each title it holds itself).
    fn call(&self, language: &str, method: &str, path: &str, body: Option<&Value>, fresh: bool, keep: bool) -> Option<Value> {
        let key = keyed(language, cache_key(method, path, body));
        let (asked, localised_headers) = localised(method, path, language);
        let incomplete = |answer: &Value| incomplete(method, answer);
        let cached = self.cache.get(&key);
        if let Some(hit) = cached.as_ref().filter(|hit| !fresh && !expired(incomplete(hit), hit)) {
            return Some(hit.clone());
        }
        // Read token and identity together on every network call: the shared claim's daily
        // version refresh must reach an already-created metadata transport too.
        let headers = {
            let claim = self.claim.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            let Some(token) = claim.token.as_ref().filter(|token| !token.is_empty()) else {
                return cached.filter(|_| !fresh);
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
            headers.extend(localised_headers);
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
                &super::plextv::plextv_url(&format!("{PROVIDER}{asked}")),
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
        if method == "GET" {
            while let Some(container) = fetched.as_mut().and_then(|answer| answer.get_mut("MediaContainer")) {
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
        }
        // The provider unreachable: the answer it gave before, however empty.
        let Some(answer) = fetched else {
            return cached.filter(|_| !fresh);
        };
        let answer = stamped(incomplete(&answer), answer);
        if keep {
            self.cache.put(&key, &answer);
        }
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
    // Guids only, which name a title whatever its language: asked as the en-US default.
    struct Agent(PlexMeta);
    impl crate::metadata::PlexProvider for Agent {
        /// `PlexProvider.match`: the first external id, `tmdb://` ones first, whose match is
        /// a `plex://` item of the kind asked for.
        fn match_item(&mut self, kind: &str, external_ids: &[String]) -> Option<(String, Option<String>)> {
            let answer = crate::identify::plex_match(&self.0.in_language("en-US"), kind, external_ids)?;
            let slug = answer.row.get("slug").and_then(Value::as_str).map(str::to_string);
            Some((answer.guid, slug))
        }
        fn children(&mut self, guid: &str) -> HashMap<String, String> {
            crate::identify::plex_children(&self.0.in_language("en-US"), guid)
        }
    }
    Some(crate::metadata::apply_plex_guids_with_provider(items, &mut Agent(agent)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_match_without_a_poster_is_asked_again() {
        let answer = |row: Value| json!({"MediaContainer": {"Metadata": [row]}});
        assert!(incomplete("POST", &answer(json!({"guid": "plex://movie/5d77683b6f4521001ea9d3e3"}))));
        assert!(!incomplete("POST", &answer(json!({"guid": "plex://movie/x", "thumb": "https://metadata-static.plex.tv/p.jpg"}))));
        // A season's episodes without a still are as complete as the agent makes them.
        assert!(!incomplete("GET", &answer(json!({"guid": "plex://episode/x"}))));
        assert!(incomplete("POST", &json!({"MediaContainer": {"Metadata": []}})));
    }

    #[test]
    fn cache_keys_are_the_python_modules() {
        assert_eq!(
            python_json(&json!({"type": 1, "title": "Amélie", "excludeElements": "Media", "year": 2001})),
            "{\"excludeElements\": \"Media\", \"title\": \"Am\\u00e9lie\", \"type\": 1, \"year\": 2001}"
        );
    }

    /// en-US is asked exactly as plexlab's and plexav's PMS asked: the match's headers, and each
    /// item GET's query in its recorded order once identify's path is localised.
    #[test]
    fn a_library_language_is_sent_where_a_pms_sends_it() {
        let matches: Value = serde_json::from_str(include_str!("../../../tests/fixtures/plexmeta_matches_s143.json")).unwrap();
        let sent: Vec<(String, String)> = matches["request"]["req_headers"]
            .as_array()
            .unwrap()
            .iter()
            .map(|pair| (pair[0].as_str().unwrap().to_string(), pair[1].as_str().unwrap().to_string()))
            .filter(|(name, _)| matches!(name.as_str(), "x-plex-language" | "x-plex-country"))
            .collect();
        let (path, headers) = localised("POST", "/library/metadata/matches", "en-US");
        let ours: Vec<(String, String)> = headers.into_iter().map(|(name, value)| (name.to_ascii_lowercase(), value)).collect();
        assert_eq!((path.as_str(), ours), ("/library/metadata/matches", sent));
        let agent: Value = serde_json::from_str(include_str!("../../../tests/fixtures/music/agent.json")).unwrap();
        for asked in agent["answers"].as_array().unwrap() {
            let query: Vec<String> = asked["query"].as_array().unwrap().iter().map(|pair| format!("{}={}", pair[0].as_str().unwrap(), pair[1].as_str().unwrap())).collect();
            let own: Vec<&String> = query.iter().filter(|pair| !pair.starts_with("X-Plex-")).collect();
            let path = asked["path"].as_str().unwrap();
            let identify = if own.is_empty() { path.to_string() } else { format!("{path}?{}", own.iter().map(|pair| pair.as_str()).collect::<Vec<_>>().join("&")) };
            assert_eq!(localised("GET", &identify, "en-US"), (format!("{path}?{}", query.join("&")), Vec::new()));
        }
        // Another library's language, and the country its tag names.
        assert_eq!(
            localised("POST", "/library/metadata/matches", "de-DE").1,
            vec![("X-Plex-Language", "de-DE".to_string()), ("X-Plex-Country", "DE".to_string())]
        );
        assert_eq!(localised("GET", "/library/metadata/5d9c/children", "pt-BR").0, "/library/metadata/5d9c/children?X-Plex-Country=BR&X-Plex-Language=pt-BR");
        assert_eq!(localised("GET", "/library/metadata/5d9c", "de").0, "/library/metadata/5d9c?X-Plex-Language=de");
        assert_eq!(localised("GET", "/library/metadata/5d9c", "").0, "/library/metadata/5d9c?X-Plex-Country=US&X-Plex-Language=en-US");
    }
}
