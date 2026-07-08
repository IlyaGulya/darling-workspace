#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#include <darlingserver/duct-tape/fatal-exception-reply.h>

static void
check(bool condition, const char* message)
{
	if (!condition) {
		fprintf(stderr, "%s\n", message);
		exit(1);
	}
}

int
main(void)
{
	check(!dtape_exception_reply_wait_is_bounded(false),
	    "non-fatal reply wait is unexpectedly bounded");
	check(dtape_exception_reply_wait_option(false) == MACH_MSG_OPTION_NONE,
	    "non-fatal reply wait should not set receive timeout");
	check(dtape_exception_reply_wait_timeout(false) == MACH_MSG_TIMEOUT_NONE,
	    "non-fatal reply wait should remain unbounded");

	check(dtape_exception_reply_wait_is_bounded(true),
	    "fatal reply wait is not bounded");
	check(dtape_exception_reply_wait_option(true) == MACH_RCV_TIMEOUT,
	    "fatal reply wait should arm MACH_RCV_TIMEOUT");
	check(dtape_exception_reply_wait_timeout(true) == DTAPE_FATAL_EXC_REPLY_TIMEOUT_MS,
	    "fatal reply wait should use the configured timeout");
	check(dtape_exception_reply_wait_timeout(true) == 3000,
	    "fatal reply wait timeout should default to 3000ms");
}
