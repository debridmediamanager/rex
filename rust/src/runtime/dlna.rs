//! The DLNA media server a real PMS runs beside its own once `DlnaEnabled` is on: SSDP on udp/1900
//! and UPnP on tcp/32469. Every shape here is plexlab's (1.43.4, unclaimed, 2026-11-10), recorded
//! by `probes/record_dlna.py` into `tests/fixtures/dlna/plexlab_1434_s1110.json`, plus read-only
//! follow-ups the same day (findings, "DLNA server"). PMS served VLC, a Samsung TV and Windows Media
//! Player the same tree byte for byte and every one the original file, so rex converts nothing.
//!
//! The tree is a measured subset of PMS's: Video holds each movie and show library and an empty
//! Video Channels; Music and Photos are empty. PMS's `Preferences` folders are left out: their
//! options set the client's transcode quality, and browsing one SETS it (the recording's first walk
//! did), while rex converts nothing for DLNA, so they would do nothing. Object ids are rex's own,
//! the path from the root (PMS's are hashes; a client keeps them opaque): stable across restarts,
//! and an object's parent is its id less the last segment, which PMS's last-listed parent is not.
//!
//! DLNA has no authentication, so its port answers only peers on private networks, read off the
//! socket as the feed's are (`feed::source_peer`), and SSDP answers no one else either.

use super::*;
use regex::Regex;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::time::{Duration, Instant};

const SERVER: &str = "UPnP/1.0 DLNADOC/1.50 Platinum/1.0.5.13";
const GROUP: Ipv4Addr = Ipv4Addr::new(239, 255, 255, 250);
const MEDIA_SERVER: &str = "urn:schemas-upnp-org:device:MediaServer:1";
const DIRECTORY: &str = "urn:schemas-upnp-org:service:ContentDirectory:1";
const CONNECTIONS: &str = "urn:schemas-upnp-org:service:ConnectionManager:1";
const REGISTRAR: &str = "urn:microsoft.com:service:X_MS_MediaReceiverRegistrar:1";
/// The services in the description's order: type, id, and the path segment of their URLs.
const SERVICES: [(&str, &str, &str); 3] = [
    (REGISTRAR, "urn:microsoft.com:serviceId:X_MS_MediaReceiverRegistrar", "X_MS_MediaReceiverRegistrar"),
    (DIRECTORY, "urn:upnp-org:serviceId:ContentDirectory", "ContentDirectory"),
    (CONNECTIONS, "urn:upnp-org:serviceId:ConnectionManager", "ConnectionManager"),
];
const DIDL: &str = "<DIDL-Lite xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\" xmlns:dc=\"http://purl.org/dc/elements/1.1/\" \
                    xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\" xmlns:dlna=\"urn:schemas-dlna-org:metadata-1-0/\">";
const FOLDER: &str = "object.container.storageFolder";
const PLAYLIST: &str = "object.container.playlistContainer";
/// Every `res` and stream answer: original file, byte seek, no conversion.
const FEATURES: &str = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01500000000000000000000000000000";
/// A movie and a show library's folders in PMS's order, less those rex leaves out (By Collection,
/// By Edition, the person, country, rating and resolution axes, By Folder; a show library's Recently
/// Released, Recently Viewed and Continue Watching). PMS lists By First Letter before By Genre in a
/// show library and after it in a movie library.
const MOVIE_FOLDERS: [(&str, &str); 10] = [
    ("all", "All Movies"), ("unwatched", "Unwatched"), ("released", "Recently Released"),
    ("added", "Recently Added"), ("viewed", "Recently Viewed"), ("resume", "Continue Watching"),
    ("genre", "By Genre"), ("year", "By Year"), ("decade", "By Decade"), ("letter", "By First Letter"),
];
const SHOW_FOLDERS: [(&str, &str); 5] = [
    ("all", "All Shows"), ("unwatched", "Unwatched"), ("added", "Recently Added"),
    ("letter", "By First Letter"), ("genre", "By Genre"),
];

/// `REX_DLNA_PORT` / `REX_SSDP_PORT`: the suites move both off the host's real ports.
fn port(name: &str, default: u16) -> u16 {
    std::env::var(name).ok().and_then(|value| value.trim().parse().ok()).filter(|port| *port != 0).unwrap_or(default)
}

/// What one run of the server announces, from turning `DlnaEnabled` on to turning it off.
struct Advert {
    uuid: String,
    http_port: u16,
    ssdp_port: u16,
    lease: u64,
    /// BOOTID.UPNP.ORG: plexlab's was the second its server started (its alive NOTIFYs).
    boot: u64,
}

struct Running {
    advert: Arc<Advert>,
    stop: Arc<AtomicBool>,
    host: String,
    announced: Instant,
}

static ACTIVE: Mutex<Option<Running>> = Mutex::new(None);

/// Run the server while `DlnaEnabled` is on, read every second: plexlab sent byebye and then alive
/// when the pref was turned off and on, with no restart.
pub(super) fn start(runtime: &SharedRuntime, host: &str) {
    let (runtime, host) = (Arc::clone(runtime), host.to_string());
    let _ = thread::Builder::new().name("dlna".to_string()).spawn(move || {
        // A start that failed (the port taken) is tried again only once the pref is turned off and on.
        let mut failed = false;
        loop {
            let wanted = feed::lock(&runtime).prefs.get("DlnaEnabled").and_then(Value::as_bool) == Some(true);
            {
                let mut active = ACTIVE.lock().unwrap_or_else(PoisonError::into_inner);
                if !wanted {
                    if let Some(running) = active.take() {
                        running.stop();
                    }
                } else if let Some(running) = active.as_mut() {
                    // Not measured: the recording was 20 s of a 1800 s lease. Half the lease keeps a
                    // renderer's entry through one lost round.
                    if running.announced.elapsed() >= Duration::from_secs(running.advert.lease / 2) {
                        running.announced = Instant::now();
                        running.alive();
                    }
                } else if !failed {
                    *active = Running::start(&runtime, &host);
                    failed = active.is_none();
                }
                failed &= wanted;
            }
            // A server with no state saver to end it (`--no-state`) ends here, once its byebye is out.
            if TERMINATE.load(Ordering::SeqCst) {
                bye();
                if !SAVER.load(Ordering::SeqCst) {
                    info!("stopping");
                    std::process::exit(0);
                }
                return;
            }
            thread::sleep(Duration::from_secs(1));
        }
    });
}

/// The byebye a stopping server sends (the state saver's SIGTERM path, and the loop above).
pub(super) fn bye() {
    if let Some(running) = ACTIVE.lock().unwrap_or_else(PoisonError::into_inner).take() {
        running.stop();
    }
}

impl Running {
    fn start(runtime: &SharedRuntime, host: &str) -> Option<Self> {
        let (uuid, lease) = {
            let guard = feed::lock(runtime);
            let lease = guard.server_pref("DlnaAnnouncementLeaseTime").and_then(|value| value_as_i64(&value)).filter(|lease| *lease > 0);
            (guard.dlna_uuid(), lease.unwrap_or(1800) as u64)
        };
        let (http_port, ssdp_port) = (port("REX_DLNA_PORT", 32469), port("REX_SSDP_PORT", 1900));
        let listener = match TcpListener::bind((host, http_port)) {
            Ok(listener) => listener,
            Err(error) => {
                warning!("DLNA: could not bind tcp/{http_port}: {} -- the DLNA server is off", py_os_error(&error));
                return None;
            }
        };
        let boot = SystemTime::now().duration_since(UNIX_EPOCH).map(|elapsed| elapsed.as_secs()).unwrap_or(0);
        let advert = Arc::new(Advert { uuid, http_port, ssdp_port, lease, boot });
        let stop = Arc::new(AtomicBool::new(false));
        // Shared with any other SSDP listener on the host, as GDM's socket is.
        match crate::platform::gdm_socket(ssdp_port) {
            Ok(socket) => {
                let interface = lan_ipv4(host).unwrap_or(Ipv4Addr::UNSPECIFIED);
                if let Err(error) = socket.join_multicast_v4(&GROUP, &interface) {
                    warning!("DLNA: could not join {GROUP} on {interface}: {}", py_os_error(&error));
                }
                let (runtime, advert, stop) = (Arc::clone(runtime), Arc::clone(&advert), Arc::clone(&stop));
                let _ = thread::Builder::new().name("dlna-ssdp".to_string()).spawn(move || listen_ssdp(socket, runtime, advert, stop));
            }
            Err(error) => warning!("DLNA: could not bind udp/{ssdp_port}: {} -- renderers will not find this server by searching", py_os_error(&error)),
        }
        let (shared, stopped) = (Arc::clone(runtime), Arc::clone(&stop));
        let _ = thread::Builder::new().name("dlna-http".to_string()).spawn(move || accept(listener, shared, stopped));
        install_sigterm();
        let running = Running { advert, stop, host: host.to_string(), announced: Instant::now() };
        running.alive();
        info!("DLNA server on tcp/{http_port}, SSDP on udp/{ssdp_port}");
        Some(running)
    }

    /// Two rounds of an alive NOTIFY per target, 50 ms apart and the rounds 250 ms apart, as
    /// plexlab sent them, out of the LAN interface. With no private IPv4 there is no one to tell.
    fn alive(&self) {
        let Some((socket, lan)) = self.notifier() else {
            info!("DLNA: no LAN address to announce on; renderers that search still find it");
            return;
        };
        let advert = &self.advert;
        for round in 0..2 {
            for (index, (nt, usn)) in targets(&advert.uuid).into_iter().enumerate() {
                if index > 0 {
                    thread::sleep(Duration::from_millis(50));
                }
                let message = format!(
                    "NOTIFY * HTTP/1.1\r\nHost: {GROUP}:{}\r\nLocation: http://{lan}:{}/DeviceDescription.xml\r\nCache-Control: max-age={}\r\n\
                     Server: {SERVER}\r\nNTS: ssdp:alive\r\nUSN: {usn}\r\nNT: {nt}\r\n\r\n",
                    advert.ssdp_port, advert.http_port, advert.lease
                );
                let _ = socket.send_to(message.as_bytes(), (GROUP, advert.ssdp_port));
            }
            if round == 0 {
                thread::sleep(Duration::from_millis(250));
            }
        }
    }

    /// The listener and SSDP threads stop, and two rounds of byebye go out at once, as plexlab's did.
    fn stop(self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some((socket, _)) = self.notifier() {
            for _ in 0..2 {
                for (nt, usn) in targets(&self.advert.uuid) {
                    let message = format!("NOTIFY * HTTP/1.1\r\nHost: {GROUP}:{}\r\nNTS: ssdp:byebye\r\nUSN: {usn}\r\nNT: {nt}\r\n\r\n", self.advert.ssdp_port);
                    let _ = socket.send_to(message.as_bytes(), (GROUP, self.advert.ssdp_port));
                }
            }
        }
        info!("DLNA server stopped");
    }

    /// A socket whose multicast leaves by the LAN interface, and that interface's address: read
    /// again each time, as the announce reads it (a start before DHCP has an address).
    fn notifier(&self) -> Option<(UdpSocket, Ipv4Addr)> {
        let lan = lan_ipv4(&self.host)?;
        let socket = UdpSocket::bind((lan, 0)).ok()?;
        if let Err(error) = crate::platform::multicast_interface_v4(&socket, lan) {
            debug!("DLNA: multicast interface {lan}: {}", py_os_error(&error));
        }
        Some((socket, lan))
    }
}

/// The LAN address the announce uses, when it is IPv4: the bound host, else the routeable
/// interface, private only. Unlike plex.tv's announce, a container network's counts: a multicast
/// never leaves the network it is sent on, so whoever hears it can reach that address.
fn lan_ipv4(host: &str) -> Option<Ipv4Addr> {
    match lan_address(host, &local_interface_address().unwrap_or_default())?.to_canonical() {
        IpAddr::V4(v4) => Some(v4),
        IpAddr::V6(_) => None,
    }
}

/// Every NT plexlab announced and every ST it answered, in its order: (NT, USN).
fn targets(uuid: &str) -> Vec<(String, String)> {
    let mut out = vec![
        ("upnp:rootdevice".to_string(), format!("uuid:{uuid}::upnp:rootdevice")),
        (format!("uuid:{uuid}"), format!("uuid:{uuid}")),
    ];
    for kind in [MEDIA_SERVER, REGISTRAR, DIRECTORY, CONNECTIONS] {
        out.push((kind.to_string(), format!("uuid:{uuid}::{kind}")));
    }
    out
}

/// What an M-SEARCH asks for: the targets it matches and its MX. `ssdp:all` matches every one.
fn search(packet: &[u8], uuid: &str) -> Option<(Vec<(String, String)>, u64)> {
    let text = std::str::from_utf8(packet).ok()?;
    let mut lines = text.split("\r\n");
    if !lines.next()?.starts_with("M-SEARCH * ") {
        return None;
    }
    let header = |name: &str| {
        lines.clone().find_map(|line| {
            let (key, value) = line.split_once(':')?;
            key.trim().eq_ignore_ascii_case(name).then(|| value.trim().to_string())
        })
    };
    let st = header("ST")?;
    let mx = header("MX").and_then(|mx| mx.parse::<u64>().ok()).unwrap_or(0);
    let matched: Vec<_> = targets(uuid).into_iter().filter(|(nt, _)| st == "ssdp:all" || *nt == st).collect();
    (!matched.is_empty()).then_some((matched, mx))
}

/// Answers in flight, bounded: each waits up to MX seconds on a thread of its own.
static SEARCHES: AtomicUsize = AtomicUsize::new(0);

fn listen_ssdp(socket: UdpSocket, runtime: SharedRuntime, advert: Arc<Advert>, stop: Arc<AtomicBool>) {
    let _ = socket.set_read_timeout(Some(Duration::from_millis(500)));
    let mut packet = [0_u8; 4096];
    while !stop.load(Ordering::SeqCst) {
        let Ok((length, peer)) = socket.recv_from(&mut packet) else {
            continue;
        };
        // Measured: plexlab answered an M-SEARCH sent to its LAN address and none sent to loopback.
        // A public peer is never answered: an SSDP answer to a forged source is a reflection.
        if peer.ip().is_loopback() || !feed::source_peer(peer.ip()) {
            continue;
        }
        let Some((matched, mx)) = search(&packet[..length], &advert.uuid) else {
            continue;
        };
        if SEARCHES.fetch_add(1, Ordering::SeqCst) >= 16 {
            SEARCHES.fetch_sub(1, Ordering::SeqCst);
            continue;
        }
        let (runtime, advert) = (Arc::clone(&runtime), Arc::clone(&advert));
        let spawned = thread::Builder::new().name("dlna-search".to_string()).spawn(move || {
            answer_search(peer, &matched, mx, &runtime, &advert);
            SEARCHES.fetch_sub(1, Ordering::SeqCst);
        });
        if spawned.is_err() {
            SEARCHES.fetch_sub(1, Ordering::SeqCst);
        }
    }
}

/// Measured on plexlab: the answers come after whole seconds below MX, up to 4 (MX 1 and 2: 0 s;
/// MX 3: 1 and 2 s; MX 5: 0 s; MX 8: 3 s), from a port of their own, and every answer is sent
/// again 200 ms later. LOCATION is the address this host reaches the searcher from.
fn answer_search(peer: SocketAddr, matched: &[(String, String)], mx: u64, runtime: &SharedRuntime, advert: &Advert) {
    let spread = mx.min(5);
    if spread > 1 {
        let mut byte = [0_u8; 1];
        let _ = crate::platform::random_bytes(&mut byte);
        thread::sleep(Duration::from_secs(u64::from(byte[0]) % spread));
    }
    let Ok(socket) = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0)) else {
        return;
    };
    if socket.connect(peer).is_err() {
        return;
    }
    let Ok(local) = socket.local_addr() else {
        return;
    };
    let config = feed::lock(runtime).dlna_config(&advert.uuid);
    for round in 0..2 {
        if round == 1 {
            thread::sleep(Duration::from_millis(200));
        }
        for (st, usn) in matched {
            let message = format!(
                "HTTP/1.1 200 OK\r\nLocation: http://{}:{}/DeviceDescription.xml\r\nCache-Control: max-age={}\r\nServer: {SERVER}\r\nEXT: \r\n\
                 BOOTID.UPNP.ORG: {}\r\nCONFIGID.UPNP.ORG: {config}\r\nUSN: {usn}\r\nST: {st}\r\nDate: {}\r\n\r\n",
                local.ip(), advert.http_port, advert.lease, advert.boot, http_date()
            );
            let _ = socket.send(message.as_bytes());
        }
    }
}

fn accept(listener: TcpListener, runtime: SharedRuntime, stop: Arc<AtomicBool>) {
    // Polled, so turning DLNA off closes the port.
    if listener.set_nonblocking(true).is_err() {
        return;
    }
    while !stop.load(Ordering::SeqCst) {
        match listener.accept() {
            Ok((stream, peer)) => {
                // No authentication: a peer off the private networks is closed on, answered nothing.
                if !feed::source_peer(peer.ip()) {
                    continue;
                }
                let _ = stream.set_nonblocking(false);
                let _ = stream.set_read_timeout(Some(Duration::from_secs(20)));
                let (runtime, stop) = (Arc::clone(&runtime), Arc::clone(&stop));
                let _ = thread::Builder::new().name("dlna-connection".to_string()).spawn(move || connection(stream, runtime, stop));
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => thread::sleep(Duration::from_millis(50)),
            Err(_) => thread::sleep(Duration::from_millis(50)),
        }
    }
}

/// One connection: kept alive unless the request asks to close, as plexlab's was (it wrote
/// `Connection: close` only to a request that sent it, or an HTTP/1.0 one).
fn connection(mut stream: TcpStream, runtime: SharedRuntime, stop: Arc<AtomicBool>) {
    let local = stream.local_addr().ok();
    let mut pending = Vec::new();
    loop {
        let Ok(Some(request)) = read_request_bounded(&mut stream, &mut pending, false, 16) else {
            return;
        };
        let close = request.version != "HTTP/1.1"
            || request.headers.get("connection").is_some_and(|value| value.eq_ignore_ascii_case("close"));
        // The address the client reached: what every URL this server hands it names.
        let host = request.headers.get("host").cloned().filter(|host| !host.is_empty()).or_else(|| local.map(|local| local.to_string())).unwrap_or_default();
        let (path, _) = split_target(&request.target);
        let open = match route(&runtime, &request, &path, &host) {
            Reply::Done(answer) => answer.write(&mut stream, request.method == "HEAD", close).is_ok(),
            Reply::Part(part) => {
                let _ = part.serve(&mut stream, &runtime, close);
                false
            }
            Reply::Art(url, headers) => match transcoder::fetch_remote_image(&url, headers.as_deref()) {
                Some((body, kind)) => Answer::typed(200, &kind, body),
                None => Answer::empty(404),
            }
            .write(&mut stream, false, close)
            .is_ok(),
        };
        debug!("DLNA {} {} -> {}", request.method, path, if open { "sent" } else { "closed" });
        if !open || close || stop.load(Ordering::SeqCst) {
            return;
        }
    }
}

/// An answer in plexlab's DLNA header order: what precedes them, then `Connection: close` when the
/// request asked for it, then `Server` last.
struct Answer {
    status: u16,
    reason: &'static str,
    lines: Vec<(&'static str, String)>,
    body: Vec<u8>,
}

impl Answer {
    /// Platinum's bare answer: an error, an icon's HEAD.
    fn empty(status: u16) -> Self {
        Answer { status, reason: reason(status), lines: vec![("Date", http_date()), ("Content-Length", "0".to_string())], body: Vec::new() }
    }

    fn typed(status: u16, kind: &str, body: Vec<u8>) -> Self {
        let lines = vec![("Date", http_date()), ("Content-Length", body.len().to_string()), ("Content-Type", kind.to_string())];
        Answer { status, reason: reason(status), lines, body }
    }

    fn xml(body: String) -> Self {
        Self::typed(200, "text/xml; charset=\"utf-8\"", body.into_bytes())
    }

    /// Every SOAP answer, a fault too: `Ext:` first.
    fn soap(status: u16, inner: &str) -> Self {
        let body = format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\r\n<s:Envelope s:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\" \
             xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\"><s:Body>{inner}</s:Body></s:Envelope>"
        );
        let mut answer = Self::typed(status, "text/xml; charset=\"utf-8\"", body.into_bytes());
        answer.lines.insert(0, ("Ext", String::new()));
        answer
    }

    fn fault(code: u16, description: &str) -> Self {
        Self::soap(
            500,
            &format!(
                "<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail><UPnPError \
                 xmlns=\"urn:schemas-upnp-org:control-1-0\"><errorCode>{code}</errorCode><errorDescription>{description}</errorDescription>\
                 </UPnPError></detail></s:Fault>"
            ),
        )
    }

    fn write(&self, stream: &mut impl Write, head: bool, close: bool) -> io::Result<()> {
        write_head(stream, self.status, self.reason, &self.lines, close)?;
        if !head {
            stream.write_all(&self.body)?;
        }
        stream.flush()
    }
}

fn write_head(stream: &mut impl Write, status: u16, reason: &str, lines: &[(&str, String)], close: bool) -> io::Result<()> {
    let mut head = format!("HTTP/1.1 {status} {reason}\r\n");
    for (name, value) in lines {
        head.push_str(&format!("{name}: {}\r\n", value.replace(['\r', '\n'], " ")));
    }
    if close {
        head.push_str("Connection: close\r\n");
    }
    head.push_str(&format!("Server: {SERVER}\r\n\r\n"));
    stream.write_all(head.as_bytes())
}

fn reason(status: u16) -> &'static str {
    match status {
        412 => "Precondition Failed",
        status => wire::reason_phrase(status),
    }
}

enum Reply {
    Done(Answer),
    /// A Part's bytes, read from its source once the runtime lock is released.
    Part(PartRead),
    /// A poster fetched once the lock is released: its url, and a source's own headers.
    Art(String, Option<Vec<(&'static str, String)>>),
}

fn route(runtime: &SharedRuntime, request: &Request, path: &str, host: &str) -> Reply {
    let method = request.method.as_str();
    let mut guard = feed::lock(runtime);
    let uuid = guard.dlna_uuid();
    let segments: Vec<&str> = path.trim_start_matches('/').split('/').collect();
    let read = matches!(method, "GET" | "HEAD");
    let reply = match segments.as_slice() {
        ["DeviceDescription.xml"] if read => Reply::Done(Answer::xml(guard.dlna_description(&uuid))),
        [service, id, leaf] if *id == uuid && SERVICES.iter().any(|(_, _, name)| name == service) => match (*leaf, method) {
            ("scpd.xml", "GET" | "HEAD") => match sidecar_bytes(&format!("dlna_{service}.xml")) {
                Some(body) => Reply::Done(Answer::typed(200, "text/xml; charset=\"utf-8\"", body)),
                None => Reply::Done(Answer::empty(404)),
            },
            ("control.xml", "POST") => Reply::Done(guard.dlna_control(service, request, host)),
            // Measured: SUBSCRIBE answers a SID and Platinum's own 1800 s whatever the client asks.
            // rex sends no events, so it keeps no subscription, and every UNSUBSCRIBE names one it
            // does not hold: plexlab's answer to that is 412.
            ("event.xml", "SUBSCRIBE") => {
                let mut sid = [0_u8; 16];
                let _ = crate::platform::random_bytes(&mut sid);
                let mut answer = Answer::empty(200);
                answer.lines.insert(0, ("TIMEOUT", "Second-1800".to_string()));
                answer.lines.insert(0, ("SID", format!("uuid:{}", dashed(&hex_bytes(&sid)))));
                Reply::Done(answer)
            }
            ("event.xml", "UNSUBSCRIBE") => Reply::Done(Answer::empty(412)),
            _ => Reply::Done(Answer::empty(404)),
        },
        // Measured: a HEAD of an icon or a poster is Platinum's bare 200, no type and no length.
        ["proxy", _, _] if method == "HEAD" => Reply::Done(Answer::empty(200)),
        ["proxy", "icon", file] if method == "GET" => Reply::Done(icon(&guard, file)),
        ["proxy", key, "albumart.jpg" | "icon.jpg"] if method == "GET" => guard.dlna_art(key),
        ["object", key, _] if read => guard.dlna_part(key, request),
        _ => Reply::Done(Answer::empty(404)),
    };
    // What the answer read is let go of, as after any request (`Catalog::pack_pending`).
    guard.catalog.pack_pending();
    reply
}

/// The description's icons: `DlnaDescriptionIcons` (`png,jpeg;260x260,120x120,48x48`, each type
/// at each size), rex's own plain stand-ins at those sizes, never Plex's artwork.
fn icons(runtime: &Runtime) -> Vec<(&'static str, u32, u32)> {
    let setting = runtime.server_pref("DlnaDescriptionIcons").and_then(|value| value.as_str().map(str::to_string)).unwrap_or_default();
    let (kinds, sizes) = setting.split_once(';').unwrap_or(("", ""));
    let mut out = Vec::new();
    for kind in kinds.split(',') {
        let mime = match kind.trim() {
            "png" => "image/png",
            "jpeg" | "jpg" => "image/jpeg",
            _ => continue,
        };
        for size in sizes.split(',') {
            if let Some((width, height)) = size.trim().split_once('x').and_then(|(w, h)| Some((w.parse().ok()?, h.parse().ok()?))) {
                out.push((mime, width, height));
            }
        }
    }
    out
}

fn icon_file(mime: &str, width: u32, height: u32) -> String {
    format!("{width}x{height}.{}", if mime == "image/png" { "png" } else { "jpg" })
}

fn icon(runtime: &Runtime, file: &str) -> Answer {
    match icons(runtime).into_iter().find(|(mime, width, height)| icon_file(mime, *width, *height) == file) {
        Some(("image/png", width, height)) => Answer::typed(200, "image/png", generated_png(width, height, [32, 34, 38])),
        Some((mime, width, height)) => Answer::typed(200, mime, generated_jpeg(width, height, [32, 34, 38])),
        None => Answer::empty(404),
    }
}

fn hex_bytes(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// 32 characters as a UUID's 8-4-4-4-12.
fn dashed(text: &str) -> String {
    let text: String = text.chars().chain(std::iter::repeat('0')).take(32).collect();
    format!("{}-{}-{}-{}-{}", &text[..8], &text[8..12], &text[12..16], &text[16..20], &text[20..])
}

/// Text and attribute values inside DIDL and the description: plexlab wrote `'` as `&apos;`.
fn esc(text: &str) -> String {
    text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&apos;")
}

/// A SOAP argument's text: the DIDL inside `Result` is escaped again, its quotes left as they are.
fn esc_text(text: &str) -> String {
    text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

fn unesc(text: &str) -> String {
    text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&apos;", "'").replace("&amp;", "&")
}

/// A SOAP request's arguments, by element name (any prefix), each the text it holds.
fn arguments(body: &[u8]) -> HashMap<String, String> {
    static PAIR: std::sync::OnceLock<Regex> = std::sync::OnceLock::new();
    let pair = PAIR.get_or_init(|| Regex::new(r"<(?:[\w.-]+:)?([\w.-]+)(?:\s[^>]*)?(?:/>|>([^<]*)</(?:[\w.-]+:)?([\w.-]+)>)").expect("valid"));
    let text = String::from_utf8_lossy(body);
    let mut out = HashMap::new();
    for found in pair.captures_iter(&text) {
        let name = found.get(1).map_or("", |name| name.as_str());
        if found.get(3).is_some_and(|close| close.as_str() != name) {
            continue;
        }
        out.entry(name.to_string()).or_insert_with(|| unesc(found.get(2).map_or("", |text| text.as_str())));
    }
    out
}

/// `H:MM:SS.mmm`: plexlab wrote whole seconds (a Media of 10,030 ms is `0:00:10.000`).
fn duration(milliseconds: i64) -> String {
    let seconds = milliseconds.max(0) / 1000;
    format!("{}:{:02}:{:02}.000", seconds / 3600, seconds / 60 % 60, seconds % 60)
}

/// The Part's type: the DLNA protocol list's (`DlnaDefaultProtocolInfo`) for a container it names,
/// else what the Part read answers on the PMS port (`part_content_type`).
fn part_type(part: &Value, container: &str) -> String {
    match container {
        "mkv" | "matroska" => "video/x-matroska".to_string(),
        "mp4" | "m4v" => "video/mp4".to_string(),
        "mpeg" | "mpg" => "video/mpeg".to_string(),
        "mpegts" | "ts" => "video/vnd.dlna.mpeg-tts".to_string(),
        "avi" => "video/avi".to_string(),
        "asf" | "wmv" => "video/x-ms-wmv".to_string(),
        "wtv" => "video/wtv".to_string(),
        _ => part_content_type(part),
    }
}

/// `contentFeatures.dlna.org` and a `res` protocolInfo's fourth field. plexlab named a DLNA profile
/// for one file only, its mp4: H.264 High, 1080p at 24, AAC. That combination is named as measured;
/// no other was measured, so none is named (the DLNA.ORG_PN is optional; plexlab's Matroska had none).
fn features(media: &Value) -> String {
    let field = |name: &str| media.get(name).map(string_value).unwrap_or_default();
    let measured = [("container", "mp4"), ("videoCodec", "h264"), ("videoProfile", "high"), ("videoResolution", "1080"), ("videoFrameRate", "24p"), ("audioCodec", "aac")];
    if measured.iter().all(|(name, value)| field(name) == *value) {
        format!("DLNA.ORG_PN=AVC_MP4_HP_HD_24_AAC;{FEATURES}")
    } else {
        FEATURES.to_string()
    }
}

/// A row's Part for DLNA: the first Media's first Part. ponytail: a row of several versions or parts
/// offers only that one; a renderer has no version picker to offer the rest in.
fn first_part(row: &Value) -> Option<(&Value, &Value)> {
    let media = row.get("Media")?.as_array()?.first()?;
    let part = media.get("Part")?.as_array()?.first()?;
    Some((media, part))
}

fn container_of(media: &Value, part: &Value) -> String {
    part.get("container")
        .or_else(|| media.get("container"))
        .map(string_value)
        .filter(|container| !container.is_empty())
        .or_else(|| part.get("file").and_then(Value::as_str).and_then(|file| file.rsplit_once('.')).map(|(_, extension)| extension.to_ascii_lowercase()))
        .unwrap_or_else(|| "bin".to_string())
}

/// A stream read accepted under the lock, carried out without it.
struct PartRead {
    plan: PartProxy,
    kind: String,
    /// `transferMode.dlna.org` and `contentFeatures.dlna.org`, each only when the request asked:
    /// plexlab echoed the first and answered the second to `getcontentFeatures.dlna.org: 1`.
    dlna: Vec<(&'static str, String)>,
}

impl PartRead {
    fn serve(self, stream: &mut TcpStream, runtime: &SharedRuntime, close: bool) -> io::Result<()> {
        let head = self.plan.head;
        match open_part(&self.plan) {
            // Measured: Platinum's 416 carries the Part's own type and the HTML refusal.
            PartOpened::Unsatisfiable => {
                let body = error_document(416);
                let lines = vec![
                    ("Content-Length", body.len().to_string()),
                    ("Content-Type", self.kind),
                    ("Date", http_date()),
                    ("Cache-Control", "no-cache".to_string()),
                    ("Accept-Ranges", "bytes".to_string()),
                ];
                Answer { status: 416, reason: reason(416), lines, body }.write(stream, head, close)
            }
            PartOpened::Refused(status, _) => Answer::empty(status).write(stream, head, close),
            PartOpened::Elsewhere(location) => {
                let mut answer = Answer::empty(302);
                answer.lines.insert(0, ("Location", location));
                answer.write(stream, true, close)
            }
            PartOpened::Body { status, content_range, length, want, mut upstream } => {
                let mut lines = Vec::new();
                if let Some(length) = length {
                    lines.push(("Content-Length", length.to_string()));
                }
                lines.push(("Content-Type", self.kind));
                lines.push(("Date", http_date()));
                if let Some(range) = content_range {
                    lines.push(("Content-Range", range));
                }
                lines.extend(self.dlna);
                lines.push(("Accept-Ranges", "bytes".to_string()));
                write_head(stream, status, reason(status), &lines, close)?;
                if !head {
                    pump_part(stream, runtime, &mut upstream, want, &self.plan.part_id);
                }
                stream.flush()
            }
        }
    }
}

/// What the tree's objects are. An object's id is its path from the root (`dlna.rs` header).
enum Entry {
    /// One of rex's own containers: its title, class and, on a show library, its series title.
    Folder { title: String, class: &'static str, series: Option<String> },
    Movie(String),
    Show(String),
    /// A show's `All episodes`, by the show's key.
    AllEpisodes(String),
    Season(String),
    /// An episode, its title with its show's (`Show - S1 E3 - Title`) unless `bare`, and the
    /// series title its listing gives it: the show's in a season, else the library's.
    Episode { key: String, bare: bool, series: String },
}

struct Node {
    id: String,
    parent: String,
    entry: Entry,
}

impl Entry {
    /// The row the object is drawn from: `All episodes` its show's.
    fn key(&self) -> Option<&str> {
        match self {
            Entry::Movie(key) | Entry::Show(key) | Entry::AllEpisodes(key) | Entry::Season(key) | Entry::Episode { key, .. } => Some(key),
            Entry::Folder { .. } => None,
        }
    }
}

fn folder(id: impl Into<String>, parent: impl Into<String>, title: impl Into<String>, class: &'static str) -> Node {
    Node { id: id.into(), parent: parent.into(), entry: Entry::Folder { title: title.into(), class, series: None } }
}

/// A listing inside a library: its section, folder and bucket.
struct Listing<'a> {
    section: &'a Value,
    folder: &'static str,
    bucket: Option<String>,
}

impl Listing<'_> {
    fn kind(&self) -> &str {
        self.section.get("type").and_then(Value::as_str).unwrap_or("")
    }

    fn title(&self) -> String {
        self.section.get("title").map(string_value).unwrap_or_default()
    }
}

fn folders(kind: &str) -> &'static [(&'static str, &'static str)] {
    if kind == "show" { &SHOW_FOLDERS } else { &MOVIE_FOLDERS }
}

fn bucket_title(folder: &str, value: &str) -> String {
    if folder == "decade" { format!("{value}s") } else { value.to_string() }
}

/// A bucket's id segment: a genre or letter in hex, so a name with a `/` stays one segment.
fn bucket_segment(folder: &str, value: &str) -> String {
    if matches!(folder, "genre" | "letter") { hex_bytes(value.as_bytes()) } else { value.to_string() }
}

fn bucket_value(folder: &str, segment: &str) -> Option<String> {
    if !matches!(folder, "genre" | "letter") {
        return segment.parse::<i64>().ok().map(|number| number.to_string());
    }
    let bytes = (0..segment.len()).step_by(2).map(|at| segment.get(at..at + 2).and_then(|pair| u8::from_str_radix(pair, 16).ok())).collect::<Option<Vec<u8>>>()?;
    String::from_utf8(bytes).ok()
}

fn sort_key(row: &RowRef<'_>) -> String {
    crate::hierarchy::stored_sort_title(row).to_lowercase()
}

impl Runtime {
    /// The UDN: the machineIdentifier's first 32 characters as a UUID, as plexlab's was of its own.
    fn dlna_uuid(&self) -> String {
        dashed(&self.machine.to_ascii_lowercase())
    }

    /// CONFIGID.UPNP.ORG and the description's `configId`: 24 bits, as plexlab's 9821517. rex's is
    /// read off what the description says, so it changes when the description does.
    fn dlna_config(&self, uuid: &str) -> u32 {
        let version = self.pms_identity().version.clone();
        let digest = Sha1::digest(format!("{uuid}\n{}\n{version}\n{:?}", self.friendly, icons(self)).as_bytes());
        u32::from_be_bytes([0, digest[0], digest[1], digest[2]])
    }

    fn dlna_description(&self, uuid: &str) -> String {
        let version = self.pms_identity().version.split('-').next().unwrap_or_default().to_string();
        let mut icon_list = String::new();
        for (mime, width, height) in icons(self) {
            icon_list.push_str(&format!(
                "      <icon>\r\n        <mimetype>{mime}</mimetype>\r\n        <width>{width}</width>\r\n        <height>{height}</height>\r\n\
                 \x20       <depth>24</depth>\r\n        <url>/proxy/icon/{}</url>\r\n      </icon>\r\n",
                icon_file(mime, width, height)
            ));
        }
        let mut services = String::new();
        for (kind, id, name) in SERVICES {
            services.push_str(&format!(
                "      <service>\r\n        <serviceType>{kind}</serviceType>\r\n        <serviceId>{id}</serviceId>\r\n\
                 \x20       <SCPDURL>/{name}/{uuid}/scpd.xml</SCPDURL>\r\n        <controlURL>/{name}/{uuid}/control.xml</controlURL>\r\n\
                 \x20       <eventSubURL>/{name}/{uuid}/event.xml</eventSubURL>\r\n      </service>\r\n"
            ));
        }
        format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\r\n<root configId=\"{}\" xmlns=\"urn:schemas-upnp-org:device-1-0\" \
             xmlns:dlna=\"urn:schemas-dlna-org:device-1-0\">\r\n  <specVersion>\r\n    <major>1</major>\r\n    <minor>1</minor>\r\n  \
             </specVersion>\r\n  <device>\r\n    <deviceType>{MEDIA_SERVER}</deviceType>\r\n    <friendlyName>Plex Media Server: {}</friendlyName>\r\n\
             \x20   <manufacturer>Plex, Inc.</manufacturer>\r\n    <manufacturerURL>http://www.plexapp.com/</manufacturerURL>\r\n\
             \x20   <modelDescription>Plex Media Server</modelDescription>\r\n    <modelName>Plex Media Server</modelName>\r\n\
             \x20   <modelNumber>{}</modelNumber>\r\n    <modelURL>http://www.plexapp.com/</modelURL>\r\n    <UDN>uuid:{uuid}</UDN>\r\n\
             \x20   <dlna:X_DLNADOC xmlns:dlna=\"urn:schemas-dlna-org:device-1-0\">DMS-1.50</dlna:X_DLNADOC>\r\n    <iconList>\r\n{icon_list}\
             \x20   </iconList>\r\n    <serviceList>\r\n{services}    </serviceList>\r\n  </device>\r\n</root>",
            self.dlna_config(uuid),
            esc(&self.friendly),
            esc(&version)
        )
    }

    /// A SOAP action on one of the three services. No `SOAPACTION` is plexlab's `500 Bad Request`
    /// with no body; an action the service does not list is fault 401.
    fn dlna_control(&self, service: &str, request: &Request, host: &str) -> Answer {
        let Some(action) = request.headers.get("soapaction").and_then(|value| value.trim_matches('"').rsplit_once('#')).map(|(_, action)| action.to_string()) else {
            let mut answer = Answer::empty(500);
            answer.reason = "Bad Request";
            return answer;
        };
        let kind = SERVICES.iter().find(|(_, _, name)| *name == service).map_or("", |(kind, _, _)| *kind);
        let args = arguments(&request.body);
        let update = self.catalog.generation() & 0xffff_ffff;
        let out = match (service, action.as_str()) {
            ("ContentDirectory", "Browse") => return self.dlna_browse(&args, host, update),
            ("ContentDirectory", "GetSortCapabilities") => "<SortCaps></SortCaps>".to_string(),
            ("ContentDirectory", "GetSearchCapabilities") => "<SearchCaps></SearchCaps>".to_string(),
            ("ContentDirectory", "GetSystemUpdateID") => format!("<Id>{update}</Id>"),
            ("ConnectionManager", "GetProtocolInfo") => {
                let source = self.server_pref("DlnaDefaultProtocolInfo").map(|value| string_value(&value)).unwrap_or_default();
                format!("<Source>{}</Source><Sink></Sink>", esc_text(&source))
            }
            ("ConnectionManager", "GetCurrentConnectionIDs") => "<ConnectionIDs>0</ConnectionIDs>".to_string(),
            ("ConnectionManager", "GetCurrentConnectionInfo") => "<RcsID>-1</RcsID><AVTransportID>-1</AVTransportID><ProtocolInfo>http-get:*:*:*</ProtocolInfo>\
                <PeerConnectionManager>/</PeerConnectionManager><PeerConnectionID>-1</PeerConnectionID><Direction>Output</Direction><Status>Unknown</Status>"
                .to_string(),
            ("X_MS_MediaReceiverRegistrar", "IsAuthorized" | "IsValidated") => "<Result>1</Result>".to_string(),
            ("X_MS_MediaReceiverRegistrar", "RegisterDevice") => "<RegistrationRespMsg></RegistrationRespMsg>".to_string(),
            _ => return Answer::fault(401, "Invalid Action"),
        };
        Answer::soap(200, &format!("<u:{action}Response xmlns:u=\"{kind}\">{out}</u:{action}Response>"))
    }

    /// Browse, measured: a flag other than the two is fault 701 `Invalid Name`, an unknown object
    /// 701 `No such object`; BrowseMetadata answers the object, of an ITEM BrowseDirectChildren
    /// answers the item itself with its own id as its parent, and RequestedCount 0 is every child.
    fn dlna_browse(&self, args: &HashMap<String, String>, host: &str, update: u64) -> Answer {
        let flag = args.get("BrowseFlag").map(String::as_str).unwrap_or("");
        if !matches!(flag, "BrowseMetadata" | "BrowseDirectChildren") {
            return Answer::fault(701, "Invalid Name");
        }
        let id = args.get("ObjectID").map(String::as_str).unwrap_or("");
        let Some(node) = self.dlna_node(id) else {
            return Answer::fault(701, "No such object");
        };
        let number = |name: &str| args.get(name).and_then(|value| value.trim().parse::<usize>().ok()).unwrap_or(0);
        let (nodes, total) = if flag == "BrowseMetadata" {
            (vec![node], 1)
        } else if matches!(node.entry, Entry::Movie(_) | Entry::Episode { .. }) {
            (vec![Node { parent: node.id.clone(), ..node }], 1)
        } else {
            let children = self.dlna_children(&node);
            let total = children.len();
            let start = number("StartingIndex").min(total);
            let count = match number("RequestedCount") {
                0 => total,
                count => count,
            };
            (children.into_iter().skip(start).take(count).collect(), total)
        };
        let didl = self.dlna_didl(&nodes, host);
        Answer::soap(
            200,
            &format!(
                "<u:BrowseResponse xmlns:u=\"{DIRECTORY}\"><Result>{}</Result><NumberReturned>{}</NumberReturned><TotalMatches>{total}</TotalMatches>\
                 <UpdateID>{update}</UpdateID></u:BrowseResponse>",
                esc_text(&didl),
                nodes.len()
            ),
        )
    }

    /// The object an id names, or None. Its parent is its id less the last segment.
    fn dlna_node(&self, id: &str) -> Option<Node> {
        match id {
            "0" => return Some(folder("0", "", "", FOLDER)),
            "video" => return Some(folder("video", "0", "Video", FOLDER)),
            "music" => return Some(folder("music", "0", "Music", FOLDER)),
            "photos" => return Some(folder("photos", "0", "Photos", FOLDER)),
            "video/channels" => return Some(folder("video/channels", "video", "Video Channels", FOLDER)),
            _ => {}
        }
        let segments: Vec<&str> = id.split('/').collect();
        let section = self.dlna_section(segments[0].strip_prefix('s')?)?;
        let parent = |depth: usize| segments[..depth].join("/");
        if segments.len() == 1 {
            return Some(self.library_node(section));
        }
        let (folder_key, folder_title) = *folders(section.get("type").and_then(Value::as_str)?).iter().find(|(key, _)| *key == segments[1])?;
        let bucketed = matches!(folder_key, "genre" | "year" | "decade" | "letter");
        if segments.len() == 2 {
            return Some(folder(id, parent(1), folder_title, FOLDER));
        }
        let listing = Listing { section, folder: folder_key, bucket: if bucketed { Some(bucket_value(folder_key, segments[2])?) } else { None } };
        let depth = if bucketed { 3 } else { 2 };
        if bucketed && segments.len() == 3 {
            let value = listing.bucket.clone()?;
            return Some(self.bucket_node(id.to_string(), parent(2), folder_key, &value));
        }
        let rest = &segments[depth..];
        let title = self.catalog.lazy_get(rest[0])?;
        if title.get("librarySectionID").map(string_value) != section.get("key").map(string_value) {
            return None;
        }
        let kind = title.get("type").and_then(Field::as_str).unwrap_or("").to_string();
        match (rest.len(), kind.as_str()) {
            (1, "movie") => Some(Node { id: id.to_string(), parent: parent(depth), entry: Entry::Movie(rest[0].to_string()) }),
            (1, "show") => Some(Node { id: id.to_string(), parent: parent(depth), entry: Entry::Show(rest[0].to_string()) }),
            (1, "episode") if listing.kind() == "show" && folder_key == "added" => {
                Some(Node { id: id.to_string(), parent: parent(depth), entry: Entry::Episode { key: rest[0].to_string(), bare: false, series: listing.title() } })
            }
            (2 | 3, "show") => {
                let show = rest[0].to_string();
                let (entry, bare) = if rest[1] == "all" {
                    (Entry::AllEpisodes(show.clone()), true)
                } else {
                    let season = self.catalog.lazy_get(rest[1])?;
                    if season.get("parentRatingKey").map(string_value).as_deref() != Some(show.as_str()) {
                        return None;
                    }
                    (Entry::Season(rest[1].to_string()), false)
                };
                if rest.len() == 2 {
                    return Some(Node { id: id.to_string(), parent: parent(depth + 1), entry });
                }
                let episode = self.catalog.lazy_get(rest[2])?;
                let in_show = if bare {
                    episode.get("grandparentRatingKey").map(string_value).as_deref() == Some(show.as_str())
                } else {
                    episode.get("parentRatingKey").map(string_value).as_deref() == Some(rest[1])
                };
                if !in_show || episode.get("type").and_then(Field::as_str) != Some("episode") {
                    return None;
                }
                let series = if bare { listing.title() } else { title.get("title").map(string_value).unwrap_or_default() };
                Some(Node { id: id.to_string(), parent: parent(depth + 2), entry: Entry::Episode { key: rest[2].to_string(), bare, series } })
            }
            _ => None,
        }
    }

    /// A movie or show library rex serves to DLNA, by key.
    fn dlna_section(&self, key: &str) -> Option<&Value> {
        self.section(key).filter(|section| matches!(section.get("type").and_then(Field::as_str), Some("movie" | "show")))
    }

    fn library_node(&self, section: &Value) -> Node {
        let key = section.get("key").map(string_value).unwrap_or_default();
        let title = section.get("title").map(string_value).unwrap_or_default();
        // Measured: a show library's container carries its own title as its series title.
        let series = (section.get("type").and_then(Value::as_str) == Some("show")).then(|| title.clone());
        Node { id: format!("s{key}"), parent: "video".to_string(), entry: Entry::Folder { title, class: PLAYLIST, series } }
    }

    fn bucket_node(&self, id: String, parent: String, folder: &str, value: &str) -> Node {
        // Measured: a genre is `object.container.genre.musicGenre`, in a video library too.
        let class = if folder == "genre" { "object.container.genre.musicGenre" } else { FOLDER };
        Node { id, parent, entry: Entry::Folder { title: bucket_title(folder, value), class, series: None } }
    }

    /// A container's children in their measured order, from the rows' hot fields only.
    fn dlna_children(&self, node: &Node) -> Vec<Node> {
        let id = node.id.as_str();
        let child = |key: String, entry: Entry| Node { id: format!("{id}/{key}"), parent: id.to_string(), entry };
        match (id, &node.entry) {
            ("0", _) => vec![folder("video", "0", "Video", FOLDER), folder("music", "0", "Music", FOLDER), folder("photos", "0", "Photos", FOLDER)],
            ("video", _) => {
                let mut out: Vec<Node> = self
                    .catalog
                    .sections
                    .iter()
                    .filter(|section| matches!(section.get("type").and_then(Field::as_str), Some("movie" | "show")))
                    .map(|section| self.library_node(section))
                    .collect();
                out.push(folder("video/channels", "video", "Video Channels", FOLDER));
                out
            }
            (_, Entry::Show(show)) => {
                let mut out = vec![child("all".to_string(), Entry::AllEpisodes(show.clone()))];
                for season in self.children_refs(show) {
                    if season.get("type").and_then(Field::as_str) == Some("season") {
                        if let Some(key) = key_of(&season) {
                            out.push(child(key.clone(), Entry::Season(key)));
                        }
                    }
                }
                out
            }
            (_, Entry::AllEpisodes(show)) => {
                let series = self.section_of_id(id).and_then(|section| section.get("title")).map(string_value).unwrap_or_default();
                self.leaves_refs(show)
                    .iter()
                    .filter_map(|episode| key_of(episode))
                    .map(|key| child(key.clone(), Entry::Episode { key, bare: true, series: series.clone() }))
                    .collect()
            }
            (_, Entry::Season(season)) => {
                let series = self.catalog.lazy_get(season).and_then(|season| season.get("parentTitle").map(string_value)).unwrap_or_default();
                self.children_refs(season)
                    .iter()
                    .filter(|episode| episode.get("type").and_then(Field::as_str) == Some("episode"))
                    .filter_map(|episode| key_of(episode))
                    .map(|key| child(key.clone(), Entry::Episode { key, bare: false, series: series.clone() }))
                    .collect()
            }
            _ => self.listing_children(node),
        }
    }

    /// The section an id under `s<key>` is in.
    fn section_of_id(&self, id: &str) -> Option<&Value> {
        self.dlna_section(id.split('/').next()?.strip_prefix('s')?)
    }

    /// A library's folders, a folder's buckets, and the titles of a folder or bucket.
    fn listing_children(&self, node: &Node) -> Vec<Node> {
        let id = node.id.as_str();
        let segments: Vec<&str> = id.split('/').collect();
        let Some(section) = self.section_of_id(id) else {
            return Vec::new();
        };
        let kind = section.get("type").and_then(Value::as_str).unwrap_or("");
        if segments.len() == 1 {
            return folders(kind).iter().map(|(key, title)| folder(format!("{id}/{key}"), id, *title, FOLDER)).collect();
        }
        let Some((folder_key, _)) = folders(kind).iter().find(|(key, _)| *key == segments[1]).copied() else {
            return Vec::new();
        };
        let bucketed = matches!(folder_key, "genre" | "year" | "decade" | "letter");
        if bucketed && segments.len() == 2 {
            return self
                .dlna_buckets(section, folder_key)
                .into_iter()
                .map(|value| self.bucket_node(format!("{id}/{}", bucket_segment(folder_key, &value)), id.to_string(), folder_key, &value))
                .collect();
        }
        let bucket = if bucketed { segments.get(2).and_then(|segment| bucket_value(folder_key, segment)) } else { None };
        let listing = Listing { section, folder: folder_key, bucket };
        let series = listing.title();
        self.dlna_titles(&listing)
            .iter()
            .filter_map(|row| {
                let key = key_of(row)?;
                let entry = match row.get("type").and_then(Field::as_str) {
                    Some("movie") => Entry::Movie(key.clone()),
                    Some("show") => Entry::Show(key.clone()),
                    _ => Entry::Episode { key: key.clone(), bare: false, series: series.clone() },
                };
                Some(Node { id: format!("{id}/{key}"), parent: id.to_string(), entry })
            })
            .collect()
    }

    /// A library's titles of its own type (a show library's Recently Added: its episodes).
    fn section_titles(&self, listing: &Listing<'_>) -> Vec<RowRef<'_>> {
        let key = listing.section.get("key").map(string_value).unwrap_or_default();
        if listing.kind() == "show" && listing.folder == "added" {
            return self.typed_section_refs(&key, 4);
        }
        let kind = listing.kind().to_string();
        self.catalog.lazy_by(crate::catalog::By::Section, &key).into_iter().filter(|row| row.get("type").and_then(Field::as_str) == Some(kind.as_str())).collect()
    }

    /// Each row's genres, read off the search index (`corpus.rs`) rather than each row's body.
    fn dlna_genres(&self, rows: &[RowRef<'_>]) -> Vec<Vec<String>> {
        let hot = self.catalog.hot();
        let (corpus, names) = hot.corpus();
        let slot = crate::corpus::CORPUS_TAG_FIELDS.iter().position(|field| *field == "Genre").unwrap_or(2);
        let ids: Vec<Vec<u32>> = rows
            .iter()
            .map(|row| corpus.get(row.hot.position).map(|entry| names.tags(entry, slot).iter().map(|(_, id)| *id).collect()).unwrap_or_default())
            .collect();
        let wanted: HashSet<u32> = ids.iter().flatten().copied().collect();
        let named: HashMap<u32, &str> = names.each().filter(|(_, id)| wanted.contains(id)).map(|(name, id)| (id, name)).collect();
        ids.into_iter().map(|ids| ids.iter().filter_map(|id| named.get(id).map(|name| name.to_string())).collect()).collect()
    }

    fn dlna_buckets(&self, section: &Value, folder: &str) -> Vec<String> {
        let listing = Listing { section, folder: "all", bucket: None };
        let rows = self.section_titles(&listing);
        let mut values: Vec<String> = match folder {
            "genre" => self.dlna_genres(&rows).into_iter().flatten().collect(),
            "year" => rows.iter().filter_map(|row| row.get("year").and_then(value_as_i64)).map(|year| year.to_string()).collect(),
            "decade" => rows.iter().filter_map(|row| row.get("year").and_then(value_as_i64)).map(|year| (year.div_euclid(10) * 10).to_string()).collect(),
            _ => rows.iter().map(|row| axes::first_letter(row)).collect(),
        };
        values.sort();
        values.dedup();
        // Measured: years and decades newest first, genres and letters in order.
        if matches!(folder, "year" | "decade") {
            values.reverse();
        }
        values
    }

    /// A folder's or bucket's titles in plexlab's order: by title, the recent ones newest first.
    fn dlna_titles(&self, listing: &Listing<'_>) -> Vec<RowRef<'_>> {
        let mut rows = self.section_titles(listing);
        let bucket = listing.bucket.as_deref().unwrap_or("");
        let year = |row: &RowRef<'_>| row.get("year").and_then(value_as_i64);
        match listing.folder {
            "unwatched" if listing.kind() == "show" => rows.retain(|row| self.has_unwatched(row)),
            "unwatched" => rows.retain(|row| !row.get("viewCount").is_some_and(truthy)),
            "viewed" => rows.retain(|row| row.get("lastViewedAt").is_some_and(truthy)),
            "resume" => rows.retain(|row| row.get("viewOffset").is_some_and(truthy)),
            "year" => rows.retain(|row| year(row).map(|year| year.to_string()).as_deref() == Some(bucket)),
            "decade" => rows.retain(|row| year(row).map(|year| (year.div_euclid(10) * 10).to_string()).as_deref() == Some(bucket)),
            "letter" => rows.retain(|row| axes::first_letter(row) == bucket),
            "genre" => {
                let genres = self.dlna_genres(&rows);
                let mut keep = genres.iter().map(|names| names.iter().any(|name| name == bucket));
                rows.retain(|_| keep.next().unwrap_or(false));
            }
            _ => {}
        }
        let number = |row: &RowRef<'_>, name: &str| row.get(name).and_then(value_as_i64).unwrap_or(0);
        match listing.folder {
            "released" => rows.sort_by_cached_key(|row| (std::cmp::Reverse(row.get("originallyAvailableAt").map(string_value).unwrap_or_default()), sort_key(row))),
            "added" => rows.sort_by_cached_key(|row| (std::cmp::Reverse(number(row, "addedAt")), sort_key(row))),
            "viewed" | "resume" => rows.sort_by_cached_key(|row| (std::cmp::Reverse(number(row, "lastViewedAt")), sort_key(row))),
            _ => rows.sort_by_cached_key(sort_key),
        }
        rows
    }

    /// The page's objects as DIDL-Lite, their rows read at once (`Catalog::read_listed`).
    fn dlna_didl(&self, nodes: &[Node], host: &str) -> String {
        let keys: Vec<String> = nodes.iter().filter_map(|node| node.entry.key().map(str::to_string)).collect();
        let rows: HashMap<String, std::borrow::Cow<'_, Value>> =
            keys.iter().cloned().zip(self.catalog.read_listed(&keys)).filter_map(|(key, row)| Some((key, row?))).collect();
        let mut out = DIDL.to_string();
        for node in nodes {
            let row = node.entry.key().and_then(|key| rows.get(key)).map(|row| row.as_ref());
            out.push_str(&self.didl_object(node, row, host));
        }
        out.push_str("</DIDL-Lite>");
        out
    }

    fn didl_object(&self, node: &Node, row: Option<&Value>, host: &str) -> String {
        let mut out = String::new();
        let tag = |out: &mut String, name: &str, text: &str| out.push_str(&format!("<{name}>{}</{name}>", esc(text)));
        let open = |out: &mut String, count: Option<i64>| {
            out.push_str(&format!("<container id=\"{}\" parentID=\"{}\" restricted=\"1\" searchable=\"0\"", esc(&node.id), esc(&node.parent)));
            if let Some(count) = count {
                out.push_str(&format!(" childCount=\"{count}\""));
            }
            out.push('>');
        };
        let text = |row: &Value, name: &str| row.get(name).map(string_value).filter(|text| !text.is_empty());
        let tags = |row: &Value, name: &str| -> Vec<String> {
            row.get(name).and_then(Value::as_array).into_iter().flatten().filter_map(|tag| tag.get("tag").map(string_value)).filter(|tag| !tag.is_empty()).collect()
        };
        let year_date = |row: &Value| row.get("year").and_then(value_as_i64).map(|year| format!("{year}-01-01"));
        let art = |out: &mut String, row: &Value, key: &str, which: &str| {
            if row.get("thumb").is_some_and(truthy) {
                match which {
                    "albumart" => out.push_str(&format!("<upnp:albumArtURI dlna:profileID=\"JPEG_MED\">http://{}/proxy/{key}/albumart.jpg</upnp:albumArtURI>", esc(host))),
                    _ => out.push_str(&format!("<upnp:icon>http://{}/proxy/{key}/icon.jpg</upnp:icon>", esc(host))),
                }
            }
        };
        let empty = Value::Null;
        let row = row.unwrap_or(&empty);
        let key = key_of(row).unwrap_or_default();
        match &node.entry {
            Entry::Folder { title, class, series } => {
                open(&mut out, None);
                tag(&mut out, "dc:title", title);
                tag(&mut out, "dc:creator", "Unknown");
                tag(&mut out, "upnp:genre", "Unknown");
                if !title.is_empty() {
                    tag(&mut out, "dc:description", title);
                }
                if let Some(series) = series {
                    tag(&mut out, "upnp:seriesTitle", series);
                }
                tag(&mut out, "upnp:class", class);
                out.push_str("</container>");
            }
            Entry::Show(_) => {
                let title = text(row, "title").unwrap_or_default();
                open(&mut out, Some(row.get("leafCount").and_then(value_as_i64).unwrap_or_else(|| self.leaves_refs(&key).len() as i64)));
                tag(&mut out, "dc:title", &title);
                tag(&mut out, "dc:creator", &text(row, "studio").unwrap_or_else(|| "Unknown".to_string()));
                if let Some(date) = text(row, "originallyAvailableAt").or_else(|| year_date(row)) {
                    tag(&mut out, "dc:date", &date);
                }
                genres(&mut out, &tags(row, "Genre"));
                art(&mut out, row, &key, "albumart");
                tag(&mut out, "dc:description", &title);
                if let Some(summary) = text(row, "summary") {
                    tag(&mut out, "upnp:longDescription", &summary);
                }
                art(&mut out, row, &key, "icon");
                tag(&mut out, "upnp:seriesTitle", &title);
                tag(&mut out, "upnp:class", PLAYLIST);
                out.push_str("</container>");
            }
            // Measured: `All episodes` and a season are dated their year's first day, credit no one
            // and carry no genre or summary; `All episodes` is drawn with its show's poster.
            Entry::AllEpisodes(_) | Entry::Season(_) => {
                let season = matches!(node.entry, Entry::Season(_));
                let title = if season { text(row, "title").unwrap_or_default() } else { "All episodes".to_string() };
                let count = row.get("leafCount").and_then(value_as_i64).unwrap_or_else(|| self.leaves_refs(&key).len() as i64);
                open(&mut out, Some(count));
                tag(&mut out, "dc:title", &title);
                tag(&mut out, "dc:creator", "Unknown");
                let show_year = || self.catalog.lazy_get(&text(row, "parentRatingKey").unwrap_or_default()).and_then(|show| show.get("year").and_then(value_as_i64)).map(|year| format!("{year}-01-01"));
                if let Some(date) = year_date(row).or_else(|| if season { show_year() } else { None }) {
                    tag(&mut out, "dc:date", &date);
                }
                tag(&mut out, "upnp:genre", "Unknown");
                art(&mut out, row, &key, "albumart");
                tag(&mut out, "dc:description", &title);
                art(&mut out, row, &key, "icon");
                if season {
                    tag(&mut out, "upnp:seriesTitle", &text(row, "parentTitle").unwrap_or_default());
                }
                tag(&mut out, "upnp:class", if season { PLAYLIST } else { FOLDER });
                out.push_str("</container>");
            }
            Entry::Movie(_) | Entry::Episode { .. } => {
                let title = match &node.entry {
                    Entry::Episode { bare, .. } => {
                        let number = format!(
                            "S{} E{} - {}",
                            row.get("parentIndex").map(string_value).unwrap_or_default(),
                            row.get("index").map(string_value).unwrap_or_default(),
                            text(row, "title").unwrap_or_default()
                        );
                        if *bare { number } else { format!("{} - {number}", text(row, "grandparentTitle").unwrap_or_default()) }
                    }
                    _ => match row.get("year").and_then(value_as_i64) {
                        Some(year) => format!("{} ({year})", text(row, "title").unwrap_or_default()),
                        None => text(row, "title").unwrap_or_default(),
                    },
                };
                out.push_str(&format!("<item id=\"{}\" parentID=\"{}\" restricted=\"1\">", esc(&node.id), esc(&node.parent)));
                tag(&mut out, "dc:title", &title);
                tag(&mut out, "dc:creator", tags(row, "Director").first().map_or("Unknown", String::as_str));
                if let Some(date) = text(row, "originallyAvailableAt").or_else(|| year_date(row)) {
                    tag(&mut out, "dc:date", &date);
                }
                for actor in tags(row, "Role") {
                    tag(&mut out, "upnp:actor", &actor);
                }
                genres(&mut out, &tags(row, "Genre"));
                art(&mut out, row, &key, "albumart");
                tag(&mut out, "dc:description", &title);
                if let Some(summary) = text(row, "summary") {
                    tag(&mut out, "upnp:longDescription", &summary);
                }
                art(&mut out, row, &key, "icon");
                if let Entry::Episode { series, .. } = &node.entry {
                    tag(&mut out, "upnp:programTitle", &title);
                    tag(&mut out, "upnp:seriesTitle", series);
                    if let Some(index) = text(row, "index") {
                        tag(&mut out, "upnp:episodeNumber", &index);
                    }
                }
                if let Some((media, part)) = first_part(row) {
                    let container = container_of(media, part);
                    let mut res = String::from("<res");
                    if let Some(duration_ms) = media.get("duration").or_else(|| row.get("duration")).and_then(value_as_i64) {
                        res.push_str(&format!(" duration=\"{}\"", duration(duration_ms)));
                    }
                    if let Some(size) = part.get("size").and_then(value_as_i64) {
                        res.push_str(&format!(" size=\"{size}\""));
                    }
                    if let (Some(width), Some(height)) = (media.get("width").and_then(value_as_i64), media.get("height").and_then(value_as_i64)) {
                        res.push_str(&format!(" resolution=\"{width}x{height}\""));
                    }
                    // Measured: bytes per second, the Media's kbps times 125.
                    if let Some(bitrate) = media.get("bitrate").and_then(value_as_i64) {
                        res.push_str(&format!(" bitrate=\"{}\"", bitrate * 125));
                    }
                    if let Some(channels) = media.get("audioChannels").and_then(value_as_i64) {
                        res.push_str(&format!(" nrAudioChannels=\"{channels}\""));
                    }
                    res.push_str(&format!(
                        " protocolInfo=\"http-get:*:{}:{}\">http://{}/object/{key}/file.{}</res>",
                        esc(&part_type(part, &container)),
                        features(media),
                        esc(host),
                        esc(&container)
                    ));
                    out.push_str(&res);
                }
                let class = if matches!(node.entry, Entry::Movie(_)) { "object.item.videoItem.movie" } else { "object.item.videoItem" };
                tag(&mut out, "upnp:class", class);
                out.push_str("</item>");
            }
        }
        out
    }

    /// A title's poster for `albumArtURI` and `upnp:icon`, through rex's artwork fetching: an
    /// upload, a source's own artwork with its login, else the public url. rex cannot resize, so a
    /// TMDB poster is asked for at 342 wide, inside DLNA's JPEG_MED. One with none is the
    /// generated tile.
    fn dlna_art(&self, key: &str) -> Reply {
        let Some(row) = self.catalog.get(key).filter(|row| row.get("thumb").is_some_and(truthy)) else {
            return Reply::Done(Answer::empty(404));
        };
        let Some(url) = row.get("_poster_url").and_then(Value::as_str).filter(|url| !url.is_empty()) else {
            return Reply::Done(Answer::typed(200, "image/jpeg", generated_jpeg(342, 513, [32, 34, 38])));
        };
        if let Some((body, kind)) = self.upload(url) {
            return Reply::Done(Answer::typed(200, &kind, body));
        }
        match url.strip_prefix("source://").and_then(|rest| rest.split_once('/')) {
            Some((name, path)) => match self.sources.iter().find(|source| source.name == name) {
                Some(source) => {
                    let (url, headers) = source.artwork(path);
                    Reply::Art(url, Some(headers))
                }
                None => Reply::Done(Answer::empty(404)),
            },
            None => Reply::Art(transcoder::tmdb_at_width(url, Some("342")), None),
        }
    }

    /// `/object/<ratingKey>/file.<ext>`: the row's Part through the PMS port's own part read
    /// (`open_part`): its source, Range, 206 and Content-Range, a redirect source's link.
    fn dlna_part(&self, key: &str, request: &Request) -> Reply {
        let Some(row) = self.catalog.get(key).filter(|row| matches!(row.get("type").and_then(Field::as_str), Some("movie" | "episode"))) else {
            return Reply::Done(Answer::empty(404));
        };
        let Some((media, part)) = first_part(row) else {
            return Reply::Done(Answer::empty(404));
        };
        let Some((source, path)) = part.get("file").and_then(Value::as_str).and_then(|file| self.part_source(row, file)) else {
            error!("DLNA object {key}: no source serves its file");
            return Reply::Done(Answer::empty(502));
        };
        let range = request.headers.get("range").cloned();
        let mut dlna = Vec::new();
        if let Some(mode) = request.headers.get("transfermode.dlna.org") {
            dlna.push(("transferMode.dlna.org", mode.clone()));
        }
        if request.headers.get("getcontentfeatures.dlna.org").is_some_and(|value| value.trim() == "1") {
            dlna.push(("contentFeatures.dlna.org", features(media)));
        }
        let plan = PartProxy {
            source,
            path,
            window: part_window(range.as_deref(), part.get("size").and_then(Value::as_u64).unwrap_or(0)),
            head: request.method == "HEAD",
            part_id: part.get("id").map(string_value).unwrap_or_default(),
            method: request.method.clone(),
            route: format!("/object/{key}"),
            range,
            redirect: self.part_mode.starts_with("redirect"),
            redirect_only: self.part_mode == "redirect-only",
        };
        Reply::Part(PartRead { plan, kind: part_type(part, &container_of(media, part)), dlna })
    }
}

/// A title's genres, or plexlab's `Unknown` when it has none (every episode).
fn genres(out: &mut String, names: &[String]) {
    if names.is_empty() {
        out.push_str("<upnp:genre>Unknown</upnp:genre>");
    }
    for name in names {
        out.push_str(&format!("<upnp:genre>{}</upnp:genre>", esc(name)));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_search_matches_its_target_or_every_one() {
        let uuid = "78a364ad-8877-87f7-4e83-4a5c48c987d3";
        let ask = |st: &str, mx: &str| search(format!("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: {mx}\r\nST: {st}\r\n\r\n").as_bytes(), uuid);
        let (all, mx) = ask("ssdp:all", "3").unwrap();
        assert_eq!(mx, 3);
        assert_eq!(all.iter().map(|(nt, _)| nt.as_str()).collect::<Vec<_>>(), [
            "upnp:rootdevice", "uuid:78a364ad-8877-87f7-4e83-4a5c48c987d3", MEDIA_SERVER, REGISTRAR, DIRECTORY, CONNECTIONS
        ]);
        assert_eq!(ask(DIRECTORY, "1").unwrap().0, [(DIRECTORY.to_string(), format!("uuid:{uuid}::{DIRECTORY}"))]);
        assert_eq!(ask(&format!("uuid:{uuid}"), "1").unwrap().0[0].1, format!("uuid:{uuid}"));
        assert!(ask("urn:schemas-upnp-org:device:MediaRenderer:1", "1").is_none());
        assert!(search(b"NOTIFY * HTTP/1.1\r\nST: ssdp:all\r\n\r\n", uuid).is_none());
    }

    #[test]
    fn soap_arguments_and_ids() {
        let body = b"<s:Body><u:Browse xmlns:u=\"x\"><ObjectID>s1/genre/41</ObjectID><BrowseFlag>BrowseMetadata</BrowseFlag><Filter>*</Filter>\
                     <SortCriteria></SortCriteria><StartingIndex/><Odd>a &amp; b</Odd></u:Browse></s:Body>";
        let args = arguments(body);
        assert_eq!(args["ObjectID"], "s1/genre/41");
        assert_eq!(args["BrowseFlag"], "BrowseMetadata");
        assert_eq!(args["SortCriteria"], "");
        assert_eq!(args["StartingIndex"], "");
        assert_eq!(args["Odd"], "a & b");
        assert_eq!(bucket_value("genre", &bucket_segment("genre", "Sci-Fi & Fantasy")).as_deref(), Some("Sci-Fi & Fantasy"));
        assert_eq!(bucket_value("genre", "4"), None);
        assert_eq!(dashed("78a364ad887787f74e834a5c48c987d3e6fb165a"), "78a364ad-8877-87f7-4e83-4a5c48c987d3");
        assert_eq!(duration(10_030), "0:00:10.000");
        assert_eq!(duration(3_723_999), "1:02:03.000");
    }
}
