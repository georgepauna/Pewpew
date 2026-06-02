"""Render each major UI state and save a PNG into screenshots/.
Kept around between runs so the user has a current visual snapshot."""
import sys, os
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"
sys.path.insert(0, r"d:\Github\Pewpew")
import pygame
pygame.mixer.pre_init(22050, -16, 1, 256)
pygame.init()
try: pygame.mixer.init()
except Exception: pass
pygame.display.set_mode((640, 480), pygame.HIDDEN)

import pewpew

SHOT_DIR = r"d:\Github\Pewpew\screenshots"
os.makedirs(SHOT_DIR, exist_ok=True)

class FakeApp: pass
app = FakeApp()
app.profile_name = pewpew.PROFILE_NAMES[0] if hasattr(pewpew, "PROFILE_NAMES") else "ANDROMEDA"
app.assets = pewpew.make_assets()
pewpew.BackgroundRibbon.set_backdrops(app.assets.get("_backdrops", {}))
pewpew.Bullet.set_glyphs(app.assets.get("_projectiles", {}))
pewpew.ExplosionRing.set_fx(app.assets.get("_fx", {}))
app.sounds = pewpew.make_sounds()
app.save = pewpew.SaveData()
app.save.unlocked = [f"L{n:03d}" for n in range(1, 41)]
app.save.completed = [f"L{n:03d}" for n in range(1, 13)]
app.save.credits = 8240
app.save.high_score = 145600
app.save.loadout.main_type = "rail"
app.save.loadout.main_rail = 3
app.save.loadout.main_spread = 2  # owned but not equipped, shows the "equip" path
app.save.loadout.side_type = "missile"
app.save.loadout.side_missile = 1
app.save.loadout.shield = 2
app.fonts = {}
for _scale in range(1, 8):
    app.fonts[_scale] = pewpew.BitmapFont(scale=_scale)
app.fonts["tiny"]  = app.fonts[1]
app.fonts["small"] = app.fonts[2]
app.fonts["big"]   = app.fonts[3]
app.fonts["large"] = app.fonts[4]
app.fonts["huge"]  = app.fonts[5]
app.fonts["mega"]  = app.fonts[6]
app.fonts["giant"] = app.fonts[7]
app.screen = pygame.display.get_surface()
app.display = app.screen
app.integer_scale = True
try:
    app.vignette = pewpew.make_vignette()
except AttributeError:
    app.vignette = pygame.Surface((640, 480), pygame.SRCALPHA)
app.logo = pewpew.make_logo("PEWPEW", scale=7, color=(120, 220, 255))
app.title_gloss_stripe = pewpew._make_gloss_stripe(
    height=app.logo.get_height(), stripe_w=70, peak=140)
app.title_yellow_mask = pewpew._make_yellow_mask(app.logo)
app.title_yellow_dim = pewpew._make_yellow_dim_layer(app.title_yellow_mask)
app.levels = pewpew.make_levels()
app.music_channel = None
app.music_tracks = {}
app.current_music = None
def noop(*a, **kw): pass
app.set_music = noop
app.sfx_bus = pewpew.AudioBus(1.0, label="VOL")
app.music_bus = pewpew.AudioBus(0.95, label="MUSIC")
app._apply_sfx_volume = lambda *a, **kw: None
app._apply_music_volume = lambda *a, **kw: None
app.perf = pewpew.PerfMonitor()
app.clock = pygame.time.Clock()

def shot(name):
    pygame.image.save(app.screen, os.path.join(SHOT_DIR, name + ".png"))

# Title (with Continue available)
ts = pewpew.TitleScreen(app)
ts.has_save = True
ts.options = ["Continue", "New Game", "SOUND", "MUSIC", "Quit"]
ts.run([], pewpew.Controls())
shot("title")

# Mission map
ms = pewpew.MapScreen(app)
ms.run([], pewpew.Controls())
shot("map")

# Shop / hangar
ss = pewpew.ShopScreen(app)
ss.run([], pewpew.Controls())
shot("shop")

# Gameplay (in an asteroid sector)
play = pewpew.PlayState(app, app.levels["L013"])
ctrl = pewpew.Controls()
ctrl.fire = True
for _ in range(360):
    play.run([], ctrl)
shot("play")

# Centre-screen banners (pause / win / loss) so the layout editor and
# any reviewer can see all three states. Each is a fresh PlayState so
# nothing leaks between captures.
def _banner_capture(name, mutate):
    p = pewpew.PlayState(app, app.levels["L001"])
    p.intro_t = 0
    p.player.cinematic = False
    p.player.cinematic_scale = 1.0
    for _ in range(30):
        p.run([], pewpew.Controls())
    mutate(p)
    p.run([], pewpew.Controls())
    shot(name)

def _set_pause(p):  p.pause = True
def _set_win(p):    p.outcome = "win";  p.credits_earned = 250
_banner_capture("play_paused", _set_pause)
_banner_capture("play_win",    _set_win)

# Boss in progress (drop a boss in directly and burn frames)
play_b = pewpew.PlayState(app, app.levels["L010"])
play_b.intro_t = 0
play_b.player.cinematic = False
play_b.player.cinematic_scale = 1.0
play_b.player.y = pewpew.PLAY_H - 80
play_b.player.rect.center = (int(play_b.player.x), int(play_b.player.y))
play_b.enemies.append(pewpew.Boss(app.assets["boss"], app.assets.get("boss_flash")))
play_b.enemies[-1].y = 100
play_b.enemies[-1].rect.centery = 100
play_b.boss_spawned = True
for _ in range(120):
    play_b.run([], pewpew.Controls(__init__=None) if False else pewpew.Controls())
shot("boss")

# Outro mid (docking)
play_o = pewpew.PlayState(app, app.levels["L001"])
play_o.intro_t = 0
play_o.player.cinematic = False
play_o.player.cinematic_scale = 1.0
play_o.player.y = pewpew.PLAY_H - 60
play_o.player.rect.center = (int(play_o.player.x), int(play_o.player.y))
play_o.elapsed = play_o.level.duration + 1
play_o.enemies = []
play_o._begin_outro()
for _ in range(80):
    play_o.run([], pewpew.Controls())
shot("outro_dock")

# Game-over screen so the layout editor has a real backdrop reference.
go = pewpew.GameOverScreen(app, score=145600)
go.run([], pewpew.Controls())
shot("gameover")

# ──────────────────────────────────────────────────────────────────────────
# Ghost Mode variants — the screens look meaningfully different in
# Ghost (GHOST MODE stamp, shield/side info hidden, dead-pause prompt,
# MISSION FAILED partial-clear banner, rewind-unlocked HUD label).
# Generated last so the module-level _GHOST_ACTIVE flip doesn't leak
# into earlier captures. The flow diagram (_flow_diagram.py) consumes
# both sets to render a two-track navigation map.
# ──────────────────────────────────────────────────────────────────────────
pewpew._GHOST_ACTIVE = True
pewpew._GHOST_REWIND_UNLOCKED = False
app.save.ghost_mode = True
app.save.rewind_unlocked = False

ts_g = pewpew.TitleScreen(app)
ts_g.has_save = True
ts_g.options = ["Continue", "New Game", "SOUND", "MUSIC", "Quit"]
ts_g.run([], pewpew.Controls())
shot("title_ghost")

ms_g = pewpew.MapScreen(app)
ms_g.run([], pewpew.Controls())
shot("map_ghost")

ss_g = pewpew.ShopScreen(app)
ss_g.run([], pewpew.Controls())
shot("shop_ghost")

play_g = pewpew.PlayState(app, app.levels["L013"])
ctrl = pewpew.Controls()
ctrl.fire = True
for _ in range(360):
    play_g.run([], ctrl)
shot("play_ghost")

# Dead-pause prompt: kill player + flip _dead_paused so the CRT glitch
# overlay + "HOLD X TO REWIND" pulse render this frame.
play_dp = pewpew.PlayState(app, app.levels["L001"])
play_dp.intro_t = 0
play_dp.player.cinematic = False
play_dp.player.cinematic_scale = 1.0
play_dp.player.y = pewpew.PLAY_H - 60
play_dp.player.rect.center = (int(play_dp.player.x), int(play_dp.player.y))
for _ in range(60):
    play_dp.run([], pewpew.Controls())
play_dp.player.alive = False
play_dp._dead_paused = True
play_dp.run([], pewpew.Controls())
shot("play_ghost_dead_pause")

# Partial-clear MISSION FAILED banner (held_progress < 1.0 in Ghost).
play_pf = pewpew.PlayState(app, app.levels["L001"])
play_pf.intro_t = 0
play_pf.player.cinematic = False
play_pf.player.cinematic_scale = 1.0
for _ in range(30):
    play_pf.run([], pewpew.Controls())
play_pf._win_held = True
play_pf._held_progress = 0.42
play_pf.credits_earned = 120
play_pf.run([], pewpew.Controls())
shot("play_ghost_fail")

# HUD with rewind unlocked — bomb-row label flips from "bomb" to
# "rewind". Toggle the flag, force-rebake the HUD chrome by invalidating
# the cache key, then render a frame.
pewpew._GHOST_REWIND_UNLOCKED = True
app.save.rewind_unlocked = True
play_ru = pewpew.PlayState(app, app.levels["L013"])
ctrl = pewpew.Controls()
ctrl.fire = True
for _ in range(360):
    play_ru.run([], ctrl)
shot("play_ghost_rewind_unlocked")

# Restore module flags so any later test scaffolding sees a clean Normal
# starting state (defensive — _smoke.py currently ends here, but a
# future addition shouldn't accidentally inherit Ghost).
pewpew._GHOST_ACTIVE = False
pewpew._GHOST_REWIND_UNLOCKED = False
app.save.ghost_mode = False
app.save.rewind_unlocked = False

# Naked variants for the layout editor preview: render with all built-in
# chrome stripped (so the editor can overlay live element positions
# without ghosting from the baked-in originals).
pewpew._RENDER_NAKED = True
try:
    ts2 = pewpew.TitleScreen(app)
    ts2.has_save = True
    ts2.options = ["Continue", "New Game", "Quit"]
    ts2.run([], pewpew.Controls())
    shot("title_naked")

    go2 = pewpew.GameOverScreen(app, score=145600)
    go2.run([], pewpew.Controls())
    shot("gameover_naked")
finally:
    pewpew._RENDER_NAKED = False

print("screenshots saved to", SHOT_DIR)
