"""
cli/main.py
───────────
Command-line interface for midi2tmu.

Usage examples
──────────────
  python -m midi2tmu song.mid
  python -m midi2tmu song.mid out.tmu
  python -m midi2tmu song.mid out.tmu --rows-per-beat 2
  python -m midi2tmu song.mid --verbose
  python -m midi2tmu song.mid --debug
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from midi2tmu.midi.parser import MidiParser
from midi2tmu.tmu.converter import FMConverter
from midi2tmu.tmu.writer import TmuWriter


# ─────────────────────────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool, debug: bool) -> None:
    if debug:
        level = logging.DEBUG
        fmt = "%(levelname)-8s %(name)s: %(message)s"
    elif verbose:
        level = logging.INFO
        fmt = "%(levelname)-8s %(message)s"
    else:
        level = logging.WARNING
        fmt = "%(message)s"

    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)


# ─────────────────────────────────────────────────────────────────
# Argument parser
# ─────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="midi2tmu",
        description="Convert a MIDI file to a TriloTracker TMU file (FM/OPLL target).",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "input",
        metavar="INPUT.MID",
        help="Source MIDI file.",
    )
    p.add_argument(
        "output",
        metavar="OUTPUT.TMU",
        nargs="?",
        default=None,
        help="Output TMU file (default: same name as input with .tmu extension).",
    )
    p.add_argument(
        "--rows-per-beat", "-r",
        type=int,
        default=4,
        metavar="N",
        help=(
            "Quantisation granularity:\n"
            "  2 = 8th notes  (coarse)\n"
            "  4 = 16th notes (default)\n"
            "  8 = 32nd notes (fine)\n"
        ),
    )
    p.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print progress and summary information.",
    )
    p.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Print detailed debug output (implies --verbose).",
    )
    return p


# ─────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────

def _print_report(
    midi_path: Path,
    tmu_path: Path,
    midi_song,
    tmu_song,
    converter: FMConverter,
) -> None:
    sep = "─" * 60
    print(sep)
    print("  midi2tmu — Conversion Report")
    print(sep)
    print(f"  MIDI input  : {midi_path}")
    print(f"  TMU output  : {tmu_path}")
    print(f"  BPM         : {midi_song.initial_bpm:.1f}  →  TMU speed {tmu_song.speed}")
    print(f"  Rows        : {midi_song.total_rows}  ({len(tmu_song.patterns)} pattern(s))")
    print(f"  Order       : {tmu_song.order}")
    print()
    print(f"  FM Voices ({len(tmu_song.fm_voices)} generated):")
    print(converter.voice_bank.summary() or "    (none)")
    print()
    print(f"  Instruments ({len(tmu_song.instruments)} slots used):")
    print(converter.instrument_bank.summary() or "    (none)")
    print()
    print("  MIDI Tracks:")
    for t in midi_song.tracks:
        tag = "[DRUM] " if t.is_drum_channel else f"prog={t.program:3d}"
        print(f"    ch{t.midi_channel:2d}  {tag}  {t.gm_family:<12s}  {len(t.notes)} notes")
    print(sep)


# ─────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    _setup_logging(verbose=args.verbose, debug=args.debug)
    log = logging.getLogger(__name__)

    input_path  = Path(args.input)
    output_path = Path(args.output) if args.output else input_path.with_suffix(".tmu")

    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        return 1

    # ── 1. Parse MIDI ─────────────────────────────────────────────
    log.info("[1/3] Parsing MIDI: %s", input_path)
    midi_parser = MidiParser(rows_per_beat=args.rows_per_beat)
    try:
        midi_song = midi_parser.parse(input_path)
    except Exception as exc:
        print(f"Error reading MIDI file: {exc}", file=sys.stderr)
        log.debug("Parse error", exc_info=True)
        return 1

    log.info(
        "      %d melodic tracks, %d total rows, BPM=%.1f",
        len(midi_song.melodic_tracks), midi_song.total_rows, midi_song.initial_bpm,
    )

    # ── 2. Convert → TmuSong ──────────────────────────────────────
    log.info("[2/3] Converting to TMU (FM/OPLL)…")
    converter = FMConverter()
    try:
        tmu_song = converter.convert(midi_song)
    except Exception as exc:
        print(f"Error during conversion: {exc}", file=sys.stderr)
        log.debug("Conversion error", exc_info=True)
        return 1

    # ── 3. Write .tmu ─────────────────────────────────────────────
    log.info("[3/3] Writing: %s", output_path)
    writer = TmuWriter()
    try:
        writer.write(tmu_song, output_path)
    except Exception as exc:
        print(f"Error writing TMU file: {exc}", file=sys.stderr)
        log.debug("Write error", exc_info=True)
        return 1

    # ── Report ────────────────────────────────────────────────────
    if args.verbose or args.debug:
        _print_report(input_path, output_path, midi_song, tmu_song, converter)
    else:
        print(f"Done: {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
