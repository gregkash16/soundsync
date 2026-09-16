@echo off
REM ---------------------------------------------------------------------------
REM Opens the Sound Syncing window, running from source.
REM
REM This needs Python, numpy, scipy and ffmpeg on the machine. If you want a
REM version that needs none of those, build the standalone app:
REM     python build.py --shortcut
REM ---------------------------------------------------------------------------
setlocal
set "HERE=%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 goto :nopython

start "" pythonw "%HERE%SoundSync.pyw"
exit /b 0

:nopython
where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python isn't on your PATH.
  echo Install it from python.org and tick "Add python.exe to PATH",
  echo then run:  python -m pip install numpy scipy
  echo.
  pause
  exit /b 1
)
python "%HERE%SoundSync.pyw"
exit /b %errorlevel%
