# Superseded prototype migration map

| Superseded prototype responsibility | Rust controller v1 | Python after migration |
| --- | --- | --- |
| `test_prefix.py` prefix/marker acquisition | `PrefixCapability`, `MarkerCapability`, `Prepared → Acquired` | pass inherited descriptors only |
| Python marker content/mode/owner checks | `MarkerObservation` and `MarkerCapability::revalidate` | none |
| Python `/proc`, starttime, pidfd, child census | `PidFdCapability`, `MembershipSnapshot`, `MembershipBound` | none |
| Python pre-shutdown membership comparison | `MembershipBound::verify_before_shutdown` | none |
| Python launcher call and return-code inference | `SignalExecutor` + typed `SignalEvidence` | transport/orchestration only |
| Python path-based finalize/socket cleanup | `EndpointCleanupExecutor` and `Quiescent → Cleaned` | none |
| Python trace/reducer/obligation decisions | `JournalEvent`, `RecoveryObligation`, `ControllerResponse` | serialize/return response |
| Python closure/provenance envelope | Rust request/response handshake plus binary closure | pass bounded request |

Reusable: Rust reducer semantics, typed recovery policy, transport limits,
consumer domain names, source-closure handshake, and existing deterministic
RED evidence.  Deleted from authority: Python syscalls, process identity
classification, synthetic signal events, path cleanup, and duplicate reducer
logic.  Product routing remains deliberately deferred.
