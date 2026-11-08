#include <errno.h>
#include <stdint.h>
#include <stdio.h>

#include <darling/emulation/xnu_syscall/bsd/impl/process/fork.h>
#include <darling/emulation/linux_premigration/linux-syscalls/linux.h>
#include <darlingserver/rpc.h>

typedef enum {
	EVENT_MLDR_POSTFORK = 1,
	EVENT_GUARD_POSTFORK,
	EVENT_RING_RESET,
	EVENT_LIFETIME_REFRESH,
	EVENT_GUARD_LIFETIME,
	EVENT_CHECKIN,
	EVENT_FCHDIR,
	EVENT_CLOSE_LIFETIME,
	EVENT_AUDIT_PREPARE,
	EVENT_SAVEDREF_PREPARE,
	EVENT_WORKQ_PREPARE,
	EVENT_MLDR_PREPARE,
	EVENT_FORK,
	EVENT_MLDR_PARENT,
	EVENT_WORKQ_RELEASE,
	EVENT_SAVEDREF_RELEASE,
	EVENT_AUDIT_RELEASE,
} event_t;

static event_t events[32];
static int event_count;
static long fork_result;
static int checkin_stack_seen;

static void record(event_t event)
{
	if (event_count < (int)(sizeof(events) / sizeof(events[0])))
		events[event_count++] = event;
}

long test_linux_syscall(long nr, ...)
{
	if (nr == __NR_clone || nr == __NR_fork) {
		record(EVENT_FORK);
		return fork_result;
	}
	return -22;
}

int errno_linux_to_bsd(int err)
{
	return err;
}

int get_perthread_wd(void)
{
	return 55;
}

void darling_audit_bookkeeping_prefork_prepare(void) { record(EVENT_AUDIT_PREPARE); }
void darling_savedref_bookkeeping_prefork_prepare(void) { record(EVENT_SAVEDREF_PREPARE); }
void darling_workq_bookkeeping_prefork_prepare(void) { record(EVENT_WORKQ_PREPARE); }
void darling_workq_bookkeeping_prefork_release(void) { record(EVENT_WORKQ_RELEASE); }
void darling_savedref_bookkeeping_prefork_release(void) { record(EVENT_SAVEDREF_RELEASE); }
void darling_audit_bookkeeping_prefork_release(void) { record(EVENT_AUDIT_RELEASE); }
void __mldr_prefork_prepare(void) { record(EVENT_MLDR_PREPARE); }
void __mldr_postfork_parent(void) { record(EVENT_MLDR_PARENT); }

void __mldr_postfork_child(void)
{
	record(EVENT_MLDR_POSTFORK);
}

void guard_table_postfork_child(void)
{
	record(EVENT_GUARD_POSTFORK);
}


void __dserver_ring_postfork_reset(void)
{
	record(EVENT_RING_RESET);
}

int __dserver_process_lifetime_pipe_refresh(void)
{
	record(EVENT_LIFETIME_REFRESH);
	return 77;
}


int __dserver_get_process_lifetime_pipe(void)
{
	return 77;
}


void __dserver_close_process_lifetime_pipe(int fd)
{
	(void)fd;
	record(EVENT_CLOSE_LIFETIME);
}

/* Exercise the supported pre-publication RPC route, not a fabricated page completion. */
void* __dserver_process_control_page(void) { return 0; }
uint64_t __dserver_fd_courier_send(int fd, uint32_t kind)
{
	(void)fd; (void)kind;
	__builtin_trap();
}
int __dserver_fd_courier_socket(void) { __builtin_trap(); }
int dserver_rpc_checkin(_Bool fork_child, void* stack_addr, int lifetime_pipe)
{
	if (fork_child == 1 && stack_addr != 0 && lifetime_pipe == 77)
		checkin_stack_seen = 1;
	record(EVENT_CHECKIN);
	return 0;
}

long sys_fchdir(int fd)
{
	if (fd != 55)
		return -1;
	record(EVENT_FCHDIR);
	return 0;
}

typedef struct {
	void (*close)(int fd);
} guard_entry_options_t;

void guard_table_add(int fd, int flags, guard_entry_options_t* options)
{
	if (fd != 77) __builtin_trap();
	record(EVENT_GUARD_LIFETIME);
	if (flags == 0 || options == 0 || options->close == 0)
		record(0);
}

void __simple_printf(const char* message, ...)
{
	(void)message;
}

void __simple_abort(void)
{
	__builtin_trap();
}

static int expect_int(const char* label, long got, long want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %ld, want %ld\n", label, got, want);
	return 1;
}

static int check_child_path_order(void)
{
	static const event_t expected[] = {
		EVENT_AUDIT_PREPARE,
		EVENT_SAVEDREF_PREPARE,
		EVENT_WORKQ_PREPARE,
		EVENT_MLDR_PREPARE,
		EVENT_FORK,
		EVENT_MLDR_POSTFORK,
		EVENT_WORKQ_RELEASE,
		EVENT_SAVEDREF_RELEASE,
		EVENT_AUDIT_RELEASE,
		EVENT_GUARD_POSTFORK,
		EVENT_RING_RESET,
		EVENT_LIFETIME_REFRESH,
		EVENT_GUARD_LIFETIME,
		EVENT_CHECKIN,
		EVENT_FCHDIR,
		EVENT_CLOSE_LIFETIME,
	};

	event_count = 0;
	checkin_stack_seen = 0;
	fork_result = 0;
	long ret = sys_fork();
	if (expect_int("child ret", ret, 0))
		return 1;
	if (expect_int("child event count", event_count, (long)(sizeof(expected) / sizeof(expected[0]))))
		return 1;
	for (int i = 0; i < event_count; ++i) {
		if (expect_int("child event order", events[i], expected[i]))
			return 1;
	}
	if (expect_int("checkin stack args", checkin_stack_seen, 1))
		return 1;
	return 0;
}

static int check_parent_and_error_paths_do_not_run_child_hooks(void)
{
	static const event_t expected[] = {
		EVENT_AUDIT_PREPARE, EVENT_SAVEDREF_PREPARE, EVENT_WORKQ_PREPARE,
		EVENT_MLDR_PREPARE, EVENT_FORK, EVENT_MLDR_PARENT,
		EVENT_WORKQ_RELEASE, EVENT_SAVEDREF_RELEASE, EVENT_AUDIT_RELEASE,
	};
	event_count = 0;
	fork_result = 123;
	if (expect_int("parent ret", sys_fork(), 123))
		return 1;
	if (expect_int("parent events", event_count, sizeof(expected) / sizeof(expected[0])))
		return 1;
	for (int i = 0; i < event_count; ++i)
		if (expect_int("parent order", events[i], expected[i])) return 1;

	event_count = 0;
	fork_result = -LINUX_ECHILD;
	if (expect_int("error ret", sys_fork(), -LINUX_ECHILD))
		return 1;
	if (expect_int("error events", event_count, sizeof(expected) / sizeof(expected[0])))
		return 1;
	for (int i = 0; i < event_count; ++i)
		if (expect_int("error order", events[i], expected[i])) return 1;
	return 0;
}

int main(void)
{
	if (check_child_path_order())
		return 1;
	if (check_parent_and_error_paths_do_not_run_child_hooks())
		return 1;
	puts("GREEN: fork postfork child contract");
	return 0;
}
