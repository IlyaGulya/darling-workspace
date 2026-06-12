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

The behavior is supported by the upstream sources:

- [Apple libplatform's generic `_platform_bzero()` calls `_platform_memset()`][apple-bzero],
  and [`_platform_memset()` explicitly returns its original destination][apple-memset].
  This gives Apple's generic implementation the effective machine-level
  behavior of leaving the original pointer in the return register, despite the
  public `void` signature.
- [Apple's `_platform_memset_pattern4()` advances through four-byte chunks][apple-pattern].
  For Perl's `0xc50`-byte interpreter allocation, the final helper destination
  is `ptr + 3148`, exactly matching the invalid pointer observed before this
  fix.
- [Perl 5.28's `perl_alloc()` returns the value of `ZeroD(...)`][perl-alloc],
  so generated code uses the result of the zeroing expression as the
  interpreter pointer.
- LLVM's Darwin x86 regression tests require zero-filled memset lowering to
  call `___bzero` for both [fixed-size][llvm-bzero] and
  [variable-size][llvm-variable-bzero] cases.

## What Changed

- Implement x86_64 `_platform_bzero()` as a small assembly wrapper around
  `_platform_memset()`. It saves the original destination on the stack and
  ends with `popq %rax; retq`, making the effective Darwin return-register
  contract immune to compiler-generated C epilogue instrumentation.
- Keep the generic C implementation for non-x86_64 architectures.
- Add a regression executable that calls `___bzero` through a return-typed
  declaration and checks both that the observed return value is the original
  destination pointer and that the entire buffer was zeroed.
- Register the regression with CTest when a cross-compiling emulator is
  configured. Darling executables are Mach-O, so registering it without an
  emulator would create a test that host CTest cannot execute.

## Verification

- Confirmed the regression before the fix: `___bzero` returned `ptr + 3148`
  for a 0xc50-byte buffer.
- Rebuilt `platform`, `platform_static64`, and
  `darling_bzero_return_regress`.
- Inspected the rebuilt `libsystem_platform.dylib`; `___bzero` and
  `_platform_bzero` resolve to the assembly wrapper ending in
  `popq %rax; retq`.
- Verified Perl 5.18 and 5.28 start successfully in Darling.
- Verified Command Line Tools 13.2 installs through `/usr/bin/installer`.

[apple-bzero]: https://github.com/apple-oss-distributions/libplatform/blob/2512ffd8bb5c6caff3c8ea83331ab8a0adc820c3/src/string/generic/bzero.c#L61-L67
[apple-memset]: https://github.com/apple-oss-distributions/libplatform/blob/2512ffd8bb5c6caff3c8ea83331ab8a0adc820c3/src/string/generic/bzero.c#L36-L49
[apple-pattern]: https://github.com/apple-oss-distributions/libplatform/blob/2512ffd8bb5c6caff3c8ea83331ab8a0adc820c3/src/string/generic/memset_pattern.c#L25-L39
[perl-alloc]: https://github.com/Perl/perl5/blob/ca7c7d676b0e55e60226a121fbea5f8cd5f1bad2/perl.c#L191-L205
[llvm-bzero]: https://github.com/llvm/llvm-project/blob/ce7dae3727322304712f6a1bcdebb254cad9ee60/llvm/test/CodeGen/X86/darwin-bzero.ll#L1-L13
[llvm-variable-bzero]: https://github.com/llvm/llvm-project/blob/ce7dae3727322304712f6a1bcdebb254cad9ee60/llvm/test/CodeGen/X86/variable-sized-darwin-bzero.ll
