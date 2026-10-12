//! rex's side of plex.tv: `claim.py`, `heartbeat.py` and `accountstate.py`.
//!
//! The Python modules are the oracle, request for request: the same hosts, paths,
//! query order, headers in the order they are set, the same claim file beside the
//! catalog, the same threads and cadences and the same log lines. The docs for WHY
//! each request is shaped as it is live on the Python side and in docs/findings.md;
//! this file cites them rather than repeating them.
//!
//! Every URL goes through [`plextv_url`], which `REX_PLEXTV_URL` redirects to a
//! local stub (`tests/test_plextv.py`): these flows WRITE to a plex.tv account, and a
//! test identity claiming or announcing against the real one is what reset the
//! account's tokens on 2026-09-11.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};

use super::logging::{error, info, warning};
use super::upstream;
use super::SharedRuntime;

const EXCHANGE_URL: &str = "https://plex.tv/api/claim/exchange";
const CONNECTIVITY_URL: &str = "https://plex.tv/api/servers/%s/connectivity";
const FEATURES_URL: &str = "https://servers.plex.tv/api/v2/features";
const ACCOUNT_URL: &str = "https://servers.plex.tv/api/v2/user";
const ANNOUNCE_BASE: &str = "https://servers.plex.tv";
const RESOURCES_URL: &str =
    "https://plex.tv/api/v2/resources?includeHttps=1&includeRelay=1&includeIPv6=1";
const CONSENT_URL: &str = "https://servers.plex.tv/api/v2/server/users/%s/view_state_sync";
const STATE_URL: &str = "https://scrobbles.plex.tv/state/%s";
const SERVICES_URL: &str = "https://servers.plex.tv/api/v2/server/users/services";
const SERVERS_URL: &str = "https://plex.tv/servers.xml";
const PUBSUB_URL: &str = "https://pubsub.plex.tv/sub/eventsource/%s/%s";
const ACCESS_TOKENS_URL: &str = "https://plex.tv/api/v2/server/access_tokens";
/// `claim.ACCESS_REFRESH_WINDOW`: zen's 1.42.2 PMS re-reads the access-token list for an
/// unknown token unless its last read finished about 6 s ago or less
/// (tests/fixtures/plextv_access_tokens_s1422.json).
const ACCESS_REFRESH_WINDOW: Duration = Duration::from_secs(6);
/// `claim.PUBSUB_TIMEOUT` / `PUBSUB_RETRY` (the probe PMS, measured).
const PUBSUB_TIMEOUT: u64 = 20;
const PUBSUB_RETRY: u64 = 15;

/// `heartbeat.DEFAULT_INTERVAL`: a registration lease is ~40 minutes (measured).
pub(crate) const HEARTBEAT_INTERVAL: u64 = 600;
/// `accountstate.DEFAULT_INTERVAL`.
pub(crate) const ACCOUNT_STATE_INTERVAL: u64 = 1800;
const ACCOUNT_REFRESH_INTERVAL: i64 = 86400;
/// Cycle 47: the owner read and the feature read are 10.8--11.45 s apart.
const ACCOUNT_FEATURE_DELAY: u64 = 11;
pub(crate) const DECRYPT_SECRET_ENV: &str = "REX_SCROBBLING_DECRYPT_SECRET_B64";
const PLEXTV_URL_ENV: &str = "REX_PLEXTV_URL";

/// `claim.plextv_url`: unset is the real service; set, `<base>/<host><path>`.
pub(crate) fn plextv_url(url: &str) -> String {
    match std::env::var(PLEXTV_URL_ENV) {
        Ok(base) if !base.is_empty() => format!(
            "{}/{}",
            base.trim_end_matches('/'),
            url.split_once("://").map_or(url, |(_, rest)| rest)
        ),
        _ => url.to_string(),
    }
}

fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}

// -- urllib ------------------------------------------------------------------

/// `urllib.parse.urlencode` over ordered pairs.
fn urlencode(pairs: &[(&str, String)]) -> String {
    pairs
        .iter()
        .map(|(name, value)| {
            format!(
                "{}={}",
                upstream::quote_plus(name),
                upstream::quote_plus(value)
            )
        })
        .collect::<Vec<_>>()
        .join("&")
}

fn reason(status: u16) -> &'static str {
    match status {
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        409 => "Conflict",
        422 => "Unprocessable Entity",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        504 => "Gateway Timeout",
        _ => "",
    }
}

/// An `HTTPError` keeps its status and body; anything else is a transport failure.
#[derive(Debug)]
enum Failure {
    Http(u16, Vec<u8>),
    Other(String),
}

impl std::fmt::Display for Failure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Failure::Http(status, _) => {
                write!(formatter, "HTTP Error {status}: {}", reason(*status))
            }
            Failure::Other(message) => formatter.write_str(message),
        }
    }
}

/// `urllib.request.urlopen(...).read()`: a status of 400 or more raises.
fn urlopen(
    method: &str,
    url: &str,
    headers: &[(&str, String)],
    body: Option<&[u8]>,
    timeout: u64,
) -> Result<(u16, Vec<u8>), Failure> {
    let (status, response_headers, payload) = upstream::fetch(
        method,
        &plextv_url(url),
        headers,
        body.unwrap_or(b""),
        Duration::from_secs(timeout),
    )
    .map_err(|error| Failure::Other(format!("<urlopen error {error}>")))?;
    if status >= 400 {
        return Err(Failure::Http(status, payload));
    }
    Ok((status, inflate(&response_headers, payload).map_err(Failure::Other)?))
}

/// `claim.read_body`: plex.tv gzips its answer whenever a PMS's `Accept-Encoding: gzip`
/// asks for it (the device PUT and the claim exchange both, in the fixture).
fn inflate(headers: &[(String, String)], payload: Vec<u8>) -> Result<Vec<u8>, String> {
    let gzipped = headers.iter().any(|(name, value)| {
        name.eq_ignore_ascii_case("content-encoding") && value.eq_ignore_ascii_case("gzip")
    });
    if !gzipped {
        return Ok(payload);
    }
    let mut out = Vec::new();
    std::io::Read::read_to_end(&mut flate2::read::GzDecoder::new(payload.as_slice()), &mut out)
        .map_err(|error| error.to_string())?;
    Ok(out)
}

/// `repr(bytes)`, which is what `"%s" % body` prints for a response body.
fn bytes_repr(bytes: &[u8]) -> String {
    let quote = if bytes.contains(&b'\'') && !bytes.contains(&b'"') {
        '"'
    } else {
        '\''
    };
    let mut out = format!("b{quote}");
    for &byte in bytes {
        match byte {
            b'\\' => out.push_str("\\\\"),
            b'\n' => out.push_str("\\n"),
            b'\r' => out.push_str("\\r"),
            b'\t' => out.push_str("\\t"),
            _ if byte as char == quote => {
                out.push('\\');
                out.push(quote);
            }
            0x20..=0x7e => out.push(byte as char),
            _ => out.push_str(&format!("\\x{byte:02x}")),
        }
    }
    out.push(quote);
    out
}

// -- ElementTree, as far as these documents need it ----------------------------

#[derive(Debug, Clone)]
struct Element {
    name: String,
    attrs: HashMap<String, String>,
    children: Vec<Element>,
}

impl Element {
    fn get(&self, name: &str) -> Option<&str> {
        self.attrs.get(name).map(String::as_str)
    }

    /// `Element.iter(tag)`: this element and every descendant, document order.
    fn iter<'a>(&'a self, tag: &str, out: &mut Vec<&'a Element>) {
        if self.name == tag {
            out.push(self);
        }
        for child in &self.children {
            child.iter(tag, out);
        }
    }

    fn all(&self, tag: &str) -> Vec<&Element> {
        let mut out = Vec::new();
        self.iter(tag, &mut out);
        out
    }
}

fn unescape(text: &str) -> Result<String, String> {
    let mut out = String::new();
    let mut rest = text;
    while let Some(index) = rest.find('&') {
        out.push_str(&rest[..index]);
        let end = rest[index..]
            .find(';')
            .ok_or_else(|| "not well-formed (invalid token)".to_string())?;
        let entity = &rest[index + 1..index + end];
        match entity {
            "amp" => out.push('&'),
            "lt" => out.push('<'),
            "gt" => out.push('>'),
            "quot" => out.push('"'),
            "apos" => out.push('\''),
            _ => {
                let code = if let Some(hex) = entity.strip_prefix("#x") {
                    u32::from_str_radix(hex, 16).ok()
                } else if let Some(decimal) = entity.strip_prefix('#') {
                    decimal.parse().ok()
                } else {
                    None
                };
                out.push(
                    code.and_then(char::from_u32)
                        .ok_or_else(|| format!("undefined entity &{entity};"))?,
                );
            }
        }
        rest = &rest[index + end + 1..];
    }
    out.push_str(rest);
    Ok(out)
}

/// `ET.fromstring`: the root element, or the parse error that raises there.
fn parse_xml(bytes: &[u8]) -> Result<Element, String> {
    let text = std::str::from_utf8(bytes).map_err(|_| "not well-formed (invalid token)")?;
    let mut stack: Vec<Element> = Vec::new();
    let mut root = None;
    let mut rest = text;
    loop {
        let Some(start) = rest.find('<') else {
            if !rest.trim().is_empty() && root.is_some() {
                return Err("junk after document element".to_string());
            }
            break;
        };
        if root.is_some() && !rest[..start].trim().is_empty() {
            return Err("junk after document element".to_string());
        }
        rest = &rest[start..];
        if rest.starts_with("<?") {
            let end = rest.find("?>").ok_or("unclosed token")?;
            rest = &rest[end + 2..];
            continue;
        }
        if rest.starts_with("<!--") {
            let end = rest.find("-->").ok_or("unclosed token")?;
            rest = &rest[end + 3..];
            continue;
        }
        if rest.starts_with("<!") {
            let end = rest.find('>').ok_or("unclosed token")?;
            rest = &rest[end + 1..];
            continue;
        }
        let end = find_tag_end(rest).ok_or("unclosed token")?;
        let tag = &rest[1..end];
        rest = &rest[end + 1..];
        if let Some(name) = tag.strip_prefix('/') {
            let element = stack.pop().ok_or("not well-formed (invalid token)")?;
            if element.name != name.trim() {
                return Err("mismatched tag".to_string());
            }
            match stack.last_mut() {
                Some(parent) => parent.children.push(element),
                None => root = Some(element),
            }
            continue;
        }
        if root.is_some() {
            return Err("junk after document element".to_string());
        }
        let closed = tag.ends_with('/');
        let body = tag.trim_end_matches('/');
        let name_end = body.find(|c: char| c.is_whitespace()).unwrap_or(body.len());
        let name = &body[..name_end];
        if name.is_empty() {
            return Err("not well-formed (invalid token)".to_string());
        }
        let mut attrs = HashMap::new();
        let mut rest_attrs = body[name_end..].trim_start();
        while !rest_attrs.is_empty() {
            let equals = rest_attrs
                .find('=')
                .ok_or("not well-formed (invalid token)")?;
            let key = rest_attrs[..equals].trim();
            let after = rest_attrs[equals + 1..].trim_start();
            let quote = after
                .chars()
                .next()
                .ok_or("not well-formed (invalid token)")?;
            if quote != '"' && quote != '\'' {
                return Err("not well-formed (invalid token)".to_string());
            }
            let close = after[1..]
                .find(quote)
                .ok_or("not well-formed (invalid token)")?;
            attrs.insert(key.to_string(), unescape(&after[1..1 + close])?);
            rest_attrs = after[close + 2..].trim_start();
        }
        let element = Element {
            name: name.to_string(),
            attrs,
            children: Vec::new(),
        };
        if closed {
            match stack.last_mut() {
                Some(parent) => parent.children.push(element),
                None => root = Some(element),
            }
        } else {
            stack.push(element);
        }
    }
    if !stack.is_empty() {
        return Err("no element found".to_string());
    }
    root.ok_or_else(|| "no element found".to_string())
}

fn find_tag_end(text: &str) -> Option<usize> {
    let mut quote = None;
    for (index, c) in text.char_indices().skip(1) {
        match (quote, c) {
            (None, '"' | '\'') => quote = Some(c),
            (Some(open), _) if c == open => quote = None,
            (None, '>') => return Some(index),
            _ => {}
        }
    }
    None
}

// -- claim.py --------------------------------------------------------------------

/// `claim.Claim`: the claimed identity, persisted in `claim_state.json`.
#[derive(Debug, Clone)]
pub(crate) struct Claim {
    pub machine: String,
    pub name: String,
    pub version: String,
    path: PathBuf,
    pub token: Option<String>,
    pub owner: Option<String>,
    pub email: Option<String>,
    pub feature_uuids: Vec<String>,
    pub account_id: Option<i64>,
    pub view_state_cursor: Option<String>,
}

pub(crate) type SharedClaim = Arc<Mutex<Claim>>;

fn optional_string(value: Option<&Value>) -> Option<String> {
    match value {
        Some(Value::String(text)) => Some(text.clone()),
        _ => None,
    }
}

impl Claim {
    pub(crate) fn load(machine: &str, path: PathBuf, name: &str, version: &str) -> Self {
        let mut claim = Self {
            machine: machine.to_string(),
            name: name.to_string(),
            version: version.to_string(),
            path,
            token: None,
            owner: None,
            email: None,
            feature_uuids: Vec::new(),
            account_id: None,
            view_state_cursor: None,
        };
        if !claim.path.exists() {
            return claim;
        }
        let state = std::fs::read(&claim.path)
            .map_err(|error| error.to_string())
            .and_then(|bytes| {
                serde_json::from_slice::<Value>(&bytes).map_err(|error| error.to_string())
            });
        match state {
            Ok(state) if state.get("machine").and_then(Value::as_str) == Some(machine) => {
                claim.token = optional_string(state.get("token"));
                claim.owner = optional_string(state.get("owner"));
                claim.email = optional_string(state.get("email"));
                let mut uuids = state
                    .get("feature_uuids")
                    .and_then(Value::as_array)
                    .map(|rows| {
                        rows.iter()
                            .filter_map(Value::as_str)
                            .map(str::to_string)
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default();
                uuids.sort();
                uuids.dedup();
                claim.feature_uuids = uuids;
                claim.account_id = state.get("account_id").and_then(Value::as_i64);
                claim.view_state_cursor = optional_string(state.get("view_state_cursor"));
                info!(
                    "loaded claim for {} (owner {})",
                    claim.machine,
                    claim.owner.as_deref().unwrap_or("None")
                );
            }
            Ok(_) => {
                // A different machineIdentifier is a different server; its claim does
                // not transfer.
                warning!("stored claim is for another machineIdentifier; ignoring");
            }
            Err(message) => error!("could not read claim state: {message}"),
        }
        claim
    }

    pub(crate) fn claimed(&self) -> bool {
        self.token.as_deref().is_some_and(|token| !token.is_empty())
    }

    /// `json.dump` of the state, in its key order and separators, then `chmod 0600`.
    fn save(&self) {
        let string = |value: &Option<String>| {
            value
                .as_ref()
                .map_or_else(|| "null".to_string(), |text| python_json_string(text))
        };
        let document = format!(
            "{{\"machine\": {}, \"token\": {}, \"owner\": {}, \"email\": {}, \"feature_uuids\": [{}], \"account_id\": {}, \"view_state_cursor\": {}}}",
            python_json_string(&self.machine),
            string(&self.token),
            string(&self.owner),
            string(&self.email),
            self.feature_uuids
                .iter()
                .map(|uuid| python_json_string(uuid))
                .collect::<Vec<_>>()
                .join(", "),
            self.account_id
                .map_or_else(|| "null".to_string(), |id| id.to_string()),
            string(&self.view_state_cursor),
        );
        // The claim holds the server's token: owner-only on Unix. A Windows profile's
        // folders are already private to their user.
        let result = std::fs::write(&self.path, document).and_then(|_| {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&self.path, std::fs::Permissions::from_mode(0o600))?;
            }
            Ok(())
        });
        if let Err(message) = result {
            error!("could not persist claim state: {message}");
        }
    }

    /// `Claim._headers`, in its order.
    fn headers(&self) -> Vec<(&'static str, String)> {
        vec![
            ("X-Plex-Client-Identifier", self.machine.clone()),
            ("X-Plex-Product", "Plex Media Server".to_string()),
            ("X-Plex-Version", self.version.clone()),
            ("X-Plex-Platform", "Linux".to_string()),
            ("X-Plex-Platform-Version", "6.1".to_string()),
            ("X-Plex-Device", "PC".to_string()),
            ("X-Plex-Device-Name", self.name.clone()),
            ("X-Plex-Provides", "server".to_string()),
            ("Accept", "application/xml".to_string()),
        ]
    }

    /// `Claim.pms_headers`: what a real PMS sends on every claim and registration call,
    /// in its order (tests/fixtures/plextv_registration_s1422.json).
    fn pms_headers(&self, agent: &str, accept_encoding: bool) -> Vec<(&'static str, String)> {
        let mut headers = vec![
            ("User-Agent", format!("{agent}/{}", self.version)),
            ("Accept", "*/*".to_string()),
        ];
        if accept_encoding {
            headers.push(("Accept-Encoding", "gzip".to_string()));
        }
        headers.extend([
            ("X-Plex-Client-Identifier", self.machine.clone()),
            ("X-Plex-Version", self.version.clone()),
            ("X-Plex-Provides", "server".to_string()),
            ("X-Plex-Platform", "Linux".to_string()),
            ("X-Plex-Product", "Plex Media Server".to_string()),
            ("X-Plex-Platform-Version", "6.1".to_string()),
            ("X-Plex-Device-Name", self.name.clone()),
            ("X-Plex-Device", "PC".to_string()),
            ("X-Plex-Model", "x86_64".to_string()),
            ("X-Plex-Device-Vendor", "rex".to_string()),
        ]);
        headers
    }

    /// `owner_features`: the named defaults unioned with the account's UUIDs, sorted.
    pub(crate) fn owner_features(&self, named: &str) -> String {
        let mut values = named
            .split(',')
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .collect::<Vec<_>>();
        values.extend(self.feature_uuids.iter().cloned());
        values.sort();
        values.dedup();
        values.join(",")
    }
}

/// `json.dumps` of a string: ASCII only, `\uXXXX` for the rest.
fn python_json_string(text: &str) -> String {
    let mut out = String::from("\"");
    for unit in text.encode_utf16() {
        match unit {
            0x22 => out.push_str("\\\""),
            0x5c => out.push_str("\\\\"),
            0x0a => out.push_str("\\n"),
            0x0d => out.push_str("\\r"),
            0x09 => out.push_str("\\t"),
            0x08 => out.push_str("\\b"),
            0x0c => out.push_str("\\f"),
            0x20..=0x7e => out.push(unit as u8 as char),
            _ => out.push_str(&format!("\\u{unit:04x}")),
        }
    }
    out.push('"');
    out
}

fn snapshot(claim: &SharedClaim) -> Claim {
    claim
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone()
}

/// `Claim.exchange`: redeem a `claim-…` token for this server's own token.
pub(crate) fn exchange(claim: &SharedClaim, claim_token: &str) -> Result<(), String> {
    let current = snapshot(claim);
    let url = format!(
        "{EXCHANGE_URL}?{}",
        urlencode(&[("token", claim_token.to_string())])
    );
    let headers = current.pms_headers("PlexMediaServer", true);
    let (_, body) =
        urlopen("POST", &url, &headers, Some(b""), 30).map_err(|failure| failure.to_string())?;
    let root = parse_xml(&body)?;
    let token = root
        .get("authToken")
        .filter(|token| !token.is_empty())
        .or_else(|| root.get("authenticationToken"))
        .filter(|token| !token.is_empty())
        .ok_or_else(|| "claim exchange returned no authToken".to_string())?
        .to_string();
    let owner = {
        let mut state = claim
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.token = Some(token);
        state.owner = root.get("username").map(str::to_string);
        state.email = root.get("email").map(str::to_string);
        // The pubsub path names the account; the exchange answer already carries it.
        if let Some(id) = root
            .get("id")
            .filter(|id| !id.is_empty() && id.bytes().all(|byte| byte.is_ascii_digit()))
            .and_then(|id| id.parse::<i64>().ok())
        {
            state.account_id = Some(id);
        }
        state.save();
        state.owner.clone()
    };
    info!(
        "CLAIMED by {}; server token stored",
        owner.as_deref().unwrap_or("None")
    );
    Ok(())
}

fn fetch_features(claim: &Claim) -> Result<Vec<String>, String> {
    let Some(token) = claim.token.clone().filter(|token| !token.is_empty()) else {
        return Ok(Vec::new());
    };
    let url = format!("{FEATURES_URL}?{}", urlencode(&[("X-Plex-Token", token)]));
    let (_, body) =
        urlopen("GET", &url, &claim.headers(), None, 30).map_err(|failure| failure.to_string())?;
    let root = parse_xml(&body)?;
    let mut uuids = root
        .all("feature")
        .into_iter()
        .filter_map(|node| node.get("uuid").filter(|uuid| !uuid.is_empty()))
        .map(str::to_string)
        .collect::<Vec<_>>();
    uuids.sort();
    uuids.dedup();
    Ok(uuids)
}

fn fetch_account_id(claim: &Claim) -> Result<Option<i64>, String> {
    let Some(token) = claim.token.clone().filter(|token| !token.is_empty()) else {
        return Ok(None);
    };
    let url = format!(
        "{ACCOUNT_URL}?{}",
        urlencode(&[
            ("includeSubscriptions", "1".to_string()),
            ("includeProviders", "1".to_string())
        ])
    );
    let mut headers = claim.headers();
    headers.push(("X-Plex-Token", token));
    let (_, body) =
        urlopen("GET", &url, &headers, None, 30).map_err(|failure| failure.to_string())?;
    let root = parse_xml(&body)?;
    root.get("id")
        .filter(|id| !id.is_empty() && id.bytes().all(|byte| byte.is_ascii_digit()))
        .and_then(|id| id.parse::<i64>().ok())
        .map(Some)
        .ok_or_else(|| "account response returned no numeric id".to_string())
}

/// `ensure_account_id`: acquire and persist the owner id an older claim file lacks.
fn ensure_account_id(claim: &SharedClaim) -> Result<Option<i64>, String> {
    let current = snapshot(claim);
    if current.account_id.is_some() || !current.claimed() {
        return Ok(current.account_id);
    }
    let fetched = fetch_account_id(&current)?;
    let mut state = claim
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if state.account_id.is_none() {
        state.account_id = fetched;
        state.save();
    }
    Ok(state.account_id)
}

pub(super) fn uuid4() -> String {
    let mut bytes = [0_u8; 16];
    if let Ok(mut random) = std::fs::File::open("/dev/urandom") {
        use std::io::Read;
        let _ = random.read_exact(&mut bytes);
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let hex = bytes
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    )
}

/// `announce_connectivity`: `PUT /api/servers/{id}/connectivity`, a real PMS asking plex.tv
/// to test its reachability under an asyncIdentifier of its own; the verdict comes back
/// over pubsub. `(status, body[:200])`; an HTTP error is an answer, a transport failure
/// is not.
pub(crate) fn announce_connectivity(
    claim: &SharedClaim,
    async_identifier: &str,
) -> Result<(String, String), String> {
    let current = snapshot(claim);
    let Some(token) = current.token.clone().filter(|token| !token.is_empty()) else {
        return Ok(("None".to_string(), "not claimed".to_string()));
    };
    let url = format!(
        "{}?{}",
        CONNECTIVITY_URL.replace("%s", &current.machine),
        urlencode(&[
            ("X-Plex-Token", token),
            ("asyncIdentifier", async_identifier.to_string())
        ])
    );
    let clip = |body: &[u8]| bytes_repr(&body[..body.len().min(200)]);
    let headers = current.pms_headers("PlexMediaServer", true);
    match urlopen("PUT", &url, &headers, Some(b""), 30) {
        Ok((status, body)) => Ok((status.to_string(), clip(&body))),
        Err(Failure::Http(status, body)) => Ok((status.to_string(), clip(&body))),
        Err(Failure::Other(message)) => Err(message),
    }
}

/// `Claim.fetch_access_tokens`: every token plex.tv lets reach this server -- its own, the
/// owner's devices' and shared users' -- one `<token>` row each; `tokenDisabled="1"` is not
/// let in.
fn fetch_access_tokens(claim: &Claim, unknown: Option<&str>) -> Result<HashSet<String>, String> {
    let token = claim.token.clone().unwrap_or_default();
    let mut pairs = vec![("auth_token", token.clone())];
    if let Some(unknown) = unknown.filter(|unknown| !unknown.is_empty()) {
        pairs.push(("unknownToken", unknown.to_string()));
    }
    let url = format!("{ACCESS_TOKENS_URL}?{}", urlencode(&pairs));
    let mut headers = claim.pms_headers("PlexMediaServer", true);
    headers.push(("X-Plex-Token", token));
    let (_, body) =
        urlopen("GET", &url, &headers, None, 30).map_err(|failure| failure.to_string())?;
    Ok(parse_xml(&body)?
        .all("token")
        .into_iter()
        .filter(|row| row.get("tokenDisabled") != Some("1"))
        .filter_map(|row| row.get("token").filter(|token| !token.is_empty()))
        .map(str::to_string)
        .collect())
}

/// The tokens plex.tv lets reach this server, as last read (`Claim.access_tokens`), and when
/// that read finished. In memory only: a claimed start reads the list again.
#[derive(Debug, Default)]
pub(crate) struct Access {
    tokens: Mutex<HashSet<String>>,
    /// Held for a whole read, so a request arriving meanwhile waits for its answer ("Auth:
    /// tokens were just refreshed, caller should check again") instead of reading again.
    read: Mutex<Option<std::time::Instant>>,
}

impl Access {
    fn holds(&self, token: &str) -> bool {
        self.tokens
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .contains(token)
    }

    /// `Claim.refresh_access_tokens`, with the read lock already held: a failed read keeps
    /// the last list.
    fn refresh(&self, claim: &SharedClaim, unknown: Option<&str>, read: &mut Option<std::time::Instant>) {
        match fetch_access_tokens(&snapshot(claim), unknown) {
            Ok(tokens) => {
                info!("plex.tv lists {} access tokens", tokens.len());
                *self
                    .tokens
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()) = tokens;
            }
            Err(message) => error!("access tokens read failed: {message}"),
        }
        *read = Some(std::time::Instant::now());
    }

    /// Whether `token` opens this claimed server, without asking plex.tv: its own token or
    /// one in the list as last read. The request router asks this under the runtime lock.
    pub(crate) fn knows(&self, claim: &SharedClaim, token: &str) -> bool {
        let claimed = claim
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .token
            .as_deref()
            .filter(|own| !own.is_empty())
            .map(|own| own == token);
        !token.is_empty() && claimed.is_some_and(|own| own || self.holds(token))
    }

    /// `Claim.admits`: `knows`, after re-reading the list for an unknown token unless that
    /// was done within ACCESS_REFRESH_WINDOW. Asks plex.tv, so it runs before the runtime
    /// lock is taken.
    pub(crate) fn admits(&self, claim: &SharedClaim, token: &str) -> bool {
        let claimed = claim
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .claimed();
        if token.is_empty() || !claimed {
            return false;
        }
        if self.knows(claim, token) {
            return true;
        }
        let mut read = self.read.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if self.knows(claim, token) {
            return true;
        }
        if read.is_some_and(|at| at.elapsed() < ACCESS_REFRESH_WINDOW) {
            return false;
        }
        self.refresh(claim, Some(token), &mut read);
        self.knows(claim, token)
    }
}

/// `xml.sax.saxutils.escape` with `"` too, inside double quotes.
fn xml_attr(value: &str) -> String {
    format!(
        "\"{}\"",
        value
            .replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;")
            .replace('"', "&quot;")
    )
}

/// `Claim.publish`: `POST /servers.xml`, which creates and then updates plex.tv's SERVER
/// record. plex.tv answers it 422 every time, for a real PMS too, and writes the record
/// anyway (docs/findings.md). The status, as a string.
fn publish(claim: &SharedClaim, local_address: &str) -> Result<String, String> {
    let current = snapshot(claim);
    let micros = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_micros();
    let body = format!(
        "<MediaContainer size=\"1\">\n<Server relay=\"0\" name={} host=\"\" localAddresses={} machineIdentifier={} version={} date=\"{micros}\" />\n</MediaContainer>\n",
        xml_attr(&current.name),
        xml_attr(local_address),
        xml_attr(&current.machine),
        xml_attr(&current.version),
    );
    let url = format!(
        "{SERVERS_URL}?{}",
        urlencode(&[("auth_token", current.token.clone().unwrap_or_default())])
    );
    let mut headers = current.pms_headers("PlexMediaServer", true);
    headers.push(("Content-Type", "application/xml".to_string()));
    match urlopen("POST", &url, &headers, Some(body.as_bytes()), 30) {
        Ok((status, _)) | Err(Failure::Http(status, _)) => Ok(status.to_string()),
        Err(Failure::Other(message)) => Err(message),
    }
}

/// `Claim.subscribe`: one pubsub subscription, read until plex.tv or the network ends
/// it. `presence` is this stream (docs/findings.md).
fn subscribe(claim: &SharedClaim) -> Result<(), String> {
    let account_id = ensure_account_id(claim)?;
    let current = snapshot(claim);
    let url = format!(
        "{}?{}",
        PUBSUB_URL
            .replacen("%s", &account_id.map_or_else(|| "None".to_string(), |id| id.to_string()), 1)
            .replacen("%s", &current.machine, 1),
        urlencode(&[("X-Plex-Token", current.token.clone().unwrap_or_default())])
    );
    let headers = current.pms_headers("PlexMediaServer-EventSourceClient", false);
    let response = upstream::open(
        "GET",
        &plextv_url(&url),
        &headers,
        b"",
        Duration::from_secs(PUBSUB_TIMEOUT),
    )
    .map_err(|error| format!("<urlopen error {error}>"))?;
    if response.status >= 400 {
        return Err(Failure::Http(response.status, Vec::new()).to_string());
    }
    info!("pubsub subscribed for {}", current.machine);
    // Online: PMS requests a reachability check 2 ms later (`Claim.subscribe`).
    let (status, body) = announce_connectivity(claim, &uuid4())?;
    info!("connectivity PUT -> {status} {body}");
    let mut lines = std::io::BufRead::lines(std::io::BufReader::new(response));
    while let Some(line) = lines.next() {
        let line = line.map_err(|error| error.to_string())?;
        if let Some(payload) = line.strip_prefix("data:") {
            command(payload.trim());
        }
    }
    Ok(())
}

/// `Claim._command`: the one measured message is `notifyConnectivity`, plex.tv's verdict.
fn command(payload: &str) {
    let Ok(message) = parse_xml(payload.as_bytes()) else {
        let clip: String = payload.chars().take(200).collect();
        warning!("pubsub: unparsable message {clip:?}");
        return;
    };
    match message.get("command") {
        Some("notifyConnectivity") => info!(
            "pubsub: reachability {} for asyncIdentifier {}",
            message.get("connectivity").unwrap_or("None"),
            message.get("asyncIdentifier").unwrap_or("None")
        ),
        // ponytail: only notifyConnectivity is measured; other commands are logged, not acted on.
        other => info!("pubsub: unhandled command {}", other.unwrap_or("None")),
    }
}

/// `Claim.start_registration`: the SERVER record (`publish`), the device PUT after a claim
/// when there is a URL to announce, and the pubsub stream, resubscribed `PUBSUB_RETRY`
/// seconds after it drops. Nothing else is periodic (the probe, measured for an hour).
/// `access`: a claimed START reads the access-token list first, the recording's first call
/// there; the claim itself did not.
pub(crate) fn start_registration(
    claim: SharedClaim,
    local_address: String,
    beat: Option<Beat>,
    access: Option<Arc<Access>>,
) {
    let _ = thread::Builder::new()
        .name("rex-registration".to_string())
        .spawn(move || {
            if let Some(access) = &access {
                let mut read = access.read.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                access.refresh(&claim, None, &mut read);
            }
            match publish(&claim, &local_address) {
                Ok(status) => info!("servers.xml POST -> {status}"),
                Err(message) => error!("registration failed: {message}"),
            }
            if let Some(beat) = &beat {
                if let Err(message) = beat.beat(&claim) {
                    error!("registration failed: {message}");
                }
            }
            loop {
                match subscribe(&claim) {
                    Ok(()) => warning!("pubsub stream ended; resubscribing in {PUBSUB_RETRY}s"),
                    Err(message) => {
                        error!("pubsub failed: {message}; resubscribing in {PUBSUB_RETRY}s")
                    }
                }
                thread::sleep(Duration::from_secs(PUBSUB_RETRY));
            }
        });
}

// -- heartbeat.py ------------------------------------------------------------------

/// `heartbeat.announce`: one registration PUT, byte-shaped like a real PMS's
/// (`tests/fixtures/outbound-announce.json`). `url` is a comma-separated list: a server
/// with several connections sends one `Connection[][uri]` pair each, in a row
/// (`tests/fixtures/pms_announce_connections_s1422.json`).
fn announce(token: &str, machine: &str, url: &str, name: &str) -> Result<(), String> {
    let mut pairs = url
        .split(',')
        .filter(|uri| !uri.is_empty())
        .map(|uri| ("Connection[][uri]", uri.to_string()))
        .collect::<Vec<_>>();
    pairs.extend([
        ("httpsEnabled", "1".to_string()),
        ("httpsRequired", "0".to_string()),
        ("dnsRebindingProtection", "0".to_string()),
        ("X-Plex-Token", token.to_string()),
    ]);
    let query = urlencode(&pairs);
    let version = super::VERSION.to_string();
    let headers = [
        ("User-Agent", format!("PlexMediaServer/{version}")),
        ("Accept", "*/*".to_string()),
        ("Accept-Encoding", "gzip".to_string()),
        ("X-Plex-Client-Identifier", machine.to_string()),
        ("X-Plex-Version", version),
        ("X-Plex-Provides", "server".to_string()),
        ("X-Plex-Platform", "Linux".to_string()),
        ("X-Plex-Product", "Plex Media Server".to_string()),
        ("X-Plex-Platform-Version", "6.1".to_string()),
        ("X-Plex-Device-Name", name.to_string()),
        ("X-Plex-Device", "PC".to_string()),
        ("X-Plex-Model", "x86_64".to_string()),
        ("X-Plex-Device-Vendor", "rex".to_string()),
    ];
    let (_, body) = urlopen(
        "PUT",
        &format!("{ANNOUNCE_BASE}/devices/{machine}?{query}"),
        &headers,
        Some(b""),
        25,
    )
    .map_err(|failure| failure.to_string())?;
    parse_xml(&body).map(|_| ())
}

/// `heartbeat.is_visible`: is the server in the list every client reads?
fn is_visible(token: &str, name: &str) -> Result<bool, String> {
    let headers = [
        ("X-Plex-Token", token.to_string()),
        ("X-Plex-Client-Identifier", "rex-heartbeat".to_string()),
        ("Accept", "application/json".to_string()),
    ];
    let (_, body) =
        urlopen("GET", RESOURCES_URL, &headers, None, 25).map_err(|failure| failure.to_string())?;
    let rows: Value = serde_json::from_slice(&body).map_err(|error| error.to_string())?;
    let rows = rows
        .as_array()
        .ok_or_else(|| "'dict' object has no attribute 'get'".to_string())?;
    Ok(rows.iter().any(|row| {
        row.get("name").and_then(Value::as_str) == Some(name)
            && row
                .get("provides")
                .and_then(Value::as_str)
                .is_some_and(|provides| provides.contains("server"))
    }))
}

/// `heartbeat.Heartbeat`'s target: the account token it was started with, which a
/// claim's own token replaces (a claimed PMS announces with its own).
#[derive(Debug, Clone)]
pub(crate) struct Beat {
    pub token: String,
    pub machine: String,
    pub url: String,
    pub name: String,
}

impl Beat {
    fn token(&self, claim: &SharedClaim) -> String {
        snapshot(claim)
            .token
            .filter(|token| !token.is_empty())
            .unwrap_or_else(|| self.token.clone())
    }

    /// `Heartbeat.beat`: one announce.
    fn beat(&self, claim: &SharedClaim) -> Result<(), String> {
        announce(&self.token(claim), &self.machine, &self.url, &self.name)
    }
}

/// `heartbeat.Heartbeat.start`: re-announce on an interval, on its own thread.
pub(crate) fn start_heartbeat(beat: Beat, claim: SharedClaim, interval: u64) {
    let started = format!(
        "heartbeat every {interval}s for machine {} -> {}",
        beat.machine, beat.url
    );
    let _ = thread::Builder::new()
        .name("rex-heartbeat".to_string())
        .spawn(move || {
            let mut failures = 0_u64;
            let (token, name) = (beat.token.clone(), beat.name.clone());
            loop {
                let result = beat.beat(&claim).and_then(|_| {
                    failures = 0;
                    // Verifying costs a second request but turns a silent
                    // disappearance into a log line.
                    if !is_visible(&token, &name)? {
                        warning!(
                            "announced but NOT visible in /api/v2/resources -- clients cannot see rex right now"
                        );
                    }
                    Ok(())
                });
                if let Err(message) = result {
                    failures += 1;
                    error!("heartbeat failed ({failures} in a row): {message}");
                }
                thread::sleep(Duration::from_secs(interval));
            }
        });
    info!("{started}");
}

// -- accountstate.py ---------------------------------------------------------------

/// `AccountRefresh`: the owner profile and feature UUIDs, at start and once a day.
#[derive(Debug, Default)]
pub(crate) struct AccountRefresh {
    started: AtomicBool,
}

impl AccountRefresh {
    fn refresh(claim: &SharedClaim) -> Result<bool, String> {
        let current = snapshot(claim);
        if !current.claimed() {
            return Ok(false);
        }
        let account_id = fetch_account_id(&current)?;
        thread::sleep(Duration::from_secs(ACCOUNT_FEATURE_DELAY));
        let uuids = fetch_features(&snapshot(claim))?;
        let mut state = claim
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.account_id = account_id;
        state.feature_uuids = uuids;
        state.save();
        Ok(true)
    }

    pub(crate) fn start(&self, claim: SharedClaim) {
        if self.started.swap(true, Ordering::SeqCst) {
            return;
        }
        let _ = thread::Builder::new()
            .name("rex-account-refresh".to_string())
            .spawn(move || {
                let mut failures = 0_u64;
                let mut next = 0_i64;
                loop {
                    let started = now();
                    if started >= next {
                        let result = Self::refresh(&claim);
                        next = started + ACCOUNT_REFRESH_INTERVAL;
                        match result {
                            Ok(_) => failures = 0,
                            Err(message) => {
                                failures += 1;
                                error!(
                                    "claimed-account refresh failed ({failures} in a row): {message}"
                                );
                            }
                        }
                    }
                    thread::sleep(Duration::from_secs((next - now()).max(0) as u64));
                }
            });
    }
}

// AES-256 decryption, local for the same reason as accountstate.py's: the server
// path adds no crypto dependency. The table is the public Rijndael S-box.
const SBOX: [u8; 256] = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
];

fn inv_sbox() -> [u8; 256] {
    let mut inverse = [0_u8; 256];
    for (index, value) in SBOX.iter().enumerate() {
        inverse[*value as usize] = index as u8;
    }
    inverse
}

fn gf_mul(mut left: u8, mut right: u8) -> u8 {
    let mut value = 0;
    for _ in 0..8 {
        if right & 1 != 0 {
            value ^= left;
        }
        let high = left & 0x80;
        left <<= 1;
        if high != 0 {
            left ^= 0x1b;
        }
        right >>= 1;
    }
    value
}

fn aes256_round_keys(key: &[u8; 32]) -> Vec<[u8; 16]> {
    let mut words: Vec<[u8; 4]> = key
        .chunks(4)
        .map(|chunk| [chunk[0], chunk[1], chunk[2], chunk[3]])
        .collect();
    let mut rcon = 1_u8;
    while words.len() < 60 {
        let index = words.len();
        let mut word = words[index - 1];
        if index % 8 == 0 {
            word = [
                SBOX[word[1] as usize] ^ rcon,
                SBOX[word[2] as usize],
                SBOX[word[3] as usize],
                SBOX[word[0] as usize],
            ];
            rcon = gf_mul(rcon, 2);
        } else if index % 8 == 4 {
            word = word.map(|value| SBOX[value as usize]);
        }
        let back = words[index - 8];
        words.push([
            back[0] ^ word[0],
            back[1] ^ word[1],
            back[2] ^ word[2],
            back[3] ^ word[3],
        ]);
    }
    words
        .chunks(4)
        .map(|chunk| {
            let mut round = [0_u8; 16];
            for (index, word) in chunk.iter().enumerate() {
                round[index * 4..index * 4 + 4].copy_from_slice(word);
            }
            round
        })
        .collect()
}

fn aes_decrypt_block(block: &[u8], keys: &[[u8; 16]], inverse: &[u8; 256]) -> [u8; 16] {
    let mut state = [0_u8; 16];
    for index in 0..16 {
        state[index] = block[index] ^ keys[14][index];
    }
    let inv_shift = |state: &[u8; 16]| {
        let mut out = *state;
        for row in 1..4 {
            for column in 0..4 {
                out[row + 4 * ((column + row) % 4)] = state[row + 4 * column];
            }
        }
        out
    };
    let inv_mix = |state: &[u8; 16]| {
        let matrix = [
            [14, 11, 13, 9],
            [9, 14, 11, 13],
            [13, 9, 14, 11],
            [11, 13, 9, 14],
        ];
        let mut out = [0_u8; 16];
        for column in 0..4 {
            for row in 0..4 {
                let mut value = 0;
                for offset in 0..4 {
                    value ^= gf_mul(matrix[row][offset], state[4 * column + offset]);
                }
                out[4 * column + row] = value;
            }
        }
        out
    };
    for round in (1..14).rev() {
        state = inv_shift(&state);
        state = state.map(|value| inverse[value as usize]);
        for index in 0..16 {
            state[index] ^= keys[round][index];
        }
        state = inv_mix(&state);
    }
    state = inv_shift(&state);
    state = state.map(|value| inverse[value as usize]);
    for index in 0..16 {
        state[index] ^= keys[0][index];
    }
    state
}

/// MD5 (RFC 1321), for PMS's key expansion only.
fn md5(input: &[u8]) -> [u8; 16] {
    const SHIFTS: [u32; 64] = [
        7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 5, 9, 14, 20, 5, 9, 14, 20, 5,
        9, 14, 20, 5, 9, 14, 20, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 6, 10,
        15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
    ];
    let constants: Vec<u32> = (0..64)
        .map(|index| ((index as f64 + 1.0).sin().abs() * 4_294_967_296.0) as u32)
        .collect();
    let mut message = input.to_vec();
    let bits = (input.len() as u64).wrapping_mul(8);
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&bits.to_le_bytes());
    let (mut a0, mut b0, mut c0, mut d0) = (
        0x6745_2301_u32,
        0xefcd_ab89_u32,
        0x98ba_dcfe_u32,
        0x1032_5476_u32,
    );
    for chunk in message.chunks(64) {
        let words: Vec<u32> = chunk
            .chunks(4)
            .map(|bytes| u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
            .collect();
        let (mut a, mut b, mut c, mut d) = (a0, b0, c0, d0);
        for index in 0..64 {
            let (f, g) = match index / 16 {
                0 => ((b & c) | (!b & d), index),
                1 => ((d & b) | (!d & c), (5 * index + 1) % 16),
                2 => (b ^ c ^ d, (3 * index + 5) % 16),
                _ => (c ^ (b | !d), (7 * index) % 16),
            };
            let f = f
                .wrapping_add(a)
                .wrapping_add(constants[index])
                .wrapping_add(words[g]);
            a = d;
            d = c;
            c = b;
            b = b.wrapping_add(f.rotate_left(SHIFTS[index]));
        }
        a0 = a0.wrapping_add(a);
        b0 = b0.wrapping_add(b);
        c0 = c0.wrapping_add(c);
        d0 = d0.wrapping_add(d);
    }
    let mut out = [0_u8; 16];
    out[..4].copy_from_slice(&a0.to_le_bytes());
    out[4..8].copy_from_slice(&b0.to_le_bytes());
    out[8..12].copy_from_slice(&c0.to_le_bytes());
    out[12..].copy_from_slice(&d0.to_le_bytes());
    out
}

/// `base64.b64decode(..., validate=True)` (or the urlsafe alphabet, unvalidated
/// beyond the alphabet): `None` where Python raises.
pub(super) fn b64decode(text: &str, urlsafe: bool) -> Option<Vec<u8>> {
    let bytes = text.as_bytes();
    if bytes.len() % 4 != 0 {
        return None;
    }
    let value = |byte: u8| -> Option<u32> {
        Some(match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' if !urlsafe => 62,
            b'/' if !urlsafe => 63,
            b'-' if urlsafe => 62,
            b'_' if urlsafe => 63,
            _ => return None,
        } as u32)
    };
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for (index, chunk) in bytes.chunks(4).enumerate() {
        let last = index == bytes.len() / 4 - 1;
        let padding = chunk.iter().rev().take_while(|byte| **byte == b'=').count();
        if padding > 2 || (padding > 0 && !last) {
            return None;
        }
        let mut accumulator = 0_u32;
        for byte in &chunk[..4 - padding] {
            accumulator = (accumulator << 6) | value(*byte)?;
        }
        accumulator <<= 6 * padding as u32;
        let decoded = accumulator.to_be_bytes();
        out.extend_from_slice(&decoded[1..4 - padding]);
    }
    Some(out)
}

/// `_decrypt_scrobbling_token`: base64, AES-256-CBC with PMS's MD5 expansion
/// (`D1=MD5(secret)`, `D2=MD5(D1||secret)`, `D3=MD5(D2||secret)`), then PMS's
/// permissive strip of the count in the final byte.
fn decrypt_scrobbling_token(encoded: &str, secret: &[u8]) -> Result<String, String> {
    let ciphertext =
        b64decode(encoded, false).ok_or("scrobbling service token is not valid base64")?;
    if ciphertext.is_empty() || ciphertext.len() % 16 != 0 {
        return Err("scrobbling service token is not whole AES blocks".to_string());
    }
    if secret.len() != 32 {
        return Err("scrobbling decrypt secret must be exactly 32 bytes".to_string());
    }
    let mut expanded = Vec::new();
    let mut previous = Vec::new();
    while expanded.len() < 48 {
        let mut input = previous.clone();
        input.extend_from_slice(secret);
        previous = md5(&input).to_vec();
        expanded.extend_from_slice(&previous);
    }
    let mut key = [0_u8; 32];
    key.copy_from_slice(&expanded[..32]);
    let keys = aes256_round_keys(&key);
    let inverse = inv_sbox();
    let mut plaintext = Vec::with_capacity(ciphertext.len());
    let mut chain = expanded[32..48].to_vec();
    for block in ciphertext.chunks(16) {
        let decoded = aes_decrypt_block(block, &keys, &inverse);
        plaintext.extend(decoded.iter().zip(&chain).map(|(left, right)| left ^ right));
        chain = block.to_vec();
    }
    let count = *plaintext.last().unwrap_or(&0) as usize;
    if !(1..=16).contains(&count) {
        return Err("scrobbling service token has invalid padding count".to_string());
    }
    plaintext.truncate(plaintext.len() - count);
    String::from_utf8(plaintext).map_err(|_| "scrobbling service token is not UTF-8".to_string())
}

fn python_str(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Bool(false)) => "False".to_string(),
        Some(other) => other.to_string(),
    }
}

/// `ScrobblingBearerProvider`: the owner's short-lived scrobbling JWT, memory only.
#[derive(Debug, Default)]
struct BearerProvider {
    cached: Mutex<Option<(String, i64)>>,
}

impl BearerProvider {
    fn secret() -> Result<Vec<u8>, String> {
        let encoded = std::env::var(DECRYPT_SECRET_ENV).unwrap_or_default();
        if encoded.is_empty() {
            return Err(format!(
                "{DECRYPT_SECRET_ENV} is required for account-state sync"
            ));
        }
        let secret = b64decode(&encoded, false)
            .ok_or_else(|| format!("{DECRYPT_SECRET_ENV} is not valid base64"))?;
        if secret.len() != 32 {
            return Err("scrobbling decrypt secret must be exactly 32 bytes".to_string());
        }
        Ok(secret)
    }

    fn fetch(claim: &SharedClaim) -> Result<(String, i64), String> {
        let account_id =
            ensure_account_id(claim)?.ok_or("account id unavailable for scrobbling service")?;
        let current = snapshot(claim);
        let token = current.token.clone().unwrap_or_default();
        let url = format!(
            "{SERVICES_URL}?{}",
            urlencode(&[("auth_token", token.clone())])
        );
        let version = current.version.clone();
        let headers = [
            ("User-Agent", format!("PlexMediaServer/{version}")),
            ("Accept", "application/xml".to_string()),
            ("X-Plex-Client-Identifier", current.machine.clone()),
            ("X-Plex-Version", version),
            ("X-Plex-Provides", "server".to_string()),
            ("X-Plex-Platform", "Linux".to_string()),
            ("X-Plex-Product", "Plex Media Server".to_string()),
            ("X-Plex-Platform-Version", "6.1".to_string()),
            ("X-Plex-Device-Name", current.name.clone()),
            ("X-Plex-Device", "PC".to_string()),
            ("X-Plex-Token", token),
        ];
        let (_, body) =
            urlopen("GET", &url, &headers, None, 30).map_err(|failure| failure.to_string())?;
        let root = parse_xml(&body)?;
        let wanted = account_id.to_string();
        let service = root
            .all("user")
            .into_iter()
            .find(|user| python_str(user.get("id").map(|id| json!(id)).as_ref()) == wanted)
            .and_then(|user| {
                user.all("service")
                    .into_iter()
                    .find(|node| node.get("identifier") == Some("scrobbling"))
                    .cloned()
            });
        let token = service
            .as_ref()
            .and_then(|node| node.get("token"))
            .filter(|token| !token.is_empty())
            .ok_or("scrobbling service credential absent")?;
        let bearer = decrypt_scrobbling_token(token, &Self::secret()?)?;
        let parts = bearer.split('.').collect::<Vec<_>>();
        let segment = |part: &str| -> Option<Value> {
            let mut padded = part.to_string();
            while padded.len() % 4 != 0 {
                padded.push('=');
            }
            serde_json::from_slice(&b64decode(&padded, true)?).ok()
        };
        let (Some(header), Some(payload)) = (
            parts.first().and_then(|part| segment(part)),
            parts.get(1).and_then(|part| segment(part)),
        ) else {
            return Err("decrypted scrobbling credential is not a JWT".to_string());
        };
        let exp = payload.get("exp").and_then(Value::as_i64);
        if parts.len() != 3
            || header.get("alg").and_then(Value::as_str) != Some("RS256")
            || !payload.get("aud").is_some_and(Value::is_string)
            || !payload.get("iss").is_some_and(Value::is_string)
            || python_str(payload.get("userId")) != wanted
            || exp.is_none()
        {
            return Err("decrypted scrobbling JWT has an unexpected shape".to_string());
        }
        let exp = exp.unwrap_or_default();
        if exp <= now() {
            return Err("decrypted scrobbling JWT is expired".to_string());
        }
        Ok((bearer, exp))
    }

    fn bearer(&self, claim: &SharedClaim) -> Result<String, String> {
        let mut cached = self
            .cached
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some((bearer, expires)) = cached.as_ref() {
            if *expires > now() + 30 {
                return Ok(bearer.clone());
            }
        }
        let fresh = Self::fetch(claim)?;
        *cached = Some(fresh.clone());
        Ok(fresh.0)
    }
}

/// `WatchStateSync`: one consent/page pair per interval, applied to the library.
#[derive(Debug)]
pub(crate) struct WatchStateSync {
    started: AtomicBool,
    interval: u64,
    provider: BearerProvider,
    failures: AtomicU64,
}

impl WatchStateSync {
    pub(crate) fn new(interval: u64) -> Self {
        Self {
            started: AtomicBool::new(false),
            interval,
            provider: BearerProvider::default(),
            failures: AtomicU64::new(0),
        }
    }

    fn read_json(url: &str, headers: &[(&str, String)]) -> Result<Value, String> {
        let (_, body) =
            urlopen("GET", url, headers, None, 30).map_err(|failure| failure.to_string())?;
        serde_json::from_slice(&body).map_err(|error| error.to_string())
    }

    fn refresh(&self, claim: &SharedClaim, runtime: &SharedRuntime) -> Result<usize, String> {
        if !snapshot(claim).claimed() {
            return Ok(0);
        }
        let Some(account_id) = ensure_account_id(claim)? else {
            return Ok(0);
        };
        let current = snapshot(claim);
        // `_server_headers`: the claim identity, then these four (Accept replaced in
        // place, the rest appended), in the dict order urllib sends them.
        let mut headers = current.headers();
        for (name, value) in headers.iter_mut() {
            if *name == "Accept" {
                *value = "*/*".to_string();
            }
        }
        headers.push(("User-Agent", format!("PlexMediaServer/{}", current.version)));
        headers.push(("Content-Type", "application/json".to_string()));
        headers.push(("X-Plex-Token", current.token.clone().unwrap_or_default()));
        let consent = Self::read_json(
            &CONSENT_URL.replace("%s", &account_id.to_string()),
            &headers,
        )?;
        // Every measured response was consent=true and deletionRequestedAt=null.
        if consent.get("consent") != Some(&Value::Bool(true))
            || !consent
                .get("deletionRequestedAt")
                .is_some_and(Value::is_null)
        {
            info!("cloud watch-state sync is not consented");
            return Ok(0);
        }
        let bearer = self.provider.bearer(claim)?;
        if bearer.is_empty() {
            return Err("no scrobbling bearer available".to_string());
        }
        let query = urlencode(&[
            (
                "after",
                snapshot(claim).view_state_cursor.unwrap_or_default(),
            ),
            ("count", "100".to_string()),
        ]);
        let page = Self::read_json(
            &format!(
                "{}?{query}",
                STATE_URL.replace("%s", &account_id.to_string())
            ),
            &[
                ("User-Agent", format!("PlexMediaServer/{}", current.version)),
                ("Accept", "*/*".to_string()),
                ("Authorization", format!("Bearer {bearer}")),
                ("Content-Type", "application/json".to_string()),
                ("X-Plex-Version", current.version.clone()),
                ("X-Plex-Product", "Plex Media Server".to_string()),
            ],
        )?;
        let mut applied = 0;
        if let Some(items) = page.get("items").and_then(Value::as_array) {
            let mut guard = runtime
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            for state in items {
                if guard.import_account_state(state) {
                    applied += 1;
                }
            }
            guard.save_if_dirty();
        }
        let info = page.get("pageInfo").cloned().unwrap_or(Value::Null);
        if let Some(cursor) = info
            .get("endCursor")
            .and_then(Value::as_str)
            .filter(|cursor| !cursor.is_empty())
        {
            // Last on purpose: a crash while applying a page must replay it.
            let mut state = claim
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            state.view_state_cursor = Some(cursor.to_string());
            state.save();
        }
        if info.get("hasNextPage") == Some(&Value::Bool(true)) {
            warning!("cloud state returned an unmeasured multi-page response");
        }
        self.failures.store(0, Ordering::SeqCst);
        Ok(applied)
    }

    pub(crate) fn start(self: &Arc<Self>, claim: SharedClaim, runtime: SharedRuntime) {
        if self.started.swap(true, Ordering::SeqCst) {
            return;
        }
        let sync = Arc::clone(self);
        let _ = thread::Builder::new()
            .name("rex-account-state".to_string())
            .spawn(move || loop {
                if let Err(message) = sync.refresh(&claim, &runtime) {
                    let failures = sync.failures.fetch_add(1, Ordering::SeqCst) + 1;
                    error!("cloud watch-state sync failed ({failures} in a row): {message}");
                }
                thread::sleep(Duration::from_secs(sync.interval));
            });
    }
}

/// What `main()` wires for plex.tv, held by the runtime so a claim can start it.
#[derive(Debug)]
pub(crate) struct PlexTv {
    pub claim: SharedClaim,
    pub refresh: AccountRefresh,
    pub sync: Option<Arc<WatchStateSync>>,
    /// The announce heartbeat, when `--announce-url` runs one: a claim announces at once.
    pub beat: Mutex<Option<Beat>>,
    /// The tokens a claimed server lets in besides its own (`Claim.admits`).
    pub access: Arc<Access>,
}

impl PlexTv {
    pub(crate) fn new(claim: Claim, account_state_interval: u64) -> Self {
        let sync = std::env::var(DECRYPT_SECRET_ENV)
            .ok()
            .filter(|value| !value.is_empty())
            .map(|_| Arc::new(WatchStateSync::new(account_state_interval)));
        Self {
            claim: Arc::new(Mutex::new(claim)),
            refresh: AccountRefresh::default(),
            sync,
            beat: Mutex::new(None),
            access: Arc::default(),
        }
    }

    /// The account threads `main()` starts for a claimed server, and a claim starts.
    pub(crate) fn start_account(&self, runtime: &SharedRuntime) {
        self.refresh.start(Arc::clone(&self.claim));
        if let Some(sync) = &self.sync {
            sync.start(Arc::clone(&self.claim), Arc::clone(runtime));
        }
    }
}

pub(crate) fn claim_path(catalog: &Path) -> PathBuf {
    // `os.path.dirname(os.path.abspath(catalog))`.
    std::path::absolute(catalog)
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
        .join("claim_state.json")
}

// -- what the server shows of it -------------------------------------------------

impl super::Runtime {
    pub(super) fn claim_mut(&mut self) -> std::sync::MutexGuard<'_, Claim> {
        self.plextv
            .claim
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn claim_snapshot(&self) -> Claim {
        snapshot(&self.plextv.claim)
    }

    /// `_claimed`: `--assert-claimed`, or a real claim.
    pub(super) fn claimed(&self) -> bool {
        self.assert_claimed || self.claim_snapshot().claimed()
    }

    /// `_owner`: what a real PMS reports as myPlexUsername, the owner's EMAIL.
    pub(super) fn owner(&self) -> String {
        let claim = self.claim_snapshot();
        if claim.claimed() {
            if let Some(value) = claim
                .email
                .filter(|value| !value.is_empty())
                .or(claim.owner.filter(|value| !value.is_empty()))
            {
                return value;
            }
        }
        self.owner_email.clone()
    }

    /// `owner_features`: the named defaults plus the account's measured feature UUIDs.
    pub(super) fn owner_features(&self) -> String {
        self.claim_snapshot().owner_features(super::OWNER_FEATURES)
    }

    /// `_identity_prefs`.
    pub(super) fn identity_pref(&self, id: &str) -> Option<Value> {
        match id {
            "FriendlyName" => Some(json!(self.friendly)),
            "MachineIdentifier" => Some(json!(self.machine)),
            "PlexOnlineMail" => Some(json!(if self.claimed() {
                self.owner()
            } else {
                String::new()
            })),
            _ => None,
        }
    }

    /// `myplex_account`: who this server is signed in as. No `authToken`: the token
    /// gate defaults OFF, so the claim token would reach every peer (cycle 49).
    pub(super) fn myplex_account(&self) -> super::Response {
        let claimed = self.claimed();
        let private = !self.private_address.is_empty();
        super::Response::json(
            200,
            json!({"MyPlex": {
                "username": if claimed { json!(self.owner()) } else { Value::Null },
                "mappingState": "mapped",
                "mappingError": "",
                "signInState": if claimed { "ok" } else { "none" },
                "publicAddress": "",
                "publicPort": 0,
                "privateAddress": self.private_address,
                "privatePort": if private { self.private_port } else { 0 },
                "subscriptionFeatures": self.owner_features(),
                "subscriptionActive": claimed,
                "subscriptionState": if claimed { "Active" } else { "Unknown" },
            }}),
        )
    }

    /// `library.import_account_state`: one scrobbles.plex.tv row by its Plex GUID.
    /// An import, not a scrobble: no history row, no increment of a complete item and
    /// the cloud's timestamps rather than the local clock.
    pub(super) fn import_account_state(&mut self, state: &Value) -> bool {
        let Some(guid) = state.get("guid").and_then(Value::as_str) else {
            return false;
        };
        let Some((key, count)) = self
            .catalog
            .items()
            .iter()
            .filter(|row| row.get("guid").and_then(Value::as_str) == Some(guid))
            .find_map(|row| {
                let key = super::key_of(row)?;
                (!self.deleted.contains(&key)).then(|| {
                    (
                        key,
                        row.get("viewCount")
                            .and_then(|value| {
                                value
                                    .as_i64()
                                    .or_else(|| value.as_str().and_then(|text| text.parse().ok()))
                            })
                            .unwrap_or(0),
                    )
                })
            })
        else {
            return false;
        };
        let Some(viewed_at) = state.get("viewedAt").and_then(Value::as_i64) else {
            return false;
        };
        let mut writes: Vec<(&str, Option<Value>)> = Vec::new();
        match state.get("viewState").and_then(Value::as_str) {
            Some("complete") => {
                writes.push(("viewCount", Some(json!(count.max(1)))));
                writes.push(("lastViewedAt", Some(json!(viewed_at.div_euclid(1000)))));
                writes.push(("viewOffset", None));
            }
            Some("unplayed") => {
                writes.push(("viewCount", None));
                writes.push(("lastViewedAt", None));
                writes.push(("viewOffset", None));
            }
            other => {
                warning!(
                    "unknown cloud viewState {} for {guid}",
                    other.map_or_else(|| "None".to_string(), |value| format!("'{value}'"))
                );
                return false;
            }
        }
        // Applied before the rating is validated, as there: a malformed rating
        // refuses the row with its view state already written.
        for (field, value) in writes.drain(..) {
            self.update_view(&key, field, value);
        }
        // All 39 measured rows paired rating and ratedAt; `-1` is the clear, and a row
        // with neither is view-only and leaves the stars alone.
        match (state.get("rating"), state.get("ratedAt")) {
            (Some(rating), Some(rated_at)) => {
                let (Some(rating), Some(rated_at)) = (rating.as_i64(), rated_at.as_i64()) else {
                    return false;
                };
                if rating < 0 {
                    writes.push(("userRating", None));
                    writes.push(("lastRatedAt", None));
                } else {
                    writes.push(("userRating", Some(json!(rating as f64))));
                    writes.push(("lastRatedAt", Some(json!(rated_at.div_euclid(1000)))));
                }
            }
            (None, None) => {}
            _ => return false,
        }
        for (field, value) in writes {
            self.update_view(&key, field, value);
        }
        true
    }
}

/// The half of `do_claim` that talks to plex.tv, run by `handle_connection` after the
/// router accepted the token and the runtime lock is released.
pub(super) fn finish_claim(runtime: &SharedRuntime, token: &str, _target: &str) -> super::Response {
    let plextv = {
        let guard = runtime
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        Arc::clone(&guard.plextv)
    };
    if let Err(message) = exchange(&plextv.claim, token) {
        error!("claim exchange failed: {message}");
        return super::Response::error(500, &format!("rex: claim exchange failed: {message}"));
    }
    // What the probe PMS did next: POST /servers.xml, the device PUT, then the pubsub
    // stream that makes it present (`do_claim`).
    let local_address = runtime
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .private_address
        .clone();
    let beat = plextv
        .beat
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone();
    start_registration(Arc::clone(&plextv.claim), local_address, beat, None);
    plextv.start_account(runtime);
    let guard = runtime
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    guard.myplex_account()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn aes256_matches_the_nist_block_vector() {
        let key: Vec<u8> = (0..32)
            .map(|index| {
                u8::from_str_radix(
                    &"603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4"
                        [index * 2..index * 2 + 2],
                    16,
                )
                .unwrap()
            })
            .collect();
        let hex = |text: &str| -> Vec<u8> {
            (0..text.len() / 2)
                .map(|index| u8::from_str_radix(&text[index * 2..index * 2 + 2], 16).unwrap())
                .collect()
        };
        let mut fixed = [0_u8; 32];
        fixed.copy_from_slice(&key);
        let block = aes_decrypt_block(
            &hex("f3eed1bdb5d2a03c064b5a7e3db181f8"),
            &aes256_round_keys(&fixed),
            &inv_sbox(),
        );
        assert_eq!(block.to_vec(), hex("6bc1bee22e409f96e93d7e117393172a"));
    }

    #[test]
    fn md5_matches_rfc_1321() {
        let digest = md5(b"abc")
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        assert_eq!(digest, "900150983cd24fb0d6963f7d28e17f72");
    }

    #[test]
    fn base64_is_strict_like_validate_true() {
        assert_eq!(b64decode("YWJj", false), Some(b"abc".to_vec()));
        assert_eq!(b64decode("YWI=", false), Some(b"ab".to_vec()));
        assert_eq!(b64decode("YWI", false), None);
        assert_eq!(b64decode("YW=j", false), None);
    }

    #[test]
    fn parses_the_documents_the_modules_read() {
        let root = parse_xml(
            b"<?xml version=\"1.0\"?>\n<users><user id=\"42\"><services><service identifier=\"s\" token=\"a&amp;b\"/></services></user></users>",
        )
        .unwrap();
        assert_eq!(root.all("service")[0].get("token"), Some("a&b"));
        assert!(parse_xml(b"<broken").is_err());
    }

    #[test]
    fn repr_matches_python() {
        assert_eq!(bytes_repr(b""), "b''");
        assert_eq!(bytes_repr(b"it's"), "b\"it's\"");
        assert_eq!(bytes_repr(b"a\nb\x00"), "b'a\\nb\\x00'");
    }
}
