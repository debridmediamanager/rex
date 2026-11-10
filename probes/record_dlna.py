#!/usr/bin/env python3
"""Record what a real PMS's DLNA server answers (`tests/fixtures/dlna/`).

PMS's DLNA server is off by default (`DlnaEnabled`); once on it answers SSDP on UDP 1900 and
UPnP on TCP 32469. Run this on the PMS's own network namespace, since SSDP is multicast:

    docker exec -i plexlab-gw python3 - --label NAME < probes/record_dlna.py > out.json

It turns `DlnaEnabled` off and on again through `--pms` (unclaimed lab server, no token) to
catch the byebye and alive NOTIFYs, then M-SEARCHes each search target, reads the device and
service descriptions, walks the ContentDirectory with Browse (children and metadata, a page,
an unknown object), asks every other action each service lists, and reads each `res` and
`albumArtURI` with HEAD and a two-byte Range GET. Bodies that are media or artwork are kept
as length and SHA-256 only. `--agent` repeats the walk under another User-Agent.
"""
import argparse
import hashlib
import json
import re
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from xml.etree import ElementTree as ET

GROUP = ("239.255.255.250", 1900)
TARGETS = ["ssdp:all", "upnp:rootdevice", "urn:schemas-upnp-org:device:MediaServer:1",
           "urn:schemas-upnp-org:service:ContentDirectory:1",
           "urn:schemas-upnp-org:service:ConnectionManager:1",
           "urn:microsoft.com:service:X_MS_MediaReceiverRegistrar:1"]
DIDL = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
TEXT = ("text/", "application/xml", "application/json")


def body_record(ctype, data):
    if any((ctype or "").startswith(t) for t in TEXT):
        return {"text": data.decode("utf-8", "replace")}
    return {"length": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def exchange(method, url, headers=None, body=None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        resp = urllib.request.urlopen(req, timeout=20)
        status, rheaders, data = resp.status, resp.getheaders(), resp.read()
    except urllib.error.HTTPError as err:
        status, rheaders, data = err.code, err.headers.items(), err.read()
    ctype = dict((k.lower(), v) for k, v in rheaders).get("content-type", "")
    rec = {"method": method, "path": url.split("/", 3)[3] if url.count("/") >= 3 else url,
           "request_headers": headers or {}, "status": status, "headers": rheaders}
    if body is not None:
        rec["request_body"] = body.decode()
    rec["body"] = body_record(ctype, data) if method != "HEAD" else None
    return rec, data


def soap(base, control, service, action, args):
    inner = "".join("<%s>%s</%s>" % (k, v, k) for k, v in args)
    env = ('<?xml version="1.0" encoding="utf-8"?>'
           '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
           's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
           '<u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>' % (action, service, inner, action))
    headers = {"Content-Type": 'text/xml; charset="utf-8"',
               "SOAPACTION": '"%s#%s"' % (service, action)}
    if AGENT:
        headers["User-Agent"] = AGENT
    return exchange("POST", base + control, headers, env.encode())


def search(target, wait=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(0.3)
    msg = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\n'
           'MX: 1\r\nST: %s\r\n\r\n' % target)
    s.sendto(msg.encode(), GROUP)
    out, end = [], time.time() + wait
    while time.time() < end:
        try:
            data, addr = s.recvfrom(8192)
            out.append(data.decode())
        except socket.timeout:
            pass
    return {"request": msg, "responses": out}


def listen(seconds, sink):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", 1900))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 struct.pack("4sl", socket.inet_aton(GROUP[0]), socket.INADDR_ANY))
    s.settimeout(0.5)
    end = time.time() + seconds
    while time.time() < end:
        try:
            data, addr = s.recvfrom(8192)
            text = data.decode("utf-8", "replace")
            if text.startswith("NOTIFY"):
                sink.append({"t": round(time.time(), 2), "text": text})
        except socket.timeout:
            pass


def set_pref(pms, value):
    exchange("PUT", "%s/:/prefs?DlnaEnabled=%d" % (pms, value))


def walk(base, control, root="0", limit=400):
    cds = "urn:schemas-upnp-org:service:ContentDirectory:1"
    out, queue, seen = [], [root], set()
    while queue and len(seen) < limit:
        oid = queue.pop(0)
        # A Preferences option (`<uuid>_<value>`) is a setting: browsing it SETS it for the
        # client (measured 2026-11-10, the first walk left every option at the last one read).
        if oid in seen or re.match(r"^[0-9a-f-]{36}_", oid):
            continue
        seen.add(oid)
        for flag in ("BrowseMetadata", "BrowseDirectChildren"):
            rec, data = soap(base, control, cds, "Browse",
                             [("ObjectID", oid), ("BrowseFlag", flag), ("Filter", "*"),
                              ("StartingIndex", "0"), ("RequestedCount", "0"), ("SortCriteria", "")])
            out.append(rec)
            if flag != "BrowseDirectChildren" or rec["status"] != 200:
                continue
            result = re.search(r"<Result>(.*?)</Result>", data.decode(), re.S)
            if not result:
                continue
            didl = ET.fromstring(result.group(1).replace("&lt;", "<").replace("&gt;", ">")
                                 .replace("&quot;", '"').replace("&amp;", "&"))
            for node in didl:
                if node.tag == "{%s}container" % DIDL:
                    queue.append(node.get("id"))
                else:
                    ITEMS.append(node)
    return out


AGENT = None
ITEMS = []


def main():
    global AGENT
    ap = argparse.ArgumentParser()
    ap.add_argument("--pms", default="http://127.0.0.1:32400")
    ap.add_argument("--base", default="http://127.0.0.1:32469")
    ap.add_argument("--label", required=True)
    ap.add_argument("--agent", action="append", default=[])
    ap.add_argument("--notify-seconds", type=float, default=20)
    args = ap.parse_args()
    rec = {"label": args.label, "recorded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    notifies = []
    t = threading.Thread(target=listen, args=(args.notify_seconds, notifies))
    t.start()
    time.sleep(1)
    set_pref(args.pms, 0)
    time.sleep(4)
    set_pref(args.pms, 1)
    t.join()
    rec["notify"] = notifies
    time.sleep(2)
    rec["search"] = [search(st) for st in TARGETS]

    desc, data = exchange("GET", args.base + "/DeviceDescription.xml")
    rec["description"] = desc
    root = ET.fromstring(data)
    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    rec["descriptions"], rec["icons"], rec["actions"] = [], [], []
    for icon in root.iterfind(".//d:icon/d:url", ns):
        rec["icons"].append(exchange("GET", args.base + icon.text)[0])
    services = []
    for svc in root.iterfind(".//d:service", ns):
        stype = svc.find("d:serviceType", ns).text
        scpd, sdata = exchange("GET", args.base + svc.find("d:SCPDURL", ns).text)
        rec["descriptions"].append(scpd)
        actions = [a.text for a in ET.fromstring(sdata).iterfind(
            ".//{urn:schemas-upnp-org:service-1-0}action/{urn:schemas-upnp-org:service-1-0}name")]
        services.append((stype, svc.find("d:controlURL", ns).text, actions))
        rec["actions"].append(exchange("GET", args.base + svc.find("d:eventSubURL", ns).text)[0])
        rec["actions"].append(exchange("SUBSCRIBE", args.base + svc.find("d:eventSubURL", ns).text,
                                       {"CALLBACK": "<http://127.0.0.1:9/>", "NT": "upnp:event",
                                        "TIMEOUT": "Second-300"})[0])
    args_for = {
        "GetCurrentConnectionInfo": [("ConnectionID", "0")],
        "IsAuthorized": [("DeviceID", "")], "IsValidated": [("DeviceID", "")],
        "RegisterDevice": [("RegistrationReqMsg", "")],
        "Search": [("ContainerID", "0"), ("SearchCriteria", 'upnp:class derivedfrom "object.item.videoItem"'),
                   ("Filter", "*"), ("StartingIndex", "0"), ("RequestedCount", "5"), ("SortCriteria", "")],
        "X_GetFeatureList": [], "X_SetBookmark": [("CategoryType", ""), ("RID", ""), ("ObjectID", ""), ("PosSecond", "0")],
    }
    cds_control = None
    for stype, control, actions in services:
        if stype.endswith("ContentDirectory:1"):
            cds_control = control
        for action in actions:
            if action == "Browse":
                continue
            rec["actions"].append(soap(args.base, control, stype, action, args_for.get(action, []))[0])
        rec["actions"].append(soap(args.base, control, stype, "NoSuchAction", [])[0])

    cds = "urn:schemas-upnp-org:service:ContentDirectory:1"
    for agent in [None] + args.agent:
        AGENT = agent
        del ITEMS[:]
        browse = walk(args.base, cds_control)
        extra = [soap(args.base, cds_control, cds, "Browse", a)[0] for a in (
            [("ObjectID", "0"), ("BrowseFlag", "BrowseDirectChildren"), ("Filter", "*"),
             ("StartingIndex", "1"), ("RequestedCount", "2"), ("SortCriteria", "")],
            [("ObjectID", "no-such-object"), ("BrowseFlag", "BrowseDirectChildren"), ("Filter", "*"),
             ("StartingIndex", "0"), ("RequestedCount", "0"), ("SortCriteria", "")],
            [("ObjectID", "0"), ("BrowseFlag", "Nonsense"), ("Filter", "*"),
             ("StartingIndex", "0"), ("RequestedCount", "0"), ("SortCriteria", "")])]
        reads = []
        for item in ITEMS[:6]:
            for res in item.iterfind("{%s}res" % DIDL):
                url = res.text
                hdr = {"getcontentFeatures.dlna.org": "1", "transferMode.dlna.org": "Streaming"}
                if agent:
                    hdr["User-Agent"] = agent
                reads.append(exchange("HEAD", url, hdr)[0])
                reads.append(exchange("GET", url, dict(hdr, Range="bytes=0-1"))[0])
            art = item.find("{urn:schemas-upnp-org:metadata-1-0/upnp/}albumArtURI")
            if art is not None:
                reads.append(exchange("GET", art.text)[0])
        rec.setdefault("walks", []).append({"agent": agent, "browse": browse, "extra": extra,
                                            "reads": reads})
    json.dump(rec, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
