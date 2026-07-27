# Arch v6 publication readiness

The historical v5 authoring object closure was unavailable after disposable
authoring cleanup.  Exact recovery was attempted only with preserved mbox
content, base/tree/message/author metadata, and documented committer-date
rules.  It reproduced the declared trees but not the v5 source object IDs;
those sources are `SUPERSEDED_UNPUBLISHED`.

The proposed v6 rule is command-scoped: committer `Darling Patch Stack v6
<patch-stack-v6@example.invalid>`, with committer date equal to the preserved
author date.  Author identity, author date, subject, ordered content, and
expected tree are retained.  The local review package is
the preserved offline publication-evidence bundle; it contains independent bare closure
copies, SHA-256 inventories, dual clean-ODB results, and the v5-to-v6 table.

Authoring-retirement gate: a v6 authoring ODB must remain present until
`v6_publication_closure_contract.py` has verified every proposed lock against
that ODB and at least two distinct clean bare closure copies.  The contract
requires every base, ordered commit, source object, and declared local
base/source ref in each copy; a missing source is a hard failure.  This is the
pre-cleanup handoff proof, not a best-effort recovery procedure.

The arch mapping uses one series per patch.  The CI-host-regression entry is a
profile-topology correction: it uses the reviewed three-commit integration
chain ending at `30835a...`, not historical intermediate `b41159e...`, so the
following Darling integration has its proven base.  This correction has its
own v6 lock and must not be described as a tree-identical rewrite of the old
historical lock.

The reviewed immutable tags and scoped tag protections are now hosted. The
v6 lock URLs name those verified immutable locations; no local closure URL is
part of a production lock or manifest.

This file remains the immutable v6 audit snapshot.  Perf v7 now supplies the
actual XNU prerequisite integration tip `92cc4fe7…`, so the three XNU arch
entries are separately reissued as v7 profile-integration locks.  The
Darlingserver and Darling v6 arch boundaries remain the actual final perf
integration tips.  See `perf-v7-profile-integration-readiness.md`; neither
the v6 package nor its closure copies are modified by that follow-up.
