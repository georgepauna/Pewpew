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

# Tell pewpew.py this is a handheld so it takes the fullscreen + device
# button scheme + hardware-volume path. Anbernic stock auto-detects via the
# mali driver / /mnt/mmc; every other CFW (ROCKNIX/JELOS/Batocera on kmsdrm)
# needs this hint. Override with PEWPEW_ON_DEVICE=0 to force the dev path.
export PEWPEW_ON_DEVICE="${PEWPEW_ON_DEVICE:-1}"

# Bundled pygame for firmwares that don't ship it (e.g. ROCKNIX/JELOS) lives in
# ./pylibs — either a flat unpacked wheel (pylibs/pygame/...) or per-cpython-tag
# subdirs (pylibs/cp311/pygame/...). The wiring is INSIDE the python loop below
# so it can match the interpreter's version, and it's only added when the system
# has no pygame (PYTHONPATH precedes site-packages, so we must not shadow a
# working system build with a possibly ABI-mismatched bundle).

# Auto-update used to live here (pull pewpew.py + JSON from master before
# launch). As of v0.6.0 the updater is inside pewpew.py itself, so it can
# track a channel (stable=latest GitHub release, uat=master tip) and
# overwrite launch.sh too. Disable with PEWPEW_AUTOUPDATE=0 or a
# .no_autoupdate marker; see `_check_release_update` in pewpew.py.

# Prefer the firmware-provided python; fall back to anything on PATH.
# Note: `exec cmd | tee` does NOT replace the shell because the pipeline
# forces a fork. Without an explicit `exit`, the for loop would advance to
# the next candidate after pewpew exits and launch a second instance —
# which on the RG35XX Pro looked like the game restarting once before
# the launcher menu re-appeared. Run, then exit with the same status.
for PY in python3 python /usr/bin/python3 /usr/bin/python; do
    if command -v "$PY" >/dev/null 2>&1; then
        # Only fall back to the bundled pygame when the system has none. Pick
        # the per-cpython-tag subdir (pylibs/cp311) matching this interpreter,
        # else a flat pylibs/.
        if ! "$PY" -c "import pygame" >/dev/null 2>&1; then
            PGTAG="$("$PY" -c 'import sys;print("cp%d%d"%sys.version_info[:2])' 2>/dev/null)"
            if [ -n "$PGTAG" ] && [ -d "$DIR/pylibs/$PGTAG" ]; then
                export PYTHONPATH="$DIR/pylibs/$PGTAG:${PYTHONPATH}"
            elif [ -d "$DIR/pylibs" ]; then
                export PYTHONPATH="$DIR/pylibs:${PYTHONPATH}"
            fi
        fi
        # Diagnostic header lands in last_run.log so a failed first boot on
        # a new device tells us the python version, whether pygame imports,
        # and the resolved drivers — without needing SSH access.
        {
            echo "=== pewpew launch ==="
            echo "PY=$("command" -v "$PY")  $("$PY" -V 2>&1)"
            "$PY" -c "import pygame; print('pygame', pygame.version.ver, '| image.get_extended', pygame.image.get_extended())" 2>&1 \
                || echo "!! pygame import FAILED — drop an aarch64 pygame wheel into ./pylibs"
            echo "SDL_VIDEODRIVER=$SDL_VIDEODRIVER  SDL_AUDIODRIVER=$SDL_AUDIODRIVER  PEWPEW_ON_DEVICE=$PEWPEW_ON_DEVICE"
            echo "PYTHONPATH=$PYTHONPATH"
            echo "====================="
        } > "$DIR/last_run.log" 2>&1
        # Append straight to the log (no tee pipe) so we capture pewpew's
        # REAL exit status — a pipe would give us tee's. 139 = SIGSEGV (the
        # GLES/mali crash), 134 = SIGABRT, 0 = clean quit.
        "$PY" "$DIR/pewpew.py" "$@" >> "$DIR/last_run.log" 2>&1
        status=$?
        echo "exit=$status" >> "$DIR/last_run.log"
        exit "$status"
    fi
done

echo "No python interpreter found." >&2
exit 1
