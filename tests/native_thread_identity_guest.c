/*
 * native_thread_identity_guest.c -- the GUEST half of the permanent oracle for the guest false-identity defect
 * (bead dar-liaq).
 *
 * It loads a NATIVE (host ELF) library through the loader's own native-library API (`_elfcalls->dlopen`, the same
 * mechanism the product's native wrappers use), hands it a callback that reads THIS thread's guest TSD slot 3
 * (__TSD_MACH_THREAD_SELF) the way libsystem_platform/libdispatch do (`movq %gs:24`), and lets the native library
 * create a thread with the HOST libc's pthread_create and report, for both the creating and the created thread, the
 * GS base and the slot 3 word.
 *
 * argv[1] is the path of the native library to load, as the HOST dynamic loader must resolve it (mldr dlopens it
 * in the guest process, so the prefix's own absolute path is the right spelling). The ABI header is staged beside
 * it in the prefix's persistent guest temp directory; the include below is the GUEST path for that same file.
 *
 * Exit status is the native oracle's: 0 GREEN (the child has its own identity or none), 1 RED (the child answers
 * the creator's live token), 2 the instrument could not measure.
 */
#include "/private/var/tmp/natident-elfcalls.h"

#include <stdio.h>
#include <unistd.h>

/* The loader installs the elfcalls table in every guest process and libsystem_kernel publishes a getter for it; the
 * getter is called rather than referencing the table symbol directly, so the fixture does not need the symbol to
 * appear in the SDK's libSystem stub (the deployed dylib is the one that defines it). */
extern void* elfcalls_get_pointer(void);

/* Read THIS thread's guest TSD slot 3. The same instruction libplatform's direct reader uses. */
static int guest_slot3(void)
{
	unsigned long v = 0;
	__asm__ volatile("movq %%gs:24, %0" : "=r"(v));
	return (int)(unsigned)v;
}

int main(int argc, char** argv)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	printf("NATIDENT-GUEST guest-main pid=%d slot3=0x%x\n", (int)getpid(), guest_slot3());
	fflush(NULL);

	if (argc < 2) {
		printf("NATIDENT-GUEST-VERDICT INSTRUMENT-FAILED no native library path was passed as argv[1]\n");
		printf("NATIDENT-GUEST-DONE=1\n");
		return 2;
	}

	struct elf_calls* calls = (struct elf_calls*)elfcalls_get_pointer();
	printf("NATIDENT-GUEST elfcalls=%p\n", (void*)calls);
	fflush(NULL);
	if (calls == NULL || calls->dlopen == NULL || calls->dlsym == NULL) {
		printf("NATIDENT-GUEST-VERDICT INSTRUMENT-FAILED the elfcalls table is unavailable\n");
		printf("NATIDENT-GUEST-DONE=1\n");
		return 2;
	}

	void* handle = calls->dlopen(argv[1]);
	printf("NATIDENT-GUEST dlopen(%s) -> %p dlerror=%s\n", argv[1], handle,
	    handle ? "(none)" : (calls->dlerror() ? calls->dlerror() : "(null)"));
	fflush(NULL);
	if (handle == NULL) {
		printf("NATIDENT-GUEST-VERDICT INSTRUMENT-FAILED the native library did not load\n");
		printf("NATIDENT-GUEST-DONE=1\n");
		return 2;
	}

	int (*probe)(int (*)(void)) = (int (*)(int (*)(void)))calls->dlsym(handle, "natident_probe");
	printf("NATIDENT-GUEST probe=%p\n", (void*)probe);
	fflush(NULL);
	if (probe == NULL) {
		printf("NATIDENT-GUEST-VERDICT INSTRUMENT-FAILED dlsym(natident_probe) is NULL\n");
		printf("NATIDENT-GUEST-DONE=1\n");
		return 2;
	}

	printf("NATIDENT-GUEST guest-slot-before=0x%x\n", guest_slot3());
	fflush(NULL);
	int rc = probe(guest_slot3);
	fflush(NULL);
	printf("NATIDENT-GUEST guest-slot-after=0x%x\n", guest_slot3());
	printf("NATIDENT-GUEST-RC=%d\n", rc);
	printf("NATIDENT-GUEST-DONE=1\n");
	fflush(NULL);
	return rc;
}
