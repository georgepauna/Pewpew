"""Compose a single PNG showing the full screen flow + button mapping.

Consumes the screenshots in screenshots/ (regenerate first with
`python _smoke.py` if you've made UI changes) and emits
screenshots/flow_diagram.png. Two stacked sections — NORMAL MODE on
top, GHOST MODE below — so the user can see at a glance how every
state looks and how the player reaches it.

Button labels in the arrows reference the PC silk letters (the
diagram is generated on Windows); the actual in-game labels follow
BUTTON_SCHEME and swap on the RG. Where the gamepad input is named
by face position rather than letter (north/east/west/south), the
label uses that — same physical position on every controller.
"""
import os, sys, math
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
import pygame
pygame.init()
pygame.display.set_mode((640, 480), pygame.HIDDEN)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pewpew

SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "screenshots")
OUT_PATH = os.path.join(SHOT_DIR, "flow_diagram.png")

# ── Canvas + tile geometry ──────────────────────────────────────────
TILE_W, TILE_H = 360, 270   # source 640x480 × 0.5625
COL_GAP = 60
ROW_GAP = 120
HEAD_BAR = 26
SEC_HEAD = 56
MARGIN = 60

COLS = 5
ROWS_PER_SEC = 2

SEC_W = MARGIN * 2 + COLS * TILE_W + (COLS - 1) * COL_GAP
SEC_H = (SEC_HEAD + HEAD_BAR + TILE_H + ROW_GAP
         + HEAD_BAR + TILE_H + MARGIN)

CANVAS_W = SEC_W
CANVAS_H = MARGIN + 2 * SEC_H + 40

BG = (18, 22, 34)
SEC_NORMAL_BG = (24, 30, 48)
SEC_GHOST_BG = (32, 22, 38)
TILE_BORDER = (90, 105, 140)
HEAD_FG = (240, 240, 255)
ARROW = (130, 200, 255)
ARROW_GHOST = (240, 140, 200)
LABEL_BG = (10, 14, 26)
LABEL_FG = (240, 240, 255)
DIM = (170, 180, 200)

# ── Layout: (id, screenshot file, title, col, row) ──────────────────
# col 0..4, row 0..1 within the section. Sections placed at fixed y.
NORMAL = [
    ("title",   "title.png",       "TITLE",        0, 0),
    ("map",     "map.png",         "MAP",          1, 0),
    ("shop",    "shop.png",        "SHOP",         2, 0),
    ("gameover","gameover.png",    "GAME OVER",    4, 0),
    ("play",    "play.png",        "PLAY",         0, 1),
    ("paused",  "play_paused.png", "PAUSED",       1, 1),
    ("win",     "play_win.png",    "MISSION COMPLETE", 2, 1),
    ("loss",    "play_loss.png",   "SHIP DESTROYED",   3, 1),
]

GHOST = [
    ("title_g",   "title_ghost.png",          "TITLE (Ghost)",    0, 0),
    ("map_g",     "map_ghost.png",            "MAP (Ghost)",      1, 0),
    ("shop_g",    "shop_ghost.png",           "SHOP (Ghost)",     2, 0),
    ("rewu",      "play_ghost_rewind_unlocked.png",
                                              "PLAY (rewind unlocked)", 4, 0),
    ("play_g",    "play_ghost.png",           "PLAY (Ghost)",     0, 1),
    ("deadp",     "play_ghost_dead_pause.png","DEAD-PAUSE PROMPT",1, 1),
    ("win_g",     "play_win.png",             "MISSION COMPLETE 100%", 2, 1),
    ("fail_g",    "play_ghost_fail.png",      "MISSION FAILED (<100%)",3, 1),
]

# Arrows: (src_id, dst_id, label, color_key)
# color_key: "blue" = action / common, "yellow" = optional / conditional
NORMAL_ARROWS = [
    ("title", "map",     "fire on Continue/New Game", "blue"),
    ("map",   "play",    "fire on level node", "blue"),
    ("map",   "shop",    "ability (shop button)", "blue"),
    ("shop",  "map",     "cancel", "yellow"),
    ("map",   "title",   "cancel", "yellow"),
    ("play",  "paused",  "START", "blue"),
    ("paused","play",    "START (resume)", "yellow"),
    ("play",  "win",     "(level complete)", "blue"),
    ("play",  "loss",    "(ship destroyed)", "blue"),
    ("win",   "shop",    "fire continue", "blue"),
    ("win",   "play",    "ability retry (partial)", "yellow"),
    ("loss",  "gameover","fire continue", "blue"),
    ("gameover","title", "fire continue", "blue"),
]

GHOST_ARROWS = [
    ("title_g", "map_g",  "fire on Continue/New Game", "blue"),
    ("map_g",   "play_g", "fire on level node", "blue"),
    ("map_g",   "shop_g", "ability (shop button)", "blue"),
    ("shop_g",  "map_g",  "cancel", "yellow"),
    ("map_g",   "title_g","cancel", "yellow"),
    ("play_g",  "deadp",  "(player dies — 1-hit kill)", "blue"),
    ("deadp",   "play_g", "east hold = REWIND (first time → unlocks)", "blue"),
    ("deadp",   "fail_g", "START = give up", "yellow"),
    ("play_g",  "win_g",  "(level end, 100% kills)", "blue"),
    ("play_g",  "fail_g", "(level end, <100% kills)", "blue"),
    ("win_g",   "shop_g", "fire continue", "blue"),
    ("fail_g",  "play_g", "east hold = rewind back into sim", "yellow"),
    ("fail_g",  "play_g", "ability retry", "yellow"),
    ("fail_g",  "title_g","fire give up → GameOver → Title", "yellow"),
    ("play_g",  "rewu",   "after FIRST rewind: HUD shows 'rewind' on east", "yellow"),
]

# Cross-mode toggle (rendered separately).
MODE_TOGGLE = "MODE TOGGLE: north (cancel/Y on RG, X on PC) on title  ↔  per-profile ghost flag flips, save reloads"

# ── Fonts ───────────────────────────────────────────────────────────
def font(size, bold=False):
    return pygame.font.SysFont("consolas", size, bold=bold)


F_SEC = font(34, bold=True)
F_TITLE = font(16, bold=True)
F_LABEL = font(12)
F_FOOT = font(14)
F_NOTE = font(13)


# ── Helpers ─────────────────────────────────────────────────────────
def tile_xy(col, row, sec_y):
    x = MARGIN + col * (TILE_W + COL_GAP)
    y = sec_y + SEC_HEAD + HEAD_BAR + row * (TILE_H + ROW_GAP + HEAD_BAR)
    return x, y


def tile_rect(col, row, sec_y):
    x, y = tile_xy(col, row, sec_y)
    return pygame.Rect(x, y, TILE_W, TILE_H)


def edge_point(rect, target):
    """Return the point on the rect's edge closest to `target`. Used so
    arrow endpoints land on the tile border rather than the centre."""
    cx, cy = rect.centerx, rect.centery
    tx, ty = target
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    # Project to the rect boundary along (dx, dy).
    rx = TILE_W / 2 / max(abs(dx), 1e-6)
    ry = TILE_H / 2 / max(abs(dy), 1e-6)
    t = min(rx, ry)
    return int(cx + dx * t), int(cy + dy * t)


def draw_tile(canvas, screen_id, shot_path, title, rect):
    """Blit one screenshot tile with a header bar."""
    # Header bar above the tile
    hdr_rect = pygame.Rect(rect.x, rect.y - HEAD_BAR, rect.w, HEAD_BAR)
    pygame.draw.rect(canvas, (40, 50, 78), hdr_rect)
    pygame.draw.rect(canvas, TILE_BORDER, hdr_rect, 1)
    title_surf = F_TITLE.render(title, True, HEAD_FG)
    canvas.blit(title_surf, (hdr_rect.x + 8,
                             hdr_rect.y + (HEAD_BAR - title_surf.get_height()) // 2))
    # Screenshot
    try:
        shot = pygame.image.load(shot_path)
    except Exception as e:
        pygame.draw.rect(canvas, (60, 30, 30), rect)
        err = F_LABEL.render(f"missing: {os.path.basename(shot_path)}",
                             True, (240, 200, 200))
        canvas.blit(err, (rect.x + 10, rect.y + 10))
        pygame.draw.rect(canvas, TILE_BORDER, rect, 2)
        return
    shot_scaled = pygame.transform.smoothscale(shot, (rect.w, rect.h))
    canvas.blit(shot_scaled, rect.topleft)
    pygame.draw.rect(canvas, TILE_BORDER, rect, 2)


def draw_arrow(canvas, src_rect, dst_rect, label, color, curve_off=0):
    """Draw an arrow from src to dst with a button-label chip. curve_off
    biases the midpoint perpendicular to the direct line so parallel
    arrows between the same pair don't overlap their labels."""
    sx, sy = src_rect.center
    dx, dy = dst_rect.center
    # Edge points so arrowhead lands at the border.
    sp = edge_point(src_rect, (dx, dy))
    dp = edge_point(dst_rect, (sx, sy))
    # Optional curve: bend the midpoint perpendicular.
    if curve_off != 0:
        mx = (sp[0] + dp[0]) / 2
        my = (sp[1] + dp[1]) / 2
        ang = math.atan2(dp[1] - sp[1], dp[0] - sp[0])
        nx, ny = -math.sin(ang), math.cos(ang)
        cp = (int(mx + nx * curve_off), int(my + ny * curve_off))
        pygame.draw.lines(canvas, color, False,
                          [sp, cp, dp], 3)
        ang_end = math.atan2(dp[1] - cp[1], dp[0] - cp[0])
        label_xy = cp
    else:
        pygame.draw.line(canvas, color, sp, dp, 3)
        ang_end = math.atan2(dp[1] - sp[1], dp[0] - sp[0])
        label_xy = ((sp[0] + dp[0]) // 2, (sp[1] + dp[1]) // 2)
    # Arrowhead
    head = 14
    a1 = (dp[0] - head * math.cos(ang_end - math.pi / 7),
          dp[1] - head * math.sin(ang_end - math.pi / 7))
    a2 = (dp[0] - head * math.cos(ang_end + math.pi / 7),
          dp[1] - head * math.sin(ang_end + math.pi / 7))
    pygame.draw.polygon(canvas, color, [dp, a1, a2])
    # Label chip
    if label:
        lsurf = F_LABEL.render(label, True, LABEL_FG)
        lw, lh = lsurf.get_size()
        chip = pygame.Rect(label_xy[0] - lw // 2 - 6,
                           label_xy[1] - lh // 2 - 3,
                           lw + 12, lh + 6)
        pygame.draw.rect(canvas, LABEL_BG, chip)
        pygame.draw.rect(canvas, color, chip, 1)
        canvas.blit(lsurf, (chip.x + 6, chip.y + 3))


def draw_section(canvas, header, screens, arrows, sec_y, bg, arrow_color):
    """Render one mode-section: header band, tiles, arrows."""
    band = pygame.Rect(MARGIN // 2, sec_y, CANVAS_W - MARGIN, SEC_H)
    pygame.draw.rect(canvas, bg, band, border_radius=12)
    pygame.draw.rect(canvas, (60, 75, 110), band, 2, border_radius=12)
    head_surf = F_SEC.render(header, True, HEAD_FG)
    canvas.blit(head_surf, (band.x + 24, sec_y + 14))

    # Build a rect index for arrow routing.
    rects = {}
    for screen_id, shot_file, title, col, row in screens:
        rect = tile_rect(col, row, sec_y)
        rects[screen_id] = rect
        draw_tile(canvas, screen_id,
                  os.path.join(SHOT_DIR, shot_file),
                  title, rect)

    # Bucket arrows by (src, dst) pair so parallel arrows can curve.
    bucket = {}
    for src, dst, label, color_key in arrows:
        key = tuple(sorted((src, dst)))
        bucket.setdefault(key, []).append((src, dst, label, color_key))
    for pair, entries in bucket.items():
        n = len(entries)
        for i, (src, dst, label, color_key) in enumerate(entries):
            if src not in rects or dst not in rects:
                continue
            # Curve offset: 0 for single, ±28 for two, fans out for more.
            curve = 0
            if n > 1:
                curve = (i - (n - 1) / 2) * 36
            color = arrow_color if color_key == "blue" else (255, 210, 90)
            draw_arrow(canvas, rects[src], rects[dst], label, color, curve)


def main():
    canvas = pygame.Surface((CANVAS_W, CANVAS_H))
    canvas.fill(BG)

    # Section headers — include the version stamp so the file is
    # self-dating without a separate footer.
    head = F_NOTE.render(
        f"PEWPEW screen-flow & button map — v{pewpew.VERSION}",
        True, DIM)
    canvas.blit(head, (MARGIN, 16))

    sec_n_y = MARGIN
    sec_g_y = sec_n_y + SEC_H + 40

    draw_section(canvas, "NORMAL MODE", NORMAL, NORMAL_ARROWS,
                 sec_n_y, SEC_NORMAL_BG, ARROW)
    draw_section(canvas, "GHOST MODE", GHOST, GHOST_ARROWS,
                 sec_g_y, SEC_GHOST_BG, ARROW_GHOST)

    # Mode-toggle footnote between/below the sections.
    foot = F_FOOT.render(MODE_TOGGLE, True, DIM)
    canvas.blit(foot, (MARGIN, CANVAS_H - 28))

    pygame.image.save(canvas, OUT_PATH)
    print(f"flow diagram -> {OUT_PATH}  ({CANVAS_W}x{CANVAS_H})")


if __name__ == "__main__":
    main()
