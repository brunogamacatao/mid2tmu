"""
tmu/model.py
────────────
TMU-level data structures that sit between the chip converter and
the binary writer.  These map directly onto the TMU file format.

Hierarchy
─────────
  TmuSong
    ├─ instruments[]     31 slots, 1-indexed
    ├─ fm_voices[]       up to 16 custom OPLL patches (FM mode)
    ├─ drum_names[]      19 names (FM mode)
    ├─ patterns[]        TmuPattern objects
    └─ order[]           sequence of pattern indices

  TmuPattern
    └─ rows[64]          list of TmuRow (one per tracker row)

  TmuRow
    └─ channels[8]       list of TmuCell

  TmuCell
    ├─ note    0 / 1-96 / 97=release / 98=sustain
    ├─ ins     0=empty, 1-31
    ├─ vol     0=empty, 1-15
    ├─ cmd     effect command nibble
    └─ par     effect parameter byte
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from midi2tmu.tmu.constants import (
    ROWS_PER_PATTERN,
    CHANNELS_PER_ROW,
    MAX_INSTRUMENTS,
    MAX_CUSTOM_VOICES,
    MAX_DRUMS,
    DRUM_NAME_LEN,
    NOTE_EMPTY,
)


# ─────────────────────────────────────────────────────────────────
# Cell — one channel slot in one pattern row
# ─────────────────────────────────────────────────────────────────

@dataclass
class TmuCell:
    note: int = NOTE_EMPTY   # 0=empty, 1-96=note, 97=release, 98=sustain
    ins:  int = 0            # 0=empty, 1-31
    vol:  int = 0            # 0=empty, 1-15
    cmd:  int = 0            # effect command nibble (0-15)
    par:  int = 0            # effect parameter byte (0-255)

    def is_empty(self) -> bool:
        return self.note == 0 and self.ins == 0 and self.vol == 0 and self.cmd == 0

    def pack(self) -> tuple[int, int, int, int]:
        """Return (note, ins, volcmd, par) as stored in the pattern bytes."""
        return (
            self.note & 0xFF,
            self.ins  & 0xFF,
            ((self.vol & 0xF) << 4) | (self.cmd & 0xF),
            self.par  & 0xFF,
        )


# ─────────────────────────────────────────────────────────────────
# Row — 8 channels in one pattern row
# ─────────────────────────────────────────────────────────────────

@dataclass
class TmuRow:
    channels: List[TmuCell] = field(
        default_factory=lambda: [TmuCell() for _ in range(CHANNELS_PER_ROW)]
    )

    def __getitem__(self, ch: int) -> TmuCell:
        return self.channels[ch]

    def __setitem__(self, ch: int, cell: TmuCell) -> None:
        self.channels[ch] = cell


# ─────────────────────────────────────────────────────────────────
# Pattern — 64 rows
# ─────────────────────────────────────────────────────────────────

@dataclass
class TmuPattern:
    index: int    # pattern number (0-based)
    rows:  List[TmuRow] = field(
        default_factory=lambda: [TmuRow() for _ in range(ROWS_PER_PATTERN)]
    )

    def cell(self, row: int, channel: int) -> TmuCell:
        return self.rows[row].channels[channel]

    def set_cell(self, row: int, channel: int, cell: TmuCell) -> None:
        self.rows[row].channels[channel] = cell

    def flat_bytes(self) -> List[int]:
        """
        Flatten to 2048 bytes in TMU layout:
          offset = channel * 4 + row * PATTERN_LINE_SIZE
        """
        from midi2tmu.tmu.constants import PATTERN_SIZE, PATTERN_LINE_SIZE
        buf = [0] * PATTERN_SIZE
        for row_idx, row in enumerate(self.rows):
            for ch_idx, cell in enumerate(row.channels):
                n, i, vc, p = cell.pack()
                base = ch_idx * 4 + row_idx * PATTERN_LINE_SIZE
                buf[base + 0] = n
                buf[base + 1] = i
                buf[base + 2] = vc
                buf[base + 3] = p
        return buf

    def is_empty(self) -> bool:
        return all(c.is_empty() for row in self.rows for c in row.channels)


# ─────────────────────────────────────────────────────────────────
# Instrument macro
# ─────────────────────────────────────────────────────────────────

@dataclass
class TmuInstrument:
    """
    One instrument slot (1-31).
    The macro is a list of rows; each row is 4 bytes:
      [noise_byte, vol_byte, tone_low, tone_high]
    restart < len(rows) — loops back to that row on the last tick.
    """
    slot:    int          # 1-31
    name:    str = ""
    voice:   int = 0      # 0=none/default, 1-15=hw preset, 16-31=custom voice
    restart: int = 0      # loop row index; MUST be < len(rows)
    rows:    List[bytes] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.rows:
            # Default: one silent row
            self.rows = [bytes([0x00, 0x00, 0x00, 0x00])]

    @property
    def length(self) -> int:
        return len(self.rows)


# ─────────────────────────────────────────────────────────────────
# TmuSong — the full TMU-level song representation
# ─────────────────────────────────────────────────────────────────

@dataclass
class TmuSong:
    """
    Everything needed to write a valid .tmu binary file.
    Produced by a chip converter (FMConverter, …) and consumed
    by TmuWriter.
    """

    # ── header fields ────────────────────────────────────────────
    title:      str = "Untitled"
    author:     str = ""
    speed:      int = 6        # initial playback speed (1-15)
    period:     int = 0        # tuning table (PERIOD_MODERN etc.)
    chipset:    int = 0x10     # CHIPSET_FM, CHIPSET_SCC, CHIPSET_SMS
    chan_setup: int = 0x00     # CHANSETUP_DEFAULT or CHANSETUP_2_6
    order:      List[int] = field(default_factory=list)   # pattern indices
    loop_pos:   int = 0xFF     # 0xFF = no loop

    # ── instruments ──────────────────────────────────────────────
    instruments: List[TmuInstrument] = field(default_factory=list)

    # ── FM-specific ───────────────────────────────────────────────
    fm_voices:  List[bytes] = field(default_factory=list)    # 0-16 × 8 bytes
    drum_names: List[str]   = field(
        default_factory=lambda: [
            "Bass Drum", "Snare",    "Hi-Hat",  "Cymbal",   "Tom",
            "Rim Shot",  "Cowbell",  "Clap",    "Tamb",     "Conga",
            "Bongo",     "Cabasa",   "Maracas", "Whistle",  "Guiro",
            "Claves",    "Agogo",    "Triangle","Open HH",
        ]
    )

    # ── patterns ─────────────────────────────────────────────────
    patterns: List[TmuPattern] = field(default_factory=list)
