"""Pure discovery and shell-value completion metadata for Darling profiles."""
from __future__ import annotations

import stat
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from . import test_manifest
    from .test_runtime import load_ctest_runtime_profiles
except ImportError:  # Loaded as a West extension module, not a package.
    import test_manifest
    from test_runtime import load_ctest_runtime_profiles


PATCH_PROFILE_KIND = "patch"
RUNTIME_PROFILE_KIND = "runtime"
ALL_PROFILE_KIND = "all"
PROFILE_KINDS = (PATCH_PROFILE_KIND, RUNTIME_PROFILE_KIND, ALL_PROFILE_KIND)

PROFILE_OPTION = "--profile"
WITH_RUNTIME_PROFILE_OPTION = "--with-runtime-profile"
BOOTSTRAP_RUNTIME_PROFILE_OPTION = "--bootstrap-runtime-profile"
PROFILE_COMPLETION_KINDS = {
    PROFILE_OPTION: PATCH_PROFILE_KIND,
    WITH_RUNTIME_PROFILE_OPTION: RUNTIME_PROFILE_KIND,
    BOOTSTRAP_RUNTIME_PROFILE_OPTION: RUNTIME_PROFILE_KIND,
}
BOOTSTRAP_RUNTIME_PROFILE_PURPOSES = (
    "guest-toolchain-provisioning",
    "prefix-baseline",
)




class ProfileCatalogError(ValueError):
    """A profile catalog source is malformed or escapes its manifest boundary."""


class ProfileCatalogOperationalError(RuntimeError):
    """A profile catalog source could not be read from the filesystem."""


def add_profile_argument(parser: Any, option: str, completion_kind: str, **kwargs: Any) -> Any:
    """Add one catalog-backed profile option without changing argparse semantics."""

    expected_kind = PROFILE_COMPLETION_KINDS.get(option)
    if expected_kind is None or completion_kind != expected_kind:
        raise ValueError(
            f"unsupported profile completion option/kind: {option}={completion_kind}"
        )
    action = parser.add_argument(option, **kwargs)
    if action is not None:
        action.profile_completion_kind = completion_kind
    return action


def collect_profile_catalog(
    manifest_repo: Path,
    kind: str = ALL_PROFILE_KIND,
    purposes: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Return the deterministic v1 patch/runtime profile catalog."""

    if kind not in PROFILE_KINDS:
        raise ProfileCatalogError(
            f"profile kind must be one of: {', '.join(PROFILE_KINDS)}"
        )
    normalized_purposes = tuple(
        sorted(
            {
                _safe_name(purpose, "runtime profile purpose filter")
                for purpose in purposes
            }
        )
    )
    if normalized_purposes and kind == PATCH_PROFILE_KIND:
        raise ProfileCatalogError(
            "runtime profile purpose filters require --kind runtime or --kind all"
        )
    root = _manifest_root(manifest_repo)
    rows: list[dict[str, Any]] = []
    try:
        if kind in {PATCH_PROFILE_KIND, ALL_PROFILE_KIND}:
            rows.extend(collect_patch_profiles(root))
        if kind in {RUNTIME_PROFILE_KIND, ALL_PROFILE_KIND}:
            runtime_rows = collect_runtime_profiles(root)
            if normalized_purposes:
                runtime_rows = [
                    row
                    for row in runtime_rows
                    if row["purpose"] in normalized_purposes
                ]
            rows.extend(runtime_rows)
    except (ProfileCatalogError, ProfileCatalogOperationalError):
        raise
    except OSError as error:
        raise ProfileCatalogOperationalError(str(error)) from error
    except (ValueError, TypeError, AttributeError) as error:
        raise ProfileCatalogError(str(error)) from error
    rows.sort(key=lambda row: (row["kind"], row["name"]))
    inputs: dict[str, Any] = {"kind": kind}
    if normalized_purposes:
        inputs["purposes"] = list(normalized_purposes)
    return {
        "schema_version": 1,
        "operation": "profiles",
        "state": "valid",
        "inputs": inputs,
        "profiles": rows,
        "returncode": 0,
    }


def profile_names(payload: dict[str, Any]) -> list[str]:
    """Extract ordered names for the newline-delimited shell protocol."""

    return [str(profile["name"]) for profile in payload.get("profiles", [])]


def bash_profile_completion() -> str:
    """Emit the narrow Bash completion used only for catalog-backed values."""

    patch_option = PROFILE_OPTION
    runtime_option = WITH_RUNTIME_PROFILE_OPTION
    bootstrap_option = BOOTSTRAP_RUNTIME_PROFILE_OPTION
    bootstrap_purposes = " ".join(
        f'--purpose "{purpose}"' for purpose in BOOTSTRAP_RUNTIME_PROFILE_PURPOSES
    )
    return f'''# Darling profile-value completion only; this is not a full West completer.
_darling_west_profile_values() {{
    local current="${{COMP_WORDS[COMP_CWORD]}}"
    local previous=""
    local option=""
    local prefix="$current"
    local kind=""
    local command=""
    local action=""
    local patch_context=""
    local equals_form=""
    local output=""
    local name
    local word
    local index
    local -a candidates=()
    local -a purpose_args=()

    if (( COMP_CWORD > 0 )); then
        previous="${{COMP_WORDS[COMP_CWORD-1]}}"
    fi
    index=1
    while (( index < ${{#COMP_WORDS[@]}} )); do
        word="${{COMP_WORDS[index]}}"
        case "$word" in
            -z|--zephyr-base)
                ((index += 2))
                continue
                ;;
            -z?*|--zephyr-base=*)
                ((index += 1))
                continue
                ;;
            -h|--help|-V|--version|-v*|--verbose|-q*|--quiet)
                ((index += 1))
                continue
                ;;
            --)
                ((index += 1))
                ;;
        esac
        if (( index < ${{#COMP_WORDS[@]}} )); then
            command="${{COMP_WORDS[index]}}"
            if (( index + 1 < ${{#COMP_WORDS[@]}} )); then
                action="${{COMP_WORDS[index+1]}}"
            fi
        fi
        break
    done
    case "$command:$action" in
        patch:*|test:*|dev:status|dev:check|dev:package)
            patch_context=1
            ;;
    esac

    if [[ "$previous" == "=" && "$COMP_CWORD" -gt 1 ]]; then
        option="${{COMP_WORDS[COMP_CWORD-2]}}"
        equals_form=split
    elif [[ "$current" == --*=* ]]; then
        option="${{current%%=*}}"
        prefix="${{current#*=}}"
        equals_form=joined
    else
        option="$previous"
    fi

    case "$option" in
        {patch_option})
            [[ -n "$patch_context" ]] || return 0
            kind="{PATCH_PROFILE_KIND}"
            ;;
        {runtime_option})
            [[ "$command" == test ]] || return 0
            kind="{RUNTIME_PROFILE_KIND}"
            ;;
        {bootstrap_option})
            [[ "$command" == test ]] || return 0
            kind="{RUNTIME_PROFILE_KIND}"
            purpose_args=({bootstrap_purposes})
            ;;
        *)
            return 0
            ;;
    esac

    COMPREPLY=()
    compopt +o default +o bashdefault 2>/dev/null || true
    if ! output="$("${{COMP_WORDS[0]}}" dev profiles --names --kind "$kind" "${{purpose_args[@]}}" 2>/dev/null)"; then
        return 0
    fi
    mapfile -t candidates <<< "$output"
    for name in "${{candidates[@]}}"; do
        [[ -n "$name" && "$name" == "$prefix"* ]] || continue
        if [[ "$equals_form" == joined ]]; then
            COMPREPLY+=("$option=$name")
        else
            COMPREPLY+=("$name")
        fi
    done
}}
complete -o default -o bashdefault -F _darling_west_profile_values west
'''


def collect_patch_profiles(manifest_repo: Path) -> list[dict[str, Any]]:
    """Load immediate patch profile manifests without consulting Git state."""

    root = _manifest_root(manifest_repo)
    patch_root = root / "patches"
    if not _is_directory(patch_root):
        raise ProfileCatalogError(f"patch profile directory not found: {patch_root}")
    rows: list[dict[str, Any]] = []
    try:
        profile_directories = sorted(patch_root.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise ProfileCatalogOperationalError(
            f"cannot enumerate patch profile directory {patch_root}: {error}"
        ) from error
    for profile_dir in profile_directories:
        profile_path = profile_dir / "patches.yml"
        if not _is_directory(profile_dir) or not _is_regular_file(profile_path):
            continue
        name = _safe_name(profile_dir.name, f"patch profile at {profile_path}")
        relative_path = _manifest_relative_path(root, profile_path)
        try:
            profile = test_manifest.load_test_profile(profile_path)
        except OSError as error:
            raise ProfileCatalogOperationalError(
                f"cannot read patch profile {relative_path}: {error}"
            ) from error
        except Exception as error:
            raise ProfileCatalogError(
                f"invalid patch profile {relative_path}: {error}"
            ) from error
        description = _safe_description(
            profile.get("description", ""), f"description in {relative_path}"
        )
        base_profile = profile.get("base-profile")
        patches = profile.get("patches", [])
        if base_profile is not None:
            base_profile = _safe_name(
                base_profile, f"base-profile in {relative_path}"
            )
        if not isinstance(patches, list):
            raise ProfileCatalogError(
                f"invalid patch profile {relative_path}: patches must be a list"
            )
        rows.append(
            {
                "kind": PATCH_PROFILE_KIND,
                "name": name,
                "description": description,
                "base_profile": base_profile,
                "patch_count": len(patches),
                "path": relative_path,
            }
        )
    return rows


def collect_runtime_profiles(manifest_repo: Path) -> list[dict[str, Any]]:
    """Load CTest runtime profile definitions without deploying a runtime."""

    root = _manifest_root(manifest_repo)
    profile_path = root / "testkit" / "runtime-profiles.yml"
    relative_path = _manifest_relative_path(root, profile_path)
    if not _is_regular_file(profile_path):
        raise ProfileCatalogError(f"runtime profile manifest not found: {relative_path}")
    try:
        profiles = load_ctest_runtime_profiles(profile_path)
    except OSError as error:
        raise ProfileCatalogOperationalError(
            f"cannot read runtime profile manifest {relative_path}: {error}"
        ) from error
    except Exception as error:
        raise ProfileCatalogError(
            f"invalid runtime profile manifest {relative_path}: {error}"
        ) from error
    rows: list[dict[str, Any]] = []
    for raw_name, profile in profiles.items():
        name = _safe_name(raw_name, f"runtime profile in {relative_path}")
        source_profile = _safe_name(
            profile["source-profile"], f"source-profile for runtime profile {name!r}"
        )
        source_module = _safe_relative_value(
            profile["source-module"], f"source-module for runtime profile {name!r}"
        )
        rows.append(
            {
                "kind": RUNTIME_PROFILE_KIND,
                "name": name,
                "source_profile": source_profile,
                "source_module": source_module,
                "purpose": profile["purpose"],
                "path": relative_path,
            }
        )
    return rows


def _safe_description(value: Any, location: str) -> str:
    if (
        not isinstance(value, str)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProfileCatalogError(f"unsafe {location}: {value!r}")
    return value


def _safe_name(value: Any, location: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.startswith("-")
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProfileCatalogError(f"unsafe {location}: {value!r}")
    return value


def _safe_relative_value(value: Any, location: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ProfileCatalogError(f"unsafe {location}: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} for part in value.split("/")
    ):
        raise ProfileCatalogError(f"unsafe {location}: {value!r}")
    return value


def _manifest_root(manifest_repo: Path) -> Path:
    try:
        return Path(manifest_repo).resolve()
    except OSError as error:
        raise ProfileCatalogOperationalError(
            f"cannot resolve manifest repository path {manifest_repo}: {error}"
        ) from error


def _manifest_relative_path(root: Path, path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except OSError as error:
        raise ProfileCatalogOperationalError(
            f"cannot resolve profile manifest path {path}: {error}"
        ) from error
    try:
        resolved.relative_to(root)
        lexical = path.relative_to(root)
    except ValueError as error:
        raise ProfileCatalogError(f"unsafe profile manifest path: {path}") from error
    if any(part in {"", ".", ".."} for part in lexical.parts):
        raise ProfileCatalogError(f"unsafe profile manifest path: {path}")
    return lexical.as_posix()


def _is_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ProfileCatalogOperationalError(f"cannot inspect directory {path}: {error}") from error


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ProfileCatalogOperationalError(f"cannot inspect file {path}: {error}") from error
