#!/usr/bin/env python3
"""Record a real PMS's DLNA answers at the edges `record_dlna.py` does not walk
(`tests/fixtures/dlna/plexlab_1434_edges_s1110.json`). Read-only: it browses no Preferences
option (browsing one SETS it, see `record_dlna.py`) and writes nothing.

Run it in the PMS's network namespace, with its DLNA server on, the walk's ids at hand:

    docker exec -i plexlab-gw python3 - < probes/record_dlna_edges.py > out.json

It measures when and from where M-SEARCH answers come (unicast to the LAN address and to
loopback, multicast at several MX, an unknown ST), BrowseMetadata and BrowseDirectChildren of an
ITEM, a page past the end, and the HTTP edges: an unknown path, HEAD, UNSUBSCRIBE, a control URL
read with GET, a POST with no SOAPACTION, a Range past a file's end, which DLNA headers a stream
answers to which request headers, and when the connection is kept.
"""
import json
import socket
import time

HOST, PORT = "127.0.0.1", 32469
CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
# plexlab's own ids, from `plexlab_1434_s1110.json`: its UDN, Big Buck Bunny, Firefly S1E1.
UUID = "78a364ad-8877-87f7-4e83-4a5c48c987d3"
MOVIE, EPISODE = "41c446bafeed2dd66013", "ca292076b0e2a5704f56"
OBJECT = "/object/%s/file.mkv" % MOVIE


def raw(method, path, headers=None, body=b"", version="HTTP/1.1", timeout=3):
    """One request on its own socket; whether the server closed it, and how long it took."""
    sock = socket.create_connection((HOST, PORT), timeout=timeout)
    lines = ["%s %s %s" % (method, path, version), "Host: %s:%d" % (HOST, PORT)]
    lines += ["%s: %s" % pair for pair in (headers or {}).items()]
    if body:
        lines.append("Content-Length: %d" % len(body))
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
    data, started, closed = b"", time.time(), False
    while len(data) < 200000:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            closed = True
            break
        data += chunk
    sock.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    return {"request": lines, "head": head.decode("latin-1").split("\r\n"),
            "body": rest.decode("utf-8", "replace"), "closed_by_server": closed,
            "seconds": round(time.time() - started, 2)}


def browse(oid, flag, start=0, count=0):
    inner = "".join("<%s>%s</%s>" % (k, v, k) for k, v in (
        ("ObjectID", oid), ("BrowseFlag", flag), ("Filter", "*"), ("StartingIndex", start),
        ("RequestedCount", count), ("SortCriteria", "")))
    envelope = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:Browse xmlns:u="%s">%s</u:Browse>'
                '</s:Body></s:Envelope>' % (CDS, inner)).encode()
    return raw("POST", "/ContentDirectory/%s/control.xml" % UUID,
               {"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": '"%s#Browse"' % CDS,
                "Connection": "close"}, envelope)


def search(dest, mx, st="upnp:rootdevice", wait=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(0.2)
    message = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: %s\r\nST: %s\r\n\r\n'
               % (mx, st))
    started = time.time()
    sock.sendto(message.encode(), dest)
    answers = []
    while time.time() < started + (wait or int(mx or 0) + 1.5):
        try:
            data, address = sock.recvfrom(8192)
            answers.append({"after": round(time.time() - started, 3), "from": list(address), "text": data.decode()})
        except socket.timeout:
            pass
    return {"to": list(dest), "mx": mx, "st": st, "answers": answers}


def main():
    lan = socket.gethostbyname(socket.gethostname())
    out = {"label": "plexlab-1.43.4-edges", "recorded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    out["search"] = [search((lan, 1900), 1), search(("127.0.0.1", 1900), 1, wait=3),
                     search(("239.255.255.250", 1900), 1, st="urn:schemas-upnp-org:device:MediaRenderer:1", wait=3)]
    out["search"] += [search(("239.255.255.250", 1900), mx) for mx in (1, 2, 2, 3, 3, 5, 8)]
    out["browse"] = {
        "item_metadata": browse(MOVIE, "BrowseMetadata"),
        "episode_metadata": browse(EPISODE, "BrowseMetadata"),
        "item_children": browse(MOVIE, "BrowseDirectChildren"),
        "root_metadata_paged": browse("0", "BrowseMetadata", 0, 5),
        "root_children_past_end": browse("0", "BrowseDirectChildren", 10, 5),
    }
    close = {"Connection": "close"}
    dlna = {"getcontentFeatures.dlna.org": "1", "transferMode.dlna.org": "Streaming"}
    out["http"] = {
        "unknown_path": raw("GET", "/nothing-here", close),
        "head_description": raw("HEAD", "/DeviceDescription.xml", close),
        "head_description_http10": raw("HEAD", "/DeviceDescription.xml", version="HTTP/1.0"),
        "head_description_keepalive": raw("HEAD", "/DeviceDescription.xml", {"Connection": "keep-alive"}),
        "unsubscribe_unknown_sid": raw("UNSUBSCRIBE", "/ContentDirectory/%s/event.xml" % UUID,
                                       dict(close, SID="uuid:00000000-0000-0000-0000-000000000000")),
        "control_get": raw("GET", "/ContentDirectory/%s/control.xml" % UUID, close),
        "soap_without_soapaction": raw("POST", "/ContentDirectory/%s/control.xml" % UUID,
                                       dict(close, **{"Content-Type": "text/xml"}),
                                       ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                                        '<s:Body><u:GetSystemUpdateID xmlns:u="%s"/></s:Body></s:Envelope>' % CDS).encode()),
        "unknown_object": raw("GET", "/object/nosuchid/file.mkv", close),
        "range_past_end": raw("GET", OBJECT, dict(close, Range="bytes=99999999-")),
        "head_no_dlna_headers": raw("HEAD", OBJECT, close),
        "head_dlna_headers": raw("HEAD", OBJECT, dict(close, **dlna)),
        "head_getcontentfeatures_only": raw("HEAD", OBJECT, dict(close, **{"getcontentFeatures.dlna.org": "1"})),
        "head_transfermode_interactive": raw("HEAD", OBJECT, dict(close, **{"transferMode.dlna.org": "Interactive"})),
        "head_albumart": raw("HEAD", "/proxy/15b7d05119eb998f87e8/albumart.jpg", close),
        "head_icon": raw("HEAD", "/proxy/fecb6a9349c859983f3e/260x260.png", close),
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
