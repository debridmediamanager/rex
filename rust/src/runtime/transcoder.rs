//! `/photo/:/transcode` -- the photo transcoder, ported from `server.py`'s `_route`.
//!
//! Cycles 27-52, measured on plexlab (1.43.4) and the 1.42.2 reference: the dimensions are
//! validated first, then five more parameters, then the `url=`. A relative url is FETCHED
//! through the server's own route table and the answer follows that fetch -- anything but a
//! 200 is a 404, a 200 that is not an image is the 500, and an image is the direct path's
//! own bytes -- so the transcoder never serves a picture the direct path refuses.

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
    /// The transcoder's OWN `width`/`height` while its inner fetch runs: an item with
    /// no image to resolve is the oracle's `("png", query)`, the generated tile at the
    /// size the TRANSCODER was asked for, not the direct path's 342x513.
    pub(super) static TRANSCODE_SIZE: std::cell::Cell<Option<(u32, u32)>> =
        const { std::cell::Cell::new(None) };
}

impl Runtime {
    pub(super) fn route_photo_transcode(
        &mut self,
        request: &Request,
        query: &HashMap<String, String>,
    ) -> Response {
        for name in ["width", "height"] {
            let Some(value) = query.get(name).filter(|value| !value.is_empty()) else {
                return Response::photo_error(400, "rex: bad request");
            };
            if !photo_int(value) {
                return Response::photo_error(400, "rex: bad request");
            }
        }
        for name in ["opacity", "quality", "blur", "minSize", "upscale"] {
            let Some(value) = query.get(name) else {
                continue;
            };
            if !photo_int(value) {
                return Response::photo_error(400, "rex: bad request");
            }
            if matches!(name, "minSize" | "upscale") && !photo_bool(value) {
                return Response::photo_error(400, "rex: bad request");
            }
            if name == "blur"
                && value.starts_with('-')
                && value.trim_start_matches(['-', '0']) != ""
            {
                return Response::photo_error(500, "rex: server error");
            }
        }
        let Some(url) = query.get("url") else {
            return Response::photo_error(400, "rex: bad request");
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
            return Response::photo_error(404, "rex: not found");
        }
        let (inner, _) = target.split_once('?').unwrap_or((target.as_str(), ""));
        if let Some(relative) = inner.strip_prefix("/:/resources/") {
            let response = self.route_bundled_resource(relative, false);
            if response.status != 200 {
                return Response::photo_error(response.status, "rex: not found");
            }
            let mut response = response;
            response
                .headers
                .insert(0, ("X-Plex-Protocol".to_string(), "1.0".to_string()));
            response.headers.push(("Date".to_string(), http_date()));
            return transcoded(response);
        }
        if IN_TRANSCODE.with(|cell| cell.get()) {
            return Response::photo_error(404, "rex: not found");
        }
        let segments = inner
            .split('/')
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>();
        if segments.last() == Some(&"metadata") && segments.contains(&"theme") {
            // A GET that MINTS a row must not run from here.
            return Response::photo_error(500, "rex: server error");
        }
        let mut headers = super::inner_headers(request);
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
        let dimension = |name: &str| {
            query
                .get(name)
                .and_then(|value| value.trim().parse::<i64>().ok())
                .map(|value| value.clamp(1, 2000) as u32)
                .unwrap_or(1)
        };
        TRANSCODE_SIZE.with(|cell| cell.set(Some((dimension("width"), dimension("height")))));
        IN_TRANSCODE.with(|cell| cell.set(true));
        let fetched = self.request_route(&inner_request);
        IN_TRANSCODE.with(|cell| cell.set(false));
        TRANSCODE_SIZE.with(|cell| cell.set(None));
        if fetched.status != 200 {
            // Every refusal the direct path makes -- 400, 404, 500 -- is a 404 here.
            return Response::photo_error(404, "rex: not found");
        }
        // Cycle 83: an upload is served `image/jpeg` whatever its bytes, and the
        // transcoder DECODES them -- 32 bytes of text is its 500 on plexscratch42 -- so a
        // jpeg-labelled body that is no image is refused here too. (A remote image's
        // body is still empty at this point: it is fetched after the lock.)
        if !fetched.content_type.starts_with("image/")
            || (fetched.content_type == "image/jpeg"
                && !fetched.body.is_empty()
                && !artwork::image_type(&fetched.body).starts_with("image/"))
        {
            self.pending_image = None;
            return Response::photo_error(500, "rex: server error");
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
            doc: None,
        })
    }
}

/// `_at_width` (`TMDB_WIDTHS`): rex cannot resize, so a TMDB url is re-pointed at the
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
/// (bounded, as the oracle's is), over plain HTTP or TLS against the system roots. A source's
/// own artwork comes with its headers (`from_source`) and may be on rex's own networks, as the
/// source is; every other url is public only (`fetch_public`).
pub(super) fn fetch_remote_image(url: &str, from_source: Option<&[(&'static str, String)]>) -> Option<(Vec<u8>, String)> {
    static CACHE: OnceLock<Mutex<HashMap<String, (Vec<u8>, String)>>> = OnceLock::new();
    let cache = CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    // Kept apart: a client naming a source's artwork url (`/photo/:/transcode?url=`) must not
    // be served what the source's own fetch cached, which `fetch_public` would have refused.
    let key = if from_source.is_some() { format!("source {url}") } else { url.to_string() };
    let cached = cache.lock().ok().and_then(|map| map.get(&key).cloned());
    info!(
        "ARTWORK {} {}",
        if cached.is_some() { "cache" } else { "fetch" },
        url.chars().take(110).collect::<String>()
    );
    if let Some(hit) = cached {
        return Some(hit);
    }
    let short = url.chars().take(80).collect::<String>();
    let fetched = match from_source {
        Some(headers) => super::upstream::fetch("GET", url, headers, b"", std::time::Duration::from_secs(20)),
        None => super::upstream::fetch_public(
            "GET",
            url,
            // `_serve_image`'s urllib request: the upstream User-Agent and no `Accept`.
            &[("User-Agent", super::upstream::UPSTREAM_UA.to_string())],
            std::time::Duration::from_secs(20),
        ),
    };
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
        remember_image(&mut map, &key, hit.clone());
    }
    Some(hit)
}

/// Plex's artwork is full size (a background runs to megabytes), so 512 entries alone could
/// hold a gigabyte on a host with 8 GB; the cache is bounded by bytes too.
const IMAGE_CACHE_BYTES: usize = 64 << 20;

fn remember_image(map: &mut HashMap<String, (Vec<u8>, String)>, url: &str, hit: (Vec<u8>, String)) {
    let held: usize = map.values().map(|(body, _)| body.len()).sum();
    if map.len() > 512 || held + hit.0.len() > IMAGE_CACHE_BYTES {
        map.clear();
    }
    if hit.0.len() <= IMAGE_CACHE_BYTES {
        map.insert(url.to_string(), hit);
    }
}

#[cfg(test)]
mod image_cache_tests {
    use super::*;

    #[test]
    fn the_image_cache_never_holds_more_than_its_byte_budget() {
        let mut map = HashMap::new();
        let image = |n: usize| (vec![0u8; n], "image/jpeg".to_string());
        for i in 0..40 {
            remember_image(&mut map, &format!("u{i}"), image(3 << 20));
            assert!(map.values().map(|(body, _)| body.len()).sum::<usize>() <= IMAGE_CACHE_BYTES);
        }
        remember_image(&mut map, "huge", image(IMAGE_CACHE_BYTES + 1));
        assert!(!map.contains_key("huge"));
    }
}
