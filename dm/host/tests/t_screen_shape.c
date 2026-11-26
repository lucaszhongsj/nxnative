// DM_SCREEN_ASPECT: the state page a screen shape is written into (loader.c dm_fps_state_apply), for the shapes the iOS front end asks for.
// A phone standing up asks for its upper half's shape, 0.92 on a 19.5:9 screen, so the accepted range has to reach below 1.3: the lower bound
// once sat at 1.3, and the front end's 0.92 was refused with "[RED] a width / height from 1.3 to 2.4; keeping 16:9" while the screen never changed.
#include "../loader.c"
#include <assert.h>
#include <stdio.h>
#include <string.h>

void mtl_present_screen(uint32_t w, uint32_t h) { (void)w; (void)h; }   // nvn_mtl.m: the picture's shape, no renderer here
static uint32_t g_pic_w, g_pic_h, g_screen_w, g_screen_h;
void mtl_present_picture(uint32_t w, uint32_t h) { g_pic_w = w; g_pic_h = h; }   // nvn_mtl.m: the picture's rectangle in a window that holds every shape
// What loader.c links against elsewhere (as t_nro.c provides): the flags, the clock, the gate and symbol tables, and the module loader's
// deactivation, which this fixture never reaches.
struct dm_flags g_flag;
uint64_t now_ns(void) { return 0; }
void gate_register(uint64_t a, const char *k, int n, uint64_t o) { (void)a; (void)k; (void)n; (void)o; }
void hlecount_seal(void) {}
void *hlecount_wrap(const char *kind, const char *name, void *fn) { (void)kind; (void)name; return fn; }
static uint64_t missing;
uint64_t resolve_import(module_t *m, const char *name) { (void)m; (void)name; return (uint64_t)&missing; }
int hle_implements(const char *name) { (void)name; return 0; }
int svc_nro_deactivate(module_t *m) { m->loaded = 0; return 1; }
void dm_thread_service_connect(void) {}
void dm_exl_sync_all(void) {}   // loader.c's run_module_inits: no exl modules here

static uint8_t g_page[0x400];
static int g_cases, g_fails;

static uint32_t at(size_t off) { uint32_t v; memcpy(&v, g_page + off, 4); return v; }
static float atf(size_t off) { float v; memcpy(&v, g_page + off, 4); return v; }
// patches.py (main+0x1527f00): the first read of the state page in a frame applies the shape a runtime change asked for, so no frame mixes two
static void frame_commit(void) {
    uint32_t seq = at(0x23c), done = at(0x238);
    if (!seq || seq == done) return;
    uint32_t req[6]; memcpy(req, g_page + 0x220, sizeof req);
    memcpy(g_page + 0x64, req + 0, 4); memcpy(g_page + 0x48, req + 1, 4);
    memcpy(g_page + 0x4c, req + 2, 4); memcpy(g_page + 0x50, req + 3, 4);
    memcpy(g_page + 0x58, req + 4, 4); memcpy(g_page + 0x5c, req + 5, 4);
    memcpy(g_page + 0x238, &seq, 4);
}

static void shape(const char *value, uint32_t want_w, uint32_t want_h, const char *why) {
    g_cases++;
    memset(g_page, 0, sizeof g_page);
    unsetenv("DM_SCREEN_ASPECT_UNION");
    if (value) setenv("DM_SCREEN_ASPECT", value, 1); else unsetenv("DM_SCREEN_ASPECT");
    dm_fps_state_apply();
    uint32_t w = at(0x64), h = at(0x48);
    if (w != want_w || h != want_h) {
        printf("✗ %s: DM_SCREEN_ASPECT=%s gave %ux%u, wanted %ux%u\n", why, value ? value : "(unset)", w, h, want_w, want_h);
        g_fails++;
    } else printf("✓ %s: DM_SCREEN_ASPECT=%s → %ux%u\n", why, value ? value : "(unset)", w, h);
}
static void check(int ok, const char *why) {
    g_cases++;
    if (ok) printf("✓ %s\n", why); else { printf("✗ %s\n", why); g_fails++; }
}
// The union framebuffer (DM_SCREEN_ASPECT_UNION): the window holds the widest and tallest output of the range (1920x2040), while the picture keeps
// the shape asked for; the phone turning moves the picture inside the same window (dm_screen_shape)
static void union_shape(void) {
    memset(g_page, 0, sizeof g_page); g_pic_w = g_pic_h = 0;
    setenv("DM_SCREEN_ASPECT", "2.17", 1); setenv("DM_SCREEN_ASPECT_UNION", "1", 1);
    dm_fps_state_apply();
    check(at(0x68) == 1920 && at(0x6c) == 2040, "the union window is the widest and tallest output the 19.5:9 phone asks for (1920x2040)");
    check(at(0x64) == 1920 && at(0x48) == 888 && g_pic_w == 1920 && g_pic_h == 888, "the picture keeps the shape asked for (1920x888), and the present is told its rectangle");
    dm_screen_shape(0.92f);
    check(at(0x64) == 1920 && at(0x48) == 888, "the phone turns: the frame's fields wait for its first read (no frame mixes two shapes)");
    check(at(0x220) == 1880 && at(0x224) == 2040 && at(0x23c) == 1, "the request names the picture (1880x2040), with its sequence");
    frame_commit();
    check(at(0x64) == 1880 && at(0x48) == 2040 && atf(0x58) > 0.92f && atf(0x58) < 0.9217f, "the next frame applies it: the picture follows (1880x2040) inside the same window");
    check(at(0x68) == 1920 && at(0x6c) == 2040, "the window keeps its size while the game runs");
    dm_screen_shape(0.0f);
    check(at(0x64) == 1880 && at(0x48) == 2040, "16:9 waits for the same read");
    frame_commit();
    check(at(0x64) == 0 && at(0x48) == 0 && at(0x58) == 0, "16:9: the page is zeroed at the frame, and the game's own rectangle stands");
    // The picture's output is capped to the view's pixels (dm_screen_shape_px): the shape's 1920-wide space is a coordinate space, not a resolution
    dm_screen_shape_px(0.92f, 1206, 2622);
    check(at(0x220) == 1200 && at(0x224) == 1304, "a standing phone's picture is capped to the view's width (1200x1304)");
    frame_commit();
    check(at(0x64) == 1200 && at(0x48) == 1304, "the frame applies the capped size");
    dm_screen_shape_px(2.174f, 2622, 1206);
    check(at(0x220) == 1920 && at(0x224) == 880, "a lying-down phone's view is wider than the shape: 1920x880 stands");
    frame_commit();
    // Without bit 10 the size table's entry is not the picture's rectangle: one shape, as before
    memset(g_page, 0, sizeof g_page);
    setenv("DM_SCREEN_ASPECT_SITES", "0xfffffbff", 1);
    dm_fps_state_apply();
    check(at(0x68) == 0 && at(0x6c) == 0, "bit 10 off: no union window");
    unsetenv("DM_SCREEN_ASPECT_SITES");
    // A window of one shape keeps it when the phone turns
    memset(g_page, 0, sizeof g_page);
    unsetenv("DM_SCREEN_ASPECT_UNION");
    dm_fps_state_apply();
    dm_screen_shape(0.92f);
    check(at(0x64) == 1920 && at(0x48) == 888, "no union: the picture keeps the shape the window was built for");
    unsetenv("DM_SCREEN_ASPECT");
}

int main(void) {
    dm_fps_state = g_page;
    g_fps_mod = "test";

#if DM_GAME_SCREEN_UNION
    shape("0.92", 1880, 2040, "a 19.5:9 phone standing up: its upper half's shape");
#else
    shape("0.92", 0, 0, "a profile without the union stubs keeps main's 1.3 range: 16:9");
#endif
    shape("1.12", 0, 0, "a 16:9 phone standing up is not the adapted device: 16:9");
    shape("1.43", 1920, 1344, "an iPhone Duo keeps main's range");
#if DM_GAME_SCREEN_UNION
    shape("2.17", 1920, 888, "a 19.5:9 phone lying down");
#else
    shape("2.17", 0, 0, "without the union stubs the range stops at 1.77: 16:9");
#endif
    shape("0.90", 0, 0, "below the portrait band: 16:9");
    shape("2.19", 0, 0, "above the landscape band: 16:9");
    shape("1.77", 0, 0, "main's ceiling is exclusive: 16:9");
    shape("0", 0, 0, "0 is 16:9");
    shape(NULL, 0, 0, "unset is 16:9");
#if DM_GAME_SCREEN_ASPECT
    union_shape();
    {   // The boot shape takes the same cap from the environment (front_ios.m sets it before the host starts)
        memset(g_page, 0, sizeof g_page);
        setenv("DM_SCREEN_ASPECT", "0.92", 1); setenv("DM_SCREEN_ASPECT_UNION", "1", 1); setenv("DM_SCREEN_ASPECT_PX", "1206x2622", 1);
        dm_fps_state_apply();
        check(at(0x64) == 1200 && at(0x48) == 1304, "the boot shape is capped to the front end's view pixels (DM_SCREEN_ASPECT_PX)");
        unsetenv("DM_SCREEN_ASPECT_PX"); unsetenv("DM_SCREEN_ASPECT"); unsetenv("DM_SCREEN_ASPECT_UNION");
    }
#endif

    printf("== %d checks, %d failed ==\n", g_cases, g_fails);
    return g_fails != 0;
}
