# SPDX-License-Identifier: MIT-0
#
# add_compat_test() — generator for Darling compatibility regression tests.
#
# Why a generator instead of plain add_test(): the same source must be runnable
# in more than one ENVIRONMENT (host glibc / Darling guest / — later — native
# macOS), exactly like gVisor stamps `_native`, `_runsc_systrap`, ... targets out
# of one cc_test, and like Wine builds one test source for both Wine and real
# Windows. CTest has no native "one test, N environments" concept, so we mint one
# ctest entry per environment here and tag each with labels the `west test`
# orchestrator consumes (env:*, bead:*, submod:*).
#
#   add_compat_test(
#     NAME          glibc_fork_lock_reset    # logical case name
#     SOURCE        regression/glibc_fork_lock_reset.c
#     ENVS          host                     # host;darling;macos (one ctest entry each)
#     BEAD          dar-gwn.5                # owning issue -> label bead:dar-gwn.5
#     SUBMODULES    xnu                      # code this covers -> label submod:xnu
#     FUZZ                                   # label fuzz:true
#     STRESS                                 # label stress:true
#     DIAG          guarded                  # bare|guarded|forensic (see below)
#     TIMEOUT       60                       # per-test seconds (default 60)
#     WILL_FAIL                              # negative case: non-zero == pass
#     # --- building a case against the REAL production code: ---
#     EXTRA_SOURCES src/.../production.c      # compiled WITH the harness
#     INCLUDES      src/.../include_dir       # added to the include path
#     DEFINES       SOME_TEST_HOOK            # -D added to the compile
#     LIBS          ${CMAKE_DL_LIBS}          # linked in
#     WORKDIR       ${CMAKE_BINARY_DIR}       # cwd at run time (relative dlopen etc.)
#     ARGS          --fixture-mode            # argv appended to the test binary
#     # --- macOS version axis + Tommy-compatible shipping: ---
#     MIN_VERSION   13.0                      # OSX_DEPLOYMENT_TARGET + macos:<v> label
#     MAX_VERSION   15.0                      # upper bound of the macos:<min>-<max> label
#     INSTALL                                 # emit install(TARGETS->testcase/) like upstream
#     RESOURCES     resources                 # dir(s) installed under resource/<NAME>/
#   )
#
# Compatibility stance: this generator is a convenience SUPERSET of what the
# upstream darling-testsuite writes by hand (add_executable + target_link_libraries
# + add_test + install). Case sources, availability.h usage, and the INSTALL
# layout (testcase/ + resource/) match upstream, so a case authored here ports
# upstream unchanged; the extra ergonomics (envs, diag tiers, labels) live only
# in our tree and in `west test`.
#
# Diagnosis tiers (DIAG) — the flexible knob that keeps fast runs fast and the
# bundle dir from ballooning (a real prefix accumulated 7.4G / 980 bundles from
# unbounded forensic capture):
#
#   bare      no executor at all — plain ctest exec. Zero overhead, zero disk.
#             Right for stable HOST tests.
#   guarded   executor as a WATCHDOG only: hard timeout + process-group kill.
#             A small text bundle (~20K: cmd/exit/stdout/stderr) is written
#             ONLY on failure/hang. No gdb, no /proc, no rpctrace. Near-zero
#             overhead on green; survives a runtime hang (we observed a bare
#             `darling shell echo` stall indefinitely). Right for env=darling.
#   forensic  full capture: gdb backtrace + /proc snapshot + (opt-in) rpctrace.
#             Expensive and large — opt-in per case or via `west test --diag
#             forensic`. This is what produced the 7.4G; never a mass default.
#
# Default DIAG by environment when unset: host/macos -> bare, darling -> guarded.
# The executor binary is supplied at configure time via DARLING_TEST_EXECUTOR;
# if a non-bare tier is requested but no executor is configured, the test falls
# back to bare (with a warning) so the suite still runs. Darling guest C tests
# are SOURCE-driven: the local suite uploads the C source, compiles it with the
# guest CLT inside the prefix, then runs the guest binary. Do not run Linux host
# test binaries under `darling shell`.

set(_ADD_COMPAT_TEST_CMAKE_DIR "${CMAKE_CURRENT_LIST_DIR}")
get_filename_component(_ADD_COMPAT_TEST_ROOT
  "${_ADD_COMPAT_TEST_CMAKE_DIR}/.." ABSOLUTE)
set(DARLING_SHELL "${DARLING_SHELL}" CACHE STRING
  "Deprecated command list for Darling shell; prefer DARLING_LAUNCHER")
set(DARLING_LAUNCHER "${DARLING_LAUNCHER}" CACHE FILEPATH
  "Path to the darling launcher used for env=darling CTest entries")
set(DARLING_TEST_PREFIX "${DARLING_TEST_PREFIX}" CACHE PATH
  "Darling prefix exported to env=darling tests as DPREFIX/DARLING_PREFIX")
set(DARLING_TEST_BUNDLE_ROOT "${DARLING_TEST_BUNDLE_ROOT}" CACHE PATH
  "Debug bundle root passed to darling-debug-runner for guarded/forensic tests")

function(add_compat_test)
  set(options INSTALL FUZZ STRESS)
  set(oneValue NAME SOURCE BEAD DIAG WORKDIR TIMEOUT MIN_VERSION MAX_VERSION OK_MARKER EXPECT_FAILURE_MARKER)
  set(multiValue ENVS SUBMODULES EXTRA_SOURCES INCLUDES DEFINES LIBS RESOURCES ARGS)
  cmake_parse_arguments(ACT "${options}" "${oneValue}" "${multiValue}" ${ARGN})

  if(NOT ACT_NAME OR NOT ACT_SOURCE)
    message(FATAL_ERROR "add_compat_test: NAME and SOURCE are required")
  endif()
  if(NOT ACT_ENVS)
    set(ACT_ENVS host)
  endif()
  foreach(env IN LISTS ACT_ENVS)
    if(NOT env MATCHES "^(host|darling|macos)$")
      message(FATAL_ERROR "add_compat_test(${ACT_NAME}): unknown ENV '${env}' "
        "(expected host|darling|macos)")
    endif()
  endforeach()
  if(ACT_DIAG AND NOT ACT_DIAG MATCHES "^(bare|guarded|forensic)$")
    message(FATAL_ERROR "add_compat_test: DIAG must be bare|guarded|forensic")
  endif()
  if(NOT ACT_TIMEOUT)
    set(ACT_TIMEOUT 60)
  endif()
  if(ACT_WILL_FAIL)
    message(FATAL_ERROR
      "add_compat_test(${ACT_NAME}): WILL_FAIL is unsupported because it accepts "
      "an arbitrary failure; use EXPECT_FAILURE_MARKER instead")
  endif()

  # One built executable per host/macos case (shared across those environments).
  # env=darling is source-driven and compiles inside the guest prefix instead.
  set(_needs_local_target FALSE)
  foreach(env IN LISTS ACT_ENVS)
    if(env STREQUAL "host" OR env STREQUAL "macos")
      set(_needs_local_target TRUE)
    endif()
  endforeach()
  set(target "compat.${ACT_NAME}")
  if(_needs_local_target)
    # EXTRA_SOURCES lets a case compile the REAL production code under test
    # alongside its harness (e.g. mldr/glibc_fork_reset.c) instead of a copy, so
    # the test tracks the code it guards.
    add_executable("${target}" "${ACT_SOURCE}" ${ACT_EXTRA_SOURCES})
    if(ACT_INCLUDES)
      target_include_directories("${target}" PRIVATE ${ACT_INCLUDES})
    endif()
    if(ACT_DEFINES)
      target_compile_definitions("${target}" PRIVATE ${ACT_DEFINES})
    endif()
    if(ACT_LIBS)
      target_link_libraries("${target}" PRIVATE ${ACT_LIBS})
    endif()
  elseif(ACT_EXTRA_SOURCES OR ACT_INCLUDES OR ACT_DEFINES OR ACT_LIBS)
    message(FATAL_ERROR
      "add_compat_test(${ACT_NAME}): env=darling source-driven tests do not "
      "support EXTRA_SOURCES/INCLUDES/DEFINES/LIBS yet")
  endif()

  # macOS version axis (build-once-run-many). MIN_VERSION sets the deployment
  # target so the binary runs on that macOS and every newer one (macOS binaries
  # are forward-compatible). It also drives __MAC_OS_X_VERSION_MIN_REQUIRED, so a
  # case can gate expectations with upstream availability.h's
  # MIN_VERSION_MACOS_ABI_TARGET_SUPPORTED(min,max). MIN/MAX_VERSION become the
  # macos:<min>-<max> label; the live OS the test runs on is the comparison axis,
  # not a rebuild. (No-op off Apple platforms — host/darling ignore it.)
  if(_needs_local_target AND ACT_MIN_VERSION AND APPLE)
    set_target_properties("${target}" PROPERTIES
      OSX_DEPLOYMENT_TARGET "${ACT_MIN_VERSION}")
  endif()

  # Tommy-compatible install layout: same DESTINATION names the upstream suite
  # uses (testcase/ + resource/), so a case authored here drops into a build
  # that is shipped to real macOS / Darling and run there. Opt-in via INSTALL.
  if(ACT_INSTALL AND _needs_local_target)
    if(NOT DEFINED INSTALL_DIR_TESTCASE)
      set(INSTALL_DIR_TESTCASE "testcase")  # match upstream default
    endif()
    install(TARGETS "${target}" DESTINATION "${INSTALL_DIR_TESTCASE}")
    if(ACT_RESOURCES)
      if(NOT DEFINED INSTALL_DIR_RESOURCE)
        set(INSTALL_DIR_RESOURCE "resource")
      endif()
      install(DIRECTORY ${ACT_RESOURCES}
        DESTINATION "${INSTALL_DIR_RESOURCE}/${ACT_NAME}")
    endif()
  endif()

  foreach(env IN LISTS ACT_ENVS)
    set(test_name "${env}/${ACT_NAME}")

    # Per-environment launch command.
    if(env STREQUAL "host")
      set(cmd "$<TARGET_FILE:${target}>" ${ACT_ARGS})
    elseif(env STREQUAL "darling")
      if(DARLING_LAUNCHER)
        set(guest_marker_args)
        if(ACT_OK_MARKER)
          list(APPEND guest_marker_args --ok-marker "${ACT_OK_MARKER}")
        endif()
        set(cmd
          "${_ADD_COMPAT_TEST_ROOT}/scripts/run-darling-c-test.sh"
          --name "${ACT_NAME}"
          --source "${ACT_SOURCE}"
          --launcher "${DARLING_LAUNCHER}"
          ${guest_marker_args}
          -- ${ACT_ARGS})
      else()
        set(cmd
          "${CMAKE_COMMAND}"
          "-DTEST_NAME=${test_name}"
          -P "${_ADD_COMPAT_TEST_CMAKE_DIR}/MissingDarlingShell.cmake")
      endif()
    elseif(env STREQUAL "macos")
      if(APPLE)
        set(cmd "$<TARGET_FILE:${target}>" ${ACT_ARGS})
      else()
        # A Linux-built binary is not a macOS oracle. Keep the registration
        # discoverable, but fail plainly until a remote macOS transport exists.
        set(cmd
          "${CMAKE_COMMAND}"
          "-DTEST_NAME=${test_name}"
          -P "${_ADD_COMPAT_TEST_CMAKE_DIR}/MissingMacOSRunner.cmake")
      endif()
    endif()

    # Resolve the diagnosis tier: explicit DIAG wins, else per-env default.
    set(diag "${ACT_DIAG}")
    if(NOT diag)
      if(env STREQUAL "darling")
        set(diag "guarded")  # guest can hang at the runtime level -> watchdog
      else()
        set(diag "bare")     # host/macos are stable + fast -> no wrapper
      endif()
    endif()

    # Executor indirection (the part neither ctest nor upstream has). bare uses
    # no wrapper. guarded/forensic prefix the executor; if none is configured we
    # degrade to bare so the suite still runs.
    if(NOT diag STREQUAL "bare")
      if(DARLING_TEST_EXECUTOR)
        set(exec_args run --name "${test_name}" --timeout-seconds ${ACT_TIMEOUT})
        if(DARLING_TEST_BUNDLE_ROOT)
          list(APPEND exec_args --bundle-root "${DARLING_TEST_BUNDLE_ROOT}")
        endif()
        if(diag STREQUAL "forensic")
          # Full capture: gdb backtrace + whole process tree. Expensive/large;
          # opt-in only. (rpctrace stays off even here unless asked separately.)
          list(APPEND exec_args --capture-gdb --capture-tree)
        endif()
        set(cmd ${DARLING_TEST_EXECUTOR} ${exec_args} -- ${cmd})
      else()
        message(WARNING
          "add_compat_test(${test_name}): DIAG=${diag} requested but "
          "DARLING_TEST_EXECUTOR is unset; falling back to bare")
        set(diag "bare")
      endif()
    endif()

    # CTest's WILL_FAIL only inverts the exit status. A RED case instead needs
    # a specific observed symptom so unrelated launcher/build failures cannot
    # be accepted as regression evidence.
    if(ACT_EXPECT_FAILURE_MARKER)
      set(cmd
        "${_ADD_COMPAT_TEST_ROOT}/scripts/expect-failure.sh"
        --marker "${ACT_EXPECT_FAILURE_MARKER}"
        -- ${cmd})
    endif()

    add_test(NAME "${test_name}" COMMAND ${cmd})
    if(ACT_WORKDIR)
      set_property(TEST "${test_name}" PROPERTY WORKING_DIRECTORY "${ACT_WORKDIR}")
    endif()
    if(env STREQUAL "darling" AND DARLING_TEST_PREFIX)
      set_property(TEST "${test_name}" APPEND PROPERTY ENVIRONMENT
        "DPREFIX=${DARLING_TEST_PREFIX}"
        "DARLING_PREFIX=${DARLING_TEST_PREFIX}")
    endif()

    set(labels "env:${env}" "diag:${diag}")
    if(ACT_BEAD)
      list(APPEND labels "bead:${ACT_BEAD}")
    endif()
    foreach(sm IN LISTS ACT_SUBMODULES)
      list(APPEND labels "submod:${sm}")
    endforeach()
    if(ACT_FUZZ)
      list(APPEND labels "fuzz:true")
    endif()
    if(ACT_STRESS)
      list(APPEND labels "stress:true")
    endif()
    # Version-range label so `west test --label macos:13.0` (or a CI matrix row)
    # can select the slice a case applies to. Darling reports one fixed version
    # (its SystemVersion.plist), so its run is compared against the matching row.
    if(ACT_MIN_VERSION)
      set(_vr "macos:${ACT_MIN_VERSION}")
      if(ACT_MAX_VERSION)
        set(_vr "${_vr}-${ACT_MAX_VERSION}")
      endif()
      list(APPEND labels "${_vr}")
    endif()

    set_property(TEST "${test_name}" PROPERTY LABELS ${labels})
    set_property(TEST "${test_name}" PROPERTY TIMEOUT ${ACT_TIMEOUT})
  endforeach()
endfunction()
