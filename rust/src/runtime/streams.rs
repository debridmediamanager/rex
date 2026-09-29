//! Stream titles as each client is sent them, ported from `server.py`'s
//! `_with_stream_language` and `_with_client_titles`.
//!
//! The catalog's own titles are recomputed at load (`Catalog::normalize`, the
//! `_retitle_streams` pass); a response then localises a Stream's language and the
//! words its titles are composed from, and finally gives the products in
//! `CODEC_TITLE_PRODUCTS` the codec in a video's and a subtitle's `displayTitle`.

use super::*;
use std::sync::OnceLock;

/// The products whose `displayTitle` carries the codec, measured on both servers
/// against 38 product names, case-sensitively (cycle 63 added the last eight).
const CODEC_TITLE_PRODUCTS: &[&str] = &[
    "Plex Web",
    "Plex for iOS",
    "Plex for Apple TV",
    "Plex for Android (TV)",
    "Plex for Android (Mobile)",
    "Plex for LG",
    "Plex for Samsung",
    "Plex for Windows",
    "Plex for Mac",
    "Plex for Xbox",
    "Plex for PlayStation 4",
    "Plex for PlayStation 5",
    "Plex for Vizio",
    "Plex HTPC for Windows",
    "Plex HTPC for Mac",
    "Plex HTPC for Linux",
    "Plex for Linux",
    "Plex for Fetch TV",
    "Plex for Smart TVs",
    "Plex for VIDAA",
    "Plex for X1",
];

struct StreamTables {
    /// language -> English language name -> localised name (from the captured
    /// `languageOverride`/`audioLanguage` enum lists, aligned by code).
    names: HashMap<String, HashMap<String, String>>,
    /// language -> language tag -> localised name (`item_language_names.json`).
    by_tag: HashMap<String, HashMap<String, String>>,
    /// word -> language -> localised word (`Unknown`, `Forced`, `SDH`, `External`).
    words: Map<String, Value>,
}

fn tables(catalog: &Path) -> &'static StreamTables {
    static TABLES: OnceLock<StreamTables> = OnceLock::new();
    TABLES.get_or_init(|| {
        let mut names: HashMap<String, HashMap<String, String>> = HashMap::new();
        let pairs = |text: &str| {
            text.split('|')
                .filter_map(|pair| pair.split_once(':'))
                .map(|(code, name)| (code.to_string(), name.to_string()))
                .collect::<Vec<_>>()
        };
        if let Some(enums) = load_json_sidecar(catalog, "translations.json")
            .and_then(|document| document.get("enumValues").cloned())
            .and_then(|value| value.as_object().cloned())
        {
            for (english_enum, translated) in enums {
                if !english_enum.contains("en:English") {
                    continue;
                }
                let english = pairs(&english_enum);
                let Some(translated) = translated.as_object() else {
                    continue;
                };
                for (code, other) in translated {
                    let other = pairs(&string_value(other))
                        .into_iter()
                        .collect::<HashMap<_, _>>();
                    for (key, name) in &english {
                        if key.is_empty() {
                            continue;
                        }
                        if let Some(local) = other.get(key).filter(|local| !local.is_empty()) {
                            names
                                .entry(code.clone())
                                .or_default()
                                .entry(name.clone())
                                .or_insert_with(|| local.clone());
                        }
                    }
                }
            }
        }
        let mut by_tag: HashMap<String, HashMap<String, String>> = HashMap::new();
        if let Some(table) = load_json_sidecar(catalog, "item_language_names.json")
            .and_then(|document| document.get("names").cloned())
            .and_then(|value| value.as_object().cloned())
        {
            for (language, entries) in table {
                if let Some(entries) = entries.as_object() {
                    by_tag.insert(
                        language,
                        entries
                            .iter()
                            .map(|(tag, name)| (tag.clone(), string_value(name)))
                            .collect(),
                    );
                }
            }
        }
        let words = load_json_sidecar(catalog, "stream_words.json")
            .and_then(|document| document.get("words").cloned())
            .and_then(|value| value.as_object().cloned())
            .unwrap_or_default();
        StreamTables {
            names,
            by_tag,
            words,
        }
    })
}

fn localise_streams(
    node: &mut Value,
    names: &HashMap<String, String>,
    by_tag: &HashMap<String, String>,
    words: &Map<String, Value>,
) {
    match node {
        Value::Array(items) => {
            for item in items {
                localise_streams(item, names, by_tag, words);
            }
        }
        Value::Object(object) => {
            let kind = object.get("streamType").and_then(Value::as_i64);
            if matches!(kind, Some(2 | 3)) {
                let english = object
                    .get("language")
                    .and_then(Value::as_str)
                    .map(str::to_string);
                let tag = object
                    .get("languageTag")
                    .and_then(Value::as_str)
                    .map(str::to_string);
                let localised = tag
                    .as_ref()
                    .and_then(|tag| by_tag.get(tag))
                    .or_else(|| english.as_ref().and_then(|english| names.get(english)))
                    .cloned();
                if localised.is_some() || !words.is_empty() {
                    if let (Some(_), Some(localised)) = (&english, &localised) {
                        object.insert("language".to_string(), json!(localised));
                    }
                    // The omission is decided on the STORED name (a track titled
                    // "French" keeps its `French` part under `en`).
                    crate::mediainfo::stream_titles(object, english.as_deref(), Some(words));
                }
            }
            for value in object.values_mut() {
                if value.is_object() || value.is_array() {
                    localise_streams(value, names, by_tag, words);
                }
            }
        }
        _ => {}
    }
}

fn client_titles(node: &mut Value, external: &str) {
    match node {
        Value::Array(items) => {
            for item in items {
                client_titles(item, external);
            }
        }
        Value::Object(object) => {
            let kind = object.get("streamType").and_then(Value::as_i64);
            let title = object
                .get("displayTitle")
                .and_then(Value::as_str)
                .filter(|title| !title.is_empty())
                .map(str::to_string);
            if let (Some(kind @ (1 | 3)), Some(title)) = (kind, title) {
                let codec = if kind == 1 {
                    crate::mediainfo::video_codec_label(object).unwrap_or_default()
                } else {
                    crate::mediainfo::subtitle_codec_label(object, external)
                };
                if !codec.is_empty() {
                    object.insert(
                        "displayTitle".to_string(),
                        json!(format!("{title} ({codec})")),
                    );
                }
            }
            for value in object.values_mut() {
                if value.is_object() || value.is_array() {
                    client_titles(value, external);
                }
            }
        }
        _ => {}
    }
}

impl Runtime {
    /// The Stream pass every JSON response takes: the client's language first (a
    /// real PMS composes the codec suffix onto the already-localised title), then the
    /// product's codec suffix.  Cycle 50: a DECISION's streams get no suffix (kids
    /// answers `1080p` there to Plex Web), and neither do the session rows.
    pub(super) fn stream_titles_response(&self, request: &Request, response: &mut Response) {
        if response.status != 200 || response.content_type != "application/json" {
            return;
        }
        let (path, raw_query) = split_target(&request.target);
        if path.contains("/decision") || path.starts_with("/status/sessions") {
            return;
        }
        let query = parse_query(&raw_query)
            .into_iter()
            .collect::<HashMap<String, String>>();
        let language = framework::request_language(&query, &request.headers);
        // Cycle 55: the HEADER beats the query for the product.
        let product = request
            .headers
            .get("x-plex-product")
            .cloned()
            .or_else(|| query.get("X-Plex-Product").cloned());
        let codec_titles = product
            .as_deref()
            .is_some_and(|product| CODEC_TITLE_PRODUCTS.contains(&product));
        let tables = tables(&self.catalog_path);
        let mut words = Map::new();
        if let Some(language) = &language {
            for (word, table) in &tables.words {
                if let Some(local) = table.get(language).filter(|local| !local.is_null()) {
                    words.insert(word.clone(), local.clone());
                }
            }
        }
        let names = language
            .as_ref()
            .and_then(|language| tables.names.get(language));
        let by_tag = language
            .as_ref()
            .and_then(|language| tables.by_tag.get(language));
        let localise = language.is_some()
            && (names.is_some_and(|map| !map.is_empty())
                || by_tag.is_some_and(|map| !map.is_empty())
                || !words.is_empty());
        if !localise && !codec_titles {
            return;
        }
        let Ok(mut document) = serde_json::from_slice::<Value>(&response.body) else {
            return;
        };
        let segments = path
            .split('/')
            .filter(|piece| !piece.is_empty())
            .collect::<Vec<_>>();
        if segments.len() == 4
            && segments[..2] == ["library", "sections"]
            && matches!(segments[3], "audioLanguage" | "subtitleLanguage")
        {
            // Cycle 60, measured on zen: the language AXIS names its values in the
            // client's language too (`Französisch` under `de`, `French` under `en`),
            // keyed by the value -- the language tag.
            if let (Some(by_tag), Some(rows)) = (
                by_tag.filter(|map| !map.is_empty()),
                document["MediaContainer"]
                    .get_mut("Directory")
                    .and_then(Value::as_array_mut),
            ) {
                for row in rows.iter_mut() {
                    let key = row.get("key").map(string_value).unwrap_or_default();
                    if let Some(title) = by_tag.get(&key).filter(|title| !title.is_empty()) {
                        row["title"] = json!(title);
                    }
                }
                if let Ok(body) = serde_json::to_vec(&document) {
                    response.body = body;
                }
            }
            return;
        }
        if !response
            .body
            .windows(10)
            .any(|window| window == b"streamType")
        {
            return;
        }
        if localise {
            let empty = HashMap::new();
            localise_streams(
                &mut document,
                names.unwrap_or(&empty),
                by_tag.unwrap_or(&empty),
                &words,
            );
        }
        if codec_titles {
            let external = words
                .get("External")
                .map(string_value)
                .unwrap_or_else(|| "External".to_string());
            client_titles(&mut document, &external);
        }
        if let Ok(body) = serde_json::to_vec(&document) {
            response.body = body;
        }
    }
}
