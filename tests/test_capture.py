"""Captures: page loading, size cap, fonts, encoding, the file URL settings, ad blocking and the sandbox."""
import os
import subprocess
import time
import unittest

from helpers import (INTERNAL, SCRAPER, SITE, SITE_CONTROL, fixture_stats, http, new_token, png_size, processes,
                     run_scraper)


class CaptureTest(unittest.TestCase):
    def test_page_with_constant_traffic_loads(self):
        # It never goes network-idle
        code, result, _, seconds = run_scraper(SITE + "/polling")
        self.assertEqual(code, 0, result)
        self.assertLess(seconds, 45)

    def test_page_with_a_resource_that_never_loads(self):
        # An image doesn't hold up the HTML parser; a script does
        for path in ("/hanging-image", "/hanging-script"):
            with self.subTest(path=path):
                code, result, _, seconds = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                self.assertLess(seconds, 40)
                _, _, html = http("GET", result["htmlUrl"])
                self.assertIn(b"visible content", html)

    def test_svg_document(self):
        code, result, _, seconds = run_scraper(SITE + "/image.svg")
        self.assertEqual(code, 0, result)
        self.assertLess(seconds, 30)

    def test_pages_that_redirect_by_themselves(self):
        for path in ("/js-redirect?ms=1500", "/meta-refresh?s=1", "/js-redirect?ms=8000&height=20000"):
            with self.subTest(path=path):
                code, result, _, _ = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                _, _, html = http("GET", result["htmlUrl"])
                self.assertIn(b"LANDED", html)

    def test_navigation_that_sends_no_request(self):
        code, result, _, _ = run_scraper(f"{SITE}/js-redirect?ms=3000&height=20000&to=about:blank")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["finalUrl"], "about:blank")

    def test_navigations_that_load_no_document(self):
        # The page stays as it was, with its own status
        for to in ("/no-content", "/download"):
            with self.subTest(to=to):
                path = f"/js-redirect?ms=500&to={to}"
                code, result, _, seconds = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                self.assertLess(seconds, 30)
                self.assertEqual((result["httpStatus"], result["finalUrl"]), (200, SITE + path))

    def test_page_that_keeps_reloading(self):
        code, result, _, seconds = run_scraper(SITE + "/reloading")
        self.assertEqual(code, 0, result)
        self.assertLess(seconds, 60)

    def test_redirect_to_a_page_that_cannot_load_fails(self):
        https_target = INTERNAL.replace("http:", "https:", 1) + "/"
        for path, message in (
            (f"/js-redirect?ms=300&to={https_target}", f"ERR_TUNNEL_CONNECTION_FAILED at {https_target}"),
            ("/redirect?to=http://site:8099/", "ECONNREFUSED"),
        ):
            with self.subTest(path=path):
                code, result, _, _ = run_scraper(SITE + path)
                self.assertEqual(code, 1, result)
                self.assertIn(message, result["error"])

    def test_http_status_and_final_url(self):
        for path, status, final_path in (
            ("/missing", 404, "/missing"),
            ("/redirect?to=/landed", 200, "/landed"),
            ("/js-redirect?ms=300", 200, "/landed"),
        ):
            with self.subTest(path=path):
                code, result, _, _ = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                self.assertEqual((result["httpStatus"], result["finalUrl"]), (status, SITE + final_path))

    def test_chromium_runs_with_its_sandbox(self):
        self.assertNotEqual(os.getuid(), 0)
        scraper = subprocess.Popen(["node", SCRAPER, SITE + "/hanging-image"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 20
            while not any("--type=renderer" in args for _, args in processes()):
                self.assertLess(time.time(), deadline, "no renderer started")
                time.sleep(0.2)
            running = processes()
            browser = next(pid for pid, args in running if "--remote-debugging-pipe" in args and "--type=" not in args)
            renderer = next(pid for pid, args in running if "--type=renderer" in args)
            self.assertFalse(any("--no-sandbox" in args for _, args in running))
            # The renderer runs in a user namespace of its own
            self.assertNotEqual(os.readlink(f"/proc/{browser}/ns/user"), os.readlink(f"/proc/{renderer}/ns/user"))
        finally:
            scraper.kill()
            scraper.wait()

    def test_screenshots_are_cut_at_8192_px(self):
        for height, expected in ((20000, 8192), (500, 1080)):
            with self.subTest(height=height):
                code, result, _, _ = run_scraper(f"{SITE}/tall?height={height}")
                self.assertEqual(code, 0, result)
                _, _, png = http("GET", result["screenshotUrl"])
                self.assertEqual(png_size(png), (1920, expected))

    def test_scrolling_stops_at_the_screenshot_height(self):
        code, result, _, seconds = run_scraper(f"{SITE}/tall?height=30000")
        self.assertEqual(code, 0, result)
        self.assertLess(seconds, 40)

    def test_html_is_saved_as_utf8(self):
        code, result, _, _ = run_scraper(SITE + "/latin1")
        self.assertEqual(code, 0, result)
        _, headers, html = http("GET", result["htmlUrl"])
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("Año, señal, café".encode(), html)
        # The page's own <meta charset> would contradict the file
        self.assertIn(b'<meta charset="utf-8">', html)
        self.assertNotIn(b"iso-8859-1", html.lower())

    def test_failed_adblock_list_downloads_fail(self):
        process = subprocess.run(
            ["node", "--test", "/app/tests/adblocker.test.js"], capture_output=True, text=True,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def test_fonts_for_non_latin_scripts(self):
        for lang in ("ja", "zh-cn", "ko", "ar", "he", "hi", "th"):
            with self.subTest(lang=lang):
                fonts = subprocess.run(["fc-list", f":lang={lang}", "family"],
                                       capture_output=True, text=True).stdout
                self.assertTrue(fonts.strip(), f"no font for {lang}")
        families = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
        self.assertIn("Noto Color Emoji", families)

    def test_presigned_urls_by_default(self):
        code, result, _, _ = run_scraper(SITE + "/landed")
        self.assertEqual(code, 0, result)
        for url in (result["screenshotUrl"], result["htmlUrl"]):
            self.assertIn("X-Amz-Signature=", url)
            self.assertIn("X-Amz-Expires=86400", url)
            self.assertEqual(http("GET", url)[0], 200)

    def test_presigned_url_lifetime_is_configurable(self):
        code, result, _, _ = run_scraper(SITE + "/landed", {"PRESIGNED_URL_EXPIRES_IN": "600"})
        self.assertEqual(code, 0, result)
        self.assertIn("X-Amz-Expires=600", result["screenshotUrl"])

    def test_public_urls(self):
        env = {"FILE_URL_MODE": "public", "PUBLIC_FILE_BASE_URL": "https://files.example.com/bucket/"}
        code, result, _, _ = run_scraper(SITE + "/landed", env)
        self.assertEqual(code, 0, result)
        self.assertRegex(result["screenshotUrl"],
                         r"^https://files\.example\.com/bucket/screenshot-\d+-[0-9a-f]{64}\.png$")
        self.assertRegex(result["htmlUrl"],
                         r"^https://files\.example\.com/bucket/page-\d+-[0-9a-f]{64}\.html$")

    def test_bad_url_settings_fail_before_capturing(self):
        for env, message in (
            ({"PRESIGNED_URL_EXPIRES_IN": "abc"}, "PRESIGNED_URL_EXPIRES_IN must be a whole number"),
            ({"PRESIGNED_URL_EXPIRES_IN": "604801"}, "up to 604800 (7 days)"),
            ({"FILE_URL_MODE": "foo"}, 'FILE_URL_MODE must be "presigned" or "public"'),
            ({"FILE_URL_MODE": "public"}, "FILE_URL_MODE=public needs PUBLIC_FILE_BASE_URL"),
        ):
            with self.subTest(env=env):
                token = new_token()
                code, result, _, _ = run_scraper(f"{SITE}/landed?{token}", env)
                self.assertEqual(code, 1, result)
                self.assertIn(message, result["error"])
                # The page was never even loaded
                requests = fixture_stats(SITE_CONTROL)["requests"]
                self.assertFalse(any(token in path for path in requests))


if __name__ == "__main__":
    unittest.main()
