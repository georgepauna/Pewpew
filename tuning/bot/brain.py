"""In-play bot AI.

Each frame, reads PlayState and fills a Controls instance. The brain is
pure (no side effects on game state) so a recorded input stream replays
identically given the same seed.

Movement uses **position sampling** rather than a per-threat force sum.
We score the player's current position plus 8 candidate neighbour positions
and move toward the highest-scoring one. Scoring combines:

  - Bullet threat (predicted closest-approach within `lookahead_sec`)
  - Enemy contact threat, with **AoE radius** for mines / future bombs
  - Pickup attraction
  - Aim alignment to a selected target
  - Edge avoidance
  - A vertical bias toward the engagement zone (upper 2/3)
  - **Boss-fight horizontal weave** when a boss is active

Sampling beats per-threat vector sums in crossfire situations: when two
bullets converge from opposite directions, the per-bullet vectors cancel
and the bot stands still. Sampling sees that every NSEW step has lower
threat than staying put and picks the best escape.

Stochastic dropout (`skill.dodge_dropout`) is a per-frame chance to skip
the dodge calculation entirely — bad bots occasionally just... don't
dodge. Uses a dedicated RNG so the game's seeded random state is
unaffected (and replays don't desync).
"""

import math
import random


class PlayBot:
    def __init__(self, skill, play_w, play_h, rng_seed=None):
        self.skill = skill
        self.play_w = play_w
        self.play_h = play_h
        # Dedicated RNG so dodge dropouts don't perturb the game's random
        # stream — keeps recorded replays deterministic.
        self._rng = random.Random(rng_seed if rng_seed is not None else 0xBADBAB)
        self._boss_phase = 0.0   # boss weave oscillator
        # 9-direction sample offsets (stay + 8-way).
        self._sample_offsets = [(0.0, 0.0)]
        for ang_deg in (0, 45, 90, 135, 180, 225, 270, 315):
            ang = math.radians(ang_deg)
            self._sample_offsets.append((
                math.cos(ang) * 45.0,
                math.sin(ang) * 45.0,
            ))

    # ------------------------------------------------------------------
    # Public per-frame step
    # ------------------------------------------------------------------

    def step(self, play_state, controls):
        controls.reset_pulses()
        player = play_state.player
        if not player.alive or getattr(player, "cinematic", False):
            controls.left = controls.right = controls.up = controls.down = False
            controls.fire = False
            return

        # Pick a target enemy for aim alignment.
        target = self._pick_target(play_state, player.x, player.y)

        # Boss weave: oscillate horizontal target when a boss is active.
        boss_x = None
        if getattr(play_state, "is_boss_fight", False):
            self._boss_phase += 0.045
            boss_x = self.play_w * 0.5 + math.sin(self._boss_phase) * (self.play_w * 0.30)

        # Stochastic skill dropout — bad bots miss frames.
        skip_dodge = False
        if self.skill.dodge_dropout > 0.0:
            if self._rng.random() < self.skill.dodge_dropout:
                skip_dodge = True

        # Score each sample position; move toward the best.
        mvx, mvy = self._decide_movement(play_state, target, boss_x, skip_dodge)

        # Threshold into the d-pad bools.
        DZ = 0.20
        controls.left  = mvx < -DZ
        controls.right = mvx >  DZ
        controls.up    = mvy < -DZ
        controls.down  = mvy >  DZ

        # Fire trigger
        controls.fire = self.skill.fire_always

        # Ability: reactively when shield low, or proactively when crowded.
        ability_ready = getattr(player, "ability_cd", 0.0) <= 0.0
        if ability_ready:
            ratio = float(player.shield_hp) / max(1.0, float(player.shield_max))
            if ratio < self.skill.bomb_threshold:
                controls.ability_pressed = True
            elif self.skill.bomb_proactive:
                active = 0
                for e in play_state.enemies:
                    if getattr(e, "alive", False) and float(e.y) >= 0:
                        active += 1
                if active >= 8:
                    controls.ability_pressed = True

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
        # Normalise the chosen offset to a unit-ish vector so the d-pad
        # threshold sees a consistent magnitude regardless of sample step.
        m = math.hypot(best_dx, best_dy)
        if m < 1e-6:
            return 0.0, 0.0
        return best_dx / m, best_dy / m

    # ------------------------------------------------------------------
    # Position scoring
    # ------------------------------------------------------------------

    def _score_position(self, x, y, ps, target, boss_x, skip_dodge):
        """Higher = better. Penalties for threats, bonuses for pickups / aim."""
        sk = self.skill
        score = 0.0

        # ---- Bullet threats (skipped on a dodge-dropout frame) ----
        if not skip_dodge:
            look = sk.lookahead_sec
            dr = sk.danger_radius_px
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
                if d < dr:
                    # Squared penalty so very close bullets dominate.
                    w = (dr - d) / dr
                    score -= w * w * 4.0 * sk.repel_strength

            # ---- Enemy contact / AoE threats ----
            for e in ps.enemies:
                if not getattr(e, "alive", False):
                    continue
                rx = float(e.x) - x
                ry = float(e.y) - y
                d = math.hypot(rx, ry)
                # Mines and any future AoE-on-death enemies expose
                # EXPLOSION_RADIUS — respect it with a small margin.
                aoe = getattr(e, "EXPLOSION_RADIUS", None)
                if aoe is not None:
                    contact_r = float(aoe) + 18.0   # margin for safety
                    weight = 2.4
                else:
                    contact_r = sk.danger_radius_px * 1.2
                    weight = 1.6
                if d < contact_r:
                    w = (contact_r - d) / contact_r
                    score -= w * w * weight * sk.repel_strength

        # ---- Pickup attraction ----
        if sk.pickup_radius_px > 0 and ps.pickups:
            pr = sk.pickup_radius_px
            for p in ps.pickups:
                pr_rect = p.rect
                rx = pr_rect.centerx - x
                ry = pr_rect.centery - y
                d = math.hypot(rx, ry)
                if d < pr:
                    w = (1.0 - d / pr)
                    score += w * sk.pickup_weight * 0.6

        # ---- Aim alignment ----
        aim_x = boss_x if boss_x is not None else (target.x if target is not None else None)
        if aim_x is not None:
            dx = abs(float(aim_x) - x)
            # Closer is better; ranges over ~half the playfield width.
            score -= (dx / (self.play_w * 0.5)) * sk.aim_weight * 0.8

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

        # ---- Engagement-zone bias (upper 2/3) ----
        ideal_y = self.play_h * 0.62
        score -= abs(y - ideal_y) / self.play_h * 0.4

        return score

    # ------------------------------------------------------------------
    # Target selection
    # ------------------------------------------------------------------

    def _pick_target(self, ps, px, py):
        target = None
        best_score = -1e18
        for e in ps.enemies:
            if not getattr(e, "alive", False):
                continue
            ey = float(e.y)
            if ey < -10:
                continue
            score = (float(e.hp) * 100.0
                     - abs(float(e.x) - px)
                     - max(0.0, ey - py))
            if score > best_score:
                best_score = score
                target = e
        return target
