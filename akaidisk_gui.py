#!/usr/bin/env python3
r"""
akaidisk_gui.py -- point-and-shoot front end for akaidisk.py.

Pick one or more AKAI S1000/S3000 disk images (.iso / .img / raw), pick an
output folder, hit Extract -> WAVs with a manifest.tsv. No command line needed.

Runs on any machine with Python 3 + tkinter (tkinter ships with Python on
Windows/macOS; on Linux: apt install python3-tk). akaidisk itself is pure
stdlib -- no other dependencies.
"""
import io
import os
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import akaidisk  # the reader/exporter (same folder)


class _Args:
    """A plain args object matching what akaidisk.cmd_extract() reads."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _LogStream(io.TextIOBase):
    """Redirect target: akaidisk prints its progress; we forward it to the log."""
    def __init__(self, say):
        self._say = say
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._say(line + "\n")
        return len(s)

    def flush(self):
        if self._buf:
            self._say(self._buf)
            self._buf = ""


class App:
    def __init__(self, root):
        self.root = root
        root.title("AKAI Disk Extractor")
        root.geometry("680x520")
        root.minsize(600, 460)
        self.images = []
        self.outdir = tk.StringVar(value="")
        self.resample = tk.BooleanVar(value=True)      # -> 44100 (MPC1000 native)
        self.join_stereo = tk.BooleanVar(value=True)
        self.overwrite = tk.BooleanVar(value=False)
        self.loops = tk.StringVar(value="auto")
        self.volume = tk.StringVar(value="")

        pad = dict(padx=10, pady=6)
        top = ttk.Frame(root)
        top.pack(fill='x', **pad)
        ttk.Button(top, text="Add disk images…", command=self.pick_files).pack(side='left')
        ttk.Button(top, text="Clear", command=self.clear_files).pack(side='left', padx=6)

        self.lst = tk.Listbox(root, height=6)
        self.lst.pack(fill='both', expand=False, padx=10)

        out = ttk.Frame(root)
        out.pack(fill='x', **pad)
        ttk.Button(out, text="Output folder…", command=self.pick_out).pack(side='left')
        ttk.Label(out, textvariable=self.outdir, foreground='#555').pack(side='left', padx=8)

        opts = ttk.LabelFrame(root, text="Options")
        opts.pack(fill='x', padx=10, pady=4)
        r1 = ttk.Frame(opts); r1.pack(fill='x', padx=6, pady=4)
        ttk.Checkbutton(r1, text="Resample to 44.1 kHz (MPC1000 native)",
                        variable=self.resample).pack(side='left')
        ttk.Checkbutton(r1, text="Join L/R into stereo",
                        variable=self.join_stereo).pack(side='left', padx=12)
        ttk.Checkbutton(r1, text="Overwrite existing",
                        variable=self.overwrite).pack(side='left')
        r2 = ttk.Frame(opts); r2.pack(fill='x', padx=6, pady=4)
        ttk.Label(r2, text="Loops:").pack(side='left')
        ttk.OptionMenu(r2, self.loops, "auto", "auto", "always", "never").pack(side='left', padx=6)
        ttk.Label(r2, text="Only volumes containing:").pack(side='left', padx=(16, 4))
        ttk.Entry(r2, textvariable=self.volume, width=18).pack(side='left')

        self.go = ttk.Button(root, text="Extract  ▶", command=self.start)
        self.go.pack(**pad)
        self.prog = ttk.Progressbar(root, mode='indeterminate')
        self.prog.pack(fill='x', padx=10)

        self.log = tk.Text(root, height=12, wrap='word', state='disabled', font=('Consolas', 9))
        self.log.pack(fill='both', expand=True, padx=10, pady=(6, 10))
        self._say("Pick one or more AKAI disk images (.iso/.img/raw) and an output folder,\n"
                  "then Extract. Output is out/<disc>/<partition>_<volume>/<sample>.wav\n"
                  "plus a manifest.tsv with root key, tuning and loop points.\n")

    # ---- thread-safe logging (Tk widgets touched only on the main loop) ----
    def _say(self, s):
        self.root.after(0, self._say_ui, s)

    def _say_ui(self, s):
        self.log.configure(state='normal')
        self.log.insert('end', s)
        self.log.see('end')
        self.log.configure(state='disabled')

    def pick_files(self):
        fs = filedialog.askopenfilenames(
            title="Select AKAI disk images",
            filetypes=[("AKAI disk image", "*.iso *.img *.bin *.hds"),
                       ("All files", "*.*")])
        for f in fs:
            if f not in self.images:
                self.images.append(f)
                self.lst.insert('end', os.path.basename(f))
        if self.images and not self.outdir.get():
            self.outdir.set(os.path.join(os.path.dirname(self.images[0]), "akai_out"))

    def clear_files(self):
        self.images = []
        self.lst.delete(0, 'end')

    def pick_out(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.outdir.set(d)

    def start(self):
        if not self.images:
            return messagebox.showwarning("No images", "Add at least one disk image.")
        if not self.outdir.get():
            return messagebox.showwarning("No output", "Choose an output folder.")
        self.go.configure(state='disabled')
        self.prog.start(12)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        stream = _LogStream(self._say)
        old_stdout = sys.stdout
        try:
            for img in self.images:
                self._say(f"\n▶ {os.path.basename(img)}\n")
                args = _Args(
                    image=img,
                    outdir=self.outdir.get(),
                    partition=None,
                    volume=(self.volume.get().strip() or None),
                    loops=self.loops.get(),
                    stereo=("join" if self.join_stereo.get() else "split"),
                    rate=(44100 if self.resample.get() else None),
                    no_smpl=False,
                    no_manifest=False,
                    force=self.overwrite.get(),
                    verbose=True,
                )
                try:
                    sys.stdout = stream
                    akaidisk.cmd_extract(args)
                    stream.flush()
                except Exception as e:
                    sys.stdout = old_stdout
                    self._say(f"    ✗ error: {e}\n{traceback.format_exc()}\n")
                finally:
                    sys.stdout = old_stdout
            self._say("\nDone.\n")
        finally:
            sys.stdout = old_stdout
            self.root.after(0, self._finish_ui)

    def _finish_ui(self):
        self.prog.stop()
        self.go.configure(state='normal')


if __name__ == '__main__':
    r = tk.Tk()
    App(r)
    r.mainloop()
