// Where the iOS front end puts the game's picture and the touch controls (front_ios.m lays the views out, DmGamepad.m its controls): pure functions of the view's bounds,
// its safe area and an iPhone Duo's fold (UIKit's division region, which includes its interactive margins), so the Duo harness tests them without a device (dm/ios/tests/duo).
#pragma once
#import <UIKit/UIKit.h>
#include "game_profile.h"   // DM_GAME_SCREEN_UNION: the one device dm_picture_shape_auto follows beyond main's range

NS_ASSUME_NONNULL_BEGIN

// game: the picture. companion: the band under it, where a phone standing up shows what the game's own screens cannot (the pad floats over it, so the
// panel is only where no key is drawn). controls: the pad's area, which in a split is the companion's band. Zero and unused where separated is NO.
// A split's companion and controls are the same rectangle: DmGamepad.m owns the band's hit testing, taking its controls' touches and the home
// indicator's strip (where the triple tap switches 3D and Tap); a touch on no control passes to the panel below it.
typedef struct { CGRect game, companion, controls; BOOL separated, topSafeOnly; } DmLayout;

static inline UIEdgeInsets dm_game_safe_insets(CGRect bounds, UIEdgeInsets insets, BOOL duo) {
    return duo && bounds.size.height > bounds.size.width ? UIEdgeInsetsMake(insets.top, 0, 0, 0) : insets;
}

// The picture's width / height before the game's first present (front.h front_picture_shape gives the real one: a taller shape under DM_SCREEN_ASPECT, 1920x2088 at 0.92)
#define DM_PICTURE_16_9 (16.0 / 9.0)

// The largest centered rectangle of the picture's shape (aspect = width / height) in b
static inline CGRect dm_aspect_fit(CGRect b, CGFloat aspect) {
    CGFloat w = b.size.width, h = b.size.height;
    if (w <= 0 || h <= 0 || aspect <= 0) return CGRectZero;
    if (w / h > aspect) w = h * aspect; else h = w / aspect;
    return CGRectMake(floor(CGRectGetMidX(b) - w / 2), floor(CGRectGetMidY(b) - h / 2), w, h);
}

// Picture "Fill" (DM_PICTURE_FIT=fill, a launcher and in-game menu setting): the smallest centered rectangle of the picture's shape that covers b. The game's picture keeps
// its shape, so what lies outside b is cropped, never stretched. Not rounded, so it covers b exactly.
static inline CGRect dm_aspect_fill(CGRect b, CGFloat aspect) {
    CGFloat w = b.size.width, h = b.size.height;
    if (w <= 0 || h <= 0 || aspect <= 0) return CGRectZero;
    if (w / h > aspect) h = w / aspect; else w = h * aspect;
    return CGRectMake(CGRectGetMidX(b) - w / 2, CGRectGetMidY(b) - h / 2, w, h);
}

static inline BOOL dm_picture_fill(const char *_Nullable value) { return value && !strcmp(value, "fill"); }   // DM_PICTURE_FIT: fit (default) or fill

// Picture shape "auto" (the launcher's Match screen): for the one device the adapter follows beyond main's taller-screen range, a 19.5:9 phone,
// the shape of the area the picture fills. Standing up that is the upper half's shape (twice the screen's width / its height: 0.92); lying down
// the screen's own wide shape (2.17). Every other window keeps main's answer, the screen's long side over its short one from 1.3 to 1.77 (an
// iPad's 4:3, an unfolded Duo's 1.43). The bands are loader.c's. 0 = 16:9.
static inline float dm_picture_shape_auto(CGSize window) {
#if DM_GAME_SCREEN_UNION
    if (window.height > window.width) {
        double half = 2 * window.width / MAX(window.height, 1);   // standing up: the upper half's shape
        if (half >= 0.91 && half < 0.93) return (float)half;
    } else {
        double wide = MAX(window.width, window.height) / MAX(MIN(window.width, window.height), 1);
        if (wide >= 2.15 && wide < 2.19) return (float)wide;
    }
#endif
    double r = MAX(window.width, window.height) / MAX(MIN(window.width, window.height), 1);   // main's shape: a screen taller than 16:9
    return r >= 1.3 && r < 1.77 ? (float)r : 0.0f;
}

// Whether the picture's shape changes as the phone turns: only the 19.5:9 phone's two shapes above.
static inline BOOL dm_picture_shape_follows(CGSize window) {
    float r = dm_picture_shape_auto(window);
    return (r >= 0.91f && r < 0.93f) || (r >= 2.15f && r < 2.19f);
}

static inline CGRect dm_horizontal_division(CGRect b, CGRect division) {
    if (CGRectIsNull(division) || division.size.width <= division.size.height) return CGRectNull;
    // Preserve zero-height inactive folds and clip regions during display handoff.
    CGFloat x = MAX(CGRectGetMinX(b), CGRectGetMinX(division));
    CGFloat y = MAX(CGRectGetMinY(b), CGRectGetMinY(division));
    CGFloat right = MIN(CGRectGetMaxX(b), CGRectGetMaxX(division));
    CGFloat bottom = MIN(CGRectGetMaxY(b), CGRectGetMaxY(division));
    if (right - x < b.size.width * 0.9 || bottom < y ||
        y <= CGRectGetMinY(b) || bottom >= CGRectGetMaxY(b)) return CGRectNull;
    return CGRectMake(x, y, right - x, bottom - y);
}

static inline DmLayout dm_layout(CGRect b, UIEdgeInsets insets, BOOL partiallyOpen, CGRect division, CGFloat aspect) {
    DmLayout l = { dm_aspect_fit(b, aspect), CGRectZero, b, NO, NO };
    if (CGRectIsEmpty(b)) return l;
    division = dm_horizontal_division(b, division);
    BOOL portrait = b.size.height > b.size.width;
    if (!portrait && (!partiallyOpen || CGRectIsNull(division))) return l;
    // A phone standing up with no fold splits the screen at its midpoint: the picture takes the upper half, the panel the lower half. Picture shape
    // "auto" asks for that half's own shape, so the fitted picture fills it exactly; a shape that does not match (a manual DM_SCREEN_ASPECT) keeps its
    // whole picture and leaves bars instead of cropping it. A fold sets both edges within the safe area.
    BOOL folded = !CGRectIsNull(division);
    CGRect safe = UIEdgeInsetsInsetRect(b, insets);
    // Where the picture's part ends and the band begins. A phone standing up with no fold splits at the midpoint, so the picture takes the upper half
    // and the band the lower one; with "auto" the picture's shape is that half's, so it fills it with no bars. A fold sets both edges.
    CGFloat upperBottom = folded ? CGRectGetMinY(division) : CGRectGetMidY(b);
    CGFloat bandTop = folded ? CGRectGetMaxY(division) : upperBottom;
    CGRect upper = folded ? CGRectMake(CGRectGetMinX(safe), CGRectGetMinY(safe), safe.size.width, upperBottom - CGRectGetMinY(safe))
                          : CGRectMake(CGRectGetMinX(b), CGRectGetMinY(b), b.size.width, upperBottom - CGRectGetMinY(b));
    CGRect lower = CGRectMake(CGRectGetMinX(b), bandTop, b.size.width, CGRectGetMaxY(b) - bandTop);
    if (upper.size.height <= 0 || CGRectGetMaxY(safe) <= CGRectGetMinY(lower)) return l;
    // The picture keeps its whole shape: with "auto" that shape is the upper half's, so the fitted rectangle fills it exactly. Nothing is stretched.
    l.game = dm_aspect_fit(upper, aspect);
    l.companion = lower; l.controls = lower; l.separated = YES;
    return l;
}

// The picture is on a TV (front_ios.m DmTVSceneDelegate): the controls take the whole view and no touch goes to the game's touch screen (the game is docked there).
static inline DmLayout dm_layout_pad_only(CGRect b) { return (DmLayout){ CGRectZero, CGRectZero, b, NO, NO }; }

// ---- A standing phone's controls ----
// The touch pad of a portrait split (DmGamepad.m): two rows in the band, the left stick and the ABXY diamond in the upper one, the D-pad and the
// right stick in the lower one, each in its own column. The rows are at 50 % and 80 % of the band, which leaves the top of the band clear for the
// companion panel and keeps the lower row clear of the home indicator; the columns at a quarter and three quarters of its width, mirrored about
// its middle, so neither thumb's pair is squeezed against the other.
#define DM_PAD_PORTRAIT_ROW_TOP 0.50
#define DM_PAD_PORTRAIT_ROW_LOW 0.80
#define DM_PAD_PORTRAIT_COL_LEFT 0.25
#define DM_PAD_PORTRAIT_COL_RIGHT 0.75
// The sizes of that pad, in u, shared by the pad that draws them (DmGamepad.m) and the unit below.
//
// A profile's later generation (DM_GAME_PORTRAIT_PAD) draws every gap between neighbouring keys at DM_PAD_PORTRAIT_GAP_U: the ABXY keys are sized so
// their outer edge sits on the stick's ring with that gap between them, the D-pad's arms are spaced by it, and the shoulders are that far apart with
// the stick clicks on the line between them. The first generation keeps the overlay's key distance and takes the D-pad out to the diamond's reach.
#define DM_PAD_SPLIT_SCALE 1.25
#define DM_PAD_STICK_R_U 77
#define DM_PAD_PORTRAIT_GAP_U 14.2   // between neighbouring keys -- the shoulders, the stick clicks, the ABXY keys and the D-pad's arms (10 points at
                                     // the unit the two rows give a 390 x 844 band, u = 0.704)
#define DM_PAD_DPAD_ARM_RATIO (17.0 / 48.0)
#define DM_PAD_DPAD_SCALE 0.85       // the D-pad's arm against the stick's ring
typedef struct { CGFloat faceGap, faceR, dpadR, armW, armIn; } DmPadPortraitSizes;   // in u
static inline DmPadPortraitSizes dm_pad_portrait_sizes(BOOL spaced) {
    if (!spaced) return (DmPadPortraitSizes){50, 27 * DM_PAD_SPLIT_SCALE, 83.75, 27, 18};
    // The ABXY keys keep the landscape's proportions -- key distance 50 u, key radius 27 u, so their outer edge is the stick's ring -- so the two
    // layouts read as one pad. The pad's gap spaces the shoulders, the stick clicks and the D-pad's arms; the diamond's own gap follows its keys.
    CGFloat dpadR = DM_PAD_DPAD_SCALE * DM_PAD_STICK_R_U;
    return (DmPadPortraitSizes){50, 27, dpadR, dpadR * DM_PAD_DPAD_ARM_RATIO, DM_PAD_PORTRAIT_GAP_U / M_SQRT2};
}
// The unit of that pad, in the same measure as the landscape's (1 = 430 points tall): the gap between the two rows, so every device gets the largest
// size its rows allow. The upper row reaches down with the diamond's lower key (the stick's ring when that is larger) and the lower one up with the
// D-pad's drawn arm or the right stick's ring; the band's width only has to keep the two columns apart.
static inline CGFloat dm_pad_portrait_unit(CGRect band, BOOL spaced) {
    CGFloat rows = (DM_PAD_PORTRAIT_ROW_LOW - DM_PAD_PORTRAIT_ROW_TOP) * band.size.height;
    CGFloat span, columns;
    if (spaced) {   // each row reaches to the stick's ring (the diamond's outer edge is that ring; the D-pad's arm stays inside it), so the
                    // gap between the rows is two rings; the width keeps the two columns apart by the same reach
        span = 2 * DM_PAD_STICK_R_U;
        columns = span;
    } else {        // the first generation's numbers, unchanged
        span = DM_PAD_STICK_R_U + hypot(83.75, 27);
        columns = hypot(83.75, 27) + (50 + 27 * DM_PAD_SPLIT_SCALE);
    }
    CGFloat byRows = (rows - 8) / span;
    CGFloat byHeight = MIN(band.size.height / 430.0, 1.3);
    CGFloat byWidth = band.size.width * 0.5 / columns;
    return MIN(MIN(byRows, byHeight), MIN(byWidth, 1.3));
}
// The lower row sits a little deeper on the later generation's pad: its D-pad and right stick move down by that share of the band.
#define DM_PAD_PORTRAIT_LOW_SHIFT 0.025
static inline CGPoint dm_pad_portrait_row(CGRect band, BOOL lower, BOOL spaced) {
    CGFloat share = lower ? DM_PAD_PORTRAIT_ROW_LOW + (spaced ? DM_PAD_PORTRAIT_LOW_SHIFT : 0) : DM_PAD_PORTRAIT_ROW_TOP;
    return CGPointMake(0, CGRectGetMinY(band) + band.size.height * share);
}
// The shoulders' own offset on the later generation's pad, as shares of the band: they sit in from the edge and down from its top.
#define DM_PAD_PORTRAIT_SHOULDER_SHIFT 0.025
// The later generation's landscape layout keeps the three clusters on one line, with the margins the shoulders' own circles keep to the screen's edges:
// the stick's ring, the diamond's outer edge and the D-pad's circle all sit DM_PAD_SPACED_EDGE_X_U from the side and DM_PAD_SPACED_EDGE_Y_U from the
// top and bottom, so the stick's and the diamond's centres are that plus their reach (77 u), and the D-pad's centre is on the same line.
#define DM_PAD_SPACED_EDGE_X_U (44.0 - 26.0)   // the shoulders' centres (44 u) less their radius (26 u)
#define DM_PAD_SPACED_EDGE_Y_U (40.0 - 26.0)
#define DM_PAD_SPACED_LOW_X_U 264.0            // the D-pad's centre from the outer edge: clear of the stick by the pad's own gap
static inline CGPoint dm_pad_portrait_column(CGRect band, BOOL right) { return CGPointMake(CGRectGetMinX(band) + band.size.width * (right ? DM_PAD_PORTRAIT_COL_RIGHT : DM_PAD_PORTRAIT_COL_LEFT), 0); }

// Fill covers the full view only in the overlay layout (landscape). A portrait or tabletop split keeps the picture fitted in its
// area above the controls, where cropping a small picture would cost more than the bars.
static inline DmLayout dm_layout_picture(DmLayout l, CGRect b, BOOL fill, CGFloat aspect) {
    if (fill && !l.separated && !CGRectIsEmpty(b)) l.game = dm_aspect_fill(b, aspect);
    return l;
}

// A point in the view as the game's touch screen sees it: normalized on the game rectangle, which with Fill is larger than the view,
// so a touch on the visible part lands where that part of the picture is. Clamped to [0, 0.999].
static inline CGPoint dm_game_touch_point(CGRect game, CGPoint p) {
    CGFloat w = game.size.width, h = game.size.height;
    if (w <= 0 || h <= 0) return CGPointZero;
    return CGPointMake(fmin(fmax((p.x - CGRectGetMinX(game)) / w, 0), 0.999), fmin(fmax((p.y - CGRectGetMinY(game)) / h, 0), 0.999));
}

UIView *dm_gamepad_view(CGRect frame);
void dm_gamepad_arrange(UIView *view, DmLayout layout);
void dm_gamepad_reset(void);
void dm_gamepad_opmode_changed(void);   // the console mode changed (the in-game menu, a TV): the Tap layout exists only in handheld mode
CGRect dm_gamepad_menu_rect(UIView *view);   // the menu gear's rectangle in the pad's own unit: the frame-rate counter sits under it
int dm_gamepad_controller_connected(void);   // a physical controller is in use: the touch gamepad is off (DmGamepad.m padsChanged)

NS_ASSUME_NONNULL_END
