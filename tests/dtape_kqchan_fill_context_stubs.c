/*
 * Stubs for the dar-dtape-explicit-context-6to3.5 host contract.
 *
 * Only the leaves of the driven path are stubbed; each stub records the thread/task/
 * space/map it was handed so a substitution of the ambient thread is observable.
 * Kernel-flavoured host build: no libc headers (the XNU headers shadow libc types).
 */
#include <darlingserver/duct-tape.h>
#include <darlingserver/duct-tape/thread.h>
#include <darlingserver/duct-tape/kqchan.h>
#include <darlingserver/duct-tape/task.h>
#include <darlingserver/duct-tape/log.h>
#include <darlingserver/duct-tape/memory.h>

#include <ipc/ipc_mqueue.h>
#include <ipc/ipc_port.h>
#include <ipc/ipc_kmsg.h>
#include <ipc/ipc_space.h>
#include <kern/thread.h>
#include <kern/task.h>
#include <vm/vm_map.h>
#include <kern/turnstile.h>
#include <kern/waitq.h>
#include <kern/sched_prim.h>
#include <mach/mach_types.h>

extern thread_t contract_ambient_thread;
extern int contract_ambient_calls;
extern uintptr_t contract_copyout_size_map;
extern uintptr_t contract_copyout_space;
extern uintptr_t contract_trailer_thread;
extern uintptr_t contract_add_trailer_thread;
extern uintptr_t contract_put_thread;
extern uintptr_t contract_put_task;
extern uintptr_t contract_put_map;
extern uintptr_t contract_importance_task;
extern uintptr_t contract_voucher_task;
int contract_waitq_assert_calls = 0;
int contract_turnstile_prepare_calls = 0;

thread_t current_thread(void) {
	contract_ambient_calls++;
	return contract_ambient_thread;
}

void dtape_log(dtape_log_level_t level, const char* format, ...) {
	(void)level;
	(void)format;
}

void dtape_semaphore_up(dtape_semaphore_t* semaphore) {
	(void)semaphore;
}

void (panic)(const char* fmt, ...) {
	(void)fmt;
	__builtin_trap();
}

/* ---- leaf stubs on the driven path ---- */

mach_msg_size_t ipc_kmsg_copyout_size(ipc_kmsg_t kmsg, vm_map_t map) {
	(void)kmsg;
	contract_copyout_size_map = (uintptr_t)map;
	return 32;
}

void ipc_kmsg_rmqueue(ipc_kmsg_queue_t queue, ipc_kmsg_t kmsg) {
	if (queue->ikmq_base == kmsg) {
		queue->ikmq_base = 0;
	}
}

mach_msg_return_t ipc_kmsg_copyout_on_thread(ipc_kmsg_t kmsg, ipc_space_t space, vm_map_t map, mach_msg_body_t* slist, mach_msg_option_t option, thread_t self) {
	(void)kmsg;
	(void)slist;
	(void)option;
	(void)self;
	contract_copyout_space = (uintptr_t)space;
	contract_copyout_size_map = (uintptr_t)map;
	return MACH_MSG_SUCCESS;
}

mach_msg_trailer_size_t ipc_kmsg_trailer_size(mach_msg_option_t option, thread_t thread) {
	(void)option;
	contract_trailer_thread = (uintptr_t)thread;
	return 0;
}

void ipc_kmsg_add_trailer(ipc_kmsg_t kmsg, ipc_space_t space, mach_msg_option_t option, thread_t thread,
    mach_port_seqno_t seqno, boolean_t minimal_trailer, mach_vm_offset_t context) {
	(void)kmsg;
	(void)space;
	(void)option;
	(void)seqno;
	(void)minimal_trailer;
	(void)context;
	contract_add_trailer_thread = (uintptr_t)thread;
}

mach_msg_return_t ipc_kmsg_put_on_thread(ipc_kmsg_t kmsg, mach_msg_option_t option, mach_vm_address_t rcv_addr,
    mach_msg_size_t rcv_size, mach_msg_size_t trailer_size, mach_msg_size_t* size, thread_t thread) {
	(void)kmsg;
	(void)option;
	(void)rcv_addr;
	(void)rcv_size;
	(void)trailer_size;
	/* The copyout boundary must name the requester and derive task/map from it, never
	 * from the ambient thread: record exactly what the receive path handed over. */
	contract_put_thread = (uintptr_t)thread;
	contract_put_task = (uintptr_t)thread->task;
	contract_put_map = (uintptr_t)thread->map;
	if (size) {
		*size = 32;
	}
	return MACH_MSG_SUCCESS;
}

/* Map-explicit copyout leaves reached from the TOO_LARGE branch of the receive path;
 * they must be handed the requester's map. */
int dtape_copyout_on_map(vm_map_t map, const void* kaddr, user_addr_t uaddr, vm_size_t nbytes) {
	(void)kaddr;
	(void)uaddr;
	(void)nbytes;
	contract_put_map = (uintptr_t)map;
	return 0;
}

int dtape_copyoutmsg_on_map(vm_map_t map, const char* kaddr, user_addr_t uaddr, mach_msg_size_t nbytes) {
	(void)kaddr;
	(void)uaddr;
	(void)nbytes;
	contract_put_map = (uintptr_t)map;
	return 0;
}

/* ---- additional unreferenced-by-this-path leaves the linker still needs ----
 * (numbered stubs; prototypes copied from the XNU headers so the host build links
 *  the same objects the product compiles). */

void Assert(const char* file, int line, const char* expression) {
	(void)file;
	(void)line;
	(void)expression;
	__builtin_trap();
}

void io_free(unsigned int otype, ipc_object_t object) {
	(void)otype;
	(void)object;
}

void io_lock(ipc_object_t io) {
	(void)io;
}

struct turnstile* turnstile_prepare(uintptr_t proprietor, struct turnstile** tstore, struct turnstile* turnstile, turnstile_type_t type) {
	contract_turnstile_prepare_calls++;
	(void)proprietor;
	(void)tstore;
	(void)turnstile;
	(void)type;
	return 0;
}

void turnstile_update_inheritor_complete(struct turnstile* turnstile, turnstile_update_complete_flags_t flags) {
	(void)turnstile;
	(void)flags;
}

void ipc_port_recv_update_inheritor(ipc_port_t port, struct turnstile* turnstile, turnstile_update_flags_t flags) {
	(void)port;
	(void)turnstile;
	(void)flags;
}

void thread_set_pending_block_hint(thread_t thread, block_hint_t block_hint) {
	(void)thread;
	(void)block_hint;
}

wait_result_t waitq_assert_wait64_locked(struct waitq* waitq, event64_t wait_event, wait_interrupt_t interruptible,
    wait_timeout_urgency_t urgency, uint64_t deadline, uint64_t leeway, thread_t thread) {
	contract_waitq_assert_calls++;
	(void)waitq;
	(void)wait_event;
	(void)interruptible;
	(void)urgency;
	(void)deadline;
	(void)leeway;
	(void)thread;
	return 0;
}

void waitq_unlock(struct waitq* wq) {
	(void)wq;
}

int waitq_is_valid(struct waitq* waitq) {
	(void)waitq;
	return 1;
}

int waitq_set_iterate_preposts(struct waitq_set* wqset, void* ctx, waitq_iterator_t it) {
	(void)wqset;
	(void)ctx;
	(void)it;
	return 0;
}

ipc_kmsg_t ipc_kmsg_queue_next(ipc_kmsg_queue_t queue, ipc_kmsg_t kmsg) {
	(void)queue;
	(void)kmsg;
	return 0;
}

void ipc_importance_receive_for_task(ipc_kmsg_t kmsg, mach_msg_option_t option, task_t task_self) {
	(void)kmsg;
	(void)option;
	contract_importance_task = (uintptr_t)task_self;
}

void ipc_importance_unreceive(ipc_kmsg_t kmsg, mach_msg_option_t option) {
	(void)kmsg;
	(void)option;
}

void ipc_importance_clean(ipc_kmsg_t kmsg) {
	(void)kmsg;
}

void ipc_voucher_receive_postprocessing_for_task(ipc_kmsg_t kmsg, mach_msg_option_t option, task_t task_self) {
	(void)kmsg;
	(void)option;
	contract_voucher_task = (uintptr_t)task_self;
}

void ipc_port_adjust_special_reply_port_locked(ipc_port_t special_reply_port, struct knote* kn, uint8_t flags, boolean_t get_turnstile) {
	(void)special_reply_port;
	(void)kn;
	(void)flags;
	(void)get_turnstile;
}

void ipc_kmsg_copyout_dest(ipc_kmsg_t kmsg, ipc_space_t space) {
	(void)kmsg;
	(void)space;
}

/* waitq/clock leaves reached via the real ipc_mqueue_release_msgcount. */
void ipc_object_validate(ipc_object_t object) {
	(void)object;
}

void waitq_lock(struct waitq* wq) {
	(void)wq;
}

unsigned int waitq_held(struct waitq* wq) {
	(void)wq;
	return 1;
}

kern_return_t waitq_wakeup64_one(struct waitq* waitq, event64_t wake_event, wait_result_t result, int priority) {
	(void)waitq;
	(void)wake_event;
	(void)result;
	(void)priority;
	return KERN_FAILURE;
}

int waitq_clear_prepost_locked(struct waitq* waitq) {
	(void)waitq;
	return 0;
}

void clock_interval_to_deadline(uint32_t interval, uint32_t scale_factor, uint64_t* result) {
	(void)interval;
	(void)scale_factor;
	if (result) {
		*result = 0;
	}
}
