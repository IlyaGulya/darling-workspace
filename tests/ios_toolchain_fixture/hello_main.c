/*
 * Entry point of the iOS toolchain fixture.
 *
 * Stage T4 links this translation unit together with hello.c (and, once the compiler stage is
 * green, hello.m) into a minimal arm64 iOS Mach-O using the real Xcode toolchain: the Apple
 * clang driver plus the Apple arm64 ld, against a real iPhoneOS SDK. The link is the subject,
 * so the program is deliberately trivial -- but the reference is real, not decorative: a link
 * that drops the object would leave the symbol unresolved, and a link that produces a broken
 * image cannot run.
 */
extern int ios_fixture_answer(void);

int main(void) {
	return ios_fixture_answer() == 42 ? 0 : 1;
}
