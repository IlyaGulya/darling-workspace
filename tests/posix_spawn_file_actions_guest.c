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

static void run_case(const char* tag, const char* path, int with_actions, struct case_result* out)
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
	if (with_actions) {
		posix_spawn_file_actions_addclose(&fa, 0);
		posix_spawn_file_actions_adddup2(&fa, p_in[0], 0);
		posix_spawn_file_actions_addclose(&fa, 1);
		posix_spawn_file_actions_adddup2(&fa, p_out[1], 1);
	}

	posix_spawnattr_t attr;
	posix_spawnattr_init(&attr);
	short flags = POSIX_SPAWN_CLOEXEC_DEFAULT;
	posix_spawnattr_setflags(&attr, flags);

	char* argv[] = {(char*)path, (char*)"child", (char*)tag, NULL};
	pid_t pid = -1;
	out->spawn_rc = posix_spawn(&pid, path, &fa, &attr, argv, environ);
	posix_spawn_file_actions_destroy(&fa);
	posix_spawnattr_destroy(&attr);

	printf("FIR psfa %s spawn rc=%d pid=%d with_actions=%d flags=CLOEXEC_DEFAULT path=%s\n", tag,
		out->spawn_rc, (int)pid, with_actions, path);
	fflush(stdout);
	if (out->spawn_rc != 0) return;

	// what the CHILD's own descriptor table says, from the guest side: /proc is the host's, and guest pids are
	// the host pids here, so this is the same question the harness asked of SWBBuildService
	char fdsnap[512];
	int used = 0;
	fdsnap[0] = 0;
	for (int fd = 0; fd <= 3; fd++) {
		char link[128], tgt[512];
		snprintf(link, sizeof(link), "/proc/%d/fd/%d", (int)pid, fd);
		ssize_t r = readlink(link, tgt, sizeof(tgt) - 1);
		if (r < 0) { used += snprintf(fdsnap + used, sizeof(fdsnap) - used, "%d=absent ", fd); }
		else { tgt[r] = 0; used += snprintf(fdsnap + used, sizeof(fdsnap) - used, "%d=%s ", fd, tgt); }
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
	out->parent_read = 0;
	memset(out->parent_text, 0, sizeof(out->parent_text));
	for (int i = 0; i < 600; i++) {
		ssize_t got = read(p_out[0], out->parent_text + out->parent_read,
			sizeof(out->parent_text) - 1 - out->parent_read);
		if (got > 0) { out->parent_read += (long)got; if (out->parent_read >= (long)sizeof(out->parent_text) - 1) break; continue; }
		if (got == 0) break;  // EOF: every writer closed, the run is done
		if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) break;
		struct timespec ts = {0, 5 * 1000 * 1000};
		nanosleep(&ts, NULL);
	}
	close(p_out[0]);

	int status = 0;
	waitpid(pid, &status, 0);
	out->child_status = status;

	printf("FIR psfa %s parent_read=%ld child_status=%d pipe_text=\"%s\"\n", tag, out->parent_read,
		out->child_status, out->parent_text);
	fflush(stdout);
}

int main(int argc, char** argv)
{
	alarm(90);
	// The control case spawns a child that CANNOT receive the ping (no actions), so the parent's write to the
	// pipe may find no reader: that is a legitimate observed outcome ("the child cannot be reached"), not a
	// reason for the fixture to die before the remaining cases run (measured in the v3 run: SIGPIPE killed it
	// right after the control spawn).
	signal(SIGPIPE, SIG_IGN);
	setvbuf(stdout, NULL, _IOLBF, 0);

	if (argc >= 3 && strcmp(argv[1], "child") == 0) {
		return child_role(argv[2], -1);
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

	char alt[4096];
	snprintf(alt, sizeof(alt), "/Volumes/SystemRoot%s", self);
	printf("FIR psfa start altname=%s\n", alt);

	struct case_result r;

	run_case("actions", self, 1, &r);
	run_case("control", self, 0, &r);
	run_case("altname", alt, 1, &r);

	printf("FIR psfa summary actions_done=1 control_done=1 altname_done=1\n");
	printf("PSFA_DONE\n");
	fflush(stdout);
	return 0;
}
