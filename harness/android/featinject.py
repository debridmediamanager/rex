"""Grant the account the client-side WebsocketsForPms feature, locally, in the proxy.

Plex for Android 2026.19.1 opens a PMS's /:/websockets/notifications only when
`useUserHasFeature(Features.WebsocketsForPms)` holds (js:333116, uuid
0668d10e-74a3-462b-b31d-2cd11b2ff420), read off the `/api/v2/features` list keyed by uuid; the
test account lacks it, so the dashboard never consumes server events. Nothing is written to
plex.tv: the proxy edits the answer the app receives (and drops the conditional header so a
cached 304 cannot keep the old list).
"""
import json
from mitmproxy import http
FEATURE = {"id": "pms-websockets", "uuid": "0668d10e-74a3-462b-b31d-2cd11b2ff420"}
def target(flow):
    return flow.request.pretty_host.endswith("plex.tv") and flow.request.path.split("?")[0] == "/api/v2/features"
class FeatInject:
    def request(self, flow: http.HTTPFlow):
        if target(flow):
            flow.request.headers.pop("If-None-Match", None)
            flow.request.headers.pop("If-Modified-Since", None)
    def response(self, flow: http.HTTPFlow):
        if not target(flow):
            return
        try:
            d = json.loads(flow.response.get_text())
        except Exception:
            return
        if isinstance(d, list) and not any(f.get("uuid") == FEATURE["uuid"] for f in d if isinstance(f, dict)):
            d.append(FEATURE)
            flow.response.set_text(json.dumps(d))
            flow.response.headers["X-C94-Feature"] = "WebsocketsForPms"
addons = [FeatInject()]
