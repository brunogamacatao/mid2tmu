"""
fm/voices.py
────────────
Synthetic OPLL (YM2413) voice patch generation from GM program numbers.

The OPLL has a 2-operator FM architecture. Each custom patch is 8 bytes:
  [0] Modulator: AM/VIB/EG/KSR/MULT
  [1] Carrier:   AM/VIB/EG/KSR/MULT
  [2] Modulator: KSL/TL  (total level — controls modulation depth)
  [3] Carrier:   KSL/TL  (forced 0 for max carrier output)
  [4] Modulator: AR/DR   (attack/decay rates)
  [5] Carrier:   AR/DR
  [6] Modulator: SL/RR   (sustain level/release rate)
  [7] Carrier:   SL/RR

Each GM program maps to a family that drives the envelope parameters.
These are intentional approximations — the tracker allows manual tuning
after import.

Tuning guide
────────────
  brightness  controls modulator TL (higher = more harmonic content)
  EG flag     enables envelope generator sustain on the modulator
  mod_mult    modulator frequency multiplier (changes timbre)
  car_mult    carrier frequency multiplier (usually 1)

To manually tune a voice:
  1. Load the TMU in TriloTracker.
  2. Open the FM voice editor.
  3. Adjust TL, AR, DR, SL, RR for the modulator and carrier.
  4. Re-save the TMU.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────
# GM family table
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GmFamily:
    name:       str
    attack:     int    # 0-15 — higher = faster
    decay:      int    # 0-15
    sustain:    int    # 0-15 — higher = more sustain
    release:    int    # 0-15
    brightness: int    # 0x00-0x40  higher = more harmonic richness
    mod_mult:   int = 2
    car_mult:   int = 1
    eg_flag:    bool = False  # envelope generator sustain on modulator


_GM_FAMILIES: List[Tuple[range, GmFamily]] = [
    (range(  0,   8), GmFamily("piano",      12,  8, 10,  6, 0x40)),
    (range(  8,  16), GmFamily("chromperc",  14,  5,  8,  4, 0x20)),
    (range( 16,  24), GmFamily("organ",       4,  0, 12,  2, 0x20, eg_flag=True)),
    (range( 24,  32), GmFamily("guitar",     13,  7,  5,  5, 0x10)),
    (range( 32,  40), GmFamily("bass",       14,  9,  4,  4, 0x00)),
    (range( 40,  48), GmFamily("strings",     6,  4, 10,  6, 0x10, eg_flag=True)),
    (range( 48,  56), GmFamily("ensemble",    5,  3, 11,  5, 0x10, eg_flag=True)),
    (range( 56,  64), GmFamily("brass",       9,  6,  8,  5, 0x20)),
    (range( 64,  72), GmFamily("reed",        7,  5, 10,  4, 0x20, eg_flag=True)),
    (range( 72,  80), GmFamily("pipe",        6,  2, 12,  3, 0x10, eg_flag=True)),
    (range( 80,  88), GmFamily("synthlead",  10,  8,  8,  6, 0x30)),
    (range( 88,  96), GmFamily("synthpad",    4,  2, 12,  4, 0x10, eg_flag=True)),
    (range( 96, 104), GmFamily("synthfx",     6,  4,  9,  5, 0x20)),
    (range(104, 112), GmFamily("ethnic",     12,  8,  7,  5, 0x10)),
    (range(112, 120), GmFamily("percussive", 14, 10,  3,  4, 0x00)),
    (range(120, 128), GmFamily("soundfx",    15, 12,  0,  2, 0x00)),
]

_UNKNOWN_FAMILY = GmFamily("unknown", 10, 8, 8, 6, 0x20)


def get_gm_family(program: int) -> GmFamily:
    """Return the GmFamily for a GM program number (0-127)."""
    for r, fam in _GM_FAMILIES:
        if program in r:
            return fam
    return _UNKNOWN_FAMILY


def gm_family_name(program: int) -> str:
    return get_gm_family(program).name


# ─────────────────────────────────────────────────────────────────
# OPLLVoice
# ─────────────────────────────────────────────────────────────────

@dataclass
class OPLLVoice:
    """
    An 8-byte OPLL custom voice patch.

    Can be constructed automatically from a GM program number,
    or built manually for precise tuning.

    Manual construction example
    ───────────────────────────
    voice = OPLLVoice.from_bytes(bytes([0x22,0x21,0x1c,0x00,0xf7,0xe5,0x75,0x74]))
    """

    program:    int         # source GM program (informational)
    family:     str         # family name (informational)
    data:       bytes       # 8 raw bytes sent to OPLL

    # ── factories ─────────────────────────────────────────────────

    @classmethod
    def from_program(cls, program: int) -> "OPLLVoice":
        """
        Synthesise an OPLL patch from a GM program number.
        The result is a reasonable approximation of the GM timbre
        that can be further tuned in the TriloTracker FM voice editor.
        """
        fam = get_gm_family(program)
        data = _synthesise_patch(fam)
        return cls(program=program, family=fam.name, data=data)

    @classmethod
    def from_bytes(cls, data: bytes, program: int = 0) -> "OPLLVoice":
        """Create a voice from a raw 8-byte patch (e.g. imported from hardware)."""
        if len(data) != 8:
            raise ValueError(f"OPLL voice must be exactly 8 bytes, got {len(data)}")
        fam = get_gm_family(program)
        return cls(program=program, family=fam.name, data=bytes(data))

    # ── display ───────────────────────────────────────────────────

    def hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.data)

    def __repr__(self) -> str:
        return f"OPLLVoice(prog={self.program} '{self.family}' [{self.hex()}])"


def _synthesise_patch(fam: GmFamily) -> bytes:
    """
    Derive 8 OPLL bytes from a GmFamily descriptor.

    OPLL register layout reminder:
      b0/b1  [AM|VIB|EG|KSR | MULT(4)]   operator flags + frequency multiplier
      b2/b3  [KSL(2) | TL(6)]            key scale + total level
      b4/b5  [AR(4) | DR(4)]             attack + decay
      b6/b7  [SL(4) | RR(4)]             sustain level + release

    For the carrier (b1,b3,b5,b7): TL is forced to 0 so the carrier
    outputs at full level; the envelope shape alone controls volume.
    """

    # Modulator
    mod_eg   = 0x20 if fam.eg_flag else 0x00
    mod_mult = fam.mod_mult & 0x0F
    mod_tl   = min(63, 16 + (fam.brightness >> 2))
    mod_ar   = min(15, fam.attack  + 2)
    mod_dr   = min(15, fam.decay   + 2)
    mod_sl   = min(15, 15 - fam.sustain)
    mod_rr   = min(15, fam.release + 1)

    # Carrier — always EG-sustained so notes hold while key is pressed
    car_eg   = 0x20
    car_mult = fam.car_mult & 0x0F
    car_tl   = 0
    car_ar   = min(15, fam.attack)
    car_dr   = min(15, fam.decay)
    car_sl   = min(15, 15 - fam.sustain)
    car_rr   = min(15, fam.release)

    b0 = mod_eg | mod_mult
    b1 = car_eg | car_mult
    b2 = mod_tl & 0x3F
    b3 = car_tl & 0x3F
    b4 = (mod_ar << 4) | mod_dr
    b5 = (car_ar << 4) | car_dr
    b6 = (mod_sl << 4) | mod_rr
    b7 = (car_sl << 4) | car_rr

    return bytes([b0, b1, b2, b3, b4, b5, b6, b7])
