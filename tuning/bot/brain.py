"""In-play bot AI.

Each frame, reads PlayState and fills a Controls instance. The brain is
pure (no side effects on game state) so a recorded input stream replays
identically given the same seed.

Movement uses **position sampling** rather than a per-threat force sum.
We score the player's current position plus 16 candidate offsets (8-way
at one radius + 8-way at half radius) and move toward the best. Scoring:

  - Hostile bullet threat (closest-approach within `lookahead_sec`),
    including ricocheted player bullets — they're friendly=False now.
  - Enemy contact threat with AoE for mines + a margin for shielded
    enemies (a shielded contact still hurts you).
  - Pickup attraction, weighted higher when the kind matches a current
    need (shield low / main undertiered).
  - Aim alignment to the chosen target — the right value of "aim" depends
    on the weapon: spread fans wide, vulcan needs tight x-alignment.
  - Edge avoidance + engagement-zone bias.
  - Boss-fight horizontal weave when a boss is active.

**Shield-aware weapon swap** (snapshot 06): the brain inspects the chosen
target's `shield_color`. blue → hold L1 (Pulse), red → hold R1 (Spread),
yellow / no shield → release both (Vulcan). Skill knobs gate this: the
bot's reaction time before swapping, and a per-decision chance to pick
the wrong shoulder, are both per-profile fields. Less-skilled bots will
ricochet bullets back at themselves — same penalty a real player pays
for forgetting to swap.

**Ability strategy** (`skill.ability_strategy`):
  - "panic"        — fire only when shield_hp ratio dips below threshold
  - "crowd"        — panic + fire when N+ enemies on screen
  - "boss"         — panic + dump on boss as soon as ability is ready
  - "always_ready" — fire as soon as cooldown is up (skill-floor: just
                     press the button)

Stochastic dropout (`skill.dodge_dropout`) is a per-frame chance to skip
the dodge calculation entirely — bad bots occasionally just... don't
dodge. Uses a dedicated RNG so the game's seeded random state is
unaffected (and replays don't desync).
"""

import math
import random


_SHIELD_COLOR_TO_MAIN = {
    "blue":   "pulse",
    "red":    "spread",
    "yellow": "vulcan",
}


class PlayBot:
    def __init__(self, skill, play_w, play_h, rng_seed=None):
        self.skill = skill
        self.play_w = play_w
        self.play_h = play_h
        # Dedicated RNG so dodge dropouts / mis-swaps don't perturb the
        # game's random stream — keeps recorded replays deterministic.
        self._rng = random.Random(rng_seed if rng_seed is not None else 0xBADBAB)
        self._boss_phase = 0.0   # boss weave oscillator

        # Sample 8-way at full radius + 8-way at half radius + stay. The
        # second ring lets the bot creep into tight gaps between bullets
        # without committing to a full-radius dash.
        self._sample_offsets = [(0.0, 0.0)]
        for r in (45.0, 22.0):
            for ang_deg in (0, 45, 90, 135, 180, 225, 270, 315):
                ang = math.radians(ang_deg)
                self._sample_offsets.append(
                    (math.cos(ang) * r, math.sin(ang) * r))

        # Weapon-swap state. The bot has a *current* held weapon
        # (_desired_main) and a *pending* swap candidate. When the ideal
        # weapon for the current target differs from what's held, we
        # accumulate _pending_wait until it crosses the profile's reaction
        # time, then commit (with a skill roll). Scrub-tier reaction times
        # (e.g. 99 s) effectively mean "never swap" because the wait never
        # exceeds the level length.
        self._desired_main = getattr(skill, "prefers_main", "vulcan")
        self._pending_main = None
        self._pending_wait = 0.0

    # ------------------------------------------------------------------
    # Public per-frame step
    # ------------------------------------------------------------------

    def step(self, play_state, controls):
        controls.reset_pulses()
        # Re-zero the held bools too — reset_pulses() only clears the
        # one-shots, not the shoulder holds the bot drives every frame.
        controls.l1_held = False
        controls.r1_held = False
        controls.left = controls.right = controls.up = controls.down = False
        controls.fire = False

        player = play_state.player
        if not player.alive or getattr(player, "cinematic", False):
            return

        dt = 1.0 / 60.0

        # ---- Pick a target ----
        # During a boss fight, force-pick the boss so we don't waste DPS
        # on minor enemies that wander through. The boss has hp > 40k so
        # _pick_target would prefer it anyway, but only when it's already
        # on screen and inside the y-window.
        is_boss = bool(getattr(play_state, "is_boss_fight", False))
        target = None
        if is_boss:
            # Find the boss by hp (cheap; there's only one).
            for e in play_state.enemies:
                if (getattr(e, "alive", False)
                        and getattr(e, "hp", 0) > 30000):
                    target = e
                    break
        if target is None:
            target = self._pick_target(play_state, player)

        # ---- Decide which main to hold this frame ----
        self._update_weapon_choice(target, dt)
        if self._desired_main == "pulse":
            controls.l1_held = True
        elif self._desired_main == "spread":
            controls.r1_held = True
        # vulcan = neither held

        # ---- Boss weave only overrides aim when the boss has no shield ----
        # When the boss IS shielded, the player needs to align with the
        # boss's actual x to land matching-weapon hits and crack the bubble
        # — weaving away wastes the limited 5s window before the next
        # respawn. When the boss is naked, weaving helps dodge bullets at
        # the cost of some DPS.
        boss_x = None
        if is_boss and target is not None and not getattr(target, "shield_color", None):
            self._boss_phase += 0.045
            boss_x = self.play_w * 0.5 + math.sin(self._boss_phase) * (self.play_w * 0.30)

        # ---- Movement ----
        skip_dodge = (self.skill.dodge_dropout > 0.0
                      and self._rng.random() < self.skill.dodge_dropout)
        mvx, mvy = self._decide_movement(play_state, target, boss_x, skip_dodge)

        DZ = 0.20
        controls.left  = mvx < -DZ
        controls.right = mvx >  DZ
        controls.up    = mvy < -DZ
        controls.down  = mvy >  DZ

        # ---- Fire trigger ----
        if self.skill.fire_always:
            controls.fire = True
        else:
            # Aim-gated fire: only shoot when roughly aligned with a target.
            # Helps "tank"-style profiles that prefer conserving bullet
            # collisions (and the resulting screen clutter).
            if target is not None:
                dx = abs(float(target.x) - float(player.x))
                aim_band = self.skill.min_aim_align_px
                controls.fire = dx < aim_band

        # ---- Ability ----
        self._maybe_use_ability(play_state, controls)

    # ------------------------------------------------------------------
    # Weapon-swap decision
    # ------------------------------------------------------------------

    def _update_weapon_choice(self, target, dt):
        """Pick which main to hold this frame (sets self._desired_main).

        See-then-react model: when the ideal weapon disagrees with what's
        currently held, accumulate _pending_wait until it crosses the
        profile's weapon_swap_reaction_sec; then commit the swap (with a
        skill roll). If the ideal flips back to what we already hold, the
        pending swap is cancelled. Profiles with very high reaction (e.g.
        99 s for "scrub") effectively never swap — they ride prefers_main
        for the whole run."""
        ideal = self._ideal_main_for_target(target)
        if ideal == self._desired_main:
            self._pending_main = None
            self._pending_wait = 0.0
            return
        if ideal != self._pending_main:
            # New trigger — restart the reaction timer.
            self._pending_main = ideal
            self._pending_wait = 0.0
            return
        # Same trigger as last frame — accumulate wait.
        self._pending_wait += dt
        reaction = float(getattr(self.skill, "weapon_swap_reaction_sec", 0.0))
        if self._pending_wait < reaction:
            return
        # Commit. Skill < 1.0 lets us pick the wrong shoulder by mistake;
        # the wait resets so we'll get another chance after reaction.
        skill = float(getattr(self.skill, "weapon_swap_skill", 1.0))
        if skill < 1.0 and self._rng.random() > skill:
            wrong = [m for m in ("pulse", "spread", "vulcan") if m != ideal]
            self._desired_main = self._rng.choice(wrong)
        else:
            self._desired_main = ideal
        self._pending_wait = 0.0

    def _ideal_main_for_target(self, target):
        """blue→pulse, red→spread, yellow→vulcan, no shield→prefers_main."""
        if target is None:
            return getattr(self.skill, "prefers_main", "vulcan")
        sc = getattr(target, "shield_color", None)
        if sc and sc in _SHIELD_COLOR_TO_MAIN:
            return _SHIELD_COLOR_TO_MAIN[sc]
        return getattr(self.skill, "prefers_main", "vulcan")

    # ------------------------------------------------------------------
    # Movement decision
    # ------------------------------------------------------------------

    def _decide_movement(self, ps, target, boss_x, skip_dodge):
        px = float(ps.player.x)
        py = float(ps.player.y)
        best_dx, best_dy = 0.0, 0.0
        best_score = self._score_position(px, py, ps, target, boss_x, skip_dodge)
        for ox, oy in self._sample_offsets[1:]:
            nx = px + ox
            ny = py + oy
            s = self._score_position(nx, ny, ps, target, boss_x, skip_dodge)
            if s > best_score:
                best_score = s
                best_dx, best_dy = ox, oy
        m = math.hypot(best_dx, best_dy)
        if m < 1e-6:
            return 0.0, 0.0
        return best_dx / m, best_dy / m

    # ------------------------------------------------------------------
    # Position scoring
    # ------------------------------------------------------------------

    def _score_position(self, x, y, ps, target, boss_x, skip_dodge):
        """Higher = better. Penalties for threats, bonuses for pickups + aim."""
        sk = self.skill
        score = 0.0

        # ---- Hostile bullet threats (includes ricochets) ----
        if not skip_dodge:
            look = sk.lookahead_sec
            dr = sk.danger_radius_px
            ricochet_aware = bool(getattr(sk, "ricochet_aware", False))
            for b in ps.bullets:
                if (not getattr(b, "alive", False)) or getattr(b, "friendly", True):
                    continue
                rx = float(b.x) - x
                ry = float(b.y) - y
                vx = float(getattr(b, "vx", 0.0))
                vy = float(getattr(b, "vy", 0.0))
                vsq = vx * vx + vy * vy
                if vsq < 1.0:
                    continue
                t_close = -(rx * vx + ry * vy) / vsq
                if t_close < 0.0:
                    t_close = 0.0
                elif t_close > look:
                    t_close = look
                fx = rx + vx * t_close
                fy = ry + vy * t_close
                d = math.hypot(fx, fy)
                # Ricocheted bullets carry weapon_kind + ricocheted=True;
                # high-skill bots weight them extra because they came from
                # the player's own line of fire and are usually closer.
                ric_bump = 1.0
                if ricochet_aware and getattr(b, "ricocheted", False):
                    ric_bump = 1.4
                if d < dr:
                    w = (dr - d) / dr
                    score -= w * w * 4.0 * sk.repel_strength * ric_bump

            # ---- Enemy contact + AoE ----
            for e in ps.enemies:
                if not getattr(e, "alive", False):
                    continue
                rx = float(e.x) - x
                ry = float(e.y) - y
                d = math.hypot(rx, ry)
                aoe = getattr(e, "EXPLOSION_RADIUS", None)
                if aoe is not None:
                    contact_r = float(aoe) + 18.0   # margin for the explosion
                    weight = 2.4
                else:
                    contact_r = sk.danger_radius_px * 1.2
                    weight = 1.6
                if d < contact_r:
                    w = (contact_r - d) / contact_r
                    score -= w * w * weight * sk.repel_strength

        # ---- Pickup attraction (situational weights) ----
        if sk.pickup_radius_px > 0 and ps.pickups:
            pr = sk.pickup_radius_px
            # Boost the right pickup kind for our current state.
            player = ps.player
            need_shield = (player.shield_hp / max(1.0, player.shield_max)) < 0.55
            for p in ps.pickups:
                pr_rect = p.rect
                rx = pr_rect.centerx - x
                ry = pr_rect.centery - y
                d = math.hypot(rx, ry)
                if d >= pr:
                    continue
                w = (1.0 - d / pr)
                bump = 1.0
                if getattr(p, "kind", "") == "shield" and need_shield:
                    bump = sk.pickup_priority_shield
                elif getattr(p, "kind", "") == "main":
                    bump = sk.pickup_priority_main
                elif getattr(p, "kind", "") == "money":
                    bump = sk.pickup_priority_money
                score += w * sk.pickup_weight * 0.6 * bump

        # ---- Aim alignment ----
        aim_x = boss_x if boss_x is not None else (
            float(target.x) if target is not None else None)
        if aim_x is not None:
            dx = abs(aim_x - x)
            # Spread fans wide — being slightly off-axis still scores.
            # Vulcan + pulse need tight alignment.
            tolerance = 1.0
            if self._desired_main == "spread":
                tolerance = 2.2
            score -= (dx / (self.play_w * 0.5 * tolerance)) * sk.aim_weight * 0.8

        # ---- Edge repulsion ----
        er = sk.edge_repel_px
        if x < er:
            score -= ((er - x) / er) * 1.5
        elif x > self.play_w - er:
            score -= ((x - (self.play_w - er)) / er) * 1.5
        if y < er:
            score -= ((er - y) / er) * 1.5
        elif y > self.play_h - er:
            score -= ((y - (self.play_h - er)) / er) * 1.5

        # ---- Engagement-zone bias ----
        ideal_y = self.play_h * sk.engagement_y_frac
        score -= abs(y - ideal_y) / self.play_h * 0.4

        return score

    # ------------------------------------------------------------------
    # Target selection
    # ------------------------------------------------------------------

    def _pick_target(self, ps, player):
        """Pick the most worthwhile enemy to focus on. Factors in:

          - HP × distance-to-screen-exit (high HP that's about to leave =
            urgency)
          - Shield color preference: if our currently-held weapon matches
            the shield, the target is "easy" damage and gets a bonus
          - x-distance from us (closer = less movement needed)
          - Don't bother with off-screen / out-of-arena enemies
        """
        sk = self.skill
        px, py = float(player.x), float(player.y)
        target = None
        best_score = -1e18
        cur_main = self._desired_main
        for e in ps.enemies:
            if not getattr(e, "alive", False):
                continue
            ey = float(e.y)
            if ey < -10 or ey > self.play_h + 20:
                continue
            ex = float(e.x)
            # Threat = closer-to-player + shooting + close-to-exit.
            urgency = max(0.0, ey / self.play_h)   # 0 at top, 1 at bottom
            x_dist = abs(ex - px)
            hp = float(getattr(e, "hp", 1))
            score = (hp * 100.0
                     - x_dist * (1.0 - sk.target_threat_weight * 0.5)
                     - max(0.0, ey - py) * 0.5
                     + urgency * 4000.0 * sk.target_threat_weight)
            # Shield-color compatibility: a shielded enemy whose colour
            # matches what we're holding is a juicy target; a shielded
            # enemy whose colour doesn't match is a *cost* (we'd ricochet).
            sc = getattr(e, "shield_color", None)
            if sc:
                ideal = _SHIELD_COLOR_TO_MAIN.get(sc)
                if ideal == cur_main:
                    score += 1500.0
                else:
                    score -= 1200.0
            if score > best_score:
                best_score = score
                target = e
        return target

    # ------------------------------------------------------------------
    # Ability decision
    # ------------------------------------------------------------------

    def _maybe_use_ability(self, ps, controls):
        player = ps.player
        if getattr(player, "ability_cd", 0.0) > 0.0:
            return
        sk = self.skill
        strategy = getattr(sk, "ability_strategy", "panic")
        ratio = float(player.shield_hp) / max(1.0, float(player.shield_max))
        is_boss = bool(getattr(ps, "is_boss_fight", False))

        # Shared rules across all strategies:
        # 1) Always dump on the boss the moment cooldown is up. Bosses are
        #    high-HP, the ability hard-counters them (mega_laser bypasses
        #    shield, screen_clear bypasses, shield_burst gives invuln to
        #    weather the next pattern). Holding the bomb for "the right
        #    moment" wastes its second + third uses during the 60-90s
        #    fight.
        # 2) Panic-fire when shield ratio dips below threshold.
        if is_boss:
            controls.ability_pressed = True
            return
        if strategy != "always_ready" and ratio < sk.bomb_threshold:
            controls.ability_pressed = True
            return

        if strategy == "always_ready":
            controls.ability_pressed = True
            return
        if strategy == "crowd":
            active = sum(1 for e in ps.enemies
                         if getattr(e, "alive", False) and float(e.y) >= 0)
            if active >= 8:
                controls.ability_pressed = True
            return
        # "panic" / "boss" — no further triggers; "boss" already fired
        # above on the is_boss check.
