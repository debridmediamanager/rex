//! The response envelope: `server.py`'s `_send`, `_ranged`, `_maybe_gzip`,
//! `_cors_headers` and `end_headers`, ported so every response names its connection
//! state, carries `Cache-Control`, answers `Range` and writes its headers in the
//! measured per-family order with `Date` last (cycles 16, 29, 33, 35, 44, 49).

use super::*;

/// What the writer needs to know about the request a response answers.
pub(super) struct Wire {
    pub method: String,
    /// The raw request path, before any trailing-slash normalisation.
    pub path: String,
    pub range: Option<String>,
    pub origin: Option<String>,
    pub accept_encoding: Option<String>,
    pub pipeline_disabled: bool,
}

impl Wire {
    pub(super) fn bare() -> Self {
        Self {
            method: String::new(),
            path: String::new(),
            range: None,
            origin: None,
            accept_encoding: None,
            pipeline_disabled: false,
        }
    }

    pub(super) fn of(request: &Request, path: &str, query: &HashMap<String, String>) -> Self {
        let pipeline = request
            .headers
            .get("x-plex-http-pipeline")
            .cloned()
            .or_else(|| query.get("X-Plex-Http-Pipeline").cloned());
        Self {
            method: request.method.clone(),
            path: path.to_string(),
            range: request.headers.get("range").cloned(),
            origin: request.headers.get("origin").cloned(),
            accept_encoding: request.headers.get("accept-encoding").cloned(),
            pipeline_disabled: pipeline.as_deref() == Some("disable"),
        }
    }
}

/// A response built outside `_send` (the preflight, a part read) is marked with this
/// private header; the writer strips it.
pub(super) const RAW_MARK: &str = "X-Plexsim-Raw";

/// `Range` -> (status, Content-Range, first, last) over a body of `total` bytes, or
/// None when the header is not a Range (cycles 16, 35, 44).
pub(super) fn range_window(total: usize, header: Option<&str>) -> Option<(u16, Option<String>, usize, usize)> {
    let header = header?;
    let (_, spec) = header.split_once('=')?;
    if spec.is_empty() || spec == "-" {
        return None;
    }
    let total_i = total as i64;
    let whole = (206, Some(format!("bytes 0-{}/{}", total_i - 1, total)), 0, total.saturating_sub(1));
    let leading_int = |text: &str| -> Option<(i64, usize)> {
        let bytes = text.as_bytes();
        let mut end = 0;
        if end < bytes.len() && (bytes[end] == b'+' || bytes[end] == b'-') {
            end += 1;
        }
        let digits_start = end;
        while end < bytes.len() && bytes[end].is_ascii_digit() {
            end += 1;
        }
        if end == digits_start {
            return None;
        }
        text[..end].trim_start_matches('+').parse::<i64>().ok().map(|value| (value, end))
    };
    if let Some(suffix) = spec.strip_prefix('-') {
        let length = leading_int(suffix).map_or(0, |(value, _)| value);
        if length < 0 {
            return None;
        }
        if length == 0 {
            return Some((206, Some(format!("bytes 0-0/{total}")), 0, total.saturating_sub(1)));
        }
        if length >= total_i {
            return Some(whole);
        }
        let first = (total_i - length) as usize;
        return Some((206, Some(format!("bytes {first}-{}/{total}", total - 1)), first, total - 1));
    }
    let Some((first, end)) = leading_int(spec) else {
        return Some(whole);
    };
    let rest = &spec[end..];
    let Some(rest) = rest.strip_prefix('-') else {
        return Some(whole);
    };
    let last = if rest.is_empty() {
        total_i - 1
    } else if rest.contains('-') {
        return Some(whole);
    } else {
        match leading_int(rest) {
            Some((value, used)) if used == rest.len() => value,
            _ => 0,
        }
    };
    if first >= total_i || last < first {
        return Some((416, None, 0, 0));
    }
    let last = last.min(total_i - 1);
    Some((206, Some(format!("bytes {first}-{last}/{total}")), first as usize, last as usize))
}

const ORDER_CONTAINER: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Content-Type", "Content-Length",
    "Content-Range", "X-Plex-Container-Start", "X-Plex-Container-Total-Size", "Connection",
    "Keep-Alive", "Content-Length", "Cache-Control", "Access-Control-Allow-Headers",
    "Access-Control-Allow-Origin", "Date",
];
const ORDER_GZIP: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Content-Type", "Connection",
    "Keep-Alive", "Content-Encoding", "X-Plex-Content-Original-Length",
    "X-Plex-Content-Compressed-Length", "Content-Length", "Cache-Control",
    "Access-Control-Allow-Headers", "Access-Control-Allow-Origin", "Date",
];
const ORDER_ERROR: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Content-Length", "Content-Type",
    "Connection", "Keep-Alive", "Cache-Control", "Access-Control-Allow-Headers",
    "Access-Control-Allow-Origin", "Date",
];
const ORDER_RANGE_ERROR: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Content-Length", "Content-Type",
    "Cache-Control", "Access-Control-Allow-Origin", "Date",
];
const ORDER_RANGE_ERROR_CONTAINER: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Content-Length", "Content-Type",
    "Connection", "Keep-Alive", "Cache-Control", "Access-Control-Allow-Origin", "Date",
];
const ORDER_SYSTEM: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Content-Length", "Content-Type",
    "Set-Cookie", "Connection", "Keep-Alive", "Cache-Control", "Access-Control-Allow-Headers",
    "Access-Control-Allow-Origin", "Date",
];
const ORDER_SYSTEM_NOCACHE_FIRST: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Cache-Control", "Content-Length",
    "Content-Type", "Set-Cookie", "Connection", "Keep-Alive", "Access-Control-Allow-Headers",
    "Access-Control-Allow-Origin", "Date",
];
const ORDER_SYSTEM_GZIP: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Content-Type", "Set-Cookie",
    "Connection", "Keep-Alive", "Content-Encoding", "X-Plex-Content-Original-Length",
    "X-Plex-Content-Compressed-Length", "Content-Length", "Cache-Control",
    "Access-Control-Allow-Headers", "Access-Control-Allow-Origin", "Date",
];
const ORDER_ARTWORK: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Content-Type", "X-Plex-Local-Asset",
    "Cache-Control", "Accept-Ranges", "Content-Range", "Connection", "Keep-Alive", "Content-Length",
    "Access-Control-Allow-Headers", "Access-Control-Allow-Origin", "Date",
];
const ORDER_PHOTO: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Accept-Ranges", "Connection",
    "Keep-Alive", "Content-Length", "Content-Type", "Cache-Control", "Access-Control-Allow-Headers",
    "Access-Control-Allow-Origin", "Date",
];
const ORDER_ASSET: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Cache-Control", "Accept-Ranges",
    "Content-Range", "Connection", "Keep-Alive", "Content-Length", "Content-Type",
    "Access-Control-Allow-Origin", "Date",
];
const ORDER_PART: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "ETag", "Last-Modified",
    "Content-Disposition", "Accept-Ranges", "Content-Range", "Connection", "Keep-Alive",
    "Content-Length", "Content-Type", "Access-Control-Allow-Origin", "Date",
];
const ORDER_REDIRECT: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Location", "Cache-Control",
    "Content-Length", "Connection", "Keep-Alive", "Access-Control-Allow-Origin", "Date",
];
const ORDER_PREFLIGHT: &[&str] = &[
    "X-Plex-Protocol", "Access-Control-Allow-Methods", "Access-Control-Max-Age", "Vary",
    "Content-Length", "Content-Type", "Connection", "Keep-Alive", "Cache-Control",
    "Access-Control-Allow-Headers", "Access-Control-Allow-Origin", "Date",
];

fn value_of<'a>(lines: &'a [(String, String)], name: &str) -> Option<&'a str> {
    lines
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case(name))
        .map(|(_, value)| value.as_str())
}

/// Which order this response belongs to (`server.py` `_header_family`).
fn header_family(lines: &[(String, String)], status: u16, path: &str, range_container: bool) -> &'static [&'static str] {
    let has = |name: &str| value_of(lines, name).is_some();
    if has("etag") || has("content-disposition") {
        return ORDER_PART;
    }
    if status == 416 {
        return if range_container { ORDER_RANGE_ERROR_CONTAINER } else { ORDER_RANGE_ERROR };
    }
    if has("location") {
        return ORDER_REDIRECT;
    }
    if has("access-control-allow-methods") {
        return ORDER_PREFLIGHT;
    }
    let path = path.trim_end_matches('/');
    if path == "/system" || path.starts_with("/system/") {
        if path == "/system/services/url/lookup" && has("content-encoding") {
            return ORDER_SYSTEM_GZIP;
        }
        let parent = path.rsplit_once('/').map_or("", |(head, _)| head);
        if framework::CACHE_CONTROL_FIRST.contains(&path)
            || parent == "/system/channels"
            || parent == "/system/plugins"
        {
            return ORDER_SYSTEM_NOCACHE_FIRST;
        }
        return if path == "/system/agents" { ORDER_CONTAINER } else { ORDER_SYSTEM };
    }
    if has("set-cookie") {
        return ORDER_SYSTEM;
    }
    if value_of(lines, "content-type").is_some_and(|value| value.starts_with("text/event-stream")) {
        return &[
            "X-Plex-Protocol", "Access-Control-Expose-Headers", "Vary", "Content-Type", "Connection",
            "Cache-Control", "Access-Control-Allow-Origin", "Date",
        ];
    }
    if has("x-plex-local-asset") {
        return ORDER_ARTWORK;
    }
    if status < 400 && path.starts_with("/photo/:/transcode") {
        return ORDER_PHOTO;
    }
    if status < 400 && (path.starts_with("/web") || path.starts_with("/:/resources") || path == "/favicon.ico") {
        return ORDER_ASSET;
    }
    if has("content-encoding") {
        return ORDER_GZIP;
    }
    if status >= 400 {
        return ORDER_ERROR;
    }
    if value_of(lines, "content-length") == Some("0") {
        return ORDER_ERROR;
    }
    if has("accept-ranges") {
        return ORDER_ARTWORK;
    }
    ORDER_CONTAINER
}

/// Order the lines by the family table: a repeated name is ranked by occurrence, and
/// an unmeasured header goes just before `Date`.
fn order_lines(lines: Vec<(String, String)>, order: &[&str]) -> Vec<(String, String)> {
    let mut ranks: HashMap<String, Vec<usize>> = HashMap::new();
    for (index, name) in order.iter().enumerate() {
        ranks.entry(name.to_ascii_lowercase()).or_default().push(index);
    }
    let unknown = ranks.get("date").map_or(order.len(), |slots| slots[0]) as f64 - 0.5;
    let mut seen: HashMap<String, usize> = HashMap::new();
    let mut ranked = lines
        .into_iter()
        .enumerate()
        .map(|(position, (name, value))| {
            let lower = name.to_ascii_lowercase();
            let rank = match ranks.get(&lower) {
                Some(slots) => {
                    let which = seen.get(&lower).copied().unwrap_or(0).min(slots.len() - 1);
                    seen.insert(lower, which + 1);
                    slots[which] as f64
                }
                None => unknown,
            };
            (rank, position, name, value)
        })
        .collect::<Vec<_>>();
    ranked.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal).then(a.1.cmp(&b.1)));
    ranked.into_iter().map(|(_, _, name, value)| (name, value)).collect()
}

const GZIP_TYPES: &[&str] = &["text/xml", "application/xml", "application/json", "text/html", "text/plain"];

fn gzip(body: &[u8]) -> Option<Vec<u8>> {
    let mut encoder = flate2::GzBuilder::new().mtime(0).write(Vec::new(), Compression::new(6));
    encoder.write_all(body).ok()?;
    encoder.finish().ok()
}

/// The finished envelope: (status, ordered header lines, body, keep the socket).
pub(super) fn envelope(response: &Response, wire: &Wire, keep: bool) -> (u16, Vec<(String, String)>, Vec<u8>, bool) {
    let mut status = response.status;
    let mut content_type = response.content_type.clone();
    let mut body = response.body.clone();
    let raw = response.headers.iter().any(|(name, _)| name == RAW_MARK);
    let mut extra: Vec<(String, String)> = response
        .headers
        .iter()
        .filter(|(name, _)| {
            // Cycle 53: a framework STRING result carries the literal `Content-type`
            // spelling as its own header line (`Response::framework_string`); only the
            // canonical spelling is the envelope's to write.
            name == "Content-type"
                || !matches!(
                name.to_ascii_lowercase().as_str(),
                "date" | "connection" | "keep-alive" | "content-length" | "content-type" | "x-plex-protocol"
                    | "x-plexsim-raw" | "x-plexsim-no-head-gzip"
            ) && !(wire.origin.is_some()
                && !raw
                && matches!(
                    name.to_ascii_lowercase().as_str(),
                    "access-control-allow-origin" | "access-control-expose-headers" | "vary"
                ))
        })
        .cloned()
        .collect();
    let mut keep = keep && !response.close;
    let lower_names = |extra: &[(String, String)]| extra.iter().map(|(n, _)| n.to_ascii_lowercase()).collect::<HashSet<_>>();
    // Cycle 16/35/44: `Range` applies to every 200 body; a part read ranges itself.
    let mut content_range: Option<String> = None;
    let part = extra.iter().any(|(name, _)| name == "ETag");
    let already_ranged = extra.iter().any(|(name, _)| name.eq_ignore_ascii_case("content-range"));
    let mut range_container = false;
    if status == 200 && !part && !already_ranged {
        if let Some((code, range, first, last)) = range_window(body.len(), wire.range.as_deref()) {
            if code == 416 {
                status = 416;
            } else {
                body = body.get(first..=last.min(body.len().saturating_sub(1))).map(<[u8]>::to_vec).unwrap_or_default();
                status = code;
                content_range = range;
            }
        }
    }
    if status == 416 && !part {
        // Cycle 29/36/44: a 416 is an ERROR response -- the refusal document as
        // text/html, without Accept-Ranges, Cache-Control's success value or the
        // local-asset mark; a container's 416 names its connection state.
        range_container = content_type.contains("xml") || content_type.contains("json");
        content_type = "text/html".to_string();
        body = error_document(416);
        extra.retain(|(name, _)| {
            !matches!(name.to_ascii_lowercase().as_str(), "accept-ranges" | "cache-control" | "x-plex-local-asset" | "content-range")
        });
    }
    let mut lines: Vec<(String, String)> = Vec::new();
    let names = lower_names(&extra);
    if !raw && !part && !names.contains("cache-control") {
        lines.push(("Cache-Control".to_string(), "no-cache".to_string()));
    }
    let silent = status == 404 && (wire.path.starts_with("/:/resources/") || wire.path.starts_with("/web/"));
    if silent {
        keep = false;
    } else if !(status == 416 && !range_container) {
        if wire.pipeline_disabled || !keep {
            keep = false;
            lines.push(("Connection".to_string(), "close".to_string()));
        } else {
            lines.push(("Connection".to_string(), "Keep-Alive".to_string()));
            lines.push(("Keep-Alive".to_string(), "timeout=20".to_string()));
        }
    }
    // The framework's traceback (`plugin-xml`): a HEAD reports the uncompressed length
    // and no Content-Encoding on both versions, where its GET honours a gzip offer.
    let head_plain = wire.method == "HEAD"
        && response.headers.iter().any(|(name, _)| name == "X-Plexsim-No-Head-Gzip");
    let offered = !head_plain && wire
        .accept_encoding
        .as_deref()
        .is_some_and(|value| value.split(',').any(|piece| piece.split(';').next().map(str::trim) == Some("gzip")));
    if content_range.is_none() && !content_type.is_empty() && !part && !names.contains("content-encoding") {
        let asset = wire.path.starts_with("/web") || names.contains("accept-ranges");
        if offered && !asset && !body.is_empty() && GZIP_TYPES.iter().any(|kind| content_type.starts_with(kind)) {
            if let Some(squeezed) = gzip(&body) {
                lines.push(("Content-Encoding".to_string(), "gzip".to_string()));
                lines.push(("X-Plex-Content-Original-Length".to_string(), body.len().to_string()));
                lines.push(("X-Plex-Content-Compressed-Length".to_string(), squeezed.len().to_string()));
                body = squeezed;
            }
        }
    } else if let Some(range) = &content_range {
        // Cycle 33: a ranged CONTAINER answers the whole body's length as well as the
        // slice's; a ranged artwork/asset (which carries Accept-Ranges) the slice alone.
        if !names.contains("accept-ranges") {
            lines.push(("Content-Length".to_string(), range.rsplit('/').next().unwrap_or("0").to_string()));
        }
        lines.push(("Content-Range".to_string(), range.clone()));
    }
    if !content_type.is_empty() {
        lines.push(("Content-Type".to_string(), content_type.clone()));
    }
    lines.push(("Content-Length".to_string(), body.len().to_string()));
    lines.push(("X-Plex-Protocol".to_string(), "1.0".to_string()));
    if let Some(origin) = &wire.origin {
        if !raw {
            lines.push(("Access-Control-Allow-Origin".to_string(), origin.clone()));
            lines.push(("Access-Control-Expose-Headers".to_string(), "Location, Date".to_string()));
            lines.push(("Vary".to_string(), "Origin, X-Plex-Token".to_string()));
        }
    }
    lines.extend(extra);
    lines.push(("Date".to_string(), http_date()));
    let order = header_family(&lines, status, &wire.path, range_container);
    let lines = order_lines(lines, order);
    if wire.method == "HEAD" {
        body.clear();
    }
    (status, lines, body, keep)
}

pub(super) fn reason_phrase(status: u16) -> &'static str {
    match status {
        101 => "Switching Protocols",
        200 => "OK",
        201 => "Created",
        204 => "No Content",
        206 => "Partial Content",
        // Cycle 16: a real PMS says "Moved Temporarily", not Python's "Found".
        302 => "Moved Temporarily",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        406 => "Not Acceptable",
        409 => "Conflict",
        416 => "Requested Range Not Satisfiable",
        500 => "Internal Server Error",
        502 => "Bad Gateway",
        503 => "Service Unavailable",
        _ => "",
    }
}

/// Write one response through the envelope; answers whether the socket stays open.
pub(super) fn write_enveloped<S: Write>(stream: &mut S, response: &Response, wire: &Wire, keep: bool) -> Result<bool, String> {
    let (status, lines, body, keep) = envelope(response, wire, keep);
    let mut out = format!("HTTP/1.1 {} {}\r\n", status, reason_phrase(status));
    for (name, value) in &lines {
        out.push_str(&format!("{name}: {value}\r\n"));
    }
    out.push_str("\r\n");
    stream.write_all(out.as_bytes()).map_err(|e| e.to_string())?;
    stream.write_all(&body).map_err(|e| e.to_string())?;
    stream.flush().map_err(|e| e.to_string())?;
    Ok(keep)
}

/// `excludeElements=` as a property of the RESPONSE (`server.py` `_exclude_container`,
/// cycles 11, 15, 22, 70): every MediaContainer's rows -- and every hub's -- lose the
/// named child elements; dropping `Media` substitutes `mediaCount` and
/// `mediaCountOptimized`, dropping `Part` gives each Media a `partCount`. A decision
/// is the measured exception.
pub(super) fn exclude_container(response: &mut Response, query: &HashMap<String, String>, path: &str) {
    const EXCLUDABLE: &[&str] = &[
        "Media", "Part", "Genre", "Country", "Guid", "Rating", "Director", "Writer", "Role",
        "Producer", "CommonSenseMedia",
    ];
    let excluded = query
        .get("excludeElements")
        .map(|value| value.split(',').filter(|name| EXCLUDABLE.contains(name)).map(str::to_string).collect::<Vec<_>>())
        .unwrap_or_default();
    if excluded.is_empty()
        || path.starts_with("/video/:/transcode/universal/decision")
        || response.status != 200
        || !response.content_type.starts_with("application/json")
    {
        return;
    }
    let Ok(mut document) = serde_json::from_slice::<Value>(&response.body) else {
        return;
    };
    let Some(object) = document.as_object() else {
        return;
    };
    if object.len() != 1 || !object.contains_key("MediaContainer") {
        return;
    }
    fn trim_rows(rows: &mut [Value], excluded: &[String]) {
        for row in rows.iter_mut() {
            let Some(object) = row.as_object_mut() else { continue };
            // Cycle 22/70: the option reaches the rows INSIDE a detail row's inline
            // `Related` and `Extras` blocks too.
            for block in ["Related", "Extras"] {
                if let Some(holder) = object.get_mut(block) {
                    walk(holder, excluded);
                }
            }
            let media_count = object.get("Media").and_then(Value::as_array).map(Vec::len);
            for name in excluded {
                object.remove(name);
            }
            if excluded.iter().any(|name| name == "Media") {
                let count = media_count.map(|n| json!(n)).or_else(|| object.get("mediaCount").cloned()).unwrap_or(json!(0));
                object.insert("mediaCount".to_string(), count);
                object.insert("mediaCountOptimized".to_string(), json!(0));
            } else if excluded.iter().any(|name| name == "Part") {
                if let Some(media_rows) = object.get_mut("Media").and_then(Value::as_array_mut) {
                    for media in media_rows {
                        if let Some(media) = media.as_object_mut() {
                            if let Some(parts) = media.remove("Part") {
                                let count = parts.as_array().map_or(0, Vec::len);
                                media.insert("partCount".to_string(), json!(count));
                            }
                        }
                    }
                }
            }
        }
    }
    fn walk(container: &mut Value, excluded: &[String]) {
        if let Some(rows) = container.get_mut("Metadata").and_then(Value::as_array_mut) {
            trim_rows(rows, excluded);
        }
        if let Some(hubs) = container.get_mut("Hub").and_then(Value::as_array_mut) {
            for hub in hubs {
                walk(hub, excluded);
            }
        }
    }
    if let Some(body) = document.get_mut("MediaContainer") {
        walk(body, &excluded);
    }
    response.body = serde_json::to_vec(&document).unwrap_or_default();
}
