"""Compose a single PNG showing the full screen flow + button mapping.

Two stacked sections — NORMAL MODE on top, GHOST MODE below — so the
user can see at a glance how every state looks and how the player
reaches it. Run after `_smoke.py` to refresh the source screenshots.

Routing strategy
----------------
Each section has THREE horizontal highways:
  - top highway     — runs above row 0, used for same-row-0 arrows
  - middle highway  — runs between rows 0 and 1, used for cross-row
  - bottom highway  — runs below row 1, used for same-row-1 arrows
Adjacent same-row pairs use a direct side-to-side line; everything
else exits the tile via the closest highway, hops across, and enters
the dst tile via its own highway-adjacent edge. Per-tile port
allocation spreads attachment points along each edge so multiple
arrows on the same side don't pile up.

Button labels reference the PC silk letters (this diagram is
generated on Windows); the in-game labels follow BUTTON_SCHEME and
swap on the RG. Face-position names (north/east/west/south) are
used where the mapping is the same on every controller.
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
TILE_W, TILE_H = 380, 285
COL_GAP = 70
HEAD_BAR = 30
SEC_HEAD = 60
MARGIN = 70

# Three highway bands, each large enough to host its fan of arrows.
TOP_HWY = 90
MID_HWY = 140
BOT_HWY = 90

COLS = 5

SEC_W = MARGIN * 2 + COLS * TILE_W + (COLS - 1) * COL_GAP
SEC_H = (SEC_HEAD + TOP_HWY + HEAD_BAR + TILE_H
         + MID_HWY + HEAD_BAR + TILE_H + BOT_HWY + 20)

CANVAS_W = SEC_W
CANVAS_H = MARGIN + 2 * SEC_H + 70

BG = (18, 22, 34)
SEC_NORMAL_BG = (24, 30, 48)
SEC_GHOST_BG = (32, 22, 42)
TILE_BORDER = (90, 105, 140)
HEAD_FG = (240, 240, 255)
ARROW_BLUE = (130, 200, 255)
ARROW_BLUE_GHOST = (235, 130, 200)
ARROW_YELLOW = (255, 210, 90)
LABEL_BG = (12, 16, 28)
LABEL_FG = (240, 240, 255)
DIM = (170, 180, 200)

# ── Layout: (id, screenshot file, title, col, row) ──────────────────
NORMAL = [
    ("title",   "title.png",       "TITLE",            0, 0),
    ("map",     "map.png",         "MAP",              1, 0),
    ("shop",    "shop.png",        "SHOP",             2, 0),
    ("gameover","gameover.png",    "GAME OVER",        4, 0),
    ("play",    "play.png",        "PLAY",             0, 1),
    ("paused",  "play_paused.png", "PAUSED",           1, 1),
    ("win",     "play_win.png",    "MISSION COMPLETE", 2, 1),
    ("loss",    "play_loss.png",   "SHIP DESTROYED",   3, 1),
]

GHOST = [
    ("title_g", "title_ghost.png",          "TITLE (Ghost)",         0, 0),
    ("map_g",   "map_ghost.png",            "MAP (Ghost)",           1, 0),
    ("shop_g",  "shop_ghost.png",           "SHOP (Ghost)",          2, 0),
    ("rewu",    "play_ghost_rewind_unlocked.png",
                                            "PLAY (rewind unlocked)",4, 0),
    ("play_g",  "play_ghost.png",           "PLAY (Ghost)",          0, 1),
    ("deadp",   "play_ghost_dead_pause.png","DEAD-PAUSE PROMPT",     1, 1),
    ("win_g",   "play_win.png",             "MISSION COMPLETE 100%", 2, 1),
    ("fail_g",  "play_ghost_fail.png",      "MISSION FAILED (<100%)",3, 1),
]

NORMAL_ARROWS = [
    ("title", "map",     "fire on Continue/New Game", "blue"),
    ("map",   "play",    "fire on level node",        "blue"),
    ("map",   "shop",    "ability (shop button)",     "blue"),
    ("shop",  "map",     "cancel",                    "yellow"),
    ("map",   "title",   "cancel",                    "yellow"),
    ("play",  "paused",  "START",                     "blue"),
    ("paused","play",    "START (resume)",            "yellow"),
    ("play",  "win",     "(level complete)",          "blue"),
    ("play",  "loss",    "(ship destroyed)",          "blue"),
    ("win",   "shop",    "fire continue",             "blue"),
    ("win",   "play",    "ability retry (partial)",   "yellow"),
    ("loss",  "gameover","fire continue",             "blue"),
    ("gameover","title", "fire continue",             "blue"),
]

GHOST_ARROWS = [
    ("title_g", "map_g",  "fire on Continue/New Game",        "blue"),
    ("map_g",   "play_g", "fire on level node",               "blue"),
    ("map_g",   "shop_g", "ability (shop button)",            "blue"),
    ("shop_g",  "map_g",  "cancel",                           "yellow"),
    ("map_g",   "title_g","cancel",                           "yellow"),
    ("play_g",  "deadp",  "(player dies — 1-hit kill)",       "blue"),
    ("deadp",   "play_g", "east hold = REWIND (1st = unlock)","blue"),
    ("deadp",   "fail_g", "START = give up",                  "yellow"),
    ("play_g",  "win_g",  "(level end, 100%)",                "blue"),
    ("play_g",  "fail_g", "(level end, <100%)",               "blue"),
    ("win_g",   "shop_g", "fire continue",                    "blue"),
    ("fail_g",  "play_g", "east hold = rewind into sim",      "yellow"),
    ("fail_g",  "play_g", "ability retry",                    "yellow"),
    ("play_g",  "rewu",   "after 1st rewind: HUD label flips","yellow"),
]

MODE_TOGGLE = ("MODE TOGGLE: north on title (silk Y on RG, silk X on PC) "
               "flips the active profile's ghost flag and reloads the save")

# ── Fonts ───────────────────────────────────────────────────────────
def font(size, bold=False):
    return pygame.font.SysFont("consolas", size, bold=bold)


F_SEC = font(38, bold=True)
F_TITLE = font(17, bold=True)
F_LABEL = font(13)
F_FOOT = font(15)
F_HEAD = font(14)


# ── Geometry helpers ────────────────────────────────────────────────
def tile_xy(col, row, sec_y):
    x = MARGIN + col * (TILE_W + COL_GAP)
    if row == 0:
        y = sec_y + SEC_HEAD + TOP_HWY + HEAD_BAR
    else:
        y = (sec_y + SEC_HEAD + TOP_HWY + HEAD_BAR + TILE_H
             + MID_HWY + HEAD_BAR)
    return x, y


def tile_rect(col, row, sec_y):
    x, y = tile_xy(col, row, sec_y)
    return pygame.Rect(x, y, TILE_W, TILE_H)


# ── Routing decision ────────────────────────────────────────────────
def highway_for_arrow(src_row, dst_row, adjacent):
    """Choose which highway band the arrow should use."""
    if adjacent and src_row == dst_row:
        return "side"
    if src_row == 0 and dst_row == 0:
        return "top"
    if src_row == 1 and dst_row == 1:
        return "bottom"
    return "mid"


def side_for_attachment(highway, role, row):
    """Which tile edge an arrow attaches to.

    role is "src" or "dst"; the side returned identifies which of
    the four tile edges the arrow exits or enters."""
    if highway == "side":
        return None  # decided per-arrow by direction
    if highway == "top":
        return "top"
    if highway == "bottom":
        return "bottom"
    # mid highway
    if row == 0:
        return "bottom"
    return "top"


# ── Port allocator ──────────────────────────────────────────────────
def allocate_ports(arrows, screens):
    """For each arrow assign (src_x_off, dst_x_off, side_for_src,
    side_for_dst, highway). Spreads multiple arrows attached to the
    same tile-edge along that edge so they don't share a point."""
    rows = {s[0]: s[4] for s in screens}
    cols = {s[0]: s[3] for s in screens}

    # Edge buckets per tile per side: tile_id → side → list of (arrow_idx, x_or_y)
    edge_arrows = {s[0]: {"top": [], "bottom": [],
                          "left": [], "right": []} for s in screens}

    decisions = {}  # arrow_idx → (highway, src_side, dst_side)
    for idx, (src, dst, _, _) in enumerate(arrows):
        if src not in rows or dst not in rows:
            continue
        hd = abs(cols[dst] - cols[src])
        adjacent = hd <= 1
        hwy = highway_for_arrow(rows[src], rows[dst], adjacent)
        if hwy == "side":
            if cols[dst] > cols[src]:
                src_side, dst_side = "right", "left"
            else:
                src_side, dst_side = "left", "right"
        else:
            src_side = side_for_attachment(hwy, "src", rows[src])
            dst_side = side_for_attachment(hwy, "dst", rows[dst])
        decisions[idx] = (hwy, src_side, dst_side)
        edge_arrows[src][src_side].append(idx)
        edge_arrows[dst][dst_side].append(idx)

    # Per (tile, side) — spread arrows along the edge.
    port_x = {}  # arrow_idx → x offset (for top/bottom edges)
    port_y = {}  # arrow_idx → y offset (for left/right edges)
    role_for = {}  # (arrow_idx, "src"/"dst") → (side, tile_id)
    for idx, (src, dst, _, _) in enumerate(arrows):
        if idx in decisions:
            hwy, src_side, dst_side = decisions[idx]
            role_for[(idx, "src")] = (src_side, src)
            role_for[(idx, "dst")] = (dst_side, dst)

    src_x_off = {}
    dst_x_off = {}
    src_y_off = {}
    dst_y_off = {}
    for tile_id, sides in edge_arrows.items():
        for side, idx_list in sides.items():
            n = len(idx_list)
            if n == 0:
                continue
            if side in ("top", "bottom"):
                span = TILE_W * 0.7
                step = span / n
                start = -span / 2 + step / 2
                for i, idx in enumerate(idx_list):
                    off = start + i * step
                    # Decide whether this is src or dst attachment.
                    if role_for.get((idx, "src")) == (side, tile_id):
                        src_x_off[idx] = off
                    if role_for.get((idx, "dst")) == (side, tile_id):
                        dst_x_off[idx] = off
            else:  # left/right
                span = TILE_H * 0.6
                step = span / n
                start = -span / 2 + step / 2
                for i, idx in enumerate(idx_list):
                    off = start + i * step
                    if role_for.get((idx, "src")) == (side, tile_id):
                        src_y_off[idx] = off
                    if role_for.get((idx, "dst")) == (side, tile_id):
                        dst_y_off[idx] = off

    return decisions, src_x_off, dst_x_off, src_y_off, dst_y_off


# ── Routing ─────────────────────────────────────────────────────────
def edge_point(rect, side, x_off=0, y_off=0):
    if side == "top":
        return (rect.centerx + x_off, rect.top)
    if side == "bottom":
        return (rect.centerx + x_off, rect.bottom)
    if side == "left":
        return (rect.left, rect.centery + y_off)
    if side == "right":
        return (rect.right, rect.centery + y_off)
    return rect.center


def route_arrow(src_rect, dst_rect, src_side, dst_side, highway,
                top_hy, mid_hy, bot_hy,
                src_x_off, dst_x_off, src_y_off, dst_y_off,
                lane_y):
    """Return the polyline for the arrow."""
    sp = edge_point(src_rect, src_side, src_x_off, src_y_off)
    dp = edge_point(dst_rect, dst_side, dst_x_off, dst_y_off)
    if highway == "side":
        # Direct horizontal with a small y lane stagger.
        return [(sp[0], sp[1] + lane_y), (dp[0], dp[1] + lane_y)]
    if highway == "top":
        hy = top_hy + lane_y
    elif highway == "bottom":
        hy = bot_hy + lane_y
    else:
        hy = mid_hy + lane_y
    return [sp, (sp[0], hy), (dp[0], hy), dp]


# ── Drawing primitives ──────────────────────────────────────────────
def draw_tile(canvas, screen_id, shot_path, title, rect):
    hdr_rect = pygame.Rect(rect.x, rect.y - HEAD_BAR, rect.w, HEAD_BAR)
    pygame.draw.rect(canvas, (40, 50, 78), hdr_rect)
    pygame.draw.rect(canvas, TILE_BORDER, hdr_rect, 1)
    title_surf = F_TITLE.render(title, True, HEAD_FG)
    canvas.blit(title_surf, (hdr_rect.x + 10,
                             hdr_rect.y + (HEAD_BAR - title_surf.get_height()) // 2))
    try:
        shot = pygame.image.load(shot_path)
        shot_scaled = pygame.transform.smoothscale(shot, (rect.w, rect.h))
        canvas.blit(shot_scaled, rect.topleft)
    except Exception:
        pygame.draw.rect(canvas, (60, 30, 30), rect)
        err = F_LABEL.render(f"missing: {os.path.basename(shot_path)}",
                             True, (240, 200, 200))
        canvas.blit(err, (rect.x + 10, rect.y + 10))
    pygame.draw.rect(canvas, TILE_BORDER, rect, 2)


def draw_polyline_arrow(canvas, points, label, color, label_seg_idx=None):
    if len(points) < 2:
        return
    pygame.draw.lines(canvas, color, False, points, 3)
    last = points[-1]
    prev = points[-2]
    ang = math.atan2(last[1] - prev[1], last[0] - prev[0])
    head = 15
    a1 = (last[0] - head * math.cos(ang - math.pi / 7),
          last[1] - head * math.sin(ang - math.pi / 7))
    a2 = (last[0] - head * math.cos(ang + math.pi / 7),
          last[1] - head * math.sin(ang + math.pi / 7))
    pygame.draw.polygon(canvas, color, [last, a1, a2])
    if not label:
        return
    # Pick the longest segment for the label.
    if label_seg_idx is None:
        best_i, best_len = 0, 0
        for i in range(len(points) - 1):
            dx = points[i + 1][0] - points[i][0]
            dy = points[i + 1][1] - points[i][1]
            l = dx * dx + dy * dy
            if l > best_len:
                best_len = l
                best_i = i
        label_seg_idx = best_i
    p0 = points[label_seg_idx]
    p1 = points[label_seg_idx + 1]
    mx = (p0[0] + p1[0]) // 2
    my = (p0[1] + p1[1]) // 2
    lsurf = F_LABEL.render(label, True, LABEL_FG)
    lw, lh = lsurf.get_size()
    chip = pygame.Rect(mx - lw // 2 - 7, my - lh // 2 - 4,
                       lw + 14, lh + 8)
    pygame.draw.rect(canvas, LABEL_BG, chip)
    pygame.draw.rect(canvas, color, chip, 1)
    canvas.blit(lsurf, (chip.x + 7, chip.y + 4))


# ── Section renderer ────────────────────────────────────────────────
def draw_section(canvas, header, screens, arrows, sec_y, bg,
                 arrow_blue, arrow_yellow):
    band = pygame.Rect(MARGIN // 2, sec_y, CANVAS_W - MARGIN, SEC_H)
    pygame.draw.rect(canvas, bg, band, border_radius=14)
    pygame.draw.rect(canvas, (60, 75, 110), band, 2, border_radius=14)
    head_surf = F_SEC.render(header, True, HEAD_FG)
    canvas.blit(head_surf, (band.x + 28, sec_y + 12))

    rects = {}
    rows_by_id = {}
    for sid, fname, title, col, row in screens:
        rect = tile_rect(col, row, sec_y)
        rects[sid] = rect
        rows_by_id[sid] = row
        draw_tile(canvas, sid,
                  os.path.join(SHOT_DIR, fname),
                  title, rect)

    # Highway y centres.
    row0_top = sec_y + SEC_HEAD + TOP_HWY + HEAD_BAR
    row0_bot = row0_top + TILE_H
    row1_top = row0_bot + MID_HWY + HEAD_BAR
    row1_bot = row1_top + TILE_H
    top_hy = sec_y + SEC_HEAD + TOP_HWY // 2 + 10
    mid_hy = (row0_bot + row1_top) // 2
    bot_hy = row1_bot + BOT_HWY // 2

    decisions, src_x_off, dst_x_off, src_y_off, dst_y_off = allocate_ports(
        arrows, screens)

    # Lane offsets: small per-arrow y stagger so parallel paths don't
    # share a y on the same highway. Group arrows by highway so each
    # group gets its own narrow lane staircase.
    by_highway = {"top": [], "mid": [], "bottom": [], "side": []}
    for idx, _ in enumerate(arrows):
        if idx not in decisions:
            continue
        by_highway[decisions[idx][0]].append(idx)
    lane_for = {}
    LANE_STEP = 9
    for hwy, idx_list in by_highway.items():
        n = len(idx_list)
        for i, idx in enumerate(idx_list):
            lane_for[idx] = (i - (n - 1) / 2) * LANE_STEP

    for idx, (src, dst, label, color_key) in enumerate(arrows):
        if idx not in decisions or src not in rects or dst not in rects:
            continue
        hwy, src_side, dst_side = decisions[idx]
        points = route_arrow(
            rects[src], rects[dst], src_side, dst_side, hwy,
            top_hy, mid_hy, bot_hy,
            src_x_off.get(idx, 0), dst_x_off.get(idx, 0),
            src_y_off.get(idx, 0), dst_y_off.get(idx, 0),
            lane_for.get(idx, 0),
        )
        color = arrow_blue if color_key == "blue" else arrow_yellow
        seg = 1 if len(points) == 4 else None
        draw_polyline_arrow(canvas, points, label, color, label_seg_idx=seg)


def main():
    canvas = pygame.Surface((CANVAS_W, CANVAS_H))
    canvas.fill(BG)

    head = F_HEAD.render(
        f"PEWPEW screen-flow & button map — v{pewpew.VERSION}",
        True, DIM)
    canvas.blit(head, (MARGIN, 20))

    legend = F_HEAD.render(
        "blue = primary flow   yellow = back / conditional / alternate   "
        "(highways: above row 0, between rows, below row 1)",
        True, DIM)
    canvas.blit(legend, (MARGIN, 44))

    sec_n_y = MARGIN + 14
    sec_g_y = sec_n_y + SEC_H + 40

    draw_section(canvas, "NORMAL MODE", NORMAL, NORMAL_ARROWS,
                 sec_n_y, SEC_NORMAL_BG, ARROW_BLUE, ARROW_YELLOW)
    draw_section(canvas, "GHOST MODE", GHOST, GHOST_ARROWS,
                 sec_g_y, SEC_GHOST_BG, ARROW_BLUE_GHOST, ARROW_YELLOW)

    foot = F_FOOT.render(MODE_TOGGLE, True, DIM)
    canvas.blit(foot, (MARGIN, CANVAS_H - 32))

    pygame.image.save(canvas, OUT_PATH)
    print(f"flow diagram -> {OUT_PATH}  ({CANVAS_W}x{CANVAS_H})")


if __name__ == "__main__":
    main()
