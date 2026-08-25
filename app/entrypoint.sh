#!/bin/bash
# Container entrypoint: start Xvfb on :99 so headed Chrome / Chromium has
# something to attach to. The Chrome render path probes EGL platforms
# in this order (NVIDIA EGL impl on this kernel module exposes X11 +
# Surfaceless but only one ends up answering), so giving it an X display
# at least lets the X11 platform try.
set -e

mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix 2>/dev/null || true

Xvfb :99 -screen 0 1920x1080x24 +extension GLX +render -noreset \
    > /tmp/xvfb.log 2>&1 &

export DISPLAY=:99

for i in 1 2 3 4 5 6 7 8 9 10; do
    if [ -e /tmp/.X11-unix/X99 ]; then break; fi
    sleep 0.1
done

exec "$@"
