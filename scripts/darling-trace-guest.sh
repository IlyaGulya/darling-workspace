#!/usr/bin/env python3
"""Trace the short-lived guest processes of one Darling run and report what they actually map.

WHY THIS EXISTS. A guest process in Darling is not identifiable the way a host process is, and this was
measured rather than assumed:

  * /proc/<pid>/exe      fails with ENOENT -- the image file is gone (unlinked or memfd), so a matcher that
                         resolves exe, including one written for exactly this purpose, cannot see it;
  * /proc/<pid>/cmdline  is EMPTY -- not merely an in-guest path, so the prefix-substring matcher sees nothing;
  * /proc/<pid>/comm     is the one stable name ("mldr", "launchd", "vchroot", "shellspawn");
  * /proc/<pid>/maps     IS readable, and it is the only instrument that answers "which shared library is this
                         process actually executing".

That last question is not academic: probes compiled into BOTH deployed copies of libsystem_kernel did not fire,
while messages from other code paths of the same library do appear in the run log, and no amount of reading the
source can distinguish "the probe never ran" from "another copy of the library ran".

The processes live under two seconds, so a shell loop that forks a reader per sample misses them entirely; this
polls in-process at --interval and keeps a UNION over every sample, so a map seen once is reported.

Usage:
  darling-trace-guest.sh --prefix PATH [--wait SECONDS] [--interval SECONDS] [--comm NAME]... [--env K=V]...
                         [--cmd 'shell command'] [--log PATH]
"""

import argparse
import glob
import os
import subprocess
import sys
import time

DESCRIBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "darling-describe-artifact.sh")


def read(path, binary=False):
    """Read a /proc file, tolerating the races: a process can vanish between the glob and the open, and a
    binary file cannot be opened with `errors=` -- the first version of this function did, and died on the
    very first cmdline it read."""
    try:
        if binary:
            with open(path, "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf8", "replace").strip()
        with open(path, "r", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def sample(pids_of_interest):
    """Return {pid: {"comm":..., "cmdline":..., "maps": set(), "state":..., "exe":...}} for one moment."""
    out = {}
    for d in glob.glob("/proc/[0-9]*"):
        pid = d.rsplit("/", 1)[-1]
        comm = read(d + "/comm")
        if comm is None or comm not in pids_of_interest:
            continue
        maps = set()
        raw = read(d + "/maps")
        if raw:
            for line in raw.splitlines():
                parts = line.split()
                if len(parts) >= 6 and parts[-1].startswith("/"):
                    maps.add(parts[-1])
        stat = read(d + "/stat") or ""
        # field 3 is the state; the exit code is field 52 (index 51) once the process is a zombie
        fields = stat.rsplit(")", 1)[-1].split() if ")" in stat else []
        out[pid] = {
            "comm": comm,
            "cmdline": read(d + "/cmdline", binary=True) or "",
            # WHAT it is blocked in, not merely THAT it is blocked: /proc/<pid>/syscall gives the syscall
            # number, its arguments and the stack pointer for a sleeping process, and for a running one it
            # gives -1. MEASURED need (doc section 183): a guest process that prints nothing, takes no
            # signal and never returns cannot be distinguished from one parked in a bounded wait without
            # this field -- and "bounded waits cannot park forever" was exactly the assumption that did not
            # survive contact with the run.
            "syscall": read(d + "/syscall") or "",
            "exe": os.readlink(d + "/exe") if os.path.exists(d + "/exe") else "<ENOENT>",
            "maps": maps,
            # the RAW lines as well: the ranges are what a pc is resolved against, and a set of paths cannot
            # answer "which file is this address in" (measured: the first version of this resolution silently
            # did nothing because only the paths were kept).
            "maps_raw": raw or "",
            "state": fields[0] if fields else "?",
            "exit_code": fields[50] if len(fields) > 50 else "",
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--wait", type=float, default=45.0)
    ap.add_argument("--interval", type=float, default=0.05)
    ap.add_argument("--comm", action="append", default=None,
                    help="guest process name to watch (default: mldr launchd vchroot shellspawn)")
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--cmd", default="echo HELLO=1")
    ap.add_argument("--log", default=None)
    args = ap.parse_args()

    prefix = os.path.abspath(args.prefix)
    comms = set(args.comm or ["mldr", "launchd", "vchroot", "shellspawn"])
    log = args.log or "/tmp/darling-trace-%d.log" % int(time.time())

    env = dict(os.environ,
               DPREFIX=prefix, DARLING_PREFIX=prefix,
               DARLING_ROOTLESS="1", DARLING_NOOVERLAYFS="1", DARLING_EUNION="1")
    for kv in args.env:
        if "=" not in kv:
            print("--env wants KEY=VALUE, got %r" % kv, file=sys.stderr)
            return 2
        k, v = kv.split("=", 1)
        env[k] = v

    launcher = os.path.join(prefix, "bin", "darling")
    if not os.path.exists(launcher):
        print("no launcher at %s" % launcher, file=sys.stderr)
        return 2

    before = set(glob.glob("/proc/[0-9]*"))
    f = open(log, "w")
    proc = subprocess.Popen([launcher, "--rootless", "shell", "/bin/sh", "-c", args.cmd],
                            env=env, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)

    # union over every sample, so a map that exists for one poll is still reported
    seen = {}
    order = []
    deadline = time.time() + args.wait
    while time.time() < deadline:
        for pid, info in sample(comms).items():
            # A process that already existed before the launch belongs to another run: reporting it as part
            # of this one attributes an artifact from a different prefix to this measurement. MEASURED: a
            # stale shellspawn of another prefix appeared in the report and read as "this run loads its
            # libraries from somewhere else".
            if ("/proc/" + pid) in before:
                continue
            if pid not in seen:
                order.append(pid)
                seen[pid] = dict(info, maps=set(info["maps"]), samples=0, states=[], cmds=set(), syscalls={})
            rec = seen[pid]
            rec["maps"] |= info["maps"]
            rec["samples"] += 1
            rec["states"].append(info["state"])
            if info["cmdline"]:
                rec["cmds"].add(info["cmdline"])
            # Resolve the parked pc against THIS sample's maps: an address is meaningless across runs (ASLR) and
            # "the wait is in libc" versus "the wait is in the loader" is the whole remaining question, so the
            # answer has to be produced where the maps are still readable.
            if info.get("syscall"):
                parts = info["syscall"].split(" ")
                if len(parts) >= 9:
                    try:
                        pc = int(parts[-1], 16)  # the LAST field is the pc; parts[-2] is the sp (stack, anonymous)
                    except ValueError:
                        pc = None
                    # READ THE ARGUMENTS THE KERNEL WAS HANDED, not just their addresses. A FUTEX_WAIT with a
                    # one-millisecond relative timeout that does not return cannot be diagnosed from the call
                    # itself -- the timeout POINTER is what the kernel reads, and /proc/<pid>/mem holds the
                    # bytes. MEASURED: this is the only field that separates "the kernel was asked for one
                    # millisecond and did not honour it" from "the caller passed something else".
                    if len(parts) >= 5:
                        try:
                            tmo = int(parts[4], 16)
                        except ValueError:
                            tmo = 0
                        if tmo > 0x10000:
                            try:
                                with open(os.path.join("/proc", str(pid), "mem"), "rb", 0) as mf:
                                    mf.seek(tmo)
                                    raw16 = mf.read(16)
                                if len(raw16) == 16:
                                    sec, nsec = int.from_bytes(raw16[:8], "little", signed=True), int.from_bytes(raw16[8:], "little", signed=True)
                                    rec.setdefault("timeouts", {})
                                    key = "sec=%d nsec=%d" % (sec, nsec)
                                    rec["timeouts"][key] = rec["timeouts"].get(key, 0) + 1
                            except (OSError, ValueError):
                                pass
                    if pc is not None:
                        for line_ in (info.get("maps_raw") or "").splitlines():
                            f = line_.split()
                            if len(f) >= 6 and f[1] not in ("r--p", "---p"):
                                try:
                                    lo, hi = (int(x, 16) for x in f[0].split("-"))
                                except ValueError:
                                    continue
                                if lo <= pc < hi:
                                    fo = int(f[2], 16)
                                    rec.setdefault("pc_where", {})
                                    key = "%s+0x%x" % (f[5], fo + (pc - lo))
                                    rec["pc_where"][key] = rec["pc_where"].get(key, 0) + 1
                                    break
                # keep the union, and count repeats: "the same syscall in every sample" is what says parked.
                # The FULL line is kept too, because the number alone says which syscall and not what it was
                # asked to do -- and for futex the arguments (op, compare value, timespec pointer, stack
                # pointer) are the whole question. MEASURED: a FUTEX_WAIT with a one-millisecond relative
                # timeout that does not return cannot be diagnosed from the syscall NUMBER.
                sc = info["syscall"].split(" ")[0]
                rec["syscalls"][sc] = rec["syscalls"].get(sc, 0) + 1
                rec.setdefault("syscall_full", {})
                rec["syscall_full"][info["syscall"]] = rec["syscall_full"].get(info["syscall"], 0) + 1
            if info["exit_code"] != "":
                rec["exit_code"] = info["exit_code"]
            rec["exe"] = info["exe"] if rec.get("exe", "<ENOENT>") == "<ENOENT>" else rec["exe"]
        time.sleep(args.interval)

    print("run log: %s" % log)
    print("guest processes observed: %d" % len(order))
    for pid in order:
        rec = seen[pid]
        print("\npid=%s comm=%s samples=%d states=%s exit_code=%s"
              % (pid, rec["comm"], rec["samples"], "".join(sorted(set(rec["states"]))),
                 rec.get("exit_code", "")))
        print("  exe:     %s" % rec.get("exe", "<ENOENT>"))
        if rec.get("syscalls"):
            tops = sorted(rec["syscalls"].items(), key=lambda kv: -kv[1])[:4]
            print("  syscalls: %s" % ", ".join("%s x%d" % (k, v) for k, v in tops))
        if rec.get("timeouts"):
            for k, v in sorted(rec["timeouts"].items(), key=lambda kv: -kv[1])[:3]:
                print("  timeout-arg: %s  (x%d)" % (k, v))
        if rec.get("pc_where"):
            for k, v in sorted(rec["pc_where"].items(), key=lambda kv: -kv[1])[:3]:
                print("  pc-where: %s  (x%d)" % (k[:110], v))
        if rec.get("syscall_full"):
            for line, n in sorted(rec["syscall_full"].items(), key=lambda kv: -kv[1])[:2]:
                print("  parked:   %s  (x%d)" % (line[:120], n))
        if rec["cmds"]:
            for c in sorted(rec["cmds"]):
                print("  cmdline: %s" % c[:100])
        else:
            print("  cmdline: (empty -- measured for guest processes)")
        # Include ANY path under a Darling prefix, not just ones whose name contains "darling": the exec'd
        # image itself (`<prefix>/sbin/launchd`) does not, and excluding it hid the answer to "which binary is
        # this process actually running" -- the question this tool exists for.
        libs = sorted(p for p in rec["maps"]
                      if "libsystem" in p or p.endswith("/dyld") or "mldr" in p or "/darling" in p
                      or "/sbin/" in p or "/usr/libexec/" in p or p.startswith("/tmp/dr-"))
        others = sorted(set(rec["maps"]) - set(libs))
        if libs:
            print("  the files it actually maps:")
            for p in libs:
                exists = os.path.exists(p)
                mark = "" if exists else "   <-- NOT ON DISK"
                print("    %s%s" % (p, mark))
                if exists and os.path.exists(DESCRIBE):
                    # a mapped library that is not the one deployed is the whole point of printing this
                    r = subprocess.run(["sh", DESCRIBE, p], capture_output=True, text=True)
                    for line in r.stdout.splitlines():
                        if line.startswith("sha256:"):
                            print("      %s" % line.strip())
        elif rec["maps"]:
            print("  %d mapped file(s), none of them a Darling library:" % len(rec["maps"]))
            for p in others[:8]:
                print("    %s" % p)
        else:
            print("  no path-backed mappings observed (sampled before exec, or all anonymous)")
    if proc.poll() is None:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
