#!/bin/sh
# Runs the command with a virtual display for Chrome's window
Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp &
until [ -S "/tmp/.X11-unix/X${DISPLAY#:}" ]; do
    kill -0 $! 2>/dev/null || exit 1
    sleep 0.1
done
exec "$@"
