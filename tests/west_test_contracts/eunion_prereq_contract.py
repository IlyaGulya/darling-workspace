"""E-UNION inputs belong to the installed image; upper state belongs to the run."""

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from runtime_install import InstalledRuntime
from test_prefix import PrefixLifecycleMixin


class Harness(PrefixLifecycleMixin):
    def __init__(self, runtime):
        self.runtime = runtime
        self.launches = 0
        self.stops = 0

    def _installed_runtime(self):
        return self.runtime

    def die(self, message):
        raise ValueError(message)

    def _shutdown_runtime_prefix(self, _prefix):
        self.stops += 1
        return True

    def _boot_eunion_runtime_prefix(self, _invocation, _env, _prefix):
        self.launches += 1

    def _verify_eunion_runtime_prefix(self, invocation, env, prefix, _probe_dirs):
        # Only the transport is doubled: context admission, assertions, writes
        # and cleanup exercise the actual prefix fixture implementation.
        assert env["DPREFIX"] == str(prefix)
        assert invocation["name"] == "case"


with tempfile.TemporaryDirectory(prefix="west-eunion-inputs-") as directory:
    root = Path(directory)
    install = root / "runtime"
    system_root = install / "libexec/darling"
    runtime = InstalledRuntime(install, install / "bin/darling", system_root, None)
    prefix = root / "guest"
    guest_root = "/usr/share/darling/tests/eunion/contract"
    lower = system_root / guest_root.lstrip("/") / "lower.txt"
    invocation = {
        "name": "case", "requires_resources": ["darling-eunion-prefix"],
        "eunion_template_files": [{"guest-path": guest_root + "/lower.txt", "contents": "LOWER\n"}],
        "eunion_upper_files": [{"guest-path": guest_root + "/upper.txt", "contents": "UPPER\n"}],
        "eunion_cleanup_dirs": [guest_root],
    }
    host = Harness(runtime)
    environment = runtime.environment(prefix, {})
    try:
        with host._eunion_prefix_context(invocation, environment):
            raise AssertionError("missing installed fixture was accepted")
    except ValueError as error:
        assert "tests-data" in str(error)
    assert host.launches == 0
    assert not prefix.exists(), "failed admission wrote guest state"
    assert not system_root.exists(), "runner manufactured missing image inputs"

    lower.parent.mkdir(parents=True)
    lower.write_text("LOWER\n")
    before = lower.read_bytes()
    with host._eunion_prefix_context(invocation, environment):
        assert lower.read_bytes() == before
        assert (prefix / guest_root.lstrip("/") / "upper.txt").read_text() == "UPPER\n"
    assert lower.read_bytes() == before
    assert not (prefix / guest_root.lstrip("/")).exists()
    assert host.launches == 1

    upper_case = prefix / guest_root.lstrip("/")
    upper_case.mkdir(parents=True)
    foreign = upper_case / "foreign.txt"
    foreign.write_text("KEEP\n")
    try:
        with host._eunion_prefix_context(invocation, environment):
            raise AssertionError("foreign upper case state was accepted")
    except ValueError as error:
        assert "already exists" in str(error)
    assert foreign.read_text() == "KEEP\n"
    assert lower.read_bytes() == before

print("PASS eunion-prereq-contract")
