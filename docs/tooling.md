# Diagnostic tooling for Darling guest/runtime work

Normative index. The reasoning and the measured incidents behind every rule here
are in `docs/direct-transport-descriptor-architecture.md` (sections 148-155 and
onward). The operational guidance an agent loads is the `darling-diagnostics`
skill; this file is the durable index it points at.

For the **interactive** counterpart to these batch tools — attaching to a live
runtime and asking what the server believes about one guest thread — see
`docs/darling-debugger.md`. The two are complementary: this file's tools produce
repeatable regression evidence, while the debugger inspects live state to form
the hypothesis that a regression then pins.

## Tools

| tool | answers | notes |
|---|---|---|
| `scripts/darling-boot-run.sh` | one measured run end to end | clean start, unique log, bounded completion wait, retained bundle and final verdict plus prefix cleanup. `--status-marker FIELD=` is sufficient for a custom workload: without explicit `--poll`, wait for all declared fields, without requiring fixture `HELLO`/`FINAL`. An explicit poll wins; additional `--marker` checks remain required. Status declarations pass only on integer zero; an earlier failure cannot be erased by a later zero or oracle hit. Callers without status declarations retain fixture defaults. `--hatch` enables the socket-disabled migration hatch, `--env K=V` adds environment, `--verify-probe TAG` gates deployed tag presence, `--assert-prefix-maps` refuses another prefix's runtime |
| `west darling-build --deploy-only --deploy-component NAME` (wrapper: `scripts/darling-deploy-verify.sh`) | are **all** runtime copies of a component the file just built | installs into every existing runtime directory and compares sha256 per copy; includes `libmdns`, `libnetwork`, `cfnetwork`, `installer`, `swift_foundation`, `launch` and `service_management` in both prefix roots; install a new framework's complete normal CMake layout before this byte-only deployment, rather than inventing links or accepting all-skipped destinations; `swift_foundation` stages the recipe-built payload, not a substitute for compiling its sources; `--component` **accumulates** |
| `scripts/darling-artifact-manifest.sh` | did any artifact change since a baseline, without git history | `--save`/`--check` over the build tree and every deployed copy; `--probe TAG` reports tag presence in every deployed copy |
| `scripts/darling-describe-artifact.sh` | where a Mach-O starts and what is really in it | `LC_MAIN` as a file offset (address = `__TEXT.vmaddr + entryoff - fileoff`), symbol address + disassembly, tag offsets, and identity of prefix copies. File-only `--pointer VM --arch x86_64 --json FILE` resolves a chained pointer with the selected slice's UUID, VM/file offsets, encoding, target and bounded C string; it validates chain membership and slice geometry. Unsupported encodings, external binds and invalid/unmapped slots fail explicitly, without guessing or requiring a prefix |
| `scripts/darling-trace-guest.sh` | what a short-lived guest process actually executes | samples at 30 ms, unions every sample, filters to processes that appeared after the launch, prints the mapped Darling libraries with their sha256 |
| `scripts/darling-probe-report.sh` | a run log as a probe BISECTION | groups probes by identity, reports UNBALANCED stacks (entered, following probe never appeared), the last events in order, and the run's own reporting; reading this by hand went wrong three separate ways |
| `scripts/darling-stop-reason.sh` | the recognized stop, context and product verdict from retained run evidence | reads the run log and bundle captures, including indexed `.zst` payloads, and emits text or `--json`. Nonzero guest, `--rc` or `--runner-rc` status overrides an oracle hit; unavailable evidence is reported and cannot produce PASS. Explicit guest `--rc=0` establishes completion; runner zero alone does not. License, loader and fault causes remain fatal; SQLite diagnostics with explicit successful guest completion remain context rather than the stop. SQLite codes are database results, not OS errno values. Product verdict is separate from parser exit: exit 0 means a named stop was recognized, 3 means none, not workload success. `darling-boot-run.sh` retains the report in its bundle. Preserve the log's first `[dwdiag-env prefix=… log=… bundle=…]` stamp: log-only diagnostics use it to select the run's server evidence |
| `scripts/darling-prefix-map.sh` | which prefix a process is rooted in | reads `maps`, because a guest process has no readable path |
| `scripts/prefix-cleanup.sh` | prefix-scoped cleanup and a cleanliness gate | three matching arms: `exe`, `cmdline`, and `comm` + `maps` for guest processes; `--dry-run` first |
| `scripts/guest-probe.h` | the probe contract | single literal tag, one syscall, registers preserved, and the Linux syscall-numbering rule |
| `scripts/closure_analyze.py` | what the Xcode load-time ladder's closure demands, and which route supplies each missing name | the generated manifest is the declaration source (`--declarations` reconciles it against CMake in both directions); `--sweep` groups missing install names by supplying route (`xcode-provided`, `no-referrer`, `weak-only`, `declaration`, `payload`, `source`, `substitute`) with supporting evidence. Source routing requires a matched definition, not just a header or similarly named umbrella; shortened-name matching must not confuse an X framework with `libswiftX.dylib`. `STATIC_CALLING`, `STATIC_NONCALLING` and `STATIC_UNATTRIBUTED` describe inspected code shape only; JSON records `evidence_tier=static`, `runtime_status=unverified`. A constant return, empty class or bounded source search does not establish genuine runtime behavior or observed absence |
| `scripts/closure_analyze.py --cover` | the load closure at SYMBOL granularity, and which unmet demand stops the loader first | walks the closure breadth-first from the entry, resolving every install name in the deployed prefix (BOTH roots) and then in Xcode's tree, decodes each image's imports (chained fixups or dyld-info -- one of the two commands prints nothing for each form), and labels every import: own export trie, the field image's NESTED re-export graph (aliases included), dyld's flat lookup for the special ordinals, absent-but-weak, or missing; `--order N` prints the predicted stop sequence (only an absent EAGER demand can stop the loader), `--without`/`--without-symbol` reconstruct a past state, and `--json` writes the graph. A `@rpath` name is expanded against the referrer's LC_RPATHs AND its loader chain's (dyld's `RPathChain`), and a field-less flat/weak lookup resolves against EVERY image the closure maps -- dyld binds only after the whole closure is loaded (`recursiveLoadLibraries` then `recursiveBind`), so resolving either against only the images walked so far under-reports (measured: libunwind's flat pair, SKIPC's `@rpath/SKSupport.dylib`) |
| `scripts/closure_missing.py` | the MISSING census of a `--cover` run: which strong eager import of the closure stops the loader, and who demands it from where | reads the cover graph and adds no resolution of its own, so a census is reproducible from the cover artifact alone: prints the label counts, then every `missing` row with its dependency FIELD, lib_ordinal, weak/eager flags, the referrer's sha256, and whether the referrer is a PREFIX or an XCODE image (the two routes a fix can take); the other non-exported classes are printed as context, and it exits non-zero while any row stands, so it gates before and after a change |
| `scripts/rung-driver.py` | the current loader rung and its structural verification | `stop --log LOGICAL_RUN_LOG --prefix OWNED_PREFIX` reads raw or indexed `.zst` evidence, parses the last missing image or symbol, and reports per-field demand with demangled shapes and weak/strong binding. Unresolved observed referrers or unknown referrers in a truncated stop fail with exit 2 and the searched prefix; unknown demand is not a `NO-REFERRER` non-blocker. Source-route evidence does not establish deployment. `verify --component X` checks deployed exports and nested re-exports against strong demands, including weak/upward-only referrers, and checks payload == staged resource == deployed copy. The built artifact's own export trie gates only with `--fail-on-built-miss`. Loader structure and artifact identity are not consumer-visible runtime proof |
| `scripts/west-pin-sync.py` | whether `west.yml`'s revisions match the darling superproject's gitlinks | `check` prints the before/after table and fails when a pin is behind its gitlink; `sync` rewrites only the `revision:` values of named projects (comments and formatting survive); a pin **ahead** of its gitlink is refused unless `--allow-downgrade` says so |

For closure analysis, `--xcode` accepts the selected `Xcode.app` bundle or its
`Contents` directory. With `--cover` or `--order`, `--json` writes one JSON
document to stdout, without the human table or predicted-order preamble,
including when an entry yields no readable images. Exit 0 means no modeled
loader stops, 1 means stops remain, and 2 means incomplete or invalid inputs.
This is static coverage of the selected entry, not runtime API behavior or
successful application compilation; later `dlopen` targets need their own
`--entry` or an observed guest run log.

The `cfnetwork` component includes public `NSURLSession` HTTP data tasks backed
by the existing `NSURLConnection`/CFHTTPStream transport, a shared session worker
and ordered delegate operations. Verify real response bytes, the supplied
delegate executor, cancellation and terminal object release; a completion
callback alone does not prove ownership cleanup. This backend does not supply
all of Xcode's private HTTP policies, upload/download task support or the complete
libmdns dependency closure.
The mDNS-consumed task host override routes through real CFHost/socket
properties without rewriting the request URL or HTTP Host. Its address and
hostname paths are loopback-exercised, including an IPv6 hostname destination,
replacement/clear ownership and terminal release. The original URL peer is
plumbed before TLS context creation, but TLS handshake/validation behavior is
not accepted by those plain-HTTP checks. No ignored policy setter substitutes
for missing transport support.
Default session configurations are independent values; a session snapshots its
configuration. Selected nil cookie/cache stores disable both reads and writes,
including redirects. Ephemeral configurations own separate memory-only stores.
The private header-suppression setting supports automatic `User-Agent` only,
case-insensitively, and preserves an explicit header. The reachability switch
controls the existing socket route checks; it does not implement their stubs.
Unsupported storage subclasses and nonnil credential stores fail before transport.
Nil credential storage is not proof of connection-authentication isolation.
Response cookies without Domain use their real origin URL. The existing cookie
provider still lacks URL filtering; these checks are not acceptance of complete
cookie or authentication policy.

## Rules, each measured

1. **Probe contract.** One literal tag (so presence is checkable), one syscall,
   every ABI register saved and restored besides `%rcx`/`%r11`. A probe that
   clobbers `%rax` or `%rdi` returns a **wrong result**, not silence. This applies
   to every string a probe emits, not just its tag: an identity marker assembled
   character by character (as one version did for `" sp="`) exists nowhere in the
   artifact, so the presence check reports the feature missing and the next round
   is spent on the wrong suspect.
2. **The raw `syscall` number in a Darling guest is Linux-numbered**: `write` is
   `1`. A probe using `4` runs as Linux `stat` and prints nothing.
3. **A probe must be in the artifact under test, and in the copy that runs.**
   Both are checkable without running anything (rules 1 and the two artifact
   tools). A silent probe is not evidence about the code path until both hold.
4. **Deploy every copy** and verify by content; `dyld` in particular is taken
   from `libexec/...` on some paths.
5. **A run can be served by another prefix's live runtime** -- the log looks
   normal and every conclusion is about the wrong artifacts.
6. **A guest process is not identifiable by path**: `exe` is `ENOENT`, `cmdline`
   is empty, `comm` is the stable name, `maps` is the only readable statement.
7. **A per-process diagnostic line is not a verdict.** Read markers and counters
   after the workload's own completion, and keep one log path per run.
8. **Check the premise before blaming the result** -- and when a measurement
   contradicts expectation, suspect the instrument first.
9. **A probe must not add a syscall, and must be portable across the arches the
   component is built for.** `libsystem_kernel.dylib` is built for `x86_64` *and*
   `i386`, so `%rsp` is rejected by the 32-bit pass. An earlier probe obtained a
   thread identity with `gettid`, i.e. an extra EMULATED syscall on a hot path,
   and the run being measured got **shorter** -- the boot stopped reaching the
   console at all. Identity comes from the address of a local buffer: free,
   distinct per thread and per frame, no register, no syscall.
10. **Attribute a probe, or its counts explain nothing.** Fourteen entries and six
   returns across several guest processes cannot say which one died; a probe that
   prints an identity (the buffer address above) turns a histogram into a
   bisection. Two different guests in one log looked like one inconsistent guest
   until the probes carried that identity. And the identity must be the **same
   kind** in every component: a launchd tag without one cannot be lined up with a
   dylib probe that has one, so the thread whose `open-entry` has no
   `open-postcancel` cannot be confirmed as launchd's.
   It must also be **per-thread, not per-frame**. An identity taken from the
   address of the probe's own local buffer changes at every call, so it cannot
   correlate a caller with a callee (`sys_open` and `sys_openat_nocancel` have
   different frames), and "does this guest reach the syscall layer at all" becomes
   unanswerable by identity. Use the TCB self-pointer (`%fs:0` on x86_64,
   `%gs:0` on i386): per-thread, stable across the whole call chain, no syscall,
   and no TLS runtime requirement. That is what made it possible to confirm that
   launchd's thread prints its own seven tags and **no** probe from
   `libsystem_kernel` at all after `CONSOLE_OPEN_BEGIN`.
11. **An image that names itself is not a client.** `llvm-objdump --macho
   --dylibs-used` prints the image's own `LC_ID_DYLIB` as its first dependency
   line (measured on `SharedFrameworks/AppThinning.framework/Versions/A/AppThinning`),
   so a referrer set built from that output alone contains the image itself —
   with no `weak` attribute, because an id is not a link. Counting it made a
   component look like its own **strong** referrer, which is exactly the signal
   the ladder's blocker test reads: a name only ever weakly linked would have
   read as a strong, load-time blocker. `closure_analyze.py` compares each
   image's own id (`--macho --dylib-id`) against the install name before counting
   it as a referrer, and a missing name with **no** referrer is its own route
   (`no-referrer`), never a "weak-only" absence.
12. **Xcode's own tree is not the prefix.** The guest reaches Xcode's files
   through the host-root mount, so an `@rpath/...` name, or a path that already
   names the mount, is supplied by Xcode and is **not** a prefix gap; an
   ABSOLUTE guest path (`/System/...`, `/usr/...`) never is. The guest's
   `/System` and `/usr` come from the prefix (`libexec/darling`), which is why
   the ladder exists at all, and Xcode ships images with colliding basenames —
   its own `libswiftIOKit.dylib` lives under
   `Toolchains/XcodeDefault.xctoolchain/usr/lib/swift-5.0/macosx`. A resolver
   that accepted those copies reported 61 of the closure's gaps as supplied; the
   strict rule (mount-marked paths and `@rpath` only, with a bounded search for a
   bare `@rpath` dylib or a nested framework bundle) reports the 15 the wave can
   actually act on.
13. **The generated manifest is the declaration source, and the CMake text is the
   second opinion — neither is trusted silently.** The manifest is the resolved
   declaration a prefix is actually filled from, and the only place a
   generated/payload declaration exists (the `CONFIGURE_DEPENDS` Swift runtime,
   the payload framework glob); the CMake text is where a declaration the prefix
   would never receive shows up as a **deployment gap**. Reading only the CMake
   text made the analyzer's new report contradict the prefix: it listed ~3.9k
   "manifest-only" declarations that were in fact CMake declarations spelled as
   `"<target>|<guest_path>"` pairs for a `foreach` (`cmake/RootlessHomebrew.cmake`),
   so the pair spelling and `cmake/*.cmake` are read too, templates (`${...}`) are
   skipped rather than guessed, and declarations of the OTHER rootless components
   (the bootstrap manifest) are subtracted instead of reported as this
   component's gap.
14. **A pin that moves backwards silently un-supplies work.** `west.yml`'s
   revision for a project is the only record of what the workspace runs; a
   gitlink is the superproject's record of the same thing. When they disagree the
   tool moves the manifest FORWARD, and refuses a pin that is **ahead** of its
   gitlink unless `--allow-downgrade` says so out loud — the measured case being
   the Swift payload, whose west pin carries the overlay commits the darling
   superproject had not yet recorded (so the gitlink is the older tree, and a
   blind sync would have removed the payload the guest was already loading).
15. **`llvm-objdump --macho --dyld-info` mislabels neighbouring GOT slots, and
   cannot express a weak import on a chained-fixups image — decode instead.**
   Measured 2026-10-12 on `SharedFrameworks/DVTInstrumentsFoundation.framework/Versions/A/DVTInstrumentsFoundation`
   (x86_64, `LC_DYLD_CHAINED_FIXUPS`): it attributed
   `__LSCopyMatchingApplicationsWithItems` to the slot whose import entry is a
   different `__LSC...` name, and 25 of its rows disagree with the image's own
   imports table (`_GRCCopyAllCounterSourceGroup` and friends read `bind` where
   the imports table's weak flag says `weak_bind`; a second Xcode image shows the
   same shape on `____chkstk_darwin`). A rung only caught it by decoding the
   fixups itself (format 6 = `DYLD_CHAINED_PTR_64_OFFSET`: bind bit 63, ordinal
   in the low 24 bits, page chains) and cross-reading the import table, with the
   caller's own disassembly as the tie-break. So no tool here takes a name, a
   field or a kind from that command: `closure_analyze.py` decodes
   (`image_imports`, `binds_by_dependency`), reads the text only to
   cross-check, and records the disagreement (`binds_disagreement`), which
   `rung-driver.py` prints next to the referrer it belongs to.

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

For namespace-aware live fault capture, use the supported top-level runner:

```sh
scripts/dwdiag run --name NAME --capture-gdb --capture-tree --gdb-namespace \
  --capture-pattern HANDLE --gdb-ex 'handle SIGSEGV stop print nopass' \
  --timeout-seconds 280 --env KEY=VALUE -- LAUNCHER COMMAND
```

Use the observable loader handle for HANDLE, not a guest argv absent from host
cmdline. Read the announced bundle; do not recreate namespace descriptors or
hand-roll gdb/nsenter discovery loops. The prefix must be in a supported stable
lifecycle state; read the boot skill before launching it.

## Why these exist

Each tool was written, used, and corrected at least once, because the first
version quietly did less than it claimed:

* `grep` without `-F` on a tag (`[launchd-MAIN]` is a bracket expression) plus
  `|| echo 0` turned grep's `Invalid range end` into "the probe is not in the
  artifact";
* `grep -c` returns 1 for "no match", which was read as failure;
* `--component` assigned instead of accumulating, so `--component a --component b`
  deployed only `b`, reported `ok`, and left `a` stale -- a whole run then
  measured the wrong dylib;
* the run harness matched its own command line (it is invoked **with**
  `--prefix`) and killed its own pipeline; `prefix-cleanup` had the same defect;
* a probe wrote its tag one byte at a time, so no tag existed as a string and
  presence was unverifiable;
* a probe used syscall `4` instead of `1` and silently did nothing.

When you find another, fix the tool, add the rule here, and record the incident in
the architecture document.

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
| `dwdiag deploy` | Build, deploy and run ONE workload, then report which instruments fired and which stayed silent: the four steps this stage repeats, with the prefix, the build tree and the artifact pair taken from the environment. Deploy the complete component set from ONE build tree into a prefix, verifying every copy by sha256 |
| `dwdiag cycle` | Options: |
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
| `dwdiag job` | Diagnose one finished run as a whole: census the instruments, say where it stopped, join the wait/wake vocabulary, look for a loop, then read a core or the kernel's report -- and report what the processes are doing now, per thread, with the kernel stacks when root is available. This exists so a job owner never has to know which of the other commands answers which question, which is how evidence gets missed |
| `dwdiag unwind` | Read a stopped guest's stacks: per-thread rsp from the debugger, then the words above it, filtered to guest images and symbolized. Host gdb cannot walk a Darwin-ABI frame, so this is how a stuck guest gets function names instead of a plausible story. With --catch SIGNAL it instead attaches, arms a stop on that signal, resumes the target and reports the trap |
| `dwdiag waits` | Join the server's whole wait/wake vocabulary per ulock object: who waits, who owns, and whether a wake was ever issued for it. A stall is not diagnosed by the last log line, and this table was re-derived by hand in every investigation until it became a command |
| `dwdiag trace` | The chronologically ordered story of ONE process in a run log: every line that names its pid or tid, plus the plane/urgent refusals around it. This is the question of this whole stage -- "what happened to this child?" -- and it was reconstructed by hand with grep/sed/python six times, each time risking the wrong slice of a two-million-line log |
| `dwdiag denials` | One row per DISTINCT denial in a run log, with the caller SYMBOLIZED and the post-fork context next to it. |
| `dwdiag prefix` | Prove a PREFIX rather than a working tree: bootstrap it, install the built artifacts with a sha256 check each, install the test assets, then run ONE workload on it. Prints a PREFIX-STAGE line before every step, so a gate that dies part way says where it died instead of going silent |
| `dwdiag processes` | Report the processes a PREFIX owns, matched by executable path AND command line: a name match that finds nothing is not a clean prefix, and the count this prints is the one a stale-process claim may rest on |
| `dwdiag trap` | Read the KERNEL's own report of where a guest died and symbolize the address inside a prefix. |
| `dwdiag cores` | Turn crash dumps on, off or report them -- and say why an unreadable crash is unreadable. |
| `dwdiag core` | Read a CORE and name the faulting function. A guest's ud2 runs NATIVELY, so Darling cannot report it as a syscall and four other routes measured blind on the same crash (silent dmesg, an armed-but-silent witness, a guest that masks SIGSEGV, and a catch waiting for a swallowed signal). The core is written by the kernel and read by gdb with no ptrace; it is also OFF by default here, which this command states rather than hides |
| `dwdiag identity` | Census the per-thread identity a core (or a live prefix process) holds: `__TSD_MACH_THREAD_SELF` per thread, with machine-readable zero/duplicate/unstable flags and a count per distinct value. |
| `dwdiag loops` | Report the pattern of a process that is NOT working any more: one line repeating near the end of the log with almost nothing new between its repetitions. |
| `dwdiag mappings` | Print a core's OWN mapping table (NT_FILE), one row per mapping, plus optionally the mapping that owns an address and the bytes the core carries there. |
| `dwdiag env` | Print the run's artifact identity line (`RUN-ENV prefix=... mldr=... libsystem_kernel.dylib=... dyld=...`): the sha256 of every DEPLOYED runtime copy, which is what answers "which build actually ran". |
| `dwdiag help` | error: unrecognized subcommand '--help' |
<!-- dwdiag-roster:end -->

Everything that used to be a separate shell script for this work -- judging a guest workload, running a set of them,
symbolizing an offset, resolving a crash -- is now a subcommand of the Rust tool in the sibling tooling repository:

```
scripts/dwdiag <build|cycle|symbolize|crash|verdict|suite|witness|waits|unwind> [OPTIONS]   # build-and-exec shim, stable path
scripts/dwdiag <core|identity> [OPTIONS]                                                     # read a core: the fault, and who each thread says it is
scripts/dwdiag <mappings|env> [OPTIONS]                                                      # a core's OWN NT_FILE table / the deployed-copy digest (RUN-ENV)
  = tools/darling-debug-runner/target/release/darling-debug-runner diag ...
```

### `dwdiag mappings` -- a core's OWN mapping table, because `readelf` refuses the note

```
scripts/dwdiag mappings [--core FILE|--dir DIR] [--prefix P] [--addr ADDR] [--read ADDR:LEN] [--json]
```

MEASURED why this is a command: the host's `readelf -n` REFUSES this core's 64-bit `NT_FILE` note ("Cannot decode
64-bit note in 32-bit build"), and the mapping list (or the bytes of a mapping) was then read by a throwaway script
written from scratch three times, twice with the wrong page/byte unit for the note's file offsets. The reader is the
one `core`/`identity`/`unwind` already use; this is its standalone face:

```
MAPPINGS core=/tmp/t4cores/core.2915140 size=895692800
MAPPINGS-COUNT source=NT_FILE count=1267 range-scale=bytes (decided from this core: 1267 of 1267 entries fall inside a PT_LOAD as bytes, 0 as pages) file-offset-unit=the note's declared page size
MAPPINGS-ROW start=0x5ee28f52c000 end=0x5ee28f52e000 file_off=0x0 name=<prefix>/libexec/darling/usr/libexec/darling/mldr
MAPPINGS-OWNER addr=0x5ee28f52c100 base=0x5ee28f52c000 file_off=0x100 name=...
MAPPINGS-READ addr=0x5ee28f52c000 len=16 bytes=7f 45 4c 46 02 01 01 00
```

An address in no mapping is reported as such (never as offset 0); a range the dump does not cover is refused with the
reason; a core with no `NT_FILE` prints `MAPPINGS-EMPTY` rather than zero rows.

### `dwdiag env` -- the deployed-copy digest on its own

```
scripts/dwdiag env [--prefix P] [--json]      # RUN-ENV prefix=... mldr=<12hex> libsystem_kernel.dylib=... dyld=...
```

The candidate table lives in ONE place (the tool), so `verdict`'s `RUN-ENV` line and the boot harness's bundle row can
never describe different sets of files. A prefix with no runtime installed prints the line and then exits 1 with
`RUN-ENV-EMPTY`, because an empty digest must not read as "the same build as the other run".

The tool is **vendored in this workspace** (`tools/darling-debug-runner`, source + README + `Cargo.lock`), so its code,
its documentation and every instruction that calls it live in one tree; the sibling repository of the same name is only a
fallback for an older checkout. Concrete, verified invocations:

```sh
# offset -> `symbol + offset` (server binary or guest dylib); `--addr` for an absolute address, `--json` to compose
scripts/dwdiag symbolize --binary ~/work/ringmm-build/src/external/darlingserver/darlingserver \
  --base-symbol dserver_crash_probe --delta 0x19d54c
# crash line -> location + stack walk + disassembly around the fault
scripts/dwdiag crash --binary ~/work/ringmm-build/src/external/darlingserver/darlingserver \
  --log $(ls -t /tmp/darling-boot-*.log | head -1)
# one workload, judged by its OWN line, naming the first denial and its caller
scripts/dwdiag verdict --prefix /tmp/dr-on-matched --mode sem_timed --args "300 1" --wait 60
# a set of workloads -> one table with denied/created per row (exit 1 if any row is not PASS)
scripts/dwdiag suite --prefix /tmp/dr-on-matched --wait-base 60 --require-zero-creations \
  -- 'sem_ready 2 :: sem_block 100 1 :: basic 20'
```

Why one tool rather than four scripts: they share two primitives (`llvm-nm` symbolization and reading a run log), they
must agree on one verdict rule, and every one of them was measured getting a verdict or a parse wrong in a way the others
could not see. The interface is meant to be composed rather than scraped:

* **one verdict rule** -- `RING_MACH_TEST mode=<M> ... pass=1`; the ABSENCE of that line is `FAIL`/`HANG`, never PASS
  The absence is classified by the WORKLOAD'S OWN exit status, which the guest shell prints (`__DWDIAG_RC=$?`):
  `CRASH <SIG>` + `rc=128+N` (died of a signal -- the cause, named), `EXIT rc=N` (returned without its line), `HANG`
  (no status at all), `NO-RUN` (never started). MEASURED 2026-09-27: `basic 2`/`basic 3` die of `SIGSEGV` at the
  second iteration's port teardown, which produced byte-identical evidence to a deadlock and was reported as `HANG`;
  the investigation then spent a long stretch chasing a lock that never existed. `--json` carries `rc` and `signal`.
  (MEASURED: a harness marker that matched a workload's START line reported PASS for two workloads that never finished);
* **`--json` on every subcommand**, so callers chain `crash` into `symbolize` instead of parsing text;
* **stable exit codes** -- 0 ok/PASS, 1 verdict or acceptance failure, 2 usage, 3 tool error;
* it **delegates** rather than reimplements: prefix start/stop stays in the boot harness (`--boot-runner`), and the
  symbol table stays `llvm-nm`'s, because a second implementation of either is a second source of truth.

Defects this tool found in ITSELF while being used, each fixed and kept as a rule:

* the boot harness was invoked as `--cmd "" <command>`, i.e. two arguments, so it exited instantly and the verdict
  reported `NO-RUN` -- a tool defect that looked exactly like a workload that failed to start;
* the JSON escaping handled quotes, backslashes and newlines but not tabs or other control characters, so a crash
  document with a disassembly in it was **unparseable** -- a tool that emits "json" a consumer cannot parse is worse
  than one that emits text, because the consumer trusted it;
* crash fields were parsed with a first-match-else chain, so `addr` was silently empty whenever it shared a comma-field
  with `sig` (`[dserver-CRASH sig=b addr=0x0`) -- a parse that looked right and dropped a field.


### `dwdiag build` and `dwdiag cycle` -- the iteration loop, with the paths out of the command

```
DWDIAG_BUILD=/home/ilyagulya/work/r1-repro-build DWDIAG_PREFIX=/tmp/r1-repro-prefix \
  scripts/dwdiag build --expect '[sigexc-in '
BUILD targets=libsystem_kernel.dylib,dyld rc=0 errors=0
  artifact .../libsystem_kernel.dylib stamp=12:33:08 bytes=2777588
  artifact .../dyld stamp=12:36:19 bytes=5755632

DWDIAG_BUILD=... DWDIAG_PREFIX=... \
  scripts/dwdiag cycle --mode basic --args 20 --repeat 3 --probe sigexc-in,native-exit
CYCLE[1/3] mode=basic verdict=CRASH SEGV (Segmentation fault) denied=0 created=0 rc=139
  instruments fired=[native-exit=2 sigexc-in=1] silent=[iter-marks] log=/tmp/dwdiag-verdict-...log
```

`build` exists because the pair rule is easy to violate: the same loader code is compiled twice, once into
`libsystem_kernel.dylib` and once into the `dyld` image, so building only one leaves the other stale -- measured
during this work, and it cost a full rebuild-and-run cycle to notice. `--expect STRING` verifies the instrument
string is present in the BUILT file, which is the check that turns "the probe printed nothing" from a mystery into
a statement about the artifact.

`cycle` exists because the four steps it composes were retyped as a long shell command at every attempt. The
artifact pair is the default, the build tree and the prefix come from `DWDIAG_BUILD`/`DWDIAG_PREFIX`, the verdict is
the workload's own line, and the instrument census comes from `witness` rather than from a grep: **silent** is
reported next to **fired**, because a probe that cannot speak is indistinguishable from a guard that does nothing.

### `dwdiag core` -- the one artifact that still has the thread in it

`scripts/dwdiag core [--core FILE] [--dir DIR] [--prefix P] [--log LOG] [--traces] [--json]` reads a core and
answers every question it can be made to answer:

```
CORE file=/tmp/t4cores/core.127004 size=705396736
CORE-SIGNAL sig=4 SIGILL (NT_SIGINFO si_signo=4 si_code=-6 si_errno=0 si_addr=0x3e80001f01c)
CORE-SIGNAL-CODE SI_TKILL: a THREAD sent this signal (tgkill/raise), so this is not the CPU's own trap code
CORE-THREAD faulting=lwp 127004 of 28 thread(s) (note order (no --log: the first NT_PRSTATUS is the thread the kernel dumped from))
CORE-REGISTERS rip=0x763d6c1131dd rsp=0x7fffffdfd580 rbp=0x7fffffdfd610 rdi=0x763d5799d988 gs_base=0x763d6f130060
CORE-GENERATED-BY /Volumes/SystemRoot/home/.../Developer/usr/bin/xcodebuild
CORE-MAPPINGS source=NT_FILE count=1174 range-scale=bytes (...) file-offset-unit=the note's declared page size
CORE-FAULT image=/…/prefix-verify14/usr/lib/system/libdispatch.dylib base=0x763d6c0a7000 file+0x6d1dd vm+0x6c1dd image_addr=0x6c1dd symbol=_dispatch_once_wait + 0x17d
CORE-INSN 0f 0b e9 00 00 00 00 48 -- ud2 (deliberate trap: __builtin_trap / unreachable) (...)
CORE-OPERANDS predicate_reg=rdi predicate=0x763d5799d988 once_word=0x101 once_word_reg=rdx masked_owner=0x100 masked_owner_reg=rcx check=consistent
CORE-OPERANDS-TSD3 gs_base=0x763d6f130060 slot=3 value=0x103 (*(unsigned long*)($gs_base+24) = __TSD_MACH_THREAD_SELF)
CORE-STACK frames=27 (walked through the core's own frame pointers and mapped through the core's own NT_FILE table, not through gdb)
  #0 fp=0x7fffffdfd610 0x763d6c0e0572 libdispatch.dylib file+0x3a572 vm+0x39572 dispatch_once_f + 0x122
  #2 fp=0x7fffffdfd6e0 0x763d5729f654 DVTFoundation file+0x57654 vm+0x53654 -[DVTToolsInfo + 0x1b
  …
CORE-STACK-STOP return address 0x8 is in no mapped image, so the walk ends there
```

**Why a core, when there is already `trap`.** A guest's `ud2` is a **native** instruction: the guest's code runs on
the host CPU, so Darling never sees it as a syscall and cannot report it. Four other routes were measured blind on
one crash: `dmesg` is silent because the guest installs its own handler; the loader's fault witness armed eight
times and never fired; the guest masks SIGSEGV on that path; and a gdb catch waits for a signal the guest
swallows. The core needs none of that: the kernel writes it, and its own notes carry NT_PRSTATUS (every thread's
registers), NT_SIGINFO (the signal AND how it arrived), NT_FILE (the mappings) and NT_PRPSINFO (the command line).

**The notes are read here, by neither `readelf` nor `gdb`.** `readelf -n` REFUSES this note on this host
("Cannot decode 64-bit note in 32-bit build"), gdb's `bt` cannot unwind a guest frame at all, and the previous
version parsed `info registers`/`bt`/`info threads` text. One reader now decodes the notes directly, which is also
what makes `identity` possible: `pr_pid` per note is the host LWP, and `elf_gregset_t` is the register file.

**The faulting THREAD is identified, and how it was chosen is printed.** Every NT_PRSTATUS note carries the dumped
signal (all 28 threads showed `si_signo=4`), so the signal cannot say which thread died; note ORDER does -- the
first PT_NOTE's note is the thread the kernel dumped from, which is the one gdb calls "Current thread". With
`--log` the guest's own `[sigexc-fatal ... tid=N]` outranks that, and the two are compared out loud: a log whose
mark names a different ip than the core's rip is reported as describing a DIFFERENT fault rather than folded in.

**The operands are BEST-EFFORT, and the rule is printed with them.** The faulting frame's `self`, once word and
TSD identity were derived by hand once and are the point of reading a `ud2` in `_dispatch_once_wait`; a core is one
instruction and the guest's libdispatch has no debug info, so they come from the register file. The rule is stated
(`CORE-OPERANDS source=registers-at-trap`): a register that POINTS AT a word whose low bits name an owner another
register also holds names `(predicate, once word, masked owner)`. MEASURED on `/tmp/t4cores/core.127004`:
`rdi -> 0x101`, `rcx = 0x100 = 0x101 & !3`, `rdx = 0x101`. Nothing that does not match is reported: an unmatched
register file prints `CORE-OPERANDS unavailable: <why>`, and a faulting frame that is demonstrably not a
`dispatch_once` wait is not attempted at all.

**The caller chain is walked by the tool, not by gdb.** Every return address is read from the core's own frame
pointers and mapped through the core's own NT_FILE table, with the validation stated (`CORE-STACK-STOP`): the chain
must ascend, and a return address in no image ends the walk instead of inventing a caller. Doing this by hand took a
python script over NT_FILE and a gdb round, and gdb's own `bt` on this core is `#0 0x… in ?? ()` followed by
pointers.

**Mappings are read from NT_FILE, never from gdb.** gdb lists NO mappings for a core whose image paths do not
exist on the host, and a guest's `/Volumes/SystemRoot/...` never does -- so `info proc mappings` came back empty
and the faulting address matched no image, while the note had all 1174 of them. The note's units are not assumed:
the RANGE scale is decided by how many entries fall inside a PT_LOAD **of this core** (bytes beat pages 1174 to 0
here, with no stack needed), and the FILE OFFSET is multiplied by the unit the note itself DECLARES in its header
-- MEASURED, the kernel writes `page_size=0x1000` with a page-numbered file offset while gdb's `gcore` writes
`page_size=1` with a byte one, and assuming 4096 made every gcore offset 4096× too large.

**A file offset is not a VM address, and the tool converts -- or says it cannot.** NT_FILE reports FILE offsets;
`symbolize`/`llvm-nm` address by VM address. Measured on the deployed `libsystem_platform.dylib` they differ by a
constant, because the dylibs are UNIVERSAL (`cafe babe`, x86_64 + i386) and a slice's segment offsets are
slice-relative: a core fault at file offset `0x3a9e` is VM `0x2a9e`, so an unconverted offset named
`__os_once + 0x5e` where the truth was `_os_unfair_lock_unlock_slow + 0x10e`. Both names look plausible, which is
why the translation is computed from the load commands, both offsets are printed (`file+0x… vm+0x…`), and a
non-Mach-O image (a host core's libc) is reported as NOT converted rather than symbolized at the wrong address.

**De-virtualization and symbolization are the command's job, not the reader's.** Guest paths are mapped back to
the prefix (existence-checked at every step), and EVERY caller is symbolized with its image, both offsets and
symbol -- getting the path or the scale wrong resolves nothing while looking like an answer.

**`crash` points here when a run died of a signal.** A log whose marks show a signal death says nothing about the
operands, so `dwdiag crash --log …` prints the core the run left, with the pattern DISCOVERED from
`core_pattern` (not `/tmp/t4cores`) and the core's age next to the log's:

```
CRASH-CORE pattern=/tmp/t4cores/core.%p (read from /proc/sys/kernel/core_pattern, not assumed) dir=/tmp/t4cores name-prefix="core."
CRASH-CORE newest=/tmp/t4cores/core.127004 size=705396736 (newer than the log: it can be this run's)
CRASH-CORE-NEXT the faulting frame's operands (the masked lock owner, the once word, the thread's TSD identity) are one instant and live only in that core: dwdiag core --core /tmp/t4cores/core.127004 --log …
```

A `|`-prefixed pattern is reported as piping the dump to a handler (no file is kept), and a core OLDER than the log
is flagged, because an earlier run's core sits in the same directory and reading it as this run's is how evidence
gets misattributed.

**It is OFF by default here, and `dwdiag cores` is what turns it on.** `ulimit -c` was 0 and `core_pattern` piped
to apport, so a crash wrote nothing and looked identical to a crash with no information:

```
dwdiag cores status    # the pattern, the limit, and a VERDICT: piped (nothing kept) or written to a file
dwdiag cores on        # file pattern under --dir, remembering the previous value
dwdiag cores off       # restore what was there
```

`on` reports the pattern, the limit and the remaining obligation -- `ulimit -c unlimited` must ALSO be set in the
process that crashes, because a file pattern alone still writes nothing. A leading `|` in the pattern is the only
thing that decides piped-vs-file; testing for `%` said "a file" about apport's own `-p%p` pattern.

Measured: with both set, a crash produces an `ELF 64-bit LSB core file, x86-64` and `dwdiag core` names the
faulting function; `tests/run-core-contract.sh` generates its own core this way (no guest, no prefix) and asserts
the mappings, the faulting thread, the instruction, the operand rule, the walked and symbolized callers, the log
cross-check and the `crash` linkage. The 670 MB guest core of a real crash is an OPTIONAL input to that contract
(`DWDIAG_GUEST_CORE=/tmp/t4cores/core.127004 bash tests/run-core-contract.sh`), which is where the acceptance
values (image address `0x6c1dd`, `ud2`, `self`/once word, and the chain on into DVTFoundation) are pinned; it is
never committed.

### `dwdiag identity` -- who each thread says it is

`scripts/dwdiag identity [--core FILE] [--dir DIR] [--pid PID] [--prefix P] [--json]` censuses
`__TSD_MACH_THREAD_SELF` -- TSD slot 3, i.e. `*(unsigned long*)($gs_base+24)` -- for every thread of a core or of a
live process, with the host LWP, the instruction pointer's image and symbol, machine-readable flags, and a count per
distinct value:

```
IDENTITY source=core=/tmp/t4cores/core.127004 threads=28
IDENTITY-THREAD lwp=127004 slot3=0x103 zero=false duplicate=true unstable=n/a ip=0x763d6c1131dd image=libdispatch.dylib image_base=0x763d6c0a7000 image_addr=0x6c1dd symbol=_dispatch_once_wait + 0x17d
IDENTITY-DUPLICATE value=0x103 count=26 lwps=127004,127021,… (these threads carry the SAME identity, so a lock held by one reads as held by the other)
IDENTITY-SUMMARY value=0x103 count=26
IDENTITY-SUMMARY distinct=3 threads=28 unreadable=0 zero=0
```

**Why it is a command.** This census was taken by hand for the libdispatch SIGILL with `thread apply all p/x`
piped through greps, and one of those greps silently dropped lines and produced a WRONG count -- of the number that
IS the answer (26 of those 28 threads share `0x103`). Two properties are therefore the point: it counts every
thread it lists, and a thread whose identity could not be read gets a stated FAILURE, never a plausible number.

**The flags and the zeros.** `zero=true` marks an identity of 0, which names nobody and clears whatever word its
unlock writes. `duplicate=true` marks a value shared by more than one thread -- and a ZERO IS NOT A DUPLICATE: two
threads at 0 are unset, not two threads claiming one name. `unstable` is observable only on a live target, and only
because TWO samples are taken with the process RUNNING in between (a stopped target cannot show a value changing);
in a core it is `null`, because a core is one instant. A thread seen in only one of the two live samples is
reported as churn rather than as instability.

**A live target has to be one this process may trace.** `--pid` reads `$gs_base` with gdb, which needs ptrace: with
`kernel.yama.ptrace_scope=1` a non-root tracer may only attach to its own descendants, so the census must be run as
the target's ancestor or through `sudo`, and an attach that reads nothing is an ERROR naming gdb's message rather
than a report of "0 threads". MEASURED on a HOST process: every `$gs_base` is 0 (Linux x86-64 keeps thread-locals in
FS, not GS), so every thread comes back as a stated read failure and the output says the target is not a GS-based
guest -- which is the honest answer, not a zero.

`tests/run-identity-contract.sh` covers both paths and the invariants above, on a target and a core it makes itself
with gdb's `generate-core-file` (so it never touches `core_pattern`, and cannot fight `run-core-contract.sh` for
it). A real GUEST census is the same optional input: `DWDIAG_GUEST_CORE=/tmp/t4cores/core.127004 bash
tests/run-identity-contract.sh`.

### `dwdiag loops` -- is the process working, or going round in circles?

`scripts/dwdiag loops [--log FILE] [--guest-log FILE] [--min N] [--tail-len N] [--spin-max-distinct N] [--json]`
reports patterns that repeat near the END of a log and how varied that end is:

```
LOOPS-VERDICT patterns=10 tail_lines=30 tail_distinct=5 tail=spin
LOOP count=89 span_lines=4533 distinct_in_span=133 first=1312 last=5845 line=[pc-postplane st=#- sp=#]
```

**Why this exists, MEASURED three times over.** "The log stopped growing" cannot tell a stuck process from an
EXITED one -- two runs in one session were read as a stall after the process had already exited -- and line
counts only say what a process has done, never whether it is still doing anything. The signature that actually
identified the failure was a handful of markers repeating for pages with nothing new between, and it had to be
found by eye.

**The deciding measurement is the tail's own variety, and it is measured, not chosen.** Two metrics were tried
and discarded first: repeats-versus-span-in-lines (always in the thousands, so it flags every busy log), then
variety over 200 lines (too wide to see the spin). The last 30 lines separate the two available runs: 5-6
distinct for the spin, 14 for the run whose process exited. Those are the defaults, and both are flags.

**Growth is not evidence about YOUR process.** MEASURED, in the same run where the linker had already died of
SIGILL: the harness log kept GROWING, because other guest processes in the prefix were printing, and the shell was
found parked in `do_wait` -- the child it was waiting for was gone. A run log belongs to the PREFIX, not to one
process, so neither its growth nor its flatness is a statement about the process you are investigating. This is the
mirror image of the earlier rule, and both point the same way: ask the process.

**A varied tail RULES OUT a spin and says nothing else**, so the verdict says `varied` and never `progressing`:
the run where the linker exited has a varied tail too. Whether a process is still working is a question for the
process (`proc=` from `dwdiag waits`, the run's own exit line), not for its log.

**Lines are normalized before counting** (a number starts at a digit and continues through hex digits), because
otherwise every repetition differs by a pid, a tid or a stack word and a loop of a thousand identical events
looks like a thousand unique ones. That normalization is contract-tested both ways: numbers must collapse, and
marker text whose letters happen to be hex digits (`pc-entry`) must survive -- starting a number at any hex
digit made it `p#-#ntry`.

### `dwdiag trap` -- the kernel already said where the guest died

`scripts/dwdiag trap [--kernel-log FILE] [--prefix PREFIX] [--process SUBSTR] [--limit N] [--json]` reads the
host kernel's report of a guest fault and symbolizes the address inside a prefix:

```
TRAP pid=1747380 name=mldr reason=trap image=libsystem_kernel.dylib base=0x778c21416000 size=0x9d000 off=0x485fa -> __simple_atoi16 + 0xea
```

**Why this command exists, MEASURED.** A guest fault is delivered by the host, so `dmesg` carries the process,
the reason, the image, its load base and the instruction pointer -- one line that names the faulting function
after one symbolize. In the session that added this command, that line sat unread while three custom probes, a
gdb catch and a stack scanner each produced a story, and two of those stories were wrong; the line is also
written when no core is kept (`ulimit -c 0` in this environment), which is exactly when the usual routes fail.

Two shapes are parsed, and both must be, because a kernel that writes one and a parser that reads only the
other reports NOTHING rather than an error:

```
traps: NAME[PID] trap invalid opcode ip:HEX sp:... error:0 in IMG[BASE+SIZE]
NAME[PID]: segfault at HEX ip HEX sp HEX error E in IMG[BASE+SIZE]
```

The second has no `traps: ` prefix, its pid is followed by a colon and its `ip` has no `=`; and both start
with a timestamp like `[  125.000000]` whose internal spaces broke the pid parse until they were stripped.

**Read `off` as the fact and the symbol as a hint.** `symbolize` answers with the nearest symbol AT OR BELOW
the address, so when the containing function is missing from the table the answer is the previous function
plus an offset larger than that function's own size (`__simple_atoi16 + 0xea`, for a function about 0x9a long).
The command says this in its own output instead of leaving it to the reader.

### `dwdiag waits` -- who waits, who owns, and whether anyone was ever woken

`scripts/dwdiag waits --log RUN_LOG [--json]` joins the server's whole ulock wait/wake vocabulary
per object: `ulock-wait-park` (who waits and who owns), `ulock-wait-cleanup`, `ulock-wake-entry`,
`ulock-wake`, `ulock-wake-nobody`, `ulock-wake-enoent`, `ulock-wake-late`. It exists because a
stall is not diagnosed by the last log line: the question that decides the diagnosis is whether a
wake was ever issued for the object at all, and whether the server had anybody to wake when it was.

```
scripts/dwdiag waits --log /tmp/t4.log
WAITS log=/tmp/t4.log objects=2
OBJECT pid=333962 addr=0x7c4b803a3e00 op=UL_UNFAIR_LOCK owner_tid=333962 waiters=[333964] parks=1 cleanups=0 wake_entry=0 wake=0 woke=0 wake_nobody=0 wake_enoent=0 wake_late=0 verdict=NEVER-WOKEN
WAITS-STALL pid=333962 addr=0x7c4b803a3e00 op=UL_UNFAIR_LOCK owner_tid=333962 waiters=[333964] reason=no-wake-was-ever-issued-for-this-object
WAITS-VERDICT objects=2 ulock_lines=2 stalling=2
```

The verdicts are deliberately different defects, not degrees of one: `NEVER-WOKEN` means nobody
asked to wake the object; `WAKE-MISSED-THE-WAITER` means a wake was issued and the server found
nobody (`wake-enoent`/`wake-late`/`wake-nobody`); `WOKEN` with `woke=0` means it ran and nothing
moved; `NO-WAITERS` means the object was never contended. A `WAITS-CYCLE` line is printed only
when both directions are present in the same object set, so a one-sided wait is never called one.

Because it consumes instruments that already existed, the same question is answered by
`witness`: a log where `ulock-wait-park` fired and every `ulock-wake*` stayed silent says the same
thing in one line, and that is the case the T4 stall is in.

**A stall line describes a park that HAPPENED, so every one carries `proc=present|absent`.** A run
where the T4 linker died instead of stalling still printed `WAITS-STALL`, because these lines read
the log -- and taking that for a current stall sends the next investigation at a dead process. When
every stalling object belongs to a process that is gone, a `WAITS-NOTE` says so in one line.

Presence is matched by the THREAD IDS the record carries, not by the pid, and that is measured: this
host has `/proc/100`, a kernel thread whose tid is also 100, so a fixture park logged by pid 100 came
out `proc=present`. A bare pid is not evidence that the process a record describes is still there.
The contract builds both cases from the running shell's own pid, so it cannot drift with the host.

### When NOT to reach for `--catch`: a masked signal is not catchable

MEASURED, twice, at the cost of two full runs: a catch session on a guest from this environment printed the
stop and then nothing -- no registers, no frames -- and ended on its own timeout. The guest trace explains it:
the faulting path masks the signal (`[sigsegv-mask how=1 ...]`), so gdb's `continue` never returns on it and a
session that waits for a stop waits forever. The kernel still records the fault, so `dwdiag trap` is the route
for "how did this guest die", and `--catch` is for a signal that is actually deliverable.

### `dwdiag unwind` -- guest frames, which host gdb cannot walk

`scripts/dwdiag unwind --pid PID [--image SUBSTR] [--window BYTES] [--max N] [--json]` reads a
stopped guest's stacks: per-thread `rsp`/`rip` come from the debugger, the words above each `rsp`
are scanned, and any word that falls inside a guest image past that image's first 16 KiB **and
resolves to a symbol** is reported as a frame.

It exists because the three obvious routes do not work, each measured: host gdb attached to a
stopped guest resolves only the loader's syscall trampoline; `/proc/<tid>/maps` `[stack]` is
present only for the main thread; and `startstack` in `/proc/<tid>/stat` is zero for non-leaders.
Two of those mistakes produced "frames" that were image data (`image_base+2`), which is why both
filters are stated in the output rather than left implicit.

**Frames come from the frame pointer, and the scan is a labelled fallback.** The guest closure is built
`-O0`, so `rbp` is a real frame pointer and `[rbp]`/`[rbp+8]` walk the chain properly; every step is validated
(above `rsp`, 8-aligned, monotone, landing in an executable guest image), so a garbage word cannot invent a
caller. Each thread therefore reports `frames_via=fp` or `frames_via=scan`, because those are different kinds
of evidence: MEASURED, a scan-only unwind of the T4 linker printed `gr_attach_lane ->
__dserver_plane_request_once -> dserver_rpc_explicit_ulock_wake -> dserver_rpc_explicit_debug_list_processes
-> _dispatch_source_xref_dispose`, which puts SERVER-side names inside a guest stack in an order no thread
could run. A `scan` result is a set of plausible frames, not a call chain.

**The order of the frames is depth, and that had to be fixed.** The words are scanned upward from
`rsp`, so the deepest (innermost) frame comes first. The command used to sort those words, which
orders them by code ADDRESS instead and turns a call chain into a set -- two runs reported "chains"
in which an unrelated function read as the caller of any other. The scan order is the answer now,
and a unit test pins it (`stack_words` in `tools/darling-debug-runner`): the innermost word in the
test is the smallest, so a sort would reorder all three and fail.

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

A failure whose victim lives a few hundred milliseconds cannot be caught by watching: by the time a
poll sees the process it is gone (measured: `no ld appeared`, and a stall-triggered capture reported
`RESULT=exited`). The supported sequence is two pieces:

```
# 1. the guest command exports a SELECTIVE window: only this image stops before running
export MLDR_STOP_AT_START=XcodeDefault.xctoolchain/usr/bin/ld
# 2. attach to the stopped target and catch the signal it dies of
scripts/dwdiag unwind --pid PID --catch SEGV --catch-timeout 150
```

`MLDR_STOP_AT_START` is a substring of the target's argv (`"*"` keeps a global stop, which is
useless in practice: it halts the shell that would launch the target). `MLDR_HOLD_ON_SIGNAL` also
exists -- the loader fault witness pauses instead of restoring `SIG_DFL` -- but it does NOT work as
a capture, and that is measured: the re-arm loop and the runtime's own install alternate every
20 ms, so at delivery time the runtime handler swallows the signal and no witness line appears. The
window plus `--catch` is the mechanism that works; gdb must be told to pass `SIGSTOP` through
because the target is already stopped by it.

Measured result of one such catch on the iOS linker: `Thread 5 "mldr" received signal SIGILL,
Illegal instruction.` followed by `SIGSEGV, Segmentation fault`, with `rip`, `rsp` and the host LWP
-- i.e. the trap arm is not silent, it dies after an illegal instruction.

### `dwdiag waits` / `dwdiag unwind` contracts

`tests/waits-contract.sh` carries one object of each verdict class (never-woken, wake-found-nobody,
woken) and checks that only the first is reported as stalling. It caught a real defect before its
commit: an object whose wake found nobody was reported as never-woken, which contradicts the
distinction the command exists to make.

### `dwdiag progress` -- where a run stopped

`darling-workspace/scripts/dwdiag progress --log RUN_LOG [--guest-log MLDR_DIAG_LOG] [--mode M]` answers the question a
stalled acceptance run always raises, from the two logs the run already wrote, without a shell pipeline: whether the
workload produced its own machine-readable line, the last `[mldr-ctl]` stage the guest loader reached (a `seq=N after-*`
bootstrap stage, the request it published, or the spin it is in), and the last plane op the **server serviced**.
Published-versus-serviced is the diagnosis: a request that the guest published and the server never serviced is a
server-side stop, and one the server serviced while the guest still waits is a completion that did not land.
`--json` is supported; `verdict` composes the same summary into a `VERDICT-STAGE` line whenever its verdict is not PASS,
so a HANG no longer needs a manual grep to be readable.

### 2026-09-27: two harness defects that made a run unreadable

* **`--marker` split multi-word markers.** The boot harness stored markers space-separated and re-split them with a
  plain `for`, so `--marker 'ITER 0 dropped'` became `ITER`, `0`, `dropped` and reported `MISS dropped` for a
  marker that was never queried as a whole. Markers are `|`-separated now and the IFS change is restored after the
  loop.
* **A probe on a pre-libc path used `getenv`.** A diagnostic added to the guest's `mach_msg_overwrite` trap read its
  hatch with `getenv` on first call; `mach_msg` runs during bootstrap, so the boot stopped reaching the workload
  (`Rootless shellspawn did not become ready within 30000ms`) and the harness reported only a generic failure. It
  now scans `/proc/self/environ` with raw syscalls, which is what `dserver-ring.c` already did. Any probe reached
  before libc MUST use that scan.

### 2026-09-27: the guest must be able to describe its own death

A guest crash is invisible from the host: guest tids are not host pids (so `/proc/<tid>` sampling cannot name the
faulting thread) and a `SIGSEGV` inside Darling's emulated-syscall window does not reach a handler the workload
itself installed -- the program-level reporter added to `ring_mach_msg_test` (signal, fault address, fault PC,
returning frames with image+symbol, `sigaltstack` for stack-overflow faults, `_exit(128+sig)` because `raise()` is
a denied `pthread_kill` under the hard-socket hatch) prints nothing in that case, while it does print for a
deliberate fault (`crash_test` mode). A guest fault reporter therefore belongs in the guest's own signal-exception
layer (`emulation/src/linux_premigration/signal/sigexc.c`, which already has register/mcontext helpers), not in the
program under test.

### 2026-09-27: a fatal guest signal had NO observability in a normal build

`sigexc.c` wraps every diagnostic in `kern_printf`, which is defined to **nothing** unless `DEBUG_SIGEXC` is
compiled in. The path that actually kills a guest -- default-effect signal handling -- therefore produced no line
anywhere, and hours went into reading a silent death as a deadlock. The file now emits one **bounded, raw** line per
fatal signal number per process:

```
[sigexc-fatal sig=15 code=0 addr=0x3E80018FAE6 pid=1636880 tid=1636880]
[sigexc-default sig=15 tid=1636880]
```

`__simple_fprintf` is a plain write, safe from a raw handler on `sigexc_altstack`; the once-per-signum bound keeps a
signal storm from flooding the log. Validated by running the shape it exists for: a deliberate guest fault
(`ring_mach_msg_test crash_test`) reports `sig=11 code=1 addr=0x0` for the faulting process **and** the fixture's own
reporter reports the same process -- two independent instruments agreeing.

Two rules follow from using it:

* **`sigexc.c` is compiled TWICE** (`emulation.dir` -> `libsystem_kernel.dylib`, `emulation_dyld.dir` -> the `dyld`
  image). Rebuilding `system_kernel` alone leaves the loader copy stale (measured: the `emulation_dyld` object was six
  hours older and contained no instrument), so a diagnostic in this file needs `--target dyld` and a deploy of **both**
  prefix copies, sha256-verified.
* **The harness's own stop signal is not the subject's death.** Every run of a hanging workload shows
  `[sigexc-fatal sig=15 code=0 ...]` for a *neighbouring* guest process with `code=0` (`SI_USER`, `uid=1000`, the
  sender's pid in `si_addr`): that is the tool stopping what it started. A verdict about the subject must name the
  subject's own pid.

### 2026-09-27: probes that killed the process they measured

Two diagnostics added to `ring_mach_msg_test` stopped the workload at the probe itself, and both were caught only
because the marks that should follow them disappeared:

* `pthread_sigmask(SIG_SETMASK, NULL, &cur)` -- `SIG_SETMASK` with a NULL set is undefined; the call never returned.
  The defined query (block an empty set, take the previous mask) works and is what the probe uses now.
* `sigaction(SIGSEGV, NULL, &old)` -- the disposition *query* did not return in this guest; the mark after it never
  appeared. The probe was removed rather than guessed at.

Keep every probe bracketed by its own mark, so a probe that breaks the subject is visible as a missing mark instead of
as a missing crash.

### 2026-09-27: `--repeat` and an explicit `LOG=` line (a verdict that cannot see flake)

The verdict command gained `--repeat N`, one `LOG=<path>` line per run, and a `STABILITY mode=… runs=… pass=…
stable=… distribution=…` summary; the exit status is non-zero unless every run passed. The same command produced
`PASS` and `CRASH SEGV` on consecutive runs all session, and each single-run reading was treated as a change in
behaviour. Any claim about a failing shape must now come from repeated runs.

`scripts/dwdiag` also rebuilds when the source is newer than the binary and prints `tool=… binary=…`. It previously
built only a **missing** binary, so an edited tool kept answering from the stale one (a new flag was absent from
`--help` while the sibling repository's copy had it).

### 2026-09-27: an oracle that reported PASS for a workload that never ran

`ring_mach_msg_test delay 0 N` read its iteration count from `argv[delay_ms ? 3 : 2]`, so with `delay_ms == 0` it
took the count from `argv[2]` -- the **delay** -- ran zero iterations and printed `pass=1`. Four such runs were read
as "the destroy is safe with a delay". The index is now a property of the mode, and `iters == 0` prints
`pass=0 error=no-op` and exits non-zero: a workload that did not execute must never report success.

Measured consequence, after the fix: `delay 0 1` and `basic 1` are the same workload and both crash, while
`delay 300 1` really runs and passes.

### 2026-09-27: the guest Darwin-syscall trace, and the two ways it broke the guest

`threadnoop 2` proved the current failing subject without ports, messages or destroys: **create a thread that
does nothing, twice** -> `CRASH SEGV` 4/4, and the death is inside the *second* `pthread_create` (the first
create/join completes; `[bsc wrap pre]` never prints for the second, and the server sees nothing for it). To name
the Darwin syscall reached there, the dispatcher gained an opt-in trace (`DARLING_GUEST_SYSCALL_TRACE=1`) in the
13-byte entry hook of `__darling_bsd_syscall` -- the same hook xtrace patches, occupied statically as `jmp rel32`
plus 8 NOP bytes.

Two implementations were MEASURED breaking the guest, and both lessons are now enforced by
`tests/run-guest-syscall-trace-contract.sh`:

* **The trace captured itself.** The first version printed with `__simple_printf`, whose output takes a `write`
  syscall through this very dispatcher: 21 of its first 28 lines were the emitter's own writes and the guest
  stopped starting (NO-RUN 3/3). The trace therefore emits with a RAW Linux `write` and formats its own line (the
  discipline the plane probes already use), and the contract fails if one syscall number dominates the trace.
* **The trampoline is copied, not invented.** A hand-rolled save/restore (7 pushes, `subq $16`) turned every run
  into NO-RUN with the hatch *off* as well, i.e. it corrupted syscall delivery globally. The implementation now
  copies xtrace's proven `trampoline_enter`/`trampoline_leave` pattern for this exact hook, and the contract's
  first claim ("the instrument preserves the guest when it is off") is the oracle that catches it.
* **The hook is a `call`, not a `jmp`.** MEASURED: a `jmp` into the trampoline is followed by the trampoline's
  `ret`, which returns to the *syscall's caller* and skips the entire dispatcher -- every Darwin syscall then
  returns garbage and every run is NO-RUN, hatch or not. xtrace installs this hook with
  `setup_hook(..., jump=false)`, i.e. a call; the mnemonic is the parameter's name. The 13 bytes hold
  `call rel32` + 8 NOP exactly as well as a `jmp` does, and a semantics-preserving `jmp` inside the same 13 bytes
  was measured to work, which is what separated "the hook area is not statically occupiable" from "my jump kind
  was wrong".

The contract is registered in `ci/run-host-tier.py` as an EXCLUDED contract with its reason: it needs a booted
prefix and a guest runtime, which the host tier does not own.

### 2026-09-27: instrument the branch that is COMPILED

`bsdthread_create.c` builds with `-DBSDTHREAD_WRAP_LINUX_PTHREAD` (measured in `build.ninja`), so its
raw-`clone` branch is compiled out entirely, and the `emulation_dyld` copy of the whole file is excluded by
`#ifndef VARIANT_DYLD`. Marks placed in the dead branch are invisible by construction while looking perfectly
correct in the source; check the built object (`strings` on the `.o`) before trusting any instrument in a file
that is built more than once.

### 2026-09-27: the dispatcher trace is real, and its COVERAGE is measured, not assumed

`tests/run-guest-syscall-trace-contract.sh` is green: the guest is unaffected with the hatch off, the traced arm
still completes, the log carries thousands of `[bsys nr=` lines covering dozens of distinct syscall numbers, and one
number does not dominate (the self-tracing signature). The contract's own first version had a defect worth keeping:
the verdict list arrives space-separated, so `NO-RUN PASS` satisfied a "no verdict without PASS" check and a run that
never started counted as "the instrument preserves the guest"; every verdict token must now be exactly `PASS`.

Calibration of what the trace does and does not see (measured, `threadnoop 1 --env DARLING_GUEST_SYSCALL_TRACE=1`):

* 10241 traced lines in the run, 81 distinct numbers -- the instrument is not silent;
* for the WORKLOAD's own thread, only 6 lines, and **no `nr=4`** (`write`) although that thread printed five marks
  through `printf`. The same `nr=4` floods appear for the harness's own binaries in the same run.

So the trace covers the `call __darling_bsd_syscall` entry (which `SYS.h` emits for every generated stub when
`DARLING` is defined) and does NOT cover whatever serves the workload binary's own `write` -- i.e. a second entry
that must be found before this tracer can be used to say "the second `pthread_create` issues no Darwin syscall".
Until then, "absent from the trace" is a statement about this entry only, and it must not be read as "no syscall".

### 2026-09-27: ROOT CAUSE of the session's silent guest death (fix applied and verified)

`_pthread_deallocate` (guest `libsystem_pthread`) freed the thread's stack+pthread-object region with
`mach_vm_deallocate(t->freeaddr, t->freesize)` -- called from the **joining** thread -- while the loader
(`darling_thread_entry`'s exit branch) already `munmap`s exactly that region (`t_freeaddr`/`t_freesize`, the
arguments the guest passes to `__darling_thread_terminate`), from the thread that is actually finished. Two owners,
two frees; the guest's fires while the memory is still live (the exiting thread's loader-side teardown and
libpthread's own bookkeeping read the pthread object that sits above the stack in that same mapping).

Signature it produced, all measured: a **silent** SIGSEGV (rc=139, no handler report, no server RPC) whose location
varied between the next `pthread_create`, the syscall dispatcher and the program's exit path; it needed a
create+join cycle (`threadnoop 2`, `basic >= 1`, `stress_churn`, `basic`'s helper thread) and passed with no thread
(`selfdrop`, `timeout`, `sem_ready`). glibc reported `free(): invalid pointer` in a crashing run once
`MALLOC_CHECK_=3` was set, which is what named the class.

Decided by measurement, not argument:

* disabling the **guest** deallocation (`DARLING_GUEST_NO_PTHREAD_DEALLOC=1`) -> `threadnoop 2`, `basic 3`,
  `stress_churn 3` all `PASS 3/3` stable;
* disabling the **loader** unmap -> still `CRASH` (so the guest's free is the harmful one);
* with the fix committed (deallocation removed, ownership left to the loader), no hatch involved:
  `threadnoop 2`, `basic 1`, `basic 3`, `delay 0 1`, `stress_churn 3`, `selfdrop 2`, `timeout 200 4` are all
  `PASS 3/3 stable`.

Lessons kept as rules: an mmap'd region's lifetime has ONE owner; a hatch that removes a free is how a double free is
decided (and it must be a hatch, not a guess); and `MALLOC_CHECK_`/`MALLOC_PERTURB_` in the loader's environment is
the cheapest way to make heap corruption speak.

### 2026-09-27: the workload's line must name the mode the CALLER asked for

`sem_gap <ms> <n>` runs `sem_block`'s implementation and reported `RING_MACH_TEST mode=sem_block ...`, while
`dwdiag verdict --mode sem_gap` looks for the mode it was asked to run. A **passing** 5.001 s semantic wait was
therefore reported as `EXIT rc=0 :: <no result line>` -- a false acceptance failure, and the kind of misreport that
makes a green workload look broken. The result line now carries the requested mode (`run_sem_block` takes its label
from the caller). Same rule as the verdict rule itself: the workload's own machine-readable line is the oracle, so it
must answer the question that was asked.

### 2026-09-27: a denial is a migration signal even when the row PASSES, and a row flake must not decide the suite

Two tool defects, both measured on the acceptance table after the `_pthread_deallocate` fix:

* `sem_gap 5000 1` passed 3/3 **while one run reported `denied=1`**, and the tool printed the denial's call site
  only for non-PASS rows -- so the one fact needed to remove the last datagram dependency was hidden by a green
  verdict. `verdict` now prints `DENIAL denied=<n> first-denial=<call> caller=<location> verdict=<v>` whenever
  `denied > 0`.
* Two rows failed in one suite run (`sem_gap 5000 1` HANG, `basic 20` NO-RUN) and then passed 3/3 and 4/4 when run
  individually, i.e. a row-level flake decided the suite verdict. `suite` retries a row that is not PASS **once**
  and prints `ROW-RETRY mode=<m> first=<v1> retry=<v2>`; the retry is announced, never silent, because hiding the
  first verdict is the same defect as a verdict that cannot fail. `--retry-failed-rows false` turns it off.
* A suite verdict is provenance, and provenance that lives only in terminal scrollback cannot say which build it
  belongs to. `suite --record-receipt <prefix>/.darling-deploy-receipt.json` merges this run into the receipt the
  deployment path wrote: one `runtime_verdict` field (kind, verdict, rows, failures, retry setting, zero-creation
  setting and the per-row table), printed as `RECEIPT-RECORD <path> verdict=… rows=… failures=…`. The receipt
  belongs to `west_commands/deploy_receipt.py`, so the tool adds that one field and preserves every other byte; it
  **refuses** a receipt that already carries a verdict, because a verdict is evidence about one build and
  overwriting one makes the receipt describe a run it never measured, and it never creates a receipt. Absent the
  flag nothing is recorded: a run that records nothing says nothing rather than inventing provenance.

### 2026-10-04: a workload that is not installed is not a hang, and `/usr/bin` is not where the fixture lives

`dwdiag verdict` defaulted to `--guest-command /usr/bin/ring_mach_msg_test` -- the one path this tool's own
documentation measures as INVISIBLE to a guest, because the guest's `/usr/bin` resolves through
`/Volumes/SystemRoot` to the HOST's `/usr/bin`. Measured cost: a 200-run boot gate used that default, every run
burned its full watchdog and reported `HANG (watchdog)`, and four hours of "product failures" were one wrong
path; the fixture was installed, in the prefix, at `/private/var/tmp/ring_mach_msg_test`, which is where `prefix`
and `suite` already look.

* The default is now `/private/var/tmp/ring_mach_msg_test`, the guest-visible writable path.
* `verdict` checks the workload's presence INSIDE THE PREFIX before it pays a boot: absence prints
  `VERDICT-PREREQ workload=absent <host path>` and returns `NO-RUN (workload absent)` in seconds (measured: 5 s
  against the 200-run gate's ~71 s), so a wrong path can no longer be laundered into a wait then a verdict about
  the workload.
* A guest shell's `rc=127` (command not found) is classified `NO-RUN rc=127 (workload not found)` instead of
  `EXIT rc=127`, which read as the workload's own exit status.

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

`scripts/darling-boot-run.sh` writes a **run bundle** on every run (`<log>.bundle`; `--bundle DIR` to move it,
`--no-bundle` to suppress). No extra flag is needed, because the failures it removes were all "the evidence existed
and nobody knew where it was":

```
BUNDLE dir=/absolute/owned/state/bundles/demo/harness.log.bundle
BUNDLE-ITEM guest-capture-prd-minproj-bare-t.trace.3081254 path=.../captures/... note=captured from <prefix>/private/var/tmp/... (3169724 bytes)
BUNDLE-COUNT guest-captures path=.../captures 10 capture(s) written during this run ...
BUNDLE-COUNT child-records path=.../children 15 guest process(es) sampled from the HOST while alive ...
BUNDLE-MISSING core reason=no new core under /tmp/t4cores during this run ... (ulimit -c 0)
BUNDLE-ITEM witness-census path=.../witness.txt note=instruments fired=14; SILENT=[fault,segvdisp,...]
BUNDLE-RUN-ENV RUN-ENV prefix=... mldr=1baff608437b libsystem_kernel.dylib=0ebe7c78dad2 dyld=e7b9a432bdb6
BUNDLE-LAUNCHD the services launchd recorded for this run (top 5 by count; ...)
```

It holds, and NAMES: **both logs** (the harness's own and a finite, run-scoped `server.log` slice).
The collector retains the original server-log descriptor and records its inode, size and boundary fingerprint;
append capture starts at that boundary, and rename rotation also collects the new inode. It never copies the
whole accumulated server history before and after a run. **Every guest capture** has its path printed
(`*.trace.<pid>` for the traced process, `*.traceg.<pid>` for its children, and the
workload's own redirected `*.out`); the **child's environment and descriptor table read from the HOST** (sampled
while the child is alive, because a guest cannot read its own `/proc`); **any core** written during the run (symlinked,
not copied); the **abort/terminate text wherever it landed**, lines copied WHOLE with the longest one's length
reported; the **plane/ledger counters**; the **fixed boot-component file hashes** (the `RUN-ENV` line, produced
by `dwdiag env`, so there is one candidate table and not two); launchd's own debug log and its quoted
`Mach service lookup` lines; and the **instrument census that names every silent instrument**.
`RUN-ENV` does not cover custom workload binaries/libraries or prove loaded-image identity.

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
* nothing is truncated at a small constant. A 512-byte cap once cut the very buffer an abort message was in; the
  bound is now the record's own declared length (or `XTRACE_MAX_PAYLOAD_BYTES`, default 64 KiB) and **a cut is
  printed** -- `...[truncated: printed the first N of M declared byte(s)]`.

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
Source/build/prefix/cache trees and metadata remain excluded. The host needs `zstd`; if `/proc` hides potential
writers, an isolated read-only `sudo -n` census is attempted. Unavailable ownership inspection preserves raw files
and reports the reason rather than guessing that they are idle.

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


**A run of this kind is archived** so the claim is reproducible rather than asserted:
`evidence/instrument-auto-bundle-2026-10-15/` holds one bundle from the current ladder workload
(`probe-arms3.sh minproj-bare-t`, `VERDICT: PASS`) with its console, `MANIFEST` (11 announced captures, 14 child
records, the census naming 29 silent instruments, both `MISSING` rows with their reasons), the launchd log's
look-up lines, the run's server-log **delta**, and `SHA256SUMS`. The README there says which bytes the bundle held
that are deliberately not archived (the two 36 MB server-log copies) and why.

### Reading foreign memory without killing the process being measured (xtrace)

An instrument that reads the traced programme's memory cannot be allowed to fault: MEASURED, a descriptor's
**address** was dereferenced on a formatting path and a traced arm died with SIGSEGV 3 s in (`RC=139`, core
`/tmp/t4cores/core.2915140`) -- the instrument killed its own subject and the run was lost. A bound on the LENGTH
cannot prevent that; the BASE can be stale. So `src/xtrace/safe_read.cpp` is the ONE reader:

* it reads with a raw `process_vm_readv` (a syscall cannot raise) and never with a load; a raw `syscall` is also
  invisible to the tracer's own hooks, so it cannot recurse;
* an unmapped or partly-mapped range returns `XTRACE_READ_UNREADABLE` / a short count, and the caller prints
  `unreadable: <reason>` -- a printed record, never silence and never a zero;
* `print_mach_msg` also CLAMPS its descriptor walk to the message's own `msgh_size`, so a truncated message stops the
  walk with `DECODE-STOPPED` instead of reading past the buffer;
* the 32-bit image keeps its previous behaviour and says so: it does not read a descriptor's out-of-line content at
  all, because the guard cannot be built there.

Contract: `tests/run-xtrace-safe-read-contract.sh` compiles that reader on the host and holds it to real mappings,
with a RED arm that dereferences the same address directly and must still die -- without that arm the contract would
pass against a reader that refuses everything.

## darling-debug-runner: self-attributing run logs (`RUN-ENV` / `[dwdiag-env ...]`)

FIXED (measured cause): a run's log recorded what the guest and the loader printed but not **which copy of the
loader served it**. Two prefixes existed -- one freshly deployed, one stale -- and a log served by the stale copy
was read as evidence about the fresh build, twice, before the mistake was caught by hand. Any per-thread claim
drawn from such a log ("this thread never entered the loader") was therefore unsound.

`dwdiag cycle`/`dwdiag verdict` now print a single machine-readable identity line before the workload starts,
write the same line to a sidecar next to the log, and append it to the log once the run has finished:

```
RUN-ENV prefix=/tmp/r1-repro-prefix mldr=8dcb3246bcb4 libsystem_kernel.dylib=13170ac1ab11 dyld=3f97b08dac35
[dwdiag-env prefix=/tmp/r1-repro-prefix mldr=8dcb3246bcb4 libsystem_kernel.dylib=13170ac1ab11 dyld=3f97b08dac35]
```

Rules this makes enforceable: a log that carries no `[dwdiag-env ...]` line was produced by an older tool or by a
hand-rolled command and cannot support a claim about a specific build; when two runs disagree, compare their
`mldr=`/`dylib=`/`dyld=` digests before comparing anything else; and the digests are of the **deployed** prefix
files, so they answer "what actually ran", not "what was built".

### `suite` honours `DWDIAG_PREFIX`

FIXED (measured friction): `cycle` resolved the runtime prefix from `DWDIAG_PREFIX` but `suite` required the
`--prefix` flag, so a run started with the variable exported died on clap's usage error and read like a broken
tool rather than a missing argument. `suite` now resolves the flag-or-variable pair the same way the other
subcommands do. Verified by use without the flag: `rows=2 failures=0 SUITE-VERDICT PASS`.

### `dwdiag source`: refuse a build tree that does not compile the file you edited

FIXED (measured friction): this stage edited the darlingserver's thread creator, ran `ninja darlingserver`, read
`ninja: no work to do`, and only then found that the build tree has **zero** rules mentioning that source file --
the target imports a prebuilt binary, so a probe there could never appear in a run and a cycle would have reported
"the instrument stayed silent" about a byte-for-byte old artifact.

`dwdiag source --build DIR --source FILE` answers from the generated build graph, not from timestamps (a target
that imports a prebuilt artifact looks up to date whatever the source tree says). It prints, one line per file:

```
SOURCE-UNBUILT  file=kern_support.c rules=0  build=... -- no rule ...; a run here would exercise the OLD artifact
SOURCE-CONSUMED file=threads.c      rules=54 build=...
```

Exit code 3 means "do not trust a run that claims to exercise this source", which turns a silent no-op probe into
a refusal. Verified by use on both cases above (`rules=0` rc=3; `rules=54` rc=0).

### Run logs are read lossily (a passing run must not be erased by one binary byte)

FIXED (measured defect): a run log carries whatever the guest and the loader wrote to fd 2, including raw
register/pointer dumps from probes that deliberately bypass libc. The verdict path used
`fs::read_to_string`, so the FIRST invalid byte made the read fail, and every caller treated that as "no log text":
`dwdiag verdict --log <log>` then reported **NO-RUN for a run that had printed its own `pass=1` result line**, with
the only evidence an error buried in a transcript. Reproduced and fixed on a real log:
`/tmp/dwdiag-verdict-284379-threadnoopr6.log` contained `RING_MACH_TEST mode=threadnoop iters=5 pass=1` and was
judged NO-RUN before the fix, PASS after it.

Every log read now goes through `read_log_lossy` (bytes -> `String::from_utf8_lossy`); `/proc` reads that must stay
strict keep `std::fs::read_to_string`. Consequence for method: any verdict recorded before this fix must be treated
as possibly a READ failure rather than a run failure, and re-judged offline with the current tool.

### A diagnosis reads the prefix the RUN used, from the run's own identity line

FIXED (measured defect): several diagnostics carried a hardcoded default of `/tmp/dr-on-matched`, so a run served by
another prefix had its server-side evidence read from the WRONG prefix's log -- and an absence there reads like "the
server never sent it". Observed on a real hanging run: `dwdiag progress` reported
`SERVER-SENT plane-doorbell-sent=19210 (from /tmp/dr-on-matched/private/var/log/dserver.log)` while the run used
`/tmp/r1-repro-prefix`.

`resolve_prefix()` now answers in a fixed order -- an explicit flag, then `DWDIAG_PREFIX`, then the `prefix=` field of
the `[dwdiag-env ...]` line the run itself wrote -- and the server-log default is derived from it. Verified by use on
the same log: `SERVER-LOG-PATH /tmp/r1-repro-prefix/private/var/log/dserver.log`.

### `--artifact` accepts the guest's pthread library

FIXED (measured friction): the deployable-component table knew `darlingserver`, `mldr`, `libsystem_kernel` and `dyld`
only, so `dwdiag cycle --artifact libsystem_pthread.dylib=...` refused with "unknown component" even though the
prefix installs it fine -- a dead end in the tool while diagnosing the thread-creation path, which lives in exactly
that library. The component is now in the layout table (both prefix copies) and verified by use:
`PREFIX-INSTALL MATCH ... usr/lib/system/libsystem_pthread.dylib` and the `libexec/darling/...` copy.

## A prefix is a SET, and a launch failure is its own verdict

Added to `scripts/dwdiag` after a measured two-hour wall. `dwdiag deploy --build B --prefix P` installs the seven
runtime components from one build tree and verifies each deployed copy by sha256 (paths owned by
`scripts/darling-artifact-manifest.sh`). `dwdiag cycle` prints `PREFIX-PREREQ` before running, and `dwdiag verdict`
returns `BOOT-FAIL (<cause>)` for logs carrying `Failed to exec launchd`, `shellspawn did not become ready` or
`no recognized stable state`.

The rule this encodes: a mixed-build prefix boots into a failure that looks like something else. In the measured
case the prefix held a darlingserver from the instrumented tree, guest libraries from a second tree and
shellspawn/launchd from a third; the run log said only `Failed to exec launchd: No such file or directory`, and the
verdict said `HANG (watchdog)` ten times in a row. A verdict about a prefix must never be readable as a verdict
about the workload, and a component set must never be assembled by hand one file at a time.

## ETXTBSY belongs to processes, so the tool names them

An aborted cycle left `darlingserver` (reparented to pid 1), `launchd` and `shellspawn` alive in the prefix. The next
install of `mldr` failed with `ETXTBSY`, and the tool reported only `installing .../mldr (after a shutdown attempt;
first error: Text file busy)` -- no holder, no next step, and the gate run died at staging rather than at the
workload. `bin/darling --rootless shutdown` stops the server the harness knows about, not the guest processes it does
not.

`install_artifact_into_prefix()` is now the ONE way both `deploy` and the cycle's staging path put a file into a
prefix: copy, and on failure shut the server down, stop the processes that still hold the prefix (matched on `exe`
AS WELL AS `cmdline`, because a guest process runs through the prefix's `mldr` so its `exe` is the loader and its
`cmdline` is the guest argv, while the server's `cmdline` names the prefix without `exe` doing so; the caller's own
ancestry is excluded at every depth), retry once, and report `PREFIX-STOP-HELD pids=...` when holders were found.

Verified by use: a deliberate holder executing a staged file produced `PREFIX-STOP-HELD ... pids=2359645`, the copy
then completed, and the destination was restored to the build's sha256.

## Server-log resolution (fixed 2026-09-30)

`dwdiag progress` reads the server's own log for the counts it prints beside a run
(`SERVER-SENT plane-doorbell-sent=...`). It used to fall back to a hardcoded
`/tmp/dr-on-matched` when the caller did not pass `--server-log`, so a run of
another prefix was reported with ANOTHER prefix's numbers: measured on a
`/tmp/r1-repro-prefix` boot failure, the report said 19228 doorbells read from
`/tmp/dr-on-matched/private/var/log/dserver.log`, and after the fix the same run
reads 16894 from its own prefix.

Resolution order is now: the explicit `--server-log`, then the prefix recorded in
the run log's own identity line, then the default. The chosen path is always
printed with its source, and the default case says so out loud, because a count
whose file belongs to another prefix is worse than no count.

## Guest fixture refresh (fixed 2026-09-30)

`dwdiag cycle` runs the guest fixture at `/private/var/tmp/ring_mach_msg_test`
(inside the prefix). It used to take whatever copy was already there, so a run of
an OLD fixture was indistinguishable from a run of the new one. Measured: the
`forkexec` mode was added to the workload source, the workload was rebuilt, and
the run still answered `RING_MACH_TEST mode=forkexec pass=0 error=unknown_mode`
because the prefix held a fixture built hours earlier.

`cycle` now refreshes the fixture from the build tree (`DWDIAG_BUILD`,
`src/tools/<name>`) before the run, verifies the copy by sha256 like every other
artifact, and prints the outcome:

```
FIXTURE-INSTALL MATCH <sha256-16> <path>            # refreshed, or already current
FIXTURE-INSTALL MISSING-BUILD-ARTIFACT <path>       # source absent; the run proceeds and says so
FIXTURE-INSTALL FAILED <error>
```

Verification by use: with the refresh in place the same `forkexec` run reports
`FIXTURE-INSTALL MATCH 6a91f572f3b789b3` and `verdict=PASS denied=0 created=0`.

## Workload: `forkexec` mode

The workload now implements the mode its own tool list advertised: it forks, the
child `execve`s the same binary with a small transport-using mode, and the parent
waits and counts results, printing

```
RING_MACH_TEST mode=forkexec iters=<n> child_ok=<n> child_bad=<n> pass=<0|1>
```

so a successful child is evidence the transport survives both fork and exec,
rather than an assumption.

## `cycle --capture-wchan` (added 2026-09-30)

`dwdiag cycle --capture-wchan` samples `/proc/<pid>/wchan` and the process state
for every process whose cmdline names the prefix, once a second, for the whole
run -- including the harness' boot wait, which is the window a boot failure
occupies. On a non-PASS verdict it prints the sample count and the last 24
samples:

```
CAPTURE-WCHAN samples=<n> prefix=<path>
CAPTURE-WCHAN s=<second> pid=<pid> state=<S|D|R> wchan=<symbol> cmd=<cmdline>
```

WHY: the boot failure this was written for leaves no guest-side evidence, and its
rate moves when the GUEST is instrumented (per-iteration marks took it from 10/10
to 7/10). Sampling from the host changes nothing in the guest, so this is the one
question that can be asked without perturbing the measurement. It prints the
count so that an empty capture is never read as "nothing was blocked".

Measured use: twelve consecutive boots with the flag on were all PASS, so the flag
had nothing to print -- which is itself the correct behaviour and the reason the
count must be printed rather than inferred.

### `--capture-wchan`: per-pid aggregation (added after the first real catch)

The first catch printed only the last 24 samples, and by then every guest process
of the prefix had exited: the output showed the harness shell waiting in
`do_wait` and nothing else -- true, and useless for the question asked. The flag
now prints, on a non-PASS verdict:

```
CAPTURE-WCHAN samples=<n> prefix=<path>
CAPTURE-WCHAN distinct-pids=<n>
CAPTURE-WCHAN last s=<second> pid=<pid> state=<S|D|R> wchan=<symbol> cmd=<cmdline>   # per pid
CAPTURE-WCHAN tail <...>                                                            # last 8 raw
```

so each process's LAST observed state and the second it was last seen are both
visible: a process that waited for 300 seconds is distinguishable from one that
vanished in the first second, which the tail alone could not show.

Measured value of the first catch (before this aggregation): a BOOT-FAIL run with
`denied=0 created=0` had NO prefix guest process alive at the end -- only the
harness shell in `do_wait` -- so the failure is not a live hang at the endpoint;
whatever stalls it happens earlier and the prefix has already torn down by the
time the watchdog expires.

### `--capture-wchan`: per-run reset (added after the second catch)

Across a `--repeat` series the sampler used to accumulate every run's samples, so
the `distinct-pids` list on a failure mixed runs and could not say which process
belonged to which run. The buffer is now cleared at the start of each iteration,
which makes the next catch attributable to a single run.

Census from the two catches so far (before the reset, so run attribution is not
available): the non-harness entries are the prefix's own launcher and guest shells
-- `bin/darling --rootless shell /bin/bash -c ...` in `sigsuspend` (one process)
and in `do_poll.constprop.0` (its companion) -- plus one instance of that same
shell pair in `state=D wchan=jbd2_log_wait_commit`, i.e. blocked on the HOST
filesystem journal. The harness shells themselves are always `do_wait` or
`pipe_read`.

That D-state is a candidate, not a conclusion: with the mixed buffer it may have
belonged to a passing run, and host-I/O sensitivity would also explain why
instrumenting the guest changes the failure rate. The per-run reset exists so the
next failed boot can be attributed before that hypothesis is repeated.

### `--gap-seconds` and the host-I/O test it enabled

`dwdiag cycle --gap-seconds N` waits N seconds between runs. It exists because
runs back-to-back keep the host filesystem journal busy, and host-I/O stalls were
the leading candidate for the boot flap; a gap is the cheapest way to test that
without touching the guest. It also stops one run's teardown from overlapping the
next run's boot, which is friction in its own right.

RESULT OF THE TEST -- host-I/O spacing is NOT the factor:

* a 30-run series with `--gap-seconds 5` produced three failures (runs 13, 15,
  22), which is the same rate as the pooled back-to-back series (~1-3 per 30);
* none of the three attributed captures contained the `state=D
  wchan=jbd2_log_wait_commit` entry that suggested the hypothesis, so that entry
  is now known to have belonged to a passing run in the mixed-buffer era, exactly
  as the earlier note warned.

What the attributed captures DO show, in all three: the prefix's own
`bin/darling --rootless shell ...` pair sits in its normal steady state
(`sigsuspend` and `do_poll.constprop.0`) with the harness shells in `do_wait` /
`pipe_read`, and nothing is blocked on a host resource. Two of the three failures
had `denied=0`; one had `denied=1`. So the flap is the guest shell pair failing to
complete its work, not the host stalling underneath it.

## Freeze-on-fail is now reachable from the diagnostic tool, and the snapshot is taken by the catcher (2026-10-01)

Two pieces of friction were solved in the same session, both because a caught failure was being destroyed
before it could be read.

* `dwdiag cycle` gained `--freeze-on-fail`. The boot harness has implemented the freeze for a while, but the
  tool that builds the harness command did not forward it, so holding a failure for inspection meant
  abandoning the tool and retyping the harness invocation by hand -- which loses `MLDR_DIAG_LOG`, the
  single-argument `--cmd` shape, and the deployment identity the tool records into the run's own log. The
  cycle stops at the first frozen failure and prints `CYCLE-FROZEN` with the log to inspect, because the
  frozen run owns the prefix and a following run would collide with it. Default is off: acceptance never
  freezes, and a frozen prefix is left running on purpose, to be cleaned with the command the harness prints.

* `snap-on-freeze.sh` (diagnostic only) takes the server snapshot in the same process that catches the
  freeze. WHY: the harness wraps the launcher in `timeout 150`, so a frozen incarnation is destroyed roughly
  a minute after the freeze is announced. The first attempt read the interesting fact from a second tool call
  and lost that race -- by inspection time the server process was gone, and the question (was the semaphore
  timed wait's timerfd armed, with which deadline, and had it ticked) could not be re-asked without another
  run. The script reads `/proc/<server>/fdinfo/<timerfd>` twice, two seconds apart, then the debugger's
  identity queries, so the armed deadline and the tick count are captured while the server is still alive.

Measured on the frozen failure (current build, `stress_mixed 20`): a guest thread of `launchd` (pid 1) sits
with `active_call` 62 `semaphore_timedwait`, `waiting_for_reply=true`, `suspended=true`, while the stall dump
reports `timer=1 tactive=1` for it -- a timer IS armed for the wait -- and the server's main loop is in
`epoll_wait` (which uses a 500 ms timeout whenever the stall dump is enabled, so the loop is cycling). The
workload's own boot-side evidence in the same window is `shellspawn` stopped after its last instrumented
step, which is NOT evidence of anything: that step is followed by a blocking `accept()` by design, and the
instrumentation simply ends there. Likewise `release-drops-pending` is background noise: it fires 19-26
times in a passing `basic` run and 70-102 times in a passing `stress_mixed` run.

## Provenance witness and gate (2026-10-01, dar-4cp9)

The transport phase produced strong runtime evidence from an untracked scratch source tree whose revision
matched no branch, so a closed Bead rested on source nobody could reproduce and the fix could not be exported
as a patch. That is a process defect, and it is now machine-checkable instead of being something a later
reader has to guess.

`dwdiag source --provenance` (the existing source-inspection command, extended rather than duplicated) reports:

* the source root the build tree was configured against, taken from `CMAKE_HOME_DIRECTORY` in its cache;
* for every component (darlingserver, mldr, xnu, launchd, dyld): revision, branch, dirty tracked file count and
  untracked file count, or `no-git`/`missing` when the directory is not a repository;
* the hashes of the artifacts actually deployed into the prefix, because a source identity says nothing about
  the binary a run executed.

It then prints `PROVENANCE-CANONICAL` or `PROVENANCE-NON-CANONICAL` with the reasons. `--require-canonical`
exits 2 when not canonical; `dwdiag cycle --provenance --require-canonical` prints the same witness at the end
of a series and refuses the series the same way. A dirty tree is NOT an error: diagnostic runs from dirty
source are legitimate. What is refused is calling such a run the evidence for a product claim.

MEASURED USE, both arms: against the scratch build the witness reports
`PROVENANCE-NON-CANONICAL ... reasons=src/external/darlingserver:untracked=21,...` (correct: that tree is
untracked) while still naming the revision it was cut from and four deployed artifact hashes including the
server's; and a one-run series that PASSED was refused with
`PROVENANCE-REFUSED` and exit 2, which is the deliberately tested negative contract.

### Crash records identify their own image (2026-10-01)

This session reversed guest/server crash attribution twice, and both times the correct answer came from asking
which deployed image contains the reporting symbol. `dwdiag crash` now answers that itself: with `--prefix` (or
`DWDIAG_PREFIX`) it hashes every candidate image and reports, per image, whether it carries
`dserver_crash_probe`, then prints how many images report the probe, the prefix's live server host pid, and
`host-tid=unavailable` when the record genuinely does not carry one.

Measured on the recorded failure: five candidate images hashed, `images-reporting-the-probe=1`, with
`bin/darlingserver ... reports-dserver-crash-probe=y` and every guest image `no` -- i.e. the record belongs to
the server, which is the fact that had to be established by hand before. No product change was needed for this,
which matters because the scratch product tree is frozen until canonicalization completes.

### An error, an exhausted retry, and a component name (2026-10-01)

Three friction points found the hard way while chasing a guest abort, all fixed in the tool.

**A runtime error must reach the caller, not only the transcript.** `dwdiag prefix` and `verdict` are long runs,
so their stdout and stderr are redirected into `/tmp/dwdiag-transcript-*.out`. The clap usage errors were already
routed around that redirection, but a command's own error was not: passing an unknown component name to
`--artifact` made the whole invocation fail with `TRANSCRIPT=...` and `ESSENTIALS 0 line(s)` on the terminal and
the reason inside the file, which reads as "there was nothing to do" and cost an install that silently did not
happen. `main` now prints such an error through the same `say()` path the essentials use and exits 3. Verified:
`--artifact nosuchcomponent=/bin/true` prints `ERROR: --artifact nosuchcomponent: unknown component; known:
darlingserver, mldr, libsystem_kernel, dyld, libsystem_pthread.dylib` on the caller's stream with rc=3.

**A component may be named by its library file name.** `--artifact` takes the component key
(`libsystem_kernel`, `dyld`, `mldr`, `darlingserver`, `libsystem_pthread.dylib`), but what a caller has in front
of them is the file they just built, and the lookup ran before any pair was installed, so
`--artifact libsystem_kernel.dylib=...` aborted the invocation and a correct `dyld=...` beside it did nothing as
well. Both spellings now resolve.

**An exhausted plane retry is nameable.** `__dserver_plane_request_ex` retried nine times and printed
`[plane-retry]` once per process, but the exit that actually decides the caller's fate -- the one that hands back
-1 and sends the caller to the datagram fallback the hard socket gate then denies -- printed nothing, so "the
plane refused once and then answered" and "the plane never answered at all" left the same log. It now prints a
bounded `[plane-exhausted] n= op= tid= attempts=9 status=` line. Measured use: in a batch where the failing runs
all carried `call=checkin image=loader`, this instrument stayed silent, which is itself the answer -- those
failures are the loader's datagram-path checkin being denied, not a plane refusal.

### The loader's checkin diagnostics need two variables (2026-10-01)

`[mldr-ctl]` lines -- `checkin-publish BEGIN`, `deferred-checkin ... status= ready=`, the skip notice -- are
gated on `MLDR_COURIER_DIAG` (`mldr_diag_on()` reads exactly that), while `MLDR_DIAG_LOG` only chooses the
sink and falls back to fd 2 when unset. Setting the sink alone therefore produces a log full of
`[mldr-dthread ...]` lines, which are a different instrument, and a reader concludes the checkin diagnostics
are broken. Measured cost: one batch run under `MLDR_DIAG_LOG` alone, which answered nothing. Use both:

```
dwdiag verdict --prefix P --mode basic --args 20 \
  --env MLDR_COURIER_DIAG=1 --env MLDR_DIAG_LOG=/tmp/mldr-$n.log
```

What they answered when set: in crashing and passing runs alike the deferred checkin goes through the plane
(`checkin-publish BEGIN` then `deferred-checkin status=0 ready=1`), so the deferred checkin is not the call
that dies, and a run that reports `denied=1` can still PASS -- the denial is a correlation with the crash, not
its cause, and any explanation of that failure class has to name the call site.

### `handoff/` deletes files it does not own (2026-10-01)

`west dw handoff` regenerates `handoff/*.bundle` and, in doing so, removes files in that directory it does not
recognize. Measured twice: preservation diffs committed under `handoff/` were gone from the working tree after
the next `west dw handoff`, unstaged and unannounced, which is exactly the loss the archives existed to
prevent. Keep anything durable outside that directory; the preservation copies of the xnu mach_msg delta now
live in `artifacts/xnu-ring-mach-msg/`, and one `west dw handoff` run confirms they survive it (2 files still
present, 0 diff files left in `handoff/`).

## `dwdiag processes` (added with the stale-process correction)

A prefix-process census matched by executable path AND command line, `--list` and `--json`, exit 1 when the prefix is
not clean. Added because `pgrep -c <name>` said a prefix was empty while it held three stale guest launchers: the
launcher's process name is not the pattern, so the count was zero for the same reason a broken instrument is silent.
Use it before any claim that a prefix is clean, and use `scripts/prefix-cleanup.sh` -- which prints its own
prefix-owned before/after counts -- to act on the result.

## When a long `dwdiag` job "is still running" but is not

Two waits in one session were spent on a job board rather than on work: the suite had finished all nine modes and its
per-run logs were written, the process was gone (`pgrep` found nothing), and the board still said running -- once for
over an hour. The rule is the same one the verdict logic follows: a status line is a claim, not evidence. The evidence
is (a) the tool's own per-run logs under `/tmp/dwdiag-verdict-*.log`, each carrying its mode's machine-readable
`pass=1` line, and (b) whether a tool process is actually alive for the prefix, which `dwdiag processes --prefix P
--list` now prints with the executable and command line it matched on. If no tool process is alive and the logs are
complete, the job is over no matter what the board says; if the tool does linger after its last run, its per-run logs
are still the result, and the outer timeout is only there to reclaim the process.

## Attributing a hang that the log cannot explain (the recipe)

When a workload never speaks and its log ends in ordinary traffic, the log tail is exhausted as evidence -- chasing
the last marker is how this session spent three rounds on a non-cause (`[pc-nothreads]` is a normal early return, and
the repeated `[pc-entry]` pairs are courier wakeups: the plane's publish path does `sendto`, `sendto` calls
`CANCELATION_POINT()`, and that calls `sys_pthread_canceled(0)`, which answers `-EINVAL` in a process without
threads). What is left is to ask the RUNNING state, not a finished file:

1. `dwdiag verdict --prefix P --mode M --args A --repeat N` and let it time out. The watchdog kills the workload, so
   the interesting process is gone by the time the run ends -- which is why this does not work retroactively.
2. Instead, run with a wait long enough to catch it: the per-run `--wait` is what bounds the workload, so raise it and
   drive ONE run at a time, keeping the tool in the foreground of a job whose output is not consumed.
3. While it is stuck: `dwdiag processes --prefix P --list` lists what the prefix still owns, by executable and command
   line. That names the process -- the tool's own `pid` is excluded from the census.
4. For the named pid: read `/proc/<pid>/wchan` (what it blocks in) and `/proc/<pid>/stack` if readable, then
   `dwdiag progress --log <run log>` for what the guest and the server last did, and `dwdiag trace --log <run log> --pid
   <pid>` for that process's own chronology in the log.
5. `dwdiag crash --log <run log> --binary <PREFIX>/bin/darlingserver` if a `[dserver-CRASH ...]` line is present --
   with the SERVER binary, since that line is the server's.

The point of writing this down: the heuristic that works on a crash (read the last line of the log) does not work on a
hang, and the difference cost this session repeated attempts before it was stated plainly.
