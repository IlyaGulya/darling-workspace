# XNU disabled ownership stubs

## Summary

Make disabled `chown`, `lchown`, and `fchownat` operations report their real
unsupported status instead of returning success. Invalid path pointers return
`EFAULT` before any disabled fallback or path expansion is reached.

## Validation

- Host RED/GREEN contract covers all disabled ownership entry points.
- Guest fixture calls the public APIs through the deployed `libsystem_kernel`.
- The change is kept in the local Darling XNU fork until the rootless guest
  regression is stable and the patch is reviewed for upstreaming.
