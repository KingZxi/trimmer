"""Exact overlap detection by matching encoded packets.

OBS saves a replay by writing out packets it already encoded, so two
replays that overlap contain byte-identical packets for the shared
footage. Fingerprinting every packet (no decoding) finds the shared run
exactly: no timestamp guessing, no audio correlation, no safety margin,
and it works on silent footage. The join is then done by stream copy,
cutting the earlier clip at the very packet where the next clip begins,
so nothing repeats and nothing is lost.

Video packet hashes are taken after stripping parameter-set/SEI units,
because muxers re-insert SPS/PPS into keyframes (a remux changes those
bytes but not the picture data).

When clips don't share packets (OBS restarted between saves, files that
were re-encoded), callers fall back to the audio/timestamp method.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import ffmpeg as ff
from .ffmpeg import CancelToken

# Units stripped before hashing video packets (parameter sets, SEI, AUD).
_STRIP_UNITS = {
    "h264": "filter_units=remove_types=6|7|8|9",
    "hevc": "filter_units=remove_types=32|33|34|35|39|40",
}

EPS = 0.0005   # outpoints sit just below a packet's dts so that packet is excluded


@dataclass
class PacketList:
    path: Path
    hashes: List[str] = field(default_factory=list)
    pts: List[float] = field(default_factory=list)
    dts: List[float] = field(default_factory=list)
    key: List[bool] = field(default_factory=list)

    def __len__(self):
        return len(self.hashes)


_packet_cache: Dict[tuple, PacketList] = {}


def _probe_packets(path: Path, stream: str, with_hash: bool):
    entries = "packet=pts_time,dts_time,flags" + (",data_hash" if with_hash else "")
    cmd = [ff.ffprobe_bin(), "-v", "error", "-select_streams", stream, "-show_entries", entries]
    if with_hash:
        cmd += ["-show_data_hash", "md5"]
    cmd += ["-of", "csv=p=0", str(path)]
    rows = []
    for line in ff.run_quiet(cmd).stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        try:
            pts = float(parts[0]) if parts[0] not in ("", "N/A") else float("nan")
            dts = float(parts[1]) if parts[1] not in ("", "N/A") else pts
        except ValueError:
            continue
        rows.append((pts, dts, "K" in parts[2], parts[3] if with_hash and len(parts) > 3 else ""))
    return rows


def _video_hashes(path: Path, codec: str) -> List[str]:
    args = ["-v", "error", "-i", str(path), "-map", "0:v:0", "-c", "copy"]
    if codec in _STRIP_UNITS:
        args += ["-bsf:v", _STRIP_UNITS[codec]]
    args += ["-f", "framemd5", "-"]
    out = ff.run_quiet([ff.ffmpeg_bin()] + args)
    hashes = []
    for line in out.stdout.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        hashes.append(line.rsplit(",", 1)[-1].strip())
    return hashes


def read_packets(path, kind: str = "video", cancel: CancelToken = None) -> PacketList:
    """Per-packet fingerprint, pts, dts and keyframe flag, in decode order,
    for the first video stream or the first audio stream."""
    path = Path(path)
    key = (str(path.resolve()), kind, path.stat().st_mtime, path.stat().st_size)
    if key in _packet_cache:
        return _packet_cache[key]
    if cancel:
        cancel.check()
    pl = PacketList(path)
    if kind == "video":
        info = ff.probe(path)
        rows = _probe_packets(path, "v:0", with_hash=False)
        hashes = _video_hashes(path, info.video_codec)
        if len(hashes) != len(rows):
            # Packet counts disagree: don't trust the pairing.
            _packet_cache[key] = pl
            return pl
    else:
        rows = _probe_packets(path, "a:0", with_hash=True)
        hashes = [r[3] for r in rows]
    for (pts, dts, k, _), h in zip(rows, hashes):
        pl.hashes.append(h)
        pl.pts.append(pts)
        pl.dts.append(dts)
        pl.key.append(k)
    _packet_cache[key] = pl
    return pl


_pts_cache: Dict[tuple, List[float]] = {}


def read_pts(path, cancel: CancelToken = None) -> List[float]:
    """Presentation times of the first video stream's packets, sorted (no
    fingerprints, so much quicker than read_packets on a long file)."""
    path = Path(path)
    key = (str(path.resolve()), path.stat().st_mtime, path.stat().st_size)
    if key not in _pts_cache:
        if cancel:
            cancel.check()
        _pts_cache[key] = sorted(r[0] for r in _probe_packets(path, "v:0", with_hash=False) if r[0] == r[0])
    return _pts_cache[key]


MIN_RUN = 30   # packets that must match in a row before a candidate is examined


def find_in(haystack: List[str], needle: List[str], expected: Optional[int] = None,
            window: Optional[int] = None) -> Optional[Tuple[int, bool]]:
    """Where `needle` starts inside `haystack`, requiring the WHOLE
    overlapping run to be identical. Returns (position, contained), where
    contained means all of needle lies inside haystack, or None.

    `expected`/`window` keep candidates near the timestamp estimate, which
    rules out accidental matches between identical static frames."""
    if not needle or not haystack:
        return None
    first = needle[0]
    candidates = [i for i, h in enumerate(haystack) if h == first]
    if expected is not None and window is not None:
        candidates = [i for i in candidates if abs(i - expected) <= window]
        candidates.sort(key=lambda i: abs(i - expected))
    k = min(MIN_RUN, len(needle))
    for i in candidates:
        overlap = min(len(haystack) - i, len(needle))
        if overlap < k:
            continue
        if haystack[i:i + overlap] == needle[:overlap]:
            return i, len(needle) <= len(haystack) - i
    return None


@dataclass
class Piece:
    """The used part of one source clip: from its start to `stop`."""
    clip: object                      # merge.ReplayClip
    video: PacketList
    audio: Optional[PacketList]
    stop: Optional[int] = None        # video packet index where the next piece takes over
    audio_stop: Optional[int] = None  # audio packet index where the next piece takes over
    duration: Optional[float] = None  # timeline length given to the concat demuxer

    def used_video(self) -> List[str]:
        return self.video.hashes[: self.stop if self.stop is not None else len(self.video)]

    def used_audio(self) -> List[str]:
        if self.audio is None:
            return []
        return self.audio.hashes[: self.audio_stop if self.audio_stop is not None else len(self.audio)]


@dataclass
class PacketJoin:
    pieces: List[Piece]
    dropped: List[object]
    ok: bool                          # False = some join had no exact match
    notes: List[str] = field(default_factory=list)
    # id(clip) -> (packet index where the clip starts in the one continuous
    # stream, its packet count, seconds from its last packet to its end).
    # Used and dropped clips alike, so every clip sits on one timeline.
    places: Dict[int, Tuple[int, int, float]] = field(default_factory=dict)

    def piece_at(self, index: int) -> Optional[int]:
        """Which piece holds the stream's packet `index` (the last piece
        for anything past the end)."""
        found = None
        for k, p in enumerate(self.pieces):
            base = self.places.get(id(p.clip), (None,))[0]
            if base is None:
                return None
            if index < base:
                break
            found = k
        return found

    def locate_end(self, clip) -> Optional[Tuple[int, float]]:
        """Where the clip's original, uncut end lies: (piece index, seconds
        after that piece's start_time). Found by following the clip's last
        packet, which is byte-identical in the piece that carries it."""
        place = self.places.get(id(clip))
        if place is None or not self.pieces:
            return None
        base, count, tail = place
        k = self.piece_at(base + count - 1)
        if k is None:
            return None
        p = self.pieces[k]
        j = min(base + count - 1 - self.places[id(p.clip)][0], len(p.video) - 1)
        return k, p.video.pts[j] - p.clip.info.start_time + tail

    def expected_video(self) -> List[str]:
        return sum((p.used_video() for p in self.pieces), [])

    def expected_audio(self) -> List[str]:
        return sum((p.used_audio() for p in self.pieces), [])

    def copy_bytes(self) -> int:
        """About how big a stream-copied join is: each clip's size for the
        share of its frames that is used."""
        return int(sum(p.clip.info.size * len(p.used_video()) / max(1, len(p.video)) for p in self.pieces))

    @property
    def total_duration(self) -> float:
        total = 0.0
        for p in self.pieces:
            if p.duration is not None:
                total += p.duration
            elif p.stop is not None and p.stop < len(p.video):
                total += p.video.pts[p.stop] - p.clip.info.start_time
            else:
                total += p.clip.duration
        return total


def join_by_packets(clips, fps: float = 60.0, window_s: float = 30.0,
                    cancel: CancelToken = None) -> PacketJoin:
    """Chain clips (sorted by start time) into one gap-free, repeat-free
    stream wherever their packets match exactly.

    Each clip's first packet (a keyframe) is searched for in everything
    merged so far, near where the timestamps put it. If the whole overlap
    is identical, the merged stream is cut at that packet and the new clip
    appended. A clip whose packets all already appear is dropped; an
    earlier clip that the new one completely replaces is dropped too.
    Any join without an exact match makes ok=False (caller falls back)."""
    notes: List[str] = []
    pieces: List[Piece] = []
    merged: List[str] = []
    owner: List[Tuple[int, int]] = []          # merged index -> (piece, packet)
    dropped = []
    places: Dict[int, Tuple[int, int, float]] = {}

    def place(clip, v, at):
        end = clip.info.start_time + clip.duration
        tail = end - v.pts[-1]
        places[id(clip)] = (at, len(v), tail if tail == tail else 0.0)

    for clip in clips:
        v = read_packets(clip.path, "video", cancel)
        a = read_packets(clip.path, "audio", cancel) if clip.info.audio else None
        if not v.hashes or not v.key or not v.key[0]:
            return PacketJoin(pieces, dropped, False,
                              notes + [f"{clip.path.name}: packets couldn't be read or it doesn't start on a keyframe"])
        if not pieces:
            pieces.append(Piece(clip, v, a))
            place(clip, v, 0)
            merged, owner = list(v.hashes), [(0, j) for j in range(len(v))]
            continue
        last = pieces[-1]
        last_base = next(i for i, (pi, _) in enumerate(owner) if pi == len(pieces) - 1)
        offset_s = (clip.start - last.clip.start).total_seconds()
        expected = last_base + int(round(offset_s * fps))
        hit = find_in(merged, v.hashes, expected, int(window_s * fps))
        if hit is None:
            notes.append(f"{clip.path.name}: no identical packets found where it overlaps the clip before it")
            return PacketJoin(pieces, dropped, False, notes, places)
        pos, contained = hit
        place(clip, v, pos)
        if contained:
            dropped.append(clip)
            notes.append(f"{clip.path.name}: every frame already appears in the clips before it; dropped")
            continue
        piece_i, pkt_i = owner[pos]
        for p in pieces[piece_i + 1:]:
            dropped.append(p.clip)
            notes.append(f"{p.clip.path.name}: fully covered once {clip.path.name} joins; dropped")
        pieces = pieces[:piece_i + 1]
        if pkt_i == 0:
            dropped.append(pieces[piece_i].clip)
            notes.append(f"{pieces[piece_i].clip.path.name}: fully covered by {clip.path.name}; dropped")
            pieces.pop()
        else:
            pieces[piece_i].stop = pkt_i
            notes.append(f"{clip.path.name}: exact match, takes over after "
                         f"{pieces[piece_i].video.pts[pkt_i]:.3f}s of {pieces[piece_i].clip.path.name}")
        merged, owner = merged[:pos], owner[:pos]
        pieces.append(Piece(clip, v, a))
        owner += [(len(pieces) - 1, j) for j in range(len(v))]
        merged += v.hashes

    # Timeline lengths and audio cut points for every piece but the last.
    for p, nxt in zip(pieces, pieces[1:]):
        p_start, n_start = p.clip.info.start_time, nxt.clip.info.start_time
        kf_pts_in_p = p.video.pts[p.stop]
        kf_pts_in_n = nxt.video.pts[0]
        p.duration = (kf_pts_in_p - p_start) - (kf_pts_in_n - n_start)
        if p.audio is not None and nxt.audio is not None and nxt.audio.hashes:
            # The next clip's first audio packet, located by time in this
            # clip, must be byte-identical too.
            t = kf_pts_in_p + (nxt.audio.pts[0] - kf_pts_in_n)
            cands = sorted(range(len(p.audio)), key=lambda i: abs(p.audio.pts[i] - t))[:5]
            match = next((i for i in cands if p.audio.hashes[i] == nxt.audio.hashes[0]), None)
            if match is None:
                notes.append(f"{nxt.clip.path.name}: audio packets didn't line up exactly; "
                             f"audio is cut at the video join (may overlap by a few milliseconds)")
                p.audio_stop = next((i for i, d in enumerate(p.audio.dts)
                                     if d >= p.video.dts[p.stop] - EPS), len(p.audio))
            else:
                p.audio_stop = match
    return PacketJoin(pieces, dropped, True, notes, places)


def _concat_list(pieces: List[Piece], kind: str) -> str:
    lines = ["ffconcat version 1.0"]
    for p in pieces:
        path = Path(p.clip.path).resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{path}'")
        stop = p.stop if kind == "video" else p.audio_stop
        pl = p.video if kind == "video" else p.audio
        if stop is not None and pl is not None and stop < len(pl):
            lines.append(f"outpoint {pl.dts[stop] - EPS:.6f}")
        if p.duration is not None:
            lines.append(f"duration {p.duration:.6f}")
    return "\n".join(lines) + "\n"


def write_concat_lists(join: PacketJoin, folder: Path) -> Tuple[Path, Optional[Path]]:
    """ffconcat files for the video and (separately cut) audio streams."""
    v = Path(folder) / "video.ffconcat"
    v.write_text(_concat_list(join.pieces, "video"), encoding="utf-8")
    if all(p.audio is not None for p in join.pieces):
        a = Path(folder) / "audio.ffconcat"
        a.write_text(_concat_list(join.pieces, "audio"), encoding="utf-8")
        return v, a
    return v, None


def compare(expected: List[str], actual: List[str], what: str = "video") -> Optional[str]:
    """None if identical, else where the first difference is."""
    if expected == actual:
        return None
    n = min(len(expected), len(actual))
    for i in range(n):
        if expected[i] != actual[i]:
            return f"{what} differs from the source footage at packet {i + 1} of {len(expected)}"
    if len(actual) < len(expected):
        return f"{what} is missing the last {len(expected) - len(actual)} packet(s) of source footage"
    return f"{what} has {len(actual) - len(expected)} more packet(s) than the sources"
