"""Layout editor for Pewpew screens.

Edit the per-screen overlay items (texts, rects, images) that ship in
art/layout.json. The game's TitleScreen / MapScreen / ShopScreen /
GameOverScreen / PlayState / hud_draw each call
pewpew.draw_layout_overlay(surf, "<screen>", fonts, assets) at the end of
their draw pass, so anything added here lands on top of the existing chrome
without touching the screen code.

Controls. Arrow keys / WASD are the workhorse keys; their meaning shifts
per edit mode. Two modes only: transform (spatial / structural) and
details (visual / content). Text editing is a SUB-STATE of details,
entered via Tab on text/menu items.

  Screen               ;  '                  /  START  (cycles forward)
  Item                 [  ]                  /  LB  RB
  Mode cycle           Tab                   /  SELECT     (transform ↔ details)
  Move active          arrows                /  D-pad      (transform)
  Resize active        A D W S               /  X B Y A    (transform)
                         text:  WASD = font ± (any direction)
                         rect:  A/D = w ± , W/S = h ±
                         image: WASD = scale ±
                         menu:  A/D = font , W/S = line_height
                         progress_bar: A/D = w , W/S = h
                         container: A/D = w ± , W/S = h ±
  Anchor cycle         arrows                /  D-pad      (details)
                         text/image:  L/R = h-anchor , U/D = v-anchor
                         menu:        L/R = align (U/D no-op)
  Visual nudge         A D W S               /  X B Y A    (details)
                         X/B: color cycle (sprite for image, bg for container)
                         Y/A: per-type secondary —
                              text/menu: font ±        image: scale ±
                              rect: outline ±          progress_bar: segments ±
                              container: layout cycle (free / stack_v / stack_h / grid)
  Edit text            Tab / SELECT (details, text/menu/container) → text-edit
                         sub-state. type to fill (keyboard); Tab/SEL cycles
                         subfield (menu decor templates); Enter exits; arrows
                         still cycle anchor. Container's title is the subfield.
  Grid container       R2 + X/B          cols − / +
                       R2 + Y/A          rows − / +
                       R2 + D-pad L/R    gap_x − / +
                       R2 + D-pad U/D    gap_y − / +
  Add                  N text  /  M rect  /  I image  /  B bar  /  C container
  Duplicate            P                     /  L3
  Delete               Delete / Backspace    /  R3       (resets built-in to defaults)
  Pick up (cut)        X                     /  R2 + LB  (detaches active item)
  Drop  (paste)        V                     /  R2 + RB  (or release R2 after pad pickup)
  While carrying (R2 held — works on leaves AND containers; a container
  picked up brings its whole subtree with it):
    D-pad L/R         — up / dive (navigate the hierarchy with the carry)
    D-pad U/D         — prev / next sibling
    X                 — discard carry (throw away)
    B                 — cancel (put the carry back at its origin)
    Y                 — wrap: new container appears here with the carry inside
    A                 — drop (MOVE) here; same as releasing R2
    L3                — drop a COPY here, keep carrying the original
  Color preset         1..8                  (menu: Shift+1..8 = sel_color)
  Big stride (5x)      Shift held            /  L2 held
  Save layout.json     End                   /  R2 + START
  Reload from disk     Ctrl+R
  Toggle gizmos                              /  R2 + R3   (hide selection boxes)
  Hierarchy nav                              /  right stick: L/R = up/dive
                                                              U/D = prev/next sibling
  Quit (warns unsaved) Esc                   /  R2 + SELECT

The play / hud screens cross-dim the inactive side so the editable area
stands out: editing play dims the HUD strip, editing HUD dims the
playfield. Gizmos can be toggled off (R2+R3) to preview clean.

A zoom band under the preview re-renders the active item's region at
the largest integer scale that fits the available height — useful for
working on small HUD elements without squinting at the 2× main preview.

Item schema (one object per item in layout.json["screens"][<name>]["items"]):
  text:  type, id, x, y, anchor, text, font (1..7), color [r,g,b],
         alpha (0..255), shadow (bool)
  rect:  type, id, x, y, w, h, color [r,g,b], alpha, outline (0 = filled)
  image: type, id, x, y, anchor, sprite (sprite name from art/sprites/),
         scale (float), alpha
"""
import copy
import json
import os
import sys
from pathlib import Path
import pygame

ROOT = Path(__file__).resolve().parent
ART = ROOT / "art"
SPRITES_DIR = ART / "sprites"
LAYOUT_PATH = ART / "layout.json"
SHOTS_DIR = ROOT / "screenshots"

# Logical game resolution. Items are saved in this coord space.
SCREEN_W, SCREEN_H = 640, 480

SCREENS = ("title", "map", "shop", "play", "hud", "gameover")
# Backdrop screenshot per screen. play.png covers hud + play; gameover gets
# the dock outro shot as the closest available match. Missing files just
# leave the backdrop blank (preview shows on dark canvas).
BACKDROP_FILE = {
    "title":    "title.png",
    "map":      "map.png",
    "shop":     "shop.png",
    "play":     "play.png",
    "hud":      "play.png",
    "gameover": "gameover.png",
}

# Named palette mirrors pewpew's runtime colors. Keep in sync with
# pewpew.WHITE / DIM / etc.
PALETTE = [
    ("WHITE",  (240, 240, 240)),
    ("DIM",    (140, 140, 160)),
    ("CYAN",   (80, 220, 255)),
    ("YELLOW", (255, 220, 80)),
    ("ORANGE", (255, 140, 40)),
    ("RED",    (255, 70, 70)),
    ("GREEN",  (90, 230, 120)),
    ("PURPLE", (200, 90, 220)),
    ("BLACK",  (0, 0, 0)),
]

ANCHORS = ("tl", "t", "tr", "l", "c", "r", "bl", "b", "br")
ITEM_TYPES = ("text", "rect", "image", "menu", "progress_bar", "tiered_bar", "container")
# Font size sequence cycled by Y/A in details mode. (family, scale)
# pairs ordered by rendered line-height so font+ always picks the
# next-bigger visible size, regardless of which family it lives in.
# "" = default 5x7 family; "7x9" = mid-size family.
FONT_SIZES = (
    ("",    1),   # 5x7 s1 --  7 px line-height
    ("7x9", 1),   # 7x9 s1 --  8 px  (new bold 7x10 patterns, BASE_H=8)
    ("",    2),   # 5x7 s2 -- 14 px
    ("7x9", 2),   # 7x9 s2 -- 16 px
    ("",    3),   # 5x7 s3 -- 21 px
    ("7x9", 3),   # 7x9 s3 -- 24 px
    ("",    4),   # 5x7 s4 -- 28 px
    ("7x9", 4),   # 7x9 s4 -- 32 px
    ("",    5),   # 5x7 s5 -- 35 px
    ("",    6),   # 5x7 s6 -- 42 px
    ("",    7),   # 5x7 s7 -- 49 px
)
# Mode cycle. text editing is a SUB-STATE of details (entered via Tab on
# text/menu items), not a peer mode — keeps the SELECT cycle to 2 stops.
MODES = ("transform", "details")

# Editor window chrome.
WIN_W, WIN_H = 1366, 800
TOPBAR_H = 34
STATUS_H = 26
MARGIN = 8
PANEL_W = 360       # right-most info / properties panel
TREE_PANEL_W = 240  # middle hierarchy panel (between preview + info)
ZOOM_BAND_H = 220   # bottom band below preview that re-renders the active
                    # item at integer scale for a closer look. Preview drops
                    # one integer scale step if the desired one wouldn't fit
                    # alongside this band.

BG = (18, 20, 28)
PANEL_BG = (28, 32, 44)
BORDER = (60, 70, 100)
INK = (220, 230, 240)
DIM_INK = (130, 140, 160)
ACCENT = (255, 196, 64)
ACTIVE_OUTLINE = (255, 255, 255)
OTHER_OUTLINE = (160, 200, 240)
LIST_HIGHLIGHT = (60, 70, 100)
DIRTY = (255, 110, 110)
SAVED = (120, 220, 140)
PREVIEW_BG = (8, 10, 16)
MODE_BG = (60, 80, 120)

# Topbar background tint per mode — at-a-glance signal that the editor
# is in transform vs details, with a warm sub-state colour while typing.
# Hues are intentionally low-saturation so the ink/accent colours still
# read; values stay close to PANEL_BG's luminance.
MODE_TOPBAR_BG = {
    "transform": (28, 44, 76),    # cool blue — moving things in space
    "details":   (24, 56, 44),    # green     — tweaking item properties
}
TEXT_EDIT_TOPBAR_BG = (76, 52, 24)   # warm amber — typing characters

# Foreground equivalents — bright versions of the mode tints, used to
# colour the mode-only keybind labels in the hint panel so the eye can
# match label colour to the current topbar tint.
MODE_LABEL_COLOR = {
    "transform": (120, 180, 255),   # bright blue
    "details":   (120, 220, 140),   # bright green
}
TEXT_EDIT_LABEL_COLOR = (255, 180, 80)   # bright amber

INITIAL_REPEAT_MS = 250
REPEAT_INTERVAL_MS = 60
TRIGGER_THRESHOLD = 0.1

# Xbox / XInput button indices
JB_A, JB_B, JB_X, JB_Y = 0, 1, 2, 3
JB_LB, JB_RB = 4, 5
JB_BACK, JB_START = 6, 7
JB_LSB, JB_RSB = 8, 9
JA_LT, JA_RT = 4, 5
# Right stick axes — used for hierarchy navigation: push right/down to dive
# into the active container, left/up to pop back out.
JA_RSX, JA_RSY = 2, 3
RSTICK_THRESH = 0.5


# ---------------------------------------------------------------------------
# Layout file IO
# ---------------------------------------------------------------------------

def load_layout():
    data = {}
    if LAYOUT_PATH.exists():
        try:
            data = json.loads(LAYOUT_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"layout.json load failed: {e}; starting fresh")
    screens = data.get("screens") or {}
    out = {}
    for s in SCREENS:
        entry = screens.get(s) or {}
        items = entry.get("items") or []
        clean = []
        for it in items:
            if isinstance(it, dict) and it.get("type") in ITEM_TYPES:
                clean.append(dict(it))
        out[s] = {"items": clean}
    return {"screens": out}


def save_layout(layout):
    out = {"screens": {s: {"items": layout["screens"][s]["items"]}
                       for s in SCREENS}}
    LAYOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAYOUT_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Item factories
# ---------------------------------------------------------------------------

def _gen_id(prefix):
    return f"{prefix}_{pygame.time.get_ticks() % 1000000}"


def default_item(kind):
    cx, cy = SCREEN_W // 2, SCREEN_H // 2
    if kind == "text":
        return {"type": "text", "id": _gen_id("txt"),
                "x": cx, "y": cy, "anchor": "c",
                "text": "TEXT", "font": 3,
                "color": [240, 240, 240], "alpha": 255,
                "shadow": False}
    if kind == "rect":
        return {"type": "rect", "id": _gen_id("rect"),
                "x": cx - 60, "y": cy - 24, "w": 120, "h": 48,
                "color": [60, 80, 120], "alpha": 200, "outline": 0}
    if kind == "image":
        return {"type": "image", "id": _gen_id("img"),
                "x": cx, "y": cy, "anchor": "c",
                "sprite": "pickup_main", "scale": 1.0, "alpha": 255}
    if kind == "progress_bar":
        return {"type": "progress_bar", "id": _gen_id("bar"),
                "x": cx - 40, "y": cy, "w": 80, "h": 6,
                "value": 0.5, "max": 1.0, "segments": 10,
                "color": [80, 220, 255], "bg_color": [40, 46, 70],
                "alpha": 255}
    if kind == "tiered_bar":
        # Default to a 5-tier x 4-sub layout (main-weapon shape). Each tier
        # fills bottom-up by sub-level; sub-level separators vanish when
        # the tier is full.
        return {"type": "tiered_bar", "id": _gen_id("tbar"),
                "x": cx - 40, "y": cy, "w": 80, "h": 10,
                "value": 7, "max": 20, "tiers": 5,
                "color": [80, 220, 255], "bg_color": [40, 46, 70],
                "sep_color": [20, 26, 44]}
    if kind == "container":
        # panel_skin=1 = HUD-style chrome (bg/border/caps + title chip if
        # set) — set to 0 for an invisible grouping container. Skin owns
        # the chrome look; explicit bg/border/etc. on the dict override.
        return {"type": "container", "id": _gen_id("box"),
                "x": cx - 80, "y": cy - 60, "w": 160, "h": 120,
                "layout": "free", "padding": 4, "gap": 4,
                # Grid params are stashed even on free containers so toggling
                # to layout="grid" later doesn't require manual seeding.
                "rows": 2, "cols": 2, "gap_x": 4, "gap_y": 4,
                "panel_skin": 1, "alpha": 255,
                "children": []}
    raise ValueError(kind)


# ---------------------------------------------------------------------------
# Held-key dispatch (mirrors _sprite_editor.HeldKey)
# ---------------------------------------------------------------------------

class HeldKey:
    __slots__ = ("action", "next_fire_ms")

    def __init__(self, action, now_ms):
        self.action = action
        self.next_fire_ms = now_ms + INITIAL_REPEAT_MS


def _screen_render_offset(screen_name):
    """Top-level item coords on this screen render with this (x, y) offset.
    Lets the HUD spec stay in HUD-local coords (0..HUD_W) while the
    editor's full-screen preview lands it in the right strip. The engine
    achieves the same result by blitting a HUD-local surface at HUD_X."""
    if screen_name == "hud":
        return (480, 0)   # HUD_X
    return (0, 0)


def _strip_meta(node):
    """Recursive deep-copy of dicts/lists with `_`-prefixed keys removed.
    Used when materializing override entries from spec defaults — the
    `_label` / `_preview_options` / etc. fields are editor-only metadata
    and shouldn't pollute layout.json once the override is saved."""
    if isinstance(node, dict):
        return {k: _strip_meta(v) for k, v in node.items()
                if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(node, list):
        return [_strip_meta(x) for x in node]
    if isinstance(node, tuple):
        return tuple(_strip_meta(x) for x in node)
    return node


def _fmt_val(v):
    """Compact representation for flash messages — keeps the status bar
    readable even when several fields change in one tick."""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_fmt_val(x) for x in v) + "]"
    if isinstance(v, float):
        s = f"{v:.3f}".rstrip("0").rstrip(".")
        return s if s else "0"
    if isinstance(v, str):
        if len(v) <= 18:
            return repr(v)
        return repr(v[:18] + "…")
    return str(v)


# ---------------------------------------------------------------------------
# Editor state + behavior
# ---------------------------------------------------------------------------

class Editor:
    def __init__(self):
        # Import pewpew lazily so pygame.display is already up — BitmapFont
        # needs it to build glyph surfaces. Pewpew is a module-level import
        # of pygame plus class defs; no display / audio side effects.
        import pewpew
        self._pewpew = pewpew

        # Engine-side fonts (scale 1..7) — preview matches the real game.
        self.fonts = {scale: pewpew.BitmapFont(scale=scale) for scale in range(1, 8)}
        # 7x9 family entries match Game.__init__ — keyed by ("7x9", scale).
        for s in range(1, 5):
            self.fonts[("7x9", s)] = pewpew.BitmapFont7x9(scale=s)

        self.layout = load_layout()
        self.screen_idx = 0
        self.item_idx = 0
        self.mode = "transform"
        self.text_editing = False    # sub-state of details mode (Tab toggles)
        self.dirty = False
        self.quit_armed = False
        self.flash_t = 0
        self.flash_msg = ""
        self.flash_kind = "info"

        self.held = {}
        self.gamepad = None
        self.modifier_l2 = False
        self.modifier_r2 = False
        pygame.joystick.init()
        if pygame.joystick.get_count() > 0:
            self.gamepad = pygame.joystick.Joystick(0)
            self.gamepad.init()
            print(f"gamepad: {self.gamepad.get_name()}")

        self.sprite_names = self._scan_sprites()
        self._sprite_cache = {}
        self._backdrops = self._load_backdrops()
        self.list_rects = []
        self.show_grid = False
        self.show_gizmos = True   # selection boxes; toggle via R2+R3
        self.preview_scale = 2    # set for real in main() once display size is known
        # Hierarchical navigation: when self.container_stack is non-empty
        # we've dived into a user-created container, and self.item_idx now
        # indexes into that container's `children` list. R3 dives in (when
        # active item is a container), L3 pops back up. LB/RB cycle the
        # siblings at the current level.
        self.container_stack = []   # list of (container_dict, prev_item_idx)
        # Right-stick quantized state — used to fire dive/up once per
        # cross of the threshold rather than every frame.
        self._rstick_prev = (0, 0)   # (qx, qy) ∈ {-1, 0, 1}
        # Reparent clipboard. While carrying, the item is detached from
        # the tree and the topbar shows a "CARRY" tag.
        #   carry_origin = (owner_list, index) — used by carry_cancel to
        #     put the item back exactly where it was lifted from.
        #   carry_via_r2 = True if pickup happened during an R2 hold;
        #     releasing R2 then auto-drops at the current container.
        self.carrying = None
        self.carry_origin = None
        self.carry_via_r2 = False

    # ---- discovery -------------------------------------------------------
    def _scan_sprites(self):
        if not SPRITES_DIR.is_dir():
            return ["pickup_main"]
        names = []
        for p in sorted(SPRITES_DIR.glob("*.png")):
            names.append(p.stem)
        return names or ["pickup_main"]

    def _load_backdrops(self):
        out = {}
        for s, fname in BACKDROP_FILE.items():
            # Prefer naked variants (rendered with chrome stripped, so the
            # editor can overlay the live element values without ghosting
            # from baked-in chrome positions).
            stem = Path(fname).stem
            naked = SHOTS_DIR / f"{stem}_naked.png"
            path = naked if naked.exists() else SHOTS_DIR / fname
            if not path.exists():
                continue
            try:
                img = pygame.image.load(str(path)).convert_alpha()
                if img.get_size() != (SCREEN_W, SCREEN_H):
                    img = pygame.transform.smoothscale(img, (SCREEN_W, SCREEN_H))
                out[s] = img
            except Exception as e:
                print(f"backdrop load {s} failed: {e}")
        return out

    def sprite_lookup(self, name):
        if not name:
            return None
        if name in self._sprite_cache:
            return self._sprite_cache[name]
        path = SPRITES_DIR / f"{name}.png"
        if not path.exists():
            self._sprite_cache[name] = None
            return None
        try:
            img = pygame.image.load(str(path)).convert_alpha()
        except Exception:
            img = None
        self._sprite_cache[name] = img
        return img

    # ---- accessors -------------------------------------------------------
    @property
    def current_screen(self):
        return SCREENS[self.screen_idx]

    @property
    def current_items(self):
        """The raw layout-file items list for this screen (mutable). Holds
        overrides for built-ins (matched by id) plus any user-added items."""
        return self.layout["screens"][self.current_screen]["items"]

    def builtins_for(self, screen):
        return self._pewpew.LAYOUT_ELEMENTS.get(screen, [])

    def _builtin_ids(self, screen):
        return {b["id"] for b in self.builtins_for(screen)}

    def current_container(self):
        """The user-container we're navigating inside, or None when at the
        screen root (built-ins + top-level user items)."""
        return self.container_stack[-1][0] if self.container_stack else None

    def _merged_container(self, cont):
        """Spec+override view of a container handle. Fresh overrides for
        top-level builtin containers only carry {id, type, children} —
        the spec supplies x/y/w/h/padding/layout. Nested children carry
        their own copy of those fields (deep-copied at dive time) so
        this no-ops when the id doesn't appear in the screen's spec
        list."""
        cid = cont.get("id")
        if not cid:
            return cont
        for b in self.builtins_for(self.current_screen):
            if b.get("id") != cid:
                continue
            merged = dict(b)
            for k, v in cont.items():
                if k in ("id", "type"):
                    continue
                merged[k] = v
            return merged
        return cont

    def container_path_labels(self):
        """Human-readable breadcrumbs of the dive stack: ['screen', 'hud_chrome', 'status']."""
        out = [self.current_screen]
        for cont, _ in self.container_stack:
            out.append(cont.get("id") or cont.get("type") or "?")
        return out

    def display_rows(self):
        """Ordered display list at the CURRENT navigation level. At root
        that's built-ins + top-level user items (matches earlier behaviour).
        Inside a container it's just that container's children."""
        cont = self.current_container()
        if cont is None:
            return self._root_rows()
        children = cont.get("children") or []
        return [("child", id(c)) for c in children]

    def _moved_builtin_ids(self):
        """Builtin ids whose override has been carried into a child
        container (any depth) and therefore should no longer appear at
        screen root. Mirrors pewpew._layout_collect_deep_ids restricted
        to the current screen's builtins."""
        builtin_ids = self._builtin_ids(self.current_screen)
        out = set()
        def walk(its):
            for it in its:
                rid = it.get("id")
                if rid and rid in builtin_ids:
                    out.add(rid)
                children = it.get("children")
                if children:
                    walk(children)
        for it in self.current_items:
            children = it.get("children")
            if children:
                walk(children)
        return out

    def _root_rows(self):
        s = self.current_screen
        ids = self._builtin_ids(s)
        moved = self._moved_builtin_ids()
        rows = [("builtin", b["id"]) for b in self.builtins_for(s)
                if b["id"] not in moved]
        for it in self.current_items:
            if it.get("id") not in ids:
                rows.append(("user", id(it)))
        return rows

    def _row_at(self, idx):
        """Resolve a display-row index to (handle_or_None, kind, builtin_default).
        kind ∈ {"builtin", "user", "child"}. handle is the mutable item dict
        (None for built-ins with no override yet)."""
        rows = self.display_rows()
        if not rows:
            return None, None, None
        idx = max(0, min(idx, len(rows) - 1))
        kind, ident = rows[idx]
        cont = self.current_container()
        if cont is not None:
            # Inside a container — children list is the source of truth.
            for child in cont.get("children") or []:
                if id(child) == ident:
                    return child, "child", None
            return None, "child", None
        # At root.
        s = self.current_screen
        if kind == "builtin":
            builtin = next((b for b in self.builtins_for(s) if b["id"] == ident), None)
            for it in self.current_items:
                if it.get("id") == ident:
                    return it, "builtin", builtin
            return None, "builtin", builtin
        for it in self.current_items:
            if id(it) == ident:
                return it, "user", None
        return None, "user", None

    def active_handle(self, create=True):
        """Return the mutable item dict for the active row. For built-ins
        with no override yet, creates an override stub with id+type so
        mutations land in layout.json."""
        handle, kind, builtin = self._row_at(self.item_idx)
        if handle is None and create and kind == "builtin" and builtin is not None:
            handle = {"id": builtin["id"], "type": builtin["type"]}
            self.current_items.append(handle)
        return handle, kind, builtin

    def active_merged(self):
        """Read-only merged view of the active row. Use active_handle for writes."""
        handle, kind, builtin = self._row_at(self.item_idx)
        if kind == "builtin" and builtin is not None:
            merged = dict(builtin)
            if handle:
                for k, v in handle.items():
                    if k in ("id", "type"):
                        continue
                    merged[k] = v
            return merged
        return handle

    # ---- reparent (pickup / drop) ----------------------------------------
    def pick_up(self):
        """Detach the active item from its parent's children list and
        stash it on self.carrying. The item vanishes from display until
        dropped. Records carry_origin so carry_cancel can put it back.

        Builtins are special: the engine renders them inline via
        get_element() / spec walk, so just "removing the override" isn't
        enough — the spec would still render at root. Pickup of a builtin
        materializes the full merged dict (spec + any existing root
        override) into self.carrying and clears the root override; the
        engine then sees the id appear deep in the tree and hides the
        spec at root automatically."""
        handle, kind, builtin = self.active_handle(create=False)
        cont = self.current_container()

        if kind == "builtin" and builtin is not None:
            # Build a self-contained carry: every field the spec provides,
            # plus any user-tweaked fields layered on top. The strip drops
            # editor-only metadata (`_label`, etc.) so the carry persists
            # cleanly to layout.json if dropped without further edits.
            materialized = _strip_meta(dict(builtin))
            if handle is not None:
                for k, v in handle.items():
                    if k in ("id", "type"):
                        continue
                    materialized[k] = v
            if handle is not None and handle in self.current_items:
                self.current_items.remove(handle)
            if self.carrying is not None:
                self._flash(f"replacing carry: previous {self.carrying.get('id')} "
                            f"left at screen root", kind="warn")
                self.current_items.append(self.carrying)
            self.carrying = materialized
            # carry_origin: ("builtin", original_root_override_or_None).
            # carry_cancel restores by reinstating that override.
            self.carry_origin = ("builtin", handle)
            self.carry_via_r2 = self.modifier_r2
            self.item_idx = max(0, min(self.item_idx,
                                        len(self.display_rows()) - 1))
            self._touch()
            self._flash(f"picked up {builtin['id']} (built-in)  "
                        f"— navigate + drop")
            return

        if handle is None:
            return
        owner = (cont.get("children") if cont is not None
                 else self.current_items)
        if handle not in owner:
            self._flash("can't pick up — item not in current scope",
                        kind="warn")
            return
        if self.carrying is not None:
            self._flash(f"replacing carry: previous {self.carrying.get('id')} "
                        f"left at screen root", kind="warn")
            self.current_items.append(self.carrying)
        idx = owner.index(handle)
        owner.remove(handle)
        self.carrying = handle
        self.carry_origin = ("list", owner, idx)
        self.carry_via_r2 = self.modifier_r2
        self.item_idx = max(0, min(self.item_idx,
                                    len(self.display_rows()) - 1))
        self._touch()
        self._flash(f"picked up {handle.get('id') or handle.get('type')}  "
                    f"— navigate + drop (V / release R2)")

    def drop(self):
        """Append self.carrying to the current container's children
        (or screen's top-level user items at root)."""
        if self.carrying is None:
            return
        cont = self.current_container()
        if cont is None:
            self.current_items.append(self.carrying)
            where = f"{self.current_screen} root"
        else:
            cont.setdefault("children", []).append(self.carrying)
            where = cont.get("id") or cont.get("type") or "?"
        carried = self.carrying
        self.carrying = None
        self.carry_origin = None
        self.carry_via_r2 = False
        self.item_idx = len(self.display_rows()) - 1
        self._touch()
        self._flash(f"dropped {carried.get('id') or carried.get('type')}  "
                    f"→ {where}")

    def carry_cancel(self):
        """Put the carried item back at its exact origin position."""
        if self.carrying is None:
            return
        if self.carry_origin is None:
            # No origin recorded — fall back to dropping at root.
            self.drop()
            return
        tag = self.carry_origin[0]
        if tag == "list":
            _, owner, idx = self.carry_origin
            idx = max(0, min(idx, len(owner)))
            owner.insert(idx, self.carrying)
        elif tag == "builtin":
            # Restore the original root override (if any). If none existed
            # the builtin already renders at spec defaults, so nothing to
            # add back — the engine's deep-id check will stop suppressing
            # the spec once the carry id leaves the tree.
            _, original_override = self.carry_origin
            if original_override is not None:
                self.current_items.append(original_override)
        carried = self.carrying
        self.carrying = None
        self.carry_origin = None
        self.carry_via_r2 = False
        self._touch()
        self._flash(f"cancelled — {carried.get('id') or '?'} returned to origin")

    def carry_discard(self):
        """Throw the carried item away (no drop, no restore)."""
        if self.carrying is None:
            return
        carried = self.carrying
        self.carrying = None
        self.carry_origin = None
        self.carry_via_r2 = False
        self._touch()
        self._flash(f"discarded {carried.get('id') or carried.get('type')}",
                    kind="warn")

    def carry_drop_copy(self):
        """Drop a copy of the carried item into the current container,
        keep carrying the original. Useful for placing the same element
        at multiple locations from one pickup."""
        if self.carrying is None:
            return
        clone = copy.deepcopy(self.carrying)
        # Fresh id so the clone doesn't collide with the original on save.
        if clone.get("id"):
            clone["id"] = _gen_id(clone.get("type", "x")[:3])
        cont = self.current_container()
        owner = (cont.setdefault("children", []) if cont is not None
                 else self.current_items)
        owner.append(clone)
        # The user is now explicitly managing the carry — clear the R2
        # gesture flag so releasing R2 doesn't auto-drop the original
        # *here too*, which would land two items at the same spot.
        self.carry_via_r2 = False
        self._touch()
        where = (cont.get("id") if cont is not None else f"{self.current_screen} root")
        self._flash(f"copy of {self.carrying.get('id') or '?'} → {where}  "
                    f"(still carrying original)")

    def carry_wrap(self):
        """Create a new container at the current navigation level and
        drop the carried item inside it as its first child. End carry."""
        if self.carrying is None:
            return
        wrap = default_item("container")
        # Keep the wrapper small so it doesn't dominate the screen — sized
        # to fit a typical UI label cluster.
        wrap["w"] = 200
        wrap["h"] = 80
        wrap["children"] = [self.carrying]
        cont = self.current_container()
        owner = (cont.setdefault("children", []) if cont is not None
                 else self.current_items)
        owner.append(wrap)
        carried = self.carrying
        self.carrying = None
        self.carry_origin = None
        self.carry_via_r2 = False
        self.item_idx = len(self.display_rows()) - 1
        self._touch()
        self._flash(f"wrapped {carried.get('id') or '?'} in new container "
                    f"{wrap['id']}")

    # ---- tree navigation -------------------------------------------------
    def dive(self):
        """If the active item is a container, push it onto the nav stack
        and reset item_idx to the first child. For BUILT-IN containers we
        eagerly materialize an override with a deep-copy of the spec's
        children so subsequent edits land in the override (and persist to
        layout.json on save) instead of silently mutating LAYOUT_ELEMENTS
        in memory. `_`-prefixed metadata fields are stripped from the
        copy to keep the saved file clean."""
        merged = self.active_merged()
        if merged is None:
            return False
        if merged.get("type") != "container" and "children" not in merged:
            return False
        handle, kind, builtin = self.active_handle(create=True)
        if handle is None:
            return False
        if handle.get("type") != "container":
            return False
        if (kind == "builtin" and builtin is not None
                and "children" not in handle):
            spec_children = builtin.get("children") or []
            if spec_children:
                handle["children"] = _strip_meta(spec_children)
        children = handle.setdefault("children", [])
        self.container_stack.append((handle, self.item_idx))
        self.item_idx = 0
        self._flash(f"dive → {handle.get('id') or handle.get('type')}  "
                    f"({len(children)} children)")
        return True

    def up(self):
        """Pop the nav stack and restore the previous selection."""
        if not self.container_stack:
            return False
        prev_container, prev_idx = self.container_stack.pop()
        self.item_idx = prev_idx
        self._flash(f"up → {self.container_path_labels()[-1]}")
        return True

    @property
    def active_item(self):
        # Kept for backward-compat with any old call sites; equivalent to
        # active_merged() (read-only view).
        return self.active_merged()

    def all_items_merged(self):
        """All ROOT-level items for the current screen in render order:
        built-ins (with overrides applied) first, then user top-level
        items. Container children render via the engine's recursive
        path. Builtins that the user has carried into a child container
        are skipped here — their moved copy renders via the recursive
        path, and re-rendering the spec at root would duplicate it."""
        s = self.current_screen
        out = []
        overrides = {it.get("id"): it for it in self.current_items
                     if it.get("id") in self._builtin_ids(s)}
        moved = self._moved_builtin_ids()
        for b in self.builtins_for(s):
            if b["id"] in moved:
                continue
            merged = dict(b)
            ov = overrides.get(b["id"])
            if ov:
                for k, v in ov.items():
                    if k in ("id", "type"):
                        continue
                    merged[k] = v
            out.append(("builtin", merged))
        ids = self._builtin_ids(s)
        for it in self.current_items:
            if it.get("id") not in ids:
                out.append(("user", it))
        return out

    def current_level_items(self):
        """(kind, item_dict) pairs for siblings at the current nav level.
        At root: same as all_items_merged. Inside a container: that
        container's children (which are mutable dicts directly)."""
        cont = self.current_container()
        if cont is None:
            return self.all_items_merged()
        children = cont.get("children") or []
        return [("child", c) for c in children]

    def active_global_offset(self):
        """(gx, gy) world offset to add to a current-level item's local
        x/y so the editor can draw its selection box in screen-space.
        Includes the per-screen render offset so HUD-local specs land in
        the right strip. Only `free` layouts accumulate offsets — stack/
        grid auto-position children, so the editor returns None and the
        selection box for children of such a container is omitted."""
        sox, soy = _screen_render_offset(self.current_screen)
        gx, gy = sox, soy
        for cont, _ in self.container_stack:
            cont = self._merged_container(cont)
            layout = (cont.get("layout") or "free").lower()
            if layout != "free":
                return None
            pad = int(cont.get("padding", 0))
            gx += int(cont.get("x", 0)) + pad
            gy += int(cont.get("y", 0)) + pad
        return gx, gy

    def current_container_screen_pos(self):
        """On-screen (x, y) of the current container's top-left border.
        Sum of screen offset + every ancestor container's x/y/padding +
        the current container's own x/y (without its OWN padding — the
        outline wraps the border, not the padded inner area)."""
        if not self.container_stack:
            return None
        sox, soy = _screen_render_offset(self.current_screen)
        gx, gy = sox, soy
        n = len(self.container_stack)
        for i, (c, _) in enumerate(self.container_stack):
            c = self._merged_container(c)
            layout = (c.get("layout") or "free").lower()
            if layout != "free":
                return None
            gx += int(c.get("x", 0))
            gy += int(c.get("y", 0))
            if i < n - 1:   # add padding for all ancestors but not self
                pad = int(c.get("padding", 0))
                gx += pad
                gy += pad
        return gx, gy

    def full_tree_rows(self):
        """Recursive flat list of every item in the layout, for the
        hierarchy panel. Each entry: (depth, kind, item_dict, is_active,
        is_current_container).

        Matching is by `id` field — for root built-ins active_merged()
        returns a fresh merged dict whose Python id() doesn't match the
        dict the walk iterates over, so id-field match is the only way
        active items at root get highlighted."""
        out = []
        active = self.active_merged()
        active_id = active.get("id") if isinstance(active, dict) else None
        current_cont = self.current_container()
        current_cont_id = (current_cont.get("id")
                           if current_cont is not None else None)

        def walk(item, depth, kind):
            it_id = item.get("id")
            is_active = (active_id is not None and it_id == active_id)
            is_cur = (current_cont_id is not None and it_id == current_cont_id)
            out.append((depth, kind, item, is_active, is_cur))
            if item.get("type") == "container" or "children" in item:
                for ch in item.get("children") or ():
                    walk(ch, depth + 1, "child")

        for kind, item in self.all_items_merged():
            walk(item, 0, kind)
        return out

    # ---- input -----------------------------------------------------------
    def current_stride(self):
        if pygame.key.get_mods() & pygame.KMOD_SHIFT:
            return 5
        if self.modifier_l2:
            return 5
        return 1

    def start_action(self, key, action):
        self.apply_action(action)
        self.held[key] = HeldKey(action, pygame.time.get_ticks())

    def stop_action(self, key):
        self.held.pop(key, None)

    def tick_held(self):
        now = pygame.time.get_ticks()
        for h in list(self.held.values()):
            fires = 0
            while h.next_fire_ms <= now and fires < 4:
                self.apply_action(h.action)
                h.next_fire_ms += REPEAT_INTERVAL_MS
                fires += 1
            if h.next_fire_ms + REPEAT_INTERVAL_MS < now:
                h.next_fire_ms = now + REPEAT_INTERVAL_MS

    def apply_action(self, action):
        stride = self.current_stride()

        # Snapshot just enough state to describe what changed after the
        # action runs. Items have a `_label` field we don't want diffed.
        before_screen = self.current_screen
        before_mode = self.mode
        before_idx = self.item_idx
        before_rows = self.display_rows()
        before_merged = None
        if before_rows:
            m = self.active_merged()
            if m:
                before_merged = {k: v for k, v in m.items() if not k.startswith("_")}

        nav_or_special = action in (
            "screen_next", "screen_prev", "item_next", "item_prev", "mode_cycle",
            "add_text", "add_rect", "add_image", "add_progress_bar",
            "add_tiered_bar",
            "add_container", "dive", "up", "duplicate", "delete",
            "toggle_grid", "pick_up", "drop",
            "carry_cancel", "carry_discard", "carry_drop_copy", "carry_wrap",
            "panel_skin_next", "panel_skin_prev",
        )

        # Navigation actions never touch a specific item — handle first.
        if action == "screen_next": self.cycle_screen(1)
        elif action == "screen_prev": self.cycle_screen(-1)
        elif action == "item_next":   self.cycle_item(1)
        elif action == "item_prev":   self.cycle_item(-1)
        elif action == "mode_cycle":  self.cycle_mode()
        elif action == "add_text":    self._add(default_item("text"))
        elif action == "add_rect":    self._add(default_item("rect"))
        elif action == "add_image":   self._add(default_item("image"))
        elif action == "add_progress_bar": self._add(default_item("progress_bar"))
        elif action == "add_tiered_bar":   self._add(default_item("tiered_bar"))
        elif action == "add_container":    self._add(default_item("container"))
        elif action == "dive":        self.dive()
        elif action == "up":          self.up()
        elif action == "duplicate":   self._duplicate()
        elif action == "delete":      self._delete()
        elif action == "pick_up":     self.pick_up()
        elif action == "drop":        self.drop()
        elif action == "carry_cancel":     self.carry_cancel()
        elif action == "carry_discard":    self.carry_discard()
        elif action == "carry_drop_copy":  self.carry_drop_copy()
        elif action == "carry_wrap":       self.carry_wrap()
        elif action == "panel_skin_next":  self.cycle_panel_skin(+1)
        elif action == "panel_skin_prev":  self.cycle_panel_skin(-1)
        elif action == "toggle_grid":
            self.show_grid = not self.show_grid
            self._flash(f"grid {'on' if self.show_grid else 'off'}")
        else:
            self._apply_mutation_action(action, stride)

        if not nav_or_special:
            self._emit_change_feedback(before_merged)
        else:
            self._emit_nav_feedback(action, before_screen, before_mode,
                                    before_idx, before_rows)

    def _apply_mutation_action(self, action, stride):
        merged = self.active_merged()
        if merged is None:
            return
        kind = merged.get("type")
        # Transform-mode arrows + WASD.
        if action == "pos_left":   self._set_field("x", int(merged.get("x", 0)) - stride)
        elif action == "pos_right": self._set_field("x", int(merged.get("x", 0)) + stride)
        elif action == "pos_up":    self._set_field("y", int(merged.get("y", 0)) - stride)
        elif action == "pos_down":  self._set_field("y", int(merged.get("y", 0)) + stride)
        elif action == "size_w_dec": self._nudge_size(-stride, 0)
        elif action == "size_w_inc": self._nudge_size(+stride, 0)
        elif action == "size_h_dec": self._nudge_size(0, -stride)
        elif action == "size_h_inc": self._nudge_size(0, +stride)
        # Style-mode arrows: per-item-type nudges.
        elif action == "style_left":   self._style_horiz(-stride)
        elif action == "style_right":  self._style_horiz(+stride)
        elif action == "style_up":     self._style_vert(-stride)
        elif action == "style_down":   self._style_vert(+stride)
        # Style-mode WASD: alpha + anchor.
        elif action == "alpha_dec":   self._nudge_alpha(-4 * stride)
        elif action == "alpha_inc":   self._nudge_alpha(+4 * stride)
        elif action == "anchor_prev": self._cycle_anchor(-1)
        elif action == "anchor_next": self._cycle_anchor(+1)
        elif action == "shadow_toggle":
            if kind == "text":
                self._set_field("shadow", not merged.get("shadow", False))
        # Anchor / alignment cycling. (align_h_* are back-compat aliases
        # from the earlier R2 chord that's been retired.)
        elif action in ("anchor_h_prev", "align_h_prev"): self.cycle_align_horiz(-1)
        elif action in ("anchor_h_next", "align_h_next"): self.cycle_align_horiz(+1)
        elif action == "anchor_v_prev":    self.cycle_anchor_vert(-1)
        elif action == "anchor_v_next":    self.cycle_anchor_vert(+1)
        # Details face buttons (per-type dispatch).
        elif action == "details_x_dec":    self.details_x(-1)
        elif action == "details_x_inc":    self.details_x(+1)
        elif action == "details_y_dec":    self.details_y(-1)
        elif action == "details_y_inc":    self.details_y(+1)
        # Back-compat: the old text-mode action names still work as aliases
        # so any keybindings or scripted tests built against them keep firing.
        elif action == "text_color_prev":  self.details_x(-1)
        elif action == "text_color_next":  self.details_x(+1)
        elif action == "text_font_dec":    self.details_y(-1)
        elif action == "text_font_inc":    self.details_y(+1)
        # Other helpers kept available for future bindings.
        elif action == "vspacing_dec":     self.nudge_vspacing(-1)
        elif action == "vspacing_inc":     self.nudge_vspacing(+1)
        elif action == "palette_prev":     self.cycle_palette(-1)
        elif action == "palette_next":     self.cycle_palette(+1)
        elif action == "sel_palette_prev": self.cycle_sel_palette(-1)
        elif action == "sel_palette_next": self.cycle_sel_palette(+1)
        # Grid chord actions (R2 + face / D-pad on a grid container).
        elif action == "grid_cols_dec":  self.nudge_grid_cols(-1)
        elif action == "grid_cols_inc":  self.nudge_grid_cols(+1)
        elif action == "grid_rows_dec":  self.nudge_grid_rows(-1)
        elif action == "grid_rows_inc":  self.nudge_grid_rows(+1)
        elif action == "gap_x_dec":      self.nudge_grid_gap_x(-1)
        elif action == "gap_x_inc":      self.nudge_grid_gap_x(+1)
        elif action == "gap_y_dec":      self.nudge_grid_gap_y(-1)
        elif action == "gap_y_inc":      self.nudge_grid_gap_y(+1)

    # ---- feedback --------------------------------------------------------
    def _emit_change_feedback(self, before_merged):
        """Diff the active item's merged values against the snapshot and
        flash a 'field: before → after' message. Quiet if nothing changed
        (e.g. clamped at a limit)."""
        if before_merged is None:
            return
        merged = self.active_merged()
        if merged is None:
            return
        after = {k: v for k, v in merged.items() if not k.startswith("_")}
        diffs = []
        for k, v in after.items():
            if before_merged.get(k) != v:
                diffs.append(f"{k} {_fmt_val(before_merged.get(k))} → {_fmt_val(v)}")
        for k in before_merged:
            if k not in after:
                diffs.append(f"{k} {_fmt_val(before_merged[k])} → —")
        if not diffs:
            return
        # Cap at 3 fields so the bar stays readable.
        msg = "  ·  ".join(diffs[:3])
        if len(diffs) > 3:
            msg += f"  · +{len(diffs)-3} more"
        self._flash(msg)

    def _emit_nav_feedback(self, action, before_screen, before_mode,
                            before_idx, before_rows):
        if action in ("screen_next", "screen_prev"):
            after = self.current_screen
            if after != before_screen:
                n = len(self.display_rows())
                self._flash(f"screen {before_screen} → {after}  ({n} items)")
            return
        if action in ("item_next", "item_prev"):
            rows = self.display_rows()
            if not rows:
                self._flash("(no items on this screen)")
                return
            kind, ident = rows[self.item_idx]
            tag = "[B]" if kind == "builtin" else "[U]"
            self._flash(
                f"item {before_idx + 1}/{len(before_rows) if before_rows else 0} → "
                f"{self.item_idx + 1}/{len(rows)}  {tag} {ident}")
            return
        if action == "mode_cycle":
            self._flash(f"mode {before_mode} → {self.mode}")
            return
        # add_*, duplicate, delete, toggle_grid set their own flash.

    # ---- mutators --------------------------------------------------------
    def _touch(self):
        self.dirty = True
        self.quit_armed = False

    def _set_field(self, key, value):
        """Write a single field to the active item handle (creating an
        override entry for built-ins on demand)."""
        handle, _, _ = self.active_handle(create=True)
        if handle is None:
            return
        handle[key] = value
        self._touch()

    def _add(self, item):
        """Add a new item to the current container's children, or to the
        screen's top-level user items when at root. Avoid id collisions
        with built-ins so the new item is always classified as user-owned."""
        ids = self._builtin_ids(self.current_screen)
        while item.get("id") in ids:
            item["id"] = _gen_id(item["type"][:3])
        cont = self.current_container()
        if cont is None:
            self.current_items.append(item)
        else:
            # When the parent positions its children (stack/grid), zero out
            # the default x/y so the child sits at the slot the parent
            # picked instead of at the screen-center default it carries.
            layout = (cont.get("layout") or "free").lower()
            if layout in ("stack", "grid"):
                item["x"] = 0
                item["y"] = 0
                # For grid cells we usually want the child centered in its
                # cell — flip the anchor default to "c" where supported.
                if layout == "grid" and item.get("type") in ("text", "image"):
                    item["anchor"] = "c"
            cont.setdefault("children", []).append(item)
        self.item_idx = len(self.display_rows()) - 1
        self._touch()
        where = cont.get("id") if cont is not None else "screen root"
        self._flash(f"added {item['type']} → {where}")

    def _duplicate(self):
        merged = self.active_merged()
        if merged is None:
            return
        kind = merged.get("type", "text")
        clone = dict(merged)
        # Strip registry-only metadata before persisting.
        for k in list(clone.keys()):
            if k.startswith("_"):
                clone.pop(k)
        clone["id"] = _gen_id(kind[:3])
        clone["x"] = int(merged.get("x", 0)) + 12
        clone["y"] = int(merged.get("y", 0)) + 12
        # Drop built-in-only fields so the clone renders cleanly as a user
        # item via the standard text/rect/image/menu draw paths.
        self.current_items.append(clone)
        self.item_idx = len(self.display_rows()) - 1
        self._touch()
        self._flash("duplicated")

    def _delete(self):
        handle, kind, builtin = self.active_handle(create=False)
        if handle is None:
            if kind == "builtin":
                self._flash(f"{builtin['id']} already at defaults")
            return
        if kind == "builtin":
            # Built-in delete = reset to defaults (remove the override).
            self.current_items.remove(handle)
            self._touch()
            self._flash(f"reset {builtin['id']} to defaults")
            return
        # User item or child item: remove from the owning list.
        cont = self.current_container()
        owner = (cont.get("children") if cont is not None else self.current_items)
        if handle in owner:
            owner.remove(handle)
        self.item_idx = max(0, self.item_idx - 1)
        self._touch()
        self._flash("deleted")

    def _nudge_size(self, dw, dh):
        merged = self.active_merged()
        if merged is None:
            return
        kind = merged.get("type")
        if kind == "rect":
            if dw: self._set_field("w", max(1, int(merged.get("w", 1)) + dw))
            if dh: self._set_field("h", max(1, int(merged.get("h", 1)) + dh))
        elif kind == "image":
            if dw or dh:
                step = (dw + dh) * 0.05
                self._set_field("scale", max(0.1, round(float(merged.get("scale", 1.0)) + step, 3)))
        elif kind == "menu":
            # WASD on a menu nudges line_height (W/S) and font (A/D).
            if dh:
                self._set_field("line_height", max(8, int(merged.get("line_height", 44)) + dh))
            if dw:
                self._cycle_font_size(merged, 1 if dw > 0 else -1)
        elif kind == "text":
            # Text has no literal container — the rendered bbox is driven
            # by font scale. WASD all step font (one slot per press through
            # the merged 5x7/7x9 sequence).
            step = 0
            if dw > 0 or dh > 0: step = +1
            if dw < 0 or dh < 0: step = -1
            if step:
                self._cycle_font_size(merged, step)

    def _style_horiz(self, step):
        merged = self.active_merged()
        if merged is None: return
        kind = merged.get("type")
        if kind in ("text", "menu"):
            self._cycle_font_size(merged, 1 if step > 0 else -1)
        elif kind == "rect":
            self._set_field("outline", max(0, int(merged.get("outline", 0)) + (1 if step > 0 else -1)))
        elif kind == "image":
            names = self.sprite_names
            if not names: return
            cur = merged.get("sprite") or names[0]
            idx = names.index(cur) if cur in names else 0
            idx = (idx + (1 if step > 0 else -1)) % len(names)
            self._set_field("sprite", names[idx])
        elif kind == "container":
            self.cycle_container_layout(1 if step > 0 else -1)

    def _style_vert(self, step):
        merged = self.active_merged()
        if merged is None: return
        kind = merged.get("type")
        if kind in ("text", "rect", "menu"):
            cur = tuple(merged.get("color") or PALETTE[0][1])
            names = [p[1] for p in PALETTE]
            idx = 0
            for i, c in enumerate(names):
                if tuple(c) == cur:
                    idx = i; break
            idx = (idx + (1 if step > 0 else -1)) % len(names)
            self._set_field("color", list(names[idx]))
        elif kind == "image":
            step_f = (-1 if step > 0 else 1) * 0.05
            self._set_field("scale", max(0.1, round(float(merged.get("scale", 1.0)) - step_f, 3)))
        elif kind == "container":
            # Cycle bg color through PALETTE.
            cur = tuple(merged.get("bg") or PALETTE[0][1])
            names = [p[1] for p in PALETTE]
            idx = 0
            for i, c in enumerate(names):
                if tuple(c) == cur:
                    idx = i; break
            idx = (idx + (1 if step > 0 else -1)) % len(names)
            self._set_field("bg", list(names[idx]))
        elif kind in ("progress_bar", "tiered_bar"):
            cur = tuple(merged.get("color") or PALETTE[0][1])
            names = [p[1] for p in PALETTE]
            idx = 0
            for i, c in enumerate(names):
                if tuple(c) == cur:
                    idx = i; break
            idx = (idx + (1 if step > 0 else -1)) % len(names)
            self._set_field("color", list(names[idx]))

    def _nudge_alpha(self, delta):
        merged = self.active_merged()
        if merged is None: return
        cur = int(merged.get("alpha", 255))
        self._set_field("alpha", max(0, min(255, cur + delta)))

    def _cycle_anchor(self, delta):
        merged = self.active_merged()
        if merged is None: return
        kind = merged.get("type")
        if kind == "rect":
            return
        if kind == "menu":
            # Menus use `align` (left/center/right) — not the 9-point anchor.
            aligns = ("left", "center", "right")
            cur = (merged.get("align") or "center").lower()
            i = aligns.index(cur) if cur in aligns else 1
            i = (i + delta) % len(aligns)
            self._set_field("align", aligns[i])
            return
        cur = merged.get("anchor", "tl")
        idx = ANCHORS.index(cur) if cur in ANCHORS else 0
        idx = (idx + delta) % len(ANCHORS)
        self._apply_anchor(ANCHORS[idx])

    def _apply_anchor(self, new_anchor):
        """Set `anchor` while compensating x/y so the rendered top-left
        stays put — changing the pivot shouldn't drag the visual position.
        Falls back to a plain anchor write when bounds aren't computable
        (no font yet, missing sprite, etc.)."""
        merged = self.active_merged()
        if merged is None: return
        kind = merged.get("type")
        if kind not in ("text", "image"):
            self._set_field("anchor", new_anchor)
            return
        # Snapshot the rendered TL via item_bounds (px, py) — this is the
        # same geometry the gizmo box uses, so compensation keeps both
        # the rendered glyphs AND the selection rect in place.
        px, py, w, h = item_bounds(merged, self)
        ox, oy = anchor_offset(new_anchor, w, h)
        new_x = px - ox
        new_y = py - oy
        handle, _, _ = self.active_handle(create=True)
        if handle is None: return
        handle["x"] = int(new_x)
        handle["y"] = int(new_y)
        handle["anchor"] = new_anchor
        self._touch()

    def set_palette(self, idx):
        merged = self.active_merged()
        if merged is None: return
        if merged.get("type") not in ("text", "rect", "menu"): return
        if 0 <= idx < len(PALETTE):
            self._set_field("color", list(PALETTE[idx][1]))

    def set_selected_palette(self, idx):
        """Menu-only: set the selected_color from PALETTE."""
        merged = self.active_merged()
        if merged is None or merged.get("type") != "menu": return
        if 0 <= idx < len(PALETTE):
            self._set_field("selected_color", list(PALETTE[idx][1]))

    # ---- chord-friendly cycles (used by R2 + face/D-pad gamepad chords) ---
    def cycle_align_horiz(self, delta):
        """Cycle horizontal alignment. For menu, that's the `align` field
        (left/center/right). For text/image, cycle the horizontal half of
        `anchor` while preserving the vertical half."""
        merged = self.active_merged()
        if merged is None: return
        kind = merged.get("type")
        if kind == "menu":
            aligns = ("left", "center", "right")
            cur = (merged.get("align") or "center").lower()
            i = aligns.index(cur) if cur in aligns else 1
            i = max(0, min(len(aligns) - 1, i + delta))
            self._set_field("align", aligns[i])
            return
        if kind in ("text", "image"):
            cur = merged.get("anchor", "tl")
            if cur == "c":
                v_part, h_part = "c", "c"
            elif len(cur) == 1:
                # single-char anchors: "t","b","l","r" — pick a sensible split.
                if cur in ("t", "b"): v_part, h_part = cur, "c"
                else:                 v_part, h_part = "c", cur
            else:
                v_part, h_part = cur[0], cur[1]
            h_seq = ("l", "c", "r")
            i = h_seq.index(h_part) if h_part in h_seq else 1
            i = max(0, min(2, i + delta))
            new_h = h_seq[i]
            # Re-encode (v_part, new_h) → anchor code from ANCHORS.
            if v_part == "t":
                new_anchor = {"l": "tl", "c": "t", "r": "tr"}[new_h]
            elif v_part == "b":
                new_anchor = {"l": "bl", "c": "b", "r": "br"}[new_h]
            else:  # "c"
                new_anchor = {"l": "l", "c": "c", "r": "r"}[new_h]
            self._apply_anchor(new_anchor)

    def cycle_anchor_vert(self, delta):
        """Cycle the vertical half of `anchor` (top/center/bottom) for
        text/image while preserving the horizontal half. No-op for menu/rect."""
        merged = self.active_merged()
        if merged is None: return
        kind = merged.get("type")
        if kind not in ("text", "image"):
            return
        cur = merged.get("anchor", "tl")
        if cur == "c":
            v_part, h_part = "c", "c"
        elif len(cur) == 1:
            if cur in ("t", "b"): v_part, h_part = cur, "c"
            else:                 v_part, h_part = "c", cur
        else:
            v_part, h_part = cur[0], cur[1]
        v_seq = ("t", "c", "b")
        i = v_seq.index(v_part) if v_part in v_seq else 1
        i = max(0, min(2, i + delta))
        new_v = v_seq[i]
        if h_part == "l":
            new_anchor = {"t": "tl", "c": "l", "b": "bl"}[new_v]
        elif h_part == "r":
            new_anchor = {"t": "tr", "c": "r", "b": "br"}[new_v]
        else:  # "c"
            new_anchor = {"t": "t", "c": "c", "b": "b"}[new_v]
        self._apply_anchor(new_anchor)

    def nudge_vspacing(self, delta):
        """R2+up/down: per-type vertical-spacing nudge.
        menu: line_height. text: font scale. rect: h. image: scale."""
        merged = self.active_merged()
        if merged is None: return
        kind = merged.get("type")
        stride = self.current_stride()
        if kind == "menu":
            self._set_field("line_height",
                            max(8, int(merged.get("line_height", 44)) + delta * stride))
        elif kind == "text":
            self._cycle_font_size(merged, delta)
        elif kind == "rect":
            self._set_field("h", max(1, int(merged.get("h", 1)) + delta * stride))
        elif kind == "image":
            step = delta * 0.05 * stride
            self._set_field("scale", max(0.1, round(float(merged.get("scale", 1.0)) + step, 3)))

    def _cycle_palette_for(self, key, delta):
        merged = self.active_merged()
        if merged is None: return
        cur = tuple(merged.get(key) or PALETTE[0][1])
        names = [p[1] for p in PALETTE]
        i = 0
        for j, c in enumerate(names):
            if tuple(c) == cur:
                i = j; break
        i = (i + delta) % len(names)
        self._set_field(key, list(names[i]))

    def cycle_palette(self, delta):
        """R2+X/B: cycle the primary `color` through PALETTE."""
        merged = self.active_merged()
        if merged is None: return
        if merged.get("type") not in ("text", "rect", "menu"): return
        self._cycle_palette_for("color", delta)

    def cycle_sel_palette(self, delta):
        """R2+Y/A: cycle the menu `selected_color` through PALETTE.
        Extend here when other element types grow a 'special' color."""
        merged = self.active_merged()
        if merged is None: return
        if merged.get("type") != "menu":
            return
        self._cycle_palette_for("selected_color", delta)

    # ---- container / grid -------------------------------------------------
    def _active_grid_container(self):
        """Return (merged, handle) when the active item is a grid container,
        else (None, None). Grid chord actions early-out using this."""
        merged = self.active_merged()
        if merged is None: return None, None
        if merged.get("type") != "container": return None, None
        if (merged.get("layout") or "free").lower() != "grid": return None, None
        handle, _, _ = self.active_handle(create=True)
        return merged, handle

    def nudge_grid_cols(self, delta):
        merged, handle = self._active_grid_container()
        if merged is None: return
        new = max(1, int(merged.get("cols", 1)) + delta)
        self._set_field("cols", new)

    def nudge_grid_rows(self, delta):
        merged, handle = self._active_grid_container()
        if merged is None: return
        new = max(1, int(merged.get("rows", 1)) + delta)
        self._set_field("rows", new)

    def nudge_grid_gap_x(self, delta):
        merged, handle = self._active_grid_container()
        if merged is None: return
        new = max(0, int(merged.get("gap_x", 0)) + delta * self.current_stride())
        self._set_field("gap_x", new)

    def nudge_grid_gap_y(self, delta):
        merged, handle = self._active_grid_container()
        if merged is None: return
        new = max(0, int(merged.get("gap_y", 0)) + delta * self.current_stride())
        self._set_field("gap_y", new)

    def details_x(self, delta):
        """Details mode X/B (or A/D on kb): cycle the primary 'what'.
        Color for text/rect/menu/progress_bar/tiered_bar; bg for container;
        sprite for image (no color field). delta = ±1."""
        merged = self.active_merged()
        if merged is None: return
        kind = merged.get("type")
        if kind in ("text", "rect", "menu", "progress_bar", "tiered_bar"):
            self._cycle_palette_for("color", delta)
        elif kind == "container":
            self._cycle_palette_for("bg", delta)
        elif kind == "image":
            names = self.sprite_names
            if not names: return
            cur = merged.get("sprite") or names[0]
            idx = names.index(cur) if cur in names else 0
            idx = (idx + delta) % len(names)
            self._set_field("sprite", names[idx])

    def details_y(self, delta):
        """Details mode Y/A (or W/S on kb): the secondary attribute per
        type — font for text/menu, scale for image, outline for rect,
        segments for progress_bar, layout for container. delta = ±1.

        Font size cycling is intentionally inverted from the other
        secondaries: Y / details_y_dec / W picks the BIGGER size
        because going "up" with the joystick feels like growing the
        text. (Other types keep the convention W=down, S=up so rect
        outline / image scale stays consistent.)"""
        merged = self.active_merged()
        if merged is None: return
        kind = merged.get("type")
        if kind in ("text", "menu"):
            self._cycle_font_size(merged, -delta)
        elif kind == "image":
            cur = float(merged.get("scale", 1.0)) + delta * 0.05
            self._set_field("scale", max(0.1, round(cur, 3)))
        elif kind == "rect":
            cur = max(0, int(merged.get("outline", 0)) + delta)
            self._set_field("outline", cur)
        elif kind == "progress_bar":
            cur = max(1, int(merged.get("segments", 10)) + delta)
            self._set_field("segments", cur)
        elif kind == "container":
            self.cycle_container_layout(1 if delta > 0 else -1)

    def _cycle_font_size(self, merged, delta):
        """Move the active item one step through FONT_SIZES (the merged
        5x7/7x9 sequence ordered by line height). Writes both `font` and
        `font_family` so the engine resolves the correct cell. delta is
        +1 / -1; clamped at the ends (no wrap)."""
        cur_fam = (merged.get("font_family") or "").strip()
        cur_scale = int(merged.get("font", 3))
        try:
            idx = FONT_SIZES.index((cur_fam, cur_scale))
        except ValueError:
            # Item set to a font/family combo not in our sequence (eg
            # 5x7 scale > 7); pick the closest by scale alone.
            idx = next((i for i, (f, s) in enumerate(FONT_SIZES)
                        if s >= cur_scale), len(FONT_SIZES) - 1)
        idx = max(0, min(len(FONT_SIZES) - 1, idx + delta))
        new_fam, new_scale = FONT_SIZES[idx]
        if new_scale != cur_scale:
            self._set_field("font", new_scale)
        # Always touch font_family so swapping back to the default
        # family writes the empty string (override removal happens at
        # the on-disk layer if/when that matters).
        if (new_fam or "") != cur_fam:
            self._set_field("font_family", new_fam)

    def cycle_panel_skin(self, delta):
        """Cycle the active container's `panel_skin` field. Each skin is
        a preset chrome look (0 = nothing, 1 = HUD panel, ...) — see
        pewpew._PANEL_SKINS. Number of available skins is read from the
        engine module so adding a new skin there exposes it here too."""
        merged = self.active_merged()
        if merged is None or merged.get("type") != "container":
            return
        n_skins = len(self._pewpew._PANEL_SKINS)
        if n_skins <= 1:
            return
        cur = int(merged.get("panel_skin", 0))
        new = (cur + delta) % n_skins
        self._set_field("panel_skin", new)

    def cycle_container_layout(self, delta):
        """Container style mode L/R: cycle layout (free → stack_v → stack_h → grid)."""
        merged = self.active_merged()
        if merged is None or merged.get("type") != "container": return
        seq = ["free", "stack_v", "stack_h", "grid"]
        # The on-disk fields are `layout` + optional `direction` for stack.
        cur_layout = (merged.get("layout") or "free").lower()
        cur_dir = (merged.get("direction") or "vertical").lower()
        if cur_layout == "stack":
            cur = "stack_h" if cur_dir == "horizontal" else "stack_v"
        else:
            cur = cur_layout if cur_layout in seq else "free"
        idx = seq.index(cur) if cur in seq else 0
        idx = (idx + delta) % len(seq)
        new = seq[idx]
        if new == "stack_v":
            self._set_field("layout", "stack")
            self._set_field("direction", "vertical")
        elif new == "stack_h":
            self._set_field("layout", "stack")
            self._set_field("direction", "horizontal")
        else:
            self._set_field("layout", new)

    # ---- text entry ------------------------------------------------------
    def text_subfields(self, merged):
        """Editable string fields for the active item in text mode. For
        text items: just "text". For menu items: both decor templates.
        For containers: "title" (the panel chip / chrome label)."""
        if merged is None:
            return ()
        kind = merged.get("type")
        if kind == "text":
            return ("text",)
        if kind == "menu":
            return ("selected_decor", "unselected_decor")
        if kind == "container":
            return ("title",)
        return ()

    def active_text_field(self):
        """The string-field name currently targeted by text-mode entry."""
        merged = self.active_merged()
        fields = self.text_subfields(merged)
        if not fields:
            return None
        field = getattr(self, "text_subfield", None)
        if field not in fields:
            field = fields[0]
            self.text_subfield = field
        return field

    def cycle_text_subfield(self):
        """Advance to the next subfield in text mode. Returns True if a
        new subfield is now active, False when we've fallen off the end
        (caller exits text mode). No wrap — Tab/SEL after the last
        subfield should exit, not loop back to the first."""
        merged = self.active_merged()
        fields = self.text_subfields(merged)
        if len(fields) <= 1:
            return False
        cur = getattr(self, "text_subfield", fields[0])
        i = fields.index(cur) if cur in fields else 0
        if i + 1 >= len(fields):
            return False
        self.text_subfield = fields[i + 1]
        self._flash(f"editing → {self.text_subfield}")
        return True

    def append_text(self, ch):
        field = self.active_text_field()
        if field is None: return
        merged = self.active_merged()
        cur = str(merged.get(field, ""))
        self._set_field(field, cur + ch)

    def pop_text(self):
        field = self.active_text_field()
        if field is None: return
        merged = self.active_merged()
        cur = str(merged.get(field, ""))
        if cur:
            self._set_field(field, cur[:-1])

    # ---- screen / item navigation ---------------------------------------
    def cycle_screen(self, delta):
        self.screen_idx = (self.screen_idx + delta) % len(SCREENS)
        self.item_idx = 0
        self.quit_armed = False
        # Drop the dive chain — it was pointing at containers on the
        # previous screen. Leaving it would make display_rows / active_*
        # resolve to children of an item that doesn't exist on this screen.
        self.container_stack = []

    def cycle_item(self, delta):
        rows = self.display_rows()
        if not rows:
            return
        self.item_idx = (self.item_idx + delta) % len(rows)
        self.quit_armed = False

    def cycle_mode(self):
        """transform ↔ details. Text editing is no longer a peer mode —
        it's a sub-state of details, toggled on text/menu items via Tab."""
        idx = MODES.index(self.mode)
        idx = (idx + 1) % len(MODES)
        self.mode = MODES[idx]
        # Exit any active text-edit when leaving details.
        if self.mode != "details":
            self.text_editing = False

    def begin_text_edit(self):
        """Enter text-edit sub-state on the active item, if it has any
        editable string subfields (text item: text; menu: decor templates)."""
        merged = self.active_merged()
        fields = self.text_subfields(merged)
        if not fields:
            return False
        self.text_subfield = fields[0]
        self.text_editing = True
        return True

    def end_text_edit(self):
        self.text_editing = False

    # ---- save / reload --------------------------------------------------
    def save(self):
        save_layout(self.layout)
        self.dirty = False
        self.quit_armed = False
        self._flash(f"saved {LAYOUT_PATH.name}", kind="saved")

    def reload(self):
        self.layout = load_layout()
        self.dirty = False
        self.quit_armed = False
        self.item_idx = 0
        self._flash("reloaded from disk")

    def request_quit(self):
        if self.dirty and not self.quit_armed:
            self.quit_armed = True
            self._flash("UNSAVED — press Esc again to discard, or End to save and quit",
                        ms=6000, kind="warn")
            return False
        return True

    def _flash(self, msg, ms=3000, kind="info"):
        """kind: 'info' (default, neutral), 'saved' (green), 'warn' (red).
        Default 3 s so per-action feedback stays on screen long enough to
        read between nudges."""
        self.flash_msg = msg
        self.flash_t = ms
        self.flash_kind = kind


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

ANCHOR_AX = {"tl":0,"t":0.5,"tr":1,"l":0,"c":0.5,"r":1,"bl":0,"b":0.5,"br":1}
ANCHOR_AY = {"tl":0,"t":0,"tr":0,"l":0.5,"c":0.5,"r":0.5,"bl":1,"b":1,"br":1}


def anchor_offset(anchor, w, h):
    ax = ANCHOR_AX.get(anchor, 0.0)
    ay = ANCHOR_AY.get(anchor, 0.0)
    return int(round(-w * ax)), int(round(-h * ay))


def _item_font(it, ed):
    """Editor-side equivalent of pewpew._resolve_layout_font — looks up
    the BitmapFont for an item honouring its font_family. Falls back
    to the integer-keyed 5x7 entry so old items keep working."""
    fam = (it.get("font_family") or "").strip()
    raw_scale = int(it.get("font", 3))
    if fam == "7x9":
        scale = max(1, min(4, raw_scale))
        return ed.fonts.get((fam, scale)) or ed.fonts.get(scale)
    scale = max(1, min(7, raw_scale))
    return ed.fonts.get(scale)


def item_bounds(it, ed):
    """Logical-coord (x, y, w, h) used to draw the selection box."""
    kind = it.get("type")
    x = int(it.get("x", 0))
    y = int(it.get("y", 0))
    if kind == "rect":
        return (x, y, max(1, int(it.get("w", 1))), max(1, int(it.get("h", 1))))
    if kind == "text":
        font = _item_font(it, ed)
        if font is None:
            return (x, y, 8, 8)
        # Account for {dpad} placeholder so the selection box wraps the
        # icon too — same math as _draw_text_with_dpad. Icon scale tracks
        # the 5x7-equivalent height so the cross stays inline visually.
        fam = (it.get("font_family") or "").strip()
        raw_scale = int(it.get("font", 3))
        if fam == "7x9":
            icon_scale = max(1, raw_scale + (raw_scale // 2))
        else:
            icon_scale = max(1, min(7, raw_scale))
        text = str(it.get("text", ""))
        # font.size() returns line_height (cap-height) for h, but the actual
        # rendered glyph surface includes the descender area below the
        # baseline (rows for p / g / q / y / j). Use full_height so the
        # bounding box wraps the entire rendered glyph — otherwise the
        # zoom-panel subsurface clips descenders and selection boxes don't
        # fully enclose the visible letters.
        full_h = getattr(font, "full_height", None) or font.get_height()
        if "{dpad}" in text:
            left, right = text.split("{dpad}", 1)
            lw, _ = font.size(left)
            rw, _ = font.size(right)
            icon_w = 7 * icon_scale
            w = lw + icon_w + rw
            h = max(full_h, 7 * icon_scale)
        else:
            w, _ = font.size(text)
            h = full_h
        ox, oy = anchor_offset(it.get("anchor", "tl"), w, h)
        return (x + ox, y + oy, max(1, w), max(1, h))
    if kind == "image":
        img = ed.sprite_lookup(it.get("sprite"))
        if img is None:
            return (x - 8, y - 8, 16, 16)
        w, h = img.get_size()
        scale = float(it.get("scale", 1.0))
        w = max(1, int(w * scale)); h = max(1, int(h * scale))
        ox, oy = anchor_offset(it.get("anchor", "tl"), w, h)
        return (x + ox, y + oy, w, h)
    if kind == "container":
        w = max(1, int(it.get("w", 1)))
        h = max(1, int(it.get("h", 1)))
        return (x, y, w, h)
    if kind == "progress_bar":
        w = max(1, int(it.get("w", 60)))
        h = max(1, int(it.get("h", 6)))
        return (x, y, w, h)
    if kind == "menu":
        # Match the renderer geometry exactly: each option centered (or
        # left/right anchored) at (x, y + i*line_h). The first option sits
        # on y, so vertical extent is (n-1)*line_h + font_h, top edge at
        # y - font_h/2. Use full_height so the bottom of the bounding box
        # reaches past the descender on the last menu line.
        font = _item_font(it, ed)
        opts = it.get("_preview_options") or ["Option"]
        line_h = int(it.get("line_height", 44))
        sel_decor = it.get("selected_decor") or ">  {opt}  <"
        unsel_decor = it.get("unselected_decor") or "   {opt}   "
        # Width = max(rendered width across both decor variants × all opts).
        widths = []
        for o in opts:
            widths.append(font.size(sel_decor.replace("{opt}", str(o)))[0])
            widths.append(font.size(unsel_decor.replace("{opt}", str(o)))[0])
        max_w = max(widths, default=80)
        font_h = getattr(font, "full_height", None) or font.get_height()
        n = max(1, len(opts))
        total_h = (n - 1) * line_h + font_h
        top = y - font_h // 2
        align = (it.get("align") or "center").lower()
        if align == "left":
            left = x
        elif align == "right":
            left = x - max_w
        else:
            left = x - max_w // 2
        return (left, top, max_w, total_h)
    return (x, y, 8, 8)


def render_preview(ed):
    """Compose the 640x480 preview surface: backdrop + every item (built-ins
    with overrides applied + user items) + grid lines for the active screen."""
    surf = pygame.Surface((SCREEN_W, SCREEN_H))
    bg = ed._backdrops.get(ed.current_screen)
    if bg is not None:
        surf.blit(bg, (0, 0))
    else:
        surf.fill(PREVIEW_BG)
        msg = ed.fonts[2].render(f"(no screenshot for {ed.current_screen})",
                                 False, DIM_INK)
        surf.blit(msg, msg.get_rect(center=(SCREEN_W // 2, SCREEN_H // 2)))

    if ed.show_grid:
        for x in range(0, SCREEN_W, 32):
            pygame.draw.line(surf, (40, 50, 70), (x, 0), (x, SCREEN_H), 1)
        for y in range(0, SCREEN_H, 32):
            pygame.draw.line(surf, (40, 50, 70), (0, y), (SCREEN_W, y), 1)

    sprite_assets = {n: ed.sprite_lookup(n) for n in ed.sprite_names}
    pp = ed._pewpew
    # Placeholder values for {score}, {best}, etc. in editor preview so
    # users can see what the interpolated text will look like in-game.
    # Mirrors what hud_draw passes at runtime so the editor preview shows
    # the HUD with realistic placeholder values. Extend when you add a
    # new {name} placeholder anywhere in a built-in spec.
    preview_vars = {
        # Generic
        "score": 12345, "best": 145600, "credits": 8240, "time": 25,
        # HUD chrome vars
        "level_short": "ASTEROID 3/9",
        "main_name": "RAIL GUN", "main_lvl": 3, "main_max": 5,
        "main_lvl_color": [240, 240, 240],
        "side_name": "HEATSEEKERS", "side_lvl": 1, "side_max": 3,
        "side_lvl_color": [240, 240, 240], "side_visible": True,
        "shield_lvl": 2, "shield_max": 5,
        "shield_lvl_color": [240, 240, 240],
        "engine_lvl": 2, "engine_max": 3,
        "engine_lvl_color": [240, 240, 240],
        "bombs": 3, "ability_name": "SCREEN CLEAR",
        # HUD dynamic vars
        "shield_ratio": 0.7,
        "ability_ready": True,
        "ability_cd_ratio": 1.0,
        "ability_cd_color": [255, 140, 40],
    }
    sox, soy = _screen_render_offset(ed.current_screen)
    for _kind, it in ed.all_items_merged():
        if sox or soy:
            it = dict(it)
            it["x"] = int(it.get("x", 0)) + sox
            it["y"] = int(it.get("y", 0)) + soy
        try:
            pp._layout_draw_item(surf, it, ed.fonts, sprite_assets, preview_vars)
        except Exception as e:
            print(f"preview draw {it.get('type')} failed: {e}")

    # Cross-dim the non-active strip on play/hud so the editable area
    # visually pops. Play has the playfield on the left (x: 0..PLAY_W),
    # HUD on the right (x: PLAY_W..SCREEN_W). Dim the OPPOSITE side.
    PLAY_W = 480
    if ed.current_screen == "play":
        dim = pygame.Surface((SCREEN_W - PLAY_W, SCREEN_H), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 190))
        surf.blit(dim, (PLAY_W, 0))
    elif ed.current_screen == "hud":
        dim = pygame.Surface((PLAY_W, SCREEN_H), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 190))
        surf.blit(dim, (0, 0))
    return surf


def draw_topbar(screen, ed, font, font_small):
    bar = pygame.Rect(0, 0, screen.get_width(), TOPBAR_H)
    # Mode-tinted background — at-a-glance cue for transform/details/
    # text-edit. Falls back to PANEL_BG for unknown modes.
    bg = TEXT_EDIT_TOPBAR_BG if ed.text_editing \
        else MODE_TOPBAR_BG.get(ed.mode, PANEL_BG)
    pygame.draw.rect(screen, bg, bar)
    pygame.draw.line(screen, BORDER, (0, TOPBAR_H - 1),
                     (screen.get_width(), TOPBAR_H - 1))

    label = font.render("LAYOUT EDITOR", False, ACCENT)
    screen.blit(label, (12, (TOPBAR_H - label.get_height()) // 2))

    x = label.get_width() + 32
    # Screen name with prev/next ghosts.
    prev_s = SCREENS[(ed.screen_idx - 1) % len(SCREENS)]
    next_s = SCREENS[(ed.screen_idx + 1) % len(SCREENS)]
    t = font_small.render(f"< {prev_s}  ", False, DIM_INK)
    screen.blit(t, (x, (TOPBAR_H - t.get_height()) // 2)); x += t.get_width()
    t = font.render(ed.current_screen.upper(), False, INK)
    screen.blit(t, (x, (TOPBAR_H - t.get_height()) // 2)); x += t.get_width()
    t = font_small.render(f"  {next_s} >", False, DIM_INK)
    screen.blit(t, (x, (TOPBAR_H - t.get_height()) // 2)); x += t.get_width()

    # Item counter (built-ins + user items merged).
    x += 28
    rows = ed.display_rows()
    idx_text = f"item {ed.item_idx + 1 if rows else 0}/{len(rows)}"
    t = font_small.render(idx_text, False, INK)
    screen.blit(t, (x, (TOPBAR_H - t.get_height()) // 2)); x += t.get_width()

    # Mode chip.
    x += 28
    chip_w = 150
    chip = pygame.Rect(x, 6, chip_w, TOPBAR_H - 12)
    pygame.draw.rect(screen, MODE_BG, chip)
    pygame.draw.rect(screen, BORDER, chip, 1)
    label_mode = f"mode: {ed.mode}"
    if ed.text_editing:
        label_mode += " · TXT"
    chip_color = ACCENT if ed.text_editing else INK
    t = font_small.render(label_mode, False, chip_color)
    screen.blit(t, t.get_rect(center=chip.center))
    # Carry indicator — chip immediately to the right when an item is
    # picked up for reparenting.
    if ed.carrying is not None:
        carry_label = f"CARRY [{ed.carrying.get('id') or ed.carrying.get('type') or '?'}]"
        cw = font_small.size(carry_label)[0] + 16
        cchip = pygame.Rect(chip.right + 8, 6, cw, TOPBAR_H - 12)
        pygame.draw.rect(screen, (90, 60, 30), cchip)
        pygame.draw.rect(screen, ACCENT, cchip, 1)
        ct = font_small.render(carry_label, False, ACCENT)
        screen.blit(ct, ct.get_rect(center=cchip.center))

    # Dirty indicator (right side).
    x = screen.get_width() - 12
    if ed.dirty:
        t = font.render("UNSAVED", False, DIRTY)
        screen.blit(t, (x - t.get_width(),
                        (TOPBAR_H - t.get_height()) // 2))


def draw_preview(screen, ed, preview_rect, font_small, preview_surf=None):
    """Render the 640×480 preview at exactly 2× integer scale (1280×960),
    centred in preview_rect. Falls back to the largest integer scale that
    fits when the window is too small for 2× (small laptop displays). The
    integer + nearest-neighbour scale keeps every game pixel crisp.

    `preview_surf` (optional) is the already-rendered 640×480 surface.
    Pass it in when both the main preview and the zoom panel render from
    the same source so we don't re-walk the layout tree twice per frame."""
    pygame.draw.rect(screen, (8, 10, 16), preview_rect)
    pygame.draw.rect(screen, BORDER, preview_rect, 1)

    preview = preview_surf if preview_surf is not None else render_preview(ed)
    sw, sh = preview.get_size()
    desired = ed.preview_scale or 2
    if sw * desired > preview_rect.w or sh * desired > preview_rect.h:
        # Fall back to the biggest int scale that fits (1× at minimum).
        scale = max(1, min(preview_rect.w // sw, preview_rect.h // sh))
    else:
        scale = desired
    dw, dh = sw * scale, sh * scale
    ox = preview_rect.x + (preview_rect.w - dw) // 2
    oy = preview_rect.y + (preview_rect.h - dh) // 2
    # pygame.transform.scale = nearest neighbour — exactly what integer
    # scale wants (no blurring from smoothscale).
    scaled = pygame.transform.scale(preview, (dw, dh))
    screen.blit(scaled, (ox, oy))

    # Stash the scaling for hit testing if needed later.
    ed._preview_xform = (ox, oy, scale)

    # Selection boxes (gizmos). R2+R3 toggles them off so the user can see
    # the layout exactly as it'll render in-game. When inside a container,
    # boxes show the container's children, offset by the container chain.
    if ed.show_gizmos:
        gpos = ed.active_global_offset()
        # gpos is None when an ancestor uses non-free layout — selection
        # boxes inside such a container are skipped (can't easily compute
        # the child's true rendered position without replicating engine).
        if gpos is not None:
            cgx, cgy = gpos
            for i, (kind, it) in enumerate(ed.current_level_items()):
                bx, by, bw, bh = item_bounds(it, ed)
                bx += cgx; by += cgy
                rx = ox + int(bx * scale)
                ry = oy + int(by * scale)
                rw = max(1, int(bw * scale))
                rh = max(1, int(bh * scale))
                is_active = (i == ed.item_idx)
                if is_active:
                    color = ACTIVE_OUTLINE
                    thick = 2
                else:
                    color = ((90, 130, 200) if kind == "builtin"
                             else OTHER_OUTLINE)
                    thick = 1
                pygame.draw.rect(screen, color,
                                 (rx - 1, ry - 1, rw + 2, rh + 2), thick)
                if is_active:
                    for cx_, cy_ in ((rx, ry), (rx + rw, ry),
                                     (rx, ry + rh), (rx + rw, ry + rh)):
                        pygame.draw.rect(screen, ACCENT, (cx_ - 2, cy_ - 2, 4, 4))
        # Always outline the current-container itself so the user knows
        # where they are — positioned via the ancestor chain + per-screen
        # render offset (so HUD nested containers land in the HUD strip).
        cont = ed.current_container()
        cpos = ed.current_container_screen_pos()
        if cont is not None and cpos is not None:
            cx_, cy_ = cpos
            cmerged = ed._merged_container(cont)
            cw_ = max(1, int(cmerged.get("w", 1)))
            ch_ = max(1, int(cmerged.get("h", 1)))
            rx = ox + int(cx_ * scale)
            ry = oy + int(cy_ * scale)
            rw = int(cw_ * scale)
            rh = int(ch_ * scale)
            pygame.draw.rect(screen, ACCENT,
                             (rx - 3, ry - 3, rw + 6, rh + 6), 2)

    # Caption strip under the preview.
    gz = "gizmos on" if ed.show_gizmos else "gizmos off (R2+R3)"
    cap = font_small.render(
        f"{SCREEN_W}x{SCREEN_H}  ({scale}x)  G grid  ·  {gz}",
        False, DIM_INK)
    screen.blit(cap, (preview_rect.x + 4, preview_rect.bottom + 4))


def draw_zoom_panel(screen, ed, zoom_rect, font_small, preview_surf=None):
    """Re-render the active item's region at the largest integer scale
    that fits zoom_rect, centred. Lets the user work on a small element
    without squinting at the 2× main preview. Uses the SAME 640×480
    preview surface as the main panel so we don't walk the layout twice."""
    pygame.draw.rect(screen, (8, 10, 16), zoom_rect)
    pygame.draw.rect(screen, BORDER, zoom_rect, 1)

    merged = ed.active_merged()
    if merged is None:
        msg = font_small.render("(no selection)", False, DIM_INK)
        screen.blit(msg, msg.get_rect(center=zoom_rect.center))
        return

    bx, by, bw, bh = item_bounds(merged, ed)
    gpos = ed.active_global_offset()
    if gpos is None:
        gpos = _screen_render_offset(ed.current_screen)
    bx += gpos[0]; by += gpos[1]

    # Pad so the user sees a bit of surrounding context.
    pad = 12
    bx -= pad; by -= pad
    bw += pad * 2; bh += pad * 2
    # Clip to the preview surface bounds.
    bx = max(0, bx); by = max(0, by)
    if bx >= SCREEN_W or by >= SCREEN_H:
        return
    bw = max(1, min(bw, SCREEN_W - bx))
    bh = max(1, min(bh, SCREEN_H - by))

    src = preview_surf if preview_surf is not None else render_preview(ed)
    region = src.subsurface(pygame.Rect(bx, by, bw, bh)).copy()
    int_scale = min(zoom_rect.w // bw, zoom_rect.h // bh)
    if int_scale >= 1:
        # Fits at integer scale — nearest-neighbour keeps pixels crisp.
        sw, sh = bw * int_scale, bh * int_scale
        scaled = pygame.transform.scale(region, (sw, sh))
        scale_label = f"{int_scale}×"
    else:
        # Region is bigger than the band even at 1×. Fall back to a float
        # fit (smoothscale to soften the down-sample).
        fit = min(zoom_rect.w / bw, zoom_rect.h / bh)
        sw, sh = max(1, int(bw * fit)), max(1, int(bh * fit))
        scaled = pygame.transform.smoothscale(region, (sw, sh))
        scale_label = f"{fit:.2f}×"
    ox = zoom_rect.x + (zoom_rect.w - sw) // 2
    oy = zoom_rect.y + (zoom_rect.h - sh) // 2
    screen.blit(scaled, (ox, oy))

    cap_text = (f"zoom {ed.active_merged().get('id', '?')}  "
                f"{bx},{by}  {bw}×{bh} @ {scale_label}")
    cap = font_small.render(cap_text, False, DIM_INK)
    screen.blit(cap, (zoom_rect.x + 6, zoom_rect.y + 4))


def draw_panel(screen, ed, panel_rect, font, font_small, font_tiny,
               font_mid=None):
    pygame.draw.rect(screen, PANEL_BG, panel_rect)
    pygame.draw.rect(screen, BORDER, panel_rect, 1)

    px, py = panel_rect.x + 10, panel_rect.y + 10
    width = panel_rect.w - 20

    # No more sibling list here — the hierarchy panel to the left covers
    # that. Keep `list_rects` empty so old mouse-click handlers no-op.
    ed.list_rects = []

    # The bottom hint block has a known height — reserve room for it up
    # front so the property block never overlaps the hints. Row height
    # matches the hint font (7x9 mid = 11 px / 5x7 small fallback = 16 px).
    _hint_font = font_mid if font_mid is not None else font_small
    hint_h_reserve = _hint_font.get_height() + 2
    hint_block_h = len(_hints_for_selection(ed)) * hint_h_reserve + 14
    bottom_limit = panel_rect.bottom - hint_block_h - 6

    # ===== Active item properties =======================================
    it = ed.active_merged()
    if it is None:
        msg = font_small.render("no active item", False, DIM_INK)
        screen.blit(msg, (px, py))
    else:
        _, kind, builtin = ed.active_handle(create=False)
        tag = "[built-in]" if kind == "builtin" else "[user]"
        prop_title = font.render(
            f"{it.get('type','?')} - {it.get('id','?')}  {tag}", False, INK)
        screen.blit(prop_title, (px, py)); py += prop_title.get_height() + 4
        if builtin is not None and builtin.get("_label"):
            note = font_tiny.render(builtin["_label"], False, DIM_INK)
            screen.blit(note, (px, py)); py += note.get_height() + 4

        # Value-column x: wider than the SysFont default so 11-12 char
        # labels (border width / panel skin / line height) don't collide
        # with the value at 10px-advance BitmapFont scale=2.
        VALUE_X = 160

        def row(label, value, color=INK):
            nonlocal py
            # Stop rendering once we'd cross into the bottom hint block.
            if py + font_small.get_height() > bottom_limit:
                return
            ll = font_small.render(label, False, DIM_INK)
            vv = font_small.render(str(value), False, color)
            screen.blit(ll, (px, py))
            screen.blit(vv, (px + VALUE_X, py))
            py += ll.get_height() + 2

        def color_row(label, raw):
            """Render a colour-valued field. Supports literal [r,g,b]
            lists (rendered with a colour swatch) and `"{name}"` template
            references (rendered as the template string in accent — the
            engine resolves these at draw time, the editor can't preview)."""
            if isinstance(raw, str) and raw.startswith("{") and raw.endswith("}"):
                row(label, raw, color=ACCENT)
                return
            if not raw:
                row(label, "—", color=DIM_INK)
                return
            try:
                c = (int(raw[0]), int(raw[1]), int(raw[2]))
            except (TypeError, ValueError, IndexError):
                row(label, str(raw)[:18], color=DIM_INK)
                return
            row(label, f"{c[0]},{c[1]},{c[2]}", color=c)

        row("x", it.get("x", 0))
        row("y", it.get("y", 0))
        type_ = it.get("type")
        if type_ == "rect":
            row("w", it.get("w", 0))
            row("h", it.get("h", 0))
            row("outline", it.get("outline", 0))
            color_row("color", it.get("color"))
            row("alpha", it.get("alpha", 255))
        elif type_ == "text":
            row("text", repr(it.get("text", ""))[:28])
            row("font scale", it.get("font", 3))
            row("font family", (it.get("font_family") or "") or "5x7")
            row("anchor", it.get("anchor", "tl"))
            color_row("color", it.get("color"))
            row("alpha", it.get("alpha", 255))
            row("shadow", "on" if it.get("shadow") else "off")
            if it.get("blink") is not None:
                row("blink", "on" if it.get("blink") else "off")
        elif type_ == "image":
            row("sprite", it.get("sprite", "?"))
            row("scale", round(float(it.get("scale", 1.0)), 3))
            row("anchor", it.get("anchor", "tl"))
            row("alpha", it.get("alpha", 255))
        elif type_ == "menu":
            row("font scale", it.get("font", 3))
            row("font family", (it.get("font_family") or "") or "5x7")
            row("line height", it.get("line_height", 44))
            row("align", it.get("align", "center"))
            color_row("color", it.get("color"))
            color_row("sel color", it.get("selected_color"))
            row("alpha", it.get("alpha", 255))
            row("sel decor",   repr(it.get("selected_decor", ""))[:24])
            row("unsel decor", repr(it.get("unselected_decor", ""))[:24])
        elif type_ == "progress_bar":
            row("w", it.get("w", 60))
            row("h", it.get("h", 6))
            row("value", it.get("value", 0))
            row("max", it.get("max", 1.0))
            row("segments", it.get("segments", 10))
            color_row("color", it.get("color"))
            color_row("bg color", it.get("bg_color"))
            row("alpha", it.get("alpha", 255))
        elif type_ == "container":
            row("w", it.get("w", 0))
            row("h", it.get("h", 0))
            row("layout", it.get("layout", "free"))
            if (it.get("layout") or "free").lower() == "stack":
                row("direction", it.get("direction", "vertical"))
                row("gap", it.get("gap", 0))
            elif (it.get("layout") or "free").lower() == "grid":
                row("rows", it.get("rows", 1))
                row("cols", it.get("cols", 1))
                row("gap_x", it.get("gap_x", 0))
                row("gap_y", it.get("gap_y", 0))
            row("padding", it.get("padding", 0))
            # Resolved chrome — merges the panel_skin defaults with any
            # explicit field overrides. The user sees what'll actually render.
            chrome = ed._pewpew._container_chrome(it)
            skin = int(it.get("panel_skin", 0))
            n_skins = len(ed._pewpew._PANEL_SKINS)
            row("panel skin", f"{skin}/{n_skins - 1}  (K cycles)",
                color=ACCENT if skin else DIM_INK)
            color_row("bg", chrome.get("bg"))
            color_row("border", chrome.get("border"))
            row("border width", chrome.get("border_width", 0))
            if chrome.get("caps"):
                row("caps", "on", color=ACCENT)
            # Title is always shown so the user can see they're about to
            # add one when entering text-edit (Tab) on a title-less container.
            t = it.get("title", "")
            row("title", repr(t)[:24] if t else "(none)",
                color=ACCENT if ed.text_editing else INK)
            row("alpha", it.get("alpha", 255))
            row("children", len(it.get("children") or []))
        # Active text-edit subfield (shown so the user knows which decor
        # template typing will modify when in text-edit on a menu item).
        if ed.text_editing:
            sub = ed.active_text_field()
            if sub:
                row("editing →", sub, color=ACCENT)

    # ===== Mode hints (bottom of panel) =================================
    # Both label and description use the 7x9 font_mid — the row's
    # natural mid-size, sized so 10-char labels (R2+START / Shift+1..8)
    # fit in the label column with room to spare for descriptions.
    # 7x9 scale 1: 8 px advance, 9 px line height.
    hint_font = font_mid if font_mid is not None else font_small
    hints = _hints_for_selection(ed)
    hint_h = hint_font.get_height() + 2
    # Label column up to 10 chars (R2+START / Shift+1..8 at 8 px advance
    # = 80 px) + 8 px panel padding + 8 px gap → description at +96.
    DESC_X = panel_rect.x + 96
    hint_y = panel_rect.bottom - (len(hints) * hint_h + 10)
    pygame.draw.line(screen, BORDER, (panel_rect.x + 6, hint_y - 4),
                     (panel_rect.right - 6, hint_y - 4))
    # Mode-only labels share the topbar tint so the eye can match the
    # active mode at a glance. Text-edit substate wins over the base
    # mode colour.
    if ed.text_editing:
        mode_color = TEXT_EDIT_LABEL_COLOR
    else:
        mode_color = MODE_LABEL_COLOR.get(ed.mode, ACCENT)
    for label, descr, group in hints:
        if group == "sep":
            pygame.draw.line(screen, BORDER,
                             (panel_rect.x + 12, hint_y + hint_h // 2),
                             (panel_rect.right - 12, hint_y + hint_h // 2))
            hint_y += hint_h
            continue
        label_color = mode_color if group == "mode" else ACCENT
        l = hint_font.render(label, False, label_color)
        d = hint_font.render(descr, False, DIM_INK)
        screen.blit(l, (panel_rect.x + 8, hint_y))
        screen.blit(d, (DESC_X, hint_y))
        hint_y += hint_h


def _hints_for_selection(ed):
    """Selection-aware hint list. Returns 3-tuples (label, descr, group):
      "global"  — nav / mode / screen / save / quit  (mode-independent)
      "mode"    — current mode only (filtered by ed.mode + item type)
      "r2"      — R2 chords (always; carry / grid blocks when active)
      "kb"      — keyboard-only (no gamepad equivalent)
      "sep"     — separator row, drawn as a horizontal line

    Group is used by the renderer to tint mode-only labels with the
    current mode's hue. Labels prefer the gamepad button name; kb
    labels appear only when there's no gamepad binding."""
    SEP = ("---", "", "sep")
    merged = ed.active_merged()
    kind = merged.get("type") if merged else None

    def tagged(rows, group):
        return [(lab, dsc, group) for (lab, dsc) in rows]

    # ---------- GLOBAL (no chord) ----------
    glob = [
        ("SEL",       "cycle mode"),
        ("START",     "next screen"),
        ("LB / RB",   "prev / next sibling"),
        ("R3",        "dive into container"),
        ("L3",        "up to parent"),
        ("RS",        "nav  lr=dive  ud=sibling"),
    ]

    # ---------- CURRENT MODE ----------
    mode_block = []
    if ed.mode == "transform":
        mode_block.append(("DP", "move"))
        wasd_descr = {
            "text":         "font + (any direction)",
            "rect":         "X/B = w   Y/A = h",
            "image":        "scale +",
            "menu":         "X/B = font   Y/A = line_h",
            "progress_bar": "X/B = w   Y/A = h",
            "container":    "X/B = w   Y/A = h",
        }.get(kind)
        if wasd_descr:
            mode_block.append(("X B Y A", wasd_descr))
    else:    # details
        dp_descr = {
            "text":  "l/r h-anchor  u/d v-anchor",
            "image": "l/r h-anchor  u/d v-anchor",
            "menu":  "l/r align  (u/d no-op)",
        }.get(kind)
        if dp_descr:
            mode_block.append(("DP", dp_descr))
        xb_descr = {
            "text":         "color cycle",
            "rect":         "color cycle",
            "image":        "sprite cycle",
            "menu":         "color cycle",
            "progress_bar": "color cycle",
            "container":    "bg color cycle",
        }.get(kind)
        if xb_descr:
            mode_block.append(("X / B", xb_descr))
        ya_descr = {
            "text":         "font +",
            "rect":         "outline +",
            "image":        "scale +",
            "menu":         "font +",
            "progress_bar": "segments +",
            "container":    "layout cycle",
        }.get(kind)
        if ya_descr:
            mode_block.append(("Y / A", ya_descr))
        if ed.text_subfields(merged):
            sub_descr = {
                "text":      "edit text",
                "menu":      "edit decor",
                "container": "edit title",
            }.get(kind, "edit")
            mode_block.append(("SEL", f"type to {sub_descr}"))

    # ---------- R2 CHORDS ----------
    r2 = [
        ("R2+LB",    "pick up (cut)"),
        ("R2+RB",    "drop (paste)"),
        ("R2+L3",    "duplicate"),
        ("R2+R3",    "toggle gizmos"),
        ("R2+SEL",   "quit"),
        ("R2+START", "save"),
    ]
    if ed.carrying is not None:
        r2.append(("R2+DP",   "carry: navigate hierarchy"))
        r2.append(("R2+XBYA", "X=disc B=can Y=wrap A=drop"))
        r2.append(("R2+L3",   "drop COPY (keep carrying)"))
    if (kind == "container"
            and (merged.get("layout") or "free").lower() == "grid"):
        r2.append(("R2+X/B",  "(grid) cols -/+"))
        r2.append(("R2+Y/A",  "(grid) rows -/+"))
        r2.append(("R2+DP",   "(grid) gap_x  gap_y"))

    # ---------- KEYBOARD-ONLY ----------
    kb = [
        ("N M I B C",  "add text / rect / image / bar / cont"),
        ("Del",        "delete user / reset built-in"),
        ("G",          "toggle preview grid"),
    ]
    if kind == "text":
        kb.append(("H", "toggle shadow"))
    if kind == "container":
        kb.append(("K", "cycle panel skin"))
    kb.append(("1..8", "color preset"))
    if kind == "menu":
        kb.append(("Shift+1..8", "sel_color preset"))

    out = list(tagged(glob, "global"))
    if mode_block:
        out.append(SEP)
        out.extend(tagged(mode_block, "mode"))
    out.append(SEP)
    out.extend(tagged(r2, "r2"))
    out.append(SEP)
    out.extend(tagged(kb, "kb"))
    return out


def draw_tree_panel(screen, ed, tree_rect, font, font_small, font_tiny):
    """Hierarchy panel between preview and info panel. Shows every item
    in the layout tree, fully expanded, with depth indentation. Marks the
    active item with a chevron and the current navigation container with
    an accent dot."""
    pygame.draw.rect(screen, PANEL_BG, tree_rect)
    pygame.draw.rect(screen, BORDER, tree_rect, 1)

    px, py = tree_rect.x + 8, tree_rect.y + 8
    breadcrumbs = " ▸ ".join(ed.container_path_labels())
    title = font.render("HIERARCHY", False, INK)
    screen.blit(title, (px, py)); py += title.get_height() + 2
    bc = font_tiny.render(breadcrumbs, False, ACCENT)
    screen.blit(bc, (px, py)); py += bc.get_height() + 6

    list_clip = pygame.Rect(px, py, tree_rect.w - 16, tree_rect.bottom - py - 8)
    pygame.draw.rect(screen, (12, 14, 22), list_clip)
    pygame.draw.rect(screen, BORDER, list_clip, 1)

    rows = ed.full_tree_rows()
    row_h = 16
    inner_y = list_clip.y + 4
    visible_rows = max(1, (list_clip.h - 8) // row_h)
    # Scroll so active row stays visible.
    active_row_idx = next((i for i, r in enumerate(rows) if r[3]), 0)
    scroll = max(0, active_row_idx - visible_rows // 2)
    scroll = min(scroll, max(0, len(rows) - visible_rows))
    for vi, idx in enumerate(range(scroll, min(len(rows), scroll + visible_rows))):
        depth, kind, item, is_active, is_cur_container = rows[idx]
        rr = pygame.Rect(list_clip.x + 1, inner_y + vi * row_h,
                         list_clip.w - 2, row_h)
        if is_active:
            pygame.draw.rect(screen, LIST_HIGHLIGHT, rr)
        ind = "  " * depth
        # Marker precedence (only set when applicable so leaves stay clean
        # — active items get the row highlight regardless of marker):
        #   active container  ▸
        #   current container ●
        #   diveable container >
        #   leaf              blank
        is_container = item.get("type") == "container"
        if is_active and is_container:
            marker = "▸"
        elif is_cur_container:
            marker = "●"
        elif is_container:
            marker = ">"
        else:
            marker = " "
        ident = item.get("id") or "?"
        type_ = item.get("type") or "?"
        tag = "B" if kind == "builtin" else ("C" if type_ == "container" else "U")
        line = f"{ind}{marker} [{tag}] {ident[:14]:<14} {type_}"
        if type_ == "container":
            n = len(item.get("children") or [])
            line += f"  ({n})"
        col = INK if is_active else (
            ACCENT if is_cur_container
            else ((180, 210, 255) if kind == "builtin" else DIM_INK))
        t = font_small.render(line, False, col)
        screen.blit(t, (rr.x + 4, rr.y + (row_h - t.get_height()) // 2))


def draw_status(screen, ed, font_small):
    bar_y = screen.get_height() - STATUS_H
    bar = pygame.Rect(0, bar_y, screen.get_width(), STATUS_H)
    pygame.draw.rect(screen, PANEL_BG, bar)
    pygame.draw.line(screen, BORDER, (0, bar_y), (screen.get_width(), bar_y))
    if ed.flash_t > 0 and ed.flash_msg:
        kind = getattr(ed, "flash_kind", "info")
        if kind == "warn":
            color = DIRTY
        elif kind == "saved":
            color = SAVED
        else:
            color = INK
        t = font_small.render(ed.flash_msg, False, color)
        screen.blit(t, (12, bar_y + (STATUS_H - t.get_height()) // 2))
    else:
        if ed.text_editing:
            help_text = ("type to edit  |  Tab/SEL = cycle subfield / exit "
                         " |  Enter = exit  |  Backspace = del char")
        elif ed.mode == "transform":
            help_text = ("arrows = pos  |  WASD = size  |  R2+: grid cell/gap"
                         "  |  Shift = x5")
        else:   # details
            help_text = ("arrows = anchor  |  X/B = color  |  Y/A = font/scale"
                         "  |  Tab = type text")
        t = font_small.render(help_text, False, DIM_INK)
        screen.blit(t, (12, bar_y + (STATUS_H - t.get_height()) // 2))


# ---------------------------------------------------------------------------
# Input handlers
# ---------------------------------------------------------------------------

# Per-mode arrow/WASD action tables.
ARROW_KEYS = (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN)
WASD_KEYS = (pygame.K_a, pygame.K_d, pygame.K_w, pygame.K_s)

MODE_ARROWS = {
    # transform = move x/y.  details = anchor (L/R = h, U/D = v).
    # menu has no v-anchor → details U/D no-ops for menu.
    "transform": ("pos_left", "pos_right", "pos_up", "pos_down"),
    "details":   ("anchor_h_prev", "anchor_h_next", "anchor_v_prev", "anchor_v_next"),
}
MODE_WASD = {
    # transform face buttons (A/D w ±, W/S h ±, top-left anchored). Per-type
    # branches inside _nudge_size handle image scale, menu font/line_height,
    # text font, etc.
    "transform": ("size_w_dec", "size_w_inc", "size_h_dec", "size_h_inc"),
    # details face buttons:
    #   X/B (A/D)  → primary "what" — color cycle for most types; sprite
    #                cycle for image; bg cycle for container.
    #   Y/A (W/S)  → secondary — font ± for text/menu, scale ± for image,
    #                outline ± for rect, segments ± for progress_bar,
    #                layout cycle for container.
    "details":   ("details_x_dec", "details_x_inc", "details_y_dec", "details_y_inc"),
}


def _arrow_action(mode, k):
    try:
        return MODE_ARROWS[mode][ARROW_KEYS.index(k)]
    except (ValueError, IndexError):
        return None


def _wasd_action(mode, k):
    try:
        return MODE_WASD[mode][WASD_KEYS.index(k)]
    except (ValueError, IndexError):
        return None


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
        return True

    # Text-edit sub-state: typing fills the active subfield; arrow keys
    # mirror the gamepad D-pad anchor mapping; Tab cycles subfield (or
    # exits if there are no more); Enter exits; Esc handled above.
    if ed.text_editing:
        if k == pygame.K_TAB:
            # Cycle subfield; once all subfields are seen, exit text-edit
            # AND cycle mode so a follow-up Tab doesn't just re-enter
            # text-edit on this same text item.
            if not ed.cycle_text_subfield():
                ed.end_text_edit()
                ed.cycle_mode()
            return True
        if k == pygame.K_RETURN:
            ed.end_text_edit(); return True
        if k == pygame.K_BACKSPACE:
            ed.pop_text(); return True
        if k in ARROW_KEYS:
            action = _arrow_action(ed.mode, k)
            if action:
                ed.start_action(("kb", k), action)
            return True
        ch = evt.unicode
        if ch and ch.isprintable():
            ed.append_text(ch)
        return True

    ed.quit_armed = False

    if ctrl and k == pygame.K_r:
        ed.reload(); return True
    if k == pygame.K_TAB:
        # In details on a text/menu item, Tab BEGINS text editing instead
        # of cycling mode — the user explicitly opts into typing. Anywhere
        # else, Tab cycles mode.
        if ed.mode == "details" and ed.begin_text_edit():
            return True
        ed.cycle_mode(); return True
    if k == pygame.K_SEMICOLON:
        ed.start_action(("kb", k), "screen_prev"); return True
    if k == pygame.K_QUOTE:
        ed.start_action(("kb", k), "screen_next"); return True
    if k == pygame.K_LEFTBRACKET:
        ed.start_action(("kb", k), "item_prev"); return True
    if k == pygame.K_RIGHTBRACKET:
        ed.start_action(("kb", k), "item_next"); return True
    # One-shot actions (no auto-repeat).
    if k == pygame.K_n:
        ed.apply_action("add_text"); return True
    if k == pygame.K_m:
        ed.apply_action("add_rect"); return True
    if k == pygame.K_i:
        ed.apply_action("add_image"); return True
    if k == pygame.K_b:
        ed.apply_action("add_progress_bar"); return True
    if k == pygame.K_t:
        ed.apply_action("add_tiered_bar"); return True
    if k == pygame.K_c:
        ed.apply_action("add_container"); return True
    if k == pygame.K_PERIOD:
        ed.apply_action("dive"); return True
    if k == pygame.K_COMMA:
        ed.apply_action("up"); return True
    if k == pygame.K_p:
        ed.apply_action("duplicate"); return True
    if k in (pygame.K_DELETE, pygame.K_BACKSPACE):
        ed.apply_action("delete"); return True
    if k == pygame.K_g:
        ed.apply_action("toggle_grid"); return True
    if k == pygame.K_h:
        # Toggle shadow on text items.
        ed.apply_action("shadow_toggle"); return True
    if k == pygame.K_x:
        ed.apply_action("pick_up"); return True
    if k == pygame.K_v:
        ed.apply_action("drop"); return True
    if k == pygame.K_k:
        # Cycle container panel_skin (0 = none, 1 = HUD panel, ...).
        action = ("panel_skin_prev" if (mods & pygame.KMOD_SHIFT)
                  else "panel_skin_next")
        ed.apply_action(action); return True
    # 1..8 palette presets. Shift+1..8 sets menu selected_color instead.
    if pygame.K_1 <= k <= pygame.K_8:
        idx = k - pygame.K_1
        if mods & pygame.KMOD_SHIFT:
            ed.set_selected_palette(idx)
        else:
            ed.set_palette(idx)
        return True

    if k in ARROW_KEYS:
        action = _arrow_action(ed.mode, k)
        if action:
            ed.start_action(("kb", k), action)
        return True
    if k in WASD_KEYS:
        action = _wasd_action(ed.mode, k)
        if action:
            ed.start_action(("kb", k), action)
        return True
    return True


def handle_key_up(ed, evt):
    ed.stop_action(("kb", evt.key))


def update_right_stick(ed):
    """Poll the right analog stick and fire one action per threshold
    cross (the quantized state debounces so a single push fires once
    even with the stick held).
      RS right  → dive into active container
      RS left   → up to parent
      RS down   → next sibling
      RS up     → previous sibling"""
    if not ed.gamepad:
        return
    try:
        rx = ed.gamepad.get_axis(JA_RSX)
        ry = ed.gamepad.get_axis(JA_RSY)
    except Exception:
        return
    qx = 1 if rx > RSTICK_THRESH else (-1 if rx < -RSTICK_THRESH else 0)
    qy = 1 if ry > RSTICK_THRESH else (-1 if ry < -RSTICK_THRESH else 0)
    pqx, pqy = ed._rstick_prev
    if qx != pqx and qx != 0:
        ed.apply_action("dive" if qx > 0 else "up")
    if qy != pqy and qy != 0:
        ed.apply_action("item_next" if qy > 0 else "item_prev")
    ed._rstick_prev = (qx, qy)


def update_gamepad_modifiers(ed):
    """Poll L2/R2 each frame. On R2 transition, re-dispatch the current
    D-pad state so the grid-chord actions swap in/out for grid containers
    without the user having to release-and-repress the D-pad. Also: when
    R2 is released and we're carrying an item that was picked up via R2,
    auto-drop into the current container (the gesture model — pickup on
    R2+LB press, navigate, release R2 to drop)."""
    if not ed.gamepad:
        return
    try:
        prev_r2 = ed.modifier_r2
        ed.modifier_l2 = ed.gamepad.get_axis(JA_LT) > TRIGGER_THRESHOLD
        ed.modifier_r2 = ed.gamepad.get_axis(JA_RT) > TRIGGER_THRESHOLD
    except Exception:
        ed.modifier_l2 = ed.modifier_r2 = False
        return
    if prev_r2 != ed.modifier_r2:
        # R2 released while carrying via the R2 gesture → drop at current
        # container. Do this BEFORE the D-pad re-dispatch so the drop
        # happens before any state-resolution.
        if prev_r2 and not ed.modifier_r2 and ed.carrying is not None \
                and ed.carry_via_r2:
            ed.drop()
        try:
            hat = ed.gamepad.get_hat(0)
        except Exception:
            return
        for a in _ALL_DPAD_ACTIONS:
            ed.stop_action(("gp_hat", a))

        class _SyntheticHat:
            __slots__ = ("value",)
            def __init__(self, v): self.value = v
        handle_joy_hat(ed, _SyntheticHat(hat))


def _gp_face_action(mode, btn):
    """X/B/Y/A on the pad → matching WASD action for the current mode."""
    wasd = MODE_WASD.get(mode, (None,) * 4)
    mapping = {JB_X: wasd[0], JB_B: wasd[1], JB_Y: wasd[2], JB_A: wasd[3]}
    return mapping.get(btn)


# R2 + face on a grid container → cell add/remove (X/B = cols, Y/A = rows).
_R2_GRID_FACE = {
    JB_X: "grid_cols_dec", JB_B: "grid_cols_inc",
    JB_Y: "grid_rows_dec", JB_A: "grid_rows_inc",
}
# R2 + D-pad on a grid container → gap nudge.
_R2_GRID_HAT = ("gap_x_dec", "gap_x_inc", "gap_y_dec", "gap_y_inc")


def handle_joy_button_down(ed, evt):
    ed.quit_armed = False
    btn = evt.button
    key = ("gp_btn", btn)
    # SELECT cycles modes; START cycles screens. R2-chorded they become
    # quit / save (save sits under the louder START button so it's harder
    # to hit accidentally). R2+R3 toggles gizmo visibility in the preview.
    if btn == JB_BACK:           # SELECT
        if ed.modifier_r2:
            if not ed.request_quit():
                return
            pygame.event.post(pygame.event.Event(pygame.QUIT))
            return
        # Mirror keyboard Tab: while text_editing, cycle subfield or exit;
        # in details mode on an item with editable text, begin text-edit;
        # otherwise cycle mode. Lets gamepad-only users enter the title /
        # text edit flow (typing characters still needs a keyboard).
        # When subfields are exhausted, we *also* cycle mode — otherwise
        # SELECT would just re-enter text-edit on the next press and the
        # user could never get back to transform mode on a text item.
        if ed.text_editing:
            if not ed.cycle_text_subfield():
                ed.end_text_edit()
                ed.cycle_mode()
            return
        if ed.mode == "details" and ed.begin_text_edit():
            return
        ed.start_action(key, "mode_cycle")
    elif btn == JB_START:
        if ed.modifier_r2:
            ed.save()
            return
        ed.start_action(key, "screen_next")
    elif btn == JB_LB:
        # R2 chord: pick up the active item (cut). Else: prev sibling.
        if ed.modifier_r2:
            ed.apply_action("pick_up")
            return
        ed.start_action(key, "item_prev")
    elif btn == JB_RB:
        # R2 chord: explicit drop (also fires automatically on R2 release
        # when the carry started via R2). Else: next sibling.
        if ed.modifier_r2:
            ed.apply_action("drop")
            return
        ed.start_action(key, "item_next")
    elif btn == JB_LSB:
        # L3: pop up to parent container. R2 chord = duplicate (or, if
        # currently carrying, drop a copy and keep carrying the original).
        if ed.modifier_r2:
            if ed.carrying is not None:
                ed.apply_action("carry_drop_copy")
            else:
                ed.apply_action("duplicate")
            return
        ed.apply_action("up")
    elif btn == JB_RSB:
        # R3: dive into the active container (R2 chord = toggle gizmos).
        if ed.modifier_r2:
            ed.show_gizmos = not ed.show_gizmos
            ed._flash(f"gizmos {'on' if ed.show_gizmos else 'off'}")
            return
        ed.apply_action("dive")
    elif btn in (JB_X, JB_B, JB_Y, JB_A):
        if ed.modifier_r2 and ed.carrying is not None:
            # Carry-time chord: act on the picked-up item.
            #   X = discard   B = cancel (restore origin)
            #   Y = wrap      A = drop (MOVE — same as releasing R2)
            # Copy now lives on R2+L3 (the "duplicate" button when not
            # carrying) so A keeps its universal "confirm" meaning.
            action = {
                JB_X: "carry_discard",
                JB_B: "carry_cancel",
                JB_Y: "carry_wrap",
                JB_A: "drop",
            }.get(btn)
            if action:
                ed.apply_action(action)
            return
        if ed.modifier_r2 and ed._active_grid_container()[0] is not None:
            action = _R2_GRID_FACE.get(btn)
        else:
            action = _gp_face_action(ed.mode, btn)
        if action:
            ed.start_action(key, action)


def handle_joy_button_up(ed, evt):
    ed.stop_action(("gp_btn", evt.button))


_ALL_DPAD_ACTIONS = (
    "pos_left", "pos_right", "pos_up", "pos_down",
    "style_left", "style_right", "style_up", "style_down",
    "anchor_h_prev", "anchor_h_next", "anchor_v_prev", "anchor_v_next",
    "gap_x_dec", "gap_x_inc", "gap_y_dec", "gap_y_inc",
)


def handle_joy_hat(ed, evt):
    hx, hy = evt.value
    ed.quit_armed = False
    # Carry-time D-pad nav: fire each navigation step once per hat
    # motion event (no auto-repeat — don't want to spam dive). Then
    # bail before the regular held-key dispatch.
    if ed.modifier_r2 and ed.carrying is not None:
        # Pygame fires one event per hat-value change, so each push
        # produces one apply_action call here.
        if hx < 0: ed.apply_action("up")
        elif hx > 0: ed.apply_action("dive")
        if hy > 0: ed.apply_action("item_prev")
        elif hy < 0: ed.apply_action("item_next")
        return
    # Priority for D-pad meaning while R2 held (without a carry):
    #   1. grid container active → gap adjust
    #   2. fall back to current mode's D-pad mapping
    if (ed.modifier_r2
            and ed._active_grid_container()[0] is not None):
        actions = _R2_GRID_HAT
    else:
        actions = MODE_ARROWS.get(ed.mode, (None,) * 4)
    desired = set()
    if actions[0] and hx < 0: desired.add(actions[0])
    if actions[1] and hx > 0: desired.add(actions[1])
    if actions[2] and hy > 0: desired.add(actions[2])
    if actions[3] and hy < 0: desired.add(actions[3])
    for a in _ALL_DPAD_ACTIONS:
        key = ("gp_hat", a)
        if key in ed.held and a not in desired:
            ed.stop_action(key)
    for a in desired:
        key = ("gp_hat", a)
        if key not in ed.held:
            ed.start_action(key, a)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    # On Windows, Python defaults to DPI-unaware so SDL sees the
    # downscaled desktop resolution (e.g. 1280x720 instead of 1920x1080)
    # and the editor renders smaller than the actual screen. Opt in to
    # per-monitor DPI awareness BEFORE pygame.init() so it queries the
    # real resolution. No-op on other platforms.
    import sys, ctypes
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    pygame.init()
    # (0, 0) tells pygame to use the current desktop resolution.
    # Combined with NOFRAME, the editor covers the whole screen
    # borderless without taking exclusive fullscreen.
    screen = pygame.display.set_mode((0, 0), pygame.NOFRAME)
    global WIN_W, WIN_H
    WIN_W, WIN_H = screen.get_size()
    pygame.display.set_caption("Pewpew layout editor")
    pygame.key.set_repeat()   # we handle repeat ourselves
    # Editor chrome uses the same hand-pixeled fonts as the game.
    # Local import — pewpew expects pygame.display already up (matches
    # the pattern Editor.__init__ uses).
    import pewpew
    font       = pewpew.BitmapFont(scale=2)        # headings (10x14)
    font_small = pewpew.BitmapFont(scale=2)        # body     (10x14)
    font_tiny  = pewpew.BitmapFont(scale=1)        # subnotes (5x7)
    # Mid-size 7x9 reserved for the hint section — key column and
    # description both. The 9 px line height slots cleanly between
    # 5x7 scale-1 (too dense) and 5x7 scale-2 (overflows the panel).
    font_mid   = pewpew.BitmapFont7x9(scale=1)     # hint rows (7x9)
    clock = pygame.time.Clock()
    ed = Editor()

    # Layout: preview | hierarchy panel | info panel — each separated by a
    # MARGIN. A ZOOM_BAND under the preview re-renders the active item at
    # integer scale; preview drops one int step if the desired scale
    # wouldn't fit alongside the band.
    CAP_BAND = 22
    avail_w = WIN_W - PANEL_W - TREE_PANEL_W - MARGIN * 4
    avail_h = WIN_H - TOPBAR_H - STATUS_H - MARGIN * 2 - CAP_BAND - ZOOM_BAND_H - MARGIN
    desired_scale = 2
    if SCREEN_W * desired_scale > avail_w or SCREEN_H * desired_scale > avail_h:
        ed.preview_scale = max(1, min(avail_w // SCREEN_W,
                                       max(1, avail_h // SCREEN_H)))
    else:
        ed.preview_scale = desired_scale
    pw = SCREEN_W * ed.preview_scale + 2   # +2 for the 1px border on each side
    ph = SCREEN_H * ed.preview_scale + 2
    preview_rect = pygame.Rect(MARGIN, TOPBAR_H + MARGIN, pw, ph)
    # Zoom band sits below the preview + caption strip; takes whatever's
    # left in the column above the status bar.
    zoom_top = preview_rect.bottom + CAP_BAND + MARGIN
    zoom_h = WIN_H - STATUS_H - MARGIN - zoom_top
    zoom_rect = pygame.Rect(MARGIN, zoom_top, pw, max(20, zoom_h))
    tree_rect = pygame.Rect(preview_rect.right + MARGIN, TOPBAR_H + MARGIN,
                            TREE_PANEL_W,
                            WIN_H - TOPBAR_H - STATUS_H - MARGIN * 2)
    panel_rect = pygame.Rect(WIN_W - PANEL_W - MARGIN, TOPBAR_H + MARGIN,
                             PANEL_W,
                             WIN_H - TOPBAR_H - STATUS_H - MARGIN * 2)

    running = True
    while running:
        dt = clock.tick(60)
        if ed.flash_t > 0:
            ed.flash_t = max(0, ed.flash_t - dt)
        update_gamepad_modifiers(ed)
        update_right_stick(ed)
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
                for rrect, idx in ed.list_rects:
                    if rrect.collidepoint(mx, my):
                        ed.item_idx = idx
                        ed.quit_armed = False
                        break

        screen.fill(BG)
        # Render the 640×480 preview surface once and reuse it for the
        # main preview panel + the zoom band. Avoids walking the layout
        # tree twice per frame.
        preview_surf = render_preview(ed)
        draw_topbar(screen, ed, font, font_small)
        draw_preview(screen, ed, preview_rect, font_small, preview_surf)
        draw_zoom_panel(screen, ed, zoom_rect, font_small, preview_surf)
        draw_tree_panel(screen, ed, tree_rect, font, font_small, font_tiny)
        draw_panel(screen, ed, panel_rect, font, font_small, font_tiny,
                   font_mid=font_mid)
        draw_status(screen, ed, font_small)
        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    main()
