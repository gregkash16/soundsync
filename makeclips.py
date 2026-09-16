#!/usr/bin/env python3
"""
makeclips.py -- stage 2b on the command line.

Bakes each matched pair into a new self-contained media file. The work happens
in soundsync/clips.py; this file is the CLI.

    python makeclips.py matches.json --out-dir "D:\\Shoot\\Day1\\Synced" --jam-timecode

Before committing a whole shoot, check what your chosen format will actually
do to your footage:

    python makeclips.py matches.json --check --video-codec copy --container mxf
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from soundsync import clips, compat, fcpxml, proc   # noqa: E402
from soundsync.options import ClipOptions           # noqa: E402
from soundsync.report import ConsoleReporter        # noqa: E402


def main():
    ap = argparse.ArgumentParser(
        description="Bake matched pairs into new self-contained clips.")
    ap.add_argument("matches", nargs="?", default="matches.json")
    ap.add_argument("--out-dir", default="Synced",
                    help="where to write the finished clips (default: ./Synced)")
    ap.add_argument("--video-codec", default="copy", choices=clips.CODEC_ORDER,
                    help="copy (default, lossless stream copy) or an intra codec to "
                         "re-encode to: prores_hq, prores_422, prores_lt, "
                         "prores_4444, dnxhr_hqx, dnxhr_sq, h264, h265")
    ap.add_argument("--container", default="auto",
                    help="output wrapper. 'auto' keeps the source's own wrapper for a "
                         "stream copy, except where that wrapper can't hold the stream "
                         "correctly -- MXF and MP4 sources become MOV. For a re-encode "
                         "'auto' means the codec's usual wrapper. Forcing mxf on "
                         "long-GOP footage produces stuttering video.")
    ap.add_argument("--suffix", default="_synced")
    ap.add_argument("--audio-codec", default="pcm_s24le",
                    help="pcm_s24le (default, lossless), pcm_s16le, or aac")
    ap.add_argument("--reencode", action="store_true",
                    help="deprecated alias for --video-codec h264")
    ap.add_argument("--keep-camera-audio", action="store_true",
                    help="keep the camera scratch audio as an extra track")
    ap.add_argument("--no-pad", action="store_true",
                    help="don't pad short audio with silence")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the post-render check that decodes the first few "
                         "seconds of each clip and confirms the frames come back "
                         "in order")
    ap.add_argument("--no-fallback", action="store_true",
                    help="don't retry a failed clip as MOV or as a re-encode")
    ap.add_argument("--jam-timecode", action="store_true",
                    help="stamp each roll's clips with matching timecode")
    ap.add_argument("--jobs", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the ffmpeg commands without running them")
    ap.add_argument("--check", action="store_true",
                    help="report what these settings will do to this footage, "
                         "then exit without writing anything")
    args = ap.parse_args()

    if not os.path.isfile(args.matches):
        raise SystemExit(f"no such file: {args.matches}")
    data = fcpxml.load_matches(args.matches)

    opts = ClipOptions(
        out_dir=args.out_dir,
        container=args.container,
        video_codec="h264" if args.reencode and args.video_codec == "copy" else args.video_codec,
        audio_codec=args.audio_codec,
        suffix=args.suffix,
        keep_camera_audio=args.keep_camera_audio,
        pad=not args.no_pad,
        verify=not args.no_verify,
        fallback=not args.no_fallback,
        jam_timecode=args.jam_timecode,
        jobs=args.jobs,
        dry_run=args.dry_run)

    medias = [m for m in data.get("media", {}).values() if m.get("has_video")]
    findings = compat.analyze(medias, opts, opts.out_dir)

    if args.check:
        print(f"\n{clips.spec_for(opts).label} -> "
              f"{clips.container_for(medias[0]['name'] if medias else 'x.mov', opts).upper()}\n")
        for f in findings:
            print(f"[{f.label}] {f.title}   ({f.evidence})")
            print("    " + f.detail.replace("\n", "\n    "))
            if f.files:
                print(f"    affects {len(f.files)} file(s)")
            print()
        return 0

    severe = [f for f in findings if f.level == compat.SEVERE]
    for f in findings:
        if f.level in (compat.SEVERE, compat.WARN, compat.UNTESTED):
            print(f"[{f.label}] {f.title} ({f.evidence})")
    if severe:
        print("\nRun with --check to read the detail. Continuing anyway in 3 seconds; "
              "Ctrl+C to stop.")
        try:
            import time
            time.sleep(3)
        except KeyboardInterrupt:
            return 1

    try:
        result = clips.run_clips(data, opts, ConsoleReporter())
    except proc.ToolsMissing as e:
        raise SystemExit(str(e))
    except ValueError as e:
        raise SystemExit(str(e))
    return 0 if not result.get("failed") else 1


if __name__ == "__main__":
    sys.exit(main())
