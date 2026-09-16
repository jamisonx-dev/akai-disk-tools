# AKAI Disk Tools

Created as a way to get around some sketchy software choices available, such as they are. One can find these Akai disks/disk images scattered around but they won't operate without something that knows how to to read the proprietary structure Akai used. Here it is. I hope this helps someone somewhere.

---


This reads **AKAI S1000 / S3000** sampler disk images and exports the samples to WAV — with root key, tuning, and loop points
preserved. Pure Python, standard library only.

The `.iso` extension on these images is usually a lie: there's no ISO 9660
filesystem inside, just a raw image of the sampler's own filesystem, which is
why ordinary disc tools see nothing. This reads it directly.

- **`akaidisk.py`** — the format reader + WAV exporter (command line)
- **`akaidisk_gui.py`** — a point-and-click window for the same thing
- **`FORMAT.md`** — the reverse-engineered disk format, and how each claim was verified

> Making MPC kits from these samples? See the companion project
> **[mpc-pgm-tools](https://github.com/jamisonx-dev/mpc-pgm-tools)** — it turns
> an AKAI program into an MPC1000 / JJOS2XL `.PGM` kit.

---

## Easiest way (Windows)

Download **`AKAI-Disk-Extractor.exe`** from the
[Releases](https://github.com/jamisonx-dev/akai-disk-tools/releases) page and
double-click it. No Python needed.

1. **Add disk images** — pick one or more `.iso` / `.img` / raw dumps
2. **Output folder** — where the WAVs go
3. **Extract**

Defaults resample to 44.1 kHz (MPC1000 native) and merge L/R sample pairs into
real stereo. Uncheck those if you want the originals untouched.

## From Python (any OS)

Needs Python 3. The GUI also needs `tkinter` (bundled with Python on
Windows/macOS; on Linux run `sudo apt install python3-tk`).

Point-and-click:

```bash
python akaidisk_gui.py
```

Or the command line:

```bash
# look around
python akaidisk.py list  "MyDisc.iso"
python akaidisk.py list  "MyDisc.iso" -v          # every file
python akaidisk.py programs "MyDisc.iso" -v        # multisample key maps

# extract to WAV
python akaidisk.py extract "MyDisc.iso" ./out --rate 44100
```

Output lands in `out/<disc>/<partition>_<volume>/<sample>.wav`, alongside a
`manifest.tsv` that records the root key, tuning (semitones/cents), loop points,
original sample rate, and stereo/mono for every sample. Every WAV gets a
`smpl` chunk carrying the root key and loop, so a sampler or the MPC knows how
to map it.

### Useful flags

| Flag | Effect |
|---|---|
| `--rate 44100` | resample (e.g. 48 kHz material down to the MPC1000's 44.1) |
| `--stereo join` | merge `-L`/`-R` into real stereo (default; `split` keeps them apart) |
| `--loops auto` | honour the AKAI playback mode (default); `always` / `never` to override |
| `--partition A` | only this partition letter |
| `--volume NAME` | only volumes whose name contains this text |
| `--no-smpl` | plain WAV, no `smpl` chunk |
| `--force` | overwrite existing files |

Filenames are made filesystem-safe (the original 12-character AKAI name is kept
in `manifest.tsv`), and if two samples in a volume would ever collide, the
second gets a `~2` suffix — a silent overwrite can't happen.

---

## Note on sample content

**No sample libraries are included in this repository.** This is a *tool* for
reading disks you already own. The format work was verified against
commercially licensed AKAI discs, which are not distributed here.

## License

MIT — see [LICENSE](LICENSE).
