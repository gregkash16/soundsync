#!/usr/bin/env python3
"""
What a given export setting will actually do to *these* source files.

This exists because the failure that cost the most time on this project was
invisible: an output that opened cleanly, probed clean, reported the right
duration and frame rate, and had a third of its frames missing. Nothing warned
about it. So before anything is written, every source is probed and checked
against what we know about wrappers, essences and Premiere's importers.

Every finding carries how confident we are and why:

    verified   measured on real footage through this tool
    inferred   follows from a documented ffmpeg or Premiere limitation
    untested   we have not put this combination through Premiere

"untested" is deliberately not the same as "fine". A combination we have never
tried gets said so, rather than being quietly presented as safe.
"""

import os
import shutil
from collections import OrderedDict
from dataclasses import dataclass, field

from . import clips as clipmod

SEVERE = "severe"
WARN = "warn"
UNTESTED = "untested"
NOTE = "note"
OK = "ok"

RANK = {SEVERE: 0, WARN: 1, UNTESTED: 2, NOTE: 3, OK: 4}

LEVEL_LABEL = {SEVERE: "WILL NOT WORK", WARN: "RISKY", UNTESTED: "UNTESTED",
               NOTE: "NOTE", OK: "OK"}


@dataclass
class Finding:
    level: str
    title: str
    detail: str
    evidence: str = "inferred"
    files: list = field(default_factory=list)

    @property
    def label(self):
        return LEVEL_LABEL.get(self.level, self.level.upper())


# ------------------------------------------------------------------ traits

def traits(m):
    """Everything the rules below care about, pulled out of one probe result."""
    pix = (m.get("pix_fmt") or "").lower()
    depth = 12 if "12" in pix else (10 if "10" in pix else 8)
    if "444" in pix:
        chroma = "4:4:4"
    elif "422" in pix:
        chroma = "4:2:2"
    elif "420" in pix:
        chroma = "4:2:0"
    else:
        chroma = "?"
    return {
        "codec": (m.get("video_codec") or "").lower(),
        "pix_fmt": pix,
        "depth": depth,
        "chroma": chroma,
        "long_gop": bool(m.get("has_b_frames")),
        "ext": os.path.splitext(m.get("name", ""))[1].lstrip(".").lower(),
        "profile": m.get("profile") or "",
        "name": m.get("name", ""),
    }


CODEC_LABEL = {"h264": "H.264", "hevc": "H.265", "prores": "ProRes",
               "dnxhd": "DNxHD/DNxHR", "mpeg2video": "MPEG-2",
               "mjpeg": "Motion JPEG", "dvvideo": "DV", "cfhd": "CineForm"}


def describe(t):
    codec = CODEC_LABEL.get(t["codec"], t["codec"].upper() or "unknown")
    parts = [codec]
    # Camera profiles often already spell out the chroma ("High 4:2:2 Intra"),
    # so only add the profile when it isn't saying the same thing twice.
    if t["profile"] and "4:2" not in t["profile"] and "4:4" not in t["profile"]:
        parts.append(t["profile"])
    parts.append(f"{t['chroma']} {t['depth']}-bit")
    parts.append("long-GOP" if t["long_gop"] else "intra-only")
    parts.append("." + (t["ext"] or "?"))
    return " ".join(parts)


def group_sources(medias):
    """Collapse the source list into distinct kinds, so 39 identical clips
    produce one line rather than 39."""
    groups = OrderedDict()
    for m in medias:
        if not m.get("has_video"):
            continue
        t = traits(m)
        key = (t["codec"], t["pix_fmt"], t["long_gop"], t["ext"], t["profile"])
        groups.setdefault(key, {"traits": t, "files": [], "media": []})
        groups[key]["files"].append(t["name"])
        groups[key]["media"].append(m)
    return list(groups.values())


# ------------------------------------------------------------------- rules

def _copy_findings(t, files, container, opts):
    """Rules for a stream copy, where the source essence lands in a new wrapper
    untouched and everything depends on whether that wrapper can hold it."""
    out = []
    codec, depth, chroma = t["codec"], t["depth"], t["chroma"]

    if container == "mxf" and t["long_gop"]:
        out.append(Finding(
            SEVERE,
            "Long-GOP video into MXF produces stuttering, broken files",
            "ffmpeg's MXF muxer only writes frame-reordering information for the "
            "intra-only essences it was built around (AVC-Intra, D-10, DNxHD, "
            "JPEG2000). Stream-copying long-GOP H.264/H.265 into MXF throws the "
            "reordering away: every packet claims to be displayed in the order it "
            "was stored and decode timestamps collide. Each group of three frames "
            "plays in the wrong order with one dropped -- measured at 31 of 48 "
            "frames surviving two seconds. The file still opens, still probes "
            "clean, and still reports the right duration.",
            "verified", files))

    if container == "mkv":
        out.append(Finding(
            SEVERE,
            "Premiere cannot import MKV at all",
            "MKV round-trips this footage perfectly and plays correctly "
            "everywhere else, so it is a fine archive wrapper -- but Premiere has "
            "no MKV importer, so these clips will not appear in your project.",
            "verified", files))

    if container in ("mov", "mp4") and codec == "h264" and (depth > 8 or chroma not in ("4:2:0", "?")):
        out.append(Finding(
            SEVERE,
            f"Premiere will not import {chroma} {depth}-bit H.264 from a {container.upper()}",
            "Premiere reaches this essence only through its Canon XF-AVC importer, "
            "which is keyed to the MXF wrapper. The same stream copied into a "
            f"{container.upper()} carries an avc1 tag, and Premiere's QuickTime "
            "importer handles only 8-bit 4:2:0 H.264 there. The file is correct -- "
            "it plays perfectly in VLC and decodes frame-for-frame identical to the "
            "camera original -- Premiere just will not open it. Combined with the "
            "MXF rule above, there is no stream-copy wrapper for this footage: "
            "re-encode to ProRes 422 HQ or DNxHR HQX instead.",
            "verified", files))

    elif container in ("mov", "mp4") and codec == "hevc" and chroma not in ("4:2:0", "?"):
        out.append(Finding(
            UNTESTED,
            f"{chroma} H.265 in a {container.upper()} has not been tested here",
            "4:2:0 HEVC imports into Premiere from MOV/MP4 without trouble. "
            f"{chroma} HEVC goes through a different path and we have not put it "
            "through a real Premiere install. Try one clip before committing a "
            "whole shoot -- and if it fails, an intra re-encode always works.",
            "untested", files))

    if container in ("mov", "mp4") and codec in ("h264", "hevc"):
        out.append(Finding(
            NOTE,
            "Access-unit delimiters stripped and faststart applied automatically",
            "Camera H.264 carries several kilobytes of Annex-B extradata (the C70 "
            "emits about 9.7 kB). The MOV muxer builds its avcC record straight "
            "from that and produces a malformed one, so the file opens as 'moov "
            "atom not found' or decodes to nothing. The AUD NALs are optional "
            "delimiters rather than picture data, so removing them is lossless -- "
            "verified bit-for-bit identical to the camera original, 804/804 and "
            "432/432 frames.",
            "verified", files))

    if container == "mp4" and opts.audio_codec.startswith("pcm"):
        out.append(Finding(
            WARN,
            "MP4 cannot carry PCM audio -- your recorder audio will be re-encoded to AAC",
            "ffmpeg nominally accepts PCM in MP4 but silently promotes it to "
            "pcm_s32le and the result decodes mis-timed, so AAC 320k is "
            "substituted instead. That is lossy. Use MOV if you want the recorder "
            "audio to arrive untouched.",
            "verified", files))

    if container == "mxf" and not t["long_gop"]:
        out.append(Finding(
            OK if codec in ("dnxhd", "mpeg2video", "mjpeg") else UNTESTED,
            f"Intra-only {CODEC_LABEL.get(codec, codec.upper())} into MXF",
            "Intra-only essence has no frame reordering for the muxer to lose, so "
            "the MXF problem does not apply here."
            + ("" if codec in ("dnxhd", "mpeg2video", "mjpeg") else
               " That said, this particular codec has not been round-tripped "
               "through MXF and Premiere by this tool."),
            "inferred" if codec in ("dnxhd", "mpeg2video", "mjpeg") else "untested",
            files))

    if not out or all(f.level in (NOTE, OK) for f in out):
        if container in ("mov",) and codec in ("h264", "hevc") and depth == 8 and chroma == "4:2:0":
            out.append(Finding(
                OK, "Stream copy into MOV should import normally",
                "8-bit 4:2:0 H.264/H.265 in a MOV is exactly what Premiere's "
                "QuickTime importer is built for, and the picture is bit-for-bit "
                "the camera original.",
                "verified", files))
    return out


def _encode_findings(t, files, container, opts, spec):
    """Rules for a re-encode, where we control the essence but pay for it."""
    out = []
    if container == "mkv":
        out.append(Finding(
            SEVERE, "Premiere cannot import MKV at all",
            "Whatever is inside it. Choose MOV, or MXF for the DNxHR profiles.",
            "verified", files))

    if container == "mxf":
        if spec.key.startswith("dnxhr"):
            out.append(Finding(
                OK, "DNxHR into MXF is the one safe MXF output",
                "DNxHR is intra-only, so ffmpeg's MXF muxer handles it correctly "
                "and the frame-order problem does not arise.",
                "inferred", files))
        elif spec.key.startswith("prores"):
            out.append(Finding(
                UNTESTED, "ProRes inside MXF has not been tested here",
                "ffmpeg can mux ProRes into MXF OP1a, and being intra-only it "
                "avoids the frame-order trap, but this combination has not been "
                "put through Premiere by this tool. ProRes in MOV is the "
                "well-trodden path.",
                "untested", files))

    if not spec.keeps_quality and (t["depth"] > 8 or t["chroma"] in ("4:2:2", "4:4:4")):
        out.append(Finding(
            WARN,
            f"This throws away colour information your camera recorded",
            f"The source is {t['chroma']} {t['depth']}-bit. "
            f"{spec.label} cannot carry that -- the extra chroma resolution and "
            "bit depth are discarded permanently in the output files. If these "
            "clips are what you grade from, use ProRes 422 HQ or DNxHR HQX "
            "instead.",
            "inferred", files))

    if not spec.intra:
        out.append(Finding(
            NOTE, "Long-GOP output re-encodes slowly and is harder to scrub",
            "Intra-only codecs (ProRes, DNxHR) decode any frame without reading "
            "its neighbours, which is what makes them comfortable to edit. "
            "H.264/H.265 output is smaller but heavier on the timeline.",
            "inferred", files))

    if spec.intra and container in ("mov", "mxf"):
        out.append(Finding(
            OK, f"{spec.label} imports everywhere",
            "Intra-only, widely supported, and unaffected by every wrapper trap "
            "above. The cost is disk space and encode time -- roughly real time.",
            "inferred", files))
    return out


def analyze(medias, clip_opts, dest_dir=None, include_disk=True):
    """
    Findings for the current export settings against these sources.

    medias: the probed video files (dicts as written into matches.json).
    """
    findings = []
    spec = clipmod.spec_for(clip_opts)

    for group in group_sources(medias):
        t, files = group["traits"], group["files"]
        # container_for needs a filename to resolve 'auto'
        container = clipmod.container_for(files[0] if files else "x.mov", clip_opts)
        if spec.key == "copy":
            findings += _copy_findings(t, files, container, clip_opts)
        else:
            findings += _encode_findings(t, files, container, clip_opts, spec)

    if include_disk:
        disk = disk_forecast(medias, clip_opts, dest_dir)
        if disk:
            findings.append(disk)

    findings.sort(key=lambda f: RANK.get(f.level, 9))
    return findings


def worst(findings):
    return min((RANK.get(f.level, 9) for f in findings), default=9)


def headline(findings):
    """One line for the always-visible strip above the log."""
    if not findings:
        return OK, "Pick your source folders and press Scan."
    counts = {}
    for f in findings:
        counts[f.level] = counts.get(f.level, 0) + 1
    for level in (SEVERE, WARN, UNTESTED):
        if counts.get(level):
            n = counts[level]
            thing = f"{n} thing{'s' if n > 1 else ''} about these export settings"
            verdict = {SEVERE: "will not work",
                       WARN: f"{'are' if n > 1 else 'is'} risky",
                       UNTESTED: f"{'have' if n > 1 else 'has'} never been tested"}[level]
            return level, f"{thing} {verdict} -- see the Compatibility tab"
    return OK, "These export settings look right for this footage."


# -------------------------------------------------------------- disk space

def human_bytes(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit not in ("B", "KB") else f"{n:,.0f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def disk_forecast(medias, clip_opts, dest_dir=None):
    """Predicted output size against free space on the destination drive."""
    videos = [m for m in medias if m.get("has_video")]
    if not videos:
        return None

    source_total = sum(m.get("file_size") or 0 for m in videos)
    parts = [clipmod.estimate_bytes(m, clip_opts) for m in videos]
    unknown = any(p is None for p in parts)
    out_total = sum(p for p in parts if p) if not unknown else None

    free = None
    if dest_dir:
        probe_dir = dest_dir
        while probe_dir and not os.path.isdir(probe_dir):
            parent = os.path.dirname(probe_dir)
            if parent == probe_dir:
                break
            probe_dir = parent
        if probe_dir and os.path.isdir(probe_dir):
            try:
                free = shutil.disk_usage(probe_dir).free
            except OSError:
                free = None

    detail = [f"Source footage: {human_bytes(source_total)}."]
    if out_total is None:
        detail.append("This codec is quality-targeted (CRF), so output size "
                      "depends on content and cannot be predicted -- expect "
                      "something well under the source for H.264/H.265.")
    else:
        ratio = out_total / source_total if source_total else 0
        detail.append(f"Estimated output: {human_bytes(out_total)}"
                      + (f" ({ratio:.1f}x the source)." if ratio else "."))
    if free is not None:
        detail.append(f"Free on the destination drive: {human_bytes(free)}.")

    level = NOTE
    if out_total and free is not None:
        if out_total > free:
            level = SEVERE
            detail.append("That does not fit. Choose a smaller codec or another drive.")
        elif out_total > free * 0.85:
            level = WARN
            detail.append("That fits, but only just.")

    return Finding(level, "Disk space", " ".join(detail), "inferred", [])


# ------------------------------------------------------------- xml findings

def xml_findings(opts):
    """Things worth knowing about the sequence route. Nothing here is a failure
    mode -- it is the route that works -- but the trade-offs are real."""
    out = [Finding(
        OK, "Sequences reference your original files -- nothing is transcoded",
        "The XML points at the camera media where it already sits, so this route "
        "costs no disk space and no encode time, and Premiere reads the footage "
        "through its own native importer. Verified importing into a live "
        "Premiere install.",
        "verified", [])]

    out.append(Finding(
        NOTE, "These are sequences, not merged clips",
        "Double-clicking one opens it as a timeline rather than loading it in "
        "the Source Monitor -- to use one as source you right-click and choose "
        "Open in Source Monitor, and it behaves as a nest on your timeline. "
        "Premiere has no sequence-to-clip conversion, batch or otherwise. If "
        "that friction matters, use pair bins plus Merge Clips, or the multicam "
        "route, both of which produce genuine master clips.",
        "verified", []))

    out.append(Finding(
        NOTE, "Moving or renaming the source files breaks the link",
        "The XML stores absolute paths. Import it before you reorganise the "
        "shoot folder, or Premiere will ask you to relink every clip.",
        "inferred", []))

    if opts.pair_bins and not opts.jam_timecode:
        out.append(Finding(
            WARN, "Pair bins without jam timecode makes Merge Clips much slower",
            "The point of pair bins is Ctrl+A then Merge Clips with sync point "
            "set to Timecode. Without jam timecode there is no matching timecode "
            "to merge on, so you would have to fall back to waveform analysis "
            "per clip.",
            "inferred", []))

    if opts.flat:
        out.append(Finding(
            NOTE, "Flat mode emits no master clips",
            "Sequences go straight into the top bin and their clip items point "
            "at the files directly. Tidier for a small shoot; you lose the VIDEO "
            "and AUDIO bins of real master clips.",
            "inferred", []))

    return out
