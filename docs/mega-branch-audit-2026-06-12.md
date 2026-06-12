# Darling mega-branch migration audit

Date: 2026-06-12

## Scope

This audit covers work reachable from:

- top-level `fix/homebrew-psynch-ruby-hang` at
  `5fb98a598737725fc1a70f1b792e76d36c856369`;
- top-level `backup/local-snapshot-20260609` at
  `663a2d6b04115b5c580db7866d23ecc461b5ea5d`;
- the corresponding historical refs in Darling subrepositories;
- uncommitted source changes found during migration;
- current clean branches, `patches/homebrew`, PR drafts, and handoff bundles.

The canonical editable sources remain clean `fix/*` branches. Historical
mega-branches and bundles are archives, not publishable branches.

## Result

The Homebrew profile contains 20 extracted fixes. Tree-level comparison found
one migration defect: `fix/mldr-glibc-fork-reset` omitted the `elf_calls`
`postfork_child` field and initializer which connect mldr to the xnu fork-child
hook. The clean branch was amended to
`d90fe8f2a949bcf51f5d4a5aed6ce3fd3a7ef98f`, its patch was regenerated, and
the PR draft was corrected.

No other product change in the final darlingserver, xnu, or libpthread mega
trees is missing from the clean branches:

- darlingserver differs from the four clean fixes only by eight `[PH]`
  tracing calls in `duct-tape/src/thread.c`;
- xnu differs from the nine clean fixes only by diagnostic logging, disabled
  signal tracing, and whitespace;
- libpthread differs from `fix/psynch-negative-returns` only in
  `kern/kern_synch.c`, an XNU kernel source not included by Darling's CMake
  build. The equivalent active implementation is the darlingserver fix
  `fix/psynch-cvwait-balanced`.

## Extracted fixes

| Area | Clean branch | Bead | Disposition |
| --- | --- | --- | --- |
| darlingserver cvwait validation | `fix/psynch-cvwait-balanced` | `dar-q95.1` | keep |
| darlingserver resume race | `fix/microthread-resume-race` | `dar-q95.8` | keep |
| darlingserver fork checkin bound | `fix/fork-checkin-bound` | `dar-q95.13` | keep |
| darlingserver stale wait timer | `fix/cancel-stale-wait-timer` | `dar-q95.15` | keep |
| xnu cvsignal arguments | `fix/psynch-cvsignal-args` | `dar-q95.2` | keep |
| xnu select fd sets | `fix/select-pselect-fdset` | `dar-q95.3` | keep |
| xnu Darwin priorities | `fix/darwin-priority` | `dar-q95.10` | keep |
| xnu SIOCGIFCONF compatibility | `fix/socket-siocgifconf` | `dar-q95.11` | keep |
| xnu post-fork mldr hook | `fix/fork-postfork-child` | `dar-q95.12` | keep |
| xnu getattrlist | `fix/getattrlist-name-objtype` | `dar-q95.17` | keep |
| xnu getattrlistbulk | `fix/getattrlistbulk` | `dar-q95.18` | keep |
| xnu negative psynch errno | `fix/psynch-negative-errno` | `dar-q95.19` | keep |
| xnu SA_RESTART | `fix/sigexc-sa-restart` | `dar-q95.20` | keep |
| libplatform bzero return | `fix/bzero-return-register` | `dar-q95.4` | keep |
| Perl executable path config | `fix/disable-nsgetexecutablepath` | `dar-q95.5` | keep |
| LibreSSL strict aliasing | `fix/libressl-283-nist-strict-aliasing` | `dar-q95.6` | keep |
| libpthread negative psynch returns | `fix/psynch-negative-returns` | `dar-q95.16` | keep |
| mldr raw-fork reset and bridge | `fix/mldr-glibc-fork-reset` | `dar-q95.14` | keep |
| sandbox-exec compatibility | `fix/sandbox-exec-pass-through` | `dar-q95.21` | keep |
| SDK Homebrew detection | `fix/sdk-homebrew-detection` | `dar-q95.22` | keep |

## Residual repository changes

### dyld: investigate, do not publish yet

Historical commit:
`a9c2e2922a9fa6226f591fe9101bcbc706cf6dcd`.

It adds `-Wl,-u,_elfcalls` to `system_loader`, which is linked with
`-dead_strip`. The change may be necessary to retain the `_elfcalls` object
used by `__dyld_get_elfcalls`, but the historical commit has no build or
runtime proof. It remains archived in
`handoff/src__external__dyld.bundle`.

Disposition: investigate under `dar-q95.26`. Extract only after an A/B build,
symbol-table check, and runtime lookup test.

### objc4: discard this implementation, retain the intent

Historical commit:
`ec7d97e9c95ab298af3d2ee4c898b9ce0866c9eb`.

It changes `OBJC_IS_DEBUG_BUILD` from always `1` to `1` for Debug and `0`
otherwise. The intent is plausible, but this implementation is not safe:
`runtime/objc-internal.h` tests `defined(OBJC_IS_DEBUG_BUILD)`, so defining it
to `0` still exposes the debug-only declaration. It also needs verification
against the `DEBUG != OBJC_IS_DEBUG_BUILD` assertion.

Disposition: do not extract the historical commit. Correct and validate the
macro contract under `dar-q95.27`.

### zlib: drop

Historical commit:
`ada74d8636d868f9ca609cea5ead2254610081ab`.

It disables `VEC_OPTIMIZE` and adds a process-global, unlocked allocation list
so `zcfree()` can accept interior pointers. This hides an invalid-free caller
bug, is not thread-safe, adds allocation bookkeeping to every zlib allocation,
and can still fail to record allocations. It is not suitable for upstream or
the local integration profile.

Disposition: drop. The commit remains recoverable from
`handoff/src__external__zlib.bundle`.

### libunwind: drop

Historical commit:
`57781ec72f7a426a809c2a807ce83d98dd3f5ee0`.

It adds `-O0` to `unwind_static`, while the repository-wide C and C++ flags
already contain `-O0`. It is redundant debug configuration without a
separable behavior fix.

Disposition: drop. The commit remains recoverable from
`handoff/src__external__libunwind.bundle`.

### top-level build/install changes: investigate separately

Commit `6343ce9c28d0e53e98a2b7ffee3dcc508ea3ea50` also contains:

- removal of an existing fat static archive before `PRE_LINK`;
- creation of `System/Library/LaunchDaemons` during install;
- passing the installed Darling executable to `shutdown-user.sh`;
- preserving that executable's directory across `sudo`/`su`.

These are plausible build/install fixes, but they were committed together with
debug tooling and experimental submodule pointers and have no independent
reproduction in the migration record.

Disposition: archived pending independent validation under `dar-q95.28`.

## Diagnostics and reproducers

The Rust runner embedded under `tools/darling-debug-runner` was extracted to
the private `darling-debug-runner` repository. Its standalone history includes
the embedded state through `9ed5c811c` and additional cleanup through
`c85f1bd1e`; therefore the embedded copy is superseded.

The following remain useful investigation assets but are not product patches:

- Ruby psynch/select/SA_RESTART regression scripts under `tests/regression`;
- DarlingServer resume/timer A/B reproducers under `tools/repro-*`;
- `tools/gdb_maloader.py` and `tools/ruby-thread-watchdog.rb`;
- legacy shell capture, cleanup, SSH, and summary helpers.

They remain recoverable from `handoff/root.bundle`. Their final home beside the
standalone runner or in a diagnostics archive is tracked by `dar-l76`. The
legacy shell runner is superseded by the Rust runner and must not be restored
into the Darling source tree as workspace tooling.

The uncommitted darlingserver `KWQDBG`/`CALLDBG` tracing was preserved exactly
as `diagnostics/darlingserver-kwq-call-tracing.patch`, SHA-256
`f7f972989b720e9a0ac300db35859e413b871ed5fe08a59c724eb6c2bf9d14aa`.
The final mega-tree's remaining `[PH]` tracing and xnu diagnostic messages are
also archive-only.

## Superseded experiments

- DarlingServer commit `8ff03f000541eb93951d407e9f23fd7d1596605b`
  implemented a dedicated timer-expiry workaround. It was removed after
  `fix/microthread-resume-race` repaired the shared scheduling race. It has no
  net product diff in the final mega-tree.
- DarlingServer commits `a16975b6` and `97faab5c` add and then remove a
  disproven experiment. Only diagnostic logging survives.
- Top-level submodule-pointer-only commits are historical integration
  snapshots. They are replaced by `patches/homebrew/patches.yml` and must not
  become PR branches.
- The libpthread `kern/kern_synch.c` cvwait change is inactive in Darling's
  CMake build and duplicates the active darlingserver fix.
- `docs/darling-docs`, `.gdbinit`, and the old debug helpers are workspace
  conveniences, not Homebrew runtime fixes.

## Preservation and retirement

The current handoff manifest records the historical mega refs and all clean
branches. Bundle verification succeeds when each bundle is checked from its
owning repository. Source worktrees are clean; workspace metadata exists only
in `darling-workspace`.

The historical mega branches may be deleted from normal local branch lists
after this report and refreshed handoff bundles are pushed. Keep the bundles
until all of these are resolved:

- `dar-q95.26` dyld retention validation;
- `dar-q95.27` objc4 macro validation;
- `dar-q95.28` top-level build/install validation;
- `dar-l76` repro asset placement.

Deleting the refs is optional cleanup. They must never be used as PR heads or
as the integration source of truth.
