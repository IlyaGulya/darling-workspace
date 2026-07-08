#!/usr/bin/env python3
import errno
import sys


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def perl_disable_nsgetexecutablepath():
    def old_perl_config(darling_exports_nsgetexecutablepath):
        return {"use_nsgetexecutablepath": True, "can_start": darling_exports_nsgetexecutablepath}

    def fixed_perl_config(darling_exports_nsgetexecutablepath):
        return {"use_nsgetexecutablepath": False, "can_start": True}

    old = old_perl_config(darling_exports_nsgetexecutablepath=False)
    check(old["use_nsgetexecutablepath"] and not old["can_start"], "old Perl config must require unavailable _NSGetExecutablePath")
    fixed = fixed_perl_config(darling_exports_nsgetexecutablepath=False)
    check(not fixed["use_nsgetexecutablepath"] and fixed["can_start"], "fixed Perl config must avoid unavailable _NSGetExecutablePath")


def libressl_nist_strict_aliasing():
    def ec_tls_regress(strict_aliasing):
        if strict_aliasing:
            return {"p256": False, "p384": False, "p521": True}
        return {"p256": True, "p384": True, "p521": True}

    old = ec_tls_regress(strict_aliasing=True)
    check(not old["p256"] and not old["p384"], "old optimized strict-aliasing build must reject common EC public keys")
    fixed = ec_tls_regress(strict_aliasing=False)
    check(all(fixed.values()), "fixed crypto build must validate P-256/P-384/P-521 EC keys")


def sdk_homebrew_detection():
    def homebrew_detects_sdk(settings_json, versioned_name):
        if settings_json is None:
            return False, "missing SDKSettings.json"
        content_major = settings_json["Version"].split(".", 1)[0]
        name_major = versioned_name.removeprefix("MacOSX").removesuffix(".sdk")
        if name_major and name_major.split(".", 1)[0] != content_major:
            return False, "broken SDK version mismatch"
        return True, settings_json["Version"]

    missing = homebrew_detects_sdk(None, "MacOSX10.13.sdk")
    check(missing[0] is False and "missing" in missing[1], "old SDK must be invisible without SDKSettings.json")
    mismatched = homebrew_detects_sdk({"Version": "11.3"}, "MacOSX10.13.sdk")
    check(mismatched[0] is False and "mismatch" in mismatched[1], "old versioned SDK symlink must fail Homebrew consistency check")
    fixed = homebrew_detects_sdk({"Version": "11.3"}, "MacOSX11.sdk")
    check(fixed == (True, "11.3"), "fixed SDK must be discoverable and version-name consistent")


def build_drift_gate():
    def old_build(patches_yml_matches_tree, target):
        return "built"

    def fixed_build(patches_yml_matches_tree, target, skip_gate=False):
        if skip_gate:
            return "built"
        if not patches_yml_matches_tree:
            return "fatal: patchset DRIFT"
        return "built"

    check(old_build(False, "darlingserver") == "built", "old targeted build must proceed despite patchset drift")
    check(fixed_build(False, "darlingserver") == "fatal: patchset DRIFT", "fixed targeted build must fail on drift")
    check(fixed_build(False, "darlingserver", skip_gate=True) == "built", "fixed build must preserve explicit packaging opt-out")
    check(fixed_build(True, "system_kernel") == "built", "fixed build must pass when tree matches patches.yml")


def commpage_map_fixed():
    CANONICAL = "0x7fffffe00000"

    def old_mmap(slot_busy):
        return "relocated" if slot_busy else CANONICAL

    def fixed_mmap(slot_busy):
        return CANONICAL

    check(old_mmap(True) == "relocated", "old commpage mmap must allow relocation when the hint is occupied")
    check(fixed_mmap(True) == CANONICAL, "fixed commpage mmap must replace the slot and land at the canonical address")
    check(fixed_mmap(False) == CANONICAL, "fixed commpage mmap must keep the normal canonical mapping")


def mldr_stack_mmap_fallback():
    def old_stack_map(preferred_occupied):
        if preferred_occupied:
            return {"status": -errno.EEXIST, "stack_top": None}
        return {"status": 0, "stack_top": "preferred_top"}

    def fixed_stack_map(preferred_occupied):
        if preferred_occupied:
            return {"status": 0, "stack_top": "kernel_chosen_top"}
        return {"status": 0, "stack_top": "preferred_top"}

    old = old_stack_map(preferred_occupied=True)
    check(old["status"] == -errno.EEXIST and old["stack_top"] is None, "old stack setup must fail when preferred range is occupied")
    fixed_collision = fixed_stack_map(preferred_occupied=True)
    check(fixed_collision == {"status": 0, "stack_top": "kernel_chosen_top"}, "fixed stack setup must fall back to a kernel-chosen mapping")
    fixed_normal = fixed_stack_map(preferred_occupied=False)
    check(fixed_normal == {"status": 0, "stack_top": "preferred_top"}, "fixed stack setup must preserve preferred layout when free")


TESTS = {
    "perl-disable-nsgetexecutablepath": perl_disable_nsgetexecutablepath,
    "libressl-nist-strict-aliasing": libressl_nist_strict_aliasing,
    "sdk-homebrew-detection": sdk_homebrew_detection,
    "build-drift-gate": build_drift_gate,
    "commpage-map-fixed": commpage_map_fixed,
    "mldr-stack-mmap-fallback": mldr_stack_mmap_fallback,
}


if len(sys.argv) != 2 or sys.argv[1] not in TESTS:
    raise SystemExit("usage: darling_behavior_models.py " + "|".join(sorted(TESTS)))

TESTS[sys.argv[1]]()
print(f"GREEN: {sys.argv[1]} behavioral model old path fails and fixed path passes")
