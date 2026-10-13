# Vendored Apple symbol-surface inputs: which ones the CMake build never passes

**Result: for x86_64 the class is closed. One instance was real and is fixed; nothing
else that could ADD a name to a deployed dylib is left unreferenced.**

This record exists so the sweep is not repeated. It answers one question: the tree
vendors Apple's build inputs (alias lists, export lists, xcconfig flags), Darling's
CMake sometimes never passes them -- so a name Apple implements is simply absent from
the deployed export trie. A guest then stops at load time with `Symbol not found`
even though the tree already implements the symbol.

Measured instance (fixed): `src/external/libpthread/xcodescripts/pthread.aliases`
(`_sched_yield _pthread_yield_np`, plus `_cthread_yield`, `__pthread_is_threaded`) is
consumed by Apple's `pthread.xcconfig:91` via `ALIAS_LDFLAGS = -Wl,-alias_list,...`
and was referenced by no CMake file. `XCResultKit` demanded `_pthread_yield_np` and the
loader stopped. The link now passes the file (`src/external/libpthread/src/CMakeLists.txt`,
`fix/ladder-pthread-alias-list`), and `_pthread_yield_np` sits at the same address as
`_sched_yield` on both slices -- an alias, not a second body.

## Method

For every vendored input under the source checkouts that affects a symbol surface:
read the file, find the Apple build setting that consumes it (xcconfig/xcodeproj line),
then search the CMake build for that file name and for the flag. WIRED means the path
the build actually takes passes it; UNWIRED means it exists but nothing references it.
Classification is mechanical, and the deployed tries are checked only where cheap --
`grep` cannot read Mach-O, so absence claims rest on the source-level argument the
input is the only thing that creates the name.

## Verdict by name kind

| Input | Component / surface | Kind | State |
|---|---|---|---|
| `libpthread/xcodescripts/pthread.aliases` | `libsystem_pthread.dylib` | adds 3 aliases | WIRED (this change) |
| `libc/xcodescripts/alias.list` | `libsystem_c.dylib` | adds 23 aliases | WIRED (`libc/CMakeLists.txt:220`) |
| `libplatform/xcodeconfig/libplatform.aliases` | `libsystem_platform.dylib` | adds 3 aliases | WIRED (`libplatform/CMakeLists.txt:111`) |
| `libdispatch/xcodeconfig/libdispatch.aliases` | `libdispatch.dylib` | adds 11 aliases | WIRED (`libdispatch/CMakeLists.txt:96`) |
| `libm/Exports/libm_Intel.a.alias`, `libmathCommonIntel.alias` | `libsystem_m.dylib` | ~200 `$fenv_access_off` aliases | WIRED (`libm/CMakeLists.txt:177`) |
| `corefoundation/SymbolAliases` | CoreFoundation | ~90 `kCF*` aliases | WIRED (`corefoundation/CMakeLists.txt:189`) |
| `MITKerberosShim/stubs/reexport-stubs.txt` | Heimdal proxy | Kerberos aliases | WIRED (`MITKerberosShim/CMakeLists.txt:20`) |
| `libpthread/xcodescripts/pthread-i386.aliases` | i386 slice of `libsystem_pthread.dylib` | adds 18 aliases | **UNWIRED** |
| `libc/xcodescripts/legacy_alias.list` | i386 slice of `libsystem_c.dylib` | adds 9 aliases | **UNWIRED** |
| `libclosure/Blocks.alias` | `libsystem_blocks.dylib` | adds 6 aliases | **UNWIRED, but adds nothing** -- `data.c:21-26` already defines all six |
| `libm/Exports/libm_static_Intel.a.alias` | `system_m_static` archive | alias set for a static archive | **UNWIRED**, no deployed dylib impact |
| `libm/Exports/libm_arm_vfp.a.alias` | libm arm slice | arm64 long-double aliases | **UNWIRED**, no arm target is built |
| `foundation/reexport_i386.exp` | Foundation i386 slice | reexports | **UNWIRED** (the i386 branch is commented out; x86_64 `reexport_x86_64.exp` is wired) |
| `zlib/libz.exp`, `libutil/libutil.exports`, OpenDirectory/CFOpenDirectory `.exp`, `SmartCardServices/.../PCSC.exp`, `coretls/coretls.exp`, `libstdcxx-*.exp` | those dylibs | **restriction** lists | UNWIRED, and cannot remove a name: Darling links without an export list, so globals stay exported |
| `bzip2`, `libedit`, `libarchive` `unexports` | those dylibs | **hide-only** lists | UNWIRED; omission exports a superset |
| Heimdal, OpenLDAP, SecurityTokend, commonCrypto, corecrypto, libxpc, passwordserver_sasl, Security `.exp-in`, libcxx/libcxxabi, dyld `system_loader`, CoreServices `reexport.exp`, libxml2, libressl, objc4, corefoundation/foundation x86_64 reexports, libstdcxx `weak/notweak` | as named | export/reexport lists | WIRED |

## Deliberately left unwired

- The two i386-only alias lists. No x86_64 effect, no guest demand for any of the 27
  names anywhere in the coverage corpus, Apple's own config gates them to
  `[arch=i386*]`, and Darling builds no arm slice. Wiring them would add surface with
  no consumer and no way to prove it: if a 32-bit guest ever appears, these two files
  are the first place to look.
- `Blocks.alias`: the six names it would alias are already defined.
- Everything in the restriction/hide-only rows: those lists can only remove names.
- Kernel/kext export lists (`xnu/config/*.exports`, IOKit family `.exports`) and
  upstream test-harness `.exp` files: not inputs to any Darling userspace dylib.

## Gap this sweep did not cover: inputs that name a missing IMAGE

The sweep classified inputs that change a **symbol surface**. A rung then found a related
input class it had missed, and the distinction matters: Apple's
`src/external/libsystem/optionallibs` names `system_featureflags` (line 5), i.e. the
sub-library `/usr/lib/system/libsystem_featureflags.dylib`, which Apple's own
`libSystem.B.tbd` carries as a nested sub-library exporting
`__os_feature_enabled_impl` and `__os_feature_enabled_simple_impl`. Darling never
consumes that file, has no source tree for the library, no install name and no
re-export entry -- so the family had **no owner at all** in this tree, which is why the
ladder stopped on a name no component implemented.

**That gap is now swept, and the class is closed for demand.** Apple names ten sub-libraries
in `optionallibs` / `optionallibs_i386` / `libSystem.B.tbd` that this tree neither builds
nor re-exports; measured against the load closure, **only two have any demand, and both are
already absorbed by an owner**:

| sub-library | demand in the closure | state |
|---|---|---|
| `libsystem_containermanager.dylib` | XCTHarness binds ten container-query names eagerly | family answered in `src/sandbox/sandbox.c`; image still not built or re-exported, and no longer needed |
| `libsystem_featureflags.dylib` | XCTAutomationSupport binds `__os_feature_enabled_impl` | answered in `src/external/libplatform/src/os/feature_flags.c` |
| `libsystem_collections.dylib` | **none** | a source tree EXISTS (`src/external/libc/collections/`); Apple builds it, CMake never references it |
| `libsystem_secinit`, `_symptoms`, `_eligibility`, `_darwindirectory`, `_trial`, `_sanitizers`, `_product_info_filter` | none | no source tree, no consumer; `product_info_filter` has no SDK tbd at all |

The verdict on `libsystem_collections` is deliberate: it is the one name here that is both
buildable and undemanded, so wiring it would add an image to every guest process with
nothing measured to gain -- the same reason the i386-only alias lists and
`_MobileGestalt_copy_runnableArchitectures_obj` were left alone. If a guest ever demands an
`_os_map_*`/`_os_set_*` symbol, this is the first place to look.

Method and its limit: the demand column was read from the most recent full coverage dump
(`state/urlresourcevalues-ioblocksize-rung/cover-pre-deploy.json`, 254 images, 248 missing
/ 373 field-image-absent) rather than by re-running the analyzer, and that limit was stated
in the report. It was then re-verified independently here: grepping that same 37 MB dump
for every candidate name and symbol prefix (`libsystem_collections`,
`libsystem_containermanager`, `libsystem_featureflags`, `_os_map_`, `_os_set_`, `_symptom_`,
`_libsecinit_`, `__os_eligibility_`, `_DarwinDirectory`) returns **zero** occurrences of
each, so no closure image links any of them by ordinal or symbol.



Re-run the sweep after adding a component or changing a link: for each vendored input,
compare the Apple setting that consumes it against the CMake path. The load-time
symptom this class produces is a `Symbol not found` for a name the source tree already
implements, so the first question for any new rung is still "does the tree already
have this, and is it merely not exported?".
