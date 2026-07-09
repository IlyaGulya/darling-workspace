#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

static const char *fallback_fault_path = "/private/var/tmp/dserver-fork-checkin-fault";
static volatile sig_atomic_t signals_seen;

static void on_alarm(int signo)
{
	(void)signo;
	++signals_seen;
}

static FILE *open_fault_file(void)
{
	const char *env_path = getenv("DSERVER_TEST_FAULT_FILE");
	if (env_path && env_path[0] == '/') {
		FILE *file = fopen(env_path, "w");
		if (file) {
			return file;
		}

		char system_root_path[4096];
		snprintf(system_root_path, sizeof(system_root_path),
		    "/Volumes/SystemRoot%s", env_path);
		file = fopen(system_root_path, "w");
		if (file) {
			return file;
		}
	}

	return fopen(fallback_fault_path, "w");
}

static void write_fault(void)
{
	FILE *file = open_fault_file();
	if (!file) {
		perror("fopen fault");
		exit(20);
	}
	fputs("fork.skip_checkin_semaphore\n", file);
	fclose(file);
}

static void start_signal_storm(void)
{
	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = on_alarm;
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGALRM, &sa, NULL) != 0) {
		perror("sigaction");
		exit(21);
	}

	struct itimerval timer;
	memset(&timer, 0, sizeof(timer));
	timer.it_interval.tv_usec = 1000;
	timer.it_value.tv_usec = 1000;
	if (setitimer(ITIMER_REAL, &timer, NULL) != 0) {
		perror("setitimer");
		exit(22);
	}
}

static void stop_signal_storm(void)
{
	struct itimerval timer;
	memset(&timer, 0, sizeof(timer));
	(void)setitimer(ITIMER_REAL, &timer, NULL);
}

int main(void)
{
	write_fault();
	start_signal_storm();

	pid_t pid = fork();
	if (pid < 0) {
		perror("fork");
		return 2;
	}

	if (pid == 0) {
		_exit(0);
	}

	stop_signal_storm();

	int status = 0;
	while (waitpid(pid, &status, 0) < 0) {
		if (errno == EINTR) {
			continue;
		}
		perror("waitpid");
		return 3;
	}

	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		fprintf(stderr, "child exited unexpectedly: status=%d\n", status);
		return 4;
	}

	if (signals_seen == 0) {
		fprintf(stderr, "signal storm did not interrupt fork/checkin path\n");
		return 5;
	}

	puts("FORK_CHECKIN_FORCED_LOST_WAKEUP_OK");
	return 0;
}
