---
name: darling-boot
description: >-
  Cleanly tear down and boot a Darling prefix without wedging launchd. Use
  before any live boot/smoke test in the Darling workspace (guest launch,
  shellspawn, clang-in-guest, DCC2 smoke). Prevents the perf#23a wedge where
  rapid boot/kill churn hangs launchd in __skb_wait_for_more_packets and a
  leftover orphan mldr/launchd silently blocks the next boot.
---

# darling-boot: clean teardown + single boot

Darling boots via a setuid launcher; a botched teardown leaves an **orphan
`launchd`/`mldr` process tree** that silently blocks the next boot (shellspawn.sock
never appears). Rapid boot/kill churn additionally wedges launchd in
`__skb_wait_for_more_packets` (perf#23a). This skill enforces the only reliable
sequence: **full teardown → kill orphans by PID → settle → ONE background boot → poll**.

## When to use
Before ANY live boot or in-guest smoke test: `darling shell`, `shellspawn`,
`/usr/bin/true` in guest, clang-in-guest, DCC2 live smoke, closure boot tests.

## Preconditions (verify first)
- `west darling-doctor` is green (manifest/build/deploy aligned). If it fails, STOP
  and fix drift first — do not boot a mismatched prefix (that was #89/#90).
- Know your prefix: `DPREFIX` (default `~/.darling`); the launcher lives at
  `~/work/darling-prefix/bin/darling`.

## Protocol (do these in order — do NOT skip the orphan kill)

1. **Graceful shutdown** (ignore failure — the point is best-effort):
   ```
   darling shutdown 2>/dev/null || true
   ```

2. **Kill the process tree by PID** — server, vchroot, AND any orphan
   `mldr`/`launchd`. This is the step people skip; the orphan is the real confound.
   ```
   pgrep -f 'darlingserver' | xargs -r kill 2>/dev/null || true
   pgrep -f 'vchroot'       | xargs -r kill 2>/dev/null || true
   pgrep -f 'mldr'          | xargs -r kill 2>/dev/null || true
   pgrep -f 'sbin/launchd'  | xargs -r kill 2>/dev/null || true
   ```
   GOTCHA: the Bash tool aborts a compound command when `pkill`/`kill` exits 1
   (nothing matched). Always use `xargs -r` (skip if empty) or append `|| true`.
   Never let a "no process" exit 1 abort the teardown.

3. **Confirm nothing is left**:
   ```
   pgrep -af 'mldr|darlingserver|vchroot|sbin/launchd' || echo "clean"
   ```
   Must print `clean`. If not, kill remaining PIDs explicitly and re-check.

4. **Settle** — give the kernel time to tear down sockets/namespaces. A short
   real wait (2–3 s), NOT a tight retry loop. Churn = wedge.

5. **ONE background boot + poll** — start exactly one boot, then poll for
   `shellspawn.sock` (or your success signal). Do NOT launch multiple boots, and
   do NOT wrap the boot in an aggressive timeout-kill that races the poll.
   ```
   # start the single boot (background), then poll for readiness
   darling shell true &            # or your specific smoke command
   # poll: look for shellspawn.sock / expected stdout, up to ~10s
   ```

6. **On success**: run the smoke. **On failure/hang**: go back to step 2 (kill
   orphans) BEFORE retrying — never retry a boot on top of a half-dead tree.

## Teardown after the test
Repeat steps 1–3. Leaving an orphan launchd running is what silently breaks the
*next* session's boot. End every boot session on a verified-`clean` process table.

## Hard rules
- Never tight-loop boot/kill (perf#23a wedge).
- Never assume "shutdown" cleaned up — always verify with `pgrep`.
- Never boot when `west darling-doctor` is red.
- Never touch prod baseline binaries during boot testing (dyld 79b22273 /
  mldr f0cd2a82 / dserver 835946f9). Restore byte-identical if you deployed.
