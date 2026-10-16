/*
 * startup_stream_purity_guest.c -- a guest process's startup must not write to its own redirected stdout or
 * stderr.
 *
 * WHY. MEASURED 2026-10-16 (evidence/ios-ci-rung): sys_bsdthread_register printed an unconditional
 * registration record with __simple_printf, which writes to the process's OWN fd 1, so a guest command
 * started with `> file` wrote the record INTO that file: `cat > contents.xcworkspacedata <<EOF` produced
 * 208 bytes whose first line was `[reg pid=... tsd_offset=...]`, and Xcode's project loader then refused
 * the file ("Unable to read project ... Could not open workspace file at ..."). The same mechanism put the
 * line where a harness expected a value (`installed-sha=[reg pid=...]`). The record is now compile-time
 * gated off in a production build; this fixture is the behaviour that keeps it off.
 *
 * WHAT IT ASSERTS. The child is a real, freshly EXEC'd guest process (`/bin/bash -c 'printf ...'`) with
 * fd 1 and fd 2 redirected to files. The child writes exactly one line to stdout and nothing to stderr, so:
 *   * stdout must be exactly those bytes -- nothing may be prepended or appended;
 *   * stderr must be EMPTY -- any byte there came from the runtime, not from the child.
 * The fork-without-exec shape cannot catch this class (it copies an already-registered process), so the
 * child is always an exec.
 *
 * RED/GREEN. The verdict line `SSP_DONE` is printed only when both assertions hold, so the CTest
 * OK_MARKER check fails on RED while the diagnostic lines below still name what leaked.
 *
 * Output (each line on its own line so an exact-line marker check is unambiguous):
 *   SSP child=spawned stdout-bytes=N stderr-bytes=M
 *   SSP leak stdout=<escaped>            (only when stdout is not exact)
 *   SSP leak stderr=<escaped>            (only when stderr is not empty)
 *   SSP_DONE                             (GREEN only)
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHILD_LINE "SSP-CHILD-OUT\n"

static const char* g_out;
static const char* g_err;

static void show_escaped(const char* tag, const char* path)
{
	int fd = open(path, O_RDONLY);
	char buf[256];
	ssize_t n, i;

	printf("SSP leak %s=", tag);
	if (fd < 0) {
		printf("<unreadable errno=%d(%s)>\n", errno, strerror(errno));
		return;
	}
	n = read(fd, buf, sizeof buf);
	close(fd);
	if (n <= 0) {
		printf("<empty-or-error n=%zd>\n", n);
		return;
	}
	putchar('"');
	for (i = 0; i < n; i++) {
		unsigned char c = (unsigned char) buf[i];
		if (c == '\n')
			fputs("\\n", stdout);
		else if (c == '"' || c == '\\')
			printf("\\%c", c);
		else if (c >= 32 && c < 127)
			putchar(c);
		else
			printf("\\x%02x", c);
	}
	printf("\" (%zd byte(s))\n", n);
}

static ssize_t file_size(const char* path)
{
	struct stat st;
	if (stat(path, &st) != 0)
		return -1;
	return (ssize_t) st.st_size;
}

int main(void)
{
	char dir[128];
	static char out[128], err[128];
	pid_t pid;
	int status = 0, ok_out, ok_err;

	snprintf(dir, sizeof dir, "/private/var/tmp/ssp-XXXXXX");
	if (mkdtemp(dir) == NULL) {
		printf("SSP mkdtemp failed errno=%d(%s)\n", errno, strerror(errno));
		return 1;
	}
	snprintf(out, sizeof out, "%s/child.out", dir);
	snprintf(err, sizeof err, "%s/child.err", dir);
	g_out = out;
	g_err = err;

	pid = fork();
	if (pid < 0) {
		printf("SSP fork failed errno=%d(%s)\n", errno, strerror(errno));
		return 1;
	}
	if (pid == 0) {
		int fd;
		char* const argv[] = { (char*) "/bin/bash", (char*) "-c",
			(char*) "printf 'SSP-CHILD-OUT\\n'", NULL };
		fd = open(g_out, O_WRONLY | O_CREAT | O_TRUNC, 0644);
		if (fd < 0 || dup2(fd, 1) != 1)
			_exit(9);
		fd = open(g_err, O_WRONLY | O_CREAT | O_TRUNC, 0644);
		if (fd < 0 || dup2(fd, 2) != 2)
			_exit(9);
		execv("/bin/bash", argv);
		_exit(8); /* exec failed: the child never ran, so this run is not evidence */
	}
	if (waitpid(pid, &status, 0) < 0) {
		printf("SSP waitpid failed errno=%d(%s)\n", errno, strerror(errno));
		return 1;
	}

	printf("SSP child=spawned exit=%d stdout-bytes=%zd stderr-bytes=%zd\n", WIFEXITED(status) ? WEXITSTATUS(status) : -1,
	       file_size(g_out), file_size(g_err));

	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		printf("SSP child did not exit 0; run is not evidence\n");
		show_escaped("stdout", g_out);
		show_escaped("stderr", g_err);
		return 1;
	}

	ok_out = file_size(g_out) == (ssize_t) strlen(CHILD_LINE);
	if (ok_out) {
		char buf[64];
		int fd = open(g_out, O_RDONLY);
		ssize_t n = (fd >= 0) ? read(fd, buf, sizeof buf) : -1;
		if (fd >= 0)
			close(fd);
		ok_out = (n == (ssize_t) strlen(CHILD_LINE)) && (memcmp(buf, CHILD_LINE, strlen(CHILD_LINE)) == 0);
	}
	ok_err = file_size(g_err) == 0;

	if (!ok_out) {
		printf("SSP stdout is not exactly the child's own bytes\n");
		show_escaped("stdout", g_out);
	}
	if (!ok_err) {
		printf("SSP stderr is not empty although the child wrote nothing to it\n");
		show_escaped("stderr", g_err);
	}

	if (ok_out && ok_err)
		printf("SSP_DONE\n");
	return (ok_out && ok_err) ? 0 : 1;
}
