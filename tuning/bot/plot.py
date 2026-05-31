"""Plot a bot run.

Reads the telemetry dicts produced by session.BotSession.run() and lays
out one figure with multiple subplots showing how each profile progressed
through the game.
"""

import os
os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# One distinct hue per archetype so every plot reads at a glance. Picked
# to be hue-separated AND luminance-separated so they print legibly in
# grayscale too.
PROFILE_COLORS = {
    "scrub":       "#888888",   # gray   — the "everyman"
    "casual":      "#f4a93b",   # amber  — relaxed
    "focused":     "#1f77b4",   # blue   — systems player
    "speedrunner": "#e74c3c",   # red    — fast + risky
    "tank":        "#2ca02c",   # green  — defensive
    "expert":      "#9467bd",   # purple — top tier
}
# Solid lines everywhere — each archetype already has a unique colour, so
# linestyle was the legacy disambiguator and isn't needed any more.
PROFILE_LINESTYLES = {k: "-" for k in PROFILE_COLORS}


def plot_telemetries(telemetries, out_path, snapshot_id, lever_values=None):
    fig, axes = plt.subplots(4, 3, figsize=(18, 18))
    title = f"Bot run vs snapshot {snapshot_id}"
    if lever_values:
        # Concise lever description in subtitle so we can tell A/B runs apart.
        parts = []
        for k, v in lever_values.items():
            parts.append(f"{k}={v}")
        title += "    levers: " + ", ".join(parts)
    fig.suptitle(title, fontsize=14, y=0.997)

    ax_lvl, ax_credits, ax_shield = axes[0]
    ax_main, ax_engine, ax_deaths = axes[1]
    ax_killpct, ax_killpct_attempt, ax_skips = axes[2]
    ax_progress, ax_progress_scatter, ax_deadair = axes[3]

    ax_lvl.set_title("Legit boss wins over time (force-skips don't count)")
    ax_lvl.set_xlabel("Simulated time (s)")
    ax_lvl.set_ylabel("Boss wins")
    ax_lvl.set_ylim(-0.5, 10.5)
    ax_lvl.set_yticks(range(0, 11))

    ax_credits.set_title("Credits on hand")
    ax_credits.set_xlabel("Simulated time (s)")
    ax_credits.set_ylabel("Credits")

    ax_shield.set_title("Shield level (max 5)")
    ax_shield.set_xlabel("Simulated time (s)")
    ax_shield.set_ylabel("Shield Lv")
    ax_shield.set_ylim(0.5, 5.5)

    ax_main.set_title("Main weapon level — max across rail/ball/vulcan (max 20)")
    ax_main.set_xlabel("Simulated time (s)")
    ax_main.set_ylabel("Main Lv")
    ax_main.set_ylim(0.5, 20.5)

    ax_engine.set_title("Engine level (max 5)")
    ax_engine.set_xlabel("Simulated time (s)")
    ax_engine.set_ylabel("Engine Lv")
    ax_engine.set_ylim(0.5, 5.5)

    ax_deaths.set_title("Cumulative deaths")
    ax_deaths.set_xlabel("Simulated time (s)")
    ax_deaths.set_ylabel("Deaths")

    ax_killpct.set_title("Enemies killed % (winning attempt per level)")
    ax_killpct.set_xlabel("Level")
    ax_killpct.set_ylabel("Kill %")
    ax_killpct.set_ylim(0, 105)
    ax_killpct.set_xlim(0, 101)

    ax_killpct_attempt.set_title("Enemies killed % (all attempts, scatter)")
    ax_killpct_attempt.set_xlabel("Level")
    ax_killpct_attempt.set_ylabel("Kill %")
    ax_killpct_attempt.set_ylim(0, 105)
    ax_killpct_attempt.set_xlim(0, 101)

    ax_skips.set_title("Max attempts per level (higher = harder for this profile)")
    ax_skips.set_xlabel("Level")
    ax_skips.set_ylabel("Max attempt count")
    ax_skips.set_xlim(0, 101)

    ax_progress.set_title("Avg in-level progress % across attempts")
    ax_progress.set_xlabel("Level")
    ax_progress.set_ylabel("Progress %")
    ax_progress.set_ylim(0, 105)
    ax_progress.set_xlim(0, 101)

    ax_progress_scatter.set_title("In-level progress % (all attempts, scatter)")
    ax_progress_scatter.set_xlabel("Level")
    ax_progress_scatter.set_ylabel("Progress %")
    ax_progress_scatter.set_ylim(0, 105)
    ax_progress_scatter.set_xlim(0, 101)

    ax_deadair.set_title("Min difficulty_adjust reached per level (0 = no help; deeper = more help)")
    ax_deadair.set_xlabel("Level")
    ax_deadair.set_ylabel("Min difficulty_adjust")
    ax_deadair.set_xlim(0, 101)

    for name, tele in telemetries.items():
        evs = tele.get("events", [])
        if not evs:
            continue
        ts = [ev["wall_time_total"] for ev in evs]
        creds = [ev["credits_total"] for ev in evs]
        shields = [ev["shield_lvl"] for ev in evs]
        mains = [ev["main_lvl"] for ev in evs]
        engines = [ev["engine_lvl"] for ev in evs]
        deaths = [ev["deaths_total"] for ev in evs]
        # "Legit boss wins over time" — counts every event whose level
        # is a boss (every 10th) AND ev["won"]=True. Force-skipped bosses
        # don't count. With force-skip-rewards on, ~every profile reaches
        # L100 so the old "highest level completed" curve was flat at 100
        # for everyone; legit-boss-wins differentiates real progression.
        cum_boss_wins = 0
        lvl_curve = []
        for ev in evs:
            if ev["won"]:
                try:
                    n = int(ev["level"][1:])
                except (ValueError, IndexError):
                    n = 0
                if n > 0 and n % 10 == 0:
                    cum_boss_wins += 1
            lvl_curve.append(cum_boss_wins)

        # Kill % per level: take the winning attempt if any, else the
        # latest attempt — that's the "result" we'd want to look at.
        # Also collect all attempts for the scatter subplot.
        per_level_pct = {}
        scatter_x = []
        scatter_y = []
        for ev in evs:
            n = int(ev["level"][1:])
            pct = float(ev.get("kill_pct") or 0.0)
            scatter_x.append(n)
            scatter_y.append(pct)
            cur = per_level_pct.get(n)
            # Prefer the winning attempt's kill % over an earlier loss.
            if cur is None or (ev["won"] and not cur[1]) or (
                    ev["won"] == cur[1]):
                per_level_pct[n] = (pct, ev["won"])
        levels_sorted = sorted(per_level_pct.keys())
        line_x = levels_sorted
        line_y = [per_level_pct[n][0] for n in levels_sorted]

        # Max attempts per level (line plot, x=level 1..100, y=max attempt).
        # Bigger number = bot retried that level more times before clearing
        # (or in the case of retry-cap=999, just kept dying). With force-
        # skips effectively gone in the new run mode, this is the most
        # direct "how hard was this level for this profile" signal.
        max_attempts_per_level = {}
        for ev in evs:
            n = int(ev["level"][1:])
            a = int(ev.get("attempt", 1))
            if a > max_attempts_per_level.get(n, 0):
                max_attempts_per_level[n] = a
        att_x = sorted(max_attempts_per_level.keys())
        att_y = [max_attempts_per_level[n] for n in att_x]

        # Avg in-level progress per level (across attempts). Levels that
        # took multiple attempts pull the average down even if the bot
        # eventually won — that captures "how hard was this level for
        # this bot" better than the best-case attempt.
        progress_attempts_per_level = {}
        progress_scatter_x = []
        progress_scatter_y = []
        for ev in evs:
            n = int(ev["level"][1:])
            p = float(ev.get("progress_pct") or 0.0)
            progress_scatter_x.append(n)
            progress_scatter_y.append(p)
            progress_attempts_per_level.setdefault(n, []).append(p)
        prog_levels = sorted(progress_attempts_per_level.keys())
        prog_x = prog_levels
        prog_y = [
            sum(progress_attempts_per_level[n]) / len(progress_attempts_per_level[n])
            for n in prog_levels
        ]

        # Min difficulty_adjust reached per level. A bigger negative
        # value = the adaptive system had to fight harder to nudge this
        # level toward the bot. Floor at 0 means the bot waltzed through
        # without help. Older telemetry without the field just shows 0.
        min_adj_per_level = {}
        for ev in evs:
            n = int(ev["level"][1:])
            a = float(ev.get("difficulty_adjust") or 0.0)
            if a < min_adj_per_level.get(n, 0.0):
                min_adj_per_level[n] = a
        adj_x = sorted(min_adj_per_level.keys())
        adj_y = [min_adj_per_level[n] for n in adj_x]

        color = PROFILE_COLORS.get(name, "gray")
        ls = PROFILE_LINESTYLES.get(name, "-")
        kw = dict(label=name, color=color, linewidth=1.7, linestyle=ls)
        ax_lvl.step(ts, lvl_curve, where="post", **kw)
        ax_credits.plot(ts, creds, **kw)
        ax_shield.step(ts, shields, where="post", **kw)
        ax_main.step(ts, mains, where="post", **kw)
        ax_engine.step(ts, engines, where="post", **kw)
        ax_deaths.step(ts, deaths, where="post", **kw)
        ax_killpct.plot(line_x, line_y, **kw)
        ax_killpct_attempt.scatter(scatter_x, scatter_y, s=10,
                                    color=color, alpha=0.55, label=name)
        ax_skips.plot(att_x, att_y, **kw)
        ax_progress.plot(prog_x, prog_y, **kw)
        ax_progress_scatter.scatter(progress_scatter_x, progress_scatter_y,
                                    s=10, color=color, alpha=0.55, label=name)
        ax_deadair.plot(adj_x, adj_y, **kw)

    for ax in (ax_lvl, ax_credits, ax_shield, ax_main, ax_engine, ax_deaths,
               ax_killpct, ax_killpct_attempt, ax_skips,
               ax_progress, ax_progress_scatter, ax_deadair):
        ax.legend(fontsize=8, loc="best")
        ax.grid(True, alpha=0.3)

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.savefig(out_path, dpi=110)
    plt.close(fig)
