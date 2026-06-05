"""Compose a single PNG showing the full screen flow + button mapping.

The game has one mode (rewind-as-default, what used to be Ghost
Mode), so the diagram is a single radial section. Run after
`_smoke.py` to refresh the source screenshots.

Layout
------
8 screens placed on a circle at 45° intervals, clockwise from
12 o'clock. The order is brute-forced (n-1)! to minimise interior
chord crossings, with TITLE locked at slot 0 (12 o'clock) to break
rotation symmetry. If the optimiser can't avoid all crossings, the
worst-offender chords get promoted to outer-arc lanes that ride
unique radii outside the tile ring.

Routing
-------
- adjacent-on-circle (one step either direction) → quadratic Bezier
  arc that bulges *outward* past the tile ring; reverse arrows on
  the same pair bulge inward for a clean visual split
- non-adjacent → straight chord through the interior (unless
  promoted to an outer-arc lane to escape crossings)

Button labels use the Xbox face-button convention:
  A = south (fire),  B = east (bomb),
  X = west (ability), Y = north (cancel).
Same physical position on every controller — only the silk letters
differ on the RG (south=B, east=A, west=Y, north=X), so "A on
Continue" on this diagram means the south face button, which is
silk B on the RG hardware.
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
TILE_W, TILE_H = 380, 285
RADIUS = 460
SEC_HEAD = 60
MARGIN = 70
RIM_MARGIN = 50

# Outer-arc lanes for non-adjacent arrows that would otherwise cross
# in the interior. Each promoted chord gets its own concentric lane.
OUTER_LANE_BASE = 1.34
OUTER_LANE_STEP = 0.11


def section_outer(lanes_used):
    tile_reach = RADIUS + math.hypot(TILE_W, TILE_H) / 2
    if lanes_used > 0:
        ring = RADIUS * (OUTER_LANE_BASE
                         + OUTER_LANE_STEP * (lanes_used - 1))
    else:
        ring = 0
    return max(tile_reach, ring) + RIM_MARGIN


BG = (18, 22, 34)
SEC_BG = (28, 26, 46)
TILE_BORDER = (90, 105, 140)
HEAD_BAR = 26
HEAD_FG = (240, 240, 255)
ARROW_BLUE = (130, 200, 255)
ARROW_YELLOW = (255, 210, 90)
ARROW_RED = (255, 110, 110)   # flags a transition with no key in this scheme
LABEL_BG = (12, 16, 28)
LABEL_FG = (240, 240, 255)
DIM = (170, 180, 200)

# ── 8 slots — TITLE locked at slot 0 by the optimiser ───────────────
SCREENS = [
    ("title",   "title.png",                "TITLE"),
    ("map",     "map.png",                  "MAP"),
    ("shop",    "shop.png",                 "SHOP"),
    ("play",    "play.png",                 "PLAY"),
    ("paused",  "play_paused.png",          "PAUSED"),
    ("deadp",   "play_dead_pause.png",      "DEAD-PAUSE PROMPT"),
    ("win",     "play_win.png",             "MISSION COMPLETE 100%"),
    ("fail",    "play_fail.png",            "MISSION FAILED (<100%)"),
    ("rewu",    "play_rewind_unlocked.png", "PLAY (rewind unlocked)"),
]
# Optimiser hits combinatorial limits beyond ~8 nodes; trim the
# rewind-unlocked HUD variant from the circle (it's a chrome change
# of PLAY, not a separate state — surfaced in the footer note).
SCREENS = [s for s in SCREENS if s[0] != "rewu"]

# Each arrow carries BOTH a controller label and a keyboard/mouse label,
# so the same flow renders twice with input-appropriate annotations:
#   (src, dst, controller_label, keyboard_label, color)
# Controller letters are Xbox-style (A=south, B=east, X=west, Y=north).
# Keyboard/mouse mapping (v0.9.273): fire = Mouse-1/Numpad-2/Enter,
# east(rewind/exit) = Space/Numpad-0, west(ability) = C, pause = Esc.
# NORTH (cancel) has NO keyboard key — the MAP->SHOP toggle is therefore
# pad-only; the keyboard diagram flags it in red + the footer.
ARROWS = [
    # GO (South) forward chain: title -> map -> play -> shop -> map -> play.
    ("title",   "map",     "A: Continue / New Game",  "Enter / Space",           "blue"),
    ("map",     "play",    "A on level node",         "Enter / Space",           "blue"),
    ("win",     "shop",    "A: continue",             "Enter / Space",           "blue"),
    ("shop",    "map",     "A: ready / launch",       "Enter / Space",           "blue"),
    # BACK (East) chain: map -> shop -> title.
    ("map",     "shop",    "B: back",                 "Backspace",               "yellow"),
    ("shop",    "title",   "B: back",                 "Backspace",               "yellow"),
    # Play <-> pause / banners.
    ("play",    "paused",  "START",                   "Esc",                     "blue"),
    ("paused",  "play",    "START resume",            "Esc resume",              "yellow"),
    ("paused",  "map",     "Y: abort",                "Q",                       "yellow"),
    ("play",    "deadp",   "(1-hit kill)",            "(1-hit kill)",            "blue"),
    ("deadp",   "play",    "B hold = REWIND",         "Space hold = REWIND",     "blue"),
    ("deadp",   "shop",    "Y: give up",              "Q: give up"   ,            "yellow"),
    ("play",    "win",     "(level end 100%)",        "(level end 100%)",        "blue"),
    ("play",    "fail",    "(level end <100%)",       "(level end <100%)",       "blue"),
    ("win",     "play",    "X: replay level",         "E: replay",               "yellow"),
    ("fail",    "play",    "X: retry",                "E: retry",                "yellow"),
    ("fail",    "play",    "B hold = rewind",         "Space hold = rewind",     "yellow"),
    ("fail",    "shop",    "Y: give up",              "Q: give up"   ,            "yellow"),
    ("map",     "play",    "X: watch saved replay",   "E (if saved)",            "yellow"),
]

# Label-tuple index per scheme + per-scheme header / footer. CTRL uses
# index 2, KBM index 3; color is always index 4.
CTRL_LABEL, KB_LABEL, COLOR_IDX = 2, 3, 4

FOOTER_CTRL = ("Face buttons Xbox-style - A=south (GO/shoot), B=east (BACK/rewind), "
               "X=west (other: replay/retry/buy), Y=north (other: abort/give-up); "
               "same physical positions on the RG, only silk letters differ "
               "(RG = B/A/Y/X).  GO = forward, BACK (East) = map>shop>title.")
FOOTER_KB = ("Keyboard/mouse (v" + pewpew.VERSION + "), CONTEXT-AWARE: MENUS go=Enter/Space/LMB, "
             "back=Backspace, west=E, north=Q, pause=Esc, wheel=scroll cursor.  PLAY shoot=Num1/O/"
             "LMB, rail=Num2/I/wheel, ball=Num3/P/RMB, rewind=Space/MMB(hold), north=Q.  Same key "
             "can mean different things per context (Space=go in menus, rewind in play).")
SCHEMES = [
    ("controller", "CONTROLLER",      CTRL_LABEL, FOOTER_CTRL),
    ("keyboard",   "KEYBOARD + MOUSE", KB_LABEL,  FOOTER_KB),
]

# ── Fonts ───────────────────────────────────────────────────────────
def font(size, bold=False):
    return pygame.font.SysFont("consolas", size, bold=bold)


F_SEC = font(38, bold=True)
F_TITLE = font(17, bold=True)
F_LABEL = font(13)
F_FOOT = font(15)
F_HEAD = font(14)


# ── Order optimisation ──────────────────────────────────────────────
def order_score(perm, arrows):
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
    cx, cy = rect.centerx, rect.centery
    tx, ty = target
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
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
    a_src = slot_angle(src_idx, n)
    a_dst = slot_angle(dst_idx, n)
    mid = (a_src + a_dst) / 2
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
    apex = pts[len(pts) // 2]
    draw_label_chip(canvas, int(apex[0]), int(apex[1]), label, color)


def draw_outer_arc_arrow(canvas, src_rect, dst_rect,
                         src_idx, dst_idx, n, circle_c,
                         lane_idx, label, color):
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
    sp = rect_edge_toward(src_rect, dst_rect.center)
    dp = rect_edge_toward(dst_rect, src_rect.center)
    if lane_offset == 0:
        pygame.draw.line(canvas, color, sp, dp, 3)
        mid = ((sp[0] + dp[0]) / 2, (sp[1] + dp[1]) / 2)
        draw_arrowhead(canvas, dp, sp, color)
    else:
        mx = (sp[0] + dp[0]) / 2
        my = (sp[1] + dp[1]) / 2
        ang = math.atan2(dp[1] - sp[1], dp[0] - sp[0])
        nx, ny = -math.sin(ang), math.cos(ang)
        ctrl = (mx + nx * lane_offset, my + ny * lane_offset)
        pts = quadratic_bezier(sp, ctrl, dp)
        pygame.draw.lines(canvas, color, False, pts, 3)
        mid = ctrl
        draw_arrowhead(canvas, pts[-1], pts[-2], color)
    draw_label_chip(canvas, int(mid[0]), int(mid[1]), label, color)


# ── Section renderer ────────────────────────────────────────────────
def draw_section(canvas, header, ordered, arrows, outer_assignments,
                 sec_y, sec_w, sec_h, outer, bg,
                 arrow_blue, arrow_yellow, label_idx):
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

    pair_counter = {}
    for idx, arr in enumerate(arrows):
        src, dst = arr[0], arr[1]
        if src not in rects or dst not in rects:
            continue
        key = tuple(sorted((src, dst)))
        pair_counter.setdefault(key, []).append(idx)

    chord_seen = {}
    for idx, arr in enumerate(arrows):
        src, dst = arr[0], arr[1]
        if src not in rects or dst not in rects:
            continue
        si, di = indices[src], indices[dst]
        diff = abs(short_arc_diff(si, di, n))
        if diff > 1 and idx not in outer_assignments:
            chord_seen.setdefault((src, dst), []).append(idx)

    for idx, arr in enumerate(arrows):
        src, dst = arr[0], arr[1]
        color_key, label = arr[COLOR_IDX], arr[label_idx]
        if src not in rects or dst not in rects:
            continue
        si, di = indices[src], indices[dst]
        diff = abs(short_arc_diff(si, di, n))
        color = arrow_blue if color_key == "blue" else arrow_yellow
        # A transition with no key in this scheme is flagged red.
        if "NO keyboard key" in label:
            color = ARROW_RED

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


def render_scheme(scheme_id, scheme_name, label_idx, footer):
    """Render one input-variant diagram (controller or keyboard+mouse).
    The circle layout is identical across variants — only the arrow
    labels, header and footer change."""
    ordered, score = find_best_order(SCREENS, ARROWS)
    outer_arcs = assign_outer_arcs(ARROWS, ordered)
    print(f"[{scheme_id}] crossings={score[0]}  arcs={-score[1]}  "
          f"outer_lanes={len(set(outer_arcs.values()))}  "
          f"order={[s[0] for s in ordered]}")

    max_lanes = (max(outer_arcs.values()) + 1) if outer_arcs else 0
    outer = section_outer(max_lanes)

    sec_w = int(2 * outer + 2 * MARGIN)
    sec_h = int(SEC_HEAD + 2 * outer + 30)
    canvas_w = sec_w
    canvas_h = MARGIN + sec_h + 80

    canvas = pygame.Surface((canvas_w, canvas_h))
    canvas.fill(BG)

    head = F_HEAD.render(
        f"PEWPEW screen-flow & input map ({scheme_name}) — v{pewpew.VERSION}",
        True, DIM)
    canvas.blit(head, (MARGIN, 20))

    legend = F_HEAD.render(
        "blue = primary flow   yellow = back / conditional / alternate   "
        "red = no key in this scheme   "
        "(adjacent transitions arc outward; reverse arcs bulge inward)",
        True, DIM)
    canvas.blit(legend, (MARGIN, 44))

    sec_y = MARGIN + 14
    draw_section(canvas, "SCREEN FLOW", ordered, ARROWS,
                 outer_arcs, sec_y, sec_w, sec_h, outer,
                 SEC_BG, ARROW_BLUE, ARROW_YELLOW, label_idx)

    foot = F_FOOT.render(footer, True, DIM)
    canvas.blit(foot, (MARGIN, canvas_h - 32))

    out_path = os.path.join(SHOT_DIR, f"flow_diagram_{scheme_id}.png")
    pygame.image.save(canvas, out_path)
    print(f"  -> {out_path}  ({canvas_w}x{canvas_h})")


def main():
    for scheme_id, scheme_name, label_idx, footer in SCHEMES:
        render_scheme(scheme_id, scheme_name, label_idx, footer)


if __name__ == "__main__":
    main()
