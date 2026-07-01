# E-UNION bootstrap trace — current tip (xnu 460b4da), 2026-06-20

Re-ran trace-bootstrap.sh in DEFAULT unprivileged docker (ubuntu:24.04, no caps/
privileged/userns) against the current-tip EUNION build (libsystem_kernel.dylib
rebuilt from 460b4da and swapped into eunion-stage).

## RESULT: BOOTSTRAP STALL IS GONE. Guest boots end-to-end.

boot.stdout contains: HELLO_EUNION

Daemon bring-up (execve chain) — ALL of these were ABSENT in the old (pre-35025e9)
trace, which died at launchctl with zero daemons:
  - opendirectoryd, memberd, securityd, iokitd  (system daemons forked)
  - shellspawn        (the daemon whose absence WAS the bug)
  - bash --login -c 'echo HELLO_EUNION'   (the user command actually ran)
  - cp User Template, path_helper          (login setup)

Timeline proof:
  14:15:25.802  launchctl connect() -> launchd/sock  = 0   (SUBMITJOB IPC works)
  14:15:27.65   shellspawn execs
  14:15:30.32   bash --login -c echo HELLO_EUNION execs
  14:15:33.22   bash exits 0
  14:15:34.90   launcher exit_group(0)                       (clean success)

Everything after 14:15:34.9 (iokitd SIGABRTs, launchctl's 60s BootCache oneshot
timer expiring at 14:16:25, SIGILL/SIGTERM) is POST-SUCCESS TEARDOWN: the launcher
already exited 0; daemons still mid-RPC during darlingserver shutdown get
ECONNREFUSED (-111) and abort. Cosmetic shutdown race, not a boot failure.

## Why the "wall" was stale
The epic's recorded bootstrap-stall wall was captured with a build from BEFORE
35025e9 (Jun16 02:49). Both keystone fixes are in HEAD (460b4da):
  - 35025e9  detranslate lower-template fds/paths to guest paths (no EXIT_PATH escape)
  - 4ebed05  sys_fstatfs64 tolerate missing /proc/self/mounts (opendir LaunchDaemons)
Nobody had re-run the live boot since. This trace closes the question.
