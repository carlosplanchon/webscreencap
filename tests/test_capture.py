"""Captures: the browser, page loading, size cap, fonts, encoding, the file URL settings, ad blocking and the sandbox."""
import glob
import json
import os
import re
import subprocess
import time
import unittest

from helpers import (INTERNAL, SCRAPER, SITE, SITE_CONTROL, fixture_stats, http, new_token, png_size, processes,
                     run_scraper)


class CaptureTest(unittest.TestCase):
    def test_browser_looks_like_a_regular_chrome(self):
        code, result, _, _ = run_scraper(SITE + "/whoami")
        self.assertEqual(code, 0, result)
        _, _, html = http("GET", result["htmlUrl"])
        headers, browser = (json.loads(re.search(rb'<pre id="%s">(.*?)</pre>' % name, html).group(1))
                            for name in (b"headers", b"browser"))
        for user_agent in (headers["User-Agent"], browser["userAgent"]):
            self.assertIn("Chrome/", user_agent)
            self.assertNotIn("Headless", user_agent)
        self.assertTrue(headers["Accept-Language"].startswith("en-US"))
        self.assertIs(browser["webdriver"], False)

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

    def test_html_that_takes_a_while_to_arrive(self):
        code, result, _, _ = run_scraper(SITE + "/slow-html")
        self.assertEqual(code, 0, result)
        _, _, html = http("GET", result["htmlUrl"])
        self.assertIn(b"END", html)

    def test_page_that_prints_itself(self):
        # Chrome's print dialog would hold it until the task times out
        code, result, _, seconds = run_scraper(SITE + "/print")
        self.assertEqual(code, 0, result)
        self.assertLess(seconds, 30)

    def test_popups_are_closed_right_away(self):
        token = new_token()
        code, result, _, _ = run_scraper(f"{SITE}/popup?token={token}")
        self.assertEqual(code, 0, result)
        requests = fixture_stats(SITE_CONTROL)["requests"]
        self.assertIn(f"/popup-page?token={token}", requests)
        self.assertNotIn(f"/popup-alive?token={token}", requests)

    def test_svg_document(self):
        # An image, saved as a file
        code, result, _, seconds = run_scraper(SITE + "/image.svg")
        self.assertEqual(code, 0, result)
        self.assertLess(seconds, 30)
        self.assertEqual(http("GET", result["fileUrl"])[2], http("GET", SITE + "/image.svg")[2])
        self.assertIsNone(result["htmlUrl"])

    def test_files_are_saved_as_the_site_sent_them(self):
        for path, content_type in (
            ("/image.png", "image/png"),
            ("/data.json", "application/json"),
            ("/text.txt", "text/plain; charset=utf-8"),
            # Chrome keeps the text it read, which isn't the bytes the site sent, so it downloads them again
            ("/utf8.txt", "text/plain"),
            ("/latin1.txt", "text/plain; charset=iso-8859-1"),
            ("/latin1.xml", "application/xml"),
            ("/bom.json", "application/json"),
            # Chrome keeps no copy of these, so it downloads them again, which the page's CSP doesn't forbid
            ("/file.pdf", "application/pdf"),
            ("/file.pdf?csp=1", "application/pdf"),
            ("/video.mp4?csp=1", "video/mp4"),
            ("/big.png?csp=1", "image/png"),
        ):
            with self.subTest(path=path):
                code, result, _, _ = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                self.assertEqual(result["contentType"], content_type)
                self.assertIsNone(result["htmlUrl"])
                _, headers, data = http("GET", result["fileUrl"])
                self.assertEqual(data, http("GET", SITE + path)[2])
                self.assertEqual(headers["Content-Type"], content_type)
                # What Chrome shows
                self.assertEqual(http("GET", result["screenshotUrl"])[2][:8], b"\x89PNG\r\n\x1a\n")

    def test_files_without_a_content_type(self):
        # Saved with the type Chrome makes out from their content
        for path, content_type in (("/untyped/image.png", "image/png"), ("/untyped/file.pdf", "application/pdf")):
            with self.subTest(path=path):
                code, result, _, _ = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                self.assertEqual((result["contentType"], result["htmlUrl"]), (None, None))
                _, headers, data = http("GET", result["fileUrl"])
                self.assertEqual(data, http("GET", SITE + path)[2])
                self.assertEqual(headers["Content-Type"], content_type)

    def test_page_with_two_content_types(self):
        # Chrome goes by the last one
        code, result, _, _ = run_scraper(SITE + "/two-content-types")
        self.assertEqual(code, 0, result)
        self.assertEqual((result["contentType"], result["fileUrl"]), ("text/html; charset=utf-8", None))
        self.assertIn(b"two content types", http("GET", result["htmlUrl"])[2])

    def test_file_that_changes_when_it_is_fetched_again_fails(self):
        # The second time it redirects (with no size to tell it apart), answers a page instead, or sends other text
        for path in ("/once.pdf?then=/landed&unsized=1&token=", "/once.pdf?token=", "/once.txt?token="):
            with self.subTest(path=path):
                code, result, _, _ = run_scraper(SITE + path + new_token())
                self.assertEqual(code, 1, result)
                self.assertIn("The file changed when it was fetched again", result["error"])

    def test_page_that_sends_itself_to_a_file(self):
        code, result, _, _ = run_scraper(f"{SITE}/js-redirect?ms=500&to=/data.json")
        self.assertEqual(code, 0, result)
        self.assertEqual((result["finalUrl"], result["contentType"]), (SITE + "/data.json", "application/json"))
        self.assertEqual(http("GET", result["fileUrl"])[2], b'{"captured": "as sent"}')

    def test_page_that_sends_itself_to_a_pdf_while_still_navigating(self):
        # What the ad blocker did in the page could freeze the tab as it turned into the PDF
        for ms in (205, 210, 215):
            with self.subTest(ms=ms):
                code, result, _, _ = run_scraper(f"{SITE}/js-redirect?ms=200&to=/never&ms={ms}&to=/slow.pdf")
                self.assertEqual(code, 0, result)
                self.assertEqual(http("GET", result["fileUrl"])[2], http("GET", SITE + "/slow.pdf")[2])

    def test_sandboxed_file_that_chrome_keeps_no_copy_of_fails(self):
        # A sandboxed page can't download it again
        code, result, _, seconds = run_scraper(SITE + "/video.mp4?sandbox=1")
        self.assertEqual(code, 1, result)
        self.assertIn("The file couldn't be fetched again", result["error"])
        self.assertLess(seconds, 40)

    def test_file_that_never_finishes_loading(self):
        # A live stream: only its screenshot is saved
        code, result, _, seconds = run_scraper(SITE + "/live")
        self.assertEqual(code, 0, result)
        self.assertEqual((result["htmlUrl"], result["fileUrl"], result["contentType"]), (None, None, "text/plain"))
        self.assertEqual(http("GET", result["screenshotUrl"])[2][:8], b"\x89PNG\r\n\x1a\n")
        self.assertLess(seconds, 120)

    def test_downloads_are_saved_without_a_screenshot(self):
        # Chrome saves them instead of showing anything: the site asks for it, or Chrome can't show the type
        for path, extension, disposition in (
            ("/download", ".txt", "attachment; filename=file.txt"),
            ("/archive.zip", ".zip", None),
        ):
            with self.subTest(path=path):
                code, result, _, _ = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                self.assertEqual((result["screenshotUrl"], result["htmlUrl"], result["httpStatus"]), (None, None, 200))
                self.assertTrue(result["fileUrl"].split("?")[0].endswith(extension), result["fileUrl"])
                _, headers, data = http("GET", result["fileUrl"])
                self.assertEqual((data, headers["Content-Disposition"]), (http("GET", SITE + path)[2], disposition))

    def test_download_of_an_http_error_has_no_content(self):
        # Chrome doesn't save it
        code, result, _, seconds = run_scraper(SITE + "/gone-download")
        self.assertEqual(code, 0, result)
        self.assertEqual((result["screenshotUrl"], result["htmlUrl"], result["fileUrl"]), (None, None, None))
        self.assertEqual(result["httpStatus"], 404)
        self.assertLess(seconds, 30)

    def test_download_that_stalls_fails(self):
        code, result, _, seconds = run_scraper(SITE + "/stalled-download")
        self.assertEqual(code, 1, result)
        self.assertIn("The download didn't finish in 60 s", result["error"])
        self.assertLess(seconds, 90)

    def test_downloads_that_the_page_starts_are_left_out(self):
        # Of a file larger than the cap, before the page sends itself to a PDF that Chrome downloads again
        code, result, _, _ = run_scraper(f"{SITE}/starts-download?file=/big&to=/slow.pdf")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["contentType"], "application/pdf")
        self.assertEqual(http("GET", result["fileUrl"])[2], http("GET", SITE + "/slow.pdf")[2])

    def test_url_without_content(self):
        code, result, _, _ = run_scraper(SITE + "/no-content")
        self.assertEqual(code, 0, result)
        self.assertEqual((result["screenshotUrl"], result["htmlUrl"], result["fileUrl"]), (None, None, None))
        self.assertEqual(result["httpStatus"], 204)

    def test_files_over_50_mb_fail(self):
        # A download that doesn't say its size is stopped once it passes 50 MB
        for path in ("/big", "/endless"):
            with self.subTest(path=path):
                before = set(glob.glob("/tmp/webscreencap-*"))
                code, result, _, seconds = run_scraper(SITE + path)
                self.assertEqual(code, 1, result)
                self.assertIn("The file is larger than 50 MB", result["error"])
                self.assertLess(seconds, 60)
                # Nothing is left of it, nor of Chrome's profile
                self.assertEqual(set(glob.glob("/tmp/webscreencap-*")) - before, set())

    def test_pages_that_redirect_by_themselves(self):
        for path in ("/js-redirect?ms=1500", "/meta-refresh?s=1", "/js-redirect?ms=2000&height=20000"):
            with self.subTest(path=path):
                code, result, _, _ = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                _, _, html = http("GET", result["htmlUrl"])
                self.assertIn(b"LANDED", html)

    def test_navigation_that_sends_no_request(self):
        code, result, _, _ = run_scraper(f"{SITE}/js-redirect?ms=2000&height=20000&to=about:blank")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["finalUrl"], "about:blank")

    def test_navigations_that_load_no_document(self):
        # The page stays as it was, with its own status, also when they get there through a redirect
        for to in ("/no-content", "/download", "/archive.zip", "/archive.zip%23part", "/redirect%3Fto%3D/no-content",
                   "/redirect%3Fto%3D/download"):
            with self.subTest(to=to):
                path = f"/js-redirect?ms=500&to={to}"
                code, result, _, seconds = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                self.assertLess(seconds, 30)
                self.assertEqual((result["httpStatus"], result["finalUrl"]), (200, SITE + path))
                self.assertEqual((result["contentType"], result["fileUrl"]), ("text/html; charset=utf-8", None))

    def test_page_that_reloads_into_a_download(self):
        path = f"/reloads-into-download?token={new_token()}"
        code, result, _, _ = run_scraper(SITE + path)
        self.assertEqual(code, 0, result)
        self.assertEqual((result["httpStatus"], result["contentType"], result["fileUrl"]),
                         (200, "text/html; charset=utf-8", None))

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
                code, result, _, seconds = run_scraper(SITE + path)
                self.assertEqual(code, 1, result)
                self.assertIn(message, result["error"])
                # Chrome's error page retries the navigation by itself, which isn't waited for
                self.assertLess(seconds, 20)

    def test_http_status_and_final_url(self):
        for path, status, final_path in (
            ("/missing", 404, "/missing"),
            ("/empty-error", 500, "/empty-error"),
            ("/redirect?to=/landed", 200, "/landed"),
            ("/js-redirect?ms=300", 200, "/landed"),
            # After a download, which failed as a navigation
            ("/js-redirect?ms=300&to=/download&ms=1500&to=/empty-error", 500, "/empty-error"),
        ):
            with self.subTest(path=path):
                code, result, _, seconds = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                self.assertEqual((result["httpStatus"], result["finalUrl"]), (status, SITE + final_path))
                self.assertIsNone(result["fileUrl"])
                # An error without a body never sends HTML, so it isn't waited for
                self.assertLess(seconds, 30)

    def test_chrome_runs_with_a_window_and_its_sandbox(self):
        self.assertNotEqual(os.getuid(), 0)
        scraper = subprocess.Popen(["node", SCRAPER, SITE + "/hanging-image"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 20
            while not any("--type=renderer" in args for _, args in processes()):
                self.assertLess(time.time(), deadline, "no renderer started")
                time.sleep(0.2)
            running = processes()
            browser, browser_args = next((pid, args) for pid, args in running
                                         if "--remote-debugging-pipe" in args and "--type=" not in args)
            renderer = next(pid for pid, args in running if "--type=renderer" in args)
            # Google Chrome itself, not headless
            self.assertTrue(browser_args.startswith("/opt/google/chrome/"), browser_args)
            self.assertNotIn("--headless", browser_args)
            self.assertFalse(any("--no-sandbox" in args for _, args in running))
            # The renderer runs in a user namespace of its own
            self.assertNotEqual(os.readlink(f"/proc/{browser}/ns/user"), os.readlink(f"/proc/{renderer}/ns/user"))
        finally:
            scraper.kill()
            scraper.wait()

    def test_screenshots_are_cut_at_8192_px(self):
        sizes = {}
        for height in (20000, 500):
            code, result, _, _ = run_scraper(f"{SITE}/tall?height={height}")
            self.assertEqual(code, 0, result)
            _, _, png = http("GET", result["screenshotUrl"])
            sizes[height] = png_size(png)
        self.assertEqual(sizes[20000][1], 8192)
        # A short page is as tall as the window
        self.assertLess(sizes[500][1], 1080)
        self.assertTrue(all(width <= 1920 for width, _ in sizes.values()), sizes)

    def test_long_page_is_put_back_after_its_screenshot(self):
        # It's cut short while it's captured, so that Chrome doesn't draw all of it
        for query, tags in (
            ("", [b"<html>", b"<body>"]),
            ("&styled=1", [b'<html style="background-color: white; -moz-osx-font-smoothing: grayscale;">',
                           b'<body style="margin: 0px; *zoom: 1;">']),
        ):
            with self.subTest(query=query):
                code, result, _, _ = run_scraper(f"{SITE}/tall?height=20000{query}")
                self.assertEqual(code, 0, result)
                _, _, html = http("GET", result["htmlUrl"])
                self.assertEqual(re.findall(rb"<(?:html|body)\b[^>]*>", html), tags)

    def test_html_has_the_lazy_content_below_the_screenshot(self):
        # Also when the page asks for smooth scrolling
        for path in ("/lazy", "/lazy?smooth=1"):
            with self.subTest(path=path):
                code, result, _, seconds = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                self.assertLess(seconds, 30)
                _, _, html = http("GET", result["htmlUrl"])
                self.assertIn(b"LAZY-CONTENT", html)

    def test_html_is_saved_as_utf8(self):
        code, result, _, _ = run_scraper(SITE + "/latin1")
        self.assertEqual(code, 0, result)
        _, headers, html = http("GET", result["htmlUrl"])
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("Año, señal, café".encode(), html)
        # The page's own <meta charset> would contradict the file
        self.assertIn(b'<meta charset="utf-8">', html)
        self.assertNotIn(b"iso-8859-1", html.lower())

    def test_html_has_the_content_of_open_shadow_roots(self):
        code, result, _, _ = run_scraper(SITE + "/shadow")
        self.assertEqual(code, 0, result)
        _, _, html = http("GET", result["htmlUrl"])
        self.assertTrue(html.startswith(b'<!DOCTYPE html><html lang="en">'), html[:100])
        self.assertIn(b'<div id="open"><template shadowrootmode="open"><p>open shadow content</p>', html)
        self.assertIn(b"<b>nested shadow content</b>", html)
        self.assertIn(b'<div id="declarative"><template shadowrootmode="open"><p>declarative shadow content</p>'
                      b"</template></div>", html)
        # Closed shadow roots can't be read
        self.assertNotIn(b"closed shadow content", html)

    def test_ad_blocker_styles_are_left_out_of_the_html(self):
        # Also the ones it keeps adding while the HTML is read
        for path in ("/ad", "/ad?history=1"):
            with self.subTest(path=path):
                code, result, _, _ = run_scraper(SITE + path)
                self.assertEqual(code, 0, result)
                # They hide the ad in the screenshot, so the page fits in the window
                _, _, png = http("GET", result["screenshotUrl"])
                self.assertLess(png_size(png)[1], 1080)
                # The HTML keeps the page's own style and the ad
                _, _, html = http("GET", result["htmlUrl"])
                self.assertEqual(re.findall(rb"<style\b[^>]*>(.*?)</style>", html, re.S), [b"p { color: black; }"])
                self.assertIn(b"<ad-slot", html)

    def test_ads_stay_hidden_when_the_page_is_captured_again(self):
        # Its download makes the capture start over, on the same page
        code, result, _, _ = run_scraper(SITE + "/ad?download=1")
        self.assertEqual(code, 0, result)
        # The ad would make it as tall as the cap
        _, _, png = http("GET", result["screenshotUrl"])
        self.assertLess(png_size(png)[1], 8192)
        _, _, html = http("GET", result["htmlUrl"])
        self.assertEqual(re.findall(rb"<style\b[^>]*>(.*?)</style>", html, re.S), [b"p { color: black; }"])

    def test_ad_blocking_list_downloads_and_updates(self):
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
