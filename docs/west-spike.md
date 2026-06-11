# West backend validation

West is the selected workspace backend. The `repo` implementation remains
available as a rollback/bootstrap reference until the first integration
profile and branch migration are complete.

## Acceptance criteria

- `west manifest --validate` accepts all Darling projects plus private tools.
- Parent Darling and nested submodule paths can be updated together.
- `west status`, `west diff`, and `west forall` work across that layout.
- `west manifest --freeze` produces a reproducible manifest.
- Private projects such as `darling-debug-runner` update over SSH.
- Existing handoff bundles can be restored after `west update`.
- A west extension can replace the remaining useful `dw` commands.

## Fetch overrides

`darling/src/external/libressl-2.8.3` is fetched from
`IlyaGulya/darling-libressl`: darlinghq's current `v2.8.3` ref no longer
contains Darling's historical base `2a56b36`. The fork preserves that base.
The local `75c81f4` fix is applied by the Homebrew patch profile instead of
being hidden in the manifest revision.

## Local spike

```bash
west init -l ~/work/darling-workspace ~/work/darling-west
cd ~/work/darling-west
west manifest --validate
west update darling darling-src-external-zlib darling-debug-runner
west status
west manifest --freeze -o west.lock.yml
```

Do not retire `repo` manifests or the old checkout until a full update and one
integration profile have passed.

## Validation result

Completed on June 11, 2026 with west 1.5.0:

- full update succeeded for 163 managed projects;
- parent Darling and recursively nested project paths coexist correctly;
- the private Rust debug runner updates over SSH;
- `west status`, `west forall`, manifest validation, and full freeze pass;
- `west dw summary` and `west dw beads ...` load from the manifest repository;
- the frozen manifest contains exact SHAs for every project.

The remaining adoption gate is a declarative integration profile which
composes clean fix branches and restores the existing private handoff refs.
