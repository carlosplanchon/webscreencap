#!/usr/bin/env python3
"""Test website: site.py public|internal [port] [control port]

The control port reports the connections and requests the site got.
"""
import json
import os
import struct
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MODE = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
CONTROL_PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8081

lock = threading.Lock()
stats = {"connections": 0, "requests": []}
# Tokens of the URLs that answer something else the second time, already served
served = set()

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


def pdf(text):
    """A one-page PDF that shows the text."""
    stream = f"BT /F1 24 Tf 20 60 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data, offsets = b"%PDF-1.4\n", []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += b"%d 0 obj\n%s\nendobj\n" % (number, obj)
    xref = len(data)
    data += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    data += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return data + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


def png(width, height, rgb=None):
    """A PNG of a single color, or of random pixels (stored as they are, since they don't compress)."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + (bytes(rgb) * width if rgb else os.urandom(3 * width)) for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress(rows, 6 if rgb else 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


FILES = {
    "/file.pdf": (pdf("PDF CONTENT"), "application/pdf"),
    "/image.png": (png(40, 20, (255, 0, 0)), "image/png"),
    # Larger than what Chrome keeps of a response
    "/big.png": (png(4096, 3400), "image/png"),
    "/data.json": (b'{"captured": "as sent"}', "application/json"),
    "/text.txt": (b"plain text, as sent", "text/plain; charset=utf-8"),
    "/video.mp4": (b"\x00\x00\x00\x18ftypmp42" + bytes(1000), "video/mp4"),
    # A type Chrome can't show, so it downloads it
    "/archive.zip": (b"PK\x05\x06" + bytes(18), "application/zip"),
}


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

    def _send_file(self, data, content_type, headers=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
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
        if url.path in FILES:
            # ?csp=1 forbids the page to fetch anything, its own file included, and ?sandbox=1 also to download it
            csp = "default-src 'none'; sandbox" if "sandbox" in query else "default-src 'none'" if "csp" in query else None
            return self._send_file(*FILES[url.path], {"Content-Security-Policy": csp} if csp else None)
        if url.path == "/once.pdf":
            # The PDF the first time, and a redirect afterwards
            with lock:
                again = query["token"][0] in served
                served.add(query["token"][0])
            if again:
                return self._send(302, "", {"Location": query["then"][0]})
            return self._send_file(*FILES["/file.pdf"])
        if url.path == "/big":
            # Larger than the 50 MB cap: a download, since browsers can't show it
            size = 51 * 1024 * 1024
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            try:
                for _ in range(size // 65536):
                    self.wfile.write(bytes(65536))
            except OSError:
                pass  # the browser hung up
            return
        if url.path == "/endless":
            # A download that doesn't say its size and never ends, at about 10 MB/s
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(bytes(65536))
                    time.sleep(0.006)
            except OSError:
                return  # the browser hung up
        if url.path == "/live":
            # A text stream that never ends, like a live feed
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                self.wfile.write(b"live\n" * 500)
                while True:
                    time.sleep(1)
                    self.wfile.write(b"live\n")
            except OSError:
                return  # the browser hung up
        if url.path == "/reloads-into-download":
            # A page that reloads itself, when its URL is a download
            with lock:
                again = query["token"][0] in served
                served.add(query["token"][0])
            if again:
                return self._send(200, "a file", {"Content-Disposition": "attachment; filename=file.txt"})
            return self._send(200, "<h1>reloading</h1><script>setTimeout(() => location.reload(), 300);</script>")
        if url.path == "/slow-html":
            # HTML whose end arrives 20 s after its start
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h1>START</h1>")
            self.wfile.flush()
            time.sleep(20)
            self.wfile.write(b"<h1>END</h1>")
            return
        if url.path == "/print":
            # Prints itself as it loads, as invoices and tickets do
            return self._send(200, '<body onload="window.print()"><h1>printed</h1></body>')
        if url.path == "/popup":
            # Opens a window once the capture is under way, and keeps the capture going with its requests
            return self._send(200, "<h1>popup opener</h1><script>"
                                   "setInterval(() => fetch('/ping?' + Date.now()), 300);"
                                   f"setTimeout(() => window.open('/popup-page?token={query['token'][0]}'), 1500);"
                                   "</script>")
        if url.path == "/popup-page":
            # Tells the site if it's still open 2 s after it loads
            return self._send(200, "<h1>popup</h1><script>"
                                   f"setTimeout(() => fetch('/popup-alive?token={query['token'][0]}'), 2000);</script>")
        if url.path == "/landed":
            return self._send(200, "<h1>LANDED</h1>")
        if url.path == "/poses-as-proxy":
            # A page that claims to be a block by the egress proxy
            return self._send(200, "<h1>not blocked</h1>", {"X-Egress-Proxy-Error": query["value"][0]})
        if url.path == "/missing":
            return self._send(404, "<h1>missing</h1>")
        if url.path == "/empty-error":
            return self._send(500, "")
        if url.path == "/whoami":
            # What the browser tells sites about itself
            headers = json.dumps({name: self.headers.get(name) for name in ("User-Agent", "Accept-Language")})
            return self._send(200, f'<pre id="headers">{headers}</pre><pre id="browser"></pre><script>'
                                   'document.getElementById("browser").textContent = JSON.stringify({'
                                   ' webdriver: navigator.webdriver, userAgent: navigator.userAgent });</script>')
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
            # With declarations that Chrome doesn't know, as stylesheets for other browsers have
            if "styled" in query:
                page = (f'<html style="background-color: white; -moz-osx-font-smoothing: grayscale;">'
                        f'<body style="margin: 0px; *zoom: 1;">{page}</body></html>')
            return self._send(200, page)
        if url.path == "/lazy":
            # Loads part of its content once that part scrolls into view, far below the screenshot's height;
            # ?smooth=1 asks for smooth scrolling, as Bootstrap does
            smooth = "<style>:root { scroll-behavior: smooth; }</style>" if "smooth" in query else ""
            return self._send(200, smooth + '<div style="height: 15000px">top</div><div id="lazy" style="height: 1000px"></div>'
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
            page = "<style>p { color: black; }</style><p>content</p>"
            if "download" in query:
                # Below a few screens of content, and a download that starts once the page is scrolled
                page += ('<div style="height: 3000px"></div><script>addEventListener("scroll",'
                         ' () => setTimeout(() => location.href = "/download", 300), { once: true });</script>')
            if "history" in query:
                # A URL that keeps changing, as single-page apps do, for which the ad blocker adds its styles again
                page += '<script>setInterval(() => history.replaceState(null, "", "?history=" + Date.now()), 60);</script>'
            return self._send(200, page + '<ad-slot style="display: block; height: 20000px; background: red">AD</ad-slot>')
        if url.path == "/shadow":
            # Open, nested, closed and declarative shadow roots, whose texts the script doesn't spell out
            return self._send(200, """<!doctype html>
<html lang="en"><body><h1>shadow</h1>
<div id="open"></div><div id="closed"></div>
<div id="declarative"><template shadowrootmode="open"><p>declarative shadow content</p></template></div>
<script>
const text = (kind) => kind + " shadow content";
const open = document.getElementById("open").attachShadow({ mode: "open" });
open.innerHTML = `<p>${text("open")}</p><span id="inner"></span>`;
open.getElementById("inner").attachShadow({ mode: "open" }).innerHTML = `<b>${text("nested")}</b>`;
document.getElementById("closed").attachShadow({ mode: "closed" }).innerHTML = `<p>${text("closed")}</p>`;
</script></body></html>""")
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
            # To each `to`, after its `ms`
            height = int(query.get("height", ["0"])[0])
            redirects = "".join(f'setTimeout(() => location.href = "{to}", {int(ms)});'
                                for ms, to in zip(query["ms"], query.get("to", ["/landed"])))
            return self._send(200, f'<div style="height: {height}px">before</div><script>{redirects}</script>')
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
