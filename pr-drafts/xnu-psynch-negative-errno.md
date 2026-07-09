# PR draft - xnu: preserve psynch wait status through syscall/cerror

- **beads:** dar-q95.19
- **repo:** darlinghq/darling-xnu
- **current branch:** `fix/psynch-negative-errno`
- **top-level bumps:** `f38d2a146`, `f510f71bb`
- **clean PR branch:** local
- **files:** libsystem_kernel psynch wait wrappers

## Title
libsystem_kernel: preserve emulated psynch wait errno/status bits

## Body
Darling's emulated psynch wait path needs to report wait-specific BSD errors
such as `EINTR` and `ETIMEDOUT`, including condition-variable status bits. The
fixed branch keeps the full psynch error/status value in Darling's internal
negative-error syscall transport so the public syscall wrapper routes it through
`cerror` without truncating the status bits.

## Layering

This is no longer paired with the old `dar-q95.16` libpthread negative-return
workaround. `dar-q95.29.2` resolved the boundary: libsystem_kernel owns syscall
transport and cerror preservation, while `libpthread/psynch-kernel-return-helper`
owns pthread-level decode and retry policy.
