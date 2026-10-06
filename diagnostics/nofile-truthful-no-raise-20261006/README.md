# truthful-no-raise NOFILE acceptance evidence (dar-dar6x4-perf-5dq.34)

Architecture decision applied here: **DarlingServer must not raise RLIMIT_NOFILE at runtime**. The inherited
Linux limit is authoritative for the server and every descendant; no hidden server-only raise, no conditional
raises. This directory holds the acceptance evidence for that decision.

## Artifacts

| file | what it is |
|---|---|
| `bootstrap-receipt.json` | canonical `west darling-bootstrap` run on the pinned state: build, deploy, doctor, guest smoke marker |
| `deploy-verify.txt` | `darling-deploy-verify.sh`: sha256 of the BUILT file against EVERY deployed copy |
| `component-sha256.txt` | built vs deployed hashes for the launcher and the server |
| `contract-run.txt` | `tests/run-truthful-nofile-contract.sh` with `DW_NOFILE_PREFIX` set: audit + runtime layers |
| `fd-slope-matrix.txt` | `scripts/darling-fd-slope.sh` with the true per-thread holder, N=1/8/16/32 |

## Product state

| item | value |
|---|---|
| darlingserver | `c338f90a6499c6cdfbb745a12541486037068134` (branch `fix/truthful-nofile`) |
| launcher (`darling`) | `060fc61c5366be246580e41f3d6a6043bcdd13d8` (same branch name, in the darling repo) |
| deployed server sha256 | `9116397fa833045193baeedfe9792f0565d5fa803356f65b1b862d1239817376` (built == deployed) |
| deployed launcher sha256 | `1daaa26758e4874a5045da261352bf6b366453950ad569137b0ae6fef6d4a567` (built == deployed) |
| manifest pins | `west.yml` now pins both of the above revisions, so the bootstrap reports no manifest drift |

## Gate results

1. **No runtime raise.** The server's startup raise to `/proc/sys/fs/nr_open` and the child-side compensating
   restore are gone; the ASAN special case that existed only because of the raise went with them. Audit layer:
   no `setrlimit(RLIMIT_NOFILE)` and no `nr_open` read anywhere in the server (comments stripped, so the audit
   matches code rather than the prose explaining the removal).

2. **Inherited values are truthful.** With the inherited soft limit at 256, the witness saw `darling`,
   `darlingserver`, `mldr` and the guest command all holding exactly 256 -- nothing above the inherited value,
   over a 2.4 s window at 10 Hz. This is the measurement that distinguishes a raised server from a truthful
   one, and it is why the limit is lowered rather than assumed.

3. **Normal supported limit boots.** Bootstrap on the pinned state: exit 0, `WEST_PREFIX_BOOTSTRAP_OK`, doctor
   healthy (0 problems, 0 warnings, 1 passed), plus 7941 passed checks on the earlier run of the same pair.

4. **Insufficient limit refuses legibly.** At inherited 32 the server prints one diagnostic naming the inherited
   soft limit, the required value and the reason, and exits rc=1: no signal, no guest process, no 30-second
   shellspawn timeout (0.0 s measured). The launcher had to be fixed for the last part: it judged the server
   alive with `kill(pid, 0)`, which succeeds for a zombie, so it previously spun 31 s after the server had
   already refused.

5. **1/8/16/32 FD matrix, zero hidden slope per thread.** With the true holder (`stress_pool <live pairs>`):
   guest_peak = 10 at every size; server_peak = 65 at N=1/8/16 and 67 at N=32. A per-thread AF_UNIX endpoint
   would add one descriptor per thread, i.e. +31 at N=32; the observed +2 at the largest size is not
   proportional to thread count and is recorded as observed rather than smoothed. All four runs PASS with the
   workload's own `pass=1` line.

## The requirement constant

The server's low-limit check compares the inherited soft limit against a requirement DERIVED in code from
measured per-role descriptor peaks on the canonical build: `darlingserver 50`, `launchd 28`, `shellspawn 23`,
`launchctl 21`, `launcher 10`; each role gets a quarter for bootstrap churn and the largest is aligned to 16 =>
64. The observed boot wall (PASS 59 / FAIL 58, bisected) is recorded in the same comment as a cross-check and is
explicitly not the source of the value.

## Reproducing

```
# contract (audit only, no prefix needed)
bash tests/run-truthful-nofile-contract.sh

# contract with the behavior layer
DW_NOFILE_PREFIX=/path/to/prefix bash tests/run-truthful-nofile-contract.sh

# FD matrix (run from the manifest repository root: the tool resolves scripts/dwdiag relatively)
DW_NOFILE_PREFIX= bash scripts/darling-fd-slope.sh --prefix /path/to/prefix --threads 1,8,16,32 \
    --holder-iters 60 --hold-seconds 60
```

## Not in scope here

* The window-race harness (`tests/limit_window_race_proof`) is **N/A** for this architecture: the raise is gone
  and no transient limit mutation exists, so there is no window to race. It stays as a regression gate for a
  future design that introduces one.
* No strict "virtual cap" enforcement: the decision replaced that aim with a truthful inherited limit.
