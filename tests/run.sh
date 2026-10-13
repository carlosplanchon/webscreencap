#!/bin/bash
# Runs the tests in a throwaway Docker Compose stack (compose.test.yaml) and removes
# the stack afterwards. Extra arguments go to unittest, e.g. `tests/run.sh -k redirect`.
set -u
cd "$(dirname "$0")/.."

compose=(docker compose -f compose.test.yaml)
"${compose[@]}" build || exit 1
"${compose[@]}" run --rm --quiet-pull tests python -m unittest discover -v "$@"
status=$?
"${compose[@]}" down
exit "$status"
