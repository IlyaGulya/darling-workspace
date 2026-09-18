/* Guest-side fixture for the relay integration proof.
 *
 * This program runs INSIDE a bootstrapped Darling prefix and measures the
 * premises the recommended relay architecture depends on, against the real
 * product runtime (mldr loader, darlingserver, per-thread RPC sockets, the
 * ring transport, the descriptor guard).  It never changes product source and
 * it never decides a verdict: it prints measurements, and the host runner
 * (run-relay-integration-proof.sh) owns the must-pass/fail decision.
 *
 * Modes (argv[1]):
 *
 *   defect                     I1: advertised descriptor limits plus a
 *                              top-down dup2 walk over the last window of the
 *                              advertised range.
 *   hidden       PRE POST      I2: pid + the descriptors the guest itself can
 *                              enumerate (fcntl F_GETFD over the advertised
 *                              range and a /dev/fd listing).
 *   threads N    PRE POST      I3: create N threads (N <= 64), each performing
 *                              one mach_host_self() RPC, and hold them alive.
 *   ring W       PRE POST      I4: W mach_host_self() warm-up calls, i.e. the
 *                              calls that attach the ring (the runner passes
 *                              36).
 *   scm          PRE POST      I5: SCM_RIGHTS descriptor transfer over an
 *                              AF_UNIX socketpair plus the proof that the
 *                              received descriptor works and the sender-side
 *                              descriptor is untouched.
 *   close-range  PRE POST      I6: close every descriptor from 3 up to the
 *                              advertised limit, then use the loader again.
 *                              This mode adds one extra barrier: it uses the
 *                              loader first, then prints RI_BARRIER armed and
 *                              holds PRE ms so the host can sample the
 *                              descriptor set that is about to be closed.
 *   rpc N        PRE POST      I7: N mach_host_self() RPCs of pure transport
 *                              workload.
 *   concurrent T R [PRE POST [KIND [DISCIPLINE [STAGGER_US]]]]
 *                              I9/I10: T threads, each performing R requests in
 *                              a loop, released from a start gate and joined,
 *                              so the host can ask the server's own counters
 *                              how many requests one server wake drained (T x R
 *                              is the nominal request total the host divides
 *                              by).  PRE and POST are optional here and default
 *                              to 0.  KIND picks the request class: `mach` (the
 *                              default) is mach_host_self(), `bsd` is
 *                              setuid(getuid()), a BSD syscall whose Darling
 *                              emulation issues one dserver uidgid RPC per
 *                              call.  DISCIPLINE is `simultaneous` (the
 *                              default, one gate release for every thread) or
 *                              `staggered`, which releases the threads
 *                              STAGGER_US microseconds apart (default 250) so
 *                              their requests do not align.  The workload line
 *                              prints the chosen kind and discipline, the
 *                              requested spacing and the achieved release
 *                              spread.
 *
 * PRE and POST are millisecond hold windows.  The protocol is one-directional
 * and needs no host-to-guest channel (defect prints no barrier at all, and
 * close-range adds an "armed" barrier between pre and action-done).  The
 * fixture flushes
 *
 *   RI_PID pid=<guest pid>
 *   RI_BARRIER pre            (then hold PRE ms)
 *   ...action + measurement lines...
 *   RI_BARRIER action-done    (then hold POST ms)
 *   RI_BARRIER done
 *
 * so the host can read /proc/<pid>/fd, /proc/<pid>/status and /proc/<pid>/io
 * while the guest is parked in a sleep.  A guest process' getpid() equals the
 * host pid of the mldr process that hosts it, which is what makes the outside
 * view possible.  Every line is flushed so the host observes the barrier as
 * soon as it happens; the host refuses to trust a sample taken after the next
 * barrier has already appeared.
 *
 * Compile inside the guest:
 *   /Library/Developer/CommandLineTools/usr/bin/clang \
 *     -isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk -O1 \
 *     -Wno-deprecated-declarations -o <bin> relay_integration_fixture.c
 * (-Wno-deprecated-declarations is for the one deliberate raw syscall() call
 * that reports the getdtablesize syscall number's answer.)
 */

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#include <mach/mach.h>

#define RI_MAX_THREADS 64
#define RI_MAX_VISIBLE_FDS 256
#define RI_MAX_DEVFD_ENTRIES 200000
#define RI_PROBE_WINDOW 8

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
	emit("RI_BARRIER %s\n", phase);
}

static void nap_ms(long ms)
{
	struct timespec ts;

	if (ms <= 0) {
		return;
	}
	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (ms % 1000) * 1000000L;
	while (nanosleep(&ts, &ts) != 0 && errno == EINTR) {
	}
}

static double mono_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (double)ts.tv_sec * 1000.0 + (double)ts.tv_nsec / 1000000.0;
}

/* Second clock source: the guest's monotonic clock has been observed to step
 * backwards across a long close() sweep, so the close sweep is timed with both
 * and the disagreement is reported rather than hidden. */
static double wall_ms(void)
{
	struct timeval tv;

	gettimeofday(&tv, NULL);
	return (double)tv.tv_sec * 1000.0 + (double)tv.tv_usec / 1000.0;
}

static int open_fd_count(int *visible_numbers, int max_numbers)
{
	int limit = getdtablesize();
	int count = 0;
	int fd;

	for (fd = 0; fd < limit; ++fd) {
		if (fcntl(fd, F_GETFD) == -1) {
			continue;
		}
		if (visible_numbers != NULL && count < max_numbers) {
			visible_numbers[count] = fd;
		}
		++count;
	}
	return count;
}

static void describe_visible_fds(const char *tag)
{
	static int numbers[RI_MAX_VISIBLE_FDS];
	int count = open_fd_count(numbers, RI_MAX_VISIBLE_FDS);
	int i;
	int shown = count < RI_MAX_VISIBLE_FDS ? count : RI_MAX_VISIBLE_FDS;
	int limit = getdtablesize();
	char list[1024];
	size_t off = 0;

	list[0] = '\0';
	for (i = 0; i < shown; ++i) {
		off += (size_t)snprintf(list + off, sizeof(list) - off, "%s%d",
		    i == 0 ? "" : ",", numbers[i]);
		if (off >= sizeof(list)) {
			break;
		}
	}
	emit("%s guest_visible_count=%d scan_range=[0,%d) list=[%s]%s\n", tag,
	    count, limit, list,
	    count > RI_MAX_VISIBLE_FDS ? " list_truncated=1" : "");
}

static void describe_fd_dir(const char *tag, const char *path)
{
	DIR *dir = opendir(path);
	struct dirent *ent;
	int raw = 0;
	int count = 0;
	int capped = 0;
	int err = 0;
	char list[512];
	size_t off = 0;

	list[0] = '\0';
	if (dir == NULL) {
		emit("%s dir=%s listing=unavailable errno=%d(%s)\n", tag, path, errno,
		    strerror(errno));
		return;
	}
	while ((ent = readdir(dir)) != NULL) {
		++raw;
		if (strcmp(ent->d_name, ".") == 0 || strcmp(ent->d_name, "..") == 0) {
			continue;
		}
		if (count < 64 && off < sizeof(list) - 8) {
			off += (size_t)snprintf(list + off, sizeof(list) - off,
			    "%s%s", count == 0 ? "" : ",", ent->d_name);
		}
		++count;
		if (count >= RI_MAX_DEVFD_ENTRIES) {
			capped = 1;
			break;
		}
	}
	err = errno;
	closedir(dir);
	emit("%s dir=%s raw_entries=%d entries=%d list=[%s] errno=%d%s\n", tag, path,
	    raw, count, list, err, capped ? " capped=1" : "");
}

static void describe_fd_dir_via_ls(const char *tag, const char *path)
{
	char cmd[256];
	FILE *p;
	char line[128];
	int count = 0;
	int capped = 0;
	int status = 0;
	char list[512];
	size_t off = 0;

	list[0] = '\0';
	snprintf(cmd, sizeof(cmd), "/bin/ls -1 %s 2>&1", path);
	p = popen(cmd, "r");
	if (p == NULL) {
		emit("%s dir=%s via=ls listing=unavailable errno=%d(%s)\n", tag, path,
		    errno, strerror(errno));
		return;
	}
	while (fgets(line, sizeof(line), p) != NULL) {
		line[strcspn(line, "\n")] = '\0';
		if (line[0] == '\0') {
			continue;
		}
		if (count < 64 && off < sizeof(list) - 8) {
			off += (size_t)snprintf(list + off, sizeof(list) - off,
			    "%s%s", count == 0 ? "" : ",", line);
		}
		++count;
		if (count >= RI_MAX_DEVFD_ENTRIES) {
			capped = 1;
			break;
		}
	}
	status = pclose(p);
	emit("%s dir=%s via=ls entries=%d list=[%s] status=%d%s\n", tag, path, count,
	    list, status, capped ? " capped=1" : "");
}

static void describe_dev_fd(const char *tag)
{
	describe_fd_dir(tag, "/dev/fd");
	describe_fd_dir(tag, "/proc/self/fd");
	describe_fd_dir_via_ls(tag, "/dev/fd");
}

/* ---------------------------------------------------------------- I1 */

static int probe_dup2(int target)
{
	int rc = dup2(0, target);

	if (rc == target) {
		return target;
	}
	return -1;
}

static int mode_defect(void)
{
	int top = getdtablesize();
	long open_max = sysconf(_SC_OPEN_MAX);
	struct rlimit rl;
	long raw = -1;
	int raw_errno = 0;
	int probes[RI_PROBE_WINDOW];
	int probe_errno[RI_PROBE_WINDOW];
	int highest = -1;
	int top_minus_one = -1;
	int i;

	memset(&rl, 0, sizeof(rl));
	if (getrlimit(RLIMIT_NOFILE, &rl) != 0) {
		rl.rlim_cur = (rlim_t)-1;
		rl.rlim_max = (rlim_t)-1;
	}

	errno = 0;
#ifdef SYS_getdtablesize
	raw = syscall(SYS_getdtablesize);
#else
	raw = syscall(89); /* XNU SYS_getdtablesize */
#endif
	raw_errno = errno;

	emit("I1 limits getdtablesize=%d sysconf_open_max=%ld "
	    "syscall_getdtablesize=%ld syscall_errno=%d rlim_cur=%llu rlim_max=%llu\n",
	    top, open_max, raw, raw_errno, (unsigned long long)rl.rlim_cur,
	    (unsigned long long)rl.rlim_max);

	for (i = 0; i < RI_PROBE_WINDOW; ++i) {
		int target = top - i;

		errno = 0;
		probes[i] = probe_dup2(target);
		probe_errno[i] = errno;
		emit("I1 probe fd=%d result=%d errno=%d(%s)\n", target, probes[i],
		    probe_errno[i], probes[i] == target ? "-" : strerror(probe_errno[i]));
		if (probes[i] == target && highest < 0) {
			highest = target;
		}
	}
	if (top - 1 >= 0) {
		top_minus_one = probes[1] == top - 1 ? 1 : 0;
	}

	/* Close only the descriptors this probe created: they are dups of fd 0
	 * and nothing else can see them. */
	for (i = 0; i < RI_PROBE_WINDOW; ++i) {
		if (probes[i] >= 0) {
			close(probes[i]);
		}
	}

	emit("I1 defect advertised=%d top_minus_one=%d top_minus_one_accepted=%d "
	    "highest_accepted=%d refused_in_window=%d\n",
	    top, top - 1, top_minus_one, highest,
	    highest < 0 ? RI_PROBE_WINDOW : top - highest);
	return 0;
}

/* ---------------------------------------------------------------- I2 */

static int mode_hidden(long pre_ms, long post_ms)
{
	emit("I2 pid=%d\n", (int)getpid());
	barrier("pre");
	nap_ms(pre_ms);

	describe_visible_fds("I2 visible");
	describe_dev_fd("I2 devfd");
	emit("I2 hold advertised=%d\n", getdtablesize());

	barrier("action-done");
	nap_ms(post_ms);
	barrier("done");
	return 0;
}

/* ---------------------------------------------------------------- I3 */

struct thr_ctx {
	pthread_mutex_t lock;
	pthread_cond_t cond;
	int total;
	int started;
	int rpc_ok;
	int rpc_failed;
	int release;
};

static void *thr_main(void *arg)
{
	struct thr_ctx *ctx = arg;
	mach_port_t host = mach_host_self();
	int ok = host != MACH_PORT_NULL;

	pthread_mutex_lock(&ctx->lock);
	if (ok) {
		++ctx->rpc_ok;
	} else {
		++ctx->rpc_failed;
	}
	++ctx->started;
	pthread_cond_broadcast(&ctx->cond);
	while (!ctx->release) {
		pthread_cond_wait(&ctx->cond, &ctx->lock);
	}
	pthread_mutex_unlock(&ctx->lock);
	return NULL;
}

static int mode_threads(int requested, long pre_ms, long post_ms)
{
	pthread_t threads[RI_MAX_THREADS];
	struct thr_ctx ctx;
	int created = 0;
	int i;
	double t0;
	double t1;

	if (requested < 0) {
		requested = 0;
	}
	if (requested > RI_MAX_THREADS) {
		requested = RI_MAX_THREADS;
	}

	memset(&ctx, 0, sizeof(ctx));
	pthread_mutex_init(&ctx.lock, NULL);
	pthread_cond_init(&ctx.cond, NULL);
	ctx.total = requested;

	emit("I3 pid=%d\n", (int)getpid());
	barrier("pre");
	nap_ms(pre_ms);

	t0 = mono_ms();
	for (i = 0; i < requested; ++i) {
		if (pthread_create(&threads[i], NULL, thr_main, &ctx) != 0) {
			break;
		}
		++created;
	}

	pthread_mutex_lock(&ctx.lock);
	while (ctx.started < created) {
		pthread_cond_wait(&ctx.cond, &ctx.lock);
	}
	pthread_mutex_unlock(&ctx.lock);
	t1 = mono_ms();

	emit("I3 threads requested=%d created=%d rpc_ok=%d rpc_failed=%d "
	    "elapsed_ms=%.1f\n", requested, created, ctx.rpc_ok, ctx.rpc_failed,
	    t1 - t0);

	barrier("action-done");
	nap_ms(post_ms);

	pthread_mutex_lock(&ctx.lock);
	ctx.release = 1;
	pthread_cond_broadcast(&ctx.cond);
	pthread_mutex_unlock(&ctx.lock);
	for (i = 0; i < created; ++i) {
		pthread_join(threads[i], NULL);
	}

	pthread_cond_destroy(&ctx.cond);
	pthread_mutex_destroy(&ctx.lock);
	barrier("done");
	return 0;
}

/* ---------------------------------------------------------------- I4 */

static int mode_ring(int warmup, long pre_ms, long post_ms)
{
	int ok = 0;
	int i;
	double t0;
	double t1;

	if (warmup < 0) {
		warmup = 0;
	}

	emit("I4 pid=%d\n", (int)getpid());
	barrier("pre");
	nap_ms(pre_ms);

	t0 = mono_ms();
	for (i = 0; i < warmup; ++i) {
		if (mach_host_self() != MACH_PORT_NULL) {
			++ok;
		}
	}
	t1 = mono_ms();

	emit("I4 ring mach_traps=%d ok=%d elapsed_ms=%.1f us_per_call=%.3f\n", warmup,
	    ok, t1 - t0, warmup > 0 ? (t1 - t0) * 1000.0 / (double)warmup : 0.0);

	barrier("action-done");
	nap_ms(post_ms);
	barrier("done");
	return 0;
}

/* ---------------------------------------------------------------- I5 */

static int mode_scm(long pre_ms, long post_ms)
{
	int sv[2] = { -1, -1 };
	int pfd[2] = { -1, -1 };
	int recvfd = -1;
	int flags_before = -1;
	int flags_after = -1;
	int payload_ok = 0;
	int inode_match = 0;
	int sender_read_ok = 0;
	int sent_ok = 0;
	int recv_ok = 0;
	ssize_t written;
	ssize_t got;
	char buf[16];
	struct stat st_sender;
	struct stat st_recv;
	union {
		struct cmsghdr hdr;
		char buf[CMSG_SPACE(sizeof(int))];
	} cmsg;
	char recvbuf[CMSG_SPACE(sizeof(int)) * 2];
	struct iovec iov;
	struct msghdr msg;
	struct iovec riov;
	struct msghdr rmsg;
	struct cmsghdr *cm;
	struct cmsghdr *rcm;
	char payload[8] = { 'P', 'A', 'Y', 'L', 'O', 'A', 'D', '\0' };

	emit("I5 pid=%d\n", (int)getpid());
	barrier("pre");
	nap_ms(pre_ms);

	memset(buf, 0, sizeof(buf));
	if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) != 0) {
		emit("I5 setup socketpair_failed errno=%d(%s)\n", errno, strerror(errno));
	} else if (pipe(pfd) != 0) {
		emit("I5 setup pipe_failed errno=%d(%s)\n", errno, strerror(errno));
	} else {
		errno = 0;
		flags_before = fcntl(pfd[0], F_GETFD);

		memset(&msg, 0, sizeof(msg));
		memset(&cmsg, 0, sizeof(cmsg));
		iov.iov_base = payload;
		iov.iov_len = 1;
		msg.msg_iov = &iov;
		msg.msg_iovlen = 1;
		msg.msg_control = cmsg.buf;
		msg.msg_controllen = sizeof(cmsg.buf);
		cm = CMSG_FIRSTHDR(&msg);
		cm->cmsg_level = SOL_SOCKET;
		cm->cmsg_type = SCM_RIGHTS;
		cm->cmsg_len = CMSG_LEN(sizeof(int));
		memcpy(CMSG_DATA(cm), &pfd[0], sizeof(int));
		sent_ok = sendmsg(sv[0], &msg, 0) == 1;

		memset(&rmsg, 0, sizeof(rmsg));
		memset(recvbuf, 0, sizeof(recvbuf));
		riov.iov_base = buf;
		riov.iov_len = sizeof(buf);
		rmsg.msg_iov = &riov;
		rmsg.msg_iovlen = 1;
		rmsg.msg_control = recvbuf;
		rmsg.msg_controllen = sizeof(recvbuf);
		got = recvmsg(sv[1], &rmsg, 0);
		recv_ok = got >= 1 ? 1 : 0;
		for (rcm = CMSG_FIRSTHDR(&rmsg); rcm != NULL;
		    rcm = CMSG_NXTHDR(&rmsg, rcm)) {
			if (rcm->cmsg_level == SOL_SOCKET &&
			    rcm->cmsg_type == SCM_RIGHTS) {
				memcpy(&recvfd, CMSG_DATA(rcm), sizeof(int));
			}
		}

		if (recvfd >= 0) {
			written = write(pfd[1], payload, sizeof(payload));
			if (written == (ssize_t)sizeof(payload)) {
				memset(buf, 0, sizeof(buf));
				got = read(recvfd, buf, sizeof(payload));
				payload_ok = got == (ssize_t)sizeof(payload) &&
				    memcmp(buf, payload, sizeof(payload)) == 0;
			}
			if (fstat(pfd[0], &st_sender) == 0 &&
			    fstat(recvfd, &st_recv) == 0) {
				inode_match = st_sender.st_ino == st_recv.st_ino &&
				    st_sender.st_dev == st_recv.st_dev;
			}
			written = write(pfd[1], "SIDE", 4);
			if (written == 4) {
				memset(buf, 0, sizeof(buf));
				got = read(pfd[0], buf, 4);
				sender_read_ok = got == 4 && memcmp(buf, "SIDE", 4) == 0;
			}
		}
		errno = 0;
		flags_after = fcntl(pfd[0], F_GETFD);
	}

	emit("I5 transfer socketpair=[%d,%d] pipe=[%d,%d] sent_fd=%d recv_fd=%d "
	    "sent_ok=%d recv_ok=%d payload_ok=%d inode_match=%d sender_read_ok=%d "
	    "sender_flags_before=%d sender_flags_after=%d\n",
	    sv[0], sv[1], pfd[0], pfd[1], pfd[0], recvfd, sent_ok, recv_ok,
	    payload_ok, inode_match, sender_read_ok, flags_before, flags_after);
	emit("I5 verdict scm_rights_round_trip=%d sender_unaffected=%d\n",
	    payload_ok && inode_match && sent_ok && recv_ok,
	    sender_read_ok && flags_before == flags_after);

	barrier("action-done");
	nap_ms(post_ms);
	barrier("done");
	return 0;
}

/* ---------------------------------------------------------------- I6 */

static int mode_close_range(long pre_ms, long post_ms)
{
	int limit = getdtablesize();
	int method_loop = 0;
	int method_close_range = 0;
	int closed_attempts = 0;
	int rpc_before = 0;
	int rpc_after = 0;
	int open_ok = 0;
	int err = 0;
	int i;
	long elapsed;
	long wall_elapsed;
	double t0;
	double t1;
	double wall0;
	double wall1;
	int fd;
	int probe_fd;
	char path[128];

	emit("I6 pid=%d\n", (int)getpid());
	barrier("pre");
	nap_ms(pre_ms);

	/* Use the loader first, so the per-thread RPC channel exists (and, on a
	 * build with the ring transport, so the warm-up calls attach it) before
	 * anything is closed.  The host samples the armed state in the hold. */
	for (i = 0; i < 40; ++i) {
		if (mach_host_self() != MACH_PORT_NULL) {
			++rpc_before;
		}
	}
	barrier("armed");
	/* Which of the descriptors about to be closed can the guest itself see?
	 * The host pairs this with its own pre-close inventory, so it can tell
	 * guest-owned descriptors from the loader's guarded band. */
	describe_visible_fds("I6 visible");
	nap_ms(pre_ms);

#ifdef SYS_close_range
	{
		long rc;

		errno = 0;
		rc = syscall(SYS_close_range, 3u, ~0u, 0u);
		if (rc == 0) {
			method_close_range = 1;
		} else {
			err = errno;
		}
	}
#else
	err = ENOSYS;
#endif
	if (!method_close_range) {
		method_loop = 1;
		t0 = mono_ms();
		wall0 = wall_ms();
		for (fd = 3; fd < limit; ++fd) {
			++closed_attempts;
			close(fd);
		}
		t1 = mono_ms();
		wall1 = wall_ms();
		elapsed = (long)(t1 - t0);
		wall_elapsed = (long)(wall1 - wall0);
	} else {
		elapsed = 0;
		wall_elapsed = 0;
	}

	/* Evidence first: a loader that breaks on the next RPC must still leave
	 * the measured numbers behind. */
	emit("I6 close method_close_range=%d method_loop=%d close_range_errno=%d(%s) "
	    "close_attempts=%d elapsed_ms=%ld wallclock_ms=%ld monotonic_went_backwards=%d "
	    "rpc_before_close=%d limit=%d\n",
	    method_close_range, method_loop, err, strerror(err), closed_attempts,
	    elapsed, wall_elapsed, elapsed < 0 ? 1 : 0, rpc_before, limit);

	rpc_after = mach_host_self() != MACH_PORT_NULL;

	snprintf(path, sizeof(path), "/private/var/tmp/relay-int-close-range-%d", (int)getpid());
	probe_fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
	if (probe_fd >= 0) {
		open_ok = write(probe_fd, "x", 1) == 1;
		close(probe_fd);
		unlink(path);
	} else {
		open_ok = 0;
	}

	emit("I6 post-close rpc_after_close=%d open_after_close=%d loader_still_works=%d\n",
	    rpc_after, open_ok, rpc_after && open_ok);

	barrier("action-done");
	nap_ms(post_ms);
	barrier("done");
	return 0;
}

/* ---------------------------------------------------------------- I7 */

static int mode_rpc(int count, long pre_ms, long post_ms)
{
	int ok = 0;
	int i;
	double t0;
	double t1;

	if (count < 0) {
		count = 0;
	}

	emit("I7 pid=%d\n", (int)getpid());
	barrier("pre");
	nap_ms(pre_ms);

	t0 = mono_ms();
	for (i = 0; i < count; ++i) {
		if (mach_host_self() != MACH_PORT_NULL) {
			++ok;
		}
	}
	t1 = mono_ms();

	emit("I7 workload rpc=%d ok=%d elapsed_ms=%.1f us_per_call=%.3f\n", count,
	    ok, t1 - t0, count > 0 ? (t1 - t0) * 1000.0 / (double)count : 0.0);

	barrier("action-done");
	nap_ms(post_ms);
	barrier("done");
	return 0;
}

/* ---------------------------------------------------------------- I9 / I10 */

/* Concurrency workload: THREADS threads, each running REQUESTS requests in a
 * loop.  Two dimensions beyond the count are selectable:
 *
 *   kind        `mach` is mach_host_self(), a Mach trap (the class I9 already
 *               used).  `bsd` is setuid(getuid()), a BSD syscall: Darling's
 *               emulation (sys_setuid -> __setuidgid) issues one dserver
 *               uidgid RPC per invocation, on the same per-thread transport
 *               and against the same darlingserver as the trap.  Only the get
 *               direction is cached by the guest (getuid/getgid/getgroups read
 *               a cached value after the first call), so the target uid is
 *               read once here, outside the timed window, and the loop only
 *               ever sets it back -- a privilege no-op that the server serves
 *               from its task lock with no I/O, no allocation and no host
 *               syscall.
 *   discipline  `simultaneous` releases every thread from one gate;
 *               `staggered` releases them STAGGER_US apart so their requests
 *               do not align.
 *
 * The threads are released only once every created thread is standing in the
 * gate, so the discipline measures release spacing and not thread-creation
 * cost.  Each thread uses its own ring lane (the guest attaches one lane per
 * thread), so nothing here shares a lane.  This mode measures and prints; it
 * never decides a verdict.
 */

enum {
	RI_KIND_MACH = 0,
	RI_KIND_BSD = 1,
};

static int usage(void);

struct conc_ctx {
	pthread_mutex_t lock;
	pthread_cond_t gate;
	/* Staggered release: one flag per thread, written by the releasing
	 * thread and spun on by the released one.  A condition variable would
	 * cost a kernel round trip per release in this guest, which is larger
	 * than the spacing being asked for; the simultaneous discipline keeps
	 * the single shared gate broadcast, which is the shape I9 was measured
	 * with. */
	volatile int released[RI_MAX_THREADS];
	int requests;
	int started;
	int gate_open;
	int rpc_ok;
	int rpc_failed;
	int kind;
	int staggered;
	uid_t bsd_uid;
};

struct conc_thread {
	struct conc_ctx *ctx;
	int index;
};

static void *conc_thr_main(void *arg)
{
	struct conc_thread *self = arg;
	struct conc_ctx *ctx = self->ctx;
	int ok = 0;
	int failed = 0;
	int i;

	pthread_mutex_lock(&ctx->lock);
	++ctx->started;
	pthread_cond_broadcast(&ctx->gate);
	if (ctx->staggered) {
		pthread_mutex_unlock(&ctx->lock);
		while (!ctx->released[self->index]) {
		}
	} else {
		while (!ctx->gate_open) {
			pthread_cond_wait(&ctx->gate, &ctx->lock);
		}
		pthread_mutex_unlock(&ctx->lock);
	}

	for (i = 0; i < ctx->requests; ++i) {
		int ok_this;

		if (ctx->kind == RI_KIND_BSD) {
			/* One uidgid RPC on the server side, every call. */
			ok_this = setuid(ctx->bsd_uid) == 0;
		} else {
			ok_this = mach_host_self() != MACH_PORT_NULL;
		}
		if (ok_this) {
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

static int mode_concurrent(int requested, int per_thread, long pre_ms,
    long post_ms, const char *kind_name, const char *discipline_name,
    long stagger_us)
{
	pthread_t threads[RI_MAX_THREADS];
	struct conc_thread args[RI_MAX_THREADS];
	struct conc_ctx ctx;
	const char *kind;
	int kind_id;
	int staggered;
	int nominal;
	int actual;
	int created = 0;
	int i;
	double t0;
	double t1;
	double release_first = 0.0;
	double release_last = 0.0;
	char detail[80] = "";

	if (strcmp(kind_name, "mach") == 0) {
		kind = "mach";
		kind_id = RI_KIND_MACH;
	} else if (strcmp(kind_name, "bsd") == 0) {
		kind = "bsd";
		kind_id = RI_KIND_BSD;
	} else {
		fprintf(stderr, "unknown kind '%s' (want mach or bsd)\n",
		    kind_name);
		return usage();
	}
	if (strcmp(discipline_name, "simultaneous") == 0) {
		staggered = 0;
	} else if (strcmp(discipline_name, "staggered") == 0) {
		staggered = 1;
	} else {
		fprintf(stderr, "unknown discipline '%s' (want simultaneous "
		    "or staggered)\n", discipline_name);
		return usage();
	}
	if (stagger_us < 0) {
		stagger_us = 0;
	}

	if (requested < 0) {
		requested = 0;
	}
	if (requested > RI_MAX_THREADS) {
		requested = RI_MAX_THREADS;
	}
	if (per_thread < 0) {
		per_thread = 0;
	}
	nominal = requested * per_thread;

	memset(&ctx, 0, sizeof(ctx));
	pthread_mutex_init(&ctx.lock, NULL);
	pthread_cond_init(&ctx.gate, NULL);
	ctx.requests = per_thread;
	ctx.kind = kind_id;
	ctx.staggered = staggered;
	ctx.bsd_uid = getuid();
	if (kind_id == RI_KIND_BSD) {
		snprintf(detail, sizeof(detail), " bsd_target_uid=%u",
		    (unsigned)ctx.bsd_uid);
	}

	emit("I9 pid=%d\n", (int)getpid());
	barrier("pre");
	nap_ms(pre_ms);

	t0 = mono_ms();
	for (i = 0; i < requested; ++i) {
		args[i].ctx = &ctx;
		args[i].index = i;
		if (pthread_create(&threads[i], NULL, conc_thr_main,
		    &args[i]) != 0) {
			break;
		}
		++created;
	}

	/* Release the gate only once every created thread is standing in it,
	 * so the release spacing below is not mixed with thread creation. */
	pthread_mutex_lock(&ctx.lock);
	while (ctx.started < created) {
		pthread_cond_wait(&ctx.gate, &ctx.lock);
	}
	if (staggered && created > 1) {
		/* The release train is timed against the guest's monotonic clock
		 * and spins, because a sleep is far coarser than the requested
		 * spacing on this runtime; the achieved spread is printed so the
		 * caller can see what spacing actually happened. */
		double base;

		pthread_mutex_unlock(&ctx.lock);
		base = mono_ms();
		for (i = 0; i < created; ++i) {
			double deadline;

			if (i > 0) {
				deadline = base +
				    (double)i * (double)stagger_us / 1000.0;
				while (mono_ms() < deadline) {
				}
			}
			ctx.released[i] = 1;
			if (i == 0) {
				release_first = mono_ms();
			}
			release_last = mono_ms();
		}
	} else {
		ctx.gate_open = 1;
		pthread_cond_broadcast(&ctx.gate);
		if (created > 0) {
			release_first = release_last = mono_ms();
		}
		pthread_mutex_unlock(&ctx.lock);
	}

	for (i = 0; i < created; ++i) {
		pthread_join(threads[i], NULL);
	}
	t1 = mono_ms();
	actual = created * per_thread;

	emit("I9 workload threads_requested=%d threads_created=%d "
	    "requests_per_thread=%d nominal_requests=%d actual_requests=%d rpc_ok=%d "
	    "rpc_failed=%d kind=%s start_gate=%s stagger_us=%ld "
	    "release_spread_ms=%.3f elapsed_ms=%.1f us_per_call=%.3f%s\n",
	    requested, created, per_thread, nominal, actual, ctx.rpc_ok,
	    ctx.rpc_failed, kind, staggered ? "staggered" : "simultaneous",
	    stagger_us, release_last - release_first, t1 - t0,
	    actual > 0 ? (t1 - t0) * 1000.0 / (double)actual : 0.0, detail);

	barrier("action-done");
	nap_ms(post_ms);
	pthread_cond_destroy(&ctx.gate);
	pthread_mutex_destroy(&ctx.lock);
	barrier("done");
	return 0;
}

/* ------------------------------------------------------------------ */

static int usage(void)
{
	fprintf(stderr,
	    "usage: %s defect\n"
	    "       %s hidden PRE_MS POST_MS\n"
	    "       %s threads N PRE_MS POST_MS\n"
	    "       %s ring WARMUP PRE_MS POST_MS\n"
	    "       %s scm PRE_MS POST_MS\n"
	    "       %s close-range PRE_MS POST_MS\n"
	    "       %s rpc N PRE_MS POST_MS\n"
	    "       %s concurrent THREADS REQUESTS_PER_THREAD PRE_MS POST_MS\n"
	    "          [KIND [DISCIPLINE [STAGGER_US]]]\n"
	    "          KIND=mach|bsd DISCIPLINE=simultaneous|staggered\n",
	    "relay_integration_fixture", "relay_integration_fixture",
	    "relay_integration_fixture", "relay_integration_fixture",
	    "relay_integration_fixture", "relay_integration_fixture",
	    "relay_integration_fixture", "relay_integration_fixture");
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

	if (strcmp(mode, "defect") == 0 && argc == 2) {
		return mode_defect();
	}
	if (strcmp(mode, "hidden") == 0 && argc == 4) {
		return mode_hidden(atol(argv[2]), atol(argv[3]));
	}
	if (strcmp(mode, "threads") == 0 && argc == 5) {
		return mode_threads(atoi(argv[2]), atol(argv[3]), atol(argv[4]));
	}
	if (strcmp(mode, "ring") == 0 && argc == 5) {
		return mode_ring(atoi(argv[2]), atol(argv[3]), atol(argv[4]));
	}
	if (strcmp(mode, "scm") == 0 && argc == 4) {
		return mode_scm(atol(argv[2]), atol(argv[3]));
	}
	if (strcmp(mode, "close-range") == 0 && argc == 4) {
		return mode_close_range(atol(argv[2]), atol(argv[3]));
	}
	if (strcmp(mode, "rpc") == 0 && argc == 5) {
		return mode_rpc(atoi(argv[2]), atol(argv[3]), atol(argv[4]));
	}
	if (strcmp(mode, "concurrent") == 0 && argc >= 4 && argc <= 9) {
		return mode_concurrent(atoi(argv[2]), atoi(argv[3]),
		    argc > 4 ? atol(argv[4]) : 0, argc > 5 ? atol(argv[5]) : 0,
		    argc > 6 ? argv[6] : "mach",
		    argc > 7 ? argv[7] : "simultaneous",
		    argc > 8 ? atol(argv[8]) : 250);
	}
	return usage();
}
