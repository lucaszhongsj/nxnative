"""Single-game iOS bundle ID team suffix and Xcode team auto-detection tests."""
from contextlib import ExitStack, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
for folder in ('scripts', 'dm/games', 'dm/tools', 'dm/ios/tools'):
    sys.path.insert(0, str(REPO / folder))
import build_ipa_from_nsp as build
import game
import nacp
import profile_guess
import romfs_image
import xcode_projects

GAME = '0100A6301214E000'
TID_LOWER = GAME.lower()
SCRIPT = (REPO / 'dm/ios/tools/build_ipa.sh').read_text()
BUNDLE_BLOCK = SCRIPT[SCRIPT.index('DM_TEAM_ID=${DM_TEAM_ID:-0000000000}'):SCRIPT.index('\nif [ "${DM_SHOW_FPS:-0}" = 1 ]')]
PREPARE_ENTITLEMENTS = SCRIPT[
    SCRIPT.index('# A prepared Xcode project is signed in Xcode'):
    SCRIPT.index('# DM_COPY_ROMFS=NO also for the prepared project')
]
SIGNING = SCRIPT[SCRIPT.index('# The entitlements of an unsigned IPA'):SCRIPT.index('\nP=Payload/DmIOS.app\n')]
CAN_SIGN = sys.platform == 'darwin' and shutil.which('codesign') and shutil.which('cc')


def make_completed(returncode=0, stdout='', stderr=''):
    return subprocess.CompletedProcess(['tool'], returncode, stdout=stdout, stderr=stderr)


class TeamDetectionTests(unittest.TestCase):
    def test_single_unique_apple_development_team_is_detected(self):
        find_id = make_completed(0, '  1) AAAA1111 "Apple Development: user@example.com (CERTNAME01)"\n     1 valid identities found\n')
        cert_pem = make_completed(0, '-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n')
        subject = make_completed(0, 'subject=UID=U1, CN=Apple Development: user@example.com (CERTNAME01), OU=TEAM123456, O=User, C=US\n')
        with patch.object(build.subprocess, 'run', side_effect=[find_id, cert_pem, subject]) as mocked:
            self.assertEqual(build.detect_development_team(), 'TEAM123456')
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(mocked.call_args_list[0].kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(mocked.call_args_list[0].kwargs['timeout'], 5)

    def test_duplicate_certs_for_same_team_still_resolve_single_team(self):
        find_id = make_completed(0, (
            '  1) AAAA1111 "Apple Development: user@example.com (CERTNAME01)"\n'
            '  2) BBBB2222 "iPhone Developer: user@example.com (CERTNAME02)"\n'
            '     2 valid identities found\n'
        ))
        cert = make_completed(0, 'PEM')
        subj = make_completed(0, 'subject=CN=x, OU=SAME123456, O=User, C=US\n')
        with patch.object(build.subprocess, 'run', side_effect=[find_id, cert, subj, cert, subj]):
            self.assertEqual(build.detect_development_team(), 'SAME123456')

    def test_zero_teams_returns_empty(self):
        find_id = make_completed(0, '  1) AAAA1111 "Developer ID Application: User (CORP123456)"\n     1 valid identities found\n')
        with patch.object(build.subprocess, 'run', return_value=find_id):
            self.assertEqual(build.detect_development_team(), '')

    def test_two_different_teams_return_empty(self):
        find_id = make_completed(0, (
            '  1) AAAA1111 "Apple Development: a@example.com (NAME000001)"\n'
            '  2) BBBB2222 "Apple Development: b@example.com (NAME000002)"\n'
            '     2 valid identities found\n'
        ))
        cert = make_completed(0, 'PEM')
        subj1 = make_completed(0, 'subject=CN=a, OU=TEAMAAAAAA, O=A, C=US\n')
        subj2 = make_completed(0, 'subject=CN=b, OU=TEAMBBBBBB, O=B, C=US\n')
        with patch.object(build.subprocess, 'run', side_effect=[find_id, cert, subj1, cert, subj2]):
            self.assertEqual(build.detect_development_team(), '')

    def test_timeout_and_errors_return_empty(self):
        with patch.object(build.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['security'], 5)):
            self.assertEqual(build.detect_development_team(), '')
        with patch.object(build.subprocess, 'run', side_effect=OSError('missing')):
            self.assertEqual(build.detect_development_team(), '')
        with patch.object(build.subprocess, 'run', return_value=make_completed(1, '')):
            self.assertEqual(build.detect_development_team(), '')
        find_id = make_completed(0, '  1) AAAA1111 "Apple Development: a@example.com (NAME000001)"\n')
        with patch.object(build.subprocess, 'run', side_effect=[find_id, subprocess.TimeoutExpired(['security'], 5)]):
            self.assertEqual(build.detect_development_team(), '')


class CliBundleIdWorkflowTests(unittest.TestCase):
    def run_cli(self, extra_args, *, saved_signing=None, env_overrides=None, security_side_effect=None,
                platform='darwin', setting_bundle_id='', setting_team_id=''):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary).resolve()
            (root / 'prod.keys').touch()
            base = root / 'base.nsp'
            base.touch()
            registered = game.load(GAME)
            profile = SimpleNamespace(
                id=registered.id, title_id=registered.title_id, dir=root / 'profile' / registered.id,
                get=registered.get, words=registered.words, modules_in=registered.modules_in,
                builds=lambda: {'v0': {'display': '1.0.0', 'main': 'build-0'}},
            )
            sources = {key: (False, '') for key in ('glslc_capture', 'vk_capture', 'gl_capture', 'ue_decode', 'oodle_packs', 'ue_legacy')}
            sources['graphics'] = ('vulkan', 'fixture')
            fam = {'base': base, 'updates': [], 'dlc': []}
            extraction = root / 'extracted' / 'v0'
            (extraction / 'RomFS').mkdir(parents=True)
            (extraction / 'ExeFS').mkdir()
            romfs_image.pack(extraction / 'RomFS', extraction / 'RomFS.img')

            captured = {}

            def fake_run(cmd, log, **kwargs):
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_text('')
                captured['cmd'] = cmd
                captured['env'] = kwargs.get('env', {})
                if cmd and str(cmd[0]) == 'bash' and str(cmd[1]).endswith('build_ipa.sh'):
                    captured['build_ipa_called'] = True
                    if '--prepare-xcode' not in cmd:
                        if 'DM_FRAMEWORK_ONLY' in captured['env']:
                            fw = Path(captured['env']['DM_FRAMEWORK_ONLY']) / f'DmGame{registered.id.upper()}.framework'
                            fw.mkdir(parents=True, exist_ok=True)
                            (fw / 'game.manifest.json').write_text('{}')
                        else:
                            Path(cmd[2]).parent.mkdir(parents=True, exist_ok=True)
                            Path(cmd[2]).write_bytes(b'ipa')

            def fake_mac_app(*args, **kwargs):
                captured['mac_app_bundle_id'] = args[16]

            for name, value in {
                'scan_nsps': (int(registered.id, 16), fam), 'check_prereqs': None, 'check_metal': None,
                'check_native_python': None, 'free_gb': 10000, 'find_nx_extract': root / 'nx_extract',
                'extract': extraction, 'check_formats': None, 'can_clone': False, 'build_mismatch': [],
                'extract_aoc': None, 'rewrite': profile, 'build_vk_shaders': root / 'shaders',
                'provisional_prep': None, 'chain_header': None,
                'mac_host': (root / 'dmhost', root / 'font.ttf'), 'mac_run': 'run',
            }.items():
                stack.enter_context(patch.object(build, name, return_value=value))
            stack.enter_context(patch.object(build, 'run', side_effect=fake_run))
            stack.enter_context(patch.object(build, 'mac_app', side_effect=fake_mac_app))
            stack.enter_context(patch.object(build.tree_tar, 'unpack'))
            stack.enter_context(patch.object(build.tree_tar, 'pack'))
            stack.enter_context(patch.object(build, 'apple_silicon', return_value=True))
            stack.enter_context(patch.dict(os.environ, env_overrides or {}, clear=False))
            if not env_overrides or 'DM_TEAM_ID' not in env_overrides:
                os.environ.pop('DM_TEAM_ID', None)
            if not env_overrides or 'DM_BUNDLE_ID' not in env_overrides:
                os.environ.pop('DM_BUNDLE_ID', None)
            stack.enter_context(patch.object(build, 'BUNDLE_ID', setting_bundle_id))
            stack.enter_context(patch.object(build, 'TEAM_ID', setting_team_id))
            stack.enter_context(patch.object(build.sys, 'platform', platform))
            stack.enter_context(patch.object(game, 'by_title', return_value=registered))
            stack.enter_context(patch.object(game, 'load', return_value=profile))
            stack.enter_context(patch.object(profile_guess, 'build_sources', return_value=sources))
            stack.enter_context(patch.object(nacp, 'name', return_value='Fixture Game'))
            stack.enter_context(patch.object(nacp, 'version', return_value='1.0.0'))
            stack.enter_context(patch.object(nacp, 'languages', return_value=['en-US']))
            stack.enter_context(patch.object(xcode_projects, 'signing_settings', return_value=saved_signing or {}))
            stack.enter_context(patch.object(xcode_projects, 'register_project'))
            stack.enter_context(patch.object(xcode_projects, 'check_bundle_id'))
            sec_mock = stack.enter_context(patch.object(build.subprocess, 'run', side_effect=security_side_effect))
            progress = build.Progress()
            stack.enter_context(patch.object(build, 'PROG', progress))
            out = io.StringIO()
            try:
                with redirect_stdout(out):
                    code = build.cli([str(root), '-k', str(root / 'prod.keys'), *extra_args])
            finally:
                progress.close()
            self.assertEqual(code, 0, out.getvalue())
            captured['security_calls'] = sec_mock.call_count
            return captured

    def test_explicit_bundle_id_always_wins(self):
        res = self.run_cli(
            ['--prepare-xcode', '--bundle-id', 'com.explicit.cli', '--team-id', 'TEAM123456'],
            saved_signing={'PRODUCT_BUNDLE_IDENTIFIER': 'com.saved.game', 'DEVELOPMENT_TEAM': 'OLDTEAM999'},
            env_overrides={'DM_BUNDLE_ID': 'com.env.game'},
            setting_bundle_id='com.setting.game',
        )
        self.assertEqual(res['env']['DM_BUNDLE_ID'], 'com.explicit.cli')
        self.assertEqual(res['env']['DM_TEAM_ID'], 'TEAM123456')

        res_env = self.run_cli(
            ['--prepare-xcode', '--team-id', 'TEAM123456'],
            saved_signing={'PRODUCT_BUNDLE_IDENTIFIER': 'com.saved.game'},
            env_overrides={'DM_BUNDLE_ID': 'com.env.game'},
        )
        self.assertEqual(res_env['env']['DM_BUNDLE_ID'], 'com.env.game')

    def test_custom_and_pinned_saved_bundle_identifiers_are_preserved(self):
        for saved in ('com.me.customgame', f'org.nxnative.{TID_LOWER}.OLDTEAM999'):
            with self.subTest(saved=saved):
                res = self.run_cli(
                    ['--prepare-xcode', '--team-id', 'NEWTEAM123'],
                    saved_signing={'PRODUCT_BUNDLE_IDENTIFIER': saved, 'DEVELOPMENT_TEAM': '0000000000'},
                )
                self.assertEqual(res['env']['DM_BUNDLE_ID'], saved)
                self.assertEqual(res['env']['DM_TEAM_ID'], 'NEWTEAM123')

    def test_bare_saved_bundle_id_upgrades_once_real_team_resolved(self):
        res = self.run_cli(
            ['--prepare-xcode', '--team-id', 'TEAM123456'],
            saved_signing={'PRODUCT_BUNDLE_IDENTIFIER': f'org.nxnative.{TID_LOWER}', 'DEVELOPMENT_TEAM': '0000000000'},
        )
        self.assertEqual(res['env']['DM_BUNDLE_ID'], f'org.nxnative.{TID_LOWER}.TEAM123456')
        self.assertEqual(res['env']['DM_TEAM_ID'], 'TEAM123456')

        res_saved_team = self.run_cli(
            ['--prepare-xcode'],
            saved_signing={'PRODUCT_BUNDLE_IDENTIFIER': f'org.nxnative.{TID_LOWER}', 'DEVELOPMENT_TEAM': 'SAVEDTEAM1'},
            security_side_effect=AssertionError('security must not run when saved DEVELOPMENT_TEAM is already real'),
        )
        self.assertEqual(res_saved_team['security_calls'], 0)
        self.assertEqual(res_saved_team['env']['DM_BUNDLE_ID'], f'org.nxnative.{TID_LOWER}.SAVEDTEAM1')
        self.assertEqual(res_saved_team['env']['DM_TEAM_ID'], 'SAVEDTEAM1')

    def test_prepare_xcode_detects_single_team_via_security_and_handles_ambiguous_or_error(self):
        single = [
            make_completed(0, '  1) AAAA "Apple Development: dev@example.com (CERT000001)"\n'),
            make_completed(0, 'PEM'),
            make_completed(0, 'subject=CN=x, OU=SOLE123456, O=Dev, C=US\n'),
        ]
        res_one = self.run_cli(['--prepare-xcode'], security_side_effect=single)
        self.assertEqual(res_one['env']['DM_TEAM_ID'], 'SOLE123456')
        self.assertEqual(res_one['env']['DM_BUNDLE_ID'], f'org.nxnative.{TID_LOWER}.SOLE123456')

        res_zero = self.run_cli(['--prepare-xcode'], security_side_effect=[make_completed(0, '     0 valid identities found\n')])
        self.assertEqual(res_zero['env']['DM_TEAM_ID'], '0000000000')
        self.assertEqual(res_zero['env']['DM_BUNDLE_ID'], f'org.nxnative.{TID_LOWER}')

        two = [
            make_completed(0, '  1) AAAA "Apple Development: a@example.com (C1)"\n  2) BBBB "Apple Development: b@example.com (C2)"\n'),
            make_completed(0, 'PEM1'),
            make_completed(0, 'subject=CN=a, OU=TEAM111111, O=A, C=US\n'),
            make_completed(0, 'PEM2'),
            make_completed(0, 'subject=CN=b, OU=TEAM222222, O=B, C=US\n'),
        ]
        res_two = self.run_cli(['--prepare-xcode'], security_side_effect=two)
        self.assertEqual(res_two['env']['DM_TEAM_ID'], '0000000000')
        self.assertEqual(res_two['env']['DM_BUNDLE_ID'], f'org.nxnative.{TID_LOWER}')

        res_timeout = self.run_cli(['--prepare-xcode'], security_side_effect=subprocess.TimeoutExpired(['security'], 5))
        self.assertEqual(res_timeout['env']['DM_TEAM_ID'], '0000000000')
        self.assertEqual(res_timeout['env']['DM_BUNDLE_ID'], f'org.nxnative.{TID_LOWER}')

    def test_unsigned_single_game_ipa_keeps_bare_bundle_id_and_skips_keychain_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_ipa = Path(tmp) / 'game.ipa'
            res = self.run_cli(
                ['--platform', 'ios', '--romfs', 'bundle', '-o', str(out_ipa)],
                security_side_effect=AssertionError('security must not run for unsigned generic IPA'),
            )
            self.assertEqual(res['security_calls'], 0)
            self.assertEqual(res['env']['DM_TEAM_ID'], '0000000000')
            self.assertEqual(res['env']['DM_BUNDLE_ID'], f'org.nxnative.{TID_LOWER}')

    def test_mac_and_shell_framework_are_unaffected_by_team_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_out = Path(tmp) / 'Fixture.app'
            res_mac_app = self.run_cli(
                ['--platform', 'mac-app', '--team-id', 'TEAM123456', '-o', str(app_out)],
                env_overrides={'DM_TEAM_ID': 'TEAM123456'},
            )
            self.assertEqual(res_mac_app['mac_app_bundle_id'], f'org.nxnative.{TID_LOWER}')

            res_mac = self.run_cli(
                ['--platform', 'mac', '--team-id', 'TEAM123456'],
                env_overrides={'DM_TEAM_ID': 'TEAM123456'},
                security_side_effect=AssertionError('security must not run for Mac platform'),
            )
            self.assertEqual(res_mac['security_calls'], 0)
            self.assertFalse(res_mac.get('build_ipa_called'))
            args_mac = SimpleNamespace(bundle_id=None, team_id='TEAM123456')
            self.assertEqual(
                build.resolve_bundle_id(int(GAME, 16), args_mac, {'DM_TEAM_ID': 'TEAM123456'}, {}, 'TEAM123456', mac=True),
                f'org.nxnative.{TID_LOWER}',
            )

            fw_dir = Path(tmp) / 'fw'
            res_fw = self.run_cli(
                ['--shell-framework', str(fw_dir), '--team-id', 'TEAM123456'],
                env_overrides={'DM_TEAM_ID': 'TEAM123456'},
            )
            self.assertEqual(res_fw['env']['DM_TEAM_ID'], 'TEAM123456')
            self.assertEqual(res_fw['env']['DM_BUNDLE_ID'], f'org.nxnative.{TID_LOWER}')


class BuildIpaShellDefaultsAndEntitlementsTests(unittest.TestCase):
    def run_bundle_block(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith('DM_')}
        env.update(GAME=GAME, **extra)
        script = (
            'set -u\n'
            'gp() {\n  printf "public.app-category.games\\n"\n}\n'
            'DM_DISPLAY_NAME="Fixture"\n'
            'DM_APP_VERSION="1.0"\n'
            + BUNDLE_BLOCK
            + '\nprintf "%s\\t%s\\n" "$DM_BUNDLE_ID" "$DM_TEAM_ID"\n'
        )
        result = subprocess.run(['/bin/bash', '-c', script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        bundle_id, team_id = result.stdout.strip().split('\t')
        return bundle_id, team_id

    def test_build_ipa_sh_default_bundle_id_with_and_without_team(self):
        self.assertEqual(self.run_bundle_block(DM_TEAM_ID='TEAM123456'), (f'org.nxnative.{TID_LOWER}.TEAM123456', 'TEAM123456'))
        self.assertEqual(self.run_bundle_block(DM_TEAM_ID='0000000000'), (f'org.nxnative.{TID_LOWER}', '0000000000'))
        self.assertEqual(self.run_bundle_block(), (f'org.nxnative.{TID_LOWER}', '0000000000'))
        self.assertEqual(self.run_bundle_block(DM_TEAM_ID=''), (f'org.nxnative.{TID_LOWER}', '0000000000'))
        self.assertEqual(self.run_bundle_block(DM_BUNDLE_ID='com.override.app', DM_TEAM_ID='TEAM123456'), ('com.override.app', 'TEAM123456'))
        self.assertEqual(self.run_bundle_block(FRAMEWORK_ONLY='/tmp/fw', DM_TEAM_ID='TEAM123456'), (f'org.nxnative.{TID_LOWER}', 'TEAM123456'))

    def test_prepare_xcode_entitlements_preserve_increased_memory_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'Gen').mkdir()
            (root / 'Gen/dm.xcconfig').touch()
            env = {k: v for k, v in os.environ.items() if not k.startswith('DM_')}
            env.update(PREPARE_XCODE='YES', DM_BUNDLE_ID=f'org.nxnative.{TID_LOWER}.TEAM123456')
            script = 'set -u\nt() {\n  true\n}\ndie() {\n  echo "$*" >&2\n  exit 1\n}\n' + PREPARE_ENTITLEMENTS
            result = subprocess.run(['/bin/bash', '-c', script], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entitlements = (root / 'Gen/DmIOS.entitlements').read_text()
            self.assertIn('com.apple.developer.kernel.increased-memory-limit', entitlements)
            self.assertIn('CODE_SIGN_ENTITLEMENTS = Gen/DmIOS.entitlements', (root / 'Gen/dm.xcconfig').read_text())

    @unittest.skipUnless(CAN_SIGN, 'needs macOS codesign and a C compiler')
    def test_adhoc_signing_preserves_increased_memory_limit_with_team_bundle_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'DmIOS.app').mkdir()
            (root / 'pkg').mkdir()
            (root / 'main.c').write_text('int main(void) { return 0; }\n')
            subprocess.run(['cc', '-o', str(root / 'DmIOS.app/DmIOS'), str(root / 'main.c')], check=True)
            env = {k: v for k, v in os.environ.items() if not k.startswith('DM_')}
            env.update(APP=str(root / 'DmIOS.app'), O=str(root / 'pkg'), DM_BUNDLE_ID=f'org.nxnative.{TID_LOWER}.TEAM123456')
            script = 'set -u\nt() {\n  true\n}\ndie() {\n  echo "$*" >&2\n  exit 1\n}\n' + SIGNING
            result = subprocess.run(['/bin/bash', '-c', script], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            shown = subprocess.run(['codesign', '-d', '--entitlements', '-', str(root / 'DmIOS.app/DmIOS')], capture_output=True, text=True)
            self.assertIn('com.apple.developer.kernel.increased-memory-limit', shown.stdout + shown.stderr)


if __name__ == '__main__':
    unittest.main()
