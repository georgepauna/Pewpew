"""Layout editor for Pewpew screens.

Edit the per-screen overlay items (texts, rects, images) that ship in
art/layout.json. The game's TitleScreen / MapScreen / ShopScreen /
GameOverScreen / PlayState / hud_draw each call
pewpew.draw_layout_overlay(surf, "<screen>", fonts, assets) at the end of
their draw pass, so anything added here lands on top of the existing chrome
without touching the screen code.

Controls. Arrow keys / WASD are the workhorse keys; their meaning shifts
per edit mode (transform | style | text). R2 on the pad is a chord
modifier — hold it to swap a handful of mode-agnostic actions in.

  Screen               ;  '                  /  START  (cycles forward)
  Item                 [  ]                  /  LB  RB
  Mode cycle           Tab                   /  SELECT
  Move active          arrows                /  D-pad     (transform)
  Resize active        A D W S               /  X B Y A   (transform; rect/image/menu)
  Style nudge          arrows                /  D-pad     (style)
                         text: l/r = font, u/d = color
                         rect: l/r = outline, u/d = alpha
                         image: l/r = sprite, u/d = scale
                         menu: l/r = font, u/d = color
  Alpha / anchor       A D W S               /  X B Y A   (style)
                         A/D = alpha -/+ ;  W/S = anchor cycle (align for menu)
  Edit text            Tab into text mode, then type. Enter exits.
                         For menu, Tab cycles selected_decor → unselected_decor.
  Add                  N text  /  M rect  /  I image
  Duplicate            P                     /  L3
  Delete               Delete / Backspace    /  R3       (resets built-in to defaults)
  Color preset         1..8                  /  R2 + X/B (prev / next from PALETTE)
  Selected color       Shift+1..8            /  R2 + Y/A (menu sel_color)
  Horizontal alignment                       /  R2 + D-pad left/right
  Vertical spacing                           /  R2 + D-pad up/down
                                                (menu: line_height ; text: font ;
                                                 rect: h ; image: scale)
  Big stride (5x)      Shift held            /  L2 held
  Save layout.json     End                   /  R2 + START
  Reload from disk     Ctrl+R
  Quit (warns unsaved) Esc                   /  R2 + SELECT

Item schema (one object per item in layout.json["screens"][<name>]["items"]):
  text:  type, id, x, y, anchor, text, font (1..7), color [r,g,b],
         alpha (0..255), shadow (bool)
  rect:  type, id, x, y, w, h, color [r,g,b], alpha, outline (0 = filled)
  image: type, id, x, y, anchor, sprite (sprite name from art/sprites/),
         scale (float), alpha
"""
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
ITEM_TYPES = ("text", "rect", "image")
MODES = ("transform", "style", "text")

# Editor window chrome.
WIN_W, WIN_H = 1366, 800
TOPBAR_H = 34
STATUS_H = 26
MARGIN = 8
PANEL_W = 360

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

INITIAL_REPEAT_MS = 250
REPEAT_INTERVAL_MS = 60
TRIGGER_THRESHOLD = 0.1

# Xbox / XInput button indices
JB_A, JB_B, JB_X, JB_Y = 0, 1, 2, 3
JB_LB, JB_RB = 4, 5
JB_BACK, JB_START = 6, 7
JB_LSB, JB_RSB = 8, 9
JA_LT, JA_RT = 4, 5


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
    raise ValueError(kind)


# ---------------------------------------------------------------------------
# Held-key dispatch (mirrors _sprite_editor.HeldKey)
# ---------------------------------------------------------------------------

class HeldKey:
    __slots__ = ("action", "next_fire_ms")

    def __init__(self, action, now_ms):
        self.action = action
        self.next_fire_ms = now_ms + INITIAL_REPEAT_MS


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

        self.layout = load_layout()
        self.screen_idx = 0
        self.item_idx = 0
        self.mode = "transform"
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

    def display_rows(self):
        """Ordered display list for the current screen: every built-in
        element (in registry order) first, then every user-added item.
        Each row is a tuple ("builtin", builtin_id) or ("user", py_id)."""
        s = self.current_screen
        ids = self._builtin_ids(s)
        rows = [("builtin", b["id"]) for b in self.builtins_for(s)]
        for it in self.current_items:
            if it.get("id") not in ids:
                rows.append(("user", id(it)))
        return rows

    def _row_at(self, idx):
        """Resolve a display-row index to (override_handle_or_None,
        "builtin"|"user", builtin_default_or_None). For built-ins with no
        override yet, handle is None — call active_handle() to materialize."""
        rows = self.display_rows()
        if not rows:
            return None, None, None
        idx = max(0, min(idx, len(rows) - 1))
        kind, ident = rows[idx]
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
        """Return the merged view (built-in defaults + override, or the
        user item itself) for the active row. Use this for read-only
        access in mutators and previews — write through active_handle."""
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

    @property
    def active_item(self):
        # Kept for backward-compat with any old call sites; equivalent to
        # active_merged() (read-only view).
        return self.active_merged()

    def all_items_merged(self):
        """All display items for the current screen in render order:
        built-ins (with overrides applied) first, then user items."""
        s = self.current_screen
        out = []
        # Find override per built-in id.
        overrides = {it.get("id"): it for it in self.current_items
                     if it.get("id") in self._builtin_ids(s)}
        for b in self.builtins_for(s):
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
            "add_text", "add_rect", "add_image", "duplicate", "delete",
            "toggle_grid",
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
        elif action == "duplicate":   self._duplicate()
        elif action == "delete":      self._delete()
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
        # R2 chord actions (gamepad).
        elif action == "align_h_prev":     self.cycle_align_horiz(-1)
        elif action == "align_h_next":     self.cycle_align_horiz(+1)
        elif action == "vspacing_dec":     self.nudge_vspacing(-1)
        elif action == "vspacing_inc":     self.nudge_vspacing(+1)
        elif action == "palette_prev":     self.cycle_palette(-1)
        elif action == "palette_next":     self.cycle_palette(+1)
        elif action == "sel_palette_prev": self.cycle_sel_palette(-1)
        elif action == "sel_palette_next": self.cycle_sel_palette(+1)

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
        self._flash(msg, ms=900)

    def _emit_nav_feedback(self, action, before_screen, before_mode,
                            before_idx, before_rows):
        if action in ("screen_next", "screen_prev"):
            after = self.current_screen
            if after != before_screen:
                n = len(self.display_rows())
                self._flash(f"screen {before_screen} → {after}  ({n} items)", ms=900)
            return
        if action in ("item_next", "item_prev"):
            rows = self.display_rows()
            if not rows:
                self._flash("(no items on this screen)", ms=900)
                return
            kind, ident = rows[self.item_idx]
            tag = "[B]" if kind == "builtin" else "[U]"
            self._flash(
                f"item {before_idx + 1}/{len(before_rows) if before_rows else 0} → "
                f"{self.item_idx + 1}/{len(rows)}  {tag} {ident}",
                ms=900)
            return
        if action == "mode_cycle":
            self._flash(f"mode {before_mode} → {self.mode}", ms=900)
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
        # User items live alongside overrides in current_items. Avoid id
        # collisions with built-ins so the new item shows up as "user".
        ids = self._builtin_ids(self.current_screen)
        while item.get("id") in ids:
            item["id"] = _gen_id(item["type"][:3])
        self.current_items.append(item)
        # New row appears at the end of display_rows; jump cursor to it.
        self.item_idx = len(self.display_rows()) - 1
        self._touch()
        self._flash(f"added {item['type']}")

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
        # User item: drop from the list.
        self.current_items.remove(handle)
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
                cur = max(1, min(7, int(merged.get("font", 3)) + (1 if dw > 0 else -1)))
                if cur != merged.get("font"):
                    self._set_field("font", cur)

    def _style_horiz(self, step):
        merged = self.active_merged()
        if merged is None: return
        kind = merged.get("type")
        if kind in ("text", "menu"):
            cur = max(1, min(7, int(merged.get("font", 3)) + (1 if step > 0 else -1)))
            if cur != merged.get("font"):
                self._set_field("font", cur)
        elif kind == "rect":
            self._set_field("outline", max(0, int(merged.get("outline", 0)) + (1 if step > 0 else -1)))
        elif kind == "image":
            names = self.sprite_names
            if not names: return
            cur = merged.get("sprite") or names[0]
            idx = names.index(cur) if cur in names else 0
            idx = (idx + (1 if step > 0 else -1)) % len(names)
            self._set_field("sprite", names[idx])

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
        self._set_field("anchor", ANCHORS[idx])

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
        """R2+left/right: cycle horizontal alignment. For menu, that's the
        `align` field (left/center/right). For text/image, cycle the
        horizontal half of `anchor` while preserving the vertical half."""
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
            self._set_field("anchor", new_anchor)

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
            cur = max(1, min(7, int(merged.get("font", 3)) + delta))
            if cur != merged.get("font"):
                self._set_field("font", cur)
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

    # ---- text entry ------------------------------------------------------
    def text_subfields(self, merged):
        """Editable string fields for the active item in text mode. For
        text items: just "text". For menu items: both decor templates."""
        if merged is None:
            return ()
        kind = merged.get("type")
        if kind == "text":
            return ("text",)
        if kind == "menu":
            return ("selected_decor", "unselected_decor")
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
        """Cycle to the next subfield in text mode. Returns True if a new
        subfield is now active, False if there's nothing to cycle to (caller
        should exit text mode in that case)."""
        merged = self.active_merged()
        fields = self.text_subfields(merged)
        if len(fields) <= 1:
            return False
        cur = getattr(self, "text_subfield", fields[0])
        i = fields.index(cur) if cur in fields else 0
        i = (i + 1) % len(fields)
        self.text_subfield = fields[i]
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

    def cycle_item(self, delta):
        rows = self.display_rows()
        if not rows:
            return
        self.item_idx = (self.item_idx + delta) % len(rows)
        self.quit_armed = False

    def cycle_mode(self):
        idx = MODES.index(self.mode)
        # text mode only makes sense for items with editable string subfields
        for _ in range(len(MODES)):
            idx = (idx + 1) % len(MODES)
            m = MODES[idx]
            if m == "text":
                merged = self.active_merged()
                if not self.text_subfields(merged):
                    continue
                # Reset subfield cursor on text-mode entry.
                fields = self.text_subfields(merged)
                self.text_subfield = fields[0]
            self.mode = m
            return

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

    def _flash(self, msg, ms=1400, kind="info"):
        """kind: 'info' (default, neutral), 'saved' (green), 'warn' (red)."""
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


def item_bounds(it, ed):
    """Logical-coord (x, y, w, h) used to draw the selection box."""
    kind = it.get("type")
    x = int(it.get("x", 0))
    y = int(it.get("y", 0))
    if kind == "rect":
        return (x, y, max(1, int(it.get("w", 1))), max(1, int(it.get("h", 1))))
    if kind == "text":
        scale = max(1, min(7, int(it.get("font", 3))))
        font = ed.fonts.get(scale)
        if font is None:
            return (x, y, 8, 8)
        # Account for {dpad} placeholder so the selection box wraps the
        # icon too — same math as _draw_text_with_dpad.
        text = str(it.get("text", ""))
        if "{dpad}" in text:
            left, right = text.split("{dpad}", 1)
            lw, lh = font.size(left)
            rw, rh = font.size(right)
            icon_w = 7 * scale
            w = lw + icon_w + rw
            h = max(lh, rh, 7 * scale)
        else:
            w, h = font.size(text)
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
    if kind == "menu":
        # Match the renderer geometry exactly: each option centered (or
        # left/right anchored) at (x, y + i*line_h). The first option sits
        # on y, so vertical extent is (n-1)*line_h + font_h, top edge at
        # y - font_h/2.
        scale = max(1, min(7, int(it.get("font", 3))))
        font = ed.fonts.get(scale)
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
        font_h = font.get_height()
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
    preview_vars = {"score": 12345, "best": 145600}
    for _kind, it in ed.all_items_merged():
        t = it.get("type")
        try:
            if t == "text":
                # Interpolate {placeholders} for preview only — don't mutate
                # the stored value (a copy keeps the file/editor view clean).
                txt = str(it.get("text", ""))
                if "{" in txt:
                    try:
                        it = dict(it)
                        it["text"] = txt.format(**preview_vars)
                    except (KeyError, IndexError, ValueError):
                        pass
                pp._draw_text_with_dpad(surf, it, ed.fonts)
            elif t == "rect":
                pp._layout_draw_rect(surf, it)
            elif t == "image":
                pp._layout_draw_image(surf, it, sprite_assets)
            elif t == "menu":
                pp._layout_draw_menu(surf, it, ed.fonts)
        except Exception as e:
            print(f"preview draw {t} failed: {e}")
    return surf


def draw_topbar(screen, ed, font, font_small):
    bar = pygame.Rect(0, 0, screen.get_width(), TOPBAR_H)
    pygame.draw.rect(screen, PANEL_BG, bar)
    pygame.draw.line(screen, BORDER, (0, TOPBAR_H - 1),
                     (screen.get_width(), TOPBAR_H - 1))

    label = font.render("LAYOUT EDITOR", False, ACCENT)
    screen.blit(label, (12, (TOPBAR_H - label.get_height()) // 2))

    x = 200
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
    x += 24
    rows = ed.display_rows()
    idx_text = f"item {ed.item_idx + 1 if rows else 0}/{len(rows)}"
    t = font_small.render(idx_text, False, INK)
    screen.blit(t, (x, (TOPBAR_H - t.get_height()) // 2)); x += t.get_width()

    # Mode chip.
    x += 24
    chip_w = 110
    chip = pygame.Rect(x, 6, chip_w, TOPBAR_H - 12)
    pygame.draw.rect(screen, MODE_BG, chip)
    pygame.draw.rect(screen, BORDER, chip, 1)
    label_mode = f"mode: {ed.mode}"
    t = font_small.render(label_mode, False, INK)
    screen.blit(t, t.get_rect(center=chip.center))

    # Dirty indicator (right side).
    x = screen.get_width() - 12
    if ed.dirty:
        t = font.render("UNSAVED", False, DIRTY)
        screen.blit(t, (x - t.get_width(),
                        (TOPBAR_H - t.get_height()) // 2))


def draw_preview(screen, ed, preview_rect, font_small):
    """Render the 640×480 preview at exactly 2× integer scale (1280×960),
    centred in preview_rect. Falls back to the largest integer scale that
    fits when the window is too small for 2× (small laptop displays). The
    integer + nearest-neighbour scale keeps every game pixel crisp."""
    pygame.draw.rect(screen, (8, 10, 16), preview_rect)
    pygame.draw.rect(screen, BORDER, preview_rect, 1)

    preview = render_preview(ed)
    sw, sh = preview.get_size()
    desired = 2
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

    # Selection box on top, drawn in editor-space pixels. Iterate display
    # rows so built-in elements get outlined too.
    for i, (kind, it) in enumerate(ed.all_items_merged()):
        bx, by, bw, bh = item_bounds(it, ed)
        rx = ox + int(bx * scale)
        ry = oy + int(by * scale)
        rw = max(1, int(bw * scale))
        rh = max(1, int(bh * scale))
        is_active = (i == ed.item_idx)
        if is_active:
            color = ACTIVE_OUTLINE
            thick = 2
        else:
            color = (90, 130, 200) if kind == "builtin" else OTHER_OUTLINE
            thick = 1
        pygame.draw.rect(screen, color, (rx - 1, ry - 1, rw + 2, rh + 2), thick)
        if is_active:
            for cx, cy in ((rx, ry), (rx + rw, ry),
                           (rx, ry + rh), (rx + rw, ry + rh)):
                pygame.draw.rect(screen, ACCENT, (cx - 2, cy - 2, 4, 4))

    # Caption strip under the preview.
    cap = font_small.render(
        f"{SCREEN_W}x{SCREEN_H}  ({scale}x)  G toggles grid", False, DIM_INK)
    screen.blit(cap, (preview_rect.x + 4, preview_rect.bottom + 4))


def draw_panel(screen, ed, panel_rect, font, font_small, font_tiny):
    pygame.draw.rect(screen, PANEL_BG, panel_rect)
    pygame.draw.rect(screen, BORDER, panel_rect, 1)

    px, py = panel_rect.x + 10, panel_rect.y + 10
    width = panel_rect.w - 20

    # ===== Item list =====================================================
    rows_count = len(ed.display_rows())
    user_count = sum(1 for k, _ in ed.all_items_merged() if k == "user")
    builtin_count = rows_count - user_count
    title = font.render(
        f"items on {ed.current_screen}  ({builtin_count} built-in + {user_count} user)",
        False, INK)
    screen.blit(title, (px, py))
    py += title.get_height() + 6

    ed.list_rects = []
    list_h = panel_rect.h // 2 - (py - panel_rect.y)
    list_clip = pygame.Rect(px, py, width, list_h)
    pygame.draw.rect(screen, (12, 14, 22), list_clip)
    pygame.draw.rect(screen, BORDER, list_clip, 1)

    row_h = 18
    inner_y = list_clip.y + 4
    visible_rows = max(1, (list_clip.h - 8) // row_h)
    rows = list(ed.all_items_merged())
    if not rows:
        empty = font_small.render("(no items — press N / M / I to add)",
                                  False, DIM_INK)
        screen.blit(empty, (list_clip.x + 8, list_clip.y + 8))
    else:
        scroll = max(0, ed.item_idx - visible_rows + 1)
        scroll = min(scroll, max(0, len(rows) - visible_rows))
        builtin_ids = ed._builtin_ids(ed.current_screen)
        # Match display ordering to all_items_merged (built-ins first, user
        # items after) — same as display_rows().
        for vi, idx in enumerate(range(scroll, min(len(rows), scroll + visible_rows))):
            kind, it = rows[idx]
            rr = pygame.Rect(list_clip.x + 2, inner_y + vi * row_h,
                             list_clip.w - 4, row_h)
            if idx == ed.item_idx:
                pygame.draw.rect(screen, LIST_HIGHLIGHT, rr)
            type_ = it.get("type", "?")
            descr = ""
            if type_ == "text":
                descr = (it.get("text") or "")[:18]
            elif type_ == "rect":
                descr = f"{int(it.get('w',1))}x{int(it.get('h',1))}"
            elif type_ == "image":
                descr = (it.get("sprite") or "?")
            elif type_ == "menu":
                opts = it.get("_preview_options") or []
                descr = f"({len(opts)} opts)"
            tag = "B" if kind == "builtin" else "U"
            ident = it.get("id") or "?"
            line = f"[{tag}] {ident[:14]:<14} {type_:<5} {descr}"
            row_color = INK if idx == ed.item_idx else (
                (180, 210, 255) if kind == "builtin" else DIM_INK)
            t = font_small.render(line, False, row_color)
            screen.blit(t, (rr.x + 6, rr.y + (row_h - t.get_height()) // 2))
            ed.list_rects.append((rr, idx))

    py = list_clip.bottom + 12

    # ===== Active item properties =======================================
    it = ed.active_merged()
    if it is None:
        msg = font_small.render("no active item", False, DIM_INK)
        screen.blit(msg, (px, py))
    else:
        _, kind, builtin = ed.active_handle(create=False)
        tag = "[built-in]" if kind == "builtin" else "[user]"
        prop_title = font.render(
            f"{it.get('type','?')} — {it.get('id','?')}  {tag}", False, INK)
        screen.blit(prop_title, (px, py)); py += prop_title.get_height() + 4
        if builtin is not None and builtin.get("_label"):
            note = font_tiny.render(builtin["_label"], False, DIM_INK)
            screen.blit(note, (px, py)); py += note.get_height() + 4

        def row(label, value, color=INK):
            nonlocal py
            ll = font_small.render(label, False, DIM_INK)
            vv = font_small.render(str(value), False, color)
            screen.blit(ll, (px, py))
            screen.blit(vv, (px + 110, py))
            py += ll.get_height() + 2

        row("x", it.get("x", 0))
        row("y", it.get("y", 0))
        type_ = it.get("type")
        if type_ == "rect":
            row("w", it.get("w", 0))
            row("h", it.get("h", 0))
            row("outline", it.get("outline", 0))
            c = tuple(it.get("color") or (0, 0, 0))
            row("color", f"{c[0]},{c[1]},{c[2]}", color=c)
            row("alpha", it.get("alpha", 255))
        elif type_ == "text":
            row("text", repr(it.get("text", ""))[:28])
            row("font scale", it.get("font", 3))
            row("anchor", it.get("anchor", "tl"))
            c = tuple(it.get("color") or (0, 0, 0))
            row("color", f"{c[0]},{c[1]},{c[2]}", color=c)
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
            row("line height", it.get("line_height", 44))
            row("align", it.get("align", "center"))
            c = tuple(it.get("color") or (0, 0, 0))
            row("color", f"{c[0]},{c[1]},{c[2]}", color=c)
            sc = tuple(it.get("selected_color") or (0, 0, 0))
            row("sel color", f"{sc[0]},{sc[1]},{sc[2]}", color=sc)
            row("alpha", it.get("alpha", 255))
            row("sel decor",   repr(it.get("selected_decor", ""))[:24])
            row("unsel decor", repr(it.get("unselected_decor", ""))[:24])
        # Active text-edit subfield (shown so the user knows which decor
        # template typing will modify when in text mode on a menu).
        if ed.mode == "text":
            sub = ed.active_text_field()
            if sub:
                row("editing →", sub, color=ACCENT)

    # ===== Mode hints (bottom of panel) =================================
    hint_y = panel_rect.bottom - 110
    pygame.draw.line(screen, BORDER, (panel_rect.x + 6, hint_y - 4),
                     (panel_rect.right - 6, hint_y - 4))
    hints = [
        ("Tab / SEL",   "cycle mode  |  text mode: cycle subfield"),
        (";  '/START",  "prev / next screen"),
        ("[ ]  LB/RB",  "prev / next item"),
        ("N M I",       "add text / rect / image"),
        ("P/L3 Del/R3", "duplicate  |  delete user / reset built-in"),
        ("1..8 / Sh",   "color  |  Shift+1..8 = menu sel_color"),
        ("R2 + DP",     "L/R align  |  U/D spacing"),
        ("R2 + X/B/Y/A","palette: X/B color   Y/A sel_color"),
        ("End",         "save (R2+START)  |  Ctrl+R reload"),
        ("Esc",         "quit (R2+SELECT) — warns if unsaved"),
    ]
    for label, descr in hints:
        l = font_small.render(label, False, ACCENT)
        d = font_tiny.render(descr, False, DIM_INK)
        screen.blit(l, (panel_rect.x + 8, hint_y))
        screen.blit(d, (panel_rect.x + 90, hint_y + 1))
        hint_y += 14


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
        mode = ed.mode
        if mode == "transform":
            help_text = "arrows = pos  |  WASD = size/scale  |  Shift = x5"
        elif mode == "style":
            help_text = ("arrows = type-specific (font/color/outline/sprite/scale)"
                         "  |  A/D = alpha  |  W/S = anchor")
        else:
            help_text = "type to edit text  |  Backspace = del char  |  Enter = exit text mode"
        t = font_small.render(help_text, False, DIM_INK)
        screen.blit(t, (12, bar_y + (STATUS_H - t.get_height()) // 2))


# ---------------------------------------------------------------------------
# Input handlers
# ---------------------------------------------------------------------------

# Per-mode arrow/WASD action tables.
ARROW_KEYS = (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN)
WASD_KEYS = (pygame.K_a, pygame.K_d, pygame.K_w, pygame.K_s)

MODE_ARROWS = {
    "transform": ("pos_left", "pos_right", "pos_up", "pos_down"),
    "style":     ("style_left", "style_right", "style_up", "style_down"),
    "text":      (None, None, None, None),    # text mode ignores arrows
}
MODE_WASD = {
    # A, D, W, S — A/D shrink/grow width, W/S shrink/grow height (top-left
    # anchored, so W moves the bottom edge up = smaller).
    "transform": ("size_w_dec", "size_w_inc", "size_h_dec", "size_h_inc"),
    "style":     ("alpha_dec", "alpha_inc", "anchor_next", "anchor_prev"),
    "text":      (None, None, None, None),
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

    # Text mode: most keys go to the active subfield; Tab cycles subfield
    # (or exits if there are no more); Enter exits; Esc handled above.
    if ed.mode == "text":
        if k == pygame.K_TAB:
            if not ed.cycle_text_subfield():
                ed.mode = "transform"
            return True
        if k == pygame.K_RETURN:
            ed.mode = "transform"; return True
        if k == pygame.K_BACKSPACE:
            ed.pop_text(); return True
        ch = evt.unicode
        if ch and ch.isprintable():
            ed.append_text(ch)
        return True

    ed.quit_armed = False

    if ctrl and k == pygame.K_r:
        ed.reload(); return True
    if k == pygame.K_TAB:
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
    if k == pygame.K_p:
        ed.apply_action("duplicate"); return True
    if k in (pygame.K_DELETE, pygame.K_BACKSPACE):
        ed.apply_action("delete"); return True
    if k == pygame.K_g:
        ed.apply_action("toggle_grid"); return True
    if k == pygame.K_h:
        # Toggle shadow on text items.
        ed.apply_action("shadow_toggle"); return True
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


def update_gamepad_modifiers(ed):
    """Poll L2/R2 each frame. When R2 transitions, re-dispatch the current
    D-pad state so the chord actions swap in/out without needing the user
    to release-and-repress the D-pad."""
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
        try:
            hat = ed.gamepad.get_hat(0)
        except Exception:
            return
        # Stop every currently-held D-pad action so the new mode-set takes
        # over cleanly, then re-fire as if the hat just moved.
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


# R2 + face button → palette / sel-palette cycle (chord overrides WASD).
_R2_FACE_CHORD = {
    JB_X: "palette_prev",
    JB_B: "palette_next",
    JB_Y: "sel_palette_prev",
    JB_A: "sel_palette_next",
}


def handle_joy_button_down(ed, evt):
    ed.quit_armed = False
    btn = evt.button
    key = ("gp_btn", btn)
    # SELECT cycles modes (matches sprite editor); START cycles screens.
    # With R2 held, the pair turns into quit / save (swapped — save sits
    # under the louder START button so it's harder to hit by accident).
    if btn == JB_BACK:           # SELECT
        if ed.modifier_r2:
            if not ed.request_quit():
                return
            pygame.event.post(pygame.event.Event(pygame.QUIT))
            return
        ed.start_action(key, "mode_cycle")
    elif btn == JB_START:
        if ed.modifier_r2:
            ed.save()
            return
        ed.start_action(key, "screen_next")
    elif btn == JB_LB:
        ed.start_action(key, "item_prev")
    elif btn == JB_RB:
        ed.start_action(key, "item_next")
    elif btn == JB_LSB:
        ed.start_action(key, "duplicate")
    elif btn == JB_RSB:
        # R3 free now (mode moved to SELECT, screens to START). Use for
        # delete so destructive ops live away from the navigation cluster.
        ed.start_action(key, "delete")
    elif btn in (JB_X, JB_B, JB_Y, JB_A):
        if ed.modifier_r2:
            action = _R2_FACE_CHORD.get(btn)
        else:
            action = _gp_face_action(ed.mode, btn)
        if action:
            ed.start_action(key, action)


def handle_joy_button_up(ed, evt):
    ed.stop_action(("gp_btn", evt.button))


# R2 + D-pad → align / vertical-spacing chord.
_R2_HAT_ACTIONS = ("align_h_prev", "align_h_next", "vspacing_dec", "vspacing_inc")
_ALL_DPAD_ACTIONS = (
    "pos_left", "pos_right", "pos_up", "pos_down",
    "style_left", "style_right", "style_up", "style_down",
) + _R2_HAT_ACTIONS


def handle_joy_hat(ed, evt):
    hx, hy = evt.value
    ed.quit_armed = False
    actions = _R2_HAT_ACTIONS if ed.modifier_r2 else MODE_ARROWS.get(ed.mode, (None,) * 4)
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
    pygame.init()
    info = pygame.display.Info()
    global WIN_W, WIN_H
    WIN_W = info.current_w
    WIN_H = info.current_h
    screen = pygame.display.set_mode((WIN_W, WIN_H), pygame.NOFRAME)
    pygame.display.set_caption("Pewpew layout editor")
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
                for rrect, idx in ed.list_rects:
                    if rrect.collidepoint(mx, my):
                        ed.item_idx = idx
                        ed.quit_armed = False
                        break

        screen.fill(BG)
        panel_rect = pygame.Rect(WIN_W - PANEL_W - MARGIN, TOPBAR_H + MARGIN,
                                 PANEL_W, WIN_H - TOPBAR_H - STATUS_H - MARGIN * 2)
        preview_rect = pygame.Rect(MARGIN, TOPBAR_H + MARGIN,
                                   WIN_W - PANEL_W - MARGIN * 3,
                                   WIN_H - TOPBAR_H - STATUS_H - MARGIN * 2)
        draw_topbar(screen, ed, font, font_small)
        draw_preview(screen, ed, preview_rect, font_small)
        draw_panel(screen, ed, panel_rect, font, font_small, font_tiny)
        draw_status(screen, ed, font_small)
        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    main()
