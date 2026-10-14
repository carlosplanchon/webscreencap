#!/usr/bin/env python3
"""Fixture web server for the tests (see compose.test.yaml).

usage: site.py public|internal [port] [control port]

public:   a "public" website with test pages (redirects, a page that loads URLs in
          every way a browser can, a page that never stops making requests...)
internal: stands in for an internal service; the browser must never reach it

Both count every connection and request they get. The test runner reads those
numbers on the control port, which isn't counted.
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MODE = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
CONTROL_PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8081

lock = threading.Lock()
stats = {"connections": 0, "requests": []}

# Every way a page can make the browser load a URL. {t} is the target, {k} a token
# that tells this page's requests apart from other tests'.
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

    def _send(self, code, body, headers=None, encoding="utf-8"):
        data = body.encode(encoding)
        self.send_response(code)
        self.send_header("Content-Type", f"text/html; charset={encoding}")
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
            return self._send(200, f'<div style="height: {height}px; background: linear-gradient(red, blue)"></div>')
        if url.path == "/latin1":
            return self._send(200, '<meta charset="iso-8859-1"><p>Año, señal, café</p>', encoding="iso-8859-1")
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
