#!/usr/bin/env python3

from celery import Celery
from subprocess import run, PIPE
import os
import json5
from shared import CONFIG


# Initialize Celery:
celery_app = Celery(
    __name__,
    broker=CONFIG["celery"]["broker"],
    backend=CONFIG["celery"]["backend"],
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
            text=True
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
