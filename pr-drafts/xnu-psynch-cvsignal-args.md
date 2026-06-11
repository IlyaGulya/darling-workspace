# PR draft — xnu: fix psynch cvsignal/cvbroad arguments

- **beads:** dar-q95.2
- **repo:** darlinghq/darling-xnu
- **branch:** `fix/psynch-cvsignal-args` (off `origin/main` `5f26a4c`)
- **files:**
  `.../emulation/include/xnu_syscall/bsd/impl/psynch/psynch_cvsignal.h`,
  `.../emulation/src/xnu_syscall/bsd/impl/psynch/psynch_cvsignal.c`,
  `.../emulation/src/xnu_syscall/bsd/impl/psynch/psynch_cvbroad.c`

## Title
psynch: fix cvsignal/cvbroad argument widths and cvbroad mutex argument

## Body
Two bugs in the libsystem_kernel psynch shims:

1. `sys_psynch_cvsignal()` declared `cvlsgen` and `mugen` as `uint32_t`,
   truncating the 64-bit generation-count words the darlingserver RPC expects.
   Widen both to `uint64_t` (header + impl).
2. `sys_psynch_cvbroad()` passed `mugen` in the slot where the **mutex pointer**
   belongs (`dserver_rpc_psynch_cvbroad(..., mugen, mugen, ...)`), so the server
   never received the condition variable's associated mutex. Pass `mutex`.

## Note
Separate PR from the select/pselect fdset fix (dar-q95.3) even though both were
in the same investigation commit.
