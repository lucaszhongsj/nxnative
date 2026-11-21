#!/usr/bin/env python3
"""Eden Duo's ACNH code pins, checked against the patched main image the host maps.

Usage: python3 dm/tools/companion_pins.py <main.patched.bin> <pins.inc>...

The .inc files in dm/third_party/eden_duo/acnh hold the exact instruction words of the
functions the companion finds; the module closes the domain a differing word belongs to,
so a new build would take panels away without an error. Each pin carries its main-relative
offset, and the host maps the patched image from its first byte, so that is also the file
offset. A pin past the end of the image is a mismatch, and a .inc file with no pin, or a
line that cannot be read, fails as well: a skipped pin is a domain that closes silently.
The image's build ID is the one patch_nso.py writes beside it (main.cuts.tsv); a .inc
whose header names another build fails before any word is compared; when there is no cuts
file beside the image the words alone decide, and the check says so.

Needs game files to say anything, not to be imported: the tests build their own image
and .inc text.
"""
import argparse
import re
import struct
import sys
from collections import namedtuple
from pathlib import Path

PIN = re.compile(r'inline const \w+\s+(\w+)\s*\{\s*"([^"]+)"\s*,\s*0x([0-9a-fA-F]+)\s*,\s*\{([^}]*)\}\s*\}\s*;')
WORD = re.compile(r'0x[0-9a-fA-F]{1,8}')
STATED = re.compile(r'build ([0-9a-fA-F]{16})')
BUILD_ID = re.compile(r'#\s*BUILD_ID\s+([0-9a-fA-F]{32})\s*')

Pin = namedtuple('Pin', 'source name offset words stated')


def parse_pins(path):
    """One .inc file's pins, in order, each with the build its header names (None if it names none)."""
    text = path.read_text()
    found = STATED.search(text)
    stated = found.group(1).lower() if found else None
    pins = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('//'): continue
        m = PIN.fullmatch(line)
        if not m:
            raise ValueError(f'{path}:{n}: not a pin: {line[:60]}')
        name, offset, words = m.group(2), int(m.group(3), 16), [w.strip() for w in m.group(4).split(',') if w.strip()]
        if not words or any(not WORD.fullmatch(w) for w in words):
            raise ValueError(f'{path}:{n}: {name}: bad instruction words')
        pins.append(Pin(path, name, offset, tuple(int(w, 16) for w in words), stated))
    if not pins:
        raise ValueError(f'{path}: no pins')
    return pins


def check(image, pins):
    """The (pin, word index, expected, found) of every word that differs; found is None past the image's end."""
    out, size = [], image.stat().st_size
    with image.open('rb') as f:
        for pin in pins:
            for i, want in enumerate(pin.words):
                at = pin.offset + 4 * i
                if at + 4 > size:
                    out.append((pin, i, want, None)); break
                f.seek(at)
                got, = struct.unpack('<I', f.read(4))
                if got != want: out.append((pin, i, want, got))
    return out


def image_build(image):
    """The build ID patch_nso.py writes beside the image, or None when there is no cuts file."""
    cuts = image.with_name('main.cuts.tsv')
    if not cuts.exists(): return None
    with cuts.open() as f:
        m = BUILD_ID.fullmatch(f.readline())
    return m.group(1).lower() if m else None


def other_builds(pins, build):
    """The .inc files whose header names a build other than the image's."""
    if not build: return []
    return sorted({p.source for p in pins if p.stated and p.stated != build[:16]})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('image', type=Path, help='the patched main image (main.patched.bin)')
    ap.add_argument('pins', nargs='+', type=Path, help='the .inc files')
    a = ap.parse_args()
    try:
        pins = [p for path in a.pins for p in parse_pins(path)]
    except (ValueError, OSError) as e:
        print(f'companion_pins: {e}', file=sys.stderr)
        return 2
    build = image_build(a.image)
    if not build:
        print('companion_pins: no main.cuts.tsv beside the image: the build ID is not checked', file=sys.stderr)
    wrong = other_builds(pins, build)
    for path in wrong:
        print(f'companion_pins: {path} was generated from another build than {build}', file=sys.stderr)
    if wrong: return 1
    counts = {p.source.name: 0 for p in pins}
    for p in pins: counts[p.source.name] += 1
    listed = ', '.join(f'{name} {n}' for name, n in counts.items())
    bad = check(a.image, pins)
    for pin, i, want, got in bad:
        found = 'past the end of the image' if got is None else f'{got:#010x}'
        print(f'{pin.source}: {pin.name} at {pin.offset:#x} word {i}: {want:#010x}, image has {found}')
    if bad:
        n = len(bad)
        print(f'companion_pins: {n} word{"s" if n != 1 else ""} differ{"s" if n == 1 else ""} in {len({p.name for p, _, _, _ in bad})} of {len(pins)} pins ({listed})'
              + (f' build {build}' if build else ''))
        return 1
    n = len(pins)
    print(f'companion_pins: {n} pin{"s" if n != 1 else ""} match{"es" if n == 1 else ""} ({listed})' + (f' build {build}' if build else ''))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
