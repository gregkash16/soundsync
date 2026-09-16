#!/usr/bin/env python3
"""
Stage 2a -- turn matches into an XML file Premiere Pro can import.

Produces FCP7-style XML (xmeml v5), which is what Premiere's File > Import
understands as "Final Cut Pro XML".

Importing builds this structure in the project panel:

    Merged/
        VIDEO/       master clips for the camera files
        AUDIO/       master clips for the recorder files
        SEQUENCES/   one synced sequence per matched clip

Each sequence carries video on V1 and the recorder audio on A1 (A1/A2/... for
multichannel), offset so the two line up. It references the original camera
files where they already sit -- nothing is copied, converted or re-encoded.

Sequences rather than merged clips, for two reasons: Merge Clips has no
scripting API at all, and merged clips are known not to survive XML interchange.
"""

import json
import os
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from xml.dom import minidom

from .options import XmlOptions
from .report import Reporter

# Frame rates that are really 1000/1001 of an integer timebase.
NTSC_RATES = {23.976: 24, 23.98: 24, 29.97: 30, 47.952: 48, 59.94: 60, 119.88: 120}
NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def timebase_of(fps):
    """FCP XML wants an integer timebase plus an NTSC flag, not a float rate."""
    if not fps or fps <= 0:
        return 30, "FALSE"
    for approx, tb in NTSC_RATES.items():
        if abs(fps - approx) < 0.02:
            return tb, "TRUE"
    if abs(fps - round(fps)) < 0.001:
        return int(round(fps)), "FALSE"
    tb = int(round(fps))
    return tb, ("TRUE" if abs(fps - tb * 1000.0 / 1001.0) < 0.02 else "FALSE")


def pathurl(path):
    r"""Windows C:\a\b.mxf -> file://localhost/C:/a/b.mxf, properly escaped."""
    p = os.path.abspath(path).replace("\\", "/")
    if len(p) > 1 and p[1] == ":":
        p = "/" + p
    return "file://localhost" + urllib.parse.quote(p)


def sec_to_frames(seconds, timebase, ntsc):
    rate = timebase * 1000.0 / 1001.0 if ntsc == "TRUE" else float(timebase)
    return int(round(seconds * rate))


def sub(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def add_rate(parent, timebase, ntsc):
    r = sub(parent, "rate")
    sub(r, "timebase", timebase)
    sub(r, "ntsc", ntsc)


def frames_to_tc(frames, timebase):
    """Frame count -> HH:MM:SS:FF, non-drop. Kept non-drop deliberately so the
    string and the frame number can never disagree."""
    frames = max(0, int(frames))
    ff = frames % timebase
    total_s = frames // timebase
    return f"{total_s // 3600:02d}:{(total_s // 60) % 60:02d}:{total_s % 60:02d}:{ff:02d}"


def add_timecode(parent, timebase, ntsc, start_frames=0):
    tc = sub(parent, "timecode")
    add_rate(tc, timebase, ntsc)
    sub(tc, "string", frames_to_tc(start_frames, timebase))
    sub(tc, "frame", int(start_frames))
    sub(tc, "displayformat", "NDF")


class Registry:
    """
    Hands out stable ids and emits each source file's <file> definition once.

    This matters: one recorder roll can back several sequences, and if its full
    definition is repeated Premiere imports it as several separate master clips
    instead of one. The full definition goes in the master clip; everything
    afterwards gets a bare <file id="..."/> stub pointing at it.
    """

    def __init__(self, start_tc=None):
        self.file_ids = {}
        self.master_ids = {}
        # path -> start timecode in SECONDS, when jam-syncing is switched on
        self.start_tc = start_tc or {}

    def tc_frames(self, path, timebase, ntsc):
        secs = self.start_tc.get(path)
        return 0 if secs is None else sec_to_frames(secs, timebase, ntsc)

    def master_id(self, path, instance=None):
        # A file can need more than one master clip: in pair-bin mode the same
        # recorder roll sits in several bins at once, and a master clip can only
        # live in one bin. The <file> definition is still emitted once.
        key = (path, instance)
        if key not in self.master_ids:
            self.master_ids[key] = f"masterclip-{len(self.master_ids) + 1}"
        return self.master_ids[key]

    def attach_file(self, parent, media, timebase, ntsc):
        key = media["path"]
        if key in self.file_ids:
            ET.SubElement(parent, "file", {"id": self.file_ids[key]})
            return
        fid = f"file-{len(self.file_ids) + 1}"
        self.file_ids[key] = fid

        f = ET.SubElement(parent, "file", {"id": fid})
        sub(f, "name", media["name"])
        sub(f, "pathurl", pathurl(media["path"]))
        add_rate(f, timebase, ntsc)
        sub(f, "duration", sec_to_frames(media["duration"], timebase, ntsc))
        add_timecode(f, timebase, ntsc, self.tc_frames(key, timebase, ntsc))

        m = sub(f, "media")
        if media.get("has_video"):
            v = sub(m, "video")
            sc = sub(v, "samplecharacteristics")
            add_rate(sc, timebase, ntsc)
            sub(sc, "width", media.get("width") or 1920)
            sub(sc, "height", media.get("height") or 1080)
            sub(sc, "anamorphic", "FALSE")
            sub(sc, "pixelaspectratio", "square")
            sub(sc, "fielddominance", "none")
        if media.get("has_audio"):
            a = sub(m, "audio")
            sc = sub(a, "samplecharacteristics")
            sub(sc, "depth", 16)
            sub(sc, "samplerate", 48000)
            sub(a, "channelcount", audio_channel_count(media))


def audio_channel_count(media):
    """
    Total mono channels the file exposes.

    Broadcast MXF usually carries several discrete single-channel streams rather
    than one multichannel stream, so the channel count is streams x channels.
    """
    streams = max(1, media.get("audio_streams") or 1)
    per = max(1, media.get("audio_channels") or 1)
    return streams * per


def build_master_clip(media, reg, timebase, ntsc, instance=None):
    """A master clip -- what shows up as a clip in a bin, backed by one file."""
    mcid = reg.master_id(media["path"], instance)
    frames = max(1, sec_to_frames(media["duration"], timebase, ntsc))

    clip = ET.Element("clip", {"id": mcid})
    sub(clip, "uuid", str(uuid.uuid5(NS, f"{media['path']}|{instance}")))
    sub(clip, "masterclipid", mcid)
    sub(clip, "ismasterclip", "TRUE")
    sub(clip, "name", media["name"])
    sub(clip, "duration", frames)
    add_rate(clip, timebase, ntsc)
    sub(clip, "in", -1)
    sub(clip, "out", -1)
    add_timecode(clip, timebase, ntsc, reg.tc_frames(media["path"], timebase, ntsc))

    m = sub(clip, "media")
    if media.get("has_video"):
        v = sub(m, "video")
        tr = sub(v, "track")
        ci = ET.SubElement(tr, "clipitem", {"id": f"{mcid}-v"})
        sub(ci, "masterclipid", mcid)
        sub(ci, "name", media["name"])
        sub(ci, "duration", frames)
        add_rate(ci, timebase, ntsc)
        sub(ci, "in", 0)
        sub(ci, "out", frames)
        reg.attach_file(ci, media, timebase, ntsc)
        st = sub(ci, "sourcetrack")
        sub(st, "mediatype", "video")
        sub(st, "trackindex", 1)

    if media.get("has_audio"):
        a = sub(m, "audio")
        for ch in range(1, audio_channel_count(media) + 1):
            tr = sub(a, "track")
            ci = ET.SubElement(tr, "clipitem", {"id": f"{mcid}-a{ch}"})
            sub(ci, "masterclipid", mcid)
            sub(ci, "name", media["name"])
            sub(ci, "duration", frames)
            add_rate(ci, timebase, ntsc)
            sub(ci, "in", 0)
            sub(ci, "out", frames)
            reg.attach_file(ci, media, timebase, ntsc)
            st = sub(ci, "sourcetrack")
            sub(st, "mediatype", "audio")
            sub(st, "trackindex", ch)
    return clip


def build_sequence(idx, pair, vmeta, ameta, reg, keep_camera_audio,
                   vmc=None, amc=None, use_masterclips=True):
    timebase, ntsc = timebase_of(vmeta.get("fps") or 30)
    vframes = sec_to_frames(vmeta["duration"], timebase, ntsc)
    aframes = sec_to_frames(ameta["duration"], timebase, ntsc)
    if vframes <= 0:
        return None

    # In flat mode no master clips are emitted, so referencing them would leave
    # dangling ids; the clipitems just point straight at the files instead.
    vmc = (vmc or reg.master_id(vmeta["path"])) if use_masterclips else None
    amc = (amc or reg.master_id(ameta["path"])) if use_masterclips else None

    seq = ET.Element("sequence", {"id": f"sequence-{idx}"})
    stem = os.path.splitext(vmeta["name"])[0]
    sub(seq, "uuid", str(uuid.uuid5(NS, "seq:" + vmeta["path"])))
    sub(seq, "name", f"{stem} [synced]")
    sub(seq, "duration", vframes)
    add_rate(seq, timebase, ntsc)
    add_timecode(seq, timebase, ntsc)

    media = sub(seq, "media")

    # ---- video ----------------------------------------------------------
    video = sub(media, "video")
    fmt = sub(video, "format")
    sc = sub(fmt, "samplecharacteristics")
    add_rate(sc, timebase, ntsc)
    sub(sc, "width", vmeta.get("width") or 1920)
    sub(sc, "height", vmeta.get("height") or 1080)
    sub(sc, "anamorphic", "FALSE")
    sub(sc, "pixelaspectratio", "square")
    sub(sc, "fielddominance", "none")

    vtrack = sub(video, "track")
    ci = ET.SubElement(vtrack, "clipitem", {"id": f"clipitem-{idx}-v"})
    if vmc:
        sub(ci, "masterclipid", vmc)
    sub(ci, "name", vmeta["name"])
    sub(ci, "duration", vframes)
    add_rate(ci, timebase, ntsc)
    sub(ci, "start", 0)
    sub(ci, "end", vframes)
    sub(ci, "in", 0)
    sub(ci, "out", vframes)
    reg.attach_file(ci, vmeta, timebase, ntsc)
    st = sub(ci, "sourcetrack")
    sub(st, "mediatype", "video")
    sub(st, "trackindex", 1)

    # ---- audio ----------------------------------------------------------
    # offset = the point in the audio file that lines up with the video's first
    # frame. Positive: recorder was already rolling, so trim into it. Negative:
    # recorder started late, so the audio sits further along the timeline.
    off = sec_to_frames(pair["offset"], timebase, ntsc)
    a_in, a_start = (off, 0) if off >= 0 else (0, -off)

    a_len = min(vframes - a_start, aframes - a_in)
    if a_len <= 0:
        return None                      # no usable overlap; caller drops it
    a_out, a_end = a_in + a_len, a_start + a_len

    audio = sub(media, "audio")
    afmt = sub(audio, "format")
    asc = sub(afmt, "samplecharacteristics")
    sub(asc, "depth", 16)
    sub(asc, "samplerate", 48000)

    for ch in range(1, audio_channel_count(ameta) + 1):
        tr = sub(audio, "track")
        aci = ET.SubElement(tr, "clipitem", {"id": f"clipitem-{idx}-a{ch}"})
        if amc:
            sub(aci, "masterclipid", amc)
        sub(aci, "name", ameta["name"])
        sub(aci, "duration", aframes)
        add_rate(aci, timebase, ntsc)
        sub(aci, "start", a_start)
        sub(aci, "end", a_end)
        sub(aci, "in", a_in)
        sub(aci, "out", a_out)
        reg.attach_file(aci, ameta, timebase, ntsc)
        st = sub(aci, "sourcetrack")
        sub(st, "mediatype", "audio")
        sub(st, "trackindex", ch)

    if keep_camera_audio and vmeta.get("has_audio"):
        for ch in range(1, audio_channel_count(vmeta) + 1):
            tr = sub(audio, "track")
            sub(tr, "enabled", "FALSE")           # reference only, muted
            aci = ET.SubElement(tr, "clipitem", {"id": f"clipitem-{idx}-cam{ch}"})
            if vmc:
                sub(aci, "masterclipid", vmc)
            sub(aci, "name", vmeta["name"])
            sub(aci, "duration", vframes)
            add_rate(aci, timebase, ntsc)
            sub(aci, "start", 0)
            sub(aci, "end", vframes)
            sub(aci, "in", 0)
            sub(aci, "out", vframes)
            reg.attach_file(aci, vmeta, timebase, ntsc)
            st = sub(aci, "sourcetrack")
            sub(st, "mediatype", "audio")
            sub(st, "trackindex", ch)

    return seq


def plan_jam_timecode(matches, media, hour_step=1):
    """
    Invent matching timecode for every file, so pairs come out jam-synced.

    Each audio roll is anchored at its own hour (01:00:00:00, 02:00:00:00, ...)
    and every video matched to it is stamped at that anchor plus its measured
    offset. The footage then behaves exactly as if it had been jam-synced on set:
    Merge Clips > Timecode lines it up perfectly, and multicam by timecode
    becomes instant and exact instead of re-analysing waveforms.

    Separate hours per roll stop clips from different rolls colliding in
    timecode space and being grouped together by mistake.
    """
    start = {}
    anchors = {}
    for pair in matches:
        vm, am = media.get(pair["video"]), media.get(pair["audio"])
        if not vm or not am:
            continue
        if am["path"] not in anchors:
            anchors[am["path"]] = 3600.0 * (len(anchors) + 1) * hour_step
            start[am["path"]] = anchors[am["path"]]
        start[vm["path"]] = anchors[am["path"]] + pair["offset"]
    return start


def project_timebase(media):
    """Default rate for the project, taken from the commonest video frame rate."""
    rates = [m["fps"] for m in media.values() if m.get("has_video") and m.get("fps")]
    if not rates:
        return 30, "FALSE"
    return timebase_of(Counter(round(r, 3) for r in rates).most_common(1)[0][0])


def run_xml(data, opts=None, report=None, cancel=None):
    """
    The whole of stage 2a. Writes opts.out_path and returns a summary dict.
    """
    opts = opts or XmlOptions()
    report = report or Reporter()

    media = data.get("media", {})
    matches = data.get("matches", [])
    if not matches:
        raise ValueError("there are no accepted matches to build sequences from")
    if not opts.out_path:
        raise ValueError("no output path was set for the XML")

    report.stage("Building sequences", f"{len(matches)} matched pair(s)")

    proj_tb, proj_ntsc = project_timebase(media)

    root = ET.Element("xmeml", {"version": "5"})
    project = sub(root, "project")
    sub(project, "name", opts.bin)
    top = sub(sub(project, "children"), "bin")
    sub(top, "name", opts.bin)
    top_children = sub(top, "children")

    def make_bin(name):
        b = sub(top_children, "bin")
        sub(b, "name", name)
        return sub(b, "children")

    tc_plan = plan_jam_timecode(matches, media) if opts.jam_timecode else None
    reg = Registry(tc_plan)

    # Which files are actually involved, in a stable order.
    vpaths, apaths = [], []
    for p in matches:
        for name, bucket in ((p["video"], vpaths), (p["audio"], apaths)):
            m = media.get(name)
            if m and m["path"] not in bucket:
                bucket.append(m["path"])

    if opts.include_unmatched:
        for name in data.get("unmatched_video", []):
            m = media.get(name)
            if m and m["path"] not in vpaths:
                vpaths.append(m["path"])
        for name, m in media.items():
            if not m.get("has_video") and m["path"] not in apaths:
                apaths.append(m["path"])

    by_path = {m["path"]: m for m in media.values()}

    # Master clips first: that's where each <file> gets its full definition,
    # so the sequences below can reference them as stubs.
    pair_ids = {}
    if opts.pair_bins:
        # One bin per pair. Each holds only that video and its audio, so the
        # whole bin can be selected and merged in one keystroke.
        for i, pair in enumerate(matches, 1):
            cancel and cancel.check()
            vmeta, ameta = media.get(pair["video"]), media.get(pair["audio"])
            if not vmeta or not ameta:
                continue
            stem = os.path.splitext(vmeta["name"])[0]
            children = make_bin(stem)
            tb, nt = (timebase_of(vmeta["fps"]) if vmeta.get("fps")
                      else (proj_tb, proj_ntsc))
            children.append(build_master_clip(vmeta, reg, tb, nt, f"pair{i}"))
            children.append(build_master_clip(ameta, reg, proj_tb, proj_ntsc, f"pair{i}"))
            pair_ids[i] = (reg.master_id(vmeta["path"], f"pair{i}"),
                           reg.master_id(ameta["path"], f"pair{i}"))
        seq_children = make_bin(opts.sequence_bin) if opts.sequence_bin else top_children
    elif not opts.flat:
        vbin = make_bin(opts.video_bin)
        for path in vpaths:
            m = by_path[path]
            tb, nt = timebase_of(m.get("fps") or 0) if m.get("fps") else (proj_tb, proj_ntsc)
            vbin.append(build_master_clip(m, reg, tb, nt))

        abin = make_bin(opts.audio_bin)
        for path in apaths:
            abin.append(build_master_clip(by_path[path], reg, proj_tb, proj_ntsc))

        seq_children = make_bin(opts.sequence_bin)
    else:
        seq_children = top_children

    made, dropped = 0, []
    for i, pair in enumerate(matches, 1):
        cancel and cancel.check()
        report.progress(i, len(matches))
        vmeta, ameta = media.get(pair["video"]), media.get(pair["audio"])
        if not vmeta or not ameta:
            dropped.append((pair["video"], "missing media metadata"))
            continue
        vmc, amc = pair_ids.get(i, (None, None))
        seq = build_sequence(i, pair, vmeta, ameta, reg, opts.keep_camera_audio,
                             vmc, amc, use_masterclips=not opts.flat)
        if seq is None:
            dropped.append((pair["video"], "no usable overlap at the computed offset"))
            continue
        seq_children.append(seq)
        made += 1

    if not made:
        raise ValueError("no sequence could be built from these matches")

    pretty = minidom.parseString(ET.tostring(root, encoding="unicode")).toprettyxml(indent=" ")
    body = "\n".join(line for line in pretty.split("\n")[1:] if line.strip())
    os.makedirs(os.path.dirname(os.path.abspath(opts.out_path)) or ".", exist_ok=True)
    with open(opts.out_path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        fh.write(body + "\n")

    report.stage("XML written", f"{made} sequence(s) -> {opts.out_path}")
    if opts.pair_bins:
        report.log(f"{opts.bin}: {len(pair_ids)} pair bin(s), one per matched clip")
    elif not opts.flat:
        report.log(f"{opts.bin}/{opts.video_bin}: {len(vpaths)} master clip(s)")
        report.log(f"{opts.bin}/{opts.audio_bin}: {len(apaths)} master clip(s)")
        report.log(f"{opts.bin}/{opts.sequence_bin}: {made} sequence(s)")
    for name, why in dropped:
        report.log(f"dropped {name}: {why}", "warn")
    if opts.jam_timecode:
        report.log("Jam-sync timecode applied -- each recorder roll anchored on its "
                   "own hour, so Merge Clips > Timecode and multicam-by-timecode "
                   "both line up exactly.")
    report.log(f"In Premiere: File > Import, choose {os.path.basename(opts.out_path)}.",
               "good")

    return {"sequences": made, "dropped": dropped, "out_path": opts.out_path,
            "video_clips": len(vpaths), "audio_clips": len(apaths)}


def load_matches(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
