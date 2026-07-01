# PR draft — darlingserver: contain exceptions thrown from processCall

- **beads:** dar-gwn.1.5 (parent: dar-gwn.1 / epic dar-gwn; relates to dar-pot)
- **repo:** darlinghq/darlingserver
- **branch:** `fix/processcall-exception-guard` (off `origin/main` `89751e6`)
- **files:** `src/thread.cpp`, `src/call.cpp`, `src/server.cpp`
- **status:** blocked (local-only) — see "Verification & open items" below; not yet upstreamable.

## Title
thread/call/server: contain exceptions that can terminate the server so a single bad RPC can't kill it

## Body
A `Call::processCall()` implementation can throw. Several do, by design, on races
that are routine under a heavy fork/exec workload (e.g. building Homebrew formulae):

- `std::system_error` from `Process::writeMemory` / `readMemory` / `memoryInfo` /
  `memoryRegionInfo` when the target guest thread or process dies concurrently with
  an in-flight RPC that references it (ESRCH/EFAULT).
- `std::runtime_error("Main thread for process died?")` from the exec-replacement
  path in `Process`, reached on essentially every `posix_spawn`/`execve`.
- `std::system_error` from the S2C mmap/mprotect/msync helpers in `Thread`.

Additionally, `Call::callFromMessage()` itself can throw during message
construction/dispatch — most importantly
`std::runtime_error("Thread's pending call overwritten while active")` from
`Thread::setPendingCall()` when an RPC races a still-pending call (routine under a
heavy fork/signal storm, e.g. building a Homebrew formula), plus
`std::invalid_argument` for a malformed/unknown call.

**Three** sites were therefore exposed, all with **no surrounding try/catch**:

1. `Thread::microthreadWorker()` runs on a `makecontext()`-established fiber stack
   and never returns normally — it always `setcontext()`s away at the end. An
   exception unwinding out of it therefore runs off the top of a fiber stack, which
   is undefined behavior and in practice reaches `std::terminate()` → `abort`/exit.
   **The entire darlingserver process dies.** Every other guest that had an
   in-flight RPC is then stranded forever blocked in `recvmsg` on its per-thread
   RPC socket (the socket is never EOF'd), and the whole guest tree is orphaned.
   The existing dispatch code already carried a `// TODO: wrap all processCall
   calls in try-catch like this`.
2. `Call::callFromMessage()`'s `kernelAsync` path (thread-less calls) had the same
   exposure.
3. `Call::callFromMessage()` is invoked **directly in the main event loop**
   (`Server::start()`), again with no try/catch, so a throw during message
   construction (e.g. the pending-call-overwrite race) unwinds out of the event
   loop and terminates the server just the same — a distinct exit path from #1/#2.

### Fix
Wrap all three invocations.

- In `microthreadWorker`, on a caught exception, log the offending call number and
  attempt `Call::sendBasicReply(-code)` so the originating client gets an error
  reply instead of hanging. Calls that return data don't override `sendBasicReply`
  (the base implementation throws); that throw is caught and ignored. Then the
  normal post-call `setcontext` flow runs, so **the server survives**.
- In the `kernelAsync` path there is no client thread to reply to, so it just logs
  and drops the call.
- In the event loop, log and drop just the one offending message so the server keeps
  serving every other guest.

This converts "one unlucky RPC race terminates the server and wedges every guest"
into "that one RPC fails (error reply or dropped); all other guests keep working."

### Why this is the right layer
The throwing operations are inherently racy (they touch another process's state
that can vanish at any moment), so the call sites must tolerate failure. Catching at
the dispatch boundary is the minimal, localized place that covers every current and
future `processCall` without auditing each handler. A fuller follow-up could give
every call a typed error reply (so even data-returning calls unblock their client),
but server-survival is the load-bearing fix.

### Verification & open items
- Built and deployed; boots cleanly. Non-regressive across repeated cold
  `brew install libunistring` boots: 0 zombie darlingservers, 0 orphaned guests after
  cleanup — the dar-pot mass-leak symptom did not occur.
- **One clean before/after on the event-loop site (#3) was captured.** Same
  `brew install libunistring` boot: the leg-B fork-storm SEGV fires and the guest
  then issues an RPC that hits the pending-call-overwrite race. Pre-fix the server
  printed `terminate called after throwing ... what(): Thread's pending call
  overwritten while active`, **died**, and orphaned ~14 `mldr` guests (dar-pot).
  Post-fix the same SEGV fires but the server **stays alive**, prints no terminate,
  and the boot exits cleanly.
- Caveat: the overwrite race is flaky, so the guard's `Dropping message …` log line
  was not separately observed firing in an additional controlled boot; the
  justification rests on that one before/after plus the static proof that this is the
  only uncaught throw site on the event-loop path (and, for #1/#2, live `__cxa_throw`
  tracing that showed `std::system_error`/`std::runtime_error` thrown repeatedly in
  the fork-storm window).
- Two separate, pre-existing defects still block a green `brew install` and so block
  publication: **(B) the leg-B root cause itself** — a client-side spurious `SIGSEGV`
  (a signal delivered to a syscall-parked guest thread via the sigexc path, arriving
  SI_USER, tagged 11) which is what *triggers* the overwrite race this patch merely
  contains; and (C) the dar-gwn.1 fetch-phase reply-loss hang. Neither is fixed here —
  this patch stops the cascade from killing the server, it does not stop the SEGV.
