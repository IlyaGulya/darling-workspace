# PR draft — darlingserver: allow balanced psynch cvwait sequences

- **beads:** dar-q95.1
- **repo:** darlinghq/darlingserver
- **branch:** `fix/psynch-cvwait-balanced` (off `origin/main` `89751e6`)
- **files:** `duct-tape/pthread/kern_synch.c`

## Title
psynch: allow balanced cvwait sequences (is_seqhigher)

## Body
`_psynch_cvwait()` rejected valid cvwait sequences by comparing the condition
sequence against the lock sequence with `is_seqhigher_eq()`. Equal sequence
numbers are legitimate for a balanced `cvwait`/`cvsignal` pair, so a correct
program could trip `__FAILEDUSERTEST__("invalid sequence numbers")` and get
`EINVAL`. Use `is_seqhigher()` so only a strictly-higher (genuinely stale)
sequence is rejected.

The error path now emits a detailed `__FAILEDUSERTEST2__` trace (cv, generation
words, mutex, flags, sequences) to make any real mismatch debuggable.

## Open question for review
Whether to keep the verbose `__FAILEDUSERTEST2__` diagnostic as-is or gate it
behind a debug build. The behavioral fix is the `is_seqhigher_eq` →
`is_seqhigher` change alone.

## Reproduced by
`tests/regression/ruby_spawn_pipe_psynch` (Ruby Mutex/ConditionVariable traffic).
