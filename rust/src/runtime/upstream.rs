//! The server's OUTBOUND HTTP: zurg (a Part's bytes, a library scan), the provider URL
//! zurg redirects to, TMDB, Plex's metadata provider and remote artwork.
//!
//! The Python server reaches all of them through `urllib`, which speaks TLS against the
//! system roots and follows redirects; the native runtime could only write a plain
//! `http://` request by hand, so every `https://` one was unreachable. Plain `http://`
//! stays: zurg is usually on the same host, and `tests/test_part_bytes.py` stands its
//! zurg stub up on a local plain port.

use super::*;
use rustls::pki_types::ServerName;
use rustls::{ClientConfig, ClientConnection, RootCertStore};
use std::net::ToSocketAddrs;
use std::sync::OnceLock;
use std::time::Duration;

/// `server.py`'s `UPSTREAM_UA`, sent on artwork and part reads.
pub(super) const UPSTREAM_UA: &str = "rex/0.1";

/// The roots urllib uses on the Linux hosts rex runs on. Loaded once.
fn client_config() -> Option<Arc<ClientConfig>> {
    static CONFIG: OnceLock<Option<Arc<ClientConfig>>> = OnceLock::new();
    CONFIG
        .get_or_init(|| {
            let mut roots = RootCertStore::empty();
            for bundle in ["/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem"] {
                let Ok(pem) = fs::read(bundle) else {
                    continue;
                };
                for cert in certs(&mut pem.as_slice()).flatten() {
                    let _ = roots.add(cert);
                }
                if !roots.is_empty() {
                    break;
                }
            }
            if roots.is_empty() {
                return None;
            }
            Some(Arc::new(
                ClientConfig::builder()
                    .with_root_certificates(roots)
                    .with_no_client_auth(),
            ))
        })
        .clone()
}

enum Conn {
    Plain(TcpStream),
    Tls(Box<StreamOwned<ClientConnection, TcpStream>>),
}

impl Read for Conn {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Conn::Plain(stream) => stream.read(buf),
            // A peer that closes without close_notify is an ordinary end of body for a
            // close-delimited response; urllib reads it as one.
            Conn::Tls(stream) => match stream.read(buf) {
                Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => Ok(0),
                other => other,
            },
        }
    }
}

impl Write for Conn {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            Conn::Plain(stream) => stream.write(buf),
            Conn::Tls(stream) => stream.write(buf),
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        match self {
            Conn::Plain(stream) => stream.flush(),
            Conn::Tls(stream) => stream.flush(),
        }
    }
}

enum Framing {
    Length(u64),
    Chunked { left: u64, done: bool },
    Close,
}

/// One upstream response whose head has been read; the body is read through `Read`,
/// de-chunked and bounded by its declared length.
pub(super) struct Upstream {
    pub status: u16,
    /// The status line's reason phrase, which `urllib.error.HTTPError` puts in its text.
    pub reason: String,
    pub headers: Vec<(String, String)>,
    reader: BufReader<Conn>,
    framing: Framing,
}

impl Upstream {
    pub(super) fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }

    /// The whole body, bounded so a misbehaving upstream cannot exhaust memory.
    pub(super) fn read_body(&mut self, limit: usize) -> Result<Vec<u8>, String> {
        let mut body = Vec::new();
        let mut chunk = [0_u8; 64 * 1024];
        loop {
            let count = self.read(&mut chunk).map_err(|error| error.to_string())?;
            if count == 0 {
                return Ok(body);
            }
            if body.len() + count > limit {
                return Err("upstream body too large".to_string());
            }
            body.extend_from_slice(&chunk[..count]);
        }
    }
}

fn read_line(reader: &mut BufReader<Conn>) -> io::Result<String> {
    let mut line = Vec::new();
    std::io::BufRead::read_until(reader, b'\n', &mut line)?;
    if line.len() > 64 * 1024 {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "header line too long"));
    }
    Ok(String::from_utf8_lossy(&line)
        .trim_end_matches(['\r', '\n'])
        .to_string())
}

impl Read for Upstream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if buf.is_empty() {
            return Ok(0);
        }
        match &mut self.framing {
            Framing::Close => self.reader.read(buf),
            Framing::Length(left) => {
                if *left == 0 {
                    return Ok(0);
                }
                let want = buf.len().min(usize::try_from(*left).unwrap_or(usize::MAX));
                let count = self.reader.read(&mut buf[..want])?;
                *left -= count as u64;
                Ok(count)
            }
            Framing::Chunked { left, done } => {
                if *done {
                    return Ok(0);
                }
                if *left == 0 {
                    let line = read_line(&mut self.reader)?;
                    let size = u64::from_str_radix(line.split(';').next().unwrap_or("").trim(), 16)
                        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "bad chunk size"))?;
                    if size == 0 {
                        // Trailers, up to the blank line.
                        while !read_line(&mut self.reader)?.is_empty() {}
                        *done = true;
                        return Ok(0);
                    }
                    *left = size;
                }
                let want = buf.len().min(usize::try_from(*left).unwrap_or(usize::MAX));
                let count = self.reader.read(&mut buf[..want])?;
                if count == 0 {
                    return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "chunk cut short"));
                }
                *left -= count as u64;
                if *left == 0 {
                    read_line(&mut self.reader)?;
                }
                Ok(count)
            }
        }
    }
}

struct Target {
    tls: bool,
    host: String,
    port: u16,
    path: String,
}

fn parse_url(url: &str) -> Option<Target> {
    let (tls, rest) = if let Some(rest) = url.strip_prefix("https://") {
        (true, rest)
    } else if let Some(rest) = url.strip_prefix("http://") {
        (false, rest)
    } else {
        return None;
    };
    let (authority, path) = match rest.find(['/', '?']) {
        Some(index) if rest.as_bytes()[index] == b'/' => (&rest[..index], rest[index..].to_string()),
        Some(index) => (&rest[..index], format!("/{}", &rest[index..])),
        None => (rest, "/".to_string()),
    };
    let authority = authority.rsplit_once('@').map_or(authority, |(_, host)| host);
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) if !host.is_empty() => (host.to_string(), port.parse::<u16>().ok()?),
        _ => (authority.to_string(), if tls { 443 } else { 80 }),
    };
    if host.is_empty() {
        return None;
    }
    Some(Target { tls, host, port, path })
}

fn connect(target: &Target, timeout: Duration) -> Result<Conn, String> {
    let addresses = (target.host.as_str(), target.port)
        .to_socket_addrs()
        .map_err(|error| format!("resolve {}: {error}", target.host))?;
    let mut last = format!("no address for {}", target.host);
    for address in addresses {
        match TcpStream::connect_timeout(&address, timeout) {
            Ok(stream) => {
                let _ = stream.set_read_timeout(Some(timeout));
                let _ = stream.set_write_timeout(Some(timeout));
                let _ = stream.set_nodelay(true);
                if !target.tls {
                    return Ok(Conn::Plain(stream));
                }
                let config = client_config().ok_or("no system certificate roots")?;
                let name = ServerName::try_from(target.host.clone())
                    .map_err(|error| error.to_string())?;
                let connection =
                    ClientConnection::new(config, name).map_err(|error| error.to_string())?;
                return Ok(Conn::Tls(Box::new(StreamOwned::new(connection, stream))));
            }
            Err(error) => last = error.to_string(),
        }
    }
    Err(last)
}

fn exchange(
    method: &str,
    target: &Target,
    headers: &[(&str, String)],
    body: &[u8],
    timeout: Duration,
) -> Result<Upstream, String> {
    let mut conn = connect(target, timeout)?;
    let default_port = if target.tls { 443 } else { 80 };
    let host = if target.port == default_port {
        target.host.clone()
    } else {
        format!("{}:{}", target.host, target.port)
    };
    let mut head = format!("{method} {} HTTP/1.1\r\nHost: {host}\r\n", target.path);
    for (name, value) in headers {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    if !body.is_empty() || matches!(method, "POST" | "PUT") {
        head.push_str(&format!("Content-Length: {}\r\n", body.len()));
    }
    head.push_str("Connection: close\r\n\r\n");
    let mut request = head.into_bytes();
    request.extend_from_slice(body);
    conn.write_all(&request).map_err(|error| error.to_string())?;
    conn.flush().map_err(|error| error.to_string())?;
    let mut reader = BufReader::with_capacity(256 * 1024, conn);
    let status_line = read_line(&mut reader).map_err(|error| error.to_string())?;
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| format!("bad upstream status line {status_line:?}"))?;
    let mut response_headers = Vec::new();
    loop {
        let line = read_line(&mut reader).map_err(|error| error.to_string())?;
        if line.is_empty() {
            break;
        }
        if response_headers.len() > 200 {
            return Err("too many upstream headers".to_string());
        }
        if let Some((name, value)) = line.split_once(':') {
            response_headers.push((name.trim().to_string(), value.trim().to_string()));
        }
    }
    let find = |name: &str| {
        response_headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.clone())
    };
    let framing = if method == "HEAD" || status == 204 || status == 304 || (100..200).contains(&status)
    {
        Framing::Length(0)
    } else if find("transfer-encoding").is_some_and(|value| value.to_ascii_lowercase().contains("chunked")) {
        Framing::Chunked { left: 0, done: false }
    } else if let Some(length) = find("content-length").and_then(|value| value.parse::<u64>().ok()) {
        Framing::Length(length)
    } else {
        Framing::Close
    };
    let reason = status_line
        .splitn(3, ' ')
        .nth(2)
        .unwrap_or_default()
        .trim()
        .to_string();
    Ok(Upstream {
        status,
        reason,
        headers: response_headers,
        reader,
        framing,
    })
}

/// Open `url` and read the response head. A GET follows redirects the way urllib's
/// default opener does (301/302/303/307/308, at most ten hops); any other method gets
/// the redirect back as its answer.
pub(super) fn open(
    method: &str,
    url: &str,
    headers: &[(&str, String)],
    body: &[u8],
    timeout: Duration,
) -> Result<Upstream, String> {
    let mut url = url.to_string();
    let origin = parse_url(&url).map(|target| (target.tls, target.host, target.port));
    let mut headers = headers.to_vec();
    for _ in 0..10 {
        let target = parse_url(&url).ok_or_else(|| format!("unsupported url {url:?}"))?;
        // A source's credential is for the source: a redirect to another origin (a CDN, a
        // presigned link) goes without it, as curl and browsers send it. S3 refuses a
        // presigned URL that also carries one ("Only one auth mechanism allowed").
        if origin.as_ref() != Some(&(target.tls, target.host.clone(), target.port)) {
            headers.retain(|(name, _)| !name.eq_ignore_ascii_case("authorization"));
        }
        let response = exchange(method, &target, &headers, body, timeout)?;
        let redirect = matches!(response.status, 301 | 302 | 303 | 307 | 308)
            && matches!(method, "GET" | "HEAD");
        let Some(location) = response.header("location").filter(|_| redirect) else {
            return Ok(response);
        };
        url = if location.starts_with("http://") || location.starts_with("https://") {
            location.to_string()
        } else if location.starts_with('/') {
            format!(
                "{}://{}:{}{}",
                if target.tls { "https" } else { "http" },
                target.host,
                target.port,
                location
            )
        } else {
            return Ok(response);
        };
    }
    Err("too many upstream redirects".to_string())
}

/// One exchange, redirects NOT followed: what a server answers, `Location` and all.
pub(super) fn open_once(
    method: &str,
    url: &str,
    headers: &[(&str, String)],
    timeout: Duration,
) -> Result<Upstream, String> {
    let target = parse_url(url).ok_or_else(|| format!("unsupported url {url:?}"))?;
    exchange(method, &target, headers, b"", timeout)
}

/// `open` plus the whole body: `(status, headers, body)`.
pub(crate) fn fetch(
    method: &str,
    url: &str,
    headers: &[(&str, String)],
    body: &[u8],
    timeout: Duration,
) -> Result<(u16, Vec<(String, String)>, Vec<u8>), String> {
    let mut response = open(method, url, headers, body, timeout)?;
    let payload = response.read_body(64 * 1024 * 1024)?;
    Ok((response.status, response.headers, payload))
}

/// `urllib.parse.urlencode`'s escaper (`quote_plus`): unreserved bytes pass, a space
/// is `+`, everything else is `%XX`.
pub(super) fn quote_plus(value: &str) -> String {
    let mut out = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || b"_.-~".contains(&byte) {
            out.push(byte as char);
        } else if byte == b' ' {
            out.push('+');
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}
