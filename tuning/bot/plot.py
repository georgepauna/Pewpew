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


PROFILE_COLORS = {
    "good_optimal": "#1f77b4",
    "good_avg":     "#7baacf",
    "med_optimal":  "#2ca02c",
    "med_avg":      "#8bcf8b",
    "bad_optimal":  "#d62728",
    "bad_avg":      "#e88a8a",
}
PROFILE_LINESTYLES = {
    "good_optimal": "-",  "good_avg": "--",
    "med_optimal":  "-",  "med_avg":  "--",
    "bad_optimal":  "-",  "bad_avg":  "--",
}


def plot_telemetries(telemetries, out_path, snapshot_id, lever_values=None):
    fig, axes = plt.subplots(3, 3, figsize=(18, 14))
    title = f"Bot run vs snapshot {snapshot_id}"
    if lever_values:
        # Concise lever description in subtitle so we can tell A/B runs apart.
        parts = []
        for k, v in lever_values.items():
            parts.append(f"{k}={v}")
        title += "    levers: " + ", ".join(parts)
    fig.suptitle(title, fontsize=14, y=0.995)

    ax_lvl, ax_credits, ax_shield = axes[0]
    ax_main, ax_engine, ax_deaths = axes[1]
    ax_killpct, ax_killpct_attempt, ax_skips = axes[2]

    ax_lvl.set_title("Highest level completed")
    ax_lvl.set_xlabel("Simulated time (s)")
    ax_lvl.set_ylabel("Level")

    ax_credits.set_title("Credits on hand")
    ax_credits.set_xlabel("Simulated time (s)")
    ax_credits.set_ylabel("Credits")

    ax_shield.set_title("Shield level")
    ax_shield.set_xlabel("Simulated time (s)")
    ax_shield.set_ylabel("Shield Lv")
    ax_shield.set_ylim(0.5, 5.5)

    ax_main.set_title("Main weapon level")
    ax_main.set_xlabel("Simulated time (s)")
    ax_main.set_ylabel("Main Lv")
    ax_main.set_ylim(0.5, 5.5)

    ax_engine.set_title("Engine level")
    ax_engine.set_xlabel("Simulated time (s)")
    ax_engine.set_ylabel("Engine Lv")
    ax_engine.set_ylim(0.5, 3.5)

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

    ax_skips.set_title("Cumulative force-skips (3-loss give-ups)")
    ax_skips.set_xlabel("Simulated time (s)")
    ax_skips.set_ylabel("Force-skips")

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
        max_lvl = 0
        lvl_curve = []
        for ev in evs:
            if ev["won"]:
                n = int(ev["level"][1:])
                if n > max_lvl:
                    max_lvl = n
            lvl_curve.append(max_lvl)

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

        # Force-skips: a level was force-skipped when its FINAL attempt
        # was a loss (i.e. the bot ran out of retries on that level). The
        # event ordering of the telemetry preserves attempts per level so
        # we can mark each final-loss event and accumulate over time.
        last_event_per_level = {}
        for ev in evs:
            last_event_per_level[ev["level"]] = ev
        cum_skips = 0
        skips_curve = []
        for ev in evs:
            if (last_event_per_level[ev["level"]] is ev) and not ev["won"]:
                cum_skips += 1
            skips_curve.append(cum_skips)

        color = PROFILE_COLORS.get(name, "gray")
        ls = PROFILE_LINESTYLES.get(name, "-")
        kw = dict(label=name, color=color, linewidth=1.7, linestyle=ls)
        ax_lvl.plot(ts, lvl_curve, **kw)
        ax_credits.plot(ts, creds, **kw)
        ax_shield.step(ts, shields, where="post", **kw)
        ax_main.step(ts, mains, where="post", **kw)
        ax_engine.step(ts, engines, where="post", **kw)
        ax_deaths.step(ts, deaths, where="post", **kw)
        ax_killpct.plot(line_x, line_y, **kw)
        ax_killpct_attempt.scatter(scatter_x, scatter_y, s=10,
                                    color=color, alpha=0.55, label=name)
        ax_skips.step(ts, skips_curve, where="post", **kw)

    for ax in (ax_lvl, ax_credits, ax_shield, ax_main, ax_engine, ax_deaths,
               ax_killpct, ax_killpct_attempt, ax_skips):
        ax.legend(fontsize=8, loc="best")
        ax.grid(True, alpha=0.3)

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.savefig(out_path, dpi=110)
    plt.close(fig)
