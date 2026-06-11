# PR draft - xnu: implement getattrlistbulk

- **beads:** dar-q95.18
- **repo:** darlinghq/darling-xnu
- **current commit:** `6159147`
- **top-level bump:** `4b504ae86`
- **clean PR branch:** not split yet
- **files:**
  `darling/src/libsystem_kernel/emulation/include/xnu_syscall/bsd/impl/xattr/getattrlistbulk.h`,
  `darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl/xattr/getattrlistbulk.c`

## Title
libsystem_kernel: implement getattrlistbulk

## Body
`getattrlistbulk()` was stubbed as `ENOTSUP`, which breaks Darwin directory
enumeration clients that prefer bulk attribute reads. Implement the syscall
using the existing directory read/stat plumbing and pack returned attributes in
Darwin layout.

## Tests

- getattrlistbulk probe over ordinary directories
- Ruby/Homebrew directory enumeration smoke after the `ATTR_CMN_NAME` fix
