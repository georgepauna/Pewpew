"""Bot profiles. A profile is (SkillProfile, UpgradeProfile).

SkillProfile drives in-play behavior — how far ahead it reads bullets, how
hard it dodges, whether it chases pickups, when it fires the bomb.

UpgradeProfile drives shop choices — an ordered priority list of purchases.
The session driver walks the list and buys each item when affordable.
Repeated entries mean "do this upgrade again" (e.g. four "shield" entries =
shield level 2,3,4,5).

Profiles are pure data — change a number, re-run the bot, see what shifts.
"""

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class SkillProfile:
    name: str
    lookahead_sec: float          # how far ahead the bot reads enemy bullets
    danger_radius_px: float       # bullet/enemy must come within this to dodge
    repel_strength: float         # weight on threat avoidance (0..1+)
    pickup_radius_px: float       # 0 disables chasing pickups
    pickup_weight: float
    aim_weight: float             # 0..1, alignment to high-HP enemy
    edge_repel_px: float
    bomb_threshold: float         # ratio shield_hp/shield_max; below = pop bomb
    bomb_proactive: bool          # use ability offensively too (when crowded)
    dodge_dropout: float = 0.0    # per-frame chance to skip dodging entirely
                                  # — simulates slow reactions / inattention
    fire_always: bool = True      # bad players still hold trigger — skill is positional


SKILL_GOOD = SkillProfile(
    name="good",
    lookahead_sec=0.60,
    danger_radius_px=55,
    repel_strength=1.0,
    pickup_radius_px=240,
    pickup_weight=0.70,
    aim_weight=1.0,
    edge_repel_px=40,
    bomb_threshold=0.45,
    bomb_proactive=True,
    dodge_dropout=0.0,
)

SKILL_MED = SkillProfile(
    name="med",
    lookahead_sec=0.35,
    danger_radius_px=40,
    repel_strength=0.70,
    pickup_radius_px=140,
    pickup_weight=0.35,
    aim_weight=0.60,
    edge_repel_px=30,
    bomb_threshold=0.25,
    bomb_proactive=False,
    dodge_dropout=0.06,
)

SKILL_BAD = SkillProfile(
    name="bad",
    lookahead_sec=0.15,
    danger_radius_px=25,
    repel_strength=0.40,
    pickup_radius_px=0,            # ignores pickups entirely
    pickup_weight=0.0,
    aim_weight=0.25,
    edge_repel_px=20,
    bomb_threshold=0.10,
    bomb_proactive=False,
    dodge_dropout=0.20,
)


# Each entry is (kind, target). kind is one of:
#   "shield"          — bump shield level by 1
#   "engine"          — bump engine level by 1
#   "main_upgrade"    — buy the named main weapon (first or +1 level)
#   "side_first"      — first purchase of the named side weapon
#   "side_upgrade"    — +1 level on an owned side weapon
@dataclass
class UpgradeProfile:
    name: str
    priority: List[Tuple[str, str]]
    impatient: bool         # if True, skip down the list when top is unaffordable
    keep_bombs: int


# With snapshot 03 (20-level main weapons, 12-level side weapons), each
# weapon has 19/11 upgrade purchases on top of L1. Tier-jumps (every 4
# sub-levels) drive the bullet-count + fire-rate steps; sub-levels add
# damage only. Optimal play prioritises survivability (shield) early, then
# rides each weapon tier-jump as it becomes affordable.

def _main_steps(weapon, count):
    return [("main_upgrade", weapon)] * count

def _side_steps(weapon, count):
    return [("side_upgrade", weapon)] * count


# Optimal: shield first, then pulse tier-jump streaks. Each "block" buys
# a tier-jump (1 expensive step) + the 3 sub-levels that precede the next
# tier-jump. Engine + side weapons slot in between damage tiers.
PRIORITY_OPTIMAL: List[Tuple[str, str]] = (
    [("shield", "")]            # shield L2 (cheap, biggest survivability win)
    + _main_steps("pulse", 4)   # pulse L2..L5  (3 subs + T1->T2 jump → 2 bullets)
    + [("shield", "")]          # shield L3
    + [("engine", "")]          # engine L2
    + _main_steps("pulse", 4)   # pulse L6..L9  (subs + T2->T3 jump → 3 bullets)
    + [("shield", "")]          # shield L4
    + _main_steps("pulse", 4)   # pulse L10..L13 (subs + T3->T4 jump → 4 bullets)
    + [("shield", "")]          # shield L5
    + [("engine", "")]          # engine L3
    + _main_steps("pulse", 4)   # pulse L14..L17 (subs + T4->T5 jump → 6 bullets)
    + [("side_first", "missile")]
    + _side_steps("missile", 4) # missile L2..L5 (subs + T1->T2 → 2 missiles)
    + _main_steps("pulse", 3)   # pulse L18..L20 (final T5 subs, max damage)
    + _side_steps("missile", 4) # missile L6..L9 (subs + T2->T3 → 3 missiles)
    + _side_steps("missile", 3) # missile L10..L12 (final T3 subs)
)


# Average: distracted by shiny things — buys engine + side weapon early,
# doesn't bank credits for the big tier-jumps. Interleaves shield and
# main-weapon sub-levels with side / engine spends.
PRIORITY_AVG: List[Tuple[str, str]] = (
    [("engine", "")]            # engine L2 — flashy first
    + _main_steps("pulse", 1)   # pulse L2 (cheap sub-level, feels good)
    + [("shield", "")]          # shield L2
    + [("side_first", "missile")]
    + _main_steps("pulse", 3)   # pulse L3..L5 (subs + tier jump)
    + _side_steps("missile", 1) # missile L2 (sub)
    + [("shield", "")]          # shield L3
    + [("engine", "")]          # engine L3
    + _main_steps("pulse", 4)   # pulse L6..L9 (sub-grind + T2->T3 jump)
    + _side_steps("missile", 3) # missile L3..L5 (subs + T1->T2 → 2 missiles)
    + [("shield", "")]          # shield L4
    + _main_steps("pulse", 4)   # pulse L10..L13 (subs + T3->T4 jump)
    + _side_steps("missile", 3) # missile L6..L8 (T2 subs)
    + [("shield", "")]          # shield L5
    + _main_steps("pulse", 4)   # pulse L14..L17 (subs + T4->T5 jump)
    + _side_steps("missile", 4) # missile L9..L12 (T2->T3 jump + T3 subs)
    + _main_steps("pulse", 3)   # pulse L18..L20 (final T5 subs)
)

UPGRADE_OPTIMAL = UpgradeProfile(
    name="optimal", priority=PRIORITY_OPTIMAL, impatient=False, keep_bombs=3,
)
UPGRADE_AVG = UpgradeProfile(
    name="avg", priority=PRIORITY_AVG, impatient=True, keep_bombs=2,
)


@dataclass
class BotProfile:
    name: str
    skill: SkillProfile
    upgrade: UpgradeProfile


def _mk(name, skill, upgrade):
    return BotProfile(name, skill, upgrade)


PROFILES = {
    "good_optimal": _mk("good_optimal", SKILL_GOOD, UPGRADE_OPTIMAL),
    "good_avg":     _mk("good_avg",     SKILL_GOOD, UPGRADE_AVG),
    "med_optimal":  _mk("med_optimal",  SKILL_MED,  UPGRADE_OPTIMAL),
    "med_avg":      _mk("med_avg",      SKILL_MED,  UPGRADE_AVG),
    "bad_optimal":  _mk("bad_optimal",  SKILL_BAD,  UPGRADE_OPTIMAL),
    "bad_avg":      _mk("bad_avg",      SKILL_BAD,  UPGRADE_AVG),
}

ALL_PROFILE_NAMES = list(PROFILES.keys())
