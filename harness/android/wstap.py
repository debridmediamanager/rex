"""Log every websocket message to a JSONL file (option wslog)."""
import json, time
from mitmproxy import ctx, http
class WsTap:
    def load(self, loader):
        loader.add_option("wslog", str, "/tmp/ws.jsonl", "websocket log")
    def websocket_message(self, flow: http.HTTPFlow):
        m = flow.websocket.messages[-1]
        with open(ctx.options.wslog, "a") as fh:
            fh.write(json.dumps({"t": time.time(), "host": flow.request.pretty_host, "port": flow.request.port,
                                 "path": flow.request.path.split("?")[0], "from_client": m.from_client,
                                 "text": m.text if m.is_text else "<%d bytes>" % len(m.content)}) + "\n")
addons = [WsTap()]

def _end(self, flow):
    ws = flow.websocket
    with open(ctx.options.wslog, "a") as fh:
        fh.write(json.dumps({"t": time.time(), "end": True, "path": flow.request.path.split("?")[0],
                             "closed_by_client": ws.closed_by_client, "code": ws.close_code,
                             "reason": ws.close_reason, "messages": len(ws.messages),
                             "client_msgs": [m.text if m.is_text else "<%d bytes op>" % len(m.content)
                                             for m in ws.messages if m.from_client][:10]}) + "\n")
WsTap.websocket_end = _end
