# FD-less vchroot production port

This patch set removes the `@fd` transfer from the production `vchroot` RPC without weakening the FD-isolation constraints.

## Exact bases

- `darling-next/darlingserver`: `82f41e2e0dc352c18dd8910963d908395c94afda`
- `darling-next/darling-xnu`: `88dcbf670cd4d1c000dd7f7d95324784bafb0dca`

These are the ring-era source commits available from the handoff evidence. The later private keeper SHAs are not available through the connected GitHub repository, so the patch is intentionally anchored to the exact available commits instead of being rebased onto public `main`.

## Production design used by this patch

The source audit changed the narrow vchroot port in an important way: vchroot does not need the generic side-effecting execution-plan machinery. The server never performs a filesystem operation through the transferred dirfd; it only converts the received duplicate to a path string. The guest already computes exactly that path.

Therefore the production RPC is reduced to a path snapshot:

1. The guest resolves `/proc/self/fd/<dfd>` **before** issuing the RPC into a function-local 4096-byte snapshot.
2. The guest sends `(path pointer, path_size)` instead of `@fd`.
3. The server copies those bytes immediately into server-private memory, validates the bounded absolute path, and stores the path string on `Process`.
4. The guest installs the same snapshot into its own `prefix_path` only after the RPC succeeds.
5. No descriptor is duplicated or transferred to darlingserver.

This is narrower than the generic plan model and removes a race in the old implementation: previously the server captured a duplicate of `dfd`, returned, and only then the guest independently read `/proc/self/fd/<dfd>`. A concurrent `dup2()` could therefore make server and guest cache different roots. With one pre-RPC path snapshot, both consume the same bytes.

## Files

- `darlingserver.patch`
  - removes `@fd directory_fd` from the schema;
  - adds `path + path_size`;
  - makes `Call::Vchroot` copy the path from guest memory;
  - removes `_vchrootDescriptor` and makes `Process` store only the cached path.
- `darling-xnu.patch`
  - snapshots the local fd path first;
  - sends the path snapshot through the new RPC;
  - commits the same bytes to `prefix_path` after server acceptance.

## What this patch deliberately does not add

The external production-semantic gate proved the generic `(fd,generation)` lock, `decision_id`, replay state, and full-parameter MAC model. Those mechanisms are still required for operations that execute later against an FD or have side effects. They are **not needed by this vchroot setter after this port**, because no delayed FD-relative operation remains: vchroot becomes a one-shot path-state update.

The four genuinely-server-owned RPCs (`checkin`, `checkout`, `ring_attach`, `push_reply`) remain a separate track.

## Validation required after application

1. Regenerate RPC headers/wrappers and rebuild darlingserver + xnu consumer.
2. Run the existing vchroot tests plus a concurrent replacement test:
   - one thread repeatedly swaps `dfd` with `dup2`;
   - each successful `__darling_vchroot` must leave server and guest with exactly the same root path snapshot.
3. Verify `dserver_rpc_vchroot` messages carry zero SCM_RIGHTS descriptors.
4. Re-run ordinary pinned-Docker acceptance; no privileged options.

The connected GitHub App could read the upstream source but returned HTTP 403 when asked to create an upstream fix branch, so these patches are staged in the writable workspace repository rather than silently editing upstream `main`.
