# xnu: Guard against NULL path in vchroot-based path syscalls

**Bead:** dar-gwn.1.6
**Branch:** `fix/vchroot-pathnull-guard` (base `preupstream/main`)
**Status:** ready (independent of E-UNION; baseline bug)

## Problem

A forked child building a Homebrew formula under Darling intermittently
SIGSEGVs during dyld two-level-namespace symbol binding. The fault is a
genuine `strcpy(dst, NULL)` → `strlen(NULL)`:

```
_platform_strlen+0xa4   (movq (%rax,%rcx),%r8 with rax=0; fault addr=0x0, SEGV_MAPERR)
 <- _stpcpy+0x18                         [libsystem_c]
 <- ___strcpy_chk+0x18                   [libsystem_c]   == strcpy(dst, NULL)
 <- sys_lstat64()                        [libsystem_kernel emulation]
 <- dyld ImageLoaderMachOCompressed::doBind / eachBind / instantiateFromCache
 <- libdyld start
```

`sys_lstat64()` copies its `path` argument into the vchroot expand buffer
with `strcpy(vc.path, path)` **before any NULL check**. dyld's bind path
calls `lstat64(NULL)`, so `strcpy(vc.path, NULL)` faults.

`sys_lstat()` (the 32-bit sibling) already has `if (!path) return -EFAULT;`,
but the guard was applied **inconsistently**: 15 path-taking syscalls in the
emulation layer do `strcpy(vc.path, path)` (and `linkat` also
`strcpy(vc2.path, link)`) with no NULL check. Each is a latent
`strcpy(NULL)` crash reachable from any guest passing a NULL path.

## Fix

Add the existing `if (!path) return -EFAULT;` guard (verbatim style of the
already-guarded functions) to every unguarded site:

`lstat64`, `statfs64`, `utimes`, `chdir`, `chmod_extended`, `fchmodat`,
`fchownat`, `lchown`, `linkat` (guards `path` **and** `link`), `mknod`,
`readlinkat`, `truncate`, `unlinkat`, `getxattr`, `listxattr`.

`EFAULT` is the documented errno for a bad path pointer and matches the
existing guards + POSIX. Files that did not already include `<sys/errno.h>`
get the include (mirroring the guarded siblings such as `mkdirat.c`).

## Scope notes

- **Not** an E-UNION change — these are baseline path syscalls; the fix
  applies cleanly on `preupstream/main`.
- `setxattr`/`removexattr` use `strcpy(vc.path, path)` **only under the
  E-UNION xattr-isolation patch**; at baseline they pass `path` straight to
  the Linux syscall (no strcpy), so they are not part of this patch. If/when
  the E-UNION xattr patch lands, it should carry its own guard.

## Verification

- `emulation_dyld` builds clean (the 6 EFAULT-undeclared errors from the
  missing `<sys/errno.h>` includes were the only compile issues; resolved).
- Each changed object (`lstat.c.o`, `statfs.c.o`, `utimes.c.o`, `linkat.c.o`,
  …) recompiles without error. `_sys_lstat64` disassembly confirms the guard:
  `testq %rdi,%rdi; je → movq $-0xe,%rax` (early `-EFAULT` return) precedes the
  `strcpy`/`___strcpy_chk` call.
- Live-verified in a Darling guest on the deployed dylib: a forked guest child
  (which performs dyld two-level-namespace binding on launch — the original
  crash trigger) calling `lstat/stat/chdir/truncate/readlink/unlink` with a
  NULL path now returns `-1 / EFAULT (errno 14)` for every one instead of
  `SIGSEGV`. Negative control: the same path on the pre-fix dylib SIGSEGVs in a
  from-source build child. (errno 14 = `EFAULT` = the guard's `-0xe`.)
