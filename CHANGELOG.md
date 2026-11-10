# Changelog

## 1.3.0 (not released yet)

From testers' feedback.

- New: an optional update check. Trimmer can ask GitHub for new versions when it opens, but only if
  you say yes: the installer asks (Allow update checks, or Local features only), and the portable
  zip asks the first time it opens. A new version brings up its release notes with Install update,
  Skip this version and Don't ask again; Settings > Updates and Help > Check for updates check any
  time. Install update downloads the installer, checks it against the release's SHA256SUMS.txt,
  installs it and reopens Trimmer, waiting for the queue to finish first if it's running.
  Nothing about you, your clips or your settings is sent, and with Local features only the update
  code isn't installed at all, so Trimmer has nothing that can connect to the internet.
- New: click a column header to sort a list by it (click again to reverse): clips in Clips, sessions
  in Replay merge, and waiting jobs in the Queue. Sorting the queue changes the order jobs run in, so
  Trimmer asks first (Don't ask again turns that off; Settings > Queue turns it back on). The running
  job and finished ones stay where they are.
- New: Auto 3's quality target is a slider from 80 to 99 instead of three choices, so it can go
  lower than 90 for smaller files. Marks under it show the recommended 90, 93 (default) and 95, and
  the text beside it says what the number means. Below 90 it searches higher CRFs (with one more
  sample encode), so each step down really makes a smaller file instead of stopping at the old
  ceiling.
- New: the hand cursor over buttons, check boxes, drop-downs, tabs, sliders and sortable column
  headers, so it's clear they can be pressed. Disabled ones keep the arrow.
- New: thin dividers between columns in the Clips, Replay merge and Queue lists and their headers.

## 1.2.0 (2026-11-06)

First release on GitHub, with a Windows installer and a portable zip.

- New: the queue's details panel starts with the job's full compression settings (codec and
  encoder, quality, preset, audio, size caps, "keep the original if bigger"), and updates when
  they change, including after "Auto settings, measured" has picked its settings.
- New: Settings > Replay merge > "Show the Replay merge tab" hides the tab for anyone who only
  trims clips (it also stops watching the replay folder until the tab is shown again).
- New: run logs and crash reports older than 30 days are deleted when Trimmer starts. Settings >
  App changes the age or turns it off, and "Delete all logs now" clears them.
- New: a Help menu with Getting started (F1), the keyboard shortcuts and the OBS tips, and a Quick
  start in the README.
- New: a cleaner, bolder icon that stays sharp at taskbar sizes, the logo in the toolbar, and a
  small splash while Trimmer starts.
- Changed: Settings' long General page is now two pages: Files (output folder, names, deleting
  originals, space saved, file type, cuts, ripped audio) and App (theme, timeline previews, on
  close, ffmpeg, logs). The "already queued" and "looks already processed" warnings are on the
  Queue page.
- Changed: tidier layout. The Add files / Add folder buttons are only in the toolbar (they add to
  the tab you're on), the Replay merge session list gets more room and its list, timeline and
  options can be resized by dragging, the Clips preview and options can be resized too, the kept
  parts list only takes the room it needs, the Video column is shorter ("1080p60 h264"; the full
  details are in its tooltip), and status messages clear when you switch tabs.
- New: when Windows stops Qt's video player from loading, the preview bar says "No sound" (hover for
  why) and Logs\playback.txt has the details, instead of quietly showing a slower, silent preview.
  The release build now checks that playback loads before it's published.
- Fixed: the Windows build's preview had no sound and was slow, because a part of Qt the video
  player needs had been left out of the build.
- New: Settings > Files > Space saved > Edit sets every figure (space saved, how much smaller
  the outputs are, clips processed and the date counting started), e.g. to carry totals over.
- New: when a source is damaged partway (for example a recording cut off by a crash), the job
  still fails and keeps the original, but the footage before the damage is saved as
  "name (partial, first 1h27m13s)" instead of being thrown away.
- Fixed: a crash or power cut while Trimmer was saving could empty its settings or the space
  saved totals. Both are now written safely, and the previous copy is used if a file is damaged.
- Fixed: a stray line above the Settings tabs.
- Fixed: the installed Trimmer.exe showed a blank icon on the taskbar while running.
- Changed: the Windows build compiles its own PyInstaller launcher, which antivirus programs
  mistake for malware far less often than the shared prebuilt one.
- New: Replay merge has Deselect all, Remove and Clear right above its session list, and
  "Remove from the list" (Delete) in its right-click menu. The files stay where they are.
- New: a "↺" button at the end of every Settings row that isn't on its default puts just that
  setting back (nothing is saved until Save), and "↺ Restore default" does the same for the
  custom compression settings.
- New: an "Auto 3 (H.265, quality 93)" compression preset: measured auto quality at the balanced
  target, keeping the original if compressing would make it bigger. Existing preset lists get it
  once; deleting it afterwards sticks.
- Fixed: number boxes (such as "Keyframe every") had their text against the left edge, unlike
  the other boxes, and their labels touched them.
- Fixed: Settings > Replay merge "Match clips frame by frame" always showed as ticked, even when
  it had been turned off.
- Changed: the Settings pages scroll, so the dialog and its Save button fit on smaller screens.
- Fixed: "Original" audio rips of FLAC tracks (common in MKV recordings) failed their check,
  because the copied file still claimed the whole source's length.
- Fixed: audio rips now carry the track's name (Mic, Desktop...) on the track itself, not only
  as the file's title, so players and Trimmer show it.
- Fixed: preview sound kept playing on the old speakers or headphones after the default output
  device was switched in Windows. It now follows the change.
- Changed: footage that already has a gap in its frames (a stretch where the recording has no
  picture, e.g. when the game stalled) no longer fails the job with "output has a ...s gap in its
  frames". Trimmer checks the gap against the source: if the source has it at that place, the job
  finishes and reports it (length, where it is in the output and in the source) in the queue's
  Details, the Notes column of Clips and Replay merge, the job log and the Replay merge info
  file. A gap the source doesn't have still fails the job, as before.
- Fixed: a gap from damaged frames (they are in the file but can't be decoded, so every player
  skips them) still failed the job as "a gap that isn't in the source". Trimmer now decodes the
  source around the gap, and reports it as "damaged frames that can't be played".
- Fixed: ffmpeg could keep running (with high CPU) after Trimmer closed, most often a quality
  trial for "Auto settings, measured" / Auto 3 that was still testing a waiting clip. Cancelling
  now stops those trials at once, closing Trimmer ends every ffmpeg it started, and on Windows
  each ffmpeg is tied to Trimmer so it ends too if Trimmer crashes or is ended from Task Manager.
- New: when ffmpeg fails, the queue says why in plain words (the disk is full, the file is open
  in another program, the GPU encoder isn't available, the source is damaged...), with ffmpeg's
  own last lines below it in the job's details.
- Fixed: a crash or power cut while Trimmer was saving the waiting queue could lose it. The queue
  is now saved the same safe way as the settings.
- Changed: audio rips, video-only rips and Replay merge's separate clips from a damaged source
  also keep the footage before the damage as "(partial, first ...)", like the main video.
- Changed: when "keep the original if compressing would make it bigger" skips a compressed file
  before making it, there's no longer a name-clash question about that file.
- Changed: each saved file's length is checked more closely against what was asked for (within
  1 second, or 0.2% on long files; it was 2%, which let up to 12 seconds go missing from a
  10-minute clip unnoticed).
- Changed: Clips > Estimate size follows "keep the original if compressing would make it bigger",
  as the queue does, and no longer guesses lossless sizes: lossless clips are listed as not counted.
- Fixed: an exact Replay merge with a resolution or frame rate cap re-encoded the sound when only one
  track was chosen to mix. It is copied untouched now.

## 1.1.0 (2026-10-30)

- New: "10-bit colour" option for H.265 and AV1 compression: about 5-10% smaller at the same
  quality, with less banding. Off by default. Hardware encoders are checked before use, and file
  names and the queue show "10-bit".
- New: "Auto settings, measured" compression method. Each clip is tested with H.265, AV1 and
  H.264 at several CRFs, and the smallest that keeps the chosen quality is used. "Quality to give
  up" (0-100) sets the target (VMAF 99 down to 80; 32 = VMAF 93). The queue chooses the settings
  before it can start and shows what it picked, and how the codecs compared, in the details panel.
- New: Queue right-click > Compression > "Custom, edit first…" edits the Custom settings and applies them.
- New: Replay merge has the same "Edit…" button next to the compression preset as Clips.
- Fixed: H.265 "Target file size" compression failed ("Incomplete CU-tree stats file").
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
