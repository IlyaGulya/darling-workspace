/*
 * A stub that reports success is worse than one that fails: the caller proceeds as if a keybag or smartcard
 * operation completed. This measures the contract on one representative entry point of libaks, the library where
 * the inventory found stubs printing a STUB line and then returning 0 (which for these APIs is kAKSReturnSuccess).
 *
 * The symbol is resolved the way a caller would - libaks is a separate dylib - so the test also observes which
 * image answers. Only one representative function is CALLED: the stubs ignore their arguments, but a real
 * implementation would not, and a probe that crashes against a future implementation would be a bad oracle. The
 * remaining four sites are covered by the source contract that no `return 0;` remains in the file.
 *
 * Verdict line (`dwdiag verdict` reads the first):
 *   AKS pass=1 image=<path> rc=<value> not-success=1
 */
#include <dlfcn.h>
#include <stdio.h>

typedef int (*aks_smartcard_request_unlock_t)(int a, unsigned char *b, int c, void **d, size_t *e);

int main(void)
{
	void *handle_holder = NULL;
	/* libaks is built as a static archive and is not a dylib of its own, so the symbols live in whichever loaded
	 * image links it. RTLD_DEFAULT asks the already-loaded set first (the same way my binding audit resolves the
	 * kqueue hooks), and the dlopen list is only a fallback in case a future build ships it separately. */
	aks_smartcard_request_unlock_t request_unlock =
		(aks_smartcard_request_unlock_t)dlsym(RTLD_DEFAULT, "aks_smartcard_request_unlock");
	if (!request_unlock) {
		const char *names[] = { "libaks.dylib", "/usr/lib/system/libaks.dylib", "/usr/lib/libaks.dylib" };
		for (unsigned i = 0; i < sizeof names / sizeof names[0] && !request_unlock; ++i) {
			handle_holder = dlopen(names[i], RTLD_NOW);
			if (handle_holder)
				request_unlock = (aks_smartcard_request_unlock_t)dlsym(handle_holder, "aks_smartcard_request_unlock");
		}
	}
	if (!request_unlock) {
		printf("AKS pass=0 reason=symbol-missing (aks_smartcard_request_unlock is in no loaded image)\n");
		return 1;
	}
	Dl_info info;
	dladdr((void *)request_unlock, &info);

	/* The stub ignores these arguments; the value it returns is what is under test. */
	int rc = request_unlock(0, NULL, 0, NULL, NULL);

	int pass = (rc != 0) ? 1 : 0;   /* kAKSReturnSuccess is 0: reporting it would be a lie */
	printf("AKS pass=%d image=%s rc=%d not-success=%d\n",
	       pass, info.dli_fname ? info.dli_fname : "(unresolved)", rc, rc != 0);
	return pass ? 0 : 1;
}
