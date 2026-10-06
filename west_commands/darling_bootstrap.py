"""`west darling-bootstrap` — fill a rootless Darling prefix from an explicit build dir.

The West-native path. The source state is whatever West has checked out (the manifest's pins), the build
variant is whatever ``--build-dir`` was configured with, and this command only turns that build into a
working prefix. It looks up no runtime profile, no source-profile, no patch, no lock and no materialized
forest, and the plan it reads cannot name a revision: a plan that carries a source-selection key is refused
rather than resolved.

In order:

  1. assert the build dir is configured and carries the plan's explicit cmake defines (a build dir for a
     different variant is refused, with the configure line that would produce this one);
  2. create the prefix directory if it is absent;
  3. ``west darling-doctor --scope workspace`` — doctor before build/deploy;
  4. build the plan's targets in the build dir;
  5. deploy through ``RuntimeDeploymentService.deployed()`` — the same deploy half the profile-coupled
     bootstrap runs: typed mode marker, CMake component manifests, Mach-O closure resolution, one
     transaction and the deployment receipt — and, inside that deployment, run one guest smoke. The
     deployment and its receipt are retained only if the smoke passes; anything else rolls back;
  6. ``west darling-doctor --scope runtime`` after the guest smoke, plus an explicit receipt check;
  7. report the parity facts: the prefix, the plan, the receipt and whether the deployed bytes still match
     the build they were deployed from.
"""

from __future__ import annotations

import json
import os
import re
import resource
import subprocess
from pathlib import Path
from typing import Any

import yaml
from west.commands import WestCommand

try:
    from .deploy_receipt import read_receipt, receipt_path, verify_receipt
    from .prefix_repair import cleanup_prefix_mounts, darling_init_pid_is_usable
    from .test_execution import process_output_text
    from .test_guest_execution import run_guest_shell
    from .test_prefix import PrefixLifecycleMixin, PrefixLifecycleOwner
    from .test_runtime import RuntimePlanMixin
    from .test_runtime_deploy import RuntimeDeploymentService
except ImportError:  # west loads command files as top-level modules
    from deploy_receipt import read_receipt, receipt_path, verify_receipt
    from prefix_repair import cleanup_prefix_mounts, darling_init_pid_is_usable
    from test_execution import process_output_text
    from test_guest_execution import run_guest_shell
    from test_prefix import PrefixLifecycleMixin, PrefixLifecycleOwner
    from test_runtime import RuntimePlanMixin
    from test_runtime_deploy import RuntimeDeploymentService


DEFAULT_PLAN_PATH = Path(__file__).resolve().parent.parent / "testkit" / "darling-bootstrap.yml"
SMOKE_MARKER = "WEST_PREFIX_BOOTSTRAP_OK"
DEFAULT_SMOKE_TIMEOUT_SECONDS = 300

# A plan describes a deploy, never a source selection. These are the keys the profile/lock/materializer
# machinery owns; a bootstrap plan that carried one would smuggle back the indirection this path removes.
_SOURCE_SELECTION_KEYS = (
    "source-mode",
    "source-profile",
    "source-module",
    "source-modules",
    "patch",
    "patches",
    "patchset",
    "lock",
    "locks",
    "profile",
    "profiles",
    "revision",
    "revisions",
    "materialize",
    "materialized",
)
_PLAN_KEYS = {
    "schema",
    "build-targets",
    "cmake-defines",
    "runtime-mode",
    "launcher-env",
    "smoke-timeout-seconds",
    "smoke-script",
    "runtime-artifacts",
}
_DEFINE_TRUE = {"ON", "TRUE", "1", "YES", "Y"}
_DEFINE_FALSE = {"OFF", "FALSE", "0", "NO", "N", ""}
_CMAKE_CACHE_ENTRY = re.compile(r"^([A-Za-z0-9_]+):[A-Za-z0-9_]+=(.*)$")
_APPARMOR_USERNS_SETTING = "kernel.apparmor_restrict_unprivileged_userns"
_APPARMOR_USERNS_SYSCTL = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


def unprivileged_userns_problem(probe=None, sysctl_path: Path = _APPARMOR_USERNS_SYSCTL) -> str | None:
    """Return why this host cannot create an unprivileged user namespace, or None.

    Darling's rootless runtime creates mount and PID namespaces, so a host that refuses unprivileged user
    namespaces cannot boot ANY prefix. That is worth naming up front rather than discovering it as a
    shellspawn readiness timeout: measured, one such host turned every bootstrap -- including a prefix that
    had booted earlier the same day -- into `Rootless shellspawn did not become ready`, with the reason
    (an AppArmor denial of `userns_create`) visible only in the kernel log.
    """

    if probe is None:
        def probe() -> tuple[int, str]:
            try:
                result = subprocess.run(
                    ["unshare", "--user", "--map-root-user", "true"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                return 1, str(error)
            return result.returncode, (result.stderr or result.stdout).strip()

    code, detail = probe()
    if code == 0:
        return None
    setting = ""
    try:
        value = sysctl_path.read_text().strip()
        if value and value != "0":
            setting = f" ({_APPARMOR_USERNS_SETTING}={value})"
    except OSError:
        pass
    reason = detail.splitlines()[-1] if detail else "unshare --user failed"
    return (
        f"unprivileged user namespaces are unavailable{setting}: {reason}. "
        "Darling's rootless runtime needs them to create mount and PID namespaces; "
        "relax the restriction (for example: sysctl -w kernel.apparmor_restrict_unprivileged_userns=0) "
        "and verify with `unshare --user --map-root-user true`."
    )


def inherited_nofile_limits(getrlimit=None) -> tuple[int | None, int | None]:
    """The NOFILE soft/hard limit the bootstrap passes on to the runtime it starts.

    Recorded because every boot-based NOFILE measurement depends on it: the server inherits this, keeps or
    raises it, and restores it for its children, so a run whose receipt does not name it cannot be compared
    with another run. A host that cannot report the limit is not an error -- the pair is then absent.
    """

    if getrlimit is None:
        def getrlimit(resource_id):
            return resource.getrlimit(resource_id)

    try:
        soft, hard = getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return None, None
    return soft, hard


class BootstrapPlan:
    """One explicit deploy plan: a build variant, a closure and a smoke.

    A plain class on purpose: west loads this file without registering it in sys.modules, and
    dataclasses.dataclass resolves the class's own module namespace there, so a decorated class in a
    command module fails at import time.
    """

    def __init__(
        self,
        *,
        path: Path,
        build_targets: list[str],
        cmake_defines: dict[str, Any],
        runtime_mode: str | None,
        launcher_env: dict[str, str],
        smoke_timeout_seconds: int | None,
        smoke_script: str,
        runtime_artifacts: list[dict[str, Any]],
    ):
        self.path = path
        self.build_targets = build_targets
        self.cmake_defines = cmake_defines
        self.runtime_mode = runtime_mode
        self.launcher_env = launcher_env
        self.smoke_timeout_seconds = smoke_timeout_seconds
        self.smoke_script = smoke_script
        self.runtime_artifacts = runtime_artifacts

    def all_build_targets(self) -> list[str]:
        targets = list(self.build_targets)
        for artifact in self.runtime_artifacts:
            for target in artifact.get("build-targets", []):
                if target not in targets:
                    targets.append(target)
        return targets

    def proof(self) -> dict[str, Any]:
        """The deploy-half proof: exactly the fields RuntimeDeploymentService reads."""

        proof: dict[str, Any] = {"runtime-artifacts": self.runtime_artifacts}
        if self.runtime_mode is not None:
            proof["runtime-mode"] = self.runtime_mode
        if self.launcher_env:
            proof["launcher-env"] = dict(self.launcher_env)
        return proof

    def cmake_configure_line(self, topdir: Path, prefix: Path) -> str:
        defines = " ".join(
            f"-D{name}={'ON' if value is True else 'OFF' if value is False else value}"
            for name, value in self.cmake_defines.items()
        )
        return (
            f"cmake -S {topdir / 'darling'} -B <build-dir> -G Ninja "
            f"-DCMAKE_INSTALL_PREFIX={prefix} {defines}"
        ).strip()


def load_bootstrap_plan(path: Path) -> BootstrapPlan:
    """Read and validate one explicit plan, refusing anything profile-shaped."""

    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as error:
        raise ValueError(f"cannot read bootstrap plan {path}: {error}") from error
    except yaml.YAMLError as error:
        raise ValueError(f"invalid bootstrap plan {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"bootstrap plan {path} must be a mapping")
    smuggled = sorted(key for key in _SOURCE_SELECTION_KEYS if key in raw)
    if smuggled:
        raise ValueError(
            f"bootstrap plan {path} names source selection ({', '.join(smuggled)}); "
            "the source state comes only from the West manifest"
        )
    unknown = sorted(set(raw) - _PLAN_KEYS)
    if unknown:
        raise ValueError(f"bootstrap plan {path} has unknown keys: {', '.join(unknown)}")
    if raw.get("schema") != 1:
        raise ValueError(f"bootstrap plan {path} must declare schema: 1")
    targets = raw.get("build-targets")
    if not isinstance(targets, list) or not all(
        isinstance(target, str) and target for target in targets
    ):
        raise ValueError(f"bootstrap plan {path} needs a build-targets list of names")
    defines = raw.get("cmake-defines", {})
    if not isinstance(defines, dict):
        raise ValueError(f"bootstrap plan {path} cmake-defines must be a mapping")
    launcher_env = raw.get("launcher-env", {})
    if not isinstance(launcher_env, dict):
        raise ValueError(f"bootstrap plan {path} launcher-env must be a mapping")
    artifacts = raw.get("runtime-artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError(f"bootstrap plan {path} needs a non-empty runtime-artifacts list")
    for index, artifact in enumerate(artifacts):
        where = f"runtime-artifacts[{index}]"
        if not isinstance(artifact, dict):
            raise ValueError(f"bootstrap plan {path} {where} must be a mapping")
        module = artifact.get("module")
        if not isinstance(module, str) or not module:
            raise ValueError(f"bootstrap plan {path} {where} needs a module")
        if "resource" not in artifact and "deploy" not in artifact:
            raise ValueError(
                f"bootstrap plan {path} {where} needs a resource or a deploy path"
            )
        deploy = artifact.get("deploy")
        if deploy is not None and (
            not isinstance(deploy, list)
            or not all(isinstance(entry, str) and entry for entry in deploy)
        ):
            raise ValueError(
                f"bootstrap plan {path} {where}.deploy must be a list of guest paths"
            )
        artifact_targets = artifact.get("build-targets", [])
        if not isinstance(artifact_targets, list) or not all(
            isinstance(target, str) and target for target in artifact_targets
        ):
            raise ValueError(
                f"bootstrap plan {path} {where}.build-targets must be a list of names"
            )
    timeout = raw.get("smoke-timeout-seconds")
    if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
        raise ValueError(
            f"bootstrap plan {path} smoke-timeout-seconds must be a positive integer"
        )
    script = raw.get("smoke-script")
    if script is not None and not isinstance(script, str):
        raise ValueError(f"bootstrap plan {path} smoke-script must be a string")
    runtime_mode = raw.get("runtime-mode")
    if runtime_mode is not None and not isinstance(runtime_mode, str):
        raise ValueError(f"bootstrap plan {path} runtime-mode must be a string")
    plan = BootstrapPlan(
        path=path,
        build_targets=[str(target) for target in targets],
        cmake_defines=dict(defines),
        runtime_mode=runtime_mode,
        launcher_env={str(key): str(value) for key, value in launcher_env.items()},
        smoke_timeout_seconds=timeout,
        smoke_script=(
            script
            if script is not None
            else f"set -eu\nprintf '%s\\n' {SMOKE_MARKER}\n"
        ),
        runtime_artifacts=artifacts,
    )
    if not plan.all_build_targets():
        raise ValueError(
            f"bootstrap plan {path} names no build target: build-targets and every "
            "artifact's build-targets are empty"
        )
    return plan


def build_install_prefix(cache: Path) -> str | None:
    """The CMAKE_INSTALL_PREFIX the build dir was configured with.

    It is not a variant knob: the launcher bakes it as INSTALL_PREFIX and finds the server at
    ``<it>/bin/darlingserver``, so a build dir configured for another install root cannot boot the prefix it
    is deployed into. Measured: a build dir left at the default /usr/local produced a launcher that exec'd
    /usr/local/bin/darlingserver, got ENOENT, printed "Failed to start darlingserver" and left the launcher
    polling shellspawn.sock for its whole timeout -- with no server, no launchd and no shellspawn ever started.
    """

    try:
        text = cache.read_text(errors="replace")
    except OSError as error:
        raise ValueError(f"cannot read {cache}: {error}") from error
    for line in text.splitlines():
        if line.startswith("CMAKE_INSTALL_PREFIX:"):
            return line.split("=", 1)[1].strip() if "=" in line else None
    return None


def compare_cmake_defines(cache: Path, expected: dict[str, Any]) -> list[str]:
    """Return one line per plan define the configured build dir does not satisfy."""

    try:
        text = cache.read_text(errors="replace")
    except OSError as error:
        raise ValueError(f"cannot read {cache}: {error}") from error
    observed: dict[str, str] = {}
    for line in text.splitlines():
        match = _CMAKE_CACHE_ENTRY.match(line)
        if match:
            observed[match.group(1)] = match.group(2)
    problems: list[str] = []
    for name, want in expected.items():
        if name not in observed:
            problems.append(f"{name}: absent (expected {want!r})")
            continue
        have = observed[name].strip()
        if isinstance(want, bool):
            matches = have.upper() in (_DEFINE_TRUE if want else _DEFINE_FALSE)
        else:
            matches = have == str(want)
        if not matches:
            problems.append(
                f"{name} = {have!r} (expected {'ON' if want is True else 'OFF' if want is False else want!r})"
            )
    return problems


class DarlingBootstrap(PrefixLifecycleMixin, RuntimePlanMixin, WestCommand):
    def __init__(self):
        super().__init__(
            "darling-bootstrap",
            "Fill a rootless Darling prefix from an explicit build dir",
            "Create/fill --prefix from --build-dir, deploy the rootless closure, doctor-gate it and smoke it",
            accepts_unknown_args=False,
        )

    def do_add_parser(self, parser_adder):
        p = parser_adder.add_parser(self.name, description=self.description)
        p.add_argument(
            "--prefix",
            required=True,
            help="the prefix to create or fill (a symlink is refused)",
        )
        p.add_argument(
            "--build-dir",
            required=True,
            help="the configured CMake build dir holding the accepted build variant",
        )
        p.add_argument(
            "--plan",
            default=str(DEFAULT_PLAN_PATH),
            help="explicit deploy plan (default: testkit/darling-bootstrap.yml)",
        )
        p.add_argument(
            "--no-build",
            action="store_true",
            help="deploy an already-built build dir without running ninja",
        )
        p.add_argument(
            "--smoke-timeout-seconds",
            type=int,
            default=None,
            help="override the plan's guest-smoke deadline",
        )
        p.add_argument(
            "--skip-define-check",
            action="store_true",
            help="deploy a build dir whose cmake defines differ from the plan (recorded loudly)",
        )
        p.add_argument(
            "--json",
            action="store_true",
            help="emit one machine-readable result line",
        )
        return p

    # -- the host surface the shared deployment machinery expects --------------
    def _prefix_lifecycle_owner(self) -> PrefixLifecycleOwner:
        return PrefixLifecycleOwner(
            resolve_launcher=self._resolve_darling_launcher,
            prefix_env=self._darling_prefix_env,
            cleanup_mounts=cleanup_prefix_mounts,
            init_pid_is_usable=darling_init_pid_is_usable,
            inf=self.inf,
            err=self.err,
            wrn=self.wrn,
            process_entries=self._ps_entries,
        )

    def _shutdown_runtime_prefix(
        self, prefix: Path, *, extra_env: dict[str, str] | None = None
    ) -> bool:
        """Stop a prefix the deploy is about to replace.

        The plan's launcher environment is the only source of runtime flags here: this path has no retained
        provider marker to consult, which is the point of it.
        """

        self._prefix_env = dict(extra_env or {})
        return self._prefix_lifecycle_owner().shutdown(Path(prefix), extra_env=extra_env)

    # -- run ------------------------------------------------------------------
    def do_run(self, args, unknown):
        plan_path = Path(args.plan).expanduser()
        try:
            plan = load_bootstrap_plan(plan_path)
        except ValueError as error:
            self.die(str(error))
        topdir = Path(self.topdir)
        build_dir = Path(args.build_dir).expanduser().resolve()
        prefix = Path(args.prefix).expanduser()
        if prefix.is_symlink():
            self.die(f"--prefix must not be a symlink: {prefix}")
        prefix = prefix.resolve()
        if prefix == prefix.parent:
            self.die(f"--prefix must not be a filesystem root: {prefix}")
        if not prefix.parent.is_dir():
            self.die(f"--prefix parent is not a directory: {prefix.parent}")
        self._prefix_env = dict(plan.launcher_env)

        # The limit the runtime will inherit, named before anything runs: every boot-based NOFILE
        # measurement is relative to it, and a run that fails must still say what it passed on.
        inherited_soft, inherited_hard = inherited_nofile_limits()
        self.inf(
            f"inherited NOFILE soft/hard limit passed to the runtime: "
            f"{inherited_soft}/{inherited_hard}"
        )

        # 0. the host can actually boot a rootless prefix at all.
        userns_problem = unprivileged_userns_problem()
        if userns_problem is not None:
            self.die(f"cannot bootstrap {prefix}: {userns_problem}")

        # 1. the build variant is explicit, and it is asserted, not assumed.
        cache = build_dir / "CMakeCache.txt"
        if not cache.is_file():
            self.die(f"{build_dir} is not a configured build dir (no CMakeCache.txt)")
        define_problems = compare_cmake_defines(cache, plan.cmake_defines)
        if define_problems:
            detail = "; ".join(define_problems)
            if args.skip_define_check:
                self.wrn(
                    f"build dir {build_dir} does not carry the plan's configuration "
                    f"({plan_path.name}): {detail} -- --skip-define-check given, deploying anyway"
                )
            else:
                self.die(
                    f"build dir {build_dir} does not carry the plan's configuration "
                    f"({plan_path.name}): {detail}\n"
                    f"  configure it explicitly, for example:\n    "
                    f"{plan.cmake_configure_line(topdir, prefix)}"
                )

        # The launcher bakes CMAKE_INSTALL_PREFIX as INSTALL_PREFIX and execs <it>/bin/darlingserver, so a
        # build dir configured for another install root cannot boot the prefix it is deployed into. This is a
        # hard gate, not a warning: the failure it prevents is an ENOENT exec with no server, no launchd and no
        # shellspawn, which looks like a runtime defect from every log the run keeps.
        baked_install_prefix = build_install_prefix(cache)
        if baked_install_prefix is None:
            self.wrn(
                f"{cache.name} names no CMAKE_INSTALL_PREFIX; the launcher's baked install root "
                "cannot be checked before it is deployed"
            )
        elif Path(baked_install_prefix).expanduser().resolve() != prefix:
            self.die(
                f"build dir {build_dir} was configured to install into {baked_install_prefix}, but this "
                f"bootstrap deploys into {prefix}.\n"
                f"  The launcher would exec "
                f"{Path(baked_install_prefix) / 'bin' / 'darlingserver'} and fail with ENOENT.\n"
                f"  Configure a build dir for this prefix, for example:\n    "
                f"{plan.cmake_configure_line(topdir, prefix)}"
            )

        # 2. prefix directory (contents, if any, are the deploy's business, not this command's).
        prefix.mkdir(parents=True, exist_ok=True)

        # 3. doctor before build/deploy.
        if not self._doctor("workspace", build_dir=build_dir):
            self.die("workspace doctor FAILED before the bootstrap; nothing was built or deployed")

        # 4. build the closure.
        targets = plan.all_build_targets()
        if args.no_build:
            self.inf(f"(--no-build) skipping ninja for {len(targets)} target(s) in {build_dir}")
        else:
            self.inf(f"== ninja ({len(targets)} targets) in {build_dir} ==")
            rc = subprocess.run(["ninja", *targets], cwd=build_dir).returncode
            if rc != 0:
                self.die(f"ninja FAILED in {build_dir} (rc {rc}); nothing was deployed")
            self.inf("build OK")

        # 5. deploy + guest smoke: the deployment is retained only if the smoke passes.
        timeout = (
            args.smoke_timeout_seconds
            or plan.smoke_timeout_seconds
            or DEFAULT_SMOKE_TIMEOUT_SECONDS
        )
        launcher = str(prefix / "bin" / "darling")
        guest_env = os.environ.copy()
        guest_env.update(plan.launcher_env)
        guest_env.update(
            {
                "DPREFIX": str(prefix),
                "DARLING_PREFIX": str(prefix),
                "DARLING_LAUNCHER": launcher,
            }
        )
        self.inf(
            f"== deploy + guest smoke (timeout {timeout}s) into {prefix} =="
        )
        service = RuntimeDeploymentService(self)
        with service.deployed(
            plan.proof(),
            build_dir,
            prefix,
            label="darling-bootstrap",
            restore_deployment=False,
        ):
            result = run_guest_shell(
                launcher,
                prefix,
                plan.smoke_script,
                cwd=topdir,
                env=guest_env,
                timeout_seconds=timeout,
                capture_output=True,
                heartbeat_seconds=30,
                heartbeat=lambda elapsed: self.inf(
                    f"prefix bootstrap heartbeat: guest smoke still running ({elapsed:.0f}s)"
                ),
                output_line=lambda stream, line: self.inf(
                    f"prefix bootstrap guest {stream}: {line}"
                ),
            )
            output = process_output_text(result)
            if result.timed_out:
                self.err(output)
                self.die(
                    f"guest smoke timed out after {timeout}s; the deployment was rolled back"
                )
            if result.returncode != 0 or SMOKE_MARKER not in output:
                self.err(output)
                self.die(
                    f"guest smoke failed (rc {result.returncode}, marker "
                    f"{'present' if SMOKE_MARKER in output else 'absent'}); "
                    "the deployment was rolled back"
                )

        # 6. doctor after guest smoke, then the receipt.
        runtime_doctor_ok = self._doctor("runtime", build_dir=build_dir, prefix=prefix)
        receipt_file = receipt_path(prefix)
        receipt = read_receipt(prefix)
        receipt_problems: list[str] = []
        receipt_notes: list[str] = []
        if receipt is None:
            receipt_problems = [f"no deployment receipt at {receipt_file}"]
        else:
            receipt_problems, receipt_notes = verify_receipt(receipt)

        summary = {
            "prefix": str(prefix),
            "build-dir": str(build_dir),
            "plan": str(plan_path),
            "build-targets": targets,
            "receipt": str(receipt_file),
            "receipt-valid": not receipt_problems and receipt is not None,
            "runtime-doctor": "PASS" if runtime_doctor_ok else "FAIL",
            "smoke": "PASS",
            "inherited-nofile-soft": inherited_soft,
            "inherited-nofile-hard": inherited_hard,
        }
        if args.json:
            print(json.dumps(summary, sort_keys=True))
        else:
            for note in receipt_notes:
                self.inf(f"deployment receipt: {note}")
            for problem in receipt_problems:
                self.err(f"deployment receipt: {problem}")
            self.inf(
                f"prefix bootstrap complete: {prefix}\n"
                f"  build dir: {build_dir}\n"
                f"  receipt:   {receipt_file}\n"
                f"  smoke:     PASS ({SMOKE_MARKER})\n"
                f"  receipt:   {'valid' if not receipt_problems else 'INVALID'}\n"
                f"  runtime:   {'doctor PASS' if runtime_doctor_ok else 'doctor FAIL'}\n"
                f"  inherited NOFILE soft/hard: {inherited_soft}/{inherited_hard}"
            )
        if not runtime_doctor_ok:
            self.die(
                "the deployment was retained, but the runtime doctor FAILED after the "
                "guest smoke; investigate before trusting this prefix"
            )
        if receipt_problems:
            self.die(
                "the deployment was retained, but its receipt does not verify against "
                "the deployed files; investigate before trusting this prefix"
            )

    def _doctor(
        self, scope: str, *, build_dir: Path | None = None, prefix: Path | None = None
    ) -> bool:
        cmd = ["west", "darling-doctor", "--scope", scope]
        if build_dir is not None:
            cmd.extend(["--build-dir", str(build_dir)])
        if prefix is not None:
            cmd.extend(["--prefix", str(prefix)])
        self.inf(f"== doctor ({scope}) ==")
        return subprocess.run(cmd, cwd=Path(self.topdir)).returncode == 0
