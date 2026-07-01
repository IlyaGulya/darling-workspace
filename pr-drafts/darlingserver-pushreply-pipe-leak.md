# darlingserver: never strand the push_reply synchronization pipe on a throw

**Bead:** dar-gwn.1.7
**Branch:** `fix/dar-gwn-1-7-pushreply-pipe-leak` (base `fix/processcall-exception-guard` → `preupstream/main`)
**Status:** blocked (local-only until brew is green end-to-end)

## Problem

A guest building a Homebrew formula intermittently *hangs* (flavor-C of the
same fork-storm race whose SEGV outcome is dar-gwn.1.6). The signature:
darlingserver goes silent mid-build but **stays alive and idle**; a guest
thread that was parked mid-RPC took an `INTERRUPT_ENTER` while parked, and the
matching reply never arrives — the thread blocks forever in `recvmsg` on its
per-thread RPC socket, and stranded siblings time out with
`semaphore_timedwait code=-111`.

### Mechanism

When a signal interrupts a thread that is waiting for an RPC reply, the
interrupt protocol can deliver the *interrupted call's* reply to the client
before the `interrupt_enter` reply. The client handles this by pushing the
unexpected reply back to the server (`push_reply`, callnum `0xbadca11`); the
server stashes it as `savedReply` and re-sends it after `interrupt_exit`.

The client's push hook (`__dserver_rpc_hooks_push_reply` in
libsystem_kernel's `dserver-rpc-defs.c`) creates a pipe, sends the **write
end** to the server, and then **blocks in `read()` on the read end** until the
server writes a handshake byte or closes its write end (EOF).

On the server, `Call::callFromMessage()` handles `push_reply` by:

1. `extractDescriptorAtIndex(...)` — pulls the write-end fd out of the message
   as a **raw `int`** (the fd is now owned by that local, not by the Message).
2. `readMemory(...)` the pushed reply body — **can throw**.
3. invariant checks (`_interrupts.empty()` → *"Client tried to push reply
   outside of interrupt"*; `savedReply` already set → *"overwriting"*) — **can
   throw**.
4. only then `write(pipeDesc, ...)` the handshake byte and `close(pipeDesc)`.

If any throw fires between (1) and (4), the raw fd **leaks** — never written,
never closed. The client's `read()` then blocks **forever** (no byte, no EOF),
the interrupted call's reply is never re-delivered, and the guest hangs.

The event-loop exception-containment guard (dar-gwn.1.8,
`processcall-exception-guard`) catches the throw and *drops the message* rather
than crashing the server — which is correct, but it converts the throw into
exactly this silent pipe leak and one-guest hang.

The realistic recurring trigger under a fork storm is the `_interrupts.empty()`
race: `push_reply` arrives **after** its interrupt was already torn down by
`interrupt_exit`, so there is no `savedReply` slot left — the old code threw and
dropped the reply.

## Fix

1. **Bind the extracted fd to an RAII `FD` immediately.** Its destructor closes
   the fd on every exit path, including a C++ exception unwinding out of the
   handler. The client's `read()` then always returns — a byte on success, EOF
   on any failure — and never strands the guest.

2. **Don't drop a `push_reply` that arrives with no live interrupt.** That
   pushed reply *is* the reply to the interrupted call, and the client is still
   waiting for it. Instead of throwing, send it straight back to the client so
   the call completes.

The `savedReply`-overwrite case (a genuine invariant violation) still throws,
but is now hang-safe: the FD dtor closes the pipe so the client unblocks via
EOF.

## Verification

- **Standalone mechanism test** (`dar-gwn17-verify/pipe_mechanism_test.cpp`)
  reproduces the exact client/server pipe protocol over `SCM_RIGHTS` and
  forces the server to throw before the handshake write:
  - OLD (raw fd leaked on throw): client `read()` **hangs forever** — the bug.
  - NEW (FD RAII closes on throw): client `read()` returns EOF — **unblocked**.
  - `RESULT: PASS — old code hangs, fixed code unblocks.`
- **In-binary live fault injection.** A debug-gated injection
  (`DSERVER_INJECT_PUSHREPLY_FAULT`, reverted before shipping) forced the real
  darlingserver to throw in the `push_reply` handler on a genuine `push_reply`
  from a Homebrew build fork-storm. The server logged the forced throw, the
  event-loop guard dropped the message, the **server stayed alive (0
  terminate)**, and — crucially — the guest thread whose `push_reply` was
  sabotaged **kept running** (continued sigexc/PH processing ~80 s later)
  instead of wedging in `recvmsg`. Pre-fix, the leaked pipe would have blocked
  that thread at the injection point forever.
- Clean boots (no injection) are non-regressive: guest boots, server comes up
  and shuts down cleanly, no orphaned guests.

## Scope notes

- Stacks on `processcall-exception-guard` (`a52a15e`): only with throws
  contained does the leak manifest as a hang rather than a crash, so the two
  are a matched pair for this bead family.
- Could not drive a full guarded brew build to completion: brew's
  download_queue psynch `cond_wait` deadlocks on nearly every boot
  (orthogonal sibling defect), independent of this fix. The injection +
  mechanism tests are the deterministic proof.
