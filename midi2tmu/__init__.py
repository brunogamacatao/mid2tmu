"""
midi2tmu
────────
MIDI to TriloTracker TMU converter.

Quick start
───────────
    from midi2tmu.midi.parser import MidiParser
    from midi2tmu.tmu.converter import FMConverter
    from midi2tmu.tmu.writer import TmuWriter

    song    = MidiParser(rows_per_beat=4).parse("my_song.mid")
    tmu     = FMConverter().convert(song)
    TmuWriter().write(tmu, "my_song.tmu")
"""
