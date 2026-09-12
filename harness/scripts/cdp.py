"""Minimal CDP driver for the plexlab browser.

A raw websocket rather than Playwright: the browser lives in a container and a
bare websocket-client needs suppress_origin=True, which Playwright hides. Kept
small on purpose -- its only job is to make the official web client perform a
real navigation sequence so the proxy records it.
"""
import json, time, threading, base64
import websocket, requests

CDP_HTTP = "http://127.0.0.1:9224"


class Browser:
    def __init__(self, url=CDP_HTTP):
        self.http = url
        ws_url = requests.get(f"{url}/json/version", timeout=10).json()["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=30)
        self._id = 0
        self._lock = threading.Lock()

    def send(self, method, params=None, session=None):
        with self._lock:
            self._id += 1
            mid = self._id
            msg = {"id": mid, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            self.ws.send(json.dumps(msg))
        deadline = time.time() + 60
        while time.time() < deadline:
            raw = json.loads(self.ws.recv())
            if raw.get("id") == mid:
                if "error" in raw:
                    raise RuntimeError(f"{method}: {raw['error']}")
                return raw.get("result", {})
        raise TimeoutError(method)

    def new_page(self, url="about:blank"):
        tid = self.send("Target.createTarget", {"url": url})["targetId"]
        sid = self.send("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        self.send("Page.enable", session=sid)
        self.send("Runtime.enable", session=sid)
        return tid, sid

    def goto(self, sid, url, settle=6):
        self.send("Page.navigate", {"url": url}, session=sid)
        time.sleep(settle)

    def js(self, sid, expr):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True},
                      session=sid)
        return r.get("result", {}).get("value")

    def shot(self, sid, path):
        data = self.send("Page.captureScreenshot", {"format": "png"}, session=sid)["data"]
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))
        return path

    def close(self, tid):
        try:
            self.send("Target.closeTarget", {"targetId": tid})
        except Exception:
            pass
