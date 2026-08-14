# Rootless namespace-writer inventory v1

This document is a source inventory and routing gate, not a product change.
The machine-readable source of truth is
`lifecycle/namespace-writer-inventory-v1.json`; the contract prints
`coverage-tier: source` and binds every listed owner to an existing source
file and symbol anchor.

## Authority and threat model

The intended authority is one Rust-owned lifecycle controller. It opens and
retains the prefix anchor and `.lifecycle.lock`, acquires an exclusive flock,
then performs identity observations and mutations through retained
fd-relative capabilities. Python remains orchestration/transport only.

The current inventory is deliberately classified as **cooperative-writer,
global routing deferred**. Seven product records plus the Rust-owned transport
record in the first three bounded cohorts are `cohort-ready`, not globally
`compatible`; the other 74 records remain
`incompatible`. A writer is not considered routable merely because it
does an advisory check or happens to run under a West context. It must retain
the exact `.lifecycle.lock` FD and hold `LOCK_EX` before the first namespace
observation or mutation. Hostile same-UID writers are outside this v1 threat
model; if that threat model is reopened, a stronger capability boundary is
required rather than another pathname check.

## Current writers

The registry covers 143 finite production source paths (82 typed writer
records). The complete production-forest scan (anchored by the build/runtime
closure) discovers 1,754 namespace-mutation candidates: 111 hit typed owner
paths, 37 hit exact SHA-bound exclusions, and 1,606 have individual records in
`lifecycle/namespace-writer-candidate-audit-v1.json`. Every audit record has
an exact source SHA-256, finite classification, path-specific reason, matched
mutation operators, and SHA-bound build/runtime anchor. There is no implicit
remainder classification. The contract validates the mDNSResponder, libutil,
dynamic_pager, OpenSSH and newsyslog CMake/install closure, including
computed/configured path writers, and does not use the owner grouping list as
the scan universe. It also resolves 24 exact installed LaunchDaemon plist
sources to 20 unique production executable declarations and requires all 34
mutation-first sources compiled directly or through object-library closure
into those targets to be typed writers.

The first Rootless endpoint cohort now has an opt-in Rust route: one retained
prefix FD and one exact session-long `.lifecycle.lock` lease publish `.init.pid`
and four finite listeners, including the vchroot-visible controller transport.
This is recorded as `cohort-ready`; it cannot be
promoted to global `compatible` until every overlapping writer uses the same
authority. All remaining records stay `incompatible`.

The third bounded cohort routes only the Homebrew/source-line
`/private/var/log/dserver.log`. Rust opens the regular file fd-relative under
the retained prefix and session lease, retains its exact identity, and hands
Darlingserver only an append-only writer descriptor. The Perf-derived
`dserver-auxlog.txt` call sites remain explicitly incompatible until that
composition receives the same retained-prefix ABI; splitting the registry
record prevents main-log acceptance from silently promoting the aux path.

| Owner | Namespace responsibility | Phase | Current lock evidence |
| --- | --- | --- | --- |
| `darling-workspace/lifecycle/operation-boundary/src/cohort_routing.rs` | `/.lc-v1.sock` | controller start/stop | retained Rust session lease; global activation deferred |
| `darling-workspace/lifecycle/operation-boundary/src/{lib,linux_backend,quarantine_gc}.rs` | generic fd-relative mutation, backend quarantine handoff and bounded GC | infrastructure/recovery | individually typed but incompatible until a production consumer binds the complete writer set to the same exact lease |
| `darling/src/startup/darling.c` | Prefix provisioning, `.init.pid` publication/repair, stale shellspawn endpoint | create, boot, shutdown | endpoint/PID cohort ready; prefix provisioning incompatible |
| `darling/src/shellspawn/shellspawn.c` | `/var/run/shellspawn.sock` unlink/bind/chmod | runtime start/stop | opt-in Rust cohort route, global activation deferred |
| `darling/src/external/darlingserver/src/darlingserver.cpp` | `/var/run`/`/var/tmp` wipe, home layout, prefix copy/permissions/mount | runtime start/materialization | no `.lifecycle.lock` acquisition |
| `darling/src/external/darlingserver/src/server.cpp` | `.darlingserver.sock` unlink/bind/cleanup | runtime start/stop | opt-in Rust cohort route, global activation deferred |
| `darling/src/external/darlingserver/src/logging.cpp` | `dserver.log` | runtime diagnostics | opt-in Rust cohort route, global activation deferred |
| `darling/src/external/darlingserver/src/{kqchan,call}.cpp` | auxiliary RPC log | Perf runtime diagnostics | incompatible; composition lacks the retained-prefix controller ABI |
| `darling/src/launchd/src/ipc.c` | launchd socket directory and socket lifecycle | launchd start/stop | system and per-user endpoints cohort ready; global activation deferred |
| `darling/src/launchd/src/{core,launchd,log}.c` and `support/launchctl.c` | job stdio/per-user directories, generic opens, persistent logs, sockets, `/var/run` and `/tmp` cleanup, `utmpx`, `.systemStarterRunning`, mode changes | launchd boot/runtime | no `.lifecycle.lock` acquisition |
| `darling/src/xcselect/xcode-select.c` | xcode-select database links under `/var/db` and `/usr/share` | guest toolchain selection | no `.lifecycle.lock` acquisition |
| `darling/src/external/mDNSResponder/{mDNSShared,mDNSMacOSX}/...` | mDNS UDS/PID, conditional named-error sockets, and state dumps | daemon/client diagnostics and stop | no `.lifecycle.lock` acquisition |
| `darling/src/external/libutil/pidfile.c` | generic `/var/run/<program>.pid` create/write/remove helper | daemon pidfile lifecycle | no `.lifecycle.lock` acquisition |
| `darling/src/external/libc/{gen,util}` and Heimdal login helpers | utmp/wtmp/lastlog and console-message persistence | login/session recording and diagnostics | no `.lifecycle.lock` acquisition |
| `darling/src/external/OpenLDAP/.../nssov`, syslog ASL helper | nslcd listener and ASL store creation/rotation | module/syslog runtime | no `.lifecycle.lock` acquisition |
| `darling/src/external/nghttp2` and passwordserver SASL daemons | configured PID, socket, run-directory and lock files | daemon start/stop | no `.lifecycle.lock` acquisition |
| `darling/src/external/network_cmds/{rtsol,rtadvd}` and security `authd` | routing-daemon PID/dump files and `/var/db/auth.db` recovery | daemon/database lifecycle | no `.lifecycle.lock` acquisition |
| `darling/src/external/system_cmds/dynamic_pager.tproj/dynamic_pager.c` | sysctl/argv-derived swap directory cleanup, creation and ownership | boot-time swap setup | no `.lifecycle.lock` acquisition |
| `darling/src/external/openssh/openssh/{sshd,session,ssh-agent,loginrec}.c` | configured daemon PID, session/agent sockets and login records | daemon/session start, restart and stop | no `.lifecycle.lock` acquisition |
| `darling/src/external/crontabs/newsyslog/newsyslog.c` | LaunchDaemon-scheduled creation, rotation, removal and archival of configured logs | scheduled runtime log rotation | no `.lifecycle.lock` acquisition |
| CUPS scheduler `main`, `cert`, `client`, `conf`, `cups-driverd.cxx`, `cups-lpd`, `dirsvc`, `file`, `ipp`, `job`, `log`, `printers`, `process` and notifier sources | configured scheduler directories, PPD cache, job/printer state, certificates, logs, LPD files and notifier locks | installed cupsd/cups-driverd/cups-lpd runtime | no `.lifecycle.lock` acquisition |
| Heimdal KX/database, BIND named, pcscd, auditd, rsync and screen sources | computed/configured PID, lock, socket, certificate and database paths | daemon/session runtime | no `.lifecycle.lock` acquisition |
| SystemConfiguration preferences, Security database helpers and FSEvents | preference locks, corruption/keybag artifacts and listener socket | framework/daemon runtime | no `.lifecycle.lock` acquisition |
| `darling/src/external/DirectoryService/{Server,PlugIns/Local}` | DirectoryService pid/markers and shadow-auth directories/files | directory service start/auth/stop | no `.lifecycle.lock` acquisition |
| `darling/src/external/OpenDirectory/{memberd-21.1,opendirectoryd}` | memberd pid/config and opendirectory diagnostics | daemon start/config/diagnostics | no `.lifecycle.lock` acquisition |
| `darling/src/external/OpenLDAP/.../slapd/daemon.c` | slapd marker and local listener cleanup | LDAP start/stop | no `.lifecycle.lock` acquisition |
| `darling/src/external/configd/configd.tproj` | state snapshots, crash marker, configd log | configd state/boot | no `.lifecycle.lock` acquisition |
| `darling/src/external/libnotify/notifyd/notifyd.c` | notifyd status/log and shared-memory outputs | notifyd runtime | no `.lifecycle.lock` acquisition |
| `darling/src/external/security/securityd/src` | securityd shared memory, token cache and shutdown log | securityd runtime/stop | no `.lifecycle.lock` acquisition |
| `darling/src/external/syslog/syslogd.tproj` | ASL store, lockdown socket, pid/log/boot files | syslogd start/runtime | no `.lifecycle.lock` acquisition |
| `darling/src/external/syslog/aslcommon/asl_common.c` | configured ASL output/database directories, files and current-file symlinks | shared syslogd/aslmanager runtime | no `.lifecycle.lock` acquisition |
| `darling/src/external/Heimdal/lib/roken/write_pid.c` | generic `_PATH_VARRUN` pid helper | daemon start/stop | no `.lifecycle.lock` acquisition |
| `darling/src/external/{libdispatch,libpthread,libc}/...` | optional `/var/tmp` diagnostic files | guest diagnostics | no `.lifecycle.lock` acquisition |
| XNU `vchroot_userspace.c` | E-UNION copy-up, whiteouts, opaque markers, xattrs, metadata and rename/remove helpers | guest filesystem mutation | no `.lifecycle.lock` acquisition |
| XNU syscall wrappers | create/remove/rename/link/symlink/open/bind/metadata forwarding into E-UNION | guest filesystem mutation | no `.lifecycle.lock` acquisition |
| `west_commands/test_prefix.py` | Host stale-entry and runtime socket cleanup | host shutdown | flock is on prefix parent directory, not `.lifecycle.lock` |
| `west_commands/prefix_repair.py` | Prefix repair, stale PID/socket removal, tmp modes, symlink repair | preflight/repair | no `.lifecycle.lock` acquisition |
| `west_commands/deploy_transaction.py` and `test_runtime_deploy.py` | Runtime file replacement, rollback, directories and modes | deploy/restore | no `.lifecycle.lock` acquisition |
| `west_commands/test_bootstrap.py` | Runtime marker and bootstrap logs | post-deploy bootstrap | no `.lifecycle.lock` acquisition |
| `west_commands/fresh_prefix.py` | New-prefix tree and copy/reflink provisioning | prefix create | no `.lifecycle.lock` acquisition |
| `west_commands/darling_build.py` | Operator backup and deployment into closure trees and binaries | operator deploy | no `.lifecycle.lock` acquisition |
| `west_commands/guest_toolchain.py` | Package staging and guest installer writes | toolchain provisioning | no `.lifecycle.lock` acquisition |
| `west_commands/darling_prefix_repair.py` | CLI entry point for mutating repair/cleanup | operator preflight | no `.lifecycle.lock` acquisition |

The XNU list is intentionally split into a policy helper and syscall wrapper
family. The helper is the only place that decides copy-up/whiteout behavior;
the wrappers are the concrete production call paths that reach it. A future
writer added anywhere in the complete production forest must add an owner path
and anchor to the registry before it can be considered by routing.

## Source scan and exclusions

The contract scans the complete Darling production forest rooted at `src` and
the complete workspace `west_commands` and lifecycle operation-boundary
runtime roots. Recursive
`src/CMakeLists.txt` closure validates build membership, and conditional
targets such as mDNSResponder, libutil and dynamic_pager are pinned by
`build_closure` entries and their concrete source/install files. The
`production_roots` entries in the JSON are only typed-owner grouping/index
metadata; omitting one cannot hide a source candidate.
Candidate generation is mutation-first across the entire universe. It does
not prefilter source files by a runtime-path token. The narrow grammar covers
direct name creation/removal/rename/link and metadata syscalls, plus
create/truncate opens; generic read-only `open()` and unqualified `write()`
are deliberately not namespace mutations. C, Objective-C and C++ translation
units include `.cxx`; this is what keeps the installed CUPS `cups-driverd`
PPD-cache writer in the universe. Every resulting translation unit
is a typed owner, an exact content-bound exclusion, or an explicit per-path
audit record. Acceptance never classifies the unclaimed remainder: it requires
exact key-set equality, and rejects missing, foreign, duplicate or stale-SHA
entries. Build/runtime anchors are content-bound too, so a changed source or
evidence file requires a new path-level review. Pinned build/install sources are checked independently so paths
arriving through argv, sysctl, configuration or IPC remain visible. This
catches dynamic_pager, `options.pid_file` in the installed OpenSSH sshd target,
and LaunchDaemon-driven newsyslog rotation without requiring a literal `/var`
token in the discovery prefilter.

Installed-service classification is target-aware rather than proximity-based.
The contract parses real, uncommented CMake `install(FILES|DIRECTORY ...)`
calls, resolves the exact plist `Program`/`ProgramArguments` output through
`add_darling_executable` and `OUTPUT_NAME`, expands direct and same-file
`set()` source lists, then recursively follows `$<TARGET_OBJECTS:...>` object
libraries and `target_sources()` additions. Cycle, target-expansion and
source-token budgets are fail-closed. The resulting closure is intersected with
mutation-first discovery: a mutation source compiled into an installed service
cannot remain `caller-selected-library-output`; it must be a typed writer.
Embedded `${CMAKE_CURRENT_SOURCE_DIR}` and `${CMAKE_CURRENT_LIST_DIR}` path
variables are resolved relative to the declaring list; every remaining CMake
variable or generator expression in source position is rejected. Seven
configure-time MIG outputs are exact-keyed by declaring CMake/source token and
SHA-bound to their CMake declaration and `.defs` input.
The contract evaluates actual top-level `set`/`unset`/`mig(input)` calls in
source order and derives each user/server output name from the suffix state at
that invocation. Conditional or otherwise control-nested MIG calls and suffix
assignments are rejected. The 29 SHA-bound generator records are explicitly a
bounded static inventory derived from the bootstrap target and its quoted
local inputs, not a claim that a handwritten include scan represents the
complete compiler closure.

The authoritative generator proof is a bounded clean replay. The contract
builds the real `build-mig`/`migcom`, clean-configures the full source tree,
extracts each of the seven exact Ninja MIG commands, and materializes only
those outputs in one task-owned temporary root. Bash, Awk, CMake, Ninja,
Clang/Clang++, Flex and Bison paths, versions and executable SHA-256 digests
are pinned. Every generated output is bound by declaring CMake/source token,
size and SHA-256 and is scanned with the same namespace-mutation grammar. This
canonical SHA requires one exact C `ctime()`-grammar MIG `stub generated`
banner and replaces only its timestamp capture with `${SOURCE_DATE_EPOCH}`
because this legacy generator ignores the environment value; absence,
duplication or trailing banner payload fails closed. Both raw bytes and the
canonical output are mutation-scanned. The normalization rule is itself part
of the reproduction metadata. The output
comparison captures the semantic effect of repo-local angle includes
and lexer/parser generation without pretending to enumerate that dependency
closure by regex. Unrelated but otherwise valid generator, input, output,
declared-input or replay evidence cannot satisfy the binding;
missing or stale generated-source classifications fail closed.
Pinned regressions cover cupsd configuration/job/log sources, syslogd variable
list and `aslcommon` object-library expansion, securityd output-name resolution,
and newsyslog runtime role. The same pass promoted analogous memberd, tftpd,
trustd, atrun and aslmanager sources to typed writers. Two BUILD_TESTING-only
plist services are exact SHA-bound exceptions to the production-target rule.

The `rg` and Python fallback engines execute the same compiled grammar and
ignore policy. Syscall spellings remain case-sensitive; identifiers inside
their argument expressions can use either case without affecting selection.
A parity fixture compares both engines (including an ignored-file case) and
separately proves both select the real `sshd.c`.
The scan is source-only and deterministic; it never executes Darling or
changes a prefix. Every discovered path must be an owner, exact-file exclusion,
or materialized per-path audit record, and every owner path must lie under the
complete production forest. Negative fixtures cover direct, indirect and
fully computed paths and run through the real `_check_scan_coverage()` path.

The 38 current explicit excluded scan hits are exact regular files; subtree
exclusions are forbidden. They cover disposable workspace/XNU/security
regression fixtures, upstream sample/test servers, caller-owned outputs and the
Darlingserver debug tool. Changing an exclusion path, source content, reason,
or repository is a contract-visible change.

## Explicit exclusions

`rootless_shutdown_lifecycle.py` writes only task-owned trace/evidence roots;
it is not a runtime-prefix writer. E-UNION fixture setup in `test.py`,
`test_guest_macho.py`, `test_guest_c.py`, Darlingserver debug tools, and
Darlingserver tests are likewise excluded from the production inventory. Their
temporary namespace must remain explicitly task-owned and cannot be used as
evidence that a product writer holds the runtime lease.

Installed `syslogd.tproj/asl_action.c` is deliberately not an exclusion: its
service-target membership makes it part of the typed syslogd runtime writer.

## Migration plan

1. Create/open the exact `.lifecycle.lock` under a retained prefix anchor with
   `O_NOFOLLOW`; retain the FD and take `flock(LOCK_EX)` before any observation
   or mutation.
2. Give the Rust controller a transaction/root brand and require it on every
   endpoint, PID, marker, deployment and repair mutation. A compatible entry
   must have `lock.status=exact-exclusive-flock` and `retained_fd=true`.
3. Route startup, Darlingserver, launchd and shellspawn endpoint/PID
   publication through retained fd-relative capabilities. Remove blanket
   `/var/run`/`/var/tmp` cleanup from the unbound product path.
4. Define an explicit E-UNION writer sublease for guest syscalls. The wrapper
   family must not infer compatibility from a pathname or from a caller that
   merely happens to hold a different lock.
5. Keep Python repair/deploy/bootstrap as incompatible orchestration until
   their filesystem authority is removed or delegated to the Rust controller.
6. Re-run the source inventory after every writer cohort. `cohort-ready`
   records remain opt-in; only when all overlapping production records are
   globally compatible may the `.7` default route be enabled.

## Local contract

Run:

```sh
./tests/run-namespace-writer-inventory-contract.sh
```

The contract is source-only and fail-closed. It checks registry shape,
repository/path existence, symbol anchors, finite scan coverage, duplicate
ownership, dynamically discovered scan coverage, compatible-writer lock
requirements, and negative fixtures for an unregistered path and a compatible
writer without an exact exclusive flock. It emits deterministic digests for
the typed owner sources, per-path candidate audit, registry, and contract,
plus candidate/owner/exclusion and installed-service target counts. The
35-case negative suite covers direct,
indirect and fully computed unregistered paths, a false CMake reachability
claim, commented/same-basename non-install evidence, an executable with no
direct mutations but a mutating object-library/target_sources dependency, an
object-graph cycle and both target/source-token budget exhaustion, an untyped
real cupsd target source, a mutating `.cxx` source, embedded source-directory
resolution, unknown CMake variables, unsupported generator expressions,
unbound generated output, a stale generated input, unrelated-but-valid MIG
generator/input/output/declared-input bindings, late suffix reassignment and a
conditional `mig()` invocation, missing/foreign/stale-SHA audit entries, stale exclusion
content, a
forbidden subtree exclusion, a compatible writer without a flock, and a
compatible writer that claims a flock without retaining its FD. It also
rejects stale replay-tool provenance, missing or stale materialized outputs,
forged generated-output mutation evidence, a banner-line mutation payload and
a non-`ctime()` timestamp. The engine parity gate
also compares the `rg` and Python paths against real OpenSSH source. No product
writer, patch, lock, mapping, ref, runtime default, or CI workflow is changed
by this inventory.
