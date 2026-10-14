#!/bin/bash
# Extra arguments go to unittest, e.g. tests/run.sh -k redirect
set -u
cd "$(dirname "$0")/.."

compose=(docker compose -f compose.test.yaml)
"${compose[@]}" build || exit 1
"${compose[@]}" run --rm --quiet-pull tests python -m unittest discover -v "$@"
status=$?
if [ "$status" -ne 0 ]; then
    "${compose[@]}" logs --no-color api worker
fi
"${compose[@]}" down
exit "$status"
