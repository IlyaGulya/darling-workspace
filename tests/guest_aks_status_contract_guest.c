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


/* The library under test is linked INTO this fixture, so the honest way to reach it is a direct call: if the
 * archive were not linked, this becomes a loud link error instead of a quiet "not found". `dlsym(RTLD_DEFAULT)`
 * is deliberately not used -- a statically linked symbol in the main image need not be exported for lookup, and
 * that produced a false "symbol-missing" that looked like a missing implementation. */
extern int aks_smartcard_request_unlock(int a, unsigned char *b, int c, void **d, size_t *e);

int main(void)
{
	Dl_info info;
	dladdr((void *)&aks_smartcard_request_unlock, &info);

	/* The stub ignores these arguments; the value it returns is what is under test. */
	int rc = aks_smartcard_request_unlock(0, NULL, 0, NULL, NULL);

	int pass = (rc != 0) ? 1 : 0;   /* kAKSReturnSuccess is 0: reporting it would be a lie */
	printf("AKS pass=%d image=%s rc=%d not-success=%d\n",
	       pass, info.dli_fname ? info.dli_fname : "(unresolved)", rc, rc != 0);
	/* A verdict line the runner can match exactly: its --ok-marker check is `grep -F -x`, a whole-line
	 * match, so the informative line above (which carries a run-specific path) cannot serve as one. */
	if (pass) {
		printf("AKS_STATUS_OK\n");
	}
	return pass ? 0 : 1;
}
