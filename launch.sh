#!/bin/sh
# Pewpew launcher for RG35XX Pro (MuOS / Knulli / Batocera / GarlicOS).
# Place this file and pewpew.py together under your firmware's apps folder.

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
        exec "$PY" "$DIR/pewpew.py" "$@"
    fi
done

echo "No python interpreter found." >&2
exit 1
