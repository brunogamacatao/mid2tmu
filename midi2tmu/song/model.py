"""
song/model.py
─────────────
High-level, chip-agnostic representation of a song loaded from MIDI.
This is the intermediate representation that sits between the MIDI
parser and any chip-specific converter.

Layers
──────
  MidiSong
    └─ tracks[]        one per unique (midi_channel, program) combination
         └─ notes[]    each note with timing in *rows* (quantised)
    └─ tempo_map       list of (row, bpm) pairs for tempo changes
    └─ meta            free-form metadata (name, author, source file…)

Rows are the universal time unit throughout the project.
The quantisation resolution is set at parse time (rows_per_beat).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple


# ─────────────────────────────────────────────────────────────────
# Note
# ─────────────────────────────────────────────────────────────────

@dataclass
class Note:
    """A single note in absolute row time."""

    # Timing (in rows, quantised)
    start_row: int
    end_row:   int          # exclusive — the row where the note ends / releases

    # Pitch & dynamics
    midi_note:  int         # raw MIDI note number (0-127)
    velocity:   int         # 0-127

    @property
    def duration_rows(self) -> int:
        return max(0, self.end_row - self.start_row)

    @property
    def tmu_note(self) -> int:
        """
        Convert MIDI note to TMU note number.
        MIDI 24 (C1) → TMU 1.  MIDI 119 (B8) → TMU 96.
        Returns 0 if out of the TMU melodic range.
        """
        tmu = self.midi_note - 23
        return tmu if 1 <= tmu <= 96 else 0

    def __repr__(self) -> str:
        names = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
        n = self.midi_note
        return (f"Note({names[n%12]}{n//12-1} "
                f"rows {self.start_row}–{self.end_row} vel={self.velocity})")


# ─────────────────────────────────────────────────────────────────
# Track
# ─────────────────────────────────────────────────────────────────

@dataclass
class Track:
    """
    One logical voice in the song — corresponds to a unique
    (midi_channel, program) pair in the source MIDI.
    """
    midi_channel: int
    program:      int          # GM program number 0-127
    name:         str = ""
    notes:        List[Note] = field(default_factory=list)

    # ── convenience ──────────────────────────────────────────────

    @property
    def is_drum_channel(self) -> bool:
        return self.midi_channel == 9

    @property
    def gm_family(self) -> str:
        from midi2tmu.fm.voices import gm_family_name
        return gm_family_name(self.program)

    def notes_sorted(self) -> List[Note]:
        return sorted(self.notes, key=lambda n: n.start_row)

    def last_row(self) -> int:
        if not self.notes:
            return 0
        return max(n.end_row for n in self.notes)

    def __repr__(self) -> str:
        return (f"Track(ch={self.midi_channel} prog={self.program} "
                f"'{self.name}' {len(self.notes)} notes)")


# ─────────────────────────────────────────────────────────────────
# Tempo map
# ─────────────────────────────────────────────────────────────────

@dataclass
class TempoChange:
    row: int
    bpm: float


# ─────────────────────────────────────────────────────────────────
# Song metadata
# ─────────────────────────────────────────────────────────────────

@dataclass
class SongMeta:
    title:        str = "Untitled"
    author:       str = "Unknown"
    source_file:  str = ""
    rows_per_beat: int = 4      # quantisation used during parse
    ticks_per_beat: int = 480   # original MIDI resolution (informational)


# ─────────────────────────────────────────────────────────────────
# MidiSong  — top-level container
# ─────────────────────────────────────────────────────────────────

class MidiSong:
    """
    Chip-agnostic representation of a parsed MIDI file.

    This object is the output of MidiParser and the input of any
    chip converter (FMConverter, SCCConverter, …).
    It contains no knowledge of TMU, OPLL, or SCC internals.
    """

    def __init__(self, meta: Optional[SongMeta] = None):
        self.meta: SongMeta = meta or SongMeta()
        self.tracks: List[Track] = []
        self.tempo_map: List[TempoChange] = []

    # ── helpers ──────────────────────────────────────────────────

    @property
    def initial_bpm(self) -> float:
        if self.tempo_map:
            return self.tempo_map[0].bpm
        return 120.0

    @property
    def melodic_tracks(self) -> List[Track]:
        """All tracks except the drum channel."""
        return [t for t in self.tracks if not t.is_drum_channel]

    @property
    def total_rows(self) -> int:
        if not self.tracks:
            return 0
        return max((t.last_row() for t in self.tracks), default=0) + 8

    def get_or_create_track(self, midi_channel: int, program: int) -> Track:
        for t in self.tracks:
            if t.midi_channel == midi_channel and t.program == program:
                return t
        t = Track(midi_channel=midi_channel, program=program)
        self.tracks.append(t)
        return t

    def all_programs(self) -> List[int]:
        """Unique GM programs used by melodic tracks."""
        return sorted({t.program for t in self.melodic_tracks})

    def summary(self) -> str:
        lines = [
            f"Song : '{self.meta.title}' by '{self.meta.author}'",
            f"Source : {self.meta.source_file}",
            f"BPM  : {self.initial_bpm:.1f}",
            f"Rows : {self.total_rows}  ({self.meta.rows_per_beat} rows/beat)",
            f"Tracks : {len(self.tracks)}  ({len(self.melodic_tracks)} melodic)",
        ]
        for t in self.tracks:
            tag = "[DRUM]" if t.is_drum_channel else f"prog={t.program:3d}"
            lines.append(f"  ch{t.midi_channel:2d} {tag} '{t.gm_family}'  {len(t.notes)} notes")
        return "\n".join(lines)
