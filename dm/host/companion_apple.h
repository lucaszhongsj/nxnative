#pragma once
#import <Foundation/Foundation.h>
// Main-thread UI bridge; the module ABI does not receive these capabilities.
void dm_companion_ui_activity(int source,int busy);
NS_ASSUME_NONNULL_BEGIN
@interface DMCompanion : NSObject
+ (instancetype)shared;
@property(readonly) BOOL supported;
@property(readonly) NSDictionary *snapshot;
- (void)start;
- (void)setVisible:(BOOL)visible;
- (void)action:(NSString *)name argument:(int64_t)argument;
- (void)image:(NSString *)key completion:(void (^)(NSData * _Nullable pixels,NSUInteger width,NSUInteger height))completion;
@end
NS_ASSUME_NONNULL_END
// The renderer reaches the adapter through C, as it reaches the rest of the host (DmGameMenu.h): a shell game framework's
// Swift imports no engine class, since gen_objc_namespace.py renames each one per framework. `engine` is the shared adapter,
// NULL when the build carries no module; it lives for the process, so nothing releases it.
#ifdef __cplusplus
extern "C" {
#endif
void *_Nullable dm_companion_engine(void);
void dm_companion_engine_start(void * _Nonnull engine);
NSDictionary * _Nonnull dm_companion_engine_snapshot(void * _Nonnull engine);
void dm_companion_engine_action(void * _Nonnull engine, const char * _Nonnull name, long long value);
void dm_companion_engine_image(void * _Nonnull engine, const char * _Nonnull key, void (^ _Nonnull done)(NSData * _Nullable pixels, int width, int height));
void dm_companion_engine_set_visible(void * _Nonnull engine, bool visible);
#ifdef __cplusplus
}
#endif
