//! Independent consumer checks, not byte snapshots emitted by rex.
use super::*;
use serde_json::json;

fn scratch() -> PathBuf {
    let path = std::env::temp_dir().join(format!("rex-thumbnail-test-{}-{}", std::process::id(), SERIAL.fetch_add(1, Ordering::Relaxed)));
    fs::create_dir(&path).unwrap();
    path
}

fn input(url: String) -> analysis::Input {
    analysis::Input { key: "1".into(), part_id: "2".into(), url, auth: None, duration_ms: 21_000,
        audio_index: None, video_index: Some(0), chapters: vec![
            json!({"index":1,"startTimeOffset":0}),
            json!({"index":2,"startTimeOffset":10_040}),
            json!({"index":3,"startTimeOffset":20_040})], group: None }
}

#[test]
fn absent_video_and_disabled_work_do_not_create_artifacts() {
    let root = scratch();
    let destination = root.join("not-created");
    let mut source = input("never-read".into());
    source.video_index = None;
    let generated = generate(Path::new("never-executed"), &source, &destination, true, true).unwrap();
    assert!(generated.bif.is_none() && generated.chapter_images.is_empty());
    assert!(!destination.exists());
    source.video_index = Some(0);
    assert!(generate(Path::new("never-executed"), &source, &destination, false, false).unwrap().bif.is_none());
    assert!(!valid_jpeg(&destination));
    assert!(validate_bif(&destination).is_err());
    fs::remove_dir_all(root).unwrap();
}

#[test]
fn unbounded_indexes_and_header_injection_are_rejected_before_io() {
    let root = scratch();
    let destination = root.join("not-created");
    let mut source = input("never-read".into());
    source.duration_ms = i64::MAX;
    assert!(generate(Path::new("never-executed"), &source, &destination, true, false).is_err());
    source.duration_ms = 100;
    source.auth = Some("Bearer secret\r\nOther: header".into());
    assert!(generate(Path::new("never-executed"), &source, &destination, true, false).is_err());
    assert!(!destination.exists());
    fs::remove_dir_all(root).unwrap();
}

/// A separate parser transcribed from Roku's published v0 tables. In particular,
/// this never calls rex's writer, parser, magic constant or image-count constant.
fn reference_frames(bytes: &[u8]) -> Vec<(u64, &[u8])> {
    assert_eq!(&bytes[0..8], &[137, 66, 73, 70, 13, 10, 26, 10]);
    let uint = |offset| u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap()) as usize;
    assert_eq!(uint(8), 0);
    assert!(bytes[20..64].iter().all(|byte| *byte == 0));
    let count = uint(12);
    let multiplier = match uint(16) { 0 => 1000, n => n };
    assert_eq!(uint(64 + count * 8), 0xffff_ffff);
    assert_eq!(uint(68 + count * 8), bytes.len());
    let mut result = Vec::new();
    for index in 0..count {
        let start = uint(68 + index * 8);
        let end = uint(76 + index * 8);
        assert!(start >= 64 + (count + 1) * 8 && end > start);
        let jpeg = &bytes[start..end];
        assert_eq!(&jpeg[..2], &[255, 216]);
        assert_eq!(&jpeg[jpeg.len() - 2..], &[255, 217]);
        result.push(((uint(64 + index * 8) * multiplier) as u64, jpeg));
    }
    result
}

fn run(command: &mut Command) {
    assert!(command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::inherit()).status().unwrap().success());
}

fn pixel(ffmpeg: &Path, root: &Path, image: &[u8], name: &str) -> [u8; 3] {
    let path = root.join(format!("{name}.jpg"));
    let raw = root.join(format!("{name}.rgb"));
    fs::write(&path, image).unwrap();
    run(Command::new(ffmpeg).args(["-v", "error", "-y", "-i"]).arg(&path)
        .args(["-vf", "scale=1:1", "-pix_fmt", "rgb24", "-frames:v", "1", "-f", "rawvideo"]).arg(&raw));
    fs::read(raw).unwrap().try_into().unwrap()
}

/// Run explicitly on the isolated media-test host with FFmpeg installed:
/// cargo test thumbnails::tests::ffmpeg_images_and_roku_index -- --ignored
/// REX_THUMBNAIL_TEST_FFMPEG may name an absolute FFmpeg executable.
#[test]
#[ignore = "requires real FFmpeg media processing; isolated-host smoke test"]
fn ffmpeg_images_and_roku_index() {
    let ffmpeg = PathBuf::from(std::env::var_os("REX_THUMBNAIL_TEST_FFMPEG").unwrap_or_else(|| "ffmpeg".into()));
    let root = scratch();
    let video = root.join("colors.mkv");
    run(Command::new(&ffmpeg).args(["-v", "error", "-y", "-f", "lavfi", "-i", "color=red:s=64x48:r=25:d=10",
        "-f", "lavfi", "-i", "color=green:s=64x48:r=25:d=10", "-f", "lavfi", "-i", "color=blue:s=64x48:r=25:d=1",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-c:v", "ffv1"]).arg(&video));
    let mut source = input(video.to_str().unwrap().into());
    let cache = root.join("cache");
    let generated = generate(&ffmpeg, &source, &cache, true, true).unwrap();
    let bif = generated.bif.as_ref().unwrap();
    let data = fs::read(bif).unwrap();
    let frames = reference_frames(&data);
    assert_eq!(frames.iter().map(|(time, _)| *time).collect::<Vec<_>>(), [0, 10_000, 20_000]);
    for (index, (_, jpeg)) in frames.iter().enumerate() {
        let rgb = pixel(&ffmpeg, &root, jpeg, &format!("seek{index}"));
        assert!(rgb[index] > 70 && rgb[index] > rgb[(index + 1) % 3].saturating_add(50), "wrong timed frame: {rgb:?}");
        assert_eq!(seek_jpeg(bif, index as u64 * 10_000 + 123).unwrap(), *jpeg);
    }
    assert_eq!(generated.chapter_images.len(), 3);
    for (index, path) in &generated.chapter_images {
        let rgb = pixel(&ffmpeg, &root, &fs::read(path).unwrap(), &format!("chapter{index}"));
        assert!(rgb[index - 1] > 70 && rgb[index - 1] > rgb[index % 3].saturating_add(50));
    }
    // Short clips still have a zero-time preview, no fabricated chapter image.
    source.duration_ms = 1;
    source.chapters.clear();
    let short = generate(&ffmpeg, &source, &cache, true, true).unwrap();
    assert_eq!(reference_frames(&fs::read(short.bif.as_ref().unwrap()).unwrap()).len(), 1);
    assert!(short.chapter_images.is_empty());
    // Truncation, impossible offsets, and bad terminal entries cannot advertise.
    for (name, corrupt) in [
        ("truncated", data[..data.len() - 1].to_vec()),
        ("bad-offset", { let mut bytes = data.clone(); bytes[68..72].copy_from_slice(&0u32.to_le_bytes()); bytes }),
        ("bad-sentinel", { let mut bytes = data.clone(); bytes[88..92].copy_from_slice(&0u32.to_le_bytes()); bytes }),
    ] {
        let path = root.join(name); fs::write(&path, corrupt).unwrap();
        assert!(validate_bif(&path).is_err());
        assert!(seek_jpeg(&path, 0).is_err());
    }
    fs::remove_dir_all(root).unwrap();
}
