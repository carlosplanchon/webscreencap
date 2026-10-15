"""Shared helpers for the tests, which run inside the test stack (compose.test.yaml)."""
import json
import os
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_URL = os.environ["API_URL"]
API_KEY = os.environ["API_KEY"]
SITE = os.environ["SITE_URL"]
SITE_CONTROL = os.environ["SITE_CONTROL_URL"]
INTERNAL = os.environ["INTERNAL_URL"]
INTERNAL_CONTROL = os.environ["INTERNAL_CONTROL_URL"]
SCRAPER = "/app/src/scraper.js"


def http(method, url, body=None, headers=None, timeout=30):
    """Returns (status, headers, body bytes). HTTP errors are returned, not raised."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def api(method, path, body=None, key=API_KEY):
    headers = {"X-API-Key": key} if key is not None else {}
    status, _, data = http(method, API_URL + path, body, headers)
    return status, json.loads(data) if data else None


def wait_for_api(timeout=60):
    parts = urllib.parse.urlsplit(API_URL)
    deadline = time.time() + timeout
    while True:
        try:
            http("GET", f"{parts.scheme}://{parts.netloc}/openapi.json")
            return
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(1)


def submit(url):
    status, body = api("POST", "/task/screenshot", {"url": url})
    assert status == 200, (status, body)
    return body["id"]


def wait_for_task(task_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, body = api("GET", f"/task_details/{task_id}")
        if body and body.get("status") in ("SUCCESS", "FAILURE"):
            return body
        time.sleep(1)
    raise AssertionError(f"task {task_id} didn't finish in {timeout} s")


def run_scraper(url, env=None, timeout=200):
    """Runs scraper.js like the worker; returns (exit code, JSON result, stderr, seconds)."""
    start = time.time()
    process = subprocess.run(
        ["node", SCRAPER, url], capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )
    lines = process.stdout.strip().splitlines()
    result = json.loads(lines[-1]) if lines else {}
    return process.returncode, result, process.stderr, time.time() - start


def fixture_stats(control_url):
    """Connections and requests a fixture server (tests/fixtures/site.py) has received."""
    _, _, data = http("GET", control_url)
    return json.loads(data)


def new_token():
    return uuid.uuid4().hex


def png_size(data):
    """(width, height) of a PNG, from its header."""
    return struct.unpack(">II", data[16:24])


def drop_redis_connections():
    """Makes Redis close every client connection, as a restart does."""
    url = urllib.parse.urlsplit(os.environ["REDIS_URL"])
    with socket.create_connection((url.hostname, url.port or 6379), timeout=10) as connection:
        connection.sendall(b"CLIENT KILL TYPE normal\r\n")
        return connection.recv(64)


def processes():
    """(pid, command line) of every process in this container."""
    found = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                found.append((pid, f.read().replace(b"\0", b" ").decode(errors="replace")))
        except OSError:
            pass
    return found


class LoopbackListener:
    """A server on 127.0.0.1 that counts the connections it gets."""

    def __enter__(self):
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                listener.connections += 1

            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.connections = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()


class UdpListener:
    """A UDP socket on every address of this container that counts the datagrams it gets."""

    def __enter__(self):
        self.datagrams = 0
        self.stopped = False
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("0.0.0.0", 0))
        self.socket.settimeout(0.2)
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self._count, daemon=True)
        self.thread.start()
        return self

    def _count(self):
        while not self.stopped:
            try:
                self.socket.recvfrom(4096)
                self.datagrams += 1
            except TimeoutError:
                pass

    def __exit__(self, *exc):
        self.stopped = True
        self.thread.join()
        self.socket.close()
