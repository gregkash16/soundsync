#!/usr/bin/env python3
"""
Stage 1 -- match video clips to separately-recorded audio by waveform sync.

How it works
------------
Camera scratch audio and a field recorder never produce similar waveforms --
different mics, different placement, different noise. What they do share is
*timing*: the same transients happen at the same instants. So each file is
reduced to a per-band spectral flux matrix (where energy rises, in 16 frequency
bands, every 10ms) and those are cross-correlated. That is robust to level, EQ
and noise differences in a way raw waveform correlation is not.

A match is accepted on the z-score of the correlation peak against every other
possible alignment, not on the raw peak height. A true sync stands 20-40 sigma
above the field; an unrelated pair rarely clears 9.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np
from scipy import fft as spfft
from scipy import signal

from . import proc
from .options import MatchOptions
from .report import Reporter

# ---------------------------------------------------------------- constants

SR = 8000               # analysis rate; speech transients live well under 4kHz
NFFT = 512              # 64ms analysis window
HOP = 80                # 10ms hop -> flux frame rate of 100Hz
FINE_HOP = HOP / SR     # 0.010 s
COARSE_DECIM = 5        # coarse pass runs at 50ms
COARSE_HOP = FINE_HOP * COARSE_DECIM
N_BANDS = 16
BAND_LO, BAND_HI = 100.0, 3800.0
TOP_K = 6               # candidates carried from coarse ranking into the fine pass
MIN_OVERLAP_S = 2.0     # ignore alignments with less overlap than this
N_CANDIDATES = 3        # runner-ups kept per clip for the report
Z_HALF = 10.0           # z-score that reads as 50% confidence

VIDEO_EXT = {".mov", ".mp4", ".mxf", ".avi", ".mts", ".m2ts", ".mkv", ".m4v", ".wmv"}
AUDIO_EXT = {".wav", ".aif", ".aiff", ".mp3", ".m4a", ".flac", ".caf",
             ".bwf", ".ogg", ".mka", ".w64", ".rf64", ".opus"}


def _band_edges():
    edges = np.logspace(np.log10(BAND_LO), np.log10(BAND_HI), N_BANDS + 1)
    bins = np.floor(edges / (SR / NFFT)).astype(int)
    return [(bins[i], max(bins[i] + 1, bins[i + 1])) for i in range(N_BANDS)]


BANDS = _band_edges()


# ---------------------------------------------------------------- data types

@dataclass
class Media:
    path: str
    name: str
    duration: float = 0.0
    fps: float = 0.0
    width: int = 0
    height: int = 0
    has_video: bool = False
    has_audio: bool = False
    audio_channels: int = 0
    audio_streams: int = 0
    video_codec: str = ""
    pix_fmt: str = ""
    profile: str = ""
    has_b_frames: int = 0
    bit_rate: int = 0
    file_size: int = 0
    fine: np.ndarray = field(default=None, repr=False)     # (N_BANDS, frames)
    coarse: np.ndarray = field(default=None, repr=False)   # (frames/5,)

    def meta(self):
        return {"path": self.path, "name": self.name,
                "duration": round(self.duration, 4), "fps": self.fps,
                "width": self.width, "height": self.height,
                "has_video": self.has_video, "has_audio": self.has_audio,
                "audio_channels": self.audio_channels,
                "audio_streams": self.audio_streams,
                "video_codec": self.video_codec,
                "pix_fmt": self.pix_fmt,
                "profile": self.profile,
                "has_b_frames": self.has_b_frames,
                "bit_rate": self.bit_rate,
                "file_size": self.file_size}


# ---------------------------------------------------------------- ffprobe

def probe(path, cancel=None):
    """Read the container's own description of itself. Cheap -- no decoding."""
    cmd = [proc.ffprobe(), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    res = proc.run(cmd, cancel=cancel)
    if res.returncode != 0:
        raise RuntimeError(proc.explain_failure(res, "ffprobe"))
    try:
        info = json.loads(res.stdout or "")
    except ValueError:
        raise RuntimeError("ffprobe returned nothing readable for this file")

    m = Media(path=os.path.abspath(path), name=os.path.basename(path))
    try:
        m.duration = float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        m.duration = 0.0
    try:
        m.file_size = int(info["format"]["size"])
    except (KeyError, TypeError, ValueError):
        try:
            m.file_size = os.path.getsize(path)
        except OSError:
            m.file_size = 0
    try:
        m.bit_rate = int(info["format"]["bit_rate"])
    except (KeyError, TypeError, ValueError):
        m.bit_rate = 0

    for s in info.get("streams", []):
        kind = s.get("codec_type")
        if kind == "video" and not m.has_video:
            # skip cover-art / thumbnail streams attached to audio files
            if s.get("disposition", {}).get("attached_pic"):
                continue
            m.has_video = True
            m.video_codec = (s.get("codec_name") or "").lower()
            m.width = int(s.get("width") or 0)
            m.height = int(s.get("height") or 0)
            m.pix_fmt = (s.get("pix_fmt") or "").lower()
            m.profile = str(s.get("profile") or "")
            try:
                m.has_b_frames = int(s.get("has_b_frames") or 0)
            except (TypeError, ValueError):
                m.has_b_frames = 0
            for key in ("avg_frame_rate", "r_frame_rate"):
                try:
                    num, den = (s.get(key) or "0/0").split("/")
                    if float(den) and float(num):
                        m.fps = float(num) / float(den)
                        break
                except (ValueError, ZeroDivisionError):
                    continue
        elif kind == "audio":
            m.audio_streams += 1
            if not m.has_audio:
                m.has_audio = True
                m.audio_channels = int(s.get("channels") or 0)
    return m


# ---------------------------------------------------------------- fingerprint

def _run_decode(cmd, cancel=None):
    res = proc.run(cmd, cancel=cancel, text=False)
    if res.returncode != 0 or not res.stdout:
        return None
    return np.frombuffer(res.stdout, dtype=np.float32)


def _loud(x):
    return 0.0 if x is None or not x.size else float(np.abs(x).max())


def decode_mono(path, n_streams=1, cancel=None):
    """
    Fold every audio stream in the file down to one mono track at SR.

    Left to itself, ffmpeg picks a single "best" audio stream. That's wrong for
    broadcast MXF, which typically carries four or eight discrete mono streams
    with the usable scratch audio on tracks 3/4 and silence on 1/2 -- the default
    pick comes back empty and the clip looks unsyncable. So all streams get mixed
    together, with fallbacks for older ffmpeg builds and odd files.
    """
    base = [proc.ffmpeg(), "-v", "error", "-i", path, "-vn"]
    tail = ["-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]

    attempts = []
    if n_streams > 1:
        ins = "".join(f"[0:a:{i}]" for i in range(n_streams))
        # normalize=0 sums rather than averaging, so silent tracks don't
        # attenuate the ones that matter. Not in ffmpeg < 4.4, hence the retry.
        attempts.append(base + ["-filter_complex",
                                f"{ins}amix=inputs={n_streams}:duration=longest:normalize=0[m]",
                                "-map", "[m]"] + tail)
        attempts.append(base + ["-filter_complex",
                                f"{ins}amix=inputs={n_streams}:duration=longest[m]",
                                "-map", "[m]"] + tail)
    attempts.append(base + tail)                      # ffmpeg's default choice

    for cmd in attempts:
        data = _run_decode(cmd, cancel)
        if _loud(data) > 1e-5:
            return data

    # everything mixed came out silent: fall back to the single loudest stream
    best = None
    for i in range(max(1, n_streams)):
        data = _run_decode(base + ["-map", f"0:a:{i}"] + tail, cancel)
        if _loud(data) > _loud(best):
            best = data
    return best


def band_flux(x):
    """
    Per-band spectral flux: (N_BANDS, frames), each band zero-mean / unit-variance.

    Rising energy only -- decays are discarded, because onsets are what survive
    the trip through a different microphone.
    """
    _, _, Z = signal.stft(x, fs=SR, nperseg=NFFT, noverlap=NFFT - HOP,
                          padded=False, boundary=None)
    mag = np.abs(Z).astype(np.float32)
    if mag.shape[1] < 4:
        return np.zeros((N_BANDS, 0), dtype=np.float32)

    banded = np.stack([mag[lo:hi].mean(axis=0) for lo, hi in BANDS])
    logb = np.log1p(banded * 100.0)
    dif = np.diff(logb, axis=1, prepend=logb[:, :1])
    np.maximum(dif, 0.0, out=dif)

    sd = dif.std(axis=1, keepdims=True)
    sd[sd < 1e-9] = 1.0
    return ((dif - dif.mean(axis=1, keepdims=True)) / sd).astype(np.float32)


def coarse_from_fine(fine):
    """Summed flux, decimated to 50ms, for cheap first-pass ranking."""
    if fine.shape[1] == 0:
        return np.zeros(0, dtype=np.float32)
    summed = fine.sum(axis=0)
    n = (len(summed) // COARSE_DECIM) * COARSE_DECIM
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    return summed[:n].reshape(-1, COARSE_DECIM).sum(axis=1).astype(np.float32)


def _unit(v):
    v = v - v.mean(axis=-1, keepdims=True)
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    n = np.where(n < 1e-12, 1.0, n)
    return (v / n).astype(np.float32)


def _peak_stats(corr, nv, na, hop):
    """Locate the best valid lag and score how far it stands above the field."""
    lags = np.arange(corr.size) - (nv - 1)
    overlap = np.minimum(np.minimum(na - lags, nv), na)
    valid = overlap >= max(4, int(MIN_OVERLAP_S / hop))
    if valid.sum() < 8:
        return 0.0, 0.0, 0.0

    masked = np.where(valid, corr, -np.inf)
    peak_i = int(np.argmax(masked))
    peak = float(corr[peak_i])
    vals = corr[valid]

    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med))) * 1.4826
    spread = mad if mad > 1e-12 else (float(vals.std()) or 1e-12)
    z = (peak - med) / spread

    # sub-hop refinement: fit a parabola through the peak and its neighbours,
    # so precision isn't capped at the 10ms analysis grid
    shift = 0.0
    if 0 < peak_i < corr.size - 1:
        y0, y1, y2 = float(corr[peak_i - 1]), peak, float(corr[peak_i + 1])
        denom = y0 - 2 * y1 + y2
        if abs(denom) > 1e-12:
            shift = float(np.clip(0.5 * (y0 - y2) / denom, -0.5, 0.5))

    guard = max(3, int(round(0.5 / hop)))
    masked[max(0, peak_i - guard):min(corr.size, peak_i + guard + 1)] = -np.inf
    finite = masked[np.isfinite(masked)]
    runner = float(finite.max()) if finite.size else 0.0
    prom = peak / runner if runner > 1e-9 else 99.0

    offset = (float(lags[peak_i]) + shift) * hop
    return offset, float(z), float(min(prom, 99.0))


def correlate_1d(v, a, hop):
    if v.size == 0 or a.size == 0:
        return 0.0, 0.0, 0.0
    corr = signal.fftconvolve(_unit(a), _unit(v)[::-1], mode="full")
    return _peak_stats(corr, len(v), len(a), hop)


def correlate_bands(V, A, hop=FINE_HOP):
    """Sum the cross-correlation of all bands, via one batched 2-D FFT."""
    nv, na = V.shape[1], A.shape[1]
    if nv == 0 or na == 0:
        return 0.0, 0.0, 0.0
    n = spfft.next_fast_len(nv + na - 1)
    FV = spfft.rfft(_unit(V)[:, ::-1], n=n, axis=1, workers=-1)
    FA = spfft.rfft(_unit(A), n=n, axis=1, workers=-1)
    corr = spfft.irfft(FA * FV, n=n, axis=1, workers=-1)[:, :nv + na - 1].sum(axis=0)
    return _peak_stats(corr, nv, na, hop)


# ---------------------------------------------------------------- pipeline

def gather(folder, exts, recursive=False):
    """Media files in a folder, sorted. Raises ValueError if it isn't a folder."""
    if not os.path.isdir(folder):
        raise ValueError(f"not a folder: {folder}")
    out = []
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for e in files:
                if os.path.splitext(e)[1].lower() in exts:
                    out.append(os.path.join(root, e))
    else:
        for e in os.listdir(folder):
            full = os.path.join(folder, e)
            if os.path.isfile(full) and os.path.splitext(e)[1].lower() in exts:
                out.append(full)
    return sorted(out, key=lambda p: (os.path.basename(p).lower(), p))


def fingerprint(path, cancel=None):
    """Probe + decode + fingerprint one file. Returns (Media, None) or (None, reason)."""
    try:
        m = probe(path, cancel)
    except RuntimeError as e:
        return None, str(e)
    if not m.has_audio:
        return None, "no audio track -- cannot be synced by waveform"
    pcm = decode_mono(path, m.audio_streams, cancel)
    if pcm is None or pcm.size < SR:
        return None, f"audio undecodable or under 1s ({m.audio_streams} stream(s) tried)"
    if float(np.abs(pcm).max()) < 1e-5:
        return None, f"all {m.audio_streams} audio stream(s) are silent"
    m.fine = band_flux(pcm)
    m.coarse = coarse_from_fine(m.fine)
    del pcm
    if m.fine.shape[1] < 8:
        return None, "too short to fingerprint"
    return m, None


def load(paths, label, jobs, report=None, cancel=None):
    """Decode and fingerprint in parallel -- ffmpeg is a subprocess, so the GIL
    isn't in the way and this is the stage that dominates wall-clock time."""
    report = report or Reporter()
    cancel = cancel or proc.NULL_CANCELLER
    items, skipped = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(fingerprint, p, cancel): p for p in paths}
        try:
            for fut in as_completed(futures):
                base = os.path.basename(futures[fut])
                done += 1
                report.progress(done, len(paths))
                report.log(f"[{label} {done}/{len(paths)}] {base}", "detail")
                try:
                    m, why = fut.result()
                except proc.Cancelled:
                    raise
                except Exception as e:                     # noqa: BLE001
                    m, why = None, f"unexpected error: {e}"
                if m is None:
                    # Say why right here, while the user is watching -- if
                    # every file goes this way the run ends before the summary.
                    report.log(f"{base}  SKIPPED: {why}", "warn")
                    skipped.append((base, why))
                else:
                    items.append(m)
        except proc.Cancelled:
            for f in futures:
                f.cancel()
            raise
    items.sort(key=lambda m: m.name)
    skipped.sort()
    return items, skipped


def diagnose(paths, label, report=None, cancel=None):
    """Report what ffprobe/ffmpeg actually see in each file. No matching done."""
    report = report or Reporter()
    report.stage(label)
    rows = []
    for i, p in enumerate(paths, 1):
        cancel and cancel.check()
        base = os.path.basename(p)
        report.progress(i, len(paths))
        try:
            m = probe(p, cancel)
        except RuntimeError as e:
            report.log(f"{base}: PROBE FAILED: {e}", "error")
            rows.append({"file": base, "usable": False, "detail": str(e)})
            continue
        bits = [f"{m.duration:.1f}s"]
        if m.has_video:
            bits += [f"{m.width}x{m.height}", f"{m.fps:.3f}fps"]
        bits.append(f"{m.audio_streams} audio stream(s)")
        report.log(f"{base}  --  {', '.join(bits)}")
        if not m.has_audio:
            report.log("NO AUDIO -- cannot be synced by waveform", "warn")
            rows.append({"file": base, "usable": False, "detail": "no audio"})
            continue
        for s in range(m.audio_streams):
            one = _run_decode([proc.ffmpeg(), "-v", "error", "-i", p, "-vn",
                               "-map", f"0:a:{s}", "-ac", "1", "-ar", str(SR),
                               "-f", "f32le", "-"], cancel)
            peak = _loud(one)
            report.log(f"stream a:{s}  " + ("silent" if peak < 1e-5 else f"peak {peak:.3f}"),
                       "detail")
        mixed = decode_mono(p, m.audio_streams, cancel)
        peak = _loud(mixed)
        usable = peak >= 1e-5
        report.log(f"-> mixed for analysis: peak {peak:.3f}"
                   + ("" if usable else "   *** UNUSABLE ***"),
                   "detail" if usable else "warn")
        rows.append({"file": base, "usable": usable, "detail": f"mixed peak {peak:.3f}"})
    return rows


def confidence(z):
    """
    The z-score as a percentage a human can read.

    z is unbounded and only means something if you know the scale: unrelated
    files rarely clear 9, real syncs land at 20-40. This maps that onto 0-100
    so a report can say "97%" instead of "z 31.4". The curve crosses 50% at
    Z_HALF, which is deliberately the same neighbourhood as the default accept
    threshold, so a number under 50 means "this one needs your eyes on it".

    It is a readability aid, not a probability -- nothing here is calibrated
    against a labelled corpus.
    """
    z = max(0.0, float(z))
    return round(100.0 * z ** 3 / (z ** 3 + Z_HALF ** 3), 1)


def _candidate(a, ai, off, z, prom):
    return {"audio": a.name, "audio_path": a.path, "offset": round(off, 4),
            "z": round(z, 2), "prominence": round(prom, 2),
            "confidence": confidence(z), "_ai": ai}


def match(videos, audios, opts, report=None, cancel=None):
    report = report or Reporter()
    cancel = cancel or proc.NULL_CANCELLER
    pairs = []
    for vi, v in enumerate(videos, 1):
        cancel.check()
        report.progress(vi, len(videos))
        report.log(f"[match {vi}/{len(videos)}] {v.name}", "detail")

        ranked = sorted(
            ((correlate_1d(v.coarse, a.coarse, COARSE_HOP)[1], ai)
             for ai, a in enumerate(audios)), key=lambda r: -r[0])

        # Every audio file that survived the coarse pass is scored properly and
        # kept, not just the winner. The runner-ups are what the CSV report and
        # the Results table show you when a pairing needs a second opinion.
        cands = []
        for _, ai in ranked[:TOP_K]:
            a = audios[ai]
            off, z, prom = correlate_bands(v.fine, a.fine)
            cands.append(_candidate(a, ai, off, z, prom))
        cands.sort(key=lambda c: -c["z"])

        if cands:
            best = dict(cands[0])
            best.update({"video": v.name, "video_path": v.path,
                         "candidates": [{k: c[k] for k in
                                         ("audio", "audio_path", "offset", "z",
                                          "prominence", "confidence")}
                                        for c in cands[:N_CANDIDATES]]})
            pairs.append(best)

    pairs.sort(key=lambda p: -p["z"])
    used, accepted, rejected = set(), [], []
    for p in pairs:
        if p["z"] < opts.min_z or p["prominence"] < opts.min_prominence:
            p["reason"] = (f"below confidence threshold "
                           f"(z {p['z']}, prominence {p['prominence']})")
            rejected.append(p)
        elif opts.one_to_one and p["_ai"] in used:
            p["reason"] = "that audio file was already claimed by a stronger match"
            rejected.append(p)
        else:
            used.add(p["_ai"])
            accepted.append(p)
    for p in accepted + rejected:
        p.pop("_ai", None)
    return accepted, rejected


def _skip_summary(skipped):
    """Why every file was dropped, grouped -- the one thing worth saying."""
    if not skipped:
        return ""
    counts = {}
    for _f, why in skipped:
        counts[why] = counts.get(why, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
    lines = [f"  {n} file(s): {why}" for why, n in top]
    if len(counts) > len(top):
        lines.append(f"  ...and {len(counts) - len(top)} other reason(s)")
    return f"\n\nAll {len(skipped)} file(s) were skipped:\n" + "\n".join(lines)


def run_match(video_dir, audio_dir, opts=None, out_path=None,
              report=None, cancel=None):
    """
    The whole of stage 1. Returns the matches dict, and writes it to out_path
    if one is given.
    """
    opts = opts or MatchOptions()
    report = report or Reporter()
    cancel = cancel or proc.NULL_CANCELLER

    vpaths = gather(video_dir, VIDEO_EXT, opts.recursive)
    apaths = gather(audio_dir, AUDIO_EXT, opts.recursive)
    if not vpaths:
        raise ValueError(f"no video files found in {video_dir}")
    if not apaths:
        raise ValueError(f"no audio files found in {audio_dir}")

    jobs = opts.jobs if opts.jobs > 0 else min(8, (os.cpu_count() or 2))
    report.stage("Analysing",
                 f"{len(vpaths)} video and {len(apaths)} audio file(s), {jobs} at a time")
    videos, vskip = load(vpaths, "video", jobs, report, cancel)
    audios, askip = load(apaths, "audio", jobs, report, cancel)
    if not videos:
        raise ValueError("no video files had usable scratch audio -- nothing to sync "
                         "against." + _skip_summary(vskip))
    if not audios:
        raise ValueError("no audio files could be analysed." + _skip_summary(askip))

    report.stage("Matching")
    accepted, rejected = match(videos, audios, opts, report, cancel)

    matched = {p["video"] for p in accepted}
    unmatched = [v.name for v in videos if v.name not in matched]

    data = {"matches": accepted, "rejected": rejected,
            "unmatched_video": unmatched,
            "skipped": [{"file": f, "reason": r} for f, r in vskip + askip],
            "media": {m.name: m.meta() for m in videos + audios}}

    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    report.stage("Matched",
                 f"{len(accepted)} matched, {len(unmatched)} unmatched, "
                 f"{len(vskip) + len(askip)} skipped")
    for p in accepted:
        report.log(f"{p['video']}  <-  {p['audio']}   offset {p['offset']:+.3f}s"
                   f"   ({p.get('confidence', 0)}%, z {p['z']}, "
                   f"prom {p['prominence']})", "good")
        report.row("match", p)
    for p in rejected:
        if p["video"] in unmatched:
            report.log(f"{p['video']}  <-  NO MATCH   best guess {p['audio']} "
                       f"at {p['offset']:+.3f}s, {p['reason']}", "warn")
            report.row("unmatched", p)
    for f, r in vskip + askip:          # already logged as they happened
        report.row("skipped", {"video": f, "reason": r})

    return data
