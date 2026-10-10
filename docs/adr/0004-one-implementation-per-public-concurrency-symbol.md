# ADR: one implementation per public concurrency symbol; no weak no-op stub may satisfy it

Status: accepted (2026-10-10). Implemented and proven: the four public stubs are gone, the emulation's own
locks moved to a private `libsimple_lock`, and the guest's lock image, exclusion behaviour and the T4
end-to-end gate were measured on one runtime (xnu `95588ba` on `fix/private-ulock-return-channel`;
evidence in `evidence/dar-liaq-unfair-lock-stub/`). Supersedes
`0002-publish-thread-owner-token-before-guest-execution.md`, whose premise (an unpublished owner token)
measurement refuted.

The rule is deliberately stated as a property of bindings, not of the source, because the same audit is now
being applied to the kqueue-shaped placeholders (`kqueue_impl`, `kevent_impl`, `kevent64_impl`,
`kqueue_close`, `kqueue_closed_fd`, `kqueue_dup`, `__darling_kqueue_register_listen`): those are either
private or loud (`kqueue_impl` prints "No kqueue implementation?!" before returning `-ENOSYS`), and the
question that decides whether hiding them is safe is which of them the guest can actually reach and bind.

## Context

`os_unfair_lock` is the owner-identity lock the toolchain and the C++ runtime use everywhere. Its owner is
the calling thread's Mach thread-port name, read from TSD slot `__TSD_MACH_THREAD_SELF`; zero is the
unlocked value, and the fast path is a compare-and-exchange from zero to that token. If the token read is
zero, the exchange succeeds while storing zero: the word never becomes owned, and every thread that takes
the lock is admitted. That failure is silent, because a lock that admits everyone looks exactly like an
uncontended lock from the outside.

Measured on a live prefix with a guest fixture that holds every census thread live behind a barrier:

- the thread's owner token is present and stable: a direct `gs`-relative read of slot 3 gives the main
  thread `0x103`, no participant reads zero, and a sample taken immediately before each lock is non-zero
  in all 2800 rounds;
- the deployed `libsystem_platform.dylib` reads that same token and stores it correctly: its fast path is
  `slot = 3` -> `mov %gs:0x0(,%rax,8),%eax` -> `lock cmpxchg %edx,(%rcx)`;
- nevertheless a private, freshly initialised lock keeps its word at zero after a successful lock, and a
  shared lock admits two threads at once in 2798 of 2800 rounds;
- `dladdr` on the guest's `os_unfair_lock_lock` reports `/usr/lib/system/libsystem_kernel.dylib`, and that
  library defines it as `void __attribute__((weak)) os_unfair_lock_lock(os_unfair_lock_t lock) {}` in two
  emulation files (four stubs in total: `lock` and `unlock` in each).

The stub was written to let the emulation code compile before libplatform exists, and `weak` was chosen so
that a real definition would win. It does not win: an exported weak definition in a loaded library still
satisfies the guest's dynamic binding, so the public symbol resolves to a function that does nothing.

## Decision

**Every public concurrency primitive has exactly one implementation in the guest, and a stub whose purpose
is to satisfy an internal caller must not be able to satisfy a public binding.**

Concretely:

| Layer | Obligation |
| --- | --- |
| libsystem_kernel emulation | The locks it needs for its own bookkeeping get a private implementation: file-local statics or hidden-visibility symbols. It does not export a weak no-op under a public name. |
| Any component with a placeholder | A placeholder is private, or it fails loudly. It is never a silent no-op behind a public symbol. |
| Diagnostics | A guest probe reports which image implements a primitive it depends on; a binding is a measured fact, not an assumption. |
| Tests | A behavioral exclusion test over a shared unfair lock runs in the guest tier, and the symbol that implements it is asserted to come from the library that is supposed to own it. |

The invariant from ADR 0002 survives and is unaffected: a nonzero owner token before lock-capable
execution still has to hold. What changes is the owner of the fix: publication was never broken in the
observed case, while the dispatched implementation was.

## Consequences

- The failure mode "a lock that admits everyone" can no longer be produced by a placeholder, and its
  absence is asserted by a test that measures exclusion rather than by reading the source.
- The emulation code that needs a lock during early startup keeps one, but its lock is its own; changing
  it cannot change what the guest sees.
- A weak definition is no longer usable as a forward declaration across a library boundary in this tree.
  Where a real implementation is genuinely optional, the caller tests for it explicitly instead.
- Debugging stops at the binding question early: a probe that names its implementing image is cheaper than
  reading three candidate libraries.
- The earlier lines of investigation that this decision closes: the owner-token publication theory, the
  XNU apple-vector `th_port` theory, the TSD-base theory. Each is recorded in the owning Bead with its
  measurement, so a future session does not repeat them.

## Rejected alternatives

### Remove the stubs outright, without migrating their callers

The emulation calls the lock for its own bookkeeping, so deleting the definitions alone breaks its
link. Deleting them *after* every caller has moved to a private primitive is the selected fix, and the
two must not be confused: removal without migration is a link error, or worse, a silent fall back to
whichever library happens to define the name.

### Keep the weak stubs and rely on link order

Link order is not a contract, and it demonstrably did not hold here. A silent no-op that can win a binding
is a latent failure of every consumer of the symbol.

### Make the stubs forward to a real implementation

A definition in libsystem_kernel cannot forward to libplatform's symbol without creating the same circular
dependency the stub was written to avoid; the lock belongs to the caller's own storage.

### Treat a zero owner token as "unlocked" inside libplatform

The lock would still be correct only for the copy that runs. The defect is not in the token comparison, and
a defensive change there would leave the shadowed binding in place.

## Enforcement

- A guest fixture takes a shared `os_unfair_lock` from several live threads with an atomic occupancy
  counter and fails when two are admitted; the same fixture reads the owner slot directly, without the lock,
  and reports `dladdr` for the lock symbol so the implementing image is part of the evidence.
- The fixture is registered where guest tests run, and the exclusion arm is the behavioral gate: no source
  or help-text matching.
- A host-side contract asserts that no exported weak definition of a public concurrency symbol exists in
  the emulation libraries, and it fails on a new one by name.
