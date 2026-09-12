#include <mach/mach.h>
#include <mach/mach_traps.h>
#include <pthread.h>
#include <stdatomic.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

extern mach_port_t mach_reply_port(void);

static int
check_kr(const char* what, kern_return_t kr)
{
	if (kr == KERN_SUCCESS)
		return 0;
	printf("%s failed: %d\n", what, kr);
	return 1;
}

static int
run_mach_ops(const char* label)
{
	for (int i = 0; i < 64; i++) {
		mach_port_t task = mach_task_self();
		if (task == MACH_PORT_NULL) {
			printf("%s: mach_task_self returned null\n", label);
			return 1;
		}

		mach_port_t receive = MACH_PORT_NULL;
		kern_return_t kr = mach_port_allocate(task, MACH_PORT_RIGHT_RECEIVE, &receive);
		if (check_kr("mach_port_allocate", kr))
			return 1;
		if (receive == MACH_PORT_NULL) {
			printf("%s: mach_port_allocate returned null port\n", label);
			return 1;
		}

		kr = mach_port_insert_right(task, receive, receive, MACH_MSG_TYPE_MAKE_SEND);
		if (check_kr("mach_port_insert_right", kr))
			return 1;

		mach_port_type_t type = 0;
		kr = mach_port_type(task, receive, &type);
		if (check_kr("mach_port_type", kr))
			return 1;
		if ((type & MACH_PORT_TYPE_RECEIVE) == 0 || (type & MACH_PORT_TYPE_SEND) == 0) {
			printf("%s: unexpected port type: 0x%x\n", label, type);
			return 1;
		}

		mach_port_t reply = mach_reply_port();
		if (reply == MACH_PORT_NULL) {
			printf("%s: mach_reply_port returned null\n", label);
			return 1;
		}

		kr = mach_port_deallocate(task, receive);
		if (check_kr("mach_port_deallocate send", kr))
			return 1;
		kr = mach_port_mod_refs(task, receive, MACH_PORT_RIGHT_RECEIVE, -1);
		if (check_kr("mach_port_mod_refs receive", kr))
			return 1;
	}
	return 0;
}

static int
run_host_port_ops(void)
{
	for (int i = 0; i < 32; i++) {
		mach_port_t host = mach_host_self();
		mach_port_type_t type = 0;
		if (host == MACH_PORT_NULL ||
			check_kr("host port type", mach_port_type(mach_task_self(), host, &type)) ||
			(type & MACH_PORT_TYPE_SEND) == 0 ||
			check_kr("host port release", mach_port_deallocate(mach_task_self(), host)))
			return 1;
	}
	return 0;
}

static _Atomic int host_worker_running;
static int host_worker_failed;

static void*
host_port_worker(void* unused)
{
	(void)unused;
	while (atomic_load(&host_worker_running)) {
		if (run_host_port_ops() != 0) {
			host_worker_failed = 1;
			break;
		}
	}
	return NULL;
}

static int
run_host_port_contention(const char* executable)
{
	// Task destruction releases the shared host port on a server worker while
	// another task mints host rights. The contended IPC lock must suspend a
	// real fiber, not jump from inline execution into an earlier task's context.
	pthread_t worker;
	atomic_store(&host_worker_running, 1);
	if (pthread_create(&worker, NULL, host_port_worker, NULL) != 0)
		return 1;
	int result = 0;
	for (int round = 0; round < 32; round++) {
		pid_t children[8];
		int spawned = 0;
		int failed = 0;
		for (; spawned < 8; spawned++) {
			pid_t child = fork();
			if (child < 0) {
				perror("host contention fork");
				failed = 1;
				break;
			}
			if (child == 0) {
				execl(executable, executable, "--host-port-child", (char*)NULL);
				_exit(127);
			}
			children[spawned] = child;
		}
		failed |= run_host_port_ops();
		for (int i = 0; i < spawned; i++) {
			int status = 0;
			if (waitpid(children[i], &status, 0) != children[i] ||
				!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
				printf("host contention child failed: round=%d status=%d\n", round, status);
				failed = 1;
			}
		}
		if (failed) {
			result = 1;
			break;
		}
	}
	atomic_store(&host_worker_running, 0);
	if (pthread_join(worker, NULL) != 0 || host_worker_failed)
		result = 1;
	if (result)
		return result;
	puts("WEST_HOST_PORT_CONTENTION_OK");
	return 0;
}

int
main(int argc, char** argv)
{
	if (argc == 2 && strcmp(argv[1], "--host-port-child") == 0)
		return run_host_port_ops();

	if (run_mach_ops("parent") != 0)
		return 1;

	pid_t pid = fork();
	if (pid < 0) {
		perror("fork");
		return 1;
	}
	if (pid == 0)
		_exit(run_mach_ops("child"));

	int status = 0;
	if (waitpid(pid, &status, 0) != pid) {
		perror("waitpid");
		return 1;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		printf("child mach ops failed: status=%d\n", status);
		return 1;
	}
	if (run_host_port_contention(argv[0]) != 0)
		return 1;


	puts("WEST_SHMEM_RING_MACH_OPS_OK");
	return 0;
}
