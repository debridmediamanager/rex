#!/usr/bin/env python3
"""rex's DLNA server against a real PMS's, black-box. No credential needed.

A real PMS runs a DLNA server beside its own once `DlnaEnabled` is on: SSDP on udp/1900 and UPnP
on tcp/32469. Every expectation here is read out of plexlab's recordings (1.43.4, 2026-11-10):
`fixtures/dlna/plexlab_1434_s1110.json` (`probes/record_dlna.py`: the NOTIFYs of a toggle, each
search target, the descriptions, every action, a whole Browse walk and its streams) and
`fixtures/dlna/plexlab_1434_edges_s1110.json` (`probes/record_dlna_edges.py`: search timing, item
Browse, the HTTP edges). rex's ids are its own, so a DIDL object is compared by its shape -- element
and attribute names in order -- and its values where they are the library's.

The server is `catalog_ui.json` under `--assert-claimed --no-state`, its Part files served by a
local stub source, with `REX_DLNA_PORT`/`REX_SSDP_PORT` moved below 32768. It turns `DlnaEnabled`
on through `PUT /:/prefs`, listens for the NOTIFYs where this host loops multicast back to itself,
searches at the host's LAN address (plexlab answered no search sent to loopback), and turns it off.

    python3 tests/test_dlna.py
"""
import html
import http.server
import json
import os
import re
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from xml.etree import ElementTree as ET

from server_command import register_source, server_command
from startup_wait import poll_until
from test_ports import free_port

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RECORDED = json.load(open(os.path.join(HERE, "fixtures/dlna/plexlab_1434_s1110.json"), encoding="utf-8"))
EDGES = json.load(open(os.path.join(HERE, "fixtures/dlna/plexlab_1434_edges_s1110.json"), encoding="utf-8"))
PLEXLAB = "78a364ad-8877-87f7-4e83-4a5c48c987d3"
GROUP = "239.255.255.250"
CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
DIDL, DC, UPNP = ("{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}", "{http://purl.org/dc/elements/1.1/}",
                  "{urn:schemas-upnp-org:metadata-1-0/upnp/}")
MOVIE, SHOW = "1007", "20001"
results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", label, "" if ok else "  -- %s" % (detail,)))


def names(headers):
    """Header names in order, from a recording's pairs or a raw head's lines."""
    return [pair[0] if isinstance(pair, (list, tuple)) else pair.split(":", 1)[0] for pair in headers]


# -- the stub source: every path a file whose byte i is (7i + 13) % 251, Range honoured ----------
def byte_range(first, last):
    return bytes(((i * 7 + 13) % 251) for i in range(first, last + 1))


class Source(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    size = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        first, last, partial = 0, Source.size - 1, False
        match = re.match(r"bytes=(\d+)-(\d*)$", self.headers.get("Range") or "")
        if match:
            first, partial = int(match.group(1)), True
            last = min(int(match.group(2)), last) if match.group(2) else last
        self.send_response(206 if partial else 200)
        self.send_header("Content-Length", str(last - first + 1))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (first, last, Source.size))
        self.end_headers()
        try:
            for at in range(first, last + 1, 1 << 16):
                self.wfile.write(byte_range(at, min(at + (1 << 16) - 1, last)))
        except OSError:
            pass


# -- the wire ------------------------------------------------------------------------------------
def raw(port, method, path, headers=(), body=b"", close=True):
    """One request; (status line, header lines, body). `Connection: close` as the recorder's urllib sent."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    lines = ["%s %s HTTP/1.1" % (method, path), "Host: 127.0.0.1:%d" % port] + ["%s: %s" % pair for pair in headers]
    if body:
        lines.append("Content-Length: %d" % len(body))
    if close:
        lines.append("Connection: close")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
    data = b""
    while True:
        chunk = sock.recv(1 << 16)
        if not chunk:
            break
        data += chunk
    sock.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    head = head.decode("latin-1").split("\r\n")
    return head[0], head[1:], rest


def value(lines, name):
    return next((line.split(":", 1)[1].strip() for line in lines if line.split(":", 1)[0].lower() == name.lower()), None)


def envelope(service, action, args):
    """The recorder's own request body (`probes/record_dlna.py` `soap`)."""
    inner = "".join("<%s>%s</%s>" % (k, v, k) for k, v in args)
    return ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%s xmlns:u="%s">%s</u:%s>'
            '</s:Body></s:Envelope>' % (action, service, inner, action)).encode()


class Dlna:
    def __init__(self, port, uuid):
        self.port, self.uuid = port, uuid

    def soap(self, service_path, service, action, args=(), soapaction=True):
        headers = [("Content-Type", 'text/xml; charset="utf-8"')]
        if soapaction:
            headers.append(("SOAPACTION", '"%s#%s"' % (service, action)))
        return raw(self.port, "POST", "/%s/%s/control.xml" % (service_path, self.uuid), headers, envelope(service, action, args))

    def browse(self, oid, flag="BrowseDirectChildren", start=0, count=0):
        status, lines, body = self.soap("ContentDirectory", CDS, "Browse", [
            ("ObjectID", html.escape(oid)), ("BrowseFlag", flag), ("Filter", "*"),
            ("StartingIndex", start), ("RequestedCount", count), ("SortCriteria", "")])
        return status, lines, body.decode()


def result(text):
    """A BrowseResponse's DIDL objects and its three counts."""
    didl = ET.fromstring(html.unescape(re.search(r"<Result>(.*?)</Result>", text, re.S).group(1)))
    counts = [int(n) for n in re.search(r"<NumberReturned>(\d+)</NumberReturned><TotalMatches>(\d+)</TotalMatches>"
                                        r"<UpdateID>(\d+)</UpdateID>", text).groups()]
    return list(didl), counts


def shape(node):
    """A DIDL object's element names in order (a repeated one once, starred) and its attribute names."""
    children = []
    for child in node:
        tag = child.tag.split("}")[1]
        if children and children[-1].rstrip("*") == tag:
            children[-1] = tag + "*"
        else:
            children.append(tag)
    return node.tag.split("}")[1], list(node.attrib), children


def title(node):
    return node.find(DC + "title").text or ""


def text_of(node, tag):
    found = node.find(tag)
    return None if found is None else found.text


def fits(ours, recorded_shapes):
    """Our element names, in an order the recorded objects of the kind put them in, and every name
    each of them carries: the recorded ones differ only by what their rows hold (cast, a summary)."""
    order = []
    for _, _, children in recorded_shapes:
        for name in children:
            if name.rstrip("*") not in [n.rstrip("*") for n in order]:
                index = len(order)
                for later in children[children.index(name) + 1:]:
                    hits = [i for i, n in enumerate(order) if n.rstrip("*") == later.rstrip("*")]
                    if hits:
                        index = min(index, hits[0])
                order.insert(index, name)
    bare = [n.rstrip("*") for n in order]
    positions = [bare.index(n.rstrip("*")) if n.rstrip("*") in bare else -1 for n in ours[2]]
    always = set.intersection(*[{n.rstrip("*") for n in shape[2]} for shape in recorded_shapes])
    return -1 not in positions and positions == sorted(positions) and always <= {n.rstrip("*") for n in ours[2]} \
        and all(ours[0] == s[0] and ours[1] == s[1] for s in recorded_shapes)


def recorded_walk():
    """plexlab's walk: every object by id, its Browse children by parent, and every object's shape."""
    objects, children = {}, {}
    for rec in RECORDED["walks"][0]["browse"]:
        if rec["status"] != 200:
            continue
        oid = re.search(r"<ObjectID>(.*?)</ObjectID>", rec["request_body"]).group(1)
        nodes, _ = result(rec["body"]["text"])
        if "BrowseDirectChildren" in rec["request_body"]:
            children[oid] = nodes
        for node in nodes:
            objects.setdefault(node.get("id"), node)
    return objects, children


def recorded_child(children, parent_title_path):
    """A recorded object by its titles from the root, e.g. ("Video", "TV Shows")."""
    nodes, found = children["0"], None
    for wanted in parent_title_path:
        found = next(node for node in nodes if title(node) == wanted)
        nodes = children.get(found.get("id"), [])
    return found, nodes


# -- SSDP ----------------------------------------------------------------------------------------
def lan_address():
    """The address this host's default route leaves from, as rex finds its own (`local_interface_address`)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def search(address, port, st, mx=1, wait=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.2)
    message = 'M-SEARCH * HTTP/1.1\r\nHOST: %s:1900\r\nMAN: "ssdp:discover"\r\nMX: %d\r\nST: %s\r\n\r\n' % (GROUP, mx, st)
    started = time.time()
    sock.sendto(message.encode(), (address, port))
    answers = []
    while time.time() < started + (wait or mx + 1.5):
        try:
            data, _ = sock.recvfrom(8192)
            answers.append((time.time() - started, data.decode()))
        except socket.timeout:
            pass
    sock.close()
    return answers


class Listener:
    """The SSDP port shared with rex (both set SO_REUSEADDR and SO_REUSEPORT), in the group."""

    def __init__(self, port):
        self.port, self.notes, self.sock = port, [], None
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", port))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton(GROUP),
                                                                                 socket.inet_aton("0.0.0.0")))
        sock.settimeout(0.2)
        self.sock = sock

    def loops_back(self):
        """Whether this host delivers its own multicast to itself; without it NOTIFYs cannot be seen."""
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"rex-dlna-loop", (GROUP, self.port))
        sender.close()
        return any(text == "rex-dlna-loop" for text in self.collect(1.0, until=lambda notes: notes))

    def collect(self, seconds, until=None):
        end, got = time.time() + seconds, []
        while time.time() < end and not (until and until(got)):
            try:
                data, _ = self.sock.recvfrom(8192)
                got.append(data.decode("utf-8", "replace"))
            except socket.timeout:
                pass
        return got

    def close(self):
        self.sock.close()


def notify_checks(label, notes, recorded, uuid, location=None):
    """A toggle's NOTIFYs against plexlab's: twelve, each NT twice in its order, the same header names."""
    plexlab = [n["text"] for n in RECORDED["notify"] if ("ssdp:" + label) in n["text"]]
    nts = lambda texts: [re.search(r"\r\nNT: (.*?)\r\n", t).group(1).replace(PLEXLAB, uuid) for t in texts]
    check("%s: %d NOTIFYs, each target twice in plexlab's order" % (label, len(plexlab)), nts(notes) == nts(plexlab),
          nts(notes))
    head = lambda text: names(text.split("\r\n")[1:-2])
    check("%s: plexlab's header names in its order" % label, all(head(n) == head(plexlab[0]) for n in notes),
          notes[:1])
    check("%s: each USN the UDN and its NT" % label,
          all(re.search(r"\r\nUSN: uuid:%s(::.*)?\r\n" % uuid, n) for n in notes), notes[:1])
    if location:
        check("alive: Location is the LAN address, the lease plexlab's, the Server plexlab's",
              all(value(n.split("\r\n"), "Location") == location and value(n.split("\r\n"), "Cache-Control") == "max-age=1800"
                  and value(n.split("\r\n"), "Server") == value(plexlab[0].split("\r\n"), "Server") for n in notes),
              notes[:1])


def image_size(body):
    if body.startswith(b"\x89PNG"):
        return struct.unpack(">II", body[16:24])
    at = 2
    while at < len(body):
        marker, length = body[at + 1], struct.unpack(">H", body[at + 2:at + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", body[at + 5:at + 9])
            return width, height
        at += 2 + length
    return None


def pms(port, path, method="GET"):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method,
                                     headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body.strip().startswith(b"{") else None


def set_dlna(port, on):
    pms(port, "/:/prefs?DlnaEnabled=%d" % int(on), "PUT")


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def free_udp_port():
    while True:
        port = free_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()


def main():
    objects, walked = recorded_walk()
    shapes = lambda kind: [shape(n) for n in objects.values() if kind(n)]
    source = socketserver.ThreadingTCPServer(("127.0.0.1", free_port()), Source)
    source.daemon_threads = True
    threading.Thread(target=source.serve_forever, daemon=True).start()
    tcp, dlna_port, ssdp_port = free_port(), free_port(), free_udp_port()
    log = tempfile.TemporaryFile()
    env = dict(os.environ, REX_DLNA_PORT=str(dlna_port), REX_SSDP_PORT=str(ssdp_port),
               REX_PLEXTV_URL="http://127.0.0.1:9")
    process = subprocess.Popen(server_command("--port", tcp, "--catalog", os.path.join(HERE, "catalog_ui.json"),
                                              "--assert-claimed", "--gdm", "off", root=ROOT),
                               cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    lan = lan_address()
    private = bool(lan) and (lan.startswith(("10.", "192.168.")) or re.match(r"172\.(1[6-9]|2\d|3[01])\.", lan))
    try:
        if not poll_until(lambda: listening(tcp), 60):
            check("rex starts", False)
            return 1
        version = pms(tcp, "/identity")["MediaContainer"]["version"]
        movie = pms(tcp, "/library/metadata/%s" % MOVIE)["MediaContainer"]["Metadata"][0]
        show = pms(tcp, "/library/metadata/%s" % SHOW)["MediaContainer"]["Metadata"][0]
        Source.size = movie["Media"][0]["Part"][0]["size"]
        check("the stub registers as a source over /rex", register_source(tcp, "stub", {
            "url": "http://127.0.0.1:%d/dav" % source.server_address[1], "mount": "/rex"}) == 200)

        print("-- off by default (DlnaEnabled false), as a real PMS's")
        check("no DLNA port while DlnaEnabled is off", not listening(dlna_port))

        # Windows binds SSDP's port without sharing it (`platform::gdm_socket`): no listener there.
        listener = Listener(ssdp_port) if os.name != "nt" else None
        loops = bool(listener) and listener.loops_back()
        set_dlna(tcp, True)
        check("PUT /:/prefs?DlnaEnabled=1 opens the DLNA port, no restart", poll_until(lambda: listening(dlna_port), 10))
        if not results[-1]:
            return 1
        described = raw(dlna_port, "GET", "/DeviceDescription.xml")
        description = described[2].decode()
        uuid = re.search(r"<UDN>uuid:(.*?)</UDN>", description).group(1)
        config = re.search(r'configId="(\d+)"', description).group(1)
        rex = Dlna(dlna_port, uuid)

        print("-- SSDP")
        if not loops:
            print("  SKIP NOTIFY checks: this host does not deliver its own multicast to itself, or shares no port (Windows)")
        elif not private:
            print("  SKIP NOTIFY checks: no private LAN address (%s) for rex to announce on" % lan)
        else:
            alive = [n for n in listener.collect(6, until=lambda got: len([n for n in got if "ssdp:alive" in n]) >= 12)
                     if n.startswith("NOTIFY") and "ssdp:alive" in n]
            notify_checks("alive", alive, RECORDED, uuid, "http://%s:%d/DeviceDescription.xml" % (lan, dlna_port))
        if listener:
            listener.close()
        if not private:
            print("  SKIP M-SEARCH checks: no private LAN address (%s) to search at" % lan)
        else:
            check("an M-SEARCH sent to loopback draws no answer, as plexlab's", not search("127.0.0.1", ssdp_port, "ssdp:all"))
            for recorded in RECORDED["search"]:
                st = re.search(r"\r\nST: (.*?)\r\n", recorded["request"]).group(1)
                answers = search(lan, ssdp_port, st)
                expected = [r.replace(PLEXLAB, uuid) for r in recorded["responses"]]
                strip = lambda text: re.sub(r"(Location|BOOTID\.UPNP\.ORG|CONFIGID\.UPNP\.ORG|Date): [^\r]*", r"\1:", text)
                check("M-SEARCH %s: plexlab's %d answers, header for header" % (st, len(expected)),
                      [strip(a) for _, a in answers] == [strip(e) for e in expected], [a for _, a in answers][:2])
                lines = answers[0][1].split("\r\n") if answers else []
                check("M-SEARCH %s: LOCATION the LAN address, CONFIGID the description's" % st,
                      value(lines, "Location") == "http://%s:%d/DeviceDescription.xml" % (lan, dlna_port)
                      and value(lines, "CONFIGID.UPNP.ORG") == config and (value(lines, "BOOTID.UPNP.ORG") or "").isdigit(),
                      lines)
                half = len(answers) // 2
                check("M-SEARCH %s: every answer sent again 200 ms later" % st,
                      answers and 0.15 < answers[half][0] - answers[0][0] < 0.5, [round(a[0], 3) for a in answers])
            check("an unknown ST draws no answer", not search(lan, ssdp_port, "urn:schemas-upnp-org:device:MediaRenderer:1"))
            timed = search(lan, ssdp_port, "upnp:rootdevice", mx=3, wait=4.5)
            check("MX 3: answered within whole seconds below it", timed and timed[0][0] < 2.5, [round(a[0], 3) for a in timed])

        print("-- the device and its services")
        recorded = RECORDED["description"]
        check("description: status and plexlab's header names", described[0].split()[1] == "200"
              and names(described[1]) == names(recorded["headers"]), described[1])
        expected = recorded["body"]["text"].replace(PLEXLAB, uuid).replace('configId="9821517"', 'configId="%s"' % config)
        expected = expected.replace("Plex Media Server: plexlab", "Plex Media Server: rex-ui-fixture")
        expected = expected.replace("<modelNumber>1.43.4.10903<", "<modelNumber>%s<" % version.split("-")[0])
        expected = re.sub(r"/proxy/[0-9a-f]{20}/", "/proxy/icon/", expected)
        check("description: plexlab's byte for byte but rex's name, UDN, version, configId and icon paths",
              description == expected, description)
        check("the UDN is the machineIdentifier's as plexlab's is its own", uuid == "f1x7u4e0-0000-0000-0000-000000000000", uuid)
        head = EDGES["http"]["head_description"]["head"]
        status, lines, body = raw(dlna_port, "HEAD", "/DeviceDescription.xml")
        check("HEAD of the description: its length, no body", names(lines) == names(head[1:]) and not body
              and value(lines, "Content-Length") == str(len(description.encode())), lines)
        for scpd in RECORDED["descriptions"]:
            path = "/" + scpd["path"].replace(PLEXLAB, uuid)
            status, lines, body = raw(dlna_port, "GET", path)
            check("SCPD %s: plexlab's bytes and header names" % path.split("/")[1],
                  body.decode() == scpd["body"]["text"] and names(lines) == names(scpd["headers"]), lines)
        icons = re.findall(r"<mimetype>(.*?)</mimetype>\r\n\s*<width>(\d+)</width>\r\n\s*<height>(\d+)</height>"
                           r"\r\n\s*<depth>24</depth>\r\n\s*<url>(.*?)</url>", description)
        for (mime, width, height, url), recorded in zip(icons, RECORDED["icons"]):
            status, lines, body = raw(dlna_port, "GET", url)
            check("icon %s: rex's own %s at %sx%s, plexlab's header names" % (url, mime, width, height),
                  value(lines, "Content-Type") == mime and image_size(body) == (int(width), int(height))
                  and names(lines) == names(recorded["headers"]), (lines, image_size(body)))
        status, lines, body = raw(dlna_port, "HEAD", icons[0][3])
        check("an icon's HEAD is Platinum's bare 200", names(lines) == names(EDGES["http"]["head_icon"]["head"][1:])
              and value(lines, "Content-Length") == "0", lines)
        for rec in RECORDED["actions"]:
            path = "/" + rec["path"].replace(PLEXLAB, uuid)
            if rec["method"] in ("GET", "SUBSCRIBE"):
                status, lines, _ = raw(dlna_port, rec["method"], path, list(rec["request_headers"].items()))
                ok = status.split()[1] == str(rec["status"]) and names(lines) == names(rec["headers"])
                if rec["method"] == "SUBSCRIBE":
                    ok = ok and value(lines, "TIMEOUT") == "Second-1800" and re.match(r"uuid:[0-9a-f-]{36}$", value(lines, "SID") or "")
                check("%s %s: %d, plexlab's header names" % (rec["method"], path.split("/")[1], rec["status"]), ok, (status, lines))
                continue
            action = rec["request_headers"]["SOAPACTION"].strip('"').split("#")[1]
            status, lines, body = raw(dlna_port, "POST", path, list(rec["request_headers"].items()), rec["request_body"].encode())
            ours = re.sub(r"<Id>\d+</Id>", "<Id/>", body.decode())
            theirs = re.sub(r"<Id>\d+</Id>", "<Id/>", rec["body"]["text"])
            check("%s %s: %d, plexlab's header names and body" % (path.split("/")[1], action, rec["status"]),
                  status.split()[1] == str(rec["status"]) and names(lines) == names(rec["headers"]) and ours == theirs,
                  (status, lines, body[:300]))
        for label, method, path in (("unknown_path", "GET", "/nothing-here"),
                                    ("control_get", "GET", "/ContentDirectory/%s/control.xml" % uuid),
                                    ("unsubscribe_unknown_sid", "UNSUBSCRIBE", "/ContentDirectory/%s/event.xml" % uuid)):
            status, lines, _ = raw(dlna_port, method, path)
            check("%s: plexlab's %s" % (label, EDGES["http"][label]["head"][0]),
                  status == EDGES["http"][label]["head"][0] and names(lines) == names(EDGES["http"][label]["head"][1:]), (status, lines))
        status, lines, body = rex.soap("ContentDirectory", CDS, "GetSystemUpdateID", soapaction=False)
        check("no SOAPACTION: plexlab's `500 Bad Request`, no body",
              status == EDGES["http"]["soap_without_soapaction"]["head"][0] and not body, status)

        print("-- Browse")
        browse = {re.search(r"<ObjectID>(.*?)</ObjectID>.*?<BrowseFlag>(.*?)</", r["request_body"]).groups(): r
                  for r in RECORDED["walks"][0]["browse"]}
        status, lines, text = rex.browse("0", "BrowseMetadata")
        recorded = browse[("0", "BrowseMetadata")]
        check("the root's metadata is plexlab's byte for byte (but the UpdateID)",
              re.sub(r"<UpdateID>\d+", "", text) == re.sub(r"<UpdateID>\d+", "", recorded["body"]["text"])
              and names(lines) == names(recorded["headers"]), text)
        status, lines, text = rex.browse("0")
        nodes, counts = result(text)
        theirs = walked["0"]
        check("root: Video, Music, Photos in plexlab's shape", [title(n) for n in nodes] == [title(n) for n in theirs]
              and [shape(n) for n in nodes] == [shape(n) for n in theirs] and counts[:2] == [3, 3], text)
        nodes, counts = result(rex.browse("0", start=1, count=2)[2])
        extra = result(RECORDED["walks"][0]["extra"][0]["body"]["text"])
        check("a page: StartingIndex 1, RequestedCount 2 is plexlab's 2 of 3", counts[:2] == extra[1][:2]
              and [title(n) for n in nodes] == [title(n) for n in extra[0]], counts)
        recorded = EDGES["browse"]["root_children_past_end"]["body"]
        nodes, counts = result(rex.browse("0", start=10, count=5)[2])
        check("a page past the end: none of 3, an empty DIDL-Lite", counts[:2] == [0, 3] and not nodes
              and "&lt;/DIDL-Lite&gt;" in recorded, counts)
        for (label, args), rec in zip((("an unknown object", ("no-such-object", "BrowseDirectChildren")),
                                       ("a flag of neither kind", ("0", "Nonsense"))), RECORDED["walks"][0]["extra"][1:]):
            status, lines, text = rex.browse(*args)
            check("%s: plexlab's fault, byte for byte" % label, text == rec["body"]["text"]
                  and names(lines) == names(rec["headers"]) and status.split()[1] == "500", text)
        nodes, _ = result(rex.browse("video")[2])
        video, theirs = recorded_child(walked, ("Video",))
        check("Video: each movie and show library, then Video Channels (no Preferences)",
              [title(n) for n in nodes] == ["Debrid Movies", "Debrid TV", "Video Channels"], [title(n) for n in nodes])
        mine = {title(n): n for n in nodes}
        for ours, plexlabs in (("Debrid Movies", "Movies"), ("Debrid TV", "TV Shows"), ("Video Channels", "Video Channels")):
            recorded = next(n for n in theirs if title(n) == plexlabs)
            check("%s in plexlab's %s shape" % (ours, plexlabs), shape(mine[ours]) == shape(recorded), shape(mine[ours]))
        check("a show library carries its title as its series title", text_of(mine["Debrid TV"], UPNP + "seriesTitle") == "Debrid TV")
        check("Video Channels is empty", result(rex.browse(mine["Video Channels"].get("id"))[2])[1][:2] == [0, 0])
        for name in ("music", "photos"):
            check("%s is an empty container" % name.title(), result(rex.browse(name)[2])[1][:2] == [0, 0])
        for library, plexlabs, kept in (
                ("Debrid Movies", "Movies", {"All Movies", "Unwatched", "Recently Released", "Recently Added", "Recently Viewed",
                                             "Continue Watching", "By Genre", "By Year", "By Decade", "By First Letter"}),
                ("Debrid TV", "TV Shows", {"All Shows", "Unwatched", "Recently Added", "By Genre", "By First Letter"})):
            nodes, _ = result(rex.browse(mine[library].get("id"))[2])
            recorded = recorded_child(walked, ("Video", plexlabs))[1]
            check("%s: plexlab's folders in its order, less what rex leaves out" % library,
                  [title(n) for n in nodes] == [title(n) for n in recorded if title(n) in kept], [title(n) for n in nodes])
            check("%s: each folder in plexlab's shape" % library, all(shape(n) == shape(recorded[0]) for n in nodes))
        movies = {title(n): n.get("id") for n in result(rex.browse(mine["Debrid Movies"].get("id"))[2])[0]}
        shows = {title(n): n.get("id") for n in result(rex.browse(mine["Debrid TV"].get("id"))[2])[0]}

        print("-- movies")
        nodes, _ = result(rex.browse(movies["All Movies"])[2])
        titles = [title(n) for n in nodes]
        check("All Movies: every film titled `Title (Year)`, by title", titles == sorted(titles, key=str.lower)
              and "Tears of Steel (2012)" in titles, titles)
        item = next(n for n in nodes if n.get("id").endswith("/" + MOVIE))
        movie_shapes = shapes(lambda n: text_of(n, UPNP + "class") == "object.item.videoItem.movie")
        check("a film is an item in plexlab's element order", fits(shape(item), movie_shapes), shape(item))
        res = item.find(DIDL + "res")
        recorded_res = next(o for o in objects.values() if o.find(DIDL + "res") is not None
                            and ".mkv" in o.find(DIDL + "res").text).find(DIDL + "res")
        media = movie["Media"][0]
        check("res: plexlab's attributes in its order", list(res.attrib) == list(recorded_res.attrib), list(res.attrib))
        check("res: the first Part's size, WxH, channels, bytes per second (kbps x 125), H:MM:SS.mmm",
              res.get("size") == str(media["Part"][0]["size"]) and res.get("resolution") == "%dx%d" % (media["width"], media["height"])
              and res.get("bitrate") == str(media["bitrate"] * 125) and res.get("nrAudioChannels") == str(media["audioChannels"])
              and re.match(r"\d+:\d\d:\d\d\.000$", res.get("duration")), dict(res.attrib))
        check("res: plexlab's Matroska protocolInfo", res.get("protocolInfo") == recorded_res.get("protocolInfo"), res.get("protocolInfo"))
        check("res: /object/<key>/file.mkv on the address the client used",
              res.text == "http://127.0.0.1:%d/object/%s/file.mkv" % (dlna_port, MOVIE), res.text)
        art = item.find(UPNP + "albumArtURI")
        check("albumArtURI is a JPEG_MED on this server", art is not None and art.get(
            "{urn:schemas-dlna-org:metadata-1-0/}profileID") == "JPEG_MED" and art.text.startswith("http://127.0.0.1:%d/proxy/" % dlna_port))
        status, lines, _ = raw(dlna_port, "HEAD", art.text.split(str(dlna_port), 1)[1])
        check("a poster's HEAD is Platinum's bare 200", names(lines) == names(EDGES["http"]["head_albumart"]["head"][1:]), lines)
        status, lines, body = raw(dlna_port, "GET", art.text.split(str(dlna_port), 1)[1])
        if status.split()[1] == "200":
            check("the poster is the row's, an image", value(lines, "Content-Type").startswith("image/") and body, lines)
        else:
            print("  SKIP the poster's bytes: image.tmdb.org unreachable from here (%s)" % status)
        status, lines, text = rex.browse(item.get("id"), "BrowseMetadata")
        meta = result(text)[0][0]
        check("an item's metadata: its listing as its parent", meta.get("parentID") == movies["All Movies"]
              and title(meta) == title(item), meta.attrib)
        nodes, counts = result(rex.browse(item.get("id"))[2])
        recorded = result(EDGES["browse"]["item_children"]["body"])[0][0]
        check("an item's children: the item itself, its own id as its parent (plexlab's)",
              counts[:2] == [1, 1] and nodes[0].get("parentID") == item.get("id") == nodes[0].get("id")
              and recorded.get("parentID") == recorded.get("id"), nodes[0].attrib)
        genres = result(rex.browse(movies["By Genre"])[2])[0]
        check("By Genre: in order, each plexlab's musicGenre", [title(g) for g in genres] == sorted(title(g) for g in genres)
              and all(text_of(g, UPNP + "class") == "object.container.genre.musicGenre" for g in genres)
              and shape(genres[0]) == shape(recorded_child(walked, ("Video", "Movies", "By Genre"))[1][0]), [title(g) for g in genres])
        inside = result(rex.browse(genres[0].get("id"))[2])[0]
        check("a genre holds its films", inside and all(text_of(n, UPNP + "class") == "object.item.videoItem.movie" for n in inside))
        years = [title(n) for n in result(rex.browse(movies["By Year"])[2])[0]]
        decades = [title(n) for n in result(rex.browse(movies["By Decade"])[2])[0]]
        letters = [title(n) for n in result(rex.browse(movies["By First Letter"])[2])[0]]
        check("By Year newest first, By Decade as `2010s`, By First Letter in order",
              years == sorted(years, reverse=True) and decades == ["2020s", "2010s"] and letters == sorted(letters) == ["T", "V"],
              (years, decades, letters))
        pms(tcp, "/:/scrobble?key=1012&identifier=com.plexapp.plugins.library")
        pms(tcp, "/:/progress?key=1013&identifier=com.plexapp.plugins.library&time=120000&state=stopped")
        unwatched = [n.get("id").rsplit("/", 1)[1] for n in result(rex.browse(movies["Unwatched"])[2])[0]]
        viewed = [n.get("id").rsplit("/", 1)[1] for n in result(rex.browse(movies["Recently Viewed"])[2])[0]]
        resume = [n.get("id").rsplit("/", 1)[1] for n in result(rex.browse(movies["Continue Watching"])[2])[0]]
        check("the owner's watch state: a played film leaves Unwatched for Recently Viewed, one in progress is Continue Watching",
              "1012" not in unwatched and "1007" in unwatched and viewed[:1] == ["1012"] and resume == ["1013"], (unwatched, viewed, resume))

        print("-- shows")
        nodes, _ = result(rex.browse(shows["All Shows"])[2])
        recorded = recorded_child(walked, ("Video", "TV Shows", "All Shows"))[1][0]
        check("All Shows: a show is a playlistContainer of plexlab's shape, childCount its episodes",
              [title(n) for n in nodes] == [show["title"]] and fits(shape(nodes[0]), [shape(recorded)])
              and nodes[0].get("childCount") == str(show["leafCount"]), (shape(nodes[0]), nodes[0].attrib))
        show_id = nodes[0].get("id")
        children, _ = result(rex.browse(show_id)[2])
        recorded_children = walked[recorded.get("id")]
        seasons = [s for s in pms(tcp, "/library/metadata/%s/children" % SHOW)["MediaContainer"]["Metadata"]]
        check("a show: All episodes, then each season in order, plexlab's shapes",
              [title(n) for n in children] == ["All episodes"] + [s["title"] for s in seasons]
              and fits(shape(children[0]), [shape(recorded_children[0])])
              and all(fits(shape(n), [shape(recorded_children[1])]) for n in children[1:]), [title(n) for n in children])
        check("childCounts: the show's episodes, each season's",
              [n.get("childCount") for n in children] == [str(show["leafCount"])] + [str(s["leafCount"]) for s in seasons])
        episode_shapes = shapes(lambda n: text_of(n, UPNP + "class") == "object.item.videoItem")
        season = children[-1]
        episodes, _ = result(rex.browse(season.get("id"))[2])
        rows = pms(tcp, "/library/metadata/%s/children" % seasons[-1]["ratingKey"])["MediaContainer"]["Metadata"]
        check("a season: its episodes titled `Show - S1 E3 - Title`, the show its series title",
              [title(n) for n in episodes] == ["%s - S%s E%s - %s" % (show["title"], r["parentIndex"], r["index"], r["title"]) for r in rows]
              and all(text_of(n, UPNP + "seriesTitle") == show["title"] for n in episodes), [title(n) for n in episodes])
        check("an episode is an item in plexlab's element order", all(fits(shape(n), episode_shapes) for n in episodes),
              [shape(n) for n in episodes][:1])
        everything, _ = result(rex.browse(children[0].get("id"))[2])
        check("All episodes: `S1 E3 - Title`, the library its series title (plexlab's)",
              len(everything) == show["leafCount"] and all(re.match(r"S\d+ E\d+ - ", title(n)) for n in everything)
              and all(text_of(n, UPNP + "seriesTitle") == "Debrid TV" for n in everything), [title(n) for n in everything][:3])
        meta = result(rex.browse(season.get("id"), "BrowseMetadata")[2])[0][0]
        check("a season's metadata: its show as its parent", meta.get("parentID") == show_id, meta.attrib)
        added, _ = result(rex.browse(shows["Recently Added"])[2])
        check("a show library's Recently Added: its episodes, newest first, the library their series title",
              len(added) == show["leafCount"] and all(text_of(n, UPNP + "class") == "object.item.videoItem" for n in added)
              and all(text_of(n, UPNP + "seriesTitle") == "Debrid TV" for n in added), [title(n) for n in added][:3])

        print("-- streams")
        part = "/object/%s/file.mkv" % MOVIE
        dlna = [("getcontentFeatures.dlna.org", "1"), ("transferMode.dlna.org", "Streaming")]
        reads = RECORDED["walks"][0]["reads"]
        head = next(r for r in reads if r["method"] == "HEAD" and r["path"].endswith(".mkv"))
        ranged = next(r for r in reads if r["method"] == "GET" and r["path"].endswith(".mkv"))
        status, lines, body = raw(dlna_port, "HEAD", part, dlna)
        check("HEAD: plexlab's header names, the Part's size and type, its contentFeatures",
              status.split()[1] == "200" and names(lines) == names(head["headers"]) and not body
              and value(lines, "Content-Length") == str(Source.size) and value(lines, "Content-Type") == "video/x-matroska"
              and value(lines, "contentFeatures.dlna.org") == dict(head["headers"])["contentFeatures.dlna.org"], lines)
        status, lines, body = raw(dlna_port, "GET", part, dlna + [("Range", "bytes=0-1")])
        check("Range 0-1: 206, plexlab's header names, the source's two bytes",
              status.split()[1] == "206" and names(lines) == names(ranged["headers"]) and body == byte_range(0, 1)
              and value(lines, "Content-Range") == "bytes 0-1/%d" % Source.size, (status, lines, body))
        status, lines, body = raw(dlna_port, "GET", part, dlna + [("Range", "bytes=1000-1009")])
        check("a Range in the middle is the source's bytes", status.split()[1] == "206" and body == byte_range(1000, 1009))
        for label, headers in (("head_no_dlna_headers", []), ("head_getcontentfeatures_only", dlna[:1]),
                               ("head_transfermode_interactive", [("transferMode.dlna.org", "Interactive")])):
            status, lines, _ = raw(dlna_port, "HEAD", part, headers)
            recorded = EDGES["http"][label]["head"]
            check("%s: plexlab's header names" % label, names(lines) == names(recorded[1:])
                  and value(lines, "transferMode.dlna.org") == value(recorded, "transferMode.dlna.org"), lines)
        status, lines, body = raw(dlna_port, "GET", part, [("Range", "bytes=%d-" % (Source.size + 10))])
        recorded = EDGES["http"]["range_past_end"]
        check("a Range past the end: plexlab's 416, its header names and refusal",
              status == recorded["head"][0] and names(lines) == names(recorded["head"][1:]) and body.decode() == recorded["body"], (status, lines))
        status, lines, _ = raw(dlna_port, "GET", "/object/999999/file.mkv")
        check("an unknown object: plexlab's 404", status == EDGES["http"]["unknown_object"]["head"][0])
        print("  SKIP a peer off the private networks: every address this host can dial is its own "
              "(the gate is `feed::source_peer`, unit-tested in runtime/feed.rs)")

        print("-- off again")
        listener = Listener(ssdp_port) if loops and private else None
        set_dlna(tcp, False)
        check("PUT /:/prefs?DlnaEnabled=0 closes the DLNA port, no restart", poll_until(lambda: not listening(dlna_port), 10))
        if listener:
            bye = [n for n in listener.collect(4, until=lambda got: len([n for n in got if "ssdp:byebye" in n]) >= 12)
                   if n.startswith("NOTIFY") and "ssdp:byebye" in n]
            listener.close()
            notify_checks("byebye", bye, RECORDED, uuid)
        if private:
            time.sleep(1)
            check("no M-SEARCH answer once off", not search(lan, ssdp_port, "ssdp:all"))

        print("-- shutdown")
        if not (loops and private and os.name != "nt"):
            print("  SKIP the byebye of a SIGTERM: no NOTIFY can be seen here, or no SIGTERM (Windows)")
        else:
            listener = Listener(ssdp_port)
            set_dlna(tcp, True)
            poll_until(lambda: listening(dlna_port), 10)
            listener.collect(3, until=lambda got: len([n for n in got if "ssdp:alive" in n]) >= 12)
            process.terminate()
            bye = [n for n in listener.collect(4, until=lambda got: len([n for n in got if "ssdp:byebye" in n]) >= 12)
                   if n.startswith("NOTIFY") and "ssdp:byebye" in n]
            listener.close()
            check("SIGTERM with DLNA on: the byebye goes out before rex exits (--no-state, no saver)", len(bye) == 12, len(bye))
            check("and rex exits", process.wait(timeout=10) == 0)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        source.shutdown()
        if not all(results):
            log.seek(0)
            print("---- rex's log (tail)")
            print(log.read().decode("utf-8", "replace")[-3000:])
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    status = main()
    print("%d/%d passed" % (sum(results), len(results)))
    sys.exit(status or int(not all(results)))
