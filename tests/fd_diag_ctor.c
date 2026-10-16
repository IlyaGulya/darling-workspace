// fd_diag_ctor -- a tiny injected guest dylib that dumps the GUEST'S OWN descriptor table, WITH ATTRIBUTION.
//
// FU 39/40 carried forward: Darling's guest /proc/<pid>/fd does NOT implement the fd symlinks (so identity is
// PROBED with fstat -- mode + dev/ino/rdev -- never readlinked), and a heavy dynamically linked closure does not
// renumber the guest's stdio. The two loader C sites stay retired (they fire for neither run); the emulation
// sites fire for both and are the anchor.
//
// FU 40 fix: a table without attribution is not evidence. Every process therefore emits ONE identity line FIRST
//   [fd-id]    pid=<n> ppid=<n> exe=<_NSGetExecutablePath> argv0=<argv[0]> argv=<argv[1..4]>
// followed by one table line per fd 0..15
//   [fd-diag]  pid=<n> fd=<n> kind=<fifo|chr|sock|reg|dir|other> mode=0<octal> dev=… ino=… rdev=…
// so a reader can GROUP TABLES BY IMAGE, and the fixture's own intentional spawns are recognizable by argv.
// One bounded pass, fixed sink /private/var/tmp/psfa-fd-diag.log, O_APPEND. Injected through
// DYLD_INSERT_LIBRARIES; linked -nostdlib -undefined dynamic_lookup like the build tree's own dylibs.

#include <crt_externs.h>
#include <fcntl.h>
#include <mach-o/dyld.h>
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
	int pid = (int)getpid();
	int ppid = (int)getppid();

	char exe[1024];
	uint32_t sz = sizeof(exe);
	if (_NSGetExecutablePath(exe, &sz) != 0) {
		snprintf(exe, sizeof(exe), "?");
	}

	char** argv = *_NSGetArgv();
	char a0[256], a1[256], a2[256], a3[256], a4[256];
	snprintf(a0, sizeof(a0), "%s", argv && argv[0] ? argv[0] : "?");
	snprintf(a1, sizeof(a1), "%s", argv && argv[1] ? argv[1] : "-");
	snprintf(a2, sizeof(a2), "%s", argv && argv[2] ? argv[2] : "-");
	snprintf(a3, sizeof(a3), "%s", argv && argv[3] ? argv[3] : "-");
	snprintf(a4, sizeof(a4), "%s", argv && argv[4] ? argv[4] : "-");

	char id[1400];
	int m = snprintf(id, sizeof(id),
		"[fd-id] pid=%d ppid=%d exe=%s argv0=%s argv1=%s argv2=%s argv3=%s argv4=%s\n",
		pid, ppid, exe, a0, a1, a2, a3, a4);
	if (m > 0) {
		write(dfd, id, m);
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
			"[fd-diag] pid=%d fd=%d kind=%s mode=0%o dev=%llu ino=%llu rdev=%llu\n",
			pid, fd, kind, (unsigned)st.st_mode,
			(unsigned long long)st.st_dev, (unsigned long long)st.st_ino,
			(unsigned long long)st.st_rdev);
		if (n > 0) {
			write(dfd, buf, n);
		}
	}
	close(dfd);
}
