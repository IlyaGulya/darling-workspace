# PR draft - perl: disable unavailable _NSGetExecutablePath

- **beads:** dar-q95.5
- **repo:** darlinghq/darling-perl
- **branch:** `fix/disable-nsgetexecutablepath`

## Title
perl: disable unavailable _NSGetExecutablePath

## Body
Darling's Perl configurations advertise `_NSGetExecutablePath`, but the symbol
is not available to these builds. Perl then selects an executable-path lookup
that cannot be linked or used reliably.

Mark `USE_NSGETEXECUTABLEPATH` and `usensgetexecutablepath` as unavailable in
the checked-in Perl 5.18 and 5.28 configurations, including their matching
`DSTROOT` copies. This keeps the generated and installed configuration data
consistent and makes Perl use its existing fallback path resolution.

The change is configuration-only and intentionally does not include the
investigation-time tracing or unrelated Perl workarounds.
