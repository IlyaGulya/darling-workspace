/*
 * Stubs for the dar-dtape-explicit-context-6to3.6 receive-copyout host contract.
 *
 * Only the leaves of the two driven functions are stubbed; the knote-selection stubs record the
 * knote they were handed so a substitution of the ambient thread is observable.
 * Kernel-flavoured host build: no libc headers (the XNU headers shadow libc types).
 */
#include <ipc/ipc_object.h>
#include <ipc/ipc_right.h>
#include <ipc/ipc_port.h>
#include <ipc/ipc_entry.h>
#include <ipc/ipc_space.h>
#include <kern/thread.h>
#include <kern/locks.h>
#include <kern/lock_group.h>

extern thread_t contract_ambient_thread;
extern uintptr_t contract_prepare_knote;
extern uintptr_t contract_srp_knote;

thread_t current_thread(void) {
	return contract_ambient_thread;
}

/* The space lock macros take &ipc_lck_grp; the lock itself is not exercised (inactive space). */
lck_grp_t ipc_lck_grp;

void lck_spin_lock_grp(lck_spin_t* lck, lck_grp_t* grp) {
	(void)lck;
	(void)grp;
}

void lck_spin_unlock(lck_spin_t* lck) {
	(void)lck;
}

/* ---- recorded knote-selection leaves ---- */

void filt_machport_turnstile_prepare_lazily(struct knote* kn, mach_msg_type_name_t msgt_name, ipc_port_t port) {
	(void)msgt_name;
	(void)port;
	contract_prepare_knote = (uintptr_t)kn;
}

void ipc_port_adjust_special_reply_port_locked(ipc_port_t special_reply_port, struct knote* kn, uint8_t flags, boolean_t get_turnstile) {
	(void)special_reply_port;
	(void)flags;
	(void)get_turnstile;
	contract_srp_knote = (uintptr_t)kn;
}

/* ---- remaining leaves the linker needs ---- */

void ipc_entry_modified(ipc_space_t space, mach_port_name_t name, ipc_entry_t entry) {
	(void)space;
	(void)name;
	(void)entry;
}

void ipc_port_release_receive(ipc_port_t port) {
	(void)port;
}

void (panic)(const char* fmt, ...) {
	(void)fmt;
	__builtin_trap();
}

void Assert(const char* file, int line, const char* expression) {
	(void)file;
	(void)line;
	(void)expression;
	__builtin_trap();
}

/* ---- additional unreferenced-by-this-path leaves the linker still needs ---- */

void ipc_port_release_send(ipc_port_t port) {
	(void)port;
}

void ipc_notify_send_once(ipc_port_t port) {
	(void)port;
}

void imq_lock(ipc_mqueue_t mq) {
	(void)mq;
}

void ipc_port_adjust_port_locked(ipc_port_t port, struct knote* kn, boolean_t sync_bootstrap_checkin) {
	(void)port;
	(void)kn;
	(void)sync_bootstrap_checkin;
}

mach_port_delta_t ipc_port_impcount_delta(ipc_port_t port, mach_port_delta_t delta, ipc_port_t base) {
	(void)port;
	(void)delta;
	(void)base;
	return 0;
}

void ipc_port_send_turnstile_complete(ipc_port_t port) {
	(void)port;
}

bool pinned_control_port_enabled;

kern_return_t ipc_entries_hold(ipc_space_t space, natural_t count) {
	(void)space;
	(void)count;
	return KERN_SUCCESS;
}

kern_return_t ipc_entry_grow_table(ipc_space_t space, ipc_table_elems_t target_size) {
	(void)space;
	(void)target_size;
	return KERN_FAILURE;
}

kern_return_t ipc_entry_claim(ipc_space_t space, mach_port_name_t* namep, ipc_entry_t* entryp) {
	(void)space;
	(void)namep;
	(void)entryp;
	return KERN_SUCCESS;
}

ipc_entry_t ipc_entry_lookup(ipc_space_t space, mach_port_name_t name) {
	(void)space;
	(void)name;
	return 0;
}

boolean_t ipc_hash_lookup(ipc_space_t space, ipc_object_t obj, mach_port_name_t* namep, ipc_entry_t* entryp) {
	(void)space;
	(void)obj;
	(void)namep;
	(void)entryp;
	return FALSE;
}

void ipc_hash_insert(ipc_space_t space, ipc_object_t obj, mach_port_name_t name, ipc_entry_t entry) {
	(void)space;
	(void)obj;
	(void)name;
	(void)entry;
}

void ipc_hash_delete(ipc_space_t space, ipc_object_t obj, mach_port_name_t name, ipc_entry_t entry) {
	(void)space;
	(void)obj;
	(void)name;
	(void)entry;
}

bool ipc_kobject_label_check(ipc_space_t space, ipc_port_t port, mach_msg_type_name_t msgt_name,
    ipc_object_copyout_flags_t* flags, ipc_port_t* subst_portp) {
	(void)space;
	(void)port;
	(void)msgt_name;
	(void)flags;
	(void)subst_portp;
	return TRUE;
}

void zone_id_require(zone_id_t zone_id, vm_size_t elem_size, void* addr) {
	(void)zone_id;
	(void)elem_size;
	(void)addr;
}

#undef OSCompareAndSwap
Boolean OSCompareAndSwap(UInt32 oldValue, UInt32 newValue, volatile UInt32* address) {
	(void)oldValue;
	(void)newValue;
	(void)address;
	return TRUE;
}

void ipc_port_finalize(ipc_port_t port) {
	(void)port;
}

void lck_spin_destroy(lck_spin_t* lck, lck_grp_t* grp) {
	(void)lck;
	(void)grp;
}

#undef zfree
void zfree(zone_t zone, void* elem) {
	(void)zone;
	(void)elem;
}
