// iOS front end (the iOS implementation of front.h): a Metal view of the picture's shape (16:9, or what the game presents: front_picture_shape) + a full-screen virtual gamepad layer (DmGamepad.m). Where they go is DmLayout.h's: in landscape the picture
// is centered (black bars) and the controls are drawn over it; in portrait, or on an iPhone Duo half folded, the picture sits above the controls. With a TV connected the Metal view
// is on the TV and the phone shows the pad (DmTVSceneDelegate).
// The host runs on the big-stack thread started by main.m; front_run runs the guest directly on the calling thread.
#import <UIKit/UIKit.h>
#import <UserNotifications/UserNotifications.h>
#import <QuartzCore/CAMetalLayer.h>
#import <Metal/Metal.h>
#import <AVFAudio/AVFAudio.h>
#include <errno.h>
#include <stdatomic.h>   // g_screen_hz: read on the main thread, used from the render thread (front_screen_max_fps)
#include "front.h"
#include "hle.h"
#include "dm.h"
#include "haptics_apple.h"
#include "save_sync.h"
#import "CloudSaves.h"

void dm_gamepad_active(void);
#include "game_profile.h"
#import "DmLayout.h"
#import "DmGameMenu.h"

void front_ios_boot(void);
int front_ios_boot_ok(void);
const char *front_ios_boot_message(void);   // main.m: why a build that keeps the game files off the IPA found none usable (NULL = the generic hint)
const char *front_ios_device_check(void);   // main.m: why this device cannot run the games (its GPU has no BC textures; NULL = supported)
bool front_ios_hud_enabled_from_saved_choice(void);   // main.m: the saved choice enabled Metal's HUD at launch
bool dm_show_launcher(void *view_controller, void (*done)(void));   // Launcher.swift: false = no launcher (no dm/launcher.json, or DM_LAUNCHER=0)

static CAMetalLayer *g_layer;
static void front_ios_notify_setup(void);
static void front_ios_notify_ask(void);
static void front_ios_power_watch(void);
static void screen_refresh(void);
static void front_ios_audio_watch(void);
static UILabel *front_ios_fps_label(void);

@interface DmGameView : UIView
@property (nonatomic) CGFloat maxPixelWidth;   // 0: the screen's own pixels; a TV caps the drawable at 1920 across (tv_attach)
@end
@implementation DmGameView
+ (Class)layerClass { return CAMetalLayer.class; }
- (instancetype)initWithFrame:(CGRect)f {
    if ((self = [super initWithFrame:f])) {
        [self registerForTraitChanges:@[UITraitDisplayScale.class] withHandler:^(id<UITraitEnvironment> environment, UITraitCollection *previous) {
            [(UIView *)environment setNeedsLayout];
        }];
        CAMetalLayer *l = (CAMetalLayer *)self.layer;
        l.device = MTLCreateSystemDefaultDevice(); l.pixelFormat = MTLPixelFormatBGRA8Unorm;
        l.contentsScale = MAX(self.traitCollection.displayScale, 1); l.framebufferOnly = NO; l.opaque = YES;
        self.backgroundColor = UIColor.blackColor;
    }
    return self;
}
- (void)didMoveToWindow { [super didMoveToWindow]; [self setNeedsLayout]; screen_refresh(); }   // the phone's screen or a TV's (DmTVSceneDelegate)
- (void)layoutSubviews {   // render at the pixels of the screen the view is on (an iPad window can move to another), at most maxPixelWidth across
    [super layoutSubviews];
    CAMetalLayer *l = (CAMetalLayer *)self.layer;
    UIScreen *screen = self.window.windowScene.screen;
    CGFloat scale = screen ? screen.nativeScale : MAX(self.traitCollection.displayScale, 1);
    if (self.maxPixelWidth > 0) scale = MIN(scale, self.maxPixelWidth / MAX(self.bounds.size.width, 1));
    l.contentsScale = scale;
    screen_refresh();   // an iPad window moved to another display lays out again (the display scale trait above)
    l.drawableSize = CGSizeMake(self.bounds.size.width * l.contentsScale, self.bounds.size.height * l.contentsScale);
}
@end
static void tv_attach(void);

@interface DmRootView : UIView
@end
@implementation DmRootView
+ (BOOL)requiresConstraintBasedLayout { return YES; }
- (void)updateConstraints {
#if __has_include(<UIKit/UIHingeInteraction.h>)
    if (@available(iOS 27.1, *)) {
        // Reading here registers UIKit's automatic reserved-region invalidation.
        [self reservedRegionsOfKind:UIViewReservedRegionKind.divisionRegionKind options:UIViewReservedRegionQueryOptionsIncludeInactive];
        [self setNeedsLayout];
    }
#endif
    [super updateConstraints];
}
@end

// SwiftUI's companion (ACNHCompanion.swift): a hosting controller for the game's data-driven interface. Only a build whose profile has companion.json
// defines DM_COMPANION (build_ipa.sh) and compiles the renderer, so nothing refers to this symbol in the other games' apps.
#if DM_COMPANION
extern void *dm_companion_view_controller(void);
void dm_companion_ui_state(int hidden, int physical, int presented);   // ACNHCompanion.swift: band shown, controller in use, pad hidden
void dm_companion_ui_dock_insets(double left, double right, double bottom);   // the band's parts under the safe area
extern void dm_companion_background_state(int on);
#endif

// DmGamepad.m: a 3D layout floats the pad over the band, so the band keeps only its wallpaper until Tap comes back. Always
// defined, so a build without the renderer still links the pad.
void dm_companion_ui_background(int on) {
#if DM_COMPANION
    static int last = -1;   // the band's own state, so the log names each switch once
    if (on != last) { last = on; LOG("front end: companion band %s", on ? "keeps its wallpaper alone (3D)" : "shows its page"); }
    dm_companion_background_state(on);
#else
    (void)on;
#endif
}

@interface DmRootVC : UIViewController
@property (nonatomic, strong) DmGameView *game;
@property (nonatomic, strong) UIView *companion;   // under the pad, in the band a split gives it (DmLayout.h dm_layout): what the game's own screens cannot show there
@property (nonatomic, strong) UIViewController *companionVC;   // owns the SwiftUI companion; the view alone would let the controller go
@property (nonatomic, strong) UIView *pad;
@property (nonatomic, strong) UILabel *fps;
@property (nonatomic) BOOL launched;
@property (nonatomic, strong) id tv;   // the TV scene accessory's registration (iOS 27)
@property (nonatomic) BOOL fill;   // DM_PICTURE_FIT=fill: in landscape the picture covers the screen, its edges cropped (DmLayout.h dm_layout_picture)
@property (nonatomic) BOOL partiallyOpen;   // an iPhone Duo's hinge half open (UIHingeInteraction, iOS 27.1)
@property (nonatomic) NSInteger hingeStatus;
@property (nonatomic) BOOL standing;   // the last orientation followOrientation saw: YES with the phone up
@property (nonatomic) BOOL shapeAuto;   // Picture shape "auto" resolved the area's shape: the phone may turn, so the picture follows it (dm_screen_shape)
@property (nonatomic) float lastShape;   // the shape last given to the game (0: 16:9)
@property (nonatomic) uint32_t lastPixelW, lastPixelH;   // the view's pixels last given to the game (the picture's output is capped to them)
@property (nonatomic) int modeWhenLying;   // the console mode to go back to when the phone lies down; -1 = standing, nothing to restore
@property (nonatomic, copy) NSString *layoutLog;   // the last layout line logged
#if __has_include(<UIKit/UIHingeInteraction.h>)
- (void)hingeChanged:(UIHinge *)hinge API_AVAILABLE(ios(27.1));
#endif
- (void)startGame;
- (void)showFPS:(BOOL)on;
@end
static DmRootVC *g_root;
static CGFloat g_picture_aspect = DM_PICTURE_16_9;   // the presented picture's width / height (front_picture_shape); main thread
static void launcher_done(void) { [g_root startGame]; }
@implementation DmRootVC
- (void)loadView { self.view = [DmRootView new]; }
- (void)showHint:(NSString *)text {   // why the game cannot start, centered over the black screen
    UILabel *lb = [[UILabel alloc] initWithFrame:CGRectInset(self.view.bounds, 40, 40)];
    lb.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    lb.numberOfLines = 0; lb.textAlignment = NSTextAlignmentCenter; lb.textColor = UIColor.whiteColor;
    lb.text = text; [self.view addSubview:lb];
}
- (void)viewDidLoad {
    [super viewDidLoad];
    self.view.backgroundColor = UIColor.blackColor;
#if __has_include(<UIKit/UIHingeInteraction.h>)
    if (@available(iOS 27.1, *)) {
        __weak DmRootVC *weakSelf = self;
        [self.view addInteraction:[[UIHingeInteraction alloc] initWithUpdateHandler:^(UIHingeInteraction *interaction, UIHingeInteractionUpdate *update) {
            [weakSelf hingeChanged:update.hinge];
        }]];
    }
#endif
#if DM_COMPANION
    [[NSNotificationCenter defaultCenter] addObserver:self selector:@selector(appStateChanged:) name:UIApplicationWillResignActiveNotification object:nil];
    [[NSNotificationCenter defaultCenter] addObserver:self selector:@selector(appStateChanged:) name:UIApplicationDidBecomeActiveNotification object:nil];
#endif
    front_ios_power_watch();
    front_ios_notify_setup();
    if (!front_ios_boot_ok()) {   // No game files: show a hint and do not start the host
        const char *why = front_ios_boot_message();
        [self showHint:why ? [NSString stringWithFormat:@"Game files unusable.\n%s", why] : @"Game files not found.\nPut the RomFS folder into this App's Documents as \"romfs\" (Files app), then restart."]; return;
    }
    front_ios_audio_watch();
#if defined(__IPHONE_27_0)   // compiled only with an SDK that has the scene accessory API
    if (@available(iOS 27.0, *)) {   // iOS 27 offers a TV scene only to a registered scene accessory, no longer from the Info.plist entry alone (that path stays for iOS 26)
        UISceneConfiguration *c = [[UISceneConfiguration alloc] initWithName:@"TV" sessionRole:UIWindowSceneSessionRoleExternalDisplayNonInteractive];
        c.delegateClass = NSClassFromString(@"DmTVSceneDelegate");
        self.tv = [self registerSceneAccessory:[UISceneAccessory externalNonInteractiveSceneAccessoryWithConfiguration:c]];
    }
#endif
}
#if __has_include(<UIKit/UIHingeInteraction.h>)
- (void)hingeChanged:(UIHinge *)hinge API_AVAILABLE(ios(27.1)) {
    NSInteger status = hinge ? hinge.status : UIHingeStatusUnknown;
    if (self.hingeStatus != status) {
        self.hingeStatus = status;
        LOG("front end: Duo hinge status %ld, angle %.3f", (long)status, hinge.angle);
    }
    self.partiallyOpen = status == UIHingeStatusPartiallyOpen;
    [self.view setNeedsUpdateConstraints];
    [self.view setNeedsLayout];
}
#endif
// The game view, the pad and the frame-rate counter are made once the launcher has set the switches they read (DM_SHOW_FPS, DM_METAL_HUD); the host's loading began at app launch (main.m)
- (void)startGame {
    if (self.game) return;
    self.modeWhenLying = -1;   // the first layout pass sets the mode for the orientation the game starts in
    dm_cloud_saves_env();   // DM_SAVE_SYNC from the launcher's Cloud saves pick, before the game's first save mount reads it (CloudSaves.m, save_sync.h)
    front_hud_env();   // apply the Play selection before the device check and game view (GitHub issue #840)
    const char *dev = front_ios_device_check();
    if (dev) { [self showHint:[NSString stringWithFormat:@"This device is not supported.\n%s", dev]]; return; }   // the host does not start
    {   // Picture shape "auto" (the launcher's Match screen). Resolved here, before the host writes the state page (main.c dm_fps_state_apply).
        // A build with screen-shape stubs asks for the shape of the area the picture fills, so its background covers that area with nothing cropped:
        // lying down the screen's own wide shape (a 19.5:9 phone at 2.17:1), standing up the upper half's shape (0.92), so the picture fills that half
        // and the panel takes the lower half. The console mode is not part of it: it changes the game's resolution, not the picture's shape. Physical
        // rotation changes nothing. Another build keeps the window's own shape where its long side over the short one is 1.3 to 1.77 (an iPad's 4:3,
        // an unfolded Duo's 1.43), else 16:9: a phone standing up has no stubs to draw its half. The window's, not the screen's: on an iPad under
        // Stage Manager or Split View the app does not fill the screen.
        const char *a = getenv("DM_SCREEN_ASPECT");
#if !DM_GAME_SCREEN_ASPECT
        if (a && !strcmp(a, "auto")) {
            CGSize sz = self.view.window ? self.view.window.bounds.size : UIScreen.mainScreen.bounds.size;
            double r = MAX(sz.width, sz.height) / MAX(MIN(sz.width, sz.height), 1);
            char v[16]; if (r >= 1.3 && r < 1.77) snprintf(v, sizeof v, "%.3f", r); else strcpy(v, "0");
            setenv("DM_SCREEN_ASPECT", v, 1); LOG("front end: picture shape auto: window %.0fx%.0f points (%.3f:1) → DM_SCREEN_ASPECT=%s", sz.width, sz.height, r, v);
        }
#else
        if (a && !strcmp(a, "auto")) {
            // The window's bounds are still the other shape on a cold start (the scene turns after this call). Put its edges in the order the scene's
            // orientation says, so a phone launched lying down resolves to its wide shape, not to the standing one.
            CGSize sz = self.view.window ? self.view.window.bounds.size : UIScreen.mainScreen.bounds.size;
            UIWindowScene *scene = self.view.window.windowScene;
            BOOL portrait = scene ? UIInterfaceOrientationIsPortrait(scene.interfaceOrientation) : sz.height > sz.width;
            if (!portrait && sz.width < sz.height) sz = CGSizeMake(sz.height, sz.width);
            if (portrait && sz.width > sz.height) sz = CGSizeMake(sz.height, sz.width);
            float r = dm_picture_shape_auto(sz);
            char v[16]; if (r > 0) snprintf(v, sizeof v, "%.3f", r); else strcpy(v, "0");
            setenv("DM_SCREEN_ASPECT", v, 1); LOG("front end: picture shape auto: window %.0fx%.0f points (%s) → DM_SCREEN_ASPECT=%s", sz.width, sz.height, portrait ? "standing" : "lying down", v);
            // The pixels the picture is shown in (the game view is the window's width both ways): the host caps the picture's output to them, so the game
            // draws at the screen's resolution instead of the shape's 1920-wide space -- a standing phone drew 1880x2040 into a 1206x1311 view, 2.4 times
            // the pixels, and a turn's frames were 2.2 times the lying-down ones (loader.c shape_output_capped)
            CGFloat sc = self.view.window ? self.view.window.screen.nativeScale : UIScreen.mainScreen.nativeScale;
            char px[32]; snprintf(px, sizeof px, "%ux%u", (unsigned)(sz.width * sc), (unsigned)(sz.height * sc));
            setenv("DM_SCREEN_ASPECT_PX", px, 1); LOG("front end: picture shape auto: the picture's output is capped to the window's %s pixels", px);
            self.lastPixelW = (uint32_t)(sz.width * sc); self.lastPixelH = (uint32_t)(sz.height * sc);   // the layout's first pass then has nothing to send: the host applies the boot shape with these pixels
            // The game's window textures are built once, at start: a profile whose stubs read the union size has the host make them large enough for every
            // shape its range allows, so the phone can turn while the game runs (dm_screen_shape, followShape below)
            self.shapeAuto = r > 0 && DM_GAME_SCREEN_UNION; self.lastShape = self.shapeAuto ? r : 0;
            if (self.shapeAuto) { setenv("DM_SCREEN_ASPECT_UNION", "1", 1); g_picture_aspect = r; LOG("front end: picture shape auto: the game's window holds every shape (DM_SCREEN_ASPECT_UNION), so the picture follows the phone turning"); }
        }
#endif
    }
    self.fill = dm_picture_fill(getenv("DM_PICTURE_FIT"));
    if (self.fill) LOG("front end: picture Fill (DM_PICTURE_FIT=fill), the 16:9 picture covers the screen in landscape and its edges are cropped");
    self.view.clipsToBounds = YES;   // with Fill the game view is larger than the screen
    self.view.window.traitOverrides.preferredContentSizeCategory = UIContentSizeCategorySmall;   // UIKit draws the in-game menu (GameMenu.swift) in this window: it keeps one compact text size over the game, whatever the system's text size
    self.game = [[DmGameView alloc] initWithFrame:self.view.bounds];
    [self.view addSubview:self.game];
    g_layer = (CAMetalLayer *)self.game.layer;
    if (getenv("DM_METAL_HUD") && atoi(getenv("DM_METAL_HUD"))) { g_layer.developerHUDProperties = @{@"mode": @"default"}; LOG("front end: Metal performance HUD on (DM_METAL_HUD)"); }   // Apple's HUD over the game: frame rate, frame and GPU time, memory
    else if (front_ios_hud_enabled_from_saved_choice()) g_layer.developerHUDProperties = @{@"mode": @"disabled"};   // the launcher switched off a HUD enabled by the saved choice
    dm_haptics_apple_init();
    // The band under the picture in a split (DmLayout.h dm_layout companion): the SwiftUI companion's view, with the pad floating over it. Hidden while
    // there is no split: the picture takes the whole view and the pad's own background is all there is. The companion takes its own touches; the pad keeps
    // the ones that land on a key (DmGamepad.m hitTest). A game without a companion keeps the empty placeholder.
#if DM_COMPANION
    UIViewController *companionVC = (__bridge_transfer UIViewController *)dm_companion_view_controller();
#else
    UIViewController *companionVC = nil;
#endif
    self.companionVC = companionVC;
    self.companion = companionVC ? companionVC.view : [[UIView alloc] initWithFrame:CGRectZero];
    self.companion.backgroundColor = [UIColor colorWithWhite:0.07 alpha:1];
    self.companion.hidden = YES;
    if (companionVC) {   // a child, not only a subview: SwiftUI's appearance callbacks and safe-area propagation follow containment
        [self addChildViewController:companionVC];
        [self.view addSubview:companionVC.view];
        [companionVC didMoveToParentViewController:self];
    } else {
        [self.view addSubview:self.companion];
    }
    self.pad = dm_gamepad_view(self.view.bounds);
    self.pad.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    [self.view addSubview:self.pad];
    const char *fps = getenv("DM_SHOW_FPS");
    [self showFPS:fps ? atoi(fps) != 0 : [[NSBundle.mainBundle objectForInfoDictionaryKey:@"DMShowFPS"] boolValue]];
    [self.view setNeedsLayout]; [self.view layoutIfNeeded];
#if DM_GAME_PORTRAIT_PAD
    [self showPadSwitchHint];   // this profile's pad switches on a triple tap at the bottom; say so once, at the start
#endif
    UIApplication.sharedApplication.idleTimerDisabled = YES;
    tv_attach();   // a TV connected while the launcher showed
    front_ios_boot();
}
static UIWindow *g_tv;   // the TV scene's window while one shows the picture (tv_attach)
// Console mode follows the screen's orientation while no TV shows the picture: a phone standing up is handheld, so the game's own touch screens (its software
// keyboard above all) take touches, and the picture is the handheld 1280x720 one; lying down it returns to the mode the player had. A TV docks the game
// whatever the phone does (tv_attach / tv_detach own the mode then). A choice made in the in-game menu while standing up is the one lying down returns to.
// The switch is dm_set_opmode, which pushes the console mode change notification the game watches, so it follows while it runs.
- (void)followOrientation:(CGRect)b {
    if (g_tv) return;   // the picture is on the TV: docked there
    BOOL standing = b.size.height > b.size.width;
    if (standing == self.standing) return;
    self.standing = standing;
    if (standing) {
        self.modeWhenLying = dm_opmode();
        if (self.modeWhenLying != 0) { dm_set_opmode(0); dm_gamepad_opmode_changed(); LOG("front end: console mode handheld, a phone standing up (the game's own keyboard takes touches)"); }
    } else if (self.modeWhenLying >= 0) {
        int m = self.modeWhenLying; self.modeWhenLying = -1;
        if (dm_opmode() != m) { dm_set_opmode(m); dm_gamepad_opmode_changed(); LOG("front end: console mode %s again, lying down", m ? "docked" : "handheld"); }
    }
}
#if DM_COMPANION
- (void)appStateChanged:(NSNotification *)note {   // the panel stops sampling while the app is in the background; the band is laid out again on return
    if (!self.companionVC) return;
    BOOL physical = dm_gamepad_controller_connected();
    // The notification says which transition happened: applicationState may still read Active when WillResignActive arrives.
    BOOL active = [note.name isEqualToString:UIApplicationDidBecomeActiveNotification];
    dm_companion_ui_state(physical, physical, active && !self.companion.hidden);
    [self.view setNeedsLayout];
}
#endif
- (void)viewDidLayoutSubviews {
    [super viewDidLayoutSubviews];
    CGRect b = self.view.bounds; if (!self.game) return;
    [self followOrientation:b];
    if (self.shapeAuto) {   // the picture takes the area's shape now (a phone that turned); the game's window holds every shape (DM_SCREEN_ASPECT_UNION)
        float a = dm_picture_shape_auto(b.size);
        CGFloat sc = self.view.window ? self.view.window.screen.nativeScale : UIScreen.mainScreen.nativeScale;
        uint32_t pw = (uint32_t)(b.size.width * sc), ph = (uint32_t)(b.size.height * sc);   // the game view's pixels: the window's width both ways
        BOOL turned = a > self.lastShape * 1.01f || a < self.lastShape * 0.99f;
        if (turned || pw != self.lastPixelW || ph != self.lastPixelH) {
            self.lastShape = a; self.lastPixelW = pw; self.lastPixelH = ph;
            dm_screen_shape_px(a, pw, ph);
            // This pass lays the views out with the new shape: letting the present report it first sizes the first pass's game view by the shape of the
            // area the phone just left -- with Fill that view is 2.4 times the screen for one layout pass, and the next one snaps it back
            g_picture_aspect = a > 0 ? a : DM_PICTURE_16_9;
        }
    }
    CGRect division = CGRectNull;   // an iPhone Duo's fold, as UIKit reports it (iOS 27.1)
    NSMutableArray<NSString *> *regionLog = [NSMutableArray array];
#if __has_include(<UIKit/UIHingeInteraction.h>)
    if (@available(iOS 27.1, *)) {
        for (UIViewReservedRegion *region in [self.view reservedRegionsOfKind:UIViewReservedRegionKind.divisionRegionKind options:UIViewReservedRegionQueryOptionsIncludeInactive]) {
            [regionLog addObject:[NSString stringWithFormat:@"%@ active=%d", NSStringFromCGRect(region.frame), region.isActive]];
            CGRect candidate = dm_horizontal_division(b, region.frame);
            if (CGRectIsNull(division) && !CGRectIsNull(candidate)) division = candidate;
        }
    }
#endif
    BOOL duoPortrait = b.size.height > b.size.width && (self.hingeStatus != 0 || !CGRectIsNull(division));
    UIEdgeInsets gameInsets = dm_game_safe_insets(b, self.view.safeAreaInsets, duoPortrait);
    BOOL onTV = self.game.superview != self.view;   // the TV's view lays the picture out; the phone is all pad
    DmLayout layout = onTV ? dm_layout_pad_only(b) : dm_layout_picture(dm_layout(b, gameInsets, self.partiallyOpen, division, g_picture_aspect), b, self.fill, g_picture_aspect);
    if (!onTV) self.game.frame = layout.game;
    self.companion.frame = layout.companion;
    self.companion.hidden = !layout.separated;
    layout.topSafeOnly = duoPortrait;
#if DM_COMPANION
    if (self.companionVC) {   // the renderer follows the band's presentation (and stops sampling while it is hidden), and lays its content out of the safe area
        BOOL physical = dm_gamepad_controller_connected();
        BOOL presented = layout.separated && UIApplication.sharedApplication.applicationState == UIApplicationStateActive;
        dm_companion_ui_state(physical, physical, presented);
        if (layout.separated) {
            CGRect safe = UIEdgeInsetsInsetRect(b, self.view.safeAreaInsets);
            dm_companion_ui_dock_insets(MAX(0, CGRectGetMinX(safe) - CGRectGetMinX(layout.companion)),
                                        MAX(0, CGRectGetMaxX(layout.companion) - CGRectGetMaxX(safe)),
                                        MAX(0, CGRectGetMaxY(layout.companion) - CGRectGetMaxY(safe)));
        } else {
            dm_companion_ui_dock_insets(0, 0, 0);
        }
    }
#endif
    NSString *line = [NSString stringWithFormat:@"%@, hinge %ld%s, divisions %@, picture %@, controls %@", onTV ? @"pad only (picture on the TV)" : layout.separated ? @"split" : self.fill ? @"overlay, Fill" : @"overlay",
                      (long)self.hingeStatus, self.partiallyOpen ? " (half open)" : "", regionLog, NSStringFromCGRect(layout.game), NSStringFromCGRect(layout.controls)];
    if (![line isEqualToString:self.layoutLog]) { self.layoutLog = line; LOG("front end: layout %s", line.UTF8String); }
    self.pad.frame = b;
    dm_gamepad_arrange(self.pad, layout);
    if (self.fps) {
#if DM_GAME_PORTRAIT_PAD
        // This profile's pad keeps the top row to the shoulders and − / +: the counter sits under the menu gear, centred on it.
        CGRect gear = dm_gamepad_menu_rect(self.pad);
        self.fps.frame = CGRectMake(floor(CGRectGetMidX(gear) - 32), floor(CGRectGetMaxY(gear) + 6), 64, 22);
#else
        // The top row's free stretch: right of DmGamepad.m's L3 key (102 + 17 of its units from the safe edge), level with − / + (22 units down).
        CGRect s = CGRectIntersection(layout.controls, UIEdgeInsetsInsetRect(b, gameInsets));
        CGFloat u = layout.separated ? MIN(MIN(s.size.height / 430.0, s.size.width / 700.0), 1.3) : MIN(b.size.height / 430.0, 1.3);
        self.fps.frame = CGRectMake(floor(CGRectGetMinX(s) + 130 * u), floor(CGRectGetMinY(s) + 22 * u - 11), 64, 22);
#endif
    }
}
// This profile's pad switches between the pad and the game's own touch screen on a triple tap in the bottom strip (DmGamepad.m bottomNavTap). Said
// once, when the game starts, for ten seconds: the game's own start-up screens are what the player sees first, and the strip is invisible.
- (void)showPadSwitchHint {
    UILabel *l = [UILabel new];
    BOOL chinese = [NSLocale.preferredLanguages.firstObject hasPrefix:@"zh"];
    l.text = chinese ? @"三击底部切换摇杆 / 触控" : @"Triple-tap the bottom to switch";
    l.font = [UIFont systemFontOfSize:12 weight:UIFontWeightSemibold];
    l.textColor = UIColor.whiteColor; l.textAlignment = NSTextAlignmentCenter; l.numberOfLines = 2;
    l.backgroundColor = [UIColor colorWithWhite:0 alpha:0.55]; l.layer.cornerRadius = 8; l.layer.masksToBounds = YES;
    l.userInteractionEnabled = NO;   // the strip under it still takes its taps
    // On the home indicator itself: the label's own bottom edge sits on the indicator's top edge, so it points at what it names.
    l.frame = CGRectMake(CGRectGetMidX(self.view.bounds) - 85, CGRectGetMaxY(self.view.bounds) - 13 - 28, 170, 28);
    l.autoresizingMask = UIViewAutoresizingFlexibleTopMargin | UIViewAutoresizingFlexibleLeftMargin | UIViewAutoresizingFlexibleRightMargin;
    l.alpha = 0;
    [self.view addSubview:l];
    [UIView animateWithDuration:0.25 animations:^{ l.alpha = 1; }];
    LOG("front end: pad switch hint shown (a triple tap at the bottom)");
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(10 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        [UIView animateWithDuration:0.35 animations:^{ l.alpha = 0; } completion:^(BOOL done) { (void)done; [l removeFromSuperview]; }];
    });
}
- (void)showFPS:(BOOL)on {   // the frame-rate counter: DM_SHOW_FPS at Play, then the in-game menu
    if (on && !self.fps) { self.fps = front_ios_fps_label(); [self.view addSubview:self.fps]; [self.view setNeedsLayout]; }   // above the pad, which hides while a controller is connected
    self.fps.hidden = !on;
}
- (void)viewDidAppear:(BOOL)a {
    [super viewDidAppear:a];
    if (!front_ios_boot_ok() && front_ios_boot_message() && !self.presentedViewController) {   // a slim build whose game file image is missing or another version (main.m)
        UIAlertController *a = [UIAlertController alertControllerWithTitle:@"Game files" message:@(front_ios_boot_message()) preferredStyle:UIAlertControllerStyleAlert];
        [a addAction:[UIAlertAction actionWithTitle:@"OK" style:UIAlertActionStyleDefault handler:nil]];
        [self presentViewController:a animated:YES completion:nil];
    }
    if (self.launched || !front_ios_boot_ok()) return;
    self.launched = YES; g_root = self;
    if (!dm_show_launcher((__bridge void *)self, launcher_done)) [self startGame];   // the settings screen first (Launcher.swift), when the app has one
    front_ios_notify_ask();
}
- (BOOL)prefersHomeIndicatorAutoHidden { return YES; }
- (UIRectEdge)preferredScreenEdgesDeferringSystemGestures { return UIRectEdgeAll; }   // Edge gestures need two swipes, to avoid accidental exits
- (BOOL)prefersStatusBarHidden { return YES; }
- (UIInterfaceOrientationMask)supportedInterfaceOrientations { return UIInterfaceOrientationMaskAll; }
@end
UIViewController *front_ios_root(void) { return [DmRootVC new]; }

// A TV (cable or AirPlay): iOS gives the app the TV as a scene of its own instead of mirroring the phone. The game's view moves there, 16:9 fills the TV, and the game is
// docked as a Switch on its dock (1080p, the operation mode change notification); the phone keeps the pad. Unplugging puts the view back and the mode the game had before
@interface DmTVVC : UIViewController
@end
@implementation DmTVVC
- (void)viewDidLoad { [super viewDidLoad]; self.view.backgroundColor = UIColor.blackColor; }
- (void)viewDidLayoutSubviews { [super viewDidLayoutSubviews]; for (UIView *v in self.view.subviews) v.frame = dm_aspect_fit(self.view.bounds, g_picture_aspect); }
@end
static int g_tv_mode_was = -1;   // the console mode to go back to when the TV is unplugged
static void tv_attach(void) {
    DmGameView *v = g_root.game; if (!g_tv || !v || v.superview == g_tv.rootViewController.view) return;
    // at most 1080p: the game renders 1080p docked, and a 4K drawable only adds an upscale and a 4K stream (an AirPlay TV showed black, the GPU went from 15 to 60 ms a frame).
    // Taken from the picture, not the screen, so a display of another shape still gets the full 1920 pixels across the picture (DmGameView layoutSubviews applies it)
    v.maxPixelWidth = 1920;
    [g_tv.rootViewController.view addSubview:v]; [g_tv.rootViewController.view setNeedsLayout]; [g_root.view setNeedsLayout];   // the phone's pad takes the whole screen
    CGRect f = dm_aspect_fit(g_tv.bounds, g_picture_aspect); CGFloat scale = MIN(g_tv.windowScene.screen.nativeScale, 1920.0 / MAX(f.size.width, 1));
    if (g_tv_mode_was < 0) g_tv_mode_was = dm_opmode();
    dm_set_opmode(1); dm_gamepad_opmode_changed();
    LOG("front end: TV connected (%.0fx%.0f points), the picture moves there at %.0fx%.0f, console mode", g_tv.bounds.size.width, g_tv.bounds.size.height, f.size.width * scale, f.size.height * scale);
}
static void tv_detach(void) {
    DmGameView *v = g_root.game; g_tv = nil; v.maxPixelWidth = 0;
    if (v && v.superview != g_root.view) { [g_root.view insertSubview:v atIndex:0]; [g_root.view setNeedsLayout]; }
    if (g_tv_mode_was >= 0) { dm_set_opmode(g_tv_mode_was); g_tv_mode_was = -1; dm_gamepad_opmode_changed(); }
    LOG("front end: TV disconnected, the picture is back on the phone");
}
void front_picture_shape(int w, int h) {   // front.h: the views take the presented picture's shape (a taller one under DM_SCREEN_ASPECT) instead of 16:9
    if (w <= 0 || h <= 0) return;
    CGFloat a = (CGFloat)w / h;
    dispatch_async(dispatch_get_main_queue(), ^{
        if (a == g_picture_aspect) return;
        g_picture_aspect = a; LOG("front end: picture shape %dx%d, the game's view follows it", w, h);
        [g_root.view setNeedsLayout]; if (g_tv) [g_tv.rootViewController.view setNeedsLayout];
    });
}

// The settings the in-game menu offers (GameMenu.swift, from the gamepad's menu button), each taking effect while the game runs; the launcher's other settings need the next start.
// value NULL: only whether env is one of them; otherwise apply it (GameMenu.swift has saved it and put it in the environment). Console mode changes as on a Switch put in or taken out of its
// dock (the operation mode change notification); while a TV shows the picture the game stays docked, and the choice applies when the TV is unplugged
bool dm_setting_live(const char *env, const char *value) {
    if (!env || !g_root.game) return false;
    if (!strcmp(env, "DM_PICTURE_FIT")) {
        if (value) { g_root.fill = dm_picture_fill(value); [g_root.view setNeedsLayout]; LOG("front end: picture %s (in-game menu)", g_root.fill ? "Fill" : "Fit"); }
        return true;
    }
    if (!strcmp(env, "DM_OPMODE")) {
        if (!value) return true;
        int m = atoi(value) ? 1 : 0;
        if (g_tv_mode_was >= 0) { g_tv_mode_was = m; LOG("front end: console mode %s once the TV is unplugged (in-game menu)", m ? "docked" : "handheld"); return true; }
        if (g_root.standing) g_root.modeWhenLying = m;   // the player chose it while standing up: lying down keeps that choice, not the one it replaced
        dm_set_opmode(m); dm_gamepad_opmode_changed(); LOG("front end: console mode %s (in-game menu)", m ? "docked" : "handheld");
        return true;
    }
    if (!strcmp(env, "DM_SHOW_FPS")) { if (value) [g_root showFPS:atoi(value) != 0]; return true; }
    if (!strcmp(env, "DM_TOUCH_HAPTICS")) { if (value) dm_gamepad_touch_feedback(atoi(value) != 0); return true; }
    if (!strcmp(env, "DM_RUMBLE")) { if (value) { dm_haptics_set_enabled(atoi(value) != 0); LOG("front end: game rumble %s (in-game menu)", atoi(value) ? "on" : "off"); } return true; }
    return false;
}
@interface DmTVSceneDelegate : UIResponder <UIWindowSceneDelegate>
@property (strong, nonatomic) UIWindow *window;
@end
@implementation DmTVSceneDelegate
- (void)scene:(UIScene *)scene willConnectToSession:(UISceneSession *)session options:(UISceneConnectionOptions *)o {
    self.window = [[UIWindow alloc] initWithWindowScene:(UIWindowScene *)scene];
    self.window.rootViewController = [DmTVVC new];
    self.window.hidden = NO;   // without a window iOS goes on mirroring the phone; not key, so the phone's window keeps the keyboard
    g_tv = self.window; tv_attach();
}
- (void)sceneDidDisconnect:(UIScene *)s { if (g_tv == self.window) tv_detach(); self.window = nil; }
@end

// Thermal state and screen brightness for the power and present rows every 300 frames (hle_nvn.c; its weak defaults return -1 on the Mac). thermalState: 0 nominal, 1 fair, 2 serious, 3 critical.
// Brightness is read on the main thread at start and on each change; every change of the thermal state and of Low Power Mode (which lowers the CPU clock) is logged with the time since launch
static _Atomic double g_brightness = -1;
int dm_thermal_now(void) { return (int)NSProcessInfo.processInfo.thermalState; }
int dm_low_power_now(void) { return NSProcessInfo.processInfo.lowPowerModeEnabled; }   // the frame step does not probe in it (hle_nvn.c fstep_cpu_slowed)
double dm_brightness_now(void) { return g_brightness; }
static void front_ios_power_watch(void) {
    g_brightness = UIScreen.mainScreen.brightness;
    [NSNotificationCenter.defaultCenter addObserverForName:UIScreenBrightnessDidChangeNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *n) { (void)n; g_brightness = UIScreen.mainScreen.brightness; }];
    [NSNotificationCenter.defaultCenter addObserverForName:NSProcessInfoThermalStateDidChangeNotification object:nil queue:nil usingBlock:^(NSNotification *n) {
        (void)n; LOG("front end: thermal state → %d (%.0f s after launch)", dm_thermal_now(), (now_ns() - g_t0_ns) / 1e9); }];
    [NSNotificationCenter.defaultCenter addObserverForName:NSProcessInfoPowerStateDidChangeNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *n) {
        (void)n; LOG("front end: Low Power Mode → %s (%.0f s after launch)", dm_low_power_now() ? "on" : "off", (now_ns() - g_t0_ns) / 1e9); screen_refresh(); }];
    [NSNotificationCenter.defaultCenter addObserverForName:UIScreenModeDidChangeNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *n) { (void)n; screen_refresh(); }];
    screen_refresh();
    LOG("front end: thermal state %d, brightness %.2f, Low Power Mode %s, screen %d Hz", dm_thermal_now(), g_brightness, dm_low_power_now() ? "on" : "off", front_screen_max_fps());
}

// Frame-rate counter: the frames put on screen in the last second (nvn_mtl.m counts them for NVN and for the Vulkan layer alike; frames the display link merged away are not counted).
// On when the IPA was built with it (the NSP script's SHOW_FPS / --show-fps → DMShowFPS in Info.plist); DM_SHOW_FPS=0 / 1 (the launcher, Documents/env.txt) overrides that without a rebuild,
// and the in-game menu turns it on and off ([DmRootVC showFPS:])
uint64_t mtl_present_count(void);
static UILabel *front_ios_fps_label(void) {
    UILabel *l = [UILabel new];
    l.font = [UIFont monospacedDigitSystemFontOfSize:13 weight:UIFontWeightSemibold]; l.textColor = UIColor.whiteColor; l.textAlignment = NSTextAlignmentCenter;
    l.backgroundColor = [UIColor colorWithWhite:0 alpha:0.45]; l.layer.cornerRadius = 6; l.layer.masksToBounds = YES; l.userInteractionEnabled = NO;
    l.text = @"-- fps";
    __block uint64_t n0 = mtl_present_count(); __block double t0 = CACurrentMediaTime();
    [NSTimer scheduledTimerWithTimeInterval:1.0 repeats:YES block:^(NSTimer *t) {
        (void)t; uint64_t n = mtl_present_count(); double now = CACurrentMediaTime();
        if (now - t0 < 1.5) l.text = [NSString stringWithFormat:@"%.0f fps", (n - n0) / (now - t0)];   // a longer span is the time in the background: no reading
        n0 = n; t0 = now;
    }];
    LOG("front end: frame-rate counter on");
    return l;
}

// Progress screen of the prepare step (front.h front_progress): a dark view over the game's view and the pad, the title, a bar and a count, centered; UIKit views, so it draws while the game's thread waits
static UIView *g_prog; static UILabel *g_prog_count; static UIProgressView *g_prog_bar; static UILabel *g_prog_title;
void (*front_progress_hook)(const char *title, long done, long total);
void front_progress(const char *title, long done, long total) {
    if (!g_layer) return;
    NSString *tt = title ? @(title) : nil;
    void (*hook)(const char *, long, long) = front_progress_hook;
    if (hook) { dispatch_async(dispatch_get_main_queue(), ^{ hook(tt.UTF8String, done, total); }); return; }   // the host app draws it (front.h)
    dispatch_async(dispatch_get_main_queue(), ^{
        if (!tt) { [g_prog removeFromSuperview]; g_prog = nil; return; }
        if (!g_prog) {
            UIView *root = g_root.view; if (!root) return;
            g_prog = [[UIView alloc] initWithFrame:root.bounds]; g_prog.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
            g_prog.backgroundColor = [UIColor colorWithWhite:0.06 alpha:0.94];
            UIView *box = [[UIView alloc] initWithFrame:CGRectMake(floor((root.bounds.size.width - 320) / 2), floor((root.bounds.size.height - 96) / 2), 320, 96)];
            box.autoresizingMask = UIViewAutoresizingFlexibleLeftMargin | UIViewAutoresizingFlexibleRightMargin | UIViewAutoresizingFlexibleTopMargin | UIViewAutoresizingFlexibleBottomMargin;
            g_prog_title = [[UILabel alloc] initWithFrame:CGRectMake(0, 4, 320, 30)]; g_prog_title.textAlignment = NSTextAlignmentCenter; g_prog_title.textColor = UIColor.whiteColor;
            g_prog_title.font = [UIFont systemFontOfSize:20 weight:UIFontWeightSemibold];
            g_prog_bar = [[UIProgressView alloc] initWithProgressViewStyle:UIProgressViewStyleDefault]; g_prog_bar.frame = CGRectMake(20, 48, 280, 4);
            g_prog_count = [[UILabel alloc] initWithFrame:CGRectMake(0, 64, 320, 22)]; g_prog_count.textAlignment = NSTextAlignmentCenter;
            g_prog_count.textColor = [UIColor colorWithWhite:0.72 alpha:1]; g_prog_count.font = [UIFont monospacedDigitSystemFontOfSize:13 weight:UIFontWeightRegular];
            [box addSubview:g_prog_title]; [box addSubview:g_prog_bar]; [box addSubview:g_prog_count]; [g_prog addSubview:box];
            [root addSubview:g_prog];
        }
        g_prog_title.text = tt; g_prog_bar.progress = total > 0 ? (float)done / (float)total : 0;
        g_prog_count.text = [NSString stringWithFormat:@"%@ of %@", [NSNumberFormatter localizedStringFromNumber:@(done) numberStyle:NSNumberFormatterDecimalStyle],
                             [NSNumberFormatter localizedStringFromNumber:@(total) numberStyle:NSNumberFormatterDecimalStyle]];
    });
}
void *front_metal_layer(void) { return (__bridge void *)g_layer; }
int front_display_pixels(int *w, int *h) {   // the screen in pixels, landscape (nativeBounds is portrait); UIScreen's sizes may be read from any thread
    CGRect b = UIScreen.mainScreen.nativeBounds; int a = (int)b.size.width, c = (int)b.size.height;
    if (a <= 0 || c <= 0) return 0;
    *w = a > c ? a : c; *h = a > c ? c : a; return 1;
}
// The highest frame rate of the screen the game is shown on (front.h), which the 120 fps path needs at 120: hle_nvn.c high_cap_tick caps at 60 on a screen below 120 Hz,
// and the display link follows it (nvn_mtl.m link_range, link_sync). Read on the main thread, as the Mac front end does (front_mac.m screen_refresh), whenever it can change:
// at launch, when the game view moves to a window (the phone's, or a TV's: its own rate, as the picture is there) or lays out (an iPad window moved to another display),
// when the screen's mode changes and when Low Power Mode is turned on or off. Low Power Mode holds a ProMotion screen at 60 Hz, so the rate is 60 then, whatever
// maximumFramesPerSecond says. Before the game view has a window: the app's own scene (UIScreen.mainScreen is deprecated in iOS 26). 0 until read
static _Atomic int g_screen_hz;
int front_screen_max_fps(void) { return atomic_load_explicit(&g_screen_hz, memory_order_relaxed); }
static void screen_refresh(void) {
    UIWindowScene *ws = (g_root.game ?: g_root.view).window.windowScene;
    if (!ws) for (UIScene *s in UIApplication.sharedApplication.connectedScenes)
        if ([s isKindOfClass:UIWindowScene.class] && [s.session.role isEqualToString:UIWindowSceneSessionRoleApplication]) { ws = (UIWindowScene *)s; break; }
    int hz = ws ? (int)ws.screen.maximumFramesPerSecond : 0, lpm = dm_low_power_now();
    if (hz <= 0) return;   // no screen yet: keep what was read
    if (lpm && hz > 60) hz = 60;
    int was = atomic_exchange_explicit(&g_screen_hz, hz, memory_order_relaxed);
    if (was && was != hz) LOG("front end: screen %d Hz → %d Hz%s", was, hz, lpm ? " (Low Power Mode)" : "");
}
// Offline HTML pages (web_apple.m): the page's view goes over the game view and under the gamepad, which lets touches on no control through to it while the page is
// shown (DmGamepad.m hitTest); the page takes them as a click on what they land on, and the gamepad and controllers drive it as on the Switch
void front_web_attach(UIView *box) {
    UIView *v = g_root.view; box.frame = v.bounds;
    if (g_root.game) [v insertSubview:box aboveSubview:g_root.game]; else [v addSubview:box];
}
void front_web_detach(UIView *box) { (void)box; }
// An app of several programs (main.m: dm/programs.txt; DM_EXEC_OUT is set only there). iOS cannot start the app's process again, so oe::ExecuteProgram records the next program and the
// app ends (hle_oe.c); the next start runs it. To spare the player a silent quit, the app names the program on screen for 1.5 s, then schedules a local notification 1 s ahead that
// opens the app again ("Tap to start …"), then ends. Notifications are asked for at the first start of such an app; a player who refuses is asked on screen to open the app again.
// An app of one program that asks to start again (oe::RequestToRelaunchApplication: Super Smash Bros. Ultimate after a language change) cannot either: it asks the player on screen to
// open it again, for 3 s, then ends. No notification there: such an app never asked for them, and a prompt now would come as the app ends
static int multi_program(void) { const char *e = getenv("DM_EXEC_OUT"); return e && *e; }
@interface DmNotifyDelegate : NSObject <UNUserNotificationCenterDelegate>
@end
@implementation DmNotifyDelegate
// The notification can come due before the graceful exit has ended the process: show it anyway, so it waits in the notification centre once the app is gone
- (void)userNotificationCenter:(UNUserNotificationCenter *)c willPresentNotification:(UNNotification *)n withCompletionHandler:(void (^)(UNNotificationPresentationOptions))done {
    (void)c; (void)n; done(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList);
}
@end
static DmNotifyDelegate *g_notify_delegate;
static void front_ios_notify_setup(void) {   // at each start: the delegate, and no notification left from an earlier hand-off (the app is open now)
    if (!multi_program()) return;
    UNUserNotificationCenter *c = UNUserNotificationCenter.currentNotificationCenter;
    g_notify_delegate = [DmNotifyDelegate new]; c.delegate = g_notify_delegate;
    [c removeAllPendingNotificationRequests]; [c removeAllDeliveredNotifications];
}
static void front_ios_notify_ask(void) {   // iOS asks the player only while the choice is not made, so this shows a prompt at the first start alone
    if (!multi_program()) return;
    [UNUserNotificationCenter.currentNotificationCenter requestAuthorizationWithOptions:UNAuthorizationOptionAlert | UNAuthorizationOptionSound completionHandler:^(BOOL granted, NSError *e) {
        LOG("front end: notifications %s%s%s", granted ? "allowed" : "not allowed", e ? ": " : "", e ? e.localizedDescription.UTF8String : ""); }];
}
static int notify_allowed(void) {
    __block UNAuthorizationStatus st = UNAuthorizationStatusNotDetermined; dispatch_semaphore_t got = dispatch_semaphore_create(0);
    [UNUserNotificationCenter.currentNotificationCenter getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *t) { st = t.authorizationStatus; dispatch_semaphore_signal(got); }];
    if (dispatch_semaphore_wait(got, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC))) return 0;
    return st == UNAuthorizationStatusAuthorized || st == UNAuthorizationStatusProvisional || st == UNAuthorizationStatusEphemeral;
}
static void handoff_notice(NSString *text) {   // the game is ending: no sound, and a line on a dark screen in its place
    dm_audio_suspend(1);
    dispatch_async(dispatch_get_main_queue(), ^{
        UIWindow *w = g_root.view.window; if (!w) return;
        UIView *o = [[UIView alloc] initWithFrame:w.bounds]; o.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
        o.backgroundColor = [UIColor colorWithWhite:0 alpha:0.85];
        UILabel *lb = [[UILabel alloc] initWithFrame:CGRectInset(o.bounds, 40, 0)]; lb.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
        lb.numberOfLines = 0; lb.textAlignment = NSTextAlignmentCenter; lb.textColor = UIColor.whiteColor;
        lb.font = [UIFont systemFontOfSize:22 weight:UIFontWeightSemibold]; lb.text = text;
        [o addSubview:lb]; [w addSubview:o];
    });
}
void front_program_handoff(int index, const char *name) {   // front.h; on the guest thread that called oe::ExecuteProgram or oe::RequestToRelaunchApplication, before the graceful exit
    if (!multi_program()) {
        handoff_notice(@"The game restarts.\nOpen the app again to continue.");
        LOG("front end: the app ends to start again; the player is asked to open it again");
        usleep(3000 * 1000); return;
    }
    NSString *nm = name && *name ? @(name) : [NSString stringWithFormat:@"program %d", index];
    if (!notify_allowed()) {
        handoff_notice([NSString stringWithFormat:@"Open the app again to start %@.", nm]);
        LOG("front end: program %d starts at the next start of the app (notifications not allowed)", index);
        usleep(3000 * 1000); return;
    }
    handoff_notice([NSString stringWithFormat:@"Starting %@\u2026", nm]);
    usleep(1500 * 1000);
    UNMutableNotificationContent *c = [UNMutableNotificationContent new]; c.body = [NSString stringWithFormat:@"Tap to start %@", nm];
    UNNotificationRequest *r = [UNNotificationRequest requestWithIdentifier:@"dm.program" content:c trigger:[UNTimeIntervalNotificationTrigger triggerWithTimeInterval:1.0 repeats:NO]];
    __block NSError *err = nil; dispatch_semaphore_t added = dispatch_semaphore_create(0);
    [UNUserNotificationCenter.currentNotificationCenter addNotificationRequest:r withCompletionHandler:^(NSError *e) { err = e; dispatch_semaphore_signal(added); }];
    if (dispatch_semaphore_wait(added, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC))) LOG("[RED] front end: the notification for program %d was not scheduled within 2 s", index);
    else if (err) LOG("[RED] front end: the notification for program %d could not be scheduled: %s", index, err.localizedDescription.UTF8String);
    else LOG("front end: \"Starting %s\" shown, notification for program %d scheduled", nm.UTF8String, index);
}
void front_run(void (*guest)(void)) {   // Already on main.m's big-stack thread
    void dm_companion_host_ready(void);
    dm_companion_host_ready();   // the guest modules are loaded: the companion reader can now answer main's base, size and build ID
    guest();
}

// The account applet offers only choices allowed by the host. Native actions keep
// the choice with the player, including creation, skip and cancellation.
int front_user_select(const front_user *users, int count, int allow_create, int allow_skip) {
    if (!g_layer) return FRONT_USER_CANCEL;
    __block int result = FRONT_USER_CANCEL;
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    dispatch_async(dispatch_get_main_queue(), ^{
        UIViewController *vc = nil;
        for (UIScene *s in UIApplication.sharedApplication.connectedScenes) if ([s isKindOfClass:UIWindowScene.class])
            for (UIWindow *w in ((UIWindowScene *)s).windows) if (w.isKeyWindow) vc = w.rootViewController;
        while (vc.presentedViewController) vc = vc.presentedViewController;
        if (!vc || vc.isBeingDismissed) { dispatch_semaphore_signal(done); return; }
        UIAlertController *alert = [UIAlertController alertControllerWithTitle:@"Choose a user" message:@"Choose who will play. Each user keeps separate progress." preferredStyle:UIAlertControllerStyleAlert];
        for (int i = 0; i < count; i++) {
            int slot = users[i].slot;
            NSString *title = [NSString stringWithFormat:@"%s (user %d)%s", users[i].nickname, slot + 1, users[i].open ? " - in use" : ""];
            [alert addAction:[UIAlertAction actionWithTitle:title style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) {
                (void)a; result = slot; dispatch_semaphore_signal(done);
            }]];
        }
        if (allow_create) [alert addAction:[UIAlertAction actionWithTitle:@"Add user" style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) {
            (void)a; result = FRONT_USER_CREATE; dispatch_semaphore_signal(done);
        }]];
        if (allow_skip) [alert addAction:[UIAlertAction actionWithTitle:@"Skip" style:UIAlertActionStyleDefault handler:^(UIAlertAction *a) {
            (void)a; result = FRONT_USER_SKIP; dispatch_semaphore_signal(done);
        }]];
        [alert addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:^(UIAlertAction *a) {
            (void)a; dispatch_semaphore_signal(done);
        }]];
        dm_gamepad_reset();
        [vc presentViewController:alert animated:YES completion:nil];
    });
    dispatch_semaphore_wait(done, DISPATCH_TIME_FOREVER);
    return result;
}

// Software keyboard: show a UIAlertController on the main thread, the calling thread waits for the result
int front_text_input(const char *title, const char *sub, const char *guide, const char *initial, int max_chars, char *out, int out_cap) {
    (void)max_chars;
    if (!g_layer) return -1;
    __block int r = 0; __block NSString *text = nil;
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    NSString *t = title && *title ? @(title) : @"", *m = [NSString stringWithFormat:@"%s%s%s", sub ? sub : "", sub && *sub && guide && *guide ? "\n" : "", guide ? guide : ""];
    NSString *init = initial ? @(initial) : @"", *ph = guide ? @(guide) : @"";
    dispatch_async(dispatch_get_main_queue(), ^{
        UIAlertController *ac = [UIAlertController alertControllerWithTitle:t message:m preferredStyle:UIAlertControllerStyleAlert];
        [ac addTextFieldWithConfigurationHandler:^(UITextField *f) { f.text = init; f.placeholder = ph; }];
        [ac addAction:[UIAlertAction actionWithTitle:@"OK" style:UIAlertActionStyleDefault handler:^(UIAlertAction *x) { (void)x; r = 1; text = ac.textFields.firstObject.text; dispatch_semaphore_signal(done); }]];
        [ac addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:^(UIAlertAction *x) { (void)x; dispatch_semaphore_signal(done); }]];
        UIViewController *vc = nil;
        for (UIScene *s in UIApplication.sharedApplication.connectedScenes) if ([s isKindOfClass:UIWindowScene.class]) for (UIWindow *w in ((UIWindowScene *)s).windows) if (w.isKeyWindow) vc = w.rootViewController;
        if (!vc) { dispatch_semaphore_signal(done); return; }
        [vc presentViewController:ac animated:YES completion:nil];
    });
    // The guest clock keeps running meanwhile, as on the Switch (hle_swkbd.c): a freeze here as well stopped Octopath Traveler 0's frame loop for as long as the box was up and made its sound stutter
    // (4,977 ms and 2,577 ms frames, 5,181 wakeups a second). The inline keyboard freezes around its own call (hle_swkbd.c)
    dispatch_semaphore_wait(done, DISPATCH_TIME_FOREVER);
    if (!r) return 0;
    snprintf(out, out_cap, "%s", text.UTF8String ? text.UTF8String : "");
    return 1;
}

// Save write failed (most likely the phone's storage is full): show a notice on the main thread without blocking the game. hle_fs.c calls this when a save transaction is refused or a mount has to fall back to read-only;
// the iOS front end used not to implement it, so there was only a log line and the player never learned that progress was not being saved
void front_save_failed(int err) {
    LOG("front end: save data write failed errno=%d, telling the player", err);
    dispatch_async(dispatch_get_main_queue(), ^{
        UIViewController *vc = nil;
        for (UIScene *s in UIApplication.sharedApplication.connectedScenes) if ([s isKindOfClass:UIWindowScene.class]) for (UIWindow *w in ((UIWindowScene *)s).windows) if (w.isKeyWindow) vc = w.rootViewController;
        while (vc.presentedViewController) vc = vc.presentedViewController;
        if (!vc) { LOG("[RED] front end: no window for the save-failure alert errno=%d", err); return; }
        NSString *m = err == ENOSPC ? @"Storage is full, so progress was not saved. Free up space and save again."
                                    : [NSString stringWithFormat:@"Saving failed (errno %d); progress was not saved.", err];
        UIAlertController *a = [UIAlertController alertControllerWithTitle:@"Save failed" message:m preferredStyle:UIAlertControllerStyleAlert];
        [a addAction:[UIAlertAction actionWithTitle:@"OK" style:UIAlertActionStyleDefault handler:nil]];
        [vc presentViewController:a animated:YES completion:nil];
    });
}

// Audio session: the frames RemoteIO asks for in each callback follow the I/O buffer duration, about 21 ms by default on iOS (1024 frames at 48 kHz). The game keeps only 4 blocks × 5 ms = 20 ms in flight,
// so every callback came up 1.3 ms short and was zero-filled, about 47 gaps per second (crackling; reproducible on the Mac by forcing 1024-frame callbacks: 1999 underruns in 2000 callbacks). Ask for 5 ms and the system picks the nearest it supports;
// the category is Playback, as a game's or media app's: the ring / silent switch does not mute it (the default SoloAmbient follows the switch, which silenced the game whenever the phone was on silent) and it still interrupts other apps' audio.
// Set once at boot and again on return to the foreground (the system may reset it after an interruption)
static int g_session_off;   // the last activation failed (another app or a call still held the session): tried again when the app becomes active
// Six channels when the route takes them (HDMI, a multichannel interface or dock): the session's maximum is the route's, and it stays 2 on headphones, AirPods included.
// Asked at activation and again on each route change (front_ios_audio_watch), since a preference does not carry from one route to the next
static void front_ios_audio_channels(AVAudioSession *s) {
    NSError *e = nil; NSInteger most = s.maximumOutputNumberOfChannels, want = most >= 6 ? 6 : 2;
    if (want > 2 && ![s setPreferredOutputNumberOfChannels:want error:&e]) LOG("[RED] audio session: %ld output channels could not be set: %s", (long)want, e.localizedDescription.UTF8String);
    LOG("audio session: route %s, output channels %ld (route maximum %ld)", s.currentRoute.outputs.firstObject.portType.UTF8String ?: "(none)", (long)s.outputNumberOfChannels, (long)most);
}
void front_ios_audio_session(void) {
    AVAudioSession *s = AVAudioSession.sharedInstance; NSError *e = nil;
    if (![s setCategory:AVAudioSessionCategoryPlayback error:&e]) LOG("[RED] audio session category could not be set: %s", e.localizedDescription.UTF8String);
    [s setPreferredSampleRate:48000 error:nil];
    if (![s setPreferredIOBufferDuration:0.005 error:&e]) LOG("[RED] audio session I/O buffer duration could not be set: %s", e.localizedDescription.UTF8String);
    g_session_off = ![s setActive:YES error:&e];
    if (g_session_off) LOG("[RED] audio session activation failed: %s", e.localizedDescription.UTF8String);
    LOG("audio session: sample rate %.0f Hz, I/O buffer %.2f ms (asked for 5 ms)", s.sampleRate, s.IOBufferDuration * 1000);
    front_ios_audio_channels(s);
}
// The channels the route takes now (hle_audio.c decides the output from it: six only when the session has six)
int dm_ios_output_channels(void) { return (int)AVAudioSession.sharedInstance.outputNumberOfChannels; }

// Real audio output is suspended (the silent sink paces the game) while the app is in the background or another audio session has interrupted ours (Siri, an alarm, a call).
// Without the interruption observer the AudioUnits stayed "started" while iOS no longer called them, and a game waiting for its buffers back lost its sound until restart.
// All of this runs on the main queue, like the scene callbacks
static int g_audio_bg, g_audio_intr;
static void front_ios_audio_apply(void) { dm_audio_suspend(g_audio_bg || g_audio_intr); }
static BOOL front_ios_same_outputs(NSArray<AVAudioSessionPortDescription *> *previous, NSArray<AVAudioSessionPortDescription *> *current) {
    if (previous.count != current.count) return NO;
    for (NSUInteger i = 0; i < current.count; i++) {
        AVAudioSessionPortDescription *a = previous[i], *b = current[i];
        if (![a.UID isEqualToString:b.UID] || ![a.portType isEqualToString:b.portType]) return NO;
    }
    return YES;
}
static void front_ios_audio_watch(void) {
    NSNotificationCenter *nc = NSNotificationCenter.defaultCenter;
    [nc addObserverForName:AVAudioSessionInterruptionNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *n) {
        NSUInteger type = [n.userInfo[AVAudioSessionInterruptionTypeKey] unsignedIntegerValue], reason = [n.userInfo[AVAudioSessionInterruptionReasonKey] unsignedIntegerValue];
        if (type == AVAudioSessionInterruptionTypeBegan) { LOG("audio session: interrupted (reason %lu)", (unsigned long)reason); g_audio_intr = 1; }
        else {
            LOG("audio session: interruption ended"); g_audio_intr = 0;
            if (!g_audio_bg) front_ios_audio_session();
        }
        front_ios_audio_apply();
    }];
    // Reapply the channel preference first. A changed output can also interrupt callbacks at the same channel count.
    [nc addObserverForName:AVAudioSessionRouteChangeNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *n) {
        NSUInteger reason = [n.userInfo[AVAudioSessionRouteChangeReasonKey] unsignedIntegerValue];
        AVAudioSession *session = AVAudioSession.sharedInstance;
        AVAudioSessionRouteDescription *previous = n.userInfo[AVAudioSessionRouteChangePreviousRouteKey];
        BOOL moved = previous ? !front_ios_same_outputs(previous.outputs, session.currentRoute.outputs)
                              : reason != AVAudioSessionRouteChangeReasonCategoryChange;
        if (!moved && reason == AVAudioSessionRouteChangeReasonCategoryChange) return;
        int old_channels = dm_audio_route();
        front_ios_audio_channels(session);
        if (moved || (session.outputNumberOfChannels >= 6 ? 6 : 2) != old_channels) {
            LOG("audio session: output route changed (reason %lu)", (unsigned long)reason);
            dm_audio_route_changed();
        }
    }];
    [nc addObserverForName:AVAudioSessionMediaServicesWereResetNotification object:nil queue:NSOperationQueue.mainQueue usingBlock:^(NSNotification *n) {
        (void)n; LOG("audio session: the media services were reset; setting the session and the outputs up again");
        front_ios_audio_session(); dm_audio_reset();
    }];
}

// Scene lifecycle (called by SceneDelegate in main.m): 0 resign active, 1 enter background, 2 return to foreground, 3 become active.
// The screen stays on while the game is active: only touches reset the idle timer, so with a controller (or during a cutscene) the screen dimmed and locked mid-game
// Cloud saves (save_sync.h): what the game committed goes out before iOS suspends the app, without the sender's quiet wait. iOS gives a background task about half a minute
static void save_sync_background(void) {
    UIApplication *app = UIApplication.sharedApplication;
    __block UIBackgroundTaskIdentifier task = UIBackgroundTaskInvalid;
    void (^end)(void) = ^{ if (task != UIBackgroundTaskInvalid) { [app endBackgroundTask:task]; task = UIBackgroundTaskInvalid; } };   // on the main queue, as the expiry handler runs
    task = [app beginBackgroundTaskWithName:@"cloud save" expirationHandler:end];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        if (!save_sync_flush(20000)) LOG("front end: the save did not reach the cloud before the app was suspended; it goes after the next save or at the next start");
        dispatch_async(dispatch_get_main_queue(), end);
    });
}

void front_ios_focus_event(int ev) {
    if (!g_layer) return;
    switch (ev) {
        case 0: dm_movie_active(0); dm_gamepad_reset(); dm_focus_set(2); break;
        case 1:
            UIApplication.sharedApplication.idleTimerDisabled = NO;
            dm_focus_set(3);
            g_audio_bg = 1;
            front_ios_audio_apply();
            guest_clock_freeze_for(1, 1);
            save_sync_background();
            dm_gfx_suspend(1);
            break;
        case 2: dm_gfx_suspend(0); guest_clock_freeze_for(1, 0); front_ios_audio_session(); g_audio_bg = 0; front_ios_audio_apply(); dm_focus_set(2); break;
        case 3:   // Becoming active again also ends an interruption whose "ended" never came (not every one sends it), and retries a session that would not activate on the way back
            UIApplication.sharedApplication.idleTimerDisabled = YES;
            if (g_audio_intr) { LOG("audio session: active again with no interruption-ended notice; resuming"); g_audio_intr = 0; g_session_off = 1; }
            if (g_session_off) front_ios_audio_session();
            front_ios_audio_apply(); dm_gamepad_active(); dm_focus_set(1); dm_movie_active(1); break;
    }
}
