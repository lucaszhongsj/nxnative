"""Eden Duo's pin check (dm/tools/companion_pins.py) on synthetic .inc text and images,
no game files: every word found, one changed, a pin past the end of the image, a line
that is not a pin, a file without pins, a name in two files, and a .inc generated for
another build than the image's cuts file names."""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import companion_pins as cp

# The emitted formatting: one pin a line, comments between, the build in the header.
LIVE = '''// Generated from the 3.0.3 main (build ff1d1c05670db602...).
// Exact instruction words; a mismatch fails the dependent domain closed.
inline const Pin PinSaveMgr{"SaveMgr", 0x6e19d8, {0xf0026c88, 0x91010108, 0xf9400108}};
// save object vtable slot 2: ldr x0,[x0,#0x10]; ret
inline const Pin PinSlotData{"SlotData", 0x260bd28, {0xf9400800, 0xd65f03c0}};
'''
BAG = '''// Generated from the 3.0.3 main (build ff1d1c05670db602...).
inline const BagPin PinBagSwap{"BagSwap", 0x26fd1f8, {0xb9415808, 0x52800299}};
inline const BagPin PinPocketSlot{"PocketSlot", 0x32874e8, {0x94000248, 0xf9004680}};
'''
BUILD = 'ff1d1c05670db6021c85b624a710b963'


def write(root, name, text):
    p = Path(root) / name
    p.write_text(text)
    return p


def image(root, words):
    """An image holding the given words ({offset: [u32, ...]}), as long as its last one."""
    p = Path(root) / 'main.patched.bin'
    data = bytearray(max(o + 4 * len(ws) for o, ws in words.items()))
    for o, ws in words.items():
        struct.pack_into('<' + 'I' * len(ws), data, o, *ws)
    p.write_bytes(data)
    return p


class Pins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_every_word_found(self):
        live = write(self.tmp.name, 'live.inc', LIVE)
        img = image(self.tmp.name, {0x6e19d8: [0xf0026c88, 0x91010108, 0xf9400108],
                                    0x260bd28: [0xf9400800, 0xd65f03c0]})
        pins = cp.parse_pins(live)
        self.assertEqual([(p.name, p.offset) for p in pins], [('SaveMgr', 0x6e19d8), ('SlotData', 0x260bd28)])
        self.assertEqual(pins[0].stated, BUILD[:16])
        self.assertEqual(cp.check(img, pins), [])

    def test_one_changed_word(self):
        live = write(self.tmp.name, 'live.inc', LIVE)
        img = image(self.tmp.name, {0x6e19d8: [0xf0026c88, 0x91010108, 0xdeadbeef],
                                    0x260bd28: [0xf9400800, 0xd65f03c0]})
        bad = cp.check(img, cp.parse_pins(live))
        self.assertEqual(len(bad), 1)
        pin, i, want, got = bad[0]
        self.assertEqual((pin.name, i, want, got), ('SaveMgr', 2, 0xf9400108, 0xdeadbeef))

    def test_pin_past_the_end_is_a_mismatch(self):
        live = write(self.tmp.name, 'live.inc', 'inline const Pin PinSaveMgr{"SaveMgr", 0x6e19d8, {0xf0026c88}};\n')
        img = image(self.tmp.name, {0x100: [0]})
        bad = cp.check(img, cp.parse_pins(live))
        self.assertEqual([(p.name, i, got) for p, i, _, got in bad], [('SaveMgr', 0, None)])

    def test_a_pin_running_past_the_end_stops_at_its_first_missing_word(self):
        live = write(self.tmp.name, 'live.inc', 'inline const Pin PinEdge{"Edge", 0x100, {0x0, 0x1}};\n')
        img = image(self.tmp.name, {0x100: [0]})
        bad = cp.check(img, cp.parse_pins(live))
        self.assertEqual([(p.name, i, got) for p, i, _, got in bad], [('Edge', 1, None)])

    def test_a_line_that_is_not_a_pin_fails(self):
        bad = write(self.tmp.name, 'bad.inc', 'inline const Pin PinBad{"Bad", 0x10};\n')
        with self.assertRaises(ValueError): cp.parse_pins(bad)

    def test_a_bad_instruction_word_fails(self):
        bad = write(self.tmp.name, 'bad.inc', 'inline const Pin PinBad{"Bad", 0x10, {0x1, 0xzz}};\n')
        with self.assertRaises(ValueError): cp.parse_pins(bad)

    def test_a_file_without_pins_fails(self):
        empty = write(self.tmp.name, 'empty.inc', '// nothing here\n')
        with self.assertRaises(ValueError): cp.parse_pins(empty)

    def test_a_name_in_two_files_is_two_pins(self):
        live = write(self.tmp.name, 'live.inc', LIVE)
        bag = write(self.tmp.name, 'bag.inc', BAG.replace('BagSwap', 'PocketSlot'))
        pins = cp.parse_pins(live) + cp.parse_pins(bag)
        self.assertEqual([p.name for p in pins], ['SaveMgr', 'SlotData', 'PocketSlot', 'PocketSlot'])
        img = image(self.tmp.name, {0x6e19d8: [0xf0026c88, 0x91010108, 0xf9400108],
                                    0x260bd28: [0xf9400800, 0xd65f03c0],
                                    0x26fd1f8: [0xb9415808, 0x52800299],
                                    0x32874e8: [0x94000248, 0xf9004680]})
        self.assertEqual(len(cp.check(img, pins)), 0)

    def test_the_image_build_and_the_pins_build(self):
        img = image(self.tmp.name, {0x10: [0]})
        (Path(self.tmp.name) / 'main.cuts.tsv').write_text(f'# BUILD_ID\t{BUILD}\n0x5c9b80\t29200c29\tc23a6f15\ta cut\n')
        live = write(self.tmp.name, 'live.inc', LIVE)
        self.assertEqual(cp.image_build(img), BUILD)
        self.assertEqual(cp.other_builds(cp.parse_pins(live), BUILD), [])
        old = write(self.tmp.name, 'old.inc', LIVE.replace(BUILD[:16], '0123456789abcdef'))
        self.assertEqual(cp.other_builds(cp.parse_pins(old), BUILD), [old])

    def test_no_cuts_file_leaves_the_build_unknown(self):
        self.assertIsNone(cp.image_build(image(self.tmp.name, {0x10: [0]})))


if __name__ == '__main__':
    unittest.main()
