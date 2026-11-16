# Eden Duo companion in nxnative

The `eden_duo/` tree in this directory is a copy of the Eden Duo ACNH companion
integration from AnimalDuo 1.3 (application build 3.0.76). Its own provenance,
component licenses and historical modification notices are in `NOTICES.txt`;
the exact upstream commits are in `UPSTREAM.json`.

Original component licenses:

- ACNH native module, portable C ABI headers and the data-driven interface
  renderer ported to this repository: GPL-3.0-or-later (`LICENSE-GPL-3.0.txt`).
- BC decoder: MPL-2.0 (`LICENSE-MPL-2.0.txt`).
- stb_image / stb_truetype: MIT or public domain (`LICENSE-STB.txt`).
- Zstandard decompression sources, in `../zstd`: BSD (`../zstd/LICENSE`).
- The original nxnative sources keep their MIT notices.

No extracted game images are added to this source directory. All companion
pictures, names and island information come from the player's own running game
and RomFS.

A build that includes this companion is supplied with its corresponding source
and build instructions under GPL-3.0-or-later, preserving the original notices
and the licenses of its component source files. The remainder of the nxnative
source tree is MIT-licensed.
