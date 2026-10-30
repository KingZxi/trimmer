# Changelog

## 1.0.1 (unreleased)

- Fixed: compressed trims that start partway into a clip (including the second part of a split)
  and "Original" audio rips of them could play with the audio late in some players. The copied
  audio kept up to a keyframe interval of sound from before the cut.
- Fixed: trims kept without re-encoding with several parts (and plain Replay merges that fall
  back to joining files) let the audio fall further behind at each join. Each part's audio is
  now cut so it stays within half an audio packet (about 11 ms) of the picture.
- Fixed: smart cut audio could be up to 17 ms early in each part.
- Fixed: trims without re-encoding from MKV files failed their check, and smart cut on MKV
  always fell back to re-encoding (and could start a frame early).
- Fixed: changing a track's volume without compressing failed.
- Fixed: the mixed audio track was 5 ms late (the limiter's delay).
- Fixed: Replay merges joined without re-encoding from replays that don't share packets let the
  sound fall 10-20 ms further behind at each join (each replay's last audio packet is played in full).

## 1.0.0 (2026-10-29)

First public release.

- Clips: preview with thumbnails and waveform, trims with any number of parts, "Split here" and
  "Save parts separately", frame-exact cuts by smart cut, audio track choice and mixing, compression
  (CRF, auto2, auto3 with a quality target, target size, lossless; H.264/H.265/AV1, NVENC/QSV/AMF),
  audio and video rips, full screen preview.
- Replay merge: overlapping OBS replays joined frame-exactly by matching packets, with a
  nothing-lost check; trim mode; watching the replay folder; never saves a compressed merge bigger
  than a plain merge (when "keep the original if bigger" is on).
- Queue: pause and resume in place, automatic pause while a game is full screen, a global pause key
  with chimes, crash recovery, safe deletion of originals only after every job that needs them is done.
- Fully offline, with a first launch guide for getting ffmpeg.
