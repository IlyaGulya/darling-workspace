/*
 * plane_proc_open_guest.c -- reach the PROCESS-CONTROL PLANE dispatch, not a datagram wrapper.
 *
 * The first reproducer opened /dev/console, which the guest routes over the ordinary datagram
 * call wrapper: the server then handles it through Thread::doWork (site=doWork-parked), so
 * Thread::dispatchOrQueuePlaneCall was never entered and could not be validated.
 *
 * This fixture parks the MAIN thread in a blocking Mach receive (callnum 38) and has a worker
 * register EVFILT_PROC on the process through libkqueue. libkqueue's evproc_create() issues
 * DSERVER_PROCESS_CONTROL_OP_KQCHAN_PROC_OPEN through __dserver_plane_request_ex -- the
 * management plane -- and the server synthesizes that Call with header.tid = pid (the process
 * scoped, non-retargetable op), i.e. onto the PARKED main thread, then refuses it in
 * Thread::dispatchOrQueuePlaneCall with queueIfBusy == false.
 *
 * The guest result is not the oracle: a refused plane request falls back to the datagram and can
 * still succeed. The server trace is (plane-dispatch-entry / plane-call-refused /
 * plane-call-disarmed, DARLING_PLANE_DISPATCH_DIAG=1).
 */

#include <errno.h>
#include <mach/mach.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/event.h>
#include <time.h>
#include <unistd.h>

static mach_port_t g_recv_port;
static int g_kevent[2];

static void msleep(int ms)
{
	struct timespec ts;
	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (long)(ms % 1000) * 1000000L;
	(void)nanosleep(&ts, NULL);
}

static void *worker(void *arg)
{
	(void)arg;
	msleep(1000);
	for (int i = 0; i < 2; ++i) {
		int err = 0;
		int rc = -1;
		int kq = kqueue();
		if (kq >= 0) {
			struct kevent ev;
			EV_SET(&ev, getpid(), EVFILT_PROC, EV_ADD | EV_ENABLE, NOTE_EXIT, 0, NULL);
			rc = kevent(kq, &ev, 1, NULL, 0, NULL);
			err = (rc < 0) ? errno : 0;
			close(kq);
		} else {
			err = errno;
		}
		g_kevent[i] = rc;
		printf("PPO kevent%d rc=%d errno=%d/%s\n", i + 1, rc, err,
		       (rc < 0) ? strerror(err) : "-");
		fflush(stdout);
		msleep(5000);
	}
	return NULL;
}

int main(void)
{
	pthread_t th;
	kern_return_t kr;
	int msg_res = 0;

	setvbuf(stdout, NULL, _IONBF, 0);
	printf("PPO begin pid=%d\n", (int)getpid());

	kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &g_recv_port);
	if (kr != KERN_SUCCESS) {
		printf("PPO setup-fail mach_port_allocate=%d\n", (int)kr);
		return 2;
	}
	if (pthread_create(&th, NULL, worker, NULL) != 0) {
		printf("PPO setup-fail pthread_create\n");
		return 2;
	}

	union {
		mach_msg_header_t hdr;
		char buf[512];
	} msg;
	memset(&msg, 0, sizeof(msg));
	msg_res = (int)mach_msg(&msg.hdr, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
	                        (mach_msg_size_t)sizeof(msg), g_recv_port, 12000, MACH_PORT_NULL);
	printf("PPO main-wake mach_msg=%d\n", msg_res);

	pthread_join(th, NULL);

	int ok = (g_kevent[0] == 0) && (g_kevent[1] == 0);
	printf("PPO result kevent1=%d kevent2=%d mach=%d verdict=%s\n",
	       g_kevent[0], g_kevent[1], msg_res, ok ? "both-registered" : "registration-refused");
	printf("PPO_STATUS=%d\n", ok ? 0 : 1);
	return ok ? 0 : 1;
}
