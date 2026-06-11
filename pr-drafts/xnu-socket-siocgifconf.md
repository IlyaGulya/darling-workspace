# PR draft — xnu: stub SIOCGIFCONF

- **beads:** dar-q95.11
- **repo:** darlinghq/darling-xnu
- **branch:** `fix/socket-siocgifconf` (off `origin/main` `5f26a4c`)
- **files:** `.../emulation/src/xnu_syscall/bsd/helper/ioctl/socket.c`

## Title
socket ioctl: stub SIOCGIFCONF (0xC00C6924) returning an empty interface list

## Body
`handle_socket()` let `SIOCGIFCONF` (`0xC00C6924`) fall through unhandled, so
callers enumerating network interfaces failed instead of getting a (possibly
empty) list. Answer it by setting `ifc_len = 0` and returning success (or
`-EFAULT` when `arg` is NULL).

Stop-gap consistent with the existing `SIOCGIFFLAGS`/etc. TODO ("has to be
implemented for container with separate networking"): report no interfaces
rather than error out.

This is pragmatic compatibility, not a complete interface-enumeration
implementation. A real networking parity fix still needs host/container
interface discovery and correctly populated `ifconf` entries.

## Possible relation to dar-gwn.2
`brew install` still stalls during *Fetching downloads*; if any of that path
depends on interface enumeration, returning an empty list (vs a real loopback
entry) may matter. Worth checking when diagnosing dar-gwn.2.
