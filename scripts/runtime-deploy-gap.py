#!/usr/bin/env python3
"""Enumerate the runtime deploy gap for one build tree and one prefix, per provider.

The question this answers ONCE, instead of discovering it one abort at a time:

    which runtime artifacts does the build produce that the prefix does not carry,
    and WHICH mechanism decides that?

Three providers decide it for this workspace:

  A. the canonical plan -- ``testkit/darling-bootstrap.yml`` read by
     ``west darling-bootstrap``. Its set is the plan's explicit ``deploy:`` paths
     plus the two CMake component manifests
     (``build/<dir>/darling-rootless-{bootstrap,toolchain}.json``) plus the
     Mach-O closure resolver's dependencies
     (``west_commands/test_runtime_deploy.py``).
  B. the declared runtime additions -- ``scripts/ios-toolchain-runtime-additions.sh``,
     whose set is its ``targets`` array of CMake install targets, read here
     statically out of each target's generated ``cmake_install.cmake`` (the
     declaration, not the effect of running it).
  C. the runtime BUILD -- the plan's own ``build-targets``, whose transitive
     input closure in ``build.ninja`` is what "the runtime this prefix serves"
     means. A runtime artifact OUTSIDE that closure is not something this
     deployment was ever asked to ship; a runtime artifact INSIDE it that no
     declaration names is a declaration gap by construction.

The script is read-only. It prints:

  1. provider A's RAW target list (every target of every deploy path, before the
     alias resolution the deploy transaction performs) and, of that list, every
     prefix FILE named through more than one path. That is exactly the class that
     aborted the canonical bootstrap with "duplicate deploy destination".
  2. provider A's delivered plan and its distinct destination-file count.
  3. provider A's destinations the prefix does not carry yet, or carries with
     different bytes -- the deploy GAP, with sizes and sha256.
  4. a prefix inventory under the artifact roots, classifying everything
     provider A does not deliver (provider B's installs, the debug-symbol cache,
     runtime state) with the reason.
  5. THE BUILD DIRECTION (added 2026-10-14, the MobileDevice rung): every Mach-O
     image the build produces that carries an absolute-path LC_ID -- i.e. an
     image that declares the guest path the loader can be asked for -- classified
     by whether provider A's plan or provider B's declaration delivers that guest
     path, and, for the ones no provider delivers, whether the RUNTIME BUILD
     produces the image. The last subset is the honest defect set; the rest is the
     part of the build tree that is not the rootless runtime at all (the GUI
     frameworks, the native/X11 subsystem, the pam modules, ...), and reporting
     them as "undeclared" instead of separating them is how a gate over this
     property becomes noise.

Run it from the workspace root; it imports the West command modules, so it needs
the West interpreter (``mise exec -- python3`` or the pipx West environment).
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
WORKSPACE = HERE.parents[1]
sys.path.insert(0, str(WORKSPACE / "west_commands"))

import test_runtime_deploy as deploy_module  # noqa: E402
from test_runtime_build import RuntimeBuildService  # noqa: E402
import darling_bootstrap  # noqa: E402

# Mach-O magics: thin 32/64 both endiannesses and the fat headers, because the
# build tree holds fat (x86_64 + arm64) and thin images side by side and a scan
# that reads only one spelling under-reports the defect set. Measured
# 2026-10-14: reading only the thin little-endian magics found 26 images with an
# LC_ID in this tree; including the fat headers found 445.
MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
}

ADDITIONS_SCRIPT = "scripts/ios-toolchain-runtime-additions.sh"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Host:
    """The host surface RuntimeDeploymentService uses; notes are kept."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.warnings: list[str] = []

    def die(self, message: str) -> None:
        raise SystemExit(f"DIE: {message}")

    def inf(self, message: str) -> None:
        self.notes.append(str(message))

    def wrn(self, message: str) -> None:
        self.warnings.append(str(message))

    def _runtime_red_find_build_output(self, build_root: Path, deploy_path: str) -> Path:
        return RuntimeBuildService(self).find_build_output(build_root, deploy_path)


def classify_delta(relative: str) -> str:
    if relative.startswith(".darling"):
        return "deploy-transaction state (West's own, not a runtime artifact)"
    if relative.startswith("private/var/tmp/") or relative.startswith("private/tmp/"):
        return "workload scratch (the xcodebuild fixtures and their outputs)"
    if relative.startswith(("private/var/", "private/etc/", "var/")):
        return "runtime state written by a boot, not a built artifact"
    if relative.startswith(("Library/", "Users/")):
        return "guest user data"
    if "/Caches/dsym/" in relative:
        return "debug-symbol cache (deliberately not deployed: symbols, not code)"
    if relative.endswith("/Current"):
        return "framework version symlink (CMake install makes it, not the deploy plan)"
    if relative in ("dev", "etc"):
        return "pre-existing prefix symlink (top of the prefix, not a plan path)"
    return "delivered by a provider other than the canonical plan (the additions script / an install target)"


def provider_b_declarations(build_root: Path, prefix: Path) -> set[str]:
    """Provider B's declared prefix-relative destinations, read from its install targets.

    The declaration is the ``targets`` array (CMake install targets); what each target
    installs is the tree's own ``cmake_install.cmake`` for that directory, whose
    ``file(INSTALL ... DESTINATION ...)`` lines name both the literal prefix path and the
    ``${CMAKE_INSTALL_PREFIX}`` spelling. Reading it is the static form of the declaration --
    the script itself must run ninja to make it true, and this tool never runs it.
    """
    script = WORKSPACE / ADDITIONS_SCRIPT
    if not script.is_file():
        return set()
    text = script.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^targets=\((.*?)^\)", text, re.S | re.M)
    if not match:
        return set()
    targets = [
        line.split("#")[0].strip()
        for line in match.group(1).splitlines()
        if line.split("#")[0].strip()
    ]
    destination = re.compile(
        r'"(\$ENV\{DESTDIR\})?(?:\$\{CMAKE_INSTALL_PREFIX\}|' + re.escape(str(prefix)) + r')/([^"]*)"'
    )
    declared: set[str] = set()
    for target in targets:
        directory = target[: -len("/install")] if target.endswith("/install") else target
        script_path = build_root / directory / "cmake_install.cmake"
        if not script_path.is_file():
            continue
        for line in script_path.read_text(encoding="utf-8", errors="replace").splitlines():
            for found in destination.finditer(line):
                declared.add(found.group(2))
    return declared


def built_images_with_install_names(build_root: Path) -> dict[str, str]:
    """path -> absolute LC_ID for every Mach-O in the build tree that declares one.

    The absolute LC_ID is the property that makes an image part of this class: the image
    itself states the guest path the loader may be asked for it by, so no LOAD command in
    any referrer has to exist for it to be a runtime demand.
    """
    found: dict[str, str] = {}
    for root, _dirs, names in os.walk(build_root):
        for name in names:
            path = os.path.join(root, name)
            try:
                with open(path, "rb") as handle:
                    if handle.read(4) not in MACHO_MAGICS:
                        continue
            except OSError:
                continue
            install_name = read_lc_id(path)
            if install_name and install_name.startswith("/"):
                found[path] = install_name
    return found


def read_lc_id(path: str) -> str | None:
    """The image's own LC_ID_DYLIB, from the first slice that carries one, or None."""
    import struct

    try:
        data = open(path, "rb").read()
    except OSError:
        return None
    if len(data) < 8:
        return None
    magic = struct.unpack_from(">I", data, 0)[0]
    slices = []
    if magic in (0xCAFEBABE, 0xCAFEBABF):
        count = struct.unpack_from(">I", data, 4)[0]
        offset = 8
        for _ in range(count):
            _cpu, _sub, start, size, _align = struct.unpack_from(">iiIII", data, offset)
            offset += 20
            slices.append(data[start:start + size])
    else:
        slices.append(data)
    for blob in slices:
        if len(blob) < 32 or struct.unpack_from("<I", blob, 0)[0] != 0xFEEDFACF:
            continue
        ncmds = struct.unpack_from("<I", blob, 16)[0]
        offset = 32
        for _ in range(ncmds):
            if offset + 8 > len(blob):
                break
            cmd, cmdsize = struct.unpack_from("<II", blob, offset)
            if cmdsize < 8:
                break
            if cmd == 0xD:  # LC_ID_DYLIB
                name_off = struct.unpack_from("<I", blob, offset + 8)[0]
                end = blob.index(b"\0", offset + name_off)
                return blob[offset + name_off:end].decode("utf-8", "replace")
            offset += cmdsize
    return None


def runtime_build_closure(build_root: Path, roots: list[str]) -> set[str]:
    """Every build.ninja node reachable from the plan's build targets.

    This is the derived boundary of "the runtime this prefix serves": the plan says which
    targets the build dir is built with, and everything those targets produce (transitively)
    is what the deployment is asked to ship or deliberately leave to a declaration.
    """
    ninja = build_root / "build.ninja"
    if not ninja.is_file():
        return set()
    dependencies: dict[str, list[str]] = {}
    for line in ninja.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("build "):
            continue
        body = line[len("build "):]
        if ":" not in body:
            continue
        outputs, rest = body.split(":", 1)
        parts = rest.strip().split(" ", 1)
        inputs = parts[1] if len(parts) > 1 else ""
        tokens = [token for token in inputs.replace("|", " ").split() if token]
        for output in outputs.split():
            dependencies.setdefault(output, []).extend(tokens)
    seen: set[str] = set()
    stack = [root for root in roots if root in dependencies]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for dependency in dependencies.get(node, ()):
            if dependency not in seen:
                stack.append(dependency)
    return seen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--plan", default=str(WORKSPACE / "testkit/darling-bootstrap.yml"))
    parser.add_argument("--delta-limit", type=int, default=0,
                        help="print at most N classified prefix-delta lines (0: all)")
    parser.add_argument("--no-build-direction", action="store_true",
                        help="skip the build -> providers direction (needs to read every Mach-O)")
    args = parser.parse_args()

    build_root = Path(args.build_dir).expanduser().resolve()
    prefix = Path(args.prefix).expanduser().resolve()
    plan = darling_bootstrap.load_bootstrap_plan(Path(args.plan))
    host = Host()

    # Record every deploy path's targets while the REAL planner computes them; the
    # planner then resolves aliases away, so this is the only exact view of the
    # pre-dedupe list.
    raw_calls: list[tuple[str, str, list]] = []
    real_targets = deploy_module.runtime_deploy_targets

    def recording_targets(prefix_arg, deploy_path, **kwargs):
        targets = real_targets(prefix_arg, deploy_path, **kwargs)
        raw_calls.append((deploy_path, str(kwargs.get("placement", "runtime")), list(targets)))
        return targets

    deploy_module.runtime_deploy_targets = recording_targets
    try:
        service = deploy_module.RuntimeDeploymentService(host)
        delivered = service.deployment_plan(plan.proof(), build_root, prefix)
    finally:
        deploy_module.runtime_deploy_targets = real_targets

    print(f"build dir: {build_root}")
    print(f"prefix:    {prefix}")
    print(f"plan:      {args.plan}")
    print()

    # --- 1. provider A, literal vs resolved ---------------------------------
    raw = [target for _path, _placement, targets in raw_calls for target in targets]
    by_file: dict[Path, list[Path]] = collections.defaultdict(list)
    for target in raw:
        if target not in by_file[target.resolve()]:
            by_file[target.resolve()].append(target)
    aliased = {resolved: paths for resolved, paths in by_file.items() if len(paths) > 1}
    print(f"provider A raw targets: {len(raw)} over {len(raw_calls)} deploy paths")
    print(f"  distinct destination FILES: {len(by_file)}")
    print(f"  destinations named through more than one path: {len(aliased)}")
    source_by_path: dict[Path, Path] = {}
    for source, destination in delivered:
        source_by_path[destination.resolve()] = source
    for resolved, paths in sorted(aliased.items()):
        sources = {source_by_path.get(path.resolve()) for path in paths}
        print(f"    {resolved}")
        for path in paths:
            print(f"      via   {path}")
        print(f"      from  {' / '.join(sorted(str(s) for s in sources if s is not None))}")
    print()

    # --- 2. the delivered plan ----------------------------------------------
    delivered_files = {destination.resolve(): source for source, destination in delivered}
    print(f"provider A delivered plan: {len(delivered)} pairs, "
          f"{len(delivered_files)} distinct destination files")
    for note in host.notes:
        print(f"  plan note:{note}")
    for note in host.warnings:
        print(f"  plan warning: {note}")
    print()

    # --- 3. the gap: delivered but absent / different ------------------------
    absent: list[tuple[Path, Path]] = []
    different: list[tuple[Path, Path, str, str]] = []
    for destination, source in sorted(delivered_files.items(), key=lambda item: str(item[0])):
        if not destination.exists():
            absent.append((destination, source))
        elif not destination.is_file():
            different.append((destination, source, "not-a-regular-file", sha256(source)))
        else:
            deployed = sha256(destination)
            built = sha256(source)
            if deployed != built:
                different.append((destination, source, deployed, built))
    print(f"provider A destinations the prefix does NOT carry: {len(absent)}")
    for destination, source in absent:
        print(f"  ABSENT  {destination}")
        print(f"          <- {source} bytes={source.stat().st_size} sha256={sha256(source)}")
    print(f"provider A destinations the prefix carries with DIFFERENT bytes: {len(different)}")
    for destination, source, deployed, built in different:
        print(f"  DIFFERS {destination} deployed={deployed} built={built} <- {source}")
    print()

    # --- 4. inventory -------------------------------------------------------
    prefix_files: list[Path] = []
    for root, dirs, files in os.walk(prefix, followlinks=False):
        for name in files:
            prefix_files.append(Path(root) / name)
        for name in list(dirs):
            candidate = Path(root) / name
            if candidate.is_symlink():
                prefix_files.append(candidate)
    delta = sorted(
        str(path.relative_to(prefix))
        for path in prefix_files
        if path.resolve() not in delivered_files
    )
    classes = collections.Counter(classify_delta(relative) for relative in delta)
    print(f"prefix entries (files + symlinks): {len(prefix_files)}")
    print(f"entries provider A does not deliver: {len(delta)}")
    for reason, count in classes.most_common():
        print(f"  {count:6d}  {reason}")
    print()
    print("delta, classified:")
    shown = delta if args.delta_limit == 0 else delta[: args.delta_limit]
    for relative in shown:
        print(f"  {classify_delta(relative):<78} {relative}")
    print()

    # --- 5. the build direction: what the BUILD declares, and who ships it ---
    if args.no_build_direction:
        return 0

    # guest path -> delivered prefix-relative path, both roots collapsed
    plan_paths: set[str] = set()
    for source, destination in delivered:
        relative = str(destination.relative_to(prefix))
        plan_paths.add(relative)
        if relative.startswith("libexec/darling/"):
            plan_paths.add(relative[len("libexec/darling/"):])
    declared_b = provider_b_declarations(build_root, prefix)
    print(f"provider B declared destinations (static, from its install targets): {len(declared_b)}")

    images = built_images_with_install_names(build_root)
    print(f"built Mach-O images with an absolute LC_ID: {len(images)}")

    produced_by_runtime = runtime_build_closure(build_root, plan.all_build_targets())
    print(f"build-tree nodes in the runtime build's input closure: {len(produced_by_runtime)}")
    print()

    undeclared: dict[str, str] = {}
    for path, install_name in sorted(images.items(), key=lambda item: item[1]):
        name = install_name.lstrip("/")
        if "_firstpass" in os.path.basename(path) or "CMakeFiles" in path:
            continue  # a build intermediate, not an installable image
        if name in plan_paths or install_name in plan_paths or name in declared_b:
            continue
        undeclared[path] = install_name

    in_runtime = {path: name for path, name in undeclared.items()
                  if os.path.relpath(path, build_root) in produced_by_runtime}
    out_runtime = {path: name for path, name in undeclared.items()
                   if os.path.relpath(path, build_root) not in produced_by_runtime}

    print(f"built images NO provider delivers (excluding _firstpass intermediates): {len(undeclared)}")
    print(f"  of those, PRODUCED BY the runtime build -- a declaration gap by construction: "
          f"{len(in_runtime)}")
    for path, name in sorted(in_runtime.items(), key=lambda item: item[1]):
        print(f"    GAP  {name}")
        print(f"         <- {os.path.relpath(path, build_root)}")
    print(f"  of those, NOT produced by the runtime build -- outside the scope this plan serves: "
          f"{len(out_runtime)}")
    kinds = collections.Counter()
    for path, _name in out_runtime.items():
        relative = os.path.relpath(path, build_root)
        parts = relative.split(os.sep)
        kinds["/".join(parts[:2]) if len(parts) > 1 else parts[0]] += 1
    for kind, count in kinds.most_common():
        print(f"    {count:6d}  {kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
