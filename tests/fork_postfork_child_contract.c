#include <errno.h>
#include <stdint.h>
#include <stdio.h>

#include <darling/emulation/xnu_syscall/bsd/impl/process/fork.h>
#include <darling/emulation/linux_premigration/linux-syscalls/linux.h>

typedef enum {
	EVENT_MLDR_POSTFORK = 1,
	EVENT_GUARD_POSTFORK,
	EVENT_SOCKET_REFRESH,
	EVENT_RING_RESET,
	EVENT_LIFETIME_REFRESH,
	EVENT_GUARD_SOCKET,
	EVENT_GUARD_LIFETIME,
	EVENT_CHECKIN,
	EVENT_FCHDIR,
	EVENT_CLOSE_LIFETIME,
} event_t;

static event_t events[16];
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
	if (nr == __NR_clone || nr == __NR_fork)
		return fork_result;
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

void __mldr_postfork_child(void)
{
	record(EVENT_MLDR_POSTFORK);
}

void guard_table_postfork_child(void)
{
	record(EVENT_GUARD_POSTFORK);
}

void __dserver_per_thread_socket_refresh(void)
{
	record(EVENT_SOCKET_REFRESH);
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

int __dserver_per_thread_socket(void)
{
	return 88;
}

int __dserver_get_process_lifetime_pipe(void)
{
	return 77;
}

void __dserver_close_socket(int fd)
{
	(void)fd;
}

void __dserver_close_process_lifetime_pipe(int fd)
{
	(void)fd;
	record(EVENT_CLOSE_LIFETIME);
}

int dserver_rpc_checkin(int fork_child, void* stack_addr, int lifetime_pipe)
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
	if (fd == 88)
		record(EVENT_GUARD_SOCKET);
	else if (fd == 77)
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
	record(0);
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
		EVENT_MLDR_POSTFORK,
		EVENT_GUARD_POSTFORK,
		EVENT_SOCKET_REFRESH,
		EVENT_LIFETIME_REFRESH,
		EVENT_GUARD_SOCKET,
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
	int j = 0;
	for (int i = 0; i < event_count; ++i) {
		if (events[i] == EVENT_RING_RESET)
			continue;
		if (j >= (int)(sizeof(expected) / sizeof(expected[0]))) {
			fprintf(stderr, "unexpected extra child event %d\n", events[i]);
			return 1;
		}
		if (expect_int("child event order", events[i], expected[j]))
			return 1;
		++j;
	}
	if (expect_int("child required event count", j, (long)(sizeof(expected) / sizeof(expected[0]))))
		return 1;
	if (expect_int("checkin stack args", checkin_stack_seen, 1))
		return 1;
	return 0;
}

static int check_parent_and_error_paths_do_not_run_child_hooks(void)
{
	event_count = 0;
	fork_result = 123;
	if (expect_int("parent ret", sys_fork(), 123))
		return 1;
	if (expect_int("parent events", event_count, 0))
		return 1;

	event_count = 0;
	fork_result = -LINUX_ECHILD;
	if (expect_int("error ret", sys_fork(), -LINUX_ECHILD))
		return 1;
	if (expect_int("error events", event_count, 0))
		return 1;
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
