# ADR: the diagnostics toolset is one evidence pipeline with named views

Status: proposed (2026-10-10).

## Context

The workspace's diagnostics tool set grew one command at a time, each from a concrete incident. That
produced individually defensible commands and a surface that is no longer one product. Measured, not
asserted:

- `docs/tooling.md`, the index, does not mention `futex`, `core-lock` or `watch`.
- The Rust sources contain 203 distinct uppercase machine-line prefixes. By first segment the largest
  families are CORE 22, PREFIX 9, COURIER 8, DIAG 7, SERVER 6, WAITS 5, MODES 5, GUEST 5, CORES 5,
  CATCH 5, PROVENANCE 4, FUTEX 4, WAKES 3, WAKE 3. `CORE` and `CORES` are separate families; `WAITS`,
  `FUTEX`, `WAKES` and `WAKE` are four vocabularies over neighbouring questions.
- 22 of 25 subcommands accept `--json`; `deploy`, `trace` and `cores` do not.
- `scripts/dwdiag` forwards `run`, `capture`, `darling` and `help` but not `signal` or `diag`, so the
  same subcommand is reachable or not depending on which spelling a reader files it under.
- Three commands independently own installation knowledge (`deploy`, `prefix`, and `cycle`'s
  composition), and `job` re-dispatches the analyzers rather than owning their implementations.

Two things are already right and this decision keeps them. The refusal vocabulary (`UNMEASURED`,
`EVIDENCE-NO-DATA`, `WITNESS-SILENT`, `HISTORY unavailable`) is the strongest property of the set: it
makes "not measured" unspeakable as a plausible number. The run manifest written at job start, and the
byte slices read from it, is the beginning of one evidence model.

## Decision

**The toolset is one pipeline over run evidence, exposed as named views with one output contract, one
dispatch registry and one owner per question.**

| family | owns |
| --- | --- |
| Live run and capture | spawning a workload, attaching to it, sampling it, signaling it, and publishing the bundle that everything else reads |
| Artifacts and lifecycle | source/build provenance, and the one supported deployment/provisioning transaction |
| Acceptance | judging a workload by its own machine-readable verdict, including suites and receipts |
| Finished-run report | `report --run MANIFEST` with selectors for phases, transports, instruments, process chronology, denials and repetition |
| Waits and locks | wait-object explanations, with the evidence strength of each explicitly named: log history, shared-object snapshot, core lock snapshot |
| Symbols and fault forensics | symbol resolution and fault decoding per source: image, instrument log, kernel report, core |
| Process and host administration | the current process census and host crash-dump policy; mutation stays outside read-only analysis |
| CLI discovery | one registry for help and routing, with family landing pages |

Rules that make it one product:

- Every view consumes evidence with a run identity. A command either takes a run manifest (or a bundle
  or core that carries one), or an explicit artifact, and never guesses which run it is looking at.
- Every refusal is reported with the vocabulary above, and the vocabulary is a contract, not a habit.
- Every command that reports measurements emits one machine-readable document when asked for it,
  versioned, with measurements, evidence references, limitations and completion status as fields; notes
  never disappear because a human renderer truncated them.
- One owner per question: analyzers own their analysis, orchestration owns selection, and no second
  engine reimplements installation.
- A command's own test is behavioral. Nothing asserts unification by matching help text or source.

## Consequences

- `diag` stops being a competing public spelling: the families above are the surface, and any internal
  namespace is an implementation detail with one documented entry point.
- Views that measure different evidence keep different names and keep saying which evidence they used.
  Unification must not flatten `waits` (log history) into `futex` (shared-object snapshot) or `core`
  (fault) into `core locks` (owner tokens): the difference is what keeps a reader from treating a
  weaker instrument as a stronger one.
- `docs/tooling.md` becomes the index: one section per family, the command list, the output contract,
  and pointers to the Beads that hold incident reasoning instead of restating it.
- Removing a route is a migration of its callers, not a deletion of its capability.

## Rejected alternatives

### Keep the surface and document it better

The index drift is a symptom: 203 prefixes and three installation engines cannot be fixed by writing
them down. Documentation cannot supply an evidence identity that a command does not consult.

### Rename everything in one change

A single sweep moves the churn into every caller and skill at once, and a rename that lands without its
tests leaves the set less trustworthy than before. The migration therefore proceeds family by family,
each step leaving the tool working.

### Merge analyzers that answer neighbouring questions

`progress` (phase and completion) and `courier` (per-transport traffic and endpoint purity) read the same
log and answer different questions; `waits`/`futex`/`core-lock` differ in evidence strength. Merging them
would hide which instrument produced a claim, which is the failure this toolset exists to prevent.

### Adopt a general framework instead of one pipeline

The pipeline is small, and its value is in the refusals and in the evidence identity, not in the
framework. Nothing here needs a plugin architecture; it needs one owner per question and one place that
decides what a run is.

## Enforcement

Behavioral contracts, not text matching: the CLI registry executes every public route and asserts one
owner per question; analyzers run against two concurrent synthetic runs sharing an append-only log and
must contain only their own closed byte ranges; truncated, rotated, missing and unreadable logs must
report unavailable or partial rather than a measured zero; conflicting prefix overrides must be an
explicit comparison rather than a silent replacement; every JSON-advertising command must emit exactly
one parseable document; ignored explicit flags must fail validation before side effects; the report and
its individual views must agree on identical evidence; histories and snapshots must remain distinct; and
deployment is exercised through a fake transaction backend, never a real prefix. Contracts live where the
workspace rules require them to be registered, so that a contract nothing runs cannot exist.
