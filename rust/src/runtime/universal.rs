//! The universal video transcoder: `/video/:/transcode/universal/start.m3u8` and the HLS
//! session it opens, served by an FFmpeg child per session that reads the Part's bytes
//! straight from its source.
//!
//! Off unless `REX_TRANSCODER` names an FFmpeg binary (jellyfin-ffmpeg is the one this
//! was measured with: its full-GPU VA-API pipeline and its own Intel driver). Without it
//! rex is the encoder-less server every parity suite measures, and nothing here runs.
//!
//! The wire shapes are plexscratch 1.43.4's, recorded for Plex Web 4.160's exact request
//! (`tests/fixtures/transcode/`): the master playlist names `session/<session>/base/
//! index.m3u8`, the media playlist lists EVERY segment of the title up front with
//! `#EXTINF:<n>, nodesc` and `#EXT-X-ENDLIST`, segments are `%05d.ts` MPEG-TS, and
//! ping/stop are empty 200s that become 404s once the session is gone.
//!
//! What is ours rather than Plex's: segment lengths (4 s converted, 10 s remuxed, as
//! Plex remuxes), the encoder settings, the hardware ladder, and how a far seek is
//! served -- ffmpeg is restarted at the segment's time with timestamps counted from
//! there and the segmenter's `-initial_offset` putting them back on the title's clock.
//! Plex's own fork keeps absolute timestamps (`-copyts` + its `-segment_copyts`); on
//! upstream FFmpeg 8 that pair wrote ONE unreadable segment for a seeked remux
//! (measured on the test host, jellyfin-ffmpeg 8.1.3), where the offset cuts every
//! boundary.

use super::*;
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

/// A converted segment's length. rex places a keyframe on every boundary, so it can be
/// short, and the first one is what a play start waits for: on the test host's iGPU a 2 s
/// segment is finished 1.3 s after FFmpeg starts, a 5 s one 2.4 s (measured, HEVC 1080p).
const TRANSCODE_SEGMENT: u32 = 2;
/// A remux cannot place keyframes, so its cut points are wherever the source's GOP
/// allows; Plex remuxes in 10 s segments for the same reason.
const COPY_SEGMENT: u32 = 10;
/// DASH's `SegmentTemplate duration` for a remux: the reference's 5 s. A remux cuts
/// only on the source's keyframes, so a segment shorter than its GOP would number
/// segments out of step with time; a conversion uses `TRANSCODE_SEGMENT`.
const DASH_COPY_SEGMENT: u32 = 5;
/// How far production may run ahead of the player before ffmpeg is paused
/// (`TranscoderThrottleBuffer`, whose default is 60 s), and where it resumes.
const THROTTLE_AHEAD: f64 = 90.0;
const RESUME_AHEAD: f64 = 45.0;
/// A session nobody has asked anything of for this long is killed.
const IDLE: Duration = Duration::from_secs(120);
/// A request this many segments beyond what has been produced is a seek, not a wait.
const SEEK_GAP: i64 = 4;
/// Segments more than this far behind the player are deleted.
const KEEP_BEHIND: f64 = 120.0;
/// An embedded text subtitle is read one line at a time (`mkvsubs::Index::events`), in time
/// order from where the player is: the run starts on the first two minutes and restarts on
/// fifteen, then on all of it. At `SUBTITLE_RATE` that is 12-18 minutes of a film read each
/// minute, many times what a conversion makes, so the run never reaches a minute not yet read.
const SUBTITLE_WINDOWS_MS: [i64; 2] = [2 * 60 * 1000, 15 * 60 * 1000];
/// Reads of one track: `SUBTITLE_BURST` at once, then `SUBTITLE_RATE` a second. Real-Debrid's
/// CDN resets the connection after a budget of reads of one link, which zurg answers with a
/// 500 and, after three, a 5-minute hold on the file: unpaced (~10 a second) the 219th read
/// failed, at 6 a second the 399th, at 4 a second 450 passed (zurg on fun, 2026-10-19). That
/// reads as a burst of ~120 refilled at ~4 a second; rex stays under both, on every source.
const SUBTITLE_BURST: f64 = 100.0;
const SUBTITLE_RATE: f64 = 3.0;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum Accel {
    None,
    Vaapi(String),
    Cuda,
}

impl Accel {
    fn name(&self) -> &'static str {
        match self {
            Accel::None => "none",
            Accel::Vaapi(_) => "vaapi",
            Accel::Cuda => "cuda",
        }
    }
}

/// What the probe found the device can do: decoders by `codec` / `codec10`, and whether
/// the H.264 encoder opened.
#[derive(Clone, Debug)]
pub(crate) struct Hardware {
    pub accel: Accel,
    pub decode: HashSet<String>,
    pub encode: bool,
    /// jellyfin-ffmpeg's `tonemapx`; upstream FFmpeg has none, and an HDR source then
    /// converts untonemapped rather than failing on every path.
    pub tonemapx: bool,
    /// The device tonemaps through OpenCL derived from VA-API (jellyfin-ffmpeg's
    /// `tonemap_opencl`, which applies a profile 5 RPU): HDR then stays on the GPU end to end.
    /// fun's J4105 converts 4K HDR10 and DV5 at 1.3-1.7x so, against 0.1-0.2x through the
    /// CPU's tonemapx (measured 2026-10-17).
    pub opencl_tonemap: bool,
    /// Ask the source for bounded ranges (`-request_size`). Windows only: jellyfin-ffmpeg
    /// 8.1.3's Windows build reads an abandoned open-ended range to its end, so a seek into
    /// a 74 MB file read 150 MB and took 78 s at 2 MB/s, against 2.2 MB on Linux, macOS and
    /// Gyan's Windows build (measured 2026-10-12). Bounded, it read 3.9 MB in 2 s.
    pub bounded_http: bool,
    /// A burned text subtitle is drawn on a transparent canvas at 10 frames a second and laid
    /// over the picture on the device (jellyfin-ffmpeg's `alphasrc` and the `subtitles` filter's
    /// `alpha`/`sub2video`, with `overlay_vaapi`), as Jellyfin does on Intel. Downloading every
    /// frame to draw on it and uploading it again ran a 4K HDR remux on fun's J4105 at 0.32x,
    /// against 1.94x without the subtitle; the overlay ran it at 1.53x (measured 2026-10-19).
    pub alpha_subtitles: bool,
}

impl Hardware {
    fn software() -> Self {
        Self { accel: Accel::None, decode: HashSet::new(), encode: false, tonemapx: false, opencl_tonemap: false, bounded_http: false, alpha_subtitles: false }
    }
}

#[derive(Clone, Debug)]
pub(crate) struct VideoPlan {
    pub index: i64,
    pub copy: bool,
    pub source_codec: String,
    pub bit_depth: i64,
    pub hdr: bool,
    pub width: i64,
    pub height: i64,
    pub bitrate: i64,
    pub frame_rate: f64,
    /// The source's `profile` and `level`, for a copied stream's DASH `codecs`.
    pub source_profile: String,
    pub source_level: i64,
}

#[derive(Clone, Debug)]
pub(crate) struct AudioPlan {
    pub index: i64,
    pub copy: bool,
    pub source_codec: String,
    pub codec: String,
    pub channels: i64,
    pub bitrate: i64,
    pub sampling_rate: i64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Protocol {
    Hls,
    Dash,
    /// A download's one whole MP4 file, the profile's `static` target (offline downloads).
    File,
}

impl Protocol {
    pub fn name(self) -> &'static str {
        match self {
            Protocol::Hls => "hls",
            Protocol::Dash => "dash",
            Protocol::File => "http",
        }
    }

    pub fn container(self) -> &'static str {
        match self {
            Protocol::Hls => "mpegts",
            Protocol::Dash | Protocol::File => "mp4",
        }
    }

    pub fn parse(name: &str) -> Option<Self> {
        match name {
            "hls" => Some(Protocol::Hls),
            "dash" => Some(Protocol::Dash),
            "static" => Some(Protocol::File),
            _ => None,
        }
    }
}

/// One conversion, decided from the client's own request: what to copy, what to
/// convert to what, and where the bytes are.
#[derive(Clone, Debug)]
pub(crate) struct Plan {
    pub rating_key: String,
    pub url: String,
    pub auth: Option<String>,
    pub duration_ms: i64,
    pub video: Option<VideoPlan>,
    pub audio: Option<AudioPlan>,
    /// The selected embedded image subtitle's ffprobe index, burned into the picture.
    pub subtitle: Option<i64>,
    /// HLS in Matroska segments behind an `EXT-X-MAP` header, for a target whose container is
    /// `mkv` (Plex for Android and iOS: `subtitles_s1422.json`), carrying `text_subtitle`.
    pub matroska: bool,
    /// The selected embedded text subtitle's ffprobe index, copied into the Matroska segments.
    pub text_subtitle: Option<i64>,
    /// It is an MP4's timed text (`mov_text`), which Matroska cannot hold: converted to SRT.
    pub text_to_srt: bool,
    /// A selected subtitle uploaded to rex: copied into Matroska segments, burned otherwise.
    pub sidecar: Option<Sidecar>,
    /// A selected embedded text subtitle of a Matroska release, burned for a client that is
    /// not given Matroska: read out of the release before the run (`mkvsubs`).
    pub text_burn: Option<TextBurn>,
    pub incomplete_segments: bool,
    /// The playback session (`X-Plex-Session-Identifier`) the conversion belongs to.
    pub playback: String,
    pub protocol: Protocol,
}

/// A subtitle file uploaded to rex (`_content` on its Stream row), written beside the run's
/// segments as FFmpeg's second input.
#[derive(Clone, Debug)]
pub(crate) struct Sidecar {
    /// The Stream row's `id`, which the decision's row is matched by.
    pub id: String,
    pub format: String,
    pub content: String,
}

/// An embedded text subtitle to burn: its ffprobe index, and whether it is ASS/SSA.
#[derive(Clone, Debug)]
pub(crate) struct TextBurn {
    pub index: i64,
    pub ass: bool,
}

impl TextBurn {
    pub fn file_name(&self) -> &'static str {
        if self.ass { "embedded.ass" } else { "embedded.srt" }
    }
}

impl Sidecar {
    pub fn file_name(&self) -> String {
        format!("sidecar.{}", if self.format == "ssa" { "ass" } else { self.format.as_str() })
    }

    /// Whether this plan draws it into the picture rather than carrying it as a stream.
    fn burned(plan: &Plan) -> bool {
        plan.sidecar.is_some() && !plan.matroska
    }
}

/// `PT0H30M0.689941S`, as the reference writes a DASH duration.
fn dash_duration(milliseconds: i64) -> String {
    // Through a 32-bit float, as the reference's own `0.689941` for 1 800 690 ms shows.
    let seconds = (milliseconds.max(0) as f32 / 1000.0) as f64;
    let hours = (seconds / 3600.0).floor();
    let minutes = ((seconds - hours * 3600.0) / 60.0).floor();
    format!("PT{}H{}M{:.6}S", hours as i64, minutes as i64, seconds - hours * 3600.0 - minutes * 60.0)
}

impl Plan {
    /// The output's container, as the decision and the session rows name it.
    pub fn container(&self) -> &'static str {
        if self.matroska {
            "mkv"
        } else {
            self.protocol.container()
        }
    }

    /// The DASH representations in output order: the video's id 0, the audio's next.
    pub fn representations(&self) -> usize {
        self.video.is_some() as usize + self.audio.is_some() as usize
    }

    /// The video's `codecs`: H.264 High at the level its size and rate need, or the
    /// copied source's own profile and level.
    fn video_codecs(&self) -> String {
        let Some(video) = self.video.as_ref() else {
            return String::new();
        };
        if video.copy && video.source_codec == "hevc" {
            // ffprobe reports an HEVC level as the `codecs` string writes it (120 = 4.0).
            return format!("hvc1.{}.6.L{}.B0", if video.bit_depth > 8 { 2 } else { 1 }, video.source_level.max(30));
        }
        let (profile, level) = if video.copy {
            let profile = match video.source_profile.to_ascii_lowercase().as_str() {
                "baseline" | "constrained baseline" => "42",
                "main" => "4d",
                _ => "64",
            };
            (profile, video.source_level.clamp(10, 62))
        } else {
            let pixels = video.width * video.height;
            let fast = video.frame_rate > 31.0;
            let level = match () {
                _ if pixels > 2_228_224 => 51,
                _ if pixels > 921_600 && fast => 42,
                _ if pixels > 921_600 => 40,
                _ if fast => 32,
                _ => 31,
            };
            ("64", level)
        };
        format!("avc1.{profile}00{level:02x}")
    }

    fn audio_codecs(&self) -> &'static str {
        match self.audio.as_ref().map(|audio| audio.codec.as_str()) {
            Some("ac3") => "ac-3",
            Some("eac3") => "ec-3",
            Some("mp3") => "mp4a.40.34",
            Some("opus") => "opus",
            Some("flac") => "fLaC",
            _ => "mp4a.40.2",
        }
    }

    /// `start.mpd`, in the reference's own layout (`scratch_dash_copy.json`).
    pub fn mpd(&self, session: &str) -> String {
        let length = self.segment_seconds();
        let duration = dash_duration(self.duration_ms);
        let template = format!(
            "\t\t\t<SegmentTemplate timescale=\"1\" duration=\"{length}\" initialization=\"session/{session}/$RepresentationID$/header\" media=\"session/{session}/$RepresentationID$/$Number$.m4s\" startNumber=\"0\">\n\t\t\t</SegmentTemplate>\n"
        );
        let mut out = format!(
            "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<MPD xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"\n\txmlns=\"urn:mpeg:dash:schema:mpd:2011\"\n\txmlns:xlink=\"http://www.w3.org/1999/xlink\"\n\txsi:schemaLocation=\"urn:mpeg:dash:schema:mpd:2011 http://standards.iso.org/ittf/PubliclyAvailableStandards/MPEG-DASH_schema_files/DASH-MPD.xsd\"\n\tprofiles=\"urn:mpeg:dash:profile:isoff-live:2011\"\n\ttype=\"static\"\n\tmediaPresentationDuration=\"{duration}\"\n\tmaxSegmentDuration=\"PT{}S\"\n\tminBufferTime=\"PT{}S\">\n\t<Period start=\"PT0S\" id=\"0\" duration=\"{duration}\">\n",
            length * 2,
            // Ours, not the reference's 2x segment: Plex Web's Shaka player starts only
            // once max(minBufferTime, 2 s) is buffered, so PT10S held every start for
            // two 5 s segments (6.3 s to first frame on the test host, measured).
            length
        );
        let mut id = 0;
        if let Some(video) = self.video.as_ref() {
            out.push_str("\t\t<AdaptationSet segmentAlignment=\"true\">\n");
            out.push_str(&template);
            out.push_str(&format!(
                "\t\t\t<Representation id=\"{id}\" mimeType=\"video/mp4\" codecs=\"{}\" bandwidth=\"{}\" width=\"{}\" height=\"{}\">\n\t\t\t</Representation>\n\t\t</AdaptationSet>\n",
                self.video_codecs(),
                video.bitrate * 1000,
                video.width,
                video.height
            ));
            id += 1;
        }
        if let Some(audio) = self.audio.as_ref() {
            out.push_str("\t\t<AdaptationSet segmentAlignment=\"true\">\n");
            out.push_str(&template);
            out.push_str(&format!(
                "\t\t\t<Representation id=\"{id}\" mimeType=\"audio/mp4\" codecs=\"{}\" bandwidth=\"{}\" audioSamplingRate=\"{}\">\n\t\t\t\t<AudioChannelConfiguration schemeIdUri=\"urn:mpeg:dash:23003:3:audio_channel_configuration:2011\" value=\"{}\"/>\n\t\t\t</Representation>\n\t\t</AdaptationSet>\n",
                self.audio_codecs(),
                audio.bitrate * 1000,
                audio.sampling_rate,
                audio.channels
            ));
        }
        out.push_str("\t</Period>\n</MPD>\n");
        out
    }

    pub fn segment_seconds(&self) -> u32 {
        match (self.protocol, self.video.as_ref().is_none_or(|video| video.copy)) {
            (_, false) => TRANSCODE_SEGMENT,
            (Protocol::Dash, true) => DASH_COPY_SEGMENT,
            (Protocol::Hls | Protocol::File, true) => COPY_SEGMENT,
        }
    }

    pub fn segment_count(&self) -> i64 {
        let length = self.segment_seconds() as i64 * 1000;
        ((self.duration_ms.max(1) + length - 1) / length).max(1)
    }

    /// The output's total bitrate in kbps, as the decision's `Media` reports it.
    pub fn bitrate(&self) -> i64 {
        self.video.as_ref().map_or(0, |video| video.bitrate)
            + self.audio.as_ref().map_or(0, |audio| audio.bitrate)
    }

    /// The master playlist (`start.m3u8`), byte for byte the reference's.
    pub fn master_playlist(&self, session: &str) -> String {
        let (width, height, rate) = self
            .video
            .as_ref()
            .map_or((0, 0, 0.0), |video| (video.width, video.height, video.frame_rate));
        let suffix = if self.incomplete_segments { "?X-Plex-Incomplete-Segments=1" } else { "" };
        format!(
            "#EXTM3U\n#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH={},RESOLUTION={width}x{height},FRAME-RATE={rate:.6}\nsession/{session}/base/index.m3u8{suffix}\n",
            self.bitrate() * 1000
        )
    }

    /// The media playlist: every segment of the title, the last one the remainder.
    pub fn media_playlist(&self) -> String {
        let length = self.segment_seconds() as i64;
        let count = self.segment_count();
        // Matroska segments are headerless clusters; the header is its own URI.
        let map = if self.matroska { "#EXT-X-VERSION:7\n#EXT-X-MAP:URI=\"header\"\n" } else { "" };
        let mut out = format!(
            "#EXTM3U\n{map}#EXT-X-TARGETDURATION:{length}\n#EXT-X-ALLOW-CACHE:NO\n#EXT-X-MEDIA-SEQUENCE:0\n"
        );
        for index in 0..count {
            let remaining = (self.duration_ms - index * length * 1000).max(0);
            let seconds = if index + 1 == count {
                ((remaining + 999) / 1000).clamp(1, length)
            } else {
                length
            };
            out.push_str(&format!("#EXTINF:{seconds}, nodesc\n{index:05}.ts\n"));
        }
        out.push_str("#EXT-X-ENDLIST\n");
        out
    }
}

/// HDR to SDR on the device: VA-API frames mapped into OpenCL, tonemapped there (a profile 5
/// RPU applied), and mapped back for the encoder -- Jellyfin's Intel pipeline.
const OPENCL_TONEMAP: &str = "hwmap=derive_device=opencl:mode=read,tonemap_opencl=format=nv12:p=bt709:t=bt709:m=bt709:tonemap=bt2390:peak=100:desat=0,hwmap=derive_device=vaapi:mode=write:reverse=1,format=vaapi";

/// The encoder ladder a session tries in order: the device for decode and encode, the
/// device for encode only, then the CPU.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Path {
    Full,
    EncodeOnly,
    Software,
}

/// A video Stream row that needs tonemapping: PQ or HLG, or any Dolby Vision layer --
/// profile 5 signals no transfer at all (`tests/fixtures/transcode/hdr_probes.json`).
fn hdr_stream(stream: &Value) -> bool {
    let text = |name: &str| stream.get(name).and_then(Value::as_str).unwrap_or_default();
    // zurg before d730b399 pushed no color_transfer at all (go-ffprobe v2.2.0 had no field): 2,677
    // HDR10 rows on rex-fun read 10-bit bt2020nc with colorTrc null and converted grey (2026-10-17).
    let untagged_bt2020 = text("colorTrc").is_empty()
        && stream.get("bitDepth").and_then(Value::as_i64) == Some(10)
        && (text("colorSpace").starts_with("bt2020") || text("colorPrimaries") == "bt2020");
    matches!(text("colorTrc"), "smpte2084" | "arib-std-b67") || untagged_bt2020 || stream.get("DOVIPresent").is_some_and(truthy)
}

/// The decoder key the probe stores: `hevc10` for a 10-bit HEVC, `h264` for H.264.
fn decode_key(codec: &str, bit_depth: i64) -> String {
    let codec = match codec {
        "mpeg2video" | "mpeg2" => "mpeg2video",
        other => other,
    };
    if bit_depth > 8 {
        format!("{codec}10")
    } else {
        codec.to_string()
    }
}

/// The paths worth trying for this plan, best first.
pub(crate) fn ladder(plan: &Plan, hardware: &Hardware) -> Vec<Path> {
    let Some(video) = plan.video.as_ref().filter(|video| !video.copy) else {
        return vec![Path::Software];
    };
    let mut paths = Vec::new();
    if hardware.encode && hardware.accel != Accel::None {
        // HDR stays on the device only where the probe tonemapped a frame through OpenCL;
        // otherwise it is tonemapped on the CPU (`tonemapx`), since a wrong guess is a green
        // picture. A burned image subtitle is overlaid on CPU frames at the source's size, so
        // its decode stays on the CPU; a text one is drawn on the scaled frame, downloaded.
        let tonemaps = !video.hdr || (hardware.opencl_tonemap && matches!(hardware.accel, Accel::Vaapi(_)));
        if tonemaps && plan.subtitle.is_none() && hardware.decode.contains(&decode_key(&video.source_codec, video.bit_depth)) {
            paths.push(Path::Full);
        }
        paths.push(Path::EncodeOnly);
    }
    paths.push(Path::Software);
    paths
}

/// The FFmpeg command line for one run of a session, starting at `start` (a segment).
pub(crate) fn ffmpeg_args(plan: &Plan, path: Path, hardware: &Hardware, start: i64, dir: &std::path::Path) -> Vec<String> {
    let length = plan.segment_seconds() as i64;
    let offset = start * length;
    let mut args: Vec<String> = ["-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-progress", "pipe:1"]
        .iter()
        .map(|arg| arg.to_string())
        .collect();
    let push = |args: &mut Vec<String>, items: &[&str]| args.extend(items.iter().map(|item| item.to_string()));
    let video = plan.video.as_ref();
    let converting = video.is_some_and(|video| !video.copy);
    let device = match (&hardware.accel, path) {
        (Accel::Vaapi(device), Path::Full | Path::EncodeOnly) => Some(device.clone()),
        _ => None,
    };
    let cuda = hardware.accel == Accel::Cuda && matches!(path, Path::Full | Path::EncodeOnly);
    if converting {
        if let Some(device) = &device {
            push(&mut args, &["-init_hw_device", &format!("vaapi=va:{device}")]);
            if path == Path::Full && video.is_some_and(|video| video.hdr) {
                push(&mut args, &["-init_hw_device", "opencl=ocl@va"]);
            }
            push(&mut args, &["-filter_hw_device", "va"]);
            if path == Path::Full {
                push(&mut args, &["-hwaccel", "vaapi", "-hwaccel_device", "va", "-hwaccel_output_format", "vaapi"]);
            }
        } else if cuda {
            push(&mut args, &["-init_hw_device", "cuda=cu:0", "-filter_hw_device", "cu"]);
            if path == Path::Full {
                push(&mut args, &["-hwaccel", "cuda", "-hwaccel_device", "cu", "-hwaccel_output_format", "cuda"]);
            }
        }
    }
    // The source is a remote Range-capable HTTP file: reconnect through a dropped read,
    // and keep the probe small -- the catalog already knows the streams, and every MB
    // probed is a MB waited for before the first segment.
    push(&mut args, &[
        "-reconnect", "1", "-reconnect_on_network_error", "1", "-reconnect_delay_max", "4",
        "-probesize", "4M", "-analyzeduration", "4M", "-fflags", "+genpts",
    ]);
    if hardware.bounded_http {
        push(&mut args, &["-initial_request_size", "1048576", "-request_size", "8388608"]);
    }
    if let Some(auth) = &plan.auth {
        // ponytail: the credential is visible in this host's process list; a loopback
        // relay through rex would hide it, worth it once a source with auth is shared.
        push(&mut args, &["-headers", &format!("Authorization: {auth}\r\n")]);
    }
    push(&mut args, &["-user_agent", super::upstream::UPSTREAM_UA]);
    if offset > 0 {
        push(&mut args, &["-ss", &offset.to_string()]);
        if !converting {
            push(&mut args, &["-noaccurate_seek"]);
        }
    }
    push(&mut args, &["-i", &plan.url]);
    let sidecar_file = plan.sidecar.as_ref().map(|sidecar| dir.join(sidecar.file_name()).to_string_lossy().into_owned());
    if let (Some(file), false) = (&sidecar_file, Sidecar::burned(plan)) {
        // Its own seek, so its events land on the run's clock as the source's packets do.
        if offset > 0 {
            push(&mut args, &["-ss", &offset.to_string()]);
        }
        push(&mut args, &["-i", file]);
    }
    // The text drawn into the picture: an upload, or an embedded track once it has been read.
    let text_file = sidecar_file
        .clone()
        .filter(|_| Sidecar::burned(plan))
        .or_else(|| {
            let file = dir.join(plan.text_burn.as_ref()?.file_name());
            file.exists().then(|| file.to_string_lossy().into_owned())
        })
        .map(|file| filter_path(&file));
    // libass draws by the frame's time, which a seek restarts at 0: shift it to the title's clock.
    let shifted = |subtitles: String| {
        if offset > 0 {
            format!("setpts=PTS+{offset}/TB,{subtitles},setpts=PTS-{offset}/TB")
        } else {
            subtitles
        }
    };
    let burn_file = text_file.as_ref().map(|file| shifted(format!("subtitles=f='{file}'")));
    let canvas = text_file.as_ref().zip(video).filter(|_| path == Path::Full && device.is_some() && hardware.alpha_subtitles && converting).map(|(file, video)| {
        format!(
            "alphasrc=s={}x{}:r=10:start=0,format=yuva420p,{},hwupload",
            video.width,
            video.height,
            shifted(format!("subtitles=f='{file}':alpha=1:sub2video=1"))
        )
    });
    // On the device the text is drawn on the scaled frame: downloaded, drawn, uploaded.
    let drawn = |chain: String, upload: &str| match burn_file.as_ref().filter(|_| canvas.is_none()) {
        Some(subtitles) => format!("{chain},hwdownload,format=nv12,{subtitles},format=nv12,{upload}"),
        None => chain,
    };
    // A row without ffprobe's stream `index` maps the first stream of the kind.
    let video_input = video.map(|video| if video.index >= 0 { format!("0:{}", video.index) } else { "0:v:0".to_string() });
    let burn = plan.subtitle.filter(|_| converting);
    if let (Some(input), None, None) = (&video_input, burn, &canvas) {
        push(&mut args, &["-map", input]);
    }
    let video_filter = video.filter(|video| !video.copy).map(|video| {
        let (w, h) = (video.width, video.height);
        match path {
            Path::Full if device.is_some() && video.hdr => drawn(format!("scale_vaapi=w={w}:h={h}:format=p010,{OPENCL_TONEMAP}"), "hwupload"),
            Path::Full if device.is_some() => drawn(format!("scale_vaapi=w={w}:h={h}:format=nv12"), "hwupload"),
            Path::Full if cuda => drawn(format!("scale_cuda=w={w}:h={h}:format=nv12"), "hwupload_cuda"),
            _ => {
                let mut chain = Vec::new();
                if let Some(subtitles) = &burn_file {
                    chain.push(subtitles.clone());
                }
                if video.hdr && hardware.tonemapx {
                    chain.push("tonemapx=tonemap=bt2390:desat=0:peak=100:t=bt709:m=bt709:p=bt709:format=yuv420p".to_string());
                }
                if device.is_some() {
                    chain.push("format=nv12".to_string());
                    chain.push("hwupload".to_string());
                    chain.push(format!("scale_vaapi=w={w}:h={h}:format=nv12"));
                } else if cuda {
                    chain.push("format=nv12".to_string());
                    chain.push("hwupload_cuda".to_string());
                    chain.push(format!("scale_cuda=w={w}:h={h}:format=nv12"));
                } else {
                    chain.push(format!("scale={w}:{h}:flags=bicubic"));
                    chain.push("format=yuv420p".to_string());
                }
                chain.join(",")
            }
        }
    });
    if let (Some(input), Some(subtitle), Some(filter)) = (&video_input, burn, &video_filter) {
        // The subtitle is drawn at the source's size, before the scale and any upload;
        // FFmpeg turns the bitmap stream into frames for `overlay` itself.
        let graph = format!("[{input}][0:{subtitle}]overlay=eof_action=pass,{filter}[v]");
        push(&mut args, &["-filter_complex", &graph, "-map", "[v]"]);
    }
    if let (Some(input), Some(canvas), Some(filter)) = (&video_input, &canvas, &video_filter) {
        let graph = format!("[{input}]{filter}[main];{canvas}[sub];[main][sub]overlay_vaapi=eof_action=pass:repeatlast=0[v]");
        push(&mut args, &["-filter_complex", &graph, "-map", "[v]"]);
    }
    if let Some(audio) = plan.audio.as_ref() {
        push(&mut args, &["-map", &if audio.index >= 0 { format!("0:{}", audio.index) } else { "0:a:0".to_string() }]);
    }
    let text = plan.text_subtitle.filter(|_| plan.matroska).map(|index| format!("0:{index}"))
        .or_else(|| plan.sidecar.as_ref().filter(|_| plan.matroska).map(|_| "1:0".to_string()));
    if let Some(input) = &text {
        push(&mut args, &["-map", input, "-c:s", if plan.text_to_srt && plan.text_subtitle.is_some() { "srt" } else { "copy" }]);
    }
    match video {
        Some(video) if video.copy => push(&mut args, &["-c:v", "copy"]),
        Some(video) => {
            if burn.is_none() && canvas.is_none() {
                push(&mut args, &["-vf", video_filter.as_deref().unwrap_or_default()]);
            }
            let rate = format!("{}k", video.bitrate);
            let buffer = format!("{}k", video.bitrate * 2);
            let gop = ((video.frame_rate.max(1.0)) * length as f64).round() as i64;
            if device.is_some() && path != Path::Software {
                push(&mut args, &["-c:v", "h264_vaapi", "-rc_mode", "VBR", "-b:v", &rate, "-maxrate", &rate, "-bufsize", &buffer, "-profile:v", "high"]);
            } else if cuda && path != Path::Software {
                push(&mut args, &["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-rc", "vbr", "-b:v", &rate, "-maxrate", &rate, "-bufsize", &buffer, "-profile:v", "high"]);
            } else {
                push(&mut args, &["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", &rate, "-bufsize", &buffer, "-profile:v", "high", "-sc_threshold", "0"]);
            }
            // A keyframe opens every segment; `t` counts from the run's own start.
            push(&mut args, &["-g", &gop.to_string(), "-force_key_frames", &format!("expr:gte(t,n_forced*{length})")]);
        }
        None => {}
    }
    if let Some(audio) = plan.audio.as_ref() {
        if audio.copy {
            push(&mut args, &["-c:a", "copy"]);
        } else {
            push(&mut args, &[
                "-c:a", &audio.codec, "-ac", &audio.channels.to_string(), "-b:a", &format!("{}k", audio.bitrate),
                "-af", "aresample=async=1",
            ]);
        }
    }
    // A converted run's seek decodes from the keyframe before it and drops frames up to it,
    // but a copied audio stream keeps every packet from that keyframe: with x265's ~10 s GOP
    // a restart at 26 s started its AC3 5.3 s early on the title's clock (measured, FFmpeg 8).
    // An output `-ss 0` drops the copied packets before the seek too.
    if converting && offset > 0 && (plan.audio.as_ref().is_some_and(|audio| audio.copy) || text.is_some()) {
        push(&mut args, &["-ss", "0"]);
    }
    if text.is_none() {
        push(&mut args, &["-sn"]);
    }
    // A remux's pre-roll from the keyframe before the seek keeps its negative time, so
    // with the offset every packet lands on its true time on the title's clock.
    push(&mut args, &["-dn", "-map_metadata", "-1", "-map_chapters", "-1", "-avoid_negative_ts", "disabled"]);
    if plan.protocol == Protocol::File {
        push(&mut args, &["-movflags", "+faststart", "-f", "mp4", "-y", &dir.join("media.mp4").to_string_lossy()]);
        return args;
    }
    if plan.protocol == Protocol::Dash {
        // One fragmented MP4 per representation, `init-<rep>.m4s` and `chunk-<rep>-<n>.m4s`
        // numbered from 1 in the run's own directory. The MP4 muxer rebases every
        // fragment to zero unless told the fragments are discontinuous; with
        // `frag_discont` and the offset, `tfdt` is the title's clock (measured on the
        // 1.42.2 reference: a run from 600 s writes tfdt 14 400 000 at timescale 24 000).
        if offset > 0 {
            push(&mut args, &["-output_ts_offset", &offset.to_string()]);
        }
        // AAC's priming (and a copied stream's B-frame dts) starts a title below zero, and
        // `frag_discont` writes that as a wrapped 64-bit `tfdt` (measured: 4.2e14 s on the
        // first audio fragment). Shift a run that would go negative up to zero instead.
        let position = args.iter().rposition(|arg| arg == "-avoid_negative_ts").map(|at| at + 1);
        if let Some(position) = position {
            args[position] = "make_non_negative".to_string();
        }
        let sets = match (plan.video.is_some(), plan.audio.is_some()) {
            (true, true) => "id=0,streams=v id=1,streams=a",
            (true, false) => "id=0,streams=v",
            _ => "id=0,streams=a",
        };
        // FFmpeg's DASH muxer finds the manifest's folder by its last '/', so with a
        // Windows path it wrote every segment into the working directory instead and each
        // segment request failed (measured with FFmpeg 9.0.2 on Windows 11). Windows takes
        // '/' as a separator, so hand it one.
        let manifest = dir.join(format!("r{start}")).join("manifest.mpd").to_string_lossy().into_owned();
        let manifest = if cfg!(windows) { manifest.replace('\\', "/") } else { manifest };
        push(&mut args, &[
            "-f", "dash", "-format_options", "movflags=+frag_discont", "-seg_duration", &length.to_string(),
            "-use_template", "1", "-use_timeline", "0", "-single_file", "0", "-window_size", "0",
            "-remove_at_exit", "0", "-dash_segment_type", "mp4", "-adaptation_sets", sets,
            "-init_seg_name", "init-$RepresentationID$.m4s",
            "-media_seg_name", "chunk-$RepresentationID$-$Number%05d$.m4s",
            &manifest,
        ]);
        return args;
    }
    if plan.matroska {
        // One header for every run: a restart's differs only in its random segment and track
        // UIDs, and clusters name tracks by number (measured, FFmpeg 8.1).
        let header = dir.join("init-0.m4s").to_string_lossy().into_owned();
        push(&mut args, &["-f", "segment", "-segment_format", "matroska", "-segment_header_filename", &header]);
    } else {
        push(&mut args, &["-max_delay", "5000000", "-f", "segment", "-segment_format", "mpegts"]);
    }
    push(&mut args, &["-segment_start_number", &start.to_string(), "-segment_time", &length.to_string()]);
    if offset > 0 {
        push(&mut args, &["-initial_offset", &offset.to_string()]);
    }
    // No `-segment_list`: FFmpeg buffers it and writes it only as the run ends, so it
    // cannot say which segment is finished. The next file opening says it (`watch`).
    push(&mut args, &[
        "-segment_time_delta", "0.0625", "-individual_header_trailer", "0",
        &dir.join("%05d.ts").to_string_lossy(),
    ]);
    args
}

#[derive(Debug, Default)]
struct Progress {
    /// Finished segments: index -> (start, end) seconds on the title's clock, and the
    /// run (its first segment) whose files hold it.
    done: BTreeMap<i64, (f64, f64, i64)>,
    speed: f64,
    out_seconds: f64,
    paused: bool,
    exited: Option<i32>,
    finished: bool,
    /// The run's first segment, and the path it is on.
    run_start: i64,
    path: Option<Path>,
    error: bool,
    last_request: Option<i64>,
    /// Counts segment requests; only the newest may move FFmpeg (`segment`).
    requests: u64,
    /// The request whose seek claimed the latest restart.
    seek: u64,
    /// Input failures (the source answering 5xx, a dropped connection) retried so far.
    input_retries: u32,
    touched: Option<Instant>,
}

pub(crate) struct Session {
    pub id: String,
    pub plan: Plan,
    dir: PathBuf,
    state: Mutex<Progress>,
    child: Mutex<Option<Child>>,
    /// Held across a seek's launch, so two seeks' launches cannot cross (`segment`).
    launching: Mutex<()>,
    run: std::sync::atomic::AtomicU64,
    stopped: AtomicBool,
    pub started: f64,
}

impl Session {
    fn touch(&self) {
        if let Ok(mut state) = self.state.lock() {
            state.touched = Some(Instant::now());
        }
    }

    fn kill_child(&self) {
        if let Ok(mut child) = self.child.lock() {
            if let Some(mut process) = child.take() {
                // A paused process ignores SIGKILL until it is continued on some kernels'
                // job control; continue it first.
                crate::platform::resume(process.id());
                let _ = process.kill();
                let _ = process.wait();
            }
        }
    }

    /// Pause (`true`) or resume the FFmpeg, through `platform`: SIGSTOP and SIGCONT on
    /// Unix, a whole-process suspend and resume on Windows.
    fn set_paused(&self, paused: bool) {
        if let Ok(child) = self.child.lock() {
            if let Some(process) = child.as_ref() {
                if paused {
                    crate::platform::pause(process.id());
                } else {
                    crate::platform::resume(process.id());
                }
            }
        }
    }

    pub fn throttled(&self) -> bool {
        self.state.lock().map(|state| state.paused).unwrap_or(false)
    }

    pub fn errored(&self) -> bool {
        self.state.lock().map(|state| state.error).unwrap_or(false)
    }

    /// `maxOffsetAvailable`/`minOffsetAvailable`: the produced window in seconds.
    pub fn window(&self) -> (f64, f64) {
        self.state
            .lock()
            .map(|state| {
                let min = state.done.values().next().map_or(0.0, |range| range.0);
                let max = state.done.values().next_back().map_or(0.0, |range| range.1);
                (min.max(0.0), max.max(0.0))
            })
            .unwrap_or((0.0, 0.0))
    }

    pub fn speed(&self) -> f64 {
        self.state.lock().map(|state| state.speed).unwrap_or(0.0)
    }

    pub fn finished(&self) -> bool {
        self.state.lock().map(|state| state.finished).unwrap_or(false)
    }

    pub fn path(&self) -> Option<Path> {
        self.state.lock().ok().and_then(|state| state.path)
    }

    /// Where a run writes: HLS's flat `%05d.ts`, DASH's `r<run>/` directory.
    fn run_dir(&self, run: i64) -> PathBuf {
        match self.plan.protocol {
            Protocol::Hls | Protocol::File => self.dir.clone(),
            Protocol::Dash => self.dir.join(format!("r{run}")),
        }
    }

    /// The file holding segment `index` of representation `rep` as run `run` wrote it.
    fn segment_file(&self, index: i64, rep: usize, run: i64) -> PathBuf {
        match self.plan.protocol {
            Protocol::Hls | Protocol::File => self.dir.join(format!("{index:05}.ts")),
            Protocol::Dash => self.run_dir(run).join(format!("chunk-{rep}-{:05}.m4s", index - run + 1)),
        }
    }

    fn init_file(&self, rep: usize, run: i64) -> PathBuf {
        self.run_dir(run).join(format!("init-{rep}.m4s"))
    }

    /// Whether every representation of segment `index` has been opened by run `run`.
    fn opened(&self, index: i64, run: i64) -> bool {
        (0..self.plan.representations().max(1)).all(|rep| self.segment_file(index, rep, run).exists())
    }

    fn remove_segment(&self, index: i64, run: i64) {
        for rep in 0..self.plan.representations().max(1) {
            let _ = std::fs::remove_file(self.segment_file(index, rep, run));
        }
    }
}

pub(crate) struct Transcoder {
    ffmpeg: PathBuf,
    root: PathBuf,
    hardware: Mutex<Hardware>,
    sessions: Mutex<HashMap<String, Arc<Session>>>,
}

impl std::fmt::Debug for Transcoder {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("Transcoder").field("ffmpeg", &self.ffmpeg).finish()
    }
}

/// The segment a request asked for, served once it exists, outside the runtime lock.
pub(crate) struct SegmentPlan {
    pub session: Arc<Session>,
    pub index: i64,
    /// The DASH representation; 0 for HLS.
    pub rep: usize,
    /// A DASH `header` rather than a numbered segment.
    pub header: bool,
    pub transcoder: Arc<Transcoder>,
}

impl Transcoder {
    /// `REX_TRANSCODER` (an FFmpeg binary), `REX_TRANSCODE_DIR` (default: the system temp
    /// directory's `rex-transcode-<port>`, one per server, since a start clears it),
    /// `REX_TRANSCODE_HW` (`auto`, `vaapi`, `cuda`, `none`) and `REX_TRANSCODE_DEVICE`
    /// (default `/dev/dri/renderD128`).
    pub fn from_env(port: u16) -> Option<Arc<Self>> {
        let ffmpeg = std::env::var("REX_TRANSCODER").ok().filter(|path| !path.is_empty())?;
        let root = std::env::var("REX_TRANSCODE_DIR")
            .ok()
            .filter(|path| !path.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| std::env::temp_dir().join(format!("rex-transcode-{port}")));
        // Sessions do not survive a restart; neither do their segments or their FFmpegs.
        kill_leftovers(&root);
        let _ = std::fs::remove_dir_all(&root);
        if let Err(error) = std::fs::create_dir_all(&root) {
            error!("transcoder off: cannot create {}: {error}", root.display());
            return None;
        }
        let transcoder = Arc::new(Self {
            ffmpeg: PathBuf::from(ffmpeg),
            root,
            hardware: Mutex::new(Hardware::software()),
            sessions: Mutex::new(HashMap::new()),
        });
        let probing = Arc::clone(&transcoder);
        thread::spawn(move || {
            let found = probing.probe();
            info!(
                "transcoder: {} ({}), hardware {} encode={} tonemapx={} opencl_tonemap={} bounded_http={} alpha_subtitles={} decode=[{}]",
                probing.ffmpeg.display(),
                probing.root.display(),
                found.accel.name(),
                found.encode,
                found.tonemapx,
                found.opencl_tonemap,
                found.bounded_http,
                found.alpha_subtitles,
                {
                    let mut names = found.decode.iter().cloned().collect::<Vec<_>>();
                    names.sort();
                    names.join(",")
                }
            );
            if let Ok(mut hardware) = probing.hardware.lock() {
                *hardware = found;
            }
        });
        let reaping = Arc::clone(&transcoder);
        thread::spawn(move || loop {
            thread::sleep(Duration::from_secs(5));
            reaping.reap();
        });
        Some(transcoder)
    }

    pub fn hardware(&self) -> Hardware {
        self.hardware.lock().map(|hardware| hardware.clone()).unwrap_or_else(|_| Hardware::software())
    }

    fn quiet(&self, args: &[String]) -> bool {
        Command::new(&self.ffmpeg)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
    }

    /// Which device answers, and what it decodes: a frame encoded on it, and for each
    /// codec a clip encoded on the CPU decoded on it into device memory -- a decoder the
    /// device lacks makes FFmpeg fall back to software, and `hwdownload` then refuses
    /// the software frame, so a pass is a real hardware decode.
    fn probe(&self) -> Hardware {
        let mut found = self.probe_device();
        let filters = Command::new(&self.ffmpeg).args(["-hide_banner", "-filters"]).stdin(Stdio::null()).stderr(Stdio::null()).output();
        let filters = filters.map(|output| String::from_utf8_lossy(&output.stdout).to_string()).unwrap_or_default();
        found.tonemapx = filters.contains(" tonemapx ");
        // A frame tagged PQ tonemapped through OpenCL derived from the device: an OpenCL
        // runtime missing or refusing the device fails here, not in a viewer's session.
        if let (Accel::Vaapi(device), true) = (&found.accel, filters.contains(" tonemap_opencl ")) {
            let chain = format!("setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc,format=p010,hwupload,{OPENCL_TONEMAP}");
            found.opencl_tonemap = self.quiet(&[
                "-v", "error", "-init_hw_device", &format!("vaapi=va:{device}"), "-init_hw_device", "opencl=ocl@va",
                "-filter_hw_device", "va", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=10", "-frames:v", "5",
                "-vf", &chain, "-c:v", "h264_vaapi", "-f", "null", "-",
            ].map(str::to_string));
        }
        // A subtitle drawn on a canvas and laid over a frame on the device, end to end.
        if let (Accel::Vaapi(device), true) = (&found.accel, filters.contains(" alphasrc ") && filters.contains(" overlay_vaapi ")) {
            let srt = self.root.join("probe.srt");
            if std::fs::write(&srt, "1\n00:00:00,000 --> 00:00:01,000\nrex\n").is_ok() {
                let graph = format!(
                    "[0:v]format=nv12,hwupload[main];alphasrc=s=320x240:r=10:start=0,format=yuva420p,subtitles=f='{}':alpha=1:sub2video=1,hwupload[sub];[main][sub]overlay_vaapi[v]",
                    filter_path(&srt.to_string_lossy())
                );
                found.alpha_subtitles = self.quiet(&[
                    "-v", "error", "-init_hw_device", &format!("vaapi=va:{device}"), "-filter_hw_device", "va",
                    "-f", "lavfi", "-i", "testsrc2=s=320x240:r=10", "-frames:v", "5", "-filter_complex", &graph,
                    "-map", "[v]", "-c:v", "h264_vaapi", "-f", "null", "-",
                ].map(str::to_string));
                let _ = std::fs::remove_file(&srt);
            }
        }
        // An FFmpeg too old for the option refuses to start with it, so ask first.
        found.bounded_http = cfg!(windows) && {
            let help = Command::new(&self.ffmpeg).args(["-hide_banner", "-h", "protocol=http"]).stdin(Stdio::null()).stderr(Stdio::null()).output();
            help.is_ok_and(|output| String::from_utf8_lossy(&output.stdout).contains("-request_size"))
        };
        found
    }

    fn probe_device(&self) -> Hardware {
        let wanted = std::env::var("REX_TRANSCODE_HW").unwrap_or_else(|_| "auto".to_string());
        let device = std::env::var("REX_TRANSCODE_DEVICE").unwrap_or_else(|_| "/dev/dri/renderD128".to_string());
        let strings = |items: &[&str]| items.iter().map(|item| item.to_string()).collect::<Vec<_>>();
        let mut candidates = Vec::new();
        if matches!(wanted.as_str(), "auto" | "vaapi") {
            candidates.push(Accel::Vaapi(device.clone()));
        }
        if matches!(wanted.as_str(), "auto" | "cuda" | "nvenc") {
            candidates.push(Accel::Cuda);
        }
        for accel in candidates {
            let (init, upload, encoder, hwaccel, out_format) = match &accel {
                Accel::Vaapi(device) => (format!("vaapi=hw:{device}"), "format=nv12,hwupload", "h264_vaapi", "vaapi", "vaapi"),
                Accel::Cuda => ("cuda=hw:0".to_string(), "format=nv12,hwupload_cuda", "h264_nvenc", "cuda", "cuda"),
                Accel::None => continue,
            };
            let mut encode = strings(&["-v", "error", "-init_hw_device"]);
            encode.push(init.clone());
            encode.extend(strings(&["-filter_hw_device", "hw", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=10", "-frames:v", "5", "-vf", upload, "-c:v", encoder, "-f", "null", "-"]));
            if !self.quiet(&encode) {
                continue;
            }
            let mut decode = HashSet::new();
            let samples: [(&str, &[&str]); 7] = [
                ("h264", &["-c:v", "libx264", "-pix_fmt", "yuv420p"]),
                ("h26410", &["-c:v", "libx264", "-pix_fmt", "yuv420p10le", "-profile:v", "high10"]),
                ("hevc", &["-c:v", "libx265", "-pix_fmt", "yuv420p", "-x265-params", "log-level=none"]),
                ("hevc10", &["-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-x265-params", "log-level=none"]),
                ("vp9", &["-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", "-deadline", "realtime"]),
                ("av1", &["-c:v", "libsvtav1", "-pix_fmt", "yuv420p10le"]),
                ("mpeg2video", &["-c:v", "mpeg2video", "-pix_fmt", "yuv420p"]),
            ];
            for (key, codec) in samples {
                let sample = self.root.join(format!("probe-{key}.mkv"));
                let mut make = strings(&["-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=10", "-frames:v", "10"]);
                make.extend(strings(codec));
                make.push(sample.to_string_lossy().to_string());
                if !self.quiet(&make) {
                    continue;
                }
                let download = if key.ends_with("10") || key == "av1" { "hwdownload,format=p010le" } else { "hwdownload,format=nv12" };
                let mut read = strings(&["-v", "error", "-init_hw_device"]);
                read.push(init.clone());
                read.extend(strings(&["-hwaccel", hwaccel, "-hwaccel_device", "hw", "-hwaccel_output_format", out_format, "-i"]));
                read.push(sample.to_string_lossy().to_string());
                read.extend(strings(&["-vf", download, "-f", "null", "-"]));
                if self.quiet(&read) {
                    // An AV1 sample is 10-bit; a device that decodes it decodes 8-bit too.
                    decode.insert(key.to_string());
                    if key == "av1" {
                        decode.insert("av110".to_string());
                    }
                }
                let _ = std::fs::remove_file(&sample);
            }
            return Hardware { accel, decode, encode: true, tonemapx: false, opencl_tonemap: false, bounded_http: false, alpha_subtitles: false };
        }
        Hardware::software()
    }

    pub fn session(&self, id: &str) -> Option<Arc<Session>> {
        self.sessions.lock().ok()?.get(id).cloned()
    }

    pub fn sessions(&self) -> Vec<Arc<Session>> {
        let mut sessions = self
            .sessions
            .lock()
            .map(|sessions| sessions.values().cloned().collect::<Vec<_>>())
            .unwrap_or_default();
        sessions.sort_by(|a, b| a.started.total_cmp(&b.started));
        sessions
    }

    /// The live conversion a playback session is watching, if any.
    pub fn for_playback(&self, playback: &str) -> Option<Arc<Session>> {
        self.sessions()
            .into_iter()
            .rev()
            .find(|session| session.plan.playback == playback && !session.stopped.load(Ordering::SeqCst))
    }

    /// `start.m3u8`: a fresh session under the client's `session` id (a previous one of
    /// the same id is replaced), already running from segment 0.
    pub fn start(self: &Arc<Self>, id: &str, plan: Plan) -> Result<Arc<Session>, String> {
        self.stop(id);
        let dir = self.root.join(sanitize(id));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).map_err(|error| format!("cannot create {}: {error}", dir.display()))?;
        let session = Arc::new(Session {
            id: id.to_string(),
            plan,
            dir,
            state: Mutex::new(Progress { touched: Some(Instant::now()), ..Progress::default() }),
            child: Mutex::new(None),
            launching: Mutex::new(()),
            run: std::sync::atomic::AtomicU64::new(0),
            stopped: AtomicBool::new(false),
            started: now_seconds(),
        });
        self.sessions
            .lock()
            .map_err(|_| "transcoder lock poisoned".to_string())?
            .insert(id.to_string(), Arc::clone(&session));
        match session.plan.text_burn.clone() {
            Some(text) => {
                let transcoder = Arc::clone(self);
                let reading = Arc::clone(&session);
                thread::spawn(move || transcoder.read_text(&reading, &text));
            }
            None => self.launch(&session, 0, None),
        }
        Ok(session)
    }

    /// A download's conversion: one FFmpeg writing `<root>/download-<id>/media.mp4` to the end,
    /// unthrottled and never reaped, down the hardware ladder until a path finishes. `progress`
    /// gets FFmpeg's output seconds, bytes written and speed; `cancel` kills it. Blocks.
    pub fn convert_file(&self, id: i64, plan: &Plan, cancel: &AtomicBool, progress: impl Fn(f64, u64, f64)) -> Result<PathBuf, String> {
        let dir = self.root.join(format!("download-{id}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).map_err(|error| format!("cannot create {}: {error}", dir.display()))?;
        let hardware = self.hardware();
        let mut failure = "no conversion path".to_string();
        for path in ladder(plan, &hardware) {
            let mut command = Command::new(&self.ffmpeg);
            command.args(ffmpeg_args(plan, path, &hardware, 0, &dir)).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
            let mut child = spawn_tied(command).map_err(|error| format!("cannot start {}: {error}", self.ffmpeg.display()))?;
            let (mut seconds, mut bytes, mut speed) = (0.0, 0, 0.0);
            for line in child.stdout.take().map(BufReader::new).into_iter().flat_map(|out| out.lines().map_while(Result::ok)) {
                if cancel.load(Ordering::SeqCst) {
                    let _ = child.kill();
                    break;
                }
                if let Some(value) = line.strip_prefix("out_time_us=") {
                    seconds = value.trim().parse::<f64>().map_or(seconds, |micros| micros / 1_000_000.0);
                } else if let Some(value) = line.strip_prefix("total_size=") {
                    bytes = value.trim().parse().unwrap_or(bytes);
                } else if let Some(value) = line.strip_prefix("speed=") {
                    speed = value.trim().trim_end_matches('x').parse().unwrap_or(speed);
                } else if line.starts_with("progress=") {
                    progress(seconds, bytes, speed);
                }
            }
            let status = child.wait().map_err(|error| error.to_string())?;
            if cancel.load(Ordering::SeqCst) {
                let _ = std::fs::remove_dir_all(&dir);
                return Err("cancelled".to_string());
            }
            if status.success() {
                return Ok(dir.join("media.mp4"));
            }
            failure = format!("{path:?} exited {status}");
            error!("DOWNLOAD {id}: {failure}");
        }
        Err(failure)
    }

    /// `stop` / `DELETE /transcode/sessions/{id}`: whether there was such a session.
    pub fn stop(&self, id: &str) -> bool {
        let removed = self.sessions.lock().ok().and_then(|mut sessions| sessions.remove(id));
        match removed {
            Some(session) => {
                session.stopped.store(true, Ordering::SeqCst);
                session.kill_child();
                let _ = std::fs::remove_dir_all(&session.dir);
                info!("TRANSCODE {id} stopped");
                true
            }
            None => false,
        }
    }

    pub fn stop_playback(&self, playback: &str) {
        for session in self.sessions() {
            if session.plan.playback == playback {
                self.stop(&session.id);
            }
        }
    }

    pub fn ping(&self, id: &str) -> bool {
        self.session(id).map(|session| session.touch()).is_some()
    }

    fn reap(&self) {
        for session in self.sessions() {
            let idle = session
                .state
                .lock()
                .ok()
                .and_then(|state| state.touched)
                .is_none_or(|touched| touched.elapsed() > IDLE);
            if idle {
                info!("TRANSCODE {} idle, killed", session.id);
                self.stop(&session.id);
            }
        }
    }

    /// Read the burned embedded subtitle out of its release beside the run's segments, the
    /// minutes from the player's position first; the run starts on those and restarts as more
    /// is read (`SUBTITLE_WINDOWS_MS`). A track already read this start is reused. One that cannot be read (no
    /// Cues for it, a source refusing ranges) leaves the conversion without it, as before.
    fn read_text(self: &Arc<Self>, session: &Arc<Session>, text: &TextBurn) {
        let file = session.dir.join(text.file_name());
        let digest = sha1::Sha1::digest(format!("{}#{}", session.plan.url, text.index).as_bytes());
        let cached = self.root.join("subtitles").join(format!("{}.{}", digest.iter().map(|byte| format!("{byte:02x}")).collect::<String>(), if text.ass { "ass" } else { "srt" }));
        if std::fs::copy(&cached, &file).is_ok() {
            self.relaunch(session);
            return;
        }
        let plan = &session.plan;
        let bucket = Mutex::new((SUBTITLE_BURST, Instant::now()));
        let read = |offset: u64, length: usize| {
            loop {
                if session.stopped.load(Ordering::SeqCst) {
                    return Err("the session stopped".to_string());
                }
                let wait = {
                    let mut bucket = bucket.lock().unwrap_or_else(|poison| poison.into_inner());
                    bucket.0 = (bucket.0 + bucket.1.elapsed().as_secs_f64() * SUBTITLE_RATE).min(SUBTITLE_BURST);
                    bucket.1 = Instant::now();
                    if bucket.0 >= 1.0 {
                        bucket.0 -= 1.0;
                        break;
                    }
                    (1.0 - bucket.0) / SUBTITLE_RATE
                };
                thread::sleep(Duration::from_secs_f64(wait));
            }
            read_range(&plan.url, plan.auth.as_deref(), offset, length)
        };
        let begun = Instant::now();
        let index = match super::mkvsubs::index(&read, text.index) {
            Ok(index) => index,
            Err(error) => {
                warning!("TRANSCODE {} cannot read subtitle stream {}, converting without it: {error}", session.id, text.index);
                self.relaunch(session);
                return;
            }
        };
        let from = session.state.lock().map(|state| state.run_start).unwrap_or(0) * plan.segment_seconds() as i64 * 1000;
        let mut stages = vec![Vec::new(); SUBTITLE_WINDOWS_MS.len() + 1];
        for cue in &index.cues {
            let stage = SUBTITLE_WINDOWS_MS.iter().position(|window| cue.start >= from && cue.start < from + window);
            stages[stage.unwrap_or(SUBTITLE_WINDOWS_MS.len())].push(cue.clone());
        }
        let (mut events, mut failed, mut script) = (Vec::new(), 0, String::new());
        for (stage, cues) in stages.iter().enumerate() {
            // The first stage starts the run even when it holds no line.
            if stage > 0 && cues.is_empty() {
                continue;
            }
            let (more, more_failed, refused) = index.events(&read, cues);
            events.extend(more);
            events.sort_by_key(|event| event.start);
            failed += more_failed;
            if session.stopped.load(Ordering::SeqCst) {
                return;
            }
            script = index.render(&events);
            if let Err(error) = std::fs::write(&file, &script) {
                warning!("TRANSCODE {} cannot write its subtitle: {error}", session.id);
            }
            info!("TRANSCODE {} subtitle stream {}: {} of {} cues from {}s read in {:.1}s", session.id, text.index, events.len(), index.cues.len(), from / 1000, begun.elapsed().as_secs_f64());
            self.relaunch(session);
            if refused {
                warning!("TRANSCODE {} subtitle stream {}: the source refused a read, carrying on with {} lines", session.id, text.index, events.len());
                return;
            }
        }
        if failed == 0 && std::fs::create_dir_all(self.root.join("subtitles")).is_ok() {
            let _ = std::fs::write(&cached, &script);
        }
        info!("TRANSCODE {} subtitle stream {}: {} cues read, {failed} unreadable, in {:.1}s", session.id, text.index, events.len(), begun.elapsed().as_secs_f64());
    }

    /// Restart the run from the first segment it has not finished, on its own path, so it
    /// reads the subtitle file as it now is; with no run yet, the first one.
    fn relaunch(self: &Arc<Self>, session: &Arc<Session>) {
        if session.stopped.load(Ordering::SeqCst) {
            return;
        }
        let _launching = session.launching.lock();
        let Ok((resume, path, finished)) = session.state.lock().map(|state| {
            let resume = state.done.range(state.run_start..).next_back().map_or(state.run_start, |(key, _)| key + 1);
            (resume, state.path, state.finished)
        }) else {
            return;
        };
        match path {
            None => self.launch(session, resume, None),
            Some(_) if finished => {}
            Some(path) => self.launch_on(session, resume, path),
        }
    }

    /// Start (or restart) ffmpeg at `start`, on the best path not yet failed.
    fn launch(self: &Arc<Self>, session: &Arc<Session>, start: i64, after: Option<Path>) {
        self.launch_with(session, start, after, None);
    }

    /// Restart on the same path, after the source failed the read.
    fn launch_on(self: &Arc<Self>, session: &Arc<Session>, start: i64, path: Path) {
        self.launch_with(session, start, None, Some(path));
    }

    fn launch_with(self: &Arc<Self>, session: &Arc<Session>, start: i64, after: Option<Path>, same: Option<Path>) {
        session.kill_child();
        let hardware = self.hardware();
        let paths = ladder(&session.plan, &hardware);
        let path = match (same, after) {
            (Some(path), _) => path,
            (None, after) => match after {
            None => paths[0],
            Some(failed) => match paths.iter().position(|path| *path == failed) {
                Some(position) if position + 1 < paths.len() => paths[position + 1],
                _ => {
                    if let Ok(mut state) = session.state.lock() {
                        state.error = true;
                    }
                    error!("TRANSCODE {} failed on every path", session.id);
                    return;
                }
            },
            },
        };
        let args = ffmpeg_args(&session.plan, path, &hardware, start, &session.dir);
        if let Some(sidecar) = &session.plan.sidecar {
            if let Err(err) = std::fs::write(session.dir.join(sidecar.file_name()), &sidecar.content) {
                warning!("TRANSCODE {} cannot write its subtitle: {err}", session.id);
            }
        }
        let run = session.run.fetch_add(1, Ordering::SeqCst) + 1;
        debug!(
            "TRANSCODE {} run {run}: {}",
            session.id,
            args.iter()
                .map(|arg| if arg.starts_with("Authorization:") { "Authorization: <redacted>".to_string() } else { arg.clone() })
                .collect::<Vec<_>>()
                .join(" ")
        );
        if let Ok(mut state) = session.state.lock() {
            state.run_start = start;
            state.path = Some(path);
            state.paused = false;
            state.finished = false;
            state.exited = None;
            state.error = false;
            state.speed = 0.0;
            // A restart's segments replace whatever the earlier run left from `start` on,
            // and the segment a killed run was still writing is not a segment.
            state.done.retain(|index, _| *index < start);
            match session.plan.protocol {
                Protocol::Hls | Protocol::File => {
                    if let Ok(entries) = std::fs::read_dir(&session.dir) {
                        for entry in entries.flatten() {
                            let name = entry.file_name().to_string_lossy().to_string();
                            let index = name.strip_suffix(".ts").and_then(|stem| stem.parse::<i64>().ok());
                            if index.is_some_and(|index| !state.done.contains_key(&index)) {
                                let _ = std::fs::remove_file(entry.path());
                            }
                        }
                    }
                }
                Protocol::Dash => {
                    let fresh = session.run_dir(start);
                    let _ = std::fs::remove_dir_all(&fresh);
                    let _ = std::fs::create_dir_all(&fresh);
                }
            }
        }
        let mut command = Command::new(&self.ffmpeg);
        command.args(&args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        let mut child = match spawn_tied(command) {
            Ok(child) => child,
            Err(error) => {
                error!("TRANSCODE {} cannot start {}: {error}", session.id, self.ffmpeg.display());
                if let Ok(mut state) = session.state.lock() {
                    state.error = true;
                }
                return;
            }
        };
        info!(
            "TRANSCODE {} run {run} from segment {start} on {:?} ({})",
            session.id,
            path,
            hardware.accel.name()
        );
        let stdout = child.stdout.take();
        let stderr = child.stderr.take();
        if let Ok(mut slot) = session.child.lock() {
            // Two restarts racing each kill the old child and then store their own; the
            // one stored first must not be left running unowned.
            if let Some(mut older) = slot.replace(child) {
                crate::platform::resume(older.id());
                let _ = older.kill();
                let _ = older.wait();
            }
        }
        if let Some(stdout) = stdout {
            let watched = Arc::clone(session);
            thread::spawn(move || {
                for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                    if watched.run.load(Ordering::SeqCst) != run {
                        break;
                    }
                    if let Some(speed) = line.strip_prefix("speed=") {
                        if let Ok(speed) = speed.trim().trim_end_matches('x').parse::<f64>() {
                            if let Ok(mut state) = watched.state.lock() {
                                state.speed = speed;
                            }
                        }
                    } else if let Some(time) = line.strip_prefix("out_time_us=") {
                        if let Ok(micros) = time.trim().parse::<i64>() {
                            if let Ok(mut state) = watched.state.lock() {
                                state.out_seconds = micros as f64 / 1_000_000.0;
                            }
                        }
                    }
                }
            });
        }
        let errors = Arc::new(Mutex::new(String::new()));
        if let Some(stderr) = stderr {
            let errors = Arc::clone(&errors);
            thread::spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    if let Ok(mut text) = errors.lock() {
                        if text.len() < 4000 {
                            text.push_str(&line);
                            text.push('\n');
                        }
                    }
                }
            });
        }
        let transcoder = Arc::clone(self);
        let watched = Arc::clone(session);
        thread::spawn(move || transcoder.watch(watched, run, start, path, errors));
    }

    /// One run's monitor: notes each segment as the next one opens, throttles, and on an
    /// early failure moves the session down the ladder.
    fn watch(self: Arc<Self>, session: Arc<Session>, run: u64, start: i64, path: Path, errors: Arc<Mutex<String>>) {
        let length = session.plan.segment_seconds() as f64;
        let duration = session.plan.duration_ms as f64 / 1000.0;
        let span = |index: i64| (index as f64 * length, ((index + 1) as f64 * length).min(duration.max(length)), start);
        // The segment FFmpeg is writing: finished once the one after it opens.
        let mut next = start;
        loop {
            thread::sleep(Duration::from_millis(30));
            if session.stopped.load(Ordering::SeqCst) || session.run.load(Ordering::SeqCst) != run {
                return;
            }
            while session.opened(next + 1, start) {
                if let Ok(mut state) = session.state.lock() {
                    state.done.insert(next, span(next));
                    state.input_retries = 0;
                }
                next += 1;
            }
            let exited = session
                .child
                .lock()
                .ok()
                .and_then(|mut child| child.as_mut().and_then(|process| process.try_wait().ok().flatten()))
                .map(|status| status.code().unwrap_or(-1));
            if let Some(code) = exited {
                if code == 0 {
                    if let Ok(mut state) = session.state.lock() {
                        if session.opened(next, start) {
                            state.done.insert(next, span(next));
                        }
                        state.exited = Some(0);
                        state.finished = true;
                    }
                    info!("TRANSCODE {} run {run} finished at segment {next}", session.id);
                    return;
                }
                let produced = next > start;
                let detail = errors.lock().map(|text| text.trim().to_string()).unwrap_or_default();
                error!("TRANSCODE {} run {run} on {path:?} exited {code}: {detail}", session.id);
                if let Ok(mut state) = session.state.lock() {
                    state.exited = Some(code);
                }
                // The source refusing or dropping the read says nothing about the encoder:
                // retry the same path after a pause instead of falling down the ladder.
                let input = detail.contains("Error opening input") || detail.contains("Server returned") || detail.contains("Connection reset");
                let retry = input
                    && session.state.lock().map(|mut state| {
                        state.input_retries += 1;
                        state.input_retries <= 3
                    }).unwrap_or(false);
                if retry {
                    thread::sleep(Duration::from_secs(1));
                    if session.run.load(Ordering::SeqCst) == run && !session.stopped.load(Ordering::SeqCst) {
                        let resume = session.state.lock().map(|state| state.done.range(start..).next_back().map_or(start, |(key, _)| key + 1)).unwrap_or(start);
                        self.launch_on(&session, resume, path);
                    }
                } else if !produced {
                    // Nothing came out of this path; the next one down may do better.
                    self.launch(&session, start, Some(path));
                } else if let Ok(mut state) = session.state.lock() {
                    state.error = true;
                }
                return;
            }
            // Throttle on how far production runs ahead of the player.
            let (ahead, paused) = match session.state.lock() {
                Ok(state) => {
                    let player = state.last_request.unwrap_or(start) as f64 * length;
                    let produced = state.done.values().next_back().map_or(start as f64 * length, |range| range.1);
                    (produced - player, state.paused)
                }
                Err(_) => continue,
            };
            if !paused && ahead > THROTTLE_AHEAD {
                session.set_paused(true);
                if let Ok(mut state) = session.state.lock() {
                    state.paused = true;
                }
            } else if paused && ahead < RESUME_AHEAD {
                session.set_paused(false);
                if let Ok(mut state) = session.state.lock() {
                    state.paused = false;
                }
            }
        }
    }

    /// The bytes of segment `index` (of DASH representation `rep`), waiting for ffmpeg to
    /// finish it; a request far from what is being produced restarts ffmpeg there. A DASH
    /// segment is served as the reference serves it: its run's init, then the fragment.
    pub fn segment(self: &Arc<Self>, session: &Arc<Session>, index: i64, rep: usize) -> Result<Vec<u8>, u16> {
        if index < 0 || index >= session.plan.segment_count() || rep >= session.plan.representations().max(1) {
            return Err(404);
        }
        session.touch();
        let length = session.plan.segment_seconds() as f64;
        let deadline = Instant::now() + Duration::from_secs(45);
        let ticket = match session.state.lock() {
            Ok(mut state) => {
                state.requests += 1;
                state.last_request = Some(index);
                state.requests
            }
            Err(_) => return Err(500),
        };
        let mut restarted = false;
        loop {
            let mut restart = false;
            let (ready, error) = match session.state.lock() {
                Ok(mut state) => {
                    // Old segments behind the player go, so a long play keeps a bounded
                    // footprint on disk.
                    let floor = (index as f64 - KEEP_BEHIND / length).floor() as i64;
                    let stale = state.done.range(..floor).map(|(key, range)| (*key, range.2)).collect::<Vec<_>>();
                    for (key, run) in stale {
                        state.done.remove(&key);
                        session.remove_segment(key, run);
                    }
                    let ready = state.done.get(&index).map(|range| range.2);
                    if ready.is_none() && !state.error && !session.stopped.load(Ordering::SeqCst) {
                        // How far THIS run has got; earlier runs' leftovers say nothing.
                        let highest = state.done.range(state.run_start..).next_back().map_or(state.run_start - 1, |(key, _)| *key);
                        if index < state.run_start || index > highest + SEEK_GAP || state.finished {
                            // Only the newest request moves FFmpeg, and only once. A request
                            // the player has since left behind (it seeked elsewhere) gives up:
                            // two of them restarting in turn at their own positions was a
                            // loop (3 531 restarts in seconds on the test host, until zurg
                            // 5xx'd).
                            if state.requests != ticket || restarted {
                                if state.requests != ticket {
                                    return Err(404);
                                }
                            } else {
                                info!("TRANSCODE {} seek to segment {index} (run from {}, produced to {highest})", session.id, state.run_start);
                                // Claimed under the lock, so the other representation's request
                                // for the same segment waits for this restart.
                                state.run_start = index;
                                state.seek = ticket;
                                state.finished = false;
                                state.input_retries = 0;
                                restart = true;
                            }
                        }
                    }
                    (ready, state.error)
                }
                Err(_) => return Err(500),
            };
            if let Some(run) = ready {
                let fragment = std::fs::read(session.segment_file(index, rep, run)).map_err(|_| 500u16)?;
                if session.plan.protocol == Protocol::Dash {
                    let mut bytes = std::fs::read(session.init_file(rep, run)).map_err(|_| 500u16)?;
                    bytes.extend(fragment);
                    return Ok(bytes);
                }
                return Ok(fragment);
            }
            if session.stopped.load(Ordering::SeqCst) {
                return Err(404);
            }
            if error {
                return Err(500);
            }
            if restart {
                restarted = true;
                // The claim above is made under the lock, the launch after it. On the Windows
                // runner killing the old FFmpeg took long enough that a newer seek claimed and
                // launched in between, and this older launch then replaced it: the newest
                // request waited on a run that was gone (CI 2026-10-18). Launches go one at a
                // time, and one a newer seek has overtaken is dropped.
                let _launching = session.launching.lock();
                if session.state.lock().is_ok_and(|state| state.seek == ticket) {
                    self.launch(session, index, None);
                }
            } else if session.throttled() {
                session.set_paused(false);
                if let Ok(mut state) = session.state.lock() {
                    state.paused = false;
                }
            }
            if Instant::now() > deadline {
                return Err(500);
            }
            thread::sleep(Duration::from_millis(25));
        }
    }

    /// A DASH representation's `header`: the init the current run wrote.
    pub fn header(&self, session: &Arc<Session>, rep: usize) -> Result<Vec<u8>, u16> {
        if rep >= session.plan.representations().max(1) {
            return Err(404);
        }
        session.touch();
        let deadline = Instant::now() + Duration::from_secs(45);
        loop {
            // The init is complete once its run has finished a segment -- not once that
            // run's FIRST fragment exists: the player re-fetches the header on every seek,
            // long after the first fragments were pruned behind it (that hung the seek).
            let (run, finished) = session
                .state
                .lock()
                .map(|state| (state.run_start, state.done.values().any(|range| range.2 == state.run_start)))
                .map_err(|_| 500u16)?;
            let path = session.init_file(rep, run);
            if path.exists() && (finished || session.segment_file(run, rep, run).exists()) {
                return std::fs::read(path).map_err(|_| 500);
            }
            if session.stopped.load(Ordering::SeqCst) {
                return Err(404);
            }
            if session.errored() || Instant::now() > deadline {
                return Err(500);
            }
            thread::sleep(Duration::from_millis(25));
        }
    }
}

/// A file path as a filter option takes it: quoted, so only `:` needs escaping.
fn filter_path(path: &str) -> String {
    path.replace('\\', "/").replace(':', "\\:").replace('\'', "")
}

/// `length` bytes of a source's file from `offset`, as `mkvsubs` reads a subtitle track.
fn read_range(url: &str, auth: Option<&str>, offset: u64, length: usize) -> Result<Vec<u8>, String> {
    let mut headers = vec![
        ("User-Agent", super::upstream::UPSTREAM_UA.to_string()),
        ("Range", format!("bytes={offset}-{}", offset + length.max(1) as u64 - 1)),
    ];
    if let Some(auth) = auth {
        headers.push(("Authorization", auth.to_string()));
    }
    let mut response = super::upstream::open("GET", url, &headers, b"", Duration::from_secs(30))?;
    // A source that ignores Range would answer the whole release.
    if response.status != 206 {
        return Err(format!("the source answered {} to a range", response.status));
    }
    response.read_body(length + 1024)
}

/// Spawn FFmpeg so it dies with rex. On Linux the child asks for SIGKILL when its parent
/// goes (`PR_SET_PDEATHSIG`), which fires when the spawning THREAD exits -- so every
/// child is spawned from one thread that lives as long as the process, never from the
/// request thread that asked for it (measured: a killed rex left a conversion running an
/// hour, reading from zurg into a deleted directory).
fn spawn_tied(command: Command) -> std::io::Result<Child> {
    use std::sync::mpsc;
    type Job = (Command, mpsc::Sender<std::io::Result<Child>>);
    static SPAWNER: OnceLock<Mutex<mpsc::Sender<Job>>> = OnceLock::new();
    let sender = SPAWNER.get_or_init(|| {
        let (sender, receiver) = mpsc::channel::<Job>();
        thread::spawn(move || {
            for (mut command, reply) in receiver {
                #[cfg(target_os = "linux")]
                unsafe {
                    use std::os::unix::process::CommandExt;
                    command.pre_exec(|| {
                        libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
                        Ok(())
                    });
                }
                let _ = reply.send(command.spawn());
            }
        });
        Mutex::new(sender)
    });
    let (reply, answer) = mpsc::channel();
    sender
        .lock()
        .map_err(|_| std::io::Error::other("spawner lock poisoned"))?
        .send((command, reply))
        .map_err(|_| std::io::Error::other("spawner gone"))?;
    answer.recv().map_err(|_| std::io::Error::other("spawner gone"))?
}

/// FFmpeg processes a previous rex left running in `root` (killed before the tie above
/// existed, or by a signal that skipped it): every process of ours whose command line
/// names the transcode directory.
fn kill_leftovers(root: &std::path::Path) {
    let needle = root.to_string_lossy().to_string();
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return;
    };
    let me = std::process::id();
    for entry in entries.flatten() {
        let Some(pid) = entry.file_name().to_str().and_then(|name| name.parse::<u32>().ok()) else {
            continue;
        };
        if pid == me {
            continue;
        }
        let Ok(cmdline) = std::fs::read(entry.path().join("cmdline")) else {
            continue;
        };
        let cmdline = String::from_utf8_lossy(&cmdline).replace('\0', " ");
        if cmdline.contains("ffmpeg") && cmdline.contains(&needle) {
            warning!("transcoder: killing a leftover FFmpeg (pid {pid}) from an earlier run");
            crate::platform::kill(pid);
        }
    }
}

fn sanitize(id: &str) -> String {
    id.chars()
        .map(|ch| if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' { ch } else { '_' })
        .collect()
}

/// `TranscodeSession`, in the reference's field order. `key` is the bare id on
/// `/transcode/sessions` and `/transcode/sessions/<id>` inside a session or timeline.
impl std::fmt::Debug for SegmentPlan {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.debug_struct("SegmentPlan").field("session", &self.session.id).field("index", &self.index).finish()
    }
}

pub(crate) fn transcode_session_row(session: &Session, hardware: &Hardware, nested: bool) -> Value {
    let plan = &session.plan;
    let (min, max) = session.window();
    let duration = plan.duration_ms;
    let finished = session.finished();
    let speed = session.speed();
    let progress = if duration > 0 { (max * 1000.0 / duration as f64 * 100.0).min(100.0) } else { 0.0 };
    let remaining = if speed > 0.0 { (((duration as f64 / 1000.0) - max).max(0.0) / speed).round() as i64 } else { 0 };
    let video = plan.video.as_ref();
    let audio = plan.audio.as_ref();
    let mut row = Map::new();
    row.insert(
        "key".to_string(),
        json!(if nested { format!("/transcode/sessions/{}", session.id) } else { session.id.clone() }),
    );
    row.insert("throttled".to_string(), json!(session.throttled()));
    row.insert("complete".to_string(), json!(finished));
    row.insert("progress".to_string(), json!((progress * 10.0).round() / 10.0));
    row.insert("size".to_string(), json!(-1));
    row.insert("speed".to_string(), json!((speed * 10.0).round() / 10.0));
    row.insert("error".to_string(), json!(session.errored()));
    row.insert("duration".to_string(), json!(duration));
    row.insert("remaining".to_string(), json!(remaining));
    row.insert("context".to_string(), json!("streaming"));
    if let Some(video) = video {
        row.insert("sourceVideoCodec".to_string(), json!(video.source_codec));
    }
    if let Some(audio) = audio {
        row.insert("sourceAudioCodec".to_string(), json!(audio.source_codec));
    }
    if let Some(video) = video {
        row.insert("videoDecision".to_string(), json!(if video.copy { "copy" } else { "transcode" }));
    }
    if let Some(audio) = audio {
        row.insert("audioDecision".to_string(), json!(if audio.copy { "copy" } else { "transcode" }));
    }
    row.insert("protocol".to_string(), json!(plan.protocol.name()));
    row.insert("container".to_string(), json!(plan.container()));
    if let Some(video) = video {
        row.insert("videoCodec".to_string(), json!(if video.copy { video.source_codec.clone() } else { "h264".to_string() }));
    }
    if let Some(audio) = audio {
        row.insert("audioCodec".to_string(), json!(audio.codec));
        row.insert("audioChannels".to_string(), json!(audio.channels));
    }
    if let Some(video) = video {
        row.insert("width".to_string(), json!(video.width));
        row.insert("height".to_string(), json!(video.height));
    }
    let path = session.path();
    let device = video.is_some_and(|video| !video.copy) && hardware.accel != Accel::None && path != Some(Path::Software);
    row.insert("transcodeHwRequested".to_string(), json!(device));
    // Unmeasured: no reference here has a device. The names are the ones PMS's binary
    // holds for these fields; Plex Web prints "(hw)" when `transcodeHwEncoding` is set.
    if device {
        let name = hardware.accel.name();
        if path == Some(Path::Full) {
            row.insert("transcodeHwDecoding".to_string(), json!(name));
        }
        row.insert("transcodeHwEncoding".to_string(), json!(name));
    }
    row.insert("transcodeHwFullPipeline".to_string(), json!(device && path == Some(Path::Full)));
    row.insert("timeStamp".to_string(), json!(session.started));
    row.insert("maxOffsetAvailable".to_string(), json!((max * 1000.0).round() / 1000.0));
    row.insert("minOffsetAvailable".to_string(), json!((min * 1000.0).round() / 1000.0));
    Value::Object(row)
}

/// Serve a segment plan: wait outside the runtime lock, then answer with the measured
/// head (`video/MP2T`, `Accept-Ranges: bytes`, `Connection: close`).
pub(crate) fn finish_segment(plan: SegmentPlan, response: &mut Response) {
    let bytes = if plan.header {
        plan.transcoder.header(&plan.session, plan.rep)
    } else {
        plan.transcoder.segment(&plan.session, plan.index, plan.rep)
    };
    match bytes {
        Ok(bytes) => {
            response.status = 200;
            response.body = bytes;
        }
        Err(status) => {
            *response = Response::error(status, if status == 404 { "rex: not found" } else { "rex: transcode failed" });
        }
    }
}

/// `videoResolution` as PMS labels a converted Media: by width, `SD` below 720p.
pub(crate) fn resolution_label(width: i64, height: i64) -> &'static str {
    if width >= 3200 || height >= 1800 {
        "4k"
    } else if width >= 1700 || height >= 1000 {
        "1080p"
    } else if width >= 1200 || height >= 700 {
        "720p"
    } else {
        "SD"
    }
}

/// The codecs `X-Plex-Client-Profile-Extra` adds to the profile's HLS conversion target:
/// `append-`/`add-transcode-target-codec` name video and audio codecs,
/// `add-`/`append-transcode-target-audio-codec` audio ones. Plex Web 4.160 sends
/// `append-transcode-target-codec(type=videoProfile&context=streaming&videoCodec=h264&audioCodec=aac&protocol=hls)`.
pub(crate) fn profile_extra_codecs(extra: &str, protocol: Protocol) -> (Vec<String>, Vec<String>) {
    let mut video = Vec::new();
    let mut audio = Vec::new();
    for directive in extra.split('+') {
        let Some((name, rest)) = directive.split_once('(') else {
            continue;
        };
        let arguments = rest.trim_end_matches(')');
        let pairs = arguments
            .split('&')
            .filter_map(|pair| pair.split_once('='))
            .map(|(key, value)| (key.to_string(), percent_decode(value)))
            .collect::<HashMap<_, _>>();
        if pairs.get("type").is_some_and(|kind| kind != "videoProfile")
            || pairs.get("protocol").is_some_and(|named| named != protocol.name())
            || pairs.get("context").is_some_and(|context| context != "streaming")
        {
            continue;
        }
        let codecs = |key: &str| {
            pairs
                .get(key)
                .map(|list| list.split(',').map(|codec| codec.trim().to_ascii_lowercase()).filter(|codec| !codec.is_empty()).collect::<Vec<_>>())
                .unwrap_or_default()
        };
        match name.trim() {
            "append-transcode-target-codec" | "add-transcode-target-codec" => {
                video.extend(codecs("videoCodec"));
                audio.extend(codecs("audioCodec"));
            }
            "add-transcode-target-audio-codec" | "append-transcode-target-audio-codec" => audio.extend(codecs("audioCodec")),
            _ => {}
        }
    }
    (video, audio)
}

fn even(value: f64) -> i64 {
    ((value / 2.0).round() as i64 * 2).max(2)
}

fn number_of(value: Option<&Value>) -> Option<f64> {
    match value? {
        Value::Number(number) => number.as_f64(),
        Value::String(text) => text.trim().parse::<f64>().ok(),
        _ => None,
    }
}

impl Runtime {
    /// The conversion this request asks for, or `None` when rex cannot make one: no
    /// transcoder, not HLS, no conversion target in the profile, or no source to read.
    pub(super) fn transcode_plan(
        &self,
        query: &HashMap<String, String>,
        headers: &HashMap<String, String>,
        key: &str,
        media: &Value,
        profile_name: &str,
        protocol: &str,
    ) -> Option<Plan> {
        let tonemapx = self.transcoder.as_ref()?.hardware().tonemapx;
        let media = crate::catalog::unpacked(media);
        let media = media.as_ref();
        let wire = Protocol::parse(protocol)?;
        let extra = query
            .get("X-Plex-Client-Profile-Extra")
            .or_else(|| headers.get("x-plex-client-profile-extra"))
            .cloned()
            .unwrap_or_default();
        let profile = &decision::with_extra(decision::resolve(&self.decision_profiles, profile_name)?, &extra);
        // A real PMS refuses Dolby Vision profile 5 (2003): its IPT colour has no HDR10 base.
        // tonemapx applies the RPU (measured 2026-10-17: Jellyfin's P5 clip tonemaps to its
        // P8.1 and HDR10 twins' colours; untonemapped it is green), so with it rex converts one.
        let refused_as = if tonemapx { None } else { Some(media) };
        // A download converts to the profile's `static` target, which the streaming check skips.
        if wire != Protocol::File && decision::conversion(profile, protocol, refused_as) != decision::Outcome::Convert {
            return None;
        }
        let row = self.catalog.get(key)?;
        let part = media.get("Part").and_then(Value::as_array).and_then(|parts| parts.first())?;
        let file = part.get("file").and_then(Value::as_str).filter(|file| !file.is_empty())?;
        let (source, path) = self.part_source(row, file)?;
        let (url, auth) = source.ffmpeg_input(&path);
        let target = profile
            .get("targets")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(Value::as_object)
            .find(|target| {
                let context = target.get("context").and_then(Value::as_str);
                target.get("tag").and_then(Value::as_str) == Some("VideoProfile")
                    && if wire == Protocol::File {
                        context == Some("static")
                    } else {
                        target.get("protocol").and_then(Value::as_str) == Some(protocol) && context != Some("static")
                    }
            })?;
        let list = |value: Option<&Value>| {
            value
                .and_then(Value::as_str)
                .unwrap_or("")
                .split(',')
                .map(|codec| codec.trim().to_ascii_lowercase())
                .filter(|codec| !codec.is_empty())
                .collect::<Vec<_>>()
        };
        let mut video_codecs = list(target.get("codec").or_else(|| target.get("videoCodec")));
        let mut audio_codecs = list(target.get("audioCodec"));
        let (more_video, more_audio) = profile_extra_codecs(&extra, wire);
        video_codecs.extend(more_video);
        audio_codecs.extend(more_audio);
        let streams = part.get("Stream").and_then(Value::as_array).cloned().unwrap_or_default();
        let kind = |stream: &Value| stream.get("streamType").and_then(Value::as_i64);
        let video_stream = streams.iter().find(|stream| kind(stream) == Some(1));
        let audio_stream = streams
            .iter()
            .find(|stream| kind(stream) == Some(2) && stream.get("selected").is_some_and(truthy))
            .or_else(|| streams.iter().find(|stream| kind(stream) == Some(2)));
        let flag = |name: &str| query.get(name).map(String::as_str) != Some("0");
        // An embedded image subtitle is burned in the conversion's own pass; a text one is read
        // out of the release first (`text_burn`), since FFmpeg's `subtitles` filter reads its
        // whole file before the first frame.
        let burn = matches!(query.get("subtitles").map(String::as_str), None | Some("burn" | "auto"));
        let subtitle = streams
            .iter()
            .filter(|_| burn)
            .find(|stream| kind(stream) == Some(3) && stream.get("selected").is_some_and(truthy) && stream.get("key").is_none())
            .filter(|stream| {
                let codec = stream.get("codec").map(string_value).unwrap_or_default().to_ascii_lowercase();
                decision::IMAGE_SUBTITLES.contains(&codec.as_str())
            })
            .and_then(|stream| stream.get("index").and_then(Value::as_i64))
            .filter(|index| *index >= 0 && video_stream.is_some());
        // A target in Matroska carries a selected embedded text subtitle it lists, as the
        // reference copies an SRT into Plex for Android's and iOS's segments; an image one is
        // still burned. ASS is copied too: the reference burns it, which rex cannot without
        // reading the whole file first, and both apps list it.
        let matroska = wire == Protocol::Hls && target.get("container").and_then(Value::as_str) == Some("mkv");
        let subtitle_codecs = list(target.get("subtitleCodec"));
        let text_subtitle = streams
            .iter()
            .filter(|_| burn && matroska)
            .find(|stream| kind(stream) == Some(3) && stream.get("selected").is_some_and(truthy) && stream.get("key").is_none())
            .filter(|stream| {
                let codec = stream.get("codec").map(string_value).unwrap_or_default().to_ascii_lowercase();
                !decision::IMAGE_SUBTITLES.contains(&codec.as_str())
                    && (subtitle_codecs.contains(&codec) || codec == "mov_text" && subtitle_codecs.iter().any(|listed| listed == "srt"))
            });
        // An MP4's timed text is converted to SRT as it is read; its packets come in the pass.
        let text_to_srt = text_subtitle.is_some_and(|stream| stream.get("codec").map(string_value).is_some_and(|codec| codec.eq_ignore_ascii_case("mov_text")));
        let text_subtitle = text_subtitle.and_then(|stream| stream.get("index").and_then(Value::as_i64)).filter(|index| *index >= 0);
        // Any other client gets a selected embedded text subtitle burned, as the reference
        // burns an SRT for the iOS profile's MPEG-TS (Plex Web's `segments-subs` is not built,
        // so it is burned for Plex Web too). A Matroska or MP4 release: `mkvsubs` reads the
        // track through its Cues or sample tables, where FFmpeg would read the whole release.
        let text_burn = streams
            .iter()
            .filter(|_| burn && !matroska && wire != Protocol::File && video_stream.is_some())
            .filter(|_| matches!(media.get("container").and_then(Value::as_str), Some("mkv" | "mp4" | "mov")))
            .find(|stream| kind(stream) == Some(3) && stream.get("selected").is_some_and(truthy) && stream.get("key").is_none())
            .and_then(|stream| {
                let codec = stream.get("codec").map(string_value).unwrap_or_default().to_ascii_lowercase();
                let index = stream.get("index").and_then(Value::as_i64).filter(|index| *index >= 0)?;
                matches!(codec.as_str(), "srt" | "ass" | "ssa" | "mov_text").then_some(TextBurn { index, ass: matches!(codec.as_str(), "ass" | "ssa") })
            });
        // A selected upload carries its text on its Stream row (`_content`).
        let sidecar = streams
            .iter()
            .filter(|_| burn && subtitle.is_none() && text_subtitle.is_none() && text_burn.is_none())
            .find(|stream| kind(stream) == Some(3) && stream.get("selected").is_some_and(truthy) && stream.get("key").is_some())
            .and_then(|stream| {
                let id = stream.get("id").map(string_value)?;
                let format = stream.get("codec").map(string_value).unwrap_or_default().to_ascii_lowercase();
                let fits = if matroska { subtitle_codecs.contains(&format) } else { video_stream.is_some() };
                let content = stream.get("_content").and_then(Value::as_str)?.to_string();
                (fits && matches!(format.as_str(), "srt" | "ass" | "ssa")).then_some(Sidecar { id, format, content })
            });
        let max_kbps = query.get("maxVideoBitrate").and_then(|value| value.parse::<i64>().ok()).filter(|value| *value > 0);
        let cap = query.get("videoResolution").and_then(|value| {
            let (w, h) = value.split_once('x')?;
            Some((w.trim().parse::<f64>().ok()?, h.trim().parse::<f64>().ok()?))
        });
        let audio = audio_stream.map(|stream| {
            let codec = stream.get("codec").map(string_value).unwrap_or_default().to_ascii_lowercase();
            let channels = stream.get("channels").and_then(Value::as_i64).unwrap_or(2).max(1);
            let source_bitrate = stream.get("bitrate").and_then(Value::as_i64).unwrap_or(0);
            let sampling_rate = stream.get("samplingRate").and_then(Value::as_i64).filter(|rate| *rate > 0).unwrap_or(48000);
            let copy = flag("directStreamAudio")
                && audio_codecs.contains(&codec)
                && decision::codec_limitation(profile, "VideoAudioCodec", &codec, media).is_none();
            if copy {
                AudioPlan { index: stream.get("index").and_then(Value::as_i64).unwrap_or(-1), copy, source_codec: codec.clone(), codec, channels, bitrate: source_bitrate, sampling_rate }
            } else {
                let out = if audio_codecs.iter().any(|codec| codec == "aac") || audio_codecs.is_empty() { "aac".to_string() } else { audio_codecs[0].clone() };
                // The profile's own channel bound for this codec (Web: 6), else stereo.
                let mut probe = media.clone();
                let mut allowed = channels;
                while allowed > 2 {
                    if let Some(found) = probe
                        .get_mut("Part")
                        .and_then(Value::as_array_mut)
                        .and_then(|parts| parts.first_mut())
                        .and_then(|part| part.get_mut("Stream"))
                        .and_then(Value::as_array_mut)
                        .and_then(|streams| streams.iter_mut().find(|stream| stream.get("index") == audio_stream.and_then(|chosen| chosen.get("index"))))
                    {
                        found["channels"] = json!(allowed);
                        found["codec"] = json!(out);
                    }
                    if decision::codec_limitation(profile, "VideoAudioCodec", &out, &probe).is_none() {
                        break;
                    }
                    allowed -= 1;
                }
                let channels = allowed.min(6);
                AudioPlan {
                    index: stream.get("index").and_then(Value::as_i64).unwrap_or(-1),
                    copy: false,
                    source_codec: codec,
                    codec: out,
                    channels,
                    bitrate: if channels > 2 { 384 } else { 192 },
                    sampling_rate,
                }
            }
        });
        let video = video_stream.map(|stream| {
            let codec = stream.get("codec").map(string_value).unwrap_or_default().to_ascii_lowercase();
            let width = number_of(stream.get("width")).or_else(|| number_of(media.get("width"))).unwrap_or(1920.0);
            let height = number_of(stream.get("height")).or_else(|| number_of(media.get("height"))).unwrap_or(1080.0);
            let bit_depth = stream.get("bitDepth").and_then(Value::as_i64).unwrap_or(8);
            let frame_rate = number_of(stream.get("frameRate")).filter(|rate| *rate > 0.0).unwrap_or(23.976);
            let audio_bitrate = audio_stream.and_then(|stream| stream.get("bitrate").and_then(Value::as_i64)).unwrap_or(0);
            let source_bitrate = stream
                .get("bitrate")
                .and_then(Value::as_i64)
                .or_else(|| media.get("bitrate").and_then(Value::as_i64).map(|total| (total - audio_bitrate).max(0)))
                .unwrap_or(0);
            let fits = cap.is_none_or(|(w, h)| width <= w && height <= h);
            // A burned subtitle is part of the picture, so the picture is converted.
            let copy = subtitle.is_none()
                && text_burn.is_none()
                && !(sidecar.is_some() && !matroska)
                && flag("directStream")
                && video_codecs.contains(&codec)
                && decision::codec_limitation(profile, "VideoCodec", &codec, media).is_none()
                && max_kbps.is_none_or(|max| source_bitrate <= max)
                && fits;
            let (out_width, out_height) = match cap {
                Some((w, h)) if !copy && !fits => {
                    let scale = (w / width).min(h / height).min(1.0);
                    (even(width * scale), even(height * scale))
                }
                _ => (width as i64, height as i64),
            };
            let audio_out = audio.as_ref().map_or(0, |audio| audio.bitrate);
            let bitrate = if copy {
                source_bitrate
            } else if let Some(max) = max_kbps {
                (max - audio_out).max(300)
            } else if source_bitrate > 0 {
                // An H.264 encode needs more bits than the HEVC/AV1/VP9 it replaces.
                let factor = if codec == "h264" { 1.1 } else { 1.5 };
                ((source_bitrate as f64 * factor) as i64).clamp(1500, 40000)
            } else {
                match out_height {
                    h if h > 1100 => 20000,
                    h if h > 730 => 8000,
                    h if h > 500 => 4000,
                    _ => 2000,
                }
            };
            VideoPlan {
                index: stream.get("index").and_then(Value::as_i64).unwrap_or(-1),
                copy,
                source_codec: codec,
                bit_depth,
                hdr: hdr_stream(stream),
                width: out_width,
                height: out_height,
                bitrate,
                frame_rate,
                source_profile: stream.get("profile").map(string_value).unwrap_or_default(),
                source_level: stream.get("level").and_then(Value::as_i64).unwrap_or(40),
            }
        });
        if video.is_none() && audio.is_none() {
            return None;
        }
        // Without tonemapx a re-encoded profile 5 is green, worse than the reference's refusal.
        let profile_5 = video_stream.and_then(|stream| number_of(stream.get("DOVIProfile"))) == Some(5.0);
        if profile_5 && !tonemapx && video.as_ref().is_some_and(|video| !video.copy) {
            return None;
        }
        Some(Plan {
            rating_key: key.to_string(),
            url,
            auth,
            duration_ms: part
                .get("duration")
                .and_then(Value::as_i64)
                .or_else(|| media.get("duration").and_then(Value::as_i64))
                .unwrap_or(0),
            video,
            audio,
            subtitle,
            matroska,
            text_subtitle,
            text_to_srt,
            sidecar,
            text_burn,
            incomplete_segments: query.get("X-Plex-Incomplete-Segments").map(String::as_str) == Some("1")
                || headers.get("x-plex-incomplete-segments").map(String::as_str) == Some("1"),
            playback: self.playback_session(query, headers),
            protocol: wire,
        })
    }

    /// The converted Media, from the decision's direct-play projection of the source
    /// one. Measured (`scratch_copy_mkv.json`, `scratch_q720_2mbps.json`): Media and Part
    /// describe the OUTPUT (`mpegts`/`hls`, its size and bitrate) and keep only these
    /// fields; the Part loses `key`, `file` and `size`; each Stream keeps its own fields
    /// with the output's codec, size and bitrate and gains `decision` and `location`.
    /// The decision's copy keeps `deepAnalysisVersion` and `requiredBandwidths`; the
    /// session row's does not.
    /// Whether `plan` would be one video encode past the owner's "Maximum simultaneous video
    /// transcode" (`TranscodeCountLimit`, or the 1.42.2 `_CPU-TranscodeCountLimit`; 0 is
    /// unlimited). A copied video costs almost nothing, so remuxes are never counted or
    /// refused, and the client's own earlier conversion (a seek, a quality change) is not
    /// counted against it.
    // ponytail: hardware and CPU encodes share one count; split them if a GPU host needs the CPU-only cap.
    pub(super) fn over_conversion_cap(&self, plan: &Plan) -> bool {
        let Some(transcoder) = &self.transcoder else { return false };
        if plan.video.as_ref().is_none_or(|video| video.copy) {
            return false;
        }
        let Some(cap) = ["TranscodeCountLimit", "_CPU-TranscodeCountLimit"]
            .iter()
            .filter_map(|id| number_of(self.server_pref(id).as_ref()))
            .map(|limit| limit as i64)
            .filter(|limit| *limit > 0)
            .min()
        else {
            return false;
        };
        let encoding = transcoder
            .sessions()
            .iter()
            .filter(|session| !session.stopped.load(Ordering::SeqCst) && session.plan.playback != plan.playback)
            .filter(|session| session.plan.video.as_ref().is_some_and(|video| !video.copy))
            .count();
        encoding as i64 >= cap
    }

    pub(super) fn converted_media(&self, projected: &Value, plan: &Plan, decision: bool) -> Value {
        let projected = crate::catalog::unpacked(projected);
        let source = projected.as_object().cloned().unwrap_or_default();
        let part = source
            .get("Part")
            .and_then(Value::as_array)
            .and_then(|parts| parts.first())
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let video = plan.video.as_ref();
        let audio = plan.audio.as_ref();
        let (width, height) = video.map_or((0, 0), |video| (video.width, video.height));
        let video_codec = video.map(|video| if video.copy { video.source_codec.clone() } else { "h264".to_string() });
        let mut media = Map::new();
        for name in ["audioProfile", "id", "videoProfile"] {
            if let Some(value) = source.get(name).filter(|value| !value.is_null()) {
                media.insert(name.to_string(), value.clone());
            }
        }
        if let Some(audio) = audio {
            media.insert("audioChannels".to_string(), json!(audio.channels));
            media.insert("audioCodec".to_string(), json!(audio.codec));
        }
        media.insert("bitrate".to_string(), json!(plan.bitrate()));
        let dash = plan.protocol == Protocol::Dash;
        // A download's Media (`download_queue_s1422.json`): optimized, no `protocol`, streams `direct`.
        let file = plan.protocol == Protocol::File;
        media.insert("container".to_string(), json!(plan.container()));
        media.insert("duration".to_string(), json!(plan.duration_ms));
        if video.is_some() {
            media.insert("height".to_string(), json!(height));
        }
        // Measured on the DASH decision only (`scratch_dash_copy.json`).
        if dash || file {
            media.insert("optimizedForStreaming".to_string(), json!(true));
        }
        if !file {
            media.insert("protocol".to_string(), json!(plan.protocol.name()));
        }
        if let Some(codec) = &video_codec {
            media.insert("videoCodec".to_string(), json!(codec));
            if let Some(rate) = source.get("videoFrameRate") {
                media.insert("videoFrameRate".to_string(), rate.clone());
            }
            media.insert("videoResolution".to_string(), json!(resolution_label(width, height)));
            media.insert("width".to_string(), json!(width));
        }
        media.insert("selected".to_string(), json!(true));
        let mut out_part = Map::new();
        let names: &[&str] = if decision {
            &["audioProfile", "deepAnalysisVersion", "id", "requiredBandwidths", "videoProfile"]
        } else {
            &["audioProfile", "id", "videoProfile"]
        };
        for name in names {
            if let Some(value) = part.get(*name).filter(|value| !value.is_null()) {
                out_part.insert(name.to_string(), value.clone());
            }
        }
        out_part.insert("bitrate".to_string(), json!(plan.bitrate()));
        out_part.insert("container".to_string(), json!(plan.container()));
        out_part.insert("duration".to_string(), json!(plan.duration_ms));
        if video.is_some() {
            out_part.insert("height".to_string(), json!(height));
        }
        if dash || file {
            out_part.insert("optimizedForStreaming".to_string(), json!(true));
        }
        if !file {
            out_part.insert("protocol".to_string(), json!(plan.protocol.name()));
        }
        if video.is_some() {
            out_part.insert("width".to_string(), json!(width));
        }
        out_part.insert("decision".to_string(), json!("transcode"));
        out_part.insert("selected".to_string(), json!(true));
        let streams = part
            .get("Stream")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .filter_map(|stream| {
                let mut stream = stream.as_object()?.clone();
                let kind = stream.get("streamType").and_then(Value::as_i64);
                if !decision {
                    stream.shift_remove("requiredBandwidths");
                }
                match (kind, video, audio) {
                    (Some(1), Some(video), _) => {
                        if !video.copy {
                            stream.insert("codec".to_string(), json!("h264"));
                            stream.insert("bitrate".to_string(), json!(video.bitrate));
                            stream.insert("width".to_string(), json!(video.width));
                            stream.insert("height".to_string(), json!(video.height));
                        }
                        stream.insert("decision".to_string(), json!(if video.copy { "copy" } else { "transcode" }));
                    }
                    (Some(2), _, Some(audio)) => {
                        if !audio.copy {
                            stream.insert("codec".to_string(), json!(audio.codec));
                            stream.insert("channels".to_string(), json!(audio.channels));
                            stream.insert("bitrate".to_string(), json!(audio.bitrate));
                        }
                        stream.insert("decision".to_string(), json!(if audio.copy { "copy" } else { "transcode" }));
                    }
                    // The reference's rows (`subtitles_s1422.json`): a burned subtitle says
                    // `burn: "1"`, a copied one its `format`, both in the HLS segments.
                    (Some(3), _, _) if plan.subtitle.is_some() && stream.get("index").and_then(Value::as_i64) == plan.subtitle
                        || plan.text_burn.as_ref().is_some_and(|text| stream.get("index").and_then(Value::as_i64) == Some(text.index)) => {
                        stream.insert("burn".to_string(), json!("1"));
                        stream.insert("decision".to_string(), json!("burn"));
                    }
                    (Some(3), _, _) if plan.matroska && plan.text_subtitle.is_some() && stream.get("index").and_then(Value::as_i64) == plan.text_subtitle => {
                        // A converted subtitle names its new format and says `transcode`, as the
                        // reference's SRT turned ASS for Plex Web does (`subtitles_s1422.json`).
                        if plan.text_to_srt {
                            stream.insert("format".to_string(), json!("srt"));
                            stream.insert("decision".to_string(), json!("transcode"));
                        } else {
                            let codec = stream.get("codec").cloned().unwrap_or(Value::Null);
                            stream.insert("format".to_string(), codec);
                            stream.insert("decision".to_string(), json!("copy"));
                        }
                    }
                    // The reference's sidecar rows keep their `key` (stream 5 there).
                    (Some(3), _, _) if plan.sidecar.as_ref().is_some_and(|sidecar| stream.get("id").map(string_value).as_deref() == Some(sidecar.id.as_str())) => {
                        stream.shift_remove("_content");
                        if Sidecar::burned(plan) {
                            stream.insert("burn".to_string(), json!("1"));
                            stream.insert("decision".to_string(), json!("burn"));
                        } else {
                            let codec = stream.get("codec").cloned().unwrap_or(Value::Null);
                            stream.insert("format".to_string(), codec);
                            stream.insert("decision".to_string(), json!("copy"));
                        }
                    }
                    // Any other subtitle is not carried through a conversion.
                    _ => return None,
                }
                // HLS muxes both into one stream; DASH keeps a representation each.
                let location = match (dash, kind) {
                    _ if file => "direct",
                    (false, _) => "segments-av",
                    (_, Some(3)) => "segments-video",
                    (true, Some(1)) => "segments-video",
                    (true, _) => "segments-audio",
                };
                stream.insert("location".to_string(), json!(location));
                Some(Value::Object(stream))
            })
            .collect::<Vec<_>>();
        if !streams.is_empty() {
            out_part.insert("Stream".to_string(), Value::Array(streams));
        }
        out_part.insert("_session".to_string(), json!(true));
        media.insert("Part".to_string(), json!([Value::Object(out_part)]));
        media.insert("_session".to_string(), json!(true));
        Value::Object(media)
    }

    /// The `/video/:/transcode/universal/*` leaves once there is an encoder; `None` hands
    /// the request back to the encoder-less answers.
    pub(super) fn route_universal(&mut self, segments: &[&str], query: &HashMap<String, String>, request: &Request) -> Option<Response> {
        let transcoder = Arc::clone(self.transcoder.as_ref()?);
        let leaf = segments.get(4).copied().unwrap_or_default();
        match leaf {
            "start.m3u8" | "start.mpd" => {
                let protocol = if leaf == "start.mpd" { "dash" } else { "hls" };
                let key = match self.decision_target(query, request) {
                    Ok(key) => key,
                    Err(response) => return Some(response),
                };
                let playback = self.playback_session(query, &request.headers);
                if !self.decided_sessions.contains(&(playback.clone(), key.clone())) {
                    return Some(Response::error(400, "rex: no decision for this session"));
                }
                let profile = ["X-Plex-Client-Profile-Name", "X-Plex-Platform", "X-Plex-Device"]
                    .iter()
                    .filter_map(|name| query.get(*name).or_else(|| request.headers.get(&name.to_ascii_lowercase())))
                    .find(|name| self.decision_profiles.contains_key(name.as_str()))
                    .cloned()?;
                let row = self.catalog.get(&key)?.clone();
                let media_rows = row.get("Media").and_then(Value::as_array).cloned().unwrap_or_default();
                let index = query.get("mediaIndex").and_then(|value| value.parse::<usize>().ok()).filter(|index| *index < media_rows.len()).unwrap_or(0);
                let mut media = media_rows.get(index).cloned()?;
                let parts = media.get("Part").and_then(Value::as_array).cloned().unwrap_or_default();
                let part_index = query.get("partIndex").and_then(|value| value.parse::<usize>().ok()).filter(|index| *index < parts.len()).unwrap_or(0);
                if let (Some(object), Some(part)) = (media.as_object_mut(), parts.get(part_index)) {
                    object.insert("Part".to_string(), json!([part]));
                }
                let plan = self.transcode_plan(query, &request.headers, &key, &media, &profile, protocol)?;
                if self.over_conversion_cap(&plan) {
                    return Some(Response::error(503, "rex: the simultaneous conversion limit is reached"));
                }
                let id = query.get("session").filter(|id| !id.is_empty()).cloned().unwrap_or_else(|| playback.clone());
                let (manifest, content_type) = match plan.protocol {
                    Protocol::Hls => (plan.master_playlist(&id), "application/vnd.apple.mpegurl"),
                    Protocol::Dash => (plan.mpd(&id), "application/dash+xml"),
                    Protocol::File => return Some(Response::error(400, "rex: bad request")),
                };
                info!(
                    "TRANSCODE {id} start {} video={} audio={}",
                    logging::py_opt(query.get("path")),
                    plan.video.as_ref().map_or("none".to_string(), |video| if video.copy { format!("copy {}", video.source_codec) } else { format!("{}->h264 {}x{} {}k", video.source_codec, video.width, video.height, video.bitrate) }),
                    plan.audio.as_ref().map_or("none".to_string(), |audio| if audio.copy { format!("copy {}", audio.codec) } else { format!("{}->{} {}ch", audio.source_codec, audio.codec, audio.channels) }),
                );
                if let Err(error) = transcoder.start(&id, plan) {
                    error!("TRANSCODE {id}: {error}");
                    return Some(Response::error(500, "rex: transcode failed"));
                }
                let mut response = Response::bytes(200, content_type);
                response.body = manifest.into_bytes();
                response.headers.push(("Cache-Control".to_string(), "no-cache".to_string()));
                Some(response)
            }
            "session" => {
                let id = segments.get(5).copied().unwrap_or_default();
                // The reference answers a live session's playlist 404 to POST, PUT and DELETE.
                let Some(session) = transcoder.session(id).filter(|_| matches!(request.method.as_str(), "GET" | "HEAD")) else {
                    return Some(Response::error(404, "rex: not found"));
                };
                match (segments.get(6).copied(), segments.get(7).copied(), segments.len()) {
                    (Some("base"), Some("index.m3u8"), 8) => {
                        session.touch();
                        let mut response = Response::bytes(200, "application/vnd.apple.mpegurl");
                        response.body = session.plan.media_playlist().into_bytes();
                        response.headers.push(("Cache-Control".to_string(), "no-cache".to_string()));
                        Some(response)
                    }
                    // Matroska's `EXT-X-MAP` header, `application/octet-stream` on the
                    // reference while its clusters stay `video/MP2T` (`subtitles_s1422.json`).
                    (Some("base"), Some("header"), 8) if session.plan.matroska => {
                        let mut response = Response::bytes(200, "application/octet-stream");
                        response.headers.push(("Cache-Control".to_string(), "no-cache".to_string()));
                        response.headers.push(("Accept-Ranges".to_string(), "bytes".to_string()));
                        response.close = true;
                        self.pending_segment = Some(SegmentPlan { session, index: 0, rep: 0, header: true, transcoder });
                        Some(response)
                    }
                    (Some("base"), Some(name), 8) if name.ends_with(".ts") => {
                        let Ok(index) = name.trim_end_matches(".ts").parse::<i64>() else {
                            return Some(Response::error(404, "rex: not found"));
                        };
                        let mut response = Response::bytes(200, "video/MP2T");
                        response.headers.push(("Cache-Control".to_string(), "no-cache".to_string()));
                        response.headers.push(("Accept-Ranges".to_string(), "bytes".to_string()));
                        response.close = true;
                        self.pending_segment = Some(SegmentPlan { session, index, rep: 0, header: false, transcoder });
                        Some(response)
                    }
                    // DASH: `session/<id>/<rep>/header` and `session/<id>/<rep>/<n>.m4s`,
                    // both `application/octet-stream` on the reference.
                    (Some(rep), Some(name), 8) if session.plan.protocol == Protocol::Dash => {
                        let Ok(rep) = rep.parse::<usize>() else {
                            return Some(Response::error(404, "rex: not found"));
                        };
                        let index = if name == "header" {
                            None
                        } else {
                            match name.strip_suffix(".m4s").and_then(|stem| stem.parse::<i64>().ok()) {
                                Some(index) => Some(index),
                                None => return Some(Response::error(404, "rex: not found")),
                            }
                        };
                        let mut response = Response::bytes(200, "application/octet-stream");
                        response.headers.push(("Cache-Control".to_string(), "no-cache".to_string()));
                        response.headers.push(("Accept-Ranges".to_string(), "bytes".to_string()));
                        response.close = true;
                        self.pending_segment = Some(SegmentPlan { session, index: index.unwrap_or(0), rep, header: index.is_none(), transcoder });
                        Some(response)
                    }
                    _ => Some(Response::error(404, "rex: not found")),
                }
            }
            "ping" | "stop" => {
                let id = query.get("session")?.clone();
                let known = if leaf == "ping" { transcoder.ping(&id) } else { transcoder.stop(&id) };
                Some(if known { Response::empty(200) } else { Response::error(404, "rex: not found") })
            }
            _ => None,
        }
    }

    /// `/transcode/sessions` and `/transcode/sessions/{id}` with an encoder.
    pub(super) fn route_transcode_sessions(&mut self, id: Option<&str>, method: &str) -> Response {
        let Some(transcoder) = self.transcoder.clone() else {
            return match id {
                None => Response::json(200, json!({"MediaContainer": {"size": 0}})),
                Some(_) => Response::error(404, "rex: not found"),
            };
        };
        let hardware = transcoder.hardware();
        match id {
            None => {
                let rows = transcoder.sessions().iter().map(|session| transcode_session_row(session, &hardware, false)).collect::<Vec<_>>();
                let mut fields = Map::new();
                fields.insert("size".to_string(), json!(rows.len()));
                if !rows.is_empty() {
                    fields.insert("TranscodeSession".to_string(), Value::Array(rows));
                }
                Response::json(200, json!({"MediaContainer": fields}))
            }
            Some(id) => match (transcoder.session(id), method) {
                (Some(_), "DELETE") => {
                    transcoder.stop(id);
                    Response::empty(200)
                }
                (Some(session), _) => Response::json(
                    200,
                    json!({"MediaContainer": {"size": 1, "TranscodeSession": [transcode_session_row(&session, &hardware, false)]}}),
                ),
                (None, _) => Response::error(404, "rex: not found"),
            },
        }
    }

    /// What a timeline beat carries about a live conversion (measured on plexscratch):
    /// the `TranscodeSession`, a `Bandwidth` of ~1.05x the OUTPUT bitrate labelled with
    /// the output's resolution, and a conversion that died as `terminationCode 4006`.
    /// A `stopped` beat ends the conversion.
    pub(super) fn timeline_transcode(&self, playback: &str, key: &str, state: &str, fields: &mut Map<String, Value>) {
        let Some(transcoder) = self.transcoder.as_ref() else {
            return;
        };
        if state == "stopped" {
            transcoder.stop_playback(playback);
            return;
        }
        let Some(session) = transcoder.for_playback(playback).filter(|session| session.plan.rating_key == key) else {
            return;
        };
        session.touch();
        let (width, height) = session.plan.video.as_ref().map_or((0, 0), |video| (video.width, video.height));
        fields.insert(
            "Bandwidths".to_string(),
            json!([{"Bandwidth": [{"time": 0,
                                   "bandwidth": (session.plan.bitrate() as f64 * 1.05).round() as i64,
                                   "resolution": resolution_label(width, height)}]}]),
        );
        if session.errored() {
            fields.insert("terminationCode".to_string(), json!(4006));
            fields.insert("terminationText".to_string(), json!("Conversion failed. The transcoder exited due to an error."));
        }
        fields.insert("TranscodeSession".to_string(), json!([transcode_session_row(&session, &transcoder.hardware(), true)]));
    }

    /// A Now Playing row whose player is watching a conversion: the converted Media and
    /// the `TranscodeSession` after `Session`, as the reference lists it.
    pub(super) fn session_transcode(&self, playback: &str, key: &str, object: &mut Map<String, Value>) {
        let Some(transcoder) = self.transcoder.as_ref() else {
            return;
        };
        let Some(session) = transcoder.for_playback(playback).filter(|session| session.plan.rating_key == key) else {
            return;
        };
        if let Some(media) = object.get("Media").and_then(Value::as_array).and_then(|rows| rows.first()).cloned() {
            object.insert("Media".to_string(), json!([self.converted_media(&media, &session.plan, false)]));
        }
        // A converting session's bandwidth is ~1.05x the OUTPUT (509 for 485 kbps,
        // `scratch_copy_mkv.json`), not direct play's ~2.1x the source.
        if let Some(entry) = object.get_mut("Session").and_then(Value::as_object_mut) {
            entry.insert("bandwidth".to_string(), json!((session.plan.bitrate() as f64 * 1.05).round() as i64));
        }
        object.insert("TranscodeSession".to_string(), transcode_session_row(&session, &transcoder.hardware(), true));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan(copy: bool, duration_ms: i64) -> Plan {
        Plan {
            rating_key: "1".into(),
            url: "http://zurg/dav/movies/a.mkv".into(),
            auth: None,
            duration_ms,
            video: Some(VideoPlan {
                index: 0,
                copy,
                source_codec: "hevc".into(),
                bit_depth: 8,
                hdr: false,
                width: 1280,
                height: 720,
                bitrate: 4000,
                frame_rate: 23.976,
                source_profile: "main".into(),
                source_level: 120,
            }),
            audio: Some(AudioPlan { index: 1, copy: false, source_codec: "eac3".into(), codec: "aac".into(), channels: 2, bitrate: 192, sampling_rate: 48000 }),
            subtitle: None,
            matroska: false,
            text_subtitle: None,
            text_to_srt: false,
            sidecar: None,
            text_burn: None,
            incomplete_segments: true,
            playback: "p".into(),
            protocol: Protocol::Hls,
        }
    }

    #[test]
    fn mpd_matches_the_recorded_shape() {
        // plexscratch's DASH remux of Sintel for Plex Web 4.147.1's request.
        let recorded: Value = serde_json::from_str(include_str!("../../../tests/fixtures/transcode/scratch_dash_copy.json")).unwrap();
        let body = recorded["exchanges"][1]["res_body"].as_str().unwrap();
        let mut copy = plan(true, 1800690);
        copy.protocol = Protocol::Dash;
        if let Some(video) = copy.video.as_mut() {
            video.source_codec = "h264".into();
            video.source_profile = "high".into();
            video.source_level = 40;
            video.width = 1920;
            video.height = 1080;
            video.bitrate = 401;
        }
        if let Some(audio) = copy.audio.as_mut() {
            audio.copy = true;
            audio.channels = 1;
            audio.bitrate = 84;
            audio.sampling_rate = 44100;
        }
        // Byte for byte the reference's, but for `minBufferTime`, which is ours (`mpd`).
        assert_eq!(copy.mpd("rexdashdash-cop"), body.replace("minBufferTime=\"PT10S\"", "minBufferTime=\"PT5S\""));
    }

    #[test]
    fn bounded_requests_follow_the_probe() {
        let video_plan = plan(false, 60_000);
        let joined = |hardware: &Hardware| ffmpeg_args(&video_plan, Path::Software, hardware, 0, std::path::Path::new("/tmp/x")).join(" ");
        assert!(!joined(&Hardware::software()).contains("-request_size"));
        let bounded = Hardware { bounded_http: true, ..Hardware::software() };
        let args = joined(&bounded);
        // Input options: before `-i`, or FFmpeg applies them to the output.
        let at = args.find("-request_size 8388608").expect("bounded requests");
        assert!(args.contains("-initial_request_size 1048576") && at < args.find(" -i ").unwrap(), "{args}");
    }

    #[test]
    fn a_dash_run_keeps_the_titles_clock() {
        let mut dash = plan(false, 60_000);
        dash.protocol = Protocol::Dash;
        assert_eq!(dash.segment_seconds(), 2);
        let joined = ffmpeg_args(&dash, Path::Software, &Hardware::software(), 3, std::path::Path::new("/tmp/x")).join(" ");
        assert!(joined.contains("-ss 6 -i"), "{joined}");
        assert!(joined.contains("-output_ts_offset 6 -f dash -format_options movflags=+frag_discont -seg_duration 2"), "{joined}");
        assert!(joined.contains("/tmp/x/r3/manifest.mpd"), "{joined}");
        // FFmpeg's DASH muxer splits the manifest path on '/' only.
        let output = ffmpeg_args(&dash, Path::Software, &Hardware::software(), 3, &std::env::temp_dir().join("x"));
        assert!(!output.last().unwrap().contains('\\'), "{output:?}");
        assert!(joined.contains("expr:gte(t,n_forced*2)"), "{joined}");
        assert!(!joined.contains("-f segment"));
    }

    #[test]
    fn media_playlist_matches_the_recorded_shape() {
        // plexscratch's copy of Sintel: 1800.69 s in 10 s segments is 180 of 10 and a
        // last one of 1, then ENDLIST.
        let recorded: Value = serde_json::from_str(include_str!("../../../tests/fixtures/transcode/scratch_copy_mkv.json")).unwrap();
        let body = recorded["exchanges"][3]["res_body"].as_str().unwrap();
        assert_eq!(plan(true, 1800690).media_playlist(), body);
    }

    #[test]
    fn master_playlist_matches_the_recorded_shape() {
        let recorded: Value = serde_json::from_str(include_str!("../../../tests/fixtures/transcode/scratch_copy_mkv.json")).unwrap();
        let body = recorded["exchanges"][2]["res_body"].as_str().unwrap();
        let mut copy = plan(true, 1800690);
        if let Some(video) = copy.video.as_mut() {
            video.width = 1920;
            video.height = 1080;
            video.frame_rate = 24.0;
            video.bitrate = 401;
        }
        if let Some(audio) = copy.audio.as_mut() {
            audio.bitrate = 84;
        }
        assert_eq!(copy.master_playlist("rexcapcopy-mkv"), body);
    }

    #[test]
    fn a_seek_restart_cuts_on_absolute_boundaries() {
        let dir = std::path::Path::new("/tmp/x");
        let args = ffmpeg_args(&plan(false, 60_000), Path::Software, &Hardware::software(), 5, dir);
        let joined = args.join(" ");
        assert!(joined.contains("-ss 10 -i"), "{joined}");
        assert!(joined.contains("-segment_start_number 5 -segment_time 2 -initial_offset 10"), "{joined}");
        assert!(!joined.contains("-copyts"));
        assert!(joined.contains("expr:gte(t,n_forced*2)"));
        assert!(!joined.contains("-noaccurate_seek"));
        assert!(!joined.contains("-ss 0"), "a converted audio stream is cut by the decode: {joined}");
        let mut copied = plan(false, 60_000);
        if let Some(audio) = copied.audio.as_mut() {
            audio.copy = true;
        }
        let seek = ffmpeg_args(&copied, Path::Software, &Hardware::software(), 5, dir).join(" ");
        assert!(seek.contains("-c:a copy -ss 0 -sn"), "{seek}");
        let start = ffmpeg_args(&copied, Path::Software, &Hardware::software(), 0, dir).join(" ");
        assert!(!start.contains("-ss"), "{start}");
    }

    #[test]
    fn a_remux_copies_and_seeks_to_the_keyframe() {
        let args = ffmpeg_args(&plan(true, 60_000), Path::Software, &Hardware::software(), 2, std::path::Path::new("/tmp/x")).join(" ");
        assert!(args.contains("-c:v copy"));
        assert!(args.contains("-ss 20 -noaccurate_seek"));
        assert!(!args.contains("force_key_frames"));
    }

    #[test]
    fn the_device_path_keeps_frames_on_the_device() {
        let hardware = Hardware { accel: Accel::Vaapi("/dev/dri/renderD128".into()), decode: HashSet::from(["hevc".to_string()]), encode: true, tonemapx: true, opencl_tonemap: false, bounded_http: false, alpha_subtitles: false };
        let video_plan = plan(false, 60_000);
        assert_eq!(ladder(&video_plan, &hardware), vec![Path::Full, Path::EncodeOnly, Path::Software]);
        let full = ffmpeg_args(&video_plan, Path::Full, &hardware, 0, std::path::Path::new("/tmp/x")).join(" ");
        assert!(full.contains("-hwaccel_output_format vaapi"));
        assert!(full.contains("scale_vaapi=w=1280:h=720:format=nv12"));
        assert!(full.contains("h264_vaapi"));
        let upload = ffmpeg_args(&video_plan, Path::EncodeOnly, &hardware, 0, std::path::Path::new("/tmp/x")).join(" ");
        assert!(!upload.contains("-hwaccel vaapi"));
        assert!(upload.contains("format=nv12,hwupload,scale_vaapi"));
        // A codec the device cannot decode skips the full path.
        let mut av1 = plan(false, 60_000);
        av1.video.as_mut().unwrap().source_codec = "av1".into();
        assert_eq!(ladder(&av1, &hardware), vec![Path::EncodeOnly, Path::Software]);
        // HDR is tonemapped on the CPU, then uploaded.
        let mut hdr = plan(false, 60_000);
        hdr.video.as_mut().unwrap().hdr = true;
        assert_eq!(ladder(&hdr, &hardware)[0], Path::EncodeOnly);
        assert!(ffmpeg_args(&hdr, Path::EncodeOnly, &hardware, 0, std::path::Path::new("/tmp/x")).join(" ").contains("tonemapx="));
        // Upstream FFmpeg has no tonemapx: the conversion goes on without it.
        let upstream = Hardware { tonemapx: false, ..hardware.clone() };
        assert!(!ffmpeg_args(&hdr, Path::EncodeOnly, &upstream, 0, std::path::Path::new("/tmp/x")).join(" ").contains("tonemapx"));
    }

    #[test]
    fn hdr_stays_on_the_device_where_opencl_tonemaps() {
        let hardware = Hardware { accel: Accel::Vaapi("/dev/dri/renderD128".into()), decode: HashSet::from(["hevc".to_string()]), encode: true, tonemapx: true, opencl_tonemap: true, bounded_http: false, alpha_subtitles: false };
        let mut hdr = plan(false, 60_000);
        hdr.video.as_mut().unwrap().hdr = true;
        // The CPU's tonemapx stays the step down when the device path fails.
        assert_eq!(ladder(&hdr, &hardware), vec![Path::Full, Path::EncodeOnly, Path::Software]);
        let full = ffmpeg_args(&hdr, Path::Full, &hardware, 0, std::path::Path::new("/tmp/x")).join(" ");
        assert!(full.contains("-init_hw_device vaapi=va:/dev/dri/renderD128 -init_hw_device opencl=ocl@va -filter_hw_device va"), "{full}");
        assert!(full.contains(&format!("-vf scale_vaapi=w=1280:h=720:format=p010,{OPENCL_TONEMAP} ")), "{full}");
        assert!(full.contains("h264_vaapi") && !full.contains("tonemapx"), "{full}");
        assert!(ffmpeg_args(&hdr, Path::EncodeOnly, &hardware, 0, std::path::Path::new("/tmp/x")).join(" ").contains("tonemapx="));
        // An SDR source on the same device opens no OpenCL device.
        let sdr = ffmpeg_args(&plan(false, 60_000), Path::Full, &hardware, 0, std::path::Path::new("/tmp/x")).join(" ");
        assert!(!sdr.contains("opencl"), "{sdr}");
        // OpenCL is derived from VA-API only: NVENC keeps HDR off its full path.
        let cuda = Hardware { accel: Accel::Cuda, ..hardware.clone() };
        assert_eq!(ladder(&hdr, &cuda)[0], Path::EncodeOnly);
        // A device whose probe failed to tonemap keeps HDR on the CPU.
        let without = Hardware { opencl_tonemap: false, ..hardware };
        assert_eq!(ladder(&hdr, &without)[0], Path::EncodeOnly);
    }

    #[test]
    fn every_hdr_and_dolby_vision_kind_is_tonemapped() {
        let doc: Value = serde_json::from_str(include_str!("../../../tests/fixtures/transcode/hdr_probes.json")).unwrap();
        for (name, probe) in doc["probes"].as_object().unwrap() {
            let media = crate::mediainfo::build_media(probe, 1, 1, "/library/parts/1/1/file.mp4", "file.mp4", 0, None);
            let video = media["Part"][0]["Stream"].as_array().unwrap().iter().find(|s| s["streamType"] == 1).unwrap().clone();
            assert!(hdr_stream(&video), "{name}: {video}");
        }
        assert!(!hdr_stream(&json!({"streamType": 1, "colorTrc": "bt709"})));
    }

    #[test]
    fn a_ten_bit_bt2020_row_with_no_transfer_is_tonemapped() {
        // rex-fun's row for The.Breadwinner.2026.MULTi.2160p.WEB-DL...DV.HDR.HEVC-KiT, whose file
        // ffprobe reads as smpte2084/bt2020 but zurg pushed without either (2026-10-17).
        let pushed = json!({"streamType": 1, "codec": "hevc", "profile": "main 10", "bitDepth": 10, "colorPrimaries": null, "colorTrc": null, "colorSpace": "bt2020nc", "colorRange": "tv"});
        assert!(hdr_stream(&pushed));
        // A 10-bit SDR encode says nothing of BT.2020 and stays untonemapped.
        assert!(!hdr_stream(&json!({"streamType": 1, "bitDepth": 10, "colorTrc": null, "colorSpace": null})));
        assert!(!hdr_stream(&json!({"streamType": 1, "bitDepth": 10, "colorTrc": "bt709", "colorSpace": "bt2020nc"})));
    }

    #[test]
    fn a_matroska_target_copies_its_text_subtitle_behind_one_header() {
        let recorded: Value = serde_json::from_str(include_str!("../../../tests/fixtures/transcode/subtitles_s1422.json")).unwrap();
        let mut mkv = plan(false, 30_000);
        mkv.matroska = true;
        mkv.text_subtitle = Some(2);
        let dir = std::path::Path::new("/tmp/x");
        let args = ffmpeg_args(&mkv, Path::Software, &Hardware::software(), 0, dir).join(" ");
        assert!(args.contains("-map 0:2 -c:s copy"), "{args}");
        assert!(!args.contains("-sn"), "{args}");
        // The paths are joined with the OS's separator, `\` on Windows.
        assert!(args.contains(&format!("-f segment -segment_format matroska -segment_header_filename {}", dir.join("init-0.m4s").display())), "{args}");
        assert!(!args.contains("mpegts"), "{args}");
        // A seek drops the copied subtitle's packets from before it, as the copied audio's.
        let seek = ffmpeg_args(&mkv, Path::Software, &Hardware::software(), 5, dir).join(" ");
        assert!(seek.contains("-ss 10 -i") && seek.contains("-ss 0 -dn"), "{seek}");
        // The reference's playlist opens with the version and the header's map; the rest is
        // the MPEG-TS one (its 5 s segments are its own choice, as rex's 2 s are).
        let reference = recorded["sessions"]["android"]["index.m3u8"].as_str().unwrap();
        let head = |text: &str| text.lines().take(3).collect::<Vec<_>>().join("\n");
        assert_eq!(head(&mkv.media_playlist()), head(reference));
        assert_eq!(mkv.container(), "mkv");
        assert!(!plan(false, 30_000).media_playlist().contains("EXT-X-MAP"));
    }

    #[test]
    fn an_uploaded_subtitle_is_copied_into_matroska_or_burned_on_the_titles_clock() {
        let sidecar = Sidecar { id: "7".into(), format: "srt".into(), content: "1\n".into() };
        let mut mkv = plan(false, 30_000);
        mkv.matroska = true;
        mkv.sidecar = Some(sidecar.clone());
        let dir = std::path::Path::new("C:/rex/s");
        let copied = ffmpeg_args(&mkv, Path::Software, &Hardware::software(), 5, dir).join(" ");
        assert!(copied.contains(&format!("-ss 10 -i http://zurg/dav/movies/a.mkv -ss 10 -i {}", dir.join("sidecar.srt").display())), "{copied}");
        assert!(copied.contains("-map 1:0 -c:s copy"), "{copied}");
        assert!(!copied.contains("subtitles="), "{copied}");
        let mut ts = plan(false, 30_000);
        ts.sidecar = Some(sidecar);
        let burned = ffmpeg_args(&ts, Path::Software, &Hardware::software(), 5, dir).join(" ");
        assert!(burned.contains("-vf setpts=PTS+10/TB,subtitles=f='C\\:/rex/s/sidecar.srt',setpts=PTS-10/TB,scale="), "{burned}");
        assert_eq!(burned.matches(" -i ").count(), 1, "{burned}");
        assert!(burned.contains("-sn"), "{burned}");
        let start = ffmpeg_args(&ts, Path::Software, &Hardware::software(), 0, dir).join(" ");
        assert!(start.contains("-vf subtitles=f='C\\:/rex/s/sidecar.srt',scale="), "{start}");
        // libass draws on CPU frames: the device decodes and scales, the frame comes down
        // for the text and goes back up for the encoder.
        let hardware = Hardware { accel: Accel::Vaapi("/dev/dri/renderD128".into()), decode: HashSet::from(["h264".to_string(), "hevc".to_string()]), encode: true, tonemapx: true, opencl_tonemap: false, bounded_http: false, alpha_subtitles: false };
        assert_eq!(ladder(&ts, &hardware)[0], Path::Full);
        let full = ffmpeg_args(&ts, Path::Full, &hardware, 5, dir).join(" ");
        assert!(full.contains("-vf scale_vaapi=w=1280:h=720:format=nv12,hwdownload,format=nv12,setpts=PTS+10/TB,subtitles=f='C\\:/rex/s/sidecar.srt',setpts=PTS-10/TB,format=nv12,hwupload -c:v h264_vaapi"), "{full}");
        assert!(ladder(&mkv, &hardware).contains(&Path::Full));
    }

    #[test]
    fn an_embedded_text_subtitle_is_burned_once_it_has_been_read() {
        let dir = std::env::temp_dir().join(format!("rex-text-burn-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let mut ts = plan(false, 30_000);
        ts.text_burn = Some(TextBurn { index: 2, ass: true });
        // Until the track is read the run converts without it.
        let before = ffmpeg_args(&ts, Path::Software, &Hardware::software(), 0, &dir).join(" ");
        assert!(!before.contains("subtitles="), "{before}");
        assert!(before.contains("-sn"), "{before}");
        std::fs::write(dir.join("embedded.ass"), "[Script Info]\n").unwrap();
        // As the filter takes it: `/` on Windows too, `:` escaped.
        let file = filter_path(&dir.join("embedded.ass").to_string_lossy());
        let after = ffmpeg_args(&ts, Path::Software, &Hardware::software(), 0, &dir).join(" ");
        assert!(after.contains(&format!("-vf subtitles=f='{file}',scale=")), "{after}");
        assert!(after.contains("-sn") && after.matches(" -i ").count() == 1, "{after}");
        let cuda = Hardware { accel: Accel::Cuda, decode: HashSet::from(["hevc".to_string()]), encode: true, tonemapx: false, opencl_tonemap: false, bounded_http: false, alpha_subtitles: false };
        let full = ffmpeg_args(&ts, Path::Full, &cuda, 0, &dir).join(" ");
        assert!(full.contains(&format!("scale_cuda=w=1280:h=720:format=nv12,hwdownload,format=nv12,subtitles=f='{file}',format=nv12,hwupload_cuda")), "{full}");
        // With jellyfin-ffmpeg's canvas the frames stay on the device, the text laid over them.
        let vaapi = Hardware { accel: Accel::Vaapi("/dev/dri/renderD128".into()), decode: HashSet::from(["hevc".to_string()]), encode: true, tonemapx: true, opencl_tonemap: false, bounded_http: false, alpha_subtitles: true };
        let overlay = ffmpeg_args(&ts, Path::Full, &vaapi, 5, &dir).join(" ");
        assert!(overlay.contains(&format!(
            "-filter_complex [0:0]scale_vaapi=w=1280:h=720:format=nv12[main];alphasrc=s=1280x720:r=10:start=0,format=yuva420p,setpts=PTS+10/TB,subtitles=f='{file}':alpha=1:sub2video=1,setpts=PTS-10/TB,hwupload[sub];[main][sub]overlay_vaapi=eof_action=pass:repeatlast=0[v] -map [v] -map 0:1"
        )), "{overlay}");
        assert!(!overlay.contains("hwdownload") && !overlay.contains("-vf ") && !overlay.contains("-map 0:0"), "{overlay}");
        // Decoded on the CPU, the frame is drawn on before its upload, as before.
        let upload = ffmpeg_args(&ts, Path::EncodeOnly, &vaapi, 0, &dir).join(" ");
        assert!(upload.contains(&format!("-vf subtitles=f='{file}',format=nv12,hwupload,scale_vaapi")), "{upload}");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_burned_subtitle_is_overlaid_before_the_scale_and_upload() {
        let hardware = Hardware { accel: Accel::Vaapi("/dev/dri/renderD128".into()), decode: HashSet::from(["hevc".to_string()]), encode: true, tonemapx: true, opencl_tonemap: false, bounded_http: false, alpha_subtitles: false };
        let mut burned = plan(false, 60_000);
        burned.subtitle = Some(3);
        assert_eq!(ladder(&burned, &hardware), vec![Path::EncodeOnly, Path::Software]);
        let soft = ffmpeg_args(&burned, Path::Software, &Hardware::software(), 0, std::path::Path::new("/tmp/x")).join(" ");
        assert!(soft.contains("-filter_complex [0:0][0:3]overlay=eof_action=pass,scale=1280:720:flags=bicubic,format=yuv420p[v] -map [v] -map 0:1"), "{soft}");
        assert!(!soft.contains("-vf ") && !soft.contains("-map 0:0"), "{soft}");
        let upload = ffmpeg_args(&burned, Path::EncodeOnly, &hardware, 0, std::path::Path::new("/tmp/x")).join(" ");
        assert!(upload.contains("overlay=eof_action=pass,format=nv12,hwupload,scale_vaapi"), "{upload}");
    }

    #[test]
    fn resolution_labels() {
        assert_eq!(resolution_label(1920, 1080), "1080p");
        assert_eq!(resolution_label(718, 404), "SD");
        assert_eq!(resolution_label(1280, 720), "720p");
        assert_eq!(resolution_label(3840, 2160), "4k");
    }
}
