@echo off
REM ---------------------------------------------------------------------------
REM sync.bat -- drag a shoot folder onto this file, or run:
REM     sync.bat "D:\Shoot\dump"
REM
REM The folder needs one subfolder of video and one of audio. Any of these
REM names work: Video / Videos / Footage / Clips / Camera
REM        and:  Audio / Sound / Sounds / Recorder / Rec
REM
REM Runs syncmatch.py, then makefcpxml.py, and leaves merged.xml ready to
REM import into Premiere. The XML references your original camera files, so
REM nothing is copied or re-encoded.
REM
REM This is the quick route with default settings. For separate video/audio
REM folders, a chosen destination, a different export format, or baked media
REM files, use the app: run SoundSync.bat (or SoundSync.exe if you have the
REM packaged build).
REM
REM This window always stays open, including on errors.
REM ---------------------------------------------------------------------------
setlocal

if "%~1"=="" (
  echo Usage: drag a shoot folder onto sync.bat
  echo    or: sync.bat "D:\Shoot\dump"
  goto :fail
)

set "ROOT=%~1"
set "HERE=%~dp0"

if not exist "%ROOT%\" (
  echo ERROR: that isn't a folder:
  echo   "%ROOT%"
  goto :fail
)

echo Shoot folder: "%ROOT%"
echo.

REM ---- locate the two subfolders, whatever they're called -------------------
set "VDIR="
for %%N in (Video Videos Footage Clips Camera) do (
  if exist "%ROOT%\%%N\" if not defined VDIR set "VDIR=%ROOT%\%%N"
)
set "ADIR="
for %%N in (Audio Sound Sounds Recorder Rec) do (
  if exist "%ROOT%\%%N\" if not defined ADIR set "ADIR=%ROOT%\%%N"
)

if not defined VDIR goto :nofolders
if not defined ADIR goto :nofolders

echo   video: "%VDIR%"
echo   audio: "%ADIR%"
echo.

REM ---- check the tools are actually installed -------------------------------
where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python isn't on your PATH.
  echo Install it from python.org and tick "Add python.exe to PATH".
  echo.
  echo Or use the packaged build ^(SoundSync.exe^), which needs no Python at all.
  goto :fail
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ERROR: ffmpeg isn't on your PATH. Install it with:
  echo     winget install -e --id Gyan.FFmpeg
  echo then close this window and open a fresh one.
  goto :fail
)

where ffprobe >nul 2>nul
if errorlevel 1 (
  echo ERROR: ffprobe isn't on your PATH ^(it ships alongside ffmpeg^).
  goto :fail
)

python -c "import numpy, scipy" >nul 2>nul
if errorlevel 1 (
  echo ERROR: numpy and/or scipy are missing. Install them with:
  echo     python -m pip install numpy scipy
  goto :fail
)

REM ---- step 1: match --------------------------------------------------------
echo === Step 1 of 2: analysing and matching ===
python "%HERE%syncmatch.py" --video "%VDIR%" --audio "%ADIR%" --out "%ROOT%\matches.json"
if errorlevel 1 (
  echo.
  echo Matching failed - see the message above.
  echo To see what's actually inside the files, run:
  echo   python "%HERE%syncmatch.py" --video "%VDIR%" --audio "%ADIR%" --diagnose
  goto :fail
)

REM ---- step 2: build the sequences ------------------------------------------
echo.
echo === Step 2 of 2: building synced sequences ===
python "%HERE%makefcpxml.py" "%ROOT%\matches.json" --out "%ROOT%\merged.xml" --jam-timecode
if errorlevel 1 (
  echo.
  echo Building the XML failed - see the message above.
  goto :fail
)

echo.
echo Done. In Premiere: File ^> Import, and choose:
echo   "%ROOT%\merged.xml"
echo.
echo That builds VIDEO / AUDIO / SEQUENCES bins pointing at your original
echo camera files. Nothing was copied or re-encoded.
echo.
echo Sequences open as timelines, not in the Source Monitor. For real master
echo clips, select the VIDEO and AUDIO bins and use
echo   Clip ^> Create Multi-Camera Source Sequence, synced by Timecode.
echo.
pause
exit /b 0

REM ---------------------------------------------------------------------------
:nofolders
echo ERROR: couldn't find both a video and an audio subfolder in there.
echo.
echo What's actually inside "%ROOT%":
for /d %%D in ("%ROOT%\*") do echo   [folder] %%~nxD
echo.
echo Rename them to Video and Audio ^(or Sound^), point the scripts straight
echo at them, or use the app, which lets you pick each folder separately:
echo   python "%HERE%syncmatch.py" --video "FOLDER1" --audio "FOLDER2" --out matches.json
goto :fail

:fail
echo.
pause
exit /b 1
