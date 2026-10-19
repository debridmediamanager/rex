//! An MP4 or MOV release's timed text (`tx3g`, ffprobe's `mov_text`), placed sample by sample.
//!
//! The sample tables in `moov` give every sample's offset, size and time, so the track is read
//! as a Matroska one is through its Cues (`mkvsubs`): one Range request per line of text.
//! `moov` is found by walking the top-level boxes, wherever the muxer put it.

use super::mkvsubs::{Compression, Cue, Index, Place, Read};

/// The boxes between `start` and `end`: type, data start, data end.
fn boxes(bytes: &[u8], start: usize, end: usize) -> Vec<([u8; 4], usize, usize)> {
    let mut out = Vec::new();
    let mut at = start;
    let end = end.min(bytes.len());
    while at + 8 <= end {
        let size = u32::from_be_bytes(bytes[at..at + 4].try_into().unwrap()) as usize;
        let kind: [u8; 4] = bytes[at + 4..at + 8].try_into().unwrap();
        let (data, stop) = match size {
            0 => (at + 8, end),
            1 if at + 16 <= end => (at + 16, at.saturating_add(u64::from_be_bytes(bytes[at + 8..at + 16].try_into().unwrap()) as usize)),
            size if size >= 8 => (at + 8, at.saturating_add(size)),
            _ => break,
        };
        if stop > end {
            break;
        }
        out.push((kind, data, stop));
        at = stop;
    }
    out
}

fn child<'a>(bytes: &'a [u8], kind: &[u8; 4]) -> Option<&'a [u8]> {
    boxes(bytes, 0, bytes.len()).into_iter().find(|entry| &entry.0 == kind).map(|(_, a, e)| &bytes[a..e])
}

fn path<'a>(bytes: &'a [u8], kinds: &[&[u8; 4]]) -> Option<&'a [u8]> {
    kinds.iter().try_fold(bytes, |inner, kind| child(inner, kind))
}

fn u32_at(bytes: &[u8], at: usize) -> Option<u64> {
    Some(u32::from_be_bytes(bytes.get(at..at + 4)?.try_into().ok()?) as u64)
}

/// A full box's timescale (`mvhd`, `mdhd`): after the times, whose width its version sets.
fn timescale(full: &[u8]) -> Option<u64> {
    u32_at(full, if full.first() == Some(&1) { 20 } else { 12 }).filter(|scale| *scale > 0)
}

/// A full box's table: its entry count after version and flags, then `width` bytes each.
fn table(full: &[u8], skip: usize, width: usize) -> Vec<&[u8]> {
    let count = u32_at(full, 4 + skip).unwrap_or(0) as usize;
    let rows = full.get(8 + skip..).unwrap_or_default();
    rows.chunks_exact(width).take(count).collect()
}

fn be(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0, |value, byte| (value << 8) | *byte as u64)
}

/// The `tx3g` track behind ffprobe's stream `stream`: ffprobe numbers an MP4's streams in
/// `trak` order.
pub(crate) fn index(read: Read, stream: i64) -> Result<Index, String> {
    let mut at = 0u64;
    let moov = loop {
        let head = read(at, 16)?;
        if head.len() < 8 {
            return Err("no moov box".to_string());
        }
        let size = match be(&head[..4]) {
            1 => be(head.get(8..16).ok_or("a short box header")?),
            0 => return Err("no moov box before the open-ended last box".to_string()),
            size => size,
        };
        let header = if be(&head[..4]) == 1 { 16 } else { 8 };
        if size < header {
            return Err(format!("a {size} byte box at {at}"));
        }
        if &head[4..8] == b"moov" {
            if size > 64 * 1024 * 1024 {
                return Err(format!("a moov of {size} bytes"));
            }
            break read(at + header, (size - header) as usize)?;
        }
        at = at.checked_add(size).ok_or("a box past any file's end")?;
    };
    let movie_scale = child(&moov, b"mvhd").and_then(timescale).unwrap_or(1000);
    let trak = boxes(&moov, 0, moov.len())
        .into_iter()
        .filter(|entry| &entry.0 == b"trak")
        .nth(usize::try_from(stream).map_err(|_| "negative stream index")?)
        .map(|(_, a, e)| &moov[a..e])
        .ok_or_else(|| format!("no track for stream {stream}"))?;
    let entry = path(trak, &[b"mdia", b"minf", b"stbl", b"stsd"]).and_then(|stsd| stsd.get(12..16));
    if entry != Some(b"tx3g") {
        return Err(format!("stream {stream} is {}, not timed text", String::from_utf8_lossy(entry.unwrap_or_default())));
    }
    let scale = path(trak, &[b"mdia", b"mdhd"]).and_then(timescale).ok_or("no media timescale")?;
    // The edit list: leading empty edits delay the track, the first real one starts it at
    // its media time.
    let mut delay_ms = 0i64;
    let mut media_start = 0i64;
    if let Some(elst) = path(trak, &[b"edts", b"elst"]) {
        let wide = elst.first() == Some(&1);
        for edit in table(elst, 0, if wide { 20 } else { 12 }) {
            let (duration, time) = if wide {
                (be(&edit[..8]), be(&edit[8..16]) as i64)
            } else {
                (be(&edit[..4]), be(&edit[4..8]) as u32 as i32 as i64)
            };
            if time == -1 {
                delay_ms += (duration * 1000 / movie_scale) as i64;
            } else {
                media_start = time;
                break;
            }
        }
    }
    let stbl = path(trak, &[b"mdia", b"minf", b"stbl"]).ok_or("no sample table")?;
    let stsz = child(stbl, b"stsz").ok_or("no stsz")?;
    let fixed = u32_at(stsz, 4).unwrap_or(0) as usize;
    // The count is the file's word: one fixed size for 4 billion samples asked a 32 GiB
    // table. A text track has thousands of lines.
    let count = (u32_at(stsz, 8).unwrap_or(0) as usize).min(1 << 20);
    let sizes: Vec<usize> = if fixed > 0 { vec![fixed; count] } else { table(stsz, 4, 4).into_iter().map(|row| be(row) as usize).collect() };
    let chunks: Vec<u64> = match child(stbl, b"co64") {
        Some(co64) => table(co64, 0, 8).into_iter().map(be).collect(),
        None => table(child(stbl, b"stco").ok_or("no stco")?, 0, 4).into_iter().map(be).collect(),
    };
    let stsc: Vec<(u64, u64)> = table(child(stbl, b"stsc").ok_or("no stsc")?, 0, 12).into_iter().map(|row| (be(&row[..4]), be(&row[4..8]))).collect();
    let mut offsets = Vec::with_capacity(sizes.len());
    for (number, chunk) in chunks.iter().enumerate() {
        let per_chunk = stsc.iter().rev().find(|entry| entry.0 <= number as u64 + 1).map_or(1, |entry| entry.1);
        let mut offset = *chunk;
        for _ in 0..per_chunk {
            let Some(size) = sizes.get(offsets.len()) else { break };
            offsets.push((offset, *size));
            offset += *size as u64;
        }
    }
    let mut cues = Vec::new();
    let mut ticks = 0i64;
    let mut sample = 0;
    for row in table(child(stbl, b"stts").ok_or("no stts")?, 0, 8) {
        let (repeat, delta) = (be(&row[..4]), be(&row[4..8]) as i64);
        for _ in 0..repeat {
            // Past the last sample the table addresses nothing more: a row claiming billions
            // of repeats spun here for seconds.
            if sample >= offsets.len() {
                break;
            }
            let to_ms = |ticks: i64| ticks * 1000 / scale as i64;
            // An empty sample (its two length bytes alone) is a gap between lines.
            if let Some(&(offset, size)) = offsets.get(sample).filter(|entry| entry.1 > 2 && entry.1 <= 64 * 1024) {
                cues.push(Cue { start: delay_ms + to_ms(ticks - media_start), duration: Some(to_ms(delta)), place: Place::Sample { offset, size } });
            }
            ticks += delta;
            sample += 1;
        }
    }
    if cues.is_empty() {
        return Err(format!("stream {stream} has no text"));
    }
    Ok(Index { number: 0, codec: "tx3g".to_string(), private: Vec::new(), compression: Compression::None, scale: 1_000_000, cues })
}

#[cfg(test)]
mod tests {
    use super::super::mkvsubs;

    /// Releases muxed by FFmpeg 8 (libavformat: what HandBrake and most MP4 rips are written
    /// with): 1 s of 64x36 H.264 with `mkvsubs`' three-line SRT as `mov_text`, its `moov`
    /// after `mdat` as the muxer leaves it, and moved to the front by `-movflags +faststart`:
    /// `ffmpeg -i v.mkv -i a.srt -map 0 -map 1 -c:v copy -c:s mov_text mov_text.mp4`.
    const AT_END: &[u8] = include_bytes!("../../../tests/fixtures/transcode/mov_text.mp4");
    const AT_FRONT: &[u8] = include_bytes!("../../../tests/fixtures/transcode/mov_text_faststart.mp4");

    fn reader(release: &'static [u8]) -> impl Fn(u64, usize) -> Result<Vec<u8>, String> + Sync {
        move |offset, length| {
            let start = (offset as usize).min(release.len());
            Ok(release[start..(start + length).min(release.len())].to_vec())
        }
    }

    #[test]
    fn timed_text_is_read_sample_by_sample_wherever_moov_is() {
        for release in [AT_END, AT_FRONT] {
            let read = reader(release);
            let index = mkvsubs::index(&read, 1).unwrap();
            assert!(!index.ass());
            let (events, failed, _) = index.events(&read, &index.cues);
            assert_eq!(failed, 0);
            assert_eq!(
                index.render(&events),
                "1\n00:00:00,100 --> 00:00:00,400\nFirst line\n\n2\n00:00:00,500 --> 00:00:00,700\nSecond\nline\n\n3\n00:00:00,800 --> 00:00:00,950\nThird, last\n\n"
            );
        }
    }

    /// `AT_FRONT` with 32-bit words written into its text track's `kind` box, at offsets from
    /// the box's type: the text track is the second `trak`, so its tables are the last ones.
    fn patched(kind: &[u8; 4], words: &[(usize, u32)]) -> &'static [u8] {
        let mut release = AT_FRONT.to_vec();
        let at = release.windows(4).rposition(|window| window == kind).unwrap();
        for (offset, value) in words {
            release[at + offset..at + offset + 4].copy_from_slice(&value.to_be_bytes());
        }
        Box::leak(release.into_boxed_slice())
    }

    #[test]
    fn a_sample_table_claiming_billions_of_samples_is_read_as_far_as_it_reaches() {
        // stts (version and flags at +4, its count at +8, rows of 8 from +12): the last of its
        // seven rows repeats 4,294,967,295 times, past the seven samples there are.
        let read = reader(patched(b"stts", &[(12 + 8 * 6, u32::MAX)]));
        let started = std::time::Instant::now();
        let index = mkvsubs::index(&read, 1).unwrap();
        assert!(started.elapsed() < std::time::Duration::from_millis(500), "{:?}", started.elapsed());
        assert_eq!(index.cues.len(), 3);
        // stsz (sample size at +8, count at +12): one size for 4,294,967,295 samples asked for
        // a 32 GiB table before anything was read.
        let read = reader(patched(b"stsz", &[(8, 1), (12, u32::MAX)]));
        let started = std::time::Instant::now();
        let _ = mkvsubs::index(&read, 1);
        assert!(started.elapsed() < std::time::Duration::from_millis(500), "{:?}", started.elapsed());
    }

    #[test]
    fn a_stream_that_is_not_timed_text_is_refused() {
        let read = reader(AT_END);
        assert!(mkvsubs::index(&read, 0).unwrap_err().contains("not timed text"));
        assert!(mkvsubs::index(&read, 5).is_err());
    }
}
