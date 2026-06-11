# PR draft — xnu: convert fd_set between BSD and Linux word sizes

- **beads:** dar-q95.3
- **repo:** darlinghq/darling-xnu
- **branch:** `fix/select-pselect-fdset` (off `origin/main` `5f26a4c`)
- **files:**
  `.../emulation/src/xnu_syscall/bsd/impl/select/fdset.h` (new),
  `.../emulation/src/xnu_syscall/bsd/impl/select/pselect.c`,
  `.../emulation/src/xnu_syscall/bsd/impl/select/select.c`

## Title
select/pselect: convert fd_set between BSD (32-bit) and Linux (long) words

## Body
A BSD/Darwin `fd_set` is an array of 32-bit words; a Linux `fd_set` is an array
of `unsigned long` (64-bit on x86_64) words. The emulation passed the Darwin
`fd_set` straight to the Linux `select` / `pselect6` / `_newselect` syscalls,
so on 64-bit the kernel mis-read the request bitmap and mis-wrote the ready
bitmap — fds above 31 (and the high half of each 64-bit word) were handled
incorrectly.

Add `fdset.h` with `bsd_fdset_to_linux()` / `linux_fdset_to_bsd()` helpers and
stage `rfds`/`wfds`/`efds` through stack (`alloca`) Linux-layout buffers on the
way into the syscall and back out on success.

## Scope note
Current-boundary fix in libsystem_kernel. Longer term this conversion belongs
with Linux syscall mediation in mldr.

## Reproduced by
`tests/regression/ruby_thread_raise_select`.
