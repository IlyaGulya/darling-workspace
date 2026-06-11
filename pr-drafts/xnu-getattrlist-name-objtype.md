# PR draft - xnu: implement getattrlist ATTR_CMN_NAME and ATTR_CMN_OBJTYPE

- **beads:** dar-q95.17
- **repo:** darlinghq/darling-xnu
- **current commit:** `d157c3f`
- **top-level bump:** `7df268d2d`
- **clean PR branch:** not split yet
- **files:** `darling/src/libsystem_kernel/emulation/include/xnu_syscall/bsd/helper/xattr/getattrlist_generic.c`

## Title
libsystem_kernel: implement getattrlist ATTR_CMN_NAME and ATTR_CMN_OBJTYPE

## Body
Darwin Ruby's glob implementation resolves literal path components with
`getattrlist(..., ATTR_CMN_NAME | ATTR_CMN_OBJTYPE, FSOPT_NOFOLLOW)`. Darling's
emulation rejected those attributes with `EINVAL`, causing `Dir.glob` and
Homebrew's `empty_installation?` check to report existing directories as empty.

Implement both attributes in the generic getattrlist helper: pack the
`ATTR_CMN_NAME` attrreference/name payload and return the filesystem object type
for `ATTR_CMN_OBJTYPE`.

## Tests

- C getattrlist probes for `ATTR_CMN_NAME`
- Ruby `Dir.glob` over `/usr`, `/bin`, and Homebrew keg paths
- Homebrew `brew install --build-from-source lz4` advanced past the empty-keg
  failure.
