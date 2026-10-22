//! Persistent movie/TV images. BIF v0 follows Roku's independent specification:
//! https://developer.roku.com/dev/docs/bif-file-creation.md#bif-file-specification
//! No source URL, credentials, title or part id is used as a filesystem component.
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use super::analysis;

pub const ARTIFACT_VERSION: u32 = 1;
const STEP_MS: i64 = 10_000;
const MAX_IMAGES: usize = 100_000;
const MAX_JPEG: u64 = 4 * 1024 * 1024;
const MAGIC: &[u8; 8] = b"\x89BIF\r\n\x1a\n";
static SERIAL: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Default)]
pub struct Generated {
    pub bif: Option<PathBuf>,
    /// Source chapter `index` (normally one-based), not vector position.
    pub chapter_images: Vec<(usize, PathBuf)>,
}

struct Pending(PathBuf);
impl Drop for Pending {
    fn drop(&mut self) { let _ = fs::remove_dir_all(&self.0); }
}

/// `output` is a trusted persistent cache root, never the transcoder scratch root.
/// Each successful call atomically publishes a fresh v1-* directory. Parent must
/// persist the returned paths with the source/Part fingerprint; delete the previous
/// generation only after publishing the replacement. The analysis worker handles GC.
/// Unreferenced `.pending-*` directories can be removed on startup.
pub fn generate(ffmpeg: &Path, input: &analysis::Input, output: &Path, previews: bool, chapters: bool) -> Result<Generated, String> {
    if (!previews && !chapters) || input.video_index.is_none() || input.duration_ms <= 0 {
        return Ok(Generated::default());
    }
    if input.video_index.is_some_and(|index| index < 0) { return Err("invalid video stream index".into()); }
    if input.auth.as_deref().is_some_and(|auth| auth.contains(['\r', '\n'])) { return Err("invalid source authorization header".into()); }
    let count = ((input.duration_ms - 1) / STEP_MS + 1) as u64;
    if previews && count > MAX_IMAGES as u64 { return Err("seek preview index exceeds image limit".into()); }
    if chapters && input.chapters.len() > MAX_IMAGES { return Err("chapter count exceeds image limit".into()); }
    fs::create_dir_all(output).map_err(io_error)?;
    let root = fs::canonicalize(output).map_err(io_error)?;
    let (pending, complete) = loop {
        let nonce = SERIAL.fetch_add(1, Ordering::Relaxed);
        let name = format!("v{ARTIFACT_VERSION}-{}-{nonce}", std::process::id());
        let pending = root.join(format!(".pending-{name}"));
        let complete = root.join(name);
        if complete.exists() { continue; }
        match fs::create_dir(&pending) {
            Ok(()) => break (Pending(pending), complete),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(io_error(error)),
        }
    };
    let deadline = Instant::now() + Duration::from_secs(1800);
    let mut generated = Generated::default();
    if previews {
        let mut frames = Vec::with_capacity(count as usize);
        for index in 0..count as usize {
            let path = pending.0.join(format!("seek-{index}.jpg"));
            frame(ffmpeg, input, index as i64 * STEP_MS, &path, deadline)?;
            frames.push(path);
        }
        let path = pending.0.join("index-sd.bif");
        write_bif(&path, &frames).map_err(io_error)?;
        validate_bif(&path).map_err(io_error)?;
        for frame in frames { fs::remove_file(frame).map_err(io_error)?; }
        generated.bif = Some(complete.join("index-sd.bif"));
    }
    if chapters {
        let mut seen = std::collections::HashSet::new();
        for chapter in &input.chapters {
            let start = chapter["startTimeOffset"].as_i64().ok_or("chapter has no millisecond boundary")?;
            let index = chapter["index"].as_u64().and_then(|index| usize::try_from(index).ok()).ok_or("chapter has no valid index")?;
            if start < 0 || start >= input.duration_ms || !seen.insert(index) { return Err("invalid or duplicate chapter boundary/index".into()); }
            let name = format!("chapter-{index}.jpg");
            frame(ffmpeg, input, start, &pending.0.join(&name), deadline)?;
            generated.chapter_images.push((index, complete.join(name)));
        }
    }
    if generated.bif.is_none() && generated.chapter_images.is_empty() { return Ok(generated); }
    fs::rename(&pending.0, &complete).map_err(io_error)?;
    Ok(generated)
}

fn io_error(error: io::Error) -> String { format!("thumbnail artifact IO: {error}") }

fn frame(ffmpeg: &Path, input: &analysis::Input, at_ms: i64, path: &Path, deadline: Instant) -> Result<(), String> {
    let mut command = Command::new(ffmpeg);
    command.args(["-v", "error", "-nostdin", "-n"]);
    if input.url.starts_with("http://") || input.url.starts_with("https://") {
        command.args(["-rw_timeout", "15000000", "-user_agent", super::upstream::UPSTREAM_UA]);
        if let Some(auth) = &input.auth { command.args(["-headers", &format!("Authorization: {auth}\r\n")]); }
    }
    // Input seeking with FFmpeg's default accurate_seek decodes/discards to the
    // millisecond boundary; never keyframe-only/-noaccurate_seek extraction.
    command.args(["-ss", &format!("{}.{:03}", at_ms / 1000, at_ms % 1000), "-i", &input.url,
        "-map", &format!("0:{}", input.video_index.unwrap()), "-an", "-sn", "-dn",
        "-frames:v", "1", "-vf", "scale=320:-2:out_range=full,setsar=1,format=yuvj420p", "-color_range", "pc", "-q:v", "3", "-threads", "1", "-f", "image2"]);
    command.arg(path).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    if Instant::now() >= deadline { return Err("thumbnail generation deadline exceeded".into()); }
    let mut child = super::universal::spawn_tied(command).map_err(|_| "cannot start thumbnail FFmpeg".to_string())?;
    let expires = deadline.min(Instant::now() + Duration::from_secs(45));
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(Some(status)) => return Err(format!("thumbnail FFmpeg failed (exit {:?})", status.code())),
            Ok(None) if Instant::now() < expires => std::thread::sleep(Duration::from_millis(25)),
            Ok(None) => { let _ = child.kill(); let _ = child.wait(); return Err("thumbnail FFmpeg timed out".into()); }
            Err(_) => { let _ = child.kill(); let _ = child.wait(); return Err("cannot wait for thumbnail FFmpeg".into()); }
        }
    }
    if !valid_jpeg(path) { return Err("thumbnail FFmpeg produced no complete JPEG".into()); }
    File::open(path).and_then(|file| file.sync_all()).map_err(io_error)
}

fn invalid() -> io::Error { io::Error::new(io::ErrorKind::InvalidData, "invalid thumbnail artifact") }
fn jpeg_region(file: &mut File, offset: u64, len: u64) -> io::Result<()> {
    if !(4..=MAX_JPEG).contains(&len) { return Err(invalid()); }
    file.seek(SeekFrom::Start(offset))?;
    let mut reader = std::io::BufReader::new(file.take(len));
    fn byte(reader: &mut impl Read) -> io::Result<u8> {
        let mut value = [0]; reader.read_exact(&mut value)?; Ok(value[0])
    }
    if byte(&mut reader)? != 0xff || byte(&mut reader)? != 0xd8 { return Err(invalid()); }
    let (mut dimensions, mut scan, mut entropy) = (false, false, false);
    loop {
        let mut prefix = byte(&mut reader)?;
        if entropy {
            while prefix != 0xff { prefix = byte(&mut reader)?; }
        } else if prefix != 0xff { return Err(invalid()); }
        let mut marker = byte(&mut reader)?;
        while marker == 0xff { marker = byte(&mut reader)?; }
        if entropy && (marker == 0 || (0xd0..=0xd7).contains(&marker)) { continue; }
        if marker == 0xd9 {
            if !dimensions || !scan || reader.read(&mut [0])? != 0 { return Err(invalid()); }
            return Ok(());
        }
        if marker == 0 || marker == 0xd8 || (0xd0..=0xd7).contains(&marker) { return Err(invalid()); }
        let length = u16::from_be_bytes([byte(&mut reader)?, byte(&mut reader)?]) as u64;
        if length < 2 { return Err(invalid()); }
        let mut remaining = length - 2;
        if matches!(marker, 0xc0 | 0xc1 | 0xc2) {
            if remaining < 6 { return Err(invalid()); }
            let _precision = byte(&mut reader)?;
            let height = u16::from_be_bytes([byte(&mut reader)?, byte(&mut reader)?]);
            let width = u16::from_be_bytes([byte(&mut reader)?, byte(&mut reader)?]);
            if height == 0 || width == 0 { return Err(invalid()); }
            dimensions = true;
            remaining -= 5;
        }
        // Bounded stack buffer, including entropy scanning: no complete-image
        // allocation merely to decide whether a persisted artifact is usable.
        let copied = io::copy(&mut reader.by_ref().take(remaining), &mut io::sink())?;
        if copied != remaining { return Err(invalid()); }
        entropy = marker == 0xda;
        if entropy {
            if !dimensions || length < 6 { return Err(invalid()); }
            scan = true;
        }
    }
}

pub fn valid_jpeg(path: &Path) -> bool {
    (|| -> io::Result<()> {
        if !fs::symlink_metadata(path)?.file_type().is_file() { return Err(invalid()); }
        let mut file = File::open(path)?;
        let len = file.metadata()?.len();
        jpeg_region(&mut file, 0, len)
    })().is_ok()
}

fn write_bif(path: &Path, frames: &[PathBuf]) -> io::Result<()> {
    if frames.is_empty() || frames.len() > MAX_IMAGES { return Err(invalid()); }
    let mut offset = 64u64 + (frames.len() as u64 + 1) * 8;
    let mut file = OpenOptions::new().write(true).create_new(true).open(path)?;
    file.write_all(MAGIC)?;
    file.write_all(&0u32.to_le_bytes())?;
    file.write_all(&(frames.len() as u32).to_le_bytes())?;
    file.write_all(&(STEP_MS as u32).to_le_bytes())?;
    file.write_all(&[0; 44])?;
    for (index, frame) in frames.iter().enumerate() {
        file.write_all(&(index as u32).to_le_bytes())?;
        file.write_all(&u32::try_from(offset).map_err(|_| invalid())?.to_le_bytes())?;
        offset = offset.checked_add(fs::metadata(frame)?.len()).ok_or_else(invalid)?;
    }
    file.write_all(&u32::MAX.to_le_bytes())?;
    file.write_all(&u32::try_from(offset).map_err(|_| invalid())?.to_le_bytes())?;
    for frame in frames { io::copy(&mut File::open(frame)?, &mut file)?; }
    file.sync_all()
}

fn word(file: &mut File) -> io::Result<u32> {
    let mut bytes = [0; 4]; file.read_exact(&mut bytes)?; Ok(u32::from_le_bytes(bytes))
}

/// Complete bounded index validation, without loading JPEG payloads or the BIF.
/// Parent should use this before restoring/advertising persisted indexes.
pub fn validate_bif(path: &Path) -> io::Result<()> { bif_region(path, None).map(|_| ()) }

fn bif_region(path: &Path, at_ms: Option<u64>) -> io::Result<(u64, u64)> {
    if !fs::symlink_metadata(path)?.file_type().is_file() { return Err(invalid()); }
    let mut file = File::open(path)?;
    let len = file.metadata()?.len();
    let mut header = [0; 64]; file.read_exact(&mut header)?;
    let value = |start| u32::from_le_bytes(header[start..start + 4].try_into().unwrap());
    if &header[..8] != MAGIC || value(8) != 0 || header[20..].iter().any(|byte| *byte != 0) { return Err(invalid()); }
    let count = value(12) as usize;
    if count == 0 || count > MAX_IMAGES { return Err(invalid()); }
    let multiplier = match value(16) { 0 => 1000, n => n } as u64;
    let data_start = 64 + (count as u64 + 1) * 8;
    if data_start >= len || len > u32::MAX as u64 { return Err(invalid()); }
    let mut previous: Option<(u32, u64)> = None;
    let mut selected = None;
    for index in 0..=count {
        file.seek(SeekFrom::Start(64 + index as u64 * 8))?;
        let timestamp = word(&mut file)?;
        let offset = word(&mut file)? as u64;
        if offset < data_start || offset > len { return Err(invalid()); }
        if index == count {
            if timestamp != u32::MAX || offset != len { return Err(invalid()); }
        } else if timestamp == u32::MAX { return Err(invalid()); }
        if let Some((before, start)) = previous {
            if offset <= start || (index < count && timestamp <= before) { return Err(invalid()); }
            if at_ms.is_none() { jpeg_region(&mut file, start, offset - start)?; }
            if selected.is_none() || at_ms.is_some_and(|at| before as u64 * multiplier <= at) { selected = Some((start, offset - start)); }
        }
        previous = Some((timestamp, offset));
    }
    let selected = selected.ok_or_else(invalid)?;
    if at_ms.is_some() { jpeg_region(&mut file, selected.0, selected.1)?; }
    Ok(selected)
}

/// Returns the indexed JPEG at or immediately before the requested millisecond.
/// Only the chosen image (at most 4 MiB), never the complete BIF, is allocated.
pub fn seek_jpeg(path: &Path, at_ms: u64) -> io::Result<Vec<u8>> {
    let (offset, len) = bif_region(path, Some(at_ms))?;
    let mut file = File::open(path)?;
    file.seek(SeekFrom::Start(offset))?;
    let mut bytes = vec![0; len as usize];
    file.read_exact(&mut bytes)?;
    Ok(bytes)
}


#[cfg(test)]
#[path = "thumbnails_tests.rs"]
mod tests;
