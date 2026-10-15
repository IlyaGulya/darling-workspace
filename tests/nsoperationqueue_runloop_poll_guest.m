// nsoperationqueue_runloop_poll_guest -- the shape xcodebuild parks in, reduced to a fixture.
//
// WHY THIS EXISTS. Reading the deployed Xcode3Core pinned the wall exactly (see
// evidence/oslog-framework-rung/README.md section 24): the arm's main thread sits in
//
//     -[Xcode3CommandLineBuildTool waitForBuildWithBuildLog:...]   (file VM 0x2a6dc)
//         setCompletionBlock: / executeOperation:withCommandName:... (or buildToolQueue addOperation:)
//         while (![operationToEnqueue isFinished]) CFRunLoopRun();     <- callq at 0x2a9d1, return 0x2a9d6
//
// i.e. an asynchronous NSOperation's completion is POLLED between BARE, ONE-SHOT CFRunLoopRun() calls, and
// NSOperation/NSOperationQueue/CFRunLoop in this environment are Darling's implementations. The arm burns
// ~0 CPU in an infinite receive, so CFRunLoopRun() is not returning to re-poll. This fixture separates the
// candidates by measurement, each variant with its own marker, its own wall time and a bounded deadline:
//
//   (a) private concurrent NSOperationQueue + NSBlockOperation, polled by      while (![op isFinished]) CFRunLoopRun();
//   (b) the same on [NSOperationQueue mainQueue];
//   (c) a main-queue dispatch_async setting a flag, polled by                   while (!flag) CFRunLoopRun();
//   (d) control: the same main-queue setter, polled by a BOUNDED run loop       while (!flag) CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, false);
//   (e) control: a background-queue setter, polled by the same bounded loop;
//   (f) return-control: nothing to poll at all -- a background thread stops the run loop after 300 ms; does a
//       bare CFRunLoopRun() return at all?
//   (g) the DVTOperation shape: a CUSTOM ASYNCHRONOUS NSOperation (isAsynchronous = YES, KVO'd isFinished set
//       from a background block when its work finishes), polled by the bare loop.
//
// The deadline is not a timeout of the fixture: it is part of the experiment. A background timer stops the
// main run loop at DEADLINE_SECS and marks the deadline; the loop then re-polls once and the variant reports
// HUNG_AT_DEADLINE (the loop did NOT leave on its own), versus EXITED (it did). Every variant therefore
// finishes in bounded time, and a hang is a printed FAIL rather than a stall.
//
// VERDICT LINES: `Vx BEGIN <ms>`, `Vx RESULT=<EXITED|HUNG_AT_DEADLINE> ...`, and the final
// `NSOPERATIONQUEUE_RUNLOOP_POLL_DONE` marker. The marker asserts that every variant reached a bounded
// result; the per-variant RESULT lines are what say which primitive failed.
//
// WEAK SPOT, stated rather than hidden (the same rule the fcd_sync_send_park_guest registration states): if
// this fixture cannot create a queue at all (nil from [NSOperationQueue new]), the variant reports
// NIL_QUEUE and still reaches the final marker. The registration's own run quotes the RESULT lines.

#import <Foundation/Foundation.h>
#import <dispatch/dispatch.h>
#import <CoreFoundation/CoreFoundation.h>

#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#define DEADLINE_SECS 2.0

static volatile int g_flag;      // set by the async completion under test
static volatile int g_deadline;  // set when the deadline timer fired

static uint64_t now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t) ts.tv_sec * 1000u + (uint64_t) ts.tv_nsec / 1000000u;
}

// Arm a ONE-SHOT deadline on a background queue: it marks g_deadline and stops the main run loop, so a bare
// CFRunLoopRun() that would otherwise block forever returns and the variant can report HUNG_AT_DEADLINE.
static void arm_deadline(void)
{
	g_deadline = 0;
	dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t) (DEADLINE_SECS * NSEC_PER_SEC)),
		       dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{
		g_deadline = 1;
		CFRunLoopStop(CFRunLoopGetMain());
	});
}

static void report(const char* variant, const char* result, const char* detail, uint64_t begin, int flag, int deadline, NSString* extra)
{
	printf("%s RESULT=%s wall_ms=%llu flag=%d deadline=%d %s%s%s\n",
	       variant, result, (unsigned long long) (now_ms() - begin), flag, deadline,
	       detail ? detail : "",
	       extra ? " " : "",
	       extra ? [extra UTF8String] : "");
	fflush(stdout);
}

static void begin(const char* variant)
{
	printf("%s BEGIN %llu\n", variant, (unsigned long long) now_ms());
	fflush(stdout);
}

// (a) and (b): an NSOperationQueue-backed block operation, polled exactly as the tool polls its DVTOperation.
static void operation_variant(const char* name, NSOperationQueue* queue)
{
	begin(name);
	if (queue == nil) {
		report(name, "NIL_QUEUE", "the variant could not get a queue", now_ms(), 0, 0, nil);
		return;
	}

	g_flag = 0;
	NSBlockOperation* op = [NSBlockOperation blockOperationWithBlock: ^{
		g_flag = 1;
	}];
	[queue addOperation: op];

	uint64_t begin_at = now_ms();
	arm_deadline();
	// THE SHAPE UNDER TEST, verbatim: a bare one-shot run loop, exited only by the polled predicate.
	while (![op isFinished] && !g_deadline)
		CFRunLoopRun();
	uint64_t wall = now_ms() - begin_at;

	NSString* extra = [NSString stringWithFormat:
		@"finished=%d executing=%d cancelled=%d queue_count=%lu",
		[op isFinished], [op isExecuting], [op isCancelled], (unsigned long) [queue operationCount]];
	report(name, [op isFinished] ? "EXITED" : "HUNG_AT_DEADLINE", "predicate=isFinished", begin_at, g_flag, g_deadline, extra);
	(void) wall;
	// Leave the queue empty for the next variant without forcing execution.
	[queue cancelAllOperations];
}

// (c): a main-queue dispatch_async, polled by the bare loop. The run loop itself is the only thing that can
// drain the main queue here.
static void bare_loop_flag_variant(void)
{
	begin("Vc");
	g_flag = 0;
	dispatch_async(dispatch_get_main_queue(), ^{
		g_flag = 1;
	});

	uint64_t begin_at = now_ms();
	arm_deadline();
	while (!g_flag && !g_deadline)
		CFRunLoopRun();
	report("Vc", g_flag ? "EXITED" : "HUNG_AT_DEADLINE", "setter=main-queue dispatch_async", begin_at, g_flag, g_deadline, nil);
}

// (d) and (e): the same setters, but the run loop is BOUNDED, so the loop always returns and re-polls. This
// isolates "the completion never ran" from "the bare loop never returns".
static void bounded_loop_variant(const char* name, int from_main_queue)
{
	begin(name);
	g_flag = 0;
	if (from_main_queue) {
		dispatch_async(dispatch_get_main_queue(), ^{
			g_flag = 1;
		});
	} else {
		dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{
			g_flag = 1;
		});
	}

	uint64_t begin_at = now_ms();
	arm_deadline();
	while (!g_flag && !g_deadline)
		CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, false);
	report(name, g_flag ? "EXITED" : "HUNG_AT_DEADLINE",
	       from_main_queue ? "setter=main-queue, loop=bounded" : "setter=background-queue, loop=bounded",
	       begin_at, g_flag, g_deadline, nil);
}

// (f): does a BARE CFRunLoopRun() return when a background thread stops it, with nothing to poll at all?
static void bare_loop_return_variant(void)
{
	begin("Vf");
	g_deadline = 0;
	dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t) (0.3 * NSEC_PER_SEC)),
		       dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{
		g_deadline = 1;
		CFRunLoopStop(CFRunLoopGetMain());
	});

	uint64_t begin_at = now_ms();
	CFRunLoopRun();
	report("Vf", g_deadline ? "EXITED" : "RETURNED_EARLY", "no predicate: a bare CFRunLoopRun stopped by another thread", begin_at, 0, g_deadline, nil);
}

// (g): the DVTOperation shape -- a custom ASYNCHRONOUS operation whose isFinished is set by its own async
// completion, which is what the tool polls with [operationToEnqueue isFinished].
@interface PollAsyncOperation : NSOperation
@end

@implementation PollAsyncOperation {
	BOOL _finished;
	BOOL _executing;
}

- (BOOL) isAsynchronous { return YES; }
- (BOOL) isExecuting { return _executing; }
- (BOOL) isFinished { return _finished; }

- (void) start
{
	// An asynchronous operation must set these with KVO, the way DVT's operations do.
	[self willChangeValueForKey: @"isExecuting"];
	_executing = YES;
	[self didChangeValueForKey: @"isExecuting"];

	dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{
		usleep(100 * 1000);	// the work
		g_flag = 1;
		[self willChangeValueForKey: @"isExecuting"];
		[self willChangeValueForKey: @"isFinished"];
		_executing = NO;
		_finished = YES;
		[self didChangeValueForKey: @"isFinished"];
		[self didChangeValueForKey: @"isExecuting"];
	});
}

@end

static void async_operation_variant(void)
{
	begin("Vg");
	g_flag = 0;
	NSOperationQueue* queue = [[NSOperationQueue alloc] init];
	[queue setMaxConcurrentOperationCount: 1];
	PollAsyncOperation* op = [[PollAsyncOperation alloc] init];
	[queue addOperation: op];

	uint64_t begin_at = now_ms();
	arm_deadline();
	while (![op isFinished] && !g_deadline)
		CFRunLoopRun();

	NSString* extra = [NSString stringWithFormat: @"finished=%d executing=%d flag=%d", [op isFinished], [op isExecuting], g_flag];
	report("Vg", [op isFinished] ? "EXITED" : "HUNG_AT_DEADLINE", "asynchronous NSOperation subclass", begin_at, g_flag, g_deadline, extra);
}

int main(void)
{
	// An outer backstop so a fault in the fixture itself cannot hang the guest run: the harness's own
	// timeout is the second backstop.
	alarm(45);

	printf("NSOPERATIONQUEUE_RUNLOOP_POLL deadline_secs=%.2f\n", DEADLINE_SECS);
	fflush(stdout);

	@autoreleasepool {
		NSOperationQueue* private_queue = [[NSOperationQueue alloc] init];
		[private_queue setMaxConcurrentOperationCount: 4];
		operation_variant("Va", private_queue);

		operation_variant("Vb", [NSOperationQueue mainQueue]);
		bare_loop_flag_variant();
		bounded_loop_variant("Vd", 1);
		bounded_loop_variant("Ve", 0);
		bare_loop_return_variant();
		async_operation_variant();
	}

	printf("NSOPERATIONQUEUE_RUNLOOP_POLL_DONE\n");
	fflush(stdout);
	return 0;
}
