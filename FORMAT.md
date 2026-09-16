# AKAI S1000 / S3000 disk format — findings

Everything here was verified byte-for-byte against the six Spectrasonics discs
in `../akai disk images/`. Where a claim is inferred rather than measured, it
says so. Same discipline as `_planning/pgm_re/FINDINGS.md`.

The `.iso` extension on these files is a lie — there is no ISO9660 filesystem.
They are raw images of the sampler's own filesystem, which is why normal disc
tools see nothing.

Cross-checked against the struct definitions in **akaiutil** (M. Indlekofer,
BSD) — the mature C reference implementation. Our independent read of the bytes
and akaiutil's headers agree on every field.

---

## Verification status

| Claim | How verified |
|---|---|
| Block size 8192 | file start-blocks vs sizes across a whole volume |
| Partition magic table | 98 entries of `(3333*k) & 0xFFFF`, all 9 partitions × 6 discs |
| 24-byte file entries | matches akaiutil `akai_voldir_entry_s` exactly |
| Sample header 150 (S1000) / 192 (S3000) | `header + frames*2 == filesize` on **all 9381 samples, 0 mismatches** |
| PCM is signed 16-bit LE mono | autocorrelation pitch matches declared root key within a few cents |
| Root key is a plain MIDI note | tuned multisamples land on the right note (C3 = 60) |
| Keygroup layout / velocity zones | 4 zones decode as 1-86 / 87-104 / 105-119 / 120-127 |
| **Keygroup stride = header size** (150 S1000 / 192 S3000) | `header + kgnum * KG == filesize` on **all 2002 programs, 0 exceptions** |
| Loop `at` = END, `len` = length backwards | `at - len` always lands inside the sample; `at` often == frame count |

---

## Layout

All multi-byte integers are **little-endian**.

### Partition

Partitions are 7680 blocks (60 MB) and sit back-to-back from offset 0. A disc
is simply N of them; the last may be short. Detect one by the magic table.

```
0x0000  size[2]        partition size in blocks (7680)
0x0002  magic[98][2]   (3333 * k) & 0xFFFF, k = 0..97   <- partition signature
0x00C6  chksum[4]
0x00CA  vol[100][16]   volume directory (see below)
0x070A  fat[7680][2]   block allocation table
```

**Volume entry (16 bytes)** — `name[12] type[2] start[2]`
`type` 0 = unused slot (the discs are full of `VOLUME 004` placeholders);
non-zero = active. `start` is the block holding that volume's file directory.

**FAT codes**

| Value | Meaning |
|---|---|
| `0x0000` | free |
| `0x4000` | reserved (blocks 0-2 = partition header, plus each volume dir block) |
| `0xC000` | end of file |
| other | next block number |

Files are *usually* contiguous on a mastered CD, but `akaidisk.py` follows the
chain anyway, so fragmented volumes read correctly.

### Volume directory

One 8 KB block, 125 entries of 24 bytes:

```
+0x00  name[12]     Akai charset
+0x0C  tag[4]       0x20 filler on these discs
+0x10  type[1]
+0x11  size[3]      file size in bytes
+0x14  start[2]     first block (partition-relative)
+0x16  osver[2]     e.g. 04 28 -> "4.40"
```

**File types seen across the six discs**

| Byte | Char | Meaning | Count |
|---|---|---|---|
| `0x73` | `s` | S1000 sample | 3177 |
| `0xF3` | `S` | S3000 sample | 6204 |
| `0x70` | `p` | S1000 program | 1413 |
| `0xF0` | `P` | S3000 program | 589 |
| `0x6D`/`0xED` | `m`/`M` | multi | 105 |
| `0x64` | `d` | drum settings | 191 |
| `0x74` | `t` | take | 103 |
| `0x78` | `x` | **unidentified** | 102 |

`0x78` is not read by our tool and not needed for sample extraction.

### Character set

Akai packs names into a 6-bit set — this is why the names look like binary
garbage in a hex editor:

```
index: 0-9 -> '0'-'9'
       10  -> ' '
       11-36 -> 'A'-'Z'
       37-40 -> '#' '+' '-' '.'
```

### Sample file

`header (150 or 192 bytes) || signed 16-bit LE mono PCM`

S3000 = the S1000 header plus 42 unused bytes, so the field offsets below are
identical for both.

```
+0x00  blockid        0x03
+0x01  bandwidth
+0x02  rkey           MIDI root key, C3 = 60
+0x03  name[12]
+0x10  nloops         active loop count
+0x11  first_loop
+0x13  pmode          PLAYBACK, see below
+0x14  ctune          cents, signed
+0x15  stune          semitones, signed
+0x16  locat[4]       sampler RAM address, ignore
+0x1A  slen[4]        frame count
+0x1E  start[4] / +0x22 end[4]
+0x26  loop[8][12]    at[4] fine[2] len[4] time[2]
+0x88  stpaira[2]     stereo partner header address, 0xFFFF = none
+0x8A  srate[2]       44100 or 48000 on these discs
```

**Loops.** `at` is the loop **END** point; `len` is measured *backwards* from
it, so `loop_start = at - len`. `time` = 9999 means hold/infinite. A sample
with `at == slen` and no active loop is the idle default.

**PLAYBACK (`pmode`).** Values 0 and 1 loop; 2 and 3 do not. Ordering
`0 LOOP IN RELEASE / 1 LOOP UNTIL RELEASE / 2 NO LOOPING / 3 PLAY TO SAMPLE
END` is the documented S1000 parameter order — **inferred**, but consistent
with the data (5947 of 9381 samples are `NO LOOPING`, and the ones marked
looping have valid in-range loop points).

**Stereo.** Stored as two mono files named `... -L` / `... -R`. On these discs
exactly 2090 samples have `stpaira` set, and exactly 2090 have an `-L`/`-R`
suffix — the two signals agree perfectly, so name-based pairing is safe.

### Program file

`header (150 or 192) || keygroup[kgnum] (150 bytes each)`

```
+0x00  blockid    0x01
+0x01  kg1a[2]    offset of first keygroup (= header size)
+0x03  name[12]
+0x10  midich1
+0x13  keylo / +0x14 keyhi
+0x15  oct        signed octave offset
+0x29  kgxf       keygroup crossfade
+0x2A  kgnum      number of keygroups
```

**Keygroup stride — the easy thing to get wrong.** A keygroup record is the
same size as its program's header: **150 bytes for S1000, 192 for S3000.**
Hardcoding 150 parses the first S3000 keygroup correctly and then garbles
every one after it, producing plausible-looking nonsense names rather than an
obvious crash. Verified: `header + kgnum * stride == filesize` for all 2002
programs with no exceptions.

**Keygroup (150 / 192 bytes)**

```
+0x00  blockid    0x02
+0x01  nextkg[2]
+0x03  key_low
+0x04  key_high
+0x22  sample[4][24]   4 velocity zones
```

**Keygroup sample entry (24 bytes)** — `name[12] vel_low vel_high ...`

The four zones are the velocity layers. A typical Spectrasonics program uses
`1-86 / 87-104 / 105-119 / 120-127`, which is why sample names end in
A/B/C/D.

Adjacent keygroups frequently reference the *same* sample (e.g. both G2 and
G#2 play `COLLINGSG#2D`); `akai2mpc.py` merges those into one pad spanning the
union of their ranges.

**Stereo keygroups hold two entries per velocity zone** — the `-L` and `-R`
halves — so a 2-layer stereo keygroup has 4 entries, exactly like a 4-layer
mono one. Velocity layers must therefore be counted by distinct
`(vel_low, vel_high)` zone, not by entry, or "layer 2" lands on the other
channel of layer 1.

### Names

Akai names are **12 characters**; the MPC1000 allows **16**, so nothing gets
truncated on the way across and there are **zero** MPC-name collisions on any
of the six discs. That leaves 4 characters spare for a `~2` disambiguation
suffix if one is ever needed. `pgmlib.sanitize_name` maps anything outside
`alnum + " !#$%&'()-@_{}"` to `_`, which includes `.`.

**Trailing dots are a real trap.** These libraries ship genuinely distinct
samples named `-MM BRONX F.` and `-MM BRONX F`. Windows silently drops a
trailing dot, so naively stripping it merges the two and one overwrites the
other — 24 samples would have vanished from Bass Legends CD 1 alone, with no
error. `safe_filename()` maps trailing dots to `_`, and `unique_name()` is a
final backstop that suffixes `~2`, `~3` rather than ever letting two samples
land on one file.

### Missing samples

359 keygroup references across the six discs point at samples that aren't on
their own disc. 351 of those aren't anywhere in the set: Vocal Planet is a
multi-volume library and only Vols 1, 2 and 5 are here, so some programs
reference samples that ship on discs we don't have. A further 10 references
are to `SINE`, the sampler's built-in ROM waveform, which is not a file at
all. This is missing source media, not a parsing failure — 98.8% of all 29267
references resolve.

---

## Why this matters for the MPC

**An MPC1000 pad holds four sample slots, each with its own velocity range** —
in both DRUM and INST programs. Per the
[mybunnyhug PGM spec](https://www.mybunnyhug.org/fileformats/pgm/), a slot
lives at `(pad * 0xA4) + (slot * 0x18)` and its `+0x12`/`+0x13` bytes are
velocity **Range Lower / Range Upper**. Overlapping ranges sound together.

An Akai keygroup also holds up to four samples, each with a velocity zone. The
two line up almost exactly, so nothing has to be discarded:

```
Akai keygroup  ->  MPC pad
  key range    ->  pad  +0x1C / +0x1D   (INST LOW/HIGH)
  vel zone 1   ->  slot 0  +0x12/+0x13
  vel zone 2   ->  slot 1
  vel zone 3   ->  slot 2
  vel zone 4   ->  slot 3
```

Key ranges come from the Akai keygroups directly rather than being guessed
from root-note midpoints, so the multisample maps across the keyboard exactly
as the original did.

### Slot +0x12/+0x13 is VELOCITY — an easy and expensive mistake

`pgmlib.build_inst` used to write the *key* range into these bytes, commented
"(harmless)". It is not harmless: a pad covering F#2 got velocity range 42-42,
so that sample only sounded when struck at exactly velocity 42, and most of
the program was silent. Two independent sources say otherwise — the spec
above, and the working `6strBAx.PGM` multisample, whose slots are all `0/127`
while its key splits sit at pad `+0x1C/+0x1D` (FINDINGS session 10). Fixed in
both `pgmlib.build_inst` and `akai2mpc.py`.

### Program type

Some instruments want an INST program, some want a DRUM program — `--type`
picks. INST sets the header INST flag, writes pad key ranges, and uses play
mode 1 (Note On). DRUM leaves key ranges alone (the MPC's own pad→note map
applies) and uses play mode 0 (One Shot).
