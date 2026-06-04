"""Render the COMPLETE controls reference as two images — one for
keyboard+mouse, one for controller — showing every screen and, per
screen, every input that does something + whether/how it is shown
on-screen as a hint.

This module's CONTROLS table is the single authoritative source of
truth for the binding documentation (see the [[project-keyboard-bindings]]
+ [[feedback-button-scheme]] memories). Two layers are kept distinct:
MENU screens vs in-GAME sub-states, plus a GLOBAL section.

Run:  python _controls_map.py   ->   screenshots/controls_map_{keyboard,controller}.png
"""
import os, sys
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
import pygame
pygame.init()
pygame.display.set_mode((64, 64), pygame.HIDDEN)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pewpew

SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")

# Each row: (action, keyboard/mouse, controller, on-screen-hint or None, dev?)
# hint=None  -> NOT shown on screen.  hint="..."  -> shown; text describes
# how/where it appears + how the action is named.
def R(act, kb, ctrl, hint=None, dev=False):
    return {"act": act, "kb": kb, "ctrl": ctrl, "hint": hint, "dev": dev}

SCREENS = [
    # ───────────────────────── GLOBAL ─────────────────────────
    {"id": "global", "name": "GLOBAL  (any screen)", "layer": "GLOBAL", "rows": [
        R("Master volume +/-", "+ / =   and   -", "RG hardware vol keys", "transient volume bar"),
        R("Quit game", "Alt+F4 / window X", "SELECT+START  ·  Home(RG)", None),
    ]},

    # ───────────────────────── MENU LAYER ─────────────────────
    {"id": "title", "name": "TITLE  (menu)", "layer": "MENU", "rows": [
        R("Move cursor", "W/S  ·  Up/Down", "D-pad / stick", 'tip: "{fire} confirm | dpad select"'),
        R("Confirm option", "Enter / Num2 / LMB", "South", 'tip: "{fire} confirm"'),
        R("Confirm (alt)", "Esc", "START", None),
        R("Jump cursor to Quit", "Space / Num0", "East", None),
        R("SOUND/MUSIC nudge", "Left/Right · A/D", "D-pad L/R", 'live "NN%" on row'),
        R("Prev profile", "Q", "L1", 'profile line: "< L1"'),
        R("Next profile", "E", "R1", 'profile line: "R1 >"'),
        R("Open notes / install update", "C", "West", '"({ability})" when update ready'),
        R("Cycle scale mode", "Tab", "SEL + West", 'btm-right "SEL+{ability}: scale"'),
        R("Cycle FPS lock", "Shift+Space", "SEL + East", 'btm-right "SEL+{bomb}: fps"'),
        R("Toggle update channel", "Shift+Esc", "SEL + START", None),
        R("Test mission", "Shift+Tab", "SEL + North", None, dev=True),
        R("Play bot replay", "—  (no kb path)", "L2/R2 + D-pad dir", None, dev=True),
    ]},
    {"id": "title_notes", "name": "TITLE · update-notes overlay", "layer": "MENU", "rows": [
        R("Scroll", "W/S · Up/Down", "D-pad / stick", 'footer "D-pad scroll"'),
        R("Page up / down", "PgUp / PgDn", "L1/L2 · R1/R2", None),
        R("Dismiss", "Enter/Space/Z/Esc", "South · START", 'footer "{fire}: close"'),
        R("Install update", "X  (or C)", "West", 'footer "{ability}: install"'),
    ]},
    {"id": "title_ow", "name": "TITLE · OVERWRITE? modal", "layer": "MENU", "rows": [
        R("Confirm wipe", "Tab", "North", 'modal: "{cancel} to confirm"'),
        R("Cancel", "Enter / LMB / Esc", "South · START", 'modal: "{fire} to cancel"'),
    ]},
    {"id": "map", "name": "MAP  (menu)", "layer": "MENU", "rows": [
        R("Move cursor (nearest node)", "WASD · arrows", "D-pad / stick", "cursor ring (no text)"),
        R("Play cursored level", "Enter / Num2 / LMB", "South", 'CONTROL: {fire} "play"'),
        R("Open shop  (map<->shop)", "Tab", "North", 'CONTROL: {cancel} "shop"'),
        R("Level details overlay", "C", "West", 'CONTROL: {ability} "details"'),
        R("Back to title", "Space/Num0 · Esc", "East · START", 'CONTROL: {bomb} "title"'),
        R("Prev / next sector", "Q / E", "L1/L2 · R1/R2", '"< L"  /  "R >"'),
        R("Play bot replay", "—  (no kb path)", "L2/R2 + D-pad dir", None, dev=True),
        R("Unlock+complete all", "Ctrl+U", "SEL + West", None, dev=True),
    ]},
    {"id": "map_det", "name": "MAP · level-details overlay", "layer": "MENU", "rows": [
        R("Watch saved replay", "Enter / LMB", "South", 'footer "{fire} watch" (if saved)'),
        R("Close", "Tab / C / Space / Enter", "any face", '"{ability} close" / "any button"'),
    ]},
    {"id": "map_tune", "name": "MAP · layer-tune overlay", "layer": "MENU", "rows": [
        R("Enter tune mode", "hold Shift", "hold SELECT", 'overlay "RS up/down | B swap"'),
        R("Nudge layer volume", "—", "Right-stick up/down", 'overlay "RS up/down"'),
        R("Swap layer below", "Space/Num0", "East", 'overlay "B swap-below"'),
    ]},
    {"id": "shop", "name": "SHOP  (menu)", "layer": "MENU", "rows": [
        R("Move cursor", "W/S · Up/Down", "D-pad / stick", "row highlight (no text)"),
        R("Buy / upgrade (tap)", "tap C", "tap West", 'CONTROL: {ability} "tap buy"'),
        R("Downgrade / refund (hold .45s)", "hold C", "hold West", 'CONTROL: "hold {ability}: refund tier"'),
        R("To map (ready/launch)", "Enter / Num2 / LMB", "South", 'CONTROL: {fire} "map"'),
        R("To map (back)", "Tab", "North", 'CONTROL: {cancel} "map"'),
        R("Back to title", "Space/Num0 · Esc", "East · START", 'CONTROL: {bomb} "title"'),
        R("Skip unlock cascade", "any of above", "any face", "cells flash 'T# UNLOCKED'"),
    ]},
    {"id": "gameover", "name": "GAME OVER  (menu)", "layer": "MENU", "rows": [
        R("Return to map", "Enter/Num2/LMB · Tab · Esc", "South · North · START", 'blinking "{fire} return to map"'),
    ]},

    # ───────────────────────── GAME LAYER ─────────────────────
    {"id": "play", "name": "PLAY  (active)", "layer": "GAME", "rows": [
        R("Move ship", "WASD · arrows", "D-pad / stick", 'HUD: {dpad} "move"'),
        R("Fire (Vulcan = default)", "Enter / Num2 / LMB", "South", 'HUD: {fire} "fire"'),
        R("Rail (swap + fire)", "Q / Num1 / Wheel-up", "L1 / L2", None),
        R("Ball (swap + charge/fire)", "E / Num3 / RMB-hold", "R1 / R2", None),
        R("Detonate ball in flight", "re-press E / RMB", "R1 / R2", None),
        R("Rewind time (hold)", "hold Space / Num0", "hold East", 'HUD: {bomb} "rewind" *hidden until unlocked'),
        R("Pause", "Esc", "START", 'HUD: ST "pause"'),
        R("Perf/debug overlay cycle", "—", "R3", None, dev=True),
        R("Instant-clear cheat", "—  (no kb path)", "SEL + L2 + R2", None, dev=True),
    ]},
    {"id": "paused", "name": "PLAY · PAUSED", "layer": "GAME", "rows": [
        R("Resume", "Esc", "START", 'banner "START continue"'),
        R("Abort to map", "C", "West", 'banner "{ability} abort"'),
        R("(East deliberately unbound)", "—", "—", None),
    ]},
    {"id": "deadp", "name": "PLAY · DEAD-PAUSE (1-hit kill)", "layer": "GAME", "rows": [
        R("Rewind out (always honored)", "hold Space / Num0", "hold East", 'big "HOLD {bomb} TO REWIND"'),
        R("Give up", "Esc", "START", '"(START to give up)"'),
    ]},
    {"id": "win", "name": "PLAY · MISSION COMPLETE 100%", "layer": "GAME", "rows": [
        R("Continue to shop", "Enter / Num2 / LMB", "South", '"{fire} continue"'),
        R("Replay level", "Tab", "North", '"{cancel} replay level" (if recorded)'),
        R("Rewind into sim (hold)", "hold Space / Num0", "hold East", '"hold {bomb} to rewind" (if unlocked)'),
    ]},
    {"id": "fail", "name": "PLAY · MISSION FAILED <100%", "layer": "GAME", "rows": [
        R("Give up -> game over", "Enter / Num2 / LMB", "South", '"{fire} give up"'),
        R("Retry level", "C", "West", '"{ability} retry"'),
        R("Rewind into sim (always honored)", "hold Space / Num0", "hold East", '"hold {bomb} to rewind"'),
    ]},
    {"id": "replay", "name": "PLAY · REPLAY view", "layer": "GAME", "rows": [
        R("Jog / shuttle speed", "W/S · Up/Down", "stick / D-pad", '"up/down speed"'),
        R("Continue / exit to shop", "Enter / Num2 / LMB", "South", '"{fire} continue"'),
        R("Exit replay", "Space / Num0", "East", '"{bomb} exit"'),
        R("Save replay", "C", "West", '"{ability} save" (if saveable)'),
        R("Pause / resume", "Tab", "North", '"{cancel} pause/resume"'),
    ]},
    {"id": "youwin", "name": "PLAY · YOU WIN (game complete)", "layer": "GAME", "rows": [
        R("Keep flying (move + fire)", "WASD + fire keys", "D-pad + South", "free-flight, no hint"),
        R("Dismiss to title", "Esc", "START", '"START to exit" (after grace)'),
    ]},
    {"id": "test", "name": "PLAY · TEST MISSION menu", "layer": "GAME", "rows": [
        R("Move between rows", "W/S · Up/Down", "D-pad / stick", "active row shows < val >"),
        R("Change value", "A/D · Left/Right", "D-pad L/R", "live < value >"),
        R("Close menu", "Esc / Enter", "START / South", 'footer "START/{fire} close"'),
        R("Abort to map", "C", "West", 'footer "{ability} abort"'),
        R("Prev/next wave or boss", "—", "Right-stick L/R", "transient banner", dev=True),
    ], "dev": True},
]

# ── colours ─────────────────────────────────────────────────────────
BG        = (18, 22, 34)
CARD_BG   = (28, 32, 50)
CARD_DEV  = (34, 28, 40)
HEAD_BG   = {"GLOBAL": (70, 60, 40), "MENU": (40, 55, 85), "GAME": (60, 40, 70)}
HEAD_FG   = (240, 240, 255)
BORDER    = (80, 95, 130)
INPUT_FG  = (255, 210, 110)
ACT_FG    = (235, 238, 248)
HINT_FG   = (120, 220, 150)     # shown on-screen
NOHINT_FG = (120, 128, 150)     # not shown
DEV_FG    = (180, 150, 200)
DIM       = (170, 180, 200)
DOT_ON    = (110, 220, 140)
DOT_OFF   = (90, 98, 120)

def font(sz, bold=False):
    return pygame.font.SysFont("consolas", sz, bold=bold)

F_H1   = font(26, bold=True)
F_LEG  = font(15)
F_CARD = font(15, bold=True)
F_ROW  = font(13)
F_HINT = font(12)
F_FOOT = font(14)

# ── layout geometry ─────────────────────────────────────────────────
CARD_W   = 620
COL_GAP  = 26
MARGIN   = 34
TOP      = 96
HEAD_H   = 30
ROW_H    = 38          # two text lines per row
PAD      = 10
N_COLS   = 3

C_INPUT_X = 12
C_ACT_X   = 232
ROW_W     = CARD_W - 24

def trunc(surf_font, text, max_w):
    if surf_font.size(text)[0] <= max_w:
        return text
    while text and surf_font.size(text + "...")[0] > max_w:
        text = text[:-1]
    return text + "..."

def card_height(card):
    return HEAD_H + len(card["rows"]) * ROW_H + PAD

def render(scheme_key, scheme_name, btn_letters, footer, out_name):
    # Greedy masonry: place each card in the currently-shortest column.
    col_h = [TOP] * N_COLS
    placed = []
    for card in SCREENS:
        c = min(range(N_COLS), key=lambda i: col_h[i])
        x = MARGIN + c * (CARD_W + COL_GAP)
        y = col_h[c]
        placed.append((card, x, y))
        col_h[c] = y + card_height(card) + COL_GAP

    canvas_w = MARGIN * 2 + N_COLS * CARD_W + (N_COLS - 1) * COL_GAP
    canvas_h = max(col_h) + 70
    canvas = pygame.Surface((canvas_w, canvas_h))
    canvas.fill(BG)

    canvas.blit(F_H1.render(
        f"PEWPEW controls — {scheme_name}  ·  v{pewpew.VERSION}", True, HEAD_FG),
        (MARGIN, 24))
    canvas.blit(F_LEG.render(
        "green dot = shown on-screen (hint text = how it's named/where)   "
        "grey dot = no on-screen hint   purple = hidden/dev   "
        "{fire}/{bomb}/{ability}/{cancel} = face-button letter",
        True, DIM), (MARGIN, 58))

    for card, x, y in placed:
        h = card_height(card)
        dev_card = card.get("dev")
        pygame.draw.rect(canvas, CARD_DEV if dev_card else CARD_BG,
                         (x, y, CARD_W, h), border_radius=10)
        pygame.draw.rect(canvas, BORDER, (x, y, CARD_W, h), 2, border_radius=10)
        # header
        hb = pygame.Rect(x, y, CARD_W, HEAD_H)
        pygame.draw.rect(canvas, HEAD_BG[card["layer"]], hb,
                         border_top_left_radius=10, border_top_right_radius=10)
        canvas.blit(F_CARD.render(card["name"], True, HEAD_FG), (x + 12, y + 6))
        lay = F_HINT.render(card["layer"], True, (210, 210, 230))
        canvas.blit(lay, (x + CARD_W - lay.get_width() - 12, y + 9))
        # rows
        ry = y + HEAD_H + 4
        for row in card["rows"]:
            inp = row[scheme_key]
            dev = row["dev"]
            act_col = DEV_FG if dev else ACT_FG
            inp_col = DEV_FG if dev else INPUT_FG
            canvas.blit(F_ROW.render(trunc(F_ROW, inp, C_ACT_X - C_INPUT_X - 8),
                                     True, inp_col), (x + C_INPUT_X, ry))
            canvas.blit(F_ROW.render(trunc(F_ROW, row["act"], ROW_W - C_ACT_X),
                                     True, act_col), (x + C_ACT_X, ry))
            # second line: shown marker + hint
            hint = row["hint"]
            dot = DOT_ON if hint else DOT_OFF
            pygame.draw.circle(canvas, dot, (x + C_INPUT_X + 5, ry + 24), 4)
            if hint:
                txt = hint.replace("{fire}", btn_letters["fire"]) \
                          .replace("{bomb}", btn_letters["bomb"]) \
                          .replace("{ability}", btn_letters["ability"]) \
                          .replace("{cancel}", btn_letters["cancel"])
                canvas.blit(F_HINT.render(trunc(F_HINT, txt, ROW_W - 26), True, HINT_FG),
                            (x + C_INPUT_X + 16, ry + 18))
            else:
                canvas.blit(F_HINT.render("not shown on-screen", True, NOHINT_FG),
                            (x + C_INPUT_X + 16, ry + 18))
            ry += ROW_H

    canvas.blit(F_FOOT.render(footer, True, DIM), (MARGIN, canvas_h - 34))
    out = os.path.join(SHOT_DIR, out_name)
    pygame.image.save(canvas, out)
    print(f"-> {out}  ({canvas_w}x{canvas_h})")


def main():
    # Controller variant: show silk letters. Use the PC/Xbox scheme as the
    # canonical labels (footer notes the RG silk differences).
    pc = pewpew._PC_BUTTON_SCHEME
    pc_letters = {k: pc[k][1] for k in ("fire", "bomb", "ability", "cancel")}
    render("ctrl", "CONTROLLER",
           pc_letters,
           "Face letters shown Xbox-style: South=A(fire) East=B(rewind/exit) West=X(ability) "
           "North=Y(cancel).  RG silk swaps to B/A/Y/X (same physical positions).  "
           "L2/R2 = analog or digital triggers; SEL=SELECT.",
           "controls_map_controller.png")

    # Keyboard+mouse variant: spell the action words so {fire} etc. read as
    # the action, not a letter.
    kb_letters = {"fire": "fire", "bomb": "east", "ability": "west", "cancel": "north"}
    render("kb", "KEYBOARD + MOUSE",
           kb_letters,
           "Hints on-screen show the gamepad glyph, not the key — the same hint serves "
           "both schemes.  Num# = numpad (Num Lock on).  LMB/RMB = mouse buttons.  "
           "north has only one key: Tab.",
           "controls_map_keyboard.png")


if __name__ == "__main__":
    main()
