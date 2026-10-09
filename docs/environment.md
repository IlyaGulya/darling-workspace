# Environment (one canonical workspace, one iOS experiment area)

Status of this note: normative for "where do I work". It records paths and rules, not history.
Last verified: 2026-10-07.

## Canonical product workspace

    /home/ilyagulya/work/wr-fresh

It resolves from its own manifest (`darling-workspace`, branch `change/ios-toolchain`) and from the
durable product refs recorded in the handoff. Verified state: `west status` reports no modified
project; every managed project is at the manifest revision; the only untracked path under `darling/`
is `docs/`, which is the west-managed project mount `darling/docs/darling-docs`, not an override.

Run orchestration and West commands from the manifest directory, not the workspace root:

    cd /home/ilyagulya/work/wr-fresh/darling-workspace
    mise run west -- status
    mise run dw dev ...

Build paths:

    .west-test/runtime-build-cache    framework-managed, identity-keyed runtime builds (~9 G).
                                      Never `rm -rf` it: its entries are registered Git worktrees.
                                      Discard through the framework.
    build/toolchain                   the iOS toolchain build (configured, reusable).

## iOS experiment area

    source workspace   /home/ilyagulya/work/wr-fresh
    build directory    /home/ilyagulya/work/wr-fresh/build/toolchain
    runtime prefix     /home/ilyagulya/work/wr-fresh/prefix-ios        (darlingserver a384108)
    diagnostic prefix  /home/ilyagulya/work/wr-fresh/prefix-ios-diag   (reserved name, see below)
    Xcode input        /home/ilyagulya/work/local-target/xcode-26.3/Xcode.app   (read-only)
    Swift overlay      /home/ilyagulya/work/wr-fresh/toolchain-overlays/swift-6.3.1-macos

Rules that keep the area usable:

* The canonical iOS prefix is never modified by copying random binaries into it. Every intended
  runtime addition goes through `scripts/ios-toolchain-runtime-additions.sh`, which installs each
  artifact with its own project's CMake install target and refreshes the guest-visible shadow copy.
* A diagnosis that needs a mutated runtime uses the explicitly named `prefix-ios-diag`. The hand
  copy of the Swift toolchain lives in the overlay above with its provenance; it is diagnostic
  evidence, never a product runtime, and it is never copied into `prefix-ios`.
* A diagnostic prefix may be thrown away. The canonical one may not.
* The prefix must carry the T3 libc++: Apple's `ld` references `std::__fs::filesystem::path::__filename()`,
  and a prefix whose `usr/lib/libc++.1.dylib` predates the filesystem build fails the link in dyld
  ("Symbol not found: __ZNKSt3__14__fs10filesystem4path10__filenameEv") rather than hanging. Deploy it
  with `scripts/ios-toolchain-runtime-additions.sh --build-dir <build/toolchain> --prefix prefix-ios
  --receipt <file>`, which refreshes the guest-visible shadow copy too; never by copying the dylib by
  hand, because a hand copy is exactly how a stale libc++ came back and cost a bisect.

## Xcode input (read-only)

    path          /home/ilyagulya/work/local-target/xcode-26.3/Xcode.app
    version       26.3
    build         17C529
    SDK           iPhoneOS26.2 (`.../Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS26.2.sdk`;
                  `iPhoneOS.sdk` is a convenience symlink to it)
    size          12 G; the unpacked `.xip` (Content / Content.cpio / Metadata) sits in
                  /home/ilyagulya/work/local-target/xip
    Swift package /home/ilyagulya/work/local-target/swiftpkg/swift-6.3.1-RELEASE-osx-package.pkg

Do not modify the Xcode tree in place and do not scatter copies into `/tmp`.

## Prefixes

    prefix-dtape-ts   the DTAPE .4b product runtime (darlingserver cd897eb) -- KEEP as the known-good
                      product prefix, but note it is manually MUTATED: it carries the Swift overlay
                      (23 files under usr/lib/swift) and zlib additions, so it is product runtime plus
                      diagnostic overlay, not a clean accepted-product prefix.
    prefix-ios        the current iOS experiment prefix (darlingserver a384108).
    prefix-ios-diag   reserved for deliberate manual mutation.

How to create a fresh prefix (its own invocation, not combined with a test selection):

    cd /home/ilyagulya/work/wr-fresh/darling-workspace
    mise run west test --prefix /absolute/path --bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal
    # or ...-guest-toolchain-provisioning to also install the reviewed guest CommandLineTools
    mise run west -- darling-prefix-repair --prefix /absolute/path   # if prerequisites are reported missing

Long runs are owned by a job, never by an external `timeout`/kill: use `scripts/west-job.sh
start|follow|cancel`, or `west dev run ... --detach` with `west dev follow|cancel`. A run killed from
outside leaves a daemonized `darlingserver`; `scripts/darling-boot-run.sh` now detects it through the
prefix directory capability and `.init.pid` and clears stale runtime files before the next boot.

## Retired legacy workspaces

`/home/ilyagulya/work/darling-gwn-resume` and
`/home/ilyagulya/work/darling-dev` have been removed. Do not recreate them as
source authorities. The full legacy/runtime archives have also been removed.
Useful source candidates, repros, diagnostic records and cleanup receipts are
retained outside GC roots in `<workspace>/.hunt-value-audit/keepers.tar.gz`,
with `keepers.SHA256SUMS` and the archive's `keepers-manifest.json`.
This is selective retention, not a backup of every old file or Git reference.
The archive-removal receipt is
`<workspace>/.hunt-value-audit/cleanup/deletion-result.json`.

The external `darlingserver-port`, `darlingserver-timerfix`, and `tmp-apply-probe`
worktrees use the live common store under
`hunt/delete-prep-20261009/legacy-debugger/retained-git/darling/src/external/darlingserver/.git`.
This retained Git store is not disposable cache: keep it while its worktrees
depend on it. Compact migration and source-identity records are retained in
`<workspace>/.hunt-value-audit/runtime/compact-cleanup-receipt.json` and the keeper package.

Runnable diagnostics must resolve the manifest/source workspace from their own
location, not either legacy root. `r1-clean-base-reproduction.sh` uses the
canonical sibling Darling repository for its unchanged pinned commit and writes
new evidence under `<workspace>/hunt/r1-clean-base-reproduction`.
The census runners use the maintained `scripts/darling-stat` client, with their
existing explicit tool overrides. The demux fixture requires an explicit
`DEMUX_FOREST` containing the reviewed generator and header inputs; missing
historical inputs must not silently become a run against current source.
Retained evidence belongs outside framework GC roots; it is not an executable
default or a second source authority. Keep the selective package and its checksum
while the contained unmerged candidates and historical repro inputs remain useful.
The retained R1 content-baseline capture is under
`<workspace>/.hunt-value-audit/legacy-source/keepers/r1-source-baseline/`.
Its old absolute pointers are provenance, not valid checkout paths.
The former `350d6b58` Python lifecycle prototype was explicitly superseded by
the Rust controller; it is not an unfinished product fix to restore.

## Cleaned on 2026-10-07

19 disposable prefix directories (ring/candidate/debug/incomplete) and 2 superseded build
directories were removed after verifying no live process or mount owned them. Their deploy receipts
are kept in `hunt/env-hygiene-2026-10-07/prefix-receipts/`. Left in place deliberately: the two
prefixes above, `hunt/` evidence, the runtime build cache, and both legacy workspaces.

## Recreating a prefix loses the iOS runtime additions

`prefix-ios` was recreated with the declared bootstrap profile because its runtime state had become broken
(every artifact generation died with SIGSEGV at 0xFFFFFFFFFFFFFFFF after the plane steps). Recreation
restores a clean runtime but drops what had been deployed into the old prefix by hand-declared tooling: the
iOS toolchain runtime additions that `scripts/ios-toolchain-runtime-additions.sh` installs (48 guest-visible
files, zlib among them) and the reviewed guest CommandLineTools.

Symptom to recognise: the toolchain gate's T1/T2/T4 fail with `dyld: Library not loaded: /usr/lib/libz.1.dylib`
followed by `abort_with_payload: reason: dyld: No shared cache present` -- the library is absent from the
filesystem and there is no shared cache to supply it, so the real Xcode clang cannot start in the guest at
all (rc=139). Re-run `scripts/ios-toolchain-runtime-additions.sh --build-dir <build> --prefix <prefix>
--receipt <path>` before blaming the toolchain, and re-provision the CommandLineTools through the declared
profile before a guest CTest that needs them.

## The swift submodule's LFS objects, when the LFS host is unreachable

The reviewed guest CommandLineTools profile materializes the `swift` submodule
(`darling/src/external/swift`), whose 44 files are git-lfs pointers addressed by SHA-256 and served from
`https://git-lfs.darlinghq.org/lubos/darling-swift` -- a project service that needs its own credential and
that this host has none for (the only helpers present are for `github.com`). When that fetch fails the
bootstrap dies with `smudge filter lfs failed`.

The canonical checkout already holds the objects. Run from the West workspace
root: `git -C darling/src/external/swift lfs fsck --objects`.
There is no longer a legacy archive copy of the Swift LFS payload. If the
canonical store is lost, obtain the reviewed objects from a reachable LFS mirror;
do not point recovery commands at the removed legacy archive.
`fsck OK` verifies content addressing; changing `lfs.url` to a reviewed reachable
mirror changes retrieval, not the required object identity.

## A bootstrap needs the submodules at their manifest pins

Measured twice in a row: with `src/external/darlingserver` and `src/external/xnu` sitting on local
`fix/*` branches, a bootstrap built the runtime and then failed its guest smoke -- the guest printed
`[machport-open] before-rpc` (libkqueue/src/linux/machport.c:126) and never reached `after-rpc`, so it hung
inside the `kqchan_mach_port_open` RPC and shellspawn never became ready. Detaching both to the pinned
commits (a384108, f3e71f3) while the branches kept every commit made the same profile succeed unchanged
(rc=0, launcher present). `west status` showed the drift as "new commits, modified content" on the two
submodules. Keep the fix work on branches, but return the checkouts to their pins before bootstrapping, and
re-checkout the branches (or redeploy) when the fixed runtime is wanted again.
