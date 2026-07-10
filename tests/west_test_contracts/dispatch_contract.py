from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_dispatch import dispatch_fixture_runner


calls = []


def guest_runner(invocation, env):
    calls.append(("guest", invocation["name"], env))
    return 17


def c_runner(invocation, env):
    calls.append(("c", invocation["name"], env))
    return 23


def fallback(invocation, env):
    calls.append(("fallback", invocation["name"], env))
    return 0


env = {"DPREFIX": "/tmp/prefix"}
assert dispatch_fixture_runner(
    {"name": "guest-precedence", "guest_c_fixture": True, "c_fixture": True},
    env,
    runners=(("guest_c_fixture", guest_runner), ("c_fixture", c_runner)),
    fallback=fallback,
) == 17
assert calls == [("guest", "guest-precedence", env)], calls

calls.clear()
assert dispatch_fixture_runner(
    {"name": "ordinary-command"},
    None,
    runners=(("guest_c_fixture", guest_runner), ("c_fixture", c_runner)),
    fallback=fallback,
) == 0
assert calls == [("fallback", "ordinary-command", None)], calls

print("PASS west-test-dispatch-contract")
