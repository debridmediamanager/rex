//! Artwork analysis: the `thumbBlurHash`/`artBlurHash` and `UltraBlurColors` a real PMS keeps for
//! every image a row owns (docs/findings.md, "Artwork BlurHash and UltraBlur colours").
//!
//! Measured on plexav (1.43.4, 2026-10-31): PMS works both out once per image, when the row gets
//! it, and stores them on the row; a row it has not analysed yet answers neither. rex works them
//! out the first time a response shows the row, on a thread of its own and never under the
//! runtime lock, so a listing never waits: until the image is read the row answers as plexav's
//! unanalysed one. Each answer is kept by the artwork's versioned path (`/library/metadata/{id}/
//! thumb/{stamp}`, re-minted whenever the artwork changes) in `blur_cache.db` beside the state
//! file, and all of it is read back into memory on start.

use super::diskcache::DiskCache;
use super::{transcoder, Runtime, SharedRuntime};
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet, VecDeque};
use std::path::Path;
use std::sync::{Arc, Condvar, Mutex, PoisonError, Weak};
use std::time::Duration;

/// One image's analysis: its BlurHash and its four corner colours (topLeft, topRight,
/// bottomRight, bottomLeft, the order PMS writes them in), none for an image PMS finds no palette
/// in (near-white throughout).
#[derive(Clone, Debug, PartialEq)]
pub(super) struct Analysis {
    pub(super) hash: String,
    pub(super) colors: Option<[String; 4]>,
}

pub(super) fn colors_object(colors: &[String; 4]) -> Value {
    let mut object = Map::new();
    for (corner, value) in CORNERS.iter().zip(colors) {
        object.insert(corner.to_string(), json!(value));
    }
    Value::Object(object)
}

const CORNERS: [&str; 4] = ["topLeft", "topRight", "bottomRight", "bottomLeft"];

/// What the worker fetches for one artwork path: the row's key and which leaf.
struct Job {
    path: String,
    rating_key: String,
    leaf: &'static str,
}

#[derive(Default)]
struct Lane {
    queue: VecDeque<Job>,
    queued: HashSet<String>,
    started: bool,
}

impl std::fmt::Debug for Blurs {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("Blurs")
    }
}

pub(super) struct Blurs {
    /// By artwork path: the analysis, or `None` for an image that could not be read (kept in
    /// memory only, so a restart tries it again).
    known: Mutex<HashMap<String, Option<Arc<Analysis>>>>,
    lane: Mutex<Lane>,
    wake: Condvar,
    disk: Option<DiskCache>,
    /// `REX_ARTWORK_ANALYSIS=off`: no listing shows either field rex works out (the test servers
    /// `tests/server_command.py` starts, whose answers must not change between two reads).
    enabled: bool,
}

impl Blurs {
    /// Kept beside the state file; with `--no-state` in memory only, as the rest of the state.
    pub(super) fn new(state: Option<&Path>) -> Arc<Self> {
        Arc::new(Self {
            known: Mutex::new(HashMap::new()),
            lane: Mutex::new(Lane::default()),
            wake: Condvar::new(),
            disk: state.map(|state| DiskCache::open(&state.with_file_name("blur_cache.json"))),
            enabled: std::env::var("REX_ARTWORK_ANALYSIS").map_or(true, |value| value != "off"),
        })
    }

    /// The analysis of `path`, or `None` with the image queued for the worker.
    fn lookup(&self, known: &HashMap<String, Option<Arc<Analysis>>>, path: &str, rating_key: &str, leaf: &'static str) -> Option<Arc<Analysis>> {
        match known.get(path) {
            Some(found) => found.clone(),
            None => {
                let mut lane = self.lane.lock().unwrap_or_else(PoisonError::into_inner);
                if lane.queued.insert(path.to_string()) {
                    lane.queue.push_back(Job { path: path.to_string(), rating_key: rating_key.to_string(), leaf });
                    self.wake.notify_one();
                }
                None
            }
        }
    }

    pub(super) fn cached(&self, path: &str) -> Option<Arc<Analysis>> {
        self.known.lock().unwrap_or_else(PoisonError::into_inner).get(path).cloned().flatten()
    }

    fn remember(&self, path: String, analysis: Option<Analysis>) {
        if let (Some(disk), Some(analysis)) = (&self.disk, &analysis) {
            disk.put(&path, &json!({"hash": analysis.hash, "colors": analysis.colors}));
        }
        self.known.lock().unwrap_or_else(PoisonError::into_inner).insert(path, analysis.map(Arc::new));
    }
}

pub(super) fn start(shared: &SharedRuntime) {
    let blurs = Arc::clone(&shared.lock().unwrap_or_else(PoisonError::into_inner).blurs);
    if !blurs.enabled {
        return;
    }
    {
        let mut lane = blurs.lane.lock().unwrap_or_else(PoisonError::into_inner);
        if lane.started {
            return;
        }
        lane.started = true;
    }
    let weak = Arc::downgrade(shared);
    let _ = std::thread::Builder::new().name("artwork-blur".to_string()).spawn(move || worker(weak, blurs));
}

/// Where one job's bytes come from, read off the row under the lock.
enum Target {
    Bytes(Vec<u8>, bool),
    Fetch(String, Option<Vec<(&'static str, String)>>, bool),
}

fn worker(shared: Weak<Mutex<Runtime>>, blurs: Arc<Blurs>) {
    if let Some(disk) = &blurs.disk {
        let restored = disk.all();
        let mut known = blurs.known.lock().unwrap_or_else(PoisonError::into_inner);
        for (path, value) in restored {
            let colors = value["colors"].as_array().map(|colors| colors.iter().filter_map(Value::as_str).map(str::to_string).collect::<Vec<_>>());
            if let Some(hash) = value["hash"].as_str() {
                let colors = colors.and_then(|colors| <[String; 4]>::try_from(colors).ok());
                known.insert(path, Some(Arc::new(Analysis { hash: hash.to_string(), colors })));
            }
        }
    }
    loop {
        let job = {
            let mut lane = blurs.lane.lock().unwrap_or_else(PoisonError::into_inner);
            loop {
                if shared.strong_count() == 0 {
                    return;
                }
                if let Some(job) = lane.queue.pop_front() {
                    break job;
                }
                lane = blurs.wake.wait_timeout(lane, Duration::from_secs(1)).unwrap_or_else(PoisonError::into_inner).0;
            }
        };
        let done = blurs.known.lock().unwrap_or_else(PoisonError::into_inner).contains_key(&job.path);
        let target = if done {
            None
        } else {
            let Some(runtime) = shared.upgrade() else { return };
            let guard = runtime.lock().unwrap_or_else(PoisonError::into_inner);
            resolve(&guard, &job)
        };
        if let Some(target) = target {
            let (bytes, portrait) = match target {
                Some(Target::Bytes(bytes, portrait)) => (Some(bytes), portrait),
                Some(Target::Fetch(url, headers, portrait)) => (transcoder::fetch_remote_image(&url, headers.as_deref()).map(|(bytes, _)| bytes), portrait),
                None => (None, false),
            };
            blurs.remember(job.path.clone(), bytes.and_then(|bytes| analyse(&bytes, portrait)));
        }
        blurs.lane.lock().unwrap_or_else(PoisonError::into_inner).queued.remove(&job.path);
    }
}

/// The image behind a job, as the artwork route serves it: `Some(None)` for a path with no image
/// behind it (an artist's `art` with no background), nothing for a row whose artwork has moved
/// on since the job was queued.
fn resolve(runtime: &Runtime, job: &Job) -> Option<Option<Target>> {
    let row = runtime.catalog.get(&job.rating_key)?;
    let field = if job.leaf == "art" { "art" } else { "thumb" };
    if row.get(field).and_then(Value::as_str) != Some(job.path.as_str()) {
        return None;
    }
    let Some(url) = row.get(if job.leaf == "art" { "_art_url" } else { "_poster_url" }).and_then(Value::as_str).filter(|url| !url.is_empty()) else {
        return Some(None);
    };
    let portrait = job.leaf == "thumb" && matches!(row.get("type").and_then(Value::as_str), Some("movie" | "show"));
    if let Some((bytes, _)) = runtime.uploads.get(url) {
        return Some(Some(Target::Bytes(bytes.clone(), portrait)));
    }
    if let Some((name, path)) = url.strip_prefix("source://").and_then(|rest| rest.split_once('/')) {
        let Some(source) = runtime.sources.iter().find(|source| source.name == name) else { return Some(None) };
        let (url, headers) = source.artwork(path);
        return Some(Some(Target::Fetch(url, Some(headers), portrait)));
    }
    // TMDB's 342-wide poster and 780-wide background, not the originals: a few pixels' worth
    // is analysed, and the originals run to megabytes.
    let url = transcoder::tmdb_at_width(url, Some(if job.leaf == "art" { "780" } else { "342" }));
    Some(Some(Target::Fetch(url, None, portrait)))
}

/// The row types PMS analyses the artwork of (plexav: a track, which borrows its album's
/// cover, carries neither field).
fn analysed_type(kind: &str) -> bool {
    matches!(kind, "movie" | "show" | "season" | "episode" | "artist" | "album")
}

/// The row's own `thumb`/`art` path, or nothing for one it borrows.
fn own_path<'a>(row: &'a Map<String, Value>, field: &str, rating_key: &str) -> Option<&'a str> {
    let path = row.get(field)?.as_str()?;
    let rest = path.strip_prefix("/library/metadata/")?.strip_prefix(rating_key)?;
    rest.strip_prefix('/')?.starts_with(field).then_some(path)
}

impl Runtime {
    /// `thumbBlurHash`, `artBlurHash` and `UltraBlurColors` on every row of a response, as
    /// plexav answers them: the colours always, of the row's own background else its own
    /// poster; each hash only when `includeFields` names it, of its own image; a row whose
    /// image is not analysed yet shows neither (a film, show, season or episode keeps the
    /// fixed palette rex has always sent).
    pub(super) fn blur_response(&self, request: &super::Request, response: &mut super::Response) {
        if !self.blurs.enabled || response.status != 200 || response.content_type != "application/json" {
            return;
        }
        // A body already written is parsed only when it holds rows at all.
        if response.doc.is_none() && !response.body.windows(11).any(|window| window == b"\"ratingKey\"") {
            return;
        }
        let (_, raw_query) = super::split_target(&request.target);
        let included = super::parse_query(&raw_query)
            .into_iter()
            .filter(|(key, _)| key == "includeFields")
            .flat_map(|(_, value)| value.split(',').map(str::to_string).collect::<Vec<_>>())
            .collect::<HashSet<_>>();
        let Some(mut document) = response.take_document() else { return };
        let changed = {
            let known = self.blurs.known.lock().unwrap_or_else(PoisonError::into_inner);
            let mut changed = false;
            if let Some(body) = document.get_mut("MediaContainer").and_then(Value::as_object_mut) {
                let mut lists: Vec<&mut Value> = Vec::new();
                for (key, value) in body.iter_mut() {
                    match key.as_str() {
                        "Metadata" => lists.push(value),
                        "Hub" => {
                            for hub in value.as_array_mut().into_iter().flatten() {
                                if let Some(list) = hub.get_mut("Metadata") {
                                    lists.push(list);
                                }
                            }
                        }
                        _ => {}
                    }
                }
                for row in lists.into_iter().filter_map(Value::as_array_mut).flatten() {
                    if let Some(row) = row.as_object_mut() {
                        changed |= self.blur_row(&known, row, &included);
                    }
                }
            }
            changed
        };
        if changed {
            response.store_document(document);
        } else {
            response.restore_document(document);
        }
    }

    fn blur_row(&self, known: &HashMap<String, Option<Arc<Analysis>>>, row: &mut Map<String, Value>, included: &HashSet<String>) -> bool {
        if !row.get("type").and_then(Value::as_str).is_some_and(analysed_type) {
            return false;
        }
        let Some(rating_key) = row.get("ratingKey").map(super::string_value) else { return false };
        let thumb = own_path(row, "thumb", &rating_key).map(str::to_string);
        let art = own_path(row, "art", &rating_key).map(str::to_string);
        let thumb_analysis = thumb.as_deref().and_then(|path| self.blurs.lookup(known, path, &rating_key, "thumb"));
        let art_analysis = art.as_deref().and_then(|path| self.blurs.lookup(known, path, &rating_key, "art"));
        let mut changed = false;
        // plexav: a row with its own background takes its colours from it, else from its poster.
        // rex's artist rows name an `art` path with no image behind it: that one is no background.
        let pending_art = art.as_deref().is_some_and(|path| !known.contains_key(path));
        let colors = match (&art_analysis, pending_art) {
            (Some(analysis), _) => Some(analysis),
            (None, true) => None,
            (None, false) => thumb_analysis.as_ref(),
        };
        if let Some(colors) = colors.and_then(|analysis| analysis.colors.as_ref()) {
            let value = colors_object(colors);
            match row.get_mut("UltraBlurColors") {
                Some(Value::Array(list)) => *list = vec![value],
                Some(existing) => *existing = value,
                None => {
                    // After `Image`, before the row's tags, where plexav writes it.
                    let at = row.keys().position(|key| key == "Image").map(|index| index + 1).unwrap_or_else(|| {
                        row.values().position(|value| value.is_array() || value.is_object()).unwrap_or(row.len())
                    });
                    row.shift_insert(at, "UltraBlurColors".to_string(), value);
                }
            }
            changed = true;
        }
        for (field, analysis) in [("thumbBlurHash", &thumb_analysis), ("artBlurHash", &art_analysis)] {
            let Some(analysis) = analysis.as_ref().filter(|_| included.contains(field)) else { continue };
            // Before `year` (else `tagline`, else `thumb`): after the counts and ratings.
            let at = ["year", "tagline", "thumb"].iter().find_map(|wanted| row.keys().position(|key| key == wanted)).unwrap_or(row.len());
            let at = if field == "artBlurHash" { row.keys().position(|key| key == "thumbBlurHash").map_or(at, |index| index + 1) } else { at };
            row.shift_insert(at, field.to_string(), json!(analysis.hash));
            changed = true;
        }
        changed
    }
}

/// `/service(s)/ultrablur/colors`'s answer from the image its url resolved to, read outside the
/// lock: its colours, plexav's 500 for one with no palette (a white image), or `fallback` for one
/// rex could not fetch or read.
pub(super) fn colors_response(image: super::Response, fallback: Value) -> super::Response {
    let colors = match (image.status == 200).then(|| decode(&image.body)).flatten() {
        Some((width, height, rgb)) => match ultrablur(width, height, &rgb) {
            Some(colors) => colors_object(&colors),
            None => return super::Response::error(500, "rex: no palette"),
        },
        None => fallback,
    };
    colors_answer(colors)
}

pub(super) fn colors_answer(colors: Value) -> super::Response {
    super::Response::json(200, json!({"MediaContainer": {"size": 1, "UltraBlurColors": [colors]}}))
}

// -- decoding ------------------------------------------------------------------------------

/// An image as 8-bit RGB: width, height and the pixels row by row. Plex's artwork comes as
/// JPEG, PNG or WebP; anything else is unreadable.
pub(super) fn decode(bytes: &[u8]) -> Option<(usize, usize, Vec<u8>)> {
    let (width, height, pixels, channels) = if bytes.starts_with(b"\xff\xd8\xff") {
        let options = zune_jpeg::zune_core::options::DecoderOptions::default()
            .jpeg_set_out_colorspace(zune_jpeg::zune_core::colorspace::ColorSpace::RGB);
        let mut decoder = zune_jpeg::JpegDecoder::new_with_options(std::io::Cursor::new(bytes), options);
        let pixels = decoder.decode().ok()?;
        let (width, height) = decoder.dimensions()?;
        (width, height, pixels, 3)
    } else if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        let mut decoder = png::Decoder::new(std::io::Cursor::new(bytes));
        decoder.set_transformations(png::Transformations::normalize_to_color8());
        let mut reader = decoder.read_info().ok()?;
        let mut pixels = vec![0; reader.output_buffer_size()?];
        let frame = reader.next_frame(&mut pixels).ok()?;
        pixels.truncate(frame.buffer_size());
        let channels = frame.color_type.samples();
        (frame.width as usize, frame.height as usize, pixels, channels)
    } else if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WEBP" {
        let mut decoder = image_webp::WebPDecoder::new(std::io::Cursor::new(bytes)).ok()?;
        let (width, height) = decoder.dimensions();
        let mut pixels = vec![0; decoder.output_buffer_size()?];
        decoder.read_image(&mut pixels).ok()?;
        (width as usize, height as usize, pixels, if decoder.has_alpha() { 4 } else { 3 })
    } else {
        return None;
    };
    if width == 0 || height == 0 || pixels.len() < width * height * channels {
        return None;
    }
    let rgb = match channels {
        3 => pixels,
        // Gray, gray+alpha, RGBA: the colour channels, alpha dropped as PMS's decode drops it.
        1 | 2 | 4 => pixels
            .chunks_exact(channels)
            .flat_map(|pixel| if channels < 3 { [pixel[0]; 3] } else { [pixel[0], pixel[1], pixel[2]] })
            .collect(),
        _ => return None,
    };
    Some((width, height, rgb))
}

/// The size PMS's photo transcoder hashes an image at: inside 100x100, its aspect kept, never
/// larger than it is (f32, rounded half up, as the binary works it out).
fn hash_size(width: usize, height: usize) -> (usize, usize) {
    let (w, h) = (width as f32, height as f32);
    let scale = if w / h <= 1.0 { h / 100.0 } else { w / 100.0 };
    let (to_width, to_height) = (((w / scale) as f64 + 0.5) as usize, ((h / scale) as f64 + 0.5) as usize);
    if to_width > width || to_height > height || to_width == 0 || to_height == 0 {
        (width, height)
    } else {
        (to_width, to_height)
    }
}

/// FreeImage's `FreeImage_Rescale` with `FILTER_BICUBIC` (Mitchell-Netravali, B = C = 1/3), the
/// filter PMS's transcoder shrinks with: one pass per axis, the one that shrinks more first,
/// rounded to bytes in between.
pub(super) fn rescale(width: usize, height: usize, rgb: &[u8], to_width: usize, to_height: usize) -> Vec<u8> {
    let filter = |x: f64| {
        let (b, c) = (1.0 / 3.0, 1.0 / 3.0);
        let x = x.abs();
        if x < 1.0 {
            (6.0 - 2.0 * b) / 6.0 + x * x * ((-18.0 + 12.0 * b + 6.0 * c) / 6.0 + x * (12.0 - 9.0 * b - 6.0 * c) / 6.0)
        } else if x < 2.0 {
            (8.0 * b + 24.0 * c) / 6.0 + x * ((-12.0 * b - 48.0 * c) / 6.0 + x * ((6.0 * b + 30.0 * c) / 6.0 + x * (-b - 6.0 * c) / 6.0))
        } else {
            0.0
        }
    };
    // `CWeightsTable`: each output pixel's first source pixel and weights.
    let table = |to: usize, from: usize| -> Vec<(usize, Vec<f64>)> {
        let scale = to as f64 / from as f64;
        let (width, filter_scale) = if scale < 1.0 { (2.0 / scale, scale) } else { (2.0, 1.0) };
        (0..to)
            .map(|index| {
                let center = index as f64 / scale + 0.5 / scale;
                let left = ((center - width + 0.5) as i64).max(0) as usize;
                let right = ((center + width + 0.5) as i64).min(from as i64) as usize;
                let mut weights: Vec<f64> = (left..right).map(|source| filter_scale * filter(filter_scale * (source as f64 + 0.5 - center))).collect();
                let total: f64 = weights.iter().sum();
                if total > 0.0 && total != 1.0 {
                    weights.iter_mut().for_each(|weight| *weight /= total);
                }
                (left, weights)
            })
            .collect()
    };
    let byte = |value: f64| (value + 0.5).clamp(0.0, 255.0) as u8;
    let across = |rgb: &[u8], width: usize, height: usize, to: usize| -> Vec<u8> {
        let taps = table(to, width);
        let mut out = vec![0; to * height * 3];
        for y in 0..height {
            for (x, (left, weights)) in taps.iter().enumerate() {
                for channel in 0..3 {
                    let sum: f64 = weights.iter().enumerate().map(|(at, weight)| weight * rgb[(y * width + left + at) * 3 + channel] as f64).sum();
                    out[(y * to + x) * 3 + channel] = byte(sum);
                }
            }
        }
        out
    };
    let down = |rgb: &[u8], width: usize, height: usize, to: usize| -> Vec<u8> {
        let taps = table(to, height);
        let mut out = vec![0; width * to * 3];
        for (y, (top, weights)) in taps.iter().enumerate() {
            for x in 0..width * 3 {
                let sum: f64 = weights.iter().enumerate().map(|(at, weight)| weight * rgb[(top + at) * width * 3 + x] as f64).sum();
                out[y * width * 3 + x] = byte(sum);
            }
        }
        out
    };
    if to_width * height <= to_height * width {
        down(&across(rgb, width, height, to_width), to_width, height, to_height)
    } else {
        across(&down(rgb, width, height, to_height), width, to_height, to_width)
    }
}

// -- BlurHash ------------------------------------------------------------------------------

const BASE83: &[u8] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz#$%*+,-.:;=?@[]^_{|}~";

fn base83(value: u32, digits: u32, out: &mut String) {
    for digit in (0..digits).rev() {
        out.push(BASE83[(value / 83u32.pow(digit) % 83) as usize] as char);
    }
}

fn to_linear(value: f32) -> f32 {
    let value = value / 255.0;
    if value <= 0.04045 {
        value / 12.92
    } else {
        ((value + 0.055) / 1.055).powf(2.4)
    }
}

fn to_srgb(value: f32) -> u32 {
    let value = value.clamp(0.0, 1.0);
    if value <= 0.003_130_8 {
        (value * 12.92 * 255.0 + 0.5) as u32
    } else {
        ((1.055 * value.powf(1.0 / 2.4) - 0.055) * 255.0 + 0.5) as u32
    }
}

/// Wolt's BlurHash of an RGB image, `x` by `y` components, as its reference C encoder
/// (`blurHashForPixels`, which PMS calls) writes it.
pub(super) fn blurhash(width: usize, height: usize, pixels: &[u8], x: usize, y: usize) -> String {
    let linear = pixels.iter().map(|value| to_linear(*value as f32)).collect::<Vec<_>>();
    let mut factors = Vec::with_capacity(x * y);
    for j in 0..y {
        let rows = (0..height).map(|py| (std::f32::consts::PI * j as f32 * py as f32 / height as f32).cos()).collect::<Vec<_>>();
        for i in 0..x {
            let columns = (0..width).map(|px| (std::f32::consts::PI * i as f32 * px as f32 / width as f32).cos()).collect::<Vec<_>>();
            let mut sum = [0f32; 3];
            for (py, row_basis) in rows.iter().enumerate() {
                for (px, column_basis) in columns.iter().enumerate() {
                    let basis = row_basis * column_basis;
                    let at = (py * width + px) * 3;
                    for channel in 0..3 {
                        sum[channel] += basis * linear[at + channel];
                    }
                }
            }
            let scale = if i == 0 && j == 0 { 1.0 } else { 2.0 } / (width * height) as f32;
            factors.push(sum.map(|value| value * scale));
        }
    }
    let mut hash = String::with_capacity(4 + 2 * x * y);
    base83(((x - 1) + (y - 1) * 9) as u32, 1, &mut hash);
    let ac = &factors[1..];
    let maximum = if ac.is_empty() {
        base83(0, 1, &mut hash);
        1.0
    } else {
        let actual = ac.iter().flatten().fold(0f32, |most, value| most.max(value.abs()));
        let quantised = ((actual * 166.0 - 0.5).floor() as i32).clamp(0, 82);
        base83(quantised as u32, 1, &mut hash);
        (quantised + 1) as f32 / 166.0
    };
    let dc = factors[0];
    base83((to_srgb(dc[0]) << 16) + (to_srgb(dc[1]) << 8) + to_srgb(dc[2]), 4, &mut hash);
    for factor in ac {
        let quant = |value: f32| {
            let scaled = value / maximum;
            ((scaled.signum() * scaled.abs().sqrt() * 9.0 + 9.5).floor() as i32).clamp(0, 18) as u32
        };
        base83(quant(factor[0]) * 19 * 19 + quant(factor[1]) * 19 + quant(factor[2]), 2, &mut hash);
    }
    hash
}

// -- the analysis ----------------------------------------------------------------------------

/// An image's analysis, or nothing for bytes that are not a picture rex reads. `portrait`: a
/// film's or show's poster, which PMS hashes with 3x4 components where every other image (a
/// season's poster too, and every background) gets 4x3.
pub(super) fn analyse(bytes: &[u8], portrait: bool) -> Option<Analysis> {
    let (width, height, rgb) = decode(bytes)?;
    // PMS's photo transcoder also has libjpeg decode at a reduced DCT size first (a 256 hint),
    // which no pure-Rust decoder offers: the full decode shrunk the same way lands within a
    // unit or two of it on average (docs/findings.md).
    let (to_width, to_height) = hash_size(width, height);
    let small = rescale(width, height, &rgb, to_width, to_height);
    let (x, y) = if portrait { (3, 4) } else { (4, 3) };
    Some(Analysis { hash: blurhash(to_width, to_height, &small, x, y), colors: ultrablur(width, height, &rgb) })
}

pub(super) use super::ultrablur::corners as ultrablur;

#[cfg(test)]
mod tests {
    use super::*;

    /// plexav's answers for rex's own synthetic covers (`probes/record_blur.py`).
    const FIXTURE: &str = include_str!("../../../tests/fixtures/artwork/plexav_blur.json");

    fn cover(name: &str) -> &'static [u8] {
        match name {
            "artist.jpg" => include_bytes!("../../../tests/fixtures/artwork/artist.jpg"),
            "diagonal.jpg" => include_bytes!("../../../tests/fixtures/artwork/diagonal.jpg"),
            "fresh.jpg" => include_bytes!("../../../tests/fixtures/artwork/fresh.jpg"),
            "gradient.png" => include_bytes!("../../../tests/fixtures/artwork/gradient.png"),
            "quadrants.jpg" => include_bytes!("../../../tests/fixtures/artwork/quadrants.jpg"),
            "solid.png" => include_bytes!("../../../tests/fixtures/artwork/solid.png"),
            "stripes.jpg" => include_bytes!("../../../tests/fixtures/artwork/stripes.jpg"),
            "wide.jpg" => include_bytes!("../../../tests/fixtures/artwork/wide.jpg"),
            _ => panic!("{name}"),
        }
    }

    fn corners_of(value: &Value) -> Option<[String; 4]> {
        let colors = value.as_object()?;
        Some(CORNERS.map(|corner| colors[corner].as_str().unwrap_or_default().to_string()))
    }

    fn channel_distance(ours: &[String; 4], theirs: &[String; 4]) -> u8 {
        let byte = |hex: &str, at: usize| u8::from_str_radix(&hex[at..at + 2], 16).unwrap();
        (0..4).flat_map(|corner| [0, 2, 4].map(|at| byte(&ours[corner], at).abs_diff(byte(&theirs[corner], at)))).max().unwrap()
    }

    #[test]
    fn solid_images_answer_plexavs_corners_exactly() {
        let fixture: Value = serde_json::from_str(FIXTURE).unwrap();
        let solids = fixture["solids"].as_object().unwrap();
        assert!(solids.len() >= 20);
        for (name, entry) in solids {
            let hex = &name[2..8];
            let rgb: Vec<u8> = (0..3).map(|at| u8::from_str_radix(&hex[at * 2..at * 2 + 2], 16).unwrap()).collect();
            let pixels: Vec<u8> = rgb.iter().copied().cycle().take(300 * 300 * 3).collect();
            // A white image is plexav's 500: no palette once near-white is left out.
            let theirs = (entry["status"] == 200).then(|| corners_of(&entry["colors"][0])).flatten();
            assert_eq!(ultrablur(300, 300, &pixels), theirs, "{name}");
        }
    }

    #[test]
    fn tiny_images_answer_as_plexav() {
        // A quadrant of an image under 2x2 is empty: no palette, plexav's 500.
        let fixture: Value = serde_json::from_str(FIXTURE).unwrap();
        let images: [(&str, usize, usize, Vec<u8>); 4] = [
            ("p1x1_black.png", 1, 1, vec![0, 0, 0]),
            ("p1x1_blue.png", 1, 1, vec![16, 32, 48]),
            ("p2x2.png", 2, 2, vec![200, 30, 30, 30, 200, 30, 30, 30, 200, 220, 200, 40]),
            ("p3x1.png", 3, 1, vec![200, 30, 30, 30, 200, 30, 30, 30, 200]),
        ];
        for (name, width, height, pixels) in images {
            let entry = &fixture["tiny"][name];
            let theirs = (entry["status"] == 200).then(|| corners_of(&entry["colors"][0])).flatten();
            assert_eq!(ultrablur(width, height, &pixels), theirs, "{name}");
        }
    }

    #[test]
    fn covers_answer_plexavs_hash_and_corners() {
        let fixture: Value = serde_json::from_str(FIXTURE).unwrap();
        let mut seen = 0;
        for entry in fixture["rows"].as_object().unwrap().values() {
            let Some(name) = entry["cover"].as_str() else { continue };
            let analysis = analyse(cover(name), false).unwrap();
            let theirs = corners_of(&entry["UltraBlurColors"]).unwrap();
            let colors = analysis.colors.clone().unwrap();
            if name.ends_with(".png") {
                // Lossless: the same pixels PMS read, so the same answers.
                assert_eq!(analysis.hash, entry["thumbBlurHash"].as_str().unwrap(), "{name}");
                assert_eq!(colors, theirs, "{name}");
            } else {
                // A JPEG decodes a unit or two apart from libjpeg-turbo's at hard edges.
                assert!(channel_distance(&colors, &theirs) <= 6, "{name}: {colors:?} {theirs:?}");
            }
            seen += 1;
        }
        assert!(seen >= 8, "{seen}");
    }

    /// `REX_BH_ORACLE=<dir>:<tsv>`, lines `file<TAB>plexav's hash`: rex's hash of each file
    /// (3x4 where plexav's starts with `T`). Plex's own artwork stays out of the tree, so this
    /// reads a local copy; not run by default.
    #[test]
    #[ignore]
    fn hashes_against_recorded_oracle() {
        let spec = std::env::var("REX_BH_ORACLE").unwrap();
        let (dir, table) = spec.split_once(':').unwrap();
        for line in std::fs::read_to_string(table).unwrap().lines() {
            let (name, theirs) = line.split_once('\t').unwrap();
            let ours = analyse(&std::fs::read(format!("{dir}/{name}")).unwrap(), theirs.starts_with('T')).unwrap().hash;
            println!("HASH {} {name} {ours} {theirs}", ours == theirs);
        }
    }

    #[test]
    fn webp_artwork_is_read() {
        let pixels: Vec<u8> = (0..16 * 8).flat_map(|at| if at % 16 < 8 { [200, 30, 30] } else { [30, 30, 200] }).collect();
        let mut encoded = Vec::new();
        image_webp::WebPEncoder::new(&mut encoded).encode(&pixels, 16, 8, image_webp::ColorType::Rgb8).unwrap();
        let (width, height, rgb) = decode(&encoded).unwrap();
        assert_eq!((width, height, rgb), (16, 8, pixels));
        assert!(analyse(&encoded, false).is_some_and(|analysis| analysis.hash.starts_with('L')));
        // A film's or show's poster is hashed 3x4 (plexav's Firefly poster), anything else 4x3.
        assert!(analyse(&encoded, true).is_some_and(|analysis| analysis.hash.starts_with('T')));
        assert!(decode(b"GIF89a").is_none());
    }
}
