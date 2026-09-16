#!/usr/bin/env python3
"""
Sound Syncing -- desktop front end.

.pyw so double-clicking it on Windows opens the window without a console
behind it. The packaged build runs this same entry point.

Run with --selftest to import everything and exit without opening a window.
build.py does that against the freshly built exe: a packaged app can fail at
launch over a module the build left out, and a windowed exe has nowhere to
print the traceback, so the check writes it to a file and reports through the
exit code instead.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def selftest():
    """Import every module the app touches. Exit 0 if the build is complete."""
    import tempfile
    import traceback
    try:
        import numpy                                   # noqa: F401
        import scipy.fft                               # noqa: F401
        import scipy.signal                            # noqa: F401
        import tkinter                                 # noqa: F401
        from soundsync import (clips, compat, csvout, fcpxml,   # noqa: F401
                               gui, matcher, options, pipeline, proc, report)
        ok, lines = proc.tools_report()
        report_path = os.path.join(tempfile.gettempdir(), "soundsync-selftest.txt")
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write("imports OK\n" + "\n".join(lines) + "\n")
        return 0 if ok else 2
    except Exception:                                   # noqa: BLE001
        path = os.path.join(tempfile.gettempdir(), "soundsync-selftest.txt")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    from soundsync.gui import main
    sys.exit(main())
