"""The companion gate of build_ipa.sh: an image its pins were not found on must stop the pack.

The tool's own parsing and comparison are dm/tools/tests/test_companion_pins.py's; this runs the
steps build_ipa.sh takes before it bundles the companion, on a synthetic image and pin file.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[3]
BUILD = 'ff1d1c05670db6021c85b624a710b963'
PINS = f'''// Generated from the build (build {BUILD[:16]}).
inline const Pin PinSaveMgr{{"SaveMgr", 0x10, {{0x11111111, 0x22222222}}}};
'''


def block():
    """The gate as build_ipa.sh runs it, between the companion profile's first use and its copy."""
    script = (REPO / 'dm/ios/tools/build_ipa.sh').read_text()
    return script[script.index('UI=$(python3'):script.index('cp "$GD/companion.json"')]


class CompanionPackTests(unittest.TestCase):
    def run_gate(self, root, write_builds=(BUILD,), words=(0x11111111, 0x22222222), cuts=True, pins=True):
        dm, gd, patched = root / 'dm', root / 'profile', root / 'patched'
        vendor = dm / 'third_party' / 'eden_duo' / 'acnh'
        (dm / 'tools').mkdir(parents=True); vendor.mkdir(parents=True); gd.mkdir(); patched.mkdir()
        (dm / 'tools' / 'companion_pins.py').symlink_to(REPO / 'dm/tools/companion_pins.py')
        if pins: (vendor / 'x_pins.inc').write_text(PINS)
        (gd / 'companion.json').write_text(json.dumps({'ui': 'acnh', 'write_builds': list(write_builds)}))
        image = bytearray(0x18); image[0x10:0x18] = b''.join(w.to_bytes(4, 'little') for w in words)
        (patched / 'main.patched.bin').write_bytes(image)
        if cuts: (patched / 'main.cuts.tsv').write_text(f'# BUILD_ID\t{BUILD}\n')
        env = {k: v for k, v in os.environ.items() if k not in ('DM_GAMES_DIR', 'DM_GAMES_FIRST')}
        env.update(DM=str(dm), GD=str(gd), PATCHED=str(patched))
        return subprocess.run(['/bin/bash', '-c', 'set -u\ndie() { echo "$*" >&2; exit 1; }\n' + block()],
                              env=env, capture_output=True, text=True)

    def test_the_image_the_pins_were_found_on_packs(self):
        with tempfile.TemporaryDirectory() as temporary:
            r = self.run_gate(Path(temporary))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn('1 pin matches', r.stdout)

    def test_a_differing_word_stops_the_pack(self):
        with tempfile.TemporaryDirectory() as temporary:
            r = self.run_gate(Path(temporary), words=(0x11111111, 0xdeadbeef))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('is not the image the pins were found on', r.stderr)
            self.assertIn('SaveMgr', r.stdout)

    def test_a_build_the_host_does_not_open_stops_the_pack(self):
        with tempfile.TemporaryDirectory() as temporary:
            r = self.run_gate(Path(temporary), write_builds=('0' * 32,))
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('write_builds', r.stderr)

    def test_an_image_without_its_build_id_stops_the_pack(self):
        with tempfile.TemporaryDirectory() as temporary:
            r = self.run_gate(Path(temporary), cuts=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('the build ID is not checked', r.stderr)
            self.assertIn('build unknown', r.stderr)

    def test_a_game_without_pin_files_stops_the_pack(self):
        with tempfile.TemporaryDirectory() as temporary:
            r = self.run_gate(Path(temporary), pins=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('no *_pins.inc', r.stderr)


if __name__ == '__main__':
    unittest.main()
