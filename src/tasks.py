#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import uuid
from typing import Annotated

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from taskiq import AckableMessage, Context, NoResultError, TaskiqDepends, TaskiqEvents, TaskiqState
from taskiq.serializers import JSONSerializer
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker


# In case scraper.js hangs past its own deadline
SCRAPER_TIMEOUT = 180  # seconds
# As long as the presigned URLs
RESULT_TTL = 86400  # seconds
# A capture cut short by a dead worker is retried once
MAX_ATTEMPTS = 2
# How often the worker checks whether the ad-blocking lists are due for an update
ADBLOCKER_CHECK_INTERVAL = 3600  # seconds

REDIS_URL = os.environ["REDIS_URL"]
# Reconnects when Redis has dropped a connection (after a restart, for instance)
REDIS_RETRY = Retry(ExponentialBackoff(cap=2, base=0.2), 5)

logger = logging.getLogger(__name__)


class StreamBroker(RedisStreamBroker):
    # Deletes each message once it's acknowledged, so the stream only keeps the ones still to do
    def _ack_generator(self, id, queue_name):
        async def ack():
            async with Redis(connection_pool=self.connection_pool) as redis_conn:
                pipeline = redis_conn.pipeline()
                pipeline.xack(queue_name, self.consumer_group_name, id).xdel(queue_name, id)
                await pipeline.execute()

        return ack

    # Takes over a dead worker's messages also when no new message arrives
    async def listen(self):
        async with Redis(connection_pool=self.connection_pool) as redis_conn:
            while True:
                fetched = await redis_conn.xreadgroup(
                    self.consumer_group_name, self.consumer_name, {self.queue_name: ">"},
                    count=self.count, block=self.block,
                )
                for _, messages in fetched:
                    async for message in self._tasks(messages):
                        yield message
                _, claimed, *_ = await redis_conn.xautoclaim(
                    self.queue_name, self.consumer_group_name, self.consumer_name,
                    min_idle_time=self.idle_timeout, count=self.unacknowledged_batch_size,
                )
                async for message in self._tasks(claimed):
                    yield message

    async def _tasks(self, entries):
        for message_id, message in entries:
            ack = self._ack_generator(id=message_id, queue_name=self.queue_name)
            # Anything else in the stream would fail the worker every time it's taken over
            if b"data" not in message:
                logger.warning("Dropped stream entry %s, which isn't a task", message_id)
                await ack()
                continue
            yield AckableMessage(data=message[b"data"], ack=ack)


# One message at a time; a dead worker's message is retried after idle_timeout
broker = StreamBroker(
    REDIS_URL,
    # Not TaskIQ's default names, which another app on the same Redis may use
    queue_name="webscreencap",
    consumer_group_name="webscreencap",
    consumer_id="0",
    xread_count=1,
    unacknowledged_batch_size=1,
    idle_timeout=300_000,
    retry=REDIS_RETRY,
).with_result_backend(
    RedisAsyncResultBackend(
        REDIS_URL,
        result_ex_time=RESULT_TTL,
        serializer=JSONSerializer(),
        prefix_str="webscreencap-result",
        retry=REDIS_RETRY,
    )
)
redis = Redis(connection_pool=broker.connection_pool)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def keep_ad_blocking_lists_updated(state: TaskiqState) -> None:
    async def update():
        script_path = os.path.join(os.path.dirname(__file__), "adblocker.js")
        while True:
            try:
                process = await asyncio.create_subprocess_exec("node", script_path, "--update")
                await process.wait()
            except Exception:
                logger.exception("Could not update the ad-blocking lists")
            await asyncio.sleep(ADBLOCKER_CHECK_INTERVAL)

    # Kept, so that the task isn't garbage collected
    state.adblocker_updates = asyncio.create_task(update())


# Unknown ids have no result either, so the ones handed out are remembered
def task_marker_key(task_id: str) -> str:
    return f"webscreencap-task-{task_id}"


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
    task_id = context.message.task_id
    # Done already, but its acknowledgment got lost
    if await broker.result_backend.is_result_ready(task_id):
        raise NoResultError
    # So old that the API has forgotten it; otherwise, the API keeps it while it runs
    if not await redis.expire(task_marker_key(task_id), RESULT_TTL):
        raise NoResultError

    # One member per run, so a command that redis-py resends doesn't count twice
    attempts_key = f"webscreencap-attempts-{task_id}"
    await redis.sadd(attempts_key, uuid.uuid4().hex)
    await redis.expire(attempts_key, RESULT_TTL)
    if await redis.scard(attempts_key) > MAX_ATTEMPTS:
        raise ScrapeError("The worker stopped while processing the URL")

    script_path = os.path.join(os.path.dirname(__file__), "scraper.js")
    process = await asyncio.create_subprocess_exec(
        "node", script_path, url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Outside the worker's process group, so a Ctrl+C in its terminal lets the capture finish
        start_new_session=True,
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
    if process.returncode != 0 or "finalUrl" not in result:
        # The details (stack traces, ...) stay in the worker logs
        logger.warning(
            "scraper.js failed for %s (exit code %s):\n%s",
            url, process.returncode, stderr.decode(errors="replace")
        )
        raise ScrapeError(result.get("error") or "Failed to process the URL")

    # Successful scrape
    saved = " and ".join(name for name, key in (("screenshot", "screenshotUrl"), ("HTML", "htmlUrl"), ("file", "fileUrl"))
                         if result.get(key))
    return {
        "status": "success",
        "message": f"{saved[:1].upper()}{saved[1:]} processed successfully" if saved else "The URL sent no content",
        "screenshot_url": result.get("screenshotUrl"),
        "html_url": result.get("htmlUrl"),
        "file_url": result.get("fileUrl"),
        "content_type": result.get("contentType"),
        "http_status": result.get("httpStatus"),
        "final_url": result.get("finalUrl"),
    }
