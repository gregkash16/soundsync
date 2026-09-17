#!/usr/bin/env python3
"""
Build the standalone app.

Produces dist\\SoundSync\\ -- a folder holding SoundSync.exe, an embedded
Python interpreter, numpy/scipy, and ffmpeg. The machine you copy it to needs
none of those installed. Copy the whole folder; the exe will not run on its
own.

    python build.py                     build it
    python build.py --shortcut          build it and put a shortcut on the Desktop
    python build.py --installer         build it, then wrap it in an installer:
                                        a setup wizard on Windows
                                        (dist\\SoundSync-Setup-<version>.exe),
                                        a drag-to-Applications disk image on macOS
                                        (dist/SoundSync-<version>.dmg)
    python build.py --ffmpeg-dir "C:\\ffmpeg\\bin"
    python build.py --clean             throw away build caches first

Notes
-----
PyInstaller builds for the platform it runs on, so this has to be run once per
platform. On Windows it produces dist\\SoundSync\\; on macOS it produces both
dist/SoundSync/ and dist/SoundSync.app, and the .app is the one you ship.

Building on a Mac, in order:
  * Use python.org Python 3.12+, not the system one -- system Python's Tk 8.5
    makes the GUI look and behave badly. python3 -m pip install numpy scipy.
  * Get a STATIC ffmpeg/ffprobe (evermeet.cx publishes them) and point at it
    with --ffmpeg-dir. Homebrew's ffmpeg links against dylibs in /opt/homebrew
    that will not exist on anyone else's machine; bundling it produces an app
    that runs perfectly for you and dies instantly for them. This script warns
    if it spots that.
  * An arm64 build will not run on an Intel Mac. Match the target, or build
    universal2 (which needs universal2 numpy/scipy wheels and ffmpeg too).
  * The result is unsigned, so it arrives quarantined: the recipient gets
    "Apple could not verify this app is free of malware" and has to allow it
    under System Settings > Privacy & Security > Open Anyway, or run
    xattr -dr com.apple.quarantine /path/to/SoundSync.app
    Signing and notarizing properly needs a paid Apple Developer account.

One-folder rather than one-file on purpose: a single 300 MB exe re-extracts
itself to a temp directory on every launch, which costs 10-20 seconds each
time and trips antivirus far more often.

--installer on Windows hands that folder to Inno Setup (installer.iss) and
produces a normal setup wizard: Start Menu entry, optional desktop shortcut,
Add/Remove Programs listing, uninstaller. Per-user by default, so no admin
prompt. Needs Inno Setup 6 on this machine (the build machine only --
recipients need nothing):  winget install -e --id JRSoftware.InnoSetup

--installer on macOS wraps the .app in a compressed disk image with an
Applications shortcut next to it -- the standard Mac "installer": open the
.dmg, drag the app across, done. Built with hdiutil, which every Mac has.
A .dmg also survives transit intact, unlike a bare .app whose executable
bits get stripped by most upload forms.
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "SoundSync"
ENTRY = os.path.join(HERE, "SoundSync.pyw")

# scipy and numpy ship pieces PyInstaller's static analysis misses. Names move
# between scipy versions, so anything not importable here is dropped rather
# than passed on -- PyInstaller only warns about a missing hidden import, but
# the warning buries the real ones.
CANDIDATE_HIDDEN = [
    "scipy._lib.messagestream",
    "scipy.signal._spectral_py",
    "scipy.fft._pocketfft",
    "scipy.special._cdflib",
    # scipy's array-API shim lives under _external in 1.15+, _lib before that.
    "scipy._external.array_api_compat.numpy",
    "scipy._external.array_api_compat.numpy.fft",
    "scipy._external.array_api_compat.numpy.linalg",
    "scipy._lib.array_api_compat.numpy.fft",
    "numpy.testing",
    # scipy.stats._sobol needs this at runtime; PyInstaller's scan misses it
    # on some Pythons.
    "importlib.resources",
]

# Only third-party weight. Do NOT exclude stdlib here: scipy's array-API shim
# clones the whole numpy module on import, which pulls in numpy.testing, which
# imports unittest. Excluding unittest builds fine and then dies on launch with
# "No module named 'unittest'".
EXCLUDE = ["matplotlib", "PIL", "pandas", "IPython", "notebook", "pytest"]


def die(msg):
    print("\nERROR: " + msg)
    sys.exit(1)


def have(module):
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def ensure_pyinstaller():
    if have("PyInstaller"):
        return
    print("PyInstaller is not installed. It is only needed to build -- the app "
          "itself does not use it.")
    answer = input("Install it now with pip? [y/N] ").strip().lower()
    if answer != "y":
        die("cannot build without PyInstaller. Install it with:\n"
            "    python -m pip install pyinstaller")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
    if not have("PyInstaller"):
        die("PyInstaller still isn't importable after installing.")


def resolve_hidden():
    """Keep only the hidden imports that exist in the installed scipy/numpy."""
    found = [name for name in CANDIDATE_HIDDEN if have(name)]
    missing = [name for name in CANDIDATE_HIDDEN if name not in found]
    if missing:
        print("Not present in this scipy/numpy, so not bundled: "
              + ", ".join(missing))
    return found


def find_ffmpeg(explicit):
    """Locate ffmpeg.exe and ffprobe.exe to bundle."""
    exe = ".exe" if os.name == "nt" else ""
    if explicit:
        pair = [os.path.join(explicit, f"ffmpeg{exe}"),
                os.path.join(explicit, f"ffprobe{exe}")]
        for p in pair:
            if not os.path.isfile(p):
                die(f"{p} does not exist")
        return pair

    pair = [shutil.which("ffmpeg"), shutil.which("ffprobe")]
    if not all(pair):
        if os.name == "nt":
            how = ("Install them (winget install -e --id Gyan.FFmpeg) or point at them:\n"
                   '    python build.py --ffmpeg-dir "C:\\ffmpeg\\bin"')
        else:
            how = ("Install them, or point at a static build:\n"
                   "    python build.py --ffmpeg-dir ~/ffmpeg-static")
        die("ffmpeg/ffprobe are not on your PATH, so there is nothing to bundle.\n" + how)
    return pair


def warn_if_not_static(paths):
    """
    A dynamically-linked ffmpeg bundles fine and then fails on the target.

    Homebrew's ffmpeg is linked against a stack of dylibs under /opt/homebrew
    (or /usr/local on Intel). Those live outside the .app, so the copy we embed
    works on this machine and dies on anyone else's with a dyld error naming a
    library they have never heard of -- the worst failure to debug remotely.
    Warn, don't block: the build is still correct for local use.
    """
    if sys.platform != "darwin":
        return
    for path in paths:
        try:
            out = subprocess.run(["otool", "-L", path], capture_output=True,
                                 text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return                      # no otool, nothing to say
        bad = sorted({line.split()[0] for line in out.splitlines()[1:]
                      if line.strip().startswith(("/opt/homebrew", "/usr/local"))})
        if bad:
            print(f"\nWARNING: {path} is not statically linked. It depends on:")
            for lib in bad[:6]:
                print("    " + lib)
            if len(bad) > 6:
                print(f"    ... and {len(bad) - 6} more")
            print("These are not inside the app, so it will run here and fail on\n"
                  "any machine without them. Use a static build (evermeet.cx) and\n"
                  "pass --ffmpeg-dir, unless this build is only for you.\n")


def check_not_running(out_dir):
    """
    Stop early if the last build is still open.

    PyInstaller deletes dist\\SoundSync\\ before it writes the new one, and
    Windows refuses to unlink a .pyd or .dll that a running process has
    mapped. Left to itself that surfaces as a PermissionError traceback out of
    shutil.rmtree, several minutes in, naming some random bundled module --
    which reads like a broken build rather than "close the app".
    """
    exe = os.path.join(out_dir, NAME + (".exe" if os.name == "nt" else ""))
    if not os.path.isfile(exe):
        return
    try:
        with open(exe, "ab"):          # a running exe is locked against writes
            return
    except OSError:
        pass

    hint = ""
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {NAME}.exe", "/NH"],
                                 capture_output=True, text=True, timeout=15).stdout
            pids = [line.split()[1] for line in out.splitlines()
                    if line.strip().lower().startswith(NAME.lower())]
            if pids:
                hint = f" It is running as PID {', '.join(pids)}."
        except (OSError, subprocess.SubprocessError, IndexError):
            pass

    die(f"the app from the last build is still open.{hint}\n"
        "Close the SoundSync window -- closing it properly is also what saves "
        "your settings -- then run this again.\n"
        f"Nothing is wrong with the code: the build cannot delete {out_dir} "
        f"while a file inside it is loaded.")


def main():
    ap = argparse.ArgumentParser(description="Build the standalone SoundSync app.")
    ap.add_argument("--ffmpeg-dir", default="",
                    help="folder holding ffmpeg.exe and ffprobe.exe "
                         "(default: wherever they are on PATH)")
    ap.add_argument("--clean", action="store_true",
                    help="clear PyInstaller's caches first")
    ap.add_argument("--shortcut", action="store_true",
                    help="put a shortcut to the built app on the Desktop")
    ap.add_argument("--installer", action="store_true",
                    help="after building, make an installer: an Inno Setup "
                         "wizard on Windows (dist\\SoundSync-Setup-<version>.exe), "
                         "a disk image on macOS (dist/SoundSync-<version>.dmg)")
    ap.add_argument("--no-bundle-ffmpeg", action="store_true",
                    help="build without ffmpeg inside -- the app will then need it "
                         "on PATH, which is not what most people want")
    args = ap.parse_args()

    for mod, pkg in (("numpy", "numpy"), ("scipy", "scipy")):
        if not have(mod):
            die(f"{mod} is missing. Install the runtime dependencies first:\n"
                f"    python -m pip install numpy scipy")
    ensure_pyinstaller()

    if not os.path.isfile(ENTRY):
        die(f"entry point missing: {ENTRY}")

    iscc = None
    if args.installer:
        if os.name == "nt":
            iscc = find_iscc()      # check now, not after a five-minute build
        elif sys.platform != "darwin":
            die("--installer only knows how to package for Windows and macOS.")

    out_dir = os.path.join(HERE, "dist", NAME)
    check_not_running(out_dir)

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir",
           "--windowed", "--name", NAME, "--distpath", os.path.join(HERE, "dist"),
           "--workpath", os.path.join(HERE, "build"),
           "--specpath", os.path.join(HERE, "build")]
    if args.clean:
        cmd.append("--clean")

    icon = os.path.join(HERE, "icon.icns" if sys.platform == "darwin" else "icon.ico")
    if os.path.isfile(icon):
        cmd += ["--icon", icon]

    if sys.platform == "darwin":
        # Only matters if the .app is ever signed or notarized, but it has to
        # be baked in at build time, so set it now rather than rebuild later.
        cmd += ["--osx-bundle-identifier", "com.gregkash.soundsync"]

    if not args.no_bundle_ffmpeg:
        tools = find_ffmpeg(args.ffmpeg_dir)
        warn_if_not_static(tools)
        for path in tools:
            cmd += ["--add-binary", f"{path}{os.pathsep}ffmpeg"]

    readme = os.path.join(HERE, "README.md")
    if os.path.isfile(readme):
        cmd += ["--add-data", f"{readme}{os.pathsep}."]

    for h in resolve_hidden():
        cmd += ["--hidden-import", h]
    for e in EXCLUDE:
        cmd += ["--exclude-module", e]

    cmd.append(ENTRY)

    print("Building. This takes a few minutes and prints a lot.\n")
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd) + "\n")
    result = subprocess.call(cmd)
    if result != 0:
        die(f"PyInstaller exited with code {result} -- see the output above.")

    exe = os.path.join(out_dir, NAME + (".exe" if os.name == "nt" else ""))
    if not os.path.isfile(exe):
        die(f"the build finished but {exe} isn't there.")

    if not selftest(exe):
        die("the app was built but fails to start. See the traceback above.\n"
            "If it names a missing module, add it to CANDIDATE_HIDDEN in this "
            "file and rebuild.")

    app = os.path.join(HERE, "dist", NAME + ".app")
    shipped = app if (sys.platform == "darwin" and os.path.isdir(app)) else out_dir
    size = sum(os.path.getsize(os.path.join(root, f))
               for root, _d, files in os.walk(shipped) for f in files)
    print("\n" + "-" * 70)
    print(f"Built: {shipped}")
    print(f"Folder size: {size / (1024 ** 3):.2f} GB"
          if size > 1024 ** 3 else f"Folder size: {size / (1024 ** 2):.0f} MB")
    if sys.platform == "darwin":
        print(f"\nShip {os.path.basename(shipped)}. It needs no Python and no ffmpeg, but it is\n"
              "unsigned, so whoever opens it will be told macOS 'could not verify' it.\n"
              "They allow it under System Settings > Privacy & Security > Open Anyway,\n"
              "or with:  xattr -dr com.apple.quarantine /Applications/SoundSync.app\n"
              "Zip it rather than sending the folder -- a bare .app loses its\n"
              "executable bits in transit and stops opening.")
    else:
        print("\nCopy the whole SoundSync folder to any 64-bit Windows machine. It "
              "needs\nno Python, no ffmpeg and no other install.")

    if args.shortcut:
        make_shortcut(exe)
    if iscc:
        setup = make_installer(iscc, out_dir)
        print(f"\nInstaller: {setup}")
        print("Send that one file. It installs per-user (no admin prompt) with a\n"
              "Start Menu entry, an optional desktop shortcut and an uninstaller.")
    elif args.installer and sys.platform == "darwin":
        dmg = make_dmg(app)
        print(f"\nInstaller: {dmg}")
        print("Send that one file. The recipient opens it, drags SoundSync to\n"
              "Applications, and (because the app is unsigned) allows it once under\n"
              "System Settings > Privacy & Security > Open Anyway.")
    print("-" * 70)
    return 0


def selftest(exe):
    """
    Launch the built exe with --selftest: it imports everything and exits.

    Worth the ten seconds. A packaged app can build cleanly and still die at
    launch over a module PyInstaller left out, and a windowed exe has nowhere
    to print the traceback -- so the app writes it to a file and we read it
    back here rather than letting the failure surface as a dialog days later.
    """
    import tempfile
    report = os.path.join(tempfile.gettempdir(), "soundsync-selftest.txt")
    if os.path.exists(report):
        try:
            os.remove(report)
        except OSError:
            pass

    print("\nChecking the built app starts...")
    try:
        code = subprocess.call([exe, "--selftest"], timeout=120)
    except subprocess.TimeoutExpired:
        print("  it didn't exit within two minutes.")
        return False
    except OSError as e:
        print(f"  couldn't run it: {e}")
        return False

    detail = ""
    if os.path.isfile(report):
        with open(report, encoding="utf-8", errors="replace") as fh:
            detail = fh.read().strip()

    if code == 0:
        print("  imports fine, and it found its ffmpeg:")
        for line in detail.splitlines()[1:]:
            print("    " + line)
        return True
    if code == 2:
        print("  imports fine, but it could not find ffmpeg:")
        print("    " + detail.replace("\n", "\n    "))
        print("  Rebuild without --no-bundle-ffmpeg, or point at it with "
              "--ffmpeg-dir.")
        return True                      # the app runs; ffmpeg is a separate fix
    print(f"  it exited with code {code}:\n")
    print(detail or "  (no detail was written)")
    return False


def find_iscc():
    """Locate Inno Setup's command-line compiler, or die with the install hint."""
    found = shutil.which("ISCC")
    if found:
        return found
    roots = [os.environ.get("LOCALAPPDATA", ""),
             os.environ.get("ProgramFiles(x86)", ""),
             os.environ.get("ProgramFiles", "")]
    for root in roots:
        if not root:
            continue
        for sub in ("Programs\\Inno Setup 6", "Inno Setup 6"):
            cand = os.path.join(root, sub, "ISCC.exe")
            if os.path.isfile(cand):
                return cand
    die("--installer needs Inno Setup 6, which isn't installed. It is only "
        "needed on this\nmachine to build the installer -- whoever runs the "
        "installer needs nothing.\n"
        "    winget install -e --id JRSoftware.InnoSetup")


def app_version():
    sys.path.insert(0, HERE)
    import soundsync
    return soundsync.__version__


def make_installer(iscc, out_dir):
    """Compile installer.iss around the built folder. Returns the setup exe path."""
    script = os.path.join(HERE, "installer.iss")
    if not os.path.isfile(script):
        die(f"installer.iss is missing from {HERE}")
    version = app_version()
    dist = os.path.join(HERE, "dist")
    setup = os.path.join(dist, f"SoundSync-Setup-{version}.exe")
    cmd = [iscc, "/Qp", f"/DAppVersion={version}", f"/DSourceDir={out_dir}",
           f"/DOutputDir={dist}", script]
    print("\nCompiling the installer (compressing 300 MB -- a minute or two)...")
    result = subprocess.call(cmd, cwd=HERE)
    if result != 0:
        die(f"Inno Setup exited with code {result} -- see the output above.")
    if not os.path.isfile(setup):
        die(f"Inno Setup finished but {setup} isn't there.")
    size = os.path.getsize(setup) / (1024 ** 2)
    print(f"  {size:.0f} MB")
    return setup


def make_dmg(app):
    """
    Wrap dist/SoundSync.app in a compressed disk image. Returns the dmg path.

    Staged through a temp folder holding the app plus an /Applications
    symlink, so the opened image shows the familiar drag-here layout. hdiutil
    ships with macOS -- nothing to install.
    """
    if not os.path.isdir(app):
        die(f"{app} isn't there -- the .app is only produced by --windowed "
            "builds on macOS.")
    version = app_version()
    dmg = os.path.join(HERE, "dist", f"{NAME}-{version}.dmg")
    if os.path.exists(dmg):
        os.remove(dmg)

    import tempfile
    with tempfile.TemporaryDirectory(prefix="soundsync-dmg-") as stage:
        # copytree(symlinks=True) rather than a naive copy: the .app is full
        # of framework symlinks, and following them both bloats the image and
        # can break code signatures.
        shutil.copytree(app, os.path.join(stage, os.path.basename(app)),
                        symlinks=True)
        os.symlink("/Applications", os.path.join(stage, "Applications"))
        print("\nCompressing the disk image (a minute or two)...")
        result = subprocess.call(
            ["hdiutil", "create", "-volname", f"{NAME} {version}",
             "-srcfolder", stage, "-format", "UDZO", "-ov", "-quiet", dmg])
    if result != 0:
        die(f"hdiutil exited with code {result} -- see the output above.")
    if not os.path.isfile(dmg):
        die(f"hdiutil finished but {dmg} isn't there.")
    print(f"  {os.path.getsize(dmg) / (1024 ** 2):.0f} MB")
    return dmg


def make_shortcut(exe):
    if os.name != "nt":
        print("(--shortcut only does anything on Windows)")
        return
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    link = os.path.join(desktop, "Sound Syncing.lnk")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
          f"$s.TargetPath='{exe}';"
          f"$s.WorkingDirectory='{os.path.dirname(exe)}';"
          f"$s.Description='Sync dual-system audio to camera clips';"
          f"$s.Save()")
    try:
        subprocess.check_call(["powershell", "-NoProfile", "-NonInteractive",
                               "-Command", ps])
        print(f"Shortcut: {link}")
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"Could not create the shortcut ({e}). Make one by hand from {exe}.")


if __name__ == "__main__":
    sys.exit(main())
