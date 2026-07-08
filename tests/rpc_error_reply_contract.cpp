#include <cerrno>
#include <cstdlib>
#include <iostream>

struct call_header {
	unsigned int number;
};

struct reply_header {
	unsigned int number;
	int code;
};

#include <darlingserver/rpc-error-reply.hpp>

static void
check(bool condition, const char* message)
{
	if (!condition) {
		std::cerr << message << std::endl;
		std::exit(1);
	}
}

int
main()
{
	call_header call = {};
	call.number = 0x8000002a;

	auto reply = DarlingServer::rpcErrorReplyHeaderFromCall<reply_header>(&call, -ESRCH);
	check(reply.number == call.number, "error reply did not preserve call number");
	check(reply.code == -ESRCH, "error reply did not preserve error code");

	call.number = 0x7f;
	reply = DarlingServer::rpcErrorReplyHeaderFromCall<reply_header>(&call, -EAGAIN);
	check(reply.number == 0x7f, "retry reply used stale call number");
	check(reply.code == -EAGAIN, "retry reply used stale error code");
}
