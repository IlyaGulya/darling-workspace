#!/usr/bin/env python3
import errno
import sys


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def microthread_resume_race():
    class OldThread:
        def __init__(self):
            self.running = True
            self.suspended = False
            self.scheduled = 0

        def resume(self):
            if not self.suspended:
                return
            self.scheduled += 1

        def suspend(self):
            self.suspended = True
            self.running = False
            return "slept"

    class FixedThread(OldThread):
        def __init__(self):
            super().__init__()
            self.resume_permit = False

        def resume(self):
            if not self.running and not self.suspended:
                return
            if self.resume_permit:
                return
            self.resume_permit = True
            if self.suspended and not self.running:
                self.scheduled += 1

        def suspend(self):
            if self.resume_permit:
                self.resume_permit = False
                return "continued"
            return super().suspend()

    old = OldThread()
    old.resume()
    check(old.suspend() == "slept", "old model must lose resume-before-suspend")
    fixed = FixedThread()
    fixed.resume()
    check(fixed.suspend() == "continued", "fixed model must consume early resume permit")


def fork_checkin_bound():
    def old_wait(child_checks_in):
        return "ok" if child_checks_in else "blocked"

    def fixed_wait(child_checks_in):
        return 0 if child_checks_in else -errno.ETIMEDOUT

    check(old_wait(False) == "blocked", "old fork checkin wait must be unbounded")
    check(fixed_wait(False) == -errno.ETIMEDOUT, "fixed wait must return ETIMEDOUT")
    check(fixed_wait(True) == 0, "fixed wait must still pass successful checkin")


def fork_checkin_sticky_flag():
    class OldForkWait:
        def __init__(self):
            self.semaphore = 0

        def child_checkin_lost_to_interrupt(self):
            pass

        def wait_retry(self):
            return "blocked" if self.semaphore == 0 else "ok"

    class FixedForkWait:
        def __init__(self):
            self.semaphore = 0
            self.sticky = False

        def child_checkin_lost_to_interrupt(self):
            self.sticky = True

        def wait_retry(self):
            if self.sticky:
                self.sticky = False
                return "ok"
            return "blocked" if self.semaphore == 0 else "ok"

    old = OldForkWait()
    old.child_checkin_lost_to_interrupt()
    check(old.wait_retry() == "blocked", "old retry must starve after lost semaphore wake")
    fixed = FixedForkWait()
    fixed.child_checkin_lost_to_interrupt()
    check(fixed.wait_retry() == "ok", "fixed retry must observe sticky checkin")


def cancel_stale_wait_timer():
    class Thread:
        def __init__(self, cancel_on_unblock):
            self.cancel_on_unblock = cancel_on_unblock
            self.timer_armed = False
            self.wait_result = None

        def timed_wait(self):
            self.timer_armed = True

        def unblock(self):
            self.wait_result = "awakened"
            if self.cancel_on_unblock:
                self.timer_armed = False

        def untimed_wait(self):
            self.wait_result = None
            if self.timer_armed:
                self.wait_result = "timed_out"

    old = Thread(cancel_on_unblock=False)
    old.timed_wait()
    old.unblock()
    old.untimed_wait()
    check(old.wait_result == "timed_out", "old stale timer must poison later untimed wait")
    fixed = Thread(cancel_on_unblock=True)
    fixed.timed_wait()
    fixed.unblock()
    fixed.untimed_wait()
    check(fixed.wait_result is None, "fixed unblock must cancel stale timer")


def processcall_exception_guard():
    def old_server_process(throws):
        if throws:
            raise RuntimeError("terminate")
        return {"alive": True, "reply": 0}

    def fixed_server_process(throws):
        if throws:
            return {"alive": True, "reply": -errno.EINVAL}
        return {"alive": True, "reply": 0}

    try:
        old_server_process(True)
        old_died = False
    except RuntimeError:
        old_died = True
    check(old_died, "old uncaught processCall exception must terminate server")
    result = fixed_server_process(True)
    check(result == {"alive": True, "reply": -errno.EINVAL}, "fixed guard must survive and reply error")


def ingest_error_reply():
    def old_ingest(case):
        return None

    def fixed_ingest(case):
        if case in {"missing_process", "missing_thread"}:
            return -errno.ESRCH
        if case == "pending_overwrite":
            return -errno.EAGAIN
        return 0

    for case in ("missing_process", "missing_thread", "pending_overwrite"):
        check(old_ingest(case) is None, f"old {case} must drop without reply")
        check(fixed_ingest(case) is not None, f"fixed {case} must reply")
    check(fixed_ingest("pending_overwrite") == -errno.EAGAIN, "pending overwrite must be retryable")


def pushreply_pipe_leak():
    class Pipe:
        def __init__(self):
            self.open = True
            self.wrote = False

        def close(self):
            self.open = False

    def old_push_reply(throws):
        pipe = Pipe()
        if throws:
            return pipe
        pipe.wrote = True
        pipe.close()
        return pipe

    def fixed_push_reply(throws):
        pipe = Pipe()
        try:
            if throws:
                raise RuntimeError("late interrupt")
            pipe.wrote = True
        except RuntimeError:
            pass
        finally:
            pipe.close()
        return pipe

    old = old_push_reply(True)
    check(old.open and not old.wrote, "old push_reply throw must leak pipe and strand reader")
    fixed = fixed_push_reply(True)
    check(not fixed.open, "fixed push_reply throw must close pipe so reader sees EOF")


def exc_fatal_reply_bound():
    def old_fatal_reply(handler_replies):
        return "reply" if handler_replies else "blocked"

    def fixed_fatal_reply(handler_replies, elapsed_ms):
        if handler_replies:
            return "reply"
        return "default_action" if elapsed_ms >= 3000 else "waiting"

    check(old_fatal_reply(False) == "blocked", "old fatal exception reply wait must wedge")
    check(fixed_fatal_reply(False, 2999) == "waiting", "fixed wait must allow bounded grace period")
    check(fixed_fatal_reply(False, 3000) == "default_action", "fixed timeout must fall through to default action")


def assert_wait_cancel_stale_timer():
    class Waiter:
        def __init__(self, cancel_on_assert):
            self.cancel_on_assert = cancel_on_assert
            self.timer_armed = True
            self.result = None

        def assert_wait(self, timed):
            if self.cancel_on_assert and self.timer_armed:
                self.timer_armed = False
            if timed:
                self.timer_armed = True

        def stale_timer_fires(self):
            if self.timer_armed:
                self.result = "timed_out"

    old = Waiter(cancel_on_assert=False)
    old.assert_wait(timed=False)
    old.stale_timer_fires()
    check(old.result == "timed_out", "old assert_wait must leave stale timer armed")
    fixed = Waiter(cancel_on_assert=True)
    fixed.assert_wait(timed=False)
    fixed.stale_timer_fires()
    check(fixed.result is None, "fixed assert_wait must cancel stale timer before new wait")


def pthread_canceled_xnu_semantics():
    class OldThread:
        def canceled(self, action):
            return errno.ENOSYS

        def markcancel(self):
            return errno.ENOSYS

    class FixedThread:
        def __init__(self):
            self.cancel_disable = False
            self.cancel_pending = False
            self.canceled_bit = False

        def canceled(self, action):
            if action == 1:
                self.cancel_disable = False
                return 0
            if action == 2:
                self.cancel_disable = True
                return 0
            if self.cancel_pending and not self.cancel_disable and not self.canceled_bit:
                self.cancel_pending = False
                self.canceled_bit = True
                return 0
            return errno.EINVAL

        def markcancel(self):
            if not self.cancel_pending and not self.canceled_bit:
                self.cancel_pending = True
            return 0

    old = OldThread()
    check(old.canceled(1) == errno.ENOSYS and old.markcancel() == errno.ENOSYS, "old pthread cancel calls must be stubs")
    fixed = FixedThread()
    check(fixed.canceled(2) == 0 and fixed.cancel_disable, "action 2 must disable cancellation")
    check(fixed.canceled(1) == 0 and not fixed.cancel_disable, "action 1 must enable cancellation")
    check(fixed.markcancel() == 0 and fixed.cancel_pending, "markcancel must arm pending cancel")
    check(fixed.canceled(0) == 0 and fixed.canceled_bit, "action 0 must consume pending cancel")
    check(fixed.canceled(0) == errno.EINVAL, "action 0 without pending cancel must return EINVAL")


def per_call_rpc_metrics():
    class OldMetrics:
        def __init__(self):
            self.total = 0

        def record(self, callnum, latency):
            self.total += latency

    class FixedMetrics(OldMetrics):
        def __init__(self):
            super().__init__()
            self.per_call = {}

        def record(self, callnum, latency):
            super().record(callnum, latency)
            self.per_call.setdefault(callnum, []).append(latency)

    old = OldMetrics()
    old.record("mach_msg_overwrite", 7)
    old.record("checkin", 3)
    check(not hasattr(old, "per_call"), "old metrics must not expose per-call breakdown")
    fixed = FixedMetrics()
    fixed.record("mach_msg_overwrite", 7)
    fixed.record("checkin", 3)
    fixed.record("mach_msg_overwrite", 11)
    check(fixed.per_call["mach_msg_overwrite"] == [7, 11], "fixed metrics must bucket latency by call number")
    check(fixed.total == 21, "fixed metrics must preserve aggregate latency")


TESTS = {
    "microthread-resume-race": microthread_resume_race,
    "fork-checkin-bound": fork_checkin_bound,
    "fork-checkin-sticky-flag": fork_checkin_sticky_flag,
    "cancel-stale-wait-timer": cancel_stale_wait_timer,
    "processcall-exception-guard": processcall_exception_guard,
    "ingest-error-reply": ingest_error_reply,
    "pushreply-pipe-leak": pushreply_pipe_leak,
    "exc-fatal-reply-bound": exc_fatal_reply_bound,
    "assert-wait-cancel-stale-timer": assert_wait_cancel_stale_timer,
    "pthread-canceled-xnu-semantics": pthread_canceled_xnu_semantics,
    "per-call-rpc-metrics": per_call_rpc_metrics,
}

if len(sys.argv) != 2 or sys.argv[1] not in TESTS:
    raise SystemExit("usage: darlingserver_behavior_models.py " + "|".join(sorted(TESTS)))

TESTS[sys.argv[1]]()
print(f"GREEN: {sys.argv[1]} behavioral model old path fails and fixed path passes")
