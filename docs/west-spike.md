# West backend validation

West is the selected workspace backend. The `repo` implementation remains
available as a rollback/bootstrap reference until the first integration
profile and branch migration are complete.

## Acceptance criteria

- `west manifest --validate` accepts all Darling projects plus private tools.
- Parent Darling and nested submodule paths can be updated together.
- `west status`, `west diff`, and `west forall` work across that layout.
- `west manifest --freeze` produces a reproducible manifest.
- Workspace tools such as `darling-debug-runner` update from the public
  `darling-next` organization without CI-only credentials.
- Existing handoff bundles can be restored after `west update`.
- A west extension can replace the remaining useful `dw` commands.

## Fetch overrides

`darling/src/external/libressl-2.8.3` is fetched from the public
`darling-next/darling-libressl`: darlinghq's current `v2.8.3` ref no longer
contains Darling's historical base `2a56b36`. The fork preserves that base.
The local `75c81f4` fix is applied by the Homebrew patch profile instead of
being hidden in the manifest revision.

The four third-party pins that were previously routed through unavailable
`darlinghq` mirrors use their canonical public repositories: `mruby/mruby`,
`h2o/neverbleed`, `google/googletest`, and `antirez/linenoise`.

## Local spike

```bash
west init -l ~/work/darling-workspace ~/work/darling-west
cd ~/work/darling-west
west manifest --validate
DARLING_WEST_UPDATE_JOBS=8 ~/work/darling-workspace/ci/west-update-parallel.sh
west status
west manifest --freeze -o west.lock.yml
```

Do not retire `repo` manifests or the old checkout until a full update and one
integration profile have passed.

## Validation result

Completed on June 11, 2026 with west 1.5.0:

- full update succeeded for 163 managed projects;
- parent Darling and recursively nested project paths coexist correctly;
- the public Rust debug runner updates from `darling-next`;
- `west status`, `west forall`, manifest validation, and full freeze pass;
- `west dw summary` and `west dw beads ...` load from the manifest repository;
- the frozen manifest contains exact SHAs for every project.

The remaining adoption gate is a declarative integration profile which
composes clean fix branches and restores the existing private handoff refs.
