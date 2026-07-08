#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static void
expect_ebadf(const char *name, int rc)
{
	if (rc != -1 || errno != EBADF) {
		fprintf(stderr, "%s: rc=%d errno=%d, expected -1/EBADF\n", name, rc, errno);
		_exit(2);
	}
}

static void
run_case(const char *name, void (*fn)(int))
{
	pid_t pid = fork();
	if (pid < 0) {
		perror("fork");
		exit(2);
	}
	if (pid == 0) {
		fn(getdtablesize());
		_exit(0);
	}

	int status = 0;
	if (waitpid(pid, &status, 0) != pid) {
		perror("waitpid");
		exit(2);
	}
	if (WIFSIGNALED(status)) {
		fprintf(stderr, "%s: child died from signal %d\n", name, WTERMSIG(status));
		exit(1);
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		fprintf(stderr, "%s: child exit status 0x%x\n", name, status);
		exit(1);
	}
}

static void
case_dup_from_guarded(int guarded_fd)
{
	errno = 0;
	expect_ebadf("dup(guarded)", dup(guarded_fd));
}

static void
case_dup2_from_guarded(int guarded_fd)
{
	errno = 0;
	expect_ebadf("dup2(guarded, stdout)", dup2(guarded_fd, STDOUT_FILENO));
}

static void
case_dup2_to_guarded(int guarded_fd)
{
	errno = 0;
	expect_ebadf("dup2(stdout, guarded)", dup2(STDOUT_FILENO, guarded_fd));
}

static void
case_fcntl_getfd_guarded(int guarded_fd)
{
	errno = 0;
	expect_ebadf("fcntl(guarded, F_GETFD)", fcntl(guarded_fd, F_GETFD));
}

static void
case_fcntl_dupfd_guarded(int guarded_fd)
{
	errno = 0;
	expect_ebadf("fcntl(guarded, F_DUPFD_CLOEXEC)", fcntl(guarded_fd, F_DUPFD_CLOEXEC, 0));
}

int
main(void)
{
	run_case("dup from guarded", case_dup_from_guarded);
	run_case("dup2 from guarded", case_dup2_from_guarded);
	run_case("dup2 to guarded", case_dup2_to_guarded);
	run_case("fcntl getfd guarded", case_fcntl_getfd_guarded);
	run_case("fcntl dupfd guarded", case_fcntl_dupfd_guarded);

	puts("FD_GUARD_EBADF_GUEST_OK");
	return 0;
}
