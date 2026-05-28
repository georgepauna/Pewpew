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

Snapshot 06+: the brain reads enemy shield colours and drives
l1_held/r1_held to swap mains in flight; profiles span scrub..expert so
the same balance pass tells us both whether new players can survive and
whether expert play breaks the pacing.
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
from . import levers as _levers


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
    lever_values = _levers.parse_levers_arg(cli.get("levers", ""))
    out_dir = cli["out_dir"] or _default_out_dir(snapshot_id, lever_values)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    print(f"[bot] snapshot={snapshot_id}  out={out_path}")
    if lever_values:
        print(f"[bot] levers: {_levers.describe(lever_values)}")

    meta = {
        "snapshot_id": snapshot_id,
        "seed_base": cli["seed"],
        "max_steps": cli["max_steps"],
        "profiles": profiles,
        "levers": lever_values,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_path / "meta.json").write_text(json.dumps(meta, indent=2))

    app = pewpew.App(windowed=True)
    # Silence any disk writes via SaveData.save (we keep our own state).
    app.save.save = lambda *a, **kw: None

    # Apply sim-only lever overrides before any session runs; revert at the
    # end so the live game (and subsequent imports of pewpew in this
    # process) see default values. App is passed so levers that operate on
    # the per-run state (e.g. lmul mutates app.levels[].difficulty) work.
    revert_levers = _levers.apply_levers(pewpew, app, lever_values)

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
              f"recorded {summ.get('levels_recorded', 0)}/100, "
              f"{summ['wall_time_sec']:.1f}s sim, "
              f"{elapsed:.1f}s real")

    revert_levers()

    import pygame
    pygame.quit()

    from .plot import plot_telemetries
    plot_telemetries(telemetries, out_path / "progression.png",
                     snapshot_id, lever_values)
    print(f"[bot] plot: {out_path / 'progression.png'}")

    # Auto-commit + push the plot + meta for this run so every sim is
    # reviewable in git history. Replays + telemetry JSONs stay local
    # (gitignored). If git push fails (network / hooks) we keep going.
    _push_run_artifacts(out_path, snapshot_id, lever_values)

    # Mirror + deploy only when running without sim-only lever overrides.
    # Lever runs are analytical — replaying them on device would imply
    # those values are also in the live game, which by user request they
    # are not until explicitly confirmed.
    if lever_values:
        print("[bot] levers active — skipping mirror + deploy "
              "(simulation-only run)")
    else:
        mirrored = _mirror_replays_to_fixed_path(
            out_path, profiles, telemetries)
        if mirrored:
            from . import deploy as _deploy
            _deploy.deploy_replays(mirrored)


def _pick_better_run(prev_summary, new_summary):
    """For when we rerun a profile and need to choose which session to keep.
    Returns True if the new run reached further than the previous.

    Tiebreakers, in order:
      1. higher max_level (furthest level reached)
      2. more levels_completed (broader coverage)
      3. fewer deaths (cleaner run)
    """
    if prev_summary is None:
        return True
    p_max = int(prev_summary.get("max_level") or 0)
    n_max = int(new_summary.get("max_level") or 0)
    if n_max != p_max:
        return n_max > p_max
    p_done = int(prev_summary.get("levels_completed") or 0)
    n_done = int(new_summary.get("levels_completed") or 0)
    if n_done != p_done:
        return n_done > p_done
    p_d = int(prev_summary.get("deaths") or 0)
    n_d = int(new_summary.get("deaths") or 0)
    return n_d < p_d


def _mirror_replays_to_fixed_path(out_path, profiles, telemetries):
    """Copy each profile's replay-<name>.bin into project_root/replays/.

    Per profile we keep the **longest-path run** across sessions: if there's
    already a replay-<name>.bin from a previous run that reached further
    than this one, we leave it alone. That way reruns only ever improve
    what the device sees.

    Returns the list of destination Paths to deploy (the kept set).
    The `latest.json` index next to the bins records the summary of
    whichever run is currently kept per profile.
    """
    import shutil
    project_root = Path(__file__).resolve().parents[2]
    dest_dir = project_root / "replays"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Load existing index so we can compare against past runs.
    index_path = dest_dir / "latest.json"
    prev_index = {}
    if index_path.is_file():
        try:
            prev_index = json.loads(index_path.read_text()).get("profiles", {})
        except Exception:
            prev_index = {}

    written = []
    profile_summaries = {}
    for name in profiles:
        src = out_path / f"replay-{name}.bin"
        if not src.is_file():
            continue
        dst = dest_dir / f"replay-{name}.bin"
        new_summary = _summary_for_index(telemetries.get(name, {}).get("summary", {}))
        existing_bin_ok = dst.is_file()
        prev_summary = prev_index.get(name) if existing_bin_ok else None
        if _pick_better_run(prev_summary, new_summary):
            shutil.copy2(src, dst)
            profile_summaries[name] = dict(new_summary, source_run=out_path.name)
            print(f"[mirror] {name}: kept new "
                  f"(max_level={new_summary['max_level']}, "
                  f"deaths={new_summary['deaths']})")
        else:
            profile_summaries[name] = prev_summary
            print(f"[mirror] {name}: kept previous "
                  f"(max_level={prev_summary['max_level']}, "
                  f"deaths={prev_summary['deaths']})")
        written.append(dst)
    latest_json = {
        "last_run": out_path.name,
        "profiles": profile_summaries,
    }
    index_path.write_text(json.dumps(latest_json, indent=2))
    return written


def _summary_for_index(summary):
    return {
        "levels_completed": int(summary.get("levels_completed") or 0),
        "max_level":        int(summary.get("max_level") or 0),
        "deaths":           int(summary.get("deaths") or 0),
        "wall_time_sec":    float(summary.get("wall_time_sec") or 0.0),
    }


def _push_run_artifacts(out_path, snapshot_id, lever_values):
    """git add + commit + push the run's progression.png and meta.json.

    Each bot run produces a folder; we commit only the plot and metadata
    (replays + telemetry JSONs are gitignored — too bulky + regeneratable).
    Failures (no git, no network, hook rejection) are non-fatal: the run
    is still on disk, the user can push manually later.
    """
    import subprocess
    png = out_path / "progression.png"
    meta = out_path / "meta.json"
    if not png.is_file():
        print("[bot] no plot to push — skipping git step")
        return
    lever_tag = ""
    if lever_values:
        lever_tag = " (" + ", ".join(
            f"{k}={v}" for k, v in lever_values.items()) + ")"
    msg = f"Bot run: {out_path.name} vs {snapshot_id}{lever_tag}"

    def _run(args):
        return subprocess.run(args, capture_output=True, text=True)

    try:
        _run(["git", "add", str(png), str(meta)])
        # Skip if nothing staged
        check = _run(["git", "diff", "--cached", "--quiet"])
        if check.returncode == 0:
            print("[bot] no run artifacts to commit (already up to date)")
            return
        r = _run(["git", "commit", "-m", msg])
        if r.returncode != 0:
            print(f"[bot] git commit failed (rc={r.returncode}): "
                  f"{r.stderr.strip() or r.stdout.strip()}")
            return
        r = _run(["git", "push"])
        if r.returncode != 0:
            print(f"[bot] git push failed (rc={r.returncode}): "
                  f"{r.stderr.strip() or r.stdout.strip()}")
            print("[bot] commit landed locally — push manually when reachable")
            return
        print(f"[bot] pushed run artifacts: {png.name}, {meta.name}")
    except Exception as e:
        print(f"[bot] git step failed ({type(e).__name__}: {e}) — keeping local")


def _default_out_dir(snapshot_id, lever_values=None):
    """Naming convention: NN_YYMMDD-HHMMSS_<snapshot>[_lever_tags]
    NN is the snapshot's own counter — every run of snapshot 01 is
    `01_<timestamp>_…`, every run of snapshot 02 is `02_<timestamp>_…`.
    The timestamp makes folders unique within a snapshot's set.
    """
    counter_str, snap_part = _split_snapshot_id(snapshot_id)
    ts = time.strftime("%y%m%d-%H%M%S")
    lever_tag = ""
    if lever_values:
        lever_tag = "_" + "_".join(
            f"{k}{v}".replace(".", "p") for k, v in lever_values.items())
    return f"tuning/runs/{counter_str}_{ts}_{snap_part}{lever_tag}"


def _split_snapshot_id(snapshot_id):
    """Pull the leading digits off a snapshot id ("01-baseline" → ("01",
    "baseline")) so we can use the counter on the run folder. If there's
    no digit prefix, returns ("00", snapshot_id) as a defensive fallback.
    """
    i = 0
    while i < len(snapshot_id) and snapshot_id[i].isdigit():
        i += 1
    if i == 0:
        return ("00", snapshot_id)
    counter_str = snapshot_id[:i]
    rest = snapshot_id[i + 1:] if i < len(snapshot_id) else ""
    return (counter_str, rest or snapshot_id)


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
        # Apply this profile's starting loadout preferences. main_type is
        # driven by the L1/R1 hold each frame in-game, so we only need to
        # set the ability + the preferred side-weapon slot.
        self.app.save.loadout.ability = getattr(profile, "ability", "screen_clear")
        # preferred_side stays unbought until the upgrade list spends on
        # it; storing it here is informational only.
        self._preferred_side = getattr(profile, "preferred_side", "missile")

        # Bot RNG seed derived from session seed so dodge-dropout decisions
        # are deterministic but independent from the game's random stream.
        self.brain = PlayBot(profile.skill,
                             play_w=pewpew.PLAY_W,
                             play_h=pewpew.PLAY_H,
                             rng_seed=(seed * 9176001) & 0xFFFFFFFF)
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
        # Lowered from 5 to 3: with a deterministic per-attempt seed, repeated
        # attempts on the same loadout rarely diverge much. 3 retries is a
        # better tradeoff between giving the bot a real chance and not burning
        # the step budget on a wall it cannot get past.
        self._max_attempts_per_level = 3
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

            (sim_t, frame_count, won, score, creds_gained,
             n_spawned, n_killed, progress_pct,
             dead_air_pct, longest_lull_sec, mean_alive, peak_alive
             ) = self._play_level(ps, controls)
            wall_t += sim_t

            # Force-skip: when the bot fails this level for the third
            # attempt in a row, we pretend it cleared. That means SAME
            # rewards as a legit win — credit bonus + boss-tier unlocks —
            # otherwise the bot soft-locks at T2 weapons after the first
            # missed boss and cascade-fails the rest of the run. The
            # session output still records `won=False` so plots/metrics
            # can distinguish the real wins from the propped-up ones via
            # the new `force_skipped` flag.
            force_skipped = (not won) and attempts[level_key] >= self._max_attempts_per_level
            effective_win = won or force_skipped

            if effective_win:
                if level_key not in save.completed:
                    save.completed.append(level_key)
                for nxt in pewpew.MAP_GRAPH[level_key].nexts:
                    if nxt not in save.unlocked:
                        save.unlocked.append(nxt)
                save.credits += creds_gained
                # Apply boss-tier unlocks (mutates save in place). Bot
                # doesn't see the shop reveal animation, just continues.
                pewpew._apply_boss_unlocks(save, level_key)
            if not won:
                deaths += 1

            kill_pct = (100.0 * n_killed / n_spawned) if n_spawned else 0.0
            self.telemetry["events"].append({
                "step": step,
                "level": level_key,
                "attempt": attempt,
                "won": won,
                "force_skipped": force_skipped,
                "time_sec": round(sim_t, 2),
                "frames": frame_count,
                "score": score,
                "credits_earned": creds_gained,
                "credits_total": save.credits,
                "enemies_spawned": n_spawned,
                "enemies_killed": n_killed,
                "kill_pct": round(kill_pct, 1),
                "progress_pct": round(progress_pct, 1),
                "dead_air_pct": round(dead_air_pct, 1),
                "longest_lull_sec": round(longest_lull_sec, 2),
                "mean_enemies_alive": round(mean_alive, 2),
                "peak_enemies_alive": peak_alive,
                "shield_lvl": save.loadout.shield,
                "shield_max": pewpew.SHIELD_MAX[save.loadout.shield],
                "engine_lvl": save.loadout.engine,
                "main_type": save.loadout.main_type,
                # All 3 mains are always owned now — report each and use
                # the max for the legacy main_lvl plot field.
                "main_pulse_lvl":  save.loadout.main_pulse,
                "main_spread_lvl": save.loadout.main_spread,
                "main_vulcan_lvl": save.loadout.main_vulcan,
                "main_lvl": max(save.loadout.main_pulse,
                                 save.loadout.main_spread,
                                 save.loadout.main_vulcan),
                "side_type": save.loadout.side_type,
                "side_lvl": save.loadout.side_level(),
                "bombs": save.loadout.bombs,
                "spent_this_step": spent,
                "deaths_total": deaths,
                "wall_time_total": round(wall_t, 2),
            })
            self.replay.end_level(won=won, score=score)
            # Force-skip rewards are applied above (effective_win branch),
            # so the next level inherits the same save state a legit win
            # would have produced.

        max_lvl = max([int(k[1:]) for k in save.completed], default=0)
        # Compute level coverage in the replay — which levels are guaranteed
        # to play back via the map-screen shortcut.
        recorded_levels = set()
        for ev in self.telemetry["events"]:
            recorded_levels.add(ev["level"])
        missing = [f"L{n:03d}" for n in range(1, 101)
                   if f"L{n:03d}" not in recorded_levels]
        self.telemetry["summary"] = {
            "levels_completed": len(save.completed),
            "levels_recorded": len(recorded_levels),
            "levels_missing_from_replay": missing,
            "max_level": max_lvl,
            "deaths": deaths,
            "wall_time_sec": round(wall_t, 2),
            "final_credits": save.credits,
            "final_shield_lvl": save.loadout.shield,
            "final_engine_lvl": save.loadout.engine,
            "final_main_type": save.loadout.main_type,
            "final_main_pulse_lvl":  save.loadout.main_pulse,
            "final_main_spread_lvl": save.loadout.main_spread,
            "final_main_vulcan_lvl": save.loadout.main_vulcan,
            "final_main_lvl": max(save.loadout.main_pulse,
                                   save.loadout.main_spread,
                                   save.loadout.main_vulcan),
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

        # Track enemy spawns + kills for the per-level kill-percentage metric.
        # We mark each enemy with a _bot_seen flag the first time we observe
        # it so we count exactly once per spawn even though the cleanup pass
        # in _update reassigns ps.enemies each frame.
        spawned = [0]
        killed = [0]
        WallCls = pewpew.Wall

        def mark_new_spawns():
            for e in ps.enemies:
                if not getattr(e, "_bot_seen", False):
                    e._bot_seen = True
                    if not isinstance(e, WallCls):
                        spawned[0] += 1

        orig_on_kill = ps._on_kill

        def counting_on_kill(enemy, drop=True):
            killed[0] += 1
            return orig_on_kill(enemy, drop=drop)

        ps._on_kill = counting_on_kill

        # Wave-overlap instrumentation: at each frame, count non-wall alive
        # enemies. From this we derive how often there are zero enemies on
        # screen (dead air = gap between waves) and the average pressure.
        alive_samples = []   # one int per frame
        dead_air_frames = 0
        longest_lull_frames = 0
        cur_lull = 0

        while outcome is None and frame_count < self._frame_cap_per_level:
            mark_new_spawns()
            self.brain.step(ps, controls)
            self.replay.record_frame(controls)
            ps._update(dt, controls)
            outcome = ps.outcome
            # Per-frame enemy-pressure sample (excluding walls).
            alive = 0
            for e in ps.enemies:
                if getattr(e, "alive", False) and not isinstance(e, WallCls):
                    alive += 1
            alive_samples.append(alive)
            if alive == 0:
                dead_air_frames += 1
                cur_lull += 1
                if cur_lull > longest_lull_frames:
                    longest_lull_frames = cur_lull
            else:
                cur_lull = 0
            frame_count += 1
            sim_t += dt
        # One final pass to catch spawns added on the closing frame.
        mark_new_spawns()

        won = False
        score = ps.score
        creds_gained = 0
        if outcome == "win":
            won = True
            creds_gained = ps.credits_earned
        elif outcome == "loss":
            won = False
        elif outcome is None:
            won = False

        # Per-level progress: how far through the level the bot got before
        # the attempt ended. Wins are 100%, losses are (elapsed / last
        # spawn time) clamped. The denominator is the time of the last
        # spawn event in the timeline — that's the moment the level
        # finishes spawning enemies and clearing them becomes the only
        # remaining task. For boss levels this caps progress at the boss
        # spawn instant; finer-grained boss-fight % would need separate
        # tracking but isn't worth the complexity yet.
        if won:
            progress_pct = 100.0
        else:
            tl = getattr(ps.level, "timeline", None)
            last_t = max((t for t, _ in tl), default=0.0) if tl else 0.0
            if last_t > 0:
                progress_pct = max(0.0, min(100.0, 100.0 * ps.elapsed / last_t))
            else:
                progress_pct = 0.0

        # Wave-overlap metrics
        if alive_samples:
            dead_air_pct = 100.0 * dead_air_frames / len(alive_samples)
            mean_alive = sum(alive_samples) / len(alive_samples)
            peak_alive = max(alive_samples)
        else:
            dead_air_pct = 0.0
            mean_alive = 0.0
            peak_alive = 0
        longest_lull_sec = longest_lull_frames * dt

        return (sim_t, frame_count, won, score, creds_gained,
                spawned[0], killed[0], progress_pct,
                dead_air_pct, longest_lull_sec, mean_alive, peak_alive)

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
            if (cur + 1) > save.unlocked_tier_shield:
                return None
            return pewpew.WEAPON_COSTS["shield"][cur]
        if kind == "engine":
            cur = lo.engine
            if cur >= pewpew.MAX_LEVELS["engine"]:
                return None
            if (cur + 1) > save.unlocked_tier_engine:
                return None
            return pewpew.WEAPON_COSTS["engine"][cur]
        if kind == "main_upgrade":
            cur = getattr(lo, f"main_{target}", 0)
            if cur == 0:
                return pewpew.MAIN_BUY_COST
            if cur >= pewpew.MAIN_WEAPON_MAX:
                return None
            unlocked = getattr(save, f"unlocked_tier_{target}", 2)
            if pewpew._main_tier(cur + 1) > unlocked:
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
            unlocked = getattr(save, f"unlocked_tier_{target}", 2)
            if (cur + 1) > unlocked:
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
            # main_type is driven by L1/R1 hold during play, not by which
            # weapon was last upgraded. Just bump the level.
            cur = getattr(lo, f"main_{target}", 0)
            new = min(pewpew.MAIN_WEAPON_MAX, cur + 1)
            setattr(lo, f"main_{target}", new)
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
