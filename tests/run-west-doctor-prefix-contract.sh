#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

python3 - <<'PY'
import tempfile
import sys
import types
from argparse import Namespace
from pathlib import Path

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.doctor import DarlingDoctor


def make_doctor():
    doctor = DarlingDoctor.__new__(DarlingDoctor)
    doctor.fail = 0
    doctor.messages = []
    doctor.inf = lambda message: doctor.messages.append(("inf", message))
    doctor.wrn = lambda message: doctor.messages.append(("wrn", message))
    doctor.err = lambda message: doctor.messages.append(("err", message))
    return doctor


with tempfile.TemporaryDirectory() as temp:
    prefix = Path(temp)
    args = Namespace(prefix=str(prefix), extra_prefix=[])

    doctor = make_doctor()
    doctor._check_prefix_boot_prereqs(args)
    assert doctor.fail == 1
    assert any("private/var/tmp missing" in text for _, text in doctor.messages), doctor.messages
    assert any("libexec/darling/private/var/tmp missing" in text for _, text in doctor.messages), doctor.messages
    assert any("var/run missing" in text for _, text in doctor.messages), doctor.messages

    (prefix / "private/var/tmp").mkdir(parents=True)
    (prefix / "libexec/darling/private/var/tmp").mkdir(parents=True)
    (prefix / "private/var/tmp").chmod(0o1777)
    (prefix / "libexec/darling/private/var/tmp").chmod(0o1777)
    (prefix / "var/run").mkdir(parents=True)
    (prefix / "var/tmp").mkdir(parents=True)

    doctor = make_doctor()
    doctor._check_prefix_boot_prereqs(args)
    assert doctor.fail == 0
    assert any("private/var/tmp exists with mode 1777" in text for _, text in doctor.messages), doctor.messages
    assert any("libexec/darling/private/var/tmp exists with mode 1777" in text for _, text in doctor.messages), doctor.messages

print("PASS west-doctor-prefix-contract")
PY
