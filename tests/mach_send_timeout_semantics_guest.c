// mach_send_timeout_semantics_guest -- what Darling returns for MACH_SEND_TIMEOUT, and when.
//
// WHY THIS EXISTS. The arm's own capture (state work-fcd-fix19-threads-183203-2053722, the traced
// xcodebuild's main thread) shows, in order: the process allocating its OWN receive right
// (`mach_port_allocate(MACH_PORT_RIGHT_RECEIVE) -> 4099`), making a send right to itself in the same name
// (`mach_port_insert_right(new name 4099, port right 4099, MACH_MSG_TYPE_MAKE_SEND)`), inserting that port as
// a MEMBER of two port sets (3843 and 3075), receiving on the set with an infinite timeout
// (`mach_msg_trap(..., MACH_RCV_MSG|MACH_RCV_LARGE|MACH_RCV_VOUCHER, 0, 3072, port 3843, -1, port 0)`), and
// meanwhile sending 24-byte `id = 0` messages to that same member 4099 with `MACH_SEND_MSG|MACH_SEND_TIMEOUT`,
// 18 of which returned `MACH_SEND_TIMED_OUT`. Reading that as "a request to another process that was never
// consumed" is an assumption until this fixture pins what the flag means here.
//
// The cases (each prints its own NST line; every one is bounded; all are single-process so that no fork
// effect can confuse the reading):
//   A  an own receive right with a send right to itself in the same name, NEVER received on while sending:
//      how many sends succeed before the first MACH_SEND_TIMED_OUT, and how many messages a later receive
//      actually finds (i.e. is a timed-out message delivered later, or lost?);
//   B  control: the same, but a receiver thread drains the port while the sends run -- every send must succeed;
//   C  a PORT SET as a send target, and a member that has its own send right;
//   D  the arm's actual shape: own receive right + send to itself in the same name + member of a port set +
//      receive ON THE SET.
//
// FIRST ATTEMPT, KEPT AS A RECORDED NEGATIVE: the first version of this fixture forked a child that sent to
// a right the parent had allocated. Every send in the forked child returned MACH_SEND_INVALID_DEST (A and B),
// so the fork did not carry a usable right into the child here; that is measured, unexplained, and exactly the
// kind of reading this fixture exists to avoid, so the cases below are single-process instead.
//
// WEAK SPOT, stated rather than hidden: the queue depth of a fresh receive right is not a documented constant
// here; the fixture measures it and reports it.

#include <mach/mach.h>
#include <mach/message.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define SEND_TIMEOUT_MS 200
#define MAX_SENDS 64

static uint64_t now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t) ts.tv_sec * 1000u + (uint64_t) ts.tv_nsec / 1000000u;
}

static kern_return_t send_id0(mach_port_t dest)
{
	struct { mach_msg_header_t header; } msg;
	memset(&msg, 0, sizeof(msg));
	msg.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0);
	msg.header.msgh_size = sizeof(msg);
	msg.header.msgh_remote_port = dest;
	msg.header.msgh_local_port = MACH_PORT_NULL;
	msg.header.msgh_id = 0;
	return mach_msg(&msg.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT, sizeof(msg), 0,
			MACH_PORT_NULL, SEND_TIMEOUT_MS, MACH_PORT_NULL);
}

static const char* kr_name(kern_return_t kr)
{
	switch (kr) {
	case KERN_SUCCESS: return "KERN_SUCCESS";
	case MACH_SEND_TIMED_OUT: return "MACH_SEND_TIMED_OUT";
	case MACH_SEND_INVALID_DEST: return "MACH_SEND_INVALID_DEST";
	default: return "OTHER";
	}
}

static int receive_n(mach_port_t port, int max, unsigned timeout_ms)
{
	struct { mach_msg_header_t header; char body[64]; } rmsg;
	int got = 0;
	for (int i = 0; i < max; i++) {
		memset(&rmsg, 0, sizeof(rmsg));
		if (mach_msg(&rmsg.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0, sizeof(rmsg),
			     port, timeout_ms, MACH_PORT_NULL) != KERN_SUCCESS)
			break;
		got++;
	}
	return got;
}

// A: own receive right, send right to itself in the same name, no receive while sending.
static void case_a(void)
{
	mach_port_t rx = MACH_PORT_NULL;
	if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &rx) != KERN_SUCCESS ||
	    mach_port_insert_right(mach_task_self(), rx, rx, MACH_MSG_TYPE_MAKE_SEND) != KERN_SUCCESS) {
		printf("NST A SETUP_FAIL\n");
		return;
	}
	uint64_t t0 = now_ms();
	int ok = 0, timedout = 0, first_bad_at = -1;
	kern_return_t first_bad = KERN_SUCCESS;
	for (int i = 0; i < MAX_SENDS; i++) {
		kern_return_t r = send_id0(rx);
		if (r == KERN_SUCCESS) ok++;
		else {
			if (first_bad_at < 0) { first_bad_at = i; first_bad = r; }
			if (r == MACH_SEND_TIMED_OUT) timedout++;
		}
	}
	uint64_t wall = now_ms() - t0;
	int drained = receive_n(rx, MAX_SENDS, 20);
	printf("NST A selftarget=1 sends=%d ok=%d timedout=%d first_bad_at=%d first_bad=%s drained_after=%d wall_ms=%llu\n",
	       MAX_SENDS, ok, timedout, first_bad_at, kr_name(first_bad), drained,
	       (unsigned long long) wall);
	mach_port_mod_refs(mach_task_self(), rx, MACH_PORT_RIGHT_RECEIVE, -1);
}

// B: control -- a receiver thread drains the port while the sends run.
static volatile int b_stop;

static void* b_receiver(void* arg)
{
	mach_port_t rx = (mach_port_t) (uintptr_t) arg;
	int got = 0;
	while (!b_stop) got += receive_n(rx, 4, 20);
	return (void*) (intptr_t) got;
}

static void case_b(void)
{
	mach_port_t rx = MACH_PORT_NULL;
	if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &rx) != KERN_SUCCESS ||
	    mach_port_insert_right(mach_task_self(), rx, rx, MACH_MSG_TYPE_MAKE_SEND) != KERN_SUCCESS) {
		printf("NST B SETUP_FAIL\n");
		return;
	}
	pthread_t tid;
	b_stop = 0;
	pthread_create(&tid, NULL, b_receiver, (void*) (uintptr_t) rx);

	uint64_t t0 = now_ms();
	int ok = 0, timedout = 0;
	for (int i = 0; i < 16; i++) {
		kern_return_t r = send_id0(rx);
		if (r == KERN_SUCCESS) ok++;
		else if (r == MACH_SEND_TIMED_OUT) timedout++;
	}
	uint64_t wall = now_ms() - t0;
	b_stop = 1;
	void* ret = NULL;
	pthread_join(tid, &ret);
	printf("NST B receiver=thread sends=16 ok=%d timedout=%d received=%d wall_ms=%llu\n",
	       ok, timedout, (int) (intptr_t) ret, (unsigned long long) wall);
	mach_port_mod_refs(mach_task_self(), rx, MACH_PORT_RIGHT_RECEIVE, -1);
}

// C: a port set as a send target, and a member that has its own send right.
static void case_c(void)
{
	mach_port_t set = MACH_PORT_NULL, member = MACH_PORT_NULL;
	if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &member) != KERN_SUCCESS ||
	    mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_PORT_SET, &set) != KERN_SUCCESS) {
		printf("NST C SETUP_FAIL\n");
		return;
	}
	mach_port_insert_member(mach_task_self(), member, set);
	kern_return_t kr_set = send_id0(set);
	mach_port_insert_right(mach_task_self(), member, member, MACH_MSG_TYPE_MAKE_SEND);
	kern_return_t kr_member = send_id0(member);
	printf("NST C send_to_set_name=%s send_to_member_with_send_right=%s\n",
	       kr_name(kr_set), kr_name(kr_member));
	mach_port_mod_refs(mach_task_self(), member, MACH_PORT_RIGHT_RECEIVE, -1);
}

// D: the arm's actual shape.
static void case_d(void)
{
	mach_port_t rx = MACH_PORT_NULL, set = MACH_PORT_NULL;
	if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &rx) != KERN_SUCCESS ||
	    mach_port_insert_right(mach_task_self(), rx, rx, MACH_MSG_TYPE_MAKE_SEND) != KERN_SUCCESS ||
	    mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_PORT_SET, &set) != KERN_SUCCESS ||
	    mach_port_insert_member(mach_task_self(), rx, set) != KERN_SUCCESS) {
		printf("NST D SETUP_FAIL\n");
		return;
	}
	int ok = 0, timedout = 0;
	kern_return_t first_bad = KERN_SUCCESS;
	for (int i = 0; i < 24; i++) {
		kern_return_t r = send_id0(rx);
		if (r == KERN_SUCCESS) ok++;
		else { if (first_bad == KERN_SUCCESS) first_bad = r; if (r == MACH_SEND_TIMED_OUT) timedout++; }
	}
	int drained = receive_n(set, 32, 20);
	printf("NST D member_of_set=1 sends=24 ok=%d timedout=%d first_bad=%s drained_on_set=%d\n",
	       ok, timedout, kr_name(first_bad), drained);
	mach_port_extract_member(mach_task_self(), rx, set);
	mach_port_mod_refs(mach_task_self(), rx, MACH_PORT_RIGHT_RECEIVE, -1);
}

int main(void)
{
	printf("NST BEGIN send_timeout_ms=%d max_sends=%d\n", SEND_TIMEOUT_MS, MAX_SENDS);
	fflush(stdout);
	case_a();
	case_b();
	case_c();
	case_d();
	printf("MACH_SEND_TIMEOUT_SEMANTICS_DONE\n");
	fflush(stdout);
	return 0;
}
