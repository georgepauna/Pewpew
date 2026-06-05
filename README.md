# Pewpew

A Tyrian-style vertical scrolling shooter for the **Anbernic RG35XX Pro**.
A hundred levels across ten branching star sectors, three weapon trees with
colour-coded enemy shields you have to swap mains to crack, side weapons,
abilities, and named save profiles — single Python file plus a sibling
pixel-art folder, sound and music synthesised in code at startup.

Targets MuOS / Knulli / Batocera (any RG35XX Pro CFW that ships Python +
Pygame). Also runs on a desktop for dev with `python pewpew.py`, and on
Steam Deck via the included auto-updating launcher.

## What's in it

- **640×480 native**: 480×480 playfield + 160-wide side HUD, no scaling
  artifacts on the device's panel.
- **Branching mission map**: 10 sectors × 10 levels with multiple paths
  through each sector and a boss to close it. Cleared levels can be
  replayed for credits.
- **Three main weapons**, each with its own behaviour and a five-tier
  upgrade curve:
  - **Rail Gun** — slow, high-damage hitscan beam.
  - **Vulcan** — rapid-fire stream.
  - **Ball** — chargeable AOE projectile that can absorb incoming enemy
    bullets while charging.
- **Colour-coded enemy shields** (blue / yellow / red) gate damage to the
  matching main — swap your active weapon mid-fight to break the right
  shield, or bounce off and dodge.
- **Side weapons**: homing missiles or auto-aim drones, both with their
  own tier track.
- **Shield + Engine upgrades**: max HP / regen and movement speed,
  five tiers each.
- **Three swappable abilities**: Pulse Bomb (damage all on-screen),
  Shield Burst (refill + brief invuln), Mega Laser (sustained beam).
- **Bombs**: consumable screen-clears, capped per loadout.
- **Five named profile slots** (galaxy names — Andromeda, Milky Way,
  Sombrero, Pinwheel, Whirlpool). Each profile keeps its own progress,
  save state, audio mix and binding.
- **In-game update channel**: the title screen polls GitHub for newer
  releases and surfaces the changelog; pressing the ability button on
  the overlay swaps the running build for the new one. Off by default,
  fully opt-in, falls back to the cached build if the network is gone.
- **Procedurally synthesized music + SFX**: no audio files ship with
  the game; tracks are generated and cached on first launch under
  `music_cache/` so the second boot is instant.

## Controls

Face buttons are mapped by **physical position**, so the same spot on the
pad always does the same thing — only the displayed silk letter changes
between platforms.

| Action            | RG35XX Pro (silk) | Xbox / PC pad (silk) | Keyboard                  |
|-------------------|-------------------|----------------------|---------------------------|
| Move              | D-Pad / L-stick   | D-Pad / L-stick      | WASD or Arrow keys        |
| Fire (hold)       | south (B)         | south (A)            | Mouse-1 / Numpad-2 / Enter|
| East (rewind/exit)| east (A)          | east (B)             | Space / Numpad-0          |
| Ability           | west (Y)          | west (X)             | C                         |
| Cancel / back     | north (X)         | north (Y)            | —                         |
| Swap to rail      | L1 (hold)         | L1 (hold)            | Mouse-wheel-up / Numpad-1 / Q |
| Charge ball       | R1 (hold)         | R1 (hold)            | Mouse-2 / Numpad-3 / E    |
| Pause             | START             | START                | Esc                       |
| Select            | SELECT            | SELECT               | Shift                     |
| Quit              | SELECT + START    | SELECT + START       | Alt+F4                    |

(Numpad keys need Num Lock on. Rail's wheel-up is a single shot per flick — it's a fire-and-cooldown weapon, so a momentary pulse is one shot.)

The right stick (or its keyboard mirrors) drives menu navigation on the
title and map screens, so you can browse without leaving the D-Pad
position you'd reach for in combat.

## Run on a PC

```bash
pip install pygame
python pewpew.py              # fullscreen 640×480
python pewpew.py --windowed   # windowed, integer-scaled with black bands
```

## Install on the RG35XX Pro

### MuOS (recommended)
1. Copy this folder to `MUOS/application/Pewpew/` on your SD card.
2. Make sure `launch.sh` keeps its executable bit (`chmod +x launch.sh`
   from a Linux/macOS shell before copying — Windows often strips it).
3. Boot MuOS, open **Applications → Pewpew**.

If MuOS doesn't pick it up, drop a `mux_launch.sh` symlink (or copy) of
`launch.sh` in the same folder — older MuOS builds look for that name.

### Knulli / Batocera
Copy the folder to `roms/pygame/Pewpew/`. It appears under the **Pygame**
system.

### Stock OS
Stock Anbernic firmware doesn't ship Python or a generic app-launching
mechanism. Use MuOS on a separate SD card (the RG35XX Pro has dual
slots — your stock OS card stays untouched).

## Install on a Steam Deck (auto-updating, launches from Game Mode)

The repo ships `pewpew_launcher.py` — a single Python script that clones
the repo on first run, pulls the latest `master` every time after, and
runs the game. Add it to Steam once and Game Mode always launches the
current build.

1. **Switch to Desktop Mode** (Steam → Power → Switch to Desktop) and
   open Konsole.

2. **Grab the launcher**:

   ```bash
   curl -L -o ~/pewpew_launcher.py \
        https://raw.githubusercontent.com/georgepauna/Pewpew/master/pewpew_launcher.py
   chmod +x ~/pewpew_launcher.py
   ```

3. **Add it as a non-Steam game**:
   1. Steam (Desktop) → Library → **Add a Game → Add a Non-Steam Game**.
   2. Pick any placeholder (e.g. Konsole) so the dialog accepts
      something, then press OK.
   3. Right-click the new entry → **Properties**.
   4. Set **Target** to `/usr/bin/python3`
   5. Set **Launch options** to `"/home/deck/pewpew_launcher.py"`
      (keep the quotes — Steam splits unquoted paths on spaces)
   6. Set **Start in** to `/home/deck/`
   7. (Optional) rename it to "Pewpew" and set a custom icon — the
      [contact sheet PNG](screenshots/contact_sheet.png) makes a fine
      grid art source.

4. **Back to Game Mode** (Steam → Power → Return to Gaming Mode).
   Pewpew appears in your library. Launching it auto-updates from
   GitHub before running.

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
  `~/.local/share/pewpew/launcher.log` so you can diagnose from
  Desktop Mode later.

To force a clean rebuild, delete `~/.local/share/pewpew/` — the next
launch re-clones the repo and re-creates the venv.

## Install on Powkiddy RGB10 Max 3 (ROCKNIX / JELOS)

ROCKNIX ships Python 3 but not pygame, so the game needs an aarch64 pygame
wheel bundled alongside it. The helper script does that and installs the
whole thing onto the SD card — **run it on a desktop Linux / SteamOS box
that can write the card** (it cross-downloads the ARM wheel with nothing but
Python's standard library — no `pip`, no `unzip`, no system changes).

Insert the card, find its `ports` dir, then run (replace `/rom/ports` with
your card's actual ports path):

```bash
curl -fsSL https://raw.githubusercontent.com/georgepauna/Pewpew/master/rocknix_setup.sh | PORTS=/rom/ports bash
```

That clones the repo, fetches pygame for Python 3.10/3.11/3.12 (the device's
`launch.sh` auto-picks the matching one), copies the bundle to
`PORTS/Pewpew/`, and writes the `Pewpew.sh` Ports entry. Eject, put the card
in the Max 3, open **PORTS → Pewpew**.

If it doesn't start, read `PORTS/Pewpew/last_run.log` back on the desktop —
its header reports the device Python version, whether pygame imported, the
resolved SDL drivers, and the chosen `PYTHONPATH`.

## Save file

A single `save.json` lives next to `pewpew.py` after your first run; it
holds all five profile slots side by side. Pick / rename / wipe profiles
from the title screen — no need to touch the file by hand. To put it
elsewhere, set `PEWPEW_SAVE=/path/to/file.json` before launching. To
nuke everything, delete the file.

## File layout

```
Pewpew/
├── pewpew.py            # game — single file
├── pewpew_launcher.py   # Steam Deck auto-updating launcher (optional)
├── launch.sh            # RG35XX Pro launcher (sets SDL drivers, locates python)
├── art/                 # PNG sprite sheets + sprite_engine.json hitbox/pivot tables
├── music_cache/         # procedural music tracks cached after first launch
├── screenshots/         # contact sheet + reference shots
└── README.md
```

## Why Pygame

Pygame uses SDL2, which is already on the RG35XX Pro for RetroArch.
640×480 matches the panel 1:1. The Cortex-A53 has plenty of headroom
for a 2D shooter at 60 fps, and Pygame ships preinstalled on every
major CFW.

Pixel art lives in `art/` as PNG sprite sheets sliced at load time
against a manifest (`art/sprite_engine.json`) that carries each
sprite's hitbox + pivot. Sounds and music are synthesised in code
(square waves, shaped noise, simple polyphonic mixing) — the music
tracks are cached to `music_cache/` after their first generation so
boot stays fast. Saving is the only thing the game writes at runtime.
