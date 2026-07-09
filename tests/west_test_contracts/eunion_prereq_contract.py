import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from prefix_repair import eunion_prefix_prerequisite_problems


with tempfile.TemporaryDirectory() as tmp:
    prefix = Path(tmp)
    (prefix / "libexec/darling").mkdir(parents=True)
    kernel = prefix / "usr/lib/system/libsystem_kernel.dylib"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"plain kernel")
    assert any(
        "lacks E-UNION markers" in problem
        for problem in eunion_prefix_prerequisite_problems(prefix)
    )
    kernel.write_bytes(b"/.union-work user.union.whiteout user.union.opaque")
    assert eunion_prefix_prerequisite_problems(prefix) == []
