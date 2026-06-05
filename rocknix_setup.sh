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
# CPython versions to bundle. ROCKNIX ships 3.13 currently; we grab a range so
# launch.sh auto-picks the matching one — no need to know which. Tags with no
# wheel for this pygame version are just skipped.
TAGS="cp310 cp311 cp312 cp313"

echo "==> 1/4  clone or update the repo"
if [ -d Pewpew/.git ]; then
    git -C Pewpew pull --ff-only
else
    git clone "$REPO"
fi
cd Pewpew

echo "==> 2/4  (no pip needed — SteamOS's python often lacks it)"

echo "==> 3/4  download + unpack aarch64 pygame $PGVER for: $TAGS"
# Pure-stdlib: query the PyPI JSON API, pull the matching manylinux aarch64
# wheels (zips) over https and extract them. Needs only python3 (urllib, json,
# zipfile, ssl) — all in the base interpreter. No pip, no 'unzip'.
PGVER="$PGVER" TAGS="$TAGS" python3 - <<'PY'
import json, os, sys, ssl, zipfile, urllib.request
ver  = os.environ["PGVER"]
tags = set(os.environ["TAGS"].split())
url  = f"https://pypi.org/pypi/pygame/{ver}/json"
ctx  = ssl.create_default_context()
data = json.load(urllib.request.urlopen(url, context=ctx))
got  = set()
for f in data["urls"]:
    fn = f["filename"]
    if not (fn.endswith(".whl") and "manylinux" in fn and "aarch64" in fn):
        continue
    tag = next((t for t in tags if f"-{t}-" in fn), None)
    if not tag:
        continue
    dest = os.path.join("pylibs", tag)
    if os.path.isdir(os.path.join(dest, "pygame")):
        print(f"    - {tag}: already present, skipping")
        got.add(tag)
        continue
    os.makedirs(dest, exist_ok=True)
    tmp = os.path.join("/tmp", fn)
    print(f"    - {tag}: {fn}")
    with urllib.request.urlopen(f["url"], context=ctx) as r, open(tmp, "wb") as o:
        o.write(r.read())
    with zipfile.ZipFile(tmp) as z:
        z.extractall(dest)
    got.add(tag)
missing = tags - got
if missing:
    print(f"    !! no aarch64 wheel found for: {sorted(missing)}", file=sys.stderr)
if not got:
    sys.exit("    FAILED: downloaded nothing — check internet / PyPI reachability")
print(f"    pylibs built for: {sorted(got)}")
PY

# Where to install. Explicit PORTS=... env var or first arg wins; otherwise
# auto-detect the SD card's roms/ports under the dynamic SteamOS mount point
# (/run/media/<user>/<LABEL>/... — the label/uuid changes per card).
PORTS="${PORTS:-${1:-}}"
if [ -z "$PORTS" ]; then
    ME="${USER:-$(id -un 2>/dev/null)}"
    for cand in \
        /run/media/"$ME"/*/roms/ports  /run/media/*/roms/ports \
        /media/"$ME"/*/roms/ports      /media/*/roms/ports; do
        if [ -d "$cand" ]; then
            PORTS="$cand"
            echo "==> auto-detected card ports dir: $PORTS"
            break
        fi
    done
fi

if [ -n "$PORTS" ] && [ -d "$PORTS" ]; then
    DEST="$PORTS/Pewpew"
    echo "==> 4/4  installing to $DEST"
    rm -rf "$DEST"; mkdir -p "$DEST"
    # Copy the whole bundle except the git metadata (the in-game updater
    # fetches over https, it doesn't need the .git checkout).
    cp -r . "$DEST"
    rm -rf "$DEST/.git"
    # ROCKNIX shows a Ports entry for each .sh directly under ports/. This
    # wrapper hands off to the bundle's launcher; dirname keeps it working
    # regardless of where the card mounts on the device.
    printf '#!/bin/sh\nexec "$(dirname "$0")/Pewpew/launch.sh" "$@"\n' > "$PORTS/Pewpew.sh"
    chmod +x "$PORTS/Pewpew.sh" "$DEST/launch.sh" 2>/dev/null || true
    sync
    echo ""
    echo "    DONE — bundle + pygame ($(ls pylibs 2>/dev/null | tr '\n' ' ')) installed."
    echo "    Eject the card, put it in the Max 3, open PORTS, launch Pewpew."
    echo "    If it won't start, send back:  $DEST/last_run.log"
else
    echo "==> 4/4  bundle built at $(pwd) — but couldn't find the card's ports dir."
    if [ -n "$PORTS" ]; then
        echo "    (PORTS='$PORTS' does not exist — is the card mounted there?)"
    fi
    echo "    Candidate mounts on this machine:"
    ls -d /run/media/*/*/roms/ports /run/media/*/roms/ports \
          /media/*/*/roms/ports     /media/*/roms/ports 2>/dev/null \
       | sed 's/^/       /' || echo "       (none found — is the SD card inserted/mounted?)"
    cat <<'NEXT'
    Then re-run with that path, e.g.:
       curl -fsSL https://raw.githubusercontent.com/georgepauna/Pewpew/master/rocknix_setup.sh | PORTS=/run/media/deck/<LABEL>/roms/ports bash
NEXT
fi
