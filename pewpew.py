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
import sys
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
HUD_X = PLAY_W
HUD_W = SCREEN_W - PLAY_W
FPS = 60

SAVE_PATH = Path(os.environ.get("PEWPEW_SAVE", str(Path(__file__).resolve().parent / "save.json")))

JOY_A = 0
JOY_B = 1
JOY_X = 2
JOY_Y = 3
JOY_L1 = 4
JOY_R1 = 5
JOY_SELECT = 6
JOY_START = 7
JOY_MENU = 8

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

# Upgrade prices: cost to go FROM level i TO level i+1, indexed by current level (1-based).
WEAPON_COSTS = {
    "main":   [0, 400, 900, 1600, 2600],   # max level 5
    "side":   [0, 600, 1400, 2800],        # max level 3
    "shield": [0, 350, 800, 1500, 2400],   # max level 5
    "engine": [0, 500, 1200],              # max level 3
}
MAX_LEVELS = {"main": 5, "side": 3, "shield": 5, "engine": 3}
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
    f = pygame.font.SysFont(None, 14, bold=True)
    txt = f.render(letter, False, BLACK)
    s.blit(txt, txt.get_rect(center=(7, 7)))
    return s


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
    # Pickup icons + their silhouettes
    a["pickup_main"] = _frame(YELLOW, "W")
    a["pickup_side"] = _frame(GREEN, "S")
    a["pickup_shield"] = _frame(CYAN, "+")
    a["pickup_bomb"] = _frame(PURPLE, "B")
    a["pickup_money"] = _frame((180, 180, 80), "$")
    return a


def make_sounds():
    return {
        "shoot":  tone(880, 0.05, 0.18, square=True),
        "shoot2": tone(660, 0.05, 0.16, square=True),
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
    main: int = 1
    side: int = 0
    shield: int = 1
    engine: int = 1
    bombs: int = 2
    ability: str = "screen_clear"


@dataclass
class SaveData:
    credits: int = 0
    current_node: str = "L001"
    completed: list = field(default_factory=list)
    unlocked: list = field(default_factory=lambda: ["L001"])
    high_score: int = 0
    loadout: Loadout = field(default_factory=Loadout)

    @staticmethod
    def load():
        try:
            raw = json.loads(SAVE_PATH.read_text())
            # Detect the old 5-level key format and reset to the new layout.
            unlocked = raw.get("unlocked") or []
            if unlocked and not all(isinstance(k, str) and k.startswith("L") for k in unlocked):
                return SaveData()
            loadout = Loadout(**raw.pop("loadout", {}))
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
    """Three-layer starfield. Layer 0 = far/slow/dim, 2 = near/fast/bright."""
    def __init__(self, width=PLAY_W, height=PLAY_H, counts=(60, 40, 25)):
        self.width = width
        self.height = height
        self.layers = []
        speeds = (30, 80, 170)
        shades = ((90, 90, 110), (160, 160, 180), (230, 230, 255))
        for n, sp, sh in zip(counts, speeds, shades):
            layer = []
            for _ in range(n):
                layer.append([random.uniform(0, width), random.uniform(0, height), sp, sh])
            self.layers.append(layer)

    def update(self, dt):
        for layer in self.layers:
            for s in layer:
                s[1] += s[2] * dt
                if s[1] > self.height:
                    s[1] -= self.height
                    s[0] = random.uniform(0, self.width)

    def draw(self, surf):
        for layer in self.layers:
            for s in layer:
                x, y = int(s[0]), int(s[1])
                shade = s[3]
                if s[2] > 100:
                    # near-layer stars: 1-px streaks
                    surf.set_at((x, y), shade)
                    if y + 1 < self.height:
                        surf.set_at((x, y + 1), shade)
                else:
                    surf.set_at((x, y), shade)


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
    def __init__(self, level_key, width=PLAY_W, tile_h=PLAY_H * 2):
        self.width = width
        self.tile_h = tile_h
        self.scroll = 0.0
        self.speed = 24.0
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
        y = -int(self.scroll)
        surf.blit(self.layer, (0, y))
        surf.blit(self.layer, (0, y + self.tile_h))


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


def make_vignette():
    """Subtle dark falloff at playfield edges. Pre-rendered once."""
    v = pygame.Surface((PLAY_W, PLAY_H), pygame.SRCALPHA)
    edge = 40
    for i in range(edge):
        alpha = int(80 * (1 - i / edge) ** 2)
        pygame.draw.rect(v, (0, 0, 0, alpha), (i, i, PLAY_W - i * 2, PLAY_H - i * 2), 1)
    return v


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
    __slots__ = ("x", "y", "vx", "vy", "color", "size", "friendly", "alive", "rect", "damage", "pierce")

    def __init__(self, x, y, vx, vy, color, friendly=True, size=(3, 7), damage=1, pierce=0):
        self.x = float(x)
        self.y = float(y)
        self.vx = vx
        self.vy = vy
        self.color = color
        self.size = size
        self.friendly = friendly
        self.alive = True
        self.damage = damage
        self.pierce = pierce
        self.rect = pygame.Rect(int(x) - size[0] // 2, int(y) - size[1] // 2, size[0], size[1])

    def update(self, dt):
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.rect.x = int(self.x) - self.size[0] // 2
        self.rect.y = int(self.y) - self.size[1] // 2
        if self.x < -20 or self.x > PLAY_W + 20 or self.y < -20 or self.y > PLAY_H + 20:
            self.alive = False

    def draw(self, surf):
        # Trail: 3 segments behind the bullet, fading
        r, g, b = self.color[0], self.color[1], self.color[2]
        sx = self.size[0]
        sy = self.size[1]
        # Step back along the velocity vector
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
        # Core: bright body + white hot center
        pygame.draw.rect(surf, self.color, self.rect)
        if sx >= 3 and sy >= 3:
            pygame.draw.rect(surf, WHITE, (self.rect.x + sx // 2 - 1, self.rect.y + 1, 2, max(1, sy - 2)))


class Missile(Bullet):
    def __init__(self, x, y, target_ref, color=(255, 200, 80)):
        super().__init__(x, y, 0, -200, color, friendly=True, size=(4, 9), damage=2)
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

    def draw(self, surf):
        pygame.draw.rect(surf, self.color, self.rect)
        # trail
        tail_y = int(self.y - self.vy * 0.02)
        tail_x = int(self.x - self.vx * 0.02)
        pygame.draw.line(surf, (255, 100, 40), (tail_x, tail_y), (int(self.x), int(self.y)), 2)


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


class ExplosionRing:
    """Expanding ring + bright core, used on enemy/boss death."""
    __slots__ = ("x", "y", "max_r", "color", "life", "max_life", "alive")

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
MAIN_FIRE_RATE = {1: 0.18, 2: 0.16, 3: 0.14, 4: 0.12, 5: 0.10}


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
            self.cooldown_main = MAIN_FIRE_RATE[self.loadout.main]
            self._fire_main(bullets, sounds)

        # Side weapons (auto-fire)
        if self.loadout.side > 0 and self.cooldown_side <= 0:
            self.cooldown_side = 1.6 - (self.loadout.side - 1) * 0.3
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

    def _fire_main(self, bullets, sounds):
        cx, cy = self.rect.centerx, self.rect.top + 2
        lvl = self.loadout.main
        if lvl == 1:
            bullets.append(Bullet(cx, cy, 0, -500, CYAN, size=(3, 8)))
        elif lvl == 2:
            bullets.append(Bullet(cx - 5, cy, 0, -520, CYAN, size=(3, 8)))
            bullets.append(Bullet(cx + 5, cy, 0, -520, CYAN, size=(3, 8)))
        elif lvl == 3:
            bullets.append(Bullet(cx, cy, 0, -540, CYAN, size=(4, 9)))
            bullets.append(Bullet(cx - 6, cy + 3, -80, -520, CYAN, size=(3, 7)))
            bullets.append(Bullet(cx + 6, cy + 3, 80, -520, CYAN, size=(3, 7)))
        elif lvl == 4:
            for off in (-9, -3, 3, 9):
                bullets.append(Bullet(cx + off, cy, 0, -560, CYAN, size=(3, 9), damage=1))
        else:  # lvl 5
            for off in (-9, -3, 3, 9):
                bullets.append(Bullet(cx + off, cy, 0, -580, CYAN, size=(3, 10), damage=2))
            bullets.append(Bullet(cx - 12, cy + 3, -160, -500, CYAN, size=(3, 7)))
            bullets.append(Bullet(cx + 12, cy + 3, 160, -500, CYAN, size=(3, 7)))
        sounds["shoot"].play()

    def _fire_side(self, bullets, enemies_ref, sounds):
        cx, cy = self.rect.centerx, self.rect.centery
        targets = enemies_ref()
        if not targets:
            return
        targets = sorted(targets, key=lambda e: abs(e.rect.centerx - cx) + (cy - e.rect.centery) * 0.3)
        n = self.loadout.side
        for i in range(n):
            target = targets[i % len(targets)] if targets else None
            ref = (lambda t: (lambda: t if t.alive else None))(target)
            off = (-12 if i % 2 == 0 else 12)
            bullets.append(Missile(cx + off, cy, ref))
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
            if self.loadout.main < MAX_LEVELS["main"]:
                self.loadout.main += 1
            else:
                return ("credits", 200)
        if k == "side":
            if self.loadout.side < MAX_LEVELS["side"]:
                self.loadout.side += 1
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
        # Three engine flames at the W's exhaust points. All three sprites
        # share the same bottom row, so the exhaust positions don't move with
        # bank. The dipped wing's flame is shorter to reinforce the depth cue
        # but never fully cuts out.
        flicker = (int(self.thrust) % 4)
        cx = self.rect.centerx
        fy = self.rect.bottom - 1
        for off, dip_side in ((-8, -1), (0, 0), (8, +1)):
            fx = cx + off
            dipped = dip_side != 0 and self.tilt * dip_side > 0.4
            if dipped:
                # Foreshortened flame for the wing that's pointing away from camera.
                pygame.draw.polygon(surf, ORANGE, [
                    (fx - 1, fy),
                    (fx + 1, fy),
                    (fx, fy + 3 + flicker // 2),
                ])
            else:
                pygame.draw.polygon(surf, ORANGE, [
                    (fx - 2, fy),
                    (fx + 2, fy),
                    (fx, fy + 5 + flicker),
                ])
                pygame.draw.polygon(surf, YELLOW, [
                    (fx - 1, fy),
                    (fx + 1, fy),
                    (fx, fy + 2 + flicker // 2),
                ])
        # Pick sprite based on tilt; flash variant takes priority for the brief
        # invulnerability blink right after taking damage.
        if self.tilt < -0.5:
            img = self.assets["player_left"]
        elif self.tilt > 0.5:
            img = self.assets["player_right"]
        else:
            img = self.image
        rect = img.get_rect(center=self.rect.center)
        surf.blit(img, rect)
        # shield ring
        if self.shield_hp > 0 and (self.invuln > 0 or self.shield_recharge_delay < 0.3):
            pygame.draw.circle(surf, CYAN, self.rect.center, max(self.rect.w, self.rect.h) // 2 + 4, 1)


# =============================================================================
# ENEMIES
# =============================================================================

class Enemy:
    SCORE = 10
    CREDITS = 10
    DROP_TABLE = ("money",)
    DROP_CHANCE = 0.10

    def __init__(self, x, y, asset, hp=1, flash_asset=None):
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
        bullets.append(Bullet(self.rect.centerx, self.rect.bottom, vx, vy, RED, friendly=False, size=(4, 4)))
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
        for ang in (-22, -8, 8, 22):
            rad = math.radians(90 + ang)
            vx = math.cos(rad) * 200
            vy = math.sin(rad) * 200
            bullets.append(Bullet(self.rect.centerx, self.rect.bottom, vx, vy, ORANGE, friendly=False, size=(4, 6)))


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
        for ang in (-15, 0, 15):
            dx = player.rect.centerx - self.x
            dy = player.rect.centery - self.y
            base = math.atan2(dy, dx) + math.radians(ang)
            vx = math.cos(base) * 230
            vy = math.sin(base) * 230
            bullets.append(Bullet(self.rect.centerx, self.rect.bottom, vx, vy, PURPLE, friendly=False, size=(4, 4)))


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

    def _fire_pattern(self, bullets, player):
        cx, cy = self.rect.centerx, self.rect.bottom
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
    if kind == "scout":     return Scout(x, assets["scout"], flash)
    if kind == "gunner":    return Gunner(x, assets["gunner"], flash)
    if kind == "weaver":    return Weaver(x, assets["weaver"], flash)
    if kind == "bomber":    return Bomber(x, assets["bomber"], flash)
    if kind == "kamikaze":  return Kamikaze(x, assets["kamikaze"], flash)
    if kind == "turret":    return Turret(x, assets["turret"], flash)
    if kind == "boss":      return Boss(assets["boss"], flash)
    raise ValueError(kind)


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
        flash = state.assets.get("boss_flash")
        b = Boss(state.assets["boss"], flash, hp_mul=hp_mul)
        state.enemies.append(b)
        state.is_boss_fight = True
        state.boss_intro_t = 2.6
        state.app.sounds["warn"].play()
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


def _gen_timeline(n, is_boss):
    """Procedural enemy timeline for level n (1..100)."""
    pool = ["scout"]
    if n >= 3:  pool.append("gunner")
    if n >= 7:  pool.append("weaver")
    if n >= 12: pool.append("kamikaze")
    if n >= 18: pool.append("turret")
    if n >= 25: pool.append("bomber")
    # Heavier types become more frequent later: weight them in the pool.
    weighted = list(pool)
    if n >= 30: weighted += ["gunner", "weaver"]
    if n >= 50: weighted += ["kamikaze", "bomber"]
    if n >= 70: weighted += ["turret", "bomber"]

    rng = random.Random(0xC0FFEE ^ (n * 2654435761))

    timeline = []
    if is_boss:
        # 3-4 softening waves before the boss drops in
        for i in range(4):
            t = 1.5 + i * 4.0
            kind = rng.choice(weighted)
            count = 3 + n // 14
            choice = rng.randint(0, 2)
            spawner = (spawn_line(kind, count, gap=60) if choice == 0
                       else spawn_v(kind, count) if choice == 1
                       else spawn_random(kind, count))
            timeline.append((t, spawner))
        hp_mul = 1.0 + ((n - 10) // 10) * 0.35  # boss HP grows per sector
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
                # paired ambush: two simultaneous spawn points
                spawner_a = spawn_at(kind, PLAY_W * 0.25)
                spawner_b = spawn_at(kind, PLAY_W * 0.75)
                def combo(state, sa=spawner_a, sb=spawner_b):
                    sa(state); sb(state)
                spawner = combo
            timeline.append((t, spawner))
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
        )
    return levels


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


def hud_draw(surf, fonts, assets, player, save, level_name, score, time_left):
    pygame.draw.rect(surf, HUD_BG, (HUD_X, 0, HUD_W, SCREEN_H))
    pygame.draw.line(surf, HUD_LINE, (HUD_X, 0), (HUD_X, SCREEN_H), 1)

    x = HUD_X + 6
    inner_w = HUD_W - 12

    # HEADER
    _panel(surf, x, 6, inner_w, 26)
    title = fonts["small"].render("PEWPEW", False, CYAN)
    surf.blit(title, title.get_rect(center=(x + inner_w // 2, 6 + 13)))

    # MISSION
    py = 42
    _panel(surf, x, py, inner_w, 36, "MISSION", fonts)
    surf.blit(fonts["tiny"].render(level_name.upper(), False, WHITE), (x + 6, py + 8))
    surf.blit(fonts["tiny"].render(f"T {max(0, int(time_left))}s", False, DIM), (x + 6, py + 22))

    # STATUS
    sy = 88
    _panel(surf, x, sy, inner_w, 64, "STATUS", fonts)
    surf.blit(fonts["tiny"].render("SHLD", False, DIM), (x + 6, sy + 8))
    sh_ratio = max(0, player.shield_hp / player.shield_max) if player.shield_max > 0 else 0
    _segbar(surf, x + 36, sy + 10, inner_w - 42, 8, sh_ratio, CYAN, segments=10)
    surf.blit(fonts["tiny"].render(f"SC {score:07d}", False, WHITE), (x + 6, sy + 26))
    surf.blit(fonts["tiny"].render(f"$  {save.credits}", False, YELLOW), (x + 6, sy + 42))

    # LOADOUT
    ly = 162
    _panel(surf, x, ly, inner_w, 86, "LOADOUT", fonts)
    yy = ly + 10
    for label, key in (("MAIN", "main"), ("SIDE", "side"), ("SHLD", "shield"), ("ENGN", "engine")):
        lv = getattr(player.loadout, key)
        mx = MAX_LEVELS[key]
        col = GREEN if lv == mx else WHITE
        surf.blit(fonts["tiny"].render(label, False, DIM), (x + 6, yy))
        bar_x = x + 44
        cell_w = (inner_w - 50) // max(mx, 1)
        for i in range(mx):
            cell = pygame.Rect(bar_x + i * cell_w, yy + 2, cell_w - 1, 7)
            pygame.draw.rect(surf, DARKER, cell)
            if i < lv:
                pygame.draw.rect(surf, col, cell.inflate(-2, -2))
        yy += 18

    # ARMS
    ay = 258
    _panel(surf, x, ay, inner_w, 56, "ARMS", fonts)
    surf.blit(fonts["tiny"].render(f"BOMB x{player.loadout.bombs}", False, PURPLE), (x + 6, ay + 8))
    ab_name = ABILITY_NAMES.get(player.loadout.ability, "?")
    surf.blit(fonts["tiny"].render(ab_name.upper(), False, ORANGE if player.ability_cd <= 0 else DIM), (x + 6, ay + 24))
    cd_ratio = clamp(1 - player.ability_cd / 18.0, 0, 1)
    seg_color = ORANGE if cd_ratio >= 1 else (130, 80, 40)
    _segbar(surf, x + 6, ay + 40, inner_w - 12, 6, cd_ratio, seg_color, segments=8)

    # CONTROL (bottom)
    hy = SCREEN_H - 86
    _panel(surf, x, hy, inner_w, 78, "CONTROL", fonts)
    hints = [
        ("D", "move"),
        ("B", "fire"),
        ("A", "bomb"),
        ("X", "ability"),
        ("ST", "pause"),
    ]
    yy = hy + 10
    for k, v in hints:
        surf.blit(fonts["tiny"].render(k, False, CYAN), (x + 6, yy))
        surf.blit(fonts["tiny"].render(v, False, DIM), (x + 30, yy))
        yy += 13


# =============================================================================
# PLAY STATE
# =============================================================================

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
        self.bg_ribbon = BackgroundRibbon(level.theme)
        self.vignette = app.vignette
        self.difficulty = level.difficulty
        self.flash = 0
        self.shake = 0
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
        # Cinematic level transitions: ship launches from a station and docks at the next.
        n = int(level.key[1:]) if level.key.startswith("L") and level.key[1:].isdigit() else 1
        sec_here = (n - 1) // 10
        sec_next = min(9, n // 10)   # next sector index, capped at 9 for L100
        self.station_start = make_station(seed=n * 71 + 11, sector_idx=sec_here)
        self.station_end = make_station(seed=n * 71 + 137, sector_idx=sec_next)
        self.intro_t = 2.4
        self.outro_t = 0.0
        self._outro_start_y = float(self.player.y)
        # Ship starts inside the departing station, off the bottom of the playfield.
        self.player.y = PLAY_H + 36
        self.player.rect.center = (int(self.player.x), int(self.player.y))
        self.player.cinematic = True

    def run(self, events, controls):
        dt = 1.0 / FPS
        if controls.start_pressed:
            self.pause = not self.pause

        if not self.pause:
            self._update(dt, controls)
        self._draw(controls)
        if self.outcome is not None:
            return self.outcome
        return None

    def _update(self, dt, controls):
        self.stars.update(dt)
        self.nebula.update(dt)
        self.bg_ribbon.update(dt)
        self.boss_intro_t = max(0, self.boss_intro_t - dt)

        # Cinematic intro: ship climbs from the launch station into the playfield.
        if self.intro_t > 0:
            self.intro_t -= dt
            p = clamp(1.0 - max(0.0, self.intro_t) / 2.4, 0.0, 1.0)
            eased = 1.0 - (1.0 - p) ** 3
            self.player.y = lerp(PLAY_H + 36, PLAY_H - 60, eased)
            self.player.rect.center = (int(self.player.x), int(self.player.y))
            self.player.thrust += dt * 80   # extra-fast flame flicker during boost
            self.player.tilt = 0.0
            self.stars.update(dt * 1.6)
            self.sparks = [s for s in self.sparks if s.alive]
            self.explosions = [ex for ex in self.explosions if ex.alive]
            if self.intro_t <= 0:
                self.player.cinematic = False
                self.player.invuln = 1.0  # short grace period after takeoff
            return

        # Cinematic outro: gameplay frozen, ship climbs up and docks at next station.
        if self.outro_t > 0:
            self.outro_t -= dt
            p = clamp(1.0 - max(0.0, self.outro_t) / 2.4, 0.0, 1.0)
            eased = p * p
            self.player.y = lerp(self._outro_start_y, -40, eased)
            self.player.rect.center = (int(self.player.x), int(self.player.y))
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

        self.elapsed += dt
        # Spawn from timeline
        while self.timeline_idx < len(self.level.timeline):
            t, fn = self.level.timeline[self.timeline_idx]
            if self.elapsed >= t:
                fn(self)
                self.timeline_idx += 1
            else:
                break

        # Player
        self.player.update(dt, controls, self.bullets, lambda: self.enemies, self.particles,
                           self.app.sounds, self.lasers, on_bomb=self._bomb)

        # Bullets
        for b in self.bullets:
            b.update(dt)

        # Enemies
        for e in self.enemies:
            e.update(dt, self.bullets, lambda: self.player if self.player.alive else None, self.app.sounds)

        # Lasers (damage continuously)
        for laser in self.lasers:
            laser.update(dt)
            hit = laser.hit_rect()
            for e in self.enemies:
                if e.alive and hit.colliderect(e.rect):
                    if e.hit(int(laser.damage_per_sec * dt)):
                        self._on_kill(e)

        # Pickups
        for p in self.pickups:
            p.update(dt)

        for part in self.particles:
            part.update(dt)
        for s in self.sparks:
            s.update(dt)
        for ex in self.explosions:
            ex.update(dt)

        # Bullet vs enemy
        for b in self.bullets:
            if not (b.alive and b.friendly):
                continue
            for e in self.enemies:
                if e.alive and b.rect.colliderect(e.rect):
                    killed = e.hit(b.damage)
                    # impact sparks at the hit point
                    for _ in range(5):
                        self.sparks.append(Spark(b.rect.centerx, b.rect.centery, YELLOW))
                    self.sparks.append(Spark(b.rect.centerx, b.rect.centery, WHITE))
                    e.hit_flash_t = 0.08  # white-out flash on hit
                    if killed:
                        self._on_kill(e)
                    if b.pierce > 0:
                        b.pierce -= 1
                    else:
                        b.alive = False
                    break

        # Bullet vs player
        if self.player.alive:
            for b in self.bullets:
                if not (b.alive and not b.friendly):
                    continue
                if b.rect.colliderect(self.player.rect):
                    b.alive = False
                    self._damage_player(2)

        # Enemy vs player (ramming)
        if self.player.alive:
            for e in self.enemies:
                if e.alive and e.rect.colliderect(self.player.rect):
                    if not isinstance(e, Boss):
                        e.hit(99)
                        self._on_kill(e, drop=False)
                    self._damage_player(8)

        # Pickup pickup
        if self.player.alive:
            for p in self.pickups:
                if p.alive and p.rect.colliderect(self.player.rect):
                    p.alive = False
                    result = self.player.collect(p)
                    if result and result[0] == "credits":
                        self._earn(result[1])
                    self.app.sounds["money" if p.kind == "money" else "pickup"].play()

        # Cleanup
        self.bullets = [b for b in self.bullets if b.alive]
        self.enemies = [e for e in self.enemies if e.alive]
        self.pickups = [p for p in self.pickups if p.alive]
        self.particles = [p for p in self.particles if p.alive]
        self.sparks = [s for s in self.sparks if s.alive]
        self.explosions = [ex for ex in self.explosions if ex.alive]
        self.lasers = [l for l in self.lasers if l.alive]

        self.flash = max(0, self.flash - dt * 4)
        self.shake = max(0, self.shake - dt * 4)

        if self.message_timer > 0:
            self.message_timer -= dt

        # Win/loss
        if not self.player.alive:
            self.outcome = "loss"
        elif self.level.has_boss:
            if any(isinstance(e, Boss) for e in self.enemies):
                self.boss_spawned = True
            if self.boss_spawned and not any(isinstance(e, Boss) for e in self.enemies):
                self._begin_outro()
        else:
            if self.elapsed >= self.level.duration and not self.enemies:
                self._begin_outro()

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
        color = RED if is_boss else ORANGE
        n = 40 if is_boss else 16
        for _ in range(n):
            self.particles.append(Particle(cx, cy, color, size=4))
        if is_boss:
            # multi-stage explosion: several rings of different sizes and timings
            self.explosions.append(ExplosionRing(cx, cy, max_r=80, color=YELLOW, life=0.55))
            self.explosions.append(ExplosionRing(cx, cy, max_r=120, color=RED, life=0.80))
            self.explosions.append(ExplosionRing(cx - 20, cy + 10, max_r=50, color=ORANGE, life=0.5))
            self.explosions.append(ExplosionRing(cx + 25, cy - 15, max_r=55, color=ORANGE, life=0.6))
            for _ in range(4):
                kind = random.choice(["main", "side", "shield", "bomb"])
                self.pickups.append(Pickup(cx + random.uniform(-20, 20),
                                           cy + random.uniform(-20, 20),
                                           kind, self.assets["pickup_" + kind]))
            self.shake = 2.0
        else:
            self.explosions.append(ExplosionRing(cx, cy, max_r=int(max(enemy.rect.width, enemy.rect.height) * 0.9),
                                                 color=ORANGE, life=0.42))
            if drop and random.random() < enemy.DROP_CHANCE * self.scrap_drop_factor:
                kind = random.choice(enemy.DROP_TABLE)
                self.pickups.append(Pickup(cx, cy, kind, self.assets["pickup_" + kind]))
        self.app.sounds["big_boom" if is_boss else "boom"].play()

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
        screen = self.app.screen
        shake_x = random.randint(-int(self.shake * 3), int(self.shake * 3)) if self.shake > 0 else 0
        shake_y = random.randint(-int(self.shake * 3), int(self.shake * 3)) if self.shake > 0 else 0
        playfield = pygame.Surface((PLAY_W, PLAY_H))
        playfield.fill(BLACK)
        self.bg_ribbon.draw(playfield)
        self.nebula.draw(playfield)
        self.stars.draw(playfield)
        for p in self.pickups:
            p.draw(playfield)
        for b in self.bullets:
            b.draw(playfield)
        for laser in self.lasers:
            laser.draw(playfield)
        for e in self.enemies:
            e.draw(playfield)
        for part in self.particles:
            part.draw(playfield)
        for s in self.sparks:
            s.draw(playfield)
        for ex in self.explosions:
            ex.draw(playfield)
        if self.player.alive:
            self.player.draw(playfield)
        # Departing station scrolls down out of the screen during the intro.
        if self.intro_t > 0:
            p = clamp(1.0 - max(0.0, self.intro_t) / 2.4, 0.0, 1.0)
            sh = self.station_start.get_height()
            sx = (PLAY_W - self.station_start.get_width()) // 2
            # Starts with the top edge at PLAY_H - sh (fully visible at bottom);
            # ends with the top edge at PLAY_H (fully scrolled off the bottom).
            sy = int(PLAY_H - sh + p * (sh + 20))
            playfield.blit(self.station_start, (sx, sy))
        # Arrival station scrolls in from above during the outro.
        if self.outro_t > 0:
            p = clamp(1.0 - max(0.0, self.outro_t) / 2.4, 0.0, 1.0)
            sh = self.station_end.get_height()
            sx = (PLAY_W - self.station_end.get_width()) // 2
            entry = min(p / 0.5, 1.0)  # enters fully over the first half
            sy = int(-sh + entry * (sh + 20))
            playfield.blit(self.station_end, (sx, sy))
        if self.boss_intro_t > 0:
            self._draw_boss_intro(playfield)
        if self.player.bomb_flash > 0:
            o = pygame.Surface((PLAY_W, PLAY_H))
            o.fill(WHITE)
            o.set_alpha(int(180 * self.player.bomb_flash))
            playfield.blit(o, (0, 0))
        if self.flash > 0:
            o = pygame.Surface((PLAY_W, PLAY_H))
            o.fill(RED if self.outcome != "win" else CYAN)
            o.set_alpha(int(80 * self.flash))
            playfield.blit(o, (0, 0))
        playfield.blit(self.vignette, (0, 0))
        screen.fill(BLACK)
        screen.blit(playfield, (shake_x, shake_y))
        hud_draw(screen, self.app.fonts, self.assets, self.player, self.app.save,
                 self.level.name, self.score,
                 (self.level.duration - self.elapsed) if not self.level.has_boss else 0)

        if self.pause:
            _center_text(screen, self.app.fonts, "PAUSED", "START to resume")
        if self.outcome == "win":
            _center_text(screen, self.app.fonts, "MISSION COMPLETE", f"+{self.credits_earned} cr   B continue")
        elif self.outcome == "loss":
            _center_text(screen, self.app.fonts, "SHIP DESTROYED", "B continue")


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
        max_n = self._max_unlocked_n()
        self.sector_idx = (max_n - 1) // 10
        self.cursor = self._default_cursor()

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
        save = self.app.save
        # prefer the current node if it's in this sector
        if save.current_node in self._sector_keys():
            return save.current_node
        for k in self._sector_keys():
            if k in save.unlocked and k not in save.completed:
                return k
        # fall back to the first sector level
        return self._sector_keys()[0]

    def run(self, events, controls):
        dt = 1.0 / FPS
        self.t += dt
        self.stars.update(dt)

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

        # D-pad within sector
        if any(ev.type in (pygame.KEYDOWN, pygame.JOYHATMOTION) for ev in events):
            self._handle_nav(events)

        if controls.confirm_pressed:
            if self.cursor in self.app.save.unlocked:
                self.app.save.current_node = self.cursor
                self.app.save.save()
                level = self.app.levels[self.cursor]
                self.outcome = ("play", level)
            else:
                self.app.sounds["deny"].play()

        if controls.cancel_pressed:
            self.outcome = ("shop", None)

        self._draw(controls)
        return self.outcome

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
        screen.fill(BLACK)
        self.stars.draw(screen)

        save = self.app.save
        sector_name = SECTOR_NAMES[self.sector_idx]
        title = self.app.fonts["big"].render(sector_name, False, CYAN)
        screen.blit(title, title.get_rect(center=(PLAY_W // 2, 30)))
        sub = self.app.fonts["tiny"].render(
            f"SECTOR {self.sector_idx + 1} / 10", False, DIM)
        screen.blit(sub, sub.get_rect(center=(PLAY_W // 2, 56)))

        # L1/R1 indicators
        if self.sector_idx > 0:
            arrow = self.app.fonts["small"].render("< L1", False, DIM)
            screen.blit(arrow, (12, 30))
        if self.sector_idx < self._max_sector():
            arrow = self.app.fonts["small"].render("R1 >", False, DIM)
            screen.blit(arrow, (PLAY_W - arrow.get_width() - 12, 30))

        keys = self._sector_keys()
        # edges between consecutive levels in this sector
        for i in range(len(keys) - 1):
            a = MAP_GRAPH[keys[i]].pos
            b = MAP_GRAPH[keys[i + 1]].pos
            completed = keys[i] in save.completed
            color = GREEN if completed else DARKER
            pygame.draw.line(screen, color, a, b, 2)

        for i, k in enumerate(keys):
            node = MAP_GRAPH[k]
            cx, cy = node.pos
            is_boss = self.app.levels[k].has_boss
            done = k in save.completed
            avail = k in save.unlocked
            if done:        fill = GREEN
            elif avail:     fill = CYAN
            else:           fill = DARKER
            r = 18 if is_boss else 14
            pygame.draw.circle(screen, fill, (cx, cy), r)
            ring = WHITE if (avail or done) else (60, 60, 80)
            pygame.draw.circle(screen, ring, (cx, cy), r, 2)
            if k == self.cursor:
                pr = r + 4 + int(math.sin(self.t * 6) * 2)
                pygame.draw.circle(screen, YELLOW, (cx, cy), pr, 2)
            # Level number inside the node
            n = int(k[1:])
            label = f"B" if is_boss else f"{n}"
            ntxt = self.app.fonts["tiny"].render(label, False, BLACK if avail or done else DIM)
            screen.blit(ntxt, ntxt.get_rect(center=(cx, cy)))

        # right-side panel
        pygame.draw.rect(screen, HUD_BG, (HUD_X, 0, HUD_W, SCREEN_H))
        pygame.draw.line(screen, HUD_LINE, (HUD_X, 0), (HUD_X, SCREEN_H), 1)
        x = HUD_X + 8
        y = 12
        screen.blit(self.app.fonts["small"].render("PEWPEW", False, CYAN), (x, y)); y += 22
        screen.blit(self.app.fonts["tiny"].render(f"$ {save.credits}", False, YELLOW), (x, y)); y += 16
        screen.blit(self.app.fonts["tiny"].render(f"HI {save.high_score:08d}", False, DIM), (x, y)); y += 22

        node_level = self.app.levels[self.cursor]
        screen.blit(self.app.fonts["tiny"].render(self.cursor, False, WHITE), (x, y)); y += 14
        # Word-wrap the level name in the narrow HUD
        for token in node_level.name.split():
            screen.blit(self.app.fonts["tiny"].render(token, False, DIM), (x, y))
            y += 12
        y += 6
        if self.cursor in save.completed:
            screen.blit(self.app.fonts["tiny"].render("CLEARED", False, GREEN), (x, y)); y += 14
        elif self.cursor in save.unlocked:
            screen.blit(self.app.fonts["tiny"].render("READY", False, CYAN), (x, y)); y += 14
        else:
            screen.blit(self.app.fonts["tiny"].render("LOCKED", False, DIM), (x, y)); y += 14
        screen.blit(self.app.fonts["tiny"].render(f"DIFF x{node_level.difficulty:.2f}",
                                                  False, DIM), (x, y)); y += 14

        progress_n = sum(1 for k in save.completed if k.startswith("L"))
        screen.blit(self.app.fonts["tiny"].render(f"PROG {progress_n}/100",
                                                  False, ORANGE), (x, y)); y += 18

        y = SCREEN_H - 76
        for line in ("D-PAD  pick", "L1/R1 sector", "B  launch", "Y  shop"):
            screen.blit(self.app.fonts["tiny"].render(line, False, DIM), (x, y)); y += 14

        # End-of-game banner
        if progress_n >= 100:
            banner = self.app.fonts["small"].render("ALL CLEAR", False, GREEN)
            screen.blit(banner, banner.get_rect(center=(PLAY_W // 2, SCREEN_H - 24)))


# =============================================================================
# SHOP SCREEN
# =============================================================================

SHOP_ITEMS = [
    ("main",   "Main Cannon"),
    ("side",   "Side Missiles"),
    ("shield", "Shield Generator"),
    ("engine", "Engine"),
    ("bomb",   "Extra Bomb"),
    ("ability_screen_clear", "Ability: Pulse Bomb"),
    ("ability_shield_burst", "Ability: Shield Burst"),
    ("ability_mega_laser",   "Ability: Mega Laser"),
]


class ShopScreen:
    def __init__(self, app):
        self.app = app
        self.cursor = 0
        self.outcome = None
        self.flash_text = None
        self.flash_t = 0

    def run(self, events, controls):
        dt = 1.0 / FPS
        for ev in events:
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_UP:    self.cursor = (self.cursor - 1) % len(SHOP_ITEMS)
                if ev.key == pygame.K_DOWN:  self.cursor = (self.cursor + 1) % len(SHOP_ITEMS)
            if ev.type == pygame.JOYHATMOTION:
                _, hy = ev.value
                if hy > 0: self.cursor = (self.cursor - 1) % len(SHOP_ITEMS)
                if hy < 0: self.cursor = (self.cursor + 1) % len(SHOP_ITEMS)

        if controls.confirm_pressed:
            self._buy()
        if controls.cancel_pressed:
            self.app.save.save()
            self.outcome = ("map", None)

        if self.flash_t > 0:
            self.flash_t -= dt
        self._draw()
        if self.outcome:
            return self.outcome
        return None

    def _item_cost(self, key):
        save = self.app.save
        if key == "bomb":
            return BOMB_PRICE
        if key.startswith("ability_"):
            return 0
        lvl = getattr(save.loadout, key)
        costs = WEAPON_COSTS[key]
        if lvl >= MAX_LEVELS[key]:
            return None
        return costs[lvl]

    def _can_buy(self, key):
        save = self.app.save
        cost = self._item_cost(key)
        if key.startswith("ability_"):
            ability = key[len("ability_"):]
            return save.loadout.ability != ability
        if cost is None:
            return False
        return save.credits >= cost

    def _buy(self):
        key = SHOP_ITEMS[self.cursor][0]
        save = self.app.save
        if not self._can_buy(key):
            self.app.sounds["deny"].play()
            self.flash_text = "NOT ENOUGH" if not key.startswith("ability_") else "ALREADY EQUIPPED"
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
            cost = self._item_cost(key)
            save.credits -= cost
            setattr(save.loadout, key, getattr(save.loadout, key) + 1)
            self.flash_text = "UPGRADED"
        self.flash_t = 1.2
        self.app.sounds["confirm"].play()
        save.save()

    def _draw(self):
        screen = self.app.screen
        screen.fill(BLACK)
        # left panel
        pygame.draw.rect(screen, HUD_BG, (0, 0, PLAY_W, SCREEN_H))
        title = self.app.fonts["big"].render("HANGAR", False, CYAN)
        screen.blit(title, (20, 14))
        sub = self.app.fonts["tiny"].render(f"$ {self.app.save.credits}", False, YELLOW)
        screen.blit(sub, (20, 56))

        y = 90
        for i, (key, label) in enumerate(SHOP_ITEMS):
            row_color = WHITE if i == self.cursor else DIM
            cost = self._item_cost(key)
            line_left = label
            if key.startswith("ability_"):
                ability = key[len("ability_"):]
                equipped = self.app.save.loadout.ability == ability
                line_right = "EQUIPPED" if equipped else "free"
            elif key == "bomb":
                line_right = f"${BOMB_PRICE}    x{self.app.save.loadout.bombs}"
            else:
                lvl = getattr(self.app.save.loadout, key)
                mx = MAX_LEVELS[key]
                bars = "[" + "#" * lvl + "." * (mx - lvl) + "]"
                if cost is None:
                    line_right = f"{bars}  MAX"
                else:
                    line_right = f"{bars}  ${cost}"
            row_bg = (30, 36, 60) if i == self.cursor else None
            if row_bg:
                pygame.draw.rect(screen, row_bg, (12, y - 2, PLAY_W - 24, 22))
            left_surf = self.app.fonts["small"].render(line_left, False, row_color)
            right_surf = self.app.fonts["small"].render(line_right, False, row_color)
            screen.blit(left_surf, (24, y))
            screen.blit(right_surf, (PLAY_W - 24 - right_surf.get_width(), y))
            y += 26

        if self.flash_t > 0 and self.flash_text:
            txt = self.app.fonts["small"].render(self.flash_text, False, YELLOW)
            screen.blit(txt, txt.get_rect(center=(PLAY_W // 2, SCREEN_H - 36)))

        # right panel
        pygame.draw.rect(screen, HUD_BG, (HUD_X, 0, HUD_W, SCREEN_H))
        pygame.draw.line(screen, HUD_LINE, (HUD_X, 0), (HUD_X, SCREEN_H), 1)
        x = HUD_X + 8
        y = 12
        screen.blit(self.app.fonts["small"].render("PEWPEW", False, CYAN), (x, y)); y += 22
        screen.blit(self.app.fonts["tiny"].render("HANGAR", False, DIM), (x, y)); y += 18
        screen.blit(self.app.fonts["tiny"].render("D-PAD  pick", False, DIM), (x, y)); y += 14
        screen.blit(self.app.fonts["tiny"].render("B  buy", False, DIM), (x, y)); y += 14
        screen.blit(self.app.fonts["tiny"].render("Y  exit", False, DIM), (x, y)); y += 24

        # preview of current upgrade
        key = SHOP_ITEMS[self.cursor][0]
        screen.blit(self.app.fonts["tiny"].render("DETAIL:", False, DIM), (x, y)); y += 14
        desc = self._describe(key)
        for line in desc:
            screen.blit(self.app.fonts["tiny"].render(line, False, WHITE), (x, y))
            y += 14

    def _describe(self, key):
        save = self.app.save
        if key == "main":
            descs = ["L1: single shot", "L2: dual shot", "L3: triple spread", "L4: quad shot", "L5: quad + wing"]
            cur = save.loadout.main
            return [f"Lv {cur}/{MAX_LEVELS['main']}", descs[cur - 1]]
        if key == "side":
            descs = ["L0: none", "L1: 1 missile", "L2: 2 missiles", "L3: 2 + faster"]
            cur = save.loadout.side
            return [f"Lv {cur}/{MAX_LEVELS['side']}", descs[cur]]
        if key == "shield":
            cur = save.loadout.shield
            return [f"Lv {cur}/{MAX_LEVELS['shield']}", f"Max {SHIELD_MAX[cur]} HP", f"Regen {SHIELD_REGEN[cur]}/s"]
        if key == "engine":
            cur = save.loadout.engine
            return [f"Lv {cur}/{MAX_LEVELS['engine']}", f"{ENGINE_SPEEDS[cur]} px/s"]
        if key == "bomb":
            return ["Adds 1 bomb", "Max 9 held"]
        if key.startswith("ability_"):
            ability = key[len("ability_"):]
            details = {
                "screen_clear": ["Damages all", "enemies on", "screen"],
                "shield_burst": ["Refills shield", "+ brief invuln"],
                "mega_laser":   ["Sustained beam", "high DPS"],
            }
            return details[ability]
        return []


# =============================================================================
# TITLE / GAMEOVER
# =============================================================================

class TitleScreen:
    def __init__(self, app):
        self.app = app
        self.stars = ParallaxStars(SCREEN_W, SCREEN_H, counts=(80, 60, 40))
        self.t = 0
        self.outcome = None
        self.cursor = 0
        self.has_save = SAVE_PATH.exists()
        self.options = ["Continue" if self.has_save else "New Game", "New Game", "Quit"] if self.has_save else ["New Game", "Quit"]

    def run(self, events, controls):
        dt = 1.0 / FPS
        self.t += dt
        self.stars.update(dt)
        for ev in events:
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_UP:    self.cursor = (self.cursor - 1) % len(self.options)
                if ev.key == pygame.K_DOWN:  self.cursor = (self.cursor + 1) % len(self.options)
            if ev.type == pygame.JOYHATMOTION:
                _, hy = ev.value
                if hy > 0: self.cursor = (self.cursor - 1) % len(self.options)
                if hy < 0: self.cursor = (self.cursor + 1) % len(self.options)
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
        self._draw()
        return self.outcome

    def _draw(self):
        screen = self.app.screen
        screen.fill(BLACK)
        self.stars.draw(screen)
        logo = self.app.logo
        screen.blit(logo, logo.get_rect(center=(SCREEN_W // 2, 130)))
        sub = self.app.fonts["small"].render("a vertical shooter", False, DIM)
        screen.blit(sub, sub.get_rect(center=(SCREEN_W // 2, 180)))

        # menu options
        y = 260
        for i, opt in enumerate(self.options):
            sel = i == self.cursor
            color = YELLOW if sel else WHITE
            prefix = "> " if sel else "  "
            txt = self.app.fonts["small"].render(prefix + opt, False, color)
            screen.blit(txt, txt.get_rect(center=(SCREEN_W // 2, y)))
            y += 32

        if int(self.t * 2) % 2 == 0:
            press = self.app.fonts["tiny"].render("B confirm  |  D-PAD up/down", False, DIM)
            screen.blit(press, press.get_rect(center=(SCREEN_W // 2, 420)))


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
        b = self.app.fonts["huge"].render("SHIP LOST", False, RED)
        screen.blit(b, b.get_rect(center=(SCREEN_W // 2, 180)))
        s = self.app.fonts["small"].render(f"Score: {self.score}", False, WHITE)
        screen.blit(s, s.get_rect(center=(SCREEN_W // 2, 240)))
        h = self.app.fonts["tiny"].render(f"Best: {self.app.save.high_score}", False, DIM)
        screen.blit(h, h.get_rect(center=(SCREEN_W // 2, 268)))
        if int(self.t * 2) % 2 == 0:
            p = self.app.fonts["tiny"].render("B return to map", False, DIM)
            screen.blit(p, p.get_rect(center=(SCREEN_W // 2, 320)))
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
        self.vignette = make_vignette()
        self.logo = make_logo("PEWPEW", scale=6, color=(120, 220, 255))
        if pygame.mixer.get_init():
            self.sounds = make_sounds()
        else:
            self.sounds = {k: _Silent() for k in ("shoot", "shoot2", "hit", "boom", "big_boom",
                                                  "pickup", "money", "bomb", "menu", "confirm",
                                                  "deny", "warn")}
        self.fonts = {
            "huge":  pygame.font.SysFont(None, 72, bold=True),
            "big":   pygame.font.SysFont(None, 40, bold=True),
            "small": pygame.font.SysFont(None, 22, bold=True),
            "tiny":  pygame.font.SysFont(None, 16, bold=True),
        }
        self.levels = make_levels()
        self.save = SaveData.load()
        self.state = TitleScreen(self)
        self.controls = Controls()

    def run(self):
        running = True
        select_held = False
        start_held = False
        while running:
            self.clock.tick(FPS)
            events = pygame.event.get()
            for ev in events:
                if ev.type == pygame.QUIT:
                    running = False
                if ev.type == pygame.JOYBUTTONDOWN:
                    if ev.button == JOY_SELECT: select_held = True
                    if ev.button == JOY_START:  start_held = True
                    if ev.button == JOY_MENU:   running = False
                if ev.type == pygame.JOYBUTTONUP:
                    if ev.button == JOY_SELECT: select_held = False
                    if ev.button == JOY_START:  start_held = False
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_F4 and (pygame.key.get_mods() & pygame.KMOD_ALT):
                    running = False

            if select_held and start_held:
                running = False

            self.controls.poll(self.joys, events)
            outcome = self.state.run(events, self.controls)
            if outcome is not None:
                kind, payload = outcome
                self._transition(kind, payload)
            pygame.display.flip()

        self.save.save()
        pygame.quit()

    def _transition(self, kind, payload):
        if kind == "play":
            level = payload
            self.state = PlayState(self, level)
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
    if out == "win":
        return ("post_play", (self.score, self.level.key, True))
    if out == "loss":
        return ("post_play", (self.score, self.level.key, False))
    return None


PlayState.run = _play_run


def main():
    windowed = "--windowed" in sys.argv
    App(windowed=windowed).run()


if __name__ == "__main__":
    main()
