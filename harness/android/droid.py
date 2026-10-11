"""Cycle 70 instrument: Plex for Android (emulator) pinned to ONE server.

- plex.tv's resource list is replaced by exactly one server entry (the target),
  so the app can never reach fun or zen (production) and every library request
  goes to the server under test.
- plex.tv is read-only: any non-GET is answered locally with {} (never sent),
  except sign-in, which is answered with the real account's own /api/v2/user
  body, and PIN polling, which gets the account token injected. The account is
  never mutated (no device link, no settings writes).
- Direct connections to production hosts are refused outright.
"""
import json, re, urllib.request
from mitmproxy import http, ctx

PROD_HOSTS = {"192.168.88.245", "192.168.88.244", "zen", "fun"}
PROD_HASHES = ("0347ff6da1a340c7a5cba3561adf848f", "52f61baa62dc4b78ae65d126bb72698d")


class Droid:
    def load(self, loader):
        loader.add_option("rewrite", str, "", "a=>b;c=>d applied to target-server response bodies")
        for name in ("tname", "tmachine", "turi", "ttoken", "tversion", "acct", "resfile"):
            loader.add_option(name, str, "", name)
        self._user = None

    def _user_body(self):
        if self._user is None:
            req = urllib.request.Request("https://plex.tv/api/v2/user", headers={
                "X-Plex-Token": ctx.options.acct, "Accept": "application/json",
                "X-Plex-Client-Identifier": "rex-c70-droid", "X-Plex-Product": "rex-c70"})
            self._user = urllib.request.urlopen(req, timeout=20).read().decode()
        return self._user

    def server_connect(self, data):
        # The emulator resolves names itself and CONNECTs to the literal
        # address; an AAAA answer has no route from this host. Re-resolve the
        # SNI name over IPv4.
        import socket
        host, port = data.server.address
        try:
            socket.inet_pton(socket.AF_INET, host)
            return
        except OSError:
            pass
        name = data.server.sni or host
        try:
            socket.inet_pton(socket.AF_INET6, name)
            return
        except OSError:
            pass
        try:
            ip = socket.getaddrinfo(name, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
            data.server.address = (ip, port)
        except Exception as e:
            ctx.log.warn(f"c70 resolve {name}: {e}")

    def request(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host.lower()
        if host in PROD_HOSTS or any(h in host for h in PROD_HASHES) or \
                (flow.request.port == 32400 and host.startswith("192.168.88.")):
            flow.response = http.Response.make(403, b"blocked: production", {"X-C70": "prod"})
            return
        if host.endswith(".plex.direct"):
            flow.response = http.Response.make(403, b"blocked: plex.direct", {"X-C70": "direct"})
            return
        if not (host == "plex.tv" or host.endswith(".plex.tv")):
            return
        path = flow.request.path.split("?")[0]
        m = flow.request.method
        if m != "GET" and re.search(r"/users/(signin|authenticate)$", path):
            body = self._user_body()
            ct = "application/json"
            flow.response = http.Response.make(201, body.encode(), {"Content-Type": ct, "X-C70": "signin"})
            return
        if m == "POST" and re.fullmatch(r"/api/v2/(pins|users/anonymous)", path):
            return  # creating a PIN mutates nothing on the account
        if m not in ("GET", "HEAD", "OPTIONS"):
            flow.response = http.Response.make(
                200, b"{}", {"Content-Type": "application/json", "X-C70": "write-refused"})

    def response(self, flow: http.HTTPFlow):
        host = flow.request.pretty_host.lower()
        if ctx.options.rewrite and flow.response and ctx.options.turi.split("://")[1].startswith(
                f"{host}:{flow.request.port}"):
            body = flow.response.get_text(strict=False) or ""
            for pair in ctx.options.rewrite.split(";"):
                a, b = pair.split("=>")
                body = body.replace(a, b)
            flow.response.set_text(body)
        if not (host == "plex.tv" or host.endswith(".plex.tv")) or not flow.response:
            return
        path = flow.request.path.split("?")[0]
        if re.fullmatch(r"/api/v2/pins/\d+", path) and flow.request.method == "GET":
            try:
                d = json.loads(flow.response.get_text())
                d["authToken"] = ctx.options.acct
                flow.response.set_text(json.dumps(d))
                flow.response.headers["X-C70"] = "pin-auth"
            except Exception:
                pass
            return
        if "settings/bulk" in path and "favoriteLibraries" in flow.request.url:
            # The account's favourite libraries decide which servers feed Home; answer
            # them as the target server's own sections instead of writing the account.
            try:
                data = json.loads(flow.response.get_text())
                req = urllib.request.Request(ctx.options.turi + "/library/sections", headers={
                    "Accept": "application/json", "X-Plex-Token": ctx.options.ttoken})
                dirs = json.load(urllib.request.urlopen(req, timeout=10))["MediaContainer"].get("Directory") or []
                libs = [{"librarySectionID": d["key"], "serverId": ctx.options.tmachine,
                         "serverName": ctx.options.tname, "serverOwnerId": 316687974,
                         "ownerName": "plexa630", "type": d["type"], "title": d["title"],
                         "key": "/library/sections/%s" % d["key"],
                         "hubKey": "/hubs/sections/%s" % d["key"]} for d in dirs]
                for entry in data:
                    if entry.get("key") == "favoriteLibraries":
                        entry["value"] = dict(entry.get("value") or {}, libraries=libs)
                flow.response.set_text(json.dumps(data))
                flow.response.headers["X-C70"] = "favorites"
            except Exception as e:
                ctx.log.warn(f"c70 favorites: {e}")
            return
        if "resources" not in path:
            return
        ctype = (flow.response.headers.get("content-type") or "").lower()
        tmpl = json.load(open(ctx.options.resfile))
        base = next(r for r in tmpl if r["name"] == "plexlab")
        uri = ctx.options.turi
        proto, rest = uri.split("://")
        addr, port = rest.rstrip("/").split(":")
        ent = dict(base)
        ent.update({
            "name": ctx.options.tname, "clientIdentifier": ctx.options.tmachine,
            "productVersion": ctx.options.tversion, "accessToken": ctx.options.ttoken,
            "presence": True, "httpsRequired": False, "publicAddressMatches": True,
            "connections": [{"protocol": proto, "address": addr, "port": int(port),
                             "uri": uri, "local": True, "relay": False, "IPv6": False}],
        })
        if "json" in ctype:
            try:
                data = json.loads(flow.response.get_text())
            except Exception:
                return
            if not isinstance(data, list):
                return
            kept = [d for d in data if "server" not in (d.get("provides") or "")]
            flow.response.set_text(json.dumps(kept + [ent]))
            flow.response.headers["X-C70"] = "resources"
        elif "xml" in ctype:
            body = flow.response.get_text()
            body = re.sub(r"<Device\b[^>]*provides=\"[^\"]*server[^\"]*\".*?</Device>", "", body, flags=re.S)
            conn = (f'<Connection protocol="{proto}" address="{addr}" port="{port}" uri="{uri}" '
                    f'local="1" relay="0" IPv6="0"/>')
            dev = (f'<Device name="{ent["name"]}" product="Plex Media Server" productVersion="{ent["productVersion"]}" '
                   f'platform="Linux" clientIdentifier="{ent["clientIdentifier"]}" provides="server" owned="1" '
                   f'accessToken="{ent["accessToken"]}" httpsRequired="0" presence="1" publicAddressMatches="1">{conn}</Device>')
            body = body.replace("</MediaContainer>", dev + "</MediaContainer>")
            flow.response.set_text(body)
            flow.response.headers["X-C70"] = "resources-xml"


addons = [Droid()]
