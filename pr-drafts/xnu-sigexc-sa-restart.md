# PR draft - xnu: honor SA_RESTART for sigexc handlers

- **beads:** dar-q95.20
- **repo:** darlinghq/darling-xnu
- **current commit:** `b8d9ccc`
- **top-level commit:** `7f61970a7`
- **clean PR branch:** not split yet
- **files:** signal/sigexc handling plus Ruby regression

## Title
signal: honor SA_RESTART for sigexc handlers

## Body
Mirror Darwin restart behavior for interrupted syscalls delivered through
Darling's sigexc path. Without this, Ruby thread/signal traffic can observe
unexpected interrupted reads instead of the expected restarted syscall behavior.

## Tests

- `tests/regression/run-ruby-thread-kill-read.sh`
- `tests/regression/ruby_thread_kill_read.rb`
