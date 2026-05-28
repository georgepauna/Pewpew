"""Math-only check: player DPS at max upgrades vs wave total HP per level.
Computes the load factor (time-to-clear-wave / gap-between-waves) so we
can see where waves start overlapping under ideal play (best weapon held,
every shot lands)."""

# ---- Per-weapon stats at max ----
# Vulcan T5 / L20:  5 bullets / 0.055s, dmg = 100+10*(20-1) = 290 each
# Pulse  T5 / L20:  6 bullets / 0.10s,  dmg = 290
# Spread T5 / L20: 10 bullets / 0.14s,  dmg = 290
DPS_VULCAN = 5 * 290 / 0.055
DPS_PULSE  = 6 * 290 / 0.10
DPS_SPREAD = 10 * 290 / 0.14
# Missile T5 / L5: 5 missiles / 0.70s, 200 damage each, homing
DPS_MISSILE = 5 * 200 / 0.70


def main_dps_at_level(n):
    """Effective single-target main DPS at level n. Player swaps shoulders
    to match shield colour — wrong weapon = lower DPS, not zero."""
    shield_rate = 0.20 + 0.30 * (n - 1) / 99
    p_blue, p_red, p_yel = shield_rate/3, shield_rate/3, shield_rate/3
    p_naked = 1.0 - shield_rate
    return ((p_naked + p_yel) * DPS_VULCAN
            + p_blue * DPS_PULSE
            + p_red * DPS_SPREAD)


def ideal_dps(n):
    return main_dps_at_level(n) + DPS_MISSILE


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
    print(f"{'Lvl':>4} {'avgHP':>6} {'diff':>5} {'waveHP':>7} "
          f"{'/wave':>5} {'gap':>6} {'DPS':>6} {'TTK':>6} {'load':>5} verdict")
    print("-" * 80)
    for n in [1, 5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 99, 100]:
        a_hp = avg_enemy_hp_at(n)
        d    = difficulty_mul(n)
        w_hp = wave_total_hp(n)
        g    = gap_between_waves(n)
        dps  = ideal_dps(n)
        ttk  = time_to_clear_wave(n)
        lf   = load_factor(n)
        verdict = "OK" if lf < 1.0 else "OVERLAP"
        print(f"L{n:03d} {a_hp:>6.0f} {d:>5.2f} {w_hp:>7.0f} "
              f"{enemies_per_wave(n):>5} {g:>5.1f}s {dps:>6.0f} "
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
