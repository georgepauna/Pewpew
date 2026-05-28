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

# Optional GitHub auto-update: pull the latest pewpew.py + JSON companions
# from master before launching, so a `git push` is enough to roll out a
# code-only change without touching the SD card. BMPs + music_cache stay
# whatever _deploy.py last wrote — the device's SDL has no PNG decoder so
# we can't auto-update art this way.
#
# Disable by setting PEWPEW_AUTOUPDATE=0 in the environment, or by deleting
# .autoupdate from the bundle dir. A 5 s curl timeout means an offline /
# slow Wi-Fi device still launches the cached copy on time.
if [ "${PEWPEW_AUTOUPDATE:-1}" = "1" ] && [ ! -e "$DIR/.no_autoupdate" ]; then
    RAW="https://raw.githubusercontent.com/georgepauna/Pewpew/master"
    for f in pewpew.py art/layout.json art/sprite_engine.json; do
        # Download to a sibling tempfile; only move into place on a clean
        # HTTP 200 so a half-finished fetch can't brick the bundle.
        tmp="$DIR/$f.update"
        if curl -fsSL --max-time 5 -o "$tmp" "$RAW/$f" 2>/dev/null; then
            mv "$tmp" "$DIR/$f"
        else
            rm -f "$tmp"
        fi
    done
fi

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
