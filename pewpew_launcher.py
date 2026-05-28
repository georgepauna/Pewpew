#!/usr/bin/env python3
"""Pewpew auto-updating launcher.

One-file launcher: clones github.com/georgepauna/Pewpew on first run, pulls
the latest master every subsequent launch, ensures pygame is importable,
then execs pewpew.py. Falls back to the last cached copy when there's no
network so you can still play offline.

────────────────────────────────────────────────────────────────────────────
Steam Deck install (Game Mode launches always-latest Pewpew)
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

  4. Back in Game Mode (Steam → Power → Return to Gaming Mode), Pewpew appears
     in your library. Launching it auto-updates and runs the latest commit
     on master.

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
LOG_FILE = CACHE_DIR / "launcher.log"
ENTRY = "pewpew.py"

# Network timeouts — long enough for slow Wi-Fi, short enough that a missing
# router doesn't lock the Deck on a black screen.
CLONE_TIMEOUT = 120
FETCH_TIMEOUT = 25
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
    """Clone on first run, fast-forward to origin/<BRANCH> on every later run.
    A failure (no network, GitHub down, etc.) is non-fatal as long as a
    cached copy with pewpew.py exists — we just play offline."""
    if REPO_DIR.exists() and (REPO_DIR / ".git").is_dir():
        # Repo is present; try to update it.
        try:
            r = run(
                ["git", "-C", str(REPO_DIR), "fetch", "--depth=1",
                 "origin", BRANCH],
                timeout=FETCH_TIMEOUT, capture_output=True, text=True,
            )
            if r.returncode == 0:
                run(
                    ["git", "-C", str(REPO_DIR), "reset", "--hard",
                     f"origin/{BRANCH}"],
                    timeout=15, capture_output=True, text=True,
                )
                log(f"Updated to latest origin/{BRANCH}.")
            else:
                log(f"Fetch failed (rc={r.returncode}); using cached copy. "
                    f"stderr={r.stderr.strip()!r}")
        except subprocess.TimeoutExpired:
            log("Fetch timed out; using cached copy.")
        except Exception as e:
            log(f"Update failed ({e!r}); using cached copy.")
    else:
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


def ensure_pygame():
    """Try to import pygame. If that fails, install it into the user
    site-packages dir via pip and try again. Returns True on success."""
    try:
        import pygame  # noqa: F401
        return True
    except ImportError:
        pass
    log("pygame not importable; running pip install --user pygame.")
    try:
        r = run(
            [sys.executable, "-m", "pip", "install", "--user",
             "--disable-pip-version-check", "pygame"],
            timeout=PIP_TIMEOUT, capture_output=True, text=True,
        )
        if r.returncode != 0:
            log(f"pip install failed (rc={r.returncode}): "
                f"{r.stderr.strip()!r}")
            return False
    except Exception as e:
        log(f"pip install exception: {e!r}")
        return False
    # Pip writes to ~/.local/lib/pythonX.Y/site-packages — that path is on
    # the import path automatically, but a second-pass import in this
    # process can miss it depending on site init order. Try once more.
    try:
        import importlib
        import site
        importlib.reload(site)
        import pygame  # noqa: F401
        log("pygame imported after install.")
        return True
    except ImportError as e:
        log(f"pygame still not importable after install: {e!r}")
        return False


def launch_game():
    """Replace this process with `python3 pewpew.py` running inside the
    repo so save.json + screenshots land alongside the source. Steam sees
    the game's exit code rather than the launcher's."""
    entry = REPO_DIR / ENTRY
    log(f"Launching {entry}")
    os.chdir(REPO_DIR)
    # Forward any extra CLI args (e.g. --windowed) the user passed through
    # Steam's "Launch Options".
    argv = [sys.executable, str(entry)] + sys.argv[1:]
    try:
        os.execv(sys.executable, argv)
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
    if not ensure_pygame():
        log("Continuing without confirmed pygame — pewpew.py may fail on import.")
    return launch_game() or 0


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
