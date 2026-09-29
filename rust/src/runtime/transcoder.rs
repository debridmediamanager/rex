//! `/photo/:/transcode` -- the photo transcoder, ported from `server.py`'s `_route`.
//!
//! Cycles 27-52, measured on kids (1.43.4) and zen (1.42.2): the dimensions are validated
//! first, then five more parameters, then the `url=`. A relative url is FETCHED through the
//! server's own route table and the answer follows that fetch -- anything but a 200 is a
//! 404, a 200 that is not an image is the 500, and an image is the direct path's own
//! bytes -- so the transcoder never serves a picture the direct path refuses.

use super::*;

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
            if name == "blur" && value.starts_with('-') && value.trim_start_matches(['-', '0']) != "" {
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
                !matches!(name.as_str(), "Cache-Control" | "Accept-Ranges" | "X-Plex-Local-Asset")
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
            // An absolute url is the agent's remote artwork; the native runtime has no
            // image fetcher, so it answers the placeholder the direct artwork path does.
            let mut response = Response::image(jpeg_placeholder());
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
            response.headers.insert(0, ("X-Plex-Protocol".to_string(), "1.0".to_string()));
            response.headers.push(("Date".to_string(), http_date()));
            return transcoded(response);
        }
        if IN_TRANSCODE.with(|cell| cell.get()) {
            return Response::photo_error(404, "plexsim: not found");
        }
        let segments = inner.split('/').filter(|s| !s.is_empty()).collect::<Vec<_>>();
        if segments.last() == Some(&"metadata") && segments.contains(&"theme") {
            // A GET that MINTS a row must not run from here.
            return Response::photo_error(500, "plexsim: server error");
        }
        let mut headers = request.headers.clone();
        headers.insert("accept".to_string(), "application/json".to_string());
        let inner_request = Request {
            method: "GET".to_string(),
            target: target.clone(),
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
            return Response::photo_error(500, "plexsim: server error");
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
