#!/usr/bin/env python3
"""
syncmatch.py -- stage 1 on the command line.

Matches video clips to separately-recorded audio by waveform sync and writes
matches.json. The work happens in soundsync/matcher.py; this file is the CLI.

    python syncmatch.py --video "D:\\Shoot\\Day1\\Video" --audio "D:\\Shoot\\Day1\\Audio"
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from soundsync import csvout, matcher, proc  # noqa: E402
from soundsync.options import MatchOptions   # noqa: E402
from soundsync.report import ConsoleReporter  # noqa: E402


def main():
    ap = argparse.ArgumentParser(
        description="Match video clips to separately-recorded audio by waveform sync.")
    ap.add_argument("--video", required=True, help="folder of video files")
    ap.add_argument("--audio", required=True, help="folder of audio files")
    ap.add_argument("--out", default="matches.json")
    ap.add_argument("--csv", nargs="?", const="", metavar="PATH",
                    help="also write the pairing list as a spreadsheet: video, "
                         "best sound, confidence %%, second choice, confidence %%. "
                         "Give a path, or pass --csv on its own to write it next "
                         "to --out.")
    ap.add_argument("--min-z", type=float, default=12.0,
                    help="confidence threshold (default 12). True syncs typically "
                         "score 20-40, unrelated pairs under 9. Lower it to catch "
                         "marginal clips, raise it if you get a wrong pairing.")
    ap.add_argument("--min-prominence", type=float, default=1.2,
                    help="minimum peak-to-runner-up ratio (default 1.2)")
    ap.add_argument("--one-to-one", action="store_true",
                    help="each audio file may serve only one video. Off by default, "
                         "so one long recorder roll can cover several camera takes.")
    ap.add_argument("--recursive", action="store_true",
                    help="search sub-folders of the source folders too")
    ap.add_argument("--jobs", type=int, default=0,
                    help="parallel decode workers (default: one per CPU core)")
    ap.add_argument("--diagnose", action="store_true",
                    help="just report what's inside each file (streams, levels) "
                         "and exit -- use this when clips are being skipped")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    report = ConsoleReporter(verbose=not args.quiet)
    opts = MatchOptions(min_z=args.min_z, min_prominence=args.min_prominence,
                        one_to_one=args.one_to_one, jobs=args.jobs,
                        recursive=args.recursive)

    try:
        if args.diagnose:
            matcher.diagnose(matcher.gather(args.video, matcher.VIDEO_EXT, opts.recursive),
                             "VIDEO", report)
            matcher.diagnose(matcher.gather(args.audio, matcher.AUDIO_EXT, opts.recursive),
                             "AUDIO", report)
            return 0

        data = matcher.run_match(args.video, args.audio, opts, args.out, report)
        if args.csv is not None:
            path = args.csv or (os.path.splitext(args.out)[0] + ".csv")
            csvout.write_csv(data, path, report)
    except proc.ToolsMissing as e:
        raise SystemExit(str(e))
    except ValueError as e:
        raise SystemExit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
