"""
midi/parser.py
──────────────
Converts a MIDI file into a chip-agnostic MidiSong object.

Responsibilities
────────────────
  • Flatten all MIDI tracks into a single timeline (absolute ticks).
  • Resolve program-change messages per channel.
  • Build Note objects with quantised row timing.
  • Build the tempo map (supports multiple tempo changes).
  • Skip MIDI channel 9 (drums) — handled separately in the future.

Usage
─────
  from midi2tmu.midi.parser import MidiParser
  song = MidiParser(rows_per_beat=4).parse("song.mid")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import mido

from midi2tmu.song.model import MidiSong, SongMeta, Track, Note, TempoChange

log = logging.getLogger(__name__)


class MidiParser:
    """
    Parses a MIDI file and produces a MidiSong.

    Parameters
    ──────────
    rows_per_beat : int
        Quantisation granularity.
        4  = 16th notes (default, good for most songs)
        2  = 8th notes  (coarser, fewer rows, less pattern RAM)
        8  = 32nd notes (finer, more rows)
    """

    DRUM_CHANNEL = 9

    def __init__(self, rows_per_beat: int = 4):
        if rows_per_beat < 1:
            raise ValueError("rows_per_beat must be >= 1")
        self.rows_per_beat = rows_per_beat

    # ── public ────────────────────────────────────────────────────

    def parse(self, path: str | Path) -> MidiSong:
        path = Path(path)
        log.info("Parsing MIDI file: %s", path)

        mid = mido.MidiFile(str(path))
        tpb = mid.ticks_per_beat
        log.debug("ticks_per_beat=%d, type=%d, tracks=%d", tpb, mid.type, len(mid.tracks))

        msgs = self._flatten(mid)
        tempo_map = self._build_tempo_map(msgs, tpb)
        log.info("Tempo map: %s", [(tc.row, f"{tc.bpm:.1f}") for tc in tempo_map])

        initial_bpm = tempo_map[0].bpm if tempo_map else 120.0
        ticks_per_row = tpb / self.rows_per_beat

        meta = SongMeta(
            title=path.stem,
            author="",
            source_file=str(path),
            rows_per_beat=self.rows_per_beat,
            ticks_per_beat=tpb,
        )
        song = MidiSong(meta=meta)
        song.tempo_map = tempo_map

        self._build_tracks(msgs, song, ticks_per_row)

        log.info(
            "Parsed: %d melodic tracks, %d total rows, BPM=%.1f",
            len(song.melodic_tracks), song.total_rows, initial_bpm,
        )
        return song

    # ── internals ─────────────────────────────────────────────────

    def _flatten(self, mid: mido.MidiFile) -> List[Tuple[int, mido.Message]]:
        """
        Flatten all MIDI tracks to a single list of (abs_tick, msg),
        sorted by abs_tick.
        """
        events: List[Tuple[int, mido.Message]] = []
        for track in mid.tracks:
            abs_tick = 0
            for msg in track:
                abs_tick += msg.time
                events.append((abs_tick, msg))
        events.sort(key=lambda e: e[0])
        log.debug("Flattened %d MIDI events", len(events))
        return events

    def _build_tempo_map(
        self,
        msgs: List[Tuple[int, mido.Message]],
        tpb: int,
    ) -> List[TempoChange]:
        """
        Build a list of TempoChange objects (row, bpm).
        Multiple tempo changes within a song are preserved.
        """
        changes: List[TempoChange] = []
        ticks_per_row = tpb / self.rows_per_beat

        # Default 120 BPM if no set_tempo found
        found_any = False
        for abs_tick, msg in msgs:
            if msg.type == "set_tempo":
                row = int(abs_tick / ticks_per_row)
                bpm = round(60_000_000 / msg.tempo, 2)
                changes.append(TempoChange(row=row, bpm=bpm))
                log.debug("Tempo change at row %d → %.1f BPM (tick %d)", row, bpm, abs_tick)
                found_any = True

        if not found_any:
            log.debug("No tempo events found; defaulting to 120 BPM")
            changes.append(TempoChange(row=0, bpm=120.0))

        return changes

    def _build_tracks(
        self,
        msgs: List[Tuple[int, mido.Message]],
        song: MidiSong,
        ticks_per_row: float,
    ) -> None:
        """
        Walk the event list and build Track/Note objects.
        Maintains an open-notes dict to pair note_on with note_off.
        """
        programs: Dict[int, int] = {ch: 0 for ch in range(16)}

        # (channel, midi_note) → (start_row, velocity)
        open_notes: Dict[Tuple[int, int], Tuple[int, int]] = {}

        max_tick = 0

        for abs_tick, msg in msgs:
            max_tick = max(max_tick, abs_tick)
            row = int(abs_tick / ticks_per_row)

            if msg.type == "program_change":
                old = programs[msg.channel]
                programs[msg.channel] = msg.program
                log.debug(
                    "ch%d program_change %d → %d at row %d",
                    msg.channel, old, msg.program, row,
                )

            elif msg.type == "note_on" and msg.velocity > 0:
                key = (msg.channel, msg.note)
                if key in open_notes:
                    # Implicitly close the previous note
                    self._close_note(key, row, programs, open_notes, song)
                open_notes[key] = (row, msg.velocity)

            elif msg.type == "note_off" or (
                msg.type == "note_on" and msg.velocity == 0
            ):
                key = (msg.channel, msg.note)
                if key in open_notes:
                    self._close_note(key, row, programs, open_notes, song)

        # Close any notes still open at end-of-file
        tail_row = int(max_tick / ticks_per_row) + 4
        for key in list(open_notes.keys()):
            self._close_note(key, tail_row, programs, open_notes, song)

    def _close_note(
        self,
        key: Tuple[int, int],
        end_row: int,
        programs: Dict[int, int],
        open_notes: Dict[Tuple[int, int], Tuple[int, int]],
        song: MidiSong,
    ) -> None:
        ch, midi_note = key
        start_row, velocity = open_notes.pop(key)

        # Skip drum channel
        if ch == self.DRUM_CHANNEL:
            return

        note = Note(
            start_row=start_row,
            end_row=end_row,
            midi_note=midi_note,
            velocity=velocity,
        )

        if note.tmu_note == 0:
            log.debug(
                "Note %d on ch%d is out of TMU range — skipped", midi_note, ch
            )
            return

        track = song.get_or_create_track(ch, programs[ch])
        track.notes.append(note)
        log.debug("  %s → %s", key, note)
