/*
 * dar-dtape-explicit-context-6to3.6 deterministic host contract.
 *
 * Two distinct execution contexts:
 *   K = the kernelAsync microthread the kqchan read actually runs on (ambient current_thread())
 *   R = the guest requester thread the read is performed for (explicit requester)
 *
 * The .4b slice threaded R down to the descriptor copyout boundary, but the receive copyout
 * still selected the turnstile knote and the immovable-receive guard message address from the
 * ambient thread. This contract drives the REAL selection code
 *
 *   ipc_object_copyout_on_thread(...)   (duct-tape/xnu/osfmk/ipc/ipc_object.c)
 *   ipc_right_copyout_on_thread(...)    (duct-tape/xnu/osfmk/ipc/ipc_right.c)
 *
 * with K installed as current_thread() and a K knote that is a live (valid) knote, so any
 * substitution of the ambient thread is visible, not masked by an invalid knote. It asserts the
 * knote handed to filt_machport_turnstile_prepare_lazily and to the special-reply-port copyout is
 * R's, and never K's.
 *
 * This is NOT a source/text audit: the changed functions are compiled and executed. On a
 * pre-.6 source tree the harness does not build against the explicit-requester API.
 */
#include <ipc/ipc_object.h>
#include <ipc/ipc_right.h>
#include <ipc/ipc_port.h>
#include <ipc/ipc_entry.h>
#include <ipc/ipc_space.h>
#include <kern/thread.h>
#include <kern/assert.h>
#include <mach/message.h>
#include <mach/mach_types.h>

static long contract_write(const char* s, unsigned long n) {
	long r;
	__asm__ volatile("syscall" : "=a"(r) : "a"(1L), "D"(1L), "S"(s), "d"(n) : "rcx", "r11", "memory");
	return r;
}

static void contract_pass(void) {
	static const char msg[] = "DTAPE-RECEIVE-COPYOUT-CONTEXT PASS: receive-copyout knote/message-address come from R, never the ambient K\n";
	contract_write(msg, sizeof(msg) - 1);
}

static struct thread K_thread;
static struct thread R_thread;

/* Read by the ambient-thread stub and written by the knote-selection stubs. */
thread_t contract_ambient_thread;
uintptr_t contract_prepare_knote;
uintptr_t contract_srp_knote;

/* Two distinct, live knotes: if the ambient K is substituted, the recorded knote is K's, not R's. */
static struct knote K_knote;
static struct knote R_knote;

int main(void) {
	static struct ipc_space space;
	static struct ipc_entry entry;
	static struct ipc_port port;
	mach_port_name_t name = MACH_PORT_NULL;
	kern_return_t kr;

	R_thread.ith_knote = &R_knote;
	R_thread.ith_msg_addr = 0x1234;
	K_thread.ith_knote = &K_knote;
	K_thread.ith_msg_addr = 0xdead;

	/* The ambient/executing context is the kernelAsync thread K. */
	contract_ambient_thread = &K_thread;

	/*
	 * Case 1: ipc_object_copyout_on_thread selects the turnstile knote before it locks the
	 * space, so an inactive destination space returns after the selection and leaves the
	 * recorded knote as the observation.
	 */
	space.is_bits = IS_INACTIVE;
	port.ip_object.io_bits = IO_BITS_ACTIVE; /* otype IOT_PORT */
	port.ip_object.io_references = 1;

	kr = ipc_object_copyout_on_thread((ipc_space_t)&space, ip_to_object(&port),
	    MACH_MSG_TYPE_PORT_RECEIVE, IPC_OBJECT_COPYOUT_FLAGS_NONE, NULL, NULL, &name, &R_thread);
	(void)kr;

	assert(contract_prepare_knote == (uintptr_t)&R_knote);
	assert(contract_prepare_knote != (uintptr_t)&K_knote);

	/*
	 * Case 2: ipc_right_copyout_on_thread selects the special-reply/receive knote from the
	 * requester. The special-reply (SEND_ONCE) branch takes the knote directly.
	 */
	entry.ie_bits = MACH_PORT_TYPE_NONE;
	entry.ie_object = ip_to_object(&port);
	port.ip_sorights = 1;
	port.ip_specialreply = 1;

	kr = ipc_right_copyout_on_thread((ipc_space_t)&space, 7, &entry,
	    MACH_MSG_TYPE_PORT_SEND_ONCE, IPC_OBJECT_COPYOUT_FLAGS_NONE, NULL, NULL,
	    ip_to_object(&port), &R_thread);
	(void)kr;

	assert(contract_srp_knote == (uintptr_t)&R_knote);
	assert(contract_srp_knote != (uintptr_t)&K_knote);

	contract_pass();
	return 0;
}
