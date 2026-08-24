#include <darlingserver/lifecycle-bootstrap.hpp>
#include <darlingserver/rootless-session-drain.hpp>

#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstdlib>
#include <iostream>

extern "C" int shutdown_rootless_lifecycle_controller(
	int controller_pidfd, int timeout_ms);

static void require(bool condition, const char* message) {
	if (!condition) {
		std::cerr << "FAIL " << message << std::endl;
		std::exit(1);
	}
}

static pid_t waitingChild() {
	pid_t child = fork();
	require(child >= 0, "fork waiting child");
	if (child == 0) {
		for (;;)
			pause();
	}
	return child;
}

static void bootstrapContract() {
	int sockets[2];
	require(socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, sockets) == 0,
		"bootstrap socketpair");
	int directory = open("/tmp", O_PATH | O_DIRECTORY | O_CLOEXEC);
	require(directory >= 0, "bootstrap directory");
	darling_lifecycle_cohort_bootstrap envelope = {};
	const char name[] = "/.lc-v1.sock";
	memcpy(envelope.control_name, name, sizeof(name) - 1);
	envelope.control_name_len = sizeof(name) - 1;
	memset(envelope.nonce_hex, 'a', sizeof(envelope.nonce_hex));
	require(DarlingServer::sendLifecycleBootstrap(sockets[0], directory, envelope),
		"send typed bootstrap");
	auto received = DarlingServer::receiveLifecycleBootstrap(sockets[1]);
	require(received.directoryFD >= 0, "receive retained directory");
	require((fcntl(received.directoryFD, F_GETFD) & FD_CLOEXEC) != 0,
		"received descriptor cloexec");
	require(received.envelope.control_name_len == envelope.control_name_len,
		"bootstrap envelope identity");
	close(received.directoryFD);

	envelope.nonce_hex[0] = 'X';
	require(!DarlingServer::sendLifecycleBootstrap(sockets[0], directory, envelope),
		"malformed bootstrap rejected");
	close(directory);
	close(sockets[0]);
	close(sockets[1]);
}

static void processDrainContract() {
	require(prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0,
		"enable subreaper");
	pid_t retainedController = waitingChild();
	pid_t intermediate = fork();
	require(intermediate >= 0, "fork intermediate");
	if (intermediate == 0) {
		pid_t leaf = fork();
		if (leaf < 0)
			_exit(2);
		if (leaf == 0) {
			if (setsid() < 0)
				_exit(3);
			char program[] = "/bin/sleep";
			char duration[] = "30";
			char* argv[] = {program, duration, nullptr};
			char* environment[] = {nullptr};
			execve(program, argv, environment);
			_exit(4);
		}
		_exit(0);
	}
	int status = 0;
	require(waitpid(intermediate, &status, 0) == intermediate && WIFEXITED(status),
		"reap intermediate");
	usleep(100000);
	require(DarlingServer::drainRootlessSessionChildren(
		retainedController, std::chrono::milliseconds(500),
		std::chrono::seconds(2)) == 0, "bounded descendant drain");
	require(kill(retainedController, 0) == 0, "controller exclusion retained");

	int children = open("/proc/thread-self/children", O_RDONLY | O_CLOEXEC);
	require(children >= 0, "open final child census");
	char census[128] = {};
	ssize_t count = read(children, census, sizeof(census) - 1);
	close(children);
	require(count > 0, "retained controller present");
	char expected[32];
	snprintf(expected, sizeof(expected), "%d", retainedController);
	require(strstr(census, expected) != nullptr, "only retained controller identity");

	kill(retainedController, SIGKILL);
	require(waitpid(retainedController, &status, 0) == retainedController,
		"reap retained controller");
}

static void startupShutdownOrderingContract() {
	int ready[2];
	require(pipe(ready) == 0, "startup ordering pipe");
	pid_t child = fork();
	require(child >= 0, "startup ordering fork");
	if (child == 0) {
		close(ready[0]);
		if (DarlingServer::blockRootlessLifecycleTerminationSignals() != 0)
			_exit(2);
		const char published = 'P';
		if (write(ready[1], &published, 1) != 1)
			_exit(3);
		close(ready[1]);
		usleep(200000);
		sigset_t terminationSignals;
		sigemptyset(&terminationSignals);
		sigaddset(&terminationSignals, SIGTERM);
		int signalNumber = 0;
		if (sigwait(&terminationSignals, &signalNumber) != 0 ||
			signalNumber != SIGTERM)
			_exit(4);
		_exit(0);
	}
	close(ready[1]);
	char published = 0;
	require(read(ready[0], &published, 1) == 1 && published == 'P',
		"startup identity published after signal mask");
	close(ready[0]);
	require(kill(child, SIGTERM) == 0, "shutdown during startup");
	usleep(50000);
	int status = 0;
	require(waitpid(child, &status, WNOHANG) == 0,
		"blocked termination cannot bypass graceful consumer");
	require(waitpid(child, &status, 0) == child && WIFEXITED(status) &&
		WEXITSTATUS(status) == 0, "startup termination consumed deterministically");
}

static void retainedPidfdReuseContract() {
#if defined(SYS_pidfd_open)
	pid_t original = waitingChild();
	int pidfd = static_cast<int>(syscall(SYS_pidfd_open, original, 0));
	require(pidfd >= 0, "retain shutdown pidfd before identity validation ends");
	require(kill(original, SIGKILL) == 0, "terminate original shutdown target");
	int status = 0;
	require(waitpid(original, &status, 0) == original, "reap original target");

	pid_t unrelated = waitingChild();
	const int result = shutdown_rootless_lifecycle_controller(pidfd, 50);
	require(result == -ESRCH, "gone retained pidfd is typed and not reopened by PID");
	require(kill(unrelated, 0) == 0, "unrelated replacement remains alive");
	close(pidfd);
	require(kill(unrelated, SIGKILL) == 0, "terminate unrelated process");
	require(waitpid(unrelated, &status, 0) == unrelated, "reap unrelated process");
#endif
}

int main() {
	bootstrapContract();
	processDrainContract();
	startupShutdownOrderingContract();
	retainedPidfdReuseContract();
	std::cout << "LIFECYCLE_PRODUCT_COHORT_HOST_VALID" << std::endl;
	return 0;
}
