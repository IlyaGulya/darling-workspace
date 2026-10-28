/*
 * Real console progress while main is blocked in Mach receive.
 * The worker must use two real descriptors before sending main's release.
 * A receive timeout is failure, never an event enabling eventual success.
 * Refusal/disarm ownership is covered separately by the native server tests.
 */
#include <fcntl.h>
#include <mach/mach.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static mach_port_t receive_port;
static int console_ok[2];

static void *worker(void *argument)
{
	(void)argument;
	/* Allow main to enter its blocking receive before publishing console work. */
	struct timespec delay = { 1, 0 };
	(void)nanosleep(&delay, NULL);
	for (int i = 0; i < 2; ++i) {
		int fd = open("/dev/console", O_WRONLY);
		char payload[96];
		int length = snprintf(payload, sizeof(payload),
		                      "PDA console-progress pid=%d open=%d\n", (int)getpid(), i + 1);
		int ok = fd >= 0 && write(fd, payload, (size_t)length) == length;
		if (fd >= 0 && close(fd) != 0)
			ok = 0;
		console_ok[i] = ok;
		printf("PDA progress-open%d io=%d\n", i + 1, ok);
	}
	mach_msg_header_t message;
	memset(&message, 0, sizeof(message));
	message.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_MAKE_SEND, 0);
	message.msgh_size = sizeof(message);
	message.msgh_remote_port = receive_port;
	message.msgh_id = 750;
	kern_return_t result = mach_msg(&message, MACH_SEND_MSG, sizeof(message), 0,
	                               MACH_PORT_NULL, 0, MACH_PORT_NULL);
	printf("PDA progress-release mach_msg=%d\n", (int)result);
	return NULL;
}

int main(void)
{
	pthread_t thread;
	setvbuf(stdout, NULL, _IONBF, 0);
	printf("PDA begin mode=progress pid=%d\n", (int)getpid());
	if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE,
	                       &receive_port) != KERN_SUCCESS ||
	    pthread_create(&thread, NULL, worker, NULL) != 0) {
		printf("PDA setup-fail\n");
		return 2;
	}
	union {
		mach_msg_header_t header;
		char bytes[512]; /* Include room for the received Mach trailer. */
	} message;
	memset(&message, 0, sizeof(message));
	int result = (int)mach_msg(&message.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT,
	                           0, sizeof(message), receive_port, 12000, MACH_PORT_NULL);
	int ok = result == KERN_SUCCESS && message.header.msgh_id == 750;
	if (ok) {
		ok = pthread_join(thread, NULL) == 0;
		if (ok)
			ok = console_ok[0] && console_ok[1];
	}
	printf("PDA result mode=progress mach=%d before-main-release=%d verdict=%s\n",
	       result, ok, ok ? "console-progress" : "console-stalled");
	printf("PDA_STATUS=%d\nPDA end\n", ok ? 0 : 1);
	/* Do not join a worker whose console request is stranded behind main. */
	return ok ? 0 : 1;
}
