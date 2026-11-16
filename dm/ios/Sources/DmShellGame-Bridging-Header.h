// What the shell's game framework sees from Swift (project-framework.yml's DmShellGame, OTHER_SWIFT_FLAGS): the in-game
// menu's calls into the host (DmGameMenu.h) and the companion's native engine, the band's model and renderer read
// (companion_apple.h, companion_layout.h). The app's own bridging header (DmIOS-Bridging-Header.h) carries the launcher's
// dependencies instead: the framework compiles neither Launcher.swift nor its cloud-save pickers.
#import "DmGameMenu.h"
#import "companion_apple.h"
#include "companion_layout.h"
