# PR draft — xnu: call __mldr_postfork_child() in the fork child

- **beads:** dar-q95.12
- **repo:** darlinghq/darling-xnu
- **branch:** `fix/fork-postfork-child` (off `origin/main` `5f26a4c`)
- **files:**
  `.../emulation/include/linux_premigration/elfcalls_wrapper.h`,
  `.../emulation/src/linux_premigration/elfcalls_wrapper.c`,
  `.../emulation/src/xnu_syscall/bsd/impl/process/fork.c`

## Title
fork: reset mldr/elfcalls state in the fork child

## Body
The fork child inherited the parent's mldr/elfcalls state, which is no longer
valid after `fork()`. Add a thin `__mldr_postfork_child()` wrapper around
`elfcalls()->postfork_child()` and invoke it at the top of the child path in
`sys_fork()`, before the darlingserver checkin, so mldr can reset its
post-fork state.

## Excluded
The enriched "Failed to checkin with darlingserver after fork" diagnostic
printf that was in the same investigation file is **dropped** from this branch;
only the `__mldr_postfork_child()` call ships.

## Note
Related to the SwiftPM / Homebrew fork handling; discovered during the
investigation, not in the original PR plan.
