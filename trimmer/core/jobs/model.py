"""What a job is: the two kinds of job, the options they run with, their result, and the
callbacks to whoever runs them. Saved in the queue file, so field names stay stable."""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .. import merge as mg
from ..encode import EncodeSettings
from ..ffmpeg import CancelToken, Progress

VIDEO_ONLY = " - video only"      # name extra of the video-only rip
NOT_COMPRESSED = " (NOT COMPRESSED)"    # kept uncompressed because compressing made it bigger


# Used when a cut must be frame-exact but the user didn't ask to compress:
# high quality H.264, same as the old merger's defaults.
EXACT_SETTINGS = EncodeSettings(mode="lossy", codec="h264", quality=18, preset="medium", audio="192")


@dataclass
class JobOptions:
    """Snapshot of the settings a job runs with, taken when it is queued."""
    output_dir: str = ""
    conflict_policy: str = "ask"
    delete_originals: bool = False
    permanent_delete: bool = False
    name_suffix: str = ""
    add_operation_tags: bool = True
    add_trim_range: bool = True          # the kept times in the "(trimmed ...)" name tag
    container: str = "same"
    exact_cuts: bool = False
    smart_cut: bool = True               # exact cuts re-encode only around each cut
    rip_format: str = "copy"
    # merge
    merge_mode: str = "merge"
    merge_method: str = "auto"
    max_gap: float = 1.5
    safety_margin: float = 0.25
    extra_trim_padding: float = 0.0
    audio_align: bool = True
    align_margin: float = 3.0
    seamless: bool = False
    lossless_max_repeat: float = 2.0
    timestamp_is_end: bool = False
    generate_clip_log: bool = False
    use_packet_matching: bool = True
    add_quality_label: bool = True       # "(LOSSY), (CRF23, H265, medium)" in compressed names

    @classmethod
    def from_settings(cls, s) -> "JobOptions":
        o = cls()
        for k in cls.__dataclass_fields__:
            if hasattr(s, k):
                setattr(o, k, getattr(s, k))
        o.safety_margin = (s.safety_margin if s.safety_margin_enabled else 0.0)
        if s.seamless:
            o.safety_margin = 0.0
        o.use_packet_matching = bool(getattr(s, "ui", {}).get("packet_matching", True))
        return o

    def merge_options(self) -> mg.MergeOptions:
        return mg.MergeOptions(max_gap=self.max_gap, safety_margin=self.safety_margin,
                               extra_padding=self.extra_trim_padding, audio_align=self.audio_align,
                               align_margin=self.align_margin, never_lose=not self.seamless,
                               lossless_max_repeat=self.lossless_max_repeat,
                               timestamp_is_end=self.timestamp_is_end)


@dataclass
class ClipJob:
    source: str
    segments: List[Tuple[float, Optional[float]]] = field(default_factory=list)
    make_video: bool = True
    audio_tracks: Optional[List[int]] = None     # None = all, [] = none
    mix_tracks: Optional[List[int]] = None
    volumes: dict = field(default_factory=dict)  # str(track) -> gain
    keep_mixed_originals: bool = True
    compress: bool = False
    encode: dict = field(default_factory=dict)
    rip_audio_tracks: List[int] = field(default_factory=list)
    rip_video: bool = False
    options: JobOptions = field(default_factory=JobOptions)
    output_name: str = ""                        # user's name for the outputs ("" = from the source)
    kind: str = "clip"

    @property
    def title(self) -> str:
        return self.output_name.strip() or Path(self.source).name

    def sources(self) -> List[str]:
        return [self.source]


@dataclass
class MergeJob:
    clip_paths: List[str]
    compress: bool = False
    encode: dict = field(default_factory=dict)
    audio_tracks: Optional[List[int]] = None
    mix_tracks: Optional[List[int]] = None
    keep_mixed_originals: bool = True
    make_video: bool = True                      # the merged (or trimmed) video files
    rip_audio_tracks: List[int] = field(default_factory=list)
    rip_video: bool = False
    title_text: str = ""
    options: JobOptions = field(default_factory=JobOptions)
    output_name: str = ""                        # merged file (or trim-mode folder) name
    kind: str = "merge"

    @property
    def title(self) -> str:
        return (self.output_name.strip() or self.title_text
                or f"Replay session ({len(self.clip_paths)} clips)")

    def sources(self) -> List[str]:
        return list(self.clip_paths)


def job_to_dict(job) -> dict:
    return asdict(job)


def job_from_dict(d: dict):
    d = dict(d)
    opts = JobOptions(**{k: v for k, v in (d.pop("options", None) or {}).items()
                         if k in JobOptions.__dataclass_fields__})
    cls = MergeJob if d.get("kind") == "merge" else ClipJob
    fields_ = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
    if cls is ClipJob:
        fields_["segments"] = [tuple(s) for s in fields_.get("segments", [])]
    return cls(options=opts, **fields_)


@dataclass
class JobResult:
    status: str = "done"                  # done | reverted | failed | skipped | cancelled
    outputs: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    in_bytes: int = 0
    out_bytes: int = 0
    deleted: List[str] = field(default_factory=list)
    deleted_bytes: int = 0                # size of the originals that were really removed
    deferred: List[str] = field(default_factory=list)   # originals a later queued job still needs
    warnings: List[str] = field(default_factory=list)   # problems in the sources that didn't stop the job
    rip_from: str = ""                    # saved video still holding every source audio track untouched
    source_count: int = 0                 # clips this job read (a merge counts every replay)


class JobContext:
    """Callbacks from the engine to whoever runs it (GUI worker or CLI)."""

    def __init__(self, log: Callable[[str], None] = None,
                 progress: Callable[[str, Progress], None] = None,
                 resolve_conflict: Callable[[Path], str] = None,
                 cancel: CancelToken = None, still_needed: Callable[[Path], bool] = None):
        self.log = log or (lambda m: None)
        self.progress = progress or (lambda stage, p: None)
        self.resolve_conflict = resolve_conflict or (lambda p: "rename")
        self.cancel = cancel or CancelToken()
        # Truthy (True or a reason) for an original another queued job still reads,
        # or one whose other job didn't finish: its delete waits, or doesn't happen.
        self.still_needed = still_needed or (lambda p: False)
        # Asks whoever runs the job to let go of these files (close previews) before they're deleted.
        self.release: Callable[[List[Path]], None] = lambda paths: None
        self.skipped: List[str] = []      # outputs skipped by the name-clash policy
        self.warnings: List[str] = []     # source problems reported, not failed (frame gaps)
        self._sticky = None

    def stage(self, text: str):
        self.progress(text, Progress(None, 0, None, None, None))

    def reporter(self, text: str):
        return lambda p: self.progress(text, p)


class _Skip(Exception):
    pass
