"""
Finding and running ffmpeg/ffprobe.

Two things this exists for:

1. **Self-contained builds.** A packaged copy of this app ships ffmpeg.exe and
   ffprobe.exe beside itself, so it works on a machine that has never had
   ffmpeg installed. Bundled copies win over whatever is on PATH, so the app
   isn't at the mercy of some other ffmpeg the user installed years ago.

2. **No flashing console windows.** A GUI run spawns hundreds of ffmpeg
   processes. On Windows each one would pop a console window for a fraction of
   a second unless CREATE_NO_WINDOW is passed.

Every subprocess is also registered with its Canceller, so pressing Cancel
actually stops work in progress instead of waiting out a 40-minute bake.
"""

import os
import shutil
import subprocess
import sys
import threading

# Stops a console window flashing for every ffmpeg call in a windowed build.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0

EXE = ".exe" if os.name == "nt" else ""


class ToolsMissing(RuntimeError):
    """ffmpeg or ffprobe could not be found anywhere."""


class Cancelled(Exception):
    """Raised inside worker code when the user asks to stop."""


# ------------------------------------------------------------------ locating

def app_dir():
    """The folder the application lives in -- next to the .exe when frozen."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _candidate_dirs():
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)      # onefile temp extraction
    if meipass:
        roots += [meipass, os.path.join(meipass, "ffmpeg")]
    base = app_dir()
    roots += [base,
              os.path.join(base, "ffmpeg"),
              os.path.join(base, "bin"),
              os.path.join(base, "ffmpeg", "bin"),
              os.path.join(base, "_internal", "ffmpeg")]
    # macOS .app bundle: the executable sits in Contents/MacOS, but anything
    # added with --add-binary lands in Contents/Frameworks -- one up and over.
    if sys.platform == "darwin":
        roots += [os.path.normpath(os.path.join(base, "..", "Frameworks", "ffmpeg")),
                  os.path.normpath(os.path.join(base, "..", "Frameworks")),
                  os.path.normpath(os.path.join(base, "..", "Resources", "ffmpeg"))]
    return roots


_cache = {}


def find_tool(name):
    """Bundled copy first, then PATH. Returns None if there isn't one."""
    if name in _cache:
        return _cache[name]
    for root in _candidate_dirs():
        path = os.path.join(root, name + EXE)
        if os.path.isfile(path):
            _cache[name] = path
            return path
    found = shutil.which(name)
    _cache[name] = found
    return found


def ffmpeg():
    path = find_tool("ffmpeg")
    if not path:
        raise ToolsMissing(_missing_message())
    return path


def ffprobe():
    path = find_tool("ffprobe")
    if not path:
        raise ToolsMissing(_missing_message())
    return path


def _missing_message():
    if os.name == "nt":
        how = "winget install -e --id Gyan.FFmpeg"
    elif sys.platform == "darwin":
        how = "brew install ffmpeg"
    else:
        how = "your package manager, e.g. apt install ffmpeg"
    return ("ffmpeg and ffprobe could not be found.\n\n"
            "A packaged copy of this app carries its own; if you are running "
            "from source, either install ffmpeg and put it on your PATH "
            f"({how}) or drop ffmpeg{EXE} and "
            f"ffprobe{EXE} into:\n  " + os.path.join(app_dir(), "ffmpeg"))


def tools_report():
    """(ok, lines) describing what was found -- shown in the GUI's About box."""
    lines, ok = [], True
    for name in ("ffmpeg", "ffprobe"):
        path = find_tool(name)
        if path:
            # Not a startswith on app_dir(): inside a macOS .app the bundled
            # copy lives in a sibling of the executable's folder, not under it.
            owned = {os.path.normcase(os.path.abspath(d)) for d in _candidate_dirs()}
            here = os.path.normcase(os.path.abspath(os.path.dirname(path)))
            where = "bundled" if here in owned else "on PATH"
            lines.append(f"{name}: {path}  ({where})")
        else:
            ok = False
            lines.append(f"{name}: NOT FOUND")
    return ok, lines


# --------------------------------------------------------------- cancelling

class Canceller:
    """
    A stop button that reaches into running subprocesses.

    Worker code calls check() at safe points; anything long-running goes
    through run(), which registers the process so cancel() can terminate it.
    """

    def __init__(self):
        self._event = threading.Event()
        self._procs = set()
        self._lock = threading.Lock()

    def cancel(self):
        self._event.set()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except Exception:                     # noqa: BLE001 - already gone
                pass

    def reset(self):
        self._event.clear()

    @property
    def cancelled(self):
        return self._event.is_set()

    def check(self):
        if self._event.is_set():
            raise Cancelled()

    def _register(self, proc):
        with self._lock:
            self._procs.add(proc)
        if self._event.is_set():                  # cancelled while we were starting
            try:
                proc.kill()
            except Exception:                     # noqa: BLE001
                pass

    def _forget(self, proc):
        with self._lock:
            self._procs.discard(proc)


NULL_CANCELLER = Canceller()


class Result:
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run(cmd, cancel=None, text=True, timeout=None):
    """
    Run a command to completion. Same shape as subprocess.run(capture_output=True),
    but window-free and killable.
    """
    cancel = cancel or NULL_CANCELLER
    cancel.check()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
        text=text, encoding="utf-8" if text else None,
        errors="replace" if text else None)
    cancel._register(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    finally:
        cancel._forget(proc)
    cancel.check()
    return Result(proc.returncode, out, err)


def quote(cmd):
    """A command line you can paste into a terminal -- used by dry-run."""
    return " ".join(f'"{c}"' if (" " in c or not c) else c for c in cmd)
