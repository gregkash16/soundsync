#!/usr/bin/env python3
"""
The pairing list as a spreadsheet.

This is the output for when you don't want the tool to touch your media at
all -- you just want to be told which sound file goes with which clip, and how
sure the tool is, so you can do the merging yourself in Premiere.

One row per video file, in the order the clips were analysed:

    A  Video              the camera clip
    B  Sound              the audio file it most likely belongs to
    C  Confidence         0-100, see matcher.confidence()
    D  Second choice      the next most likely audio file
    E  Confidence         its score, for comparison

A clip with nothing to pair against (no scratch audio, unreadable, silent)
still gets a row, with the reason in column B and blank scores. A clip that
matched but has only one audio file to choose from gets blanks in D and E.
"""

import csv
import os

HEADER = ["Video", "Sound", "Confidence %", "Second choice", "Confidence %"]


def _rows(data):
    """Every video the analysis saw, best candidate first, worst news last."""
    rows = []
    seen = set()

    accepted = data.get("matches") or []
    rejected = data.get("rejected") or []
    unmatched = set(data.get("unmatched_video") or [])

    for p in accepted + rejected:
        name = p.get("video")
        if not name or name in seen:
            continue
        seen.add(name)
        cands = p.get("candidates") or []
        if not cands:
            # An older matches.json, written before runner-ups were kept.
            cands = [{"audio": p.get("audio", ""),
                      "confidence": p.get("confidence", "")}]
        first = cands[0]
        second = cands[1] if len(cands) > 1 else {}
        label = first.get("audio", "")
        if name in unmatched:
            label = f"{label}  (below threshold -- check this one)" if label else ""
        rows.append([name, label, first.get("confidence", ""),
                     second.get("audio", ""), second.get("confidence", "")])

    for s in data.get("skipped") or []:
        name = s.get("file")
        if not name or name in seen:
            continue
        seen.add(name)
        rows.append([name, f"not analysed -- {s.get('reason', 'skipped')}", "", "", ""])

    return rows


def write_csv(data, out_path, report=None):
    """Write the pairing list. Returns {"out_path", "rows"}."""
    rows = _rows(data)
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    # newline="" is what stops csv writing a blank line between every row on
    # Windows; utf-8-sig is what stops Excel mangling accented filenames.
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        w.writerows(rows)
    if report:
        report.log(f"CSV: {out_path}  ({len(rows)} clip(s))", "good")
    return {"out_path": out_path, "rows": len(rows)}
