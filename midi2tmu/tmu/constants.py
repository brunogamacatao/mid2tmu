"""
tmu/constants.py
────────────────
All TMU binary format constants derived from the TriloTracker source:
  code/variablesFM.asm, code/disk_tmu.asm, code/compression2.asm

Keep this file as the single source of truth — never scatter magic
numbers across converter code.

References
──────────
  _CHIPSET_SCC  equ $00
  _CHIPSET_FM   equ $10
  _CHIPSET_SMS  equ $30
  INSTRUMENT_LEN equ 32       ; max macro rows
  INSTRUMENT_SIZE equ (32*4)+3 = 131
  MAX_DRUMS      equ 20
  DRUMMACRO_SIZE equ (7*16)+1 = 113
  SONG_PATLNSIZE equ 4*8 = 32   ; bytes per pattern row
  SONG_PATSIZE   equ 32*64 = 2048
  SONG_SEQSIZE   equ 200       ; max order list length
"""

# ── TMU version ────────────────────────────────────────────────────────────────
# The tracker load routine gates the extra-bytes block on exactly version==11:
#   and $0f; cp 11; jr nz, skip_extras
# Versions 12+ cause the extra block to be skipped entirely, corrupting all
# subsequent field offsets.  Always write version 11.
TMU_VERSION = 11

# ── Chip type nibble (bits [6:4] of the header byte) ──────────────────────────
CHIPSET_SCC = 0x00
CHIPSET_FM  = 0x10
CHIPSET_SMS = 0x30

# ── Channel setup (bit 7 of the header byte) ──────────────────────────────────
# 0 = default 8-channel setup
# 0x80 = 2/6 split (2 PSG + 6 FM)  — set by the tracker when replay_chan_setup=1
CHANSETUP_DEFAULT = 0x00
CHANSETUP_2_6     = 0x80

# ── Period / tuning tables ──────────────────────────────────────────────────────
PERIOD_MODERN = 0   # default — matches OPLL_notes_modern in TMUCompile
PERIOD_KONAMI = 1
PERIOD_A448   = 2
PERIOD_EARTH  = 3

# ── Pattern geometry ───────────────────────────────────────────────────────────
ROWS_PER_PATTERN  = 64
CHANNELS_PER_ROW  = 8
BYTES_PER_CHANNEL = 4   # [note, ins, (vol<<4)|cmd, par]
PATTERN_LINE_SIZE = CHANNELS_PER_ROW * BYTES_PER_CHANNEL   # = 32
PATTERN_SIZE      = PATTERN_LINE_SIZE * ROWS_PER_PATTERN   # = 2048

# ── Instrument / macro limits ──────────────────────────────────────────────────
MAX_INSTRUMENTS   = 31   # slots 1-31 (slot 0 reserved/empty)
MAX_MACRO_ROWS    = 32
INSTRUMENT_SIZE   = MAX_MACRO_ROWS * BYTES_PER_CHANNEL + 3  # = 131

# ── FM voice slots ─────────────────────────────────────────────────────────────
MAX_CUSTOM_VOICES  = 16   # custom voice slots 0-15
VOICE_HARDWARE_MAX = 15   # OPLL hardware presets 1-15 (0 = none)
VOICE_CUSTOM_BASE  = 16   # voice bytes 16-31 → custom voices 0-15

# ── Drum limits (FM / SMS) ─────────────────────────────────────────────────────
MAX_DRUMS        = 20
DRUM_NAME_LEN    = 16
DRUM_MACRO_ROWS  = 16
DRUM_ROW_BYTES   = 7
DRUMMACRO_SIZE   = DRUM_MACRO_ROWS * DRUM_ROW_BYTES + 1   # = 113

# ── Order list ─────────────────────────────────────────────────────────────────
MAX_ORDER_LEN = 200   # SONG_SEQSIZE
ORDER_END     = 0xFF  # no-loop marker in the order restart position

# ── Special note values (stored raw in the pattern, replayer reads directly) ───
NOTE_EMPTY   = 0    # no event
NOTE_RELEASE = 97   # release the note (key-off)  — replayer: cp 97; jr z, _dc_restNote
NOTE_SUSTAIN = 98   # hold current note            — replayer: jr c, _dc_sustainNote
NOTE_VOL0    = 99   # set track volume to 0        — replayer: cp 99; jr z, _dc_vol0note

# ── Extra-bytes block layout (written after the version byte for v>=11) ────────
# Save writes: [count=34][period][instrument_types×32][drum_type]
# Load reads:  count bytes into buffer+1, then picks fields by offset.
EXTRA_COUNT         = 34   # = 1 (period) + 32 (instrument_types) + 1 (drum_type)
EXTRA_PERIOD_OFFSET = 0    # buffer+1
EXTRA_ITYPES_OFFSET = 1    # buffer+2 … buffer+33
EXTRA_DRUMTYPE_OFFSET = 33 # buffer+34

# Instrument type values used in the instrument_types array
ITYPE_FM  = 3   # default for all slots in a new FM song
ITYPE_PSG = 1
