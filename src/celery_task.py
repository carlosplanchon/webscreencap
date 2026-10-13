#!/usr/bin/env python3

from celery import Celery
from subprocess import run, PIPE, TimeoutExpired
import os
import json5


# Backstop in case scraper.js hangs past its own deadline (SCRIPT_TIMEOUT_MS):
SCRAPER_TIMEOUT = 180  # seconds


# Initialize Celery (both the API and the worker need these variables):
celery_app = Celery(
    __name__,
    broker=os.environ["CELERY_BROKER_URL"],
    backend=os.environ["CELERY_RESULT_BACKEND"],
    broker_connection_retry_on_startup=True,
)


@celery_app.task(bind=True)
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

        # Check return code
        if process.returncode != 0:
            return {
                "status": "error",
                "message": "Failed to process the URL",
                "error": process.stderr,
            }

        # Parse the script's JSON output
        output = process.stdout
        data = json5.loads(output)

    except TimeoutExpired as e:
        return {
            "status": "error",
            "message": "Timed out while processing the URL",
            "error": str(e),
        }
    except Exception as e:
        return {
            "status": "error",
            "message": "An unexpected error occurred",
            "error": str(e),
        }

    # Successful scrape
    return {
        "status": "success",
        "message": "Screenshot and HTML processed successfully",
        "screenshot_url": data.get("screenshotUrl"),
        "html_url": data.get("htmlUrl"),
    }
