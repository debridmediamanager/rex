//! zurg: where a Part's bytes come from and what a library scan lists.
//!
//! plexsim reads no media itself. A Part's `file` is the path zurg's own mount gives the
//! file (`<mount>/<directory>/<release>/<file>`), which is what zurg's Plex integration
//! sends as a scan's `path=` and reads back from each Part when it matches releases to
//! ratingKeys. The same file is `/dav/<directory>/<release>/<file>` on zurg's WebDAV,
//! which answers a Range GET with the bytes -- the path rclone's mount reads through, so
//! archives, failover and every provider are zurg's code rather than a copy of it.
//!
//! `ZURG_URL` (with `ZURG_USER`/`ZURG_PASSWORD` when zurg asks for basic auth) and
//! `--zurg-mount` (zurg's `mount_path`) configure it.

use super::upstream;
use super::*;
use std::time::Duration;

#[derive(Debug, Clone)]
pub(crate) struct Zurg {
    base: String,
    /// `Basic <base64>`; never logged.
    auth: Option<String>,
    mount: String,
}

/// What a WebDAV listing holds: sub-folders and files (path, size), as `/dav/...`
/// paths with each segment decoded.
#[derive(Debug, Default, Clone, PartialEq)]
pub(super) struct Listing {
    pub folders: Vec<String>,
    pub files: Vec<(String, u64)>,
}

#[derive(Debug, Clone, PartialEq)]
pub(super) enum ListError {
    /// zurg says the folder is not there: a release that was deleted or renamed.
    Gone,
    /// zurg is still loading its library (503): ask again shortly. Never "all gone".
    Loading,
    /// Anything else -- zurg still loading (503), a refused credential, the network.
    /// A scan that cannot list must change nothing.
    Failed(String),
}

/// One path segment as a URL carries it: unreserved bytes pass, every other is `%XX`
/// (a `+` in a release name is a literal plus, never a space).
fn quote_segment(segment: &str) -> String {
    let mut out = String::new();
    for byte in segment.bytes() {
        if byte.is_ascii_alphanumeric() || b"-._~".contains(&byte) {
            out.push(byte as char);
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

/// A path's `%XX` escapes decoded once; `+` stays a plus.
fn unquote(path: &str) -> String {
    let bytes = path.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).unwrap_or("");
            if let Ok(value) = u8::from_str_radix(hex, 16) {
                out.push(value);
                index += 3;
                continue;
            }
        }
        out.push(bytes[index]);
        index += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn xml_unescape(text: &str) -> String {
    text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&#39;", "'")
        .replace("&amp;", "&")
}

/// The extensions a scan takes as media; anything else in a release (subtitles, nfo,
/// samples' images) is not a Part.
pub(super) fn is_video(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    [
        ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".mts", ".webm", ".wmv",
        ".mpg", ".mpeg", ".flv", ".divx", ".iso",
    ]
    .iter()
    .any(|extension| lower.ends_with(extension))
}

impl Zurg {
    pub(crate) fn new(base: &str, user: &str, password: &str, mount: &str) -> Self {
        let auth = (!user.is_empty() || !password.is_empty())
            .then(|| format!("Basic {}", base64_encode(format!("{user}:{password}").as_bytes())));
        let mount = mount.trim_end_matches('/');
        Self {
            base: base.trim_end_matches('/').to_string(),
            auth,
            mount: if mount.is_empty() { "/".to_string() } else { mount.to_string() },
        }
    }

    /// From `ZURG_URL`, `ZURG_USER` and `ZURG_PASSWORD`; none without a URL.
    pub(crate) fn from_env(mount: &str) -> Option<Self> {
        let base = std::env::var("ZURG_URL").ok().filter(|url| !url.is_empty())?;
        let user = std::env::var("ZURG_USER").unwrap_or_default();
        let password = std::env::var("ZURG_PASSWORD").unwrap_or_default();
        Some(Self::new(&base, &user, &password, mount))
    }

    /// `<mount>/a/b` -> `/dav/a/b`, decoded; `None` for a path outside the mount.
    pub(super) fn dav_path(&self, file: &str) -> Option<String> {
        let rest = if self.mount == "/" {
            file.strip_prefix('/')?
        } else {
            file.strip_prefix(&self.mount)?.strip_prefix('/')?
        };
        let rest = rest.trim_end_matches('/');
        (!rest.is_empty()).then(|| format!("/dav/{rest}"))
    }

    /// `/dav/a/b` -> `<mount>/a/b`.
    pub(super) fn file_path(&self, dav: &str) -> String {
        let rest = dav.strip_prefix("/dav/").unwrap_or(dav).trim_matches('/');
        if self.mount == "/" {
            format!("/{rest}")
        } else {
            format!("{}/{rest}", self.mount)
        }
    }

    /// The URL of a decoded `/dav/...` path, each segment escaped.
    pub(super) fn url(&self, dav: &str) -> String {
        let escaped = dav
            .split('/')
            .map(quote_segment)
            .collect::<Vec<_>>()
            .join("/");
        format!("{}{escaped}", self.base)
    }

    pub(super) fn headers(&self) -> Vec<(&'static str, String)> {
        let mut headers = vec![("User-Agent", upstream::UPSTREAM_UA.to_string())];
        if let Some(auth) = &self.auth {
            headers.push(("Authorization", auth.clone()));
        }
        headers
    }

    /// Open a file's bytes: a GET with the caller's headers (a `Range`) and zurg's auth.
    pub(super) fn open(
        &self,
        dav: &str,
        extra: &[(&'static str, String)],
    ) -> Result<upstream::Upstream, String> {
        let mut headers = self.headers();
        headers.extend(extra.iter().cloned());
        upstream::open("GET", &self.url(dav), &headers, b"", Duration::from_secs(30))
    }

    /// `checkFiles`: whether zurg still serves the file. Only zurg's own 404 is "gone";
    /// a transport failure or any other answer is not collapsed into a missing file.
    pub(super) fn exists(&self, dav: &str) -> bool {
        match upstream::open("HEAD", &self.url(dav), &self.headers(), b"", Duration::from_secs(15)) {
            Ok(response) => response.status != 404,
            Err(_) => true,
        }
    }

    /// One WebDAV level (`PROPFIND`, `Depth: 1`) of a decoded `/dav/...` folder.
    pub(super) fn list(&self, dav: &str) -> Result<Listing, ListError> {
        let folder = format!("{}/", dav.trim_end_matches('/'));
        let mut headers = self.headers();
        headers.push(("Depth", "1".to_string()));
        let (status, _, body) =
            upstream::fetch("PROPFIND", &self.url(&folder), &headers, b"", Duration::from_secs(120))
                .map_err(ListError::Failed)?;
        match status {
            207 => Ok(parse_multistatus(&String::from_utf8_lossy(&body), &folder)),
            404 => Err(ListError::Gone),
            503 => Err(ListError::Loading),
            other => Err(ListError::Failed(format!("PROPFIND {folder} answered {other}"))),
        }
    }

    /// Every media file under a folder, folders followed `depth` levels down; a file
    /// path lists itself. `Gone` only when the folder itself is gone.
    pub(super) fn walk(&self, dav: &str, depth: usize) -> Result<Vec<(String, u64)>, ListError> {
        let listing = self.list(dav)?;
        let mut files: Vec<(String, u64)> =
            listing.files.into_iter().filter(|(path, _)| is_video(path)).collect();
        if depth > 0 {
            for folder in listing.folders {
                match self.walk(&folder, depth - 1) {
                    Ok(found) => files.extend(found),
                    // A release can vanish between the two listings.
                    Err(ListError::Gone) => {}
                    Err(error) => return Err(error),
                }
            }
        }
        Ok(files)
    }

    /// zurg's `/api/library/entry` for a decoded `/dav/...` path: the release's hash and
    /// name, the IMDb id zurg identified it as, and the file's stored ffprobe.
    pub(super) fn entry(&self, dav: &str) -> Result<Value, String> {
        let escaped = dav
            .split('/')
            .map(quote_segment)
            .collect::<Vec<_>>()
            .join("/");
        let url = format!("{}/api/library/entry?path={}", self.base, quote_segment(&escaped));
        let (status, _, body) = upstream::fetch("GET", &url, &self.headers(), b"", Duration::from_secs(60))?;
        if status != 200 {
            return Err(format!("library entry for {dav} answered {status}"));
        }
        serde_json::from_slice(&body).map_err(|error| format!("library entry for {dav}: {error}"))
    }

    /// Where `--part-mode redirect` sends a client: zurg's signed `.strm` URL for the
    /// file needs no credential and answers with the provider's own download URL, so
    /// the client is sent there, as it was sent to the debrid CDN before. `None` when zurg
    /// streams the file itself (an archive member, a Usenet release): then plexsim proxies.
    pub(super) fn redirect_target(&self, dav: &str) -> Option<String> {
        let signed = self.entry(dav).ok()?;
        let signed = signed.get("signed_path").and_then(Value::as_str)?.to_string();
        let response = upstream::open_once(
            "GET",
            &format!("{}{signed}", self.base),
            &[("User-Agent", upstream::UPSTREAM_UA.to_string())],
            Duration::from_secs(30),
        )
        .ok()?;
        matches!(response.status, 301 | 302 | 303 | 307 | 308)
            .then(|| response.header("location").map(str::to_string))
            .flatten()
            .filter(|location| location.starts_with("http"))
    }
}

/// The `<d:response>` rows of a PROPFIND answer, minus the folder's own row.
fn parse_multistatus(body: &str, folder: &str) -> Listing {
    static ROW: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    static HREF: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    static SIZE: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    static FOLDER: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let row = ROW.get_or_init(|| {
        regex::Regex::new(r"(?s)<(?:[A-Za-z]+:)?response\b[^>]*>(.*?)</(?:[A-Za-z]+:)?response>").unwrap()
    });
    let href = HREF.get_or_init(|| {
        regex::Regex::new(r"(?s)<(?:[A-Za-z]+:)?href\b[^>]*>(.*?)</(?:[A-Za-z]+:)?href>").unwrap()
    });
    let size = SIZE.get_or_init(|| {
        regex::Regex::new(r"<(?:[A-Za-z]+:)?getcontentlength\b[^>]*>\s*(\d+)\s*<").unwrap()
    });
    let collection = FOLDER.get_or_init(|| {
        regex::Regex::new(r"<(?:[A-Za-z]+:)?collection\b").unwrap()
    });
    let own = folder.trim_end_matches('/');
    let mut listing = Listing::default();
    for found in row.captures_iter(body) {
        let block = &found[1];
        let Some(raw) = href.captures(block).map(|c| xml_unescape(c[1].trim())) else {
            continue;
        };
        // An absolute href names its host; only the path is wanted.
        let path = match raw.find("://") {
            Some(at) => raw[at + 3..].find('/').map_or("/".to_string(), |slash| raw[at + 3 + slash..].to_string()),
            None => raw,
        };
        let path = unquote(&path);
        let trimmed = path.trim_end_matches('/').to_string();
        if trimmed == own || trimmed.is_empty() {
            continue;
        }
        if collection.is_match(block) {
            listing.folders.push(trimmed);
        } else {
            let bytes = size
                .captures(block)
                .and_then(|c| c[1].parse::<u64>().ok())
                .unwrap_or(0);
            listing.files.push((trimmed, bytes));
        }
    }
    listing
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mount_and_dav_paths_are_the_same_file() {
        let zurg = Zurg::new("http://127.0.0.1:9999/", "", "", "/mnt/zurg/");
        let file = "/mnt/zurg/movies/A+B (2020)/A+B.mkv";
        let dav = zurg.dav_path(file).unwrap();
        assert_eq!(dav, "/dav/movies/A+B (2020)/A+B.mkv");
        assert_eq!(zurg.file_path(&dav), file);
        assert_eq!(zurg.url(&dav), "http://127.0.0.1:9999/dav/movies/A%2BB%20%282020%29/A%2BB.mkv");
        assert_eq!(zurg.dav_path("/plexsim/1/movie.mkv"), None);
        assert_eq!(zurg.dav_path("/mnt/zurgish/x.mkv"), None);
    }

    /// The shape zurg writes (`pkg/dav/response.go`), hrefs rooted by its middleware.
    #[test]
    fn a_zurg_listing_parses_into_folders_and_files() {
        let body = "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<d:multistatus xmlns:d=\"DAV:\">\
<d:response>\n\t<d:href>/dav/movies/</d:href>\n\t<d:propstat>\n\t\t<d:prop>\n\t\t\t<d:resourcetype>\n\t\t\t\t<d:collection/>\n\t\t\t</d:resourcetype>\n\t\t\t<d:getlastmodified>Mon, 02 Jan 2026 15:04:05 GMT</d:getlastmodified>\n\t\t</d:prop>\n\t\t<d:status>HTTP/1.1 200 OK</d:status>\n\t</d:propstat>\n</d:response>\
<d:response>\n\t<d:href>/dav/movies/A%2BB%20%26%20C/</d:href>\n\t<d:propstat>\n\t\t<d:prop>\n\t\t\t<d:resourcetype>\n\t\t\t\t<d:collection/>\n\t\t\t</d:resourcetype>\n\t\t\t<d:getlastmodified>Mon, 02 Jan 2026 15:04:05 GMT</d:getlastmodified>\n\t\t</d:prop>\n\t\t<d:status>HTTP/1.1 200 OK</d:status>\n\t</d:propstat>\n</d:response>\
<d:response>\n\t<d:href>/dav/movies/Film.2020.mkv</d:href>\n\t<d:propstat>\n\t\t<d:prop>\n\t\t\t<d:getcontentlength>1234</d:getcontentlength>\n\t\t\t<d:getlastmodified>Mon, 02 Jan 2026 15:04:05 GMT</d:getlastmodified>\n\t\t\t<d:resourcetype></d:resourcetype>\n\t\t</d:prop>\n\t\t<d:status>HTTP/1.1 200 OK</d:status>\n\t</d:propstat>\n</d:response></d:multistatus>";
        let listing = parse_multistatus(body, "/dav/movies/");
        assert_eq!(listing.folders, vec!["/dav/movies/A+B & C".to_string()]);
        assert_eq!(listing.files, vec![("/dav/movies/Film.2020.mkv".to_string(), 1234)]);
    }
}
