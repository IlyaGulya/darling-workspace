# XNU disabled ownership stubs

## Summary

Make disabled `chown`, `lchown`, and `fchownat` operations report their real
unsupported status instead of returning success. Invalid path pointers return
`EFAULT` before any disabled fallback or path expansion is reached.

## Validation

- The current host RED/GREEN gate executes the production syscall closure and
  preserves the NULL-path `EFAULT` regression.
- `dar-nmda` supersedes unsupported ownership with real Linux operations after
  E-UNION copy-up; its host and source-driven guest fixtures own current behavior.
- The old ENOTSUP guest source, Mach-O binary, and hosted receipts remain
  byte-identical historical corpus artifacts, excluded from active runtime
  selection. They are not evidence for the new ownership implementation.
- Publication remains blocked pending current-stack review.
