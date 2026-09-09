# Diagnostic patches

These patches are preserved investigation artifacts, not integration-profile
inputs and not upstream PR candidates.

For diagnostics, configure a local context once from the manifest
directory with `mise run dw dev context homebrew --prefix /absolute/path/to/new-prefix`,
then use `mise run dw dev run homebrew-prepare`, `homebrew-preflight`, or
`exact-capture` as appropriate. Reconnect or cancel with the commands printed by
the run; streams and Homebrew logs register automatically. See
[test-infra.md](../docs/test-infra.md) for managed advanced West jobs, exact
archive interpretation, and separate Homebrew source-build acceptance criteria.
Use lifecycle-owned cleanup and deployment. Do not infer a hang from quiet logs
or manually overwrite a running prefix's binaries.

## darlingserver-kwq-call-tracing.patch

- Repository: `darling/src/external/darlingserver`
- Source branch: `fix/homebrew-psynch-ruby-hang`
- Base commit: `32af4d56ddcdfa98c932d7b1dfb98c758663b7f0`
- SHA256: `f7f972989b720e9a0ac300db35859e413b871ed5fe08a59c724eb6c2bf9d14aa`
- Purpose: verbose `KWQDBG` mutex queue tracing and `CALLDBG` RPC receipt logging.
- Disposition: archive only; never include in a production patch profile or PR.
