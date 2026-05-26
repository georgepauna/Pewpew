"""In-play bot AI.

Each frame, reads PlayState and fills a Controls instance. Pure function of
state — no internal RNG — so a recorded input stream replays identically
when paired with the same seed.

Movement is a simple potential field:
  - Enemy bullets within `lookahead_sec` repel (predicted closest approach).
  - Enemy bodies repel at short range.
  - Pickups attract within `pickup_radius_px`.
  - The strongest visible-HP enemy attracts horizontally for aiming.
  - Playfield edges repel.

Firing is always-on (skill is positional, not trigger-disciplined).

Ability is fired when the shield ratio drops below `bomb_threshold`, or
proactively when many enemies are on screen if the profile allows it.
"""

import math


class PlayBot:
    def __init__(self, skill, play_w, play_h):
        self.skill = skill
        self.play_w = play_w
        self.play_h = play_h

    def step(self, play_state, controls):
        controls.reset_pulses()
        player = play_state.player
        if not player.alive or getattr(player, "cinematic", False):
            controls.left = controls.right = controls.up = controls.down = False
            controls.fire = False
            return

        sk = self.skill
        px, py = float(player.x), float(player.y)

        mvx = 0.0
        mvy = 0.0

        # --- Threat repulsion: enemy bullets ---
        look = sk.lookahead_sec
        dr = sk.danger_radius_px
        for b in play_state.bullets:
            if (not getattr(b, "alive", False)) or getattr(b, "friendly", True):
                continue
            rx = float(b.x) - px
            ry = float(b.y) - py
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
            if 0.5 < d < dr:
                w = (dr - d) / dr * sk.repel_strength
                mvx -= (fx / d) * w
                mvy -= (fy / d) * w

        # --- Enemy contact repulsion ---
        contact_r = dr * 1.2
        for e in play_state.enemies:
            if not getattr(e, "alive", False):
                continue
            rx = float(e.x) - px
            ry = float(e.y) - py
            d = math.hypot(rx, ry)
            if 0.5 < d < contact_r:
                w = (contact_r - d) / contact_r * sk.repel_strength * 1.5
                mvx -= (rx / d) * w
                mvy -= (ry / d) * w

        # --- Pickup attraction ---
        if sk.pickup_radius_px > 0 and play_state.pickups:
            pr = sk.pickup_radius_px
            for p in play_state.pickups:
                pr_rect = p.rect
                rx = pr_rect.centerx - px
                ry = pr_rect.centery - py
                d = math.hypot(rx, ry)
                if 0.5 < d < pr:
                    w = sk.pickup_weight * (1.0 - d / pr)
                    mvx += (rx / d) * w
                    mvy += (ry / d) * w

        # --- Aim alignment: pick a target ---
        target = None
        best_score = -1e18
        for e in play_state.enemies:
            if not getattr(e, "alive", False):
                continue
            if float(e.y) < -10:
                continue
            # Prefer high-HP, on-screen, close horizontally, not too far below.
            score = (float(e.hp) * 100.0
                     - abs(float(e.x) - px)
                     - max(0.0, float(e.y) - py))
            if score > best_score:
                best_score = score
                target = e
        if target is not None:
            dx = float(target.x) - px
            mag = min(abs(dx), 60.0) / 60.0 * sk.aim_weight
            if dx > 0:
                mvx += mag
            else:
                mvx -= mag

        # --- Edge repulsion ---
        er = sk.edge_repel_px
        if px < er:
            mvx += (er - px) / er
        elif px > self.play_w - er:
            mvx -= (px - (self.play_w - er)) / er
        if py < er:
            mvy += (er - py) / er
        elif py > self.play_h - er:
            mvy -= (py - (self.play_h - er)) / er

        # --- Bias slightly upward when low to engage enemies ---
        if py > self.play_h * 0.55:
            mvy -= 0.2

        # --- Threshold into D-pad ---
        DZ = 0.20
        controls.left  = mvx < -DZ
        controls.right = mvx >  DZ
        controls.up    = mvy < -DZ
        controls.down  = mvy >  DZ

        # --- Fire trigger ---
        controls.fire = sk.fire_always

        # --- Ability ---
        ability_ready = getattr(player, "ability_cd", 0.0) <= 0.0
        if ability_ready:
            ratio = float(player.shield_hp) / max(1.0, float(player.shield_max))
            if ratio < sk.bomb_threshold:
                controls.ability_pressed = True
            elif sk.bomb_proactive:
                active = 0
                for e in play_state.enemies:
                    if getattr(e, "alive", False) and float(e.y) >= 0:
                        active += 1
                if active >= 8:
                    controls.ability_pressed = True
