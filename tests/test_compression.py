"""
tests/test_compression.py
─────────────────────────
Round-trip and edge-case tests for the TMU pattern RLE codec.
Run with:  python -m pytest tests/
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from midi2tmu.tmu.compression import compress_pattern, decompress_pattern, verify_roundtrip
from midi2tmu.tmu.constants import PATTERN_SIZE


# ── helpers ───────────────────────────────────────────────────────────────────

def make_flat(overrides: dict = None) -> list:
    """All-zero 2048-byte pattern with optional byte overrides."""
    flat = [0] * PATTERN_SIZE
    for idx, val in (overrides or {}).items():
        flat[idx] = val
    return flat


# ── tests ─────────────────────────────────────────────────────────────────────

def test_all_zeros():
    """All-zero pattern compresses to minimum bytes and round-trips."""
    flat = [0] * PATTERN_SIZE
    compressed = compress_pattern(flat)
    # Should be: [0x00][255] [0x00][255] ... [0x00][rem] [0x00][0x00]
    assert compressed[-2:] == bytes([0x00, 0x00]), "Missing terminator"
    assert verify_roundtrip(flat)


def test_all_nonzero():
    """All non-zero pattern: every byte written literally."""
    flat = [0x42] * PATTERN_SIZE
    compressed = compress_pattern(flat)
    # Each byte is written as-is, then terminator
    assert len(compressed) == PATTERN_SIZE + 2
    assert verify_roundtrip(flat)


def test_single_note():
    """Single note at row 0, channel 0."""
    flat = make_flat({0: 0x10, 1: 0x01, 2: 0xC0, 3: 0x00})
    assert verify_roundtrip(flat)


def test_release_marker():
    """Release marker (0x61 = 97) round-trips correctly."""
    flat = make_flat({32: 97})   # channel 0, row 1
    assert verify_roundtrip(flat)


def test_zero_run_boundary():
    """Zero run that crosses the 255-byte boundary."""
    flat = [0] * PATTERN_SIZE
    flat[0] = 1
    flat[500] = 2    # gap of 499 zeros — crosses 255 boundary twice
    flat[1000] = 3
    assert verify_roundtrip(flat)


def test_max_zero_run():
    """Exactly 255 consecutive zeros followed by more zeros."""
    flat = [0] * PATTERN_SIZE
    flat[0]   = 1
    flat[256] = 2   # 255 zeros between index 1 and 256
    assert verify_roundtrip(flat)


def test_terminator_not_in_data():
    """Compressed stream must not contain 0x00 0x00 before the terminator."""
    flat = make_flat({100: 0x30, 200: 0x40})
    compressed = compress_pattern(flat)
    # Find 0x00 0x00 — it must only appear at the end
    for i in range(len(compressed) - 2):
        assert not (compressed[i] == 0 and compressed[i+1] == 0), (
            f"Premature 0x00 0x00 at offset {i}"
        )
    assert compressed[-2:] == bytes([0x00, 0x00])


def test_decompressor_pads_to_2048():
    """Decompressor always returns exactly 2048 bytes."""
    flat = make_flat({10: 5})
    compressed = compress_pattern(flat)
    restored = decompress_pattern(compressed)
    assert len(restored) == PATTERN_SIZE


def test_realistic_pattern():
    """Realistic-ish pattern with 8 channels of notes."""
    from midi2tmu.tmu.constants import PATTERN_LINE_SIZE
    flat = [0] * PATTERN_SIZE
    for row in range(16):
        for ch in range(4):
            base = ch * 4 + row * PATTERN_LINE_SIZE
            flat[base]     = (row % 12) + 1   # note
            flat[base + 1] = ch + 1            # instrument
            flat[base + 2] = 0xC0              # vol=12, cmd=0
    assert verify_roundtrip(flat)


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
