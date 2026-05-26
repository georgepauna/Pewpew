"""Replay file format.

A replay file is the input stream of one bot run. Together with the seed
recorded in the header and the same pewpew.py code, replaying yields a
frame-identical reproduction of the run.

Format (little-endian):

  Header:
    4s    magic = b"PWRP"
    u32   version = 1
    u64   seed
    u16   profile_name_len
    Ns    profile_name (utf-8)

  Per-level block (repeats until EOF):
    u32   marker = 0xC001
    4s    level_key  (e.g. b"L001")
    u32   attempt    (1-based)
    u32   save_json_len
    Ns    save_json   (utf-8 dump of SaveData asdict at level start)
    u32   frame_count
    Hs    per-frame controls bitfields (u16 each, frame_count of them)
    u32   trailer = 0xC002
    u8    won
    u32   score

The bot stores ONE replay file per (run, profile). Reading it back lets the
Phase 2 replay player reconstruct what happened.
"""

import json
import struct
import sys
from pathlib import Path


def controls_to_bits(c):
    b = 0
    if c.left:              b |= 1 << 0
    if c.right:             b |= 1 << 1
    if c.up:                b |= 1 << 2
    if c.down:              b |= 1 << 3
    if c.fire:              b |= 1 << 4
    if c.bomb_pressed:      b |= 1 << 5
    if c.ability_pressed:   b |= 1 << 6
    if c.confirm_pressed:   b |= 1 << 7
    if c.cancel_pressed:    b |= 1 << 8
    if c.start_pressed:     b |= 1 << 9
    if c.select:            b |= 1 << 10
    if c.start:             b |= 1 << 11
    return b


def bits_to_controls(c, b):
    c.reset_pulses()
    c.left =                bool(b & (1 << 0))
    c.right =               bool(b & (1 << 1))
    c.up =                  bool(b & (1 << 2))
    c.down =                bool(b & (1 << 3))
    c.fire =                bool(b & (1 << 4))
    c.bomb_pressed =        bool(b & (1 << 5))
    c.ability_pressed =     bool(b & (1 << 6))
    c.confirm_pressed =     bool(b & (1 << 7))
    c.cancel_pressed =      bool(b & (1 << 8))
    c.start_pressed =       bool(b & (1 << 9))
    c.select =              bool(b & (1 << 10))
    c.start =               bool(b & (1 << 11))


class ReplayWriter:
    def __init__(self, seed, profile):
        self.seed = seed
        self.profile = profile
        self._buf = bytearray()
        self._buf += b"PWRP"
        self._buf += struct.pack("<IQ", 1, seed & 0xFFFFFFFFFFFFFFFF)
        prof_b = profile.encode("utf-8")
        self._buf += struct.pack("<H", len(prof_b))
        self._buf += prof_b
        self._cur_meta = None
        self._cur_frames = []

    def begin_level(self, level_key, attempt, save_snapshot, per_level_seed=None):
        self._cur_meta = {
            "level_key": level_key,
            "attempt": int(attempt),
            "save": save_snapshot,
            "per_level_seed": int(per_level_seed) if per_level_seed is not None else None,
        }
        self._cur_frames = []

    def record_frame(self, controls):
        self._cur_frames.append(controls_to_bits(controls))

    def end_level(self, won, score):
        if self._cur_meta is None:
            return
        meta_bytes = json.dumps(self._cur_meta, default=str).encode("utf-8")
        key_b = self._cur_meta["level_key"].encode("ascii")[:4].ljust(4, b" ")
        self._buf += struct.pack("<I", 0xC001)
        self._buf += key_b
        self._buf += struct.pack("<I", self._cur_meta["attempt"])
        self._buf += struct.pack("<I", len(meta_bytes))
        self._buf += meta_bytes
        self._buf += struct.pack("<I", len(self._cur_frames))
        for f in self._cur_frames:
            self._buf += struct.pack("<H", int(f) & 0xFFFF)
        self._buf += struct.pack("<I", 0xC002)
        self._buf += struct.pack("<BI", 1 if won else 0, int(score) & 0xFFFFFFFF)
        self._cur_meta = None
        self._cur_frames = []

    def save(self, path):
        Path(path).write_bytes(bytes(self._buf))


def read_replay(path):
    """Return (seed, profile_name, [level_block, ...])."""
    data = Path(path).read_bytes()
    o = 0
    if data[o:o + 4] != b"PWRP":
        raise ValueError("not a pewpew replay")
    o += 4
    version, seed = struct.unpack_from("<IQ", data, o); o += 12
    if version != 1:
        raise ValueError(f"unsupported replay version {version}")
    (prof_len,) = struct.unpack_from("<H", data, o); o += 2
    profile = data[o:o + prof_len].decode("utf-8"); o += prof_len
    blocks = []
    while o < len(data):
        (marker,) = struct.unpack_from("<I", data, o); o += 4
        if marker != 0xC001:
            raise ValueError(f"unexpected marker 0x{marker:08X} at {o}")
        level_key = data[o:o + 4].decode("ascii").strip(); o += 4
        (attempt,) = struct.unpack_from("<I", data, o); o += 4
        (meta_len,) = struct.unpack_from("<I", data, o); o += 4
        meta = json.loads(data[o:o + meta_len].decode("utf-8")); o += meta_len
        (frame_count,) = struct.unpack_from("<I", data, o); o += 4
        frames = list(struct.unpack_from(f"<{frame_count}H", data, o))
        o += 2 * frame_count
        (trailer,) = struct.unpack_from("<I", data, o); o += 4
        if trailer != 0xC002:
            raise ValueError(f"bad trailer at {o}")
        won, score = struct.unpack_from("<BI", data, o); o += 5
        blocks.append({
            "level_key": level_key,
            "attempt": attempt,
            "meta": meta,
            "frames": frames,
            "won": bool(won),
            "score": score,
        })
    return seed, profile, blocks


def play_replay_from_cli(cli):
    """Phase 2 stub. Real visual replay player comes next iteration."""
    path = cli["replay"]
    seed, profile, blocks = read_replay(path)
    print(f"[replay] {path}")
    print(f"  seed:    {seed}")
    print(f"  profile: {profile}")
    print(f"  levels:  {len(blocks)}")
    total_frames = sum(len(b['frames']) for b in blocks)
    print(f"  frames:  {total_frames} ({total_frames / 60:.1f}s simulated)")
    wins = sum(1 for b in blocks if b["won"])
    print(f"  outcome: {wins}/{len(blocks)} won")
    print("")
    print("Visual playback is a Phase 2 deliverable. The file above has")
    print("everything needed: seed + initial save + per-frame inputs.")
    sys.exit(0)
