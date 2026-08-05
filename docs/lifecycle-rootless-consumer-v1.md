# Rootless shutdown consumer v1

`dar-4ush.7` begins with a deliberately narrow Rootless shutdown consumer. The
consumer accepts an observed shutdown trace from the existing Rootless
acceptance path and routes it to the Rust lifecycle boundary through the
transport-only West adapter. Rust replay remains the only lifecycle authority;
this module does not inspect `/proc`, open prefixes, send signals, or implement
an alternate reducer.

The domain binding is explicitly signal-bearing: it requires
`intent=REQUEST_SHUTDOWN`, `profile=rootless`, a `rootless-shutdown` provenance
envelope, a capability-targeted signal event, and a final terminal event. The
Rust boundary then validates the complete schema, ownership, identity,
recovery, budgets, and terminal outcome. A missing signal, raw/non-capability
target, `CREATE_PREFIX`/`RECREATE_PREFIX` intent, or non-Rootless profile is
rejected before it can be treated as a Rootless result.

Signal-free shutdown where the process is already gone is handled by the
separate, explicitly named `RootlessShutdownGoneConsumer`. It requires an
`identity_revalidated=GONE` observation for the single authoritative
`SESSION_ROOT_PIDFD` in the initial capability catalog and rejects
signal-bearing traces; the signal-bearing contract above is not weakened.

This first consumer is observational and behavior-preserving. It does not
change Darling shutdown code, runtime locks, source patches, mappings, or
production defaults. The existing Rootless guest regression remains the
product behavior gate; this consumer adds a shared model/replay gate around
captured lifecycle evidence. A later `.7` slice may connect a typed production
observer once that hook is separately reviewed. Optimized-overhead measurement
and default routing remain open acceptance work for `.7`.

Run the focused gate with:

```text
tests/run-rootless-shutdown-consumer-contract.sh
```
