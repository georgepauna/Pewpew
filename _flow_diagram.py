"""Compose a single PNG showing the full screen flow + button mapping.

Two stacked sections — NORMAL MODE on top, GHOST MODE below — so the
user can see at a glance how every state looks and how the player
reaches it. Run after `_smoke.py` to refresh the source screenshots.

Arrows route orthogonally (L-shapes) through a horizontal "highway"
that runs between the two tile rows in each section, so no arrow
cuts across a tile body. Per-tile port allocation spreads the
attachment points along the top / bottom edges, and per-arrow
highway lanes keep parallel paths visually distinct.

Button labels reference the PC silk letters (the diagram is
generated on Windows); the in-game labels follow BUTTON_SCHEME and
swap on the RG. Where the gamepad input is named by face position
rather than letter (north/east/west/south), the label uses that —
same physical position on every controller.
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
TILE_W, TILE_H = 380, 285   # source 640x480 × ~0.594
COL_GAP = 70
ROW_GAP = 200               # vertical highway lives in here
HEAD_BAR = 30
SEC_HEAD = 60
MARGIN = 70

COLS = 5

SEC_W = MARGIN * 2 + COLS * TILE_W + (COLS - 1) * COL_GAP
SEC_H = (SEC_HEAD + HEAD_BAR + TILE_H + ROW_GAP
         + HEAD_BAR + TILE_H + MARGIN)

CANVAS_W = SEC_W
CANVAS_H = MARGIN + 2 * SEC_H + 60

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
# col 0..4, row 0..1 within the section.
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

# Arrows: (src_id, dst_id, label, color_key)
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
    y = sec_y + SEC_HEAD + HEAD_BAR + row * (TILE_H + ROW_GAP + HEAD_BAR)
    return x, y


def tile_rect(col, row, sec_y):
    x, y = tile_xy(col, row, sec_y)
    return pygame.Rect(x, y, TILE_W, TILE_H)


# ── Port allocator ──────────────────────────────────────────────────
def allocate_ports(arrows, screens):
    """For each arrow, return (src_x_offset, dst_x_offset) relative to
    the centre of the src tile's exit edge and dst tile's entry edge.

    Each tile has at most ~5 arrows attached to a given side. We
    spread them along the side using equal spacing so attachments
    don't pile up. Arrows exit row-0 tiles via the bottom edge and
    row-1 tiles via the top edge (so they all enter the horizontal
    highway between rows). Same on the receiving end.

    For same-row arrows that route directly side-to-side (adjacent
    columns), we use the side edges instead — but the allocator
    doesn't distinguish; the renderer overrides the attachment for
    that case."""
    rows = {sid: row for sid, *_, _, row in
            [(s[0], s[3], s[4]) for s in screens]}

    out_edge = {sid: [] for sid, *_ in screens}
    in_edge = {sid: [] for sid, *_ in screens}

    for idx, (src, dst, _, _) in enumerate(arrows):
        if src in rows:
            out_edge[src].append(idx)
        if dst in rows:
            in_edge[dst].append(idx)

    def spread(buckets):
        result = {}
        for tile_id, indices in buckets.items():
            n = len(indices)
            if n == 0:
                continue
            # Spread across 60% of tile width, centred.
            span = TILE_W * 0.6
            step = span / max(n, 1)
            start = -span / 2 + step / 2
            for i, idx in enumerate(indices):
                result[idx] = start + i * step
        return result

    src_x = spread(out_edge)
    dst_x = spread(in_edge)
    return src_x, dst_x


# ── Orthogonal router ───────────────────────────────────────────────
def route_orthogonal(src_rect, dst_rect, src_row, dst_row,
                     src_x_off, dst_x_off, highway_y, lane_y_off,
                     allow_side_direct=True):
    """Return a polyline (list of points) from src to dst.

    - Same row, adjacent columns: direct side-to-side horizontal.
    - Otherwise: down/up into the horizontal highway between rows,
      across, then up/down into dst."""

    horiz_dist = abs(dst_rect.centerx - src_rect.centerx)
    adjacent = horiz_dist < (TILE_W + COL_GAP) * 1.5

    if allow_side_direct and src_row == dst_row and adjacent:
        # Side-to-side direct line, with vertical lane stagger.
        sy = src_rect.centery + lane_y_off
        dy = dst_rect.centery + lane_y_off
        if dst_rect.centerx > src_rect.centerx:
            sp = (src_rect.right, sy)
            dp = (dst_rect.left, dy)
        else:
            sp = (src_rect.left, sy)
            dp = (dst_rect.right, dy)
        return [sp, dp]

    # Highway route: exit via top/bottom edge, hop into highway, exit
    # to dst's bottom/top edge.
    sp = (src_rect.centerx + src_x_off,
          src_rect.bottom if src_row == 0 else src_rect.top)
    dp = (dst_rect.centerx + dst_x_off,
          dst_rect.top if dst_row == 0 else dst_rect.bottom)
    hy = highway_y + lane_y_off
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
    # Arrowhead
    last = points[-1]
    prev = points[-2]
    ang = math.atan2(last[1] - prev[1], last[0] - prev[0])
    head = 15
    a1 = (last[0] - head * math.cos(ang - math.pi / 7),
          last[1] - head * math.sin(ang - math.pi / 7))
    a2 = (last[0] - head * math.cos(ang + math.pi / 7),
          last[1] - head * math.sin(ang + math.pi / 7))
    pygame.draw.polygon(canvas, color, [last, a1, a2])
    # Label chip — place at midpoint of the longest segment so it
    # has room. Or use the explicit `label_seg_idx` segment between
    # points[label_seg_idx] and points[label_seg_idx+1].
    if not label:
        return
    if label_seg_idx is None:
        # Pick the segment with the greatest length.
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
    chip = pygame.Rect(mx - lw // 2 - 7,
                       my - lh // 2 - 4,
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

    # Compute highway y — middle of the gap between row 0 bottom and
    # row 1 top.
    row0_y = sec_y + SEC_HEAD + HEAD_BAR
    row0_bot = row0_y + TILE_H
    row1_y = row0_bot + ROW_GAP + HEAD_BAR
    highway_y = (row0_bot + row1_y) // 2

    src_x_off, dst_x_off = allocate_ports(arrows, screens)

    # Per-arrow lane offset on the highway so parallel paths through
    # the same x range don't sit on the same y. Use a small staircase
    # that wraps every 6 arrows.
    LANE_Y_STEP = 9
    LANE_COUNT = 7
    for idx, (src, dst, label, color_key) in enumerate(arrows):
        if src not in rects or dst not in rects:
            continue
        lane = (idx % LANE_COUNT) - (LANE_COUNT - 1) // 2
        lane_y = lane * LANE_Y_STEP
        sp_off = src_x_off.get(idx, 0)
        dp_off = dst_x_off.get(idx, 0)
        points = route_orthogonal(
            rects[src], rects[dst],
            rows_by_id[src], rows_by_id[dst],
            sp_off, dp_off, highway_y, lane_y,
        )
        color = arrow_blue if color_key == "blue" else arrow_yellow
        # For 4-point highway routes the long segment is the
        # horizontal middle one (index 1); use it for the label.
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
        "blue = primary flow   yellow = back / conditional / alternate",
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
