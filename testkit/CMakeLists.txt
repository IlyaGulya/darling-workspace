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

find_package(Threads REQUIRED)

# Locate the darling source checkout. Defaults to the sibling layout
# (workspace/.. /darling); override with -DDARLING_SRC=/path/to/darling.
if(NOT DEFINED DARLING_SRC)
  get_filename_component(_ws_parent "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
  set(DARLING_SRC "${_ws_parent}/darling")
endif()

# ---------------------------------------------------------------------------
# dar-dar6x4-perf-5dq.1 — shell-gate migration for the mldr checkin wait.
#
# The original Darling regression script builds thread_create_checkin_wait.c,
# proves the old sched_yield spin path fails, then runs the futex/adaptive wait
# path. Register the same source in CTest directly: one normal GREEN entry and
# one WILL_FAIL RED arm selected by argv `spin`.
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
    WILL_FAIL
  )
else()
  message(WARNING
    "darling checkout missing ${_thread_checkin_wait}; skipping "
    "dar-dar6x4-perf-5dq.1")
endif()
set(_mldr "${DARLING_SRC}/src/startup/mldr")
set(_libsystem_kernel_tests
  "${DARLING_SRC}/src/external/xnu/darling/src/libsystem_kernel/tests")

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
  )
else()
  message(WARNING
    "xnu checkout missing ${_abort_with_payload_guest}; skipping dar-cps")
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
