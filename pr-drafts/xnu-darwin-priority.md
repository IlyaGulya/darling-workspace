# PR draft — xnu: handle PRIO_DARWIN_THREAD/PROCESS

- **beads:** dar-q95.10
- **repo:** darlinghq/darling-xnu
- **branch:** `fix/darwin-priority` (off `origin/main` `5f26a4c`)
- **files:**
  `.../emulation/src/xnu_syscall/bsd/impl/process/getpriority.c`,
  `.../emulation/src/xnu_syscall/bsd/impl/process/setpriority.c`

## Title
getpriority/setpriority: handle PRIO_DARWIN_THREAD and PRIO_DARWIN_PROCESS

## Body
`sys_getpriority` / `sys_setpriority` forwarded the `which` argument verbatim to
the Linux `getpriority` / `setpriority` syscalls. The Darwin-only selectors
`PRIO_DARWIN_THREAD` and `PRIO_DARWIN_PROCESS` are not valid Linux `which`
values, so the syscall rejected them — and Ruby / Homebrew use them to set
per-thread Darwin priority/background state.

Handle these selectors in-emulation:

- `PRIO_DARWIN_THREAD`: `who` must be 0 (else `EINVAL`).
- `setpriority` accepts `prio` of `0`, `PRIO_DARWIN_BG`, or `PRIO_DARWIN_NONUI`
  (else `EINVAL`); `getpriority` returns 0.
- `PRIO_DARWIN_PROCESS`: same `prio` validation; `getpriority` returns 0.

Other `which` values fall through to the Linux syscall unchanged.

## Note
Discovered during the Homebrew investigation; not in the original PR plan.
Currently a behavior shim (no real per-thread Darwin background state); good
enough to unblock callers that just set/query it.
