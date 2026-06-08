"""Pure-GPU render PoC (branch gpu-mt-spike). Renders a representative game
frame through pygame._sdl2 Renderer using REAL game assets, reads it back to a
PNG for visual verification, and times the frame. Proves the GPU backend
foundation end-to-end before converting pewpew's render layer.

Constraint discovered on-device: pygame 2.6.1 Renderer has NO triangle/geometry
API, so filled procedural shapes (cooldown arcs, gradients) become BAKED
textures (rendered once in software, uploaded). This PoC demonstrates that
pattern alongside textured-quad sprites + fill_rect HUD bars + bitmap text.
"""
import os, sys, time, math
sys.path.insert(0, "/storage/roms/ports/Pewpew/pylibs/cp313")
sys.path.insert(0, "/storage/roms/ports/Pewpew")
os.environ.setdefault("XDG_RUNTIME_DIR", "/var/run/0-runtime-dir")
os.environ.setdefault("WAYLAND_DISPLAY", "wayland-1")
os.environ["PEWPEW_ON_DEVICE"] = "1"
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
import pygame
from pygame._sdl2 import video as v
pygame.display.init()
try:
    pygame.mixer.init()
except Exception:
    pass
import pewpew
W, H = pewpew.PLAY_W, pewpew.PLAY_H   # gameplay area (640x480 on device)

# --- GPU context: hidden window, accelerated renderer, 640x480 render target ---
win = v.Window("gpu_poc", size=(W, H), hidden=True)
ren = v.Renderer(win, accelerated=True, vsync=False, target_texture=True)
frame_tex = v.Texture(ren, (W, H), target=True)

# --- assets -> textures (uploaded once) ---
assets = pewpew.make_assets()
fonts = {s: pewpew.BitmapFont(scale=s) for s in range(1, 5)}

def tex(surf):
    return v.Texture.from_surface(ren, surf)

sprite_tex = {}
for k, val in assets.items():
    if isinstance(val, pygame.Surface):
        sprite_tex[k] = tex(val)

# Background: first backdrop if present, else a dark fill.
bg_tex = None
bd = assets.get("_backdrops") or {}
for name, surf in bd.items():
    if isinstance(surf, pygame.Surface):
        bg_tex = tex(surf); break

# Bullet glyph from _projectiles.
bullet_tex = None
proj = assets.get("_projectiles") or {}
for name, surf in proj.items():
    if isinstance(surf, pygame.Surface):
        bullet_tex = tex(surf); break

# BAKED procedural shape: a cooldown-arc-style gradient ring, rendered ONCE in
# software (the only way without RenderGeometry), uploaded as a texture. In the
# real port the animated fill is a bottom-aligned sub-rect crop of this (the
# same crop trick already used on RG).
def bake_arc(r=40, col=(80, 200, 255)):
    s = pygame.Surface((r*2+4, r*2+4), pygame.SRCALPHA)
    cx = cy = r+2
    for j in range(8):
        rr = r - j*2
        a = int(60 + 195 * (1 - abs(j-4)/4))
        pygame.draw.circle(s, (col[0], col[1], col[2], a), (cx, cy), rr, 2)
    return s
arc_tex = tex(bake_arc())

# Pre-render a HUD text line to a texture (bitmap font -> surface -> texture).
def text_tex(font, msg, color):
    surf = font.render(msg, False, color)
    return tex(surf), surf.get_width(), surf.get_height()
hud_tex, hud_w, hud_h = text_tex(fonts[2], "SCORE 145600", (220, 230, 240))

# Choose a handful of enemy/player sprites that actually loaded.
def pick(*names):
    for n in names:
        if n in sprite_tex:
            return sprite_tex[n]
    return None
player_t = pick("player_left", "player_right")
enemy_ts = [t for t in (pick("mine"), pick("pylon"), pick("crystal"),
                        pick("scout"), pick("gunner")) if t]

def draw_frame():
    ren.target = frame_tex
    ren.draw_color = (8, 10, 24, 255)
    ren.clear()
    # background (stretched to playfield)
    if bg_tex is not None:
        bg_tex.draw(dstrect=pygame.Rect(0, 0, W, H))
    # scatter ~80 enemy sprites with rotation + alpha (GPU's strength)
    if enemy_ts:
        for i in range(80):
            t = enemy_ts[i % len(enemy_ts)]
            x = (i * 71) % (W - 32); y = (i * 47) % (H - 32)
            t.alpha = 255
            t.draw(dstrect=pygame.Rect(x, y, t.width, t.height),
                   angle=(i * 13) % 360)
    # ~120 bullets
    if bullet_tex is not None:
        for i in range(120):
            x = (i * 53) % (W - 8); y = (i * 29) % (H - 8)
            bullet_tex.draw(dstrect=pygame.Rect(x, y, bullet_tex.width, bullet_tex.height))
    # player + baked cooldown arc around it
    if player_t is not None:
        px, py = W//2 - player_t.width//2, H - 90
        player_t.draw(dstrect=pygame.Rect(px, py, player_t.width, player_t.height))
        aw = arc_tex.width
        arc_tex.draw(dstrect=pygame.Rect(px + player_t.width//2 - aw//2,
                                         py + player_t.height//2 - aw//2, aw, aw))
    # HUD: a fill_rect health bar (native) + text line
    ren.draw_color = (40, 60, 90, 255); ren.fill_rect(pygame.Rect(12, 12, 200, 10))
    ren.draw_color = (90, 220, 140, 255); ren.fill_rect(pygame.Rect(13, 13, 150, 8))
    hud_tex.draw(dstrect=pygame.Rect(12, 28, hud_w, hud_h))
    ren.target = None

# --- timing ---
for _ in range(10):
    draw_frame()
ts = []
for _ in range(120):
    t0 = time.perf_counter(); draw_frame(); ts.append((time.perf_counter()-t0)*1000)
ts = sorted(ts[10:])
print("GPU full-frame (bg + 80 rotated sprites + 120 bullets + player + arc + HUD):")
print("  mean=%.2fms p50=%.2fms p95=%.2fms" % (
    sum(ts)/len(ts), ts[len(ts)//2], ts[int(len(ts)*0.95)]))
print("  sprite textures uploaded:", len(sprite_tex))

# --- read back to PNG for visual verification ---
ren.target = frame_tex
out = ren.to_surface()
ren.target = None
png = "/storage/roms/ports/Pewpew/_gpu_poc.png"
pygame.image.save(out, png)
print("saved", png, out.get_size())
print("DONE")
