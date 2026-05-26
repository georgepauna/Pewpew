#!/usr/bin/env python3
"""Pewpew - a Tyrian-style vertical shooter for the RG35XX Pro.

Single-file, no external assets. Sprites and sounds are generated in code.
Branching mission map, weapon upgrades, abilities, varied enemies.
"""

import array
import json
import math
import os
import random
import struct
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

os.environ.setdefault("SDL_VIDEO_CENTERED", "1")

import pygame


# =============================================================================
# CONSTANTS
# =============================================================================

SCREEN_W, SCREEN_H = 640, 480
PLAY_W = 480
PLAY_H = 480
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
JOY_MENU = 13     # device home/menu button — quits the game

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
MAIN_WEAPON_MAX = 5
SIDE_WEAPON_MAX = 3
# First-purchase cost when the weapon is not yet owned (level 0 → level 1).
MAIN_BUY_COST = 1500
SIDE_BUY_COST = 800
# Cost to go FROM level i TO level i+1, indexed by current level (1-based).
MAIN_UPGRADE_COSTS = {
    "pulse":  [0, 400, 900, 1600, 2600],
    "spread": [0, 450, 1000, 1700, 2700],
    "vulcan": [0, 500, 1100, 1800, 2800],
}
SIDE_UPGRADE_COSTS = {
    "missile": [0, 600, 1400, 2800],
    "drone":   [0, 700, 1500, 3000],
}

# Equipment that is just leveled (no type selection).
WEAPON_COSTS = {
    "shield": [0, 350, 800, 1500, 2400],   # max level 5
    "engine": [0, 500, 1200],              # max level 3
}
MAX_LEVELS = {"shield": 5, "engine": 3}
BOMB_PRICE = 250

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
    # Equipped weapon types and their per-type levels. Level 0 means "not owned".
    main_type: str = "pulse"
    main_pulse: int = 1
    main_spread: int = 0
    main_vulcan: int = 0
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

    @staticmethod
    def load():
        try:
            raw = json.loads(SAVE_PATH.read_text())
            # Detect the old 5-level key format and reset to the new layout.
            unlocked = raw.get("unlocked") or []
            if unlocked and not all(isinstance(k, str) and k.startswith("L") for k in unlocked):
                return SaveData()
            raw_loadout = raw.pop("loadout", {}) or {}
            # Migrate pre-Tyrian-refactor saves: old `main`/`side` integer levels
            # become per-type levels under the default equipped types.
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
            # Drop any unknown keys so a stale save can't crash the dataclass.
            allowed = set(Loadout.__dataclass_fields__.keys())
            raw_loadout = {k: v for k, v in raw_loadout.items() if k in allowed}
            loadout = Loadout(**raw_loadout)
            return SaveData(loadout=loadout, **raw)
        except Exception:
            return SaveData()

    def save(self):
        try:
            data = asdict(self)
            SAVE_PATH.write_text(json.dumps(data, indent=2))
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

    def draw(self, surf):
        surf_w = surf.get_width()
        surf_h = surf.get_height()
        # Centre horizontally if the layer is wider than the target surface
        # (happens after remake_native_aspect_h(fit_h=None) — the bg ends up
        # 256*mirror_n = 768 px wide while the playfield/screen is narrower).
        x0 = -(self.width - surf_w) // 2 if self.width > surf_w else 0
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


def _draw_dpad_icon(surf, x, y, scale=1, color=(255, 255, 255)):
    """Draw a square D-pad cross at (x, y) - symmetric 7x7 plus with
    3-cell-thick arms. Both arms are equal length (7 cells / 7*scale px),
    so the shape reads as an unmistakable + at any scale."""
    # Vertical arm: cols 2..4 (3 wide), rows 0..6 (full 7 tall).
    pygame.draw.rect(surf, color, (x + 2 * scale, y, 3 * scale, 7 * scale))
    # Horizontal arm: cols 0..6 (full 7 wide), rows 2..4 (3 tall).
    pygame.draw.rect(surf, color, (x, y + 2 * scale, 7 * scale, 3 * scale))


def _glyph_to_surface(pattern, scale, color):
    """Render a 5x7 glyph at the given scale, applying a vertical gradient.
    Top of the glyph is ~15% brighter than the base, bottom is ~45% darker;
    interpolated smoothly across the pixel rows so larger sizes show the
    shading as a proper bevel."""
    rows = pattern.split("/")
    h_rows = len(rows)
    w = 5 * scale
    h = h_rows * scale
    s = pygame.Surface((w, h), pygame.SRCALPHA)
    has_alpha = len(color) >= 4
    base_a = color[3] if has_alpha else 255
    br, bg, bb = color[0], color[1], color[2]
    # Pre-compute one color per pixel-y so glyph blocks share row colors.
    row_colors = []
    for py in range(h):
        t = py / max(1, h - 1)
        # Slight peak just below the top, gentle fall-off, harder darkening
        # near the bottom. Tuned to look like a beveled retro font.
        if t < 0.25:
            factor = 1.05 + (1.18 - 1.05) * (t / 0.25)
        else:
            factor = 1.18 - (1.18 - 0.55) * ((t - 0.25) / 0.75)
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

    def __init__(self, scale=2):
        self.scale = scale
        self.advance = (self.BASE_W + self.SPACING) * scale
        self.line_height = self.BASE_H * scale
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

    def size(self, text):
        return (max(1, len(text) * self.advance - self.scale), self.line_height)

    def render(self, text, antialias, color, background=None):
        text = str(text)
        cache_key = None
        if background is None and len(text) <= 48:
            cache_key = (text, color[0], color[1], color[2])
            cached = self._render_cache.get(cache_key)
            if cached is not None:
                return cached
        glyphs = self._glyphs(color)
        chars = list(text)
        total_w = max(1, len(chars) * self.advance - self.scale)
        surf = pygame.Surface((total_w, self.line_height), pygame.SRCALPHA)
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
                 "rect", "damage", "pierce", "sprite")

    def __init__(self, x, y, vx, vy, color, friendly=True, size=(3, 7), damage=1, pierce=0):
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

    def __init__(self, x, y, target_ref, color=(255, 200, 80)):
        super().__init__(x, y, 0, -200, color, friendly=True, size=(4, 9), damage=2)
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
        self.damage_per_sec = 80
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

ENGINE_SPEEDS = {1: 200, 2: 260, 3: 320}
SHIELD_MAX = {1: 20, 2: 30, 3: 40, 4: 55, 5: 75}
SHIELD_REGEN = {1: 1.5, 2: 2.0, 3: 2.5, 4: 3.5, 5: 5.0}


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

# Front-weapon fire rates (seconds between shots) keyed by type, then level.
MAIN_FIRE_RATE_BY_TYPE = {
    "pulse":  {1: 0.18, 2: 0.16, 3: 0.14, 4: 0.12, 5: 0.10},
    "spread": {1: 0.22, 2: 0.20, 3: 0.18, 4: 0.16, 5: 0.14},
    "vulcan": {1: 0.10, 2: 0.085, 3: 0.075, 4: 0.065, 5: 0.055},
}
# Sidekick fire rates by type, then level.
SIDE_FIRE_RATE_BY_TYPE = {
    "missile": {1: 1.6, 2: 1.3, 3: 1.0},
    "drone":   {1: 0.45, 2: 0.36, 3: 0.28},
}

# Bullet patterns per main-weapon type: {level: [(off_x, off_y, vx, vy), ...]}.
# Sizes/colors are baked into the fire dispatcher per weapon kind.
PULSE_PATTERNS = {
    1: [(0, 0, 0, -500)],
    2: [(-5, 0, 0, -520), (5, 0, 0, -520)],
    3: [(0, 0, 0, -540), (-6, 3, -80, -520), (6, 3, 80, -520)],
    4: [(-9, 0, 0, -560), (-3, 0, 0, -560), (3, 0, 0, -560), (9, 0, 0, -560)],
    5: [(-9, 0, 0, -580), (-3, 0, 0, -580), (3, 0, 0, -580), (9, 0, 0, -580),
        (-12, 3, -160, -500), (12, 3, 160, -500)],
}
SPREAD_PATTERNS = {
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
VULCAN_PATTERNS = {
    1: [(0, 0, 0, -620)],
    2: [(-3, 0, 0, -640), (3, 0, 0, -640)],
    3: [(-4, 0, -40, -660), (0, 0, 0, -660), (4, 0, 40, -660)],
    4: [(-6, 0, -50, -680), (-2, 0, -10, -680),
        (2, 0, 10, -680), (6, 0, 50, -680)],
    5: [(-8, 0, -70, -700), (-3, 0, -20, -700),
        (0, 0, 0, -700),
        (3, 0, 20, -700), (8, 0, 70, -700)],
}
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

        # Fire main weapon
        self.cooldown_main -= dt
        self.cooldown_side -= dt
        if controls.fire and self.cooldown_main <= 0:
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
        # Vulcan deals less per bullet, spread deals slightly more, pulse scales with level.
        if mtype == "vulcan":
            dmg = 1
        elif mtype == "spread":
            dmg = 1 + (1 if lvl >= 4 else 0)
        else:  # pulse
            dmg = 2 if lvl >= 5 else 1
        for off_x, off_y, vx, vy in MAIN_PATTERNS[mtype][lvl]:
            bullets.append(Bullet(cx + off_x * PLAY_SCALE, cy + off_y * PLAY_SCALE,
                                  vx, vy, color, size=size, damage=dmg))
        sounds["shoot"].play()

    def _fire_side(self, bullets, enemies_ref, sounds):
        stype = self.loadout.side_type
        if stype == "none":
            return
        lvl = self.loadout.side_level()
        if lvl <= 0:
            return
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
            for i in range(lvl):
                target = targets[i % len(targets)]
                ref = (lambda t: (lambda: t if t.alive else None))(target)
                tx, ty = mleft if i % 2 == 0 else mright
                bullets.append(Missile(tx, ty, ref))
            sounds["shoot2"].play()
        elif stype == "drone":
            # Twin drones flank the ship and fire straight bullets. More levels =
            # extra drones / faster shots (fire-rate handled in update).
            shots = lvl  # 1, 2 or 3 drone shots per volley
            dummies = [
                ("drone_left",  (cx_def + -16 * PLAY_SCALE, cy_def + -2 * PLAY_SCALE)),
                ("drone_right", (cx_def +  16 * PLAY_SCALE, cy_def + -2 * PLAY_SCALE)),
                ("drone_top",   (cx_def,                    cy_def + -8 * PLAY_SCALE)),
            ][:shots]
            for name, default in dummies:
                px, py = self._dummy_pos(name, default)
                bullets.append(Bullet(px, py, 0, -560,
                                      (180, 220, 255), size=(2, 6), damage=1))
            sounds["shoot2"].play()

    def _use_ability(self, bullets, enemies_ref, particles, sounds, lasers):
        if self.loadout.ability == "screen_clear":
            for e in enemies_ref():
                e.hp -= 4
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

    def collect(self, pickup):
        k = pickup.kind
        if k == "money":
            return ("credits", 50)
        if k == "main":
            mtype = self.loadout.main_type
            lvl = self.loadout.main_level()
            if lvl < MAIN_WEAPON_MAX:
                setattr(self.loadout, f"main_{mtype}", lvl + 1)
            else:
                return ("credits", 200)
        if k == "side":
            stype = self.loadout.side_type
            if stype == "none":
                # First side pickup grants a basic missile.
                self.loadout.side_type = "missile"
                self.loadout.side_missile = max(1, self.loadout.side_missile)
            else:
                lvl = self.loadout.side_level()
                if lvl < SIDE_WEAPON_MAX:
                    setattr(self.loadout, f"side_{stype}", lvl + 1)
                else:
                    return ("credits", 200)
        if k == "shield":
            self.shield_hp = min(self.shield_max, self.shield_hp + 10)
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
        # Shield ring shrinks with the ship so it still hugs the silhouette.
        if self.shield_hp > 0 and (self.invuln > 0 or self.shield_recharge_delay < 0.3):
            r = max(sprite_rect.w, sprite_rect.h) // 2 + 4
            pygame.draw.circle(surf, CYAN, self.rect.center, r, 1)


# =============================================================================
# ENEMIES
# =============================================================================

class Enemy:
    SCORE = 10
    CREDITS = 10
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
    CREDITS = 12
    DROP_CHANCE = 0.06

    def __init__(self, x, asset, flash):
        super().__init__(x, -20, asset, hp=1, flash_asset=flash)
        self.speed = random.uniform(130, 170)

    def _move(self, dt):
        self.y += self.speed * dt
        self.x += math.sin(self.t * 2 + self.x) * 30 * dt


class Gunner(Enemy):
    SCORE = 40
    CREDITS = 30
    DROP_CHANCE = 0.12
    DROP_TABLE = ("money", "money", "shield")

    def __init__(self, x, asset, flash):
        super().__init__(x, -24, asset, hp=3, flash_asset=flash)
        self.speed = 80
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
    CREDITS = 20
    DROP_CHANCE = 0.18
    DROP_TABLE = ("main", "side", "money")

    def __init__(self, x, asset, flash):
        super().__init__(x, -20, asset, hp=2, flash_asset=flash)
        self.base_x = x
        self.speed = 100

    def _move(self, dt):
        self.y += self.speed * dt
        self.x = self.base_x + math.sin(self.t * 3) * 80
        self.x = clamp(self.x, 20, PLAY_W - 20)


class Bomber(Enemy):
    SCORE = 80
    CREDITS = 60
    DROP_CHANCE = 0.25
    DROP_TABLE = ("main", "side", "shield", "bomb", "money")

    def __init__(self, x, asset, flash):
        super().__init__(x, -30, asset, hp=8, flash_asset=flash)
        self.speed = 50

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
    CREDITS = 25
    DROP_CHANCE = 0.10

    def __init__(self, x, asset, flash):
        super().__init__(x, -20, asset, hp=2, flash_asset=flash)
        self.acquired = False
        self.vx = 0
        self.vy = 80

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
    CREDITS = 40
    DROP_CHANCE = 0.20
    DROP_TABLE = ("shield", "main", "bomb")

    def __init__(self, x, asset, flash):
        super().__init__(x, -24, asset, hp=5, flash_asset=flash)
        self.stop_y = random.uniform(40, 100)
        self.speed = 60

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
    CREDITS = 3
    DROP_TABLE = ("money",)
    DROP_CHANCE = 0.05

    def __init__(self, x, asset, flash):
        super().__init__(x, -20, asset, hp=1, flash_asset=flash)
        self.speed = random.uniform(60, 110)
        self.drift = random.uniform(-25, 25)

    def _move(self, dt):
        self.y += self.speed * dt
        self.x += self.drift * dt


class BigAsteroid(Enemy):
    """Bigger rock - takes more hits, drops something useful."""
    SCORE = 25
    CREDITS = 18
    DROP_TABLE = ("money", "shield", "bomb")
    DROP_CHANCE = 0.20

    def __init__(self, x, asset, flash):
        super().__init__(x, -30, asset, hp=4, flash_asset=flash)
        self.speed = random.uniform(40, 70)
        self.drift = random.uniform(-18, 18)

    def _move(self, dt):
        self.y += self.speed * dt
        self.x += self.drift * dt


class Mine(Enemy):
    """Floating mine - wobbles, doesn't shoot, explodes on death damaging nearby player."""
    SCORE = 20
    CREDITS = 12
    DROP_TABLE = ()
    DROP_CHANCE = 0.0
    EXPLOSION_RADIUS = 60
    EXPLOSION_DAMAGE = 6

    def __init__(self, x, asset, flash):
        super().__init__(x, -20, asset, hp=2, flash_asset=flash)
        self.speed = random.uniform(35, 55)

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
    CREDITS = 45
    DROP_TABLE = ("shield", "main", "money", "bomb")
    DROP_CHANCE = 0.25

    def __init__(self, x, asset, flash):
        super().__init__(x, -50, asset, hp=10, flash_asset=flash)
        self.speed = 55


class Crystal(Enemy):
    """Rare cargo crystal. Modest HP, drops a powerup with high probability."""
    SCORE = 60
    CREDITS = 35
    DROP_TABLE = ("main", "side", "shield", "bomb")
    DROP_CHANCE = 0.70

    def __init__(self, x, asset, flash):
        super().__init__(x, -25, asset, hp=2, flash_asset=flash)
        self.speed = random.uniform(50, 80)


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
        super().__init__(x, -asset.get_height() // 2, asset, hp=999, flash_asset=flash)
        self.speed = 60

    def _move(self, dt):
        self.y += self.speed * dt

    def hit(self, dmg):
        # Walls can't be killed; they just spark.
        self.hit_flash_t = 0.06
        return False


class Boss(Enemy):
    SCORE = 2000
    CREDITS = 800
    DROP_CHANCE = 1.0
    DROP_TABLE = ("main", "side", "shield", "bomb")

    def __init__(self, asset, flash=None, hp_mul=1.0):
        x = PLAY_W // 2
        super().__init__(x, -120, asset, hp=int(240 * hp_mul), flash_asset=flash)
        self.speed = 60
        self.hp_mul = hp_mul
        self.phase = 0
        self.dwell = 0
        self.pattern_cd = 1.0
        self.sweep_dir = 1

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
            self.pattern_cd = [1.2, 0.9, 0.6][self.phase]
            self._fire_pattern(bullets, player_ref())

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
            for ang in range(-60, 61, 12):
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
        bar_w = PLAY_W - 40
        ratio = max(0.0, self.hp / self.max_hp)
        pygame.draw.rect(surf, DARKER, (20, 8, bar_w, 6))
        pygame.draw.rect(surf, RED, (20, 8, int(bar_w * ratio), 6))
        pygame.draw.rect(surf, WHITE, (20, 8, bar_w, 6), 1)


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


def _scale_enemy(e, state):
    """Apply level difficulty to a freshly-spawned enemy's HP."""
    mul = getattr(state, "difficulty", 1.0)
    if mul != 1.0 and not isinstance(e, Boss):
        e.hp = max(1, int(e.hp * mul))
        e.max_hp = e.hp


def spawn_line(kind, count, gap=50, y_off=0):
    def fn(state):
        total = (count - 1) * gap
        start_x = (PLAY_W - total) / 2
        for i in range(count):
            e = _enemy_factory(kind, start_x + i * gap, state.assets)
            e.y += y_off
            _scale_enemy(e, state)
            state.enemies.append(e)
    return fn


def spawn_v(kind, count):
    def fn(state):
        for i in range(count):
            x = PLAY_W // 2 + (i - count // 2) * 40
            e = _enemy_factory(kind, x, state.assets)
            e.y = -30 - abs(i - count // 2) * 30
            _scale_enemy(e, state)
            state.enemies.append(e)
    return fn


def spawn_random(kind, count, x_range=(40, PLAY_W - 40)):
    def fn(state):
        for _ in range(count):
            x = random.uniform(*x_range)
            e = _enemy_factory(kind, x, state.assets)
            _scale_enemy(e, state)
            state.enemies.append(e)
    return fn


def spawn_at(kind, x):
    def fn(state):
        e = _enemy_factory(kind, x, state.assets)
        _scale_enemy(e, state)
        state.enemies.append(e)
    return fn


def spawn_boss(hp_mul=1.0):
    def fn(state):
        sec = getattr(state.level, "sector_idx", 0)
        key = f"boss_{sec}"
        if key not in state.assets:
            key = "boss"
        flash = state.assets.get(f"{key}_flash") or state.assets.get("boss_flash")
        b = Boss(state.assets[key], flash, hp_mul=hp_mul)
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
        for i in range(count):
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
            e = _enemy_factory(kind, x, state.assets)
            e.y = -30 - i * 50
            _scale_enemy(e, state)
            state.enemies.append(e)
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
                    sa(state); sb(state)
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
        # Difficulty multiplies enemy HP. 1.0 at L1, scales toward ~3.5 by L100.
        difficulty = 1.0 + (n - 1) * 0.025
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
            b = Boss(asset, flash, hp_mul=1.0)
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

    def reset_pulses(self):
        self.bomb_pressed = False
        self.ability_pressed = False
        self.confirm_pressed = False
        self.cancel_pressed = False
        self.start_pressed = False

    def poll(self, joys, events):
        self.reset_pulses()
        keys = pygame.key.get_pressed()
        self.left = keys[pygame.K_LEFT]
        self.right = keys[pygame.K_RIGHT]
        self.up = keys[pygame.K_UP]
        self.down = keys[pygame.K_DOWN]
        self.fire = keys[pygame.K_z] or keys[pygame.K_SPACE]
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
                if JOY_B < j.get_numbuttons() and j.get_button(JOY_B):
                    self.fire = True
                if JOY_SELECT < j.get_numbuttons():
                    self.select = bool(j.get_button(JOY_SELECT))
                if JOY_START < j.get_numbuttons():
                    self.start = bool(j.get_button(JOY_START))
            except pygame.error:
                pass

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
            if ev.type == pygame.JOYBUTTONDOWN:
                if ev.button == JOY_A:
                    self.bomb_pressed = True
                if ev.button == JOY_X:
                    self.ability_pressed = True
                if ev.button == JOY_B:
                    self.confirm_pressed = True
                if ev.button == JOY_Y:
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
    drawn live on top each frame and never go into the cache. Cuts
    draw.hud from ~2 ms to ~0.3 ms on the RG35XX Pro."""
    surface = None
    key = None


def _hud_cache_key(player, level_name):
    lo = player.loadout
    side_lvl = lo.side_level() if lo.side_type != "none" else 0
    return (level_name, lo.main_type, lo.main_level(),
            lo.side_type, side_lvl, lo.shield, lo.engine,
            lo.bombs, lo.ability, _LAYOUT_REV)


def _hud_panel(id_, x, y, w, h, *, title="", children=()):
    """One bordered HUD chrome panel (the look produced by _panel) as a
    container spec. Used by _build_hud_layout_spec to keep the HUD tree
    declaration readable."""
    return {
        "id": id_, "type": "container",
        "x": x, "y": y, "w": w, "h": h,
        "layout": "free",
        "bg": [22, 26, 44],
        "border": [60, 80, 130],
        "border_width": 1,
        "caps": True, "caps_color": [110, 160, 220], "caps_length": 5,
        "title": title, "title_color": [160, 200, 240], "title_font": 1,
        "padding": 0,
        "children": list(children),
    }


def _hud_lvl_bar_x_y_w(panel_inner_w, inset=0):
    """Geometry helper for the loadout level pips — keeps the lambdas in
    the spec compact."""
    return 8 + inset, 6 - 1, panel_inner_w - 16 - inset


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
        {"id": "loadout_main_bar", "type": "progress_bar",
         "x": 8, "y": PAD + LH + 1, "w": INNER - 16, "h": 6,
         "value": "{main_lvl}", "max": "{main_max}",
         "segments": "{main_max}",
         "color": "{main_lvl_color}", "bg_color": [60, 64, 88]},
        {"id": "loadout_side_name", "type": "text",
         "x": 8, "y": PAD + LH * 2, "anchor": "tl",
         "text": "{side_name}", "font": 1, "color": [255, 140, 40]},
        {"id": "loadout_side_bar", "type": "progress_bar",
         "x": 8, "y": PAD + LH * 3 + 1, "w": INNER - 16, "h": 6,
         "value": "{side_lvl}", "max": "{side_max}",
         "segments": "{side_max}",
         "color": "{side_lvl_color}", "bg_color": [60, 64, 88],
         "visible_when": "side_visible"},
        # Shield + Engine rows: label on the left, pip bar on the right.
        {"id": "loadout_shld_label", "type": "text",
         "x": 8, "y": PAD + LH * 4, "anchor": "tl",
         "text": "SHLD", "font": 1, "color": [140, 140, 160]},
        {"id": "loadout_shld_bar", "type": "progress_bar",
         "x": 40, "y": PAD + LH * 4 + 1, "w": INNER - 46, "h": 6,
         "value": "{shield_lvl}", "max": "{shield_max}",
         "segments": "{shield_max}",
         "color": "{shield_lvl_color}", "bg_color": [60, 64, 88]},
        {"id": "loadout_engn_label", "type": "text",
         "x": 8, "y": PAD + LH * 5, "anchor": "tl",
         "text": "ENGN", "font": 1, "color": [140, 140, 160]},
        {"id": "loadout_engn_bar", "type": "progress_bar",
         "x": 40, "y": PAD + LH * 5 + 1, "w": INNER - 46, "h": 6,
         "value": "{engine_lvl}", "max": "{engine_max}",
         "segments": "{engine_max}",
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
         "text": "B", "font": 1, "color": [80, 220, 255]},
        {"id": "ctrl_b_label", "type": "text",
         "x": 32, "y": PAD + LH, "anchor": "tl",
         "text": "fire", "font": 1, "color": [140, 140, 160]},
        {"id": "ctrl_a", "type": "text",
         "x": 8, "y": PAD + LH * 2, "anchor": "tl",
         "text": "A", "font": 1, "color": [80, 220, 255]},
        {"id": "ctrl_a_label", "type": "text",
         "x": 32, "y": PAD + LH * 2, "anchor": "tl",
         "text": "bomb", "font": 1, "color": [140, 140, 160]},
        {"id": "ctrl_x", "type": "text",
         "x": 8, "y": PAD + LH * 3, "anchor": "tl",
         "text": "X", "font": 1, "color": [80, 220, 255]},
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


def _hud_chrome_vars(level_name, lo):
    """Vars referenced by non-dynamic HUD items (cached chrome). Resolved
    at chrome-bake time — when these change, the chrome cache fingerprint
    invalidates and the chrome surface gets re-rendered."""
    parts = level_name.split()
    slot = parts[-1] if parts and "/" in parts[-1] else ""
    short = parts[0].upper() if parts else ""
    if slot:
        short = f"{short} {slot}"
    main_lvl = lo.main_level()
    side_lvl = lo.side_level() if lo.side_type != "none" else 0
    g = list(GREEN); w_ = list(WHITE)
    return {
        "level_short": short,
        "main_name": MAIN_WEAPON_NAMES[lo.main_type].upper(),
        "main_lvl": main_lvl, "main_max": MAIN_WEAPON_MAX,
        "main_lvl_color": g if main_lvl >= MAIN_WEAPON_MAX else w_,
        "side_name": SIDE_WEAPON_NAMES[lo.side_type].upper(),
        "side_lvl": side_lvl, "side_max": SIDE_WEAPON_MAX,
        "side_lvl_color": g if side_lvl >= SIDE_WEAPON_MAX else w_,
        "side_visible": lo.side_type != "none",
        "shield_lvl": lo.shield, "shield_max": MAX_LEVELS["shield"],
        "shield_lvl_color": g if lo.shield >= MAX_LEVELS["shield"] else w_,
        "engine_lvl": lo.engine, "engine_max": MAX_LEVELS["engine"],
        "engine_lvl_color": g if lo.engine >= MAX_LEVELS["engine"] else w_,
        "bombs": lo.bombs,
        "ability_name": ABILITY_NAMES.get(lo.ability, "?").upper(),
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


def _build_hud_chrome(fonts, level_name, lo):
    """Render the static (cached) HUD chrome from the layout tree. Walks
    LAYOUT_ELEMENTS["hud"] with dynamic_filter=False; dynamic items are
    skipped here and painted per-frame by hud_draw()."""
    surf = pygame.Surface((HUD_W, SCREEN_H))
    surf.fill(HUD_BG)
    tvars = _hud_chrome_vars(level_name, lo)
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


def resolved_layout_tree(screen_name):
    """Apply layout.json overrides on top of LAYOUT_ELEMENTS for `screen_name`.
    For each spec entry, an override with the matching id wins for every
    non-id/type field (children inclusive — the editor saves the whole
    modified subtree). User-added items (no matching spec id) append at the
    end so they render on top."""
    spec = LAYOUT_ELEMENTS.get(screen_name) or []
    overrides_list = _layout_load().get(screen_name) or []
    overrides = {it.get("id"): it for it in overrides_list if it.get("id")}
    out = []
    spec_ids = set()
    for spec_item in spec:
        spec_ids.add(spec_item.get("id"))
        ov = overrides.get(spec_item.get("id"))
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
    return out


_LAYOUT_ANCHOR_AX = {"tl":0,"t":0.5,"tr":1,"l":0,"c":0.5,"r":1,"bl":0,"b":0.5,"br":1}
_LAYOUT_ANCHOR_AY = {"tl":0,"t":0,"tr":0,"l":0.5,"c":0.5,"r":0.5,"bl":1,"b":1,"br":1}


def _layout_anchor_offset(anchor, w, h):
    ax = _LAYOUT_ANCHOR_AX.get(anchor, 0.0)
    ay = _LAYOUT_ANCHOR_AY.get(anchor, 0.0)
    return int(round(-w * ax)), int(round(-h * ay))


def _layout_draw_text(surf, it, fonts):
    text = str(it.get("text") or "")
    if not text:
        return
    scale = int(it.get("font", 3))
    scale = max(1, min(7, scale))
    color = tuple(it.get("color") or (240, 240, 240))[:3]
    alpha = int(it.get("alpha", 255))
    font = fonts.get(scale) or fonts.get("big")
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
    intact). Otherwise return val. Used for progress_bar fields that need
    direct values, not str(format()) coercions."""
    if (isinstance(val, str) and len(val) >= 3
            and val.startswith("{") and val.endswith("}")
            and "{" not in val[1:-1]):
        key = val[1:-1]
        if template_vars and key in template_vars:
            return template_vars[key]
    return val if val is not None else default


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
    bg = it.get("bg")
    border = it.get("border")
    border_w = int(it.get("border_width", 1)) if border else 0
    alpha = int(it.get("alpha", 255))

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
        if it.get("caps") and bg is not None and border is not None:
            cap = tuple(it.get("caps_color") or (110, 160, 220))[:3]
            cap_len = int(it.get("caps_length", 5))
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
        title = it.get("title")
        if title and fonts:
            if "{" in title and template_vars:
                try: title = title.format(**template_vars)
                except (KeyError, IndexError, ValueError): pass
            t_color = tuple(it.get("title_color") or (160, 200, 240))[:3]
            t_font_scale = max(1, min(7, int(it.get("title_font", 1))))
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
         "text": "B confirm  |  {dpad} select",
         "font": 2, "color": [140, 140, 160], "alpha": 255,
         "shadow": False, "blink": True,
         "_label": "controls hint (blinks; {dpad} = D-pad icon)"},
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
         "text": "B return to map", "font": 1,
         "color": [140, 140, 160], "alpha": 255, "shadow": False,
         "blink": True,
         "_label": "return hint (blinks)"},
    ],
    # HUD: built programmatically because the tree is large and references
    # screen-geometry constants. The result is a single `hud_root`
    # container with six chrome panels + dynamic items (timer / score /
    # credits / shield bar / ability cd / ability-ready highlight).
    "hud": _build_hud_layout_spec(),
}

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
        try:
            spec["text"] = str(text).format(**template_vars)
        except (KeyError, IndexError, ValueError):
            pass
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
    scale = max(1, min(7, int(it.get("font", 3))))
    font = fonts.get(scale) or fonts.get("big")
    color = tuple(it.get("color") or (240, 240, 240))[:3]
    alpha = int(it.get("alpha", 255))
    left = font.render(left_txt, False, color)
    right = font.render(right_txt, False, color)
    icon_scale = max(1, scale)   # match the text scale so the icon reads
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
    scale = max(1, min(7, int(it.get("font", 3))))
    font = fonts.get(scale) or fonts.get("big")
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
                try:
                    copy["text"] = txt.format(**template_vars)
                except (KeyError, IndexError, ValueError):
                    pass
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


def hud_draw(surf, fonts, assets, player, save, level_name, score, time_left):
    # 1) Cached chrome (rebuilt only on loadout / mission / layout change).
    key = _hud_cache_key(player, level_name)
    if key != _HudCache.key or _HudCache.surface is None:
        _HudCache.surface = _build_hud_chrome(fonts, level_name,
                                              player.loadout)
        _HudCache.key = key
    surf.blit(_HudCache.surface, (HUD_X, 0))

    # 2) Per-frame dynamic items walked from the same layout tree. Drawn
    # onto a HUD-local scratch surface so the spec stays in HUD-local
    # coords (0..HUD_W); the result is blit at HUD_X.
    chrome_vars = _hud_chrome_vars(level_name, player.loadout)
    tvars = {**chrome_vars, **_hud_dyn_vars(player, save, score, time_left)}
    dyn_surf = pygame.Surface((HUD_W, SCREEN_H), pygame.SRCALPHA)
    for it in resolved_layout_tree("hud"):
        _layout_draw_item(dyn_surf, it, fonts, assets, tvars,
                          dynamic_filter=True)
    surf.blit(dyn_surf, (HUD_X, 0))

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
        self.stars.lateral_shift(self.player.x - prev_player_x)
        # Camera offset: the playfield blit slides opposite to the player's
        # distance from screen-centre, so bg_ribbon and entities pick up the
        # same parallax. Lerp the offset so quick jukes don't jolt.
        target_parallax = clamp(
            -(self.player.x - PLAY_W / 2) * 0.25, -40.0, 40.0)
        self.parallax_x += (target_parallax - self.parallax_x) * min(1.0, dt * 6)
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
        hit_rects = [e.hit_rect for e in enemies]
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
                    b.alive = False
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
                    self._damage_player(2)
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
                    e.hit(99)
                    self._on_kill(e, drop=False)
                self._damage_player(8)
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
                result = self.player.collect(p)
                if result and result[0] == "credits":
                    self._earn(result[1])
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
        """Convert a weapon power-up drop into "money" when the player's
        matching weapon is already at max — picking it up would just give
        credits anyway, so spawn the credit pickup directly."""
        lo = self.player.loadout
        if kind == "main" and lo.main_level() >= MAIN_WEAPON_MAX:
            return "money"
        if kind == "side" and lo.side_type != "none" and lo.side_level() >= SIDE_WEAPON_MAX:
            return "money"
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

    def _on_kill(self, enemy, drop=True):
        self.score += enemy.SCORE
        self._earn(enemy.CREDITS)
        cx, cy = enemy.rect.centerx, enemy.rect.centery
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
                kind = self._resolve_drop_kind(random.choice(enemy.DROP_TABLE))
                self.pickups.append(Pickup(cx, cy, kind, self.assets["pickup_" + kind]))
        self.app.sounds["big_boom" if is_boss else "boom"].play()

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
        # The full playfield surface is PLAY_MARGIN wider than PLAY_W on
        # each side, with the bg_ribbon filling that margin. So we just
        # blit it offset by the parallax + shake — the bg always covers
        # whatever screen pixels the offset would otherwise expose. No
        # screen clear needed.
        perf.start("draw.blit_screen")
        parallax_off = int(self.parallax_x)
        screen.blit(playfield_full,
                    (shake_x + parallax_off - PLAY_MARGIN, shake_y))
        perf.end("draw.blit_screen")
        perf.start("draw.hud")
        hud_draw(screen, self.app.fonts, self.assets, self.player, self.app.save,
                 self.level.name, self.score,
                 (self.level.duration - self.elapsed) if not self.level.has_boss else 0)
        perf.end("draw.hud")

        if self.pause:
            _center_text(screen, self.app.fonts, "PAUSED", "START to resume")
        if self.outcome == "win":
            _center_text(screen, self.app.fonts, "MISSION COMPLETE", f"+{self.credits_earned} cr   B continue")
        elif self.outcome == "loss":
            _center_text(screen, self.app.fonts, "SHIP DESTROYED", "B continue")

        draw_layout_overlay(screen, "play", self.app.fonts, self.app.assets)

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


def _center_text(surf, fonts, big, small):
    cx = PLAY_W // 2
    cy = PLAY_H // 2
    overlay = pygame.Surface((PLAY_W, 80), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, 160))
    surf.blit(overlay, (0, cy - 40))
    b = fonts["big"].render(big, False, WHITE)
    s = fonts["small"].render(small, False, DIM)
    surf.blit(b, b.get_rect(center=(cx, cy - 10)))
    surf.blit(s, s.get_rect(center=(cx, cy + 20)))


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

        # Sector pagination
        sector_changed = False
        for ev in events:
            if ev.type == pygame.JOYBUTTONDOWN:
                if ev.button == JOY_L1 and self.sector_idx > 0:
                    self.sector_idx -= 1; sector_changed = True
                if ev.button == JOY_R1 and self.sector_idx < self._max_sector():
                    self.sector_idx += 1; sector_changed = True
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_q and self.sector_idx > 0:
                    self.sector_idx -= 1; sector_changed = True
                if ev.key == pygame.K_e and self.sector_idx < self._max_sector():
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

        # D-pad within sector
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

        if self.sector_idx > 0:
            arrow = fonts["small"].render("< L1", False, accent)
            screen.blit(arrow, (12, 8))
        if self.sector_idx < max_sector:
            arrow = fonts["small"].render("R1 >", False, accent)
            screen.blit(arrow, (PLAY_W - arrow.get_width() - 12, 8))

        # ---- Sector header banner ----
        header_y = 32
        header_h = 50
        _panel(screen, 60, header_y, PLAY_W - 120, header_h)
        title = fonts["big"].render(SECTOR_NAMES[self.sector_idx], False, accent)
        screen.blit(title, title.get_rect(center=(PLAY_W // 2, header_y + 18)))
        sub = fonts["tiny"].render(
            f"SECTOR {self.sector_idx + 1:02d}/10  -  CLEARED {progress_n:02d}/100",
            False, DIM)
        screen.blit(sub, sub.get_rect(center=(PLAY_W // 2, header_y + 40)))

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

        # ---- Right HUD panel ----
        pygame.draw.rect(screen, HUD_BG, (HUD_X, 0, HUD_W, SCREEN_H))
        pygame.draw.line(screen, HUD_LINE, (HUD_X, 0), (HUD_X, SCREEN_H), 1)
        x = HUD_X + 6
        inner_w = HUD_W - 12

        _panel(screen, x, 6, inner_w, 26)
        title_h = fonts["small"].render("PEWPEW", False, CYAN)
        screen.blit(title_h, title_h.get_rect(center=(x + inner_w // 2, 19)))

        _panel(screen, x, 38, inner_w, 78, "STATUS", fonts)
        screen.blit(fonts["small"].render(f"$ {save.credits}", False, YELLOW), (x + 8, 52))
        screen.blit(fonts["small"].render(f"HI {save.high_score:07d}", False, DIM), (x + 8, 70))
        screen.blit(fonts["small"].render(f"PROG {progress_n}/100", False, ORANGE), (x + 8, 88))
        ratio = progress_n / 100.0
        _segbar(screen, x + 8, 106, inner_w - 16, 6, ratio, GREEN, segments=10)

        _panel(screen, x, 122, inner_w, 68, "LOADOUT", fonts)
        yy = 138
        # MAIN: equipped weapon name + level bar (5 cells).
        lo = save.loadout
        main_label = MAIN_WEAPON_NAMES[lo.main_type]
        screen.blit(fonts["tiny"].render(main_label.upper(), False, CYAN), (x + 8, yy))
        yy += 11
        lv = lo.main_level()
        mx = MAIN_WEAPON_MAX
        cell_w = max(2, (inner_w - 16) // max(mx, 1))
        for i in range(mx):
            cell = pygame.Rect(x + 8 + i * cell_w, yy, cell_w - 1, 6)
            pygame.draw.rect(screen, DARKER, cell)
            if i < lv:
                pygame.draw.rect(screen, WHITE, cell.inflate(-2, -2))
        yy += 12
        # SHLD: short bar
        screen.blit(fonts["small"].render("SHLD", False, DIM), (x + 8, yy))
        sb_x = x + 58
        scw = max(2, (inner_w - 64) // max(MAX_LEVELS["shield"], 1))
        for i in range(MAX_LEVELS["shield"]):
            cell = pygame.Rect(sb_x + i * scw, yy + 3, scw - 1, 8)
            pygame.draw.rect(screen, DARKER, cell)
            if i < lo.shield:
                pygame.draw.rect(screen, WHITE, cell.inflate(-2, -2))

        # Controls at bottom
        chy = SCREEN_H - 116
        _panel(screen, x, chy, inner_w, 108, "CONTROL", fonts)
        hints = (
            ("D",    "pick"),
            ("L/R",  "sector"),
            ("B",    "launch"),
            ("Y",    "shop"),
            ("SL+X", "unlock"),
        )
        yy = chy + 14
        for k_, v in hints:
            if k_ == "D":
                _draw_dpad_icon(screen, x + 8, yy + 2, scale=2, color=CYAN)
            else:
                screen.blit(fonts["small"].render(k_, False, CYAN), (x + 8, yy))
            screen.blit(fonts["small"].render(v, False, DIM), (x + 60, yy))
            yy += 18

        # End-of-game banner
        if progress_n >= 100:
            banner = fonts["small"].render("ALL CLEAR", False, GREEN)
            screen.blit(banner, banner.get_rect(center=(PLAY_W // 2, SCREEN_H - 10)))

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
    def __init__(self, app):
        self.app = app
        self.cursor = 0
        self.outcome = None
        self.flash_text = None
        self.flash_t = 0

    def run(self, events, controls):
        dt = 1.0 / FPS
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

        if self.flash_t > 0:
            self.flash_t -= dt
        self._draw()
        if self.outcome:
            return self.outcome
        return None

    def _item_cost(self, key):
        """Returns the credit cost for the action this row offers, or None if
        the row is currently at MAX. Equip actions cost 0."""
        save = self.app.save
        if key == "bomb":
            return BOMB_PRICE
        if key.startswith("ability_"):
            return 0
        slot, wtype = _parse_weapon_key(key)
        if slot == "main":
            lvl = getattr(save.loadout, f"main_{wtype}")
            if lvl == 0:
                return MAIN_BUY_COST
            if save.loadout.main_type != wtype:
                # Owned but not equipped — equipping is free.
                return 0
            if lvl >= MAIN_WEAPON_MAX:
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
            return SIDE_UPGRADE_COSTS[wtype][lvl]
        # Shield / engine: legacy leveled equipment.
        lvl = getattr(save.loadout, key)
        costs = WEAPON_COSTS[key]
        if lvl >= MAX_LEVELS[key]:
            return None
        return costs[lvl]

    def _row_action(self, key):
        """Human-readable verb for what B does on this row right now."""
        save = self.app.save
        slot, wtype = _parse_weapon_key(key)
        if slot == "main":
            lvl = getattr(save.loadout, f"main_{wtype}")
            if lvl == 0:
                return "buy"
            if save.loadout.main_type != wtype:
                return "equip"
            if lvl >= MAIN_WEAPON_MAX:
                return "max"
            return "upgrade"
        if slot == "side":
            lvl = getattr(save.loadout, f"side_{wtype}")
            if lvl == 0:
                return "buy"
            if save.loadout.side_type != wtype:
                return "equip"
            if lvl >= SIDE_WEAPON_MAX:
                return "max"
            return "upgrade"
        if key == "bomb":
            return "buy"
        if key.startswith("ability_"):
            return "equipped" if save.loadout.ability == key[len("ability_"):] else "equip"
        lvl = getattr(save.loadout, key)
        if lvl >= MAX_LEVELS[key]:
            return "max"
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
                if lvl == 0:
                    save.credits -= MAIN_BUY_COST
                    setattr(save.loadout, f"main_{wtype}", 1)
                    save.loadout.main_type = wtype
                    self.flash_text = "PURCHASED"
                elif save.loadout.main_type != wtype:
                    save.loadout.main_type = wtype
                    self.flash_text = "EQUIPPED"
                else:
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
        title = fonts["big"].render("HANGAR", False, CYAN)
        screen.blit(title, (20, 18))

        # Column layout: name on left, bar at fixed column, cost right-aligned.
        NAME_X = 20
        BAR_X = PLAY_W - 200
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
            # Mark currently EQUIPPED main/side with a small chevron tag.
            if slot == "main" and save.loadout.main_type == wtype and getattr(save.loadout, f"main_{wtype}") > 0:
                tag = fonts["tiny"].render("EQ", False, GREEN)
                screen.blit(tag, (NAME_X + name_surf.get_width() + 6, y + 2))
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
                if slot == "main":
                    lvl = getattr(save.loadout, f"main_{wtype}")
                    mx = MAIN_WEAPON_MAX
                elif slot == "side":
                    lvl = getattr(save.loadout, f"side_{wtype}")
                    mx = SIDE_WEAPON_MAX
                else:
                    lvl = getattr(save.loadout, key)
                    mx = MAX_LEVELS[key]
                cell_w = 14
                gap = 2
                bar_h = 12
                fill_col = GREEN if lvl == mx else (WHITE if i == self.cursor else (160, 160, 200))
                # Compress cell width if the bar is longer than the column.
                if mx > 5:
                    cell_w = 10
                for ci in range(mx):
                    cell = pygame.Rect(BAR_X + ci * (cell_w + gap), y + 2, cell_w, bar_h)
                    pygame.draw.rect(screen, DARKER, cell)
                    pygame.draw.rect(screen, (60, 70, 110), cell, 1)
                    if ci < lvl:
                        pygame.draw.rect(screen, fill_col, cell.inflate(-3, -3))
                if action == "max":
                    cost_str, cost_col = "MAX", GREEN
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
        screen.blit(fonts["tiny"].render("NEXT", False, ORANGE), (rx, ly))
        screen.blit(fonts["tiny"].render(next_effect, False, WHITE), (rx, ly + 20))
        screen.blit(fonts["tiny"].render(cost_str, False, cost_col), (rx, ly + 36))

        if self.flash_t > 0 and self.flash_text:
            txt = fonts["small"].render(self.flash_text, False, YELLOW)
            screen.blit(txt, txt.get_rect(center=(PLAY_W // 2, detail_y - 14)))

        # ===== Right HUD: PEWPEW header at top + CONTROL pinned to bottom =======
        pygame.draw.rect(screen, HUD_BG, (HUD_X, 0, HUD_W, SCREEN_H))
        pygame.draw.line(screen, HUD_LINE, (HUD_X, 0), (HUD_X, SCREEN_H), 1)
        x = HUD_X + 6
        inner_w = HUD_W - 12

        _panel(screen, x, 6, inner_w, 24)
        h = fonts["small"].render("PEWPEW", False, CYAN)
        screen.blit(h, h.get_rect(center=(x + inner_w // 2, 18)))

        bal_y, bal_h = 40, 72
        _panel(screen, x, bal_y, inner_w, bal_h, "BALANCE", fonts)
        bal_surf = fonts["big"].render(f"${save.credits}", False, YELLOW)
        # Centre exactly in the panel (both axes).
        screen.blit(bal_surf, bal_surf.get_rect(
            center=(x + inner_w // 2, bal_y + bal_h // 2)))

        chy = SCREEN_H - 98
        _panel(screen, x, chy, inner_w, 92, "CONTROL", fonts)
        hints = (
            ("D",   "pick"),
            ("B",   "buy"),
            ("Y",   "exit"),
        )
        yy = chy + 16
        for k_, v in hints:
            if k_ == "D":
                _draw_dpad_icon(screen, x + 6, yy + 2, scale=2, color=CYAN)
            else:
                screen.blit(fonts["small"].render(k_, False, CYAN), (x + 6, yy))
            screen.blit(fonts["small"].render(v, False, DIM), (x + 40, yy))
            yy += 20

        draw_layout_overlay(screen, "shop", fonts, self.app.assets)

    def _detail_pieces(self, key, cost):
        """Returns 5-tuple: current level string, current effect, next effect,
        cost string, cost colour. Used by the bottom DETAIL strip."""
        save = self.app.save
        slot, wtype = _parse_weapon_key(key)
        if slot == "main":
            descs = {
                "pulse":  ["single shot", "dual shot", "triple spread",
                           "quad shot", "quad + wing"],
                "spread": ["3-way fan", "5-way fan", "7-way fan",
                           "9-way fan", "11-way wave"],
                "vulcan": ["rapid 1", "rapid dual", "rapid triple",
                           "rapid quad", "rapid quint"],
            }[wtype]
            lvl = getattr(save.loadout, f"main_{wtype}")
            mx = MAIN_WEAPON_MAX
            equipped = save.loadout.main_type == wtype and lvl > 0
            tag = " (EQ)" if equipped else ""
            if lvl == 0:
                return ("not owned", "—", descs[0], f"Buy ${cost}", ORANGE)
            if not equipped:
                return (f"Lv {lvl}/{mx}{tag}", descs[lvl - 1],
                        "equip with B", "free", CYAN)
            if lvl < mx:
                return (f"Lv {lvl}/{mx}{tag}", descs[lvl - 1], descs[lvl],
                        f"Cost ${cost}", YELLOW)
            return (f"Lv {lvl}/{mx}{tag}", descs[lvl - 1],
                    "fully upgraded", "MAX", GREEN)
        if slot == "side":
            descs = {
                "missile": ["1 heatseeker", "2 heatseekers", "3 heatseekers"],
                "drone":   ["1 drone shot", "2 drone shots", "3 drone shots"],
            }[wtype]
            lvl = getattr(save.loadout, f"side_{wtype}")
            mx = SIDE_WEAPON_MAX
            equipped = save.loadout.side_type == wtype and lvl > 0
            tag = " (EQ)" if equipped else ""
            if lvl == 0:
                return ("not owned", "—", descs[0], f"Buy ${cost}", ORANGE)
            if not equipped:
                return (f"Lv {lvl}/{mx}{tag}", descs[lvl - 1],
                        "equip with B", "free", CYAN)
            if lvl < mx:
                return (f"Lv {lvl}/{mx}{tag}", descs[lvl - 1], descs[lvl],
                        f"Cost ${cost}", YELLOW)
            return (f"Lv {lvl}/{mx}{tag}", descs[lvl - 1],
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
                    "Pulse Bomb on A",
                    "Adds 1 bomb (max 9)",
                    f"Cost ${BOMB_PRICE}", YELLOW)
        if key.startswith("ability_"):
            ab = key[len("ability_"):]
            equipped = save.loadout.ability == ab
            descs = {
                "screen_clear": ("clears all enemies on screen",
                                 "swap on B"),
                "shield_burst": ("refills shield + brief invuln",
                                 "swap on B"),
                "mega_laser":   ("sustained high-dps beam",
                                 "swap on B"),
            }
            d, action = descs.get(ab, ("", ""))
            if equipped:
                return ("EQUIPPED", d, action, "free", GREEN)
            return ("not equipped", d, action, "Equip with B", YELLOW)
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
        self.t = 0
        self.outcome = None
        self.cursor = 0
        self.has_save = SAVE_PATH.exists()
        self.options = ["Continue" if self.has_save else "New Game", "New Game", "Quit"] if self.has_save else ["New Game", "Quit"]

    def run(self, events, controls):
        dt = 1.0 / FPS
        self.t += dt
        self.bg_ribbon.update(dt)
        self.stars.update(dt)
        moved = False
        for ev in events:
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_UP:
                    self.cursor = (self.cursor - 1) % len(self.options); moved = True
                if ev.key == pygame.K_DOWN:
                    self.cursor = (self.cursor + 1) % len(self.options); moved = True
            if ev.type == pygame.JOYHATMOTION:
                _, hy = ev.value
                if hy > 0:
                    self.cursor = (self.cursor - 1) % len(self.options); moved = True
                if hy < 0:
                    self.cursor = (self.cursor + 1) % len(self.options); moved = True
        if moved:
            self.app.sounds["menu"].play()
        if controls.confirm_pressed or controls.start_pressed:
            choice = self.options[self.cursor]
            if choice == "Continue":
                self.outcome = ("map", None)
            elif choice == "New Game":
                self.app.save = SaveData()
                self.app.save.save()
                self.outcome = ("map", None)
            elif choice == "Quit":
                self.outcome = ("quit", None)
        # Hidden visual-checkup mission: SELECT held + Y. cancel_pressed is
        # the Y-button pulse from Controls; combined with held SELECT it
        # avoids colliding with a plain "back to title" Y press.
        if controls.select and controls.cancel_pressed:
            self.outcome = ("play", make_test_level())
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
            if (hitbox and stripe is not None and yellow_mask is not None
                    and abs(scale - 1.0) < 0.001):
                hx, hy, hw, hh = hitbox
                hx = int(hx); hy = int(hy)
                hw = max(1, int(hw)); hh = max(1, int(hh))
                stripe_w = stripe.get_width()
                period = 3.6   # seconds per full sweep
                travel = hw + stripe_w * 2
                cycle = (self.t % period) / period
                stripe_x = int(cycle * travel) - stripe_w
                overlay = pygame.Surface((hw, hh), pygame.SRCALPHA)
                overlay.blit(stripe, (stripe_x, 0))
                mask_rect = pygame.Rect(hx, hy, hw, hh).clip(yellow_mask.get_rect())
                if mask_rect.w > 0 and mask_rect.h > 0:
                    overlay.blit(yellow_mask.subsurface(mask_rect),
                                 (mask_rect.x - hx, mask_rect.y - hy),
                                 special_flags=pygame.BLEND_MULT)
                glossed = logo.copy()
                glossed.blit(overlay, (hx, hy),
                             special_flags=pygame.BLEND_RGB_ADD)
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
        tip_el = get_element("title", "tip")
        if tip_el is not None and (not tip_el.get("blink", True)
                                   or int(self.t * 2) % 2 == 0):
            _draw_text_with_dpad(screen, tip_el, self.app.fonts)

        draw_layout_overlay(screen, "title", self.app.fonts, self.app.assets)


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
        vars_ = {"score": self.score, "best": self.app.save.high_score}
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
        flags = pygame.SCALED
        if not windowed:
            flags |= pygame.FULLSCREEN
        self.screen = pygame.display.set_mode((SCREEN_W, SCREEN_H), flags)
        pygame.display.set_caption("Pewpew")
        pygame.mouse.set_visible(False)
        self.clock = pygame.time.Clock()

        self.joys = []
        for i in range(pygame.joystick.get_count()):
            j = pygame.joystick.Joystick(i)
            j.init()
            self.joys.append(j)

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
        self.title_gloss_stripe = _make_gloss_stripe(
            height=self.logo.get_height(), stripe_w=70, peak=140)
        self.title_yellow_mask = _make_yellow_mask(self.logo)
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
        self.fonts["tiny"]  = self.fonts[1]   #  7 px tall
        self.fonts["small"] = self.fonts[2]   # 14 px
        self.fonts["big"]   = self.fonts[3]   # 21 px
        self.fonts["large"] = self.fonts[4]   # 28 px
        self.fonts["huge"]  = self.fonts[5]   # 35 px
        self.fonts["mega"]  = self.fonts[6]   # 42 px
        self.fonts["giant"] = self.fonts[7]   # 49 px
        self.levels = make_levels()
        self.save = SaveData.load()
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
            pygame.display.flip()
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

    def _transition(self, kind, payload):
        if kind == "play":
            level = payload
            self.state = PlayState(self, level)
        elif kind == "title":
            self.state = TitleScreen(self)
        elif kind == "map":
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
            score, level_key, won = payload
            self.save.high_score = max(self.save.high_score, score)
            if won:
                if level_key not in self.save.completed:
                    self.save.completed.append(level_key)
                for nxt in MAP_GRAPH[level_key].nexts:
                    if nxt not in self.save.unlocked:
                        self.save.unlocked.append(nxt)
                self.save.save()
                self.state = ShopScreen(self)
            else:
                self.state = GameOverScreen(self, score)


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
    if out == "win":
        return ("post_play", (self.score, self.level.key, True))
    if out == "loss":
        return ("post_play", (self.score, self.level.key, False))
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
    _acquire_single_instance_lock()
    try:
        windowed = "--windowed" in sys.argv
        App(windowed=windowed).run()
    finally:
        _release_single_instance_lock()


if __name__ == "__main__":
    main()
