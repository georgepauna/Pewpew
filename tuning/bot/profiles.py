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


PRIORITY_OPTIMAL: List[Tuple[str, str]] = [
    ("shield", ""),                  # L2 (cheap, biggest survivability win)
    ("main_upgrade", "pulse"),       # L2 (cheap, doubles DPS)
    ("main_upgrade", "pulse"),       # L3
    ("shield", ""),                  # L3
    ("engine", ""),                  # L2 (movement -> better dodge)
    ("main_upgrade", "pulse"),       # L4
    ("shield", ""),                  # L4
    ("main_upgrade", "pulse"),       # L5 (+1 dmg per bullet)
    ("engine", ""),                  # L3
    ("shield", ""),                  # L5
    ("side_first", "missile"),
    ("side_upgrade", "missile"),     # L2
    ("side_upgrade", "missile"),     # L3
]

PRIORITY_AVG: List[Tuple[str, str]] = [
    ("engine", ""),                  # L2 — flashy first
    ("main_upgrade", "pulse"),       # L2
    ("shield", ""),                  # L2
    ("side_first", "missile"),       # spends 800 on a side weapon early
    ("main_upgrade", "pulse"),       # L3
    ("side_upgrade", "missile"),     # L2
    ("shield", ""),                  # L3
    ("engine", ""),                  # L3
    ("main_upgrade", "pulse"),       # L4
    ("side_upgrade", "missile"),     # L3
    ("shield", ""),                  # L4
    ("main_upgrade", "pulse"),       # L5
    ("shield", ""),                  # L5
]

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
