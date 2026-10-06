/*
 * dar-dtape-explicit-context-6to3.5 deterministic host contract.
 *
 * Two distinct execution contexts:
 *   K = the kernelAsync microthread the read actually runs on (ambient thread)
 *   R = the guest requester thread the read is performed for (explicit requester)
 *
 * The contract drives the real product path
 *
 *   dtape_kqchan_mach_port_fill(kqchan, R, ...)       (duct-tape/src/kqchan.c)
 *     -> filt_machportprocess_on_thread(kn, kev, R)   (duct-tape/xnu ipc_pset.c)
 *          -> ipc_mqueue_receive_on_thread(..., R)
 *               -> ipc_mqueue_select_on_thread(...)   (duct-tape/xnu ipc_mqueue.c)
 *          -> mach_msg_receive_results_on_thread(&size, R) (duct-tape/xnu mach_msg.c)
 *
 * and asserts every piece of semantic state lands on R, never K:
 *   kevent_ctx, ith_*, the receive task's messages_received, and the task/space/map
 *   used for copyout. K is installed as the ambient thread (current_thread()) with
 *   poisoned state; any stage that substituted the ambient thread fails the asserts.
 *
 * This is a kernel-flavoured host build (no libc): it uses the kernel assert and a raw
 * write syscall, because the XNU headers deliberately shadow the host libc types.
 */
#include <darlingserver/duct-tape.h>
#include <darlingserver/duct-tape/thread.h>
#include <darlingserver/duct-tape/kqchan.h>
#include <darlingserver/duct-tape/task.h>

#include <ipc/ipc_mqueue.h>
#include <ipc/ipc_port.h>
#include <ipc/ipc_kmsg.h>
#include <ipc/ipc_space.h>
#include <kern/thread.h>
#include <kern/task.h>
#include <kern/assert.h>
#include <vm/vm_map.h>
#include <sys/event.h>

#define K_ITH_STATE 0xdead0000u
#define K_MSG_RECEIVED 100
#define R_MSG_RECEIVED 0

static struct task K_task;
static struct task R_task;

/* Opaque unique storage for the (large/incomplete) space/map/turnstile pointers. */
static char K_space_s[16];
static char R_space_s[16];
static char K_map_s[16];
static char R_map_s[16];
static char K_ts_s[16];
static char R_ts_s[16];

static dtape_thread_t K_thread;
static dtape_thread_t R_thread;

/* Read by the ambient-thread stub. */
thread_t contract_ambient_thread = 0;
int contract_ambient_calls = 0;

/* Recorded by the stubs. */
uintptr_t contract_copyout_size_map = 0;
uintptr_t contract_copyout_space = 0;
uintptr_t contract_trailer_thread = 0;
uintptr_t contract_add_trailer_thread = 0;
uintptr_t contract_put_thread = 0;
uintptr_t contract_put_task = 0;
uintptr_t contract_put_map = 0;
uintptr_t contract_importance_task = 0;
uintptr_t contract_voucher_task = 0;

static long contract_write(const char* s, unsigned long n) {
	long r;
	__asm__ volatile("syscall" : "=a"(r) : "a"(1L), "D"(1L), "S"(s), "d"(n) : "rcx", "r11", "memory");
	return r;
}

static void contract_pass(void) {
	static const char msg[] = "DTAPE-KQCHAN-FILL-CONTEXT PASS: R semantic state (kevent_ctx/ith_*/messages_received/map/space) with K != R\n";
	contract_write(msg, sizeof(msg) - 1);
}

int main(void) {
	static struct ipc_port port;
	static struct ipc_kmsg kmsg;
	static mach_msg_header_t hdr;
	static struct dtape_kqchan_mach_port kqchan;
	static unsigned char buffer[1024];
	static dserver_kqchan_reply_mach_port_read_t reply;
	int result;

	K_thread.xnu_thread.task = &K_task;
	K_thread.xnu_thread.map = (vm_map_t)K_map_s;
	K_thread.xnu_thread.turnstile = (struct turnstile*)K_ts_s;

	R_thread.xnu_thread.task = &R_task;
	R_thread.xnu_thread.map = (vm_map_t)R_map_s;
	R_thread.xnu_thread.turnstile = (struct turnstile*)R_ts_s;

	K_task.itk_space = (ipc_space_t)K_space_s;
	R_task.itk_space = (ipc_space_t)R_space_s;
	K_task.messages_received = K_MSG_RECEIVED;
	R_task.messages_received = R_MSG_RECEIVED;

	/* The ambient/executing context is the kernelAsync thread K. */
	contract_ambient_thread = &K_thread.xnu_thread;

	/* Poison K's receive state so any ambient substitution is visible. */
	K_thread.xnu_thread.ith_state = K_ITH_STATE;

	/* --- the port/mqueue + one queued message --- */
	port.ip_object.io_bits = IO_BITS_ACTIVE; /* otype IOT_PORT */
	port.ip_object.io_references = 1;
	port.ip_messages.data.port.waitq.waitq_type = WQT_QUEUE;
	port.ip_messages.data.port.waitq.waitq_isvalid = 1;
	port.ip_messages.data.port.messages.ikmq_base = &kmsg;
	port.ip_messages.data.port.seqno = 1;
	port.ip_messages.data.port.receiver_name = 42;
	port.ip_context = 0x1234;

	hdr.msgh_remote_port = &port;
	hdr.msgh_local_port = MACH_PORT_NULL;
	hdr.msgh_size = 32;
	hdr.msgh_bits = 0;
	kmsg.ikm_header = &hdr;
	kmsg.ikm_size = 32;
	kmsg.ikm_ppriority = 7;
	kmsg.ikm_qos_override = 3;

	/* --- the kqchan knote over that mqueue --- */
	kqchan.knote.kn_mqueue = &port.ip_messages;
	kqchan.knote.kn_id = 5;
	kqchan.knote.kn_filter = EVFILT_MACHPORT;
	kqchan.knote.kn_sfflags = MACH_RCV_MSG;
	kqchan.knote.kn_ext[0] = 0;
	kqchan.knote.kn_ext[1] = 0; /* let the filter carve R's kevent_ctx buffer */
	kqchan.waiter_read_semaphore = 0;

	/* --- drive the explicit path with R as requester --- */
	result = dtape_kqchan_mach_port_fill(&kqchan, &R_thread, &reply, (uint64_t)(uintptr_t)buffer, sizeof(buffer));

	assert(result == true);

	/* kevent_ctx belongs to R and was carved by the fill. */
	assert(R_thread.kevent_ctx.kec_data_out != 0);
	assert(R_thread.kevent_ctx.kec_data_resid < sizeof(buffer));

	/* the received state landed on R, never on the ambient K. */
	assert(R_thread.xnu_thread.ith_state == MACH_MSG_SUCCESS);
	assert(R_thread.xnu_thread.ith_seqno == 1);
	/* on a successful receive the kmsg slot is unioned with the received QoS that
	 * mach_msg_receive_results_on_thread saves back onto the requester. */
	assert(R_thread.xnu_thread.ith_ppriority == 7);
	assert(R_thread.xnu_thread.ith_qos_override == 3);
	assert(K_thread.xnu_thread.ith_state == K_ITH_STATE);
	assert(K_thread.xnu_thread.ith_ppriority == 0);

	/* messages_received is charged to R's task. */
	assert(R_task.messages_received == R_MSG_RECEIVED + 1);
	assert(K_task.messages_received == K_MSG_RECEIVED);

	/* the copyout context came from R. */
	assert(contract_copyout_size_map == (uintptr_t)R_map_s);
	assert(contract_copyout_space == (uintptr_t)R_space_s);
	assert(contract_trailer_thread == (uintptr_t)&R_thread.xnu_thread);
	assert(contract_add_trailer_thread == (uintptr_t)&R_thread.xnu_thread);

	/* the message copyout boundary was handed the requester R, and its task/map are R's. */
	assert(contract_put_thread == (uintptr_t)&R_thread.xnu_thread);
	assert(contract_put_task == (uintptr_t)&R_task);
	assert(contract_put_map == (uintptr_t)R_map_s);

	/* importance/voucher postprocessing act on R's task, not the kernel worker's. */
	assert(contract_importance_task == (uintptr_t)&R_task);
	assert(contract_voucher_task == (uintptr_t)&R_task);

	contract_pass();
	return 0;
}
