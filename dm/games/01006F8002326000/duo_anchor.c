// The iPhone Duo's taller layout space (DM_SCREEN_ASPECT, Animal Crossing: New Horizons 3.0.3). nn::ui2d's Pane: parent +0x18, child list +0x20 (first child
// at +0x28), translation +0x30, scale +0x48, size +0x50, flags +0x58, global matrix 3x4 row-major +0x70 (scale +0x70 / +0x84, translation +0x7c / +0x8c), name +0xb0.
// duo_calc runs from Pane::Calculate (main+0x27d4c0) right after a pane's global matrix has been computed (main+0x27d570) and before its children's are, so what it does to
// a pane's matrix its children and text follow. The library recomputes a global matrix only when the pane or a parent changed, so a shift is added once per computation:
// - a widget at the layout's top or bottom edge moves out by the extra half-height with everything in it, so it sits at the screen's edge again.
// Layouts placed by their root (balloons over characters, set at a point of the 3D scene) are left alone.
// The game's panes are its own classes (no RTTI): what draws is told by the vtable's DrawSelf (slot 21), which null panes, alignments, captures and parts keep from
// nn::ui2d::Pane (main+0x27e2c8, an empty function; fn_null_draw is its address).
// Background cover has one owner: the draw-time backdrop (view.s _dm_view_backdrop), which expands a full-screen image quad by the picture's ratio and
// extends its UVs with the quad. This function never scales a background; it traces the pane classes that hook consumes.
// st: the parameters (state page + 0x80), in 720 units: st[0] the shift (layout half-height - 360; 0 = off), st[1] the vertical stretch (layout height / 720), st[2] / st[3]
// unused here, st[4] / st[5] / st[6] a traced backdrop candidate's width / height / tallest. Compiled to position-independent code with no data (constants are immediates)
typedef unsigned int u32;
static inline float fab(float x) { return x < 0 ? -x : x; }
static inline float f(const char *p, int off) { return *(const float *)(p + off); }
static inline const char *parent(const char *p) { return *(const char *const *)(p + 0x18); }
static inline int draws(const char *p, const void *fn_null_draw) { return (*(const void *const *const *)p)[21] != fn_null_draw; }
static inline int moved(const char *p) { float tx = f(p, 0x30), ty = f(p, 0x34); return tx * tx + ty * ty > 0.25f; }
// a pane that only holds others: one that draws nothing and is smaller than 64 either way (the null panes the game's animations hang widgets on, 30 x 30 / 30 x 40, and
// alignments, 0 x 0), or one as large as the layout (Footer's part L_Footer, 3020 x 1208, whose bar sits at the bottom). A part that draws nothing itself but has a size
// (the main screen's road button, 360 x 360) is a widget of its own
static inline int container(const char *p, const void *fn_null_draw, const float *st, float u) {
    float w = fab(f(p, 0x50) * f(p, 0x70)), h = fab(f(p, 0x54) * f(p, 0x84));
    if (w >= st[4] * u && h >= st[5] * u) return 1;
    return !draws(p, fn_null_draw) && (w < 64 * u || h < 64 * u);
}

// A key of the game's text-entry keyboard (the panes named "L_BtnNml_01" and on), in the name a pane holds inline
static inline int key(const char *p) { return *(const unsigned long long *)(p + 0xb0) == 0x6C6D4E6E74425F4CULL; }

// The language list the keyboard's globe key opens is a layout of its own: it takes the keys away and draws
// its own background mask ("N_BgMask", 20 x 20) and radio marks while it is up. Its panes are calculated on
// every present, so they hold the keyboard's place while the keys are gone
// The text-entry dialog's name card ("W_BaseL_01"): the dialog's first pane, calculated when the dialog is created,
// before its keys, so it arms the dock from the dialog's first frame. The dock's own stamp is at st + 0x170 (the
// state page's + 0x1f0), which view.s holds for a moment after the last signal
static inline int card(const char *p) { return *(const unsigned long long *)(p + 0xb0) == 0x5F4C657361425F57ULL; }

static inline int menu(const char *p) {
    const unsigned long long nm = *(const unsigned long long *)(p + 0xb0);
    return nm == 0x6B73614D67425F4EULL || nm == 0x0000000000525F4EULL;   // "N_BgMask", "N_R"
}

// The composition trace (DM_SCREEN_TRACE, state page +0x240; loader.c writes the flag and dm_screen_trace_poll drains the ring at each present). Off by default: one
// load per calculated pane. A record is 40 bytes in the ring at st + 0x260 (state +0x2e0, 64 records): kind, flags, the root's low address, the global width / height
// and centre offset in 720 units, the pane's first eight name bytes, and the class the rules chose. A layout is recorded once per present it is calculated in, and its
// panes once each in that present: a table of eight pane addresses at st + 0x220 (state +0x2a0) holds them, and the layout's root and present at st + 0xc68/+0xc70.
// Head and tail sit at st + 0xc60/+0xc64, clear of the keyboard and language stamps the view keeps at st + 0x1f0..st + 0x200. A record the full ring could not take
// marks nothing, so the next calculation retries it. Records only, no host call.
static int trace_rec(const float *st, unsigned kind, unsigned flags, unsigned root, float w, float h, float x, float y, const char *name, unsigned cls) {
    char *bp = (char *)st;
    unsigned head = *(volatile unsigned *)(bp + 0xc60), tail = *(volatile unsigned *)(bp + 0xc64);
    if (head - tail >= 64) return 0;   // the host drains each present; a full ring drops the record, never overwrites one being read
    char *r = bp + 0x260 + (head & 63) * 40;
    *(volatile unsigned *)(r + 0) = kind;
    *(volatile unsigned *)(r + 4) = flags;
    *(volatile unsigned *)(r + 8) = root;
    *(volatile float *)(r + 12) = w;
    *(volatile float *)(r + 16) = h;
    *(volatile float *)(r + 20) = x;
    *(volatile float *)(r + 24) = y;
    *(volatile unsigned *)(r + 28) = *(const unsigned *)(name);
    *(volatile unsigned *)(r + 32) = *(const unsigned *)(name + 4);
    *(volatile unsigned *)(r + 36) = cls;
    __atomic_store_n((unsigned *)(bp + 0xc60), head + 1, __ATOMIC_RELEASE);
    return 1;
}
// Whether the present's table holds the pane: 1 for yes, 2 when its eight slots are all other panes (a menu's decoration, not its composition), 0 when there is room.
// Only a record the ring took is remembered, so a dropped one is retried on the next calculation
static int trace_lookup(const float *st, const char *pane) {
    char *bp = (char *)st;
    for (int i = 0; i < 8; i++) if (*(const void *const *)(bp + 0x220 + 8 * i) == (const void *)pane) return 1;
    for (int i = 0; i < 8; i++) if (!*(const unsigned long long *)(bp + 0x220 + 8 * i)) return 0;
    return 2;
}
static void trace_remember(const float *st, const char *pane) {
    char *bp = (char *)st;
    for (int i = 0; i < 8; i++) {
        volatile unsigned long long *slot = (volatile unsigned long long *)(bp + 0x220 + 8 * i);
        if (!*slot) { *slot = (unsigned long long)pane; return; }
    }
}
// Root name and size, the space its distances are in, whether the layouts' fit is on (view.s Lscreen_projection, bit 11), the last projection it accepted, and
// the picture's ratio, once per present the layout is calculated in. The pane records of a layout follow it. flags: 1 = the 1920 space, 2 = the fit is on,
// 4 = the last UI projection was accepted, 8 = the shift is 0 (a 16:9 picture, or the loader's cleared shift on a phone shape), ratio x 100 in bits 8..15,
// 1024 = the app-canvas background cover is on
static void trace_layout(const float *st, const char *root, float rw, float rh, float u, const unsigned *sites, unsigned present) {
    char *bp = (char *)st;
    volatile unsigned long long *last = (volatile unsigned long long *)(bp + 0xc68);
    if (*(const void *const *)last == (const void *)root && *(const volatile unsigned *)(bp + 0xc70) == present) return;
    const float ratio = *(const float *)(bp - 0x28);
    unsigned hundred = ratio > 0 ? (unsigned)(ratio * 100.0f + 0.5f) : 0;
    if (hundred > 255) hundred = 255;
    unsigned flags = (u > 1.25f ? 1u : 0u) | ((*sites & (1u << 11)) ? 2u : 0u) | (*(const volatile unsigned *)(bp + 0x1cc) ? 4u : 0u)
                   | (st[0] == 0 ? 8u : 0u) | (hundred << 8) | (*(const volatile unsigned *)(bp + 0x1d0) ? 1024u : 0u);
    if (!trace_rec(st, 0, flags, (unsigned)(unsigned long long)root, rw, rh, 0, 0, root + 0xb0, 0)) return;   // a full ring: retried, with its panes
    *last = (unsigned long long)root;
    *(volatile unsigned *)(bp + 0xc70) = present;
    for (int i = 0; i < 8; i++) *(volatile unsigned long long *)(bp + 0x220 + 8 * i) = 0;
}
// A pane at least as tall as the backdrop threshold, once per layout: size, centre, whether it is a drawing leaf, its class. The classes name what the
// draw-time backdrop consumes: a full-screen drawing leaf, or the tall-narrow leaf (the tailor's halves).
// flags: 1 leaf, 2 draws, 4 full width, 8 full height, 16 centred x, 32 centred y, 64 within the tallest limit, 128 a container, 256 the shift is 0;
// class: 0 none, 1 a full-screen leaf (BACKDROP), 3 a tall-narrow leaf (BACKDROP), 6 a container
static void trace_pane(const float *st, char *pane, const float *sp, float gw, float gh, float u, const void *fn_null_draw, const char *root) {
    if (gh < sp[5] * u || trace_lookup(st, pane)) return;
    float *g = (float *)(pane + 0x70);
    unsigned leaf = *(char *const *)(pane + 0x28) == pane + 0x20, dr = (unsigned)draws(pane, fn_null_draw);
    unsigned fullw = gw >= sp[4] * u, cx = (unsigned)(fab(g[3]) < 40 * u), cy = (unsigned)(fab(g[7]) < 40 * u);
    unsigned tall = gh <= sp[6] * u, box = (unsigned)container(pane, fn_null_draw, sp, u);
    unsigned flags = leaf | (dr << 1) | (fullw << 2) | 8u | (cx << 4) | (cy << 5) | (tall << 6) | (box << 7) | ((sp[0] == 0) << 8);
    unsigned cls = 0;
    if (fullw && tall && cx && cy) cls = leaf && dr ? 1u : 6u;
    else if (leaf && dr && cy && gh < 720 * u * sp[1] - 2) cls = 3u;
    if (trace_rec(st, 1, flags, (unsigned)(unsigned long long)root, gw / u, gh / u, g[3] / u, g[7] / u, pane + 0xb0, cls)) trace_remember(st, pane);
}
// A widget that reached the edge rule (kind 2): class 4 when it moves, 5 when it stays (docked with the keyboard, or a panel); flags 256 = a panel, 512 = docked.
static void trace_edge(const float *st, char *pane, float gw, float gh, float u, const char *root, int movedit, int panel, int docked) {
    if (trace_lookup(st, pane)) return;
    float *g = (float *)(pane + 0x70);
    unsigned flags = 2u | (panel << 8) | (docked << 9);
    if (trace_rec(st, 2, flags, (unsigned)(unsigned long long)root, gw / u, gh / u, g[3] / u, g[7] / u, pane + 0xb0, movedit ? 4u : 5u)) trace_remember(st, pane);
}

void duo_calc(char *pane, const float *st, const void *fn_null_draw) {
    const unsigned trace = *(const volatile unsigned *)((const char *)st + 0x1c0);
    const unsigned cover = *(const volatile unsigned *)((const char *)st + 0x1d0);
    const unsigned taller = st[1] > 1.05f;   // the layout is taller than 16:9's 720 units: only there can a canvas leave a gap
    if (st[0] == 0 && !trace && !(cover && taller)) return;
    const unsigned present = *(const unsigned *)((const char *)st + 0x28);
    const char *pp = parent(pane);
    float *g = (float *)(pane + 0x70);
    float gw = fab(f(pane, 0x50) * g[0]), gh = fab(f(pane, 0x54) * g[5]);
    // The layout's space: most of the game's layouts are 1920x1080 (793 of 959), the rest 1280x720 or an app canvas of 1280x1000 (the root pane's size); the root's
    // width names the space (1920 or 1280), the parameters are in 720 units
    const char *root = pp;
    for (int i = 0; i < 48 && root && parent(root); i++) root = parent(root);
    const char *rf = root ? root : pane;
    float u = f(rf, 0x50) >= 1900 ? 1.5f : 1.0f, d = st[0] * u;
    // The sites' mask (state +0x54) sits 0x2c below the parameters; the trace reports whether the layouts' fit (bit 11) is on
    const unsigned *sites = (const unsigned *)((const char *)st - 0x2c);
    if (trace) trace_layout(st, rf, f(rf, 0x50), f(rf, 0x54), u, sites, present);
    // The panes the draw-time backdrop would cover are named even on the picture shapes, before the early return: this is the classification a Mac run reads back
    if (trace) trace_pane(st, pane, st, gw, gh, u, fn_null_draw, root);
    // The app's own canvas background: a full-screen leaf wider than the layout whose height is short of the picture (the map's, the critterpedia's
    // and the passport's 1280x1000 canvases at a taller picture). The draw-time backdrop cannot cover it -- that hook takes a quad drawn at its
    // texture's own size, and these leaves are drawn scaled -- so the leaf's matrix grows uniformly about its centre until the canvas fills the
    // picture's height. Only a background leaf scales; a UI pane and its text are never full-width and centred at once.
    if (cover && taller && st[1] * 720.0f > gh / u + 40) {
        const float lw = f(rf, 0x50) / u;
        unsigned leaf = *(char *const *)(pane + 0x28) == pane + 0x20, dr = (unsigned)draws(pane, fn_null_draw);
        unsigned fullw = gw >= st[4] * u, wide = gw / u > lw * 1.1f;
        unsigned tall = gh <= st[6] * u, cx = (unsigned)(fab(g[3]) < 40 * u), cy = (unsigned)(fab(g[7]) < 40 * u);
        if (leaf && dr && fullw && wide && tall && cx && cy) {
            const float k = st[1] * 720.0f / (gh / u);
            g[0] *= k;
            g[5] *= k;
        }
    }
    // A 16:9 picture has no composition to change; the trace above still names its layouts and panes
    if (st[0] == 0) return;
    // The keyboard's keys mark the keyboard as up (state +0x30, the present counter at that moment), and while it is up
    // every layout's own panes (the direct children of a root) move down by the picture's extra half-height, everything in
    // them following: the name card, the keys and the bottom bar (globe / ABC / Space / Confirm) stay one piece at the
    // picture's bottom, with the scene above them. The edge rule below must then leave the keyboard's bar where the layout
    // has it. The row of button hints is a layout of its own, and keeps the place that layout gives it, over the keys
    if (key(pane)) {
        *(volatile unsigned *)((char *)st + 0x30) = present;
        *(volatile unsigned *)((char *)st + 0x1f8) = present;
    } else if (menu(pane)) {
        *(volatile unsigned *)((char *)st + 0x1f4) = present;
    } else if (card(pane)) {
        *(volatile unsigned *)((char *)st + 0x170) = present;
    }
    const unsigned stamp = *(const unsigned *)((const char *)st + 0x30);
    const int keyboard = stamp && present - stamp <= 2;
    // The language list takes the keys away and keeps the projection docked (view.s), so the whole interface stays at
    // the picture's bottom: its panes need no move of their own
    const unsigned mstamp = *(volatile unsigned *)((const char *)st + 0x1f4);
    const int menuup = mstamp && present - mstamp <= 4;
    if (!pp) return;
    if (!parent(pp)) return;
    // A pane the root's own shift carried: the layout now ends below the display's bottom edge
    for (const char *a = pane; a; a = parent(a)) {
        const float *ag = (const float *)(a + 0x70);
        if (ag[7] - fab(f(a, 0x54) * ag[5]) * 0.5f < -(st[5] * u * 0.5f) - 100 * u) return;
    }
    // A widget is the topmost pane of its chain that is not a container. Null panes are where the game's animations hang widgets, anywhere: the version label sits under
    // a null pane at the bottom (Version's N_InOut_00, -430) and is placed 908 above it, so moving the top pane with a translation took the label down to the middle
    if (container(pane, fn_null_draw, st, u)) return;
    const char *p = pp;
    for (int i = 0; i < 48; i++) {
        const char *par = parent(p);
        if (!par) { if (moved(p)) return; break; }   // p is the root: a layout placed by its root (a balloon over a character) is not on the screen's edges
        if (!container(p, fn_null_draw, st, u)) return;   // a widget above: the widget's top is further up
        p = par;
    }
    // A widget as wide as the layout is a panel, not one of the edge widgets this rule places: the keyboard's backing
    // (1240 of 1920 wide, across the dialog's bottom) is part of the dialog, and moving it alone takes the bottom row
    // away from the keys above it, which sit in the layout's middle and stay. It stays with the rest of the dialog
    if (gw >= st[4] * u * 0.6f) {
        if (trace) trace_edge(st, pane, gw, gh, u, root, 0, 1, 0);
        return;
    }
    float *gy = g + 7;
    // In 720 units (x 1.5 in the 1080 space): 230 for a widget towards a corner (|x| 400 or more: the top right's road and album buttons at 420 and 389, the bottom left's
    // clock at -416, the bottom right's minimap at -354, the title's version and settings lines), 270 for one nearer the middle (the bottom's button guides at -486 and
    // -540, the title's press-A line at -414, the timer at 450) so the title logo's upper line (+352) stays with the rest of the logo; not the room guide at the left
    // (256). Its own distances, not DM_SCREEN_HUD's (Breath of the Wild's 280 left the album button behind)
    float edge = (fab(g[3]) >= 400 * u ? 230 : 270) * u;
    // While the keyboard or the language list is up the whole UI docks down by the picture's extra half-height, so a
    // widget near the bottom must stay where the layout has it: moving it as well would take it below the picture
    int docked = *gy < 0 && (keyboard || menuup);
    if (fab(*gy) >= edge) {
        if (trace) trace_edge(st, pane, gw, gh, u, root, !docked, 0, docked);
        if (!docked) *gy += *gy > 0 ? d : -d;
    }
}
