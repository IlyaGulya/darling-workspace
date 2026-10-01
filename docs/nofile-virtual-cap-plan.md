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
