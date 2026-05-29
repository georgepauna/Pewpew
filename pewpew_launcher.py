#!/usr/bin/env python3
"""Pewpew launcher (no auto-pull).

One-file launcher: clones github.com/georgepauna/Pewpew on first run,
sets up pygame inside a private venv (~/.local/share/pewpew/venv —
avoids SteamOS's read-only multi-arch lib paths that make
`pip install --user` fail with "wrong ELF class"), then execs pewpew.py
from that venv's python.

**As of v0.8.x the launcher does NOT pull on subsequent launches.** Updates
are opt-in inside the game: the title screen surfaces an "UPDATE
AVAILABLE" overlay when there's a newer release, and pressing ability
(silk X / Y) installs. Pre-v0.8.0 builds of this launcher did a
`git fetch + reset --hard origin/master` every launch — that bypassed
the channel system + release-notes prompt entirely. If you're seeing
silent updates on every launch, your launcher predates this change;
re-fetch it with the curl command below.

────────────────────────────────────────────────────────────────────────────
Steam Deck install (Game Mode launches Pewpew; updates are opt-in)
────────────────────────────────────────────────────────────────────────────

  1. Switch the Deck to **Desktop Mode** (Steam → Power → Switch to Desktop).

  2. Save this script somewhere stable, e.g.:

         curl -L -o ~/pewpew_launcher.py \
              https://raw.githubusercontent.com/georgepauna/Pewpew/master/pewpew_launcher.py
         chmod +x ~/pewpew_launcher.py

  3. Add it as a non-Steam game:
       a. Open **Steam (Desktop)** → Library → Add a Game → Add a Non-Steam Game.
       b. Browse for a placeholder (e.g. Konsole) so the dialog accepts something,
          then press OK.
       c. Right-click the new entry → Properties.
       d. Set **Target** to:        /usr/bin/python3
       e. Set **Launch options** to: "/home/deck/pewpew_launcher.py"   (with quotes)
       f. Set **Start in** to:      /home/deck/
       g. (Optional) Set a name like "Pewpew" and a custom icon.

  4. Back in Game Mode (Steam → Power → Return to Gaming Mode), Pewpew
     appears in your library. The first launch clones the repo into
     ~/.local/share/pewpew/repo; subsequent launches run the cached copy.
     Updates land via the in-game "UPDATE AVAILABLE" overlay — press
     ability on the title to install whichever release is current.

Logs are written to ~/.local/share/pewpew/launcher.log — check there if the
game doesn't appear.

────────────────────────────────────────────────────────────────────────────
"""

import os
import sys
import subprocess
import time
import traceback
from pathlib import Path

REPO_URL = "https://github.com/georgepauna/Pewpew.git"
BRANCH = "master"
CACHE_DIR = Path.home() / ".local" / "share" / "pewpew"
REPO_DIR = CACHE_DIR / "repo"
VENV_DIR = CACHE_DIR / "venv"
LOG_FILE = CACHE_DIR / "launcher.log"
ENTRY = "pewpew.py"

# Network timeouts — long enough for slow Wi-Fi, short enough that a missing
# router doesn't lock the Deck on a black screen. Only the clone path uses
# network now; the launcher no longer fetches on subsequent runs.
CLONE_TIMEOUT = 120
PIP_TIMEOUT = 240


def log(msg):
    """Emit to stdout (visible if someone runs from Konsole) and append to
    the persistent launcher log."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd, **kwargs):
    """Subprocess wrapper that always logs the command before it runs."""
    pretty = " ".join(cmd) if isinstance(cmd, (list, tuple)) else cmd
    log(f"$ {pretty}")
    return subprocess.run(cmd, **kwargs)


def ensure_repo():
    """Clone on first run; on every later run, just verify the cached
    repo still has pewpew.py and return. Subsequent updates are owned
    by pewpew.py's in-game opt-in updater (the "UPDATE AVAILABLE"
    overlay), NOT this launcher — the launcher used to do
    `git fetch + reset --hard origin/master` here, which silently
    bypassed the channel system and the user prompt."""
    if REPO_DIR.exists() and (REPO_DIR / ".git").is_dir():
        return (REPO_DIR / ENTRY).is_file()
    # First run — clone.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        r = run(
            ["git", "clone", "--depth=1", "--branch", BRANCH,
             REPO_URL, str(REPO_DIR)],
            timeout=CLONE_TIMEOUT, capture_output=True, text=True,
        )
        if r.returncode != 0:
            log(f"Clone failed (rc={r.returncode}): "
                f"{r.stderr.strip()!r}")
            return False
        log("Cloned repo.")
    except FileNotFoundError:
        log("git not found on PATH — install git first.")
        return False
    except subprocess.TimeoutExpired:
        log("Clone timed out.")
        return False
    except Exception as e:
        log(f"Clone exception: {e!r}")
        return False
    return (REPO_DIR / ENTRY).is_file()


def _venv_python():
    """Path to the venv's interpreter. Linux/macOS first, Windows fallback."""
    for rel in ("bin/python3", "bin/python",
                "Scripts/python.exe", "Scripts/python3.exe"):
        p = VENV_DIR / rel
        if p.exists():
            return p
    return None


def ensure_pygame():
    """Resolve a Python executable that has pygame importable.

    Order of attempts:
      1. Current interpreter — if `import pygame` already works, use it.
      2. The launcher's private venv at CACHE_DIR/venv — create it if
         missing, install pygame inside it. SteamOS's read-only base +
         multi-arch lib paths make `pip install --user` flaky (see
         "wrong ELF class" failures), but a self-contained venv ships
         pygame's own SDL2 and bypasses every system-level conflict.

    Returns the path to a Python executable to launch pewpew.py with,
    or None if both attempts failed."""
    try:
        import pygame  # noqa: F401
        return sys.executable
    except ImportError:
        pass

    # Try the venv path.
    if not VENV_DIR.is_dir() or _venv_python() is None:
        log(f"Creating venv at {VENV_DIR}")
        try:
            VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
            r = run(
                [sys.executable, "-m", "venv", str(VENV_DIR)],
                timeout=120, capture_output=True, text=True,
            )
            if r.returncode != 0:
                log(f"venv creation failed (rc={r.returncode}): "
                    f"{r.stderr.strip()!r}")
                return None
        except Exception as e:
            log(f"venv exception: {e!r}")
            return None

    py = _venv_python()
    if py is None:
        log("Venv created but no python interpreter found inside it.")
        return None

    # Is pygame already present in the venv?
    r = run([str(py), "-c", "import pygame, sys; print(pygame.ver)"],
            timeout=15, capture_output=True, text=True)
    if r.returncode == 0:
        log(f"pygame {r.stdout.strip()} already present in venv.")
        return str(py)

    # Install it.
    log("Installing pygame into venv (pip will pull a prebuilt wheel + SDL2).")
    try:
        r = run(
            [str(py), "-m", "pip", "install",
             "--disable-pip-version-check", "pygame"],
            timeout=PIP_TIMEOUT, capture_output=True, text=True,
        )
        if r.returncode != 0:
            log(f"pip install pygame failed (rc={r.returncode}): "
                f"{r.stderr.strip()!r}")
            return None
    except Exception as e:
        log(f"pip install exception: {e!r}")
        return None

    # Confirm it imports.
    r = run([str(py), "-c", "import pygame; print('pygame', pygame.ver)"],
            timeout=15, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"pygame still not importable in venv: {r.stderr.strip()!r}")
        return None
    log(r.stdout.strip())
    return str(py)


def launch_game(python_exe):
    """Replace this process with `<python_exe> pewpew.py` running inside the
    repo so save.json + screenshots land alongside the source. Steam sees
    the game's exit code rather than the launcher's."""
    entry = REPO_DIR / ENTRY
    log(f"Launching {entry} via {python_exe}")
    os.chdir(REPO_DIR)
    # Forward any extra CLI args (e.g. --windowed) the user passed through
    # Steam's "Launch Options".
    argv = [python_exe, str(entry)] + sys.argv[1:]
    try:
        os.execv(python_exe, argv)
    except OSError as e:
        log(f"execv failed ({e!r}); falling back to subprocess.")
        return subprocess.call(argv)


def main():
    log("=== Pewpew launcher start ===")
    log(f"Python {sys.version.split()[0]} at {sys.executable}")
    log(f"Cache dir: {CACHE_DIR}")
    if not ensure_repo():
        log("FATAL: no usable Pewpew repo to run.")
        return 1
    py = ensure_pygame()
    if py is None:
        log("Continuing with system python — pewpew.py may fail on import.")
        py = sys.executable
    return launch_game(py) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        log("FATAL launcher exception:")
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        traceback.print_exc()
        sys.exit(1)
