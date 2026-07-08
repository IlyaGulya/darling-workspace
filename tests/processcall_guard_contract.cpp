#include <cerrno>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <system_error>

#include <darlingserver/processcall-guard.hpp>

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
	bool ran = false;
	check(DarlingServer::processCallBasicReplyCode([&]() { ran = true; }) == 0,
	    "non-throwing processCall did not return success");
	check(ran, "non-throwing processCall was not invoked");

	check(DarlingServer::processCallBasicReplyCode([]() {
		throw std::system_error(ESRCH, std::generic_category());
	}) == -ESRCH, "system_error did not map to negative errno");

	check(DarlingServer::processCallBasicReplyCode([]() {
		throw std::runtime_error("bad call");
	}) == -EINVAL, "std exception did not map to EINVAL");

	check(DarlingServer::processCallBasicReplyCode([]() {
		throw 17;
	}) == -EINVAL, "non-std exception did not map to EINVAL");
}
