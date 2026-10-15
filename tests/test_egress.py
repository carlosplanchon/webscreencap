"""The worker's egress proxy: what a page loads can only reach public addresses."""
import socket
import subprocess
import time
import unittest

from helpers import (INTERNAL, INTERNAL_CONTROL, SITE, SITE_CONTROL, LoopbackListener, UdpListener, fixture_stats,
                     new_token, run_scraper)

# What the /mechanisms page loads, except the popup: pages can't open them
MECHANISMS = {"prefetch", "css", "img", "iframe", "fetch", "xhr", "worker", "ws", "eventsource", "beacon"}


class EgressTest(unittest.TestCase):
    def test_address_policy(self):
        process = subprocess.run(
            ["node", "--test", "/app/tests/egress_proxy.test.js"], capture_output=True, text=True,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def test_non_public_targets_are_rejected_before_loading(self):
        internal_ip = socket.gethostbyname("internal")
        for url, message in (
            ("file:///etc/passwd", "Only http and https URLs are allowed"),
            (INTERNAL + "/", f"resolves to {internal_ip}, which is not a public address"),
            (f"http://{internal_ip}:8080/", "is not a public address"),
            ("http://127.0.0.1/", "is not a public address"),
            ("http://localhost/", "is not a public address"),
            ("http://169.254.169.254/latest/meta-data/", "is not a public address"),
        ):
            with self.subTest(url=url):
                code, result, _, seconds = run_scraper(url)
                self.assertEqual(code, 1, result)
                self.assertIn(message, result["error"])
                self.assertLess(seconds, 20)

    def test_redirects_to_internal_addresses_are_blocked(self):
        before = fixture_stats(INTERNAL_CONTROL)["connections"]
        code, result, _, _ = run_scraper(f"{SITE}/redirect?to={INTERNAL}/secret")
        self.assertEqual(code, 1, result)
        self.assertIn(f"is not a public address at {INTERNAL}/secret", result["error"])
        self.assertEqual(fixture_stats(INTERNAL_CONTROL)["connections"], before)

    def test_pages_cannot_reach_internal_addresses(self):
        internal_ip = socket.gethostbyname("internal")
        before = fixture_stats(INTERNAL_CONTROL)["connections"]
        with LoopbackListener() as loopback:
            for target in (
                INTERNAL,
                f"http://{internal_ip}:8080",
                f"https://{internal_ip}:8080",
                f"http://127.0.0.1:{loopback.port}",
                f"http://localhost:{loopback.port}",
            ):
                with self.subTest(target=target):
                    code, result, _, _ = run_scraper(f"{SITE}/mechanisms?target={target}&token={new_token()}")
                    # The page still loads
                    self.assertEqual(code, 0, result)
            self.assertEqual(loopback.connections, 0)
        self.assertEqual(fixture_stats(INTERNAL_CONTROL)["connections"], before)

    def test_pages_can_reach_public_addresses(self):
        token = new_token()
        code, result, _, _ = run_scraper(f"{SITE}/mechanisms?target={SITE}&token={token}")
        self.assertEqual(code, 0, result)
        reached = {
            path.rsplit("/", 1)[1]
            for path in fixture_stats(SITE_CONTROL)["requests"]
            if path.startswith(f"/m/{token}/")
        }
        self.assertEqual(reached, MECHANISMS)

    def test_webrtc_cannot_send_udp(self):
        # UDP can't go through the proxy, so WebRTC must not send it at all
        own_ip = socket.gethostbyname(socket.gethostname())
        with UdpListener() as udp:
            stun = "&".join(f"stun=stun:{host}:{udp.port}" for host in ("127.0.0.1", own_ip))
            code, result, _, _ = run_scraper(f"{SITE}/webrtc?{stun}")
            self.assertEqual(code, 0, result)
            time.sleep(0.5)
        self.assertEqual(udp.datagrams, 0)


if __name__ == "__main__":
    unittest.main()
