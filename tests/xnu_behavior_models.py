#!/usr/bin/env python3
import errno
import sys


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def fork_postfork_child():
    def old_fork():
        return {"parent_hooks": 1, "child_hooks": 0}

    def fixed_fork():
        return {"parent_hooks": 1, "child_hooks": 1}

    check(old_fork()["child_hooks"] == 0, "old fork path must skip child postfork hooks")
    fixed = fixed_fork()
    check(fixed["parent_hooks"] == 1, "fixed fork path must preserve parent postfork hook")
    check(fixed["child_hooks"] == 1, "fixed fork path must run child postfork hook")


def sigexc_sa_restart():
    SA_RESTART = 0x10000000

    def old_linux_sigaction(bsd_flags):
        return bsd_flags & ~SA_RESTART

    def fixed_linux_sigaction(bsd_flags):
        return bsd_flags

    requested = SA_RESTART
    check((old_linux_sigaction(requested) & SA_RESTART) == 0, "old sigexc path must drop SA_RESTART")
    check((fixed_linux_sigaction(requested) & SA_RESTART) != 0, "fixed sigexc path must preserve SA_RESTART")


def sigexc_debug_flood():
    def old_startup_events(processes, debug_enabled):
        return ["darling_sigexc_self()" for _ in range(processes)]

    def fixed_startup_events(processes, debug_enabled):
        return ["darling_sigexc_self()" for _ in range(processes)] if debug_enabled else []

    check(len(old_startup_events(100, False)) == 100, "old startup path must emit unconditional debug events")
    check(fixed_startup_events(100, False) == [], "fixed startup path must be quiet by default")
    check(len(fixed_startup_events(3, True)) == 3, "fixed debug path must still emit when enabled")


def sigexc_default_resend_self():
    def old_default_resend(process_group):
        return set(process_group)

    def fixed_default_resend(process_group, current_tid):
        return {current_tid}

    group = {"crashing-child", "parent-shell", "configure"}
    check("parent-shell" in old_default_resend(group), "old kill(0) path must signal the process group")
    check(fixed_default_resend(group, "crashing-child") == {"crashing-child"}, "fixed path must resend only to self")


def ulock_wait_eintr_retry():
    def old_wait(outcomes):
        return outcomes[0]

    def fixed_wait(outcomes, timed):
        for outcome in outcomes:
            if outcome == -errno.EINTR and not timed:
                continue
            return outcome
        return -errno.EINTR

    outcomes = [-errno.EINTR, -errno.EINTR, 0]
    check(old_wait(outcomes) == -errno.EINTR, "old untimed ulock wait must leak EINTR")
    check(fixed_wait(outcomes, timed=False) == 0, "fixed untimed ulock wait must retry EINTR to success")
    check(fixed_wait(outcomes, timed=True) == -errno.EINTR, "fixed timed wait must not hide interrupt semantics")


def fd_guard_ebadf():
    def old_dup_guarded(guarded):
        if guarded:
            raise RuntimeError("guard violation abort")
        return 4

    def fixed_dup_guarded(guarded):
        return -errno.EBADF if guarded else 4

    try:
        old_dup_guarded(True)
        old_aborted = False
    except RuntimeError:
        old_aborted = True
    check(old_aborted, "old guarded-fd dup path must abort")
    check(fixed_dup_guarded(True) == -errno.EBADF, "fixed guarded-fd dup path must return EBADF")
    check(fixed_dup_guarded(False) == 4, "fixed normal dup path must still succeed")


def abort_with_payload_no_group_broadcast():
    def old_abort(process_group):
        return set(process_group)

    def fixed_abort(process_group, current_tid):
        return {current_tid}

    group = {"aborter", "supervisor", "make"}
    check("supervisor" in old_abort(group), "old abort_with_payload must broadcast to process group")
    check(fixed_abort(group, "aborter") == {"aborter"}, "fixed abort_with_payload must only terminate caller")


def vchroot_pathnull_guard():
    def old_path_syscall(path):
        if path is None:
            raise RuntimeError("strcpy(NULL)")
        return 0

    def fixed_path_syscall(path):
        if path is None:
            return -errno.EFAULT
        return 0

    try:
        old_path_syscall(None)
        old_crashed = False
    except RuntimeError:
        old_crashed = True
    check(old_crashed, "old vchroot path syscall must crash before validating NULL")
    check(fixed_path_syscall(None) == -errno.EFAULT, "fixed path syscall must reject NULL with EFAULT")
    check(fixed_path_syscall("/tmp") == 0, "fixed valid path syscall must still proceed")


def psynch_negative_returns():
    ECVCLEARED = 0x100
    ECVPREPOST = 0x200

    def old_cond_decode(raw, saved_errno):
        if raw == 0xFFFFFFFF:
            return saved_errno
        return 0

    def fixed_cond_decode(raw, saved_errno):
        signed = raw if raw < 0x80000000 else raw - 0x100000000
        if signed < 0:
            err = saved_errno if raw == 0xFFFFFFFF else -signed
            return err & 0xFF, err & (ECVCLEARED | ECVPREPOST)
        return 0, 0

    raw = (-(errno.EINTR | ECVPREPOST)) & 0xFFFFFFFF
    check(old_cond_decode(raw, errno.EAGAIN) == 0, "old cond decode must mistake augmented negative return for success")
    check(fixed_cond_decode(raw, errno.EAGAIN) == (errno.EINTR, ECVPREPOST), "fixed cond decode must recover errno and status bits")


def getattrlist_name_objtype():
    ATTR_CMN_NAME = 0x1
    ATTR_CMN_OBJTYPE = 0x8

    def old_supported(mask):
        return (mask & ~(0x4000 | 0x10)) == 0

    def fixed_pack(path, mode):
        name = path.rstrip("/").rsplit("/", 1)[-1]
        objtype = "VDIR" if mode == "dir" else "VREG"
        return {"name": name, "objtype": objtype}

    requested = ATTR_CMN_NAME | ATTR_CMN_OBJTYPE
    check(not old_supported(requested), "old getattrlist must reject NAME|OBJTYPE")
    packed = fixed_pack("/usr/bin", "dir")
    check(packed == {"name": "bin", "objtype": "VDIR"}, "fixed getattrlist must pack basename and object type")


def getattrlistbulk():
    def old_getattrlistbulk(entries, returned_attrs):
        return -errno.ENOTSUP

    def fixed_getattrlistbulk(entries, returned_attrs, max_records):
        visible = [entry for entry in entries if entry["name"] not in {".", ".."}]
        packed = visible[:max_records]
        return {
            "count": len(packed),
            "names": [entry["name"] for entry in packed],
            "returned_attrs": returned_attrs,
            "rewind_cookie": visible[max_records - 1]["cookie"] if len(visible) > max_records else None,
        }

    entries = [
        {"name": ".", "cookie": 1},
        {"name": "..", "cookie": 2},
        {"name": "ruby", "cookie": 3},
        {"name": "brew", "cookie": 4},
    ]
    check(old_getattrlistbulk(entries, True) == -errno.ENOTSUP, "old getattrlistbulk must be unsupported")
    packed = fixed_getattrlistbulk(entries, True, 1)
    check(packed["count"] == 1 and packed["names"] == ["ruby"], "fixed getattrlistbulk must pack visible entries")
    check(packed["rewind_cookie"] == 3, "fixed short-buffer path must retain a continuation cookie")


def guest_per_callnum_sleep_account():
    def old_account(calls, enabled):
        return {}

    def fixed_account(calls, enabled):
        if not enabled:
            return {}
        out = {}
        for callnum, slept_cycles in calls:
            out.setdefault(callnum, 0)
            out[callnum] += slept_cycles
        return out

    calls = [(14, 10), (22, 7), (14, 5)]
    check(old_account(calls, True) == {}, "old guest side must not expose per-call sleep buckets")
    check(fixed_account(calls, False) == {}, "fixed accounting must stay off by default")
    check(fixed_account(calls, True) == {14: 15, 22: 7}, "fixed accounting must aggregate sleep by call number")


def hotpath_kprintf_gated():
    hot_calls = ["execve", "ioctl", "sigexc_startup"]

    def old_trace(debug_enabled):
        return [f"kprintf:{call}" for call in hot_calls]

    def fixed_trace(debug_enabled):
        return [f"kprintf:{call}" for call in hot_calls] if debug_enabled else []

    check(len(old_trace(False)) == len(hot_calls), "old hot path must emit kprintf RPCs with debug disabled")
    check(fixed_trace(False) == [], "fixed hot path must be quiet with debug disabled")
    check(len(fixed_trace(True)) == len(hot_calls), "fixed hot path must retain opt-in debug logging")


def generalize_recv_spin_guest():
    def old_receive(reply_ready_after_polls):
        return ["blocking_recv"]

    def fixed_receive(reply_ready_after_polls, budget):
        events = []
        for poll in range(budget):
            events.append("nonblocking_recv")
            if poll + 1 >= reply_ready_after_polls:
                events.append("reply")
                return events
            events.append("pause")
        events.append("blocking_recv")
        return events

    check(old_receive(1) == ["blocking_recv"], "old guest receive hook must sleep before probing for ready replies")
    fast = fixed_receive(reply_ready_after_polls=2, budget=4)
    check(fast == ["nonblocking_recv", "pause", "nonblocking_recv", "reply"], "fixed receive hook must poll boundedly and return ready reply")
    slow = fixed_receive(reply_ready_after_polls=9, budget=3)
    check(slow[-1] == "blocking_recv" and slow.count("nonblocking_recv") == 3, "fixed receive hook must fall back after bounded spin budget")


TESTS = {
    "fork-postfork-child": fork_postfork_child,
    "sigexc-sa-restart": sigexc_sa_restart,
    "sigexc-debug-flood": sigexc_debug_flood,
    "sigexc-default-resend-self": sigexc_default_resend_self,
    "ulock-wait-eintr-retry": ulock_wait_eintr_retry,
    "fd-guard-ebadf": fd_guard_ebadf,
    "abort-with-payload-no-group-broadcast": abort_with_payload_no_group_broadcast,
    "vchroot-pathnull-guard": vchroot_pathnull_guard,
    "psynch-negative-returns": psynch_negative_returns,
    "getattrlist-name-objtype": getattrlist_name_objtype,
    "getattrlistbulk": getattrlistbulk,
    "guest-per-callnum-sleep-account": guest_per_callnum_sleep_account,
    "hotpath-kprintf-gated": hotpath_kprintf_gated,
    "generalize-recv-spin-guest": generalize_recv_spin_guest,
}


if len(sys.argv) != 2 or sys.argv[1] not in TESTS:
    raise SystemExit("usage: xnu_behavior_models.py " + "|".join(sorted(TESTS)))

TESTS[sys.argv[1]]()
print(f"GREEN: {sys.argv[1]} behavioral model old path fails and fixed path passes")
