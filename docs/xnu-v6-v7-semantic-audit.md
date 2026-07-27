# XNU v6→v7 arch semantic audit

This review-only audit uses independent clean ODB
the preserved offline semantic-audit evidence bundle; it does not read or change the active
West forest.  The full path classification is retained in
the retained path-classification evidence table.

## Boundary inventory

| Series | v6 base / tree | v6 source / tree | v7 base / tree | v7 source / tree |
| --- | --- | --- | --- | --- |
| ring committed-or-unknown | `d068e65d…` / `6ccaaee2…` | `8d746cb7…` / `b479b8fd…` | `92cc4fe7…` / `50258531…` | `1be0fc89…` / `8483bec8…` |
| interruptible RPC disconnect | `8d746cb7…` / `b479b8fd…` | `aadfa984…` / `4131382…` | `1be0fc89…` / `8483bec8…` | `a8b2a19b…` / `e74619c7…` |
| RPC disconnect contract | `aadfa984…` / `4131382…` | `8becacfa…` / `5179fad4…` | `a8b2a19b…` / `e74619c7…` | `23f4fc69…` / `b1195498…` |

Every entry has one ordered commit.  The actual corrected perf integration
predecessor is `92cc4fe7a5eff5027bafa16e7973484eb0aaa12f`, tree
`50258531a2475add6e9e1203d4ded408cfc605c9`.

## Decomposition and commutation evidence

The prerequisite delta `d068e65d… → 92cc4fe7…` changes the paths classified
`PERF_INHERITED` in the TSV.  None of those paths overlaps the arch replay
paths.  The three arch replay deltas are respectively:

| Series | Changed paths | v6/v7 stable patch-id | range-diff |
| --- | --- | --- | --- |
| ring | `dserver-ring.h`, `dserver-ring.c` | `b329716e3113dca8e40e49d2d282254a07b74051` | exact `=` |
| RPC disconnect | `dserver-rpc-defs.h`, `sigexc.c` | `e1df84b1db082eb5fa11fdab7414c07aa4d29ca1` | exact `=` |
| RPC contract | `rpc_disconnect_status_contract_test.sh` | `327c327522a3559f08c7d3f70398cc6fff15acee` | exact `=` |

Thus no hunk was dropped, duplicated, conflict-resolved, or semantically
altered by the restack.  The final tree difference consists of corrected perf
prerequisite content plus the identical arch replay content.  There are no
`SEMANTIC_CHANGE` paths.

## Focused executed checks

On the disposable fully materialized v7 candidate:

- `rpc_disconnect_status_contract_test.sh` emitted
  `RPC_DISCONNECT_STATUS_CONTRACT_OK`;
- `ring_committed_unknown_contract_test.sh` passed;
- `psynch_cvsignal_args_contract.sh` emitted
  `PSYNCH_CVSIGNAL_ARGS_CONTRACT_OK`;
- `run-psynch-return-contract.sh` exited zero.

These are focused host/source contracts, not deployed acceptance.  They cover
the existing RPC/disconnect, transport no-retry, errno/status mapping and
signal/wait contract surfaces affected by the respective replay or inherited
perf prerequisite.  Deployed A0 and shellspawn acceptance remain intentionally
unrun until this report is reviewed.

## Verdict

**ACCEPT_V7.**  The v6 XNU trees are superseded because their prerequisite was
not the corrected canonical perf integration state, not because v7 changes
arch behavior.  Retaining v6 trees would discard accepted perf prerequisite
content.  The three v6 XNU locks should be marked `SUPERSEDED_UNPUBLISHED`
when the v7 publication package is approved.
