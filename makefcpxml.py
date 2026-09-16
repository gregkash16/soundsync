#!/usr/bin/env python3
"""
makefcpxml.py -- stage 2a on the command line.

Turns matches.json into FCP7 XML (xmeml v5) that Premiere imports as bins of
synced sequences referencing your original media. The work happens in
soundsync/fcpxml.py; this file is the CLI.

    python makefcpxml.py matches.json --out merged.xml --pair-bins --jam-timecode
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from soundsync import fcpxml                 # noqa: E402
from soundsync.options import XmlOptions     # noqa: E402
from soundsync.report import ConsoleReporter  # noqa: E402


def main():
    ap = argparse.ArgumentParser(
        description="Convert matches.json into Final Cut Pro XML for Premiere.")
    ap.add_argument("matches", nargs="?", default="matches.json")
    ap.add_argument("--out", default="merged.xml")
    ap.add_argument("--bin", default="Merged", help="top-level bin name")
    ap.add_argument("--video-bin", default="VIDEO")
    ap.add_argument("--audio-bin", default="AUDIO")
    ap.add_argument("--sequence-bin", default="SEQUENCES")
    ap.add_argument("--pair-bins", action="store_true",
                    help="one bin per matched pair, each holding just that video "
                         "and its audio. Makes Merge Clips fast: open a bin, "
                         "Ctrl+A, merge shortcut, Enter.")
    ap.add_argument("--flat", action="store_true",
                    help="skip the sub-bins and put the sequences straight in "
                         "the top-level bin")
    ap.add_argument("--include-unmatched", action="store_true",
                    help="also put unmatched clips and unused audio in the VIDEO "
                         "and AUDIO bins, so nothing is left out of the project")
    ap.add_argument("--jam-timecode", action="store_true",
                    help="stamp matching timecode on each pair, so the footage "
                         "behaves as if it had been jam-synced on set. Lets you "
                         "use Merge Clips > Timecode, or batch multicam by "
                         "timecode, to get real master clips.")
    ap.add_argument("--keep-camera-audio", action="store_true",
                    help="also include the camera's scratch audio on a muted track")
    args = ap.parse_args()

    if not os.path.isfile(args.matches):
        raise SystemExit(f"no such file: {args.matches}")

    structure = "pairs" if args.pair_bins else ("flat" if args.flat else "subbins")
    opts = XmlOptions(out_path=args.out, bin=args.bin, video_bin=args.video_bin,
                      audio_bin=args.audio_bin, sequence_bin=args.sequence_bin,
                      structure=structure,
                      include_unmatched=args.include_unmatched,
                      jam_timecode=args.jam_timecode,
                      keep_camera_audio=args.keep_camera_audio)

    try:
        fcpxml.run_xml(fcpxml.load_matches(args.matches), opts, ConsoleReporter())
    except ValueError as e:
        raise SystemExit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
