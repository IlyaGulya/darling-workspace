# Process-control slot ownership: working candidate and its measurements

WHAT THIS IS. The unlanded working state of the dar-b5pe slot-ownership fix, kept here because the scratch source
trees are not git repositories and the only copies of this work are those files. Both files are the CURRENT content of
the scratch trees (port-src2, which the r1-repro build tree shares by inode):

* `dserver-ring.c` -- the client's give-up sites now use a CONDITIONAL STORE: a slot whose reply word is
  `DSERVER_PROCESS_CONTROL_CLAIMED` is left alone, and every other case still stores IDLE exactly as before. The
  first version of this change instead compare-and-swapped from PENDING only, which is measured to break the boot.
* `mldr.c` -- the loader's two give-up sites carry the same conditional release plus an ungated
  `[mldr-release-held-claimed]` marker naming the site.

MEASURED, in this order:
1. Safe release as a CAS from PENDING (client only): 12/12 BOOT-FAIL (shellspawn, denied=1) against 7/12 PASS before.
2. All three parts (release CAS + generation-scoped reclaim + server-side generation-conditional completion):
   12/12 BOOT-FAIL again. Reverted; 4/4 PASS confirmed.
3. Traced attribution with `DARLING_GUEST_PLANE_STEPS=1`: baseline 3/3 PASS; release CAS alone 3/3 BOOT-FAIL. The
   failing logs carry no `release-held-claimed`, no `plane-refuse` and no `no-slot`, and the plane work visible is a
   normal claim reaching `F-claimed`/`M-returning`.
4. Loader-only conditional release: 3/3 PASS, marker never fired -- so the loader's give-up is not the breaking site.
5. The conditional STORE (client only): 3/3 PASS. The plain store it preserves is what returns a slot left at DONE,
   and the code's own comment records that leaving a slot at DONE breaks the boot worse than any alternative -- which
   is why the CAS version failed and this one does not.

WHAT IS NOT YET MEASURED: the effect of the conditional store on the failure class itself. The twelve-run basic-20
batch that would answer it was interrupted, so the comparison against the 7 PASS / 4 CRASH / 1 HANG baseline is open.
Nothing here is landed in a profile patch; the design and its deterministic test live in
`tests/process_control_slot_ownership_model.py`.
