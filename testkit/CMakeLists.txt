# SPDX-License-Identifier: MIT-0
#
# Local compatibility suite for `west test`. Self-contained: builds with plain
# cc/clang, registers cases via add_compat_test(), and is driven by ctest (and,
# on top of it, by `west test`). It compiles the REAL production code under test
# from the darling checkout (DARLING_SRC), so regressions track the code they
# guard rather than a copy.

cmake_minimum_required(VERSION 3.13)
project(darling-testkit C)

include(CTest)
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/AddCompatTest.cmake")

set(DARLING_COMPAT_INSTALL_MANIFEST
  "${CMAKE_CURRENT_BINARY_DIR}/compat-install-manifest.tsv")
file(WRITE "${DARLING_COMPAT_INSTALL_MANIFEST}" "")
install(FILES "${DARLING_COMPAT_INSTALL_MANIFEST}" DESTINATION ".")

find_package(Threads REQUIRED)

# Locate the darling source checkout. Defaults to the sibling layout
# (workspace/.. /darling); override with -DDARLING_SRC=/path/to/darling.
if(NOT DEFINED DARLING_SRC)
  get_filename_component(_ws_parent "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
  set(DARLING_SRC "${_ws_parent}/darling")
endif()
get_filename_component(_workspace_root "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)

# Host-only XNU tests can be pointed at a materialized XNU worktree without
# replacing the whole Darling checkout. West uses this to keep source-base RED
# and profile GREEN builds isolated from the developer's current source tree.
if(NOT DEFINED DARLING_XNU_SRC)
  set(DARLING_XNU_SRC "${DARLING_SRC}/src/external/xnu")
endif()

# ---------------------------------------------------------------------------
# dar-dar6x4-perf-5dq.1 — shell-gate migration for the mldr checkin wait.
#
# The original Darling regression script builds thread_create_checkin_wait.c,
# proves the old sched_yield spin path fails, then runs the futex/adaptive wait
# path. Register the same source in CTest directly: one normal GREEN entry and
# one RED arm selected by argv `spin`, with its expected busy-spin symptom.
# ---------------------------------------------------------------------------
set(_thread_checkin_wait
  "${DARLING_SRC}/tests/regression/thread_create_checkin_wait.c")
if(EXISTS "${_thread_checkin_wait}")
  add_compat_test(
    NAME       mldr_thread_create_checkin_wait
    SOURCE     "${_thread_checkin_wait}"
    LIBS       Threads::Threads
    ENVS       host
    BEAD       dar-dar6x4-perf-5dq.1
    SUBMODULES darling
    DIAG       bare
    TIMEOUT    5
  )

  add_compat_test(
    NAME       mldr_thread_create_checkin_wait_spin_red
    SOURCE     "${_thread_checkin_wait}"
    LIBS       Threads::Threads
    ENVS       host
    BEAD       dar-dar6x4-perf-5dq.1
    SUBMODULES darling
    DIAG       bare
    TIMEOUT    5
    ARGS       spin
    EXPECT_FAILURE_MARKER "FAIL: creator burned"
  )
else()
  message(WARNING
    "darling checkout missing ${_thread_checkin_wait}; skipping "
    "dar-dar6x4-perf-5dq.1")
endif()

# E-UNION's host harness executes the selected profile's XNU sources and
# provides source-base RED proof. A static testkit configuration only knows
# the current checkout, which may be a different profile. West therefore owns
# this source-profile runner through patch metadata; registering it here would
# make a generic CTest smoke test run a homebrew assertion against, for example,
# integration/arch.
set(_mldr "${DARLING_SRC}/src/startup/mldr")
set(_libsystem_kernel_tests
  "${DARLING_XNU_SRC}/darling/src/libsystem_kernel/tests")

# dar-yfp4 — the E-UNION resolver is pure host filesystem policy. Compile the
# production resolver plus its colocated C fixture as a regular CTest case;
# the fixture creates real upper/lower trees and xattr markers, not a source
# matcher or a model of vchroot.
set(_eunion_resolver_dir
  "${DARLING_XNU_SRC}/darling/src/libsystem_kernel/emulation")
set(_eunion_resolver_source
  "${_eunion_resolver_dir}/tests/eunion_resolver_test.c")
set(_eunion_resolver_impl
  "${_eunion_resolver_dir}/src/linux_premigration/eunion_resolver.c")
if(EXISTS "${_eunion_resolver_source}" AND EXISTS "${_eunion_resolver_impl}")
  add_executable(eunion_resolver_test
    "${_eunion_resolver_source}"
    "${_eunion_resolver_impl}")
  target_include_directories(eunion_resolver_test PRIVATE
    "${_eunion_resolver_dir}/include")
  add_test(NAME host/eunion_resolver_test COMMAND eunion_resolver_test)
  set_property(TEST host/eunion_resolver_test PROPERTY
    LABELS "env:host;diag:bare;bead:dar-yfp4;submod:xnu;eunion-host")
endif()

# ---------------------------------------------------------------------------
# dar-cps — guest runtime regression for abort_with_payload().
#
# This must execute inside Darling: the oracle calls libSystem's
# abort_with_payload() and verifies the child-local SIGABRT behavior. The CTest
# entry is source-driven; add_compat_test uploads the source, compiles it with
# the guest CLT, and runs the resulting guest binary through the selected
# Darling prefix.
# ---------------------------------------------------------------------------
set(_abort_with_payload_guest
  "${_libsystem_kernel_tests}/abort_with_payload_no_group_broadcast.c")
if(EXISTS "${_abort_with_payload_guest}")
  add_compat_test(
    NAME       abort_with_payload_no_group_broadcast
    SOURCE     "${_abort_with_payload_guest}"
    ENVS       darling
    BEAD       dar-cps
    SUBMODULES xnu
    TIMEOUT    30
    OK_MARKER  "GREEN: abort_with_payload stayed local to the child; parent survived"
  )
else()
  message(WARNING
    "xnu checkout missing ${_abort_with_payload_guest}; skipping dar-cps")
endif()

# ---------------------------------------------------------------------------
# dar-q95.3 — source-driven guest regression for high-fd select/pselect.
#
# The fixture is maintained beside its patch metadata in this workspace, not in
# the xnu checkout.  add_compat_test() uploads and compiles it inside Darling,
# so registration needs no Linux build and the CTest case follows the deployed
# libsystem_kernel selected by the active prefix.
# ---------------------------------------------------------------------------
set(_select_fdset_guest "${_workspace_root}/tests/select_fdset_guest.c")
if(EXISTS "${_select_fdset_guest}")
  add_compat_test(
    NAME       select_fdset_guest
    SOURCE     "${_select_fdset_guest}"
    ENVS       darling
    BEAD       dar-q95.3
    SUBMODULES xnu
    TIMEOUT    30
    OK_MARKER  "SELECT_FDSET_GUEST_OK"
  )
else()
  message(WARNING
    "workspace missing ${_select_fdset_guest}; skipping dar-q95.3")
endif()

# ---------------------------------------------------------------------------
# dar-e1j — Ruby-style literal glob components depend on the public
# getattrlist(NAME|OBJTYPE) ABI.  This fixture creates regular, directory, and
# symlink entries in the guest, then verifies the packed names and vnode types;
# it is the focused behavioral root cause behind the original Dir.glob report.
# ---------------------------------------------------------------------------
set(_getattrlist_name_objtype_guest
  "${_workspace_root}/tests/getattrlist_name_objtype_guest.c")
if(EXISTS "${_getattrlist_name_objtype_guest}")
  add_compat_test(
    NAME       getattrlist_name_objtype_guest
    SOURCE     "${_getattrlist_name_objtype_guest}"
    ENVS       darling macos
    BEAD       dar-e1j
    SUBMODULES xnu
    RUNTIME_PROFILE homebrew-rootless-no-mount
    INSTALL
    SMOKE
    TIMEOUT    30
    OK_MARKER  "GETATTRLIST_NAME_OBJTYPE_GUEST_OK"
  )
else()
  message(WARNING
    "workspace missing ${_getattrlist_name_objtype_guest}; skipping dar-e1j")
endif()

set(_darwin_priority_guest "${_workspace_root}/tests/darwin_priority_guest.c")
if(EXISTS "${_darwin_priority_guest}")
  add_compat_test(
    NAME       darwin_priority_guest
    SOURCE     "${_darwin_priority_guest}"
    ENVS       darling
    BEAD       dar-q95.10
    SUBMODULES xnu
    TIMEOUT    30
    OK_MARKER  "DARWIN_PRIORITY_GUEST_OK"
  )
endif()

set(_socket_siocgifconf_guest "${_workspace_root}/tests/socket_siocgifconf_guest.c")
if(EXISTS "${_socket_siocgifconf_guest}")
  add_compat_test(
    NAME       socket_siocgifconf_guest
    SOURCE     "${_socket_siocgifconf_guest}"
    ENVS       darling
    BEAD       dar-q95.11
    SUBMODULES xnu
    TIMEOUT    30
    OK_MARKER  "SOCKET_SIOCGIFCONF_GUEST_OK"
  )
endif()

# dar-q95.4 changes libsystem_platform rather than libsystem_kernel.  Keep its
# guest verdict in CTest, but select the dedicated provider so the test never
# reads whichever platform dylib happened to be installed in the prefix.
set(_bzero_return_register_guest
  "${_workspace_root}/tests/bzero_return_register_guest.c")
if(EXISTS "${_bzero_return_register_guest}")
  add_compat_test(
    NAME       bzero_return_register_guest
    SOURCE     "${_bzero_return_register_guest}"
    ENVS       darling
    BEAD       dar-q95.4
    SUBMODULES libplatform
    RUNTIME_PROFILE homebrew-libplatform
    TIMEOUT    30
    OK_MARKER  "BZERO_RETURN_REGISTER_GUEST_OK"
  )
endif()

set(_simple_xnu_guest_cases
  "dar-q95.20|sigexc_sa_restart_guest|tests/sigexc_sa_restart_guest.c|SIGEXC_SA_RESTART_GUEST_OK"
  "dar-gwn.6.4|sigexc_default_resend_self_guest|tests/sigexc_default_resend_self_guest.c|SIGEXC_DEFAULT_RESEND_SELF_GUEST_OK"
  "dar-gwn.6|ulock_eintr_retry_guest|tests/ulock_eintr_retry_guest.c|ULOCK_EINTR_RETRY_GUEST_OK"
  "dar-gwn.1.6|vchroot_pathnull_guard_guest|tests/vchroot_pathnull_guard_guest.c|VCHROOT_PATHNULL_GUARD_GUEST_OK"
  "dar-gyvb|chown_disabled_null_guard_guest|tests/chown_disabled_null_guard_guest.c|CHOWN_DISABLED_NULL_GUARD_GUEST_OK"
  "dar-6x4.1|fd_guard_ebadf_guest|tests/fd_guard_ebadf_guest.c|FD_GUARD_EBADF_GUEST_OK")
foreach(_case IN LISTS _simple_xnu_guest_cases)
  string(REPLACE "|" ";" _case_fields "${_case}")
  list(GET _case_fields 0 _case_bead)
  list(GET _case_fields 1 _case_name)
  list(GET _case_fields 2 _case_source_rel)
  list(GET _case_fields 3 _case_marker)
  set(_case_source "${_workspace_root}/${_case_source_rel}")
  if(EXISTS "${_case_source}")
    add_compat_test(
      NAME       "${_case_name}"
      SOURCE     "${_case_source}"
      ENVS       darling
      BEAD       "${_case_bead}"
      SUBMODULES xnu
      TIMEOUT    30
      OK_MARKER  "${_case_marker}"
    )
  endif()
endforeach()

set(_fork_checkin_signal_storm_guest
  "${_workspace_root}/tests/fork_checkin_signal_storm_guest.c")
if(EXISTS "${_fork_checkin_signal_storm_guest}")
  add_compat_test(
    NAME       fork_checkin_signal_storm_guest
    SOURCE     "${_fork_checkin_signal_storm_guest}"
    ENVS       darling
    BEAD       dar-gwn.6.5
    SUBMODULES darlingserver
    RUNTIME_PROFILE perf-darlingserver
    TIMEOUT    45
    OK_MARKER  "FORK_CHECKIN_SIGNAL_STORM_OK"
  )
endif()

set(_rootless_no_mount_guest
  "${_workspace_root}/tests/rootless_no_mount_guest.c")
if(EXISTS "${_rootless_no_mount_guest}")
  add_compat_test(
    NAME       rootless_no_mount_guest
    SOURCE     "${_rootless_no_mount_guest}"
    ENVS       darling
    BEAD       dar-test-infra-sp5.8.4.5
    SUBMODULES darling darlingserver xnu
    RUNTIME_PROFILE homebrew-rootless-no-mount
    TIMEOUT    45
    OK_MARKER  "ROOTLESS_NO_MOUNT_GUEST_OK"
  )
endif()

# ---------------------------------------------------------------------------
# dar-gwn.5 — real regression for the mldr raw-fork glibc lock reset.
#
# Mirrors tests/regression/run-glibc-fork-lock-reset.sh exactly: the harness is
# linked against the REAL production reset (mldr/glibc_fork_reset.c) with the
# test-only accessors enabled, and dlopen()s a TLS-bearing module to exercise
# _dl_load_tls_lock. HOST test (plain glibc) — declared `guarded` so a real
# inherited-lock deadlock becomes a captured timeout, not a wedged run.
# ---------------------------------------------------------------------------
if(EXISTS "${_mldr}/glibc_fork_reset.c")
  # The TLS-bearing object the harness dlopen()s as ./tls_mod.so.
  add_library(tls_mod SHARED
    "${DARLING_SRC}/tests/regression/glibc_fork_lock_reset_tls.c")
  set_target_properties(tls_mod PROPERTIES
    PREFIX "" OUTPUT_NAME "tls_mod"            # -> tls_mod.so
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}")

  add_compat_test(
    NAME          glibc_fork_lock_reset
    SOURCE        "${DARLING_SRC}/tests/regression/glibc_fork_lock_reset.c"
    EXTRA_SOURCES "${_mldr}/glibc_fork_reset.c"
    INCLUDES      "${_mldr}"
    DEFINES       GLIBC_FORK_RESET_TEST_HOOKS
    LIBS          ${CMAKE_DL_LIBS} Threads::Threads
    ENVS          host
    BEAD          dar-gwn.5
    SUBMODULES    xnu
    DIAG          guarded
    WORKDIR       "${CMAKE_BINARY_DIR}"        # harness dlopen()s ./tls_mod.so
  )
  add_dependencies(compat.glibc_fork_lock_reset tls_mod)
else()
  message(WARNING
    "darling checkout not found at ${DARLING_SRC}; skipping dar-gwn.5 "
    "(set -DDARLING_SRC=/path/to/darling)")
endif()
