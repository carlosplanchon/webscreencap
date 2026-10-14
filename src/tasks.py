#!/usr/bin/env python3

import asyncio
import json
import logging
import os
from typing import Annotated

from redis import exceptions as redis_errors
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from taskiq import Context, TaskiqDepends
from taskiq.serializers import JSONSerializer
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker


# In case scraper.js hangs past its own deadline
SCRAPER_TIMEOUT = 180  # seconds
# As long as the presigned URLs
RESULT_TTL = 86400  # seconds
# A capture cut short by a dead worker is retried once
MAX_ATTEMPTS = 2

REDIS_URL = os.environ["REDIS_URL"]
# Reconnects when Redis has dropped a connection (after a restart, for instance)
REDIS_RETRY = {
    "retry": Retry(ExponentialBackoff(cap=2, base=0.2), 5),
    "retry_on_error": [redis_errors.ConnectionError, redis_errors.TimeoutError],
}

logger = logging.getLogger(__name__)

# One message at a time; a dead worker's message is retried after idle_timeout
broker = RedisStreamBroker(
    REDIS_URL,
    consumer_id="0",
    xread_count=1,
    unacknowledged_batch_size=1,
    unacknowledged_lock_timeout=30,
    idle_timeout=300_000,
    maxlen=10_000,
    **REDIS_RETRY,
).with_result_backend(
    RedisAsyncResultBackend(
        REDIS_URL,
        result_ex_time=RESULT_TTL,
        serializer=JSONSerializer(),
        prefix_str="webscreencap-result",
        **REDIS_RETRY,
    )
)
redis = Redis.from_url(REDIS_URL, **REDIS_RETRY)


class ScrapeError(Exception):
    """A failed capture. Its message is meant for API clients."""


class ExpectedErrorsFilter(logging.Filter):
    # A ScrapeError is an expected outcome: INFO, without a traceback
    def filter(self, record):
        error = record.exc_info[1] if record.exc_info else None
        if isinstance(error, ScrapeError):
            record.levelno, record.levelname = logging.INFO, "INFO"
            record.msg, record.args, record.exc_info = "Capture failed: %s", (error,), None
        return True


logging.getLogger("taskiq.receiver.receiver").addFilter(ExpectedErrorsFilter())


def read_result(stdout):
    # scraper.js prints its result as the last line of stdout, in JSON
    lines = stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1])
    except (IndexError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


@broker.task(task_name="capture_screenshot_and_html")
async def capture_screenshot_and_html(url: str, context: Annotated[Context, TaskiqDepends()]) -> dict:
    attempts_key = f"webscreencap-attempts-{context.message.task_id}"
    attempts = await redis.incr(attempts_key)
    await redis.expire(attempts_key, RESULT_TTL)
    if attempts > MAX_ATTEMPTS:
        raise ScrapeError("The worker stopped while processing the URL")

    script_path = os.path.join(os.path.dirname(__file__), "scraper.js")
    process = await asyncio.create_subprocess_exec(
        "node", script_path, url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), SCRAPER_TIMEOUT)
    except TimeoutError:
        raise ScrapeError("Timed out while processing the URL")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    result = read_result(stdout.decode(errors="replace"))
    if process.returncode != 0 or "screenshotUrl" not in result:
        # The details (stack traces, ...) stay in the worker logs
        logger.warning(
            "scraper.js failed for %s (exit code %s):\n%s",
            url, process.returncode, stderr.decode(errors="replace")
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
