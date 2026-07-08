#include <cstdlib>
#include <iostream>

#include <darlingserver/microthread-resume.hpp>

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
	bool permit = false;

	check(!DarlingServer::microthreadRecordResume(false, false, permit),
	    "idle microthread was scheduled");
	check(!permit, "idle microthread recorded a stale permit");

	check(!DarlingServer::microthreadRecordResume(true, false, permit),
	    "running microthread was scheduled before it physically stopped");
	check(permit, "running microthread did not record resume permit");
	check(!DarlingServer::microthreadRecordResume(true, false, permit),
	    "duplicate running resume was not coalesced");
	check(DarlingServer::microthreadConsumePendingResume(permit),
	    "suspend did not consume early resume permit");
	check(!permit, "early resume permit remained after consume");

	bool suspended = false;
	permit = true;
	check(DarlingServer::microthreadConsumeResumeDuringSuspend(suspended, permit),
	    "context-capture race did not consume resume permit");
	check(!suspended, "context-capture race left thread suspended");
	check(!permit, "context-capture race left resume permit set");

	suspended = true;
	permit = false;
	check(DarlingServer::microthreadRecordResume(false, suspended, permit),
	    "parked resume did not request scheduling");
	check(permit, "parked resume did not record permit");

	check(DarlingServer::microthreadShouldScheduleAfterStop(true, true, false, false),
	    "late resume after physical stop did not schedule");
	check(!DarlingServer::microthreadShouldScheduleAfterStop(true, true, true, false),
	    "terminating thread scheduled after stop");
	check(!DarlingServer::microthreadShouldScheduleAfterStop(true, true, false, true),
	    "dead thread scheduled after stop");
	check(!DarlingServer::microthreadShouldScheduleAfterStop(true, false, false, false),
	    "non-suspended thread scheduled after stop");
}
