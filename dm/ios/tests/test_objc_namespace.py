"""Tests for gen_objc_namespace.py and dual-framework dlopen isolation in one process."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = Path(__file__).resolve().parents[3]
TOOLS_DIR = REPO / 'dm/ios/tools'
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import gen_objc_namespace as ns  # noqa: E402


class ObjCNamespaceScannerTests(unittest.TestCase):
    def test_real_engine_scan_finds_owned_classes_and_excludes_system_mac_and_app_classes(self):
        dm = REPO / 'dm'
        roots = ns.default_source_roots(dm)
        classes = set(ns.scan_objc_classes(roots))
        expected_subset = {
            'DmRootVC',
            'DmRootView',
            'DmGameView',
            'DmGamepad',
            'DmMenuButton',
            'DmCtrlPrompt',
            'DmTVVC',
            'DmTVSceneDelegate',
            'DmNotifyDelegate',
            'DmQuitView',
            'DmRumbleChannel',
            'DmDs4Channel',
            'S2Command',
            'S2Session',
            'S2Central',
            'DmWebBox',
            'DmWebRelay',
            'NvShader',
            'NvProg',
            'NvArena',
            'DmRecTable',
            'DmRecEnc',
            'DmRecCB',
        }
        self.assertTrue(expected_subset.issubset(classes), f'missing expected classes: {expected_subset - classes}')

        forbidden = {
            'NSObject',
            'UIView',
            'UIViewController',
            'UIResponder',
            'GCController',
            'App',
            'SceneDelegate',
            'DMController',
            'DmWinDelegate',
            'DmCheatsPanel',
            'HqScrim',
            'TestController',
            'NamedSteamController',
        }
        self.assertEqual(classes & forbidden, set(), f'unexpected classes matched: {classes & forbidden}')

        # Cross-check that every @interface <Cls> : <Super> in the same non-test engine files is covered.
        declared_all: set[str] = set()
        for src in ns.iter_source_files(roots):
            _, declared = ns.extract_classes_from_text(src.read_text(encoding='utf-8', errors='replace'))
            declared_all.update(declared)
        self.assertEqual(classes, declared_all)

    def test_categories_comments_and_string_literals_are_ignored(self):
        with tempfile.TemporaryDirectory(prefix='test_ns_scan_') as tmp:
            root = Path(tmp)
            sample = root / 'Sample.m'
            sample.write_text(
                textwrap.dedent(
                    """\
                    // @implementation FakeLineComment
                    /*
                     @implementation FakeBlockComment
                    */
                    static const char *kStr = "@implementation FakeStringLiteral";
                    @interface RealOwnedClass : NSObject
                    @end
                    @interface RealOwnedClass ()
                    @end
                    @implementation RealOwnedClass
                    @end
                    @implementation GCController (HIDCategory)
                    @end
                    @implementation UIView
                        (MultilineCategory)
                    @end
                    """
                ),
                encoding='utf-8',
            )
            mac_only = root / 'only_mac.m'
            mac_only.write_text('@implementation MacOnlyClass\n@end\n', encoding='utf-8')
            sources_dir = root / 'Sources'
            sources_dir.mkdir()
            (sources_dir / 'main.m').write_text('@implementation App\n@end\n', encoding='utf-8')
            tests_dir = root / 'tests'
            tests_dir.mkdir()
            (tests_dir / 't_mock.m').write_text('@implementation MockTestClass\n@end\n', encoding='utf-8')

            found = ns.scan_objc_classes([root])
            self.assertEqual(found, ['RealOwnedClass'])

    def test_raw_string_with_unbalanced_quotes_does_not_mask_following_classes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'web.m').write_text(
                textwrap.dedent(
                    r"""
                    static NSString *const kScript = @R"JS(
                        const re = /^(['"])(.*)\1$/;
                        const s = "odd quote inside raw literal;
                        const fake = "@implementation FakeInsideRaw";
                    )JS";

                    @interface RawFollowedClass : NSObject
                    @end
                    @implementation RawFollowedClass
                    @end
                    """
                ),
                encoding='utf-8',
            )
            found = ns.scan_objc_classes([root])
            self.assertEqual(found, ['RawFollowedClass'])

    def test_namespace_tags_distinguish_titles_and_chained_frameworks_and_reject_unsafe_input(self):
        t_a = ns.make_namespace_tag(game='010015100b514000', framework_name='DmGame010015100B514000')
        t_b = ns.make_namespace_tag(game='0100535012974000', framework_name='DmGame0100535012974000')
        t_c1_fw = ns.make_namespace_tag(game='010015100B514000', framework_name='DmGame010015100B514000_1')
        t_c1_idx = ns.make_namespace_tag(game='010015100B514000', chain_index='1')
        t_c2_idx = ns.make_namespace_tag(game='010015100B514000', chain_index='2')

        self.assertEqual(t_a, '010015100B514000')
        self.assertEqual(t_b, '0100535012974000')
        self.assertEqual(t_c1_fw, '010015100B514000_1')
        self.assertEqual(t_c1_idx, '010015100B514000_c1')
        self.assertEqual(t_c2_idx, '010015100B514000_c2')
        self.assertEqual(len({t_a, t_b, t_c1_fw, t_c1_idx, t_c2_idx}), 5)

        for kwargs in (
            {'tag': 'bad-tag'},
            {'tag': 'bad space'},
            {'tag': 'bad$tag'},
            {'game': '../0100'},
            {'game': '0100', 'framework_name': 'DmGame-bad'},
            {'game': '0100', 'chain_index': '1/2'},
            {},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ns.make_namespace_tag(**kwargs)

    def test_exported_symbols_match_shell_dlsym_contract_and_survive_urls_in_strings(self):
        symbols = ns.collect_exported_symbols(REPO / 'dm/ios')
        self.assertEqual(
            symbols,
            [
                '_dmgame_image_marker',
                '_dm_glue_version',
                '_dm_glue_start_game',
                '_dm_glue_present_game',
                '_dm_glue_focus_event',
            ],
        )
        with tempfile.TemporaryDirectory(prefix='test_ns_exports_') as tmp:
            fake_ios = Path(tmp)
            (fake_ios / 'Sources').mkdir()
            (fake_ios / 'Shell').mkdir()
            (fake_ios / 'Sources/FrameworkBootstrap.h').write_text(
                '/* #define DM_FRAMEWORK_MARKER "ignored_comment_marker" */\n'
                '#define DM_FRAMEWORK_MARKER "dmgame_image_marker"\n',
                encoding='utf-8',
            )
            (fake_ios / 'Shell/ShellMain.m').write_text(
                'void f(void *h) {\n'
                '    const char *u = "https://example.invalid/path"; void *p = dlsym(h, "dm_glue_extra_hook");\n'
                '    (void)u; (void)p;\n'
                '}\n',
                encoding='utf-8',
            )
            extra = ns.collect_exported_symbols(fake_ios)
            self.assertIn('_dm_glue_extra_hook', extra)
            self.assertNotIn('_ignored_comment_marker', extra)


@unittest.skipUnless(
    sys.platform == 'darwin' and shutil.which('clang') and shutil.which('swiftc') and shutil.which('nm'),
    'needs macOS clang, swiftc and nm',
)
class DualFrameworkDlopenIsolationTests(unittest.TestCase):
    """Compile two dylibs sharing identical ObjC and Swift source names and dlopen both in one process."""

    # Mirrors DmGameMenu.h: pure C functions bridged between Objective-C and Swift.
    BRIDGE_H = """\
    #include <stdbool.h>
    int dm_pad_layout(void);
    void *dm_game_menu(void);
    """

    MODULE_M = """\
    #import <Foundation/Foundation.h>
    #import <objc/runtime.h>
    #include "DmGameMenu.h"

    static int g_token_value = FRAMEWORK_TOKEN;
    static char g_report[512];

    @interface DmTVSceneDelegate : NSObject
    + (int)delegateToken;
    @end

    @interface NvShader : NSObject
    - (int)shaderSeed;
    @end
    @implementation NvShader
    - (int)shaderSeed { return g_token_value + 7; }
    @end

    @interface DmRootVC : NSObject
    + (int)frameworkToken;
    + (Class)tvDelegateClass;
    @end
    @implementation DmRootVC
    + (int)frameworkToken {
        NvShader *sh = [NvShader new];
        return [sh shaderSeed] - 7;
    }
    + (Class)tvDelegateClass {
        return DmTVSceneDelegate.class;
    }
    @end

    @implementation DmTVSceneDelegate
    + (int)delegateToken { return g_token_value; }
    @end

    int dm_pad_layout(void) {
        Class rtCls = objc_getClass(class_getName([DmRootVC class]));
        return [(id)rtCls frameworkToken];
    }

    int internal_helper_secret(void) {
        return g_token_value * 10;
    }

    __attribute__((used, visibility("default")))
    const char dmgame_image_marker[] = "DmGame";

    __attribute__((used, visibility("default")))
    const int dm_glue_version = 1;

    __attribute__((used, visibility("default")))
    const char *dm_glue_start_game(const char *const *argv, int argc, const void *fbase) {
        (void)argv;
        (void)argc;
        (void)fbase;
        NSString *swiftInfo = CFBridgingRelease(dm_game_menu());
        NSString *rootName = NSStringFromClass([DmRootVC class]);
        Class tvCls = [DmRootVC tvDelegateClass];
        Class rtTvCls = objc_getClass(class_getName(tvCls));
        int tvTok = [(id)rtTvCls delegateToken];
        snprintf(g_report, sizeof g_report, "%s|%s|%s|%d",
                 rootName.UTF8String, NSStringFromClass(rtTvCls).UTF8String,
                 swiftInfo.UTF8String, tvTok);
        return g_report;
    }

    __attribute__((used, visibility("default")))
    void dm_glue_present_game(void *window) {
        (void)window;
    }

    __attribute__((used, visibility("default")))
    void dm_glue_focus_event(int kind) {
        (void)kind;
    }
    """

    MODULE_SWIFT = """\
    import Foundation

    public final class MenuBridgeObject: NSObject {
        public func tokenString() -> String {
            return String(dm_pad_layout())
        }
    }

    public final class LauncherModel {
        public init() {}
        public func tag() -> String {
            let helper = MenuBridgeObject()
            return String(reflecting: type(of: self)) + ":" + helper.tokenString()
        }
    }

    @_cdecl("dm_game_menu")
    public func dmGameMenu() -> UnsafeMutableRawPointer {
        let model = LauncherModel()
        let text = model.tag() as NSString
        return Unmanaged.passRetained(text).toOpaque()
    }
    """

    HOST_C = """\
    #include <dlfcn.h>
    #include <stdio.h>
    #include <stdlib.h>
    #include <string.h>

    typedef const char *(*start_fn)(const char *const *, int, const void *);
    typedef void (*present_fn)(void *);
    typedef void (*focus_fn)(int);

    static int check_one(void *h, const char *path, int check_hidden, char *out, size_t out_sz) {
        const void *marker = dlsym(h, "dmgame_image_marker");
        const int *ver = (const int *)dlsym(h, "dm_glue_version");
        start_fn start = (start_fn)dlsym(h, "dm_glue_start_game");
        present_fn present = (present_fn)dlsym(h, "dm_glue_present_game");
        focus_fn focus = (focus_fn)dlsym(h, "dm_glue_focus_event");
        if (!marker || !ver || *ver != 1 || !start || !present || !focus) {
            fprintf(stderr, "missing required glue symbol in %s\\n", path);
            return 3;
        }
        if (check_hidden) {
            void *leaked_c = dlsym(h, "internal_helper_secret");
            void *leaked_pad = dlsym(h, "dm_pad_layout");
            void *leaked_swift = dlsym(h, "dm_game_menu");
            if (leaked_c != NULL || leaked_pad != NULL || leaked_swift != NULL) {
                fprintf(stderr, "internal symbol leaked in %s\\n", path);
                return 4;
            }
        }
        const char *res = start(NULL, 0, NULL);
        snprintf(out, out_sz, "%s", res ? res : "");
        return 0;
    }

    int main(int argc, char **argv) {
        if (argc != 4) return 1;
        int check_hidden = atoi(argv[1]);
        void *ha = dlopen(argv[2], RTLD_LAZY | RTLD_LOCAL);
        void *hb = dlopen(argv[3], RTLD_LAZY | RTLD_LOCAL);
        if (!ha || !hb) {
            fprintf(stderr, "dlopen failed: %s\\n", dlerror());
            return 2;
        }
        char rep_a[512];
        char rep_b[512];
        int rc_a = check_one(ha, argv[2], check_hidden, rep_a, sizeof rep_a);
        if (rc_a != 0) return rc_a;
        int rc_b = check_one(hb, argv[3], check_hidden, rep_b, sizeof rep_b);
        if (rc_b != 0) return rc_b;
        printf("A=%s\\nB=%s\\n", rep_a, rep_b);
        return 0;
    }
    """

    def _build_dylib(self, root: Path, name: str, tag: str, token: int, namespaced: bool) -> Path:
        work = root / name
        work.mkdir()
        src_dir = work / 'Sources'
        src_dir.mkdir()
        bridge_h = src_dir / 'DmGameMenu.h'
        module_m = src_dir / 'module.m'
        module_swift = src_dir / 'GameMenu.swift'
        bridge_h.write_text(textwrap.dedent(self.BRIDGE_H), encoding='utf-8')
        module_m.write_text(textwrap.dedent(self.MODULE_M), encoding='utf-8')
        module_swift.write_text(textwrap.dedent(self.MODULE_SWIFT), encoding='utf-8')

        ns_h = work / 'objc_namespace.h'
        exp_txt = work / 'exported_symbols.txt'
        cmd = [
            sys.executable,
            str(TOOLS_DIR / 'gen_objc_namespace.py'),
            '--tag',
            tag,
            '--header',
            str(ns_h),
            '--exports',
            str(exp_txt),
            str(src_dir),
        ]
        gen = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(gen.returncode, 0, gen.stderr)

        objc_o = work / 'module_objc.o'
        cflags = [
            'clang',
            '-fobjc-arc',
            '-O1',
            f'-DFRAMEWORK_TOKEN={token}',
            f'-I{src_dir}',
        ]
        if namespaced:
            cflags += ['-include', str(ns_h)]
        cflags += ['-c', str(module_m), '-o', str(objc_o)]
        r_objc = subprocess.run(cflags, capture_output=True, text=True)
        self.assertEqual(r_objc.returncode, 0, r_objc.stderr)

        swift_o = work / 'module_swift.o'
        mod_name = f'DmGame_{tag}' if namespaced else 'DmGameShared'
        sflags = [
            'swiftc',
            '-O',
            '-parse-as-library',
            '-module-name',
            mod_name,
            '-import-objc-header',
            str(bridge_h),
        ]
        if namespaced:
            sflags += ['-Xcc', '-include', '-Xcc', str(ns_h)]
        sflags += ['-c', str(module_swift), '-o', str(swift_o)]
        r_swift = subprocess.run(sflags, capture_output=True, text=True)
        self.assertEqual(r_swift.returncode, 0, r_swift.stderr)

        dylib = work / f'lib{name}.dylib'
        ldflags = [
            'clang',
            '-dynamiclib',
            '-fobjc-arc',
            str(objc_o),
            str(swift_o),
            '-framework',
            'Foundation',
            '-L/usr/lib/swift',
        ]
        if namespaced:
            ldflags += [f'-Wl,-exported_symbols_list,{exp_txt}']
        ldflags += ['-o', str(dylib)]
        r_link = subprocess.run(ldflags, capture_output=True, text=True)
        self.assertEqual(r_link.returncode, 0, r_link.stderr)
        return dylib

    def _build_host(self, root: Path) -> Path:
        host_c = root / 'host.c'
        host_bin = root / 'host'
        host_c.write_text(textwrap.dedent(self.HOST_C), encoding='utf-8')
        r_host = subprocess.run(['clang', '-O1', '-Wall', '-Werror', str(host_c), '-o', str(host_bin)],
                                capture_output=True, text=True)
        self.assertEqual(r_host.returncode, 0, r_host.stderr)
        return host_bin

    def test_namespaced_dylibs_coexist_without_objc_warnings_cross_talk_or_leaked_exports(self):
        root = Path(tempfile.mkdtemp(prefix='test_dlopen_ns_')).resolve()
        self.addCleanup(shutil.rmtree, root)

        dylib_a = self._build_dylib(root, 'fwA', '010015100B514000', 111, namespaced=True)
        dylib_b = self._build_dylib(root, 'fwB', '010015100B514000_c1', 222, namespaced=True)

        expected_exports = {
            '_dmgame_image_marker',
            '_dm_glue_version',
            '_dm_glue_start_game',
            '_dm_glue_present_game',
            '_dm_glue_focus_event',
        }
        for dylib in (dylib_a, dylib_b):
            nm_out = subprocess.run(['nm', '-gU', str(dylib)], capture_output=True, text=True, check=True).stdout
            got = {ln.split()[-1] for ln in nm_out.splitlines() if ln.strip()}
            self.assertEqual(got, expected_exports)

        host_bin = self._build_host(root)
        run_res = subprocess.run([str(host_bin), '1', str(dylib_a), str(dylib_b)], capture_output=True, text=True)
        self.assertEqual(run_res.returncode, 0, run_res.stderr)
        self.assertNotIn('is implemented in both', run_res.stderr)
        self.assertEqual(run_res.stderr.strip(), '')
        lines = dict(ln.split('=', 1) for ln in run_res.stdout.strip().splitlines())
        self.assertEqual(
            lines['A'],
            'DmFw_010015100B514000_DmRootVC|'
            'DmFw_010015100B514000_DmTVSceneDelegate|'
            'DmGame_010015100B514000.LauncherModel:111|111',
        )
        self.assertEqual(
            lines['B'],
            'DmFw_010015100B514000_c1_DmRootVC|'
            'DmFw_010015100B514000_c1_DmTVSceneDelegate|'
            'DmGame_010015100B514000_c1.LauncherModel:222|222',
        )

    def test_unnamespaced_control_reproduces_duplicate_class_warning_and_objc_runtime_cross_talk(self):
        root = Path(tempfile.mkdtemp(prefix='test_dlopen_ctrl_')).resolve()
        self.addCleanup(shutil.rmtree, root)

        dylib_a = self._build_dylib(root, 'rawA', '0100AAAA', 111, namespaced=False)
        dylib_b = self._build_dylib(root, 'rawB', '0100BBBB', 222, namespaced=False)

        host_bin = self._build_host(root)
        res = subprocess.run([str(host_bin), '0', str(dylib_a), str(dylib_b)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('is implemented in both', res.stderr)
        # Without namespacing, objc_getClass resolves Framework B's classes to Framework A's definitions (111).
        lines = dict(ln.split('=', 1) for ln in res.stdout.strip().splitlines())
        self.assertEqual(lines['B'], 'DmRootVC|DmTVSceneDelegate|DmGameShared.LauncherModel:111|111')


if __name__ == '__main__':
    unittest.main()
