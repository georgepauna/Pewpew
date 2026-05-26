"""Bot session driver.

A session = one full bot playthrough. The driver:
  - constructs a pewpew.App headlessly,
  - replaces save state with a fresh in-memory SaveData (no disk writes),
  - chains levels via PlayState (bypassing title/map/shop screens),
  - applies upgrades between levels according to the upgrade profile,
  - drives Controls each frame via PlayBot,
  - records telemetry + a replay file.

Skipping the menu states is intentional: the bot's job is to exercise the
combat + economy levers, not navigate UI. Visual replay (Phase 2) will be
able to reconstruct each PlayState scene from the recorded inputs.
"""

import json
import random
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .profiles import PROFILES, ALL_PROFILE_NAMES
from .brain import PlayBot
from .replay import ReplayWriter


def run_bot_from_cli(cli):
    """Entry point called from pewpew.main() when --bot=<profile> is set."""
    import pewpew

    profile_arg = cli["bot"]
    if profile_arg == "all":
        profiles = list(ALL_PROFILE_NAMES)
    elif profile_arg in PROFILES:
        profiles = [profile_arg]
    else:
        print(f"[bot] unknown profile '{profile_arg}'. "
              f"Choose from: {', '.join(ALL_PROFILE_NAMES)}, or 'all'.")
        sys.exit(2)

    snapshot_id = _detect_snapshot_id()
    out_dir = cli["out_dir"] or _default_out_dir(snapshot_id)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    print(f"[bot] snapshot={snapshot_id}  out={out_path}")

    meta = {
        "snapshot_id": snapshot_id,
        "seed_base": cli["seed"],
        "max_steps": cli["max_steps"],
        "profiles": profiles,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_path / "meta.json").write_text(json.dumps(meta, indent=2))

    app = pewpew.App(windowed=True)
    # Silence any disk writes via SaveData.save (we keep our own state).
    app.save.save = lambda *a, **kw: None

    telemetries = {}
    for i, name in enumerate(profiles):
        seed = cli["seed"] + i * 100003
        print(f"[bot] profile={name} seed={seed}")
        random.seed(seed)
        prof = PROFILES[name]
        sess = BotSession(app, prof, seed, max_steps=cli["max_steps"])
        t0 = time.perf_counter()
        sess.run()
        elapsed = time.perf_counter() - t0
        sess.telemetry["wall_clock_elapsed_real_sec"] = round(elapsed, 2)
        (out_path / f"telemetry-{name}.json").write_text(
            json.dumps(sess.telemetry, indent=2))
        sess.replay.save(out_path / f"replay-{name}.bin")
        telemetries[name] = sess.telemetry
        summ = sess.telemetry["summary"]
        print(f"[bot]   -> {summ['levels_completed']} levels, "
              f"{summ['deaths']} deaths, "
              f"{summ['wall_time_sec']:.1f}s sim, "
              f"{elapsed:.1f}s real")

    import pygame
    pygame.quit()

    from .plot import plot_telemetries
    plot_telemetries(telemetries, out_path / "progression.png", snapshot_id)
    print(f"[bot] plot: {out_path / 'progression.png'}")

    # Mirror the latest run's replays to a fixed `replays/` folder next to
    # pewpew.py so the title-screen / map-screen replay shortcuts have a
    # stable place to find them. Also try to deploy to device.
    mirrored = _mirror_replays_to_fixed_path(out_path, profiles, telemetries)
    if mirrored:
        from . import deploy as _deploy
        _deploy.deploy_replays(mirrored)


def _mirror_replays_to_fixed_path(out_path, profiles, telemetries):
    """Copy each profile's replay-<name>.bin into project_root/replays/.

    Returns the list of destination Paths actually written. We also drop a
    `latest.json` next to them so the in-game replay shortcuts can show
    which profiles are available + how far each one got.
    """
    import shutil
    project_root = Path(__file__).resolve().parents[2]
    dest_dir = project_root / "replays"
    dest_dir.mkdir(parents=True, exist_ok=True)
    written = []
    summary = {}
    for name in profiles:
        src = out_path / f"replay-{name}.bin"
        if not src.is_file():
            continue
        dst = dest_dir / f"replay-{name}.bin"
        shutil.copy2(src, dst)
        written.append(dst)
        s = telemetries.get(name, {}).get("summary", {})
        summary[name] = {
            "levels_completed": s.get("levels_completed", 0),
            "max_level": s.get("max_level", 0),
            "deaths": s.get("deaths", 0),
            "wall_time_sec": s.get("wall_time_sec", 0.0),
        }
    latest_json = {
        "source_run": out_path.name,
        "profiles": summary,
    }
    (dest_dir / "latest.json").write_text(json.dumps(latest_json, indent=2))
    return written


def _default_out_dir(snapshot_id):
    ts = time.strftime("%Y-%m-%d_%H%M%S")
    return f"tuning/runs/{snapshot_id}_{ts}"


def _detect_snapshot_id():
    snaps = sorted(Path("tuning/snapshots").glob("*.json"))
    if not snaps:
        return "no-snapshot"
    return snaps[-1].stem


class BotSession:
    """One bot playthrough, level by level, until done or limits hit."""

    def __init__(self, app, profile, seed, max_steps=200):
        import pewpew
        self.pewpew = pewpew
        self.app = app
        self.profile = profile
        self.skill = profile.skill
        self.upgrade = profile.upgrade
        self.seed = seed
        self.max_steps = max_steps

        # Fresh save in memory; never touch disk.
        self.app.save = pewpew.SaveData()
        self.app.save.save = lambda *a, **kw: None

        self.brain = PlayBot(profile.skill,
                             play_w=pewpew.PLAY_W,
                             play_h=pewpew.PLAY_H)
        self.replay = ReplayWriter(seed=seed, profile=profile.name)
        self.telemetry = {
            "profile": profile.name,
            "seed": seed,
            "events": [],
            "summary": {},
        }
        self._dt = 1.0 / pewpew.FPS
        # State for upgrade walker: a mutable list of priority items still
        # to spend on. We mutate this as we buy.
        self._priority_remaining = list(self.upgrade.priority)
        self._max_attempts_per_level = 5
        self._frame_cap_per_level = 60 * 240   # 4 minutes of sim time

    # -------- top-level loop --------

    def run(self):
        pewpew = self.pewpew
        save = self.app.save
        controls = pewpew.Controls()
        deaths = 0
        wall_t = 0.0
        attempts = {}

        for step in range(self.max_steps):
            level_key = self._next_level(save)
            if level_key is None:
                break
            spent = self._apply_upgrades(save)

            # Deterministic per-level seed so a recorded run can be replayed
            # frame-for-frame later, even when only a single level is played
            # back. Same formula in the replay player.
            attempt = attempts.get(level_key, 0) + 1
            attempts[level_key] = attempt
            per_level_seed = _per_level_seed(self.seed, level_key, attempt)
            random.seed(per_level_seed)

            level = self.app.levels[level_key]
            ps = pewpew.PlayState(self.app, level)

            self.replay.begin_level(
                level_key, attempt,
                save_snapshot=_snapshot_save(save),
                per_level_seed=per_level_seed)

            sim_t, frame_count, won, score, creds_gained = \
                self._play_level(ps, controls)
            wall_t += sim_t

            if won:
                if level_key not in save.completed:
                    save.completed.append(level_key)
                for nxt in pewpew.MAP_GRAPH[level_key].nexts:
                    if nxt not in save.unlocked:
                        save.unlocked.append(nxt)
                save.credits += creds_gained
            else:
                deaths += 1

            self.telemetry["events"].append({
                "step": step,
                "level": level_key,
                "attempt": attempt,
                "won": won,
                "time_sec": round(sim_t, 2),
                "frames": frame_count,
                "score": score,
                "credits_earned": creds_gained,
                "credits_total": save.credits,
                "shield_lvl": save.loadout.shield,
                "shield_max": pewpew.SHIELD_MAX[save.loadout.shield],
                "engine_lvl": save.loadout.engine,
                "main_type": save.loadout.main_type,
                "main_lvl": save.loadout.main_level(),
                "side_type": save.loadout.side_type,
                "side_lvl": save.loadout.side_level(),
                "bombs": save.loadout.bombs,
                "spent_this_step": spent,
                "deaths_total": deaths,
                "wall_time_total": round(wall_t, 2),
            })
            self.replay.end_level(won=won, score=score)

            # (attempt was incremented before play; don't double-count here)

            # Bail-out on persistent failure: bot is hard-stuck on this level.
            if (not won) and attempts[level_key] >= self._max_attempts_per_level:
                # Force-unlock to keep the run informative rather than
                # ending after 5 deaths on the same wall.
                if level_key not in save.completed:
                    save.completed.append(level_key)
                for nxt in pewpew.MAP_GRAPH[level_key].nexts:
                    if nxt not in save.unlocked:
                        save.unlocked.append(nxt)

        max_lvl = max([int(k[1:]) for k in save.completed], default=0)
        self.telemetry["summary"] = {
            "levels_completed": len(save.completed),
            "max_level": max_lvl,
            "deaths": deaths,
            "wall_time_sec": round(wall_t, 2),
            "final_credits": save.credits,
            "final_shield_lvl": save.loadout.shield,
            "final_engine_lvl": save.loadout.engine,
            "final_main_type": save.loadout.main_type,
            "final_main_lvl": save.loadout.main_level(),
            "final_side_type": save.loadout.side_type,
            "final_side_lvl": save.loadout.side_level(),
        }

    # -------- per-level loop --------

    def _play_level(self, ps, controls):
        pewpew = self.pewpew
        dt = self._dt
        sim_t = 0.0
        frame_count = 0
        outcome = None
        while outcome is None and frame_count < self._frame_cap_per_level:
            self.brain.step(ps, controls)
            self.replay.record_frame(controls)
            ps._update(dt, controls)
            outcome = ps.outcome
            frame_count += 1
            sim_t += dt
        won = False
        score = ps.score
        creds_gained = 0
        if outcome == "win":
            won = True
            creds_gained = ps.credits_earned
        elif outcome == "loss":
            won = False
        elif outcome is None:
            # Hit the frame cap. Treat as a loss but record what was earned.
            won = False
        return sim_t, frame_count, won, score, creds_gained

    # -------- upgrades --------

    def _next_level(self, save):
        for n in range(1, 101):
            key = f"L{n:03d}"
            if key in save.completed:
                continue
            if key not in save.unlocked:
                continue
            return key
        return None

    def _apply_upgrades(self, save):
        pewpew = self.pewpew
        spent = 0
        pri = self._priority_remaining
        while pri:
            kind, target = pri[0]
            cost = self._cost_of(save, kind, target)
            if cost is None:
                pri.pop(0)
                continue
            if save.credits >= cost:
                self._apply_purchase(save, kind, target)
                save.credits -= cost
                spent += cost
                pri.pop(0)
                continue
            if self.upgrade.impatient:
                # Try cheaper items later in the list. Don't consume the
                # head item; just pluck a later affordable one.
                bought_idx = None
                for j in range(1, len(pri)):
                    k2, t2 = pri[j]
                    c2 = self._cost_of(save, k2, t2)
                    if c2 is not None and save.credits >= c2:
                        self._apply_purchase(save, k2, t2)
                        save.credits -= c2
                        spent += c2
                        bought_idx = j
                        break
                if bought_idx is not None:
                    pri.pop(bought_idx)
                    continue
            break
        # Keep bombs topped up if cheap enough.
        while (save.loadout.bombs < self.upgrade.keep_bombs
               and save.credits >= pewpew.BOMB_PRICE):
            save.loadout.bombs += 1
            save.credits -= pewpew.BOMB_PRICE
            spent += pewpew.BOMB_PRICE
        return spent

    def _cost_of(self, save, kind, target):
        pewpew = self.pewpew
        lo = save.loadout
        if kind == "shield":
            cur = lo.shield
            if cur >= pewpew.MAX_LEVELS["shield"]:
                return None
            return pewpew.WEAPON_COSTS["shield"][cur]
        if kind == "engine":
            cur = lo.engine
            if cur >= pewpew.MAX_LEVELS["engine"]:
                return None
            return pewpew.WEAPON_COSTS["engine"][cur]
        if kind == "main_upgrade":
            cur = getattr(lo, f"main_{target}", 0)
            if cur == 0:
                return pewpew.MAIN_BUY_COST
            if cur >= pewpew.MAIN_WEAPON_MAX:
                return None
            return pewpew.MAIN_UPGRADE_COSTS[target][cur]
        if kind == "side_first":
            cur = getattr(lo, f"side_{target}", 0)
            if cur > 0:
                return None
            return pewpew.SIDE_BUY_COST
        if kind == "side_upgrade":
            cur = getattr(lo, f"side_{target}", 0)
            if cur == 0:
                return pewpew.SIDE_BUY_COST
            if cur >= pewpew.SIDE_WEAPON_MAX:
                return None
            return pewpew.SIDE_UPGRADE_COSTS[target][cur]
        return None

    def _apply_purchase(self, save, kind, target):
        pewpew = self.pewpew
        lo = save.loadout
        if kind == "shield":
            lo.shield = min(pewpew.MAX_LEVELS["shield"], lo.shield + 1)
        elif kind == "engine":
            lo.engine = min(pewpew.MAX_LEVELS["engine"], lo.engine + 1)
        elif kind == "main_upgrade":
            cur = getattr(lo, f"main_{target}", 0)
            new = min(pewpew.MAIN_WEAPON_MAX, cur + 1)
            setattr(lo, f"main_{target}", new)
            lo.main_type = target
        elif kind == "side_first":
            cur = getattr(lo, f"side_{target}", 0)
            if cur == 0:
                setattr(lo, f"side_{target}", 1)
                lo.side_type = target
        elif kind == "side_upgrade":
            cur = getattr(lo, f"side_{target}", 0)
            new = min(pewpew.SIDE_WEAPON_MAX, cur + 1)
            if cur == 0:
                lo.side_type = target
            setattr(lo, f"side_{target}", new)


def _per_level_seed(base_seed, level_key, attempt):
    """Deterministic seed for one level attempt. Same formula used by the
    in-game replay player, so RNG state aligns frame-for-frame."""
    try:
        n = int(level_key[1:])
    except (ValueError, IndexError):
        n = 0
    s = (int(base_seed) * 2654435761) & 0xFFFFFFFF
    s ^= (n * 7919) & 0xFFFFFFFF
    s ^= (int(attempt) * 1597) & 0xFFFFFFFF
    return s & 0xFFFFFFFF


def _snapshot_save(save):
    """Best-effort dict of the SaveData (used in replay metadata)."""
    try:
        return asdict(save)
    except Exception:
        return {
            "credits": save.credits,
            "completed": list(save.completed),
            "unlocked": list(save.unlocked),
            "loadout": save.loadout.__dict__,
        }
