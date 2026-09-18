/*
 * fd-semantics-proof.c -- is a "virtual soft limit with post-filter" scheme
 * semantics-preserving, per descriptor-creating class?
 *
 * The proposed scheme (workspace design doc
 * docs/direct-transport-descriptor-architecture.md, option A4.2) keeps the real
 * RLIMIT_NOFILE soft limit high, publishes a lower "virtual" limit V to the
 * guest, and enforces it in userspace by calling the syscall and then closing
 * the returned descriptor with EMFILE if the number is >= V.
 *
 * This harness measures, for every creator a Darling guest can reach, whether
 * that post-filter is observably equivalent to the kernel's own behaviour at a
 * real soft limit of V.  Each case runs the same operation in two fresh
 * children that differ only in how the limit is enforced:
 *
 *   LEG_NATIVE      real soft limit == V.  The wrapper is the identity, so the
 *                   kernel itself decides.  This is the reference semantics.
 *   LEG_POST        real soft limit == REAL_LIMIT (> V).  The wrapper performs
 *                   the syscall and post-filters: a returned number >= V is
 *                   closed and reported as EMFILE.
 *
 * The below-limit range is filled before the operation, so in both legs the
 * guest-visible descriptor space is full.  Whatever differs between the two
 * children is therefore the post-filter's error, not a limit artefact.
 *
 * Two further legs carry the mechanism analysis:
 *   LEG_RACE        the wrapper pre-checks for a free below-limit slot, closes
 *                   its probe, and another thread (deterministic hand-off, not
 *                   a natural race) takes that slot before the syscall runs.
 *   LEG_BOUNDED     SCM_RIGHTS only: the wrapper sizes the kernel-facing
 *                   control buffer to the number of free below-limit slots
 *                   before the call and lets the kernel's own MSG_CTRUNC path
 *                   drop and close the excess descriptors.
 *
 * Claims (one line each, in both environments):
 *   S1  regime: the native children ran at soft == V and the wrapper children
 *       at soft == REAL_LIMIT, with V == 64 and REAL_LIMIT == 96.
 *   C1  openat(O_CREAT new path)            NOT equivalent
 *   C2  openat(O_TRUNC existing file)       NOT equivalent
 *   C3  openat(O_CREAT|O_EXCL new path)     NOT equivalent
 *   C4  accept4() with a queued connection  NOT equivalent
 *   C5  recvmsg() + SCM_RIGHTS (dgram)      NOT equivalent
 *   C6  recvmsg() + SCM_RIGHTS (stream)     NOT equivalent
 *   C7  pipe2()                             equivalent
 *   C8  socket() (AF_UNIX and AF_INET)      equivalent
 *   C9  socketpair()                        equivalent
 *   C10 eventfd()                           equivalent
 *   C11 epoll_create() (libkqueue's kqueue backend) equivalent
 *   C12 inotify_init1()                     equivalent
 *   C13 signalfd()                          equivalent
 *   C14 timerfd_create()                    equivalent
 *   C15 SCM_RIGHTS, no free slot: the bounded-delivery scheme is equivalent
 *   C16 SCM_RIGHTS, one free slot, two descriptors: the naive post-filter is
 *       NOT equivalent; the bounded-delivery scheme is
 *   C17 openat(O_TRUNC) under the check-to-act interleaving: NOT equivalent
 *       even for a pre-check scheme
 *   C18 accept4() under the same interleaving: NOT equivalent
 *
 * The harness must be able to fail.  Three compile-time mutations of the model
 * (built by the runner in its temporary directory) must turn named claims red:
 *   MUT_NO_CLOSE_AFTER_REJECT  the post-filter forgets close()          C1..C14 red
 *   MUT_NATIVE_LIMIT_RAISED    the native leg never lowers the real limit
 *                                                                  C1..C7, C15 red
 *   MUT_PRECHECK_FOR_SINGLE    the wrapper pre-checks instead of post-filtering
 *                                                                       C1..C4 red
 *
 * Prints one line per claim, HARNESS OK when every claim passed, and exits
 * non-zero otherwise.  No project code is touched; all scratch state lives in a
 * mkdtemp() directory owned by this process.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/eventfd.h>
#include <sys/inotify.h>
#include <sys/resource.h>
#include <sys/signalfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/timerfd.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

/* ---------------------------------------------------------------- mutations */
#ifndef MUT_NO_CLOSE_AFTER_REJECT
#define MUT_NO_CLOSE_AFTER_REJECT 0
#endif
#ifndef MUT_NATIVE_LIMIT_RAISED
#define MUT_NATIVE_LIMIT_RAISED 0
#endif
#ifndef MUT_PRECHECK_FOR_SINGLE
#define MUT_PRECHECK_FOR_SINGLE 0
#endif

/* ------------------------------------------------------------------ regime */
#define V_LIMIT 64	/* the guest-visible ("virtual") soft limit */
#define REAL_LIMIT 96	/* the real soft limit the wrapper scheme runs at */
#define PRE_SIZE 4096	/* size of the file the O_TRUNC cases start from */
#define PAYLOAD_LEN 5
#define FD_TABLE_MAX 1024

enum { LEG_NATIVE = 0, LEG_POST = 1, LEG_RACE = 2, LEG_BOUNDED = 3 };
static const char *const leg_name[] = { "native", "postfilter", "race", "bounded" };

enum {
	C1_OPEN_CREAT = 1, C2_OPEN_TRUNC, C3_OPEN_EXCL, C4_ACCEPT, C5_SCM_DGRAM,
	C6_SCM_STREAM, C7_PIPE2, C8_SOCKET, C9_SOCKETPAIR, C10_EVENTFD, C11_EPOLL,
	C12_INOTIFY, C13_SIGNALFD, C14_TIMERFD, C15_SCM_BOUNDED0, C16_SCM_BOUNDED1,
	C17_RACE_OPEN_TRUNC, C18_RACE_ACCEPT, C_MAX
};

struct obs {
	int child_ok;	/* the child completed and delivered this record */
	int soft;	/* soft limit measured in the child after the leg is set up */
	int rc, err;	/* primary syscall result */
	int fd, fd2;	/* descriptor numbers it installed */
	int rc2, err2;	/* second variant of a creator (C8: AF_INET) */
	int exists, size;	/* filesystem observation */
	int peer_rc, peer_err;	/* accept: the peer's view */
	int stage2_rc, stage2_err;	/* the retried operation */
	int consumed;	/* the queued message/connection was consumed */
	int ctrunc, ctrl_fds, ctrl_bytes, data;	/* SCM_RIGHTS observation */
	int free_before, free_after;	/* free descriptor numbers below the limit */
	int extra_before, extra_after;	/* occupied numbers at or above the bound */
	int precheck_fd;	/* the number the wrapper's pre-check saw */
	int race_taken;	/* the number the interleaving thread took */
	int early;	/* the pre-check mutation returned before the syscall */
};

static int dirfd_ = -1;		/* pre-opened /proc/self/fd */
static int nullfd_ = -1;	/* filler source */

static void fatal(const char *what)
{
	perror(what);
	_exit(90);
}

static void req(int ok, const char *what)
{
	if (!ok)
		fatal(what);
}

/* ------------------------------------------------------------- descriptors */

/* Occupy every descriptor number below `bound`.  The kernel hands out the
 * lowest free number (fs/file.c: alloc_fd()), so this fills the range without
 * knowing anything about the current table. */
static void fill_to(int bound)
{
	for (;;) {
		int fd = dup(nullfd_);
		if (fd < 0)
			break;
		if (fd >= bound) {
			close(fd);
			break;
		}
	}
}

/* Occupancy of the real descriptor table, read through an already-open dirfd so
 * that the scan itself allocates nothing (it must work while the table is
 * full). */
static void scan_occ(int end, int *free_low, int *extra)
{
	static unsigned char seen[FD_TABLE_MAX];
	char buf[8192];
	long r;
	int i, free_n = 0, ex = 0;

	memset(seen, 0, sizeof seen);
	if (lseek(dirfd_, 0, SEEK_SET) < 0)
		fatal("lseek /proc/self/fd");
	while ((r = syscall(SYS_getdents64, dirfd_, buf, sizeof buf)) > 0) {
		long off = 0;
		while (off < r) {
			struct {
				unsigned long d_ino;
				long d_off;
				unsigned short d_reclen;
				unsigned char d_type;
				char d_name[];
			} *d = (void *)(buf + off);
			int fd = atoi(d->d_name);
			if (fd >= 0 && fd < FD_TABLE_MAX)
				seen[fd] = 1;
			off += d->d_reclen;
		}
	}
	for (i = 0; i < end; i++)
		if (!seen[i])
			free_n++;
	for (i = end; i < FD_TABLE_MAX; i++)
		if (seen[i])
			ex++;
	*free_low = free_n;
	*extra = ex;
	if (getenv("FSP_DEBUG")) {
		char line[4096];
		int off2 = 0;
		off2 += snprintf(line + off2, sizeof line - (size_t)off2, "scan end=%d occupied:", end);
		for (i = 0; i < FD_TABLE_MAX && off2 < 3900; i++)
			if (seen[i])
				off2 += snprintf(line + off2, sizeof line - (size_t)off2, " %d", i);
		fprintf(stderr, "%s\n", line);
	}
}

/* --------------------------------------------------------------- policies */

/* Post-filter (option A4.2): every descriptor at or above the published limit
 * is closed and the call is reported as EMFILE. */
static void post_filter(const int *fds, int n, struct obs *o)
{
	int i, high = 0;

	if (fds[0] < 0) {
		o->rc = -1;
		o->err = errno;
		return;
	}
	for (i = 0; i < n; i++)
		if (fds[i] >= V_LIMIT)
			high = 1;
	if (high) {
		for (i = 0; i < n; i++)
			if (!MUT_NO_CLOSE_AFTER_REJECT)
				close(fds[i]);
		o->fd = o->fd2 = -1;
		o->rc = -1;
		o->err = EMFILE;
		errno = EMFILE;
		return;
	}
	o->fd = fds[0];
	o->fd2 = (n > 1) ? fds[1] : -1;
	o->rc = fds[0];
	o->err = 0;
}

static int post_filter_one(int fd, struct obs *o)
{
	int fds[1];

	fds[0] = fd;
	post_filter(fds, 1, o);
	return o->rc;
}

/* Pre-check: reserve n below-limit slots before the syscall and release them.
 * Returns 0 when the operation may proceed, -1 when the guest space is full
 * (in which case the syscall must not be performed at all). */
static int precheck(int n)
{
	int i, got[4];

	for (i = 0; i < n; i++) {
		int fd = dup(nullfd_);
		if (fd < 0 || fd >= V_LIMIT) {
			if (fd >= 0)
				close(fd);
			while (i-- > 0)
				close(got[i]);
			return -1;
		}
		got[i] = fd;
	}
	for (i = 0; i < n; i++)
		close(got[i]);
	return 0;
}

/* Deterministic hand-off to a second thread that takes the lowest free slot the
 * pre-check just released.  This models a concurrent guest thread; it is not a
 * natural race and is not presented as one. */
static atomic_int race_go_, race_done_, race_taken_;

static void *racer_fn(void *unused)
{
	int fd;

	(void)unused;
	while (!atomic_load_explicit(&race_go_, memory_order_acquire))
		;
	fd = dup(nullfd_);
	atomic_store(&race_taken_, fd);
	atomic_store_explicit(&race_done_, 1, memory_order_release);
	return NULL;
}

static int race_steal_one_slot(void)
{
	pthread_t th;

	atomic_store(&race_go_, 0);
	atomic_store(&race_done_, 0);
	atomic_store(&race_taken_, -1);
	req(pthread_create(&th, NULL, racer_fn, NULL) == 0, "pthread_create");
	atomic_store_explicit(&race_go_, 1, memory_order_release);
	while (!atomic_load_explicit(&race_done_, memory_order_acquire))
		;
	req(pthread_join(th, NULL) == 0, "pthread_join");
	return atomic_load(&race_taken_);
}

static void set_leg_limits(int leg)
{
	struct rlimit rl;
	unsigned soft = (leg == LEG_NATIVE)
		? (MUT_NATIVE_LIMIT_RAISED ? REAL_LIMIT : V_LIMIT)
		: REAL_LIMIT;

	rl.rlim_cur = rl.rlim_max = soft;
	req(setrlimit(RLIMIT_NOFILE, &rl) == 0, "setrlimit");
}

/* Shared prologue: install the leg's limit, fill the guest-visible range up to
 * `fill_end`, run the race leg's pre-check plus the interleaving, and record the
 * occupancy the operation starts from (measured against `scan_end`). */
static void arm(int leg, int fill_end, struct obs *o, int precheck_n)
{
	struct rlimit rl;

	set_leg_limits(leg);
	req(getrlimit(RLIMIT_NOFILE, &rl) == 0, "getrlimit");
	o->soft = (int)rl.rlim_cur;
	fill_to(fill_end);
	if (leg == LEG_RACE) {
		int p = dup(nullfd_);
		o->precheck_fd = p;
		if (p >= 0)
			close(p);
		o->race_taken = race_steal_one_slot();
	}
	if (leg == LEG_POST && MUT_PRECHECK_FOR_SINGLE && precheck_n > 0 &&
	    precheck(precheck_n) < 0) {
		o->rc = -1;
		o->err = EMFILE;
		o->early = 1;
	}
	scan_occ(V_LIMIT, &o->free_before, &o->extra_before);
}

/* Restore the regime's occupancy so that both legs end in the same state, so a
 * leaked or silently freed descriptor cannot hide inside the difference. */
static void finish(struct obs *o, int refill_end)
{
	fill_to(refill_end);
	scan_occ(V_LIMIT, &o->free_after, &o->extra_after);
}

/* ------------------------------------------------------------------- cases */

static void write_image(const char *path)
{
	char img[PRE_SIZE];
	int fd;

	memset(img, 'A', sizeof img);
	(void)unlink(path);
	fd = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0600);
	req(fd >= 0, "create pre-existing file");
	req(write(fd, img, sizeof img) == (ssize_t)sizeof img, "write pre-existing file");
	close(fd);
}

/* C1/C2/C3 and the race variant C17 */
static void case_open(int id, int leg, const char *dir, struct obs *o)
{
	char path[512];
	struct stat st;
	struct rlimit rl;
	int flags, pre = 0, fd;

	switch (id) {
	case C1_OPEN_CREAT:
		flags = O_CREAT | O_WRONLY;
		break;
	case C2_OPEN_TRUNC:
	case C17_RACE_OPEN_TRUNC:
		flags = O_WRONLY | O_TRUNC;
		pre = 1;
		break;
	default:
		flags = O_CREAT | O_EXCL | O_WRONLY;
		break;
	}
	snprintf(path, sizeof path, "%s/open_%d_%s", dir, id, leg_name[leg]);
	if (pre)
		write_image(path);
	else
		(void)unlink(path);

	/* The race leg leaves one free slot for the pre-check to find; the act
	 * itself then runs with the guest-visible range full. */
	if (leg == LEG_RACE)
		arm(leg, V_LIMIT - 1, o, 0);
	else
		arm(leg, V_LIMIT, o, 1);

	if (!o->early) {
		errno = 0;
		fd = open(path, flags, 0600);
		post_filter_one(fd, o);
	}

	o->exists = (stat(path, &st) == 0);
	o->size = o->exists ? (int)st.st_size : -1;
	if (o->rc >= 0 && o->rc < V_LIMIT) {
		close(o->rc);
		o->fd = o->rc;	/* keep the number for the record */
	}
	req(getrlimit(RLIMIT_NOFILE, &rl) == 0, "getrlimit");
	o->soft = (int)rl.rlim_cur;
	finish(o, V_LIMIT);
}

/* C4 and the race variant C18 */
static void case_accept(int id, int leg, const char *dir, struct obs *o)
{
	char sockpath[512];
	struct sockaddr_un sa;
	char c;
	int lfd, cfd, fd;
	ssize_t r;

	snprintf(sockpath, sizeof sockpath, "%s/accept_%d_%s.sock", dir, id, leg_name[leg]);
	(void)unlink(sockpath);
	lfd = socket(AF_UNIX, SOCK_STREAM, 0);
	req(lfd >= 0, "listener socket");
	memset(&sa, 0, sizeof sa);
	sa.sun_family = AF_UNIX;
	{
		size_t plen = strlen(sockpath);
		req(plen < sizeof sa.sun_path, "AF_UNIX path too long");
		memcpy(sa.sun_path, sockpath, plen + 1);
	}
	req(bind(lfd, (void *)&sa, sizeof sa) == 0, "bind");
	req(listen(lfd, 4) == 0, "listen");
	/* the retry accept must not block on an emptied queue */
	req(fcntl(lfd, F_SETFL, O_NONBLOCK) == 0, "F_SETFL listener");
	cfd = socket(AF_UNIX, SOCK_STREAM, 0);
	req(cfd >= 0, "client socket");
	/* An AF_UNIX stream connect queues the connection on the listener without
	 * the server accepting it: net/unix/af_unix.c unix_stream_connect() waits
	 * only when the listener's receive queue is full. */
	req(connect(cfd, (void *)&sa, sizeof sa) == 0, "connect AF_UNIX stream");

	if (leg == LEG_RACE)
		arm(leg, V_LIMIT - 1, o, 0);
	else
		arm(leg, V_LIMIT, o, 1);

	if (!o->early) {
		errno = 0;
		fd = accept4(lfd, NULL, NULL, SOCK_NONBLOCK);
		post_filter_one(fd, o);
	}

	/* Peer observation: a consumed-and-closed connection reads as EOF (or RST)
	 * on the client; a still-queued one reads as EAGAIN. */
	errno = 0;
	r = recv(cfd, &c, 1, MSG_DONTWAIT);
	o->peer_rc = (int)r;
	o->peer_err = errno;

	/* Queue observation: free exactly one below-limit slot (every number below
	 * V is occupied) and accept again. */
	close(V_LIMIT - 1);
	errno = 0;
	fd = accept4(lfd, NULL, NULL, SOCK_NONBLOCK);
	o->stage2_rc = fd;
	o->stage2_err = errno;
	if (fd >= 0 && fd < V_LIMIT)
		close(fd);
	o->consumed = (o->peer_rc == 0 || o->peer_err == ECONNRESET);
	finish(o, V_LIMIT);
	close(cfd);
	close(lfd);
}

/* C5/C6 (naive post-filter, dgram and stream) and C15/C16 (bounded delivery).
 * C15/C16 carry two descriptors; C16 leaves exactly one below-limit slot free. */
static void case_scm(int id, int leg, const char *dir, struct obs *o)
{
	int dgram = (id == C5_SCM_DGRAM);
	int nfds = 2;
	int partial = (id == C16_SCM_BOUNDED1);
	int fill_end = partial ? V_LIMIT - 1 : V_LIMIT;
	int sv[2], payload[4], i, k = 0;
	char cbuf[CMSG_SPACE(sizeof(int) * 4)];
	char data[32];
	struct iovec iov;
	struct msghdr msg;
	struct cmsghdr *cm;
	ssize_t rc;

	req(socketpair(AF_UNIX, dgram ? SOCK_DGRAM : SOCK_STREAM, 0, sv) == 0, "socketpair");
	for (i = 0; i < nfds; i++) {
		char p[512];
		snprintf(p, sizeof p, "%s/scm_payload_%d_%s_%d", dir, id, leg_name[leg], i);
		payload[i] = open(p, O_CREAT | O_RDWR, 0600);
		req(payload[i] >= 0, "payload file");
	}

	arm(leg, fill_end, o, 0);

	/* Queue one message carrying nfds descriptors. */
	memset(&msg, 0, sizeof msg);
	iov.iov_base = (void *)"hello";
	iov.iov_len = PAYLOAD_LEN;
	msg.msg_iov = &iov;
	msg.msg_iovlen = 1;
	msg.msg_control = cbuf;
	msg.msg_controllen = CMSG_SPACE(sizeof(int) * nfds);
	cm = CMSG_FIRSTHDR(&msg);
	cm->cmsg_level = SOL_SOCKET;
	cm->cmsg_type = SCM_RIGHTS;
	cm->cmsg_len = CMSG_LEN(sizeof(int) * nfds);
	memcpy(CMSG_DATA(cm), payload, sizeof(int) * nfds);
	req(sendmsg(sv[0], &msg, 0) == PAYLOAD_LEN, "sendmsg SCM_RIGHTS");
	req(fcntl(sv[1], F_SETFL, O_NONBLOCK) == 0, "F_SETFL");

	/* The wrapper has to decide the kernel-facing control buffer BEFORE the
	 * call.  The bounded scheme sizes it to the number of free below-limit
	 * slots; the naive post-filter passes the guest's own request. */
	if (leg == LEG_BOUNDED)
		k = o->free_before;	/* free below-limit slots, measured before the call */

	memset(cbuf, 0, sizeof cbuf);
	memset(data, 0, sizeof data);
	o->fd = o->fd2 = -1;
	iov.iov_base = data;
	iov.iov_len = sizeof data;
	memset(&msg, 0, sizeof msg);
	msg.msg_iov = &iov;
	msg.msg_iovlen = 1;
	msg.msg_control = cbuf;
	msg.msg_controllen = sizeof cbuf;
	if (leg == LEG_BOUNDED && k == 0) {
		msg.msg_control = NULL;
		msg.msg_controllen = 0;
	} else if (leg == LEG_BOUNDED) {
		/* sizeof(struct cmsghdr) + k*sizeof(int) is exactly what makes the
		 * kernel's scm_max_fds() come out as k */
		msg.msg_controllen = sizeof(struct cmsghdr) + (size_t)k * sizeof(int);
	}
	errno = 0;
	rc = recvmsg(sv[1], &msg, 0);
	o->rc = (int)rc;
	o->err = rc < 0 ? errno : 0;
	o->ctrunc = !!(msg.msg_flags & MSG_CTRUNC);
	o->ctrl_bytes = (int)msg.msg_controllen;
	o->data = rc > 0 ? (int)rc : 0;
	o->ctrl_fds = 0;
	cm = (msg.msg_controllen > 0) ? CMSG_FIRSTHDR(&msg) : NULL;
	if (cm && cm->cmsg_level == SOL_SOCKET && cm->cmsg_type == SCM_RIGHTS) {
		o->ctrl_fds = (int)((cm->cmsg_len - CMSG_LEN(0)) / sizeof(int));
		for (i = 0; i < o->ctrl_fds; i++) {
			int got;
			memcpy(&got, CMSG_DATA(cm) + i * sizeof(int), sizeof got);
			if (i == 0)
				o->fd = got;
			else
				o->fd2 = got;
		}
	}

	/* Post-filter policy for SCM_RIGHTS: if any installed descriptor is at or
	 * above the published limit, close every descriptor the kernel installed
	 * and report EMFILE.  Native and bounded legs keep the below-limit ones
	 * (they are closed here only to normalise the occupancy). */
	if (leg == LEG_POST && rc >= 0 && o->ctrl_fds > 0) {
		int high = 0, j;
		for (j = 0; j < o->ctrl_fds; j++) {
			int got;
			memcpy(&got, CMSG_DATA(cm) + j * sizeof(int), sizeof got);
			if (got >= V_LIMIT)
				high = 1;
		}
		for (j = 0; j < o->ctrl_fds; j++) {
			int got;
			memcpy(&got, CMSG_DATA(cm) + j * sizeof(int), sizeof got);
			if (high && !MUT_NO_CLOSE_AFTER_REJECT)
				close(got);
			else if (!high && got < V_LIMIT)
				close(got);
		}
		if (high) {
			o->rc = -1;
			o->err = EMFILE;
		}
	} else if (rc >= 0 && o->ctrl_fds > 0) {
		int j;
		for (j = 0; j < o->ctrl_fds; j++) {
			int got;
			memcpy(&got, CMSG_DATA(cm) + j * sizeof(int), sizeof got);
			close(got);
		}
	}
	/* The wrapper owns the guest's msghdr: it reports the control bytes a
	 * native call with the guest's buffer would have written (the kernel's
	 * internal advance is capped by the wrapper's own smaller buffer). */
	if (leg == LEG_BOUNDED)
		o->ctrl_bytes = (o->ctrl_fds > 0) ? CMSG_SPACE(o->ctrl_fds * sizeof(int)) : 0;

	/* Consumption test: the kernel consumes the message even when it cannot
	 * install the descriptors (that is what MSG_CTRUNC means). */
	errno = 0;
	iov.iov_len = sizeof data;
	memset(&msg, 0, sizeof msg);
	msg.msg_iov = &iov;
	msg.msg_iovlen = 1;
	rc = recvmsg(sv[1], &msg, MSG_DONTWAIT);
	o->consumed = (rc < 0 && errno == EAGAIN);

	finish(o, fill_end);
	for (i = 0; i < nfds; i++)
		close(payload[i]);
	close(sv[0]);
	close(sv[1]);
}

/* C7..C14: creators whose object is never nameable from outside. */
static void case_creator(int id, int leg, const char *dir, struct obs *o)
{
	int fds[2], fd, r, i;
	sigset_t mask;

	(void)dir;
	arm(leg, V_LIMIT, o, (id == C7_PIPE2 || id == C9_SOCKETPAIR) ? 2 : 1);
	if (o->early) {
		/* the pre-check mutation refuses every creator of this class */
		o->rc2 = -1;
		o->err2 = EMFILE;
		goto done;
	}

	switch (id) {
	case C7_PIPE2:
		errno = 0;
		r = pipe2(fds, O_CLOEXEC);
		if (r == 0)
			post_filter(fds, 2, o);
		else
			post_filter((int[]){ -1 }, 1, o);
		break;
	case C9_SOCKETPAIR:
		errno = 0;
		r = socketpair(AF_UNIX, SOCK_STREAM, 0, fds);
		if (r == 0)
			post_filter(fds, 2, o);
		else
			post_filter((int[]){ -1 }, 1, o);
		break;
	case C8_SOCKET:
		errno = 0;
		fd = socket(AF_UNIX, SOCK_STREAM, 0);
		post_filter_one(fd, o);
		{
			/* the second variant is recorded separately */
			struct obs t = *o;
			errno = 0;
			fd = socket(AF_INET, SOCK_STREAM, 0);
			post_filter_one(fd, &t);
			if (t.rc >= 0)
				close(t.rc);
			o->rc2 = t.rc;
			o->err2 = t.err;
		}
		break;
	case C10_EVENTFD:
		errno = 0;
		post_filter_one(eventfd(0, EFD_CLOEXEC), o);
		break;
	case C11_EPOLL:
		/* libkqueue's Linux backend opens the guest's kqueue with
		 * epoll_create(1) (src/external/libkqueue/src/linux/platform.c:77 in
		 * the deployed forest); this is that call. */
		errno = 0;
		post_filter_one(epoll_create(1), o);
		break;
	case C12_INOTIFY:
		errno = 0;
		post_filter_one(inotify_init1(IN_CLOEXEC), o);
		break;
	case C13_SIGNALFD:
		sigemptyset(&mask);
		errno = 0;
		post_filter_one(signalfd(-1, &mask, SFD_CLOEXEC), o);
		break;
	case C14_TIMERFD:
	default:
		errno = 0;
		post_filter_one(timerfd_create(CLOCK_MONOTONIC, TFD_CLOEXEC), o);
		break;
	}

done:
	/* close whatever stayed open below the limit, to normalise the occupancy */
	for (i = 0; i < 2; i++) {
		int *cand = (i == 0) ? &o->fd : &o->fd2;
		if (*cand >= 0 && *cand < V_LIMIT)
			close(*cand);
		*cand = -1;
	}
	finish(o, V_LIMIT);
}

static void run_case(int id, int leg, const char *dir, struct obs *o)
{
	memset(o, 0, sizeof *o);
	o->fd = o->fd2 = -1;
	o->size = -1;
	switch (id) {
	case C1_OPEN_CREAT:
	case C2_OPEN_TRUNC:
	case C3_OPEN_EXCL:
	case C17_RACE_OPEN_TRUNC:
		case_open(id, leg, dir, o);
		break;
	case C4_ACCEPT:
	case C18_RACE_ACCEPT:
		case_accept(id, leg, dir, o);
		break;
	case C5_SCM_DGRAM:
	case C6_SCM_STREAM:
	case C15_SCM_BOUNDED0:
	case C16_SCM_BOUNDED1:
		case_scm(id, leg, dir, o);
		break;
	default:
		case_creator(id, leg, dir, o);
		break;
	}
}

/* ---------------------------------------------------------------- children */

static struct obs run_child(int id, int leg, const char *dir)
{
	struct obs o;
	int p[2], status;
	pid_t pid;
	ssize_t got = 0, n;

	memset(&o, 0, sizeof o);
	o.fd = o.fd2 = -1;
	o.size = -1;
	req(pipe(p) == 0, "pipe");
	pid = fork();
	req(pid >= 0, "fork");
	if (pid == 0) {
		struct obs r;
		int fd;

		close(p[0]);
		/* Isolate the descriptor space: only stdio and the result pipe
		 * survive, so any descriptor at or above the bound found later was
		 * created by the case under test.  /proc/self/fd must be opened here:
		 * the magic symlink is resolved when it is opened, so a descriptor
		 * inherited from the parent would read the parent's table. */
		for (fd = 3; fd < FD_TABLE_MAX; fd++)
			if (fd != p[1])
				(void)close(fd);
		dirfd_ = open("/proc/self/fd", O_RDONLY | O_DIRECTORY);
		nullfd_ = open("/dev/null", O_RDONLY);
		req(dirfd_ >= 0 && nullfd_ >= 0, "open /proc/self/fd and /dev/null");
		run_case(id, leg, dir, &r);
		n = write(p[1], &r, sizeof r);
		(void)n;
		_exit(0);
	}
	close(p[1]);
	while (got < (ssize_t)sizeof o) {
		n = read(p[0], (char *)&o + got, sizeof o - got);
		if (n <= 0)
			break;
		got += n;
	}
	close(p[0]);
	req(waitpid(pid, &status, 0) == pid, "waitpid");
	if (got != (ssize_t)sizeof o || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		memset(&o, 0, sizeof o);
		o.fd = o.fd2 = -1;
		o.size = -1;
		o.soft = -1;
		o.child_ok = 0;
	} else {
		o.child_ok = 1;
	}
	return o;
}

/* ---------------------------------------------------------------- verdicts */

static int claims_failed;

static void fmt(char *buf, size_t n, const struct obs *o)
{
	snprintf(buf, n,
		 "rc=%d errno=%s soft=%d fd=%d exists=%d size=%d peer=%d/%s stage2=%d/%s "
		 "consumed=%d ctrunc=%d ctrlfds=%d ctrlbytes=%d data=%d "
		 "free=%d->%d extra=%d->%d precheck=%d race_taken=%d "
		 "rc2=%d errno2=%s%s",
		 o->rc, o->err ? strerror(o->err) : "-", o->soft, o->fd, o->exists, o->size,
		 o->peer_rc, o->peer_err ? strerror(o->peer_err) : "-",
		 o->stage2_rc, o->stage2_err ? strerror(o->stage2_err) : "-",
		 o->consumed, o->ctrunc, o->ctrl_fds, o->ctrl_bytes, o->data,
		 o->free_before, o->free_after, o->extra_before, o->extra_after,
		 o->precheck_fd, o->race_taken, o->rc2,
		 o->err2 ? strerror(o->err2) : "-", o->child_ok ? "" : " CHILD-FAILED");
}

/* The guest-visible range started and ended in the configured state (expect_free
 * free slots below the limit) and nothing at or above the limit was leaked. */
static int invariants(const struct obs *o, int expect_free)
{
	return o->child_ok && o->free_before == expect_free &&
	       o->free_after == expect_free && o->extra_before == 0 && o->extra_after == 0;
}

static void claim(int id, const char *name, int ok, const char *fmtstr, ...)
{
	va_list ap;
	char text[4096];
	int off;

	off = snprintf(text, sizeof text, "C%d %s %s: ", id, ok ? "PASS" : "FAIL", name);
	va_start(ap, fmtstr);
	vsnprintf(text + off, sizeof text - (size_t)off, fmtstr, ap);
	va_end(ap);
	printf("%s\n", text);
	if (!ok)
		claims_failed++;
	fflush(stdout);
}

int main(void)
{
	char dir[] = "/tmp/fd-semantics-proof.XXXXXX";
	char b1[1400], b2[1400], b3[1400];
	struct rlimit rl;
	static struct obs nat[C_MAX], post[C_MAX], race[C_MAX], bounded[C_MAX];
	int id;

	req(mkdtemp(dir) != NULL, "mkdtemp");
	req(getrlimit(RLIMIT_NOFILE, &rl) == 0, "getrlimit");
#define INIT_ARR(a)                                                       \
	for (id = 0; id < C_MAX; id++) {                                  \
		memset(&(a)[id], 0, sizeof((a)[id]));                     \
		(a)[id].fd = (a)[id].fd2 = -1;                            \
		(a)[id].size = -1;                                        \
	}
	INIT_ARR(nat)
	INIT_ARR(post)
	INIT_ARR(race)
	INIT_ARR(bounded)
#undef INIT_ARR

	for (id = 1; id < C_MAX; id++) {
		nat[id] = run_child(id, LEG_NATIVE, dir);
		if (id == C15_SCM_BOUNDED0) {
			bounded[id] = run_child(id, LEG_BOUNDED, dir);
		} else if (id == C16_SCM_BOUNDED1) {
			bounded[id] = run_child(id, LEG_BOUNDED, dir);
			post[id] = run_child(id, LEG_POST, dir);
		} else if (id == C17_RACE_OPEN_TRUNC || id == C18_RACE_ACCEPT) {
			race[id] = run_child(id, LEG_RACE, dir);
		} else {
			post[id] = run_child(id, LEG_POST, dir);
		}
	}

	/* S1: the regime that was actually measured. */
	{
		int ok = rl.rlim_max >= REAL_LIMIT &&
			 nat[C1_OPEN_CREAT].child_ok && post[C1_OPEN_CREAT].child_ok &&
			 nat[C1_OPEN_CREAT].soft == V_LIMIT &&
			 post[C1_OPEN_CREAT].soft == REAL_LIMIT;
		printf("S1 %s regime: initial soft=%lu hard=%lu, V=%d, REAL_LIMIT=%d, "
		       "native child soft=%d, wrapper child soft=%d\n",
		       ok ? "PASS" : "FAIL", (unsigned long)rl.rlim_cur,
		       (unsigned long)rl.rlim_max, V_LIMIT, REAL_LIMIT,
		       nat[C1_OPEN_CREAT].soft, post[C1_OPEN_CREAT].soft);
		if (!ok)
			claims_failed++;
		fflush(stdout);
	}

	/* C1..C3: open() with a filesystem side effect. */
	{
		struct obs *n = &nat[C1_OPEN_CREAT], *p = &post[C1_OPEN_CREAT];
		int ok = invariants(n, 0) && invariants(p, 0) && n->soft == V_LIMIT &&
			 p->soft == REAL_LIMIT && n->soft == V_LIMIT &&
			 p->soft == REAL_LIMIT && n->rc == -1 && n->err == EMFILE &&
			 n->exists == 0 && p->rc == -1 && p->err == EMFILE && p->exists == 1;
		fmt(b1, sizeof b1, n);
		fmt(b2, sizeof b2, p);
		claim(1, "openat(O_CREAT,new)", ok,
		      "native{%s} postfilter{%s} => %s; observable side effect: an empty regular "
		      "file was created and left behind (native leaves ENOENT)", b1, b2,
		      ok ? "NOT EQUIVALENT" : "UNEXPECTED");
	}
	{
		struct obs *n = &nat[C2_OPEN_TRUNC], *p = &post[C2_OPEN_TRUNC];
		int ok = invariants(n, 0) && invariants(p, 0) && n->soft == V_LIMIT &&
			 p->soft == REAL_LIMIT && n->rc == -1 && n->err == EMFILE &&
			 n->exists == 1 && n->size == PRE_SIZE && p->rc == -1 &&
			 p->err == EMFILE && p->size == 0;
		fmt(b1, sizeof b1, n);
		fmt(b2, sizeof b2, p);
		claim(2, "openat(O_TRUNC,existing)", ok,
		      "native{%s} postfilter{%s} => %s; observable side effect: the file's "
		      "contents were destroyed (native leaves size %d)", b1, b2,
		      ok ? "NOT EQUIVALENT" : "UNEXPECTED", PRE_SIZE);
	}
	{
		struct obs *n = &nat[C3_OPEN_EXCL], *p = &post[C3_OPEN_EXCL];
		int ok = invariants(n, 0) && invariants(p, 0) && n->soft == V_LIMIT &&
			 p->soft == REAL_LIMIT && n->rc == -1 && n->err == EMFILE &&
			 n->exists == 0 && p->rc == -1 && p->err == EMFILE && p->exists == 1;
		fmt(b1, sizeof b1, n);
		fmt(b2, sizeof b2, p);
		claim(3, "openat(O_CREAT|O_EXCL,new)", ok,
		      "native{%s} postfilter{%s} => %s; observable side effect: the exclusive "
		      "create created the file (a retry now fails EEXIST)", b1, b2,
		      ok ? "NOT EQUIVALENT" : "UNEXPECTED");
	}
	/* C4: accept4. */
	{
		struct obs *n = &nat[C4_ACCEPT], *p = &post[C4_ACCEPT];
		int peer_gone = (p->peer_rc == 0 || p->peer_err == ECONNRESET);
		int ok = invariants(n, 0) && invariants(p, 0) && n->soft == V_LIMIT &&
			 p->soft == REAL_LIMIT && n->rc == -1 && n->err == EMFILE &&
			 n->stage2_rc >= 0 && n->peer_err == EAGAIN && p->rc == -1 &&
			 p->err == EMFILE && peer_gone && p->stage2_err == EAGAIN;
		fmt(b1, sizeof b1, n);
		fmt(b2, sizeof b2, p);
		claim(4, "accept4(pending connection)", ok,
		      "native{%s} postfilter{%s} => %s; observable side effect: the queued "
		      "connection was accepted and closed - the peer saw it disappear, while "
		      "native leaves it queued and a retry accept returns it", b1, b2,
		      ok ? "NOT EQUIVALENT" : "UNEXPECTED");
	}
	/* C5/C6: SCM_RIGHTS, naive post-filter. */
	{
		int ids[2] = { C5_SCM_DGRAM, C6_SCM_STREAM };
		const char *names[2] = { "recvmsg+SCM_RIGHTS(dgram)", "recvmsg+SCM_RIGHTS(stream)" };
		int j;
		for (j = 0; j < 2; j++) {
			struct obs *n = &nat[ids[j]], *p = &post[ids[j]];
			int ok = invariants(n, 0) && invariants(p, 0) && n->soft == V_LIMIT &&
			 p->soft == REAL_LIMIT && n->rc == PAYLOAD_LEN &&
				 n->err == 0 && n->ctrunc == 1 && n->ctrl_fds == 0 &&
				 n->ctrl_bytes == 0 && n->data == PAYLOAD_LEN && n->consumed == 1 &&
				 p->rc == -1 && p->err == EMFILE && p->data == PAYLOAD_LEN &&
				 p->consumed == 1;
			fmt(b1, sizeof b1, n);
			fmt(b2, sizeof b2, p);
			claim(ids[j], names[j], ok,
			      "native{%s} postfilter{%s} => %s; observable side effect: native "
			      "SUCCEEDS - payload delivered, MSG_CTRUNC set, descriptors dropped "
			      "and closed - while the post-filter returns EMFILE after the payload "
			      "was copied and the message consumed", b1, b2,
			      ok ? "NOT EQUIVALENT" : "UNEXPECTED");
		}
	}
	/* C7..C14: creators with no nameable object. */
	{
		struct { int id; const char *name; } tab[] = {
			{ C7_PIPE2, "pipe2" },
			{ C8_SOCKET, "socket(AF_UNIX)+socket(AF_INET)" },
			{ C9_SOCKETPAIR, "socketpair(AF_UNIX)" },
			{ C10_EVENTFD, "eventfd" },
			{ C11_EPOLL, "epoll_create(1) [libkqueue backend]" },
			{ C12_INOTIFY, "inotify_init1" },
			{ C13_SIGNALFD, "signalfd" },
			{ C14_TIMERFD, "timerfd_create" },
		};
		size_t j;
		for (j = 0; j < sizeof tab / sizeof tab[0]; j++) {
			struct obs *n = &nat[tab[j].id], *p = &post[tab[j].id];
			int ok = invariants(n, 0) && invariants(p, 0) && n->soft == V_LIMIT &&
			 p->soft == REAL_LIMIT && n->rc == -1 &&
				 n->err == EMFILE && p->rc == -1 && p->err == EMFILE &&
				 (tab[j].id != C8_SOCKET ||
				  (n->rc2 == -1 && n->err2 == EMFILE && p->rc2 == -1 &&
				   p->err2 == EMFILE));
			fmt(b1, sizeof b1, n);
			fmt(b2, sizeof b2, p);
			claim(tab[j].id, tab[j].name, ok,
			      "native{%s} postfilter{%s} => %s (identical EMFILE, nothing created, "
			      "nothing leaked); the object exists only inside the wrapper call and "
			      "is not nameable afterwards - the transient descriptor number at or "
			      "above V is not measured here", b1, b2,
			      ok ? "EQUIVALENT" : "UNEXPECTED");
		}
	}
	/* C15/C16: the bounded-delivery mechanism for SCM_RIGHTS. */
	{
		struct obs *n = &nat[C15_SCM_BOUNDED0], *b = &bounded[C15_SCM_BOUNDED0];
		int ok = invariants(n, 0) && invariants(b, 0) && n->soft == V_LIMIT &&
			 b->soft == REAL_LIMIT && n->rc == PAYLOAD_LEN &&
			 b->rc == PAYLOAD_LEN && n->ctrunc == 1 && b->ctrunc == 1 &&
			 n->ctrl_fds == 0 && b->ctrl_fds == 0 &&
			 n->ctrl_bytes == b->ctrl_bytes && n->data == b->data &&
			 n->consumed == b->consumed;
		fmt(b1, sizeof b1, n);
		fmt(b2, sizeof b2, b);
		claim(15, "scm_bounded(no free slot)", ok,
		      "native{%s} bounded{%s} => %s; the wrapper sizes the kernel-facing control "
		      "buffer to the number of free below-limit slots (0, i.e. suppress it) and "
		      "the kernel's own MSG_CTRUNC path drops and closes the descriptors", b1, b2,
		      ok ? "EQUIVALENT" : "UNEXPECTED");
	}
	{
		struct obs *n = &nat[C16_SCM_BOUNDED1], *b = &bounded[C16_SCM_BOUNDED1];
		struct obs *naive = &post[C16_SCM_BOUNDED1];
		int ok = invariants(n, 1) && invariants(b, 1) && n->soft == V_LIMIT &&
			 b->soft == REAL_LIMIT && n->rc == PAYLOAD_LEN &&
			 n->ctrunc == 1 && n->ctrl_fds == 1 && n->fd == V_LIMIT - 1 &&
			 b->rc == PAYLOAD_LEN && b->ctrunc == 1 && b->ctrl_fds == 1 &&
			 b->fd == n->fd && b->ctrl_bytes == n->ctrl_bytes &&
			 b->data == n->data && naive->child_ok && naive->rc == -1 &&
			 naive->err == EMFILE && naive->consumed == 1;
		fmt(b1, sizeof b1, n);
		fmt(b2, sizeof b2, b);
		fmt(b3, sizeof b3, naive);
		claim(16, "scm_two_fds_one_free_slot", ok,
		      "native{%s} bounded{%s} naive-postfilter{%s} => bounded EQUIVALENT (same rc, "
		      "MSG_CTRUNC, cmsg descriptor count and number); the naive post-filter is "
		      "NOT EQUIVALENT - it rejected a message native partially delivered", b1, b2,
		      b3);
	}
	/* C17/C18: the check-to-act interleaving. */
	{
		struct obs *r = &race[C17_RACE_OPEN_TRUNC], *n = &nat[C17_RACE_OPEN_TRUNC];
		int ok = invariants(r, 0) && r->soft == REAL_LIMIT && r->rc == -1 &&
			 r->err == EMFILE && r->size == 0 &&
			 r->free_before == 0 && r->precheck_fd == V_LIMIT - 1 &&
			 r->race_taken == V_LIMIT - 1 && n->size == PRE_SIZE &&
			 n->rc == -1 && n->err == EMFILE;
		fmt(b1, sizeof b1, r);
		fmt(b2, sizeof b2, n);
		claim(17, "precheck+interleaving(openat O_TRUNC)", ok,
		      "the pre-check saw free slot %d, released it, another thread took %d, and "
		      "the act ran with the guest-visible range full %s; native in the same "
		      "state %s => %s; observable side effect: the truncation happened while "
		      "EMFILE was returned", r->precheck_fd, r->race_taken, b1, b2,
		      ok ? "NOT EQUIVALENT" : "UNEXPECTED");
	}
	{
		struct obs *r = &race[C18_RACE_ACCEPT], *n = &nat[C18_RACE_ACCEPT];
		int peer_gone = (r->peer_rc == 0 || r->peer_err == ECONNRESET);
		int ok = invariants(r, 0) && r->soft == REAL_LIMIT && r->rc == -1 &&
			 r->err == EMFILE && r->precheck_fd == V_LIMIT - 1 && r->free_before == 0 && peer_gone &&
			 r->stage2_err == EAGAIN && n->stage2_rc >= 0;
		fmt(b1, sizeof b1, r);
		fmt(b2, sizeof b2, n);
		claim(18, "precheck+interleaving(accept4)", ok,
		      "the pre-check saw free slot %d, another thread took %d, and the act ran "
		      "with the guest-visible range full%s; native{%s} => %s; observable side "
		      "effect: the queued connection was accepted and closed while EMFILE was "
		      "returned", r->precheck_fd, r->race_taken, b1, b2,
		      ok ? "NOT EQUIVALENT" : "UNEXPECTED");
	}

	{
		char cmd[1200];
		snprintf(cmd, sizeof cmd, "rm -rf -- '%s'", dir);
		if (system(cmd) != 0)
			fprintf(stderr, "warning: scratch directory %s not removed\n", dir);
	}

	if (claims_failed == 0) {
		printf("HARNESS OK fd-semantics-proof (mutations %d%d%d)\n",
		       MUT_NO_CLOSE_AFTER_REJECT, MUT_NATIVE_LIMIT_RAISED,
		       MUT_PRECHECK_FOR_SINGLE);
		return 0;
	}
	printf("HARNESS FAILED %d claim(s)\n", claims_failed);
	return 1;
}
