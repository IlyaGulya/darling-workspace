#![no_main]

use darling_lifecycle_operation_boundary::fuzz::{
    dispose_replay_roots, safe_replay, MAX_CAMPAIGN_FORENSIC_ROOTS,
};
use libfuzzer_sys::fuzz_target;
use std::sync::atomic::{AtomicUsize, Ordering};

static RETAINED_FORENSIC_ROOTS: AtomicUsize = AtomicUsize::new(0);

fuzz_target!(|data: &[u8]| {
    // safe_replay turns malformed/truncated bytecode and reducer event
    // rejection into typed DECODE_REJECTED/REJECTED reports.  Those are
    // normal fuzz outcomes and must never stop a campaign.  It also converts
    // an unexpected panic into a report so the target can emit one bounded,
    // deterministic libFuzzer artifact below.
    let report = safe_replay(data);
    if report.status == "DECODE_REJECTED" || report.status == "REJECTED" {
        return;
    }

    // Only a fully accepted trace may crash the target for an invariant
    // failure.  In particular, an invalid capability or recovery event is a
    // typed rejection, not evidence that the reducer invariant failed.
    let accepted_invariant_failure = report.status == "INVARIANT_VIOLATION"
        && report.rejected_events == 0;
    let unsafe_recovery = matches!(
        report.recovery_status.as_deref(),
        Some("UNSAFE" | "UNDETECTED")
    );
    let retain_forensic = unsafe_recovery || accepted_invariant_failure;
    if retain_forensic {
        let roots = report.forensic_roots.len();
        let retained = RETAINED_FORENSIC_ROOTS.fetch_add(roots, Ordering::Relaxed) + roots;
        if retained > MAX_CAMPAIGN_FORENSIC_ROOTS {
            panic!(
                "lifecycle fuzz forensic campaign budget exceeded retained={} limit={}",
                retained,
                MAX_CAMPAIGN_FORENSIC_ROOTS
            );
        }
    }
    if let Err(error) = dispose_replay_roots(&report, retain_forensic) {
        panic!("lifecycle fuzz root disposition failed: {error}");
    }
    if accepted_invariant_failure
        || unsafe_recovery
        || report.status == "PANIC"
    {
        panic!(
            "lifecycle fuzz oracle failure status={} seed={} trace={:?} source={} census={} reproduce={} fingerprint={:?} errors={:?}",
            report.status,
            report.seed,
            report.minimized_replay_trace,
            report.source_identity,
            report.resource_census,
            report.reproduction_command,
            report.failure_fingerprint,
            report.errors
        );
    }
});
