#!/bin/sh
# Pewpew launcher. Works on:
#   - Anbernic RG35XX Pro stock OS (Ubuntu 22.04 + python3 + python3-pygame)
#   - MuOS / Knulli / Batocera CFWs
#   - Any Linux with Python and Pygame on PATH

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

# Anbernic stock OS ships SDL with the Mali EGL driver (same one RetroArch
# uses for HW-accelerated video). MuOS/Knulli/Batocera ship newer SDLs that
# prefer kmsdrm. Either gets resolved at runtime if the caller doesn't set it.
if [ -z "$SDL_VIDEODRIVER" ]; then
    if [ -e /usr/lib/libSDL2-2.0.so.0.12.0 ]; then
        export SDL_VIDEODRIVER=mali
    else
        export SDL_VIDEODRIVER=kmsdrm
    fi
fi
export SDL_AUDIODRIVER="${SDL_AUDIODRIVER:-alsa}"
export SDL_NOMOUSE=1
export PYTHONUNBUFFERED=1

# Prefer the firmware-provided python; fall back to anything on PATH.
# Note: `exec cmd | tee` does NOT replace the shell because the pipeline
# forces a fork. Without an explicit `exit`, the for loop would advance to
# the next candidate after pewpew exits and launch a second instance —
# which on the RG35XX Pro looked like the game restarting once before
# the launcher menu re-appeared. Run, then exit with the same status.
for PY in python3 python /usr/bin/python3 /usr/bin/python; do
    if command -v "$PY" >/dev/null 2>&1; then
        "$PY" "$DIR/pewpew.py" "$@" 2>&1 | tee "$DIR/last_run.log"
        exit $?
    fi
done

echo "No python interpreter found." >&2
exit 1
