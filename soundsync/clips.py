#!/usr/bin/env python3
"""
Stage 2b -- bake each matched pair into a new, self-contained media file.

Instead of describing the sync to Premiere and asking it to merge anything,
this writes one finished clip per take: the camera's video handled as you
choose (copied through untouched, or re-encoded to an intra codec), with the
recorder audio muxed in at the right offset.

Stream copy is free and lossless but the output essence has to be something
the wrapper -- and Premiere -- can actually read. See compat.py, which works
that out for your specific footage before anything is written.
"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace

from . import proc
from .options import ClipOptions
from .report import Reporter


# --------------------------------------------------------------- codecs

@dataclass
class CodecSpec:
    key: str
    label: str
    args: list = field(default_factory=list)
    intra: bool = True
    """Intra-only codecs store every frame independently, so there is no frame
    reordering for a muxer to lose -- which is what makes them safe in MXF."""
    bits_per_pixel: float = 0.0
    """Rough data rate, in bits per pixel per frame, for size estimates.
    0 means 'varies too much to predict' (the CRF encoders)."""
    default_container: str = "mov"
    containers: tuple = ("mov",)
    keeps_quality: bool = True
    note: str = ""


VIDEO_CODECS = {
    "copy": CodecSpec(
        "copy", "Stream copy - no re-encode (fast, lossless)",
        ["-c:v", "copy"], intra=False, bits_per_pixel=0.0,
        default_container="auto", containers=("auto", "mov", "mp4", "mxf", "mkv", "avi"),
        note="Picture is bit-for-bit the camera original and it runs at disk speed. "
             "Whether the result is usable depends on the wrapper -- check the "
             "Compatibility tab."),

    "prores_hq": CodecSpec(
        "prores_hq", "ProRes 422 HQ (10-bit, edit-ready)",
        ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le",
         "-vendor", "apl0"],
        bits_per_pixel=3.54, default_container="mov", containers=("mov", "mkv", "mxf"),
        note="The safe answer for 10-bit 4:2:2 camera footage. Keeps the colour "
             "depth, imports everywhere, roughly 5x the disk."),

    "prores_422": CodecSpec(
        "prores_422", "ProRes 422 (10-bit, smaller)",
        ["-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le",
         "-vendor", "apl0"],
        bits_per_pixel=2.36, default_container="mov", containers=("mov", "mkv", "mxf")),

    "prores_lt": CodecSpec(
        "prores_lt", "ProRes 422 LT (10-bit, smallest)",
        ["-c:v", "prores_ks", "-profile:v", "1", "-pix_fmt", "yuv422p10le",
         "-vendor", "apl0"],
        bits_per_pixel=1.64, default_container="mov", containers=("mov", "mkv", "mxf")),

    "prores_4444": CodecSpec(
        "prores_4444", "ProRes 4444 (12-bit, largest)",
        ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le",
         "-vendor", "apl0"],
        bits_per_pixel=5.31, default_container="mov", containers=("mov", "mkv")),

    "dnxhr_hqx": CodecSpec(
        "dnxhr_hqx", "DNxHR HQX (10-bit, edit-ready)",
        ["-c:v", "dnxhd", "-profile:v", "dnxhr_hqx", "-pix_fmt", "yuv422p10le"],
        bits_per_pixel=3.50, default_container="mov", containers=("mov", "mxf", "mkv"),
        note="Avid's equivalent of ProRes HQ. Intra-only, so this is the one "
             "re-encode that makes MXF output safe."),

    "dnxhr_sq": CodecSpec(
        "dnxhr_sq", "DNxHR SQ (8-bit, smaller)",
        ["-c:v", "dnxhd", "-profile:v", "dnxhr_sq", "-pix_fmt", "yuv422p"],
        bits_per_pixel=2.30, default_container="mov", containers=("mov", "mxf", "mkv"),
        keeps_quality=False,
        note="8-bit. Fine for offline, throws away depth on a 10-bit source."),

    "h264": CodecSpec(
        "h264", "H.264 (8-bit 4:2:0, small, lossy)",
        ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"],
        intra=False, bits_per_pixel=0.0, default_container="mov",
        containers=("mov", "mp4", "mkv"), keeps_quality=False,
        note="Small files, but 8-bit 4:2:0 -- it discards the extra colour "
             "information a 10-bit or 4:2:2 camera recorded. Slow to encode."),

    "h265": CodecSpec(
        "h265", "H.265 / HEVC (10-bit 4:2:0, smallest, lossy)",
        ["-c:v", "libx265", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p10le"],
        intra=False, bits_per_pixel=0.0, default_container="mov",
        containers=("mov", "mp4", "mkv"), keeps_quality=False,
        note="Keeps 10 bits but subsamples colour to 4:2:0. Slowest option."),
}

CODEC_ORDER = ["copy", "prores_hq", "prores_422", "prores_lt", "prores_4444",
               "dnxhr_hqx", "dnxhr_sq", "h264", "h265"]

AUDIO_CODECS = {
    "pcm_s24le": "PCM 24-bit (lossless, default)",
    "pcm_s16le": "PCM 16-bit (lossless, smaller)",
    "aac": "AAC 320k (lossy, smallest)",
}

CONTAINERS = ["auto", "mov", "mxf", "mkv", "mp4"]


# Containers that carry uncompressed PCM correctly. MP4 nominally accepts PCM
# but ffmpeg silently promotes it to pcm_s32le and the result decodes
# mis-timed, so PCM is downgraded to AAC there instead.
PCM_SAFE = {"mov", "mxf", "mkv", "avi", "wav"}

# MXF is deliberately NOT kept as the output wrapper for a stream copy, even
# for MXF sources.
#
# ffmpeg's MXF muxer only knows how to write presentation-order ("temporal")
# offsets for the intra-only essences it was built around -- AVC-Intra, D-10,
# DNxHD, JPEG2000. Hand it a long-GOP H.264 stream with B-frames (Canon
# XF-AVC, Sony XAVC Long, and most other modern camera MXF) and it writes a
# flat index instead: every packet comes out claiming PTS == its position in
# the file, so the reordering is thrown away and DTS values collide.
#
# The result decodes with each group of pictures shown in coded order rather
# than display order, and roughly one frame in three dropped outright. On
# screen that reads as a hard stutter several times a second.
REMAP = {"mp4": "mov", "m4v": "mov", "braw": "mov", "r3d": "mov",
         "ari": "mov", "mxf": "mov"}

# Codecs whose Annex-B access-unit delimiters have to be stripped before a
# stream copy can be wrapped in MOV/MP4. Camera H.264 carries large Annex-B
# extradata (Canon's C70 emits ~9.7 kB of it); the MOV muxer builds its avcC
# record straight from that and produces a malformed one, so the output opens
# as "moov atom not found" or decodes to nothing. Dropping the AUD NALs --
# which are optional delimiters, not picture data -- lets it build a valid
# record. Verified frame-for-frame identical to the source with them removed.
AUD_BSF = {"h264": "h264_metadata=aud=remove", "hevc": "hevc_metadata=aud=remove",
           "h265": "hevc_metadata=aud=remove"}

FASTSTART_CONTAINERS = {"mov", "mp4", "m4v"}

_codec_cache = {}


# --------------------------------------------------------------- helpers

def tc_string(seconds, fps):
    """Seconds -> HH:MM:SS:FF for ffmpeg's -timecode, non-drop."""
    if not fps or fps <= 0:
        fps = 25.0
    total = max(0, int(round(seconds * fps)))
    ff = total % int(round(fps))
    s = total // int(round(fps))
    return f"{s // 3600:02d}:{(s // 60) % 60:02d}:{s % 60:02d}:{ff:02d}"


def audio_chain(offset, n_streams, pad):
    """
    Build one synced output track per source audio stream.

    Returns (filtergraph, [labels]).

    Streams are kept SEPARATE rather than mixed. A recorder with a lav on one
    track and a boom on another must arrive in Premiere as two tracks -- mixing
    them here would throw away the separation you need to balance them later.
    A single multichannel stream (a poly WAV) is passed through intact, so its
    channel layout survives too.

    offset is where in the audio file the video starts. Positive means the
    recorder was already rolling and we trim into it; negative means it started
    late and we push the audio later by inserting silence.
    """
    if offset >= 0.0005:
        body = f"atrim=start={offset:.6f},asetpts=PTS-STARTPTS"
    elif offset <= -0.0005:
        body = f"adelay={int(round(-offset * 1000))}:all=1"
    else:
        body = "anull"
    if pad:
        body += ",apad"

    parts, labels = [], []
    for i in range(max(1, n_streams)):
        label = f"a{i}"
        parts.append(f"[1:a:{i}]{body}[{label}]")
        labels.append(label)
    return ";".join(parts), labels


def spec_for(opts):
    return VIDEO_CODECS.get(opts.video_codec, VIDEO_CODECS["copy"])


def container_for(source_name, opts):
    """
    Pick the output wrapper.

    'auto' with a stream copy keeps the camera's own container, except where
    that container can't hold the stream correctly (see REMAP). 'auto' with a
    re-encode uses whatever that codec is normally carried in.
    """
    if opts.container and opts.container != "auto":
        return opts.container.lstrip(".").lower()
    spec = spec_for(opts)
    if spec.key != "copy":
        return spec.default_container
    ext = os.path.splitext(source_name)[1].lstrip(".").lower() or "mov"
    return REMAP.get(ext, ext)


def video_codec_of(vmeta, cancel=None):
    """Codec name for the source video stream, probing only if stage 1 didn't
    record one (matches.json written by an older version)."""
    known = (vmeta.get("video_codec") or "").lower()
    if known:
        return known
    path = vmeta["path"]
    if path not in _codec_cache:
        res = proc.run([proc.ffprobe(), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
                       cancel=cancel)
        out = (res.stdout or "").strip() if res.returncode == 0 else ""
        _codec_cache[path] = out.splitlines()[0].strip().lower() if out else ""
    return _codec_cache[path]


def audio_codec_for(container, opts):
    """PCM everywhere it works; fall back to AAC only where it doesn't."""
    if opts.audio_codec.startswith("pcm") and container not in PCM_SAFE:
        return "aac"
    return opts.audio_codec


def estimate_bytes(vmeta, opts):
    """
    Rough output size for one clip, in bytes.

    A stream copy comes out about the size of its source. A re-encode is
    predicted from the codec's bits-per-pixel figure; the CRF encoders vary
    far too much with content to guess, so they return None.
    """
    spec = spec_for(opts)
    if spec.key == "copy":
        return vmeta.get("file_size") or 0
    if not spec.bits_per_pixel:
        return None
    w = vmeta.get("width") or 1920
    h = vmeta.get("height") or 1080
    fps = vmeta.get("fps") or 25.0
    dur = vmeta.get("duration") or 0.0
    video = w * h * fps * spec.bits_per_pixel * dur / 8.0
    channels = max(1, vmeta.get("audio_streams") or 1)
    audio = 48000 * 3 * channels * dur if opts.audio_codec.startswith("pcm") else 40000 * dur
    return int(video + audio)


# --------------------------------------------------------------- rendering

def build_cmd(pair, vmeta, ameta, out_path, opts, start_tc=None, cancel=None):
    spec = spec_for(opts)
    cmd = [proc.ffmpeg(), "-y", "-v", "error", "-stats",
           "-i", vmeta["path"], "-i", ameta["path"]]

    n = max(1, ameta.get("audio_streams") or 1)
    graph, labels = audio_chain(pair["offset"], n, opts.pad)
    cmd += ["-filter_complex", graph, "-map", "0:v:0"]
    for lab in labels:
        cmd += ["-map", f"[{lab}]"]

    if opts.keep_camera_audio and vmeta.get("has_audio"):
        cmd += ["-map", "0:a"]

    cmd += list(spec.args)
    container = os.path.splitext(out_path)[1].lstrip(".").lower()

    # Stream copies into MOV/MP4 need the access-unit delimiters stripped, or
    # the muxer writes an avcC record the file can't be read back through.
    if spec.key == "copy" and container in FASTSTART_CONTAINERS:
        bsf = AUD_BSF.get(video_codec_of(vmeta, cancel))
        if bsf:
            cmd += ["-bsf:v", bsf]

    acodec = audio_codec_for(container, opts)
    cmd += ["-c:a", acodec]
    if acodec.startswith("pcm"):
        cmd += ["-ar", "48000"]
    elif acodec == "aac":
        cmd += ["-b:a", "320k"]
    if start_tc:
        cmd += ["-timecode", start_tc]

    # Without faststart the MOV muxer leaves the mdat header claiming more
    # bytes than it wrote, so every parser walks past the moov and reports
    # "moov atom not found". faststart's second pass rewrites the file with a
    # correct box layout.
    if container in FASTSTART_CONTAINERS:
        cmd += ["-movflags", "+faststart"]

    # An explicit duration rather than -shortest. apad makes the audio branch
    # infinite, and -shortest then decides where to stop by whichever thread
    # gets there first -- output length varied run to run and came up a couple
    # of frames short of the video. Cutting at the known video duration is
    # deterministic and keeps the last frames.
    duration = vmeta.get("duration") or 0
    cmd += (["-t", f"{duration:.6f}"] if duration > 0 else ["-shortest"])
    cmd += ["-map_metadata", "0", out_path]
    return cmd


def verify_output(path, fps, seconds=3.0, cancel=None):
    """
    Decode the first few seconds and check the frames come back in order.

    This is the check that would have caught the MXF remux: that output opened
    fine, reported the right duration and played at the right length, but a
    third of its frames were missing and the rest were timestamped in coded
    order rather than display order. Nothing short of decoding it shows that.

    Returns None if the file looks sound, or a short description of what's wrong.
    """
    res = proc.run(
        [proc.ffprobe(), "-v", "error", "-select_streams", "v:0", "-read_intervals",
         f"%+{seconds:g}", "-show_entries", "frame=pts_time", "-of", "csv=p=0", path],
        cancel=cancel)
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()[-1:] or ["?"]
        return f"output won't open ({tail[0][:80]})"

    times = []
    for line in (res.stdout or "").splitlines():
        line = line.strip().rstrip(",")
        try:
            times.append(float(line))
        except ValueError:
            continue
    if not times:
        return "output decodes to no frames at all"
    if any(b <= a for a, b in zip(times, times[1:])):
        return "frames come back out of order -- video will stutter"
    if fps and fps > 0:
        expected = min(seconds, times[-1] + 1.0 / fps) * fps
        if len(times) < expected * 0.9:
            return (f"only {len(times)} of about {int(expected)} frames survived "
                    f"the first {seconds:g}s -- video will stutter")
    return None


def _discard(path):
    """Don't leave a broken stub sitting next to the good files."""
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _attempt(pair, vmeta, ameta, path, opts, start_tc, cancel):
    """Run one ffmpeg build and check what came out. Returns (ok, problem)."""
    res = proc.run(build_cmd(pair, vmeta, ameta, path, opts, start_tc, cancel),
                   cancel=cancel)
    if res.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) <= 1024:
        tail = ((res.stderr or "").strip().splitlines()[-1:] or [""])[0][:90]
        return False, tail or "ffmpeg failed"
    if not opts.verify:
        return True, None
    bad = verify_output(path, vmeta.get("fps"), cancel=cancel)
    return (False, bad) if bad else (True, None)


def _rescue_codec(vmeta):
    """
    What to re-encode to when a stream copy won't work.

    An intra codec that preserves what the camera recorded -- so 10-bit or
    4:2:2 footage goes to ProRes HQ rather than being crushed to 8-bit 4:2:0
    behind your back.
    """
    pix = (vmeta.get("pix_fmt") or "").lower()
    if "10" in pix or "12" in pix or "422" in pix or "444" in pix:
        return "prores_hq"
    return "h264"


def render_one(pair, vmeta, ameta, out_path, opts, start_tc, cancel=None):
    ok, why = _attempt(pair, vmeta, ameta, out_path, opts, start_tc, cancel)
    if ok:
        return out_path, None
    if not opts.fallback or spec_for(opts).key != "copy":
        _discard(out_path)
        return None, why

    # The stream copy either wouldn't mux or came back with damaged frames.
    # Try MOV first if we aren't already writing one -- it holds long-GOP
    # essence correctly where the broadcast wrappers don't -- and only
    # re-encode as a last resort, since that costs both time and disk.
    _discard(out_path)
    base, ext = os.path.splitext(out_path)
    if ext.lower() != ".mov":
        alt_path = base + ".mov"
        ok, _alt_why = _attempt(pair, vmeta, ameta, alt_path, opts, start_tc, cancel)
        if ok:
            return alt_path, f"{why}; wrote .mov instead"
        _discard(alt_path)

    rescue = _rescue_codec(vmeta)
    forced = replace(opts, video_codec=rescue, container="mov")
    enc_path = base + ".mov"
    ok, enc_why = _attempt(pair, vmeta, ameta, enc_path, forced, start_tc, cancel)
    if ok:
        return enc_path, (f"stream copy failed ({why}); re-encoded to "
                          f"{VIDEO_CODECS[rescue].label}")
    _discard(enc_path)
    return None, enc_why


# --------------------------------------------------------------- driver

def plan_jobs(data, opts):
    """Work out every clip that will be written, without writing anything."""
    media, matches = data.get("media", {}), data.get("matches", [])

    # optional jam-sync anchors, one hour per recorder roll
    anchors, starts = {}, {}
    if opts.jam_timecode:
        for p in matches:
            am = media.get(p["audio"])
            if am and am["path"] not in anchors:
                anchors[am["path"]] = 3600.0 * (len(anchors) + 1)
        for p in matches:
            am = media.get(p["audio"])
            if am:
                starts[p["video"]] = anchors[am["path"]] + p["offset"]

    jobs = []
    for p in matches:
        vmeta, ameta = media.get(p["video"]), media.get(p["audio"])
        if not vmeta or not ameta:
            continue
        stem = os.path.splitext(vmeta["name"])[0]
        ext = container_for(vmeta["name"], opts)
        out = os.path.join(opts.out_dir, f"{stem}{opts.suffix}.{ext}")
        tc = tc_string(starts[p["video"]], vmeta.get("fps")) if p["video"] in starts else None
        jobs.append((p, vmeta, ameta, out, tc))
    return jobs


def run_clips(data, opts=None, report=None, cancel=None):
    """
    The whole of stage 2b. Returns a summary dict.
    """
    opts = opts or ClipOptions()
    report = report or Reporter()
    cancel = cancel or proc.NULL_CANCELLER

    if not data.get("matches"):
        raise ValueError("there are no accepted matches to build clips from")
    if not opts.out_dir:
        raise ValueError("no output folder was set for the synced clips")

    jobs = plan_jobs(data, opts)
    if not jobs:
        raise ValueError("no matched pair had usable metadata")

    if opts.dry_run:
        report.stage("Dry run", f"{len(jobs)} clip(s) -- commands only, nothing written")
        for p, vm, am, out, tc in jobs:
            report.log(proc.quote(build_cmd(p, vm, am, out, opts, tc, cancel)), "detail")
        return {"written": [], "failed": [], "dry_run": True, "planned": len(jobs)}

    os.makedirs(opts.out_dir, exist_ok=True)
    spec = spec_for(opts)
    n = opts.jobs if opts.jobs > 0 else min(4, (os.cpu_count() or 2))
    report.stage("Building clips",
                 f"{len(jobs)} clip(s) -> {opts.out_dir}  ({spec.label}, {n} at a time)")

    ok, failed, notes = [], [], []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = {pool.submit(render_one, p, vm, am, out, opts, tc, cancel): (p, out)
                for p, vm, am, out, tc in jobs}
        try:
            for i, fut in enumerate(as_completed(futs), 1):
                p, out = futs[fut]
                report.progress(i, len(jobs))
                try:
                    path, note = fut.result()
                except proc.Cancelled:
                    raise
                except Exception as e:                       # noqa: BLE001
                    path, note = None, f"unexpected error: {e}"
                base = os.path.basename(out)
                if path:
                    ok.append(path)
                    mb = os.path.getsize(path) / 1e6
                    report.log(f"[{i}/{len(jobs)}] {os.path.basename(path)}  ({mb:,.0f} MB)"
                               + (f"  -- {note}" if note else ""),
                               "warn" if note else "good")
                    report.row("clip", {"video": p["video"], "output": path,
                                        "size": os.path.getsize(path), "note": note or ""})
                    if note:
                        notes.append((base, note))
                else:
                    failed.append((p["video"], note))
                    report.log(f"[{i}/{len(jobs)}] {base}  FAILED: {note}", "error")
                    report.row("clip", {"video": p["video"], "output": "",
                                        "size": 0, "note": f"FAILED: {note}"})
        except proc.Cancelled:
            for f in futs:
                f.cancel()
            raise

    total = sum(os.path.getsize(f) for f in ok) / 1e9
    report.stage("Clips done", f"{len(ok)} written, {total:,.1f} GB")
    for name, why in failed:
        report.log(f"FAILED {name}: {why}", "error")
    if failed and spec.key == "copy":
        report.log("Retry the failures with an intra codec (ProRes 422 HQ or "
                   "DNxHR HQX) -- those re-encode, but they always import.", "warn")
    return {"written": ok, "failed": failed, "notes": notes,
            "total_bytes": sum(os.path.getsize(f) for f in ok)}
