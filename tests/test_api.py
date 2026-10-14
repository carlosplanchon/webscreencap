"""The API end to end: authentication, validation, results and failures."""
import unittest

from helpers import API_KEY, API_URL, INTERNAL, SITE, api, http, submit, wait_for_api, wait_for_task


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wait_for_api()

    def test_requires_the_api_key_header(self):
        for key in (None, "wrong"):
            with self.subTest(key=key):
                status, _ = api("POST", "/task/screenshot", {"url": SITE + "/landed"}, key=key)
                self.assertEqual(status, 401)
                status, _ = api("GET", "/task_details/whatever", key=key)
                self.assertEqual(status, 401)

    def test_old_style_requests_are_rejected(self):
        # GET with the key in the query string
        status, _, _ = http("GET", f"{API_URL}/task/screenshot?url={SITE}/landed&api_key={API_KEY}")
        self.assertEqual(status, 405)

    def test_only_http_urls_are_accepted(self):
        for url in ("file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)"):
            with self.subTest(url=url):
                status, _ = api("POST", "/task/screenshot", {"url": url})
                self.assertEqual(status, 422)

    def test_unknown_task_is_404(self):
        status, _ = api("GET", "/task_details/no-such-task")
        self.assertEqual(status, 404)

    def test_task_is_pending_until_it_finishes(self):
        task_id = submit(SITE + "/landed")
        status, body = api("GET", f"/task_details/{task_id}")
        self.assertEqual((status, body["status"], body["result"]), (200, "PENDING", None))
        wait_for_task(task_id)

    def test_capture_returns_downloadable_files(self):
        details = wait_for_task(submit(SITE + "/landed"))
        self.assertEqual(details["status"], "SUCCESS", details)
        result = details["result"]
        self.assertEqual((result["http_status"], result["final_url"]), (200, SITE + "/landed"))

        status, headers, png = http("GET", result["screenshot_url"])
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

        status, headers, html = http("GET", result["html_url"])
        self.assertEqual((status, headers["Content-Type"]), (200, "text/html; charset=utf-8"))
        self.assertIn(b"LANDED", html)

    def test_failed_capture_is_a_failure_with_a_short_message(self):
        details = wait_for_task(submit(INTERNAL + "/"))
        self.assertEqual(details["status"], "FAILURE", details)
        message = details["result"]["message"]
        self.assertIn("is not a public address", message)
        # No stack traces, call logs or paths inside the container
        self.assertNotIn("\n", message)
        self.assertNotIn("/app/", message)


if __name__ == "__main__":
    unittest.main()
