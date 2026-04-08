"""
tests/test_writer.py
────────────────────
Validates that TmuWriter produces a binary file whose structure
matches exactly what TriloTracker's open_tmufile Z80 routine expects.

Run with:  python -m pytest tests/
"""

import sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from midi2tmu.tmu.model import TmuSong, TmuPattern, TmuRow, TmuCell, TmuInstrument
from midi2tmu.tmu.writer import TmuWriter
from midi2tmu.tmu.constants import (
    TMU_VERSION, CHIPSET_FM, MAX_INSTRUMENTS, MAX_CUSTOM_VOICES,
    EXTRA_COUNT, ITYPE_FM, ROWS_PER_PATTERN,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def minimal_song() -> TmuSong:
    """A valid TmuSong with one empty pattern."""
    pat = TmuPattern(index=0)
    ins = TmuInstrument(slot=1, name="Test", voice=16, restart=0,
                        rows=[bytes([0x00, 0x8F, 0x00, 0x00])])
    return TmuSong(
        title="Test Song",
        author="Tester",
        speed=6,
        chipset=CHIPSET_FM,
        order=[0],
        instruments=[ins],
        fm_voices=[bytes(8)],
        patterns=[pat],
    )


def write_and_read(song: TmuSong) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".tmu", delete=False) as f:
        path = f.name
    TmuWriter().write(song, path)
    return open(path, "rb").read()


def parse_header(data: bytes) -> dict:
    """Parse every section of the TMU binary and return field offsets/values."""
    idx = 0
    r = {}

    # Header byte
    b0 = data[idx]; idx += 1
    r["header_byte"]  = b0
    r["version"]      = b0 & 0x0F
    r["chipset_nibble"] = (b0 >> 4) & 0x07
    r["chan_setup_bit7"] = (b0 >> 7) & 1

    # Extra bytes
    r["extra_count"]  = data[idx]; idx += 1
    r["period"]       = data[idx]; idx += 1
    r["itypes"]       = list(data[idx:idx+32]); idx += 32
    r["drum_type"]    = data[idx]; idx += 1

    # Name + author
    r["name"]   = data[idx:idx+32].rstrip(b" \x00"); idx += 32
    r["author"] = data[idx:idx+32].rstrip(b" \x00"); idx += 32

    # Speed / order
    r["speed"]     = data[idx]; idx += 1
    r["loop_pos"]  = data[idx]; idx += 1
    r["order_len"] = data[idx]; idx += 1
    r["order"]     = list(data[idx:idx+r["order_len"]]); idx += r["order_len"]

    # Instrument names
    r["ins_names_start"] = idx
    idx += MAX_INSTRUMENTS * 16

    # Instrument macros
    r["ins_macros_start"] = idx
    for i in range(MAX_INSTRUMENTS):
        l = data[idx]; idx += 1 + 1 + 1 + l * 4   # length + restart + voice + rows
    r["ins_macros_end"] = idx

    # FM voices
    r["voices_start"] = idx
    idx += MAX_CUSTOM_VOICES * 8
    r["voices_end"] = idx

    # Drum names + macros
    r["drums_start"] = idx
    idx += 19 * 16 + 19  # 19 names + 19 zero-length macros
    r["drums_end"] = idx

    # Patterns
    r["patterns_start"] = idx
    pats = []
    while idx < len(data):
        pnum = data[idx]
        if pnum == 0xFF:
            idx += 1
            break
        idx += 1
        plen = data[idx] + data[idx+1]*256; idx += 2
        pats.append((pnum, plen))
        idx += plen
    r["patterns"] = pats
    r["end"] = idx
    r["total_size"] = len(data)
    return r


# ── tests ─────────────────────────────────────────────────────────────────────

def test_header_byte():
    """Version nibble must be 11, chipset nibble must be 1 (FM)."""
    data = write_and_read(minimal_song())
    r = parse_header(data)
    assert r["version"] == TMU_VERSION == 11, f"version={r['version']}"
    assert r["chipset_nibble"] == 1, f"chipset_nibble={r['chipset_nibble']}"


def test_extra_bytes_count():
    """extra_count must be 34 so the loader reads period + instrument_types + drum_type."""
    data = write_and_read(minimal_song())
    r = parse_header(data)
    assert r["extra_count"] == EXTRA_COUNT == 34


def test_instrument_types_all_fm():
    """All 32 instrument_type bytes must be 3 (ITYPE_FM)."""
    data = write_and_read(minimal_song())
    r = parse_header(data)
    assert all(v == ITYPE_FM for v in r["itypes"]), f"itypes={r['itypes']}"


def test_name_fields():
    """Song name and author are correctly space-padded."""
    data = write_and_read(minimal_song())
    r = parse_header(data)
    assert r["name"] == b"Test Song"
    assert r["author"] == b"Tester"


def test_order():
    """Order list length and contents match the song."""
    data = write_and_read(minimal_song())
    r = parse_header(data)
    assert r["order_len"] == 1
    assert r["order"] == [0]


def test_instrument_restart_never_0xff():
    """No instrument macro restart byte may be 0xFF (causes tracker hang)."""
    data = write_and_read(minimal_song())
    idx = parse_header(data)["ins_macros_start"]
    for slot in range(MAX_INSTRUMENTS):
        l       = data[idx]; idx += 1
        restart = data[idx]; idx += 1
        voice   = data[idx]; idx += 1
        idx    += l * 4
        assert restart != 0xFF, f"Slot {slot+1} restart=0xFF (forbidden)"
        assert restart < l, f"Slot {slot+1} restart={restart} >= length={l}"


def test_pattern_terminator():
    """Pattern section must end with 0xFF."""
    data = write_and_read(minimal_song())
    r = parse_header(data)
    # The byte just before r["end"] is the 0xFF terminator
    assert data[r["end"] - 1] == 0xFF, "Missing end-of-patterns 0xFF marker"


def test_no_trailing_bytes():
    """Writer must consume exactly the bytes it declares — no garbage at end."""
    data = write_and_read(minimal_song())
    r = parse_header(data)
    assert r["end"] == r["total_size"], (
        f"Trailing bytes: end={r['end']} totalsize={r['total_size']}"
    )


def test_round_trip_pattern_count():
    """Number of pattern records written matches the order list length."""
    song = minimal_song()
    song.patterns = [TmuPattern(index=i) for i in range(3)]
    song.order    = [0, 1, 2]
    data = write_and_read(song)
    r = parse_header(data)
    assert len(r["patterns"]) == 3


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
