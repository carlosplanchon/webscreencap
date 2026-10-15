#!/bin/sh
# Starts the TaskIQ worker
set -e
cd "$(dirname "$0")"

# TaskIQ would keep restarting a worker process that can't start, so bad settings fail here first
python -c "import asyncio, tasks; asyncio.run(tasks.redis.ping())"
# One capture at a time, so Chromium fits in the VM's memory
exec taskiq worker tasks:broker --workers 1 --max-async-tasks 1 "$@"
