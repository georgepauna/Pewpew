"""Sim-only knob overrides.

Each lever knows how to mutate pewpew module-level state for the duration
of a bot session and how to revert it afterwards. The bot session driver
applies all requested levers before running its profiles and reverts them
when finished — so the LIVE game (launched without --bot) is never
affected.

Once a tuning value is confirmed by simulation, the next step is to
promote it: edit pewpew.py with the new constant, re-snapshot, and from
then on the lever's "default" should become the new live value.

Lever schema:
  - default: live-game value (informational)
  - apply(pewpew_mod, value) -> revert callable
  - describe(value) -> short human-readable string

Add new levers here as we tune. CLI: `--levers=income_mul=0.5,…`
"""


def apply_income_mul(pewpew_mod, mul):
    """Scale every source of credits the player earns by `mul`.

    Touches both halves of the income economy:
      - enemy bounty (Enemy class CREDITS attribute) — paid on kill
      - money pickups dropped by enemies (Player.collect returns 50 by
        default; we wrap collect to scale that down)
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


def apply_damage_taken_mul(pewpew_mod, mul):
    """Scale every source of incoming damage to the player by `mul`.

    Wraps Player.take_damage — the single funnel that bullets, body
    contact (8 dmg), and mine AoE (6 dmg) all flow through. A minimum of
    1 is enforced for non-zero damage so mul<1 can't make hits free.
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


LEVERS = {
    "income_mul": {
        "default": 1.0,
        "apply": apply_income_mul,
        "describe": lambda v: f"income x{v}",
    },
    "damage_taken_mul": {
        "default": 1.0,
        "apply": apply_damage_taken_mul,
        "describe": lambda v: f"dmg-in x{v}",
    },
}


def apply_levers(pewpew_mod, lever_values):
    """Apply all requested levers; returns a single revert callable."""
    reverts = []
    for name, val in lever_values.items():
        spec = LEVERS.get(name)
        if spec is None:
            print(f"[levers] unknown lever '{name}' — ignored")
            continue
        rev = spec["apply"](pewpew_mod, val)
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
    """Parse 'income_mul=0.5,xyz=1.2' into {'income_mul': 0.5, 'xyz': 1.2}."""
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
