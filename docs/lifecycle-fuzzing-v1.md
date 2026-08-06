# Stateful lifecycle fuzzing v1 (dar-4ush.4)

This is an infrastructure-only fuzz surface.  Rust owns the bytecode decoder,
generator, reducer replay and explorer invocation; the Python file is only a
bounded process/JSON contract.  It does not introduce a second lifecycle
model and it does not route production shutdown.

The reducer is the sole semantic oracle. Every accepted reducer trace is
checked against the complete fuzz `FUZZ_INVARIANT_REGISTRY` (the base model
registry plus typed historical predicates); a terminal trace that drops
an invariant is reported as `INVARIANT_VIOLATION`, not accepted. A trace with
one or more reducer event rejections is instead a typed `REJECTED` input, even
when its suffix contains a terminal and the rejected prefix would otherwise
drop an invariant. This keeps malformed mutations from stopping a campaign;
terminal is a strict final bytecode event, including `Noop` and `Advance`.
Budget exhaustion remains `BUDGET_EXCEEDED` and is never promoted to an
invariant crash. Only an accepted trace may crash the target for an invariant
failure.

## Bounded bytecode

`DLF1` version 1 has a maximum input of 4096 bytes and 64 operations.  The
Rust harness enforces limits of 5 capabilities, 64 schedule steps, the
existing model virtual-time and recovery-step limits, 16 bounded error records
of 256 bytes each, 64 replay events, and 64 KiB serialized output.  Decode
rejects truncated, malformed, trailing and oversized input.  The target uses
the bounded `safe_replay` adapter to classify malformed/rejected inputs without
stopping the campaign, then re-raises only a true panic, accepted invariant
failure, or unsafe/undetected outcome as a libFuzzer artifact containing the
exact input.

## Corpus

The authoritative registry contains 38 seeds:

* 6 golden traces: `shared-session-signal-gone`,
  `shared-session-signal-rejected`, `shared-session-retained-holder-timeout`,
  `shared-session-pid-reuse`, `shared-session-late-fork`, and
  `session-root-exit-before-snapshot`;
* 24 named FORENSIC explorer outcomes (the full 16-boundary/12-interleaving
  matrix selectors are checked by the contract);
* 8 historical seeds: inode ABA, PID reuse, late fork, deadline, rejected
  signal, root GONE, cleanup failure, and quarantine replacement. Each has
  explicit immutable provenance, a distinct typed bad arm, and an expected
  invariant violation; the verifier replays both arms and rejects duplicate
  encoded programs.

`lifecycle-fuzz --verify-corpus` checks decode/encode identity, the 6/24/8
inventory, 38 unique bytecodes, all eight historical good/bad replays, and
reproduction through the existing deterministic explorer.
`lifecycle-fuzz --corpus-hex NAME` emits a canonical seed without introducing
an opaque fixture file.
For a cargo-fuzz run, `lifecycle-fuzz --materialize-corpus DIR` writes exactly
those 38 bounded seeds into a task-owned temporary directory; it never writes
the repository corpus directory.

Retained real FORENSIC roots are created only below the caller-owned `TMPDIR`
and receive a typed `lifecycle-fuzz-manifest.json`. CLEAN and expected
`FORENSIC_REQUIRED` roots are removed by the fuzz target after bounded manifest
extraction. Only an actual `UNSAFE`/`UNDETECTED` result or an accepted reducer
invariant failure may retain a root, and the campaign-wide retained-root
budget is eight. The external contract owns and removes its exact temporary
namespace after checking manifests.

## Local commands

```text
CARGO_NET_OFFLINE=true cargo test --manifest-path lifecycle/operation-boundary/Cargo.toml --all-targets
bash tests/run-lifecycle-fuzz-contract.sh
bash tests/run-lifecycle-fuzz-ub-gate.sh  # bounded Miri semantic-core leg

cd lifecycle/operation-boundary/fuzz
FUZZ=/tmp/dar-4ush-cargo-fuzz-tool/bin/cargo-fuzz
RUSTUP_TOOLCHAIN=nightly "$FUZZ" build lifecycle_fuzz --target-dir /tmp/dar-4ush.4-fuzz-target
../target/debug/lifecycle-fuzz --materialize-corpus /tmp/dar-4ush.4-corpus
RUSTUP_TOOLCHAIN=nightly TMPDIR=/tmp/dar-4ush.4-asan \
  ASAN_OPTIONS='symbolize=0:detect_odr_violation=0' LLVM_SYMBOLIZER_PATH=/bin/true \
  "$FUZZ" run lifecycle_fuzz --sanitizer address \
  --target-dir /tmp/dar-4ush.4-fuzz-target /tmp/dar-4ush.4-corpus -- \
  -runs=40 -max_total_time=15 -max_len=4096 -rss_limit_mb=512 -timeout=5
RUSTUP_TOOLCHAIN=nightly TMPDIR=/tmp/dar-4ush.4-tsan \
  TSAN_OPTIONS='report_signal_unsafe=0:symbolize=0:external_symbolizer_path=/bin/true' \
  LLVM_SYMBOLIZER_PATH=/bin/true \
  "$FUZZ" run lifecycle_fuzz --sanitizer thread --build-std \
  --target-dir /tmp/dar-4ush.4-fuzz-target-tsan-std /tmp/dar-4ush.4-corpus -- \
  -runs=40 -max_total_time=15 -max_len=4096 -rss_limit_mb=512 -timeout=5
```

The address and thread runs are bounded smoke runs.  The installed
`cargo-fuzz 0.13.2` exposes only `address`, `leak`, `memory`, `thread` and
`none`; it has no `undefined` sanitizer.  Direct nightly `rustc` likewise
rejects `-Zsanitizer=undefined` (the accepted list has no `undefined` value),
so UBSan is recorded as **UNSUPPORTED**, not reported as PASS. The accepted
policy is `ASAN_TSAN_MIRI_SEMANTIC_CORE`: ASan and TSan run the complete fuzz
target, while Miri runs the filesystem-free decoder/reducer core, every
reducer corpus seed, all eight typed historical bad arms, minimization and
deterministic mutations. Miri does not run `explore_one`, because it does not
implement Linux `O_PATH`/fd-relative syscalls. `tests/run-lifecycle-fuzz-ub-gate.sh`
emits `UB_GATE_STATUS=PASS` only after that bounded Miri target succeeds; the
separate sanitizer logs remain required evidence. The initial
stable attempt is recorded too: stable rejects cargo-fuzz's required
`-Zsanitizer=address` as a nightly-only option.  TSan requires `--build-std`
and the installed nightly `rust-src`; without that, Rust reports sanitizer ABI
mismatch against the prebuilt standard library.

The libFuzzer entry point executes the selected boundary/interleaving pair
through `explore_one`; it does not normalize inputs to a canonical
`BeforePrepare`/`None` slice. The deterministic contract additionally executes
all 24 named FORENSIC selectors and verifies their exact outcomes. The smoke
runner performs valid bytecode mutations after corpus initialization and
reports the mutated execution count.

## Failure artifacts

Every replay report includes source identity (the explorer commit/tree as an
immutable provenance anchor only, plus a deterministic authoritative digest and
per-file inventory for the complete semantic closure: reducer, explorer,
state, policy/schema, both lockfiles, target, contracts and the fuzzing docs).
The closure digest, not an uncommitted workspace HEAD, binds the code that was
actually replayed; the report still records workspace HEAD for context and
bytecode version. It also carries a bounded resource
census, coverage hash, a reproduction command, and a failure fingerprint
(missing invariants, normalized error classes, scenario and recovery status).
A minimized reducer bytecode/replay trace is retained only when that exact
fingerprint survives minimization.
A product defect is not fixed here: the deterministic minimized input is the
regression seed for a separate Bead.
