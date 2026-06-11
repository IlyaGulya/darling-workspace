# PR draft - libplatform: preserve bzero return register on x86_64

- **beads:** dar-q95.4
- **repo:** darlinghq/darling-libplatform
- **branch:** `fix/bzero-return-register`
- **existing PR:** https://github.com/darlinghq/darling-libplatform/pull/5

## Title
Preserve bzero return register on x86_64

## Body
## Why

Darling-built Perl 5.28 can compile `return memset(ptr, 0, len)` into a tail
call to `___bzero`. The caller still expects the original destination pointer
as the expression result. On x86_64, Darling's generic `___bzero` path
performed the zeroing but left the return register with the value from the
internal helper call, so callers could observe a shifted pointer.

This showed up while running Command Line Tools package scripts:
`perl_alloc()` returned an invalid interpreter pointer after zeroing the newly
allocated interpreter structure.

## What Changed

- Preserve the original destination pointer in the x86_64 return register
  after `_platform_bzero()` calls `_platform_memset()`.
- Add a regression executable that calls `___bzero` through a return-typed
  declaration and checks that the observed return value is the original
  destination pointer.

## Verification

- Confirmed the regression before the fix: `___bzero` returned `ptr + 3148`
  for a 0xc50-byte buffer.
- Rebuilt `libsystem_platform.dylib` and verified the regression passes.
- Verified Perl 5.18 and 5.28 start successfully in Darling.
- Verified Command Line Tools 13.2 installs through `/usr/bin/installer`.
