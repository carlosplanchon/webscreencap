"""Captures: page loading, size cap, fonts, encoding, the file URL settings and ad blocking."""
import subprocess
import unittest

from helpers import INTERNAL, SITE, SITE_CONTROL, fixture_stats, http, new_token, png_size, run_scraper


class CaptureTest(unittest.TestCase):
    def test_page_with_constant_traffic_loads(self):
        # It never goes network-idle, which used to make it fail after 60 s
        code, result, _, seconds = run_scraper(SITE + "/polling")
        self.assertEqual(code, 0, result)
        self.assertLess(seconds, 45)

    def test_page_with_a_resource_that_never_loads(self):
        code, result, _, seconds = run_scraper(SITE + "/hanging-image")
        self.assertEqual(code, 0, result)
        self.assertLess(seconds, 40)
        _, _, html = http("GET", result["htmlUrl"])
        self.assertIn(b"visible content", html)

    def test_pages_that_redirect_by_themselves(self):
        for path in ("/js-redirect?ms=1500", "/meta-refresh?s=1", "/js-redirect?ms=8000&height=20000"):
            with self.subTest(path=path):
                code, result, _, _ = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                _, _, html = http("GET", result["htmlUrl"])
                self.assertIn(b"LANDED", html)

    def test_redirect_to_a_page_that_cannot_load_fails(self):
        target = INTERNAL.replace("http:", "https:", 1) + "/"
        code, result, _, _ = run_scraper(f"{SITE}/js-redirect?ms=300&to={target}")
        self.assertEqual(code, 1, result)
        self.assertIn(f"ERR_TUNNEL_CONNECTION_FAILED at {target}", result["error"])

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
