/* Minimal Objective-C translation unit for the iOS toolchain regression fixture.
 * It pulls in the real UIKit SDK surface, so compiling it proves the header search
 * path, the Objective-C frontend and the SDK's module map all work. */
#import <UIKit/UIKit.h>

int ios_fixture_uikit_surface(void) {
	Class appClass = NSClassFromString(@"UIApplication");
	return appClass != (Class)0 ? 1 : 0;
}
