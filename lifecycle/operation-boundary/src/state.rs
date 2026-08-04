//! Rust lifecycle reducer used by the operation boundary.
//!
//! The JSON/Python model in `.1` remains a differential reference oracle. This
//! reducer is the authority consumed by the future explorer: it computes
//! ownership, journal transitions, recovery targets, and terminal obligations
//! rather than accepting recorded state as evidence.

use std::collections::{BTreeMap, BTreeSet};

use crate::{BoundaryError, Result};

pub const MODEL_MAX_EVENTS: usize = 128;
pub const MODEL_MAX_VIRTUAL_TIME_NS: u64 = 1_000_000;
pub const MODEL_MAX_LIVE_CAPABILITIES: usize = 64;
pub const MODEL_MAX_RECOVERY_STEPS: usize = 16;

pub const INVARIANT_REGISTRY: &[&str] = &[
    "no-raw-path-authority",
    "no-unvalidated-fd",
    "stable-journal-independent",
    "total-recovery-matrix",
    "identity-before-signal",
    "no-children-after-gone",
    "shared-lease-bound",
    "pidfd-identity-before-signal",
    "late-fork-closes-snapshot",
    "bounded-replay",
    "terminal-trace",
];

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum StableState {
    Uninitialized,
    Ready,
    Running,
    Draining,
    Stopped,
    Corrupt,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum JournalPhase {
    None,
    Prepare,
    Publish,
    Cleanup,
    Commit,
    Abort,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum IntentKind {
    None,
    CreatePrefix,
    StartSession,
    RequestShutdown,
    ReleaseSession,
    RecreatePrefix,
    Recover,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CapabilityKind {
    PrefixDirectory,
    SessionRootPidfd,
    SessionMemberPidfd,
    SharedSessionLease,
    RuntimeEndpoint,
    JournalRecord,
}

impl CapabilityKind {
    fn is_pidfd(self) -> bool {
        matches!(self, Self::SessionRootPidfd | Self::SessionMemberPidfd)
    }

    fn is_identity(self) -> bool {
        self.is_pidfd() || matches!(self, Self::SharedSessionLease)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RecoveryAction {
    Initialize,
    Noop,
    RollbackUninitialized,
    RollbackReady,
    RollbackRunning,
    RollbackStopped,
    CompletePublish,
    CompleteCleanup,
    DrainToReady,
    ContinueDrain,
    FailClosed,
    Quarantine,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Outcome {
    Success,
    FailClosed,
    Timeout,
    Recovered,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Event {
    Acquire {
        id: String,
        kind: CapabilityKind,
        generation: u64,
        owner: String,
    },
    Move {
        id: String,
        from: String,
        to: String,
    },
    Release {
        id: String,
        owner: String,
    },
    Intent {
        intent: IntentKind,
        transaction: String,
    },
    BarrierEntered,
    Membership {
        members: Vec<String>,
        completeness: String,
    },
    Identity {
        id: String,
        matches: bool,
    },
    Signal {
        id: String,
    },
    Endpoint {
        endpoint: String,
        operation: String,
        result: String,
    },
    MemberObserved {
        id: String,
        origin: String,
    },
    Fault {
        name: String,
    },
    Recovery(RecoveryAction),
    Terminal(Outcome),
}

#[derive(Clone, Debug)]
struct CatalogEntry {
    kind: CapabilityKind,
    generation: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StateSnapshot {
    pub stable: StableState,
    pub journal: JournalPhase,
    pub intent: IntentKind,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RecoveryObservation {
    pub stable: StableState,
    pub journal: JournalPhase,
    pub action: RecoveryAction,
}

#[derive(Clone, Debug)]
pub struct Reducer {
    snapshot: StateSnapshot,
    catalog: BTreeMap<String, CatalogEntry>,
    live: BTreeMap<String, String>,
    matched: BTreeSet<String>,
    gone: BTreeSet<String>,
    consumed: BTreeSet<(String, u64)>,
    checkpoint_live: BTreeMap<String, String>,
    checkpoint_matched: BTreeSet<String>,
    checkpoint_gone: BTreeSet<String>,
    transaction: Option<String>,
    obligations: BTreeSet<String>,
    membership_closed: bool,
    terminal: Option<Outcome>,
    observations: Vec<RecoveryObservation>,
    membership_seen_after_gone: bool,
    signal_without_identity: bool,
    late_fork_seen: bool,
    late_fork_after_closed: bool,
    late_fork_fault_seen: bool,
}

impl Reducer {
    pub fn new(stable: StableState) -> Self {
        Self {
            snapshot: StateSnapshot {
                stable,
                journal: JournalPhase::None,
                intent: IntentKind::None,
            },
            catalog: BTreeMap::new(),
            live: BTreeMap::new(),
            matched: BTreeSet::new(),
            gone: BTreeSet::new(),
            consumed: BTreeSet::new(),
            checkpoint_live: BTreeMap::new(),
            checkpoint_matched: BTreeSet::new(),
            checkpoint_gone: BTreeSet::new(),
            transaction: None,
            obligations: BTreeSet::new(),
            membership_closed: false,
            terminal: None,
            observations: Vec::new(),
            membership_seen_after_gone: false,
            signal_without_identity: false,
            late_fork_after_closed: false,
            late_fork_seen: false,
            late_fork_fault_seen: false,
        }
    }

    pub fn seed_ownership(&mut self, id: &str, owner: &str) -> Result<()> {
        let generation = self
            .catalog
            .get(id)
            .ok_or(BoundaryError::WrongCapability("unknown seeded capability"))?
            .generation;
        if self.live.contains_key(id) {
            return Err(BoundaryError::WrongCapability(
                "duplicate seeded capability",
            ));
        }
        self.consumed.insert((id.to_string(), generation));
        self.live.insert(id.to_string(), owner.to_string());
        Ok(())
    }

    pub fn register_capability(
        &mut self,
        id: impl Into<String>,
        kind: CapabilityKind,
        generation: u64,
    ) -> Result<()> {
        let id = id.into();
        if !id.starts_with("cap.") || generation == 0 || self.catalog.contains_key(&id) {
            return Err(BoundaryError::WrongCapability(
                "duplicate or invalid capability catalog entry",
            ));
        }
        self.catalog.insert(id, CatalogEntry { kind, generation });
        Ok(())
    }

    pub fn apply(&mut self, event: Event) -> Result<()> {
        // A rejected event is observationally atomic.  The reducer is an
        // oracle, so a malformed transition must not leave partially applied
        // ownership, journal, or obligation state behind for a later event.
        let before = self.clone();
        match self.apply_inner(event) {
            Ok(()) => Ok(()),
            Err(error) => {
                *self = before;
                Err(error)
            }
        }
    }

    fn apply_inner(&mut self, event: Event) -> Result<()> {
        if self.terminal.is_some() {
            return Err(BoundaryError::WrongCapability("event after terminal"));
        }
        let before = self.snapshot.clone();
        match event {
            Event::Acquire {
                id,
                kind,
                generation,
                owner,
            } => self.acquire(&id, kind, generation, &owner)?,
            Event::Move { id, from, to } => {
                if self.live.get(&id) != Some(&from) {
                    return Err(BoundaryError::WrongCapability("move ownership"));
                }
                self.live.insert(id, to);
            }
            Event::Release { id, owner } => {
                if self.live.get(&id) != Some(&owner) {
                    return Err(BoundaryError::WrongCapability("release ownership"));
                }
                self.live.remove(&id);
                self.matched.remove(&id);
            }
            Event::Intent {
                intent,
                transaction,
            } => {
                if self.transaction.is_some() {
                    return Err(BoundaryError::WrongCapability(
                        "nested lifecycle transaction",
                    ));
                }
                self.transaction = Some(transaction);
                self.checkpoint_live = self.live.clone();
                self.checkpoint_matched = self.matched.clone();
                self.checkpoint_gone = self.gone.clone();
                self.snapshot.journal = JournalPhase::Prepare;
                self.snapshot.intent = intent;
            }
            Event::BarrierEntered => {
                self.snapshot.stable = StableState::Draining;
            }
            Event::Membership {
                members,
                completeness,
            } => {
                if !self.gone.is_empty() {
                    self.membership_seen_after_gone = true;
                }
                if !self.gone.is_empty() {
                    return Err(BoundaryError::WrongCapability(
                        "membership after gone identity",
                    ));
                }
                for member in members {
                    let entry = self
                        .catalog
                        .get(&member)
                        .ok_or(BoundaryError::WrongCapability("unknown member"))?;
                    if !entry.kind.is_pidfd() || !self.live.contains_key(&member) {
                        return Err(BoundaryError::WrongCapability(
                            "membership requires live pidfd",
                        ));
                    }
                }
                if completeness == "CLOSED" {
                    self.membership_closed = true;
                }
                if matches!(completeness.as_str(), "TIMEOUT" | "OVERFLOW") {
                    self.obligations.insert("membership-incomplete".to_string());
                } else {
                    self.obligations.insert("membership-closed".to_string());
                }
            }
            Event::Identity { id, matches } => {
                let entry = self
                    .catalog
                    .get(&id)
                    .ok_or(BoundaryError::WrongCapability("unknown identity"))?;
                if !entry.kind.is_identity() || !self.live.contains_key(&id) {
                    return Err(BoundaryError::WrongCapability(
                        "identity requires live capability",
                    ));
                }
                if !matches {
                    self.live.remove(&id);
                    self.matched.remove(&id);
                    self.gone.insert(id);
                    self.obligations.insert("identity-failure".to_string());
                } else {
                    self.matched.insert(id);
                }
            }
            Event::Signal { id } => {
                let entry = self
                    .catalog
                    .get(&id)
                    .ok_or(BoundaryError::WrongCapability("unknown signal capability"))?;
                if !entry.kind.is_pidfd()
                    || !self.live.contains_key(&id)
                    || !self.matched.contains(&id)
                {
                    self.signal_without_identity = true;
                    return Err(BoundaryError::WrongCapability(
                        "signal requires matching pidfd",
                    ));
                }
                self.snapshot.journal = JournalPhase::Cleanup;
            }
            Event::Endpoint { .. } => {}
            Event::MemberObserved { id, origin } => {
                let entry = self
                    .catalog
                    .get(&id)
                    .ok_or(BoundaryError::WrongCapability("unknown observed member"))?;
                if !entry.kind.is_pidfd() {
                    return Err(BoundaryError::WrongCapability(
                        "member observation requires pidfd",
                    ));
                }
                if origin == "LATE_FORK" {
                    self.late_fork_seen = true;
                    self.late_fork_after_closed = self.membership_closed;
                    self.obligations.insert("late-fork-recovery".to_string());
                }
            }
            Event::Fault { name } => {
                self.obligations.insert(format!("fault:{name}"));
                if name == "LATE_FORK" {
                    self.late_fork_fault_seen = true;
                }
            }
            Event::Recovery(action) => self.recover(action)?,
            Event::Terminal(outcome) => {
                if outcome == Outcome::Success && self.unresolved() {
                    return Err(BoundaryError::WrongCapability(
                        "successful terminal has unresolved obligations",
                    ));
                }
                self.terminal = Some(outcome);
            }
        }
        if !stable_transition_allowed(before.stable, self.snapshot.stable)
            || !journal_transition_allowed(before.journal, self.snapshot.journal)
        {
            return Err(BoundaryError::WrongCapability(
                "illegal lifecycle transition",
            ));
        }
        Ok(())
    }

    fn acquire(
        &mut self,
        id: &str,
        kind: CapabilityKind,
        generation: u64,
        owner: &str,
    ) -> Result<()> {
        let entry = self
            .catalog
            .get(id)
            .ok_or(BoundaryError::WrongCapability("unknown capability"))?;
        if entry.kind != kind
            || entry.generation != generation
            || self.live.contains_key(id)
            || !self.consumed.insert((id.to_string(), entry.generation))
        {
            return Err(BoundaryError::WrongCapability(
                "double or reused capability generation",
            ));
        }
        self.live.insert(id.to_string(), owner.to_string());
        Ok(())
    }

    fn recover(&mut self, action: RecoveryAction) -> Result<()> {
        let expected = recovery_action(self.snapshot.stable, self.snapshot.journal);
        if action != expected {
            return Err(BoundaryError::WrongCapability(
                "recovery action does not cover state/phase",
            ));
        }
        let before = self.snapshot.clone();
        self.snapshot = recovery_target(action, before.clone())?;
        self.observations.push(RecoveryObservation {
            stable: before.stable,
            journal: before.journal,
            action,
        });
        if matches!(
            action,
            RecoveryAction::RollbackUninitialized
                | RecoveryAction::RollbackReady
                | RecoveryAction::RollbackRunning
                | RecoveryAction::RollbackStopped
                | RecoveryAction::FailClosed
                | RecoveryAction::Quarantine
        ) {
            self.live = self.checkpoint_live.clone();
            self.matched = self.checkpoint_matched.clone();
            self.gone = self.checkpoint_gone.clone();
            self.transaction = None;
            self.obligations.retain(|item| {
                !item.starts_with("fault:")
                    && item != "identity-failure"
                    && item != "late-fork-recovery"
            });
        } else if matches!(
            action,
            RecoveryAction::Initialize
                | RecoveryAction::CompletePublish
                | RecoveryAction::CompleteCleanup
                | RecoveryAction::DrainToReady
        ) {
            self.transaction = None;
        }
        self.obligations.retain(|item| {
            !item.starts_with("fault:")
                && item != "identity-failure"
                && item != "late-fork-recovery"
        });
        Ok(())
    }

    pub fn finish(&self) -> Result<Outcome> {
        self.terminal
            .ok_or(BoundaryError::WrongCapability("trace has no terminal"))
    }

    fn unresolved(&self) -> bool {
        self.obligations.iter().any(|item| {
            item.starts_with("fault:")
                || matches!(
                    item.as_str(),
                    "identity-failure" | "late-fork-recovery" | "membership-incomplete"
                )
        })
    }

    pub fn snapshot(&self) -> &StateSnapshot {
        &self.snapshot
    }
    pub fn live(&self) -> &BTreeMap<String, String> {
        &self.live
    }
    pub fn obligations(&self) -> &BTreeSet<String> {
        &self.obligations
    }
    pub fn observations(&self) -> &[RecoveryObservation] {
        &self.observations
    }

    pub fn terminal(&self) -> Option<Outcome> {
        self.terminal
    }

    pub fn invariant_names(
        &self,
        event_count: usize,
        final_time_ns: u64,
        max_events: usize,
        max_virtual_time_ns: u64,
        max_live_capabilities: usize,
        max_recovery_steps: usize,
    ) -> BTreeSet<String> {
        let mut satisfied = BTreeSet::from([
            "no-raw-path-authority".to_string(),
            "no-unvalidated-fd".to_string(),
            "stable-journal-independent".to_string(),
            "total-recovery-matrix".to_string(),
            "identity-before-signal".to_string(),
            "no-children-after-gone".to_string(),
            "shared-lease-bound".to_string(),
            "pidfd-identity-before-signal".to_string(),
            "late-fork-closes-snapshot".to_string(),
            "bounded-replay".to_string(),
            "terminal-trace".to_string(),
        ]);
        if self.signal_without_identity {
            satisfied.remove("no-unvalidated-fd");
            satisfied.remove("identity-before-signal");
            satisfied.remove("pidfd-identity-before-signal");
        }
        if self.membership_seen_after_gone {
            satisfied.remove("no-children-after-gone");
        }
        if self.terminal == Some(Outcome::Success)
            && self.live.iter().any(|(id, _)| {
                self.catalog
                    .get(id)
                    .is_some_and(|entry| entry.kind == CapabilityKind::SharedSessionLease)
            })
        {
            satisfied.remove("shared-lease-bound");
        }
        let late_fork_ok = !self.late_fork_seen
            || (self.late_fork_after_closed
                && self.late_fork_fault_seen
                && !self.obligations.contains("late-fork-recovery")
                && !self.observations.is_empty());
        if !late_fork_ok {
            satisfied.remove("late-fork-closes-snapshot");
        }
        if event_count > max_events
            || final_time_ns > max_virtual_time_ns
            || self.live.len() > max_live_capabilities
            || self.observations.len() > max_recovery_steps
        {
            satisfied.remove("bounded-replay");
        }
        satisfied
    }
}

fn stable_transition_allowed(from: StableState, to: StableState) -> bool {
    match from {
        StableState::Uninitialized => matches!(
            to,
            StableState::Uninitialized | StableState::Ready | StableState::Corrupt
        ),
        StableState::Ready => matches!(
            to,
            StableState::Ready | StableState::Running | StableState::Stopped | StableState::Corrupt
        ),
        StableState::Running => matches!(
            to,
            StableState::Running
                | StableState::Draining
                | StableState::Ready
                | StableState::Stopped
                | StableState::Corrupt
        ),
        StableState::Draining => matches!(
            to,
            StableState::Draining
                | StableState::Running
                | StableState::Ready
                | StableState::Stopped
                | StableState::Corrupt
        ),
        StableState::Stopped => matches!(
            to,
            StableState::Stopped | StableState::Ready | StableState::Corrupt
        ),
        StableState::Corrupt => to == StableState::Corrupt,
    }
}

fn journal_transition_allowed(from: JournalPhase, to: JournalPhase) -> bool {
    match from {
        JournalPhase::None => matches!(
            to,
            JournalPhase::None | JournalPhase::Prepare | JournalPhase::Commit | JournalPhase::Abort
        ),
        JournalPhase::Prepare => matches!(
            to,
            JournalPhase::Prepare
                | JournalPhase::Publish
                | JournalPhase::Cleanup
                | JournalPhase::Abort
        ),
        JournalPhase::Publish => matches!(
            to,
            JournalPhase::Publish | JournalPhase::Commit | JournalPhase::Abort
        ),
        JournalPhase::Cleanup => matches!(
            to,
            JournalPhase::Cleanup | JournalPhase::Commit | JournalPhase::Abort
        ),
        JournalPhase::Commit => matches!(
            to,
            JournalPhase::Commit | JournalPhase::None | JournalPhase::Abort | JournalPhase::Prepare
        ),
        JournalPhase::Abort => matches!(
            to,
            JournalPhase::Abort | JournalPhase::None | JournalPhase::Prepare
        ),
    }
}

pub fn recovery_action(stable: StableState, journal: JournalPhase) -> RecoveryAction {
    use JournalPhase::*;
    use RecoveryAction::*;
    use StableState::*;
    match (stable, journal) {
        (Uninitialized, None) => Initialize,
        (Uninitialized, Prepare) => RollbackUninitialized,
        (Uninitialized, Publish) => RollbackUninitialized,
        (Uninitialized, Cleanup) => RollbackUninitialized,
        (Uninitialized, Commit) => FailClosed,
        (Uninitialized, Abort) => RollbackUninitialized,
        (Ready, None) => Noop,
        (Ready, Prepare) => RollbackReady,
        (Ready, Publish) => CompletePublish,
        (Ready, Cleanup) => CompleteCleanup,
        (Ready, Commit) => Noop,
        (Ready, Abort) => RollbackReady,
        (Running, None) => Noop,
        (Running, Prepare) => RollbackRunning,
        (Running, Publish) => CompletePublish,
        (Running, Cleanup) => DrainToReady,
        (Running, Commit) => Noop,
        (Running, Abort) => RollbackRunning,
        (Draining, None) => ContinueDrain,
        (Draining, Prepare) => RollbackRunning,
        (Draining, Publish) => CompletePublish,
        (Draining, Cleanup) => ContinueDrain,
        (Draining, Commit) => DrainToReady,
        (Draining, Abort) => RollbackRunning,
        (Stopped, None) => Noop,
        (Stopped, Prepare) => RollbackStopped,
        (Stopped, Publish) => CompletePublish,
        (Stopped, Cleanup) => CompleteCleanup,
        (Stopped, Commit) => Noop,
        (Stopped, Abort) => RollbackStopped,
        (Corrupt, Abort) => Quarantine,
        (Corrupt, _) => FailClosed,
    }
}

fn recovery_target(action: RecoveryAction, from: StateSnapshot) -> Result<StateSnapshot> {
    use IntentKind::*;
    use JournalPhase::*;
    use RecoveryAction::*;
    use StableState::*;
    let target = match action {
        Noop | ContinueDrain => from.clone(),
        Initialize | CompletePublish => StateSnapshot {
            stable: Ready,
            journal: Commit,
            intent: CreatePrefix,
        },
        RollbackUninitialized => StateSnapshot {
            stable: Uninitialized,
            journal: Abort,
            intent: Recover,
        },
        RollbackReady => StateSnapshot {
            stable: Ready,
            journal: Abort,
            intent: Recover,
        },
        RollbackRunning => StateSnapshot {
            stable: Running,
            journal: Abort,
            intent: Recover,
        },
        RollbackStopped => StateSnapshot {
            stable: Stopped,
            journal: Abort,
            intent: Recover,
        },
        CompleteCleanup | DrainToReady => StateSnapshot {
            stable: if action == CompleteCleanup {
                Stopped
            } else {
                Ready
            },
            journal: Commit,
            intent: ReleaseSession,
        },
        FailClosed | Quarantine => StateSnapshot {
            stable: Corrupt,
            journal: Abort,
            intent: Recover,
        },
    };
    if stable_transition_allowed(from.stable, target.stable)
        && journal_transition_allowed(from.journal, target.journal)
    {
        Ok(target)
    } else {
        Err(BoundaryError::WrongCapability(
            "unreachable recovery target",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn root_reducer() -> Reducer {
        let mut reducer = Reducer::new(StableState::Running);
        reducer
            .register_capability("cap.root", CapabilityKind::SessionRootPidfd, 1)
            .unwrap();
        reducer
            .register_capability("cap.lease", CapabilityKind::SharedSessionLease, 1)
            .unwrap();
        reducer
    }

    #[test]
    fn recovery_matrix_is_total() {
        let states = [
            StableState::Uninitialized,
            StableState::Ready,
            StableState::Running,
            StableState::Draining,
            StableState::Stopped,
            StableState::Corrupt,
        ];
        let phases = [
            JournalPhase::None,
            JournalPhase::Prepare,
            JournalPhase::Publish,
            JournalPhase::Cleanup,
            JournalPhase::Commit,
            JournalPhase::Abort,
        ];
        assert_eq!(states.len() * phases.len(), 36);
        for state in states {
            for phase in phases {
                let action = recovery_action(state, phase);
                let snapshot = StateSnapshot {
                    stable: state,
                    journal: phase,
                    intent: IntentKind::None,
                };
                assert!(
                    recovery_target(action, snapshot).is_ok(),
                    "unreachable recovery target for {state:?}/{phase:?}"
                );
            }
        }
    }

    #[test]
    fn capability_generations_and_identity_are_authoritative() {
        let mut reducer = root_reducer();
        reducer
            .apply(Event::Acquire {
                id: "cap.root".to_string(),
                kind: CapabilityKind::SessionRootPidfd,
                generation: 1,
                owner: "controller".to_string(),
            })
            .unwrap();
        assert!(reducer
            .apply(Event::Acquire {
                id: "cap.root".to_string(),
                kind: CapabilityKind::SessionRootPidfd,
                generation: 1,
                owner: "controller".to_string()
            })
            .is_err());
        reducer
            .apply(Event::Intent {
                intent: IntentKind::RequestShutdown,
                transaction: "txn".to_string(),
            })
            .unwrap();
        reducer
            .apply(Event::Identity {
                id: "cap.root".to_string(),
                matches: false,
            })
            .unwrap();
        assert!(reducer.apply(Event::Terminal(Outcome::Success)).is_err());
        reducer
            .apply(Event::Recovery(RecoveryAction::RollbackRunning))
            .unwrap();
        assert!(reducer.live().contains_key("cap.root"));
        assert!(reducer.obligations().is_empty());
        reducer
            .apply(Event::Intent {
                intent: IntentKind::RequestShutdown,
                transaction: "txn-2".to_string(),
            })
            .unwrap();
    }

    #[test]
    fn shared_lease_cannot_signal_and_terminal_is_unique() {
        let mut reducer = root_reducer();
        reducer
            .apply(Event::Acquire {
                id: "cap.lease".to_string(),
                kind: CapabilityKind::SharedSessionLease,
                generation: 1,
                owner: "controller".to_string(),
            })
            .unwrap();
        assert!(reducer
            .apply(Event::Identity {
                id: "cap.lease".to_string(),
                matches: true
            })
            .is_ok());
        assert!(reducer
            .apply(Event::Signal {
                id: "cap.lease".to_string()
            })
            .is_err());
        reducer.apply(Event::Terminal(Outcome::FailClosed)).unwrap();
        assert!(reducer.apply(Event::Terminal(Outcome::FailClosed)).is_err());
    }

    #[test]
    fn rejected_events_and_duplicate_catalog_registration_are_atomic() {
        let mut reducer = Reducer::new(StableState::Ready);
        reducer
            .register_capability("cap.root", CapabilityKind::SessionRootPidfd, 1)
            .unwrap();
        assert!(reducer
            .register_capability("cap.root", CapabilityKind::SessionRootPidfd, 2)
            .is_err());
        reducer
            .apply(Event::Acquire {
                id: "cap.root".to_string(),
                kind: CapabilityKind::SessionRootPidfd,
                generation: 1,
                owner: "controller".to_string(),
            })
            .unwrap();
        let before = reducer.snapshot().clone();
        assert!(reducer.apply(Event::BarrierEntered).is_err());
        assert_eq!(&before, reducer.snapshot());
    }
}
