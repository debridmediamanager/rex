"""GDM -- Plex's LAN discovery protocol: the UDP half of being FOUND.

A client on the same network does not ask plex.tv where the server is.  It sends
one datagram to a multicast group and every Plex Media Server on the wire answers
with a seven-line block naming itself.  rex answered nothing at all, so no
official client could find it without a plex.tv account or a hand-typed address --
which is the "detected rather than merely reachable" half of `heartbeat.py`, on the
LAN instead of through plex.tv.

Everything here is measured, 2026-09-18, on four servers:

  * the 1.42.2 reference        1.42.2, CLAIMED    -- the exact build rex reports
  * plexlab    1.43.4, CLAIMED    -- the reference PMS on plexlab
  * plexscratch42 1.42.2, UNCLAIMED
  * plexscratch   1.43.4, UNCLAIMED

The two versions answer byte-identical blocks apart from their own values, so
nothing here is version-split.  The CLAIM is what splits it -- see `Host` below.

The measurements
----------------

**Two ports, and only two.** A real PMS answers a GDM query on UDP **32410** and
UDP **32414** and on nothing else: 32400-32409, 32411, 32412, 32413 and 32415-32419
were swept on the 1.42.2 reference and answer nothing.  32412/32413 are the PLAYER side
of the same protocol -- a server neither listens on them nor answers them.

**Both ports are joined to 239.0.0.250 and both answer a broadcast.**  Sent from
the 1.42.2 reference's own LAN: `239.0.0.250:32410`, `239.0.0.250:32414`,
`255.255.255.255:32410`, `255.255.255.255:32414` and the subnet broadcast each drew a
reply from the 1.42.2 reference AND from the second server on that wire.
`239.0.0.250:32412` and `:32413` drew none.  The reply is unicast back to the datagram's
source, from the port it arrived on.

**The trigger is a PREFIX COMPARE, not a parse.**  The datagram and the constant
``b"M-SEARCH * "`` must agree over ``min(len(datagram), 11)`` bytes.  Bisected byte by
byte on the 1.42.2 reference:

  * every proper prefix answers -- including the EMPTY datagram, and ``b"M"``;
  * one wrong byte at any of the eleven positions answers nothing
    (``M-SEARCH *X`` and ``M-SEARCH *\r\n`` are silent; the eleventh byte must be
    the space);
  * anything at all may follow the eleven bytes -- ``M-SEARCH * ZZZZ`` answers, and
    so does a 2 KB datagram;
  * it is case-SENSITIVE (``m-search`` is silent) and unanchored forms are silent
    (a leading space, a leading CRLF, ``MSEARCH``, ``M-SEARCH\t*``, ``M-SEARCH  *``).

That is why a client's own ``M-SEARCH * HTTP/1.1\r\n`` works and why so does
``M-SEARCH * HTTP/1.0\r\n\r\n``: the version and the terminator are never read.

**The reply.**  ``HTTP/1.0 200 OK`` and then the headers in ALPHABETICAL order,
CRLF line endings, terminated by a blank line:

    HTTP/1.0 200 OK\r\n
    Content-Type: plex/media-server\r\n
    Host: <certificate hash>.plex.direct\r\n      <- CLAIMED servers only
    Name: <friendlyName>\r\n
    Port: <the TCP port>\r\n
    Resource-Identifier: <machineIdentifier>\r\n
    Updated-At: <seconds>\r\n
    Version: <full version string>\r\n
    \r\n

**`Host` is the one field the claim moves.**  Both claimed references carry it and
neither unclaimed copy carries it at all -- the line is absent, not empty.  An
unclaimed PMS has no plex.direct certificate, so the claim and the certificate are
confounded on these four instruments; rex therefore emits the line only when it
actually holds a plex.direct hostname, which is the honest reading either way.

**A real PMS also SENDS.**  Listening on the group from its own LAN, the 1.42.2
reference emits ``b"M-SEARCH * HTTP/1.1\r\n"`` -- 21 bytes, ONE CRLF, not the doubled
terminator -- to 239.0.0.250:32414 every **5.00 seconds**, on every interface it holds
(nine samples at 0.96, 5.96, 10.96 ... 40.97 s).  That is how `/servers` is populated:
the 1.42.2 reference's `/servers` listed itself, the second server on the wire, and --
within six seconds of rex being started with this module -- rex, at the address and port
rex advertised.  A row ages out when its server stops answering; see SERVERS_TTL.

**`Updated-At` is not a clock.**  It stands still under polling (the 1.42.2 reference
answered the same value to fifteen reads over a minute), it is stale on an idle server
-- the second server on the 1.42.2 reference's wire answered a value 3.5 days old -- and
on an IDLE unclaimed copy it did not move once in 86 samples over seventeen minutes.  It
is the time the advertisement was last (re)generated.  The two ports can even disagree:
plexscratch42 answered 1789624280 on 32410 and 1789690002 on 32414 in the same second,
and converged afterwards, so each socket carries its own last-regenerated stamp rather
than reading one server-wide value. Cycle 50 then sampled both disposable versions
across the server's write surface: library create, edit and delete move the stamp;
FriendlyName changes the advertised Name without moving it; refresh, item edit,
collection create and index deletion also leave it alone. Rex therefore stamps at
construction and touches only those three library-lifecycle hooks.
"""

import logging
import socket
import struct
import threading
import time

logger = logging.getLogger("rex.gdm")

# The two ports a real PMS answers on, measured by sweeping 32400-32419 on the 1.42.2
# reference.
PORTS = (32410, 32414)

# The GDM multicast group.  Both ports join it; the player ports (32412, 32413) are
# not the server's business.
GROUP = "239.0.0.250"

# The whole trigger.  A datagram answers when it agrees with this over
# min(len(datagram), len(QUERY)) bytes -- so "" and b"M" answer and b"M-SEARCH *X"
# does not.  The trailing space is load-bearing and was bisected.
QUERY = b"M-SEARCH * "

# What a real PMS sends when it goes looking, byte for byte: one CRLF, no blank line.
SEARCH = b"M-SEARCH * HTTP/1.1\r\n"

# Measured on the 1.42.2 reference: every 5.00 s, to GROUP:32414.
SEARCH_INTERVAL = 5.0

# How long a discovered row survives without an answer.  Bracketed on the 1.42.2
# reference by starting a server on its wire, stopping it and polling `/servers` every
# three seconds: present at 177 s, gone at 180 s.  An earlier run read (154, 165] and is
# discarded -- it reused a machineIdentifier the 1.42.2 reference had already held, so
# its row's age was not its last answer's. 180 s is the same cutoff PMS applies to a
# playback session (sessions.py).
SERVERS_TTL = 180.0

# Alphabetical, which is the order both references emit.  `Host` is inserted in
# its alphabetical place when the server has one.
CONTENT_TYPE = "plex/media-server"


def build_response(*, name, port, machine, version, host=None, updated_at=None):
    """The exact block both references answer, as bytes.

    `host` is the plex.direct hostname; when it is None the line is OMITTED, which
    is what an unclaimed PMS with no certificate does -- it does not send an empty
    one.
    """
    lines = ["HTTP/1.0 200 OK", "Content-Type: %s" % CONTENT_TYPE]
    if host:
        lines.append("Host: %s" % host)
    lines.append("Name: %s" % name)
    lines.append("Port: %d" % port)
    lines.append("Resource-Identifier: %s" % machine)
    lines.append("Updated-At: %d" % int(updated_at if updated_at is not None else time.time()))
    lines.append("Version: %s" % version)
    # The block ends with a blank line: every line, the last one included, is
    # CRLF-terminated and one more CRLF closes it.
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")


def matches(datagram):
    """The measured trigger: agree with QUERY over the shorter of the two lengths."""
    n = min(len(datagram), len(QUERY))
    return datagram[:n] == QUERY[:n]


def parse_response(datagram):
    """Read a peer's answer into a row, or None when it is not one.

    The block is HTTP-shaped but is not HTTP: the field names are the ones in
    `build_response`, and a peer that answers anything else is not a Plex server.
    """
    try:
        text = datagram.decode("utf-8", "replace")
    except Exception:
        return None
    lines = text.split("\r\n")
    if not lines or not lines[0].startswith("HTTP/1.0 200"):
        return None
    fields = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            fields[name.strip()] = value.strip()
    if fields.get("Content-Type") != CONTENT_TYPE:
        # `plex/media-player` answers the player ports; it is not a server.
        return None
    machine = fields.get("Resource-Identifier")
    if not machine:
        return None
    try:
        port = int(fields.get("Port") or 0)
    except ValueError:
        return None
    return {"machine": machine, "name": fields.get("Name") or "",
            "port": port, "version": fields.get("Version") or "",
            "host": fields.get("Host") or ""}


class Discovery(object):
    """Answers GDM on both ports, on unicast, broadcast and the multicast group."""

    def __init__(self, *, name, port, machine, version, host=None, ports=PORTS,
                 group=GROUP, bind="0.0.0.0", discover=True):
        self.name = name
        self.port = port
        self.machine = machine
        self.version = version
        self.host = host
        self.ports = tuple(ports)
        self.group = group
        self.bind = bind
        self._sockets = []
        self._threads = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Stamped when the advertisement is built, as a real PMS's is -- not read
        # off the clock per request, which is the one thing it provably is not.
        self.updated_at = int(time.time())
        self.answered = 0
        self.discover = discover
        # ADDRESS -> (row, last seen).  A real PMS's /servers is exactly this table plus
        # its own row, which is why the list is a function of the WIRE -- and why it is
        # keyed on the address the answer came from rather than on the identity in it.
        self._peers = {}

    # -- the advertised state -------------------------------------------------

    def touch(self):
        """Re-stamp `Updated-At`: the advertised state changed."""
        with self._lock:
            self.updated_at = int(time.time())

    def response(self):
        with self._lock:
            return build_response(name=self.name, port=self.port, machine=self.machine,
                                  version=self.version, host=self.host,
                                  updated_at=self.updated_at)

    # -- the sockets ----------------------------------------------------------

    def _open(self, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # A real PMS shares the group with every other Plex process on the host;
        # SO_REUSEPORT is what lets a second one bind the same port at all.  It does
        # not exist everywhere, so it is best-effort.
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", port))
        try:
            mreq = struct.pack("4sl", socket.inet_aton(self.group), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as error:
            # A host with no multicast route still answers unicast, which is what a
            # Tailscale peer sends.  Losing the group is a reduced service, not a
            # failure to start.
            logger.warning("GDM: could not join %s on %d: %s", self.group, port, error)
        sock.settimeout(0.5)
        return sock

    def _serve(self, sock, port):
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            if not matches(data):
                continue
            try:
                sock.sendto(self.response(), addr)
                with self._lock:
                    self.answered += 1
            except OSError as error:
                logger.debug("GDM: could not answer %s: %s", addr, error)

    # -- the search half ------------------------------------------------------

    def peers(self, now=None):
        """Every server seen on the wire recently, in first-seen order.

        A row that has stopped answering ages out: SERVERS_TTL is measured, not
        chosen.  rex's own machineIdentifier is never in here -- its reply to its
        own search is dropped, so `/servers` carries one self row and no duplicate.
        """
        now = time.time() if now is None else now
        with self._lock:
            live = [(row, seen) for row, seen in self._peers.values()
                    if now - seen <= SERVERS_TTL]
            return [dict(row) for row, _seen in live]

    def _record(self, datagram, addr):
        """Record a peer's answer, keyed by its ADDRESS.

        Cycle 50, measured on the 1.42.2 reference with three responders at one address
        answering its searches: `/servers` never listed two rows for that address -- the
        single row alternates, last writer wins -- while distinct addresses are listed
        side by side. So the table is keyed on where the answer came FROM, not on the
        identity in it, and rex keying on the machineIdentifier listed two servers on
        one host twice.
        """
        row = parse_response(datagram)
        if row is None or row["machine"] == self.machine:
            return
        row = dict(row, address=addr[0])
        with self._lock:
            # A dict preserves insertion order, and a real PMS's list keeps an address
            # where it first appeared, so an existing key is updated in place.
            self._peers[addr[0]] = (row, time.time())

    def _search(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        try:
            while not self._stop.is_set():
                try:
                    sock.sendto(SEARCH, (self.group, self.ports[-1]))
                except OSError as error:
                    logger.debug("GDM: search failed: %s", error)
                deadline = time.time() + SEARCH_INTERVAL
                while time.time() < deadline and not self._stop.is_set():
                    try:
                        data, addr = sock.recvfrom(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    self._record(data, addr)
        finally:
            sock.close()

    def start(self):
        for port in self.ports:
            try:
                sock = self._open(port)
            except OSError as error:
                logger.warning("GDM: could not bind udp/%d: %s -- LAN clients will not "
                               "discover this server on that port", port, error)
                continue
            self._sockets.append(sock)
            thread = threading.Thread(target=self._serve, args=(sock, port),
                                      name="gdm-%d" % port, daemon=True)
            thread.start()
            self._threads.append(thread)
        if self._sockets and self.discover:
            # A real PMS searches as well as answers, every 5 s; `/servers` is what
            # that search found.
            thread = threading.Thread(target=self._search, name="gdm-search", daemon=True)
            thread.start()
            self._threads.append(thread)
        if self._sockets:
            logger.info("GDM discovery on udp/%s (group %s) as %r",
                        ",".join(str(s.getsockname()[1]) for s in self._sockets),
                        self.group, self.name)
        return bool(self._sockets)

    def stop(self):
        self._stop.set()
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2)
        self._sockets = []
        self._threads = []
