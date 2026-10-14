/*
 * xcode_system_resources_load_guest.c -- guest-side check for the Apple frameworks the declared
 * provisioning route (west darling-xcode-system-resources) installs.
 *
 * WHAT IT CHECKS, and why each part is checkable:
 *
 *   1. the demanded image exists at the guest path a real Mac's Xcode install produces
 *      (/Library/Developer/PrivateFrameworks/DVTDownloads.framework/Versions/A/DVTDownloads), and
 *      the file there IS that image: its Mach-O LC_ID_DYLIB names exactly the demanded install name
 *      the referrer's LC_LOAD_DYLIB carries. This is the deployment property the route establishes,
 *      measured from the deployed bytes rather than from the receipt;
 *   2. THE LOADER FINDS IT. dlopen() of the demanded absolute path is the loader's own decision. If
 *      it succeeds, the image loaded. If it fails, the fixture parses dyld's message: it reads the
 *      path after "Library not loaded:" and fails only when THAT path is the demanded one (that is
 *      exactly the "image not found" the previous rung's wall reported). A failure whose missing
 *      path is a DIFFERENT image means the loader resolved this framework and stopped inside its
 *      own dependency closure instead -- which the fixture prints with identity as the next unmet
 *      demand, because that closure (DiskImages2, libcryptex, libimg4, libswiftSystem_Foundation)
 *      is not in XcodeSystemResources.pkg and this tree implements none of it.
 *
 * XCODE_SYSTEM_RESOURCES_VERDICT=GREEN is the machine-readable line the transport's ok-marker reads.
 * Exit status is 0 on GREEN, 1 on RED. Every measured value is printed unconditionally.
 */

#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

/* The demanded install names, one per line of the route's DEMANDED_FRAMEWORKS table. */
static const char *const demanded_images[] = {
	"/Library/Developer/PrivateFrameworks/DVTDownloads.framework/Versions/A/DVTDownloads",
};

#define DEMANDED_COUNT (sizeof(demanded_images) / sizeof(demanded_images[0]))

#define LC_ID_DYLIB 0x0d
#define CPU_TYPE_X86_64 0x01000007
#define CPU_TYPE_ARM64 0x0100000c
#define FAT_MAGIC 0xcafebabe
#define MH_MAGIC_64 0xfeedfacf

static int failures;

/* Reads the LC_ID_DYLIB name of the x86_64 slice into `out`. Returns 1 when found. */
static int image_install_name(const char *path, char *out, size_t out_size)
{
	FILE *file = fopen(path, "rb");
	if (!file) {
		printf("XCODE_SYSTEM_RESOURCES-FILE %s does not open: %s\n", path, strerror(errno));
		return 0;
	}
	fseek(file, 0, SEEK_END);
	long size = ftell(file);
	fseek(file, 0, SEEK_SET);
	if (size <= 0) {
		fclose(file);
		return 0;
	}
	unsigned char *bytes = (unsigned char *)malloc((size_t)size);
	if (!bytes) {
		fclose(file);
		return 0;
	}
	size_t got = fread(bytes, 1, (size_t)size, file);
	fclose(file);
	if (got != (size_t)size) {
		free(bytes);
		return 0;
	}

	unsigned long base = 0;
	uint32_t magic = ((uint32_t)bytes[0] << 24) | ((uint32_t)bytes[1] << 16) |
			 ((uint32_t)bytes[2] << 8) | (uint32_t)bytes[3];
	if (magic == FAT_MAGIC) {
		uint32_t nfat = ((uint32_t)bytes[4] << 24) | ((uint32_t)bytes[5] << 16) |
				((uint32_t)bytes[6] << 8) | (uint32_t)bytes[7];
		int found = 0;
		for (uint32_t i = 0; i < nfat && !found; i++) {
			unsigned char *arch = bytes + 8 + i * 20;
			uint32_t cputype = ((uint32_t)arch[0] << 24) | ((uint32_t)arch[1] << 16) |
					   ((uint32_t)arch[2] << 8) | (uint32_t)arch[3];
			uint32_t offset = ((uint32_t)arch[8] << 24) | ((uint32_t)arch[9] << 16) |
					  ((uint32_t)arch[10] << 8) | (uint32_t)arch[11];
			if (cputype == CPU_TYPE_X86_64 || cputype == CPU_TYPE_ARM64) {
				base = offset;
				found = 1;
			}
		}
		if (!found) {
			free(bytes);
			return 0;
		}
	} else if (magic == MH_MAGIC_64) {
		base = 0;
	} else {
		free(bytes);
		return 0;
	}

	unsigned char *header = bytes + base;
	uint32_t thin_magic = (uint32_t)header[0] | ((uint32_t)header[1] << 8) |
			      ((uint32_t)header[2] << 16) | ((uint32_t)header[3] << 24);
	if (thin_magic != MH_MAGIC_64) {
		free(bytes);
		return 0;
	}
	uint32_t ncmds = (uint32_t)header[16] | ((uint32_t)header[17] << 8) |
			 ((uint32_t)header[18] << 16) | ((uint32_t)header[19] << 24);
	unsigned long command = 32;
	int found = 0;
	for (uint32_t i = 0; i < ncmds && !found; i++) {
		uint32_t cmd = (uint32_t)header[command] | ((uint32_t)header[command + 1] << 8) |
			       ((uint32_t)header[command + 2] << 16) | ((uint32_t)header[command + 3] << 24);
		uint32_t cmdsize = (uint32_t)header[command + 4] | ((uint32_t)header[command + 5] << 8) |
				   ((uint32_t)header[command + 6] << 16) |
				   ((uint32_t)header[command + 7] << 24);
		if (cmdsize < 8)
			break;
		if (cmd == LC_ID_DYLIB) {
			uint32_t name_offset = (uint32_t)header[command + 8] |
					       ((uint32_t)header[command + 9] << 8) |
					       ((uint32_t)header[command + 10] << 16) |
					       ((uint32_t)header[command + 11] << 24);
			const char *name = (const char *)(header + command + name_offset);
			snprintf(out, out_size, "%s", name);
			found = 1;
		}
		command += cmdsize;
	}
	free(bytes);
	return found;
}

/* The path dyld's own message names after "Library not loaded: ", into `out`. Returns 1 when the
 * message has that shape (i.e. a dependency was not found at all). The token ends at the first
 * whitespace, so a message that continues on the same line ("... missing (referenced from ...)")
 * still yields the path alone. */
static int missing_image_of(const char *error, char *out, size_t out_size)
{
	const char *marker = strstr(error, "Library not loaded: ");
	if (!marker)
		return 0;
	marker += strlen("Library not loaded: ");
	size_t length = 0;
	while (marker[length] != '\0' && marker[length] != '\n' && marker[length] != '\r' &&
	       marker[length] != ' ' && marker[length] != '\t' && marker[length] != '(') {
		length++;
	}
	if (length == 0 || length >= out_size)
		return 0;
	memcpy(out, marker, length);
	out[length] = '\0';
	return 1;
}

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);

	for (unsigned long i = 0; i < DEMANDED_COUNT; i++) {
		const char *expected = demanded_images[i];
		char install_name[1024] = "";

		if (!image_install_name(expected, install_name, sizeof(install_name))) {
			failures++;
			printf("XCODE_SYSTEM_RESOURCES-DECISION %s is not a Mach-O whose LC_ID_DYLIB can be "
			       "read, so the demanded install name is not on disk\n", expected);
			continue;
		}
		int identity_ok = strcmp(install_name, expected) == 0;
		if (!identity_ok)
			failures++;
		printf("XCODE_SYSTEM_RESOURCES-IMAGE %-64s %-9s lc_id=%s\n", expected,
		       identity_ok ? "ok" : "MISMATCH", install_name);

		void *handle = dlopen(expected, RTLD_NOW | RTLD_GLOBAL);
		const char *error = handle ? NULL : dlerror();
		printf("XCODE_SYSTEM_RESOURCES-DLOPEN %-64s handle=%p error=%s\n", expected, handle,
		       error ? error : "-");
		if (handle) {
			printf("XCODE_SYSTEM_RESOURCES-RESOLVED %s loaded by the guest loader\n", expected);
			continue;
		}
		char missing[1024] = "";
		char literal[1088];
		snprintf(literal, sizeof(literal), "Library not loaded: %s", expected);
		if (strstr(error ? error : "", literal) != NULL) {
			failures++;
			printf("XCODE_SYSTEM_RESOURCES-DECISION the guest loader does NOT find %s -- dyld's "
			       "own text names it as the missing image, which is the wall this route "
			       "removes\n", expected);
			continue;
		}
		if (!missing_image_of(error ? error : "", missing, sizeof(missing))) {
			failures++;
			printf("XCODE_SYSTEM_RESOURCES-DECISION %s failed to load for a reason that is not a "
			       "missing image (\"%s\"), so the loader's decision cannot be read here\n",
			       expected, error ? error : "(no dlerror)");
			continue;
		}
		if (strcmp(missing, expected) == 0) {
			failures++;
			printf("XCODE_SYSTEM_RESOURCES-DECISION the guest loader does NOT find %s -- dyld "
			       "names it as the missing image, which is the wall this route removes\n",
			       expected);
			continue;
		}
		printf("XCODE_SYSTEM_RESOURCES-RESOLVED %s found by the guest loader; the stop has moved "
		       "into its own dependency closure: missing image %s\n", expected, missing);
	}

	printf("XCODE_SYSTEM_RESOURCES-FAILURES=%d\n", failures);
	if (failures == 0)
		printf("XCODE_SYSTEM_RESOURCES_VERDICT=GREEN\n");
	else
		printf("XCODE_SYSTEM_RESOURCES_VERDICT=RED\n");
	return failures == 0 ? 0 : 1;
}
