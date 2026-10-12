//! The plug-in framework's surface: `/system/*` and `/:/plugins/*`.
//!
//! Cycles 18-58 measured this subtree on plexlab (1.43.4), the 1.42.2 reference and both
//! disposable copies as a DIFFERENT SERVER with an envelope of its own: its empty answers
//! carry no `Content-Type`, its documents are written by the framework's serialiser
//! (single-quoted declaration, two-space indent), its errors are the framework's own Python
//! tracebacks, and every response but three carries the framework's `cereal1` session
//! cookie. The Python oracle is `server.py` (`handle`, the `-- system --` block of `_route`
//! and the `framework-*` kinds of `_dispatch`); this module ports it as one unit so the
//! rest of the runtime keeps no second, approximate copy.

use super::*;
use regex::Regex;
use std::sync::OnceLock;

/// `Set-Cookie` value the framework hands back, after the owner's `=`.
const FRAMEWORK_COOKIE_VALUE: &str =
    "Y2VyZWFsMQozCmRpY3QKbGlzdApkaWN0CjIKcjEKczcKY29va2llc3IyCnM3CnNlc3Npb24wCjAKcjAK";
/// A state cookie the framework can unpack begins with this `cereal1` marker.
const FRAMEWORK_STATE_PREFIX: &str = "Y2VyZWFsMQ";

/// Cycle 52: the seventeen plug-ins a real PMS hosts, in the order every reference lists
/// them. A fixed table in the image, not this library's.
pub(super) const PLUGIN_LIST: [&str; 17] = [
    "com.plexapp.agents.fanarttv",
    "com.plexapp.framework",
    "com.plexapp.agents.htbackdrops",
    "com.plexapp.agents.lastfm",
    "com.plexapp.agents.localmedia",
    "com.plexapp.agents.lyricfind",
    "com.plexapp.resources.mediaflags",
    "com.plexapp.agents.movieposterdb",
    "org.musicbrainz.agents.music",
    "com.plexapp.agents.none",
    "com.plexapp.agents.imdb",
    "com.plexapp.agents.plexthememusic",
    "com.plexapp.system.scanners",
    "com.plexapp.system",
    "com.plexapp.agents.themoviedb",
    "com.plexapp.agents.thetvdb",
    "com.plexapp.plugins.webclient",
];

/// The framework's own `messaging/wait` reply: an empty cerealizer dump.
const PLUGIN_WAIT: &[u8] = b"Y2VyZWFsMQowCmIx";

const FRAMEWORK_RUNTIME_FRAME: &str = "  File \"/usr/lib/plexmediaserver/Resources/Plug-ins-f737b826c/Framework.bundle/Contents/Resources/Versions/2/Python/Framework/components/runtime.py\", line 843, in handle_request\n    result = f(**d)\n";

/// `handlers/runtime.py:113` redirects `root` to the first registered prefix, and a bundle
/// that registers none -- every agent -- raises there.
fn plugin_root_error() -> Vec<u8> {
    format!(
        "<?xml version='1.0' encoding='utf-8'?>\n<Response code=\"2000\" status=\"IndexError: list index out of range\">\n  <Traceback>Traceback (most recent call last):\n{FRAMEWORK_RUNTIME_FRAME}  File \"/usr/lib/plexmediaserver/Resources/Plug-ins-f737b826c/Framework.bundle/Contents/Resources/Versions/2/Python/Framework/handlers/runtime.py\", line 113, in root\n    return Framework.objects.Redirect(self._core, handlers[0].prefix)\nIndexError: list index out of range\n</Traceback>\n</Response>\n"
    )
    .into_bytes()
}

/// Cycle 52: the plug-in framework's TypeError document. Byte for byte on plexlab and the
/// 1.42.2 reference once the bundle hash and line number are the reported version's.
fn framework_traceback(sentence: &str) -> Vec<u8> {
    format!(
        "<?xml version='1.0' encoding='utf-8'?>\n<Response code=\"2000\" status=\"TypeError: {sentence}\">\n  <Traceback>Traceback (most recent call last):\n{FRAMEWORK_RUNTIME_FRAME}TypeError: {sentence}\n</Traceback>\n</Response>\n"
    )
    .into_bytes()
}

fn framework_error(name: &str, least: usize, given: usize) -> Vec<u8> {
    framework_traceback(&format!(
        "{name}() takes at least {least} arguments ({given} given)"
    ))
}

fn framework_exactly(name: &str, count: usize) -> Vec<u8> {
    framework_traceback(&format!(
        "{name}() takes exactly {count} arguments (1 given)"
    ))
}

fn framework_unexpected(name: &str, keyword: &str) -> Vec<u8> {
    framework_traceback(&format!(
        "{name}() got an unexpected keyword argument '{keyword}'"
    ))
}

/// Cycle 53: framework routes whose Python function needs arguments the URL does not
/// supply, so a bare GET is the framework's own traceback with a 500.
fn framework_bare_error(route: &str) -> Option<Vec<u8>> {
    Some(match route {
        "/system/services/search" => framework_error("cs_search", 3, 1),
        "/system/services/searches" => framework_exactly("cs_searches", 2),
        "/system/services/relatedcontent/lookup" => {
            framework_exactly("cs_relatedcontent_lookup", 2)
        }
        "/system/agents/attribution" => framework_exactly("get_attribution_string", 2),
        "/system/agents/attribution/image" => framework_exactly("get_attribution_image", 2),
        _ => return None,
    })
}

/// `peerservice.py` registers channels and plugins over these five directory names.
const FRAMEWORK_PEER_DIRECTORIES: [&str; 5] = ["all", "music", "video", "photos", "applications"];
/// Cycle 53: the five framework routes whose `Cache-Control` comes second.
pub(super) const CACHE_CONTROL_FIRST: [&str; 3] = [
    "/system/search",
    "/system/library/sections",
    "/system/notify/serverUpdated",
];
const FRAMEWORK_EMPTY_CONTAINER: &[u8] = b"<?xml version='1.0' encoding='utf-8'?>\n<MediaContainer noHistory=\"0\" replaceParent=\"0\" size=\"0\" identifier=\"com.plexapp.system\"/>\n";
const FRAMEWORK_AGENT_STATUS: &[u8] = b"AgentService Status\n-------------------\nUpdate queue size   : 0\nNotify queue size   : 0\nUpdate mutexes:     : 0\n\nInfo lock state     : Unlocked\nUpdate lock state   : Unlocked\n\nProcessing updates  : {\n}\n\nPending updates     : [\n]\n";

/// The four actions under `/system/agents`: the Python function, its declared argument
/// count, the names it declares, and whether it lacks `**kwargs` (so an undeclared name
/// raises). Read out of `agentservice.py`; `runtime.py:843` calls `f(**d)`.
fn framework_agent_action(
    action: &str,
) -> Option<(&'static str, usize, &'static [&'static str], bool)> {
    Some(match action {
        "search" => (
            "search",
            4,
            &["identifier", "mediaType", "lang", "manual"],
            false,
        ),
        "update" => (
            "update",
            3,
            &[
                "mediaType",
                "guid",
                "id",
                "parentGUID",
                "force",
                "agent",
                "async",
                "parentID",
                "libraryAgent",
                "periodic",
                "respectTags",
            ],
            true,
        ),
        "contributors" => (
            "list_contributors",
            3,
            &["mediaType", "primaryAgent", "primary_only"],
            true,
        ),
        "media/get" => (
            "media_get",
            4,
            &["mediaType", "guid", "url", "parentGUID"],
            true,
        ),
        _ => return None,
    })
}

/// `agentservice.py`'s `media_types`.
fn legacy_media_class(number: i64) -> Option<&'static str> {
    Some(match number {
        1 => "Movie",
        2 => "TV_Show",
        8 => "Artist",
        9 => "Album",
        13 => "Photo",
        20 => "Plex_Personal_Media",
        _ => return None,
    })
}

fn xml_text(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn xml_quote(value: &str) -> String {
    format!("\"{}\"", xml_text(value))
}

/// Python's `repr()` of a text value, which the framework's ValueError quotes.
fn python_repr(value: &str) -> String {
    if value.contains('\'') && !value.contains('"') {
        format!("\"{value}\"")
    } else {
        format!("'{}'", value.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// The framework's own sidecar tables, read once.
struct FrameworkTables {
    bundles: Value,
    prefs: Value,
    attributions: Value,
    legacy_agents: Value,
    legacy_config: Value,
    flags: Value,
    flag_blank: Vec<u8>,
    substitutions: HashMap<String, Vec<(Regex, String)>>,
    system_agents: Value,
    agent_languages: Value,
}

fn tables(catalog: &Path) -> &'static FrameworkTables {
    static TABLES: OnceLock<FrameworkTables> = OnceLock::new();
    TABLES.get_or_init(|| {
        let load = |name: &str| load_json_sidecar(catalog, name).unwrap_or(Value::Null);
        let flags = load("media_flags.json");
        let flag_blank = flags
            .get("blank")
            .and_then(Value::as_str)
            // The captured blank is MIME base64, wrapped at 76 columns.
            .map(|text| text.split_whitespace().collect::<String>())
            .and_then(|text| base64_decode_bytes(&text))
            .unwrap_or_default();
        let mut substitutions = HashMap::new();
        if let Some(kinds) = flags.get("substitutions").and_then(Value::as_object) {
            for (kind, rules) in kinds {
                let compiled = rules
                    .as_array()
                    .map(|rules| {
                        rules
                            .iter()
                            .filter_map(|rule| {
                                let expression = rule.get(0)?.as_str()?;
                                let name = rule.get(1)?.as_str()?;
                                let regex = Regex::new(&format!("(?i){expression}")).ok()?;
                                Some((regex, name.to_string()))
                            })
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default();
                substitutions.insert(kind.clone(), compiled);
            }
        }
        FrameworkTables {
            bundles: load("plugin_bundles.json"),
            prefs: load("plugin_prefs.json"),
            attributions: load("attributions.json"),
            legacy_agents: load("legacy_agents.json"),
            legacy_config: load("legacy_agent_config.json"),
            flags,
            flag_blank,
            substitutions,
            system_agents: load("system_agents.json"),
            agent_languages: load("agent_languages.json"),
        }
    })
}

impl Response {
    /// Cycle 21: an empty body with NO `Content-Type`, the `/system` subtree's shape.
    pub(super) fn bare(status: u16) -> Self {
        Self {
            status,
            content_type: String::new(),
            body: Vec::new(),
            headers: Vec::new(),
            close: false,
        }
    }

    /// The framework's XML traceback (`plugin-xml`); it keeps the connection alive.
    fn plugin_xml(status: u16, body: Vec<u8>) -> Self {
        Self {
            status,
            content_type: "application/xml".to_string(),
            body,
            headers: vec![("X-Rex-No-Head-Gzip".to_string(), "1".to_string())],
            close: false,
        }
    }

    /// Cycle 53: a framework STRING result. `construct_response` writes the header
    /// literally as `Content-type`, the one lower-case `t` on the surface.
    fn framework_string(status: u16, body: Vec<u8>, content_type: &str) -> Self {
        Self {
            status,
            content_type: String::new(),
            body,
            headers: vec![("Content-type".to_string(), content_type.to_string())],
            close: false,
        }
    }

    fn framework_document(status: u16, body: Vec<u8>) -> Self {
        Self {
            status,
            content_type: "application/xml".to_string(),
            body,
            headers: Vec::new(),
            close: false,
        }
    }

    /// A bundled image: `no-cache`, and a range unit only where the asset handler serves it.
    fn asset(content_type: &str, body: Vec<u8>, ranged: bool) -> Self {
        let mut headers = vec![("Cache-Control".to_string(), "no-cache".to_string())];
        if ranged {
            headers.push(("Accept-Ranges".to_string(), "bytes".to_string()));
        }
        Self {
            status: 200,
            content_type: content_type.to_string(),
            body,
            headers,
            close: false,
        }
    }
}

/// `(start, size)` the request asked for, both required and non-negative digits.
fn container_window(query: &HashMap<String, String>) -> Option<(usize, usize)> {
    let read = |name: &str| {
        let value = query.get(name)?;
        let digits = value.strip_prefix('+').unwrap_or(value);
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        digits.parse::<usize>().ok()
    };
    Some((
        read("X-Plex-Container-Start")?,
        read("X-Plex-Container-Size")?,
    ))
}

/// Cycle 53: slice a captured framework document to the window the client asked for,
/// keeping every row's bytes, and name the window in the response headers.
fn page_framework_document(document: &str, query: &HashMap<String, String>) -> Response {
    let Some((start, size)) = container_window(query) else {
        return Response::framework_document(200, document.as_bytes().to_vec());
    };
    let mut parts = document.splitn(3, '\n');
    let head = parts.next().unwrap_or_default();
    let mut container = parts.next().unwrap_or_default().to_string();
    let body = parts.next().unwrap_or_default();
    let rows = body
        .split('\n')
        .filter(|line| {
            let trimmed = line.trim_start();
            trimmed.starts_with("<Setting") || trimmed.starts_with("<Agent")
        })
        .collect::<Vec<_>>();
    if container.ends_with("/>") {
        container.truncate(container.len() - 2);
        container.push('>');
    }
    let page = rows
        .iter()
        .skip(start)
        .take(size)
        .copied()
        .collect::<Vec<_>>();
    let size_re = Regex::new(r#"size="\d+""#).expect("static regex");
    container = size_re
        .replacen(&container, 1, format!("size=\"{}\"", page.len()).as_str())
        .into_owned();
    container = container.replacen(
        "<MediaContainer ",
        &format!("<MediaContainer totalSize=\"{}\" ", rows.len()),
        1,
    );
    let text = if page.is_empty() {
        format!("{head}\n{}\n", container.replacen('>', "/>", 1))
    } else {
        format!(
            "{head}\n{container}\n{}\n</MediaContainer>\n",
            page.join("\n")
        )
    };
    let mut response = Response::framework_document(200, text.into_bytes());
    response
        .headers
        .push(("X-Plex-Container-Start".to_string(), start.to_string()));
    response.headers.push((
        "X-Plex-Container-Total-Size".to_string(),
        rows.len().to_string(),
    ));
    response
}

/// `request_language` from `server.py`: the language a request asks for, by the measured
/// precedence (`X-Plex-Language` query then header, then `X-Plex-Accept-Language` header
/// and query, then `Accept-Language`). `None` is the no-language default.
pub(super) fn request_language(
    query: &HashMap<String, String>,
    headers: &HashMap<String, String>,
) -> Option<String> {
    const TRANSLATED: [&str; 35] = [
        "af", "ar", "ca", "cs", "da", "de", "en", "es", "es-419", "et", "fi", "fr", "fr-CA", "he",
        "hr", "hu", "it", "ja", "ko", "lt", "nb", "nl", "no", "pl", "pt", "pt-BR", "pt-PT", "ro",
        "ru", "sk", "sl", "sr", "sv", "uk", "zh",
    ];
    const LOCALISED_EXTRA: [&str; 10] = [
        "bg", "el", "en-GB", "fa", "is", "my", "th", "tr", "vi", "zh-TW",
    ];
    let translated = |code: &str| TRANSLATED.contains(&code);
    let localised = |code: &str| translated(code) || LOCALISED_EXTRA.contains(&code);
    let wanted = query
        .get("X-Plex-Language")
        .filter(|value| !value.is_empty())
        .or_else(|| {
            headers
                .get("x-plex-language")
                .filter(|value| !value.is_empty())
        });
    let Some(text) = wanted else {
        for source in [
            headers.get("x-plex-accept-language"),
            query.get("X-Plex-Accept-Language"),
            headers.get("accept-language"),
        ] {
            if let Some(order) = source.and_then(|value| accept_language_order(value)) {
                if order.is_empty() {
                    // an all-disqualified list reads as no header
                } else {
                    let mut fallback = None;
                    for tag in order {
                        let lowered = tag.replace('_', "-").to_ascii_lowercase();
                        let subtags = lowered.split('-').collect::<Vec<_>>();
                        if subtags[0] == "zh"
                            && subtags[1..]
                                .iter()
                                .any(|subtag| matches!(*subtag, "hant" | "tw" | "hk" | "mo"))
                        {
                            continue;
                        }
                        if matches!(subtags[0], "ca" | "nn") {
                            let target = if subtags[0] == "ca" { "ca" } else { "no" };
                            if fallback.is_none() && translated(target) {
                                fallback = Some(target.to_string());
                            }
                            continue;
                        }
                        if matches!(lowered.as_str(), "pt-br" | "pt-pt") {
                            return Some("pt-BR".to_string());
                        }
                        if translated(subtags[0]) {
                            return Some(subtags[0].to_string());
                        }
                    }
                    return fallback;
                }
            }
            if let Some(value) = source {
                if !value.trim().is_empty() {
                    return None;
                }
            }
        }
        return None;
    };
    if let Some(first) = text.chars().next() {
        if !first.is_alphanumeric() {
            let rest = if matches!(first, '-' | '_' | ' ') {
                &text[1..]
            } else {
                ""
            };
            let primary = rest
                .replace('_', "-")
                .to_ascii_lowercase()
                .split('-')
                .next()
                .unwrap_or_default()
                .to_string();
            return Some(if translated(&primary) {
                primary
            } else {
                "en".to_string()
            });
        }
    }
    let normalized = text.replace('_', "-").to_ascii_lowercase();
    let regional = match normalized.as_str() {
        "zh-tw" => Some("zh-TW"),
        "en-gb" => Some("en-GB"),
        "es-419" => Some("es-419"),
        "fr-ca" => Some("fr-CA"),
        "pt-pt" => Some("pt-PT"),
        _ => None,
    };
    if let Some(code) = regional.filter(|code| localised(code)) {
        return Some(code.to_string());
    }
    match normalized.as_str() {
        "zh-hk" | "zh-tw" => return Some("en".to_string()),
        "pt-br" | "pt-pt" => return Some("pt-BR".to_string()),
        _ => {}
    }
    let primary = normalized.split('-').next().unwrap_or_default().to_string();
    if primary == "nn" {
        return Some("no".to_string());
    }
    if primary.is_empty() {
        None
    } else {
        Some(primary)
    }
}

/// An `Accept-Language` list, best first; `None` when any member is malformed.
fn accept_language_order(value: &str) -> Option<Vec<String>> {
    let weight_re = Regex::new(r"^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$").expect("static regex");
    let mut ranked = Vec::new();
    for (index, member) in value.split(',').enumerate() {
        let member = member.trim();
        if member.is_empty() {
            continue;
        }
        let (tag, parameters) = match member.split_once(';') {
            Some((tag, rest)) => (tag.trim(), Some(rest)),
            None => (member, None),
        };
        if tag.is_empty() {
            return None;
        }
        let mut weight = 1.0f64;
        if let Some(parameters) = parameters {
            let (name, raw) = parameters.split_once('=')?;
            if name.trim() != "q" {
                return None;
            }
            let raw = raw.trim();
            if !weight_re.is_match(raw) {
                return None;
            }
            weight = raw.parse().unwrap_or(0.0);
        }
        if tag == "*" || weight == 0.0 {
            continue;
        }
        ranked.push((weight, index, tag.to_string()));
    }
    ranked.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(a.1.cmp(&b.1))
    });
    Some(ranked.into_iter().map(|(_, _, tag)| tag).collect())
}

/// Rewrite `"Language":[a,b]` into `"Language":a,"Language":b` -- cycle 21's one endpoint
/// that writes a repeated child element as a repeated JSON KEY.
fn repeat_keys(mut text: String) -> String {
    for name in ["Language", "MediaType"] {
        let needle = format!("\"{name}\":[");
        while let Some(start) = text.find(&needle) {
            let bytes = text.as_bytes();
            let open = start + needle.len() - 1;
            let mut depth = 0i32;
            let mut index = open;
            let mut in_string = false;
            let mut escaped = false;
            while index < bytes.len() {
                let c = bytes[index];
                if in_string {
                    if escaped {
                        escaped = false;
                    } else if c == b'\\' {
                        escaped = true;
                    } else if c == b'"' {
                        in_string = false;
                    }
                } else if c == b'"' {
                    in_string = true;
                } else if c == b'[' || c == b'{' {
                    depth += 1;
                } else if c == b']' || c == b'}' {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                }
                index += 1;
            }
            let inner = &text[open + 1..index];
            let mut rows = Vec::new();
            let mut piece_start = 0;
            let mut depth = 0i32;
            let mut in_string = false;
            let mut escaped = false;
            for (offset, c) in inner.bytes().enumerate() {
                if in_string {
                    if escaped {
                        escaped = false;
                    } else if c == b'\\' {
                        escaped = true;
                    } else if c == b'"' {
                        in_string = false;
                    }
                    continue;
                }
                match c {
                    b'"' => in_string = true,
                    b'{' | b'[' => depth += 1,
                    b'}' | b']' => depth -= 1,
                    b',' if depth == 0 => {
                        rows.push(&inner[piece_start..offset]);
                        piece_start = offset + 1;
                    }
                    _ => {}
                }
            }
            if !inner[piece_start..].trim().is_empty() {
                rows.push(&inner[piece_start..]);
            }
            let replacement = rows
                .iter()
                .map(|row| format!("\"{name}\":{}", row.trim()))
                .collect::<Vec<_>>()
                .join(",");
            text = format!("{}{}{}", &text[..start], replacement, &text[index + 1..]);
        }
    }
    text
}

impl Runtime {
    /// Whether this response carries the framework's cookie (`_framework_cookie`).
    pub(super) fn framework_cookie(method: &str, raw_path: &str) -> Option<String> {
        if matches!(method, "OPTIONS" | "PATCH") {
            return None;
        }
        let path = raw_path.trim_end_matches('/');
        if let Some(rest) = path.strip_prefix("/:/plugins/") {
            let owner = rest.split('/').next().unwrap_or_default();
            if PLUGIN_LIST.contains(&owner) {
                return Some(format!("{owner}={FRAMEWORK_COOKIE_VALUE}"));
            }
            return None;
        }
        if path != "/system" && !path.starts_with("/system/") {
            return None;
        }
        if matches!(path, "/system/agents" | "/system/proxy") {
            return None;
        }
        let pieces = path.split('/').collect::<Vec<_>>();
        let cookie = Some(format!("com.plexapp.system={FRAMEWORK_COOKIE_VALUE}"));
        if pieces.len() == 6 && pieces[..3] == ["", "system", "agents"] && pieces[4] == "config" {
            // Cycle 53/58: the config READ is the one framework document with no cookie;
            // its PUT, and a GET whose mediaType does not parse, carry it.
            if !matches!(method, "GET" | "HEAD") {
                return cookie;
            }
            if pieces[5].trim().parse::<i64>().is_err() {
                return cookie;
            }
            return None;
        }
        if pieces.len() == 4
            && pieces[2] == "scanners"
            && !pieces[3].is_empty()
            && pieces[3].bytes().all(|b| b.is_ascii_digit())
        {
            return None;
        }
        cookie
    }

    /// `_stale_framework_cookie`: a framework route whose state cookie will not unpack
    /// answers the plug-in's own Tornado server's empty 200.
    fn stale_framework_cookie(
        &self,
        route: &str,
        headers: &HashMap<String, String>,
    ) -> Option<Response> {
        let cookie = headers.get("cookie")?;
        if cookie.is_empty() {
            return None;
        }
        let owner = if route == "/system" || route.starts_with("/system/") {
            if matches!(route, "/system/agents" | "/system/proxy")
                || route.starts_with("/system/scanners/")
            {
                return None;
            }
            "com.plexapp.system"
        } else if let Some(rest) = route.strip_prefix("/:/plugins/") {
            let owner = rest.split('/').next().unwrap_or_default();
            if !PLUGIN_LIST.contains(&owner) {
                return None;
            }
            owner
        } else {
            return None;
        };
        for piece in cookie.split(';') {
            let (name, value) = piece.trim().split_once('=').unwrap_or((piece.trim(), ""));
            if name != owner {
                continue;
            }
            if !value.starts_with(FRAMEWORK_STATE_PREFIX) {
                let mut response = Response::bare(200);
                response.content_type = "text/html; charset=UTF-8".to_string();
                response
                    .headers
                    .push(("Server".to_string(), "TornadoServer/1.0".to_string()));
                return Some(response);
            }
        }
        None
    }

    /// The framework subtree, or `None` when the path is not the framework's.
    ///
    /// `path` is the normalised route (one trailing slash removed); `raw_path` is the
    /// path as the request spelled it.
    pub(super) fn route_framework(
        &mut self,
        request: &Request,
        path: &str,
        query: &HashMap<String, String>,
    ) -> Option<Response> {
        let under_system = path == "/system" || path.starts_with("/system/");
        let under_plugins = path.starts_with("/:/plugins/");
        if !under_system && !under_plugins && path != "/:/plugins" {
            return None;
        }
        let method = request.method.as_str();
        let segments = path
            .split('/')
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>();
        if segments.len() >= 2 && segments[..2] == ["system", "proxy"] {
            // Cycle 22: every verb, GET included, answers its own 400.
            return Some(Response::error(400, "rex: bad request"));
        }
        if under_system {
            if method == "OPTIONS" {
                return Some(Response::bare(200));
            }
            if matches!(method, "POST" | "PUT" | "DELETE")
                && !(method == "PUT"
                    && segments.len() == 5
                    && segments[1] == "agents"
                    && segments[3] == "config")
            {
                return Some(Response::bare(404));
            }
        }
        if under_plugins && !matches!(method, "GET" | "HEAD") {
            let owner = segments.get(2).copied().unwrap_or_default();
            if PLUGIN_LIST.contains(&owner) {
                if method == "OPTIONS" {
                    return Some(Response::bare(200));
                }
                return Some(Response::bare(404));
            }
        }
        if path == "/:/plugins" && !matches!(method, "GET" | "HEAD") {
            // The listing is a read-only route: every other verb is the 404 document.
            return Some(Response::error(404, "rex: not found"));
        }
        if segments.len() > 2 && segments[..2] == ["system", "scanners"] {
            // Cycle 36: the type is a SIGNED 32-bit integer, guarded before the framework
            // runs -- past the range a 400, a negative inside it the 85-byte 404.
            let raw = segments[2];
            let digits = raw.strip_prefix('-').unwrap_or(raw);
            if !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()) {
                match raw.parse::<i64>() {
                    Ok(number) if (-2_147_483_648..=2_147_483_647).contains(&number) => {
                        if number < 0 {
                            return Some(Response::error(404, "rex: not found"));
                        }
                    }
                    _ => return Some(Response::error(400, "rex: bad request")),
                }
            }
        }
        if let Some(stale) = self.stale_framework_cookie(path, &request.headers) {
            return Some(stale);
        }
        let mut response = self.framework_route_inner(request, path, &segments, query);
        if under_system && response.status == 404 && !response.content_type.is_empty() {
            // Cycle 21: an empty response under `/system` carries no Content-Type.
            response = Response::bare(404);
        }
        Some(response)
    }

    fn framework_route_inner(
        &mut self,
        request: &Request,
        route: &str,
        segments: &[&str],
        query: &HashMap<String, String>,
    ) -> Response {
        let method = request.method.as_str();
        let tables = tables(&self.catalog_path);
        let supplied = || {
            let mut names = query
                .keys()
                .filter(|key| !key.starts_with('_') && !key.starts_with("X-Plex-"))
                .cloned()
                .collect::<Vec<_>>();
            names.sort();
            names
        };
        if segments.len() == 5 && segments[..2] == ["system", "agents"] && segments[3] == "config" {
            return self.legacy_agent_config(method, segments[2], segments[4], query, tables);
        }
        let identifier = query.get("identifier").filter(|value| !value.is_empty());
        if let Some(body) = framework_bare_error(route) {
            if !(route.starts_with("/system/agents/attribution") && identifier.is_some()) {
                return Response::plugin_xml(500, body);
            }
        }
        if segments.len() == 3
            && matches!(segments[1], "channels" | "plugins")
            && segments[0] == "system"
            && FRAMEWORK_PEER_DIRECTORIES.contains(&segments[2])
        {
            // Cycle 87 (`server.py`): all ten page like every other framework container --
            // `totalSize="0"` and both `X-Plex-Container-*` headers for a full window.
            let document = std::str::from_utf8(FRAMEWORK_EMPTY_CONTAINER).expect("ascii");
            return page_framework_document(document, query);
        }
        if route == "/system/notify/serverUpdated" {
            return Response::framework_string(200, Vec::new(), "text/plain");
        }
        if route == "/system/debug/agentservice/status" {
            return Response::framework_string(200, FRAMEWORK_AGENT_STATUS.to_vec(), "text/plain");
        }
        if segments.len() >= 3
            && segments[..2] == ["system", "messaging"]
            && matches!(
                segments[2],
                "clear_events" | "register_for_event" | "broadcast_event"
            )
        {
            return Response::framework_string(200, Vec::new(), "text/plain");
        }
        if let Some(action) = route.strip_prefix("/system/agents/") {
            let action = action.trim_matches('/');
            if let Some((name, least, known, strict)) = framework_agent_action(action) {
                let names = supplied();
                if action == "contributors"
                    && query.get("mediaType").is_some_and(|v| !v.is_empty())
                    && query.get("primaryAgent").is_some_and(|v| !v.is_empty())
                {
                    return self.legacy_agent_contributors(
                        &query["mediaType"],
                        &query["primaryAgent"],
                        query,
                        tables,
                    );
                }
                // Python iterates the query in request order; take the first undeclared
                // name the request supplied.
                let unexpected = names_in_request_order(request, &names)
                    .into_iter()
                    .find(|key| !known.contains(&key.as_str()));
                if strict {
                    if let Some(keyword) = unexpected {
                        return Response::plugin_xml(500, framework_unexpected(name, &keyword));
                    }
                }
                let given = 1 + known
                    .iter()
                    .filter(|key| names.iter().any(|n| n == *key))
                    .count();
                return Response::plugin_xml(500, framework_error(name, least, given));
            }
        }
        if matches!(
            route,
            "/system/agents/attribution" | "/system/agents/attribution/image"
        ) {
            if let Some(identifier) = identifier {
                let entry = tables.attributions.get(identifier.as_str());
                if route.ends_with("/image") {
                    let Some(size) = entry
                        .and_then(|entry| entry.get("image"))
                        .and_then(Value::as_array)
                    else {
                        return Response::error(404, "rex: not found");
                    };
                    let width = size.first().and_then(Value::as_u64).unwrap_or(1) as u32;
                    let height = size.get(1).and_then(Value::as_u64).unwrap_or(1) as u32;
                    let kind = entry
                        .and_then(|entry| entry.get("imageType"))
                        .and_then(Value::as_str)
                        .unwrap_or("image/png");
                    return Response::asset(
                        kind,
                        generated_png(width, height, [32, 34, 38]),
                        false,
                    );
                }
                let Some(text) = entry
                    .and_then(|entry| entry.get("text"))
                    .and_then(Value::as_str)
                else {
                    return Response::error(404, "rex: not found");
                };
                let kind = entry
                    .and_then(|entry| entry.get("textType"))
                    .and_then(Value::as_str)
                    .unwrap_or("text/plain");
                return if kind == "text/plain" {
                    Response::framework_string(200, text.as_bytes().to_vec(), kind)
                } else {
                    let mut response = Response::framework_document(200, text.as_bytes().to_vec());
                    response.content_type = kind.to_string();
                    response
                };
            }
        }
        if segments.len() == 3 && segments[..2] == ["system", "agents"] {
            return Response::error(404, "rex: not found");
        }
        if segments.len() == 4 && segments[..2] == ["system", "agents"] && segments[3] == "config" {
            return Response::error(404, "rex: not found");
        }
        if route.starts_with("/system/agents/") {
            return Response::bare(404);
        }
        if route == "/system/agents" {
            return self.system_agents(request, query, tables);
        }
        if segments.len() >= 2 && segments[..2] == ["system", "scanners"] {
            if segments.len() == 2 {
                return Response::error(404, "rex: not found");
            }
            return self.system_scanners(segments[2]);
        }
        if segments.len() >= 3 && segments[..3] == ["system", "library", "sections"] {
            if segments.len() > 3 {
                return Response::bare(404);
            }
            return self.system_library_sections(query);
        }
        if let Some(relative) = route.strip_prefix("/system/bundle/media/flags/") {
            return self.media_flag(relative, tables);
        }
        if route == "/system/bundle/media/flags" {
            return Response::bare(404);
        }
        if route.starts_with("/system/appstore") {
            return Response::error(404, "rex: not found");
        }
        if route == "/system/services/url/lookup" {
            // Cycle 87 (`server.py`): the traceback is the BARE request only. Any `url`
            // argument, empty included, is a 404 with an empty, untyped body on
            // plexscratch42, plexlab and the 1.42.2 reference: the function runs and no
            // service claims it.
            if query.contains_key("url") {
                return Response::bare(404);
            }
            return Response::plugin_xml(500, framework_error("url_lookup", 2, 1));
        }
        if route == "/system" {
            let rows = vec![
                json!({"key": "library", "title": "Library Sections", "name": "Library Sections"}),
                json!({"key": "plugins", "title": "Plug-ins", "name": "Plug-ins"}),
            ];
            return self.framework_container(query, rows, "Directory", true);
        }
        if route == "/system/search" {
            let body = json!({"MediaContainer": {
                "size": 0,
                "identifier": "com.plexapp.system",
                "mediaTagPrefix": MEDIA_TAG_PREFIX,
                "prefsKey": "/:/plugins/com.plexapp.system/prefs",
            }});
            return system_body(&body);
        }
        if route.starts_with("/:/plugins") {
            return self.route_plugins(route, request, query, tables);
        }
        if route == "/system/library" {
            return Response::bare(404);
        }
        let _ = method;
        Response::bare(404)
    }

    fn route_plugins(
        &self,
        route: &str,
        request: &Request,
        query: &HashMap<String, String>,
        tables: &FrameworkTables,
    ) -> Response {
        if route == "/:/plugins" {
            // Cycle 52: the CORE's handler lists PMS's own bundled plug-ins.
            let rows = PLUGIN_LIST
                .iter()
                .map(|identifier| {
                    json!({"identifier": identifier,
                           "state": if *identifier == "com.plexapp.system" { 0 } else { 4 }})
                })
                .collect::<Vec<_>>();
            return Response::json(
                200,
                json!({"MediaContainer": {"size": rows.len(), "Plugin": rows}}),
            );
        }
        let pieces = route
            .strip_prefix("/:/plugins/")
            .unwrap_or_default()
            .split('/')
            .collect::<Vec<_>>();
        let owner = pieces.first().copied().unwrap_or_default();
        if !PLUGIN_LIST.contains(&owner) {
            return Response::error(404, "rex: not found");
        }
        let bundle = tables.bundles.get(owner).filter(|value| value.is_object());
        let rest = &pieces[1..];
        if bundle.is_none() && !rest.is_empty() {
            // A bundle the server hosts but does not RUN answers the framework's 500.
            return Response::error(500, "rex: plug-in has no preferences");
        }
        if rest == ["messaging", "wait"] && bundle.is_some() {
            return Response::framework_string(200, PLUGIN_WAIT.to_vec(), "text/plain");
        }
        if rest == ["prefs", "set"] && bundle.is_some() {
            return Response::framework_string(200, Vec::new(), "text/plain");
        }
        if rest == ["plist"] {
            if let Some(plist) = bundle.and_then(|b| b.get("plist")).and_then(Value::as_str) {
                return Response::framework_string(200, plist.as_bytes().to_vec(), "text/plain");
            }
        }
        if rest == ["root"] && bundle.is_some() {
            if owner == "com.plexapp.system" {
                let host = request.headers.get("host").cloned().unwrap_or_default();
                // server.py's `redirect` result: an exact `X-Plex-Neuter-Redirect: 1`
                // keeps the envelope and answers an empty 200, as `/web` does.
                let neutered = request
                    .headers
                    .get("x-plex-neuter-redirect")
                    .is_some_and(|value| value == "1");
                return Response {
                    status: if neutered { 200 } else { 302 },
                    content_type: String::new(),
                    body: Vec::new(),
                    headers: vec![
                        ("X-Plex-Protocol".to_string(), "1.0".to_string()),
                        ("Location".to_string(), format!("http://{host}/system")),
                        ("Cache-Control".to_string(), "public".to_string()),
                    ],
                    close: false,
                };
            }
            return Response::plugin_xml(500, plugin_root_error());
        }
        if rest.len() == 2 && rest[0] == "resources" && bundle.is_some() {
            let Some(size) = bundle
                .and_then(|b| b.get("resources"))
                .and_then(|r| r.get(rest[1]))
                .and_then(Value::as_array)
            else {
                return Response::bare(404);
            };
            let width = size.first().and_then(Value::as_u64).unwrap_or(1) as u32;
            let height = size.get(1).and_then(Value::as_u64).unwrap_or(1) as u32;
            return Response::asset(
                "image/png",
                generated_png(width, height, [32, 34, 38]),
                false,
            );
        }
        if rest == ["prefs"] || rest == ["prefs", "list"] {
            let Some(document) = tables.prefs.get(owner).and_then(Value::as_str) else {
                return Response::error(500, "rex: plug-in has no preferences");
            };
            return page_framework_document(document, query);
        }
        Response::bare(404)
    }

    /// A framework container: `noHistory`/`replaceParent` lead, it pages with `totalSize`
    /// first and no `offset`, and it names its window in the response headers.
    fn framework_container(
        &self,
        query: &HashMap<String, String>,
        rows: Vec<Value>,
        element: &str,
        noise_first: bool,
    ) -> Response {
        let total = rows.len();
        let window = page_window(query);
        let page = match window {
            Some((start, size)) => rows.into_iter().skip(start).take(size).collect::<Vec<_>>(),
            None => rows,
        };
        let mut body = Map::new();
        if window.is_some() {
            body.insert("totalSize".to_string(), json!(total));
        }
        if noise_first {
            body.insert("noHistory".to_string(), json!(0));
            body.insert("replaceParent".to_string(), json!(0));
            body.insert("size".to_string(), json!(page.len()));
        } else {
            body.insert("size".to_string(), json!(page.len()));
            body.insert("noHistory".to_string(), json!(0));
            body.insert("replaceParent".to_string(), json!(0));
        }
        body.insert("identifier".to_string(), json!("com.plexapp.system"));
        if !page.is_empty() {
            body.insert(element.to_string(), Value::Array(page));
        }
        let mut response = system_body(&json!({"MediaContainer": body}));
        if let Some((start, _)) = window {
            response
                .headers
                .push(("X-Plex-Container-Start".to_string(), start.to_string()));
            response
                .headers
                .push(("X-Plex-Container-Total-Size".to_string(), total.to_string()));
        }
        response
    }

    fn system_library_sections(&self, query: &HashMap<String, String>) -> Response {
        let host = if self.private_address.is_empty() {
            "127.0.0.1".to_string()
        } else {
            self.private_address.clone()
        };
        let port = if self.private_port == 0 {
            32400
        } else {
            self.private_port
        };
        let titles = self
            .catalog
            .sections
            .iter()
            .map(|section| string_value(section.get("title").unwrap_or(&Value::Null)))
            .collect::<Vec<_>>();
        let mut sections = self.catalog.sections.clone();
        sections.sort_by_key(|section| {
            string_value(section.get("title").unwrap_or(&Value::Null)).to_lowercase()
        });
        let rows = sections
            .iter()
            .map(|section| {
                let key = string_value(section.get("key").unwrap_or(&Value::Null));
                let title = section.get("title").cloned().unwrap_or(Value::Null);
                let title_text = string_value(&title);
                let url = format!("http://{host}:{port}/library/sections/{key}");
                let encoded = base64_encode(url.as_bytes())
                    .replace('+', "-")
                    .replace('/', "_")
                    .replace('=', "_");
                json!({
                    "key": encoded,
                    "unique": if titles.iter().filter(|t| **t == title_text).count() == 1 { 1 } else { 0 },
                    "name": title,
                    "title": title,
                    "type": section.get("type").cloned().unwrap_or(Value::Null),
                    "serverVersion": VERSION,
                    "uuid": section.get("uuid").cloned().unwrap_or(Value::Null),
                    "host": host,
                    "serverName": self.friendly,
                    "path": format!("/library/sections/{key}"),
                    "machineIdentifier": self.machine,
                    "local": 1,
                    "port": port,
                })
            })
            .collect::<Vec<_>>();
        self.framework_container(query, rows, "Directory", true)
    }

    fn system_scanners(&self, type_id: &str) -> Response {
        let (size, names): (usize, &[&str]) = match type_id {
            "1" => (
                4,
                &[
                    "Plex Movie",
                    "Plex Movie Scanner",
                    "Plex Video Files Scanner",
                    "Plex Video Files",
                ],
            ),
            "2" => (2, &["Plex TV Series", "Plex Series Scanner"]),
            "8" | "9" => (3, &["Plex Music", "Plex Music Scanner"]),
            "13" => (1, &["Plex Photo Scanner"]),
            _ => return Response::error(500, "rex: internal error"),
        };
        // Cycle 87, measured on the 1.42.2 reference, plexlab and plexscratch42,
        // byte-identical on all three and under every Accept. The MUSIC list is written by
        // a different writer from the other three -- a DOUBLE-quoted declaration, and the
        // third row its `size="3"` counts serialised as an empty indented line -- 220 bytes
        // where the framework's own form of the same two rows is 217.
        let music = size > names.len();
        let quote = if music { '"' } else { '\'' };
        let rows: String = names
            .iter()
            .map(|name| format!("  <Scanner name=\"{name}\"/>\n"))
            .collect();
        let body = format!(
            "<?xml version={quote}1.0{quote} encoding={quote}utf-8{quote}?>\n\
             <MediaContainer noHistory=\"0\" replaceParent=\"0\" size=\"{size}\" \
             identifier=\"com.plexapp.system\">\n{rows}{}</MediaContainer>\n",
            if music { "  \n" } else { "" }
        );
        Response::framework_document(200, body.into_bytes())
    }

    fn media_flag(&self, relative: &str, tables: &FrameworkTables) -> Response {
        const TYPES: [&str; 10] = [
            "aspectRatio",
            "audioChannels",
            "audioCodec",
            "contentRating",
            "media",
            "recordLabel",
            "studio",
            "videoCodec",
            "videoFrameRate",
            "videoResolution",
        ];
        let (kind, value) = relative.split_once('/').unwrap_or((relative, ""));
        if !TYPES.contains(&kind) || value.is_empty() || value.matches('/').count() > 1 {
            return Response::bare(404);
        }
        let mut name = value.to_string();
        if let Some(rules) = tables.substitutions.get(kind) {
            if let Some((_, substitute)) = rules.iter().find(|(pattern, _)| pattern.is_match(value))
            {
                name = substitute.clone();
            }
        }
        let size = tables
            .flags
            .get("flags")
            .and_then(|flags| flags.get(kind))
            .and_then(|table| table.get(name.as_str()))
            .and_then(Value::as_array);
        let Some(size) = size else {
            return Response::asset("image/png", tables.flag_blank.clone(), false);
        };
        let width = size.first().and_then(Value::as_u64).unwrap_or(1) as u32;
        let height = size.get(1).and_then(Value::as_u64).unwrap_or(1) as u32;
        Response::asset(
            "image/png",
            generated_png(width, height, [32, 34, 38]),
            false,
        )
    }

    fn legacy_agent_document(rows: &[Value]) -> String {
        let head = format!(
            "<?xml version='1.0' encoding='utf-8'?>\n<MediaContainer noHistory=\"0\" replaceParent=\"0\" size=\"{}\" identifier=\"com.plexapp.system\"",
            rows.len()
        );
        if rows.is_empty() {
            return head + "/>\n";
        }
        let lines = rows
            .iter()
            .map(|row| {
                format!(
                    "  <Agent name={}{} hasPrefs=\"{}\" identifier={}/>",
                    xml_quote(&string_value(row.get("name").unwrap_or(&Value::Null))),
                    if row.get("enabled").and_then(Value::as_bool).unwrap_or(false) {
                        " enabled=\"1\""
                    } else {
                        ""
                    },
                    if row
                        .get("hasPrefs")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                    {
                        1
                    } else {
                        0
                    },
                    xml_quote(&string_value(row.get("identifier").unwrap_or(&Value::Null))),
                )
            })
            .collect::<Vec<_>>();
        format!("{head}>\n{}\n</MediaContainer>\n", lines.join("\n"))
    }

    fn legacy_agent_error(
        tables: &FrameworkTables,
        key: &str,
        values: &[(&str, String)],
    ) -> Response {
        let mut body = tables
            .legacy_config
            .get("errors")
            .and_then(|errors| errors.get(key))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        for (name, value) in values {
            body = body.replace(&format!("{{{name}}}"), &xml_text(value));
        }
        Response::plugin_xml(500, body.into_bytes())
    }

    /// `/system/agents/{identifier}/config/{mediaType}`, GET and PUT (cycle 58).
    fn legacy_agent_config(
        &mut self,
        method: &str,
        identifier: &str,
        media_type: &str,
        query: &HashMap<String, String>,
        tables: &FrameworkTables,
    ) -> Response {
        let Ok(number) = media_type.trim().parse::<i64>() else {
            return Self::legacy_agent_error(
                tables,
                if method == "PUT" {
                    "valueerror_put"
                } else {
                    "valueerror_config"
                },
                &[("value", python_repr(media_type))],
            );
        };
        let Some(media_class) = legacy_media_class(number) else {
            return Response::bare(404);
        };
        let order_key = format!("_agentOrder:{identifier}|{media_class}");
        if method == "PUT" {
            let Some(order) = query.get("order") else {
                return Response::plugin_xml(
                    500,
                    framework_traceback(
                        "set_agent_configuration() takes exactly 4 arguments (3 given)",
                    ),
                );
            };
            if order.is_empty() {
                return Self::legacy_agent_error(tables, "order_empty", &[]);
            }
            let order = order.split(',').map(str::to_string).collect::<Vec<_>>();
            self.prefs.insert(
                order_key,
                Value::Array(order.iter().map(|name| json!(name)).collect()),
            );
            self.dirty = true;
            let sources = order
                .iter()
                .map(|ident| format!("    <agent>{}</agent>\n", xml_text(ident)))
                .collect::<String>();
            let document = format!(
                "<?xml version='1.0' encoding='utf-8'?>\n<combine class={}>\n  <sources>\n{sources}  </sources>\n</combine>\n",
                xml_quote(media_class)
            );
            return Response::framework_string(200, document.into_bytes(), "application/xml");
        }
        if number == 20 {
            return Self::legacy_agent_error(tables, "personal_media", &[]);
        }
        let default = tables
            .legacy_config
            .get("config")
            .and_then(|config| config.get(format!("{identifier}|{number}").as_str()))
            .and_then(Value::as_array);
        let Some(default) = default else {
            if matches!(number, 8 | 9) && !identifier.starts_with("com.plexapp.agents.") {
                return Self::legacy_agent_error(
                    tables,
                    &format!("keyerror_{number}"),
                    &[("identifier", identifier.to_string())],
                );
            }
            return Response::framework_document(
                200,
                Self::legacy_agent_document(&[]).into_bytes(),
            );
        };
        let mut agents = default
            .iter()
            .map(|row| {
                (
                    string_value(row.get("identifier").unwrap_or(&Value::Null)),
                    row.clone(),
                )
            })
            .collect::<Vec<_>>();
        let mut order = self
            .prefs
            .get(&order_key)
            .and_then(Value::as_array)
            .map(|names| names.iter().map(string_value).collect::<Vec<_>>())
            .unwrap_or_else(|| {
                default
                    .iter()
                    .filter(|row| row.get("enabled").and_then(Value::as_bool).unwrap_or(false))
                    .map(|row| string_value(row.get("identifier").unwrap_or(&Value::Null)))
                    .collect()
            });
        if !order.iter().any(|ident| ident == identifier) {
            order.push(identifier.to_string());
        }
        let mut rows = Vec::new();
        for ident in &order {
            if let Some(index) = agents.iter().position(|(id, _)| id == ident) {
                let (_, mut row) = agents.remove(index);
                if let Some(object) = row.as_object_mut() {
                    object.insert("enabled".to_string(), json!(true));
                }
                rows.push(row);
            }
        }
        agents.sort_by(|a, b| a.0.cmp(&b.0));
        for (_, mut row) in agents {
            if let Some(object) = row.as_object_mut() {
                object.shift_remove("enabled");
            }
            rows.push(row);
        }
        Response::framework_document(200, Self::legacy_agent_document(&rows).into_bytes())
    }

    fn legacy_agent_contributors(
        &self,
        media_type: &str,
        primary: &str,
        query: &HashMap<String, String>,
        tables: &FrameworkTables,
    ) -> Response {
        let Ok(number) = media_type.trim().parse::<i64>() else {
            return Self::legacy_agent_error(
                tables,
                "valueerror_contributors",
                &[("value", python_repr(media_type))],
            );
        };
        if legacy_media_class(number).is_none() {
            return Response::bare(404);
        }
        let rows = tables
            .legacy_config
            .get("contributors")
            .and_then(|c| c.get(format!("{primary}|{number}").as_str()))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        page_framework_document(&Self::legacy_agent_document(&rows), query)
    }

    /// `/system/agents` -- PMS's own handler (no cookie, the core writer), listing the
    /// eleven legacy plug-in agents before the modern four (cycles 25-56).
    fn system_agents(
        &self,
        request: &Request,
        query: &HashMap<String, String>,
        tables: &FrameworkTables,
    ) -> Response {
        let mut wanted = query
            .get("mediaType")
            .filter(|value| !value.is_empty())
            .cloned();
        if let Some(value) = &wanted {
            let digits = value.strip_prefix('+').unwrap_or(value);
            let parsed = if let Some(negative) = digits.strip_prefix('-') {
                negative.parse::<i64>().ok().map(|n| -n)
            } else {
                digits.parse::<i64>().ok()
            };
            match parsed {
                None => return Response::error(400, "rex: bad request"),
                Some(-1) => wanted = None,
                Some(n) => wanted = Some(n.to_string()),
            }
        }
        let empty = Vec::new();
        let modern_all = tables
            .system_agents
            .get("agents")
            .and_then(Value::as_array)
            .unwrap_or(&empty);
        let modern = match &wanted {
            Some(kind) => tables
                .system_agents
                .get("mediaTypes")
                .and_then(|types| types.get(kind.as_str()))
                .and_then(Value::as_array)
                .map(|names| {
                    names
                        .iter()
                        .filter_map(|name| {
                            modern_all
                                .iter()
                                .find(|agent| agent.get("identifier") == Some(name))
                                .cloned()
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default(),
            None => modern_all.clone(),
        };
        // Cycle 87, plexscratch42: a modern agent whose BUILT-IN group a client deleted
        // is gone from both forms (group N is the N-th modern agent).
        let gone = modern_all
            .iter()
            .enumerate()
            .filter(|(index, _)| self.deleted_builtin_groups.contains(&(*index as i64 + 1)))
            .filter_map(|(_, agent)| agent.get("identifier").cloned())
            .collect::<Vec<_>>();
        let modern = modern
            .into_iter()
            .filter(|agent| agent.get("identifier").is_none_or(|id| !gone.contains(id)))
            .collect::<Vec<_>>();
        let legacy = match &wanted {
            Some(kind) => tables
                .legacy_agents
                .get("byType")
                .and_then(|types| types.get(kind.as_str()))
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .map(|row| {
                            let mut object = row
                                .get("attrs")
                                .and_then(Value::as_object)
                                .cloned()
                                .unwrap_or_default();
                            object.insert(
                                "Language".to_string(),
                                row.get("Language").cloned().unwrap_or(Value::Null),
                            );
                            Value::Object(object)
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default(),
            None => tables
                .legacy_agents
                .get("bare")
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .map(|row| {
                            let mut object = row
                                .get("attrs")
                                .and_then(Value::as_object)
                                .cloned()
                                .unwrap_or_default();
                            if row
                                .get("MediaType")
                                .and_then(Value::as_array)
                                .is_some_and(|m| !m.is_empty())
                            {
                                object.insert("MediaType".to_string(), row["MediaType"].clone());
                            }
                            Value::Object(object)
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default(),
        };
        let language = request_language(query, &request.headers).unwrap_or_default();
        let languages = tables
            .agent_languages
            .get("languages")
            .and_then(|all| all.get(language.as_str()).or_else(|| all.get("")))
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let modern = modern
            .into_iter()
            .map(|agent| {
                let mut object = agent.as_object().cloned().unwrap_or_default();
                if !languages.is_empty() {
                    let rows = languages
                        .iter()
                        .filter_map(|row| {
                            let row = row.as_array()?;
                            let mut entry = Map::new();
                            entry.insert("code".to_string(), row.first()?.clone());
                            entry.insert("title".to_string(), row.get(1)?.clone());
                            if row.len() > 2 {
                                entry.insert("default".to_string(), json!(true));
                            }
                            Some(Value::Object(entry))
                        })
                        .collect::<Vec<_>>();
                    object.insert("Language".to_string(), Value::Array(rows));
                }
                Value::Object(object)
            })
            .collect::<Vec<_>>();
        let agents = legacy.into_iter().chain(modern).collect::<Vec<_>>();
        let container = json!({"MediaContainer": {"size": agents.len(), "Agent": agents}});
        let ordered = xmlout::ordered(&container, None);
        if accept_wants_xml(
            request
                .headers
                .get("accept")
                .map(String::as_str)
                .unwrap_or(""),
        ) {
            // Cycle 83, the 1.42.2 reference and plexlab: the XML pages like the JSON --
            // Start 1 Size 2 answers `size 2 totalSize 5 offset 1`, the second and third
            // agents and both X-Plex-Container headers. This XML is written here rather
            // than by the pipeline, so it went out past the pager: every agent, no
            // `totalSize`, and python-plexapi's `agents()` read `totalSize`/`offset` as
            // None. Page the JSON first, the way the pipeline pages every other container,
            // then render it.
            let mut paged = Response::bytes(200, "application/json");
            paged.body = serde_json::to_vec(&ordered).unwrap_or_default();
            page_container(&mut paged, query, "/system/agents");
            if paged.status != 200 {
                return paged;
            }
            let document = serde_json::from_slice::<Value>(&paged.body).unwrap_or(ordered);
            let mut response = Response::bytes(200, "text/xml;charset=utf-8");
            response.body = xmlout::to_xml(&document, None);
            response
                .headers
                .push(("Cache-Control".to_string(), "no-cache".to_string()));
            let mut headed = Response::bytes(200, "application/json");
            headed.body = paged.body;
            add_paging_headers(&mut headed, query);
            response.headers.extend(headed.headers);
            return response;
        }
        // Cycle 87, the 1.42.2 reference under Plex Web's paging headers: a paged JSON
        // answer repeats every `Language` key exactly as the unpaged one does. This body
        // used to reach the pipeline's pager already rewritten into repeated keys; the
        // pager parsed it, which keeps only the LAST of each, and wrote one `Language` per
        // agent back. Page the array form here, the way the XML branch does, and repeat the
        // keys after: a container that carries `totalSize` is one the pipeline's pager
        // leaves alone.
        let mut response = Response::bytes(200, "application/json");
        response.body = serde_json::to_vec(&drop_empty_value(&ordered)).unwrap_or_default();
        page_container(&mut response, query, "/system/agents");
        if response.status != 200 {
            return response;
        }
        let text = String::from_utf8(std::mem::take(&mut response.body)).unwrap_or_default();
        response.body = repeat_keys(text).into_bytes();
        response
            .headers
            .push(("Cache-Control".to_string(), "no-cache".to_string()));
        response
    }
}

/// `page_window` from `server.py`: both parameters, non-negative integers.
fn page_window(query: &HashMap<String, String>) -> Option<(usize, usize)> {
    let start = query
        .get("X-Plex-Container-Start")?
        .trim()
        .parse::<i64>()
        .ok()?;
    let size = query
        .get("X-Plex-Container-Size")?
        .trim()
        .parse::<i64>()
        .ok()?;
    if start < 0 || size < 0 {
        return None;
    }
    Some((start as usize, size as usize))
}

/// A framework container in the client's format: the framework serialiser for XML
/// (`application/xml`, no charset) and ordinary JSON otherwise.
/// Cycle 87, measured on the 1.42.2 reference, plexlab and plexscratch42 with `Accept:
/// application/json`: the framework's serialiser has no JSON form, so `/system`,
/// `/system/search`, `/system/library/sections` and `/system/scanners/{N}` answer the same
/// `application/xml` document whatever Accept says -- only the bare `/system/agents`,
/// which PMS implements itself, answers JSON. rex answered JSON to all four.
fn system_body(container: &Value) -> Response {
    Response::framework_document(200, xmlout::to_system_xml(container, None))
}

/// The supplied query names in the order the request line spelled them.
fn names_in_request_order(request: &Request, names: &[String]) -> Vec<String> {
    let (_, raw) = split_target(&request.target);
    let mut ordered = Vec::new();
    for (key, _) in parse_query(&raw) {
        if names.contains(&key) && !ordered.contains(&key) {
            ordered.push(key);
        }
    }
    for name in names {
        if !ordered.contains(name) {
            ordered.push(name.clone());
        }
    }
    ordered
}
