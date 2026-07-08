#include <cstdlib>
#include <iostream>
#include <unistd.h>

#include <darlingserver/push-reply-sync-pipe.hpp>

static void
check(bool condition, const char* message)
{
	if (!condition) {
		std::cerr << message << std::endl;
		std::exit(1);
	}
}

static void
makePipe(int fds[2])
{
	check(pipe(fds) == 0, "pipe failed");
}

int
main()
{
	int fds[2];
	makePipe(fds);
	{
		DarlingServer::PushReplySyncPipe sync(fds[1]);
		check(!!sync, "valid sync pipe reported invalid");
	}
	char byte = 0;
	check(read(fds[0], &byte, 1) == 0, "unacknowledged sync pipe did not close to EOF");
	close(fds[0]);

	makePipe(fds);
	{
		DarlingServer::PushReplySyncPipe sync(fds[1]);
		sync.acknowledge();
		check(read(fds[0], &byte, 1) == 1, "acknowledged sync pipe did not deliver a byte");
		check(byte == 1, "acknowledged sync pipe delivered wrong byte");
	}
	check(read(fds[0], &byte, 1) == 0, "acknowledged sync pipe did not close after ack");
	close(fds[0]);

	DarlingServer::PushReplySyncPipe invalid(-1);
	check(!invalid, "invalid sync pipe reported valid");
}
