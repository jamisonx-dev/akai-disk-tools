#!/usr/bin/env python3
"""akaidisk.py - read AKAI S1000/S3000 native disk images (pure stdlib).

Reads the sampler's own filesystem straight out of a raw CD/HD image and
exports samples as 16-bit WAVs, preserving root key and loop points.

Everything here was verified byte-for-byte against the Spectrasonics discs in
"akai disk images/" (see FORMAT.md).  Layout summary, block size 0x2000 (8 KB):

  partition (7680 blocks = 60 MB, repeated back-to-back across the image)
    0x0000  size[2]            partition size in blocks
    0x0002  magic[98][2]       (3333 * k) & 0xFFFF  -- identifies a partition
    0x00C6  chksum[4]
    0x00CA  vol[100][16]       name[12] type[2] start[2]   (type 0 = unused)
    0x070A  fat[7680][2]       0x0000 free, 0x4000 reserved, 0xC000 end-of-file
                               anything else = next block number

  volume directory  (one 8 KB block at vol.start), 125 entries of 24 bytes:
    name[12] tag[4] type[1] size[3] start[2] osver[2]
    type 'p'/'s' = S1000 program/sample, +0x80 = S3000

  sample file: header then signed 16-bit little-endian mono PCM
    S1000 header 150 bytes (0x96), S3000 header 192 bytes (0xC0)

Author's note: file blocks are followed through the FAT, not assumed
contiguous, so fragmented volumes read correctly too.
"""

import os
import re
import struct
import sys

# ───────────────────────────────────────────────────────────── constants ────

BLOCK = 0x2000                  # 8 KB
PART_BLOCKS = 7680              # 60 MB partition
MAGIC_N = 98
MAGIC_STEP = 3333

VOL_OFF, VOL_N, VOL_SZ = 0x00CA, 100, 16
FAT_OFF = VOL_OFF + VOL_N * VOL_SZ          # 0x070A
DIR_ENTRIES, DIR_SZ = 125, 24

FAT_FREE, FAT_RESERVED, FAT_EOF = 0x0000, 0x4000, 0xC000

S1000_HDR, S3000_HDR = 0x96, 0xC0
# A keygroup record is the same size as its program's header: 150 for S1000,
# 192 for S3000 (the S3000 variant is the S1000 one plus 42 unused bytes).
# Verified: header + kgnum * KG == filesize for all 2002 programs on the six
# discs, with no exceptions.  Hardcoding 150 silently garbles every S3000
# program past its first keygroup.
KGS_N, KGS_OFF, KGS_SZ = 4, 34, 24          # 4 velocity zones per keygroup

# Akai's 6-bit character set.
CHARSET = "0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ#+-."

FTYPE = {
    0x70: ("p", "S1000 program"), 0x73: ("s", "S1000 sample"),
    0x64: ("d", "drum settings"), 0x71: ("q", "cue list"),
    0x74: ("t", "take"),          0x6D: ("m", "S1000 multi"),
    0xF0: ("P", "S3000 program"), 0xF3: ("S", "S3000 sample"),
    0xED: ("M", "S3000 multi"),
}

# Akai "PLAYBACK" parameter.  0/1 loop, 2/3 do not.
PMODE = {0: "LOOP IN RELEASE", 1: "LOOP UNTIL RELEASE",
         2: "NO LOOPING", 3: "PLAY TO SAMPLE END"}
PMODE_LOOPS = (0, 1)

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def akai_str(raw):
    """12-byte Akai name -> python str (trailing padding removed)."""
    return "".join(CHARSET[c] if c < len(CHARSET) else "?" for c in raw).rstrip()


def note_name(midi):
    """MIDI number -> name in the C3=60 convention Akai/JJOS both use."""
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def _u16(b, o):
    return b[o] | (b[o + 1] << 8)


def _u24(b, o):
    return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16)


def _u32(b, o):
    return int.from_bytes(b[o:o + 4], "little")


def _s8(v):
    return v - 256 if v > 127 else v


def safe_filename(name):
    """Akai names allow characters Windows forbids; keep it readable.

    Trailing dots are mapped to '_' rather than stripped.  Windows silently
    drops a trailing '.', and these libraries really do ship distinct samples
    named '-MM BRONX F.' and '-MM BRONX F' -- stripping merges them and one
    quietly overwrites the other.
    """
    out = re.sub(r'[<>:"/\\|?*]', "_", name).strip()
    out = re.sub(r"\.+$", lambda m: "_" * len(m.group()), out)
    return out or "UNNAMED"


def unique_name(base, used):
    """Reserve `base` in `used`, suffixing ~2, ~3... if it is already taken.

    A last-resort guard so two different Akai samples can never collapse onto
    one output file: silently losing a sample is worse than an odd name.
    """
    name, n = base, 1
    while name.lower() in used:
        n += 1
        name = f"{base}~{n}"
    used.add(name.lower())
    return name


# ───────────────────────────────────────────────────────────────── model ────

class AkaiSample:
    """Parsed sample header + a handle to its PCM."""

    def __init__(self, entry, raw):
        self.entry = entry
        self.hdr_len = S3000_HDR if entry.type in (0xF3,) else S1000_HDR
        h = raw
        self.blockid = h[0]
        self.bandwidth = h[1]
        self.root_key = h[2]                # MIDI note, C3=60
        self.name = akai_str(h[3:15])
        self.nloops = h[16]
        self.first_loop = h[17]
        self.pmode = h[19]
        self.cents = _s8(h[20])
        self.semis = _s8(h[21])
        self.nframes = _u32(h, 26)
        self.start = _u32(h, 30)
        self.end = _u32(h, 34)
        self.loops = []
        for i in range(8):
            o = 38 + i * 12
            at = _u32(h, o)                 # loop END point
            length = _u32(h, o + 6)         # length measured back from `at`
            hold = _u16(h, o + 10)          # 9999 = infinite / HOLD
            self.loops.append((at, length, hold))
        self.stereo_partner = _u16(h, 136)
        self.rate = _u16(h, 138)

    @property
    def loop(self):
        """(start, end) frames of the first active loop, or None."""
        if self.nloops < 1:
            return None
        at, length, _hold = self.loops[self.first_loop if
                                       self.first_loop < 8 else 0]
        if length <= 0 or at > self.nframes or at - length < 0:
            return None
        return (at - length, at)

    def loop_is_active(self, policy="auto"):
        if policy == "never":
            return False
        lp = self.loop
        if lp is None:
            return False
        if policy == "always":
            return True
        return self.pmode in PMODE_LOOPS

    def describe(self):
        lp = self.loop
        return (f"{self.name:<12} root={note_name(self.root_key)}"
                f"({self.root_key:3}) {self.rate}Hz {self.nframes:>8} fr "
                f"tune={self.semis:+d}/{self.cents:+d} "
                f"{PMODE.get(self.pmode, '?')}"
                + (f" loop {lp[0]}-{lp[1]}" if lp else " no-loop"))


class KeygroupSample:
    __slots__ = ("name", "vel_low", "vel_high")

    def __init__(self, raw):
        self.name = akai_str(raw[0:12])
        self.vel_low = raw[12]
        self.vel_high = raw[13]


class Keygroup:
    def __init__(self, raw):
        self.key_low = raw[3]
        self.key_high = raw[4]
        self.samples = []
        for i in range(KGS_N):
            o = KGS_OFF + i * KGS_SZ
            ks = KeygroupSample(raw[o:o + KGS_SZ])
            if ks.name:
                self.samples.append(ks)


class AkaiProgram:
    def __init__(self, entry, raw):
        self.entry = entry
        h = raw
        self.name = akai_str(h[3:15])
        self.midi_chan = h[16]
        self.key_low = h[19]
        self.key_high = h[20]
        self.octave = _s8(h[21])
        self.n_keygroups = h[42]
        self.keygroups = []
        hdr = S3000_HDR if entry.type == 0xF0 else S1000_HDR
        self.kg_size = hdr                  # keygroup stride == header size
        for i in range(self.n_keygroups):
            o = hdr + i * self.kg_size
            if o + self.kg_size > len(raw):
                break
            self.keygroups.append(Keygroup(raw[o:o + self.kg_size]))

    def velocity_layers(self):
        """Distinct (vel_low, vel_high) zones used, low to high."""
        zones = {(s.vel_low, s.vel_high)
                 for kg in self.keygroups for s in kg.samples}
        return sorted(zones)


class FileEntry:
    __slots__ = ("name", "type", "size", "start", "osver", "volume")

    def __init__(self, raw, volume):
        self.name = akai_str(raw[0:12])
        self.type = raw[16]
        self.size = _u24(raw, 17)
        self.start = _u16(raw, 20)
        self.osver = f"{raw[23]}.{raw[22]:02d}"
        self.volume = volume

    @property
    def type_char(self):
        return FTYPE.get(self.type, ("?", f"unknown 0x{self.type:02x}"))[0]

    @property
    def type_name(self):
        return FTYPE.get(self.type, ("?", f"unknown 0x{self.type:02x}"))[1]

    @property
    def is_sample(self):
        return self.type in (0x73, 0xF3)

    @property
    def is_program(self):
        return self.type in (0x70, 0xF0)

    def read(self):
        return self.volume.partition.read_chain(self.start, self.size)

    def parse(self):
        """-> AkaiSample / AkaiProgram / None."""
        if self.is_sample:
            return AkaiSample(self, self.read()[:S3000_HDR])
        if self.is_program:
            return AkaiProgram(self, self.read())
        return None


class Volume:
    def __init__(self, partition, index, name, vtype, start):
        self.partition = partition
        self.index = index
        self.name = name
        self.type = vtype
        self.start = start
        self._files = None

    @property
    def files(self):
        if self._files is None:
            self._files = []
            blk = self.partition.read_block(self.start)
            for i in range(DIR_ENTRIES):
                raw = blk[i * DIR_SZ:(i + 1) * DIR_SZ]
                if len(raw) < DIR_SZ or raw[16] == 0:
                    continue
                self._files.append(FileEntry(raw, self))
        return self._files


class Partition:
    def __init__(self, image, index, offset):
        self.image = image
        self.index = index
        self.offset = offset
        self.letter = chr(ord("A") + index) if index < 26 else f"#{index}"
        hdr = image.read_at(offset, FAT_OFF + PART_BLOCKS * 2)
        self.size = _u16(hdr, 0)
        self._fat = hdr
        self.volumes = []
        for i in range(VOL_N):
            o = VOL_OFF + i * VOL_SZ
            vtype = _u16(hdr, o + 12)
            if vtype == 0:
                continue
            self.volumes.append(
                Volume(self, i, akai_str(hdr[o:o + 12]), vtype,
                       _u16(hdr, o + 14)))

    @staticmethod
    def looks_like_partition(hdr):
        if len(hdr) < FAT_OFF:
            return False
        return all(_u16(hdr, 2 + 2 * k) == (MAGIC_STEP * k) & 0xFFFF
                   for k in range(MAGIC_N))

    def fat(self, blk):
        return _u16(self._fat, FAT_OFF + blk * 2)

    def read_block(self, blk):
        return self.image.read_at(self.offset + blk * BLOCK, BLOCK)

    def read_chain(self, start, size):
        """Follow the FAT chain from `start`, returning exactly `size` bytes."""
        out = bytearray()
        blk, guard = start, 0
        while len(out) < size:
            if blk == 0 or blk >= self.size:
                break
            out += self.read_block(blk)
            nxt = self.fat(blk)
            if nxt in (FAT_EOF, FAT_FREE, FAT_RESERVED):
                break
            blk = nxt
            guard += 1
            if guard > PART_BLOCKS:
                raise ValueError(f"FAT loop at block {start}")
        return bytes(out[:size])


class AkaiImage:
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "rb")
        self.size = os.path.getsize(path)
        self.partitions = []
        off, idx = 0, 0
        while off + FAT_OFF < self.size:
            hdr = self.read_at(off, FAT_OFF)
            if not Partition.looks_like_partition(hdr):
                break
            p = Partition(self, idx, off)
            self.partitions.append(p)
            off += PART_BLOCKS * BLOCK
            idx += 1

    def read_at(self, offset, length):
        self.fh.seek(offset)
        return self.fh.read(length)

    def close(self):
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def walk(self):
        for p in self.partitions:
            for v in p.volumes:
                for f in v.files:
                    yield p, v, f


# ─────────────────────────────────────────────────────────── WAV writing ────

def resample_16(pcm, src_rate, dst_rate, channels=1):
    """Linear-interpolation resample of interleaved 16-bit PCM.

    Only used to bring 48 kHz Akai samples to the MPC1000's native 44.1 kHz.
    Linear is adequate for a small downward ratio; if you care about the last
    dB of quality, resample externally with sox and skip --rate.
    """
    if src_rate == dst_rate:
        return pcm
    n = len(pcm) // (2 * channels)
    src = struct.unpack(f"<{n * channels}h", pcm[:n * channels * 2])
    out_n = int(n * dst_rate / src_rate)
    step = src_rate / dst_rate
    out = []
    for i in range(out_n):
        pos = i * step
        i0 = int(pos)
        frac = pos - i0
        i1 = min(i0 + 1, n - 1)
        for c in range(channels):
            a = src[i0 * channels + c]
            b = src[i1 * channels + c]
            out.append(int(a + (b - a) * frac))
    return struct.pack(f"<{len(out)}h", *out)


def interleave(left, right):
    """Two mono 16-bit buffers -> interleaved stereo (shorter one zero-padded)."""
    n = max(len(left), len(right)) // 2
    l = struct.unpack(f"<{len(left)//2}h", left[:len(left) // 2 * 2])
    r = struct.unpack(f"<{len(right)//2}h", right[:len(right) // 2 * 2])
    out = []
    for i in range(n):
        out.append(l[i] if i < len(l) else 0)
        out.append(r[i] if i < len(r) else 0)
    return struct.pack(f"<{len(out)}h", *out)


def write_wav(path, pcm, rate, root=None, loop=None, total_frames=None,
              channels=1):
    """16-bit WAV (mono or stereo).  When `root` is given, a JJOS2XL-shaped
    `smpl` chunk is written *before* the data chunk (JJOS ignores one that
    follows data)."""
    chunks = b""
    if root is not None:
        frames = (total_frames if total_frames is not None
                  else len(pcm) // (2 * channels))
        loops_block = b""
        if loop:
            loops_block = struct.pack("<6I", 0x706F6F6C, 0,
                                      int(loop[0]), int(loop[1]), 0, 0)
        sd = struct.pack("<HH8xII", 2, 60, 0, max(frames - 1, 0))
        body = (struct.pack("<9I", 0x01000047, 0x5E,
                            int(1_000_000_000 / rate), int(root),
                            0, 0, 0, len(loops_block) // 24, len(sd))
                + loops_block + sd)
        chunks += struct.pack("<4sI", b"smpl", len(body)) + body

    blk = 2 * channels
    fmt = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, channels, rate,
                      rate * blk, blk, 16)
    data = struct.pack("<4sI", b"data", len(pcm)) + pcm
    if len(pcm) & 1:
        data += b"\x00"
    body = b"WAVE" + fmt + chunks + data
    with open(path, "wb") as fh:
        fh.write(struct.pack("<4sI", b"RIFF", len(body)) + body)


# ───────────────────────────────────────────────────────────── commands ────

def cmd_list(args):
    with AkaiImage(args.image) as img:
        print(f"{os.path.basename(img.path)}  {img.size:,} bytes  "
              f"{len(img.partitions)} partition(s)")
        n_s = n_p = 0
        for p in img.partitions:
            print(f"\n  partition {p.letter}  ({p.size} blocks, "
                  f"{len(p.volumes)} volume(s))")
            for v in p.volumes:
                files = v.files
                print(f"    [{p.letter}] {v.name:<12}  {len(files)} files")
                if args.verbose:
                    for f in files:
                        print(f"         {f.name:<12} {f.type_char} "
                              f"{f.size:>9,}  os{f.osver}")
                for f in files:
                    n_s += f.is_sample
                    n_p += f.is_program
        print(f"\n  totals: {n_s} samples, {n_p} programs")


def cmd_programs(args):
    with AkaiImage(args.image) as img:
        for p, v, f in img.walk():
            if not f.is_program:
                continue
            prog = f.parse()
            zones = prog.velocity_layers()
            print(f"\n[{p.letter}] {v.name} / {prog.name}   "
                  f"{prog.n_keygroups} keygroups, keys "
                  f"{note_name(prog.key_low)}-{note_name(prog.key_high)}, "
                  f"{len(zones)} velocity layer(s)")
            for i, (lo, hi) in enumerate(zones):
                print(f"      layer {i + 1}: velocity {lo}-{hi}")
            if args.verbose:
                for kg in prog.keygroups:
                    names = ", ".join(f"{s.name}[{s.vel_low}-{s.vel_high}]"
                                      for s in kg.samples)
                    print(f"      {note_name(kg.key_low)}-"
                          f"{note_name(kg.key_high)}: {names}")


def stereo_base(name):
    """'WHOA E 3C -L' -> ('WHOA E 3C', 'L'), else (name, None)."""
    m = re.match(r"^(.*?)\s*-([LR])$", name)
    return (m.group(1).rstrip(), m.group(2)) if m else (name, None)


def sample_pcm(entry):
    """(AkaiSample, raw PCM bytes) for a sample file entry."""
    smp = entry.parse()
    raw = entry.read()
    pcm = raw[smp.hdr_len:smp.hdr_len + smp.nframes * 2]
    if len(pcm) < smp.nframes * 2:
        print(f"  ! {entry.name}: short read "
              f"({len(pcm)//2}/{smp.nframes} frames)", file=sys.stderr)
    return smp, pcm


def cmd_extract(args):
    made = skipped = joined = converted = collided = 0
    manifest = []
    with AkaiImage(args.image) as img:
        disc = os.path.splitext(os.path.basename(img.path))[0]
        for p in img.partitions:
            if args.partition and p.letter != args.partition.upper():
                continue
            for v in p.volumes:
                if args.volume and args.volume.upper() not in v.name.upper():
                    continue
                samples = [f for f in v.files if f.is_sample]
                if not samples:
                    continue
                outdir = os.path.join(args.outdir, safe_filename(disc),
                                      f"{p.letter}_{safe_filename(v.name)}")
                os.makedirs(outdir, exist_ok=True)

                # pair up -L/-R within this volume
                pairs, singles, seen = {}, [], {}
                if args.stereo == "join":
                    for f in samples:
                        base, side = stereo_base(f.name)
                        if side:
                            seen.setdefault(base, {})[side] = f
                    pairs = {b: d for b, d in seen.items() if len(d) == 2}
                for f in samples:
                    base, side = stereo_base(f.name)
                    if side and base in pairs:
                        if side == "R":
                            continue            # handled with its L partner
                        singles.append((base, pairs[base]["L"],
                                        pairs[base]["R"]))
                    else:
                        singles.append((f.name, f, None))

                used = set()
                for outname, fl, fr in singles:
                    stem = unique_name(safe_filename(outname), used)
                    if stem != safe_filename(outname):
                        collided += 1
                    path = os.path.join(outdir, stem + ".wav")
                    if os.path.exists(path) and not args.force:
                        skipped += 1
                        continue
                    smp, pcm = sample_pcm(fl)
                    channels = 1
                    if fr is not None:
                        _rs, rpcm = sample_pcm(fr)
                        pcm = interleave(pcm, rpcm)
                        channels = 2
                        joined += 1
                    rate = smp.rate
                    loop = smp.loop if smp.loop_is_active(args.loops) else None
                    if args.rate and rate != args.rate:
                        pcm = resample_16(pcm, rate, args.rate, channels)
                        if loop:
                            k = args.rate / rate
                            loop = (int(loop[0] * k), int(loop[1] * k))
                        rate = args.rate
                        converted += 1
                    frames = len(pcm) // (2 * channels)
                    write_wav(path, pcm, rate,
                              root=None if args.no_smpl else smp.root_key,
                              loop=loop, total_frames=frames,
                              channels=channels)
                    made += 1
                    manifest.append((p.letter, v.name, outname, smp, loop,
                                     channels, rate, frames))
                    if args.verbose:
                        tag = " [stereo]" if channels == 2 else ""
                        print(f"  {smp.describe()}{tag}")

    if manifest and not args.no_manifest:
        mpath = os.path.join(args.outdir, safe_filename(disc), "manifest.tsv")
        os.makedirs(os.path.dirname(mpath), exist_ok=True)
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write("partition\tvolume\tsample\troot\troot_name\tchannels\t"
                     "rate\tsrc_rate\tframes\tsemis\tcents\tplayback\t"
                     "loop_start\tloop_end\n")
            for letter, vol, name, s, loop, ch, rate, frames in manifest:
                fh.write(f"{letter}\t{vol}\t{name}\t{s.root_key}\t"
                         f"{note_name(s.root_key)}\t{ch}\t{rate}\t{s.rate}\t"
                         f"{frames}\t{s.semis}\t{s.cents}\t"
                         f"{PMODE.get(s.pmode, s.pmode)}\t"
                         f"{loop[0] if loop else ''}\t"
                         f"{loop[1] if loop else ''}\n")
        print(f"manifest -> {mpath}")
    print(f"extracted {made} WAV(s)"
          + (f", {joined} stereo pair(s) joined" if joined else "")
          + (f", {converted} resampled to {args.rate}Hz" if converted else "")
          + (f", {collided} renamed to avoid a filename clash" if collided else "")
          + (f", skipped {skipped} existing" if skipped else ""))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="show partitions / volumes / files")
    lp.add_argument("image")
    lp.add_argument("-v", "--verbose", action="store_true")
    lp.set_defaults(func=cmd_list)

    pp = sub.add_parser("programs", help="show program keygroup maps")
    pp.add_argument("image")
    pp.add_argument("-v", "--verbose", action="store_true")
    pp.set_defaults(func=cmd_programs)

    xp = sub.add_parser("extract", help="export samples to WAV")
    xp.add_argument("image")
    xp.add_argument("outdir")
    xp.add_argument("--partition", help="only this partition letter")
    xp.add_argument("--volume", help="only volumes matching this substring")
    xp.add_argument("--loops", choices=("auto", "always", "never"),
                    default="auto",
                    help="auto = honour the Akai PLAYBACK mode (default)")
    xp.add_argument("--stereo", choices=("join", "split"), default="join",
                    help="join = merge -L/-R pairs into stereo (default)")
    xp.add_argument("--rate", type=int, metavar="HZ",
                    help="resample everything to this rate "
                         "(use 44100 for the MPC1000)")
    xp.add_argument("--no-smpl", action="store_true",
                    help="omit the smpl chunk (plain WAV)")
    xp.add_argument("--no-manifest", action="store_true")
    xp.add_argument("--force", action="store_true", help="overwrite existing")
    xp.add_argument("-v", "--verbose", action="store_true")
    xp.set_defaults(func=cmd_extract)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
