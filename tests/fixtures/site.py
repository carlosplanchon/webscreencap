#!/usr/bin/env python3
"""Test website: site.py public|internal [port] [control port]

The control port reports the connections and requests the site got.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MODE = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
CONTROL_PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8081

lock = threading.Lock()
stats = {"connections": 0, "requests": []}

# Every way a page can load a URL; {t} is the target and {k} a per-test token
MECHANISMS = """<!doctype html>
<html><head><title>mechanisms</title>
<link rel="prefetch" href="{t}/m/{k}/prefetch">
<style>body {{ background-image: url("{t}/m/{k}/css"); }}</style>
</head><body>
<h1>mechanisms</h1>
<img src="{t}/m/{k}/img">
<iframe src="{t}/m/{k}/iframe"></iframe>
<script>
fetch("{t}/m/{k}/fetch", {{ mode: "no-cors" }}).catch(() => {{}});
const xhr = new XMLHttpRequest(); xhr.open("GET", "{t}/m/{k}/xhr"); xhr.send();
new Worker(URL.createObjectURL(new Blob(['fetch("{t}/m/{k}/worker", {{ mode: "no-cors" }}).catch(() => {{}})'],
                                        {{ type: "text/javascript" }})));
try {{ new WebSocket("{ws}/m/{k}/ws"); }} catch (e) {{}}
try {{ new EventSource("{t}/m/{k}/eventsource"); }} catch (e) {{}}
navigator.sendBeacon("{t}/m/{k}/beacon", "x");
window.open("{t}/m/{k}/popup");
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        # Count every TCP connection, so tunnels that never send plain HTTP count too
        super().setup()
        with lock:
            stats["connections"] += 1

    def _send(self, code, body, headers=None, encoding="utf-8", content_type="text/html"):
        data = body.encode(encoding)
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset={encoding}")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with lock:
            stats["requests"].append(self.path)
        if MODE == "internal":
            return self._send(200, "<h1>INTERNAL-SECRET</h1>")

        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/landed":
            return self._send(200, "<h1>LANDED</h1>")
        if url.path == "/missing":
            return self._send(404, "<h1>missing</h1>")
        if url.path == "/empty-error":
            return self._send(500, "")
        if url.path == "/whoami":
            # What the browser tells sites about itself
            headers = json.dumps({name: self.headers.get(name) for name in ("User-Agent", "Accept-Language")})
            return self._send(200, f'<pre id="headers">{headers}</pre><pre id="browser"></pre><script>'
                                   'document.getElementById("browser").textContent = JSON.stringify({'
                                   ' webdriver: navigator.webdriver, userAgent: navigator.userAgent,'
                                   ' innerWidth, innerHeight, outerWidth, outerHeight });</script>')
        if url.path == "/redirect":
            return self._send(302, "", {"Location": query["to"][0]})
        if url.path == "/mechanisms":
            target = query["target"][0]
            return self._send(200, MECHANISMS.format(
                t=target, ws=target.replace("http", "ws", 1), k=query["token"][0]))
        if url.path == "/polling":
            # Never goes network-idle: a request every 300 ms, forever
            return self._send(200, "<h1>polling</h1><script>"
                                   "setInterval(() => fetch('/ping?' + Date.now()), 300);</script>")
        if url.path == "/tall":
            height = int(query["height"][0])
            page = f'<div style="height: {height}px; background: linear-gradient(red, blue)"></div>'
            if "styled" in query:
                page = f'<html style="background-color: white;"><body style="margin: 0px;">{page}</body></html>'
            return self._send(200, page)
        if url.path == "/lazy":
            # Loads part of its content once that part scrolls into view, far below the screenshot's height
            return self._send(200, '<div style="height: 15000px">top</div><div id="lazy" style="height: 1000px"></div>'
                                   '<div style="height: 4000px"></div><script>'
                                   'new IntersectionObserver((entries, observer) => {'
                                   ' if (!entries[0].isIntersecting) return;'
                                   ' observer.disconnect();'
                                   ' fetch("/lazy-content").then((response) => response.text())'
                                   '   .then((text) => document.getElementById("lazy").textContent = text);'
                                   '}).observe(document.getElementById("lazy"));</script>')
        if url.path == "/lazy-content":
            time.sleep(1.5)
            return self._send(200, "LAZY-CONTENT", content_type="text/plain")
        if url.path == "/ad":
            # An ad slot, which the ad blocker's styles hide, taller than the screenshot
            return self._send(200, "<style>p { color: black; }</style><p>content</p>"
                                   '<ad-slot style="display: block; height: 20000px; background: red">AD</ad-slot>')
        if url.path == "/latin1":
            return self._send(200, '<meta charset="iso-8859-1"><p>Año, señal, café</p>', encoding="iso-8859-1")
        if url.path == "/never":
            time.sleep(600)
            return self._send(200, "")
        if url.path == "/hanging-image":
            return self._send(200, '<h1>visible content</h1><img src="/never">')
        if url.path == "/hanging-script":
            return self._send(200, '<h1>visible content</h1><script src="/never"></script>')
        if url.path == "/image.svg":
            return self._send(200, '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
                                   '<rect width="200" height="100" fill="red"/></svg>', content_type="image/svg+xml")
        if url.path == "/no-content":
            return self._send(204, "")
        if url.path == "/download":
            return self._send(200, "a file", {"Content-Disposition": "attachment; filename=file.txt"})
        if url.path == "/reloading":
            return self._send(200, '<meta http-equiv="refresh" content="1"><p>reloading</p>')
        if url.path == "/js-redirect":
            to = query.get("to", ["/landed"])[0]
            height = int(query.get("height", ["0"])[0])
            return self._send(200, f'<div style="height: {height}px">before</div>'
                                   f'<script>setTimeout(() => location.href = "{to}", {int(query["ms"][0])});</script>')
        if url.path == "/meta-refresh":
            return self._send(200, f'<meta http-equiv="refresh" content="{int(query["s"][0])};url=/landed"><p>before</p>')
        if url.path == "/webrtc":
            servers = json.dumps([{"urls": stun} for stun in query["stun"]])
            return self._send(200, "<p>webrtc</p><script>"
                                   f"const pc = new RTCPeerConnection({{iceServers: {servers}}});"
                                   "pc.createDataChannel('x');"
                                   "pc.createOffer().then((offer) => pc.setLocalDescription(offer));</script>")
        return self._send(200, "<h1>public ok</h1>")

    do_POST = do_GET

    def log_message(self, *args):
        pass


class ControlHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with lock:
            data = json.dumps(stats).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


threading.Thread(
    target=ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler).serve_forever,
    daemon=True,
).start()
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
