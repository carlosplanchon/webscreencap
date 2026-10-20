#!/bin/sh
# Runs the command with a virtual display for Chrome's window
set -e
fifo="$(mktemp -d)/display"
mkfifo "$fifo"
# In a session of its own, so that a Ctrl+C lets the command finish, and stopped when the command ends
setsid setpriv --pdeathsig TERM Xvfb -displayfd 3 -screen 0 1920x1080x24 -nolisten tcp 3>"$fifo" &
# Xvfb picks a free display and writes its number once it's ready, or nothing if it fails
read -r number <"$fifo" || true
rm -r "${fifo%/*}"
[ -n "$number" ] || exit 1
export DISPLAY=":$number"
exec "$@"
