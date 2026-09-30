//! `/photo/:/transcode` -- the photo transcoder, ported from `server.py`'s `_route`.
//!
//! Cycles 27-52, measured on kids (1.43.4) and zen (1.42.2): the dimensions are validated
//! first, then five more parameters, then the `url=`. A relative url is FETCHED through the
//! server's own route table and the answer follows that fetch -- anything but a 200 is a
//! 404, a 200 that is not an image is the 500, and an image is the direct path's own
//! bytes -- so the transcoder never serves a picture the direct path refuses.

use super::*;
use regex::Regex;
use std::sync::OnceLock;

/// `_photo_int`: `0`, `1`, `01` and `+1` parse; `1.5`, empty, `abc` and a space do not.
fn photo_int(value: &str) -> bool {
    let digits = value.trim_start_matches(['+', '-']);
    !value.is_empty()
        && !digits.is_empty()
        && digits.bytes().all(|b| b.is_ascii_digit())
        && value
            .chars()
            .next()
            .is_some_and(|c| c == '+' || c == '-' || c.is_ascii_digit())
}

/// `_bool_option`'s grammar for a 0/1 value: `+?0*[01]`.
fn photo_bool(value: &str) -> bool {
    let digits = value.strip_prefix('+').unwrap_or(value);
    let trimmed = digits.trim_start_matches('0');
    !digits.is_empty()
        && digits.bytes().all(|b| b.is_ascii_digit())
        && (trimmed.is_empty() || trimmed == "1")
}

/// `urllib.parse.urlparse`'s scheme: letters first, then letters, digits, `+-.`, then `:`.
fn url_scheme(target: &str) -> Option<String> {
    let (scheme, _) = target.split_once(':')?;
    let mut chars = scheme.chars();
    let first = chars.next()?;
    if !first.is_ascii_alphabetic() {
        return None;
    }
    if !chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.')) {
        return None;
    }
    Some(scheme.to_ascii_lowercase())
}

thread_local! {
    static IN_TRANSCODE: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

impl Runtime {
    pub(super) fn route_photo_transcode(
        &mut self,
        request: &Request,
        query: &HashMap<String, String>,
    ) -> Response {
        for name in ["width", "height"] {
            let Some(value) = query.get(name).filter(|value| !value.is_empty()) else {
                return Response::photo_error(400, "plexsim: bad request");
            };
            if !photo_int(value) {
                return Response::photo_error(400, "plexsim: bad request");
            }
        }
        for name in ["opacity", "quality", "blur", "minSize", "upscale"] {
            let Some(value) = query.get(name) else {
                continue;
            };
            if !photo_int(value) {
                return Response::photo_error(400, "plexsim: bad request");
            }
            if matches!(name, "minSize" | "upscale") && !photo_bool(value) {
                return Response::photo_error(400, "plexsim: bad request");
            }
            if name == "blur"
                && value.starts_with('-')
                && value.trim_start_matches(['-', '0']) != ""
            {
                return Response::photo_error(500, "plexsim: server error");
            }
        }
        let Some(url) = query.get("url") else {
            return Response::photo_error(400, "plexsim: bad request");
        };
        let target = percent_decode(url);
        let scheme = url_scheme(&target);
        let transcoded = |mut response: Response| {
            response.headers.retain(|(name, _)| {
                !matches!(
                    name.as_str(),
                    "Cache-Control" | "Accept-Ranges" | "X-Plex-Local-Asset"
                )
            });
            response
                .headers
                .push(("Accept-Ranges".to_string(), "bytes".to_string()));
            response
                .headers
                .push(("Cache-Control".to_string(), "max-age=259200".to_string()));
            response
        };
        if matches!(scheme.as_deref(), Some("http" | "https")) {
            // An absolute url (a person thumb, or art handed out earlier) is fetched as
            // it is, at the TMDB variant nearest the width, once the lock is released.
            self.pending_image = Some(tmdb_at_width(
                &target,
                query.get("width").map(String::as_str),
            ));
            let mut response = Response::bytes(200, "image/jpeg");
            response.headers = vec![
                ("X-Plex-Protocol".to_string(), "1.0".to_string()),
                ("Date".to_string(), http_date()),
            ];
            return transcoded(response);
        }
        if scheme.is_some() || !target.starts_with('/') {
            return Response::photo_error(404, "plexsim: not found");
        }
        let (inner, _) = target.split_once('?').unwrap_or((target.as_str(), ""));
        if let Some(relative) = inner.strip_prefix("/:/resources/") {
            let response = self.route_bundled_resource(relative, false);
            if response.status != 200 {
                return Response::photo_error(response.status, "plexsim: not found");
            }
            let mut response = response;
            response
                .headers
                .insert(0, ("X-Plex-Protocol".to_string(), "1.0".to_string()));
            response.headers.push(("Date".to_string(), http_date()));
            return transcoded(response);
        }
        if IN_TRANSCODE.with(|cell| cell.get()) {
            return Response::photo_error(404, "plexsim: not found");
        }
        let segments = inner
            .split('/')
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>();
        if segments.last() == Some(&"metadata") && segments.contains(&"theme") {
            // A GET that MINTS a row must not run from here.
            return Response::photo_error(500, "plexsim: server error");
        }
        let mut headers = request.headers.clone();
        headers.insert("accept".to_string(), "application/json".to_string());
        // The transcoder's own width picks the composite's upstream variant, as the
        // oracle's `_at_width` does on the fetched image.
        let mut inner_target = target.clone();
        if inner.starts_with("/library/sections/")
            && inner.contains("/composite/")
            && !target.contains('?')
        {
            if let Some(width) = query.get("width") {
                inner_target = format!("{inner}?width={width}");
            }
        }
        let inner_request = Request {
            method: "GET".to_string(),
            target: inner_target,
            version: request.version.clone(),
            headers,
            body: Vec::new(),
            body_complete: true,
        };
        IN_TRANSCODE.with(|cell| cell.set(true));
        let fetched = self.request_route(&inner_request);
        IN_TRANSCODE.with(|cell| cell.set(false));
        if fetched.status != 200 {
            // Every refusal the direct path makes -- 400, 404, 500 -- is a 404 here.
            return Response::photo_error(404, "plexsim: not found");
        }
        if !fetched.content_type.starts_with("image/") {
            self.pending_image = None;
            return Response::photo_error(500, "plexsim: server error");
        }
        // The direct path answered with a remote image it has not fetched yet: the
        // transcoder asks for it at the variant nearest ITS width (`_at_width` over the
        // inner result), and the fetch happens once the lock is released.
        if let Some(url) = self.pending_image.take() {
            self.pending_image = Some(tmdb_at_width(
                &url,
                query.get("width").map(String::as_str),
            ));
        }
        transcoded(Response {
            status: 200,
            content_type: fetched.content_type,
            body: fetched.body,
            headers: vec![
                ("X-Plex-Protocol".to_string(), "1.0".to_string()),
                ("Date".to_string(), http_date()),
            ],
            close: false,
        })
    }
}

/// `_at_width` (`TMDB_WIDTHS`): plexsim cannot resize, so a TMDB url is re-pointed at the
/// smallest published variant at least as wide as the client asked for.
pub(super) fn tmdb_at_width(url: &str, width: Option<&str>) -> String {
    const TMDB_WIDTHS: [i64; 7] = [92, 154, 185, 342, 500, 780, 1280];
    let Some(wanted) = width.and_then(|value| value.trim().parse::<i64>().ok()) else {
        return url.to_string();
    };
    if !url.contains("image.tmdb.org") {
        return url.to_string();
    }
    let target = TMDB_WIDTHS
        .iter()
        .find(|variant| **variant >= wanted)
        .map(|variant| format!("w{variant}"))
        .unwrap_or_else(|| "original".to_string());
    let Ok(pattern) = Regex::new(r"/t/p/(w\d+|original)/") else {
        return url.to_string();
    };
    pattern
        .replace(url, format!("/t/p/{target}/").as_str())
        .into_owned()
}

/// `_serve_image`'s upstream half: fetch a remote artwork url once, cached in memory
/// (bounded, as the oracle's is), over plain HTTP or TLS against the system roots.
pub(super) fn fetch_remote_image(url: &str) -> Option<(Vec<u8>, String)> {
    static CACHE: OnceLock<Mutex<HashMap<String, (Vec<u8>, String)>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    let cached = cache.lock().ok().and_then(|map| map.get(url).cloned());
    info!(
        "ARTWORK {} {}",
        if cached.is_some() { "cache" } else { "fetch" },
        url.chars().take(110).collect::<String>()
    );
    if let Some(hit) = cached {
        return Some(hit);
    }
    let short = url.chars().take(80).collect::<String>();
    let fetched = super::upstream::fetch(
        "GET",
        url,
        &[
            ("User-Agent", super::upstream::UPSTREAM_UA.to_string()),
            ("Accept", "*/*".to_string()),
        ],
        b"",
        std::time::Duration::from_secs(20),
    );
    let (status, headers, body) = match fetched {
        Ok(fetched) => fetched,
        Err(error) => {
            debug!("artwork fetch failed for {short}: <urlopen error {error}>");
            return None;
        }
    };
    if !(200..300).contains(&status) {
        debug!(
            "artwork fetch failed for {short}: HTTP Error {status}: {}",
            super::wire::reason_phrase(status)
        );
        return None;
    }
    let content_type = headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case("content-type"))
        .map(|(_, value)| value.clone())
        .unwrap_or_else(|| "image/jpeg".to_string());
    let hit = (body, content_type);
    if let Ok(mut map) = cache.lock() {
        if map.len() > 512 {
            map.clear();
        }
        map.insert(url.to_string(), hit.clone());
    }
    Some(hit)
}
