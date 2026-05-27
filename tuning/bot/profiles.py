"""Bot profiles. A profile is (SkillProfile, UpgradeProfile, side+ability prefs).

SkillProfile drives in-play behaviour — bullet read-ahead, dodge strength,
which main weapon it prefers in the absence of a shielded target, how
quickly + correctly it swaps when a coloured shield appears, when it pops
its ability.

UpgradeProfile drives shop choices — an ordered priority list of
purchases the session driver walks each shop visit.

Profiles are pure data — change a number, re-run the bot, see what shifts.

Six archetypes cover the realistic skill curve:
  scrub       — new player. Hand-eye is poor; misses dodge windows; never
                swaps weapons (just holds the trigger). Tests the game's
                accessibility floor.
  casual      — average player. Reads bullets a bit. Swaps weapons when
                obvious + given a beat to react. Spends on whatever looks
                shiny. Tests the mid-skill progression curve.
  focused     — knows the systems. Reliable swaps, decent threat-priority
                targeting, optimal-ish shop path. Tests the "designed-for"
                difficulty.
  speedrunner — risk-it for DPS. Min dodge, max engine + main, bombs as
                soon as ready. Tests if any level is *unrecoverable* from
                an aggressive play angle.
  tank        — turtle. Max shield priority, sits centre, fires only when
                aligned. Tests whether pure defence can clear the game.
  expert      — optimal play. Long bullet read, instant + correct swaps,
                ricochet-aware movement, ability timed to the situation.
                Tests the top of the curve — should walk through.
"""

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class SkillProfile:
    name: str
    # Dodge + threat reading
    lookahead_sec: float            # how far ahead bullets are tracked
    danger_radius_px: float         # bullet/enemy must be within to dodge
    repel_strength: float           # weight on threat avoidance
    dodge_dropout: float = 0.0      # per-frame chance to skip dodging entirely
    ricochet_aware: bool = False    # weight reflected bullets extra

    # Pickup chasing
    pickup_radius_px: float = 200.0
    pickup_weight: float = 0.6
    pickup_priority_main: float = 1.0
    pickup_priority_shield: float = 1.0
    pickup_priority_money: float = 1.0

    # Targeting + firing
    aim_weight: float = 1.0
    target_threat_weight: float = 0.5  # 0 = pure HP/dist, 1 = pure urgency
    fire_always: bool = True
    min_aim_align_px: float = 60.0     # only used when fire_always=False

    # Edge + engagement zone
    edge_repel_px: float = 35.0
    engagement_y_frac: float = 0.62    # y position the bot likes to occupy

    # Shield + weapon swap
    prefers_main: str = "vulcan"             # which main to hold by default
    weapon_swap_reaction_sec: float = 0.0    # delay before noticing a swap is needed
    weapon_swap_skill: float = 1.0           # 0..1 chance the swap is correct

    # Ability
    bomb_threshold: float = 0.30             # shield ratio below = panic-fire
    ability_strategy: str = "panic"          # panic / crowd / boss / always_ready


# ---------------------------------------------------------------------------
# Six skill archetypes — span the realistic player skill spectrum.
# ---------------------------------------------------------------------------

SKILL_SCRUB = SkillProfile(
    name="scrub",
    lookahead_sec=0.15,
    danger_radius_px=28,
    repel_strength=0.45,
    dodge_dropout=0.18,
    pickup_radius_px=0,
    pickup_weight=0.0,
    aim_weight=0.25,
    target_threat_weight=0.2,
    edge_repel_px=18,
    prefers_main="vulcan",
    weapon_swap_reaction_sec=99.0,   # effectively never swaps
    weapon_swap_skill=0.5,           # and if forced, often wrong
    bomb_threshold=0.10,
    ability_strategy="panic",
)

SKILL_CASUAL = SkillProfile(
    name="casual",
    lookahead_sec=0.35,
    danger_radius_px=42,
    repel_strength=0.75,
    dodge_dropout=0.06,
    pickup_radius_px=160,
    pickup_weight=0.4,
    pickup_priority_shield=1.6,
    aim_weight=0.65,
    target_threat_weight=0.4,
    edge_repel_px=28,
    prefers_main="vulcan",
    weapon_swap_reaction_sec=0.6,    # half-second to notice a shield
    weapon_swap_skill=0.85,          # mostly right
    bomb_threshold=0.30,
    ability_strategy="panic",
)

SKILL_FOCUSED = SkillProfile(
    name="focused",
    lookahead_sec=0.55,
    danger_radius_px=52,
    repel_strength=0.95,
    dodge_dropout=0.0,
    pickup_radius_px=220,
    pickup_weight=0.6,
    pickup_priority_main=1.3,
    pickup_priority_shield=1.4,
    aim_weight=0.95,
    target_threat_weight=0.6,
    edge_repel_px=35,
    prefers_main="vulcan",
    weapon_swap_reaction_sec=0.20,
    weapon_swap_skill=0.95,
    bomb_threshold=0.35,
    ability_strategy="crowd",
)

SKILL_SPEEDRUNNER = SkillProfile(
    name="speedrunner",
    lookahead_sec=0.40,           # less than focused — they take hits to push DPS
    danger_radius_px=40,
    repel_strength=0.65,          # softer dodge — keeps aim instead
    dodge_dropout=0.0,
    pickup_radius_px=280,
    pickup_weight=0.85,
    pickup_priority_main=1.6,
    pickup_priority_shield=0.6,   # actively skips shield pickups
    aim_weight=1.15,
    target_threat_weight=0.7,
    edge_repel_px=25,
    engagement_y_frac=0.55,       # closer to enemies = less travel for bullets
    prefers_main="vulcan",
    weapon_swap_reaction_sec=0.20,
    weapon_swap_skill=0.90,
    bomb_threshold=0.20,
    ability_strategy="always_ready",
)

SKILL_TANK = SkillProfile(
    name="tank",
    lookahead_sec=0.50,
    danger_radius_px=48,
    repel_strength=0.85,
    dodge_dropout=0.0,
    pickup_radius_px=100,         # rarely chases — stays put
    pickup_weight=0.3,
    pickup_priority_shield=2.0,
    aim_weight=0.40,              # care less about aim
    target_threat_weight=0.8,     # care a lot about who's about to hit me
    edge_repel_px=45,
    engagement_y_frac=0.78,       # sits low, far from spawn
    fire_always=False,            # only fires when aligned (saves clutter)
    min_aim_align_px=55.0,
    prefers_main="spread",        # spread = wide cone, less alignment needed
    weapon_swap_reaction_sec=0.40,
    weapon_swap_skill=0.85,
    bomb_threshold=0.45,
    ability_strategy="panic",
)

SKILL_EXPERT = SkillProfile(
    name="expert",
    lookahead_sec=0.70,
    danger_radius_px=58,
    repel_strength=1.0,
    dodge_dropout=0.0,
    ricochet_aware=True,
    pickup_radius_px=260,
    pickup_weight=0.7,
    pickup_priority_main=1.5,
    pickup_priority_shield=1.5,
    aim_weight=1.10,
    target_threat_weight=0.75,
    edge_repel_px=40,
    engagement_y_frac=0.60,
    prefers_main="vulcan",
    weapon_swap_reaction_sec=0.05,   # near-instant
    weapon_swap_skill=0.99,          # ~always correct
    bomb_threshold=0.40,
    ability_strategy="boss",
)


# ---------------------------------------------------------------------------
# Upgrade priorities. Each profile gets its OWN priority list tailored to
# its play-style — speedrunner buys engine + damage first, tank front-loads
# shield, etc. Snapshot 06 made all 3 mains always-owned, so "main_upgrade"
# now means "level up whichever main you target". Pickups upgrade whichever
# main the bot is currently *holding* (its main_type), so we steer the
# default-main pickups by upgrading prefers_main here.
# ---------------------------------------------------------------------------

@dataclass
class UpgradeProfile:
    name: str
    priority: List[Tuple[str, str]]
    impatient: bool
    keep_bombs: int


def _main(weapon, n):
    return [("main_upgrade", weapon)] * n

def _side(weapon, n):
    return [("side_upgrade", weapon)] * n


# Optimal: DPS-first. Boss 1 (L010) walls bots that can't push ~4k DPS,
# so vulcan T2 (V5..V8) is the early target. Shield is cheap insurance
# AFTER the damage curve is up. Round-robins all three mains so shielded
# enemies don't ricochet — a strong vulcan with weak pulse/spread leaves
# the bot defenceless against blue/red shields in late game.
PRIO_OPTIMAL = (
    _main("vulcan", 4)                  # V1->V5: T1 subs + T1->T2 jump (2 bullets!)
    + [("shield", "")]                  # shield L2 (cheap)
    + _main("vulcan", 4)                # V5->V9: T2 subs + T2->T3 jump (3 bullets)
    + _main("pulse", 4)                 # P1->P5: pulse to T2 for blue shields
    + [("shield", "")]                  # shield L3
    + _main("spread", 4)                # S1->S5: spread to T2 for red shields
    + [("engine", "")]                  # engine L2
    + _main("vulcan", 4)                # V9->V13: T3 subs + T3->T4 jump (4 bullets)
    + [("shield", "")]                  # shield L4
    + _main("pulse", 4)                 # P5->P9: pulse to T3
    + _main("spread", 4)                # S5->S9: spread to T3
    + [("side_first", "missile")]       # passive AoE backup
    + _side("missile", 4)               # missile to T2
    + _main("vulcan", 4)                # V13->V17: T4 subs + T4->T5 jump
    + [("shield", "")]                  # shield L5
    + [("engine", "")]                  # engine L3
    + _main("pulse", 4)                 # P9->P13: pulse to T4
    + _main("spread", 4)                # S9->S13: spread to T4
    + _main("vulcan", 3)                # V17->V20 final subs (max damage)
    + _side("missile", 4)               # missile to T3
)

# Speedrunner: vulcan first to clear waves faster (= more $$ per minute).
# Engine + bomb topping happen between vulcan tiers. Shield buys are
# minimal — they'll out-damage incoming pressure.
PRIO_SPEED = (
    _main("vulcan", 4)                   # V1->V5 jump (2 bullets) ASAP
    + [("engine", "")]                   # eng L2 — close gaps fast
    + _main("vulcan", 4)                 # V5->V9 jump (3 bullets)
    + [("shield", "")]                   # minimum survivability bump
    + _main("vulcan", 4)                 # V9->V13 jump (4 bullets)
    + _main("pulse", 4)                  # P->T2 for blue shields
    + [("engine", "")]                   # eng L3
    + _main("vulcan", 4)                 # V13->V17 jump (5 bullets)
    + _main("spread", 4)                 # S->T2 for red shields
    + [("shield", "")]
    + _main("vulcan", 3)                 # V17->V20 final
    + _main("pulse", 4)                  # P->T3
    + _main("spread", 4)                 # S->T3
)

# Tank: shield-max first, drone for passive DPS, then mains. Spread is
# the preferred fire (wide cone = less alignment) but vulcan + pulse get
# their tier-2 jump too so shielded enemies aren't a wall.
PRIO_TANK = (
    [("shield", "")]
    + [("shield", "")]                 # shield L3 fast
    + [("side_first", "drone")]
    + _main("spread", 4)               # spread T2 (wider cone)
    + [("shield", "")]                 # shield L4
    + _side("drone", 4)                # drone T2 (2 shots)
    + _main("vulcan", 4)               # vulcan T2 — for yellow shields + bosses
    + [("shield", "")]                 # shield L5 (cap)
    + _main("pulse", 4)                # pulse T2 — for blue shields
    + _side("drone", 4)
    + _main("spread", 4)
    + [("engine", "")]
    + _main("vulcan", 4)
)

# Average: distracted — engine + side early, shield late, doesn't bank.
PRIO_AVG = (
    [("engine", "")]
    + _main("vulcan", 1)
    + [("shield", "")]
    + [("side_first", "missile")]
    + _main("vulcan", 3)
    + _side("missile", 1)
    + [("shield", "")]
    + [("engine", "")]
    + _main("vulcan", 4)
    + _side("missile", 3)
    + [("shield", "")]
    + _main("pulse", 4)
    + _side("missile", 3)
    + [("shield", "")]
    + _main("spread", 4)
    + _main("vulcan", 4)
)

# Scrub: barely upgrades — buys what's at the top of the list and runs out
# of credits fast because they don't bank.
PRIO_SCRUB = (
    [("engine", "")]
    + _main("vulcan", 1)
    + [("side_first", "missile")]
    + [("engine", "")]
    + _main("pulse", 1)
    + _main("spread", 1)
    + [("shield", "")]
    + _main("vulcan", 1)
    + _side("missile", 1)
    + _main("vulcan", 1)
    + [("shield", "")]
    + _main("pulse", 1)
)


UP_OPTIMAL = UpgradeProfile("optimal",  PRIO_OPTIMAL, impatient=False, keep_bombs=3)
UP_SPEED   = UpgradeProfile("speed",    PRIO_SPEED,   impatient=True,  keep_bombs=4)
UP_TANK    = UpgradeProfile("tank",     PRIO_TANK,    impatient=False, keep_bombs=2)
UP_AVG     = UpgradeProfile("avg",      PRIO_AVG,     impatient=True,  keep_bombs=2)
UP_SCRUB   = UpgradeProfile("scrub",    PRIO_SCRUB,   impatient=True,  keep_bombs=1)


# ---------------------------------------------------------------------------
# Compose the six profiles (one per archetype). Each profile also names a
# preferred side weapon + ability — the session driver applies these at
# session start so the bot's loadout matches its play style.
# ---------------------------------------------------------------------------

@dataclass
class BotProfile:
    name: str
    skill: SkillProfile
    upgrade: UpgradeProfile
    ability: str = "screen_clear"      # screen_clear / shield_burst / mega_laser
    preferred_side: str = "missile"    # which side to default when buying


def _mk(name, skill, upgrade, ability="screen_clear", side="missile"):
    return BotProfile(name, skill, upgrade, ability=ability, preferred_side=side)


PROFILES = {
    "scrub":       _mk("scrub",       SKILL_SCRUB,       UP_SCRUB,   ability="shield_burst", side="missile"),
    "casual":      _mk("casual",      SKILL_CASUAL,      UP_AVG,     ability="screen_clear", side="missile"),
    "focused":     _mk("focused",     SKILL_FOCUSED,     UP_OPTIMAL, ability="screen_clear", side="missile"),
    "speedrunner": _mk("speedrunner", SKILL_SPEEDRUNNER, UP_SPEED,   ability="mega_laser",   side="missile"),
    "tank":        _mk("tank",        SKILL_TANK,        UP_TANK,    ability="shield_burst", side="drone"),
    "expert":      _mk("expert",      SKILL_EXPERT,      UP_OPTIMAL, ability="mega_laser",   side="missile"),
}

ALL_PROFILE_NAMES = list(PROFILES.keys())
