#!/usr/bin/env bash
#
# Pewpew — ROCKNIX/JELOS setup, run on a SteamOS (or any x86_64 Linux) box.
# ===========================================================================
# ROCKNIX ships Python 3 but NOT pygame, so we cross-download an aarch64
# pygame wheel here and unpack it into the bundle's ./pylibs (launch.sh adds
# the version-matching subdir to PYTHONPATH on the device). Then you copy the
# finished Pewpew/ folder onto the handheld's SD card.
#
# COPY-PASTE THIS WHOLE FILE into a SteamOS Konsole (Desktop Mode), or:
#   curl -fsSL https://raw.githubusercontent.com/georgepauna/Pewpew/master/rocknix_setup.sh | bash
#
# Nothing here touches the system; pip installs to --user and downloads to /tmp.
# ===========================================================================
set -eu

REPO="https://github.com/georgepauna/Pewpew.git"
PGVER="2.6.1"
# CPython versions to bundle. ROCKNIX is almost certainly one of these; we grab
# all three so launch.sh auto-picks the matching one — no need to know which.
TAGS="cp310 cp311 cp312"

echo "==> 1/4  clone or update the repo"
if [ -d Pewpew/.git ]; then
    git -C Pewpew pull --ff-only
else
    git clone "$REPO"
fi
cd Pewpew

echo "==> 2/4  ensure pip is available (installs to ~/.local if missing)"
python3 -m ensurepip --user >/dev/null 2>&1 || true
python3 -m pip --version >/dev/null 2>&1 || python3 -m pip install --user --upgrade pip

echo "==> 3/4  download + unpack aarch64 pygame $PGVER for: $TAGS"
mkdir -p pylibs
for T in $TAGS; do
    PYV="${T#cp}"                     # cp311 -> 311 (pip accepts the bare form)
    echo "    - $T (python 3.${PYV#?})"
    rm -rf "/tmp/pg-$T"; mkdir -p "/tmp/pg-$T"
    python3 -m pip download "pygame==$PGVER" \
        --only-binary=:all: --no-deps \
        --platform manylinux2014_aarch64 \
        --implementation cp --abi "$T" --python-version "$PYV" \
        -d "/tmp/pg-$T"
    WHL="$(ls /tmp/pg-$T/pygame-*.whl 2>/dev/null | head -n1)"
    if [ -z "$WHL" ]; then echo "      !! no wheel for $T (skipping)"; continue; fi
    rm -rf "pylibs/$T"; mkdir -p "pylibs/$T"
    python3 -m zipfile -e "$WHL" "pylibs/$T"   # no 'unzip' dependency
done
echo "    pylibs now contains: $(ls pylibs)"

echo "==> 4/4  DONE building. Bundle is ready at:  $(pwd)"
cat <<'NEXT'

--------------------------------------------------------------------------
Now put it on the Max 3's SD card:

  1. Insert the card. Find its ports dir (ROCKNIX games partition):
       PORTS="/run/media/$USER/<ROMS-partition>/roms/ports"

  2. Copy the whole folder + add a Ports launcher entry:
       cp -r . "$PORTS/Pewpew"
       printf '#!/bin/sh\nexec "$(dirname "$0")/Pewpew/launch.sh" "$@"\n' > "$PORTS/Pewpew.sh"
       chmod +x "$PORTS/Pewpew.sh" "$PORTS/Pewpew/launch.sh"
       sync

  3. Eject, put the card in the Max 3, open PORTS, launch Pewpew.

If it still doesn't start, read the log back on this machine:
       cat "$PORTS/Pewpew/last_run.log"
and send it over — the header shows python version + pygame status.
--------------------------------------------------------------------------
NEXT
