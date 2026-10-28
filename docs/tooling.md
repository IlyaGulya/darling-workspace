# Diagnostic tooling for Darling guest/runtime work

Normative index. The reasoning and the measured incidents behind every rule here
are in `docs/direct-transport-descriptor-architecture.md` (sections 148-155 and
onward). The operational guidance an agent loads is the `darling-diagnostics`
skill; this file is the durable index it points at.

For the implemented C++ server debug ABI, its unavailable Rust client/LLDB
glue, and current operational capture routes, see the
[debugger reference](darling-debugger.md). Use `dwdiag processes`, `unwind` and
`run` for available host/process evidence; these do not consume the server ABI
or provide its server-state identity graph.

## Installation, guest state and diagnostic inputs

Use an absolute immutable installation root `R` and a separate, disjoint writable
guest prefix `P`. The product is `R/bin/darling`, `R/bin/darlingserver` and
`R/libexec/darling`; `P` is `DPREFIX`, not an installation. There is no `P/bin`
launcher discovery and no second system-library deployment into `P`.

Configure the product with absolute `CMAKE_INSTALL_PREFIX=R`, keep the normal
`COMPONENTS=stock` default, build ALL and perform the complete normal install:

```sh
mise run west darling-build --build-dir "$B" --runtime-root "$R" --install --with-test-data
mise run west darling-bootstrap --runtime-root "$R" --prefix "$P" --no-build
```

`B`, `R` and `P` stand for explicit owned absolute paths; keep writable state and
evidence beneath the owned `DW_STATE_ROOT`. `--with-test-data` adds optional
installed fixtures, not a different runtime. For real guest C/C++ compilation,
bootstrap with `--guest-toolchain darling-command-line-tools`. Runtime smoke
does not establish a working compiler or workload acceptance.

Before invoking the runner, set `TMPDIR` to an existing owned directory beneath
`DW_STATE_ROOT` so automatically named run logs also stay in durable state.

New code requires a newly configured/published root, never an overwrite of an
existing image. `dwdiag --runtime-root "$R"` selects the same installation for
every diagnostic subcommand; the direct binary accepts the global option before
`diag`. `DARLING_RUNTIME_ROOT` is the environment selector. `--prefix` and
`DWDIAG_PREFIX` select writable guest state only. Keep the caller's raw prefix
spelling for product no-follow and `..` checks.

Workspace lifecycle ownership uses a persistent external per-prefix sibling
lease, distinct from the product's `P/.lifecycle.lock`. Never delete either
persistent lock to bypass an owner. Finish the owning run's status and cleanup
before staging inputs or reusing `P`; separate experiments need separate lanes.
Ordinary acceptance requires product shutdown without external rescue.

`dwdiag prefix --prefix "$P"` stages the fixture from
`R/libexec/darling/usr/bin/<guest-command-basename>` into
`P/private/var/tmp/...`, verifying its SHA-256. A missing installed fixture fails;
it is not fetched from an ambient build. For custom inputs use
`--asset SOURCE=private/var/tmp/NAME --guest-command /private/var/tmp/NAME`.
Assets are restricted to guest `/private/var/tmp`, cannot traverse guest symlinks
or `..`, and are not runtime libraries. `prefix --bootstrap --build "$B"` opts
into normal bootstrap; `--no-build` consumes an existing installation.

Before standalone `verdict` or `suite`, use `prefix --prefix "$P"` with the
same global runtime root to stage the fixture; those verdict commands check
prerequisites but do not provision missing inputs. Their guest-path lookup can
use a selected upper file or an installed lower file.

`dwdiag suite --record-evidence "$DW_STATE_ROOT/evidence/suite.json"` creates a
new standalone workload-evidence receipt. Its parent must exist and its path must
be unused. It records the selected runtime fingerprint and suite results, never
changes the immutable installed-image receipt, and does not turn a retry,
partial oracle or rescue cleanup into clean acceptance. Retain original logs
and retry verdicts; use `--retry-failed-rows false` for a no-retry series.

CLI `installation_identity` is a concise receipt/path/SHA, artifact-count,
source-revision/dirty and provenance summary. The schema-2 receipt retains the
complete actual installed inventory, including install-hook files and actual
CMake manifest hashes; verification rejects extra bytes. It remains about that
image even when the build directory changes. `RUN-ENV` is a smaller diagnostic
fingerprint, not the full receipt or proof of every loaded image.

Command examples describe supported interfaces, not executed results. Keep
one-off investigation chronology in its owning evidence/Bead or architecture
record, not in this operational index. Fixed addresses in retained output
illustrations are examples, never defaults or proof about another run.


## Tools

| tool | answers | notes |
|---|---|---|
| `scripts/darling-boot-run.sh --runtime-root R --prefix P` | one measured run end to end | owned lifecycle, unique log, bounded completion wait, retained bundle, workload oracle and cleanup. `--status-marker FIELD=` requires integer zero for all declared fields; an earlier failure cannot be erased by a later zero or oracle hit. Explicit `--poll` wins; additional `--marker` checks remain required. Without status declarations fixture defaults apply. `--hatch` is an explicit socket-disabled diagnostic opt-in; `--env K=V`, `--verify-probe TAG` and `--assert-prefix-maps` retain their diagnostic roles |
| `west darling-build --build-dir B --runtime-root R --install [--with-test-data]` | publish the complete ordinary installation | builds ALL and uses normal CMake install rules into the configured fresh `R`; never a partial component copy or an in-place image update |
| `scripts/darling-artifact-manifest.sh --runtime-root R [--prefix P]` | did a selected boot artifact change since a baseline | `--save`/`--check` cover its fixed build/installed-image inventory, not every installed file; `--probe TAG` reports presence, not a missing-tag failure gate |
| `scripts/darling-describe-artifact.sh` | where a Mach-O starts and what is really in it | `LC_MAIN` as a file offset (address = `__TEXT.vmaddr + entryoff - fileoff`), symbol address + disassembly, tag offsets and installed-image identity. File-only `--pointer VM --arch x86_64 --json FILE` resolves a chained pointer with the selected slice's UUID, VM/file offsets, encoding, target and bounded C string; unsupported encodings, external binds and invalid/unmapped slots fail explicitly |
| `scripts/darling-trace-guest.sh --runtime-root R --prefix P` | what an owned guest process actually executes | holds the shared external prefix lease through normal product shutdown; samples prefix-owned PID/starttime identities at 50 ms by default, with optional comm filtering, raw maps/thread syscalls/stack pointers and page bytes. Retains guest log, adjacent `.samples.jsonl` and `.report` in a unique durable directory unless `--log` selects a new path. Sampling may miss a short-lived image; exit zero is diagnostic completion plus clean shutdown, not workload PASS |
| `tests/run-plane-deferred-arm-contract.sh --runtime-root R --prefix P` | process-scoped console progress while main is parked in Mach receive | requires two real console opens, writes and closes before the worker releases main; both payloads must reach the run's own server-log slice. Holds one external prefix lease, checks installed-server identity, completed consumer status and normal clean shutdown. Compiles with explicitly provisioned selected guest CLT, or validates and executes a copied guest Mach-O supplied by `--fixture` through `/Volumes/SystemRoot`; records original/copy SHA-256. The same consumer oracle applies to bad and fixed images |
| `scripts/darling-probe-report.sh` | a run log as a probe BISECTION | groups probes by identity, reports UNBALANCED stacks (entered, following probe never appeared), the last events in order, and the run's own reporting; reading this by hand went wrong three separate ways |
| `scripts/darling-stop-reason.sh` | the recognized stop, context and product verdict from retained run evidence | reads the run log and bundle captures, including indexed `.zst` payloads, and emits text or `--json`. Nonzero guest, `--rc` or `--runner-rc` status overrides an oracle hit; unavailable evidence is reported and cannot produce PASS. Explicit guest `--rc=0` establishes completion; runner zero alone does not. License, loader and fault causes remain fatal; SQLite diagnostics with explicit successful guest completion remain context rather than the stop. SQLite codes are database results, not OS errno values. Product verdict is separate from parser exit: exit 0 means a named stop was recognized, 3 means none, not workload success. `darling-boot-run.sh` retains the report in its bundle. Preserve the log's first `[dwdiag-env prefix=… log=… bundle=…]` stamp: log-only diagnostics use it to select the run's server evidence |
| `scripts/darling-prefix-map.sh` | which prefix a process is rooted in | reads `maps`, because a guest process has no readable path |
| `scripts/prefix-cleanup.sh --runtime-root R --prefix P` | prefix-scoped cleanup and a cleanliness gate | inspect `--dry-run` first; ownership comes from prefix-scoped process identity, never a shared installation path or a process name alone. External rescue can clean a failure but returns failure and cannot produce production PASS |
| `scripts/guest-probe.h` | the probe contract | single literal tag, one syscall, registers preserved, and the Linux syscall-numbering rule |
| `scripts/closure_analyze.py` | the Xcode load closure and the evidence for each supplying route | ordinary CMake `install_manifest.txt` records expanded installation paths; `--declarations` reports missing recorded files or a missing install prerequisite, not complete source/install reconciliation. `--sweep` classifies routes with evidence. CMake target-name and bounded source searches are heuristics, not installed-file or runtime proof. `STATIC_CALLING`, `STATIC_NONCALLING` and `STATIC_UNATTRIBUTED` are static code-shape classifications; JSON retains `evidence_tier=static`, `runtime_status=unverified` |
| `scripts/closure_analyze.py --cover --runtime-root R --prefix P` | the load closure at SYMBOL granularity and the first modeled loader stop | resolves the writable `P` upper and installed `R/libexec/darling` lower, then permitted Xcode paths; follows nested re-exports, aliases, flat/weak lookup and loader-chain `LC_RPATH`s. `--order N` predicts absent eager demands; `--without`/`--without-symbol` model a past state; `--json` writes the graph. This is static structure, not observed runtime success |
| `scripts/closure_missing.py COVER_JSON [--json REPORT] [--order N]` | the missing census of a retained `--cover --json` graph | reads the pure JSON graph through its raw or indexed `.zst` logical path; adds no resolution of its own. Reports labels, strong eager demands, binding flags and referrer hashes/routes. Exit 1 means modeled loader demands remain; exit 2 means unreadable, corrupt or invalid input, never an empty census. A PREFIX image can come from the selected upper or installed lower; the other route is XCODE |
| `scripts/rung-driver.py` | the current loader rung and structural verification | `stop --log LOGICAL_RUN_LOG --runtime-root R --prefix P` reads raw/indexed `.zst` evidence. Unknown observed referrers fail, not `NO-REFERRER`. `verify --component X --runtime-root R --prefix P` checks selected-image exports/re-exports and payload == installed reference == selected guest image; `--fail-on-installed-miss` additionally gates the installed reference's own export surface. Source routing, static loader structure and file identity are not consumer-visible runtime proof |
| `scripts/west-pin-sync.py` | whether `west.yml`'s revisions match the darling superproject's gitlinks | `check` prints the before/after table and fails when a pin is behind its gitlink; `sync` rewrites only the `revision:` values of named projects (comments and formatting survive); a pin **ahead** of its gitlink is refused unless `--allow-downgrade` says so |

For closure analysis, `--xcode` accepts the selected `Xcode.app` bundle or its
`Contents` directory. With `--cover` or `--order`, `--json` writes one JSON
document to stdout, without the human table or predicted-order preamble,
including when an entry yields no readable images. Exit 0 means no modeled
loader stops, 1 means stops remain, and 2 means incomplete or invalid inputs.
This is static coverage of the selected entry, not runtime API behavior or
successful application compilation; later `dlopen` targets need their own
`--entry` or an observed guest run log.


## Rules, each measured

1. **Preserve the probe ABI.** Use a literal tag and one raw write, preserving
   required registers. The raw Linux `syscall` number for x86_64 `write` is `1`,
   not Darwin's number. `scripts/guest-probe.h` is x86_64-specific, not a portable
   i386 implementation.
2. **Check both presence and execution identity.** A tag must exist in the built
   artifact and in the installed image that served the run. Presence alone does
   not prove a path executed; silence can mean a disabled or unreachable hook.
3. **Publish complete images.** Shared emulation changes require both
   libsystem_kernel and dyld from the same normal ALL/full installation at `R`.
   Never synchronize system libraries into `P` or repair an image in place.
4. **Bind observations to the run.** Retain installed-image identity, raw
   workload host PID/starttime and prefix ownership. Shared `R` executable paths,
   process names or guest namespace IDs alone do not identify one prefix.
   `/proc` visibility may be partial; state missing evidence explicitly.
5. **A diagnostic line is not a verdict.** Require the workload's requested
   completion/result oracle and final lifecycle cleanup. A readiness marker,
   zero diagnostic counter or clean-looking log tail is not completion.
6. **Minimize perturbation.** Do not obtain probe identity through an additional
   emulated syscall. Use an existing stable per-thread identity; a per-frame
   buffer address cannot correlate callers and callees. Pre-libc paths cannot
   depend on `getenv`, constructors or a fully initialized TLS runtime.
7. **Inspect the compiled variant.** A source file can build for multiple
   architectures or into both dylib and dyld variants. A hook in a compiled-out
   branch is not an execution measurement. Exercise the off arm and every
   supported target ABI when changing instrumentation.
8. **Keep memory reads guarded and bounded.** A length cap does not make a stale
   base address safe. Use the existing guarded reader, preserve declared record
   lengths, and report unreadable/partial/truncated payloads.
9. **Separate install IDs from dependencies.** An image's `LC_ID_DYLIB` is not a
   referrer to itself. Distinguish weak-only demand and no referrer from a strong
   eager loader dependency.
10. **Respect guest roots.** Selected upper `P` and installed lower
    `R/libexec/darling` resolve ordinary guest system paths. Only permitted
    Xcode/mount paths and loader `@rpath` resolution are Xcode-provided; an
    arbitrary same-basename Xcode file does not satisfy a system dependency.
11. **Use ordinary installation declarations.** Closure analysis reads the
    matching build's normal `install_manifest.txt` and configured install root.
    CMake target names are hints, not proof of installation. A mutable build
    manifest is not the immutable image's provenance receipt; select inputs
    corresponding to the observed image.
12. **Treat instruction/bind decoding as evidence, not a spelling match.**
    Universal-image file offsets, slice-relative offsets and Mach-O VM addresses
    differ. Chained imports carry their own weak/bind information; preserve
    decoder disagreements instead of trusting an ambiguous disassembler label.
13. **Preserve exact source pins.** Manifest revisions and superproject gitlinks
    must identify the intended closure. A pin-sync operation is not permission
    to discard a deliberately newer dependency or foreign changes.

## Agent workflow and instrumentation safety

Load the canonical `.agents/skills/darling-diagnostics/SKILL.md` before measuring;
the root instruction file routes tasks here instead of duplicating the command
reference. `scripts/dwdiag-roster.py --write|--check` owns only the marked roster
in this document, not `AGENTS.md`.

For traced memory, use xtrace's guarded raw `process_vm_readv` reader in
`src/xtrace/safe_read.cpp`. Never dereference an untrusted descriptor address
while formatting diagnostics: a length cap cannot make a stale base safe.
Report short/unmapped reads, clamp descriptor walks to declared `msgh_size`
and emit DECODE-STOPPED on truncation. The 32-bit image must not read out-of-line
payloads unless the guard is supported there. Preserve declared record lengths
and explicitly report any output cap; retain complete abort/terminate text.

`cycle --fresh` (implied by `--probe`) requires a FRESHNESS witness from the
run's own workload lines; it is not a guest environment override or a verdict
cache switch. The cycle path does not invoke west test's verdict cache.
A log without execution evidence must fail, not look like a reused success.
Prefer the fixture's raw-Linux `host_pid=` declaration to guessed shell/loader
matches or emulated Darwin pids. The managed ring fixture is installed at
`/private/var/tmp/ring_mach_msg_test`, not host `/usr/bin`.

For namespace-aware fault capture, use the existing runner around a managed
workload whose inner command owns the prefix lifecycle:

```sh
scripts/dwdiag --runtime-root "$R" run --name NAME --bundle-root "$DW_STATE_ROOT/bundles" \
  --capture-gdb --capture-tree --gdb-namespace \
  --capture-pattern "$HANDLE" --gdb-ex 'handle SIGSEGV stop print nopass' \
  --timeout-seconds 280 -- mise run west test --workload "$WORKLOAD" \
  --source-root "$S" --runtime-root "$R" --prefix "$P"
```

Choose an observable loader handle for `HANDLE`, not a guest argv absent from
host cmdline. Generic `run` supervises a host command: it does not infer a prefix,
acquire its lifecycle lease or guarantee product shutdown. Do not wrap an inner
owner in another prefix lock. A direct launcher instead needs a caller-owned
lifecycle plan or the prefix-aware runner's `darling` route. Capture timeout or
interrupted cleanup cannot become clean PASS. Read the announced bundle; do not
reimplement namespace attachment or start a second guest beside its owner.

## In-process diagnostics: one switch, `DARLING_DIAG`

Some facts can only be measured INSIDE a guest process: which block a faulting pointer belonged to, when a CFURL
was released and by which caller, whether an NSURL wrapper outlived its CFURL. Those hooks live in libmalloc,
CoreFoundation and Foundation, and they are enabled through ONE variable:

| token | library | what it enables |
|---|---|---|
| `alloc` | libsystem_malloc | the allocation ring: last 1M alloc/free events plus, on SIGSEGV/SIGABRT/SIGTRAP, the faulting receiver's whole history (and any `_darl_trace_note` the CF layer attached to it) |
| `url-free` | CoreFoundation | `[cfurl-free] url=… releaser=…` for every CFURL deallocation, where `releaser` is the caller that asked for the LAST release |
| `url-life` | Foundation | `[nsurl] retain/release self=… cls=… cfurl=… cftype=… rc=…` for every Foundation NSURL |
| `url-cache` | CoreFoundation | re-enable the URL resource-info cache, which is off by default (see CLASS-D evidence section 64) |

`DARLING_DIAG=alloc,url-life` is the normal spelling; the per-knob variables (`MALLOC_TRACE_RING`,
`CFURL_FREE_TRACE`, `NSURL_TRACE`, `CF_URL_RESOURCE_CACHE`) are still honoured so older recipes keep working.

Rules that came from using them:

* they are OUT-OF-PROCESS-SAFE but IN-PROCESS-ONLY: no `dwdiag` subcommand can produce them, so a session that
  needs them must name the token in the run's environment, not add a sixth ad-hoc variable;
* the ring is 1M entries on purpose: with 8192 the receiver of a real run (millions of allocations) is never in it;
* a recorder installed from a constructor broke guest startup, so installation is lazy, on the first allocator
  entry; `__builtin_return_address` deeper than frame 2 also broke runs, so the ring records one caller;
* every dump is BOUNDED per process (first N lines then silence): a diagnostic that floods a log hides the record
  it was meant to preserve.

The instruments print on the guest's own fd 1, so they appear in the run's capture, not in the harness log.


## The diagnostics are ONE tool: `dwdiag`

Before writing a script for a core, a stack or a symbolized offset: check the roster below. Two
generations of this workspace added a parallel tool that `dwdiag` already provided
(`scripts/darling-core-frames.py`, removed; `scripts/guest-core-report.py`, now marked SUPERSEDED).
The roster is generated from the tool's own `--help` and a contract fails when it drifts.

<!-- dwdiag-roster:begin (generated by scripts/dwdiag-roster.py; do not edit by hand) -->
| command | what it answers |
|---|---|
| `dwdiag watch` | Watch a running guest's kernel signal dispositions (SigCgt/SigBlk) against the run log's size |
| `dwdiag source` | Report whether a build tree actually compiles given source files, and refuse when it does not |
| `dwdiag build` | Build the runtime pair the rule requires (libsystem_kernel and the dyld image TOGETHER) and check that the instrument strings a caller expects are present in the built artifact |
| `dwdiag deploy` | Publish the complete normal CMake installation into the selected immutable runtime root |
| `dwdiag cycle` | Publish or explicitly reuse an installed image, run a workload and report fired instruments |
| `dwdiag symbolize` | Resolve a reported offset to `symbol + offset` for any image (guest dylib or server binary) |
| `dwdiag crash` | Turn a `dserver-CRASH` line into a location, a stack walk and the disassembly around the fault |
| `dwdiag verdict` | Run ONE guest workload and judge it by its own machine-readable line; absence of that line is FAIL/HANG |
| `dwdiag suite` | Run a SET of guest workloads and print one table with the counters that decide acceptance |
| `dwdiag progress` | Say WHERE a finished run stopped: the workload's own line, the guest loader's last stage, and the last plane op the server actually serviced. Published-vs-serviced is the diagnosis; this prints both instead of grepping |
| `dwdiag courier` | Count the packets per TRANSPORT for a run log, and judge the AF_UNIX endpoint's purity. Rows with no instrument behind them are printed UNMEASURED, never as zero: a census that cannot see a transport must say so |
| `dwdiag futex` | Resolve the futex waits a capture bundle holds through BOTH sides' memory maps and join them by shared-object identity (device, inode, file offset + displacement), naming the object and saying which side is missing. A waiter and its waker usually live in different processes, so comparing virtual addresses is meaningless; this is the join that a stall needs and that was done by hand every time |
| `dwdiag core-lock` | Recover, from a core, the unfair lock a dying thread was aborting on: the lock address, its word, the self token the slow path compared against, and every live thread's self token. It exists because this exact step was done by hand three times, twice wrongly, and because the only honest answer it can give about acquisition history is "a core is one instant" |
| `dwdiag ring` | Decode the server's lock-free event ring: the fixed six-word records a dump prints, their schema, and what the dump CANNOT show. The ring exists so a diagnostic event never prints or syscalls on the path it observes (`dtape_test_ring_record` reserves one slot with a single atomic RMW and then stores, so a torn record is possible by design and overwritten records are simply absent). A reader that skips those two facts reports half-written events as measurements and silence as evidence, which is why this is a command and not a grep |
| `dwdiag witness` | Report which INSTRUMENTS fired in a run log, and which stayed SILENT. A registered instrument with zero hits is the failure this exists to catch: a guard that silently does nothing cannot be told from no guard at all, and a hand-written grep over one log finds a line but cannot tell you which of the other instruments never spoke |
| `dwdiag evidence` | Say what a finished run's instrument evidence IS before anything censuses it: which registered windows belong to this run, what was truncated, rotated or unreadable, and which state the census below is in. It is the read half of the run manifest `west-job.sh` writes at start, and it exists so an unreadable log is reported as `unavailable`/`partial` rather than as an instrument that stayed silent |
| `dwdiag job` | Diagnose a finished managed job using its persisted scope and finite captured logs: show actual failure bytes, instrument activity, progress, waits and repetition. A core requires an unambiguous registered capture; ambient cores and boot-wide kernel logs are never attributed. Current process observations are scoped separately to the selected prefix/runtime, not evidence of the past run |
| `dwdiag unwind` | Read a stopped guest's stacks: per-thread rsp from the debugger, then the words above it, filtered to guest images and symbolized. Host gdb cannot walk a Darwin-ABI frame, so this is how a stuck guest gets function names instead of a plausible story. With --catch SIGNAL it instead attaches, arms a stop on that signal, resumes the target and reports the trap |
| `dwdiag waits` | Join the server's whole wait/wake vocabulary per ulock object: who waits, who owns, and whether a wake was ever issued for it. A stall is not diagnosed by the last log line, and this table was re-derived by hand in every investigation until it became a command |
| `dwdiag trace` | The chronologically ordered story of ONE process in a run log: every line that names its pid or tid, plus the plane/urgent refusals around it. This is the question of this whole stage -- "what happened to this child?" -- and it was reconstructed by hand with grep/sed/python six times, each time risking the wrong slice of a two-million-line log |
| `dwdiag denials` | One row per DISTINCT denial in a run log, with the caller SYMBOLIZED and the post-fork context next to it. |
| `dwdiag prefix` | Prepare writable guest state and test assets for the selected installed runtime, then optionally run a workload. Runtime files are never copied into the guest prefix |
| `dwdiag processes` | Report processes owning the guest prefix through directory descriptors or exact prefix environment. The count describes the observed census; normal product shutdown and lifecycle cleanup remain required |
| `dwdiag trap` | Read the KERNEL's own report of where a guest died and symbolize the address inside a prefix. |
| `dwdiag cores` | Turn crash dumps on, off or report them -- and say why an unreadable crash is unreadable. |
| `dwdiag core` | Read a CORE and name the faulting function. A guest's ud2 runs NATIVELY, so Darling cannot report it as a syscall and four other routes measured blind on the same crash (silent dmesg, an armed-but-silent witness, a guest that masks SIGSEGV, and a catch waiting for a swallowed signal). The core is written by the kernel and read by gdb with no ptrace; it is also OFF by default here, which this command states rather than hides |
| `dwdiag identity` | Census the per-thread identity a core (or a live prefix process) holds: `__TSD_MACH_THREAD_SELF` per thread, with machine-readable zero/duplicate/unstable flags and a count per distinct value. |
| `dwdiag loops` | Report the pattern of a process that is NOT working any more: one line repeating near the end of the log with almost nothing new between its repetitions. |
| `dwdiag mappings` | Print a core's OWN mapping table (NT_FILE), one row per mapping, plus optionally the mapping that owns an address and the bytes the core carries there. |
| `dwdiag env` | Print the runtime root and compact fingerprint of key installed binaries, with guest state reported separately. Shares verdict/cycle's candidate set; this compact fingerprint does not replace the complete install receipt |
<!-- dwdiag-roster:end -->

The implementation is vendored at `tools/darling-debug-runner`. The shim can
build it when missing or stale, including for `--help`; use an already-built
binary directly when research must not build:

```sh
scripts/dwdiag --runtime-root "$R" <SUBCOMMAND> [OPTIONS]
tools/darling-debug-runner/target/release/darling-debug-runner --runtime-root "$R" diag <SUBCOMMAND> [OPTIONS]
```

### `dwdiag mappings` -- a core's OWN mapping table, because `readelf` refuses the note

```sh
scripts/dwdiag mappings --core "$CORE" --addr "$ADDRESS" --json
```

The reader uses the core's `NT_FILE`, not a live process's current mappings or
GDB's pathname guesses. It reports the mapping count, range units, file offset,
address owner and any requested bytes. An address outside every mapping is not
offset zero; absent/unreadable bytes are reported, and no `NT_FILE` produces
`MAPPINGS-EMPTY`.

Keep the source core and matching image identities. Guest paths in an archived
core can describe an older layout; they are evidence to resolve, not deployment
instructions.

### `dwdiag env` -- the installed-image fingerprint on its own

```sh
scripts/dwdiag --runtime-root "$R" env --prefix "$P" --json
```

The fingerprint candidates live in one tool-owned table. `env` hashes that fixed
installed-image set under `R`; `P` supplies guest-state context, not library
discovery. Incomplete installations or fingerprint inventories fail. Compare
the run's own identity before interpreting its output; this table is not the
complete installed inventory and does not prove which images were mapped.

The tool is **vendored in this workspace** (`tools/darling-debug-runner`, source + README + `Cargo.lock`), so its code,
its documentation and every instruction that calls it live in one tree; the sibling repository of the same name is only a
fallback for an older checkout. Example invocations (not measured results):

```sh
scripts/dwdiag symbolize --binary "$R/bin/darlingserver" \
  --base-symbol dserver_crash_probe --delta 0x19d54c
scripts/dwdiag crash --binary "$R/bin/darlingserver" --log "$RUN_LOG"
scripts/dwdiag --runtime-root "$R" verdict --prefix "$P" \
  --mode sem_timed --args "300 1" --wait 60
scripts/dwdiag --runtime-root "$R" suite --prefix "$P" --wait-base 60 \
  --require-zero-creations --retry-failed-rows false \
  --record-evidence "$DW_STATE_ROOT/evidence/suite.json" \
  -- 'sem_ready 2 :: sem_block 100 1 :: basic 20'
```

The commands share symbolization, log reading and workload interpretation rather
than requiring callers to scrape each other's text. Use `--json` where the
selected command supports it; inspect that command's exit semantics instead of
treating every parser zero as workload PASS.

For the ring fixture, the requested mode's completed machine-readable result and
guest exit status are load-bearing. Missing results can mean no execution,
launch failure, signal death, ordinary nonzero exit or watchdog expiry; retain
the actual classification and raw evidence. A successful status/readiness
marker cannot erase an earlier failure. Diagnostic counters such as denials
remain relevant even when the workload's own oracle passes.


### `dwdiag build` and `dwdiag cycle` -- the iteration loop, with the paths out of the command

```sh
DWDIAG_BUILD="$B" scripts/dwdiag build --expect '[sigexc-in '
DWDIAG_BUILD="$B" DWDIAG_PREFIX="$P" \
  scripts/dwdiag --runtime-root "$R" cycle \
  --mode basic --args 20 --repeat 3 --probe sigexc-in,native-exit
```

`build` defaults to `libsystem_kernel.dylib` and `dyld`, which share emulation
code. It is build-only; `--expect STRING` gates presence in both built files.
It does not publish or establish guest execution.

`cycle` normally invokes the complete ordinary build/install with optional
tests-data, stages the installed fixture, then runs and censuses the workload.
`--target` adds build-only targets before that complete install. `R` must be a
fresh configured install root: a later changed-code iteration needs a new root,
not an overwrite. For repeat diagnostic runs of an existing immutable image,
pass `--skip-build`; it performs no build/install and cannot combine with
`--target`. `--repeat` reuses that selected image for separate workload runs.

`--probe` implies `--fresh`: require the run's own workload execution witness,
then report fired and silent registered instruments. This is independent of
West's verdict cache. A successful install, a fingerprint or a silent probe is
not workload acceptance; preserve completion, warnings and cleanup evidence.

### `dwdiag core` -- the one artifact that still has the thread in it

```sh
scripts/dwdiag --runtime-root "$R" core --core "$CORE" --prefix "$P" --log "$RUN_LOG" --json
```

A core retains register notes, signal information, mapping notes and process
metadata even when a guest signal handler or an absent live witness hid the
fault. Preserve these distinctions:

- `NT_SIGINFO` describes the signal and delivery code; it does not alone identify
  the faulting thread. The tool states its thread-selection rule and cross-checks
  a supplied fatal-signal log rather than merging incompatible faults.
- `NT_FILE` range and file-offset units come from the core. Kernel cores and
  GDB-generated cores need not use the same units.
- Universal Mach-O file/slice offsets are translated to image VM addresses before
  symbolization. Both offsets and failures to convert remain visible.
- Frame-pointer walks validate bounds/order/mappings and state why they stop;
  a plausible symbol is not proof of a complete caller chain.
- Dispatch-once operand reconstruction is best-effort register evidence, only
  emitted when its stated relationship matches. A core is one instant, not an
  ownership or acquisition history.

`crash` can point to a matching core, but age and run identity must agree.
Inspect `dwdiag cores status` before deliberately enabling core collection.
`cores on`/`off` change the host core-pattern configuration and must be scoped
and restored; the crashing process also needs an appropriate core-size limit.
Do not assume a copied historical `/tmp` core path exists.

The focused host entrypoint is `tests/run-core-contract.sh`; an optional real
guest core is additional evidence, not a substitute for exercising the contract.

### `dwdiag identity` -- who each thread says it is

```sh
scripts/dwdiag --runtime-root "$R" identity --core "$CORE" --prefix "$P" --json
scripts/dwdiag --runtime-root "$R" identity --pid "$HOST_PID" --prefix "$P" --json
```

This censuses the supported guest's `__TSD_MACH_THREAD_SELF` slot, with host LWP,
image/symbol and zero/duplicate/unstable flags. Zero means unset, not a shared
nonzero identity. Unreadable identities are explicit failures, not fabricated
zero values.

A core is one instant. Live instability requires two samples with execution
between; thread churn is not itself identity instability. Live inspection needs
ptrace permissions, and attachment can perturb the target. Do not infer that
a plain Linux process uses the guest's GS-based identity layout.

`tests/run-identity-contract.sh` retains the focused host/core checks; interpret
an optional real guest input separately from those cases.

### `dwdiag loops` -- is the process working, or going round in circles?

`scripts/dwdiag loops --log RUN_LOG [--guest-log FILE] [--min N] [--tail-len N] [--spin-max-distinct N] [--json]`
reports repeated normalized patterns and diversity in the selected log tail.
Numeric normalization collapses changing IDs/addresses without discarding marker
text.

This is a log-window heuristic. A varied tail does not prove forward progress;
an unchanging tail does not prove that the process is still alive. Other prefix
processes can keep a log growing after the subject exits. Correlate patterns
with the subject's current identity, exit evidence and wait/stack captures.

### `dwdiag trap` -- the kernel already said where the guest died

`scripts/dwdiag --runtime-root R trap [--kernel-log FILE] [--prefix P] [--process SUBSTR] [--limit N] [--json]`
reads retained or available host-kernel fault reports and symbolizes against
the selected installed image. It handles both invalid-opcode trap and segfault
record shapes; unavailable kernel evidence is not proof that no fault occurred.

Keep the reported instruction/load offset as the fact and a nearest-symbol
answer as a hint. A large offset from the preceding symbol can mean that the
containing function is absent from the symbol table. A kernel report can remain
useful when no core or deliverable debugger stop exists.

### `dwdiag waits` -- who waits, who owns, and whether anyone was ever woken

`scripts/dwdiag waits --log RUN_LOG [--json]` joins recorded ulock park,
cleanup and wake events by object. `NEVER-WOKEN`, `WAKE-MISSED-THE-WAITER`,
`WOKEN` and `NO-WAITERS` are different event histories, not interchangeable
degrees of a hang. A cycle requires both recorded directions.

The log describes events that happened. `proc=present|absent` is a current
presence observation, not a guarantee of PID/TID identity across reuse; a park
record can belong to an exited process. Compare the exact thread/run evidence.
An opt-in instrument that was not enabled cannot establish a zero wake count.

### When NOT to reach for `--catch`: a masked signal is not catchable

A debugger catch needs a deliverable stop. Guest signal masking/handlers can
make a catch expire without registers or frames. That timeout is unavailable
capture, not proof that no fault occurred. Use retained kernel reports or a core
where available, and retain the actual signal/identity evidence.

### `dwdiag unwind` -- guest frames, which host gdb cannot walk

`scripts/dwdiag --runtime-root R unwind --pid HOST_PID [--image SUBSTR] [--window BYTES] [--max N] [--scan] [--json]`
reads an attachable live guest's stacks: per-thread `rsp`/`rip` come from the
debugger. A validated frame-pointer walk is preferred; the labelled scan
fallback reports candidate addresses in mapped guest images, not a backtrace.

It exists because the three obvious routes do not work, each measured: host gdb attached to a
stopped guest resolves only the loader's syscall trampoline; `/proc/<tid>/maps` `[stack]` is
present only for the main thread; and `startstack` in `/proc/<tid>/stat` is zero for non-leaders.
Two of those mistakes produced "frames" that were image data (`image_base+2`), which is why both
filters are stated in the output rather than left implicit.

**Frame-pointer walks can be incomplete.** Each step checks stack bounds,
alignment, monotone frame pointers and executable mappings. Optimized production
code may omit frame pointers, so `frames_via=fp` does not prove that every caller
is present. Do not assume the guest was built with `-O0`.

Use `--scan` to select the existing stack-word scanner even when a partial
frame-pointer chain is valid. It reports `frames_via=scan`: candidate addresses,
not a backtrace. Stack data, function pointers and stale return addresses can
produce plausible symbols. Candidates retain increasing stack-memory order;
neither that order nor symbolization proves a call relationship. The default
still prefers the validated frame-pointer chain and falls back to scanning
when no chain is available.

`--catch SEGV` resolves frames too, from a stack image gdb dumps while the target is stopped on the
signal -- the part gdb cannot do itself, since it cannot walk a guest frame. Each stopped thread is
reported as `CATCH-STACK rsp=... frames=N` followed by its `FRAME` lines, in the same depth order.

```
scripts/dwdiag unwind --pid 890075 --image prefix-ios --max 6
UNWIND pid=890075 window=524288 threads=3
THREAD tid=890075 rsp=0x7fffffdff808 rip_image=libsystem_kernel.dylib symbolized_frames=6
  FRAME 0x741d7a2babd9 image=libobjc.A.dylib _ZN8method_taSERKS_ + 0x99
  FRAME 0x741d7a3f6959 image=libxpc.dylib xpc_pipe_get_log + 0x1209
UNWIND-VERDICT threads=3 symbolized_frames=18 note=frames are words above each thread rsp that fall in a guest image, sit past that image's first 16 KiB, and resolve to a symbol; a thread with none resolved either has nothing there or points at a stripped image
```

**A table on the stack looks like a stack.** MEASURED against the stalled ld: one thread's "frames"
came out as a run of consecutive addresses ten bytes apart inside `libkxld.dylib`, each resolving to
the same symbol -- that is a vtable the thread is reading, not a call chain. So read the offsets, not
just the symbol names: a real frame offset advances by bytes of code, a table advances by a constant
small stride and repeats one symbol. The tool prints the offsets for exactly this reason.

A loaded image is mapped at a random address, so an absolute stack word is not an address inside
the image: the scan rebases it by `mapping_start - file_offset` before symbolizing. Skipping that
step made every shared-library frame resolve to a bogus symbol while still looking plausible.

### Catching a guest that dies in milliseconds -- the stop window plus `--catch`

For an explicitly requested short-lived-target diagnostic, the loader's
default-off `MLDR_STOP_AT_START` hook can establish a selective stop window.
Configure it through the owning workload's launch environment; do not globally
stop unrelated images or start a second guest to attach.

`dwdiag unwind --pid HOST_PID --catch SIGNAL --catch-timeout N` then uses the
existing bounded debugger route. The target PID, delivered signal, masks and
cleanup remain part of the evidence. A loader hold hook is not by itself proof
that GDB will receive the expected stop.

### `dwdiag waits` / `dwdiag unwind` contracts

`tests/waits-contract.sh` retains the object-history classification regressions.
The runner's native tests cover frame-pointer validation and the explicit scan
route. These references identify the owners of the checks, not results from an
unexecuted guest or a claim that candidate stack words form a backtrace.

### `dwdiag progress` -- where a run stopped

`scripts/dwdiag progress --state-dir JOB [--mode M] [--json]` reads a
finished managed job's finite registered captures and persisted runtime/prefix
selection. It never fills missing capture boundaries from mutable prefix logs,
the observer's environment or ambient processes.

For a standalone run, use `scripts/dwdiag progress --log RUN_LOG
[--guest-log MLDR_LOG] [--prefix P] [--server-log SERVER_LOG] [--mode M] [--json]`.
This combines the workload result, enabled loader stages and selected server
events. Process identity needs an explicit/recorded prefix and selected runtime;
current process observations are not proof about the captured past run.

Name the actual run/server-log paths and observation window. An absent log or
disabled receive-side instrument is unavailable evidence, not zero activity.
Counts from independently clocked files do not establish an interleaved order;
`--min-plane-publishes` is a configurable regression tripwire, not progress proof.

### Declaring a custom command's completion

`--cmd` replaces the fixture command. For a workload that prints an integer exit status, declare it with
`--status-marker FIELD=`. Without an explicit `--poll`, the harness waits for all declared status fields and does
not add fixture-only `HELLO=1`/`FINAL=1` requirements. Every declared status must be integer zero; missing,
unparseable or nonzero values fail. Additional explicit `--marker` assertions are still required.

An explicit `--poll REGEX` keeps its caller-chosen completion boundary. Ordinary presence markers do not infer
completion: they can describe an intermediate observation window. Without status declarations, the original
fixture defaults remain. Missing completion evidence is not proof that a command never started, and a spent
watchdog budget is not a measurement of compiler execution time.

## The RUN BUNDLE: one run, one self-describing directory, announced on stdout

`scripts/darling-boot-run.sh` writes a run bundle by default at `<log>.bundle`;
`--bundle DIR` selects an unused owned directory and `--no-bundle` suppresses
collection. The announced `BUNDLE`/manifest paths are the evidence locations,
not a fixed historical directory.

The manifest names harness output, the run-scoped server-log delta, available
guest captures, sampled child environment/descriptors, core references,
abort/terminate text, enabled diagnostic counters and the instrument census.
The server-log collector retains the original descriptor plus inode/size/boundary
information; append and rename rotation are distinguished from a complete
recoverable history.

Core references can be symlinks, not copied payloads. Preserve their required
targets when archiving. `RUN-ENV` is the fixed installed boot-component
fingerprint, not a hash of every custom workload/library or proof of mapped
image identity. Missing, rotated or unreadable data must retain its reason.

Final teardown observations are retained in `cleanup.json` and announced as the `cleanup` manifest item:
`state` is `clean`, `residue`, or `retained`; `processes`, `mounts`, and `layer_mounts` are the post-cleanup
counts, and `shutdown_rc` is the shutdown command's status. Isolation overlays are recorded separately as
`layer_mounts`, not residue. State follows the remaining processes/mounts, not shutdown rc alone.
With `--freeze-on-fail`, state is `retained` and all unmeasured counts/status are `null`, never fabricated zeros.
`verdict.txt` remains the final harness outcome, including workload checks, collection and cleanup: a clean
prefix does not turn a failed workload into PASS. An interrupted run without a cleanup receipt proves no cleanup.

Every item is a row of the bundle's `MANIFEST`: `PRESENT <path>` or `MISSING <reason>`. Rules:

* a piece the harness could not collect is **MISSING with the reason**, never silently absent -- including the
  directories that were searched, so the next reader cannot look in the wrong place;
* **INCOMPLETE** means collection has a known gap or finalization did not finish. Truncation/regrowth is reported
  with the available bytes retained; intermediate rotations absent at both boundaries cannot be recovered.
  The manifest ends with `BUNDLE-COMPLETE` only after successful collection and finalization;
* a `--bundle DIR` that already holds files is **refused**: a mixed bundle reads as this run's evidence;
* a silent instrument is NAMED (`BUNDLE-WITNESS silent=[...]`), and `build-declarations.txt` records where the build
  ITSELF says a section is absent, so "silent by build" is distinguished from "silent, and therefore broken";
* payload bounds are explicit: preserve the declared record length and any
  `XTRACE_MAX_PAYLOAD_BYTES` cap (default 64 KiB), and retain the printed
  truncation notice rather than silently treating a prefix as the whole record.

Contracts: `tests/run-run-bundle-contract.sh` (offline, stub launcher: the announcements, a `MISSING` row's reason,
the whole >512-byte abort line, launchd's lines quoted, the refusal of a dirty bundle directory).

### Finalized evidence storage

Completed bundle payloads use zstd level 3 when it saves at least 10% and 4096 bytes, including finalized
`.stdout` and `.asm.stdout` captures. Compression streams with bounded memory, verifies the decompressed SHA-256,
publishes an immutable index, and only then removes the unchanged raw file. The first publication is
`evidence-index.json`; later batches use exclusive `evidence-index.<id>.json` additions rather than rewriting
previous indexes or seals. Their merged mapping records logical basenames, stored paths, sizes and digests;
duplicate or conflicting mappings fail verification. Metadata, source files, live writers and symlinks remain
uncompressed. Original payload mtimes are retained, and the external live run log stays raw.

Only a wholly absent optional stream is skipped. A non-regular payload path,
missing indexed bytes or a corrupt index is invalid evidence, not absence;
verdict and failure-phase readers must not infer a result from another stream.

`dwdiag` reads raw or `.zst` logs, core files and retained symbolization images; a missing logical path resolves to
its `.zst` sibling. Seekable cores are decoded into an immediately unlinked temporary file, not a core-sized RAM
allocation. Other consumers can stream the same logical path through the shared helper:

```bash
python3 -B scripts/darling-evidence.py cat PATH
python3 -B scripts/darling-evidence.py compress --recursive evidence
python3 -B scripts/darling-evidence.py verify evidence --recursive
```

Recursive archival protects tracked files by default. An explicit `--include-tracked` migration permits only clean
tracked diagnostic payloads under a finalized evidence subtree; staged or working changes remain untouched.
Source/build/prefix/cache trees and metadata remain excluded. The host needs `zstd`. Writer inspection completes
the natural-user `/proc` census, then uses an isolated read-only `sudo -n` census only for inaccessible processes
and merges their writer identities. Root cannot replace the first census: user-owned FUSE mounts can deny root
while remaining readable by their owner. Any unresolved ownership inspection preserves raw files and reports
the reason rather than guessing that they are idle.

The helper's `compact-runtime UNIT --archive-root DIR` preserves a closed runtime unit's diagnostics, provenance
and content-deduplicated runtime images before separate West GC. Keep that archive outside GC roots and verify its
SHA-256 manifests. Generated objects/source forests are deliberately omitted and are not advertised as archived.
`verify` checks every immutable index publication, stored and decoded hashes, and old logical `SHA256SUMS`
references without extraction. Repeat compression validates existing stored payloads rather than re-pinning them.
For a closed archive without a top-level receipt, `--write-manifest` writes a new physical `SHA256SUMS` only after
verification; it refuses overwrite and never hashes that receipt into itself. Existing invalid receipts are errors,
not silently re-pinned hashes.

`verify` reports receipt integrity separately from coverage: `manifest_count` counts receipts found;
`unsealed_files` and `unsealed_paths` identify regular files in the selected `coverage_scope` that no receipt covers.
`failed: false` means the checked hashes agree, not that later files were sealed. A legacy logical checksum covers
its verified `.zst` replacement; storage-only compression can still add unsealed index metadata. No receipt means
the selected files are unsealed, even when their storage indexes validate. `created_checksums` counts rows newly
written by `--write-manifest`; `checksums` counts rows checked in existing receipts. The inventory excludes symlinks
and is a scan-time observation, not a writer lock: quiesce the owning writers before sealing. Keep later results
in a new addendum rather than overwriting an existing receipt.


### Reading foreign memory without killing the process being measured (xtrace)

Use `src/xtrace/safe_read.cpp` for traced memory. A length bound alone cannot
make a stale base address safe.

- The guarded raw `process_vm_readv` path copies from the target into owned
  storage rather than dereferencing the untrusted address while formatting.
- Unmapped/partial ranges produce an unreadable/short result and a diagnostic,
  not silent zero values.
- Message descriptor walks are clamped to declared `msgh_size`; truncated input
  stops decoding with `DECODE-STOPPED`.
- The unsupported 32-bit out-of-line-content path must remain explicitly
  unavailable, not fall back to an unsafe dereference.

`tests/run-xtrace-safe-read-contract.sh` owns the real-mapping guard checks and
the faulting unguarded comparison; merely refusing every read is not equivalent
behavior.

## darling-debug-runner: self-attributing run logs (`RUN-ENV` / `[dwdiag-env ...]`)

`cycle` and `verdict` retain the selected runtime fingerprint and guest-prefix
identity with the run. Compare that identity before comparing two results.
A build-tree hash is not installed-image identity, and the fingerprint is not
proof that every listed image was loaded. Missing identity must be established
independently or reported unknown.

### `dwdiag source`: refuse a build tree that does not compile the file you edited

```sh
scripts/dwdiag source --build "$B" --source "$SOURCE_FILE"
```

This inspects generated build-graph rules rather than trusting timestamps or a
target that reports no work. A source imported as a prebuilt binary cannot
exercise a local edit merely because its target is up to date. A refusal is
source/build evidence, not a product runtime verdict.

### Log decoding and prefix attribution

Log readers tolerate invalid UTF-8 through the shared lossy decoder; raw bytes
remain the evidence. A decoding failure is not proof that the workload never
ran. Preserve parser warnings and the original log.

Generic prefix resolution uses an explicit flag, then `DWDIAG_PREFIX`, then
the run's recorded prefix. Individual diagnostics can select their server log
separately: use the explicit run-scoped path described below, not an assumed
common default.

## Complete images and launch-failure classification

```sh
scripts/dwdiag --runtime-root "$R" deploy --build "$B"
```

This invokes the normal whole-product build/install, including optional
tests-data, into the absolute root configured in `B`. It does not take a guest
prefix or a per-file artifact list. Keep `R` immutable and disjoint from `P`;
changed code requires a newly configured image root.

Missing installed launchers, launchd execution failures, shellspawn readiness
failure and invalid prefix state are setup/boot failures, not evidence that a
workload reached its own hang or regression. Preserve the phase and exact
diagnostic instead of patching files into a running namespace.

## Server-log resolution

For a retained run, select its own bounded server-log slice explicitly:

```sh
scripts/dwdiag --runtime-root "$R" progress --log "$RUN_LOG" \
  --server-log "$SERVER_LOG" --prefix "$P" --json
```

Use the run's captured server-log slice or an explicitly verified run-derived
selection, and inspect the announced path/source. If no matching log is
available, server facts are missing: unrelated-prefix or ambient-default counts
cannot fill the gap. Pass `--server-log` when the exact retained slice matters;
prefix selection alone does not establish its time boundary.

Inspect the announced `SERVER-LOG-PATH` and its source. A missing/empty log means
server facts are unavailable. An accumulated live log also needs a run boundary;
counts from another prefix or another time window cannot explain this run.

## Installed workload inputs

`prefix` and `cycle` stage the selected installed fixture, not an ambient build
artifact. The ring fixture is built/installed by the normal product `cli`
member; optional `tests-data` provides additional lower fixture data.
Custom workload bytes must be explicit `--asset` inputs under guest
`/private/var/tmp`, with the destination and copied digest checked.

If the selected installed fixture is absent, staging fails. There is no
missing-build-artifact continuation or system-library injection fallback.
Standalone `verdict`/`suite` require their inputs to be prepared separately.

## `cycle --capture-wchan`

This optional host sampler covers the cycle's boot/workload window, sampling
prefix-associated process state and `/proc/<pid>/wchan` approximately once a
second. Association uses the current prefix identity helper, not a basename or
an argv substring alone.

Each repeated run resets its sample buffer. On a non-PASS result, output names
sample counts, distinct PIDs, each PID's last observed state and a bounded raw
tail. Read errors/unknown states are not evidence that nothing was blocked.
A process can disappear before the final sample, and host observation is not
a zero-perturbation guarantee.

`--gap-seconds N` spaces runs after their ordinary teardown; it does not replace
lifecycle ownership, demonstrate a host-I/O cause or excuse overlapping boots.

## Freeze-on-fail ownership

`cycle --freeze-on-fail` is an explicit diagnostic opt-in. It stops the series
at the first frozen failure and reports the retained run instead of beginning
another workload against that prefix.

The retained prefix is not clean acceptance. Keep its actual lifecycle owner
and job/evidence paths; observe through existing process/stack/log tools, then
finish cleanup through that owner. A frozen receipt's unmeasured cleanup values
remain `null`. Neither an external rescue nor a later successful cleanup can
rewrite the failed workload into PASS.

## Provenance witness and gate

```sh
scripts/dwdiag --runtime-root "$R" source --build "$B" --provenance --require-canonical
```

The selected mutable build/cache is identified separately from the immutable
installation. Provenance comes from the image's receipt: recorded source,
workspace/manifest, gitlinks, build/cache identity and actual image-integrity
results. A later change to `B` does not redefine which bytes image `R` contains.

`--require-canonical` refuses unverified bytes or unknown/dirty/mismatched
recorded provenance. A diagnostic run can still be useful when this gate fails,
but its workload result is not canonical acceptance. The provenance gate itself
does not exercise a workload.

Workspace source review is a separate tracked-only operation. Untracked bytes
are excluded/not collected; stage explicit new owned files before
check/package. A source-review package is not a full checkout, runtime image or
proof of independent reconstruction. Preserve accepted evidence outside
generated handoff/GC roots with its immutable checksums.

### Loader checkin diagnostic selection

`MLDR_COURIER_DIAG=1` enables the `[mldr-ctl]` checkin instrument.
`MLDR_DIAG_LOG` selects its sink; setting the sink alone does not enable that
instrument. Keep these explicit diagnostic settings default-off and retain the
actual run-selected sink. Unrelated loader lines do not prove that this
instrument was enabled, and a denial counter does not name its causal callsite.

## `dwdiag processes`

```sh
scripts/dwdiag --runtime-root "$R" processes --prefix "$P" --list --json
```

The current runner associates processes through retained prefix-directory
device/inode identity or exact `DPREFIX`/`DARLING_PREFIX` environment, excluding
its own ancestry. A shared installation executable or a process name is not
prefix identity. Read the listed executable/command details alongside the
run's actual PID/starttime evidence.

The count is a diagnostic census. Zero alone proves neither normal product
shutdown nor lifecycle cleanup; a deliberately frozen run is retained, not a
leak-free acceptance result. Use the existing owner's supported shutdown and
cleanup path, never a global kill or an in-place image overwrite.

## Long-job completion and evidence

Retain the supervisor/job handle, final rc, announced per-run logs/bundles and
cleanup receipt. A phase banner or stale job-board state is not completion,
and an absent one-time process sample does not establish cleanup.

Reconnect through the existing supervisor's `follow`; cancellation belongs to
that owner and must be followed to final status. Observer timeout is not job
cancellation. Do not start another prefix-backed run until the owning command
and lifecycle cleanup have finished.

## Attributing a hang that the log cannot explain

Plan one bounded owned run and its capture window before the workload exits.
A watchdog-expired run cannot retroactively supply the process state that
existed before teardown.

1. Identify the live subject through `dwdiag processes --prefix P --list` and
   the workload's own host identity, not a shared launcher path or guest PID.
2. Read this run's `progress` with an explicit server-log slice, then
   `dwdiag trace --log RUN_LOG --pid HOST_PID` for its recorded events.
3. Use the existing sampler or attachable-process `unwind` route for current
   wait/register/stack evidence. Permission errors, missing threads and scan
   candidates remain qualified observations.
4. For a crash, retain the matching log/core and reporting image.
   `dwdiag crash --log RUN_LOG --binary IMAGE` needs the actual reporting image,
   not a guessed `<prefix>/bin` path. A verified server image is
   `R/bin/darlingserver`; marker spelling alone does not prove attribution.
5. Finish product shutdown/cleanup through the run's owner and report the
   workload result separately from capture completeness or rescue cleanup.

The last printed event, a normal early return or recurring background traffic
does not by itself explain the subject's wait.

