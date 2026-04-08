"""
tmu/converter.py
────────────────
Converts a chip-agnostic MidiSong into a TmuSong targeting FM (OPLL).

This is where all the chip-specific decisions happen:
  • Assigning MIDI tracks to 8 TMU channels (voice allocation).
  • Building OPLL voice patches from GM programs.
  • Quantising notes into TmuPattern/TmuCell objects.
  • Computing playback speed from BPM.

Extension point
───────────────
Subclass FMConverter or create an SCCConverter alongside it following
the same interface (convert(song) → TmuSong).  The CLI and writer are
agnostic to which converter is used.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from midi2tmu.song.model import MidiSong, Track, Note
from midi2tmu.fm.voices import OPLLVoice
from midi2tmu.tmu.constants import (
    CHIPSET_FM,
    CHANSETUP_DEFAULT,
    MAX_INSTRUMENTS,
    MAX_CUSTOM_VOICES,
    ROWS_PER_PATTERN,
    CHANNELS_PER_ROW,
    VOICE_CUSTOM_BASE,
    NOTE_RELEASE,
    ORDER_END,
    PERIOD_MODERN,
)
from midi2tmu.tmu.model import (
    TmuSong,
    TmuPattern,
    TmuRow,
    TmuCell,
    TmuInstrument,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Voice bank
# ─────────────────────────────────────────────────────────────────

class VoiceBank:
    """
    Maintains a mapping from GM program → OPLL custom voice slot (0-15).

    When the bank is full (16 voices), the first registered program
    is reused for any new program.  This is a safe fallback but a
    warning is emitted so the user can manually reassign voices.

    Attributes available after conversion
    ──────────────────────────────────────
    voices   list[OPLLVoice]  — in slot order, ready for TmuSong.fm_voices
    """

    def __init__(self) -> None:
        self._program_to_slot: Dict[int, int] = {}
        self.voices: List[OPLLVoice] = []

    def get_or_add(self, program: int) -> int:
        """Return voice slot index (0-15) for this GM program."""
        if program in self._program_to_slot:
            return self._program_to_slot[program]

        if len(self.voices) >= MAX_CUSTOM_VOICES:
            fallback = next(iter(self._program_to_slot))
            log.warning(
                "Voice bank full (16 slots).  GM#%d will reuse slot for GM#%d.",
                program, fallback,
            )
            slot = self._program_to_slot[fallback]
            self._program_to_slot[program] = slot
            return slot

        slot = len(self.voices)
        voice = OPLLVoice.from_program(program)
        self.voices.append(voice)
        self._program_to_slot[program] = slot
        log.debug("Voice bank: GM#%03d → slot %02d  [%s]", program, slot, voice.hex())
        return slot

    def summary(self) -> str:
        lines = []
        for prog, slot in sorted(self._program_to_slot.items(), key=lambda x: x[1]):
            v = self.voices[slot]
            lines.append(f"  Slot {slot:02d}  GM#{prog:03d} ({v.family:<12s})  [{v.hex()}]")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
# Instrument bank
# ─────────────────────────────────────────────────────────────────

class InstrumentBank:
    """
    Maintains a mapping from GM program → TMU instrument slot (1-31).

    Each instrument has a minimal 1-row macro that:
      • enables tone output (bit 7 of byte 1)
      • sets volume to max (0x0F)
      • points to the correct OPLL custom voice
    """

    def __init__(self, voice_bank: VoiceBank) -> None:
        self._voice_bank = voice_bank
        self._program_to_slot: Dict[int, int] = {}
        self.instruments: List[TmuInstrument] = []

    def get_or_add(self, program: int) -> int:
        """Return instrument slot (1-31) for this GM program."""
        if program in self._program_to_slot:
            return self._program_to_slot[program]

        if len(self.instruments) >= MAX_INSTRUMENTS:
            fallback = next(iter(self._program_to_slot))
            log.warning(
                "Instrument bank full (31 slots).  GM#%d will reuse slot for GM#%d.",
                program, fallback,
            )
            slot = self._program_to_slot[fallback]
            self._program_to_slot[program] = slot
            return slot

        voice_slot = self._voice_bank.get_or_add(program)
        voice_byte = VOICE_CUSTOM_BASE + voice_slot   # 16-31 signals custom voice

        slot = len(self.instruments) + 1   # 1-indexed
        name = self._voice_bank.voices[voice_slot].family
        ins = TmuInstrument(
            slot=slot,
            name=f"P{program:03d}_{name[:8]}",
            voice=voice_byte,
            restart=0,    # loop row 0 — MUST be < length
            rows=[
                # byte0: noise ctrl = 0 (no noise)
                # byte1: 0x8F = tone-on(bit7) + base-vol(bits5:4=00) + vol=15
                # byte2: tone_low = 0
                # byte3: tone_high = 0
                bytes([0x00, 0x8F, 0x00, 0x00])
            ],
        )
        self.instruments.append(ins)
        self._program_to_slot[program] = slot
        log.debug(
            "Instrument bank: GM#%03d → slot %02d  name='%s'  voice_byte=0x%02x",
            program, slot, ins.name, voice_byte,
        )
        return slot

    def summary(self) -> str:
        lines = []
        for ins in self.instruments:
            lines.append(
                f"  Slot {ins.slot:02d}  '{ins.name:<20s}'  voice=0x{ins.voice:02x}"
            )
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
# Channel allocator
# ─────────────────────────────────────────────────────────────────

class ChannelAllocator:
    """
    Greedy polyphony allocator.

    Assigns each Note to one of 8 TMU channels so that no two notes
    overlap on the same channel.  When all channels are busy, the
    channel that will be free soonest is stolen (oldest note is cut).

    You can subclass this and replace the assign() method to implement
    smarter allocation (e.g. keep notes on their original MIDI channel
    where possible, or prefer FM channels over PSG channels).
    """

    def __init__(self, n_channels: int = CHANNELS_PER_ROW) -> None:
        self.n = n_channels
        self._free_at: List[int] = [0] * n_channels  # row when each ch is free

    def assign(self, start_row: int, end_row: int) -> int:
        """
        Reserve a channel for [start_row, end_row) and return its index.
        """
        # First free channel
        for ch in range(self.n):
            if self._free_at[ch] <= start_row:
                self._free_at[ch] = end_row
                return ch
        # All busy — steal the one freeing soonest
        ch = min(range(self.n), key=lambda c: self._free_at[c])
        log.debug(
            "Channel %d stolen at row %d (was busy until %d)",
            ch, start_row, self._free_at[ch],
        )
        self._free_at[ch] = end_row
        return ch


# ─────────────────────────────────────────────────────────────────
# FM Converter
# ─────────────────────────────────────────────────────────────────

class FMConverter:
    """
    Converts a MidiSong to a TmuSong targeting FM (OPLL).

    Usage
    ─────
    converter = FMConverter()
    tmu_song = converter.convert(midi_song)

    After conversion the following attributes are available for
    inspection / logging:
      converter.voice_bank     — OPLL voice assignments
      converter.instrument_bank — TMU instrument slot assignments
    """

    def __init__(self) -> None:
        self.voice_bank      = VoiceBank()
        self.instrument_bank = InstrumentBank(self.voice_bank)

    # ── public ────────────────────────────────────────────────────

    def convert(self, song: MidiSong) -> TmuSong:
        log.info("Converting MidiSong → TmuSong (FM/OPLL target)")

        # Pre-register all programs so instrument order is predictable
        for prog in song.all_programs():
            self.instrument_bank.get_or_add(prog)

        speed     = self._bpm_to_speed(song.initial_bpm)
        n_patterns = math.ceil(song.total_rows / ROWS_PER_PATTERN)
        n_patterns = max(1, min(255, n_patterns))

        log.info(
            "BPM=%.1f → speed=%d | %d total rows → %d pattern(s)",
            song.initial_bpm, speed, song.total_rows, n_patterns,
        )

        grid = self._build_grid(song, n_patterns * ROWS_PER_PATTERN)
        patterns = self._grid_to_patterns(grid, n_patterns)

        tmu = TmuSong(
            title=song.meta.title[:32],
            author=song.meta.author[:32],
            speed=speed,
            period=PERIOD_MODERN,
            chipset=CHIPSET_FM,
            chan_setup=CHANSETUP_DEFAULT,
            order=list(range(n_patterns)),
            loop_pos=ORDER_END,
            instruments=self.instrument_bank.instruments,
            fm_voices=[v.data for v in self.voice_bank.voices],
            patterns=patterns,
        )

        log.info("Conversion complete: %d instruments, %d voices, %d patterns",
                 len(tmu.instruments), len(tmu.fm_voices), len(tmu.patterns))
        return tmu

    # ── internals ─────────────────────────────────────────────────

    def _bpm_to_speed(self, bpm: float) -> int:
        """
        Map BPM to TMU playback speed.
        TriloTracker default is speed=6 at 120 BPM on a 50 Hz MSX.
        Relationship is linear: speed = round(6 × 120 / bpm).
        Clamped to 1-15.
        """
        speed = round(6.0 * 120.0 / max(1.0, bpm))
        return max(1, min(15, speed))

    def _build_grid(
        self,
        song: MidiSong,
        total_rows: int,
    ) -> List[List[TmuCell]]:
        """
        Build a grid[channel][row] = TmuCell.

        Each melodic track from the MidiSong competes for the 8
        available TMU channels via the greedy ChannelAllocator.
        Notes from drum tracks are silently skipped.
        """
        allocator = ChannelAllocator(CHANNELS_PER_ROW)
        grid: List[List[TmuCell]] = [
            [TmuCell() for _ in range(total_rows)]
            for _ in range(CHANNELS_PER_ROW)
        ]

        # Process all notes across all tracks, sorted by start row
        all_notes: List[Tuple[Note, int]] = []   # (note, program)
        for track in song.melodic_tracks:
            for note in track.notes:
                all_notes.append((note, track.program))

        all_notes.sort(key=lambda x: x[0].start_row)
        skipped = 0

        for note, program in all_notes:
            if note.tmu_note == 0:
                skipped += 1
                continue

            ch = allocator.assign(note.start_row, note.end_row)
            row = note.start_row

            if row >= total_rows:
                skipped += 1
                continue

            ins_slot = self.instrument_bank.get_or_add(program)
            vol      = max(1, min(15, note.velocity >> 3))

            cell = grid[ch][row]
            cell.note = note.tmu_note
            cell.ins  = ins_slot
            cell.vol  = vol

            # Place release marker at end_row (if the cell is free)
            if 0 < note.end_row < total_rows:
                release_cell = grid[ch][note.end_row]
                if release_cell.note == 0:
                    release_cell.note = NOTE_RELEASE

        if skipped:
            log.debug("Skipped %d out-of-range notes", skipped)

        return grid

    def _grid_to_patterns(
        self,
        grid: List[List[TmuCell]],
        n_patterns: int,
    ) -> List[TmuPattern]:
        """Convert the flat grid into TmuPattern objects."""
        patterns: List[TmuPattern] = []

        for p in range(n_patterns):
            pat = TmuPattern(index=p)
            base = p * ROWS_PER_PATTERN

            for row_idx in range(ROWS_PER_PATTERN):
                abs_row = base + row_idx
                tmu_row = TmuRow()
                for ch in range(CHANNELS_PER_ROW):
                    if abs_row < len(grid[ch]):
                        tmu_row.channels[ch] = grid[ch][abs_row]
                pat.rows[row_idx] = tmu_row

            patterns.append(pat)

        return patterns
