"""Regenerate the progression plot for an existing run directory.

Usage:
  python -m tuning.bot.replot                       # latest run dir
  python -m tuning.bot.replot tuning/runs/0001-…/    # specific dir

Reads the per-profile `telemetry-*.json` and `meta.json` from the
directory and re-runs the plot — useful for picking up plot.py changes
without burning another ~85 seconds of sim time.
"""

import json
import sys
from pathlib import Path

from .plot import plot_telemetries


def main():
    if len(sys.argv) > 1:
        run_dir = Path(sys.argv[1])
    else:
        runs_root = Path("tuning/runs")
        candidates = sorted(
            (p for p in runs_root.iterdir() if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
        )
        if not candidates:
            print("[replot] no run directories under tuning/runs/")
            sys.exit(1)
        run_dir = candidates[-1]

    print(f"[replot] {run_dir}")
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    snapshot_id = meta.get("snapshot_id", "?")
    lever_values = meta.get("levers") or {}

    telemetries = {}
    for f in sorted(run_dir.glob("telemetry-*.json")):
        profile = f.stem.replace("telemetry-", "")
        telemetries[profile] = json.loads(f.read_text())
    if not telemetries:
        print(f"[replot] no telemetry-*.json found in {run_dir}")
        sys.exit(1)

    out = run_dir / "progression.png"
    plot_telemetries(telemetries, out, snapshot_id, lever_values)
    print(f"[replot] -> {out}")


if __name__ == "__main__":
    main()
