//! Rust-owned consumer for the quarantine handoff emitted by the lifecycle
//! controller.
//!
//! The consumer is deliberately separate from the endpoint mutation backend:
//! the backend may move an endpoint into private quarantine, but only a caller
//! which has independently stopped every namespace writer may grant the
//! authority used here to delete it.  The authority constructor is private;
//! the only boundary-visible handoff is the consuming
//! [`QuarantinePending::into_parts`] API.  Python and product routing do not
//! participate in this operation.

#![allow(dead_code)]
#![allow(clippy::result_large_err)]

use crate::controller::{
    Cleaned, JournalEvent, QuarantineObligation, QuarantinePending, RecoveryObligation, ScopeId,
};
use crate::FileIdentity;
use std::ffi::CString;
use std::os::fd::{AsRawFd, OwnedFd, RawFd};
use std::sync::atomic::{AtomicU64, Ordering};

pub(crate) const MAX_GC_OBJECTS: usize = 16;
pub(crate) const MAX_GC_STEPS: usize = 128;

static NEXT_AUTHORITY_NONCE: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct GcLimits {
    pub(crate) max_objects: usize,
    pub(crate) max_steps: usize,
}

impl Default for GcLimits {
    fn default() -> Self {
        Self {
            max_objects: MAX_GC_OBJECTS,
            max_steps: MAX_GC_STEPS,
        }
    }
}

impl GcLimits {
    fn validate(self) -> Result<Self, GcError> {
        if self.max_objects == 0
            || self.max_objects > MAX_GC_OBJECTS
            || self.max_steps == 0
            || self.max_steps > MAX_GC_STEPS
        {
            Err(GcError::BudgetExceeded)
        } else {
            Ok(self)
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum GcCheckpoint {
    BeforeObject,
    BeforeEndpointDelete,
    AfterEndpointDelete,
    BeforePlaceholderDelete,
    AfterPlaceholderDelete,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum GcError {
    InvalidHandoff(&'static str),
    ScopeMismatch,
    AuthorityRevoked,
    ParentIdentityMismatch,
    WriterLeaseMissing,
    WriterIdentityMismatch,
    EndpointIdentityMismatch,
    PlaceholderIdentityMismatch,
    MissingObject(&'static str),
    BudgetExceeded,
    Interrupted,
    Fault(&'static str),
    Syscall(&'static str, i32),
}

/// A writer-stop authority is intentionally not constructible by consumers of
/// this module.  An external lifecycle controller must implement this sealed
/// grant in the same Rust crate and hand us the resulting capability.  A
/// random token or a path is never enough to create one.
mod sealed {
    pub trait Grant {}
}

pub(crate) trait QuiescenceGrant: sealed::Grant {
    fn grant(
        &mut self,
        obligation: &QuarantineObligation,
        limits: GcLimits,
    ) -> Result<QuarantineGcAuthority, GcError>;
}

/// Issuer owned by the Rust controller's `Quiescent` typestate. Construction
/// is possible only from a retained `QuarantineObligation`; every authority
/// it issues is a duplicate of those exact FDs and identities.
#[derive(Debug)]
pub(crate) struct ControllerQuiescenceGrant {
    scope: ScopeId,
    root_fd: OwnedFd,
    root_identity: FileIdentity,
    writer_parent: OwnedFd,
    writer_parent_identity: FileIdentity,
    writer_lock: OwnedFd,
    writer_lock_identity: FileIdentity,
    writer_lock_name: Vec<u8>,
}

impl ControllerQuiescenceGrant {
    fn from_obligation(obligation: &QuarantineObligation) -> Result<Self, GcError> {
        Ok(Self {
            scope: obligation.scope,
            root_fd: duplicate_capability(
                obligation.quarantine_parent.as_raw_fd(),
                "duplicate controller quarantine parent",
            )?,
            root_identity: obligation.quarantine_parent_identity,
            writer_parent: duplicate_capability(
                obligation.writer_parent.as_raw_fd(),
                "duplicate controller writer parent",
            )?,
            writer_parent_identity: obligation.writer_parent_identity,
            writer_lock: duplicate_capability(
                obligation.writer_lock.as_raw_fd(),
                "duplicate controller writer lease",
            )?,
            writer_lock_identity: obligation.writer_lock_identity,
            writer_lock_name: obligation.writer_lock_name.clone(),
        })
    }

    pub(crate) fn from_quiescent(
        quiescent: &crate::controller::Quiescent,
        obligation: &QuarantineObligation,
    ) -> Result<Self, GcError> {
        if !quiescent.gc_authority_matches(obligation.scope) {
            return Err(GcError::ScopeMismatch);
        }
        Self::from_obligation(obligation)
    }
}

impl sealed::Grant for ControllerQuiescenceGrant {}

impl QuiescenceGrant for ControllerQuiescenceGrant {
    fn grant(
        &mut self,
        obligation: &QuarantineObligation,
        limits: GcLimits,
    ) -> Result<QuarantineGcAuthority, GcError> {
        if obligation.scope != self.scope
            || obligation.quarantine_parent_identity != self.root_identity
            || obligation.writer_parent_identity != self.writer_parent_identity
            || obligation.writer_lock_identity != self.writer_lock_identity
            || obligation.writer_lock_name != self.writer_lock_name
        {
            return Err(GcError::WriterIdentityMismatch);
        }
        if unsafe { libc::flock(self.writer_lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0
        {
            return Err(GcError::WriterLeaseMissing);
        }
        QuarantineGcAuthority::new(
            self.scope,
            duplicate_capability(self.root_fd.as_raw_fd(), "duplicate controller root")?,
            self.root_identity,
            duplicate_capability(
                self.writer_parent.as_raw_fd(),
                "duplicate controller writer parent",
            )?,
            self.writer_parent_identity,
            duplicate_capability(
                self.writer_lock.as_raw_fd(),
                "duplicate controller writer lease",
            )?,
            self.writer_lock_identity,
            self.writer_lock_name.clone(),
            limits,
        )
    }
}

#[derive(Debug)]
pub(crate) struct QuarantineGcAuthority {
    scope: crate::controller::ScopeId,
    root_fd: OwnedFd,
    root_identity: FileIdentity,
    writer_parent: OwnedFd,
    writer_parent_identity: FileIdentity,
    writer_lock: OwnedFd,
    writer_lock_identity: FileIdentity,
    writer_lock_name: Vec<u8>,
    nonce: u64,
    steps: usize,
    limits: GcLimits,
}

impl QuarantineGcAuthority {
    #[allow(clippy::too_many_arguments)]
    fn new(
        scope: crate::controller::ScopeId,
        root_fd: OwnedFd,
        root_identity: FileIdentity,
        writer_parent: OwnedFd,
        writer_parent_identity: FileIdentity,
        writer_lock: OwnedFd,
        writer_lock_identity: FileIdentity,
        writer_lock_name: Vec<u8>,
        limits: GcLimits,
    ) -> Result<Self, GcError> {
        let limits = limits.validate()?;
        if root_identity.mode & libc::S_IFMT != libc::S_IFDIR || root_identity.nlink == 0 {
            return Err(GcError::ParentIdentityMismatch);
        }
        Ok(Self {
            scope,
            root_fd,
            root_identity,
            writer_parent,
            writer_parent_identity,
            writer_lock,
            writer_lock_identity,
            writer_lock_name,
            nonce: NEXT_AUTHORITY_NONCE.fetch_add(1, Ordering::Relaxed),
            steps: 0,
            limits,
        })
    }

    fn charge(&mut self) -> Result<(), GcError> {
        if self.steps >= self.limits.max_steps {
            return Err(GcError::BudgetExceeded);
        }
        self.steps += 1;
        Ok(())
    }

    fn validate_for(&mut self, obligation: &QuarantineObligation) -> Result<(), GcError> {
        self.charge()?;
        if self.nonce == 0 {
            return Err(GcError::AuthorityRevoked);
        }
        if self.scope != obligation.scope {
            return Err(GcError::ScopeMismatch);
        }
        if FileIdentity::from_fd(self.root_fd.as_raw_fd()).map_err(|_| GcError::AuthorityRevoked)?
            != self.root_identity
        {
            return Err(GcError::AuthorityRevoked);
        }
        if FileIdentity::from_fd(self.writer_lock.as_raw_fd())
            .map_err(|_| GcError::WriterIdentityMismatch)?
            != self.writer_lock_identity
        {
            return Err(GcError::WriterIdentityMismatch);
        }
        if FileIdentity::from_fd(self.writer_parent.as_raw_fd())
            .map_err(|_| GcError::WriterIdentityMismatch)?
            != self.writer_parent_identity
            || FileIdentity::from_fd(obligation.writer_parent.as_raw_fd())
                .map_err(|_| GcError::WriterIdentityMismatch)?
                != obligation.writer_parent_identity
            || obligation.writer_parent_identity != self.writer_parent_identity
        {
            return Err(GcError::WriterIdentityMismatch);
        }
        let lock_name =
            valid_name(&self.writer_lock_name).map_err(|_| GcError::WriterIdentityMismatch)?;
        if FileIdentity::from_at(self.writer_parent.as_raw_fd(), &lock_name)
            .map_err(|_| GcError::WriterIdentityMismatch)?
            != self.writer_lock_identity
            || obligation.writer_lock_name != self.writer_lock_name
        {
            return Err(GcError::WriterIdentityMismatch);
        }
        if FileIdentity::from_fd(obligation.writer_lock.as_raw_fd())
            .map_err(|_| GcError::WriterIdentityMismatch)?
            != obligation.writer_lock_identity
            || obligation.writer_lock_identity != self.writer_lock_identity
        {
            return Err(GcError::WriterIdentityMismatch);
        }
        if unsafe { libc::flock(self.writer_lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0
        {
            return Err(GcError::WriterLeaseMissing);
        }
        if FileIdentity::from_fd(obligation.quarantine_parent.as_raw_fd())
            .map_err(|_| GcError::ParentIdentityMismatch)?
            != obligation.quarantine_parent_identity
            || obligation.quarantine_parent_identity != self.root_identity
        {
            return Err(GcError::ParentIdentityMismatch);
        }
        if FileIdentity::from_fd(obligation.source_parent.as_raw_fd())
            .map_err(|_| GcError::ParentIdentityMismatch)?
            != obligation.source_parent_identity
        {
            return Err(GcError::ParentIdentityMismatch);
        }
        Ok(())
    }
}

pub(crate) trait GcFaultInjector {
    fn checkpoint(
        &mut self,
        checkpoint: GcCheckpoint,
        authority: &mut QuarantineGcAuthority,
        obligation: &QuarantineObligation,
    ) -> Result<(), GcError>;
}

#[derive(Default)]
pub(crate) struct NoGcFault;

impl GcFaultInjector for NoGcFault {
    fn checkpoint(
        &mut self,
        _checkpoint: GcCheckpoint,
        _authority: &mut QuarantineGcAuthority,
        _obligation: &QuarantineObligation,
    ) -> Result<(), GcError> {
        Ok(())
    }
}

#[must_use = "GcFailure owns retained quarantine obligations; consume it with into_parts()"]
#[derive(Debug)]
pub(crate) struct GcFailure {
    pending: QuarantinePending,
    deleted_objects: usize,
    completed_endpoints: usize,
    error: GcError,
}

impl GcFailure {
    pub(crate) fn error(&self) -> GcError {
        self.error
    }

    pub(crate) fn deleted_objects(&self) -> usize {
        self.deleted_objects
    }

    pub(crate) fn into_parts(self) -> (QuarantinePending, usize, usize, GcError) {
        (
            self.pending,
            self.deleted_objects,
            self.completed_endpoints,
            self.error,
        )
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub(crate) struct QuarantineGcConsumer {
    limits: GcLimits,
}

impl QuarantineGcConsumer {
    pub(crate) fn new(limits: GcLimits) -> Result<Self, GcError> {
        Ok(Self {
            limits: limits.validate()?,
        })
    }

    pub(crate) fn collect<F: GcFaultInjector>(
        &self,
        pending: QuarantinePending,
        authority: &mut QuarantineGcAuthority,
        fault: &mut F,
    ) -> Result<Cleaned, GcFailure> {
        self.collect_parts(pending, 0, authority, fault)
    }

    pub(crate) fn retry<F: GcFaultInjector>(
        &self,
        failure: GcFailure,
        authority: &mut QuarantineGcAuthority,
        fault: &mut F,
    ) -> Result<Cleaned, GcFailure> {
        let (pending, deleted_objects, completed_endpoints, _) = failure.into_parts();
        match self.collect_parts(pending, completed_endpoints, authority, fault) {
            Ok(cleaned) => Ok(cleaned),
            Err(mut next) => {
                next.deleted_objects += deleted_objects;
                Err(next)
            }
        }
    }

    fn collect_parts<F: GcFaultInjector>(
        &self,
        pending: QuarantinePending,
        completed_endpoints: usize,
        authority: &mut QuarantineGcAuthority,
        fault: &mut F,
    ) -> Result<Cleaned, GcFailure> {
        let (request, journal, signals, mut obligations) = pending.into_parts();
        if let Err(error) = self.validate_handoff(&journal, &obligations) {
            return Err(GcFailure {
                pending: QuarantinePending::from_parts(request, journal, signals, obligations),
                deleted_objects: 0,
                completed_endpoints,
                error,
            });
        }
        if obligations.len() > self.limits.max_objects {
            return Err(GcFailure {
                pending: QuarantinePending::from_parts(request, journal, signals, obligations),
                deleted_objects: 0,
                completed_endpoints,
                error: GcError::BudgetExceeded,
            });
        }
        let mut deleted_objects = 0;
        for index in 0..obligations.len() {
            if obligations[index].is_complete() {
                continue;
            }
            let result = self.delete_one(&mut obligations[index], authority, fault);
            match result {
                Ok(count) => deleted_objects += count,
                Err(step_failure) => {
                    deleted_objects += step_failure.deleted;
                    let removed_endpoints = index;
                    let remaining = obligations.split_off(index);
                    return Err(GcFailure {
                        pending: QuarantinePending::from_parts(
                            request, journal, signals, remaining,
                        ),
                        deleted_objects,
                        completed_endpoints: completed_endpoints + removed_endpoints,
                        error: step_failure.error,
                    });
                }
            }
        }
        if obligations
            .iter()
            .any(|obligation| !obligation.is_complete())
        {
            return Err(GcFailure {
                pending: QuarantinePending::from_parts(request, journal, signals, obligations),
                deleted_objects,
                completed_endpoints,
                error: GcError::InvalidHandoff("incomplete quarantine ledger"),
            });
        }
        let endpoint_count = completed_endpoints + obligations.len();
        let mut canonical_journal: Vec<JournalEvent> = journal.into_iter().take(6).collect();
        canonical_journal.push(JournalEvent::Cleaned { endpoint_count });
        let cleaned =
            QuarantinePending::from_parts(request, canonical_journal, signals, obligations)
                .into_cleaned(endpoint_count);
        Ok(cleaned)
    }

    fn validate_handoff(
        &self,
        journal: &[JournalEvent],
        obligations: &[QuarantineObligation],
    ) -> Result<(), GcError> {
        if obligations.is_empty() || obligations.len() > self.limits.max_objects {
            return Err(GcError::InvalidHandoff("response or obligation set"));
        }
        let expected = [
            JournalEvent::Prepared,
            JournalEvent::CapabilitiesAcquired,
            JournalEvent::MembershipBound { member_count: 0 },
            JournalEvent::ShutdownRequested {
                signals: Vec::new(),
            },
            JournalEvent::Drained,
            JournalEvent::Quiescent,
        ];
        if journal.len() < expected.len() + 1
            || !matches!(journal.first(), Some(JournalEvent::Prepared))
            || !matches!(journal.get(1), Some(JournalEvent::CapabilitiesAcquired))
            || !matches!(journal.get(2), Some(JournalEvent::MembershipBound { .. }))
            || !matches!(journal.get(3), Some(JournalEvent::ShutdownRequested { .. }))
            || !matches!(journal.get(4), Some(JournalEvent::Drained))
            || !matches!(journal.get(5), Some(JournalEvent::Quiescent))
            || !journal.iter().any(|event| {
                matches!(
                    event,
                    JournalEvent::Recovery {
                        obligation: RecoveryObligation::QuarantineGcRequired,
                        ..
                    }
                )
            })
            || journal
                .iter()
                .any(|event| matches!(event, JournalEvent::Finalized { .. }))
        {
            return Err(GcError::InvalidHandoff("journal phase order"));
        }
        let scope = obligations[0].scope;
        let parent = obligations[0].quarantine_parent_identity;
        if obligations.iter().any(|obligation| {
            obligation.scope != scope
                || obligation.quarantine_parent_identity != parent
                || obligation.source_name.is_empty()
                || obligation.endpoint_name.is_empty()
                || obligation.placeholder_name.is_empty()
                || obligation.endpoint_name == obligation.placeholder_name
        }) {
            return Err(GcError::InvalidHandoff("ownership topology"));
        }
        for obligation in obligations {
            valid_name(&obligation.source_name)?;
            valid_name(&obligation.endpoint_name)?;
            valid_name(&obligation.placeholder_name)?;
        }
        Ok(())
    }

    fn delete_one<F: GcFaultInjector>(
        &self,
        obligation: &mut QuarantineObligation,
        authority: &mut QuarantineGcAuthority,
        fault: &mut F,
    ) -> Result<usize, GcStepFailure> {
        let mut deleted = 0;
        preserve(
            fault.checkpoint(GcCheckpoint::BeforeObject, authority, obligation),
            deleted,
        )?;
        preserve(authority.validate_for(obligation), deleted)?;
        if !obligation.endpoint_deleted() {
            preserve(
                verify_named(
                    obligation.quarantine_parent.as_raw_fd(),
                    &obligation.endpoint_name,
                    obligation.identity,
                    GcError::EndpointIdentityMismatch,
                ),
                deleted,
            )?;
            let endpoint_identity = preserve(
                FileIdentity::from_fd(obligation.endpoint_fd.as_raw_fd())
                    .map_err(|_| GcError::EndpointIdentityMismatch),
                deleted,
            )?;
            if endpoint_identity != obligation.identity {
                return Err(GcStepFailure {
                    error: GcError::EndpointIdentityMismatch,
                    deleted,
                });
            }
            preserve(
                fault.checkpoint(GcCheckpoint::BeforeEndpointDelete, authority, obligation),
                deleted,
            )?;
            preserve(authority.validate_for(obligation), deleted)?;
            preserve(
                unlink_name(
                    obligation.quarantine_parent.as_raw_fd(),
                    &obligation.endpoint_name,
                    "endpoint",
                ),
                deleted,
            )?;
            obligation.mark_endpoint_deleted();
            deleted += 1;
            preserve(
                fault.checkpoint(GcCheckpoint::AfterEndpointDelete, authority, obligation),
                deleted,
            )?;
        }
        if !obligation.placeholder_deleted() {
            preserve(authority.validate_for(obligation), deleted)?;
            preserve(
                verify_named(
                    obligation.quarantine_parent.as_raw_fd(),
                    &obligation.placeholder_name,
                    obligation.placeholder_identity,
                    GcError::PlaceholderIdentityMismatch,
                ),
                deleted,
            )?;
            let placeholder_identity = preserve(
                FileIdentity::from_fd(obligation.placeholder_fd.as_raw_fd())
                    .map_err(|_| GcError::PlaceholderIdentityMismatch),
                deleted,
            )?;
            if placeholder_identity != obligation.placeholder_identity {
                return Err(GcStepFailure {
                    error: GcError::PlaceholderIdentityMismatch,
                    deleted,
                });
            }
            preserve(
                fault.checkpoint(GcCheckpoint::BeforePlaceholderDelete, authority, obligation),
                deleted,
            )?;
            preserve(authority.validate_for(obligation), deleted)?;
            preserve(
                unlink_name(
                    obligation.quarantine_parent.as_raw_fd(),
                    &obligation.placeholder_name,
                    "placeholder",
                ),
                deleted,
            )?;
            obligation.mark_placeholder_deleted();
            deleted += 1;
            preserve(
                fault.checkpoint(GcCheckpoint::AfterPlaceholderDelete, authority, obligation),
                deleted,
            )?;
        }
        Ok(deleted)
    }
}

struct GcStepFailure {
    error: GcError,
    deleted: usize,
}

fn duplicate_capability(fd: RawFd, operation: &'static str) -> Result<OwnedFd, GcError> {
    crate::duplicate_fd(fd).map_err(|_| {
        GcError::Syscall(
            operation,
            std::io::Error::last_os_error().raw_os_error().unwrap_or(-1),
        )
    })
}

fn preserve<T>(result: Result<T, GcError>, deleted: usize) -> Result<T, GcStepFailure> {
    result.map_err(|error| GcStepFailure { error, deleted })
}

fn valid_name(name: &[u8]) -> Result<CString, GcError> {
    if name.is_empty() || name == b"." || name == b".." || name.contains(&b'/') || name.contains(&0)
    {
        return Err(GcError::InvalidHandoff("non-component name"));
    }
    CString::new(name).map_err(|_| GcError::InvalidHandoff("non-component name"))
}

fn verify_named(
    parent: RawFd,
    name: &[u8],
    expected: FileIdentity,
    mismatch: GcError,
) -> Result<(), GcError> {
    let name = valid_name(name)?;
    let observed =
        FileIdentity::from_at(parent, &name).map_err(|_| GcError::MissingObject("named"))?;
    if observed == expected {
        Ok(())
    } else {
        Err(mismatch)
    }
}

fn unlink_name(parent: RawFd, name: &[u8], operation: &'static str) -> Result<(), GcError> {
    let name = valid_name(name)?;
    let result = unsafe { libc::unlinkat(parent, name.as_ptr(), 0) };
    if result == 0 {
        Ok(())
    } else {
        Err(GcError::Syscall(
            operation,
            std::io::Error::last_os_error().raw_os_error().unwrap_or(-1),
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::controller::{
        fresh_scope_for_test, ControllerBudget, ControllerOperation, ControllerProfile,
        ControllerRequest, EndpointKind, JournalEvent, RecoveryObligation, SignalEvidence,
        SignalTarget,
    };
    use std::fs::{self, File, OpenOptions};
    use std::io::Write;
    use std::os::fd::FromRawFd;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(1);

    struct Fixture {
        root: PathBuf,
        obligation: Option<QuarantineObligation>,
    }

    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "darling-quarantine-gc-{}-{}",
                std::process::id(),
                NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(root.join("source")).unwrap();
            fs::create_dir_all(root.join("quarantine")).unwrap();
            write_file(&root.join("source/source"), b"source");
            write_file(&root.join("lock"), b"lock");
            write_file(&root.join("quarantine/endpoint"), b"endpoint");
            write_file(&root.join("quarantine/placeholder"), b"placeholder");

            let source_parent = dir_fd(&root.join("source"));
            let quarantine_parent = dir_fd(&root.join("quarantine"));
            let writer_parent = dir_fd(&root);
            let writer_lock = file_fd(&root.join("lock"));
            assert_eq!(
                unsafe { libc::flock(writer_lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
                0
            );
            let endpoint_fd = file_fd(&root.join("quarantine/endpoint"));
            let placeholder_fd = file_fd(&root.join("quarantine/placeholder"));
            let source_identity = FileIdentity::from_fd(source_parent.as_raw_fd()).unwrap();
            let quarantine_identity = FileIdentity::from_fd(quarantine_parent.as_raw_fd()).unwrap();
            let endpoint_identity = FileIdentity::from_fd(endpoint_fd.as_raw_fd()).unwrap();
            let placeholder_identity = FileIdentity::from_fd(placeholder_fd.as_raw_fd()).unwrap();
            let writer_lock_identity = FileIdentity::from_fd(writer_lock.as_raw_fd()).unwrap();
            let writer_parent_identity = FileIdentity::from_fd(writer_parent.as_raw_fd()).unwrap();
            let scope = fresh_scope_for_test();
            let obligation = QuarantineObligation::new(
                scope,
                EndpointKind::Shellspawn,
                endpoint_identity,
                source_identity,
                quarantine_identity,
                placeholder_identity,
                writer_parent_identity,
                writer_lock_identity,
                source_parent,
                quarantine_parent,
                writer_parent,
                writer_lock,
                b"lock".to_vec(),
                b"source".to_vec(),
                b"endpoint".to_vec(),
                b"placeholder".to_vec(),
                endpoint_fd,
                placeholder_fd,
            );
            Self {
                root,
                obligation: Some(obligation),
            }
        }

        fn take(&mut self) -> QuarantineObligation {
            self.obligation.take().unwrap()
        }

        fn second_obligation(&self, base: &QuarantineObligation) -> QuarantineObligation {
            write_file(&self.root.join("source/source-second"), b"source-second");
            write_file(
                &self.root.join("quarantine/endpoint-second"),
                b"endpoint-second",
            );
            write_file(
                &self.root.join("quarantine/placeholder-second"),
                b"placeholder-second",
            );
            let endpoint_fd = file_fd(&self.root.join("quarantine/endpoint-second"));
            let placeholder_fd = file_fd(&self.root.join("quarantine/placeholder-second"));
            let source_parent = dir_fd(&self.root.join("source"));
            let quarantine_parent = dir_fd(&self.root.join("quarantine"));
            let writer_parent = dir_fd(&self.root);
            let writer_lock = crate::duplicate_fd(base.writer_lock.as_raw_fd()).unwrap();
            QuarantineObligation::new(
                base.scope,
                base.kind,
                FileIdentity::from_fd(endpoint_fd.as_raw_fd()).unwrap(),
                base.source_parent_identity,
                base.quarantine_parent_identity,
                FileIdentity::from_fd(placeholder_fd.as_raw_fd()).unwrap(),
                base.writer_parent_identity,
                FileIdentity::from_fd(writer_lock.as_raw_fd()).unwrap(),
                source_parent,
                quarantine_parent,
                writer_parent,
                writer_lock,
                base.writer_lock_name.clone(),
                b"source-second".to_vec(),
                b"endpoint-second".to_vec(),
                b"placeholder-second".to_vec(),
                endpoint_fd,
                placeholder_fd,
            )
        }

        fn endpoint(&self) -> PathBuf {
            self.root.join("quarantine/endpoint")
        }

        fn placeholder(&self) -> PathBuf {
            self.root.join("quarantine/placeholder")
        }

        fn lock(&self) -> PathBuf {
            self.root.join("lock")
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    fn write_file(path: &std::path::Path, bytes: &[u8]) {
        let mut file = File::create(path).unwrap();
        file.write_all(bytes).unwrap();
    }

    fn dir_fd(path: &std::path::Path) -> OwnedFd {
        File::open(path).unwrap().into()
    }

    fn file_fd(path: &std::path::Path) -> OwnedFd {
        OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .unwrap()
            .into()
    }

    struct Grant;
    impl sealed::Grant for Grant {}
    impl QuiescenceGrant for Grant {
        fn grant(
            &mut self,
            obligation: &QuarantineObligation,
            limits: GcLimits,
        ) -> Result<QuarantineGcAuthority, GcError> {
            let fd = unsafe {
                let duplicated = libc::fcntl(
                    obligation.quarantine_parent.as_raw_fd(),
                    libc::F_DUPFD_CLOEXEC,
                    0,
                );
                if duplicated < 0 {
                    return Err(GcError::Syscall(
                        "dup authority",
                        std::io::Error::last_os_error().raw_os_error().unwrap_or(-1),
                    ));
                }
                OwnedFd::from_raw_fd(duplicated)
            };
            let writer_lock = unsafe {
                let duplicated =
                    libc::fcntl(obligation.writer_lock.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0);
                if duplicated < 0 {
                    return Err(GcError::Syscall(
                        "dup writer lease",
                        std::io::Error::last_os_error().raw_os_error().unwrap_or(-1),
                    ));
                }
                OwnedFd::from_raw_fd(duplicated)
            };
            let writer_parent = unsafe {
                let duplicated = libc::fcntl(
                    obligation.writer_parent.as_raw_fd(),
                    libc::F_DUPFD_CLOEXEC,
                    0,
                );
                if duplicated < 0 {
                    return Err(GcError::Syscall(
                        "dup writer parent",
                        std::io::Error::last_os_error().raw_os_error().unwrap_or(-1),
                    ));
                }
                OwnedFd::from_raw_fd(duplicated)
            };
            QuarantineGcAuthority::new(
                obligation.scope,
                fd,
                obligation.quarantine_parent_identity,
                writer_parent,
                obligation.writer_parent_identity,
                writer_lock,
                obligation.writer_lock_identity,
                obligation.writer_lock_name.clone(),
                limits,
            )
        }
    }

    struct Fault {
        point: GcCheckpoint,
        fired: bool,
    }

    impl Fault {
        fn interrupt(point: GcCheckpoint) -> Self {
            Self {
                point,
                fired: false,
            }
        }
    }

    impl GcFaultInjector for Fault {
        fn checkpoint(
            &mut self,
            checkpoint: GcCheckpoint,
            _authority: &mut QuarantineGcAuthority,
            _obligation: &QuarantineObligation,
        ) -> Result<(), GcError> {
            if self.fired || checkpoint != self.point {
                return Ok(());
            }
            self.fired = true;
            Err(GcError::Interrupted)
        }
    }

    fn pending_with(obligations: Vec<QuarantineObligation>) -> QuarantinePending {
        let signal = SignalEvidence::rust_pidfd(
            SignalTarget::SessionRoot,
            crate::controller::SignalResult::Sent,
        );
        let request = ControllerRequest {
            schema_version: crate::controller::CONTROLLER_SCHEMA_VERSION,
            transaction_id: "gc-test".into(),
            profile: ControllerProfile::Rootless,
            operation: ControllerOperation::RequestShutdown,
            anchor_fd: 3,
            evidence_fd: None,
            controller_closure_sha256: "a".repeat(64),
            runtime_identity_digest: "b".repeat(64),
            request_nonce: "c".repeat(64),
            budget: ControllerBudget {
                max_events: 32,
                max_virtual_time_ns: 1000,
                max_members: 8,
                max_recovery_steps: 8,
                deadline_ns: 1000,
            },
        };
        QuarantinePending::from_parts_for_test(
            request,
            vec![
                JournalEvent::Prepared,
                JournalEvent::CapabilitiesAcquired,
                JournalEvent::MembershipBound { member_count: 1 },
                JournalEvent::ShutdownRequested {
                    signals: vec![signal.clone()],
                },
                JournalEvent::Drained,
                JournalEvent::Quiescent,
                JournalEvent::Recovery {
                    obligation: RecoveryObligation::QuarantineGcRequired,
                    completed: 0,
                },
            ],
            vec![signal],
            obligations,
        )
    }

    fn pending(obligation: QuarantineObligation) -> QuarantinePending {
        pending_with(vec![obligation])
    }

    #[test]
    fn normal_gc_ends_success_without_quarantine_obligation() {
        let mut fixture = Fixture::new();
        let obligation = fixture.take();
        let mut grant = Grant;
        let authority = &mut grant.grant(&obligation, GcLimits::default()).unwrap();
        let cleaned = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(pending(obligation), authority, &mut NoGcFault)
            .unwrap();
        let finalized = cleaned.finalize().unwrap();
        let (response, quarantines) = finalized.into_parts();
        assert_eq!(quarantines.len(), 0);
        assert_eq!(
            response.verdict,
            crate::controller::ControllerVerdict::Success
        );
        assert!(response.obligations.is_empty());
        assert_eq!(
            response.journal[6],
            JournalEvent::Cleaned { endpoint_count: 1 }
        );
        assert!(!response
            .journal
            .iter()
            .any(|event| matches!(event, JournalEvent::Recovery { .. })));
        assert!(!fixture.endpoint().exists());
        assert!(!fixture.placeholder().exists());
    }

    #[test]
    fn controller_issuer_binds_retained_writer_lease() {
        let mut fixture = Fixture::new();
        let obligation = fixture.take();
        let mut issuer = ControllerQuiescenceGrant::from_obligation(&obligation).unwrap();
        let mut authority = issuer.grant(&obligation, GcLimits::default()).unwrap();
        let cleaned = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(pending(obligation), &mut authority, &mut NoGcFault)
            .unwrap();
        let (response, quarantines) = cleaned.finalize().unwrap().into_parts();
        assert_eq!(
            response.verdict,
            crate::controller::ControllerVerdict::Success
        );
        assert!(quarantines.is_empty());
    }

    #[test]
    fn cooperative_writer_protocol_blocks_unleased_mutation() {
        let mut fixture = Fixture::new();
        let obligation = fixture.take();
        let competing = OpenOptions::new()
            .read(true)
            .write(true)
            .open(fixture.lock())
            .unwrap();
        let result = unsafe { libc::flock(competing.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        assert_eq!(
            result, -1,
            "an unleased writer must not enter the namespace"
        );
        let errno = std::io::Error::last_os_error().raw_os_error();
        assert!(errno == Some(libc::EWOULDBLOCK) || errno == Some(libc::EAGAIN));

        let mut grant = ControllerQuiescenceGrant::from_obligation(&obligation).unwrap();
        let mut authority = grant.grant(&obligation, GcLimits::default()).unwrap();
        let cleaned = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(pending(obligation), &mut authority, &mut NoGcFault)
            .unwrap();
        assert_eq!(
            cleaned.finalize().unwrap().into_parts().0.verdict,
            crate::controller::ControllerVerdict::Success
        );
    }

    #[test]
    fn replacement_before_gc_is_preserved() {
        let mut fixture = Fixture::new();
        let obligation = fixture.take();
        let name = valid_name(&obligation.endpoint_name).unwrap();
        assert_eq!(
            unsafe { libc::unlinkat(obligation.quarantine_parent.as_raw_fd(), name.as_ptr(), 0) },
            0
        );
        write_file(&fixture.endpoint(), b"replacement");
        let mut grant = Grant;
        let authority = &mut grant.grant(&obligation, GcLimits::default()).unwrap();
        let failure = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(pending(obligation), authority, &mut NoGcFault)
            .unwrap_err();
        assert_eq!(failure.error(), GcError::EndpointIdentityMismatch);
        let (pending, _, _, _) = failure.into_parts();
        let (_, _, _, remaining) = pending.into_parts();
        assert_eq!(remaining.len(), 1);
        assert_eq!(fs::read(fixture.endpoint()).unwrap(), b"replacement");
    }

    #[test]
    fn sigint_after_partial_delete_is_retryable_without_repeating_endpoint() {
        let mut fixture = Fixture::new();
        let obligation = fixture.take();
        let mut grant = Grant;
        let authority = &mut grant.grant(&obligation, GcLimits::default()).unwrap();
        let mut fault = Fault::interrupt(GcCheckpoint::AfterEndpointDelete);
        let failure = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(pending(obligation), authority, &mut fault)
            .unwrap_err();
        assert_eq!(failure.error(), GcError::Interrupted);
        assert_eq!(failure.deleted_objects(), 1);
        assert!(!fixture.endpoint().exists());
        let (pending, _, _, _) = failure.into_parts();
        let (request, journal, signals, mut obligations) = pending.into_parts();
        let obligation = obligations.pop().unwrap();
        let mut grant = Grant;
        let authority = &mut grant.grant(&obligation, GcLimits::default()).unwrap();
        let cleaned = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(
                QuarantinePending::from_parts(request, journal, signals, vec![obligation]),
                authority,
                &mut NoGcFault,
            )
            .unwrap();
        let finalized = cleaned.finalize().unwrap();
        let (response, _) = finalized.into_parts();
        assert_eq!(
            response.verdict,
            crate::controller::ControllerVerdict::Success
        );
        assert_eq!(
            response.journal[6],
            JournalEvent::Cleaned { endpoint_count: 1 }
        );
        assert!(!fixture.placeholder().exists());
    }

    #[test]
    fn replaced_lock_name_invalidates_writer_stop_authority() {
        let mut fixture = Fixture::new();
        let obligation = fixture.take();
        let mut grant = Grant;
        let authority = &mut grant.grant(&obligation, GcLimits::default()).unwrap();
        fs::remove_file(fixture.lock()).unwrap();
        write_file(&fixture.lock(), b"replacement-lock");
        let failure = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(pending(obligation), authority, &mut NoGcFault)
            .unwrap_err();
        assert_eq!(failure.error(), GcError::WriterIdentityMismatch);
        assert!(fixture.endpoint().exists());
        assert!(fixture.placeholder().exists());
    }

    #[test]
    fn retry_accounts_endpoints_not_deleted_objects() {
        let mut fixture = Fixture::new();
        let first = fixture.take();
        let second = fixture.second_obligation(&first);
        let mut grant = Grant;
        let authority = &mut grant.grant(&first, GcLimits::default()).unwrap();
        let mut fault = Fault::interrupt(GcCheckpoint::AfterPlaceholderDelete);
        let failure = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(pending_with(vec![first, second]), authority, &mut fault)
            .unwrap_err();
        assert_eq!(failure.deleted_objects(), 2);
        let (pending, _, _, _) = failure.into_parts();
        let (request, journal, signals, obligations) = pending.into_parts();
        assert_eq!(obligations.len(), 2);
        let mut grant = Grant;
        let authority = &mut grant.grant(&obligations[0], GcLimits::default()).unwrap();
        let cleaned = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(
                QuarantinePending::from_parts(request, journal, signals, obligations),
                authority,
                &mut NoGcFault,
            )
            .unwrap();
        let (response, _) = cleaned.finalize().unwrap().into_parts();
        assert_eq!(
            response.journal[6],
            JournalEvent::Cleaned { endpoint_count: 2 }
        );
    }

    #[test]
    fn terminal_pending_is_rejected_before_any_gc_syscall() {
        let mut fixture = Fixture::new();
        let obligation = fixture.take();
        let pending = pending(obligation);
        let (request, mut journal, signals, obligations) = pending.into_parts();
        journal.push(JournalEvent::Finalized {
            verdict: crate::controller::ControllerVerdict::FailClosed,
        });
        let pending = QuarantinePending::from_parts(request, journal, signals, obligations);
        let (request, journal, signals, obligations) = pending.into_parts();
        let obligation = obligations.into_iter().next().unwrap();
        let mut grant = Grant;
        let authority = &mut grant.grant(&obligation, GcLimits::default()).unwrap();
        let failure = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(
                QuarantinePending::from_parts(request, journal, signals, vec![obligation]),
                authority,
                &mut NoGcFault,
            )
            .unwrap_err();
        assert!(matches!(failure.error(), GcError::InvalidHandoff(_)));
    }

    #[test]
    fn budget_preserves_tail_and_does_not_delete_unowned_objects() {
        let mut fixture_a = Fixture::new();
        let a = fixture_a.take();
        let b = a.duplicate_for_test(b"-tail");
        let mut grant = Grant;
        let authority = &mut grant
            .grant(
                &a,
                GcLimits {
                    max_objects: 2,
                    max_steps: 1,
                },
            )
            .unwrap();
        let failure = QuarantineGcConsumer::new(GcLimits {
            max_objects: 2,
            max_steps: 1,
        })
        .unwrap()
        .collect(pending_with(vec![a, b]), authority, &mut NoGcFault)
        .unwrap_err();
        assert_eq!(failure.error(), GcError::BudgetExceeded);
        let (pending, _, _, _) = failure.into_parts();
        let (_, _, _, remaining) = pending.into_parts();
        assert_eq!(remaining.len(), 2);
    }

    #[test]
    fn finish_handoff_requires_external_authority() {
        let mut fixture = Fixture::new();
        let obligation = fixture.take();
        let pending = pending(obligation);
        let (_, _, _, obligations) = pending.into_parts();
        assert_eq!(obligations.len(), 1);
    }

    #[test]
    fn authority_scope_mismatch_fails_closed_without_deletion() {
        let mut fixture_a = Fixture::new();
        let mut fixture_b = Fixture::new();
        let a = fixture_a.take();
        let b = fixture_b.take();
        let mut grant = Grant;
        let authority = &mut grant.grant(&a, GcLimits::default()).unwrap();
        let failure = QuarantineGcConsumer::new(GcLimits::default())
            .unwrap()
            .collect(pending(b), authority, &mut NoGcFault)
            .unwrap_err();
        assert_eq!(failure.error(), GcError::ScopeMismatch);
        let (pending, _, _, _) = failure.into_parts();
        let (_, _, _, remaining) = pending.into_parts();
        assert_eq!(remaining.len(), 1);
        assert!(fixture_b.endpoint().exists());
        assert!(fixture_b.placeholder().exists());
    }
}
