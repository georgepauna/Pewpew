"""Compose a single PNG showing the full screen flow + button mapping.

Two stacked sections — NORMAL MODE on top, GHOST MODE below — so the
user can see at a glance how every state looks and how the player
reaches it. Run after `_smoke.py` to refresh the source screenshots.

Layout
------
8 screens per section arranged on a circle at 45° intervals,
clockwise from 12 o'clock. The order follows the natural game-flow
progression so most transitions become short outer arcs:

  0 (top)    TITLE
  1          MAP
  2 (right)  SHOP
  3          PLAY
  4 (bot)    PAUSED / DEAD-PAUSE
  5          WIN
  6 (left)   LOSS / FAIL
  7          GAMEOVER / REWIND-UNLOCKED HUD

Routing
-------
- adjacent-on-circle (one step either direction) → quadratic Bezier
  arc that bulges *outward* past the tile ring, so reverse arrows
  bulge inward for a clean visual split
- non-adjacent → straight chord through the interior

Button labels reference the PC silk letters (this diagram renders on
Windows); in-game labels follow BUTTON_SCHEME and swap on the RG.
Face-position names (north/east/west/south) are used where the
binding is the same on every controller.
"""
import os, sys, math, itertools
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
TILE_W, TILE_H = 280, 210
RADIUS = 440
SEC_HEAD = 60
MARGIN = 70
RIM_MARGIN = 50

# Outer-arc lanes for non-adjacent arrows that would otherwise cross
# in the interior. Each promoted chord gets its own concentric lane.
OUTER_LANE_BASE = 1.34
OUTER_LANE_STEP = 0.11


def section_outer(lanes_used):
    """Distance from circle centre to the section's outer edge.
    Sized to the larger of (tile diagonal reach) and (outermost
    arc-lane radius)."""
    tile_reach = RADIUS + math.hypot(TILE_W, TILE_H) / 2
    if lanes_used > 0:
        ring = RADIUS * (OUTER_LANE_BASE
                         + OUTER_LANE_STEP * (lanes_used - 1))
    else:
        ring = 0
    return max(tile_reach, ring) + RIM_MARGIN

BG = (18, 22, 34)
SEC_NORMAL_BG = (24, 30, 48)
SEC_GHOST_BG = (32, 22, 42)
TILE_BORDER = (90, 105, 140)
HEAD_BAR = 26
HEAD_FG = (240, 240, 255)
ARROW_BLUE = (130, 200, 255)
ARROW_BLUE_GHOST = (235, 130, 200)
ARROW_YELLOW = (255, 210, 90)
LABEL_BG = (12, 16, 28)
LABEL_FG = (240, 240, 255)
DIM = (170, 180, 200)

# ── Circle order: (id, screenshot file, title), 8 slots clockwise ──
NORMAL = [
    ("title",   "title.png",       "TITLE"),
    ("map",     "map.png",         "MAP"),
    ("shop",    "shop.png",        "SHOP"),
    ("play",    "play.png",        "PLAY"),
    ("paused",  "play_paused.png", "PAUSED"),
    ("win",     "play_win.png",    "MISSION COMPLETE"),
    ("gameover","gameover.png",    "GAME OVER"),
]

GHOST = [
    ("title_g", "title_ghost.png",          "TITLE (Ghost)"),
    ("map_g",   "map_ghost.png",            "MAP (Ghost)"),
    ("shop_g",  "shop_ghost.png",           "SHOP (Ghost)"),
    ("play_g",  "play_ghost.png",           "PLAY (Ghost)"),
    ("deadp",   "play_ghost_dead_pause.png","DEAD-PAUSE PROMPT"),
    ("win_g",   "play_win.png",             "MISSION COMPLETE 100%"),
    ("fail_g",  "play_ghost_fail.png",      "MISSION FAILED (<100%)"),
    ("rewu",    "play_ghost_rewind_unlocked.png",
                                            "PLAY (rewind unlocked)"),
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
    ("play",  "gameover","(ship destroyed)",          "blue"),
    ("win",   "shop",    "fire continue",             "blue"),
    ("win",   "play",    "ability retry (partial)",   "yellow"),
    ("gameover","map",   "fire/cancel/start",         "blue"),
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
F_TITLE = font(16, bold=True)
F_LABEL = font(13)
F_FOOT = font(15)
F_HEAD = font(14)


# ── Order optimisation: minimise interior chord crossings ──────────
def order_score(perm, arrows):
    """Lower is better. Tuple (crossings, -arc_count): primary
    objective is to minimise interior-chord crossings; tiebreaker is
    maximum arc count (more adjacent-on-circle = more outer arcs =
    visually cleaner). `perm` is an ordered tuple of screen ids."""
    n = len(perm)
    position = {sid: i for i, sid in enumerate(perm)}
    chord_set = set()
    arc_count = 0
    for src, dst, *_ in arrows:
        if src not in position or dst not in position:
            continue
        a, b = position[src], position[dst]
        diff = min((a - b) % n, (b - a) % n)
        if diff == 1:
            arc_count += 1
        else:
            lo, hi = sorted((a, b))
            chord_set.add((lo, hi))
    chords = list(chord_set)
    crossings = 0
    for i in range(len(chords)):
        ai, bi = chords[i]
        for j in range(i + 1, len(chords)):
            aj, bj = chords[j]
            if (ai < aj < bi < bj) or (aj < ai < bj < bi):
                crossings += 1
    return (crossings, -arc_count)


def find_best_order(screens, arrows):
    """Brute force over (n-1)! permutations with screens[0] (=TITLE)
    locked at slot 0 (12 o'clock). Rotation symmetry is broken by
    the fixed slot, so the search space is just the orderings of the
    remaining screens. With n=8 that's 5040 — trivially fast."""
    n = len(screens)
    ids = [s[0] for s in screens]
    fixed = ids[0]
    others = ids[1:]
    best_score = None
    best_perm = None
    for tail in itertools.permutations(others):
        perm = (fixed,) + tail
        s = order_score(perm, arrows)
        if best_score is None or s < best_score:
            best_score = s
            best_perm = perm
    by_id = {s[0]: s for s in screens}
    return [by_id[sid] for sid in best_perm], best_score


def assign_outer_arcs(arrows, ordered_screens):
    """Greedily promote crossing interior chords to outer-arc lanes.

    After the ordering optimisation there can still be unavoidable
    interior crossings if the arrow set requires them. For each
    remaining crossing pair we pick the chord involved in the most
    crossings and route it around the OUTSIDE of the circle (long
    way), at a unique radius so multiple outer arcs don't cross each
    other.

    Returns {arrow_idx: lane_idx}. Adjacent-on-circle arrows are not
    eligible (they're already arcs)."""
    n = len(ordered_screens)
    position = {s[0]: i for i, s in enumerate(ordered_screens)}
    pair_arrows = {}
    for idx, (src, dst, *_) in enumerate(arrows):
        if src not in position or dst not in position:
            continue
        a, b = position[src], position[dst]
        diff = min((a - b) % n, (b - a) % n)
        if diff <= 1:
            continue
        pair = tuple(sorted((a, b)))
        pair_arrows.setdefault(pair, []).append(idx)

    outer_pairs = []
    while True:
        active = [p for p in pair_arrows if p not in outer_pairs]
        per_pair = {p: 0 for p in active}
        for i in range(len(active)):
            ai, bi = active[i]
            for j in range(i + 1, len(active)):
                aj, bj = active[j]
                if (ai < aj < bi < bj) or (aj < ai < bj < bi):
                    per_pair[active[i]] += 1
                    per_pair[active[j]] += 1
        if not per_pair or max(per_pair.values()) == 0:
            break
        worst = max(per_pair, key=lambda p: per_pair[p])
        outer_pairs.append(worst)

    out_assignments = {}
    for lane, pair in enumerate(outer_pairs):
        for arrow_idx in pair_arrows[pair]:
            out_assignments[arrow_idx] = lane
    return out_assignments


# ── Circle placement ────────────────────────────────────────────────
def slot_angle(i, n):
    """Angle for slot i (0 = 12 o'clock, clockwise). Returns radians."""
    return i * (2 * math.pi / n)


def slot_center(i, n, cx, cy):
    a = slot_angle(i, n)
    return (cx + RADIUS * math.sin(a), cy - RADIUS * math.cos(a))


def slot_rect(i, n, cx, cy):
    sx, sy = slot_center(i, n, cx, cy)
    return pygame.Rect(int(sx - TILE_W // 2), int(sy - TILE_H // 2),
                       TILE_W, TILE_H)


# ── Routing primitives ─────────────────────────────────────────────
def rect_edge_toward(rect, target):
    """Point on rect border closest to the line from rect.center to target."""
    cx, cy = rect.centerx, rect.centery
    tx, ty = target
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    # Project to the rect boundary along (dx, dy).
    sx = (TILE_W / 2) / max(abs(dx), 1e-6)
    sy = (TILE_H / 2) / max(abs(dy), 1e-6)
    t = min(sx, sy)
    return (cx + dx * t, cy + dy * t)


def quadratic_bezier(p0, p1, p2, steps=28):
    out = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        out.append((x, y))
    return out


def short_arc_diff(i, j, n):
    """Signed shortest step from i to j around the circle of n slots.
    Positive = clockwise. Returns int in [-(n//2), n//2]."""
    d = (j - i) % n
    if d > n // 2:
        d -= n
    return d


# ── Drawing ────────────────────────────────────────────────────────
def draw_tile(canvas, screen_id, shot_path, title, rect):
    hdr_rect = pygame.Rect(rect.x, rect.y - HEAD_BAR, rect.w, HEAD_BAR)
    pygame.draw.rect(canvas, (40, 50, 78), hdr_rect)
    pygame.draw.rect(canvas, TILE_BORDER, hdr_rect, 1)
    title_surf = F_TITLE.render(title, True, HEAD_FG)
    canvas.blit(title_surf, (hdr_rect.x + 8,
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


def draw_arrowhead(canvas, tip, prev, color, head=14):
    ang = math.atan2(tip[1] - prev[1], tip[0] - prev[0])
    a1 = (tip[0] - head * math.cos(ang - math.pi / 7),
          tip[1] - head * math.sin(ang - math.pi / 7))
    a2 = (tip[0] - head * math.cos(ang + math.pi / 7),
          tip[1] - head * math.sin(ang + math.pi / 7))
    pygame.draw.polygon(canvas, color, [tip, a1, a2])


def draw_label_chip(canvas, mx, my, label, color):
    if not label:
        return
    lsurf = F_LABEL.render(label, True, LABEL_FG)
    lw, lh = lsurf.get_size()
    chip = pygame.Rect(mx - lw // 2 - 7, my - lh // 2 - 4,
                       lw + 14, lh + 8)
    pygame.draw.rect(canvas, LABEL_BG, chip)
    pygame.draw.rect(canvas, color, chip, 1)
    canvas.blit(lsurf, (chip.x + 7, chip.y + 4))


def draw_arc_arrow(canvas, src_rect, dst_rect,
                   src_idx, dst_idx, n, circle_c,
                   bulge_outward, label, color):
    """Quadratic Bezier from src to dst, bulging away from (or toward)
    the circle centre to give forward / back arrows separate visual
    lanes."""
    a_src = slot_angle(src_idx, n)
    a_dst = slot_angle(dst_idx, n)
    mid = (a_src + a_dst) / 2
    # Wrap-around: if the two slots straddle 0/2pi the midpoint is
    # on the wrong side; offset by pi.
    if abs(a_dst - a_src) > math.pi:
        mid += math.pi
    bulge_r = RADIUS * (1.32 if bulge_outward else 0.45)
    ctrl = (circle_c[0] + bulge_r * math.sin(mid),
            circle_c[1] - bulge_r * math.cos(mid))

    sp = rect_edge_toward(src_rect, ctrl)
    dp = rect_edge_toward(dst_rect, ctrl)
    pts = quadratic_bezier(sp, ctrl, dp)
    pygame.draw.lines(canvas, color, False, pts, 3)
    draw_arrowhead(canvas, pts[-1], pts[-2], color)

    # Label near the arc apex.
    apex = pts[len(pts) // 2]
    draw_label_chip(canvas, int(apex[0]), int(apex[1]), label, color)


def draw_outer_arc_arrow(canvas, src_rect, dst_rect,
                         src_idx, dst_idx, n, circle_c,
                         lane_idx, label, color):
    """Long-way arc around the OUTSIDE of the circle, at a per-lane
    radius so multiple outer arcs sit at distinct distances and
    never cross each other."""
    a_src = slot_angle(src_idx, n)
    a_dst = slot_angle(dst_idx, n)
    short_mid = (a_src + a_dst) / 2
    if abs(a_dst - a_src) > math.pi:
        short_mid += math.pi
    long_mid = short_mid + math.pi
    bulge_r = RADIUS * (OUTER_LANE_BASE + OUTER_LANE_STEP * lane_idx)
    ctrl = (circle_c[0] + bulge_r * math.sin(long_mid),
            circle_c[1] - bulge_r * math.cos(long_mid))
    sp = rect_edge_toward(src_rect, ctrl)
    dp = rect_edge_toward(dst_rect, ctrl)
    pts = quadratic_bezier(sp, ctrl, dp, steps=36)
    pygame.draw.lines(canvas, color, False, pts, 3)
    draw_arrowhead(canvas, pts[-1], pts[-2], color)
    apex = pts[len(pts) // 2]
    draw_label_chip(canvas, int(apex[0]), int(apex[1]), label, color)


def draw_chord_arrow(canvas, src_rect, dst_rect, label, color, lane_offset=0):
    """Straight chord with optional perpendicular offset on the
    midpoint so parallel chords don't overlap their labels."""
    sp = rect_edge_toward(src_rect, dst_rect.center)
    dp = rect_edge_toward(dst_rect, src_rect.center)
    if lane_offset == 0:
        pygame.draw.line(canvas, color, sp, dp, 3)
        mid = ((sp[0] + dp[0]) / 2, (sp[1] + dp[1]) / 2)
    else:
        mx = (sp[0] + dp[0]) / 2
        my = (sp[1] + dp[1]) / 2
        ang = math.atan2(dp[1] - sp[1], dp[0] - sp[0])
        nx, ny = -math.sin(ang), math.cos(ang)
        ctrl = (mx + nx * lane_offset, my + ny * lane_offset)
        pts = quadratic_bezier(sp, ctrl, dp)
        pygame.draw.lines(canvas, color, False, pts, 3)
        mid = ctrl
    draw_arrowhead(canvas, dp, sp if lane_offset == 0 else pts[-2], color)
    draw_label_chip(canvas, int(mid[0]), int(mid[1]), label, color)


# ── Section renderer ────────────────────────────────────────────────
def draw_section(canvas, header, ordered, arrows, outer_assignments,
                 sec_y, sec_w, sec_h, outer, bg,
                 arrow_blue, arrow_yellow):
    band = pygame.Rect(MARGIN // 2, sec_y, sec_w - MARGIN, sec_h)
    pygame.draw.rect(canvas, bg, band, border_radius=14)
    pygame.draw.rect(canvas, (60, 75, 110), band, 2, border_radius=14)
    head_surf = F_SEC.render(header, True, HEAD_FG)
    canvas.blit(head_surf, (band.x + 28, sec_y + 12))

    cx = sec_w // 2
    cy = sec_y + SEC_HEAD + int(outer)
    circle_c = (cx, cy)

    rects = {}
    indices = {}
    n = len(ordered)
    for i, (sid, fname, title) in enumerate(ordered):
        rect = slot_rect(i, n, cx, cy)
        rects[sid] = rect
        indices[sid] = i
        draw_tile(canvas, sid,
                  os.path.join(SHOT_DIR, fname), title, rect)

    # Group same-pair arrows so forward/back arcs split on opposite
    # bulge sides.
    pair_counter = {}
    for idx, (src, dst, label, color_key) in enumerate(arrows):
        if src not in rects or dst not in rects:
            continue
        key = tuple(sorted((src, dst)))
        pair_counter.setdefault(key, []).append(idx)

    # Per-pair chord-lane offsets (for chord arrows that share a pair).
    chord_seen = {}
    for idx, (src, dst, _, _) in enumerate(arrows):
        if src not in rects or dst not in rects:
            continue
        si, di = indices[src], indices[dst]
        diff = abs(short_arc_diff(si, di, n))
        if diff > 1 and idx not in outer_assignments:
            chord_seen.setdefault((src, dst), []).append(idx)

    for idx, (src, dst, label, color_key) in enumerate(arrows):
        if src not in rects or dst not in rects:
            continue
        si, di = indices[src], indices[dst]
        diff = abs(short_arc_diff(si, di, n))
        color = arrow_blue if color_key == "blue" else arrow_yellow

        if diff == 1:
            pair_key = tuple(sorted((src, dst)))
            pair_idxs = pair_counter.get(pair_key, [idx])
            bulge_outward = pair_idxs[0] == idx
            draw_arc_arrow(canvas, rects[src], rects[dst],
                           si, di, n, circle_c,
                           bulge_outward, label, color)
        elif idx in outer_assignments:
            draw_outer_arc_arrow(canvas, rects[src], rects[dst],
                                 si, di, n, circle_c,
                                 outer_assignments[idx], label, color)
        else:
            duplicates = chord_seen.get((src, dst), [idx])
            n_dup = len(duplicates)
            j = duplicates.index(idx) if idx in duplicates else 0
            offset = (j - (n_dup - 1) / 2) * 28 if n_dup > 1 else 0
            draw_chord_arrow(canvas, rects[src], rects[dst],
                             label, color, lane_offset=offset)


def main():
    # ── Optimisation pass: find best ordering + outer-arc assignments
    #    for both modes BEFORE sizing the canvas, so we reserve only
    #    as much outer-lane space as we actually use.
    n_ordered, n_score = find_best_order(NORMAL, NORMAL_ARROWS)
    n_outer_arcs = assign_outer_arcs(NORMAL_ARROWS, n_ordered)
    g_ordered, g_score = find_best_order(GHOST, GHOST_ARROWS)
    g_outer_arcs = assign_outer_arcs(GHOST_ARROWS, g_ordered)

    print(f"[NORMAL] crossings={n_score[0]}  arcs={-n_score[1]}  "
          f"outer_lanes={len(set(n_outer_arcs.values()))}  "
          f"order={[s[0] for s in n_ordered]}")
    print(f"[GHOST]  crossings={g_score[0]}  arcs={-g_score[1]}  "
          f"outer_lanes={len(set(g_outer_arcs.values()))}  "
          f"order={[s[0] for s in g_ordered]}")

    max_lanes_n = (max(n_outer_arcs.values()) + 1) if n_outer_arcs else 0
    max_lanes_g = (max(g_outer_arcs.values()) + 1) if g_outer_arcs else 0
    outer_n = section_outer(max_lanes_n)
    outer_g = section_outer(max_lanes_g)

    sec_w = int(2 * max(outer_n, outer_g) + 2 * MARGIN)
    sec_h_n = int(SEC_HEAD + 2 * outer_n + 30)
    sec_h_g = int(SEC_HEAD + 2 * outer_g + 30)

    canvas_w = sec_w
    canvas_h = MARGIN + sec_h_n + sec_h_g + 40 + 70

    canvas = pygame.Surface((canvas_w, canvas_h))
    canvas.fill(BG)

    head = F_HEAD.render(
        f"PEWPEW screen-flow & button map — v{pewpew.VERSION}",
        True, DIM)
    canvas.blit(head, (MARGIN, 20))

    legend = F_HEAD.render(
        "blue = primary flow   yellow = back / conditional / alternate   "
        "(adjacent transitions arc outward; reverse arcs bulge inward; "
        "non-adjacent that don't cross route as chords; crossings "
        "escape to outer arcs)",
        True, DIM)
    canvas.blit(legend, (MARGIN, 44))

    sec_n_y = MARGIN + 14
    sec_g_y = sec_n_y + sec_h_n + 40

    draw_section(canvas, "NORMAL MODE", n_ordered, NORMAL_ARROWS,
                 n_outer_arcs, sec_n_y, sec_w, sec_h_n, outer_n,
                 SEC_NORMAL_BG, ARROW_BLUE, ARROW_YELLOW)
    draw_section(canvas, "GHOST MODE", g_ordered, GHOST_ARROWS,
                 g_outer_arcs, sec_g_y, sec_w, sec_h_g, outer_g,
                 SEC_GHOST_BG, ARROW_BLUE_GHOST, ARROW_YELLOW)

    foot = F_FOOT.render(MODE_TOGGLE, True, DIM)
    canvas.blit(foot, (MARGIN, canvas_h - 32))

    pygame.image.save(canvas, OUT_PATH)
    print(f"flow diagram -> {OUT_PATH}  ({canvas_w}x{canvas_h})")


if __name__ == "__main__":
    main()
