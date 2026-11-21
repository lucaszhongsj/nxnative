#import <XCTest/XCTest.h>
#import <GameController/GameController.h>
#import <QuartzCore/CAMetalLayer.h>
#import <objc/runtime.h>
#if __has_include(<UIKit/UIHingeInteraction.h>)
#import <UIKit/UIHingeInteraction.h>
#import <UIKit/UIViewReservedRegion.h>
#define DUO_HARNESS_HAS_NATIVE_POSE_API 1
#else
#define DUO_HARNESS_HAS_NATIVE_POSE_API 0
#endif
#import "DmLayout.h"
#import "companion_layout.h"
#import "DmGameMenu.h"
#import "DuoHarnessRuntime.h"
#include "dm_input.h"
#include <math.h>
#include <stdio.h>
void front_picture_shape(int w, int h);   // front_ios.m (dm/host/front.h)

static NSArray<GCController *> *gDuoHarnessControllers;
static BOOL gDuoHarnessControllerSwizzleInstalled;
static UIView *gDuoHarnessReservedRegionView;
static id gDuoHarnessReservedRegion;
static BOOL gDuoHarnessSawIncludeInactiveDivisionQuery;
static NSUInteger gDuoHarnessProbeViewClassSerial;

@interface GCController (DuoHarnessControllerList)
+ (NSArray<GCController *> *)duo_harness_controllers;
@end

@implementation GCController (DuoHarnessControllerList)
+ (NSArray<GCController *> *)duo_harness_controllers {
    @synchronized (GCController.class) {
        if (gDuoHarnessControllers) return gDuoHarnessControllers;
    }
    return [self duo_harness_controllers];
}
@end

@interface DuoFakeTouch : NSObject
@property (nonatomic) CGPoint point;
@property (nonatomic) NSTimeInterval timestamp;
- (CGPoint)locationInView:(UIView *)view;
@end

@implementation DuoFakeTouch
- (CGPoint)locationInView:(UIView *)view { (void)view; return self.point; }
@end

@interface DuoLiveButton : NSObject
@property (nonatomic, getter=isPressed) BOOL pressed;
- (DuoLiveButton *)pressedInput;
@end
@implementation DuoLiveButton
- (DuoLiveButton *)pressedInput { return self; }
@end

typedef void (^DuoLiveHandler)(id<GCDevicePhysicalInput>, id<GCPhysicalInputElement>);

// Live profiles cannot be constructed through GameController's public snapshot API.
@interface DuoLiveInput : NSObject
@property (nonatomic, copy) DuoLiveHandler elementValueDidChangeHandler;
@property (nonatomic, copy) NSDictionary<NSString *, DuoLiveButton *> *buttons;
@property (nonatomic, copy) NSDictionary<NSString *, GCControllerDirectionPad *> *dpads;
- (void)setPressed:(BOOL)pressed button:(NSString *)name;
- (void)moveX:(float)x y:(float)y;
@end
@implementation DuoLiveInput
- (instancetype)init {
    if ((self = [super init])) {
        NSMutableDictionary *buttons = [NSMutableDictionary dictionary];
        for (NSString *name in @[GCInputButtonA, GCInputButtonB, GCInputButtonX, GCInputButtonY,
                                 GCInputLeftShoulder, GCInputRightShoulder, GCInputButtonMenu, GCInputButtonHome])
            buttons[name] = [DuoLiveButton new];
        _buttons = [buttons copy];
        _dpads = @{ GCInputDirectionPad: [GCController controllerWithExtendedGamepad].extendedGamepad.dpad };
    }
    return self;
}
- (void)setPressed:(BOOL)pressed button:(NSString *)name {
    DuoLiveButton *button = self.buttons[name];
    button.pressed = pressed;
    DuoLiveHandler handler = self.elementValueDidChangeHandler;
    if (handler) handler((id)self, (id)button);
}
- (void)moveX:(float)x y:(float)y {
    GCControllerDirectionPad *stick = self.dpads[GCInputDirectionPad];
    [stick setValueForXAxis:x yAxis:y];
    DuoLiveHandler handler = self.elementValueDidChangeHandler;
    if (handler) handler((id)self, (id)stick);
}
@end

@interface DuoLiveController : NSObject
@property (nonatomic, copy) NSString *productCategory;
@property (nonatomic, readonly) NSString *vendorName;
@property (nonatomic, readonly) GCExtendedGamepad *extendedGamepad;
@property (nonatomic, readonly) GCMotion *motion;
@property (nonatomic, readonly) DuoLiveInput *input;
@property (nonatomic, readonly) GCPhysicalInputProfile *physicalInputProfile;
@property (nonatomic, readonly) GCMicroGamepad *microGamepad;
- (instancetype)initWithCategory:(NSString *)category;
@end
@implementation DuoLiveController
- (instancetype)initWithCategory:(NSString *)category {
    if ((self = [super init])) {
        _productCategory = [category copy];
        _input = [DuoLiveInput new];
    }
    return self;
}
- (NSString *)vendorName { return self.productCategory; }
- (GCExtendedGamepad *)extendedGamepad { return nil; }
- (GCMotion *)motion { return nil; }
// The GameController framework asks the most recently active controller for its physical profile when the device session refreshes; a fake has none, and the pad's own
// question (turning the system gestures of Home and Capture off) then leaves the buttons as they are.
- (GCPhysicalInputProfile *)physicalInputProfile { return nil; }
- (GCMicroGamepad *)microGamepad { return nil; }
@end

#if DUO_HARNESS_HAS_NATIVE_POSE_API
@interface DuoFakeHinge : NSObject
@property (nonatomic) NSInteger status;
@property (nonatomic) CGFloat angle;
@end
@implementation DuoFakeHinge
@end

@interface DuoFakeReservedRegion : NSObject
@property (nonatomic) CGRect frame;
@property (nonatomic) UIEdgeInsets margins;
@property (nonatomic, strong) id identifier;
@property (nonatomic, strong) id kind;
@property (nonatomic, getter=isActive) BOOL active;
@end
@implementation DuoFakeReservedRegion
@end

@interface UIViewController (DuoHarnessNativeHingeCallback)
- (void)hingeChanged:(id _Nullable)hinge;
- (void)startGame;
@end

static NSArray<UIViewReservedRegion *> *DuoHarnessReservedRegionsForProbeView(
    id view, SEL selector, UIViewReservedRegionKind *kind, UIViewReservedRegionQueryOptions options) API_AVAILABLE(ios(27.1)) {
    (void)selector;
    (void)kind;
    if (view == gDuoHarnessReservedRegionView) {
        if (options & UIViewReservedRegionQueryOptionsIncludeInactive) {
            gDuoHarnessSawIncludeInactiveDivisionQuery = YES;
            return gDuoHarnessReservedRegion ? @[(UIViewReservedRegion *)gDuoHarnessReservedRegion] : @[];
        }
        return @[];
    }
    return @[];
}

static Class DuoHarnessInstallReservedRegionProbe(UIView *view) API_AVAILABLE(ios(27.1)) {
    Class originalClass = object_getClass(view);
    char className[96];
    snprintf(className, sizeof(className), "DuoHarnessReservedRegionProbe%lu", (unsigned long)++gDuoHarnessProbeViewClassSerial);
    Class probeClass = objc_allocateClassPair(originalClass, className, 0);
    if (!probeClass) return Nil;
    Method queryMethod = class_getInstanceMethod(UIView.class, @selector(reservedRegionsOfKind:options:));
    if (!queryMethod || !class_addMethod(probeClass, @selector(reservedRegionsOfKind:options:),
                                         (IMP)DuoHarnessReservedRegionsForProbeView, method_getTypeEncoding(queryMethod))) {
        objc_disposeClassPair(probeClass);
        return Nil;
    }
    objc_registerClassPair(probeClass);
    object_setClass(view, probeClass);
    return originalClass;
}

#endif

@interface DuoHarnessTests : XCTestCase
@property (nonatomic, strong) id savedLayout;
@property (nonatomic) BOOL controllerListOverridden;
- (void)setHarnessControllers:(NSArray<GCController *> * _Nullable)controllers notification:(NSNotificationName _Nullable)notification;
- (UIViewController *)harnessRootViewController;
- (void)waitForView:(UIView *)view hidden:(BOOL)hidden;
@end

@implementation DuoHarnessTests
- (void)setUp {
    [super setUp];
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    self.savedLayout = [defaults objectForKey:@"DmPadLayout"];
    [defaults setObject:@1 forKey:@"DmPadLayout"];
    [self setHarnessControllers:@[] notification:GCControllerDidDisconnectNotification];
    duo_harness_clear();
}

- (void)tearDown {
    NSUserDefaults *defaults = NSUserDefaults.standardUserDefaults;
    if (self.savedLayout) [defaults setObject:self.savedLayout forKey:@"DmPadLayout"];
    else [defaults removeObjectForKey:@"DmPadLayout"];
    if (self.controllerListOverridden) {
        @synchronized (GCController.class) {
            Method original = class_getClassMethod(GCController.class, @selector(controllers));
            Method replacement = class_getClassMethod(GCController.class, @selector(duo_harness_controllers));
            method_exchangeImplementations(original, replacement);
            gDuoHarnessControllers = nil;
            gDuoHarnessControllerSwizzleInstalled = NO;
        }
        self.controllerListOverridden = NO;
    }
    [super tearDown];
}

- (void)setHarnessControllers:(NSArray<GCController *> * _Nullable)controllers notification:(NSNotificationName _Nullable)notification {
    @synchronized (GCController.class) {
        if (!gDuoHarnessControllerSwizzleInstalled) {
            Method original = class_getClassMethod(GCController.class, @selector(controllers));
            Method replacement = class_getClassMethod(GCController.class, @selector(duo_harness_controllers));
            method_exchangeImplementations(original, replacement);
            gDuoHarnessControllerSwizzleInstalled = YES;
            self.controllerListOverridden = YES;
        }
        gDuoHarnessControllers = [controllers copy];
    }
    if (notification) [[NSNotificationCenter defaultCenter] postNotificationName:notification object:nil];
}

- (void)waitForView:(UIView *)view hidden:(BOOL)hidden {
    NSPredicate *predicate = [NSPredicate predicateWithBlock:^BOOL(UIView *candidate, NSDictionary *bindings) {
        (void)bindings;
        return candidate.hidden == hidden;
    }];
    XCTNSPredicateExpectation *expectation = [[XCTNSPredicateExpectation alloc] initWithPredicate:predicate object:view];
    XCTAssertEqual([XCTWaiter waitForExpectations:@[expectation] timeout:5.0], XCTWaiterResultCompleted);
}

- (void)deliverSnapshotChangeFor:(GCController *)controller button:(GCControllerButtonInput *)button value:(float)value {
    [button setValue:value];
    GCExtendedGamepad *profile = controller.extendedGamepad;
    GCExtendedGamepadValueChangedHandler handler = profile.valueChangedHandler;
    if (handler) handler(profile, button);
}

- (void)testPortraitSeparatesAcrossHingeStatesAndUsesNativeDivision {
    CGRect landscape = CGRectMake(0, 0, 844, 390);
    DmLayout overlay = dm_layout(landscape, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
    XCTAssertFalse(overlay.separated);
    XCTAssertTrue(CGRectEqualToRect(overlay.controls, landscape));
    XCTAssertEqualWithAccuracy(overlay.game.size.width / overlay.game.size.height, 16.0 / 9.0, 0.001);

    CGRect portrait = CGRectMake(0, 0, 390, 844);
    UIEdgeInsets safe = UIEdgeInsetsMake(59, 0, 34, 0);
    CGRect division = CGRectMake(0, 400, 390, 24);
    DmLayout separated = dm_layout(portrait, safe, NO, division, DM_PICTURE_16_9);
    XCTAssertTrue(separated.separated, @"Portrait uses a valid native horizontal division even with no confirmed hinge state");
    XCTAssertEqualWithAccuracy(CGRectGetMinY(separated.game), 119, 1);
    XCTAssertLessThanOrEqual(CGRectGetMaxY(separated.game), CGRectGetMinY(division));
    XCTAssertTrue(CGRectEqualToRect(separated.controls, CGRectMake(0, 424, 390, 420)));
    XCTAssertLessThanOrEqual(CGRectGetMaxY(separated.game), CGRectGetMinY(separated.controls), @"Video and controls must not overlap");

    DmLayout clipped = dm_layout(portrait, safe, NO, CGRectMake(-50, 400, 500, 24), DM_PICTURE_16_9);
    XCTAssertTrue(clipped.separated, @"A division that partially overlaps the view still separates its visible panels");
    XCTAssertEqualWithAccuracy(CGRectGetMinY(clipped.controls), 424, 0.001);
    XCTAssertEqualWithAccuracy(clipped.controls.size.width, portrait.size.width, 0.001);
    DmLayout flat = dm_layout(portrait, safe, NO, CGRectMake(0, 300, 390, 0), DM_PICTURE_16_9);
    XCTAssertTrue(flat.separated, @"A zero-height native division remains a useful split boundary");
    XCTAssertEqualWithAccuracy(CGRectGetMinY(flat.controls), 300, 0.001, @"Prefer a valid native split away from the fallback midpoint");
    XCTAssertLessThanOrEqual(CGRectGetMaxY(flat.game), 300);
    XCTAssertTrue(dm_layout(portrait, safe, YES, division, DM_PICTURE_16_9).separated, @"A partially-open display can use the same portrait split");
    XCTAssertTrue(dm_layout(portrait, safe, NO, CGRectNull, DM_PICTURE_16_9).separated, @"Portrait without a division still uses a midpoint split");
    XCTAssertTrue(dm_layout(portrait, safe, YES, CGRectMake(500, 400, 500, 24), DM_PICTURE_16_9).separated, @"Invalid native regions fall back to a portrait split");
    XCTAssertTrue(dm_layout(portrait, safe, NO, CGRectMake(180, 0, 24, 844), DM_PICTURE_16_9).separated, @"A vertical region falls back to a portrait split");
}

- (void)testPortraitFallbackFitsVideoAndControlsWithinSafeAreasAtNarrowSizes {
    const CGRect sizes[] = {CGRectMake(0, 0, 390, 844), CGRectMake(0, 0, 320, 568)};
    const UIEdgeInsets insets[] = {UIEdgeInsetsMake(59, 0, 34, 0), UIEdgeInsetsMake(50, 0, 24, 0)};
    for (NSUInteger i = 0; i < 2; i++) {
        CGRect bounds = sizes[i];
        CGFloat aspect = dm_picture_shape_auto(bounds.size);
        DmLayout layout = dm_layout(bounds, insets[i], NO, CGRectNull, aspect);
        // No fold: the area below the safe top splits in half, the picture fills the upper half and the panel the lower half
        CGFloat splitY = CGRectGetMidY(bounds);
        XCTAssertTrue(layout.separated);
        XCTAssertTrue(CGRectEqualToRect(layout.companion, CGRectMake(0, splitY, bounds.size.width, bounds.size.height - splitY)),
                      @"The panel keeps the lower half");
        XCTAssertTrue(CGRectEqualToRect(layout.controls, layout.companion),
                      @"The pad floats over the whole band");
        XCTAssertEqualWithAccuracy(layout.game.size.width / layout.game.size.height, aspect, 0.001);
        XCTAssertEqualWithAccuracy(layout.game.size.height, splitY, 0.5, @"The picture fills the upper half");
        XCTAssertEqualWithAccuracy(layout.game.size.width, bounds.size.width, 0.5, @"Its sides are not cropped: %@", NSStringFromCGRect(layout.game));
        XCTAssertLessThanOrEqual(CGRectGetMaxY(layout.game), CGRectGetMinY(layout.companion), @"The band's top leaves no video/panel overlap");
    }
}

- (void)testLandscapeUsesOverlayExceptForPartiallyOpenNativeDivision {
    CGRect bounds = CGRectMake(0, 0, 844, 390);
    DmLayout overlay = dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
    XCTAssertFalse(overlay.separated);
    XCTAssertTrue(CGRectEqualToRect(overlay.controls, bounds));
    XCTAssertTrue(CGRectEqualToRect(overlay.game, dm_aspect_fit(bounds, DM_PICTURE_16_9)));
    XCTAssertFalse(dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectMake(0, 180, 844, 24), DM_PICTURE_16_9).separated,
                   @"A fully-open landscape display retains the overlay layout");
    CGRect division = CGRectMake(0, 180, 844, 24);
    DmLayout separated = dm_layout(bounds, UIEdgeInsetsZero, YES, division, DM_PICTURE_16_9);
    XCTAssertTrue(separated.separated,
                  @"A landscape split still requires a partially-open hinge and a valid native division");
    XCTAssertEqualWithAccuracy(CGRectGetMaxY(separated.game), 180, 1);
    XCTAssertEqualWithAccuracy(CGRectGetMinY(separated.controls), 204, 0.001,
                               @"Keep the full reserved gap between video and controls");
    XCTAssertLessThanOrEqual(CGRectGetMaxY(separated.game), CGRectGetMinY(separated.controls));
}

- (void)testPictureShapeAutoFillsHalfTheScreenWhenStandingUp {
    // A phone standing up asks for the upper half's own shape, so the picture fills that half with nothing cropped.
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(390, 844)), 2 * 390.0 / 844.0, 0.001);
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(375, 667)), 2 * 375.0 / 667.0, 0.001);
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(430, 932)), 2 * 430.0 / 932.0, 0.001);
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(820, 1180)), 2 * 820.0 / 1180.0, 0.001);
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(744, 1133)), 2 * 744.0 / 1133.0, 0.001);
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(1032, 1376)), 2 * 1032.0 / 1376.0, 0.001, @"An unfolded Duo standing up");
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(1376, 1032)), 1376.0 / 1032.0, 0.001, @"An unfolded Duo lying down is 4:3");
    // A window wider than 16:9 keeps its own shape: loader.c takes 0.9 to 2.4, so a phone lying down at 19.5:9 is a shape to render in
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(844, 390)), 844.0 / 390.0, 0.001, @"A 19.5:9 phone lying down");
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(1024, 768)), 1024.0 / 768.0, 0.001, @"An iPad lying down is 4:3");
    // A square window takes its own shape while lying down (height is not greater than width)
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(600, 600)), 1.0f, 0.001);
    XCTAssertEqual(dm_picture_shape_auto(CGSizeZero), 0.0f);
    // The screen's own shape by its orientation: a phone lying down keeps its wide shape, a phone standing up asks for the upper half's shape.
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(402, 874)), 2 * 402.0 / 874.0, 0.001, @"An iPhone 17 Pro standing up");
    XCTAssertEqualWithAccuracy(dm_picture_shape_auto(CGSizeMake(874, 402)), 874.0 / 402.0, 0.001, @"The same phone lying down");
}

- (void)testPortraitWithoutHingeStatusStillSplitsVideoAboveControls {
    // A phone standing up splits the screen at its midpoint: the picture fills the upper half, the panel the lower half
    DmLayout layout = dm_layout(CGRectMake(0, 0, 390, 844), UIEdgeInsetsMake(59, 0, 34, 0), NO, CGRectNull, dm_picture_shape_auto(CGSizeMake(390, 844)));
    XCTAssertTrue(layout.separated);
    XCTAssertEqualWithAccuracy(layout.game.size.height, 422, 0.5, @"The picture fills the upper half's height: %@", NSStringFromCGRect(layout.game));
    XCTAssertEqualWithAccuracy(CGRectGetMinY(layout.game), 0, 0.5, @"It starts at the top of the screen");
    XCTAssertEqualWithAccuracy(CGRectGetMaxY(layout.game), CGRectGetMinY(layout.companion), 0.5, @"No bar between the picture and the band");
    XCTAssertEqualWithAccuracy(layout.game.size.width, 390, 0.5, @"The picture is the screen's width: its sides are not cropped");
    XCTAssertEqualWithAccuracy(CGRectGetMinY(layout.companion), 422, 0.5);
    XCTAssertEqualWithAccuracy(layout.companion.size.height, 422, 0.5, @"The panel keeps the lower half");
    XCTAssertTrue(CGRectEqualToRect(layout.controls, layout.companion), @"The pad floats over the whole band");
    XCTAssertEqualWithAccuracy(CGRectGetMaxY(layout.controls), 844, 0.5);
    // An iPad standing up splits the area below its safe top in half
    DmLayout ipad = dm_layout(CGRectMake(0, 0, 820, 1180), UIEdgeInsetsMake(24, 0, 20, 0), NO, CGRectNull, dm_picture_shape_auto(CGSizeMake(820, 1180)));
    XCTAssertTrue(ipad.separated);
    XCTAssertEqualWithAccuracy(CGRectGetMinY(ipad.companion), 590, 0.5, @"An iPad splits at its midpoint");
    XCTAssertEqualWithAccuracy(ipad.game.size.height, 590, 0.5, @"The picture fills the upper half");
    XCTAssertEqualWithAccuracy(ipad.game.size.width, 820, 0.5, @"Its sides are not cropped");
}

- (void)testCompanionTakesTheBandUnderThePictureAndNoOtherLayoutHasOne {
    // The pad floats over the band, so the panel is the whole band and is drawn under the keys: the same rectangle as the controls, not a part of it
    DmLayout split = dm_layout(CGRectMake(0, 0, 390, 844), UIEdgeInsetsMake(59, 0, 34, 0), NO, CGRectNull, DM_PICTURE_16_9);
    XCTAssertTrue(split.separated);
    XCTAssertTrue(CGRectEqualToRect(split.companion, split.controls), @"The band is the pad's area: %@ against %@", NSStringFromCGRect(split.companion), NSStringFromCGRect(split.controls));
    XCTAssertLessThanOrEqual(CGRectGetMaxY(split.game), CGRectGetMinY(split.companion), @"The picture stays above the band");
    // A Duo's fold moves the band with the controls
    DmLayout folded = dm_layout(CGRectMake(0, 0, 669, 951), UIEdgeInsetsMake(62, 0, 34, 0), NO, CGRectMake(0, 455.5, 669, 40), DM_PICTURE_16_9);
    XCTAssertTrue(folded.separated);
    XCTAssertTrue(CGRectEqualToRect(folded.companion, folded.controls));
    XCTAssertEqualWithAccuracy(CGRectGetMinY(folded.companion), 495.5, 0.5, @"Below the fold's lower half");
    // Landscape (the overlay) and a picture on a TV have no band
    DmLayout overlay = dm_layout(CGRectMake(0, 0, 844, 390), UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
    XCTAssertFalse(overlay.separated);
    XCTAssertTrue(CGRectIsEmpty(overlay.companion));
    DmLayout tv = dm_layout_pad_only(CGRectMake(0, 0, 390, 844));
    XCTAssertFalse(tv.separated);
    XCTAssertTrue(CGRectIsEmpty(tv.companion));
}

- (void)testProductionRootFollowsTheOrientationWithItsConsoleMode {
    // A phone standing up is handheld, so the game's own touch screens (its software keyboard above all) take touches; lying down it returns to the mode it
    // had. The harness stubs dm_set_opmode into a variable (DuoRuntimeStubs.m), so this reads the mode the front end asked for.
    UIViewController *root = [self harnessRootViewController];
    SEL follow = NSSelectorFromString(@"followOrientation:");
    XCTAssertTrue([root respondsToSelector:follow], @"The front end switches the console mode by orientation");
    NSMethodSignature *sig = [root methodSignatureForSelector:follow];
    NSInvocation *call = [NSInvocation invocationWithMethodSignature:sig];
    call.target = root; call.selector = follow;
    CGRect standing = CGRectMake(0, 0, 390, 844), lying = CGRectMake(0, 0, 844, 390);
    // The harness window stands up, so the app has already followed that orientation: settle it lying down and docked, the baseline the app starts from
    [call setArgument:&lying atIndex:2]; [call invoke];
    dm_set_opmode(1);
    [call setArgument:&standing atIndex:2]; [call invoke];
    XCTAssertEqual(dm_opmode(), 0, @"A phone standing up runs handheld: the game's keyboard takes touches");
    [call setArgument:&lying atIndex:2]; [call invoke];
    XCTAssertEqual(dm_opmode(), 1, @"Lying down it returns to the mode the player had");
    // A choice made in the in-game menu while standing up is the one lying down returns to
    [call setArgument:&standing atIndex:2]; [call invoke];
    XCTAssertEqual(dm_opmode(), 0);
    dm_set_opmode(1);   // the player docks it from the in-game menu while standing up
    [call setArgument:&lying atIndex:2]; [call invoke];
    XCTAssertEqual(dm_opmode(), 1, @"The player's docked choice while standing up is what lying down keeps");
}

- (void)testPlayerConsoleModeChosenWhileStandingUpSurvivesTheReturn {
    // A phone standing up forces handheld, so a player who wants docked there picks it in the in-game menu; lying down must keep that choice, not the
    // handheld it replaced. This is the case a baseline of docked hides: the mode held for the return would still be handheld.
    UIViewController *root = [self harnessRootViewController];
    SEL follow = NSSelectorFromString(@"followOrientation:");
    NSInvocation *call = [NSInvocation invocationWithMethodSignature:[root methodSignatureForSelector:follow]];
    call.target = root; call.selector = follow;
    CGRect standing = CGRectMake(0, 0, 390, 844), lying = CGRectMake(0, 0, 844, 390);
    dm_set_opmode(0);   // a player who was handheld lying down
    [call setArgument:&lying atIndex:2]; [call invoke];
    dm_set_opmode(0);
    [call setArgument:&standing atIndex:2]; [call invoke];
    XCTAssertEqual(dm_opmode(), 0, @"Standing up it stays handheld");
    // The in-game menu's docked pick while standing up
    XCTAssertTrue(dm_setting_live("DM_OPMODE", "1"));
    XCTAssertEqual(dm_opmode(), 1);
    [call setArgument:&lying atIndex:2]; [call invoke];
    XCTAssertEqual(dm_opmode(), 1, @"Lying down keeps the player's docked pick, not the handheld it replaced");
}

- (void)testSplitPadPassesACompanionTapThroughToTheBand {
    // A tap on no key inside the band goes to the companion's view below (tabs, item slots); a tap on a key or on the picture stays with the pad.
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    DmLayout layout = dm_layout(bounds, UIEdgeInsetsMake(59, 0, 34, 0), NO, CGRectNull, DM_PICTURE_16_9);
    UIView *pad = [self padForLayout:layout bounds:bounds];
    CGPoint minus = [self controlCenterNamed:@"−" inPad:pad];
    XCTAssertEqual([pad hitTest:minus withEvent:nil], pad, @"A key still belongs to the pad");
    // The band's middle-left is clear of the top menu pill, the two rows of thumb groups and the right stick: it must pass through
    CGPoint clear = CGPointMake(CGRectGetMinX(layout.companion) + 12, CGRectGetMidY(layout.companion));
    XCTAssertTrue(CGRectContainsPoint(layout.controls, clear));
    XCTAssertNil([pad hitTest:clear withEvent:nil], @"No key there: the companion below receives the tap");
    // Landscape has no band: the pad keeps the whole view
    DmLayout overlay = dm_layout(CGRectMake(0, 0, 844, 390), UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
    UIView *landscapePad = [self padForLayout:overlay bounds:CGRectMake(0, 0, 844, 390)];
    XCTAssertEqual([landscapePad hitTest:CGPointMake(8, 8) withEvent:nil], landscapePad, @"With no split the pad owns the view");
}

- (void)testProductionRootPutsThePanelUnderThePadAndOutOfItsWay {
    UIViewController *root = [self harnessRootViewController];
    UIView *pad = [root valueForKey:@"pad"];
    UIView *companion = [root valueForKey:@"companion"];
    XCTAssertNotNil(companion, @"The front end keeps a view for the band");
    XCTAssertEqual(companion.superview, root.view);
    // The pad is above the panel: its keys and touches cover the panel, which the band's own background fills underneath
    NSInteger padIndex = [root.view.subviews indexOfObjectIdenticalTo:pad];
    NSInteger panelIndex = [root.view.subviews indexOfObjectIdenticalTo:companion];
    XCTAssertGreaterThan(padIndex, panelIndex, @"The pad is added after the panel, so it floats over it");
    // The pad floats over the panel and passes through a tap on no key: the companion's own tabs and item slots then receive it (DmGamepad.m hitTest)
    XCTAssertTrue(companion.isUserInteractionEnabled);
    [root.view layoutIfNeeded];
    BOOL portrait = root.view.bounds.size.height > root.view.bounds.size.width;
    XCTAssertEqual(companion.hidden, !portrait, @"The panel is shown only where a split gives it a band");
    if (portrait) {
        CGFloat splitY = root.view.bounds.size.height / 2;
        XCTAssertEqualWithAccuracy(CGRectGetMinY(companion.frame), splitY, 1.0,
                                   @"The panel takes the lower half: %@", NSStringFromCGRect(companion.frame));
        XCTAssertLessThanOrEqual(CGRectGetMaxY([(UIView *)[root valueForKey:@"game"] frame]), CGRectGetMinY(companion.frame),
                                 @"The picture stays in the upper half");
    }
}

- (void)testCompanionBarKeepsTheCanvasDockRatio {
    // A standing phone's bar is the canvas's own dock row (114 of 968 body + 114 dock) plus the home
    // indicator's strip, so the panel keeps the ratio its pages were authored with.
    dm_companion_surface_layout phone = dm_companion_surface_make_safe(CGSizeMake(393, 426), 0, (dm_companion_dock_insets){0, 0, 34});
    XCTAssertEqualWithAccuracy(phone.nav_scale, 393.0 / 1240.0, 0.0005);
    XCTAssertEqualWithAccuracy(phone.footer.size.height, 114 * (393.0 / 1240.0) + 34, 0.6);
    XCTAssertEqualWithAccuracy(phone.content.size.height, 426 - phone.footer.size.height, 0.6);
    XCTAssertEqualWithAccuracy(CGRectGetMaxY(phone.footer), 426, 0.6);
    XCTAssertEqualWithAccuracy(CGRectGetMidY(phone.nav_art), CGRectGetMidY(phone.footer), 0.6,
                               @"The bar centres in the footer, the home indicator's strip included");
    // The window's own canvas span (952..1080) lands on its rectangle: the icons are drawn where the art drew them
    XCTAssertEqualWithAccuracy(phone.nav_offset.y + 952 * phone.nav_scale, CGRectGetMinY(phone.nav_art), 0.6);
    XCTAssertEqualWithAccuracy(phone.nav_offset.y + 1080 * phone.nav_scale, CGRectGetMaxY(phone.nav_art), 0.6);
    // A standing band with no home indicator: the bar is the canvas row alone.
    dm_companion_surface_layout flat = dm_companion_surface_make_safe(CGSizeMake(375, 426), 0, (dm_companion_dock_insets){0});
    XCTAssertEqualWithAccuracy(flat.footer.size.height, 114 * (375.0 / 1240.0), 0.6);
    // A narrow standing band scales by its width: the content keeps all the height above the bar.
    dm_companion_surface_layout narrow = dm_companion_surface_make_safe(CGSizeMake(320, 426), 0, (dm_companion_dock_insets){0, 0, 34});
    XCTAssertEqualWithAccuracy(narrow.footer.size.height, 114 * (320.0 / 1240.0) + 34, 0.6);
    XCTAssertEqualWithAccuracy(narrow.content.size.height + narrow.footer.size.height, 426, 0.6);
    // A wide panel (an iPad's lower half) keeps the fitted square it had.
    dm_companion_surface_layout wide = dm_companion_surface_make_safe(CGSizeMake(820, 590), 0, (dm_companion_dock_insets){0});
    XCTAssertEqualWithAccuracy(wide.body.size.width, 546, 0.6);
    XCTAssertEqualWithAccuracy(wide.body.size.height, 546, 0.6);
}

- (void)testPortraitControlsUseAnchoredGroupsWithASeparatedThreeDimensionalLeftPair {
    struct { CGRect bounds; UIEdgeInsets insets; CGRect division; } cases[] = {
        {CGRectMake(0, 0, 669, 951), UIEdgeInsetsMake(62, 0, 34, 0), CGRectMake(0, 455.5, 669, 40)},
        {CGRectMake(0, 0, 390, 844), UIEdgeInsetsMake(59, 0, 34, 0), CGRectNull},
        {CGRectMake(0, 0, 320, 568), UIEdgeInsetsMake(50, 0, 24, 0), CGRectNull},
    };
    for (NSUInteger i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        CGRect bounds = cases[i].bounds;
        DmLayout layout = [self portraitControlLayoutForBounds:bounds insets:cases[i].insets division:cases[i].division];
        XCTAssertTrue(layout.separated, @"Portrait case %lu must expose a controls panel", (unsigned long)i);
        XCTAssertLessThanOrEqual(CGRectGetMaxY(layout.game), CGRectGetMinY(layout.controls),
                                 @"Video and controls must remain disjoint in portrait case %lu", (unsigned long)i);
        UIView *pad = [self padForLayout:layout bounds:bounds];

        CGRect lower = layout.controls;
        // Two rows of controls in a standing phone's band: the stick and the diamond in the upper row, the D-pad and the right stick in the lower one,
        // each in its own column at a quarter and three quarters of the band's width.
        CGFloat u = [self portraitUnitForLayout:layout];
        CGPoint leftColumn = dm_pad_portrait_column(lower, NO);
        CGPoint rightColumn = dm_pad_portrait_column(lower, YES);
        CGFloat upperRow = dm_pad_portrait_row(lower, NO, YES).y;
        CGFloat lowerRow = dm_pad_portrait_row(lower, YES, YES).y;
        CGRect stickRing = [self largestShapePathBoundsInLayer:pad.layer];
        CGPoint renderedStick = CGPointMake(CGRectGetMidX(stickRing), CGRectGetMidY(stickRing));
        XCTAssertEqualWithAccuracy(renderedStick.x, leftColumn.x, 0.5,
                                   @"The 3D joystick should sit in the left column (case %lu)", (unsigned long)i);
        XCTAssertEqualWithAccuracy(renderedStick.y, upperRow, 0.5, @"The joystick is in the upper row (case %lu)", (unsigned long)i);

        CGPoint faces = [self faceDiamondCenterInPad:pad];
        XCTAssertEqualWithAccuracy(faces.x, rightColumn.x, 0.5,
                                   @"The ABXY diamond should be centered at the right portrait column (case %lu)", (unsigned long)i);
        XCTAssertEqualWithAccuracy(faces.y, upperRow, 0.5, @"The diamond is in the upper row (case %lu)", (unsigned long)i);

        CGPoint dpadCenter = CGPointMake(leftColumn.x, lowerRow);
        XCTAssertEqualWithAccuracy(dpadCenter.y, lowerRow, 0.5, @"The D-pad is in the lower row (case %lu)", (unsigned long)i);
        DuoFakeTouch *right = [self touchAt:CGPointMake(dpadCenter.x + 0.6 * dm_pad_portrait_sizes(YES).dpadR * u, dpadCenter.y) time:1];
        [self beginTouches:@[right] onPad:pad];
        XCTAssertEqual(duo_harness_input().buttons, DM_BTN_RIGHT,
                       @"The D-pad hit target should share its specified portrait position (case %lu)", (unsigned long)i);
        [self endTouches:@[right] onPad:pad cancelled:NO];

        XCTAssertGreaterThanOrEqual(CGRectGetMinX(stickRing), CGRectGetMinX(lower) - 0.5,
                                    @"The joystick remains inside the lower safe area (case %lu)", (unsigned long)i);
        XCTAssertLessThanOrEqual(CGRectGetMaxX(stickRing), CGRectGetMaxX(lower) + 0.5,
                                 @"The joystick remains inside the lower safe area (case %lu)", (unsigned long)i);
        XCTAssertGreaterThanOrEqual(faces.x - 77 * u, CGRectGetMinX(lower) - 0.5,
                                    @"The face cluster's expanded targets remain inside the lower safe area (case %lu)", (unsigned long)i);
        XCTAssertLessThanOrEqual(faces.x + 77 * u, CGRectGetMaxX(lower) + 0.5,
                                 @"The face cluster's expanded targets remain inside the lower safe area (case %lu)", (unsigned long)i);
    }
}

// A standing phone's pad: two rows of controls, the left stick and the ABXY diamond in the upper one, the D-pad and the right stick in the lower one,
// each in its own column. The right stick has a ring of its own, is drawn there and is pressed there.
- (void)testPortraitPadLaysTwoRowsWithARightStickOfItsOwn {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    UIEdgeInsets insets = UIEdgeInsetsMake(59, 0, 34, 0);
    DmLayout layout = [self portraitControlLayoutForBounds:bounds insets:insets division:CGRectNull];
    UIView *pad = [self padForLayout:layout bounds:bounds];
    CGRect band = layout.controls;

    CGPoint leftColumn = dm_pad_portrait_column(band, NO), rightColumn = dm_pad_portrait_column(band, YES);
    CGFloat upperRow = dm_pad_portrait_row(band, NO, YES).y, lowerRow = dm_pad_portrait_row(band, YES, YES).y;
    XCTAssertGreaterThan(lowerRow, upperRow, @"The lower row is below the upper one");
    XCTAssertGreaterThan(upperRow, CGRectGetMinY(band), @"The upper row leaves the top of the band clear for the panel");
    XCTAssertLessThan(lowerRow, CGRectGetMaxY(band), @"The lower row stays inside the band");

    // The left stick's ring is in the upper row's left column.
    CGRect stickRing = [self stickRingAt:CGPointMake(leftColumn.x, upperRow) inPad:pad];
    XCTAssertFalse(CGRectIsNull(stickRing), @"The left stick is drawn");
    XCTAssertEqualWithAccuracy(CGRectGetMidX(stickRing), leftColumn.x, 0.5, @"The stick is in the left column");
    XCTAssertEqualWithAccuracy(CGRectGetMidY(stickRing), upperRow, 0.5, @"The stick is in the upper row");

    // The right stick's ring is drawn in the lower row's right column: a circle of the stick's radius, next to the D-pad.
    CGRect rightRing = [self stickRingAt:CGPointMake(rightColumn.x, lowerRow) inPad:pad];
    XCTAssertFalse(CGRectIsNull(rightRing), @"The right stick is drawn");
    XCTAssertEqualWithAccuracy(CGRectGetMidX(rightRing), rightColumn.x, 0.5, @"The right stick is in the right column");
    XCTAssertEqualWithAccuracy(CGRectGetMidY(rightRing), lowerRow, 0.5, @"The right stick is in the lower row");
    // The lower row reads as one size: the right stick is drawn at the D-pad's own scale, a little under the left stick.
    XCTAssertEqualWithAccuracy(rightRing.size.width, stickRing.size.width * DM_PAD_DPAD_SCALE, 0.5, @"The right stick is drawn at the D-pad's scale");

    // Every shoulder is drawn at the ABXY key's radius, so the top row and the diamond read as one size. A shoulder's teardrop carries its tip 1.45
    // radii from the centre, so its bounding box is about 2.025 times the radius where a key's is two.
    CGFloat keyR = [self controlPathBoundsNamed:@"A" inPad:pad].size.width / 2;
    XCTAssertEqualWithAccuracy([self controlPathBoundsNamed:@"ZL" inPad:pad].size.width / keyR, 2.025, 0.06, @"A shoulder is drawn at the key's size");

    // The keys keep their gap: neighbouring keys' drawn circles do not touch (the first generation's did, at 3 u).
    CGPoint a = [self controlCenterNamed:@"A" inPad:pad], b = [self controlCenterNamed:@"B" inPad:pad];
    CGFloat faceR = [self controlPathBoundsNamed:@"A" inPad:pad].size.width / 2;
    XCTAssertGreaterThan(hypot(a.x - b.x, a.y - b.y), 2 * faceR + 8, @"Neighbouring keys keep a gap of their own size, not a crowded row");

    // The later generation's profile leaves 2D out of the menu; a saved 2D choice still applies (dm_pad_layout_set).
    XCTAssertFalse(dm_pad_layout_offered(0), @"The menu offers no 2D layout");
    XCTAssertTrue(dm_pad_layout_offered(1), @"3D stays");
    XCTAssertTrue(dm_pad_layout_offered(2), @"Tap stays: this profile's is the band's panel, which takes touches in either console mode");

    // A touch in the right stick's ring drives the right stick and moves nothing else.
    DuoFakeTouch *touch = [self touchAt:CGPointMake(rightColumn.x, lowerRow) time:1];
    [self beginTouches:@[touch] onPad:pad];
    [self moveTouch:touch to:CGPointMake(rightColumn.x + 30, lowerRow) onPad:pad];
    DuoHarnessInput input = duo_harness_input();
    XCTAssertGreaterThan(input.rx, 0, @"The right stick answers in its own ring");
    XCTAssertEqual(input.buttons, 0, @"The right stick is no button");
    [self endTouches:@[touch] onPad:pad cancelled:NO];
}

// The same pad, laid out lying down and then standing up: the right stick's ring comes on and off with the band, since the panel takes the picture's
// place below it. A ring that is drawn but hidden would be a control the player cannot see.
- (void)testRightStickRingFollowsTheLayoutOfOnePad {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    DmLayout lying = dm_layout(CGRectMake(0, 0, 844, 390), UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
    UIView *pad = [self padForLayout:lying bounds:bounds];
    XCTAssertTrue([self rightStickRingLayerInPad:pad].hidden, @"A lying phone leaves the right stick undrawn (a touch in the right third holds it)");
    // Lying down the ring is gone, but the right third still holds the undrawn stick at its full size.
    CGRect ctl = lying.controls;
    CGPoint inRightThird = CGPointMake(CGRectGetMaxX(ctl) - ctl.size.width * 0.15, CGRectGetMidY(ctl));
    DuoFakeTouch *touch = [self touchAt:inRightThird time:2];
    [self beginTouches:@[touch] onPad:pad];
    [self moveTouch:touch to:CGPointMake(inRightThird.x + 30, inRightThird.y) onPad:pad];
    XCTAssertGreaterThan(duo_harness_input().rx, 0, @"The right third holds the undrawn right stick");
    [self endTouches:@[touch] onPad:pad cancelled:NO];

    DmLayout standing = [self portraitControlLayoutForBounds:bounds insets:UIEdgeInsetsMake(59, 0, 34, 0) division:CGRectNull];
    dm_gamepad_arrange(pad, standing);
    [pad setNeedsLayout]; [pad layoutIfNeeded];
    XCTAssertFalse([self rightStickRingLayerInPad:pad].hidden, @"Standing up, the right stick's ring is shown with the band");

    dm_gamepad_arrange(pad, lying);
    [pad setNeedsLayout]; [pad layoutIfNeeded];
    XCTAssertTrue([self rightStickRingLayerInPad:pad].hidden, @"Lying down again, it goes");
}

// This profile's pad switches between the pad and the game's own touch screen on a triple tap in the bottom strip, standing or lying, and the
// switch is the run's own: the stored layout never changes.
- (void)testBottomTripleTapSwitchesThePadAndTouchScreen {
    CGRect sizes[] = {CGRectMake(0, 0, 390, 844), CGRectMake(0, 0, 844, 390)};
    NSString *saved = [NSUserDefaults.standardUserDefaults objectForKey:@"DmPadLayout"];
    dm_set_opmode(0);
    for (NSUInteger i = 0; i < 2; i++) {
        CGRect bounds = sizes[i];
        UIEdgeInsets insets = UIEdgeInsetsMake(59, 0, 34, 0);
        DmLayout layout = bounds.size.height > bounds.size.width
            ? [self portraitControlLayoutForBounds:bounds insets:insets division:CGRectNull]
            : dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
        UIView *pad = [self padForLayout:layout bounds:bounds];
        dm_pad_layout_set(1);
        CGRect c = layout.controls;
        CGPoint bottom = CGPointMake(CGRectGetMidX(c), CGRectGetMaxY(c) - 10);   // on the home indicator

        // Only the indicator's place answers: three taps well above it switch nothing.
        CGPoint beside = CGPointMake(CGRectGetMidX(c), CGRectGetMaxY(c) - 110);
        for (NSNumber *t in @[@0.5, @0.6, @0.7]) {
            DuoFakeTouch *touch = [self touchAt:beside time:t.doubleValue];
            [self beginTouches:@[touch] onPad:pad]; [self endTouches:@[touch] onPad:pad cancelled:NO];
        }
        XCTAssertEqual(dm_pad_layout(), 1, @"Only the home indicator's place switches (case %lu)", (unsigned long)i);

        // The pad takes the tap there even though no control did, so the panel below cannot swallow the triple tap.
        XCTAssertEqual([pad hitTest:bottom withEvent:nil], pad, @"The pad's own touch area reaches the triple tap (case %lu)", (unsigned long)i);
        // Standing up the strip stops at the safe area's edge: the band's bar above it keeps its own taps (its tabs), so a
        // panel button there is not swallowed by the triple tap.
        if (i == 0) {
            CGPoint onBar = CGPointMake(CGRectGetMidX(c), CGRectGetMaxY(c) - 34 - 4);
            XCTAssertNil([pad hitTest:onBar withEvent:nil], @"A tap on the panel's bar reaches the panel");
        }

        // Two taps are not a switch.
        [self beginTouches:@[[self touchAt:bottom time:1]] onPad:pad]; [self endTouches:@[[self touchAt:bottom time:1]] onPad:pad cancelled:NO];
        [self beginTouches:@[[self touchAt:bottom time:1.1]] onPad:pad]; [self endTouches:@[[self touchAt:bottom time:1.1]] onPad:pad cancelled:NO];
        XCTAssertEqual(dm_pad_layout(), 1, @"Two taps leave the pad (case %lu)", (unsigned long)i);

        // Three, half a second apart at most, switch to Tap, and three more switch back.
        for (NSNumber *t in @[@2.0, @2.4, @2.8]) {
            DuoFakeTouch *touch = [self touchAt:bottom time:t.doubleValue];
            [self beginTouches:@[touch] onPad:pad]; [self endTouches:@[touch] onPad:pad cancelled:NO];
        }
        XCTAssertEqual(dm_pad_layout(), 2, @"A bottom triple tap switches to Tap, also a slow one (case %lu)", (unsigned long)i);
        XCTAssertEqual(duo_harness_input().buttons, 0, @"The taps are swallowed, not pressed (case %lu)", (unsigned long)i);
        for (NSNumber *t in @[@3.0, @3.1, @3.2]) {
            DuoFakeTouch *touch = [self touchAt:bottom time:t.doubleValue];
            [self beginTouches:@[touch] onPad:pad]; [self endTouches:@[touch] onPad:pad cancelled:NO];
        }
        XCTAssertEqual(dm_pad_layout(), 1, @"Three more switch the pad back (case %lu)", (unsigned long)i);

        // The indicator's own length plus the finger's slop: the system draws it twice as long lying down, so a tap near its end counts in
        // both orientations where a tap beyond it does not.
        CGPoint atEnd = CGPointMake(CGRectGetMidX(c) + (i == 0 ? 70 : 120), CGRectGetMaxY(c) - 10);
        for (NSNumber *t in @[@4.0, @4.1, @4.2]) {
            DuoFakeTouch *touch = [self touchAt:atEnd time:t.doubleValue];
            [self beginTouches:@[touch] onPad:pad]; [self endTouches:@[touch] onPad:pad cancelled:NO];
        }
        XCTAssertEqual(dm_pad_layout(), 2, @"A triple tap at the indicator's end switches (case %lu)", (unsigned long)i);
        for (NSNumber *t in @[@5.0, @5.1, @5.2]) {
            DuoFakeTouch *touch = [self touchAt:atEnd time:t.doubleValue];
            [self beginTouches:@[touch] onPad:pad]; [self endTouches:@[touch] onPad:pad cancelled:NO];
        }
        XCTAssertEqual(dm_pad_layout(), 1, @"Three more at the end switch it back (case %lu)", (unsigned long)i);
        CGPoint past = CGPointMake(CGRectGetMidX(c) + (i == 0 ? 150 : 200), CGRectGetMaxY(c) - 10);
        for (NSNumber *t in @[@6.0, @6.1, @6.2]) {
            DuoFakeTouch *touch = [self touchAt:past time:t.doubleValue];
            [self beginTouches:@[touch] onPad:pad]; [self endTouches:@[touch] onPad:pad cancelled:NO];
        }
        XCTAssertEqual(dm_pad_layout(), 1, @"Past the indicator's end nothing switches (case %lu)", (unsigned long)i);
    }
    XCTAssertEqualObjects([NSUserDefaults.standardUserDefaults objectForKey:@"DmPadLayout"], saved ?: @1, @"The run's switch is never stored");
}

// The indicator's length follows the screen's short side, so a wider phone's trigger reaches further: a 440-point phone answers at a point a
// 390-point one's never would (a fixed length fails this).
- (void)testBottomTripleTapAreaFollowsTheScreensWidth {
    CGRect bounds = CGRectMake(0, 0, 440, 956);   // an iPhone 16 Pro Max
    DmLayout layout = [self portraitControlLayoutForBounds:bounds insets:UIEdgeInsetsMake(62, 0, 34, 0) division:CGRectNull];
    UIView *pad = [self padForLayout:layout bounds:bounds];
    NSString *saved = [NSUserDefaults.standardUserDefaults objectForKey:@"DmPadLayout"];
    dm_set_opmode(0); dm_pad_layout_set(1);
    CGRect c = layout.controls;
    CGPoint p = CGPointMake(CGRectGetMidX(c) + 85, CGRectGetMaxY(c) - 10);
    for (NSNumber *t in @[@1.0, @1.1, @1.2]) {
        DuoFakeTouch *touch = [self touchAt:p time:t.doubleValue];
        [self beginTouches:@[touch] onPad:pad]; [self endTouches:@[touch] onPad:pad cancelled:NO];
    }
    XCTAssertEqual(dm_pad_layout(), 2, @"The indicator's length follows the screen: a wider phone's end answers");
    dm_pad_layout_set(1);
    XCTAssertEqualObjects([NSUserDefaults.standardUserDefaults objectForKey:@"DmPadLayout"], saved ?: @1, @"The run's switch is never stored");
}

- (void)testPortraitThreeDimensionalAnchorsKeepStickDpadFaceAndGameTouchesIndependent {
    CGRect bounds = CGRectMake(0, 0, 669, 951);
    UIEdgeInsets insets = UIEdgeInsetsMake(62, 0, 34, 0);
    DmLayout layout = [self portraitControlLayoutForBounds:bounds insets:insets division:CGRectMake(0, 455.5, 669, 40)];
    UIView *pad = [self padForLayout:layout bounds:bounds];
    CGRect lower = layout.controls;
    CGFloat u = [self portraitUnitForLayout:layout];
    CGFloat upperRow = dm_pad_portrait_row(lower, NO, YES).y;
    CGFloat lowerRow = dm_pad_portrait_row(lower, YES, YES).y;
    CGPoint stickCenter = CGPointMake(dm_pad_portrait_column(lower, NO).x, upperRow);
    CGPoint dpadCenter = CGPointMake(dm_pad_portrait_column(lower, NO).x, lowerRow);
    CGPoint faceA = CGPointMake(dm_pad_portrait_column(lower, YES).x + dm_pad_portrait_sizes(YES).faceGap * u, upperRow);
    CGPoint video = CGPointMake(CGRectGetMidX(layout.game), CGRectGetMidY(layout.game));
    DuoFakeTouch *stick = [self touchAt:stickCenter time:1];
    DuoFakeTouch *right = [self touchAt:CGPointMake(dpadCenter.x + 0.6 * dm_pad_portrait_sizes(YES).dpadR * u, dpadCenter.y) time:1];
    DuoFakeTouch *a = [self touchAt:faceA time:1];
    DuoFakeTouch *game = [self touchAt:video time:1];

    [self beginTouches:@[stick, right, a, game] onPad:pad];
    [self moveTouch:stick to:CGPointMake(stickCenter.x + 30 * u, stickCenter.y) onPad:pad];
    DuoHarnessInput input = duo_harness_input();
    XCTAssertGreaterThan(input.lx, 0, @"The stick touch should remain owned while three other touches begin");
    XCTAssertEqual(input.buttons & (DM_BTN_RIGHT | DM_BTN_A), DM_BTN_RIGHT | DM_BTN_A,
                   @"D-pad and ABXY retain separate button ownership");
    XCTAssertEqual(input.touch_count, 1, @"The game viewport keeps its own touch slot alongside the controls");
    XCTAssertEqualWithAccuracy(input.touches[0].x, 0.5, 0.001);
    XCTAssertEqualWithAccuracy(input.touches[0].y, 0.5, 0.001);
    [self endTouches:@[stick, right, a, game] onPad:pad cancelled:NO];
    input = duo_harness_input();
    XCTAssertEqual(input.buttons, 0);
    XCTAssertEqual(input.touch_count, 0);
}

- (void)testLandscapeControlAnchorsAndMultitouchKeepTheirExistingLayout {
    CGRect bounds = CGRectMake(0, 0, 844, 390);
    DmLayout layout = dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
    XCTAssertFalse(layout.separated, @"Landscape keeps the overlay layout");
    XCTAssertTrue(CGRectEqualToRect(layout.game, dm_aspect_fit(bounds, DM_PICTURE_16_9)));
    UIView *pad = [self padForLayout:layout bounds:bounds];
    CGFloat u = MIN(bounds.size.height / 430.0, 1.3);
    // The profile's later landscape layout: the three clusters on one line, each keeping the shoulders' margin to the screen's edge.
    CGFloat reach = (DM_PAD_SPACED_EDGE_X_U + 77) * u, line = bounds.size.height - (DM_PAD_SPACED_EDGE_Y_U + 77) * u;
    CGRect stickRing = [self largestShapePathBoundsInLayer:pad.layer];
    XCTAssertEqualWithAccuracy(CGRectGetMidX(stickRing), reach, 0.5);
    XCTAssertEqualWithAccuracy(CGRectGetMidY(stickRing), line, 0.5);
    CGPoint faces = [self faceDiamondCenterInPad:pad];
    XCTAssertEqualWithAccuracy(faces.x, bounds.size.width - reach, 0.5);
    XCTAssertEqualWithAccuracy(faces.y, line, 0.5);

    CGPoint stickCenter = CGPointMake(reach, line);
    CGPoint dpadCenter = CGPointMake(DM_PAD_SPACED_LOW_X_U * u, line);
    CGPoint aCenter = [self faceAIn:layout bounds:bounds];
    CGPoint video = CGPointMake(CGRectGetMidX(layout.game), CGRectGetMidY(layout.game));
    DuoFakeTouch *stick = [self touchAt:stickCenter time:1];
    DuoFakeTouch *right = [self touchAt:CGPointMake(dpadCenter.x + 0.6 * dm_pad_portrait_sizes(YES).dpadR * u, dpadCenter.y) time:1];
    DuoFakeTouch *a = [self touchAt:aCenter time:1];
    DuoFakeTouch *game = [self touchAt:video time:1];
    [self beginTouches:@[stick, right, a, game] onPad:pad];
    [self moveTouch:stick to:CGPointMake(stickCenter.x + 30 * u, stickCenter.y) onPad:pad];
    DuoHarnessInput input = duo_harness_input();
    XCTAssertGreaterThan(input.lx, 0);
    XCTAssertEqual(input.buttons & (DM_BTN_RIGHT | DM_BTN_A), DM_BTN_RIGHT | DM_BTN_A);
    XCTAssertEqual(input.touch_count, 1);
    [self endTouches:@[stick, right, a, game] onPad:pad cancelled:NO];
    input = duo_harness_input();
    XCTAssertEqual(input.buttons, 0);
    XCTAssertEqual(input.touch_count, 0);
}

- (void)testPictureFillCoversTheLandscapeViewWithoutStretching {
    XCTAssertTrue(dm_picture_fill("fill"));
    XCTAssertFalse(dm_picture_fill("fit"));
    XCTAssertFalse(dm_picture_fill(NULL), @"Unset keeps Fit, today's picture");
    XCTAssertFalse(dm_picture_fill(""));
    const CGRect sizes[] = {CGRectMake(0, 0, 844, 390), CGRectMake(0, 0, 932, 430), CGRectMake(0, 0, 1024, 768), CGRectMake(0, 0, 1280, 720)};
    for (NSUInteger i = 0; i < 4; i++) {
        CGRect bounds = sizes[i];
        DmLayout fit = dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
        DmLayout fill = dm_layout_picture(fit, bounds, YES, DM_PICTURE_16_9);
        XCTAssertTrue(CGRectEqualToRect(dm_layout_picture(fit, bounds, NO, DM_PICTURE_16_9).game, fit.game), @"Fit leaves the layout as it was");
        XCTAssertFalse(fill.separated);
        XCTAssertTrue(CGRectEqualToRect(fill.controls, bounds), @"The touch controls keep the full view");
        XCTAssertEqualWithAccuracy(fill.game.size.width / fill.game.size.height, 16.0 / 9.0, 0.0001, @"Fill scales, never stretches");
        XCTAssertTrue(CGRectContainsRect(CGRectInset(fill.game, -0.001, -0.001), bounds), @"Fill covers the whole view");
        XCTAssertEqualWithAccuracy(CGRectGetMidX(fill.game), CGRectGetMidX(bounds), 0.001);
        XCTAssertEqualWithAccuracy(CGRectGetMidY(fill.game), CGRectGetMidY(bounds), 0.001);
        XCTAssertTrue(fill.game.size.width == bounds.size.width || fill.game.size.height == bounds.size.height,
                      @"The smallest cover: one side matches the view");
    }
    CGRect wide = dm_aspect_fill(CGRectMake(0, 0, 844, 390), DM_PICTURE_16_9);
    XCTAssertEqualWithAccuracy(wide.size.height, 474.75, 0.001);
    XCTAssertEqualWithAccuracy(CGRectGetMinY(wide), -42.375, 0.001, @"A wide iPhone crops the top and the bottom");
    CGRect pad = dm_aspect_fill(CGRectMake(0, 0, 1024, 768), DM_PICTURE_16_9);
    XCTAssertEqualWithAccuracy(CGRectGetMinX(pad), -170.667, 0.001, @"A 4:3 iPad crops the sides");
    XCTAssertTrue(CGRectIsEmpty(dm_aspect_fill(CGRectZero, DM_PICTURE_16_9)));
}

- (void)testPictureFillKeepsPortraitAndTabletopSplitsFitted {
    CGRect portrait = CGRectMake(0, 0, 390, 844);
    DmLayout split = dm_layout(portrait, UIEdgeInsetsMake(59, 0, 34, 0), NO, CGRectNull, DM_PICTURE_16_9);
    DmLayout filled = dm_layout_picture(split, portrait, YES, DM_PICTURE_16_9);
    XCTAssertTrue(filled.separated);
    XCTAssertTrue(CGRectEqualToRect(filled.game, split.game), @"Portrait keeps the picture fitted above the controls");
    CGRect landscape = CGRectMake(0, 0, 844, 390);
    DmLayout tabletop = dm_layout(landscape, UIEdgeInsetsZero, YES, CGRectMake(0, 180, 844, 24), DM_PICTURE_16_9);
    XCTAssertTrue(tabletop.separated);
    XCTAssertTrue(CGRectEqualToRect(dm_layout_picture(tabletop, landscape, YES, DM_PICTURE_16_9).game, tabletop.game), @"Tabletop keeps the picture fitted in its half");
}

- (void)testPictureFillTouchesFollowTheCroppedPicture {
    CGRect bounds = CGRectMake(0, 0, 844, 390);
    DmLayout layout = dm_layout_picture(dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9), bounds, YES, DM_PICTURE_16_9);
    CGPoint top = dm_game_touch_point(layout.game, CGPointMake(422, 0));
    XCTAssertEqualWithAccuracy(top.x, 0.5, 0.001);
    XCTAssertEqualWithAccuracy(top.y, 42.375 / 474.75, 0.001, @"The view's top edge is below the cropped top of the picture");
    CGPoint bottom = dm_game_touch_point(layout.game, CGPointMake(0, 390));
    XCTAssertEqualWithAccuracy(bottom.x, 0, 0.001);
    XCTAssertEqualWithAccuracy(bottom.y, (390 + 42.375) / 474.75, 0.001);
    CGPoint outside = dm_game_touch_point(layout.game, CGPointMake(-50, 900));
    XCTAssertEqualWithAccuracy(outside.x, 0, 0.0001);
    XCTAssertEqualWithAccuracy(outside.y, 0.999, 0.0001);

    UIView *pad = [self padForLayout:layout bounds:bounds];
    DuoFakeTouch *game = [self touchAt:CGPointMake(300, 150) time:1];
    [self beginTouches:@[game] onPad:pad];
    DuoHarnessInput input = duo_harness_input();
    XCTAssertEqual(input.touch_count, 1, @"A touch on no control goes to the game");
    XCTAssertEqualWithAccuracy(input.touches[0].x, 300 / 844.0, 0.001);
    XCTAssertEqualWithAccuracy(input.touches[0].y, (150 + 42.375) / 474.75, 0.001, @"Normalized on the scaled picture, not the view");
    [self endTouches:@[game] onPad:pad cancelled:NO];
    XCTAssertEqual(duo_harness_input().touch_count, 0);
}

- (void)testTallerPictureFillsAScreenOfItsShape {
    // DM_SCREEN_ASPECT=1.43: the game presents 1920x1344, and an unfolded iPhone Duo's landscape view has that shape
    CGFloat tall = 1920.0 / 1344.0;
    CGRect bounds = CGRectMake(0, 0, 1000, 700);
    DmLayout fit = dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, tall);
    XCTAssertTrue(CGRectEqualToRect(fit.game, bounds), @"Fit fills the screen with the picture of its shape: %@", NSStringFromCGRect(fit.game));
    XCTAssertTrue(CGRectEqualToRect(dm_layout_picture(fit, bounds, YES, tall).game, bounds), @"Fill crops nothing");
    XCTAssertEqualWithAccuracy(dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9).game.size.height, 562.5, 0.001, @"A 16:9 picture leaves bars");
    CGPoint top = dm_game_touch_point(fit.game, CGPointMake(500, 0));
    XCTAssertEqualWithAccuracy(top.y, 0, 0.0001, @"The view's top edge is the picture's top edge");
    CGRect phone = CGRectMake(0, 0, 844, 390);
    CGRect wide = dm_layout(phone, UIEdgeInsetsZero, NO, CGRectNull, tall).game;
    XCTAssertEqualWithAccuracy(wide.size.height, 390, 0.001, @"A wider screen fits the taller picture by its height");
    XCTAssertEqualWithAccuracy(wide.size.width / wide.size.height, tall, 0.01, @"Never stretched");
}

- (void)testProductionRootFollowsThePresentedPictureShape {
    UIViewController *root = [self harnessRootViewController];
    UIView *game = [root valueForKey:@"game"];
    front_picture_shape(1920, 1344);
    XCTestExpectation *applied = [self expectationWithDescription:@"shape applied"];
    dispatch_async(dispatch_get_main_queue(), ^{ [applied fulfill]; });
    [self waitForExpectations:@[applied] timeout:2];
    [root.view layoutIfNeeded];
    BOOL portrait = root.view.bounds.size.height > root.view.bounds.size.width;
    XCTAssertEqualWithAccuracy(game.frame.size.width / game.frame.size.height, 1920.0 / 1344.0, 0.01, @"The game's view takes the presented shape: %@", NSStringFromCGRect(game.frame));
    if (!portrait) XCTAssertEqualWithAccuracy(game.frame.size.height, root.view.bounds.size.height, 1.0);
    front_picture_shape(1920, 1080);   // back to 16:9 for the other tests
    XCTestExpectation *restored = [self expectationWithDescription:@"16:9 restored"];
    dispatch_async(dispatch_get_main_queue(), ^{ [restored fulfill]; });
    [self waitForExpectations:@[restored] timeout:2];
    [root.view layoutIfNeeded];
    XCTAssertEqualWithAccuracy(game.frame.size.width / game.frame.size.height, 16.0 / 9.0, 0.01);
}

- (void)testSeparatedHitTargetsMeetTheMinimumsAtTheirDrawnSize {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    DmLayout layout = [self separatedLayout:bounds];
    UIView *pad = [self padForLayout:layout bounds:bounds];
    CGFloat u = [self portraitUnitForLayout:layout];

    // Probe just inside 44 pt face targets, inward from each visible button center. The
    // expected button remains the nearest target even where neighboring target circles overlap.
    NSArray<NSString *> *faces = @[@"A", @"B", @"X", @"Y"];
    const uint64_t faceBits[] = {DM_BTN_A, DM_BTN_B, DM_BTN_X, DM_BTN_Y};
    const CGPoint faceOffsets[] = {{-21.5, 0}, {0, -21.5}, {0, 21.5}, {21.5, 0}};
    for (NSUInteger i = 0; i < faces.count; i++) {
        NSString *name = faces[i];
        CGPoint center = [self controlCenterNamed:name inPad:pad];
        CGPoint probe = CGPointMake(center.x + faceOffsets[i].x, center.y + faceOffsets[i].y);
        [self assertButton:faceBits[i] at:probe onPad:pad time:10 + i];
    }
    // The portrait split draws its keys a quarter larger, so the artwork covers the 44 pt probe; the target is still at least 44 pt.
    DmPadPortraitSizes zs = dm_pad_portrait_sizes(YES);
    CGFloat faceR = zs.faceR * u, faceGap = zs.faceGap * u;
    CGFloat faceHitR = MIN(MAX(faceR * 1.25, faceGap * 0.75), 1.2 * faceGap);
    XCTAssertGreaterThanOrEqual(faceHitR * 2, 44, @"A face key answers a 44 pt target");
    XCTAssertEqualWithAccuracy([self controlPathBoundsNamed:@"A" inPad:pad].size.width, faceR * 2, 0.01, @"The portrait split draws its keys a quarter larger, at nine tenths of the room");

    // Shoulder buttons, triggers, and stick clicks are frequent 44 pt targets too. The split draws them a quarter larger, so the artwork covers the
    // 44 pt probe at ZL and ZR; the target still answers there all the same.
    NSArray<NSString *> *wideButtons = @[@"ZL", @"L", @"ZR", @"R", @"L3", @"R3"];
    const uint64_t wideBits[] = {DM_BTN_ZL, DM_BTN_L, DM_BTN_ZR, DM_BTN_R, DM_BTN_LS, DM_BTN_RS};
    // Probe outward on corner controls that overlap expanded L3/R3 targets.
    const CGPoint wideOffsets[] = {{0, -21.5}, {0, 21.5}, {0, -21.5}, {0, 21.5}, {21.5, 0}, {-21.5, 0}};
    for (NSUInteger i = 0; i < wideButtons.count; i++) {
        NSString *name = wideButtons[i];
        CGPoint center = [self controlCenterNamed:name inPad:pad];
        CGPoint probe = CGPointMake(center.x + wideOffsets[i].x, center.y + wideOffsets[i].y);
        [self assertButton:wideBits[i] at:probe onPad:pad time:20 + i];
    }
    // A shoulder is a teardrop at the ABXY key's radius (its tip reaches past the circle); a stick click is that key's circle, a little smaller.
    CGFloat aR = [self controlPathBoundsNamed:@"A" inPad:pad].size.width / 2;
    XCTAssertEqualWithAccuracy([self controlPathBoundsNamed:@"L" inPad:pad].size.width / aR, 2.025, 0.06, @"A shoulder is drawn at the key's size");
    XCTAssertEqualWithAccuracy([self controlPathBoundsNamed:@"L3" inPad:pad].size.width, 17 * u * 1.25 * 2, 0.01, @"The stick clicks keep their own size");

    // Minus/plus use 28 pt targets. Their vertical probe is outside the drawn pill.
    NSArray<NSString *> *menuButtons = @[@"−", @"+"];
    const uint64_t menuBits[] = {DM_BTN_MINUS, DM_BTN_PLUS};
    for (NSUInteger i = 0; i < menuButtons.count; i++) {
        NSString *name = menuButtons[i];
        CGPoint center = [self controlCenterNamed:name inPad:pad];
        CGPoint probe = CGPointMake(center.x, center.y + 13.25);
        XCTAssertLessThan([self controlPathBoundsNamed:name inPad:pad].size.height, 28);
        XCTAssertFalse([self controlNamed:name containsPoint:probe inPad:pad], @"%@ artwork should keep its scaled visual size", name);
        [self assertButton:menuBits[i] at:probe onPad:pad time:30 + i];
    }

    UIButton *menu = [self menuButtonInPad:pad];
    CGPoint menuProbe = CGPointMake(menu.center.x, menu.center.y + 13.7);
    XCTAssertLessThan(menu.bounds.size.width, 28);
    XCTAssertFalse(CGRectContainsPoint(menu.frame, menuProbe), @"The menu button's artwork keeps its scaled size");
    XCTAssertEqual([pad hitTest:menuProbe withEvent:nil], menu, @"Its 44 pt target answers outside the drawn circle");
}

- (void)testExpandedTargetsRemainReachableWithinSafeAreaInNarrowSeparated {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    DmLayout layout = [self separatedLayout:bounds];
    // Model a 16 pt side safe area and 34 pt bottom safe area without depending on
    // the simulator's current display insets.
    layout.controls = CGRectMake(16, CGRectGetMinY(layout.controls), bounds.size.width - 32,
                                 CGRectGetHeight(layout.controls) - 34);
    UIView *pad = [self padForLayout:layout bounds:bounds];
    CGRect controls = layout.controls;

    for (NSString *name in @[@"A", @"B", @"X", @"Y"]) {
        CGPoint center = [self controlCenterNamed:name inPad:pad];
        XCTAssertGreaterThanOrEqual(center.x - 22, CGRectGetMinX(controls) - 0.01);
        XCTAssertLessThanOrEqual(center.x + 22, CGRectGetMaxX(controls) + 0.01);
        XCTAssertGreaterThanOrEqual(center.y - 22, CGRectGetMinY(controls) - 0.01);
        XCTAssertLessThanOrEqual(center.y + 22, CGRectGetMaxY(controls) + 0.01);
    }
    for (NSString *name in @[@"ZL", @"L", @"ZR", @"R", @"L3", @"R3"]) {
        CGPoint center = [self controlCenterNamed:name inPad:pad];
        XCTAssertGreaterThanOrEqual(center.x - 22, CGRectGetMinX(controls) - 0.01);
        XCTAssertLessThanOrEqual(center.x + 22, CGRectGetMaxX(controls) + 0.01);
        XCTAssertGreaterThanOrEqual(center.y - 22, CGRectGetMinY(controls) - 0.01);
        XCTAssertLessThanOrEqual(center.y + 22, CGRectGetMaxY(controls) + 0.01);
    }

    CGPoint a = [self controlCenterNamed:@"A" inPad:pad];
    CGPoint b = [self controlCenterNamed:@"B" inPad:pad];
    DuoFakeTouch *right = [self touchAt:CGPointMake(a.x + 21.5, a.y) time:1];
    [self beginTouches:@[right] onPad:pad];
    XCTAssertEqual(duo_harness_input().buttons, DM_BTN_A);
    [self endTouches:@[right] onPad:pad cancelled:NO];
    DuoFakeTouch *bottom = [self touchAt:CGPointMake(b.x, b.y + 21.5) time:2];
    [self beginTouches:@[bottom] onPad:pad];
    XCTAssertEqual(duo_harness_input().buttons, DM_BTN_B);
    [self endTouches:@[bottom] onPad:pad cancelled:NO];

    CGPoint zl = [self controlCenterNamed:@"ZL" inPad:pad];
    DuoFakeTouch *left = [self touchAt:CGPointMake(zl.x - 21.5, zl.y) time:3];
    [self beginTouches:@[left] onPad:pad];
    XCTAssertEqual(duo_harness_input().buttons, DM_BTN_ZL);
    [self endTouches:@[left] onPad:pad cancelled:NO];

    UIButton *menu = [self menuButtonInPad:pad];
    // The gear answers its 44-point circle wherever that lies in the safe area: the later pad's top row sits lower, so its circle stops short of the
    // very edge and the probe follows it.
    CGPoint menuProbe = CGPointMake(menu.center.x, MAX(CGRectGetMinY(controls) + 0.8, menu.center.y - 21.5));
    XCTAssertEqual([pad hitTest:menuProbe withEvent:nil], menu, @"The menu button stays reachable at the top of the safe area");
}

- (void)testFaceTargetsKeepTheirMultiButtonSlideBehavior {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    DmLayout layout = [self separatedLayout:bounds];
    UIView *pad = [self padForLayout:layout bounds:bounds];
    CGPoint a = [self controlCenterNamed:@"A" inPad:pad];
    CGPoint b = [self controlCenterNamed:@"B" inPad:pad];
    DuoFakeTouch *touch = [self touchAt:a time:1];

    [self beginTouches:@[touch] onPad:pad];
    XCTAssertEqual(duo_harness_input().buttons, DM_BTN_A);
    [self moveTouch:touch to:CGPointMake((a.x + b.x) / 2, (a.y + b.y) / 2) onPad:pad];
    XCTAssertEqual(duo_harness_input().buttons, DM_BTN_A | DM_BTN_B,
                   @"A face touch near two enlarged targets should preserve the intentional union");
    [self moveTouch:touch to:b onPad:pad];
    XCTAssertEqual(duo_harness_input().buttons, DM_BTN_B);
    [self endTouches:@[touch] onPad:pad cancelled:NO];
    XCTAssertEqual(duo_harness_input().buttons, 0);
}

- (void)testOverlappingLockedButtonTargetsChooseNearestAndStayLocked {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    UIView *pad = [self padForLayout:[self separatedLayout:bounds] bounds:bounds];
    CGPoint zl = [self controlCenterNamed:@"ZL" inPad:pad];
    CGPoint ls = [self controlCenterNamed:@"L3" inPad:pad];
    // The two targets' normalized distances cross at 0.60 of the way (their radii differ); 0.75 is clearly nearer to the click.
    CGPoint nearerToL3 = CGPointMake(zl.x + (ls.x - zl.x) * 0.75, zl.y + (ls.y - zl.y) * 0.75);
    DuoFakeTouch *touch = [self touchAt:nearerToL3 time:1];

    [self beginTouches:@[touch] onPad:pad];
    XCTAssertEqual(duo_harness_input().buttons, DM_BTN_LS,
                   @"When enlarged shoulder/click targets overlap, the nearest normalized target wins");
    [self moveTouch:touch to:zl onPad:pad];
    XCTAssertEqual(duo_harness_input().buttons, DM_BTN_LS,
                   @"A regular button stays owned by the touch-down selection while the finger moves");
    [self endTouches:@[touch] onPad:pad cancelled:NO];
    XCTAssertEqual(duo_harness_input().buttons, 0);
}

- (void)testMenuButtonAndMinusOverlapUseNearestTargetThenKeepOwnership {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    UIView *pad = [self padForLayout:[self separatedLayout:bounds] bounds:bounds];
    CGPoint minus = [self controlCenterNamed:@"−" inPad:pad];
    UIButton *menu = [self menuButtonInPad:pad];
    CGPoint m = menu.center;
    XCTAssertEqual([pad hitTest:minus withEvent:nil], pad, @"− at its own centre belongs to the pad");
    XCTAssertEqual([pad hitTest:m withEvent:nil], menu);
    UIView *last = pad; int flips = 0;
    for (int i = 0; i <= 40; i++) {   // from − to the menu button: the nearer normalized target wins, so the owner changes once
        CGPoint p = CGPointMake(minus.x + (m.x - minus.x) * i / 40.0, minus.y + (m.y - minus.y) * i / 40.0);
        UIView *v = [pad hitTest:p withEvent:nil];
        if (v != last) { flips++; last = v; }
    }
    XCTAssertEqual(flips, 1);
    DuoFakeTouch *minusTouch = [self touchAt:minus time:1];
    [self beginTouches:@[minusTouch] onPad:pad];
    [self moveTouch:minusTouch to:m onPad:pad];
    XCTAssertEqual(duo_harness_input().buttons, DM_BTN_MINUS, @"A − touch slid onto the menu button stays −");
    [self endTouches:@[minusTouch] onPad:pad cancelled:NO];
    XCTAssertEqual(duo_harness_input().buttons, 0);
}

- (void)testMenuLayoutPickReleasesHeldKeysAndIsSaved {
    dm_set_opmode(0);
    CGRect bounds = CGRectMake(0, 0, 844, 390);
    DmLayout layout = dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
    UIView *pad = [self padForLayout:layout bounds:bounds];
    dm_pad_layout_set(0);
    DuoFakeTouch *a = [self touchAt:[self faceAIn:layout bounds:bounds] time:1];
    [self beginTouches:@[a] onPad:pad];
    XCTAssertEqual(duo_harness_input().buttons, DM_BTN_A);
    dm_pad_layout_set(1);
    XCTAssertEqual(duo_harness_input().buttons, 0, @"Picking a layout releases every held key");
    XCTAssertEqual(dm_pad_layout(), 1);
    XCTAssertEqualObjects([NSUserDefaults.standardUserDefaults objectForKey:@"DmPadLayout"], @1, @"and is kept for the next start");
    XCTAssertFalse([self textLayerNamed:@"L3" inLayer:pad.layer].superlayer.hidden, @"the 3D layout's keys show");
    dm_pad_layout_set(2);
    UIButton *menu = [self menuButtonInPad:pad];
    XCTAssertFalse(menu.hidden);
    XCTAssertEqual([pad hitTest:menu.center withEvent:nil], menu, @"Tap keeps the menu button, so the menu is still reachable");
    [self endTouches:@[a] onPad:pad cancelled:YES];
}

- (void)testDockingKeepsTheTapLayoutForThisProfile {
    // This profile's Tap is the band's panel, not the game's touch screen, so a TV does not take it away and the menu still offers it.
    dm_set_opmode(0);
    CGRect bounds = CGRectMake(0, 0, 844, 390);
    [self padForLayout:dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9) bounds:bounds];
    dm_pad_layout_set(2);
    XCTAssertEqual(dm_pad_layout(), 2);
    dm_set_opmode(1); dm_gamepad_opmode_changed();
    XCTAssertEqual(dm_pad_layout(), 2, @"Docked, the band's panel still takes the touches: Tap stays");
    XCTAssertTrue(dm_pad_layout_offered(2), @"And the menu offers it");
    dm_pad_layout_set(1);
    dm_pad_layout_set(2);
    XCTAssertEqual(dm_pad_layout(), 2, @"Tap can be picked while docked");
    dm_pad_layout_set(0);
    XCTAssertEqual(dm_pad_layout(), 1, @"2D is never entered: this profile has 3D and Tap only");
    // This profile keeps the layout for the run only: the stored choice is the one setUp made, not the switch.
    XCTAssertEqualObjects([NSUserDefaults.standardUserDefaults objectForKey:@"DmPadLayout"], @1, @"The run's switch is not stored");
    dm_set_opmode(0);
}

- (void)testInGameMenuSettingsTakeEffectAtOnce {
    UIViewController *root = [self harnessRootViewController];
    XCTAssertNotNil(root);
    [root.view layoutIfNeeded];
    UIView *game = [root valueForKey:@"game"];
    XCTAssertTrue(dm_setting_live("DM_PICTURE_FIT", NULL) && dm_setting_live("DM_OPMODE", NULL) && dm_setting_live("DM_RUMBLE", NULL)
                  && dm_setting_live("DM_SHOW_FPS", NULL) && dm_setting_live("DM_TOUCH_HAPTICS", NULL));
    XCTAssertFalse(dm_setting_live("DM_LANG", NULL), @"A setting the game reads once at start is not offered");
    XCTAssertFalse(dm_setting_live("DM_FPS_TARGET", NULL));

    CGRect b = root.view.bounds;
    BOOL duo = [[root valueForKey:@"hingeStatus"] integerValue] != 0;
    DmLayout fit = dm_layout(b, dm_game_safe_insets(b, root.view.safeAreaInsets, duo), [[root valueForKey:@"partiallyOpen"] boolValue], CGRectNull, DM_PICTURE_16_9);
    dm_setting_live("DM_PICTURE_FIT", "fill"); [root.view layoutIfNeeded];
    XCTAssertTrue(CGRectEqualToRect(game.frame, dm_layout_picture(fit, b, YES, DM_PICTURE_16_9).game), @"Fill applies at once: %@", NSStringFromCGRect(game.frame));
    dm_setting_live("DM_PICTURE_FIT", "fit"); [root.view layoutIfNeeded];
    XCTAssertTrue(CGRectEqualToRect(game.frame, fit.game), @"and Fit brings the bars back: %@", NSStringFromCGRect(game.frame));

    dm_setting_live("DM_RUMBLE", "0"); XCTAssertEqual(duo_harness_rumble(), 0);
    dm_setting_live("DM_RUMBLE", "1"); XCTAssertEqual(duo_harness_rumble(), 1);
    dm_setting_live("DM_OPMODE", "1"); XCTAssertEqual(dm_opmode(), 1);
    dm_setting_live("DM_OPMODE", "0"); XCTAssertEqual(dm_opmode(), 0);
    dm_setting_live("DM_SHOW_FPS", "1");
    UILabel *fps = [root valueForKey:@"fps"];
    XCTAssertNotNil(fps); XCTAssertFalse(fps.hidden);
    dm_setting_live("DM_SHOW_FPS", "0"); XCTAssertTrue(fps.hidden);
}

- (void)testGameViewDrawableHonoursAPixelCap {
    UIViewController *root = [self harnessRootViewController];
    [root.view layoutIfNeeded];
    UIView *game = [root valueForKey:@"game"];
    CAMetalLayer *metal = (CAMetalLayer *)game.layer;
    CGFloat full = metal.drawableSize.width;
    XCTAssertGreaterThan(full, 300);
    [game setValue:@300 forKey:@"maxPixelWidth"]; [game setNeedsLayout]; [game layoutIfNeeded];
    XCTAssertEqualWithAccuracy(metal.drawableSize.width, 300, 1, @"A TV's cap survives every later layout pass (DmGameView layoutSubviews)");
    [game setNeedsLayout]; [game layoutIfNeeded];
    XCTAssertEqualWithAccuracy(metal.drawableSize.width, 300, 1);
    [game setValue:@0 forKey:@"maxPixelWidth"]; [game setNeedsLayout]; [game layoutIfNeeded];
    XCTAssertEqualWithAccuracy(metal.drawableSize.width, full, 1);
}

- (void)testGamepadTextLayersRefreshScaleFromTheCurrentTraitCollection {
    if (@available(iOS 17.0, *)) {
        CGRect bounds = CGRectMake(0, 0, 844, 390);
        DmLayout layout = dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
        UIView *pad = [self padForLayout:layout bounds:bounds];
        CATextLayer *face = [self textLayerNamed:@"A" inLayer:pad.layer];
        XCTAssertNotNil(face);
        UIViewController *root = nil;
        for (UIScene *scene in UIApplication.sharedApplication.connectedScenes) {
            if (![scene isKindOfClass:UIWindowScene.class]) continue;
            for (UIWindow *window in ((UIWindowScene *)scene).windows) if (window.isKeyWindow) root = window.rootViewController;
        }
        XCTAssertNotNil(root);
        pad.frame = root.view.bounds;
        [root.view addSubview:pad];
        [root.view layoutIfNeeded];
        [pad layoutIfNeeded];
        CGRect visualBefore = [self controlPathBoundsNamed:@"A" inPad:pad];
        CGFloat currentScale = pad.traitCollection.displayScale;
        CGFloat overrideScale = fabs(currentScale - 1.0) < 0.01 ? 2.0 : 1.0;

        pad.traitOverrides.displayScale = overrideScale;
        [pad setNeedsLayout];
        [pad layoutIfNeeded];

        XCTAssertEqualWithAccuracy(pad.traitCollection.displayScale, overrideScale, 0.001);
        XCTAssertEqualWithAccuracy(face.contentsScale, overrideScale, 0.001);
        XCTAssertEqualWithAccuracy([self controlPathBoundsNamed:@"A" inPad:pad].size.width, visualBefore.size.width, 0.001,
                                   @"Changing the text raster scale must not resize the gamepad artwork");
        pad.traitOverrides.displayScale = currentScale;
        [pad removeFromSuperview];
    }
}

- (void)testFrontRootStartsProductionGamepadAndMatchesCurrentGeometry {
    UIViewController *root = [self harnessRootViewController];
    XCTAssertNotNil(root);
    [root.view layoutIfNeeded];
    UIView *game = [root valueForKey:@"game"];
    UIView *pad = [root valueForKey:@"pad"];
    XCTAssertNotNil(game);
    XCTAssertNotNil(pad);
    CGRect division = CGRectNull;
#if DUO_HARNESS_HAS_NATIVE_POSE_API
    if (@available(iOS 27.1, *)) {
        for (UIViewReservedRegion *region in [root.view reservedRegionsOfKind:UIViewReservedRegionKind.divisionRegionKind options:UIViewReservedRegionQueryOptionsIncludeInactive]) {
            CGRect candidate = dm_horizontal_division(root.view.bounds, region.frame);
            if (CGRectIsNull(division) && !CGRectIsNull(candidate)) division = candidate;
        }
    }
#endif
    BOOL partiallyOpen = [[root valueForKey:@"partiallyOpen"] boolValue];
    BOOL duo = [[root valueForKey:@"hingeStatus"] integerValue] != 0 || !CGRectIsNull(division);
    UIEdgeInsets insets = dm_game_safe_insets(root.view.bounds, root.view.safeAreaInsets, duo);
    DmLayout layout = dm_layout(root.view.bounds, insets, partiallyOpen, division, DM_PICTURE_16_9);
    XCTAssertTrue(CGRectEqualToRect(game.frame, layout.game),
                  @"The production game view must use the current scene bounds and native division region: actual %@, expected %@",
                  NSStringFromCGRect(game.frame), NSStringFromCGRect(layout.game));
    XCTAssertTrue(CGRectEqualToRect(pad.frame, root.view.bounds));
    XCTAssertTrue([game.layer isKindOfClass:CAMetalLayer.class]);
    CAMetalLayer *metal = (CAMetalLayer *)game.layer;
    UIScreen *screen = root.view.window.windowScene.screen;
    XCTAssertNotNil(screen);
    XCTAssertEqualWithAccuracy(metal.drawableSize.width, game.bounds.size.width * screen.nativeScale, 1.0);
    XCTAssertEqualWithAccuracy(metal.drawableSize.height, game.bounds.size.height * screen.nativeScale, 1.0);
}

- (void)testProductionRootRelayoutsMetalAndInputDuringWindowResizing {
    UIViewController *root = [self harnessRootViewController];
    UIView *game = [root valueForKey:@"game"];
    UIView *pad = [root valueForKey:@"pad"];
    NSPredicate *touchControlsActive = [NSPredicate predicateWithFormat:@"padOff == NO"];
    XCTNSPredicateExpectation *ready = [[XCTNSPredicateExpectation alloc] initWithPredicate:touchControlsActive object:pad];
    XCTAssertEqual([XCTWaiter waitForExpectations:@[ready] timeout:5.0], XCTWaiterResultCompleted);
    CGRect original = root.view.frame;
    UIViewAutoresizing originalMask = root.view.autoresizingMask;
    root.view.autoresizingMask = UIViewAutoresizingNone;
    const CGSize sizes[] = {{1024, 768}, {744, 900}, {375, 650}, {640, 360}, {500, 500}, {1024, 768}};
    @try {
        for (NSUInteger i = 0; i < sizeof sizes / sizeof sizes[0]; i++) {
            root.view.frame = (CGRect){CGPointZero, sizes[i]};
            [root.view setNeedsLayout];
            [root.view layoutIfNeeded];
            XCTAssertTrue(CGSizeEqualToSize(root.view.bounds.size, sizes[i]));
            XCTAssertTrue(CGRectEqualToRect(pad.frame, root.view.bounds));
            XCTAssertTrue(CGRectContainsRect(root.view.bounds, game.frame));
            XCTAssertEqualWithAccuracy(game.bounds.size.width / game.bounds.size.height, 16.0 / 9.0, 0.001);
            CAMetalLayer *metal = (CAMetalLayer *)game.layer;
            CGFloat scale = root.view.window.windowScene.screen.nativeScale;
            XCTAssertEqualWithAccuracy(metal.drawableSize.width, game.bounds.size.width * scale, 1.0);
            XCTAssertEqualWithAccuracy(metal.drawableSize.height, game.bounds.size.height * scale, 1.0);
            [self assertButton:DM_BTN_A at:[self controlCenterNamed:@"A" inPad:pad] onPad:pad time:i + 1];
            [pad setValue:@YES forKey:@"tap"];
            DuoFakeTouch *touch = [self touchAt:CGPointMake(CGRectGetMidX(game.frame), CGRectGetMidY(game.frame)) time:i + 1];
            [self beginTouches:@[touch] onPad:pad];
            XCTAssertEqual(duo_harness_input().touch_count, 1);
            XCTAssertEqualWithAccuracy(duo_harness_input().touches[0].x, 0.5, 0.001);
            XCTAssertEqualWithAccuracy(duo_harness_input().touches[0].y, 0.5, 0.001);
            [self endTouches:@[touch] onPad:pad cancelled:NO];
            [pad setValue:@NO forKey:@"tap"];
        }
    } @finally {
        root.view.frame = original;
        root.view.autoresizingMask = originalMask;
        [root.view setNeedsLayout];
        [root.view layoutIfNeeded];
    }
}

- (void)testResizingReleasesHeldInputAndUpdatesVideoTouchCoordinates {
    const CGRect windows[] = {CGRectMake(0, 0, 1024, 768), CGRectMake(0, 0, 320, 480),
                              CGRectMake(0, 0, 640, 360), CGRectMake(0, 0, 500, 500)};
    UIView *pad = [self padForLayout:dm_layout(windows[0], UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9) bounds:windows[0]];
    for (NSUInteger i = 0; i < sizeof windows / sizeof windows[0]; i++) {
        CGRect bounds = windows[i];
        DmLayout layout = dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
        pad.frame = bounds;
        dm_gamepad_arrange(pad, layout);
        [pad layoutIfNeeded];
        XCTAssertEqual(duo_harness_input().buttons, 0);
        XCTAssertEqual(duo_harness_input().touch_count, 0);
        XCTAssertEqualWithAccuracy(layout.game.size.width / layout.game.size.height, 16.0 / 9.0, 0.001);
        [self assertButton:DM_BTN_A at:[self controlCenterNamed:@"A" inPad:pad] onPad:pad time:i + 1];
        // Tap mode isolates the game's touch viewport from the 3D right-stick gesture area.
        [pad setValue:@YES forKey:@"tap"];
        DuoFakeTouch *touch = [self touchAt:CGPointMake(CGRectGetMidX(layout.game), CGRectGetMidY(layout.game)) time:i + 1];
        [self beginTouches:@[touch] onPad:pad];
        DuoHarnessInput input = duo_harness_input();
        XCTAssertEqual(input.touch_count, 1);
        XCTAssertEqualWithAccuracy(input.touches[0].x, 0.5, 0.001);
        XCTAssertEqualWithAccuracy(input.touches[0].y, 0.5, 0.001);
        [pad setValue:@NO forKey:@"tap"];
    }
    dm_gamepad_arrange(pad, dm_layout(windows[0], UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9));
    XCTAssertEqual(duo_harness_input().touch_count, 0);
}

- (void)testDuoPortraitKeepsOnlyTopSafeArea {
    CGRect portrait = CGRectMake(0, 0, 744, 1133);
    UIEdgeInsets safe = UIEdgeInsetsMake(54, 20, 34, 24);
    UIEdgeInsets duo = dm_game_safe_insets(portrait, safe, YES);
    XCTAssertTrue(UIEdgeInsetsEqualToEdgeInsets(duo, UIEdgeInsetsMake(54, 0, 0, 0)));
    XCTAssertTrue(UIEdgeInsetsEqualToEdgeInsets(dm_game_safe_insets(portrait, safe, NO), safe));
    XCTAssertTrue(UIEdgeInsetsEqualToEdgeInsets(dm_game_safe_insets(CGRectMake(0, 0, 1133, 744), safe, YES), safe));
    DmLayout layout = dm_layout(portrait, duo, NO, CGRectNull, DM_PICTURE_16_9);
    XCTAssertEqual(CGRectGetMinX(layout.controls), 0);
    XCTAssertEqual(CGRectGetMaxX(layout.controls), 744);
    XCTAssertEqual(CGRectGetMaxY(layout.controls), 1133);
    XCTAssertGreaterThanOrEqual(CGRectGetMinY(layout.game), 54);
}



- (void)testRootOrientationMaskRemainsStableAcrossHingeStatuses {
    UIViewController *root = [self harnessRootViewController];
    XCTAssertNotNil(root);
    if (!root) return;
    UIInterfaceOrientationMask expectedMask = UIInterfaceOrientationMaskAll;
    XCTAssertEqual(root.supportedInterfaceOrientations, expectedMask);

#if DUO_HARNESS_HAS_NATIVE_POSE_API
    if (@available(iOS 27.1, *)) {
        SEL hingeCallback = @selector(hingeChanged:);
        XCTAssertTrue([root respondsToSelector:hingeCallback]);
        if (![root respondsToSelector:hingeCallback]) return;
        Class hingeClass = NSClassFromString(@"UIHingeInteraction");
        BOOL installed = NO;
        for (id interaction in root.view.interactions) if ([interaction isKindOfClass:hingeClass]) installed = YES;
        XCTAssertTrue(installed, @"The production root must observe native hinge state for layout only");

        [(id)root hingeChanged:nil];
        XCTAssertEqual(root.supportedInterfaceOrientations, expectedMask);
        XCTAssertEqualObjects([root valueForKey:@"partiallyOpen"], @NO);

        DuoFakeHinge *hinge = [DuoFakeHinge new];
        const NSInteger statuses[] = {UIHingeStatusClosed, UIHingeStatusUnknown,
                                      UIHingeStatusPartiallyOpen, UIHingeStatusFullyOpen};
        for (NSUInteger i = 0; i < sizeof statuses / sizeof statuses[0]; i++) {
            hinge.status = statuses[i];
            [(id)root hingeChanged:hinge];
            XCTAssertEqual(root.supportedInterfaceOrientations, expectedMask,
                           @"Hinge status %ld must not change the device's supported orientations", (long)statuses[i]);
            XCTAssertEqualObjects([root valueForKey:@"partiallyOpen"],
                                  @(statuses[i] == UIHingeStatusPartiallyOpen));
        }
    }
#endif

    NSData *plist = [NSData dataWithContentsOfURL:[NSBundle.mainBundle URLForResource:@"Info" withExtension:@"plist"]];
    NSDictionary *info = [NSPropertyListSerialization propertyListWithData:plist options:0 format:nil error:nil];
    NSSet *landscape = [NSSet setWithArray:@[@"UIInterfaceOrientationLandscapeLeft", @"UIInterfaceOrientationLandscapeRight"]];
    // The harness's own plist carries what the app's does, so a test that reads it reads the app's contract (test_orientation_policy.py holds both to it)
    XCTAssertEqualObjects([NSSet setWithArray:info[@"UISupportedInterfaceOrientations"]], [landscape setByAddingObject:@"UIInterfaceOrientationPortrait"]);
    NSSet *all = [landscape setByAddingObjectsFromSet:[NSSet setWithArray:@[@"UIInterfaceOrientationPortrait", @"UIInterfaceOrientationPortraitUpsideDown"]]];
    XCTAssertEqualObjects([NSSet setWithArray:info[@"UISupportedInterfaceOrientations~ipad"]], all);
    XCTAssertNil(info[@"UIRequiresFullScreen"]);

}

- (void)testInactiveNativeDivisionCanDriveSeparatedLayout {
#if DUO_HARNESS_HAS_NATIVE_POSE_API
    if (@available(iOS 27.1, *)) {
        UIViewController *root = [self harnessRootViewController];
        XCTAssertNotNil(root);
        SEL hingeCallback = @selector(hingeChanged:);
        XCTAssertTrue([root respondsToSelector:hingeCallback]);
        if (![root respondsToSelector:hingeCallback]) return;

        UIView *view = root.view;
        Class originalClass = DuoHarnessInstallReservedRegionProbe(view);
        XCTAssertNotNil(originalClass);
        if (!originalClass) return;
        CGRect bounds = view.bounds;
        DuoFakeReservedRegion *region = [DuoFakeReservedRegion new];
        region.frame = CGRectMake(CGRectGetMinX(bounds), CGRectGetMidY(bounds) - 12, CGRectGetWidth(bounds), 24);
        region.identifier = [NSObject new];
        region.kind = UIViewReservedRegionKind.divisionRegionKind;
        region.active = NO;
        gDuoHarnessReservedRegionView = view;
        gDuoHarnessReservedRegion = region;
        gDuoHarnessSawIncludeInactiveDivisionQuery = NO;
        @try {
            DuoFakeHinge *hinge = [DuoFakeHinge new];
            hinge.status = UIHingeStatusPartiallyOpen;
            hinge.angle = 2.25;
            [(id)root hingeChanged:hinge];
            [view setNeedsUpdateConstraints]; [view updateConstraintsIfNeeded];
            [root viewDidLayoutSubviews];
            [view setNeedsLayout]; [view layoutIfNeeded];

            DmLayout expected = dm_layout(view.bounds, dm_game_safe_insets(view.bounds, view.safeAreaInsets, YES), YES, region.frame, DM_PICTURE_16_9);
            UIView *game = [root valueForKey:@"game"];
            XCTAssertTrue(gDuoHarnessSawIncludeInactiveDivisionQuery,
                          @"The root must query UIKit's IncludeInactive option for the division boundary");
            XCTAssertFalse(region.isActive, @"This boundary case must use an inactive native division");
            XCTAssertTrue(expected.separated);
            XCTAssertTrue(CGRectEqualToRect(game.frame, expected.game),
                          @"An inactive but valid horizontal division should still separate the panels");

            hinge.status = UIHingeStatusFullyOpen;
            [(id)root hingeChanged:hinge];
            [root viewDidLayoutSubviews];
            DmLayout fullyOpen = dm_layout(view.bounds, dm_game_safe_insets(view.bounds, view.safeAreaInsets, YES), NO, region.frame, DM_PICTURE_16_9);
            XCTAssertEqual(fullyOpen.separated, view.bounds.size.height > view.bounds.size.width,
                           @"A fully-open landscape display overlays while portrait remains separated");
            XCTAssertTrue(CGRectEqualToRect(game.frame, fullyOpen.game),
                          @"Fully-open layout follows the stable portrait-split, landscape-overlay policy");
        } @finally {
            object_setClass(view, originalClass);
            gDuoHarnessReservedRegionView = nil;
            gDuoHarnessReservedRegion = nil;
            gDuoHarnessSawIncludeInactiveDivisionQuery = NO;
            [(id)root hingeChanged:nil];
        }
    } else {
        XCTSkip(@"Reserved division regions are available from iOS 27.1");
    }
#else
    XCTSkip(@"This SDK does not contain UIView reserved-region queries");
#endif
}



- (UIViewController *)harnessRootViewController {
    for (UIScene *scene in UIApplication.sharedApplication.connectedScenes) {
        if (![scene isKindOfClass:UIWindowScene.class]) continue;
        for (UIWindow *window in ((UIWindowScene *)scene).windows) if (window.isKeyWindow) return window.rootViewController;
    }
    return nil;
}

- (DmLayout)separatedLayout:(CGRect)bounds {
    return dm_layout(bounds, UIEdgeInsetsZero, YES, CGRectMake(0, bounds.size.height * 0.48, bounds.size.width, 24), DM_PICTURE_16_9);
}

- (UIView *)padForLayout:(DmLayout)layout bounds:(CGRect)bounds {
    UIView *pad = dm_gamepad_view(bounds);
    dm_gamepad_arrange(pad, layout);
    [pad setNeedsLayout];
    [pad layoutIfNeeded];
    return pad;
}

// The unit of a standing phone's controls: the gap between the two rows, as the pad itself takes it (DmLayout.h). The test and the pad never disagree.
- (CGFloat)portraitUnitForLayout:(DmLayout)layout { return dm_pad_portrait_unit(layout.controls, YES); }

- (DmLayout)portraitControlLayoutForBounds:(CGRect)bounds insets:(UIEdgeInsets)insets division:(CGRect)division {
    DmLayout layout = dm_layout(bounds, insets, NO, division, DM_PICTURE_16_9);
    CGRect safe = UIEdgeInsetsInsetRect(bounds, insets);
    CGFloat controlsTop = CGRectGetMinY(layout.controls);
    layout.controls = CGRectMake(CGRectGetMinX(safe), controlsTop, safe.size.width,
                                 MAX(0, CGRectGetMaxY(safe) - controlsTop));
    return layout;
}

// The right stick's ring (DmGamepad.m's _rring), which the pad names for this check.
- (CALayer *)rightStickRingLayerInPad:(UIView *)pad { return [self layerNamed:@"rightStickRing" inLayer:pad.layer]; }

- (CALayer *)layerNamed:(NSString *)name inLayer:(CALayer *)layer {
    if ([layer.name isEqualToString:name]) return layer;
    for (CALayer *child in layer.sublayers) { CALayer *found = [self layerNamed:name inLayer:child]; if (found) return found; }
    return nil;
}

- (void)collectShapePathBoundsInLayer:(CALayer *)layer result:(NSMutableArray<NSValue *> *)result {
    if ([layer isKindOfClass:CAShapeLayer.class]) {
        CGPathRef path = ((CAShapeLayer *)layer).path;
        if (path) [result addObject:[NSValue valueWithCGRect:CGPathGetBoundingBox(path)]];
    }
    for (CALayer *child in layer.sublayers) [self collectShapePathBoundsInLayer:child result:result];
}

// The circle drawn at a point of a standing phone's pad: the right stick's ring in the lower row's right column, or the left stick's in the upper row's
// left column. Both are the band's largest circles; the D-pad's arms are not circles.
- (CGRect)stickRingAt:(CGPoint)centre inPad:(UIView *)pad {
    NSMutableArray<NSValue *> *bounds = [NSMutableArray array];
    [self collectShapePathBoundsInLayer:pad.layer result:bounds];
    CGRect best = CGRectNull;
    for (NSValue *value in bounds) {
        CGRect candidate = value.CGRectValue;
        if (CGRectIsNull(candidate)) continue;
        if (fabs(CGRectGetMidX(candidate) - centre.x) > 1 || fabs(CGRectGetMidY(candidate) - centre.y) > 1) continue;
        if (CGRectIsNull(best) || candidate.size.width > best.size.width) best = candidate;
    }
    return best;
}

- (CGRect)largestShapePathBoundsInLayer:(CALayer *)layer {
    NSMutableArray<NSValue *> *bounds = [NSMutableArray array];
    [self collectShapePathBoundsInLayer:layer result:bounds];
    CGRect largest = CGRectNull;
    CGFloat largestArea = 0;
    for (NSValue *value in bounds) {
        CGRect candidate = value.CGRectValue;
        CGFloat area = candidate.size.width * candidate.size.height;
        if (area > largestArea) { largest = candidate; largestArea = area; }
    }
    return largest;
}

- (CGPoint)faceDiamondCenterInPad:(UIView *)pad {
    CGPoint sum = CGPointZero;
    for (NSString *name in @[@"A", @"B", @"X", @"Y"]) {
        CGPoint center = [self controlCenterNamed:name inPad:pad];
        sum.x += center.x; sum.y += center.y;
    }
    return CGPointMake(sum.x / 4, sum.y / 4);
}

- (DuoFakeTouch *)touchAt:(CGPoint)point time:(NSTimeInterval)time {
    DuoFakeTouch *touch = [DuoFakeTouch new];
    touch.point = point; touch.timestamp = time;
    return touch;
}

- (void)beginTouches:(NSArray<DuoFakeTouch *> *)touches onPad:(UIView *)pad {
    [pad touchesBegan:(NSSet<UITouch *> *)[NSSet setWithArray:touches] withEvent:nil];
}

- (void)moveTouch:(DuoFakeTouch *)touch to:(CGPoint)point onPad:(UIView *)pad {
    touch.point = point;
    [pad touchesMoved:(NSSet<UITouch *> *)[NSSet setWithObject:touch] withEvent:nil];
}

- (void)endTouches:(NSArray<DuoFakeTouch *> *)touches onPad:(UIView *)pad cancelled:(BOOL)cancelled {
    NSSet<UITouch *> *set = (NSSet<UITouch *> *)[NSSet setWithArray:touches];
    if (cancelled) [pad touchesCancelled:set withEvent:nil];
    else [pad touchesEnded:set withEvent:nil];
}

// The A key where the pad itself draws it (the pad's own safe area and layout), so the test touches the real key.
- (CGPoint)faceAIn:(DmLayout)layout bounds:(CGRect)bounds {
    CGRect safe = CGRectIntersection(layout.controls, UIEdgeInsetsInsetRect(bounds, UIEdgeInsetsZero));
    if (layout.separated && bounds.size.height > bounds.size.width) {
        CGFloat u = dm_pad_portrait_unit(safe, YES);
        CGPoint faceCenter = dm_pad_portrait_column(safe, YES);
        faceCenter.y = dm_pad_portrait_row(safe, NO, YES).y;
        return CGPointMake(faceCenter.x + dm_pad_portrait_sizes(YES).faceGap * u, faceCenter.y);
    }
    CGFloat u = layout.separated ? MIN(MIN(safe.size.height / 430.0, safe.size.width / 700.0), 1.3) : MIN(bounds.size.height / 430.0, 1.3);
#if DM_GAME_PORTRAIT_PAD
    // The profile's later landscape layout: the diamond's A key on the stick's line, the shoulders' margin from the right edge.
    return CGPointMake(CGRectGetMaxX(safe) - (DM_PAD_SPACED_EDGE_X_U + 50) * u, CGRectGetMaxY(safe) - (DM_PAD_SPACED_EDGE_Y_U + 77) * u);
#else
    return CGPointMake(CGRectGetMaxX(safe) - 81 * u + 50 * u, CGRectGetMaxY(safe) - 82 * u);
#endif
}

- (CATextLayer *)textLayerNamed:(NSString *)name inLayer:(CALayer *)layer {
    if ([layer isKindOfClass:CATextLayer.class] && [(id)((CATextLayer *)layer).string isEqual:name]) return (CATextLayer *)layer;
    for (CALayer *child in layer.sublayers) {
        CATextLayer *found = [self textLayerNamed:name inLayer:child];
        if (found) return found;
    }
    return nil;
}

- (UIButton *)menuButtonInPad:(UIView *)pad {
    for (UIView *v in pad.subviews) if ([v isKindOfClass:UIButton.class] && [v.accessibilityLabel isEqual:@"Menu"]) return (UIButton *)v;
    XCTFail(@"Expected the gamepad's menu button");
    return nil;
}

- (CGPoint)controlCenterNamed:(NSString *)name inPad:(UIView *)pad {
    CATextLayer *label = [self textLayerNamed:name inLayer:pad.layer];
    XCTAssertNotNil(label, @"Expected a visible control label named %@", name);
    return CGPointMake(CGRectGetMidX(label.frame), CGRectGetMidY(label.frame));
}

- (CGRect)controlPathBoundsNamed:(NSString *)name inPad:(UIView *)pad {
    CATextLayer *label = [self textLayerNamed:name inLayer:pad.layer];
    XCTAssertNotNil(label, @"Expected a visible control label named %@", name);
    CAShapeLayer *shape = (CAShapeLayer *)label.superlayer;
    XCTAssertTrue(shape.path != NULL, @"Expected the control to have a rendered path");
    return CGPathGetBoundingBox(shape.path);
}

- (BOOL)controlNamed:(NSString *)name containsPoint:(CGPoint)point inPad:(UIView *)pad {
    CATextLayer *label = [self textLayerNamed:name inLayer:pad.layer];
    XCTAssertNotNil(label, @"Expected a visible control label named %@", name);
    CAShapeLayer *shape = (CAShapeLayer *)label.superlayer;
    XCTAssertTrue(shape.path != NULL, @"Expected the control to have a rendered path");
    return CGPathContainsPoint(shape.path, NULL, point, false);
}

- (void)assertButton:(uint64_t)button at:(CGPoint)point onPad:(UIView *)pad time:(NSTimeInterval)time {
    DuoFakeTouch *touch = [self touchAt:point time:time];
    [self beginTouches:@[touch] onPad:pad];
    DuoHarnessInput input = duo_harness_input();
    XCTAssertEqual(input.buttons, button, @"The enlarged hit area should select exactly the nearest button");
    [self endTouches:@[touch] onPad:pad cancelled:NO];
}

- (void)testSimultaneousFaceButtonsAndVideoTouchKeepIndependentSlots {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    DmLayout layout = [self separatedLayout:bounds];
    UIView *pad = [self padForLayout:layout bounds:bounds];
    CGPoint a = [self faceAIn:layout bounds:bounds];
    CGFloat u = dm_pad_portrait_unit(layout.controls, YES);
    CGFloat fg = dm_pad_portrait_sizes(YES).faceGap * u;
    CGPoint b = CGPointMake(a.x - fg, a.y + fg);
    CGPoint video = CGPointMake(CGRectGetMidX(layout.game), CGRectGetMidY(layout.game));
    DuoFakeTouch *ta = [self touchAt:a time:1], *tb = [self touchAt:b time:1], *tv = [self touchAt:video time:1];

    [self beginTouches:@[ta, tb, tv] onPad:pad];
    DuoHarnessInput input = duo_harness_input();
    XCTAssertEqual(input.buttons & (DM_BTN_A | DM_BTN_B), DM_BTN_A | DM_BTN_B);
    XCTAssertEqual(input.touch_count, 1);
    XCTAssertEqualWithAccuracy(input.touches[0].x, 0.5, 0.001);
    XCTAssertEqualWithAccuracy(input.touches[0].y, 0.5, 0.001);

    [self endTouches:@[ta] onPad:pad cancelled:NO];
    input = duo_harness_input();
    XCTAssertEqual(input.buttons & DM_BTN_A, 0);
    XCTAssertNotEqual(input.buttons & DM_BTN_B, 0);
    XCTAssertEqual(input.touch_count, 1);

    [self endTouches:@[tb, tv] onPad:pad cancelled:YES];
    input = duo_harness_input();
    XCTAssertEqual(input.buttons, 0);
    XCTAssertEqual(input.touch_count, 0);
}

- (void)testVideoCoordinatesClampAtCornersAndIgnoreFoldAndControlArea {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    DmLayout layout = [self separatedLayout:bounds];
    UIView *pad = [self padForLayout:layout bounds:bounds];
    DuoFakeTouch *touch = [self touchAt:CGPointMake(CGRectGetMinX(layout.game), CGRectGetMinY(layout.game)) time:1];
    [self beginTouches:@[touch] onPad:pad];
    DuoHarnessInput input = duo_harness_input();
    XCTAssertEqual(input.touch_count, 1);
    XCTAssertEqualWithAccuracy(input.touches[0].x, 0, 0.001);
    XCTAssertEqualWithAccuracy(input.touches[0].y, 0, 0.001);

    [self moveTouch:touch to:CGPointMake(CGRectGetMidX(layout.game), CGRectGetMidY(layout.game)) onPad:pad];
    input = duo_harness_input();
    XCTAssertEqual(input.touches[0].id, 0);
    XCTAssertEqualWithAccuracy(input.touches[0].x, 0.5, 0.001);
    XCTAssertEqualWithAccuracy(input.touches[0].y, 0.5, 0.001);

    [self moveTouch:touch to:CGPointMake(CGRectGetMaxX(layout.game) - 0.001, CGRectGetMaxY(layout.game) - 0.001) onPad:pad];
    input = duo_harness_input();
    XCTAssertEqualWithAccuracy(input.touches[0].x, 0.999, 0.001);
    XCTAssertEqualWithAccuracy(input.touches[0].y, 0.999, 0.001);

    [self endTouches:@[touch] onPad:pad cancelled:NO];
    DuoFakeTouch *foldGap = [self touchAt:CGPointMake(CGRectGetMidX(bounds), CGRectGetMinY(layout.controls) - 10) time:2];
    [self beginTouches:@[foldGap] onPad:pad];
    input = duo_harness_input();
    XCTAssertEqual(input.touch_count, 0);
    XCTAssertEqual(input.buttons, 0);
    [self endTouches:@[foldGap] onPad:pad cancelled:NO];

    CGRect lower = layout.controls;
    // The lower-panel midpoint can now fall on the portrait D-pad. Use the bottom-centre
    // safe-area strip, clear of the anchored thumb groups, to verify controls do not claim it.
    DuoFakeTouch *outside = [self touchAt:CGPointMake(CGRectGetMidX(lower), CGRectGetMaxY(lower) - 10) time:2];
    [self beginTouches:@[outside] onPad:pad];
    input = duo_harness_input();
    XCTAssertEqual(input.touch_count, 0);
    XCTAssertEqual(input.buttons, 0);
    [self endTouches:@[outside] onPad:pad cancelled:NO];

    DuoFakeTouch *rightThirdVideo = [self touchAt:CGPointMake(CGRectGetMaxX(layout.game) - 1, CGRectGetMidY(layout.game)) time:3];
    [self beginTouches:@[rightThirdVideo] onPad:pad];
    input = duo_harness_input();
    XCTAssertEqual(input.touch_count, 1);
    XCTAssertEqual(input.rx, 0);
    XCTAssertEqual(input.ry, 0);
    XCTAssertEqualWithAccuracy(input.touches[0].x, 0.997, 0.002);
    XCTAssertEqualWithAccuracy(input.touches[0].y, 0.5, 0.001);
    [self endTouches:@[rightThirdVideo] onPad:pad cancelled:NO];
}

- (void)testNoOpArrangePreservesHeldInputs {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    DmLayout separated = [self separatedLayout:bounds];
    UIView *pad = [self padForLayout:separated bounds:bounds];
    DuoFakeTouch *video = [self touchAt:CGPointMake(CGRectGetMidX(separated.game), CGRectGetMidY(separated.game)) time:1];
    DuoFakeTouch *button = [self touchAt:[self faceAIn:separated bounds:bounds] time:1];
    [self beginTouches:@[video, button] onPad:pad];

    dm_gamepad_arrange(pad, separated);
    DuoHarnessInput input = duo_harness_input();
    XCTAssertNotEqual(input.buttons & DM_BTN_A, 0);
    XCTAssertEqual(input.touch_count, 1);
    XCTAssertEqualWithAccuracy(input.touches[0].x, 0.5, 0.001);
    XCTAssertEqualWithAccuracy(input.touches[0].y, 0.5, 0.001);
    [self endTouches:@[video, button] onPad:pad cancelled:NO];
}

- (void)testPoseChangeReleasesHeldButtonAndGameTouch {
    CGRect bounds = CGRectMake(0, 0, 390, 844);
    DmLayout separated = [self separatedLayout:bounds];
    UIView *pad = [self padForLayout:separated bounds:bounds];
    DuoFakeTouch *video = [self touchAt:CGPointMake(CGRectGetMidX(separated.game), CGRectGetMidY(separated.game)) time:1];
    DuoFakeTouch *button = [self touchAt:[self faceAIn:separated bounds:bounds] time:1];
    [self beginTouches:@[video, button] onPad:pad];
    XCTAssertNotEqual(duo_harness_input().buttons & DM_BTN_A, 0);
    XCTAssertEqual(duo_harness_input().touch_count, 1);

    dm_gamepad_arrange(pad, dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9));
    DuoHarnessInput input = duo_harness_input();
    XCTAssertEqual(input.buttons, 0);
    XCTAssertEqual(input.touch_count, 0);
    [self endTouches:@[video, button] onPad:pad cancelled:NO];
}


- (void)testConnectedVirtualControllerKeepsVideoTouchAndMapsPhysicalButtons {
    if (@available(iOS 17.0, *)) {
        [self setHarnessControllers:nil notification:nil];
        CGRect bounds = CGRectMake(0, 0, 844, 390);
        DmLayout layout = dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9);
        UIView *pad = [self padForLayout:layout bounds:bounds];
        GCVirtualControllerConfiguration *config = [GCVirtualControllerConfiguration new];
        config.hidden = YES;
        config.elements = [NSSet setWithArray:@[GCInputButtonA, GCInputButtonB, GCInputButtonX, GCInputButtonY,
                                                 GCInputLeftShoulder, GCInputRightShoulder, GCInputButtonMenu,
                                                 GCInputLeftThumbstick, GCInputRightThumbstick, GCInputDirectionPad]];
        GCVirtualController *controller = [[GCVirtualController alloc] initWithConfiguration:config];
        XCTestExpectation *connected = [self expectationWithDescription:@"virtual controller connected"];
        __block NSError *connectError;
        [controller connectWithReplyHandler:^(NSError *error) { connectError = error; [connected fulfill]; }];
        [self waitForExpectations:@[connected] timeout:5];
        if (connectError || !controller.controller.extendedGamepad) {
            [controller disconnect];
            XCTSkip(@"Simulator virtual extended gamepad is unavailable");
        }
        [self setHarnessControllers:@[controller.controller] notification:GCControllerDidConnectNotification];

        [controller setValue:1 forButtonElement:GCInputButtonB];
        NSPredicate *hasA = [NSPredicate predicateWithBlock:^BOOL(id object, NSDictionary *bindings) {
            (void)object; (void)bindings; return (duo_harness_state().players[0].buttons & DM_BTN_A) != 0;
        }];
        [self expectationForPredicate:hasA evaluatedWithObject:self handler:nil];
        [self waitForExpectationsWithTimeout:5 handler:nil];

        CGRect portrait = CGRectMake(0, 0, 390, 844);
        DmLayout separated = [self separatedLayout:portrait];
        pad.frame = portrait;
        dm_gamepad_arrange(pad, separated);
        [pad setNeedsLayout]; [pad layoutIfNeeded];
        XCTAssertNotEqual(duo_harness_state().players[0].buttons & DM_BTN_A, 0,
                          @"Changing the UI pose must preserve player one's controller state");

        DuoFakeTouch *video = [self touchAt:CGPointMake(CGRectGetMidX(separated.game), CGRectGetMidY(separated.game)) time:1];
        [self beginTouches:@[video] onPad:pad];
        DuoHarnessInput input = duo_harness_input();
        XCTAssertNotEqual(duo_harness_state().players[0].buttons & DM_BTN_A, 0);
        XCTAssertEqual(input.touch_count, 1);
        XCTAssertEqualWithAccuracy(input.touches[0].x, 0.5, 0.001);
        XCTAssertEqualWithAccuracy(input.touches[0].y, 0.5, 0.001);
        [self endTouches:@[video] onPad:pad cancelled:NO];
        [controller setValue:0 forButtonElement:GCInputButtonB];
        [self setHarnessControllers:@[] notification:GCControllerDidDisconnectNotification];
        [controller disconnect];
    } else {
        XCTSkip(@"Virtual controllers require iOS 17 or later");
    }
}


- (void)testLiveJoyConMapsSidewaysAndKeepsItsOwnPlayer {
    CGRect bounds = CGRectMake(0, 0, 844, 390);
    [self padForLayout:dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9) bounds:bounds];
    DuoLiveController *joy = [[DuoLiveController alloc] initWithCategory:@"Nintendo Switch Joy-Con (R)"];
    DuoLiveController *remote = [[DuoLiveController alloc] initWithCategory:@"Siri Remote"];
    [self setHarnessControllers:@[(GCController *)joy, (GCController *)remote] notification:GCControllerDidConnectNotification];
    XCTAssertEqual(duo_harness_state().connected_players, 1u);
    XCTAssertNil(remote.input.elementValueDidChangeHandler);

    NSArray *names = @[GCInputButtonX, GCInputButtonA, GCInputButtonY, GCInputButtonB,
                       GCInputLeftShoulder, GCInputRightShoulder, GCInputButtonMenu];
    const uint64_t mapped[] = {DM_BTN_A, DM_BTN_B, DM_BTN_X, DM_BTN_Y, DM_BTN_L, DM_BTN_R, DM_BTN_PLUS};
    for (NSUInteger i = 0; i < names.count; i++) {
        [joy.input setPressed:YES button:names[i]];
        XCTAssertEqual(duo_harness_state().players[0].buttons, mapped[i]);
        [joy.input setPressed:NO button:names[i]];
        XCTAssertEqual(duo_harness_state().players[0].buttons, 0u);
    }
    [joy.input moveX:1 y:-1];
    DuoHarnessPlayerInput player = duo_harness_state().players[0];
    XCTAssertEqual(player.lx, 32767);
    XCTAssertEqual(player.ly, -32767);
    XCTAssertEqual(player.rx, 0);
    XCTAssertEqual(player.ry, 0);

    GCController *extended = [GCController controllerWithExtendedGamepad];
    [self setHarnessControllers:@[extended, (GCController *)joy] notification:GCControllerDidConnectNotification];
    uint32_t other = duo_harness_state().connected_players & ~1u;
    XCTAssertEqual(__builtin_popcount(other), 1);
    if (other) {
        int slot = __builtin_ctz(other);
        [self deliverSnapshotChangeFor:extended button:extended.extendedGamepad.buttonB value:1];
        [joy.input setPressed:YES button:GCInputButtonA];
        XCTAssertEqual(duo_harness_state().players[0].buttons, DM_BTN_B);
        XCTAssertEqual(duo_harness_state().players[slot].buttons, DM_BTN_A);

        DuoLiveHandler stale = joy.input.elementValueDidChangeHandler;
        [self setHarnessControllers:@[extended] notification:GCControllerDidDisconnectNotification];
        XCTAssertNil(joy.input.elementValueDidChangeHandler);
        if (stale) stale((id)joy.input, (id)joy.input.buttons[GCInputButtonA]);
        DuoHarnessState state = duo_harness_state();
        XCTAssertEqual(state.connected_players, other);
        XCTAssertEqual(state.players[0].buttons, 0u);
        XCTAssertEqual(state.players[0].lx, 0);
        XCTAssertEqual(state.players[slot].buttons, DM_BTN_A);
    }
    [self setHarnessControllers:@[] notification:GCControllerDidDisconnectNotification];
}

- (void)testLiveJoyConsJoinOnButtonAndKeepPromptInputOutOfGame {
    UIWindow *window = [self harnessRootViewController].view.window;
    XCTAssertTrue(window && front_metal_layer());
    if (!window || !front_metal_layer()) return;

    const char *env = getenv("DM_PAD_PRESS_TO_JOIN");
    NSString *saved = env ? @(env) : nil;
    setenv("DM_PAD_PRESS_TO_JOIN", "1", 1);
    CGRect bounds = window.bounds;
    [self padForLayout:dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9) bounds:bounds];
    if (saved) setenv("DM_PAD_PRESS_TO_JOIN", saved.UTF8String, 1);
    else unsetenv("DM_PAD_PRESS_TO_JOIN");

    DuoLiveController *left = [[DuoLiveController alloc] initWithCategory:@"Nintendo Switch Joy-Con (L)"];
    DuoLiveController *right = [[DuoLiveController alloc] initWithCategory:@"Nintendo Switch Joy-Con (R)"];
    [self setHarnessControllers:@[(GCController *)right, (GCController *)left] notification:GCControllerDidConnectNotification];
    XCTAssertEqual(duo_harness_state().connected_players, 0u);
    [left.input moveX:1 y:0];
    [left.input setPressed:YES button:GCInputButtonHome];
    XCTAssertEqual(duo_harness_state().connected_players, 0u, @"Stick and system Home must not join a player");
    [left.input setPressed:YES button:GCInputLeftShoulder];
    XCTAssertEqual(duo_harness_state().connected_players, 1u, @"First press gets player 1 even when listed second");
    XCTAssertEqual(duo_harness_state().players[0].buttons, DM_BTN_L);

    NSUInteger originalSubviews = window.subviews.count;
    XCTestExpectation *closed = [self expectationWithDescription:@"controller prompt returns"];
    __block int result = -1;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        result = front_ctrl_support(2, 2);
        [closed fulfill];
    });
    NSPredicate *shown = [NSPredicate predicateWithBlock:^BOOL(UIWindow *candidate, NSDictionary *bindings) {
        (void)bindings; return candidate.subviews.count > originalSubviews;
    }];
    XCTNSPredicateExpectation *open = [[XCTNSPredicateExpectation alloc] initWithPredicate:shown object:window];
    XCTAssertEqual([XCTWaiter waitForExpectations:@[open] timeout:5], XCTWaiterResultCompleted);
    XCTAssertEqual(duo_harness_state().players[0].buttons, 0u);
    [right.input setPressed:YES button:GCInputButtonA];   // Switch B joins the second player and completes the prompt
    [self waitForExpectations:@[closed] timeout:5];
    XCTAssertEqual(result, 1);
    XCTAssertEqual(duo_harness_state().connected_players, 3u);
    XCTAssertEqual(duo_harness_state().players[1].buttons, 0u, @"The button that closed the prompt is held back until release");
    [right.input setPressed:NO button:GCInputButtonA];
    XCTAssertEqual(duo_harness_state().players[0].buttons, DM_BTN_L, @"Both players' current input is restored on release");
    [right.input setPressed:YES button:GCInputButtonA];
    XCTAssertEqual(duo_harness_state().players[1].buttons, DM_BTN_B);
    [self setHarnessControllers:@[] notification:GCControllerDidDisconnectNotification];
}

- (void)testControllersKeepIndependentPlayersAndLayoutKeepsMotionState {
    if (@available(iOS 13.0, *)) {
        GCController *first = [GCController controllerWithExtendedGamepad];
        GCController *second = [GCController controllerWithExtendedGamepad];
        XCTAssertTrue(first.isSnapshot);
        XCTAssertTrue(second.isSnapshot);
        [self setHarnessControllers:@[first, second] notification:GCControllerDidConnectNotification];

        CGRect bounds = CGRectMake(0, 0, 844, 390);
        UIView *pad = [self padForLayout:dm_layout(bounds, UIEdgeInsetsZero, NO, CGRectNull, DM_PICTURE_16_9) bounds:bounds];
        [self deliverSnapshotChangeFor:first button:first.extendedGamepad.buttonB value:1];
        [self deliverSnapshotChangeFor:second button:second.extendedGamepad.buttonA value:1];
        DuoHarnessState state = duo_harness_state();
        XCTAssertEqual(state.connected_players, 0x3);
        XCTAssertEqual(state.players[0].buttons, DM_BTN_A);
        XCTAssertEqual(state.players[1].buttons, DM_BTN_B);
        GCExtendedGamepadValueChangedHandler staleFirstHandler = first.extendedGamepad.valueChangedHandler;

        const float acceleration[] = {0.25f, -0.5f, 0.75f};
        const float rotation[] = {-1.0f, 2.0f, -3.0f};
        dm_motion_set(1, acceleration, rotation);
        CGRect portrait = CGRectMake(0, 0, 390, 844);
        DmLayout separated = [self separatedLayout:portrait];
        pad.frame = portrait;
        dm_gamepad_arrange(pad, separated);
        [pad layoutIfNeeded];
        state = duo_harness_state();
        XCTAssertEqual(state.connected_players, 0x3);
        XCTAssertEqual(state.players[0].buttons, DM_BTN_A);
        XCTAssertEqual(state.players[1].buttons, DM_BTN_B);
        XCTAssertEqual(state.controller_motion_mask & (1u << 1), 1u << 1,
                       @"Changing the UI pose must not clear player two's motion source");
        for (NSUInteger axis = 0; axis < 3; axis++) {
            XCTAssertEqualWithAccuracy(state.controller_motion[1].acceleration[axis], acceleration[axis], 0.001);
            XCTAssertEqualWithAccuracy(state.controller_motion[1].rotation[axis], rotation[axis], 0.001);
        }

        // Reordering the connected-controller snapshot must not move either player's slot.
        [self setHarnessControllers:@[second, first] notification:GCControllerDidConnectNotification];
        [self setHarnessControllers:@[second] notification:GCControllerDidDisconnectNotification];
        state = duo_harness_state();
        XCTAssertEqual(state.connected_players, 0x2);
        XCTAssertEqual(state.players[0].buttons, 0);
        XCTAssertEqual(state.players[1].buttons, DM_BTN_B);
        XCTAssertEqual(state.controller_motion_mask & 0x3, 0x2,
                       @"Disconnecting player one must leave player two's motion source intact");
        if (staleFirstHandler) staleFirstHandler(first.extendedGamepad, first.extendedGamepad.buttonB);
        state = duo_harness_state();
        XCTAssertEqual(state.players[0].buttons, 0,
                       @"A queued callback from the disconnected controller must not reclaim its slot");
        XCTAssertEqual(state.players[1].buttons, DM_BTN_B);

        [self setHarnessControllers:@[] notification:GCControllerDidDisconnectNotification];
        state = duo_harness_state();
        XCTAssertEqual(state.connected_players, 0);
        XCTAssertEqual(state.players[1].buttons, 0);
        XCTAssertEqual(state.controller_motion_mask & (1u << 1), 0,
                       @"Disconnecting player two clears its motion source");
    }
}


@end
