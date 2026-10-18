//! A Matroska release's text subtitle track, read without reading the release.
//!
//! Burning a text subtitle takes FFmpeg's `subtitles` filter, which reads its whole file
//! before the first frame: pointed at the release itself that is a full download (30-67 GB
//! for the remuxes on the production server). mkvmerge indexes every subtitle block in the
//! Cues with its exact place in its Cluster (`CueRelativePosition`, and `CueDuration`):
//! measured on two real releases, a WEB-DL's one SRT (1,468 cues) and a remux's two SRTs and
//! seven PGS tracks, every block of every subtitle track. So the track is read block by block
//! with Range requests, a few hundred bytes each: 1,468 of them took 19 s through zurg on
//! AllDebrid, at 16 or 48 at once alike (2026-10-19).

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

const SEGMENT: u64 = 0x1853_8067;
const SEEK_HEAD: u64 = 0x114D_9B74;
const SEEK: u64 = 0x4DBB;
const SEEK_ID: u64 = 0x53AB;
const SEEK_POSITION: u64 = 0x53AC;
const INFO: u64 = 0x1549_A966;
const TIMESTAMP_SCALE: u64 = 0x2A_D7B1;
const TRACKS: u64 = 0x1654_AE6B;
const TRACK_ENTRY: u64 = 0xAE;
const TRACK_NUMBER: u64 = 0xD7;
const TRACK_TYPE: u64 = 0x83;
const CODEC_ID: u64 = 0x86;
const CODEC_PRIVATE: u64 = 0x63A2;
const CONTENT_ENCODINGS: u64 = 0x6D80;
const CONTENT_ENCODING: u64 = 0x6240;
const CONTENT_ENCODING_TYPE: u64 = 0x5033;
const CONTENT_COMPRESSION: u64 = 0x5034;
const CONTENT_COMP_ALGO: u64 = 0x4254;
const CONTENT_COMP_SETTINGS: u64 = 0x4255;
const CUES: u64 = 0x1C53_BB6B;
const CUE_POINT: u64 = 0xBB;
const CUE_TIME: u64 = 0xB3;
const CUE_TRACK_POSITIONS: u64 = 0xB7;
const CUE_TRACK: u64 = 0xF7;
const CUE_CLUSTER_POSITION: u64 = 0xF1;
const CUE_RELATIVE_POSITION: u64 = 0xF0;
const CUE_DURATION: u64 = 0xB2;
const BLOCK_GROUP: u64 = 0xA0;
const BLOCK: u64 = 0xA1;
const BLOCK_DURATION: u64 = 0x9B;
const SIMPLE_BLOCK: u64 = 0xA3;

/// What a block of the track is read with: `bytes(offset, length)` of the release.
pub(crate) type Read<'a> = &'a (dyn Fn(u64, usize) -> Result<Vec<u8>, String> + Sync);

/// The subtitle track and where each of its blocks is.
#[derive(Debug)]
pub(crate) struct Index {
    number: u64,
    codec: String,
    /// An ASS/SSA track's script header: its styles and the `[Events]` format.
    private: Vec<u8>,
    compression: Compression,
    /// Nanoseconds per tick (`TimestampScale`).
    scale: u64,
    pub cues: Vec<Cue>,
}

#[derive(Debug, PartialEq)]
enum Compression {
    None,
    Zlib,
    /// Header stripping: these bytes were removed from the front of every block.
    Strip(Vec<u8>),
}

#[derive(Clone, Debug)]
pub(crate) struct Cue {
    /// Milliseconds on the title's clock.
    pub start: i64,
    duration: Option<i64>,
    /// The Cluster's absolute offset and the block's offset in its data.
    cluster: u64,
    relative: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Event {
    pub start: i64,
    pub duration: Option<i64>,
    pub text: String,
}

/// An EBML variable-length integer at `at`: its value (with its length marker for an ID)
/// and where it ends. An all-ones size is "unknown", read as `u64::MAX`.
fn vint(bytes: &[u8], at: usize, id: bool) -> Option<(u64, usize)> {
    let first = *bytes.get(at)?;
    if first == 0 {
        return None;
    }
    let length = first.leading_zeros() as usize + 1;
    let tail = bytes.get(at + 1..at + length)?;
    let marker = 0x80u64 >> (length - 1);
    let mut value = if id { first as u64 } else { first as u64 & (marker - 1) };
    let mut ones = !id && value == marker - 1;
    for byte in tail {
        value = (value << 8) | *byte as u64;
        ones &= *byte == 0xFF;
    }
    Some((if ones { u64::MAX } else { value }, at + length))
}

/// The elements between `start` and `end`: id, data start, data end (which may lie past
/// the bytes held, for an element only partly read).
fn children(bytes: &[u8], start: usize, end: usize) -> Vec<(u64, usize, usize)> {
    let mut out = Vec::new();
    let mut at = start;
    let end = end.min(bytes.len());
    while at < end {
        let Some((id, data)) = vint(bytes, at, true) else { break };
        let Some((size, data)) = vint(bytes, data, false) else { break };
        let stop = if size == u64::MAX { end } else { data.saturating_add(size as usize) };
        out.push((id, data, stop));
        at = stop;
    }
    out
}

fn uint(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0, |value, byte| (value << 8) | *byte as u64)
}

fn child<'a>(bytes: &'a [u8], start: usize, end: usize, id: u64) -> Option<&'a [u8]> {
    children(bytes, start, end).into_iter().find(|entry| entry.0 == id).and_then(|(_, a, e)| bytes.get(a..e))
}

/// Where the track behind ffprobe's stream `stream` keeps its blocks. ffprobe numbers a
/// Matroska file's streams in `TrackEntry` order, attachments after them.
pub(crate) fn index(read: Read, stream: i64) -> Result<Index, String> {
    let head = read(0, 64 * 1024)?;
    let (_, segment, segment_end) = children(&head, 0, head.len())
        .into_iter()
        .find(|entry| entry.0 == SEGMENT)
        .ok_or("no Matroska segment")?;
    let mut seeks = Vec::new();
    for (id, start, end) in children(&head, segment, segment_end) {
        if id == SEEK_HEAD && end <= head.len() {
            for (_, seek, seek_end) in children(&head, start, end).into_iter().filter(|entry| entry.0 == SEEK) {
                let id = child(&head, seek, seek_end, SEEK_ID).map(uint);
                let position = child(&head, seek, seek_end, SEEK_POSITION).map(uint);
                if let (Some(id), Some(position)) = (id, position) {
                    seeks.push((id, segment as u64 + position));
                }
            }
            break;
        }
    }
    let element = |wanted: u64| -> Result<Vec<u8>, String> {
        let at = seeks.iter().find(|seek| seek.0 == wanted).map(|seek| seek.1).ok_or_else(|| format!("no SeekHead entry {wanted:x}"))?;
        let header = read(at, 12)?;
        let (_, data) = vint(&header, 0, true).ok_or("bad element id")?;
        let (size, data) = vint(&header, data, false).ok_or("bad element size")?;
        if size > 64 * 1024 * 1024 {
            return Err(format!("element {wanted:x} of {size} bytes"));
        }
        read(at + data as u64, size as usize)
    };
    let info = element(INFO)?;
    let scale = child(&info, 0, info.len(), TIMESTAMP_SCALE).map(uint).filter(|scale| *scale > 0).unwrap_or(1_000_000);
    let tracks = element(TRACKS)?;
    let (_, start, end) = children(&tracks, 0, tracks.len())
        .into_iter()
        .filter(|entry| entry.0 == TRACK_ENTRY)
        .nth(usize::try_from(stream).map_err(|_| "negative stream index")?)
        .ok_or_else(|| format!("no track for stream {stream}"))?;
    let number = child(&tracks, start, end, TRACK_NUMBER).map(uint).ok_or("track without a number")?;
    let codec = child(&tracks, start, end, CODEC_ID).map(|bytes| String::from_utf8_lossy(bytes).to_string()).unwrap_or_default();
    if child(&tracks, start, end, TRACK_TYPE).map(uint) != Some(17) || !matches!(codec.as_str(), "S_TEXT/UTF8" | "S_TEXT/ASS" | "S_TEXT/SSA") {
        return Err(format!("stream {stream} is {codec}, not a text subtitle"));
    }
    let private = child(&tracks, start, end, CODEC_PRIVATE).unwrap_or_default().to_vec();
    let mut compression = Compression::None;
    if let Some(encodings) = child(&tracks, start, end, CONTENT_ENCODINGS) {
        for (_, a, e) in children(encodings, 0, encodings.len()).into_iter().filter(|entry| entry.0 == CONTENT_ENCODING) {
            if child(encodings, a, e, CONTENT_ENCODING_TYPE).map(uint).unwrap_or(0) != 0 {
                return Err("an encrypted track".to_string());
            }
            if let Some(compressed) = child(encodings, a, e, CONTENT_COMPRESSION) {
                compression = match child(compressed, 0, compressed.len(), CONTENT_COMP_ALGO).map(uint).unwrap_or(0) {
                    0 => Compression::Zlib,
                    3 => Compression::Strip(child(compressed, 0, compressed.len(), CONTENT_COMP_SETTINGS).unwrap_or_default().to_vec()),
                    other => return Err(format!("compression {other}")),
                };
            }
        }
    }
    let cues_bytes = element(CUES)?;
    let to_ms = |ticks: u64| (ticks as u128 * scale as u128 / 1_000_000) as i64;
    let mut cues = Vec::new();
    for (_, point, point_end) in children(&cues_bytes, 0, cues_bytes.len()).into_iter().filter(|entry| entry.0 == CUE_POINT) {
        let Some(time) = child(&cues_bytes, point, point_end, CUE_TIME).map(uint) else { continue };
        for (_, a, e) in children(&cues_bytes, point, point_end).into_iter().filter(|entry| entry.0 == CUE_TRACK_POSITIONS) {
            if child(&cues_bytes, a, e, CUE_TRACK).map(uint) != Some(number) {
                continue;
            }
            let (Some(cluster), Some(relative)) = (child(&cues_bytes, a, e, CUE_CLUSTER_POSITION).map(uint), child(&cues_bytes, a, e, CUE_RELATIVE_POSITION).map(uint)) else {
                continue;
            };
            let duration = child(&cues_bytes, a, e, CUE_DURATION).map(uint).map(to_ms);
            cues.push(Cue { start: to_ms(time), duration, cluster: segment as u64 + cluster, relative });
        }
    }
    if cues.is_empty() {
        return Err(format!("the Cues place no block of track {number}"));
    }
    cues.sort_by_key(|cue| (cue.start, cue.cluster, cue.relative));
    cues.dedup_by_key(|cue| (cue.cluster, cue.relative));
    Ok(Index { number, codec, private, compression, scale, cues })
}

impl Index {
    pub fn ass(&self) -> bool {
        self.codec != "S_TEXT/UTF8"
    }

    /// One block's text. `CueRelativePosition` counts from the Cluster's data, whose header
    /// is 5 to 12 bytes; each length is tried and the block must name this track.
    fn block(&self, read: Read, cue: &Cue) -> Result<Event, String> {
        let at = cue.cluster + cue.relative + 5;
        let mut bytes = read(at, 7 + 4096)?;
        for skip in 0..8 {
            let Some((id, data)) = vint(&bytes, skip, true) else { continue };
            let Some((size, data)) = vint(&bytes, data, false) else { continue };
            if !matches!(id, BLOCK_GROUP | SIMPLE_BLOCK) || size > 1024 * 1024 {
                continue;
            }
            let end = data + size as usize;
            if end > bytes.len() {
                // A long ASS line (a drawing) runs past the first read.
                let mut whole = read(at + skip as u64, end - skip)?;
                whole.splice(0..0, std::iter::repeat_n(0, skip));
                bytes = whole;
            }
            let (block, duration) = if id == SIMPLE_BLOCK {
                (&bytes[data..end], None)
            } else {
                let Some(block) = child(&bytes, data, end, BLOCK) else { continue };
                (block, child(&bytes, data, end, BLOCK_DURATION).map(uint))
            };
            let Some((track, payload)) = vint(block, 0, false) else { continue };
            // Track number, a 16-bit timecode, flags: no lacing for a subtitle.
            if track != self.number || block.len() < payload + 3 || block[payload + 2] & 0x06 != 0 {
                continue;
            }
            let payload = &block[payload + 3..];
            let text = match &self.compression {
                Compression::None => payload.to_vec(),
                Compression::Strip(prefix) => [prefix.as_slice(), payload].concat(),
                Compression::Zlib => {
                    let mut out = Vec::new();
                    std::io::Read::read_to_end(&mut flate2::read::ZlibDecoder::new(payload), &mut out).map_err(|error| error.to_string())?;
                    out
                }
            };
            let text = String::from_utf8_lossy(&text).trim_end_matches('\0').to_string();
            let duration = duration.map(|ticks| (ticks as u128 * self.scale as u128 / 1_000_000) as i64);
            return Ok(Event { start: cue.start, duration: cue.duration.or(duration), text });
        }
        Err(format!("no block of track {} at {at}", self.number))
    }

    /// The text of `cues`, read `workers` at a time. A block that cannot be read is left out.
    pub fn events(&self, read: Read, cues: &[Cue], workers: usize) -> (Vec<Event>, usize) {
        let next = AtomicUsize::new(0);
        let found = Mutex::new(Vec::with_capacity(cues.len()));
        let failed = AtomicUsize::new(0);
        std::thread::scope(|scope| {
            for _ in 0..workers.max(1).min(cues.len().max(1)) {
                scope.spawn(|| loop {
                    let Some(cue) = cues.get(next.fetch_add(1, Ordering::SeqCst)) else { break };
                    match self.block(read, cue) {
                        Ok(event) => found.lock().unwrap_or_else(|poison| poison.into_inner()).push(event),
                        Err(_) => {
                            failed.fetch_add(1, Ordering::SeqCst);
                        }
                    }
                });
            }
        });
        let mut events = found.into_inner().unwrap_or_else(|poison| poison.into_inner());
        events.sort_by_key(|event| event.start);
        (events, failed.into_inner())
    }

    /// The subtitle file FFmpeg's `subtitles` filter reads: SRT for `S_TEXT/UTF8`, the
    /// track's own ASS script otherwise. An event with no known duration lasts until the next.
    pub fn render(&self, events: &[Event]) -> String {
        let end = |index: usize| {
            let event = &events[index];
            match event.duration.filter(|duration| *duration > 0) {
                Some(duration) => event.start + duration,
                None => events.get(index + 1).map_or(event.start + 4000, |next| next.start.min(event.start + 8000)),
            }
        };
        if !self.ass() {
            let stamp = |ms: i64| format!("{:02}:{:02}:{:02},{:03}", ms / 3_600_000, ms / 60_000 % 60, ms / 1000 % 60, ms % 1000);
            return events
                .iter()
                .enumerate()
                .map(|(index, event)| format!("{}\n{} --> {}\n{}\n\n", index + 1, stamp(event.start), stamp(end(index)), event.text.replace("\r\n", "\n").trim_end()))
                .collect();
        }
        let stamp = |ms: i64| format!("{}:{:02}:{:02}.{:02}", ms / 3_600_000, ms / 60_000 % 60, ms / 1000 % 60, ms % 1000 / 10);
        let mut out = String::from_utf8_lossy(&self.private).trim_end().to_string();
        if !out.contains("[Events]") {
            out.push_str("\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text");
        }
        out.push('\n');
        for (index, event) in events.iter().enumerate() {
            // A block is `ReadOrder,Layer,Style,Name,MarginL,MarginR,MarginV,Effect,Text`
            // (SSA's second field is `Marked`); the file's Dialogue drops ReadOrder and
            // puts the times after the layer.
            let mut fields = event.text.splitn(3, ',');
            let (_, layer, rest) = (fields.next(), fields.next().unwrap_or("0"), fields.next().unwrap_or(""));
            out.push_str(&format!("Dialogue: {layer},{},{},{rest}\n", stamp(event.start), stamp(end(index))));
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A release generated by mkvmerge v103.0 (the muxer every release measured was made
    /// with): `tests/fixtures/transcode/mkvmerge_subtitles.mkv`, 1 s of 64x36 H.264 with an
    /// SRT, the same SRT zlib-compressed and an ASS track with two styles:
    /// `mkvmerge -o mkvmerge_subtitles.mkv v.mkv a.srt --compression 0:zlib a.srt c.ass`.
    const RELEASE: &[u8] = include_bytes!("../../../tests/fixtures/transcode/mkvmerge_subtitles.mkv");

    fn read(offset: u64, length: usize) -> Result<Vec<u8>, String> {
        let start = (offset as usize).min(RELEASE.len());
        Ok(RELEASE[start..(start + length).min(RELEASE.len())].to_vec())
    }

    #[test]
    fn an_srt_track_is_read_block_by_block_from_its_cues() {
        let index = index(&read, 1).unwrap();
        assert!(!index.ass());
        assert_eq!(index.cues.len(), 3);
        let (events, failed) = index.events(&read, &index.cues, 4);
        assert_eq!(failed, 0);
        assert_eq!(
            index.render(&events),
            "1\n00:00:00,100 --> 00:00:00,400\nFirst line\n\n2\n00:00:00,500 --> 00:00:00,700\nSecond\nline\n\n3\n00:00:00,800 --> 00:00:00,950\nThird, last\n\n"
        );
    }

    #[test]
    fn a_compressed_track_is_inflated() {
        let index = index(&read, 2).unwrap();
        assert_eq!(index.compression, Compression::Zlib);
        let (events, failed) = index.events(&read, &index.cues, 4);
        assert_eq!(failed, 0);
        assert_eq!(events.iter().map(|event| event.text.as_str()).collect::<Vec<_>>(), ["First line", "Second\r\nline", "Third, last"]);
    }

    #[test]
    fn an_ass_track_keeps_its_script_header_and_styles() {
        let index = index(&read, 3).unwrap();
        assert!(index.ass());
        let (events, _) = index.events(&read, &index.cues, 4);
        let script = index.render(&events);
        assert!(script.contains("Style: Big,"), "{script}");
        assert!(script.contains("\nDialogue: 0,0:00:00.10,0:00:00.40,Big,,0,0,0,,{\\an8}Top line\n"), "{script}");
        assert!(script.ends_with("Dialogue: 1,0:00:00.50,0:00:00.90,Default,,0,0,0,,Comma, kept\n"), "{script}");
    }

    #[test]
    fn a_stream_that_is_not_text_is_refused() {
        assert!(index(&read, 0).unwrap_err().contains("not a text subtitle"));
        assert!(index(&read, 9).is_err());
    }
}
