// fork_inherited_right_guest -- does a receive/send right a parent created survive into a CHILD, and can the
// child use it to talk back?
//
// WHY THIS EXISTS. The first version of the MACH_SEND_TIMEOUT fixture (kept as a recorded negative in
// evidence/oslog-framework-rung/README.md section 26.2) forked a child that sent on a right the parent had
// allocated, and EVERY send in the forked child returned MACH_SEND_INVALID_DEST. That is the shape of the
// channel this ladder needs -- a parent handing a child a right to talk back on -- so it is characterized here
// instead of left as "measured, unexplained":
//
//   case 1  fork:      parent allocates a receive right and a send right for it, forks; the CHILD reports what
//                      it can see of the inherited name (mach_port_type, mach_port_get_refs) and sends on it;
//                      the PARENT reports what it receives.
//   case 2  control:   the same two operations in ONE process (no fork) -- must succeed.
//   case 3  posix_spawn:the parent re-execs itself with the inherited NAME (as a decimal string) in argv, so
//                      the child that runs is a fresh image that never allocated anything; it reports the name's
//                      type/refs and tries to send. This separates "fork does not carry rights" from "exec does
//                      not carry them".
//   case 4  fork with a SEPARATE send-right name: the arm's own shape inserts the send right in the SAME name
//                      as the receive right; this case checks whether the name layout changes the outcome.
//
// Every send uses a bounded timeout and every receive a bounded timeout, so no case can stall; the whole
// fixture is bounded by alarm(60). Each case prints its own FIR line and the fixture prints
// FORK_INHERITED_RIGHT_DONE at the end (a plain marker: a --marker ending in '=' is judged a STATUS field by
// the harness and fails closed, which cost a run in follow-up 21).
//
// WEAK SPOT, stated rather than hidden: the child cannot distinguish "the name is absent" from "the right is
// present but of the wrong type" without asking the kernel, which is exactly what mach_port_type is for, so both
// are reported and neither is guessed.

#include <mach/mach.h>
#include <mach/message.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>
#include <time.h>

extern char** environ;

static uint64_t now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t) ts.tv_sec * 1000u + (uint64_t) ts.tv_nsec / 1000000u;
}

static const char* kr_name(kern_return_t kr)
{
	switch (kr) {
	case KERN_SUCCESS: return "KERN_SUCCESS";
	case MACH_SEND_TIMED_OUT: return "MACH_SEND_TIMED_OUT";
	case MACH_SEND_INVALID_DEST: return "MACH_SEND_INVALID_DEST";
	case MACH_SEND_INVALID_RIGHT: return "MACH_SEND_INVALID_RIGHT";
	case KERN_INVALID_NAME: return "KERN_INVALID_NAME";
	case KERN_INVALID_RIGHT: return "KERN_INVALID_RIGHT";
	default: return "OTHER";
	}
}

static const char* pt_name(mach_port_type_t t)
{
	static char buf[128];
	buf[0] = 0;
#define ADD(bit, name) if (t & (bit)) { if (buf[0]) strcat(buf, "|"); strcat(buf, name); }
	ADD(MACH_PORT_TYPE_SEND, "send")
	ADD(MACH_PORT_TYPE_RECEIVE, "recv")
	ADD(MACH_PORT_TYPE_SEND_ONCE, "send-once")
	ADD(MACH_PORT_TYPE_PORT_SET, "set")
	ADD(MACH_PORT_TYPE_DEAD_NAME, "dead")
	ADD(MACH_PORT_TYPE_NONE, "none")
#undef ADD
	return buf[0] ? buf : "(no bits)";
}

static kern_return_t send_id0(mach_port_t dest, mach_msg_timeout_t timeout_ms)
{
	struct { mach_msg_header_t header; } msg;
	memset(&msg, 0, sizeof(msg));
	msg.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0);
	msg.header.msgh_size = sizeof(msg);
	msg.header.msgh_remote_port = dest;
	msg.header.msgh_local_port = MACH_PORT_NULL;
	msg.header.msgh_id = 0;
	return mach_msg(&msg.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT, sizeof(msg), 0,
			MACH_PORT_NULL, timeout_ms, MACH_PORT_NULL);
}

// Report what THIS process can see of a name, and try to send on it.
static void probe_name(const char* who, mach_port_name_t name)
{
	mach_port_type_t type = 0;
	kern_return_t kr_type = mach_port_type(mach_task_self(), name, &type);
	mach_port_urefs_t refs = 0;
	kern_return_t kr_refs = mach_port_get_refs(mach_task_self(), name, MACH_PORT_RIGHT_SEND, &refs);
	kern_return_t kr_send = send_id0(name, 300);
	printf("FIR %s name=%u port_type=%s (kr=%s) send_refs=%u (kr=%s) send=%s\n",
	       who, (unsigned) name, kr_type == KERN_SUCCESS ? pt_name(type) : "-", kr_name(kr_type),
	       (unsigned) refs, kr_name(kr_refs), kr_name(kr_send));
	fflush(stdout);
}

static int receive_one(mach_port_t port, unsigned timeout_ms)
{
	struct { mach_msg_header_t header; char body[64]; } rmsg;
	memset(&rmsg, 0, sizeof(rmsg));
	kern_return_t kr = mach_msg(&rmsg.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0, sizeof(rmsg),
				    port, timeout_ms, MACH_PORT_NULL);
	return kr == KERN_SUCCESS ? 1 : 0;
}

// case 1 and 4: fork with the send right in the same name as (1) or a separate name from (4) the receive right.
static void fork_case(const char* label, int same_name)
{
	mach_port_t rx = MACH_PORT_NULL;
	if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &rx) != KERN_SUCCESS) {
		printf("FIR %s SETUP_FAIL\n", label);
		return;
	}
	mach_port_name_t send_name = rx;	// same name (the arm's shape)
	if (!same_name) {
		// A DISTINCT name for a send right to the same port: the honest API for that is
		// mach_port_extract_right, which returns the new name for the requested right.
		mach_port_t newname = MACH_PORT_NULL;
		mach_msg_type_name_t extracted_type = 0;
		kern_return_t kr = mach_port_extract_right(mach_task_self(), rx, MACH_MSG_TYPE_MAKE_SEND,
							   &newname, &extracted_type);
		if (kr != KERN_SUCCESS) {
			printf("FIR %s SETUP_FAIL extract_right=%s\n", label, kr_name(kr));
			return;
		}
		send_name = newname;
	} else {
		mach_port_insert_right(mach_task_self(), rx, rx, MACH_MSG_TYPE_MAKE_SEND);
	}

	pid_t pid = fork();
	if (pid == 0) {
		probe_name(label, send_name);
		_exit(0);
	}
	int st = 0;
	waitpid(pid, &st, 0);
	int got = receive_one(rx, 400);
	printf("FIR %s parent_received=%d child_exit=%d\n", label, got, WEXITSTATUS(st));
	fflush(stdout);
	mach_port_mod_refs(mach_task_self(), rx, MACH_PORT_RIGHT_RECEIVE, -1);
}

// case 2: control -- no fork at all.
static void same_process_case(void)
{
	mach_port_t rx = MACH_PORT_NULL;
	if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &rx) != KERN_SUCCESS ||
	    mach_port_insert_right(mach_task_self(), rx, rx, MACH_MSG_TYPE_MAKE_SEND) != KERN_SUCCESS) {
		printf("FIR case2 SETUP_FAIL\n");
		return;
	}
	kern_return_t kr = send_id0(rx, 300);
	int got = receive_one(rx, 400);
	printf("FIR case2 same-process send=%s received=%d\n", kr_name(kr), got);
	fflush(stdout);
	mach_port_mod_refs(mach_task_self(), rx, MACH_PORT_RIGHT_RECEIVE, -1);
}

// case 3: the parent re-execs itself; the running image never allocated anything.
static void spawn_case(const char* self_path)
{
	mach_port_t rx = MACH_PORT_NULL;
	if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &rx) != KERN_SUCCESS ||
	    mach_port_insert_right(mach_task_self(), rx, rx, MACH_MSG_TYPE_MAKE_SEND) != KERN_SUCCESS) {
		printf("FIR case3 SETUP_FAIL\n");
		return;
	}
	char name_str[32];
	snprintf(name_str, sizeof(name_str), "%u", (unsigned) rx);
	char* argv[] = { (char*) self_path, (char*) "child-exec", name_str, NULL };
	pid_t pid = 0;
	int rc = posix_spawn(&pid, self_path, NULL, NULL, argv, environ);
	if (rc != 0) {
		printf("FIR case3 SPAWN_FAIL rc=%d\n", rc);
		return;
	}
	int st = 0;
	waitpid(pid, &st, 0);
	int got = receive_one(rx, 400);
	printf("FIR case3 spawned_child_exit=%d parent_received=%d (name=%s)\n", WEXITSTATUS(st), got, name_str);
	fflush(stdout);
	mach_port_mod_refs(mach_task_self(), rx, MACH_PORT_RIGHT_RECEIVE, -1);
}

int main(int argc, char** argv)
{
	if (argc >= 3 && strcmp(argv[1], "child-exec") == 0) {
		// The exec'd child: it never allocated this name; everything it knows came across exec.
		mach_port_name_t name = (mach_port_name_t) strtoul(argv[2], NULL, 10);
		probe_name("case3-child", name);
		return 0;
	}

	alarm(60);
	printf("FIR BEGIN pid=%d\n", (int) getpid());
	fflush(stdout);
	fork_case("case1", 1);
	same_process_case();
	spawn_case(argv[0]);
	fork_case("case4", 0);
	printf("FORK_INHERITED_RIGHT_DONE\n");
	fflush(stdout);
	return 0;
}
