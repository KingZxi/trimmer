"""Persistent app settings, stored as JSON in the app data folder."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .encode import EncodeSettings
from .util import app_data_dir, read_safely, write_safely

SETTINGS_FILE = "settings.json"
OLD_MERGE_SETTINGS = Path.home() / ".obs_merge_settings.json"


AUTO3_PRESET = "Auto 3 (H.265, quality 93)"


def default_presets():
    return {
        "Archive (H.265, auto CRF)": EncodeSettings(codec="h265", quality="auto2", preset="slow").to_dict(),
        AUTO3_PRESET: EncodeSettings(codec="h265", quality="auto3", auto3_target=93.0, preset="medium",
                                     revert_if_larger=True).to_dict(),
        "Balanced (H.265 CRF 23)": EncodeSettings(codec="h265", quality=23, preset="medium").to_dict(),
        "Compatible (H.264 CRF 20)": EncodeSettings(codec="h264", quality=20, preset="medium").to_dict(),
        "Discord 10 MB (720p)": EncodeSettings(mode="target_size", codec="h264", target_size_mb=9.5,
                                               preset="medium", audio="96", max_height=720).to_dict(),
        "Share 25 MB (1080p)": EncodeSettings(mode="target_size", codec="h264", target_size_mb=24,
                                              preset="medium", audio="128", max_height=1080).to_dict(),
        "Lossless (H.264)": EncodeSettings(mode="lossless", codec="h264", preset="medium").to_dict(),
    }


@dataclass
class AppSettings:
    # Output
    output_dir: str = ""
    conflict_policy: str = "ask"          # ask | rename | overwrite | skip
    delete_originals: bool = False
    permanent_delete: bool = False        # False = Recycle Bin
    name_suffix: str = ""
    add_operation_tags: bool = True       # "(trimmed)", "(no audio)" ...
    add_trim_range: bool = True           # "(trimmed 1m05s-1m32s)" instead of "(trimmed)"
    prefill_names: bool = True            # rename boxes start with the original name
    add_quality_label: bool = True        # compression details in compressed file names
    container: str = "same"              # same | .mp4 | .mkv
    exact_cuts: bool = False              # frame-exact trims (smart cut or re-encode)
    smart_cut: bool = True                # exact trims re-encode only around each cut
    show_peek: bool = True                # waveform + thumbnails on the trim timeline
    rip_format: str = "copy"
    count_kept_savings: bool = False      # also count space saved when originals are kept
    warn_duplicate_jobs: bool = True      # ask before queueing a job that's already queued or done
    warn_already_processed: bool = True   # ask before redoing work a file already went through
    # Compression
    encode: dict = field(default_factory=lambda: EncodeSettings().to_dict())
    presets: dict = field(default_factory=default_presets)
    # Replay merging (names match the old ~/.obs_merge_settings.json keys)
    merge_mode: str = "merge"            # merge | trim
    merge_method: str = "auto"           # auto | lossless | exact
    max_gap: float = 1.5
    safety_margin_enabled: bool = True
    safety_margin: float = 0.25
    extra_trim_padding: float = 3.75
    audio_align: bool = True
    align_margin: float = 3.0
    seamless: bool = False
    lossless_max_repeat: float = 2.0
    timestamp_is_end: bool = False
    generate_clip_log: bool = False
    show_replay_merge: bool = True       # False hides the Replay merge tab (and stops watching)
    watch_folder: str = ""              # OBS replay folder to watch for new clips
    watch_enabled: bool = False
    # Queue
    game_pause: bool = False              # pause while a game is full screen, resume after
    game_pause_ignore: str = ""           # program names that never count ("" = the built-in list)
    pause_hotkey: str = "Ctrl+Alt+P"      # global key to pause / resume the queue ("" = none)
    pause_sounds: bool = True             # chime when the key or a game pauses / resumes the queue
    # Tools and UI
    ffmpeg_dir: str = ""
    theme: str = "dark"
    on_close: str = "ask"                 # ask | save | discard what is open when closing
    auto_delete_logs: bool = True         # delete old run logs and crash reports when Trimmer starts
    keep_logs_days: int = 30              # ... once they are older than this
    # Updates (the only network use, and only when the user says yes; see trimmer/core/updates.py)
    update_checks: str = "ask"            # ask (not asked yet) | on | off
    skipped_version: str = ""             # "Skip this version": no popup for this release
    last_update_check: str = ""           # when it last checked, as shown in Settings
    warn_queue_sort: bool = True          # say that sorting the queue changes the order jobs run in
    ui: dict = field(default_factory=dict)

    @classmethod
    def path(cls) -> Path:
        return app_data_dir() / SETTINGS_FILE

    @classmethod
    def load(cls) -> "AppSettings":
        s = cls()
        p = cls.path()
        data = None
        for text in (lambda: read_safely(p), lambda: p.with_name(p.name + ".bak").read_text(encoding="utf-8")):
            try:
                data = json.loads(text())       # a file damaged by a crash falls back to the previous copy
                break
            except (OSError, json.JSONDecodeError):
                data = None
        if isinstance(data, dict):
            for k, v in data.items():
                if k in cls.__dataclass_fields__:
                    default = getattr(s, k)
                    # Ignore values of the wrong type from a hand-edited file.
                    if isinstance(default, bool) and not isinstance(v, bool):
                        continue
                    if isinstance(default, (int, float)) and not isinstance(default, bool):
                        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                            continue
                    if isinstance(default, str) and not isinstance(v, str):
                        continue
                    if isinstance(default, dict) and not isinstance(v, dict):
                        continue
                    setattr(s, k, v)
        else:
            s._import_old_merge_settings()
        # One-time move to the 3.75 s default for anyone still on the old 0 s default.
        if isinstance(data, dict) and not s.ui.get("padding_default_375"):
            if s.extra_trim_padding == 0:
                s.extra_trim_padding = 3.75
            s.ui["padding_default_375"] = True
        # One-time: the Auto 3 preset joins existing preset lists (deleting it afterwards sticks).
        if isinstance(data, dict) and not s.ui.get("preset_auto3_added"):
            if AUTO3_PRESET not in s.presets:
                s.presets = {**s.presets, AUTO3_PRESET: default_presets()[AUTO3_PRESET]}
            s.ui["preset_auto3_added"] = True
        s.encode = EncodeSettings.from_dict(s.encode).to_dict()
        if s.conflict_policy not in ("ask", "rename", "overwrite", "skip"):
            s.conflict_policy = "ask"
        if s.on_close not in ("ask", "save", "discard"):
            s.on_close = "ask"
        if s.update_checks not in ("ask", "on", "off"):
            s.update_checks = "ask"
        return s

    def _import_old_merge_settings(self):
        """First run: carry over the old obs_merge.py Settings menu values."""
        try:
            old = json.loads(OLD_MERGE_SETTINGS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(old.get("generate_clip_log"), bool):
            self.generate_clip_log = old["generate_clip_log"]
        if isinstance(old.get("safety_margin_enabled"), bool):
            self.safety_margin_enabled = old["safety_margin_enabled"]
        pad = old.get("extra_trim_padding")
        if isinstance(pad, (int, float)) and not isinstance(pad, bool) and pad >= 0:
            self.extra_trim_padding = float(pad)

    def save(self):
        p = self.path()
        try:
            write_safely(p, json.dumps(asdict(self), indent=2))
        except OSError:
            pass

    def encode_settings(self) -> EncodeSettings:
        return EncodeSettings.from_dict(self.encode)
