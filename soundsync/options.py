"""
Every knob the tool has, as plain data.

The CLI fills these from argparse, the GUI fills them from widgets, and the
worker code only ever sees the dataclass. Saving a preset is json.dump of
to_dict(); loading one is from_dict().
"""

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields, replace  # noqa: F401


# --------------------------------------------------------------- stage 1

@dataclass
class MatchOptions:
    min_z: float = 12.0
    """Confidence threshold. True syncs land at 20-40; unrelated pairs rarely
    clear 9. Lower to catch marginal clips, raise if you got a wrong pairing."""

    min_prominence: float = 1.2
    """How far the winner must stand above the runner-up alignment."""

    one_to_one: bool = False
    """Each audio file may back only one video. Off by default so a single
    continuous recorder roll can cover several takes."""

    jobs: int = 0
    """Parallel decode workers. 0 = one per core, capped at 8."""

    recursive: bool = False
    """Search sub-folders of the chosen source folders too."""


# --------------------------------------------------------------- stage 2a

@dataclass
class XmlOptions:
    out_path: str = ""
    bin: str = "Merged"
    video_bin: str = "VIDEO"
    audio_bin: str = "AUDIO"
    sequence_bin: str = "SEQUENCES"

    structure: str = "subbins"
    """subbins -- VIDEO / AUDIO / SEQUENCES sub-bins (the default)
       pairs   -- one bin per matched pair, for fast manual Merge Clips
       flat    -- sequences straight in the top bin, no master clips"""

    include_unmatched: bool = False
    jam_timecode: bool = True
    keep_camera_audio: bool = False

    @property
    def pair_bins(self):
        return self.structure == "pairs"

    @property
    def flat(self):
        return self.structure == "flat"


# --------------------------------------------------------------- stage 2b

@dataclass
class ClipOptions:
    out_dir: str = ""
    container: str = "auto"
    """auto | mov | mxf | mkv | mp4 -- 'auto' keeps the source wrapper except
    where that wrapper cannot hold the stream correctly."""

    video_codec: str = "copy"
    """copy | prores_hq | prores_422 | prores_lt | prores_4444 | dnxhr_hqx |
       dnxhr_sq | h264 | h265. See clips.VIDEO_CODECS."""

    audio_codec: str = "pcm_s24le"
    suffix: str = "_synced"
    keep_camera_audio: bool = False
    pad: bool = True
    verify: bool = True
    """Decode the first seconds of every finished clip and confirm the frames
    come back in order. This is the check that catches the MXF stutter."""

    fallback: bool = True
    """If a clip fails or verifies bad, retry as MOV and then as an intra
    re-encode rather than just reporting failure."""

    jam_timecode: bool = False
    jobs: int = 0
    dry_run: bool = False


# --------------------------------------------------------------- the lot

@dataclass
class JobOptions:
    """Everything the GUI holds, including which stages to run."""
    video_dir: str = ""
    audio_dir: str = ""
    dest_dir: str = ""

    make_xml: bool = True
    make_clips: bool = False
    make_csv: bool = False
    """Write the pairing list as a spreadsheet. With all three off the job is
    match-only: it analyses, lists what goes with what, and exports nothing."""

    matches_name: str = "matches.json"
    xml_name: str = "merged.xml"
    csv_name: str = "matches.csv"
    clips_subfolder: str = "Synced"

    match: MatchOptions = field(default_factory=MatchOptions)
    xml: XmlOptions = field(default_factory=XmlOptions)
    clips: ClipOptions = field(default_factory=ClipOptions)

    # ---- derived paths ----
    def matches_path(self):
        return os.path.join(self.dest_dir, self.matches_name)

    def xml_path(self):
        return os.path.join(self.dest_dir, self.xml_name)

    def csv_path(self):
        return os.path.join(self.dest_dir, self.csv_name)

    def clips_dir(self):
        sub = self.clips_subfolder.strip()
        return os.path.join(self.dest_dir, sub) if sub else self.dest_dir

    # ---- persistence ----
    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        sub = {"match": MatchOptions, "xml": XmlOptions, "clips": ClipOptions}
        kwargs = {}
        for f in fields(cls):
            if f.name in sub:
                kwargs[f.name] = _build(sub[f.name], data.get(f.name))
            elif f.name in data:
                kwargs[f.name] = data[f.name]
        return cls(**kwargs)

    def save(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def _build(klass, data):
    """Tolerant constructor -- unknown keys from an older preset are ignored,
    missing ones keep their defaults."""
    if not isinstance(data, dict):
        return klass()
    known = {f.name for f in fields(klass)}
    return klass(**{k: v for k, v in data.items() if k in known})


def settings_dir():
    """Where presets and the auto-saved settings live -- per-platform, because
    a bare folder in the home directory is only normal on Windows."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = (os.environ.get("XDG_CONFIG_HOME")
                or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, "SoundSync")


def last_used_path():
    return os.path.join(settings_dir(), "settings.json")
