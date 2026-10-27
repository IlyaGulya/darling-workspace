/*
 * pid_for_task_plane_route_guest.c -- permanent guest oracle for pid_for_task.
 *
 * WHY THIS EXISTS. While IDEInitialize runs, DVTFoundation's -[DVTStackBacktrace ...]
 * symbolicates a backtrace and CoreSymbolicationDT's CSCppMachTask ctor asks
 * pid_for_task(task, &pid) for the task's own pid. In the guest that trap had NO
 * ring route and NO plane op, so the generated wrapper asked the RETIRED per-thread
 * RPC socket for a transport; mach_driver_get_fd() declines unconditionally, the
 * wrapper returned -EPIPE, and pid_for_task_impl treated a transport failure as
 * fatal and called __simple_abort(). Measured on prefix-verify14, both xcodebuild
 * workloads:
 *
 *   [rpc-socket-DENIED] pid=670051 tid=670051 call=pid_for_task image=kernel delta=0x3B0A0 denied=1
 *   pid_for_task failed (internally): -32
 *   XCB_RC=134
 *
 * and the core named the frames: __simple_abort <- pid_for_task_impl <- _darling_mach_syscall
 * <- CSCppMachTask::CSCppMachTask <- CSSymbolicatorCreateWithTaskFlagsAndNotification
 * <- +[DVTStackBacktraceSymbolicator ...] <- IDEInitialize.
 *
 * THE MACOS-SHAPED CONTRACT THIS PINS. pid_for_task(task, &pid) returns a kern_return_t
 * and has no fatal case for ANY input: for a task port the caller holds it writes that
 * task's pid and returns KERN_SUCCESS, and for a name that is not a usable task port it
 * returns a failure code. It must NEVER kill the caller -- the caller here is a stack
 * symbolicator, and an abort inside it destroys the very report the call exists to produce.
 *
 * SHAPE NOTE. pid_for_task takes a TASK PORT and RETURNS a pid (the pid -> task direction
 * is task_for_pid). The load-bearing case therefore asks about the task port that belongs
 * to this process's own pid -- mach_task_self() -- and pins three things:
 *   1. KERN_SUCCESS and a pid equal to getpid();
 *   2. the subject really is a live task port (mach_port_type reports a SEND right, an
 *      already-proven-routed call, so "the port is usable" is measured and not assumed);
 *   3. a CHILD process asking the same question gets its OWN pid, which rules out the call
 *      returning a constant or the parent's cached value.
 * The control is an input that is not a usable task port at all: MACH_PORT_NULL and a
 * name that no right backs. Both must come back as a failure code with this process still
 * alive and able to print its verdict -- that is the "error rather than die" arm.
 *
 * The raw measurement lines are printed unconditionally (PDTPROBE ...) so a RED arm can
 * quote kr/pid from the run's own output, and the verdict line is machine-readable and
 * read by the harness's ok-marker.
 */

#include <mach/mach.h>
#include <mach/mach_traps.h>
#include <mach/port.h>
#include <sys/wait.h>
#include <stdio.h>
#include <unistd.h>
#include <string.h>

static int failures;

static void fail(const char *msg)
{
	printf("PDTPROBE-FAIL %s\n", msg);
	failures++;
}

/* A name that passes MACH_PORT_VALID but is not a name of ours: no right backs it. */
#define BOGUS_TASK_NAME 0x7ffffff0u

/*
 * The same question from a second process, over a pipe so a child that died instead of
 * answering is visible as an absent line rather than as silence. The child reports the
 * kern_return_t, the pid pid_for_task returned, and its own getpid(); the parent asserts
 * the child exited normally and reported ITS pid -- not the parent's.
 */
static void probe_child(void)
{
	int fds[2];
	pid_t self = getpid();
	pid_t child;
	int status = 0;
	char buf[64];
	ssize_t n = 0;
	int ckr = -1, cpid = -1, cself = -1;

	if (pipe(fds) != 0) {
		fail("pipe() failed");
		return;
	}

	fflush(NULL);
	child = fork();
	if (child < 0) {
		fail("fork() failed");
		close(fds[0]);
		close(fds[1]);
		return;
	}

	if (child == 0) {
		int own_pid = 0;
		kern_return_t kr = pid_for_task(mach_task_self(), &own_pid);
		int written = snprintf(buf, sizeof(buf), "%d %d %d\n", (int)kr, own_pid, (int)getpid());

		close(fds[0]);
		if (written > 0) {
			ssize_t ignored = write(fds[1], buf, (size_t)written);
			(void)ignored;
		}
		_exit(0);
	}

	close(fds[1]);
	while (n < (ssize_t)sizeof(buf) - 1) {
		ssize_t r = read(fds[0], buf + n, sizeof(buf) - 1 - (size_t)n);

		if (r <= 0) {
			break;
		}
		n += r;
		if (buf[n - 1] == '\n') {
			break;
		}
	}
	close(fds[0]);
	buf[n > 0 ? n : 0] = '\0';

	waitpid(child, &status, 0);
	sscanf(buf, "%d %d %d", &ckr, &cpid, &cself);
	printf("PDTPROBE child kr=%d pid=%d child_self=%d state=%s status=%d\n",
	    ckr, cpid, cself,
	    WIFEXITED(status) ? "exited" : (WIFSIGNALED(status) ? "signaled" : "other"),
	    status);

	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		fail("child asking pid_for_task about its own task did not exit normally");
		return;
	}
	if (ckr != KERN_SUCCESS) {
		fail("child pid_for_task returned a failure code");
		return;
	}
	if (cpid != cself || cpid <= 0) {
		fail("child pid_for_task reported a pid that is not the child's own");
		return;
	}
	if (cpid == (int)self) {
		fail("child pid_for_task reported the PARENT's pid");
	}
}

int main(void)
{
	pid_t self = getpid();
	mach_port_t self_task = mach_task_self();
	kern_return_t kr;
	int pid;

	printf("PDTPROBE getpid=%d self_task=0x%x\n", (int)self, (unsigned)self_task);

	/* --- 1. the load-bearing case: this process's own task port, exactly what CSCppMachTask asks --- */
	pid = -1;
	kr = pid_for_task(self_task, &pid);
	printf("PDTPROBE own_task kr=%d pid=%d expected=%d\n", (int)kr, pid, (int)self);
	if (kr != KERN_SUCCESS) {
		fail("pid_for_task(own task port) did not return KERN_SUCCESS");
	} else if (pid != (int)self) {
		fail("pid_for_task(own task port) reported a different pid");
	}

	/* --- 2. the subject must be a USABLE task port: a live SEND right in our own space --- */
	{
		mach_port_type_t type = 0;

		kr = mach_port_type(mach_task_self(), self_task, &type);
		printf("PDTPROBE port_type kr=%d type=0x%x\n", (int)kr, (unsigned)type);
		if (kr != KERN_SUCCESS) {
			fail("mach_port_type on the task port used by pid_for_task failed");
		} else if ((type & MACH_PORT_TYPE_SEND) == 0) {
			fail("the port pid_for_task was asked about is not a live SEND right");
		}
	}

	/* --- 3. control: an input that is not a usable task port must FAIL, not kill the caller --- */
	{
		int bogus_pid;

		bogus_pid = -1;
		kr = pid_for_task(MACH_PORT_NULL, &bogus_pid);
		printf("PDTPROBE null_name kr=%d pid=%d\n", (int)kr, bogus_pid);
		if (kr == KERN_SUCCESS) {
			fail("pid_for_task(MACH_PORT_NULL) reported success");
		}

		bogus_pid = -1;
		kr = pid_for_task((mach_port_name_t)BOGUS_TASK_NAME, &bogus_pid);
		printf("PDTPROBE bogus_name kr=%d pid=%d\n", (int)kr, bogus_pid);
		if (kr == KERN_SUCCESS) {
			fail("pid_for_task(backed by no right) reported success");
		}
	}

	/* Reaching this line at all is the "returned an error rather than dying" evidence: had
	 * either control aborted, no verdict line would follow. */
	printf("PDTPROBE survived_controls=1\n");

	probe_child();

	printf("PDTPROBE failures=%d\n", failures);
	if (failures == 0) {
		printf("PID_FOR_TASK_PLANE_ROUTE_OK\n");
		return 0;
	}
	printf("PID_FOR_TASK_PLANE_ROUTE_FAIL\n");
	return 1;
}
