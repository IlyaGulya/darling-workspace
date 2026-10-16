// fd_diag_ctor -- a tiny injected guest dylib whose ONLY job is to dump the GUEST'S OWN descriptor table.
//
// FU 39: readings (1) and (2) are carried forward -- Darling's guest /proc/<pid>/fd does NOT implement the fd
// symlinks (every in-guest readlink returned nothing; f29's child_fd_targets=[absent] was never permissions),
// and a heavy dynamically linked closure does not renumber the guest's stdio. So identity here is PROBED with
// fstat (mode + dev/ino/rdev), never readlinked, and the dump is one bounded pass over fds 0..15 to the fixed
// sink /private/var/tmp/psfa-fd-diag.log, opened O_APPEND. Injected into the service spawn through
// DYLD_INSERT_LIBRARIES passed positionally to run-workload.sh. Linked -nostdlib like the build tree's own
// dylibs (undefined symbols resolve at run time against the guest's libSystem).
//
// Mode is printed in octal (0%o) so S_IFIFO=0010000, S_IFCHR=0020000 and S_IFREG=0100000 are read directly.

#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

__attribute__((constructor)) static void fd_diag_ctor(void)
{
	int dfd = open("/private/var/tmp/psfa-fd-diag.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
	if (dfd < 0) {
		return;
	}
	for (int fd = 0; fd < 16; fd++) {
		struct stat st;
		if (fstat(fd, &st) < 0) {
			continue;
		}
		unsigned ifmt = (unsigned)(st.st_mode & S_IFMT);
		const char* kind = ifmt == S_IFIFO ? "fifo" : ifmt == S_IFCHR ? "chr"
			: ifmt == S_IFSOCK ? "sock" : ifmt == S_IFREG ? "reg" : ifmt == S_IFDIR ? "dir" : "other";
		char buf[400];
		int n = snprintf(buf, sizeof(buf),
			"[fd-diag] guest-ctor pid=%d fd=%d kind=%s mode=0%o dev=%llu ino=%llu rdev=%llu\n",
			(int)getpid(), fd, kind, (unsigned)st.st_mode,
			(unsigned long long)st.st_dev, (unsigned long long)st.st_ino,
			(unsigned long long)st.st_rdev);
		if (n > 0) {
			write(dfd, buf, n);
		}
	}
	close(dfd);
}
