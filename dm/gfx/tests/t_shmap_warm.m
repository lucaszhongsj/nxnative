// Reading the shader map is one thing and starting the prewarm another. The map is read once from any thread (shmap_read_once: the prefetch at nvnProgramSetShaders, the submit thread), inside a
// dispatch_once; the prewarm and its prepare step run on the submit path, at the first submission, and reach shmap_load again (pso_prebuild). With the prewarm inside the once, that second call on
// the same thread trapped in _dispatch_once_wait: every game with a window and a prebuild, at its first program.
#include "../nvn/nvn_mtl.m"
#include <stdlib.h>
#include <unistd.h>

void *front_metal_layer(void) { return NULL; }
module_t *module_of(const void *p) { (void)p; return NULL; }
module_t *g_mod; int g_nmod;
void svc_thread_dump(void) {}
uint64_t now_ns(void) { return bp_now(); }

static int g_checks, g_failed;
#define EXPECT_EQ(name, want, got) do { \
    long long w_ = (long long)(want), g_ = (long long)(got); g_checks++; \
    printf("%s expected %lld -> got %lld %s\n", name, w_, g_, w_ == g_ ? "OK" : "FAIL"); \
    g_failed += w_ != g_; \
} while (0)

static void put(NSString *dir, NSString *name, NSString *text) {
    if (![text writeToFile:[dir stringByAppendingPathComponent:name] atomically:YES encoding:NSUTF8StringEncoding error:nil]) { printf("cannot write %s\n", name.UTF8String); exit(2); }
}

int main(void) { @autoreleasepool {
    char tmpl[] = "/tmp/t_shmap_warm.XXXXXX";
    if (!mkdtemp(tmpl)) { perror("mkdtemp"); return 2; }
    NSString *dir = @(tmpl);
    put(dir, @"shader_map.tsv", @"# no shaders\n");
    put(dir, @"pso_manifest.tsv", @"# an empty seed list: the prepare step goes on to the prebuild\n");
    setenv("DM_NVN_SHMAP", [dir stringByAppendingPathComponent:@"shader_map.tsv"].UTF8String, 1);
    setenv("DM_MSL_DIR", tmpl, 1);
    setenv("DM_PSO_PREPARE", "1", 1);   // as with a window (front_metal_layer here has none)
    unsetenv("DM_PSO_MANIFEST"); unsetenv("DM_SAVE"); unsetenv("DM_PSO_PREBUILD"); unsetenv("DM_PSO_HYBRID");
    mtl_init();

    shmap_read_once();   // what the prefetch does at the game's first program
    EXPECT_EQ("the map is read without starting the prewarm", 0, g_warm_started);

    warm_enqueue([WarmItem new]);   // a library the prefetch finished before the first submission
    EXPECT_EQ("an item finished before the prewarm starts is kept", 1, g_warm_items.count);

    mtl_prepare();   // the first submission: the prewarm, then the prebuild, which calls shmap_load again on this thread
    EXPECT_EQ("the first submission starts the prewarm", 1, g_warm_started);
    EXPECT_EQ("the prebuild path was reached: the compiler it needs exists", 1, hybrid_on());
    EXPECT_EQ("the item is still there", 1, g_warm_items.count);

    mtl_prepare(); shmap_load();   // later calls: nothing starts twice
    EXPECT_EQ("the prewarm starts once", 1, g_warm_started);

    // Embedded DMMSLTR1 shader trailer in DM_ROMFS serves shmap_read / msl_data / msl_has when DM_MSL_DIR lacks the files, while DM_MSL_DIR still wins when present
    dm_gfx_teardown();
    {
        uint8_t base_img[32 + 24] = {0};
        uint64_t bhd[3] = {32, 24, 1};
        img_ent be = {1, 0, 0, 0, 0, 0};
        memcpy(base_img, "DMROMFS1", 8); memcpy(base_img + 8, bhd, sizeof bhd); memcpy(base_img + 32, &be, sizeof be);

        NSString *bdir = [NSString stringWithFormat:@"b-%s", DMSH_CHAIN];
        NSData *bd_bytes = [bdir dataUsingEncoding:NSUTF8StringEncoding];
        NSString *stamp_txt = [NSString stringWithFormat:@"game\t%s\ninputs\ttrstamp\n", dm_profile()->id];
        NSData *st_bytes = [stamp_txt dataUsingEncoding:NSUTF8StringEncoding];
        const char *ov_json = "{\"from\":\"trailer\"}";
        size_t ov_len = strlen(ov_json);
        size_t off0 = 32;
        size_t off1 = (off0 + st_bytes.length + 15u) & ~15u;
        size_t ioff = off1 + ov_len;
        const char *f0 = "inputs.tsv", *f1 = "ov_b_reflect.json";
        size_t pool_len = bd_bytes.length + strlen(f0) + strlen(f1);
        size_t isz = 4 * sizeof(img_ent) + pool_len;
        size_t sub_sz = ioff + isz;
        uint8_t *sub = calloc(1, sub_sz);
        uint64_t shd[3] = {ioff, isz, 4};
        img_ent se[4] = {
            {1, 1, 0, 0, 0, 0},
            {2, 2, 0, (uint16_t)bd_bytes.length, 0, 0},
            {off0, st_bytes.length, (uint32_t)bd_bytes.length, (uint16_t)strlen(f0), 1, 0},
            {off1, ov_len, (uint32_t)(bd_bytes.length + strlen(f0)), (uint16_t)strlen(f1), 1, 0},
        };
        memcpy(sub, "DMROMFS1", 8); memcpy(sub + 8, shd, sizeof shd);
        memcpy(sub + off0, st_bytes.bytes, st_bytes.length);
        memcpy(sub + off1, ov_json, ov_len);
        memcpy(sub + ioff, se, sizeof se);
        memcpy(sub + ioff + sizeof se, bd_bytes.bytes, bd_bytes.length);
        memcpy(sub + ioff + sizeof se + bd_bytes.length, f0, strlen(f0));
        memcpy(sub + ioff + sizeof se + bd_bytes.length + strlen(f0), f1, strlen(f1));

        uint64_t msl_off = (sizeof(base_img) + 16383ull) & ~16383ull;
        uint64_t ftr_off = ((msl_off + sub_sz) + 7ull) & ~7ull;
        size_t full_sz = (size_t)ftr_off + sizeof(dm_msl_footer);
        uint8_t *full = calloc(1, full_sz);
        memcpy(full, base_img, sizeof base_img);
        memcpy(full + msl_off, sub, sub_sz);
        dm_msl_footer ft = {0};
        memcpy(ft.magic, "DMMSLTR1", 8);
        ft.version = 1; ft.gfx_api = DM_MSL_GFX_NVN; ft.chain_len = (uint16_t)bd_bytes.length;
        ft.base_romfs_size = sizeof base_img; ft.msl_offset = msl_off; ft.msl_size = sub_sz;
        memcpy(ft.chain, bd_bytes.bytes, bd_bytes.length);
        memcpy(full + ftr_off, &ft, sizeof ft);

        NSString *rom_path = [dir stringByAppendingPathComponent:@"rom.img"];
        [[NSData dataWithBytesNoCopy:full length:full_sz freeWhenDone:YES] writeToFile:rom_path atomically:YES];
        free(sub);

        put(dir, @"c1_full.tsv", [NSString stringWithFormat:@"# chain b-%s inputs trstamp \n", DMSH_CHAIN]);
        setenv("DM_ROMFS", rom_path.fileSystemRepresentation, 1);
        setenv("DM_MSL_DIR", [dir stringByAppendingPathComponent:@"msl_override"].fileSystemRepresentation, 1);

        shmap_read();
        EXPECT_EQ("shmap_read validates c1_full.tsv from DM_ROMFS trailer inputs.tsv", 1, g_c1_full_valid);
        EXPECT_EQ("msl_has finds trailer entry", 1, msl_has([g_msl_dir stringByAppendingPathComponent:@"inputs.tsv"]));
        EXPECT_EQ("msl_has reports absent trailer entry", 0, msl_has([g_msl_dir stringByAppendingPathComponent:@"missing.metallib"]));
        EXPECT_EQ("msl_has ignores paths outside g_msl_dir", 0, msl_has(@"/tmp/outside_nonexistent.metallib"));

        NSData *tr_ov = msl_data([g_msl_dir stringByAppendingPathComponent:@"ov_b_reflect.json"]);
        NSString *tr_str = tr_ov ? [[NSString alloc] initWithData:tr_ov encoding:NSUTF8StringEncoding] : @"";
        EXPECT_EQ("msl_data reads reflection from trailer", 1, [tr_str isEqualToString:@"{\"from\":\"trailer\"}"]);

        [NSFileManager.defaultManager createDirectoryAtPath:g_msl_dir withIntermediateDirectories:YES attributes:nil error:nil];
        put(g_msl_dir, @"ov_b_reflect.json", @"{\"from\":\"disk\"}");
        NSData *dk_ov = msl_data([g_msl_dir stringByAppendingPathComponent:@"ov_b_reflect.json"]);
        NSString *dk_str = dk_ov ? [[NSString alloc] initWithData:dk_ov encoding:NSUTF8StringEncoding] : @"";
        EXPECT_EQ("DM_MSL_DIR overrides DM_ROMFS trailer when file exists on disk", 1, [dk_str isEqualToString:@"{\"from\":\"disk\"}"]);

        dm_gfx_teardown();
        unsetenv("DM_ROMFS");
    }

    [NSFileManager.defaultManager removeItemAtPath:dir error:nil];
    printf("%d checks, %d failed\n", g_checks, g_failed);
    return g_failed != 0;
} }
