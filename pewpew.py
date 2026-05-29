#!/usr/bin/env python3
"""Pewpew - a Tyrian-style vertical shooter for the RG35XX Pro.

Single-file, no external assets. Sprites and sounds are generated in code.
Branching mission map, weapon upgrades, abilities, varied enemies.
"""

import array
import hashlib
import json
import math
import os
import random
import struct
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path


def _parse_bot_cli():
    """Parse --bot / --replay / --headless / --seed / --out / --runs from sys.argv
    before pygame is imported, so we can set dummy SDL drivers if needed.
    Returns a small dict; missing values are None."""
    # max_steps caps the number of level attempts per bot session. 500
    # leaves headroom for the new boss attempt cap (15) — worst case is
    # 9 bosses × 15 + 91 regular levels × 3 = 408 steps.
    out = {"bot": None, "replay": None, "headless": False,
           "seed": 1337, "out_dir": None, "runs": 1, "max_steps": 500,
           "levers": "", "retry_cap": None}
    for a in sys.argv[1:]:
        if a == "--headless":
            out["headless"] = True
        elif a.startswith("--bot="):
            out["bot"] = a.split("=", 1)[1]
            out["headless"] = True
        elif a.startswith("--replay="):
            out["replay"] = a.split("=", 1)[1]
        elif a.startswith("--seed="):
            out["seed"] = int(a.split("=", 1)[1])
        elif a.startswith("--out="):
            out["out_dir"] = a.split("=", 1)[1]
        elif a.startswith("--runs="):
            out["runs"] = int(a.split("=", 1)[1])
        elif a.startswith("--max-steps="):
            out["max_steps"] = int(a.split("=", 1)[1])
        elif a.startswith("--levers="):
            out["levers"] = a.split("=", 1)[1]
        elif a.startswith("--retry-cap="):
            # Force-override the per-level attempt cap (regular + boss).
            # Use a huge number like 999 to test "unlimited retries" with
            # the adaptive-difficulty knob fully exercising.
            out["retry_cap"] = int(a.split("=", 1)[1])
    return out


BOT_CLI = _parse_bot_cli()

os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
if BOT_CLI["headless"]:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

# Declare the process DPI-aware on Windows BEFORE importing pygame so SDL
# sees the real desktop pixel grid (not a DPI-scaled approximation). Without
# this, on a 1920x1080 monitor at 150% scaling Windows reports the desktop
# as 1280x720, we pick a 1× window, then Windows bilinear-upscales the
# whole pygame window — pixel-art looks fuzzy. Process-wide DPI awareness
# fixes it: pygame.display.get_desktop_sizes() returns 1920x1080, _present()
# picks 2× scale, the window draws at native resolution with no compositor
# resampling. No-op on non-Windows platforms.
if sys.platform == "win32":
    try:
        import ctypes
        # PROCESS_PER_MONITOR_DPI_AWARE (2) is the modern API; fall back to
        # the legacy SetProcessDPIAware on older Windows.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import pygame


# =============================================================================
# CONSTANTS
# =============================================================================

# ──────────────────────────────────────────────────────────────────────────
# !!! IMPORTANT: BUMP `VERSION` ON EVERY PUSH !!!
# The string below is shown on the title screen so a player (and the dev
# launching from Game Mode on a Steam Deck) can confirm at a glance that the
# auto-update actually pulled a newer build. Every git push to master must
# include a VERSION bump — patch number for fixes / tweaks, minor for new
# features, major for big-rewrites. Skipping the bump means the next user
# sees the same number and can't tell if they're on the latest build.
# ──────────────────────────────────────────────────────────────────────────
VERSION = "0.8.5"

# ──────────────────────────────────────────────────────────────────────────
# Auto-update — channel switch + GitHub release / master pull
# ──────────────────────────────────────────────────────────────────────────
# Updates are **opt-in**. Nothing runs at process start. The title screen
# fetches the changelog of releases newer than VERSION and shows an
# "UPDATE AVAILABLE" overlay; the player presses ability (silk X / Y) to
# trigger `_check_release_update(force=True)`, which hash-compares and
# replaces files, then `os.execv`s self.
#
# Channels:
#   • stable (default): GitHub releases/latest → tag → raw.github at that tag
#   • uat:               raw.github at master tip (UAT testers' channel)
# Channel is picked by `PEWPEW_CHANNEL` env or `.uat_channel` marker file.
# SELECT+ability on the title flips it; the title version stamp turns red
# when on UAT so the player can tell at a glance.
GITHUB_OWNER = "georgepauna"
GITHUB_REPO = "Pewpew"
AUTOUPDATE_FILES = (
    "pewpew.py",
    "launch.sh",
    "art/layout.json",
    "art/sprite_engine.json",
)


def _autoupdate_bundle_dir():
    """Directory pewpew.py lives in (i.e. the deployable bundle root)."""
    return Path(__file__).resolve().parent


def autoupdate_channel(bundle_dir=None):
    """Return 'uat' if the env var or marker file opts in, else 'stable'.
    Reading is cheap so callers refresh it whenever they need a fresh
    answer (e.g. after toggling the marker)."""
    env = os.environ.get("PEWPEW_CHANNEL", "").strip().lower()
    if env in ("stable", "uat"):
        return env
    if bundle_dir is None:
        bundle_dir = _autoupdate_bundle_dir()
    if (bundle_dir / ".uat_channel").exists():
        return "uat"
    return "stable"


def _autoupdate_fetch(url, timeout=5):
    """Bytes at url with a short timeout, or None on any failure. Silent —
    we always fall through to running the cached copy."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Pewpew-autoupdater"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _autoupdate_resolve_prefix(channel):
    """Per-channel raw-URL prefix: stable hits the API for the latest
    release tag, uat goes straight to master. Returns None on failure
    (skip this boot — cached copy runs)."""
    if channel == "uat":
        return (f"https://raw.githubusercontent.com/"
                f"{GITHUB_OWNER}/{GITHUB_REPO}/master")
    data = _autoupdate_fetch(
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        "/releases/latest")
    if data is None:
        return None
    try:
        tag = json.loads(data).get("tag_name")
    except Exception:
        return None
    if not tag:
        return None
    return (f"https://raw.githubusercontent.com/"
            f"{GITHUB_OWNER}/{GITHUB_REPO}/{tag}")


def _check_release_update(force=False):
    """Pull updates from the active channel. Hash-compares each managed
    file and replaces (atomic tempfile rename) on diff. If anything
    changed, re-execs self so the new code runs this boot.

    Gates (skip silently when any apply):
    • Windows (dev box) unless PEWPEW_AUTOUPDATE explicitly =1 — local
      edits would otherwise be clobbered.
    • PEWPEW_AUTOUPDATE=0 in the environment.
    • .no_autoupdate marker file next to pewpew.py.

    `force=True` (used by the title channel-switch) bypasses the Windows
    gate so the user can switch + reload from the dev box."""
    bundle_dir = _autoupdate_bundle_dir()
    if os.environ.get("PEWPEW_AUTOUPDATE", "1") == "0":
        return
    if (bundle_dir / ".no_autoupdate").exists():
        return
    if (sys.platform == "win32" and not force
            and "PEWPEW_AUTOUPDATE" not in os.environ):
        return
    channel = autoupdate_channel(bundle_dir)
    prefix = _autoupdate_resolve_prefix(channel)
    if prefix is None:
        return
    changed = False
    for rel in AUTOUPDATE_FILES:
        target = bundle_dir / rel
        if not target.parent.exists():
            continue
        data = _autoupdate_fetch(f"{prefix}/{rel}")
        if not data:
            continue
        try:
            old = target.read_bytes() if target.exists() else b""
        except Exception:
            old = b""
        if hashlib.sha256(data).digest() == hashlib.sha256(old).digest():
            continue
        tmp = target.with_suffix(target.suffix + ".update")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
            changed = True
        except Exception:
            try: tmp.unlink()
            except Exception: pass
    if changed:
        try:
            os.execv(sys.executable,
                     [sys.executable, str(bundle_dir / "pewpew.py")]
                     + sys.argv[1:])
        except Exception:
            pass  # fall through; new files apply on next boot


def autoupdate_check_available(timeout=5):
    """Quick remote-vs-local hash check across every managed file. Returns
    True iff at least one of them differs from the active channel's
    source — i.e. pressing the manual-update button would actually pull
    something new. Used to gate the (X) hint next to the version stamp
    so it only flashes when there's something to do.

    Bypasses the PEWPEW_AUTOUPDATE / .no_autoupdate gates intentionally —
    the user opted out of *automatic* application, not out of "is there
    anything new?". A True here just means the hint is shown; the
    decision to apply is still theirs (press X) and still subject to the
    auto-update gates inside `_check_release_update(force=True)`.

    Silent on network / parse failure: returns False so a flaky link
    doesn't flicker the hint on and off."""
    bundle_dir = _autoupdate_bundle_dir()
    channel = autoupdate_channel(bundle_dir)
    prefix = _autoupdate_resolve_prefix(channel)
    if prefix is None:
        return False
    for rel in AUTOUPDATE_FILES:
        target = bundle_dir / rel
        if not target.parent.exists():
            continue
        data = _autoupdate_fetch(f"{prefix}/{rel}", timeout=timeout)
        if not data:
            continue
        try:
            old = target.read_bytes() if target.exists() else b""
        except Exception:
            old = b""
        if hashlib.sha256(data).digest() != hashlib.sha256(old).digest():
            return True
    return False


def autoupdate_set_channel(channel):
    """Persist the channel choice via the .uat_channel marker file.
    channel ∈ ('stable', 'uat'). Returns the channel actually written
    (resolved through autoupdate_channel so env-var overrides win)."""
    bundle_dir = _autoupdate_bundle_dir()
    marker = bundle_dir / ".uat_channel"
    try:
        if channel == "uat":
            marker.touch()
        else:
            if marker.exists():
                marker.unlink()
    except Exception:
        pass
    return autoupdate_channel(bundle_dir)


def _parse_semver_tag(tag):
    """Parse a 'vX.Y.Z' or 'X.Y.Z' tag into a (major, minor, patch) tuple.
    Returns None on anything that doesn't fit so unparseable tags can be
    filtered out cleanly. Non-numeric suffixes (e.g. '-rc1') are dropped."""
    if not tag:
        return None
    s = tag.lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = s.split(".")
    if len(parts) < 1:
        return None
    out = []
    for p in parts[:3]:
        try:
            out.append(int(p))
        except ValueError:
            return None
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def fetch_release_notes_since(last_seen_version, timeout=5):
    """Fetch release bodies for every GitHub release strictly newer than
    `last_seen_version`, newest-first, and concatenate them into one
    block of text suitable for the title-screen overlay.

    Returns "" on any network / parse failure so callers can skip the
    overlay without special-casing — empty notes = nothing to show."""
    data = _autoupdate_fetch(
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
        "/releases?per_page=20",
        timeout=timeout)
    if data is None:
        return ""
    try:
        releases = json.loads(data)
    except Exception:
        return ""
    if not isinstance(releases, list):
        return ""
    last = _parse_semver_tag(last_seen_version)
    out = []
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = rel.get("tag_name", "")
        parsed = _parse_semver_tag(tag)
        # Skip unparseable tags so a stray legacy / hand-tagged release
        # can't poison the cutoff. If the player has no last_seen yet
        # (fresh install), include only the single newest release so
        # we don't dump the whole repo history on them.
        if parsed is None:
            continue
        if last is not None and parsed <= last:
            continue
        title = rel.get("name") or tag
        body = (rel.get("body") or "").strip()
        block = f"=== {title} ===\n"
        if body:
            block += body + "\n"
        else:
            block += "(no notes for this release)\n"
        out.append(block)
        if last is None:
            break  # fresh install: just the latest one
    return "\n".join(out).strip()

SCREEN_W, SCREEN_H = 640, 480
PLAY_W = 480
PLAY_H = 480

# Present-mode cycle for the windowed path (TAB / Y on the title screen).
# 4-step pipeline:
#   1. Always integer-scale the 640x480 screen up by the largest int that
#      fits in the host window (1, 2, 3, ...).
#   2. If the mode includes "grid", multiply a 1-px-per-cell dim-mask onto
#      the integer-scaled surface — perfectly aligned to source pixels.
#   3. If the mode includes "fill", nearest-neighbour rescale up to the
#      largest aspect-preserving fractional fit. Otherwise stop after step
#      1/2 and let the centred-with-bands blit run as-is.
# So: integer (1), scaled-grid (1+2), fill (1+3), fill-grid (1+2+3).
SCALE_MODES = ("integer", "scaled-grid", "fill", "fill-grid")
# Pixel margin added to each side of the playfield surface so the bg_ribbon
# extends past PLAY_W. The screen blit can then slide by that much in either
# direction (parallax + shake) and the wider bg covers the trailing edge —
# we don't need to fill the screen with black before each frame.
PLAY_MARGIN = 48
HUD_X = PLAY_W
HUD_W = SCREEN_W - PLAY_W
FPS = 60

# Uniform 1.5x size multiplier for every play-area sprite: ships, enemies,
# bullets, obstacles, pickups, engine flames. Bullet velocities + player
# speed stay constant — only visual + collision footprints grow.
PLAY_SCALE = 1.5

# Disable the nebula layer entirely (still instantiated so we can re-enable
# without rewiring anything). Each frame the nebula was a 480x960 SRCALPHA
# blit drawn twice for the scroll wrap — that's ~5 ms/frame on the
# RG35XX Pro mali driver, the second-biggest CPU cost after the stars.
ENABLE_NEBULA = False

# Far-off-screen rect used to neutralize already-dead enemies inside a
# Rect.collidelist sweep without rebuilding the rect list. Sized 1×1 so
# it doesn't overlap anything that might briefly visit the negative
# coordinate range.
_DEAD_RECT_SENTINEL = pygame.Rect(-99999, -99999, 1, 1)


def _ps(v):
    """Scale a small integer dimension by PLAY_SCALE, rounded to >= 1."""
    return max(1, int(round(v * PLAY_SCALE)))

SAVE_PATH = Path(os.environ.get("PEWPEW_SAVE", str(Path(__file__).resolve().parent / "save.json")))

JOY_A = 0
JOY_B = 1
JOY_X = 2
JOY_Y = 3
JOY_L1 = 4
JOY_R1 = 5
JOY_SELECT = 6
JOY_START = 7
JOY_L3 = 9        # left stick click (RG35XX Pro index)
JOY_L2 = 10       # left shoulder trigger as a digital button
JOY_R2 = 11       # right shoulder trigger as a digital button
JOY_R3 = 12       # right stick click
JOY_MENU = 13     # device home/menu button — quits the device

# Face-button schemes — keep the action ↔ physical-position binding the
# same on every platform (south=fire, east=bomb, west=ability, north=cancel)
# while letting the displayed silk letter follow each controller's labelling.
#
# RG35XX Pro silk-labels its face buttons Nintendo-style:
#       X            (Xbox would call this Y)
#    Y     A         (Xbox: X · A)
#       B            (Xbox: A)
# The Anbernic SDL reports indices that follow the silk: south = idx 1 = silk
# "B", east = idx 0 = silk "A", etc. — opposite of the SDL GameController
# convention used on a PC + Xbox controller (south = idx 0 = silk "A").
#
# Format per action: (button_index, displayed_silk_letter)
_DEVICE_BUTTON_SCHEME = {
    "fire":    (JOY_B, "B"),   # south = silk B on RG, idx 1
    "bomb":    (JOY_A, "A"),   # east  = silk A on RG, idx 0
    "ability": (JOY_X, "Y"),   # west  = silk Y on RG, idx 2
    "cancel":  (JOY_Y, "X"),   # north = silk X on RG, idx 3
}
_PC_BUTTON_SCHEME = {
    "fire":    (JOY_A, "A"),   # south = silk A on Xbox, idx 0
    "bomb":    (JOY_B, "B"),   # east  = silk B on Xbox, idx 1
    "ability": (JOY_X, "X"),   # west  = silk X on Xbox, idx 2
    "cancel":  (JOY_Y, "Y"),   # north = silk Y on Xbox, idx 3
}
# Module-level active scheme — App.__init__ swaps it in based on the
# device / PC detection. Defaults to PC so anything that touches the
# scheme before App is constructed (editor previews etc.) still works.
BUTTON_SCHEME = _PC_BUTTON_SCHEME


def set_button_scheme(on_device):
    """Switch the module-level scheme. Called once from App.__init__."""
    global BUTTON_SCHEME
    BUTTON_SCHEME = _DEVICE_BUTTON_SCHEME if on_device else _PC_BUTTON_SCHEME


def button_label_vars():
    """Template-var dict for layout {btn_fire} / {btn_bomb} / {btn_ability} /
    {btn_cancel} placeholders. Merge into any chrome / dynamic var dict."""
    return {
        "btn_fire":    BUTTON_SCHEME["fire"][1],
        "btn_bomb":    BUTTON_SCHEME["bomb"][1],
        "btn_ability": BUTTON_SCHEME["ability"][1],
        "btn_cancel":  BUTTON_SCHEME["cancel"][1],
    }


class _SafeFormatDict(dict):
    """dict subclass used with str.format_map so unknown {placeholders} are
    preserved verbatim instead of raising KeyError. Lets one substitution
    pass leave handlers further down the pipeline (e.g. {dpad} consumed by
    _draw_text_with_dpad) intact."""
    def __missing__(self, key):
        return "{" + key + "}"


def _safe_format(text, template_vars):
    """Substitute known {name} placeholders from template_vars and leave any
    other {name} in place. Returns text unchanged on any other format error."""
    try:
        return text.format_map(_SafeFormatDict(template_vars))
    except (IndexError, ValueError):
        return text


BLACK = (0, 0, 0)
WHITE = (240, 240, 240)
DIM = (140, 140, 160)
DARKER = (60, 64, 88)
CYAN = (80, 220, 255)
YELLOW = (255, 220, 80)
ORANGE = (255, 140, 40)
RED = (255, 70, 70)
GREEN = (90, 230, 120)
PURPLE = (200, 90, 220)
BLUE = (90, 130, 230)
HUD_BG = (15, 18, 32)
HUD_LINE = (40, 48, 80)

# =============================================================================
# WEAPONS — Tyrian-style: front weapons + sidekicks, each with selectable TYPE.
# Player can OWN multiple weapon types independently (each with its own level)
# and EQUIPS one main + one sidekick at a time.
# =============================================================================
MAIN_WEAPONS = ("pulse", "spread", "vulcan")
SIDE_WEAPONS = ("missile", "drone")  # "none" is also valid for side_type

MAIN_WEAPON_NAMES = {
    "pulse":  "Pulse Cannon",
    "spread": "Spread Shot",
    "vulcan": "Vulcan Gun",
}
SIDE_WEAPON_NAMES = {
    "none":    "(none)",
    "missile": "Heatseekers",
    "drone":   "Drone Cells",
}
MAIN_WEAPON_MAX = 20      # 5 tiers x 4 sub-levels (sub-levels = +damage only)
SIDE_WEAPON_MAX = 5       # 5 tiers, no sub-levels (one level per tier)
# First-purchase cost when the weapon is not yet owned (level 0 → level 1).
MAIN_BUY_COST = 3000
SIDE_BUY_COST = 1600
# Main weapons: damage scales per sub-level via 100 + 10*(level-1).
# Side weapons / shield / engine: one level per tier (no sub-levels) —
# stats step per tier. Damage on side weapons is flat across tiers.
_MAIN_COSTS = [
    0,
    300, 300, 300, 1500,        # T1 subs + T1->T2 jump (cost[1..4])
    600, 600, 600, 3000,        # T2 subs + T2->T3 jump
    1200, 1200, 1200, 6000,     # T3 subs + T3->T4 jump
    2100, 2100, 2100, 9000,     # T4 subs + T4->T5 jump
    3000, 3000, 3000,           # T5 subs (no jump out of T5)
]
MAIN_UPGRADE_COSTS = {
    "pulse":  list(_MAIN_COSTS),
    "spread": list(_MAIN_COSTS),
    "vulcan": list(_MAIN_COSTS),
}
# Side weapons: 5 levels (one per tier). cost[i] = cost to go from L=i
# to L=i+1 (i.e. tier-jump cost).
SIDE_UPGRADE_COSTS = {
    "missile": [0, 600, 1400, 2800, 5000],
    "drone":   [0, 600, 1500, 3000, 5500],
}

# Equipment that is just leveled (no type selection). Each level == one tier.
WEAPON_COSTS = {
    "shield": [0, 700, 1600, 3000, 4800],            # max level 5 (unchanged)
    "engine": [0, 1000, 2400, 5000, 10000],          # max level 5 (was 3)
}
MAX_LEVELS = {"shield": 5, "engine": 5}
BOMB_PRICE = 500

ABILITIES = ["screen_clear", "shield_burst", "mega_laser"]
ABILITY_NAMES = {
    "screen_clear": "Pulse Bomb",
    "shield_burst": "Shield Burst",
    "mega_laser":   "Mega Laser",
}


# =============================================================================
# UTILITIES
# =============================================================================

def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


def sign(v):
    return -1 if v < 0 else 1 if v > 0 else 0


class PerfMonitor:
    """Per-section frame-time accumulator with EWMA smoothing.

    Pattern:
        perf.start("draw.stars"); ... ; perf.end("draw.stars")
    Or:
        with perf.measure("draw.stars"):
            ...
    Call ``frame_end()`` once per frame to roll the current frame's totals
    into the smoothed table. Each smoothed entry tracks one EWMA of
    seconds-per-frame and a peak value across the recent window. Cost of
    start()/end() is two time.perf_counter() calls plus a dict lookup
    (~300 ns on the RG35XX Pro), so the monitor can stay always-on."""

    __slots__ = ("alpha", "current", "smoothed", "peak", "_t",
                 "_order", "peak_decay", "frame_count")

    def __init__(self, alpha=0.08, peak_decay=0.995):
        self.alpha = alpha
        self.peak_decay = peak_decay
        self.current = {}
        self.smoothed = {}
        self.peak = {}
        self._t = {}
        self._order = []
        self.frame_count = 0

    def start(self, name):
        self._t[name] = time.perf_counter()

    def end(self, name):
        t0 = self._t.pop(name, None)
        if t0 is None:
            return
        dt = time.perf_counter() - t0
        cur = self.current
        if name in cur:
            cur[name] += dt
        else:
            cur[name] = dt
            if name not in self.smoothed:
                self._order.append(name)

    def measure(self, name):
        return _PerfSpan(self, name)

    def frame_end(self):
        a = self.alpha
        ia = 1.0 - a
        decay = self.peak_decay
        cur = self.current
        sm = self.smoothed
        pk = self.peak
        for name, v in cur.items():
            sm[name] = sm[name] * ia + v * a if name in sm else v
            pk[name] = max(pk.get(name, 0.0), v)
        # Sections that didn't fire this frame: decay smoothed toward zero
        # so transient peaks fade and stale entries die. Peak decays slowly
        # too so big spikes remain visible for a few seconds.
        for name in self._order:
            if name not in cur:
                if name in sm:
                    sm[name] *= ia
                if name in pk:
                    pk[name] *= decay
        cur.clear()
        self.frame_count += 1

    def ms(self, name):
        return self.smoothed.get(name, 0.0) * 1000.0

    def peak_ms(self, name):
        return self.peak.get(name, 0.0) * 1000.0

    def order(self):
        return tuple(self._order)


class _PerfSpan:
    __slots__ = ("monitor", "name")

    def __init__(self, monitor, name):
        self.monitor = monitor
        self.name = name

    def __enter__(self):
        self.monitor.start(self.name)
        return self

    def __exit__(self, *exc):
        self.monitor.end(self.name)
        return False


def from_grid(grid, palette):
    """Build a SRCALPHA Surface from a list of strings + char->color map.
    Tolerates jagged rows (short rows are padded with transparent pixels)."""
    h = len(grid)
    w = max(len(r) for r in grid) if grid else 1
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            color = palette.get(ch)
            if color is not None:
                surf.set_at((x, y), color)
    return surf


def tone(freq, dur, vol=0.25, square=False, sweep=0.0):
    try:
        sr = 22050
        n = int(sr * dur)
        buf = array.array("h")
        amp = int(32767 * vol)
        for i in range(n):
            t = i / sr
            f = freq + sweep * t
            if i < sr * 0.005:
                env = i / (sr * 0.005)
            elif i > n - sr * 0.03:
                env = max(0.0, (n - i) / (sr * 0.03))
            else:
                env = 1.0
            if square:
                v = amp if (t * f) % 1.0 < 0.5 else -amp
            else:
                v = int(math.sin(2 * math.pi * f * t) * amp)
            buf.append(int(v * env))
        return pygame.mixer.Sound(buffer=buf.tobytes())
    except Exception:
        return _Silent()


def noise(dur, vol=0.3, lp=1.0):
    try:
        sr = 22050
        n = int(sr * dur)
        buf = array.array("h")
        amp = int(32767 * vol)
        prev = 0.0
        for i in range(n):
            env = max(0.0, 1 - i / n)
            sample = random.uniform(-1, 1)
            prev = prev * (1 - lp) + sample * lp
            buf.append(int(prev * amp * env))
        return pygame.mixer.Sound(buffer=buf.tobytes())
    except Exception:
        return _Silent()


class _Silent:
    def play(self, *a, **kw): pass
    def stop(self): pass


class RandomBank:
    """Picks one of several pre-rendered variants at random on each play.
    Used for rapid-fire sounds (player gun, side weapon) so they stop
    feeling like a single repeated beep. Implements the same `play /
    set_volume / stop` contract that pygame.mixer.Sound does, so it slots
    straight into the App's self.sounds dict without touching call sites."""

    def __init__(self, variants):
        self.variants = list(variants)
        self._last = -1

    def play(self, *a, **kw):
        n = len(self.variants)
        if n == 0:
            return
        idx = random.randint(0, n - 1)
        # Avoid playing the same variant twice in a row when we have choices.
        if n > 1 and idx == self._last:
            idx = (idx + 1) % n
        self._last = idx
        try:
            self.variants[idx].play(*a, **kw)
        except Exception:
            pass

    def set_volume(self, v):
        for s in self.variants:
            try: s.set_volume(v)
            except Exception: pass

    def stop(self):
        for s in self.variants:
            try: s.stop()
            except Exception: pass


class VolumeInput:
    """Polls the hardware volume keys non-blockingly and reports +1/-1 events.
    Doesn't hold any volume state - the caller decides what to do with each
    event (e.g. route SFX or music depending on a modifier key)."""
    DEVICE_CANDIDATES = ("/dev/input/event1", "/dev/input/event0", "/dev/input/event2")
    KEY_VOLUMEUP = 115
    KEY_VOLUMEDOWN = 114
    EV_KEY = 1
    EV_FMT = "llHHi"
    EV_SIZE = struct.calcsize(EV_FMT)

    def __init__(self):
        self.fds = []
        if sys.platform.startswith("linux"):
            for path in self.DEVICE_CANDIDATES:
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                except OSError:
                    continue
                self.fds.append(fd)

    def poll(self):
        events = []
        for fd in self.fds:
            while True:
                try:
                    buf = os.read(fd, self.EV_SIZE * 64)
                except (BlockingIOError, OSError):
                    buf = b""
                if not buf:
                    break
                for i in range(0, len(buf), self.EV_SIZE):
                    chunk = buf[i:i + self.EV_SIZE]
                    if len(chunk) < self.EV_SIZE:
                        break
                    _, _, etype, code, value = struct.unpack(self.EV_FMT, chunk)
                    if etype != self.EV_KEY or value != 1:
                        continue
                    if code == self.KEY_VOLUMEUP:
                        events.append(+1)
                    elif code == self.KEY_VOLUMEDOWN:
                        events.append(-1)
        return events

    def close(self):
        for fd in self.fds:
            try: os.close(fd)
            except OSError: pass
        self.fds = []


class AudioBus:
    """Holds a 0..1 slider level and exposes a power-curve gain. The level is
    what the user sees on the on-screen indicator; gain is what gets fed to
    pygame for actual amplitude scaling. The cube curve turns ten linear
    clicks into roughly evenly-spaced perceived loudness steps."""
    STEP = 0.1
    GAIN_EXP = 3.0

    def __init__(self, level=0.6, label="VOL"):
        self.level = clamp(float(level), 0.0, 1.0)
        self.label = label

    def adjust(self, direction):
        """direction is +1 or -1. Returns True if the level actually changed."""
        old = self.level
        self.level = clamp(self.level + direction * self.STEP, 0.0, 1.0)
        return self.level != old

    @property
    def gain(self):
        return self.level ** self.GAIN_EXP


# =============================================================================
# SPRITES
# =============================================================================

SHIP_PAL = {
    "#": (8, 12, 30),
    "b": (40, 70, 140),
    "c": (60, 130, 200),
    "C": (120, 200, 250),
    "w": (240, 250, 255),
    "y": (255, 220, 90),
    "o": (200, 70, 20),
    "O": (255, 200, 80),
}

PLAYER_GRID = [
    "##..........##",  # 0:  outer wing tops
    "##..........##",  # 1
    "#C#........#C#",  # 2:  highlight cap
    "#Cc........cC#",  # 3
    "#cc........cc#",  # 4
    "#cc........cc#",  # 5
    "#cc...##...cc#",  # 6:  central spine begins
    "#cc..#yy#..cc#",  # 7:  cockpit
    "#cc..#yy#..cc#",  # 8
    "#cc..####..cc#",  # 9
    "#cc...cc...cc#",  # 10
    "####..cc..####",  # 11: outer columns flare into engine bays
    "#oo#..cc..#oo#",  # 12
    "#OO#..oo..#OO#",  # 13: three engines lit
    "#oo#..OO..#oo#",  # 14
    "..##..##..##..",  # 15: three exhaust points (the W's feet)
]

# Bank frames simulate Y-axis (longitudinal) rotation: wings tilt toward/away
# from the camera. Both wings foreshorten (silhouette is narrower) but the
# RAISED wing is lit (bright C/c) while the DIPPED wing is shaded (dark b).
# Banking right: right wing dips down (shadow), left wing rises up (lit).
PLAYER_GRID_BANK_R = [
    ".##........##.",
    ".##........##.",
    ".#C#......#b#.",
    ".#Cc......bb#.",
    ".#cc......cb#.",
    ".#cc......cb#.",
    ".#cc..##..cb#.",
    ".#cc.#yy#.cb#.",
    ".#cc.#yy#.cb#.",
    ".#cc.####.cb#.",
    ".#cc..cc..cb#.",
    ".###..cc..###.",
    ".#o#..cc..#b#.",
    ".#O#..oo..#o#.",
    ".#o#..OO..#b#.",
    "..##..##..##..",
]

PLAYER_GRID_BANK_L = [
    ".##........##.",
    ".##........##.",
    ".#b#......#C#.",
    ".#bb......cC#.",
    ".#bc......cc#.",
    ".#bc......cc#.",
    ".#bc..##..cc#.",
    ".#bc.#yy#.cc#.",
    ".#bc.#yy#.cc#.",
    ".#bc.####.cc#.",
    ".#bc..cc..cc#.",
    ".###..cc..###.",
    ".#b#..cc..#o#.",
    ".#o#..oo..#O#.",
    ".#b#..OO..#o#.",
    "..##..##..##..",
]

SCOUT_PAL = {
    "#": (30, 8, 12),
    "r": (170, 40, 50),
    "R": (240, 80, 80),
    "y": (255, 220, 90),
    "w": (255, 255, 255),
}

SCOUT_GRID = [
    "....####....",
    "...#RRRR#...",
    "..#RRrrRR#..",
    ".#RRrwwrRR#.",
    "#RRrwyywrRR#",
    "#RRrwyywrRR#",
    ".#RrrwwrrR#.",
    ".#RRrrrrRR#.",
    "..#RR##RR#..",
    "...##..##...",
]

GUNNER_PAL = {
    "#": (30, 8, 30),
    "p": (130, 50, 150),
    "P": (200, 100, 220),
    "w": (255, 240, 255),
    "y": (255, 220, 90),
    "g": (60, 60, 80),
}

GUNNER_GRID = [
    "..############..",
    ".#PPPPPPPPPPPP#.",
    "#PPpPPPPPPPPpPP#",
    "#PPpPwwwwwwPpPP#",
    "#PPpPwyyyywPpPP#",
    "#PPpPwyyyywPpPP#",
    "#PPpPwwwwwwPpPP#",
    "#PPpPPPPPPPPpPP#",
    "#PPP########PPP#",
    ".#PPPPPPPPPPPP#.",
    "..#gg......gg#..",
    "...g........g...",
]

WEAVER_PAL = {
    "#": (10, 30, 20),
    "g": (60, 140, 80),
    "G": (120, 220, 130),
    "y": (255, 220, 90),
    "w": (255, 255, 255),
}

WEAVER_GRID = [
    "....######....",
    "...#GGGGGG#...",
    "..#GGggggGG#..",
    "#GGggwwwwggGG#",
    "#GggwwyywwggG#",
    "#GggwwyywwggG#",
    "#GGggwwwwggGG#",
    "..#GGggggGG#..",
    "...#GGGGGG#...",
    ".###......###.",
]

BOMBER_PAL = {
    "#": (8, 8, 8),
    "o": (160, 80, 30),
    "O": (240, 140, 50),
    "y": (255, 220, 90),
    "r": (220, 60, 60),
    "w": (255, 255, 255),
}

BOMBER_GRID = [
    "...##########...",
    "..#OOOOOOOOOO#..",
    ".#OOooooooooOO#.",
    "#OOoo########oOO",
    "#OOoo#wwwwww#oOO",
    "#OOoo#wyyyyw#oOO",
    "#OOoo#wyyyyw#oOO",
    "#OOoo#wwwwww#oOO",
    "#OOoo########oOO",
    ".#OOooooooooOO#.",
    "..#OOOOOOOOOO#..",
    "...##rr##rr##...",
    "....##....##....",
]

KAMI_PAL = {
    "#": (30, 14, 8),
    "o": (200, 80, 30),
    "y": (255, 220, 90),
    "Y": (255, 250, 180),
    "r": (240, 80, 60),
}

KAMI_GRID = [
    "......##......",
    ".....#yy#.....",
    "....#yYYy#....",
    "...#yYYYYy#...",
    "..#oyYYYYyo#..",
    ".#oooYYYYooo#.",
    "#ooorrYYrrooo#",
    "#ooorr##rrooo#",
    ".#oo#....#oo#.",
    "..#........#..",
]

TURRET_PAL = {
    "#": (8, 8, 18),
    "s": (60, 60, 90),
    "S": (140, 150, 180),
    "g": (100, 100, 100),
    "r": (220, 70, 70),
    "y": (255, 220, 90),
}

TURRET_GRID = [
    "..##############..",
    ".#SSSSSSSSSSSSSS#.",
    "#SSssssssssssssSS#",
    "#SssrrrrrrrrrrssS#",
    "#SssrSSSSSSSSrrsS#",
    "#SssrSyyyyyySrrsS#",
    "#SssrSyyyyyySrrsS#",
    "#SssrSSSSSSSSrrsS#",
    "#SssrrrrrrrrrrssS#",
    "#SSssssssssssssSS#",
    ".#SSSSSSSSSSSSSS#.",
    "..####gggggg####..",
    "....g##gggg##g....",
]

BOSS_PAL = {
    "#": (10, 8, 22),
    "r": (160, 40, 50),
    "R": (220, 70, 80),
    "p": (140, 50, 160),
    "P": (220, 120, 240),
    "y": (255, 220, 90),
    "Y": (255, 250, 150),
    "w": (255, 255, 255),
    "g": (80, 80, 90),
}

BOSS_GRID = [
    "......######################......",
    "....##RRRRRRRRRRRRRRRRRRRRRR##....",
    "...#RRRrrrrrrrrrrrrrrrrrrrrRRR#...",
    "..#RRrrPPPPPPPPPPPPPPPPPPPPrrRR#..",
    ".#RRrrPPpppppppppppppppppppPPrrR#.",
    "#RRrrPPppwwwwwwwwwwwwwwwwppPPrrRR#",
    "#RRrrPPppwwYYYYYYYYYYYYwwppPPrrRR#",
    "#RRrrPPppwYYyyyyyyyyyyYYwppPPrrRR#",
    "#RRrrPPppwYYyyyyyyyyyyYYwppPPrrRR#",
    "#RRrrPPppwwYYYYYYYYYYYYwwppPPrrRR#",
    "#RRrrPPppwwwwwwwwwwwwwwwwppPPrrRR#",
    "#RRrrPPpppppppppppppppppppPPrrRR#.",
    ".#RRrrPPPPPPPPPPPPPPPPPPPPrrRR#...",
    "..#RRrrrrrrrrrrrrrrrrrrrrrRR#.....",
    "..#gg#RRRRRRRRRRRRRRRRRRRR#gg#....",
    "..#gg##RRRRRRRRRRRRRRRRRR##gg#....",
    "..#g#..####RRRRRRRRRR####..#g#....",
    "..###......####RRRR####......###..",
]

POWERUP_PAL = {
    "#": (10, 14, 30),
    "G": (90, 230, 120),
    "Y": (255, 220, 90),
    "C": (80, 220, 255),
    "P": (220, 120, 240),
    "B": (255, 255, 255),
    ".": None,
}


def _frame(color, letter):
    s = pygame.Surface((14, 14), pygame.SRCALPHA)
    pygame.draw.rect(s, (10, 14, 30), (0, 0, 14, 14))
    pygame.draw.rect(s, color, (1, 1, 12, 12))
    pygame.draw.rect(s, (255, 255, 255), (1, 1, 12, 12), 1)
    f = BitmapFont(scale=1)
    txt = f.render(letter, False, BLACK)
    s.blit(txt, txt.get_rect(center=(7, 7)))
    return s


def _knock_out_dark_bg(surf, threshold=24):
    """In-place: set near-black pixels in `surf` to fully transparent. The AI
    sprite sheets are drawn on a solid black canvas, so the cropped cells
    arrive with opaque-black surroundings; without this the silhouette / hit
    flash turns each enemy into a solid white block."""
    surf.lock()
    try:
        w, h = surf.get_size()
        for y in range(h):
            for x in range(w):
                r, g, b, a = surf.get_at((x, y))
                if a > 0 and r <= threshold and g <= threshold and b <= threshold:
                    surf.set_at((x, y), (0, 0, 0, 0))
    finally:
        surf.unlock()


def make_silhouette(sprite, color=(255, 255, 255, 255)):
    """Return a same-size surface with every opaque pixel of `sprite` set to `color`."""
    try:
        mask = pygame.mask.from_surface(sprite)
        return mask.to_surface(setcolor=color, unsetcolor=(0, 0, 0, 0))
    except Exception:
        s = pygame.Surface(sprite.get_size(), pygame.SRCALPHA)
        s.blit(sprite, (0, 0))
        s.fill(color, special_flags=pygame.BLEND_RGBA_MULT)
        return s


def make_glow(sprite, color, radius=3, base_alpha=70):
    """Return a sprite-sized-plus-margin Surface with the sharp sprite on top of a colored halo."""
    w, h = sprite.get_size()
    out_w = w + radius * 2
    out_h = h + radius * 2
    out = pygame.Surface((out_w, out_h), pygame.SRCALPHA)
    silhouette = make_silhouette(sprite, color + (255,))
    for d in range(radius, 0, -1):
        a = max(0, base_alpha - d * 18)
        if a <= 0:
            continue
        s = silhouette.copy()
        s.set_alpha(a)
        for dx, dy in ((-d, 0), (d, 0), (0, -d), (0, d),
                       (-d, -d), (d, -d), (-d, d), (d, d)):
            out.blit(s, (radius + dx, radius + dy))
    out.blit(sprite, (radius, radius))
    return out


def _make_asteroid_surf(radius, base, dark, seed):
    rng = random.Random(seed)
    size = radius * 2 + 4
    s = pygame.Surface((size, size), pygame.SRCALPHA)
    cx, cy = size // 2, size // 2
    outer = []
    for i in range(10):
        ang = i * math.tau / 10 + rng.uniform(-0.1, 0.1)
        r = radius * rng.uniform(0.78, 1.0)
        outer.append((int(cx + math.cos(ang) * r), int(cy + math.sin(ang) * r)))
    pygame.draw.polygon(s, base, outer)
    pygame.draw.polygon(s, dark, outer, 1)
    # Inner shaded patch
    inner = []
    for i in range(7):
        ang = i * math.tau / 7 + 0.2
        r = radius * rng.uniform(0.35, 0.55)
        inner.append((int(cx + math.cos(ang) * r), int(cy + math.sin(ang) * r)))
    pygame.draw.polygon(s, dark, inner)
    # Craters
    for _ in range(rng.randint(2, 4)):
        cx2 = cx + rng.randint(-radius // 2, radius // 2)
        cy2 = cy + rng.randint(-radius // 2, radius // 2)
        cr = rng.randint(1, max(2, radius // 4))
        pygame.draw.circle(s, dark, (cx2, cy2), cr)
    # Bright spot for shape readability
    hl = max(1, radius // 4)
    pygame.draw.circle(s, (255, 240, 220), (cx - radius // 3, cy - radius // 3), hl)
    return s


def _make_mine_surf():
    s = pygame.Surface((22, 22), pygame.SRCALPHA)
    cx, cy = 11, 11
    r = 7
    hex_pts = [(int(cx + math.cos(math.tau * i / 6) * r),
                int(cy + math.sin(math.tau * i / 6) * r)) for i in range(6)]
    # Spike tips first (so the body covers their roots)
    for i in range(8):
        ang = math.tau * i / 8 + math.pi / 8
        tx = int(cx + math.cos(ang) * 10)
        ty = int(cy + math.sin(ang) * 10)
        pygame.draw.line(s, (60, 60, 75), (cx, cy), (tx, ty), 2)
    pygame.draw.polygon(s, (90, 90, 105), hex_pts)
    pygame.draw.polygon(s, (160, 160, 180), hex_pts, 1)
    pygame.draw.circle(s, (220, 70, 70), (cx, cy), 3)
    pygame.draw.circle(s, (255, 220, 220), (cx, cy), 1)
    return s


def _make_pylon_surf():
    w, h = 24, 56
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    pygame.draw.rect(s, (35, 40, 55), (0, 0, w, h))
    pygame.draw.rect(s, (90, 100, 130), (2, 4, w - 4, h - 8))
    pygame.draw.rect(s, (160, 180, 210), (2, 4, w - 4, 4))           # top highlight
    pygame.draw.rect(s, (50, 60, 80),    (w - 5, 4, 3, h - 8))       # right shadow
    pygame.draw.rect(s, (40, 50, 70),    (0, 0, w, 4))               # top cap
    pygame.draw.rect(s, (40, 50, 70),    (0, h - 4, w, 4))           # bottom cap
    for y in (15, 28, 41):
        pygame.draw.rect(s, (220, 200, 70), (4, y, w - 8, 4))
        pygame.draw.rect(s, (140, 110, 30), (4, y + 3, w - 8, 1))
    return s


def _make_wall_surf(width, height, sector_idx):
    """Hull-plate wall section keyed to a sector palette."""
    base, accent, dark = STATION_PALETTES[sector_idx % len(STATION_PALETTES)]
    s = pygame.Surface((width, height), pygame.SRCALPHA)
    pygame.draw.rect(s, dark, (0, 0, width, height))
    pygame.draw.rect(s, base, (2, 2, width - 4, height - 4))
    # Highlight along the top and left edges, shadow on bottom and right.
    pygame.draw.rect(s, accent, (2, 2, width - 4, 3))
    pygame.draw.rect(s, accent, (2, 2, 3, height - 4))
    pygame.draw.rect(s, dark, (2, height - 5, width - 4, 3))
    pygame.draw.rect(s, dark, (width - 5, 2, 3, height - 4))
    # Panel divisions across the height.
    for y in range(20, height - 12, 28):
        pygame.draw.line(s, dark,   (4, y),     (width - 4, y), 1)
        pygame.draw.line(s, accent, (4, y + 1), (width - 4, y + 1), 1)
    # Rivets along the inner column edges.
    for y in range(10, height - 6, 14):
        pygame.draw.rect(s, accent, (6, y, 2, 2))
        pygame.draw.rect(s, accent, (width - 8, y, 2, 2))
    # A single warning stripe per sector for variety.
    if sector_idx % 3 == 0:
        pygame.draw.rect(s, (220, 200, 70), (4, height // 2 - 3, width - 8, 6))
        pygame.draw.rect(s, dark, (4, height // 2 - 3, width - 8, 1))
    return s


def _make_crystal_surf():
    s = pygame.Surface((22, 28), pygame.SRCALPHA)
    pts = [(11, 1), (21, 13), (15, 27), (7, 27), (1, 13)]
    pygame.draw.polygon(s, (90, 220, 200), pts)
    pygame.draw.polygon(s, (200, 255, 240), pts, 1)
    # facet lines
    pygame.draw.line(s, (200, 255, 250), (11, 1), (11, 27), 1)
    pygame.draw.line(s, (60, 180, 160),  (11, 27), (1, 13), 1)
    pygame.draw.line(s, (60, 180, 160),  (11, 27), (21, 13), 1)
    # bright tip
    pygame.draw.circle(s, (255, 255, 255), (11, 5), 1)
    return s


def make_assets():
    raw = {
        "player": from_grid(PLAYER_GRID, SHIP_PAL),
        "scout": from_grid(SCOUT_GRID, SCOUT_PAL),
        "gunner": from_grid(GUNNER_GRID, GUNNER_PAL),
        "weaver": from_grid(WEAVER_GRID, WEAVER_PAL),
        "bomber": from_grid(BOMBER_GRID, BOMBER_PAL),
        "kamikaze": from_grid(KAMI_GRID, KAMI_PAL),
        "turret": from_grid(TURRET_GRID, TURRET_PAL),
        "boss": from_grid(BOSS_GRID, BOSS_PAL),
    }
    # Enemies face down toward the player (sprites are designed pointing up).
    for k in ("scout", "gunner", "weaver", "bomber", "kamikaze", "turret", "boss"):
        raw[k] = pygame.transform.flip(raw[k], False, True)
    a = {}
    scales = {"player": 2, "scout": 2, "gunner": 2, "weaver": 2,
              "bomber": 2, "kamikaze": 2, "turret": 2, "boss": 3}
    glow_colors = {
        "player":   (60, 180, 255),
        "scout":    (220, 60, 80),
        "gunner":   (200, 100, 220),
        "weaver":   (100, 220, 130),
        "bomber":   (240, 140, 50),
        "kamikaze": (240, 100, 60),
        "turret":   (140, 150, 180),
        "boss":     (220, 70, 80),
    }
    for k, surf in raw.items():
        s = scales[k]
        scaled = pygame.transform.scale(surf, (surf.get_width() * s, surf.get_height() * s))
        a[k] = scaled
        a[k + "_flash"] = make_silhouette(scaled)
    # Hand-drawn bank frames simulate Y-axis (longitudinal) rotation.
    # Build them from grids with the same palette + scale as the player.
    bank_l_raw = from_grid(PLAYER_GRID_BANK_L, SHIP_PAL)
    bank_r_raw = from_grid(PLAYER_GRID_BANK_R, SHIP_PAL)
    ps = scales["player"]
    a["player_left"] = pygame.transform.scale(
        bank_l_raw, (bank_l_raw.get_width() * ps, bank_l_raw.get_height() * ps))
    a["player_right"] = pygame.transform.scale(
        bank_r_raw, (bank_r_raw.get_width() * ps, bank_r_raw.get_height() * ps))
    a["player_left_flash"] = make_silhouette(a["player_left"])
    a["player_right_flash"] = make_silhouette(a["player_right"])
    # Deep-bank frames default to copies of the mild-bank sprites so the
    # external PNG loader can override them with art/sprites/player_left_2.png
    # and player_right_2.png if those files exist.
    a["player_left_2"] = a["player_left"].copy()
    a["player_right_2"] = a["player_right"].copy()
    a["player_left_2_flash"] = make_silhouette(a["player_left_2"])
    a["player_right_2_flash"] = make_silhouette(a["player_right_2"])
    # Pickup icons + their silhouettes
    a["pickup_main"] = _frame(YELLOW, "W")
    a["pickup_side"] = _frame(GREEN, "S")
    a["pickup_shield"] = _frame(CYAN, "+")
    a["pickup_bomb"] = _frame(PURPLE, "B")
    a["pickup_money"] = _frame((180, 180, 80), "$")
    # Side obstacles. Several rock variants for shape variety, single design
    # per other type. Each gets a corresponding white silhouette for the hit flash.
    rock_palettes = (
        ((140, 115, 90),  (70, 55, 40)),   # tan brown
        ((130, 90,  80),  (70, 40, 30)),   # reddish
        ((115, 110, 130), (60, 55, 70)),   # cool grey
        ((130, 130, 110), (70, 70, 50)),   # khaki
    )
    for idx, radius in enumerate((9, 11, 14)):
        for pi, (base, dark) in enumerate(rock_palettes):
            k = f"rock_{radius}_{pi}"
            sprite = _make_asteroid_surf(radius, base, dark, seed=idx * 37 + pi * 13 + 1)
            a[k] = sprite
            a[k + "_flash"] = make_silhouette(sprite)
    mine = _make_mine_surf();    a["mine"] = mine;    a["mine_flash"] = make_silhouette(mine)
    pylon = _make_pylon_surf();  a["pylon"] = pylon;  a["pylon_flash"] = make_silhouette(pylon)
    cryst = _make_crystal_surf();a["crystal"] = cryst;a["crystal_flash"] = make_silhouette(cryst)
    # Per-sector wall plates (one per sector palette)
    for sec in range(10):
        w = _make_wall_surf(48, 96, sec)
        a[f"wall_{sec}"] = w
        a[f"wall_{sec}_flash"] = make_silhouette(w)
    # Per-sector boss variants: seed each with a clone of the procedural boss so
    # the external sprite loader can override boss_0..boss_9 individually.
    for sec in range(10):
        a[f"boss_{sec}"] = a["boss"].copy()
        a[f"boss_{sec}_flash"] = make_silhouette(a[f"boss_{sec}"])
    # Final uniform upscale of every play-area sprite. Nearest-neighbour keeps
    # pixel edges crisp even at the non-integer factor.
    if PLAY_SCALE != 1.0:
        for k, surf in list(a.items()):
            sw = max(2, int(round(surf.get_width() * PLAY_SCALE)))
            sh = max(2, int(round(surf.get_height() * PLAY_SCALE)))
            a[k] = pygame.transform.scale(surf, (sw, sh))
    # Per-asset overrides from art/sprites/*.png. Any PNG whose stem matches a
    # procedural asset key replaces the procedural surface. The PNG is resized
    # to match the procedural size so positioning code remains unchanged. The
    # _flash silhouette is regenerated to reflect the new shape.
    sprites_dir = Path(__file__).resolve().parent / "art" / "sprites"
    def _find_sprite(stem):
        """Look for stem.bmp first (PNG support requires SDL_image, which the
        stock-OS pygame on the handheld doesn't have)."""
        for ext in (".bmp", ".png"):
            p = sprites_dir / f"{stem}{ext}"
            if p.exists():
                return p
        return None

    if sprites_dir.is_dir():
        # AI sprites are the source of truth for size — we load them at
        # whatever pixel dimensions the 1x strip cell came out as and let
        # the engine adapt via get_rect(). Procedural fallback is only used
        # when no PNG/BMP is on disk.
        for k in list(a.keys()):
            if k.endswith("_flash"):
                continue
            path = _find_sprite(k)
            if path is None:
                continue
            try:
                img = pygame.image.load(str(path)).convert_alpha()
                _knock_out_dark_bg(img)
                a[k] = img
                if (k + "_flash") in a:
                    a[k + "_flash"] = make_silhouette(img)
            except Exception:
                pass
        # The default "boss" key now mirrors boss_0 (launch_bay variant).
        if "boss_0" in a:
            a["boss"] = a["boss_0"]
            a["boss_flash"] = a["boss_0_flash"]
        # ---- Stations (one per sector, displayed full-width on dock cinematic).
        a["_stations"] = {}
        a["_launch_pads"] = {}
        for sec in range(10):
            sp = _find_sprite(f"station_{sec}")
            if sp is not None:
                try:
                    img = pygame.image.load(str(sp)).convert_alpha()
                    a["_stations"][sec] = img
                    a["_launch_pads"][sec] = img  # same art for now
                except Exception:
                    pass
        # ---- Parallax backdrops, one per ribbon theme.
        a["_backdrops"] = {}
        for theme in ("start", "asteroid", "outpost", "converge", "boss"):
            bp = _find_sprite(f"bg_{theme}")
            if bp is not None:
                try:
                    a["_backdrops"][theme] = pygame.image.load(str(bp)).convert_alpha()
                except Exception:
                    pass
        # ---- Projectile glyphs (player + enemy) cached for Bullet.draw.
        a["_projectiles"] = {}
        for name in ("glyph_pulse", "glyph_spread", "glyph_vulcan",
                     "glyph_drone", "glyph_tracker",
                     "pellet_red", "pellet_purple", "pellet_amber"):
            pp = _find_sprite(name)
            if pp is not None:
                try:
                    a["_projectiles"][name] = pygame.image.load(str(pp)).convert_alpha()
                except Exception:
                    pass
        # ---- Energy FX (single-sprite stand-ins for explosions, shield hits…).
        a["_fx"] = {}
        for name in ("burst_small", "burst_large", "shield_ring",
                     "sparkle_gold", "shockwave", "jet_droplet"):
            fp = _find_sprite(name)
            if fp is not None:
                try:
                    a["_fx"][name] = pygame.image.load(str(fp)).convert_alpha()
                except Exception:
                    pass
    # ---- Sprite editor data: per-sprite pivot, hitbox, dummies. Loaded
    # once at startup; entities consult it on the fly for fire positions
    # and collision rects, falling back to hard-coded defaults when a
    # sprite has no entry (or no specific dummy / hitbox in its entry).
    a["_engine_data"] = {}
    here = Path(__file__).resolve().parent
    for parent in (here / "art", here):
        engine_path = parent / "sprite_engine.json"
        if engine_path.is_file():
            try:
                a["_engine_data"] = json.loads(engine_path.read_text())
            except Exception as e:
                print(f"sprite_engine.json load failed: {e}")
            break
    return a


def _add_tone(buf, sr, freq, start_t, dur, vol=0.15, wave="square", decay=2.0,
              attack=0.005):
    """Add a tone with an exponential decay envelope into an int16 buffer."""
    n_dur = int(sr * dur)
    i_start = int(sr * start_t)
    n_buf = len(buf)
    fade_out = min(0.03, dur * 0.2)
    for i in range(n_dur):
        t = i / sr
        # Attack
        env_a = min(1.0, t / attack) if attack > 0 else 1.0
        # Decay (exponential body)
        env_d = math.exp(-decay * t)
        # Release tail
        remaining = dur - t
        env_r = min(1.0, remaining / fade_out) if remaining < fade_out else 1.0
        env = env_a * env_d * env_r
        if wave == "square":
            v = 1.0 if (t * freq) % 1.0 < 0.5 else -1.0
        elif wave == "saw":
            v = ((t * freq) % 1.0) * 2.0 - 1.0
        elif wave == "triangle":
            phase = (t * freq) % 1.0
            v = 2.0 * abs(2.0 * phase - 1.0) - 1.0
        else:  # sine
            v = math.sin(2.0 * math.pi * freq * t)
        sample = int(v * env * vol * 30000)
        idx = i_start + i
        if 0 <= idx < n_buf:
            x = buf[idx] + sample
            if x > 32767: x = 32767
            elif x < -32767: x = -32767
            buf[idx] = x


def _add_kick(buf, sr, start_t, vol=0.5):
    """Kick drum: short pitch-down sine pulse."""
    n_dur = int(sr * 0.16)
    i_start = int(sr * start_t)
    n_buf = len(buf)
    for i in range(n_dur):
        t = i / sr
        f = 80.0 * math.exp(-14.0 * t) + 35.0
        env = math.exp(-11.0 * t)
        sample = int(math.sin(2.0 * math.pi * f * t) * env * vol * 30000)
        idx = i_start + i
        if 0 <= idx < n_buf:
            x = buf[idx] + sample
            if x > 32767: x = 32767
            elif x < -32767: x = -32767
            buf[idx] = x


def _add_snare(buf, sr, start_t, vol=0.35):
    """Snare drum: short noise burst with quick decay."""
    n_dur = int(sr * 0.09)
    i_start = int(sr * start_t)
    n_buf = len(buf)
    for i in range(n_dur):
        t = i / sr
        env = math.exp(-22.0 * t)
        sample = int(random.uniform(-1.0, 1.0) * env * vol * 30000)
        idx = i_start + i
        if 0 <= idx < n_buf:
            x = buf[idx] + sample
            if x > 32767: x = 32767
            elif x < -32767: x = -32767
            buf[idx] = x


def _add_hihat(buf, sr, start_t, vol=0.18):
    """Hi-hat: very short, brighter noise burst."""
    n_dur = int(sr * 0.04)
    i_start = int(sr * start_t)
    n_buf = len(buf)
    prev = 0.0
    for i in range(n_dur):
        t = i / sr
        env = math.exp(-40.0 * t)
        # High-pass-ish: just use shaped noise
        sample_v = random.uniform(-1.0, 1.0)
        sample_v = sample_v - prev * 0.4
        prev = sample_v
        sample = int(sample_v * env * vol * 30000)
        idx = i_start + i
        if 0 <= idx < n_buf:
            x = buf[idx] + sample
            if x > 32767: x = 32767
            elif x < -32767: x = -32767
            buf[idx] = x


MUSIC_CACHE_VERSION = "v1"
MUSIC_CACHE_DIR = Path(os.environ.get(
    "PEWPEW_MUSIC_CACHE",
    str(Path(__file__).resolve().parent / "music_cache"),
))
MUSIC_KINDS = ("menu", "game", "boss", "takeoff", "dock")


def _music_cache_path(kind):
    return MUSIC_CACHE_DIR / f"{kind}_{MUSIC_CACHE_VERSION}.pcm"


def make_music_cached(kind):
    """Load the named track from the on-disk PCM cache if it exists; otherwise
    generate it via make_music() and persist the raw bytes for next time.
    Cache files are versioned, so bumping MUSIC_CACHE_VERSION invalidates them
    after a music-code change without manual cleanup."""
    cache_file = _music_cache_path(kind)
    if cache_file.exists():
        try:
            return pygame.mixer.Sound(buffer=cache_file.read_bytes())
        except Exception:
            pass
    sound = make_music(kind)
    try:
        MUSIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        raw = sound.get_raw()
        if raw:
            cache_file.write_bytes(raw)
    except Exception:
        pass
    return sound


def make_music(kind):
    """Build a looping music track. Returns a pygame.mixer.Sound or _Silent().
    Generation is in pure Python with an int16 buffer; takes ~0.5-1s per track
    on this hardware."""
    try:
        sr = 22050
        if kind == "menu":
            bpm = 90
            beats = 16
        elif kind == "game":
            bpm = 132
            beats = 16
        elif kind == "takeoff":
            bpm = 120
            beats = 8
        elif kind == "dock":
            bpm = 100
            beats = 8
        else:  # boss
            bpm = 150
            beats = 16
        beat = 60.0 / bpm
        total = beat * beats
        n = int(sr * total)
        buf = array.array("h", [0] * n)

        if kind == "menu":
            # Slow ambient pad: Am - F - C - G progression (Pop-Punk Cliché in A min)
            chords = [
                (0,  (220.00, 261.63, 329.63)),   # Am
                (4,  (174.61, 220.00, 261.63)),   # F
                (8,  (130.81, 164.81, 196.00)),   # C
                (12, (196.00, 246.94, 293.66)),   # G
            ]
            for start_beat, freqs in chords:
                t0 = start_beat * beat
                cd = 4.0 * beat
                for f in freqs:
                    _add_tone(buf, sr, f, t0, cd, vol=0.09, wave="sine",
                              decay=0.25, attack=0.25)
            # Soft bell-like melody over the pads
            melody = [
                (0.5,  659.25, 0.5),  # E5
                (2.5,  523.25, 0.5),  # C5
                (4.5,  523.25, 0.5),  # C5
                (6.5,  440.00, 0.5),  # A4
                (8.5,  392.00, 0.5),  # G4
                (10.5, 523.25, 0.5),  # C5
                (12.5, 440.00, 0.5),  # A4
                (14.5, 587.33, 0.5),  # D5
            ]
            for start_beat, freq, note_dur in melody:
                _add_tone(buf, sr, freq, start_beat * beat, note_dur * beat,
                          vol=0.07, wave="triangle", decay=1.5, attack=0.01)

        elif kind == "game":
            # Driving Am pentatonic loop. Bass on beats, arp on off-beats,
            # kick on 1/3, snare on 2/4, hats on every off-beat.
            #          1    2    3    4    5    6    7    8
            bass = [110.00, 110.00, 110.00, 110.00,
                    146.83, 146.83, 146.83, 146.83,
                    164.81, 164.81, 164.81, 164.81,
                    110.00, 110.00, 130.81, 130.81]
            arp = [220.00, 261.63, 329.63, 261.63,
                   293.66, 349.23, 391.99, 349.23,
                   329.63, 391.99, 440.00, 391.99,
                   261.63, 329.63, 391.99, 329.63]
            for i, f in enumerate(bass):
                _add_tone(buf, sr, f, i * beat, beat * 0.85,
                          vol=0.20, wave="square", decay=2.5)
            for i, f in enumerate(arp):
                _add_tone(buf, sr, f, i * beat + beat * 0.5, beat * 0.35,
                          vol=0.09, wave="square", decay=5.0)
            for i in range(beats):
                if i % 2 == 0:
                    _add_kick(buf, sr, i * beat, vol=0.55)
                else:
                    _add_snare(buf, sr, i * beat, vol=0.38)
                _add_hihat(buf, sr, i * beat + beat * 0.5, vol=0.20)

        elif kind == "boss":
            # Tense, faster, with a chromatic bass leaning on the tritone.
            bass = [110.00, 110.00, 116.54, 116.54,
                    155.56, 155.56, 146.83, 146.83] * 2
            for i, f in enumerate(bass):
                _add_tone(buf, sr, f, i * beat, beat * 0.85,
                          vol=0.22, wave="saw", decay=2.0)
            for i in range(beats):
                _add_kick(buf, sr, i * beat, vol=0.6)
                if i % 2 == 1:
                    _add_snare(buf, sr, i * beat + beat * 0.5, vol=0.42)
                _add_hihat(buf, sr, i * beat + beat * 0.25, vol=0.18)
                _add_hihat(buf, sr, i * beat + beat * 0.75, vol=0.16)

        elif kind == "takeoff":
            # Ascending C-major fanfare in 4 seconds; plays through the intro
            # cinematic once and loops if the player lingers.
            arp = [
                (0.00, 261.63, 0.30),  # C4
                (0.30, 329.63, 0.30),  # E4
                (0.60, 392.00, 0.30),  # G4
                (0.90, 523.25, 0.40),  # C5
                (1.30, 659.25, 0.40),  # E5
                (1.70, 783.99, 0.60),  # G5
                (2.30, 1046.50, 1.40), # C6 sustain
            ]
            for t0, f, dur in arp:
                _add_tone(buf, sr, f, t0, dur, vol=0.22, wave="square", decay=2.0)
            # Bass drone under the arpeggio.
            _add_tone(buf, sr, 130.81, 0.0, 3.5, vol=0.16, wave="saw", decay=0.4)
            # Snare roll into the high C, then a triumphant kick.
            for i in range(12):
                _add_snare(buf, sr, 1.4 + i * 0.05, vol=0.18)
            _add_kick(buf, sr, 0.0, vol=0.55)
            _add_kick(buf, sr, 2.3, vol=0.7)

        elif kind == "dock":
            # IV - V - I cadence in C major across ~4.8 seconds; reads as
            # "you made it" when the ship parks at the destination station.
            chords = [
                (0.0, [349.23, 440.00, 523.25], 1.2),  # F major
                (1.2, [392.00, 493.88, 587.33], 1.2),  # G major
                (2.4, [523.25, 659.25, 783.99], 2.0),  # C major sustained
            ]
            for t0, freqs, dur in chords:
                for f in freqs:
                    _add_tone(buf, sr, f, t0, dur, vol=0.13, wave="square", decay=0.6)
            # Walking bass F G C
            _add_tone(buf, sr, 87.31,  0.0, 1.2, vol=0.18, wave="saw", decay=1.2)
            _add_tone(buf, sr, 98.00,  1.2, 1.2, vol=0.18, wave="saw", decay=1.2)
            _add_tone(buf, sr, 130.81, 2.4, 2.2, vol=0.18, wave="saw", decay=0.5)
            # Tonic emphasis on each chord change.
            _add_kick(buf, sr, 0.0, vol=0.55)
            _add_kick(buf, sr, 1.2, vol=0.55)
            _add_kick(buf, sr, 2.4, vol=0.8)
            # A bright bell on the final tonic for a satisfying landing.
            _add_tone(buf, sr, 1046.50, 2.4, 1.6, vol=0.10, wave="triangle",
                      decay=1.2, attack=0.01)

        return pygame.mixer.Sound(buffer=buf.tobytes())
    except Exception:
        return _Silent()


def _fire_variants(base_freq, base_dur, vol, base_sweep=0.0, n=5, square=True):
    """Render a small pool of subtly de-tuned copies. Pitch jitter ~+/-1.2%,
    duration ~+/-1%, sweep ~+/-2 Hz/s — small enough that the chirp
    identity survives but successive shots aren't mechanically identical.
    base_sweep gives the tone an overall downward (negative) glide so the
    sound reads as a short space-shooter "pew" rather than a static beep."""
    jitter = [
        (1.000, 1.00,  0.0),
        (1.012, 0.99,  2.0),
        (0.988, 1.01, -2.0),
        (1.006, 1.00,  1.0),
        (0.994, 1.00, -1.0),
        (1.010, 1.01,  1.5),
        (0.990, 0.99, -1.5),
    ][:n]
    return [
        tone(int(base_freq * fmul), max(0.02, base_dur * dmul),
             vol, square=square, sweep=base_sweep + sw)
        for fmul, dmul, sw in jitter
    ]


def make_sounds():
    return {
        # Lower-frequency space "pew" — short downward chirp at low volume.
        # shoot:  540 -> ~295 Hz over 70 ms (main weapon)
        # shoot2: 360 -> ~185 Hz over 80 ms (sidekick, octave-ish lower)
        "shoot":  RandomBank(_fire_variants(540, 0.07, 0.09,
                                            base_sweep=-3500, n=5)),
        "shoot2": RandomBank(_fire_variants(360, 0.08, 0.08,
                                            base_sweep=-2200, n=5)),
        "hit":    tone(200, 0.08, 0.22, square=False),
        "boom":   noise(0.20, 0.32, lp=0.3),
        "big_boom": noise(0.55, 0.42, lp=0.15),
        "pickup": tone(1320, 0.10, 0.25, square=True),
        "money":  tone(1760, 0.04, 0.20, square=True),
        "bomb":   noise(0.6, 0.45, lp=0.2),
        "menu":   tone(500, 0.04, 0.20, square=True),
        "confirm": tone(1000, 0.08, 0.25, square=True),
        "deny":   tone(180, 0.10, 0.25, square=True),
        "warn":   tone(440, 0.30, 0.20, square=True, sweep=200),
    }


# =============================================================================
# SAVE / LOADOUT
# =============================================================================

@dataclass
class Loadout:
    # Equipped weapon types and their per-type levels. All three mains are
    # owned from the start (L1=Pulse-hold, R1=Spread-hold, nothing=Vulcan).
    # main_type tracks which one the player is currently firing so pickups
    # and upgrades apply to the live weapon.
    main_type: str = "vulcan"
    main_pulse: int = 1
    main_spread: int = 1
    main_vulcan: int = 1
    side_type: str = "none"
    side_missile: int = 0
    side_drone: int = 0
    shield: int = 1
    engine: int = 1
    bombs: int = 2
    ability: str = "screen_clear"

    def main_level(self):
        """Level of the currently equipped main weapon."""
        return getattr(self, f"main_{self.main_type}", 0)

    def side_level(self):
        """Level of the currently equipped sidekick (0 if none)."""
        if self.side_type == "none":
            return 0
        return getattr(self, f"side_{self.side_type}", 0)

    def owns_main(self, kind):
        return getattr(self, f"main_{kind}", 0) > 0

    def owns_side(self, kind):
        return getattr(self, f"side_{kind}", 0) > 0


# Hardcoded list of player-profile slot names — picked galaxies (one
# spiral neighbour, our own, then three Messier favourites). The order is
# stable so L1/R1 on the title screen cycle predictably.
PROFILE_NAMES = ("ANDROMEDA", "MILKY WAY", "SOMBRERO", "PINWHEEL", "WHIRLPOOL")
DEFAULT_PROFILE = PROFILE_NAMES[0]


@dataclass
class SaveData:
    credits: int = 0
    current_node: str = "L001"
    completed: list = field(default_factory=list)
    unlocked: list = field(default_factory=lambda: ["L001"])
    high_score: int = 0
    volume: float = 0.6        # SFX bus level
    music_volume: float = 0.5  # music bus level
    loadout: Loadout = field(default_factory=Loadout)
    # Per-upgrade tier unlocks. Default 2 — tiers 1 and 2 are unlocked
    # from the start. Bosses 1..9 unlock tiers 3, 4, 5 progressively
    # (see _boss_unlocks_for_level for the schedule + cascade rule).
    unlocked_tier_pulse:   int = 2
    unlocked_tier_spread:  int = 2
    unlocked_tier_vulcan:  int = 2
    unlocked_tier_missile: int = 2
    unlocked_tier_drone:   int = 2
    unlocked_tier_shield:  int = 2
    unlocked_tier_engine:  int = 2
    # Per-level adaptive difficulty knob. Starts at 0 (= baseline). Each
    # death on the level decrements by 1, each finish bumps by 5 (capped
    # at 0). Negative values bias the level easier — fewer enemies per
    # wave, lower shield-spawn chance, downgraded enemy types in waves,
    # bias toward bomb / shield pickups. Never goes positive (never
    # makes the level harder). See _apply_difficulty_to_spawn /
    # _effective_shield_chance / _biased_drop_kind for the application.
    level_difficulty_adjust: dict = field(default_factory=dict)

    @staticmethod
    def _read_file():
        """Return the parsed save.json as a dict, normalised to the
        profile-aware shape: {"current_profile": str, "profiles": {...},
        ...any other top-level keys (integer_scale, future display prefs,
        etc.)}. Migrates an older flat layout (everything at top level)
        by wrapping the existing fields under the first profile slot.

        Preserving unknown top-level keys is load-bearing: callers like
        `save()` and `set_current_profile()` round-trip the dict through
        this function, and anything dropped here is dropped from disk on
        the next write — that bit me with `integer_scale`, which toggled
        live but was wiped by the next normal save."""
        try:
            raw = json.loads(SAVE_PATH.read_text())
        except Exception:
            return {"current_profile": DEFAULT_PROFILE, "profiles": {}}
        if not isinstance(raw, dict):
            return {"current_profile": DEFAULT_PROFILE, "profiles": {}}
        if "profiles" in raw and isinstance(raw["profiles"], dict):
            out = dict(raw)
            out["current_profile"] = str(raw.get("current_profile")
                                         or DEFAULT_PROFILE).upper()
            out["profiles"] = raw["profiles"]
            return out
        # Legacy single-save file → migrate into the first profile slot.
        legacy = dict(raw)
        legacy.pop("profiles", None)
        legacy.pop("current_profile", None)
        return {
            "current_profile": DEFAULT_PROFILE,
            "profiles": {DEFAULT_PROFILE: legacy} if legacy else {},
        }

    @staticmethod
    def _parse_profile(raw):
        """Turn one profile's dict into a SaveData. Reused by load() and
        the migration path. Tolerates unknown keys + the pre-Tyrian
        loadout layout."""
        try:
            raw = dict(raw or {})
            unlocked = raw.get("unlocked") or []
            if unlocked and not all(isinstance(k, str) and k.startswith("L")
                                    for k in unlocked):
                return SaveData()
            raw_loadout = raw.pop("loadout", {}) or {}
            if "main" in raw_loadout and "main_pulse" not in raw_loadout:
                old_main = int(raw_loadout.pop("main"))
                raw_loadout["main_type"] = "pulse"
                raw_loadout["main_pulse"] = max(1, old_main)
            if "side" in raw_loadout and "side_missile" not in raw_loadout:
                old_side = int(raw_loadout.pop("side"))
                if old_side > 0:
                    raw_loadout["side_type"] = "missile"
                    raw_loadout["side_missile"] = old_side
                else:
                    raw_loadout["side_type"] = "none"
            allowed = set(Loadout.__dataclass_fields__.keys())
            raw_loadout = {k: v for k, v in raw_loadout.items() if k in allowed}
            # All 3 mains are always owned now — bump any zero levels to 1
            # so legacy saves don't end up with an empty trigger.
            for k in ("main_pulse", "main_spread", "main_vulcan"):
                if int(raw_loadout.get(k, 0)) < 1:
                    raw_loadout[k] = 1
            loadout = Loadout(**raw_loadout)
            sd_allowed = set(SaveData.__dataclass_fields__.keys())
            raw = {k: v for k, v in raw.items() if k in sd_allowed}
            return SaveData(loadout=loadout, **raw)
        except Exception:
            return SaveData()

    @staticmethod
    def load(profile=None):
        """Load the named profile (or current_profile if None) from the
        single save.json file. Always returns a SaveData — falls back to
        defaults when the profile slot is empty."""
        store = SaveData._read_file()
        name = (profile or store["current_profile"]).upper()
        if name not in PROFILE_NAMES:
            name = DEFAULT_PROFILE
        return SaveData._parse_profile(store["profiles"].get(name))

    @staticmethod
    def current_profile_name():
        return SaveData._read_file()["current_profile"]

    @staticmethod
    def set_current_profile(name):
        """Persist which profile is the active one without touching any
        profile's own data."""
        name = (name or DEFAULT_PROFILE).upper()
        if name not in PROFILE_NAMES:
            name = DEFAULT_PROFILE
        store = SaveData._read_file()
        store["current_profile"] = name
        try:
            SAVE_PATH.write_text(json.dumps(store, indent=2))
        except Exception:
            pass

    @staticmethod
    def load_scale_mode():
        """Read the persisted present-mode string from the top of the
        save store. Lives outside any profile because it's a per-device
        display preference, not per-character progress. Migrates the
        legacy `integer_scale` bool: True → "integer", False → "fill".
        Defaults to "integer" so a missing save / first launch starts
        in the safe (crisp) mode."""
        store = SaveData._read_file()
        val = store.get("scale_mode")
        if val in SCALE_MODES:
            return val
        # Legacy migration: the old bool key used to live here alone.
        legacy = store.get("integer_scale")
        if legacy is None:
            return "integer"
        return "integer" if bool(legacy) else "fill"

    @staticmethod
    def save_scale_mode(val):
        """Persist the present-mode string without touching profile
        data or the current_profile pointer. Also clears the legacy
        `integer_scale` key so it can't drift out of sync."""
        if val not in SCALE_MODES:
            val = "integer"
        store = SaveData._read_file()
        store["scale_mode"] = val
        store.pop("integer_scale", None)
        try:
            SAVE_PATH.write_text(json.dumps(store, indent=2))
        except Exception:
            pass

    @staticmethod
    def load_last_seen_version():
        """Top-level pointer to the VERSION string the player last saw the
        title screen with. App.__init__ compares it against VERSION to
        decide whether to show the release-notes overlay; we update it
        only when the overlay is dismissed, so a crash before dismiss
        re-shows the same notes on next launch."""
        return SaveData._read_file().get("last_seen_version") or ""

    @staticmethod
    def save_last_seen_version(val):
        """Persist last_seen_version as a top-level key, leaving profile
        data + scale_mode untouched."""
        store = SaveData._read_file()
        store["last_seen_version"] = str(val)
        try:
            SAVE_PATH.write_text(json.dumps(store, indent=2))
        except Exception:
            pass

    @staticmethod
    def profile_exists(name):
        """True when the named profile has any saved data on disk."""
        store = SaveData._read_file()
        return bool(store["profiles"].get(name.upper()))

    def save(self, profile=None):
        """Write this SaveData into the named profile slot, preserving
        all other profiles. When profile is None we write to whichever
        slot is the current_profile."""
        try:
            store = SaveData._read_file()
            name = (profile or store["current_profile"]).upper()
            if name not in PROFILE_NAMES:
                name = DEFAULT_PROFILE
            store["current_profile"] = name
            store["profiles"][name] = asdict(self)
            SAVE_PATH.write_text(json.dumps(store, indent=2))
        except Exception:
            pass


# =============================================================================
# BACKGROUND
# =============================================================================

class ParallaxStars:
    """Three-layer starfield. Layer 0 = far/slow/dim, 2 = near/fast/bright.

    Earlier version baked each layer into a PLAY_W × PLAY_H colorkey tile
    and blitted it four times per frame to cover the scroll wrap. That cost
    ~3.9 ms on the RG35XX Pro because every colorkey blit still had to
    *scan* all 230k pixels of the tile to find the ~50 actual stars. Now
    each star is its own 1×1 (or 1×2 streak) opaque sprite blitted at its
    wrapped screen position — ~125 tiny opaque blits per frame is far
    cheaper than 12 full-tile colorkey blits."""

    def __init__(self, width=PLAY_W, height=PLAY_H, counts=(60, 40, 25)):
        self.width = width
        self.height = height
        speeds = (30, 80, 170)
        shades = ((90, 90, 110), (160, 160, 180), (230, 230, 255))
        self.layers = []
        for n, sp, sh in zip(counts, speeds, shades):
            # Pre-bake the tiny sprite for this layer. Near-layer stars get
            # a 1×2 streak so they read as motion blur at speed.
            sprite_h = 2 if sp > 100 else 1
            sprite = pygame.Surface((1, sprite_h))
            sprite.fill(sh)
            try:
                sprite = sprite.convert()
            except pygame.error:
                pass
            stars = [(random.uniform(0, width), random.uniform(0, height))
                     for _ in range(n)]
            self.layers.append({
                "stars": stars,
                "sprite": sprite,
                "speed": sp,
                "scroll_y": 0.0,
                "scroll_x": 0.0,
            })

    def update(self, dt):
        h = self.height
        for L in self.layers:
            L["scroll_y"] = (L["scroll_y"] + L["speed"] * dt) % h

    def lateral_shift(self, dx):
        """Slide stars horizontally opposite to a player movement of `dx`.
        Near-layer stars (higher speed) shift more, so the side-scroll reads
        as parallax depth instead of a flat slide."""
        if dx == 0:
            return
        ref = 170.0  # near-layer reference speed
        w = self.width
        for L in self.layers:
            L["scroll_x"] = (L["scroll_x"] - dx * (L["speed"] / ref) * 0.55) % w

    def draw(self, surf):
        # Per-star blit at (bx + sx) mod w, (by + sy) mod h. scroll grows
        # over time (and with player lateral movement), so as sy grows the
        # stars move DOWN — matching the original s[1] += speed*dt.
        w = self.width
        h = self.height
        # Local-bind the blit method — saves a dict lookup per star in the
        # tight loop, which adds up over ~125 blits/frame.
        blit = surf.blit
        for L in self.layers:
            sx = L["scroll_x"]
            sy = L["scroll_y"]
            sprite = L["sprite"]
            for bx, by in L["stars"]:
                x = int(bx + sx)
                y = int(by + sy)
                if x >= w: x -= w
                if y >= h: y -= h
                blit(sprite, (x, y))


class Nebula:
    def __init__(self, tint):
        self.tint = tint
        self.layer = pygame.Surface((PLAY_W, PLAY_H * 2), pygame.SRCALPHA)
        for _ in range(40):
            x = random.randint(0, PLAY_W)
            y = random.randint(0, PLAY_H * 2)
            r = random.randint(20, 60)
            alpha = random.randint(5, 14)
            color = (tint[0], tint[1], tint[2], alpha)
            pygame.draw.circle(self.layer, color, (x, y), r)
        self.y = 0

    def update(self, dt):
        self.y = (self.y + 12 * dt) % PLAY_H

    def draw(self, surf):
        surf.blit(self.layer, (0, -int(self.y)))
        surf.blit(self.layer, (0, -int(self.y) + PLAY_H))


def _draw_asteroid(surf, cx, cy, r, base):
    pts = []
    for i in range(10):
        ang = i * math.tau / 10
        rr = r * random.uniform(0.7, 1.15)
        pts.append((cx + math.cos(ang) * rr, cy + math.sin(ang) * rr))
    pygame.draw.polygon(surf, base, pts)
    shade = tuple(max(0, c - 30) for c in base[:3])
    for _ in range(3):
        cx2 = cx + random.uniform(-r * 0.3, r * 0.3)
        cy2 = cy + random.uniform(-r * 0.3, r * 0.3)
        pygame.draw.circle(surf, shade, (int(cx2), int(cy2)), max(2, int(r * 0.2)))


def _draw_hull_plate(surf, x, y, w, h, base):
    pygame.draw.rect(surf, base, (x, y, w, h))
    edge = tuple(min(255, c + 24) for c in base[:3])
    shade = tuple(max(0, c - 28) for c in base[:3])
    pygame.draw.rect(surf, edge, (x, y, w, 2))
    pygame.draw.rect(surf, shade, (x, y + h - 2, w, 2))
    # rivets
    for rx in range(x + 5, x + w - 5, 12):
        pygame.draw.rect(surf, edge, (rx, y + h // 2 - 1, 2, 2))


def _draw_pipe(surf, x, y, length, w, base):
    pygame.draw.rect(surf, base, (x, y, w, length))
    shade = tuple(max(0, c - 30) for c in base[:3])
    pygame.draw.rect(surf, shade, (x + w - 2, y, 2, length))


class BackgroundRibbon:
    """A per-level large procedural background that scrolls slowly under the stars."""
    # AI-generated backdrop tiles (loaded once, shared across BackgroundRibbon
    # instances). Populated by `set_backdrops` from the App at startup.
    _ai_backdrops = {}

    @classmethod
    def set_backdrops(cls, backdrops):
        cls._ai_backdrops = backdrops or {}

    def __init__(self, level_key, width=PLAY_W, tile_h=PLAY_H * 2):
        self._level_key = level_key  # remembered so we can re-render later
        self.width = width
        self.tile_h = tile_h
        self.scroll = 0.0
        self.speed = 24.0
        # Use the AI backdrop if available — it's a single image stretched to
        # the ribbon tile size. Otherwise fall through to procedural _build.
        ai = self._ai_backdrops.get(level_key)
        if ai is not None:
            scaled = pygame.transform.scale(ai, (width, tile_h))
            # AI backdrops come back vivid; dim RGB by ~51% so they sit
            # behind the parallax stars instead of competing with the
            # foreground. The previous (0,0,0,130) multiplier was a bug:
            # it zeroed RGB and only dimmed alpha, leaving a transparent
            # black layer that cost 3.7 ms/frame and rendered as nothing.
            dim = pygame.Surface((width, tile_h), pygame.SRCALPHA)
            dim.blit(scaled, (0, 0))
            dim.fill((130, 130, 130, 255),
                     special_flags=pygame.BLEND_RGBA_MULT)
            # After the multiply the layer is fully opaque, so drop alpha
            # and let the blit go through the fast RGB path.
            try:
                self.layer = dim.convert()
            except pygame.error:
                self.layer = dim
            return
        self.layer = pygame.Surface((width, tile_h), pygame.SRCALPHA)
        self._build(level_key)

    def _build(self, key):
        if key == "start":
            # distant nebula wisps + faint stars
            for _ in range(120):
                x = random.randint(0, self.width)
                y = random.randint(0, self.tile_h)
                self.layer.set_at((x, y), (60, 60, 90, 200))
        elif key == "asteroid":
            for _ in range(28):
                cx = random.randint(20, self.width - 20)
                cy = random.randint(0, self.tile_h)
                r = random.randint(10, 28)
                base = (random.randint(60, 90), random.randint(50, 70), random.randint(40, 60), 255)
                _draw_asteroid(self.layer, cx, cy, r, base)
            for _ in range(60):
                x = random.randint(0, self.width)
                y = random.randint(0, self.tile_h)
                r = random.randint(2, 5)
                pygame.draw.circle(self.layer, (50, 40, 30, 200), (x, y), r)
        elif key == "outpost":
            # station hull strips along left/right edges
            for side in (0, self.width - 80):
                y = 0
                while y < self.tile_h:
                    h = random.randint(40, 90)
                    w = random.randint(30, 70)
                    x = side + (0 if side == 0 else (80 - w))
                    base = (random.randint(50, 80), random.randint(55, 80), random.randint(70, 100), 255)
                    _draw_hull_plate(self.layer, x, y, w, h, base)
                    y += h + random.randint(8, 30)
            # connecting pipes / lights in the middle
            for _ in range(14):
                px = random.randint(110, self.width - 110)
                py = random.randint(0, self.tile_h)
                length = random.randint(30, 100)
                _draw_pipe(self.layer, px, py, length, 6, (60, 70, 95, 220))
            for _ in range(40):
                lx = random.randint(0, self.width)
                ly = random.randint(0, self.tile_h)
                pygame.draw.rect(self.layer, (180, 220, 120, 200), (lx, ly, 2, 2))
        elif key == "converge":
            # dense distant starfield + soft purple smears
            for _ in range(200):
                x = random.randint(0, self.width)
                y = random.randint(0, self.tile_h)
                shade = random.randint(50, 110)
                self.layer.set_at((x, y), (shade, shade, shade + 10, 220))
            for _ in range(18):
                cx = random.randint(0, self.width)
                cy = random.randint(0, self.tile_h)
                r = random.randint(40, 80)
                pygame.draw.circle(self.layer, (90, 60, 130, 18), (cx, cy), r)
        elif key == "boss":
            # angry red glow + debris
            for _ in range(24):
                cx = random.randint(0, self.width)
                cy = random.randint(0, self.tile_h)
                r = random.randint(40, 100)
                pygame.draw.circle(self.layer, (140, 30, 50, 28), (cx, cy), r)
            for _ in range(80):
                x = random.randint(0, self.width)
                y = random.randint(0, self.tile_h)
                self.layer.set_at((x, y), (200, 80, 80, 200))

    def update(self, dt):
        self.scroll = (self.scroll + self.speed * dt) % self.tile_h

    def draw(self, surf, offset_x=0):
        surf_w = surf.get_width()
        surf_h = surf.get_height()
        # Centre horizontally if the layer is wider than the target surface
        # (happens after remake_native_aspect_h(fit_h=None) — the bg ends up
        # 256*mirror_n = 768 px wide while the playfield/screen is narrower).
        # `offset_x` lets the caller slide the bg horizontally for parallax
        # WITHOUT moving the entities drawn on the same surface.
        x0 = -(self.width - surf_w) // 2 if self.width > surf_w else 0
        x0 += int(offset_x)
        # Vertical tile is short when fit_h=None (~188 px tall source), so
        # we tile repeatedly to cover surf_h instead of just two blits.
        scroll = int(self.scroll) % self.tile_h
        y = -scroll
        while y < surf_h:
            surf.blit(self.layer, (x0, y))
            y += self.tile_h

    def make_mirrored(self):
        """Replace the current tile with a 2× taller one whose bottom half
        is the vertical flip of its top half — pixel row 0 then matches
        row 2H-1, so the wrap between two stacked tiles becomes seamless
        regardless of how busy the underlying art is. Doubles tile_h."""
        flipped = pygame.transform.flip(self.layer, False, True)
        big_h = self.tile_h * 2
        try:
            big = pygame.Surface((self.width, big_h)).convert()
        except pygame.error:
            big = pygame.Surface((self.width, big_h))
        big.blit(self.layer, (0, 0))
        big.blit(flipped, (0, self.tile_h))
        self.layer = big
        self.tile_h = big_h

    def remake_native_aspect_h(self, fit_h=None, mirror_n=3):
        """Rebuild the layer from the original AI backdrop, mirror-tiled
        `mirror_n` copies wide so the seams between tiles match invisibly.

        If `fit_h` is None the source is taken AT ITS NATIVE PIXEL SIZE — no
        scaling at all, just laid down at 1:1 — so the ribbon is exactly
        source_w * mirror_n wide and source_h tall. That's what the engine
        wants for pixel-perfect backdrops at 256x3 = 768 px.

        If `fit_h` is given the source is scaled to that height (preserving
        aspect), then tiled the same way.

        No-op for procedural ribbons (no native source to recover)."""
        ai = self._ai_backdrops.get(self._level_key) if hasattr(self, "_level_key") else None
        if ai is None:
            return
        src_w, src_h = ai.get_size()
        if fit_h is None:
            tile_w, tile_h = src_w, src_h
            scaled = ai
        else:
            tile_w = max(1, int(round(fit_h * src_w / src_h)))
            tile_h = fit_h
            scaled = pygame.transform.scale(ai, (tile_w, tile_h))
        # Same dim-multiply the constructor applies so the depth feel
        # matches the existing ribbon.
        dim = pygame.Surface((tile_w, tile_h), pygame.SRCALPHA)
        dim.blit(scaled, (0, 0))
        dim.fill((130, 130, 130, 255), special_flags=pygame.BLEND_RGBA_MULT)
        try:
            single = dim.convert()
        except pygame.error:
            single = dim
        # Mirror-tile: original | hflip | original | ... so the seams
        # between adjacent copies match column-for-column.
        flipped = pygame.transform.flip(single, True, False)
        big_w = tile_w * mirror_n
        try:
            big = pygame.Surface((big_w, tile_h)).convert()
        except pygame.error:
            big = pygame.Surface((big_w, tile_h))
        for i in range(mirror_n):
            big.blit(flipped if (i % 2) else single, (i * tile_w, 0))
        self.layer = big
        self.width = big_w
        self.tile_h = tile_h


STATION_PALETTES = [
    ((80, 130, 200),  (180, 220, 255), (40, 60, 110)),     # 1  blue (Launch Bay)
    ((150, 110, 70),  (240, 200, 140), (90, 60, 30)),      # 2  tan (Asteroid Belt)
    ((140, 80, 180),  (220, 180, 240), (80, 40, 110)),     # 3  purple (Outpost Run)
    ((150, 130, 60),  (240, 220, 140), (100, 80, 30)),     # 4  gold (Comet Wash)
    ((80, 180, 130),  (180, 240, 200), (40, 110, 70)),     # 5  green (Void Ring)
    ((200, 70, 90),   (250, 170, 190), (140, 30, 50)),     # 6  red (Crimson Shoals)
    ((90, 110, 200),  (180, 200, 240), (40, 60, 130)),     # 7  indigo (Pulsar Belt)
    ((110, 140, 160), (200, 230, 250), (60, 80, 100)),     # 8  steel (Iron Tide)
    ((210, 130, 60),  (250, 200, 140), (130, 80, 30)),     # 9  orange (Ember Field)
    ((220, 60, 80),   (250, 170, 190), (150, 30, 50)),     # 10 crimson (Final Approach)
]


def make_station(seed, sector_idx):
    """Procedurally generate a space-station Surface for level intro/outro.
    Picks one of four hull shapes (slab / ring / spire / cluster) and colors it
    with the sector's palette. The seed is per-level so the same station is
    drawn every time you replay."""
    rng = random.Random(seed)
    width = min(PLAY_W - 40, 440)
    height = 120
    s = pygame.Surface((width, height), pygame.SRCALPHA)
    base, accent, dark = STATION_PALETTES[sector_idx % len(STATION_PALETTES)]
    cx = width // 2

    kind = rng.choice(("slab", "ring", "spire", "cluster"))

    if kind == "slab":
        bar_h = 46
        bar_y = height - bar_h - 12
        pygame.draw.rect(s, base, (10, bar_y, width - 20, bar_h))
        pygame.draw.rect(s, accent, (10, bar_y, width - 20, 4))
        pygame.draw.rect(s, dark,   (10, bar_y + bar_h - 4, width - 20, 4))
        x = 30
        while x < width - 50:
            mod_w = rng.randint(26, 42)
            mod_h = rng.randint(28, 56)
            pygame.draw.rect(s, base, (x, bar_y - mod_h, mod_w, mod_h))
            pygame.draw.rect(s, accent, (x, bar_y - mod_h, mod_w, 3))
            pygame.draw.rect(s, dark,   (x + mod_w - 3, bar_y - mod_h, 3, mod_h))
            for wy in range(bar_y - mod_h + 6, bar_y - 4, 7):
                if rng.random() > 0.4:
                    pygame.draw.rect(s, (255, 230, 120), (x + 5, wy, mod_w - 10, 2))
            x += mod_w + rng.randint(12, 22)
        for sx in range(20, width - 20, 28):
            pygame.draw.rect(s, dark, (sx, bar_y + bar_h, 5, 10))

    elif kind == "ring":
        center_y = height // 2 + 8
        r = 50
        for rr in (r, r - 1, r - 2):
            pygame.draw.circle(s, base, (cx, center_y), rr, 1)
        pygame.draw.rect(s, base, (cx - r - 50, center_y - 5, r * 2 + 100, 10))
        pygame.draw.rect(s, accent, (cx - r - 50, center_y - 5, r * 2 + 100, 2))
        pygame.draw.rect(s, dark,   (cx - r - 50, center_y + 3, r * 2 + 100, 2))
        pygame.draw.rect(s, base, (cx - 3, 12, 6, center_y - 12))
        pygame.draw.circle(s, base, (cx, center_y), 9)
        pygame.draw.circle(s, accent, (cx, center_y), 9, 1)
        for ang_deg in range(0, 360, 22):
            ang = math.radians(ang_deg)
            lx = cx + math.cos(ang) * (r - 4)
            ly = center_y + math.sin(ang) * (r - 4)
            pygame.draw.rect(s, (255, 230, 100), (int(lx), int(ly), 2, 2))

    elif kind == "spire":
        col_w = 30
        col_x = cx - col_w // 2
        pygame.draw.rect(s, base, (col_x, 14, col_w, height - 28))
        pygame.draw.rect(s, accent, (col_x, 14, col_w, 4))
        pygame.draw.rect(s, dark,   (col_x + col_w - 3, 14, 3, height - 28))
        for y in range(26, height - 30, 22):
            ring_w = col_w + 24
            pygame.draw.rect(s, base, (cx - ring_w // 2, y, ring_w, 8))
            pygame.draw.rect(s, accent, (cx - ring_w // 2, y, ring_w, 2))
        pygame.draw.rect(s, accent, (cx - 1, 4, 2, 12))
        pygame.draw.rect(s, (255, 220, 100), (col_x + 8, height // 2 - 1, col_w - 16, 3))
        # symmetric side fins
        for fy in (40, height - 50):
            pygame.draw.polygon(s, dark, [(col_x, fy), (col_x - 14, fy + 6), (col_x, fy + 12)])
            pygame.draw.polygon(s, dark, [(col_x + col_w, fy), (col_x + col_w + 14, fy + 6), (col_x + col_w, fy + 12)])

    else:  # cluster
        candidates = [
            (cx - 110, 70), (cx, 45), (cx + 110, 70),
            (cx - 60, height - 32), (cx + 60, height - 32),
        ]
        rng.shuffle(candidates)
        nodes = candidates[:rng.randint(3, 5)]
        for i in range(len(nodes) - 1):
            pygame.draw.line(s, dark, nodes[i], nodes[i + 1], 5)
        for px, py in nodes:
            mod_w = rng.randint(32, 50)
            mod_h = rng.randint(30, 48)
            rect = pygame.Rect(px - mod_w // 2, py - mod_h // 2, mod_w, mod_h)
            pygame.draw.rect(s, base, rect)
            pygame.draw.rect(s, accent, (rect.x, rect.y, mod_w, 3))
            pygame.draw.rect(s, dark,   (rect.x, rect.y + mod_h - 3, mod_w, 3))
            if mod_w > 30 and mod_h > 30 and rng.random() > 0.3:
                pygame.draw.rect(s, (255, 230, 120), (rect.x + 6, rect.y + 9, mod_w - 12, 3))

    return s


# =============================================================================
# BITMAP FONT
# =============================================================================
#
# Hand-designed 5x7 pixel font. Each glyph is stored as seven 5-character rows
# joined by '/'. BitmapFont scales the glyphs nearest-neighbor at construction
# time, caches per render colour, and exposes a render() signature compatible
# with pygame.font.Font so existing call sites work unchanged.

FONT_5x7 = {
    " ":  "...../...../...../...../...../...../.....",
    "!":  "..#../..#../..#../..#../..#../...../..#..",
    "\"": ".#.#./.#.#./...../...../...../...../.....",
    "#":  ".#.#./.#.#./#####/.#.#./#####/.#.#./.#.#.",
    "$":  "..#../.####/#.#../.###./..#.#/####./..#..",
    "%":  "##..#/##.#./...#./..#../.#.../#.##./#..##",
    "&":  ".##../#..#./#..#./.##../#.#.#/#..#./.##.#",
    "'":  "..#../..#../...../...../...../...../.....",
    "(":  "...#./..#../.#.../.#.../.#.../..#../...#.",
    ")":  ".#.../..#../...#./...#./...#./..#../.#...",
    "*":  "...../..#../#.#.#/.###./#.#.#/..#../.....",
    "+":  "...../..#../..#../#####/..#../..#../.....",
    ",":  "...../...../...../...../..#../..#../.#...",
    "-":  "...../...../...../#####/...../...../.....",
    ".":  "...../...../...../...../...../..#../..#..",
    "/":  "....#/...#./..#../..#../.#.../#..../#....",
    "0":  ".###./#..##/#.#.#/##..#/#..##/#...#/.###.",
    "1":  "..#../.##../..#../..#../..#../..#../.###.",
    "2":  ".###./#...#/....#/...#./..#../.#.../#####",
    "3":  ".###./#...#/....#/..##./....#/#...#/.###.",
    "4":  "...#./..##./.#.#./#..#./#####/...#./...#.",
    "5":  "#####/#..../####./....#/....#/#...#/.###.",
    "6":  "..##./.#.../#..../####./#...#/#...#/.###.",
    "7":  "#####/....#/...#./..#../.#.../.#.../.#...",
    "8":  ".###./#...#/#...#/.###./#...#/#...#/.###.",
    "9":  ".###./#...#/#...#/.####/....#/...#./.##..",
    ":":  "...../..#../..#../...../..#../..#../.....",
    ";":  "...../..#../..#../...../..#../..#../.#...",
    "<":  "....#/...#./..#../.#.../..#../...#./....#",
    "=":  "...../...../#####/...../#####/...../.....",
    ">":  "#..../.#.../..#../...#./..#../.#.../#....",
    "?":  ".###./#...#/....#/...#./..#../...../..#..",
    "@":  ".###./#...#/#..##/#.#.#/#.###/#..../.###.",
    "A":  ".###./#...#/#...#/#####/#...#/#...#/#...#",
    "B":  "####./#...#/#...#/####./#...#/#...#/####.",
    "C":  ".####/#..../#..../#..../#..../#..../.####",
    "D":  "####./#...#/#...#/#...#/#...#/#...#/####.",
    "E":  "#####/#..../#..../####./#..../#..../#####",
    "F":  "#####/#..../#..../####./#..../#..../#....",
    "G":  ".####/#..../#..../#..##/#...#/#...#/.####",
    "H":  "#...#/#...#/#...#/#####/#...#/#...#/#...#",
    "I":  ".###./..#../..#../..#../..#../..#../.###.",
    "J":  "....#/....#/....#/....#/....#/#...#/.###.",
    "K":  "#...#/#..#./#.#../##.../#.#../#..#./#...#",
    "L":  "#..../#..../#..../#..../#..../#..../#####",
    "M":  "#...#/##.##/#.#.#/#.#.#/#...#/#...#/#...#",
    "N":  "#...#/##..#/#.#.#/#..##/#...#/#...#/#...#",
    "O":  ".###./#...#/#...#/#...#/#...#/#...#/.###.",
    "P":  "####./#...#/#...#/####./#..../#..../#....",
    "Q":  ".###./#...#/#...#/#...#/#.#.#/#..#./.##.#",
    "R":  "####./#...#/#...#/####./#.#../#..#./#...#",
    "S":  ".####/#..../#..../.###./....#/....#/####.",
    "T":  "#####/..#../..#../..#../..#../..#../..#..",
    "U":  "#...#/#...#/#...#/#...#/#...#/#...#/.###.",
    "V":  "#...#/#...#/#...#/#...#/#...#/.#.#./..#..",
    "W":  "#...#/#...#/#...#/#.#.#/#.#.#/##.##/#...#",
    "X":  "#...#/#...#/.#.#./..#../.#.#./#...#/#...#",
    "Y":  "#...#/#...#/.#.#./..#../..#../..#../..#..",
    "Z":  "#####/....#/...#./..#../.#.../#..../#####",
    "[":  "..##./..#../..#../..#../..#../..#../..##.",
    "\\": "#..../.#.../.#.../..#../..#../...#./....#",
    "]":  ".##../..#../..#../..#../..#../..#../.##..",
    "^":  "..#../.#.#./#...#/...../...../...../.....",
    "_":  "...../...../...../...../...../...../#####",
    "`":  ".#.../..#../...../...../...../...../.....",
    "a":  "...../...../.###./....#/.####/#...#/.####",
    "b":  "#..../#..../####./#...#/#...#/#...#/####.",
    "c":  "...../...../.####/#..../#..../#..../.####",
    "d":  "....#/....#/.####/#...#/#...#/#...#/.####",
    "e":  "...../...../.###./#...#/####./#..../.####",
    "f":  "..##./.#..#/.#.../####./.#.../.#.../.#...",
    "g":  "...../...../.####/#...#/.####/....#/.###.",
    "h":  "#..../#..../####./#...#/#...#/#...#/#...#",
    "i":  "..#../...../.##../..#../..#../..#../.###.",
    "j":  "....#/...../...##/....#/....#/#...#/.###.",
    "k":  "#..../#..../#..#./#.#../##.../#.#../#..#.",
    "l":  ".##../..#../..#../..#../..#../..#../.###.",
    "m":  "...../...../##.#./#.#.#/#.#.#/#.#.#/#...#",
    "n":  "...../...../####./#...#/#...#/#...#/#...#",
    "o":  "...../...../.###./#...#/#...#/#...#/.###.",
    "p":  "...../...../####./#...#/####./#..../#....",
    "q":  "...../...../.####/#...#/.####/....#/....#",
    "r":  "...../...../#.##./##..#/#..../#..../#....",
    "s":  "...../...../.####/#..../.###./....#/####.",
    "t":  ".#.../.#.../####./.#.../.#.../.#..#/..##.",
    "u":  "...../...../#...#/#...#/#...#/#...#/.####",
    "v":  "...../...../#...#/#...#/#...#/.#.#./..#..",
    "w":  "...../...../#...#/#...#/#.#.#/#.#.#/.#.#.",
    "x":  "...../...../#...#/.#.#./..#../.#.#./#...#",
    "y":  "...../...../#...#/.####/....#/....#/.###.",
    "z":  "...../...../#####/...#./..#../.#.../#####",
    "{":  "...#./..#../..#../.#.../..#../..#../...#.",
    "|":  "..#../..#../..#../..#../..#../..#../..#..",
    "}":  ".#.../..#../..#../...#./..#../..#../.#...",
    "~":  "...../...../.#..#/#.##./...../...../.....",
}


# Mid-size hand-pixeled font: 7 cols x 9 rows base glyph cell. Sits
# between FONT_5x7 scale 1 (7 px line height) and FONT_5x7 scale 2
# (14 px line height) — at scale 1 it's 9 px line height, the natural
# midpoint. Used by the editor for hint rows where the 5x7 scale-2
# would overflow but scale-1 is too dense.
#
# Convention: 1 px padding left + right on most letters (cols 0/6
# blank). Caps + digits fit in rows 1-7 (cap-height = 7); lowercase
# x-height fits rows 3-7; ascenders (b/d/f/h/k/l/t) extend up to
# rows 1-2; descenders (g/j/p/q/y) extend down to rows 8-9.
FONT_7x9 = {
    # Variant A: 7x10 BOLD with 2-px strokes. Cell is 7 cols × 10 rows;
    # caps span rows 0-7 (8 rows tall), lowercase x-height is rows 2-7
    # (6 rows), ascenders use rows 0-7, descenders extend into rows 8-9.
    # The dict + class + family key keep the historical "7x9" name so
    # layout.json and the editors continue to reference the same handle.
    " ":  "......./......./......./......./......./......./......./......./......./.......",
    "!":  "..##.../..##.../..##.../..##.../..##.../......./......./..##.../......./.......",
    '"':  ".##.##./.##.##./......./......./......./......./......./......./......./.......",
    "#":  "......./.##.##./######./.##.##./.##.##./######./.##.##./......./......./.......",
    "$":  "..##.../.######/##...../.#####./.....##/######./...##../......./......./.......",
    "%":  ".##..##/##....#/....##./...##../..##.../.#....#/##...##/......./......./.......",
    "&":  "......./..###../.##.##./..###../.###.../##.##../##..##./.###.##/......./.......",
    "'":  "..##.../..##.../......./......./......./......./......./......./......./.......",
    "(":  "...##../..##.../.##..../.##..../.##..../.##..../..##.../...##../......./.......",
    ")":  "..##.../...##../....##./....##./....##./....##./...##../..##.../......./.......",
    "*":  "......./.##.##./..###../#######/..###../.##.##./......./......./......./.......",
    "+":  "......./...##../...##../#######/#######/...##../...##../......./......./.......",
    ",":  "......./......./......./......./......./......./..##.../..##.../.##..../.......",
    "-":  "......./......./......./......./######./######./......./......./......./.......",
    ".":  "......./......./......./......./......./......./......./..##.../......./.......",
    "/":  ".....##/.....##/....##./...##../...##../..##.../.##..../.##..../......./.......",
    "0":  ".#####./##...##/##...##/##.#.##/##.#.##/##...##/##...##/.#####./......./.......",
    "1":  "...##../.####../...##../...##../...##../...##../...##../######./......./.......",
    "2":  ".#####./##...##/....##./...##../..##.../.##..../##...../#######/......./.......",
    "3":  ".#####./##...##/....##./..####./....##./....##./##...##/.#####./......./.......",
    "4":  "....##./...###./..#.##./.#..##./##..##./#######/....##./....##./......./.......",
    "5":  "#######/##...../##...../######./.....##/.....##/##...##/.#####./......./.......",
    "6":  ".######/##...../##...../######./##...##/##...##/##...##/.#####./......./.......",
    "7":  "#######/.....##/....##./...##../..##.../.##..../##...../##...../......./.......",
    "8":  ".#####./##...##/##...##/.#####./.#####./##...##/##...##/.#####./......./.......",
    "9":  ".#####./##...##/##...##/##...##/.######/.....##/.....##/.#####./......./.......",
    ":":  "......./......./......./..##.../......./......./..##.../......./......./.......",
    ";":  "......./......./......./..##.../......./......./..##.../..##.../.##..../.......",
    "<":  "......./....##./...##../..##.../.##..../..##.../...##../....##./......./.......",
    "=":  "......./......./######./######./......./######./######./......./......./.......",
    ">":  "......./.##..../..##.../...##../....##./...##../..##.../.##..../......./.......",
    "?":  ".#####./##...##/....##./...##../..##.../..##.../......./..##.../......./.......",
    "@":  ".#####./##...##/##.####/##.#.##/##.####/##...../.#####./......./......./.......",
    "A":  "..###../.##.##./##...##/##...##/#######/#######/##...##/##...##/......./.......",
    "B":  "######./##...##/##...##/######./######./##...##/##...##/######./......./.......",
    "C":  ".######/##...##/##...../##...../##...../##...../##...##/.######/......./.......",
    "D":  "#####../##..##./##...##/##...##/##...##/##...##/##..##./#####../......./.......",
    "E":  "#######/##...../##...../######./######./##...../##...../#######/......./.......",
    "F":  "#######/##...../##...../######./######./##...../##...../##...../......./.......",
    "G":  ".######/##...##/##...../##...../##.####/##...##/##...##/.######/......./.......",
    "H":  "##...##/##...##/##...##/#######/#######/##...##/##...##/##...##/......./.......",
    "I":  "#######/#######/..###../..###../..###../..###../#######/#######/......./.......",
    "J":  "....###/....###/....###/....###/....###/##..###/##..###/.#####./......./.......",
    "K":  "##...##/##..##./##.##../####.../####.../##.##../##..##./##...##/......./.......",
    "L":  "##...../##...../##...../##...../##...../##...../##...../#######/......./.......",
    "M":  "##...##/###.###/##.#.##/##.#.##/##...##/##...##/##...##/##...##/......./.......",
    "N":  "##...##/###..##/###..##/##.#.##/##.#.##/##..###/##..###/##...##/......./.......",
    "O":  ".#####./##...##/##...##/##...##/##...##/##...##/##...##/.#####./......./.......",
    "P":  "######./##...##/##...##/######./##...../##...../##...../##...../......./.......",
    "Q":  ".#####./##...##/##...##/##...##/##...##/##.#.##/##..##./.####.#/......./.......",
    "R":  "######./##...##/##...##/######./##.##../##.##../##..##./##...##/......./.......",
    "S":  ".######/##...../##...../######./.######/.....##/##...##/######./......./.......",
    "T":  "#######/#######/..###../..###../..###../..###../..###../..###../......./.......",
    "U":  "##...##/##...##/##...##/##...##/##...##/##...##/##...##/.#####./......./.......",
    "V":  "##...##/##...##/##...##/##...##/##...##/.##.##./.##.##./..###../......./.......",
    "W":  "##...##/##...##/##...##/##...##/##.#.##/##.#.##/###.###/##...##/......./.......",
    "X":  "##...##/##...##/.##.##./..###../..###../.##.##./##...##/##...##/......./.......",
    "Y":  "##...##/##...##/.##.##./..###../..###../..###../..###../..###../......./.......",
    "Z":  "#######/#######/....##./...##../..##.../.##..../#######/#######/......./.......",
    "[":  ".####../.##..../.##..../.##..../.##..../.##..../.##..../.####../......./.......",
    "\\": ".##..../.##..../..##.../...##../...##../....##./.....##/.....##/......./.......",
    "]":  "..####./....##./....##./....##./....##./....##./....##./..####./......./.......",
    "^":  "..##.../.####../##..##./......./......./......./......./......./......./.......",
    "_":  "......./......./......./......./......./......./......./......./......./#######",
    "`":  ".##..../..##.../......./......./......./......./......./......./......./.......",
    "a":  "......./......./.#####./.....##/.######/##...##/##...##/.######/......./.......",
    "b":  "##...../##...../######./##...##/##...##/##...##/##...##/######./......./.......",
    "c":  "......./......./.######/##...##/##...../##...../##...##/.######/......./.......",
    "d":  ".....##/.....##/.######/##...##/##...##/##...##/##...##/.######/......./.......",
    "e":  "......./......./.######/##...##/#######/##...../##...##/.######/......./.......",
    "f":  "..####./.##..../.##..../######./.##..../.##..../.##..../.##..../......./.......",
    "g":  "......./......./.######/##...##/##...##/##...##/##...##/.######/.....##/######.",
    "h":  "##...../##...../######./##...##/##...##/##...##/##...##/##...##/......./.......",
    "i":  "...##../......./..###../...##../...##../...##../...##../.######/......./.......",
    "j":  "....##./......./....##./....##./....##./....##./....##./....##./....##./######.",
    "k":  "##...../##...../##..##./##.##../####.../##.##../##..##./##...##/......./.......",
    "l":  ".###.../...##../...##../...##../...##../...##../...##../..####./......./.......",
    "m":  "......./......./######./##.#.##/##.#.##/##.#.##/##.#.##/##.#.##/......./.......",
    "n":  "......./......./######./##...##/##...##/##...##/##...##/##...##/......./.......",
    "o":  "......./......./.#####./##...##/##...##/##...##/##...##/.#####./......./.......",
    "p":  "......./......./######./##...##/##...##/##...##/##...##/######./##...../##.....",
    "q":  "......./......./.######/##...##/##...##/##...##/##...##/.######/.....##/.....##",
    "r":  "......./......./##.###./###..##/##...../##...../##...../##...../......./.......",
    "s":  "......./......./.######/##...../.#####./.....##/.....##/######./......./.......",
    "t":  "..##.../######./..##.../..##.../..##.../..##.../..##.../...##../......./.......",
    "u":  "......./......./##...##/##...##/##...##/##...##/##...##/.######/......./.......",
    "v":  "......./......./##...##/##...##/##...##/.##.##./.##.##./..###../......./.......",
    "w":  "......./......./##...##/##...##/##.#.##/##.#.##/###.###/##...##/......./.......",
    "x":  "......./......./##...##/.##.##./..###../..###../.##.##./##...##/......./.......",
    "y":  "......./......./##...##/##...##/.##.##./.##.##./..###../..###../....##./######.",
    "z":  "......./......./######./....##./...##../..##.../.##..../######./......./.......",
    "{":  "....##./...##../...##../..##.../...##../...##../....##./......./......./.......",
    "|":  "...##../...##../...##../...##../...##../...##../...##../...##../...##../...##..",
    "}":  ".##..../..##.../..##.../...##../..##.../..##.../.##..../......./......./.......",
    "~":  "......./......./.###..#/##.####/......#/......./......./......./......./.......",
}


def _draw_dpad_icon(surf, x, y, scale=1, color=(255, 255, 255)):
    """Draw a square D-pad cross at (x, y) - symmetric 7x7 plus with
    3-cell-thick arms. Both arms are equal length (7 cells / 7*scale px),
    so the shape reads as an unmistakable + at any scale."""
    # Vertical arm: cols 2..4 (3 wide), rows 0..6 (full 7 tall).
    pygame.draw.rect(surf, color, (x + 2 * scale, y, 3 * scale, 7 * scale))
    # Horizontal arm: cols 0..6 (full 7 wide), rows 2..4 (3 tall).
    pygame.draw.rect(surf, color, (x, y + 2 * scale, 7 * scale, 3 * scale))


def _glyph_to_surface(pattern, scale, color):
    """Render a glyph from a row/row/... pattern at the given scale,
    applying a vertical bevel gradient. Width is read from the first
    row so the same helper handles 5x7, 7x9, and any future glyph set.

    Gradient: brightest band sits at ~1/3 from the top, darkens both up
    AND down from there — same energy as light striking a tilted bezel.
    Tuned so even short glyphs (scale 1) keep a visible peak."""
    rows = pattern.split("/")
    h_rows = len(rows)
    w_cols = len(rows[0]) if rows else 0
    w = w_cols * scale
    h = h_rows * scale
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    has_alpha = len(color) >= 4
    base_a = color[3] if has_alpha else 255
    br, bg, bb = color[0], color[1], color[2]
    PEAK_T = 1.0 / 3.0
    TOP_FACTOR = 0.80
    PEAK_FACTOR = 1.16
    BOT_FACTOR = 0.72
    row_colors = []
    for py in range(h):
        t = py / max(1, h - 1)
        if t <= PEAK_T:
            factor = TOP_FACTOR + (PEAK_FACTOR - TOP_FACTOR) * (t / PEAK_T)
        else:
            factor = PEAK_FACTOR - (PEAK_FACTOR - BOT_FACTOR) \
                     * ((t - PEAK_T) / (1.0 - PEAK_T))
        r = min(255, max(0, int(br * factor)))
        g = min(255, max(0, int(bg * factor)))
        b = min(255, max(0, int(bb * factor)))
        row_colors.append((r, g, b, base_a) if has_alpha else (r, g, b))
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != "#":
                continue
            px = x * scale
            for dy in range(scale):
                py = y * scale + dy
                s.fill(row_colors[py], (px, py, scale, 1))
    return s


class BitmapFont:
    """Hand-pixeled 5x7 font scaled nearest-neighbor. Render API mirrors
    pygame.font.Font.render(text, antialias, color, background=None) closely
    enough to drop in for every existing call site - antialias is ignored,
    background is ignored, multi-line text is rendered as a single line."""
    BASE_W = 5
    BASE_H = 7
    SPACING = 1   # extra pixels between glyphs at 1x
    # Extra pattern rows ABOVE BASE_H that live below the baseline for
    # descender glyphs (g, p, q, y, j). FONT_5x7 fits its descender inside
    # the 7-row cell already, so DESCENDER_ROWS = 0; FONT_7x9 (now actually
    # 7x10 patterns with BASE_H=8) needs 2 extra rows. Callers that want
    # the full rendered glyph height (for bounding boxes, render surface
    # sizing) should use `full_height`, not `line_height`.
    DESCENDER_ROWS = 0

    def __init__(self, scale=2):
        self.scale = scale
        self.advance = (self.BASE_W + self.SPACING) * scale
        self.line_height = self.BASE_H * scale
        self.full_height = (self.BASE_H + self.DESCENDER_ROWS) * self.scale
        self._color_cache = {}
        # Cache of fully-rendered text surfaces, keyed by (text, color).
        # The HUD re-renders many static labels (and slowly-changing dynamic
        # ones like score / time) every frame — caching avoids reallocating
        # an SRCALPHA surface and re-blitting glyphs each time. Bounded FIFO
        # so dynamic strings can't grow it without limit.
        self._render_cache = {}
        self._render_cache_order = []
        self._render_cache_max = 256

    def get_height(self):
        return self.line_height

    def get_linesize(self):
        return self.line_height + self.scale

    def _glyphs(self, color):
        key = tuple(color[:3])
        cache = self._color_cache.get(key)
        if cache is None:
            cache = {}
            for ch, pat in FONT_5x7.items():
                cache[ch] = _glyph_to_surface(pat, self.scale, color)
            self._color_cache[key] = cache
        return cache

    # Common unicode chars used by the editors / debug overlays mapped to
    # single-char ASCII equivalents so they render with the 5x7 glyph set
    # (which is ASCII-only) without changing text width.
    _ASCII_FALLBACK = str.maketrans({
        "→": ">",   # ->
        "←": "<",   # <-
        "↑": "^",   # up arrow
        "↓": "v",   # down arrow
        "↔": "~",   # <->
        "—": "-",   # em dash
        "–": "-",   # en dash
        "−": "-",   # math minus
        "·": ".",   # middle dot
        "▸": ">",   # right-pointing small triangle
        "●": "*",   # filled circle
        "○": "o",   # empty circle
        "±": "~",   # plus-minus
        "×": "x",   # multiplication sign
        "…": "~",   # ellipsis (one-char placeholder)
        "✓": "v",   # checkmark
    })

    def size(self, text):
        return (max(1, len(text) * self.advance - self.scale), self.line_height)

    def draw(self, surf, x, y, text, color):
        """Draw `text` directly onto `surf` at (x, y) without allocating
        a per-string buffer Surface. Skips the alloc + intermediate blit
        that render() pays per uncached string — useful for HUD fields
        whose content changes every frame (score, credits, time).

        Caller responsibilities:
        - alpha must be applied to `surf`-side (this draws opaque glyphs).
          For per-string alpha use render() + surf.set_alpha(); or blit
          onto an SRCALPHA scratch surface and set its alpha.
        - (x, y) is the top-left of the text box; pre-measure with
          size() if you need to centre/right-align.
        - Returns the on-screen width so callers can chain draws."""
        text = str(text).translate(self._ASCII_FALLBACK)
        if not text:
            return 0
        glyphs = self._glyphs(color)
        space_glyph = glyphs.get(" ")
        advance = self.advance
        blit = surf.blit
        cx = x
        for c in text:
            g = glyphs.get(c)
            if g is None:
                g = glyphs.get(c.upper()) if c.isalpha() else None
            if g is None:
                g = space_glyph
            if g is not None:
                blit(g, (cx, y))
            cx += advance
        return cx - x - self.scale

    def render(self, text, antialias, color, background=None):
        text = str(text).translate(self._ASCII_FALLBACK)
        cache_key = None
        if background is None and len(text) <= 48:
            cache_key = (text, color[0], color[1], color[2])
            cached = self._render_cache.get(cache_key)
            if cached is not None:
                return cached
        glyphs = self._glyphs(color)
        chars = list(text)
        total_w = max(1, len(chars) * self.advance - self.scale)
        # Surface must be tall enough to fit the descender area of glyphs
        # like p / g / q / y / j — using line_height (cap-only) would clip
        # the bottom DESCENDER_ROWS of every blit. Anchor math in callers
        # uses img.get_height() so this also fixes their vertical centring.
        surf = pygame.Surface((total_w, self.full_height), pygame.SRCALPHA)
        if background is not None:
            surf.fill(background)
        space_glyph = glyphs.get(" ")
        x = 0
        for c in chars:
            g = glyphs.get(c)
            if g is None:
                g = glyphs.get(c.upper()) if c.isalpha() else None
            if g is None:
                g = space_glyph
            if g is not None:
                surf.blit(g, (x, 0))
            x += self.advance
        if cache_key is not None:
            self._render_cache[cache_key] = surf
            self._render_cache_order.append(cache_key)
            if len(self._render_cache_order) > self._render_cache_max:
                evict = self._render_cache_order.pop(0)
                self._render_cache.pop(evict, None)
        return surf


class BitmapFont7x9(BitmapFont):
    """Mid-size BOLD glyph family. The class + family-key are still named
    "7x9" for backward compatibility with layout.json / the editors, but
    the underlying patterns are now 7 cols × 10 rows with 2-px strokes
    (FONT_7x9 dict). At scale 1 the line height is 8 px (cap height);
    descenders use 2 extra pattern rows below the baseline (DESCENDER_ROWS).
    Same render API and unicode fallback table as the base font."""
    BASE_W = 7
    BASE_H = 8
    DESCENDER_ROWS = 2

    def _glyphs(self, color):
        key = tuple(color[:3])
        cache = self._color_cache.get(key)
        if cache is None:
            cache = {}
            for ch, pat in FONT_7x9.items():
                cache[ch] = _glyph_to_surface(pat, self.scale, color)
            self._color_cache[key] = cache
        return cache


def make_vignette():
    """Subtle dark falloff at playfield edges. Pre-rendered once."""
    v = pygame.Surface((PLAY_W, PLAY_H), pygame.SRCALPHA)
    edge = 40
    for i in range(edge):
        alpha = int(80 * (1 - i / edge) ** 2)
        pygame.draw.rect(v, (0, 0, 0, alpha), (i, i, PLAY_W - i * 2, PLAY_H - i * 2), 1)
    return v


def make_launch_pad(sector_idx):
    """Edge-to-edge sector-themed launch platform for level intros. Wider and
    flatter than make_station - reads as a runway / launch deck the ship lifts
    off from rather than a freestanding station."""
    base, accent, dark = STATION_PALETTES[sector_idx % len(STATION_PALETTES)]
    w = PLAY_W
    h = 70
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    # Main hull plate spanning the full width.
    pygame.draw.rect(s, dark, (0, 0, w, h))
    pygame.draw.rect(s, base, (0, 4, w, h - 4))
    pygame.draw.rect(s, accent, (0, 4, w, 4))
    pygame.draw.rect(s, dark, (0, h - 4, w, 4))
    # Vertical panel divisions.
    for x in range(60, w, 60):
        pygame.draw.line(s, dark, (x, 6), (x, h - 6), 1)
        pygame.draw.line(s, accent, (x + 1, 6), (x + 1, h - 6), 1)
    # Central launch bay - a darker recess with bright trim where the ship
    # comes out.
    bay_w = 88
    bay_x = (w - bay_w) // 2
    pygame.draw.rect(s, dark, (bay_x, 0, bay_w, 14))
    pygame.draw.rect(s, accent, (bay_x, 0, bay_w, 2))
    pygame.draw.rect(s, dark, (bay_x + bay_w - 3, 0, 3, 14))
    # Beacon lights flanking the bay (yellow).
    for x in (bay_x - 12, bay_x + bay_w + 9):
        pygame.draw.rect(s, (255, 230, 100), (x, 10, 4, 4))
    # Two structural pylons reaching the top edge.
    for ax in (40, w - 44):
        pygame.draw.rect(s, dark, (ax, 0, 4, h))
        pygame.draw.rect(s, accent, (ax - 4, 8, 12, 4))
    # Rivets along the lower trim.
    for rx in range(20, w - 20, 16):
        pygame.draw.rect(s, accent, (rx, h - 8, 2, 2))
    # A safety stripe near the front edge.
    pygame.draw.rect(s, (220, 200, 70), (0, h - 14, w, 4))
    for sx in range(8, w, 16):
        pygame.draw.rect(s, dark, (sx, h - 14, 8, 4))
    return s


_LOGO_GLYPHS = {
    "P": [
        "######.",
        "#.....#",
        "#.....#",
        "#.....#",
        "######.",
        "#......",
        "#......",
        "#......",
        "#......",
    ],
    "E": [
        "#######",
        "#......",
        "#......",
        "#......",
        "#####..",
        "#......",
        "#......",
        "#......",
        "#######",
    ],
    "W": [
        "#.....#",
        "#.....#",
        "#.....#",
        "#.....#",
        "#..#..#",
        "#..#..#",
        "##.#.##",
        "##...##",
        ".#...#.",
    ],
}


def _make_gloss_stripe(height, stripe_w=70, peak=140):
    """A narrow vertical bell-curve highlight column. Blitted with
    `BLEND_RGB_ADD` later so the bright RGB only brightens where the
    underlying sprite has visible pixels — transparent regions keep their
    alpha=0 and stay invisible. `peak` is the maximum brightness added at
    the centre column (lower = subtler shine)."""
    surf = pygame.Surface((stripe_w, height), pygame.SRCALPHA)
    half = stripe_w / 2
    for x in range(stripe_w):
        t = (x - half) / half  # -1..+1 across the stripe
        intensity = max(0.0, 1.0 - t * t)
        v = int(peak * intensity)
        pygame.draw.line(surf, (v, v, v, 255), (x, 0), (x, height - 1))
    return surf


def _make_yellow_mask(surf):
    """Returns an opaque RGB surface — white wherever `surf` is warm
    (yellow / gold / orange), black elsewhere. Used as a multiplicative
    mask under the gloss stripe so the title-screen shine only lights up
    the warm parts of the title and leaves the cyan PEWPEW body / dark
    background alone. Thresholds are intentionally loose so dull golds
    and rim-lit ochres still register."""
    w, h = surf.get_size()
    mask = pygame.Surface((w, h)).convert()
    mask.fill((0, 0, 0))
    for y in range(h):
        for x in range(w):
            r, g, b, a = surf.get_at((x, y))
            if a < 128:
                continue
            # Warm bias: red dominant, more red than blue, not too blue.
            if r >= 120 and r > b + 30 and r >= g and b <= 170:
                mask.set_at((x, y), (255, 255, 255))
    return mask


def _make_yellow_dim_layer(yellow_mask, dim_amount=191):
    """RGB surface — (dim_amount, dim_amount, dim_amount) where the mask is
    white, (0,0,0) elsewhere. Subtract from a 255-fill to land yellow
    pixels at `255 - dim_amount` (default 64 ≈ 25% brightness)."""
    w, h = yellow_mask.get_size()
    layer = pygame.Surface((w, h)).convert()
    layer.fill((dim_amount, dim_amount, dim_amount))
    layer.blit(yellow_mask, (0, 0), special_flags=pygame.BLEND_MULT)
    return layer


def make_logo(text="PEWPEW", scale=7, color=(120, 220, 255), shadow=(0, 0, 0, 200)):
    glyph_w = 7
    glyph_h = 9
    spacing = 1
    n = len(text)
    base_w = n * glyph_w + (n - 1) * spacing
    base = pygame.Surface((base_w, glyph_h), pygame.SRCALPHA)
    for i, ch in enumerate(text):
        glyph = _LOGO_GLYPHS.get(ch)
        if not glyph:
            continue
        x0 = i * (glyph_w + spacing)
        for y, row in enumerate(glyph):
            for x, c in enumerate(row):
                if c == "#":
                    base.set_at((x0 + x, y), color)
    big = pygame.transform.scale(base, (base_w * scale, glyph_h * scale))
    # color-fill gradient (top brighter, bottom darker) via per-row darken
    grad = pygame.Surface(big.get_size(), pygame.SRCALPHA)
    for row in range(big.get_height()):
        t = row / max(1, big.get_height() - 1)
        darken = int(80 * t)
        line_color = (0, 0, 0, darken)
        pygame.draw.line(grad, line_color, (0, row), (big.get_width(), row))
    big.blit(grad, (0, 0))
    # compose with shadow offset
    out = pygame.Surface((big.get_width() + scale, big.get_height() + scale), pygame.SRCALPHA)
    sil = make_silhouette(big, shadow)
    out.blit(sil, (scale, scale))
    out.blit(big, (0, 0))
    return out


# =============================================================================
# BULLETS / PROJECTILES
# =============================================================================

class Bullet:
    # AI projectile-glyph sprites, populated at startup. Keyed by glyph name.
    _glyphs = {}

    @classmethod
    def set_glyphs(cls, glyphs):
        cls._glyphs = glyphs or {}

    __slots__ = ("x", "y", "vx", "vy", "color", "size", "friendly", "alive",
                 "rect", "damage", "pierce", "sprite", "weapon_kind", "ricocheted")

    def __init__(self, x, y, vx, vy, color, friendly=True, size=(3, 7), damage=1, pierce=0,
                 weapon_kind=None):
        self.x = float(x)
        self.y = float(y)
        self.vx = vx
        self.vy = vy
        self.color = color
        # Uniform 1.5x play-area scale: bullet visual + collision rect grow,
        # but vx/vy stay the same so cross-screen travel time is unchanged.
        self.size = (_ps(size[0]), _ps(size[1]))
        self.friendly = friendly
        self.alive = True
        self.damage = damage
        self.pierce = pierce
        # weapon_kind tags player bullets with the main weapon that fired
        # them ("pulse"/"spread"/"vulcan"/"missile"/"drone"), so colored
        # enemy shields can decide ricochet vs damage. None = untyped.
        self.weapon_kind = weapon_kind
        # Set true once we've reflected off a shield — flips friendly so the
        # bullet can hurt the player on the way back, and prevents recursive
        # bounce.
        self.ricocheted = False
        # Pick a glyph sprite based on (friendly, dominant colour). Subclasses
        # like Missile override this after super().__init__().
        self.sprite = self._select_sprite(color, friendly)
        self.rect = pygame.Rect(int(x) - self.size[0] // 2,
                                int(y) - self.size[1] // 2,
                                self.size[0], self.size[1])

    @classmethod
    def _select_sprite(cls, color, friendly):
        if not cls._glyphs:
            return None
        # Enemy projectiles stay procedural — the sliced pellet sprites carry
        # too much label/text noise from the contact sheet to read cleanly at
        # small sizes. Friendly player bullets do use AI glyphs.
        if not friendly:
            return None
        r, g, b = color[0], color[1], color[2]
        # cyan -> pulse, orange -> spread, yellow -> vulcan, pale-blue -> drone
        if g > 200 and b > 200 and r < 150:
            return cls._glyphs.get("glyph_pulse")
        if r > 200 and g > 180 and b < 130:
            return cls._glyphs.get("glyph_vulcan")
        if r > 200 and 120 < g < 180 and b < 120:
            return cls._glyphs.get("glyph_spread")
        if r > 150 and g > 200 and b > 240:
            return cls._glyphs.get("glyph_drone")
        return cls._glyphs.get("glyph_pulse")

    def update(self, dt):
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.rect.x = int(self.x) - self.size[0] // 2
        self.rect.y = int(self.y) - self.size[1] // 2
        if self.x < -20 or self.x > PLAY_W + 20 or self.y < -20 or self.y > PLAY_H + 20:
            self.alive = False

    def batch_blit_info(self):
        """Return (sprite, topleft) for fast Surface.blits() batching, or
        None to signal the caller should fall through to .draw() for
        special cases (procedural enemy bullets, anything needing per-
        frame flip/rotate/scale)."""
        sprite = self.sprite
        if sprite is None:
            return None
        # The flip path only ever triggers for enemy bullets, which carry
        # sprite=None anyway — but the guard stays in case _select_sprite
        # ever returns one for a downward-aimed bullet.
        if not self.friendly and self.vy > 0:
            return None
        cx = self.rect.centerx
        cy = self.rect.centery
        return (sprite, (cx - sprite.get_width() // 2,
                         cy - sprite.get_height() // 2))

    def draw(self, surf):
        # If we have an AI glyph for this bullet, blit it at the glyph's
        # NATIVE size centred on the collision rect. The collision rect stays
        # at bullet size for gameplay-balanced hit detection, but the visual
        # uses the glyph's intended pixels so it reads cleanly.
        if self.sprite is not None:
            sprite = self.sprite
            # Enemy bullets aim downward — flip the glyph so the trail
            # tail trails behind their travel direction.
            if not self.friendly and self.vy > 0:
                sprite = pygame.transform.flip(sprite, False, True)
            sw = sprite.get_width()
            sh = sprite.get_height()
            surf.blit(sprite, (self.rect.centerx - sw // 2,
                               self.rect.centery - sh // 2))
            return
        # Fallback: trail-and-core procedural draw.
        r, g, b = self.color[0], self.color[1], self.color[2]
        sx = self.size[0]
        sy = self.size[1]
        norm = max(1.0, math.hypot(self.vx, self.vy))
        step_dx = -self.vx / norm
        step_dy = -self.vy / norm
        for i in (3, 2, 1):
            shade = 1.0 - i * 0.25
            tc = (max(0, int(r * shade)), max(0, int(g * shade)), max(0, int(b * shade)))
            tx = int(self.x + step_dx * i * 5) - sx // 2
            ty = int(self.y + step_dy * i * 5) - sy // 2
            tw = max(1, sx - i)
            th = max(1, sy - i)
            pygame.draw.rect(surf, tc, (tx + (sx - tw) // 2, ty + (sy - th) // 2, tw, th))
        pygame.draw.rect(surf, self.color, self.rect)
        if sx >= 3 and sy >= 3:
            pygame.draw.rect(surf, WHITE, (self.rect.x + sx // 2 - 1, self.rect.y + 1, 2, max(1, sy - 2)))


class Missile(Bullet):
    # Missile fields: same slots as Bullet plus the tracking-specific extras.
    __slots__ = ("target_ref", "turn", "life")

    # Pre-rotated sprite cache: missile heading is recomputed every frame,
    # so the original Missile.draw paid pygame.transform.scale +
    # transform.rotate per missile per frame (~100-300 us each on the
    # mali driver). Pre-build 32 angle buckets once and look up by heading
    # instead.
    _ROTATION_BUCKETS = 32
    _ROTATION_STEP = 360.0 / _ROTATION_BUCKETS
    _rotated_sprites = None       # tuple[Surface] of length _ROTATION_BUCKETS
    _rotated_size = None          # the (sx, sy) the cache was built for

    @classmethod
    def _ensure_rotation_cache(cls, src_sprite, size):
        if cls._rotated_sprites is not None and cls._rotated_size == size:
            return
        scaled = pygame.transform.scale(src_sprite, size)
        cls._rotated_sprites = tuple(
            pygame.transform.rotate(scaled, i * cls._ROTATION_STEP)
            for i in range(cls._ROTATION_BUCKETS)
        )
        cls._rotated_size = size

    def __init__(self, x, y, target_ref, color=(255, 200, 80), damage=200,
                 weapon_kind="missile"):
        super().__init__(x, y, 0, -200, color, friendly=True, size=(4, 9), damage=damage,
                         weapon_kind=weapon_kind)
        # Prefer the dedicated tracker glyph if it's loaded.
        tracker = self._glyphs.get("glyph_tracker") if self._glyphs else None
        if tracker is not None:
            self.sprite = tracker
        self.target_ref = target_ref
        self.turn = 5.0
        self.life = 3.5

    def update(self, dt):
        self.life -= dt
        if self.life <= 0:
            self.alive = False
            return
        target = self.target_ref()
        if target is not None:
            tx = target.rect.centerx
            ty = target.rect.centery
            angle = math.atan2(ty - self.y, tx - self.x)
            cur_angle = math.atan2(self.vy, self.vx)
            diff = (angle - cur_angle + math.pi) % (2 * math.pi) - math.pi
            cur_angle += clamp(diff, -self.turn * dt, self.turn * dt)
            speed = math.hypot(self.vx, self.vy) + 40 * dt
            speed = min(speed, 320)
            self.vx = math.cos(cur_angle) * speed
            self.vy = math.sin(cur_angle) * speed
        super().update(dt)

    def _rotated_for_heading(self):
        """Returns the pre-rotated sprite that matches the missile's
        current heading bucket (or None if no sprite is loaded)."""
        if self.sprite is None:
            return None
        Missile._ensure_rotation_cache(self.sprite, self.size)
        angle_deg = (-math.degrees(math.atan2(self.vy, self.vx)) - 90) % 360.0
        bucket = int((angle_deg + self._ROTATION_STEP * 0.5)
                     / self._ROTATION_STEP) % self._ROTATION_BUCKETS
        return self._rotated_sprites[bucket]

    def batch_blit_info(self):
        sprite = self._rotated_for_heading()
        if sprite is None:
            return None
        cx = self.rect.centerx
        cy = self.rect.centery
        return (sprite, (cx - sprite.get_width() // 2,
                         cy - sprite.get_height() // 2))

    def draw(self, surf):
        sprite = self._rotated_for_heading()
        if sprite is not None:
            cx = self.rect.centerx
            cy = self.rect.centery
            surf.blit(sprite, (cx - sprite.get_width() // 2,
                               cy - sprite.get_height() // 2))
            return
        # Procedural fallback when no tracker glyph is loaded.
        pygame.draw.rect(surf, self.color, self.rect)
        tail_y = int(self.y - self.vy * 0.02)
        tail_x = int(self.x - self.vx * 0.02)
        pygame.draw.line(surf, (255, 100, 40),
                         (tail_x, tail_y),
                         (int(self.x), int(self.y)), 2)


class Laser:
    """Continuous beam (player mega-laser ability)."""
    def __init__(self, owner):
        self.owner = owner
        self.life = 2.0
        self.alive = True
        self.width = 18
        self.damage_per_sec = 8000
        self.tick = 0

    def update(self, dt):
        self.life -= dt
        if self.life <= 0:
            self.alive = False
        self.tick += dt

    def hit_rect(self):
        cx = self.owner.rect.centerx
        return pygame.Rect(cx - self.width // 2, 0, self.width, self.owner.rect.top)

    def draw(self, surf):
        cx = self.owner.rect.centerx
        top = 0
        bottom = self.owner.rect.top
        pulse = 1.0 + 0.3 * math.sin(self.tick * 30)
        w = int(self.width * pulse)
        core = pygame.Rect(cx - w // 2, top, w, bottom - top)
        glow = pygame.Rect(cx - w, top, w * 2, bottom - top)
        s = pygame.Surface(glow.size, pygame.SRCALPHA)
        s.fill((180, 220, 255, 70))
        surf.blit(s, glow.topleft)
        pygame.draw.rect(surf, (200, 240, 255), core)
        pygame.draw.rect(surf, WHITE, (cx - 2, top, 4, bottom - top))


# =============================================================================
# PARTICLES / PICKUPS
# =============================================================================

class Particle:
    __slots__ = ("x", "y", "vx", "vy", "life", "max_life", "color", "size")

    def __init__(self, x, y, color, size=3, speed_range=(40, 220), life_range=(0.25, 0.65)):
        self.x = x
        self.y = y
        ang = random.uniform(0, math.tau)
        spd = random.uniform(*speed_range)
        self.vx = math.cos(ang) * spd
        self.vy = math.sin(ang) * spd
        self.life = random.uniform(*life_range)
        self.max_life = self.life
        self.color = color
        self.size = size

    def update(self, dt):
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.vx *= 0.92
        self.vy *= 0.92
        self.life -= dt

    @property
    def alive(self):
        return self.life > 0

    def draw(self, surf):
        a = max(0.0, self.life / self.max_life)
        size = max(1, int(self.size * a))
        pygame.draw.rect(surf, self.color, (int(self.x), int(self.y), size, size))


class Spark(Particle):
    """Short-lived fast spark for bullet impacts; brighter, smaller."""
    def __init__(self, x, y, color):
        super().__init__(x, y, color, size=2, speed_range=(100, 280), life_range=(0.10, 0.22))


# Warm fireworks palette for projectile-impact sparkles.
IMPACT_SPARK_COLORS = (
    (255, 220, 120),
    (255, 180, 80),
    (255, 240, 200),
    (255, 255, 255),
    (255, 200, 60),
    (255, 150, 50),
)


class FloatText:
    """Short-lived text that drifts upward, pops out + settles, and
    fades. Used to surface instant feedback the HUD doesn't already
    show — e.g. the credit amount earned from a kill or pickup. Font
    is shared class-side so we don't lug an extra ref through every
    spawn site."""
    _font = None
    _base_cache = {}     # (text, color) -> base-rendered surface

    # Pop animation curve. Phase 1: snap-in from 0 to PEAK over POP_IN_T
    # seconds. Phase 2: settle from PEAK to 1.0 over POP_SETTLE_T. Phase
    # 3: hold at 1.0 + drift + fade.
    POP_IN_T = 0.08
    POP_SETTLE_T = 0.18
    POP_PEAK = 2.0

    @classmethod
    def set_font(cls, font):
        cls._font = font
        cls._base_cache.clear()

    __slots__ = ("x", "y", "vy", "life", "max_life", "text", "color")

    def __init__(self, x, y, text, color=(255, 230, 110), life=1.0):
        self.x = float(x)
        self.y = float(y)
        self.vy = -42.0
        self.life = life
        self.max_life = life
        self.text = str(text)
        self.color = color

    def update(self, dt):
        self.y += self.vy * dt
        self.vy *= 0.94    # gentle deceleration
        self.life -= dt

    @property
    def alive(self):
        return self.life > 0

    def _scale(self):
        """Pop-then-settle curve. Starts at 0, overshoots to PEAK, settles
        to 1.0; stays at 1.0 for the rest of the lifetime."""
        age = self.max_life - self.life
        if age < self.POP_IN_T:
            return (age / self.POP_IN_T) * self.POP_PEAK
        if age < self.POP_IN_T + self.POP_SETTLE_T:
            t = (age - self.POP_IN_T) / self.POP_SETTLE_T
            return self.POP_PEAK - (self.POP_PEAK - 1.0) * t
        return 1.0

    def draw(self, surf):
        font = self._font
        if font is None:
            return
        # Hold full alpha for the first ~third of life, then fade. Render
        # the base text once per (text, color) and reuse — scale + alpha
        # are applied per-frame on top so the cache stays compact.
        t_life = max(0.0, self.life / self.max_life)
        alpha_f = 1.0 if t_life > 0.66 else (t_life / 0.66)
        alpha = max(0, min(255, int(255 * alpha_f)))

        key = (self.text, self.color)
        base = self._base_cache.get(key)
        if base is None:
            base = font.render(self.text, False, self.color)
            self._base_cache[key] = base

        scale = self._scale()
        bw, bh = base.get_size()
        sw = max(1, int(round(bw * scale)))
        sh = max(1, int(round(bh * scale)))
        if (sw, sh) == (bw, bh):
            scaled = base.copy()
        else:
            scaled = pygame.transform.scale(base, (sw, sh))
        scaled.set_alpha(alpha)
        surf.blit(scaled, (int(self.x - sw / 2), int(self.y - sh / 2)))


class ImpactSpark(Particle):
    """Fireworks-style spark fanned out along the projectile's travel
    direction. Used when a friendly bullet (or missile) hits an enemy —
    sparkles spray away from the impact in the direction the shot was
    going, then arc with a touch of gravity for a brief firework trail."""
    __slots__ = ("gravity",)

    def __init__(self, x, y, color, vx_in, vy_in,
                 spread_deg=70, speed_range=(140, 380),
                 life_range=(0.30, 0.70), size=4, gravity=180.0):
        self.x = float(x)
        self.y = float(y)
        speed_in = math.hypot(vx_in, vy_in)
        if speed_in > 1.0:
            base_ang = math.atan2(vy_in, vx_in)
        else:
            base_ang = -math.pi / 2  # default upward if no hint
        spread = math.radians(spread_deg)
        ang = base_ang + random.uniform(-spread * 0.5, spread * 0.5)
        spd = random.uniform(*speed_range)
        self.vx = math.cos(ang) * spd
        self.vy = math.sin(ang) * spd
        self.life = random.uniform(*life_range)
        self.max_life = self.life
        self.color = color
        self.size = size
        self.gravity = gravity

    def update(self, dt):
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.vx *= 0.94
        self.vy *= 0.94
        self.vy += self.gravity * dt
        self.life -= dt


def _sample_sprite_colors(sprite, n=10):
    """Pick `n` random opaque colours from `sprite` so death debris can be
    tinted to match the enemy that exploded. Falls back to amber on failure
    or when the sprite is mostly transparent."""
    if sprite is None:
        return [(255, 180, 90)]
    w, h = sprite.get_size()
    colors = []
    attempts = 0
    while len(colors) < n and attempts < n * 12:
        attempts += 1
        try:
            c = sprite.get_at((random.randint(0, w - 1),
                                random.randint(0, h - 1)))
        except (IndexError, ValueError):
            continue
        if c[3] > 120 and (c[0] + c[1] + c[2]) > 60:
            colors.append((c[0], c[1], c[2]))
    return colors or [(255, 180, 90)]


class Debris:
    """Rectangular chunk of debris that flies outward from an exploded
    enemy with a gravity arc, fading to invisible over its lifetime.

    Rotation was removed: each per-frame pygame.transform.rotate was
    paired with a fresh SRCALPHA Surface alloc + fill, which together
    cost more than the visual tumbling was worth under stress. The
    chunk surface is pre-baked once in __init__; per-frame draw just
    applies a fade alpha via Surface.set_alpha — one C blit, no alloc."""
    __slots__ = ("x", "y", "vx", "vy", "color", "w", "h", "chunk",
                 "life", "max_life")

    def __init__(self, x, y, color, size, speed_range=(90, 320)):
        self.x = float(x)
        self.y = float(y)
        ang = random.uniform(0, math.tau)
        spd = random.uniform(*speed_range)
        self.vx = math.cos(ang) * spd
        self.vy = math.sin(ang) * spd - 80   # initial upward kick
        self.color = color
        self.w = int(size)
        self.h = max(1, int(size * random.uniform(0.5, 1.0)))
        chunk = pygame.Surface((self.w, self.h))
        chunk.fill(color)
        try:
            chunk = chunk.convert()
        except pygame.error:
            pass
        self.chunk = chunk
        self.life = random.uniform(0.55, 1.15)
        self.max_life = self.life

    @property
    def alive(self):
        return self.life > 0

    def update(self, dt):
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.vx *= 0.96
        self.vy *= 0.96
        self.vy += 260 * dt
        self.life -= dt

    def draw(self, surf):
        a = self.life / self.max_life
        if a <= 0:
            return
        self.chunk.set_alpha(int(255 * a))
        surf.blit(self.chunk, (int(self.x) - self.w // 2,
                               int(self.y) - self.h // 2))


class ExplosionRing:
    """Expanding ring + bright core, used on enemy/boss death."""
    __slots__ = ("x", "y", "max_r", "color", "life", "max_life", "alive")

    # AI FX sprites populated at startup. Small explosions use burst_small,
    # large ones use burst_large. Falls back to procedural ring if missing.
    _fx = {}

    @classmethod
    def set_fx(cls, fx):
        cls._fx = fx or {}

    def __init__(self, x, y, max_r=28, color=ORANGE, life=0.45):
        self.x = float(x)
        self.y = float(y)
        self.max_r = max_r
        self.color = color
        self.life = life
        self.max_life = life
        self.alive = True

    def update(self, dt):
        self.life -= dt
        if self.life <= 0:
            self.alive = False

    def draw(self, surf):
        if not self.alive:
            return
        t = 1.0 - self.life / self.max_life
        # Sprite-based explosion if available: pick burst size by max_r,
        # scale up over lifetime, fade out toward end.
        sprite = None
        if self._fx:
            sprite = (self._fx.get("burst_large") if self.max_r >= 60
                      else self._fx.get("burst_small"))
        if sprite is not None:
            size = max(8, int(self.max_r * 2 * (0.3 + 0.7 * t)))
            scaled = pygame.transform.scale(sprite, (size, size))
            alpha = int(255 * max(0.0, 1.0 - t))
            scaled.set_alpha(alpha)
            surf.blit(scaled, scaled.get_rect(center=(int(self.x), int(self.y))))
            return
        r = max(1, int(self.max_r * t))
        ring_alpha = int(220 * (1.0 - t))
        if ring_alpha > 0:
            buf = pygame.Surface((r * 2 + 6, r * 2 + 6), pygame.SRCALPHA)
            thick = max(1, int(4 * (1.0 - t)))
            pygame.draw.circle(buf, (*self.color[:3], ring_alpha), (r + 3, r + 3), r, thick)
            surf.blit(buf, (int(self.x) - r - 3, int(self.y) - r - 3))
        # core flash early in the lifecycle
        if t < 0.45:
            core_alpha = int(255 * (1 - t / 0.45))
            cr = max(2, int(self.max_r * (0.25 - t * 0.4)))
            if cr > 0:
                cbuf = pygame.Surface((cr * 2 + 2, cr * 2 + 2), pygame.SRCALPHA)
                pygame.draw.circle(cbuf, (255, 255, 255, core_alpha), (cr + 1, cr + 1), cr)
                surf.blit(cbuf, (int(self.x) - cr - 1, int(self.y) - cr - 1))


PICKUP_KINDS = ("money", "main", "side", "shield", "bomb")
PICKUP_VALUES = {"money": 50, "main": 1, "side": 1, "shield": 1, "bomb": 1}


class Pickup:
    def __init__(self, x, y, kind, asset):
        self.kind = kind
        self.image = asset
        self.rect = asset.get_rect(center=(int(x), int(y)))
        self.x = float(x)
        self.y = float(y)
        self.vy = 60
        self.t = 0
        self.alive = True

    def update(self, dt):
        self.t += dt
        self.y += self.vy * dt
        self.x += math.sin(self.t * 4) * 12 * dt
        self.rect.center = (int(self.x), int(self.y))
        if self.y > PLAY_H + 12:
            self.alive = False

    def draw(self, surf):
        # subtle bob highlight
        if int(self.t * 6) % 2 == 0:
            pygame.draw.rect(surf, WHITE, self.rect.inflate(2, 2), 1)
        surf.blit(self.image, self.rect)


# =============================================================================
# PLAYER
# =============================================================================

ENGINE_SPEEDS = {1: 200, 2: 260, 3: 320, 4: 380, 5: 440}
SHIELD_MAX = {1: 2000, 2: 3000, 3: 4000, 4: 5500, 5: 7500}
SHIELD_REGEN = {1: 150, 2: 200, 3: 250, 4: 350, 5: 500}

# Coloured enemy shields. A shield is a binary MODIFIER, not an HP pool:
# while it's up, only the matching main weapon (blue=pulse, red=spread,
# yellow=vulcan) can damage the enemy. Wrong-weapon bullets ricochet off
# the shield circle at a reflected angle and turn hostile. Correct-weapon
# bullets pass through the shield untouched and damage the enemy hitbox
# behind it.
#
# Regular enemies: shield (if rolled) is permanent — pass through with
# the right weapon or let the enemy escape.
# Boss: shield cycles on/off — S seconds shielded (random colour) then
# N seconds naked. S + N = 10. The ratio S/(S+N) ramps linearly from 0.5
# at boss 1 (5s shielded / 5s naked) to 1.0 at boss 10 (always shielded,
# colour rotates every 10s). See _boss_shield_cycle().
ENEMY_SHIELD_RATE_L1   = 0.20
ENEMY_SHIELD_RATE_L100 = 0.50
ENEMY_SHIELD_COLORS = ("blue", "red", "yellow")
SHIELD_COLOR_TO_KIND = {"blue": "pulse", "red": "spread", "yellow": "vulcan"}
SHIELD_COLOR_RGB = {
    "blue":   (90, 170, 255),
    "red":    (255, 120, 120),
    "yellow": (255, 230, 110),
}
BOSS_SHIELD_CYCLE_TOTAL = 10.0    # S + N seconds per full cycle


def _boss_shield_cycle(boss_n):
    """Return (S, N) seconds for boss n (1..10): S shielded, N naked.
    S/(S+N) interpolates 0.5 at boss 1 -> 1.0 at boss 10 (boss 10 is
    always shielded; colour rotates every S=10s)."""
    n = max(1, min(10, int(boss_n)))
    s_ratio = 0.5 + 0.5 * (n - 1) / 9.0
    S = BOSS_SHIELD_CYCLE_TOTAL * s_ratio
    N = BOSS_SHIELD_CYCLE_TOTAL - S
    return (S, N)


def _ricochet_bullet(b, enemy):
    """Reflect a player bullet off the enemy's shield surface, treating the
    shield as a sphere centred on the enemy rect. New direction is the
    incoming velocity reflected through the outward normal at the impact
    point — v' = v - 2(v·n)n. Bullet becomes hostile so the next collision
    is against the player."""
    cx, cy = enemy.rect.center
    nx = b.x - cx
    ny = b.y - cy
    nl = math.hypot(nx, ny)
    if nl < 1.0:
        # Degenerate: aim it back the way it came.
        b.vx = -b.vx
        b.vy = -b.vy
    else:
        nx /= nl
        ny /= nl
        dot = b.vx * nx + b.vy * ny
        b.vx = b.vx - 2 * dot * nx
        b.vy = b.vy - 2 * dot * ny
    # Nudge the bullet outside the shield so it doesn't immediately re-hit.
    radius = (getattr(enemy, "shield_radius", 0)
              or max(enemy.rect.width, enemy.rect.height) / 2 + 6)
    if nl >= 1.0:
        b.x = cx + nx * radius
        b.y = cy + ny * radius
        b.rect.x = int(b.x) - b.size[0] // 2
        b.rect.y = int(b.y) - b.size[1] // 2
    b.friendly = False
    b.ricocheted = True
    # Damage stays the same — a ricochet that hurts the player should
    # hurt the same as it would've hurt the enemy without a shield.


_SHIELD_HALO_CACHE = {}
SHIELD_THICKNESS = 6   # binary shield: constant thickness when up


def _make_shield_halo(radius, thickness, color):
    """Build a SRCALPHA halo: a ring of `thickness` px sitting just
    outside `radius`, drawn as concentric 1-px circles whose alpha is
    high on the outermost ring and fades toward the inside. Cached by
    (radius, thickness, color) so the same entity-size halo is reused
    across enemies and frames. RGB stays constant; per-frame shimmer is
    applied at blit time via `set_alpha`."""
    key = (radius, thickness, tuple(color))
    cached = _SHIELD_HALO_CACHE.get(key)
    if cached is not None:
        return cached
    diam = radius * 2 + thickness * 2 + 4
    surf = pygame.Surface((diam, diam), pygame.SRCALPHA)
    cx = cy = diam // 2
    PEAK_ALPHA = 230   # outermost pixel — sharp
    INNER_ALPHA = 25   # innermost pixel — faded into nothing
    for i in range(thickness):
        t = i / max(1, thickness - 1)
        alpha = int(PEAK_ALPHA + (INNER_ALPHA - PEAK_ALPHA) * t)
        r = radius + (thickness - 1 - i)
        pygame.draw.circle(surf, (color[0], color[1], color[2], alpha),
                           (cx, cy), r, 1)
    _SHIELD_HALO_CACHE[key] = surf
    return surf


def _draw_enemy_shield(surf, enemy):
    """Halo ring around a shielded enemy. Constant thickness (shield is
    a binary modifier now, not an HP pool). Slow shimmer for visual
    interest; per-enemy phase offset so a cluster doesn't pulse in
    lockstep."""
    color = enemy.shield_color
    if not color:
        return
    rgb = SHIELD_COLOR_RGB.get(color, (200, 200, 200))
    cx, cy = enemy.rect.center
    radius = (getattr(enemy, "shield_radius", 0)
              or max(enemy.rect.width, enemy.rect.height) // 2 + 2)
    halo = _make_shield_halo(radius, SHIELD_THICKNESS, rgb)
    t_ms = pygame.time.get_ticks() + (id(enemy) & 0xff)
    shimmer = 0.90 + 0.12 * math.sin(t_ms * 0.0107)
    halo.set_alpha(max(0, min(255, int(255 * shimmer))))
    surf.blit(halo, halo.get_rect(center=(cx, cy)))


def _sprite_entry(assets, sprite_name):
    """Editor-defined data for a sprite — pivot, hitbox, dummies. Returns
    an empty dict if the sprite has no entry in sprite_engine.json."""
    return assets.get("_engine_data", {}).get(sprite_name, {})


def _dummy_world_pos(rect, dummy_xy):
    """Translate a (x, y) in trimmed-PNG coords (sprite-local) into world
    coords by adding the sprite's topleft."""
    return (rect.x + int(dummy_xy[0]), rect.y + int(dummy_xy[1]))


def _entity_hit_rect(rect, hitbox):
    """Return the world-coord pygame.Rect to use for collisions, given an
    entity's sprite rect and its editor-defined hitbox. Falls back to the
    sprite rect when no hitbox is configured."""
    if not hitbox:
        return rect
    hx, hy, hw, hh = hitbox
    return pygame.Rect(rect.x + int(hx), rect.y + int(hy), int(hw), int(hh))

# Tier helpers — the 4-sub-level-per-tier system means levels 1..4
# share T1 stats (1 bullet, slow fire), 5..8 share T2, ..., 17..20 share T5.
# Damage IS the per-sub-level lever; fire rate + pattern come from tier.
def _main_tier(level):
    """Map level 1..20 (or 1..MAIN_WEAPON_MAX) to its tier 1..5."""
    return min(5, max(1, (int(level) - 1) // 4 + 1))


def _side_tier(level):
    """Side weapons have one level per tier (no sub-levels): tier == level."""
    return min(5, max(1, int(level)))


# Front-weapon fire rates (seconds between shots) keyed by type. Indexed
# by LEVEL (1..20) but all 4 sub-levels in a tier share the same rate.
_PULSE_TIER_RATES  = {1: 0.18, 2: 0.16, 3: 0.14, 4: 0.12, 5: 0.10}
_SPREAD_TIER_RATES = {1: 0.22, 2: 0.20, 3: 0.18, 4: 0.16, 5: 0.14}
_VULCAN_TIER_RATES = {1: 0.10, 2: 0.085, 3: 0.075, 4: 0.065, 5: 0.055}
MAIN_FIRE_RATE_BY_TYPE = {
    "pulse":  {lvl: _PULSE_TIER_RATES[_main_tier(lvl)]  for lvl in range(1, 21)},
    "spread": {lvl: _SPREAD_TIER_RATES[_main_tier(lvl)] for lvl in range(1, 21)},
    "vulcan": {lvl: _VULCAN_TIER_RATES[_main_tier(lvl)] for lvl in range(1, 21)},
}
# Sidekick fire rates: 5 tiers, no sub-levels (level == tier).
_MISSILE_TIER_RATES = {1: 1.6,  2: 1.3,  3: 1.0,  4: 0.85, 5: 0.70}
_DRONE_TIER_RATES   = {1: 0.45, 2: 0.36, 3: 0.28, 4: 0.22, 5: 0.17}
SIDE_FIRE_RATE_BY_TYPE = {
    "missile": dict(_MISSILE_TIER_RATES),
    "drone":   dict(_DRONE_TIER_RATES),
}

# Bullet patterns per main-weapon type, indexed by tier. The same pattern
# applies across all 4 sub-levels of the tier (damage is the differentiator).
# Sizes/colors are baked into the fire dispatcher per weapon kind.
_PULSE_TIER_PATTERNS = {
    1: [(0, 0, 0, -500)],
    2: [(-5, 0, 0, -520), (5, 0, 0, -520)],
    3: [(0, 0, 0, -540), (-6, 3, -80, -520), (6, 3, 80, -520)],
    4: [(-9, 0, 0, -560), (-3, 0, 0, -560), (3, 0, 0, -560), (9, 0, 0, -560)],
    5: [(-9, 0, 0, -580), (-3, 0, 0, -580), (3, 0, 0, -580), (9, 0, 0, -580),
        (-12, 3, -160, -500), (12, 3, 160, -500)],
}
_SPREAD_TIER_PATTERNS = {
    1: [(0, 0, 0, -480), (-4, 0, -120, -440), (4, 0, 120, -440)],
    2: [(0, 0, 0, -500), (-4, 0, -180, -440), (4, 0, 180, -440),
        (-8, 4, -260, -380), (8, 4, 260, -380)],
    3: [(0, 0, 0, -520), (-4, 0, -220, -440), (4, 0, 220, -440),
        (-8, 4, -320, -380), (8, 4, 320, -380),
        (-12, 8, -380, -300), (12, 8, 380, -300)],
    4: [(0, 0, 0, -540), (-4, 0, -260, -440), (4, 0, 260, -440),
        (-8, 4, -340, -380), (8, 4, 340, -380),
        (-12, 8, -400, -300), (12, 8, 400, -300),
        (-16, 12, -440, -200), (16, 12, 440, -200)],
    5: [(0, 0, 0, -560), (-4, 0, -280, -440), (4, 0, 280, -440),
        (-8, 4, -360, -380), (8, 4, 360, -380),
        (-12, 8, -420, -300), (12, 8, 420, -300),
        (-16, 12, -480, -200), (16, 12, 480, -200),
        (-20, 16, -520, -80), (20, 16, 520, -80)],
}
_VULCAN_TIER_PATTERNS = {
    1: [(0, 0, 0, -620)],
    2: [(-3, 0, 0, -640), (3, 0, 0, -640)],
    3: [(-4, 0, -40, -660), (0, 0, 0, -660), (4, 0, 40, -660)],
    4: [(-6, 0, -50, -680), (-2, 0, -10, -680),
        (2, 0, 10, -680), (6, 0, 50, -680)],
    5: [(-8, 0, -70, -700), (-3, 0, -20, -700),
        (0, 0, 0, -700),
        (3, 0, 20, -700), (8, 0, 70, -700)],
}
# Expand to per-level dicts so existing `MAIN_PATTERNS[mtype][lvl]` keeps
# working without callsite changes.
PULSE_PATTERNS  = {lvl: _PULSE_TIER_PATTERNS [_main_tier(lvl)] for lvl in range(1, 21)}
SPREAD_PATTERNS = {lvl: _SPREAD_TIER_PATTERNS[_main_tier(lvl)] for lvl in range(1, 21)}
VULCAN_PATTERNS = {lvl: _VULCAN_TIER_PATTERNS[_main_tier(lvl)] for lvl in range(1, 21)}
MAIN_PATTERNS = {
    "pulse":  PULSE_PATTERNS,
    "spread": SPREAD_PATTERNS,
    "vulcan": VULCAN_PATTERNS,
}
# Bullet draw style per main weapon type.
MAIN_BULLET_STYLE = {
    "pulse":  {"color": CYAN,   "size": (3, 8)},
    "spread": {"color": ORANGE, "size": (3, 7)},
    "vulcan": {"color": YELLOW, "size": (2, 5)},
}


class Player:
    def __init__(self, assets, loadout):
        self.image = assets["player"]
        self.assets = assets
        self.loadout = loadout
        self.rect = self.image.get_rect(center=(PLAY_W // 2, PLAY_H - 60))
        self.x = float(self.rect.centerx)
        self.y = float(self.rect.centery)
        self.cooldown_main = 0
        self.cooldown_side = 0
        self.shield_hp = SHIELD_MAX[loadout.shield]
        self.shield_max = SHIELD_MAX[loadout.shield]
        self.shield_recharge_delay = 0
        self.invuln = 1.0
        self.thrust = 0.0
        self.alive = True
        self.ability_cd = 0
        self.bomb_flash = 0
        self.tilt = 0.0          # smoothed -1..+1 representing bank
        self.target_tilt = 0.0
        self.cinematic = False   # set during intro/outro: blocks damage, no blink
        self.cinematic_scale = 1.0  # render multiplier during takeoff/landing

    @property
    def speed(self):
        return ENGINE_SPEEDS[self.loadout.engine]

    def update(self, dt, controls, bullets, enemies_ref, particles, sounds, lasers, on_bomb):
        # Movement
        dx = dy = 0
        if controls.left:  dx -= 1
        if controls.right: dx += 1
        if controls.up:    dy -= 1
        if controls.down:  dy += 1
        if dx and dy:
            dx *= 0.7071
            dy *= 0.7071
        self.x += dx * self.speed * dt
        self.y += dy * self.speed * dt
        self.x = clamp(self.x, self.rect.width / 2, PLAY_W - self.rect.width / 2)
        self.y = clamp(self.y, self.rect.height / 2, PLAY_H - self.rect.height / 2)
        self.rect.center = (int(self.x), int(self.y))

        # Tilt smoothing: target tilt follows the horizontal input direction.
        # Bank goes from 0 to +/-1 over ~120 ms.
        self.target_tilt = float(dx)
        diff = self.target_tilt - self.tilt
        rate = 9.0
        self.tilt = clamp(self.tilt + diff * rate * dt, -1.0, 1.0)

        # Main-weapon swap: instant, hold-based. L1/L2 = Pulse, R1/R2
        # = Spread, nothing = Vulcan. Both-side-held is treated as the
        # left side (deterministic).
        left_held = controls.l1_held or controls.l2_held
        right_held = controls.r1_held or controls.r2_held
        if left_held:
            self.loadout.main_type = "pulse"
        elif right_held:
            self.loadout.main_type = "spread"
        else:
            self.loadout.main_type = "vulcan"

        # Fire main weapon. Both pairs of shoulder buttons act as
        # fire-and-swap shortcuts: holding L1, L2, R1 or R2 fires the
        # corresponding main without needing the fire button. The cheat
        # trigger requires SELECT held too, so SELECT-less L2/R2 holds
        # are unambiguously "shoot".
        self.cooldown_main -= dt
        self.cooldown_side -= dt
        firing = controls.fire or left_held or right_held
        if firing and self.cooldown_main <= 0:
            mlvl = self.loadout.main_level()
            if mlvl > 0:
                mtype = self.loadout.main_type
                self.cooldown_main = MAIN_FIRE_RATE_BY_TYPE[mtype][mlvl]
                self._fire_main(bullets, sounds)

        # Side weapons (auto-fire)
        stype = self.loadout.side_type
        slvl = self.loadout.side_level()
        if stype != "none" and slvl > 0 and self.cooldown_side <= 0:
            self.cooldown_side = SIDE_FIRE_RATE_BY_TYPE[stype][slvl]
            self._fire_side(bullets, enemies_ref, sounds)

        # Shield regen
        self.shield_recharge_delay = max(0, self.shield_recharge_delay - dt)
        if self.shield_recharge_delay <= 0 and self.shield_hp < self.shield_max:
            self.shield_hp = min(self.shield_max, self.shield_hp + SHIELD_REGEN[self.loadout.shield] * dt)

        self.invuln = max(0, self.invuln - dt)
        self.thrust += dt * 30
        self.ability_cd = max(0, self.ability_cd - dt)
        self.bomb_flash = max(0, self.bomb_flash - dt * 2)

        # Bomb
        if controls.bomb_pressed and self.loadout.bombs > 0:
            self.loadout.bombs -= 1
            self.bomb_flash = 1.0
            on_bomb()
            sounds["bomb"].play()

        # Ability
        if controls.ability_pressed and self.ability_cd <= 0:
            self.ability_cd = 18.0
            self._use_ability(bullets, enemies_ref, particles, sounds, lasers)

    def current_sprite_name(self):
        """Mirror Player.draw's tilt-based sprite selection so fire methods
        can look up dummies that belong to the sprite actually on screen."""
        t = self.tilt
        if t < -0.85: return "player_left_2"
        if t < -0.4:  return "player_left"
        if t > 0.85:  return "player_right_2"
        if t > 0.4:   return "player_right"
        return "player"

    def _dummy_pos(self, name, default_xy):
        """Banking player variants derive their dummies from the straight
        `player` entry by image-size ratio — so the editor only owns one
        set of helpers, and the banking sprites get scaled positions on the
        fly. For the straight sprite this collapses to the original
        `topleft + dummy` math."""
        entry = _sprite_entry(self.assets, "player")
        d = entry.get("dummies", {}).get(name)
        if not d:
            return default_xy
        pimg = self.assets.get("player")
        if pimg is None:
            return default_xy
        pw, ph = pimg.get_size()
        cur_name = self.current_sprite_name()
        cur_img = self.assets.get(cur_name, pimg)
        cw, ch = cur_img.get_size()
        hx = d[0] / max(1, pw) * cw
        hy = d[1] / max(1, ph) * ch
        # The banking sprite is blit centred on rect.center regardless of
        # its own size, so anchor the helper relative to the visible
        # sprite's centre rather than self.rect.topleft.
        return (self.rect.centerx - cw / 2 + hx,
                self.rect.centery - ch / 2 + hy)

    @property
    def hit_rect(self):
        entry = _sprite_entry(self.assets, self.current_sprite_name())
        return _entity_hit_rect(self.rect, entry.get("hitbox"))

    def _fire_main(self, bullets, sounds):
        # Anchor pattern offsets on the configurable barrel_center dummy
        # (falls back to the original "top of sprite + 2 px" position).
        cx, cy = self._dummy_pos(
            "barrel_center", (self.rect.centerx, self.rect.top + 2))
        mtype = self.loadout.main_type
        lvl = self.loadout.main_level()
        style = MAIN_BULLET_STYLE[mtype]
        color = style["color"]
        size = style["size"]
        # Damage per bullet is a shared linear curve across all 3 main
        # weapons: L1=100, +10 per sub-level, L20=290. Weapons differ via
        # bullets-per-shot and fire rate, both driven by tier (every 4
        # sub-levels). All HP/damage numbers are on the x100 scale.
        dmg = 100 + 10 * (lvl - 1)
        for off_x, off_y, vx, vy in MAIN_PATTERNS[mtype][lvl]:
            bullets.append(Bullet(cx + off_x * PLAY_SCALE, cy + off_y * PLAY_SCALE,
                                  vx, vy, color, size=size, damage=dmg,
                                  weapon_kind=mtype))
        sounds["shoot"].play()

    def _fire_side(self, bullets, enemies_ref, sounds):
        stype = self.loadout.side_type
        if stype == "none":
            return
        lvl = self.loadout.side_level()
        if lvl <= 0:
            return
        # Side weapons: 5 tiers, no sub-levels (tier == level). Volley
        # count grows per tier; fire rate from the tier table.
        # Damage is flat — main weapons get per-sub-level damage scaling,
        # sides get bigger volleys + faster fire instead.
        cx_def, cy_def = self.rect.centerx, self.rect.centery
        if stype == "missile":
            targets = enemies_ref()
            if not targets:
                return
            targets = sorted(targets, key=lambda e: abs(e.rect.centerx - cx_def) + (cy_def - e.rect.centery) * 0.3)
            mleft = self._dummy_pos(
                "missile_left", (cx_def - 12 * PLAY_SCALE, cy_def))
            mright = self._dummy_pos(
                "missile_right", (cx_def + 12 * PLAY_SCALE, cy_def))
            missile_dmg = 200    # flat across all tiers
            # Reuse the right/left dummy in alternation. Beyond 2 we'd
            # benefit from a centre dummy but reusing is fine for now.
            for i in range(lvl):
                target = targets[i % len(targets)]
                ref = (lambda t: (lambda: t if t.alive else None))(target)
                tx, ty = mleft if i % 2 == 0 else mright
                bullets.append(Missile(tx, ty, ref, damage=missile_dmg,
                                       weapon_kind="missile"))
            sounds["shoot2"].play()
        elif stype == "drone":
            # Drone bullets flank the ship; tier sets the volley count.
            shots = lvl
            base_dummies = [
                ("drone_left",  (cx_def + -16 * PLAY_SCALE, cy_def + -2 * PLAY_SCALE)),
                ("drone_right", (cx_def +  16 * PLAY_SCALE, cy_def + -2 * PLAY_SCALE)),
                ("drone_top",   (cx_def,                    cy_def + -8 * PLAY_SCALE)),
                ("drone_left_2",  (cx_def + -22 * PLAY_SCALE, cy_def + 4 * PLAY_SCALE)),
                ("drone_right_2", (cx_def +  22 * PLAY_SCALE, cy_def + 4 * PLAY_SCALE)),
            ][:shots]
            drone_dmg = 100      # flat across all tiers
            for name, default in base_dummies:
                px, py = self._dummy_pos(name, default)
                bullets.append(Bullet(px, py, 0, -560,
                                      (180, 220, 255), size=(2, 6), damage=drone_dmg,
                                      weapon_kind="drone"))
            sounds["shoot2"].play()

    def _use_ability(self, bullets, enemies_ref, particles, sounds, lasers):
        if self.loadout.ability == "screen_clear":
            for e in enemies_ref():
                e.hp -= 400
            for _ in range(40):
                particles.append(Particle(self.rect.centerx, self.rect.centery, CYAN, size=4, speed_range=(80, 320)))
            sounds["bomb"].play()
        elif self.loadout.ability == "shield_burst":
            self.shield_hp = self.shield_max
            self.invuln = max(self.invuln, 2.5)
            for _ in range(30):
                particles.append(Particle(self.rect.centerx, self.rect.centery, CYAN, size=3, speed_range=(60, 200)))
            sounds["pickup"].play()
        else:  # mega_laser
            lasers.append(Laser(self))
            sounds["warn"].play()

    def take_damage(self, dmg):
        if self.cinematic or self.invuln > 0:
            return False
        self.shield_hp -= dmg
        self.shield_recharge_delay = 3.0
        self.invuln = 0.25
        if self.shield_hp <= 0:
            self.alive = False
            return True
        return False

    def collect(self, pickup, save=None):
        """Apply a pickup's effect. `save` is the SaveData (optional) used
        to enforce tier-unlock gating on weapon-upgrade pickups — a main
        or side pickup at a fully-unlocked-then-some level falls back to
        credits so collecting can't bypass boss-gated tiers."""
        k = pickup.kind
        if k == "money":
            return ("credits", 25)
        if k == "main":
            mtype = self.loadout.main_type
            lvl = self.loadout.main_level()
            if lvl >= MAIN_WEAPON_MAX:
                return ("credits", 200)
            # Tier-unlock gate: don't let pickups push past the boss-gated
            # ceiling. Convert to credits if the next level would cross it.
            if save is not None:
                next_tier = _main_tier(lvl + 1)
                if next_tier > getattr(save, f"unlocked_tier_{mtype}", 5):
                    return ("credits", 200)
            setattr(self.loadout, f"main_{mtype}", lvl + 1)
        if k == "side":
            stype = self.loadout.side_type
            if stype == "none":
                # First side pickup grants a basic missile.
                self.loadout.side_type = "missile"
                self.loadout.side_missile = max(1, self.loadout.side_missile)
            else:
                lvl = self.loadout.side_level()
                if lvl >= SIDE_WEAPON_MAX:
                    return ("credits", 200)
                # Side weapons: tier == level. Next level must be unlocked.
                if save is not None:
                    if (lvl + 1) > getattr(save, f"unlocked_tier_{stype}", 5):
                        return ("credits", 200)
                setattr(self.loadout, f"side_{stype}", lvl + 1)
        if k == "shield":
            self.shield_hp = min(self.shield_max, self.shield_hp + 1000)
        if k == "bomb":
            self.loadout.bombs = min(9, self.loadout.bombs + 1)
        return None

    def draw(self, surf):
        if not self.cinematic and self.invuln > 0 and int(self.invuln * 20) % 2 == 0:
            return
        scale = max(0.05, self.cinematic_scale)
        flicker = (int(self.thrust) % 4)

        # Pick base sprite first (so we know its scaled height for flame anchor).
        # Four bank tiers per direction: neutral / mild / deep, dispatched by
        # |tilt| magnitude so the ship rolls progressively as the input
        # commits.
        t = self.tilt
        if t < -0.85:
            img = self.assets["player_left_2"]
        elif t < -0.4:
            img = self.assets["player_left"]
        elif t > 0.85:
            img = self.assets["player_right_2"]
        elif t > 0.4:
            img = self.assets["player_right"]
        else:
            img = self.image
        if scale != 1.0:
            sw = max(2, int(img.get_width() * scale))
            sh = max(2, int(img.get_height() * scale))
            img = pygame.transform.scale(img, (sw, sh))

        cx = self.rect.centerx
        sprite_rect = img.get_rect(center=self.rect.center)
        fy = sprite_rect.bottom - 1

        # Engine flames scale with the ship so the proportion stays right.
        # During takeoff (large scale, low altitude) the cinematic also pumps
        # the thrust counter for an extra-bright flicker. PLAY_SCALE keeps
        # the flame proportional to the 1.5x-bigger play-area sprites.
        s_total = scale * PLAY_SCALE
        off_base = 8 * s_total
        for off_n, dip_side in ((-1, -1), (0, 0), (1, +1)):
            fx = int(cx + off_n * off_base)
            dipped = dip_side != 0 and self.tilt * dip_side > 0.4
            length_short = (3 + flicker // 2) * s_total
            length_long = (5 + flicker) * s_total
            length_inner = (2 + flicker // 2) * s_total
            half_w_outer = max(1, int(2 * s_total))
            half_w_inner = max(1, int(1 * s_total))
            if dipped:
                pygame.draw.polygon(surf, ORANGE, [
                    (fx - half_w_inner, fy),
                    (fx + half_w_inner, fy),
                    (fx, fy + int(length_short)),
                ])
            else:
                pygame.draw.polygon(surf, ORANGE, [
                    (fx - half_w_outer, fy),
                    (fx + half_w_outer, fy),
                    (fx, fy + int(length_long)),
                ])
                pygame.draw.polygon(surf, YELLOW, [
                    (fx - half_w_inner, fy),
                    (fx + half_w_inner, fy),
                    (fx, fy + int(length_inner)),
                ])
        surf.blit(img, sprite_rect)
        # Shield halo: visible whenever the player has shield HP. Player
        # shields still have an HP pool, so thickness scales with current
        # ratio (unlike the simplified binary enemy halo). Brightness
        # pulses gently and bumps for a frame on invuln (just-hit
        # absorption).
        if self.shield_hp > 0 and self.shield_max > 0:
            base_r = max(sprite_rect.w, sprite_rect.h) // 2 + 2
            ratio = max(0.0, min(1.0, self.shield_hp / self.shield_max))
            # Thinner-when-low, thicker-when-full: 2 px at empty edge,
            # 7 px at full.
            thickness = max(2, int(round(2 + 5 * ratio)))
            halo = _make_shield_halo(base_r, thickness, WHITE)
            t_ms = pygame.time.get_ticks() + (id(self) & 0xff)
            shimmer = 0.90 + 0.12 * math.sin(t_ms * 0.0107)
            if self.invuln > 0:
                shimmer = 1.30
            halo.set_alpha(max(0, min(255, int(255 * shimmer))))
            surf.blit(halo, halo.get_rect(center=self.rect.center))


# =============================================================================
# ENEMIES
# =============================================================================

class Enemy:
    SCORE = 10
    CREDITS = 5
    DROP_TABLE = ("money",)
    DROP_CHANCE = 0.10

    def __init__(self, x, y, asset, hp=1, flash_asset=None, sprite_name=""):
        self.image = asset
        self.flash_image = flash_asset
        self.rect = asset.get_rect(center=(int(x), int(y)))
        self.x = float(x)
        self.y = float(y)
        self.hp = hp
        self.max_hp = hp
        self.alive = True
        self.t = 0
        self.fire_cd = random.uniform(1.0, 2.5)
        self.hit_flash_t = 0.0
        self.sprite_name = sprite_name
        self._assets = None   # set by _enemy_factory / spawn helpers
        # Coloured shield: binary modifier (no HP). When shield_color is
        # set, the matching main weapon (blue=pulse, red=spread,
        # yellow=vulcan) passes through and damages the hitbox; every
        # other player projectile collides with the shield circle and
        # ricochets at a reflected angle (turns hostile).
        # shield_radius is computed once when the shield is equipped so
        # collision + draw agree on the circle size.
        self.shield_color = None
        self.shield_radius = 0

    def update(self, dt, bullets, player_ref, sounds):
        self.t += dt
        self._move(dt)
        self.rect.center = (int(self.x), int(self.y))
        if self.y > PLAY_H + 40 or self.y < -120:
            self.alive = False
            return
        self.fire_cd -= dt
        if self.fire_cd <= 0 and 0 < self.y < PLAY_H * 0.8:
            self._fire(bullets, player_ref(), sounds)
        if self.hit_flash_t > 0:
            self.hit_flash_t = max(0.0, self.hit_flash_t - dt)

    def _move(self, dt):
        self.y += 80 * dt

    def _fire(self, bullets, player, sounds):
        self.fire_cd = random.uniform(1.5, 3.0)

    def hit(self, dmg):
        self.hp -= dmg
        if self.hp <= 0:
            self.alive = False
            return True
        return False

    def draw(self, surf):
        if self.hit_flash_t > 0 and self.flash_image is not None:
            surf.blit(self.flash_image, self.rect)
        else:
            surf.blit(self.image, self.rect)
        if self.shield_color:
            _draw_enemy_shield(surf, self)
        if self.hp < self.max_hp:
            w = self.rect.width
            ratio = self.hp / self.max_hp
            pygame.draw.rect(surf, DARKER, (self.rect.x, self.rect.y - 4, w, 2))
            pygame.draw.rect(surf, GREEN, (self.rect.x, self.rect.y - 4, int(w * ratio), 2))

    @property
    def hit_rect(self):
        """Collision rect from the editor's sprite_engine.json hitbox; falls
        back to the full sprite rect if no hitbox is defined."""
        if not self._assets or not self.sprite_name:
            return self.rect
        entry = _sprite_entry(self._assets, self.sprite_name)
        return _entity_hit_rect(self.rect, entry.get("hitbox"))

    @property
    def shoot_rect(self):
        """Broad-phase rect for friendly-bullet vs enemy collision. While
        shielded, expands to the shield circle's bounding box so a graze
        on the visible halo enters the refine path (which then either
        passes through for right-weapon bullets or ricochets for wrong-
        weapon ones). Otherwise it's the regular sprite hitbox. Ram
        collisions still use hit_rect — shielded enemies don't body-block
        the player any harder."""
        if self.shield_color and self.shield_radius > 0:
            cx, cy = self.rect.center
            r = self.shield_radius + SHIELD_THICKNESS
            return pygame.Rect(cx - r, cy - r, r * 2, r * 2)
        return self.hit_rect

    def fire_pos(self, dummy_name, default_xy):
        """Return the world-coord (x, y) for this entity's named dummy. If
        the editor hasn't placed a dummy with this name, returns the
        default tuple — typically the historical hard-coded launch point."""
        if self._assets and self.sprite_name:
            entry = _sprite_entry(self._assets, self.sprite_name)
            d = entry.get("dummies", {}).get(dummy_name)
            if d:
                return _dummy_world_pos(self.rect, d)
        return default_xy


class Scout(Enemy):
    SCORE = 15
    CREDITS = 6
    DROP_CHANCE = 0.06

    def __init__(self, x, asset, flash):
        super().__init__(x, -20, asset, hp=200, flash_asset=flash)
        self.speed = random.uniform(65, 85)

    def _move(self, dt):
        self.y += self.speed * dt
        self.x += math.sin(self.t * 2 + self.x) * 30 * dt


class Gunner(Enemy):
    SCORE = 40
    CREDITS = 15
    DROP_CHANCE = 0.12
    DROP_TABLE = ("money", "money", "shield")

    def __init__(self, x, asset, flash):
        super().__init__(x, -24, asset, hp=600, flash_asset=flash)
        self.speed = 40
        self.stop_y = random.uniform(80, 200)

    def _move(self, dt):
        if self.y < self.stop_y:
            self.y += self.speed * dt
        else:
            self.x += math.sin(self.t * 1.2) * 50 * dt
            self.x = clamp(self.x, 30, PLAY_W - 30)

    def _fire(self, bullets, player, sounds):
        self.fire_cd = random.uniform(1.8, 2.8)
        if player is None:
            return
        dx = player.rect.centerx - self.x
        dy = player.rect.centery - self.y
        d = math.hypot(dx, dy) or 1
        vx = dx / d * 220
        vy = dy / d * 220
        fx, fy = self.fire_pos("barrel", (self.rect.centerx, self.rect.bottom))
        bullets.append(Bullet(fx, fy, vx, vy, RED, friendly=False, size=(4, 4)))
        sounds["hit"].play()


class Weaver(Enemy):
    SCORE = 25
    CREDITS = 10
    DROP_CHANCE = 0.18
    DROP_TABLE = ("main", "side", "money")

    def __init__(self, x, asset, flash):
        super().__init__(x, -20, asset, hp=400, flash_asset=flash)
        self.base_x = x
        self.speed = 50

    def _move(self, dt):
        self.y += self.speed * dt
        self.x = self.base_x + math.sin(self.t * 3) * 80
        self.x = clamp(self.x, 20, PLAY_W - 20)


class Bomber(Enemy):
    SCORE = 80
    CREDITS = 30
    DROP_CHANCE = 0.25
    DROP_TABLE = ("main", "side", "shield", "bomb", "money")

    def __init__(self, x, asset, flash):
        super().__init__(x, -30, asset, hp=1600, flash_asset=flash)
        self.speed = 25

    def _move(self, dt):
        self.y += self.speed * dt

    def _fire(self, bullets, player, sounds):
        self.fire_cd = random.uniform(1.5, 2.2)
        fx, fy = self.fire_pos("barrel", (self.rect.centerx, self.rect.bottom))
        for ang in (-22, -8, 8, 22):
            rad = math.radians(90 + ang)
            vx = math.cos(rad) * 200
            vy = math.sin(rad) * 200
            bullets.append(Bullet(fx, fy, vx, vy, ORANGE, friendly=False, size=(4, 6)))


class Kamikaze(Enemy):
    SCORE = 30
    CREDITS = 12
    DROP_CHANCE = 0.10

    def __init__(self, x, asset, flash):
        super().__init__(x, -20, asset, hp=400, flash_asset=flash)
        self.acquired = False
        self.vx = 0
        # Half-speed drift before lock-on so the bot/player has more
        # reaction time before the dive triggers (y > 40).
        self.vy = 40

    def update(self, dt, bullets, player_ref, sounds):
        self.t += dt
        player = player_ref()
        if player and not self.acquired and self.y > 40:
            dx = player.rect.centerx - self.x
            dy = player.rect.centery - self.y
            d = math.hypot(dx, dy) or 1
            self.vx = dx / d * 260
            self.vy = dy / d * 260
            self.acquired = True
        self.x += self.vx * dt
        self.y += self.vy * dt if self.acquired else self.vy * dt
        self.rect.center = (int(self.x), int(self.y))
        if self.y > PLAY_H + 40 or self.x < -40 or self.x > PLAY_W + 40:
            self.alive = False
        if self.hit_flash_t > 0:
            self.hit_flash_t = max(0.0, self.hit_flash_t - dt)


class Turret(Enemy):
    SCORE = 60
    CREDITS = 20
    DROP_CHANCE = 0.20
    DROP_TABLE = ("shield", "main", "bomb")

    def __init__(self, x, asset, flash):
        super().__init__(x, -24, asset, hp=1000, flash_asset=flash)
        self.stop_y = random.uniform(40, 100)
        self.speed = 30

    def _move(self, dt):
        if self.y < self.stop_y:
            self.y += self.speed * dt

    def _fire(self, bullets, player, sounds):
        self.fire_cd = 1.2
        if player is None:
            return
        fx, fy = self.fire_pos("barrel", (self.rect.centerx, self.rect.bottom))
        for ang in (-15, 0, 15):
            dx = player.rect.centerx - self.x
            dy = player.rect.centery - self.y
            base = math.atan2(dy, dx) + math.radians(ang)
            vx = math.cos(base) * 230
            vy = math.sin(base) * 230
            bullets.append(Bullet(fx, fy, vx, vy, PURPLE, friendly=False, size=(4, 4)))


# =============================================================================
# OBSTACLES (passive enemies: drift down, don't shoot, hurt player on contact)
# =============================================================================

class Asteroid(Enemy):
    """Small rock that drifts down with some horizontal sway."""
    SCORE = 5
    CREDITS = 2
    DROP_TABLE = ("money",)
    DROP_CHANCE = 0.05

    def __init__(self, x, asset, flash):
        super().__init__(x, -20, asset, hp=200, flash_asset=flash)
        self.speed = random.uniform(30, 55)
        self.drift = random.uniform(-25, 25)

    def _move(self, dt):
        self.y += self.speed * dt
        self.x += self.drift * dt


class BigAsteroid(Enemy):
    """Bigger rock - takes more hits, drops something useful."""
    SCORE = 25
    CREDITS = 9
    DROP_TABLE = ("money", "shield", "bomb")
    DROP_CHANCE = 0.20

    def __init__(self, x, asset, flash):
        super().__init__(x, -30, asset, hp=800, flash_asset=flash)
        self.speed = random.uniform(20, 35)
        self.drift = random.uniform(-18, 18)

    def _move(self, dt):
        self.y += self.speed * dt
        self.x += self.drift * dt


class Mine(Enemy):
    """Floating mine - wobbles, doesn't shoot, explodes on death damaging nearby player."""
    SCORE = 20
    CREDITS = 6
    DROP_TABLE = ()
    DROP_CHANCE = 0.0
    EXPLOSION_RADIUS = 60
    EXPLOSION_DAMAGE = 1200

    def __init__(self, x, asset, flash):
        super().__init__(x, -20, asset, hp=400, flash_asset=flash)
        self.speed = random.uniform(18, 28)

    def _move(self, dt):
        self.y += self.speed * dt
        self.x += math.sin(self.t * 3) * 12 * dt

    def draw(self, surf):
        # Blinking warning light overlay
        if int(self.t * 6) % 2 == 0:
            pygame.draw.circle(surf, (255, 200, 200),
                               self.rect.center, 2)
        super().draw(surf)


class Pylon(Enemy):
    """Edge-mounted defensive pylon. Slow, high HP, drops good loot. Doesn't fire."""
    SCORE = 70
    CREDITS = 22
    DROP_TABLE = ("shield", "main", "money", "bomb")
    DROP_CHANCE = 0.25

    def __init__(self, x, asset, flash):
        super().__init__(x, -50, asset, hp=2000, flash_asset=flash)
        self.speed = 28


class Crystal(Enemy):
    """Rare cargo crystal. Modest HP, drops a powerup with high probability."""
    SCORE = 60
    CREDITS = 18
    DROP_TABLE = ("main", "side", "shield", "bomb")
    DROP_CHANCE = 0.70

    def __init__(self, x, asset, flash):
        super().__init__(x, -25, asset, hp=400, flash_asset=flash)
        self.speed = random.uniform(25, 40)


class Wall(Enemy):
    """Edge-mounted hull plating. Indestructible; blocks the player's movement
    and absorbs/blocks bullets. Scrolls down with the world."""
    SCORE = 0
    CREDITS = 0
    DROP_TABLE = ()
    DROP_CHANCE = 0.0
    SOLID = True   # marker for the collision branch in PlayState

    def __init__(self, x, asset, flash):
        # Spawn fully above the screen so it slides in without popping
        super().__init__(x, -asset.get_height() // 2, asset, hp=99999, flash_asset=flash)
        self.speed = 30

    def _move(self, dt):
        self.y += self.speed * dt

    def hit(self, dmg):
        # Walls can't be killed; they just spark.
        self.hit_flash_t = 0.06
        return False


class Boss(Enemy):
    SCORE = 2000
    CREDITS = 400
    DROP_CHANCE = 1.0
    DROP_TABLE = ("main", "side", "shield", "bomb")

    def __init__(self, asset, flash=None, hp_mul=1.0, boss_n=1):
        x = PLAY_W // 2
        super().__init__(x, -120, asset, hp=int(48000 * hp_mul), flash_asset=flash)
        self.speed = 30
        self.hp_mul = hp_mul
        self.phase = 0
        self.dwell = 0
        self.pattern_cd = 1.0
        self.sweep_dir = 1
        self.boss_n = int(boss_n)
        # Shield cycle: S seconds shielded (random colour) then N seconds
        # naked, repeating. S/(S+N) = 0.5 at boss 1, ramps to 1.0 at boss
        # 10 (always shielded, colour rotates every S=10s).
        self._shield_S, self._shield_N = _boss_shield_cycle(self.boss_n)
        # Start the fight shielded — the player sees the colour as the boss
        # descends and can pre-hold the right shoulder.
        _apply_enemy_shield(self)
        self._shield_phase_timer = self._shield_S

    def update(self, dt, bullets, player_ref, sounds):
        self.t += dt
        if self.y < 90:
            self.y += self.speed * dt
        else:
            self.x += self.sweep_dir * 50 * dt
            if self.x < 80 or self.x > PLAY_W - 80:
                self.sweep_dir *= -1
                self.x = clamp(self.x, 80, PLAY_W - 80)
        self.rect.center = (int(self.x), int(self.y))

        # Phase escalation based on HP
        phase = 0
        if self.hp < self.max_hp * 0.66: phase = 1
        if self.hp < self.max_hp * 0.33: phase = 2
        self.phase = phase

        self.pattern_cd -= dt
        if self.pattern_cd <= 0:
            # Halved fire-rate: was [1.2, 0.9, 0.6] per phase; bosses now
            # fire every [2.4, 1.8, 1.2] seconds so the cadence matches the
            # shield-cycle rhythm and gives the player time to swap.
            self.pattern_cd = [2.4, 1.8, 1.2][self.phase]
            self._fire_pattern(bullets, player_ref())

        # Shield phase cycle. While shielded, count down to naked. While
        # naked, count down to a fresh random-colour shield. With N=0 (boss
        # 10) the naked phase is instantaneous — the shield just rotates
        # colour every S seconds.
        self._shield_phase_timer -= dt
        while self._shield_phase_timer <= 0:
            if self.shield_color:
                self.shield_color = None
                self.shield_radius = 0
                self._shield_phase_timer += self._shield_N
                if self._shield_N <= 0:
                    # Skip the zero-length naked phase and re-shield now.
                    continue
            else:
                _apply_enemy_shield(self)
                self._shield_phase_timer += self._shield_S

        # Tick down the hit-flash timer like Enemy.update would. Without this
        # the boss got stuck rendering as a full white silhouette as soon as
        # it took its first hit.
        if self.hit_flash_t > 0:
            self.hit_flash_t = max(0.0, self.hit_flash_t - dt)

    def _fire_pattern(self, bullets, player):
        cx, cy = self.fire_pos(
            "barrel_center", (self.rect.centerx, self.rect.bottom))
        pick = random.choice(["fan", "ring", "aimed"])
        if pick == "fan":
            # +25% angular spacing between projectiles so the player has
            # clear gaps to dodge through. Was 12° step (11 bullets in a
            # ±60° arc); now 15° step (9 bullets in the same arc, still
            # symmetric around 0).
            for ang in range(-60, 61, 15):
                rad = math.radians(90 + ang)
                vx = math.cos(rad) * 200
                vy = math.sin(rad) * 200
                bullets.append(Bullet(cx, cy, vx, vy, RED, friendly=False, size=(5, 5)))
        elif pick == "ring":
            count = 14 + self.phase * 4
            offset = self.t * 60
            for i in range(count):
                ang = 360 * i / count + offset
                rad = math.radians(ang)
                vx = math.cos(rad) * 160
                vy = math.sin(rad) * 160
                bullets.append(Bullet(cx, cy, vx, vy, PURPLE, friendly=False, size=(5, 5)))
        elif pick == "aimed" and player is not None:
            for off in (-1, 0, 1):
                dx = player.rect.centerx - cx
                dy = player.rect.centery - cy
                base = math.atan2(dy, dx) + math.radians(off * 8)
                vx = math.cos(base) * 280
                vy = math.sin(base) * 280
                bullets.append(Bullet(cx, cy, vx, vy, ORANGE, friendly=False, size=(5, 7)))

    def draw(self, surf):
        # skip the small HP bar Enemy.draw paints over the sprite; use only the big top bar
        if self.hit_flash_t > 0 and self.flash_image is not None:
            surf.blit(self.flash_image, self.rect)
        else:
            surf.blit(self.image, self.rect)
        if self.shield_color:
            _draw_enemy_shield(surf, self)
        bar_w = PLAY_W - 40
        ratio = max(0.0, self.hp / self.max_hp)
        pygame.draw.rect(surf, DARKER, (20, 8, bar_w, 6))
        pygame.draw.rect(surf, RED, (20, 8, int(bar_w * ratio), 6))
        pygame.draw.rect(surf, WHITE, (20, 8, bar_w, 6), 1)
        # Shield-cycle phase pip: a solid coloured strip while shielded
        # (showing which weapon to hold), empty while naked (kill window).
        # The pip width = remaining time in the current phase / total phase
        # length, so it visually drains as the phase progresses.
        full_phase = (self._shield_S if self.shield_color else self._shield_N) or 1.0
        phase_t = max(0.0, self._shield_phase_timer / full_phase)
        sw = int(bar_w * phase_t)
        if self.shield_color:
            col = SHIELD_COLOR_RGB[self.shield_color]
        else:
            col = (90, 90, 110)
        pygame.draw.rect(surf, DARKER, (20, 16, bar_w, 3))
        pygame.draw.rect(surf, col, (20, 16, sw, 3))


# =============================================================================
# LEVEL DEFINITIONS
# =============================================================================

def _enemy_factory(kind, x, assets):
    flash = assets.get(kind + "_flash")
    if kind == "scout":     e = Scout(x, assets["scout"], flash)
    elif kind == "gunner":  e = Gunner(x, assets["gunner"], flash)
    elif kind == "weaver":  e = Weaver(x, assets["weaver"], flash)
    elif kind == "bomber":  e = Bomber(x, assets["bomber"], flash)
    elif kind == "kamikaze":e = Kamikaze(x, assets["kamikaze"], flash)
    elif kind == "turret":  e = Turret(x, assets["turret"], flash)
    elif kind == "boss":    e = Boss(assets["boss"], flash)
    elif kind == "asteroid":
        var = random.choice([9, 11])
        pal = random.randint(0, 3)
        key = f"rock_{var}_{pal}"
        e = Asteroid(x, assets[key], assets[key + "_flash"])
        kind = key
    elif kind == "big_asteroid":
        pal = random.randint(0, 3)
        key = f"rock_14_{pal}"
        e = BigAsteroid(x, assets[key], assets[key + "_flash"])
        kind = key
    elif kind == "mine":    e = Mine(x, assets["mine"], assets["mine_flash"])
    elif kind == "pylon":   e = Pylon(x, assets["pylon"], assets["pylon_flash"])
    elif kind == "crystal": e = Crystal(x, assets["crystal"], assets["crystal_flash"])
    else:
        raise ValueError(kind)
    e.sprite_name = kind
    e._assets = assets
    return e


def _wall_factory(x, assets, sector_idx):
    key = f"wall_{sector_idx % 10}"
    w = Wall(x, assets[key], assets[key + "_flash"])
    w.sprite_name = key
    w._assets = assets
    return w


def _level_number(state):
    """Best-effort 1..100 read of the current level number from state."""
    try:
        key = state.level.key
        if isinstance(key, str) and key.startswith("L") and key[1:].isdigit():
            return max(1, min(100, int(key[1:])))
    except Exception:
        pass
    return 1


def _enemy_shield_chance(level_num):
    """Linear ramp 20%@L1 -> 50%@L100."""
    t = (max(1, min(100, int(level_num))) - 1) / 99.0
    return ENEMY_SHIELD_RATE_L1 + (ENEMY_SHIELD_RATE_L100 - ENEMY_SHIELD_RATE_L1) * t


# ────────────────────────────────────────────────────────────────────────
# Adaptive per-level difficulty knob
# ────────────────────────────────────────────────────────────────────────
# Each level carries an integer "difficulty_adjust" in SaveData. Starts at
# 0 (= baseline). Decrement by 1 on each death; increment by 5 on each
# finish, capped at 0. Negative values bias the level easier. Never goes
# positive.
#
# Per -1 (additive each unit):
#   * -1 enemy from each spawned wave (min 1)
#   * -5 percentage points off the shield-spawn chance (min 0)
#
# Per -5 (one tier of "real trouble"):
#   * downgrade the spawned wave's enemy type one step toward scout
#   * +10% absolute chance the drop becomes a bomb
#   * +25% absolute chance the drop becomes a shield
#     (rolled before the regular drop-table pick, so they replace it)
#
# Bosses are not downgraded — they're scripted single-spawn events.
ENEMY_DOWNGRADE_CHAIN = {
    "bomber":   "turret",
    "turret":   "gunner",
    "gunner":   "kamikaze",
    "kamikaze": "weaver",
    "weaver":   "scout",
    "scout":    "scout",   # bottom of the chain
}

DIFFICULTY_PER_UNIT_ENEMIES = 1
DIFFICULTY_PER_UNIT_SHIELD_PCT = 0.05
DIFFICULTY_PER_UNIT_HP_PCT = 0.02     # each negative unit shaves 2% off enemy max HP
DIFFICULTY_PER_5_BOMB_BONUS = 0.10
DIFFICULTY_PER_5_SHIELD_DROP_BONUS = 0.25

# Base HP per enemy / obstacle kind. Used to weight waves when deciding
# which one is "worst" for the per-level difficulty adjustment (the unit
# that loses one enemy is the highest-HP wave currently in the level).
# Boss / wall are excluded — they aren't downgradable waves.
ENEMY_BASE_HP = {
    "scout":        200,
    "gunner":       600,
    "weaver":       400,
    "kamikaze":     400,
    "turret":      1000,
    "bomber":      1600,
    "asteroid":     200,
    "big_asteroid": 800,
    "mine":         400,
    "pylon":       2000,
    "crystal":      400,
}


def _downgrade_enemy_kind(kind, steps):
    """Walk `steps` down ENEMY_DOWNGRADE_CHAIN. Returns the input kind
    untouched if it isn't a downgradable type (e.g. asteroid, pylon)."""
    if kind not in ENEMY_DOWNGRADE_CHAIN or steps <= 0:
        return kind
    for _ in range(steps):
        nxt = ENEMY_DOWNGRADE_CHAIN.get(kind)
        if nxt is None or nxt == kind:
            break
        kind = nxt
    return kind


def _adjusted_spawn(state, kind, count):
    """Apply this wave's pre-computed difficulty modifier to (kind, count).
    Each wave's (downgrade_steps, count_reduction) was decided once at
    PlayState construction by _compute_wave_modifiers — the worst waves
    in the level were picked greedily so a -1 adj hits the single
    highest-HP wave, -2 hits the two worst, etc. -5 also injects one
    type downgrade onto the wave that's worst after the count cuts."""
    modifiers = getattr(state, "wave_modifiers", None)
    if not modifiers:
        return kind, count
    wave_idx = getattr(state, "timeline_idx", -1)
    mod = modifiers.get(wave_idx)
    if mod is None:
        return kind, count
    downgrade_steps, reduction = mod
    eff_kind = _downgrade_enemy_kind(kind, downgrade_steps)
    # Half-wave floor (rounded up): a 6-enemy wave can shrink to 3, a
    # 5-enemy wave to 3, a 4-enemy wave to 2, etc. Single-enemy waves
    # are immune. Mirrors the cap in _compute_wave_modifiers.
    floor = (count + 1) // 2
    eff_count = max(floor, count - reduction)
    return eff_kind, eff_count


def _compute_wave_modifiers(state):
    """Decide per-wave (downgrade_steps, count_reduction) for the level
    given state.difficulty_adjust.

    Distribution rule:
      * count reductions: -1 per unit of adjustment, applied to whichever
        wave currently has the highest HP-weight. So adj=-1 hits one
        wave; adj=-2 hits two; adj=-5 hits five (and so on). A wave is
        skipped once its remaining count would drop below 1.
      * type downgrades: 1 per -5 units, applied to whichever wave is
        worst AFTER the count reductions are settled. So adj=-5 also
        downgrades one wave (the worst of the surviving five); adj=-10
        downgrades two; etc. Scout-only waves (or any unrecognised kind)
        are skipped — there's nowhere left to downgrade.

    Returns a {timeline_idx: (downgrade_steps, reduction)} map. Waves
    not in the map have no modifier."""
    adj = int(getattr(state, "difficulty_adjust", 0))
    if adj >= 0:
        return {}
    timeline = getattr(state.level, "timeline", None) or []
    waves = []
    for idx, entry in enumerate(timeline):
        try:
            _, fn = entry
        except Exception:
            continue
        kind = getattr(fn, "wave_kind", None)
        count = int(getattr(fn, "wave_count", 0) or 0)
        if kind in ENEMY_BASE_HP and count > 0:
            waves.append({"idx": idx, "kind": kind, "count": count,
                          "reduction": 0, "downgrade": 0})
    if not waves:
        return {}

    def weight(w):
        eff_kind = _downgrade_enemy_kind(w["kind"], w["downgrade"])
        eff_count = max(1, w["count"] - w["reduction"])
        return eff_count * ENEMY_BASE_HP.get(eff_kind, 0)

    # Phase 1: count reductions, one per -1 unit of adjustment.
    # A wave can shrink down to ceil(count/2) — half (rounded up). The
    # ceil keeps single-enemy waves intact and gives a 6-enemy wave a
    # floor of 3 (-3 max), a 5-enemy wave a floor of 3 (-2 max), a 2-
    # enemy combo a floor of 1 (-1 max).
    def floor_for(w):
        return (w["count"] + 1) // 2
    remaining = -adj * DIFFICULTY_PER_UNIT_ENEMIES
    while remaining > 0:
        candidates = [w for w in waves
                      if (w["count"] - w["reduction"]) > floor_for(w)]
        if not candidates:
            break
        candidates.sort(key=lambda w: (-weight(w), w["idx"]))
        candidates[0]["reduction"] += 1
        remaining -= 1

    # Phase 2: type downgrades, one per -5 unit of adjustment.
    downgrades_left = (-adj) // 5
    while downgrades_left > 0:
        candidates = [
            w for w in waves
            if _downgrade_enemy_kind(w["kind"], w["downgrade"] + 1)
               != _downgrade_enemy_kind(w["kind"], w["downgrade"])
        ]
        if not candidates:
            break
        candidates.sort(key=lambda w: (-weight(w), w["idx"]))
        candidates[0]["downgrade"] += 1
        downgrades_left -= 1

    return {w["idx"]: (w["downgrade"], w["reduction"])
            for w in waves
            if w["downgrade"] > 0 or w["reduction"] > 0}


def _effective_shield_chance(state):
    """Base shield chance for this level, with difficulty_adjust applied.
    Clamped to [0, 1]."""
    base = _enemy_shield_chance(_level_number(state))
    adj = getattr(state, "difficulty_adjust", 0)
    if adj < 0:
        base += adj * DIFFICULTY_PER_UNIT_SHIELD_PCT
    return max(0.0, min(1.0, base))


def _biased_drop_kind(state, drop_table):
    """Roll a drop kind, biased toward bomb / shield when the level's
    difficulty_adjust is at or below -5. Each -5 tier adds an absolute
    +10% chance of bomb and +25% of shield, rolled BEFORE the regular
    drop-table pick — they replace it on a hit. Falls through to
    random.choice(drop_table) otherwise."""
    adj = getattr(state, "difficulty_adjust", 0)
    if adj <= -5:
        tiers = (-adj) // 5
        bomb_p = DIFFICULTY_PER_5_BOMB_BONUS * tiers
        shield_p = DIFFICULTY_PER_5_SHIELD_DROP_BONUS * tiers
        r = random.random()
        if r < bomb_p:
            return "bomb"
        if r < bomb_p + shield_p:
            return "shield"
    return random.choice(drop_table)


def _apply_enemy_shield(e, color=None):
    """Equip an enemy with a coloured shield (binary modifier, no HP).
    Random colour if not given. shield_radius is snapped from the sprite
    rect so collision + draw agree."""
    if color is None:
        color = random.choice(ENEMY_SHIELD_COLORS)
    e.shield_color = color
    e.shield_radius = max(e.rect.width, e.rect.height) // 2 + 2


def _scale_enemy(e, state):
    """Apply level difficulty to a freshly-spawned enemy's HP, then roll
    for a coloured shield (skipped on Walls, which are indestructible).

    HP scaling has two layers:
      * the level's static difficulty_mul (non-boss only — bosses have
        their own hp_mul curve set in spawn_boss / Boss.__init__),
      * the level's adaptive difficulty_adjust knob: each -1 unit cuts
        every NON-BOSS enemy's max HP by 2%, floored at 10% so something
        always remains to kill. Bosses are skipped — their fight is the
        whole point of the boss level, the adjust still helps via the
        boss's shield cycle being shorter & via wave deletions earlier.
    """
    mul = getattr(state, "difficulty", 1.0)
    if mul != 1.0 and not isinstance(e, Boss):
        e.hp = max(1, int(e.hp * mul))
        e.max_hp = e.hp
    adj = int(getattr(state, "difficulty_adjust", 0))
    if adj < 0 and not isinstance(e, Wall) and not isinstance(e, Boss):
        adj_mul = max(0.1, 1.0 + adj * DIFFICULTY_PER_UNIT_HP_PCT)
        e.hp = max(1, int(e.hp * adj_mul))
        e.max_hp = e.hp
    if isinstance(e, Boss) or isinstance(e, Wall):
        return
    if random.random() < _effective_shield_chance(state):
        color = _pick_compatible_shield_color(e, state)
        if color is not None:
            _apply_enemy_shield(e, color=color)


def _shield_radius_for(e):
    """The shield_radius an enemy would get if equipped now. Mirrors the
    formula in _apply_enemy_shield so spawn-time geometry checks agree
    with what the equipped shield will actually be."""
    return max(e.rect.width, e.rect.height) // 2 + 2


def _pick_compatible_shield_color(e, state):
    """Pick a shield colour that won't make this enemy's shield circle
    overlap a DIFFERENT-colour shield already on screen. Returns None
    when two or more distinct colours are already touching the spawn
    area — spawning shielded would create a forced multi-swap stack, so
    we leave this one naked instead. Bosses are skipped (their shields
    cycle on their own timer and their sprites are huge — collapsing
    everything into the boss's color would be wrong).

    Matches same-color stacks happily: if one blue shield is nearby and
    no others, this one becomes blue too. Same-color clusters of regular
    enemies are intended."""
    ex, ey = e.rect.center
    my_r = _shield_radius_for(e)
    nearby = set()
    for other in state.enemies:
        if (not getattr(other, "alive", False)
                or not getattr(other, "shield_color", None)
                or isinstance(other, Boss)):
            continue
        ox, oy = other.rect.center
        d_sq = (ox - ex) ** 2 + (oy - ey) ** 2
        min_d = my_r + other.shield_radius
        if d_sq < min_d * min_d:
            nearby.add(other.shield_color)
    if len(nearby) >= 2:
        return None
    if nearby:
        return next(iter(nearby))
    return random.choice(ENEMY_SHIELD_COLORS)


def spawn_line(kind, count, gap=50, y_off=0):
    def fn(state):
        eff_kind, eff_count = _adjusted_spawn(state, kind, count)
        total = (eff_count - 1) * gap
        start_x = (PLAY_W - total) / 2
        for i in range(eff_count):
            e = _enemy_factory(eff_kind, start_x + i * gap, state.assets)
            e.y += y_off
            _scale_enemy(e, state)
            state.enemies.append(e)
    fn.wave_kind = kind
    fn.wave_count = count
    return fn


def spawn_v(kind, count):
    def fn(state):
        eff_kind, eff_count = _adjusted_spawn(state, kind, count)
        for i in range(eff_count):
            x = PLAY_W // 2 + (i - eff_count // 2) * 40
            e = _enemy_factory(eff_kind, x, state.assets)
            e.y = -30 - abs(i - eff_count // 2) * 30
            _scale_enemy(e, state)
            state.enemies.append(e)
    fn.wave_kind = kind
    fn.wave_count = count
    return fn


def spawn_random(kind, count, x_range=(40, PLAY_W - 40)):
    def fn(state):
        eff_kind, eff_count = _adjusted_spawn(state, kind, count)
        for _ in range(eff_count):
            x = random.uniform(*x_range)
            e = _enemy_factory(eff_kind, x, state.assets)
            _scale_enemy(e, state)
            state.enemies.append(e)
    fn.wave_kind = kind
    fn.wave_count = count
    return fn


def spawn_at(kind, x):
    def fn(state):
        # Single-spawn — count is 1, so the count modifier never kicks in.
        # Still apply the kind downgrade so per-5 tiers nudge this enemy.
        eff_kind, _ = _adjusted_spawn(state, kind, 1)
        e = _enemy_factory(eff_kind, x, state.assets)
        _scale_enemy(e, state)
        state.enemies.append(e)
    fn.wave_kind = kind
    fn.wave_count = 1
    return fn


def spawn_boss(hp_mul=1.0):
    def fn(state):
        sec = getattr(state.level, "sector_idx", 0)
        key = f"boss_{sec}"
        if key not in state.assets:
            key = "boss"
        flash = state.assets.get(f"{key}_flash") or state.assets.get("boss_flash")
        # Boss number 1..10 drives the shield-cycle timing (S/N seconds).
        boss_n = sec + 1
        b = Boss(state.assets[key], flash, hp_mul=hp_mul, boss_n=boss_n)
        b.sprite_name = key
        b._assets = state.assets
        state.enemies.append(b)
        state.is_boss_fight = True
        state.boss_intro_t = 2.6
        state.app.sounds["warn"].play()
    return fn


def spawn_sides(kind, count, side="both", margin=70):
    """Drop `count` obstacles down the left/right edges of the playfield.
    Stagger them vertically so they don't all stack on top of each other."""
    def fn(state):
        eff_kind, eff_count = _adjusted_spawn(state, kind, count)
        for i in range(eff_count):
            if side == "left":
                x = random.uniform(20, margin)
            elif side == "right":
                x = random.uniform(PLAY_W - margin, PLAY_W - 20)
            else:
                pick = random.choice(("left", "right"))
                if pick == "left":
                    x = random.uniform(20, margin)
                else:
                    x = random.uniform(PLAY_W - margin, PLAY_W - 20)
            e = _enemy_factory(eff_kind, x, state.assets)
            e.y = -30 - i * 50
            _scale_enemy(e, state)
            state.enemies.append(e)
    fn.wave_kind = kind
    fn.wave_count = count
    return fn


def spawn_wall_pair(sector_idx, side="both"):
    """Spawn a wall segment on the left, right, or both edges."""
    def fn(state):
        if side in ("left", "both"):
            w = _wall_factory(24, state.assets, sector_idx)
            state.enemies.append(w)
        if side in ("right", "both"):
            w = _wall_factory(PLAY_W - 24, state.assets, sector_idx)
            state.enemies.append(w)
    return fn


@dataclass
class Level:
    key: str
    name: str
    nebula: tuple
    timeline: list
    duration: float
    has_boss: bool = False
    theme: str = "start"
    difficulty: float = 1.0
    sector_idx: int = 0
    is_test: bool = False   # hidden visual-checkup mode (SELECT+Y on title)


# Sector themes cycle: 10 sectors, each pulling from the 5 ribbon themes plus
# its own nebula tint. Sector index is (level_n - 1) // 10.
SECTOR_NAMES = [
    "Launch Bay",      # 1   L001-L010
    "Asteroid Belt",   # 2
    "Outpost Run",     # 3
    "Comet Wash",      # 4
    "Void Ring",       # 5
    "Crimson Shoals",  # 6
    "Pulsar Belt",     # 7
    "Iron Tide",       # 8
    "Ember Field",     # 9
    "Final Approach",  # 10  L091-L100
]

SECTOR_RIBBONS = [
    "start", "asteroid", "outpost", "asteroid", "converge",
    "boss",  "converge", "outpost", "asteroid", "boss",
]

SECTOR_NEBULAS = [
    (40, 80, 160),   (120, 80, 60),    (80, 40, 130),
    (120, 100, 50),  (50, 110, 90),    (140, 40, 80),
    (100, 60, 160),  (60, 90, 110),    (170, 90, 30),
    (180, 30, 50),
]


# Each sector picks which side obstacles (and whether walls) show up. Walls
# only appear on the structural sectors; asteroid/mine sectors lean on
# drifting hazards instead.
SECTOR_OBSTACLES = {
    0: (("asteroid",),                    False),  # Launch Bay     - light
    1: (("asteroid", "big_asteroid"),     False),  # Asteroid Belt
    2: (("pylon",),                       True),   # Outpost Run    - walls
    3: (("asteroid", "asteroid", "mine"), False),  # Comet Wash
    4: (("mine", "crystal"),              False),  # Void Ring
    5: (("mine", "asteroid"),             True),   # Crimson Shoals - walls
    6: (("crystal", "mine"),              False),  # Pulsar Belt
    7: (("pylon", "mine"),                True),   # Iron Tide      - walls
    8: (("asteroid", "big_asteroid"),     False),  # Ember Field
    9: (("pylon", "mine", "asteroid"),    True),   # Final Approach - walls
}


def _gen_timeline(n, is_boss):
    """Procedural enemy timeline for level n (1..100)."""
    pool = ["scout"]
    if n >= 3:  pool.append("gunner")
    if n >= 7:  pool.append("weaver")
    if n >= 12: pool.append("kamikaze")
    if n >= 18: pool.append("turret")
    if n >= 25: pool.append("bomber")
    weighted = list(pool)
    if n >= 30: weighted += ["gunner", "weaver"]
    if n >= 50: weighted += ["kamikaze", "bomber"]
    if n >= 70: weighted += ["turret", "bomber"]

    sector_idx = (n - 1) // 10
    obstacle_pool, has_walls = SECTOR_OBSTACLES[sector_idx]

    rng = random.Random(0xC0FFEE ^ (n * 2654435761))

    timeline = []
    if is_boss:
        for i in range(4):
            t = 1.5 + i * 4.0
            kind = rng.choice(weighted)
            count = 3 + n // 14
            choice = rng.randint(0, 2)
            spawner = (spawn_line(kind, count, gap=60) if choice == 0
                       else spawn_v(kind, count) if choice == 1
                       else spawn_random(kind, count))
            timeline.append((t, spawner))
        # A scattering of obstacles before the boss for atmosphere.
        if obstacle_pool:
            timeline.append((6.0, spawn_sides(rng.choice(obstacle_pool), 2)))
            timeline.append((12.0, spawn_sides(rng.choice(obstacle_pool), 3)))
        hp_mul = 1.0 + ((n - 10) // 10) * 0.35
        timeline.append((20.0, spawn_boss(hp_mul=max(1.0, hp_mul))))
    else:
        duration = min(45 + n // 2, 90)
        wave_count = 5 + n // 8
        for i in range(wave_count):
            t = 2.0 + i * (duration - 6) / max(1, wave_count - 1)
            kind = rng.choice(weighted)
            count = 3 + n // 10
            choice = rng.randint(0, 3)
            if choice == 0:
                spawner = spawn_line(kind, count, gap=60)
            elif choice == 1:
                spawner = spawn_v(kind, count)
            elif choice == 2:
                spawner = spawn_random(kind, count)
            else:
                spawner_a = spawn_at(kind, PLAY_W * 0.25)
                spawner_b = spawn_at(kind, PLAY_W * 0.75)
                def combo(state, sa=spawner_a, sb=spawner_b):
                    # A combo is one timeline entry that spawns two
                    # enemies. The wave's reduction modifier (capped at
                    # count-1 = 1 in _compute_wave_modifiers) decides how
                    # many of the two halves actually fire. Each inner
                    # spawn_at picks up the SAME modifier and applies the
                    # kind downgrade to its own single enemy.
                    modifiers = getattr(state, "wave_modifiers", None)
                    reduction = 0
                    if modifiers:
                        mod = modifiers.get(state.timeline_idx)
                        if mod is not None:
                            reduction = mod[1]
                    if reduction <= 0:
                        sa(state); sb(state)
                    elif reduction == 1:
                        sa(state)
                    # reduction >= 2 means drop both — won't happen given
                    # the count-1 cap, but defensive.
                combo.wave_kind = kind
                combo.wave_count = 2
                spawner = combo
            timeline.append((t, spawner))

        # Obstacle bursts interleaved through the level (more in later sectors).
        if obstacle_pool:
            obstacle_waves = 3 + n // 15
            for i in range(obstacle_waves):
                t = 3.5 + i * (duration - 6) / max(1, obstacle_waves)
                kind = rng.choice(obstacle_pool)
                count = 2 + n // 25
                side = rng.choice(("left", "right", "both"))
                timeline.append((t, spawn_sides(kind, count, side=side)))

        # Wall sections in structural sectors. Place at a few moments during
        # the level so the player has to navigate around them.
        if has_walls:
            wall_times = [duration * 0.18, duration * 0.42, duration * 0.68]
            for i, t in enumerate(wall_times):
                side = rng.choice(("left", "right", "both"))
                timeline.append((t, spawn_wall_pair(sector_idx, side=side)))

        timeline.sort(key=lambda x: x[0])
    return timeline


def make_levels():
    levels = {}
    for n in range(1, 101):
        key = f"L{n:03d}"
        sector_idx = (n - 1) // 10
        slot = (n - 1) % 10
        is_boss = (slot == 9)
        sector_name = SECTOR_NAMES[sector_idx]
        nebula = SECTOR_NEBULAS[sector_idx]
        theme = SECTOR_RIBBONS[sector_idx]
        name = f"{sector_name} BOSS" if is_boss else f"{sector_name} {slot + 1}/9"
        duration = 999 if is_boss else min(45 + n // 2, 90)
        # Difficulty multiplies enemy HP. 1.0 at L1, scales toward 4.0 by L100.
        difficulty = 1.0 + (n - 1) * (3.0 / 99.0)
        levels[key] = Level(
            key=key,
            name=name,
            nebula=nebula,
            timeline=_gen_timeline(n, is_boss),
            duration=duration,
            has_boss=is_boss,
            theme=theme,
            difficulty=difficulty,
            sector_idx=sector_idx,
        )
    return levels


def make_test_level():
    """Hidden visual-checkup mission (SELECT+Y on the title screen): god
    mode, all weapons cycling, a parade of every station, then a wave of
    every enemy type, then every boss in order. No save data touched."""
    enemy_kinds = ["scout", "gunner", "weaver", "kamikaze", "turret", "bomber"]
    timeline = []
    # Station parade runs first as a PlayState phase (handled in _update);
    # the timeline below starts ticking once that's done.
    t = 1.5
    for kind in enemy_kinds:
        timeline.append((t, spawn_line(kind, 5, gap=60)))
        t += 6.0
    # Each boss spawned ~14 s apart. The hp_mul stays at 1 so the player
    # can shred them and move on quickly.
    def _make_boss_spawner(idx):
        def fn(state):
            key = f"boss_{idx}"
            asset = state.assets.get(key) or state.assets.get("boss")
            if asset is None:
                return
            flash = state.assets.get(f"{key}_flash") or state.assets.get("boss_flash")
            b = Boss(asset, flash, hp_mul=1.0, boss_n=idx + 1)
            b.sprite_name = key if asset is state.assets.get(key) else "boss"
            b._assets = state.assets
            state.enemies.append(b)
            state.is_boss_fight = True
            state.boss_spawned = True
            state.boss_intro_t = 1.6
            state.app.sounds["warn"].play()
        return fn
    for boss_idx in range(10):
        timeline.append((t, _make_boss_spawner(boss_idx)))
        t += 14.0
    return Level(
        key="TEST",
        name="VISUAL CHECKUP",
        nebula=(60, 80, 140),
        timeline=timeline,
        duration=t + 5,
        has_boss=True,
        theme="start",
        difficulty=1.0,
        sector_idx=0,
        is_test=True,
    )


# =============================================================================
# MISSION MAP
# =============================================================================

@dataclass
class MapNode:
    key: str
    name: str
    pos: tuple
    nexts: list


def _build_map_graph():
    """Linear 100-node graph organized into 10 sectors of 10 nodes each.
    Within a sector, slots 0-4 are on the top row, 5-9 on the bottom row.
    Each level points to the next (no branching in this build)."""
    graph = {}
    top_y = 180
    bot_y = 320
    x_left = 60
    x_step = 80
    for n in range(1, 101):
        key = f"L{n:03d}"
        slot = (n - 1) % 10
        if slot < 5:
            x = x_left + slot * x_step
            y = top_y
        else:
            x = x_left + (slot - 5) * x_step
            y = bot_y
        name = f"L{n}"
        nexts = [f"L{n + 1:03d}"] if n < 100 else []
        graph[key] = MapNode(key, name, (x, y), nexts)
    return graph


MAP_GRAPH = _build_map_graph()


# =============================================================================
# CONTROLS
# =============================================================================

class Controls:
    def __init__(self):
        self.left = self.right = self.up = self.down = False
        self.fire = False
        self.bomb_pressed = False
        self.ability_pressed = False
        self.confirm_pressed = False
        self.cancel_pressed = False
        self.start_pressed = False
        self.select = False
        self.start = False
        # Modifier triggers held this frame (used by hidden bot-replay shortcuts).
        self.l2_held = False
        self.r2_held = False
        # Shoulder buttons held this frame — drive the main-weapon swap:
        # nothing held = Vulcan, L1 = Pulse, R1 = Spread.
        self.l1_held = False
        self.r1_held = False
        # One-shot D-pad direction presses (used together with l2/r2 modifiers).
        self.dpad_left_pressed = False
        self.dpad_right_pressed = False
        self.dpad_up_pressed = False
        self.dpad_down_pressed = False

    def reset_pulses(self):
        self.bomb_pressed = False
        self.ability_pressed = False
        self.confirm_pressed = False
        self.cancel_pressed = False
        self.start_pressed = False
        self.dpad_left_pressed = False
        self.dpad_right_pressed = False
        self.dpad_up_pressed = False
        self.dpad_down_pressed = False

    def poll(self, joys, events):
        self.reset_pulses()
        keys = pygame.key.get_pressed()
        self.left = keys[pygame.K_LEFT]
        self.right = keys[pygame.K_RIGHT]
        self.up = keys[pygame.K_UP]
        self.down = keys[pygame.K_DOWN]
        self.fire = keys[pygame.K_z] or keys[pygame.K_SPACE]
        self.l2_held = False
        self.r2_held = False
        self.l1_held = False
        self.r1_held = False
        for j in joys:
            try:
                if j.get_numhats() > 0:
                    hx, hy = j.get_hat(0)
                    if hx < 0: self.left = True
                    if hx > 0: self.right = True
                    if hy > 0: self.up = True
                    if hy < 0: self.down = True
                if j.get_numaxes() >= 2:
                    ax, ay = j.get_axis(0), j.get_axis(1)
                    if ax < -0.4: self.left = True
                    if ax > 0.4: self.right = True
                    if ay < -0.4: self.up = True
                    if ay > 0.4: self.down = True
                fire_idx = BUTTON_SCHEME["fire"][0]
                if fire_idx < j.get_numbuttons() and j.get_button(fire_idx):
                    self.fire = True
                if JOY_SELECT < j.get_numbuttons():
                    self.select = bool(j.get_button(JOY_SELECT))
                if JOY_START < j.get_numbuttons():
                    self.start = bool(j.get_button(JOY_START))
                if JOY_L2 < j.get_numbuttons() and j.get_button(JOY_L2):
                    self.l2_held = True
                if JOY_R2 < j.get_numbuttons() and j.get_button(JOY_R2):
                    self.r2_held = True
                # Analog-trigger fallback for controllers that expose L2/R2
                # as axes. Layouts differ by platform:
                #   * Linux Xbox raw joystick (Steam Deck etc.):
                #       LT = axis 2, RT = axis 5
                #   * Windows XInput pygame:
                #       LT = axis 4, RT = axis 5
                # Picking the right LT index by sys.platform sidesteps the
                # false-positive risk of reading axis 4 on Linux (where it's
                # actually right-stick Y and a hard pushdown would fake an
                # L2 hold). RT is axis 5 on both. The handheld has L2/R2
                # wired as digital buttons (above) and effectively no analog
                # axes, so the < numaxes check skips this block there.
                lt_axis = 2 if sys.platform.startswith("linux") else 4
                n_ax = j.get_numaxes()
                if n_ax > lt_axis and j.get_axis(lt_axis) > 0.3:
                    self.l2_held = True
                if n_ax > 5 and j.get_axis(5) > 0.3:
                    self.r2_held = True
                if JOY_L1 < j.get_numbuttons() and j.get_button(JOY_L1):
                    self.l1_held = True
                if JOY_R1 < j.get_numbuttons() and j.get_button(JOY_R1):
                    self.r1_held = True
            except pygame.error:
                pass

        # Keyboard fallbacks for the L2/R2 modifier triggers — useful for
        # testing replay shortcuts at the desk without a controller.
        if keys[pygame.K_a]:
            self.l2_held = True
        if keys[pygame.K_d]:
            self.r2_held = True
        # Q/E mirror the L1/R1 shoulder holds (main-weapon swap on desk).
        if keys[pygame.K_q]:
            self.l1_held = True
        if keys[pygame.K_e]:
            self.r1_held = True

        for ev in events:
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_x:
                    self.bomb_pressed = True
                if ev.key == pygame.K_c:
                    self.ability_pressed = True
                if ev.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z):
                    self.confirm_pressed = True
                if ev.key == pygame.K_ESCAPE:
                    self.cancel_pressed = True
                if ev.key == pygame.K_p:
                    self.start_pressed = True
                if ev.key == pygame.K_LEFT:
                    self.dpad_left_pressed = True
                if ev.key == pygame.K_RIGHT:
                    self.dpad_right_pressed = True
                if ev.key == pygame.K_UP:
                    self.dpad_up_pressed = True
                if ev.key == pygame.K_DOWN:
                    self.dpad_down_pressed = True
            if ev.type == pygame.JOYHATMOTION:
                hx, hy = ev.value
                if hx < 0:  self.dpad_left_pressed = True
                if hx > 0:  self.dpad_right_pressed = True
                if hy > 0:  self.dpad_up_pressed = True
                if hy < 0:  self.dpad_down_pressed = True
            if ev.type == pygame.JOYBUTTONDOWN:
                # Per-scheme face-button routing: same physical position →
                # same action on every platform, only the silk-screen
                # letter we render in tips differs.
                if ev.button == BUTTON_SCHEME["bomb"][0]:
                    self.bomb_pressed = True
                if ev.button == BUTTON_SCHEME["ability"][0]:
                    self.ability_pressed = True
                if ev.button == BUTTON_SCHEME["fire"][0]:
                    self.confirm_pressed = True
                if ev.button == BUTTON_SCHEME["cancel"][0]:
                    self.cancel_pressed = True
                if ev.button == JOY_START:
                    self.start_pressed = True


# =============================================================================
# HUD
# =============================================================================

def _panel(surf, x, y, w, h, title=None, fonts=None):
    pygame.draw.rect(surf, (22, 26, 44), (x, y, w, h))
    pygame.draw.rect(surf, (60, 80, 130), (x, y, w, h), 1)
    cap = (110, 160, 220)
    pygame.draw.rect(surf, cap, (x, y, 5, 1))
    pygame.draw.rect(surf, cap, (x + w - 5, y, 5, 1))
    pygame.draw.rect(surf, cap, (x, y + h - 1, 5, 1))
    pygame.draw.rect(surf, cap, (x + w - 5, y + h - 1, 5, 1))
    pygame.draw.rect(surf, cap, (x, y, 1, 5))
    pygame.draw.rect(surf, cap, (x + w - 1, y, 1, 5))
    pygame.draw.rect(surf, cap, (x, y + h - 5, 1, 5))
    pygame.draw.rect(surf, cap, (x + w - 1, y + h - 5, 1, 5))
    if title and fonts:
        t = fonts["tiny"].render(title, False, (160, 200, 240))
        # title chip on the top edge
        chip_w = t.get_width() + 6
        pygame.draw.rect(surf, (22, 26, 44), (x + 6, y - 1, chip_w, 2))
        surf.blit(t, (x + 9, y - 6))


def _segbar(surf, x, y, w, h, ratio, color, segments=10):
    cell_w = max(1, (w - (segments - 1)) // segments)
    for i in range(segments):
        cell = pygame.Rect(x + i * (cell_w + 1), y, cell_w, h)
        pygame.draw.rect(surf, DARKER, cell)
        if (i + 0.5) / segments <= ratio:
            pygame.draw.rect(surf, color, cell)


# HUD geometry constants — shared between the chrome builder and the live
# per-frame overlay paths so both agree on where each panel sits.
_HUD_INNER_W = HUD_W - 12
_HUD_PAD_TOP = 12   # gap between panel top border and first content line
_HUD_LINE_H = 14    # vertical step between content lines
_HUD_MISSION_Y = 38
_HUD_STATUS_Y = 84
_HUD_LOADOUT_Y = 150
_HUD_ARMS_Y = 254
_HUD_CONTROL_Y = SCREEN_H - 92


class _HudCache:
    """Cached HUD chrome (panels + semi-static labels). Rebuilt only when
    the (level, loadout, bombs, ability) fingerprint changes. The dynamic
    numbers — time / score / credits / shield bar / ability cd bar — are
    drawn live on top each frame and never go into the cache.

    `dyn_surf` is the per-frame SRCALPHA scratch surface that the dynamic
    layout walker paints into. Allocated once and reused — pygame.Surface
    SRCALPHA allocation was costing ~0.5-1 ms/frame on the mali driver.

    `dyn_records` is a list of `_DynRecord` instances built at flatten
    time. Each record carries every spec field that doesn't depend on
    per-frame template_vars already resolved (font object, color tuple,
    anchor key, format template, alpha, shadow flag, etc.) so the per-
    frame draw skips dict-lookup + type-cast work. The records also
    carry the parent container id so a runtime animation offset on
    `container_offsets` is applied correctly per frame — see comments
    on `container_offsets` below."""
    surface = None
    key = None
    dyn_surf = None
    dyn_records = None
    dyn_records_rev = None
    # Runtime container offsets — keyed by container id, value is (dx, dy).
    # Animation code (anything that wants to shake / slide a panel) can set
    # _HudCache.container_offsets["status_panel"] = (3, 0) and every
    # dynamic item inside that container will draw at the offset position
    # this frame. Unanimated containers stay out of the dict (zero cost).
    # The chrome cache surface does NOT animate — for whole-panel slides
    # the caller should also offset the chrome blit or invalidate the
    # cache; the records system only handles the dynamic items overlay.
    container_offsets = {}


def _hud_cache_key(player, level_name, save=None):
    lo = player.loadout
    side_lvl = lo.side_level() if lo.side_type != "none" else 0
    # Include the per-category unlocked-tier state so the HUD bars rebuild
    # when boss kills change the visible-tier count. Tier state changes
    # only between levels, so the cache still invalidates rarely.
    if save is not None:
        unlocks = (save.unlocked_tier_pulse, save.unlocked_tier_spread,
                   save.unlocked_tier_vulcan, save.unlocked_tier_missile,
                   save.unlocked_tier_drone, save.unlocked_tier_shield,
                   save.unlocked_tier_engine)
    else:
        unlocks = (5, 5, 5, 5, 5, 5, 5)
    return (level_name, lo.main_type, lo.main_level(),
            lo.side_type, side_lvl, lo.shield, lo.engine,
            lo.bombs, lo.ability, unlocks, _LAYOUT_REV)


def _hud_panel(id_, x, y, w, h, *, title="", children=()):
    """One HUD chrome panel as a container spec. panel_skin=1 gives the
    bordered look (bg/border/caps/title chip) for free — any of those
    can still be overridden by setting the field explicitly here."""
    return {
        "id": id_, "type": "container",
        "x": x, "y": y, "w": w, "h": h,
        "layout": "free", "padding": 0,
        "panel_skin": 1,
        "title": title,
        "children": list(children),
    }


def _hud_lvl_bar_x_y_w(panel_inner_w, inset=0):
    """Geometry helper for the loadout level pips — keeps the lambdas in
    the spec compact."""
    return 8 + inset, 6 - 1, panel_inner_w - 16 - inset


def _build_shop_panel_spec():
    """Right-side strip on the shop screen — header, balance, and
    control hints. Returned as a single shop_root container positioned
    at HUD_X. Dynamic value: {credits} on the balance readout."""
    INNER = HUD_W - 12   # 148

    header_panel = {
        "id": "shop_header_panel", "type": "container",
        "x": 6, "y": 6, "w": INNER, "h": 24,
        "layout": "free", "padding": 0,
        "panel_skin": 1,
        "children": [
            {"id": "shop_header_title", "type": "text",
             "x": INNER // 2, "y": 12, "anchor": "c",
             "text": "PEWPEW", "font": 2, "color": [80, 220, 255]},
        ],
    }
    bal_y, bal_h = 40, 72
    balance_panel = {
        "id": "shop_balance_panel", "type": "container",
        "x": 6, "y": bal_y, "w": INNER, "h": bal_h,
        "layout": "free", "padding": 0,
        "panel_skin": 1, "title": "BALANCE",
        "children": [
            {"id": "shop_balance_value", "type": "text",
             "x": INNER // 2, "y": bal_h // 2, "anchor": "c",
             "text": "${credits}", "font": 3,
             "color": [255, 220, 80], "dynamic": True},
        ],
    }
    chy = SCREEN_H - 98
    control_panel = {
        "id": "shop_control_panel", "type": "container",
        "x": 6, "y": chy, "w": INNER, "h": 92,
        "layout": "free", "padding": 0,
        "panel_skin": 1, "title": "CONTROL",
        "children": [
            {"id": "shop_ctrl_dpad_icon", "type": "text",
             "x": 6, "y": 18, "anchor": "tl",
             "text": "{dpad}", "font": 2, "color": [80, 220, 255]},
            {"id": "shop_ctrl_dpad_label", "type": "text",
             "x": 40, "y": 16, "anchor": "tl",
             "text": "pick", "font": 2, "color": [140, 140, 160]},
            {"id": "shop_ctrl_b", "type": "text",
             "x": 6, "y": 36, "anchor": "tl",
             "text": "{btn_fire}", "font": 2, "color": [80, 220, 255]},
            {"id": "shop_ctrl_b_label", "type": "text",
             "x": 40, "y": 36, "anchor": "tl",
             "text": "buy", "font": 2, "color": [140, 140, 160]},
            {"id": "shop_ctrl_y", "type": "text",
             "x": 6, "y": 56, "anchor": "tl",
             "text": "{btn_cancel}", "font": 2, "color": [80, 220, 255]},
            {"id": "shop_ctrl_y_label", "type": "text",
             "x": 40, "y": 56, "anchor": "tl",
             "text": "exit", "font": 2, "color": [140, 140, 160]},
        ],
    }

    return {
        "id": "shop_root", "type": "container",
        "x": HUD_X, "y": 0, "w": HUD_W, "h": SCREEN_H,
        "layout": "free", "padding": 0,
        "panel_skin": 0,   # plain strip — no auto panel chrome
        "bg": [15, 18, 32],   # HUD_BG fills the strip
        "_label": "Shop side panel (right strip)",
        "children": [
            {"id": "shop_left_line", "type": "rect",
             "x": 0, "y": 0, "w": 1, "h": SCREEN_H,
             "color": [40, 48, 80], "alpha": 255},
            header_panel, balance_panel, control_panel,
        ],
    }


def _build_map_panel_spec():
    """Right-side strip on the map screen — header, STATUS readout,
    LOADOUT summary, CONTROL hints. Dynamic: credits, high_score,
    progress_n / ratio, main_name / main_lvl, shield_lvl."""
    INNER = HUD_W - 12

    header_panel = {
        "id": "map_header_panel", "type": "container",
        "x": 6, "y": 6, "w": INNER, "h": 26,
        "layout": "free", "padding": 0,
        "panel_skin": 1,
        "children": [
            {"id": "map_header_title", "type": "text",
             "x": INNER // 2, "y": 13, "anchor": "c",
             "text": "PEWPEW", "font": 2, "color": [80, 220, 255]},
        ],
    }
    status_panel = {
        "id": "map_status_panel", "type": "container",
        "x": 6, "y": 38, "w": INNER, "h": 78,
        "layout": "free", "padding": 0,
        "panel_skin": 1, "title": "STATUS",
        "children": [
            {"id": "map_credits", "type": "text",
             "x": 8, "y": 14, "anchor": "tl",
             "text": "$ {credits}", "font": 2,
             "color": [255, 220, 80], "dynamic": True},
            {"id": "map_high_score", "type": "text",
             "x": 8, "y": 32, "anchor": "tl",
             "text": "HI {high_score:07d}", "font": 2,
             "color": [140, 140, 160], "dynamic": True},
            {"id": "map_progress_text", "type": "text",
             "x": 8, "y": 50, "anchor": "tl",
             "text": "PROG {progress_n}/100", "font": 2,
             "color": [255, 140, 40], "dynamic": True},
            {"id": "map_progress_bar", "type": "progress_bar",
             "x": 8, "y": 68, "w": INNER - 16, "h": 6,
             "value": "{progress_ratio}", "max": 1.0, "segments": 10,
             "color": [90, 230, 120], "bg_color": [60, 64, 88],
             "dynamic": True},
        ],
    }
    loadout_panel = {
        "id": "map_loadout_panel", "type": "container",
        "x": 6, "y": 122, "w": INNER, "h": 68,
        "layout": "free", "padding": 0,
        "panel_skin": 1, "title": "LOADOUT",
        "children": [
            {"id": "map_main_name", "type": "text",
             "x": 8, "y": 16, "anchor": "tl",
             "text": "{main_name}", "font": 1,
             "color": [80, 220, 255]},
            {"id": "map_main_bar", "type": "tiered_bar",
             "x": 8, "y": 27, "h": 8,
             "value": "{main_lvl}", "max": "{main_visible_max}",
             "tiers": "{main_visible_tiers}", "cell_px_w": 24,
             "color": [240, 240, 240], "bg_color": [60, 64, 88]},
            {"id": "map_shld_label", "type": "text",
             "x": 8, "y": 39, "anchor": "tl",
             "text": "SHLD", "font": 2, "color": [140, 140, 160]},
            {"id": "map_shld_bar", "type": "tiered_bar",
             "x": 58, "y": 42, "h": 8,
             "value": "{shield_lvl}", "max": "{shield_visible_max}",
             "tiers": "{shield_visible_tiers}", "cell_px_w": 16,
             "color": [240, 240, 240], "bg_color": [60, 64, 88]},
        ],
    }
    chy = SCREEN_H - 116
    control_panel = {
        "id": "map_control_panel", "type": "container",
        "x": 6, "y": chy, "w": INNER, "h": 108,
        "layout": "free", "padding": 0,
        "panel_skin": 1, "title": "CONTROL",
        "children": [
            {"id": "map_ctrl_dpad_icon", "type": "text",
             "x": 8, "y": 16, "anchor": "tl",
             "text": "{dpad}", "font": 2, "color": [80, 220, 255]},
            {"id": "map_ctrl_dpad_label", "type": "text",
             "x": 60, "y": 14, "anchor": "tl",
             "text": "pick", "font": 2, "color": [140, 140, 160]},
            {"id": "map_ctrl_lr", "type": "text",
             "x": 8, "y": 32, "anchor": "tl",
             "text": "L/R", "font": 2, "color": [80, 220, 255]},
            {"id": "map_ctrl_lr_label", "type": "text",
             "x": 60, "y": 32, "anchor": "tl",
             "text": "sector", "font": 2, "color": [140, 140, 160]},
            {"id": "map_ctrl_b", "type": "text",
             "x": 8, "y": 50, "anchor": "tl",
             "text": "{btn_fire}", "font": 2, "color": [80, 220, 255]},
            {"id": "map_ctrl_b_label", "type": "text",
             "x": 60, "y": 50, "anchor": "tl",
             "text": "launch", "font": 2, "color": [140, 140, 160]},
            {"id": "map_ctrl_y", "type": "text",
             "x": 8, "y": 68, "anchor": "tl",
             "text": "{btn_cancel}", "font": 2, "color": [80, 220, 255]},
            {"id": "map_ctrl_y_label", "type": "text",
             "x": 60, "y": 68, "anchor": "tl",
             "text": "shop", "font": 2, "color": [140, 140, 160]},
            {"id": "map_ctrl_slx", "type": "text",
             "x": 8, "y": 86, "anchor": "tl",
             "text": "SL+{btn_ability}", "font": 2, "color": [80, 220, 255]},
            {"id": "map_ctrl_slx_label", "type": "text",
             "x": 60, "y": 86, "anchor": "tl",
             "text": "unlock", "font": 2, "color": [140, 140, 160]},
        ],
    }

    return {
        "id": "map_root", "type": "container",
        "x": HUD_X, "y": 0, "w": HUD_W, "h": SCREEN_H,
        "layout": "free", "padding": 0,
        "panel_skin": 0,
        "bg": [15, 18, 32],
        "_label": "Map side panel (right strip)",
        "children": [
            {"id": "map_strip_line", "type": "rect",
             "x": 0, "y": 0, "w": 1, "h": SCREEN_H,
             "color": [40, 48, 80], "alpha": 255},
            header_panel, status_panel, loadout_panel, control_panel,
        ],
    }


def _build_play_banner_spec():
    """Centre-of-playfield banner used for PAUSED / MISSION COMPLETE /
    SHIP DESTROYED. One container gated by `banner_visible`; the title
    + subtitle text is template-driven (`{banner_title}` /
    `{banner_subtitle}`) so the same spec covers all three states."""
    cy = PLAY_H // 2
    return {
        "id": "play_banner", "type": "container",
        "x": 0, "y": cy - 40, "w": PLAY_W, "h": 80,
        "layout": "free", "padding": 0,
        "panel_skin": 0,
        "bg": [0, 0, 0], "alpha": 160,
        "visible_when": "banner_visible",
        "_label": "centre-screen banner (pause / win / loss)",
        "children": [
            {"id": "play_banner_title", "type": "text",
             "x": PLAY_W // 2, "y": 30, "anchor": "c",
             "text": "{banner_title}", "font": 3,
             "color": [240, 240, 240], "dynamic": True},
            {"id": "play_banner_subtitle", "type": "text",
             "x": PLAY_W // 2, "y": 60, "anchor": "c",
             "text": "{banner_subtitle}", "font": 2,
             "color": [140, 140, 160], "dynamic": True},
        ],
    }


def _build_hud_layout_spec():
    """Programmatic build of the HUD layout tree. Returned as a single
    `hud_root` container containing the six chrome panels (header,
    mission, status, loadout, arms, control) plus per-frame dynamic items
    flagged `dynamic: True`. Everything position / color / text is data-
    driven from here so the layout editor exposes it for direct editing."""
    INNER = _HUD_INNER_W
    PAD = _HUD_PAD_TOP
    LH = _HUD_LINE_H

    header_panel = _hud_panel("header_panel", 6, 6, INNER, 26, children=[
        {"id": "header_title", "type": "text",
         "x": INNER // 2, "y": 13, "anchor": "c",
         "text": "PEWPEW", "font": 2, "color": [80, 220, 255]},
    ])

    mission_panel = _hud_panel("mission_panel", 6, 38, INNER, 38,
                                title="MISSION", children=[
        {"id": "mission_label", "type": "text",
         "x": 8, "y": PAD, "anchor": "tl",
         "text": "{level_short}", "font": 1, "color": [240, 240, 240]},
        {"id": "mission_timer", "type": "text",
         "x": 8, "y": PAD + LH, "anchor": "tl",
         "text": "T {time}s", "font": 1, "color": [140, 140, 160],
         "dynamic": True},
    ])

    status_panel = _hud_panel("status_panel", 6, 84, INNER, 58,
                               title="STATUS", children=[
        {"id": "status_shld_label", "type": "text",
         "x": 8, "y": PAD, "anchor": "tl",
         "text": "SHLD", "font": 1, "color": [140, 140, 160]},
        {"id": "status_shield_bar", "type": "progress_bar",
         "x": 40, "y": PAD + 1, "w": INNER - 46, "h": 6,
         "value": "{shield_ratio}", "max": 1.0, "segments": 10,
         "color": [80, 220, 255], "bg_color": [60, 64, 88],
         "dynamic": True},
        {"id": "status_score", "type": "text",
         "x": 8, "y": PAD + LH, "anchor": "tl",
         "text": "SC {score:07d}", "font": 1, "color": [240, 240, 240],
         "dynamic": True},
        {"id": "status_credits", "type": "text",
         "x": 8, "y": PAD + LH * 2, "anchor": "tl",
         "text": "$ {credits}", "font": 1, "color": [255, 220, 80],
         "dynamic": True},
    ])

    # Loadout panel: labels + level-pip bars. Color of the pip bars goes
    # GREEN at max (template_vars carries the resolved color list per row).
    loadout_panel = _hud_panel("loadout_panel", 6, 150, INNER, 96,
                                title="LOADOUT", children=[
        {"id": "loadout_main_name", "type": "text",
         "x": 8, "y": PAD, "anchor": "tl",
         "text": "{main_name}", "font": 1, "color": [80, 220, 255]},
        {"id": "loadout_main_bar", "type": "tiered_bar",
         "x": 8, "y": PAD + LH + 1, "h": 8,
         "value": "{main_lvl}", "max": "{main_visible_max}",
         "tiers": "{main_visible_tiers}", "cell_px_w": 24,
         "color": "{main_lvl_color}", "bg_color": [60, 64, 88]},
        {"id": "loadout_side_name", "type": "text",
         "x": 8, "y": PAD + LH * 2 + 2, "anchor": "tl",
         "text": "{side_name}", "font": 1, "color": [255, 140, 40]},
        {"id": "loadout_side_bar", "type": "tiered_bar",
         "x": 8, "y": PAD + LH * 3 + 3, "h": 8,
         "value": "{side_lvl}", "max": "{side_visible_max}",
         "tiers": "{side_visible_tiers}", "cell_px_w": 24,
         "color": "{side_lvl_color}", "bg_color": [60, 64, 88],
         "visible_when": "side_visible"},
        # Shield + Engine rows: label on the left, pip bar on the right.
        {"id": "loadout_shld_label", "type": "text",
         "x": 8, "y": PAD + LH * 4, "anchor": "tl",
         "text": "SHLD", "font": 1, "color": [140, 140, 160]},
        {"id": "loadout_shld_bar", "type": "tiered_bar",
         "x": 40, "y": PAD + LH * 4 + 1, "h": 8,
         "value": "{shield_lvl}", "max": "{shield_visible_max}",
         "tiers": "{shield_visible_tiers}", "cell_px_w": 18,
         "color": "{shield_lvl_color}", "bg_color": [60, 64, 88]},
        {"id": "loadout_engn_label", "type": "text",
         "x": 8, "y": PAD + LH * 5, "anchor": "tl",
         "text": "ENGN", "font": 1, "color": [140, 140, 160]},
        {"id": "loadout_engn_bar", "type": "tiered_bar",
         "x": 40, "y": PAD + LH * 5 + 1, "h": 8,
         "value": "{engine_lvl}", "max": "{engine_visible_max}",
         "tiers": "{engine_visible_tiers}", "cell_px_w": 18,
         "color": "{engine_lvl_color}", "bg_color": [60, 64, 88]},
    ])

    # Arms panel: BOMB count + ability name (dim baseline; bright overlay
    # painted on top per-frame when the ability is ready) + cooldown bar.
    arms_panel = _hud_panel("arms_panel", 6, 254, INNER, 54,
                             title="ARMS", children=[
        {"id": "arms_bomb", "type": "text",
         "x": 8, "y": PAD, "anchor": "tl",
         "text": "BOMB x{bombs}", "font": 1, "color": [200, 90, 220]},
        {"id": "arms_ability_dim", "type": "text",
         "x": 8, "y": PAD + LH, "anchor": "tl",
         "text": "{ability_name}", "font": 1, "color": [140, 140, 160]},
        {"id": "arms_ability_ready", "type": "text",
         "x": 8, "y": PAD + LH, "anchor": "tl",
         "text": "{ability_name}", "font": 1, "color": [255, 140, 40],
         "dynamic": True, "visible_when": "ability_ready"},
        {"id": "arms_ability_cd_bar", "type": "progress_bar",
         "x": 8, "y": PAD + LH * 2 + 2, "w": INNER - 16, "h": 5,
         "value": "{ability_cd_ratio}", "max": 1.0, "segments": 8,
         "color": "{ability_cd_color}", "bg_color": [60, 64, 88],
         "dynamic": True},
    ])

    # Control hints — fully static.
    control_panel = _hud_panel("control_panel", 6, SCREEN_H - 92, INNER, 86,
                                title="CONTROL", children=[
        {"id": "ctrl_dpad_label", "type": "text",
         "x": 32, "y": PAD, "anchor": "tl",
         "text": "move",    "font": 1, "color": [140, 140, 160]},
        # D-pad icon as a separate child (rendered via {dpad} placeholder
        # in a text item — re-uses the title-screen tip pattern).
        {"id": "ctrl_dpad_icon", "type": "text",
         "x": 8, "y": PAD - 1, "anchor": "tl",
         "text": "{dpad}", "font": 1, "color": [80, 220, 255]},
        {"id": "ctrl_b", "type": "text",
         "x": 8, "y": PAD + LH, "anchor": "tl",
         "text": "{btn_fire}", "font": 1, "color": [80, 220, 255]},
        {"id": "ctrl_b_label", "type": "text",
         "x": 32, "y": PAD + LH, "anchor": "tl",
         "text": "fire", "font": 1, "color": [140, 140, 160]},
        {"id": "ctrl_a", "type": "text",
         "x": 8, "y": PAD + LH * 2, "anchor": "tl",
         "text": "{btn_bomb}", "font": 1, "color": [80, 220, 255]},
        {"id": "ctrl_a_label", "type": "text",
         "x": 32, "y": PAD + LH * 2, "anchor": "tl",
         "text": "bomb", "font": 1, "color": [140, 140, 160]},
        {"id": "ctrl_x", "type": "text",
         "x": 8, "y": PAD + LH * 3, "anchor": "tl",
         "text": "{btn_ability}", "font": 1, "color": [80, 220, 255]},
        {"id": "ctrl_x_label", "type": "text",
         "x": 32, "y": PAD + LH * 3, "anchor": "tl",
         "text": "ability", "font": 1, "color": [140, 140, 160]},
        {"id": "ctrl_st", "type": "text",
         "x": 8, "y": PAD + LH * 4, "anchor": "tl",
         "text": "ST", "font": 1, "color": [80, 220, 255]},
        {"id": "ctrl_st_label", "type": "text",
         "x": 32, "y": PAD + LH * 4, "anchor": "tl",
         "text": "pause", "font": 1, "color": [140, 140, 160]},
    ])

    return [{
        "id": "hud_root", "type": "container",
        "x": 0, "y": 0, "w": HUD_W, "h": SCREEN_H,
        "layout": "free", "padding": 0,
        # panel_skin=0 = no automatic chrome (no border / caps / title);
        # the explicit bg below still paints the strip black.
        "panel_skin": 0,
        "bg": [15, 18, 32],  # HUD_BG
        "_label": "HUD root container (positioned at HUD_X internally)",
        "children": [
            {"id": "hud_left_line", "type": "rect",
             "x": 0, "y": 0, "w": 1, "h": SCREEN_H,
             "color": [40, 48, 80], "alpha": 255},
            header_panel, mission_panel, status_panel,
            loadout_panel, arms_panel, control_panel,
        ],
    }]


def _hud_chrome_vars(level_name, lo, save=None):
    """Vars referenced by non-dynamic HUD items (cached chrome). Resolved
    at chrome-bake time — when these change, the chrome cache fingerprint
    invalidates and the chrome surface gets re-rendered.

    When `save` is provided, the visible-tier counts for each weapon are
    included so HUD/map bars can shrink/grow with boss-gated unlocks
    instead of always showing all 5 tiers."""
    parts = level_name.split()
    slot = parts[-1] if parts and "/" in parts[-1] else ""
    short = parts[0].upper() if parts else ""
    if slot:
        short = f"{short} {slot}"
    main_lvl = lo.main_level()
    side_lvl = lo.side_level() if lo.side_type != "none" else 0
    g = list(GREEN); w_ = list(WHITE)
    # Visible tiers per upgrade — defaults to fully-unlocked (5) if save
    # isn't supplied (keeps backward compat for callers that don't pass it).
    if save is not None:
        mtier = getattr(save, f"unlocked_tier_{lo.main_type}", 5)
        stier = (getattr(save, f"unlocked_tier_{lo.side_type}", 5)
                 if lo.side_type != "none" else 0)
        shtier = getattr(save, "unlocked_tier_shield", 5)
        entier = getattr(save, "unlocked_tier_engine", 5)
    else:
        mtier = 5; stier = 5; shtier = 5; entier = 5
    # Main weapon: 4 sub-levels per tier. Others: 1.
    main_visible_max = mtier * 4
    side_visible_max = stier * 1
    return {
        "level_short": short,
        "main_name": MAIN_WEAPON_NAMES[lo.main_type].upper(),
        "main_lvl": main_lvl, "main_max": MAIN_WEAPON_MAX,
        "main_visible_tiers": mtier, "main_visible_max": main_visible_max,
        "main_lvl_color": g if main_lvl >= MAIN_WEAPON_MAX else w_,
        "side_name": SIDE_WEAPON_NAMES[lo.side_type].upper(),
        "side_lvl": side_lvl, "side_max": SIDE_WEAPON_MAX,
        "side_visible_tiers": stier, "side_visible_max": side_visible_max,
        "side_lvl_color": g if side_lvl >= SIDE_WEAPON_MAX else w_,
        "side_visible": lo.side_type != "none",
        "shield_lvl": lo.shield, "shield_max": MAX_LEVELS["shield"],
        "shield_visible_tiers": shtier, "shield_visible_max": shtier,
        "shield_lvl_color": g if lo.shield >= MAX_LEVELS["shield"] else w_,
        "engine_lvl": lo.engine, "engine_max": MAX_LEVELS["engine"],
        "engine_visible_tiers": entier, "engine_visible_max": entier,
        "engine_lvl_color": g if lo.engine >= MAX_LEVELS["engine"] else w_,
        "bombs": lo.bombs,
        "ability_name": ABILITY_NAMES.get(lo.ability, "?").upper(),
        # Face-button silk letters — same physical position, per-platform
        # label. Layout tip strings reference {btn_fire} etc.
        **button_label_vars(),
    }


def _hud_dyn_vars(player, save, score, time_left):
    """Per-frame vars referenced by `dynamic: True` HUD items."""
    sh_ratio = (max(0, player.shield_hp / player.shield_max)
                if player.shield_max > 0 else 0.0)
    cd_ratio = clamp(1 - player.ability_cd / 18.0, 0, 1)
    return {
        "time": max(0, int(time_left)),
        "shield_ratio": sh_ratio,
        "score": int(score),
        "credits": save.credits,
        "ability_ready": player.ability_cd <= 0,
        "ability_cd_ratio": cd_ratio,
        "ability_cd_color": (list(ORANGE) if cd_ratio >= 1
                             else [130, 80, 40]),
    }


def _build_hud_chrome(fonts, level_name, lo, save=None):
    """Render the static (cached) HUD chrome from the layout tree. Walks
    LAYOUT_ELEMENTS["hud"] with dynamic_filter=False; dynamic items are
    skipped here and painted per-frame by hud_draw()."""
    surf = pygame.Surface((HUD_W, SCREEN_H))
    surf.fill(HUD_BG)
    tvars = _hud_chrome_vars(level_name, lo, save)
    for it in resolved_layout_tree("hud"):
        _layout_draw_item(surf, it, fonts, None, tvars,
                          dynamic_filter=False)
    try:
        return surf.convert()
    except pygame.error:
        return surf


_LAYOUT_PATH = Path(__file__).resolve().parent / "art" / "layout.json"
_LAYOUT_CACHE = None
_LAYOUT_REV = 0     # bumped on every reload — cache keys use it to invalidate
_LAYOUT_SCREENS = ("title", "map", "shop", "play", "hud", "gameover")


def _layout_load():
    global _LAYOUT_CACHE
    if _LAYOUT_CACHE is not None:
        return _LAYOUT_CACHE
    data = {}
    if _LAYOUT_PATH.exists():
        try:
            data = json.loads(_LAYOUT_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"layout.json load failed: {e}")
            data = {}
    screens = data.get("screens") or {}
    out = {s: (screens.get(s) or {}).get("items") or [] for s in _LAYOUT_SCREENS}
    _LAYOUT_CACHE = out
    return out


def reload_layout():
    """Drop the cache so the next draw picks up edits from disk."""
    global _LAYOUT_CACHE, _LAYOUT_REV
    _LAYOUT_CACHE = None
    _LAYOUT_REV += 1


def _layout_collect_deep_ids(items):
    """Set of ids that appear *nested* inside any container's children
    at any depth — top-level items don't count. Used to detect builtins
    that the layout editor has carried into a non-root container; the
    spec for such ids should not render at root (would duplicate the
    moved copy)."""
    out = set()
    def _walk(its):
        for it in its:
            rid = it.get("id")
            if rid:
                out.add(rid)
            children = it.get("children")
            if children:
                _walk(children)
    for it in items or ():
        children = it.get("children")
        if children:
            _walk(children)
    return out


_RESOLVED_TREE_CACHE = {}   # {screen_name: (rev, tree)}


def resolved_layout_tree(screen_name):
    """Apply layout.json overrides on top of LAYOUT_ELEMENTS for `screen_name`.
    For each spec entry, an override with the matching id wins for every
    non-id/type field (children inclusive — the editor saves the whole
    modified subtree). User-added items (no matching spec id) append at the
    end so they render on top.

    Result is cached per (screen_name, _LAYOUT_REV) — the layout doesn't
    change during gameplay (only the editor mutates it via reload_layout()
    which bumps _LAYOUT_REV). The HUD path used to re-resolve this every
    frame, costing meaningful ms inside hud_draw on the RG35XX Pro."""
    cached = _RESOLVED_TREE_CACHE.get(screen_name)
    if cached is not None and cached[0] == _LAYOUT_REV:
        return cached[1]
    spec = LAYOUT_ELEMENTS.get(screen_name) or []
    overrides_list = _layout_load().get(screen_name) or []
    overrides = {it.get("id"): it for it in overrides_list if it.get("id")}
    # Builtins whose override has been moved into a child container are
    # skipped here — the engine will draw them at their new location via
    # the container walk; rendering the spec at root too would duplicate.
    moved_deep = _layout_collect_deep_ids(overrides_list)
    out = []
    spec_ids = set()
    for spec_item in spec:
        sid = spec_item.get("id")
        spec_ids.add(sid)
        if sid and sid in moved_deep:
            continue
        ov = overrides.get(sid)
        if ov:
            merged = dict(spec_item)
            for k, v in ov.items():
                if k in ("id", "type"):
                    continue
                merged[k] = v
            out.append(merged)
        else:
            out.append(spec_item)
    for it in overrides_list:
        if it.get("id") not in spec_ids:
            out.append(it)
    _RESOLVED_TREE_CACHE[screen_name] = (_LAYOUT_REV, out)
    return out


_LAYOUT_ANCHOR_AX = {"tl":0,"t":0.5,"tr":1,"l":0,"c":0.5,"r":1,"bl":0,"b":0.5,"br":1}
_LAYOUT_ANCHOR_AY = {"tl":0,"t":0,"tr":0,"l":0.5,"c":0.5,"r":0.5,"bl":1,"b":1,"br":1}


def _layout_anchor_offset(anchor, w, h):
    ax = _LAYOUT_ANCHOR_AX.get(anchor, 0.0)
    ay = _LAYOUT_ANCHOR_AY.get(anchor, 0.0)
    return int(round(-w * ax)), int(round(-h * ay))


def _resolve_layout_font(fonts, it, default_scale=3):
    """Look up the font for a layout item. `font` is the integer scale
    (clamped 1..7 for 5x7, 1..4 for 7x9); `font_family` selects the
    glyph set (default = 5x7, "7x9" = the mid-size family). Falls
    back to the integer key and finally to "big" so any item still
    renders even if its desired font cell isn't loaded."""
    fam = (it.get("font_family") or "").strip()
    scale = int(it.get("font", default_scale))
    if fam == "7x9":
        scale = max(1, min(4, scale))
        return (fonts.get((fam, scale))
                or fonts.get(scale)
                or fonts.get("big"))
    scale = max(1, min(7, scale))
    return fonts.get(scale) or fonts.get("big")


def _layout_draw_text(surf, it, fonts):
    text = str(it.get("text") or "")
    if not text:
        return
    color = tuple(it.get("color") or (240, 240, 240))[:3]
    alpha = int(it.get("alpha", 255))
    font = _resolve_layout_font(fonts, it)
    img = font.render(text, False, color)
    ox, oy = _layout_anchor_offset(it.get("anchor", "tl"),
                                   img.get_width(), img.get_height())
    if it.get("shadow"):
        sh = font.render(text, False, (0, 0, 0))
        sh.set_alpha(min(alpha, 180))
        surf.blit(sh, (int(it.get("x", 0)) + ox + 1,
                       int(it.get("y", 0)) + oy + 1))
    if alpha < 255:
        img = img.copy()
        img.set_alpha(alpha)
    surf.blit(img, (int(it.get("x", 0)) + ox, int(it.get("y", 0)) + oy))


def _layout_draw_rect(surf, it):
    color = tuple(it.get("color") or (60, 80, 120))[:3]
    alpha = int(it.get("alpha", 200))
    outline = int(it.get("outline", 0))
    x = int(it.get("x", 0))
    y = int(it.get("y", 0))
    w = max(1, int(it.get("w", 10)))
    h = max(1, int(it.get("h", 10)))
    if alpha >= 255:
        if outline > 0:
            pygame.draw.rect(surf, color, (x, y, w, h), outline)
        else:
            pygame.draw.rect(surf, color, (x, y, w, h))
        return
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    col = (color[0], color[1], color[2], alpha)
    if outline > 0:
        pygame.draw.rect(s, col, (0, 0, w, h), outline)
    else:
        s.fill(col)
    surf.blit(s, (x, y))


def _layout_sprite_lookup(assets, name):
    if not name or not assets:
        return None
    img = assets.get(name)
    if img is not None:
        return img
    # Fall back to the on-disk sprite (covers names not in the runtime asset
    # dict, e.g. backdrops or UI-only sprites).
    path = _LAYOUT_PATH.parent / "sprites" / f"{name}.png"
    if not path.exists():
        return None
    try:
        return pygame.image.load(str(path)).convert_alpha()
    except Exception:
        return None


def _layout_draw_image(surf, it, assets):
    name = it.get("sprite")
    img = _layout_sprite_lookup(assets, name)
    if img is None:
        return
    scale = float(it.get("scale", 1.0))
    if abs(scale - 1.0) > 0.001:
        sw, sh = img.get_size()
        img = pygame.transform.smoothscale(
            img, (max(1, int(sw * scale)), max(1, int(sh * scale))))
    alpha = int(it.get("alpha", 255))
    if alpha < 255:
        img = img.copy()
        img.set_alpha(alpha)
    ox, oy = _layout_anchor_offset(it.get("anchor", "tl"),
                                   img.get_width(), img.get_height())
    surf.blit(img, (int(it.get("x", 0)) + ox, int(it.get("y", 0)) + oy))


def _resolve_var(val, template_vars, default):
    """Type-preserving template lookup. If `val` is the literal "{name}"
    reference, return template_vars[name] (a list / int / float survives
    intact). If the key isn't in template_vars, fall back to `default`
    (silently — beats spamming the console with KeyErrors when the editor
    previews a screen without the engine's full template_vars dict).
    Otherwise return val unchanged."""
    if (isinstance(val, str) and len(val) >= 3
            and val.startswith("{") and val.endswith("}")
            and "{" not in val[1:-1]):
        key = val[1:-1]
        if template_vars and key in template_vars:
            return template_vars[key]
        return default
    return val if val is not None else default


def _layout_draw_tiered_bar(surf, it, template_vars):
    """Weapon-level progress bar with tier+sub-level shape.

    Tier segments are laid out horizontally; each segment fills vertically
    bottom-up by sub-level. Thin separators across each segment mark the
    sub-level divisions — they vanish when the segment is fully filled,
    so a maxed tier reads as a solid block.

    Fields:
      x, y, w, h
      value          current level (1..max)
      max            max level (e.g. 20 for main, 12 for side)
      tiers          number of tier segments (default 5)
      cell_px_w      OPTIONAL fixed pixel width per tier cell — if set,
                     overrides `w` and derives total bar width from
                     `tiers * cell_px_w + (tiers - 1) gap`. Keeps cell
                     width uniform across rows even as tier count shrinks
                     (e.g. while bosses are locked).
      color          fill color
      bg_color       empty-segment color
      sep_color      thin sub-level separator color (default dim)
    """
    tvars = template_vars or {}
    x = int(it.get("x", 0))
    y = int(it.get("y", 0))
    h = max(2, int(it.get("h", 10)))
    tiers = max(1, int(_resolve_var(it.get("tiers", 5), tvars, 5)))
    cell_px_raw = _resolve_var(it.get("cell_px_w"), tvars, None)
    if cell_px_raw not in (None, ""):
        try:
            cell_w_fixed = max(1, int(float(cell_px_raw)))
        except (TypeError, ValueError):
            cell_w_fixed = None
    else:
        cell_w_fixed = None
    if cell_w_fixed is not None:
        # Bar width derived from uniform cell width.
        w = cell_w_fixed * tiers + max(0, tiers - 1)
    else:
        w = max(1, int(it.get("w", 60)))
    color_raw = _resolve_var(it.get("color"), tvars, (80, 220, 255))
    bg_raw    = _resolve_var(it.get("bg_color"), tvars, (40, 46, 70))
    sep_raw   = _resolve_var(it.get("sep_color"), tvars, (20, 26, 44))
    color = tuple(color_raw)[:3] if color_raw else (80, 220, 255)
    bg    = tuple(bg_raw)[:3]    if bg_raw    else (40, 46, 70)
    sep   = tuple(sep_raw)[:3]   if sep_raw   else (20, 26, 44)

    val_raw = _resolve_var(it.get("value", 0), tvars, 0)
    if isinstance(val_raw, str) and "{" in val_raw:
        try: val_raw = val_raw.format(**tvars)
        except (KeyError, IndexError, ValueError): val_raw = 0
    try: val = int(float(val_raw))
    except (TypeError, ValueError): val = 0

    mx_raw = _resolve_var(it.get("max", 20), tvars, 20)
    try: mx = max(1, int(float(mx_raw)))
    except (TypeError, ValueError): mx = 20

    subs = max(1, mx // tiers)
    if cell_w_fixed is not None:
        cell_w = cell_w_fixed
    else:
        cell_w = max(1, (w - (tiers - 1)) // tiers)

    for t in range(tiers):
        cx = x + t * (cell_w + 1)
        # Background
        pygame.draw.rect(surf, bg, (cx, y, cell_w, h))
        seg_min = t * subs
        seg_max = (t + 1) * subs
        if val >= seg_max:
            sub_filled = subs
        elif val > seg_min:
            sub_filled = val - seg_min
        else:
            sub_filled = 0
        if sub_filled > 0:
            fill_h = h * sub_filled // subs
            if sub_filled == subs:
                fill_h = h
            pygame.draw.rect(surf, color, (cx, y + h - fill_h, cell_w, fill_h))
        # Sub-level separators — only on tiers that aren't completely full.
        if sub_filled < subs:
            for s in range(1, subs):
                sep_y_px = y + h - (h * s // subs) - 1
                if y < sep_y_px < y + h:
                    pygame.draw.line(surf, sep,
                                     (cx, sep_y_px),
                                     (cx + cell_w - 1, sep_y_px))


def _layout_draw_progress_bar(surf, it, template_vars):
    """Segmented bar primitive (mirrors the hand-rolled _segbar used by the
    HUD). Fields:
      x, y, w, h         - bar rect
      value              - current value (number or "{name}" template)
      max                - max value (default 1.0; accepts "{name}")
      color              - filled-segment color (accepts "{name}")
      bg_color           - empty-segment color (default dark)
      segments           - segment count (default 10; accepts "{name}")
      alpha              - 0..255 (default 255)"""
    tvars = template_vars or {}
    x = int(it.get("x", 0))
    y = int(it.get("y", 0))
    w = max(1, int(it.get("w", 60)))
    h = max(1, int(it.get("h", 6)))
    segments = max(1, int(_resolve_var(it.get("segments", 10), tvars, 10)))
    color_raw = _resolve_var(it.get("color"), tvars, (80, 220, 255))
    bg_raw = _resolve_var(it.get("bg_color"), tvars, (40, 46, 70))
    color = tuple(color_raw)[:3] if color_raw else (80, 220, 255)
    bg = tuple(bg_raw)[:3] if bg_raw else (40, 46, 70)
    alpha = int(it.get("alpha", 255))
    val_raw = _resolve_var(it.get("value", 0), tvars, 0)
    if isinstance(val_raw, str) and "{" in val_raw:
        try:
            val_raw = val_raw.format(**tvars)
        except (KeyError, IndexError, ValueError):
            val_raw = 0
    try:
        val = float(val_raw)
    except (TypeError, ValueError):
        val = 0.0
    mx_raw = _resolve_var(it.get("max", 1.0), tvars, 1.0)
    try:
        mx = float(mx_raw) or 1.0
    except (TypeError, ValueError):
        mx = 1.0
    ratio = max(0.0, min(1.0, val / mx if mx > 0 else 0.0))

    cell_w = max(1, (w - (segments - 1)) // segments)
    target_surf = surf
    if alpha < 255:
        target_surf = pygame.Surface((w, h), pygame.SRCALPHA)
        cell_rect_x = 0
        cell_rect_y = 0
    else:
        cell_rect_x = x
        cell_rect_y = y
    for i in range(segments):
        cell = pygame.Rect(cell_rect_x + i * (cell_w + 1), cell_rect_y, cell_w, h)
        pygame.draw.rect(target_surf, bg, cell)
        if (i + 0.5) / segments <= ratio:
            pygame.draw.rect(target_surf, color, cell)
    if alpha < 255:
        target_surf.set_alpha(alpha)
        surf.blit(target_surf, (x, y))


# Panel-skin registry — each entry is the chrome defaults applied to a
# container whose `panel_skin` field matches the index. Explicit chrome
# fields on the container override the skin defaults. Skin 0 = no
# automatic chrome (the container still honours any explicit bg/border
# fields it carries). Skin 1 = the HUD panel look (recessed bg, border,
# corner caps, title chip). Add new entries here when more skins land.
_PANEL_SKIN_FIELDS = (
    "bg", "border", "border_width",
    "caps", "caps_color", "caps_length",
    "title_color", "title_font",
)
_PANEL_SKINS = {
    0: {},
    1: {
        "bg": [22, 26, 44],
        "border": [60, 80, 130],
        "border_width": 1,
        "caps": True,
        "caps_color": [110, 160, 220],
        "caps_length": 5,
        "title_color": [160, 200, 240],
        "title_font": 1,
    },
}


def _container_chrome(it):
    """Merge the panel skin's defaults with the container's explicit
    chrome fields. Explicit values win — set bg/border/etc. on the dict
    to customise without changing the skin. Returns a dict that the
    chrome-rendering code can read uniformly via .get()."""
    skin = int(it.get("panel_skin", 0))
    chrome = dict(_PANEL_SKINS.get(skin, {}))
    for k in _PANEL_SKIN_FIELDS:
        if k in it:
            chrome[k] = it[k]
    chrome["title"] = it.get("title")
    return chrome


def _layout_draw_container(surf, it, fonts, assets, template_vars, draw_one,
                            chrome_filter=None):
    """Render a container: optional bg + border + clipped recursive draw of
    children. Children sit at (container.x + child.x, container.y + child.y)
    when layout=free; layout=stack auto-positions them along an axis with
    gap. layout=grid uses anchor-aware cell placement.

    `draw_one` is the per-item dispatcher (passed in to keep the recursion
    cycle-free).

    `chrome_filter`: when given, only render this container's own chrome
    (bg/border/caps/title) when bool(it.get("dynamic")) == chrome_filter.
    Children are always recursed so a non-dynamic container can still
    expose dynamic children inside (e.g. HUD per-frame fields)."""
    x = int(it.get("x", 0))
    y = int(it.get("y", 0))
    w = max(0, int(it.get("w", 0)))
    h = max(0, int(it.get("h", 0)))
    pad = int(it.get("padding", 0))
    layout = (it.get("layout") or "free").lower()
    alpha = int(it.get("alpha", 255))
    # Chrome (bg / border / caps / title) is selected by panel_skin and
    # then overridden by any explicit field on the item dict.
    chrome = _container_chrome(it)
    bg = chrome.get("bg")
    border = chrome.get("border")
    border_w = int(chrome.get("border_width", 1)) if border else 0

    render_chrome = (chrome_filter is None
                     or bool(it.get("dynamic")) == chrome_filter)

    # Background + border (only when sized — w/h > 0).
    if render_chrome and w > 0 and h > 0:
        if bg is not None:
            col = (bg[0], bg[1], bg[2], alpha) if alpha < 255 else bg[:3]
            if alpha < 255:
                bg_surf = pygame.Surface((w, h), pygame.SRCALPHA)
                bg_surf.fill(col)
                surf.blit(bg_surf, (x, y))
            else:
                pygame.draw.rect(surf, col, (x, y, w, h))
        if border is not None and border_w > 0:
            pygame.draw.rect(surf, border[:3], (x, y, w, h), border_w)
        # Decorative corner caps + title chip (matches the HUD _panel look).
        if chrome.get("caps") and bg is not None and border is not None:
            cap = tuple(chrome.get("caps_color") or (110, 160, 220))[:3]
            cap_len = int(chrome.get("caps_length", 5))
            # 4 horizontal segments (top + bottom edges, both sides)
            pygame.draw.rect(surf, cap, (x, y, cap_len, 1))
            pygame.draw.rect(surf, cap, (x + w - cap_len, y, cap_len, 1))
            pygame.draw.rect(surf, cap, (x, y + h - 1, cap_len, 1))
            pygame.draw.rect(surf, cap, (x + w - cap_len, y + h - 1, cap_len, 1))
            # 4 vertical segments
            pygame.draw.rect(surf, cap, (x, y, 1, cap_len))
            pygame.draw.rect(surf, cap, (x + w - 1, y, 1, cap_len))
            pygame.draw.rect(surf, cap, (x, y + h - cap_len, 1, cap_len))
            pygame.draw.rect(surf, cap, (x + w - 1, y + h - cap_len, 1, cap_len))
        title = chrome.get("title")
        if title and fonts:
            if "{" in title and template_vars:
                title = _safe_format(title, template_vars)
            t_color = tuple(chrome.get("title_color") or (160, 200, 240))[:3]
            t_font_scale = max(1, min(7, int(chrome.get("title_font", 1))))
            t_font = fonts.get(t_font_scale) or fonts.get("tiny")
            t_img = t_font.render(title, False, t_color)
            # Clip the panel border behind the chip so text reads cleanly.
            if bg is not None:
                pygame.draw.rect(surf, bg[:3],
                                 (x + 6, y - 1, t_img.get_width() + 6, 2))
            surf.blit(t_img, (x + 9, y - 6))

    children = it.get("children") or ()
    if not children:
        return

    inner_x = x + pad
    inner_y = y + pad
    if layout == "stack":
        direction = (it.get("direction") or "vertical").lower()
        gap = int(it.get("gap", 0))
        cursor_x = inner_x
        cursor_y = inner_y
        for child in children:
            # Stack uses each child's w/h (with sensible defaults). The
            # child's own x/y is ignored — the stack positions it.
            child = dict(child)
            cw = int(child.get("w", w - pad * 2 if w else 0) or 0)
            ch = int(child.get("h", h - pad * 2 if h else 0) or 0)
            child["x"] = cursor_x
            child["y"] = cursor_y
            draw_one(surf, child, fonts, assets, template_vars)
            if direction == "horizontal":
                cursor_x += cw + gap
            else:
                cursor_y += ch + gap
        return

    if layout == "grid":
        rows = max(1, int(it.get("rows", 1)))
        cols = max(1, int(it.get("cols", 1)))
        gap_x = int(it.get("gap_x", it.get("gap", 0)))
        gap_y = int(it.get("gap_y", it.get("gap", 0)))
        inner_w = max(0, w - pad * 2)
        inner_h = max(0, h - pad * 2)
        cell_w = (inner_w - (cols - 1) * gap_x) // cols if cols else inner_w
        cell_h = (inner_h - (rows - 1) * gap_y) // rows if rows else inner_h
        cell_w = max(1, cell_w)
        cell_h = max(1, cell_h)
        max_cells = rows * cols
        for idx, child in enumerate(children):
            if idx >= max_cells:
                break  # extra children silently skipped — UI feedback in editor
            r = idx // cols
            c = idx % cols
            cell_x = inner_x + c * (cell_w + gap_x)
            cell_y = inner_y + r * (cell_h + gap_y)
            cell_child = dict(child)
            # The child's own anchor selects which point inside the cell it
            # latches to (anchor=tl → top-left, c → center, br → bottom-right).
            # Child's x/y stay as fine-tune offsets on top of that anchor point.
            anchor = child.get("anchor", "tl")
            ax = _LAYOUT_ANCHOR_AX.get(anchor, 0.0)
            ay = _LAYOUT_ANCHOR_AY.get(anchor, 0.0)
            cell_child["x"] = cell_x + int(cell_w * ax) + int(child.get("x", 0))
            cell_child["y"] = cell_y + int(cell_h * ay) + int(child.get("y", 0))
            # Default the child's size to fill its cell so progress bars /
            # rects / nested containers slot in cleanly without manual sizing.
            if "w" not in child: cell_child["w"] = cell_w
            if "h" not in child: cell_child["h"] = cell_h
            draw_one(surf, cell_child, fonts, assets, template_vars)
        return

    # Default: free positioning. Children's x/y are relative to inner_x/y.
    for child in children:
        offset_child = dict(child)
        offset_child["x"] = inner_x + int(child.get("x", 0))
        offset_child["y"] = inner_y + int(child.get("y", 0))
        draw_one(surf, offset_child, fonts, assets, template_vars)


# ---------------------------------------------------------------------------
# Built-in (chrome) element registry
# ---------------------------------------------------------------------------
# Each entry is the *default* spec for one chrome element on a screen. The
# screen's draw code calls get_element(screen, id) to fetch the merged
# (defaults + layout.json override) values and renders accordingly. This
# keeps the position/font/color/text of every label editable through the
# layout editor without changing how the engine actually composes the
# screen (menus still respond to game state, blinks still blink, etc.).

LAYOUT_ELEMENTS = {
    "title": [
        {"id": "logo", "type": "image",
         "x": 320, "y": 130, "anchor": "c",
         "sprite": "title", "scale": 1.0, "alpha": 255,
         "_label": "PEWPEW logo (with gloss sweep)"},
        {"id": "menu", "type": "menu",
         "x": 320, "y": 260, "align": "center",
         "font": 3, "color": [240, 240, 240],
         "selected_color": [255, 220, 80],
         "selected_decor": ">  {opt}  <",
         "unselected_decor": "   {opt}   ",
         "line_height": 44, "alpha": 255,
         "_label": "main menu list (Continue / New Game / Quit)",
         "_preview_options": ["Continue", "New Game", "Quit"]},
        {"id": "tip", "type": "text",
         "x": 320, "y": 420, "anchor": "c",
         "text": "{btn_fire} confirm  |  {dpad} select",
         "font": 2, "color": [140, 140, 160], "alpha": 255,
         "shadow": False, "blink": True,
         "_label": "controls hint (blinks; {dpad} = D-pad icon)",
         "_preview_vars": {"btn_fire": "A"}},
        {"id": "profile", "type": "text",
         "x": 320, "y": 392, "anchor": "c",
         "text": "< L1   {profile_name}   R1 >",
         "font": 2, "color": [160, 200, 240], "alpha": 255,
         "_label": "active player profile (L1/R1 cycle through 5 slots)",
         "_preview_vars": {"profile_name": "ANDROMEDA"}},
    ],
    "map": [
        {"id": "nav_hint_l", "type": "text",
         "x": 12, "y": 8, "anchor": "tl",
         "text": "< L1", "font": 2,
         "color": [255, 196, 64], "alpha": 255,
         "visible_when": "has_prev",
         "_label": "previous-sector hint (visible when sector > 0)"},
        {"id": "nav_hint_r", "type": "text",
         "x": 468, "y": 8, "anchor": "tr",
         "text": "R1 >", "font": 2,
         "color": [255, 196, 64], "alpha": 255,
         "visible_when": "has_next",
         "_label": "next-sector hint (visible when more sectors unlocked)"},
        {"id": "sector_title", "type": "text",
         "x": 240, "y": 50, "anchor": "c",
         "text": "{sector_name}", "font": 3,
         "color": [255, 196, 64], "alpha": 255,
         "_label": "current sector name banner"},
        {"id": "sector_subtitle", "type": "text",
         "x": 240, "y": 72, "anchor": "c",
         "text": "SECTOR {sector_n}/10  -  CLEARED {progress}/100",
         "font": 1, "color": [140, 140, 160], "alpha": 255,
         "_label": "sector progress sub-banner"},
        {"id": "all_clear", "type": "text",
         "x": 240, "y": 470, "anchor": "c",
         "text": "ALL CLEAR", "font": 2,
         "color": [90, 230, 120], "alpha": 255,
         "visible_when": "all_clear",
         "_label": "100% completion banner"},
        {"id": "back_hint", "type": "text",
         "x": 240, "y": 32, "anchor": "c",
         "text": "ST: title", "font": 1,
         "color": [140, 140, 160], "alpha": 255,
         "_label": "press START to return to the title screen"},
    ],
    "shop": [
        {"id": "hangar_title", "type": "text",
         "x": 20, "y": 18, "anchor": "tl",
         "text": "HANGAR", "font": 3,
         "color": [80, 220, 255], "alpha": 255,
         "_label": "left panel header"},
        {"id": "detail_next_label", "type": "text",
         "x": 260, "y": 394, "anchor": "tl",
         "text": "NEXT", "font": 1,
         "color": [255, 140, 40], "alpha": 255,
         "_label": "upgrade-detail strip NEXT label"},
        {"id": "back_hint", "type": "text",
         "x": 620, "y": 18, "anchor": "tr",
         "text": "ST: title", "font": 1,
         "color": [140, 140, 160], "alpha": 255,
         "_label": "press START to return to the title screen"},
    ],
    "gameover": [
        {"id": "title", "type": "text",
         "x": 320, "y": 180, "anchor": "c",
         "text": "SHIP LOST", "font": 5,
         "color": [255, 70, 70], "alpha": 255, "shadow": False,
         "_label": "headline"},
        {"id": "score", "type": "text",
         "x": 320, "y": 240, "anchor": "c",
         "text": "Score: {score}", "font": 2,
         "color": [240, 240, 240], "alpha": 255, "shadow": False,
         "_label": "this run's score ({score} is interpolated)"},
        {"id": "best", "type": "text",
         "x": 320, "y": 268, "anchor": "c",
         "text": "Best: {best}", "font": 1,
         "color": [140, 140, 160], "alpha": 255, "shadow": False,
         "_label": "best-ever score ({best} is interpolated)"},
        {"id": "tip", "type": "text",
         "x": 320, "y": 320, "anchor": "c",
         "text": "{btn_fire} return to map", "font": 1,
         "color": [140, 140, 160], "alpha": 255, "shadow": False,
         "blink": True,
         "_label": "return hint (blinks)",
         "_preview_vars": {"btn_fire": "A"}},
    ],
    # HUD: built programmatically because the tree is large and references
    # screen-geometry constants. The result is a single `hud_root`
    # container with six chrome panels + dynamic items (timer / score /
    # credits / shield bar / ability cd / ability-ready highlight).
    "hud": _build_hud_layout_spec(),
}

# Shop and Map share the right-strip layout idea with the HUD — same
# panel_skin chrome, dynamic credits/etc. Built the same way and appended
# to the existing per-screen item lists so the strip renders alongside
# whatever standalone items already live on those screens.
LAYOUT_ELEMENTS["shop"].append(_build_shop_panel_spec())
LAYOUT_ELEMENTS["map"].append(_build_map_panel_spec())
LAYOUT_ELEMENTS["play"] = [_build_play_banner_spec()]

# Override flag: when True, get_element returns None for every lookup so
# the screen draw skips its built-in chrome. _smoke uses this to capture
# "naked" backdrops for the layout editor preview (chrome-free reference
# image so the editor can re-render elements at edited positions without
# visual ghosting of the originals).
_RENDER_NAKED = False


def _layout_overrides_for(screen_name):
    """Map id -> override item from layout.json for this screen."""
    out = {}
    for it in _layout_load().get(screen_name) or ():
        rid = it.get("id")
        if rid:
            out[rid] = it
    return out


def get_element(screen_name, element_id, **template_vars):
    """Return the merged spec (defaults + layout.json override) for one
    built-in chrome element, or None if naked-render mode is active.
    Interpolates `{name}` placeholders in the text field using template_vars."""
    if _RENDER_NAKED:
        return None
    spec = None
    for el in LAYOUT_ELEMENTS.get(screen_name, ()):
        if el.get("id") == element_id:
            spec = dict(el)
            break
    if spec is None:
        return None
    overrides_list = _layout_load().get(screen_name) or []
    # If this builtin's override has been carried into a child container
    # (any depth), the engine draws it via the container walk — skip the
    # inline spec render so the moved copy isn't paired with a duplicate.
    if element_id in _layout_collect_deep_ids(overrides_list):
        return None
    override = _layout_overrides_for(screen_name).get(element_id)
    if override:
        # Allow override of any field except id/type — type is fixed by
        # the registry (a text element can't suddenly become a rect).
        for k, v in override.items():
            if k in ("id", "type"):
                continue
            spec[k] = v
    text = spec.get("text")
    if text and template_vars:
        spec["text"] = _safe_format(str(text), template_vars)
    return spec


def _is_builtin_id(screen_name, item_id):
    if not item_id:
        return False
    for el in LAYOUT_ELEMENTS.get(screen_name, ()):
        if el.get("id") == item_id:
            return True
    return False


def _draw_text_with_dpad(surf, it, fonts):
    """Render a text element with optional {dpad} placeholder replaced by
    an inline D-pad cross icon. Falls back to plain text rendering when
    the placeholder is absent."""
    text = str(it.get("text") or "")
    if "{dpad}" not in text:
        _layout_draw_text(surf, it, fonts)
        return
    left_txt, right_txt = text.split("{dpad}", 1)
    # Icon scale is the font's logical scale — same as before for the
    # 5x7 family. For the 7x9 family pick the nearest 5x7 scale by
    # comparing line heights so the cross sits inline with the text.
    fam = (it.get("font_family") or "").strip()
    raw_scale = int(it.get("font", 3))
    if fam == "7x9":
        raw_scale = max(1, min(4, raw_scale))
        icon_scale = max(1, raw_scale + (raw_scale // 2))   # ~1 -> 1, 2 -> 3
    else:
        raw_scale = max(1, min(7, raw_scale))
        icon_scale = raw_scale
    font = _resolve_layout_font(fonts, it)
    color = tuple(it.get("color") or (240, 240, 240))[:3]
    alpha = int(it.get("alpha", 255))
    left = font.render(left_txt, False, color)
    right = font.render(right_txt, False, color)
    icon_w = 7 * icon_scale
    icon_h = 7 * icon_scale
    total_w = left.get_width() + icon_w + right.get_width()
    h = max(left.get_height(), icon_h, right.get_height())
    if alpha < 255:
        left = left.copy(); left.set_alpha(alpha)
        right = right.copy(); right.set_alpha(alpha)
    ox, oy = _layout_anchor_offset(it.get("anchor", "tl"), total_w, h)
    base_x = int(it.get("x", 0)) + ox
    base_y = int(it.get("y", 0)) + oy
    text_top = base_y + (h - left.get_height()) // 2
    icon_top = base_y + (h - icon_h) // 2
    surf.blit(left, (base_x, text_top))
    icon_x = base_x + left.get_width()
    _draw_dpad_icon(surf, icon_x, icon_top, scale=icon_scale, color=color)
    surf.blit(right, (icon_x + icon_w, text_top))


def _layout_draw_menu(surf, it, fonts, options=None):
    """Render a menu element: option list stacked vertically. The first
    option's vertical centre sits at (x, y); each subsequent option steps
    down by line_height. `align` controls each line's horizontal anchor:
      "center" — line centered on x (default)
      "left"   — line's left edge at x
      "right"  — line's right edge at x
    Selected option uses selected_color + selected_decor."""
    opts = options if options is not None else (it.get("_preview_options") or [])
    if not opts:
        return
    font = _resolve_layout_font(fonts, it)
    color = tuple(it.get("color") or (240, 240, 240))[:3]
    sel_color = tuple(it.get("selected_color") or color)[:3]
    sel_decor = it.get("selected_decor") or ">  {opt}  <"
    unsel_decor = it.get("unselected_decor") or "   {opt}   "
    line_h = int(it.get("line_height", 44))
    alpha = int(it.get("alpha", 255))
    align = (it.get("align") or "center").lower()
    cursor = it.get("_preview_cursor", 0)
    cx = int(it.get("x", 0))
    y = int(it.get("y", 0))
    for i, opt in enumerate(opts):
        sel = (i == cursor)
        text = (sel_decor if sel else unsel_decor).replace("{opt}", str(opt))
        c = sel_color if sel else color
        img = font.render(text, False, c)
        if alpha < 255:
            img = img.copy(); img.set_alpha(alpha)
        line_y = y + i * line_h
        if align == "left":
            rect = img.get_rect(midleft=(cx, line_y))
        elif align == "right":
            rect = img.get_rect(midright=(cx, line_y))
        else:
            rect = img.get_rect(center=(cx, line_y))
        surf.blit(img, rect)


def _layout_draw_item(surf, it, fonts, assets, template_vars, dynamic_filter=None):
    """Per-item dispatcher used by both the top-level overlay path and
    container recursion. template_vars is the {name: value} dict for
    {placeholder} interpolation in text + progress_bar.value.

    dynamic_filter (optional):
      None  → render everything (default; editor + overlay use this)
      False → render only items NOT marked `dynamic`: True (chrome bake)
      True  → render only items marked `dynamic`: True (per-frame overlay)
    Containers are always recursed into — only their children are filtered."""
    kind = it.get("type")
    if dynamic_filter is not None and kind != "container":
        if bool(it.get("dynamic")) != dynamic_filter:
            return
    # Conditional rendering: `visible_when` names a key in template_vars
    # whose truthiness gates the draw. Lets us swap the dim/bright ability-
    # name overlays without a full conditional expression language.
    vw = it.get("visible_when")
    if vw and template_vars is not None:
        if not template_vars.get(vw):
            return
    try:
        if kind == "text":
            txt = str(it.get("text") or "")
            if "{" in txt and template_vars:
                copy = dict(it)
                copy["text"] = _safe_format(txt, template_vars)
                it = copy
            _draw_text_with_dpad(surf, it, fonts)
        elif kind == "rect":
            _layout_draw_rect(surf, it)
        elif kind == "image":
            _layout_draw_image(surf, it, assets)
        elif kind == "menu":
            _layout_draw_menu(surf, it, fonts)
        elif kind == "progress_bar":
            _layout_draw_progress_bar(surf, it, template_vars)
        elif kind == "tiered_bar":
            _layout_draw_tiered_bar(surf, it, template_vars)
        elif kind == "container":
            _layout_draw_container(
                surf, it, fonts, assets, template_vars,
                lambda *a: _layout_draw_item(*a, dynamic_filter=dynamic_filter),
                chrome_filter=dynamic_filter)
    except Exception as e:
        print(f"layout draw {kind} failed: {e}")


def draw_layout_overlay(surf, screen_name, fonts, assets=None,
                        template_vars=None):
    """Render user-editable overlay items for the named screen. Skips
    items whose id matches a built-in element — those are rendered inline
    by the screen draw via get_element(). User-added items + previews of
    built-ins (used by the editor) flow through here.

    template_vars (optional) is the dict used to interpolate {name}
    placeholders in text + progress_bar values."""
    items = _layout_load().get(screen_name)
    if not items:
        return
    tvars = template_vars or {}
    for it in items:
        if _is_builtin_id(screen_name, it.get("id")):
            continue
        _layout_draw_item(surf, it, fonts, assets, tvars)


class _DynRecord:
    """Pre-resolved per-frame draw plan for one dynamic layout item.

    Built once at flatten time. Per-frame draw uses the attributes
    directly — no dict.get with defaults, no font lookup branching, no
    color tuple conversion, no anchor key string ops. Anchor offset for
    text is recomputed per frame because the rendered text size depends
    on the live template-formatted content. The parent container id is
    carried through so any per-frame container offset registered in
    `_HudCache.container_offsets` is applied — enabling whole-panel
    animations (shake, slide, etc.) without invalidating the cache.

    `fallback_spec` lets us route exotic items we don't have a fast path
    for (image, menu, alpha < 255 progress bars, etc.) through the
    original `_layout_draw_item` dispatcher with the resolved (x, y)."""
    __slots__ = ("kind", "container_id", "x", "y", "visible_when",
                 # Text fields
                 "font", "color", "alpha", "anchor", "shadow",
                 "text_template", "text_has_braces", "text_has_dpad",
                 # Progress-bar fields
                 "w", "h", "value_raw", "max_raw", "segments_raw",
                 "color_raw", "bg_color_raw",
                 # Rect fields (reuse w, h, color, alpha + outline)
                 "outline",
                 # Fallback spec for kinds we don't fast-path
                 "fallback_spec")

    def __init__(self):
        # All fields default to None; the resolver fills in what's needed
        # per kind. Slots can't take per-attr defaults so we do it here.
        for slot in self.__slots__:
            setattr(self, slot, None)


def _resolve_dynamic_item(it, abs_x, abs_y, container_id, fonts):
    """Build a _DynRecord for a leaf item. `abs_x`/`abs_y` are the item's
    final on-surface position (after parent container offsets), and
    `container_id` is the immediate parent's id (or None) so a runtime
    container_offsets entry can shift the draw at frame time."""
    r = _DynRecord()
    r.x = abs_x
    r.y = abs_y
    r.container_id = container_id
    r.visible_when = it.get("visible_when")
    kind = it.get("type")
    if kind == "text":
        text = str(it.get("text") or "")
        r.kind = "text"
        r.font = _resolve_layout_font(fonts, it, default_scale=3)
        col = it.get("color")
        r.color = tuple(col)[:3] if col else (240, 240, 240)
        r.alpha = int(it.get("alpha", 255))
        r.anchor = it.get("anchor", "tl")
        r.shadow = bool(it.get("shadow"))
        r.text_template = text
        r.text_has_braces = "{" in text
        r.text_has_dpad = "{dpad}" in text
    elif kind == "progress_bar":
        r.kind = "progress_bar"
        r.w = max(1, int(it.get("w", 60)))
        r.h = max(1, int(it.get("h", 6)))
        r.alpha = int(it.get("alpha", 255))
        r.value_raw = it.get("value", 0)
        r.max_raw = it.get("max", 1.0)
        r.segments_raw = it.get("segments", 10)
        r.color_raw = it.get("color")
        r.bg_color_raw = it.get("bg_color")
    elif kind == "rect":
        r.kind = "rect"
        r.w = max(0, int(it.get("w", 0)))
        r.h = max(0, int(it.get("h", 0)))
        col = it.get("color")
        r.color = tuple(col)[:3] if col else (60, 80, 120)
        r.alpha = int(it.get("alpha", 200))
        r.outline = int(it.get("outline", 0))
    else:
        # Image / menu / container / future kinds — keep the original
        # spec dict with absolute coords so the slow path can render it.
        r.kind = "fallback"
        fallback = dict(it)
        fallback["x"] = abs_x
        fallback["y"] = abs_y
        r.fallback_spec = fallback
    return r


def _flatten_dynamic_items(items, fonts, ox=0, oy=0, parent_id=None,
                           out=None):
    """Walk a layout subtree once and collect every item flagged
    `dynamic: True` into a flat list of pre-resolved _DynRecord
    instances. Container offsets are baked into the absolute (x, y) and
    each record remembers its parent container id so runtime animation
    can shift just that container's children. Only `free`-layout
    containers are fully flattened; `stack` / `grid` containers fall
    back to the per-frame walker. The HUD spec uses only `free`."""
    if out is None:
        out = []
    for it in items:
        kind = it.get("type")
        if kind == "container":
            layout = (it.get("layout") or "free").lower()
            if layout != "free":
                # Signal to caller: render this subtree via the walker.
                sentinel = _DynRecord()
                sentinel.kind = "needs_walker"
                out.append(sentinel)
                continue
            pad = int(it.get("padding", 0))
            inner_x = int(it.get("x", 0)) + ox + pad
            inner_y = int(it.get("y", 0)) + oy + pad
            _flatten_dynamic_items(it.get("children") or (), fonts,
                                   inner_x, inner_y,
                                   it.get("id") or parent_id, out)
        elif it.get("dynamic"):
            abs_x = int(it.get("x", 0)) + ox
            abs_y = int(it.get("y", 0)) + oy
            out.append(_resolve_dynamic_item(it, abs_x, abs_y, parent_id,
                                             fonts))
    return out


def _format_template(template, has_braces, tvars):
    """Cheap shortcut for the common "no placeholders" case so we skip
    str.format on static-text dynamic items entirely."""
    if not has_braces:
        return template
    return _safe_format(template, tvars)


def _fast_draw_text_record(surf, rec, tvars, ox, oy):
    text = _format_template(rec.text_template, rec.text_has_braces, tvars)
    if not text:
        return
    if rec.text_has_dpad and "{dpad}" in text:
        # Cold path: D-pad icon inline. The full helper handles it.
        spec = {
            "x": rec.x + ox, "y": rec.y + oy, "anchor": rec.anchor,
            "text": text, "font": 1, "color": list(rec.color),
            "alpha": rec.alpha,
        }
        _draw_text_with_dpad(surf, spec, {"tiny": rec.font, 1: rec.font})
        return
    # Fast path: opaque + no shadow → glyph-by-glyph direct draw via
    # BitmapFont.draw(). Skips the per-string SRCALPHA buffer alloc +
    # the buffer-to-dst blit that render() pays on cache miss. We still
    # pre-measure via size() when the anchor needs the text width, so
    # centered / right-aligned dynamic text stays correctly positioned
    # as the rendered string length changes.
    if rec.alpha >= 255 and not rec.shadow:
        anchor = rec.anchor
        if anchor == "tl":
            ax = ay = 0
        else:
            w, h = rec.font.size(text)
            ax, ay = _layout_anchor_offset(anchor, w, h)
        rec.font.draw(surf, rec.x + ox + ax, rec.y + oy + ay, text,
                      rec.color)
        return
    # Cold path: alpha < 255 or shadow needed → render() buffer so we can
    # use per-surface set_alpha. Centered text still recomputes anchor
    # from the rendered image size.
    img = rec.font.render(text, False, rec.color)
    anchor = rec.anchor
    if anchor == "tl":
        ax = ay = 0
    else:
        ax, ay = _layout_anchor_offset(anchor,
                                       img.get_width(), img.get_height())
    px = rec.x + ox + ax
    py = rec.y + oy + ay
    if rec.shadow:
        sh = rec.font.render(text, False, (0, 0, 0))
        sh.set_alpha(min(rec.alpha, 180))
        surf.blit(sh, (px + 1, py + 1))
    if rec.alpha < 255:
        img = img.copy()
        img.set_alpha(rec.alpha)
    surf.blit(img, (px, py))


def _fast_draw_progress_bar_record(surf, rec, tvars, ox, oy):
    # Resolve color / value / max / segments — most are plain numbers but
    # the spec also allows "{name}" templates that evaluate per frame.
    val_raw = _resolve_var(rec.value_raw, tvars, 0)
    if isinstance(val_raw, str) and "{" in val_raw:
        try:
            val_raw = val_raw.format(**tvars)
        except (KeyError, IndexError, ValueError):
            val_raw = 0
    try:
        val = float(val_raw)
    except (TypeError, ValueError):
        val = 0.0
    mx_raw = _resolve_var(rec.max_raw, tvars, 1.0)
    try:
        mx = float(mx_raw) or 1.0
    except (TypeError, ValueError):
        mx = 1.0
    ratio = max(0.0, min(1.0, val / mx if mx > 0 else 0.0))
    segments = max(1, int(_resolve_var(rec.segments_raw, tvars, 10)))
    color_raw = _resolve_var(rec.color_raw, tvars, (80, 220, 255))
    bg_raw = _resolve_var(rec.bg_color_raw, tvars, (40, 46, 70))
    color = tuple(color_raw)[:3] if color_raw else (80, 220, 255)
    bg = tuple(bg_raw)[:3] if bg_raw else (40, 46, 70)
    w = rec.w
    h = rec.h
    cell_w = max(1, (w - (segments - 1)) // segments)
    x = rec.x + ox
    y = rec.y + oy
    if rec.alpha >= 255:
        for i in range(segments):
            cell = pygame.Rect(x + i * (cell_w + 1), y, cell_w, h)
            pygame.draw.rect(surf, bg, cell)
            if (i + 0.5) / segments <= ratio:
                pygame.draw.rect(surf, color, cell)
    else:
        target = pygame.Surface((w, h), pygame.SRCALPHA)
        for i in range(segments):
            cell = pygame.Rect(i * (cell_w + 1), 0, cell_w, h)
            pygame.draw.rect(target, bg, cell)
            if (i + 0.5) / segments <= ratio:
                pygame.draw.rect(target, color, cell)
        target.set_alpha(rec.alpha)
        surf.blit(target, (x, y))


def _fast_draw_rect_record(surf, rec, tvars, ox, oy):
    x = rec.x + ox
    y = rec.y + oy
    if rec.alpha >= 255:
        if rec.outline > 0:
            pygame.draw.rect(surf, rec.color, (x, y, rec.w, rec.h),
                             rec.outline)
        else:
            pygame.draw.rect(surf, rec.color, (x, y, rec.w, rec.h))
    else:
        s = pygame.Surface((rec.w, rec.h), pygame.SRCALPHA)
        col = (rec.color[0], rec.color[1], rec.color[2], rec.alpha)
        if rec.outline > 0:
            pygame.draw.rect(s, col, (0, 0, rec.w, rec.h), rec.outline)
        else:
            s.fill(col)
        surf.blit(s, (x, y))


def _draw_main_swap_hints(surf, fonts, assets, player):
    """Bottom-corner labels showing the left/right shoulder hold-to-swap
    binds plus a glyph of the projectile they fire. Painted on top of the
    HUD so the currently-active main is highlighted in real time. Labels
    are just "L" / "R" because either physical shoulder (L1 OR L2 / R1
    OR R2) triggers the swap-and-fire shortcut."""
    lo = player.loadout
    active = lo.main_type
    glyphs = getattr(Bullet, "_glyphs", {}) or {}
    font = fonts.get(1) or fonts.get("tiny")
    # Left corner: L hint (Pulse). Right corner: R hint (Spread).
    # Symmetric layout — text outside, glyph inside.
    bottom_y = PLAY_H - 4
    pad = 4
    for slot, kind, label in (("left", "pulse", "L"),
                              ("right", "spread", "R")):
        color = MAIN_BULLET_STYLE[kind]["color"]
        is_active = (active == kind)
        text_color = color if is_active else (110, 120, 140)
        glyph = glyphs.get(f"glyph_{kind}")
        # Render text first to measure.
        text_surf = font.render(label, False, text_color)
        tw = text_surf.get_width()
        th = text_surf.get_height()
        gw = glyph.get_width() if glyph is not None else 6
        gh = glyph.get_height() if glyph is not None else 8
        block_w = tw + 3 + gw
        if slot == "left":
            bx = pad
            text_x = bx
            glyph_x = bx + tw + 3
        else:
            bx = PLAY_W - pad - block_w
            text_x = bx
            glyph_x = bx + tw + 3
        ty = bottom_y - th
        gy = bottom_y - gh
        # Small backing plate so the labels stay legible over busy art.
        bg = pygame.Surface((block_w + 4, max(th, gh) + 2), pygame.SRCALPHA)
        bg.fill((10, 14, 22, 170))
        surf.blit(bg, (bx - 2, bottom_y - max(th, gh) - 1))
        surf.blit(text_surf, (text_x, ty))
        if glyph is not None:
            if is_active:
                surf.blit(glyph, (glyph_x, gy))
            else:
                # Dim the inactive glyph by drawing through a black overlay.
                dim = glyph.copy()
                dim.fill((0, 0, 0, 140), special_flags=pygame.BLEND_RGBA_MULT)
                surf.blit(dim, (glyph_x, gy))
        else:
            pygame.draw.rect(surf, color, (glyph_x, gy, gw, gh))


def hud_draw(surf, fonts, assets, player, save, level_name, score, time_left):
    # 1) Cached chrome (rebuilt only on loadout / tier-unlock / mission
    # / layout change — unlock state is in the cache key now so newly
    # unlocked tiers grow the weapon bars next frame).
    key = _hud_cache_key(player, level_name, save)
    if key != _HudCache.key or _HudCache.surface is None:
        _HudCache.surface = _build_hud_chrome(fonts, level_name,
                                              player.loadout, save)
        _HudCache.key = key
    surf.blit(_HudCache.surface, (HUD_X, 0))

    # 2) Per-frame dynamic items via pre-resolved records. The records
    # cache invalidates only when the editor bumps _LAYOUT_REV — during
    # gameplay it's a constant single-list iteration with no dict.get
    # lookups and no font/color resolution per frame.
    #
    # Dynamic items draw DIRECTLY onto `surf` (the screen) at +HUD_X,
    # not into an intermediate SRCALPHA scratch surface. The chrome blit
    # above just repainted the HUD region, so any old dynamic pixels are
    # already cleared. Skipping dyn_surf saves a per-frame 160x480
    # SRCALPHA fill + alpha blit (~1 ms on the mali driver).
    chrome_vars = _hud_chrome_vars(level_name, player.loadout, save)
    tvars = {**chrome_vars, **_hud_dyn_vars(player, save, score, time_left)}
    if (_HudCache.dyn_records is None
            or _HudCache.dyn_records_rev != _LAYOUT_REV):
        _HudCache.dyn_records = _flatten_dynamic_items(
            resolved_layout_tree("hud"), fonts)
        _HudCache.dyn_records_rev = _LAYOUT_REV
    records = _HudCache.dyn_records
    # If a stack/grid container was encountered the first record will be
    # a "needs_walker" sentinel; bail to the full tree walk for safety.
    if records and records[0].kind == "needs_walker":
        # Walker path still needs a scratch surface — it expects HUD-
        # local coords. Pre-allocate once and reuse.
        dyn_surf = _HudCache.dyn_surf
        if dyn_surf is None:
            dyn_surf = pygame.Surface((HUD_W, SCREEN_H), pygame.SRCALPHA)
            _HudCache.dyn_surf = dyn_surf
        dyn_surf.fill((0, 0, 0, 0))
        for it in resolved_layout_tree("hud"):
            _layout_draw_item(dyn_surf, it, fonts, assets, tvars,
                              dynamic_filter=True)
        surf.blit(dyn_surf, (HUD_X, 0))
    else:
        offsets = _HudCache.container_offsets
        # Bake HUD_X into the per-record offset so coords map straight
        # to the screen surface.
        for rec in records:
            if rec.visible_when and not tvars.get(rec.visible_when):
                continue
            cox, coy = (offsets.get(rec.container_id, (0, 0))
                        if offsets else (0, 0))
            ox = cox + HUD_X
            oy = coy
            kind = rec.kind
            if kind == "text":
                _fast_draw_text_record(surf, rec, tvars, ox, oy)
            elif kind == "progress_bar":
                _fast_draw_progress_bar_record(surf, rec, tvars, ox, oy)
            elif kind == "rect":
                _fast_draw_rect_record(surf, rec, tvars, ox, oy)
            elif kind == "fallback":
                # Fallback spec carries HUD-local x/y; translate to
                # screen coords for direct draw.
                spec = dict(rec.fallback_spec)
                spec["x"] = int(spec.get("x", 0)) + ox
                spec["y"] = int(spec.get("y", 0)) + oy
                _layout_draw_item(surf, spec, fonts, assets, tvars)

    # 3) User overlay items (any items in layout.json["hud"] that don't
    #    match a built-in id — same as for every other screen).
    draw_layout_overlay(surf, "hud", fonts, assets, template_vars=tvars)


# =============================================================================
# PLAY STATE
# =============================================================================

def _prepare_station_start(img):
    """Scale 2x and rotate 180° — the launch pad sits at the bottom with its
    docking arm pointing up, so the player visibly lifts out of it."""
    big = pygame.transform.scale2x(img)
    return pygame.transform.rotate(big, 180)


def _prepare_station_end(img):
    """Scale 2x — the destination station hangs at the top in normal
    orientation, docking arm pointing down toward the arriving ship."""
    return pygame.transform.scale2x(img)


class PlayState:
    def __init__(self, app, level):
        self.app = app
        self.level = level
        self.assets = app.assets
        self.player = Player(app.assets, app.save.loadout)
        self.bullets = []
        self.enemies = []
        self.pickups = []
        self.particles = []
        self.float_texts = []      # in-world floating numbers (e.g. "+$25")
        self.lasers = []
        self.score = 0
        self.elapsed = 0
        self.timeline_idx = 0
        self.stars = ParallaxStars(PLAY_W, PLAY_H)
        self.nebula = Nebula(level.nebula)
        self.bg_ribbon = BackgroundRibbon(level.theme,
                                          width=PLAY_W + 2 * PLAY_MARGIN)
        # Rebuild at the source's native aspect ratio, mirror-tiled 3x
        # horizontally — keeps the backdrop's true proportions instead of
        # stretching to the playfield width. Done before make_mirrored so
        # the vertical-flip seam works on the already-stretched layer.
        self.bg_ribbon.remake_native_aspect_h(mirror_n=3)
        # Mirror-tile so the wrap is seamless, then flip direction so the
        # backdrop drifts DOWN (counter to the player's forward motion).
        self.bg_ribbon.make_mirrored()
        self.bg_ribbon.speed = -abs(self.bg_ribbon.speed)
        self.vignette = app.vignette
        self.difficulty = level.difficulty
        # Adaptive per-level difficulty knob. Each death decrements; each
        # finish bumps +5 (capped at 0). Negative values bias the level
        # easier via _adjusted_spawn / _effective_shield_chance /
        # _biased_drop_kind. Bot sessions use a fresh SaveData so the
        # dict is empty -> adj = 0 baseline.
        adj_map = getattr(app.save, "level_difficulty_adjust", None) or {}
        # The stored value is a FLOAT (decrements scale with how far the
        # player got into the level before dying); the live runtime knob
        # is the truncated-toward-zero integer floor, so -0.5 -> 0 (no
        # nudge yet) and -1.0 -> -1 (first tier of help).
        self.difficulty_adjust = int(float(adj_map.get(level.key, 0.0)))
        # Per-wave modifier table: each entry is timeline_idx -> (downgrade
        # steps, count reduction). Count reductions are distributed worst-
        # wave-first (so -1 hits the single highest-HP wave, -2 hits the
        # two worst, etc.). Type downgrades follow as a second pass (1
        # per -5 units), also worst-first.
        self.wave_modifiers = _compute_wave_modifiers(self)
        self.flash = 0
        self.shake = 0
        # Lateral camera that lerps toward an offset proportional to the
        # player's horizontal distance from the centre of the playfield.
        # Applied to the playfield blit at draw time so bg_ribbon, stars and
        # every entity drift opposite to the player's input — giving the
        # impression of a slightly bigger play area panning under the ship.
        self.parallax_x = 0.0
        self.is_boss_fight = False
        self.boss_spawned = False
        self.outcome = None
        self.pause = False
        self.message = None
        self.boss_intro_t = 0.0   # seconds remaining of intro
        self.explosions = []      # list of ExplosionRing
        self.sparks = []          # impact sparks (Particle subclass)
        self.message_timer = 0
        self.credits_earned = 0
        self.scrap_drop_factor = 1.0
        # Dev/test cheat: L2+R2 instantly resolves the level (kills every
        # enemy with drops, collects every pickup) and shows a 3-second
        # summary in the middle of the screen.
        self._cheat_summary = None
        self._cheat_summary_t = 0.0
        self._cheat_l2r2_was_held = False
        # Cinematic level transitions: launch from a wide platform, dock at a station.
        n = int(level.key[1:]) if level.key.startswith("L") and level.key[1:].isdigit() else 1
        sec_here = (n - 1) // 10
        sec_next = min(9, n // 10)   # next sector index, capped for L100
        # Prefer the AI-generated station art if available (one per sector);
        # fall back to procedural shapes otherwise.
        stations = self.assets.get("_stations", {})
        pads = self.assets.get("_launch_pads", {})
        if sec_here in pads:
            self.station_start = pads[sec_here]
        else:
            self.station_start = make_launch_pad(sector_idx=sec_here)
        if sec_next in stations:
            self.station_end = stations[sec_next]
        else:
            self.station_end = make_station(seed=n * 71 + 137, sector_idx=sec_next)
        # Bases are rendered twice as big, with the launch pad rotated 180°
        # so its docking arm points up at the lifting ship.
        self.station_start = _prepare_station_start(self.station_start)
        self.station_end = _prepare_station_end(self.station_end)
        self.intro_t = 2.4
        self.outro_t = 0.0
        self._outro_start_y = float(self.player.y)

        # Pre-allocated per-frame surfaces. Allocating a 480x480 Surface every
        # frame for the playfield + a fresh overlay for each white/red/cyan
        # flash showed up in profiles as ~1 ms of pure malloc/free on device.
        # Reuse the same surfaces and just set_alpha on the overlays.
        # Playfield surface is wider than PLAY_W so bg_ribbon fills the
        # ±PLAY_MARGIN region that lateral parallax + shake reveal on each
        # side. Entities draw on a centred subsurface so their world-coord
        # logic doesn't need to know about the margin.
        full_w = PLAY_W + 2 * PLAY_MARGIN
        try:
            self._playfield_full = pygame.Surface((full_w, PLAY_H)).convert()
        except pygame.error:
            self._playfield_full = pygame.Surface((full_w, PLAY_H))
        self._playfield = self._playfield_full.subsurface(
            (PLAY_MARGIN, 0, PLAY_W, PLAY_H))
        def _solid(color, w=full_w):
            try:
                s = pygame.Surface((w, PLAY_H)).convert()
            except pygame.error:
                s = pygame.Surface((w, PLAY_H))
            s.fill(color)
            return s
        # Bomb / flash overlays cover the FULL playfield surface so a
        # screen-wide flash still fills the margins exposed by parallax.
        self._bomb_overlay = _solid(WHITE)
        self._flash_overlay_red = _solid(RED)
        self._flash_overlay_cyan = _solid(CYAN)
        # Overlay cycles via R3. 0 = debug banner (test-only info),
        # 1 = perf summary, 2 = perf detail, 3 = off. Starts off in every
        # play state — test mode overrides to start at debug below.
        self._test_overlay_mode = 3
        # Ship starts sitting in the launch bay of the platform.
        self.player.y = PLAY_H - 30
        self.player.rect.center = (int(self.player.x), int(self.player.y))
        self.player.cinematic = True
        self.player.cinematic_scale = 0.35  # small until takeoff scales it back up

        # ---- Hidden test-mission setup (SELECT+Y on the title) -------------
        self.is_test = getattr(level, "is_test", False)
        if self.is_test:
            # Start at the default loadout (in-memory only, save untouched);
            # the user upgrades from there via the test-mode controls.
            self.player.loadout = Loadout()
            self.player.shield_max = SHIELD_MAX[self.player.loadout.shield]
            self.player.shield_hp = self.player.shield_max
            # The normal launch cinematic is replaced by a parade that runs
            # a takeoff-then-land cinematic for every station.
            self.intro_t = 0
            self.player.cinematic = True
            self.player.cinematic_scale = 0.35
            self.player.y = PLAY_H - 30
            self.player.rect.center = (int(self.player.x), int(self.player.y))
            stations_raw = self.assets.get("_stations", {})
            self._test_stations_start = {
                idx: _prepare_station_start(img)
                for idx, img in stations_raw.items()
            }
            self._test_stations_end = {
                idx: _prepare_station_end(img)
                for idx, img in stations_raw.items()
            }
            self._test_parade_idx = 0
            self._test_parade_sub = "takeoff"   # or "landing"
            self._test_parade_sub_duration = 2.0
            self._test_parade_t = self._test_parade_sub_duration
            self._test_parade_done = False
            # Per-frame edge detection for triggers and right-stick directions.
            self._test_l2_held = False
            self._test_r2_held = False
            self._test_rstick_prev = (0, 0)   # quantized -1/0/+1 (qx, qy)
            self._test_action_msg = ""        # last manual action, shown in banner
            self._test_action_t = 0.0
            # Diagnostic snapshots: last button index pressed + live axes.
            # Lets the user see what indices their controller actually emits
            # if the default L2/R2/L3 mappings don't match.
            self._test_last_btn = None
            self._test_last_btn_t = 0
            self._test_diag_axes = ()
            self._test_diag_n_btn = 0
            # In test mode, start at the debug banner so users see the
            # loadout + control reminders immediately.
            self._test_overlay_mode = 0
            # Cached counts for the timeline split between waves and bosses.
            self._test_waves_end = 6           # timeline[0..5] = enemy waves
            self._test_bosses_end = 16         # timeline[6..15] = 10 bosses

    def run(self, events, controls):
        dt = 1.0 / FPS
        if controls.start_pressed:
            self.pause = not self.pause

        # R3 cycles the debug/perf overlay in every play state, not just the
        # test mission — the test mode just has an extra "debug" mode that
        # would show test-only info on a regular run.
        self._handle_overlay_toggle(events)

        if self.is_test:
            self._handle_test_inputs(events, controls)
            if self._test_action_t > 0:
                self._test_action_t = max(0.0, self._test_action_t - dt)

        if not self.pause:
            self._update(dt, controls)
        self._draw(controls)
        if self.outcome is not None:
            return self.outcome
        return None

    def _update(self, dt, controls):
        self.stars.update(dt)
        if ENABLE_NEBULA:
            self.nebula.update(dt)
        self.bg_ribbon.update(dt)
        self.boss_intro_t = max(0, self.boss_intro_t - dt)

        # SELECT + L2 + R2 cheat trigger (edge-detected so a held combo
        # fires once). SELECT is required so the player can use L2 / R2
        # as fire-and-swap shoulder shortcuts without nuking the level
        # by accident. Skips during the intro/outro and after the level
        # has already been decided so the player can't double-trigger
        # or break the cinematic.
        combo_held = (controls.select and controls.l2_held
                      and controls.r2_held)
        if (combo_held and not self._cheat_l2r2_was_held
                and self.outcome is None
                and self.outro_t <= 0
                and self.intro_t <= 0
                and self._cheat_summary_t <= 0):
            self._fire_instant_clear_cheat()
        self._cheat_l2r2_was_held = combo_held
        if self._cheat_summary_t > 0:
            self._cheat_summary_t = max(0.0, self._cheat_summary_t - dt)

        # Cinematic intro: ship lifts off from the launch platform as it scrolls away.
        if self.intro_t > 0:
            self.intro_t -= dt
            p = clamp(1.0 - max(0.0, self.intro_t) / 2.4, 0.0, 1.0)
            eased = 1.0 - (1.0 - p) ** 3
            # Most of the takeoff illusion comes from the platform scrolling
            # away underneath; the ship only nudges up a little but grows into
            # combat scale.
            self.player.y = lerp(PLAY_H - 30, PLAY_H - 60, eased)
            # Re-centre x in case a previous run nudged it.
            self.player.x = lerp(self.player.x, PLAY_W // 2, eased * 0.5)
            self.player.rect.center = (int(self.player.x), int(self.player.y))
            self.player.cinematic_scale = lerp(0.35, 1.0, eased)
            self.player.thrust += dt * 80
            self.player.tilt = 0.0
            self.stars.update(dt * 1.6)
            self.sparks = [s for s in self.sparks if s.alive]
            self.explosions = [ex for ex in self.explosions if ex.alive]
            if self.intro_t <= 0:
                self.player.cinematic = False
                self.player.cinematic_scale = 1.0
                self.player.invuln = 1.0
            return

        # Cinematic outro: ship climbs up to meet (and dock at) the arrival station.
        if self.outro_t > 0:
            self.outro_t -= dt
            p = clamp(1.0 - max(0.0, self.outro_t) / 2.4, 0.0, 1.0)
            eased = p * p
            # The end station settles with its body around y=0..120; aim for
            # the lower portion of it so the ship reads as docking from below.
            dock_y = 90
            self.player.y = lerp(self._outro_start_y, dock_y, eased)
            # Slide the player toward the playfield centre so it lines up with
            # the station's docking bay, in case combat ended off-centre.
            self.player.x = lerp(self.player.x, PLAY_W // 2, eased * 0.6)
            self.player.rect.center = (int(self.player.x), int(self.player.y))
            self.player.cinematic_scale = lerp(1.0, 0.25, eased)
            self.player.thrust += dt * 80
            self.player.tilt = 0.0
            self.stars.update(dt * 1.6)
            for b in self.bullets: b.update(dt)
            for part in self.particles: part.update(dt)
            for s in self.sparks: s.update(dt)
            for ex in self.explosions: ex.update(dt)
            self.bullets = [b for b in self.bullets if b.alive]
            self.particles = [p for p in self.particles if p.alive]
            self.sparks = [s for s in self.sparks if s.alive]
            self.explosions = [ex for ex in self.explosions if ex.alive]
            if self.outro_t <= 0:
                self.outcome = "win"
            return

        # Test mode: god mode + a parade that plays the takeoff-then-land
        # cinematic for every station before the regular timeline kicks in.
        if self.is_test:
            self.player.invuln = max(self.player.invuln, 9999.0)
            self.player.shield_hp = self.player.shield_max
            if not self._test_parade_done:
                self._update_test_parade(dt)
                return

        perf = self.app.perf
        self.elapsed += dt
        # Spawn from timeline
        perf.start("upd.spawn")
        while self.timeline_idx < len(self.level.timeline):
            t, fn = self.level.timeline[self.timeline_idx]
            if self.elapsed >= t:
                fn(self)
                self.timeline_idx += 1
            else:
                break
        perf.end("upd.spawn")

        # Player
        perf.start("upd.player")
        prev_player_x = self.player.x
        self.player.update(dt, controls, self.bullets, lambda: self.enemies, self.particles,
                           self.app.sounds, self.lasers, on_bomb=self._bomb)
        # Side-to-side parallax: stars shift opposite to player movement,
        # scaled by depth so the far layer barely budges.
        dx = self.player.x - prev_player_x
        self.stars.lateral_shift(dx)
        # Impulse-decay parallax for the playfield blit: each frame the
        # player moves, parallax_x kicks opposite to dx; with no input it
        # decays back to zero. The DECAY is what fixes the edge-clipping
        # bug — at rest, parallax_x is 0, so the player and enemies sit at
        # their natural screen positions and can reach left/right edges.
        # While moving, the whole playfield (bg + entities) lags behind
        # the player's motion, giving the camera-pan feel they wanted.
        self.parallax_x = clamp(
            self.parallax_x - dx * 0.55, -40.0, 40.0)
        self.parallax_x *= max(0.0, 1.0 - dt * 5.0)
        perf.end("upd.player")

        # Bullets
        perf.start("upd.bullets")
        for b in self.bullets:
            b.update(dt)
        perf.end("upd.bullets")

        # Enemies
        perf.start("upd.enemies")
        for e in self.enemies:
            e.update(dt, self.bullets, lambda: self.player if self.player.alive else None, self.app.sounds)
        self._separate_overlapping_shields()
        perf.end("upd.enemies")

        # Lasers (damage continuously)
        perf.start("upd.lasers")
        for laser in self.lasers:
            laser.update(dt)
            hit = laser.hit_rect()
            for e in self.enemies:
                if e.alive and hit.colliderect(e.hit_rect):
                    if e.hit(int(laser.damage_per_sec * dt)):
                        self._on_kill(e)
        perf.end("upd.lasers")

        # Pickups
        perf.start("upd.pickups")
        for p in self.pickups:
            p.update(dt)
        for ft in self.float_texts:
            ft.update(dt)
        perf.end("upd.pickups")

        perf.start("upd.particles")
        for part in self.particles:
            part.update(dt)
        for s in self.sparks:
            s.update(dt)
        for ex in self.explosions:
            ex.update(dt)
        perf.end("upd.particles")

        # Friendly bullet vs enemy/obstacle. Walls absorb the shot without
        # dying. Uses pygame.Rect.collidelist (single C-level scan) per
        # bullet instead of a Python double-loop calling colliderect —
        # under stress (70 bullets × 19 enemies = 1330 pairs/frame) the
        # Python overhead was dominating. When an enemy dies mid-pass we
        # patch its slot in the rect list with an off-screen sentinel so
        # later bullets skip it instead of re-matching a corpse.
        perf.start("col.bullet_enemy")
        enemies = self.enemies
        # shoot_rect expands to the shield's outer bounding box while
        # the enemy is shielded — so a graze on the visible halo
        # registers and the shield-gate path absorbs / ricochets the
        # shot. Reverts to the regular sprite hitbox once shield is
        # down. (Ram collisions still use hit_rect a few blocks below.)
        hit_rects = [e.shoot_rect for e in enemies]
        dead = _DEAD_RECT_SENTINEL
        sparks = self.sparks
        for b in self.bullets:
            if not (b.alive and b.friendly):
                continue
            br = b.rect
            while True:
                idx = br.collidelist(hit_rects)
                if idx == -1:
                    break
                e = enemies[idx]
                if not e.alive:
                    hit_rects[idx] = dead
                    continue
                if isinstance(e, Wall):
                    sparks.append(Spark(br.centerx, br.centery, (200, 200, 220)))
                    sparks.append(Spark(br.centerx, br.centery, WHITE))
                    e.hit_flash_t = 0.05
                    # Wall hits pay 1 credit each — mining-asteroid feel.
                    self._earn(1)
                    self.float_texts.append(FloatText(
                        br.centerx, br.centery - 6, "$1"))
                    b.alive = False
                    break
                # Coloured shield gate. Shield is a binary modifier:
                #   - right weapon  → pass through; check enemy hitbox
                #                     (rect) and damage normally if hit
                #   - wrong main    → check circle distance to shield;
                #                     if inside, ricochet (turns hostile)
                #   - missile/drone → absorbed by circle (no damage)
                #
                # The broad phase already matched on shoot_rect (shield
                # bounding box); now we refine.
                if e.shield_color:
                    shield_rgb = SHIELD_COLOR_RGB[e.shield_color]
                    right_kind = SHIELD_COLOR_TO_KIND[e.shield_color]
                    bk = b.weapon_kind
                    if bk == right_kind:
                        # Bullet passes through the shield. Damage only
                        # registers if it actually overlaps the hitbox.
                        if not br.colliderect(e.hit_rect):
                            # Inside the broad-phase rect but missed the
                            # enemy's real hitbox — flies on, no hit.
                            break
                        # else: fall through to the normal hit() path.
                    else:
                        # Wrong weapon: circle test.
                        dx = b.x - e.rect.centerx
                        dy = b.y - e.rect.centery
                        if dx * dx + dy * dy > (e.shield_radius
                                                + SHIELD_THICKNESS) ** 2:
                            break
                        if bk in ("pulse", "spread", "vulcan") and not b.ricocheted:
                            _ricochet_bullet(b, e)
                        else:
                            b.alive = False
                        sparks.append(Spark(br.centerx, br.centery, shield_rgb))
                        break
                killed = e.hit(b.damage)
                # Impact-spark burst only fires on Boss hits. Small fries
                # rely on the sprite hit_flash + (on kill) the explosion
                # particles for feedback — under stress that's the
                # difference between 100s of sparks/frame and dozens.
                if isinstance(e, Boss):
                    ix = br.centerx
                    iy = br.centery
                    burst = 12 if killed else 9
                    for _ in range(burst):
                        color = random.choice(IMPACT_SPARK_COLORS)
                        sparks.append(ImpactSpark(ix, iy, color, b.vx, b.vy))
                    # White centre flash for a bit of contrast in the burst.
                    sparks.append(Spark(ix, iy, WHITE))
                e.hit_flash_t = 0.08
                if killed:
                    self._on_kill(e)
                    hit_rects[idx] = dead
                if b.pierce > 0:
                    b.pierce -= 1
                else:
                    b.alive = False
                break
        perf.end("col.bullet_enemy")

        # Enemy bullet vs walls (absorb) then vs player. Walls list is
        # usually tiny but the per-bullet inner loop still ran in Python;
        # collidelist drops it to a single C scan.
        perf.start("col.bullet_player")
        if self.player.alive:
            walls = [e for e in self.enemies
                     if e.alive and isinstance(e, Wall)]
            wall_rects = [w.hit_rect for w in walls]
            player_hit = self.player.hit_rect
            sparks = self.sparks
            for b in self.bullets:
                if not (b.alive and not b.friendly):
                    continue
                br = b.rect
                if wall_rects and br.collidelist(wall_rects) != -1:
                    b.alive = False
                    sparks.append(Spark(br.centerx, br.centery, ORANGE))
                    continue
                if br.colliderect(player_hit):
                    b.alive = False
                    self._damage_player(400)
        perf.end("col.bullet_player")

        # Enemy vs player. Single C-level player.collidelistall over the
        # enemy rect list — yields every overlapping enemy in one call.
        # Walls push the player out, everything else damages (and dies if
        # not a boss).
        perf.start("col.enemy_player")
        if self.player.alive:
            enemies = self.enemies
            enemy_rects = [e.hit_rect for e in enemies]
            player_hit = self.player.hit_rect
            for idx in player_hit.collidelistall(enemy_rects):
                e = enemies[idx]
                if not e.alive:
                    continue
                if isinstance(e, Wall):
                    self._push_player_out(e.rect)
                    continue
                if not isinstance(e, Boss):
                    e.hit(9999)
                    self._on_kill(e, drop=False)
                self._damage_player(1600)
        perf.end("col.enemy_player")

        # Pickup pickup — single collidelistall, same shape as enemy>player.
        perf.start("col.pickup")
        if self.player.alive and self.pickups:
            pickups = self.pickups
            pickup_rects = [p.rect for p in pickups]
            player_hit = self.player.hit_rect
            for idx in player_hit.collidelistall(pickup_rects):
                p = pickups[idx]
                if not p.alive:
                    continue
                p.alive = False
                result = self.player.collect(p, save=self.app.save)
                if result and result[0] == "credits":
                    self._earn(result[1])
                    # Drift "+$N" above where the pickup was so the player
                    # sees how much they actually banked — useful when
                    # weapon-upgrade pickups convert to credits (200) and
                    # also for the basic money drop (25). Pickup is dead
                    # now, so use its last rect for the spawn point.
                    self.float_texts.append(FloatText(
                        p.rect.centerx, p.rect.top - 4,
                        f"${result[1]}"))
                self.app.sounds["money" if p.kind == "money" else "pickup"].play()
        perf.end("col.pickup")

        # Cleanup
        perf.start("upd.cleanup")
        self.bullets = [b for b in self.bullets if b.alive]
        self.enemies = [e for e in self.enemies if e.alive]
        self.pickups = [p for p in self.pickups if p.alive]
        self.particles = [p for p in self.particles if p.alive]
        self.sparks = [s for s in self.sparks if s.alive]
        self.explosions = [ex for ex in self.explosions if ex.alive]
        self.lasers = [l for l in self.lasers if l.alive]
        self.float_texts = [t for t in self.float_texts if t.alive]
        perf.end("upd.cleanup")

        self.flash = max(0, self.flash - dt * 4)
        self.shake = max(0, self.shake - dt * 4)

        if self.message_timer > 0:
            self.message_timer -= dt

        # Win/loss. Both win paths wait for any floating powerups to either be
        # collected or drift off-screen before kicking off the outro sequence.
        if not self.player.alive:
            self.outcome = "loss"
        elif self.is_test:
            # Test mode never ends on a boss kill — keep going until the full
            # timeline has fired AND the play area is clean.
            if (self.timeline_idx >= len(self.level.timeline)
                    and not self.enemies
                    and not self.pickups
                    and self.elapsed >= self.level.duration):
                self._begin_outro()
        elif self.level.has_boss:
            if any(isinstance(e, Boss) for e in self.enemies):
                self.boss_spawned = True
            if (self.boss_spawned
                    and not any(isinstance(e, Boss) for e in self.enemies)
                    and not self.pickups):
                self._begin_outro()
        else:
            if (self.elapsed >= self.level.duration
                    and not self.enemies
                    and not self.pickups):
                self._begin_outro()

    def _resolve_drop_kind(self, kind):
        """Spawn drops at their original kind. Weapon power-ups that
        can't apply to the player's current loadout (max level, locked
        tier, or — for "main" — the wrong main being held that frame)
        convert to credits inside Player.collect() at pickup time. This
        keeps the visible drop telegraph honest: a main-weapon icon
        ALWAYS spawns from main-drop rolls, even if the player can't
        currently consume it — they still get +$N from it."""
        return kind

    def _begin_outro(self):
        if self.outro_t > 0 or self.outcome is not None:
            return
        self.outro_t = 2.4
        self._outro_start_y = float(self.player.y)
        self.player.cinematic = True
        # Clear remaining hazards for a clean docking sequence
        for b in self.bullets:
            if not b.friendly:
                b.alive = False
        for e in self.enemies:
            e.alive = False
        self.enemies = []
        # The outro update path early-returns before the regular per-frame
        # decay of shake / flash / parallax_x, so any residual value would
        # freeze for the whole 2.4 s cinematic and tremble the landing
        # station. Zero them here so docking reads calm.
        self.shake = 0
        self.flash = 0
        self.parallax_x = 0.0

    def _fire_instant_clear_cheat(self):
        """Simulate playing the level to completion in a single frame: fire
        every remaining timeline spawn, kill every enemy (with their normal
        drops + credits), then collect every pickup. The collected loot
        plus credit delta is stashed in self._cheat_summary so _draw can
        show a 3-second overlay before the outro starts."""
        credits_before = self.app.save.credits
        counts = {"main": 0, "side": 0, "shield": 0, "bomb": 0, "money": 0}

        # 1. Fast-forward the rest of the timeline so the rest-of-level
        #    enemies actually exist in self.enemies before we kill them.
        while self.timeline_idx < len(self.level.timeline):
            _, fn = self.level.timeline[self.timeline_idx]
            try:
                fn(self)
            except Exception:
                pass
            self.timeline_idx += 1
        self.elapsed = max(self.elapsed, self.level.duration)

        # 2. Kill every enemy WITH drops. _on_kill awards SCORE + CREDITS
        #    and rolls the DROP_TABLE into self.pickups via the engine's
        #    existing logic.
        for e in list(self.enemies):
            if e.alive:
                # show_text=False — cheat clears the whole level in one
                # frame; the summary overlay already shows totals.
                self._on_kill(e, drop=True, show_text=False)
                e.alive = False
        self.enemies = []

        # 3. Collect every pickup the kills dropped. Maxed weapon pickups
        #    fall back to credits via player.collect() automatically.
        for p in list(self.pickups):
            if not p.alive:
                continue
            counts[p.kind] = counts.get(p.kind, 0) + 1
            result = self.player.collect(p, save=self.app.save)
            if result and result[0] == "credits":
                self._earn(result[1])
            p.alive = False
        self.pickups = []

        # 4. Stash the summary and extend the outro so the overlay is
        #    fully visible before the level transition kicks in.
        self._cheat_summary = {
            "credits": self.app.save.credits - credits_before,
            "counts": counts,
        }
        self._cheat_summary_t = 3.0
        self._begin_outro()
        # Default outro is 2.4 s; bump it so the 3 s summary completes
        # before we hand back to the App and roll the shop screen.
        self.outro_t = max(self.outro_t, self._cheat_summary_t + 0.4)

    def _bomb(self):
        # Clear all enemy bullets, damage all on-screen enemies
        for b in self.bullets:
            if not b.friendly:
                b.alive = False
        for e in self.enemies:
            if isinstance(e, Boss):
                e.hit(15)
            else:
                if e.hit(5):
                    self._on_kill(e)
        self.flash = 1.0
        self.shake = 1.0
        for _ in range(80):
            self.particles.append(Particle(
                random.uniform(0, PLAY_W), random.uniform(0, PLAY_H),
                random.choice([CYAN, WHITE, YELLOW]),
                size=4, speed_range=(40, 220), life_range=(0.3, 0.8),
            ))

    def _damage_player(self, dmg):
        killed = self.player.take_damage(dmg)
        if killed:
            self.shake = 1.2
            for _ in range(60):
                self.particles.append(Particle(self.player.rect.centerx, self.player.rect.centery,
                                               random.choice([CYAN, WHITE, ORANGE]), size=4))
            self.app.sounds["big_boom"].play()
        else:
            self.app.sounds["hit"].play()
            self.flash = 0.4
            self.shake = 0.4

    def _on_kill(self, enemy, drop=True, show_text=True):
        self.score += enemy.SCORE
        self._earn(enemy.CREDITS)
        cx, cy = enemy.rect.centerx, enemy.rect.centery
        # Drift "+$N" above the kill so the player sees the bounty per
        # enemy. Suppressed by show_text=False from the level-clear cheat
        # which kills everything in one frame and would spam the screen.
        if show_text and enemy.CREDITS > 0:
            self.float_texts.append(FloatText(
                cx, cy - enemy.rect.height // 2 - 2,
                f"${enemy.CREDITS}"))
        is_boss = isinstance(enemy, Boss)
        # Visual radius from the entity's hitbox so the explosion matches
        # the sprite the player sees (falls back to sprite rect when no
        # hitbox is defined yet).
        hr = enemy.hit_rect
        visual_r = max(8, max(hr.width, hr.height) // 2)
        sprite_colors = _sample_sprite_colors(enemy.image, n=12)
        if is_boss:
            outer_r = int(visual_r * 2.5 + 12)
            mid_r = int(visual_r * 1.6 + 6)
            inner_r = max(8, int(visual_r * 1.0))
            self.explosions.append(ExplosionRing(cx, cy, max_r=outer_r, color=RED, life=0.85))
            self.explosions.append(ExplosionRing(cx, cy, max_r=mid_r, color=YELLOW, life=0.55))
            self.explosions.append(ExplosionRing(cx, cy, max_r=inner_r, color=WHITE, life=0.25))
            off_r = int(visual_r * 0.7)
            self.explosions.append(ExplosionRing(
                cx - off_r // 2, cy + off_r // 3,
                max_r=int(visual_r * 1.2 + 8), color=ORANGE, life=0.55))
            self.explosions.append(ExplosionRing(
                cx + off_r // 2, cy - off_r // 4,
                max_r=int(visual_r * 1.3 + 8), color=ORANGE, life=0.65))
            # Halved count + doubled size: same visual mass, fewer per-
            # frame draw.particles blits (boss kill ~430 particles ->
            # ~215 with chunkier blocks reading as bigger debris).
            for _ in range(24):
                self.particles.append(Particle(cx, cy, RED, size=10,
                                               speed_range=(60, 320)))
            for _ in range(6):
                self.particles.append(Particle(cx, cy, YELLOW, size=10,
                                               speed_range=(80, 260)))
            # Sprite-coloured debris chunks
            for _ in range(22):
                c = random.choice(sprite_colors)
                sz = random.randint(6, 14)
                self.particles.append(Debris(cx, cy, c, sz,
                                             speed_range=(110, 360)))
            for _ in range(4):
                kind = self._resolve_drop_kind(
                    random.choice(["main", "side", "shield", "bomb"]))
                self.pickups.append(Pickup(cx + random.uniform(-20, 20),
                                           cy + random.uniform(-20, 20),
                                           kind, self.assets["pickup_" + kind]))
            self.shake = 2.0
        else:
            outer_r = int(visual_r * 2.1 + 8)
            mid_r = int(visual_r * 1.4 + 4)
            inner_r = max(6, int(visual_r * 0.8))
            self.explosions.append(ExplosionRing(cx, cy, max_r=outer_r, color=ORANGE, life=0.55))
            self.explosions.append(ExplosionRing(cx, cy, max_r=mid_r, color=YELLOW, life=0.40))
            self.explosions.append(ExplosionRing(cx, cy, max_r=inner_r, color=WHITE, life=0.20))
            # Halved count + doubled size: same visual presence, fewer
            # per-frame draw.particles blits during heavy combat.
            for _ in range(16):
                self.particles.append(Particle(cx, cy, ORANGE, size=10,
                                               speed_range=(60, 300)))
            for _ in range(5):
                self.particles.append(Particle(cx, cy, YELLOW, size=8,
                                               speed_range=(80, 260)))
            # Sprite-coloured debris. Count + chunk size scale with the
            # visual radius so small rocks toss a couple of chips while a
            # big bomber sprays a real shower.
            n_debris = max(4, min(14, visual_r // 3 + 4))
            for _ in range(n_debris):
                c = random.choice(sprite_colors)
                sz = random.randint(4, max(6, visual_r // 3))
                self.particles.append(Debris(cx, cy, c, sz))
            self.shake = max(self.shake, 0.4)
            if isinstance(enemy, Mine):
                # Mines get an even bigger shockwave + radius damage to the player.
                self.explosions.append(ExplosionRing(cx, cy, max_r=Mine.EXPLOSION_RADIUS,
                                                     color=(255, 160, 60), life=0.6))
                if self.player.alive:
                    d = math.hypot(self.player.rect.centerx - cx,
                                   self.player.rect.centery - cy)
                    if d < Mine.EXPLOSION_RADIUS:
                        self._damage_player(Mine.EXPLOSION_DAMAGE)
                self.shake = max(self.shake, 0.8)
            if drop and enemy.DROP_TABLE and random.random() < enemy.DROP_CHANCE * self.scrap_drop_factor:
                kind = self._resolve_drop_kind(_biased_drop_kind(self, enemy.DROP_TABLE))
                self.pickups.append(Pickup(cx, cy, kind, self.assets["pickup_" + kind]))
        self.app.sounds["big_boom" if is_boss else "boom"].play()

    def _separate_overlapping_shields(self):
        """Push apart pairs of shielded NON-BOSS enemies whose shield
        circles overlap AND whose colours differ. Same-colour overlaps
        stay (no forced weapon swap to clear them). Bosses are excluded
        — their sprites are huge + they manage their own shield cycle,
        the geometry gets messy."""
        shielded = [e for e in self.enemies
                    if e.alive and e.shield_color
                    and not isinstance(e, Boss)]
        n = len(shielded)
        if n < 2:
            return
        for i in range(n):
            a = shielded[i]
            ax = float(a.x)
            ay = float(a.y)
            ar = a.shield_radius
            for j in range(i + 1, n):
                b = shielded[j]
                if b.shield_color == a.shield_color:
                    continue
                bx = float(b.x)
                by = float(b.y)
                dx = bx - ax
                dy = by - ay
                d_sq = dx * dx + dy * dy
                min_d = ar + b.shield_radius
                if d_sq >= min_d * min_d:
                    continue
                # Overlapping. Split the displacement so both move half
                # the overlap apart along the connecting axis (+ 0.5 px
                # of slop so we don't keep re-triggering next frame).
                d = math.sqrt(d_sq) if d_sq > 1e-6 else 0.01
                overlap = min_d - d
                push = overlap * 0.5 + 0.5
                ndx = dx / d
                ndy = dy / d
                a.x = ax - ndx * push
                a.y = ay - ndy * push
                b.x = bx + ndx * push
                b.y = by + ndy * push
                a.rect.center = (int(a.x), int(a.y))
                b.rect.center = (int(b.x), int(b.y))
                ax = float(a.x)
                ay = float(a.y)

    def _push_player_out(self, wall_rect):
        """Resolve overlap between the player and a wall by pushing along the
        shallowest axis."""
        pr = self.player.rect
        overlap_left = pr.right - wall_rect.left
        overlap_right = wall_rect.right - pr.left
        overlap_top = pr.bottom - wall_rect.top
        overlap_bottom = wall_rect.bottom - pr.top
        if min(overlap_left, overlap_right, overlap_top, overlap_bottom) <= 0:
            return
        m = min(overlap_left, overlap_right, overlap_top, overlap_bottom)
        if m == overlap_left:
            self.player.x -= overlap_left + 0.5
        elif m == overlap_right:
            self.player.x += overlap_right + 0.5
        elif m == overlap_top:
            self.player.y -= overlap_top + 0.5
        else:
            self.player.y += overlap_bottom + 0.5
        self.player.rect.center = (int(self.player.x), int(self.player.y))

    def _earn(self, amount):
        self.credits_earned += amount
        self.app.save.credits += amount

    def _draw_boss_intro(self, surf):
        t = self.boss_intro_t
        pulse = 0.5 + 0.5 * math.sin((2.6 - t) * 18)
        # pulsing red border
        border_alpha = int(140 + 80 * pulse)
        border = pygame.Surface((PLAY_W, PLAY_H), pygame.SRCALPHA)
        pygame.draw.rect(border, (220, 50, 50, border_alpha), (0, 0, PLAY_W, PLAY_H), 6)
        surf.blit(border, (0, 0))
        # WARNING text
        big = self.app.fonts["big"].render("! WARNING !", False, (255, 90, 90))
        surf.blit(big, big.get_rect(center=(PLAY_W // 2, PLAY_H // 2 - 24)))
        # Subtitle (only show if pulse > 0.3 to give a flicker)
        if pulse > 0.3:
            sub = self.app.fonts["small"].render("BOSS APPROACHING", False, (220, 180, 180))
            surf.blit(sub, sub.get_rect(center=(PLAY_W // 2, PLAY_H // 2 + 18)))

    def _draw(self, controls):
        perf = self.app.perf
        screen = self.app.screen
        shake_x = random.randint(-int(self.shake * 3), int(self.shake * 3)) if self.shake > 0 else 0
        shake_y = random.randint(-int(self.shake * 3), int(self.shake * 3)) if self.shake > 0 else 0
        playfield = self._playfield
        playfield_full = self._playfield_full
        playfield_full.fill(BLACK)
        perf.start("draw.bg_ribbon")
        # Bg fills the full (PLAY_W + 2*PLAY_MARGIN)-wide surface so the
        # bands exposed by parallax/shake on either side stay covered.
        self.bg_ribbon.draw(playfield_full)
        perf.end("draw.bg_ribbon")
        perf.start("draw.nebula")
        if ENABLE_NEBULA:
            self.nebula.draw(playfield)
        perf.end("draw.nebula")
        perf.start("draw.stars")
        self.stars.draw(playfield)
        perf.end("draw.stars")
        perf.start("draw.pickups")
        for p in self.pickups:
            p.draw(playfield)
        perf.end("draw.pickups")
        perf.start("draw.bullets")
        # Batch consecutive sprite-bearing bullets into a single
        # Surface.blits() call (one C scan instead of N Python blit calls).
        # Anything that needs special draw work (procedural enemy bullets,
        # rare flipped enemy glyphs) breaks the batch with .draw().
        batch = []
        batch_append = batch.append
        playfield_blits = playfield.blits
        for b in self.bullets:
            info = b.batch_blit_info()
            if info is not None:
                batch_append(info)
            else:
                if batch:
                    playfield_blits(batch, doreturn=False)
                    batch = []
                    batch_append = batch.append
                b.draw(playfield)
        if batch:
            playfield_blits(batch, doreturn=False)
        perf.end("draw.bullets")
        perf.start("draw.lasers")
        for laser in self.lasers:
            laser.draw(playfield)
        perf.end("draw.lasers")
        perf.start("draw.enemies")
        for e in self.enemies:
            e.draw(playfield)
        perf.end("draw.enemies")
        perf.start("draw.particles")
        for part in self.particles:
            part.draw(playfield)
        for s in self.sparks:
            s.draw(playfield)
        for ex in self.explosions:
            ex.draw(playfield)
        for ft in self.float_texts:
            ft.draw(playfield)
        perf.end("draw.particles")
        # Stations are drawn BEFORE the player so the ship reads as taking off
        # from / docking at them.
        # Departing platform scrolls down out of view during the intro.
        if self.intro_t > 0:
            p = clamp(1.0 - max(0.0, self.intro_t) / 2.4, 0.0, 1.0)
            sh = self.station_start.get_height()
            sx = (PLAY_W - self.station_start.get_width()) // 2
            sy = int(PLAY_H - sh + p * (sh + 20))
            playfield.blit(self.station_start, (sx, sy))
        # Arrival station scrolls in from above during the outro.
        if self.outro_t > 0:
            p = clamp(1.0 - max(0.0, self.outro_t) / 2.4, 0.0, 1.0)
            sh = self.station_end.get_height()
            sx = (PLAY_W - self.station_end.get_width()) // 2
            entry = min(p / 0.5, 1.0)
            sy = int(-sh + entry * (sh + 20))
            playfield.blit(self.station_end, (sx, sy))
        perf.start("draw.player")
        if self.player.alive:
            self.player.draw(playfield)
        perf.end("draw.player")
        if self.boss_intro_t > 0:
            self._draw_boss_intro(playfield)
        if self.player.bomb_flash > 0:
            o = self._bomb_overlay
            o.set_alpha(int(180 * self.player.bomb_flash))
            playfield_full.blit(o, (0, 0))
        if self.flash > 0:
            o = self._flash_overlay_red if self.outcome != "win" else self._flash_overlay_cyan
            o.set_alpha(int(80 * self.flash))
            playfield_full.blit(o, (0, 0))
        # Hidden test-mission cinematic — only runs in the test mission.
        if self.is_test and not getattr(self, "_test_parade_done", True):
            self._draw_test_parade_stations(playfield)
        # Debug / perf overlay is available in any play state (R3 cycles it).
        # Tracked under its own stage so the cost of *viewing* perf-detail
        # is itself visible in the perf-detail panel.
        if self._test_overlay_mode != 3:
            perf.start("draw.overlay")
            self._draw_test_banner(playfield)
            perf.end("draw.overlay")

        playfield.blit(self.vignette, (0, 0))
        # Parallax shifts the WHOLE playfield blit so bg + entities all
        # drift opposite to the player's lateral motion. With the
        # impulse-decay parallax_x update (below) this is non-zero only
        # while the player is actively moving — when they stop, it lerps
        # back to 0, so the player and enemies can still reach the
        # screen's left/right edges at rest.
        perf.start("draw.blit_screen")
        parallax_off = int(self.parallax_x)
        screen.blit(playfield_full,
                    (shake_x + parallax_off - PLAY_MARGIN, shake_y))
        perf.end("draw.blit_screen")
        perf.start("draw.hud")
        hud_draw(screen, self.app.fonts, self.assets, self.player, self.app.save,
                 self.level.name, self.score,
                 (self.level.duration - self.elapsed) if not self.level.has_boss else 0)
        _draw_main_swap_hints(screen, self.app.fonts, self.assets, self.player)
        perf.end("draw.hud")

        # Centre-screen banner: paused / mission complete / ship destroyed.
        # All three share the same template (dim overlay + big title + small
        # subtitle); only the strings differ, so one element with template
        # vars covers everything. visible_when=banner_visible gates the
        # whole container so no banner renders when none of the three
        # states are active.
        banner_title = banner_subtitle = ""
        if self.pause:
            banner_title, banner_subtitle = "PAUSED", "START to resume"
        elif self.outcome == "win":
            banner_title = "MISSION COMPLETE"
            banner_subtitle = f"+{self.credits_earned} cr   {BUTTON_SCHEME['fire'][1]} continue"
        elif self.outcome == "loss":
            banner_title, banner_subtitle = "SHIP DESTROYED", f"{BUTTON_SCHEME['fire'][1]} continue"
        play_vars = {
            "banner_visible": bool(banner_title),
            "banner_title": banner_title,
            "banner_subtitle": banner_subtitle,
        }
        banner = get_element("play", "play_banner", **play_vars)
        if banner is not None:
            _layout_draw_item(screen, banner, self.app.fonts,
                              self.app.assets, play_vars)

        draw_layout_overlay(screen, "play", self.app.fonts, self.app.assets,
                            template_vars=play_vars)

        # Cheat summary overlay — drawn last so it sits on top of every
        # other layer (banner, vignette, etc.).
        if self._cheat_summary_t > 0 and self._cheat_summary is not None:
            self._draw_cheat_summary(screen)

    def _draw_cheat_summary(self, screen):
        """Centre-of-screen panel listing the cash + pickups the L2+R2
        cheat awarded. Visible for 3 s; alpha fades over the last 0.6 s."""
        fonts = self.app.fonts
        summary = self._cheat_summary
        credits = summary.get("credits", 0)
        counts = summary.get("counts", {})
        lines = [("INSTANT CLEAR", "big")]
        lines.append((f"+${credits}", "big"))
        labels = (
            ("main",   "Weapon"),
            ("side",   "Sidekick"),
            ("shield", "Shield"),
            ("bomb",   "Bomb"),
            ("money",  "Coin"),
        )
        for key, label in labels:
            n = counts.get(key, 0)
            if n > 0:
                lines.append((f"{label} x{n}", "small"))
        # Panel size sized to the rendered content.
        rendered = []
        for txt, sz in lines:
            font = fonts.get(sz) or fonts["small"]
            rendered.append((font.render(txt, False, WHITE), sz))
        line_h = max(s.get_height() for s, _ in rendered) + 4
        panel_w = max(s.get_width() for s, _ in rendered) + 48
        panel_h = line_h * len(rendered) + 28
        # Fade alpha over the last 0.6 s.
        fade = clamp(self._cheat_summary_t / 0.6, 0.0, 1.0)
        bg_alpha = int(210 * fade)
        text_alpha = int(255 * fade)
        panel = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
        panel.fill((10, 14, 30, bg_alpha))
        pygame.draw.rect(panel, (120, 200, 255, text_alpha),
                         (0, 0, panel_w, panel_h), 2)
        y = 14
        for surf, _ in rendered:
            surf.set_alpha(text_alpha)
            panel.blit(surf, ((panel_w - surf.get_width()) // 2, y))
            y += line_h
        screen.blit(panel,
                    ((SCREEN_W - panel_w) // 2,
                     (SCREEN_H - panel_h) // 2))

    # ---- Test-mission gamepad handlers ------------------------------------
    _TEST_MAIN_TYPES = ("pulse", "spread", "vulcan")
    _TEST_SIDE_TYPES = ("missile", "drone", "none")
    _TEST_ABILITIES = ("screen_clear", "shield_burst", "mega_laser")
    _TEST_AXIS_LT = 4
    _TEST_AXIS_RT = 5
    _TEST_AXIS_RSX = 2
    _TEST_AXIS_RSY = 3
    _TEST_TRIG_THRESH = 0.1
    _TEST_RSTICK_THRESH = 0.5
    # The handheld exposes L2/R2/L3 as plain buttons (no analog triggers).
    # These match the RG35XX Pro indices; PC builds still get L2/R2 via
    # the trigger axes below.
    _TEST_BTN_L2_FALLBACKS = (JOY_L2,)
    _TEST_BTN_R2_FALLBACKS = (JOY_R2,)
    _TEST_BTN_L3_FALLBACKS = (JOY_L3,)

    def _test_set_action(self, msg):
        self._test_action_msg = msg
        self._test_action_t = 1.8

    def _handle_overlay_toggle(self, events):
        """R3 cycles the debug/perf overlay. In test mode all 4 modes are
        reachable (debug -> perf -> perf-detail -> off); in normal play
        mode 0 (test-only debug info) is skipped so the cycle is
        off -> perf -> perf-detail -> off."""
        for ev in events:
            if ev.type != pygame.JOYBUTTONDOWN or ev.button != JOY_R3:
                continue
            if self.is_test:
                self._test_overlay_mode = (self._test_overlay_mode + 1) % 4
            else:
                cycle = (3, 1, 2)  # off, perf summary, perf detail
                cur = self._test_overlay_mode
                idx = cycle.index(cur) if cur in cycle else 0
                self._test_overlay_mode = cycle[(idx + 1) % len(cycle)]
            return  # one toggle per frame even if R3 buffered multiple events

    def _handle_test_inputs(self, events, controls):
        """Process the test-mission-only gamepad inputs. Buttons fire on
        JOYBUTTONDOWN events; triggers (L2/R2) and the right stick are
        polled because pygame doesn't surface trigger 'press' events. The
        button-fallback constants let the handler cope with controllers
        whose L2/R2 are digital buttons rather than analog axes."""
        # Edge-triggered button presses.
        for ev in events:
            if ev.type != pygame.JOYBUTTONDOWN:
                continue
            btn = ev.button
            # Diagnostic — surface the raw index in the banner so a user
            # on an unknown handheld can map their buttons by eye.
            self._test_last_btn = btn
            self._test_last_btn_t = pygame.time.get_ticks()
            if btn == JOY_L1:
                self._test_cycle_main()
            elif btn == JOY_R1:
                # SELECT held => coarse skip (parade -> waves -> first boss
                # -> next boss). Plain R1 => one atomic step (sub-phase /
                # one wave / one boss).
                if controls.select:
                    self._test_skip_chapter()
                else:
                    self._test_skip_step()
            elif btn in self._TEST_BTN_L2_FALLBACKS:
                self._test_cycle_side()
            elif btn in self._TEST_BTN_R2_FALLBACKS:
                self._test_cycle_ability()
            elif btn in self._TEST_BTN_L3_FALLBACKS:
                self._test_reset_level()
            # R3 (overlay toggle) is handled in _handle_overlay_toggle so it
            # also works outside the test mission. Test mode still wants the
            # action banner echo though — surface that here.
            elif btn == JOY_R3:
                self._test_set_action(
                    f"overlay -> {('debug', 'perf', 'perf-detail', 'off')[self._test_overlay_mode]}")
        # Triggers (analog axes) — debounced to fire once per cross.
        lt = self._test_max_axis(self._TEST_AXIS_LT)
        rt = self._test_max_axis(self._TEST_AXIS_RT)
        lt_now = lt > self._TEST_TRIG_THRESH
        rt_now = rt > self._TEST_TRIG_THRESH
        if lt_now and not self._test_l2_held:
            self._test_cycle_side()
        if rt_now and not self._test_r2_held:
            self._test_cycle_ability()
        self._test_l2_held = lt_now
        self._test_r2_held = rt_now
        # Snapshot live joystick state for the diagnostic banner line.
        if self.app.joys:
            j = self.app.joys[0]
            try:
                n_axes = j.get_numaxes()
                self._test_diag_axes = tuple(
                    round(j.get_axis(i), 2) for i in range(min(8, n_axes)))
                self._test_diag_n_btn = j.get_numbuttons()
            except pygame.error:
                pass
        # Right stick: discrete one-shot per direction-cross.
        rx = self._test_max_axis(self._TEST_AXIS_RSX, signed=True)
        ry = self._test_max_axis(self._TEST_AXIS_RSY, signed=True)
        qx = (1 if rx > self._TEST_RSTICK_THRESH
              else (-1 if rx < -self._TEST_RSTICK_THRESH else 0))
        qy = (1 if ry > self._TEST_RSTICK_THRESH
              else (-1 if ry < -self._TEST_RSTICK_THRESH else 0))
        pqx, pqy = self._test_rstick_prev
        if qy != pqy and qy != 0:
            # up = qy < 0; upgrade main on up, downgrade on down
            self._test_adjust_main_level(-1 if qy > 0 else +1)
        if qx != pqx and qx != 0:
            self._test_adjust_side_level(+1 if qx > 0 else -1)
        self._test_rstick_prev = (qx, qy)

    def _test_max_axis(self, axis, signed=False):
        """Largest axis reading across all joysticks for the given axis idx.
        signed=False returns max(value, ...) clamped to >= 0; signed=True
        keeps the sign so the caller can detect left/right or up/down."""
        best = 0.0
        for j in self.app.joys:
            try:
                if axis < j.get_numaxes():
                    v = j.get_axis(axis)
                    if signed:
                        if abs(v) > abs(best):
                            best = v
                    else:
                        if v > best:
                            best = v
            except pygame.error:
                pass
        return best

    def _test_cycle_main(self):
        types = self._TEST_MAIN_TYPES
        cur = self.player.loadout.main_type
        idx = types.index(cur) if cur in types else 0
        nxt = types[(idx + 1) % len(types)]
        self.player.loadout.main_type = nxt
        # Make sure the new type owns a non-zero level so it actually fires.
        if getattr(self.player.loadout, f"main_{nxt}", 0) <= 0:
            setattr(self.player.loadout, f"main_{nxt}", 1)
        self.player.cooldown_main = 0
        self._test_set_action(f"main -> {nxt}")

    def _test_cycle_side(self):
        types = self._TEST_SIDE_TYPES
        cur = self.player.loadout.side_type
        idx = types.index(cur) if cur in types else 0
        nxt = types[(idx + 1) % len(types)]
        self.player.loadout.side_type = nxt
        if nxt != "none" and getattr(self.player.loadout, f"side_{nxt}", 0) <= 0:
            setattr(self.player.loadout, f"side_{nxt}", 1)
        self.player.cooldown_side = 0
        self._test_set_action(f"side -> {nxt}")

    def _test_cycle_ability(self):
        types = self._TEST_ABILITIES
        cur = self.player.loadout.ability
        idx = types.index(cur) if cur in types else 0
        nxt = types[(idx + 1) % len(types)]
        self.player.loadout.ability = nxt
        self.player.ability_cd = 0
        self._test_set_action(f"ability -> {nxt}")

    def _test_adjust_main_level(self, delta):
        mtype = self.player.loadout.main_type
        if mtype not in self._TEST_MAIN_TYPES:
            return
        field = f"main_{mtype}"
        cur = getattr(self.player.loadout, field, 1)
        new = int(clamp(cur + delta, 1, MAIN_WEAPON_MAX))
        if new != cur:
            setattr(self.player.loadout, field, new)
            self.player.cooldown_main = 0
            self._test_set_action(f"{mtype} lvl {new}")

    def _test_adjust_side_level(self, delta):
        stype = self.player.loadout.side_type
        if stype == "none":
            self._test_set_action("side=none (cycle L2)")
            return
        field = f"side_{stype}"
        cur = getattr(self.player.loadout, field, 1)
        new = int(clamp(cur + delta, 1, SIDE_WEAPON_MAX))
        if new != cur:
            setattr(self.player.loadout, field, new)
            self.player.cooldown_side = 0
            self._test_set_action(f"{stype} lvl {new}")

    def _test_clear_field(self):
        """Sweep the play area — used by skip-chapter so the next chapter
        starts on a clean slate."""
        for e in self.enemies:
            e.alive = False
        for b in self.bullets:
            if not b.friendly:
                b.alive = False
        self.enemies = [e for e in self.enemies if e.alive]
        self.bullets = [b for b in self.bullets if b.alive]
        self.pickups = []
        self.particles = []
        self.sparks = []
        self.explosions = []
        self.lasers = []
        self.boss_intro_t = 0

    def _test_skip_step(self):
        """Plain R1: advance ONE atomic step — one parade sub-phase, one
        wave, or one boss. Within the parade each takeoff and each landing
        counts as a step (so a full base takes two presses to skim through)."""
        self._test_clear_field()
        if not self._test_parade_done:
            # Force the current sub-phase to finish on this tick; the
            # parade updater will advance to the next sub-phase / station.
            sub = self._test_parade_sub
            idx = self._test_parade_idx
            self._test_parade_t = 0.0
            if sub == "takeoff":
                self._test_set_action(
                    f"skip -> station {idx + 1} landing")
            else:
                if idx + 1 >= 10:
                    self._test_set_action("skip -> enemy waves")
                else:
                    self._test_set_action(
                        f"skip -> station {idx + 2} takeoff")
            return
        if self.timeline_idx >= self._test_bosses_end:
            self.elapsed = self.level.duration
            self._test_set_action("skip -> finish")
            return
        self.elapsed = self.level.timeline[self.timeline_idx][0]
        if self.timeline_idx < self._test_waves_end:
            wave_n = self.timeline_idx + 1
            self._test_set_action(f"skip -> wave {wave_n}/6")
        else:
            boss_n = self.timeline_idx - self._test_waves_end + 1
            self._test_set_action(f"skip -> boss {boss_n}/10")

    def _test_skip_chapter(self):
        """SELECT + R1: coarse jump — parade -> all enemy waves -> boss 1
        -> boss 2 -> ... -> boss 10 -> outro. `timeline_idx` is the index
        of the NEXT entry that hasn't fired yet, so we just advance
        `elapsed` to it; the regular timeline loop in _update then spawns
        that entry on the next tick."""
        self._test_clear_field()
        waves_end = self._test_waves_end       # 6
        bosses_end = self._test_bosses_end     # 16
        if not self._test_parade_done:
            self._test_parade_done = True
            self.player.cinematic = False
            self.player.cinematic_scale = 1.0
            self.player.y = PLAY_H - 60
            self.player.rect.center = (int(self.player.x), int(self.player.y))
            self.timeline_idx = 0
            self.elapsed = self.level.timeline[0][0]
            self._test_set_action("skip -> enemy waves")
            return
        if self.timeline_idx < waves_end:
            # In the waves chapter -> jump straight to boss 1
            self.timeline_idx = waves_end
            self.elapsed = self.level.timeline[waves_end][0]
            self._test_set_action("skip -> boss 1/10")
            return
        if self.timeline_idx < bosses_end:
            # Just fire the next boss in the timeline. Do NOT increment
            # timeline_idx here — it already points at the unspawned entry.
            self.elapsed = self.level.timeline[self.timeline_idx][0]
            boss_n = self.timeline_idx - waves_end + 1
            self._test_set_action(f"skip -> boss {boss_n}/10")
            return
        # All bosses fired -> finish the mission
        self.elapsed = self.level.duration
        self._test_set_action("skip -> finish")

    def _test_reset_level(self):
        """L3 (left stick click): restart the whole test mission. Wired via
        a custom outcome that the App-side wrapper turns back into ('play',
        make_test_level())."""
        self.outcome = "test_restart"
        self._test_set_action("reset level")

    def _update_test_parade(self, dt):
        """Advance the per-station takeoff/land cinematic. Each station is
        shown twice: once with the player launching from it, once with the
        player docking at it. After all 10 stations the regular timeline
        starts."""
        self._test_parade_t -= dt
        sub_duration = self._test_parade_sub_duration
        p = clamp(1.0 - max(0.0, self._test_parade_t) / sub_duration, 0, 1)
        if self._test_parade_sub == "takeoff":
            eased = 1.0 - (1.0 - p) ** 3
            self.player.y = lerp(PLAY_H - 30, PLAY_H - 60, eased)
            self.player.x = lerp(self.player.x, PLAY_W // 2, eased * 0.5)
            self.player.cinematic_scale = lerp(0.35, 1.0, eased)
        else:
            eased = p * p
            self.player.y = lerp(PLAY_H - 60, 90, eased)
            self.player.x = lerp(self.player.x, PLAY_W // 2, eased * 0.6)
            self.player.cinematic_scale = lerp(1.0, 0.25, eased)
        self.player.rect.center = (int(self.player.x), int(self.player.y))
        self.player.thrust += dt * 80
        self.player.tilt = 0.0
        self.player.cinematic = True
        self.stars.update(dt * 1.6)
        if ENABLE_NEBULA:
            self.nebula.update(dt)
        self.bg_ribbon.update(dt)

        if self._test_parade_t <= 0:
            if self._test_parade_sub == "takeoff":
                # Same station now becomes the destination — player docks.
                self._test_parade_sub = "landing"
                self._test_parade_t = sub_duration
            else:
                # Done with this station; on to the next one's takeoff.
                self._test_parade_idx += 1
                if self._test_parade_idx >= 10:
                    self._test_parade_done = True
                    self.player.cinematic = False
                    self.player.cinematic_scale = 1.0
                    self.player.y = PLAY_H - 60
                    self.player.rect.center = (int(self.player.x),
                                               int(self.player.y))
                    # Kick the timeline forward so the first enemy wave
                    # fires immediately after the parade ends.
                    if self.level.timeline:
                        self.elapsed = self.level.timeline[0][0]
                else:
                    self._test_parade_sub = "takeoff"
                    self._test_parade_t = sub_duration
                    self.player.y = float(PLAY_H - 30)
                    self.player.rect.center = (int(self.player.x),
                                               int(self.player.y))

    def _draw_test_parade_stations(self, surf):
        """Blit the current station for the active sub-phase. Mirrors the
        regular intro/outro scroll: takeoff has the station sliding down out
        of frame from the bottom, landing has it sliding in from the top."""
        idx = self._test_parade_idx
        sub_duration = self._test_parade_sub_duration
        p = clamp(1.0 - max(0.0, self._test_parade_t) / sub_duration, 0, 1)
        if self._test_parade_sub == "takeoff":
            img = self._test_stations_start.get(idx)
            if img is not None:
                sh = img.get_height()
                sx = (PLAY_W - img.get_width()) // 2
                sy = int(PLAY_H - sh + p * (sh + 20))
                surf.blit(img, (sx, sy))
        else:
            img = self._test_stations_end.get(idx)
            if img is not None:
                sh = img.get_height()
                sx = (PLAY_W - img.get_width()) // 2
                entry = min(p / 0.5, 1.0)
                sy = int(-sh + entry * (sh + 20))
                surf.blit(img, (sx, sy))
        line1 = self.app.fonts["small"].render(
            f"STATION {idx + 1}/10 - {SECTOR_NAMES[idx].upper()}",
            False, WHITE)
        line2 = self.app.fonts["tiny"].render(
            f"({self._test_parade_sub})", False, (160, 200, 240))
        surf.blit(line1, line1.get_rect(center=(PLAY_W // 2, PLAY_H - 56)))
        surf.blit(line2, line2.get_rect(center=(PLAY_W // 2, PLAY_H - 36)))

    # Panel margins — top-left corner placement, with enough padding to
    # keep the rendered glyphs from kissing the playfield edges.
    _PANEL_X = 4
    _PANEL_Y = 4
    _PANEL_PAD = 5

    def _draw_test_banner(self, surf):
        """Top-left overlay. R3 cycles between four modes:
          0 = debug banner (loadout + controls + raw input diagnostics) —
              only available in the test mission
          1 = performance summary (fps + frame ms + live object counts)
          2 = performance detail (per-stage ms breakdown of every update +
              draw phase, with peak spikes — two-column panel)
          3 = off (nothing rendered)"""
        mode = self._test_overlay_mode
        if mode == 3:
            return
        if mode == 2:
            self._draw_perf_detail_panel(surf)
            return
        if mode == 0 and not self.is_test:
            return  # debug banner needs test-mode state to render
        font = self.app.fonts["small"]   # 2x of the old tiny font
        if mode == 1:
            lines = self._test_perf_lines()
        else:
            lines = self._test_debug_lines()
        if not lines:
            return
        # Render then clamp height: never draw past the bottom of the
        # playfield. If we'd overflow we silently drop tail lines.
        line_h = font.get_height() + 2
        pad = self._PANEL_PAD
        max_lines = max(1, (PLAY_H - self._PANEL_Y * 2) // line_h - 1)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
        rendered = [font.render(t, False, c) for t, c in lines]
        max_w = max(r.get_width() for r in rendered)
        bg_w = min(PLAY_W - self._PANEL_X * 2, max_w + pad * 2)
        bg_h = line_h * len(rendered) + pad * 2
        bg = pygame.Surface((bg_w, bg_h), pygame.SRCALPHA)
        bg.fill((0, 0, 0, 180))
        surf.blit(bg, (self._PANEL_X, self._PANEL_Y))
        y = self._PANEL_Y + pad
        for r in rendered:
            surf.blit(r, (self._PANEL_X + pad, y))
            y += line_h

    # Sections we instrument inside _update and _draw, grouped for the
    # detail panel. Keep the order stable so the panel doesn't reshuffle
    # frame to frame.
    _PERF_GROUPS = (
        ("UPDATE", (
            ("upd.spawn",        "spawn"),
            ("upd.player",       "player"),
            ("upd.bullets",      "bullets"),
            ("upd.enemies",      "enemies"),
            ("upd.lasers",       "lasers"),
            ("upd.pickups",      "pickups"),
            ("upd.particles",    "particles"),
            ("col.bullet_enemy", "col b>e"),
            ("col.bullet_player","col b>p"),
            ("col.enemy_player", "col e>p"),
            ("col.pickup",       "col pick"),
            ("upd.cleanup",      "cleanup"),
        )),
        ("DRAW", (
            ("draw.bg_ribbon",   "bg ribbon"),
            ("draw.nebula",      "nebula"),
            ("draw.stars",       "stars"),
            ("draw.pickups",     "pickups"),
            ("draw.bullets",     "bullets"),
            ("draw.lasers",      "lasers"),
            ("draw.enemies",     "enemies"),
            ("draw.particles",   "particles"),
            ("draw.player",      "player"),
            ("draw.overlay",     "perf hud"),
            ("draw.blit_screen", "screen blit"),
            ("draw.hud",         "game hud"),
        )),
        ("FRAME", (
            ("app.tick",         "tick (idle)"),
            ("app.events",       "events"),
            ("app.state",        "state.run"),
            ("app.flip",         "display flip"),
            ("frame",            "TOTAL"),
        )),
    )

    def _draw_perf_detail_panel(self, surf):
        """Per-stage ms breakdown of every instrumented section. Laid out
        in TWO columns so the full update + draw + frame trees fit inside
        the 480x480 playfield at the 2x font size (single column would
        overflow vertically). Highlights anything eating >10% of the frame
        budget so the bottleneck pops visually."""
        perf = self.app.perf
        font = self.app.fonts["small"]   # 2x of the old tiny font
        line_h = font.get_height() + 2
        fps = self.app.clock.get_fps()
        budget_ms = 1000.0 / FPS
        total_ms = perf.ms("frame")

        if fps < 30: fps_col = RED
        elif fps < 50: fps_col = ORANGE
        else: fps_col = (120, 220, 140)

        def fmt(key, label):
            ms_v = perf.ms(key)
            if ms_v > budget_ms * 0.20:
                col = RED
            elif ms_v > budget_ms * 0.10:
                col = ORANGE
            elif ms_v > 0.05:
                col = WHITE
            else:
                col = DIM
            # Compact format — label + smoothed ms. The BitmapFont is
            # monospace so columns line up naturally. Per-row peak was
            # dropped so two columns fit inside the 480-px playfield at
            # the 2x font; the frame-wide peak is shown in the header.
            return (f"{label:<12}{ms_v:5.2f}", col)

        def section(group_name, entries):
            lines = []
            total = sum(perf.ms(k) for k, _ in entries if k != "frame")
            if group_name == "FRAME":
                lines.append(("-- FRAME --", YELLOW))
            else:
                lines.append((f"-- {group_name} {total:5.2f} --", YELLOW))
            for key, label in entries:
                lines.append(fmt(key, label))
            return lines

        # Header lines apply to the left column.
        header = [
            (f"PERF fps {fps:5.1f}", fps_col),
            (f"bud {budget_ms:4.1f}  tot {total_ms:5.2f}", WHITE),
            (f"frame peak {perf.peak_ms('frame'):5.2f}", DIM),
            (f"b{len(self.bullets):>3d} e{len(self.enemies):>3d} "
             f"p{len(self.particles):>3d}", DIM),
        ]

        upd_group  = self._PERF_GROUPS[0]
        drw_group  = self._PERF_GROUPS[1]
        frm_group  = self._PERF_GROUPS[2]

        col1 = header + section(*upd_group)
        col2 = section(*drw_group) + section(*frm_group)
        col2.append(("R3 cycle", DIM))

        pad = self._PANEL_PAD
        # Clamp each column's line count so we never run past the playfield
        # bottom, no matter how many sections accumulate later.
        max_lines = max(1, (PLAY_H - self._PANEL_Y * 2 - pad * 2) // line_h)
        col1 = col1[:max_lines]
        col2 = col2[:max_lines]

        rendered1 = [font.render(t, False, c) for t, c in col1]
        rendered2 = [font.render(t, False, c) for t, c in col2]
        col1_w = max(r.get_width() for r in rendered1)
        col2_w = max(r.get_width() for r in rendered2)
        gap = 12
        bg_w = min(PLAY_W - self._PANEL_X * 2,
                   col1_w + gap + col2_w + pad * 2)
        bg_h = line_h * max(len(rendered1), len(rendered2)) + pad * 2
        bg = pygame.Surface((bg_w, bg_h), pygame.SRCALPHA)
        bg.fill((0, 0, 0, 180))
        surf.blit(bg, (self._PANEL_X, self._PANEL_Y))

        x1 = self._PANEL_X + pad
        x2 = x1 + col1_w + gap
        # Drop the second column entirely if it would land off-screen — the
        # left column still gives the most-watched numbers.
        draw_col2 = x2 + col2_w <= self._PANEL_X + bg_w
        y = self._PANEL_Y + pad
        for r in rendered1:
            surf.blit(r, (x1, y))
            y += line_h
        if draw_col2:
            y = self._PANEL_Y + pad
            for r in rendered2:
                surf.blit(r, (x2, y))
                y += line_h

    def _test_debug_lines(self):
        """Loadout, control legend, raw input diagnostics. Lines are kept
        short enough to fit in the panel at the scale-2 font (~25 chars per
        line for the 480-px playfield)."""
        mt = self.player.loadout.main_type
        mlvl = getattr(self.player.loadout, f"main_{mt}", 0)
        st = self.player.loadout.side_type
        slvl = (getattr(self.player.loadout, f"side_{st}", 0)
                if st != "none" else 0)
        ab = self.player.loadout.ability
        lines = [
            ("TEST MODE", YELLOW),
            (f"main: {mt} L{mlvl}", WHITE),
            (f"side: {st}" + (f" L{slvl}" if st != "none" else ""), WHITE),
            (f"ability: {ab}", WHITE),
            ("L1 main  L2 side", DIM),
            ("R2 ability  R3 overlay", DIM),
            ("R1 step  SEL+R1 chapter", DIM),
            ("L3 reset", DIM),
            ("RStick u/d:main l/r:side", DIM),
        ]
        if self._test_action_t > 0:
            lines.append(("> " + self._test_action_msg, CYAN))
        if self._test_diag_axes:
            # Split axes into rows of 4 so each line stays narrow.
            ax = self._test_diag_axes
            for i in range(0, len(ax), 4):
                chunk = ax[i:i + 4]
                lines.append(
                    (f"ax{i}: " + " ".join(f"{v:+.2f}" for v in chunk), DIM))
        if self._test_diag_n_btn:
            lines.append((f"#btns: {self._test_diag_n_btn}", DIM))
        if (self._test_last_btn is not None
                and pygame.time.get_ticks() - self._test_last_btn_t < 3000):
            lines.append((f"last btn idx: {self._test_last_btn}", ORANGE))
        return lines

    def _test_perf_lines(self):
        """Live frame stats + object counts so you can see whether a wave
        or a boss tanks performance. Also summarizes the top-level update/
        draw/flip split from the PerfMonitor — for the per-stage breakdown
        cycle R3 once more to the perf-detail mode."""
        clock = self.app.clock
        perf = self.app.perf
        fps = clock.get_fps()
        ms = (1000.0 / fps) if fps > 0.5 else 0.0
        # Colour FPS red if it falls under 30, yellow under 50, green
        # otherwise — quick visual cue while playing.
        if fps < 30: fps_col = RED
        elif fps < 50: fps_col = ORANGE
        else: fps_col = (120, 220, 140)
        # Sum each phase's instrumented children for the headline numbers.
        upd_keys = ("upd.spawn", "upd.player", "upd.bullets", "upd.enemies",
                    "upd.lasers", "upd.pickups", "upd.particles",
                    "col.bullet_enemy", "col.bullet_player",
                    "col.enemy_player", "col.pickup", "upd.cleanup")
        drw_keys = ("draw.bg_ribbon", "draw.nebula", "draw.stars",
                    "draw.pickups", "draw.bullets", "draw.lasers",
                    "draw.enemies", "draw.particles", "draw.player",
                    "draw.overlay", "draw.blit_screen", "draw.hud")
        upd_ms = sum(perf.ms(k) for k in upd_keys)
        drw_ms = sum(perf.ms(k) for k in drw_keys)
        flip_ms = perf.ms("app.flip")
        tick_ms = perf.ms("app.tick")
        frame_ms = perf.ms("frame")
        return [
            ("PERF", YELLOW),
            (f"fps: {fps:5.1f}   {ms:4.1f} ms", fps_col),
            (f"frame  {frame_ms:5.2f} ms", WHITE),
            (f"update {upd_ms:5.2f}", WHITE),
            (f"draw   {drw_ms:5.2f}", WHITE),
            (f"flip   {flip_ms:5.2f}", WHITE),
            (f"idle   {tick_ms:5.2f}", DIM),
            (f"bullets  {len(self.bullets):>4d}", WHITE),
            (f"enemies  {len(self.enemies):>4d}", WHITE),
            (f"particles{len(self.particles):>4d}", WHITE),
            (f"sparks   {len(self.sparks):>4d}", WHITE),
            (f"explosns {len(self.explosions):>4d}", WHITE),
            (f"pickups  {len(self.pickups):>4d}", WHITE),
            (f"lasers   {len(self.lasers):>4d}", WHITE),
            ("R3: cycle overlay", DIM),
        ]


# =============================================================================
# MISSION MAP SCREEN
# =============================================================================

class MapScreen:
    """100 levels across 10 sectors. L1/R1 (or Q/E) page between sectors; D-pad picks within."""

    def __init__(self, app):
        self.app = app
        self.stars = ParallaxStars(SCREEN_W, SCREEN_H, counts=(70, 50, 30))
        self.t = 0
        self.outcome = None
        n = self._next_to_play_n()
        self.sector_idx = (n - 1) // 10
        self.cursor = f"L{n:03d}"
        self.bg_ribbon = BackgroundRibbon(SECTOR_RIBBONS[self.sector_idx],
                                          width=SCREEN_W)
        # Static backdrop on the map screen — no vertical drift.
        self.bg_ribbon.speed = 0
        # Rebuild at the source's native aspect ratio, mirror-tiled three
        # copies wide so the bg looks like the AI's actual art instead of a
        # stretched fit-to-screen blob.
        self.bg_ribbon.remake_native_aspect_h(mirror_n=3)
        self._last_sector = self.sector_idx
        self._flash_msg = None
        self._flash_t = 0.0

    def _next_to_play_n(self):
        """Lowest unlocked level number that hasn't been completed yet.
        Falls back to the lowest unlocked overall if everything's cleared."""
        save = self.app.save
        unlocked = []
        for k in save.unlocked:
            if k.startswith("L") and k[1:].isdigit():
                unlocked.append((int(k[1:]), k))
        if not unlocked:
            return 1
        unlocked.sort()
        for n, k in unlocked:
            if k not in save.completed:
                return n
        return unlocked[0][0]

    def _max_unlocked_n(self):
        nums = []
        for k in self.app.save.unlocked:
            if k.startswith("L") and k[1:].isdigit():
                nums.append(int(k[1:]))
        return max(nums) if nums else 1

    def _max_sector(self):
        return (self._max_unlocked_n() - 1) // 10

    def _sector_keys(self):
        start_n = self.sector_idx * 10 + 1
        return [f"L{n:03d}" for n in range(start_n, start_n + 10)]

    def _default_cursor(self):
        """Lowest unlocked-and-not-completed level in the current sector.
        Falls back to the lowest unlocked level here, or the sector's
        first slot if nothing's unlocked yet."""
        save = self.app.save
        keys = self._sector_keys()
        for k in keys:
            if k in save.unlocked and k not in save.completed:
                return k
        for k in keys:
            if k in save.unlocked:
                return k
        return keys[0]

    def run(self, events, controls):
        dt = 1.0 / FPS
        self.t += dt
        self.stars.update(dt)
        # Bg_ribbon stays static here — no .update() so it doesn't scroll.

        # Holding either L2 or R2 unlocks free sector navigation — this is
        # the hidden "browse bot runs in areas you haven't unlocked yet"
        # affordance. Without the modifier the normal max-sector cap applies.
        max_sec = 9 if (controls.l2_held or controls.r2_held) else self._max_sector()

        # Sector pagination
        sector_changed = False
        for ev in events:
            if ev.type == pygame.JOYBUTTONDOWN:
                if ev.button == JOY_L1 and self.sector_idx > 0:
                    self.sector_idx -= 1; sector_changed = True
                if ev.button == JOY_R1 and self.sector_idx < max_sec:
                    self.sector_idx += 1; sector_changed = True
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_q and self.sector_idx > 0:
                    self.sector_idx -= 1; sector_changed = True
                if ev.key == pygame.K_e and self.sector_idx < max_sec:
                    self.sector_idx += 1; sector_changed = True
        if sector_changed:
            self.cursor = self._default_cursor()
            self.app.sounds["menu"].play()
            self.bg_ribbon = BackgroundRibbon(SECTOR_RIBBONS[self.sector_idx],
                                              width=SCREEN_W)
            self.bg_ribbon.speed = 0
            self.bg_ribbon.remake_native_aspect_h(mirror_n=3)

        # Dev shortcut: unlock every level. Keyboard Ctrl+U, joystick SELECT+X.
        for ev in events:
            if (ev.type == pygame.KEYDOWN and ev.key == pygame.K_u
                    and (pygame.key.get_mods() & pygame.KMOD_CTRL)):
                self._unlock_all()
            if (ev.type == pygame.JOYBUTTONDOWN and ev.button == JOY_X
                    and controls.select):
                self._unlock_all()

        # D-pad within sector — but while L2 or R2 is held, the d-pad belongs
        # to the replay gesture (so the cursor stays put while you trigger
        # the shortcut for the currently-cursored level).
        if not (controls.l2_held or controls.r2_held):
            if any(ev.type in (pygame.KEYDOWN, pygame.JOYHATMOTION) for ev in events):
                self._handle_nav(events)

        if self._flash_t > 0:
            self._flash_t -= dt

        if controls.confirm_pressed:
            if self.cursor in self.app.save.unlocked:
                self.app.save.current_node = self.cursor
                self.app.save.save()
                level = self.app.levels[self.cursor]
                self.outcome = ("play", level)
            else:
                self.app.sounds["deny"].play()

        if controls.cancel_pressed:
            self.app.sounds["menu"].play()
            self.outcome = ("shop", None)

        # START → bail back to the title screen so the player can switch
        # profile or restart with a fresh slot without quitting the app.
        if controls.start_pressed:
            self.app.save.save()
            self.app.sounds["menu"].play()
            self.outcome = ("title", None)

        # Hidden bot-replay shortcut: same gesture as on the title screen,
        # but plays back just the currently-cursored level. If the replay
        # file doesn't have a block for this level (because the bot didn't
        # reach it), say so on screen rather than silently no-op'ing.
        prof = _gesture_to_profile(controls)
        if prof is not None:
            path = _find_replay_path(prof)
            if path is None:
                self._flash_msg = f"NO REPLAY: {prof}"
                self._flash_t = 1.6
                self.app.sounds["deny"].play()
            elif not _replay_has_level(path, self.cursor):
                self._flash_msg = f"{prof}: didn't reach {self.cursor}"
                self._flash_t = 1.8
                self.app.sounds["deny"].play()
            else:
                self.outcome = ("replay_level", (prof, self.cursor))

        self._draw(controls)
        return self.outcome

    def _unlock_all(self):
        """Dev affordance: unlock all 100 levels for testing."""
        all_keys = [f"L{n:03d}" for n in range(1, 101)]
        before = len(self.app.save.unlocked)
        self.app.save.unlocked = all_keys
        self.app.save.save()
        self._flash_msg = f"DEV: UNLOCKED ALL LEVELS  (+{100 - before})"
        self._flash_t = 2.5
        self.app.sounds["confirm"].play()

    def _handle_nav(self, events):
        keys = self._sector_keys()
        if self.cursor not in keys:
            self.cursor = keys[0]
        cur_pos = MAP_GRAPH[self.cursor].pos
        for ev in events:
            dx = dy = 0
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_LEFT:  dx = -1
                if ev.key == pygame.K_RIGHT: dx = 1
                if ev.key == pygame.K_UP:    dy = -1
                if ev.key == pygame.K_DOWN:  dy = 1
            if ev.type == pygame.JOYHATMOTION:
                hx, hy = ev.value
                dx, dy = hx, -hy
            if dx == 0 and dy == 0:
                continue
            best, best_score = None, 1e9
            for k in keys:
                if k == self.cursor:
                    continue
                p = MAP_GRAPH[k].pos
                vx = p[0] - cur_pos[0]
                vy = p[1] - cur_pos[1]
                if vx * dx + vy * dy <= 0:
                    continue
                dist = abs(vx) + abs(vy)
                if dist < best_score:
                    best_score = dist
                    best = k
            if best:
                self.cursor = best
                self.app.sounds["menu"].play()
                break

    def _draw(self, controls):
        screen = self.app.screen
        save = self.app.save
        fonts = self.app.fonts

        # Full-screen sector-themed ribbon under a faint colour wash, with
        # the persistent parallax stars on top. The ribbon is constructed
        # at SCREEN_W and its scroll speed is zero — it stays static here
        # so the map screen reads as a quiet menu instead of a busy field.
        screen.fill(BLACK)
        self.bg_ribbon.draw(screen)
        tint = SECTOR_NEBULAS[self.sector_idx]
        wash = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
        wash.fill((tint[0], tint[1], tint[2], 24))
        screen.blit(wash, (0, 0))
        self.stars.draw(screen)
        screen.blit(self.app.vignette, (0, 0))

        sector_palette = STATION_PALETTES[self.sector_idx]
        base, accent, dark = sector_palette
        max_sector = self._max_sector()
        progress_n = sum(1 for k in save.completed if k.startswith("L"))

        # ---- Top bar: sector tabs (10 dots) and L/R hints ----
        tabs_y = 14
        spacing = 28
        tabs_total = (10 - 1) * spacing
        tabs_x0 = (PLAY_W - tabs_total) // 2
        for i in range(10):
            x = tabs_x0 + i * spacing
            sector_done = all(f"L{(i * 10) + slot + 1:03d}" in save.completed for slot in range(10))
            if i == self.sector_idx:
                pygame.draw.circle(screen, CYAN, (x, tabs_y), 6)
                pygame.draw.circle(screen, WHITE, (x, tabs_y), 7, 1)
            elif sector_done:
                pygame.draw.circle(screen, GREEN, (x, tabs_y), 5)
            elif i <= max_sector:
                pygame.draw.circle(screen, accent, (x, tabs_y), 4, 1)
            else:
                pygame.draw.circle(screen, (60, 60, 80), (x, tabs_y), 3)

        # Editable text bits go through the layout system so the user can
        # tweak position/font/color/text via the layout editor. The dynamic
        # state (sector name, progress %) is interpolated into the spec's
        # text templates via map_vars.
        map_vars = {
            "sector_name": SECTOR_NAMES[self.sector_idx],
            "sector_n": f"{self.sector_idx + 1:02d}",
            "progress": f"{progress_n:02d}",
            "has_prev": self.sector_idx > 0,
            "has_next": self.sector_idx < max_sector,
            "all_clear": progress_n >= 100,
            **button_label_vars(),
        }
        for eid in ("nav_hint_l", "nav_hint_r"):
            el = get_element("map", eid, **map_vars)
            if el is not None:
                _layout_draw_item(screen, el, fonts, self.app.assets, map_vars)

        # ---- Sector header banner ----
        # Panel chrome stays in code; the text inside is element-driven.
        _panel(screen, 60, 32, PLAY_W - 120, 50)
        for eid in ("sector_title", "sector_subtitle", "back_hint"):
            el = get_element("map", eid, **map_vars)
            if el is not None:
                _layout_draw_item(screen, el, fonts, self.app.assets, map_vars)

        # ---- Node graph ----
        keys = self._sector_keys()

        # Edges first
        for i in range(len(keys) - 1):
            a_pos = MAP_GRAPH[keys[i]].pos
            b_pos = MAP_GRAPH[keys[i + 1]].pos
            a_done = keys[i] in save.completed
            b_avail = keys[i + 1] in save.unlocked
            _draw_map_edge(screen, a_pos, b_pos, a_done, b_avail, self.t, accent)

        # Nodes
        for k in keys:
            node = MAP_GRAPH[k]
            is_boss = self.app.levels[k].has_boss
            done = k in save.completed
            avail = k in save.unlocked
            cursor = (k == self.cursor)
            _draw_map_node(screen, node.pos[0], node.pos[1], sector_palette,
                           is_boss=is_boss, done=done, avail=avail,
                           cursor=cursor, t=self.t,
                           label_n=int(k[1:]), fonts=fonts)

        # ---- Mission dossier card at the bottom of the playfield ----
        card_y = SCREEN_H - 92
        card_h = 84
        _panel(screen, 14, card_y, PLAY_W - 28, card_h, "MISSION DOSSIER", fonts)
        cl = self.app.levels[self.cursor]
        screen.blit(fonts["big"].render(self.cursor, False, accent), (28, card_y + 14))
        screen.blit(fonts["small"].render(cl.name.upper(), False, WHITE),
                    (28, card_y + 42))
        type_label = "BOSS BATTLE" if cl.has_boss else "STANDARD"
        tl = fonts["small"].render(type_label, False, RED if cl.has_boss else DIM)
        screen.blit(tl, (28, card_y + 62))
        # Right side: status badge, difficulty stars
        rx = PLAY_W - 32
        if self.cursor in save.completed:
            status = "CLEARED"; col = GREEN
        elif self.cursor in save.unlocked:
            status = "READY"; col = CYAN
        else:
            status = "LOCKED"; col = DIM
        st = fonts["small"].render(status, False, col)
        screen.blit(st, (rx - st.get_width(), card_y + 14))
        stars_filled = max(1, min(5, int(round((cl.difficulty - 1.0) / 0.6 + 1))))
        sx = rx - 5 * 12
        for i in range(5):
            c = ORANGE if i < stars_filled else (50, 50, 70)
            cx, cy = sx + i * 12, card_y + 44
            pygame.draw.polygon(screen, c,
                                [(cx, cy - 3), (cx + 3, cy), (cx, cy + 3), (cx - 3, cy)])
        diff_label = fonts["small"].render(f"x{cl.difficulty:.2f}", False, DIM)
        screen.blit(diff_label, (rx - diff_label.get_width(), card_y + 60))

        # Right-side strip: header / STATUS / LOADOUT / CONTROL all live
        # in _build_map_panel_spec(); rendering the root container paints
        # the strip + all dynamic fields in one pass.
        lo = save.loadout
        mtier = getattr(save, f"unlocked_tier_{lo.main_type}", 5)
        shtier = getattr(save, "unlocked_tier_shield", 5)
        map_panel_vars = {
            "credits": save.credits,
            "high_score": save.high_score,
            "progress_n": progress_n,
            "progress_ratio": progress_n / 100.0,
            "main_name": MAIN_WEAPON_NAMES[lo.main_type].upper(),
            "main_lvl": lo.main_level(),
            "main_max": MAIN_WEAPON_MAX,
            "main_visible_tiers": mtier,
            "main_visible_max": mtier * 4,
            "shield_lvl": lo.shield,
            "shield_max": MAX_LEVELS["shield"],
            "shield_visible_tiers": shtier,
            "shield_visible_max": shtier,
            **button_label_vars(),
        }
        map_root = get_element("map", "map_root", **map_panel_vars)
        if map_root is not None:
            _layout_draw_item(screen, map_root, fonts, self.app.assets,
                              map_panel_vars)

        # End-of-game banner — element rendering handles visibility via
        # `visible_when: all_clear`, so the in-line check moved to map_vars.
        el = get_element("map", "all_clear", **map_vars)
        if el is not None:
            _layout_draw_item(screen, el, fonts, self.app.assets, map_vars)

        if self._flash_t > 0 and self._flash_msg:
            a = clamp(self._flash_t / 2.5, 0.0, 1.0)
            box_w = 360
            box_h = 36
            bx = (PLAY_W - box_w) // 2
            by = 96
            overlay = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
            overlay.fill((20, 28, 50, int(220 * a)))
            screen.blit(overlay, (bx, by))
            pygame.draw.rect(screen, (160, 200, 240, int(255 * a)),
                             (bx, by, box_w, box_h), 1)
            txt = fonts["small"].render(self._flash_msg, False, ORANGE)
            screen.blit(txt, txt.get_rect(center=(PLAY_W // 2, by + box_h // 2)))

        draw_layout_overlay(screen, "map", fonts, self.app.assets)


def _draw_map_node(surf, x, y, palette, is_boss, done, avail, cursor, t, label_n, fonts):
    base, accent, dark = palette
    if is_boss:
        r_outer = 20
        r_mid = 14
        r_inner = 6
        if done:
            fill = (60, 130, 80); ring_col = GREEN
        elif avail:
            fill = base; ring_col = accent
        else:
            fill = (40, 40, 56); ring_col = (90, 90, 110)
        pygame.draw.circle(surf, dark if avail or done else (30, 30, 44), (x, y), r_outer)
        pygame.draw.circle(surf, ring_col, (x, y), r_outer, 2)
        # antenna marks (4 directions)
        if avail or done:
            for ang_deg in (0, 90, 180, 270):
                ang = math.radians(ang_deg + (t * 30 if cursor else 0))
                px = int(x + math.cos(ang) * (r_outer + 4))
                py = int(y + math.sin(ang) * (r_outer + 4))
                pygame.draw.rect(surf, accent, (px - 1, py - 1, 3, 3))
        pygame.draw.circle(surf, fill, (x, y), r_mid)
        pygame.draw.circle(surf, ring_col, (x, y), r_mid, 1)
        pygame.draw.circle(surf, accent if avail or done else (80, 80, 100), (x, y), r_inner)
        label = "B"
        lc = BLACK if avail or done else (140, 140, 160)
    else:
        r = 13
        if done:
            fill = (60, 130, 80); ring_col = GREEN
        elif avail:
            fill = base; ring_col = accent
        else:
            fill = (44, 44, 60); ring_col = (90, 90, 110)
        pygame.draw.circle(surf, fill, (x, y), r)
        pygame.draw.circle(surf, ring_col, (x, y), r, 2)
        if avail or done:
            pygame.draw.circle(surf, accent, (x, y), 3)
        label = f"{label_n}"
        lc = BLACK if avail or done else (140, 140, 160)

    if cursor:
        radius = (22 if is_boss else 16) + int(math.sin(t * 6) * 2)
        pygame.draw.circle(surf, YELLOW, (x, y), radius, 2)

    if done:
        # checkmark badge
        pygame.draw.line(surf, WHITE, (x - 4, y), (x - 1, y + 3), 2)
        pygame.draw.line(surf, WHITE, (x - 1, y + 3), (x + 4, y - 3), 2)
    else:
        ntxt = fonts["tiny"].render(label, False, lc)
        surf.blit(ntxt, ntxt.get_rect(center=(x, y - (2 if is_boss else 0))))

    # mini caption below
    cap = fonts["tiny"].render(f"L{label_n}", False, DIM if not (avail or done) else WHITE)
    surf.blit(cap, cap.get_rect(center=(x, y + (28 if is_boss else 22))))


def _draw_map_edge(surf, a, b, a_done, b_avail, t, accent):
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return
    nx, ny = dx / length, dy / length

    if a_done:
        pygame.draw.line(surf, GREEN, a, b, 2)
    elif b_avail:
        # solid colored toward next available, with a subtle pulse
        pygame.draw.line(surf, accent, a, b, 2)
    else:
        # locked: dashed faint
        seg, gap = 6, 6
        pos = 0
        while pos < length:
            x1 = a[0] + nx * pos
            y1 = a[1] + ny * pos
            ne = min(pos + seg, length)
            x2 = a[0] + nx * ne
            y2 = a[1] + ny * ne
            pygame.draw.line(surf, (70, 80, 110), (int(x1), int(y1)), (int(x2), int(y2)), 1)
            pos += seg + gap
        return

    # Travelling chevron only on reachable edges
    travel_t = (t * 0.6) % 1.0
    cx = a[0] + dx * travel_t
    cy = a[1] + dy * travel_t
    angle = math.atan2(dy, dx)
    size = 5
    tip = (cx + math.cos(angle) * size, cy + math.sin(angle) * size)
    left = (cx + math.cos(angle + 2.5) * size, cy + math.sin(angle + 2.5) * size)
    right = (cx + math.cos(angle - 2.5) * size, cy + math.sin(angle - 2.5) * size)
    pygame.draw.polygon(surf, WHITE, [
        (int(tip[0]), int(tip[1])),
        (int(left[0]), int(left[1])),
        (int(right[0]), int(right[1])),
    ])


# =============================================================================
# SHOP SCREEN
# =============================================================================

SHOP_ITEMS = [
    ("main_pulse",  "Pulse Cannon"),
    ("main_spread", "Spread Shot"),
    ("main_vulcan", "Vulcan Gun"),
    ("side_missile", "Heatseekers"),
    ("side_drone",   "Drone Cells"),
    ("shield", "Shield Generator"),
    ("engine", "Engine"),
    ("bomb",   "Extra Bomb"),
    ("ability_screen_clear", "Ability: Pulse Bomb"),
    ("ability_shield_burst", "Ability: Shield Burst"),
    ("ability_mega_laser",   "Ability: Mega Laser"),
]


def _parse_weapon_key(key):
    """Split a SHOP_ITEMS key like 'main_pulse' / 'side_drone' into
    (slot, weapon_type). Returns (None, None) if not a weapon row."""
    if key.startswith("main_"):
        return ("main", key[len("main_"):])
    if key.startswith("side_"):
        return ("side", key[len("side_"):])
    return (None, None)


class ShopScreen:
    # Visual constants — uniform across every upgrade row's bar so a tier
    # cell looks the same width whether it belongs to pulse, shield, or
    # engine. Main weapons subdivide each tier into 4 sub-cells.
    TIER_PX = 32          # width of one tier segment
    TIER_GAP = 1          # 1px gap between tier segments
    BAR_H = 14

    REVEAL_PER_UNLOCK_SEC = 0.65   # duration per cascade item
    REVEAL_FLASH_COLOR = (255, 240, 140)

    def __init__(self, app, pending_unlocks=None):
        self.app = app
        self.cursor = 0
        self.outcome = None
        self.flash_text = None
        self.flash_t = 0
        # Reveal animation state. `pending_unlocks` is the list of
        # (category, new_tier) tuples produced by _apply_boss_unlocks().
        # We pop them one-by-one and animate each.
        self.pending_unlocks = list(pending_unlocks or [])
        self.current_unlock = None     # (category, new_tier)
        self.current_unlock_t = 0.0
        self._start_next_unlock()

    def _start_next_unlock(self):
        if self.pending_unlocks:
            self.current_unlock = self.pending_unlocks.pop(0)
            self.current_unlock_t = 0.0
            try:
                self.app.sounds["confirm"].play()
            except Exception:
                pass
        else:
            self.current_unlock = None

    def _is_revealing(self):
        return self.current_unlock is not None

    def _skip_reveal(self):
        self.pending_unlocks = []
        self.current_unlock = None

    def _tick_reveal(self, dt):
        if self.current_unlock is None:
            return
        self.current_unlock_t += dt
        if self.current_unlock_t >= self.REVEAL_PER_UNLOCK_SEC:
            self._start_next_unlock()

    def run(self, events, controls):
        dt = 1.0 / FPS
        # Reveal animation blocks shop interaction. Any button press skips.
        if self._is_revealing():
            self._tick_reveal(dt)
            if (controls.confirm_pressed or controls.cancel_pressed
                    or controls.bomb_pressed or controls.ability_pressed):
                self._skip_reveal()
            self._draw()
            return None
        moved = False
        for ev in events:
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_UP:
                    self.cursor = (self.cursor - 1) % len(SHOP_ITEMS); moved = True
                if ev.key == pygame.K_DOWN:
                    self.cursor = (self.cursor + 1) % len(SHOP_ITEMS); moved = True
            if ev.type == pygame.JOYHATMOTION:
                _, hy = ev.value
                if hy > 0:
                    self.cursor = (self.cursor - 1) % len(SHOP_ITEMS); moved = True
                if hy < 0:
                    self.cursor = (self.cursor + 1) % len(SHOP_ITEMS); moved = True
        if moved:
            self.app.sounds["menu"].play()

        if controls.confirm_pressed:
            self._buy()
        if controls.cancel_pressed:
            self.app.save.save()
            self.app.sounds["menu"].play()
            self.outcome = ("map", None)
        # START → back to the title screen (matches MapScreen behaviour).
        if controls.start_pressed:
            self.app.save.save()
            self.app.sounds["menu"].play()
            self.outcome = ("title", None)

        if self.flash_t > 0:
            self.flash_t -= dt
        self._draw()
        if self.outcome:
            return self.outcome
        return None

    def _item_cost(self, key):
        """Returns the credit cost for the action this row offers, or None if
        the row is at MAX or the next purchase would cross into a locked
        tier. Equip actions cost 0."""
        save = self.app.save
        if key == "bomb":
            return BOMB_PRICE
        if key.startswith("ability_"):
            return 0
        slot, wtype = _parse_weapon_key(key)
        if slot == "main":
            # All 3 main weapons are always owned + always equipped (L1/R1
            # hold swaps between them in-flight). The only action a main
            # row offers now is "upgrade".
            lvl = getattr(save.loadout, f"main_{wtype}")
            if lvl >= MAIN_WEAPON_MAX:
                return None
            if _main_tier(lvl + 1) > _unlocked_tier_for(save, key):
                return None
            return MAIN_UPGRADE_COSTS[wtype][lvl]
        if slot == "side":
            lvl = getattr(save.loadout, f"side_{wtype}")
            if lvl == 0:
                return SIDE_BUY_COST
            if save.loadout.side_type != wtype:
                return 0
            if lvl >= SIDE_WEAPON_MAX:
                return None
            # Side: tier == level. Next level must be within unlocked tiers.
            if (lvl + 1) > _unlocked_tier_for(save, key):
                return None
            return SIDE_UPGRADE_COSTS[wtype][lvl]
        # Shield / engine: each level == one tier.
        lvl = getattr(save.loadout, key)
        costs = WEAPON_COSTS[key]
        if lvl >= MAX_LEVELS[key]:
            return None
        if (lvl + 1) > _unlocked_tier_for(save, key):
            return None
        return costs[lvl]

    def _row_action(self, key):
        """Human-readable verb for what B does on this row right now."""
        save = self.app.save
        slot, wtype = _parse_weapon_key(key)
        if slot == "main":
            lvl = getattr(save.loadout, f"main_{wtype}")
            if lvl >= MAIN_WEAPON_MAX:
                return "max"
            if _main_tier(lvl + 1) > _unlocked_tier_for(save, key):
                return "locked"
            return "upgrade"
        if slot == "side":
            lvl = getattr(save.loadout, f"side_{wtype}")
            if lvl == 0:
                return "buy"
            if save.loadout.side_type != wtype:
                return "equip"
            if lvl >= SIDE_WEAPON_MAX:
                return "max"
            if (lvl + 1) > _unlocked_tier_for(save, key):
                return "locked"
            return "upgrade"
        if key == "bomb":
            return "buy"
        if key.startswith("ability_"):
            return "equipped" if save.loadout.ability == key[len("ability_"):] else "equip"
        lvl = getattr(save.loadout, key)
        if lvl >= MAX_LEVELS[key]:
            return "max"
        if (lvl + 1) > _unlocked_tier_for(save, key):
            return "locked"
        return "upgrade"

    def _can_buy(self, key):
        save = self.app.save
        if key.startswith("ability_"):
            ability = key[len("ability_"):]
            return save.loadout.ability != ability
        action = self._row_action(key)
        if action == "max":
            return False
        if action == "equip":
            return True  # free
        cost = self._item_cost(key)
        if cost is None:
            return False
        return save.credits >= cost

    def _buy(self):
        key = SHOP_ITEMS[self.cursor][0]
        save = self.app.save
        if not self._can_buy(key):
            self.app.sounds["deny"].play()
            action = self._row_action(key)
            if action == "max":
                self.flash_text = "ALREADY MAX"
            elif key.startswith("ability_"):
                self.flash_text = "ALREADY EQUIPPED"
            else:
                self.flash_text = "NOT ENOUGH"
            self.flash_t = 1.0
            return
        if key.startswith("ability_"):
            save.loadout.ability = key[len("ability_"):]
            self.flash_text = "ABILITY EQUIPPED"
        elif key == "bomb":
            save.credits -= BOMB_PRICE
            save.loadout.bombs = min(9, save.loadout.bombs + 1)
            self.flash_text = "+1 BOMB"
        else:
            slot, wtype = _parse_weapon_key(key)
            if slot == "main":
                lvl = getattr(save.loadout, f"main_{wtype}")
                save.credits -= MAIN_UPGRADE_COSTS[wtype][lvl]
                setattr(save.loadout, f"main_{wtype}", lvl + 1)
                self.flash_text = "UPGRADED"
            elif slot == "side":
                lvl = getattr(save.loadout, f"side_{wtype}")
                if lvl == 0:
                    save.credits -= SIDE_BUY_COST
                    setattr(save.loadout, f"side_{wtype}", 1)
                    save.loadout.side_type = wtype
                    self.flash_text = "PURCHASED"
                elif save.loadout.side_type != wtype:
                    save.loadout.side_type = wtype
                    self.flash_text = "EQUIPPED"
                else:
                    save.credits -= SIDE_UPGRADE_COSTS[wtype][lvl]
                    setattr(save.loadout, f"side_{wtype}", lvl + 1)
                    self.flash_text = "UPGRADED"
            else:
                cost = self._item_cost(key)
                save.credits -= cost
                setattr(save.loadout, key, getattr(save.loadout, key) + 1)
                self.flash_text = "UPGRADED"
        self.flash_t = 1.2
        self.app.sounds["confirm"].play()
        save.save()

    def _draw(self):
        screen = self.app.screen
        fonts = self.app.fonts
        save = self.app.save
        screen.fill(BLACK)

        # ===== Left panel: header + item list ====================================
        pygame.draw.rect(screen, HUD_BG, (0, 0, PLAY_W, SCREEN_H))
        for eid in ("hangar_title", "back_hint"):
            el = get_element("shop", eid)
            if el is not None:
                _layout_draw_item(screen, el, fonts, self.app.assets, {})

        # Column layout: name on left, bar at fixed column, cost right-aligned.
        # Bar must end before the longest cost label ("EQUIPPED" ~ 96 px at
        # scale-2 advance 12); cost text starts at COST_RIGHT - 96 = 360,
        # so the bar's right edge sits at BAR_X + BAR_W <= 354 with a small
        # gap. BAR_W deliberately narrower than the old 180 to leave room.
        NAME_X = 20
        BAR_X = PLAY_W - 260      # 220
        BAR_W = 130
        COST_RIGHT = PLAY_W - 24
        ROW_H = 22
        list_top = 64
        y = list_top
        for i, (key, label) in enumerate(SHOP_ITEMS):
            row_color = WHITE if i == self.cursor else DIM
            cost = self._item_cost(key)
            action = self._row_action(key)
            slot, wtype = _parse_weapon_key(key)
            if i == self.cursor:
                pygame.draw.rect(screen, (30, 36, 60), (12, y - 4, PLAY_W - 24, 22))
            name_surf = fonts["small"].render(label, False, row_color)
            screen.blit(name_surf, (NAME_X, y))
            # Mark currently EQUIPPED sidekick with a small chevron tag.
            # Main weapons are always-on now (L1/R1 hold swap), no EQ tag.
            if slot == "side" and save.loadout.side_type == wtype and getattr(save.loadout, f"side_{wtype}") > 0:
                tag = fonts["tiny"].render("EQ", False, GREEN)
                screen.blit(tag, (NAME_X + name_surf.get_width() + 6, y + 2))
            if key.startswith("ability_"):
                ability = key[len("ability_"):]
                equipped = save.loadout.ability == ability
                right = "EQUIPPED" if equipped else "free"
                right_col = GREEN if equipped else row_color
                r = fonts["small"].render(right, False, right_col)
                screen.blit(r, (COST_RIGHT - r.get_width(), y))
            elif key == "bomb":
                state = f"x{save.loadout.bombs}"
                cost_str = f"${BOMB_PRICE}"
                s = fonts["small"].render(state, False, row_color)
                screen.blit(s, (BAR_X, y))
                c = fonts["small"].render(cost_str, False, row_color)
                screen.blit(c, (COST_RIGHT - c.get_width(), y))
            else:
                # Slot setup. Each weapon / equipment carries its own
                # current level + total tiers; "subs_per_tier" is the
                # internal subdivision (4 for main, 1 for everything else).
                if slot == "main":
                    lvl = getattr(save.loadout, f"main_{wtype}")
                    subs_per_tier = 4
                    full_tiers = 5
                elif slot == "side":
                    lvl = getattr(save.loadout, f"side_{wtype}")
                    subs_per_tier = 1
                    full_tiers = 5
                else:
                    lvl = getattr(save.loadout, key)
                    subs_per_tier = 1
                    full_tiers = MAX_LEVELS[key]
                visible_tiers = max(0, min(full_tiers,
                                            _unlocked_tier_for(save, key)))
                if visible_tiers <= 0:
                    # Should never happen at runtime (default unlock = 2)
                    y += ROW_H
                    continue
                # Bar width = visible tiers * TIER_PX + (visible - 1) gaps.
                # Uniform: every row's tier-cell looks the same width.
                bar_w = visible_tiers * self.TIER_PX + max(0, (visible_tiers - 1) * self.TIER_GAP)
                bar_max = visible_tiers * subs_per_tier
                bar_val = min(lvl, bar_max)
                fill_col = (GREEN if lvl >= full_tiers * subs_per_tier
                            else (WHITE if i == self.cursor else (160, 160, 200)))
                _layout_draw_tiered_bar(screen, {
                    "x": BAR_X, "y": y + 2,
                    "w": bar_w, "h": self.BAR_H,
                    "value": bar_val, "max": bar_max,
                    "tiers": visible_tiers,
                    "color": fill_col,
                    "bg_color": DARKER,
                    "sep_color": (60, 70, 110),
                }, None)
                # Reveal-animation flash overlay on the newly-unlocked tier
                # cell for the row that matches the currently-animating unlock.
                if self.current_unlock is not None:
                    unlock_cat, unlock_tier = self.current_unlock
                    cat_key = _shop_key_for_cat(unlock_cat)
                    if cat_key == key and unlock_tier == visible_tiers:
                        # Flash intensity peaks at start, fades over duration.
                        t_norm = min(1.0, self.current_unlock_t / self.REVEAL_PER_UNLOCK_SEC)
                        alpha = int(255 * (1.0 - t_norm))
                        cell_x = BAR_X + (visible_tiers - 1) * (self.TIER_PX + self.TIER_GAP)
                        flash = pygame.Surface((self.TIER_PX, self.BAR_H),
                                                pygame.SRCALPHA)
                        flash.fill((*self.REVEAL_FLASH_COLOR, alpha))
                        screen.blit(flash, (cell_x, y + 2))
                        # Small label above the flashing cell.
                        label_txt = f"T{unlock_tier} UNLOCKED"
                        lbl = fonts["tiny"].render(label_txt, False,
                                                    self.REVEAL_FLASH_COLOR)
                        lbl.set_alpha(min(255, int(255 * (1.0 - t_norm * 0.7))))
                        screen.blit(lbl, (cell_x + self.TIER_PX // 2 - lbl.get_width() // 2,
                                          y - 8))
                # Cost label
                if action == "max":
                    cost_str, cost_col = "MAX", GREEN
                elif action == "locked":
                    cost_str, cost_col = "LOCKED", (110, 110, 130)
                elif action == "equip":
                    cost_str, cost_col = "EQUIP", CYAN
                elif action == "buy":
                    cost_str, cost_col = f"${cost}", ORANGE
                else:
                    cost_str, cost_col = f"${cost}", row_color
                c = fonts["small"].render(cost_str, False, cost_col)
                screen.blit(c, (COST_RIGHT - c.get_width(), y))
            y += ROW_H

        # ===== Bottom DETAIL strip (wide, across the playfield) =================
        key = SHOP_ITEMS[self.cursor][0]
        label = SHOP_ITEMS[self.cursor][1]
        cost = self._item_cost(key)
        detail_y = SCREEN_H - 100
        _panel(screen, 14, detail_y, PLAY_W - 28, 88, "UPGRADE DETAIL", fonts)
        # Left column: item name + current level + effect
        ly = detail_y + 14
        screen.blit(fonts["small"].render(label.upper(), False, CYAN), (28, ly))
        cur_str, cur_effect, next_effect, cost_str, cost_col = self._detail_pieces(key, cost)
        screen.blit(fonts["tiny"].render(cur_str, False, WHITE), (28, ly + 20))
        screen.blit(fonts["tiny"].render(cur_effect, False, DIM), (28, ly + 36))
        # Right column: NEXT effect + cost
        rx = PLAY_W // 2 + 20
        el = get_element("shop", "detail_next_label")
        if el is not None:
            _layout_draw_item(screen, el, fonts, self.app.assets, {})
        screen.blit(fonts["tiny"].render(next_effect, False, WHITE), (rx, ly + 20))
        screen.blit(fonts["tiny"].render(cost_str, False, cost_col), (rx, ly + 36))

        if self.flash_t > 0 and self.flash_text:
            txt = fonts["small"].render(self.flash_text, False, YELLOW)
            screen.blit(txt, txt.get_rect(center=(PLAY_W // 2, detail_y - 14)))

        # Right-side strip (header / balance / control hints) is fully
        # element-driven — see _build_shop_panel_spec(). Drawing the root
        # container recursively paints the bg fill, panels, and dynamic
        # {credits} readout in one pass.
        shop_panel_vars = {"credits": save.credits, **button_label_vars()}
        shop_root = get_element("shop", "shop_root", **shop_panel_vars)
        if shop_root is not None:
            _layout_draw_item(screen, shop_root, fonts, self.app.assets,
                              shop_panel_vars)

        draw_layout_overlay(screen, "shop", fonts, self.app.assets)

    def _detail_pieces(self, key, cost):
        """Returns 5-tuple: current level string, current effect, next effect,
        cost string, cost colour. Used by the bottom DETAIL strip."""
        save = self.app.save
        slot, wtype = _parse_weapon_key(key)
        # Tier descriptions are 5-long for main weapons, 3-long for side.
        # Within a tier, sub-levels share the tier description plus a +dmg
        # bump. Damage per bullet is 100 + 10*(level-1) for everything.
        MAIN_TIER_DESCS = {
            "pulse":  ["single shot", "dual shot", "triple spread",
                       "quad shot", "quad + wing"],
            "spread": ["3-way fan", "5-way fan", "7-way fan",
                       "9-way fan", "11-way wave"],
            "vulcan": ["rapid 1", "rapid dual", "rapid triple",
                       "rapid quad", "rapid quint"],
        }
        SIDE_TIER_DESCS = {
            "missile": ["1 heatseeker", "2 heatseekers", "3 heatseekers"],
            "drone":   ["1 drone shot", "2 drone shots", "3 drone shots"],
        }

        def _level_eff(level, max_tiers, tier_descs):
            """Format a level as 'tier-name · T<n> sub/4 · <dmg>dmg'."""
            tier = max(1, min(max_tiers, (level - 1) // 4 + 1))
            sub = (level - 1) % 4 + 1
            dmg = 100 + 10 * (level - 1)
            return f"{tier_descs[tier - 1]} · T{tier} {sub}/4 · {dmg}dmg"

        if slot == "main":
            # All mains are always owned + always equipped (L1/R1 hold).
            # The row only ever offers upgrade or shows MAX.
            tier_descs = MAIN_TIER_DESCS[wtype]
            lvl = getattr(save.loadout, f"main_{wtype}")
            mx = MAIN_WEAPON_MAX
            hold_label = {"pulse": "hold L1", "spread": "hold R1",
                          "vulcan": "no hold"}[wtype]
            cur_eff = _level_eff(lvl, 5, tier_descs)
            if lvl < mx:
                return (f"Lv {lvl}/{mx}  ({hold_label})", cur_eff,
                        _level_eff(lvl + 1, 5, tier_descs),
                        f"Cost ${cost}", YELLOW)
            return (f"Lv {lvl}/{mx}  ({hold_label})", cur_eff,
                    "fully upgraded", "MAX", GREEN)
        if slot == "side":
            tier_descs = SIDE_TIER_DESCS[wtype]
            lvl = getattr(save.loadout, f"side_{wtype}")
            mx = SIDE_WEAPON_MAX
            equipped = save.loadout.side_type == wtype and lvl > 0
            tag = " (EQ)" if equipped else ""
            if lvl == 0:
                return ("not owned", "—", _level_eff(1, 3, tier_descs),
                        f"Buy ${cost}", ORANGE)
            cur_eff = _level_eff(lvl, 3, tier_descs)
            if not equipped:
                return (f"Lv {lvl}/{mx}{tag}", cur_eff,
                        f"equip with {BUTTON_SCHEME['fire'][1]}", "free", CYAN)
            if lvl < mx:
                return (f"Lv {lvl}/{mx}{tag}", cur_eff,
                        _level_eff(lvl + 1, 3, tier_descs),
                        f"Cost ${cost}", YELLOW)
            return (f"Lv {lvl}/{mx}{tag}", cur_eff,
                    "fully upgraded", "MAX", GREEN)
        if key == "shield":
            cur = save.loadout.shield
            mx = MAX_LEVELS["shield"]
            cur_eff = f"Max {SHIELD_MAX[cur]}HP regen {SHIELD_REGEN[cur]}/s"
            if cur < mx:
                nx = cur + 1
                nxt = f"Max {SHIELD_MAX[nx]}HP regen {SHIELD_REGEN[nx]}/s"
                return (f"Lv {cur}/{mx}", cur_eff, nxt, f"Cost ${cost}", YELLOW)
            return (f"Lv {cur}/{mx}", cur_eff, "fully upgraded", "MAX", GREEN)
        if key == "engine":
            cur = save.loadout.engine
            mx = MAX_LEVELS["engine"]
            cur_eff = f"{ENGINE_SPEEDS[cur]} px/s"
            if cur < mx:
                nx = cur + 1
                return (f"Lv {cur}/{mx}", cur_eff, f"{ENGINE_SPEEDS[nx]} px/s",
                        f"Cost ${cost}", YELLOW)
            return (f"Lv {cur}/{mx}", cur_eff, "fully upgraded", "MAX", GREEN)
        if key == "bomb":
            return (f"Owned x{save.loadout.bombs}",
                    f"Pulse Bomb on {BUTTON_SCHEME['bomb'][1]}",
                    "Adds 1 bomb (max 9)",
                    f"Cost ${BOMB_PRICE}", YELLOW)
        if key.startswith("ability_"):
            ab = key[len("ability_"):]
            equipped = save.loadout.ability == ab
            swap_tip = f"swap on {BUTTON_SCHEME['fire'][1]}"
            descs = {
                "screen_clear": ("clears all enemies on screen", swap_tip),
                "shield_burst": ("refills shield + brief invuln", swap_tip),
                "mega_laser":   ("sustained high-dps beam",       swap_tip),
            }
            d, action = descs.get(ab, ("", ""))
            if equipped:
                return ("EQUIPPED", d, action, "free", GREEN)
            return ("not equipped", d, action,
                    f"Equip with {BUTTON_SCHEME['fire'][1]}", YELLOW)
        return ("", "", "", "", DIM)




# =============================================================================
# TITLE / GAMEOVER
# =============================================================================

class TitleScreen:
    def __init__(self, app):
        self.app = app
        self.stars = ParallaxStars(SCREEN_W, SCREEN_H, counts=(80, 60, 40))
        # Mirror-tiled backdrop scrolling downward behind the stars. The
        # theme follows the player's current progress (current save node →
        # sector → SECTOR_RIBBONS) so the title hints at where you are.
        self._rebuild_for_profile()
        self.t = 0
        self.outcome = None
        self.cursor = 0
        # When True, an "OVERWRITE PROGRESS?" dialog is showing and the
        # menu is suspended until the user presses Y (confirm wipe) or
        # B/Start (cancel back to the menu).
        self._confirm_new_game = False
        # Release-notes overlay state. Mounted from `app.pending_release_notes`
        # at construction; when non-None, normal title input is suspended
        # until the player presses ability (install) or confirm (dismiss).
        # Dismiss only suppresses the overlay for THIS TitleScreen
        # instance — we leave App.pending_release_notes set so any
        # subsequent return-to-title (Map → START → Title, game over →
        # Title, etc.) re-mounts the modal. The reminder keeps coming
        # back until the player actually installs the update, and the
        # notes accumulate naturally because the fetch is keyed off
        # the installed VERSION (which doesn't change without an install).
        self._notes = None
        self._notes_scroll = 0
        self._notes_dismissed = False
        self._mount_release_notes()

    def _rebuild_for_profile(self):
        """Re-pick the backdrop ribbon + Continue/New Game/Quit menu to
        match the currently-active profile. Called on init and again
        whenever L1/R1 cycles to a new profile."""
        node = self.app.save.current_node or "L001"
        try:
            n = int(node[1:])
        except (ValueError, IndexError):
            n = 1
        sector_idx = max(0, min(len(SECTOR_RIBBONS) - 1, (n - 1) // 10))
        theme = SECTOR_RIBBONS[sector_idx]
        self.bg_ribbon = BackgroundRibbon(theme, width=SCREEN_W,
                                          tile_h=SCREEN_H * 2)
        # Native aspect, mirror-tiled 3x horizontally so the source art
        # reads at its true proportions on the title screen too.
        self.bg_ribbon.remake_native_aspect_h(mirror_n=3)
        self.bg_ribbon.make_mirrored()
        self.bg_ribbon.speed = -24.0
        self.has_save = SaveData.profile_exists(self.app.profile_name)
        self.options = (["Continue", "New Game", "Quit"]
                        if self.has_save else ["New Game", "Quit"])

    def _save_has_progress(self):
        """Heuristic for "is there anything worth losing in this profile?".
        Used to gate the New Game confirm prompt — a brand-new profile
        (or one wiped a moment ago) skips the dialog and starts directly."""
        s = self.app.save
        if s.credits > 0 or s.high_score > 0:
            return True
        if s.completed:
            return True
        if s.current_node not in (None, "", "L001"):
            return True
        if s.unlocked and set(s.unlocked) - {"L001"}:
            return True
        return False

    def _start_new_game(self):
        """Reset the active profile to a fresh save and head to the map."""
        self.app.save = SaveData()
        self.app.save.save(self.app.profile_name)
        self.has_save = True
        self.options = ["Continue", "New Game", "Quit"]
        self._confirm_new_game = False
        self.outcome = ("map", None)

    def _toggle_channel(self):
        """SELECT+ability: stable ↔ uat. Persists the .uat_channel marker,
        refreshes the in-memory channel so the version stamp colour flips
        immediately, then re-runs the updater against the new channel.
        If anything diffs, `_check_release_update` will re-exec us — so
        this call may not return."""
        cur = autoupdate_channel()
        new = "uat" if cur == "stable" else "stable"
        self.app.channel = autoupdate_set_channel(new)
        try: self.app.sounds["menu"].play()
        except Exception: pass
        # `force=True` bypasses the Windows-default gate so the user can
        # drive the switch end-to-end from the dev box.
        _check_release_update(force=True)

    def _manual_update(self):
        """Ability (no SELECT) on the title: re-run the updater on the
        current channel and apply anything new. `_check_release_update`
        re-execs us when files diff, so this call may not return — when
        it does, that means there was nothing to pull. Play the menu
        sound either way so the player sees the press registered."""
        try: self.app.sounds["menu"].play()
        except Exception: pass
        _check_release_update(force=True)

    # ── Release-notes overlay ──────────────────────────────────────────
    def _mount_release_notes(self):
        """Grab any pending notes off App and prepare a wrapped + scrolled
        view. Empty pending = nothing to mount. Skipped when the player
        already dismissed during this TitleScreen instance, so a frame-
        loop call doesn't immediately re-mount after they hit close;
        a *new* TitleScreen (e.g. after returning from Map) gets a fresh
        instance and will mount again from app.pending_release_notes."""
        if self._notes is not None:
            return
        if self._notes_dismissed:
            return
        text = (getattr(self.app, "pending_release_notes", "") or "").strip()
        if not text:
            return
        self._notes = text
        self._notes_scroll = 0
        # Cache the wrap so we don't re-word-wrap every frame; the panel
        # geometry is fixed.
        self._notes_lines_cache = None

    def _dismiss_release_notes(self):
        """Player closed the overlay without updating. Only suppress for
        this TitleScreen instance — app.pending_release_notes stays set
        so the next return-to-title (Map → START → Title, game over →
        Title, etc.) re-mounts the modal. Reminder keeps coming back
        until the player installs the update; skipped versions
        accumulate naturally because fetch_release_notes_since is keyed
        off the installed VERSION."""
        self._notes = None
        self._notes_scroll = 0
        self._notes_lines_cache = None
        self._notes_dismissed = True
        try: self.app.sounds["menu"].play()
        except Exception: pass

    # Overlay panel geometry — fixed so wrap can cache.
    _NOTES_PANEL = (40, 40, SCREEN_W - 80, SCREEN_H - 80)
    _NOTES_PAD = 10
    _NOTES_TITLE_H = 22
    _NOTES_FOOTER_H = 18

    def _notes_font(self):
        """5x7 scale 1 (the "tiny" font) — a bit smaller than the
        original 7x10 bold so more of the changelog fits on screen
        before scrolling kicks in."""
        return (self.app.fonts.get("tiny")
                or self.app.fonts.get(1)
                or self.app.fonts.get(("7x9", 1)))

    def _notes_wrapped_lines(self):
        """Word-wrap the notes to the panel width. Cached after first call.
        Preserves blank lines (paragraph breaks)."""
        if self._notes_lines_cache is not None:
            return self._notes_lines_cache
        font = self._notes_font()
        px, py, pw, ph = self._NOTES_PANEL
        max_w = pw - self._NOTES_PAD * 2
        out = []
        for raw_line in self._notes.splitlines():
            if not raw_line.strip():
                out.append("")
                continue
            words = raw_line.split(" ")
            cur = ""
            for w in words:
                trial = w if not cur else cur + " " + w
                if font.render(trial, False, WHITE).get_width() <= max_w:
                    cur = trial
                else:
                    if cur:
                        out.append(cur)
                    # Word longer than the line — hard-break by character.
                    while font.render(w, False, WHITE).get_width() > max_w:
                        # Trim one char at a time until it fits; then
                        # carry the remainder.
                        lo, hi = 1, len(w)
                        fit = 1
                        while lo <= hi:
                            mid = (lo + hi) // 2
                            if font.render(w[:mid], False, WHITE).get_width() <= max_w:
                                fit = mid
                                lo = mid + 1
                            else:
                                hi = mid - 1
                        out.append(w[:fit])
                        w = w[fit:]
                    cur = w
            if cur:
                out.append(cur)
        self._notes_lines_cache = out
        return out

    def _scroll_notes(self, delta_lines):
        """Step the scroll by N lines (positive = down). Clamped so the
        last visible line never overshoots — keeps the bottom anchored
        when the user mashes down at the end."""
        font = self._notes_font()
        px, py, pw, ph = self._NOTES_PANEL
        content_h = ph - self._NOTES_PAD * 2 - self._NOTES_TITLE_H - self._NOTES_FOOTER_H
        line_h = font.full_height if hasattr(font, "full_height") else font.render("Ag", False, WHITE).get_height()
        visible = max(1, content_h // line_h)
        total = len(self._notes_wrapped_lines())
        max_scroll = max(0, total - visible)
        self._notes_scroll = max(0, min(max_scroll, self._notes_scroll + delta_lines))

    def _draw_release_notes(self, screen):
        """Render the modal: dim backdrop, framed panel, title bar,
        scrollable body, footer hint. Lines outside the body box are
        clipped via Surface.set_clip."""
        # Dim everything behind us.
        dim = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 180))
        screen.blit(dim, (0, 0))

        px, py, pw, ph = self._NOTES_PANEL
        # Panel bg + border.
        bg = pygame.Surface((pw, ph), pygame.SRCALPHA)
        bg.fill((18, 22, 38, 240))
        screen.blit(bg, (px, py))
        pygame.draw.rect(screen, (110, 160, 220), (px, py, pw, ph), 1)

        # Title bar — explains the new flow: the bundled changelog is
        # *pending* until the player presses ability to install.
        title_font = self.app.fonts.get(("7x9", 2)) or self.app.fonts.get("small")
        ab_lbl = BUTTON_SCHEME["ability"][1]
        title_txt = f"UPDATE AVAILABLE  ·  {ab_lbl} to install"
        title = title_font.render(title_txt, False, (80, 220, 255))
        screen.blit(title, (px + self._NOTES_PAD, py + 4))
        pygame.draw.rect(screen, (60, 80, 130),
                         (px + self._NOTES_PAD, py + self._NOTES_TITLE_H + 2,
                          pw - self._NOTES_PAD * 2, 1))

        # Body (clipped).
        body_x = px + self._NOTES_PAD
        body_y = py + self._NOTES_PAD + self._NOTES_TITLE_H
        body_w = pw - self._NOTES_PAD * 2
        body_h = ph - self._NOTES_PAD * 2 - self._NOTES_TITLE_H - self._NOTES_FOOTER_H
        prev_clip = screen.get_clip()
        screen.set_clip(pygame.Rect(body_x, body_y, body_w, body_h))
        font = self._notes_font()
        line_h = font.full_height if hasattr(font, "full_height") else font.render("Ag", False, WHITE).get_height()
        lines = self._notes_wrapped_lines()
        visible = max(1, body_h // line_h)
        end = min(len(lines), self._notes_scroll + visible + 1)
        cy = body_y
        for i in range(self._notes_scroll, end):
            line = lines[i]
            if line.startswith("=== ") and line.endswith(" ==="):
                # Per-release header — accent colour.
                col = (255, 200, 90)
                txt = line[4:-4]
            elif line.startswith("## "):
                col = (200, 220, 255)
                txt = line[3:]
            elif line.startswith("- ") or line.startswith("* "):
                col = (220, 220, 230)
                txt = "• " + line[2:]
            else:
                col = (200, 200, 210)
                txt = line
            screen.blit(font.render(txt, False, col), (body_x, cy))
            cy += line_h
        screen.set_clip(prev_clip)

        # Scroll indicator: a tiny up/down arrow when there's more above/below.
        if self._notes_scroll > 0:
            pygame.draw.polygon(screen, (200, 200, 220),
                                [(px + pw - 14, body_y + 6),
                                 (px + pw - 8, body_y + 14),
                                 (px + pw - 20, body_y + 14)])
        if end < len(lines):
            arrow_y = body_y + body_h - 8
            pygame.draw.polygon(screen, (200, 200, 220),
                                [(px + pw - 14, arrow_y),
                                 (px + pw - 8, arrow_y - 8),
                                 (px + pw - 20, arrow_y - 8)])

        # Footer hint — both actions, since either is reasonable from
        # the overlay (install now vs play first, install later).
        confirm_lbl = BUTTON_SCHEME["fire"][1]
        ability_lbl = BUTTON_SCHEME["ability"][1]
        footer_font = self.app.fonts.get("small") or self.app.fonts["tiny"]
        hint = footer_font.render(
            f"D-pad scroll   {ability_lbl}: install   {confirm_lbl}: close",
            False, (140, 140, 160))
        screen.blit(hint, (px + self._NOTES_PAD,
                           py + ph - hint.get_height() - 3))

    def _handle_release_notes_input(self, events, controls):
        """Scroll on d-pad / left-stick / arrows; dismiss on confirm or
        start; ability triggers the install (re-runs the updater and
        re-execs on success). Page-step on shoulders so a long
        changelog isn't a carpal-tunnel exercise."""
        for ev in events:
            if ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_UP, pygame.K_w):
                    self._scroll_notes(-1)
                elif ev.key in (pygame.K_DOWN, pygame.K_s):
                    self._scroll_notes(+1)
                elif ev.key == pygame.K_PAGEUP:
                    self._scroll_notes(-8)
                elif ev.key == pygame.K_PAGEDOWN:
                    self._scroll_notes(+8)
                elif ev.key in (pygame.K_RETURN, pygame.K_SPACE,
                                pygame.K_z, pygame.K_ESCAPE):
                    self._dismiss_release_notes()
                elif ev.key == pygame.K_x:
                    # Keyboard fallback for the ability silk letter.
                    self._manual_update()
                    self._dismiss_release_notes()
            elif ev.type == pygame.JOYHATMOTION:
                _, hy = ev.value
                if hy > 0:
                    self._scroll_notes(-1)
                elif hy < 0:
                    self._scroll_notes(+1)
            elif ev.type == pygame.JOYBUTTONDOWN:
                if ev.button in (JOY_L1, JOY_L2):
                    self._scroll_notes(-8)
                elif ev.button in (JOY_R1, JOY_R2):
                    self._scroll_notes(+8)
        # Ability = install + dismiss. _manual_update re-execs on success,
        # so if it returns we know nothing actually diffed — fall through
        # to dismissing so the player isn't stuck staring at the modal.
        if controls.ability_pressed:
            self._manual_update()
            self._dismiss_release_notes()
        elif controls.confirm_pressed or controls.start_pressed:
            self._dismiss_release_notes()

    def _cycle_profile(self, delta):
        """L1/R1 step the active profile by `delta` (±1) and reload the
        title state for the new slot. Wraps around the 5-name list."""
        try:
            idx = PROFILE_NAMES.index(self.app.profile_name)
        except ValueError:
            idx = 0
        new = PROFILE_NAMES[(idx + delta) % len(PROFILE_NAMES)]
        if new == self.app.profile_name:
            return
        self.app.switch_profile(new)
        self.cursor = 0
        self._rebuild_for_profile()
        try:
            self.app.sounds["menu"].play()
        except Exception:
            pass

    def run(self, events, controls):
        dt = 1.0 / FPS
        self.t += dt
        self.bg_ribbon.update(dt)
        self.stars.update(dt)
        # Release-notes modal: scroll on d-pad / stick / arrows; dismiss
        # on confirm; nothing else reacts while it's up.
        self._mount_release_notes()
        if self._notes is not None:
            self._handle_release_notes_input(events, controls)
            self._draw()
            return self.outcome
        moved = False
        for ev in events:
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_UP:
                    self.cursor = (self.cursor - 1) % len(self.options); moved = True
                if ev.key == pygame.K_DOWN:
                    self.cursor = (self.cursor + 1) % len(self.options); moved = True
                # Keyboard fallback for the L1/R1 profile cycle: Q / E.
                if ev.key == pygame.K_q:
                    self._cycle_profile(-1)
                if ev.key == pygame.K_e:
                    self._cycle_profile(+1)
                # TAB cycles dev-machine present modes (4-stop loop —
                # integer → scaled-grid → fill → fill-grid). No-op on
                # device since the mali path bypasses _present. Persist
                # the choice so a relaunch keeps the mode picked.
                if ev.key == pygame.K_TAB:
                    self.app.cycle_scale_mode()
                    try:
                        self.app.sounds["menu"].play()
                    except Exception:
                        pass
            if ev.type == pygame.JOYHATMOTION:
                _, hy = ev.value
                if hy > 0:
                    self.cursor = (self.cursor - 1) % len(self.options); moved = True
                if hy < 0:
                    self.cursor = (self.cursor + 1) % len(self.options); moved = True
            if ev.type == pygame.JOYBUTTONDOWN:
                if ev.button == JOY_L1:
                    self._cycle_profile(-1)
                elif ev.button == JOY_R1:
                    self._cycle_profile(+1)
        if moved:
            # Any d-pad / stick deflection while the OVERWRITE prompt is
            # up dismisses it — the player clearly meant to keep poking
            # the menu, not erase their save.
            if self._confirm_new_game:
                self._confirm_new_game = False
            self.app.sounds["menu"].play()
        if self._confirm_new_game:
            # Modal: north face (cancel-action — silk X on RG, silk Y on
            # Steam Deck) commits the wipe; south face (fire-action — silk
            # B on RG, silk A on Steam Deck) or Start cancels it.
            if controls.cancel_pressed:
                self._start_new_game()
            elif controls.confirm_pressed or controls.start_pressed:
                self._confirm_new_game = False
                try:
                    self.app.sounds["menu"].play()
                except Exception:
                    pass
        elif controls.confirm_pressed or controls.start_pressed:
            choice = self.options[self.cursor]
            if choice == "Continue":
                self.outcome = ("map", None)
            elif choice == "New Game":
                # If the active profile already has progress, ask first —
                # the actual reset happens via Y in the modal branch
                # above.
                if self.has_save and self._save_has_progress():
                    self._confirm_new_game = True
                else:
                    self._start_new_game()
            elif choice == "Quit":
                self.outcome = ("quit", None)
        # Hidden / utility face-button combos. Single if/elif chain so
        # SELECT-modified bindings take precedence over the unmodified
        # ones. Order: visual-checkup > channel-toggle > scale-cycle >
        # manual-update.
        if controls.select and controls.cancel_pressed:
            # Hidden visual-checkup mission (SELECT + Y on RG / X on PC).
            self.outcome = ("play", make_test_level())
        elif controls.select and controls.ability_pressed:
            # SELECT + ability (west face — silk Y on RG, silk X on PC):
            # flip the auto-update channel between stable (latest GitHub
            # release) and uat (master tip), persist the .uat_channel
            # marker, then reload.
            self._toggle_channel()
        elif (controls.cancel_pressed
                and not self._confirm_new_game):
            # Plain cancel/north (no SELECT, no modal): cycle the dev-
            # machine present mode — the gamepad equivalent of TAB.
            self.app.cycle_scale_mode()
            try:
                self.app.sounds["menu"].play()
            except Exception:
                pass
        elif (controls.ability_pressed
                and not self._confirm_new_game):
            # Plain ability/west (no SELECT, no modal): re-check the
            # current channel for updates and apply if any. _manual_update
            # re-execs us on success, so this call may not return.
            self._manual_update()
        # Hidden bot-replay shortcut: L2 (avg upgrade path) or R2 (optimal)
        # held + D-pad direction → play back the latest recorded bot run for
        # the matching profile. dpad left=good, up=med, right=bad.
        prof = _gesture_to_profile(controls)
        if prof is not None and _find_replay_path(prof) is not None:
            self.outcome = ("replay_full", prof)
        self._draw()
        return self.outcome

    def _draw(self):
        screen = self.app.screen
        screen.fill(BLACK)
        self.bg_ribbon.draw(screen)
        self.stars.draw(screen)

        # --- LOGO --------------------------------------------------------
        logo_el = get_element("title", "logo")
        if logo_el is not None:
            logo = self.app.logo
            scale = float(logo_el.get("scale", 1.0))
            if abs(scale - 1.0) > 0.001:
                sw, sh = logo.get_size()
                logo = pygame.transform.smoothscale(
                    logo, (max(1, int(sw * scale)), max(1, int(sh * scale))))
            anchor = logo_el.get("anchor", "c")
            ax = _LAYOUT_ANCHOR_AX.get(anchor, 0.5)
            ay = _LAYOUT_ANCHOR_AY.get(anchor, 0.5)
            lw, lh = logo.get_size()
            lx = int(logo_el.get("x", SCREEN_W // 2)) - int(lw * ax)
            ly = int(logo_el.get("y", 130)) - int(lh * ay)
            logo_rect = pygame.Rect(lx, ly, lw, lh)
            # Glossy light-wave sweep across the title's editor-defined hitbox,
            # masked so only the YELLOW areas of the logo brighten.
            entry = self.app.assets.get("_engine_data", {}).get("title", {})
            hitbox = entry.get("hitbox")
            stripe = self.app.title_gloss_stripe
            yellow_mask = self.app.title_yellow_mask
            # Sweep math + hitbox are baked at the original (unscaled) logo
            # size, so skip the gloss when the user has scaled the logo away
            # from 1.0× — a plain blit is the safer fallback.
            yellow_dim = self.app.title_yellow_dim
            # Clip the hitbox to actual logo bounds — a sprite-editor edit
            # can push the rect past the edge, which makes subsurface() raise.
            clipped_hitbox = None
            if hitbox:
                hx, hy, hw, hh = hitbox
                clipped = pygame.Rect(int(hx), int(hy), max(1, int(hw)),
                                       max(1, int(hh))).clip(logo.get_rect())
                if clipped.w > 0 and clipped.h > 0:
                    clipped_hitbox = clipped
            if (clipped_hitbox is not None and stripe is not None
                    and yellow_mask is not None and yellow_dim is not None
                    and abs(scale - 1.0) < 0.001):
                hx, hy, hw, hh = (clipped_hitbox.x, clipped_hitbox.y,
                                  clipped_hitbox.w, clipped_hitbox.h)
                stripe_w = stripe.get_width()
                # Cadence: two back-to-back sweeps, then a fixed 1s rest.
                # Total cycle = 2*sweep_dur + rest_dur (not a fixed period).
                sweep_dur = 0.9
                rest_dur = 1.0
                group_period = sweep_dur * 2 + rest_dur
                travel = hw + stripe_w * 2
                local_t = self.t % group_period
                if local_t < sweep_dur * 2:
                    cycle = (local_t % sweep_dur) / sweep_dur
                    stripe_x = int(cycle * travel) - stripe_w
                else:
                    stripe_x = None  # rest gap: dim stays, no boost
                mask_rect = pygame.Rect(hx, hy, hw, hh).clip(
                    yellow_mask.get_rect())
                # Build a per-pixel brightness multiplier (factor) sized to
                # the hitbox. Yellow pixels go to 64 (25%) by default and
                # rise back up toward 255 (100%) under the moving stripe;
                # non-yellow pixels stay at 255 (unchanged).
                factor = pygame.Surface((hw, hh)).convert()
                factor.fill((255, 255, 255))
                if mask_rect.w > 0 and mask_rect.h > 0:
                    # Dim yellow pixels to 64.
                    factor.blit(yellow_dim.subsurface(mask_rect),
                                (mask_rect.x - hx, mask_rect.y - hy),
                                special_flags=pygame.BLEND_SUB)
                    if stripe_x is not None:
                        # Boost (only on yellow) brings them back toward 255.
                        boost = pygame.Surface((hw, hh)).convert()
                        boost.fill((0, 0, 0))
                        boost.blit(stripe, (stripe_x, 0))
                        boost.blit(yellow_mask.subsurface(mask_rect),
                                   (mask_rect.x - hx, mask_rect.y - hy),
                                   special_flags=pygame.BLEND_MULT)
                        factor.blit(boost, (0, 0),
                                    special_flags=pygame.BLEND_ADD)
                glossed = logo.copy()
                glossed.subsurface((hx, hy, hw, hh)).blit(
                    factor, (0, 0), special_flags=pygame.BLEND_RGB_MULT)
                screen.blit(glossed, logo_rect)
            else:
                screen.blit(logo, logo_rect)

        # --- MENU --------------------------------------------------------
        menu_el = get_element("title", "menu")
        if menu_el is not None:
            menu_el = dict(menu_el)
            menu_el["_preview_cursor"] = self.cursor
            _layout_draw_menu(screen, menu_el, self.app.fonts,
                              options=self.options)

        # --- TIP (blinks) ------------------------------------------------
        tip_el = get_element("title", "tip", **button_label_vars())
        if tip_el is not None and (not tip_el.get("blink", True)
                                   or int(self.t * 2) % 2 == 0):
            _draw_text_with_dpad(screen, tip_el, self.app.fonts)

        # --- PROFILE NAME + L1/R1 hint -----------------------------------
        # Driven by the layout system so position/font/color are editable.
        # The text template carries the L1/R1 hints inline with the
        # interpolated {profile_name}, so the whole line is one element.
        prof_el = get_element("title", "profile",
                              profile_name=self.app.profile_name)
        if prof_el is not None:
            _layout_draw_text(screen, prof_el, self.app.fonts)

        draw_layout_overlay(screen, "title", self.app.fonts, self.app.assets)

        # "OVERWRITE PROGRESS?" confirmation modal — only when New Game
        # was picked on a profile that has saved progress.
        if self._confirm_new_game:
            self._draw_confirm_new_game(screen)

        # Version stamp in the bottom-left so the player can see at a
        # glance that the auto-update pulled a fresh build. Drawn last so
        # nothing else (vignette etc.) overlays it.
        # Use the scale-2 "small" font so the version reads at arm's length
        # on the handheld — scale-1 "tiny" was legible on a desktop monitor
        # but a 3-character build stamp at 3 px tall got lost on the device.
        ver_font = self.app.fonts.get("small") or self.app.fonts["tiny"]
        # Red on UAT so the player sees at a glance they're on the master-
        # tip channel; grey on stable. App.channel is refreshed by the
        # SELECT+ability toggle so the colour flips live.
        if getattr(self.app, "channel", "stable") == "uat":
            ver_text, ver_color = f"v{VERSION} UAT", (220, 60, 60)
        else:
            ver_text, ver_color = f"v{VERSION}", DIM
        ver_surf = ver_font.render(ver_text, False, ver_color)
        ver_x, ver_y = 6, SCREEN_H - ver_surf.get_height() - 4
        screen.blit(ver_surf, (ver_x, ver_y))
        # "(X)" update hint — appears only when the background probe
        # found the active channel has something newer than what's on
        # disk. Tinted yellow to draw the eye; the silk letter is read
        # off BUTTON_SCHEME so RG / PC both show the right glyph.
        if getattr(self.app, "update_available", False):
            ab_lbl = BUTTON_SCHEME["ability"][1]
            hint = ver_font.render(f"  ({ab_lbl})", False, (255, 200, 90))
            screen.blit(hint, (ver_x + ver_surf.get_width(), ver_y))

        # Scale-mode hint, bottom-right. Only meaningful when the OS
        # display isn't already running at the native logical 640x480 —
        # on the RG (mali fullscreen at 640x480) the toggle is a no-op
        # and the line would just confuse the player. Steam Deck and
        # any PC window are larger, so they get the hint.
        if self.app.display.get_size() != (SCREEN_W, SCREEN_H):
            scale_lbl = BUTTON_SCHEME["cancel"][1]
            hint_surf = ver_font.render(
                f"{scale_lbl}: scale ({self.app.scale_mode})", False, DIM)
            screen.blit(hint_surf,
                        (SCREEN_W - hint_surf.get_width() - 6,
                         SCREEN_H - hint_surf.get_height() - 4))

        # Release-notes overlay sits on top of everything (incl. the
        # version stamp + confirm modal — we suspend everything-else
        # input while it's up, so co-existing with another modal is a
        # non-issue here).
        if self._notes is not None:
            self._draw_release_notes(screen)

    def _draw_confirm_new_game(self, screen):
        """Dim-the-screen modal: 'OVERWRITE PROGRESS?' + a face-button hint
        whose silk letters track the active platform (RG silk X/B vs
        Steam Deck silk Y/A — north confirms, south cancels)."""
        fonts = self.app.fonts
        title_font = fonts.get("big") or fonts["small"]
        body_font = fonts.get("small") or fonts["tiny"]
        title_surf = title_font.render("OVERWRITE PROGRESS?", False, YELLOW)
        prof = self.app.profile_name
        sub_surf = body_font.render(f"Profile \"{prof}\" has saved progress.",
                                    False, WHITE)
        confirm_lbl = BUTTON_SCHEME["cancel"][1]   # north face — wipes
        back_lbl    = BUTTON_SCHEME["fire"][1]     # south face — cancels
        hint_surf = body_font.render(
            f"{confirm_lbl} to confirm    {back_lbl} to cancel", False, DIM)
        # Panel sized to the widest line + padding.
        w = max(title_surf.get_width(), sub_surf.get_width(),
                hint_surf.get_width()) + 48
        h = title_surf.get_height() + sub_surf.get_height() + hint_surf.get_height() + 56
        # Full-screen dim behind the panel so the menu underneath fades.
        dim = pygame.Surface((SCREEN_W, SCREEN_H), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 160))
        screen.blit(dim, (0, 0))
        # Panel.
        panel = pygame.Surface((w, h), pygame.SRCALPHA)
        panel.fill((20, 24, 44, 235))
        pygame.draw.rect(panel, (220, 180, 80, 255), (0, 0, w, h), 2)
        y = 14
        for surf in (title_surf, sub_surf, hint_surf):
            panel.blit(surf, ((w - surf.get_width()) // 2, y))
            y += surf.get_height() + 12
        screen.blit(panel, ((SCREEN_W - w) // 2, (SCREEN_H - h) // 2))


class GameOverScreen:
    def __init__(self, app, score):
        self.app = app
        self.score = score
        self.t = 0
        self.outcome = None
        if score > app.save.high_score:
            app.save.high_score = score
            app.save.save()

    def run(self, events, controls):
        self.t += 1.0 / FPS
        if controls.confirm_pressed or controls.cancel_pressed or controls.start_pressed:
            self.outcome = ("map", None)
        screen = self.app.screen
        screen.fill(BLACK)
        vars_ = {"score": self.score, "best": self.app.save.high_score,
                 **button_label_vars()}
        for eid in ("title", "score", "best", "tip"):
            el = get_element("gameover", eid, **vars_)
            if el is None:
                continue
            if eid == "tip" and el.get("blink", True) and int(self.t * 2) % 2 != 0:
                continue
            _layout_draw_text(screen, el, self.app.fonts)
        draw_layout_overlay(screen, "gameover", self.app.fonts, self.app.assets)
        return self.outcome


# =============================================================================
# BOT REPLAY VIEWER
# =============================================================================
#
# Reader for the binary replay format written by tuning/bot/replay.py.
# Inlined here so the device-side game (which doesn't ship the tuning/
# package) can play replays without depending on that import.

REPLAYS_DIR = Path(__file__).resolve().parent / "replays"


def _replay_bits_to_controls(c, b):
    c.reset_pulses()
    c.left              = bool(b & (1 << 0))
    c.right             = bool(b & (1 << 1))
    c.up                = bool(b & (1 << 2))
    c.down              = bool(b & (1 << 3))
    c.fire              = bool(b & (1 << 4))
    c.bomb_pressed      = bool(b & (1 << 5))
    c.ability_pressed   = bool(b & (1 << 6))
    c.confirm_pressed   = bool(b & (1 << 7))
    c.cancel_pressed    = bool(b & (1 << 8))
    c.start_pressed     = bool(b & (1 << 9))
    c.select            = bool(b & (1 << 10))
    c.start             = bool(b & (1 << 11))


def _read_replay_file(path):
    """Parse a .bin replay. Returns (seed, profile, [block, ...])."""
    data = Path(path).read_bytes()
    o = 0
    if data[o:o + 4] != b"PWRP":
        raise ValueError("not a pewpew replay")
    o += 4
    version, seed = struct.unpack_from("<IQ", data, o); o += 12
    if version != 1:
        raise ValueError(f"unsupported replay version {version}")
    (prof_len,) = struct.unpack_from("<H", data, o); o += 2
    profile = data[o:o + prof_len].decode("utf-8"); o += prof_len
    blocks = []
    while o < len(data):
        (marker,) = struct.unpack_from("<I", data, o); o += 4
        if marker != 0xC001:
            raise ValueError(f"unexpected marker 0x{marker:08X} at {o}")
        level_key = data[o:o + 4].decode("ascii").strip(); o += 4
        (attempt,) = struct.unpack_from("<I", data, o); o += 4
        (meta_len,) = struct.unpack_from("<I", data, o); o += 4
        meta = json.loads(data[o:o + meta_len].decode("utf-8")); o += meta_len
        (frame_count,) = struct.unpack_from("<I", data, o); o += 4
        frames = list(struct.unpack_from(f"<{frame_count}H", data, o))
        o += 2 * frame_count
        (trailer,) = struct.unpack_from("<I", data, o); o += 4
        if trailer != 0xC002:
            raise ValueError(f"bad trailer at {o}")
        won, score = struct.unpack_from("<BI", data, o); o += 5
        blocks.append({
            "level_key": level_key,
            "attempt": attempt,
            "meta": meta,
            "frames": frames,
            "won": bool(won),
            "score": score,
        })
    return seed, profile, blocks


def _per_level_seed_for_replay(base_seed, level_key, attempt):
    """Mirror tuning.bot.session._per_level_seed — used as a fallback when a
    replay file (recorded by an older session writer) doesn't carry its own
    per_level_seed entry."""
    try:
        n = int(level_key[1:])
    except (ValueError, IndexError):
        n = 0
    s = (int(base_seed) * 2654435761) & 0xFFFFFFFF
    s ^= (n * 7919) & 0xFFFFFFFF
    s ^= (int(attempt) * 1597) & 0xFFFFFFFF
    return s & 0xFFFFFFFF


# Boss-tier unlock schedule. Each boss level (L010..L090) unlocks one
# main-weapon tier; the cascade rule means once all three main weapons
# have a given tier, side / shield / engine catch up to that tier too.
# Boss 10 (L100) is the final boss — nothing left to unlock.
_BOSS_MAIN_UNLOCKS = {
    10: ("pulse",  3), 20: ("spread", 3), 30: ("vulcan", 3),
    40: ("pulse",  4), 50: ("spread", 4), 60: ("vulcan", 4),
    70: ("pulse",  5), 80: ("spread", 5), 90: ("vulcan", 5),
}
_CASCADE_CATS = ("missile", "drone", "shield", "engine")


def _apply_boss_unlocks(save, level_key):
    """Mutate `save` to apply boss-tier unlocks + cascade. Returns the
    list of (category, new_tier) actually unlocked this call (skipping
    ones already at or beyond that tier). The returned list is the
    sequence ShopScreen should reveal one-by-one."""
    try:
        n = int(level_key[1:])
    except (ValueError, IndexError):
        return []
    info = _BOSS_MAIN_UNLOCKS.get(n)
    if info is None:
        return []
    wtype, new_tier = info
    pending = []
    attr = f"unlocked_tier_{wtype}"
    if getattr(save, attr, 2) < new_tier:
        setattr(save, attr, new_tier)
        pending.append((wtype, new_tier))
    # Cascade: side / shield / engine T(N) unlocks the instant all 3 main
    # weapons have T(N).
    main_tiers = (save.unlocked_tier_pulse,
                  save.unlocked_tier_spread,
                  save.unlocked_tier_vulcan)
    if all(t >= new_tier for t in main_tiers):
        for cat in _CASCADE_CATS:
            cat_attr = f"unlocked_tier_{cat}"
            if getattr(save, cat_attr, 2) < new_tier:
                setattr(save, cat_attr, new_tier)
                pending.append((cat, new_tier))
    return pending


def _shop_key_for_cat(cat):
    """Map a tier-unlock category (e.g. 'pulse', 'shield') to its
    SHOP_ITEMS key ('main_pulse', 'shield')."""
    if cat in ("pulse", "spread", "vulcan"):
        return f"main_{cat}"
    if cat in ("missile", "drone"):
        return f"side_{cat}"
    return cat   # shield / engine


def _unlocked_tier_for(save, shop_key):
    """Map a SHOP_ITEMS key to its unlocked-tier counter on the save.
    Returns 5 (max) for keys that don't have tier locking (bomb, ability)."""
    mapping = {
        "main_pulse":   save.unlocked_tier_pulse,
        "main_spread":  save.unlocked_tier_spread,
        "main_vulcan":  save.unlocked_tier_vulcan,
        "side_missile": save.unlocked_tier_missile,
        "side_drone":   save.unlocked_tier_drone,
        "shield":       save.unlocked_tier_shield,
        "engine":       save.unlocked_tier_engine,
    }
    return mapping.get(shop_key, 5)


def _gesture_to_profile(controls):
    """Decode L2/R2 + dpad-direction into a profile name. Returns None when
    no gesture is in flight this frame."""
    if not (controls.l2_held or controls.r2_held):
        return None
    if controls.dpad_left_pressed:    skill = "good"
    elif controls.dpad_up_pressed:    skill = "med"
    elif controls.dpad_right_pressed: skill = "bad"
    else:
        return None
    path = "avg" if controls.l2_held else "optimal"
    return f"{skill}_{path}"


def _find_replay_path(profile_name):
    """Path to replays/replay-<profile>.bin, with device fallback.

    On the dev machine all 6 profile replays sit in replays/, so each gesture
    finds its own. On the device we deploy only the longest-path "canonical"
    profile, so we fall back to whatever single replay-*.bin is present
    rather than silently no-op'ing — the user gets to see THE bot run
    regardless of which gesture they did.
    """
    p = REPLAYS_DIR / f"replay-{profile_name}.bin"
    if p.is_file():
        return p
    if not REPLAYS_DIR.is_dir():
        return None
    candidates = sorted(REPLAYS_DIR.glob("replay-*.bin"))
    return candidates[0] if candidates else None


def _replay_has_level(path, level_key):
    """True iff the replay contains a block for `level_key`. Replay files
    are ~50 KB so a full parse here costs nothing."""
    try:
        _seed, _prof, blocks = _read_replay_file(path)
    except Exception:
        return False
    target = level_key.strip()
    return any(b["level_key"].strip() == target for b in blocks)


class ReplayState:
    """Plays back a recorded bot run.

    Modes:
      single_level_key=None  → run all blocks back-to-back (title shortcut)
      single_level_key="L042" → just that one level (map shortcut)

    Per-level flow:
      shop_view (ShopScreen, user presses Y to dismiss)
        → playing (PlayState driven by recorded inputs)
        → (next block, if any) → shop_view  OR  done

    Determinism: each level seeds random with the per_level_seed recorded
    in the block's metadata (falling back to a derived seed for older
    replay files). The recorded inputs are fed straight into the same
    Controls structure the live game uses, so the playback retraces the
    bot's path frame-for-frame.
    """

    def __init__(self, app, replay_path, single_level_key=None,
                 return_to="title"):
        self._bits_to_controls = _replay_bits_to_controls

        self.app = app
        self.return_to = return_to
        self.outcome = None
        self.banner_t = 1.4         # seconds of "REPLAY: <profile>" overlay
        try:
            self.seed, self.profile, blocks = _read_replay_file(replay_path)
        except Exception as e:
            print(f"[replay] failed to read {replay_path}: {e!r}")
            self.outcome = (return_to, None)
            self.seed, self.profile, blocks = 0, "?", []

        if single_level_key:
            blocks = [b for b in blocks if b["level_key"] == single_level_key]
        self.blocks = blocks
        self.block_idx = 0
        self.frame_idx = 0
        self.play_state = None
        self.shop_state = None
        self.replay_controls = Controls()
        self.phase = "shop_view" if blocks else "done"

        if blocks:
            self._restore_save(blocks[0]["meta"].get("save", {}))
            self.shop_state = ShopScreen(app)

    # ---------- state helpers ----------

    def _restore_save(self, save_dict):
        save = self.app.save
        save.credits = int(save_dict.get("credits", save.credits))
        save.completed = list(save_dict.get("completed", save.completed))
        save.unlocked = list(save_dict.get("unlocked", save.unlocked))
        lo = save_dict.get("loadout") or {}
        for k, v in lo.items():
            if hasattr(save.loadout, k):
                try:
                    setattr(save.loadout, k, v)
                except Exception:
                    pass

    def _enter_playing(self):
        block = self.blocks[self.block_idx]
        meta = block.get("meta", {})
        seed = meta.get("per_level_seed")
        if seed is None:
            seed = _per_level_seed_for_replay(
                self.seed, block["level_key"], block.get("attempt", 1))
        random.seed(int(seed) & 0xFFFFFFFF)
        level_key = block["level_key"]
        level = self.app.levels.get(level_key)
        if level is None:
            self._advance_block()
            return
        self.play_state = PlayState(self.app, level)
        self.frame_idx = 0
        self.phase = "playing"

    def _advance_block(self):
        self.block_idx += 1
        if self.block_idx >= len(self.blocks):
            self.phase = "done"
            return
        block = self.blocks[self.block_idx]
        self._restore_save(block.get("meta", {}).get("save", {}))
        self.shop_state = ShopScreen(self.app)
        self.phase = "shop_view"

    # ---------- frame ----------

    def run(self, events, controls):
        if self.banner_t > 0:
            self.banner_t -= 1.0 / FPS

        if self.phase == "shop_view":
            # Render the real shop; ignore its outcome (we drive transitions).
            self.shop_state.run(events, controls)
            self.shop_state.outcome = None
            if controls.cancel_pressed:
                self._enter_playing()
        elif self.phase == "playing":
            block = self.blocks[self.block_idx]
            frames = block.get("frames", [])
            if self.frame_idx < len(frames):
                self._bits_to_controls(self.replay_controls,
                                       frames[self.frame_idx])
                self.frame_idx += 1
            else:
                # Out of recorded inputs: hold neutral so the PlayState
                # finishes whatever cinematic / death animation is in flight.
                self.replay_controls.reset_pulses()
                self.replay_controls.left = self.replay_controls.right = False
                self.replay_controls.up = self.replay_controls.down = False
                self.replay_controls.fire = False
            self.play_state.run(events, self.replay_controls)
            ps_done = self.play_state.outcome is not None
            frames_done = self.frame_idx >= len(frames)
            if ps_done or (frames_done and self.frame_idx > 0):
                # Force-end if we exhausted frames but the state hangs on
                # (e.g. determinism drift left enemies alive past the cap).
                self._advance_block()

        # Always-on overlay banner during playback so the user can tell.
        self._draw_overlay()

        if self.phase == "done":
            self.outcome = (self.return_to, None)
        return self.outcome

    def _draw_overlay(self):
        # Top-left "REPLAY: <profile>" pill; visible briefly then dims.
        if self.banner_t <= 0:
            return
        fonts = self.app.fonts
        font = fonts.get("small") or fonts.get(2)
        if font is None:
            return
        text = f"REPLAY · {self.profile}"
        surf = font.render(text, False, (240, 240, 240))
        pad = 4
        bg = pygame.Surface((surf.get_width() + pad * 2,
                             surf.get_height() + pad * 2), pygame.SRCALPHA)
        a = max(0, min(255, int(255 * (self.banner_t / 1.4))))
        bg.fill((20, 30, 60, min(220, a)))
        self.app.screen.blit(bg, (12, 12))
        if a < 255:
            surf.set_alpha(a)
        self.app.screen.blit(surf, (12 + pad, 12 + pad))


# =============================================================================
# APP
# =============================================================================

class App:
    def __init__(self, windowed=False):
        pygame.mixer.pre_init(22050, -16, 1, 256)
        pygame.init()
        try:
            pygame.mixer.init()
        except pygame.error:
            pass
        self.windowed = windowed
        # Detect the handheld vs a desktop dev machine. The handheld's
        # mali SDL build advertises driver name "mali" — anywhere else
        # (Windows, regular desktop Linux, macOS) we treat as PC. The
        # PC path always renders into a fixed 640x480 logical Surface
        # and scale-blits to the actual display with integer factor +
        # black letterbox bands; the device path lets pygame.SCALED do
        # native scaling on the framebuffer.
        on_device = (pygame.display.get_driver() == "mali"
                     or Path("/mnt/mmc").exists())
        # Pick the per-platform face-button scheme NOW so Controls.poll +
        # the layout chrome both see the right indices / letters from the
        # first frame onwards.
        set_button_scheme(on_device)
        try:
            desk_w, desk_h = pygame.display.get_desktop_sizes()[0]
        except Exception:
            desk_w, desk_h = 1920, 1080

        if on_device:
            # Device path: pygame.SCALED + FULLSCREEN lets the mali
            # driver handle native scaling. screen IS the display so all
            # blits go straight to the framebuffer.
            self.display = pygame.display.set_mode(
                (SCREEN_W, SCREEN_H), pygame.SCALED | pygame.FULLSCREEN)
            self.screen = self.display
        elif windowed:
            # Dev-machine windowed: open a RESIZABLE window at the
            # largest integer multiple that fits, leaving slack for
            # window chrome + taskbar.
            init_scale = max(1, min(
                (desk_w - 80) // SCREEN_W,
                (desk_h - 120) // SCREEN_H,
            ))
            init_w = SCREEN_W * init_scale
            init_h = SCREEN_H * init_scale
            self.display = pygame.display.set_mode(
                (init_w, init_h), pygame.RESIZABLE)
            self.screen = pygame.Surface((SCREEN_W, SCREEN_H))
        else:
            # Dev-machine fullscreen: own the entire desktop ourselves
            # (NOT pygame.SCALED — that path can fractional-stretch when
            # the screen's aspect ratio doesn't match 4:3). _present()
            # then picks an integer scale and letterboxes with black
            # bands, same as the windowed path.
            self.display = pygame.display.set_mode(
                (desk_w, desk_h), pygame.FULLSCREEN)
            self.screen = pygame.Surface((SCREEN_W, SCREEN_H))
        pygame.display.set_caption("Pewpew")
        pygame.mouse.set_visible(False)
        self.clock = pygame.time.Clock()
        # Dev-machine present mode: True = nearest-neighbour at the largest
        # integer multiple that fits (crisp pixels, black bands on whichever
        # axes have slack). False = nearest-neighbour at the largest aspect-
        # preserving fractional fit (fills more of the window; pixels stay
        # hard squares but some span an extra display pixel). TitleScreen
        # TAB / Y toggles this. Ignored on device (the mali fullscreen path
        # bypasses _present). Persisted via SaveData so the user's choice
        # survives a relaunch.
        self.scale_mode = SaveData.load_scale_mode()
        # Auto-update channel cache (read at boot; refreshed on toggle).
        # Drives the title version-stamp colour: stable = grey, uat = red.
        self.channel = autoupdate_channel()
        # Release-notes queue + (X) update hint. On stable, fetch every
        # release body strictly newer than the running VERSION — that's
        # the "accumulated changelog" the title overlay shows. Non-empty
        # notes = update available; the (X) hint and overlay both light
        # up. On UAT we don't have a "release" notion, so fall back to
        # the hash probe and skip the overlay (no markdown to show).
        # Sync with a 5 s timeout so a flaky connection can't stall
        # boot indefinitely.
        self.pending_release_notes = ""
        self.update_available = False
        if self.channel == "stable":
            notes = fetch_release_notes_since(VERSION)
            self.pending_release_notes = notes
            self.update_available = bool(notes)
        else:
            # UAT: background hash-probe so a slow link doesn't stall.
            threading.Thread(target=self._autoupdate_probe,
                             daemon=True).start()

        self.joys = []
        for i in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(i)
            j.init()
            self.joys.append(j)
        # Edge-detector state for synthesising D-pad ("hat") events from
        # the left analog stick. Every screen handles JOYHATMOTION for
        # menu navigation already, so feeding it stick crossings means
        # the stick "just works" everywhere with no per-screen change.
        self._stick_dir_x = 0  # -1/0/+1
        self._stick_dir_y = 0  # -1/0/+1 (already in hat convention: +1 = UP)

        self.assets = make_assets()
        # Hand the AI backdrop dictionary to BackgroundRibbon so every ribbon
        # instance can pull from it instead of building procedurally.
        BackgroundRibbon.set_backdrops(self.assets.get("_backdrops", {}))
        # Hand the projectile glyphs to Bullet so every bullet picks the
        # matching sprite at construction time.
        Bullet.set_glyphs(self.assets.get("_projectiles", {}))
        # Hand the energy-FX sprites to ExplosionRing so death bursts use them.
        ExplosionRing.set_fx(self.assets.get("_fx", {}))
        self.vignette = make_vignette()
        self.logo = self._load_title_logo()
        # Bell-curve bright stripe + yellow-pixel mask: the title-screen
        # gloss sweep only lights up the yellow areas of the logo.
        # Peak 191 = 255 - 64. The sweep brings dimmed yellow pixels (at
        # 25% brightness, factor 64) back up to 100% brightness (factor 255).
        self.title_gloss_stripe = _make_gloss_stripe(
            height=self.logo.get_height(), stripe_w=70, peak=191)
        self.title_yellow_mask = _make_yellow_mask(self.logo)
        # Pre-baked subtract layer: (191,191,191) over yellow pixels, black
        # elsewhere. Subtracting from a 255-fill dims yellow to 64 ≈ 25%.
        self.title_yellow_dim = _make_yellow_dim_layer(self.title_yellow_mask)
        if pygame.mixer.get_init():
            self.sounds = make_sounds()
            pygame.mixer.set_num_channels(16)
            self.music_channel = pygame.mixer.Channel(0)
            # Disk-cached so the ~0.6 s of music generation only happens once.
            self.music_tracks = {kind: make_music_cached(kind) for kind in MUSIC_KINDS}
        else:
            self.sounds = {k: _Silent() for k in ("shoot", "shoot2", "hit", "boom", "big_boom",
                                                  "pickup", "money", "bomb", "menu", "confirm",
                                                  "deny", "warn")}
            self.music_channel = None
            self.music_tracks = {}
        self.current_music = None
        # Hand-pixeled bitmap font with vertical gradient. Every integer scale
        # 1..7 is available as fonts[scale] (e.g. fonts[4]) plus a few named
        # aliases for backward compatibility with the rest of the codebase.
        self.fonts = {}
        for scale in range(1, 8):
            self.fonts[scale] = BitmapFont(scale=scale)
        # 7x9 family — keyed by ("7x9", scale) so the layout engine can
        # look them up without colliding with the integer 5x7 scales.
        for scale in range(1, 5):
            self.fonts[("7x9", scale)] = BitmapFont7x9(scale=scale)
        self.fonts["tiny"]  = self.fonts[1]   #  7 px tall
        self.fonts["small"] = self.fonts[2]   # 14 px
        self.fonts["big"]   = self.fonts[3]   # 21 px
        self.fonts["large"] = self.fonts[4]   # 28 px
        self.fonts["huge"]  = self.fonts[5]   # 35 px
        self.fonts["mega"]  = self.fonts[6]   # 42 px
        self.fonts["giant"] = self.fonts[7]   # 49 px
        # Floating in-world numbers (e.g. "+$25" on pickup). One size up
        # from the tiny HUD font so the pop animation reads at distance.
        FloatText.set_font(self.fonts.get(2) or self.fonts.get("small"))
        self.levels = make_levels()
        self.profile_name = SaveData.current_profile_name()
        self.save = SaveData.load(self.profile_name)
        self.volume_input = VolumeInput()
        self.sfx_bus = AudioBus(self.save.volume, label="VOL")
        self.music_bus = AudioBus(self.save.music_volume, label="MUSIC")
        self.volume_show_t = 0.0
        self.volume_show_bus = self.sfx_bus
        self._apply_sfx_volume()
        self._apply_music_volume()
        self.perf = PerfMonitor()
        self.state = TitleScreen(self)
        self.controls = Controls()

    def _load_title_logo(self):
        """Pixel-perfect: use the title sprite at exactly the size the
        editor saved (scale + crop already baked in). The fallback is the
        procedurally drawn PEWPEW text."""
        here = Path(__file__).resolve().parent
        sprite_dir = here / "art" / "sprites"
        for ext in (".bmp", ".png"):
            path = sprite_dir / f"title{ext}"
            if path.is_file():
                try:
                    return pygame.image.load(str(path)).convert_alpha()
                except Exception:
                    continue
        return make_logo("PEWPEW", scale=7, color=(120, 220, 255))

    def _apply_sfx_volume(self):
        g = self.sfx_bus.gain
        for s in self.sounds.values():
            try: s.set_volume(g)
            except Exception: pass

    def _apply_music_volume(self):
        if self.music_channel is not None:
            try: self.music_channel.set_volume(self.music_bus.gain)
            except Exception: pass

    def cycle_scale_mode(self):
        """Advance to the next entry in SCALE_MODES and persist it. Used
        by the title-screen TAB / Y handler. The grid-mask cache stays
        valid across mode changes (it's keyed by scale+size, not mode)."""
        idx = SCALE_MODES.index(self.scale_mode) if self.scale_mode in SCALE_MODES else 0
        self.scale_mode = SCALE_MODES[(idx + 1) % len(SCALE_MODES)]
        SaveData.save_scale_mode(self.scale_mode)

    def _autoupdate_probe(self):
        """Background-thread worker: hash-compare every managed file
        against the active channel and flip `update_available` so the
        title (X) hint shows up. Silent on failure — we'd rather have a
        stale False than spam the player with red herrings."""
        try:
            self.update_available = autoupdate_check_available(timeout=3)
        except Exception:
            pass

    def _grid_mask(self, scale, w, h):
        """Build (and cache) a (w, h) RGB mask whose every `scale`-th
        column and row is dimmed. Multiplied onto an integer-scaled
        screen via BLEND_RGB_MULT — gives every source pixel a 1-px dim
        right + bottom edge. scale must be >= 2 (at 1× there's no cell
        to draw an edge in).

        Cached on the App because the mask only changes when the window
        resizes (which also resizes the host display surface — same
        cache key)."""
        if not hasattr(self, "_grid_mask_cache"):
            self._grid_mask_cache = {}
        key = (scale, w, h)
        cached = self._grid_mask_cache.get(key)
        if cached is not None:
            return cached
        mask = pygame.Surface((w, h)).convert()
        mask.fill((255, 255, 255))
        # ~0.70 multiplier (178/255). Tune here if the grid feels too
        # heavy / faint. Same dim for both axes so the corner where they
        # cross ends up ~0.49 — a touch darker still, which reads as the
        # bottom-right corner of the source pixel.
        DIM = (178, 178, 178)
        for x in range(scale - 1, w, scale):
            pygame.draw.line(mask, DIM, (x, 0), (x, h - 1))
        for y in range(scale - 1, h, scale):
            pygame.draw.line(mask, DIM, (0, y), (w - 1, y))
        self._grid_mask_cache[key] = mask
        return mask

    def _present(self):
        """Push self.screen to the actual display + flip.

        Device (fullscreen): self.screen IS self.display, so this is just
        a flip — the mali driver did the upscale.

        Windowed pipeline (per SCALE_MODES):
          1. Always nearest-neighbour upscale to the largest integer
             multiple of 640x480 that fits the window.
          2. If the mode contains "grid", multiply the integer-scaled
             surface by a 1-px-per-cell dim mask (perfectly aligned to
             source pixels).
          3. If the mode contains "fill", nearest-neighbour rescale up
             to the largest aspect-preserving fractional fit.
        Then centre-blit with black bands. Pixels stay hard squares at
        every stage."""
        if self.screen is self.display:
            pygame.display.flip()
            return
        win_w, win_h = self.display.get_size()
        mode = self.scale_mode
        wants_grid = mode in ("scaled-grid", "fill-grid")
        wants_fill = mode in ("fill", "fill-grid")

        # Step 1: integer scale.
        int_scale = max(1, min(win_w // SCREEN_W, win_h // SCREEN_H))
        int_w = SCREEN_W * int_scale
        int_h = SCREEN_H * int_scale
        if int_scale == 1:
            stage = self.screen
        else:
            stage = pygame.transform.scale(self.screen, (int_w, int_h))

        # Step 2: grid filter. Only meaningful at >= 2x — at 1x every
        # output column would also be a cell edge, dimming everything.
        if wants_grid and int_scale >= 2:
            if stage is self.screen:
                stage = stage.copy()  # don't dim the source surface
            mask = self._grid_mask(int_scale, int_w, int_h)
            stage.blit(mask, (0, 0), special_flags=pygame.BLEND_RGB_MULT)

        # Step 3: optional fractional rescale up to the fill size.
        if wants_fill:
            fscale = max(1.0, min(win_w / SCREEN_W, win_h / SCREEN_H))
            fill_w = int(SCREEN_W * fscale)
            fill_h = int(SCREEN_H * fscale)
            if (fill_w, fill_h) != (int_w, int_h):
                stage = pygame.transform.scale(stage, (fill_w, fill_h))
            out_w, out_h = fill_w, fill_h
        else:
            out_w, out_h = int_w, int_h

        ox = (win_w - out_w) // 2
        oy = (win_h - out_h) // 2
        if ox > 0 or oy > 0:
            self.display.fill((0, 0, 0))
        self.display.blit(stage, (ox, oy))
        pygame.display.flip()

    def set_music(self, kind):
        """Switch the music channel to the named track. None stops playback."""
        if kind == self.current_music:
            return
        self.current_music = kind
        if self.music_channel is None:
            return
        if kind is None:
            self.music_channel.stop()
            return
        track = self.music_tracks.get(kind)
        if track is None:
            return
        self.music_channel.play(track, loops=-1)
        self.music_channel.set_volume(self.music_bus.gain)

    def switch_profile(self, name):
        """Switch the active profile slot, load its save into self.save,
        and re-apply the volume/music levels stored on the new profile.

        Only called from the title screen — by then any in-progress
        gameplay has already returned through PlayState's win/loss path
        (which saves), so we do NOT write the current save before
        switching. Writing defaults into a never-touched slot here would
        make every profile look "started" after the user simply cycled
        past it once."""
        name = (name or DEFAULT_PROFILE).upper()
        if name not in PROFILE_NAMES:
            return
        if name == self.profile_name:
            return
        self.profile_name = name
        self.save = SaveData.load(name)
        SaveData.set_current_profile(name)
        # Per-profile audio prefs: refresh the live buses so the new
        # profile's settings take effect immediately.
        self.sfx_bus.level = self.save.volume
        self.music_bus.level = self.save.music_volume
        self._apply_sfx_volume()
        self._apply_music_volume()

    def run(self):
        running = True
        select_held = False
        start_held = False
        kb_select_held = False
        perf = self.perf
        while running:
            perf.start("frame")
            perf.start("app.tick")
            dt = self.clock.tick(FPS) / 1000.0
            perf.end("app.tick")
            perf.start("app.events")
            events = pygame.event.get()
            # Synthesise JOYHATMOTION events from left-stick crossings so
            # every menu (title / map / shop / pause / etc.) can navigate
            # via the analog stick without each one growing a JOYAXISMOTION
            # branch. Edge-only: a held stick fires once on the deflection
            # rising past 0.55 and won't refire until it crosses back near
            # neutral (under 0.35), matching how the d-pad behaves.
            STICK_PUSH = 0.55
            STICK_RELEASE = 0.35
            for j in self.joys:
                try:
                    if j.get_numaxes() < 2:
                        continue
                    ax = j.get_axis(0)
                    ay = j.get_axis(1)
                except pygame.error:
                    continue
                # X axis: stick right = +1, left = -1
                nx = self._stick_dir_x
                if abs(ax) < STICK_RELEASE:
                    nx = 0
                elif ax > STICK_PUSH:
                    nx = 1
                elif ax < -STICK_PUSH:
                    nx = -1
                # Y axis: stick down (axis>0) → hat down (hy=-1); inverted.
                ny = self._stick_dir_y
                if abs(ay) < STICK_RELEASE:
                    ny = 0
                elif ay > STICK_PUSH:
                    ny = -1
                elif ay < -STICK_PUSH:
                    ny = 1
                if nx != self._stick_dir_x and nx != 0:
                    events.append(pygame.event.Event(
                        pygame.JOYHATMOTION, value=(nx, 0)))
                if ny != self._stick_dir_y and ny != 0:
                    events.append(pygame.event.Event(
                        pygame.JOYHATMOTION, value=(0, ny)))
                self._stick_dir_x = nx
                self._stick_dir_y = ny
            vol_dirs = []  # list of +1 / -1 from keyboard fallbacks
            for ev in events:
                if ev.type == pygame.QUIT:
                    running = False
                if ev.type == pygame.JOYBUTTONDOWN:
                    if ev.button == JOY_SELECT: select_held = True
                    if ev.button == JOY_START:  start_held = True
                    if ev.button == JOY_MENU:
                        # Hard exit — bypass the post-loop cleanup so the
                        # game closes immediately even if a state has set an
                        # outcome that would otherwise transition.
                        try:
                            self.save.save()
                        except Exception:
                            pass
                        pygame.quit()
                        sys.exit(0)
                if ev.type == pygame.JOYBUTTONUP:
                    if ev.button == JOY_SELECT: select_held = False
                    if ev.button == JOY_START:  start_held = False
                if ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_F4 and (pygame.key.get_mods() & pygame.KMOD_ALT):
                        running = False
                    if ev.key in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
                        vol_dirs.append(+1)
                    elif ev.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                        vol_dirs.append(-1)
                    elif ev.key == pygame.K_LSHIFT:
                        kb_select_held = True
                if ev.type == pygame.KEYUP:
                    if ev.key == pygame.K_LSHIFT:
                        kb_select_held = False
                if ev.type == pygame.VIDEORESIZE and self.screen is not self.display:
                    # Dev-machine resize: re-create the window at the new
                    # size. The logical screen Surface stays at SCREEN_W
                    # × SCREEN_H — _present() picks a new integer scale
                    # and recentres on the next frame.
                    self.display = pygame.display.set_mode(
                        (max(SCREEN_W, ev.w), max(SCREEN_H, ev.h)),
                        pygame.RESIZABLE)

            if select_held and start_held:
                running = False
            perf.end("app.events")

            # Pump hardware volume events from /dev/input.
            for d in self.volume_input.poll():
                vol_dirs.append(d)

            # Route each volume event: SELECT held -> music bus, else SFX.
            music_modifier = select_held or kb_select_held
            for d in vol_dirs:
                bus = self.music_bus if music_modifier else self.sfx_bus
                if bus.adjust(d):
                    if bus is self.sfx_bus:
                        self._apply_sfx_volume()
                        self.save.volume = bus.level
                    else:
                        self._apply_music_volume()
                        self.save.music_volume = bus.level
                    self.save.save()
                    # Subtle tick at the bus's current level so the user
                    # can hear what the adjustment did.
                    try:
                        self.sounds["menu"].play()
                    except Exception:
                        pass
                self.volume_show_t = 1.6
                self.volume_show_bus = bus

            if self.volume_show_t > 0:
                self.volume_show_t = max(0.0, self.volume_show_t - dt)

            self.controls.poll(self.joys, events)
            perf.start("app.state")
            outcome = self.state.run(events, self.controls)
            perf.end("app.state")
            if outcome is not None:
                kind, payload = outcome
                self._transition(kind, payload)

            # State-based music selection. Boss intro / outro use the boss
            # track; standard play uses the game track; everything else menu.
            self._update_music_track()

            if self.volume_show_t > 0:
                self._draw_volume_indicator()
            perf.start("app.flip")
            self._present()
            perf.end("app.flip")
            perf.end("frame")
            perf.frame_end()

        self.volume_input.close()
        self.save.save()
        pygame.quit()

    def _update_music_track(self):
        s = self.state
        if isinstance(s, PlayState):
            if s.intro_t > 0:
                self.set_music("takeoff")
            elif s.outro_t > 0:
                self.set_music("dock")
            elif s.level.has_boss and s.boss_spawned:
                self.set_music("boss")
            else:
                self.set_music("game")
        else:
            self.set_music("menu")

    def _draw_volume_indicator(self):
        """Pip-bar showing the bus that was last adjusted."""
        bus = self.volume_show_bus
        is_music = bus is self.music_bus
        w, h = 240, 32
        x = (SCREEN_W - w) // 2
        y = SCREEN_H - h - 16
        alpha = min(1.0, self.volume_show_t / 1.0)
        bg = pygame.Surface((w, h), pygame.SRCALPHA)
        bg.fill((18, 22, 38, int(220 * alpha)))
        self.screen.blit(bg, (x, y))
        border = (200, 160, 110) if is_music else (110, 160, 220)
        pygame.draw.rect(self.screen, border, (x, y, w, h), 1)
        label = self.fonts["tiny"].render(bus.label, False, border)
        self.screen.blit(label, (x + 8, y + h // 2 - label.get_height() // 2))
        cells = 10
        bar_x = x + 52
        bar_w = w - 64
        cell_w = max(2, (bar_w - (cells - 1)) // cells)
        filled = int(round(bus.level * cells))
        fill_col = ORANGE if is_music else CYAN
        for i in range(cells):
            cell = pygame.Rect(bar_x + i * (cell_w + 1), y + 10, cell_w, 12)
            pygame.draw.rect(self.screen, (40, 46, 70), cell)
            if i < filled:
                pygame.draw.rect(self.screen, fill_col, cell.inflate(-2, -2))

    def _restore_save_after_replay(self):
        if hasattr(self, "_replay_save_backup"):
            self.save = self._replay_save_backup
            del self._replay_save_backup

    def _transition(self, kind, payload):
        if kind == "play":
            level = payload
            self.state = PlayState(self, level)
        elif kind == "title":
            self._restore_save_after_replay()
            self.state = TitleScreen(self)
        elif kind == "map":
            self._restore_save_after_replay()
            self.state = MapScreen(self)
        elif kind == "shop":
            self.state = ShopScreen(self)
        elif kind == "gameover":
            self.state = GameOverScreen(self, payload or 0)
        elif kind == "quit":
            self.save.save()
            pygame.quit()
            sys.exit(0)
        elif kind == "post_play":
            score, level_key, won, progress = payload
            self.save.high_score = max(self.save.high_score, score)
            # Adaptive per-level difficulty knob (stored as a float).
            # Death decrement = 0.5 + 0.5 * level_progress: dying at the
            # very start barely moves it, dying right at the end gives a
            # full -1.0. A finish HALVES the current value toward 0, so
            # a replay right after a win still feels slightly easier
            # than a brand-new untouched level — keeps the help fading
            # gradually instead of snapping back to baseline. Downstream
            # truncates to int when applying, so -0.5 -> 0, -1.0 -> -1.
            adj_map = self.save.level_difficulty_adjust
            cur = float(adj_map.get(level_key, 0.0))
            if won:
                adj_map[level_key] = cur / 2.0
            else:
                decrement = 0.5 + 0.5 * max(0.0, min(1.0, float(progress)))
                adj_map[level_key] = cur - decrement
            if won:
                if level_key not in self.save.completed:
                    self.save.completed.append(level_key)
                for nxt in MAP_GRAPH[level_key].nexts:
                    if nxt not in self.save.unlocked:
                        self.save.unlocked.append(nxt)
                pending_unlocks = _apply_boss_unlocks(self.save, level_key)
                self.save.save()
                self.state = ShopScreen(self, pending_unlocks=pending_unlocks)
            else:
                self.save.save()
                self.state = GameOverScreen(self, score)
        elif kind == "replay_full":
            profile_name = payload
            path = _find_replay_path(profile_name)
            if path is None:
                print(f"[replay] no replay file for profile {profile_name}")
                return
            # Stash the real save and use a throwaway one so the replay's
            # restore_save calls can't trash player progress.
            self._replay_save_backup = self.save
            self.save = SaveData()
            self.save.save = lambda *a, **kw: None
            self.state = ReplayState(self, path, single_level_key=None,
                                     return_to="title")
        elif kind == "replay_level":
            profile_name, level_key = payload
            path = _find_replay_path(profile_name)
            if path is None:
                print(f"[replay] no replay file for profile {profile_name}")
                return
            self._replay_save_backup = self.save
            self.save = SaveData()
            self.save.save = lambda *a, **kw: None
            self.state = ReplayState(self, path, single_level_key=level_key,
                                     return_to="map")


# Tie PlayState outcome back into App transitions
_orig_play_run = PlayState.run


def _play_run(self, events, controls):
    out = _orig_play_run(self, events, controls)
    if out is None:
        return None
    # Hidden test mission: never mutate save / map graph. Restart on L3,
    # otherwise drop back to the title screen.
    if getattr(self.level, "is_test", False):
        if out == "test_restart":
            return ("play", make_test_level())
        return ("title", None)
    # Level progress 0..1 — elapsed / time of the last timeline spawn
    # event. Used by the adaptive-difficulty knob so dying early in a
    # level gives a smaller adjust decrement than dying near the end.
    tl = getattr(self.level, "timeline", None) or ()
    last_t = max((t for t, _ in tl), default=0.0)
    progress = (max(0.0, min(1.0, self.elapsed / last_t))
                if last_t > 0 else (1.0 if out == "win" else 0.0))
    if out == "win":
        return ("post_play", (self.score, self.level.key, True, 1.0))
    if out == "loss":
        return ("post_play", (self.score, self.level.key, False, progress))
    return None


PlayState.run = _play_run


# Single-instance guard. The stock OS App Center on the RG35XX Pro scans
# /mnt/mmc/Roms/APPS recursively, so it picks up BOTH the outer Pewpew.sh
# entry and the inner Pewpew/launch.sh as separate launcher entries.
# Without this lock, two python instances can end up running on top of
# each other — exiting one reveals the other, which feels like the first
# exit only goes back to the title screen.
LOCK_PATH = Path(__file__).resolve().parent / ".pewpew.pid"


def _acquire_single_instance_lock():
    try:
        existing = int(LOCK_PATH.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        existing = None
    if existing and existing != os.getpid():
        try:
            os.kill(existing, 0)
            # Another live pewpew — bail without touching pygame so the
            # foreground instance is undisturbed.
            print(f"pewpew already running (pid {existing}); exiting.")
            sys.exit(0)
        except (ProcessLookupError, OSError):
            pass  # stale lock — fall through and claim it
    try:
        LOCK_PATH.write_text(str(os.getpid()))
    except OSError:
        pass


def _release_single_instance_lock():
    try:
        if LOCK_PATH.read_text().strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except (FileNotFoundError, OSError, ValueError):
        pass


def main():
    # No auto-update at launch. The title screen surfaces an "UPDATE
    # AVAILABLE" overlay when the active channel has anything newer
    # than the running build; the player presses ability (silk X) on
    # the title to opt in. See `_check_release_update` (force=True
    # path) for the apply side, and TitleScreen's release-notes
    # overlay for the surface.
    if BOT_CLI["bot"]:
        from tuning.bot.session import run_bot_from_cli
        run_bot_from_cli(BOT_CLI)
        return
    if BOT_CLI["replay"]:
        from tuning.bot.replay import play_replay_from_cli
        play_replay_from_cli(BOT_CLI)
        return
    _acquire_single_instance_lock()
    try:
        windowed = "--windowed" in sys.argv
        App(windowed=windowed).run()
    finally:
        _release_single_instance_lock()


if __name__ == "__main__":
    main()
