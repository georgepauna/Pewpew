"""Math-only check: player DPS vs wave total HP per level, with TIER
UNLOCKS respected. The player can't have V20 at L1 — tier 3+ for each
main weapon is gated behind specific bosses, and side weapons cascade
when all 3 mains hit that tier. "Ideal play" here means: the player
has cleared every boss available so far AND has credits to buy every
upgrade unlocked AND lands every shot.
"""

# ---- Per-tier bullet count + fire rate ----
# snapshot 07 carry-forwards.
PULSE_TIERS  = {1: (1, 0.18), 2: (2, 0.16), 3: (3, 0.14), 4: (4, 0.12), 5: (6, 0.10)}
SPREAD_TIERS = {1: (3, 0.22), 2: (5, 0.20), 3: (7, 0.18), 4: (8, 0.16), 5: (10, 0.14)}
VULCAN_TIERS = {1: (1, 0.10), 2: (2, 0.085), 3: (3, 0.075), 4: (4, 0.065), 5: (5, 0.055)}
# Missile tier sets volley + fire rate. Damage flat 200 per missile,
# drone flat 100 per bullet — pick missile here (the bot+player default).
MISSILE_TIERS = {1: (1, 1.6), 2: (2, 1.3), 3: (3, 1.0), 4: (4, 0.85), 5: (5, 0.7)}


def max_level_for_tier(tier):
    """Max sub-level inside a tier (mains have 4 sub-levels per tier)."""
    return min(20, tier * 4)


def dmg_per_bullet(level):
    """Main weapon per-bullet damage curve. 100 + 10*(level-1) → L1=100,
    L20=290."""
    return 100 + 10 * (level - 1)


def main_dps_at_tier(tier, table):
    """DPS for a main weapon assumed maxed within `tier`: tier*4 levels."""
    lvl = max_level_for_tier(tier)
    bullets, fire_rate = table[tier]
    return bullets * dmg_per_bullet(lvl) / fire_rate


def missile_dps_at_tier(tier):
    bullets, fire_rate = MISSILE_TIERS[tier]
    return bullets * 200 / fire_rate


# ---- Tier-unlock schedule per main weapon ----
# Default unlocked tier = 2 for everyone.
# Bosses unlock tiers 3/4/5 per main on a fixed schedule:
#   pulse:   T3@L10, T4@L40, T5@L70
#   spread:  T3@L20, T4@L50, T5@L80
#   vulcan:  T3@L30, T4@L60, T5@L90
# Side / shield / engine T(N) cascades when ALL 3 mains have T(N) —
# i.e. after the third boss in each round-robin (L30, L60, L90).
def tier_at(n, schedule):
    """schedule is the list of (unlock_level, tier) pairs in order."""
    tier = 2
    for unlock_lvl, t in schedule:
        if n > unlock_lvl:
            tier = t
    return tier

PULSE_SCHED   = [(10, 3), (40, 4), (70, 5)]
SPREAD_SCHED  = [(20, 3), (50, 4), (80, 5)]
VULCAN_SCHED  = [(30, 3), (60, 4), (90, 5)]
MISSILE_SCHED = [(30, 3), (60, 4), (90, 5)]   # cascades with vulcan


def pulse_max_lvl(n):   return max_level_for_tier(tier_at(n, PULSE_SCHED))
def spread_max_lvl(n):  return max_level_for_tier(tier_at(n, SPREAD_SCHED))
def vulcan_max_lvl(n):  return max_level_for_tier(tier_at(n, VULCAN_SCHED))
def missile_max_tier(n): return tier_at(n, MISSILE_SCHED)


def main_dps_at_level(n):
    """Single-target main DPS, weighted by shield colour distribution.
    Player swaps shoulders so the matching weapon hits during shielded
    fights — wrong-coloured shields don't reduce DPS to zero in this
    model because we assume the player held the right weapon (ideal).
    """
    p_pulse  = main_dps_at_tier(tier_at(n, PULSE_SCHED), PULSE_TIERS)
    p_spread = main_dps_at_tier(tier_at(n, SPREAD_SCHED), SPREAD_TIERS)
    p_vulcan = main_dps_at_tier(tier_at(n, VULCAN_SCHED), VULCAN_TIERS)
    shield_rate = 0.20 + 0.30 * (n - 1) / 99
    p_blue = p_red = p_yel = shield_rate / 3
    p_naked = 1.0 - shield_rate
    # naked + yellow shield → vulcan; blue → pulse; red → spread
    return ((p_naked + p_yel) * p_vulcan
            + p_blue * p_pulse
            + p_red * p_spread)


def ideal_dps(n):
    return main_dps_at_level(n) + missile_dps_at_tier(missile_max_tier(n))


HP = {"scout": 200, "gunner": 600, "weaver": 400, "kamikaze": 400,
      "turret": 1000, "bomber": 1600}


def pool_at(n):
    p = ["scout"]
    if n >= 3:  p.append("gunner")
    if n >= 7:  p.append("weaver")
    if n >= 12: p.append("kamikaze")
    if n >= 18: p.append("turret")
    if n >= 25: p.append("bomber")
    if n >= 30: p += ["gunner", "weaver"]
    if n >= 50: p += ["kamikaze", "bomber"]
    if n >= 70: p += ["turret", "bomber"]
    return p


def avg_enemy_hp_at(n):
    pool = pool_at(n)
    return sum(HP[k] for k in pool) / len(pool)


def wave_count(n):           return 5 + n // 8
def enemies_per_wave(n):     return 3 + n // 10
def duration(n):             return min(45 + n // 2, 90)
def gap_between_waves(n):    return max(1.0, (duration(n) - 6) / max(1, wave_count(n) - 1))
def difficulty_mul(n):       return 1.0 + (n - 1) * (3.0 / 99.0)
def wave_total_hp(n):
    return enemies_per_wave(n) * avg_enemy_hp_at(n) * difficulty_mul(n)
def time_to_clear_wave(n):
    return wave_total_hp(n) / ideal_dps(n)
def load_factor(n):
    return time_to_clear_wave(n) / gap_between_waves(n)


def main():
    print(f"{'Lvl':>4} {'P/S/V':>7} {'mis':>4} {'diff':>5} {'waveHP':>7} "
          f"{'/w':>3} {'gap':>5} {'DPS':>6} {'TTK':>6} {'load':>5} verdict")
    print("-" * 80)
    for n in [1, 5, 10, 11, 20, 21, 25, 30, 31, 40, 41, 50, 51,
              60, 61, 70, 71, 80, 81, 90, 91, 100]:
        d    = difficulty_mul(n)
        w_hp = wave_total_hp(n)
        g    = gap_between_waves(n)
        dps  = ideal_dps(n)
        ttk  = time_to_clear_wave(n)
        lf   = load_factor(n)
        verdict = "OK" if lf < 1.0 else "OVERLAP"
        print(f"L{n:03d} P{pulse_max_lvl(n):2d}/S{spread_max_lvl(n):2d}/V{vulcan_max_lvl(n):2d}"
              f" m{missile_max_tier(n):>1d} {d:>5.2f} {w_hp:>7.0f} "
              f"{enemies_per_wave(n):>3} {g:>4.1f}s {dps:>6.0f} "
              f"{ttk:>5.2f}s {lf:>5.2f}  {verdict}")

    print()
    print("Cumulative ideal-play assessment:")
    first_overlap = None
    for n in range(1, 101):
        if load_factor(n) >= 1.0:
            first_overlap = n
            break
    if first_overlap is None:
        print("  Player keeps up at EVERY level under ideal play.")
        print("  Ideal-case chance of clearing all 100 levels: 100%.")
    else:
        print(f"  First level where waves overlap (load >= 1): L{first_overlap:03d}")
        print(f"  Ideal-case chance of clearing L001..L{first_overlap-1:03d}: 100%")
        print(f"  Ideal-case chance of clearing L{first_overlap:03d}+: snowballs")

    loads = sorted(((n, load_factor(n)) for n in range(1, 101)),
                   key=lambda x: -x[1])
    print("\n  Worst 5 load factors (most overlap risk):")
    for n, lf in loads[:5]:
        print(f"    L{n:03d}: load={lf:.2f} "
              f"(clear={time_to_clear_wave(n):.1f}s in {gap_between_waves(n):.1f}s gap)")


if __name__ == "__main__":
    main()
