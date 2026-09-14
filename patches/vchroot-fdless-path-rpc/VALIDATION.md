# Validation status

## Source-grounded checks

The patch is anchored to exact handoff-era commits and was derived against those files, not against public `main`:

- darlingserver `82f41e2e0dc352c18dd8910963d908395c94afda`
  - the `vchroot` schema contains exactly one `@fd directory_fd` input;
  - the same generator already uses `const char*` + private `uint64_t` for pointer RPC parameters (`set_executable_path`), so the replacement representation is native to the RPC generator;
  - `Call::Vchroot::processCall` only passes the received FD to `Process::setVchrootDirectory`;
  - `Process::setVchrootDirectory` only uses that FD to `readlink("/proc/self/fd/<n>")` and cache the resulting string;
  - child-process inheritance copies the cached vchroot path, so the descriptor itself is not required for inherited semantics.
- darling-xnu `88dcbf670cd4d1c000dd7f7d95324784bafb0dca`
  - `__darling_vchroot` currently performs the FD-bearing RPC first and independently `readlink`s the local fd afterwards;
  - the file already has a 4096-byte `prefix_path` and raw `readlink/readlinkat` path resolution.

## Executable race regression

`vchroot-path-snapshot-gate.c` was built with:

```text
gcc -std=gnu11 -O2 -Wall -Wextra -Werror
```

Host result: PASS.

The positive control demonstrates the old ordering can diverge:

```text
server keeps a dup of OLD dfd -> concurrent dup2 replaces guest fd -> guest readlink sees NEW path
```

The proposed ordering stays coherent:

```text
guest readlink snapshot -> concurrent dup2 replaces fd -> server consumes snapshot -> guest publishes same snapshot
```

The gate additionally proves that the working FD number names the replacement after `dup2`, while both sides still hold the original pathname snapshot. An invalid/closed FD is rejected before the RPC.

See `vchroot-path-snapshot-gate-host-final.json`.

## Existing generic security evidence

The attached production-semantic gate already established the more general machinery for delayed FD-relative execution: generation tracking, one authoritative descriptor-table lock, replay/decision state, and full-parameter plan MAC. This vchroot port does not instantiate that machinery because after the source audit there is no delayed FD-relative operation left in this RPC.

## Not yet claimable

- The upstream source repos were **not modified**: the connected GitHub App returned HTTP 403 when creating an upstream `fix/*` branch.
- `git apply --check` against complete local checkouts was not possible in this runtime because outbound Git clone/DNS is disabled. `apply-and-check.sh` performs that exact-base check in a real workspace.
- RPC wrappers have not been regenerated here.
- darlingserver/xnu have not been rebuilt here.
- no guest prefix acceptance run has been performed for these exact patches.
- Docker is unavailable in this ChatGPT runtime; the new narrow path-snapshot gate is host-only. The previously supplied generic production-semantic gate already had three matching pinned-Docker runs.

Those are the remaining validation steps after applying the staged patches in the real workspace.
