// fd_diag_ctor -- a tiny injected guest dylib whose ONLY job is to dump the GUEST'S OWN descriptor table.
//
// WHY (follow-up 38). Every loader-side site tried so far (start_thread, mldr_bootstrap_before_dylinker_load)
// produced zero lines for the service image, and the host's /proc view of the service child is the table of a
// process whose comm is `mldr`, i.e. the LOADER's own mid-startup table -- not necessarily the guest's logical
// table. This dylib is injected into the spawn (DYLD_INSERT_LIBRARIES) so that its constructor runs inside the
// guest image itself, in the same process, and reports what the GUEST sees.
//
// It is deliberately tiny: one bounded pass over fds 0..15 at constructor time, writing
//   [fd-diag] guest-ctor pid=<n> fd=<n> flags=0x<hex> target=<readlink>
// to the fixed sink /private/var/tmp/psfa-fd-diag.log, opened O_APPEND. No other behaviour, no state, no exit
// path that can change the process's fate. A `once` guard keeps a re-injected image from writing twice from the
// same process.
//
// The sink and the format are the same ones the loader/emulation instruments used, so the four views (emulation
// post-actions, loader mid-startup via the host, loader handoff, guest constructor) are directly comparable.

#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

__attribute__((constructor)) static void fd_diag_ctor(void)
{
	static int done = 0;
	if (done) {
		return;
	}
	done = 1;

	int dfd = open("/private/var/tmp/psfa-fd-diag.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
	if (dfd < 0) {
		return;
	}
	for (int fd = 0; fd < 16; fd++) {
		int fl = fcntl(fd, F_GETFD);
		if (fl < 0) {
			continue;
		}
		char path[32];
		char tgt[256];
		snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
		ssize_t r = readlink(path, tgt, sizeof(tgt) - 1);
		if (r > 0) {
			tgt[r] = '\0';
		} else {
			snprintf(tgt, sizeof(tgt), "?");
		}
		char buf[400];
		int n = snprintf(buf, sizeof(buf), "[fd-diag] guest-ctor pid=%d fd=%d flags=0x%x target=%s\n",
			(int)getpid(), fd, fl, tgt);
		if (n > 0) {
			write(dfd, buf, n);
		}
	}
	close(dfd);
}
