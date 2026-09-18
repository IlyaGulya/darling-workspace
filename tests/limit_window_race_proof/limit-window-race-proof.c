/*
 * limit-window-race-proof.c -- standalone falsification harness for the
 * RLIMIT_NOFILE "private allocation window" drafted in
 * source-fixes/ring-fd-ownership (branch fix/ring-fd-ownership, uncommitted,
 * src/startup/mldr/mldr.c).
 *
 * WHAT IS UNDER TEST
 * ------------------
 * Linux keeps RLIMIT_NOFILE in the thread group's signal_struct, so one thread
 * changing it changes it for every thread of the process.  The draft under test
 * changes it on every private (loader-owned) descriptor allocation:
 *
 *   private_fd_allocation_begin (mldr.c:637-665)
 *       block async signals; take socket_bitmap.mutex; getrlimit(RLIMIT_NOFILE);
 *       clamp the SAVED rlim_cur down to public_fd_hard_limit(rlim_max);
 *       publish that clamped value into public_fd_limit; then
 *       private_limit.rlim_cur = private_limit.rlim_max;
 *       setrlimit(RLIMIT_NOFILE, &private_limit);     <-- process-wide RAISE
 *       if (socket_bitmap.highest == -1) socket_bitmap.highest = rlim_max - 1;
 *   private_fd_allocation_end (mldr.c:667-678)
 *       setrlimit(RLIMIT_NOFILE, &allocation->saved_limit);   <-- restore
 *       unlock;
 *
 * Its callers are the ring and RPC paths -- __mldr_adopt_ring_fd (mldr.c:796-824)
 * and __mldr_create_rpc_socket (mldr.c:868-904) -- plus
 * __mldr_create_process_lifetime_pipe (mldr.c:911-...) and mldr.c:1029.  They all
 * allocate inside the window through socket_bitmap_adopt_locked (mldr.c:738-777),
 * with F_DUPFD_CLOEXEC at mldr.c:814-815 / 878-879 (and a plain F_DUPFD at
 * mldr.c:926).
 *
 * The draft's guard predicate __mldr_fd_is_internal (mldr.c:826-839) tests
 * BITMAP MEMBERSHIP ONLY: an fd the loader has not adopted yet -- any free
 * number inside the reserved band, or any number above the published limit --
 * is NOT reported internal.  The guest-side guard contract is
 * guard_table_check(fd, guard_flag_prevent_close) (deployed tree,
 * libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/unistd/dup2.c:23 and
 * close.c:32), so a descriptor the predicate does not report is neither closed
 * nor refused by dup2.
 *
 * CONSEQUENCE THIS HARNESS MEASURES
 * ---------------------------------
 * Outside the window the kernel refuses an explicit high target because the
 * real soft limit equals the published (low) limit.  INSIDE the window the same
 * call succeeds, and the guest obtains a descriptor above its advertised limit
 * and/or inside the band the loader reserved for itself.
 *
 * CLAIMS
 * ------
 *   R1 MUST PASS  the window exists: thread B's open() during the window
 *                 returns a descriptor, the process soft limit B reads during
 *                 the window equals the HARD limit, and the published value
 *                 stayed the LOW one.
 *   R2 MUST PASS  dup2(fd, target) with an explicit FREE target above the
 *                 published limit succeeds during the window (number and band
 *                 membership printed) and the same call, same target, fails
 *                 outside the window.
 *   R3 MUST PASS  fcntl(F_DUPFD_CLOEXEC, min) with an explicit min above the
 *                 published limit does the same: succeeds inside, refused
 *                 outside.
 *   R4 MUST PASS  the vectors that allocate the LOWEST FREE number (open,
 *                 socket, pipe, dup -- no explicit target) do NOT escalate:
 *                 their numbers are below the published limit in BOTH windows.
 *                 This is the measured distinction between "safe" and
 *                 "escalating" vectors; the numbers are printed, not assumed.
 *   R5 MUST PASS  the consequence: a descriptor B obtained during the window is
 *                 NOT reported internal by the draft's guard predicate, so the
 *                 guard does not protect the reserved band; the loader's own
 *                 allocator then cannot take that exact number while B holds it,
 *                 and takes it (as an INTERNAL descriptor) as soon as B closes
 *                 it -- the number crosses ownership domains with no record.
 *   R6 MUST PASS  stability: an unrelated thread inside the window reads the
 *                 raw process soft limit at the HARD value, so a guest-visible
 *                 limit query that is not routed through the loader observes a
 *                 limit that is not the published one (the loader's own routed
 *                 query would block on socket_bitmap.mutex instead).
 *
 * Part 2 (the runner) mutates a copy of this model in its temporary directory
 * and requires the named claims to go red; the load-bearing mutation removes
 * the temporary raise and requires R2, R3 and R6 to fail.
 *
 * Part 3 pins the semantics a truthful-limit design must preserve, using the
 * same model's loader-mediated setter (__mldr_set_nofile_limits, mldr.c:701-736)
 * and the deployed loader's allocation rule (forest tree, mldr.c:622-680:
 * highest = rlim_cur - 1 at mldr.c:639, fd = highest - next_index at mldr.c:668,
 * and no setrlimit call anywhere in that file):
 *   S1 MUST PASS  a descriptor opened high in the range survives, and the soft
 *                 limit is then lowered below that descriptor's number.
 *   S2 MUST PASS  the already-open descriptor stays fully usable (read, write,
 *                 lseek, fstat) after the lowering.
 *   S3 MUST PASS  a new lowest-free allocation after the lowering returns a
 *                 number below the new soft limit, and an explicit request at or
 *                 above the new soft limit is refused by the kernel.
 *   S4 MUST PASS  a loader-style private allocation at a high number still
 *                 succeeds while the guest soft limit is low -- and this
 *                 REQUIRES the real soft limit to differ from the published
 *                 one; the deployed rule (top-down from rlim_cur-1, no raise)
 *                 instead lands inside the guest's advertised range.
 *   S5 MUST PASS  raising the soft limit back within the hard limit behaves per
 *                 the Linux contract (succeeds and is observable), and raising
 *                 above the hard limit fails with the expected errno.
 *   S6 MUST PASS  lowering the soft limit does not disturb descriptors the
 *                 loader owns: same live object, still reported internal, still
 *                 above the guest's soft limit.
 *
 * WHAT THIS HARNESS MODELS, AND WHAT IT DOES NOT
 * ----------------------------------------------
 *   * It is a MODEL of the draft's sequence, not the loader.  The functions
 *     model_private_fd_allocation_begin/_end, socket_bitmap_adopt_locked,
 *     model_fd_is_internal, model_user_fd_limit, model_get_nofile_limits and
 *     model_set_nofile_limits are transcriptions of the draft's mldr.c, with the
 *     loader-specific peripherals (ring_fds, elfcalls dispatch, the guest
 *     images, the pre-migration machinery) omitted.  The draft's
 *     setrlimit(RLIMIT_NOFILE) IS the syscall being tested, so it is executed
 *     for real.
 *   * Thread A stands in for the loader thread and thread B for a guest thread
 *     of the same process; the real loader's callers are loader-internal, so the
 *     window is reproduced with barriers instead of racing a million attempts.
 *   * The guard is modeled as "the guest refuses/closes exactly the descriptors
 *     __mldr_fd_is_internal reports", which is the deployed dup2.c/close.c
 *     contract; the guest-side guard hash table itself is not exercised.
 *   * Only the host Linux kernel is measured (the container shares it).  The
 *     Darwin contract for O_NOFILE is NOT exercised: no Darwin kernel is
 *     involved anywhere in this harness.
 *   * The harness normalizes its own RLIMIT_NOFILE regime (hard 8192 unless the
 *     environment already has less, soft = the published limit) and reports the
 *     values; the setrlimit-above-the-hard-limit contract is only observable in
 *     an unprivileged process, which is what this harness is run as.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

/* ------------------------------------------------------------------ */
/* claim bookkeeping                                                   */
/* ------------------------------------------------------------------ */

#define NCLAIMS 12
#define DET_MAX 768

static int g_ok[NCLAIMS + 1];
static char g_detail[NCLAIMS + 1][DET_MAX];
static const char *g_env = "unlabeled";

static const char *claim_label(int i)
{
	switch (i) {
	case 1: return "R1";
	case 2: return "R2";
	case 3: return "R3";
	case 4: return "R4";
	case 5: return "R5";
	case 6: return "R6";
	case 7: return "S1";
	case 8: return "S2";
	case 9: return "S3";
	case 10: return "S4";
	case 11: return "S5";
	case 12: return "S6";
	default: return "?";
	}
}

static const char *claim_name(int i)
{
	switch (i) {
	case 1: return "window exists (raw soft == hard, published stays low)";
	case 2: return "dup2 to an explicit free high target";
	case 3: return "F_DUPFD_CLOEXEC with an explicit high min";
	case 4: return "lowest-free vectors do not escalate";
	case 5: return "guard does not protect the reserved band";
	case 6: return "raw soft limit observable at the hard value";
	case 7: return "descriptor above a lowered soft limit";
	case 8: return "that descriptor stays usable";
	case 9: return "new allocations respect the lowered limit";
	case 10: return "private allocation above the guest limit";
	case 11: return "soft-limit raise contract";
	case 12: return "lowering does not disturb loader descriptors";
	default: return "?";
	}
}

static int all_ok(void);
static void print_claims(void);

static void verdict(int idx, int ok, const char *fmt, ...)
{
	va_list ap;

	g_ok[idx] = ok ? 1 : 0;
	va_start(ap, fmt);
	vsnprintf(g_detail[idx], sizeof g_detail[idx], fmt, ap);
	va_end(ap);
}

static void print_claims(void)
{
	int i;

	for (i = 1; i <= NCLAIMS; i++)
		printf("%s %s %s: %s\n", claim_label(i), g_ok[i] ? "PASS" : "FAIL",
		       claim_name(i), g_detail[i]);
	printf("HARNESS %s env=%s pid=%d\n", all_ok() ? "OK" : "FAILED", g_env,
	       (int)getpid());
	fflush(stdout);
}

static int all_ok(void)
{
	int i;

	for (i = 1; i <= NCLAIMS; i++)
		if (!g_ok[i])
			return 0;
	return 1;
}

static void die(const char *what)
{
	int i;

	fprintf(stderr, "harness fatal: %s: %s\n", what, strerror(errno));
	fflush(stderr);
	for (i = 1; i <= NCLAIMS; i++)
		verdict(i, 0, "not evaluated: %s failed (%s)", what,
			strerror(errno));
	print_claims();
	_exit(3);
}

/* ------------------------------------------------------------------ */
/* harness plumbing                                                    */
/* ------------------------------------------------------------------ */

static int g_tracked[512];
static size_t g_tracked_n;
static struct rlimit g_original_limits;

static void track_fd(int fd)
{
	if (fd >= 0) {
		if (g_tracked_n >= sizeof g_tracked / sizeof g_tracked[0])
			die("descriptor ledger overflow");
		g_tracked[g_tracked_n++] = fd;
	}
}

static void close_tracked(void)
{
	size_t i;

	for (i = 0; i < g_tracked_n; i++) {
		if (g_tracked[i] >= 0) {
			close(g_tracked[i]);
			g_tracked[i] = -1;
		}
	}
	g_tracked_n = 0;
}

static void rendezvous(pthread_barrier_t *barrier)
{
	int rc = pthread_barrier_wait(barrier);

	if (rc != 0 && rc != PTHREAD_BARRIER_SERIAL_THREAD)
		die("pthread_barrier_wait");
}

static rlim_t read_raw_soft_limit(void)
{
	struct rlimit limit;

	if (getrlimit(RLIMIT_NOFILE, &limit) < 0)
		die("getrlimit(RLIMIT_NOFILE)");
	return limit.rlim_cur;
}

/* ------------------------------------------------------------------ */
/* the model of the draft                                              */
/* ------------------------------------------------------------------ */

#define INTERNAL_FD_RESERVE 4096        /* mldr.c:621 */

/*
 * Mutation seams.  run-limit-window-race-proof.sh flips each one in a copy of
 * this file kept in its temporary directory and requires the named claims to go
 * red.
 *
 *   MODEL_RAISE_SOFT_LIMIT      the draft temporarily raises the process-wide
 *                               soft limit to the hard limit (mldr.c:653-654)
 *                               and reserves the top of the range for itself.
 *                               With 0 the raise is gone and the private
 *                               allocation follows the deployed loader
 *                               (top-down from rlim_cur - 1, no setrlimit):
 *                               no window -> R2, R3, R6 red.
 *   MODEL_GUARD_PROTECTS_BAND   with 1 the guard predicate reports every fd at
 *                               or above the published limit as internal (it
 *                               protects the reserved band) -> R5 red.
 *   MODEL_RESTORE_SOFT_LIMIT    with 0 the window never closes (mldr.c:669) ->
 *                               the "outside the window" negatives of R2/R3 red.
 *   MODEL_PUBLISH_RAW_LIMIT     with 1 the guest-visible query answers the raw
 *                               soft limit instead of the published one -> R1
 *                               and R6 red.
 */
#define MODEL_RAISE_SOFT_LIMIT 1
#define MODEL_GUARD_PROTECTS_BAND 0
#define MODEL_RESTORE_SOFT_LIMIT 1
#define MODEL_PUBLISH_RAW_LIMIT 0

typedef struct socket_bitmap {
	pthread_mutex_t mutex;
	uint8_t *bits;
	size_t bit_length;
	int highest;
	/* present only for the deployed loader's rule (forest mldr.c:622-680). */
	size_t next_index;
} socket_bitmap_t;

/* mldr.c:604-609 */
static socket_bitmap_t socket_bitmap = {
	.mutex = PTHREAD_MUTEX_INITIALIZER,
	.bits = NULL,
	.bit_length = 0,
	.highest = -1,
	.next_index = 0,
};

/* mldr.c:622: the value the guest is told the limit is. */
static rlim_t public_fd_limit = RLIM_INFINITY;

/* mldr.c:624-630 */
static rlim_t public_fd_hard_limit(rlim_t native)
{
	rlim_t reserve = native / 2;

	if (reserve > INTERNAL_FD_RESERVE)
		reserve = INTERNAL_FD_RESERVE;
	return native - reserve;
}

/* signal_atomic.h:9-15: mldr_block_async_signals. */
static void model_block_async_signals(sigset_t *saved)
{
	sigset_t set;

	sigfillset(&set);
	sigdelset(&set, SIGRTMIN);
	sigdelset(&set, SIGRTMIN + 1);
	if (pthread_sigmask(SIG_BLOCK, &set, saved) != 0)
		die("pthread_sigmask(SIG_BLOCK)");
}

/* signal_atomic.h:17-19: mldr_restore_signals. */
static void model_restore_signals(const sigset_t *saved)
{
	if (pthread_sigmask(SIG_SETMASK, saved, NULL) != 0)
		die("pthread_sigmask(SIG_SETMASK)");
}

/* mldr.c:633-635 */
typedef struct private_fd_allocation {
	sigset_t saved_signals;
	struct rlimit saved_limit;
} private_fd_allocation_t;

static int deployed_adopt_locked(socket_bitmap_t *bitmap, int source);

/* mldr.c:637-665, transcribed. */
static __attribute__((noinline)) int
model_private_fd_allocation_begin(private_fd_allocation_t *allocation)
{
	rlim_t ceiling;

	model_block_async_signals(&allocation->saved_signals);
	pthread_mutex_lock(&socket_bitmap.mutex);
	if (getrlimit(RLIMIT_NOFILE, &allocation->saved_limit) < 0)
		goto fail;
	if (allocation->saved_limit.rlim_max > INT_MAX) {
		errno = EINVAL;
		goto fail;
	}
	ceiling = public_fd_hard_limit(allocation->saved_limit.rlim_max);
	if (allocation->saved_limit.rlim_cur > ceiling)
		allocation->saved_limit.rlim_cur = ceiling;
	__atomic_store_n(&public_fd_limit, allocation->saved_limit.rlim_cur, __ATOMIC_RELEASE); /* mldr.c:651 */
#if MODEL_RAISE_SOFT_LIMIT
	{
		struct rlimit private_limit = allocation->saved_limit;

		private_limit.rlim_cur = private_limit.rlim_max;      /* mldr.c:653 */
		if (setrlimit(RLIMIT_NOFILE, &private_limit) < 0)     /* mldr.c:654 */
			goto fail;
		if (socket_bitmap.highest == -1)
			socket_bitmap.highest = (int)private_limit.rlim_max - 1; /* mldr.c:656-658 */
	}
#else
	/*
	 * MUTATED: the draft's temporary raise is gone.  The process-wide soft
	 * limit stays at the published value, so the private allocation has to
	 * happen at the published limit, as the deployed loader does (forest
	 * mldr.c:639: highest = rlim_cur - 1).
	 */
	if (socket_bitmap.highest == -1)
		socket_bitmap.highest = (int)allocation->saved_limit.rlim_cur - 1;
#endif
	return 0;
fail:
	pthread_mutex_unlock(&socket_bitmap.mutex);
	model_restore_signals(&allocation->saved_signals);
	return -1;
}

/* mldr.c:667-678, transcribed. */
static __attribute__((noinline)) void
model_private_fd_allocation_end(private_fd_allocation_t *allocation)
{
	int saved_errno = errno;

	if (MODEL_RESTORE_SOFT_LIMIT &&
	    setrlimit(RLIMIT_NOFILE, &allocation->saved_limit) < 0) {
		perror("Failed to restore public descriptor limit");
		_exit(4);
	}
	pthread_mutex_unlock(&socket_bitmap.mutex);
	model_restore_signals(&allocation->saved_signals);
	errno = saved_errno;
}

/*
 * mldr.c:738-777: the draft's own allocation primitive, used by every caller
 * (__mldr_adopt_ring_fd mldr.c:814-815, __mldr_create_rpc_socket mldr.c:878-879).
 */
static __attribute__((noinline, unused)) int
socket_bitmap_adopt_locked(socket_bitmap_t *bitmap, int source, int command,
	rlim_t native_hard)
{
	int lowest = (int)public_fd_hard_limit(native_hard);
	int highest = (int)native_hard - 1;

	if (highest > bitmap->highest)
		highest = bitmap->highest;
	for (int candidate = highest; candidate >= lowest; --candidate) {
		size_t index = (size_t)(bitmap->highest - candidate);

		if (index < bitmap->bit_length &&
		    (bitmap->bits[index / 8] & (1U << (index % 8))))
			continue;
		int duplicate = fcntl(source, command, candidate);
		if (duplicate < 0) {
			if (errno == EMFILE)
				continue;
			return -1;
		}
		index = (size_t)(bitmap->highest - duplicate);
		if (index >= bitmap->bit_length) {
			size_t old_bytes = (bitmap->bit_length + 7) / 8;
			size_t bytes = index / 8 + 1;
			uint8_t *bits = realloc(bitmap->bits, bytes);

			if (!bits) {
				close(duplicate);
				errno = ENOMEM;
				return -1;
			}
			memset(bits + old_bytes, 0, bytes - old_bytes);
			bitmap->bits = bits;
			bitmap->bit_length = bytes * 8;
		}
		bitmap->bits[index / 8] |= 1U << (index % 8);
		return duplicate;
	}
	errno = EMFILE;
	return -1;
}

/*
 * The deployed loader's rule (forest tree, mldr.c:622-680): highest is
 * rlim_cur - 1 (mldr.c:639), allocation walks top-down from it
 * (fd = highest - next_index, mldr.c:668), and setrlimit is never called.  Used
 * for the S4 comparison and, in the mutated copy, for the private allocation
 * itself.
 */
static int deployed_adopt_locked(socket_bitmap_t *bitmap, int source)
{
	if (bitmap->highest == -1) {
		struct rlimit limit;

		if (getrlimit(RLIMIT_NOFILE, &limit) < 0)
			return -1;
		if (limit.rlim_cur == RLIM_INFINITY)
			limit.rlim_cur = 1024;
		bitmap->highest = (int)limit.rlim_cur - 1;
	}
	if (bitmap->highest < 3)
		goto exhausted;
	for (size_t index = bitmap->next_index;
	     index <= (size_t)(bitmap->highest - 3); ++index) {
		int candidate = bitmap->highest - (int)index;

		if (index < bitmap->bit_length &&
		    (bitmap->bits[index / 8] & (1U << (index % 8))))
			continue;
		int duplicate = fcntl(source, F_DUPFD_CLOEXEC, candidate);

		if (duplicate < 0) {
			if (errno == EMFILE)
				continue;
			return -1;
		}
		index = (size_t)(bitmap->highest - duplicate);
		if (index >= bitmap->bit_length) {
			size_t old_bytes = (bitmap->bit_length + 7) / 8;
			size_t bytes = index / 8 + 1;
			uint8_t *bits = realloc(bitmap->bits, bytes);

			if (!bits) {
				close(duplicate);
				errno = ENOMEM;
				return -1;
			}
			memset(bits + old_bytes, 0, bytes - old_bytes);
			bitmap->bits = bits;
			bitmap->bit_length = bytes * 8;
		}
		bitmap->bits[index / 8] |= 1U << (index % 8);
		bitmap->next_index = index + 1;
		return duplicate;
	}
exhausted:
	errno = EMFILE;
	return -1;
}

/* The private allocation the draft's two callers perform inside the window. */
static int model_private_alloc(private_fd_allocation_t *allocation, int source)
{
#if MODEL_RAISE_SOFT_LIMIT
	return socket_bitmap_adopt_locked(&socket_bitmap, source,
		F_DUPFD_CLOEXEC, allocation->saved_limit.rlim_max);
#else
	/* MUTATED: the deployed loader's allocation rule, no window at all. */
	(void)allocation;
	return deployed_adopt_locked(&socket_bitmap, source);
#endif
}

/* mldr.c:826-839: BITMAP MEMBERSHIP ONLY. */
static bool model_fd_is_internal(int fd)
{
	bool owned = false;
	sigset_t saved;

	if (MODEL_GUARD_PROTECTS_BAND && fd >= 0 &&
	    (rlim_t)fd >= __atomic_load_n(&public_fd_limit, __ATOMIC_ACQUIRE))
		return true;   /* MUTATED: the guard protects the reserved band */
	model_block_async_signals(&saved);
	pthread_mutex_lock(&socket_bitmap.mutex);
	if (fd >= 0 && fd <= socket_bitmap.highest) {
		size_t index = (size_t)(socket_bitmap.highest - fd);

		owned = index < socket_bitmap.bit_length &&
			(socket_bitmap.bits[index / 8] & (1U << (index % 8))) != 0;
	}
	pthread_mutex_unlock(&socket_bitmap.mutex);
	model_restore_signals(&saved);
	return owned;
}

/*
 * mldr.c:779-786 / 788-795: the loader clears a descriptor's bit when it
 * releases one of its own.  The harness calls this wherever it closes an
 * internal descriptor, so the model's registry does not drift from the model's
 * descriptor table.
 */
static void socket_bitmap_put_locked(socket_bitmap_t *bitmap, int fd)
{
	if (fd >= 0 && fd <= bitmap->highest) {
		size_t index = (size_t)(bitmap->highest - fd);

		if (index < bitmap->bit_length)
			bitmap->bits[index / 8] &= ~(1U << (index % 8));
	}
}

static void model_socket_bitmap_put(int fd)
{
	sigset_t saved;

	if (fd < 0)
		return;
	model_block_async_signals(&saved);
	pthread_mutex_lock(&socket_bitmap.mutex);
	socket_bitmap_put_locked(&socket_bitmap, fd);
	pthread_mutex_unlock(&socket_bitmap.mutex);
	model_restore_signals(&saved);
}

/*
 * The guest-side guard contract: the deployed tree's dup2.c:23 and close.c:32
 * refuse exactly the descriptors the loader reports internal.  The draft
 * extends that predicate with __mldr_fd_is_internal.
 */
static int model_guard_table_check(int fd)
{
	return model_fd_is_internal(fd) ? 1 : 0;
}

/* mldr.c:680-682 */
static uint64_t model_user_fd_limit(void)
{
	if (MODEL_PUBLISH_RAW_LIMIT)
		return read_raw_soft_limit();   /* MUTATED: the query follows the raw
		                                   soft limit, not the published value */
	return __atomic_load_n(&public_fd_limit, __ATOMIC_ACQUIRE);
}

/* mldr.c:684-699 */
static int model_get_nofile_limits(uint64_t *current, uint64_t *maximum)
{
	sigset_t saved;
	int result;

	model_block_async_signals(&saved);
	pthread_mutex_lock(&socket_bitmap.mutex);
	{
		struct rlimit limit;

		result = getrlimit(RLIMIT_NOFILE, &limit);
		if (result == 0) {
			*current = limit.rlim_cur;
			*maximum = public_fd_hard_limit(limit.rlim_max);
		} else {
			result = -errno;
		}
	}
	pthread_mutex_unlock(&socket_bitmap.mutex);
	model_restore_signals(&saved);
	return result;
}

/* mldr.c:701-736: the guest's limit changes go through this. */
static int model_set_nofile_limits(uint64_t current, uint64_t maximum)
{
	sigset_t saved;
	struct rlimit limit;
	int result;
	rlim_t old_maximum;

	model_block_async_signals(&saved);
	pthread_mutex_lock(&socket_bitmap.mutex);
	result = getrlimit(RLIMIT_NOFILE, &limit);
	if (result < 0) {
		result = -errno;
		goto out;
	}
	old_maximum = public_fd_hard_limit(limit.rlim_max);
	if (current > maximum) {
		result = -EINVAL;
		goto out;
	}
	if (maximum > old_maximum) {
		result = -EPERM;
		goto out;
	}
	if (maximum != old_maximum)
		limit.rlim_max = maximum +
			(maximum < INTERNAL_FD_RESERVE ? maximum : INTERNAL_FD_RESERVE);
	limit.rlim_cur = current;
	if (setrlimit(RLIMIT_NOFILE, &limit) < 0) {
		result = -errno;
		goto out;
	}
	__atomic_store_n(&public_fd_limit, limit.rlim_cur, __ATOMIC_RELEASE);
	result = 0;
out:
	pthread_mutex_unlock(&socket_bitmap.mutex);
	model_restore_signals(&saved);
	return result;
}

/*
 * mldr.c:684-699 takes the same mutex as the critical section, so a guest whose
 * limit query IS routed through the loader blocks while the window is open.
 * This is the same mutex, probed without blocking.
 */
static int routed_query_would_block(void)
{
	if (pthread_mutex_trylock(&socket_bitmap.mutex) == 0) {
		pthread_mutex_unlock(&socket_bitmap.mutex);
		return 0;
	}
	return 1;
}

/* ------------------------------------------------------------------ */
/* Part 1: the deterministic window                                    */
/* ------------------------------------------------------------------ */

typedef struct window_state {
	rlim_t hard;
	rlim_t low;             /* the published (advertised) limit */
	int band_mid;           /* a free number strictly inside [low, hard) */
	int band_top;           /* hard - 1 */

	/* thread A (loader) */
	int begin_rc;
	int begin_errno;
	int loader_fd_in_window;
	int loader_alloc_errno;

	/* thread B (guest), inside the window */
	rlim_t raw_cur_before;
	rlim_t raw_cur_in;
	uint64_t published_in;
	int routed_query_blocked_in;
	int open_in;
	int socket_in;
	int pipe_in[2];
	int dup_in;
	int dup2_in_target;
	int dup2_in_rc;
	int dup2_in_errno;
	int dup2top_in_target;
	int dup2top_in_rc;
	int dupfd_in_min;
	int dupfd_in_fd;
	int dupfd_in_errno;

	/* thread B (guest), outside the window */
	rlim_t raw_cur_out;
	uint64_t published_out;
	int open_out;
	int socket_out;
	int pipe_out[2];
	int dup_out;
	int dup2_out_rc;
	int dup2_out_errno;
	int dup2_out_free_rc;
	int dup2_out_free_errno;
	int dupfd_out_fd;
	int dupfd_out_errno;

	/* the loader's own allocator: in a later window while B still holds its
	 * escalated descriptors, and again after B closed them. */
	int post_window_fd;
	int post_window_errno;
	int reclaimed_fd;
	int reclaimed_errno;
} window_state_t;

static window_state_t W;
static pthread_barrier_t b_open;
static pthread_barrier_t b_probe_done;
static pthread_barrier_t b_closed;
static int g_guest_source = -1;   /* a low guest descriptor to duplicate */
static int g_loader_source = -1;  /* one end of a loader-owned socketpair */
static int g_loader_peer = -1;

static void evaluate_window_claims(void);
static void phase_band_consequence(void);

/*
 * Thread A: the loader performing one private allocation.  It mirrors
 * __mldr_adopt_ring_fd (mldr.c:796-824) / __mldr_create_rpc_socket
 * (mldr.c:868-904): begin(), allocate with F_DUPFD_CLOEXEC inside the band,
 * end().
 */
static void *loader_thread(void *arg)
{
	private_fd_allocation_t allocation;
	int rc;

	(void)arg;
	rc = model_private_fd_allocation_begin(&allocation);
	W.begin_rc = rc;
	W.begin_errno = errno;
	/* the window is now open for the whole process, or the raise failed */
	rendezvous(&b_open);
	/* the guest has finished its in-window probes */
	rendezvous(&b_probe_done);
	if (rc == 0) {
		W.loader_fd_in_window = model_private_alloc(&allocation,
			g_loader_source);
		W.loader_alloc_errno = W.loader_fd_in_window < 0 ? errno : 0;
		model_private_fd_allocation_end(&allocation);
	}
	rendezvous(&b_closed);
	return NULL;
}

static void phase_window(void)
{
	pthread_t loader;
	struct rlimit original;
	struct rlimit normalized;
	rlim_t target_hard;

	if (getrlimit(RLIMIT_NOFILE, &original) < 0)
		die("getrlimit(RLIMIT_NOFILE)");
	target_hard = original.rlim_max;
	if (target_hard == RLIM_INFINITY || target_hard > 8192)
		target_hard = 8192;
	if (target_hard < 64)
		die("RLIMIT_NOFILE hard limit too small to model the window");

	W.hard = target_hard;
	W.low = public_fd_hard_limit(W.hard);
	if (W.hard - W.low < 8)
		die("reserved band too small to model the window");
	W.band_mid = (int)(W.low + (W.hard - W.low) / 2);
	W.band_top = (int)W.hard - 1;

	/*
	 * The harness owns the regime it measures: the guest's advertised limit
	 * (the published value) starts equal to the real soft limit, and the
	 * reserved band is [low, hard).
	 */
	normalized.rlim_cur = W.low;
	normalized.rlim_max = W.hard;
	if (setrlimit(RLIMIT_NOFILE, &normalized) < 0)
		die("setrlimit(normalize)");
	__atomic_store_n(&public_fd_limit, W.low, __ATOMIC_RELEASE);
	W.raw_cur_before = read_raw_soft_limit();

	printf("INFO regime: original cur=%lu max=%lu; normalized published=%lu "
	       "real cur=%lu max=%lu; reserved band=[%lu,%lu) mid=%d top=%d\n",
	       (unsigned long)original.rlim_cur, (unsigned long)original.rlim_max,
	       (unsigned long)W.low, (unsigned long)W.raw_cur_before,
	       (unsigned long)W.hard, (unsigned long)W.low,
	       (unsigned long)W.hard, W.band_mid, W.band_top);
	fflush(stdout);

	g_guest_source = open("/dev/null", O_RDONLY | O_CLOEXEC);
	track_fd(g_guest_source);
	if (g_guest_source < 0)
		die("open(guest source)");
	{
		int sv[2];

		if (socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0, sv) < 0)
			die("socketpair(loader source)");
		g_loader_peer = sv[0];
		g_loader_source = sv[1];
		track_fd(sv[0]);
		track_fd(sv[1]);
	}
	if ((rlim_t)g_guest_source >= W.low || (rlim_t)g_loader_source >= W.low)
		die("a harness source descriptor is above the published limit");

	if (pthread_barrier_init(&b_open, NULL, 2) != 0 ||
	    pthread_barrier_init(&b_probe_done, NULL, 2) != 0 ||
	    pthread_barrier_init(&b_closed, NULL, 2) != 0)
		die("pthread_barrier_init");
	if (pthread_create(&loader, NULL, loader_thread, NULL) != 0)
		die("pthread_create");

	/* ---------------- thread B, INSIDE the window ---------------- */
	rendezvous(&b_open);
	W.raw_cur_in = read_raw_soft_limit();
	W.published_in = model_user_fd_limit();
	W.routed_query_blocked_in = routed_query_would_block();
	/* R4: vectors that allocate the lowest free number */
	W.open_in = open("/dev/null", O_RDONLY | O_CLOEXEC);
	W.socket_in = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
	if (pipe2(W.pipe_in, O_CLOEXEC) < 0) {
		W.pipe_in[0] = -1;
		W.pipe_in[1] = -1;
	}
	W.dup_in = dup(g_guest_source);
	/* R2: the same explicit call that must fail outside the window */
	W.dup2_in_target = W.band_mid;
	errno = 0;
	W.dup2_in_rc = dup2(g_guest_source, W.dup2_in_target);
	W.dup2_in_errno = errno;
	/* R5: an escalated descriptor at the very top of the band */
	W.dup2top_in_target = W.band_top;
	W.dup2top_in_rc = dup2(g_guest_source, W.dup2top_in_target);
	/* R3: the draft's own primitive with an explicit min */
	W.dupfd_in_min = W.band_mid;
	errno = 0;
	W.dupfd_in_fd = fcntl(g_guest_source, F_DUPFD_CLOEXEC, W.dupfd_in_min);
	W.dupfd_in_errno = errno;

	rendezvous(&b_probe_done);   /* A allocates inside the same window */
	rendezvous(&b_closed);       /* and then restores the soft limit */

	/* ---------------- thread B, OUTSIDE the window --------------- */
	W.raw_cur_out = read_raw_soft_limit();
	W.published_out = model_user_fd_limit();
	W.open_out = open("/dev/null", O_RDONLY | O_CLOEXEC);
	W.socket_out = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
	if (pipe2(W.pipe_out, O_CLOEXEC) < 0) {
		W.pipe_out[0] = -1;
		W.pipe_out[1] = -1;
	}
	W.dup_out = dup(g_guest_source);
	errno = 0;
	W.dup2_out_rc = dup2(g_guest_source, W.dup2_in_target);
	W.dup2_out_errno = errno;
	errno = 0;
	W.dup2_out_free_rc = dup2(g_guest_source, W.dup2_in_target - 1);
	W.dup2_out_free_errno = errno;
	errno = 0;
	W.dupfd_out_fd = fcntl(g_guest_source, F_DUPFD_CLOEXEC, W.dupfd_in_min);
	W.dupfd_out_errno = errno;

	if (pthread_join(loader, NULL) != 0)
		die("pthread_join");

	printf("INFO window: begin_rc=%d begin_errno=%d loader_fd_in_window=%d "
	       "(alloc errno=%d)\n", W.begin_rc, W.begin_errno,
	       W.loader_fd_in_window, W.loader_alloc_errno);
	fflush(stdout);

	evaluate_window_claims();
	phase_band_consequence();
}

static void evaluate_window_claims(void)
{
	int in_band;
	int refused_out;

	/* ---------------- R1 ---------------- */
	{
		int ok = W.begin_rc == 0 && W.raw_cur_before == W.low &&
			W.raw_cur_in == W.hard && W.published_in == W.low &&
			W.low < W.hard && W.open_in >= 0;

		verdict(1, ok, "before the window real soft=%lu published=%lu; "
			"inside the window real soft=%lu (hard=%lu) published=%lu; "
			"thread B open() -> %d",
			(unsigned long)W.raw_cur_before, (unsigned long)W.low,
			(unsigned long)W.raw_cur_in, (unsigned long)W.hard,
			(unsigned long)W.published_in, W.open_in);
	}

	/* ---------------- R2 ---------------- */
	in_band = W.dup2_in_target > (int)W.low &&
		W.dup2_in_target < (int)W.hard;
	refused_out = W.dup2_out_rc < 0 &&
		(W.dup2_out_errno == EBADF || W.dup2_out_errno == EINVAL) &&
		W.dup2_out_free_rc < 0 &&
		(W.dup2_out_free_errno == EBADF || W.dup2_out_free_errno == EINVAL);
	verdict(2, W.dup2_in_rc == W.dup2_in_target && in_band && refused_out,
		"dup2(fd,%d) [reserved band=%s, published=%lu]: inside -> rc=%d "
		"errno=%d (%s); outside, same target (occupied by that success) -> "
		"rc=%d errno=%d (%s); outside, free target %d -> rc=%d errno=%d (%s)",
		W.dup2_in_target, in_band ? "yes" : "no", (unsigned long)W.low,
		W.dup2_in_rc, W.dup2_in_errno, strerror(W.dup2_in_errno),
		W.dup2_out_rc, W.dup2_out_errno,
		strerror(W.dup2_out_errno), W.dup2_in_target - 1,
		W.dup2_out_free_rc, W.dup2_out_free_errno,
		strerror(W.dup2_out_free_errno));

	/* ---------------- R3 ---------------- */
	{
		int ok_in = W.dupfd_in_fd >= W.dupfd_in_min &&
			(rlim_t)W.dupfd_in_fd < W.hard;
		int ok_out = W.dupfd_out_fd < 0 && W.dupfd_out_errno == EINVAL;

		verdict(3, ok_in && in_band && ok_out,
			"fcntl(F_DUPFD_CLOEXEC,min=%d) [above published=%lu]: "
			"inside -> fd=%d errno=%d; outside, same min -> fd=%d "
			"errno=%d (%s)",
			W.dupfd_in_min, (unsigned long)W.low, W.dupfd_in_fd,
			W.dupfd_in_errno, W.dupfd_out_fd, W.dupfd_out_errno,
			strerror(W.dupfd_out_errno));
	}

	/* ---------------- R4 ---------------- */
	{
		int inside[5] = { W.open_in, W.socket_in, W.pipe_in[0],
			W.pipe_in[1], W.dup_in };
		int outside[5] = { W.open_out, W.socket_out, W.pipe_out[0],
			W.pipe_out[1], W.dup_out };
		int ok = 1;
		int i;

		for (i = 0; i < 5; i++) {
			if (inside[i] < 0 || (rlim_t)inside[i] >= W.low)
				ok = 0;
			if (outside[i] < 0 || (rlim_t)outside[i] >= W.low)
				ok = 0;
		}
		verdict(4, ok, "published=%lu: inside open=%d socket=%d pipe=%d,%d "
			"dup=%d; outside open=%d socket=%d pipe=%d,%d dup=%d; every "
			"lowest-free vector stayed below the published limit in both "
			"windows",
			(unsigned long)W.low, inside[0], inside[1], inside[2],
			inside[3], inside[4], outside[0], outside[1], outside[2],
			outside[3], outside[4]);
	}

	/* ---------------- R6 ---------------- */
	{
		int ok = W.raw_cur_in == W.hard && W.published_in == W.low &&
			W.routed_query_blocked_in == 1 &&
			W.raw_cur_out == W.low && W.published_out == W.low;

		verdict(6, ok, "thread B inside the window: raw soft=%lu (hard=%lu) "
			"while published=%lu; the loader's routed query would block "
			"(mutex busy=%d); outside: raw soft=%lu published=%lu",
			(unsigned long)W.raw_cur_in, (unsigned long)W.hard,
			(unsigned long)W.published_in,
			W.routed_query_blocked_in, (unsigned long)W.raw_cur_out,
			(unsigned long)W.published_out);
	}
}

/* A fresh private allocation, in its own window, exactly as the draft's
 * callers do: begin(), F_DUPFD_CLOEXEC into the band, end(). */
static int private_alloc_fresh(int *out_errno)
{
	private_fd_allocation_t allocation;
	int fd;

	if (model_private_fd_allocation_begin(&allocation) < 0) {
		*out_errno = errno;
		return -1;
	}
	fd = model_private_alloc(&allocation, g_loader_source);
	*out_errno = fd < 0 ? errno : 0;
	model_private_fd_allocation_end(&allocation);
	return fd;
}

/*
 * R5: what the escalated descriptor means for the loader's own registry.
 */
static void phase_band_consequence(void)
{
	int escalated = W.dup2top_in_target;
	int mid = W.dup2_in_target;
	int is_internal_top = model_fd_is_internal(escalated) ? 1 : 0;
	int is_internal_mid = model_fd_is_internal(mid) ? 1 : 0;
	int guard_top = model_guard_table_check(escalated);
	int guard_mid = model_guard_table_check(mid);
	int closed_reclaimed;

	/* the loader, in a later window, while B still holds both numbers */
	W.post_window_fd = private_alloc_fresh(&W.post_window_errno);

	/* B releases its numbers; the loader's next window reclaims the top one */
	close(escalated);
	close(mid);
	if (W.loader_fd_in_window >= 0)
		close(W.loader_fd_in_window);
	if (W.post_window_fd >= 0)
		close(W.post_window_fd);
	/* the loader releases its own descriptors through its registry */
	model_socket_bitmap_put(W.loader_fd_in_window);
	model_socket_bitmap_put(W.post_window_fd);
	W.reclaimed_fd = private_alloc_fresh(&W.reclaimed_errno);
	closed_reclaimed = model_fd_is_internal(W.reclaimed_fd) ? 1 : 0;

	printf("INFO band: escalated top=%d mid=%d; is_internal(top)=%d "
	       "is_internal(mid)=%d guard(top)=%d guard(mid)=%d; loader private "
	       "fd in the window=%d (errno=%d); a later window while both are "
	       "open -> %d (errno=%d); after the guest closed them -> %d "
	       "(internal=%d)\n",
	       escalated, mid, is_internal_top, is_internal_mid, guard_top,
	       guard_mid, W.loader_fd_in_window, W.loader_alloc_errno,
	       W.post_window_fd, W.post_window_errno, W.reclaimed_fd,
	       closed_reclaimed);
	fflush(stdout);

	verdict(5, W.dup2top_in_rc == W.dup2top_in_target &&
		W.dup2_in_rc == W.dup2_in_target &&
		is_internal_top == 0 && is_internal_mid == 0 &&
		guard_top == 0 && guard_mid == 0 &&
		W.loader_fd_in_window >= 0 &&
		W.loader_fd_in_window != escalated &&
		W.post_window_fd >= 0 && W.post_window_fd != escalated &&
		(rlim_t)W.post_window_fd >= W.low &&
		W.reclaimed_fd == escalated && closed_reclaimed == 1,
		"guest descriptors %d/%d obtained inside the window are NOT "
		"internal (is_internal=%d/%d, guard=%d/%d), so the reserved band is "
		"unguarded; the loader's own allocator could not take %d while the "
		"guest held it (it took %d in the window and %d in a later window) "
		"but took %d (internal=%d) as soon as the guest closed it",
		escalated, mid, is_internal_top, is_internal_mid, guard_top,
		guard_mid, escalated, W.loader_fd_in_window, W.post_window_fd,
		W.reclaimed_fd, closed_reclaimed);

	/* Part 1 is done with its low vectors; the band is clean again */
	close(W.open_in);
	close(W.socket_in);
	close(W.pipe_in[0]);
	close(W.pipe_in[1]);
	close(W.dup_in);
	close(W.open_out);
	close(W.socket_out);
	close(W.pipe_out[0]);
	close(W.pipe_out[1]);
	close(W.dup_out);
	close(W.dupfd_in_fd);
	close(W.reclaimed_fd);
	model_socket_bitmap_put(W.reclaimed_fd);
	printf("INFO band: closed every Part 1 descriptor (the loader's registry "
	       "released its own); the reserved band is free again\n");
	fflush(stdout);
}

/* ------------------------------------------------------------------ */
/* Part 3: lowered soft-limit semantics                                */
/* ------------------------------------------------------------------ */

static void phase_lowered_limit(void)
{
	rlim_t advertised_max = public_fd_hard_limit(W.hard);
	private_fd_allocation_t alloc_s4;
	/*
	 * "High in the range": the top of the guest's advertised range.  The
	 * draft's setter (mldr.c:712-715) refuses current > maximum, and the
	 * advertised maximum is public_fd_hard_limit(rlim_max), so the highest
	 * number a guest descriptor can legitimately occupy is low - 1.
	 */
	int high = (int)W.low - 1;
	int low = (int)(W.low / 4);                      /* the lowered limit */
	int low2 = 64;                                   /* lowered below `low` */
	int src;
	int high_fd = -1;
	int priv = -1;
	int priv_errno = 0;
	rlim_t raw_during = 0;
	rlim_t raw_after = 0;
	uint64_t published_during = 0;
	int deployed_fd = -1;
	int deployed_ok;
	rlim_t raw_during_deployed = 0;
	socket_bitmap_t deployed_bitmap = {
		.mutex = PTHREAD_MUTEX_INITIALIZER,
		.bits = NULL,
		.bit_length = 0,
		.highest = -1,
		.next_index = 0,
	};

	if (low <= 64 || high <= low + 16)
		die("published limit too small for the lowered-limit phase");

	/* ---------------- S1 ---------------- */
	{
		char path[] = "/tmp/limit-window-race-proof.XXXXXX";
		int rc;
		int ok;

		src = mkstemp(path);
		track_fd(src);
		if (src < 0)
			die("mkstemp");
		unlink(path);
		/*
		 * A guest descriptor is opened high in the range first; the soft
		 * limit is then lowered below its number.  (It is created while the
		 * limit still covers it, as any real guest's would have been.)
		 */
		high_fd = dup2(src, high);
		rc = model_set_nofile_limits((uint64_t)low, (uint64_t)advertised_max);
		if (rc != 0) {
			errno = -rc;
			die("model_set_nofile_limits(lower for S1)");
		}
		ok = high_fd == high &&
			read_raw_soft_limit() == (rlim_t)low &&
			model_user_fd_limit() == (uint64_t)low &&
			(rlim_t)high > (rlim_t)low;
		verdict(7, ok, "guest descriptor %d created while the soft limit "
			"was %lu; guest soft limit then lowered to %lu (published "
			"%lu), below the descriptor's number %d",
			high_fd, (unsigned long)W.low, (unsigned long)low,
			(unsigned long)model_user_fd_limit(), high);
	}
	track_fd(high_fd);

	/* ---------------- S2 ---------------- */
	{
		static const char payload[] = "limit-window-race-proof";
		size_t len = sizeof payload - 1;
		ssize_t w = write(high_fd, payload, len);
		off_t pos = lseek(high_fd, 0, SEEK_SET);
		char buf[64];
		ssize_t r = read(high_fd, buf, sizeof buf);
		struct stat st;
		int fr = fstat(high_fd, &st);
		int ok = w == (ssize_t)len && pos == 0 && r == (ssize_t)len &&
			memcmp(buf, payload, len) == 0 && fr == 0 &&
			st.st_size == (off_t)len;

		verdict(8, high_fd >= 0 && ok, "fd %d (above the %lu soft limit): write=%zd "
			"lseek=%lld read=%zd fstat=%d size=%lld; the already-open "
			"descriptor is fully usable",
			high_fd, (unsigned long)low, w, (long long)pos, r, fr,
			(long long)st.st_size);
	}

	/* ---------------- S3 ---------------- */
	{
		int new_low_fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
		int at_rc, above_rc, fd_at, fd_above;
		int e_at, e_above, ef_at, ef_above;

		track_fd(new_low_fd);
		errno = 0;
		at_rc = dup2(src, low);
		e_at = errno;
		errno = 0;
		above_rc = dup2(src, low + 1);
		e_above = errno;
		errno = 0;
		fd_at = fcntl(src, F_DUPFD_CLOEXEC, low);
		ef_at = errno;
		errno = 0;
		fd_above = fcntl(src, F_DUPFD_CLOEXEC, low + 1);
		ef_above = errno;
		if (at_rc >= 0)
			close(at_rc);
		if (above_rc >= 0)
			close(above_rc);
		if (fd_at >= 0)
			close(fd_at);
		if (fd_above >= 0)
			close(fd_above);

		verdict(9, new_low_fd >= 0 && new_low_fd < low &&
			at_rc < 0 && e_at == EBADF &&
			above_rc < 0 && e_above == EBADF &&
			fd_at < 0 && ef_at == EINVAL &&
			fd_above < 0 && ef_above == EINVAL,
			"soft limit %d: new lowest-free open -> %d (below it); "
			"dup2->%d rc=%d errno=%d; dup2->%d rc=%d errno=%d; "
			"F_DUPFD min=%d fd=%d errno=%d; F_DUPFD min=%d fd=%d errno=%d",
			low, new_low_fd, low, at_rc, e_at, low + 1, above_rc,
			e_above, low, fd_at, ef_at, low + 1, fd_above, ef_above);
	}

	/* ---------------- S4 ---------------- */
	{
		rlim_t raw_before = read_raw_soft_limit();
		int rc = model_private_fd_allocation_begin(&alloc_s4);

		raw_during = read_raw_soft_limit();
		if (rc == 0) {
			priv = model_private_alloc(&alloc_s4, g_loader_source);
			priv_errno = priv < 0 ? errno : 0;
			model_private_fd_allocation_end(&alloc_s4);
		} else {
			priv_errno = errno;
		}
		raw_after = read_raw_soft_limit();
		published_during = model_user_fd_limit();
		/*
		 * The deployed loader's rule, measured for contrast: no setrlimit
		 * at all, top-down from rlim_cur - 1.
		 */
		deployed_fd = deployed_adopt_locked(&deployed_bitmap, g_loader_source);
		raw_during_deployed = read_raw_soft_limit();
		if (deployed_fd >= 0)
			close(deployed_fd);
		deployed_ok = deployed_fd >= 0 && deployed_fd < low;

		verdict(10, rc == 0 && priv > low && (rlim_t)priv < W.hard &&
			raw_before == (rlim_t)low && raw_during == W.hard &&
			published_during == (uint64_t)low &&
			raw_after == (rlim_t)low && deployed_ok &&
			raw_during_deployed == (rlim_t)low,
			"guest soft limit %d: the draft's private allocator -> fd %d "
			"(errno=%d) with the real soft raised to %lu while the "
			"published value stayed %lu, then restored to %lu -- this "
			"REQUIRES the real soft to differ from the published one; the "
			"deployed rule (top-down from rlim_cur-1, no setrlimit) "
			"instead landed at %d, inside the guest's advertised range, "
			"with the real soft untouched at %lu",
			low, priv, priv_errno, (unsigned long)raw_during,
			(unsigned long)published_during, (unsigned long)raw_after,
			deployed_fd, (unsigned long)raw_during_deployed);
	}
	track_fd(priv);

	/* ---------------- S5 ---------------- */
	{
		int raw_raise;
		int e_raw_raise;
		int loader_ceiling;
		rlim_t after_raw_raise;
		rlim_t after_loader_ceiling;
		int loader_above, raw_above, cur_gt_max, loader_cur_gt_max;
		int e_raw_above, e_gt_max;
		rlim_t raw_after_failures;
		struct rlimit rl;

		/* raising the soft limit back within the hard limit */
		rl.rlim_cur = W.hard;
		rl.rlim_max = W.hard;
		errno = 0;
		raw_raise = setrlimit(RLIMIT_NOFILE, &rl);
		e_raw_raise = errno;
		after_raw_raise = read_raw_soft_limit();
		/* the loader-mediated setter, back to the advertised maximum */
		loader_ceiling = model_set_nofile_limits((uint64_t)advertised_max,
			(uint64_t)advertised_max);
		after_loader_ceiling = read_raw_soft_limit();

		/* raising the advertised maximum above the hard-derived one */
		loader_above = model_set_nofile_limits((uint64_t)advertised_max,
			(uint64_t)advertised_max + 1);
		/* a guest soft limit above the advertised maximum */
		loader_cur_gt_max = model_set_nofile_limits((uint64_t)W.hard,
			(uint64_t)advertised_max);
		/* and the raw Linux contract for the two impossible raises */
		rl.rlim_cur = W.hard + 1;
		rl.rlim_max = W.hard + 1;
		errno = 0;
		raw_above = setrlimit(RLIMIT_NOFILE, &rl);
		e_raw_above = errno;
		rl.rlim_cur = W.hard + 1;
		rl.rlim_max = W.hard;
		errno = 0;
		cur_gt_max = setrlimit(RLIMIT_NOFILE, &rl);
		e_gt_max = errno;
		raw_after_failures = read_raw_soft_limit();

		verdict(11, raw_raise == 0 && after_raw_raise == W.hard &&
			loader_ceiling == 0 &&
			after_loader_ceiling == (rlim_t)advertised_max &&
			model_user_fd_limit() == (uint64_t)advertised_max &&
			loader_above < 0 && loader_above == -EPERM &&
			loader_cur_gt_max < 0 && loader_cur_gt_max == -EINVAL &&
			raw_above < 0 && e_raw_above == EPERM &&
			cur_gt_max < 0 && e_gt_max == EINVAL &&
			raw_after_failures == (rlim_t)advertised_max && geteuid() != 0,
			"raise soft to the hard limit %lu within the hard limit: raw "
			"setrlimit rc=%d errno=%d -> soft %lu; loader-mediated set "
			"back to the advertised maximum %lu rc=%d -> soft %lu "
			"published %lu; loader-mediated maximum above %lu rc=%d; "
			"loader-mediated soft limit above the maximum (%lu) rc=%d; "
			"raw rlim_max above the hard limit rc=%d errno=%d (%s); raw "
			"rlim_cur > rlim_max rc=%d errno=%d (%s); everything refused "
			"left the soft limit at %lu (uid=%d)",
			(unsigned long)W.hard, raw_raise, e_raw_raise,
			(unsigned long)after_raw_raise,
			(unsigned long)advertised_max, loader_ceiling,
			(unsigned long)after_loader_ceiling,
			(unsigned long)model_user_fd_limit(),
			(unsigned long)advertised_max, loader_above,
			(unsigned long)W.hard, loader_cur_gt_max, raw_above,
			e_raw_above, strerror(e_raw_above), cur_gt_max, e_gt_max,
			strerror(e_gt_max), (unsigned long)raw_after_failures,
			(int)geteuid());
	}

	/* ---------------- S6 ---------------- */
	{
		char byte = 0x5a;
		char got = 0;
		int lowered_rc, internal_before, internal_after, fcntl_rc;
		ssize_t sent, received;
		int open_low;
		uint64_t cur = 0;
		uint64_t max = 0;
		rlim_t raw_now;

		internal_before = model_fd_is_internal(priv) ? 1 : 0;
		fcntl_rc = fcntl(priv, F_GETFD);
		lowered_rc = model_set_nofile_limits((uint64_t)low2,
			(uint64_t)advertised_max);
		raw_now = read_raw_soft_limit();
		internal_after = model_fd_is_internal(priv) ? 1 : 0;
		sent = send(g_loader_peer, &byte, 1, 0);
		received = recv(priv, &got, 1, 0);
		(void)model_get_nofile_limits(&cur, &max);
		open_low = open("/dev/null", O_RDONLY | O_CLOEXEC);
		track_fd(open_low);

		verdict(12, priv >= 0 && lowered_rc == 0 &&
			raw_now == (rlim_t)low2 &&
			model_user_fd_limit() == (uint64_t)low2 &&
			internal_before == 1 && internal_after == 1 &&
			fcntl_rc >= 0 && sent == 1 && received == 1 &&
			got == byte && (rlim_t)priv > (rlim_t)low2 &&
			cur == (uint64_t)low2 && max == (uint64_t)advertised_max &&
			open_low >= 0 && open_low < low2,
			"loader-owned fd %d (internal=%d, above the guest soft limit): "
			"after lowering the soft limit to %d (published %lu) it is "
			"still internal=%d, fcntl=%d, still the same live socket "
			"(send/recv %zd/%zd, byte %02x); the loader's query reports "
			"current=%lu maximum=%lu; a new guest allocation landed at %d",
			priv, internal_before, low2,
			(unsigned long)model_user_fd_limit(), internal_after,
			fcntl_rc, sent, received, (unsigned)(unsigned char)got,
			(unsigned long)cur, (unsigned long)max, open_low);
	}

	free(deployed_bitmap.bits);
}

/* ------------------------------------------------------------------ */
/* main                                                                */
/* ------------------------------------------------------------------ */

int main(int argc, char **argv)
{
	const char *env;

	if (argc > 1 && strcmp(argv[1], "all") != 0) {
		fprintf(stderr, "usage: %s [all]\n", argv[0]);
		return 2;
	}
	env = getenv("LWR_ENV");
	g_env = (env && env[0]) ? env : "unlabeled";
	setvbuf(stdout, NULL, _IOLBF, 0);

	if (getrlimit(RLIMIT_NOFILE, &g_original_limits) < 0)
		die("getrlimit(RLIMIT_NOFILE)");

	printf("HARNESS start env=%s uid=%d raise_soft_limit=%d "
	       "guard_protects_band=%d restore_soft_limit=%d "
	       "publish_raw_limit=%d\n",
	       g_env, (int)geteuid(), MODEL_RAISE_SOFT_LIMIT,
	       MODEL_GUARD_PROTECTS_BAND, MODEL_RESTORE_SOFT_LIMIT,
	       MODEL_PUBLISH_RAW_LIMIT);
	printf("INFO model: the draft's sequence (mldr.c:637-678) with "
	       "setrlimit(RLIMIT_NOFILE) executed for real; the guard is the "
	       "deployed dup2.c:23 / close.c:32 contract over "
	       "__mldr_fd_is_internal (mldr.c:826-839)\n");
	fflush(stdout);

	phase_window();
	phase_lowered_limit();

	print_claims();

	close_tracked();
	free(socket_bitmap.bits);
	(void)setrlimit(RLIMIT_NOFILE, &g_original_limits);
	return all_ok() ? 0 : 1;
}
