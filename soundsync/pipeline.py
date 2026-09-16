#!/usr/bin/env python3
"""
Running the stages together.

scan()     probe the source folders only -- fast, no decoding. This is what
           fills the Compatibility tab the moment you pick your folders, so
           you learn that MXF output would stutter before you spend 40 minutes
           writing 210 GB of it.

run_job()  the whole thing: match, then whichever outputs were asked for --
           including none of them, which is the match-only run: it works out
           what pairs with what, writes the report (and the CSV if asked), and
           leaves every file you own exactly as it found it.
"""

import os
from dataclasses import replace

from . import clips as clipmod
from . import compat, csvout, fcpxml, matcher, proc
from .options import JobOptions
from .report import Reporter


def scan(video_dir, audio_dir, recursive=False, report=None, cancel=None):
    """
    Probe every source file without decoding anything.

    Returns {"videos": [meta], "audios": [meta], "video_paths": [...],
             "audio_paths": [...], "errors": [(name, why)]}.
    """
    report = report or Reporter()
    cancel = cancel or proc.NULL_CANCELLER

    vpaths = matcher.gather(video_dir, matcher.VIDEO_EXT, recursive) if video_dir else []
    apaths = matcher.gather(audio_dir, matcher.AUDIO_EXT, recursive) if audio_dir else []

    videos, audios, errors = [], [], []
    total = len(vpaths) + len(apaths)
    for i, path in enumerate(vpaths + apaths, 1):
        cancel.check()
        report.progress(i, total)
        try:
            m = matcher.probe(path, cancel)
        except RuntimeError as e:
            errors.append((os.path.basename(path), str(e)))
            continue
        (videos if m.has_video else audios).append(m.meta())

    report.log(f"{len(videos)} video file(s), {len(audios)} audio file(s)"
               + (f", {len(errors)} unreadable" if errors else ""),
               "warn" if errors else "info")
    for name, why in errors:
        report.log(f"{name}: {why}", "error")

    return {"videos": videos, "audios": audios,
            "video_paths": vpaths, "audio_paths": apaths, "errors": errors}


def validate(job):
    """Everything wrong with these settings, as a list of plain sentences."""
    problems = []
    if not job.video_dir or not os.path.isdir(job.video_dir):
        problems.append("Pick a video folder that exists.")
    if not job.audio_dir or not os.path.isdir(job.audio_dir):
        problems.append("Pick an audio folder that exists.")
    if not job.dest_dir:
        problems.append("Pick a destination folder.")
    if job.make_xml and not job.xml_name.strip():
        problems.append("The XML needs a filename.")
    if job.make_csv and not job.csv_name.strip():
        problems.append("The CSV needs a filename.")
    if (job.video_dir and job.audio_dir
            and os.path.abspath(job.video_dir) == os.path.abspath(job.audio_dir)):
        problems.append("The video and audio folders are the same folder. That is "
                        "allowed only if the file types differ -- check it is what "
                        "you meant.")
    return problems


def run_job(job, report=None, cancel=None, existing_matches=None):
    """
    Match and export. Returns a summary dict.

    existing_matches lets the GUI re-export from a previous analysis when only
    the output settings changed -- no point fingerprinting 39 clips again to
    switch from ProRes to DNxHR.
    """
    report = report or Reporter()
    cancel = cancel or proc.NULL_CANCELLER
    result = {"matches": None, "xml": None, "clips": None, "csv": None}

    problems = validate(job)
    if problems:
        raise ValueError("\n".join(problems))

    os.makedirs(job.dest_dir, exist_ok=True)

    if existing_matches is not None:
        data = existing_matches
        report.stage("Re-using existing matches",
                     f"{len(data.get('matches', []))} matched pair(s)")
    else:
        data = matcher.run_match(job.video_dir, job.audio_dir, job.match,
                                 job.matches_path(), report, cancel)
    result["matches"] = data

    # The CSV goes first and is written even when nothing cleared the
    # threshold -- a list of best guesses is exactly what you want to look at
    # when the answer is "nothing matched".
    if job.make_csv:
        cancel.check()
        report.stage("Writing the pairing list")
        result["csv"] = csvout.write_csv(data, job.csv_path(), report)

    if not data.get("matches"):
        report.log("Nothing matched, so there is nothing to export. Lower the "
                   "confidence threshold on the Matching tab, or run Diagnose to "
                   "see whether the clips have usable scratch audio at all.",
                   "error")
        return result

    if not job.make_xml and not job.make_clips:
        report.stage("Finished", "match only -- nothing was exported")
        report.log(f"Match report: {job.matches_path()}", "good")
        if result["csv"]:
            report.log(f"CSV: {result['csv']['out_path']}", "good")
        return result

    if job.make_xml:
        cancel.check()
        xml_opts = replace(job.xml, out_path=job.xml_path())
        result["xml"] = fcpxml.run_xml(data, xml_opts, report, cancel)

    if job.make_clips:
        cancel.check()
        clip_opts = replace(job.clips, out_dir=job.clips_dir())
        result["clips"] = clipmod.run_clips(data, clip_opts, report, cancel)

    report.stage("Finished")
    if result.get("csv"):
        report.log(f"CSV: {result['csv']['out_path']}", "good")
    if job.make_xml and result.get("xml"):
        report.log(f"XML: {result['xml']['out_path']}", "good")
    if job.make_clips and result.get("clips") and not result["clips"].get("dry_run"):
        report.log(f"Clips: {job.clips_dir()}  "
                   f"({len(result['clips']['written'])} written)", "good")
    return result


def preflight(medias, clip_opts, dest_dir):
    """Findings for the media-file route, worst first."""
    return compat.analyze(medias, clip_opts, dest_dir)
