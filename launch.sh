#!/bin/sh
# Pewpew launcher. Works on:
#   - Anbernic RG35XX Pro stock OS (Ubuntu 22.04 + python3 + python3-pygame)
#   - MuOS / Knulli / Batocera CFWs
#   - Any Linux with Python and Pygame on PATH

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

# Make sure SDL targets the framebuffer/KMSDRM instead of trying X11.
export SDL_VIDEODRIVER="${SDL_VIDEODRIVER:-kmsdrm}"
export SDL_AUDIODRIVER="${SDL_AUDIODRIVER:-alsa}"
export SDL_NOMOUSE=1
export PYTHONUNBUFFERED=1

# Prefer the firmware-provided python; fall back to anything on PATH.
for PY in python3 python /usr/bin/python3 /usr/bin/python; do
    if command -v "$PY" >/dev/null 2>&1; then
        exec "$PY" "$DIR/pewpew.py" "$@" 2>&1 | tee "$DIR/last_run.log"
    fi
done

echo "No python interpreter found." >&2
exit 1
