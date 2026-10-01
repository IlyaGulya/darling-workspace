# The Darling debugger (`darling-debug`)

The supported developer view into a **running** Darling runtime. It answers the
questions a host-side tool cannot: which guest thread a Linux thread is, what
the server believes that thread is doing, which Call it is in, what it is
waiting for, and who is expected to wake it.

It is a read-only observer of the server's own state. It is not a control plane
and it never mutates the guest, the server, or a transport.

Related documents:

| document | what it is |
| --- | --- |
| `docs/debug-abi.md` in `darling/src/external/darlingserver` | the wire specification: schema, framing, bounds, endpoint identity |
| `docs/tooling.md` | the diagnostic tool index this document is listed in |
| `darling/src/external/darlingserver/tools/darling-lldb/darling.py` | the LLDB command glue |

## Purpose

Darling's failures are usually timing-sensitive: a reply that never arrives, a
wakeup that goes to the wrong thread, a thread parked in the wrong state. A log
tells you what a thread *printed*; this tool tells you what the **server
believes** about that thread at the moment you ask, correlated with the host
Linux thread and the guest identity.

Use it to form a hypothesis about causal state, then convert the hypothesis into
a product fix, and prove the fix with a `dwdiag` regression (see
[Relationship to dwdiag](#relationship-to-dwdiag)).

## Architecture

```text
DarlingServer
    thin C++ read-only exporter
        |
        | versioned, bounded debug protocol (one request, one response, close)
        v
darling-debug
    Rust core: protocol client, identity reasoning, interpretation, rendering
        |
        v
LLDB
    upstream LLDB + a thin Python command glue
```

Ownership is deliberate and narrow.

**C++ (`darlingserver`) owns** bounded state collection, serialization into the
versioned schema, and the read-only endpoint. It does not interpret, rank,
correlate or render anything.

**Rust (`darling-debug`) owns** the protocol client, the identity graph,
interpretation, presentation, the CLI, and future snapshot/offline logic. All
reasoning about what a value *means* lives here.

**Python (`darling.py`) owns** LLDB command delegation only: it supplies the
facts only LLDB knows (the selected thread's OS tid, the frame PC, how that tid
was determined) and calls the core. It does not parse responses, hold ABI
grammar, or render.

**Native LLDB owns nothing Darling-specific.** There is no Darling C++ plugin,
no downstream LLVM patch, and no forked LLDB.

## Build / prerequisites

Three artifacts, three locations.

| artifact | repository | where the code lives |
| --- | --- | --- |
| debugger-enabled server | `darling/src/external/darlingserver` | the canonical debugger branch (see *Canonical revisions*) |
| `darling-debug` client | the `darling-debug-runner` sibling repository | the canonical debugger branch |
| LLDB glue | `darling/src/external/darlingserver/tools/darling-lldb/darling.py` | same as the server |

Build the client:

```sh
cargo build --bin darling-debug        # target/debug/darling-debug
cargo build --release --bin darling-debug   # target/release/darling-debug
```

Prerequisites:

- A Darling prefix that was bootstrapped normally (`<prefix>/bin/darling`
  exists). Nothing else about the prefix changes.
- A `darlingserver` in that prefix built from the debugger branch. Build and
  deploy it as a focused artifact, for example
  `west darling-build --targets darlingserver --deploy --deploy-darlingserver`;
  this replaces `<prefix>/bin/darlingserver` and leaves dyld and the closure
  alone.
- Upstream LLDB (18 is what the integration is validated against) and its
  Python support. Only the LLDB leg needs it.

The host tests for each layer run without a Darling runtime at all:

```sh
# from darling/src/external/darlingserver
cargo test                                    # Rust core (in the runner repo)
g++ -std=c++17 -O1 -Wall -Wextra -Werror -Iinternal-include \
    src/debug-serialize.cpp tests/debug_serialize_test.cpp -o /tmp/ser && /tmp/ser
g++ -std=c++17 -O1 -Wall -Wextra -Werror -Iinternal-include \
    src/debug-introspection.cpp src/debug-serialize.cpp \
    tests/debug_introspection_test.cpp -o /tmp/proto && /tmp/proto
bash tools/darling-lldb/t/run-darling-lldb-contract.sh
```

## Enable the debug ABI

The debug endpoint is **opt-in and absent by default**. A normally started
`darlingserver` creates no listener at all.

```sh
export DSERVER_DEBUG_ABI=1
```

Exactly the string `1` enables it. Unset, empty, `0`, `off`, `no`, or any other
value leaves it disabled.

The variable must be in the *launcher's* environment. `bin/darling` execs
`darlingserver` without resetting the environment, so exporting it before
starting the workload is enough:

```sh
export DSERVER_DEBUG_ABI=1
DPREFIX=<prefix> DARLING_PREFIX=<prefix> DARLING_ROOTLESS=1 \
  <prefix>/bin/darling --rootless shell /bin/bash -c 'echo ready; sleep 600'
```

`darling-boot-run.sh --env DSERVER_DEBUG_ABI=1` does the same thing inside the
measured-run harness.

When enabled, the server logs one line and publishes the abstract socket. When
it cannot be enabled, the reason is logged and the debug ABI stays disabled —
there is no fallback transport.

## CLI quick start

```sh
# build, then point the client at the prefix
cargo build --bin darling-debug
DD=./target/debug/darling-debug
PREFIX=/tmp/dar-dbg-prefix-20261001

# 1. is the endpoint there, and what does this server implement?
"$DD" --prefix "$PREFIX" hello

# 2. which guest threads does the server know, with every identity at once?
"$DD" --prefix "$PREFIX" list

# 3. inspect one thread by the host Linux tid
"$DD" --prefix "$PREFIX" thread --host-tid 265780

# 4. what is it waiting for, and who is expected to wake it?
"$DD" --prefix "$PREFIX" wait --host-tid 265780

# 5. which transport does the server record for it
"$DD" --prefix "$PREFIX" transport --host-tid 265780

# 6. the machine interface, and the byte-exact protocol view
"$DD" --prefix "$PREFIX" --json thread --host-tid 265780
"$DD" --prefix "$PREFIX" --raw  thread --host-tid 265780
```

If the endpoint cannot be reached the client exits `4` and prints one line to
stderr. If the endpoint answers with an error document the client exits `3` and
still prints the server's own document on stdout, so a caller can read the
server's words rather than a paraphrase.

| exit code | meaning |
| --- | --- |
| `0` | the server answered with a snapshot |
| `1` | the request could not be made: the prefix cannot be stat'd, or the core rejected the request |
| `2` | command-line usage error (an unknown subcommand, or `--json` with `--raw`) |
| `3` | the server returned an error document, or its answer was not a valid document |
| `4` | the endpoint could not be reached (connect failed) |

## LLDB quick start

Upstream LLDB, with the glue loaded:

```sh
export DARLING_DEBUG=/absolute/path/to/darling-debug
export LD_LIBRARY_PATH=<lldb lib dir>:<lldb python/lib dir>   # only for a relocated LLDB build

lldb
(lldb) command script import /absolute/path/to/darling/tools/darling-lldb/darling.py
(lldb) darling --prefix /tmp/dar-dbg-prefix-20261001 hello
(lldb) darling --prefix /tmp/dar-dbg-prefix-20261001 list
(lldb) darling --prefix /tmp/dar-dbg-prefix-20261001 thread 265780
(lldb) darling --prefix /tmp/dar-dbg-prefix-20261001 wait
(lldb) darling --prefix /tmp/dar-dbg-prefix-20261001 -j list
```

`$DARLING_DEBUG` is how the glue finds the core; otherwise it looks for
`darling-debug` on `PATH`.

With a process attached, `darling thread current` uses the thread LLDB is
stopped on: the glue passes that thread's OS tid, the selected frame's PC, and
the source of that tid. With no process attached it says so and the answer is
the server's own default.

For a scripted session:

```sh
lldb -b -s session.lldb
```

## Commands

Every command takes the same global options and the same identity selectors.

Global options:

| option | meaning |
| --- | --- |
| `--prefix <PATH>` | derive the abstract socket name from this prefix directory |
| `--socket <NAME>` | use this abstract socket name directly, skipping derivation |
| `--json` | one-line compact JSON: the machine interface |
| `--raw` | the server's exact response bytes, for protocol diagnosis |
| `--pc <PC>` | the debugger's selected frame PC, for the `Host:` line (decimal or `0x` hex) |
| `--tid-source <TEXT>` | how the host tid was determined, e.g. `LLDB thread` |

`--json` and `--raw` are mutually exclusive. `--pc` and `--tid-source` are
supplied by the LLDB layer, because only LLDB knows them; the core never invents
them.

Identity selectors (all optional; the server applies its own default when none
is given):

```text
--host-pid <PID>        --host-tid <TID>
--server-pid <PID>      --server-tid <TID>
--nsid <ID>
```

| command | purpose | input | important output |
| --- | --- | --- | --- |
| `hello` | ABI banner and the operations this server implements | none | ABI version, operation inventory |
| `identity-map` | resolve every identity space for one target | one selector | host, guest, server and dtape identity in one answer |
| `list` | every process and thread with full identity correlation | none | one row per registered thread |
| `ps` | processes only | none | the same rows, process columns |
| `threads` | threads only | none | the same rows, thread columns |
| `process` | one process | a process selector | executable, vchroot path, sizes, thread count |
| `thread` | one thread's server state and Call slots | a thread selector | active/pending Call, suspend state, wait state |
| `thread-current` | the thread the debugger is stopped on | `--host-tid` (from LLDB) | the same view, anchored to a known tid |
| `wait` | wait state and expected waker | a thread selector | run state, whether it waits for a reply, expected waker |
| `transport` | transport class and state | a thread selector | class, lane, sequence, or a reason it is unavailable |
| `artifacts` | artifact identity anchors | none | guest executable and server executable paths |

Reading an `unavailable` value: it is a statement about **this build**, not an
error. The schema separates `unavailable` (the build does not carry the state)
from a real value, and the renderer prints the reason the server gave. A tool
that guessed, or that rendered a zero as if it were a value, would be worse than
useless for this work.

Schemas for every command are emitted by the core
(`br schema`-style envelopes are not used here); the authoritative field list is
the Rust `Snapshot` type in the runner repository and the C++ schema in
`docs/debug-abi.md`.

### Raw JSON

`--raw` prints the server's bytes unchanged; `--json` prints the same document
re-encoded compactly on one line. Both are meant to be parsed.

**Contract lesson (measured live).** `--raw` must be validated as *one complete
JSON document*, never with the rendered-text rule that the human answer starts
with the ABI banner. Applying the banner rule to the raw document rejected every
passthrough answer, and because the JSON then only appeared inside the tool's
own error line, a check that merely grepped for `{"abi":` still passed. The LLDB
contract now pins both passthrough checks against the delegate error line.

## Identity model

Four identity spaces are correlated:

```text
Linux host pid/tid
    <-> Darling guest pid/tid/nsid
    <-> DarlingServer Process/Thread
    <-> dtape task/thread
```

Status of each edge **today**, which is not the same as the design:

*Live-verified:*

```text
host pid/tid            -> DarlingServer Process/Thread
guest pid/tid           -> DarlingServer Process/Thread
image path (guest and server)
process thread count, virtual size, resident size
```

*Exported by the ABI but not yet independently witnessed:*

```text
active Call
pending Call
expected waker
```

*Not available on the current build:*

```text
transport class / lane / sequence
dtape task/thread addresses and wait channel
XNU wait channel
dyld info address
pidfd anchor
```

Two consequences worth stating plainly:

- A guest process that is a plain Linux binary (for example a host binary
  reached through `/Volumes/SystemRoot`) is **not** in the server's process
  registry. The debugger then answers "not a Darling thread" — that is the
  correct answer, not a failure to resolve.
- An identity that resolves in one space but not another is reported as
  unresolved, with the space that failed named.

## Read-only debug ABI

The endpoint speaks one small grammar over an abstract `AF_UNIX SOCK_STREAM`.

```text
request:   one newline-terminated line, at most 512 bytes
response:  one JSON document, at most 64 KiB
then:      the connection is closed
```

The serializer never exceeds its budget: when a document would not fit it drops
whole sections and marks what it dropped, and the client reports truncation
rather than presenting a partial answer as a complete one.

The endpoint identity is derived from the prefix itself:

```text
darlingserver-debug:<effective-uid>:<st_dev>:<st_ino>
```

where the device and inode are those of the **prefix directory** (stat follows
symlinks, so a symlink alias identifies the same endpoint). Both sides derive it
the same way, so the endpoint is:

- bounded — it never approaches the `sun_path` limit regardless of prefix length;
- stable for one prefix;
- different for different prefixes.

If the prefix cannot be stat'd, is not a directory, or the name would not fit,
the listener is disabled with a precise reason. There is **no** truncation and
**no** path-name fallback.

## Security / isolation

| property | value |
| --- | --- |
| default | debug ABI disabled; no listener exists |
| enable | `DSERVER_DEBUG_ABI=1`, and only that exact value |
| peer authentication | `SO_PEERCRED`; the peer's effective UID must equal the server's |
| transport | abstract `AF_UNIX` `SOCK_STREAM`, deterministic bounded name |
| semantics | read-only |
| product semantic RPC | none — separate listener, separate grammar, never dispatches a `dserver_callnum` |
| `SCM_RIGHTS` | never sent, never accepted |
| memory mutation | no write path exists at all |
| guest dependency | serving never waits on a guest and never services S2C work |
| fallback transport | none — if the listener cannot be created, the ABI is simply disabled |

Bounds and lifetime:

| bound | value |
| --- | --- |
| maximum active debug connections | 16 |
| accept work per event callback | capped at the connection limit |
| connection lifetime | 30 s absolute from acceptance (monotonic) |
| request | 512 bytes, one newline-terminated frame |
| response | 64 KiB, whole sections dropped rather than exceeded |
| sessions | one request, one response, close |

The abstract namespace has no mode bits, which is exactly why the peer
credential check is mandatory and why a different UID is refused outright with
no privileged override.

## Non-perturbation guarantees

This matters more than anything else here: the tool exists to diagnose
timing-sensitive IPC, wakeup and race defects, so it must not perturb the
scheduler it is measuring.

> Debug client progress never causes DarlingServer's main event loop to wait for
> debugger I/O.

Implemented shape:

```text
accepted client fd          -> epoll-managed connection state
EPOLLIN                     -> consume immediately available bytes, then return
                               (EAGAIN returns immediately; the partial frame stays buffered)
EPOLLOUT                    -> write immediately available capacity, retain the
                               response offset on EAGAIN, then return
```

There is no `poll`-for-client, no sleep, no blocking read, no blocking write and
no wait-until-progress loop anywhere in debug socket handling. A client that
connects and sends nothing, sends half a request, or stops reading a response
cannot delay the event loop by even one callback: it holds bounded state and is
closed when its lifetime expires.

Companion bounds: at most 16 clients, and an absolute 30 s lifetime that neither
trickled input nor stalled output extends. Expiry is driven by the existing
`epoll_wait` timeout — bounded by the earliest deadline — not by a periodic
timer thread and not by polling.

Structural coverage lives in the shared C++ connection regression, which
interposes the syscalls and rejects any waiting primitive, any blocking
descriptor, and any retry after `EAGAIN` inside a debug callback.

**Not claimed:** snapshot collection itself is not free. It stays bounded,
synchronous collection on the server's owning context — it never moves to a
worker thread that would traverse `Process`/`Thread`/`dtape` concurrently. If
live measurement ever shows collection materially perturbing the runtime, that
is the piece to revisit; the socket path already does not.

## Relationship to dwdiag

Two tools, two jobs, and neither replaces the other:

| | `darling-debug` / LLDB | `dwdiag` |
| --- | --- | --- |
| role | interactive investigation, state inspection, hypothesis formation | deterministic regression evidence, acceptance gates, repeatable CI measurements |
| lifetime | the session you are in | committed, repeatable, comparable |
| question | "what is true right now?" | "did it stay fixed?" |

The intended workflow:

```text
darling-debug discovers the causal state
    -> product fix
    -> dwdiag regression proves it stays fixed
```

Use the debugger to *find* the cause; do not use it as an acceptance gate, and
do not expect `dwdiag` to explain a live thread's identity graph.

## Live validation

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

- Transport lane/sequence and dtape addresses are not exported by the current
  build; they render as `unavailable` or `partial` with the server's reason.
- `active_call`, `pending_call` and `expected_waker` are exported but have no
  independent witness yet, so they are not yet load-bearing for a diagnosis.
- A guest process with no server registry entry (a plain Linux binary) cannot be
  described.
- Snapshot collection is synchronous on the server's event context. It is
  bounded, but it is not free.
- The client is a point-in-time view. There is no history, no replay and no
  watch mode.

None of these is silently papered over: the ABI reports what it does not have.

## Troubleshooting

| symptom | cause | what to do |
| --- | --- | --- |
| client exits `4`, `no debug endpoint` | the listener was not created | confirm `DSERVER_DEBUG_ABI=1` was in the **launcher's** environment; check the server log for the reason it refused |
| connection accepted then closed immediately | peer UID differs from the server's effective UID | run the client as the same user that owns the prefix; there is no override |
| "not a Darling thread" / unresolved identity | the tid is not registered in that runtime | check you are asking the right prefix; a plain Linux guest process is legitimately unknown |
| endpoint identity mismatch | the client derived a name for a different prefix directory | pass the same `--prefix`; use `--socket` only to bypass derivation deliberately |
| a field renders `unavailable` | this build genuinely does not export that state | read the reason; it is not an error and there is no client-side workaround |
| `--raw` output rejected by a wrapper | the wrapper validated it with the rendered-text rule | validate raw output as one complete JSON document |
| LLDB: `no LLDB thread is selected` | no process is attached, so no host tid exists | expected; attach a process or pass a tid selector explicitly |
| LLDB: `darling-debug not found` | the core is not on `PATH` | set `$DARLING_DEBUG` to the built binary |
| LLDB fails to load its own libraries | relocated LLDB build | set `LD_LIBRARY_PATH` to that build's library directories |

Do not disable a safety check to make any of these go away. The same-UID rule
and the opt-in gate are the security model.

## Future work / fork gate

Deferred until a real debugging need demands it:

```text
complete dtape identity and state
real transport lane/sequence introspection
independently witnessing active_call / expected_waker
crash bundle
synthetic OS-thread presentation
DAP / IDE integration
```

The gate for anything that touches the ABI is the same as it has been: only a
*measured* live need may add to it, and only the smallest change that satisfies
that need. None of the items above is a reason to add a field today.

**LLDB fork gate:**

> No LLDB fork is currently justified.

This is not a claim that LLDB will never need one. The fork gate reopens only if
a real debugger requirement cannot be expressed through upstream LLDB's current
extension mechanisms (Python commands, `OperatingSystem` and Scripted
extensions) **and** that limitation is demonstrated on a live Darling workload.
A downstream or native plugin must start from a concrete missing capability
demonstrated live — never from preference.
