# NOFILE virtual cap: contract and measurement plan (dar-dar6x4-perf-5dq.34)

## The contract, restated from the authoritative Bead (not from handoff prose)

`dar-dar6x4-perf-5dq.34` is `perf#21c: strict guest NOFILE virtual cap (boot-compat investigation)`, activated
2026-10-01. Its own comment of 2026-09-28 reopens the premise from the transport work with these measurements:

* hidden transport FD slope per thread = 0 (server peak 63 and guest peak 8 at 1, 8, 16 and 32 live guest threads);
* per-thread RPC socket physically removed, `created=0 denied=0`;
* process doorbell: ONE eventfd per Linux guest process, `epoll_wait(-1)`, no periodic polling;
* legacy ordinary AF_UNIX = 0, zero-fd AF_UNIX wake = 0;
* courier: SCM_RIGHTS only, every data packet carries descriptors.

The work it asks for, in its own words: revisit the previously rejected strict-NOFILE design on THIS topology,
where "all internal process anchors [are] established during controlled bootstrap -> truthful Linux
RLIMIT_NOFILE installed before ordinary guest activity -> no temporary runtime raise visible to a multithreaded
guest"; the runtime raise/lower design is NOT to be reintroduced; and the hidden-band/race test is to be
repeated on the new topology. The earlier draft is recorded as FALSIFIED ON A PROCESS-WIDE RLIMIT_NOFILE RACE,
and that finding stands as the standard the retest must be judged against.

## Architectural target (unchanged)

* hidden transport FD slope per thread = 0;
* no temporary runtime NOFILE raise (the raise/lower design stays dead);
* process-level shared transport resources only.

## Baseline measurements to take before any design work

| item | how | status |
|---|---|---|
| host soft/hard RLIMIT_NOFILE | `ulimit -Sn`, `ulimit -Hn`, `/proc/self/limits` | 1048576 / 1048576 |
| guest soft/hard RLIMIT_NOFILE | sampled from `/proc/<pid>/limits` for every prefix process during a thread-parked run | 1048576 / 1048576 for launchd, shellspawn, ring_mach_msg_test and launchctl |
| hidden transport FD slope per thread | `scripts/darling-fd-slope.sh --prefix P --threads 1,8,16,32` | 0: server_peak 65 and guest_peak 10 at all four sizes, all four runs PASS with denied=0 created=0 |
| any NOFILE raise on a bootstrap or test path | search the tree for `setrlimit(RLIMIT_NOFILE)` with an increased `rlim_cur` | FOUND: darlingserver.cpp reads the default limit at startup, raises the soft limit to `/proc/sys/fs/nr_open`, and lowers it back to the default before spawning the child that becomes launchd; mldr only reads the limit |

## Measurement caveat that must travel with every number

The runtime available today is built from the frozen scratch tree, so every figure below is a NON-CANONICAL
baseline: `dwdiag source --provenance` reports it as untracked and refuses to let a run from it be product
evidence. The numbers are still worth taking, because they tell the next session whether the topology still
behaves as the transport phase measured, but they must be repeated on a canonical materialization before any
NOFILE conclusion is drawn.

## What the first measurement round already says

The runtime raise/lower pair is real and its shape is exactly what the contract forbids: a process-wide raise at
server startup (`darlingserver.cpp`, raise to `nr_open`) that is lowered again just before the child that becomes
launchd is spawned. On THIS host the raise is invisible because the host's own soft limit already equals
`nr_open` (1048576), so every prefix process -- launchd, shellspawn, the workload and launchctl -- was observed
with soft = hard = 1048576 and no window with a different value appeared. That is a host-dependent observation,
not a property of the design: on a host whose soft limit is below `nr_open` the raise would be observable, and
the process-wide window is the race the earlier design was falsified on.

So the next measurement is not "does the raise exist" (it does) but "can a multithreaded guest observe it": the
comparison has to be made with the guest's own soft limit lowered first, which a test can do from inside the
guest, and the observation window has to cover the whole bootstrap.

The FD slope and the guest limits above were taken on the frozen scratch runtime, so they are a NON-CANONICAL
baseline (see the provenance witness): they describe this topology, and no NOFILE conclusion may rest on them
until they are repeated on a canonical materialization.

## Round two: the raise, and what each side actually obeys (measured 2026-10-01)

The first round could not answer the question because this host's default soft limit already equals `nr_open`,
so the server's raise changes nothing. The question was made askable by lowering the run's soft limit only
(`ulimit -S -n 4096`, hard left at `nr_open`) -- lowering both, as `ulimit -n` does, makes the raise fail with
`Operation not permitted`, which the server logs as `Warning: failed to increase FD rlimit` and then tolerates.

With the soft limit lowered for the run and the hard limit at `nr_open`:

| process | soft limit observed | how |
|---|---|---|
| `darlingserver` | 1048576 for 38468 samples, 4096 for 19 | tight watcher from the moment the process appears |
| `launchd`, `shellspawn`, the ring workload (all `mldr`) | 4096 for the whole run | `/proc/<pid>/limits` sampled while the workload held 8 threads parked |
| launcher `darling`, harness `dash`/`timeout`, the tool | 4096 | same sampling |

So: the raise is real and succeeds when the hard limit allows it, and the server KEEPS it -- from its own point
of view the limit is raised for the whole run, not temporarily. Every guest process nevertheless held the
truthful inherited value, which is what the contract demands: the child that becomes launchd is restored to the
default before it is spawned, and no guest was ever observed with the raised value.

What is still open, and is the actual contract question: the window between the server's raise and that restore.
A process created inside that window would inherit 1048576; the launchd child does not, which shows the restore
precedes that particular spawn, but the window is not proven empty for every creation path. That is the next
measurement, and it is a timing question about the server's own bootstrap rather than a question about guests
holding threads.

## Round three: the window is closed by construction, not by timing

The remaining question was whether a process created between the server's raise and the restore can inherit the
raised limit. Reading the code answers it more cheaply than timing it, and the answer is structural:

* `darlingserver.cpp` contains exactly ONE `fork()` in the whole file (line 1446), which creates the child that
  becomes launchd;
* the raise to `nr_open` happens earlier, at server startup (line 1330);
* the lower back to the default happens at line 1495 -- INSIDE THE CHILD, which does it after the fork, before
  it hands over the green light and becomes the guest.

So the raised value exists in the child for the short window between the fork and its own restore, during which
that child is a single-threaded process that has not yet become a guest. A multithreaded guest cannot observe
it, because the guest's threads are created later, after the limit has already been lowered by the child
itself. The measurement agrees with the structure: with the run's soft limit lowered to 4096, every guest
process held 4096 for its whole lifetime while the server held 1048576.

CONCLUSION SO FAR (non-canonical runtime, and therefore a hypothesis to re-confirm on the canonical build): the
process-wide raise does not leak into multithreaded guests, and the contract's "no temporary runtime raise
visible to a multithreaded guest" holds for the creation paths this build has. The raise/lower design remains
forbidden by the Bead for any NEW work; what is established here is only that the existing one is not currently
observable by guests.

## Round four: the boot-compat floor under a strict guest NOFILE

The Bead is titled "strict guest NOFILE virtual cap (boot-compat investigation)", so the first thing the new
topology needs is its floor. One run per value, `r2 8` (eight guest threads parked), run's soft limit lowered
alone, hard left at `nr_open`:

| run soft limit | verdict |
|---|---|
| 256 | PASS (three times) |
| 160 | PASS |
| 128 | PASS |
| 96 | PASS |
| 64 | PASS |
| 48 | PASS |
| 32 | BOOT-FAIL (shellspawn) |
| 24 | BOOT-FAIL (shellspawn) |
| 16 | BOOT-FAIL (shellspawn), denied=1 |

So the topology boots and parks eight threads with only 48 descriptors, and the floor sits between 33 and 48.
The failure mode is NOT a plain descriptor exhaustion: the failing log ends with
`[dring-uds-reason] ... reason=NO_LANE_ENTRY`, an `[rpc-socket-DENIED] ... call=set_thread_handles ... denied=1`,
and then the launcher's `Rootless shellspawn did not become ready within 30000ms` -- i.e. the ring lane for the
thread is never established and the boot stalls, rather than some open() reporting EMFILE. That is the shape the
next investigation has to explain, using the existing read-only instruments (the trace ring, the stall dump,
`dwdiag denials`, `dwdiag trace`) rather than new probes, because the product tree is frozen until
canonicalization completes.

Caveat: all of this was measured on the non-canonical scratch runtime.
