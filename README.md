# Pewpew

A Tyrian-style vertical scrolling shooter for the **Anbernic RG35XX Pro**.
Branching missions, weapon upgrades, abilities, and varied enemies — single file,
no external assets (every sprite and sound effect is generated in code at startup).

Targets MuOS / Knulli / Batocera (any RG35XX Pro CFW that ships Python + Pygame).
Also runs on a desktop for dev with `python pewpew.py`.

## What's in it

- **640×480 native**: 480×480 playfield + 160-wide side HUD, no scaling artifacts on the device's panel.
- **Branching mission map**: 5 nodes in a small node-graph with two paths converging on a boss.
- **6 enemy types**: scout (sine-weave), gunner (aimed shots), weaver (powerup carrier), bomber (spread shot), kamikaze (homes onto you), turret (stationary). Plus a multi-phase boss.
- **Weapon upgrades**:
  - Main cannon (L1–L5): single → dual → triple spread → quad → quad + wing.
  - Side missiles (L0–L3): auto-targeting homing missiles.
  - Shield generator (L1–L5): max HP and regen.
  - Engine (L1–L3): movement speed.
- **3 swappable abilities**: Pulse Bomb (damage all on-screen), Shield Burst (refill + brief invuln), Mega Laser (sustained beam).
- **Bombs**: consumable screen-clears, capped at 9.
- **Persistent save** between runs: credits, upgrades, completed nodes, high score. Save file lives next to the script.

## Controls

| Action            | RG35XX Pro       | Desktop      |
|-------------------|------------------|--------------|
| Move              | D-Pad            | Arrow keys   |
| Fire (hold)       | B                | Z or Space   |
| Bomb              | A                | X            |
| Ability           | X                | C            |
| Confirm / launch  | B                | Enter / Z    |
| Cancel / shop     | Y                | Esc          |
| Pause             | START            | P            |
| Quit              | SELECT + START   | Alt+F4       |

Joystick button indices follow the most common RG35XX Pro mapping (A=0, B=1, X=2,
Y=3, L1=4, R1=5, SELECT=6, START=7, MENU=8). If your firmware reports different
numbers, edit the `JOY_*` constants near the top of `pewpew.py`.

## Run on a PC

```bash
pip install pygame
python pewpew.py              # fullscreen 640×480
python pewpew.py --windowed   # windowed
```

## Install on the RG35XX Pro

### MuOS (recommended)
1. Copy this folder to `MUOS/application/Pewpew/` on your SD card.
2. Make sure `launch.sh` keeps its executable bit (`chmod +x launch.sh` from a
   Linux/macOS shell before copying — Windows often strips it).
3. Boot MuOS, open **Applications → Pewpew**.

If MuOS doesn't pick it up, drop a `mux_launch.sh` symlink (or copy) of
`launch.sh` in the same folder — older MuOS builds look for that name.

### Knulli / Batocera
Copy the folder to `roms/pygame/Pewpew/`. It appears under the **Pygame** system.

### Stock OS
Stock Anbernic firmware doesn't ship Python or a generic app-launching
mechanism. Use MuOS on a separate SD card (the RG35XX Pro has dual slots — your
stock OS card stays untouched).

## Install on a Steam Deck (auto-updating, launches from Game Mode)

The repo ships `pewpew_launcher.py` — a single Python script that clones the
repo on first run, pulls the latest `master` every time after, and runs the
game. Add it to Steam once and Game Mode always launches the current build.

1. **Switch to Desktop Mode** (Steam → Power → Switch to Desktop) and open
   Konsole.

2. **Grab the launcher**:

   ```bash
   curl -L -o ~/pewpew_launcher.py \
        https://raw.githubusercontent.com/georgepauna/Pewpew/master/pewpew_launcher.py
   chmod +x ~/pewpew_launcher.py
   ```

3. **Add it as a non-Steam game**:
   1. Steam (Desktop) → Library → **Add a Game → Add a Non-Steam Game**.
   2. Pick any placeholder (e.g. Konsole) so the dialog accepts something,
      then press OK.
   3. Right-click the new entry → **Properties**.
   4. Set **Target** to `/usr/bin/python3`
   5. Set **Launch options** to `"/home/deck/pewpew_launcher.py"`
      (keep the quotes — Steam splits unquoted paths on spaces)
   6. Set **Start in** to `/home/deck/`
   7. (Optional) rename it to "Pewpew" and set a custom icon — the
      [contact sheet PNG](screenshots/contact_sheet.png) makes a fine
      grid art source.

4. **Back to Game Mode** (Steam → Power → Return to Gaming Mode). Pewpew
   appears in your library. Launching it auto-updates from GitHub before
   running.

The launcher behaves gracefully:
- **No network?** Cached copy still runs (you get the last version that
  successfully pulled).
- **pygame missing?** It creates a private venv at
  `~/.local/share/pewpew/venv` and installs pygame inside it.
  SteamOS's read-only base + multi-arch lib paths make
  `pip install --user` flaky (the symptom is a "wrong ELF class"
  error), but a self-contained venv ships pygame's own SDL2 and
  bypasses every system-level conflict.
- **Something broke in Game Mode?** Logs land in
  `~/.local/share/pewpew/launcher.log` so you can diagnose from Desktop
  Mode later.

To force a clean rebuild, delete `~/.local/share/pewpew/` — the next
launch re-clones the repo and re-creates the venv.

## Save file

A `save.json` is written next to `pewpew.py` after your first run. To wipe
progress, delete it. To put it elsewhere, set `PEWPEW_SAVE=/path/to/file.json`
before launching.

## File layout

```
Pewpew/
├── pewpew.py    # game — single file, ~1600 lines
├── launch.sh    # firmware launcher (sets SDL drivers, locates python)
└── README.md
```

## Why Pygame

Pygame uses SDL2, which is already on the RG35XX Pro for RetroArch.
640×480 matches the panel 1:1. The Cortex-A53 has plenty of headroom for a
2D shooter at 60fps, and Pygame ships preinstalled on every major CFW.

Pixel art is hand-defined as ASCII grids inside `pewpew.py` (search for
`PLAYER_GRID`, `BOSS_GRID`, etc.) and scaled nearest-neighbor at load. Sounds
are synthesized in code (square waves and shaped noise). The entire game is
two files; the save file is the only thing written at runtime.
