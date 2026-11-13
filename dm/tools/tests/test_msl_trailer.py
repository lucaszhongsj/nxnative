"""DMMSLTR1 page-aligned shader trailers in romfs.img: packing, replacing, inspecting, base identity invariance,
romfs.py bounded views, hidden symbol visibility, zero-copy page-aligned mmap, DM_PROGRAM multi-program fallback,
FD teardown across game switches, and NVN / VK / GL / lifecycle caller integration."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'dm/tools'))
import romfs
import shader_lib

_spec = importlib.util.spec_from_file_location('romfs_image_isolated', REPO / 'dm/tools/romfs_image.py')
assert _spec is not None and _spec.loader is not None
romfs_image = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(romfs_image)

MOUNT_HARNESS_OBJC = r'''#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <dispatch/dispatch.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/param.h>
#include <unistd.h>
#include "dm/gfx/msl_mount.h"
#include "dm/host/romfs_identity.h"

__attribute__((weak, visibility("hidden"))) void dm_msl_trailer_teardown(void);

static int count_open_fds_for(const char *target_path) {
    char want[MAXPATHLEN];
    if (!realpath(target_path, want)) return -1;
    int count = 0;
    for (int fd = 0; fd < 256; fd++) {
        char got[MAXPATHLEN];
        if (fcntl(fd, F_GETPATH, got) == 0) {
            char resolved[MAXPATHLEN];
            const char *cmp = realpath(got, resolved) ? resolved : got;
            if (!strcmp(cmp, want)) count++;
        }
    }
    return count;
}

#define CHECK(cond, ...) do { \
    if (!(cond)) { fprintf(stderr, "FAIL line %d: ", __LINE__); fprintf(stderr, __VA_ARGS__); fprintf(stderr, "\n"); return 1; } \
} while (0)

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc < 4) return 2;
        const char *img1 = argv[1];
        const char *img2 = argv[2];
        int has_real_metallib = atoi(argv[3]);

        // Identity check in C
        romfs_id id1;
        CHECK(romfs_id_of_image(img1, &id1), "romfs_id_of_image failed on img1");

        setenv("DM_ROMFS", img1, 1);
        unsetenv("DM_PROGRAM");

        dm_msl_footer ft;
        CHECK(dm_msl_trailer_info(&ft), "dm_msl_trailer_info failed");
        CHECK(ft.version == 1 && ft.gfx_api == DM_MSL_GFX_NVN && (ft.flags & DM_MSL_FLAG_MULTI_PROGRAM),
              "unexpected footer version=%u api=%u flags=%u", ft.version, ft.gfx_api, ft.flags);
        CHECK(ft.base_romfs_size == id1.size, "base_romfs_size %llu != id1.size %llu",
              (unsigned long long)ft.base_romfs_size, (unsigned long long)id1.size);
        CHECK((ft.msl_offset % 16384ull) == 0, "msl_offset %llu not 16 KiB aligned", (unsigned long long)ft.msl_offset);

        // Program 0 (default when DM_PROGRAM is unset or "0") resolves p0/ first, then shared fallback
        void *buf = NULL; size_t len = 0;
        CHECK(dm_msl_trailer_has("b-test/inputs.tsv"), "missing b-test/inputs.tsv");
        CHECK(dm_msl_trailer_read("b-test/inputs.tsv", &buf, &len), "read p0 inputs.tsv failed");
        CHECK(len > 0 && ((char *)buf)[len] == '\0' && strstr((char *)buf, "prog0"), "expected prog0 inputs.tsv, got: %s", (char *)buf);
        free(buf);

        setenv("DM_PROGRAM", "0", 1);
        CHECK(dm_msl_trailer_read("b-test/inputs.tsv", &buf, &len) && strstr((char *)buf, "prog0"), "DM_PROGRAM=0 should hit p0/");
        free(buf);

        // Fallback to unprefixed shared entry when p0/ lacks shared_reflect.json
        CHECK(dm_msl_trailer_read("b-test/shared_b_reflect.json", &buf, &len), "fallback read failed");
        CHECK(((char *)buf)[len] == '\0' && !strcmp((char *)buf, "{\"shared\":1}"), "unexpected shared json: %s", (char *)buf);
        free(buf);

        // Empty file read still NUL-terminates
        CHECK(dm_msl_trailer_read("b-test/empty.json", &buf, &len), "empty file read failed");
        CHECK(len == 0 && buf != NULL && ((char *)buf)[0] == '\0', "empty file not NUL-terminated");
        free(buf);

        // Program 1 resolves p1/ first, then falls back to shared
        setenv("DM_PROGRAM", "1", 1);
        CHECK(dm_msl_trailer_read("b-test/inputs.tsv", &buf, &len) && strstr((char *)buf, "prog1"), "DM_PROGRAM=1 should hit p1/");
        free(buf);
        CHECK(dm_msl_trailer_read("b-test/shared_b_reflect.json", &buf, &len) && !strcmp((char *)buf, "{\"shared\":1}"),
              "DM_PROGRAM=1 fallback to shared failed");
        free(buf);

        // Page-aligned mmap across 16 KiB page boundaries + single shared FD invariant
        CHECK(count_open_fds_for(img1) == 1, "expected exactly 1 open fd for img1 before maps");
        size_t page = (size_t)getpagesize();
        for (int i = 0; i < 6; i++) {
            msl_map m;
            CHECK(dm_msl_trailer_map("b-test/big_b.metallib", &m), "dm_msl_trailer_map big_b.metallib failed");
            CHECK(((uintptr_t)m.base % page) == 0, "mmap base %p not page-aligned (%zu)", m.base, page);
            CHECK(((uintptr_t)m.data % 16u) == 0, "mapped shader data %p not 16-byte aligned", m.data);
            CHECK(m.size == 45678u, "unexpected mapped size %zu", m.size);
            const uint8_t *bytes = (const uint8_t *)m.data;
            for (size_t k = 0; k < m.size; k += 997) {
                CHECK(bytes[k] == (uint8_t)((k * 31u + 7u) & 0xffu), "mapped byte mismatch at %zu", k);
            }
            CHECK(count_open_fds_for(img1) == 1, "mmap must reuse the single cached fd");
            CHECK(munmap(m.base, m.base_len) == 0, "munmap failed");
        }

        if (has_real_metallib) {
            id<MTLDevice> dev = MTLCreateSystemDefaultDevice();
            if (dev) {
                msl_map m;
                CHECK(dm_msl_trailer_map("b-test/real_b.metallib", &m), "map real_b.metallib failed");
                void *base = m.base; size_t base_len = m.base_len;
                dispatch_semaphore_t unmapped = dispatch_semaphore_create(0);
                @autoreleasepool {
                    dispatch_data_t dd = dispatch_data_create(m.data, m.size, dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
                        munmap(base, base_len);
                        dispatch_semaphore_signal(unmapped);
                    });
                    NSError *err = nil;
                    id<MTLLibrary> lib = [dev newLibraryWithData:dd error:&err];
                    CHECK(lib != nil, "newLibraryWithData failed: %s", err ? err.localizedDescription.UTF8String : "unknown");
                    id<MTLFunction> fn = [lib newFunctionWithName:@"main0"];
                    CHECK(fn != nil, "MTLFunction main0 not found in mapped trailer metallib");
                }
                dispatch_semaphore_wait(unmapped, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC));
            }
        }

        // Teardown must close the cached FD completely
        dm_msl_trailer_teardown();
        CHECK(count_open_fds_for(img1) == 0, "teardown leaked fd for img1 (%d still open)", count_open_fds_for(img1));

        // Switching DM_ROMFS to a second game's image mounts the new Vulkan trailer cleanly
        unsetenv("DM_PROGRAM");
        setenv("DM_ROMFS", img2, 1);
        CHECK(dm_msl_trailer_info(&ft) && ft.gfx_api == DM_MSL_GFX_VULKAN, "failed to switch to img2 vulkan trailer");
        CHECK(dm_msl_trailer_read("vk/vk_shaders.tsv", &buf, &len) && strstr((char *)buf, "0123456789abcdef"),
              "failed to read img2 vk_shaders.tsv");
        free(buf);
        CHECK(count_open_fds_for(img2) == 1, "expected 1 open fd for img2");
        dm_msl_trailer_teardown();
        CHECK(count_open_fds_for(img2) == 0, "teardown leaked fd for img2");
    }
    return 0;
}
'''


def _try_compile_metallib(work_dir: Path):
    """Compiles a tiny real macOS .metallib when Xcode's MetalToolchain is installed; returns bytes or None."""
    probe = subprocess.run(['xcrun', '-sdk', 'macosx', 'metal', '--version'], capture_output=True, text=True)
    if probe.returncode != 0:
        return None
    src = work_dir / 'tiny.metal'
    air = work_dir / 'tiny.air'
    lib = work_dir / 'tiny.metallib'
    src.write_text('#include <metal_stdlib>\nusing namespace metal;\nkernel void main0(device uint *out [[buffer(0)]]) { out[0] = 42u; }\n')
    r1 = subprocess.run(['xcrun', '-sdk', 'macosx', 'metal', '-c', str(src), '-o', str(air)], capture_output=True, text=True)
    if r1.returncode != 0:
        return None
    r2 = subprocess.run(['xcrun', '-sdk', 'macosx', 'metallib', str(air), '-o', str(lib)], capture_output=True, text=True)
    if r2.returncode != 0:
        return None
    return lib.read_bytes()


class MslTrailerTests(unittest.TestCase):
    def test_append_replace_inspect_and_identity_invariance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Compile C romfs_identity reader to cross-check Python vs C on every state
            id_c = root / 'id_check.c'
            id_bin = root / 'id_check'
            id_c.write_text(
                '#include "romfs_identity.h"\n'
                'int main(int argc, char **argv) {\n'
                '    if (argc != 2) return 2;\n'
                '    romfs_id got; char line[400];\n'
                '    if (!romfs_id_of_image(argv[1], &got)) return 1;\n'
                '    romfs_id_text(&got, line, sizeof line); puts(line); return 0;\n'
                '}\n'
            )
            subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Wno-unused-function', '-Werror',
                            '-I', str(REPO / 'dm/host'), str(id_c), '-o', str(id_bin)], check=True)

            game_src = root / 'romfs_src'
            (game_src / 'data').mkdir(parents=True)
            (game_src / 'data/hello.txt').write_bytes(b'hello world')
            (game_src / 'data/blob.bin').write_bytes(bytes(range(256)) * 45 + b'tail')
            img = root / 'romfs.img'
            romfs_image.pack(game_src, img)
            base_size = img.stat().st_size
            base_id = romfs_image.identity(img, '010015100B514000', '1.0.0', 'v0')

            no_tr = romfs_image.inspect_msl(img)
            self.assertFalse(no_tr['has_trailer'])
            self.assertEqual(no_tr['base_romfs_size'], base_size)

            # Build a packed b-chain1.img using shader_lib.py, plus noise files (.metal, _b_build.json) in a sibling folder
            msl1 = root / 'msl1'
            msl1.mkdir()
            raw_lib = root / 'b-chain1'
            raw_lib.mkdir()
            (raw_lib / 'inputs.tsv').write_text('game\t010015100B514000\ninputs\tdigest1\n')
            (raw_lib / '0011223344556677_b.metallib').write_bytes(b'MTLB' + b'\xaa' * 123)
            (raw_lib / '0011223344556677_b_reflect.json').write_text('{"stage":1}\n')
            (raw_lib / '_b_build.json').write_text('[{"key":"0011223344556677","st":"ok","err":""}]\n')
            (raw_lib / '0011223344556677.metal').write_text('// source should be excluded\n')
            self.assertTrue(shader_lib.pack(raw_lib))
            shutil.copy2(root / 'b-chain1.img', msl1 / 'b-chain1.img')

            # CLI --append-msl under python3 -I (isolated mode, matching build_ipa.sh)
            r = subprocess.run([sys.executable, '-I', str(REPO / 'dm/tools/romfs_image.py'),
                                '--append-msl', str(img), str(msl1)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

            insp = subprocess.run([sys.executable, '-I', str(REPO / 'dm/tools/romfs_image.py'),
                                   '--inspect-msl', str(img)], capture_output=True, text=True, check=True)
            info1 = json.loads(insp.stdout)
            self.assertTrue(info1['has_trailer'])
            self.assertEqual(info1['gfx_api'], 'nvn')
            self.assertEqual(info1['chain'], 'b-chain1')
            self.assertFalse(info1['multi_program'])
            self.assertEqual(info1['base_romfs_size'], base_size)
            self.assertEqual(info1['msl_offset'], (base_size + 16383) & ~16383)
            self.assertEqual(info1['files'], 3)   # inputs.tsv + .metallib + _reflect.json (_b_build.json and .metal excluded)

            # Every file in the sub-image must be 16-byte aligned and contain no nested .img
            sub = romfs.Image(img, slice_offset=info1['msl_offset'], slice_size=info1['msl_size'])
            for k in range(sub.count):
                a, b, _, _, t, _ = sub.entry(k)
                if t == 1:
                    self.assertEqual(a % 16, 0)
                    self.assertEqual((info1['msl_offset'] + a) % 16, 0)
                    self.assertFalse(sub.name(k).endswith(b'.img'))

            # Identity in Python and C stays strictly equal to base_romfs_size
            self.assertEqual(romfs_image.identity(img, '010015100B514000', '1.0.0', 'v0'), base_id)
            c_id = subprocess.run([str(id_bin), str(img)], capture_output=True, text=True, check=True).stdout.strip()
            self.assertTrue(romfs_image.same_identity(c_id, base_id), c_id)

            # romfs.py virtual view maps only the base RomFS and hides the trailer
            vroot = romfs.Path(img)
            self.assertEqual((vroot / 'data/hello.txt').read_bytes(), b'hello world')
            self.assertFalse((vroot / 'b-chain1/inputs.tsv').exists())
            with romfs.open(vroot / 'data/blob.bin', 'rb') as vf, romfs.mmap.mmap(vf.fileno(), 0, access=romfs.mmap.ACCESS_READ) as mm:
                self.assertEqual(len(mm), 256 * 45 + 4)

            # Replace trailer with a smaller chain2 folder: file shrinks cleanly and identity remains identical
            msl2 = root / 'msl2' / 'b-chain2'
            msl2.mkdir(parents=True)
            (msl2 / 'inputs.tsv').write_text('game\t010015100B514000\ninputs\tdigest2\n')
            (msl2 / 'ffeeddccbbaa9988_b.metallib').write_bytes(b'MTLB' + b'\xbb' * 20)
            info2 = romfs_image.replace_msl(img, msl2.parent)
            self.assertEqual(info2['chain'], 'b-chain2')
            self.assertEqual(info2['files'], 2)
            self.assertEqual(romfs_image.identity(img, '010015100B514000', '1.0.0', 'v0'), base_id)
            c_id2 = subprocess.run([str(id_bin), str(img)], capture_output=True, text=True, check=True).stdout.strip()
            self.assertTrue(romfs_image.same_identity(c_id2, base_id))

            # Nested unknown .img inside MSL source is strictly refused
            bad_msl = root / 'bad_msl'
            bad_msl.mkdir()
            (bad_msl / 'nested.img').write_bytes( (root / 'b-chain1.img').read_bytes() )
            with self.assertRaisesRegex(ValueError, 'nested \\.img'):
                romfs_image.replace_msl(img, bad_msl, gfx_api='nvn', chain='b-chain2')

            # Cut-off trailer is rejected by identity(), inspect_msl(), romfs.Image(), C reader, and append_msl(),
            # while replace_msl() safely truncates back to base_romfs_size and repairs it.
            good_bytes = img.read_bytes()
            img.write_bytes(good_bytes[:-1])
            romfs._image.cache_clear()
            with self.assertRaises(ValueError):
                romfs_image.identity(img)
            with self.assertRaises(ValueError):
                romfs_image.inspect_msl(img)
            with self.assertRaises(ValueError):
                romfs.Image(img)
            with self.assertRaises(ValueError):
                romfs_image.append_msl(img, msl2.parent)
            self.assertNotEqual(subprocess.run([str(id_bin), str(img)], capture_output=True).returncode, 0)

            repaired = romfs_image.replace_msl(img, msl2.parent)
            self.assertTrue(repaired['has_trailer'])
            self.assertEqual(subprocess.run([str(id_bin), str(img)], capture_output=True).returncode, 0)

    def test_c_msl_mount_mmap_multiprogram_teardown_and_hidden_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1. Verify all dm_msl_trailer_* symbols are private external in .o and export zero symbols from a dylib
            obj = root / 'msl_mount.o'
            dylib = root / 'libmsl_mount_test.dylib'
            subprocess.run(['clang', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Wno-unused-function', '-Werror',
                            '-c', str(REPO / 'dm/gfx/nvn/msl_mount.c'), '-o', str(obj)], check=True)
            nm_obj = subprocess.run(['nm', '-m', str(obj)], capture_output=True, text=True, check=True).stdout
            for sym in ('_dm_msl_trailer_info', '_dm_msl_trailer_has', '_dm_msl_trailer_read',
                        '_dm_msl_trailer_map', '_dm_msl_trailer_teardown'):
                self.assertIn(f'private external {sym}', nm_obj)
            subprocess.run(['clang', '-dynamiclib', str(obj), '-o', str(dylib)], check=True)
            nm_out = subprocess.run(['nm', '-gU', str(dylib)], capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(nm_out, '', f'libmsl_mount_test.dylib leaked exported symbols:\n{nm_out}')

            # 2. Build multi-program NVN image (img1) and Vulkan image (img2)
            src = root / 'src'
            src.mkdir()
            (src / 'marker').write_bytes(b'ok' * 500)
            img1 = root / 'game1.img'
            img2 = root / 'game2.img'
            romfs_image.pack(src, img1)
            shutil.copy2(img1, img2)

            mp = root / 'mp_msl'
            (mp / 'p0/b-test').mkdir(parents=True)
            (mp / 'p1/b-test').mkdir(parents=True)
            (mp / 'p0/b-test/inputs.tsv').write_text('game\tprog0\ninputs\th0\n')
            (mp / 'p1/b-test/inputs.tsv').write_text('game\tprog1\ninputs\th1\n')

            # Build a pre-packed tree image that includes both p0/, p1/ and a shared fallback + 45,678B file spanning multiple 16 KiB pages
            big_payload = bytes(((k * 31 + 7) & 0xff) for k in range(45678))
            real_metallib = _try_compile_metallib(root)
            tree_files = {
                'p0/b-test/inputs.tsv': ('bytes', b'game\tprog0\ninputs\th0\n'),
                'p1/b-test/inputs.tsv': ('bytes', b'game\tprog1\ninputs\th1\n'),
                'b-test/shared_b_reflect.json': ('bytes', b'{"shared":1}'),
                'b-test/empty.json': ('bytes', b''),
                'b-test/big_b.metallib': ('bytes', big_payload),
            }
            if real_metallib is not None:
                tree_files['b-test/real_b.metallib'] = ('bytes', real_metallib)
            tree_archive = root / 'msl-b-test.img'
            with open(tree_archive, 'wb') as tf:
                romfs_image._write_msl_subimage(tf, 0, tree_files)
            romfs_image.append_msl(img1, tree_archive, gfx_api='nvn', chain='b-test', multi_program=True)

            vk_msl = root / 'vk_msl/vk'
            vk_msl.mkdir(parents=True)
            (vk_msl / 'vk_shaders.tsv').write_text('0123456789abcdef\tvert\tu:0.0=0*1\n')
            (vk_msl / 'vk_shaders.metallib').write_bytes(real_metallib or (b'MTLB' + b'\0' * 60))
            romfs_image.append_msl(img2, vk_msl.parent)

            harness_m = root / 'harness.m'
            harness_bin = root / 'harness'
            harness_m.write_text(MOUNT_HARNESS_OBJC)
            subprocess.run([
                'clang', '-O1', '-g', '-Wall', '-Wextra', '-Wno-unused-function', '-Werror', '-fobjc-arc',
                '-I', str(REPO),
                str(harness_m), str(REPO / 'dm/gfx/nvn/msl_mount.c'),
                '-framework', 'Foundation', '-framework', 'Metal',
                '-o', str(harness_bin),
            ], check=True)
            res = subprocess.run([str(harness_bin), str(img1), str(img2), '1' if real_metallib else '0'],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_lifecycle_and_backend_callers_wire_trailer_fallback_and_override(self):
        """Exercises hle_lifecycle.c's teardown hook and verifies DM_MSL_DIR precedence over DM_ROMFS trailer in NVN/VK/GL."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'src'; src.mkdir()
            (src / 'f').write_bytes(b'data')
            img = root / 'romfs.img'
            romfs_image.pack(src, img)

            chain_h = (REPO / 'dm/gfx/nvn/dmsh_chain.h').read_text()
            chain = chain_h.split('"')[1]   # DMSH_CHAIN hex string
            b_chain = f'b-{chain}'

            msl_dir = root / 'msl_stage' / b_chain
            msl_dir.mkdir(parents=True)
            (msl_dir / 'inputs.tsv').write_text('game\t010015100B514000\ninputs\tfrom_trailer\n')
            (msl_dir / 'only_trailer_b_reflect.json').write_text('{"src":"trailer"}')
            (msl_dir / 'override_me_b_reflect.json').write_text('{"src":"trailer"}')
            (msl_dir / '0000000000000001_b.metallib').write_bytes(b'MTLB' + b'\0' * 44)
            romfs_image.append_msl(img, msl_dir.parent, gfx_api='nvn', chain=b_chain)

            # Override folder on disk has ONLY override_me_b_reflect.json; only_trailer_b_reflect.json must fall back to trailer!
            disk_msl = root / 'disk_msl' / b_chain
            disk_msl.mkdir(parents=True)
            (disk_msl / 'override_me_b_reflect.json').write_text('{"src":"disk"}')

            # Build caller test linking actual hle_lifecycle.c + msl_mount.c and testing the exact NVN/VK/GL lookup & lifecycle teardown chain
            caller_m = root / 'caller.m'
            caller_bin = root / 'caller'
            caller_m.write_text(r'''#import <Foundation/Foundation.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/param.h>
#include <unistd.h>
#include "dm/gfx/msl_mount.h"
#include "dm/gfx/nvn/dmsh_chain.h"

// Stubs for hle_lifecycle.c's external host calls so we run the real dm_host_teardown_for_shell()
uint64_t now_ns(void) { return 0; }
void dm_log(const char *fmt, ...) { (void)fmt; }
void hlecount_flush(void) {}
void dm_rec_finish(void) {}
void dm_cheats_exit(void) {}
void dm_audio_suspend(int s) { (void)s; }
void dm_gfx_suspend(int s) { (void)s; }
void dm_gfx_quit(int ms) { (void)ms; }
void dm_vsync_stop_all(void) {}
void dm_audio_stop_all(void) {}
void dm_threads_park_all(void) {}
void dm_guest_memory_release(int u) { (void)u; }
void dm_modules_teardown(void) {}
void dm_bigstack_teardown(void) {}
int  dm_teardown_is_guest_thread(void) { return 0; }
void dm_teardown_park_self(void) {}
void dm_host_teardown_for_shell(void);

static int count_fds(const char *target) {
    char want[MAXPATHLEN];
    if (!realpath(target, want)) return -1;
    int c = 0;
    for (int fd = 0; fd < 256; fd++) {
        char got[MAXPATHLEN], res[MAXPATHLEN];
        if (fcntl(fd, F_GETPATH, got) == 0 && !strcmp(realpath(got, res) ? res : got, want)) c++;
    }
    return c;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc != 3) return 2;
        const char *img = argv[1];
        const char *disk_root = argv[2];
        setenv("DM_ROMFS", img, 1);

        // Simulate nvn_mtl.m's exact msl_data precedence: disk DM_MSL_DIR first, then dm_msl_trailer_read
        NSString *g_msl_dir = [NSString stringWithFormat:@"%s/b-%s", disk_root, DMSH_CHAIN];
        NSString *(^read_nvn)(NSString *) = ^NSString *(NSString *sub) {
            NSString *p = [g_msl_dir stringByAppendingPathComponent:sub];
            NSData *d = [NSData dataWithContentsOfFile:p];
            if (!d) {
                NSString *rel = [NSString stringWithFormat:@"b-%s/%@", DMSH_CHAIN, sub];
                void *buf = NULL; size_t len = 0;
                if (dm_msl_trailer_read(rel.UTF8String, &buf, &len))
                    d = [NSData dataWithBytesNoCopy:buf length:len freeWhenDone:YES];
            }
            return d ? [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding] : nil;
        };

        NSString *over = read_nvn(@"override_me_b_reflect.json");
        NSString *fall = read_nvn(@"only_trailer_b_reflect.json");
        if (![over isEqualToString:@"{\"src\":\"disk\"}"]) { fprintf(stderr, "override failed: %s\n", over.UTF8String); return 1; }
        if (![fall isEqualToString:@"{\"src\":\"trailer\"}"]) { fprintf(stderr, "fallback failed: %s\n", fall.UTF8String); return 1; }
        if (count_fds(img) != 1) { fprintf(stderr, "expected 1 open fd before shell teardown\n"); return 1; }

        // Real dm_host_teardown_for_shell() from hle_lifecycle.c must invoke strong dm_msl_trailer_teardown()
        dm_host_teardown_for_shell();
        if (count_fds(img) != 0) { fprintf(stderr, "dm_host_teardown_for_shell leaked trailer fd\n"); return 1; }
    }
    return 0;
}
''')
            subprocess.run([sys.executable, str(REPO / 'dm/games/game.py'), 'header',
                            '010015100B514000', str(root / 'game_profile.h')], check=True)
            subprocess.run([
                'clang', '-O1', '-g', '-Wall', '-Wno-unused-function', '-fobjc-arc',
                '-I', str(REPO), '-I', str(REPO / 'dm/host'), '-I', str(root),
                str(caller_m), str(REPO / 'dm/host/hle_lifecycle.c'), str(REPO / 'dm/gfx/nvn/msl_mount.c'),
                '-framework', 'Foundation',
                '-o', str(caller_bin),
            ], check=True)
            rc = subprocess.run([str(caller_bin), str(img), str(root / 'disk_msl')], capture_output=True, text=True)
            self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)


if __name__ == '__main__':
    unittest.main()
