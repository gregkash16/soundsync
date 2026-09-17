# Sound Syncing

Takes a folder of camera clips and a folder of separately-recorded audio, works
out which audio belongs to which clip and by how much they're offset, then hands
Premiere either synced sequences or finished media files — or just tells you
which sound goes with which clip and leaves your media alone.

No timecode required. Matching is done on the audio waveform.

## Download

Everything is bundled — no Python or ffmpeg install required on any platform.
Both builds are unsigned, so each OS warns once on first launch; the two lines
below tell you the click-through.

**Windows** — grab **SoundSync-Setup-\<version\>.exe** from the
[latest release](https://github.com/gregkash16/soundsync/releases/latest) and
run it. It installs per-user (no admin needed) and adds a Start Menu entry. A
portable zip of the same build is attached to each release if you'd rather not
install. SmartScreen will warn on first run: click "More info", then
"Run anyway".

**Mac (Apple Silicon)** — grab **SoundSync-\<version\>.dmg** from the same
release, open it, and drag **SoundSync** into **Applications**. On first launch
macOS will say it "could not verify" the app: go to **System Settings →
Privacy & Security**, scroll down, and click **Open Anyway**. That's a
one-time step. Intel Macs aren't covered by this build — build from source
there (see `build.py`).

There are two ways to use it: **the app**, which is what most of this file is
about, and **the command line**, which does the same work with flags.

---

## The app

Double-click **SoundSync.exe** (packaged build) or **SoundSync.bat** (running
from source). One window, everything in it.

1. **Folders** — pick your video folder and your audio folder separately. They
   don't have to sit under a common parent or be called anything in particular.
   Pick a destination for the output. If you point at a video folder and leave
   audio empty, it offers the usual sibling name as a guess; overrule it freely.
2. Press **Scan sources**. This probes every file — a couple of seconds, no
   decoding — and tells you what you've actually got: *39x H.264 4:2:2 10-bit
   long-GOP .mxf*, and so on.
3. Choose what to produce on the **Output** tab: Premiere sequences, synced
   media files, a CSV pairing list, any combination — one analysis pass feeds
   them all. Tick nothing at all for a match-only run that writes no media.
4. Check the coloured strip under the folders. It's the summary of the
   **Compatibility** tab, which is the part worth reading before a long run.
5. Press **Run**.

**Re-export only** reruns just the export stages against the last analysis. Use
it when you want the same matches in a different format — switching from ProRes
to DNxHR shouldn't mean fingerprinting 39 clips again.

Settings persist between sessions, and **File > Save settings as preset** keeps
named ones for different cameras or jobs.

---

## The Compatibility tab

This is here because the worst failure on this project was invisible. An output
that opened cleanly, probed clean, reported the right duration and the right
frame rate — and had a third of its frames missing, in the wrong order. Nothing
warned about it.

So before anything is written, your sources are probed and checked against what
is actually known about wrappers, essences and Premiere's importers. Every
finding says how confident it is:

| | meaning |
|---|---|
| **verified** | measured on real footage through this tool |
| **inferred** | follows from a documented ffmpeg or Premiere limitation |
| **untested** | this combination has never been put through Premiere |

**untested is not the same as fine.** A combination nobody has tried gets
labelled as such rather than being quietly presented as safe.

Findings never block a run. If you want the MXF, you get the MXF — with a red
line telling you what you're about to get.

---

## Choosing an output

Tick as many as you like on the **Output** tab — one analysis pass feeds all of
them. Tick **none** and you get a match-only run: it works out what pairs with
what, fills the Results tab and writes `matches.json`, and touches none of your
media.

### Match only — just tell me what goes with what

For when you'd rather merge in Premiere yourself and only want the answer to
"which WAV belongs to this clip?". Nothing is written except the report, so it
costs one analysis pass and no disk.

Add **Pairing list (CSV)** to get that answer as a spreadsheet, `matches.csv`:

| A | B | C | D | E |
|---|---|---|---|---|
| Video | Sound | Confidence % | Second choice | Confidence % |

One row per clip, in analysis order. Every clip appears — a rejected one carries
its best guess with *below threshold — check this one* beside it, and a clip that
couldn't be analysed at all carries the reason instead of a filename. The second
column pair is the runner-up, so a row reading `96.8` against `17.8` is settled
and one reading `41` against `38` is the one to look at yourself.

The percentage is the z-score (below) rendered readable: 50% sits at z 10, right
by the accept threshold, so anything under 50 wants your eyes on it. It is a
readability aid, not a calibrated probability.

*File > Save pairing list as CSV…* writes the same sheet from whatever analysis
is already in hand, including one loaded from an existing `matches.json` — handy
when you ran the match and only afterwards wanted the spreadsheet.

### Sequences (XML) — the default, and the one that always works

Writes `merged.xml`, which Premiere imports as:

```
Merged/
    VIDEO/       master clips for the camera files
    AUDIO/       master clips for the recorder files
    SEQUENCES/   one synced sequence per matched clip
```

Each sequence carries video on V1 and the recorder audio on A1 (A1/A2/… for
multichannel), offset so they line up. It **references your original camera
files where they already sit** — nothing is copied, converted or re-encoded, so
this route costs no disk space and no encode time, and Premiere reads the
footage through its own native importer. Verified importing into a live
Premiere install.

Two things to know:

- **They're sequences, not merged clips.** Double-clicking one opens a timeline
  rather than loading it in the Source Monitor; to use one as source, right-click
  and choose *Open in Source Monitor*. On a timeline they behave as nests.
  Premiere has no sequence-to-clip conversion, batch or otherwise.
- **Moving or renaming the source files breaks the link.** The XML stores
  absolute paths.

If the nesting matters, either of these gets you genuine master clips:

**Pair bins + Merge Clips.** Set bin structure to *one bin per matched pair* and
leave jam timecode on. Then in Premiere, once: *Edit > Keyboard Shortcuts*,
search *Merge Clips*, assign a key. And per clip: open the pair bin, **Ctrl+A**
(only two items in there), press the key, sync point **Timecode**, tick *Remove
Audio From AV Clip*, **Enter**. The dialog keeps its settings, so it's about
three keystrokes each. Merge Clips takes one video plus its audio and there is
no batch form of it in any version of Premiere — this just makes it fast.

**Batch multicam.** Keep the default sub-bins with jam timecode on, import, then
select everything in **VIDEO** and **AUDIO** and use *Clip > Create Multi-Camera
Source Sequence*, synchronize point **Timecode**, audio **Camera 1**, tick *Move
source clips to Processed Clips bin*. One dialog for the whole shoot. What comes
out behaves as media — double-click loads it in the Source Monitor. Jam timecode
is what makes this exact: Premiere aligns arithmetically instead of re-analysing
waveforms across every clip.

Premiere can also run that dialog with **Audio** as the sync point and no help
from this tool at all. Try it first. What this tool adds is the report: which
clips matched, at what confidence, and which didn't. Premiere silently leaves
unmatched clips out with no explanation, which matters at 100 clips.

### Media files — one finished clip per take

Writes ordinary media: video handled how you choose, recorder audio muxed in at
the measured offset. Import the folder and each clip behaves like any other —
double-click, Source Monitor, in and out, drag to the timeline.

The camera scratch is dropped by default, the same as ticking *Remove Audio From
AV Clip* in Merge Clips. Recorder tracks stay **separate**: a stereo or poly WAV
arrives with its channel layout intact, and a recorder holding discrete streams
(lav on one, boom on another) arrives as that many tracks. Nothing is mixed
down. Verified both ways, each landing within half a millisecond of the camera
scratch.

Whether this route works at all depends on your footage, which is what the
Compatibility tab is for. The short version:

| Source | Stream copy | What to do instead |
|---|---|---|
| 8-bit 4:2:0 H.264/H.265 | works, wrapped as MOV | — |
| **10-bit or 4:2:2 H.264** (Canon C70 XF-AVC and friends) | **no wrapper works** | re-encode to ProRes 422 HQ or DNxHR HQX |
| any long-GOP footage → MXF output | **broken frame order** | never force MXF on long-GOP |
| anything → MKV output | **Premiere can't import MKV** | use MOV |

**Video export options.** Stream copy (free, lossless, wrapper-dependent);
ProRes 422 HQ / 422 / LT / 4444; DNxHR HQX / SQ; H.264; H.265. The ProRes and
DNxHR profiles are intra-only, which is both why they're comfortable to edit and
why they're the one safe thing to put in an MXF. Expect roughly 5x the source
size and about real-time encoding — the app estimates the output size against
the free space on your destination drive before you start.

Audio is PCM 24-bit by default, 16-bit or AAC if you'd rather. MP4 output can't
carry PCM at all (ffmpeg silently promotes it to `pcm_s32le` and the result
decodes mis-timed), so AAC is substituted there — the app warns when that's
about to happen.

**Every clip is checked after it's written.** Once a file is muxed, the first few
seconds are decoded back and the frame timestamps are confirmed to be in order
and complete. A clip that fails is re-wrapped as MOV, and re-encoded to an intra
codec if that still won't do it, rather than being handed to you looking fine and
stuttering in Premiere. This is the check that would have caught the MXF problem:
those files opened cleanly and reported the correct duration — only a decode
reveals a third of the frames are gone.

---

## Why MXF is never used as an output wrapper for a stream copy

This is the one thing in here worth understanding before you change it.

ffmpeg's MXF muxer can only write the frame-reordering ("temporal offset")
information that long-GOP footage needs for the handful of intra-only formats it
was built around — AVC-Intra, D-10, DNxHD, JPEG2000. Modern camera MXF (Canon
XF-AVC, Sony XAVC Long) is long-GOP H.264 with B-frames, and the muxer silently
discards the reordering: every frame comes out claiming to be displayed in the
order it was *stored*, with duplicate decode timestamps. Each group of three
frames then plays in the wrong order with one of them dropped, which on screen
looks like someone tapping pause/play eight times a second. It is not a playback
or performance problem — the frames really are wrong in the file. Measured: 31 of
48 frames surviving two seconds.

MOV carries the same stream-copied picture with the reordering intact. Two things
make that work and both are applied automatically: `-movflags +faststart` (without
it the MOV header claims more data than was written and the file won't open at
all) and stripping the access-unit delimiters from the H.264 stream (Canon's
extradata is ~9.7 kB of Annex-B, which the MOV muxer otherwise turns into a
malformed avcC record). Neither touches picture data — output is bit-for-bit
identical to the camera original, verified frame by frame, 804/804 and 432/432.

**But** the C70 records H.264 High 4:2:2 10-bit, and Premiere reaches that
essence only through its Canon XF-AVC importer, which is keyed to the MXF
wrapper. The same stream in a MOV carries an `avc1` tag, and Premiere's
QuickTime importer handles only 8-bit 4:2:0 H.264 there. So for that footage
there is no stream-copy wrapper at all: MXF plays wrong, MOV won't open in
Premiere, MKV round-trips perfectly but Premiere can't import MKV. Baking media
from it means re-encoding to an intra codec. Referencing it with the XML route
means not re-encoding anything, which is why that's the default.

---

## Reading the match report

```
A001_C001.mp4  <-  ZOOM_LONG.wav   offset +8.412s   (96.2%, z 29.12, prom 4.68)
```

`offset` is the point in the audio file that lines up with the video's first
frame. Negative means the recorder started rolling after the camera did.

`z` is the confidence: how far the winning alignment stands above every other
possible alignment of those two files. **This is the number to watch.**

| z | shown as | meaning |
|---|---|---|
| 20+ | 89%+ | solid, trust it |
| 12–20 | 63–89% | probably right, spot-check it |
| under 12 | under 63% | rejected as unmatched |
| under 9 | under 42% | almost certainly unrelated files |

Anything rejected is listed with its best guess so you can eyeball it. Nothing is
silently dropped. The **Results** tab shows all of it — including the runner-up
audio file and its score for every clip — and `matches.json` holds the same
thing on disk, with the top three candidates per clip.

`--one-to-one` (*each audio file may back only one video*) is **off** by default,
so one continuous recorder roll can back several takes — the normal run-and-gun
situation. Turn it on if you shot strictly one audio file per clip and want the
extra safety.

---

## Building the standalone app

`build.py` produces `dist\SoundSync\` — a folder holding `SoundSync.exe`, an
embedded Python interpreter, numpy/scipy, and ffmpeg. **The machine you copy it
to needs none of those installed.** No Python, no pip, no ffmpeg, nothing on
PATH.

```
python -m pip install numpy scipy pyinstaller
python build.py --shortcut
```

- Copy the **whole folder**. The exe won't run on its own.
- **Want a proper installer instead?** `python build.py --installer` also
  compiles `installer.iss` with Inno Setup and leaves
  `dist\SoundSync-Setup-<version>.exe` — one file that installs per-user
  (no admin prompt, lands in `%LOCALAPPDATA%\Programs\Sound Syncing`) with a
  Start Menu entry, an optional desktop shortcut, an Add/Remove Programs
  listing and an uninstaller. Same app, same folder, just placed by a wizard.
  Inno Setup is needed on the *build* machine only:
  `winget install -e --id JRSoftware.InnoSetup`. Installing a newer version
  over an older one replaces it in place; the uninstaller asks before touching
  saved settings. The version number comes from `soundsync/__init__.py`.
- Roughly 300 MB, most of it ffmpeg (96 MB each for ffmpeg and ffprobe) and
  scipy.
- One-folder rather than one-file on purpose: a single 300 MB exe re-extracts
  itself to a temp directory on every launch, costing 10–20 seconds each time,
  and trips antivirus far more often.
- PyInstaller builds for the platform it runs on, so a Mac build has to be
  made on a Mac. There, the build produces `dist/SoundSync.app`, and
  `--installer` wraps it in a drag-to-Applications `dist/SoundSync-<version>.dmg`
  (via `hdiutil` — nothing to install). Mac notes: use a python.org or uv
  Python 3.12+, never the Xcode/system `python3` (its Tk 8.5 renders a blank
  window), and bundle a *static* ffmpeg/ffprobe via `--ffmpeg-dir` — Homebrew's
  is dynamically linked and dies on any machine without Homebrew. The build
  warns about both.
- `--ffmpeg-dir "C:\ffmpeg\bin"` if ffmpeg isn't on your PATH.
- The build launches its own output with `--selftest` before declaring success:
  the app imports everything, confirms it can find ffmpeg, and exits. A packaged
  app can build cleanly and still die at launch over a module PyInstaller left
  out, and a windowed exe has nowhere to print the traceback — so it writes one
  to `%TEMP%\soundsync-selftest.txt` and the build reads it back.
- **Don't exclude stdlib modules from the build.** scipy's array-API shim clones
  the whole numpy module on import, which pulls in `numpy.testing`, which imports
  `unittest`. Excluding `unittest` builds fine and then fails at launch.
- The app looks for a bundled ffmpeg beside itself first and only then falls
  back to PATH, so it's never at the mercy of some other ffmpeg installed years
  ago. **Help > Where is ffmpeg?** shows which one it found.

---

## Running from source

1. **Python 3.9+** — from python.org. Tick *Add python.exe to PATH*.
2. `python -m pip install numpy scipy`
3. **ffmpeg** — a build from https://www.gyan.dev/ffmpeg/builds/ (the
   "essentials" release zip is fine), unzipped, with its `bin` folder on your
   PATH so `ffmpeg` and `ffprobe` both work from a fresh Command Prompt.
   Alternatively drop `ffmpeg.exe` and `ffprobe.exe` into a `ffmpeg\` folder
   next to these scripts.

Then `SoundSync.bat` for the window, or the command line below.

---

## Command line

The three scripts are thin wrappers over the same code the app runs.

```
python syncmatch.py  --video "D:\Shoot\Day1\Video" --audio "D:\Shoot\Day1\Audio" --out matches.json
python makefcpxml.py matches.json --out merged.xml --jam-timecode
python makeclips.py  matches.json --out-dir "D:\Shoot\Day1\Synced" --video-codec prores_hq
```

`sync.bat` chains the first two with defaults: drag a shoot folder onto it, or
`sync.bat "D:\Shoot\Day1"`. It expects a video and an audio subfolder inside,
named any of Video/Videos/Footage/Clips/Camera and
Audio/Sound/Sounds/Recorder/Rec. For anything else, use the app or the scripts
directly.

### syncmatch.py

| Flag | Effect |
|---|---|
| `--video` `--audio` | the two source folders (required, independent) |
| `--out` | where to write the report (default `matches.json`) |
| `--csv` | also write the pairing list as a spreadsheet — bare, it lands beside `--out`; or give it a path |
| `--min-z 10` | accept marginal matches (more coverage, more risk) |
| `--min-z 18` | be stricter, if you got a wrong pairing |
| `--min-prominence` | peak-to-runner-up ratio, default 1.2 |
| `--one-to-one` | each audio file may serve only one clip |
| `--recursive` | search sub-folders too |
| `--jobs 4` | limit parallel decoding |
| `--diagnose` | report what's inside each file and stop — see below |

### makefcpxml.py

| Flag | Effect |
|---|---|
| `--out` | XML path (default `merged.xml`) |
| `--jam-timecode` | stamp matching timecode on each pair — enables Merge Clips and multicam by timecode |
| `--pair-bins` | one bin per pair — makes Merge Clips fast |
| `--flat` | no sub-bins, sequences straight in the top bin |
| `--include-unmatched` | put unmatched clips and unused audio in the bins too |
| `--keep-camera-audio` | include the scratch audio on a muted track |
| `--bin` `--video-bin` `--audio-bin` `--sequence-bin` | rename any of the bins |

### makeclips.py

| Flag | Effect |
|---|---|
| `--check` | report what these settings will do to this footage, then stop |
| `--video-codec` | `copy` (default) · `prores_hq` `prores_422` `prores_lt` `prores_4444` · `dnxhr_hqx` `dnxhr_sq` · `h264` `h265` |
| `--container` | `auto` (default) · `mov` `mxf` `mkv` `mp4` |
| `--audio-codec` | `pcm_s24le` (default) · `pcm_s16le` · `aac` |
| `--out-dir` | where the clips go |
| `--suffix` | filename suffix, default `_synced` |
| `--jam-timecode` | stamp each roll's clips with matching timecode |
| `--keep-camera-audio` | keep the scratch as extra tracks |
| `--no-verify` | skip the post-render frame-order check |
| `--no-fallback` | don't retry a failed clip as MOV or as a re-encode |
| `--no-pad` | don't pad short audio with silence |
| `--jobs 8` | more clips at once |
| `--dry-run` | print the ffmpeg commands without running them |

`--reencode` still works as a deprecated alias for `--video-codec h264`.

Run `--check` before a long job:

```
python makeclips.py matches.json --video-codec copy --container mxf --check
```

---

## How the matching works

Camera scratch audio and a field recorder never produce similar *waveforms* —
different mics, different placement, different noise floor. What they share is
*timing*. So each file is reduced to a per-band spectral flux fingerprint (where
energy rises, in 16 frequency bands, every 10 ms) and those get cross-correlated.
That's robust to level, EQ and noise differences in a way raw waveform
correlation is not.

Two passes: a cheap 50 ms one to shortlist six candidates per clip, then a
detailed per-band pass on the shortlist. Peak position is parabola-interpolated,
so accuracy is a few milliseconds — well inside one frame — before it gets
rounded to the frame grid for the XML.

At 100 clips × 100 audio files, expect roughly a minute of matching plus decode
time (decode is the slow part and runs in parallel).

Measured on the MXF test shoot: baked audio sits within **0.2 ms** of the camera
scratch, on trims (recorder already rolling) and on inserts (recorder started
late) alike.

---

## Requirements and limits

- **The camera must have recorded scratch audio.** Even bad on-mic audio is
  fine — that's what gets correlated. Clips shot mute cannot be synced by any
  waveform method and are reported as skipped.
- Clips need a few seconds of overlap with actual sound in them. Near-silent
  takes won't produce a confident match.
- Offsets are rounded to the sequence frame rate, so sync is frame-accurate, not
  sample-accurate.
- Jam timecode is written into the XML. If a file carries its own embedded
  timecode, Premiere may prefer that over the value written here — check one
  clip before relying on it for a whole shoot.
- Variable-frame-rate phone footage may drift; transcode to constant frame rate
  first if you see sync wander across a long clip.
- Merge Clips has **no scripting API**, and merged clips are documented as not
  surviving XML interchange. A merged clip cannot be produced by any script, by
  any route. That's a hard limit, not a shortcut taken here.

---

## If clips are being skipped

Press **Diagnose source files** on the Matching tab, or:

```
python syncmatch.py --video "D:\Shoot\dump\Video" --audio "D:\Shoot\dump\Sound" --diagnose
```

It prints every audio stream in every file with its peak level, so you can see
exactly what the matcher is working with:

```
  A001C001.MXF
      12.0s, 1920x1080, 25.000fps, 4 audio stream(s)
        stream a:0  silent
        stream a:1  silent
        stream a:2  peak 0.668
        stream a:3  peak 0.668
      -> mixed for analysis: peak 1.335
```

If the mixed peak is non-zero, the clip is usable. `*** UNUSABLE ***` means every
stream really is silent.

**A note on MXF.** Broadcast MXF usually carries four or eight discrete mono
audio streams, and it's normal for tracks 1/2 to be silent with the real audio on
3/4. All streams get mixed together before analysis, so this is handled — but
it's why relying on ffmpeg's default stream choice would fail on this footage.

## If a clip syncs wrong

Check the Results tab or `matches.json` — every rejected candidate is in there
with its score and the offset it would have used. Raising the confidence
threshold usually fixes a bad pairing; lowering it usually recovers a missed one.

---

## Layout

```
SoundSync.exe / SoundSync.bat   the app
build.py                        makes the standalone build (--installer wraps it)
installer.iss                   Inno Setup script build.py --installer compiles
sync.bat                        drag-a-folder quick route
syncmatch.py                    stage 1 CLI
makefcpxml.py                   stage 2a CLI  (sequences)
makeclips.py                    stage 2b CLI  (media files)
soundsync/
    matcher.py    fingerprinting and cross-correlation
    fcpxml.py     FCP7 XML generation
    clips.py      ffmpeg muxing, codecs, post-render verification
    csvout.py     the pairing list as a spreadsheet
    compat.py     what an export setting will do to your sources
    pipeline.py   scanning and running the stages together
    options.py    every setting, as plain data
    proc.py       finding and running ffmpeg, cancellation
    report.py     progress reporting
    gui.py        the window
```
