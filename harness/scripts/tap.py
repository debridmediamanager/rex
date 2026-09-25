"""Record every request/response pair as one JSON line.

Written for diffing a real PMS against plexsim: the unit of comparison is the
client's *exact* request (method, path, every query param, every header) beside
the server's *exact* answer. docs/findings.md records that replaying a request
without the paging params the client actually sends hides the very field that
was missing, so nothing here is normalised or tidied away.

Media bodies are recorded by length only -- a 1.8 GB part read is a fact about
headers and status, not something to keep on disk.
"""
import json, time, os
from mitmproxy import http, ctx

BODY_LIMIT = 512 * 1024          # keep API payloads whole, drop media
MEDIA_HINTS = ("video/", "audio/", "image/", "application/octet-stream",
               "application/force-download")


class Tap:
    def load(self, loader):
        loader.add_option("taplog", str, "/capture/flows.jsonl",
                          "JSONL file to append one record per flow to")

    def _body(self, msg):
        """Decoded body, or a description of why it was not kept."""
        ctype = msg.headers.get("content-type", "").lower()
        raw = msg.raw_content or b""
        if not raw:
            return None, len(raw)
        if any(h in ctype for h in MEDIA_HINTS) or len(raw) > BODY_LIMIT:
            return f"<{len(raw)} bytes elided: {ctype or 'no content-type'}>", len(raw)
        try:
            return msg.get_text(strict=False), len(raw)
        except Exception:
            return f"<{len(raw)} bytes undecodable>", len(raw)

    def response(self, flow: http.HTTPFlow):
        req, res = flow.request, flow.response
        rq_body, rq_len = self._body(req)
        rs_body, rs_len = self._body(res)
        rec = {
            "t": flow.request.timestamp_start,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S",
                                 time.localtime(flow.request.timestamp_start)),
            "ms": round((res.timestamp_end - req.timestamp_start) * 1000, 1)
                  if res.timestamp_end else None,
            "scheme": req.scheme,
            "host": req.pretty_host,
            "port": req.port,
            "method": req.method,
            "path": req.path.split("?")[0],
            # Query kept as ordered pairs: Plex repeats keys and order has meaning.
            "query": [[k, v] for k, v in req.query.items(multi=True)],
            "req_headers": [[k, v] for k, v in req.headers.items(multi=True)],
            "req_body": rq_body,
            "req_len": rq_len,
            "status": res.status_code,
            "reason": res.reason,
            "res_headers": [[k, v] for k, v in res.headers.items(multi=True)],
            "res_body": rs_body,
            "res_len": rs_len,
        }
        path = ctx.options.taplog
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            # ASCII escapes: a body can carry a lone surrogate (a mis-decoded byte), which
            # a UTF-8 file refuses -- the addon then dropped the flow. Measured with Plex for
            # Android's traffic.
            fh.write(json.dumps(rec, ensure_ascii=True) + "\n")

    def error(self, flow: http.HTTPFlow):
        """A failed flow is evidence too -- a TLS refusal names a pinned host."""
        if flow.response:
            return
        path = ctx.options.taplog
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({
                "t": flow.request.timestamp_start,
                "host": flow.request.pretty_host,
                "method": flow.request.method,
                "path": flow.request.path.split("?")[0],
                "error": str(flow.error),
            }, ensure_ascii=False) + "\n")


addons = [Tap()]
