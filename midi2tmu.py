#!/usr/bin/env python3
"""
midi2tmu.py — MIDI to Trilo Tracker TMU converter (Proof of Concept)
Targets MSXGL / Trilo Player (FM / OPLL mode)

Architecture:
  MIDI → NoteEvents (absolute ticks) → QuantizedRows → 8-channel assignment
  → TMU binary (load_tmu-compatible) → .tmu file

FM voice generation:
  Each MIDI program is mapped to a GM family group, and a synthetic
  OPLL-style 8-byte voice patch is generated for it.
"""

import sys
import struct
import math
import mido
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

TMU_VERSION     = 0x0C          # v12 (>= 11 enables extra bytes / period)
TMU_TYPE_SCC    = 0             # type_nibble=0
TMU_TYPE_FM     = 1             # type_nibble=1
TMU_TYPE_SMS    = 2             # type_nibble=2
TMU_CHANSETUP   = 0x00          # default channel setup
TMU_PERIOD      = 0x00          # 0 = modern tuning table

ROWS_PER_PAT    = 64
MAX_CHANNELS    = 8
MAX_INSTRUMENTS = 31
MAX_VOICES      = 16            # custom FM voices

# GM program → (family_name, attack, decay, sustain, release, brightness)
# These drive the synthetic OPLL voice builder.
GM_FAMILIES = {
    # Piano
    range(0,   8):  ("piano",       12,  8, 10,  6, 0x40),
    range(8,  16):  ("chromperc",   14,  5,  8,  4, 0x20),
    range(16, 24):  ("organ",        4,  0, 12,  2, 0x20),
    range(24, 32):  ("guitar",      13,  7,  5,  5, 0x10),
    range(32, 40):  ("bass",        14,  9,  4,  4, 0x00),
    range(40, 48):  ("strings",      6,  4, 10,  6, 0x10),
    range(48, 56):  ("ensemble",     5,  3, 11,  5, 0x10),
    range(56, 64):  ("brass",        9,  6,  8,  5, 0x20),
    range(64, 72):  ("reed",         7,  5, 10,  4, 0x20),
    range(72, 80):  ("pipe",         6,  2, 12,  3, 0x10),
    range(80, 88):  ("synthlead",   10,  8,  8,  6, 0x30),
    range(88, 96):  ("synthpad",     4,  2, 12,  4, 0x10),
    range(96,104):  ("synthfx",      6,  4,  9,  5, 0x20),
    range(104,112): ("ethnic",      12,  8,  7,  5, 0x10),
    range(112,120): ("percussive",  14, 10,  3,  4, 0x00),
    range(120,128): ("soundfx",     15, 12,  0,  2, 0x00),
}

def gm_family(program: int):
    for r, val in GM_FAMILIES.items():
        if program in r:
            return val
    return ("unknown", 10, 8, 8, 6, 0x20)

def build_fm_voice(program: int) -> bytes:
    """
    Build a synthetic 8-byte OPLL custom voice from GM program number.
    OPLL voice layout (8 bytes):
      [0] AM/VIB/EG/KSR/MULT  (modulator)
      [1] AM/VIB/EG/KSR/MULT  (carrier)
      [2] KSL/TL               (modulator)
      [3] KSL/Total Level      (carrier) -- KSL only, TL forced 0
      [4] AR/DR                (modulator attack/decay)
      [5] AR/DR                (carrier)
      [6] SL/RR                (modulator sustain/release)
      [7] SL/RR                (carrier)
    """
    name, atk, dec, sus, rel, brightness = gm_family(program)

    # modulator settings vary by family brightness
    mod_mult  = 2
    mod_tl    = min(63, 20 + (brightness >> 2))
    mod_ar    = min(15, atk + 2)
    mod_dr    = min(15, dec + 2)
    mod_sl    = min(15, 15 - sus)
    mod_rr    = min(15, rel + 1)
    mod_flags = 0x20 if brightness >= 0x20 else 0x00   # EG sustain flag

    car_mult  = 1
    car_tl    = 0
    car_ar    = min(15, atk)
    car_dr    = min(15, dec)
    car_sl    = min(15, 15 - sus)
    car_rr    = min(15, rel)
    car_flags = 0x20   # always sustained carrier

    b0 = mod_flags | (mod_mult & 0x0F)
    b1 = car_flags | (car_mult & 0x0F)
    b2 = (mod_tl & 0x3F)
    b3 = (car_tl & 0x3F)
    b4 = ((mod_ar & 0xF) << 4) | (mod_dr & 0xF)
    b5 = ((car_ar & 0xF) << 4) | (car_dr & 0xF)
    b6 = ((mod_sl & 0xF) << 4) | (mod_rr & 0xF)
    b7 = ((car_sl & 0xF) << 4) | (car_rr & 0xF)

    return bytes([b0, b1, b2, b3, b4, b5, b6, b7])

# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class NoteEvent:
    start_row:  int
    end_row:    int
    tmu_note:   int          # 1-96
    velocity:   int          # 0-127
    program:    int          # 0-127 GM program
    midi_ch:    int

@dataclass
class TrackRow:
    note:   int = 0
    ins:    int = 0
    vol:    int = 0
    cmd:    int = 0
    par:    int = 0

# ──────────────────────────────────────────────────────────────────────────────
# MIDI parsing
# ──────────────────────────────────────────────────────────────────────────────

def midi_note_to_tmu(midi_note: int) -> int:
    """
    MIDI note 24 (C1) → TMU note 1
    MIDI note 120 (C9) → TMU note 97 (max melodic = 96)
    """
    tmu = midi_note - 23
    if 1 <= tmu <= 96:
        return tmu
    return 0

def parse_midi(path: str, rows_per_beat: int = 4):
    """
    Returns:
      note_events: list of NoteEvent (absolute rows)
      total_rows:  int
      programs:    dict midi_ch → program number
      bpm:         float
    """
    mid = mido.MidiFile(path)
    tpb = mid.ticks_per_beat

    # Flatten all tracks with absolute tick time
    msgs = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            msgs.append((abs_tick, msg))
    msgs.sort(key=lambda x: x[0])

    # Find first tempo (default 120 BPM)
    tempo = 500000  # µs per beat
    for _, msg in msgs:
        if msg.type == 'set_tempo':
            tempo = msg.tempo
            break
    bpm = 60_000_000 / tempo

    ticks_per_row = tpb / rows_per_beat

    # Channel programs
    programs: Dict[int, int] = {ch: 0 for ch in range(16)}
    active_notes: Dict[Tuple[int,int], int] = {}   # (ch, note) → start_row
    active_vel:   Dict[Tuple[int,int], int] = {}
    note_events:  List[NoteEvent] = []
    max_row = 0

    for abs_tick, msg in msgs:
        row = int(abs_tick / ticks_per_row)
        max_row = max(max_row, row)

        if msg.type == 'program_change':
            programs[msg.channel] = msg.program

        elif msg.type == 'note_on' and msg.velocity > 0:
            key = (msg.channel, msg.note)
            if key in active_notes:
                # close previous
                start = active_notes.pop(key)
                vel   = active_vel.pop(key)
                tmu_n = midi_note_to_tmu(msg.note)
                if tmu_n and msg.channel != 9:
                    note_events.append(NoteEvent(
                        start_row=start, end_row=row,
                        tmu_note=tmu_n, velocity=vel,
                        program=programs[msg.channel],
                        midi_ch=msg.channel))
            active_notes[(msg.channel, msg.note)] = row
            active_vel[(msg.channel, msg.note)]   = msg.velocity

        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            key = (msg.channel, msg.note)
            if key in active_notes:
                start = active_notes.pop(key)
                vel   = active_vel.pop(key)
                tmu_n = midi_note_to_tmu(msg.note)
                if tmu_n and msg.channel != 9:
                    note_events.append(NoteEvent(
                        start_row=start, end_row=row,
                        tmu_note=tmu_n, velocity=vel,
                        program=programs[msg.channel],
                        midi_ch=msg.channel))

    # Close any still-open notes
    for (ch, note), start in active_notes.items():
        tmu_n = midi_note_to_tmu(note)
        if tmu_n and ch != 9:
            note_events.append(NoteEvent(
                start_row=start, end_row=max_row+4,
                tmu_note=tmu_n, velocity=active_vel.get((ch,note),64),
                program=programs[ch], midi_ch=ch))

    return note_events, max_row + 8, programs, bpm

# ──────────────────────────────────────────────────────────────────────────────
# Voice & instrument management
# ──────────────────────────────────────────────────────────────────────────────

class VoiceBank:
    """Maps GM programs to TMU custom voice slots (max 16)."""
    def __init__(self):
        self.program_to_slot: Dict[int, int] = {}   # program → voice index 0-15
        self.voices: List[bytes] = []                # 8-byte patches

    def get_or_add(self, program: int) -> int:
        if program in self.program_to_slot:
            return self.program_to_slot[program]
        if len(self.voices) >= MAX_VOICES:
            # reuse nearest existing slot
            return self.program_to_slot[next(iter(self.program_to_slot))]
        slot = len(self.voices)
        self.voices.append(build_fm_voice(program))
        self.program_to_slot[program] = slot
        return slot

class InstrumentBank:
    """
    Maps (program) → TMU instrument slot 1-31.
    Each instrument has a 1-row macro: just set volume + voice.
    """
    def __init__(self, voice_bank: VoiceBank):
        self.vbank = voice_bank
        self.program_to_ins: Dict[int, int] = {}    # program → ins index 1-31
        self.instruments: List[dict] = []            # list of instrument dicts

    def get_or_add(self, program: int) -> int:
        if program in self.program_to_ins:
            return self.program_to_ins[program]
        if len(self.instruments) >= MAX_INSTRUMENTS:
            return self.program_to_ins[next(iter(self.program_to_ins))]
        voice_slot = self.vbank.get_or_add(program)
        idx = len(self.instruments) + 1
        name, *_ = gm_family(program)
        self.instruments.append({
            "program": program,
            "voice_slot": voice_slot,    # 0-15 → TMU voice 177+ (custom)
            "name": f"P{program:03d}_{name[:8]}",
        })
        self.program_to_ins[program] = idx
        return idx

# ──────────────────────────────────────────────────────────────────────────────
# Voice allocation across 8 TMU channels
# ──────────────────────────────────────────────────────────────────────────────

class ChannelAllocator:
    """
    Greedy voice allocator. Assigns each NoteEvent to one of MAX_CHANNELS
    TMU channels, avoiding overlaps.
    """
    def __init__(self, n_channels: int = MAX_CHANNELS):
        self.n = n_channels
        self.end_row = [0] * n_channels     # when each channel is free again

    def assign(self, event: NoteEvent) -> int:
        """Returns channel index 0..n-1, or -1 if all busy."""
        # prefer channels already free
        for ch in range(self.n):
            if self.end_row[ch] <= event.start_row:
                self.end_row[ch] = event.end_row
                return ch
        # steal the channel that frees soonest
        ch = min(range(self.n), key=lambda c: self.end_row[c])
        self.end_row[ch] = event.end_row
        return ch

# ──────────────────────────────────────────────────────────────────────────────
# Build track grid
# ──────────────────────────────────────────────────────────────────────────────

def build_track_grid(note_events: List[NoteEvent],
                     ins_bank: InstrumentBank,
                     total_rows: int) -> List[List[TrackRow]]:
    """
    Returns grid[channel][row] = TrackRow
    """
    allocator = ChannelAllocator(MAX_CHANNELS)

    # Sort events by start row for greedy allocation
    sorted_events = sorted(note_events, key=lambda e: e.start_row)

    grid: List[List[TrackRow]] = [
        [TrackRow() for _ in range(total_rows + ROWS_PER_PAT)]
        for _ in range(MAX_CHANNELS)
    ]

    for ev in sorted_events:
        ch = allocator.assign(ev)
        row = ev.start_row
        if row >= len(grid[0]):
            continue

        ins_idx  = ins_bank.get_or_add(ev.program)
        vol      = max(1, min(15, ev.velocity >> 3))

        tr = grid[ch][row]
        tr.note = ev.tmu_note
        tr.ins  = ins_idx
        tr.vol  = vol

        # Insert release one row before end (if room)
        end = ev.end_row
        if end < len(grid[0]) and end > row:
            if grid[ch][end].note == 0:
                grid[ch][end].note = 98   # release (n-1 → 97 in export)

    return grid

# ──────────────────────────────────────────────────────────────────────────────
# Pattern / compression
# ──────────────────────────────────────────────────────────────────────────────

def compress_pattern(pat: List[int]) -> bytes:
    """Reverse of decompress_pattern in the compiler."""
    out = bytearray()
    i = 0
    while i < len(pat):
        val = pat[i]
        if val != 0:
            out.append(val)
            i += 1
        else:
            count = 0
            while i < len(pat) and pat[i] == 0 and count < 255:
                count += 1
                i += 1
            out.append(0x00)
            out.append(count)
    out.append(0x00)
    out.append(0x00)   # terminator
    return bytes(out)

def rows_to_pattern_bytes(channel_rows: List[List[TrackRow]],
                           pat_idx: int) -> bytes:
    """
    Pack 8 channels × 64 rows into the 2048-byte pattern layout then compress.
    Layout: for each row (0-63): 8 channels × 4 bytes
      byte0 = note, byte1 = ins, byte2 = (vol<<4)|cmd, byte3 = par
    That means offset = chan*4 + row*32
    """
    pat = [0] * 2048
    base_row = pat_idx * ROWS_PER_PAT

    for chan in range(MAX_CHANNELS):
        for row in range(ROWS_PER_PAT):
            abs_row = base_row + row
            if abs_row >= len(channel_rows[chan]):
                continue
            tr = channel_rows[chan][abs_row]
            offset = chan * 4 + row * 32
            pat[offset + 0] = tr.note & 0xFF
            pat[offset + 1] = tr.ins  & 0xFF
            pat[offset + 2] = ((tr.vol & 0xF) << 4) | (tr.cmd & 0xF)
            pat[offset + 3] = tr.par  & 0xFF

    return compress_pattern(pat)

# ──────────────────────────────────────────────────────────────────────────────
# TMU binary writer
# ──────────────────────────────────────────────────────────────────────────────

def write_tmu(path: str,
              grid: List[List[TrackRow]],
              ins_bank: InstrumentBank,
              voice_bank: VoiceBank,
              total_rows: int,
              bpm: float,
              song_name: str = "MIDI Convert",
              song_by:   str = "midi2tmu"):

    # Derive song speed from BPM.
    # Trilo default: speed 6 ≈ ~120 BPM at 50Hz. We scale linearly.
    speed = max(1, min(15, round(6 * 120.0 / bpm)))

    n_patterns = math.ceil(total_rows / ROWS_PER_PAT)
    n_patterns = max(1, min(255, n_patterns))

    out = bytearray()

    # ── Header byte ──────────────────────────────────────────────────────────
    # bits: [chansetup][type(3)][version(4)]
    # type=0 FM, version=12
    header_byte = (TMU_VERSION & 0x0F) | (1 << 4) | (TMU_CHANSETUP & 0x80)
    out.append(header_byte)

    # ── Extra bytes (version >= 11) ───────────────────────────────────────────
    # Format: count byte, then period byte
    out.append(0x01)          # extra byte count = 1
    out.append(TMU_PERIOD)    # period = 0 (modern)

    # ── Song name (32 bytes, null-padded) ────────────────────────────────────
    name_bytes = song_name.encode('utf-8')[:32].ljust(32, b'\x00')
    out += name_bytes

    # ── Author (32 bytes) ────────────────────────────────────────────────────
    by_bytes = song_by.encode('utf-8')[:32].ljust(32, b'\x00')
    out += by_bytes

    # ── Speed ────────────────────────────────────────────────────────────────
    out.append(speed)

    # ── Restart position ─────────────────────────────────────────────────────
    out.append(0xFF)          # 0xFF = no loop (end of song)

    # ── Order length ─────────────────────────────────────────────────────────
    out.append(n_patterns & 0xFF)

    # ── Order list ───────────────────────────────────────────────────────────
    for i in range(n_patterns):
        out.append(i & 0xFF)

    # ── 31 instrument names (16 bytes each) ──────────────────────────────────
    for i in range(31):
        if i < len(ins_bank.instruments):
            name = ins_bank.instruments[i]["name"]
        else:
            name = ""
        nb = name.encode('utf-8')[:16].ljust(16, b' ')
        out += nb

    # ── 31 instrument macros ─────────────────────────────────────────────────
    # Each macro: length(1), restart(1), voice(1), then length*4 bytes of rows
    # We use a 1-row macro per instrument:
    #   row = [byte1, byte2, byte3, byte4]
    #   byte2 bit7 = tone on → set it: 0x80
    #   byte2 low nibble = volume (max = 0x0F)
    for i in range(31):
        if i < len(ins_bank.instruments):
            ins = ins_bank.instruments[i]
            vs  = ins["voice_slot"]     # 0-based custom voice
            # TMU voice number: custom voices start at index 177 in song.voices
            # but in instrument data it's stored as 1-byte voice index:
            # 0-15 = hardware OPLL presets, 16+ = custom (stored as slot+16... 
            # Actually looking at load_tmu: ins.voice = v (raw byte),
            # and in export_asm: if voice < 16 → hardware, else custom.
            # So we store slot+16 to signal custom voice:
            voice_byte = vs + 16        # custom voice

            out.append(0x01)            # length = 1 row
            out.append(0xFF)            # restart = none
            out.append(voice_byte)      # voice

            # 1 row × 4 bytes: tone on, max volume, no tone delta, no noise
            # byte1: [N|Nd|Nd|noise5bit] → 0x00 (no noise)
            # byte2: [T|Td|Vd|Vd|vol4] → 0x8F (tone on, base vol, vol=15)
            # byte3: tone low = 0
            # byte4: tone high = 0
            out += bytes([0x00, 0x8F, 0x00, 0x00])
        else:
            # empty instrument
            out.append(0x01)            # length = 1
            out.append(0xFF)            # restart = none
            out.append(0x00)            # voice = 0 (none)
            out += bytes([0x00, 0x8F, 0x00, 0x00])

    # ── 16 custom FM voices (8 bytes each) ───────────────────────────────────
    for i in range(16):
        if i < len(voice_bank.voices):
            out += voice_bank.voices[i]
        else:
            out += bytes(8)

    # ── 19 drum names (16 bytes each) — required by FM type ─────────────────
    drum_names = [
        "Bass Drum", "Snare", "Hi-Hat", "Cymbal", "Tom",
        "Rim Shot", "Cowbell", "Clap", "Tamb", "Conga",
        "Bongo", "Cabasa", "Maracas", "Whistle", "Guiro",
        "Claves", "Agogo", "Triangle", "Open HH"
    ]
    for d in range(19):
        name = drum_names[d] if d < len(drum_names) else f"Drum{d}"
        nb = name.encode('utf-8')[:16].ljust(16, b'\x00')
        out += nb

    # ── 19 drum macros (length=0 each = empty) ───────────────────────────────
    for d in range(19):
        out.append(0x00)    # length = 0 (no rows)
        # no rows to write

    # ── Pattern data ─────────────────────────────────────────────────────────
    for p in range(n_patterns):
        out.append(p & 0xFF)            # pattern number
        pat_bytes = rows_to_pattern_bytes(grid, p)
        pat_len   = len(pat_bytes)
        out.append(pat_len & 0xFF)
        out.append((pat_len >> 8) & 0xFF)
        out += pat_bytes

    # ── End-of-patterns marker ────────────────────────────────────────────────
    out.append(0xFF)

    with open(path, 'wb') as f:
        f.write(out)

    return speed, n_patterns

# ──────────────────────────────────────────────────────────────────────────────
# Stats / report
# ──────────────────────────────────────────────────────────────────────────────

def print_report(midi_path, tmu_path, note_events, ins_bank, voice_bank,
                 total_rows, n_patterns, speed, bpm, programs):
    print()
    print("=" * 60)
    print("  midi2tmu — Conversion Report")
    print("=" * 60)
    print(f"  MIDI input  : {midi_path}")
    print(f"  TMU output  : {tmu_path}")
    print(f"  BPM         : {bpm:.1f}  →  TMU speed {speed}")
    print(f"  Total rows  : {total_rows}  ({n_patterns} pattern(s))")
    print(f"  Note events : {len(note_events)}")
    print()
    print("  FM Voices generated:")
    for prog, slot in voice_bank.program_to_slot.items():
        name, *_ = gm_family(prog)
        patch = voice_bank.voices[slot]
        hex_patch = ' '.join(f'{b:02X}' for b in patch)
        print(f"    Slot {slot:02d}  GM#{prog:03d} ({name:<12s})  [{hex_patch}]")
    print()
    print("  Instruments:")
    for i, ins in enumerate(ins_bank.instruments):
        print(f"    #{i+1:02d}  {ins['name']:<20s}  voice slot {ins['voice_slot']:02d}")
    print()
    print("  MIDI channel → GM program mapping:")
    for ch, prog in sorted(programs.items()):
        if ch != 9:
            name, *_ = gm_family(prog)
            print(f"    MIDI ch {ch:2d}  → program {prog:3d}  ({name})")
    print("=" * 60)
    print()

# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

USAGE = f"""
midi2tmu.py — MIDI to Trilo Tracker TMU converter (PoC)

Usage:
  python3 {sys.argv[0]} <input.mid> [output.tmu] [rows_per_beat]

Arguments:
  input.mid      Source MIDI file
  output.tmu     Output TMU file (default: same name as input)
  rows_per_beat  Quantization (default: 4 = 16th notes)

Examples:
  python3 midi2tmu.py song.mid
  python3 midi2tmu.py song.mid song.tmu 4
  python3 midi2tmu.py song.mid song.tmu 2   # coarser (8th notes)
"""

def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print(USAGE)
        sys.exit(0)

    midi_path    = args[0]
    tmu_path     = args[1] if len(args) > 1 else midi_path.rsplit('.', 1)[0] + '.tmu'
    rows_per_beat = int(args[2]) if len(args) > 2 else 4

    print(f"[1/5] Parsing MIDI: {midi_path}")
    note_events, total_rows, programs, bpm = parse_midi(midi_path, rows_per_beat)
    print(f"      {len(note_events)} note events, BPM≈{bpm:.1f}, {total_rows} rows")

    print(f"[2/5] Building voice and instrument banks...")
    voice_bank = VoiceBank()
    ins_bank   = InstrumentBank(voice_bank)
    # Pre-register all programs seen
    for prog in set(programs.values()):
        ins_bank.get_or_add(prog)

    print(f"      {len(voice_bank.voices)} FM voices, {len(ins_bank.instruments)} instruments")

    print(f"[3/5] Allocating {MAX_CHANNELS} channels and building track grid...")
    grid = build_track_grid(note_events, ins_bank, total_rows)

    print(f"[4/5] Writing TMU binary: {tmu_path}")
    song_name = midi_path.split('/')[-1].replace('.mid', '')[:32]
    speed, n_patterns = write_tmu(
        tmu_path, grid, ins_bank, voice_bank, total_rows, bpm,
        song_name=song_name, song_by="midi2tmu PoC")
    print(f"      {n_patterns} pattern(s) written")

    print(f"[5/5] Done!")
    print_report(midi_path, tmu_path, note_events, ins_bank, voice_bank,
                 total_rows, n_patterns, speed, bpm, programs)

if __name__ == '__main__':
    main()
