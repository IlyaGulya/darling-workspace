# LibreSSL hosted immutable-tag pilot

This is the second independent schema-v2 production pilot. It freezes the
one-commit LibreSSL 2.8.3 NIST strict-aliasing fix without relying on an active
West worktree, a local branch, or any hidden object.

The reviewed lock is `locks/patch-stack/libressl-283-nist-v1.yml`. Upstream
provenance is `darlinghq/darling-libressl`; the canonical immutable mirror is
`darling-next/darling-libressl`.

```text
refs/tags/patch-stack/v1/bases/2a56b36b77a00573c53ccd8e6932eb136172c950
refs/tags/patch-stack/v1/sources/7599e3e01d3b1a742aa0293123290f267b6eb720
```

The mirror must have an active no-bypass tag ruleset covering
`refs/tags/patch-stack/v1/**/*` that blocks update and deletion. Publish only
create-only lightweight tags and fail closed when an existing ref peels to a
different commit.

## CWD-independent materialization and verification

Set both paths explicitly; do not rely on the caller's current directory:

```bash
workspace=/absolute/path/to/darling-workspace
repo=/tmp/libressl-patch-stack-clean
base=2a56b36b77a00573c53ccd8e6932eb136172c950
source=7599e3e01d3b1a742aa0293123290f267b6eb720

git init "$repo"
git -C "$repo" remote add origin https://github.com/darling-next/darling-libressl.git
git -C "$repo" fetch --no-tags origin \
  "refs/tags/patch-stack/v1/bases/$base:refs/tags/patch-stack/v1/bases/$base" \
  "refs/tags/patch-stack/v1/sources/$source:refs/tags/patch-stack/v1/sources/$source"
git -C "$repo" switch --detach "$source"
west patch preflight --repo "$repo" \
  --lock "$workspace/locks/patch-stack/libressl-283-nist-v1.yml" --json
git -C "$repo" fsck --no-dangling
git -C "$repo" format-patch --stdout "$base..$source" > /tmp/libressl-review.mbox
```

`project.path: .` in schema v2 means the Git top-level supplied by `--repo`.
The expected source tree is `c8ba46eeec25cfc71d0642c253e4ebe18c2898ae`.
