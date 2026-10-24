"""Which part of each replay clip ends up in a merge (or is kept in
"keep separate" mode). Used by the viewer to highlight that part.

`quick` uses the clip timestamps only and is instant. `exact` matches the
clips' packets like a real merge does, and falls back to the timestamp and
audio estimate when they don't share packets."""

from dataclasses import replace
from typing import Dict, Optional, Tuple

from . import merge as mg
from . import packets as pk

Range = Optional[Tuple[float, float]]     # (start, end) in seconds within the clip; None = not used


def used_quick(session: "mg.SessionPlan", opts: "mg.MergeOptions") -> Dict[str, Range]:
    """Timestamps only: each kept clip is used from where the previous one
    ends. Redundant clips aren't used at all."""
    plan = mg.build_trim_plan(session.kept, replace(opts, audio_align=False))
    used = {str(c.path): None for c in session.clips}
    for e in plan:
        used[str(e.clip.path)] = (e.trim_start, e.clip.duration)
    return used


def used_exact(session: "mg.SessionPlan", opts: "mg.MergeOptions", use_packets: bool = True,
               cancel=None) -> Tuple[Dict[str, Range], str]:
    """(ranges, method). method is "exact" when every join was matched
    packet for packet, else "estimate" (timestamps + audio alignment)."""
    if use_packets and len(session.clips) > 1:
        fps = session.clips[0].info.fps or 60.0
        pj = pk.join_by_packets(session.clips, fps=fps, cancel=cancel)
        if pj.ok:
            used = {str(c.path): None for c in session.clips}
            for p in pj.pieces:
                pts = [t for t in p.video.pts if t == t]
                t0 = min(pts) if pts else 0.0
                end = p.clip.duration
                if p.stop is not None and p.stop < len(p.video.pts):
                    end = max(0.0, p.video.pts[p.stop] - t0)
                used[str(p.clip.path)] = (0.0, end)
            return used, "exact"
    plan = mg.build_trim_plan(session.kept, opts, cancel=cancel)
    used = {str(c.path): None for c in session.clips}
    for e in plan:
        used[str(e.clip.path)] = (e.trim_start, e.clip.duration)
    return used, "estimate"
