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


def plot_telemetries(telemetries, out_path, snapshot_id):
    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    fig.suptitle(f"Bot run vs snapshot {snapshot_id}", fontsize=14, y=0.995)

    ax_lvl, ax_credits, ax_shield = axes[0]
    ax_main, ax_engine, ax_deaths = axes[1]

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
        color = PROFILE_COLORS.get(name, "gray")
        ls = PROFILE_LINESTYLES.get(name, "-")
        kw = dict(label=name, color=color, linewidth=1.7, linestyle=ls)
        ax_lvl.plot(ts, lvl_curve, **kw)
        ax_credits.plot(ts, creds, **kw)
        ax_shield.step(ts, shields, where="post", **kw)
        ax_main.step(ts, mains, where="post", **kw)
        ax_engine.step(ts, engines, where="post", **kw)
        ax_deaths.step(ts, deaths, where="post", **kw)

    for ax in axes.flatten():
        ax.legend(fontsize=8, loc="best")
        ax.grid(True, alpha=0.3)

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.savefig(out_path, dpi=110)
    plt.close(fig)
