"""Manual sprite editor for Pewpew.

Define the source rect for every sprite on every sheet, scale it down to the
engine-final size, set its pivot, preview the result in mock in-game scenes,
and write the PNGs that ship to the device. The auto-slicer (_slice_art.py)
handles the first-pass cuts; on first open this tool seeds every sprite's
rect with that auto-detection result, and you nudge from there.

Manifest schema (art/sprite_manifest.json) — one entry per sprite:
  {"rect": [x, y, w, h], "scale": 0.27, "pivot": [px, py] | null}
- `rect`  source-image pixels on the sheet
- `scale` uniform downscale applied AFTER trim_dark_border (aspect locked)
- `pivot` in UNTRIMMED-scaled-crop pixels; null = sprite centre

On save we also write art/sprite_pivots.json — one entry per sprite that has
a custom pivot, expressed in TRIMMED-PNG pixels so the engine can do
  surf.blit(sprite, (entity_x - pivot[0], entity_y - pivot[1]))
without knowing anything about the editor's coord system.

Controls (keyboard / Xbox gamepad):

  Sheet                ;  '                  /  SELECT  START
  Sprite               [  ]                  /  LB  RB
  Source rect pos      arrows                /  D-pad
  Source rect size     A D W S               /  X B Y A
                         (right + bottom edges; top-left anchored)
  Scale                Ctrl + A / D          /  R2 + X / B    (W S / Y A no-op)
  Pivot                Ctrl + arrows         /  R2 + D-pad
  Big stride (5x)      Shift held            /  L2 held
  Mouse drag           set initial rect      /  (mouse only)
  Backspace            clear active rect
  R                    reload manifest from disk
  End                  save manifest + sprite PNGs + pivots   /  R2 + START
  Esc                  quit  (warns once if unsaved)          /  R2 + SELECT

The panel preview shows the UNTRIMMED scaled crop so the dark border you'll
lose at save time is visible. Crop-mark brackets around the preview indicate
where the trim cut will land. The bottom scene strip continues to render
trimmed sprites (matches what the game sees).
"""
import json
import os
import sys
from pathlib import Path
import pygame

ROOT = Path(r"d:\Github\Pewpew")
ART = ROOT / "art"
SPRITES = ART / "sprites"
MANIFEST_PATH = ART / "sprite_manifest.json"
# Single engine-facing file: pivots, hitboxes, dummy positions for every
# sprite. All coordinates in trimmed-PNG pixels so pewpew.py can use them
# directly without knowing about the editor's untrimmed-scaled coord system.
ENGINE_DATA_PATH = ART / "sprite_engine.json"

WIN_W, WIN_H = 1366, 800
TOPBAR_H = 32
MARGIN = 8
PANEL_W = 380
SCENE_STRIP_H = 296
EDITOR_H = WIN_H - TOPBAR_H - SCENE_STRIP_H - MARGIN

BG = (18, 20, 28)
PANEL_BG = (28, 32, 44)
BORDER = (60, 70, 100)
INK = (220, 230, 240)
DIM = (130, 140, 160)
ACCENT = (255, 196, 64)
ACTIVE_OUTLINE = (255, 255, 255)
OTHER_OUTLINE = (255, 196, 64)
DRAG_OUTLINE = (120, 220, 255)
LIST_HIGHLIGHT = (60, 70, 100)
DIRTY = (255, 110, 110)
SAVED = (120, 220, 140)
PREVIEW_BG = (8, 10, 16)
SCENE_BG = (6, 8, 14)
SCENE_LABEL = (160, 200, 240)
QUIT_WARN_BG = (90, 30, 40)
QUIT_WARN_FG = (255, 240, 200)
TRIM_MARK_COLOR = (200, 140, 80)
TRIM_MARK_BAD = (255, 80, 80)
PIVOT_BLINK_MS = 220
HITBOX_COLOR_ACTIVE = (110, 220, 140)
HITBOX_COLOR_DIM = (50, 100, 70)
DUMMY_COLOR_ACTIVE = (255, 196, 64)
DUMMY_COLOR_DIM = (110, 130, 170)

# Game-side scale used to space player barrels/missiles/drones in source
# pixels — copied from pewpew.PLAY_SCALE to keep defaults aligned with the
# game's existing hard-coded offsets.
PLAY_SCALE = 1.5

EDIT_MODES = ("rect", "hitbox", "helpers")

INITIAL_REPEAT_MS = 250
REPEAT_INTERVAL_MS = 60
TRIGGER_THRESHOLD = 0.1

# Xbox / XInput button indices on pygame.joystick under Windows
JB_A, JB_B, JB_X, JB_Y = 0, 1, 2, 3
JB_LB, JB_RB = 4, 5
JB_BACK, JB_START = 6, 7
JB_LSB, JB_RSB = 8, 9
# Trigger axes on most XInput drivers
JA_LT, JA_RT = 4, 5

# ---- Per-sprite role + default helper geometry ---------------------------
# Only the straight-flying player owns helper positions. The banking
# variants derive their helper positions from it by image-size ratio
# (see _banking_derived_dummies in Editor + the engine-side Player code).
PLAYER_SPRITES = ("player",)
BANKING_SPRITES = (
    "player_left", "player_right", "player_left_2", "player_right_2",
)
ENEMY_SHOOTER_SPRITES = ("gunner", "bomber", "turret")


def sprite_role(name):
    if name in PLAYER_SPRITES:
        return "player"
    if name in ENEMY_SHOOTER_SPRITES:
        return "enemy_shooter"
    if name.startswith("boss_"):
        return "boss"
    return "other"


def helper_names_for_role(role):
    """Names of dummies a sprite of this role should have. Order matters —
    `A`/`D` (helpers mode) cycles through this list."""
    if role == "player":
        return ("barrel_center", "barrel_left", "barrel_right",
                "missile_left", "missile_right",
                "drone_left", "drone_right", "drone_top")
    if role == "enemy_shooter":
        return ("barrel",)
    if role == "boss":
        return ("barrel_center",)
    return ()


def default_dummies(role, trim_inset, trimmed_w, trimmed_h):
    """Seed dummy positions in UNTRIMMED-scaled coords from the current
    hard-coded fire offsets in pewpew.py. trim_inset is (left, top, right,
    bottom); (trimmed_w, trimmed_h) is the final sprite size that ships."""
    left, top, _right, _bottom = trim_inset
    cx_t = trimmed_w // 2
    cy_top = 2
    cy_mid = trimmed_h // 2
    cy_bot = max(0, trimmed_h - 1)
    ps = PLAY_SCALE   # float; positions are clamped to ints at the end

    def un(tx, ty):
        # Convert trimmed-PNG -> untrimmed-scaled by adding the trim inset.
        return [int(left + round(tx)), int(top + round(ty))]

    if role == "player":
        return {
            "barrel_center": un(cx_t,                  cy_top),
            "barrel_left":   un(cx_t - 9 * ps,         cy_top),
            "barrel_right":  un(cx_t + 9 * ps,         cy_top),
            "missile_left":  un(cx_t - 12 * ps,        cy_mid),
            "missile_right": un(cx_t + 12 * ps,        cy_mid),
            "drone_left":    un(cx_t - 16 * ps,        cy_mid - 2 * ps),
            "drone_right":   un(cx_t + 16 * ps,        cy_mid - 2 * ps),
            "drone_top":     un(cx_t,                  cy_mid - 8 * ps),
        }
    if role == "enemy_shooter":
        return {"barrel": un(cx_t, cy_bot)}
    if role == "boss":
        return {"barrel_center": un(cx_t, cy_bot)}
    return {}


def default_hitbox(trim_inset, trimmed_w, trimmed_h):
    """Hitbox covers the trimmed bbox — equivalent to the engine's current
    image.get_rect(). Stored in untrimmed-scaled coords."""
    left, top, _right, _bottom = trim_inset
    return [int(left), int(top), int(trimmed_w), int(trimmed_h)]


# Mock in-game scene compositions. Each item is (sprite_name, (cx, cy), opts).
SCENES = [
    {
        "title": "combat",
        "size": (330, 280),
        "backdrop": "bg_asteroid",
        "items": [
            ("scout",         (60, 50),  {}),
            ("gunner",        (160, 40), {}),
            ("kamikaze",      (260, 60), {}),
            ("weaver",        (110, 110),{}),
            ("turret",        (240, 130),{}),
            ("rock_11_2",     (40, 180), {}),
            ("rock_9_1",      (300, 200),{}),
            ("pickup_main",   (180, 150),{}),
            ("player",        (160, 240),{}),
            ("glyph_pulse",   (150, 210),{}),
            ("glyph_pulse",   (170, 200),{}),
            ("glyph_spread",  (140, 170),{}),
            ("glyph_spread",  (180, 175),{}),
            ("pellet_red",    (60, 100), {"flip_y": True}),
            ("pellet_purple", (240, 165),{"flip_y": True}),
            ("pellet_amber",  (160, 90), {"flip_y": True}),
        ],
    },
    {
        "title": "boss",
        "size": (330, 280),
        "backdrop": "bg_boss",
        "items": [
            ("boss_3",        (160, 70), {}),
            ("rock_14_0",     (40, 130), {}),
            ("rock_9_3",      (290, 145),{}),
            ("burst_small",   (115, 110),{}),
            ("sparkle_gold",  (210, 100),{}),
            ("player",        (160, 240),{}),
            ("glyph_vulcan",  (155, 215),{}),
            ("glyph_vulcan",  (165, 200),{}),
            ("glyph_tracker", (140, 195),{}),
            ("glyph_tracker", (180, 195),{}),
            ("pellet_red",    (130, 120),{"flip_y": True}),
            ("pellet_red",    (190, 120),{"flip_y": True}),
            ("pellet_purple", (100, 160),{"flip_y": True}),
            ("pellet_purple", (220, 160),{"flip_y": True}),
            ("pellet_amber",  (160, 145),{"flip_y": True}),
        ],
    },
    {
        "title": "hazards",
        "size": (330, 280),
        "backdrop": "bg_converge",
        "items": [
            ("wall_0",        (40, 80), {}),
            ("wall_3",        (40, 220),{}),
            ("wall_1",        (290, 80),{}),
            ("wall_5",        (290, 220),{}),
            ("mine",          (130, 70), {}),
            ("mine",          (200, 60), {}),
            ("crystal",       (160, 130),{}),
            ("pylon",         (110, 180),{}),
            ("pylon",         (215, 180),{}),
            ("pickup_shield", (160, 210),{}),
            ("pickup_bomb",   (200, 220),{}),
            ("player",        (160, 250),{}),
            ("glyph_drone",   (150, 225),{}),
            ("glyph_drone",   (170, 225),{}),
        ],
    },
    {
        "title": "dock",
        "size": (330, 280),
        "backdrop": "bg_outpost",
        "items": [
            ("station_2",     (160, 110),{"max_w": 280, "max_h": 140}),
            ("pickup_money",  (60, 220), {}),
            ("pickup_money",  (260, 220),{}),
            ("shield_ring",   (160, 215),{}),
            ("player",        (160, 240),{}),
        ],
    },
    {
        "title": "swarm",
        "size": (330, 280),
        "backdrop": "bg_start",
        "items": [
            ("scout",         (50, 30),  {}),
            ("scout",         (90, 45),  {}),
            ("scout",         (140, 25), {}),
            ("kamikaze",      (200, 35), {}),
            ("kamikaze",      (250, 50), {}),
            ("scout",         (290, 30), {}),
            ("scout",         (60, 85),  {}),
            ("kamikaze",      (130, 100),{}),
            ("kamikaze",      (180, 80), {}),
            ("scout",         (240, 95), {}),
            ("player",        (160, 240),{}),
            ("glyph_pulse",   (140, 200),{}),
            ("glyph_pulse",   (180, 200),{}),
            ("pellet_red",    (90, 130), {"flip_y": True}),
            ("pellet_red",    (220, 140),{"flip_y": True}),
            ("pellet_purple", (155, 120),{"flip_y": True}),
        ],
    },
    {
        "title": "asteroid field",
        "size": (330, 280),
        "backdrop": "bg_asteroid",
        "items": [
            ("rock_9_0",      (40, 30),  {}),
            ("rock_9_1",      (80, 60),  {}),
            ("rock_9_2",      (270, 35), {}),
            ("rock_9_3",      (290, 80), {}),
            ("rock_11_0",     (120, 100),{}),
            ("rock_11_1",     (200, 90), {}),
            ("rock_11_2",     (240, 150),{}),
            ("rock_11_3",     (60, 160), {}),
            ("rock_14_0",     (160, 170),{}),
            ("rock_14_1",     (280, 210),{}),
            ("rock_14_2",     (40, 220), {}),
            ("rock_14_3",     (220, 230),{}),
            ("player",        (160, 250),{}),
            ("glyph_vulcan",  (160, 220),{}),
        ],
    },
    {
        "title": "fx showcase",
        "size": (330, 280),
        "backdrop": "bg_boss",
        "items": [
            ("burst_small",   (55, 60),  {}),
            ("burst_large",   (160, 60), {}),
            ("shield_ring",   (260, 60), {}),
            ("sparkle_gold",  (55, 140), {}),
            ("shockwave",     (160, 140),{}),
            ("jet_droplet",   (260, 140),{}),
            ("player",        (160, 230),{}),
            ("shield_ring",   (160, 230),{}),
        ],
    },
    {
        "title": "bullet storm",
        "size": (330, 280),
        "backdrop": "bg_converge",
        "items": [
            ("player",        (160, 250),{}),
            ("glyph_pulse",   (140, 230),{}),
            ("glyph_pulse",   (180, 230),{}),
            ("glyph_spread",  (100, 210),{}),
            ("glyph_spread",  (220, 210),{}),
            ("glyph_vulcan",  (160, 200),{}),
            ("glyph_vulcan",  (155, 180),{}),
            ("glyph_vulcan",  (165, 180),{}),
            ("glyph_drone",   (130, 170),{}),
            ("glyph_drone",   (190, 170),{}),
            ("glyph_tracker", (160, 150),{}),
            ("pellet_red",    (60, 80),  {"flip_y": True}),
            ("pellet_red",    (130, 90), {"flip_y": True}),
            ("pellet_red",    (190, 90), {"flip_y": True}),
            ("pellet_red",    (270, 80), {"flip_y": True}),
            ("pellet_purple", (90, 130), {"flip_y": True}),
            ("pellet_purple", (160, 130),{"flip_y": True}),
            ("pellet_purple", (230, 130),{"flip_y": True}),
            ("pellet_amber",  (60, 60),  {"flip_y": True}),
            ("pellet_amber",  (160, 50), {"flip_y": True}),
            ("pellet_amber",  (260, 60), {"flip_y": True}),
        ],
    },
    {
        "title": "pickup rain",
        "size": (330, 280),
        "backdrop": "bg_outpost",
        "items": [
            ("pickup_main",   (60, 50),  {}),
            ("pickup_side",   (130, 70), {}),
            ("pickup_shield", (200, 50), {}),
            ("pickup_bomb",   (270, 70), {}),
            ("pickup_money",  (60, 140), {}),
            ("pickup_main",   (130, 160),{}),
            ("pickup_side",   (200, 140),{}),
            ("pickup_money",  (270, 160),{}),
            ("player",        (160, 240),{}),
            ("sparkle_gold",  (160, 240),{}),
        ],
    },
    {
        "title": "enemy roster",
        "size": (330, 280),
        "backdrop": "bg_start",
        "items": [
            ("scout",         (40, 60),  {}),
            ("gunner",        (110, 60), {}),
            ("weaver",        (180, 60), {}),
            ("kamikaze",      (250, 60), {}),
            ("turret",        (40, 150), {}),
            ("bomber",        (130, 150),{}),
            ("boss_0",        (240, 150),{}),
            ("player",        (160, 250),{}),
        ],
    },
    {
        "title": "wall corridor",
        "size": (330, 280),
        "backdrop": "bg_converge",
        "items": [
            ("wall_2",        (40, 60),  {}),
            ("wall_4",        (40, 180), {}),
            ("wall_6",        (290, 60), {}),
            ("wall_8",        (290, 180),{}),
            ("mine",          (120, 80), {}),
            ("crystal",       (160, 130),{}),
            ("mine",          (200, 70), {}),
            ("pylon",         (110, 200),{}),
            ("pylon",         (210, 200),{}),
            ("player",        (160, 250),{}),
            ("glyph_drone",   (150, 225),{}),
            ("glyph_drone",   (170, 225),{}),
        ],
    },
    {
        "title": "boss intro",
        "size": (330, 280),
        "backdrop": "bg_boss",
        "items": [
            ("boss_5",        (160, 70), {}),
            ("sparkle_gold",  (100, 60), {}),
            ("sparkle_gold",  (220, 60), {}),
            ("burst_large",   (160, 100),{}),
            ("rock_14_2",     (50, 170), {}),
            ("rock_14_3",     (280, 170),{}),
            ("player",        (160, 240),{}),
            ("shield_ring",   (160, 240),{}),
        ],
    },
]


def trim_dark_border(surf, threshold=4):
    left, top, right, bottom = find_trim_inset(surf, threshold)
    w, h = surf.get_size()
    rect = pygame.Rect(left, top, w - left - right, h - top - bottom)
    if rect.w <= 0 or rect.h <= 0:
        return surf
    return surf.subsurface(rect).copy()


def find_clip_warnings(sheet_img, rect, brightness_threshold=30):
    """Per-side bool: True iff the SOURCE SHEET has any non-black pixel in
    the single row/column immediately exterior to the user's rect on that
    side. Catches rect-framing mistakes: if the sprite extends past the
    rect on some side, the row/col just beyond the rect contains sprite
    pixels and we light that side's bracket red.

    Note: this is intentionally NOT about trim. The user wires this signal
    onto the same brackets that visualise the trim line, so the brackets
    serve double duty — bracket position = where trim cuts, bracket colour
    = whether the rect crops the sprite. Threshold 30 (sum of RGB) ignores
    PNG noise around fully-black backgrounds; any visibly-grey pixel
    (R+G+B > 30) counts as sprite content."""
    none = {"top": False, "bottom": False, "left": False, "right": False}
    if not rect:
        return none
    sheet_w, sheet_h = sheet_img.get_size()
    x, y, w, h = rect
    warn = dict(none)

    def any_bright(coords):
        for cx, cy in coords:
            r, g, b, _ = sheet_img.get_at((cx, cy))
            if r + g + b > brightness_threshold:
                return True
        return False

    if y > 0:
        ty = y - 1
        cols = range(max(0, x), min(sheet_w, x + w))
        warn["top"] = any_bright((cx, ty) for cx in cols)
    if y + h < sheet_h:
        by = y + h
        cols = range(max(0, x), min(sheet_w, x + w))
        warn["bottom"] = any_bright((cx, by) for cx in cols)
    if x > 0:
        lx = x - 1
        rows = range(max(0, y), min(sheet_h, y + h))
        warn["left"] = any_bright((lx, cy) for cy in rows)
    if x + w < sheet_w:
        rx = x + w
        rows = range(max(0, y), min(sheet_h, y + h))
        warn["right"] = any_bright((rx, cy) for cy in rows)
    return warn


def find_trim_inset(surf, threshold=4):
    """Return (left, top, right, bottom) inset in pixels — how many rows /
    cols of dark border trim_dark_border would strip from each edge. The
    panel preview uses this to draw crop-marks; save() uses it to translate
    the pivot from untrimmed-scaled coords to trimmed-PNG coords."""
    w, h = surf.get_size()
    def row_lum(y):
        s = 0; n = 0
        for x in range(0, w, max(1, w // 64)):
            r, g, b, _ = surf.get_at((x, y))
            s += r + g + b; n += 1
        return s / (n * 3)
    def col_lum(x):
        s = 0; n = 0
        for y in range(0, h, max(1, h // 64)):
            r, g, b, _ = surf.get_at((x, y))
            s += r + g + b; n += 1
        return s / (n * 3)
    y0 = 0
    while y0 < h - 1 and row_lum(y0) < threshold: y0 += 1
    y1 = h - 1
    while y1 > y0 and row_lum(y1) < threshold: y1 -= 1
    x0 = 0
    while x0 < w - 1 and col_lum(x0) < threshold: x0 += 1
    x1 = w - 1
    while x1 > x0 and col_lum(x1) < threshold: x1 -= 1
    return x0, y0, (w - 1) - x1, (h - 1) - y1


def make_exterior_transparent(surf, threshold=12):
    """Flood-fill from the four edges and set alpha=0 on any pixel with
    brightness (R+G+B) <= threshold that is reachable from an edge without
    crossing a brighter pixel. Interior dark pixels (enclosed by visible
    sprite content) keep their original alpha — so a sprite with a dark
    cockpit window or shadow stays opaque inside while the AI's surrounding
    black background goes away.

    Threshold is tight (≤12) because we run this at SOURCE pixel resolution
    BEFORE smoothscale — the AI's background is pure-black there, with no
    anti-alias bleed to worry about. The subsequent smoothscale interpolates
    the alpha along the new boundary, producing a soft anti-aliased edge
    instead of a brightness-30 halo around the sprite."""
    w, h = surf.get_size()
    surf = surf.convert_alpha()
    visited = bytearray(w * h)
    stack = []

    def consider(x, y):
        if x < 0 or x >= w or y < 0 or y >= h:
            return
        idx = y * w + x
        if visited[idx]:
            return
        visited[idx] = 1
        r, g, b, _ = surf.get_at((x, y))
        if r + g + b <= threshold:
            stack.append((x, y))

    for x in range(w):
        consider(x, 0); consider(x, h - 1)
    for y in range(h):
        consider(0, y); consider(w - 1, y)
    while stack:
        x, y = stack.pop()
        surf.set_at((x, y), (0, 0, 0, 0))
        consider(x + 1, y); consider(x - 1, y)
        consider(x, y + 1); consider(x, y - 1)
    return surf


def scaled_surface(surf, scale):
    if scale == 1.0:
        return surf
    sw, sh = surf.get_size()
    nw = max(1, int(round(sw * scale)))
    nh = max(1, int(round(sh * scale)))
    if (nw, nh) == (sw, sh):
        return surf
    return pygame.transform.smoothscale(surf, (nw, nh))


class HeldKey:
    """Tracks one held input (key, button, or hat direction) so we can fire
    its action on press and then auto-repeat while held."""
    __slots__ = ("action", "next_fire_ms")

    def __init__(self, action, now_ms):
        self.action = action
        self.next_fire_ms = now_ms + INITIAL_REPEAT_MS


class Editor:
    def __init__(self):
        # Import here so a real display is already up — _slice_art's
        # _ensure_pygame() is then a no-op.
        import _slice_art
        self._slicer = _slice_art

        self.sheets = []
        self.images = {}
        self.names = {}
        for fname, cfg in self._slicer.SHEETS.items():
            path = ART / fname
            if not path.exists():
                print(f"skip {fname}: missing")
                continue
            try:
                img = pygame.image.load(str(path)).convert_alpha()
            except Exception as e:
                print(f"failed to load {fname}: {e}")
                continue
            self.sheets.append(fname)
            self.images[fname] = img
            self.names[fname] = [n for n in cfg["names"] if not n.startswith("_")]
        if not self.sheets:
            raise SystemExit("no sheets loaded from art/")
        self.owner = {n: f for f, ns in self.names.items() for n in ns}

        self.manifest = self._load_or_seed_manifest()

        self.sheet_idx = 0
        self.active_idx = 0
        self.scene_page = 0           # paged through with PageUp/Down + L3/R3
        self.drag_start = None
        self.drag_end = None
        self.dirty = False
        self.quit_armed = False
        self.flash_t = 0
        self.flash_msg = ""
        self.list_rects = []
        self._trimmed_cache = {}      # for scenes + save
        self._untrimmed_cache = {}    # for the panel preview (no trim)
        # Editing-mode state. SELECT / `;` cycles through EDIT_MODES; per-
        # sprite `active_helper` remembers which dummy you were editing.
        self.mode = "rect"
        self._active_helper_by_sprite = {}   # sprite_name -> helper_name

        self.held = {}

        self.gamepad = None
        self.modifier_l2 = False
        self.modifier_r2 = False    # also doubles as pivot modifier w/ D-pad
        pygame.joystick.init()
        if pygame.joystick.get_count() > 0:
            self.gamepad = pygame.joystick.Joystick(0)
            self.gamepad.init()
            print(f"gamepad: {self.gamepad.get_name()}")

    # ----- manifest seeding -------------------------------------------------
    def _load_or_seed_manifest(self):
        if MANIFEST_PATH.exists():
            try:
                raw = json.loads(MANIFEST_PATH.read_text())
            except Exception as e:
                print(f"manifest load failed: {e}; starting fresh")
                raw = {}
        else:
            raw = {}

        needs_seed = False
        for fname in self.sheets:
            existing = raw.get(fname, {})
            for name in self.names[fname]:
                if existing.get(name, {}).get("rect") is None:
                    needs_seed = True
                    break
            if needs_seed:
                break
        seed_cells = {}
        if needs_seed:
            print("seeding from _slice_art.compute_cells...")
            try:
                seed_cells = self._slicer.dump_all_cells_json()
            except Exception as e:
                print(f"  seed failed: {e}")

        manifest = {}
        for fname in self.sheets:
            existing = raw.get(fname, {})
            seed = seed_cells.get(fname, {})
            entry_map = {}
            for name in self.names[fname]:
                entry = existing.get(name, {})
                rect = entry.get("rect") or seed.get(name)
                scale = entry.get("scale")
                if scale is None:
                    scale = self._compute_seed_scale(fname, rect, name)
                pivot = entry.get("pivot")  # may be None (= default centre)
                hitbox = entry.get("hitbox")
                dummies = dict(entry.get("dummies") or {})
                # Seed any missing hitbox / dummies from defaults derived
                # from the trimmed bbox + sprite role.
                if (hitbox is None or not dummies) and rect:
                    trim_inset, trimmed_size = self._trim_for_rect(
                        fname, rect, float(scale))
                    if trim_inset and trimmed_size:
                        tw, th = trimmed_size
                        if hitbox is None:
                            hitbox = default_hitbox(trim_inset, tw, th)
                        role = sprite_role(name)
                        defaults = default_dummies(role, trim_inset, tw, th)
                        for hname, pos in defaults.items():
                            dummies.setdefault(hname, pos)
                entry_map[name] = {
                    "rect": list(rect) if rect else None,
                    "scale": float(scale),
                    "pivot": list(pivot) if pivot else None,
                    "hitbox": list(hitbox) if hitbox else None,
                    "dummies": dummies,
                }
            manifest[fname] = entry_map
        return manifest

    def _trim_for_rect(self, fname, rect, scale):
        """Return (trim_inset, (trimmed_w, trimmed_h)) by running the same
        pipeline as get_sprite_surface does, without caching. Used while
        seeding new hitbox/dummy defaults so positions land where the engine
        would compute them today."""
        img = self.images.get(fname)
        if img is None or not rect:
            return None, None
        clip = pygame.Rect(*rect).clip(img.get_rect())
        if clip.w <= 0 or clip.h <= 0:
            return None, None
        try:
            sub = img.subsurface(clip).copy()
            scaled = scaled_surface(sub, float(scale))
            inset = find_trim_inset(scaled)
            sw, sh = scaled.get_size()
            left, top, right, bottom = inset
            tw = max(1, sw - left - right)
            th = max(1, sh - top - bottom)
            return inset, (tw, th)
        except Exception:
            return None, None

    def _compute_seed_scale(self, fname, rect, name):
        if not rect:
            return 1.0
        png_path = SPRITES / f"{name}.png"
        if png_path.exists():
            try:
                existing = pygame.image.load(str(png_path))
                eh = existing.get_height()
                img = self.images.get(fname)
                if img is not None:
                    clip = pygame.Rect(*rect).clip(img.get_rect())
                    if clip.w > 0 and clip.h > 0:
                        sub = img.subsurface(clip).copy()
                        trimmed = trim_dark_border(sub)
                        th = trimmed.get_height()
                        if th > 0:
                            return round(eh / th, 4)
            except Exception:
                pass
        # No saved sprite yet — fall back to the sheet's default_scale hint
        # if it has one (e.g. title.png ships its huge 1536x1024 source at
        # ~0.2× by default). Otherwise stay at 1.0.
        ds = self._slicer.SHEETS.get(fname, {}).get("default_scale")
        if ds is not None:
            return float(ds)
        return 1.0

    # ----- properties / layout ---------------------------------------------
    @property
    def current_sheet(self):
        return self.sheets[self.sheet_idx]

    @property
    def current_sprite(self):
        return self.names[self.current_sheet][self.active_idx]

    def panel_w(self):
        # Fixed: the panel only hosts the sprite list + active info text
        # now. The big untrimmed preview lives in target_preview_rect.
        return PANEL_W

    def _editor_left_area(self):
        """Combined width for sheet + target preview (everything to the left
        of the right-hand info panel)."""
        pw = self.panel_w()
        # outer margin, sheet area, margin, target area, margin, panel, margin
        total_w = WIN_W - pw - MARGIN * 3
        return MARGIN, total_w

    def sheet_view_rect(self):
        x0, total_w = self._editor_left_area()
        sheet_w = (total_w - MARGIN) * 2 // 3
        return pygame.Rect(x0, TOPBAR_H + MARGIN,
                           sheet_w, EDITOR_H - MARGIN * 2)

    def target_preview_rect(self):
        x0, total_w = self._editor_left_area()
        sheet_w = (total_w - MARGIN) * 2 // 3
        target_w = total_w - sheet_w - MARGIN
        return pygame.Rect(x0 + sheet_w + MARGIN, TOPBAR_H + MARGIN,
                           target_w, EDITOR_H - MARGIN * 2)

    def panel_rect(self):
        pw = self.panel_w()
        return pygame.Rect(
            WIN_W - pw - MARGIN, TOPBAR_H + MARGIN,
            pw, EDITOR_H - MARGIN * 2)

    def scene_strip_rect(self):
        return pygame.Rect(
            MARGIN, TOPBAR_H + EDITOR_H,
            WIN_W - MARGIN * 2, SCENE_STRIP_H - MARGIN)

    def fit_scale(self):
        img = self.images[self.current_sheet]
        sw, sh = img.get_size()
        area = self.sheet_view_rect()
        return min(area.w / sw, area.h / sh)

    def sheet_origin(self):
        img = self.images[self.current_sheet]
        sw, sh = img.get_size()
        area = self.sheet_view_rect()
        scale = self.fit_scale()
        dw, dh = int(sw * scale), int(sh * scale)
        return area.x + (area.w - dw) // 2, area.y + (area.h - dh) // 2

    def source_to_screen(self, sx, sy):
        ox, oy = self.sheet_origin()
        scale = self.fit_scale()
        return ox + sx * scale, oy + sy * scale

    def screen_to_source(self, mx, my):
        ox, oy = self.sheet_origin()
        scale = self.fit_scale()
        if scale <= 0:
            return None
        img = self.images[self.current_sheet]
        sx = (mx - ox) / scale
        sy = (my - oy) / scale
        if sx < 0 or sy < 0 or sx >= img.get_width() or sy >= img.get_height():
            return None
        return int(round(sx)), int(round(sy))

    # ----- input pipeline --------------------------------------------------
    def current_stride(self):
        if pygame.key.get_mods() & pygame.KMOD_SHIFT:
            return 5
        if self.modifier_l2:
            return 5
        return 1

    def apply_action(self, action):
        stride = self.current_stride()
        if action == "pos_left":      self._nudge_rect_pos(-stride, 0)
        elif action == "pos_right":   self._nudge_rect_pos(stride, 0)
        elif action == "pos_up":      self._nudge_rect_pos(0, -stride)
        elif action == "pos_down":    self._nudge_rect_pos(0, stride)
        elif action == "size_w_dec":  self._nudge_rect_size(-stride, 0)
        elif action == "size_w_inc":  self._nudge_rect_size(stride, 0)
        elif action == "size_h_dec":  self._nudge_rect_size(0, -stride)
        elif action == "size_h_inc":  self._nudge_rect_size(0, stride)
        elif action == "scale_dec":   self._nudge_scale(-0.01 * stride)
        elif action == "scale_inc":   self._nudge_scale(0.01 * stride)
        elif action == "pivot_left":  self._nudge_pivot(-stride, 0)
        elif action == "pivot_right": self._nudge_pivot(stride, 0)
        elif action == "pivot_up":    self._nudge_pivot(0, -stride)
        elif action == "pivot_down":  self._nudge_pivot(0, stride)
        elif action == "hitbox_pos_left":  self._nudge_hitbox_pos(-stride, 0)
        elif action == "hitbox_pos_right": self._nudge_hitbox_pos(stride, 0)
        elif action == "hitbox_pos_up":    self._nudge_hitbox_pos(0, -stride)
        elif action == "hitbox_pos_down":  self._nudge_hitbox_pos(0, stride)
        elif action == "hitbox_size_w_dec": self._nudge_hitbox_size(-stride, 0)
        elif action == "hitbox_size_w_inc": self._nudge_hitbox_size(stride, 0)
        elif action == "hitbox_size_h_dec": self._nudge_hitbox_size(0, -stride)
        elif action == "hitbox_size_h_inc": self._nudge_hitbox_size(0, stride)
        elif action == "helper_left":   self._nudge_helper(-stride, 0)
        elif action == "helper_right":  self._nudge_helper(stride, 0)
        elif action == "helper_up":     self._nudge_helper(0, -stride)
        elif action == "helper_down":   self._nudge_helper(0, stride)
        elif action == "helper_prev":   self._cycle_helper(-1)
        elif action == "helper_next":   self._cycle_helper(1)
        elif action == "mode_cycle":    self._cycle_mode()
        elif action == "sheet_prev":  self.cycle_sheet(-1)
        elif action == "sheet_next":  self.cycle_sheet(1)
        elif action == "sprite_prev": self.cycle_sprite(-1)
        elif action == "sprite_next": self.cycle_sprite(1)
        elif action == "page_prev":   self.cycle_scene_page(-1)
        elif action == "page_next":   self.cycle_scene_page(1)

    def start_action(self, key, action):
        self.apply_action(action)
        self.held[key] = HeldKey(action, pygame.time.get_ticks())

    def stop_action(self, key):
        self.held.pop(key, None)

    def tick_held(self):
        now = pygame.time.get_ticks()
        for key, h in list(self.held.items()):
            fires = 0
            while h.next_fire_ms <= now and fires < 4:
                self.apply_action(h.action)
                h.next_fire_ms += REPEAT_INTERVAL_MS
                fires += 1
            if h.next_fire_ms + REPEAT_INTERVAL_MS < now:
                h.next_fire_ms = now + REPEAT_INTERVAL_MS

    # ----- state mutators --------------------------------------------------
    def _nudge_rect_pos(self, dx, dy):
        entry = self.manifest[self.current_sheet][self.current_sprite]
        rect = entry.get("rect")
        if not rect:
            return
        img = self.images[self.current_sheet]
        iw, ih = img.get_size()
        x, y, w, h = rect
        nx = max(0, min(iw - w, x + dx))
        ny = max(0, min(ih - h, y + dy))
        if (nx, ny) != (x, y):
            entry["rect"] = [nx, ny, w, h]
            self._touch()

    def _nudge_rect_size(self, dw, dh):
        entry = self.manifest[self.current_sheet][self.current_sprite]
        rect = entry.get("rect")
        if not rect:
            return
        img = self.images[self.current_sheet]
        iw, ih = img.get_size()
        x, y, w, h = rect
        nw = max(2, min(iw - x, w + dw))
        nh = max(2, min(ih - y, h + dh))
        if (nw, nh) != (w, h):
            entry["rect"] = [x, y, nw, nh]
            self._touch()

    def _nudge_scale(self, ds):
        entry = self.manifest[self.current_sheet][self.current_sprite]
        scale = float(entry.get("scale", 1.0))
        new_scale = max(0.02, min(8.0, round(scale + ds, 4)))
        if new_scale != scale:
            entry["scale"] = new_scale
            self._touch()

    def _nudge_pivot(self, dx, dy):
        entry = self.manifest[self.current_sheet][self.current_sprite]
        if entry.get("rect") is None:
            return
        sz = self.untrimmed_scaled_size(self.current_sheet, self.current_sprite)
        if sz is None:
            return
        sw, sh = sz
        cur = self.effective_pivot(self.current_sheet, self.current_sprite)
        if cur is None:
            return
        npx = max(0, min(sw - 1, cur[0] + dx))
        npy = max(0, min(sh - 1, cur[1] + dy))
        if [npx, npy] != list(cur):
            entry["pivot"] = [npx, npy]
            # Pivot doesn't change the sprite surface — only marker position.
            self.dirty = True
            self.quit_armed = False

    def _ensure_hitbox(self):
        """Return the current hitbox, seeding a default one if necessary so
        nudges from a fresh sprite still work."""
        entry = self.manifest[self.current_sheet][self.current_sprite]
        hb = entry.get("hitbox")
        sz = self.untrimmed_scaled_size(self.current_sheet, self.current_sprite)
        if sz is None or entry.get("rect") is None:
            return None, None
        sw, sh = sz
        if hb is None:
            inset = self.trim_inset_for(self.current_sprite) or (0, 0, 0, 0)
            left, top, right, bottom = inset
            tw = max(1, sw - left - right)
            th = max(1, sh - top - bottom)
            hb = default_hitbox(inset, tw, th)
            entry["hitbox"] = list(hb)
        return entry, hb

    def _nudge_hitbox_pos(self, dx, dy):
        entry, hb = self._ensure_hitbox()
        if entry is None:
            return
        sw, sh = self.untrimmed_scaled_size(self.current_sheet, self.current_sprite)
        x, y, w, h = hb
        nx = max(0, min(sw - w, x + dx))
        ny = max(0, min(sh - h, y + dy))
        if [nx, ny] != [x, y]:
            entry["hitbox"] = [nx, ny, w, h]
            self.dirty = True
            self.quit_armed = False

    def _nudge_hitbox_size(self, dw, dh):
        entry, hb = self._ensure_hitbox()
        if entry is None:
            return
        sw, sh = self.untrimmed_scaled_size(self.current_sheet, self.current_sprite)
        x, y, w, h = hb
        nw = max(1, min(sw - x, w + dw))
        nh = max(1, min(sh - y, h + dh))
        if [nw, nh] != [w, h]:
            entry["hitbox"] = [x, y, nw, nh]
            self.dirty = True
            self.quit_armed = False

    def _banking_derived_dummies(self, name):
        """Scale the straight player's dummy positions by the image-size
        ratio so banking variants get visually correct (read-only) helpers
        without being editable in the editor or written to engine.json."""
        player_dummies = (
            self.manifest.get("player_ship.png", {})
                         .get("player", {})
                         .get("dummies") or {}
        )
        if not player_dummies:
            return {}
        pun = self.get_untrimmed_surface("player")
        cun = self.get_untrimmed_surface(name)
        if pun is None or cun is None:
            return dict(player_dummies)
        pw, ph = pun.get_size()
        cw, ch = cun.get_size()
        if pw <= 0 or ph <= 0:
            return dict(player_dummies)
        return {
            hn: [pos[0] / pw * cw, pos[1] / ph * ch]
            for hn, pos in player_dummies.items()
            if pos
        }

    def _display_dummies(self, name):
        """Dummies to render on the target preview. Banking sprites pull
        derived positions from the straight player; everyone else reads
        their own manifest entry."""
        if name in BANKING_SPRITES:
            return self._banking_derived_dummies(name)
        fname = self.owner.get(name)
        if fname is None:
            return {}
        entry = self.manifest.get(fname, {}).get(name, {})
        return entry.get("dummies") or {}

    def _helper_names(self):
        """The ordered list of helper slots the current sprite supports."""
        return list(helper_names_for_role(sprite_role(self.current_sprite)))

    def _active_helper(self):
        names = self._helper_names()
        if not names:
            return None
        cur = self._active_helper_by_sprite.get(self.current_sprite)
        if cur not in names:
            cur = names[0]
            self._active_helper_by_sprite[self.current_sprite] = cur
        return cur

    def _ensure_helper_pos(self, helper_name):
        """Return current helper position, seeding if missing."""
        entry = self.manifest[self.current_sheet][self.current_sprite]
        dummies = entry.setdefault("dummies", {})
        pos = dummies.get(helper_name)
        if pos is not None:
            return entry, pos
        sz = self.untrimmed_scaled_size(self.current_sheet, self.current_sprite)
        if sz is None or entry.get("rect") is None:
            return None, None
        sw, sh = sz
        inset = self.trim_inset_for(self.current_sprite) or (0, 0, 0, 0)
        left, top, right, bottom = inset
        tw = max(1, sw - left - right)
        th = max(1, sh - top - bottom)
        defaults = default_dummies(
            sprite_role(self.current_sprite), inset, tw, th)
        pos = defaults.get(helper_name) or [sw // 2, sh // 2]
        dummies[helper_name] = list(pos)
        return entry, pos

    def _nudge_helper(self, dx, dy):
        helper = self._active_helper()
        if helper is None:
            return
        entry, pos = self._ensure_helper_pos(helper)
        if entry is None:
            return
        sw, sh = self.untrimmed_scaled_size(self.current_sheet, self.current_sprite)
        x, y = pos
        nx = max(0, min(sw - 1, x + dx))
        ny = max(0, min(sh - 1, y + dy))
        if [nx, ny] != [x, y]:
            entry["dummies"][helper] = [nx, ny]
            self.dirty = True
            self.quit_armed = False

    def _cycle_helper(self, delta):
        names = self._helper_names()
        if not names:
            return
        cur = self._active_helper()
        idx = names.index(cur) if cur in names else 0
        nxt = names[(idx + delta) % len(names)]
        self._active_helper_by_sprite[self.current_sprite] = nxt
        self.quit_armed = False

    def _cycle_mode(self):
        idx = EDIT_MODES.index(self.mode) if self.mode in EDIT_MODES else 0
        self.mode = EDIT_MODES[(idx + 1) % len(EDIT_MODES)]
        self.quit_armed = False

    def _touch(self):
        self.dirty = True
        self._trimmed_cache.pop(self.current_sprite, None)
        self._untrimmed_cache.pop(self.current_sprite, None)
        self.quit_armed = False

    def set_active_rect_from_drag(self, x, y, w, h):
        entry = self.manifest[self.current_sheet][self.current_sprite]
        entry["rect"] = [int(x), int(y), int(w), int(h)]
        # Drag implies the user is reframing the sprite — reset pivot so it
        # re-defaults to the new untrimmed-scaled centre.
        entry["pivot"] = None
        self._touch()

    def clear_active_rect(self):
        entry = self.manifest[self.current_sheet][self.current_sprite]
        if entry.get("rect") is not None or entry.get("pivot") is not None:
            entry["rect"] = None
            entry["pivot"] = None
            self._touch()

    def cycle_sprite(self, delta):
        names = self.names[self.current_sheet]
        new_idx = self.active_idx + delta
        if new_idx < 0:
            # Spill back into the previous sheet's last sprite
            self.sheet_idx = (self.sheet_idx - 1) % len(self.sheets)
            self.active_idx = len(self.names[self.current_sheet]) - 1
            self.drag_start = self.drag_end = None
        elif new_idx >= len(names):
            # Spill forward into the next sheet's first sprite
            self.sheet_idx = (self.sheet_idx + 1) % len(self.sheets)
            self.active_idx = 0
            self.drag_start = self.drag_end = None
        else:
            self.active_idx = new_idx
        self.quit_armed = False

    def cycle_sheet(self, delta):
        self.sheet_idx = (self.sheet_idx + delta) % len(self.sheets)
        self.active_idx = 0
        self.drag_start = self.drag_end = None
        self.quit_armed = False

    def scenes_per_page(self):
        if not SCENES:
            return 1
        strip = self.scene_strip_rect()
        scene_w = SCENES[0]["size"][0]
        gutter = 6
        return max(1, (strip.w - 20 + gutter) // (scene_w + gutter))

    def total_scene_pages(self):
        spp = self.scenes_per_page()
        return max(1, (len(SCENES) + spp - 1) // spp)

    def cycle_scene_page(self, delta):
        n = self.total_scene_pages()
        self.scene_page = (self.scene_page + delta) % n
        self.quit_armed = False

    def visible_scenes(self):
        spp = self.scenes_per_page()
        if self.scene_page >= self.total_scene_pages():
            self.scene_page = 0
        start = self.scene_page * spp
        return SCENES[start:start + spp]

    # ----- save / load -----------------------------------------------------
    def save(self):
        out = {}
        for fname, sprite_map in self.manifest.items():
            out[fname] = {}
            for name, entry in sprite_map.items():
                out[fname][name] = {
                    "rect": entry.get("rect"),
                    "scale": entry.get("scale", 1.0),
                    "pivot": entry.get("pivot"),
                    "hitbox": entry.get("hitbox"),
                    "dummies": entry.get("dummies") or {},
                }
        MANIFEST_PATH.write_text(json.dumps(out, indent=2))
        SPRITES.mkdir(parents=True, exist_ok=True)
        engine_out = {}
        written = 0
        for fname, sprite_map in self.manifest.items():
            for name, entry in sprite_map.items():
                surf = self.get_sprite_surface(name)  # trimmed + scaled
                if surf is None:
                    continue
                pygame.image.save(surf, str(SPRITES / f"{name}.png"))
                written += 1
                un = self._build_untrimmed_surface(name)
                if un is None:
                    continue
                trim_left, trim_top, _, _ = find_trim_inset(un)
                pw, ph = surf.get_size()

                def to_trimmed_xy(p):
                    return [
                        max(0, min(pw - 1, int(p[0]) - trim_left)),
                        max(0, min(ph - 1, int(p[1]) - trim_top)),
                    ]

                engine_entry = {}
                pivot = entry.get("pivot")
                if pivot is not None:
                    engine_entry["pivot"] = to_trimmed_xy(pivot)
                hitbox = entry.get("hitbox")
                if hitbox is not None:
                    hx, hy, hw, hh = hitbox
                    tx = int(hx) - trim_left
                    ty = int(hy) - trim_top
                    # Clamp to the PNG bounds — hitbox may extend past trim if
                    # the user dragged it out.
                    x0 = max(0, tx)
                    y0 = max(0, ty)
                    x1 = min(pw, tx + int(hw))
                    y1 = min(ph, ty + int(hh))
                    if x1 > x0 and y1 > y0:
                        engine_entry["hitbox"] = [x0, y0, x1 - x0, y1 - y0]
                dummies = entry.get("dummies") or {}
                # Banking player variants derive their helpers from the
                # straight `player` entry at engine runtime, so we don't
                # write their (auto-seeded) dummies into the engine file.
                if dummies and name not in BANKING_SPRITES:
                    engine_entry["dummies"] = {
                        h_name: to_trimmed_xy(pos)
                        for h_name, pos in dummies.items()
                        if pos is not None
                    }
                if engine_entry:
                    engine_out[name] = engine_entry
        ENGINE_DATA_PATH.write_text(json.dumps(engine_out, indent=2))
        self.dirty = False
        self.flash_t = 1800
        self.flash_msg = (f"saved manifest + {written} PNGs + "
                          f"{len(engine_out)} engine entries")

    def reload(self):
        self.manifest = self._load_or_seed_manifest()
        self._trimmed_cache.clear()
        self._untrimmed_cache.clear()
        self.dirty = False
        self.quit_armed = False
        self.flash_t = 1200
        self.flash_msg = "reloaded from disk"

    # ----- sprite generation -----------------------------------------------
    def _build_untrimmed_surface(self, name):
        fname = self.owner.get(name)
        if fname is None: return None
        entry = self.manifest[fname].get(name)
        if entry is None: return None
        rect = entry.get("rect")
        if not rect: return None
        img = self.images.get(fname)
        if img is None: return None
        clip = pygame.Rect(*rect).clip(img.get_rect())
        if clip.w <= 0 or clip.h <= 0: return None
        sub = img.subsurface(clip).copy()
        # Knock out the AI's pure-black background at SOURCE resolution so
        # the subsequent smoothscale interpolates alpha along the boundary
        # (clean anti-aliased silhouette) instead of leaving a dark halo.
        # Backdrops skip — their dark areas ARE the intended image.
        if not self._is_backdrop(name):
            sub = make_exterior_transparent(sub)
        return scaled_surface(sub, float(entry.get("scale", 1.0)))

    def get_untrimmed_surface(self, name):
        fname = self.owner.get(name)
        if fname is None: return None
        entry = self.manifest[fname].get(name)
        if entry is None: return None
        rect = entry.get("rect")
        scale = entry.get("scale", 1.0)
        key = (tuple(rect) if rect else None, round(float(scale), 4))
        cached = self._untrimmed_cache.get(name)
        if cached and cached[0] == key:
            return cached[1]
        surf = self._build_untrimmed_surface(name)
        if surf is not None:
            self._untrimmed_cache[name] = (key, surf)
        return surf

    def _is_backdrop(self, name):
        return self.owner.get(name) == "backdrops.png"

    def get_sprite_surface(self, name):
        """Trimmed + scaled + exterior-transparent — what ships in the PNG
        and what scenes use. Backdrops skip the transparency step because
        their dark areas ARE the intended background."""
        fname = self.owner.get(name)
        if fname is None: return None
        entry = self.manifest[fname].get(name)
        if entry is None: return None
        rect = entry.get("rect")
        scale = entry.get("scale", 1.0)
        key = (tuple(rect) if rect else None, round(float(scale), 4))
        cached = self._trimmed_cache.get(name)
        if cached and cached[0] == key:
            return cached[1]
        if rect:
            untrimmed = self._build_untrimmed_surface(name)
            if untrimmed is None: return None
            surf = trim_dark_border(untrimmed)
        else:
            png_path = SPRITES / f"{name}.png"
            if not png_path.exists(): return None
            try:
                surf = pygame.image.load(str(png_path)).convert_alpha()
            except Exception:
                return None
        self._trimmed_cache[name] = (key, surf)
        return surf

    # ----- pivot helpers ---------------------------------------------------
    def untrimmed_scaled_size(self, fname, name):
        un = self.get_untrimmed_surface(name)
        if un is None:
            return None
        return un.get_size()

    def effective_pivot(self, fname, name):
        """Returns the pivot to render — either the user's stored pivot
        (clamped to current bounds) or the default centre."""
        sz = self.untrimmed_scaled_size(fname, name)
        if sz is None:
            return None
        sw, sh = sz
        entry = self.manifest[fname][name]
        pivot = entry.get("pivot")
        if pivot is None:
            return (sw // 2, sh // 2)
        px = max(0, min(sw - 1, int(pivot[0])))
        py = max(0, min(sh - 1, int(pivot[1])))
        return (px, py)

    def trim_inset_for(self, name):
        un = self.get_untrimmed_surface(name)
        if un is None:
            return None
        return find_trim_inset(un)

    # ----- quit handling ---------------------------------------------------
    def request_quit(self):
        if self.dirty and not self.quit_armed:
            self.quit_armed = True
            self.flash_t = 6000
            self.flash_msg = ("UNSAVED — press Esc again to discard, "
                              "or End to save and quit")
            return False
        return True


# ----- drawing -------------------------------------------------------------
def draw_topbar(screen, ed, font, font_small):
    bar = pygame.Rect(0, 0, WIN_W, TOPBAR_H)
    pygame.draw.rect(screen, PANEL_BG, bar)
    pygame.draw.line(screen, BORDER, (0, TOPBAR_H - 1), (WIN_W, TOPBAR_H - 1))
    left = font.render(
        f"Sheet {ed.sheet_idx + 1}/{len(ed.sheets)}   {ed.current_sheet}   "
        f"({len(ed.names[ed.current_sheet])} sprites)",
        True, INK)
    screen.blit(left, (MARGIN, (TOPBAR_H - left.get_height()) // 2))
    pad_state = ""
    if ed.gamepad:
        mods = []
        if ed.modifier_l2: mods.append("L2")
        if ed.modifier_r2: mods.append("R2")
        pad_state = f"  pad+ {' '.join(mods) if mods else '·'}"
    hint = ("; ' sheet  [ ] sprite  arrows pos  WASD size  "
            "Ctrl+A/D scale  Ctrl+arrows pivot  Shift=5x  End save"
            + pad_state)
    h_surf = font_small.render(hint, True, DIM)
    screen.blit(h_surf, (WIN_W - h_surf.get_width() - MARGIN,
                         (TOPBAR_H - h_surf.get_height()) // 2))


def draw_sheet(screen, ed, font_small):
    area = ed.sheet_view_rect()
    pygame.draw.rect(screen, PREVIEW_BG, area)
    pygame.draw.rect(screen, BORDER, area, 1)
    img = ed.images[ed.current_sheet]
    scale = ed.fit_scale()
    ox, oy = ed.sheet_origin()
    dw = max(1, int(img.get_width() * scale))
    dh = max(1, int(img.get_height() * scale))
    scaled = pygame.transform.smoothscale(img, (dw, dh))
    screen.blit(scaled, (ox, oy))
    sprite_map = ed.manifest[ed.current_sheet]
    active_name = ed.current_sprite
    for name, entry in sprite_map.items():
        rect = entry.get("rect")
        if not rect:
            continue
        sx, sy, sw, sh = rect
        x1, y1 = ed.source_to_screen(sx, sy)
        x2, y2 = ed.source_to_screen(sx + sw, sy + sh)
        r = pygame.Rect(int(x1), int(y1), int(x2 - x1), int(y2 - y1))
        if name == active_name:
            pygame.draw.rect(screen, ACTIVE_OUTLINE, r, 2)
            label = font_small.render(name, True, ACTIVE_OUTLINE)
            screen.blit(label, (r.x + 2, max(area.y, r.y - label.get_height() - 1)))
        else:
            pygame.draw.rect(screen, OTHER_OUTLINE, r, 1)
    if ed.drag_start and ed.drag_end:
        x1, y1 = ed.source_to_screen(*ed.drag_start)
        x2, y2 = ed.source_to_screen(*ed.drag_end)
        rx, ry = int(min(x1, x2)), int(min(y1, y2))
        rw, rh = int(abs(x2 - x1)), int(abs(y2 - y1))
        pygame.draw.rect(screen, DRAG_OUTLINE, (rx, ry, rw, rh), 1)


def _final_size(entry):
    rect = entry.get("rect")
    if not rect:
        return None
    scale = float(entry.get("scale", 1.0))
    _, _, w, h = rect
    return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def _draw_trim_marks(screen, img_rect, trim_inset, display_zoom, warnings=None):
    """Crop marks on the four edges of the panel area, outside img_rect, at
    the positions where trim_dark_border would cut. Each side is rendered red
    instead of amber when `warnings[side]` is True — meaning the trim would
    discard non-transparent pixels on that side."""
    warnings = warnings or {}
    left, top, right, bottom = trim_inset
    zx, zy = display_zoom
    x = img_rect.x
    y = img_rect.y
    w = img_rect.w
    h = img_rect.h

    trim_left_x   = x + int(round(left * zx))
    trim_right_x  = x + w - int(round(right * zx))
    trim_top_y    = y + int(round(top * zy))
    trim_bot_y    = y + h - int(round(bottom * zy))

    def color(side):
        return TRIM_MARK_BAD if warnings.get(side) else TRIM_MARK_COLOR

    GAP = 3
    LEN = 6

    ty = y - GAP
    c = color("top")
    pygame.draw.line(screen, c, (trim_left_x, ty), (trim_left_x, ty - LEN), 1)
    pygame.draw.line(screen, c, (trim_right_x, ty), (trim_right_x, ty - LEN), 1)
    if trim_right_x > trim_left_x:
        pygame.draw.line(screen, c, (trim_left_x, ty), (trim_right_x, ty), 1)
    by = y + h + GAP
    c = color("bottom")
    pygame.draw.line(screen, c, (trim_left_x, by), (trim_left_x, by + LEN), 1)
    pygame.draw.line(screen, c, (trim_right_x, by), (trim_right_x, by + LEN), 1)
    if trim_right_x > trim_left_x:
        pygame.draw.line(screen, c, (trim_left_x, by), (trim_right_x, by), 1)
    lx = x - GAP
    c = color("left")
    pygame.draw.line(screen, c, (lx, trim_top_y), (lx - LEN, trim_top_y), 1)
    pygame.draw.line(screen, c, (lx, trim_bot_y), (lx - LEN, trim_bot_y), 1)
    if trim_bot_y > trim_top_y:
        pygame.draw.line(screen, c, (lx, trim_top_y), (lx, trim_bot_y), 1)
    rx = x + w + GAP
    c = color("right")
    pygame.draw.line(screen, c, (rx, trim_top_y), (rx + LEN, trim_top_y), 1)
    pygame.draw.line(screen, c, (rx, trim_bot_y), (rx + LEN, trim_bot_y), 1)
    if trim_bot_y > trim_top_y:
        pygame.draw.line(screen, c, (rx, trim_top_y), (rx, trim_bot_y), 1)


def _draw_pivot(screen, img_rect, pivot, display_zoom):
    """Crosshair at the pivot, blinking white/black so it stays visible over
    any background colour."""
    if pivot is None:
        return
    zx, zy = display_zoom
    cx = img_rect.x + int(round(pivot[0] * zx))
    cy = img_rect.y + int(round(pivot[1] * zy))
    blink = (pygame.time.get_ticks() // PIVOT_BLINK_MS) % 2
    color = (255, 255, 255) if blink else (0, 0, 0)
    pygame.draw.line(screen, color, (cx - 7, cy), (cx + 7, cy), 1)
    pygame.draw.line(screen, color, (cx, cy - 7), (cx, cy + 7), 1)
    pygame.draw.circle(screen, color, (cx, cy), 4, 1)


def _draw_pivot_dim(screen, img_rect, pivot, display_zoom):
    """Dimmed read-only pivot, shown in hitbox / helpers modes."""
    if pivot is None:
        return
    zx, zy = display_zoom
    cx = img_rect.x + int(round(pivot[0] * zx))
    cy = img_rect.y + int(round(pivot[1] * zy))
    color = (110, 50, 60)
    pygame.draw.line(screen, color, (cx - 5, cy), (cx + 5, cy), 1)
    pygame.draw.line(screen, color, (cx, cy - 5), (cx, cy + 5), 1)


def _draw_hitbox(screen, img_rect, hitbox, display_zoom, active=False):
    if not hitbox:
        return
    zx, zy = display_zoom
    x, y, w, h = hitbox
    rx = img_rect.x + int(round(x * zx))
    ry = img_rect.y + int(round(y * zy))
    rw = max(1, int(round(w * zx)))
    rh = max(1, int(round(h * zy)))
    color = HITBOX_COLOR_ACTIVE if active else HITBOX_COLOR_DIM
    pygame.draw.rect(screen, color, (rx, ry, rw, rh),
                     2 if active else 1)


def _draw_dummies(screen, img_rect, dummies, display_zoom, font, active_name=None):
    if not dummies:
        return
    zx, zy = display_zoom
    for name, pos in dummies.items():
        if not pos:
            continue
        cx = img_rect.x + int(round(pos[0] * zx))
        cy = img_rect.y + int(round(pos[1] * zy))
        is_active = (name == active_name)
        color = DUMMY_COLOR_ACTIVE if is_active else DUMMY_COLOR_DIM
        radius = 5 if is_active else 3
        pygame.draw.circle(screen, color, (cx, cy), radius)
        pygame.draw.circle(screen, (0, 0, 0), (cx, cy), radius, 1)
        if is_active:
            # Cross hair lines so the exact pixel is unambiguous.
            pygame.draw.line(screen, color, (cx - 9, cy), (cx + 9, cy), 1)
            pygame.draw.line(screen, color, (cx, cy - 9), (cx, cy + 9), 1)
            label = font.render(name, True, color)
            screen.blit(label, (cx + 8, cy - label.get_height() // 2))


def draw_panel(screen, ed, font, font_small, font_tiny):
    panel = ed.panel_rect()
    pygame.draw.rect(screen, PANEL_BG, panel)
    pygame.draw.rect(screen, BORDER, panel, 1)
    x = panel.x + 12
    y = panel.y + 8

    title = font.render("SPRITES", True, INK)
    screen.blit(title, (x, y))
    y += title.get_height() + 4

    sprite_map = ed.manifest[ed.current_sheet]
    names = ed.names[ed.current_sheet]
    list_rects = []
    row_h = 17
    for i, name in enumerate(names):
        row_y = y + i * row_h
        rrect = pygame.Rect(panel.x + 4, row_y - 1, panel.w - 8, row_h)
        if i == ed.active_idx:
            pygame.draw.rect(screen, LIST_HIGHLIGHT, rrect)
        entry = sprite_map[name]
        has_rect = entry.get("rect") is not None
        marker = "●" if has_rect else "○"
        color = INK if has_rect else DIM
        fs = _final_size(entry)
        scale_pct = int(round(float(entry.get("scale", 1.0)) * 100))
        sz_text = f"{fs[0]}x{fs[1]} @ {scale_pct}%" if fs else "--"
        text = f"{marker} {name}   {sz_text}"
        s = font_small.render(text, True, color)
        screen.blit(s, (x, row_y))
        list_rects.append((rrect, i))
    y += len(names) * row_h + 8
    pygame.draw.line(screen, BORDER, (panel.x + 8, y),
                     (panel.x + panel.w - 8, y))
    y += 6

    active = sprite_map[ed.current_sprite]
    title2 = font.render(f"ACTIVE: {ed.current_sprite}", True, ACCENT)
    screen.blit(title2, (x, y))
    y += title2.get_height() + 4
    rect = active.get("rect")
    if rect:
        rx, ry, rw, rh = rect
        rect_line = f"rect:  x={rx} y={ry}  w={rw} h={rh}"
    else:
        rect_line = "rect:  (drag on sheet to set)"
    s = font_small.render(rect_line, True, INK)
    screen.blit(s, (x, y))
    y += s.get_height() + 2
    scale_v = float(active.get("scale", 1.0))
    fs = _final_size(active)
    if fs:
        scale_line = (f"scale: {scale_v:.2f}  ({int(round(scale_v * 100))}%)"
                      f"  ->  {fs[0]}x{fs[1]} px")
    else:
        scale_line = f"scale: {scale_v:.2f}  ({int(round(scale_v * 100))}%)"
    s2 = font_small.render(scale_line, True, INK)
    screen.blit(s2, (x, y))
    y += s2.get_height() + 2
    pivot = ed.effective_pivot(ed.current_sheet, ed.current_sprite)
    if pivot is None:
        pivot_line = "pivot: --"
    else:
        is_default = active.get("pivot") is None
        pivot_line = (f"pivot: ({pivot[0]}, {pivot[1]})"
                      f"{'  (default)' if is_default else ''}")
    s3 = font_small.render(pivot_line, True, INK)
    screen.blit(s3, (x, y))
    y += s3.get_height() + 6

    ed.list_rects = list_rects


def draw_target_preview(screen, ed, font, font_small):
    """Right-hand third of the editor area shows the UNTRIMMED scaled crop
    of the active sprite, with crop-mark brackets around the perimeter
    indicating where trim_dark_border will cut, and the blinking pivot
    crosshair at the user's pivot position."""
    area = ed.target_preview_rect()
    pygame.draw.rect(screen, PANEL_BG, area)
    pygame.draw.rect(screen, BORDER, area, 1)
    title = font.render("TARGET  (untrimmed; brackets show trim)", True, INK)
    screen.blit(title, (area.x + 8, area.y + 6))

    untrimmed = ed.get_untrimmed_surface(ed.current_sprite)
    if untrimmed is None:
        warn = font_small.render("(no rect — drag on sheet)", True, DIM)
        screen.blit(warn, (area.x + 8, area.y + 30))
        return
    sw, sh = untrimmed.get_size()
    # Leave room for crop-mark brackets (gap + length on every side) and
    # the title at the top + size label at the bottom.
    bracket_pad = 12
    title_h = 28
    label_h = 22
    avail_w = area.w - bracket_pad * 2 - 16
    avail_h = area.h - title_h - label_h - bracket_pad * 2
    int_zoom = int(min(avail_w // max(1, sw), avail_h // max(1, sh)))
    if int_zoom >= 1:
        zoom_factor = float(int_zoom)
        displayed = pygame.transform.scale(untrimmed, (sw * int_zoom, sh * int_zoom))
    else:
        zoom_factor = max(0.05,
                          min(avail_w / max(1, sw), avail_h / max(1, sh)))
        dw = max(1, int(sw * zoom_factor))
        dh = max(1, int(sh * zoom_factor))
        displayed = pygame.transform.smoothscale(untrimmed, (dw, dh))

    img_x = area.x + (area.w - displayed.get_width()) // 2
    img_y = area.y + title_h + bracket_pad + \
        (avail_h - displayed.get_height()) // 2
    bg = pygame.Rect(img_x - 2, img_y - 2,
                     displayed.get_width() + 4, displayed.get_height() + 4)
    pygame.draw.rect(screen, PREVIEW_BG, bg)
    screen.blit(displayed, (img_x, img_y))
    img_rect = pygame.Rect(img_x, img_y,
                           displayed.get_width(), displayed.get_height())
    inset = find_trim_inset(untrimmed)
    sheet_img = ed.images[ed.current_sheet]
    entry = ed.manifest[ed.current_sheet][ed.current_sprite]
    src_rect = entry.get("rect")
    warnings = find_clip_warnings(sheet_img, src_rect)
    _draw_trim_marks(screen, img_rect, inset,
                     (zoom_factor, zoom_factor), warnings)
    pivot = ed.effective_pivot(ed.current_sheet, ed.current_sprite)
    # In rect mode the pivot blinks; in other modes it stays dim.
    if ed.mode == "rect":
        _draw_pivot(screen, img_rect, pivot, (zoom_factor, zoom_factor))
    else:
        _draw_pivot_dim(screen, img_rect, pivot, (zoom_factor, zoom_factor))

    _draw_hitbox(screen, img_rect, entry.get("hitbox"),
                 (zoom_factor, zoom_factor),
                 active=(ed.mode == "hitbox"))
    # Banking sprites display dummies derived from the straight player —
    # their _display_dummies returns scaled positions instead of manifest.
    _draw_dummies(screen, img_rect, ed._display_dummies(ed.current_sprite),
                  (zoom_factor, zoom_factor),
                  font_small,
                  active_name=ed._active_helper() if ed.mode == "helpers" else None)

    zoom_label = (f"{int(zoom_factor)}x" if zoom_factor >= 1
                  else f"{zoom_factor:.2f}x")
    left, top, right, bottom = inset
    tw, th = sw - left - right, sh - top - bottom
    mode_label = f"mode: {ed.mode}"
    if ed.mode == "helpers":
        helper = ed._active_helper()
        mode_label += f"  ({helper or 'no helpers'})"
    info = font_small.render(
        f"{mode_label}    {sw}x{sh}  zoom {zoom_label}  ->  final {tw}x{th}",
        True, INK)
    screen.blit(info, (area.x + 8, area.y + area.h - info.get_height() - 6))


def render_scene(ed, scene):
    sw, sh = scene["size"]
    surf = pygame.Surface((sw, sh)).convert()
    surf.fill(SCENE_BG)
    bdname = scene.get("backdrop")
    if bdname:
        bd = ed.get_sprite_surface(bdname)
        if bd is not None:
            scaled = pygame.transform.scale(bd, (sw, sh))
            scaled.set_alpha(180)
            surf.blit(scaled, (0, 0))
    for name, (cx, cy), opts in scene["items"]:
        spr = ed.get_sprite_surface(name)
        if spr is None:
            continue
        if opts.get("max_w") or opts.get("max_h"):
            mw = opts.get("max_w") or spr.get_width()
            mh = opts.get("max_h") or spr.get_height()
            s_w, s_h = spr.get_size()
            fit = min(mw / s_w, mh / s_h, 1.0)
            if fit < 1.0:
                spr = pygame.transform.smoothscale(
                    spr, (max(1, int(s_w * fit)), max(1, int(s_h * fit))))
        if opts.get("flip_y"):
            spr = pygame.transform.flip(spr, False, True)
        rect = spr.get_rect(center=(cx, cy))
        surf.blit(spr, rect)
    return surf


def draw_scenes(screen, ed, font, font_small):
    strip = ed.scene_strip_rect()
    pygame.draw.rect(screen, PANEL_BG, strip)
    pygame.draw.rect(screen, BORDER, strip, 1)
    visible = ed.visible_scenes()
    n_pages = ed.total_scene_pages()
    title = font_small.render(
        f"IN-GAME PREVIEW  (game pixels, 1:1)   "
        f"page {ed.scene_page + 1}/{n_pages}   PgUp/PgDn or L3/R3 to cycle",
        True, SCENE_LABEL)
    screen.blit(title, (strip.x + 8, strip.y + 4))
    inner_y = strip.y + 22
    inner_h = strip.h - 26
    gutter = 6
    n = len(visible)
    if n == 0:
        return
    total_w = sum(s["size"][0] for s in visible) + gutter * (n - 1)
    x = strip.x + (strip.w - total_w) // 2
    for scene in visible:
        sw, sh = scene["size"]
        scene_surf = render_scene(ed, scene)
        if sh > inner_h:
            ratio = inner_h / sh
            scene_surf = pygame.transform.scale(
                scene_surf, (int(sw * ratio), inner_h))
            sw, sh = scene_surf.get_size()
        screen.blit(scene_surf, (x, inner_y + (inner_h - sh) // 2))
        pygame.draw.rect(screen, BORDER, (x, inner_y, sw, sh), 1)
        lab = font_small.render(scene["title"], True, SCENE_LABEL)
        screen.blit(lab, (x + 4, inner_y + sh - lab.get_height() - 2))
        x += sw + gutter


def draw_status(screen, ed, font_small):
    if ed.quit_armed:
        msg = "UNSAVED — Esc again to discard, End to save+quit"
        col = QUIT_WARN_FG
        bg = pygame.Rect(0, WIN_H - 22, WIN_W, 22)
        pygame.draw.rect(screen, QUIT_WARN_BG, bg)
        s = font_small.render(msg, True, col)
        screen.blit(s, ((WIN_W - s.get_width()) // 2,
                        WIN_H - s.get_height() - 4))
        return
    if ed.flash_t > 0:
        msg = ed.flash_msg; col = SAVED
    elif ed.dirty:
        msg = "● unsaved (End to save)"; col = DIRTY
    else:
        msg = "✓ saved"; col = DIM
    s = font_small.render(msg, True, col)
    screen.blit(s, (WIN_W - s.get_width() - MARGIN,
                    WIN_H - s.get_height() - 2))


# ----- event handling ------------------------------------------------------
# Each mode owns its own (arrow-key -> action) and (WASD-key -> action)
# tables. Ctrl is only meaningful in rect mode where it switches arrows to
# pivot edits and A/D to scale.
KB_ARROWS = (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN)
KB_WASD   = (pygame.K_a,    pygame.K_d,     pygame.K_w,  pygame.K_s)

MODE_ARROW_ACTIONS = {
    "rect":    ("pos_left",        "pos_right",        "pos_up",        "pos_down"),
    "hitbox":  ("hitbox_pos_left", "hitbox_pos_right", "hitbox_pos_up", "hitbox_pos_down"),
    "helpers": ("helper_left",     "helper_right",     "helper_up",     "helper_down"),
}
MODE_WASD_ACTIONS = {
    "rect":    ("size_w_dec",        "size_w_inc",        "size_h_dec",        "size_h_inc"),
    "hitbox":  ("hitbox_size_w_dec", "hitbox_size_w_inc", "hitbox_size_h_dec", "hitbox_size_h_inc"),
    # In helpers mode A/D cycles which dummy is active; W/S no-op.
    "helpers": ("helper_prev",       "helper_next",       None,                None),
}
KB_PIVOT_ARROWS = ("pivot_left", "pivot_right", "pivot_up", "pivot_down")


def _arrow_action(mode, k, ctrl):
    if mode == "rect" and ctrl:
        return KB_PIVOT_ARROWS[KB_ARROWS.index(k)]
    return MODE_ARROW_ACTIONS[mode][KB_ARROWS.index(k)]


def _wasd_action(mode, k, ctrl):
    if mode == "rect" and ctrl:
        # Ctrl+A/D = scale ±. Ctrl+W/S reserved no-op.
        if k == pygame.K_a: return "scale_dec"
        if k == pygame.K_d: return "scale_inc"
        return None
    return MODE_WASD_ACTIONS[mode][KB_WASD.index(k)]


def handle_key_down(ed, evt):
    """Return False to request quit."""
    mods = pygame.key.get_mods()
    ctrl = bool(mods & pygame.KMOD_CTRL)
    k = evt.key

    if k == pygame.K_ESCAPE:
        if not ed.request_quit():
            return True
        return False

    if k == pygame.K_END:
        ed.save()
        if ed.quit_armed:
            return False
        return True

    # Any other key disarms the quit confirmation
    ed.quit_armed = False

    if k == pygame.K_BACKSPACE:
        ed.clear_active_rect(); return True
    if ctrl and k == pygame.K_r:
        ed.reload(); return True
    if not ctrl and k == pygame.K_r:
        ed.reload(); return True
    if k == pygame.K_PAGEUP:
        ed.start_action(("kb", k), "page_prev"); return True
    if k == pygame.K_PAGEDOWN:
        ed.start_action(("kb", k), "page_next"); return True
    # `;` cycles editing mode (formerly sheet_prev). `'` still cycles
    # sheets forward; cycle-around means no need for a back direction.
    if k == pygame.K_SEMICOLON:
        ed.start_action(("kb", k), "mode_cycle"); return True
    if k == pygame.K_QUOTE:
        ed.start_action(("kb", k), "sheet_next"); return True
    if k == pygame.K_LEFTBRACKET:
        ed.start_action(("kb", k), "sprite_prev"); return True
    if k == pygame.K_RIGHTBRACKET:
        ed.start_action(("kb", k), "sprite_next"); return True
    if k in KB_ARROWS:
        action = _arrow_action(ed.mode, k, ctrl)
        if action:
            ed.start_action(("kb", k), action)
        return True
    if k in KB_WASD:
        action = _wasd_action(ed.mode, k, ctrl)
        if action:
            ed.start_action(("kb", k), action)
        return True
    return True


def handle_key_up(ed, evt):
    ed.stop_action(("kb", evt.key))


def update_gamepad_modifiers(ed):
    if not ed.gamepad:
        return
    try:
        ed.modifier_l2 = ed.gamepad.get_axis(JA_LT) > TRIGGER_THRESHOLD
        ed.modifier_r2 = ed.gamepad.get_axis(JA_RT) > TRIGGER_THRESHOLD
    except Exception:
        ed.modifier_l2 = ed.modifier_r2 = False


def _gp_face_action(mode, btn, r2):
    """X/B/Y/A on the pad. In rect mode they edit rect size (with R2 toggle
    for scale); in hitbox mode they edit hitbox size; in helpers mode X/B
    cycle the active helper and Y/A no-op."""
    if mode == "rect" and r2:
        if btn == JB_X: return "scale_dec"
        if btn == JB_B: return "scale_inc"
        return None
    wasd = MODE_WASD_ACTIONS[mode]
    # X<->A (kb size_w_dec), B<->D (size_w_inc), Y<->W (size_h_dec), A<->S (size_h_inc)
    mapping = {JB_X: wasd[0], JB_B: wasd[1], JB_Y: wasd[2], JB_A: wasd[3]}
    return mapping.get(btn)


def handle_joy_button_down(ed, evt):
    btn = evt.button
    key = ("gp_btn", btn)
    r2 = ed.modifier_r2
    # R2 chord on SELECT/START = quit / save (mirrors the layout editor).
    # Don't clear quit_armed here — the chord on SELECT relies on it
    # arming first so the second press confirms-and-quits when dirty.
    if btn == JB_BACK and r2:
        if not ed.request_quit():
            return
        pygame.event.post(pygame.event.Event(pygame.QUIT))
        return
    if btn == JB_START and r2:
        ed.save()
        return

    ed.quit_armed = False
    if btn == JB_BACK:
        # SELECT cycles editing mode (formerly sheet_prev).
        ed.start_action(key, "mode_cycle")
    elif btn == JB_START:
        ed.start_action(key, "sheet_next")
    elif btn == JB_LB:
        ed.start_action(key, "sprite_prev")
    elif btn == JB_RB:
        ed.start_action(key, "sprite_next")
    elif btn in (JB_X, JB_B, JB_Y, JB_A):
        action = _gp_face_action(ed.mode, btn, r2)
        if action:
            ed.start_action(key, action)
    elif btn == JB_LSB:
        ed.start_action(key, "page_prev")
    elif btn == JB_RSB:
        ed.start_action(key, "page_next")


def handle_joy_button_up(ed, evt):
    ed.stop_action(("gp_btn", evt.button))


_ALL_DPAD_ACTIONS = (
    "pos_left", "pos_right", "pos_up", "pos_down",
    "pivot_left", "pivot_right", "pivot_up", "pivot_down",
    "hitbox_pos_left", "hitbox_pos_right", "hitbox_pos_up", "hitbox_pos_down",
    "helper_left", "helper_right", "helper_up", "helper_down",
)


def handle_joy_hat(ed, evt):
    hx, hy = evt.value
    ed.quit_armed = False
    # R2 still toggles into "secondary edit" while in rect mode (pivot).
    # In hitbox/helpers modes it has no D-pad alternative.
    use_pivot = ed.modifier_r2 and ed.mode == "rect"
    if use_pivot:
        actions = KB_PIVOT_ARROWS
    else:
        actions = MODE_ARROW_ACTIONS[ed.mode]
    desired = set()
    if hx < 0: desired.add(actions[0])
    if hx > 0: desired.add(actions[1])
    if hy > 0: desired.add(actions[2])
    if hy < 0: desired.add(actions[3])
    # Stop every previously-fired D-pad direction that isn't in `desired`
    # (regardless of mode it was started in) and start the new ones.
    for a in _ALL_DPAD_ACTIONS:
        key = ("gp_hat", a)
        if key in ed.held and a not in desired:
            ed.stop_action(key)
    for a in desired:
        key = ("gp_hat", a)
        if key not in ed.held:
            ed.start_action(key, a)


def main():
    pygame.init()
    # Borderless fullscreen sized to the actual display so the editor uses
    # every pixel — no taskbar clipping, no decorations stealing height.
    info = pygame.display.Info()
    global WIN_W, WIN_H, EDITOR_H, SCENE_STRIP_H
    WIN_W = info.current_w
    WIN_H = info.current_h
    SCENE_STRIP_H = max(200, min(SCENE_STRIP_H, int(WIN_H * 0.36)))
    EDITOR_H = WIN_H - TOPBAR_H - SCENE_STRIP_H - MARGIN
    screen = pygame.display.set_mode((WIN_W, WIN_H), pygame.NOFRAME)
    pygame.display.set_caption("Pewpew sprite editor")
    pygame.key.set_repeat()   # we handle repeat ourselves
    font = pygame.font.SysFont("consolas", 14, bold=True)
    font_small = pygame.font.SysFont("consolas", 13)
    font_tiny = pygame.font.SysFont("consolas", 11)
    clock = pygame.time.Clock()
    ed = Editor()

    running = True
    while running:
        dt = clock.tick(60)
        if ed.flash_t > 0:
            ed.flash_t = max(0, ed.flash_t - dt)
        update_gamepad_modifiers(ed)
        ed.tick_held()

        for evt in pygame.event.get():
            if evt.type == pygame.QUIT:
                if ed.request_quit():
                    running = False
            elif evt.type == pygame.KEYDOWN:
                if not handle_key_down(ed, evt):
                    running = False
            elif evt.type == pygame.KEYUP:
                handle_key_up(ed, evt)
            elif evt.type == pygame.JOYBUTTONDOWN:
                handle_joy_button_down(ed, evt)
            elif evt.type == pygame.JOYBUTTONUP:
                handle_joy_button_up(ed, evt)
            elif evt.type == pygame.JOYHATMOTION:
                handle_joy_hat(ed, evt)
            elif evt.type == pygame.MOUSEBUTTONDOWN and evt.button == 1:
                mx, my = evt.pos
                hit_list = False
                for rrect, idx in ed.list_rects:
                    if rrect.collidepoint(mx, my):
                        ed.active_idx = idx
                        ed.quit_armed = False
                        hit_list = True
                        break
                if not hit_list and ed.sheet_view_rect().collidepoint(mx, my):
                    p = ed.screen_to_source(mx, my)
                    if p:
                        ed.drag_start = p
                        ed.drag_end = p
                        ed.quit_armed = False
            elif evt.type == pygame.MOUSEMOTION:
                if ed.drag_start is not None:
                    p = ed.screen_to_source(*evt.pos)
                    if p:
                        ed.drag_end = p
            elif evt.type == pygame.MOUSEBUTTONUP and evt.button == 1:
                if ed.drag_start is not None and ed.drag_end is not None:
                    x1, y1 = ed.drag_start
                    x2, y2 = ed.drag_end
                    rx, ry = min(x1, x2), min(y1, y2)
                    rw, rh = abs(x2 - x1), abs(y2 - y1)
                    if rw >= 2 and rh >= 2:
                        ed.set_active_rect_from_drag(rx, ry, rw, rh)
                ed.drag_start = ed.drag_end = None

        screen.fill(BG)
        draw_topbar(screen, ed, font, font_small)
        draw_sheet(screen, ed, font_small)
        draw_target_preview(screen, ed, font, font_small)
        draw_panel(screen, ed, font, font_small, font_tiny)
        draw_scenes(screen, ed, font, font_small)
        draw_status(screen, ed, font_small)
        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    main()
