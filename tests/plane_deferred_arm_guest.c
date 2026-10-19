/*
 * plane_deferred_arm_guest.c -- deterministic reproducer for the
 * "refusal without disarm" defect in Thread::dispatchOrQueuePlaneCall.
 *
 * WHAT IS UNDER TEST (server-side, one process):
 *   A non-deferrable management-plane op (CONSOLE_OPEN, callnum 16) is
 *   published by a worker thread while the process's MAIN thread is parked
 *   inside a blocking trap (a Mach receive, callnum 38).
 *
 *   The plane stamps every process-scoped, non-retargetable op with
 *   `header.tid = pid` (server.cpp, planeScopedTid), so the synthesized Call
 *   is armed on the MAIN thread. `Call::callFromMessage` arms it through
 *   `Thread::setPendingCall` even though that thread is parked (the
 *   `call-deferred site=setPendingCall-parked` record). The dispatch then
 *   returns false for `queueIfBusy == false` WITHOUT clearing the slot, and
 *   the pass records the request as "left PENDING for a later pass"
 *   (server.cpp planeUnserviced). The armed Call has no completion owner:
 *
 *     1. the guest's plane request never completes; after its bounded wait
 *        it reclaims the slot and publishes the same op again;
 *     2. the re-publication reaches `callFromMessage` while the abandoned
 *        Call still occupies the slot, so `setPendingCall` throws and the
 *        server answers with a stray -EAGAIN datagram addressed to the MAIN
 *        thread (call.cpp `sendBasicReply(-EAGAIN)`) and reports a plain
 *        failure for the plane request;
 *     3. the guest's `open("/dev/console")` therefore falls back to the
 *        datagram, which runs on the WORKER's Thread and succeeds -- so the
 *        guest-visible `open()` result is NOT a reliable oracle; the
 *        server-side trace is (and the abandoned Call still executes later,
 *        with its answer dropped).
 *
 * The fixture prints raw results only; the contract script decides the arm's
 * verdict. The guest-side record is:
 *   control arm: the main thread spins; the opens are served inline.
 *   park arm:    the main thread is parked in a Mach receive; the server log
 *                must show, for this pid/tid:
 *                  call-deferred site=setPendingCall-parked ... number=16 ...
 *                  setPendingCall rejected call (number 16)
 *                  call-deferred site=doWork-parked ... number=16 ...
 *                and must NOT show a `plane-deferred-completion` for it.
 *
 * Build: guest Mach-O with the Darling build's own cross-toolchain; see
 * tests/run-plane-deferred-arm-contract.sh (--build-dir extracts the compile
 * and link commands from the build's ninja graph; no product file changes).
 */

#include <errno.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static volatile int g_release;      /* control arm: main spins until this is set */
static mach_port_t g_recv_port;     /* park arm: main blocks in a receive on it */

static void msleep(int ms)
{
	struct timespec ts;
	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (long)(ms % 1000) * 1000000L;
	(void)nanosleep(&ts, NULL);
}

struct open_result {
	int attempt;
	int fd;
	int err;
};

static struct open_result g_open[2];

static void *worker(void *arg)
{
	(void)arg;

	/* Let the main thread reach its blocked/spinning state. */
	msleep(1000);

	for (int i = 0; i < 2; ++i) {
		errno = 0;
		int fd = open("/dev/console", O_RDWR);
		g_open[i].attempt = i + 1;
		g_open[i].fd = fd;
		g_open[i].err = fd < 0 ? errno : 0;
		printf("PDA open%d fd=%d errno=%d/%s\n",
		       i + 1, fd, g_open[i].err, fd < 0 ? strerror(g_open[i].err) : "-");
		fflush(stdout);
		/*
		 * The first plane request times out and reclaims its slot after a
		 * bounded wait; the second open is the one that can meet the
		 * still-armed Call.
		 */
		msleep(5000);
	}

	g_release = 1;
	return NULL;
}

int main(int argc, char **argv)
{
	const char *mode = (argc > 1) ? argv[1] : "control";
	int park = (strcmp(mode, "park") == 0);
	pthread_t th;
	kern_return_t kr = KERN_SUCCESS;
	int msg_res = 0;

	setvbuf(stdout, NULL, _IONBF, 0);
	printf("PDA begin mode=%s pid=%d\n", mode, (int)getpid());

	if (park) {
		kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &g_recv_port);
		if (kr != KERN_SUCCESS) {
			printf("PDA setup-fail mach_port_allocate=%d\n", (int)kr);
			return 2;
		}
	}

	if (pthread_create(&th, NULL, worker, NULL) != 0) {
		printf("PDA setup-fail pthread_create\n");
		return 2;
	}

	if (park) {
		/* Park the MAIN thread server-side in the Mach receive trap
		 * (callnum 38; the state the audit's evidence records as
		 * active_call=38 suspended=1). The 12 s timeout bounds the arm. */
		union {
			mach_msg_header_t hdr;
			char buf[512];
		} msg;
		memset(&msg, 0, sizeof(msg));
		msg_res = (int)mach_msg(&msg.hdr,
		                        MACH_RCV_MSG | MACH_RCV_TIMEOUT,
		                        0, (mach_msg_size_t)sizeof(msg),
		                        g_recv_port, 12000, MACH_PORT_NULL);
		printf("PDA main-wake mach_msg=%d\n", msg_res);
	} else {
		while (!g_release) {
			(void)sched_yield();
		}
		printf("PDA main-wake spin=1\n");
	}

	pthread_join(th, NULL);

	int ok = (g_open[0].fd >= 0) && (g_open[1].fd >= 0);
	printf("PDA result mode=%s open1=%d open2=%d mach=%d verdict=%s\n",
	       mode, g_open[0].fd, g_open[1].fd, msg_res,
	       ok ? "both-open" : "open-failed");
	printf("PDA end\n");
	return ok ? 0 : 1;
}
