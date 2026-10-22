"""The API end to end: authentication, validation, results, failures, Redis reconnections and docs."""
import json
import os
import tomllib
import unittest

from helpers import (API_KEY, API_ROOT, API_URL, INTERNAL, SITE, api, drop_redis_connections, http, submit,
                     wait_for_api, wait_for_task)


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

    def test_screenshot_option_is_a_boolean(self):
        status, _ = api("POST", "/task/screenshot", {"url": SITE + "/landed", "screenshot": "sometimes"})
        self.assertEqual(status, 422)

    def test_unknown_task_is_404(self):
        # "webscreencap" is also the name of the queue's key in Redis
        for task_id in ("no-such-task", "webscreencap"):
            with self.subTest(task_id=task_id):
                status, _ = api("GET", f"/task_details/{task_id}")
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
        self.assertEqual((result["file_url"], result["content_type"]), (None, "text/html; charset=utf-8"))
        self.assertEqual(result["message"], "Screenshot and HTML processed successfully")

    def test_screenshot_can_be_left_out(self):
        details = wait_for_task(submit(SITE + "/landed", screenshot=False))
        self.assertEqual(details["status"], "SUCCESS", details)
        result = details["result"]
        self.assertIsNone(result["screenshot_url"])
        self.assertIn(b"LANDED", http("GET", result["html_url"])[2])
        self.assertEqual(result["message"], "HTML processed successfully")

    def test_file_is_returned_as_the_site_sent_it(self):
        details = wait_for_task(submit(SITE + "/file.pdf"))
        self.assertEqual(details["status"], "SUCCESS", details)
        result = details["result"]
        self.assertEqual((result["html_url"], result["content_type"]), (None, "application/pdf"))
        self.assertEqual(http("GET", result["file_url"])[2], http("GET", SITE + "/file.pdf")[2])
        self.assertEqual(result["message"], "Screenshot and file processed successfully")

    def test_failed_capture_is_a_failure_with_a_short_message(self):
        details = wait_for_task(submit(INTERNAL + "/"))
        self.assertEqual(details["status"], "FAILURE", details)
        message = details["result"]["message"]
        self.assertIn("is not a public address", message)
        # No stack traces, call logs or paths inside the container
        self.assertNotIn("\n", message)
        self.assertNotIn("/app/", message)

    def test_survives_dropped_redis_connections(self):
        self.assertEqual(wait_for_task(submit(SITE + "/landed"))["status"], "SUCCESS")
        self.assertTrue(drop_redis_connections().startswith(b":"))
        details = wait_for_task(submit(SITE + "/landed"))
        self.assertEqual(details["status"], "SUCCESS", details)

    def test_docs_are_under_the_same_path(self):
        status, _, page = http("GET", API_ROOT + "/docs")
        self.assertEqual(status, 200)
        self.assertIn(b"/webscreencap/openapi.json", page)
        # So that the requests made from the docs go there too
        schema = json.loads(http("GET", API_ROOT + "/openapi.json")[2])
        self.assertEqual(schema["servers"], [{"url": "/webscreencap"}])

    def test_docs_show_the_project_version(self):
        with open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), "rb") as pyproject:
            version = tomllib.load(pyproject)["project"]["version"]
        schema = json.loads(http("GET", API_ROOT + "/openapi.json")[2])
        self.assertEqual(schema["info"]["version"], version)


if __name__ == "__main__":
    unittest.main()
