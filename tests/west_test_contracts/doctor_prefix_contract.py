"""Doctor distinguishes first-boot preparation from runtime postconditions."""
import tempfile
import sys
import types
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)
from west_commands.doctor import DarlingDoctor


def check(prefix):
    doctor = DarlingDoctor.__new__(DarlingDoctor)
    doctor.fail = 0
    doctor.inf = lambda message: None
    doctor.wrn = lambda message: None
    doctor.err = lambda message: None
    doctor._check_prefix_boot_prereqs(Namespace(prefix=str(prefix), extra_prefix=[]))
    return doctor.fail


with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    assert check(prefix) == 1, "untyped empty prefix must not pass"
    identity = prefix.stat()
    state = prefix / ".darling-prefix-state-v2"
    state.write_text(
        "DARLING_PREFIX_STATE_V2\nschema_version=2\nruntime_mode=rootless-eunion\n"
        f"generation=1\nprefix_device={identity.st_dev}\nprefix_inode={identity.st_ino}\n"
        f"owner_uid={identity.st_uid}\nowner_gid={identity.st_gid}\nprovenance=created\n"
    )
    assert check(prefix) == 0, "prepared prefix must reach its first launchd boot"
    assert not (prefix / "private").exists(), "doctor must not create guest boot state"

    init_pid = prefix / ".init.pid"
    init_pid.write_text("12345\n")
    assert check(prefix) == 1, "published runtime must satisfy directory postconditions"
    init_pid.unlink()

    (prefix / "private").write_text("not a directory")
    assert check(prefix) == 1, "first boot must not hide obstructed directory creation"
    (prefix / "private").unlink()

    (prefix / "private/tmp").mkdir(parents=True)
    (prefix / "private/tmp").chmod(0o755)
    assert check(prefix) == 1, "first boot must not bless incorrect existing permissions"
    (prefix / "private/tmp").chmod(0o1777)

    state.write_text(state.read_text().replace(f"prefix_inode={identity.st_ino}", "prefix_inode=0"))
    assert check(prefix) == 1, "a copied or stale prefix identity is not prepared"
    state.unlink()
    for rel in ("private/var/tmp", "tmp", "var/tmp"):
        (prefix / rel).mkdir(parents=True)
        (prefix / rel).chmod(0o1777)
    for rel in ("private/var/db/launchd.db/com.apple.launchd", "var/run"):
        (prefix / rel).mkdir(parents=True)
    assert check(prefix) == 0, "complete legacy prefix must remain accepted"

print("PASS west-doctor-prefix-contract")
