"""
tmu/compression.py
──────────────────
Pattern RLE compression / decompression matching the TriloTracker
Z80 routines in code/compression2.asm.

Compressor format (output)
──────────────────────────
  For each byte in the 2048-byte flat pattern:
    • Non-zero byte  → written as-is.
    • Run of zeros   → [0x00][count]  where count = 1-255.
      When count reaches 255, flush and continue.
  Terminator: [0x00][0x00]

Decompressor (from compression2.asm)
──────────────────────────────────────
  read byte from compressed stream:
    ≠ 0  → copy to output
    = 0  → read next byte:
             = 0  → DONE (terminator)
             ≠ 0  → write that many zero bytes to output

This module provides both compress_pattern and decompress_pattern
so that round-trip tests can be written easily.
"""

from __future__ import annotations

from typing import List


def compress_pattern(flat: List[int]) -> bytes:
    """
    Compress a 2048-byte flat pattern list into the TMU RLE format.

    Parameters
    ──────────
    flat : list of 2048 ints (0-255)

    Returns
    ───────
    bytes — compressed data including the 0x00 0x00 terminator.
    """
    out = bytearray()
    i = 0
    n = len(flat)

    while i < n:
        val = flat[i]
        if val != 0:
            out.append(val)
            i += 1
        else:
            # Count the run of zeros (max 255 per RLE token)
            count = 0
            while i < n and flat[i] == 0 and count < 255:
                count += 1
                i += 1
            out.append(0x00)
            out.append(count)

    # Terminator: 0x00 0x00
    out.append(0x00)
    out.append(0x00)
    return bytes(out)


def decompress_pattern(data: bytes) -> List[int]:
    """
    Decompress TMU RLE data back to a 2048-byte flat list.
    Matches the Z80 decompress_pattern routine exactly.

    Raises ValueError if data appears truncated.
    """
    out: List[int] = []
    s = 0

    while s < len(data):
        val = data[s]
        s += 1

        if val != 0:
            out.append(val)
        else:
            if s >= len(data):
                raise ValueError("Truncated compressed pattern (missing count byte)")
            count = data[s]
            s += 1
            if count == 0:
                # Terminator
                break
            for _ in range(count):
                out.append(0)

    # Pad to 2048 if the pattern ended early
    if len(out) < 2048:
        out.extend([0] * (2048 - len(out)))

    return out[:2048]


def verify_roundtrip(flat: List[int]) -> bool:
    """
    Compress then decompress and check that the result matches.
    Useful in unit tests and debug runs.
    """
    compressed = compress_pattern(flat)
    restored = decompress_pattern(compressed)
    return restored == flat[:2048]
