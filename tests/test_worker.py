"""The worker: starting and stopping it, and its queue in Redis."""
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from helpers import (SITE, SITE_CONTROL, WORKER, api, fixture_stats, new_token, processes, redis_command, submit,
                     wait_for_api, wait_for_task)

# About 15 s, since one of its images never loads
LONG_CAPTURE = SITE + "/hanging-image"
# Queues a message for a consumer that took it 10 minutes ago and never acknowledged it, as a dead worker would
ABANDON = """
local id = redis.call('XADD', KEYS[1], '*', 'data', ARGV[1])
redis.call('XREADGROUP', 'GROUP', ARGV[2], ARGV[3], 'COUNT', 1, 'STREAMS', KEYS[1], '>')
return redis.call('XCLAIM', KEYS[1], ARGV[2], ARGV[3], 0, id, 'IDLE', 600000, 'JUSTID')
"""


def wait_until(condition, timeout, message):
    deadline = time.time() + timeout
    while not condition():
        if time.time() > deadline:
            raise AssertionError(message)
        time.sleep(0.2)


class WorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wait_for_api()

    def test_fails_at_startup_with_bad_settings(self):
        missing = {name: value for name, value in os.environ.items() if name != "REDIS_URL"}
        for env, message in (
            (missing, "KeyError: 'REDIS_URL'"),
            ({**os.environ, "REDIS_URL": "redis://redis:6390/0"}, "redis:6390"),
        ):
            with self.subTest(message=message):
                start = time.time()
                process = subprocess.run([WORKER], env=env, capture_output=True, text=True, timeout=60)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(message, process.stderr)
                self.assertLess(time.time() - start, 30)

    def test_ctrl_c_lets_the_capture_in_progress_finish(self):
        with tempfile.NamedTemporaryFile() as log:
            # A process group of its own, like a command run from a terminal
            worker = subprocess.Popen([WORKER], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                wait_until(lambda: b"Listening started" in Path(log.name).read_bytes(), 60, "the worker didn't start")
                # The stack's worker takes one of them, and this one the other
                task_ids = [submit(LONG_CAPTURE) for _ in range(2)]
                wait_until(lambda: any("scraper.js" in args for _, args in processes()), 30, "this worker took no task")
                os.killpg(worker.pid, signal.SIGINT)
                for task_id in task_ids:
                    details = wait_for_task(task_id)
                    self.assertEqual(details["status"], "SUCCESS", details)
                self.assertEqual(worker.wait(timeout=60), 0, Path(log.name).read_text())
            finally:
                if worker.poll() is None:
                    os.killpg(worker.pid, signal.SIGKILL)
                    worker.wait()

    def test_finished_tasks_leave_the_queue(self):
        wait_for_task(submit(SITE + "/landed"))
        self.assertEqual(redis_command("TYPE", "webscreencap"), b"+stream\r\n")
        self.assertIn(b"webscreencap", redis_command("XINFO", "GROUPS", "webscreencap"))
        wait_until(lambda: redis_command("XLEN", "webscreencap") == b":0\r\n", 10, "finished tasks stay in the queue")

    def test_task_of_a_dead_worker_is_taken_over(self):
        token = new_token()
        task_id = new_token()
        message = {"task_id": task_id, "task_name": "capture_screenshot_and_html", "labels": {},
                   "args": [f"{SITE}/landed?{token}"], "kwargs": {}}
        redis_command("SET", f"webscreencap-task-{task_id}", "1", "EX", 3600)
        # In one step, so that the stack's worker can't take the message first
        reply = redis_command("EVAL", ABANDON, 1, "webscreencap", json.dumps(message), "webscreencap", "dead-worker")
        self.assertTrue(reply.startswith(b"*1\r\n"), reply)
        # No other task arrives
        details = wait_for_task(task_id, timeout=60)
        self.assertEqual(details["status"], "SUCCESS", details)
        self.assertTrue(any(token in path for path in fixture_stats(SITE_CONTROL)["requests"]))

    def test_task_that_waited_too_long_is_dropped(self):
        busy = submit(LONG_CAPTURE)
        token = new_token()
        late = submit(f"{SITE}/landed?{token}")
        # As if the day it's kept for had gone by
        self.assertEqual(redis_command("DEL", f"webscreencap-task-{late}"), b":1\r\n")
        wait_for_task(busy)
        # Tasks run in order, so the late one is done with by then
        wait_for_task(submit(SITE + "/landed"))
        self.assertEqual(api("GET", f"/task_details/{late}")[0], 404)
        self.assertFalse(any(token in path for path in fixture_stats(SITE_CONTROL)["requests"]))

    def test_task_with_a_result_doesnt_run_again(self):
        token = new_token()
        url = f"{SITE}/landed?{token}"
        task_id = submit(url)
        first = wait_for_task(task_id)
        self.assertEqual(first["status"], "SUCCESS", first)
        # The same message again, as when its acknowledgment gets lost
        message = {"task_id": task_id, "task_name": "capture_screenshot_and_html", "labels": {}, "args": [url],
                   "kwargs": {}}
        redis_command("XADD", "webscreencap", "*", "data", json.dumps(message))
        wait_for_task(submit(SITE + "/landed"))
        self.assertEqual(api("GET", f"/task_details/{task_id}")[1], first)
        requests = fixture_stats(SITE_CONTROL)["requests"]
        self.assertEqual(sum(token in path for path in requests), 1)


if __name__ == "__main__":
    unittest.main()
