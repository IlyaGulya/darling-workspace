# cvstorm2 flood progress: profile ownership

## Decision

`darlingserver/standard-signal-coalescing-flood-progress.patch` is owned by
**ARCH_ONLY**.  It is appended immediately after
`darlingserver/message-ctrunc-reject.patch` in the DarlingServer group of the
Arch grouped execution order.  It is therefore series 17 in the Arch typed
mapping and makes the complete Arch batch **19**, without changing the
Homebrew (69) or Perf (7) mappings.

The exact immutable boundary is:

| item | OID / tree |
| --- | --- |
| preceding Arch DarlingServer integration source | `a0877988b130f665ffbf9ee921482c2cbd67f925` |
| continuation source / ordered commit | `b545cdbe9e2f6c16828f484c36a5bef177b2b61a` |
| continuation expected tree | `7b68b23e5a3d3b00a0e4ca5c340371667c18493e` |

The schema-v2 lock is one commit and has no generated integration commit as an
input.  Its future hosted tags use the ordinary create-only
`patch-stack/v1/bases/<oid>` and `patch-stack/v1/sources/<oid>` names in the
DarlingServer immutable mirror.

## Why this is Arch-only

The stranded state is the A0 `cvstorm2-flood` workload: the guest declared a
hang after forward progress stopped while the server remained alive and was
continuously draining `PthreadKill(SIGUSR1)` signal RPC traffic.  The A0/Arch
stack supplies that stress surface and the final preceding integration commit
is `a087…`; neither the Homebrew nor Perf composition reaches this Arch
DarlingServer boundary.  Promoting the change earlier would silently change a
shared guest-signal policy without a reproducer at that earlier profile state.

The deterministic product evidence is retained under
`/tmp/dar-q95-ext4-clone-v3/evidence/cvstorm2-flood-forensic/`:

* the strict baseline capture records live server activity but lost guest
  progress under unbounded SIGUSR1 ingress;
* `b545` extends the existing standard-signal pending-mask coalescing policy
  to SIGUSR1, preserving one pending delivery while suppressing duplicate
  `tgkill` work until the existing completion clear re-arms it;
* six independent strict 20-second flood runs complete (`RESULT=OK`) and the
  selected A0 gate is `PASS=7 FAIL=0` with `DSERVER_MSTATE_ABORT=1`;
* a direct syscall probe measured 856081 baseline versus 226754 candidate
  DarlingServer `tgkill` calls for the focused flood sample.

This is not timeout or retry masking: no timer, retry, fallback, or event-loop
budget was added.  The two prior bounded-drain prototypes were rejected; one
lost the EPOLLET read edge at boot and the edge-safe version still had a strict
flood hang.  The accepted change removes the specifically amplified duplicate
standard-signal work at the source.

## Required boundaries

`lock-first-series-arch-v2.yml` and
`arch-profile-composition-v2.yml` bind all of the following before mutation:

1. the profile patch occurs exactly once;
2. the new series follows `message-ctrunc-reject` in the DarlingServer group;
3. the lock base is the final existing Arch DarlingServer source `a087…`;
4. the new boundary and final module tree are `7b68…`.

The lock-first contract has explicit missing and reordered-entry negatives.
The shared composition dependency contract rejects a tampered per-series
boundary tree before an integration final may be accepted.

## Publication guard

The checked-in lock names hosted immutable refs but does not make a local
`file:///tmp` closure a production input.  Publication must first create the
declared base/source tags without force, prove two independent hosted-only
clean ODB fetches, and then run the 69/7/19 composition.  Existing legacy
archives are retained unchanged; the new archival patch is a new review
artifact and does not rewrite any prior archive.
