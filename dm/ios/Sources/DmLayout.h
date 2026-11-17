// Where the iOS front end puts the game's picture and the touch controls (front_ios.m lays the views out, DmGamepad.m its controls): pure functions of the view's bounds,
// its safe area and an iPhone Duo's fold (UIKit's division region, which includes its interactive margins), so the Duo harness tests them without a device (dm/ios/tests/duo).
#pragma once
#import <UIKit/UIKit.h>

NS_ASSUME_NONNULL_BEGIN

// game: the picture. companion: the band under it, where a phone standing up shows what the game's own screens cannot. controls: the pad's area, which in
// a split is the whole band, floating over the panel. Zero and unused where separated is NO. Standing up with no fold the band starts where the picture
// ends and runs to the bottom of the window; the pad covers all of it, so a touch on no key goes through to the panel below. A Duo's half-open lower
// screen keeps the same one rectangle for both. DmGamepad.m owns the band's hit testing: a touch on a key goes to the pad, one off the keys to the panel.
typedef struct { CGRect game, companion, controls; BOOL separated, topSafeOnly, customControlsFullBounds, onTV; } DmLayout;

// The iPhone Duo and iPad paths are retired: only ordinary iPhones (iPhone 17, iPhone 16 Pro Max) run here.
static inline BOOL dm_layout_custom_controls_full_bounds(CGRect bounds, BOOL hingeClosed) {
    (void)bounds; (void)hingeClosed;
    return NO;
}

static inline UIEdgeInsets dm_game_safe_insets(CGRect bounds, UIEdgeInsets insets, BOOL duo) {
    (void)bounds; (void)duo;
    return insets;
}

// The picture's width / height before the game's first present (front.h front_picture_shape gives the real one: a taller shape under DM_SCREEN_ASPECT, 1920x1344)
#define DM_PICTURE_16_9 (16.0 / 9.0)

// The largest centered rectangle of the picture's shape (aspect = width / height) in b. An aspect of 0 is 16:9 (the convention of DM_SCREEN_ASPECT, the
// launcher's "auto" and the guest's own shape).
static inline CGRect dm_aspect_fit(CGRect b, CGFloat aspect) {
    CGFloat w = b.size.width, h = b.size.height;
    if (aspect <= 0) aspect = DM_PICTURE_16_9;
    if (w <= 0 || h <= 0) return CGRectZero;
    if (w / h > aspect) w = h * aspect; else h = w / aspect;
    return CGRectMake(floor(CGRectGetMidX(b) - w / 2), floor(CGRectGetMidY(b) - h / 2), w, h);
}

// Picture "Fill" (DM_PICTURE_FIT=fill, a launcher and in-game menu setting): the smallest centered rectangle of the picture's shape that covers b. The game's picture keeps
// its shape, so what lies outside b is cropped, never stretched. Not rounded, so it covers b exactly.
static inline CGRect dm_aspect_fill(CGRect b, CGFloat aspect) {
    CGFloat w = b.size.width, h = b.size.height;
    if (aspect <= 0) aspect = DM_PICTURE_16_9;
    if (w <= 0 || h <= 0) return CGRectZero;
    if (w / h > aspect) h = w / aspect; else w = h * aspect;
    return CGRectMake(CGRectGetMidX(b) - w / 2, CGRectGetMidY(b) - h / 2, w, h);
}

static inline BOOL dm_picture_fill(const char *_Nullable value) { return value && !strcmp(value, "fill"); }   // DM_PICTURE_FIT: fit (default) or fill

// Picture shape "auto" (the launcher's Match screen): the window's own shape when it is wider than 16:9 (a 19.5:9 phone lying down at 2.17:1), else 16:9.
// A phone standing up asks for 1.43 as well (1920x1344, 1280x896): a game's screen-shape stubs translate the shape into its own sizes, and the shapes a game
// accepts are the ones the adapter was built for (ACNH's adapter follows DM_SCREEN_ASPECT from 1.3 to 1.77; a square or a 0.92 portrait one crashes it).
// The range is loader.c's (0.9 to 2.4). 0 = 16:9.
#define DM_PICTURE_PORTRAIT 1.43f   // the shape a standing phone asks for: the adapter's own ratio (1920x1344)

static inline float dm_picture_shape_auto(CGSize window, CGFloat topInset) {
    (void)topInset;
    if (window.height > window.width) return DM_PICTURE_PORTRAIT;   // standing up: the adapter's taller shape
    double r = MAX(window.width, window.height) / MAX(MIN(window.width, window.height), 1);
    return r >= 0.9 && r < 2.4 ? (float)r : 0.0f;
}

// The iPhone Duo's fold is retired: an ordinary iPhone splits with no division region.
static inline CGRect dm_horizontal_division(CGRect b, CGRect division) {
    (void)b; (void)division;
    return CGRectNull;
}

static inline DmLayout dm_layout(CGRect b, UIEdgeInsets insets, BOOL partiallyOpen, CGRect division, CGFloat aspect) {
    DmLayout l = { dm_aspect_fit(b, aspect), CGRectZero, b, NO, NO, NO, NO };
    if (CGRectIsEmpty(b)) return l;
    division = dm_horizontal_division(b, division);
    BOOL portrait = b.size.height > b.size.width;
    if (!portrait && (!partiallyOpen || CGRectIsNull(division))) return l;
    // A phone standing up with no fold splits the screen at its midpoint: the picture takes the upper half, the panel the lower half. The guest's
    // picture keeps its own shape (16:9), which does not fit that half, so the picture is cropped to fill it (dm_aspect_fill): the sides are cut, never
    // stretched. A fold sets both edges within the safe area.
    BOOL folded = NO;   // the iPhone Duo's fold is retired: a split is the phone standing up, with no fold
    CGRect safe = UIEdgeInsetsInsetRect(b, insets);
    CGFloat upperBottom = folded ? CGRectGetMinY(division) : CGRectGetMidY(b);
    CGFloat bandTop = folded ? CGRectGetMaxY(division) : upperBottom;
    CGRect upper = folded ? CGRectMake(CGRectGetMinX(safe), CGRectGetMinY(safe), safe.size.width, upperBottom - CGRectGetMinY(safe))
                          : CGRectMake(CGRectGetMinX(b), CGRectGetMinY(b), b.size.width, upperBottom - CGRectGetMinY(b));
    CGRect lower = CGRectMake(CGRectGetMinX(b), bandTop, b.size.width, CGRectGetMaxY(b) - bandTop);
    if (upper.size.height <= 0) return l;
    // A standing phone crops the guest's picture to fill the upper half, so the picture takes half the screen. The picture is wider than the upper half
    // (16:9 in a tall half), so its sides are cropped: the guest keeps its shape, nothing is stretched. A fold keeps the picture fitted, so it stays whole.
    l.game = (portrait && !folded && aspect > 0 && aspect < DM_PICTURE_16_9) ? dm_aspect_fill(upper, aspect) : dm_aspect_fit(upper, aspect);
    l.companion = lower; l.controls = lower; l.separated = YES;
    return l;
}

// The picture is on a TV (front_ios.m DmTVSceneDelegate): the controls take the whole view and no touch goes to the game's touch screen (the game is docked there).
static inline DmLayout dm_layout_pad_only(CGRect b) { return (DmLayout){ CGRectZero, CGRectZero, b, NO, NO, NO, YES }; }

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
int dm_gamepad_controller_connected(void);   // a physical controller is in use: the touch gamepad is off (DmGamepad.m padsChanged)

NS_ASSUME_NONNULL_END
