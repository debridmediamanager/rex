//! Which language a request asks for, and the tree walk that translates the
//! server's own text into it. Ported from `server.py`'s `request_language`,
//! `_accept_language_order`, `_localise` and `_localise_hub_title`, whose
//! docstrings hold the measurements (cycles 16, 17, 25, 45, 50, 60, 66-68, 79).

use super::*;

/// Locales the table holds that have NO catalogue of their own (cycle 66): only
/// their ICU names move, and `Accept-Language` skips them.
const ICU_ONLY_LOCALES: &[&str] = &[
    "bg", "el", "en-GB", "fa", "is", "my", "th", "tr", "vi", "zh-TW",
];

/// A regional locale that reads another locale's catalogue (cycle 66).
const CATALOGUE_OF: &[(&str, &str)] = &[
    ("es-419", "es"),
    ("fr-CA", "fr"),
    ("pt-PT", "pt-BR"),
    ("hu-HU", "hu"),
];

/// An `Accept-Language` list, best first, or None if the header is malformed (cycle 45):
/// any malformed member disqualifies the WHOLE header, `q=0` and `*` are dropped, and a
/// tie goes to the first listed.
fn accept_language_order(value: &str) -> Option<Vec<String>> {
    let weight_pattern =
        regex::Regex::new(r"^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$").expect("valid regex");
    let mut ranked: Vec<(f64, usize, String)> = Vec::new();
    for (index, member) in value.split(',').enumerate() {
        let member = member.trim();
        if member.is_empty() {
            continue;
        }
        let (tag, parameters) = match member.split_once(';') {
            Some((tag, parameters)) => (tag.trim(), Some(parameters)),
            None => (member, None),
        };
        if tag.is_empty() {
            return None;
        }
        let mut weight = 1.0;
        if let Some(parameters) = parameters {
            let (name, raw) = parameters.split_once('=')?;
            if name.trim() != "q" {
                return None;
            }
            let raw = raw.trim();
            if !weight_pattern.is_match(raw) {
                return None;
            }
            weight = raw.parse::<f64>().ok()?;
        }
        if tag == "*" || weight == 0.0 {
            continue;
        }
        ranked.push((-weight, index, tag.to_string()));
    }
    ranked.sort_by(|left, right| {
        left.0
            .partial_cmp(&right.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(left.1.cmp(&right.1))
    });
    Some(ranked.into_iter().map(|(_, _, tag)| tag).collect())
}

pub(super) struct Languages {
    pub localised: HashSet<String>,
    pub translated: HashSet<String>,
}

impl Languages {
    pub(super) fn from_strings_pub(strings: &Value) -> Self {
        Self::from_strings(strings)
    }

    fn from_strings(strings: &Value) -> Self {
        let mut localised = HashSet::new();
        if let Some(attributes) = strings.as_object() {
            for entries in attributes.values() {
                for entry in entries.as_object().into_iter().flat_map(|map| map.values()) {
                    for code in entry.as_object().into_iter().flat_map(|map| map.keys()) {
                        localised.insert(code.clone());
                    }
                }
            }
        }
        let translated = localised
            .iter()
            .filter(|code| !ICU_ONLY_LOCALES.contains(&code.as_str()))
            .cloned()
            .collect();
        Self {
            localised,
            translated,
        }
    }
}

/// The language a request asks for, by the measured precedence: `X-Plex-Language`
/// (query, then header) beats `X-Plex-Accept-Language` (header, then query) beats
/// `Accept-Language`; a regional form works like its primary subtag except the
/// locales that keep their full tag; a language the server does not have is None.
pub(super) fn request_language(
    pairs: &[(String, String)],
    headers: &HashMap<String, String>,
    regional: bool,
    languages: &Languages,
) -> Option<String> {
    let query_value = |name: &str| {
        pairs
            .iter()
            .rev()
            .find(|(key, _)| key == name)
            .map(|(_, value)| value.clone())
    };
    let wanted = query_value("X-Plex-Language")
        .filter(|value| !value.is_empty())
        .or_else(|| {
            headers
                .get("x-plex-language")
                .cloned()
                .filter(|value| !value.is_empty())
        });
    let Some(text) = wanted else {
        for source in [
            headers.get("x-plex-accept-language").cloned(),
            query_value("X-Plex-Accept-Language"),
            headers.get("accept-language").cloned(),
        ] {
            let Some(source) = source else { continue };
            match accept_language_order(&source) {
                Some(order) if !order.is_empty() => {
                    let mut fallback = None;
                    for tag in order {
                        let normal = tag.replace('_', "-").to_lowercase();
                        let subtags = normal.split('-').collect::<Vec<_>>();
                        if subtags[0] == "zh"
                            && subtags[1..]
                                .iter()
                                .any(|s| matches!(*s, "hant" | "tw" | "hk" | "mo"))
                        {
                            continue;
                        }
                        if subtags[0] == "ca" || subtags[0] == "nn" {
                            let target = if subtags[0] == "ca" { "ca" } else { "no" };
                            if fallback.is_none() && languages.translated.contains(target) {
                                fallback = Some(target.to_string());
                            }
                            continue;
                        }
                        if regional && subtags == ["hu", "hu"] {
                            return Some("hu-HU".to_string());
                        }
                        if (normal == "pt-br" || normal == "pt-pt")
                            && languages.translated.contains("pt-BR")
                        {
                            return Some("pt-BR".to_string());
                        }
                        let primary = subtags[0];
                        if languages.translated.contains(primary) {
                            return Some(primary.to_string());
                        }
                    }
                    return fallback;
                }
                _ => {
                    // A malformed header falls back to English rather than to the next
                    // source: `de;q=abc` is not "no Accept-Language".
                    if !source.trim().is_empty() {
                        return None;
                    }
                }
            }
        }
        return None;
    };
    if let Some(first) = text.chars().next() {
        if !first.is_alphanumeric() {
            // Cycle 55: one leading `-`, `_` or space is dropped; anything that then
            // names no language, or another leading character, is ENGLISH.
            let rest = if matches!(first, '-' | '_' | ' ') {
                &text[1..]
            } else {
                ""
            };
            let primary = rest.replace('_', "-").to_lowercase();
            let primary = primary.split('-').next().unwrap_or("").to_string();
            return Some(if languages.translated.contains(&primary) {
                primary
            } else {
                "en".to_string()
            });
        }
    }
    let normalized = text.replace('_', "-").to_lowercase();
    if regional && normalized == "hu-hu" {
        return Some("hu-HU".to_string());
    }
    let keep = [
        ("zh-tw", "zh-TW"),
        ("en-gb", "en-GB"),
        ("es-419", "es-419"),
        ("fr-ca", "fr-CA"),
        ("pt-pt", "pt-PT"),
    ];
    if let Some((_, full)) = keep.iter().find(|(low, _)| *low == normalized) {
        if languages.localised.contains(*full) {
            return Some(full.to_string());
        }
    }
    let exact = [
        ("zh-hk", "en"),
        ("zh-tw", "en"),
        ("pt-br", "pt-BR"),
        ("pt-pt", "pt-BR"),
    ];
    if let Some((_, target)) = exact.iter().find(|(low, _)| *low == normalized) {
        return Some(target.to_string());
    }
    let primary = normalized.split('-').next().unwrap_or("").to_string();
    if primary == "nn" && languages.translated.contains("no") {
        return Some("no".to_string());
    }
    (!primary.is_empty()).then_some(primary)
}

/// One cell of the table: the language's own entry, else its catalogue's.
fn cell(entry: &Value, language: &str) -> Option<String> {
    if let Some(text) = entry.get(language).and_then(Value::as_str) {
        return Some(text.to_string());
    }
    let base = CATALOGUE_OF
        .iter()
        .find(|(regional, _)| *regional == language)
        .map(|(_, base)| *base)?;
    entry.get(base).and_then(Value::as_str).map(str::to_string)
}

pub(super) struct Localiser<'a> {
    pub strings: &'a Value,
    pub templates: Option<&'a Value>,
    pub enum_leads: Option<&'a Value>,
    pub language: &'a str,
    /// An item's own language enum, rebuilt piece by piece (cycle 50/60).
    pub language_enum: &'a dyn Fn(&str) -> Option<String>,
}

impl Localiser<'_> {
    /// `Recently Added in <library>` and friends are localised around the name.
    fn hub_title(&self, title: &str) -> Option<String> {
        for (template, languages) in self.templates?.as_object()? {
            let (head, tail) = template.split_once("%s")?;
            if title.len() > head.len() + tail.len()
                && title.starts_with(head)
                && title.ends_with(tail)
            {
                if let Some(target) = languages
                    .get(self.language)
                    .and_then(Value::as_str)
                    .filter(|t| !t.is_empty())
                {
                    return Some(
                        target.replace("%s", &title[head.len()..title.len() - tail.len()]),
                    );
                }
            }
        }
        None
    }

    fn string(&self, text: &str, attribute: &str, in_row: bool) -> Option<String> {
        if !in_row {
            if attribute == "enumValues" {
                if let Some(rebuilt) = (self.language_enum)(text) {
                    return Some(rebuilt);
                }
            }
            if let Some(entry) = self
                .strings
                .get(attribute)
                .and_then(|table| table.get(text))
            {
                if let Some(found) = cell(entry, self.language) {
                    return Some(found);
                }
            } else if attribute == "enumValues" {
                // Cycle 60: an enum built from the HOST can never be in the table whole;
                // its lead is translated.
                if let Some((lead, rest)) = text.split_once('|') {
                    if let Some(translated) = self
                        .enum_leads
                        .and_then(|leads| leads.get(lead))
                        .and_then(|entry| entry.get(self.language))
                        .and_then(Value::as_str)
                    {
                        return Some(format!("{translated}|{rest}"));
                    }
                }
            }
        }
        // A GENERATED title carries a number: `Season 1` is in the table and
        // `Season 19` is not, so the word comes from the numbered form.
        for word in ["Season", "Episode"] {
            if let Some(number) = text.strip_prefix(&format!("{word} ")) {
                if !number.is_empty() && number.bytes().all(|b| b.is_ascii_digit()) {
                    if let Some(pattern) = self
                        .strings
                        .get(attribute)
                        .and_then(|table| table.get(format!("{word} 1")))
                        .and_then(|entry| cell(entry, self.language))
                    {
                        return Some(pattern.replace('1', number));
                    }
                }
            }
        }
        None
    }

    /// Translate the server's own text in a response: every string is looked up by
    /// the ATTRIBUTE it appears in and then by its English text. A LIBRARY ROW (a node
    /// carrying `ratingKey`, or a `MetadataItem`) is the user's content -- only its
    /// generated titles move -- except its `Preferences` and `Related` blocks.
    pub(super) fn walk(&self, node: &mut Value, attribute: Option<&str>, in_row: bool) {
        match node {
            Value::Object(object) => {
                let row = object.contains_key("ratingKey") || attribute == Some("MetadataItem");
                if object.contains_key("hubIdentifier") {
                    if let Some(title) = object
                        .get("title")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                    {
                        if let Some(localised) = self.hub_title(&title) {
                            object.insert("title".to_string(), json!(localised));
                        }
                    }
                }
                let keys = object.keys().cloned().collect::<Vec<_>>();
                for key in keys {
                    let child_in_row = if key == "Preferences" || key == "Related" {
                        false
                    } else {
                        row || in_row
                    };
                    if let Some(child) = object.get_mut(&key) {
                        self.walk(child, Some(&key), child_in_row);
                    }
                }
                // Cycle 79: a season 0's generated `Specials` is translated like
                // `Season N`, as the season's `title` and its episodes' `parentTitle`.
                if row {
                    for (key, index, kind) in [
                        ("title", "index", "season"),
                        ("parentTitle", "parentIndex", "episode"),
                    ] {
                        let is_specials = object.get(key).and_then(Value::as_str)
                            == Some("Specials")
                            && object.get(index).and_then(Value::as_i64) == Some(0)
                            && object.get("type").and_then(Value::as_str) == Some(kind);
                        if is_specials {
                            if let Some(found) = self
                                .strings
                                .get(key)
                                .and_then(|table| table.get("Specials"))
                                .and_then(|entry| cell(entry, self.language))
                            {
                                object.insert(key.to_string(), json!(found));
                            }
                        }
                    }
                }
            }
            Value::Array(values) => {
                for child in values {
                    self.walk(child, attribute, in_row);
                }
            }
            Value::String(text) => {
                if let Some(attribute) = attribute {
                    if let Some(localised) = self.string(text, attribute, in_row) {
                        *text = localised;
                    }
                }
            }
            _ => {}
        }
    }
}

/// `activity_strings.json`: the server's gettext catalogue for activity titles
/// (`strings`) and the butler's progress messages (`progress`).
fn activity_tables(catalog: &std::path::Path) -> &'static (Value, Value) {
    static TABLES: std::sync::OnceLock<(Value, Value)> = std::sync::OnceLock::new();
    TABLES.get_or_init(|| {
        let document = load_json_sidecar(catalog, "activity_strings.json").unwrap_or(Value::Null);
        (
            document.get("strings").cloned().unwrap_or(Value::Null),
            document.get("progress").cloned().unwrap_or(Value::Null),
        )
    })
}

/// One activity title, subtitle or butler message in `language`, or the text itself
/// (the oracle's `_localise_activity_text`).  Cycle 65, measured on plexscratch42 with
/// ten sockets open through one scan: `Suche...`, `Durchsuche TV Shows`, `Abonnements
/// verarbeiten …` under `de`; `{1}` is content and stays.  A regional catalogue answers
/// first and an id it lacks reads the catalogue it is based on.
fn activity_text(text: &str, language: &str, table: &Value) -> String {
    if text.is_empty() {
        return text.to_string();
    }
    let Some(entries) = table.as_object() else {
        return text.to_string();
    };
    if let Some(exact) = entries.get(text) {
        return cell(exact, language).unwrap_or_else(|| text.to_string());
    }
    for (msgid, languages) in entries {
        let Some((head, tail)) = msgid.split_once("{1}") else {
            continue;
        };
        let Some(target) = cell(languages, language) else {
            continue;
        };
        if text.len() > head.len() + tail.len() && text.starts_with(head) && text.ends_with(tail) {
            return target.replace("{1}", &text[head.len()..text.len() - tail.len()]);
        }
    }
    text.to_string()
}

/// `refusal_strings.json` (cycle 87, measured on plexscratch42 in every language Plex
/// Web sends): the three Add Library refusal lines the server's gettext catalogue
/// holds, chosen like an activity string (`hu-HU` reads `hu_HU`); the other eight lines
/// stay English everywhere.
pub(super) fn refusal_line(catalog: &std::path::Path, text: &str, language: Option<&str>) -> String {
    static TABLE: std::sync::OnceLock<Value> = std::sync::OnceLock::new();
    let Some(language) = language else {
        return text.to_string();
    };
    let table = TABLE.get_or_init(|| {
        load_json_sidecar(catalog, "refusal_strings.json")
            .and_then(|document| document.get("strings").cloned())
            .unwrap_or(Value::Null)
    });
    activity_text(text, language, table)
}

/// The language a request's answer is written in (`request_language`, its primary subtag),
/// from the server's own translation table.
pub(super) fn request_language_of(catalog: &std::path::Path, request: &Request) -> Option<String> {
    let table = translation_table(catalog)?;
    let (_, raw_query) = split_target(&request.target);
    request_language(&parse_query(&raw_query), &request.headers, false, &table.languages)
}

/// The language a notification socket was opened with, fixed at the handshake
/// (cycle 65): `X-Plex-Language` in the query, else the handshake's
/// `Accept-Language` -- all Plex Web sends.
pub(super) fn socket_language(catalog: &std::path::Path, request: &Request) -> Option<String> {
    let table = translation_table(catalog)?;
    let languages = &table.languages;
    let (_, raw_query) = split_target(&request.target);
    let pairs = parse_query(&raw_query);
    request_language(&pairs, &request.headers, true, &languages)
}

/// The rows one socket is sent (the oracle's `_localise_activity_rows`): the
/// `ActivityNotification` rows' titles in the socket's language, and (cycle 66) the
/// butler's `ProgressNotification` messages, which are catalogue ids too.  Status
/// frames and a scan's own progress line stay English.
pub(super) fn localise_notification(
    value: &mut Value,
    language: Option<&str>,
    catalog: &std::path::Path,
) {
    let Some(language) = language else { return };
    let (strings, progress) = activity_tables(catalog);
    let Some(object) = value.as_object_mut() else {
        return;
    };
    for (element, rows) in object.iter_mut() {
        let rows: Vec<&mut Value> = match rows {
            Value::Array(list) => list.iter_mut().collect(),
            other => vec![other],
        };
        for row in rows {
            match element.as_str() {
                "ActivityNotification" => {
                    if let Some(activity) = row.get_mut("Activity").and_then(Value::as_object_mut) {
                        for field in ["title", "subtitle"] {
                            if let Some(text) = activity
                                .get(field)
                                .and_then(Value::as_str)
                                .map(str::to_string)
                            {
                                activity.insert(
                                    field.to_string(),
                                    json!(activity_text(&text, language, strings)),
                                );
                            }
                        }
                    }
                }
                "ProgressNotification" => {
                    if let Some(text) = row
                        .get("message")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                    {
                        row["message"] = json!(activity_text(&text, language, progress));
                    }
                }
                _ => {}
            }
        }
    }
}
