# PR draft - top-level: add SDKSettings and MacOSX11 SDK symlink

- **beads:** dar-q95.22
- **repo:** darlinghq/darling
- **current commits:** `75437f43b`, `3c79b03fa`
- **clean PR branch:** not split yet
- **files:**
  `Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/SDKSettings.json`,
  `Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX11.sdk`

## Title
SDK: expose SDKSettings and a versioned MacOSX11.sdk symlink

## Body
Homebrew checks the installed SDK metadata and versioned SDK symlinks. Add
`SDKSettings.json` for the bundled macOS SDK and rename the versioned symlink
from `MacOSX10.13.sdk` to `MacOSX11.sdk` so the visible SDK version matches the
emulated macOS version expectations.

## Tests

- Homebrew SDK detection / `check_broken_sdks`
- source-build smoke with CLT 13.2 in the Homebrew test prefix
