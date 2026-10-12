"""Keep the harness off production servers, and on exactly one server.

The account that claims the lab server (plexa630) also owns two production servers, which
are live. Blocking their hostnames is not enough: plex.tv still *lists* them, so
the web client selects one as the active server and the session never reaches
the lab at all -- measured, a blocked-hostname-only run drew the production server's
sidebar with unreachable warnings and issued 46 requests to the production server for every
8 to the lab.

So the resource list itself is filtered. The client then sees a single owned
server, which is also the shape a real rex user would have, and every
library request necessarily goes to the server under test.

plex.tv stays reachable throughout -- the account, discovery and registration
APIs are exactly what this harness exists to record.
"""
import json
import re
from mitmproxy import http, ctx

BLOCKED_NOTE = b"blocked by plexlab isolate.py: not the lab server"


class Isolate:
    def load(self, loader):
        loader.add_option("labhash", str, "",
                          "plex.direct certificate hash of the lab server")
        loader.add_option("labmachine", str, "",
                          "machineIdentifier of the lab server; others are filtered out")
        loader.add_option("blockhosts", str, "",
                          "comma-separated extra hostnames to block outright")

    # -- outbound -------------------------------------------------------
    def _blocked(self, host: str) -> bool:
        host = host.lower()
        extra = [h.strip().lower() for h in ctx.options.blockhosts.split(",") if h.strip()]
        if host in extra:
            return True
        if host.endswith(".plex.direct"):
            lab = ctx.options.labhash.lower()
            return not (lab and lab in host)
        return False

    def request(self, flow: http.HTTPFlow):
        if self._blocked(flow.request.pretty_host):
            flow.response = http.Response.make(
                403, BLOCKED_NOTE,
                {"Content-Type": "text/plain", "X-Plexlab-Blocked": "1"})

    # -- resource list ---------------------------------------------------
    def response(self, flow: http.HTTPFlow):
        mach = ctx.options.labmachine
        if not mach:
            return
        if "plex.tv" not in flow.request.pretty_host:
            return
        if "resources" not in flow.request.path:
            return
        ctype = (flow.response.headers.get("content-type") or "").lower()
        try:
            body = flow.response.get_text(strict=False) or ""
        except Exception:
            return

        if "json" in ctype:
            try:
                data = json.loads(body)
            except Exception:
                return
            if not isinstance(data, list):
                return
            kept = [d for d in data
                    if "server" not in (d.get("provides") or "")
                    or d.get("clientIdentifier") == mach]
            flow.response.set_text(json.dumps(kept))
            flow.response.headers["X-Plexlab-Filtered"] = str(len(data) - len(kept))
        elif "xml" in ctype:
            # /api/resources answers XML; drop every <Device> that provides a
            # server and is not the lab machine, children included.
            def drop(m):
                el = m.group(0)
                if 'provides="' in el and "server" in re.search(
                        r'provides="([^"]*)"', el).group(1):
                    if f'clientIdentifier="{mach}"' not in el:
                        return ""
                return el
            new = re.sub(r"<Device\b.*?(?:/>|</Device>)", drop, body, flags=re.S)
            if new != body:
                flow.response.set_text(new)
                flow.response.headers["X-Plexlab-Filtered"] = "xml"


addons = [Isolate()]
