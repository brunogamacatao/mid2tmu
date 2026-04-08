"""
tmu/writer.py
─────────────
Serialises a TmuSong object into the binary .tmu format that
TriloTracker's open_tmufile routine can load.

All format knowledge lives here and in tmu/constants.py.
The writer has zero knowledge of MIDI, FM synthesis, or voice
generation — it just writes bytes.

Format reference: code/disk_tmu.asm (save_tmufile / open_tmufile)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from midi2tmu.tmu.constants import (
    TMU_VERSION,
    CHIPSET_FM,
    CHANSETUP_DEFAULT,
    MAX_INSTRUMENTS,
    MAX_CUSTOM_VOICES,
    MAX_DRUMS,
    EXTRA_COUNT,
    ITYPE_FM,
    ORDER_END,
    PERIOD_MODERN,
    VOICE_CUSTOM_BASE,
)
from midi2tmu.tmu.model import TmuSong, TmuInstrument, TmuPattern
from midi2tmu.tmu.compression import compress_pattern

log = logging.getLogger(__name__)


class TmuWriter:
    """
    Writes a TmuSong to disk as a .tmu binary.

    Usage
    ─────
    writer = TmuWriter()
    writer.write(tmu_song, "output.tmu")
    """

    def write(self, song: TmuSong, path: str | Path) -> None:
        path = Path(path)
        log.info("Writing TMU: %s", path)

        data = self._serialise(song)

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

        log.info("Wrote %d bytes to %s", len(data), path)

    # ── serialisation ─────────────────────────────────────────────

    def _serialise(self, song: TmuSong) -> bytes:
        out = bytearray()

        out += self._header(song)
        out += self._extra_bytes(song)
        out += self._name_fields(song)
        out += self._order(song)
        out += self._instrument_names(song)
        out += self._instrument_macros(song)
        out += self._fm_voices(song)
        out += self._drum_section(song)
        out += self._patterns(song)

        return bytes(out)

    # ── sections ──────────────────────────────────────────────────

    def _header(self, song: TmuSong) -> bytearray:
        """
        1 byte:  version(4) | chipset_nibble(3) | chan_setup_bit7(1)

        From disk_tmu.asm save_tmufile:
          ld a,11
          or CHIPSET_CODE       ; e.g. $10 for FM
          ; then OR $80 if chan_setup applies
        """
        byte = (TMU_VERSION & 0x0F) | (song.chipset & 0x70) | (song.chan_setup & 0x80)
        log.debug("Header byte: 0x%02x (version=%d chipset=0x%02x)", byte, TMU_VERSION, song.chipset)
        return bytearray([byte])

    def _extra_bytes(self, song: TmuSong) -> bytearray:
        """
        Extra info block (version >= 11).
        Format that the tracker's save routine writes:
          [count=34][period][instrument_types×32][drum_type]
        The load routine reads 'count' bytes into buffer+1 and picks
        fields at known offsets.  We must match exactly.
        """
        out = bytearray()
        out.append(EXTRA_COUNT)          # = 34  (1+32+1)
        out.append(song.period & 0xFF)   # period / tuning table
        for _ in range(32):
            out.append(ITYPE_FM)         # instrument_types: all FM (=3)
        out.append(0x00)                 # drum_type: 0 = normal
        assert len(out) == EXTRA_COUNT + 1
        log.debug("Extra bytes: count=%d period=%d", EXTRA_COUNT, song.period)
        return out

    def _name_fields(self, song: TmuSong) -> bytearray:
        """Song name + author — 32 bytes each, space-padded."""
        out = bytearray()
        out += _pad_str(song.title,  32)
        out += _pad_str(song.author, 32)
        log.debug("Name: '%s'  Author: '%s'", song.title[:32], song.author[:32])
        return out

    def _order(self, song: TmuSong) -> bytearray:
        """speed(1) + loop_pos(1) + order_len(1) + order_data(n)"""
        order = song.order[:200]   # cap to SONG_SEQSIZE
        out = bytearray()
        out.append(song.speed & 0xFF)
        out.append(song.loop_pos & 0xFF)
        out.append(len(order) & 0xFF)
        for p in order:
            out.append(p & 0xFF)
        log.debug(
            "Order: speed=%d loop=0x%02x len=%d patterns=%s",
            song.speed, song.loop_pos, len(order), order,
        )
        return out

    def _instrument_names(self, song: TmuSong) -> bytearray:
        """31 × 16-byte space-padded names (slots 1-31)."""
        out = bytearray()
        ins_map = {ins.slot: ins for ins in song.instruments}
        for slot in range(1, MAX_INSTRUMENTS + 1):
            name = ins_map[slot].name if slot in ins_map else ""
            out += _pad_str(name, 16)
        return out

    def _instrument_macros(self, song: TmuSong) -> bytearray:
        """
        31 × (3 + length×4) bytes.

        Per slot: [length][restart][voice][row0_b0..b3][row1_b0..b3]…

        From disk_tmu.asm _otmu_samploop:
          read 1 byte → length
          read (length×4 + 2) bytes → [restart][voice][rows…]

        IMPORTANT:
          restart MUST be < length.  0xFF causes the replayer to jump
          to row 255 of a short macro → out-of-bounds → hang.
          Use restart=0 (loop on last row) for all single-row macros.
        """
        out = bytearray()
        ins_map = {ins.slot: ins for ins in song.instruments}

        for slot in range(1, MAX_INSTRUMENTS + 1):
            if slot in ins_map:
                ins = ins_map[slot]
                length = min(ins.length, 32)
                restart = min(ins.restart, length - 1)

                if restart != ins.restart:
                    log.warning(
                        "Instrument slot %d: restart=%d clamped to %d (< length=%d)",
                        slot, ins.restart, restart, length,
                    )

                out.append(length)
                out.append(restart)
                out.append(ins.voice & 0xFF)
                for row in ins.rows[:length]:
                    out += row[:4]

                log.debug(
                    "Slot %02d: len=%d restart=%d voice=0x%02x",
                    slot, length, restart, ins.voice,
                )
            else:
                # Empty slot — silent 1-row macro
                out.append(1)       # length
                out.append(0)       # restart (must be 0 < 1)
                out.append(0)       # voice = none
                out += bytes([0x00, 0x00, 0x00, 0x00])

        return out

    def _fm_voices(self, song: TmuSong) -> bytearray:
        """16 × 8-byte custom OPLL voice patches."""
        out = bytearray()
        for i in range(MAX_CUSTOM_VOICES):
            if i < len(song.fm_voices):
                patch = song.fm_voices[i]
                if len(patch) != 8:
                    log.error("FM voice %d has wrong length %d; padding with zeros", i, len(patch))
                    patch = patch[:8].ljust(8, b'\x00')
                out += patch
                log.debug("Voice %02d: %s", i, " ".join(f"{b:02X}" for b in patch))
            else:
                out += bytes(8)
        return out

    def _drum_section(self, song: TmuSong) -> bytearray:
        """
        19 drum names (16 bytes each) + 19 empty drum macros (1 byte = length 0).
        The tracker expects MAX_DRUMS-1 = 19 entries for version >= 9.
        """
        out = bytearray()
        names = (song.drum_names + [""] * MAX_DRUMS)[:MAX_DRUMS - 1]   # 19 names
        for name in names:
            out += _pad_str(name, 16)
        for _ in range(MAX_DRUMS - 1):
            out.append(0)   # drum macro length = 0 (empty)
        log.debug("Drum section: %d names + %d empty macros", len(names), MAX_DRUMS - 1)
        return out

    def _patterns(self, song: TmuSong) -> bytearray:
        """
        For each pattern: [pat_num(1)][len_lo(1)][len_hi(1)][compressed_data]
        Terminated by 0xFF.

        Empty patterns are skipped (the tracker handles missing pattern
        numbers by keeping whatever was in RAM — safe since we write
        sequential indices matching the order list).
        """
        out = bytearray()
        for pat in song.patterns:
            flat = pat.flat_bytes()
            compressed = compress_pattern(flat)
            out.append(pat.index & 0xFF)
            out.append(len(compressed) & 0xFF)
            out.append((len(compressed) >> 8) & 0xFF)
            out += compressed
            log.debug(
                "Pattern %03d: %d bytes compressed (%.0f%%)",
                pat.index, len(compressed),
                100 * len(compressed) / max(1, len(flat)),
            )
        out.append(0xFF)   # end-of-patterns marker
        return out


# ── helpers ───────────────────────────────────────────────────────────────────

def _pad_str(s: str, length: int) -> bytearray:
    """Encode string as bytes, space-padded to exactly `length` bytes."""
    encoded = s.encode("utf-8", errors="replace")[:length]
    return bytearray(encoded.ljust(length, b" "))
