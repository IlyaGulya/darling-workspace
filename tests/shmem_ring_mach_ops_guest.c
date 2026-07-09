#include <mach/mach.h>
#include <mach/mach_traps.h>
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

int
main(void)
{
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

	puts("WEST_SHMEM_RING_MACH_OPS_OK");
	return 0;
}
