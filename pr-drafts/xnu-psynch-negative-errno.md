# PR draft - xnu: return psynch wait errors as negative errno

- **beads:** dar-q95.19
- **repo:** darlinghq/darling-xnu
- **current commits:** `f3c8d94`, `0a10e19`
- **top-level bumps:** `f38d2a146`, `f510f71bb`
- **clean PR branch:** not split yet
- **files:** libsystem_kernel psynch wait wrappers

## Title
libsystem_kernel: return emulated psynch wait errors as negative errno

## Body
Darling's emulated psynch wait path needs to report wait-specific BSD errors
such as `EINTR` and `ETIMEDOUT`, including condition-variable status bits. The
local branch returns those values directly as negative return-register values
for the emulated path and documents that this does not flow through the normal
macOS `cerror` / `errno` path.

## Coupled Change

This pairs with `dar-q95.16` in libpthread, which decodes negative psynch wait
returns. Before upstreaming, review whether the deeper fix should instead be to
normalize the libsystem_kernel ABI to macOS-style `-1 + errno` and keep
libpthread closer to upstream.

Tracked as `dar-q95.23`: do not open this together with the libpthread decoder
until the intended ABI boundary is explicit. If Darwin parity requires
`-1 + errno` at the libsystem_kernel/libpthread interface, this PR should become
the lower-layer normalization fix rather than a documented negative-errno
contract.
