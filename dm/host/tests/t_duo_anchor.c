// The composition classifier this profile's duo_anchor.c runs over nn::ui2d panes, over synthetic panes: duo_anchor never scales a background (the draw-time
// backdrop owns cover), a container and its text stay as they are, an edge widget moves only in a native layout, 16:9 keeps every matrix, and the
// DM_SCREEN_TRACE ring names the layouts and the panes the backdrop consumes, once per present the layout is calculated in. No game files: the source is
// compiled into this test and the pane layout is the one duo_anchor.c documents (parent +0x18, first child +0x28, size +0x50, global matrix +0x70, name +0xb0).
// The patch stub calls duo_calc only on a phone shape, so a 16:9 run's log has no trace lines; the 16:9 vectors below cover duo_calc's own code path.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

// duo_anchor.c: the parameters come from the frame step state page (+0x80); +0x54 holds the sites mask
extern void duo_calc(char *pane, const float *st, const void *fn_null_draw);

static void fn_draw(void) {}
static void fn_null(void) {}

enum { PANE = 0x200 };
static unsigned char *pane_new(const char *name, const void *draw) {
    unsigned char *p = calloc(1, PANE);
    void **vt = calloc(22, sizeof *vt);
    vt[21] = (void *)draw;
    *(void **)p = vt;
    *(unsigned char **)(p + 0x28) = p + 0x20;   // the child list empty
    if (name) { size_t n = strlen(name); if (n > 15) n = 15; memcpy(p + 0xb0, name, n); }
    return p;
}
static void putf(void *p, int off, float v) { memcpy((char *)p + off, &v, 4); }
static float getf(const void *p, int off) { float v; memcpy(&v, (const char *)p + off, 4); return v; }
static void attach(unsigned char *par, unsigned char *child) {
    *(unsigned char **)(child + 0x18) = par;
    if (*(unsigned char **)(par + 0x28) == par + 0x20) *(unsigned char **)(par + 0x28) = child;
}
static unsigned char *child_new(unsigned char *root, const char *name, const void *draw, float w, float h, float x, float y) {
    unsigned char *p = pane_new(name, draw);
    attach(root, p);
    putf(p, 0x50, w); putf(p, 0x54, h); putf(p, 0x70, 1); putf(p, 0x84, 1); putf(p, 0x7c, x); putf(p, 0x8c, y);
    return p;
}
// A layout as the game builds it: RootPane over the layout's own node, whose children are the panes (duo_anchor.c leaves a layout whose
// direct child is the root alone: panes sit one level further in)
static unsigned char *root_new(float w, float h) {
    unsigned char *p = pane_new("RootPane", fn_null);
    putf(p, 0x50, w); putf(p, 0x54, h); putf(p, 0x70, 1); putf(p, 0x84, 1);
    return p;
}
static unsigned char *layout_new(float w, float h) {
    unsigned char *root = root_new(w, h);
    return child_new(root, "L_Layout", fn_null, w, h, 0, 0);
}

static unsigned char state[0x1000];
static float *const st = (float *)(state + 0x80);
// The parameters loader.c writes for the standing phone: shift 0, stretch 1.932, a full-screen pane 1270 x 710 in 720 units. A forced shift is
// 335.5 (a 1391-unit space's half-height less 360) and must still not scale a background.
static void state_reset(float shift, float stretch) {
    memset(state, 0, sizeof state);
    *(unsigned *)(state + 0x54) = 0xffffffffu;
    float s[7] = {shift, stretch, 280, 100, 1270, 710, 1300};
    memcpy(state + 0x80, s, sizeof s);
    float ratio = 0.92f; memcpy(state + 0x58, &ratio, 4);
    unsigned present = 100; memcpy(state + 0xa8, &present, 4);
}
static unsigned at(unsigned off) { unsigned v; memcpy(&v, state + off, 4); return v; }
static float recf(const unsigned char *r, int off) { float v; memcpy(&v, r + off, 4); return v; }

static int cases, fails;
static void check(int ok, const char *why) {
    cases++;
    if (ok) printf("✓ %s\n", why); else { printf("✗ %s\n", why); fails++; }
}

int main(void) {
    unsigned char *lay, *p;

    // The real loader parameters: the standing phone shifts nothing, so a full-screen backdrop stays as the game laid it out
    state_reset(0.0f, 1.932f);
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_BG", fn_draw, 1920, 1080, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x70) == 1.0f && getf(p, 0x84) == 1.0f, "the real standing-phone parameters leave a full-screen backdrop unscaled");

    // Forcing the old shift does not bring a background scale back: the draw-time backdrop owns cover, and one layer covers a backdrop
    state_reset(335.5f, 1.932f);
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_BG", fn_draw, 1920, 1080, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x70) == 1.0f && getf(p, 0x84) == 1.0f, "a forced shift leaves a full-screen backdrop unscaled too");
    check(getf(p, 0x7c) == 0.0f && getf(p, 0x8c) == 0.0f, "the backdrop keeps its centred place");

    // A container as large as the layout is not a backdrop: it and its text stay at their own scale
    state_reset(335.5f, 1.932f);
    lay = layout_new(1920, 1080);
    unsigned char *box = child_new(lay, "Box", fn_null, 1920, 1080, 0, 0);
    unsigned char *text = child_new(box, "T_Text", fn_draw, 300, 60, 0, 0);
    duo_calc((char *)box, st, fn_null);
    duo_calc((char *)text, st, fn_null);
    check(getf(box, 0x70) == 1.0f && getf(box, 0x84) == 1.0f, "a full-screen container is not scaled");
    check(getf(text, 0x70) == 1.0f && getf(text, 0x84) == 1.0f, "the text in a container is not scaled");

    // A tall narrow drawing leaf (the tailor's halves) is a backdrop as well: it is left alone and, being centred, never moves
    state_reset(335.5f, 1.932f);
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_BG_03", fn_draw, 960, 1080, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x70) == 1.0f && getf(p, 0x84) == 1.0f, "a tall narrow leaf is not stretched by duo_anchor");
    check(getf(p, 0x8c) == 0.0f, "the centred tall narrow leaf keeps its place");

    // An edge widget moves out by the extra half-height (335.5 x 1.5 = 503.25), with its set of edge distances (270 x 1.5)
    state_reset(335.5f, 1.932f);
    lay = layout_new(1920, 1080);
    p = child_new(lay, "W_Edge", fn_draw, 100, 100, 0, -500);
    duo_calc((char *)p, st, fn_null);
    check(fabsf(getf(p, 0x8c) - (-1003.25f)) < 1e-3f, "a bottom edge widget moves out by the picture's extra half-height");

    // While the game's keyboard is up the interface is docked by its projection: the edge rule leaves the widget where the layout has it
    state_reset(335.5f, 1.932f);
    lay = layout_new(1920, 1080);
    p = child_new(lay, "W_Edge", fn_draw, 100, 100, 0, -500);
    { unsigned stamp = 99; memcpy(state + 0xb0, &stamp, 4); }   // the keyboard was calculated on the previous present
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x8c) == -500.0f, "a docked keyboard leaves the bottom widget at the layout's place");

    // A layout its root placed (a balloon over a character) is not on the screen's edges
    state_reset(335.5f, 1.932f);
    unsigned char *root = root_new(1920, 1080);
    putf(root, 0x30, 100);   // the root's own translation: the layout is placed at a point of the scene
    unsigned char *placed = child_new(root, "L_Placed", fn_null, 1920, 1080, 0, 0);
    p = child_new(placed, "W_Balloon", fn_draw, 100, 100, 0, -500);
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x8c) == -500.0f, "a root-placed layout's widget does not move");

    // The app's own canvas background (the map's, the critterpedia's, the passport's 1280x1000 canvas at a taller picture): a full-screen leaf
    // wider than the layout grows uniformly about its centre until it fills the picture's height; the draw-time hook cannot take it because the
    // leaf is drawn scaled, so its quad is not its texture's size
    state_reset(0.0f, 1.932f);
    *(unsigned *)(state + 0x250) = 1;
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_Base_0", fn_draw, 2450, 1500, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(fabsf(getf(p, 0x70) - 1.391f) < 2e-3f && fabsf(getf(p, 0x84) - 1.391f) < 2e-3f,
          "the app-canvas background grows to the picture's height (S/C = 1391/1000)");
    check(getf(p, 0x7c) == 0.0f && getf(p, 0x8c) == 0.0f, "the canvas background keeps its centre");

    // A 1:1 full-screen backdrop (the phone's wallpaper, the title's base) is the draw-time hook's: its pane size is its texture's and the hook
    // expands it with the picture's UVs, so this rule leaves it alone
    state_reset(0.0f, 1.932f);
    *(unsigned *)(state + 0x250) = 1;
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_Tex_00", fn_draw, 1920, 1080, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x70) == 1.0f && getf(p, 0x84) == 1.0f, "a layout-wide backdrop stays with the draw-time hook");

    // Cover off: the game's own composition, as before any of this
    state_reset(0.0f, 1.932f);
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_Base_0", fn_draw, 2450, 1500, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x70) == 1.0f && getf(p, 0x84) == 1.0f, "with the cover off the canvas background keeps the game's scale");

    // A wide picture: the layout is shorter than 16:9's 720 units, so the canvas is cropped, never gapped; the cover stays off
    state_reset(0.0f, 0.822f);
    *(unsigned *)(state + 0x250) = 1;
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_Base_0", fn_draw, 2450, 1500, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x70) == 1.0f && getf(p, 0x84) == 1.0f, "a wide picture crops the canvas, never covers it");

    // UI text inside a container is not a background leaf and never scales, cover on or off
    state_reset(0.0f, 1.932f);
    *(unsigned *)(state + 0x250) = 1;
    lay = layout_new(1920, 1080);
    unsigned char *coverbox = child_new(lay, "Box", fn_null, 1920, 1080, 0, 0);
    p = child_new(coverbox, "T_Text", fn_draw, 300, 60, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x70) == 1.0f && getf(p, 0x84) == 1.0f, "UI text is never scaled by the canvas cover");

    // 16:9: the shift is 0, every matrix stays as the game computed it
    state_reset(0.0f, 1.0f);
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_BG", fn_draw, 1920, 1080, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x70) == 1.0f && getf(p, 0x84) == 1.0f, "16:9 (shift 0) is identity");

    // The trace (DM_SCREEN_TRACE) on the real parameters: one layout record, then the pane the backdrop will consume, in the state page's ring
    state_reset(0.0f, 1.932f);
    *(unsigned *)(state + 0x240) = 1;
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_BG", fn_draw, 1920, 1080, 0, 0);
    duo_calc((char *)p, st, fn_null);
    const unsigned char *rec0 = state + 0x2e0, *rec1 = state + 0x2e0 + 40;
    check(at(0xce0) == 2 && at(0xce4) == 0, "the trace records the layout and the backdrop candidate");
    check(rec0[0] == 0 && memcmp(rec0 + 28, "RootPane", 8) == 0, "the first record is the layout, with its root's name");
    check(rec1[0] == 1 && *(const unsigned *)(rec1 + 36) == 1 && memcmp(rec1 + 28, "P_BG", 8) == 0, "the second names the pane and its class (full-screen leaf)");
    check(getf(p, 0x70) == 1.0f && getf(p, 0x84) == 1.0f, "the trace changes no matrix on the real parameters");

    // The stamps the view keeps for the keyboard and the language list survive a traced calculation
    state_reset(0.0f, 1.932f);
    *(unsigned *)(state + 0x240) = 1;
    *(unsigned *)(state + 0x274) = 0x1234abcdu; *(unsigned *)(state + 0x278) = 0x5678ef01u;
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_BG", fn_draw, 1920, 1080, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(at(0x274) == 0x1234abcdu && at(0x278) == 0x5678ef01u, "a traced calculation leaves the view's keyboard and language stamps alone");

    // A 16:9 picture still traces its layout and its backdrop candidate, but changes nothing
    state_reset(0.0f, 1.0f);
    *(unsigned *)(state + 0x240) = 1;
    { float ratio = 16.0f / 9.0f; memcpy(state + 0x58, &ratio, 4); }
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_BG", fn_draw, 1920, 1080, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(at(0xce0) == 2, "16:9 with the trace on records the layout and its backdrop candidate");
    check(at(0xb0) == 0, "16:9 with the trace on writes no keyboard stamp");
    check(getf(p, 0x70) == 1.0f && getf(p, 0x84) == 1.0f, "16:9 with the trace on is still identity");

    // An app canvas the layout itself is (1280 x 1000): the root's width names the 720 space, so its full-screen background is a backdrop candidate recorded
    // at its own size. Judged by the root's height it would pass for the 1920 space, and the 1000-high pane would fall under the 710-unit record threshold
    state_reset(0.0f, 1.932f);
    *(unsigned *)(state + 0x240) = 1;
    lay = layout_new(1280, 1000);
    p = child_new(lay, "P_Canvas", fn_draw, 1280, 1000, 0, 0);
    duo_calc((char *)p, st, fn_null);
    check(at(0xce0) == 2, "a 1280 x 1000 canvas layout records its full-screen pane");
    check((at(0x2e0 + 4) & 1) == 0, "the canvas's space is the 1280 one");
    check(*(const unsigned *)(state + 0x2e0 + 40 + 36) == 1 && recf(state + 0x2e0 + 40, 12) == 1280 && recf(state + 0x2e0 + 40, 16) == 1000,
          "the canvas pane is recorded at 1280 x 1000 units, not rescaled");

    // A full ring drops the records and marks nothing: after the host drains, the next calculation records the layout and its pane
    state_reset(0.0f, 1.932f);
    *(unsigned *)(state + 0x240) = 1;
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_BG", fn_draw, 1920, 1080, 0, 0);
    { unsigned full = 64; memcpy(state + 0xce0, &full, 4); }
    duo_calc((char *)p, st, fn_null);
    check(at(0xce0) == 64, "a full ring drops the record");
    { unsigned drained = 64; memcpy(state + 0xce4, &drained, 4); }
    duo_calc((char *)p, st, fn_null);
    check(at(0xce0) == 66 && state[0x2e0] == 0 && state[0x2e0 + 40] == 1, "after the drain the next calculation retries the layout and its pane");

    // The same layout calculated again on a later present is a new record: a layout cache that reuses the root's address cannot silence the trace
    state_reset(0.0f, 1.932f);
    *(unsigned *)(state + 0x240) = 1;
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_BG", fn_draw, 1920, 1080, 0, 0);
    duo_calc((char *)p, st, fn_null);
    duo_calc((char *)p, st, fn_null);
    check(at(0xce0) == 2, "a second calculation in the same present records nothing new");
    { unsigned later = 101; memcpy(state + 0xa8, &later, 4); }
    duo_calc((char *)p, st, fn_null);
    check(at(0xce0) == 4, "a calculation on a later present records the layout again");

    // A full-screen pane off centre is a container, not an edge widget: the edge rule never moves it
    state_reset(335.5f, 1.932f);
    lay = layout_new(1920, 1080);
    p = child_new(lay, "P_Off", fn_draw, 1920, 1080, 0, 500);
    duo_calc((char *)p, st, fn_null);
    check(getf(p, 0x8c) == 500.0f, "a full-screen pane off centre stays where the layout put it");

    printf("== %d checks, %d failed ==\n", cases, fails);
    return fails != 0;
}
