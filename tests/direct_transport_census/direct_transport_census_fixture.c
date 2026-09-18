/* Guest-side fixture for the direct transport census.
 *
 * This program runs INSIDE a bootstrapped Darling prefix and produces the two
 * workloads the census windows are measured across:
 *
 *   noop                         do nothing and exit 0.  The exec target of the
 *                                process-lifecycle window's child, so the window
 *                                needs no guest program beyond this fixture.
 *   hot T R PRE HOLD POST        T guest threads, each doing ONE mach_host_self()
 *                                trap and then parking on a start gate, so the
 *                                host can count descriptors with every thread's
 *                                transport established; then each thread runs R
 *                                more mach_host_self() traps.  HOLD is the gate
 *                                hold in milliseconds.  This is the hot mach-trap
 *                                window (the fixture's request class is
 *                                mach_host_self, which is the same class the
 *                                relay integration proof measures).
 *   forkexec N PRE POST          N cycles of fork() + exec of this fixture in
 *                                `noop` mode + waitpid(), with one extra pipe
 *                                held open across every fork so the fork RPC
 *                                carries more than stdio.
 *
 * Protocol (one-directional; the host never writes to the guest):
 *
 *   DTC hot pid=<pid>            / DTC forkexec pid=<pid> exe=<path>
 *   DTC fds stage=<name> ...     a guest-visible descriptor scan
 *   DTC_BARRIER pre              (then hold PRE ms)
 *   ...workload...
 *   DTC_BARRIER threads-live     (hot mode only; then hold HOLD ms)
 *   DTC_BARRIER action-done      (then hold POST ms)
 *   DTC_BARRIER done
 *
 * The host samples the server's stat socket and /proc/<pid>/fd while the guest
 * is parked in a sleep, and refuses a sample whose window has already closed.
 * The guest process' getpid() equals the host pid of the mldr process hosting
 * it, which is what makes the outside view possible.
 *
 * The guest-visible scan uses fcntl(F_GETFD) over [0, min(getdtablesize(),
 * DTC_FD_SCAN_CAP)); the loader's own descriptors are hidden from the guest by
 * the descriptor guard, so the host's /proc/<pid>/fd count (taken by the
 * runner) is the authority for the process' true descriptor total and this scan
 * is the authority for what the guest itself can see.
 *
 * Compile inside the guest:
 *   /Library/Developer/CommandLineTools/usr/bin/clang \
 *     -isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk -O1 \
 *     -Wno-deprecated-declarations -o <bin> direct_transport_census_fixture.c
 */

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include <mach/mach.h>

#define DTC_MAX_THREADS 64
#define DTC_MAX_LISTED_FDS 64
#define DTC_FD_SCAN_CAP 4096

static void emit(const char *fmt, ...)
{
	va_list ap;

	va_start(ap, fmt);
	vprintf(fmt, ap);
	va_end(ap);
	fflush(stdout);
}

static void barrier(const char *phase)
{
	emit("DTC_BARRIER %s\n", phase);
}

static void nap_ms(long ms)
{
	struct timespec ts;

	if (ms <= 0) {
		return;
	}
	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (ms % 1000) * 1000000L;
	while (nanosleep(&ts, NULL) != 0 && errno == EINTR) {
	}
}

static double mono_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1000000.0;
}

/* Second clock source: the guest's monotonic clock has been observed to step
 * backwards across a long workload, so every elapsed time is reported from both
 * sources and a disagreement is visible rather than hidden. */
static double wall_ms(void)
{
	struct timeval tv;

	gettimeofday(&tv, NULL);
	return (double)tv.tv_sec * 1000.0 + (double)tv.tv_usec / 1000.0;
}

/* The descriptors the guest itself can see, over a bounded prefix of the
 * advertised range. */
static int visible_fd_scan(int *numbers, int max_numbers, int *scanned)
{
	int top = getdtablesize();
	int cap = top < DTC_FD_SCAN_CAP ? top : DTC_FD_SCAN_CAP;
	int count = 0;
	int fd;

	if (cap < 0) {
		cap = 0;
	}
	for (fd = 0; fd < cap; ++fd) {
		if (fcntl(fd, F_GETFD) == -1) {
			continue;
		}
		if (numbers != NULL && count < max_numbers) {
			numbers[count] = fd;
		}
		++count;
	}
	if (scanned != NULL) {
		*scanned = cap;
	}
	return count;
}

static void emit_fds(const char *stage)
{
	static int numbers[DTC_MAX_LISTED_FDS];
	int scanned = 0;
	int top = getdtablesize();
	int count = visible_fd_scan(numbers, DTC_MAX_LISTED_FDS, &scanned);
	int shown = count < DTC_MAX_LISTED_FDS ? count : DTC_MAX_LISTED_FDS;
	char list[512];
	size_t off = 0;
	int i;

	list[0] = '\0';
	for (i = 0; i < shown; ++i) {
		off += (size_t)snprintf(list + off, sizeof(list) - off, "%s%d",
		    i == 0 ? "" : ",", numbers[i]);
		if (off >= sizeof(list)) {
			break;
		}
	}
	emit("DTC fds stage=%s guest_visible=%d getdtablesize=%d scan_range=[0,%d) "
	    "list=[%s] guest_visible_capped=%d\n",
	    stage, count, top, scanned, list,
	    count > DTC_MAX_LISTED_FDS ? 1 : 0);
}

/* ------------------------------------------------------------- hot window */

struct hot_ctx {
	pthread_mutex_t lock;
	pthread_cond_t gate;
	int requests;
	int started;
	int gate_open;
	int rpc_ok;
	int rpc_failed;
};

struct hot_thread {
	struct hot_ctx *ctx;
	int index;
};

static void *hot_thr_main(void *arg)
{
	struct hot_thread *self = arg;
	struct hot_ctx *ctx = self->ctx;
	int ok = 0;
	int failed = 0;
	int i;

	/* One trap before announcing: this is what gives the thread its own
	 * transport (and, on a ring build, its lane). */
	if (mach_host_self() != MACH_PORT_NULL) {
		++ok;
	} else {
		++failed;
	}

	pthread_mutex_lock(&ctx->lock);
	++ctx->started;
	pthread_cond_broadcast(&ctx->gate);
	while (!ctx->gate_open) {
		pthread_cond_wait(&ctx->gate, &ctx->lock);
	}
	pthread_mutex_unlock(&ctx->lock);

	for (i = 0; i < ctx->requests; ++i) {
		if (mach_host_self() != MACH_PORT_NULL) {
			++ok;
		} else {
			++failed;
		}
	}

	pthread_mutex_lock(&ctx->lock);
	ctx->rpc_ok += ok;
	ctx->rpc_failed += failed;
	pthread_mutex_unlock(&ctx->lock);
	return NULL;
}

static int mode_hot(int requested, int per_thread, long pre_ms, long hold_ms,
    long post_ms)
{
	pthread_t threads[DTC_MAX_THREADS];
	struct hot_thread args[DTC_MAX_THREADS];
	struct hot_ctx ctx;
	int created = 0;
	int nominal;
	int actual;
	int i;
	double t0;
	double t1;
	double w0;
	double w1;

	if (requested < 0) {
		requested = 0;
	}
	if (requested > DTC_MAX_THREADS) {
		requested = DTC_MAX_THREADS;
	}
	if (per_thread < 0) {
		per_thread = 0;
	}
	nominal = requested * per_thread;

	memset(&ctx, 0, sizeof(ctx));
	pthread_mutex_init(&ctx.lock, NULL);
	pthread_cond_init(&ctx.gate, NULL);
	ctx.requests = per_thread;

	emit("DTC hot pid=%d\n", (int)getpid());
	emit_fds("baseline");
	barrier("pre");
	nap_ms(pre_ms);

	for (i = 0; i < requested; ++i) {
		args[i].ctx = &ctx;
		args[i].index = i;
		if (pthread_create(&threads[i], NULL, hot_thr_main,
		    &args[i]) != 0) {
			break;
		}
		++created;
	}

	pthread_mutex_lock(&ctx.lock);
	while (ctx.started < created) {
		pthread_cond_wait(&ctx.gate, &ctx.lock);
	}
	pthread_mutex_unlock(&ctx.lock);

	emit("DTC hot threads_created=%d\n", created);
	emit_fds("threads_live");
	barrier("threads-live");
	nap_ms(hold_ms);

	t0 = mono_ms();
	w0 = wall_ms();
	pthread_mutex_lock(&ctx.lock);
	ctx.gate_open = 1;
	pthread_cond_broadcast(&ctx.gate);
	pthread_mutex_unlock(&ctx.lock);
	for (i = 0; i < created; ++i) {
		pthread_join(threads[i], NULL);
	}
	t1 = mono_ms();
	w1 = wall_ms();
	actual = created * per_thread;

	emit("DTC hot threads_requested=%d threads_created=%d "
	    "requests_per_thread=%d nominal_requests=%d actual_requests=%d "
	    "rpc_ok=%d rpc_failed=%d elapsed_mono_ms=%.1f elapsed_wall_ms=%.1f "
	    "us_per_call=%.3f clock_disagree=%d\n",
	    requested, created, per_thread, nominal, actual, ctx.rpc_ok,
	    ctx.rpc_failed, t1 - t0, w1 - w0,
	    actual > 0 ? (w1 - w0) * 1000.0 / (double)actual : 0.0,
	    ((t1 - t0 < 0.0) != (w1 - w0 < 0.0)) ? 1 : 0);

	emit_fds("after_join");
	barrier("action-done");
	nap_ms(post_ms);
	barrier("done");
	pthread_cond_destroy(&ctx.gate);
	pthread_mutex_destroy(&ctx.lock);
	return 0;
}

/* -------------------------------------------------- process-lifecycle window */

static const char *exe_path = NULL;

static int mode_forkexec(int iterations, long pre_ms, long post_ms)
{
	int pfd[2] = { -1, -1 };
	int extra_held = 0;
	int fork_ok = 0;
	int fork_failed = 0;
	int wait_failed = 0;
	int child_exit_zero = 0;
	int child_exit_nonzero = 0;
	int child_exec_failed = 0;
	int child_signaled = 0;
	int i;
	double t0;
	double t1;
	double w0;
	double w1;

	if (iterations < 0) {
		iterations = 0;
	}
	if (exe_path == NULL || exe_path[0] != '/') {
		emit("DTC forkexec ERROR exe_must_be_absolute exe=%s\n",
		    exe_path == NULL ? "(null)" : exe_path);
		return 1;
	}

	emit("DTC forkexec pid=%d exe=%s\n", (int)getpid(), exe_path);
	if (pipe(pfd) == 0) {
		/* Held open across every fork, so the fork RPC carries more than
		 * the three stdio descriptors. */
		extra_held = 2;
	} else {
		pfd[0] = pfd[1] = -1;
	}
	emit_fds("baseline");
	barrier("pre");
	nap_ms(pre_ms);

	t0 = mono_ms();
	w0 = wall_ms();
	for (i = 0; i < iterations; ++i) {
		pid_t pid = fork();

		if (pid < 0) {
			++fork_failed;
			continue;
		}
		if (pid == 0) {
			execl(exe_path, exe_path, "noop", (char *)NULL);
			_exit(127);
		}
		++fork_ok;

		{
			int status = 0;

			if (waitpid(pid, &status, 0) < 0) {
				++wait_failed;
				continue;
			}
			if (WIFEXITED(status)) {
				if (WEXITSTATUS(status) == 0) {
					++child_exit_zero;
				} else {
					++child_exit_nonzero;
					if (WEXITSTATUS(status) == 127) {
						++child_exec_failed;
					}
				}
			} else if (WIFSIGNALED(status)) {
				++child_signaled;
			}
		}
	}
	t1 = mono_ms();
	w1 = wall_ms();

	if (pfd[0] >= 0) {
		close(pfd[0]);
		close(pfd[1]);
	}

	emit("DTC forkexec iterations=%d fork_ok=%d fork_failed=%d "
	    "wait_failed=%d child_exit_zero=%d child_exit_nonzero=%d "
	    "child_exit_127_exec_failed=%d child_signaled=%d "
	    "extra_descriptors_held=%d elapsed_mono_ms=%.1f elapsed_wall_ms=%.1f "
	    "ms_per_cycle_wall=%.3f clock_disagree=%d\n",
	    iterations, fork_ok, fork_failed, wait_failed, child_exit_zero,
	    child_exit_nonzero, child_exec_failed, child_signaled,
	    extra_held, t1 - t0, w1 - w0,
	    fork_ok > 0 ? (w1 - w0) / (double)fork_ok : 0.0,
	    ((t1 - t0 < 0.0) != (w1 - w0 < 0.0)) ? 1 : 0);

	emit_fds("after_loop");
	barrier("action-done");
	nap_ms(post_ms);
	barrier("done");
	return 0;
}

/* ------------------------------------------------------------------ */

static int usage(void)
{
	fprintf(stderr,
	    "usage: %s noop\n"
	    "       %s hot THREADS REQUESTS_PER_THREAD PRE_MS HOLD_MS POST_MS\n"
	    "       %s forkexec ITERATIONS PRE_MS POST_MS\n",
	    "direct_transport_census_fixture",
	    "direct_transport_census_fixture",
	    "direct_transport_census_fixture");
	return 2;
}

int main(int argc, char **argv)
{
	const char *mode;

	setvbuf(stdout, NULL, _IOLBF, 0);
	if (argc < 2) {
		return usage();
	}
	mode = argv[1];
	exe_path = argv[0];

	if (strcmp(mode, "noop") == 0 && argc == 2) {
		return 0;
	}
	if (strcmp(mode, "hot") == 0 && argc == 7) {
		return mode_hot(atoi(argv[2]), atoi(argv[3]), atol(argv[4]),
		    atol(argv[5]), atol(argv[6]));
	}
	if (strcmp(mode, "forkexec") == 0 && argc == 5) {
		return mode_forkexec(atoi(argv[2]), atol(argv[3]),
		    atol(argv[4]));
	}
	return usage();
}
