// posix_spawn_file_actions_guest -- do posix_spawn file actions reach the CHILD, and does a parent's write on
// a pipe it handed the child arrive?
//
// WHY THIS EXISTS. The matched run mrs2 (evidence/oslog-framework-rung/README.md section 32) measured the
// build-service channel and found: the arm calls
//     posix_spawn(..., { attributes = ...|POSIX_SPAWN_CLOEXEC_DEFAULT,
//                        file_actions = {close(0), dup2(54,0), close(1), dup2(57,1), chdir(...), ...} })
// the host writes the request (write_nocancel(55, ..., 465) -> 465), and the child (SWBBuildService) has
// fd 0 = /dev/null, fd 1 = not the reply pipe, while BOTH pipe ends sit in the child at fds 8/9/27 -- so the
// service never reads the request. That measurement cannot say whether the file actions are applied by the
// spawn path in general or whether something about that particular spawn (the tracer, the arm's own fds, the
// sender being Xcode's code) is responsible. This fixture decides it with a plain guest program and NO tracer:
//
//   case actions   posix_spawn with attributes CLOEXEC_DEFAULT and file actions
//                  close(0), dup2(p_read, 0), close(1), dup2(p_write, 1); the child reports where every FIFO in
//                  its own fd table is (fstat over fds 0..63), tries to read a line from fd 0 and writes a
//                  reply to fd 1.
//   case control   the same spawn WITHOUT any file actions, with CLOEXEC_DEFAULT: this separates "the actions
//                  are ignored" from "the spawn inherits whatever the parent had".
//   case altname   the same spawn as `actions`, but the child is started through the /Volumes/SystemRoot/...
//                  spelling of the same binary -- the shape the arm itself uses for SWBBuildService -- so that
//                  a path-dependent exec cannot hide the behaviour.
//
// ASSERTIONS (printed, never guessed): (a) the child's fd 0 is a FIFO, (b) the parent's write is READ by the
// child (the child echoes the exact line it read), (c) the child's reply arrives on the parent's read end.
// Each case prints one `FIR psfa ...` line; the fixture prints PSFA_DONE at the end (a plain marker: a marker
// text ending in '=' is judged a STATUS field by the harness and fails closed).
//
// WEAK SPOT, stated rather than hidden: fstat says a descriptor is a FIFO, not which pipe it is; the pairing
// claim rests on the line the child read and on the reply the parent received, which is why both are printed.
// The whole fixture is bounded by alarm(90), and every read is non-blocking with a bounded retry loop.

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
#include <sys/wait.h>

extern char** environ;

static int fifo_list(char* out, size_t n)
{
	int used = 0, count = 0;
	out[0] = 0;
	for (int fd = 0; fd < 64; fd++) {
		struct stat st;
		if (fstat(fd, &st) != 0) continue;
		if (!S_ISFIFO(st.st_mode)) continue;
		used += snprintf(out + used, n - used, "%d ", fd);
		count++;
		if (used >= (int)n - 8) break;
	}
	return count;
}

static void report_fifos(const char* tag)
{
	char line[256];
	int count = fifo_list(line, sizeof(line));
	printf("FIR psfa %s child_fifo_fds=[%s] nfifo=%d\n", tag, line, count);
	fflush(stdout);
}

static int fd_is_fifo(int fd)
{
	struct stat st;
	return fstat(fd, &st) == 0 && S_ISFIFO(st.st_mode);
}

// ---- the child role -----------------------------------------------------------------------------------
static int child_role(const char* tag, int read_fd_for_echo)
{
	// NOTE (why stderr): the file actions make this process's stdout the parent's pipe, so a report written
	// only to stdout is invisible in the harness log -- the first version of this fixture lost its own child
	// report that way. fd 2 is not touched by the actions, so the same report goes to both.
	char fifos[256];
	fifo_list(fifos, sizeof(fifos));
	fprintf(stderr, "FIR psfa %s child_started pid=%d fd0_fifo=%d fd1_fifo=%d fifo_fds=[%s]\n", tag,
		(int)getpid(), fd_is_fifo(0), fd_is_fifo(1), fifos);
	fflush(stderr);
	printf("FIR psfa %s child_started pid=%d fd0_fifo=%d fd1_fifo=%d\n", tag, (int)getpid(),
		fd_is_fifo(0), fd_is_fifo(1));
	report_fifos(tag);

	char buf[256];
	memset(buf, 0, sizeof(buf));
	ssize_t got = -1;
	// bounded, non-blocking read loop: the parent writes immediately, so a short window is enough
	fcntl(0, F_SETFL, fcntl(0, F_GETFL, 0) | O_NONBLOCK);
	for (int i = 0; i < 200; i++) {
		got = read(0, buf, sizeof(buf) - 1);
		if (got > 0) break;
		if (got < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) break;
		struct timespec ts = {0, 5 * 1000 * 1000};
		nanosleep(&ts, NULL);
	}

	fprintf(stderr, "FIR psfa %s child_read got=%ld text=\"%s\"\n", tag, (long)got, got > 0 ? buf : "");
	printf("FIR psfa %s child_read got=%ld text=\"%s\"\n", tag, (long)got, got > 0 ? buf : "");
	fflush(NULL);

	if (got > 0) {
		char reply[256];
		int n = snprintf(reply, sizeof(reply), "CHILD-REPLY %s\n", tag);
		ssize_t wrote = write(1, reply, n);
		fprintf(stderr, "FIR psfa %s child_wrote fd1=%ld\n", tag, (long)wrote);
		fflush(stderr);
	} else {
		// nothing arrived on fd 0; say so on fd 1 as well, so the parent can tell "silence" from "no channel"
		const char* nope = "CHILD-NO-INPUT\n";
		fprintf(stderr, "FIR psfa %s child_wrote fd1=%ld (no input)\n", tag, (long)write(1, nope, strlen(nope)));
		fflush(stderr);
	}
	fflush(stdout);
	(void)read_fd_for_echo;
	_exit(0);
}

// ---- one spawn case -----------------------------------------------------------------------------------
struct case_result {
	int spawn_rc;
	int child_status;
	int child_fd0_fifo;
	long parent_read;
	char parent_text[4096];
};

// Attribute ladder: the service passes SETPGROUP|SETSIGDEF|SETSIGMASK|CLOEXEC_DEFAULT, the plain fixture passed
// CLOEXEC_DEFAULT alone. Each bit can be dropped, which is the bisect.
#define A_SETPGROUP   (1u << 0)
#define A_SETSIGDEF   (1u << 1)
#define A_SETSIGMASK  (1u << 2)
#define A_CLOEXEC     (1u << 3)
#define A_ALL         (A_SETPGROUP | A_SETSIGDEF | A_SETSIGMASK | A_CLOEXEC)

// Action ladder: the service passes close(0), dup2(pin,0), close(1), dup2(pout,1), chdir(<dir>).
#define ACT_DUP2   (1u << 0)
#define ACT_CHDIR  (1u << 1)
#define ACT_ALL    (ACT_DUP2 | ACT_CHDIR)

static void run_case(const char* tag, const char* path, unsigned int acts, unsigned int attrs, const char* chdir_to)
{
	int p_in[2] = {-1, -1};   // parent -> child
	int p_out[2] = {-1, -1};  // child -> parent
	if (pipe(p_in) != 0 || pipe(p_out) != 0) {
		printf("FIR psfa %s PIPEFAIL errno=%d\n", tag, errno);
		fflush(stdout);
		return;
	}

	posix_spawn_file_actions_t fa;
	posix_spawn_file_actions_init(&fa);
	if (acts & ACT_DUP2) {
		posix_spawn_file_actions_addclose(&fa, 0);
		posix_spawn_file_actions_adddup2(&fa, p_in[0], 0);
		posix_spawn_file_actions_addclose(&fa, 1);
		posix_spawn_file_actions_adddup2(&fa, p_out[1], 1);
	}
	if ((acts & ACT_CHDIR) && chdir_to != NULL) {
		posix_spawn_file_actions_addchdir_np(&fa, chdir_to);
	}

	posix_spawnattr_t attr;
	posix_spawnattr_init(&attr);
	short flags = 0;
	if (attrs & A_SETPGROUP) flags |= POSIX_SPAWN_SETPGROUP;
	if (attrs & A_SETSIGDEF) flags |= POSIX_SPAWN_SETSIGDEF;
	if (attrs & A_SETSIGMASK) flags |= POSIX_SPAWN_SETSIGMASK;
	if (attrs & A_CLOEXEC) flags |= POSIX_SPAWN_CLOEXEC_DEFAULT;
	posix_spawnattr_setflags(&attr, flags);
	if (attrs & A_SETPGROUP) {
		posix_spawnattr_setpgroup(&attr, 0);
	}
	sigset_t empty;
	sigemptyset(&empty);
	if (attrs & A_SETSIGMASK) {
		posix_spawnattr_setsigmask(&attr, &empty);
	}
	if (attrs & A_SETSIGDEF) {
		posix_spawnattr_setsigdefault(&attr, &empty);
	}

	char* argv[] = {(char*)path, (char*)"child", (char*)tag, NULL};
	pid_t pid = -1;
	struct case_result r;
	memset(&r, 0, sizeof(r));
	int spawn_rc = posix_spawn(&pid, path, &fa, &attr, argv, environ);
	posix_spawn_file_actions_destroy(&fa);
	posix_spawnattr_destroy(&attr);

	printf("FIR psfa %s spawn rc=%d pid=%d acts=0x%x attrs=0x%x chdir=%s path=%s\n", tag, spawn_rc, (int)pid,
		acts, attrs, chdir_to ? chdir_to : "-", path);
	fflush(stdout);
	if (spawn_rc != 0) return;

	// what the CHILD's own descriptor table says, from the guest side: /proc is the host's, and guest pids are
	// the host pids here, so this is the same question the harness asked of SWBBuildService
	char fdsnap[512];
	int used = 0;
	fdsnap[0] = 0;
	for (int fd = 0; fd <= 3; fd++) {
		char link[128], tgt[512];
		snprintf(link, sizeof(link), "/proc/%d/fd/%d", (int)pid, fd);
		ssize_t rr = readlink(link, tgt, sizeof(tgt) - 1);
		if (rr < 0) { used += snprintf(fdsnap + used, sizeof(fdsnap) - used, "%d=absent ", fd); }
		else { tgt[rr] = 0; used += snprintf(fdsnap + used, sizeof(fdsnap) - used, "%d=%s ", fd, tgt); }
	}
	printf("FIR psfa %s child_fd_targets=[%s]\n", tag, fdsnap);
	fflush(stdout);

	close(p_in[0]);
	close(p_out[1]);

	const char* ping = "PARENT-PING\n";
	ssize_t w = write(p_in[1], ping, strlen(ping));
	close(p_in[1]);
	printf("FIR psfa %s parent_write=%ld\n", tag, (long)w);
	fflush(stdout);

	// drain the reply pipe completely (the first version read one chunk and lost the child's own report, which
	// had to share the pipe with the loader's startup records)
	fcntl(p_out[0], F_SETFL, fcntl(p_out[0], F_GETFL, 0) | O_NONBLOCK);
	r.parent_read = 0;
	memset(r.parent_text, 0, sizeof(r.parent_text));
	for (int i = 0; i < 600; i++) {
		ssize_t got = read(p_out[0], r.parent_text + r.parent_read,
			sizeof(r.parent_text) - 1 - r.parent_read);
		if (got > 0) { r.parent_read += (long)got; if (r.parent_read >= (long)sizeof(r.parent_text) - 1) break; continue; }
		if (got == 0) break;  // EOF: every writer closed, the run is done
		if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) break;
		struct timespec ts = {0, 5 * 1000 * 1000};
		nanosleep(&ts, NULL);
	}
	close(p_out[0]);

	int status = 0;
	waitpid(pid, &status, 0);
	r.child_status = status;

	printf("FIR psfa %s parent_read=%ld child_status=%d pipe_text=\"%.300s\"\n", tag, r.parent_read,
		r.child_status, r.parent_text);
	fflush(stdout);
}

// f35: spawn the REAL build service with the service's own five actions and four attributes, WITHOUT waiting for
// it (it never exits on its own), so the HOST can sample the child's fd table while it is alive. This decides
// whether the trigger of the fd-0-/dev/null state belongs to the child image or to the arm as parent.
static void spawn_and_hold(const char* tag, const char* path)
{
	int p_in[2] = {-1, -1}, p_out[2] = {-1, -1};
	if (pipe(p_in) != 0 || pipe(p_out) != 0) {
		printf("FIR psfa %s PIPEFAIL errno=%d\n", tag, errno);
		fflush(stdout);
		return;
	}
	posix_spawn_file_actions_t fa;
	posix_spawn_file_actions_init(&fa);
	posix_spawn_file_actions_addclose(&fa, 0);
	posix_spawn_file_actions_adddup2(&fa, p_in[0], 0);
	posix_spawn_file_actions_addclose(&fa, 1);
	posix_spawn_file_actions_adddup2(&fa, p_out[1], 1);
	posix_spawn_file_actions_addchdir_np(&fa, "/private/var/tmp");

	posix_spawnattr_t attr;
	posix_spawnattr_init(&attr);
	posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK |
		POSIX_SPAWN_CLOEXEC_DEFAULT);
	posix_spawnattr_setpgroup(&attr, 0);
	sigset_t empty;
	sigemptyset(&empty);
	posix_spawnattr_setsigmask(&attr, &empty);
	posix_spawnattr_setsigdefault(&attr, &empty);

	char* argv[] = {(char*)path, NULL};
	pid_t pid = -1;
	int rc = posix_spawn(&pid, path, &fa, &attr, argv, environ);
	posix_spawn_file_actions_destroy(&fa);
	posix_spawnattr_destroy(&attr);
	printf("FIR psfa %s SERVICE-SPAWN rc=%d pid=%d parent=%d path=%s\n", tag, rc, (int)pid, (int)getpid(), path);
	fflush(stdout);
	// PUBLISH THE PIDS INTO THE PREFIX (f36): the run's own state directory and its harness.log are
	// teardown-time artifacts (measured in f35), so a host sampler that polls them sees nothing during the hold.
	// /private/var/tmp is the prefix's own tmp and is live and host-readable the whole time.
	{
		int pf = open("/private/var/tmp/psfa-pids.txt", O_WRONLY | O_CREAT | O_APPEND, 0644);
		if (pf >= 0) {
			char b[160];
			int n = snprintf(b, sizeof(b), "self=%d service=%d rc=%d held_seconds=30 t=%ld\n",
				(int)getpid(), (int)pid, rc, (long)time(NULL));
			if (n > 0) {
				write(pf, b, n);
			}
			close(pf);
		}
	}
	if (rc != 0) return;
	close(p_in[0]);
	close(p_out[1]);
	// hold the pipes (and therefore the child) here while the host samples: the service waits for a request
	// forever, so no waitpid is used and the child is left to the harness's teardown.
	struct timespec ts = {30, 0};
	nanosleep(&ts, NULL);
	printf("FIR psfa %s SERVICE-HOLD-DONE pid=%d\n", tag, (int)pid);
	fflush(stdout);
	close(p_in[1]);
	close(p_out[0]);
}

int main(int argc, char** argv)
{
	alarm(240);
	// The control case spawns a child that CANNOT receive the ping (no actions), so the parent's write to the
	// pipe may find no reader: that is a legitimate observed outcome ("the child cannot be reached"), not a
	// reason for the fixture to die before the remaining cases run (measured in the v3 run: SIGPIPE killed it
	// right after the control spawn).
	signal(SIGPIPE, SIG_IGN);
	setvbuf(stdout, NULL, _IOLBF, 0);

	if (argc >= 3 && strcmp(argv[1], "child") == 0) {
		return child_role(argv[2], -1);
	}

	// f35: announce the pid FIRST (the host sampler needs it) and run the real-service case before the ladder, so
	// the host has the whole 30-second hold to sample the SERVICE child's fd table with targets and flags.
	printf("FIR psfa SELF-PID=%d\n", (int)getpid());
	fflush(stdout);
	{
		const char* svc = "/Volumes/SystemRoot/home/ilyagulya/work/local-target/xcode-26.3/Xcode.app/Contents/"
			"SharedFrameworks/SwiftBuild.framework/Versions/A/PlugIns/SWBBuildService.bundle/Contents/MacOS/"
			"SWBBuildService";
		if (access(svc, X_OK) == 0) {
			spawn_and_hold("real-service", svc);
		} else {
			printf("FIR psfa real-service SKIPPED path-not-executable errno=%d\n", errno);
			fflush(stdout);
		}
	}

	char self[4096];
	memset(self, 0, sizeof(self));
	ssize_t n = readlink("/proc/self/exe", self, sizeof(self) - 1);
	if (n <= 0) {
		// fall back to argv[0]: the harness runs the staged /private/var/tmp/<name>.bin
		snprintf(self, sizeof(self), "%s", argv[0]);
	}
	printf("FIR psfa start self=%s\n", self);
	fflush(stdout);

	// A BUNDLE-LIKE EXEC TARGET: the service is not a loose binary but
	// `…/SWBBuildService.bundle/Contents/MacOS/SWBBuildService`, so the ladder runs one case against a copy of
	// this fixture staged at that shape. Built in-guest with plain read/write, bounded (one copy).
	char bundle_path[4096];
	snprintf(bundle_path, sizeof(bundle_path), "/private/var/tmp/FakeSWB.bundle/Contents/MacOS/FakeSWB");
	int have_bundle = 0;
	{
		int srcd = open(self, O_RDONLY);
		if (srcd >= 0) {
			char d1[256], d2[256];
			snprintf(d1, sizeof(d1), "/private/var/tmp/FakeSWB.bundle");
			snprintf(d2, sizeof(d2), "/private/var/tmp/FakeSWB.bundle/Contents");
			mkdir(d1, 0755);
			mkdir(d2, 0755);
			mkdir("/private/var/tmp/FakeSWB.bundle/Contents/MacOS", 0755);
			int dstd = open(bundle_path, O_WRONLY | O_CREAT | O_TRUNC, 0755);
			if (dstd >= 0) {
				char buf[65536];
				ssize_t got;
				while ((got = read(srcd, buf, sizeof(buf))) > 0) {
					if (write(dstd, buf, got) != got) break;
				}
				close(dstd);
				have_bundle = (access(bundle_path, X_OK) == 0);
			}
			close(srcd);
		}
	}
	printf("FIR psfa start bundle_target=%s have_bundle=%d\n", bundle_path, have_bundle);
	fflush(stdout);

	const char* bpath = have_bundle ? bundle_path : self;
	const char* cdir = "/private/var/tmp";

	// The ladder. `plain` is the previously passing control; `svc-shape` is the service's own shape; the rest
	// drop exactly one element each, which is the bisect.
	run_case("plain", self, ACT_DUP2, A_CLOEXEC, NULL);
	run_case("svc-shape", bpath, ACT_ALL, A_ALL, cdir);
	run_case("svc-nocloexec", bpath, ACT_ALL, (A_ALL & ~A_CLOEXEC), cdir);
	run_case("svc-nochdir", bpath, (ACT_ALL & ~ACT_CHDIR), A_ALL, cdir);
	run_case("svc-nosigdef", bpath, ACT_ALL, (A_ALL & ~A_SETSIGDEF), cdir);
	run_case("svc-nosigmask", bpath, ACT_ALL, (A_ALL & ~A_SETSIGMASK), cdir);
	run_case("svc-nopgroup", bpath, ACT_ALL, (A_ALL & ~A_SETPGROUP), cdir);
	run_case("svc-noattrs", bpath, ACT_ALL, 0, cdir);

	printf("FIR psfa summary ladder_done=1\n");
	printf("PSFA_DONE\n");
	fflush(stdout);
	return 0;
}
