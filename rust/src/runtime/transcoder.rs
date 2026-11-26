//! `/photo/:/transcode` -- the photo transcoder, ported from `server.py`'s `_route`.
//!
//! Cycles 27-52, measured on plexlab (1.43.4) and the 1.42.2 reference: the dimensions are
//! validated first, then five more parameters, then the `url=`. A relative url is FETCHED
//! through the server's own route table and the answer follows that fetch -- anything but a
//! 200 is a 404, a 200 that is not an image is the 500, and an image is the direct path's
//! own bytes -- so the transcoder never serves a picture the direct path refuses. Those bytes are
//! then fitted to the asked size (`PhotoFit`, with an FFmpeg) as plexlab answers it (2026-11-17).

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
    /// How many transcodes deep the current inner fetch is. Plexamp names the transcoder
    /// inside its own `url` (`/photo/:/transcode?url=%2Fphoto%2F%3A%2Ftranscode%3F...`) for an
    /// artist's and album's header art, and plexav (1.43.4, 2026-10-31) answered that 200 at
    /// two and three levels; the oracle's one-level refusal does not hold for it.
    static TRANSCODE_DEPTH: std::cell::Cell<u8> = const { std::cell::Cell::new(0) };
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
        // Three levels measured; past eight a crafted url is refused rather than followed.
        if TRANSCODE_DEPTH.with(|cell| cell.get()) >= 8 {
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
        let outer_size = TRANSCODE_SIZE.with(|cell| cell.replace(Some((dimension("width"), dimension("height")))));
        TRANSCODE_DEPTH.with(|cell| cell.set(cell.get() + 1));
        let fetched = self.request_route(&inner_request);
        TRANSCODE_DEPTH.with(|cell| cell.set(cell.get() - 1));
        TRANSCODE_SIZE.with(|cell| cell.set(outer_size));
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

/// `_at_width` (`TMDB_WIDTHS`): a TMDB url is re-pointed at the smallest published variant at
/// least as wide as the client asked for, which `PhotoFit` then scales to the box. TMDB's
/// `original` runs to megabytes (Sintel's poster: 1000x1500, 338 KB) where plexlab fetched it
/// whole; the answer's size is the same, from a smaller fetch.
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
    static CACHE: OnceLock<Mutex<ImageCache>> = OnceLock::new();
    let cache = CACHE.get_or_init(Default::default);
    // Kept apart: a client naming a source's artwork url (`/photo/:/transcode?url=`) must not
    // be served what the source's own fetch cached, which `fetch_public` would have refused.
    let key = String::from_utf8_lossy(&remote_picture(url, from_source)).into_owned();
    let cached = cache.lock().ok().and_then(|mut map| map.get(&key));
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
        map.put(&key, hit.clone(), IMAGE_CACHE_ENTRIES);
    }
    Some(hit)
}

/// Plex's artwork is full size (a background runs to megabytes), so 512 entries alone could
/// hold a gigabyte on a host with 8 GB; the cache is bounded by bytes too.
const IMAGE_CACHE_BYTES: usize = 64 << 20;
const IMAGE_CACHE_ENTRIES: usize = 512;

/// Pictures by key within a count and `IMAGE_CACHE_BYTES`, the least recently used going first,
/// one at a time. Both caches used to be cleared whole once full: with 3 MB backgrounds the 22nd
/// dropped the 21 before it, and a grid scrolled past the budget fetched everything again (rex#24).
#[derive(Default)]
struct ImageCache {
    map: HashMap<String, (Vec<u8>, String, u64)>,
    held: usize,
    tick: u64,
}

impl ImageCache {
    fn get(&mut self, key: &str) -> Option<(Vec<u8>, String)> {
        self.tick += 1;
        let tick = self.tick;
        let (body, content_type, used) = self.map.get_mut(key)?;
        *used = tick;
        Some((body.clone(), content_type.clone()))
    }

    fn put(&mut self, key: &str, (body, content_type): (Vec<u8>, String), entries: usize) {
        if body.len() > IMAGE_CACHE_BYTES {
            return;
        }
        if let Some((old, _, _)) = self.map.remove(key) {
            self.held -= old.len();
        }
        while self.map.len() >= entries || self.held + body.len() > IMAGE_CACHE_BYTES {
            let Some(oldest) = self.map.iter().min_by_key(|(_, (_, _, used))| *used).map(|(key, _)| key.clone()) else {
                break;
            };
            if let Some((gone, _, _)) = self.map.remove(&oldest) {
                self.held -= gone.len();
            }
        }
        self.tick += 1;
        self.held += body.len();
        self.map.insert(key.to_string(), (body, content_type, self.tick));
    }
}

/// A fitted poster is tens of kilobytes, so the same budget holds thousands of them.
const FITTED_CACHE_ENTRIES: usize = 8192;

static FITTED: OnceLock<Mutex<ImageCache>> = OnceLock::new();

/// A remote picture as the caches name it: a source's own artwork by the login it is fetched with,
/// apart from the same url fetched public or with another source's login (two sources can share a host).
pub(super) fn remote_picture(url: &str, from_source: Option<&[(&'static str, String)]>) -> Vec<u8> {
    match from_source {
        Some(headers) => format!("source {headers:?} {url}").into_bytes(),
        None => url.as_bytes().to_vec(),
    }
}

/// A fitted answer is labelled one of these, so the disk cache's file name carries its label.
const DISK_TYPES: [(&str, &str); 2] = [("image/jpeg", "jpg"), ("image/png", "png")];

/// `/photo/:/transcode`'s answer at the size the client asked for, as plexlab (1.43.4) answers
/// it (`tests/fixtures/photos/transcode_s1434.json`; findings, "The photo transcoder, measured"):
/// the picture fitted inside `width`x`height`, or covering it with `minSize=1`, never past its
/// own size without `upscale=1`, upright by its EXIF orientation; a JPEG at `quality=` (75 by
/// default) with full-resolution chroma, a PNG kept a PNG for its alpha, `format=png`/`jpeg`
/// choosing; and every answer labelled `image/jpeg` but `format=png`'s, a PNG's bytes included.
/// Plex's agent artwork is the original (a poster 1920x2880, ~630 KB) where Plex Web's grid asks
/// for 240x360 (~18 KB at plexlab), so unscaled a page of posters is tens of megabytes. The
/// answer is kept by the picture the route resolved and the asked shape, in memory and, under
/// `REX_ARTWORK_CACHE_MB`, on disk, so a hit fetches nothing.
pub(super) struct PhotoFit {
    key: String,
    width: u32,
    height: u32,
    cover: bool,
    upscale: bool,
    /// `format=`: `Some(true)` PNG, `Some(false)` JPEG, `None` as the source (PNG stays PNG).
    png: Option<bool>,
    quality: u32,
    ffmpeg: std::path::PathBuf,
    dir: Option<std::path::PathBuf>,
}

impl PhotoFit {
    /// `None` for what plexlab refused (a negative side, 500) and for a route that resolved no
    /// picture here (a photo's Part, streamed from its source after the lock: served as it is).
    /// `picture` is what the route resolved: the remote url it is about to fetch (the agent's
    /// art, its name changing with the picture), or else the bytes it already read. Neither
    /// carries the viewer, so every viewer shares one kept picture, and a poster pick behind the
    /// same request url is a new picture. `dir` is where the disk cache would live.
    pub(super) fn new(query: &HashMap<String, String>, picture: &[u8], ffmpeg: std::path::PathBuf, dir: std::path::PathBuf) -> Option<Self> {
        if picture.is_empty() {
            return None;
        }
        let side = |name: &str| query.get(name).and_then(|value| value.trim().parse::<i64>().ok());
        let (width, height) = match (side("width")?, side("height")?) {
            (width, height) if width < 0 || height < 0 => return None,
            // A zero side is plexlab's 256x256 box: 0x0, 0x360 and 240x0 all answered 171x256.
            (0, _) | (_, 0) => (256, 256),
            (width, height) => (u32::try_from(width).ok()?, u32::try_from(height).ok()?),
        };
        let flag = |name: &str| query.get(name).is_some_and(|value| photo_bool(value) && value.trim_start_matches(['+', '0']) == "1");
        let (cover, upscale) = (flag("minSize"), flag("upscale"));
        let png = match query.get("format").map(|value| value.to_ascii_lowercase()).as_deref() {
            Some("png") => Some(true),
            Some("jpeg" | "jpg") => Some(false),
            _ => None,
        };
        // plexlab reads `quality=` by its low seven bits, a 0 (or none, or a negative) its 75 and
        // past 100 100: -1, 0, 128 and 256 answered 75; 101, 127 and 255 100; 129 1, 150 22, 300 44.
        let quality = match query.get("quality").and_then(|value| value.trim().parse::<i64>().ok()) {
            Some(value) if value > 0 && value % 128 != 0 => (value % 128).min(100) as u32,
            _ => 75,
        };
        let mut digest = Sha1::new();
        digest.update(picture);
        digest.update(format!(" {width}x{height} {cover} {upscale} {png:?} {quality}").as_bytes());
        let key = digest.finalize().iter().map(|byte| format!("{byte:02x}")).collect::<String>();
        let enabled = artwork_cache_budget().is_some();
        Some(Self { key, width, height, cover, upscale, png, quality, ffmpeg, dir: enabled.then(|| dir.join("artwork-cache")) })
    }

    /// A kept answer, from memory or disk.
    pub(super) fn cached(&self) -> Option<(Vec<u8>, String)> {
        let memory = FITTED.get_or_init(Default::default);
        if let Some(hit) = memory.lock().ok().and_then(|mut map| map.get(&self.key)) {
            return Some(hit);
        }
        let dir = self.dir.as_ref()?;
        let (path, content_type) = DISK_TYPES
            .into_iter()
            .map(|(content_type, ext)| (dir.join(format!("{}.{ext}", self.key)), content_type))
            .find(|(path, _)| path.is_file())?;
        let body = std::fs::read(&path).ok()?;
        // Read is use: the oldest-read file is the first pruned.
        let _ = std::fs::File::options().write(true).open(&path).and_then(|file| file.set_modified(std::time::SystemTime::now()));
        let hit = (body, content_type.to_string());
        if let Ok(mut map) = memory.lock() {
            map.put(&self.key, hit.clone(), FITTED_CACHE_ENTRIES);
        }
        Some(hit)
    }

    /// Fit the picture the route answered; anything not fitted is served as it was and kept
    /// nowhere, so a picture FFmpeg could not fit this time is tried again the next.
    pub(super) fn apply(&self, response: &mut Response) {
        if response.status != 200 {
            return;
        }
        let Some(fitted) = self.fit(&response.body) else {
            return;
        };
        (response.body, response.content_type) = fitted;
        self.remember((response.body.clone(), response.content_type.clone()));
    }

    fn fit(&self, picture: &[u8]) -> Option<(Vec<u8>, String)> {
        let (demuxer, width, height, orientation) = picture_header(picture)?;
        let (width, height) = if orientation >= 5 { (height, width) } else { (width, height) };
        let (to_width, to_height) = fitted_size((width, height), (self.width, self.height), self.cover, self.upscale)?;
        // EXIF orientation applied here rather than by FFmpeg: Ubuntu 22.04's FFmpeg 4.4 left the
        // orientation-6 picture sideways, jellyfin's 8.1.3 and FFmpeg 9.0 turned it themselves.
        let turn = ["", "", "hflip,", "hflip,vflip,", "vflip,", "transpose=0,", "transpose=1,", "transpose=3,", "transpose=2,"];
        let filter = format!("{}scale={to_width}:{to_height}", turn.get(orientation).copied().unwrap_or(""));
        let png = self.png.unwrap_or(demuxer == "png_pipe");
        match scale_image(&self.ffmpeg, picture, demuxer, &filter, png, qscale(self.quality)) {
            Some(body) => Some((body, if self.png == Some(true) { "image/png" } else { "image/jpeg" }.to_string())),
            None => {
                static WARNED: std::sync::Once = std::sync::Once::new();
                WARNED.call_once(|| warning!("photo transcode: {} could not scale a picture; its original is served", self.ffmpeg.display()));
                None
            }
        }
    }

    fn remember(&self, hit: (Vec<u8>, String)) {
        let ext = DISK_TYPES.iter().find(|(content_type, _)| *content_type == hit.1).map(|(_, ext)| ext);
        if let (Some(dir), Some(ext)) = (&self.dir, ext) {
            static WRITES: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
            let write = WRITES.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            // Two requests for one picture must not share a partial file.
            let partial = dir.join(format!("{}.{ext}.{}-{write}.partial", self.key, std::process::id()));
            if std::fs::create_dir_all(dir).is_ok() && std::fs::write(&partial, &hit.0).is_ok() {
                let _ = std::fs::rename(&partial, dir.join(format!("{}.{ext}", self.key)));
            }
            if let Some(budget) = artwork_cache_budget().filter(|_| write.is_multiple_of(128)) {
                prune_artwork_cache(dir, budget);
            }
        }
        if let Ok(mut map) = FITTED.get_or_init(Default::default).lock() {
            map.put(&self.key, hit, FITTED_CACHE_ENTRIES);
        }
    }
}

/// What FFmpeg is to read the picture as, and its size and EXIF orientation (1 upright), from
/// its header alone; `None` for anything but JPEG, PNG and WebP, the three Plex's artwork comes
/// in, which is served as it is rather than handed to FFmpeg to guess at.
fn picture_header(picture: &[u8]) -> Option<(&'static str, u32, u32, usize)> {
    match artwork::image_type(picture) {
        "image/jpeg" => {
            let mut decoder = zune_jpeg::JpegDecoder::new(std::io::Cursor::new(picture));
            decoder.decode_headers().ok()?;
            let (width, height) = decoder.dimensions()?;
            let orientation = decoder.exif().map_or(1, |tiff| exif_orientation(tiff));
            Some(("jpeg_pipe", u32::try_from(width).ok()?, u32::try_from(height).ok()?, orientation))
        }
        "image/png" if picture.len() >= 24 && &picture[12..16] == b"IHDR" => {
            let word = |at: usize| u32::from_be_bytes([picture[at], picture[at + 1], picture[at + 2], picture[at + 3]]);
            Some(("png_pipe", word(16), word(20), 1))
        }
        "image/webp" => {
            let (width, height) = image_webp::WebPDecoder::new(std::io::Cursor::new(picture)).ok()?.dimensions();
            Some(("webp_pipe", width, height, 1))
        }
        _ => None,
    }
}

/// The Orientation tag (0x0112) of an EXIF block's first directory, from its TIFF header on.
fn exif_orientation(tiff: &[u8]) -> usize {
    let big = tiff.starts_with(b"MM");
    let half = |at: usize| tiff.get(at..at + 2).map(|b| if big { u16::from_be_bytes([b[0], b[1]]) } else { u16::from_le_bytes([b[0], b[1]]) });
    let word = |at: usize| tiff.get(at..at + 4).map(|b| if big { u32::from_be_bytes([b[0], b[1], b[2], b[3]]) } else { u32::from_le_bytes([b[0], b[1], b[2], b[3]]) });
    let Some(directory) = word(4).map(|at| at as usize) else {
        return 1;
    };
    (0..half(directory).unwrap_or(0) as usize)
        .map(|entry| directory + 2 + entry * 12)
        .find(|at| half(*at) == Some(0x0112))
        .and_then(|at| half(at + 8))
        .map(usize::from)
        .filter(|value| (1..=8).contains(value))
        .unwrap_or(1)
}

/// The size plexlab answered for a picture of `size` asked into `bounds`: scaled to fit (or with
/// `cover`, to cover), at most its own size without `upscale`, each side rounded to the nearest
/// pixel (786x132 into 400x200 is 400x67; covering it 1191x200). `None` past 10000 pixels a
/// side, where it answered 500 (11000 into a poster's box; 6667x10000 was answered): served as
/// it is, as before, rather than the 500.
fn fitted_size((width, height): (u32, u32), (across, down): (u32, u32), cover: bool, upscale: bool) -> Option<(u32, u32)> {
    if width == 0 || height == 0 {
        return None;
    }
    let (x, y) = (f64::from(across) / f64::from(width), f64::from(down) / f64::from(height));
    let scale = if cover { x.max(y) } else { x.min(y) };
    let scale = if upscale { scale } else { scale.min(1.0) };
    let side = |length: u32| (f64::from(length) * scale).round().max(1.0);
    let (to_width, to_height) = (side(width), side(height));
    (to_width <= 10000.0 && to_height <= 10000.0).then_some((to_width as u32, to_height as u32))
}

/// FFmpeg's MJPEG writes no IJG tables, so PMS's IJG `quality` becomes the qscale whose files
/// came out the size libjpeg's do: its scaling percentage over ten (75 is 5, 90 is 2, 50 is 10),
/// within 2..31 (findings, "The photo transcoder, measured").
fn qscale(quality: u32) -> u32 {
    let scaling = if quality < 50 { 5000 / quality.max(1) } else { 200 - 2 * quality.min(100) };
    ((scaling + 5) / 10).clamp(2, 31)
}

/// `REX_ARTWORK_CACHE_MB`: the disk cache's budget in megabytes; unset or 0 keeps it off.
fn artwork_cache_budget() -> Option<u64> {
    static BUDGET: OnceLock<Option<u64>> = OnceLock::new();
    *BUDGET.get_or_init(|| std::env::var("REX_ARTWORK_CACHE_MB").ok()?.trim().parse::<u64>().ok().filter(|mb| *mb > 0).map(|mb| mb << 20))
}

/// The least recently read files go until the cache is under 90% of `budget` bytes.
fn prune_artwork_cache(dir: &std::path::Path, budget: u64) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    let mut files = entries
        .flatten()
        .filter_map(|entry| {
            let meta = entry.metadata().ok()?;
            Some((meta.modified().ok()?, meta.len(), entry.path()))
        })
        .collect::<Vec<_>>();
    let mut held: u64 = files.iter().map(|(_, len, _)| len).sum();
    files.sort();
    for (_, len, path) in files {
        if held <= budget / 10 * 9 {
            break;
        }
        if std::fs::remove_file(&path).is_ok() {
            held -= len;
        }
    }
}

/// At most two pictures in FFmpeg at once, each within `SCALE_TIME`: a grid asks for dozens at
/// once, and a small host (a phone running rex for Android) must not start dozens of decoders.
/// ponytail: a picture killed at `SCALE_TIME` is tried again on every request, holding a slot
/// for 15 s each time; remember such a key for a while if one is ever seen.
const SCALE_SLOTS: usize = 2;
const SCALE_WAIT: std::time::Duration = std::time::Duration::from_secs(10);
const SCALE_TIME: std::time::Duration = std::time::Duration::from_secs(15);
static SCALING: (Mutex<usize>, std::sync::Condvar) = (Mutex::new(0), std::sync::Condvar::new());

struct ScaleSlot;

impl ScaleSlot {
    /// A slot within `SCALE_WAIT`, else `None`: the picture is served as it is.
    fn take() -> Option<Self> {
        let running = SCALING.0.lock().ok()?;
        let (mut running, _) = SCALING.1.wait_timeout_while(running, SCALE_WAIT, |running| *running >= SCALE_SLOTS).ok()?;
        if *running >= SCALE_SLOTS {
            return None;
        }
        *running += 1;
        Some(Self)
    }
}

impl Drop for ScaleSlot {
    fn drop(&mut self) {
        if let Ok(mut running) = SCALING.0.lock() {
            *running -= 1;
        }
        SCALING.1.notify_one();
    }
}

/// One picture through FFmpeg on pipes, read as `demuxer` with nothing but the pipe to open (a
/// remote body FFmpeg guessed the format of could be a playlist naming other urls), `filter`
/// applied, written as PNG or as a 4:4:4 JPEG at `qscale`. Killed past `SCALE_TIME`.
fn scale_image(ffmpeg: &std::path::Path, image: &[u8], demuxer: &str, filter: &str, png: bool, qscale: u32) -> Option<Vec<u8>> {
    use std::io::{Read as _, Write as _};
    use std::process::{Command, Stdio};
    const MOST: u64 = 256 << 20;
    let _slot = ScaleSlot::take()?;
    let mut command = Command::new(ffmpeg);
    command.args(["-hide_banner", "-loglevel", "error", "-threads", "1", "-noautorotate", "-protocol_whitelist", "pipe", "-f", demuxer]);
    command.args(["-i", "pipe:0", "-frames:v", "1", "-vf", filter, "-threads", "1", "-f", "image2pipe"]);
    if png {
        command.args(["-c:v", "png"]);
        // A PNG's as RGBA: a palette one scaled kept its palette but lost its transparency. A JPEG's stays RGB, as plexlab's.
        if demuxer == "png_pipe" {
            command.args(["-pix_fmt", "rgba"]);
        }
    } else {
        command.args(["-c:v", "mjpeg", "-pix_fmt", "yuvj444p", "-q:v", &qscale.to_string()]);
    }
    command.arg("pipe:1").stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
    let mut child = super::universal::spawn_tied(command).ok()?;
    let (mut stdin, stdout) = (child.stdin.take()?, child.stdout.take()?);
    let input = image.to_vec();
    // Written and read on threads of their own: FFmpeg may answer before it has read everything.
    let writer = std::thread::spawn(move || stdin.write_all(&input));
    let reader = std::thread::spawn(move || {
        let mut out = Vec::new();
        stdout.take(MOST + 1).read_to_end(&mut out).map(|_| out)
    });
    let deadline = std::time::Instant::now() + SCALE_TIME;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if std::time::Instant::now() < deadline => std::thread::sleep(std::time::Duration::from_millis(5)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                break None;
            }
        }
    };
    let _ = writer.join();
    let out = reader.join().ok()?.ok()?;
    (status?.success() && !out.is_empty() && (out.len() as u64) <= MOST).then_some(out)
}

#[cfg(test)]
mod image_cache_tests {
    use super::*;

    fn image(n: usize) -> (Vec<u8>, String) {
        (vec![0u8; n], "image/jpeg".to_string())
    }

    #[test]
    fn the_image_cache_never_holds_more_than_its_byte_budget() {
        let mut cache = ImageCache::default();
        for i in 0..40 {
            cache.put(&format!("u{i}"), image(3 << 20), IMAGE_CACHE_ENTRIES);
            assert!(cache.map.values().map(|(body, _, _)| body.len()).sum::<usize>() <= IMAGE_CACHE_BYTES);
            assert_eq!(cache.held, cache.map.values().map(|(body, _, _)| body.len()).sum::<usize>());
        }
        cache.put("huge", image(IMAGE_CACHE_BYTES + 1), IMAGE_CACHE_ENTRIES);
        assert!(!cache.map.contains_key("huge"));
    }

    /// rex#24: the 22nd 3 MB background cleared the 21 before it; now only the oldest goes, and a
    /// picture read again since is kept over one that was not.
    #[test]
    fn a_full_image_cache_lets_only_its_least_recently_used_go() {
        let mut cache = ImageCache::default();
        for i in 0..21 {
            cache.put(&format!("u{i}"), image(3 << 20), IMAGE_CACHE_ENTRIES);
        }
        assert!(cache.get("u0").is_some());
        cache.put("u21", image(3 << 20), IMAGE_CACHE_ENTRIES);
        assert_eq!(cache.map.len(), 21, "one picture made room, not all of them");
        assert!(cache.map.contains_key("u0"), "read again, so not the least recently used");
        assert!(!cache.map.contains_key("u1"));
        // By count as well: the fitted cache's 8192, here 3.
        let mut cache = ImageCache::default();
        for key in ["a", "b", "c", "d"] {
            cache.put(key, image(10), 3);
        }
        let mut kept = cache.map.keys().cloned().collect::<Vec<_>>();
        kept.sort();
        assert_eq!(kept, ["b", "c", "d"]);
    }

    /// Every size in plexlab's answers (`tests/fixtures/photos/transcode_s1434.json`) for its
    /// poster, clearLogo and the EXIF picture, worked out from the asked box alone.
    #[test]
    fn fitted_sizes_are_plexlabs() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!("../../../tests/fixtures/photos/transcode_s1434.json")).unwrap();
        let mut compared = 0;
        for answer in fixture["answers"].as_array().unwrap() {
            let source = &fixture["sources"][answer["source"].as_str().unwrap()];
            let query = answer["query"].as_array().unwrap().iter().map(|pair| (pair[0].as_str().unwrap().to_string(), pair[1].as_str().unwrap().to_string())).collect::<HashMap<_, _>>();
            let size = |value: &serde_json::Value| (value[0].as_u64().unwrap() as u32, value[1].as_u64().unwrap() as u32);
            let (width, height) = size(&source["size"]);
            let upright = if source["orientation"].as_u64().unwrap_or(1) >= 5 { (height, width) } else { (width, height) };
            let fit = PhotoFit::new(&query, b"picture", "ffmpeg".into(), std::env::temp_dir());
            let ours = fit.as_ref().and_then(|fit| fitted_size(upright, (fit.width, fit.height), fit.cover, fit.upscale));
            match answer["status"].as_u64() {
                Some(200) => assert_eq!(ours, Some(size(&answer["size"])), "{query:?}"),
                // plexlab's 500s: past 10000 a side and a negative side are served as they are;
                // a 1-pixel side (1x100 on the poster) is fitted, not refused (not emulated).
                _ => assert!(ours.is_none() || query.get("width").map(String::as_str) == Some("1"), "{query:?}"),
            }
            compared += 1;
        }
        assert!(compared >= 60, "{compared}");
    }

    #[test]
    fn quality_is_read_as_plexlab_reads_it() {
        let quality = |value: &str| {
            let query = [("width", "240"), ("height", "360"), ("quality", value)].iter().map(|(k, v)| (k.to_string(), v.to_string())).collect::<HashMap<_, _>>();
            PhotoFit::new(&query, b"picture", "ffmpeg".into(), std::env::temp_dir()).unwrap().quality
        };
        for (asked, read) in [("1", 1), ("10", 10), ("50", 50), ("90", 90), ("100", 100), ("-1", 75), ("0", 75), ("101", 100), ("127", 100),
                              ("128", 75), ("129", 1), ("150", 22), ("200", 72), ("255", 100), ("256", 75), ("300", 44)] {
            assert_eq!(quality(asked), read, "quality={asked}");
        }
        assert_eq!((qscale(75), qscale(90), qscale(50), qscale(10), qscale(100), qscale(1)), (5, 2, 10, 31, 2, 31));
    }

    #[test]
    fn exif_orientation_is_read_either_way_round() {
        // TIFF header, one directory entry: Orientation, SHORT, 1, value.
        let tiff = |big: bool, value: u16| {
            let half = |v: u16| if big { v.to_be_bytes() } else { v.to_le_bytes() };
            let word = |v: u32| if big { v.to_be_bytes() } else { v.to_le_bytes() };
            let mut out = if big { b"MM".to_vec() } else { b"II".to_vec() };
            out.extend(half(42));
            out.extend(word(8));
            out.extend(half(1));
            out.extend(half(0x0112));
            out.extend(half(3));
            out.extend(word(1));
            out.extend(half(value));
            out.extend([0, 0, 0, 0, 0, 0]);
            out
        };
        assert_eq!(exif_orientation(&tiff(true, 6)), 6);
        assert_eq!(exif_orientation(&tiff(false, 8)), 8);
        assert_eq!(exif_orientation(&tiff(false, 99)), 1);
        assert_eq!(exif_orientation(b"II*\0"), 1);
    }

    #[test]
    fn the_artwork_cache_reads_back_from_disk_and_prunes_the_least_recently_read() {
        let dir = std::env::temp_dir().join(format!("rex-artwork-cache-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let query = [("width", "240"), ("height", "360"), ("url", "/library/metadata/7/thumb/7?X-Plex-Token=abc")]
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect::<HashMap<_, _>>();
        let mut fit = PhotoFit::new(&query, b"https://metadata-static.plex.tv/a.jpg", "ffmpeg".into(), dir.clone()).unwrap();
        fit.dir = Some(dir.clone());
        // The key is the picture the route resolved, so a poster pick behind the same request url
        // (which every viewer asks with their own token) is a new key, and one picture is one key.
        let key_of = |picture: &[u8]| PhotoFit::new(&query, picture, "ffmpeg".into(), dir.clone()).unwrap().key;
        assert_eq!(key_of(b"https://metadata-static.plex.tv/a.jpg"), fit.key);
        assert_ne!(key_of(b"https://metadata-static.plex.tv/b.jpg"), fit.key);
        // Nor does another quality of the same picture.
        let mut other = query.clone();
        other.insert("quality".to_string(), "90".to_string());
        assert_ne!(PhotoFit::new(&other, b"https://metadata-static.plex.tv/a.jpg", "ffmpeg".into(), dir.clone()).unwrap().key, fit.key);
        // Nothing resolved here (a photo's Part, streamed later) is nothing to fit.
        assert!(PhotoFit::new(&query, b"", "ffmpeg".into(), dir.clone()).is_none());
        // A source's own artwork never answers a public request for the same url.
        let login = |user: &str| vec![("Authorization", format!("Basic {user}"))];
        let (one, two) = (login("one"), login("two"));
        assert_ne!(key_of(&remote_picture("https://source/a.jpg", Some(&one))), key_of(&remote_picture("https://source/a.jpg", None)));
        // Nor does one source's, from another source on the same host.
        assert_ne!(key_of(&remote_picture("https://source/a.jpg", Some(&one))), key_of(&remote_picture("https://source/a.jpg", Some(&two))));
        let forget = |fit: &PhotoFit| FITTED.get_or_init(Default::default).lock().unwrap().map.remove(&fit.key);
        // A kept answer comes back from disk, after a restart (memory forgotten), with its label;
        // one of another label stays in memory only.
        for content_type in ["image/jpeg", "image/png"] {
            fit.remember((b"fitted bytes".to_vec(), content_type.to_string()));
            forget(&fit);
            assert_eq!(fit.cached(), Some((b"fitted bytes".to_vec(), content_type.to_string())));
            std::fs::remove_dir_all(&dir).unwrap();
        }
        fit.remember((b"webp bytes".to_vec(), "image/webp".to_string()));
        forget(&fit);
        assert_eq!(fit.cached(), None);
        // Pruning removes the least recently read file first, down to 90% of the budget.
        std::fs::create_dir_all(&dir).unwrap();
        let now = std::time::SystemTime::now();
        for (name, age) in [("old.jpg", 30), ("mid.jpg", 20), ("new.jpg", 10)] {
            std::fs::write(dir.join(name), vec![0u8; 100]).unwrap();
            let file = std::fs::File::options().write(true).open(dir.join(name)).unwrap();
            file.set_modified(now - std::time::Duration::from_secs(age)).unwrap();
        }
        prune_artwork_cache(&dir, 250);
        let mut left = std::fs::read_dir(&dir).unwrap().map(|entry| entry.unwrap().file_name().into_string().unwrap()).collect::<Vec<_>>();
        left.sort();
        assert_eq!(left, ["mid.jpg", "new.jpg"]);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// Run where FFmpeg is installed:
    /// cargo test fits_a_poster_to_the_box -- --ignored
    /// REX_THUMBNAIL_TEST_FFMPEG may name an absolute FFmpeg executable.
    #[test]
    #[ignore = "requires real FFmpeg"]
    fn fits_a_poster_to_the_box() {
        use std::process::Command;
        let ffmpeg = std::path::PathBuf::from(std::env::var_os("REX_THUMBNAIL_TEST_FFMPEG").unwrap_or_else(|| "ffmpeg".into()));
        let make = |source: &str, codec: &[&str]| {
            Command::new(&ffmpeg)
                .args(["-v", "error", "-f", "lavfi", "-i", source, "-frames:v", "1", "-f", "image2pipe"])
                .args(codec)
                .arg("pipe:1")
                .output()
                .unwrap()
                .stdout
        };
        let poster = make("testsrc2=s=1920x2880", &["-c:v", "mjpeg", "-q:v", "2"]);
        let fit = |picture: &[u8], pairs: &[(&str, &str)]| {
            let query = pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect::<HashMap<_, _>>();
            let fit = PhotoFit::new(&query, picture, ffmpeg.clone(), std::env::temp_dir()).unwrap();
            let mut response = Response::bytes(200, "image/jpeg");
            response.body = picture.to_vec();
            fit.apply(&mut response);
            assert_eq!(fit.cached().unwrap(), (response.body.clone(), response.content_type.clone()));
            response
        };
        let ask = |picture: &[u8], pairs: &[(&str, &str)]| {
            let response = fit(picture, pairs);
            let (width, height, _) = super::super::blur::decode(&response.body).unwrap();
            (width, height, response.body.len())
        };
        // Plex Web's grid: covers 240x360, which a 2:3 poster fills exactly.
        let (width, height, size) = ask(&poster, &[("width", "240"), ("height", "360"), ("minSize", "1"), ("upscale", "1")]);
        assert_eq!((width, height), (240, 360));
        assert!(size * 10 < poster.len(), "{size} of {}", poster.len());
        // Fit inside a wide box: the height decides.
        assert_eq!(ask(&poster, &[("width", "1000"), ("height", "300")]).0, 200);
        // Cover a box the picture already covers, without upscale: it does not grow.
        assert_eq!(ask(&poster, &[("width", "2000"), ("height", "2000"), ("minSize", "1")]).0, 1920);
        // A JPEG is 4:4:4 at quality 75's qscale; quality=10 is smaller.
        let grid = fit(&poster, &[("width", "240"), ("height", "360")]);
        assert!(fit(&poster, &[("width", "240"), ("height", "360"), ("quality", "10")]).body.len() < grid.body.len());
        // `format=png` is a PNG labelled so; a PNG asked for nothing stays a PNG, labelled JPEG.
        let png = fit(&poster, &[("width", "100"), ("height", "150"), ("format", "png")]);
        assert_eq!(png.content_type, "image/png");
        assert!(png.body.starts_with(b"\x89PNG"));
        let logo = make("color=red@0.5:s=786x132,format=rgba", &["-c:v", "png"]);
        let kept = fit(&logo, &[("width", "400"), ("height", "200")]);
        assert_eq!((kept.content_type.as_str(), &kept.body[..4]), ("image/jpeg", &b"\x89PNG"[..]));
        // A palette logo with a transparent background keeps it, as Plex's clearLogos come.
        let source = "color=black@0:s=786x132:d=0.04,format=rgba,drawbox=x=100:y=30:w=500:h=60:color=white:t=fill,split[a][b];[a]palettegen=reserve_transparent=1[p];[b][p]paletteuse";
        let palette = make(source, &["-c:v", "png"]);
        assert_eq!(palette[25], 3, "the source is a palette PNG");
        let kept = fit(&palette, &[("width", "400"), ("height", "200")]);
        let mut decoder = png::Decoder::new(std::io::Cursor::new(&kept.body));
        decoder.set_transformations(png::Transformations::normalize_to_color8());
        let mut reader = decoder.read_info().unwrap();
        let mut pixels = vec![0; reader.output_buffer_size().unwrap()];
        let frame = reader.next_frame(&mut pixels).unwrap();
        assert_eq!((frame.color_type, pixels[3]), (png::ColorType::Rgba, 0), "the corner stays transparent");
        // What is not JPEG, PNG or WebP is never handed to FFmpeg.
        let text = b"#EXTM3U\n#EXTINF:1,\nhttp://127.0.0.1:9/x.ts\n".to_vec();
        let query = [("width", "240"), ("height", "360")].iter().map(|(k, v)| (k.to_string(), v.to_string())).collect::<HashMap<_, _>>();
        assert!(PhotoFit::new(&query, &text, ffmpeg.clone(), std::env::temp_dir()).unwrap().fit(&text).is_none());
    }
}
