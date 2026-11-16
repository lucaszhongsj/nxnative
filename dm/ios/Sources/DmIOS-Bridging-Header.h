// The C and Objective-C functions Launcher.swift and GameMenu.swift call (CloudSaves.h, DataBookmark.h, DmGameMenu.h, save_sync.h's report before Play). project.yml points SWIFT_OBJC_BRIDGING_HEADER here.
#import "CloudSaves.h"
#import "DataBookmark.h"
#import "DmGameMenu.h"
#include "save_sync.h"

// main.m: set when the game data found is an old shard archive (romfs.idx), which is no longer read. The sentence names the
// action (re-send the game data as romfs.img from the launcher), or NULL. The launcher's Game data section shows it.
const char *dm_old_archive_message(void);

// The optional portrait companion's native engine and its C layout helpers; Swift reaches them only under the DM_COMPANION compilation condition.
#import "companion_apple.h"
#include "companion_layout.h"
