# Darling server debug ABI and current capture routes

The product contains a C++ read-only server debug ABI. It does **not** currently
ship the `darling-debug` Rust client or the LLDB Python command glue described
by the original debugger architecture. The vendored host runner builds
`darling-debug-runner`, not that client; the separate West runner does not
supply the missing client/glue either.

Use the current `dwdiag processes`, `unwind` and `run` routes for operational
process/stack capture. They are not clients of the server debug ABI and cannot
be presented as its server-state or expected-waker view.

Related documents:

| document | what it is |
| --- | --- |
| [tooling](tooling.md) | current diagnostic command index and ownership rules |
| `darling/src/external/darlingserver/internal-include/darlingserver/debug-abi.hpp` | implemented C++ snapshot schema and availability fields |
| `darling/src/external/darlingserver/internal-include/darlingserver/debug-{introspection,connection,serialize}.hpp` | protocol, connection bounds and serialization interfaces |
| `darling/src/external/darlingserver/src/debug-{collect,introspection,connection,serialize}.cpp` | actual collection, parsing, I/O and serialization |
| `darling/src/external/darlingserver/src/server.cpp` | opt-in listener integration |

## Purpose

Separate three kinds of evidence: host process/register/stack captures from
available tools, state that the C++ server exporter can represent, and facts
independently witnessed by a workload. A schema field is not a live observation;
an available host stack is not a server Call/waker snapshot.

Keep this file as the ABI reference and availability boundary. Do not repair a
missing client by inventing a protocol script, advertising an absent Cargo
target or importing a nonexistent LLDB module.

## Architecture

The implemented path is:

```text
DarlingServer Process/Thread registries and public accessors
    -> C++ DebugCollector
    -> bounded versioned JSON serialization
    -> opt-in nonblocking abstract AF_UNIX listener
```

The C++ layer owns collection, schema, serialization, peer authorization and
connection lifetime. It does not provide an interactive CLI or LLDB commands.
`darling-debug` is also the ABI name/request signature; that string does not
establish the existence of a host executable.

| layer | current availability |
| --- | --- |
| C++ server exporter and listener | present and wired into the server; opt-in |
| Rust protocol client, identity interpretation and rendering | not shipped by the current runner sources |
| LLDB Python delegation glue | not present in the product's `tools` directory |
| `dwdiag` process/stack/bundle capture | available host diagnostics, separate from the server ABI |

The original multi-layer client architecture is not a description of a
currently implemented end-to-end tool.

## Build / prerequisites

There is no supported client build recipe in these checkouts. Building the
runner or installing LLDB does not supply the absent Rust client or command
glue. The C++ headers/sources above, not an absent Rust `Snapshot` type or
`docs/debug-abi.md`, are the current ABI reference.

For operational capture:

- Select an immutable normal installation `R` and disjoint writable guest state
  `P`. Installed launchers are under `R/bin`, never `P/bin`.
- Build/publish changed product code through the normal stock ALL/full-install
  workflow in [tooling](tooling.md#installation-guest-state-and-diagnostic-inputs).
  Never copy a focused server artifact into `P` or rewrite a published image.
- Retain one lifecycle owner for `P`, with the persistent external per-prefix
  lease distinct from `P/.lifecycle.lock`. Read `darling-boot` before launch.
  An observer does not acquire the right to start another guest or clean up
  someone else's run.
- Use the actual workload's host PID, matching installation and owned durable
  evidence paths. `unwind` needs GDB and permission to inspect the selected
  process; failure to read registers/memory is unavailable evidence, not an
  empty successful stack.

## Enable the debug ABI

The implemented listener is **opt-in and absent by default**. Only the exact
launch-time value `DSERVER_DEBUG_ABI=1` enables it; unset, empty, `0`, `off`,
`no` and other values leave it disabled. This must reach the launcher before a
cold server start. Changing an observer's environment cannot alter an already
running server.

The server logs the abstract endpoint when listener setup succeeds, or a reason
when identity, socket, bind/listen or epoll setup fails. It has no fallback
transport. Enabling the endpoint does not install a consumer.

The current `dwdiag processes`, `unwind` and `run` commands do not need this
switch. Leave it disabled for ordinary capture unless the task specifically
requires investigating the server ABI. Any requested cold start still belongs
to the existing prefix lifecycle owner and normal product shutdown/cleanup.

## CLI quick start

These are commands of the **existing runner**, not a `darling-debug` client.
`R`, `P`, `DW_STATE_ROOT` and `HOST_PID` denote explicit paths/identity from the
owned run; do not copy a PID from another run. Use an existing owned `TMPDIR`
beneath `DW_STATE_ROOT`.

First inspect the prefix process census:

```sh
scripts/dwdiag --runtime-root "$R" processes --prefix "$P" --list --json
```

Resolve the guest process from that run's identity evidence. For a bounded
bundle of the current unwind observer:

```sh
scripts/dwdiag --runtime-root "$R" run \
  --name guest-unwind --bundle-root "$DW_STATE_ROOT/bundles" \
  --timeout-seconds 120 -- \
  scripts/dwdiag --runtime-root "$R" unwind --pid "$HOST_PID" --max 12 --json
```

`unwind` observes an attachable live guest PID through GDB registers and host
mappings/memory. Attachment can stop/perturb the process. A validated
frame-pointer walk can be partial in optimized production code; explicit
`--scan` selects candidate stack words, not a backtrace or a causal wait proof.

The outer `run` records observer output and status. It is a generic host-command
runner: it does not infer `DPREFIX`, own the guest lifecycle or guarantee guest
shutdown. This example observes an existing owned run; it does not launch a
second guest. Retain the printed bundle, errors, actual PID/image identity and
the lifecycle owner's final result. Neither a zero process count nor a capture
timeout proves clean product shutdown or workload PASS.

The shim may build the host runner even for help. Read the
[runner reference](../tools/darling-debug-runner/README.md) for its capture and
ownership contracts; no command above consumes the C++ ABI.

## LLDB integration availability

No Darling LLDB command glue is present in the selected product source, and
there is no shipped `darling-debug` client for it to delegate to. There is
therefore no current Darling-specific LLDB quick start, module-import path or
client environment variable to configure.

Upstream LLDB can be used independently for native debugging, but that does not
provide the missing server ABI integration. Use the available host capture
routes above rather than treating a missing module as a path setup problem.

## Server protocol operations

These are operation names exposed by `DebugCollector::operations()`, **not**
runner subcommands or a supplied CLI:

| wire operation | C++ exporter purpose |
| --- | --- |
| `hello` | ABI/version and supported-operation response |
| `identity_map` | available target identity fields |
| `list_processes` | process/thread registry discovery rows |
| `list_threads` | process/thread registry discovery rows |
| `process_info` | available process state, paths and memory summary |
| `thread_info` | public thread state and active/pending Call slots |
| `thread_current` | thread-shaped lookup; no automatic LLDB selection is supplied |
| `wait` | partial wait state from public server fields |
| `transport_current` | explicit transport availability/detail |
| `artifacts` | executable paths, not installed-byte hashes |

The parser accepts selectors named `host_pid`, `host_tid`, `server_pid`,
`server_tid`, `nsid` and `eternal_id`. Parsing a selector does not guarantee that
the collector resolves it: current thread lookup uses host/server TID, NSID or
eternal ID; process lookup uses the resolved thread, server PID or NSID.
`host_pid` is parsed but is not itself a process lookup in the current collector.

The schema distinguishes `complete`, `partial` and `unavailable`, with detail
text. Missing state must not be interpreted as zero/false. Responses and error
documents are JSON; consuming raw bytes requires validating one complete
document, not matching a banner or grepping for an ABI key inside an error.

## Identity model

The schema distinguishes Linux host, guest, server process/thread and dtape
identity/state. The current collector exposes only what its registry and public
accessors supply; it does not implement a complete cross-namespace identity
graph. Preserve `known`, availability and detail fields, and independently bind
any observation to the actual workload and prefix.

Available source paths collect process/thread IDs and NSIDs, eternal IDs,
executable/vchroot paths, thread count, available memory information, run state,
suspend/reply/signal state and public active/pending Call number/name.

Important limits in the current collector:

- Call header PID/TID/architecture are not exposed.
- `expected_waker` is deliberately empty: the exporter does not infer a waker
  from wait facts.
- Per-thread transport class/lane/sequence is unavailable.
- Full XNU/dtape wait-channel/continuation detail and a pidfd identity anchor are
  not established by the populated snapshot fields.
- Artifact paths are not hashes or evidence that those bytes served a run.

A plain Linux executable reached through `/Volumes/SystemRoot` need not have
a Darling server registry entry. Its host exit status is not proof of guest
execution or a successfully resolved Darling thread.

## Read-only debug ABI

The listener uses an abstract `AF_UNIX SOCK_STREAM`. The source contract is one
bounded newline-terminated request, one JSON response, then close:

| item | implemented bound/identity |
| --- | --- |
| ABI | `darling-debug`, version `1` |
| request | at most 512 bytes |
| response | default hard budget 64 KiB |
| abstract socket name | `darlingserver-debug:<effective-uid>:<st_dev>:<st_ino>` |
| identity source | `stat` of the guest prefix directory |

The serializer reports truncation and dropped sections rather than silently
exceeding its budget. The endpoint name is bounded independently of pathname
length. Identity/setup failure disables the listener; there is no truncated-name
or pathname fallback.

The identity helper follows `stat` aliases. This is endpoint naming, **not**
permission to normalize a caller's prefix spelling or bypass the launcher's
retained-FD/no-follow/`..` validation.

## Security / isolation

| property | implemented policy |
| --- | --- |
| default | listener disabled |
| enable | exact `DSERVER_DEBUG_ABI=1` before server startup |
| authorization | every accepted peer checked with `SO_PEERCRED`; UID must equal server effective UID |
| transport | separate abstract `AF_UNIX SOCK_STREAM` listener |
| control operations | no guest/server/transport mutation command |
| product semantic RPC | separate grammar; no `dserver_callnum` dispatch |
| descriptor transfer | no `SCM_RIGHTS` protocol |
| guest dependency | collection does not dispatch guest/S2C work |
| active connections | maximum 16 |
| lifetime | absolute 30 seconds from acceptance |
| session | one request, one response, close |

Abstract sockets have no filesystem mode bits, so peer authorization is not
optional. There is no privileged UID override or alternative transport.
Read-only semantics are not a promise of zero CPU cost or timing perturbation.

## Non-perturbation guarantees

The implemented I/O path is nonblocking and owned by the server epoll context.
`EAGAIN` retains request/response progress and returns to the event loop;
trickled input or stalled output does not extend the absolute connection
deadline. Accept work is capped per callback and the existing epoll timeout is
bounded by the earliest connection expiry.

This bounds client I/O state; it does **not** prove zero runtime perturbation.
Collection and serialization consume CPU synchronously on the owning context.
Do not describe source inspection of these mechanisms as measured latency,
live correctness or regression coverage. The separate GDB-based unwind route
can stop a target and has a different perturbation model.

## Relationship to dwdiag

`dwdiag` supplies current process, register/stack, log and bundle diagnostics,
plus workload-specific regression/evidence tooling. It does not currently
consume this C++ server ABI or supply the absent client identity graph.

Use host captures to form a hypothesis, then exercise the actual product
regression and workload oracle. Neither an observer snapshot nor a plausible
stack establishes the expected waker, a fixed race or acceptance by itself.
Keep source-only ABI evidence, diagnostic capture and behavioral verdicts
separate.

## Live validation

> **Historical measured report — non-operational.** This section records the
> original integration observation only. It is not current installed-image
> acceptance or a claim that a new guest/native run has passed.
> The client/glue in this retained report are not supplied by the current
> checkouts. This report is not a build recipe or a current availability claim.


The first end-to-end milestone ran a real stack, not a fixture:

```text
debugger-enabled DarlingServer (real prefix, DSERVER_DEBUG_ABI=1)
    + a real Darling guest workload
    + the real Rust client
    + upstream LLDB 18 with the Python glue
```

Independently verified against `/proc` for the same stopped thread:

```text
host pid / host tid        MATCH
host thread set            MATCH
process thread count       MATCH
virtual size (VmSize)      MATCH
resident size (VmRSS)      MATCH
guest and server image     MATCH
server executable          MATCH
```

Explicitly **not** independently witnessed in that run:

```text
active Call / pending Call / expected waker   exported, no independent witness yet
transport class / lane / sequence             unavailable or partial on this build
dtape task/thread state                       unavailable or partial on this build
```

The evidence bundle (run summary, witness table, raw artifacts) is kept outside
any GC-managed root, alongside the workspace's other diagnostic evidence; ask
the owning Bead (`dar-bexp`) for its current location rather than expecting a
stable in-repo path.

## Known limitations

- There is no shipped Rust ABI client or LLDB glue in the current checkouts.
- The exporter is partial: field presence alone is not a live identity or wait
  witness, and the expected-waker field is deliberately empty.
- No server registry entry means there is no corresponding target snapshot;
  a host process can legitimately be outside that registry.
- Snapshot collection is synchronous and bounded, not free.
- Current `dwdiag` host captures do not fill the missing server-side view.

Retain unavailable/partial/truncated state and collection errors explicitly;
do not replace them with guessed values or a successful verdict.

## Troubleshooting

| symptom | meaning / supported response |
| --- | --- |
| no `darling-debug` executable or Darling LLDB commands | those components are not shipped here; use current capture routes, not nonexistent build/import recipes |
| no debug listener on an ordinary server | expected default; current host capture does not require enabling it |
| requested ABI listener setup fails | inspect the server's identity/socket/epoll error; do not substitute a fallback transport |
| ABI peer is immediately rejected | same-UID authentication is mandatory; no privileged override |
| unresolved identity or unavailable field | check the actual registry/availability limits; no client-side guess can supply absent state |
| GDB/register/memory capture denied or partial | retain the exact host error and PID/image identity; missing capture is not an empty successful result |

Do not disable product prefix guards, same-UID authorization or the opt-in gate
to make an observation appear successful.

## Extension / fork gate

The absent client/glue are not installed features hidden behind configuration.
Do not advertise an end-to-end debugger until the actual components and their
contracts exist and have their own evidence.

An ABI extension requires a measured debugging need and the smallest matching
change, not an inferred capability from an unpopulated schema field.

No LLDB fork is justified by the current source reference. Reopen that question
only for a concrete requirement demonstrated on a real workload that upstream
extension mechanisms cannot express; absence of the command glue is not such
evidence.

