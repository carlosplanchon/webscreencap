#!/usr/bin/env python3

from celery import Celery
from celery.utils.log import get_task_logger
from subprocess import run, PIPE, TimeoutExpired
import json
import os


# In case scraper.js hangs past its own deadline
SCRAPER_TIMEOUT = 180  # seconds

logger = get_task_logger(__name__)


# Initialize Celery (both the API and the worker need these variables):
celery_app = Celery(
    __name__,
    broker=os.environ["CELERY_BROKER_URL"],
    backend=os.environ["CELERY_RESULT_BACKEND"],
    broker_connection_retry_on_startup=True,
)


class ScrapeError(Exception):
    """A failed capture. Its message is meant for API clients."""


def read_result(stdout):
    # scraper.js prints its result as the last line of stdout, in JSON
    lines = stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1])
    except (IndexError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


# Expected failures: logged as INFO, without a traceback
@celery_app.task(bind=True, throws=(ScrapeError,))
def capture_screenshot_and_html(self, url):
    script_path = os.path.join(os.path.dirname(__file__), "scraper.js")

    try:
        # Execute the Node.js script synchronously
        process = run(
            ["node", script_path, url],
            stdout=PIPE,
            stderr=PIPE,
            text=True,
            timeout=SCRAPER_TIMEOUT
        )
    except TimeoutExpired:
        raise ScrapeError("Timed out while processing the URL")

    result = read_result(process.stdout)
    if process.returncode != 0 or "screenshotUrl" not in result:
        # The details (stack traces, ...) stay in the worker logs
        logger.warning(
            "scraper.js failed for %s (exit code %s):\n%s",
            url, process.returncode, process.stderr
        )
        raise ScrapeError(result.get("error") or "Failed to process the URL")

    # Successful scrape
    return {
        "status": "success",
        "message": "Screenshot and HTML processed successfully",
        "screenshot_url": result["screenshotUrl"],
        "html_url": result["htmlUrl"],
        "http_status": result.get("httpStatus"),
        "final_url": result.get("finalUrl"),
    }
