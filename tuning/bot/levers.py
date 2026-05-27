"""Sim-only knob overrides.

Each lever knows how to mutate pewpew module-level state (or the in-memory
app) for the duration of a bot session and how to revert it afterwards.
The bot session driver applies all requested levers before running its
profiles and reverts them when finished — so the LIVE game (launched
without --bot) is never affected.

Once a tuning value is confirmed by simulation, the next step is to
promote it: edit pewpew.py with the new constant, re-snapshot, and from
then on the lever's "default" should become the new live value.

Lever schema:
  - key:      short CLI handle (also used in run-folder names)
  - default:  live-game value (informational)
  - apply(pewpew_mod, app, value) -> revert callable
  - describe(value) -> short human-readable string

Add new levers here as we tune. CLI: `--levers=dmg=2.0,incm=0.5,lmul=2.0`
"""


# ---------------------------------------------------------------------------
# incm — income multiplier
# ---------------------------------------------------------------------------

def apply_income_mul(pewpew_mod, app, mul):
    """Scale every source of credits the player earns by `mul`.

    Touches both halves of the income economy:
      - enemy bounty (Enemy.CREDITS class attr) — paid on kill
      - money pickups (Player.collect returns 50 by default; we wrap it)
    """
    mul = float(mul)
    enemy_classes = [
        pewpew_mod.Scout, pewpew_mod.Gunner, pewpew_mod.Weaver,
        pewpew_mod.Kamikaze, pewpew_mod.Turret, pewpew_mod.Bomber,
        pewpew_mod.Asteroid, pewpew_mod.BigAsteroid, pewpew_mod.Mine,
        pewpew_mod.Pylon, pewpew_mod.Crystal, pewpew_mod.Wall,
        pewpew_mod.Boss,
    ]
    original = {cls.__name__: cls.CREDITS for cls in enemy_classes}
    for cls in enemy_classes:
        cls.CREDITS = max(0, int(round(cls.CREDITS * mul)))

    original_collect = pewpew_mod.Player.collect

    def patched_collect(self, pickup):
        result = original_collect(self, pickup)
        if (isinstance(result, tuple) and len(result) == 2
                and result[0] == "credits"):
            return ("credits", max(0, int(round(result[1] * mul))))
        return result

    pewpew_mod.Player.collect = patched_collect

    def revert():
        for cls in enemy_classes:
            cls.CREDITS = original[cls.__name__]
        pewpew_mod.Player.collect = original_collect

    return revert


# ---------------------------------------------------------------------------
# dmg — damage taken multiplier
# ---------------------------------------------------------------------------

def apply_damage_taken_mul(pewpew_mod, app, mul):
    """Scale every source of incoming damage to the player by `mul`.

    Wraps Player.take_damage — the single funnel that bullets (2), body
    contact (8), and mine AoE (6) all flow through. A minimum of 1 is
    enforced for non-zero damage so mul<1 can't make hits free.
    """
    mul = float(mul)
    original = pewpew_mod.Player.take_damage

    def patched(self, dmg):
        if dmg > 0:
            scaled = int(round(dmg * mul))
            if scaled <= 0 and mul > 0:
                scaled = 1
            dmg = scaled
        return original(self, dmg)

    pewpew_mod.Player.take_damage = patched

    def revert():
        pewpew_mod.Player.take_damage = original
    return revert


# ---------------------------------------------------------------------------
# lmul — level difficulty multiplier (slope of the per-level HP curve)
# ---------------------------------------------------------------------------

def apply_level_difficulty_mul(pewpew_mod, app, mul):
    """Multiply the slope of the per-level difficulty curve by `mul`.

    Live formula in make_levels: `difficulty = 1.0 + (n-1) * 0.025`.
    With this lever the slope (0.025) is replaced by `0.025 * mul`, so
    mul=2.0 doubles how fast enemy HP grows with level number. The
    difficulty multiplier scales every non-boss enemy's HP at spawn
    (PlayState._scale_enemy reads it from `level.difficulty`).

    We patch the *already-constructed* app.levels dict so the new slope
    takes effect for the bot session without rebuilding levels.
    """
    mul = float(mul)
    BASE_SLOPE = 0.025
    original = {key: lvl.difficulty for key, lvl in app.levels.items()}
    for key, lvl in app.levels.items():
        try:
            n = int(key[1:])
        except ValueError:
            continue
        lvl.difficulty = 1.0 + (n - 1) * BASE_SLOPE * mul

    def revert():
        for key, lvl in app.levels.items():
            if key in original:
                lvl.difficulty = original[key]

    return revert


# ---------------------------------------------------------------------------
# ehp — enemy HP multiplier (applied on top of per-level difficulty curve)
# ---------------------------------------------------------------------------

def apply_enemy_hp_mul(pewpew_mod, app, mul):
    """Scale every enemy's spawn HP by `mul`. Wrapping the base Enemy
    __init__ catches all subclasses (Scout, Boss, etc.) because they all
    pass their target hp= up through super().__init__. Combines
    multiplicatively with the per-level difficulty multiplier and the
    boss hp_mul, so an L100 boss under `ehp=2.0` is HP * 4.15 * 2 of
    the base.
    """
    mul = float(mul)
    orig = pewpew_mod.Enemy.__init__

    def patched(self, x, y, asset, hp=1, flash_asset=None, sprite_name=""):
        scaled = max(1, int(round(hp * mul)))
        return orig(self, x, y, asset, hp=scaled,
                    flash_asset=flash_asset, sprite_name=sprite_name)

    pewpew_mod.Enemy.__init__ = patched

    def revert():
        pewpew_mod.Enemy.__init__ = orig
    return revert


# ---------------------------------------------------------------------------
# enemyspd — drift / pass-through speed multiplier (excludes bullets,
# excludes the kamikaze dive itself — only the pre-dive drift phase)
# ---------------------------------------------------------------------------

def apply_enemy_speed_mul(pewpew_mod, app, mul):
    """Scale `self.speed` on every enemy at construction time.

    For passing enemies (Scout, Weaver, asteroids, mine, crystal, pylon)
    this means they cross the screen slower so they linger longer in
    each wave.

    For anchored shooters (Gunner, Turret) this slows the entry phase
    before they stop — they take longer to reach firing position.

    Kamikaze stores drift speed in self.vy (set in __init__, replaced
    on lock-on). Scaling that vy delays the dive trigger: the kamikaze
    drifts past y=40 slower, so it spends longer pre-dive. Once it
    locks on, the dive uses its own hardcoded 260 px/s — unaffected.

    Bullets are NOT scaled by this lever (their vx/vy come from the
    Bullet ctor, not self.speed).
    """
    mul = float(mul)
    classes = [
        pewpew_mod.Scout, pewpew_mod.Gunner, pewpew_mod.Weaver,
        pewpew_mod.Kamikaze, pewpew_mod.Turret, pewpew_mod.Bomber,
        pewpew_mod.Asteroid, pewpew_mod.BigAsteroid, pewpew_mod.Mine,
        pewpew_mod.Pylon, pewpew_mod.Crystal,
    ]
    KamiCls = pewpew_mod.Kamikaze
    originals = {cls: cls.__init__ for cls in classes}

    def make_patched(orig, cls):
        is_kami = (cls is KamiCls)

        def patched(self, *args, **kwargs):
            orig(self, *args, **kwargs)
            if hasattr(self, "speed") and isinstance(self.speed, (int, float)):
                self.speed = self.speed * mul
            if is_kami:
                # Initial drift vy (pre-acquire) — scaled. Lock-on later
                # overwrites self.vy with the dive vector, so the dive
                # itself stays at its native speed.
                self.vy = self.vy * mul
        return patched

    for cls in classes:
        cls.__init__ = make_patched(originals[cls], cls)

    def revert():
        for cls, orig in originals.items():
            cls.__init__ = orig
    return revert


# ---------------------------------------------------------------------------
# cost — universal upgrade-cost multiplier
# ---------------------------------------------------------------------------

def apply_cost_mul(pewpew_mod, app, mul):
    """Scale every credit price the player pays at the shop by `mul`.

    Touches: MAIN_UPGRADE_COSTS, SIDE_UPGRADE_COSTS, WEAPON_COSTS (shield
    + engine), MAIN_BUY_COST, SIDE_BUY_COST, BOMB_PRICE. The session-side
    cost lookup walks these constants live, so the bot sees the scaled
    prices without any further plumbing.
    """
    mul = float(mul)

    def scale_list(lst):
        return [int(round(v * mul)) for v in lst]

    orig_main   = {k: list(v) for k, v in pewpew_mod.MAIN_UPGRADE_COSTS.items()}
    orig_side   = {k: list(v) for k, v in pewpew_mod.SIDE_UPGRADE_COSTS.items()}
    orig_weapon = {k: list(v) for k, v in pewpew_mod.WEAPON_COSTS.items()}
    orig_main_buy = pewpew_mod.MAIN_BUY_COST
    orig_side_buy = pewpew_mod.SIDE_BUY_COST
    orig_bomb     = pewpew_mod.BOMB_PRICE

    for k in pewpew_mod.MAIN_UPGRADE_COSTS:
        pewpew_mod.MAIN_UPGRADE_COSTS[k] = scale_list(orig_main[k])
    for k in pewpew_mod.SIDE_UPGRADE_COSTS:
        pewpew_mod.SIDE_UPGRADE_COSTS[k] = scale_list(orig_side[k])
    for k in pewpew_mod.WEAPON_COSTS:
        pewpew_mod.WEAPON_COSTS[k] = scale_list(orig_weapon[k])
    pewpew_mod.MAIN_BUY_COST = int(round(orig_main_buy * mul))
    pewpew_mod.SIDE_BUY_COST = int(round(orig_side_buy * mul))
    pewpew_mod.BOMB_PRICE    = int(round(orig_bomb * mul))

    def revert():
        for k in pewpew_mod.MAIN_UPGRADE_COSTS:
            pewpew_mod.MAIN_UPGRADE_COSTS[k] = orig_main[k]
        for k in pewpew_mod.SIDE_UPGRADE_COSTS:
            pewpew_mod.SIDE_UPGRADE_COSTS[k] = orig_side[k]
        for k in pewpew_mod.WEAPON_COSTS:
            pewpew_mod.WEAPON_COSTS[k] = orig_weapon[k]
        pewpew_mod.MAIN_BUY_COST = orig_main_buy
        pewpew_mod.SIDE_BUY_COST = orig_side_buy
        pewpew_mod.BOMB_PRICE    = orig_bomb

    return revert


LEVERS = {
    "incm": {
        "default": 1.0,
        "apply": apply_income_mul,
        "describe": lambda v: f"income x{v}",
    },
    "dmg": {
        "default": 1.0,
        "apply": apply_damage_taken_mul,
        "describe": lambda v: f"dmg-in x{v}",
    },
    "lmul": {
        "default": 1.0,
        "apply": apply_level_difficulty_mul,
        "describe": lambda v: f"diff-slope x{v}",
    },
    "cost": {
        "default": 1.0,
        "apply": apply_cost_mul,
        "describe": lambda v: f"cost x{v}",
    },
    "ehp": {
        "default": 1.0,
        "apply": apply_enemy_hp_mul,
        "describe": lambda v: f"enemy-hp x{v}",
    },
    "enemyspd": {
        "default": 1.0,
        "apply": apply_enemy_speed_mul,
        "describe": lambda v: f"enemy-speed x{v}",
    },
}


def apply_levers(pewpew_mod, app, lever_values):
    """Apply all requested levers; returns a single revert callable."""
    reverts = []
    for name, val in lever_values.items():
        spec = LEVERS.get(name)
        if spec is None:
            print(f"[levers] unknown lever '{name}' — ignored "
                  f"(known: {', '.join(sorted(LEVERS))})")
            continue
        rev = spec["apply"](pewpew_mod, app, val)
        reverts.append(rev)

    def revert_all():
        for r in reversed(reverts):
            try:
                r()
            except Exception as e:
                print(f"[levers] revert failed: {e!r}")
    return revert_all


def describe(lever_values):
    if not lever_values:
        return "(defaults)"
    parts = []
    for name, val in lever_values.items():
        spec = LEVERS.get(name)
        parts.append(spec["describe"](val) if spec else f"{name}={val}")
    return ", ".join(parts)


def parse_levers_arg(arg_str):
    """Parse 'dmg=2.0,incm=0.5' into {'dmg': 2.0, 'incm': 0.5}."""
    out = {}
    if not arg_str:
        return out
    for chunk in arg_str.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        k = k.strip()
        v = v.strip()
        try:
            v_num = float(v)
            if v_num.is_integer() and "." not in v:
                v_num = int(v_num)
            out[k] = v_num
        except ValueError:
            out[k] = v
    return out
