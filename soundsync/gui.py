#!/usr/bin/env python3
"""
The desktop front end.

Everything the command line can do is here, plus the thing the command line
can't do well: tell you what your chosen export settings are about to do to
your specific footage, before you spend an hour finding out.

Threading rule: all work happens on a worker thread and reports through a
queue; only the main thread touches a widget. tkinter tolerates nothing else.
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

from . import __version__, compat, csvout, fcpxml, matcher, pipeline, proc
from . import clips as clipmod
from .options import (ClipOptions, JobOptions, MatchOptions, XmlOptions,
                      last_used_path, settings_dir)
from .report import QueueReporter

PAD = 8

LEVEL_COLOURS = {
    compat.SEVERE:   ("#7f1020", "#fdecef"),
    compat.WARN:     ("#7a4a00", "#fff5e3"),
    compat.UNTESTED: ("#3f3f7a", "#eeeef9"),
    compat.NOTE:     ("#234a63", "#eef4f9"),
    compat.OK:       ("#1b5e20", "#edf7ed"),
}

LOG_COLOURS = {
    "error":  "#b3261e",
    "warn":   "#8a5300",
    "good":   "#1b5e20",
    "detail": "#6b6b6b",
    "info":   "#101010",
    "stage":  "#0b3d61",
}

# The same hues lifted for dark backgrounds. Aqua's dark mode hands tk.Text a
# near-black background, against which the palette above all but disappears.
LOG_COLOURS_DARK = {
    "error":  "#ff8a80",
    "warn":   "#ffb74d",
    "good":   "#81c784",
    "detail": "#9e9e9e",
    "info":   "#e8e8e8",
    "stage":  "#64b5f6",
}

# Font families are a platform property: Segoe UI and Consolas exist only on
# Windows, and 9pt is Windows sizing -- macOS draws its native UI at 13pt, so
# Tk's silent fallback comes out a third smaller than everything around it.
if sys.platform == "darwin":
    FONT_BOLD = ("Helvetica Neue", 13, "bold")
    FONT_MONO = ("Menlo", 12)
elif os.name == "nt":
    FONT_BOLD = ("Segoe UI", 9, "bold")
    FONT_MONO = ("Consolas", 9)
else:
    FONT_BOLD = ("DejaVu Sans", 10, "bold")
    FONT_MONO = ("DejaVu Sans Mono", 10)
FONT_MONO_BOLD = FONT_MONO + ("bold",)


def _open_folder(path):
    if not path or not os.path.isdir(path):
        return
    if os.name == "nt":
        os.startfile(path)                                    # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Sound Syncing {__version__}")
        self.geometry("1000x860")
        self.minsize(880, 700)

        self.queue = queue.Queue()
        self.canceller = proc.Canceller()
        self.worker = None
        self.scanned = None            # last scan() result
        self.last_matches = None       # last matches dict, for re-export
        self._refresh_job = None

        # Hint greys and note blues picked for a white window vanish against
        # a dark one (macOS dark mode). Resolve the actual theme once.
        self.dark = self._theme_is_dark()
        self.hint = "#9e9e9e" if self.dark else "#666"
        self.note = "#8fc1e3" if self.dark else "#234a63"

        self._make_vars()
        self._build_menu()
        self._build_widgets()
        self._load_last_settings()
        self._on_settings_changed()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)
        self.after(300, self._check_tools)
        self.after(120, self._place_sash)

    def _place_sash(self):
        """Give the log a real share of the window on first draw. Left alone,
        the paned window hands the notebook its full requested height (the
        tallest tab) and the log ends up one line high."""
        self.update_idletasks()
        total = self.split.winfo_height()
        if total < 200:                      # not laid out yet
            self.after(120, self._place_sash)
            return
        top = self.nametowidget(self.split.panes()[0]).winfo_reqheight()
        self.split.sashpos(0, min(top, int(total * 0.55)))

    def _check_tools(self):
        """Prove ffprobe actually runs, not just that it exists. On macOS an
        unsigned download's bundled ffmpeg gets killed by Gatekeeper, which
        otherwise shows up as every clip being 'skipped' with no explanation."""
        def work():
            problem = proc.self_test()
            if problem:
                self.queue.put(("log", problem, "error"))
                self.queue.put(("box", "ffmpeg cannot run", problem))
        threading.Thread(target=work, daemon=True).start()

    def _theme_is_dark(self):
        """Whether the window background is dark (macOS dark mode, dark themes).

        winfo_rgb resolves system colour names like Aqua's
        systemWindowBackgroundColor to what is actually on screen.
        """
        try:
            bg = (ttk.Style(self).lookup("TLabel", "background")
                  or self.cget("background"))
            r, g, b = self.winfo_rgb(bg)
            return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 0x8000
        except tk.TclError:
            return False

    # ------------------------------------------------------------ variables

    def _make_vars(self):
        d = MatchOptions()
        x = XmlOptions()
        c = ClipOptions()

        self.v = {
            "video_dir": tk.StringVar(),
            "audio_dir": tk.StringVar(),
            "dest_dir": tk.StringVar(),
            "recursive": tk.BooleanVar(value=d.recursive),

            "make_xml": tk.BooleanVar(value=True),
            "make_clips": tk.BooleanVar(value=False),
            "make_csv": tk.BooleanVar(value=False),
            "xml_name": tk.StringVar(value="merged.xml"),
            "csv_name": tk.StringVar(value="matches.csv"),
            "clips_subfolder": tk.StringVar(value="Synced"),
            "matches_name": tk.StringVar(value="matches.json"),

            "min_z": tk.DoubleVar(value=d.min_z),
            "min_prominence": tk.DoubleVar(value=d.min_prominence),
            "one_to_one": tk.BooleanVar(value=d.one_to_one),
            "match_jobs": tk.IntVar(value=d.jobs),

            "structure": tk.StringVar(value=x.structure),
            "bin": tk.StringVar(value=x.bin),
            "video_bin": tk.StringVar(value=x.video_bin),
            "audio_bin": tk.StringVar(value=x.audio_bin),
            "sequence_bin": tk.StringVar(value=x.sequence_bin),
            "xml_jam": tk.BooleanVar(value=x.jam_timecode),
            "xml_keep_cam": tk.BooleanVar(value=x.keep_camera_audio),
            "xml_unmatched": tk.BooleanVar(value=x.include_unmatched),

            "video_codec": tk.StringVar(value=clipmod.VIDEO_CODECS[c.video_codec].label),
            "container": tk.StringVar(value=c.container),
            "audio_codec": tk.StringVar(value=clipmod.AUDIO_CODECS[c.audio_codec]),
            "suffix": tk.StringVar(value=c.suffix),
            "clip_keep_cam": tk.BooleanVar(value=c.keep_camera_audio),
            "pad": tk.BooleanVar(value=c.pad),
            "verify": tk.BooleanVar(value=c.verify),
            "fallback": tk.BooleanVar(value=c.fallback),
            "clip_jam": tk.BooleanVar(value=c.jam_timecode),
            "dry_run": tk.BooleanVar(value=c.dry_run),
            "clip_jobs": tk.IntVar(value=c.jobs),
        }

        self.codec_by_label = {s.label: k for k, s in clipmod.VIDEO_CODECS.items()}
        self.acodec_by_label = {v: k for k, v in clipmod.AUDIO_CODECS.items()}

        # Anything that changes what the export will do re-runs the advisory.
        for name in ("video_codec", "container", "audio_codec", "dest_dir",
                     "make_xml", "make_clips", "make_csv", "structure", "xml_jam",
                     "clips_subfolder"):
            self.v[name].trace_add("write", lambda *_: self._schedule_refresh())

    # ---------------------------------------------------------------- menu

    def _build_menu(self):
        bar = tk.Menu(self)

        filemenu = tk.Menu(bar, tearoff=0)
        filemenu.add_command(label="Save settings as preset...", command=self._save_preset)
        filemenu.add_command(label="Load settings preset...", command=self._load_preset)
        filemenu.add_separator()
        filemenu.add_command(label="Open an existing matches.json...",
                             command=self._load_matches_file)
        filemenu.add_command(label="Save pairing list as CSV...",
                             command=self._save_csv_as)
        filemenu.add_separator()
        filemenu.add_command(label="Reset to defaults", command=self._reset_defaults)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self._on_close)
        bar.add_cascade(label="File", menu=filemenu)

        helpmenu = tk.Menu(bar, tearoff=0)
        helpmenu.add_command(label="Where is ffmpeg?", command=self._show_tools)
        helpmenu.add_command(label="About", command=self._show_about)
        bar.add_cascade(label="Help", menu=helpmenu)

        self.config(menu=bar)

    # ------------------------------------------------------------- widgets

    def _build_widgets(self):
        outer = ttk.Frame(self, padding=PAD)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        self._build_paths(outer).grid(row=0, column=0, sticky="ew")
        self._build_strip(outer).grid(row=1, column=0, sticky="ew", pady=(PAD, 0))

        # Settings above, progress log below, with a sash between them: the
        # log is what you read when something goes wrong, so it must be able
        # to take most of the window, not a fixed ten lines at the bottom.
        split = ttk.PanedWindow(outer, orient="vertical")
        split.grid(row=2, column=0, sticky="nsew", pady=(PAD, 0))
        split.add(self._build_notebook(split), weight=0)
        split.add(self._build_log(split), weight=1)
        self.split = split

        self._build_buttons(outer).grid(row=3, column=0, sticky="ew", pady=(PAD, 0))

    # ---- sources and destination ----

    def _build_paths(self, parent):
        f = ttk.LabelFrame(parent, text="Folders", padding=PAD)
        f.columnconfigure(1, weight=1)

        self._folder_row(f, 0, "Video folder", "video_dir",
                         "The camera clips. Their scratch audio is what gets matched.")
        self._folder_row(f, 1, "Audio folder", "audio_dir",
                         "The separately-recorded sound. May be one roll or many.")
        self._folder_row(f, 2, "Destination", "dest_dir",
                         "Where the XML, the report and the synced clips are written.")

        opts = ttk.Frame(f)
        opts.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Checkbutton(opts, text="Include sub-folders",
                        variable=self.v["recursive"]).pack(side="left")
        self.scan_label = ttk.Label(opts, text="Not scanned yet.", foreground=self.hint)
        self.scan_label.pack(side="left", padx=(16, 0))
        return f

    def _folder_row(self, parent, row, label, key, tip):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        entry = ttk.Entry(parent, textvariable=self.v[key])
        entry.grid(row=row, column=1, sticky="ew", padx=6, pady=2)
        ttk.Button(parent, text="Browse...", width=11,
                   command=lambda k=key: self._browse(k)).grid(row=row, column=2, pady=2)
        _tooltip(entry, tip)

    def _browse(self, key):
        start = self.v[key].get() or self.v["video_dir"].get() or os.path.expanduser("~")
        chosen = filedialog.askdirectory(initialdir=start, title="Choose a folder")
        if chosen:
            self.v[key].set(os.path.normpath(chosen))
            if key in ("video_dir", "audio_dir"):
                self._maybe_guess_siblings(key, chosen)
                self._autoscan()

    def _maybe_guess_siblings(self, key, chosen):
        """
        A convenience, never a requirement: if you pick a Video folder and the
        Audio one is still empty, look for the usual sibling names. You can
        always overrule it -- the two folders are independent.
        """
        parent = os.path.dirname(os.path.normpath(chosen))
        wanted = ("audio_dir", ("Audio", "Sound", "Sounds", "Recorder", "Rec")) \
            if key == "video_dir" else \
            ("video_dir", ("Video", "Videos", "Footage", "Clips", "Camera"))
        target, names = wanted
        if self.v[target].get().strip():
            return
        for n in names:
            candidate = os.path.join(parent, n)
            if os.path.isdir(candidate):
                self.v[target].set(os.path.normpath(candidate))
                self._log(f"Also found {n}\\ next to it -- set as the "
                          f"{'audio' if target == 'audio_dir' else 'video'} folder. "
                          f"Change it if that is not right.", "detail")
                break
        if not self.v["dest_dir"].get().strip():
            self.v["dest_dir"].set(parent)

    # ---- the advisory strip ----

    def _build_strip(self, parent):
        self.strip = tk.Label(parent, text="", anchor="w", padx=10, pady=6,
                              font=FONT_BOLD, relief="flat")
        self._set_strip(compat.OK, "Pick your folders, then press Scan.")
        return self.strip

    def _set_strip(self, level, text):
        fg, bg = LEVEL_COLOURS.get(level, LEVEL_COLOURS[compat.NOTE])
        self.strip.configure(text=text, foreground=fg, background=bg)

    # ---- tabs ----

    def _build_notebook(self, parent):
        nb = ttk.Notebook(parent)
        nb.add(self._tab_output(nb), text="Output")
        nb.add(self._tab_matching(nb), text="Matching")
        nb.add(self._tab_sequences(nb), text="Sequences (XML)")
        nb.add(self._tab_media(nb), text="Media files")
        nb.add(self._tab_compat(nb), text="Compatibility")
        nb.add(self._tab_results(nb), text="Results")
        self.notebook = nb
        return nb

    def _tab_output(self, parent):
        f = ttk.Frame(parent, padding=PAD)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="What to produce", font=FONT_BOLD) \
            .grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Checkbutton(f, text="Premiere sequences (XML)  -- references your "
                                "originals, nothing is transcoded",
                        variable=self.v["make_xml"]).grid(row=1, column=0, columnspan=3,
                                                          sticky="w", pady=(6, 0))
        ttk.Label(f, text="XML filename").grid(row=2, column=0, sticky="w", padx=(24, 0))
        ttk.Entry(f, textvariable=self.v["xml_name"], width=30) \
            .grid(row=2, column=1, sticky="w", padx=6)

        ttk.Checkbutton(f, text="Synced media files  -- one finished clip per take",
                        variable=self.v["make_clips"]).grid(row=3, column=0, columnspan=3,
                                                            sticky="w", pady=(10, 0))
        ttk.Label(f, text="Clips sub-folder").grid(row=4, column=0, sticky="w", padx=(24, 0))
        ttk.Entry(f, textvariable=self.v["clips_subfolder"], width=30) \
            .grid(row=4, column=1, sticky="w", padx=6)
        ttk.Label(f, text="(blank = straight into the destination folder)",
                  foreground=self.hint).grid(row=4, column=2, sticky="w")

        ttk.Checkbutton(f, text="Pairing list (CSV)  -- which sound goes with which "
                                "clip, and how sure",
                        variable=self.v["make_csv"]).grid(row=5, column=0, columnspan=3,
                                                          sticky="w", pady=(10, 0))
        ttk.Label(f, text="CSV filename").grid(row=6, column=0, sticky="w", padx=(24, 0))
        ttk.Entry(f, textvariable=self.v["csv_name"], width=30) \
            .grid(row=6, column=1, sticky="w", padx=6)
        ttk.Label(f, text="(video, best sound, %, second choice, %)",
                  foreground=self.hint).grid(row=6, column=2, sticky="w")

        ttk.Label(f, text="Match report filename").grid(row=7, column=0, sticky="w",
                                                        pady=(10, 0))
        ttk.Entry(f, textvariable=self.v["matches_name"], width=30) \
            .grid(row=7, column=1, sticky="w", padx=6, pady=(10, 0))

        ttk.Label(f, text="Tick as many as you like -- one analysis pass feeds them "
                          "all. Tick none and it is a match-only run: it works out "
                          "the pairings, shows them on the Results tab, and touches "
                          "none of your media.", foreground=self.hint, wraplength=640,
                  justify="left") \
            .grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 0))
        return f

    def _tab_matching(self, parent):
        f = ttk.Frame(parent, padding=PAD)
        f.columnconfigure(3, weight=1)

        ttk.Label(f, text="Confidence threshold (min z)").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(f, from_=0, to=60, increment=0.5, width=8,
                    textvariable=self.v["min_z"]).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(f, text="True syncs score 20-40. Unrelated pairs rarely clear 9. "
                          "Default 12.", foreground=self.hint) \
            .grid(row=0, column=2, columnspan=2, sticky="w")

        ttk.Label(f, text="Minimum prominence").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(f, from_=1.0, to=10.0, increment=0.1, width=8,
                    textvariable=self.v["min_prominence"]) \
            .grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(f, text="How far the winning alignment must stand above the "
                          "runner-up.", foreground=self.hint) \
            .grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(f, text="Decode workers").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(f, from_=0, to=32, width=8, textvariable=self.v["match_jobs"]) \
            .grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(f, text="0 = one per core (capped at 8). Decoding is the slow part.",
                  foreground=self.hint).grid(row=2, column=2, columnspan=2, sticky="w",
                                          pady=(6, 0))

        ttk.Checkbutton(f, text="Each audio file may back only one video "
                                "(--one-to-one)",
                        variable=self.v["one_to_one"]) \
            .grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(f, text="Off by default, so one continuous recorder roll can cover "
                          "several takes.", foreground=self.hint) \
            .grid(row=4, column=0, columnspan=4, sticky="w", padx=(24, 0))

        ttk.Separator(f, orient="horizontal").grid(row=5, column=0, columnspan=4,
                                                   sticky="ew", pady=12)
        ttk.Button(f, text="Diagnose source files",
                   command=self._run_diagnose).grid(row=6, column=0, sticky="w")
        ttk.Label(f, text="Prints every audio stream in every file with its peak "
                          "level -- use it when clips are being skipped.",
                  foreground=self.hint).grid(row=6, column=1, columnspan=3, sticky="w",
                                          padx=6)
        return f

    def _tab_sequences(self, parent):
        f = ttk.Frame(parent, padding=PAD)
        f.columnconfigure(2, weight=1)

        ttk.Label(f, text="Bin structure", font=FONT_BOLD) \
            .grid(row=0, column=0, columnspan=3, sticky="w")
        for i, (val, text, why) in enumerate([
            ("subbins", "VIDEO / AUDIO / SEQUENCES sub-bins",
             "The default. Master clips for every source, plus one synced sequence each."),
            ("pairs", "One bin per matched pair",
             "For fast manual Merge Clips: open a bin, Ctrl+A, merge shortcut, Enter."),
            ("flat", "Flat -- sequences straight in the top bin",
             "No master clips at all. Tidiest for a small shoot."),
        ]):
            ttk.Radiobutton(f, text=text, value=val, variable=self.v["structure"]) \
                .grid(row=1 + i * 2, column=0, columnspan=3, sticky="w", pady=(6, 0))
            ttk.Label(f, text=why, foreground=self.hint) \
                .grid(row=2 + i * 2, column=0, columnspan=3, sticky="w", padx=(24, 0))

        ttk.Separator(f, orient="horizontal").grid(row=7, column=0, columnspan=3,
                                                   sticky="ew", pady=10)
        names = ttk.Frame(f)
        names.grid(row=8, column=0, columnspan=3, sticky="ew")
        for i, (label, key) in enumerate([("Top bin", "bin"), ("Video bin", "video_bin"),
                                          ("Audio bin", "audio_bin"),
                                          ("Sequence bin", "sequence_bin")]):
            ttk.Label(names, text=label).grid(row=i // 2, column=(i % 2) * 2,
                                              sticky="w", padx=(0, 6), pady=2)
            ttk.Entry(names, textvariable=self.v[key], width=18) \
                .grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 20), pady=2)

        ttk.Separator(f, orient="horizontal").grid(row=9, column=0, columnspan=3,
                                                   sticky="ew", pady=10)
        ttk.Checkbutton(f, text="Jam timecode -- stamp matching timecode on each pair",
                        variable=self.v["xml_jam"]).grid(row=10, column=0, columnspan=3,
                                                         sticky="w")
        ttk.Label(f, text="Anchors each recorder roll on its own hour, so Merge Clips "
                          "by Timecode and batch multicam both line up exactly.",
                  foreground=self.hint).grid(row=11, column=0, columnspan=3, sticky="w",
                                          padx=(24, 0))
        ttk.Checkbutton(f, text="Include the camera scratch audio on a muted track",
                        variable=self.v["xml_keep_cam"]) \
            .grid(row=12, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(f, text="Include unmatched clips and unused audio in the bins",
                        variable=self.v["xml_unmatched"]) \
            .grid(row=13, column=0, columnspan=3, sticky="w", pady=(6, 0))
        return f

    def _tab_media(self, parent):
        f = ttk.Frame(parent, padding=PAD)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Export format", font=FONT_BOLD) \
            .grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(f, text="Video").grid(row=1, column=0, sticky="w", pady=(6, 0))
        codec_box = ttk.Combobox(f, textvariable=self.v["video_codec"], state="readonly",
                                 width=46,
                                 values=[clipmod.VIDEO_CODECS[k].label
                                         for k in clipmod.CODEC_ORDER])
        codec_box.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(f, text="Wrapper").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(f, textvariable=self.v["container"], state="readonly", width=12,
                     values=clipmod.CONTAINERS) \
            .grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(f, text="Audio").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(f, textvariable=self.v["audio_codec"], state="readonly", width=34,
                     values=list(clipmod.AUDIO_CODECS.values())) \
            .grid(row=3, column=1, sticky="w", padx=6, pady=(6, 0))

        self.codec_note = ttk.Label(f, text="", foreground=self.hint, wraplength=820,
                                    justify="left")
        self.codec_note.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.size_note = ttk.Label(f, text="", foreground=self.note, wraplength=820,
                                   justify="left")
        self.size_note.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Separator(f, orient="horizontal").grid(row=6, column=0, columnspan=3,
                                                   sticky="ew", pady=10)

        grid = ttk.Frame(f)
        grid.grid(row=7, column=0, columnspan=3, sticky="ew")
        ttk.Label(grid, text="Filename suffix").grid(row=0, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.v["suffix"], width=16) \
            .grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(grid, text="Render workers").grid(row=0, column=2, sticky="w",
                                                    padx=(20, 0))
        ttk.Spinbox(grid, from_=0, to=32, width=6, textvariable=self.v["clip_jobs"]) \
            .grid(row=0, column=3, sticky="w", padx=6)

        checks = [
            ("clip_jam", "Jam timecode on the baked clips"),
            ("clip_keep_cam", "Keep the camera scratch audio as extra tracks"),
            ("pad", "Pad short audio with silence to the video's length"),
            ("verify", "Verify every clip after writing (decodes it back and checks "
                       "frame order)"),
            ("fallback", "If a clip fails or verifies bad, retry as MOV then as an "
                         "intra re-encode"),
            ("dry_run", "Dry run -- print the ffmpeg commands, write nothing"),
        ]
        for i, (key, text) in enumerate(checks):
            ttk.Checkbutton(f, text=text, variable=self.v[key]) \
                .grid(row=8 + i, column=0, columnspan=3, sticky="w", pady=1)

        ttk.Label(f, text="Verification is what catches the wrapper faults that leave "
                          "a file looking perfect and playing broken. Leave it on "
                          "unless you are in a hurry.", foreground=self.hint,
                  wraplength=820, justify="left") \
            .grid(row=8 + len(checks), column=0, columnspan=3, sticky="w", pady=(8, 0))
        return f

    def _tab_compat(self, parent):
        f = ttk.Frame(parent, padding=PAD)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        ttk.Label(f, text="What these settings will do to your footage. Scan your "
                          "source folders to fill this in.", foreground=self.hint) \
            .grid(row=0, column=0, sticky="w", pady=(0, 6))

        cols = ("level", "title", "files", "evidence")
        tree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for col, text, width in [("level", "Verdict", 110), ("title", "Finding", 520),
                                 ("files", "Files", 60), ("evidence", "How we know", 110)]:
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w",
                        stretch=(col == "title"))
        tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, orient="vertical", command=tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        tree.configure(yscrollcommand=sb.set)
        for level, (fg, bg) in LEVEL_COLOURS.items():
            tree.tag_configure(level, foreground=fg, background=bg)
        tree.bind("<<TreeviewSelect>>", self._on_finding_selected)
        self.compat_tree = tree

        self.compat_detail = tk.Text(f, height=9, wrap="word", relief="solid",
                                     borderwidth=1, padx=8, pady=6)
        self.compat_detail.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.compat_detail.configure(state="disabled")
        self.findings = []
        return f

    def _tab_results(self, parent):
        f = ttk.Frame(parent, padding=PAD)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)

        cols = ("video", "audio", "conf", "second", "conf2", "offset", "z", "status")
        tree = ttk.Treeview(f, columns=cols, show="headings", height=12)
        for col, text, width in [("video", "Clip", 200), ("audio", "Sound", 170),
                                 ("conf", "%", 55), ("second", "Second choice", 150),
                                 ("conf2", "%", 55), ("offset", "Offset", 80),
                                 ("z", "z", 60), ("status", "Status", 200)]:
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w", stretch=(col in ("video", "status")))
        tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, orient="vertical", command=tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=sb.set)
        tree.tag_configure("ok", foreground="#1b5e20")
        tree.tag_configure("bad", foreground="#b3261e")
        tree.tag_configure("warn", foreground="#8a5300")
        self.results_tree = tree
        return f

    # ---- log ----

    def _build_log(self, parent):
        f = ttk.LabelFrame(parent, text="Progress", padding=PAD)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)

        self.progress = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status = ttk.Label(f, text="Ready.")
        self.status.grid(row=1, column=0, sticky="w", pady=(4, 4))

        wrap = ttk.Frame(f)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        # Background pinned rather than themed, so it always agrees with
        # whichever palette we picked.
        colours = LOG_COLOURS_DARK if self.dark else LOG_COLOURS
        self.log = tk.Text(wrap, height=14, wrap="word", relief="solid", borderwidth=1,
                           font=FONT_MONO, padx=6, pady=4,
                           background="#1e1e1e" if self.dark else "#ffffff",
                           foreground=colours["info"],
                           insertbackground=colours["info"])
        self.log.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=ysb.set)
        for level, colour in colours.items():
            self.log.tag_configure(level, foreground=colour)
        self.log.tag_configure("stage", font=FONT_MONO_BOLD)
        self.log.configure(state="disabled")
        return f

    def _build_buttons(self, parent):
        f = ttk.Frame(parent)
        self.btn_scan = ttk.Button(f, text="Scan sources", command=self._run_scan)
        self.btn_scan.pack(side="left")
        self.btn_run = ttk.Button(f, text="Run", command=self._run_all)
        self.btn_run.pack(side="left", padx=(6, 0))
        self.btn_reexport = ttk.Button(f, text="Re-export only", state="disabled",
                                       command=self._run_reexport)
        self.btn_reexport.pack(side="left", padx=(6, 0))
        self.btn_cancel = ttk.Button(f, text="Cancel", state="disabled",
                                     command=self._cancel)
        self.btn_cancel.pack(side="left", padx=(6, 0))

        ttk.Button(f, text="Open destination",
                   command=lambda: _open_folder(self.v["dest_dir"].get())) \
            .pack(side="right")
        ttk.Button(f, text="Clear log", command=self._clear_log) \
            .pack(side="right", padx=(0, 6))
        return f

    # ------------------------------------------------------------- settings

    def _collect(self):
        return JobOptions(
            video_dir=self.v["video_dir"].get().strip(),
            audio_dir=self.v["audio_dir"].get().strip(),
            dest_dir=self.v["dest_dir"].get().strip(),
            make_xml=self.v["make_xml"].get(),
            make_clips=self.v["make_clips"].get(),
            make_csv=self.v["make_csv"].get(),
            matches_name=self.v["matches_name"].get().strip() or "matches.json",
            xml_name=self.v["xml_name"].get().strip() or "merged.xml",
            csv_name=self.v["csv_name"].get().strip() or "matches.csv",
            clips_subfolder=self.v["clips_subfolder"].get().strip(),
            match=MatchOptions(
                min_z=_num(self.v["min_z"], 12.0),
                min_prominence=_num(self.v["min_prominence"], 1.2),
                one_to_one=self.v["one_to_one"].get(),
                jobs=_int(self.v["match_jobs"], 0),
                recursive=self.v["recursive"].get()),
            xml=XmlOptions(
                bin=self.v["bin"].get() or "Merged",
                video_bin=self.v["video_bin"].get() or "VIDEO",
                audio_bin=self.v["audio_bin"].get() or "AUDIO",
                sequence_bin=self.v["sequence_bin"].get() or "SEQUENCES",
                structure=self.v["structure"].get(),
                include_unmatched=self.v["xml_unmatched"].get(),
                jam_timecode=self.v["xml_jam"].get(),
                keep_camera_audio=self.v["xml_keep_cam"].get()),
            clips=ClipOptions(
                container=self.v["container"].get(),
                video_codec=self.codec_by_label.get(self.v["video_codec"].get(), "copy"),
                audio_codec=self.acodec_by_label.get(self.v["audio_codec"].get(),
                                                     "pcm_s24le"),
                suffix=self.v["suffix"].get(),
                keep_camera_audio=self.v["clip_keep_cam"].get(),
                pad=self.v["pad"].get(),
                verify=self.v["verify"].get(),
                fallback=self.v["fallback"].get(),
                jam_timecode=self.v["clip_jam"].get(),
                jobs=_int(self.v["clip_jobs"], 0),
                dry_run=self.v["dry_run"].get()),
        )

    def _apply(self, job):
        v = self.v
        v["video_dir"].set(job.video_dir)
        v["audio_dir"].set(job.audio_dir)
        v["dest_dir"].set(job.dest_dir)
        v["make_xml"].set(job.make_xml)
        v["make_clips"].set(job.make_clips)
        v["make_csv"].set(job.make_csv)
        v["matches_name"].set(job.matches_name)
        v["xml_name"].set(job.xml_name)
        v["csv_name"].set(job.csv_name)
        v["clips_subfolder"].set(job.clips_subfolder)

        m = job.match
        v["min_z"].set(m.min_z)
        v["min_prominence"].set(m.min_prominence)
        v["one_to_one"].set(m.one_to_one)
        v["match_jobs"].set(m.jobs)
        v["recursive"].set(m.recursive)

        x = job.xml
        v["structure"].set(x.structure)
        v["bin"].set(x.bin)
        v["video_bin"].set(x.video_bin)
        v["audio_bin"].set(x.audio_bin)
        v["sequence_bin"].set(x.sequence_bin)
        v["xml_jam"].set(x.jam_timecode)
        v["xml_keep_cam"].set(x.keep_camera_audio)
        v["xml_unmatched"].set(x.include_unmatched)

        c = job.clips
        spec = clipmod.VIDEO_CODECS.get(c.video_codec, clipmod.VIDEO_CODECS["copy"])
        v["video_codec"].set(spec.label)
        v["container"].set(c.container)
        v["audio_codec"].set(clipmod.AUDIO_CODECS.get(c.audio_codec,
                                                      clipmod.AUDIO_CODECS["pcm_s24le"]))
        v["suffix"].set(c.suffix)
        v["clip_keep_cam"].set(c.keep_camera_audio)
        v["pad"].set(c.pad)
        v["verify"].set(c.verify)
        v["fallback"].set(c.fallback)
        v["clip_jam"].set(c.jam_timecode)
        v["clip_jobs"].set(c.jobs)
        v["dry_run"].set(c.dry_run)

    def _load_last_settings(self):
        path = last_used_path()
        if os.path.isfile(path):
            try:
                self._apply(JobOptions.load(path))
            except Exception:                                  # noqa: BLE001
                pass

    def _save_last_settings(self):
        try:
            os.makedirs(settings_dir(), exist_ok=True)
            self._collect().save(last_used_path())
        except Exception:                                      # noqa: BLE001
            pass

    def _save_preset(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("Settings preset", "*.json")],
            initialfile="soundsync-preset.json", title="Save these settings")
        if path:
            self._collect().save(path)
            self._log(f"Settings saved to {path}", "good")

    def _load_preset(self):
        path = filedialog.askopenfilename(
            filetypes=[("Settings preset", "*.json"), ("All files", "*.*")],
            title="Load settings")
        if path:
            try:
                self._apply(JobOptions.load(path))
                self._log(f"Settings loaded from {path}", "good")
                self._on_settings_changed()
            except Exception as e:                             # noqa: BLE001
                messagebox.showerror("Could not load settings", str(e))

    def _reset_defaults(self):
        keep = (self.v["video_dir"].get(), self.v["audio_dir"].get(),
                self.v["dest_dir"].get())
        job = JobOptions()
        job.video_dir, job.audio_dir, job.dest_dir = keep
        self._apply(job)
        self._on_settings_changed()
        self._log("Settings reset to defaults. Folders kept.", "info")

    def _load_matches_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("Match report", "*.json"), ("All files", "*.*")],
            title="Open an existing matches.json")
        if not path:
            return
        try:
            data = fcpxml.load_matches(path)
        except Exception as e:                                 # noqa: BLE001
            messagebox.showerror("Could not read that file", str(e))
            return
        self.last_matches = data
        medias = [m for m in data.get("media", {}).values() if m.get("has_video")]
        self.scanned = {"videos": medias,
                        "audios": [m for m in data.get("media", {}).values()
                                   if not m.get("has_video")],
                        "errors": []}
        self.btn_reexport.configure(state="normal")
        self._log(f"Loaded {len(data.get('matches', []))} match(es) from {path}. "
                  f"Use 'Re-export only' to build outputs without re-analysing.",
                  "good")
        self._fill_results(data)
        self._on_settings_changed()

    def _save_csv_as(self):
        """The pairing list from whatever analysis is already in hand -- for
        when you ran a match and only afterwards wanted the spreadsheet."""
        if not self.last_matches:
            messagebox.showinfo("Nothing to save yet",
                                "Run a match first, or open an existing "
                                "matches.json from this menu.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=self.v["csv_name"].get() or "matches.csv",
            initialdir=self.v["dest_dir"].get() or os.path.expanduser("~"),
            filetypes=[("Spreadsheet", "*.csv"), ("All files", "*.*")],
            title="Save the pairing list")
        if not path:
            return
        try:
            out = csvout.write_csv(self.last_matches, path)
        except OSError as e:
            messagebox.showerror("Could not write that file", str(e))
            return
        self._log(f"Wrote {out['rows']} row(s) to {out['out_path']}", "good")

    # -------------------------------------------------------------- compat

    def _schedule_refresh(self):
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
        self._refresh_job = self.after(150, self._on_settings_changed)

    def _on_settings_changed(self):
        self._refresh_job = None
        job = self._collect()
        spec = clipmod.spec_for(job.clips)

        note = spec.note or ""
        if job.clips.container != "auto" and job.clips.container not in spec.containers:
            note = (f"{spec.label} is not normally carried in a "
                    f"{job.clips.container.upper()}. " + note)
        self.codec_note.configure(text=note)

        findings = []
        medias = (self.scanned or {}).get("videos") or []
        if job.make_clips and medias:
            findings += compat.analyze(medias, job.clips, job.clips_dir())
        if job.make_xml:
            findings += compat.xml_findings(job.xml)
        findings.sort(key=lambda f: compat.RANK.get(f.level, 9))
        self._fill_compat(findings)

        if medias and job.make_clips:
            forecast = compat.disk_forecast(medias, job.clips, job.clips_dir())
            self.size_note.configure(text=forecast.detail if forecast else "")
        elif medias:
            total = sum(m.get("file_size") or 0 for m in medias)
            self.size_note.configure(
                text=f"Source footage: {compat.human_bytes(total)}. The XML route "
                     f"writes none of it again.")
        else:
            self.size_note.configure(text="")

        if not job.make_xml and not job.make_clips:
            self._set_strip(
                compat.OK,
                "Match only -- your media is read and nothing is written to it. "
                + ("The pairing list goes out as CSV."
                   if job.make_csv else "The pairings appear on the Results tab."))
        elif not medias:
            self._set_strip(compat.NOTE,
                            "Scan your source folders to check these export settings "
                            "against your actual footage.")
        else:
            level, text = compat.headline(findings)
            self._set_strip(level, text)

    def _fill_compat(self, findings):
        self.findings = findings
        tree = self.compat_tree
        tree.delete(*tree.get_children())
        for i, f in enumerate(findings):
            tree.insert("", "end", iid=str(i), tags=(f.level,),
                        values=(f.label, f.title,
                                str(len(f.files)) if f.files else "-", f.evidence))
        self._set_detail("Select a line to read the detail.")

    def _on_finding_selected(self, _event=None):
        sel = self.compat_tree.selection()
        if not sel:
            return
        f = self.findings[int(sel[0])]
        text = f"{f.label}  --  {f.title}\n\n{f.detail}\n\nHow we know: {f.evidence}."
        if f.files:
            shown = ", ".join(f.files[:12])
            more = f" (+{len(f.files) - 12} more)" if len(f.files) > 12 else ""
            text += f"\n\nAffects {len(f.files)} file(s): {shown}{more}"
        self._set_detail(text)

    def _set_detail(self, text):
        self.compat_detail.configure(state="normal")
        self.compat_detail.delete("1.0", "end")
        self.compat_detail.insert("1.0", text)
        self.compat_detail.configure(state="disabled")

    # ------------------------------------------------------------- results

    def _fill_results(self, data):
        tree = self.results_tree
        tree.delete(*tree.get_children())
        for p in data.get("matches", []):
            tree.insert("", "end", tags=("ok",), values=self._result_row(p, "matched"))
        for p in data.get("rejected", []):
            if p["video"] in data.get("unmatched_video", []):
                tree.insert("", "end", tags=("warn",),
                            values=self._result_row(p, p.get("reason", "rejected"),
                                                    guess=True))
        for s in data.get("skipped", []):
            tree.insert("", "end", tags=("bad",),
                        values=(s["file"], "-", "", "", "", "-", "-", s["reason"]))

    @staticmethod
    def _result_row(p, status, guess=False):
        second = (p.get("candidates") or [{}, {}])[1:2]
        second = second[0] if second else {}
        return (p["video"],
                ("best guess: " if guess else "") + p["audio"],
                p.get("confidence", ""),
                second.get("audio", ""), second.get("confidence", ""),
                f"{p['offset']:+.3f}s", p["z"], status)

    # --------------------------------------------------------------- running

    def _busy(self, on, what=""):
        state = "disabled" if on else "normal"
        for b in (self.btn_scan, self.btn_run):
            b.configure(state=state)
        self.btn_reexport.configure(
            state="disabled" if on else ("normal" if self.last_matches else "disabled"))
        self.btn_cancel.configure(state="normal" if on else "disabled")
        if on:
            self.status.configure(text=what or "Working...")
        self.progress.configure(value=0)

    def _start(self, fn, what):
        if self.worker and self.worker.is_alive():
            return
        self.canceller.reset()
        self._busy(True, what)
        reporter = QueueReporter(self.queue)

        def wrapped():
            try:
                fn(reporter)
            except proc.Cancelled:
                self.queue.put(("log", "Cancelled.", "warn"))
            except proc.ToolsMissing as e:
                self.queue.put(("log", str(e), "error"))
                self.queue.put(("box", "ffmpeg not found", str(e)))
            except (ValueError, RuntimeError, OSError) as e:
                self.queue.put(("log", str(e), "error"))
                self.queue.put(("box", "Could not finish", str(e)))
            except Exception:                                  # noqa: BLE001
                tb = traceback.format_exc()
                self.queue.put(("log", tb, "error"))
                self.queue.put(("box", "Unexpected error", tb.splitlines()[-1]))
            finally:
                self.queue.put(("done", None, None))

        self.worker = threading.Thread(target=wrapped, daemon=True)
        self.worker.start()

    def _cancel(self):
        self.canceller.cancel()
        self.status.configure(text="Cancelling...")

    def _run_scan(self):
        job = self._collect()
        if not job.video_dir and not job.audio_dir:
            messagebox.showinfo("Nothing to scan",
                                "Pick a video folder and an audio folder first.")
            return

        def work(reporter):
            reporter.stage("Scanning sources")
            found = pipeline.scan(job.video_dir, job.audio_dir, job.match.recursive,
                                  reporter, self.canceller)
            self.queue.put(("scanned", found, None))

        self._start(work, "Scanning source folders...")

    def _autoscan(self):
        if self.v["video_dir"].get() and self.v["audio_dir"].get():
            self._run_scan()

    def _run_diagnose(self):
        job = self._collect()
        if not job.video_dir:
            messagebox.showinfo("Pick a video folder first", "Nothing to diagnose yet.")
            return

        def work(reporter):
            vpaths = matcher.gather(job.video_dir, matcher.VIDEO_EXT, job.match.recursive)
            matcher.diagnose(vpaths, "VIDEO", reporter, self.canceller)
            if job.audio_dir:
                apaths = matcher.gather(job.audio_dir, matcher.AUDIO_EXT,
                                        job.match.recursive)
                matcher.diagnose(apaths, "AUDIO", reporter, self.canceller)

        self._start(work, "Diagnosing source files...")

    def _confirm_severe(self):
        bad = [f for f in self.findings if f.level == compat.SEVERE]
        if not bad:
            return True
        lines = "\n\n".join(f"{f.title}\n    {f.detail[:220]}..." for f in bad[:3])
        return messagebox.askyesno(
            "These settings have known problems",
            f"{len(bad)} thing(s) about this export are known not to work:\n\n"
            f"{lines}\n\nRun anyway?", icon="warning", default="no")

    def _run_all(self):
        job = self._collect()
        problems = pipeline.validate(job)
        if problems:
            messagebox.showinfo("Not ready yet", "\n".join(problems))
            return
        if job.make_clips and not self._confirm_severe():
            return

        def work(reporter):
            result = pipeline.run_job(job, reporter, self.canceller)
            self.queue.put(("finished", result, None))

        self._clear_log()
        self._start(work, "Running...")

    def _run_reexport(self):
        job = self._collect()
        if not self.last_matches:
            return
        problems = [p for p in pipeline.validate(job)
                    if not p.startswith(("Pick a video", "Pick an audio"))]
        if problems:
            messagebox.showinfo("Not ready yet", "\n".join(problems))
            return
        if job.make_clips and not self._confirm_severe():
            return

        def work(reporter):
            result = pipeline.run_job(job, reporter, self.canceller,
                                      existing_matches=self.last_matches)
            self.queue.put(("finished", result, None))

        self._start(work, "Re-exporting from the existing match report...")

    # ----------------------------------------------------------------- pump

    def _poll(self):
        try:
            while True:
                kind, a, b = self.queue.get_nowait()
                if kind == "log":
                    self._log(a, b)
                elif kind == "stage":
                    self._log(f"=== {a} ===" + (f"  {b}" if b else ""), "stage")
                    self.status.configure(text=a + (f" -- {b}" if b else ""))
                elif kind == "progress":
                    total = b or 0
                    self.progress.configure(
                        mode="determinate" if total else "indeterminate",
                        value=(a / total * 100) if total else 0)
                elif kind == "row":
                    pass                       # results table filled at the end
                elif kind == "scanned":
                    self._on_scanned(a)
                elif kind == "finished":
                    self._on_finished(a)
                elif kind == "box":
                    messagebox.showerror(a, b)
                elif kind == "done":
                    self._busy(False)
                    self.status.configure(text="Ready.")
                    self.progress.configure(value=0)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _on_scanned(self, found):
        self.scanned = found
        nv, na = len(found["videos"]), len(found["audios"])
        kinds = compat.group_sources(found["videos"])
        summary = f"{nv} video, {na} audio"
        if kinds:
            summary += "  |  " + "; ".join(
                f"{len(g['files'])}x {compat.describe(g['traits'])}" for g in kinds[:3])
            if len(kinds) > 3:
                summary += f"; +{len(kinds) - 3} more kinds"
        self.scan_label.configure(text=summary)
        for g in kinds:
            self._log(f"{len(g['files'])} file(s): {compat.describe(g['traits'])}", "info")
        self._on_settings_changed()

    def _on_finished(self, result):
        data = result.get("matches")
        if data:
            self.last_matches = data
            self._fill_results(data)
            self.btn_reexport.configure(state="normal")
        clips = result.get("clips")
        if clips and clips.get("failed"):
            self._log(f"{len(clips['failed'])} clip(s) failed -- see above.", "error")

    # ------------------------------------------------------------------ log

    def _log(self, message, level="info"):
        self.log.configure(state="normal")
        for line in str(message).rstrip("\n").split("\n"):
            self.log.insert("end", line + "\n", level if level in LOG_COLOURS else "info")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---------------------------------------------------------------- help

    def _show_tools(self):
        ok, lines = proc.tools_report()
        messagebox.showinfo("ffmpeg", "\n".join(lines) +
                            ("" if ok else "\n\n" + proc._missing_message()))

    def _show_about(self):
        messagebox.showinfo(
            "Sound Syncing",
            f"Sound Syncing {__version__}\n\n"
            "Matches camera clips to separately-recorded audio on their waveforms, "
            "then hands Premiere either synced sequences that reference your "
            "originals, or finished media files.\n\n"
            "No timecode required.")

    # --------------------------------------------------------------- closing

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Still working",
                                       "Something is still running. Stop it and quit?"):
                return
            self.canceller.cancel()
        self._save_last_settings()
        self.destroy()


def _tooltip(widget, text):
    """Small hover label. Deliberately plain -- it exists to explain a field,
    not to be noticed."""
    tip = {"win": None}

    def show(_e=None):
        if tip["win"] or not text:
            return
        x = widget.winfo_rootx() + 20
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        # Both colours pinned: in dark mode the default label text is white,
        # which would vanish against the pinned yellow.
        tk.Label(win, text=text, background="#ffffe0", foreground="#000000",
                 relief="solid", borderwidth=1,
                 justify="left", padx=6, pady=3, wraplength=420).pack()
        tip["win"] = win

    def hide(_e=None):
        if tip["win"]:
            tip["win"].destroy()
            tip["win"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)


def _num(var, default):
    try:
        return float(var.get())
    except (tk.TclError, ValueError):
        return default


def _int(var, default):
    try:
        return int(var.get())
    except (tk.TclError, ValueError):
        return default


def main():
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
