/*
 * uttype_family_guest.c -- permanent guest oracle for the Objective-C UTType family the Xcode
 * workload demands, measured on the DEPLOYED UniformTypeIdentifiers.framework.
 *
 * WHY THIS EXISTS. The Xcode compatibility ladder stops in the guest with
 *
 *   Uncaught Exception: +[UTType typesWithTag:tagClass:conformingToType:]: unrecognized selector
 *     sent to instance ...
 *   ... __40+[DVTLocalComputerLocator localComputer]_block_invoke_2 (DVTFoundation)
 *   ... -[DVTDeviceManager locateOnlyLocalComputer] <- _IDEInitializeDeviceManager (IDEFoundation)
 *   ... -[Xcode3CommandLineBuildTool run]
 *
 * measured on prefix-verify14 against UniformTypeIdentifiers.framework sha256
 * 14ec858722ec7605..., whose UTType class answers exactly -methodSignatureForSelector: and
 * -forwardInvocation: -- i.e. NONE of the family.  The FAMILY, not one selector, is the scope: the
 * declarations live in Darling's Swift-overlay shim
 * (src/external/swift/overlays/UniformTypeIdentifiers/shims/DarlingUniformTypeIdentifiersShims.h) and
 * in Apple's SDK header (.../MacOSX.sdk/.../UniformTypeIdentifiers.framework/Headers/UTType.h), and
 * the workload's images reference them through two routes:
 *
 *   * directly, by selector: DVTFoundation sends typesWithTag:tagClass:conformingToType: (the
 *     measured caller -- see the rung's README for its three arguments) and conformsToType:;
 *     IDEFoundation/DVTFoundation/XCTHarness send typeWithIdentifier:; XCResultKit/XCTHarness send
 *     typeWithFilenameExtension: and stringByAppendingPathExtensionForType:;
 *   * through the component's own Swift half, libswiftUniformTypeIdentifiers.dylib, whose
 *     __objc_methname section is the exact set of ObjC selectors the overlay forwards to (the
 *     overlay is what makes IDEFoundation/XCTHarness/XCResultKit's Swift UTType calls reach it).
 *
 * This fixture measures EVERY member of that family in ONE guest process, with no compile-time
 * reference to Foundation or ObjC:
 *
 *   * which class methods the class object answers (-respondsToSelector:) and which instance
 *     methods the class carries (class_getInstanceMethod);
 *   * the behaviour of the members the workload actually sends, against the DEPLOYED runtime:
 *     the workload's own call [UTType typesWithTag:<device model code> tagClass:*_UTTagClassDeviceModelCode
 *     conformingToType:*_UTTypeDevice] (both constant slots are NULL in this tree -- measured by
 *     disassembly of the caller), the Darwin-contract call with real tag/tagClass, identifier/tag
 *     lookups, conformance, and -stringByAppendingPathExtensionForType:.
 *
 * Every send is guarded by the presence checks, so a RED arm names the missing member and exits
 * cleanly rather than dying in the runtime's uncaught-exception path.  UTTYPE_FAMILY_VERDICT=GREEN
 * is the machine-readable line the harness ok-marker reads.
 *
 * Semantics are Apple's, from the SDK header quoted in the comment above each check (an authority
 * present in this tree), never invented here.
 */

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define OBJC_LIBRARY "/usr/lib/libobjc.A.dylib"
#define FOUNDATION_LIBRARY "/System/Library/Frameworks/Foundation.framework/Versions/C/Foundation"
#define CF_LIBRARY "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
#define UT_LIBRARY "/System/Library/Frameworks/UniformTypeIdentifiers.framework/Versions/A/UniformTypeIdentifiers"

static void *(*msgSend)(void *, void *, ...);
static void *(*selRegister)(const char *);
static void *(*getClassFn)(const char *);
static const char *(*classNameFn)(void *);
static void *(*getInstanceMethodFn)(void *, void *);

static int failures;

static void *lookup(void *handle, const char *name)
{
	void *found = dlsym(handle, name);
	if (!found) {
		char underscored[160];
		snprintf(underscored, sizeof underscored, "_%s", name);
		found = dlsym(handle, underscored);
	}
	if (!found && name[0] == '_')
		found = dlsym(handle, name + 1);
	if (!found)
		printf("UTTYPE-NOTE symbol=%s error=%s\n", name, dlerror());
	return found;
}

static void *send(void *object, const char *selector)
{
	return msgSend(object, selRegister(selector));
}

static void *send1(void *object, const char *selector, void *argument)
{
	return msgSend(object, selRegister(selector), argument);
}

static void *send2(void *object, const char *selector, void *a1, void *a2)
{
	return msgSend(object, selRegister(selector), a1, a2);
}

static void *send3(void *object, const char *selector, void *a1, void *a2, void *a3)
{
	return msgSend(object, selRegister(selector), a1, a2, a3);
}

static int responds(void *object, const char *selector)
{
	return (int)(long)msgSend(object, selRegister("respondsToSelector:"), selRegister(selector));
}

static int has_instance_method(void *cls, const char *selector)
{
	return getInstanceMethodFn(cls, selRegister(selector)) != NULL;
}

static void *string_from_c(void *string_class, const char *c)
{
	return send1(string_class, "stringWithUTF8String:", (void *)c);
}

static const char *c_from_string(void *string)
{
	return string ? (const char *)send(string, "UTF8String") : NULL;
}

/* The value of an exported UTType constant: dlsym gives the ADDRESS of the data symbol, so the
 * object is what that address holds.  A NULL slot is reported, not dereferenced. */
static void *constant_value(void *handle, const char *name)
{
	void **slot = (void **)lookup(handle, name);
	return slot ? *slot : NULL;
}

static int check_int(const char *label, long got, long expected)
{
	int ok = got == expected;

	printf("UTTYPE-CHECK %s got=%ld expected=%ld ok=%d\n", label, got, expected, ok);
	if (!ok)
		failures++;
	return ok;
}

static int check_string(const char *label, const char *got, const char *expected)
{
	int ok = got != NULL && expected != NULL && strcmp(got, expected) == 0;

	printf("UTTYPE-CHECK %s got=\"%s\" expected=\"%s\" ok=%d\n",
	    label, got ? got : "(null)", expected ? expected : "(null)", ok);
	if (!ok)
		failures++;
	return ok;
}

/* family selector -> printed presence line, so the RED arm names every missing member */
static const char *const class_selectors[] = {
	"typeWithIdentifier:",
	"typeWithFilenameExtension:",
	"typeWithFilenameExtension:conformingToType:",
	"typeWithMIMEType:",
	"typeWithMIMEType:conformingToType:",
	"typeWithTag:tagClass:conformingToType:",
	"typesWithTag:tagClass:conformingToType:",
	"exportedTypeWithIdentifier:",
	"exportedTypeWithIdentifier:conformingToType:",
	"importedTypeWithIdentifier:",
	"importedTypeWithIdentifier:conformingToType:",
	NULL
};

static const char *const instance_selectors[] = {
	"identifier",
	"preferredFilenameExtension",
	"preferredMIMEType",
	"localizedDescription",
	"version",
	"referenceURL",
	"isDynamic",
	"isDeclared",
	"isPublicType",
	"tags",
	"supertypes",
	"conformsToType:",
	"isSupertypeOfType:",
	"isSubtypeOfType:",
	NULL
};

int main(void)
{
	void *objc;
	void *foundation;
	void *cf;
	void *ut;
	void *string_class;
	void *uttype_class;
	void *uttype_plain_text;
	void *uttype_image;
	void *uttype_directory;
	int i;
	int missing = 0;
	int answered = 0;

	setvbuf(stdout, NULL, _IOLBF, 0);

	objc = dlopen(OBJC_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	foundation = dlopen(FOUNDATION_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	cf = dlopen(CF_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	ut = dlopen(UT_LIBRARY, RTLD_NOW | RTLD_GLOBAL);
	if (!objc || !foundation || !cf || !ut) {
		printf("UTTYPE-FAIL stage=dlopen objc=%p foundation=%p cf=%p ut=%p err=%s\n",
		    objc, foundation, cf, ut, dlerror());
		printf("UTTYPE_FAMILY_VERDICT=RED\n");
		return 1;
	}

	msgSend = (void *(*)(void *, void *, ...))lookup(objc, "objc_msgSend");
	selRegister = (void *(*)(const char *))lookup(objc, "sel_registerName");
	getClassFn = (void *(*)(const char *))lookup(objc, "objc_getClass");
	classNameFn = (const char *(*)(void *))lookup(objc, "class_getName");
	getInstanceMethodFn = (void *(*)(void *, void *))lookup(objc, "class_getInstanceMethod");
	if (!msgSend || !selRegister || !getClassFn || !classNameFn || !getInstanceMethodFn) {
		printf("UTTYPE-FAIL stage=runtime-symbols\n");
		printf("UTTYPE_FAMILY_VERDICT=RED\n");
		return 1;
	}

	string_class = lookup(foundation, "_OBJC_CLASS_$_NSString");
	uttype_class = getClassFn("UTType");
	if (!uttype_class)
		uttype_class = lookup(ut, "_OBJC_CLASS_$_UTType");
	if (!string_class || !uttype_class) {
		printf("UTTYPE-FAIL stage=classes NSString=%p UTType=%p\n", string_class, uttype_class);
		printf("UTTYPE_FAMILY_VERDICT=RED\n");
		return 1;
	}
	printf("UTTYPE-RECEIVER class=%s\n", classNameFn(uttype_class));

	/* ---- presence: the whole family, class and instance ---- */
	for (i = 0; class_selectors[i]; i++) {
		int r = responds(uttype_class, class_selectors[i]);
		printf("UTTYPE-FAMILY class +%s responds=%d\n", class_selectors[i], r);
		if (r)
			answered++;
		else
			missing++;
	}
	for (i = 0; instance_selectors[i]; i++) {
		int r = has_instance_method(uttype_class, instance_selectors[i]);
		printf("UTTYPE-FAMILY instance -%s present=%d\n", instance_selectors[i], r);
		if (r)
			answered++;
		else
			missing++;
	}
	/* -stringByAppendingPathExtensionForType: is declared by UTAdditions.h on NSString, not on
	 * UTType (Apple's header puts it in @interface NSString (UTAdditions)), so it is asked of the
	 * string class. */
	{
		int r = has_instance_method(string_class, "stringByAppendingPathExtensionForType:");
		printf("UTTYPE-FAMILY NSString -stringByAppendingPathExtensionForType: present=%d\n", r);
		if (r)
			answered++;
		else
			missing++;
	}
	printf("UTTYPE-PRESENCE answered=%d missing=%d\n", answered, missing);

	if (missing) {
		printf("UTTYPE-FAIL stage=presence missing=%d\n", missing);
		printf("UTTYPE_FAMILY_VERDICT=RED\n");
		return 1;
	}

	/* ---- the workload's own call ----
	 * DVTFoundation's +[DVTLocalComputerLocator localComputer] block sends
	 *   [UTType typesWithTag:<device model code> tagClass:*_UTTagClassDeviceModelCode
	 *            conformingToType:*_UTTypeDevice]
	 * (measured from its disassembly: rdx = the tag, rcx = the loaded _UTTagClassDeviceModelCode,
	 * r8 = the loaded _UTTypeDevice).  Both constant slots are NULL on this runtime; the point of
	 * this check is that the send ANSWERS and hands back an array instead of raising, whatever the
	 * array holds.  Apple's header (typesWithTag:...): "An array of types, or the empty array if no
	 * such types were available." */
	{
		void *tag_class_slot = constant_value(ut, "_UTTagClassDeviceModelCode");
		void *supertype_slot = constant_value(ut, "_UTTypeDevice");
		void *tag = string_from_c(string_class, "MacBookPro18,3");
		void *result = send3(uttype_class, "typesWithTag:tagClass:conformingToType:",
		    tag, tag_class_slot, supertype_slot);
		long count = result ? (long)msgSend(result, selRegister("count")) : -1;

		printf("UTTYPE-WORKLOAD-CALL tagClass_slot=%p supertype_slot=%p array=%p count=%ld\n",
		    tag_class_slot, supertype_slot, result, count);
		if (!result || count < 0) {
			printf("UTTYPE-FAIL stage=workload-call\n");
			failures++;
		}
	}

	/* ---- tag lookup with a real tag class, and the identifier property ---- */
	uttype_plain_text = constant_value(ut, "UTTypePlainText");
	uttype_image = constant_value(ut, "UTTypeImage");
	uttype_directory = constant_value(ut, "UTTypeDirectory");
	printf("UTTYPE-CONSTANTS plainText=%p image=%p directory=%p\n",
	    uttype_plain_text, uttype_image, uttype_directory);
	if (!uttype_plain_text || !uttype_image || !uttype_directory) {
		printf("UTTYPE-FAIL stage=constants\n");
		printf("UTTYPE_FAMILY_VERDICT=RED\n");
		return 1;
	}
	{
		void *ext_class = string_from_c(string_class, "public.filename-extension");
		void *types = send3(uttype_class, "typesWithTag:tagClass:conformingToType:",
		    string_from_c(string_class, "txt"), ext_class, NULL);
		long count = types ? (long)msgSend(types, selRegister("count")) : -1;
		void *first = (types && count > 0) ? send1(types, "objectAtIndex:", (void *)(long)0) : NULL;

		printf("UTTYPE-TYPES-COUNT tag=txt tagClass=public.filename-extension count=%ld\n", count);
		check_int("txt-array-count", count, 1);
		check_string("txt-array-first-identifier",
		    first ? c_from_string(send(first, selRegister("identifier"))) : NULL,
		    "public.plain-text");
		check_string("txt-array-first-preferred-extension",
		    first ? c_from_string(send(first, selRegister("preferredFilenameExtension"))) : NULL,
		    "txt");
		check_int("txt-array-first-isDeclared",
		    first ? (int)(long)send(first, selRegister("isDeclared")) : -1, 1);
		check_int("txt-array-first-conforms-to-public.plain-text",
		    first ? (int)(long)send1(first, selRegister("conformsToType:"), uttype_plain_text) : -1, 1);
		check_int("txt-array-first-conforms-to-public.image",
		    first ? (int)(long)send1(first, selRegister("conformsToType:"), uttype_image) : -1, 0);
	}

	/* ---- +typeWithIdentifier:, -identifier, -isDynamic/-isDeclared/-isPublicType, -conformsToType: ---- */
	{
		void *folder = send1(uttype_class, "typeWithIdentifier:", string_from_c(string_class, "public.folder"));
		void *unknown = send1(uttype_class, "typeWithIdentifier:", string_from_c(string_class, "no.such.type.example"));

		check_string("public.folder-identifier",
		    folder ? c_from_string(send(folder, selRegister("identifier"))) : NULL, "public.folder");
		check_int("public.folder-isDeclared",
		    folder ? (int)(long)send(folder, selRegister("isDeclared")) : -1, 1);
		check_int("public.folder-isDynamic",
		    folder ? (int)(long)send(folder, selRegister("isDynamic")) : -1, 0);
		check_int("public.folder-isPublicType",
		    folder ? (int)(long)send(folder, selRegister("isPublicType")) : -1, 1);
		check_int("public.folder-conforms-to-public.directory",
		    folder ? (int)(long)send1(folder, selRegister("conformsToType:"), uttype_directory) : -1, 1);
		check_int("public.folder-conforms-to-public.image",
		    folder ? (int)(long)send1(folder, selRegister("conformsToType:"), uttype_image) : -1, 0);
		/* Apple's header: "A type, or nil if the type identifier is not known to the system." */
		check_int("unknown-identifier-is-nil", unknown ? 0 : 1, 1);
	}

	/* ---- +typeWithFilenameExtension: ---- */
	{
		void *from_ext = send1(uttype_class, "typeWithFilenameExtension:", string_from_c(string_class, "txt"));

		check_string("typeWithFilenameExtension-txt",
		    from_ext ? c_from_string(send(from_ext, selRegister("identifier"))) : NULL, "public.plain-text");
	}

	/* ---- -stringByAppendingPathExtensionForType: ----
	 * Apple's UTAdditions.h: the receiver's own extension is kept when a type with that extension
	 * conforms to the requested type; otherwise the type's preferred extension is appended; when it
	 * cannot be appended the method returns a copy of the receiver. */
	{
		void *plain_readme = string_from_c(string_class, "readme");
		void *plain_already = string_from_c(string_class, "readme.txt");
		void *wrong_ext = string_from_c(string_class, "readme.jpg");

		check_string("append-extension-readme",
		    c_from_string(send1(plain_readme, "stringByAppendingPathExtensionForType:", uttype_plain_text)),
		    "readme.txt");
		check_string("append-extension-kept-when-valid",
		    c_from_string(send1(plain_already, "stringByAppendingPathExtensionForType:", uttype_plain_text)),
		    "readme.txt");
		check_string("append-extension-kept-when-invalid",
		    c_from_string(send1(wrong_ext, "stringByAppendingPathExtensionForType:", uttype_plain_text)),
		    "readme.jpg.txt");
	}

	if (failures != 0) {
		printf("UTTYPE-FAIL failures=%d\n", failures);
		printf("UTTYPE_FAMILY_VERDICT=RED\n");
		return 1;
	}
	printf("UTTYPE_FAMILY_VERDICT=GREEN\n");
	return 0;
}
