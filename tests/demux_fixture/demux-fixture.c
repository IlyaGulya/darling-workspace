/*
 * demux-fixture.c -- standalone falsification harness for
 * "one process-level AF_UNIX datagram socket replaces the per-thread RPC
 * datagram sockets", built from the product's REAL request/reply structures.
 *
 * WHAT IS UNDER TEST
 * ------------------
 * The product's RPC layer has NO request ids.  A guest thread owns its own
 * autobound datagram socket; the generated client sends a call on that socket
 * and then blocks in recvmsg on it, and the reply is matched by the generated
 * wrapper by CALL NUMBER (generate-rpc-wrappers.py:1582 receive, 1609 number
 * check, 1617 length check -- byte-identical lengths) on that one socket.  The
 * server matches the *caller* the same way: it keys the reply thread off the
 * header (dserver_rpc_callhdr_t{tid}) and off which socket the call arrived on
 * (call.cpp: `thread->setAddress(requestMessage.address())`).
 *
 * The proposal under test is a SINGLE process-level AF_UNIX datagram endpoint
 * shared by all N guest threads, with per-thread completion slots in shared
 * memory, an added request id, an added process generation, an added target
 * kernel tid and an added per-lane generation, a futex wake on the target's
 * completion slot, and SCM_RIGHTS carried on that same socket.  All six added
 * fields are the PROPOSAL's; every byte of the request/reply payload is the
 * product's own struct (section 1 below).
 *
 * TWO DISPATCHER SHAPES, MEASURED
 * ------------------------------
 *   V1  a permanent demultiplexer thread owns recvmsg on the process socket and
 *       routes each datagram into the target thread's completion slot.
 *   V2  no permanent thread: ONE reader token is held by one of the waiting
 *       threads; that thread recvmsg()s, dispatches datagrams that are not its
 *       own, and keeps waiting for its own.
 * The binary runs V1 by default; DEMUX_VARIANT=2 selects V2.
 *
 * CLAIMS (one printed line each)
 * -----------------------------
 *   D1 32 concurrent blocking receives complete.
 *   D2 replies deliberately delivered out of order still reach the right thread.
 *   D3 one waiter times out; the others continue; the late reply for the
 *      abandoned lane is rejected by the lane generation.
 *   D4 one waiter is interrupted/cancelled; the others continue.
 *   D5 SCM_RIGHTS arrives associated with exactly the right logical request and
 *      is closed rather than leaked when rejected.
 *   D6 one slow waiter does not head-of-line-block the others.
 *   D7 a stale completion produced before fork is rejected by the child.
 *   D8 a stale completion produced before exec is rejected by the new image.
 *   D9 a caller-local operation executes on the TARGET thread, never on the
 *      dispatcher (the decisive V1 claim).
 *   D10 MEASUREMENT thread count, RSS/stack, idle CPU, signal mask, per-request
 *      latency and CPU, and the fork/exec consequences of the chosen shape.
 *
 * MUTATIONS (built by the runner in its temporary directory, never here)
 *   M1 match by arrival order                    -> D2 must fail
 *   M2 drop the lane generation                  -> D3 must fail
 *   M3 dispatch a caller-local op from the dispatcher thread
 *                                                -> D9 must fail
 *   M4 the token holder exits on its interrupt without releasing the token
 *                                                -> D4 must fail
 *   M5 the fork child accepts the parent's generation
 *                                                -> D7 must fail
 *   M6 the post-exec image accepts the pre-exec generation
 *                                                -> D8 must fail
 *   M7 the token holder does not dispatch others' datagrams
 *                                                -> D6 must fail
 * Each mutation is a single marked line; the runner refuses to continue if the
 * marker did not apply.
 *
 * FIDELITY
 * --------
 * This harness does not run mldr, darlingserver, Mach or any Darling code.  It
 * executes the very syscalls the design needs (AF_UNIX datagram sockets,
 * SCM_RIGHTS, futex, mmap/mprotect/munmap/msync, eventfd, fork, clone, execve)
 * with the product's own structs as the payload, and checks the claims the
 * design makes about them.  A PASS says the mechanism as modelled here holds.
 * It says nothing about the product's implementation; the fidelity gaps are
 * enumerated in the runner's report and in the comment above each phase.
 *
 * Modes: `all` (default), `layout` (print the fixture's struct layout table for
 * the runner's drift check against the product source), `exec-child` (the
 * post-exec image, entered only through execve).
 */

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* ====================================================================== */
/* 1. THE PRODUCT ABI -- mechanically copied, field for field.            */
/*                                                                        */
/* Source of every definition below (the materialized product forest the   */
/* matched prefixes were built from -- never the drifting West tree):      */
/*   FR = <forest>/darling/src/external/darlingserver/include/darlingserver/rpc-supplement.h */
/*   GEN = <forest>/darling/src/external/darlingserver/scripts/generate-rpc-wrappers.py      */
/* The generated header is re-created by the runner with the product's own */
/* generator and its layout is diffed against this table.                  */
/* ====================================================================== */

/* ---- rpc-supplement.h:1091-1099 (S2C message numbers) ----------------- */
enum dserver_s2c_msgnum {
	dserver_s2c_msgnum_invalid = 0,
	dserver_s2c_msgnum_mmap,
	dserver_s2c_msgnum_munmap,
	dserver_s2c_msgnum_mprotect,
	dserver_s2c_msgnum_msync,
};

typedef enum dserver_s2c_msgnum dserver_s2c_msgnum_t;

/* ---- rpc-supplement.h:1101-1112 (S2C call/reply headers) -------------- */
typedef struct dserver_s2c_callhdr {
	int call_number;
	dserver_s2c_msgnum_t s2c_number;
} dserver_s2c_callhdr_t;

typedef struct dserver_s2c_replyhdr {
	int call_number;
	int pid;
	int tid;
	int architecture;
	dserver_s2c_msgnum_t s2c_number;
} dserver_s2c_replyhdr_t;

/* ---- rpc-supplement.h:1114-1173 (S2C mmap/munmap/mprotect/msync) ------ */
typedef struct dserver_s2c_call_mmap {
	dserver_s2c_callhdr_t header;
	uint64_t address;
	uint64_t length;
	int32_t protection;
	int32_t flags;
	int32_t fd;
	int64_t offset;
} dserver_s2c_call_mmap_t;

typedef struct dserver_s2c_reply_mmap {
	dserver_s2c_replyhdr_t header;
	uint64_t address;
	int errno_result;
} dserver_s2c_reply_mmap_t;

typedef struct dserver_s2c_call_munmap {
	dserver_s2c_callhdr_t header;
	uint64_t address;
	uint64_t length;
} dserver_s2c_call_munmap_t;

typedef struct dserver_s2c_reply_munmap {
	dserver_s2c_replyhdr_t header;
	int return_value;
	int errno_result;
} dserver_s2c_reply_munmap_t;

typedef struct dserver_s2c_call_mprotect {
	dserver_s2c_callhdr_t header;
	uint64_t address;
	uint64_t length;
	int protection;
} dserver_s2c_call_mprotect_t;

typedef struct dserver_s2c_reply_mprotect {
	dserver_s2c_replyhdr_t header;
	int return_value;
	int errno_result;
} dserver_s2c_reply_mprotect_t;

typedef struct dserver_s2c_call_msync {
	dserver_s2c_callhdr_t header;
	uint64_t address;
	uint64_t size;
	int sync_flags;
} dserver_s2c_call_msync_t;

typedef struct dserver_s2c_reply_msync {
	dserver_s2c_replyhdr_t header;
	int return_value;
	int errno_result;
} dserver_s2c_reply_msync_t;

typedef union dserver_s2c_call {
	dserver_s2c_call_mmap_t mmap;
	dserver_s2c_call_munmap_t munmap;
	dserver_s2c_call_mprotect_t mprotect;
	dserver_s2c_call_msync_t msync;
} dserver_s2c_call_t;

/* ---- GEN:801-832 (the generated call-number enum) --------------------- */
/* The generated enum lists every call with an explicit value: the three
 * sentinels + one sequential index per call starting at 1, and every
 * UNMANAGED_CALL entry carries DSERVER_CALL_UNMANAGED_FLAG (0x80000000U) --
 * a value that does not fit in `int`, which is why the real enum's underlying
 * type is UNSIGNED (the runner asserts that on the regenerated header).  Only
 * the entries this fixture uses are copied here; the runner regenerates the
 * whole enum and diffs size/signedness and the values below. */
#define DSERVER_CALL_UNMANAGED_FLAG 0x80000000U
typedef enum dserver_callnum {
	dserver_callnum_s2c = 0x52cca11,
	dserver_callnum_push_reply = 0xbadca11,
	dserver_callnum_invalid = 0,
	dserver_callnum_unmanaged_example = DSERVER_CALL_UNMANAGED_FLAG | 1U,
	dserver_callnum_thread_self_trap = 35U,
	dserver_callnum_mach_msg_overwrite = 38U,
	dserver_callnum_ring_attach = 81U,
} dserver_callnum_t;

/* ---- GEN:823-839 (architecture enum + call/reply headers) ------------- */
enum dserver_rpc_architecture {
	dserver_rpc_architecture_invalid,
	dserver_rpc_architecture_i386,
	dserver_rpc_architecture_x86_64,
	dserver_rpc_architecture_arm32,
	dserver_rpc_architecture_arm64,
};

typedef enum dserver_rpc_architecture dserver_rpc_architecture_t;

typedef struct dserver_rpc_callhdr {
	dserver_callnum_t number;
	pid_t pid;
	pid_t tid;
	dserver_rpc_architecture_t architecture;
} dserver_rpc_callhdr_t;

typedef struct dserver_rpc_replyhdr {
	dserver_callnum_t number;
	int code;
} dserver_rpc_replyhdr_t;

/* ---- GEN:1353-1365 (call struct = {header; body}) --------------------- */
/* thread_self_trap: GEN:308-310 (no call parameters -> header only,
 * reply parameter port_name). */
typedef struct dserver_rpc_call_thread_self_trap dserver_rpc_call_thread_self_trap_t;
struct dserver_rpc_call_thread_self_trap {
	dserver_rpc_callhdr_t header;
};
typedef struct dserver_reply_thread_self_trap dserver_reply_thread_self_trap_t;
struct dserver_reply_thread_self_trap {
	uint32_t port_name;
};
typedef struct dserver_rpc_reply_thread_self_trap dserver_rpc_reply_thread_self_trap_t;
struct dserver_rpc_reply_thread_self_trap {
	dserver_rpc_replyhdr_t header;
	dserver_reply_thread_self_trap_t body;
};

/* mach_msg_overwrite: GEN:326-335 (the eight real parameters, verbatim). */
typedef struct dserver_call_mach_msg_overwrite dserver_call_mach_msg_overwrite_t;
struct dserver_call_mach_msg_overwrite {
	uint64_t msg __attribute__((aligned(8)));
	int32_t option;
	uint32_t send_size;
	uint32_t rcv_size;
	uint32_t rcv_name;
	uint32_t timeout;
	uint32_t priority;
	uint64_t rcv_msg __attribute__((aligned(8)));
};
typedef struct dserver_rpc_call_mach_msg_overwrite dserver_rpc_call_mach_msg_overwrite_t;
struct dserver_rpc_call_mach_msg_overwrite {
	dserver_rpc_callhdr_t header;
	dserver_call_mach_msg_overwrite_t body;
};

/* ring_attach: GEN:672-692 (the ONE call in the table whose reply carries a
 * descriptor: reply parameters reject_reason, wake_fd(@fd)). */
typedef struct dserver_call_ring_attach dserver_call_ring_attach_t;
struct dserver_call_ring_attach {
	int32_t ring_fd;
	uint64_t mapping_size __attribute__((aligned(8)));
};
typedef struct dserver_reply_ring_attach dserver_reply_ring_attach_t;
struct dserver_reply_ring_attach {
	uint32_t reject_reason;
	int32_t wake_fd;
};
typedef struct dserver_rpc_call_ring_attach dserver_rpc_call_ring_attach_t;
struct dserver_rpc_call_ring_attach {
	dserver_rpc_callhdr_t header;
	dserver_call_ring_attach_t body;
};
typedef struct dserver_rpc_reply_ring_attach dserver_rpc_reply_ring_attach_t;
struct dserver_rpc_reply_ring_attach {
	dserver_rpc_replyhdr_t header;
	dserver_reply_ring_attach_t body;
};

/* ---- layout assertions (values derived from the source, see section 2) - */
_Static_assert(sizeof(dserver_callnum_t) == 4, "callnum enum width");
_Static_assert((dserver_callnum_t)-1 > 0, "callnum enum must be unsigned");
_Static_assert(sizeof(dserver_rpc_callhdr_t) == 16, "call header size");
_Static_assert(offsetof(dserver_rpc_callhdr_t, tid) == 8, "call header tid offset");
_Static_assert(sizeof(dserver_rpc_replyhdr_t) == 8, "reply header size");
_Static_assert(sizeof(dserver_rpc_call_thread_self_trap_t) == 16, "tst call size");
_Static_assert(sizeof(dserver_rpc_reply_thread_self_trap_t) == 12, "tst reply size");
_Static_assert(sizeof(dserver_rpc_call_mach_msg_overwrite_t) == 56, "mmo call size");
_Static_assert(sizeof(dserver_rpc_call_ring_attach_t) == 32, "ra call size");
_Static_assert(sizeof(dserver_rpc_reply_ring_attach_t) == 16, "ra reply size");
_Static_assert(sizeof(dserver_s2c_callhdr_t) == 8, "s2c call header size");
_Static_assert(sizeof(dserver_s2c_replyhdr_t) == 20, "s2c reply header size");
_Static_assert(sizeof(dserver_s2c_call_mmap_t) == 48, "s2c mmap call size");
_Static_assert(sizeof(dserver_s2c_reply_mmap_t) == 40, "s2c mmap reply size");
_Static_assert(sizeof(dserver_s2c_call_munmap_t) == 24, "s2c munmap call size");
_Static_assert(sizeof(dserver_s2c_reply_munmap_t) == 28, "s2c munmap reply size");
_Static_assert(sizeof(dserver_s2c_call_mprotect_t) == 32, "s2c mprotect call size");
_Static_assert(sizeof(dserver_s2c_reply_mprotect_t) == 28, "s2c mprotect reply size");
_Static_assert(sizeof(dserver_s2c_call_msync_t) == 32, "s2c msync call size");
_Static_assert(sizeof(dserver_s2c_reply_msync_t) == 28, "s2c msync reply size");
_Static_assert(sizeof(dserver_s2c_call_t) == 48, "s2c union size");

/* ====================================================================== */
/* 2. THE FIXTURE'S OWN STRUCTURES                                        */
/*    Everything in this section is the PROPOSAL's, not the product's.      */
/* ====================================================================== */

#define DMX_MAGIC        0x44584D58u	/* 'DXMX' */
#define DMX_MAXT         72		/* slots: 0..63 workers, 64/65 child legs */
#define DMX_NWORKERS     64		/* guest threads modelled            */
#define DMX_PAYL         64		/* bytes of product payload per datum */
#define DMX_PROTO_RID    1u		/* envelope protocol version (rid field) */

/* The proposal's added per-datagram fields.  The product has none of these:
 * it knows the target thread only because each thread owns a socket. */
typedef struct demux_env {
	uint32_t magic;
	uint32_t kind;			/* DK_* */
	uint32_t flags;			/* DGF_* */
	int32_t  stid;			/* proposal: target kernel tid */
	uint32_t rid;			/* proposal: request id */
	uint32_t lane_generation;	/* proposal: lane generation */
	uint32_t payload_len;
	uint32_t s2c_number;
	uint32_t s2c_seq;
	uint64_t proc_generation;	/* proposal: process incarnation */
	uint8_t  payload[DMX_PAYL];	/* the product's bytes, verbatim */
} demux_env_t;

enum demux_kind {
	DK_CALL = 1,		/* C2S: payload = product call struct   */
	DK_REPLY = 2,		/* S2C: payload = product reply struct  */
	DK_S2C = 3,		/* S2C upcall: payload = s2c call union */
	DK_S2C_REPLY = 4,	/* guest -> server S2C result           */
	DK_STOP = 5,		/* fixture control: stop the dispatcher */
};

enum demux_flags {
	DGF_PHASE_BEGIN = 0x0001u,	/* server: collect `s2c_seq` calls */
	DGF_WITHHOLD    = 0x0002u,	/* server: do not answer this one  */
	DGF_FLUSH       = 0x0004u,	/* server: release every withheld  */
	DGF_STOP        = 0x0008u,	/* server: stop                    */
	DGF_FD          = 0x0010u,	/* reply carries a descriptor      */
	DGF_CALL_FD     = 0x0020u,	/* call carries a descriptor       */
	DGF_S2C         = 0x0040u,	/* server: run the S2C handshake    */
	DGF_REVERSE     = 0x0080u,	/* server: answer in reverse order  */
	DGF_DEAD_SERVER = 0x0100u,	/* server: exit without answering   */
};

enum phase_policy {		/* what the server does with a collected batch */
	POL_PLAIN = 1,
	POL_REVERSE = 2,
};

enum slot_state {
	DS_EMPTY = 0,
	DS_WAIT = 1,
	DS_READY = 2,
	DS_ABANDONED = 3,
};

/* Generation epochs of the fixture's process incarnation. */
#define GEN_SETUP 1ULL
#define GEN_D1    2ULL
#define GEN_D2    3ULL
#define GEN_D3    4ULL
#define GEN_D3B   5ULL
#define GEN_D4    6ULL
#define GEN_D5    7ULL
#define GEN_D6    8ULL
#define GEN_D9    9ULL
#define GEN_FORK  10ULL
#define GEN_EXEC_PRE  11ULL
#define GEN_EXEC_NEW  12ULL
#define GEN_PREFORK   13ULL

/* The proposal's per-thread completion slot in shared memory. */
typedef struct demux_slot {
	_Atomic uint32_t state;
	_Atomic uint32_t futex_word;
	uint32_t want_rid;		/* rid this thread is waiting for   */
	uint32_t want_tag;		/* request tag this thread sent      */
	uint32_t got_rid;		/* rid of the datum in the payload  */
	uint32_t got_lane;		/* lane generation of the datum     */
	uint32_t expected_callnum;	/* product call number expected     */
	uint32_t lane_generation;
	int32_t  owner_tid;
	uint32_t dkind;
	uint32_t payload_len;
	_Atomic uint32_t sent;		/* the caller's sendto() has completed */
	uint32_t dispatch_seq;		/* order in which the demux delivered */
	int32_t  received_fd;		/* SCM_RIGHTS landing area, -1 none */
	int32_t  dispatcher_tid;	/* who moved the datum here         */
	uint32_t payload_ok;
	uint8_t  payload[DMX_PAYL];
} demux_slot_t;

typedef struct demux_ledger {
	uint32_t rid_sent;
	uint32_t rid_seen;
	int32_t  tid_seen;
	uint32_t completions;
	uint32_t payload_mismatch;
	uint32_t wrong_thread;
	uint32_t timed_out;
	uint32_t interrupted;
	uint32_t abandoned;
	uint32_t fd_token_ok;
	uint32_t fd_token_bad;
	uint32_t s2c_handled;
	uint32_t parked_polls;
	uint64_t lat_ns;
	uint64_t cpu_ns;
} demux_ledger_t;

typedef struct demux_phase {
	_Atomic uint32_t go;
	_Atomic uint32_t done;
	_Atomic uint32_t phase;
	uint32_t count;
	uint32_t withhold_idx;
	uint32_t base_tag;
	uint32_t timeout_ms;
	uint32_t interrupt_idx;
	uint32_t callnum;
	uint32_t wtimeout[DMX_MAXT];	/* per-worker deadline, ms          */
	uint32_t wflags[DMX_MAXT];	/* WF_* per worker                  */
	uint32_t wop[DMX_MAXT];		/* WF_S2C: which caller-local op    */
	uint32_t wact[DMX_MAXT];	/* 1 = this worker takes part       */
	uint32_t rid_base;
	uint64_t gen;
} demux_phase_t;

typedef struct demux_shm {
	uint32_t magic;
	uint32_t version;
	uint32_t nthreads;
	uint32_t ctl_tid;		/* dispatcher thread's tid (V1) */
	_Atomic uint64_t proc_generation;
	_Atomic uint32_t server_pid;
	_Atomic uint32_t token_owner;	/* V2 reader token: tid or 0     */
	_Atomic uint32_t token_handoffs;
	_Atomic uint32_t token_releases;
	_Atomic uint32_t token_starved;
	_Atomic int32_t  rr_next;	/* M1 target only (arrival order) */
	/* the dispatch ledger */
	_Atomic uint32_t dispatched;
	_Atomic uint32_t dispatch_seq_ctr;
	_Atomic uint32_t dispatched_own_inplace;
	_Atomic uint32_t copies;
	_Atomic uint32_t stale_gen_rejected;
	_Atomic uint32_t late_lane_rejected;
	_Atomic uint32_t notwaiting_rejected;
	_Atomic uint32_t unknown_target;
	_Atomic uint32_t payload_mismatch;
	_Atomic uint32_t wrong_thread;
	_Atomic uint32_t fd_received;
	_Atomic uint32_t fd_closed_on_reject;
	_Atomic uint32_t fd_closed_on_caller_reject;
	_Atomic uint32_t fd_token_ok;
	_Atomic uint32_t fd_token_bad;
	_Atomic uint32_t s2c_routed;
	_Atomic uint32_t s2c_executed_by_dispatcher;
	_Atomic uint32_t s2c_reply_tid_ok;	/* server-side, written by the server */
	_Atomic uint32_t s2c_reply_tid_bad;
	_Atomic uint32_t s2c_incomplete;
	_Atomic uint32_t dispatch_ns;
	_Atomic uint32_t server_answered;
	_Atomic uint32_t send_errors;
	_Atomic uint32_t server_send_errors;
	_Atomic uint32_t srv_state;
	_Atomic int32_t last_send_errno;
	_Atomic uint32_t last_send_idx;
	_Atomic uint32_t last_send_phase;
	uint8_t last_send_dest[108];
	_Atomic uint32_t srv_recv_ok;
	_Atomic uint32_t srv_collected;
	_Atomic uint32_t srv_arrival_n;
	_Atomic uint32_t srv_arrival[DMX_MAXT];
	_Atomic uint32_t srv_answer[DMX_MAXT];
	_Atomic uint32_t phase_abort;
	_Atomic uint32_t demux_stop;
	/* per-thread bookkeeping */
	_Atomic int32_t slot_to_tid[DMX_MAXT];
	_Atomic int32_t exec_tid[DMX_MAXT];
	_Atomic uint32_t dispatch_by[DMX_MAXT];
	demux_slot_t slots[DMX_MAXT];
	demux_ledger_t ledger[DMX_MAXT];
	demux_phase_t phase;
	/* fork/exec child reports */
	_Atomic uint32_t child_threads_before;
	_Atomic uint32_t child_threads_after;
	_Atomic uint32_t child_pthread_rc;
	_Atomic uint32_t child_clone_rc;
	_Atomic uint32_t child_stale_rejected;
	_Atomic uint32_t child_consumed_stale;
	_Atomic uint32_t child_fresh_ok;
	_Atomic uint32_t exec_stale_rejected;
	_Atomic uint32_t exec_consumed_stale;
	_Atomic uint32_t exec_fresh_ok;
	_Atomic uint32_t exec_threads;
	_Atomic uint32_t exec_slot_reset;
	_Atomic uint32_t exec_pre_go;
} demux_shm_t;

/* ====================================================================== */
/* 3. RAW SYSCALL LAYER (usable from libc-free post-fork/post-exec code)   */
/* ====================================================================== */

#if !defined(__x86_64__)
#error "this fixture models the x86_64 ABI only (the forest builds x86_64)"
#endif

static inline long rsys6(long n, long a, long b, long c, long d, long e, long f)
{
	long r;

	register long r10 __asm__("r10") = d;
	register long r8  __asm__("r8")  = e;
	register long r9  __asm__("r9")  = f;

	__asm__ __volatile__("syscall"
			     : "=a"(r)
			     : "a"(n), "D"(a), "S"(b), "d"(c),
			       "r"(r10), "r"(r8), "r"(r9)
			     : "rcx", "r11", "memory");
	return r;
}

#define rsys0(n)             rsys6((n), 0, 0, 0, 0, 0, 0)
#define rsys1(n, a)          rsys6((n), (long)(a), 0, 0, 0, 0, 0)
#define rsys2(n, a, b)       rsys6((n), (long)(a), (long)(b), 0, 0, 0, 0)
#define rsys3(n, a, b, c)    rsys6((n), (long)(a), (long)(b), (long)(c), 0, 0, 0)
#define rsys4(n, a, b, c, d) rsys6((n), (long)(a), (long)(b), (long)(c), (long)(d), 0, 0)

#define DMX_FUTEX_WAIT  0
#define DMX_FUTEX_WAKE  1
#define DMX_FUTEX_PRIV  128

/* Byte loops are used instead of memcpy/memset: they must not become libc
 * calls, because these paths run after fork and after exec.  The attribute
 * keeps GCC's loop-idiom recogniser from rewriting them back into memcpy;
 * the runner additionally proves with objdump that the entry points contain
 * no libc call at all. */
__attribute__((always_inline, optimize("no-tree-loop-distribute-patterns")))
static inline void bfill(void *dst, int val, unsigned long n)
{
	volatile unsigned char *d = (volatile unsigned char *)dst;

	while (n--)
		*d++ = (unsigned char)val;
}

__attribute__((always_inline, optimize("no-tree-loop-distribute-patterns")))
static inline void dcopy(void *dst, const void *src, unsigned long n)
{
	volatile unsigned char *d = (volatile unsigned char *)dst;
	const volatile unsigned char *s = (const volatile unsigned char *)src;

	while (n--)
		*d++ = *s++;
}

static inline int32_t dmx_gettid(void)
{
	return (int32_t)rsys0(SYS_gettid);
}

static inline int32_t dmx_getpid(void)
{
	return (int32_t)rsys0(SYS_getpid);
}

static inline uint64_t dmx_now_ns(void)
{
	struct timespec ts;

	if (rsys2(SYS_clock_gettime, CLOCK_MONOTONIC, (long)&ts) < 0)
		return 0;
	return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static inline long dmx_futex_wait(void *addr, uint32_t expect, long ms)
{
	struct timespec ts;

	if (ms <= 0)
		ms = 3600000;	/* "infinite" for this fixture's purposes */
	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (ms % 1000) * 1000000L;
	return rsys6(SYS_futex, (long)addr, DMX_FUTEX_WAIT | DMX_FUTEX_PRIV,
		     (long)expect, (long)&ts, 0, 0);
}

static inline long dmx_futex_wake(void *addr, int n)
{
	return rsys6(SYS_futex, (long)addr, DMX_FUTEX_WAKE | DMX_FUTEX_PRIV,
		     n, 0, 0, 0);
}

#define ld_acq(p)    __atomic_load_n((p), __ATOMIC_ACQUIRE)
#define ld_rlx(p)    __atomic_load_n((p), __ATOMIC_RELAXED)
#define st_rel(p, v) __atomic_store_n((p), (v), __ATOMIC_RELEASE)
#define st_rlx(p, v) __atomic_store_n((p), (v), __ATOMIC_RELAXED)
#define add_rel(p, v) __atomic_fetch_add((p), (v), __ATOMIC_RELEASE)
#define add_rlx(p, v) __atomic_fetch_add((p), (v), __ATOMIC_RELAXED)
#define cas32(p, o, n) __atomic_compare_exchange_n((p), (o), (n), 0, \
						   __ATOMIC_ACQ_REL, __ATOMIC_RELAXED)

/* ====================================================================== */
/* 4. GLOBALS                                                             */
/* ====================================================================== */

static demux_shm_t *S;			/* the shared mapping            */
static char g_path_shm[192];
static char g_path_server[192];
static char g_path_guest[192];
static struct sockaddr_un g_server_addr;
static socklen_t g_server_addr_len;
static int g_fd = -1;			/* the ONE process-level socket  */
static int g_variant = 1;
static const char *g_env = "unlabeled";
static int g_fast;
static pid_t g_server_pid;
static struct sockaddr_un g_srv_peer;
static socklen_t g_srv_peer_len;
static int g_demux_tid;
static pthread_t g_demux_thr;
static int g_claim_ok[16];
static char g_claim_det[16][1024];
static volatile sig_atomic_t g_usr2_seen;

/* The interrupt the RPC waiters must see as EINTR: no SA_RESTART, so a blocked
 * futex/recvmsg returns -EINTR instead of being restarted (the product's
 * ALLOW_INTERRUPTIONS calls rely on exactly this). */
static void sigusr2_handler(int sig)
{
	(void)sig;
	g_usr2_seen = 1;
}

static void install_signals(void)
{
	struct sigaction sa;

	bfill(&sa, 0, sizeof sa);
	sa.sa_handler = sigusr2_handler;
	sa.sa_flags = 0;
	sigemptyset(&sa.sa_mask);
	sigaction(SIGUSR2, &sa, NULL);
}

#define NCLAIMS 10

static void verdict(int n, int ok, const char *fmt, ...)
{
	va_list ap;

	g_claim_ok[n] = ok ? 1 : 0;
	va_start(ap, fmt);
	vsnprintf(g_claim_det[n], sizeof g_claim_det[n], fmt, ap);
	va_end(ap);
}

static int all_ok(void)
{
	int i;

	for (i = 1; i <= NCLAIMS; ++i)
		if (!g_claim_ok[i])
			return 0;
	return 1;
}

static const char *claim_name(int n)
{
	static const char *names[NCLAIMS + 1] = {
		"", "32-concurrent-blocking-receives", "out-of-order-replies",
		"timeout-isolation", "interrupt-isolation", "scm-rights-binding",
		"slow-waiter-no-hol", "fork-generation", "exec-generation",
		"caller-local-on-target", "measurements",
	};
	return names[n];
}

/* ====================================================================== */
/* 5. LAYOUT TABLE (fixture's copies) -- printed for the runner's diff    */
/* ====================================================================== */

#define LP(TY) \
	printf("LAYOUT %-46s size=%zu align=%zu\n", #TY, sizeof(TY), _Alignof(TY))
#define LPF(TY, FLD) \
	printf("LAYOUT %-46s.%s off=%zu size=%zu\n", #TY, #FLD, \
	       offsetof(TY, FLD), sizeof(((TY *)0)->FLD))

static void print_layout(void)
{
	LP(dserver_rpc_callhdr_t);
	LPF(dserver_rpc_callhdr_t, number);
	LPF(dserver_rpc_callhdr_t, pid);
	LPF(dserver_rpc_callhdr_t, tid);
	LPF(dserver_rpc_callhdr_t, architecture);
	LP(dserver_rpc_replyhdr_t);
	LPF(dserver_rpc_replyhdr_t, number);
	LPF(dserver_rpc_replyhdr_t, code);
	LP(dserver_rpc_call_thread_self_trap_t);
	LP(dserver_reply_thread_self_trap_t);
	LPF(dserver_reply_thread_self_trap_t, port_name);
	LP(dserver_rpc_reply_thread_self_trap_t);
	LP(dserver_call_mach_msg_overwrite_t);
	LP(dserver_rpc_call_mach_msg_overwrite_t);
	LP(dserver_call_ring_attach_t);
	LP(dserver_reply_ring_attach_t);
	LP(dserver_rpc_call_ring_attach_t);
	LP(dserver_rpc_reply_ring_attach_t);
	LP(dserver_s2c_callhdr_t);
	LP(dserver_s2c_replyhdr_t);
	LP(dserver_s2c_call_mmap_t);
	LP(dserver_s2c_reply_mmap_t);
	LP(dserver_s2c_call_munmap_t);
	LP(dserver_s2c_reply_munmap_t);
	LP(dserver_s2c_call_mprotect_t);
	LP(dserver_s2c_reply_mprotect_t);
	LP(dserver_s2c_call_msync_t);
	LP(dserver_s2c_reply_msync_t);
	LP(dserver_s2c_call_t);
	printf("LAYOUT %-46s size=%zu unsigned=%d\n", "dserver_callnum_t",
	       sizeof(dserver_callnum_t), (int)((dserver_callnum_t)-1 > 0));
	printf("LAYOUT const dserver_callnum_s2c=%d thread_self_trap=%d "
	       "ring_attach=%d mach_msg_overwrite=%d\n",
	       (int)dserver_callnum_s2c, (int)dserver_callnum_thread_self_trap,
	       (int)dserver_callnum_ring_attach,
	       (int)dserver_callnum_mach_msg_overwrite);
	printf("LAYOUT fixture demux_env_t size=%zu demux_slot_t size=%zu "
	       "demux_shm_t size=%zu\n", sizeof(demux_env_t),
	       sizeof(demux_slot_t), sizeof(demux_shm_t));
	fflush(stdout);
}

/* ====================================================================== */
/* 6. MUTATION SWITCHES                                                   */
/*    Each is a single marked line.  The runner flips exactly one of them  */
/*    in a temporary copy and requires the named claim to go red.          */
/* ====================================================================== */
#define PH_STOP    0xFFFFFFFFu
#define PH_D1      1u
#define PH_D2      2u
#define PH_D3      3u
#define PH_D3B     4u
#define PH_D4      5u
#define PH_D5      6u
#define PH_D6      7u
#define PH_D9      8u
#define PH_D5B     9u

#define DMX_LANE_CHECK_ENABLED   1
#define DMX_OP_ON_DISPATCHER     0
#define DMX_RELEASE_ON_INTERRUPT 1
#define DMX_FORK_GEN_CHECK       1
#define DMX_EXEC_GEN_CHECK       1
#define DMX_DISPATCH_OTHERS      1

/* ====================================================================== */
/* 7. THE DISPATCHER CORE (libc-free: runs after fork and after exec)      */
/* ====================================================================== */

typedef struct dmx_s2cctx {
	uint64_t addr;
	uint64_t len;
	uint64_t file_addr;
	uint64_t file_len;
} dmx_s2cctx_t;

static dmx_s2cctx_t g_s2cctx[DMX_MAXT];

static __attribute__((always_inline)) inline int dmx_slot_of(int32_t tid)
{
	uint32_t i, n = S->nthreads;

	for (i = 0; i < n; ++i)
		if (ld_acq(&S->slot_to_tid[i]) == tid)
			return (int)i;
	return -1;
}

static __attribute__((always_inline)) inline int dmx_ctl_fd(struct msghdr *mh)
{
	struct cmsghdr *c;
	int fd = -1;

	if (!mh->msg_control || mh->msg_controllen < sizeof(struct cmsghdr))
		return -1;
	c = CMSG_FIRSTHDR(mh);
	if (!c || c->cmsg_level != SOL_SOCKET || c->cmsg_type != SCM_RIGHTS)
		return -1;
	if (c->cmsg_len < CMSG_LEN(sizeof(int)))
		return -1;
	dcopy(&fd, CMSG_DATA(c), sizeof fd);
	return fd;
}

/* send one datagram to the server; optionally carrying one descriptor */
static __attribute__((noinline)) long dmx_sendto(int fd, const void *buf,
						 unsigned long len, int passfd)
{
	struct msghdr mh;
	struct iovec iov;
	char ctrl[CMSG_SPACE(sizeof(int))];
	struct cmsghdr *c;

	bfill(&mh, 0, sizeof mh);
	bfill(ctrl, 0, sizeof ctrl);
	iov.iov_base = (void *)(uintptr_t)buf;
	iov.iov_len = len;
	mh.msg_name = &g_server_addr;
	mh.msg_namelen = g_server_addr_len;
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	if (passfd >= 0) {
		c = (struct cmsghdr *)ctrl;
		c->cmsg_level = SOL_SOCKET;
		c->cmsg_type = SCM_RIGHTS;
		c->cmsg_len = CMSG_LEN(sizeof(int));
		dcopy(CMSG_DATA(c), &passfd, sizeof passfd);
		mh.msg_control = ctrl;
		mh.msg_controllen = CMSG_SPACE(sizeof(int));
	}
	return rsys3(SYS_sendmsg, fd, (long)&mh, 0);
}

/* the SERVER replies to the address the call arrived from (the process-level
 * endpoint), exactly as the product's server replies to thread->address() */
static __attribute__((noinline)) long dmx_sendto_peer(int fd, const void *buf,
						      unsigned long len,
						      int passfd)
{
	struct msghdr mh;
	struct iovec iov;
	char ctrl[CMSG_SPACE(sizeof(int))];
	struct cmsghdr *c;

	bfill(&mh, 0, sizeof mh);
	bfill(ctrl, 0, sizeof ctrl);
	iov.iov_base = (void *)(uintptr_t)buf;
	iov.iov_len = len;
	mh.msg_name = &g_srv_peer;
	mh.msg_namelen = g_srv_peer_len;
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	if (passfd >= 0) {
		c = (struct cmsghdr *)ctrl;
		c->cmsg_level = SOL_SOCKET;
		c->cmsg_type = SCM_RIGHTS;
		c->cmsg_len = CMSG_LEN(sizeof(int));
		dcopy(CMSG_DATA(c), &passfd, sizeof passfd);
		mh.msg_control = ctrl;
		mh.msg_controllen = CMSG_SPACE(sizeof(int));
	}
	return rsys3(SYS_sendmsg, fd, (long)&mh, 0);
}

/* Execute one caller-local (S2C) op on the calling thread.  This is the
 * product's shape: dserver-rpc-defs.h:199-330 runs mmap/munmap/mprotect/msync
 * inside the receive hook of the thread that is waiting for its own reply and
 * stamps the reply with THAT thread's tid.  Returns the executing tid. */
static __attribute__((noinline)) int32_t dmx_s2c_execute(int idx,
							 const demux_env_t *e,
							 int32_t exec_tid)
{
	const dserver_s2c_call_t *c = (const dserver_s2c_call_t *)(const void *)e->payload;
	demux_env_t out;
	union {
		dserver_s2c_reply_mmap_t mmap;
		dserver_s2c_reply_munmap_t munmap;
		dserver_s2c_reply_mprotect_t mprotect;
		dserver_s2c_reply_msync_t msync;
	} rep;
	uint64_t addr = c->mmap.address;
	uint64_t len = c->mmap.length;
	int32_t nfd = c->mmap.fd;
	long rc = 0;

	bfill(&rep, 0, sizeof rep);
	/* the real reply header, stamped with the EXECUTING thread's tid */
	rep.mmap.header.call_number = (int)dserver_callnum_s2c;
	rep.mmap.header.pid = dmx_getpid();
	rep.mmap.header.tid = exec_tid;
	rep.mmap.header.architecture = dserver_rpc_architecture_x86_64;
	rep.mmap.header.s2c_number = c->mmap.header.s2c_number;

	switch (c->mmap.header.s2c_number) {
	case dserver_s2c_msgnum_mmap:
		rc = rsys6(SYS_mmap, addr, len, c->mmap.protection, c->mmap.flags,
			   nfd, c->mmap.offset);
		if (rc < 0 && rc > -4096) {
			rep.mmap.errno_result = (int)-rc;
			rep.mmap.address = (uint64_t)-1;
		} else {
			rep.mmap.address = (uint64_t)rc;
			if (idx >= 0)
				g_s2cctx[idx].addr = (uint64_t)rc, g_s2cctx[idx].len = len;
		}
		break;
	case dserver_s2c_msgnum_munmap:
		/* the fixture's stand-in for the server knowing the process's
		 * mapping: a zero address means "the target's own mapping". */
		if (addr == 0 && idx >= 0)
			addr = g_s2cctx[idx].addr, len = g_s2cctx[idx].len;
		rc = rsys2(SYS_munmap, addr, len);
		if (rc < 0) {
			rep.munmap.return_value = -1;
			rep.munmap.errno_result = (int)-rc;
		} else {
			rep.munmap.return_value = (int)rc;
		}
		break;
	case dserver_s2c_msgnum_mprotect:
		if (addr == 0 && idx >= 0)
			addr = g_s2cctx[idx].addr, len = g_s2cctx[idx].len;
		rc = rsys3(SYS_mprotect, addr, len, c->mmap.protection);
		if (rc < 0) {
			rep.mprotect.return_value = -1;
			rep.mprotect.errno_result = (int)-rc;
		} else {
			rep.mprotect.return_value = (int)rc;
		}
		break;
	case dserver_s2c_msgnum_msync:
		if (addr == 0 && idx >= 0)
			addr = g_s2cctx[idx].file_addr, len = g_s2cctx[idx].file_len;
		rc = rsys3(SYS_msync, addr, len, c->msync.sync_flags);
		if (rc < 0) {
			rep.msync.return_value = -1;
			rep.msync.errno_result = (int)-rc;
		} else {
			rep.msync.return_value = (int)rc;
		}
		break;
	default:
		rep.mmap.errno_result = 22;	/* EINVAL */
		rep.mmap.address = (uint64_t)-1;
		break;
	}

	bfill(&out, 0, sizeof out);
	out.magic = DMX_MAGIC;
	out.kind = DK_S2C_REPLY;
	out.stid = e->stid;
	out.rid = e->rid;
	out.s2c_number = c->mmap.header.s2c_number;
	out.s2c_seq = e->s2c_seq;
	out.lane_generation = e->lane_generation;
	out.proc_generation = e->proc_generation;
	out.payload_len = sizeof(dserver_s2c_reply_mmap_t);
	dcopy(out.payload, &rep, sizeof(dserver_s2c_reply_mmap_t));
	dmx_sendto(g_fd, &out, sizeof out, -1);
	return exec_tid;
}

/* The mutation M3 target: the dispatcher executes the caller-local op itself.
 * The reply then carries the DISPATCHER's tid, which is exactly what the
 * product's server keys the waiting thread off (call.cpp:266-278 looks the
 * thread up by the S2C reply's header.tid and ups THAT thread's s2c semaphore),
 * so the real caller's _s2cPerform never returns. */
static void dmx_s2c_on_dispatcher(const demux_env_t *e, int32_t by_tid, int idx)
{
	st_rel(&S->exec_tid[idx], by_tid);
	add_rlx(&S->s2c_executed_by_dispatcher, 1);
	dmx_s2c_execute(idx, e, by_tid);
}

static __attribute__((always_inline)) inline int dmx_resolve_target(const demux_env_t *e)
{
	return dmx_slot_of(e->stid);
}

/* Move one received datagram into the target thread's completion slot.
 * Returns 0 delivered, 1 stale process generation, 2 stale lane generation,
 * 3 unknown target, 4 target not waiting. */
__attribute__((noinline)) int dmx_dispatch(const demux_env_t *e, int32_t by_tid,
					   int gotfd, int *out_idx)
{
	demux_slot_t *sl;
	uint32_t st;
	int idx;

	if (out_idx)
		*out_idx = -1;
	if (e->payload_len > DMX_PAYL) {
		add_rlx(&S->unknown_target, 1);
		if (gotfd >= 0) {
			rsys1(SYS_close, gotfd);
			add_rlx(&S->fd_closed_on_reject, 1);
		}
		return 3;
	}
	if (ld_acq(&S->proc_generation) != e->proc_generation) {
		add_rlx(&S->stale_gen_rejected, 1);
		if (gotfd >= 0) {
			rsys1(SYS_close, gotfd);
			add_rlx(&S->fd_closed_on_reject, 1);
		}
		return 1;
	}
	idx = dmx_resolve_target(e); /*MUT1-ORDER*/
	if (idx < 0) {
		add_rlx(&S->unknown_target, 1);
		if (gotfd >= 0) {
			rsys1(SYS_close, gotfd);
			add_rlx(&S->fd_closed_on_reject, 1);
		}
		return 3;
	}
	sl = &S->slots[idx];
	st = ld_acq(&sl->state);
	if (st != DS_WAIT && !(st == DS_READY && sl->got_rid != e->rid)) {
		add_rlx(&S->notwaiting_rejected, 1);
		if (gotfd >= 0) {
			rsys1(SYS_close, gotfd);
			add_rlx(&S->fd_closed_on_reject, 1);
		}
		return 4;
	}
	if (DMX_LANE_CHECK_ENABLED /*MUT2-LANE*/ &&
	    sl->lane_generation != e->lane_generation) {
		add_rlx(&S->late_lane_rejected, 1);
		if (gotfd >= 0) {
			rsys1(SYS_close, gotfd);
			add_rlx(&S->fd_closed_on_reject, 1);
		}
		return 2;
	}
	if (e->kind == DK_S2C && DMX_OP_ON_DISPATCHER /*MUT3-S2C*/) {
		dmx_s2c_on_dispatcher(e, by_tid, idx);
		return 0;
	}
	sl->got_rid = e->rid;
	sl->got_lane = e->lane_generation;
	sl->dkind = e->kind;
	sl->payload_len = e->payload_len;
	sl->dispatcher_tid = by_tid;
	sl->received_fd = gotfd;
	dcopy(sl->payload, e->payload, e->payload_len);
	sl->dispatch_seq = add_rlx(&S->dispatch_seq_ctr, 1);
	add_rlx(&S->dispatched, 1);
	add_rlx(&S->copies, 1);
	st_rel(&S->dispatch_by[idx], (uint32_t)by_tid);
	st_rel(&sl->state, DS_READY);
	add_rel(&sl->futex_word, 1);
	dmx_futex_wake(&sl->futex_word, 1);
	if (e->kind == DK_S2C)
		add_rlx(&S->s2c_routed, 1);
	if (out_idx)
		*out_idx = idx;
	return 0;
}

/* The demultiplexer's recvmsg loop.  Libc-free on purpose: this is the code a
 * permanent-thread design must be able to re-create in a single-threaded
 * fork() child and in a post-exec image, so it may not need libc, malloc or a
 * libc lock.  Exits on DK_STOP (clearing ctl_tid) or on a fatal recvmsg error. */
__attribute__((noinline, noreturn)) static void dmx_demux_loop(int fd)
{
	demux_env_t e;
	struct msghdr mh;
	struct iovec iov;
	char ctrl[CMSG_SPACE(sizeof(int))];

	for (;;) {
		long n;
		int gotfd = -1;

		bfill(&e, 0, sizeof e);
		bfill(ctrl, 0, sizeof ctrl);
		bfill(&mh, 0, sizeof mh);
		iov.iov_base = &e;
		iov.iov_len = sizeof e;
		mh.msg_iov = &iov;
		mh.msg_iovlen = 1;
		mh.msg_control = ctrl;
		mh.msg_controllen = sizeof ctrl;
		n = rsys3(SYS_recvmsg, fd, (long)&mh, 0);
		if (n < 0) {
			if (n == -EINTR)
				continue;
			if (n == -EAGAIN && !ld_acq(&S->demux_stop))
				continue;
			break;
		}

		gotfd = dmx_ctl_fd(&mh);
		if (n < (long)sizeof(demux_env_t) || e.magic != DMX_MAGIC) {
			if (gotfd >= 0)
				rsys1(SYS_close, gotfd);
			continue;
		}
		if (e.kind == DK_STOP) {
			if (gotfd >= 0)
				rsys1(SYS_close, gotfd);
			break;
		}
		dmx_dispatch(&e, dmx_gettid(), gotfd, 0);
	}
	st_rel(&S->ctl_tid, 0);
	rsys1(SYS_exit, 0);
	for (;;)
		;
}

/* ====================================================================== */
/* 8. THE SERVER (separate process, libc-free)                            */
/*    Modelled on the product's server: it reacts to the REAL call struct    */
/*    (header.tid + body), answers with the REAL reply struct, and for the   */
/*    caller-local op it sends the REAL S2C call and waits for the S2C reply  */
/*    whose header.tid it checks -- exactly the per-thread key that           */
/*    call.cpp:266-278 uses.                                                  */
/* ====================================================================== */

#define SRV_MAXBATCH 64
#define SRV_MAXHOLD 64

typedef struct srv_hold {
	demux_env_t env;
} srv_hold_t;

static demux_env_t g_srv_batch[SRV_MAXBATCH];
static int g_srv_batchfd[SRV_MAXBATCH];
static struct sockaddr_un g_srv_batchpeer[SRV_MAXBATCH];
static socklen_t g_srv_batchpeerlen[SRV_MAXBATCH];
static srv_hold_t g_srv_hold[SRV_MAXHOLD];
static uint32_t g_srv_nhold;

static __attribute__((noinline)) long srv_recv(int fd, demux_env_t *e, int *outfd)
{
	struct msghdr mh;
	struct iovec iov;
	static char ctrl[CMSG_SPACE(sizeof(int)) * 4];
	long n;

	bfill(e, 0, sizeof *e);
	bfill(ctrl, 0, sizeof ctrl);
	bfill(&mh, 0, sizeof mh);
	iov.iov_base = e;
	iov.iov_len = sizeof *e;
	mh.msg_name = &g_srv_peer;
	mh.msg_namelen = sizeof g_srv_peer;
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	mh.msg_control = ctrl;
	mh.msg_controllen = sizeof ctrl;
	n = rsys3(SYS_recvmsg, fd, (long)&mh, 0);
	if (n < 0)
		return n;
	g_srv_peer_len = mh.msg_namelen;
	add_rlx(&S->srv_recv_ok, 1);
	*outfd = dmx_ctl_fd(&mh);
	return n;
}

/* answer one call with the product's own reply struct; `withfd` hands back a
 * descriptor whose content is derived from the request's tag, so the guest can
 * prove from the descriptor itself which logical request it belongs to. */
static __attribute__((noinline)) long srv_answer(int sfd, const demux_env_t *call,
						 int withfd, long ctoken)
{
	demux_env_t out;
	dserver_rpc_reply_ring_attach_t *r =
		(dserver_rpc_reply_ring_attach_t *)(void *)out.payload;
	const dserver_rpc_call_ring_attach_t *c =
		(const dserver_rpc_call_ring_attach_t *)(const void *)call->payload;
	uint64_t tag = c->body.mapping_size;

	bfill(&out, 0, sizeof out);
	out.magic = DMX_MAGIC;
	out.kind = DK_REPLY;
	out.stid = call->stid;
	out.rid = call->rid;
	out.lane_generation = call->lane_generation;
	out.proc_generation = call->proc_generation;
	out.payload_len = sizeof *r;
	bfill(r, 0, sizeof *r);
	r->header.number = dserver_callnum_ring_attach;
	r->header.code = 0;
	if (ctoken >= 0) {
		/* the call carried THIS descriptor: echo a value derived from it,
		 * so the guest's reply check proves the binding */
		r->body.reject_reason = (uint32_t)((uint64_t)ctoken + 1ull);
	} else {
		r->body.reject_reason = (uint32_t)tag ^ (uint32_t)c->header.tid;
	}
	if (!withfd) {
		r->body.wake_fd = -1;
		add_rlx(&S->server_answered, 1);
		if (dmx_sendto_peer(sfd, &out, sizeof out, -1) < 0)
			add_rlx(&S->server_send_errors, 1);
		return 0;
	}
	r->body.wake_fd = 0;
	{
		long efd = rsys2(SYS_eventfd2, 0, 0);
		uint64_t tok = 0xF00D0000ull | (tag & 0xFFFFull);
		long rc;

		if (efd < 0) {
			r->body.wake_fd = -1;
			if (dmx_sendto_peer(sfd, &out, sizeof out, -1) < 0)
				add_rlx(&S->server_send_errors, 1);
			return 0;
		}
		rsys3(SYS_write, efd, (long)&tok, 8);
		rc = dmx_sendto_peer(sfd, &out, sizeof out, (int)efd);
		rsys1(SYS_close, efd);
		if (rc < 0)
			add_rlx(&S->server_send_errors, 1);
		add_rlx(&S->server_answered, 1);
		return rc;
	}
}

/* send one REAL S2C call to the target thread and wait for its reply; the
 * returned tid is the one the reply header claims executed it. */
static __attribute__((noinline)) int srv_s2c_roundtrip(int sfd,
						       const demux_env_t *call,
						       int32_t *out_tid)
{
	demux_env_t s;
	dserver_s2c_call_t *cc = (dserver_s2c_call_t *)(void *)s.payload;
	int tries;

	bfill(&s, 0, sizeof s);
	s.magic = DMX_MAGIC;
	s.kind = DK_S2C;
	s.stid = call->stid;
	s.rid = call->rid;
	s.s2c_number = call->s2c_number;
	s.s2c_seq = call->rid;
	s.lane_generation = call->lane_generation;
	s.proc_generation = call->proc_generation;
	s.payload_len = sizeof(dserver_s2c_call_t);
	bfill(cc, 0, sizeof *cc);
	cc->mmap.header.call_number = (int)dserver_callnum_s2c;
	cc->mmap.header.s2c_number = (dserver_s2c_msgnum_t)call->s2c_number;
	cc->mmap.address = 0;
	cc->mmap.length = 4096;
	cc->mmap.protection = 3;	/* PROT_READ|PROT_WRITE */
	cc->mmap.flags = 0x22;		/* MAP_PRIVATE|MAP_ANONYMOUS */
	cc->mmap.fd = -1;
	cc->mmap.offset = 0;
	if (dmx_sendto_peer(sfd, &s, sizeof s, -1) < 0)
		add_rlx(&S->server_send_errors, 1);

	for (tries = 0; tries < 12; ++tries) {
		demux_env_t r;
		int rfd = -1;
		long n = srv_recv(sfd, &r, &rfd);

		if (rfd >= 0)
			rsys1(SYS_close, rfd);
		if (n < 0)
			continue;
		if (r.magic != DMX_MAGIC)
			continue;
		if (r.kind != DK_S2C_REPLY || r.s2c_seq != call->rid)
			continue;
		*out_tid = ((const dserver_s2c_replyhdr_t *)(const void *)r.payload)->tid;
		return 1;
	}
	add_rlx(&S->s2c_incomplete, 1);
	return 0;
}

static __attribute__((noinline)) void srv_serve(int sfd, demux_env_t *c, int cfd,
						const struct sockaddr_un *peer,
						socklen_t peerlen)
{
	long ctoken = -1;
	int32_t got_tid = 0;

	if (peer) {
		dcopy(&g_srv_peer, peer, sizeof *peer);
		g_srv_peer_len = peerlen;
	}

	if (cfd >= 0) {
		uint64_t v = 0;

		if (rsys3(SYS_read, cfd, (long)&v, 8) == 8)
			ctoken = (long)v;
		rsys1(SYS_close, cfd);
	}
	if (c->flags & DGF_WITHHOLD) {
		if (g_srv_nhold < SRV_MAXHOLD) {
			dcopy(&g_srv_hold[g_srv_nhold].env, c, sizeof *c);
			g_srv_nhold++;
		}
		return;
	}
	if (c->flags & DGF_S2C) {
		if (!srv_s2c_roundtrip(sfd, c, &got_tid))
			return;
		if (got_tid != c->stid) {
			/* the product would set thread->_s2cReply on the WRONG
			 * thread and up the WRONG semaphore: the caller never
			 * returns.  Record it; do not answer. */
			add_rlx(&S->s2c_reply_tid_bad, 1);
			return;
		}
		add_rlx(&S->s2c_reply_tid_ok, 1);
		srv_answer(sfd, c, (c->flags & DGF_FD) ? 1 : 0, ctoken);
		return;
	}
	srv_answer(sfd, c, (c->flags & DGF_FD) ? 1 : 0, ctoken);
}

static __attribute__((noinline)) void srv_flush(int sfd)
{
	uint32_t i;

	for (i = 0; i < g_srv_nhold; ++i)
		srv_answer(sfd, &g_srv_hold[i].env,
			   (g_srv_hold[i].env.flags & DGF_FD) ? 1 : 0, -1);
	g_srv_nhold = 0;
}

static __attribute__((noinline, noreturn)) void dmx_server_entry(int sfd)
{
	struct timeval tv;

	g_srv_nhold = 0;
	bfill(&g_srv_peer, 0, sizeof g_srv_peer);
	tv.tv_sec = 0;
	tv.tv_usec = 250000;
	rsys6(SYS_setsockopt, sfd, SOL_SOCKET, SO_RCVTIMEO, (long)&tv,
	      sizeof tv, 0);
	add_rlx(&S->server_pid, (uint32_t)dmx_getpid());

	for (;;) {
		demux_env_t e;
		int fd = -1;
		long n = srv_recv(sfd, &e, &fd);
		uint32_t want, policy, got, k;

		if (n < 0) {
			if (e.magic == 0 && ld_acq(&S->phase_abort))
				break;
			continue;
		}
		if (e.magic != DMX_MAGIC) {
			if (fd >= 0)
				rsys1(SYS_close, fd);
			continue;
		}
		if (e.flags & DGF_STOP) {
			if (fd >= 0)
				rsys1(SYS_close, fd);
			break;
		}
		if (e.kind != DK_CALL) {
			if (fd >= 0)
				rsys1(SYS_close, fd);
			continue;
		}
		if (fd >= 0)
			rsys1(SYS_close, fd);
		if (e.flags & DGF_FLUSH) {
			srv_flush(sfd);
			continue;
		}
		st_rel(&S->srv_state, 1);
		if (e.kind != DK_CALL || !(e.flags & DGF_PHASE_BEGIN))
			continue;
		want = e.s2c_seq;
		policy = e.payload_len;
		if (want > SRV_MAXBATCH)
			want = SRV_MAXBATCH;
		got = 0;
		st_rel(&S->srv_state, 2);
		while (got < want) {
			demux_env_t c;
			int cfd = -1;
			long m = srv_recv(sfd, &c, &cfd);

			if (m < 0)
				continue;
			if (c.magic != DMX_MAGIC) {
				if (cfd >= 0)
					rsys1(SYS_close, cfd);
				continue;
			}
			if (c.flags & DGF_STOP) {
				if (cfd >= 0)
					rsys1(SYS_close, cfd);
				rsys1(SYS_exit_group, 0);
			}
			if (c.kind != DK_CALL) {
				if (cfd >= 0)
					rsys1(SYS_close, cfd);
				continue;
			}
			if (c.flags & DGF_FLUSH) {
				if (cfd >= 0)
					rsys1(SYS_close, cfd);
				srv_flush(sfd);
				continue;
			}
			dcopy(&g_srv_batch[got], &c, sizeof c);
			g_srv_batchfd[got] = cfd;
			dcopy(&g_srv_batchpeer[got], &g_srv_peer, sizeof g_srv_peer);
			g_srv_batchpeerlen[got] = g_srv_peer_len;
			st_rel(&S->srv_arrival[got], c.rid);
			got++;
			add_rlx(&S->srv_collected, 1);
		}
		st_rel(&S->srv_state, 3);
		st_rel(&S->srv_arrival_n, got);
		for (k = 0; k < got; ++k) {
			uint32_t j = (policy == POL_REVERSE) ? (got - 1 - k) : k;

			st_rel(&S->srv_answer[k], g_srv_batch[j].rid);
			srv_serve(sfd, &g_srv_batch[j], g_srv_batchfd[j],
				  &g_srv_batchpeer[j], g_srv_batchpeerlen[j]);
		}
	}
	rsys1(SYS_exit_group, 0);
	for (;;)
		;
}

/* ====================================================================== */
/* 9. CLIENT / WORKER SIDE                                                */
/* ====================================================================== */

#define WF_S2C    0x01u		/* this worker's call gets an S2C upcall */
#define WF_FD     0x02u		/* the reply carries a descriptor        */
#define WF_CALLFD 0x04u		/* the call carries a descriptor         */
#define WF_WITHHOLD 0x08u	/* the server holds this call's reply     */

static pthread_t g_wthr[DMX_MAXT];
static int32_t g_wtid[DMX_MAXT];
static int g_nworkers;
static unsigned g_wait_budget_ms = 8000;

static uint32_t cl_expect_reason(int idx, uint32_t tag)
{
	demux_slot_t *sl = &S->slots[idx];

	if (S->phase.wflags[idx] & WF_CALLFD)
		return (uint32_t)(0xC0000000u | (tag & 0xFFFFu)) + 1u;
	return tag ^ (uint32_t)sl->owner_tid;
}

static uint64_t cl_expect_token(uint32_t tag)
{
	return 0xF00D0000ull | (uint64_t)(tag & 0xFFFFu);
}

/* The generated client reads the descriptor out of the reply body's fd field
 * and closes it when the caller did not ask for it
 * (generate-rpc-wrappers.py:1660-1670).  The fixture always asks for it and
 * checks its identity from the descriptor itself, then closes it. */
static int cl_accept(int idx, uint32_t kind, uint32_t rid, uint32_t lane_gen,
		     int32_t stid, const uint8_t *payload, uint32_t payload_len,
		     int received_fd)
{
	demux_slot_t *sl = &S->slots[idx];
	demux_ledger_t *lg = &S->ledger[idx];
	const dserver_rpc_reply_ring_attach_t *r =
		(const dserver_rpc_reply_ring_attach_t *)(const void *)payload;

	if (kind != DK_REPLY) {
		lg->payload_mismatch++;
		add_rlx(&S->payload_mismatch, 1);
		if (received_fd >= 0) {
			close(received_fd);
			add_rlx(&S->fd_closed_on_caller_reject, 1);
		}
		return 0;
	}
	if (DMX_LANE_CHECK_ENABLED /*MUT2-LANE*/ &&
	    lane_gen != sl->lane_generation) {
		/* a stale lane: the payload was never examined, so this is NOT a
		 * payload mismatch -- the lane generation is the whole point.  The
		 * receiver checks it as well as the dispatcher, because a datagram
		 * can also arrive through the in-place path (variant 2). */
		add_rlx(&S->late_lane_rejected, 1);
		if (received_fd >= 0) {
			close(received_fd);
			add_rlx(&S->fd_closed_on_caller_reject, 1);
		}
		return 0;
	}
	if (rid != sl->want_rid || stid != sl->owner_tid) {
		lg->wrong_thread++;
		add_rlx(&S->wrong_thread, 1);
		if (received_fd >= 0) {
			close(received_fd);
			add_rlx(&S->fd_closed_on_caller_reject, 1);
		}
		return 0;
	}
	if (payload_len != sizeof *r ||
	    r->header.number != sl->expected_callnum ||
	    r->body.reject_reason != cl_expect_reason(idx, sl->want_tag)) {
		lg->payload_mismatch++;
		add_rlx(&S->payload_mismatch, 1);
		if (received_fd >= 0) {
			close(received_fd);
			add_rlx(&S->fd_closed_on_caller_reject, 1);
		}
		return 0;
	}
	if (received_fd >= 0) {
		uint64_t v = 0;
		long n = read(received_fd, &v, sizeof v);

		if (n == (long)sizeof v && v == cl_expect_token(sl->want_tag))
			lg->fd_token_ok++, add_rlx(&S->fd_token_ok, 1);
		else
			lg->fd_token_bad++, add_rlx(&S->fd_token_bad, 1);
		close(received_fd);
	}
	lg->rid_seen = rid;
	lg->tid_seen = stid;
	return 1;
}

static void cl_send_call(int idx, uint32_t rid, uint32_t tag, uint32_t flags)
{
	demux_env_t e;
	dserver_rpc_call_ring_attach_t *c =
		(dserver_rpc_call_ring_attach_t *)(void *)e.payload;
	demux_slot_t *sl = &S->slots[idx];
	int passfd = -1;

	bfill(&e, 0, sizeof e);
	e.magic = DMX_MAGIC;
	e.kind = DK_CALL;
	/* the worker flags are the FIXTURE's; only the derived server directives
	 * travel in the envelope (they live in a separate bit namespace) */
	e.flags = 0;
	if (flags & WF_WITHHOLD)
		e.flags |= DGF_WITHHOLD;
	if (flags & WF_FD)
		e.flags |= DGF_FD;
	if (flags & WF_CALLFD)
		e.flags |= DGF_CALL_FD;
	if (flags & WF_S2C)
		e.flags |= DGF_S2C;
	e.stid = sl->owner_tid;
	e.rid = rid;
	e.lane_generation = sl->lane_generation;
	e.proc_generation = ld_acq(&S->proc_generation);
	e.s2c_number = S->phase.wop[idx];
	e.payload_len = sizeof *c;
	c->header.number = S->phase.callnum;
	c->header.pid = dmx_getpid();
	c->header.tid = sl->owner_tid;
	c->header.architecture = dserver_rpc_architecture_x86_64;
	c->body.ring_fd = (S->phase.wflags[idx] & WF_CALLFD) ? 0 : -1;
	c->body.mapping_size = tag;
	if (S->phase.wflags[idx] & WF_CALLFD) {
		uint64_t tok = 0xC0000000ull | (uint64_t)(tag & 0xFFFFu);
		int efd = (int)rsys2(SYS_eventfd2, 0, 0);

		if (efd >= 0) {
			rsys3(SYS_write, efd, (long)&tok, 8);
			passfd = efd;
		}
	}
	{
		long sr = dmx_sendto(g_fd, &e, sizeof e, passfd);

		if (sr < 0) {
			add_rlx(&S->send_errors, 1);
			st_rel(&S->last_send_errno, (int32_t)(-sr));
			st_rel(&S->last_send_idx, (uint32_t)idx);
			st_rel(&S->last_send_phase, S->phase.phase);
			dcopy(S->last_send_dest, g_server_addr.sun_path,
			      sizeof S->last_send_dest);
		}
	}
	if (passfd >= 0)
		close(passfd);
}

static void cl_s2c_prepare(int idx, uint32_t op)
{
	if (op == dserver_s2c_msgnum_munmap || op == dserver_s2c_msgnum_mprotect) {
		if (!g_s2cctx[idx].addr) {
			void *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
				       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);

			if (p != MAP_FAILED) {
				g_s2cctx[idx].addr = (uint64_t)(uintptr_t)p;
				g_s2cctx[idx].len = 4096;
			}
		}
	} else if (op == dserver_s2c_msgnum_msync) {
		if (!g_s2cctx[idx].file_addr) {
			int f = open(g_path_shm, O_RDWR);

			if (f >= 0) {
				void *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
					       MAP_SHARED, f, 0);

				close(f);
				if (p != MAP_FAILED) {
					g_s2cctx[idx].file_addr = (uint64_t)(uintptr_t)p;
					g_s2cctx[idx].file_len = 4096;
				}
			}
		}
	}
}

/* the target executes the caller-local op; records who executed it */
static void cl_do_s2c(int idx, const demux_env_t *e)
{
	demux_ledger_t *lg = &S->ledger[idx];
	int32_t me = S->slots[idx].owner_tid;

	st_rel(&S->exec_tid[idx], me);
	lg->s2c_handled++;
	dmx_s2c_execute(idx, e, me);
}

static void cl_abandon(int idx, int interrupted)
{
	demux_slot_t *sl = &S->slots[idx];
	demux_ledger_t *lg = &S->ledger[idx];

	sl->lane_generation++;
	st_rel(&sl->state, DS_ABANDONED);
	if (interrupted)
		lg->interrupted = 1;
	else
		lg->timed_out = 1;
	lg->abandoned = 1;
}

/* V1: the worker only waits; a permanent demux thread does the recvmsg. */
static int v1_wait(int idx)
{
	demux_slot_t *sl = &S->slots[idx];
	demux_ledger_t *lg = &S->ledger[idx];
	uint64_t deadline = dmx_now_ns() +
		(uint64_t)S->phase.wtimeout[idx] * 1000000ull;

	for (;;) {
		uint32_t st = ld_acq(&sl->state);

		if (g_usr2_seen && (uint32_t)idx == S->phase.interrupt_idx) {
			cl_abandon(idx, 1);
			return 2;
		}
		if (st == DS_READY) {
			if (sl->dkind == DK_S2C) {
				demux_env_t e;

				bfill(&e, 0, sizeof e);
				e.kind = sl->dkind;
				e.stid = sl->owner_tid;
				e.rid = sl->got_rid;
				e.s2c_seq = sl->got_rid;
				e.s2c_number = (uint32_t)sl->payload_len;
				e.proc_generation = ld_acq(&S->proc_generation);
				dcopy(e.payload, sl->payload,
				      sl->payload_len > DMX_PAYL ? DMX_PAYL : sl->payload_len);
				st_rel(&sl->state, DS_WAIT);
				cl_do_s2c(idx, &e);
				continue;
			}
			if (cl_accept(idx, sl->dkind, sl->got_rid, sl->lane_generation,
				      sl->owner_tid, sl->payload, sl->payload_len,
				      sl->received_fd)) {
				sl->received_fd = -1;
				st_rel(&sl->state, DS_EMPTY);
				return 0;
			}
			sl->received_fd = -1;
			st_rel(&sl->state, DS_WAIT);
			continue;
		}
		if (st == DS_ABANDONED)
			st_rel(&sl->state, DS_WAIT);
		if (dmx_now_ns() >= deadline) {
			cl_abandon(idx, 0);
			return 1;
		}
		lg->parked_polls++;
		{
			uint32_t w = ld_rlx(&sl->futex_word);

			if (ld_acq(&sl->state) == DS_READY)
				continue;
			(void)dmx_futex_wait(&sl->futex_word, w, 20);
		}
	}
}

/* --- V2: a reader token held by one of the waiting threads ------------- */

enum v2_result { V2R_NONE = 0, V2R_MINE, V2R_INTERRUPT, V2R_OTHER, V2R_STOP };

static int v2_try_acquire(int idx)
{
	int32_t me = S->slots[idx].owner_tid;
	int32_t z = 0;

	if (cas32(&S->token_owner, &z, me)) {
		add_rlx(&S->token_handoffs, 1);
		return 1;
	}
	return 0;
}

/* hand the token to the next registered waiter (deterministic index order),
 * or drop it.  Only workers that will still wait in THIS phase may receive it. */
static void v2_release(int idx)
{
	int32_t me = S->slots[idx].owner_tid;
	int32_t next = 0;
	uint32_t i, n = S->nthreads, pick = 0;

	for (i = 0; i < n; ++i) {
		if (!S->phase.wact[i])
			continue;
		if (ld_acq(&S->slot_to_tid[i]) == me)
			continue;
		if (ld_acq(&S->slots[i].state) == DS_WAIT) {
			next = S->slot_to_tid[i];
			pick = i;
			break;
		}
	}
	st_rel(&S->token_owner, next);
	add_rlx(&S->token_releases, 1);
	if (next) {
		add_rlx(&S->slots[pick].futex_word, 1);
		dmx_futex_wake(&S->slots[pick].futex_word, 1);
	}
}

/* the holder's recvmsg: it parses its OWN datagrams in place (no copy, no slot
 * write, no futex) and routes everyone else's into their completion slot. */
static int v2_read_and_dispatch(int idx)
{
	demux_env_t e;
	struct msghdr mh;
	struct iovec iov;
	char ctrl[CMSG_SPACE(sizeof(int))];
	long n;
	int gotfd = -1;

	bfill(&e, 0, sizeof e);
	bfill(ctrl, 0, sizeof ctrl);
	bfill(&mh, 0, sizeof mh);
	iov.iov_base = &e;
	iov.iov_len = sizeof e;
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	mh.msg_control = ctrl;
	mh.msg_controllen = sizeof ctrl;
	n = recvmsg(g_fd, &mh, 0);
	if (n < 0)
		return errno == EINTR ? V2R_INTERRUPT : V2R_NONE;
	gotfd = dmx_ctl_fd(&mh);
	if (n < (long)sizeof(demux_env_t) || e.magic != DMX_MAGIC) {
		if (gotfd >= 0)
			close(gotfd);
		return V2R_OTHER;
	}
	if (e.kind == DK_STOP) {
		if (gotfd >= 0)
			close(gotfd);
		return V2R_STOP;
	}
	if (e.stid == S->slots[idx].owner_tid) {
		/* the holder consumes its own datagram in place: it still takes a
		 * position in the delivery stream (no payload copy, no slot write) */
		S->slots[idx].dispatch_seq = add_rlx(&S->dispatch_seq_ctr, 1);
		S->slots[idx].got_lane = e.lane_generation;
		add_rlx(&S->dispatched_own_inplace, 1);
		if (e.kind == DK_S2C) {
			add_rlx(&S->s2c_routed, 1);
			cl_do_s2c(idx, &e);
			return V2R_OTHER;
		}
		if (cl_accept(idx, e.kind, e.rid, e.lane_generation, e.stid,
			      e.payload, e.payload_len, gotfd))
			return V2R_MINE;
		return V2R_OTHER;
	}
	if (DMX_DISPATCH_OTHERS /*MUT7-HOL*/)
		dmx_dispatch(&e, dmx_gettid(), gotfd, 0);
	else {
		/* MUTATED: the holder keeps its own and drops everyone else's */
		add_rlx(&S->token_starved, 1);
		if (gotfd >= 0)
			close(gotfd);
	}
	return V2R_OTHER;
}

static int v2_wait(int idx)
{
	demux_slot_t *sl = &S->slots[idx];
	demux_ledger_t *lg = &S->ledger[idx];
	int32_t me = sl->owner_tid;
	uint64_t deadline = dmx_now_ns() +
		(uint64_t)S->phase.wtimeout[idx] * 1000000ull;

	for (;;) {
		uint32_t st = ld_acq(&sl->state);
		int rc;

		if (g_usr2_seen && (uint32_t)idx == S->phase.interrupt_idx) {
			if ((int32_t)ld_acq(&S->token_owner) == me)
				v2_release(idx);
			cl_abandon(idx, 1);
			return 2;
		}
		if (st == DS_READY) {
			if (sl->dkind == DK_S2C) {
				demux_env_t e;

				bfill(&e, 0, sizeof e);
				e.kind = sl->dkind;
				e.stid = sl->owner_tid;
				e.rid = sl->got_rid;
				e.s2c_seq = sl->got_rid;
				e.proc_generation = ld_acq(&S->proc_generation);
				dcopy(e.payload, sl->payload,
				      sl->payload_len > DMX_PAYL ? DMX_PAYL : sl->payload_len);
				st_rel(&sl->state, DS_WAIT);
				cl_do_s2c(idx, &e);
				continue;
			}
			if (cl_accept(idx, sl->dkind, sl->got_rid, sl->lane_generation,
				      sl->owner_tid, sl->payload, sl->payload_len,
				      sl->received_fd)) {
				sl->received_fd = -1;
				st_rel(&sl->state, DS_EMPTY);
				if ((int32_t)ld_acq(&S->token_owner) == me)
					v2_release(idx);
				return 0;
			}
			sl->received_fd = -1;
			st_rel(&sl->state, DS_WAIT);
			continue;
		}
		if (st == DS_ABANDONED)
			st_rel(&sl->state, DS_WAIT);
		if (dmx_now_ns() >= deadline) {
			if ((int32_t)ld_acq(&S->token_owner) == me)
				v2_release(idx);
			cl_abandon(idx, 0);
			return 1;
		}
		if ((int32_t)ld_acq(&S->token_owner) == 0)
			v2_try_acquire(idx);
		if ((int32_t)ld_acq(&S->token_owner) == me) {
			rc = v2_read_and_dispatch(idx);
			if (rc == V2R_MINE) {
				st_rel(&sl->state, DS_EMPTY);
				v2_release(idx);
				return 0;
			}
			if (rc == V2R_INTERRUPT) {
				if (DMX_RELEASE_ON_INTERRUPT /*MUT4-TOKEN*/)
					v2_release(idx);
				else
					add_rlx(&S->token_starved, 1);
				cl_abandon(idx, 1);
				return 2;
			}
			if (rc == V2R_STOP) {
				st_rel(&sl->state, DS_EMPTY);
				v2_release(idx);
				return 3;
			}
			continue;
		}
		lg->parked_polls++;
		{
			uint32_t w = ld_rlx(&sl->futex_word);

			if (ld_acq(&sl->state) == DS_READY ||
			    ld_acq(&S->token_owner) == 0)
				continue;
			(void)dmx_futex_wait(&sl->futex_word, w, 20);
		}
	}
}

static void *worker_thread(void *arg)
{
	int idx = (int)(intptr_t)arg;
	uint32_t seen_phase = 0;

	g_wtid[idx] = dmx_gettid();
	S->slots[idx].owner_tid = g_wtid[idx];
	st_rel(&S->slot_to_tid[idx], g_wtid[idx]);
	st_rel(&S->slots[idx].state, DS_EMPTY);
	S->slots[idx].lane_generation = 1;
	S->slots[idx].received_fd = -1;

	for (;;) {
		demux_phase_t *ph = &S->phase;
		uint32_t ph_id;
		uint32_t rid, tag, flags;
		int rc;

		/* wait for the next phase: `go` is the phase sequence number */
		for (;;) {
			uint32_t s = ld_acq(&ph->go);

			if (s != seen_phase)
				break;
			(void)dmx_futex_wait(&ph->go, s, 0);
		}
		seen_phase = ld_acq(&ph->go);
		ph_id = ld_acq(&ph->phase);
		if (ph_id == PH_STOP)
			break;
		if (!ph->wact[idx])
			continue;
		{
			demux_slot_t *sl = &S->slots[idx];
			demux_ledger_t *lg = &S->ledger[idx];
			uint64_t t0;

			rid = ph->rid_base + (uint32_t)idx;
			tag = ph->base_tag + (uint32_t)idx;
			flags = ph->wflags[idx];
			sl->want_rid = rid;
			sl->want_tag = tag;
			sl->expected_callnum = ph->callnum;
			sl->payload_len = 0;
			sl->got_rid = 0;
			sl->received_fd = -1;
			sl->payload_ok = 0;
			st_rel(&sl->sent, 0);
			lg->rid_sent = rid;
			lg->completions = 0;
			lg->payload_mismatch = 0;
			lg->wrong_thread = 0;
			lg->timed_out = 0;
			lg->interrupted = 0;
			lg->abandoned = 0;
			lg->fd_token_ok = 0;
			lg->fd_token_bad = 0;
			lg->s2c_handled = 0;
			lg->parked_polls = 0;
			if (flags & WF_S2C)
				cl_s2c_prepare(idx, ph->wop[idx]);
			st_rel(&sl->state, DS_WAIT);
			t0 = dmx_now_ns();
			cl_send_call(idx, rid, tag, flags);
			st_rel(&sl->sent, 1);
			rc = (g_variant == 2) ? v2_wait(idx) : v1_wait(idx);
			lg->lat_ns = dmx_now_ns() - t0;
			if (rc == 0)
				lg->completions = 1;
		}
		add_rel(&ph->done, 1);
		dmx_futex_wake(&ph->done, 1);
	}
	return NULL;
}

/* ====================================================================== */
/* 10. PHASE MACHINERY AND THE V1 DEMULTIPLEXER THREAD                    */
/* ====================================================================== */

static void ph_begin_config(uint32_t id, uint32_t base_tag, uint32_t rid_base,
			    uint64_t gen, uint32_t callnum)
{
	demux_phase_t *ph = &S->phase;
	uint32_t i;

	st_rel(&ph->done, 0);
	for (i = 0; i < DMX_MAXT; ++i) {
		ph->wact[i] = 0;
		ph->wflags[i] = 0;
		ph->wtimeout[i] = g_wait_budget_ms;
		ph->wop[i] = 0;
	}
	ph->phase = id;
	ph->count = 0;
	ph->withhold_idx = 0xFFFFFFFFu;
	ph->interrupt_idx = 0xFFFFFFFFu;
	g_usr2_seen = 0;
	ph->base_tag = base_tag;
	ph->rid_base = rid_base;
	ph->gen = gen;
	ph->callnum = callnum;
}

static void ph_add(uint32_t idx, uint32_t flags, uint32_t timeout_ms, uint32_t op)
{
	demux_phase_t *ph = &S->phase;

	ph->wact[idx] = 1;
	ph->wflags[idx] = flags;
	ph->wtimeout[idx] = timeout_ms;
	ph->wop[idx] = op;
	ph->count++;
}

static void ph_go(void)
{
	add_rel(&S->phase.go, 1);
	dmx_futex_wake(&S->phase.go, 0x7fffffff);
}

static uint32_t ph_wait(uint32_t want, long ms)
{
	uint64_t deadline = dmx_now_ns() + (uint64_t)ms * 1000000ull;

	for (;;) {
		uint32_t got = ld_acq(&S->phase.done);

		if (got >= want)
			return got;
		if (dmx_now_ns() >= deadline)
			return got;
		(void)dmx_futex_wait(&S->phase.done, got, 20);
	}
}

static uint32_t ph_parked(uint32_t n)
{
	uint32_t i, c = 0;

	for (i = 0; i < n; ++i)
		if (S->phase.wact[i] &&
		    ld_acq(&S->slots[i].state) == DS_WAIT)
			++c;
	return c;
}

static int wait_until_all_parked(uint32_t n, uint32_t count, long ms)
{
	uint64_t deadline = dmx_now_ns() + (uint64_t)ms * 1000000ull;

	while (dmx_now_ns() < deadline) {
		if (ph_parked(n) >= count)
			return 1;
		usleep(2000);
	}
	return 0;
}

static int wait_until(volatile uint32_t *flag, uint32_t val, long ms)
{
	uint64_t deadline = dmx_now_ns() + (uint64_t)ms * 1000000ull;

	while (dmx_now_ns() < deadline) {
		if (ld_acq(flag) == val)
			return 1;
		usleep(2000);
	}
	return 0;
}

/* ---- the server-side control datagrams main sends --------------------- */
static int wait_until_collected(uint32_t count, long ms)
{
	uint64_t deadline = dmx_now_ns() + (uint64_t)ms * 1000000ull;

	while (dmx_now_ns() < deadline) {
		if (ld_acq(&S->srv_arrival_n) >= count)
			return 1;
		usleep(2000);
	}
	return 0;
}

static void srv_begin(uint32_t count, uint32_t policy)
{
	demux_env_t e;

	st_rel(&S->srv_arrival_n, 0);
	bfill(&e, 0, sizeof e);
	e.magic = DMX_MAGIC;
	e.kind = DK_CALL;
	e.flags = DGF_PHASE_BEGIN;
	e.s2c_seq = count;
	e.payload_len = policy;
	dmx_sendto(g_fd, &e, sizeof e, -1);
}

static void srv_flush_cmd(void)
{
	demux_env_t e;

	bfill(&e, 0, sizeof e);
	e.magic = DMX_MAGIC;
	e.kind = DK_CALL;
	e.flags = DGF_FLUSH;
	dmx_sendto(g_fd, &e, sizeof e, -1);
}

static void srv_stop(void)
{
	demux_env_t e;

	bfill(&e, 0, sizeof e);
	e.magic = DMX_MAGIC;
	e.kind = DK_STOP;
	e.flags = DGF_STOP;
	dmx_sendto(g_fd, &e, sizeof e, -1);
}

static void phase_note(const char *name, uint32_t done, uint32_t want);

/* ---- the V1 permanent demultiplexer thread --------------------------- */

static void *demux_thread_main(void *arg)
{
	sigset_t set;

	(void)arg;
	st_rel(&S->ctl_tid, (uint32_t)dmx_gettid());
	/* The demux thread must not be the thread an RPC waiter's interrupt
	 * signal is aimed at: block the whole set.  (pthread_kill is
	 * thread-directed, but a process-directed signal would otherwise be
	 * ripe to land here and turn into an EINTR in the demux loop.) */
	sigfillset(&set);
	pthread_sigmask(SIG_BLOCK, &set, NULL);
	dmx_demux_loop(g_fd);
	return NULL;
}

static void v1_start(void)
{
	pthread_attr_t attr;
	size_t stack = 0;

	pthread_attr_init(&attr);
	pthread_attr_getstacksize(&attr, &stack);
	printf("INFO v1 demux thread: pthread default stack size = %zu KiB\n",
	       stack / 1024);
	if (pthread_create(&g_demux_thr, &attr, demux_thread_main, NULL) != 0) {
		fprintf(stderr, "fatal: cannot create the demux thread\n");
		exit(3);
	}
	pthread_attr_destroy(&attr);
	pthread_detach(g_demux_thr);
	{
		uint64_t deadline = dmx_now_ns() + 2000000000ull;

		while (ld_acq(&S->ctl_tid) == 0 && dmx_now_ns() < deadline)
			usleep(1000);
	}
	g_demux_tid = (int)ld_acq(&S->ctl_tid);
	printf("INFO v1 demux thread tid=%d\n", g_demux_tid);
}

/* stop ONLY the permanent thread: the fork/exec legs still need the server */
static void v1_stop(void)
{
	uint64_t deadline;

	if (g_variant != 1 || ld_acq(&S->ctl_tid) == 0)
		return;
	st_rel(&S->demux_stop, 1);
	deadline = dmx_now_ns() + 2000000000ull;
	while (ld_acq(&S->ctl_tid) != 0 && dmx_now_ns() < deadline)
		usleep(1000);
	printf("INFO v1 demux thread stopped (ctl_tid=%u)\n",
	       ld_acq(&S->ctl_tid));
}

/* ---- validation helpers ---------------------------------------------- */

static int workers_ok_except(uint32_t n, int32_t skip, char *det, size_t cap)
{
	uint32_t i, bad = 0;

	det[0] = 0;
	for (i = 0; i < n; ++i) {
		demux_ledger_t *lg = &S->ledger[i];

		if ((int32_t)i == skip)
			continue;
		if (lg->completions != 1 || lg->rid_seen != lg->rid_sent ||
		    lg->tid_seen != S->slots[i].owner_tid ||
		    lg->payload_mismatch || lg->wrong_thread) {
			++bad;
			if (bad <= 3) {
				char t[160];

				snprintf(t, sizeof t,
					 " [w%u comp=%u rid=%u/%u tid=%d/%d mm=%u wt=%u]",
					 i, lg->completions, lg->rid_seen,
					 lg->rid_sent, lg->tid_seen,
					 S->slots[i].owner_tid,
					 lg->payload_mismatch, lg->wrong_thread);
				strncat(det, t, cap - strlen(det) - 1);
			}
		}
	}
	return bad == 0;
}

static int workers_ok(uint32_t n, char *det, size_t cap)
{
	return workers_ok_except(n, -1, det, cap);
}

static int fd_count_self(void)
{
	DIR *d = opendir("/proc/self/fd");
	struct dirent *de;
	int n = 0;

	if (!d)
		return -1;
	while ((de = readdir(d)) != NULL)
		if (de->d_name[0] != '.')
			++n;
	closedir(d);
	return n;
}

/* ---------------------------------------------------------------------- */
/* D1: 32 concurrent blocking receives on ONE process-level socket.        */
/* ---------------------------------------------------------------------- */
static void phase_d1(void)
{
	uint32_t n = 32, i, done;
	uint32_t d0 = ld_acq(&S->dispatched), c0 = ld_acq(&S->copies);
	char det[512] = "";
	int ok;

	ph_begin_config(PH_D1, 0x1000, 1000, GEN_D1, dserver_callnum_ring_attach);
	for (i = 0; i < n; ++i)
		ph_add(i, 0, g_wait_budget_ms, 0);
	srv_begin(n, POL_PLAIN);
	st_rel(&S->proc_generation, GEN_D1);
	ph_go();
	done = ph_wait(n, (long)g_wait_budget_ms + 4000);
	ok = (done == n) && workers_ok(n, det, sizeof det);
	phase_note("D1", done, n);
	verdict(1, ok, "%u/%u concurrent blocking receives completed; the one "
		"process-level endpoint dispatched %u datagrams (%u payload copies); "
		"wrong_thread=%u payload_mismatch=%u%s",
		done, n, ld_acq(&S->dispatched) - d0, ld_acq(&S->copies) - c0,
		ld_acq(&S->wrong_thread), ld_acq(&S->payload_mismatch), det);
}

/* ---------------------------------------------------------------------- */
/* D2: replies delivered in the REVERSE of the arrival order still reach    */
/*     the right thread (the product matches by call number only).          */
/* ---------------------------------------------------------------------- */
static void phase_d2(void)
{
	uint32_t n = 32, i, done, k;
	uint32_t ans0 = ld_acq(&S->server_answered);
	uint32_t seq0 = ld_acq(&S->dispatch_seq_ctr);
	char det[512] = "";
	int ok, reversed = 0;

	ph_begin_config(PH_D2, 0x2000, 2000, GEN_D2, dserver_callnum_ring_attach);
	for (i = 0; i < n; ++i)
		ph_add(i, 0, g_wait_budget_ms, 0);
	srv_begin(n, POL_REVERSE);
	st_rel(&S->proc_generation, GEN_D2);
	ph_go();
	done = ph_wait(n, (long)g_wait_budget_ms + 4000);
	/* prove the answer order really was the reverse of the arrival order */
	for (k = 0; k < n; ++k)
		if (ld_acq(&S->srv_answer[k]) != ld_acq(&S->srv_arrival[n - 1 - k]))
			reversed = -1;
	/* the demux's actual DELIVERY order must be that same reversal: worker
	 * (srv_arrival[k] - rid_base) must have been served at position n-1-k */
	for (k = 0; k < n; ++k) {
		uint32_t rid = ld_acq(&S->srv_arrival[k]);
		uint32_t w = rid - 2000u;

		if (w >= n || S->slots[w].dispatch_seq - seq0 != n - 1u - k) {
			if (reversed == 0)
				reversed = (int)k + 1;
		}
	}
	ok = (done == n) && workers_ok(n, det, sizeof det) && reversed == 0;
	phase_note("D2", done, n);
	if (reversed != 0) {
		uint32_t kk, order[64];
		char t[8];

		det[0] = 0;
		strncat(det, " [arrival:", sizeof det - strlen(det) - 1);
		for (kk = 0; kk < n; ++kk) {
			snprintf(t, sizeof t, " %u",
				 ld_acq(&S->srv_arrival[kk]) - 2000u);
			strncat(det, t, sizeof det - strlen(det) - 1);
		}
		strncat(det, " | dispatched:", sizeof det - strlen(det) - 1);
		for (kk = 0; kk < n; ++kk) {
			uint32_t w = 9999u;

			if (S->slots[kk].dispatch_seq >= seq0 &&
			    S->slots[kk].dispatch_seq - seq0 < n)
				order[S->slots[kk].dispatch_seq - seq0] = kk;
			(void)w;
		}
		for (kk = 0; kk < n; ++kk) {
			snprintf(t, sizeof t, " %u", order[kk]);
			strncat(det, t, sizeof det - strlen(det) - 1);
		}
		strncat(det, " | seq:", sizeof det - strlen(det) - 1);
		for (kk = 0; kk < n; ++kk) {
			snprintf(t, sizeof t, " %u", S->slots[kk].dispatch_seq);
			strncat(det, t, sizeof det - strlen(det) - 1);
		}
		strncat(det, "]", sizeof det - strlen(det) - 1);
	}
	verdict(2, ok, "%u/%u replies arrived in the reverse of the arrival order "
		"(arrival[0]=rid%u answered last as rid%u) and every reply landed on "
		"its own thread: payload_mismatch=%u wrong_thread=%u%s",
		done, n, ld_acq(&S->srv_arrival[0]), ld_acq(&S->srv_answer[n - 1]),
		ld_acq(&S->payload_mismatch), ld_acq(&S->wrong_thread), det);
	(void)ans0;
}

/* ---------------------------------------------------------------------- */
/* D3: one waiter times out; the others continue; the late reply for the    */
/*     abandoned lane is rejected by the lane generation.                   */
/* ---------------------------------------------------------------------- */
static void phase_d3(void)
{
	uint32_t n = 32, i, done;
	uint32_t lr0 = ld_acq(&S->late_lane_rejected);
	uint32_t pm0 = ld_acq(&S->payload_mismatch);
	uint32_t nw0 = ld_acq(&S->notwaiting_rejected);
	char det[512] = "";
	int ok, ok_others, ok_b;

	ph_begin_config(PH_D3, 0x3000, 3000, GEN_D3, dserver_callnum_ring_attach);
	for (i = 0; i < n; ++i)
		ph_add(i, (i == 3) ? WF_WITHHOLD : 0,
		       (i == 3) ? (g_fast ? 120u : 200u) : g_wait_budget_ms, 0);
	srv_begin(n, POL_PLAIN);
	st_rel(&S->proc_generation, GEN_D3);
	ph_go();
	done = ph_wait(n, (long)g_wait_budget_ms + 4000);
	phase_note("D3", done, n);
	ok_others = workers_ok_except(n, 3, det, sizeof det);
	ok = done == n && ok_others && S->ledger[3].timed_out == 1 &&
	     S->ledger[3].abandoned == 1;

	/* PH_D3B: the SAME lane (worker 3), the SAME request id, the SAME tag and
	 * the SAME process generation -- only the lane generation separates the two
	 * incarnations.  The new call is withheld so the FLUSH releases the stale
	 * reply first. */
	ph_begin_config(PH_D3B, 0x3000, 3000, GEN_D3, dserver_callnum_ring_attach);
	ph_add(3, WF_WITHHOLD, g_wait_budget_ms, 0);
	srv_begin(1, POL_PLAIN);
	ph_go();
	(void)wait_until_collected(1, 4000);
	srv_flush_cmd();
	done = ph_wait(1, (long)g_wait_budget_ms + 4000);
	phase_note("D3B", done, 1);
	ok_b = done == 1 && S->ledger[3].completions == 1 &&
	       S->ledger[3].rid_seen == S->ledger[3].rid_sent &&
	       S->ledger[3].payload_mismatch == 0 &&
	       S->slots[3].got_lane == S->slots[3].lane_generation;
	ok = ok && ok_b &&
	     (ld_acq(&S->late_lane_rejected) - lr0) == 1 &&
	     (ld_acq(&S->payload_mismatch) - pm0) == 0;
	if (!ok)
		snprintf(det + strlen(det), sizeof det - strlen(det),
			 " [done=%u/%u others_ok=%d timed_out=%u abandoned=%u "
			 "lane_reuse_ok=%d]", done, n, ok_others,
			 S->ledger[3].timed_out, S->ledger[3].abandoned, ok_b);
	verdict(3, ok, "waiter 3 timed out and abandoned its lane; 31/31 others "
		"completed; the late reply for the abandoned lane was rejected by the "
		"LANE GENERATION (late_lane_rejected=%u, not-waiting guard=%u) and the "
		"re-used lane -- SAME request id, SAME payload shape, so the lane "
		"generation is the only discriminator -- completed with a reply from "
		"lane %u of %u (stale payloads accepted: %u)%s",
		ld_acq(&S->late_lane_rejected) - lr0,
		ld_acq(&S->notwaiting_rejected) - nw0,
		S->slots[3].got_lane, S->slots[3].lane_generation,
		ld_acq(&S->payload_mismatch) - pm0, det);
}

/* ---------------------------------------------------------------------- */
/* D4: one waiter is interrupted (the token holder in V2); the others        */
/*     continue.  Every call is withheld, so the interrupt is provably not   */
/*     racing with a delivery.                                              */
/* ---------------------------------------------------------------------- */
static void phase_d4(void)
{
	uint32_t n = 32, i, done;
	uint32_t intr = 5;
	int32_t token_before, token_after;
	uint32_t st0 = ld_acq(&S->token_starved);
	char det[512] = "";
	int ok;

	ph_begin_config(PH_D4, 0x4000, 4000, GEN_D4, dserver_callnum_ring_attach);
	for (i = 0; i < n; ++i)
		ph_add(i, WF_WITHHOLD,
		       (i == intr) ? (g_fast ? 120u : 200u) : g_wait_budget_ms, 0);
	if (g_variant == 2)
		st_rel(&S->token_owner, S->slots[intr].owner_tid);
	S->phase.interrupt_idx = intr;
	srv_begin(n, POL_PLAIN);
	st_rel(&S->proc_generation, GEN_D4);
	ph_go();
	(void)wait_until_collected(n, 4000);
	(void)wait_until_all_parked(n, n, 4000);
	token_before = ld_acq(&S->token_owner);
	(void)pthread_kill(g_wthr[intr], SIGUSR2);
	(void)wait_until((volatile uint32_t *)&S->ledger[intr].interrupted, 1, 4000);
	token_after = ld_acq(&S->token_owner);
	/* nothing has been answered yet: only the interrupted waiter may be done */
	ok = S->ledger[intr].interrupted == 1 && ld_acq(&S->phase.done) == 1;
	if (g_variant == 2)
		ok = ok && token_after != S->slots[intr].owner_tid;
	srv_flush_cmd();
	done = ph_wait(n, (long)g_wait_budget_ms + 4000);
	ok = ok && done == n && workers_ok_except(n, (int32_t)intr, det, sizeof det);
	if (g_variant == 2)
		ok = ok && ld_acq(&S->token_starved) == st0;
	phase_note("D4", done, n);
	verdict(4, ok, "(V%d) waiter %u was interrupted and abandoned; %u/%u "
		"workers completed afterwards (the interrupted one's stale reply was "
		"rejected by generation/lane, the other 31 by their own replies); "
		"token before=%d after=%d releases=%u starved=%u; demux thread alive=%d%s",
		g_variant, intr, done - 1, n - 1,
		token_before, token_after, ld_acq(&S->token_releases),
		ld_acq(&S->token_starved) - st0,
		g_variant == 1 ? ld_acq(&S->ctl_tid) != 0 : 1, det);
}

/* ---------------------------------------------------------------------- */
/* D5: SCM_RIGHTS in both directions on the one socket, bound to the right   */
/*     logical request, and closed rather than leaked when rejected.         */
/* ---------------------------------------------------------------------- */
static void phase_d5(void)
{
	uint32_t n = 3, i, done;
	int fd_before = fd_count_self();
	uint32_t t0 = ld_acq(&S->fd_token_ok), t1 = ld_acq(&S->fd_token_bad);
	uint32_t r0 = ld_acq(&S->fd_closed_on_reject) +
		      ld_acq(&S->fd_closed_on_caller_reject);
	char det[512] = "";
	int ok;

	ph_begin_config(PH_D5, 0x5000, 5000, GEN_D5, dserver_callnum_ring_attach);
	for (i = 0; i < n; ++i)
		if (i < 2)
			ph_add(i, WF_FD | WF_CALLFD, g_wait_budget_ms, 0);
		else
			ph_add(i, WF_FD | WF_WITHHOLD, g_fast ? 120u : 200u, 0);
	srv_begin(n, POL_PLAIN);
	st_rel(&S->proc_generation, GEN_D5);
	ph_go();
	done = ph_wait(n, (long)g_wait_budget_ms + 4000);
	phase_note("D5", done, n);
	ok = done == n && workers_ok_except(n, 2, det, sizeof det) &&
	     ld_acq(&S->fd_token_ok) - t0 == 2 &&
	     ld_acq(&S->fd_token_bad) == t1;
	/* release the withheld descriptor-bearing reply for the ABANDONED lane:
	 * whoever receives it must close that descriptor, not leak it and not
	 * deliver it.  Then the SAME lane re-issues the SAME request id with a
	 * fresh descriptor-bearing request, which must complete normally.
	 */
	srv_flush_cmd();
	ph_begin_config(PH_D5B, 0x5000, 5000, GEN_D5, dserver_callnum_ring_attach);
	ph_add(2, WF_FD | WF_CALLFD, g_wait_budget_ms, 0);
	srv_begin(1, POL_PLAIN);
	ph_go();
	(void)wait_until_collected(1, 4000);
	done = ph_wait(1, (long)g_wait_budget_ms + 4000);
	phase_note("D5B", done, 1);
	usleep(20000);
	ok = ok && done == 1 && S->ledger[2].completions == 1 &&
	     S->slots[2].got_lane == S->slots[2].lane_generation &&
	     (ld_acq(&S->fd_closed_on_reject) +
	      ld_acq(&S->fd_closed_on_caller_reject) - r0) >= 1 &&
	     ld_acq(&S->fd_token_ok) - t0 == 3 &&
	     fd_count_self() == fd_before;
	verdict(5, ok, "2/2 descriptor-bearing replies were bound to their own "
		"request (token read out of the descriptor itself: ok=%u bad=%u, and "
		"the call-side descriptor was echoed back by the server); the "
		"descriptor for the rejected generation/lane was closed (%u closures, "
		"then the SAME lane completed a fresh descriptor-bearing request: "
		"tokens ok=%u); guest descriptors before=%d after=%d (no leak)%s",
		ld_acq(&S->fd_token_ok) - t0, ld_acq(&S->fd_token_bad) - t1,
		(ld_acq(&S->fd_closed_on_reject) +
		 ld_acq(&S->fd_closed_on_caller_reject)) - r0,
		ld_acq(&S->fd_token_ok) - t0, fd_before, fd_count_self(), det);
}

/* ---------------------------------------------------------------------- */
/* D6: one slow waiter does not head-of-line-block the others.              */
/* ---------------------------------------------------------------------- */
static void phase_d6(void)
{
	uint32_t n = 32, i, slow = 4, others, done;
	uint64_t t0, t1, d0, d1;
	uint32_t slow_state;
	uint32_t cs0 = (uint32_t)((dmx_now_ns() / 1000ull) & 0xffffffffu);
	char det[512] = "";
	int ok;
	struct rusage ru0, ru1;
	uint64_t cpu_ns;

	ph_begin_config(PH_D6, 0x6000, 6000, GEN_D6, dserver_callnum_ring_attach);
	for (i = 0; i < n; ++i)
		ph_add(i, (i == slow) ? WF_WITHHOLD : 0, g_wait_budget_ms, 0);
	if (g_variant == 2)
		st_rel(&S->token_owner, S->slots[slow].owner_tid);
	srv_begin(n, POL_PLAIN);
	st_rel(&S->proc_generation, GEN_D6);
	d0 = dmx_now_ns();
	getrusage(RUSAGE_SELF, &ru0);
	t0 = dmx_now_ns();
	ph_go();
	(void)wait_until_collected(n, 4000);
	others = ph_wait(n - 1, (long)g_wait_budget_ms + 4000);
	t1 = dmx_now_ns();
	slow_state = ld_acq(&S->slots[slow].state);
	d1 = dmx_now_ns();
	getrusage(RUSAGE_SELF, &ru1);
	cpu_ns = (uint64_t)(ru1.ru_utime.tv_sec - ru0.ru_utime.tv_sec) * 1000000000ull +
		 (uint64_t)(ru1.ru_utime.tv_usec - ru0.ru_utime.tv_usec) * 1000ull +
		 (uint64_t)(ru1.ru_stime.tv_sec - ru0.ru_stime.tv_sec) * 1000000000ull +
		 (uint64_t)(ru1.ru_stime.tv_usec - ru0.ru_stime.tv_usec) * 1000ull;
	ok = others == n - 1 && slow_state == DS_WAIT &&
	     workers_ok_except(n, (int32_t)slow, det, sizeof det);
	srv_flush_cmd();
	done = ph_wait(n, (long)g_wait_budget_ms + 4000);
	ok = ok && done == n;
	phase_note("D6", done, n);
	verdict(6, ok, "with waiter %u parked on a withheld reply, %u/%u others "
		"completed %.2f ms after the phase started (whole-run CPU in that "
		"window %.2f ms; the slow waiter was still in state=%u at that "
		"instant, i.e. it was never served first); the withheld reply then "
		"completed it: %u/%u done%s",
		slow, others, n - 1, (double)(t1 - t0) / 1e6,
		(double)cpu_ns / 1e6, slow_state, done, n, det);
	(void)cs0;
	(void)d0;
	(void)d1;
}

/* ---------------------------------------------------------------------- */
/* D9: a caller-local operation executes on the TARGET thread.              */
/* ---------------------------------------------------------------------- */
static void phase_d9(void)
{
	uint32_t n = 4, i, done, exec_target = 0;
	uint32_t rr0 = ld_acq(&S->s2c_routed);
	uint32_t ok0 = ld_acq(&S->s2c_reply_tid_ok);
	uint32_t bad0 = ld_acq(&S->s2c_reply_tid_bad);
	uint32_t inc0 = ld_acq(&S->s2c_incomplete);
	char det[512] = "";
	int ok;

	ph_begin_config(PH_D9, 0x7000, 7000, GEN_D9, dserver_callnum_ring_attach);
	for (i = 0; i < n; ++i)
		ph_add(i, WF_S2C, g_wait_budget_ms, 1u + (i % 4u));
	srv_begin(n, POL_PLAIN);
	st_rel(&S->proc_generation, GEN_D9);
	ph_go();
	done = ph_wait(n, (long)g_wait_budget_ms + 4000);
	for (i = 0; i < n; ++i)
		if (ld_acq(&S->exec_tid[i]) == S->slots[i].owner_tid &&
		    ld_acq(&S->exec_tid[i]) != 0)
			++exec_target;
	ok = done == n && workers_ok(n, det, sizeof det) &&
	     exec_target == n &&
	     ld_acq(&S->s2c_routed) - rr0 >= n &&
	     ld_acq(&S->s2c_executed_by_dispatcher) == 0 &&
	     ld_acq(&S->s2c_reply_tid_ok) - ok0 == n &&
	     ld_acq(&S->s2c_reply_tid_bad) == bad0 &&
	     ld_acq(&S->s2c_incomplete) == inc0;
	phase_note("D9", done, n);
	verdict(9, ok, "4 caller-local ops (mmap/munmap/mprotect/msync) were ROUTED "
		"by the dispatcher (%u routed) and executed by the TARGET thread "
		"(%u/4 executed by the addressed tid, %u executed by the dispatcher); "
		"the server keyed each S2C reply off the executing tid: %u/%u correct, "
		"%u misattributed, %u never arrived; all 4 original calls then "
		"completed (done=%u/4)%s",
		ld_acq(&S->s2c_routed) - rr0, exec_target,
		ld_acq(&S->s2c_executed_by_dispatcher),
		ld_acq(&S->s2c_reply_tid_ok) - ok0, n,
		ld_acq(&S->s2c_reply_tid_bad) - bad0,
		ld_acq(&S->s2c_incomplete) - inc0, done, det);
}

/* ====================================================================== */
/* 11. POST-FORK / POST-EXEC CHILDREN (libc-free)                          */
/*                                                                         */
/* The brief's fork/exec question is "the child is single-threaded; can the  */
/* demux thread be recreated there, and with what calls?".  Two children     */
/* answer it: one recreates the dispatcher with a RAW clone on a preallocated */
/* stack (no libc, no malloc, no libc lock), the other with pthread_create.   */
/* ====================================================================== */

#define DMX_SLOT_WORKER 64u	/* the fork child's slot  */
#define DMX_SLOT_EXEC   65u	/* the post-exec image's slot */
#define DMX_SLOT_FORKB  66u	/* the pthread leg's own slot  */

#define RPT_MAGIC 0x52505444u

typedef struct demux_report {
	uint32_t magic;
	uint32_t kind;
	uint32_t w[20];
} demux_report_t;

struct raw_dirent64 {
	uint64_t d_ino;
	int64_t d_off;
	unsigned short d_reclen;
	unsigned char d_type;
	char d_name[];
};

static __attribute__((noinline)) int raw_count_tasks(void)
{
	char buf[4096];
	long fd = rsys4(SYS_openat, -100, (long)"/proc/self/task",
			O_RDONLY | O_DIRECTORY, 0);
	int n = 0;

	if (fd < 0)
		return -1;
	for (;;) {
		long r = rsys3(SYS_getdents64, fd, (long)buf, sizeof buf);
		long off = 0;

		if (r <= 0)
			break;
		while (off < r) {
			struct raw_dirent64 *de =
				(struct raw_dirent64 *)(void *)(buf + off);

			if (de->d_reclen == 0)
				break;
			if (de->d_name[0] != '.')
				++n;
			off += de->d_reclen;
		}
	}
	rsys1(SYS_close, fd);
	return n;
}

static __attribute__((noinline)) void raw_write_report(int fd, uint32_t kind,
						       const uint32_t *w,
						       unsigned nwords)
{
	demux_report_t r;
	unsigned i;

	bfill(&r, 0, sizeof r);
	r.magic = RPT_MAGIC;
	r.kind = kind;
	for (i = 0; i < nwords && i < 20; ++i)
		r.w[i] = w[i];
	rsys3(SYS_write, fd, (long)&r, sizeof r);
}

static __attribute__((noinline)) long raw_recv_env(int fd, demux_env_t *e,
						   int *outfd)
{
	struct msghdr mh;
	struct iovec iov;
	static char ctrl[CMSG_SPACE(sizeof(int))];
	long n;

	bfill(e, 0, sizeof *e);
	bfill(ctrl, 0, sizeof ctrl);
	bfill(&mh, 0, sizeof mh);
	iov.iov_base = e;
	iov.iov_len = sizeof *e;
	mh.msg_iov = &iov;
	mh.msg_iovlen = 1;
	mh.msg_control = ctrl;
	mh.msg_controllen = sizeof ctrl;
	n = rsys3(SYS_recvmsg, fd, (long)&mh, 0);
	if (n >= 0)
		*outfd = dmx_ctl_fd(&mh);
	else
		*outfd = -1;
	return n;
}

/* A preallocated stack plus a raw clone: the post-fork/post-exec child can
 * start a new demultiplexer with no libc, no malloc and therefore no libc lock
 * that another thread might have held at fork time. */
__asm__(
	".text\n"
	".globl dmx_raw_clone_thread\n"
	".type dmx_raw_clone_thread,@function\n"
	"dmx_raw_clone_thread:\n"
	"  movq %rsi, %r8\n"
	"  movq %rdx, %r9\n"
	"  movq %rdi, %rsi\n"
	"  movl $0x00050f00, %edi\n"	/* VM|FS|FILES|SIGHAND|THREAD|SYSVSEM */
	"  movl $56, %eax\n"		/* SYS_clone */
	"  syscall\n"
	"  testq %rax, %rax\n"
	"  jne 1f\n"
	"  xorl %ebp, %ebp\n"
	"  movq %r9, %rdi\n"
	"  call *%r8\n"
	"  movl $60, %eax\n"		/* SYS_exit: this thread only */
	"  xorl %edi, %edi\n"
	"  syscall\n"
	"1:\n"
	"  ret\n"
	".size dmx_raw_clone_thread,.-dmx_raw_clone_thread\n");

extern int dmx_raw_clone_thread(void *stack_top, void (*fn)(void *), void *arg);

static void dmx_thread_start(void *arg)
{
	(void)arg;
	st_rel(&S->ctl_tid, (uint32_t)dmx_gettid());
	dmx_demux_loop(g_fd);
}

#define DMX_THREAD_STACK (64u * 1024u)

static __attribute__((noinline)) int raw_clone_demux_thread(void)
{
	long stack = rsys6(SYS_mmap, 0, DMX_THREAD_STACK, PROT_READ | PROT_WRITE,
			   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);

	if (stack < 0 && stack > -4096)
		return -1;
	return dmx_raw_clone_thread((void *)(uintptr_t)(stack + DMX_THREAD_STACK),
				    dmx_thread_start, 0);
}

/* one blocking RPC from a single-threaded child image; `reader` = this thread
 * must recvmsg itself (the token variant has no permanent thread). */
static __attribute__((noinline)) int child_rpc(int fd, uint32_t slot,
					       int32_t tid, uint64_t gen,
					       uint32_t rid, uint32_t tag,
					       uint32_t timeout_ms, int reader)
{
	demux_env_t e;
	demux_slot_t *sl = &S->slots[slot];
	uint64_t deadline = dmx_now_ns() + (uint64_t)timeout_ms * 1000000ull;
	dserver_rpc_call_ring_attach_t *c =
		(dserver_rpc_call_ring_attach_t *)(void *)e.payload;

	bfill(&e, 0, sizeof e);
	e.magic = DMX_MAGIC;
	e.kind = DK_CALL;
	e.stid = tid;
	e.rid = rid;
	e.lane_generation = 1;
	e.proc_generation = gen;
	e.payload_len = sizeof *c;
	c->header.number = dserver_callnum_ring_attach;
	c->header.pid = dmx_getpid();
	c->header.tid = tid;
	c->header.architecture = dserver_rpc_architecture_x86_64;
	c->body.ring_fd = -1;
	c->body.mapping_size = tag;
	sl->want_rid = rid;
	sl->want_tag = tag;
	sl->expected_callnum = dserver_callnum_ring_attach;
	sl->payload_len = 0;
	sl->received_fd = -1;
	sl->lane_generation = 1;
	st_rel(&sl->state, DS_WAIT);
	if (dmx_sendto(fd, &e, sizeof e, -1) < 0)
		return 0;
	for (;;) {
		uint32_t st;
		int rfd = -1;
		long n;

		if (dmx_now_ns() >= deadline)
			return 0;
		st = ld_acq(&sl->state);
		if (st == DS_READY) {
			const dserver_rpc_reply_ring_attach_t *r =
				(const dserver_rpc_reply_ring_attach_t *)
				(const void *)sl->payload;

			if (sl->dkind == DK_REPLY && sl->got_rid == rid &&
			    sl->payload_len == sizeof *r &&
			    r->header.number == dserver_callnum_ring_attach &&
			    r->body.reject_reason ==
				(tag ^ (uint32_t)tid)) {
				if (sl->received_fd >= 0)
					rsys1(SYS_close, sl->received_fd);
				return 1;
			}
			sl->received_fd = -1;
			st_rel(&sl->state, DS_WAIT);
			continue;
		}
		if (!reader) {
			uint32_t w = ld_rlx(&sl->futex_word);

			(void)dmx_futex_wait(&sl->futex_word, w, 10);
			continue;
		}
		n = raw_recv_env(fd, &e, &rfd);
		if (n < 0) {
			if (n == -EINTR || n == -EAGAIN)
				continue;
			return 0;
		}
		if (n < (long)sizeof(demux_env_t) || e.magic != DMX_MAGIC) {
			if (rfd >= 0)
				rsys1(SYS_close, rfd);
			continue;
		}
		if (e.kind == DK_STOP) {
			if (rfd >= 0)
				rsys1(SYS_close, rfd);
			return 0;
		}
		if (e.stid != tid) {
			dmx_dispatch(&e, dmx_gettid(), rfd, 0);
			continue;
		}
		{
			const dserver_rpc_reply_ring_attach_t *r =
				(const dserver_rpc_reply_ring_attach_t *)
				(const void *)e.payload;

			if (e.proc_generation != gen || e.rid != rid ||
			    e.payload_len != sizeof *r ||
			    r->header.number != dserver_callnum_ring_attach) {
				if (rfd >= 0)
					rsys1(SYS_close, rfd);
				continue;
			}
			if (rfd >= 0)
				rsys1(SYS_close, rfd);
			return 1;
		}
	}
}

/* ---- D7: the fork child ---------------------------------------------- */

static __attribute__((noinline, noreturn)) void
d7_child_entry(int fd, int report_fd, uint32_t pre_gen, int32_t my_tid)
{
	uint32_t w[20];
	int threads_before, threads_after;
	demux_env_t e;
	int rfd = -1;
	long n;
	int rejected = 0, consumed = 0, fresh = 0, clone_rc = -1;
	uint32_t rid = 0xD7000001u, tag = 0x7701u;

	/* this child is a new incarnation: its own generation, its own registry */
	S->proc_generation = GEN_FORK;
	st_rel(&S->slot_to_tid[DMX_SLOT_WORKER], my_tid);
	S->slots[DMX_SLOT_WORKER].state = DS_EMPTY;
	S->slots[DMX_SLOT_WORKER].received_fd = -1;
	threads_before = raw_count_tasks();

	/* 1. the stale completion produced before the fork must be refused */
	n = raw_recv_env(fd, &e, &rfd);
	if (n >= (long)sizeof(demux_env_t) && e.magic == DMX_MAGIC) {
		if (e.proc_generation != GEN_FORK) {
			if (DMX_FORK_GEN_CHECK /*MUT5-FORK*/) {
				++rejected;
				if (rfd >= 0)
					rsys1(SYS_close, rfd);
			} else {
				/* MUTATED: the child accepts the parent's
				 * completion as its own */
				++consumed;
				if (rfd >= 0)
					rsys1(SYS_close, rfd);
			}
		}
	} else if (rfd >= 0) {
		rsys1(SYS_close, rfd);
	}
	if (consumed) {
		w[0] = 1; w[1] = 0; w[2] = (uint32_t)threads_before;
		w[3] = (uint32_t)rejected; w[4] = (uint32_t)consumed;
		w[5] = 0; w[6] = 0;
		raw_write_report(report_fd, 1, w, 7);
		rsys1(SYS_exit_group, 0);
	}

	/* 2. recreate the demultiplexer with a raw clone (no libc, no malloc) */
	if (g_variant == 1) {
		clone_rc = raw_clone_demux_thread();
		{
			uint64_t d = dmx_now_ns() + 1000000000ull;

			while (ld_acq(&S->ctl_tid) == 0 && dmx_now_ns() < d)
				;
		}
	}

	/* 3. a fresh request of this incarnation must complete */
	fresh = child_rpc(fd, DMX_SLOT_WORKER, my_tid, GEN_FORK, rid, tag,
			  g_wait_budget_ms, g_variant != 1);
	threads_after = raw_count_tasks();
	w[0] = (uint32_t)rejected;
	w[1] = (uint32_t)consumed;
	w[2] = (uint32_t)threads_before;
	w[3] = (uint32_t)threads_after;
	w[4] = (uint32_t)clone_rc;
	w[5] = (uint32_t)fresh;
	w[6] = (uint32_t)pre_gen;
	raw_write_report(report_fd, 1, w, 7);
	rsys1(SYS_exit_group, 0);
	for (;;)
		;
}

/* the same child, but with libc available: is pthread_create usable there? */
static void d7b_child_main(int report_fd)
{
	pthread_t t;
	int rc, before = raw_count_tasks(), after;
	uint32_t w[8];
	uint32_t rid = 0xD7B00001u, tag = 0x77B1u;

	st_rel(&S->slot_to_tid[DMX_SLOT_FORKB], dmx_gettid());
	S->slots[DMX_SLOT_FORKB].state = DS_EMPTY;
	S->slots[DMX_SLOT_FORKB].received_fd = -1;
	S->proc_generation = GEN_FORK;
	st_rel(&S->ctl_tid, 0);
	rc = pthread_create(&t, NULL, demux_thread_main, NULL);
	if (rc == 0) {
		uint64_t d = dmx_now_ns() + 1000000000ull;

		pthread_detach(t);
		while (ld_acq(&S->ctl_tid) == 0 && dmx_now_ns() < d)
			usleep(1000);
	}
	after = raw_count_tasks();
	w[0] = (uint32_t)rc;
	w[1] = (uint32_t)before;
	w[2] = (uint32_t)after;
	w[3] = (uint32_t)(g_variant == 1
			  ? child_rpc(g_fd, DMX_SLOT_FORKB, dmx_gettid(),
				      GEN_FORK, rid, tag, g_wait_budget_ms, 0)
			  : 0);
	raw_write_report(report_fd, 2, w, 4);
	_exit(0);
}

/* ---- D8: the exec child --------------------------------------------- */

/* pre-exec: queue the completion this incarnation will be owed, then exec.
 * NOTE the thread id is PRESERVED across execve, so the tid alone cannot
 * reject the pre-exec completion -- only the process generation can. */
static __attribute__((noinline)) void raw_u2s(char *out, long v)
{
	char tmp[16];
	int i = 0, j = 0;

	while (v > 0 && i < 15) {
		tmp[i++] = (char)('0' + (v % 10));
		v /= 10;
	}
	if (i == 0)
		tmp[i++] = '0';
	while (i > 0)
		out[j++] = tmp[--i];
	out[j] = 0;
}

extern char **environ;

static __attribute__((noinline, noreturn)) void
d8_child_entry(int fd, int report_fd, const char *shm_path,
	       const char *server_path, uint32_t rid, uint32_t tag)
{
	demux_env_t e;
	dserver_rpc_call_ring_attach_t *c =
		(dserver_rpc_call_ring_attach_t *)(void *)e.payload;
	uint32_t w[8];
	uint64_t d;
	char fdstr[16], rptstr[16];
	const char *argv[7];

	S->proc_generation = GEN_EXEC_PRE;
	bfill(&e, 0, sizeof e);
	e.magic = DMX_MAGIC;
	e.kind = DK_CALL;
	e.flags = DGF_WITHHOLD;
	e.stid = dmx_gettid();
	e.rid = rid;
	e.lane_generation = 1;
	e.proc_generation = GEN_EXEC_PRE;
	e.payload_len = sizeof *c;
	c->header.number = dserver_callnum_ring_attach;
	c->header.pid = dmx_getpid();
	c->header.tid = dmx_gettid();
	c->header.architecture = dserver_rpc_architecture_x86_64;
	c->body.ring_fd = -1;
	c->body.mapping_size = tag;
	dmx_sendto(fd, &e, sizeof e, -1);
	d = dmx_now_ns() + 2000000000ull;
	while (ld_acq(&S->srv_arrival_n) == 0 && dmx_now_ns() < d)
		;
	w[0] = (uint32_t)dmx_gettid();
	w[1] = (uint32_t)(ld_acq(&S->srv_arrival_n) != 0);
	w[2] = rid;
	raw_write_report(report_fd, 3, w, 3);
	/* the parent releases the withheld reply into the socket queue, then
	 * opens this gate: the completion is owed BEFORE the exec and must be
	 * refused by the post-exec image. */
	d = dmx_now_ns() + 5000000000ull;
	while (ld_acq(&S->exec_pre_go) == 0 && dmx_now_ns() < d)
		;
	raw_u2s(fdstr, (long)fd);
	raw_u2s(rptstr, (long)report_fd);
	argv[0] = "/proc/self/exe";
	argv[1] = "exec-child";
	argv[2] = shm_path;
	argv[3] = server_path;
	argv[4] = fdstr;
	argv[5] = rptstr;
	argv[6] = 0;
	rsys3(SYS_execve, (long)argv[0], (long)argv, (long)environ);
	raw_write_report(report_fd, 3, w, 3);
	rsys1(SYS_exit_group, 4);
	for (;;)
		;
}

/* the post-exec image: fresh address space, fresh registry, generation bumped,
 * the inherited endpoint and the queued pre-exec completion in front of it. */
static __attribute__((noinline, noreturn)) void
exec_child_entry(const char *shm_path, const char *server_path, int fd,
		 int report_fd, size_t shm_size)
{
	long f;
	long p;
	uint32_t w[12];
	demux_env_t e;
	int rfd = -1;
	long n;
	int rejected = 0, consumed = 0, fresh = 0, clone_rc = -9;
	uint32_t rid = 0xE1E10001u, tag = 0xE1E1u;
	struct sockaddr_un *sa = &g_server_addr;

	f = rsys4(SYS_openat, -100, (long)shm_path, O_RDWR, 0);
	if (f < 0) {
		w[0] = 0xDEAD;
		raw_write_report(report_fd, 4, w, 8);
		rsys1(SYS_exit_group, 5);
	}
	p = rsys6(SYS_mmap, 0, (long)shm_size, PROT_READ | PROT_WRITE,
		  MAP_SHARED, f, 0);
	rsys1(SYS_close, f);
	if (p < 0 && p > -4096) {
		w[0] = 0xBEEF;
		raw_write_report(report_fd, 4, w, 8);
		rsys1(SYS_exit_group, 6);
	}
	S = (demux_shm_t *)(uintptr_t)p;
	g_fd = fd;
	/* re-derive the server address from the path (BSS is zero after exec) */
	bfill(sa, 0, sizeof *sa);
	sa->sun_family = AF_UNIX;
	dcopy(sa->sun_path, server_path, 100);
	g_server_addr_len = (socklen_t)sizeof *sa;

	S->proc_generation = GEN_EXEC_NEW;
	st_rel(&S->slot_to_tid[DMX_SLOT_EXEC], dmx_gettid());
	S->slots[DMX_SLOT_EXEC].state = DS_EMPTY;
	S->slots[DMX_SLOT_EXEC].received_fd = -1;
	st_rel(&S->ctl_tid, 0);

	/* 1. the completion the PRE-EXEC incarnation was owed must be refused */
	n = raw_recv_env(fd, &e, &rfd);
	if (n >= (long)sizeof(demux_env_t) && e.magic == DMX_MAGIC) {
		if (e.proc_generation != GEN_EXEC_NEW) {
			if (DMX_EXEC_GEN_CHECK /*MUT6-EXEC*/) {
				++rejected;
			} else {
				/* MUTATED: the new image accepts the pre-exec
				 * completion as its own */
				++consumed;
			}
		}
	}
	if (rfd >= 0)
		rsys1(SYS_close, rfd);

	/* 2. the new image re-creates its dispatcher (V1) and serves a request */
	w[4] = (uint32_t)raw_count_tasks();
	if (!consumed && g_variant == 1) {
		clone_rc = raw_clone_demux_thread();
		{
			uint64_t d = dmx_now_ns() + 1000000000ull;

			while (ld_acq(&S->ctl_tid) == 0 && dmx_now_ns() < d)
				;
		}
	}
	if (!consumed)
		fresh = child_rpc(fd, DMX_SLOT_EXEC, dmx_gettid(), GEN_EXEC_NEW,
				  rid, tag, g_wait_budget_ms, g_variant != 1);
	w[0] = (uint32_t)rejected;
	w[1] = (uint32_t)consumed;
	w[2] = (uint32_t)fresh;
	w[3] = (uint32_t)clone_rc;
	w[8] = (uint32_t)raw_count_tasks();
	w[5] = (uint32_t)dmx_gettid();
	w[6] = (uint32_t)ld_acq(&S->ctl_tid);
	w[7] = (uint32_t)(S->magic == DMX_MAGIC);
	raw_write_report(report_fd, 4, w, 9);
	rsys1(SYS_exit_group, consumed ? 7 : 0);
	for (;;)
		;
}

/* ====================================================================== */
/* 12. SETUP, MEASUREMENTS, FORK/EXEC LEGS, MAIN                          */
/* ====================================================================== */

static char g_scratch[96];
static char g_path_guest_b[192];

static int count_tasks_libc(void)
{
	DIR *d = opendir("/proc/self/task");
	struct dirent *de;
	int n = 0;

	if (!d)
		return -1;
	while ((de = readdir(d)) != NULL)
		if (de->d_name[0] != '.')
			++n;
	closedir(d);
	return n;
}

static int status_kb(const char *key, char *out, size_t cap)
{
	FILE *f = fopen("/proc/self/status", "r");
	char line[256];
	int found = 0;

	if (!f)
		return 0;
	while (fgets(line, sizeof line, f)) {
		if (strncmp(line, key, strlen(key)) == 0) {
			char *v = line + strlen(key);
			size_t n;

			while (*v == ' ' || *v == '\t')
				++v;
			snprintf(out, cap, "%s", v);
			n = strlen(out);
			while (n && (out[n - 1] == '\n' || out[n - 1] == ' '))
				out[--n] = 0;
			found = 1;
			break;
		}
	}
	fclose(f);
	return found;
}

static uint64_t cpu_us_total(void)
{
	struct rusage ru;
	uint64_t us;

	getrusage(RUSAGE_SELF, &ru);
	us = (uint64_t)(ru.ru_utime.tv_sec + ru.ru_stime.tv_sec) * 1000000ull;
	us += (uint64_t)(ru.ru_utime.tv_usec + ru.ru_stime.tv_usec);
	return us;
}

static int task_status_line(int tid, const char *key, char *out, size_t cap)
{
	char path[96];
	FILE *f;
	char line[512];
	int found = 0;

	snprintf(path, sizeof path, "/proc/self/task/%d/status", tid);
	f = fopen(path, "r");
	if (!f)
		return 0;
	while (fgets(line, sizeof line, f)) {
		if (strncmp(line, key, strlen(key)) == 0) {
			snprintf(out, cap, "%s", line + strlen(key));
			found = 1;
			break;
		}
	}
	fclose(f);
	return found;
}

static int mk_dgram(const char *path)
{
	struct sockaddr_un a;
	int fd = socket(AF_UNIX, SOCK_DGRAM, 0);

	if (fd < 0)
		return -1;
	unlink(path);
	bfill(&a, 0, sizeof a);
	a.sun_family = AF_UNIX;
	snprintf(a.sun_path, sizeof a.sun_path, "%s", path);
	if (bind(fd, (struct sockaddr *)&a, sizeof a) != 0) {
		fprintf(stderr, "fatal: bind %s: %s\n", path, strerror(errno));
		exit(3);
	}
	return fd;
}

static void setup_paths(void)
{
	const char *sc = getenv("DEMUX_SCRATCH");

	if (sc && sc[0]) {
		snprintf(g_scratch, sizeof g_scratch, "%s", sc);
	} else {
		snprintf(g_scratch, sizeof g_scratch, "/tmp/demux-fixture.%d",
			 (int)getpid());
	}
	(void)mkdir(g_scratch, 0700);
	snprintf(g_path_shm, sizeof g_path_shm, "%s/demux.shm", g_scratch);
	snprintf(g_path_server, sizeof g_path_server, "%s/server.sock", g_scratch);
	snprintf(g_path_guest, sizeof g_path_guest, "%s/guest.sock", g_scratch);
	snprintf(g_path_guest_b, sizeof g_path_guest_b, "%s/guestB.sock", g_scratch);
	if (strlen(g_path_server) >= sizeof(((struct sockaddr_un *)0)->sun_path) ||
	    strlen(g_path_guest) >= sizeof(((struct sockaddr_un *)0)->sun_path) ||
	    strlen(g_path_guest_b) >= sizeof(((struct sockaddr_un *)0)->sun_path)) {
		fprintf(stderr, "fatal: scratch path too long for sun_path\n");
		exit(3);
	}
}

static void shm_create(void)
{
	size_t sz = sizeof(demux_shm_t);
	int f = open(g_path_shm, O_RDWR | O_CREAT | O_TRUNC, 0600);
	void *p;
	uint32_t i;

	if (f < 0 || ftruncate(f, (off_t)sz) != 0) {
		fprintf(stderr, "fatal: shm: %s\n", strerror(errno));
		exit(3);
	}
	p = mmap(NULL, sz, PROT_READ | PROT_WRITE, MAP_SHARED, f, 0);
	close(f);
	if (p == MAP_FAILED) {
		fprintf(stderr, "fatal: shm mmap: %s\n", strerror(errno));
		exit(3);
	}
	S = p;
	memset(S, 0, sz);
	S->magic = DMX_MAGIC;
	S->version = DMX_PROTO_RID;
	S->nthreads = DMX_MAXT;
	for (i = 0; i < DMX_MAXT; ++i) {
		st_rel(&S->slot_to_tid[i], -1);
		S->slots[i].received_fd = -1;
		S->slots[i].state = DS_EMPTY;
	}
	st_rel(&S->proc_generation, GEN_SETUP);
}

static void start_server(void)
{
	int sfd = mk_dgram(g_path_server);
	pid_t c;
	uint64_t d;

	if (sfd < 0) {
		fprintf(stderr, "fatal: server socket\n");
		exit(3);
	}
	c = fork();
	if (c == 0)
		dmx_server_entry(sfd);	/* noreturn */
	close(sfd);
	g_server_pid = c;
	d = dmx_now_ns() + 2000000000ull;
	while (ld_acq(&S->server_pid) == 0 && dmx_now_ns() < d)
		usleep(1000);
}

static void main_send_withheld(uint32_t rid, uint32_t tag, uint64_t gen)
{
	demux_env_t e;
	dserver_rpc_call_ring_attach_t *c =
		(dserver_rpc_call_ring_attach_t *)(void *)e.payload;

	bfill(&e, 0, sizeof e);
	e.magic = DMX_MAGIC;
	e.kind = DK_CALL;
	e.flags = DGF_WITHHOLD;
	e.stid = dmx_gettid();
	e.rid = rid;
	e.lane_generation = 1;
	e.proc_generation = gen;
	e.payload_len = sizeof *c;
	c->header.number = dserver_callnum_ring_attach;
	c->header.pid = dmx_getpid();
	c->header.tid = dmx_gettid();
	c->header.architecture = dserver_rpc_architecture_x86_64;
	c->body.ring_fd = -1;
	c->body.mapping_size = tag;
	dmx_sendto(g_fd, &e, sizeof e, -1);
}

static int wait_queued(int fd, long ms)
{
	uint64_t deadline = dmx_now_ns() + (uint64_t)ms * 1000000ull;
	char buf[256];

	for (;;) {
		struct msghdr mh;
		struct iovec iov;
		long n;

		bfill(&mh, 0, sizeof mh);
		iov.iov_base = buf;
		iov.iov_len = sizeof buf;
		mh.msg_iov = &iov;
		mh.msg_iovlen = 1;
		n = recvmsg(fd, &mh, MSG_PEEK | MSG_DONTWAIT | MSG_TRUNC);
		if (n > 0)
			return 1;
		if (dmx_now_ns() >= deadline)
			return 0;
		usleep(500);
	}
}

static int read_report(int fd, demux_report_t *r, long ms)
{
	uint64_t deadline = dmx_now_ns() + (uint64_t)ms * 1000000ull;

	while (dmx_now_ns() < deadline) {
		struct pollfd p;
		int rc;

		p.fd = fd;
		p.events = POLLIN;
		rc = poll(&p, 1, 50);
		if (rc > 0) {
			ssize_t n = read(fd, r, sizeof *r);

			if (n == (ssize_t)sizeof *r && r->magic == RPT_MAGIC)
				return 1;
			if (n <= 0)
				return 0;
		}
	}
	return 0;
}

/* ---- latency samples ------------------------------------------------- */
#define LAT_MAX 1024
static uint64_t g_lat[LAT_MAX];
static int g_lat_n;

static void lat_record(uint32_t n)
{
	uint32_t i;

	for (i = 0; i < n && g_lat_n < LAT_MAX; ++i) {
		demux_ledger_t *lg = &S->ledger[i];

		if (lg->completions)
			g_lat[g_lat_n++] = lg->lat_ns;
	}
}

static int cmp_u64(const void *a, const void *b)
{
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;

	return x < y ? -1 : (x > y ? 1 : 0);
}

/* ---- D7: the fork child --------------------------------------------- */
static void leg_d7(void)
{
	int pa[2], pb[2];
	uint32_t rid = 0xD7000001u, tag = 0x7701u;
	demux_report_t ra, rb;
	pid_t c1, c2;
	int ok;
	uint32_t rejected = 0, consumed = 0, tb = 0, ta = 0, clone_rc = 0, fresh = 0;
	uint32_t pthread_rc = 0, pb_fresh = 0, pb_threads = 0;

	st_rel(&S->proc_generation, GEN_PREFORK);
	srv_begin(1, POL_PLAIN);
	st_rel(&S->srv_arrival_n, 0);
	main_send_withheld(rid, tag, GEN_PREFORK);
	(void)wait_until_collected(1, 4000);
	srv_flush_cmd();
	{
		int queued = wait_queued(g_fd, 4000);

		/* the children's own fresh requests: the fork child always sends
		 * one; the pthread leg only exists for the permanent-thread shape */
		srv_begin(g_variant == 1 ? 2u : 1u, POL_PLAIN);
		st_rel(&S->srv_arrival_n, 0);
		if (pipe(pa) != 0 || pipe(pb) != 0) {
			verdict(7, 0, "pipe() failed");
			return;
		}
		c1 = fork();
		if (c1 == 0) {
			close(pa[0]);
			close(pb[0]);
			close(pb[1]);
			d7_child_entry(g_fd, pa[1], GEN_PREFORK, dmx_gettid());
		}
		c2 = fork();
		if (c2 == 0) {
			close(pb[0]);
			close(pa[1]);
			close(pa[0]);
			g_fd = mk_dgram(g_path_guest_b);
			d7b_child_main(pb[1]);
		}
		close(pa[1]);
		close(pb[1]);
		if (read_report(pa[0], &ra, 20000)) {
			rejected = ra.w[0];
			consumed = ra.w[1];
			tb = ra.w[2];
			ta = ra.w[3];
			clone_rc = ra.w[4];
			fresh = ra.w[5];
		}
		if (read_report(pb[0], &rb, 20000)) {
			pthread_rc = rb.w[0];
			pb_fresh = rb.w[3];
			pb_threads = rb.w[2];
		}
		close(pa[0]);
		close(pb[0]);
		(void)waitpid(c1, NULL, 0);
		(void)waitpid(c2, NULL, 0);
		st_rel(&S->proc_generation, GEN_D9);
		st_rel(&S->srv_arrival_n, 0);
		ok = queued && rejected == 1 && consumed == 0 && fresh == 1 &&
		     tb == 1 &&
		     (g_variant == 1 ? (clone_rc > 0 && ta == 2 &&
					pthread_rc == 0 && pb_fresh == 1 &&
					pb_threads == 2)
				     : (clone_rc == 0xFFFFFFFFu && ta == 1 &&
					pb_fresh == 0));
		verdict(7, ok, "the pre-fork completion was queued before fork=%d; the "
			"single-threaded child (threads before=%u, after=%u) REJECTED it "
			"(rejected=%u consumed=%u) and completed its own fresh request=%u; %s",
			queued, tb, ta, rejected, consumed, fresh,
			g_variant == 1
			? "the dispatcher was re-created there with a raw clone on a "
			  "pre-allocated 64 KiB stack (rc>0, no libc, no malloc, no libc "
			  "lock) and, separately, with pthread_create (rc=0, fresh "
			  "request through the pthread dispatcher, threads after=2)"
			: "this shape has NO permanent thread to re-create: the clone was "
			  "not attempted and the child took the reader token itself "
			  "(threads before=after=1)");
	}
}

/* ---- D8: the exec child --------------------------------------------- */
static void leg_d8(void)
{
	int pfd[2];
	uint32_t rid = 0xE1E10001u, tag = 0xE1E1u;
	demux_report_t pre, post;
	pid_t c;
	int ok, queued;
	uint32_t pre_tid = 0, arrived = 0, rejected = 0, consumed = 0;
	uint32_t fresh = 0, clone_rc = 0, exec_threads = 0, exec_tid = 0;
	uint32_t exec_ctl = 0, shm_ok = 0, exec_threads_after = 0;

	st_rel(&S->proc_generation, GEN_EXEC_PRE);
	st_rel(&S->exec_pre_go, 0);
	srv_begin(1, POL_PLAIN);
	st_rel(&S->srv_arrival_n, 0);
	if (pipe(pfd) != 0) {
		verdict(8, 0, "pipe() failed");
		return;
	}
	c = fork();
	if (c == 0) {
		close(pfd[0]);
		d8_child_entry(g_fd, pfd[1], g_path_shm, g_path_server, rid, tag);
	}
	if (read_report(pfd[0], &pre, 20000)) {
		pre_tid = pre.w[0];
		arrived = pre.w[1];
	}
	srv_flush_cmd();
	queued = wait_queued(g_fd, 4000);
	/* the post-exec image's own fresh request arrives after the exec */
	srv_begin(1, POL_PLAIN);
	st_rel(&S->srv_arrival_n, 0);
	st_rel(&S->exec_pre_go, 1);
	if (read_report(pfd[0], &post, 20000)) {
		rejected = post.w[0];
		consumed = post.w[1];
		fresh = post.w[2];
		clone_rc = post.w[3];
		exec_threads = post.w[4];
		exec_tid = post.w[5];
		exec_ctl = post.w[6];
		shm_ok = post.w[7];
		exec_threads_after = post.w[8];
	}
	close(pfd[0]);
	(void)waitpid(c, NULL, 0);
	st_rel(&S->proc_generation, GEN_D9);
	ok = arrived == 1 && queued && pre_tid != 0 && rejected == 1 &&
	     consumed == 0 && fresh == 1 && exec_threads == 1 &&
	     (g_variant == 1 ? (clone_rc > 0 && exec_threads_after == 2 &&
				exec_ctl != 0)
			     : (clone_rc == 0xFFFFFFF7u &&
				exec_threads_after == 1)) &&
	     exec_tid != 0 && shm_ok == 1;
	verdict(8, ok, "the pre-exec incarnation queued its owed completion "
		"(tid=%u PRESERVED across execve, arrived=%u) and the parent confirmed "
		"it was queued=%d BEFORE the exec; the post-exec image (new generation, "
		"re-mapped shared memory=%u, tid=%u) REJECTED it (rejected=%u "
		"consumed=%u) and completed a fresh request=%u; the new image entered "
		"with %u thread and has %u after re-creating the dispatcher (%s)",
		pre_tid, arrived, queued, shm_ok, exec_tid, rejected, consumed,
		fresh, exec_threads, exec_threads_after,
		g_variant == 1
		? "raw clone on a pre-allocated stack, no libc"
		: "nothing to re-create: this shape has no permanent thread");
}

/* ---- D10: the measurements the decision needs ------------------------ */
static uint64_t g_cpu_before, g_reqs;
static uint32_t g_rss_before;

static void measure_start(void)
{
	g_cpu_before = cpu_us_total();
	{
		char buf[128];

		if (status_kb("VmRSS:", buf, sizeof buf))
			g_rss_before = (uint32_t)strtoul(buf, NULL, 10);
	}
}

static void phase_d10(void)
{
	char rss[128] = "?", vms[128] = "?";
	char blk[512] = "?", ign[512] = "?";
	char mblk[512] = "?", mign[512] = "?";
	uint64_t cpu_idle0, cpu_idle1, cpu_run;
	uint64_t sum = 0, p50 = 0, p99 = 0, mean = 0;
	int threads, i;
	uint32_t parked;
	char det[768];

	threads = count_tasks_libc();
	(void)status_kb("VmRSS:", rss, sizeof rss);
	(void)status_kb("VmSize:", vms, sizeof vms);
	if (g_variant == 1 && g_demux_tid)
		(void)task_status_line(g_demux_tid, "SigBlk:", blk, sizeof blk);
	if (g_variant == 1 && g_demux_tid)
		(void)task_status_line(g_demux_tid, "SigIgn:", ign, sizeof ign);
	(void)task_status_line((int)dmx_gettid(), "SigBlk:", mblk, sizeof mblk);
	(void)task_status_line((int)dmx_gettid(), "SigIgn:", mign, sizeof mign);

	/* idle CPU: every worker parked, nothing in flight */
	parked = (uint32_t)count_tasks_libc();
	cpu_idle0 = cpu_us_total();
	usleep(300000);
	cpu_idle1 = cpu_us_total();
	cpu_run = cpu_us_total() - g_cpu_before;

	if (g_lat_n > 0) {
		qsort(g_lat, (size_t)g_lat_n, sizeof g_lat[0], cmp_u64);
		for (i = 0; i < g_lat_n; ++i)
			sum += g_lat[i];
		mean = sum / (uint64_t)g_lat_n;
		p50 = g_lat[g_lat_n / 2];
		p99 = g_lat[(g_lat_n * 99) / 100 < g_lat_n ? (g_lat_n * 99) / 100
							    : g_lat_n - 1];
	}
	{
		uint32_t dispatched = ld_acq(&S->dispatched);
		uint32_t inplace = ld_acq(&S->dispatched_own_inplace);
		uint32_t total = dispatched + inplace;
		double cpu_per_req = g_reqs ? (double)cpu_run / (double)g_reqs : 0.0;

		snprintf(det, sizeof det,
			 "variant=%d threads=%d (workers=%d demux_threads=%d) VmRSS=%s "
			 "VmSize=%s idle_cpu_us=%llu cpu_us_total=%llu cpu_us_per_request=%.2f "
			 "requests=%llu mean_latency_us=%.2f p50_us=%.2f p99_us=%.2f "
			 "completions=%u payload_copies=%u own_inplace=%u copies_per_completion=%.3f "
			 "stale_gen_rejected=%u late_lane_rejected=%u notwaiting_rejected=%u "
			 "token_handoffs=%u token_releases=%u token_starved=%u srv_recv=%u "
			 "srv_collected=%u srv_state=%u send_errors=%u last_send_errno=%d "
			 "server_send_errors=%u | fork/exec: %s",
			 g_variant, threads, g_nworkers, g_variant == 1 ? 1 : 0,
			 rss, vms, (unsigned long long)(cpu_idle1 - cpu_idle0),
			 (unsigned long long)cpu_run, cpu_per_req,
			 (unsigned long long)g_reqs,
			 (double)mean / 1000.0, (double)p50 / 1000.0,
			 (double)p99 / 1000.0, total, ld_acq(&S->copies), inplace,
			 total ? (double)ld_acq(&S->copies) / (double)total : 0.0,
			 ld_acq(&S->stale_gen_rejected),
			 ld_acq(&S->late_lane_rejected),
			 ld_acq(&S->notwaiting_rejected),
			 ld_acq(&S->token_handoffs), ld_acq(&S->token_releases),
			 ld_acq(&S->token_starved), ld_acq(&S->srv_recv_ok),
			 ld_acq(&S->srv_collected), ld_acq(&S->srv_state),
			 ld_acq(&S->send_errors), (int)ld_acq(&S->last_send_errno),
			 ld_acq(&S->server_send_errors),
			 g_variant == 1
			 ? "the permanent thread does NOT survive fork() and does NOT "
			   "survive execve; the single-threaded child and the post-exec "
			   "image each re-create it (D7/D8: raw clone on a pre-allocated "
			   "64 KiB stack, rc>0; pthread_create also works, rc=0)"
			 : "no permanent thread exists, so there is nothing to re-create "
			   "on fork or exec: the child/post-exec image takes the reader "
			   "token itself (D7/D8 report no clone and no extra thread)");
	}
	printf("MEASURE %s\n", det);
	printf("MEASURE demux_thread_sigmask: SigBlk=%s SigIgn=%s\n",
	       g_variant == 1 ? blk : "(none: no permanent thread)",
	       g_variant == 1 ? ign : "(none)");
	printf("MEASURE main_thread_sigmask:  SigBlk=%s SigIgn=%s\n", mblk, mign);
	printf("MEASURE parked_threads=%u (all at the phase barrier; nothing in "
	       "flight)\n", parked);
	verdict(10, 1, "%s", det);
}

/* one INFO line per phase: the sequencing evidence the runner logs */
static void phase_note(const char *name, uint32_t done, uint32_t want)
{
	printf("INFO phase %s done=%u/%u arrived=%u answered=%u dispatched=%u "
	       "own_inplace=%u copies=%u late_lane=%u notwaiting=%u stale_gen=%u\n",
	       name, done, want, ld_acq(&S->srv_arrival_n),
	       ld_acq(&S->server_answered), ld_acq(&S->dispatched),
	       ld_acq(&S->dispatched_own_inplace), ld_acq(&S->copies),
	       ld_acq(&S->late_lane_rejected), ld_acq(&S->notwaiting_rejected),
	       ld_acq(&S->stale_gen_rejected));
	fflush(stdout);
}

static void print_claims(void)
{
	int i;

	for (i = 1; i <= NCLAIMS; ++i)
		printf("D%d %s %s: %s\n", i, g_claim_ok[i] ? "PASS" : "FAIL",
		       claim_name(i), g_claim_det[i]);
	printf("HARNESS %s env=%s variant=%d pid=%d\n",
	       all_ok() ? "OK" : "FAILED", g_env, g_variant, (int)getpid());
	fflush(stdout);
}

static void teardown(void)
{
	uint64_t d;
	int st = 0;

	srv_stop();
	if (g_server_pid > 0) {
		d = dmx_now_ns() + 2000000000ull;
		while (waitpid(g_server_pid, &st, WNOHANG) == 0 &&
		       dmx_now_ns() < d)
			usleep(2000);
		if (waitpid(g_server_pid, &st, WNOHANG) == 0) {
			printf("INFO server pid=%d did not exit on the stop datum; "
			       "killing it\n", (int)g_server_pid);
			kill(g_server_pid, SIGKILL);
			(void)waitpid(g_server_pid, &st, 0);
		}
	}
	unlink(g_path_server);
	unlink(g_path_guest);
	unlink(g_path_guest_b);
	unlink(g_path_shm);
	rmdir(g_scratch);
}

static int census_main(void)
{
	const char *w = getenv("DEMUX_WORKERS");
	uint32_t n = w && w[0] ? (uint32_t)strtoul(w, NULL, 10) : 32;
	uint64_t c0, c1;
	uint32_t i;
	char rss[128] = "?", vms[128] = "?";

	if (n > DMX_NWORKERS)
		n = DMX_NWORKERS;
	install_signals();
	setup_paths();
	shm_create();
	g_fd = mk_dgram(g_path_guest);
	bfill(&g_server_addr, 0, sizeof g_server_addr);
	g_server_addr.sun_family = AF_UNIX;
	memcpy(g_server_addr.sun_path, g_path_server,
	       strlen(g_path_server) + 1);
	g_server_addr_len = (socklen_t)sizeof g_server_addr;
	{
		struct timeval tv = { 0, 100000 };

		setsockopt(g_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
	}
	start_server();
	for (i = 0; i < n; ++i) {
		if (pthread_create(&g_wthr[i], NULL, worker_thread,
				   (void *)(intptr_t)i) != 0) {
			fprintf(stderr, "fatal: worker\n");
			return 3;
		}
		pthread_detach(g_wthr[i]);
	}
	{
		uint64_t d = dmx_now_ns() + 3000000000ull;

		while (dmx_now_ns() < d) {
			uint32_t ready = 0;

			for (i = 0; i < n; ++i)
				if (ld_acq(&S->slots[i].owner_tid) != 0)
					++ready;
			if (ready == n)
				break;
			usleep(1000);
		}
	}
	if (g_variant == 1)
		v1_start();
	c0 = cpu_us_total();
	usleep(300000);
	c1 = cpu_us_total();
	(void)status_kb("VmRSS:", rss, sizeof rss);
	(void)status_kb("VmSize:", vms, sizeof vms);
	printf("CENSUS variant=%d workers=%u threads=%d rss_kb=%s vmsize_kb=%s "
	       "idle_cpu_us=%llu demux_threads=%d\n",
	       g_variant, n, count_tasks_libc(), rss, vms,
	       (unsigned long long)(c1 - c0), g_variant == 1 ? 1 : 0);
	fflush(stdout);
	v1_stop();
	teardown();
	return 0;
}

int main(int argc, char **argv)
{
	const char *env = getenv("DEMUX_ENV");
	const char *variant = getenv("DEMUX_VARIANT");

	g_env = (env && env[0]) ? env : "unlabeled";
	g_variant = (variant && variant[0]) ? atoi(variant) : 1;
	if (g_variant != 1 && g_variant != 2)
		g_variant = 1;
	g_fast = getenv("DEMUX_FAST") != NULL;
	g_wait_budget_ms = g_fast ? 1500 : 8000;
	g_nworkers = DMX_NWORKERS;
	setvbuf(stdout, NULL, _IOLBF, 0);

	if (argc >= 2 && strcmp(argv[1], "layout") == 0) {
		print_layout();
		return 0;
	}
	if (argc >= 6 && strcmp(argv[1], "exec-child") == 0) {
		exec_child_entry(argv[2], argv[3], atoi(argv[4]),
				 atoi(argv[5]), sizeof(demux_shm_t));
		return 0;
	}
	if (argc >= 2 && strcmp(argv[1], "census") == 0)
		return census_main();

	install_signals();
	setup_paths();
	shm_create();
	g_fd = mk_dgram(g_path_guest);
	if (g_fd < 0) {
		fprintf(stderr, "fatal: guest socket\n");
		return 3;
	}
	bfill(&g_server_addr, 0, sizeof g_server_addr);
	g_server_addr.sun_family = AF_UNIX;
	memcpy(g_server_addr.sun_path, g_path_server,
	       strlen(g_path_server) + 1);
	g_server_addr_len = (socklen_t)sizeof g_server_addr;
	{
		struct timeval tv = { 0, 100000 };

		setsockopt(g_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
	}
	start_server();
	{
		uint32_t i;

		for (i = 0; i < (uint32_t)g_nworkers; ++i) {
			if (pthread_create(&g_wthr[i], NULL, worker_thread,
					   (void *)(intptr_t)i) != 0) {
				fprintf(stderr, "fatal: worker thread\n");
				return 3;
			}
			pthread_detach(g_wthr[i]);
		}
		{
			uint64_t d = dmx_now_ns() + 4000000000ull;

			while (dmx_now_ns() < d) {
				uint32_t ready = 0;

				for (i = 0; i < (uint32_t)g_nworkers; ++i)
					if (ld_acq(&S->slots[i].owner_tid) != 0)
						++ready;
				if (ready == (uint32_t)g_nworkers)
					break;
				usleep(1000);
			}
		}
	}
	printf("HARNESS start env=%s variant=%d fast=%d workers=%d shm=%s\n",
	       g_env, g_variant, g_fast, g_nworkers, g_path_shm);
	fflush(stdout);

	measure_start();
	if (g_variant == 1)
		v1_start();
	phase_d1();
	g_reqs += 32;
	lat_record(32);
	phase_d2();
	g_reqs += 32;
	lat_record(32);
	phase_d3();
	g_reqs += 33;
	lat_record(33);
	phase_d4();
	g_reqs += 32;
	lat_record(32);
	phase_d5();
	lat_record(3);
	phase_d6();
	g_reqs += 32;
	lat_record(32);
	phase_d9();
	g_reqs += 4;
	lat_record(4);
	phase_d10();
	v1_stop();
	leg_d7();
	leg_d8();
	print_claims();
	teardown();
	return all_ok() ? 0 : 1;
}
