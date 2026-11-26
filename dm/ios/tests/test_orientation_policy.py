"""Keep the production and harness phone/iPad orientation contracts aligned."""
import re
import unittest
from pathlib import Path

IOS = Path(__file__).resolve().parents[1]


class OrientationPolicyTests(unittest.TestCase):
    def test_phone_landscape_and_ipad_resizing(self):
        landscape = {"UIInterfaceOrientationLandscapeLeft", "UIInterfaceOrientationLandscapeRight"}
        all_orientations = landscape | {"UIInterfaceOrientationPortrait", "UIInterfaceOrientationPortraitUpsideDown"}
        for project in (IOS / "project.yml", IOS / "tests/duo/project.yml"):
            source = project.read_text()
            for key, expected in (("UISupportedInterfaceOrientations", landscape),
                                  ("UISupportedInterfaceOrientations~ipad", all_orientations)):
                with self.subTest(project=project.name, key=key):
                    match = re.search(r"^        " + re.escape(key) + r":\n((?:          - .*\n)+)", source, re.MULTILINE)
                    assert match is not None, f"Missing orientation array: {key}"
                    self.assertEqual(set(re.findall(r"- (\w+)", match[1])), expected)
            self.assertNotIn("UIRequiresFullScreen:", source)

    def test_portrait_is_the_union_profiles_choice(self):
        # A phone standing up splits at its midpoint (DmLayout.h dm_layout), which only the profile whose stubs read the union window follows
        # (screen_union). That profile's build adds the phone's portrait to the generated Info.plist; every other game keeps the template's contract.
        source = (IOS / "tools/build_ipa.sh").read_text()
        match = re.search(r"if grep -qE '\^screen_union[^']*' \"\$GD/game\.tsv\"; then\n[^\n]*UIInterfaceOrientationPortrait", source)
        self.assertIsNotNone(match, "build_ipa.sh: the phone's Portrait must be added only for a screen_union profile")


if __name__ == "__main__":
    unittest.main()
