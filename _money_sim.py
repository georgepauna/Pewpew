"""Simulate a perfect-clear playthrough and price-out full upgrades.

For each L001..L100 the script spins up a headless PlayState with an
immortal max-loadout player and force-kills every enemy as it spawns
(simulating the 100% clear that the universal-mechanics ruleset now
requires for a win). It records the credits earned per level — kill
bounties (Enemy.CREDITS) plus money-pickup payouts (PICKUP_VALUES
-> Player.collect -> $25 each) — and tallies cumulative against the
total shop cost to fully upgrade Rail + Ball + Vulcan + Engine.

Output: level-by-level breakdown table, then a final summary.

Note: bullet/drop RNG is per-wave seeded so the same level yields
the same money every run; no need to average across seeds.
"""
import os, sys
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
import pygame
pygame.mixer.pre_init(22050, -16, 1, 256)
pygame.init()
try:
    pygame.mixer.init()
except Exception:
    pass
pygame.display.set_mode((640, 480), pygame.HIDDEN)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pewpew


class FakeApp:
    pass


def build_app():
    app = FakeApp()
    app.assets = pewpew.make_assets()
    pewpew.BackgroundRibbon.set_backdrops(app.assets.get("_backdrops", {}))
    pewpew.Bullet.set_glyphs(app.assets.get("_projectiles", {}))
    pewpew.ExplosionRing.set_fx(app.assets.get("_fx", {}))
    app.sounds = pewpew.make_sounds()
    app.save = pewpew.SaveData()
    app.save.credits = 0
    # Tiers fully unlocked (otherwise upgrade-purchase ceilings would
    # gate the analytical "what's the bill" answer).
    app.save.unlocked_tier_rail = 5
    app.save.unlocked_tier_ball = 5
    app.save.unlocked_tier_vulcan = 5
    app.save.unlocked_tier_engine = 5
    # Max loadout so the auto-kill loop blows through fast.
    app.save.loadout.main_type = "rail"
    app.save.loadout.main_rail = 20
    app.save.loadout.main_ball = 20
    app.save.loadout.main_vulcan = 20
    app.save.loadout.engine = 5
    app.fonts = {}
    for s in range(1, 8):
        app.fonts[s] = pewpew.BitmapFont(scale=s)
    app.fonts["tiny"] = app.fonts[1]
    app.fonts["small"] = app.fonts[2]
    app.fonts["big"] = app.fonts[3]
    app.fonts["large"] = app.fonts[4]
    app.fonts["huge"] = app.fonts[5]
    app.fonts["mega"] = app.fonts[6]
    app.fonts["giant"] = app.fonts[7]
    app.screen = pygame.display.get_surface()
    try:
        app.vignette = pewpew.make_vignette()
    except AttributeError:
        app.vignette = pygame.Surface((640, 480), pygame.SRCALPHA)
    app.levels = pewpew.make_levels()
    app.music_channel = None
    app.music_tracks = {}
    app.current_music = None
    def noop(*a, **kw): pass
    app.set_music = noop
    app.sfx_bus = pewpew.AudioBus(0.0, label="VOL")
    app.music_bus = pewpew.AudioBus(0.0, label="MUSIC")
    app.master_bus = pewpew.AudioBus(0.0, label="MASTER")
    app.perf = pewpew.PerfMonitor()
    app.clock = pygame.time.Clock()
    return app


def sim_level(app, level_key, time_budget=180.0):
    """Run one level with an immortal player that force-kills every
    spawned enemy and auto-pockets every money drop. Returns the
    credits added to the save during this level."""
    level = app.levels[level_key]
    play = pewpew.PlayState(app, level)
    # Skip the intro cinematic — drop the ship at the bottom centre
    # and disable cinematic flags so _update runs the normal sim.
    play.intro_t = 0
    play.player.cinematic = False
    play.player.cinematic_scale = 1.0
    play.player.x = pewpew.PLAY_W // 2
    play.player.y = pewpew.PLAY_H - 60
    play.player.rect.center = (int(play.player.x), int(play.player.y))
    play.player.invuln = 1e9

    credits_before = app.save.credits
    ctrl = pewpew.Controls()
    MAX_FRAMES = int(time_budget * 60)
    for _ in range(MAX_FRAMES):
        play.run([], ctrl)
        # Keep player immortal — invuln decays each frame.
        play.player.invuln = 1e9
        # Force-kill every alive non-wall enemy. _on_kill awards
        # CREDITS, rolls drops, and updates enemies_killed for the
        # clear-percentage check.
        for e in list(play.enemies):
            if not e.alive:
                continue
            if isinstance(e, pewpew.Wall):
                continue
            if e.hp > 0:
                e.hp = 0
                e.alive = False
                play._on_kill(e, drop=True, show_text=False)
        # Auto-collect money pickups.
        for p in list(play.pickups):
            if not p.alive:
                continue
            result = play.player.collect(p, app.save)
            if result and result[0] == "credits":
                play._earn(result[1])
            p.alive = False
        if play._win_held or play.outcome is not None:
            break

    return app.save.credits - credits_before


def total_main_upgrade_cost():
    """Cost to take ONE main from L1 to L20."""
    return sum(pewpew.MAIN_UPGRADE_COSTS["rail"][1:20])


def total_engine_upgrade_cost():
    return sum(pewpew.WEAPON_COSTS["engine"][1:5])


def upgrade_breakdown():
    """List the per-item upgrade costs (full L1->max)."""
    main = total_main_upgrade_cost()
    engine = total_engine_upgrade_cost()
    return [
        ("Rail Gun (L1 -> L20, all 5 tiers)", main),
        ("Ball (L1 -> L20, all 5 tiers)",     main),
        ("Vulcan Gun (L1 -> L20, all 5 tiers)", main),
        ("Engine (L1 -> L5)",                 engine),
    ]


def main():
    app = build_app()
    keys = [f"L{n:03d}" for n in range(1, 101)]

    print("=" * 78)
    print("PEWPEW money simulation — 100%-clear run, every level")
    print("=" * 78)

    upgrades = upgrade_breakdown()
    total_upgrade = sum(c for _, c in upgrades)
    print(f"\nUpgrade bill (full L1 -> max on every upgrade):")
    for name, cost in upgrades:
        print(f"  {name:<40}  ${cost:>7,}")
    print(f"  {'TOTAL':<40}  ${total_upgrade:>7,}")

    print(f"\nPer-level credits (kill bounties + money drops auto-collected):")
    print(f"  {'level':<6} {'earned':>10} {'cumulative':>12} {'vs upgrade':>14}")
    print("  " + "-" * 46)

    cumulative = 0
    rows = []
    for k in keys:
        # Fresh app per level so cross-level state doesn't pollute.
        app.save.credits = 0
        earned = sim_level(app, k)
        cumulative += earned
        rows.append((k, earned, cumulative))
        gap = cumulative - total_upgrade
        gap_str = (f"+${gap:,}" if gap >= 0
                   else f"-${-gap:,}")
        print(f"  {k:<6} ${earned:>9,} ${cumulative:>11,} {gap_str:>14}")

    print("  " + "-" * 46)
    final = cumulative
    print(f"\nFinal cumulative after L100: ${final:,}")
    print(f"Total to fully upgrade:      ${total_upgrade:,}")
    if final >= total_upgrade:
        print(f"SURPLUS: ${final - total_upgrade:,}")
    else:
        print(f"SHORT: ${total_upgrade - final:,}")

    # Before the boss-100 fight specifically. L100 might be a boss
    # itself; the "before final boss" snapshot is the cumulative
    # entering L100.
    if len(rows) >= 100:
        cum_before_l100 = rows[-2][2]
        print(f"\nCumulative entering L100 (the final fight): "
              f"${cum_before_l100:,}")
        gap = cum_before_l100 - total_upgrade
        if gap >= 0:
            print(f"  -> already enough for full upgrades by L099, surplus ${gap:,}")
        else:
            print(f"  -> SHORT ${-gap:,} for full upgrades")


if __name__ == "__main__":
    main()
