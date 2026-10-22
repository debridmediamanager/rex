//! Conservative content-derived markers. No filenames or inferred placement are used.
//!
//! Intro search compares the first ten minutes of source-grouped episodes at 2 Hz.
//! It requires 12 seconds of matching, changing picture or changing non-silent audio;
//! static logos, silence and steady tones are deliberately not evidence. Credits search
//! measures sparse bright text moving upward over a dark background at 4 Hz in the
//! final ten minutes. It does not identify static cards, montage credits or OCR text.
//! Boundaries have sampling precision (500 ms intro, 250 ms credits), not frame precision.
use super::analysis;
use serde_json::{json, Value};
use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::{Arc, atomic::{AtomicBool, Ordering}};
use std::time::{Duration, Instant};

#[derive(Debug)]
pub struct ResultRow {
    pub key: String,
    pub part_id: String,
    pub markers: Vec<Value>,
}

const SEARCH_SECONDS: i64 = 600;
const INTRO_STEP: i64 = 500;
const MIN_INTRO: usize = 24;

struct Fingerprints {
    picture: Vec<Picture>,
    audio: Vec<Audio>,
}
struct Picture { pixels: [u8; 32 * 18], hash: u64, useful: bool }
struct Audio { bands: [f32; 16], useful: bool }

/// Runs outside the runtime lock. An unavailable decoder/filter or failed source read
/// is an error, never an empty successful analysis. Source credentials are not logged.
pub fn analyze(ffmpeg: &Path, inputs: &[analysis::Input]) -> Result<Vec<ResultRow>, String> {
    let mut rows = Vec::with_capacity(inputs.len());
    let mut fingerprints = Vec::with_capacity(inputs.len());
    for input in inputs {
        if input.duration_ms <= 0 { return Err("marker analysis requires a positive source duration".into()); }
        let mut markers = chapter_markers(input);
        if let Some(index) = input.video_index {
            let start = (input.duration_ms - SEARCH_SECONDS * 1000).max(0);
            let bytes = decode(ffmpeg, input, index, start, input.duration_ms - start, Some((96, 54, 4)))?;
            if let Some((a, b)) = rolling_credits(&bytes) {
                add_marker(&mut markers, "credits", start + a, start + b, input.duration_ms);
            }
        }
        let grouped = input.group.is_some() && inputs.iter().any(|other| other.key != input.key && other.group == input.group);
        let mut fp = Fingerprints { picture: Vec::new(), audio: Vec::new() };
        if grouped {
            let duration = input.duration_ms.min(SEARCH_SECONDS * 1000);
            if let Some(index) = input.video_index {
                let bytes = decode(ffmpeg, input, index, 0, duration, Some((32, 18, 2)))?;
                fp.picture = bytes.chunks_exact(32 * 18).map(picture).collect();
            }
            if let Some(index) = input.audio_index {
                let bytes = decode(ffmpeg, input, index, 0, duration, None)?;
                fp.audio = bytes.chunks_exact(2000).map(audio).collect();
            }
        }
        fingerprints.push(fp);
        rows.push(ResultRow { key: input.key.clone(), part_id: input.part_id.clone(), markers });
    }
    let mut best: Vec<Option<(usize, usize)>> = vec![None; inputs.len()];
    for i in 0..inputs.len() {
        for j in i + 1..inputs.len() {
            if inputs[i].group.is_none() || inputs[i].group != inputs[j].group || inputs[i].key == inputs[j].key { continue; }
            if let Some((a, b, len)) = repeated(&fingerprints[i], &fingerprints[j]) {
                for (index, start) in [(i, a), (j, b)] {
                    if best[index].map_or(true, |(_, old)| len > old) { best[index] = Some((start, len)); }
                }
            }
        }
    }
    for (i, found) in best.into_iter().enumerate() {
        if let Some((start, len)) = found {
            add_marker(&mut rows[i].markers, "intro", start as i64 * INTRO_STEP, (start + len) as i64 * INTRO_STEP, inputs[i].duration_ms);
        }
        rows[i].markers.sort_by_key(|m| m["startTimeOffset"].as_i64().unwrap_or(0));
    }
    Ok(rows)
}

fn add_marker(markers: &mut Vec<Value>, kind: &str, start: i64, end: i64, duration: i64) {
    let start = start.max(0);
    let end = end.min(duration);
    if start < end && !markers.iter().any(|m| m["type"] == kind) {
        markers.push(json!({"type": kind, "startTimeOffset": start, "endTimeOffset": end}));
    }
}

fn chapter_markers(input: &analysis::Input) -> Vec<Value> {
    let mut result = Vec::new();
    for chapter in &input.chapters {
        let title = chapter["title"].as_str().unwrap_or("").trim().to_ascii_lowercase();
        let kind = match title.as_str() {
            "intro" | "opening" | "opening credits" => "intro",
            "credits" | "end credits" | "closing credits" => "credits",
            _ => continue,
        };
        if let (Some(a), Some(b)) = (chapter["startTimeOffset"].as_i64(), chapter["endTimeOffset"].as_i64()) {
            // Reject malformed authored cues rather than turning them into new boundaries.
            if a >= 0 && b <= input.duration_ms { add_marker(&mut result, kind, a, b, input.duration_ms); }
        }
    }
    result
}

fn decode(ffmpeg: &Path, input: &analysis::Input, index: i64, start: i64, duration: i64, video: Option<(usize, usize, usize)>) -> Result<Vec<u8>, String> {
    if index < 0 { return Err("marker analysis received an invalid stream index".into()); }
    let mut command = Command::new(ffmpeg);
    command.args(["-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "1", "-filter_threads", "1", "-max_alloc", "67108864", "-max_pixels", "16777216"]);
    if let Some(auth) = &input.auth {
        if auth.contains(['\r', '\n']) { return Err("marker source authorization contains a newline".into()); }
        command.args(["-headers", &format!("Authorization: {auth}\r\n")]);
    }
    if input.url.starts_with("http://") || input.url.starts_with("https://") { command.args(["-rw_timeout", "15000000"]); }
    command.args(["-ss", &format!("{:.3}", start as f64 / 1000.0), "-i", &input.url, "-t", &format!("{:.3}", duration as f64 / 1000.0), "-map", &format!("0:{index}")]);
    let cap = if let Some((w, h, fps)) = video {
        command.args(["-an", "-sn", "-vf", &format!("fps={fps},scale={w}:{h}:flags=area,format=gray"), "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"]);
        w * h * fps * SEARCH_SECONDS as usize + w * h
    } else {
        command.args(["-vn", "-sn", "-ac", "1", "-ar", "2000", "-f", "s16le", "pipe:1"]);
        4000 * SEARCH_SECONDS as usize + 4000
    };
    let mut child = command.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn()
        .map_err(|_| "marker analysis cannot start the configured FFmpeg engine".to_string())?;
    let mut stdout = child.stdout.take().ok_or("marker FFmpeg stdout unavailable")?;
    let exceeded = Arc::new(AtomicBool::new(false));
    let signal = exceeded.clone();
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let mut buf = [0; 16384];
        loop {
            let n = stdout.read(&mut buf)?;
            if n == 0 { break; }
            if bytes.len() + n > cap { signal.store(true, Ordering::Relaxed); break; }
            bytes.extend_from_slice(&buf[..n]);
        }
        Ok::<_, std::io::Error>(bytes)
    });
    let deadline = Instant::now() + Duration::from_secs(120);
    let status = loop {
        if exceeded.load(Ordering::Relaxed) || Instant::now() >= deadline {
            let _ = child.kill(); let _ = child.wait(); let _ = reader.join();
            return Err("marker FFmpeg exceeded its 120-second/output resource bound".into());
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(_) => { let _ = child.kill(); let _ = child.wait(); let _ = reader.join(); return Err("marker FFmpeg process wait failed".into()); }
        }
    };
    let bytes = reader.join().map_err(|_| "marker output reader failed")?.map_err(|_| "marker output read failed")?;
    if !status.success() || exceeded.load(Ordering::Relaxed) || bytes.is_empty() {
        return Err("marker FFmpeg decoding failed: check source access, selected streams and fps/scale/format filters".into());
    }
    Ok(bytes)
}

fn picture(pixels: &[u8]) -> Picture {
    let mut hash = 0;
    for y in 0..8 { for x in 0..8 {
        if pixels[(y * 2 + 1) * 32 + x * 4] > pixels[(y * 2 + 1) * 32 + x * 4 + 3] { hash |= 1 << (y * 8 + x); }
    }}
    let mean = pixels.iter().map(|&p| p as f32).sum::<f32>() / pixels.len() as f32;
    let variance = pixels.iter().map(|&p| (p as f32 - mean).powi(2)).sum::<f32>() / pixels.len() as f32;
    let mut image = [0; 32 * 18];
    image.copy_from_slice(pixels);
    Picture { pixels: image, hash, useful: variance > 100.0 }
}

fn audio(bytes: &[u8]) -> Audio {
    let mut samples = [0.0f32; 1000];
    for (sample, pair) in samples.iter_mut().zip(bytes.chunks_exact(2)) {
        *sample = i16::from_le_bytes([pair[0], pair[1]]) as f32 / 32768.0;
    }
    let rms = (samples.iter().map(|s| s * s).sum::<f32>() / samples.len() as f32).sqrt();
    let mut bands = [0.0f32; 16];
    for (i, band) in bands.iter_mut().enumerate() {
        let coefficient = 2.0 * (std::f32::consts::TAU * (80.0 + i as f32 * 55.0) / 2000.0).cos();
        let (mut a, mut b) = (0.0, 0.0);
        for &sample in &samples { let v = sample + coefficient * a - b; b = a; a = v; }
        *band = (a * a + b * b - coefficient * a * b).max(0.0).sqrt();
    }
    let norm = bands.iter().map(|v| v * v).sum::<f32>().sqrt().max(1e-8);
    for band in &mut bands { *band /= norm; }
    Audio { bands, useful: rms > 0.008 }
}

fn picture_match(a: &Picture, b: &Picture) -> bool {
    a.useful && b.useful && (a.hash ^ b.hash).count_ones() <= 6 &&
        a.pixels.iter().zip(&b.pixels).map(|(&x, &y)| x.abs_diff(y) as usize).sum::<usize>() < a.pixels.len() * 10
}
fn audio_match(a: &Audio, b: &Audio) -> bool {
    a.useful && b.useful && a.bands.iter().zip(b.bands).map(|(x, y)| (x - y).powi(2)).sum::<f32>() < 0.035
}

fn repeated(a: &Fingerprints, b: &Fingerprints) -> Option<(usize, usize, usize)> {
    let mut best = None;
    for visual in [true, false] {
        let (n, m) = if visual { (a.picture.len(), b.picture.len()) } else { (a.audio.len(), b.audio.len()) };
        for diagonal in -(m as isize)..n as isize {
            let (mut i, mut j) = (diagonal.max(0) as usize, (-diagonal).max(0) as usize);
            let mut run = 0;
            while i < n && j < m {
                let matches = if visual { picture_match(&a.picture[i], &b.picture[j]) } else { audio_match(&a.audio[i], &b.audio[j]) };
                if matches { run += 1; } else { run = 0; }
                if run >= MIN_INTRO && run <= 360 && best.map_or(true, |(_, _, old)| run > old) {
                    let start = i + 1 - run;
                    // Require temporal change, not simply repeated scenery, black or a tone.
                    let changes = (start + 1..=i).filter(|&k| if visual {
                        a.picture[k - 1].pixels.iter().zip(&a.picture[k].pixels)
                            .map(|(&x, &y)| x.abs_diff(y) as usize).sum::<usize>() > 2 * 32 * 18
                    } else { !audio_match(&a.audio[k - 1], &a.audio[k]) }).count();
                    if changes >= 8 && changes * 5 >= run { best = Some((start, j + 1 - run, run)); }
                }
                i += 1; j += 1;
            }
        }
    }
    best
}

fn rolling_credits(bytes: &[u8]) -> Option<(i64, i64)> {
    const W: usize = 96;
    const H: usize = 54;
    let frames: Vec<&[u8]> = bytes.chunks_exact(W * H).collect();
    let mut flags = vec![false; frames.len()];
    for t in 1..frames.len() {
        let (previous, current) = (frames[t - 1], frames[t]);
        let bright = current.iter().filter(|&&p| p > 150).count();
        let dark = current.iter().filter(|&&p| p < 55).count();
        let text_rows = current.chunks_exact(W).filter(|row| {
            let count = row.iter().filter(|&&p| p > 150).count();
            let edges = row.windows(2).filter(|pair| (pair[0] > 150) != (pair[1] > 150)).count();
            count >= 4 && count < W / 2 && edges >= 6
        }).count();
        if bright < 60 || bright > W * H / 4 || dark < W * H * 3 / 4 || text_rows < 5 { continue; }
        let overlap = |shift: usize| {
            let mut hits = 0;
            let mut total = 0;
            for y in 0..H - 8 { for x in 0..W {
                if current[y * W + x] > 150 {
                    total += 1;
                    if previous[(y + shift) * W + x] > 150 { hits += 1; }
                }
            }}
            if total < 40 { 0.0 } else { hits as f32 / total as f32 }
        };
        let stationary = overlap(0);
        let moving = (1..=8).map(overlap).fold(0.0f32, f32::max);
        flags[t] = moving > 0.78 && moving > stationary + 0.12;
    }
    let (mut start, mut last, mut positives) = (0, 0, 0);
    let mut best = None;
    for (i, &flag) in flags.iter().enumerate() {
        if flag {
            if positives == 0 { start = i; }
            last = i; positives += 1;
            let span = last - start + 1;
            if positives >= 32 && positives * 4 >= span * 3 {
                best = Some(((start.saturating_sub(1) * 250) as i64, ((last + 1) * 250) as i64));
            }
        } else if i > last + 8 { positives = 0; }
    }
    best
}

#[cfg(test)]
#[path = "markers_tests.rs"]
mod tests;
