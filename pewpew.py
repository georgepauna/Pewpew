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
    ".......##.......",
    "......####......",
    "......####......",
    ".....#wwww#.....",
    ".....######.....",
    "....########....",
    "...#cCCCCCCc#...",
    "..#cCcCCCCcCc#..",
    ".#bcCCCCCCCCcb#.",
    "#bCCCCCcccCCCCb#",
    "#bCCCCCcccCCCCb#",
    ".#bcCCCCCCCCcb#.",
    "..#cCcCCCCcCc#..",
    "...#cCCCCCCc#...",
    "....########....",
    ".....#oooo#.....",
    "......oOOo......",
    ".......oo.......",
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
    txt = f.render(letter, True, BLACK)
    s.blit(txt, txt.get_rect(center=(7, 7)))
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
    # Nearest-neighbor scale to give the pixel art readable size on a 480-wide playfield.
    a = {}
    scales = {"player": 2, "scout": 2, "gunner": 2, "weaver": 2,
              "bomber": 2, "kamikaze": 2, "turret": 2, "boss": 3}
    for k, surf in raw.items():
        s = scales[k]
        a[k] = pygame.transform.scale(surf, (surf.get_width() * s, surf.get_height() * s))
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
    current_node: str = "start"
    completed: list = field(default_factory=list)
    unlocked: list = field(default_factory=lambda: ["start"])
    high_score: int = 0
    loadout: Loadout = field(default_factory=Loadout)

    @staticmethod
    def load():
        try:
            raw = json.loads(SAVE_PATH.read_text())
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

class Starfield:
    def __init__(self, n=100):
        self.stars = []
        for _ in range(n):
            self.stars.append([
                random.uniform(0, PLAY_W),
                random.uniform(0, PLAY_H),
                random.choice([50, 100, 170]),
            ])

    def update(self, dt):
        for s in self.stars:
            s[1] += s[2] * dt
            if s[1] > PLAY_H:
                s[1] = 0
                s[0] = random.uniform(0, PLAY_W)

    def draw(self, surf):
        for x, y, speed in self.stars:
            shade = min(255, int(80 + speed))
            surf.set_at((int(x), int(y)), (shade, shade, shade))


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
        pygame.draw.rect(surf, self.color, self.rect)


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
        if self.invuln > 0:
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
        if self.invuln > 0 and int(self.invuln * 20) % 2 == 0:
            return
        # engine flame
        flicker = (int(self.thrust) % 4)
        fx = self.rect.centerx
        fy = self.rect.bottom - 2
        pygame.draw.polygon(surf, ORANGE, [
            (fx - 3, fy),
            (fx + 3, fy),
            (fx, fy + 6 + flicker),
        ])
        pygame.draw.polygon(surf, YELLOW, [
            (fx - 2, fy),
            (fx + 2, fy),
            (fx, fy + 3 + flicker // 2),
        ])
        surf.blit(self.image, self.rect)
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

    def __init__(self, x, y, asset, hp=1):
        self.image = asset
        self.rect = asset.get_rect(center=(int(x), int(y)))
        self.x = float(x)
        self.y = float(y)
        self.hp = hp
        self.max_hp = hp
        self.alive = True
        self.t = 0
        self.fire_cd = random.uniform(1.0, 2.5)

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

    def __init__(self, x, asset):
        super().__init__(x, -20, asset, hp=1)
        self.speed = random.uniform(130, 170)

    def _move(self, dt):
        self.y += self.speed * dt
        self.x += math.sin(self.t * 2 + self.x) * 30 * dt


class Gunner(Enemy):
    SCORE = 40
    CREDITS = 30
    DROP_CHANCE = 0.12
    DROP_TABLE = ("money", "money", "shield")

    def __init__(self, x, asset):
        super().__init__(x, -24, asset, hp=3)
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

    def __init__(self, x, asset):
        super().__init__(x, -20, asset, hp=2)
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

    def __init__(self, x, asset):
        super().__init__(x, -30, asset, hp=8)
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

    def __init__(self, x, asset):
        super().__init__(x, -20, asset, hp=2)
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

    def __init__(self, x, asset):
        super().__init__(x, -24, asset, hp=5)
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

    def __init__(self, asset):
        x = PLAY_W // 2
        super().__init__(x, -120, asset, hp=240)
        self.speed = 60
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
    if kind == "scout":     return Scout(x, assets["scout"])
    if kind == "gunner":    return Gunner(x, assets["gunner"])
    if kind == "weaver":    return Weaver(x, assets["weaver"])
    if kind == "bomber":    return Bomber(x, assets["bomber"])
    if kind == "kamikaze":  return Kamikaze(x, assets["kamikaze"])
    if kind == "turret":    return Turret(x, assets["turret"])
    if kind == "boss":      return Boss(assets["boss"])
    raise ValueError(kind)


def spawn_line(kind, count, gap=50, y_off=0):
    def fn(state):
        total = (count - 1) * gap
        start_x = (PLAY_W - total) / 2
        for i in range(count):
            e = _enemy_factory(kind, start_x + i * gap, state.assets)
            e.y += y_off
            state.enemies.append(e)
    return fn


def spawn_v(kind, count):
    def fn(state):
        for i in range(count):
            x = PLAY_W // 2 + (i - count // 2) * 40
            e = _enemy_factory(kind, x, state.assets)
            e.y = -30 - abs(i - count // 2) * 30
            state.enemies.append(e)
    return fn


def spawn_random(kind, count, x_range=(40, PLAY_W - 40)):
    def fn(state):
        for _ in range(count):
            x = random.uniform(*x_range)
            state.enemies.append(_enemy_factory(kind, x, state.assets))
    return fn


def spawn_at(kind, x):
    def fn(state):
        state.enemies.append(_enemy_factory(kind, x, state.assets))
    return fn


def spawn_boss():
    def fn(state):
        state.enemies.append(_enemy_factory("boss", 0, state.assets))
        state.is_boss_fight = True
    return fn


@dataclass
class Level:
    key: str
    name: str
    nebula: tuple
    timeline: list
    duration: float
    has_boss: bool = False


def make_levels():
    return {
        "start": Level(
            key="start", name="Launch Sector",
            nebula=(40, 80, 160),
            duration=55,
            timeline=[
                (1.5,  spawn_line("scout", 5, gap=70)),
                (5.0,  spawn_v("scout", 5)),
                (10.0, spawn_random("scout", 4)),
                (14.0, spawn_at("gunner", PLAY_W * 0.3)),
                (14.5, spawn_at("gunner", PLAY_W * 0.7)),
                (20.0, spawn_v("weaver", 3)),
                (26.0, spawn_random("scout", 6)),
                (32.0, spawn_line("weaver", 4, gap=80)),
                (38.0, spawn_at("bomber", PLAY_W * 0.5)),
                (44.0, spawn_random("scout", 5)),
                (50.0, spawn_v("kamikaze", 3)),
            ]),
        "asteroid": Level(
            key="asteroid", name="Asteroid Field",
            nebula=(120, 80, 60),
            duration=65,
            timeline=[
                (1.0,  spawn_random("kamikaze", 3)),
                (5.0,  spawn_line("scout", 6, gap=60)),
                (10.0, spawn_random("kamikaze", 4)),
                (16.0, spawn_v("weaver", 5)),
                (22.0, spawn_random("kamikaze", 5)),
                (28.0, spawn_at("bomber", PLAY_W * 0.3)),
                (29.0, spawn_at("bomber", PLAY_W * 0.7)),
                (36.0, spawn_random("scout", 8)),
                (44.0, spawn_random("kamikaze", 6)),
                (52.0, spawn_v("weaver", 6)),
                (58.0, spawn_at("bomber", PLAY_W * 0.5)),
            ]),
        "outpost": Level(
            key="outpost", name="Outpost Run",
            nebula=(80, 40, 130),
            duration=70,
            timeline=[
                (1.5,  spawn_at("turret", PLAY_W * 0.25)),
                (2.0,  spawn_at("turret", PLAY_W * 0.75)),
                (6.0,  spawn_line("scout", 5, gap=70)),
                (12.0, spawn_at("gunner", PLAY_W * 0.5)),
                (16.0, spawn_at("turret", PLAY_W * 0.5)),
                (22.0, spawn_v("scout", 6)),
                (28.0, spawn_at("gunner", PLAY_W * 0.2)),
                (28.5, spawn_at("gunner", PLAY_W * 0.8)),
                (36.0, spawn_at("turret", PLAY_W * 0.3)),
                (36.5, spawn_at("turret", PLAY_W * 0.7)),
                (44.0, spawn_random("scout", 6)),
                (52.0, spawn_v("weaver", 4)),
                (60.0, spawn_at("bomber", PLAY_W * 0.5)),
            ]),
        "converge": Level(
            key="converge", name="Sector Crossing",
            nebula=(50, 110, 90),
            duration=75,
            timeline=[
                (1.5,  spawn_v("scout", 7)),
                (6.0,  spawn_random("kamikaze", 4)),
                (10.0, spawn_at("gunner", PLAY_W * 0.3)),
                (10.5, spawn_at("gunner", PLAY_W * 0.7)),
                (16.0, spawn_line("weaver", 5, gap=70)),
                (24.0, spawn_at("turret", PLAY_W * 0.25)),
                (24.5, spawn_at("turret", PLAY_W * 0.75)),
                (30.0, spawn_at("bomber", PLAY_W * 0.3)),
                (30.5, spawn_at("bomber", PLAY_W * 0.7)),
                (40.0, spawn_random("kamikaze", 6)),
                (48.0, spawn_v("weaver", 7)),
                (56.0, spawn_at("gunner", PLAY_W * 0.5)),
                (60.0, spawn_at("turret", PLAY_W * 0.5)),
                (66.0, spawn_random("scout", 8)),
            ]),
        "boss": Level(
            key="boss", name="Sector Boss",
            nebula=(140, 40, 80),
            duration=999,
            has_boss=True,
            timeline=[
                (1.0,  spawn_random("scout", 5)),
                (5.0,  spawn_v("kamikaze", 4)),
                (10.0, spawn_at("gunner", PLAY_W * 0.3)),
                (10.5, spawn_at("gunner", PLAY_W * 0.7)),
                (16.0, spawn_boss()),
            ]),
    }


# =============================================================================
# MISSION MAP
# =============================================================================

@dataclass
class MapNode:
    key: str
    name: str
    pos: tuple
    nexts: list


MAP_GRAPH = {
    "start":    MapNode("start",    "Launch Sector",   (70, 360),  ["asteroid", "outpost"]),
    "asteroid": MapNode("asteroid", "Asteroid Field",  (180, 200), ["converge"]),
    "outpost":  MapNode("outpost",  "Outpost Run",     (180, 400), ["converge"]),
    "converge": MapNode("converge", "Sector Crossing", (310, 290), ["boss"]),
    "boss":     MapNode("boss",     "Sector Boss",     (430, 200), []),
}


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

def hud_draw(surf, fonts, assets, player, save, level_name, score, time_left):
    pygame.draw.rect(surf, HUD_BG, (HUD_X, 0, HUD_W, SCREEN_H))
    pygame.draw.line(surf, HUD_LINE, (HUD_X, 0), (HUD_X, SCREEN_H), 1)

    x = HUD_X + 8
    y = 6
    title = fonts["small"].render("PEWPEW", True, CYAN)
    surf.blit(title, (x, y))
    y += 22

    surf.blit(fonts["tiny"].render(level_name.upper(), True, DIM), (x, y))
    y += 14
    surf.blit(fonts["tiny"].render(f"TIME {max(0, int(time_left))}", True, DIM), (x, y))
    y += 18

    surf.blit(fonts["tiny"].render("SHIELD", True, DIM), (x, y))
    y += 12
    bar_w = HUD_W - 16
    pygame.draw.rect(surf, DARKER, (x, y, bar_w, 8))
    if player.shield_max > 0:
        ratio = max(0, player.shield_hp / player.shield_max)
        pygame.draw.rect(surf, CYAN, (x, y, int(bar_w * ratio), 8))
    pygame.draw.rect(surf, HUD_LINE, (x, y, bar_w, 8), 1)
    y += 18

    surf.blit(fonts["tiny"].render(f"SCORE {score:08d}", True, WHITE), (x, y))
    y += 14
    surf.blit(fonts["tiny"].render(f"$ {save.credits}", True, YELLOW), (x, y))
    y += 22

    surf.blit(fonts["tiny"].render("LOADOUT", True, DIM), (x, y))
    y += 12
    for label, key in (("MAIN", "main"), ("SIDE", "side"), ("SHLD", "shield"), ("ENGN", "engine")):
        lv = getattr(player.loadout, key)
        mx = MAX_LEVELS[key]
        col = GREEN if lv == mx else WHITE
        bar_x = x + 38
        surf.blit(fonts["tiny"].render(label, True, DIM), (x, y))
        for i in range(mx):
            cell = pygame.Rect(bar_x + i * 11, y + 2, 8, 7)
            pygame.draw.rect(surf, DARKER, cell)
            if i < lv:
                pygame.draw.rect(surf, col, cell.inflate(-2, -2))
        y += 12
    y += 8

    surf.blit(fonts["tiny"].render(f"BOMBS x{player.loadout.bombs}", True, PURPLE), (x, y))
    y += 14
    ab_name = ABILITY_NAMES.get(player.loadout.ability, "?")
    surf.blit(fonts["tiny"].render(ab_name.upper(), True, DIM), (x, y))
    y += 12
    cd_ratio = clamp(1 - player.ability_cd / 18.0, 0, 1)
    pygame.draw.rect(surf, DARKER, (x, y, bar_w, 6))
    pygame.draw.rect(surf, ORANGE if cd_ratio >= 1 else DIM, (x, y, int(bar_w * cd_ratio), 6))
    y += 16

    # control hint at bottom
    hints = [
        "B  fire",
        "A  bomb",
        "X  ability",
        "STRT pause",
    ]
    y = SCREEN_H - 14 * len(hints) - 4
    for h in hints:
        surf.blit(fonts["tiny"].render(h, True, DIM), (x, y))
        y += 14


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
        self.stars = Starfield(120)
        self.nebula = Nebula(level.nebula)
        self.flash = 0
        self.shake = 0
        self.is_boss_fight = False
        self.boss_spawned = False
        self.outcome = None
        self.pause = False
        self.message = None
        self.message_timer = 0
        self.credits_earned = 0
        self.scrap_drop_factor = 1.0

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

        # Bullet vs enemy
        for b in self.bullets:
            if not (b.alive and b.friendly):
                continue
            for e in self.enemies:
                if e.alive and b.rect.colliderect(e.rect):
                    if isinstance(e, Boss):
                        # Boss can't be one-shot bypassed; standard hit.
                        pass
                    killed = e.hit(b.damage)
                    self.particles.append(Particle(b.rect.centerx, b.rect.centery, ORANGE, size=3))
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
        self.lasers = [l for l in self.lasers if l.alive]

        self.flash = max(0, self.flash - dt * 4)
        self.shake = max(0, self.shake - dt * 4)

        if self.message_timer > 0:
            self.message_timer -= dt

        # Win/loss
        if not self.player.alive:
            self.outcome = "loss"
        elif self.level.has_boss:
            if self.boss_spawned and not any(isinstance(e, Boss) for e in self.enemies):
                self.outcome = "win"
            if any(isinstance(e, Boss) for e in self.enemies):
                self.boss_spawned = True
        else:
            if self.elapsed >= self.level.duration and not self.enemies:
                self.outcome = "win"

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
        if isinstance(enemy, Boss):
            self._earn(enemy.CREDITS)
        else:
            self._earn(enemy.CREDITS)
        # Particles
        color = ORANGE if not isinstance(enemy, Boss) else RED
        n = 40 if isinstance(enemy, Boss) else 16
        for _ in range(n):
            self.particles.append(Particle(enemy.rect.centerx, enemy.rect.centery, color, size=4))
        self.app.sounds["big_boom" if isinstance(enemy, Boss) else "boom"].play()
        if isinstance(enemy, Boss):
            # drop several pickups
            for _ in range(4):
                kind = random.choice(["main", "side", "shield", "bomb"])
                self.pickups.append(Pickup(enemy.rect.centerx + random.uniform(-20, 20),
                                           enemy.rect.centery + random.uniform(-20, 20),
                                           kind, self.assets["pickup_" + kind]))
            self.shake = 2.0
        elif drop and random.random() < enemy.DROP_CHANCE * self.scrap_drop_factor:
            kind = random.choice(enemy.DROP_TABLE)
            self.pickups.append(Pickup(enemy.rect.centerx, enemy.rect.centery, kind, self.assets["pickup_" + kind]))

    def _earn(self, amount):
        self.credits_earned += amount
        self.app.save.credits += amount

    def _draw(self, controls):
        screen = self.app.screen
        shake_x = random.randint(-int(self.shake * 3), int(self.shake * 3)) if self.shake > 0 else 0
        shake_y = random.randint(-int(self.shake * 3), int(self.shake * 3)) if self.shake > 0 else 0
        playfield = pygame.Surface((PLAY_W, PLAY_H))
        playfield.fill(BLACK)
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
        if self.player.alive:
            self.player.draw(playfield)
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
    b = fonts["big"].render(big, True, WHITE)
    s = fonts["small"].render(small, True, DIM)
    surf.blit(b, b.get_rect(center=(cx, cy - 10)))
    surf.blit(s, s.get_rect(center=(cx, cy + 20)))


# =============================================================================
# MISSION MAP SCREEN
# =============================================================================

class MapScreen:
    def __init__(self, app):
        self.app = app
        self.cursor = self._first_available()
        self.stars = Starfield(80)
        self.t = 0
        self.outcome = None

    def _first_available(self):
        save = self.app.save
        if save.current_node in save.unlocked and save.current_node not in save.completed:
            return save.current_node
        for k in save.unlocked:
            if k not in save.completed:
                return k
        return save.unlocked[-1] if save.unlocked else "start"

    def _available_keys(self):
        # All unlocked nodes are navigable; completed nodes can be replayed for credits.
        return list(self.app.save.unlocked)

    def run(self, events, controls):
        dt = 1.0 / FPS
        self.t += dt
        self.stars.update(dt)

        if controls.cancel_pressed:
            # back to title? for now, do nothing
            pass

        # Navigation: move cursor between available nodes by direction
        if any(ev.type == pygame.KEYDOWN and ev.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN) for ev in events) \
                or any(ev.type == pygame.JOYHATMOTION for ev in events) \
                or any(ev.type == pygame.JOYBUTTONDOWN for ev in events):
            self._handle_nav(events)

        if controls.confirm_pressed:
            avail = self._available_keys()
            if self.cursor in avail:
                # play this level
                self.app.save.current_node = self.cursor
                self.app.save.save()
                level = self.app.levels[self.cursor]
                self.outcome = ("play", level)

        self._draw(controls)
        if self.outcome is not None:
            return self.outcome
        return None

    def _handle_nav(self, events):
        # cycle through available nodes in events
        avail = self._available_keys()
        if not avail:
            return
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
            # find nearest available in that direction
            best = None
            best_score = 1e9
            for k in avail:
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

        title = self.app.fonts["big"].render("MISSION SELECT", True, CYAN)
        screen.blit(title, title.get_rect(center=(SCREEN_W // 2, 36)))

        # draw edges
        for k, node in MAP_GRAPH.items():
            for nxt in node.nexts:
                a = node.pos
                b = MAP_GRAPH[nxt].pos
                completed = k in self.app.save.completed
                color = GREEN if completed else DARKER
                pygame.draw.line(screen, color, a, b, 2)

        for k, node in MAP_GRAPH.items():
            in_save = k in self.app.save.unlocked
            done = k in self.app.save.completed
            avail = in_save and not done
            cx, cy = node.pos
            if done:
                fill = GREEN
            elif avail:
                fill = CYAN
            else:
                fill = DARKER
            pygame.draw.circle(screen, fill, (cx, cy), 14)
            pygame.draw.circle(screen, WHITE if avail or done else (60, 60, 80), (cx, cy), 14, 2)
            if k == self.cursor:
                r = 18 + int(math.sin(self.t * 6) * 2)
                pygame.draw.circle(screen, YELLOW, (cx, cy), r, 2)
            txt = self.app.fonts["tiny"].render(node.name, True, WHITE if avail or done else DIM)
            screen.blit(txt, txt.get_rect(center=(cx, cy + 28)))

        # right-side panel
        pygame.draw.rect(screen, HUD_BG, (HUD_X, 0, HUD_W, SCREEN_H))
        pygame.draw.line(screen, HUD_LINE, (HUD_X, 0), (HUD_X, SCREEN_H), 1)
        x = HUD_X + 8
        y = 12
        screen.blit(self.app.fonts["small"].render("PEWPEW", True, CYAN), (x, y)); y += 22
        screen.blit(self.app.fonts["tiny"].render(f"$ {self.app.save.credits}", True, YELLOW), (x, y)); y += 18
        screen.blit(self.app.fonts["tiny"].render(f"HI {self.app.save.high_score:08d}", True, DIM), (x, y)); y += 20

        node = MAP_GRAPH[self.cursor]
        screen.blit(self.app.fonts["tiny"].render("> " + node.name.upper(), True, WHITE), (x, y)); y += 16
        if self.cursor in self.app.save.completed:
            screen.blit(self.app.fonts["tiny"].render("CLEARED", True, GREEN), (x, y)); y += 14
        elif self.cursor in self.app.save.unlocked:
            screen.blit(self.app.fonts["tiny"].render("READY", True, CYAN), (x, y)); y += 14
        else:
            screen.blit(self.app.fonts["tiny"].render("LOCKED", True, DIM), (x, y)); y += 14

        y = SCREEN_H - 90
        for line in ("D-PAD  pick", "B  launch", "Y  shop", "SEL+ST  quit"):
            screen.blit(self.app.fonts["tiny"].render(line, True, DIM), (x, y)); y += 14

        # All sectors cleared banner
        save = self.app.save
        if save.unlocked and all(k in save.completed for k in save.unlocked) and "boss" in save.completed:
            banner = self.app.fonts["small"].render("ALL SECTORS CLEAR", True, GREEN)
            screen.blit(banner, banner.get_rect(center=(PLAY_W // 2, SCREEN_H - 24)))

        if controls.cancel_pressed:
            self.outcome = ("shop", None)


def events_passthrough(controls):
    # placeholder for future shared handling
    return []


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
        title = self.app.fonts["big"].render("HANGAR", True, CYAN)
        screen.blit(title, (20, 14))
        sub = self.app.fonts["tiny"].render(f"$ {self.app.save.credits}", True, YELLOW)
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
            left_surf = self.app.fonts["small"].render(line_left, True, row_color)
            right_surf = self.app.fonts["small"].render(line_right, True, row_color)
            screen.blit(left_surf, (24, y))
            screen.blit(right_surf, (PLAY_W - 24 - right_surf.get_width(), y))
            y += 26

        if self.flash_t > 0 and self.flash_text:
            txt = self.app.fonts["small"].render(self.flash_text, True, YELLOW)
            screen.blit(txt, txt.get_rect(center=(PLAY_W // 2, SCREEN_H - 36)))

        # right panel
        pygame.draw.rect(screen, HUD_BG, (HUD_X, 0, HUD_W, SCREEN_H))
        pygame.draw.line(screen, HUD_LINE, (HUD_X, 0), (HUD_X, SCREEN_H), 1)
        x = HUD_X + 8
        y = 12
        screen.blit(self.app.fonts["small"].render("PEWPEW", True, CYAN), (x, y)); y += 22
        screen.blit(self.app.fonts["tiny"].render("HANGAR", True, DIM), (x, y)); y += 18
        screen.blit(self.app.fonts["tiny"].render("D-PAD  pick", True, DIM), (x, y)); y += 14
        screen.blit(self.app.fonts["tiny"].render("B  buy", True, DIM), (x, y)); y += 14
        screen.blit(self.app.fonts["tiny"].render("Y  exit", True, DIM), (x, y)); y += 24

        # preview of current upgrade
        key = SHOP_ITEMS[self.cursor][0]
        screen.blit(self.app.fonts["tiny"].render("DETAIL:", True, DIM), (x, y)); y += 14
        desc = self._describe(key)
        for line in desc:
            screen.blit(self.app.fonts["tiny"].render(line, True, WHITE), (x, y))
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
        self.stars = Starfield(120)
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
        title = self.app.fonts["huge"].render("PEWPEW", True, CYAN)
        screen.blit(title, title.get_rect(center=(SCREEN_W // 2, 130)))
        sub = self.app.fonts["small"].render("a vertical shooter", True, DIM)
        screen.blit(sub, sub.get_rect(center=(SCREEN_W // 2, 180)))

        # menu options
        y = 260
        for i, opt in enumerate(self.options):
            sel = i == self.cursor
            color = YELLOW if sel else WHITE
            prefix = "> " if sel else "  "
            txt = self.app.fonts["small"].render(prefix + opt, True, color)
            screen.blit(txt, txt.get_rect(center=(SCREEN_W // 2, y)))
            y += 32

        if int(self.t * 2) % 2 == 0:
            press = self.app.fonts["tiny"].render("B confirm  |  D-PAD up/down", True, DIM)
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
        b = self.app.fonts["huge"].render("SHIP LOST", True, RED)
        screen.blit(b, b.get_rect(center=(SCREEN_W // 2, 180)))
        s = self.app.fonts["small"].render(f"Score: {self.score}", True, WHITE)
        screen.blit(s, s.get_rect(center=(SCREEN_W // 2, 240)))
        h = self.app.fonts["tiny"].render(f"Best: {self.app.save.high_score}", True, DIM)
        screen.blit(h, h.get_rect(center=(SCREEN_W // 2, 268)))
        if int(self.t * 2) % 2 == 0:
            p = self.app.fonts["tiny"].render("B return to map", True, DIM)
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
