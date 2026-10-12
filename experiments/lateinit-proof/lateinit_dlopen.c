/* lateinit_dlopen.c -- positive proof that a dlopen'd image runs its constructor.
 *
 * Prints its own markers BEFORE and AFTER dlopen, and dlopens the install name
 * given as argv[1] with RTLD_NOW. The constructor of the target image writes
 * its notice to fd 2 during the load, so the notice must appear BETWEEN the
 * markers when the load succeeds and must be ABSENT when it fails.
 */
#include <stdio.h>
#include <dlfcn.h>

int main(int argc, char **argv)
{
	const char *path = argc > 1 ? argv[1] : "/usr/lib/libAppleArchive.dylib";

	fputs("LATEINIT-BEFORE\n", stderr);
	fflush(stderr);

	void *handle = dlopen(path, RTLD_NOW);
	if (handle != NULL) {
		fprintf(stderr, "LATEINIT-DLOPEN path=%s handle=OK\n", path);
	} else {
		const char *err = dlerror();
		fprintf(stderr, "LATEINIT-DLOPEN path=%s handle=NULL err=%s\n",
		        path, err != NULL ? err : "(null)");
	}
	fflush(stderr);

	fputs("LATEINIT-AFTER\n", stderr);
	fflush(stderr);
	return handle != NULL ? 0 : 3;
}
