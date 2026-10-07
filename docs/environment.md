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

## Legacy workspaces (read-only, do not develop here)

    /home/ilyagulya/work/darling-dev          legacy; its root AGENTS.md points at the canonical workspace
    /home/ilyagulya/work/darling-gwn-resume   legacy (Ring line); retained for its beads delta

Their unique state is preserved outside any GC-managed root. `darling-dev`'s working tree also held
~2.6k uncommitted lines that existed as no reachable commit anywhere (rootless_shutdown_lifecycle.py
174 -> 1364 lines, test_prefix.py +839, and the lifecycle module/contracts): they are now a keeper
branch of that workspace, `legacy/in-progress-rootless-lifecycle-20261007` = 350d6b58.

    /home/ilyagulya/work/wr-fresh/hunt/ws-retire-2026-10-07/
        unpublished-branches.bundle   sha256 15963b43aeb21f693d7e3157a480b8a3e2279bf2754b90114d473ec6668ce99e
                                      (62 refs; regenerate with `git bundle create <file>
                                      $(git for-each-ref --format='%(refname)' refs/heads | tr '\n' ' ')`
                                      and verify with `git bundle list-heads <file>`)
        darling-dev-uncommitted.patch sha256 bac3ccde0a61607547b076359ad895e607233cafb5571449629a9c2b105bdfb1
        ios-runtime-additions-receipt.json  the declared additions deployed into prefix-ios

## Cleaned on 2026-10-07

19 disposable prefix directories (ring/candidate/debug/incomplete) and 2 superseded build
directories were removed after verifying no live process or mount owned them. Their deploy receipts
are kept in `hunt/env-hygiene-2026-10-07/prefix-receipts/`. Left in place deliberately: the two
prefixes above, `hunt/` evidence, the runtime build cache, and both legacy workspaces.
