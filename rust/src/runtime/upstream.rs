//! The server's OUTBOUND HTTP: zurg (a Part's bytes, a library scan), the provider URL
//! zurg redirects to, TMDB, Plex's metadata provider and remote artwork.
//!
//! The Python server reaches all of them through `urllib`, which speaks TLS against the
//! system roots and follows redirects; the native runtime could only write a plain
//! `http://` request by hand, so every `https://` one was unreachable. Plain `http://`
//! stays: zurg is usually on the same host, and `tests/test_part_bytes.py` stands its
//! zurg stub up on a local plain port.

use super::*;
#[cfg(windows)]
use rustls::pki_types::CertificateDer;
use rustls::pki_types::ServerName;
use rustls::{ClientConfig, ClientConnection, RootCertStore};
use std::net::ToSocketAddrs;
use std::sync::OnceLock;
use std::time::Duration;

/// `server.py`'s `UPSTREAM_UA`, sent on artwork and part reads.
pub(super) const UPSTREAM_UA: &str = "rex/0.1";
/// Shared cap for complete upstream responses, including responses decoded by plex.tv.
pub(crate) const MAX_BODY_BYTES: usize = 64 * 1024 * 1024;

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
            #[cfg(windows)]
            if roots.is_empty() {
                if let Ok(certificates) = crate::platform::system_root_certificates() {
                    for certificate in certificates {
                        let _ = roots.add(CertificateDer::from(certificate));
                    }
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
    // `read_until` otherwise keeps growing its destination until a newline or EOF.  The
    // Take wrapper limits both the bytes read and the allocation when a peer leaves a
    // malformed header unterminated.
    let mut limited = reader.take(64 * 1024 + 1);
    std::io::BufRead::read_until(&mut limited, b'\n', &mut line)?;
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
                if count == 0 {
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "response body shorter than Content-Length",
                    ));
                }
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
    let (scheme, rest) = url.split_once("://")?;
    let tls = if scheme.eq_ignore_ascii_case("https") {
        true
    } else if scheme.eq_ignore_ascii_case("http") {
        false
    } else {
        return None;
    };
    let (authority, path) = match rest.find(['/', '?']) {
        Some(index) if rest.as_bytes()[index] == b'/' => (&rest[..index], rest[index..].to_string()),
        Some(index) => (&rest[..index], format!("/{}", &rest[index..])),
        None => (rest, "/".to_string()),
    };
    let authority = authority.rsplit_once('@').map_or(authority, |(_, host)| host);
    let (host, port) = if let Some(bracketed) = authority.strip_prefix('[') {
        let end = bracketed.find(']')?;
        let host = &bracketed[..end];
        if host.is_empty() {
            return None;
        }
        let suffix = &bracketed[end + 1..];
        let port = if suffix.is_empty() {
            if tls { 443 } else { 80 }
        } else {
            suffix.strip_prefix(':')?.parse::<u16>().ok()?
        };
        (host.to_ascii_lowercase(), port)
    } else {
        match authority.rsplit_once(':') {
            Some((host, port)) if !host.is_empty() => (host.to_ascii_lowercase(), port.parse::<u16>().ok()?),
            _ => (authority.to_ascii_lowercase(), if tls { 443 } else { 80 }),
        }
    };
    if host.is_empty() {
        return None;
    }
    Some(Target { tls, host, port, path })
}

/// Whether `url` is https, and its host (lowercase, IPv6 unbracketed); None for no URL `fetch` takes.
pub(super) fn scheme_host(url: &str) -> Option<(bool, String)> {
    parse_url(url).map(|target| (target.tls, target.host))
}

fn normalize_path(path: &str) -> String {
    // RFC 3986 section 5.2.4, kept as a string operation so encoded data and
    // repeated slashes remain untouched.
    let mut input = path.to_string();
    let mut output = String::new();
    while !input.is_empty() {
        if input.starts_with("../") {
            input.drain(..3);
        } else if input.starts_with("./") {
            input.drain(..2);
        } else if input.starts_with("/./") {
            input.replace_range(..3, "/");
        } else if input == "/." {
            input.replace_range(..2, "/");
        } else if input.starts_with("/../") {
            input.replace_range(..4, "/");
            if let Some(last) = output.rfind('/') {
                output.truncate(last);
            } else {
                output.clear();
            }
        } else if input == "/.." {
            input.replace_range(..3, "/");
            if let Some(last) = output.rfind('/') {
                output.truncate(last);
            } else {
                output.clear();
            }
        } else if input == "." || input == ".." {
            input.clear();
        } else {
            let end = if input.starts_with('/') {
                input[1..].find('/').map_or(input.len(), |index| index + 1)
            } else {
                input.find('/').unwrap_or(input.len())
            };
            output.push_str(&input[..end]);
            input.drain(..end);
        }
    }
    output
}

fn redirect_url(target: &Target, location: &str) -> String {
    let location = location.split('#').next().unwrap_or_default();
    if location
        .split_once("://")
        .is_some_and(|(scheme, _)| scheme.eq_ignore_ascii_case("http") || scheme.eq_ignore_ascii_case("https"))
    {
        return location.to_string();
    }
    let scheme = if target.tls { "https" } else { "http" };
    if location.starts_with("//") {
        return format!("{scheme}:{location}");
    }
    if location.is_empty() {
        return format!("{scheme}://{}{}", format_authority(target, true), target.path);
    }
    let base_path = target.path.split_once('?').map_or(target.path.as_str(), |(path, _)| path);
    let (path, query) = location.split_once('?').map_or((location, None), |(path, query)| (path, Some(query)));
    let path = if path.is_empty() {
        base_path.to_string()
    } else if path.starts_with('/') {
        normalize_path(path)
    } else {
        let directory = base_path.rsplit_once('/').map_or("", |(directory, _)| directory);
        normalize_path(&format!("{directory}/{path}"))
    };
    let query = query.map_or(String::new(), |query| format!("?{query}"));
    format!("{scheme}://{}{}{}", format_authority(target, true), path, query)
}

fn format_authority(target: &Target, include_default_port: bool) -> String {
    let host = if target.host.contains(':') {
        format!("[{}]", target.host)
    } else {
        target.host.clone()
    };
    let default_port = if target.tls { 443 } else { 80 };
    if include_default_port || target.port != default_port {
        format!("{host}:{}", target.port)
    } else {
        host
    }
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
    let host = format_authority(target, false);
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
    Ok(Upstream {
        status,
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
        url = redirect_url(&target, location);
    }
    Err("too many upstream redirects".to_string())
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
    let payload = response.read_body(MAX_BODY_BYTES)?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::{Shutdown, TcpListener};
    use std::sync::mpsc;
    use std::thread::JoinHandle;
    use std::time::Instant;

    fn stub(response: Vec<u8>, hold_open: bool) -> (String, mpsc::Sender<()>, JoinHandle<()>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("stub listener");
        let address = listener.local_addr().expect("stub address");
        let (release, released) = mpsc::channel();
        let thread = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("upstream connection");
            // Windows resets a socket closed with unread request bytes, losing the response.
            let mut request = Vec::new();
            let mut byte = [0u8; 1];
            while !request.ends_with(b"\r\n\r\n") && matches!(stream.read(&mut byte), Ok(1)) {
                request.push(byte[0]);
            }
            stream.write_all(&response).expect("stub response");
            stream.flush().expect("stub flush");
            if hold_open {
                let _ = released.recv_timeout(Duration::from_secs(5));
            }
        });
        (format!("http://{address}/"), release, thread)
    }

    #[test]
    fn redirect_paths_follow_rfc3986_examples_without_decoding() {
        let target = parse_url("http://a/b/c/d;p?q").expect("test URL parses");
        let uppercase = parse_url("HTTP://LOCALHOST:123/Path").expect("case-insensitive URL parses");
        assert_eq!(uppercase.host, "localhost");
        assert!(!uppercase.tls);
        // RFC 3986 section 5.4, plus the empty-segment and query cases that
        // matter to signed media URLs.
        let cases = [
            ("g", "http://a:80/b/c/g"),
            ("./g", "http://a:80/b/c/g"),
            ("g/", "http://a:80/b/c/g/"),
            ("/g", "http://a:80/g"),
            ("//g", "http://g"),
            ("?y", "http://a:80/b/c/d;p?y"),
            ("g?y", "http://a:80/b/c/g?y"),
            ("#s", "http://a:80/b/c/d;p?q"),
            ("g#s", "http://a:80/b/c/g"),
            (".", "http://a:80/b/c/"),
            ("./g/.", "http://a:80/b/c/g/"),
            ("..", "http://a:80/b/"),
            ("../g", "http://a:80/b/g"),
            ("/a//b", "http://a:80/a//b"),
            ("g%2Fh?x=a//b", "http://a:80/b/c/g%2Fh?x=a//b"),
            ("HTTP://A/Case", "HTTP://A/Case"),
        ];
        for (location, expected) in cases {
            assert_eq!(redirect_url(&target, location), expected, "Location: {location}");
        }
    }

    #[test]
    fn bracketed_ipv6_urls_keep_their_authority() {
        let target = parse_url("http://[::1]:8080/media/file.mkv").expect("IPv6 URL parses");
        assert_eq!(target.host, "::1");
        assert_eq!(target.port, 8080);
        assert_eq!(format_authority(&target, false), "[::1]:8080");
        assert_eq!(redirect_url(&target, ""), "http://[::1]:8080/media/file.mkv");

        let default_port = parse_url("https://[2001:db8::1]/").expect("IPv6 URL parses");
        assert_eq!(default_port.host, "2001:db8::1");
        assert_eq!(default_port.port, 443);
        assert_eq!(format_authority(&default_port, false), "[2001:db8::1]");
        assert_eq!(redirect_url(&default_port, ""), "https://[2001:db8::1]:443/");
    }

    #[test]
    fn ipv6_socket_receives_a_bracketed_host_header() {
        let listener = match TcpListener::bind(("::1", 0)) {
            Ok(listener) => listener,
            Err(error) => {
                eprintln!("IPv6 loopback unavailable; skipping socket assertion: {error}");
                return;
            }
        };
        listener
            .set_nonblocking(true)
            .expect("nonblocking IPv6 listener");
        let port = listener.local_addr().expect("IPv6 listener address").port();
        let (host_tx, host_rx) = mpsc::channel();
        let thread = std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(2);
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(connection) => break connection,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock && Instant::now() < deadline => {
                        std::thread::sleep(Duration::from_millis(5));
                    }
                    Err(_) => return,
                }
            };
            let mut request = Vec::new();
            let mut buffer = [0_u8; 1024];
            while !request.windows(4).any(|window| window == b"\r\n\r\n") {
                let count = std::io::Read::read(&mut stream, &mut buffer).expect("request");
                if count == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..count]);
            }
            let host = String::from_utf8_lossy(&request)
                .lines()
                .find_map(|line| line.strip_prefix("Host: "))
                .unwrap_or_default()
                .to_string();
            host_tx.send(host).expect("host receiver");
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .expect("response");
        });
        let target = parse_url(&format!("http://[::1]:{port}/probe")).expect("IPv6 URL");
        let response = exchange("GET", &target, &[], b"", Duration::from_secs(1))
            .expect("IPv6 loopback exchange");
        assert_eq!(response.status, 200);
        assert_eq!(host_rx.recv().expect("captured host"), format!("[::1]:{port}"));
        thread.join().expect("IPv6 stub");
    }

    #[test]
    fn unterminated_upstream_header_is_rejected_at_the_limit() {
        let mut response = b"HTTP/1.1 200 OK\r\nX-Broken: ".to_vec();
        response.extend(std::iter::repeat_n(b'x', 64 * 1024));
        let (url, release, thread) = stub(response, true);
        let target = parse_url(&url).expect("stub URL");
        let started = Instant::now();
        let error = match exchange("GET", &target, &[], b"", Duration::from_millis(250)) {
            Ok(_) => panic!("unterminated header was accepted"),
            Err(error) => error,
        };
        assert!(error.to_string().contains("header line too long"));
        assert!(started.elapsed() < Duration::from_secs(1), "reader waited for EOF");
        release.send(()).expect("release stub");
        thread.join().expect("stub thread");
    }

    #[test]
    fn valid_upstream_headers_still_read_the_body() {
        let (url, _release, thread) = stub(
            b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nX-Test: valid\r\n\r\nabc".to_vec(),
            false,
        );
        let target = parse_url(&url).expect("stub URL");
        let mut response = exchange("GET", &target, &[], b"", Duration::from_secs(1)).expect("response");
        assert_eq!(response.header("x-test"), Some("valid"));
        assert_eq!(response.read_body(3).expect("body"), b"abc");
        thread.join().expect("stub thread");
    }

    #[test]
    fn short_fixed_length_body_is_rejected() {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("stub listener");
        let address = listener.local_addr().expect("stub address");
        let (release, released) = mpsc::channel();
        let thread = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("upstream connection");
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nx")
                .expect("stub response");
            stream.shutdown(Shutdown::Write).expect("half-close stub");
            let _ = released.recv_timeout(Duration::from_secs(5));
        });
        let target = parse_url(&format!("http://{address}/")).expect("stub URL");
        let mut response = exchange("GET", &target, &[], b"", Duration::from_secs(1))
            .expect("response head");
        let error = response
            .read_body(64)
            .expect_err("short fixed-length body should fail");
        assert!(
            error.contains("shorter than Content-Length"),
            "unexpected error: {error}"
        );
        release.send(()).expect("release stub");
        thread.join().expect("stub thread");
    }

    #[cfg(windows)]
    #[test]
    fn windows_root_store_configures_tls() {
        assert!(!crate::platform::system_root_certificates().expect("Windows ROOT store").is_empty());
        assert!(client_config().is_some());
    }
}
