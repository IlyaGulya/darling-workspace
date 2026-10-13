/*
 * task_map_corpse_info_guest.c -- permanent guest oracle for the corpse-map pair.
 *
 * WHY THIS EXISTS. While IDEInitialize runs, the Xcode toolchain's crash/symbolication path asks the kernel
 * for a corpse's mapping. In the guest those two traps reached darlingserver's duct-tape entry points, which
 * were `dtape_stub_unsafe()` -- a stub whose whole body is `dtape_stub_log(__FUNCTION__, -1, ...)` followed by
 * `abort()` (duct-tape/src/stubs.c:49-80). The abort kills the SERVER, not the caller, so the caller does not
 * get a kern_return_t at all: it blocks forever in the Mach trap, and every other thread of the process loses
 * the shared process-control page at the same instant. MEASURED on prefix-verify14, BOTH xcodebuild workloads,
 * the server's last record of the whole incarnation is the stub line
 *
 *   [P:747758(747758)][T:747758(747758)] ... stub (unsafe): task_map_corpse_info_64
 *
 * after which the plane has no server: an earlier pthread_canceled publication (plane op 7) is never
 * completed, `request_state` stays PENDING, and the loader's NEXT thread-create checkin cannot claim the single
 * management slot --
 *
 *   [checkin-republish] tid=747917 attempt=1 page=1 ready=1 req=0x1 rep=0x0 pub=0
 *   [rpc-socket-DENIED] pid=747758 tid=747917 call=checkin image=loader denied=1 (no per-thread transport)
 *
 * -- so the guest aborts and then hangs to the harness timeout (XCB_RC=absent).
 *
 * THE MACOS-SHAPED CONTRACT THIS PINS. task_map_corpse_info / task_map_corpse_info_64 take a task port and a
 * corpse task port and RETURN a kern_return_t. XNU's own routine opens with
 * `if (task == TASK_NULL || task_is_a_corpse_fork(task)) return KERN_INVALID_ARGUMENT;` and
 * `if (corpse_task == TASK_NULL || !task_is_a_corpse(corpse_task) || ...) return KERN_INVALID_ARGUMENT;`
 * (xnu/osfmk/corpses/corpse.c:733). No Darling task can carry TF_CORPSE -- the only source of a corpse port,
 * task_generate_corpse(), is itself unimplemented -- so for EVERY input this platform can produce the answer
 * is an error code, and it must never kill the transport the caller's process depends on.
 *
 * THE LOAD-BEARING CASE. `corpse_task == mach_task_self()`, i.e. a live task that is not a corpse: the call
 * must come back with a failure code while the process stays able to talk to darlingserver. That is proved
 * twice: the 64-bit entry point and the 32-bit one, and then a pthread CREATE + JOIN -- the thread-create
 * checkin (mldr `__darling_thread_create` -> OP_CHECKIN on the process-control page) is the operation the
 * server's death was actually killing, and it is asserted to work both BEFORE the corpse call and AFTER it.
 * A process whose plane survived cannot report the second join as anything but success.
 *
 * WHY THE RED ARM IS A HANG AND NOT AN ABORT. The fixture dies inside the corpse call on the unfixed runtime
 * (the server it is waiting on is gone), so the discriminating RED evidence is the truncation itself: the run
 * shows the pre-map line and never the post-map line, never a join line and never the verdict. `CORPSEPROBE
 * pre-map64` is printed and flushed before the call precisely so a truncated run can be told apart from a run
 * whose fixture, upload or compiler failed.
 *
 * The raw measurement lines are printed unconditionally (CORPSEPROBE ...) so a RED arm can quote them, and
 * the verdict line is machine-readable and read by the harness's ok-marker.
 */

#include <mach/mach.h>
#include <mach/task.h>
#include <pthread.h>
#include <stdio.h>
#include <unistd.h>

static int failures;
static int workers_ran;

static void fail(const char *msg)
{
	printf("CORPSEPROBE-FAIL %s\n", msg);
	failures++;
}

static void *worker(void *arg)
{
	(void)arg;
	__sync_fetch_and_add(&workers_ran, 1);
	return NULL;
}

/* The thread-create checkin under test: create a thread, join it, and report whether the join completed. */
static void probe_thread(const char *label)
{
	pthread_t thread;
	int before = workers_ran;
	int rc;

	printf("CORPSEPROBE thread-create %s\n", label);
	fflush(NULL);
	rc = pthread_create(&thread, NULL, worker, NULL);
	if (rc != 0) {
		fail("pthread_create failed");
		return;
	}
	rc = pthread_join(thread, NULL);
	printf("CORPSEPROBE thread-joined %s rc=%d ran=%d\n", label, rc, workers_ran - before);
	if (rc != 0) {
		fail("pthread_join did not complete");
	} else if (workers_ran - before != 1) {
		fail("joined thread did not run");
	}
}

int main(void)
{
	task_t self = mach_task_self();
	kern_return_t kr;
	mach_vm_address_t addr64 = 0;
	mach_vm_size_t size64 = 0;
	vm_address_t addr32 = 0;
	uint32_t size32 = 0;

	printf("CORPSEPROBE getpid=%d self_task=0x%x\n", (int)getpid(), (unsigned)self);

	/* The checkin must be healthy BEFORE the corpse call, so the AFTER probe is a comparison and not a fluke. */
	probe_thread("pre");

	printf("CORPSEPROBE pre-map64\n");
	fflush(NULL);
	kr = task_map_corpse_info_64(self, self, &addr64, &size64);
	printf("CORPSEPROBE map64 kr=%d addr=0x%llx size=%llu\n", (int)kr,
	    (unsigned long long)addr64, (unsigned long long)size64);
	if (kr == KERN_SUCCESS) {
		fail("task_map_corpse_info_64 claimed to map corpse info for a task that is not a corpse");
	} else if (kr != KERN_INVALID_ARGUMENT) {
		fail("task_map_corpse_info_64 did not return KERN_INVALID_ARGUMENT for a non-corpse task");
	}

	printf("CORPSEPROBE pre-map32\n");
	fflush(NULL);
	kr = task_map_corpse_info(self, self, &addr32, &size32);
	printf("CORPSEPROBE map32 kr=%d addr=0x%x size=%u\n", (int)kr, (unsigned)addr32, (unsigned)size32);
	if (kr == KERN_SUCCESS) {
		fail("task_map_corpse_info claimed to map corpse info for a task that is not a corpse");
	} else if (kr != KERN_INVALID_ARGUMENT) {
		fail("task_map_corpse_info did not return KERN_INVALID_ARGUMENT for a non-corpse task");
	}

	/* The point of the whole probe: the transport the corpse call used to destroy must still be there. */
	probe_thread("post");

	printf("CORPSEPROBE VERDICT failures=%d workers_ran=%d\n", failures, workers_ran);
	if (failures == 0) {
		printf("TASK_MAP_CORPSE_INFO_OK\n");
	}
	fflush(NULL);
	return failures == 0 ? 0 : 1;
}
