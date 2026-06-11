# Homebrew-under-Darling — upstream PR drafts

Local draft branches for the fixes landed while making Homebrew usable under
Darling (epic `dar-q95`). **These are drafts only — do NOT open the PRs yet.**

Each fix lives on its own clean branch off the submodule's `origin/main`,
carrying a single concern with the debugging/tracing scaffolding stripped out.
The full, messy investigation history (with `[PH]` tracing, experiments, and
reverted dead-ends) stays on the `fix/homebrew-psynch-ruby-hang` branch in each
submodule as an archive.

## PR set

| beads | repo | branch | base | concern | branch status |
|-------|------|--------|------|---------|---------------|
| dar-q95.1  | darlingserver | `fix/psynch-cvwait-balanced`   | `89751e6` | cvwait `is_seqhigher` | **created** |
| dar-q95.8  | darlingserver | `fix/microthread-resume-race` | `89751e6` | resume-before-suspend lost wakeup | **created** |
| dar-q95.13 | darlingserver | `fix/fork-checkin-bound`       | `89751e6` | bounded fork-child checkin wait | **created** |
| dar-q95.15 | darlingserver | not split yet | upstream | cancel stale wait_timer in `thread_unblock()` | **needs split** |
| dar-q95.2  | xnu | `fix/psynch-cvsignal-args`  | `5f26a4c` | cvsignal/cvbroad arg fixes | **created** |
| dar-q95.3  | xnu | `fix/select-pselect-fdset` | `5f26a4c` | BSD↔Linux fd_set conversion | **created** |
| dar-q95.10 | xnu | `fix/darwin-priority`      | `5f26a4c` | PRIO_DARWIN_THREAD/PROCESS | **created** |
| dar-q95.11 | xnu | `fix/socket-siocgifconf`   | `5f26a4c` | SIOCGIFCONF stub | **created** |
| dar-q95.12 | xnu | `fix/fork-postfork-child`  | `5f26a4c` | `__mldr_postfork_child()` | **created** |
| dar-q95.17 | xnu | not split yet | `5f26a4c` | getattrlist `ATTR_CMN_NAME` + `ATTR_CMN_OBJTYPE` | **needs split** |
| dar-q95.18 | xnu | not split yet | `5f26a4c` | getattrlistbulk implementation | **needs split** |
| dar-q95.19 | xnu | not split yet | `5f26a4c` | psynch wait negative errno returns | **needs split** |
| dar-q95.20 | xnu | not split yet | `5f26a4c` | `SA_RESTART` in sigexc path | **needs split** |
| dar-q95.14 | top-level darling | `fix/homebrew-psynch-ruby-hang` | upstream | mldr glibc loader-lock reset | **created** |
| dar-q95.16 | libpthread | not split yet | upstream | decode negative psynch wait returns | **needs split** |
| dar-q95.21 | top-level darling | not split yet | upstream | `sandbox-exec` pass-through | **needs split** |
| dar-q95.22 | top-level darling | not split yet | upstream | SDKSettings + `MacOSX11.sdk` symlink | **needs split** |
| dar-q95.4  | libplatform | `fix/bzero-return-register`      | — | preserve bzero dest ptr (x86_64) | pre-existing (draft PR darling-libplatform#5) |
| dar-q95.5  | perl | `fix/disable-nsgetexecutablepath` | — | disable `_NSGetExecutablePath` | pre-existing |
| dar-q95.6  | libressl-2.8.3 | `fix/libressl-283-nist-strict-aliasing` | — | `-fno-strict-aliasing` (NIST ECC) | pre-existing (submodule may need init) |
| dar-q95.7  | top-level darling | (submodule pointer bump) | — | bump pointers after sub-PRs land | blocked on the above |

## Architecture follow-ups

| beads | scope | concern | status |
|-------|-------|---------|--------|
| dar-q95.23 | psynch ABI layering | decide whether psynch wait errors should be normalized at libsystem_kernel to macOS-style `-1 + errno`, or intentionally exposed as negative BSD errno values with libpthread decoding as a compatibility backstop | **open** |

Upstream repos (submodule URLs are relative `../<repo>.git`, current `origin`
is the IlyaGulya fork):
`darlinghq/darlingserver`, `darlinghq/darling-xnu`, `darlinghq/darling-libplatform`,
`darlinghq/darling-perl`, `darlinghq/darling-libressl`.

## Suggested ordering

darlingserver and xnu PRs are independent of each other and can go in any
order. The top-level submodule-pointer bump (dar-q95.7) must come last, after
the submodule PRs are settled.

Resolve `dar-q95.23` before opening the paired psynch negative-return PRs
(`dar-q95.16` and `dar-q95.19`). Those two drafts may shrink or change shape if
the root-correct answer is to normalize the ABI below libpthread.

## Patch vs root-cause audit

- `mldr` glibc fork reset (`dar-q95.14`) is not a random workaround. Darling
  uses raw `__NR_fork`, bypassing glibc's normal child-side `fork()` cleanup, so
  mldr must explicitly restore the critical host-runtime child resets it skips.
  The broader rule for future review: any host libc/runtime child reset skipped
  by Darling's raw-fork path needs an explicit mldr equivalent.
- psynch negative returns (`dar-q95.16`, `dar-q95.19`) are the main unresolved
  layering question. The current local fix works around negative BSD errno
  values reaching libpthread, but `dar-q95.23` tracks whether the deeper fix is
  to normalize libsystem_kernel to Darwin's `-1 + errno` ABI instead.
- `sandbox-exec` pass-through (`dar-q95.21`) is only a compatibility shim for
  build systems that expect the command to exist. It must not be presented as a
  sandbox implementation.
- `SIOCGIFCONF` empty-list handling (`dar-q95.11`) and fork-checkin timeout
  (`dar-q95.13`) are pragmatic compatibility/robustness fixes, not complete
  implementations of interface enumeration or fork lifecycle recovery.
- `wait_timer` cancellation, microthread resume coalescing, `getattrlist`,
  `getattrlistbulk`, `fd_set` conversion, cvsignal/cvbroad arguments, and
  `SA_RESTART` look like semantic parity fixes rather than symptom masks.

## Excluded from every PR branch (debug-only — archived on `fix/homebrew-psynch-ruby-hang`)

- darlingserver `duct-tape/src/thread.c` — `[PH]` `dtape_log_error` tracing.
- darlingserver `duct-tape/pthread/kern_synch.c` — `KWQDBG` tracing.
- darlingserver `src/call.cpp` — `CALLDBG` tracing.
- xnu `signal/sigexc.c` — enriched abort diagnostics + `DEBUG_SIGEXC` toggling.
- xnu `mach/impl/mach_traps.c` — verbose `semaphore_timedwait` abort printf.
- xnu `libsyscall/mach/mach_init.c` — `__builtin_unreachable()` → printf+abort
  on `fork_wait_for_child` failure (diagnostic; reconsider as a separate
  robustness PR if wanted).
- xnu emulation `CMakeLists.txt` trailing-newline churn; libunwind/dyld
  `CMakeLists.txt` tweaks.
- xnu `fork.c` — the enriched "Failed to checkin" printf hunk is dropped; only
  the `__mldr_postfork_child()` call ships in `fix/fork-postfork-child`.

## brew install status

The psynch / lost-timer / suspend-resume hang is **fixed and validated**
(`dar-gwn.1.4`). After the mldr glibc fork-reset hardening, a focused
Homebrew source-build smoke passed:

- `brew install --build-from-source lz4`: 3/3 iterations, `ALL-PASS`
  (`/home/ilyagulya/work/darling-debug/20260611T132529Z-lz4-glibc-fork-reset-e2e-3x`)

This validates the current PR set against the old mldr `futex_wait_queue` hang.
A longer 10-iteration run made two successful source-build iterations and then
hit a separate Ruby `pthread_cond_wait: ETIMEDOUT` abort in
`download_queue.rb:184`; keep that separate from the raw-fork loader-lock PR.
