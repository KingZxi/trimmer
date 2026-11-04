# Third-party notices

Trimmer itself is © 2026 KingZxi and licensed under the GNU General Public
License v3.0 (see `LICENSE`). The Windows app (`Trimmer.exe` and the files in
its `_internal` folder) also contains the following software, each under its
own licence. Their licence texts are included with the app: in `LICENSES\`,
and in each package's `*.dist-info` folder inside `_internal`.

## Python

The Python interpreter and standard library, © Python Software Foundation.
Licensed under the Python Software Foundation License (PSF-2.0).
Licence text: `LICENSES\Python-LICENSE.txt`. https://www.python.org/

## Qt and Qt for Python (PySide6, shiboken6)

Qt 6 and Qt for Python, © The Qt Company Ltd. and other contributors, used
under the GNU Lesser General Public License v3.0 (`LICENSES\LGPL-3.0.txt`).
https://www.qt.io/ · Source code: https://download.qt.io/official_releases/

Qt Multimedia, which plays the preview, includes FFmpeg libraries
(avcodec, avformat, avutil, swresample, swscale), © the FFmpeg developers,
built under the GNU Lesser General Public License v2.1 or later
(`LICENSES\LGPL-2.1.txt`). https://ffmpeg.org/ · Source code:
https://ffmpeg.org/download.html

Trimmer uses these libraries unmodified and links to them dynamically. They are
ordinary DLL files in the `_internal` folder, so you can replace them with your
own builds of the same versions (or rebuild Trimmer from its source code against
other versions). Doing so is allowed and expected under the LGPL.

## numpy

© NumPy Developers. BSD 3-Clause licence, plus the licences of the code it
bundles (OpenBLAS and others). Licence texts: `_internal\numpy-*.dist-info\`.
https://numpy.org/

## Send2Trash

© Virgil Dupras and contributors. BSD 3-Clause licence.
Licence text: `_internal\send2trash-*.dist-info\`.
https://github.com/arsenetar/send2trash

## PyInstaller bootloader

`Trimmer.exe` starts through the PyInstaller bootloader, © PyInstaller
Development Team, GPL-2.0-or-later with an exception that allows it to be used
in programs under any licence. https://pyinstaller.org/

## ffmpeg (not included)

Trimmer does the video work by running the separate `ffmpeg` and `ffprobe`
programs that you install yourself. They are not part of Trimmer's download.
