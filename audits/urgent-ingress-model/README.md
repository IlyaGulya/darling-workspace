# Urgent ingress ownership model

`UrgentIngress.tla` is an independent bounded abstraction, not a model of guest ABI atomics. Five FIFO admissions represent outer ENTER, nested ENTER, SIGPROCESS, nested EXIT and outer EXIT. Call identities are request numbers, not reusable transport sequence numbers. Two page epochs expose retirement/replacement between claim and publication. Logical context linking, actual creation, executor occupancy, reply production, executor return, terminal claim and publication are distinct actions. EXIT preparation retains a tombstone until executor return. Waiting arms a recheck latch, cleared by physical return.

Executor scheduling is separate from executor ownership: `executor` is the currently scheduled Call, `live` contains every executor that has not returned, and `suspended` retains the saved outer owner while a nested executor runs. Explicit `Suspend` and `Resume` actions bound the model to one saved outer executor and at most one scheduled nested executor (two live owners). A reply may become ready before suspension. Nested return removes only that nested identity from `live`; it cannot retire the saved outer owner. Resume requires the outer actual context to be current again, and SIGPROCESS/EXIT cannot run against a context whose own ENTER executor is still live.

A handler may return without a reply. Its admission becomes abandoned; no safe publication action exists for it. A claimed admission is removed before publication, so this model intentionally does not assert BUSY implies admission. Death can interrupt any stage; it need not finish requests. There is no fairness, completion or unconditional liveness assertion. The finite exploration includes terminal dead ends and stuttering.

Safety checks cover context ownership at preparation, current-incarnation publication, no fabricated DONE, per-Call at-most-once publication, executor bookkeeping and `NoLiveExecutorClaim`. The latter is independently stated as `claimed \cap live = {}`: no terminal claimant may own an executor that has not actually returned, whether scheduled or suspended. Unsafe configurations independently bypass context gating, incarnation gating, the reply obligation or per-owner executor retirement. The `nestedClaim` arm permits a ready suspended outer Call to claim after a different executor returns. Its intended witness is outer execute/reply/suspend, nested execute/return, then premature outer claim while the outer remains in `live`. Expected counterexamples are required; an unexpected success is a failed negative control. This does not prove guest wake delivery, DTape behavior, protected runtime isolation, sequence-wrap freedom or end-to-end guest correctness.

Run from the manifest using the existing isolated runner (parent-owned execution):

```sh
audits/rpc-plane-model/run-all.sh --model-dir audits/urgent-ingress-model --module UrgentIngress \
  --case safe.cfg:ContextOwnership:OK \
  --case safe.cfg:CurrentPublication:OK \
  --case safe.cfg:NoFalseDone:OK \
  --case safe.cfg:AtMostOnce:OK \
  --case safe.cfg:NoLiveExecutorClaim:OK \
  --case safe.cfg:ExecutorOwnership:OK \
  --case unsafe-context.cfg:ContextOwnership:VIOLATED \
  --case unsafe-stale.cfg:CurrentPublication:VIOLATED \
  --case unsafe-no-reply.cfg:NoFalseDone:VIOLATED \
  --case unsafe-nested-claim.cfg:NoLiveExecutorClaim:VIOLATED \
  --case unsafe-nested-claim.cfg:ExecutorOwnership:OK \
  --case unsafe-nested-claim.cfg:NoFalseDone:OK \
  --case unsafe-nested-claim.cfg:AtMostOnce:OK
```

The source-owned native test is `tests/urgent_lifecycle_test.cpp` in
darlingserver, built by `dserver_urgent_lifecycle_tests` and registered as
`dserver_urgent_lifecycle`. It exercises the production helper's refusal of
duplicate dispatch/claim, explicit no-reply abandonment, actual-versus-logical
context, EXIT cancellation, removed-context non-revival and nested terminal
ownership. Run the guarded `urgent_signal_lifecycle_guest` catalog workload
after complete normal runtime integration. Native helper and bounded model
evidence are not substitutes for that runtime oracle.
