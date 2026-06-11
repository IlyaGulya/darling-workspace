# PR draft - LibreSSL 2.8.3: disable strict aliasing for NIST EC reduction

- **beads:** dar-q95.6
- **repo:** darlinghq/darling-libressl
- **branch:** `fix/libressl-283-nist-strict-aliasing`

## Title
Build LibreSSL 2.8.3 crypto with strict aliasing disabled

## Body
LibreSSL 2.8.3's NIST P-256 and P-384 reduction code accesses `BN_ULONG`
storage through narrower word views. Under strict-aliasing optimization this
can miscompile the reduction and reject valid public keys as points not on the
curve.

Build the affected crypto target with `-fno-strict-aliasing`. This restores
correct P-256/P-384 SPKI decoding under Darling without changing the
cryptographic implementation.

The fix was validated against the Homebrew TLS path that originally failed
while decoding valid NIST EC certificates.
