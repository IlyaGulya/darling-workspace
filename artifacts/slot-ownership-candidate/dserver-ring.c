// perf #18 (dar-dar6x4-perf-5dq.30) P3: GUEST-side shared-memory ring transport.
//
// Builds the guest's ring memfd, negotiates it via the ring_attach RPC, and provides a
// fast path for eligible calls (P3: task_self_trap) that publishes a request onto the c2s
// ring + wakes the server (via the wake eventfd the server handed back) and waits for the
// reply on the s2c ring -- skipping the UDS sendmsg/recvmsg round-trip that the profile
// showed is ~90% empty cross-process wakeup latency.
//
// LIBC-FREE: raw LINUX_SYSCALL only, exactly like the rest of the emulation layer. Entire
// file compiles to nothing unless DARLING_RING_TRANSPORT is defined.
//
// P3 SCOPE (deliberately minimal, single-trust-boundary-exercise):
//   * ONE ring per process, owned by the thread that first attaches it. Other threads see a
//     foreign owner and fall back to UDS (full per-thread rings are P4). This sidesteps
//     guest TLS (which is fragile this early in the emulation layer) while staying correct:
//     a non-owner never touches the ring.
//   * Only the no-arg port traps (task_self_trap, mach_reply_port) ride the ring; the server's
//     C2S service loop allowlists the same. task_self_trap is cached per-process (perf #7) so
//     it's correctness-first; mach_reply_port is the uncached hot op that exercises the win.
//   * Adaptive wait: bounded spin (reusing the DARLING_GUEST_RECVSPIN budget) then a
//     cross-process FUTEX_WAIT on s2c_futex. Server wakes via FUTEX_WAKE (non-private).

#ifdef DARLING_RING_TRANSPORT

// Make the ring ABI (structs + SPSC helpers) visible; the server-only attach-check is gated
// out (we never validate our own ring -- the SERVER is the trust boundary).
#define DSERVER_RING_TRANSPORT 1
#define DSERVER_RING_NO_ATTACH_CHECK 1

#include <darling/emulation/common/base.h>
#include <darling/emulation/common/simple.h>
#include <darling/emulation/conversion/duct_errno.h>
#include <darling/emulation/linux_premigration/linux-syscalls/linux.h>
#include <darling/emulation/linux_premigration/resources/dserver-ring.h>
#include <darling/emulation/linux_premigration/elfcalls_wrapper.h>


#include <darlingserver/rpc.h>
#include <darlingserver/rpc-supplement.h>

#include <mach/kern_return.h>

// perf#26 RING-MACH-MSG: the two interrupt-observing option bits, declared locally rather than by
// pulling <mach/message.h> into this libc-free translation unit. Values from osfmk/mach/message.h.
#define GR_MACH_SEND_INTERRUPT 0x00000040u
#define GR_MACH_RCV_INTERRUPT  0x00000400u

#include <stdint.h>
#include <stdbool.h>

extern void* memcpy(void* dest, const void* src, __SIZE_TYPE__ n);
extern char* getenv(const char* name); // the environment, for the diagnostic hatches only

// The generated ring_attach wrapper.
extern int dserver_rpc_ring_attach(int ring_fd, uint64_t mapping_size, uint32_t* out_reject_reason, int* out_wake_fd);

// --- ring geometry (must satisfy dserver_ring_shm_validate on the server) -----------------
#define GR_SLOT_SIZE   128u   // >= sizeof(slot)+reply; in [64,4096]
#define GR_SLOT_COUNT  8u     // power of two in [2,1024]

#ifndef DARLING_GUEST_RECVSPIN
#define DARLING_GUEST_RECVSPIN 512
#endif

// futex op constants (avoid pulling a libc header; cross-PROCESS so NO private flag).
#define GR_FUTEX_WAIT 0
#define GR_FUTEX_WAKE 1

// --- PER-THREAD ring lanes (perf #18 D16, dar-1il.11) -------------------------------------
//
// P3 shipped ONE process-wide ring owned by the first-attaching TID; every OTHER thread saw a foreign
// owner and fell back to UDS. D15a measured the cost: ~2053 POST-attach eligible calls scattered to UDS
// because the eligible-op traffic is multi-thread but the ring was single-thread. D16 gives EACH guest
// thread its own SPSC lane (its own memfd/mapping/wake-fd/seq), so a non-owner thread's eligible ops ride
// the ring instead of UDS. Each lane is still strictly SPSC (one guest thread <-> the server); we never
// share a ring across threads (that would reintroduce the producer race the SPSC correctness depends on
// NOT having). On lane exhaustion a thread cleanly UDS-falls-back -- never blocks, never shares.
//
// STORAGE = a process-global CATALOG indexed by hashed gettid (NOT __thread). __thread is unsafe this far
// below libSystem: the perf#9 sleep accountant (rpc-sleep-account.h) records "TLS aborts here pre-pthread",
// and P3 chose single-owner precisely to dodge guest TLS fragility. The catalog is keyed by TID and keeps
// the SPSC-per-thread invariant (each live TID maps to exactly one lane) WITHOUT relying on the emulation
// layer's TLS machinery -- the storage mechanism is an impl detail; the per-thread SPSC lane is the
// invariant. The bookkeeping (active bit published LAST on acquire, generation bumped on every reclaim) is
// the contract pinned by tests/ring_multilane_gate_test.c (the dar-my8 exhaustive gate).
//
// perf#27 replaced the fixed table with the paged catalog defined below. The history matters: perf#18 D18a
// had already had to raise the cap 64 -> 128 because a 96-worker process wanted ~97 lanes and exhausted
// ~39k eligible ops to UDS at 64. That was a SIZE fix to a table that could not shrink; the catalog grows
// to the peak instead, and released slots are reused, so the ceiling is no longer a workload parameter.

typedef struct {
	// `active` is the free/used marker AND the publish gate: a lane is usable iff active==1, and acquire
	// sets active=1 LAST (release order) so a concurrent lookup never sees a half-initialized lane.
	uint32_t active;       // 0 = free, 1 = live (atomic; published last on acquire, cleared first on release)
	uint32_t generation;   // bumped on every (re)claim so a recycled TID can't match a stale lane epoch
	int      state;        // 0=untried, 1=attached, -1=this lane failed (UDS for this thread)
	int      owner_tid;    // gettid of the owning thread
	void*    map;
	uint64_t size;
	int      wake_fd;
	uint32_t seq;          // monotonic request seq for THIS lane (single-owner -> no race)
	int      publisher_tid; // perf#27 #7: gettid() of the thread that published the request currently in
	                        // flight. The lane is strictly SPSC, so this is an ASSERTION: the caller-S2C
	                        // upcall for that request must be executed by this same guest thread. A
	                        // mismatch means the duplex mailbox was serviced by the wrong thread, which is
	                        // exactly the class of bug the Call-owned transport context exists to prevent.
	uint32_t slot_index;   // perf#27: global catalog index, DIAGNOSTIC ONLY (trace/stat lines). The catalog
	                       // is paged now, so `L - g_lanes` -- the old lane identity in every trace line --
	                       // no longer exists; this is the same number, assigned once at page creation.
	struct gr_proc_lane_rec* proc_rec; // perf#30: the process-global record for this incarnation, or 0
	uint32_t borrowed;     // perf#30: 1 == this image did NOT create this lane, it ADOPTED the
	                       // process-global incarnation (another image holds the mapping and therefore
	                       // owns the munmap). A borrowed view is released locally and never unmaps.
} gr_lane_t;

// perf#27 (MILESTONE B, sections 19-22): SEGMENTED / GROWABLE lane catalog, replacing
// `static gr_lane_t g_lanes[GR_MAX_LANES]`.
//
// WHY the fixed table was not acceptable: `GR_MAX_LANES=128` with no reclamation meant the 129th
// historical thread fell back to UDS for every non-fd RPC, and a process with more than 128
// SIMULTANEOUS eligible-op threads could not be served by the Ring at all. Neither is compatible with
// "Ring is the default RPC transport".
//
// The catalog is a singly-linked list of fixed-size pages, allocated lazily and NEVER moved:
//   page 0: lanes 0..127, page 1: 128..255, ...
//   * stable addresses   -> a lane's address is valid for its whole lifetime. This is what lets an old
//     Call keep a shared_ptr to its OLD server-side RingBuffer incarnation while the catalog slot is
//     recycled underneath it; nothing ever has to re-look-up a lane by TID (section 17).
//   * lazy growth        -> a process using one lane pays one page, not GR_LANE_MAX_PAGES.
//   * self-reclamation   -> a released slot is reused in place, so growth tracks PEAK CONCURRENCY, never
//     the number of historical threads (section 23).
// Pages come from the same LINUX_SYSCALL(mmap) the ring mapping already uses: there is no guest malloc
// this low in libSystem, and MAP_PRIVATE|MAP_ANONYMOUS pages are zero-filled and individually owned.
// Each logical lane stays strictly SPSC; nothing here shares a ring across threads.
#define GR_LANE_PAGE_SIZE 128u  // page 0 is exactly the historical fixed table
#define GR_LANE_MAX_PAGES 32u   // 32*128 = 4096 lanes; beyond it -> UDS fallback, counted, never shared
#define GR_MAX_LANES GR_LANE_PAGE_SIZE  // back-compat alias (host gate test / diagnostics read it)

typedef struct gr_lane_page {
	struct gr_lane_page* next;                    // RELEASE-published AFTER lanes[] is initialized
	gr_lane_t            lanes[GR_LANE_PAGE_SIZE];
} gr_lane_page_t;

static gr_lane_page_t* g_lane_pages = 0;      // chain head; NULL until the first attach
// perf#30: statically reserved slots for BORROWED views (adoption needs no mmap, so it works even in the
// earliest image code where catalog growth would issue an emulated mmap RPC).
#define GR_BORROWED_SLOTS 8
static gr_lane_t g_borrowed[GR_BORROWED_SLOTS];
static int gr_borrowed_lane_usable(gr_lane_t* L, int tid) {
	return __atomic_load_n(&L->active, __ATOMIC_ACQUIRE) == 1 && L->owner_tid == tid;
}
static gr_lane_page_t* g_lane_tail = 0;       // append point; only touched under g_lane_grow_lock
static uint32_t        g_lane_page_count = 0; // pages appended so far
static uint32_t        g_lane_grow_lock = 0;  // spinlock; growth happens at most GR_LANE_MAX_PAGES times
static int             g_lanes_exhausted_logged = 0;

// perf #18 D17 (dar-1il.12): GUEST-side lane stats (RECON-only; plain process-global counters bumped on
// the slow attach path, NOT the hot publish path). Dumped to stderr once per process at clean exit when
// DARLING_GUEST_LANE_STATS=1 (the same libc-free stderr discipline as the duplex selftest; default OFF ->
// the dump function early-returns and these counters are simply never read). They answer the D17 C-vs-A
// question from the guest side: lanes_acquired (successful attaches in THIS process), lanes_exhausted
// (claims that found the table full -> UDS fallback = reason C if >0), lanes_reclaimed (a slot reused after
// a prior epoch -> generation bumped from nonzero).
static uint64_t g_stat_lanes_acquired = 0;
// perf#28 (ONE doorbell): writes on the shared process doorbell, and the fd the shared loader
// resolved for this image. Printed by lane-stats so the product evidence NAMES the singleton.
static uint64_t g_stat_doorbell_writes = 0;
// perf#28 diagnosis: WHICH published server state made the guest doorbell. The four buckets are
// DSERVER_RING_SRV_SLEEPING_EPOLL(0)/ACTIVE_POLLING(1)/SLEEP_ARMED(2)/RETIRED(3); a write under
// ACTIVE_POLLING would be an unconditional-doorbell bug, while SLEEPING/SLEEP_ARMED writes are the
// protocol working as designed. Printed by lane-stats so the hot-path claim is measured, not asserted.
// Plain storage with atomic ACCESS (the file's convention): every guest thread of the process
// increments these, so a plain ++ loses updates and made the histogram disagree with the write
            // counter.
static uint64_t g_stat_dw_state[4] = {0, 0, 0, 0};
static uint64_t g_stat_dw_active_seen = 0;
static uint64_t g_stat_lanes_exhausted = 0;
static uint64_t g_stat_lanes_reclaimed = 0;
static uint64_t g_stat_lanes_released = 0;

static dserver_ring_shm_t* gr_cb(gr_lane_t* L)  { return (dserver_ring_shm_t*)L->map; }
static dserver_ring_t* gr_c2s(gr_lane_t* L)     { return (dserver_ring_t*)((char*)L->map + gr_cb(L)->c2s_ring_off); }
static dserver_ring_t* gr_s2c(gr_lane_t* L)     { return (dserver_ring_t*)((char*)L->map + gr_cb(L)->s2c_ring_off); }

typedef enum {
	GR_WAIT_COMPLETED = 0,
	GR_WAIT_COMMITTED_UNKNOWN = 1,
} gr_wait_state_t;

typedef struct {
	gr_wait_state_t state;
	dserver_ring_slot_t* slot;
} gr_wait_result_t;

// Find THIS thread's already-attached lane, or NULL. Probes from a hashed start so distinct TIDs spread
// across the table. A lane matches iff it is active for our TID (the active read is acquire-ordered, so
// if we observe active==1 the lane is fully initialized -- the gate's INVARIANT 1).
static inline void gr_relax(void); // defined next to the futex constants below; used by the catalog
static int gr_environ_has(const char* key, __SIZE_TYPE__ keylen); // defined with the recon hatches below

// perf#29: non-static face of the same reader, for TUs that are not this one.
int __dserver_ring_environ_has(const char* key, __SIZE_TYPE__ keylen);
// Reads a POSITIVE DECIMAL value for `prefix` (e.g. "FOO=") from /proc/self/environ; returns `defl` if
// unset/zero/garbage. Same libc-free scan discipline as gr_environ_has.
static int gr_environ_int(const char* prefix, __SIZE_TYPE__ plen, int defl);
static int gr_trace_enabled(void); // defined with the recon hatches below
// perf#27: the per-op transport trace. It used to be UNCONDITIONAL, which put an fprintf-equivalent
// (a full guest write syscall, through the server, per line) inside the transport hot path: measured logs
// carried ~1768 trace lines for a 400-op benchmark, i.e. ~4.4 lines per operation, and every P1/P2 number
// taken before this gate includes that cost. It is now opt-in (DARLING_GUEST_RING_TRACE=1), scanned once
// with the same libc-free discipline as the other recon hatches.
static int gr_trace_enabled(void) {
	static int cached = -1;
	if (cached < 0) {
		cached = gr_environ_has("DARLING_GUEST_RING_TRACE=1", sizeof("DARLING_GUEST_RING_TRACE=1") - 1);
	}
	return cached;
}

static uint64_t gr_duplex_dropped_replies = 0; // M5: completions the guest suppressed after doing the work
static uint64_t gr_duplex_uid_mutated = 0;     // M3: completions published with a stale/wrong upcall id
static uint64_t gr_duplex_dup_replies = 0;     // M6: completions replayed a second time

// perf#27: the per-op transport trace lines are opt-in; see gr_trace_enabled().
#define GR_TRACE(...) do { if (gr_trace_enabled()) { __simple_printf(__VA_ARGS__); } } while (0)

#ifdef __x86_64__
static int __diag_ring_tid(void) {
	long ret;
	__asm__ volatile("syscall" : "=a"(ret) : "a"(186L) : "rcx", "r11", "memory");
	return (int)ret;
}
#else
static int __diag_ring_tid(void) { return 0; }
#endif

// perf#30 DIAGNOSIS: WHERE IN THE PLANE PUBLICATION DID THE THREAD DIE. MEASURED: a guest SIGSEGV inside this
// function left exactly one line of evidence (`modrefs-entry` with no exit) and the in-program crash reporter
// never ran, because the fault is taken by the guest's own emulated-fault machinery. Step markers are therefore
// emitted from INSIDE this path: the last step printed before the death names the statement. Read once, from
// /proc/self/environ with raw syscalls, never `getenv`: this function is reached during bootstrap.
static long __plane_step_sys4(long n, long a, long b, long c, long d) {
#ifdef __x86_64__
	long r;
	__asm__ volatile("movq %4, %%r10\n\tsyscall"
		: "=a"(r) : "a"(n), "D"(a), "S"(b), "d"(c), "rm"(d) : "rcx", "r11", "r10", "memory");
	return r;
#else
	(void)n; (void)a; (void)b; (void)c; (void)d; return -1;
#endif
}

static int __plane_steps_enabled(void) {
	static int cached = -1;
	if (cached >= 0) { return cached; }
	cached = 0;
	static const char key[] = "DARLING_GUEST_PLANE_STEPS=1";
	static char buf[8192];
	long fd = __plane_step_sys4(257, -100, (long)"/proc/self/environ", 0, 0);
	if (fd >= 0) {
		long n = __plane_step_sys4(0, fd, (long)buf, (long)(sizeof(buf) - 1), 0);
		(void)__plane_step_sys4(3, fd, 0, 0, 0);
		if (n > 0) {
			buf[n] = '\0';
			for (long i = 0; i + (long)(sizeof(key) - 1) <= n; ++i) {
				int ok = 1;
				for (long j = 0; j < (long)(sizeof(key) - 1); ++j) {
					if (buf[i + j] != key[j]) { ok = 0; break; }
				}
				if (ok) { cached = 1; break; }
			}
		}
	}
	return cached;
}

static int __dserver_ring_env_cache_ready = 0;

// File-scope now: the step markers live in TWO plane publishers in this file, and a macro defined inside one of
// them is not visible to the other (MEASURED: that is exactly how three markers landed in a function that did not
// declare them). `op` is the name every publisher already uses for the operation.
#ifdef __x86_64__
#define __PLANE_STEP_SP(out) __asm__ volatile("movq %%rsp, %0" : "=r"(out))
#else
#define __PLANE_STEP_SP(out) do { (out) = 0; } while (0)
#endif

#define __MLDR_PLANE_STEP(step, opv) do { \
	static int __ps_n = 0; \
	if (__plane_steps_enabled() && __ps_n < 200) { \
		++__ps_n; \
		uintptr_t __sp = 0; \
		__PLANE_STEP_SP(__sp); \
		__simple_fprintf(2, "[plane-step n=%d step=" step " op=%u tid=%d sp=%p]\n", \
			__ps_n, (unsigned)(opv), __diag_ring_tid(), (void*)__sp); \
	} \
} while (0)





// perf#30 (PROCESS-GLOBAL LANE DIRECTORY, client side). The loader owns one record per Linux host tid;
// every image resolves the SAME record, so one thread has exactly one logical lane incarnation no matter
// how many images execute its code. The pointer is fetched ONCE per image and cached: the hot path stays
// the local catalog walk, and the directory is consulted only when the local view is absent or retired.
struct gr_proc_lane_rec {
	volatile int32_t  host_tid;
	volatile uint32_t state;
	volatile uint32_t generation;
	volatile uint32_t slot_index;
	volatile int32_t  owner_image;
	volatile void*    mapping;
	volatile uint64_t mapping_size;
	volatile uint32_t next_seq;   // the incarnation's request sequence: ONE counter for every image
	volatile uint32_t creator_image;
};
#define GR_PROC_LANE_ACTIVE 2u
static struct gr_proc_lane_rec* g_proc_lanes = 0;
static int g_proc_lane_slots = 0;
static int g_proc_lane_probed = 0;
static int g_proc_probe_allowed = 0; // set once an attach in THIS image has proven the elfcall table live
#ifdef VARIANT_DYLD
#define GR_IMAGE_ID 1
#else
#define GR_IMAGE_ID 2
#endif
static uint64_t g_stat_proc_adopted = 0;      // Ring requests served through an ADOPTED incarnation
static uint64_t g_stat_proc_published = 0;    // incarnations this image created and published
static uint64_t g_stat_proc_attach_arbitrated = 0; // attaches this image performed because it won the CAS
static uint64_t g_stat_proc_attach_yielded = 0;    // attaches this image skipped because it lost the CAS
static uint64_t g_stat_nonfd_uds_violation = 0; // an ACTIVE process lane existed, the op is non-fd Ring-capable, and the client still chose UDS         // hatch: an OWNED lane re-entered through the adopt path
static int gr_recon_force_readopt_cached = -1;
// Deterministic cross-image gate (diagnostic build hatch). The PRODUCT workloads here never put two
// images on the SAME host tid, so the adopt path -- the whole point of the process-global directory --
// would otherwise be exercised zero times and "ownership is shared" would be an assertion, not a
// measurement. This hatch forces the very code path an adopting image takes (registry lookup -> borrowed
// view over the SAME mapping -> sequence drawn from the incarnation) for a lane this image already owns:
// it drops ONLY the local catalog view (no munmap, no unpublish) and re-enters through adoption.
static int gr_recon_force_readopt(void) {
	if (gr_recon_force_readopt_cached < 0) {
		gr_recon_force_readopt_cached = gr_environ_has("DARLING_GUEST_FORCE_READOPT=1", sizeof("DARLING_GUEST_FORCE_READOPT=1") - 1);
	}
	return gr_recon_force_readopt_cached;
}

static struct gr_proc_lane_rec* gr_proc_registry(void) {
	// The gate is SELF-VALIDATING: the directory exists iff the loader answers with a pointer and a
	// capacity. It must NOT depend on the process doorbell -- measured: right after the loader's handoff
	// the doorbell query returns -1 (the loader's variable is reset even though the fd stays open), which
	// closed this gate and made the adopting image attach a SECOND lane for the same tid; the server then
	// retired the first incarnation and boot wedged with the registry elfcalls answering perfectly.
	// Round 20's "too early to probe" hazard is therefore covered by the elfcall itself, not by a doorbell.
	if (!g_proc_probe_allowed) {
		if (__dserver_ring_lane_registry() == 0 || __dserver_ring_lane_slots() <= 0) {
			return 0; // an older loader: no directory to consult
		}
		g_proc_probe_allowed = 1;
	}
	if (!g_proc_lane_probed) {
		g_proc_lane_probed = 1;
		int slots = __dserver_ring_lane_slots();
		if (slots > 0) {
			struct gr_proc_lane_rec* reg = (struct gr_proc_lane_rec*)__dserver_ring_lane_registry();
			if (reg) {
				g_proc_lanes = reg;
				g_proc_lane_slots = slots;
			}
		}
	}
	return g_proc_lanes;
}
static uint32_t gr_proc_hash(int tid) {
	return ((uint32_t)tid * 2654435761u) % (uint32_t)g_proc_lane_slots;
}
static struct gr_proc_lane_rec* gr_proc_find_active(int tid) {
	if (!gr_proc_registry()) {
		return 0;
	}
	uint32_t start = gr_proc_hash(tid);
	for (int i = 0; i < g_proc_lane_slots; ++i) {
		struct gr_proc_lane_rec* r = &g_proc_lanes[(start + i) % (uint32_t)g_proc_lane_slots];
		if (__atomic_load_n(&r->state, __ATOMIC_ACQUIRE) == GR_PROC_LANE_ACTIVE &&
		    __atomic_load_n(&r->host_tid, __ATOMIC_RELAXED) == tid) {
			return r;
		}
	}
	return 0;
}
// Reserve the thread's incarnation slot for an attach we are about to perform. FALSE means another image
// already owns (or is creating) the incarnation: the caller must NOT attach a second lane.
static struct gr_proc_lane_rec* gr_proc_begin(int tid) {
	if (!gr_proc_registry()) {
		return 0;
	}
	uint32_t start = gr_proc_hash(tid);
	for (int round = 0; round < 2; ++round) {
		for (int i = 0; i < g_proc_lane_slots; ++i) {
			struct gr_proc_lane_rec* r = &g_proc_lanes[(start + i) % (uint32_t)g_proc_lane_slots];
			uint32_t expected = 0;
			if (__atomic_compare_exchange_n(&r->state, &expected, 1u /* ATTACHING */, false,
			                                __ATOMIC_ACQ_REL, __ATOMIC_RELAXED)) {
				r->host_tid = tid;
				return r;
			}
			if (__atomic_load_n(&r->state, __ATOMIC_ACQUIRE) == GR_PROC_LANE_ACTIVE &&
			    __atomic_load_n(&r->host_tid, __ATOMIC_RELAXED) == tid) {
				return 0; // already incarnated elsewhere
			}
		}
	}
	return 0;
}
static void gr_proc_publish(struct gr_proc_lane_rec* r, uint64_t generation, void* map, uint64_t size, uint32_t slot) {
	if (!r) {
		return;
	}
	r->generation = (uint32_t)generation;
	r->slot_index = slot;
	r->owner_image = GR_IMAGE_ID;
	r->mapping = map;
	r->mapping_size = size;
	__atomic_store_n(&r->next_seq, 1u, __ATOMIC_RELAXED); // seq lives with the incarnation
	r->creator_image = GR_IMAGE_ID;
	__atomic_store_n(&r->state, GR_PROC_LANE_ACTIVE, __ATOMIC_RELEASE); // publish LAST
	g_stat_proc_published++;
}
static void gr_proc_abandon(struct gr_proc_lane_rec* r) {
	if (!r) {
		return;
	}
	r->host_tid = 0;
	r->mapping = 0;
	r->mapping_size = 0;
	__atomic_store_n(&r->state, 0u, __ATOMIC_RELEASE);
}
static void gr_proc_unpublish(int tid) {
	struct gr_proc_lane_rec* r = gr_proc_find_active(tid);
	if (r) {
		gr_proc_abandon(r);
	}
}

// perf#30: the request sequence is part of the INCARNATION, not of the image. Two images serving the same
// thread must never publish the same seq for one lane: the server correlates replies by it, so an
// image-local counter restarting at 1 would be a latent collision. This is a fetch-add on the shared
// record; the producer is still exactly one host thread, so there is no contention to speak of, only
// image visibility.
static uint32_t gr_next_seq(gr_lane_t* L) {
	if (L->proc_rec) {
		return __atomic_fetch_add(&L->proc_rec->next_seq, 1u, __ATOMIC_RELAXED);
	}
	return L->seq++;
}

static gr_lane_t* gr_find_lane(int tid) {
	for (int i = 0; i < GR_BORROWED_SLOTS; ++i) {
		if (gr_borrowed_lane_usable(&g_borrowed[i], tid)) {
			return &g_borrowed[i];
		}
	}
	// HOT PATH: every ring op resolves its lane here, so this is a pure read walk. It only follows
	// `next` pointers that were RELEASE-published after that page's lanes[] were initialized, so any page
	// it reaches is fully readable, and rows that are not claimed simply read active!=1 and are skipped.
	// Within a page the probe starts at a TID-derived index so distinct TIDs spread out; a lane that
	// lives on a later page is still found, because the walk covers every page.
	for (gr_lane_page_t* page = __atomic_load_n(&g_lane_pages, __ATOMIC_ACQUIRE);
	     page; page = __atomic_load_n(&page->next, __ATOMIC_ACQUIRE)) {
		uint32_t start = ((uint32_t)tid * 2654435761u) % GR_LANE_PAGE_SIZE; // Knuth multiplicative hash
		for (uint32_t i = 0; i < GR_LANE_PAGE_SIZE; ++i) {
			gr_lane_t* L = &page->lanes[(start + i) % GR_LANE_PAGE_SIZE];
			if (__atomic_load_n(&L->active, __ATOMIC_ACQUIRE) == 1 && L->owner_tid == tid) {
				return L;
			}
		}
	}
	return 0;
}

// Append one initialized page and publish it, or NULL at the ceiling / on mmap failure.
//
// The page INDEX is reserved with a single CAS, and the mapping happens with NO LOCK HELD. That is not
// fastidiousness: the guest's mmap is an EMULATED syscall, so it issues an RPC to the server and can
// require an S2C upcall back into this very thread. Growing under a spinlock let every other attaching
// thread spin for the whole round trip -- measured: with the lock held across the mmap, a run that needed
// page 1 never reached its readiness marker at all. The lock below covers ONLY the pointer splice, which
// is pure memory stores and therefore cannot block.
static gr_lane_page_t* gr_catalog_grow(void) {
	uint32_t idx;
	for (;;) {
		idx = __atomic_load_n(&g_lane_page_count, __ATOMIC_RELAXED);
		if (idx >= GR_LANE_MAX_PAGES) {
			return 0; // ceiling: caller UDS-falls-back (counted, never a shared lane)
		}
		if (__atomic_compare_exchange_n(&g_lane_page_count, &idx, idx + 1, false,
		                                __ATOMIC_ACQ_REL, __ATOMIC_RELAXED)) {
			break;
		}
	}
	long m = LINUX_SYSCALL(__NR_mmap, 0, (long)sizeof(gr_lane_page_t),
	                       0x1 | 0x2 /* PROT_READ|WRITE */,
	                       0x2 | 0x20 /* MAP_PRIVATE|MAP_ANONYMOUS */, -1, 0);
	if ((unsigned long)m > (unsigned long)-4096) {
		__atomic_fetch_sub(&g_lane_page_count, 1u, __ATOMIC_ACQ_REL); // give the index back
		return 0;
	}
	gr_lane_page_t* page = (gr_lane_page_t*)m;
	// anonymous mmap is zero-filled already; set the two fields that must never read as a valid default.
	for (uint32_t i = 0; i < GR_LANE_PAGE_SIZE; ++i) {
		page->lanes[i].slot_index = idx * GR_LANE_PAGE_SIZE + i;
		page->lanes[i].wake_fd = -1;
	}
	// Splice: memory stores only, so the critical section cannot block.
	while (__atomic_exchange_n(&g_lane_grow_lock, 1u, __ATOMIC_ACQUIRE) != 0) {
		gr_relax();
	}
	__atomic_store_n(&page->next, (gr_lane_page_t*)0, __ATOMIC_RELEASE);
	if (g_lane_tail) {
		__atomic_store_n(&g_lane_tail->next, page, __ATOMIC_RELEASE); // publish LAST
	} else {
		__atomic_store_n(&g_lane_pages, page, __ATOMIC_RELEASE);
	}
	g_lane_tail = page;
	__atomic_store_n(&g_lane_grow_lock, 0u, __ATOMIC_RELEASE);
	return page;
}

// How many lane catalog pages this process currently holds (diagnostics only: the exhaustion message needs
// to say whether the catalog grew, because 'no free lane' with a 1024-slot catalog and eleven live threads
// is a different fact from a catalog that could not grow).
static unsigned gr_catalog_pages_used(void) {
	unsigned n = 0;
	for (gr_lane_page_t* page = __atomic_load_n(&g_lane_pages, __ATOMIC_ACQUIRE); page;
	     page = __atomic_load_n(&page->next, __ATOMIC_ACQUIRE)) {
		if (++n > 4096u) break;
	}
	return n;
}

static gr_lane_t* gr_claim_lane(int tid) {
	// Walk every page for a free slot; if the whole catalog is busy, append ONE page and walk again (the
	// second walk also picks up a page another thread appended concurrently). Each claim is a CAS on
	// active (0 -> transient 2 "claiming") so two threads can never take the same slot.
	for (int attempt = 0; attempt < 3; ++attempt) {
		for (gr_lane_page_t* page = __atomic_load_n(&g_lane_pages, __ATOMIC_ACQUIRE);
		     page; page = __atomic_load_n(&page->next, __ATOMIC_ACQUIRE)) {
			uint32_t start = ((uint32_t)tid * 2654435761u) % GR_LANE_PAGE_SIZE;
			for (uint32_t i = 0; i < GR_LANE_PAGE_SIZE; ++i) {
				gr_lane_t* L = &page->lanes[(start + i) % GR_LANE_PAGE_SIZE];
				uint32_t expected = 0;
				if (__atomic_compare_exchange_n(&L->active, &expected, 2u, false,
				                                __ATOMIC_ACQ_REL, __ATOMIC_RELAXED)) {
					L->wake_fd = -1; // no descriptor is usable until shared-loader adoption succeeds
					return L;
				}
			}
		}
		// Nothing free anywhere: append a page and walk again (the retry also picks up a page another
		// thread appended concurrently). This is the ONLY growth site, and it runs only when the whole
		// catalog is busy -- a released slot is always preferred to a new page.
		if (!gr_catalog_grow()) {
			break; // at the page ceiling, or mmap refused -> caller UDS-falls-back
		}
	}
	return 0; // catalog exhausted -> caller UDS-falls-back (counted; never a shared lane)
}

#ifdef DARLING_RING_PHASE_PROF
// perf #18 P6 (dar-aw2): guest-side rdtsc phase accountant. Pure diagnostic (default OFF, whole
// block compiles out). Accumulates TSC cycles for the guest-visible phases of the hot round-trip:
//   submit    = publish request + conditional doorbell (gr_wake_server)
//   roundtrip = time from after-submit until the reply is observed (server-side + wakeup latency)
//   observe   = validate + copy the port out of the reply slot
// and dumps mean cycles/phase to stderr once GR_PHASE_DUMP_AT samples accrue. Reuses the perf#9
// in-guest accountant pattern (rdtsc, NOT clock_gettime which wedges the guest this early).
#include <darling/emulation/common/simple.h>
#define GR_PHASE_DUMP_AT 5000
static unsigned long gr_ph_submit = 0, gr_ph_round = 0, gr_ph_observe = 0, gr_ph_total = 0;
static unsigned long gr_ph_n = 0;
static inline unsigned long gr_rdtsc(void) {
#if defined(__x86_64__) || defined(__i386__)
	unsigned hi, lo;
	__asm__ __volatile__("rdtsc" : "=a"(lo), "=d"(hi));
	return ((unsigned long)hi << 32) | lo;
#else
	return 0;
#endif
}
static void gr_phase_maybe_dump(void) {
	if (gr_ph_n < GR_PHASE_DUMP_AT) return;
	__simple_fprintf(2, "[gr-phase] n=%lu mean cycles: submit=%lu roundtrip=%lu observe=%lu total=%lu\n",
		gr_ph_n, gr_ph_submit / gr_ph_n, gr_ph_round / gr_ph_n, gr_ph_observe / gr_ph_n, gr_ph_total / gr_ph_n);
	gr_ph_submit = gr_ph_round = gr_ph_observe = gr_ph_total = 0; gr_ph_n = 0;
}
#endif

static inline void gr_relax(void) {
#if defined(__x86_64__) || defined(__i386__)
	__builtin_ia32_pause();
#elif defined(__aarch64__)
	__asm__ __volatile__("yield");
#else
	LINUX_SYSCALL0(__NR_sched_yield);
#endif
}

static gr_lane_t* gr_adopt_process_lane(int tid); // defined below; the postfork reset re-establishes a
                                                   // BORROWED view this image had already adopted
// perf#30 FD-COURIER (PRODUCT, dar-gwn.7.7.5): the descriptor half of a lifecycle operation rides ONE
// AF_UNIX SOCK_SEQPACKET connection per Linux process, established on first use and reused for every
// transfer, so connections track processes rather than operations. The channel carries ONLY a
// descriptor plus { generation, token, kind, fd_count } -- no callnum and no RPC body -- because the
// operation's semantics travel on the Ring; this channel exists because a file descriptor cannot.
//
// The address comes from the loader through the elfcalls table: process-scoped transport state is the
// loader's (the same ownership the lane directory expresses), and the loader is the only image that
// knows how the prefix maps to the abstract name the server bound.
//
// SOCK_SEQPACKET, not SOCK_STREAM: message boundaries are what let one persistent connection deliver
// many descriptors with their ancillaries intact. A stream read can split the descriptor from its
// envelope, and SCM_RIGHTS attaches to data rather than to a byte count.
//
// LIBC-FREE: raw LINUX_SYSCALL only, like the rest of this unit.
#define GR_FD_COURIER_SOCKET_UNTRIED (-2)
#define GR_FD_COURIER_SOCKET_INVALID (-1)
#define GR_FD_COURIER_UNIX_AF        1
#define GR_FD_COURIER_SEQPACKET      5
#define GR_FD_COURIER_SOCK_CLOEXEC   02000000
#define GR_FD_COURIER_SOL_SOCKET     1
#define GR_FD_COURIER_SCM_RIGHTS     1
#define GR_FD_COURIER_CLOCK_MONOTONIC 1

static int gr_fd_courier_socket = GR_FD_COURIER_SOCKET_UNTRIED;
// perf#30 FD-COURIER diagnostics: counted, never printed from the send path (in-band printing there is a
// measured boot hazard). They surface in the lane-stats dump at sys_exit.
static uint32_t gr_plane_attach_seq = 0;
// perf#30: the two causes the PAGE cannot report. `no_page` are attaches taken before the page exists (the
// environment-gated diagnostic is silent there, and the page cannot count what it never saw) and
// `page_ready_fail` are ones where the page existed but its transport was never ready. Kept in the guest and
// printed in the lane-stats dump at sys_exit, which is a channel already known to be safe -- an in-band print
// on the early bootstrap path is a measured boot hazard.
static uint64_t gr_attach_no_page = 0;
static uint64_t gr_attach_ready_fail = 0;

// Diagnostics for the ATTACH_LANE route are opt-in: an in-band print on the early bootstrap path has been a
// boot hazard repeatedly, so the lines exist but stay silent unless asked for.
static int gr_plane_attach_diag(void) {
	/* ALWAYS ON (dar-4cp9). Every failure reason of the ATTACH_LANE plane route used to sit behind this
	 * hatch, so in a product run the attach could fail for any of five named reasons and the log showed none
	 * of them -- the same 'instrument that cannot answer' class as the exhaustion message that was compiled
	 * out behind a profile guard. The prints are one line per failed attach attempt and the attempts are
	 * few, so the cost is bounded by construction. */
	return 1;
}

// perf#30: the page route for ATTACH_LANE is OPT-IN until the process-identity question is settled.
// MEASURED: the server observes different identifiers for one Linux process -- the page is keyed by the pid
// that mapped the region, while a courier connection opened by a THREAD reports that thread's tid
// (`[P:682023(682023)]`), and the one-time doorbell rule is keyed by pid too, so the first attaches were
// delivered no descriptor (`token=0`) while a later tid consumed the single delivery. With the route on by
// default the boot wedges right after the kqchan reply; with it off the proven datagram path runs and the
// regression is GREEN. The transaction model and the token-keyed pairing stay in the tree, exercised by this
// gate, until the identity is normalised to the thread-group leader.
static int gr_plane_attach_enabled(void) {
	// ON by default. The wedge is fixed and understood: a forked child inherited the parent's page pointer
	// (a static in the image) and published its requests into the PARENT's page, so the server answered
	// there and the child's caller waited for a reply addressed to another process. The page now names its
	// owner and a page that belongs to another process is abandoned for a fresh one (see the accessor).
	// MEASURED with the route live: full regression GREEN, `attach_route_refused=0`, `wait_fd=0`, every
	// transaction completes. The hatch turns it off for a controlled datagram comparison.
	return (getenv("DARLING_GUEST_PLANE_ATTACH_OFF") == NULL) ? 1 : 0;
}
static uint64_t gr_fd_courier_send_attempts = 0;
static uint64_t gr_fd_courier_no_address = 0;
static uint64_t gr_fd_courier_connect_failures = 0;
static uint64_t gr_fd_courier_sends_ok = 0;
static uint64_t gr_fd_courier_send_failures = 0;
static long gr_fd_courier_last_send_errno = 0;
static uint64_t gr_fd_courier_generation = 0;
static uint64_t gr_fd_courier_next_token = 0;

// A forked child must NOT keep writing onto its parent's connection: the server attributes a descriptor
// to the connection's SO_PEERCRED pid, so the child's descriptors would be paired with the parent's
// requests. The child closes its copy and reconnects on first use with its own generation.
void __dserver_fd_courier_postfork_reset(void) {
	// The connection is the LOADER's, so the loader forgets it: closing the number this image happens to
	// hold would leave the loader's cache pointing at a number the child will reuse for something else.
	__dserver_fd_courier_reset();
	// The control page belongs to the process incarnation too: the child must create its own, or its
	// requests would be serviced under the parent's pid.
	gr_fd_courier_socket = GR_FD_COURIER_SOCKET_UNTRIED;
	gr_fd_courier_generation = 0;
}

// The loader publishes a Linux sockaddr_un: 2-byte family then the abstract name. This unit is
// libc-free, so the shape is declared here exactly as the RPC defs header declares it.
struct gr_courier_address {
	unsigned short int sun_family;
	char sun_path[108];
};

// An abstract socket's name is matched over the ENTIRE address length, so passing a full-size
// sockaddr_un appends the struct's zero padding to the name and the connect is refused (measured:
// ECONNREFUSED with a live listener bound to the same visible name). The length must be exactly
// offsetof(sun_path) + 1 (the leading NUL) + the name's own length.
static unsigned int gr_fd_courier_address_len(const struct gr_courier_address* address) {
	unsigned int len = 0;
	while ((len + 1u) < sizeof(address->sun_path) && address->sun_path[len + 1u] != '\0') {
		++len;
	}
	return 2u + 1u + len;
}

// perf#30 FD-COURIER MUTATIONS. Each mode produces one of the failure shapes the server must reject,
// so the reject paths are exercised by a real descriptor rather than asserted in prose. Env-gated and
// single-mode; an unset hatch is the product path, byte for byte.
static const char* gr_fd_courier_mutation(void) {
	static const char* mode = NULL;
	static int resolved = 0;
	if (!resolved) {
		const char* value = getenv("DARLING_GUEST_COURIER_MUTATE");
		mode = value;
		resolved = 1;
	}
	return mode;
}

// One sendmsg of one descriptor with an explicit envelope. Returns true when the message went out.
static int gr_fd_courier_send_envelope(int sock, uint64_t token, uint32_t kind, uint64_t generation, int fd) {
	struct dserver_fd_courier_message envelope;
	envelope.process_generation = generation;
	envelope.token = token;
	envelope.kind = kind;
	envelope.fd_count = 1;

	char control[24];
	unsigned long zero;
	for (zero = 0; zero < sizeof(control); ++zero) {
		control[zero] = 0;
	}
	struct gr_courier_iovec {
		void* base;
		unsigned long len;
	} iov;
	iov.base = &envelope;
	iov.len = sizeof(envelope);
	struct gr_courier_msghdr {
		void* name;
		unsigned int name_len;
		struct gr_courier_iovec* iov;
		unsigned long iov_len;
		void* control;
		unsigned long control_len;
		unsigned int flags;
	} msg;
	msg.name = NULL;
	msg.name_len = 0;
	msg.iov = &iov;
	msg.iov_len = 1;
	msg.control = control;
	msg.control_len = sizeof(control);
	msg.flags = 0;

	struct gr_courier_cmsghdr {
		unsigned long len;
		int level;
		int type;
	}* cmsg = (struct gr_courier_cmsghdr*)control;
	cmsg->len = (unsigned long)(16u + sizeof(int));
	cmsg->level = GR_FD_COURIER_SOL_SOCKET;
	cmsg->type = GR_FD_COURIER_SCM_RIGHTS;
	int* cmsg_fd = (int*)((char*)control + sizeof(*cmsg));
	*cmsg_fd = fd;

	long sent = LINUX_SYSCALL(__NR_sendmsg, sock, &msg, 0);
	gr_fd_courier_last_send_errno = (sent == (long)sizeof(envelope)) ? 0 : sent;
	return sent == (long)sizeof(envelope);
}

uint64_t __dserver_fd_courier_send(int fd, uint32_t kind) {
	if (fd < 0) {
		return 0;
	}
	++gr_fd_courier_send_attempts;
	// The CONNECTION belongs to the loader, like its address: ONE per process, shared by every image.
	// MEASURED: an image must NOT keep the NUMBER across calls. An image that cached it later found a
	// DIFFERENT file at that number (sendmsg returned ENOTSOCK, the lane attach parked, boot stopped),
	// because the process reused the number for another file. Ask the owner at the moment of use -- a
	// number is not ownership.
	gr_fd_courier_socket = __dserver_fd_courier_socket();
	if (gr_fd_courier_socket < 0) {
		gr_fd_courier_socket = GR_FD_COURIER_SOCKET_INVALID;
	}
	if (gr_fd_courier_socket < 0) {
		return 0;
	}
	if (gr_fd_courier_generation == 0) {
		// This PROCESS's incarnation identity, asked of the loader so every image reports the same value:
		// the server keys descriptors by (pid, generation) and rejects a second, different generation for
		// a pid as a stale incarnation, which is exactly what a per-image value would look like.
		gr_fd_courier_generation = __dserver_process_generation();
		if (gr_fd_courier_generation == 0) {
			gr_fd_courier_generation = 1;
		}
	}

	// perf#30 IDENTITY: the pid is mixed in. MEASURED defect: the token was generation*C ^ counter, and two
	// different processes with the same generation and counter=1 produced the SAME token -- the server's
	// pending registry then paired one process's request with another process's descriptor
	// (`PARKED pid=672791 token=X` against `bundle pid=672797 token=X`, same generation). The token is the
	// identity both halves share, so it has to be unique per process as well as per send.
	uint64_t token = (gr_fd_courier_generation * 0x9E3779B97F4A7C15ull)
		^ ((uint64_t)(unsigned)LINUX_SYSCALL(__NR_getpid) * 0xC2B2AE3D27D4EB4Full)
		^ (++gr_fd_courier_next_token);
	const char* mutation = gr_fd_courier_mutation();

	if (mutation == NULL) {
		if (!gr_fd_courier_send_envelope(gr_fd_courier_socket, token, kind, gr_fd_courier_generation, fd)) {
			++gr_fd_courier_send_failures;
			__simple_fprintf(2, "[fd-courier] send failed pid=%d sock=%d send_errno=%ld probe=%ld\n",
				(int)LINUX_SYSCALL(__NR_getpid), gr_fd_courier_socket,
				gr_fd_courier_last_send_errno,
				(long)LINUX_SYSCALL(__NR_fcntl, gr_fd_courier_socket, 3, 0));
			LINUX_SYSCALL(__NR_close, gr_fd_courier_socket);
			gr_fd_courier_socket = GR_FD_COURIER_SOCKET_INVALID;
			return 0;
		}
		++gr_fd_courier_sends_ok;
		return token;
	}

	// MUTATION MODES (diagnostics only; the guest still returns a token so the semantic half is issued
	// exactly as in the product path, and each mode produces ONE specific reject or orphan server-side).
	if (mutation[0] == 'd') {
		// duplicate: the same token twice; the first bundle stays pending and the second is closed.
		(void)gr_fd_courier_send_envelope(gr_fd_courier_socket, token, kind, gr_fd_courier_generation, fd);
		(void)gr_fd_courier_send_envelope(gr_fd_courier_socket, token, kind, gr_fd_courier_generation, fd);
		return token;
	} else if (mutation[0] == 'k') {
		// wrong kind: a descriptor for an operation this call is not.
		(void)gr_fd_courier_send_envelope(gr_fd_courier_socket, token, 0xFFFFu, gr_fd_courier_generation, fd);
		return token;
	} else if (mutation[0] == 's') {
		// stale generation: the real bundle, then a decoy from another incarnation, so the recorded
		// generation no longer matches the real one by the time the semantic half resolves it.
		(void)gr_fd_courier_send_envelope(gr_fd_courier_socket, token, kind, gr_fd_courier_generation, fd);
		(void)gr_fd_courier_send_envelope(gr_fd_courier_socket, token + 0x1000000ull, kind, gr_fd_courier_generation + 1u, fd);
		return token;
	} else if (mutation[0] == 'o') {
		// orphan descriptor: a bundle nothing will ever resolve; the process exit closes it.
		(void)gr_fd_courier_send_envelope(gr_fd_courier_socket, token, kind, gr_fd_courier_generation, fd);
		(void)gr_fd_courier_send_envelope(gr_fd_courier_socket, token + 0x2000000ull, kind, gr_fd_courier_generation, fd);
		return token;
	} else if (mutation[0] == 'n') {
		// semantic orphan: no bundle at all, so the server must not fake success; the process exit
		// resolves the waiter.
		return token;
	}
	return token;
}


// perf#30 FD-COURIER (round 49, SERVER->GUEST): the reverse direction. The SAME process-scoped
// connection carries a descriptor the server hands to this process -- the console channel and the
// process kqchan -- and the semantic reply names it by token. The connection is not duplicated and no
// second socket is created: one connection per Linux process, both directions, descriptor-only traffic.
//
// Ordering: the server sends the descriptor BEFORE it publishes the reply, so by the time a caller has a
// token the descriptor is already queued. The registry below exists for the same reason the server keeps
// pending bundles: the two halves may still arrive in either order, and a token must never be resolved to
// a descriptor that belongs to another token.
#define GR_FD_COURIER_PENDING_MAX 64
struct gr_fd_courier_pending_entry {
	uint64_t token;
	int fd;
};
static struct gr_fd_courier_pending_entry gr_fd_courier_pending[GR_FD_COURIER_PENDING_MAX];
static uint64_t gr_fd_courier_received = 0;
static uint64_t gr_fd_courier_receive_misses = 0;
static uint64_t gr_fd_courier_receive_dropped = 0;
static uint64_t gr_fd_courier_receive_kind_rejects = 0;
static uint64_t gr_fd_courier_receive_stale_rejects = 0;
static int gr_fd_courier_timeout_set = 0;

// Store one received descriptor under its token. A duplicate token keeps the FIRST descriptor and closes
// the second (the operation executes once), a full registry closes the descriptor rather than leaking it,
// and every close is counted -- a dropped descriptor must be visible, never silent.
static int gr_fd_courier_store(uint64_t token, int fd) {
	if (token == 0 || fd < 0) {
		if (fd >= 0) {
			LINUX_SYSCALL(__NR_close, fd);
			++gr_fd_courier_receive_dropped;
		}
		return 0;
	}
	int free_slot = -1;
	for (int i = 0; i < GR_FD_COURIER_PENDING_MAX; ++i) {
		if (gr_fd_courier_pending[i].token != 0 && gr_fd_courier_pending[i].token == token) {
			LINUX_SYSCALL(__NR_close, fd);
			++gr_fd_courier_receive_dropped;
			return 0;
		}
		// "free" is token == 0, NOT fd < 0: MEASURED, a static array is zero-initialized, so fd < 0 is
		// false for every unused entry (fd == 0 looks like a valid descriptor), no slot was ever found,
		// the descriptor was closed on arrival and the lookup then blocked forever.
		if (free_slot < 0 && gr_fd_courier_pending[i].token == 0) {
			free_slot = i;
		}
	}
	if (free_slot < 0) {
		LINUX_SYSCALL(__NR_close, fd);
		++gr_fd_courier_receive_dropped;
		return 0;
	}
	gr_fd_courier_pending[free_slot].token = token;
	gr_fd_courier_pending[free_slot].fd = fd;
	++gr_fd_courier_received;
	return 1;
}

// One recvmsg of one envelope, plus every descriptor that came with it. Returns 1 when a message was
// consumed, 0 when nothing was available (non-blocking), -1 when the connection is gone.
static int gr_fd_courier_recv_once(int sock, int wait) {
	struct dserver_fd_courier_message envelope;
	char control[64];
	unsigned long i;
	for (i = 0; i < sizeof(control); ++i) {
		control[i] = 0;
	}
	struct gr_recv_iovec {
		void* base;
		unsigned long len;
	} iov;
	iov.base = &envelope;
	iov.len = sizeof(envelope);
	struct gr_recv_msghdr {
		void* name;
		unsigned int name_len;
		struct gr_recv_iovec* iov;
		unsigned long iov_len;
		void* control;
		unsigned long control_len;
		unsigned int flags;
	} msg;
	msg.name = NULL;
	msg.name_len = 0;
	msg.iov = &iov;
	msg.iov_len = 1;
	msg.control = control;
	msg.control_len = sizeof(control);
	msg.flags = 0;

	// MSG_DONTWAIT is 0x40, NOT 2: MEASURED, 2 is MSG_PEEK, so the "non-blocking" drain peeked the same
	// message forever and the shellspawn spun in userspace instead of booting.
	long got = LINUX_SYSCALL(__NR_recvmsg, sock, &msg, wait ? 0 : 0x40 /* MSG_DONTWAIT */);
	if (got <= 0) {
		return (got == 0) ? -1 : 0;
	}
	if (got != (long)sizeof(envelope)) {
		// SOCK_SEQPACKET preserves boundaries, so a short envelope is malformed: close whatever came
		// with it and keep the connection, exactly as the server does for the mirror case.
		struct gr_recv_cmsghdr {
			unsigned long len;
			int level;
			int type;
		}* cmsg = (struct gr_recv_cmsghdr*)control;
		if (cmsg->len >= 16u + sizeof(int) && cmsg->level == GR_FD_COURIER_SOL_SOCKET && cmsg->type == GR_FD_COURIER_SCM_RIGHTS) {
			int* came = (int*)((char*)control + sizeof(*cmsg));
			LINUX_SYSCALL(__NR_close, *came);
			++gr_fd_courier_receive_dropped;
		}
		return 1;
	}
	struct gr_recv_cmsghdr {
		unsigned long len;
		int level;
		int type;
	}* cmsg = (struct gr_recv_cmsghdr*)control;
	if (cmsg->len < 16u + sizeof(int) || cmsg->level != GR_FD_COURIER_SOL_SOCKET || cmsg->type != GR_FD_COURIER_SCM_RIGHTS) {
		// A bundle with no descriptor is malformed for this direction: the token would resolve to
		// nothing, so the whole message is refused rather than stored.
		++gr_fd_courier_receive_dropped;
		return 1;
	}
	int* came = (int*)((char*)control + sizeof(*cmsg));
	// A descriptor the guest cannot place is closed here, never stored: an unknown kind (a bundle for an
	// operation this build does not have) and a bundle from another incarnation are both REFUSED, so a
	// token can never resolve to a descriptor that belongs to a different operation or a dead epoch.
	if (envelope.kind != DSERVER_FD_COURIER_KIND_CONSOLE_FD &&
	    envelope.kind != DSERVER_FD_COURIER_KIND_KQCHAN_FD &&
	    envelope.kind != DSERVER_FD_COURIER_KIND_PROCESS_DOORBELL) {
		LINUX_SYSCALL(__NR_close, *came);
		++gr_fd_courier_receive_kind_rejects;
		return 1;
	}
	if (gr_fd_courier_generation != 0 && envelope.process_generation != gr_fd_courier_generation) {
		LINUX_SYSCALL(__NR_close, *came);
		++gr_fd_courier_receive_stale_rejects;
		return 1;
	}
	(void)gr_fd_courier_store(envelope.token, *came);
	return 1;
}

static int gr_fd_courier_lookup(uint64_t token) {
	for (int i = 0; i < GR_FD_COURIER_PENDING_MAX; ++i) {
		if (gr_fd_courier_pending[i].token != 0 && gr_fd_courier_pending[i].token == token) {
			int fd = gr_fd_courier_pending[i].fd;
			gr_fd_courier_pending[i].fd = -1;
			gr_fd_courier_pending[i].token = 0;
			return fd;
		}
	}
	return -1;
}

// Resolve a token the server named in a semantic reply. The descriptor is normally already queued (the
// server sends it first); the blocking read is the fallback for the window where it is still in flight,
// and it is bounded by the connection, not by a timer -- a token that never arrives is a committed
// failure of the operation, so reporting it is better than pretending the descriptor exists.
int __dserver_fd_courier_receive(uint64_t token) {
	if (token == 0) {
		return -1;
	}
	int sock = __dserver_fd_courier_socket();
	if (sock < 0) {
		++gr_fd_courier_receive_misses;
		return -1;
	}
	// A bounded wait, not an unbounded one: a semantic reply that names a token whose descriptor never
	// arrives is a committed failure of that operation, and it must be REPORTED rather than turned into a
	// hang. MEASURED before this: with the registry bug the fallback read waited forever and the whole
	// boot stopped with no diagnostic at all.
	if (!gr_fd_courier_timeout_set) {
		struct gr_timeval { long sec; long usec; } tv;
		tv.sec = 0;
		tv.usec = 200000;
		LINUX_SYSCALL(__NR_setsockopt, sock, GR_FD_COURIER_SOL_SOCKET, 20 /* SO_RCVTIMEO */, &tv, sizeof(tv));
		gr_fd_courier_timeout_set = 1;
	}
	while (gr_fd_courier_recv_once(sock, 0) == 1) {
		// drain everything already queued
	}
	int fd = gr_fd_courier_lookup(token);
	if (fd >= 0) {
		return fd;
	}
	if (gr_fd_courier_recv_once(sock, 1) == 1) {
		fd = gr_fd_courier_lookup(token);
		if (fd >= 0) {
			return fd;
		}
	}
	if (gr_fd_courier_receive_misses <= 4) {
		__simple_fprintf(2, "[fd-courier-recv] MISS pid=%d token=%llu stored=%llu dropped=%llu mySocket=%d\n",
			(int)LINUX_SYSCALL(__NR_getpid), (unsigned long long)token,
			(unsigned long long)gr_fd_courier_received, (unsigned long long)gr_fd_courier_receive_dropped,
			sock);
		__simple_fprintf(2, "[fd-courier-recv] MISS-detail pid=%d token=%llu stored=%llu dropped=%llu\n",
			(int)LINUX_SYSCALL(__NR_getpid), (unsigned long long)token,
			(unsigned long long)gr_fd_courier_received, (unsigned long long)gr_fd_courier_receive_dropped);
	}
	++gr_fd_courier_receive_misses;
	return -1;
}

void __dserver_ring_postfork_reset(void) {
	__dserver_fd_courier_postfork_reset();
	// The loader has closed all inherited ring FDs, including dyld's, before
	// refreshing child RPC sockets. Only release mappings here: saved FD numbers
	// may already belong to unrelated child descriptors.
	//
	// perf#27: the catalog is a chain of lazily mmap'd pages, so the child starts from an EMPTY catalog
	// (`g_lane_pages = NULL`) instead of zeroing a fixed array -- the pages are MAP_PRIVATE|ANONYMOUS, so
	// they are the child's own copies and are simply unmapped. The first attach in the child re-creates
	// page 0, which also re-establishes the generation-0 epoch space the comment below is about.
	// perf#30 STAGE 2: a BORROWED view owns nothing, so the child's catalog reset must not destroy it.
	// Measured: dropping the pages removed an adopted view with no release recorded, and the image then
	// had no usable lane at all. Save borrowed views here and reinstall them below.
	struct gr_borrowed_save {
		int tid;
		void* map;
		uint64_t size;
		uint32_t generation;
		int wake_fd;
		uint32_t slot_index;
	};
	static struct gr_borrowed_save saved[16];
	int saved_n = 0;
	{
		for (gr_lane_page_t* p = g_lane_pages; p; p = __atomic_load_n(&p->next, __ATOMIC_ACQUIRE)) {
			for (uint32_t k = 0; k < GR_LANE_PAGE_SIZE && saved_n < 16; ++k) {
				gr_lane_t* L = &p->lanes[k];
				if (__atomic_load_n(&L->active, __ATOMIC_ACQUIRE) == 1 && L->borrowed && L->owner_tid) {
					saved[saved_n].tid = (int)L->owner_tid;
					saved[saved_n].map = L->map;
					saved[saved_n].size = L->size;
					saved[saved_n].generation = L->generation;
					saved[saved_n].wake_fd = L->wake_fd;
					saved[saved_n].slot_index = L->slot_index;
					++saved_n;
				}
			}
		}
	}
	gr_lane_page_t* page = g_lane_pages;
	g_lane_pages = 0;
	g_lane_tail = 0;
	g_lane_page_count = 0;
	while (page) {
		gr_lane_page_t* next = page->next;
		LINUX_SYSCALL(__NR_munmap, page, (uint64_t)sizeof(gr_lane_page_t));
		page = next;
	}
	// ONE page cannot be released that way -- the language-neutral guest table is gone, but a lane's
	// user-visible descriptors are the loader's problem, and the loader has already closed them.
	g_lanes_exhausted_logged = 0;
	g_stat_lanes_exhausted = 0;
	g_stat_lanes_acquired = 0;
	g_stat_lanes_reclaimed = 0;
	g_stat_lanes_released = 0;	// perf#30 STAGE 2: the catalog this reset drops may hold a BORROWED view of a process-global
	// incarnation created by another image (the loader's main-thread lane). Dropping it silently
	// made the next lookup take the attach path for the SAME tid -- which retires the shared lane
	// server-side and wedges boot. Adoption is safe to redo here: it creates only a local view and
	// touches nothing the child owns. If the loader's directory is not reachable yet, this is a
	// no-op and behaviour is unchanged.
	(void)gr_adopt_process_lane((int)LINUX_SYSCALL(__NR_gettid));
	// Reinstall the preserved borrowed views on a fresh page. They reference the SAME shared mapping.
	for (int k = 0; k < saved_n; ++k) {
		gr_lane_t* L = gr_claim_lane(saved[k].tid);
		if (!L) {
			break;
		}
		L->map = saved[k].map;
		L->size = saved[k].size;
		L->generation = saved[k].generation;
		L->wake_fd = saved[k].wake_fd;
		L->owner_tid = saved[k].tid;
		L->slot_index = saved[k].slot_index;
		L->seq = __atomic_load_n(&gr_c2s(L)->tail, __ATOMIC_RELAXED) + 1;
		L->proc_rec = gr_proc_find_active(saved[k].tid);
		L->borrowed = 1;
		L->state = 1;
		__atomic_store_n(&L->active, 1u, __ATOMIC_RELEASE);
	}

}

// perf#27 (MILESTONE B, sections 15-17): RELEASE this thread's lane back to the catalog.
//
// Measured before this existed: `lanes_acquired=128 exhausted=2694 reclaimed=0` -- the guest table was
// never released, so after the first 128 HISTORICAL threads every subsequent thread fell back to UDS even
// though at most a handful were live at once. A historical thread count must not decide whether non-fd RPC
// gets a Ring path, so the exit path now returns the slot.
//
// Ordering (each step is deliberate):
//   1. take the lane OUT OF SERVICE first (active 1 -> 3 "releasing"), so a concurrent finder no longer
//      sees it as usable and a concurrent claimant cannot take it as free while it is being torn down;
//   2. unpublish the guest half (map/size/fd/owner) and bump the GENERATION, so the next claimant of this
//      slot is a new epoch -- a stale (tid, seq) from the old incarnation can never match;
//   3. release the resources: unmap OUR mapping (the server holds its own mapping, so it may still be
//      draining; anything it publishes now lands in memory nobody reads) and hand the wake descriptor back
//      to the loader, which owns it;
//   4. publish FREE (3 -> 0) LAST, so a claimant that wins the slot sees a fully torn-down lane.
//
// No server round trip is needed for correctness: the server-side RingBuffer is refcounted (the owning
// Thread holds the only shared_ptr and drops it on thread death) and a Call that is still in flight holds
// its OWN shared_ptr, which is exactly what lets a slot be recycled while an old incarnation is still
// referenced. Retiring the CATALOG SLOT here and retiring the SERVER OBJECT on thread death are therefore
// independent, and neither can hand a new lane a stale completion.
// perf#27 diagnostic switch: `DARLING_GUEST_LANE_RELEASE=0` turns the whole release lifecycle off, so an
// A/B run can attribute any behavior change to it. Default ON (the released slot IS the fix).
// perf#27 #7 mutation hatch (see the publisher_tid assignment).
static int gr_tid_mutate_enabled(void) {
	static int cached = -1;
	if (cached < 0) {
		cached = gr_environ_has("DARLING_GUEST_TID_MUTATE=1", sizeof("DARLING_GUEST_TID_MUTATE=1") - 1) ? 1 : 0;
	}
	return cached;
}

typedef struct { int64_t tv_sec; int64_t tv_nsec; } gr_timespec_t; // x86_64/aarch64 Linux timespec

// perf#27 M3/M5/M6: deterministic mutation hatches for the duplex COMPLETION protocol. Each perturbs a
// real transport path (not a stub) so the RED arm exercises the same code the GREEN arm does.
static int gr_mut(const char* key, __SIZE_TYPE__ klen, int* cache) {
	if (*cache < 0) {
		*cache = gr_environ_has(key, klen) ? 1 : 0;
	}
	return *cache;
}
#define GR_MUT(name, cache) gr_mut(name, sizeof(name) - 1, cache)
static int gr_mut_uid = -1, gr_mut_drop = -1, gr_mut_dup = -1;

// Publish ONE duplex completion, subject to the mutation hatches. M3 rewrites the upcall id, M5 discards
// the completion after the effect happened, M6 replays it.
// perf#27 M6b: replay an OLD completion, long after its (parent, upcall) pair has aged out of any bounded
// history window. The point is to prove the server's rejection does NOT depend on remembering recent
// identities: with the invariant "the reply slot is valid only for the CURRENT in-flight upcall", a stale
// pair is rejected because it is not the current one -- not because it is in a cache.
static uint32_t gr_old_parent, gr_old_uid;
static uint64_t gr_dup_total;
static int      gr_old_state = -1; // -1 = not initialised, 0 = waiting to replay, 1 = replayed
static int      gr_old_done;

static void gr_duplex_replay_old(dserver_ring_shm_t* cb, int is_munmap) {
	if (gr_old_done) return;
	gr_old_done = 1;
	if (is_munmap) {
		dserver_ring_duplex_publish_munmap_reply(cb, gr_old_parent, gr_old_uid, 0, 0);
	} else {
		dserver_ring_duplex_publish_mmap_reply(cb, gr_old_parent, gr_old_uid, 0, 0, 0);
	}
}

static void gr_duplex_complete(dserver_ring_shm_t* cb, uint32_t parent, uint32_t uid, int is_munmap,
                              int32_t status, int32_t errno_result, uint64_t value) {
	// M6b: remember the FIRST pair we ever complete; replay it once, >16 transactions later.
	static int m6b = -1;
	if (m6b < 0) {
		m6b = gr_environ_has("DARLING_GUEST_DUP_REPLAY_OLD=1", sizeof("DARLING_GUEST_DUP_REPLAY_OLD=1") - 1) ? 1 : 0;
	}
	if (m6b && gr_old_state == -1) {
		gr_old_parent = parent; gr_old_uid = uid; gr_old_state = 0;
	}
	gr_dup_total++;
	if (GR_MUT("DARLING_GUEST_DUPLEX_DROP_REPLY=1", &gr_mut_drop)) {
		gr_duplex_dropped_replies++;
		GR_TRACE("DUPLEX_DROP_REPLY parent=%u upcall=%u\n", (unsigned)parent, (unsigned)uid);
		return;
	}
	// M6b: inject the AGED-OUT pair BEFORE the real completion and give the server a window to observe it.
	// Order matters: publishing the bogus reply after the real one would overwrite the real reply in the
	// single-slot mailbox and strand the current transaction (measured). Injecting first means the server
	// sees a completion that names neither the current upcall nor any recently closed one -- the case the
	// invariant must reject WITHOUT any history -- and then the real completion arrives normally.
	if (m6b && gr_old_state == 0 && gr_dup_total >= 16 && !gr_old_done) {
		gr_old_state = 1;
		gr_duplex_replay_old(cb, is_munmap);
		static int m6b_delay = -1;
		if (m6b_delay < 0) {
			m6b_delay = gr_environ_int("DARLING_GUEST_DUP_REPLAY_DELAY_MS=",
			                           sizeof("DARLING_GUEST_DUP_REPLAY_DELAY_MS=") - 1, 0);
		}
		if (m6b_delay > 0) {
			gr_timespec_t ts;
			ts.tv_sec = m6b_delay / 1000;
			ts.tv_nsec = (int64_t)(m6b_delay % 1000) * 1000000;
			LINUX_SYSCALL(__NR_nanosleep, &ts, 0);
		}
	}
	uint32_t use_uid = uid;
	if (GR_MUT("DARLING_GUEST_DUPLEX_UID_MUTATE=1", &gr_mut_uid)) {
		use_uid = uid + 1u; // M3: stale/wrong id -- the server must reject it and keep waiting
		gr_duplex_uid_mutated++;
	}
	if (is_munmap) {
		dserver_ring_duplex_publish_munmap_reply(cb, parent, use_uid, status, errno_result);
	} else {
		dserver_ring_duplex_publish_mmap_reply(cb, parent, use_uid, status, errno_result, value);
	}
	if (GR_MUT("DARLING_GUEST_DUPLEX_DUP_REPLY=1", &gr_mut_dup)) {
		gr_duplex_dup_replies++; // M6: the same completion, a second time (destroy at most once)
		// The replay window is timing-dependent: if the server consumes the first completion before the
		// replay lands, the replay is a STALE reply in the slot and must be explicitly rejected. Without a
		// delay the two publishes usually coalesce into one slot state and no replay is ever observable, so
		// DARLING_GUEST_DUP_REPLAY_DELAY_MS forces the interesting interleaving.
		{
			static int delay_cached = -1;
			if (delay_cached < 0) {
				delay_cached = gr_environ_int("DARLING_GUEST_DUP_REPLAY_DELAY_MS=",
				                              sizeof("DARLING_GUEST_DUP_REPLAY_DELAY_MS=") - 1, 0);
			}
			if (delay_cached > 0) {
				// libc-free: this file cannot rely on usleep being declared this low in libSystem. A raw
				// nanosleep(2) with a two-word timespec is the same wait with no libc dependency.
				gr_timespec_t ts;
				ts.tv_sec = delay_cached / 1000;
				ts.tv_nsec = (int64_t)(delay_cached % 1000) * 1000000;
				LINUX_SYSCALL(__NR_nanosleep, &ts, 0);
			}
		}
		if (is_munmap) {
			dserver_ring_duplex_publish_munmap_reply(cb, parent, use_uid, status, errno_result);
		} else {
			dserver_ring_duplex_publish_mmap_reply(cb, parent, use_uid, status, errno_result, value);
		}
	}
}

static int gr_release_enabled(void) {
	static int cached = -1;
	if (cached < 0) {
		cached = gr_environ_has("DARLING_GUEST_LANE_RELEASE=0", sizeof("DARLING_GUEST_LANE_RELEASE=0") - 1)
		           ? 0 : 1; // released-by-default: the freed slot IS the fix; the switch exists for A/B
	}
	return cached;
}

static void gr_release_lane(gr_lane_t* L) {
	if (!gr_release_enabled()) {
		return;
	}
	{
		static int rel_emitted = 0;
		if (rel_emitted < 10) {
			++rel_emitted;
			__simple_fprintf(2, "[dring-lane-release] pid=%d tid=%d slot=%u borrowed=%u ret=%p\n",
			                 (int)LINUX_SYSCALL(__NR_getpid), (int)LINUX_SYSCALL(__NR_gettid),
			                 (unsigned)L->slot_index, (unsigned)L->borrowed,
			                 __builtin_return_address(0));
		}
	}
	// 1. out of service (1 -> 3). ACQ_REL: a reader that observes the transition also observes the
	// teardown that follows as ordered after it.
	uint32_t expected = 1;
	if (!__atomic_compare_exchange_n(&L->active, &expected, 3u, false,
	                                 __ATOMIC_ACQ_REL, __ATOMIC_RELAXED)) {
		return; // not ours to release (already released / never fully attached)
	}
	void*    map  = L->map;
	uint64_t size = L->size;
	int      fd   = L->wake_fd;
	int      borrowed = (int)L->borrowed;
	int      owner_tid_of_view = (int)L->owner_tid;
	// 2. unpublish the guest half + give the slot a new epoch.
	L->map = 0;
	L->size = 0;
	L->wake_fd = -1;
	L->state = 0;
	L->owner_tid = 0;
	L->seq = 1;
	L->generation = L->generation + 1;
	g_stat_lanes_released++;
	// 3. resources. perf#30: ONLY the creating image may unmap the shared mapping -- a borrowed view
	// drops its local slot and leaves the incarnation (and its mapping) to its owner.
	if (!borrowed) {
		if (map && size) {
			LINUX_SYSCALL(__NR_munmap, map, size);
		}
		gr_proc_unpublish((int)owner_tid_of_view);
	}
	L->borrowed = 0;
	// perf#28 (ONE doorbell): `fd` is the process-wide doorbell, which the lane only BORROWS. A lane
	// release therefore closes nothing: the descriptor belongs to the transport and lives until the
	// process exits. Closing it from a dying lane would silently break every other lane -- and every
	// later attach -- of the same process.
	(void)fd;
	// 4. free, published LAST.
	__atomic_store_n(&L->active, 0u, __ATOMIC_RELEASE);
}

void __dserver_ring_release_current_lane(void) {
	if (!g_lane_pages) {
		return; // this thread never attached a lane
	}
	int tid = (int)LINUX_SYSCALL(__NR_gettid);
	gr_lane_t* L = gr_find_lane(tid);
	if (!L) {
		return;
	}
	gr_release_lane(L);
}

// Attach a fresh ring lane for THIS thread: build the memfd, negotiate via ring_attach, and on success
// fill + PUBLISH the lane (active=1 last). Returns the live lane, or NULL on any failure (no lane / table
// full / server reject) -> the caller UDS-falls-back. This is the per-thread successor to P3's single
// shared attach; the body (memfd/mmap/cb fill/RPC) is unchanged -- only the destination is a per-thread
// lane instead of the g_* globals.
static void gr_attach_trace(const char* phase, int tid, long rc, uint32_t reject) {
	static int emitted[16];
	static int n = 0;
	if (n >= 12) {
		return;
	}
	++n;
	struct gr_proc_lane_rec* rec = gr_proc_find_active(tid);
	__simple_fprintf(2, "[dring-attach] %s pid=%d tid=%d image=%d adopted_view=%d registry_found=%d "
	                 "registry_state=%u registry_gen=%u rc=%ld reject=%u\n",
	                 phase, (int)LINUX_SYSCALL(__NR_getpid), tid, (int)GR_IMAGE_ID,
	                 gr_find_lane(tid) ? 1 : 0, rec ? 1 : 0,
	                 rec ? (unsigned)__atomic_load_n(&rec->state, __ATOMIC_RELAXED) : 0u,
	                 rec ? (unsigned)__atomic_load_n(&rec->generation, __ATOMIC_RELAXED) : 0u,
	                 rc, reject);
}

static gr_lane_t* gr_attach_lane(int tid) {
	gr_attach_trace("RING_ATTACH_BEGIN", tid, 0, 0);
	{
		static int probe = 0;
		if (probe < 3) {
			++probe;
			__simple_fprintf(2, "[dring-attach] slots pid=%d tid=%d b0_active=%u b0_owner=%d b0_borrowed=%u "
			                 "pages=%p\n",
			                 (int)LINUX_SYSCALL(__NR_getpid), tid,
			                 (unsigned)__atomic_load_n(&g_borrowed[0].active, __ATOMIC_ACQUIRE),
			                 (int)g_borrowed[0].owner_tid, (unsigned)g_borrowed[0].borrowed,
			                 (void*)g_lane_pages);
		}
	}
	if (gr_proc_find_active(tid)) {
		// CASE-A FIX: a process-global incarnation already exists for this tid, so this image must NOT
		// send a second ring_attach for it. Measured before this guard: the second attach retired the
		// shared lane server-side and wedged boot, with the guest having successfully adopted it moments
		// earlier. Refusing here leaves the caller on the datagram path for this call, which is always
		// safe, and keeps the incarnation alive for a later adoption.
		gr_attach_trace("RING_ATTACH_REFUSED", tid, 0, 0);
		return 0;
	}
	// perf#30: arbitrate the process-global incarnation FIRST. If another image already owns (or is
	// creating) this thread's lane, this image must not create a second one -- that duplicate is exactly
	// what used to make the server retire one of them and sentence an image to UDS for its lifetime.
	struct gr_proc_lane_rec* rec = 0;
	if (g_proc_lane_probed) {
		rec = gr_proc_begin(tid);
		if (!rec) {
			g_stat_proc_attach_yielded++;
			return 0; // the caller adopts the incarnation that already exists
		}
		g_stat_proc_attach_arbitrated++;
	}
	gr_lane_t* L = gr_claim_lane(tid); // CAS-claims a free slot (active 0 -> 2 "claiming")
	if (!L) {
		g_stat_lanes_exhausted++; // D17: a claim that found the table full (reason C if this is ever >0)
		// table full: every lane is in use by another live thread. Fall back to UDS for this thread (never
		// share a lane -- that would break SPSC). Log once so an undersized GR_MAX_LANES is visible.
		if (!g_lanes_exhausted_logged) {
			g_lanes_exhausted_logged = 1;
			/* ALWAYS NAMED, NO PROFILE GUARD (dar-4cp9). This message used to live inside
			 * #ifdef DARLING_RING_PHASE_PROF, so in a product build the single exit that means "this thread
			 * will have no lane for the rest of its life" printed nothing at all -- and the caller's own
			 * note then said ATTACH_FAILED, which reads like a transient failure rather than an exhausted
			 * catalog. The stage's rule is that a failure path must say which precondition it hit; this one
			 * now says how many pages and lanes the catalog had, and whether growing it was even attempted. */
			__simple_fprintf(2, "[dring] lane catalog exhausted pages=%u lanes/page=%u tid=%d claim-null=1\n",
				(unsigned)(gr_catalog_pages_used()), (unsigned)GR_LANE_PAGE_SIZE, tid);
		}
		return 0;
	}

	// Lay out: [control block][c2s ring][s2c ring], each ring 64-aligned.
	uint64_t hdr = sizeof(dserver_ring_shm_t);
	uint64_t ring_span = sizeof(dserver_ring_t) + (uint64_t)GR_SLOT_COUNT * GR_SLOT_SIZE;
	uint64_t c2s_off = (hdr + 63u) & ~63ull;
	uint64_t s2c_off = (c2s_off + ring_span + 63u) & ~63ull;
	uint64_t total   = (s2c_off + ring_span + 63u) & ~63ull;

	// On ANY failure below we must RELEASE the claimed slot (active back to 0) so it isn't leaked.
	// memfd_create("dring", MFD_CLOEXEC=0x1)
	long memfd = LINUX_SYSCALL(__NR_memfd_create, "dring", 0x1u);
	if (memfd < 0) { __simple_fprintf(2, "[dring-attach-fail] memfd tid=%d rc=%ld\n", tid, memfd); gr_proc_abandon(rec); __atomic_store_n(&L->active, 0u, __ATOMIC_RELEASE); return 0; }
	if (LINUX_SYSCALL(__NR_ftruncate, memfd, total) < 0) {
		__simple_fprintf(2, "[dring-attach-fail] ftruncate tid=%d bytes=%llu\n", tid, (unsigned long long)total);
		LINUX_SYSCALL1(__NR_close, memfd);
		__atomic_store_n(&L->active, 0u, __ATOMIC_RELEASE); return 0;
	}

	// map RW shared
#ifdef __NR_mmap2
	void* map = (void*)LINUX_SYSCALL(__NR_mmap2, 0, total, 0x1 | 0x2 /*PROT_READ|WRITE*/, 0x1 /*MAP_SHARED*/, memfd, 0);
#else
	void* map = (void*)LINUX_SYSCALL(__NR_mmap, 0, total, 0x1 | 0x2, 0x1, memfd, 0);
#endif
	if ((unsigned long)map > (unsigned long)-4096) {
		LINUX_SYSCALL1(__NR_close, memfd);
		__atomic_store_n(&L->active, 0u, __ATOMIC_RELEASE); return 0;
	}

	// A freshly ftruncate'd memfd is zero-filled by the kernel, so the rings/control block
	// start zeroed (head==tail==0 == empty, futex words 0); we only fill the cb fields.
	dserver_ring_shm_t* cb = (dserver_ring_shm_t*)map;
	cb->magic = DSERVER_RING_MAGIC;
	cb->abi_version = DSERVER_RING_ABI_VERSION;
	cb->slot_size = (uint16_t)GR_SLOT_SIZE;
	cb->slot_count = GR_SLOT_COUNT;
	cb->arena_off = 0;
	cb->arena_size = 0;
	cb->c2s_ring_off = (uint32_t)c2s_off;
	cb->s2c_ring_off = (uint32_t)s2c_off;
	cb->total_size = (uint32_t)total;
	cb->guest_tid = (int32_t)tid; // server cross-checks vs SCM nsid; this lane is attached by THIS thread
	// perf #18 dar-1il.2 item 2: publish OUR compiled C2S opcode set as a hash. The server rejects
	// the ring (-> reject_opcode_set -> we use UDS for everything) if it doesn't match the set the
	// SERVER was built with -- so a guest/server build/version skew degrades cleanly to all-UDS
	// instead of stranding us on the first op the older side doesn't allowlist. Same FNV fold both
	// sides compute from the IDENTICAL DSERVER_RING_C2S_OPCODES macro.
	cb->c2s_opcode_hash = dserver_ring_c2s_opcode_hash();
	// perf #18 P4 wake-model words. The memfd is zero-filled, so these are already 0, but be
	// explicit: server_state starts SLEEPING_EPOLL (== 0) so we doorbell until the server first
	// publishes ACTIVE_POLLING; no guest is parked yet so s2c_waiters starts 0.
	cb->server_state = DSERVER_RING_SRV_SLEEPING_EPOLL;
	cb->s2c_waiters = 0;

	// negotiate over UDS: hand the memfd to the server. It validates + maps + returns the
	// wake eventfd (or a non-zero reject reason). This is a per-THREAD attach -- the server keys the
	// ring on this thread's nsid (RingAttach::processCall) and registers it in _ringThreads, so the
	// main-loop spin phase drains every attached thread's lane independently. (Server already per-thread.)
	uint32_t reject = 0;
	int wake_fd = -1;
	gr_attach_trace("RING_ATTACH_RPC_SENT", tid, 0, 0);
	// perf#30 PHASE-0 ATTACH_LANE, ATTEMPT 2. The first attempt was RED with the server reporting zero
	// requests, which left the reason unknown because nothing was instrumented where the route DECIDES not
	// to run. This attempt answers that first, and BOUNDS the wait: an unanswered page degrades to the
	// datagram route instead of hanging a boot (the earlier attempt's unbounded wait is exactly what turned
	// "the server did not service the page" into "shellspawn did not become ready").
	int plane_route = 0;
	int claimed_no_lane = 0;
	int rc = 0;
	{
		struct dserver_process_control* page = gr_plane_attach_enabled()
			? (struct dserver_process_control*)__dserver_process_control_page() : NULL;
		int diag = gr_plane_attach_diag();
		if (page == NULL) {
			++gr_attach_no_page;
			if (diag) { __simple_fprintf(2, "[dring-plane-attach] no-page tid=%d\n", (int)tid); }
		} else {
			// WAIT for transport readiness, bounded. MEASURED: without this the earliest attaches (the
			// loader's seed) fall back to the datagram because the server has not mapped the region yet,
			// which is the whole of the `ring_attach uds=14` that was left on UDS. This is a TRANSPORT
			// wait, not a semantic round trip -- the server publishes `transport_ready` when it maps the
			// region, with no Process and no Thread in existence (the PHASE-0 rule), so waiting here
			// cannot depend on semantic guest registration.
	for (int waited_ms = 0; __atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE) == 0; ) {
				if (waited_ms >= 200) { break; }
				{
					long ts[2] = {0, 1000000L};
					(void)LINUX_SYSCALL6(__NR_nanosleep, ts, 0, 0, 0, 0, 0);
				}
				waited_ms += 1;
			}
			if (__atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE) == 0) {
				++gr_attach_ready_fail;
				__atomic_add_fetch(&page->attach_route_not_ready, 1u, __ATOMIC_RELAXED);
				if (diag) { __simple_fprintf(2, "[dring-plane-attach] not-ready tid=%d\n", (int)tid); }
			} else {
			uint64_t token = __dserver_fd_courier_send((int)memfd, DSERVER_FD_COURIER_KIND_LANE_BACKING);
			if (token == 0) {
				if (diag) { __simple_fprintf(2, "[dring-plane-attach] courier-refused tid=%d\n", (int)tid); }
			} else {
				// perf#30: ONE outstanding request per page is the model, so the slot is CLAIMED with a CAS
				// rather than published over. MEASURED hazard: several threads of one image publishing here
				// overwrite each other's sequence, the server answers one of them, and the others wait for a
				// reply that will never match their own sequence.
				int slot = 0;
				for (int t = 0; t < 2000 && !slot; ++t) {
					uint32_t expect = DSERVER_PROCESS_CONTROL_IDLE;
					if (__atomic_compare_exchange_n(&page->request_state, &expect,
					        DSERVER_PROCESS_CONTROL_PENDING, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
						slot = 1;
						break;
					}
					{
						long ts[2] = {0, 1000000L};
						(void)LINUX_SYSCALL6(__NR_nanosleep, ts, 0, 0, 0, 0, 0);
					}
				}
				if (!slot) {
					__atomic_add_fetch(&page->attach_route_no_slot, 1u, __ATOMIC_RELAXED);
					if (diag) { __simple_fprintf(2, "[dring-plane-attach] no-slot tid=%d\n", (int)tid); }
				} else {
				/* A PUBLISH THAT WAS NEVER CLAIMED MAY BE PUBLISHED AGAIN (dar-4cp9). MEASURED: the failing attach of
				 * a run is the [dring-plane-attach] unanswered case with waited_ms=1, i.e. the server never took
				 * the request at all -- no mutation, nothing to duplicate -- and the thread was then left without
				 * a lane, which is what a blocking operation later parks on. The label sits before every local
				 * this block declares, so a retry re-initialises them rather than continuing with stale state. */
				int attach_publish_attempts = 0;
			plane_attach_retry:
				++attach_publish_attempts;
				uint32_t mine = __atomic_add_fetch(&gr_plane_attach_seq, 1u, __ATOMIC_RELAXED);
				page->reply_state = DSERVER_PROCESS_CONTROL_IDLE;
				page->request_op = DSERVER_PROCESS_CONTROL_OP_ATTACH_LANE;
					// perf#30 (directive D1): THREAD IDENTITY travels with every request, including this
					// hand-rolled publisher: a management request that carries no identity is refused
					// (-ESRCH), never serviced against whatever thread the pid resolves to.
					page->request_tid = (int32_t)LINUX_SYSCALL(__NR_gettid);
				page->request_seq = mine;
				page->request_payload[0] = (uint64_t)tid;
				page->request_payload[1] = total;
				page->request_payload[2] = token;
				// bits 0-7: architecture (low byte, as the server reads it). bits 8+: the IMAGE identity of
				// the caller, which the server cannot otherwise know for a page request -- and which the
				// attach census needs in order to say whether the caller was mldr or the guest dylib.
				page->request_payload[3] = ((uint64_t)GR_IMAGE_ID << 8) | 1u;
				__MLDR_PLANE_STEP("G-publish", DSERVER_PROCESS_CONTROL_OP_ATTACH_LANE);
	__atomic_store_n(&page->request_state, DSERVER_PROCESS_CONTROL_PENDING, __ATOMIC_RELEASE);
	__MLDR_PLANE_STEP("H-published", DSERVER_PROCESS_CONTROL_OP_ATTACH_LANE);
				{
					int courier = __dserver_fd_courier_socket();
					if (courier >= 0) {
						char wake = 0;
						LINUX_SYSCALL6(__NR_sendto, courier, &wake, 1, 0x40 /*MSG_DONTWAIT*/, 0, 0);
					}
				}
				uint32_t spins = 0;
				uint32_t waited_ms = 0;
				int claimed = 0;
				/* A COMPLETION BELONGS TO ITS PUBLISHER (dar-4cp9): MEASURED, the attach loop exited one millisecond
				 * in because a request still in flight when this thread reset the slot wrote its own DONE over the
				 * reset, and the caller then declared the attach unanswered although the page was simply not read
				 * correctly. Wait for OUR sequence; a foreign DONE is a stale answer, not our completion. */
				while (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) != DSERVER_PROCESS_CONTROL_DONE
				       || __atomic_load_n(&page->reply_seq, __ATOMIC_ACQUIRE) != (uint32_t)mine) {
					// CLAIMED means ownership moved to the server's transaction. It is NOT completion, and
					// from here the guest must wait for completion: the two RED attempts came from treating
					// a still-pending claim as a reason to fall back, which attached the same lane twice.
					if (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_CLAIMED) {
						claimed = 1;
					}
					if (++spins < 20000) { continue; }
					// Bounded even while CLAIMED: ownership transfer means "keep waiting for COMPLETION", not
					// "wait forever". MEASURED: a 20 s bound here blew the 30 s rootless shellspawn handshake
					// (`HELLO=0`), so the bound is short enough that a boot cannot be spent in one wait, and the
					// give-up path is the no-fallback one below (a lane-less thread, counted, never a second
					// attach).
					if (waited_ms >= (claimed ? 3000u : 1000u)) { break; }
					{
						uint32_t seen = page->futex;
						// raw kernel ABI for the futex timeout: {tv_sec, tv_nsec}, 1 ms. This file is
						// libc-free, so there is no struct timespec to name.
						long ts[2] = {0, 1000000L};
						(void)LINUX_SYSCALL6(__NR_futex, &page->futex, 0 /*FUTEX_WAIT*/, seen, ts, 0, 0);
						waited_ms += 1;
					}
				}
				// read before release (doc 71): releasing first lets the next publisher overwrite the fields.
				__MLDR_PLANE_STEP("J-completed", DSERVER_PROCESS_CONTROL_OP_ATTACH_LANE);
	uint32_t seenState = __atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE);
				uint32_t seenSeq = page->reply_seq;
	{ uint32_t _st = __atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE); if (_st == DSERVER_PROCESS_CONTROL_PENDING) { static const char _m[] = "[release-drops-pending] site=dserver-ring.c:1518\n"; long _a = 1, _d = 2, _s = (long)_m, _n = sizeof(_m) - 1; __asm__ volatile("syscall" : "+a"(_a), "+D"(_d), "+S"(_s), "+d"(_n) : : "rcx", "r11", "memory"); } }
				__MLDR_PLANE_STEP("K-snapshot", DSERVER_PROCESS_CONTROL_OP_ATTACH_LANE);
	/* A GIVE-UP MUST NOT TAKE BACK A SLOT THE SERVER OWNS (dar-b5pe), AND IT MUST STILL RETURN A SPENT ONE. MEASURED:
	 * a CAS release from PENDING alone broke the boot twelve times out of twelve, because the plain store it
	 * replaced also returned a slot left at DONE -- and the code's own comment records that leaving a slot at DONE
	 * breaks the boot worse than any other choice. So the only thing this changes is the CLAIMED case: the server
	 * owns the request, and the slot stays as it is until its completion arrives. */
	if (__atomic_load_n(&(page)->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_CLAIMED) {
		static int __release_held = 0;
		if (!__release_held) {
			__release_held = 1;
			__simple_fprintf(2, "[release-held-claimed] op=%u state=%u rstate=%u tid=%d\n",
				(unsigned)__atomic_load_n(&(page)->request_op, __ATOMIC_ACQUIRE),
				(unsigned)__atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE),
				(unsigned)__atomic_load_n(&(page)->reply_state, __ATOMIC_ACQUIRE),
				(int)LINUX_SYSCALL(__NR_gettid));
		}
	} else {
		DSERVER_PROCESS_CONTROL_RELEASE(page);
	}
	__MLDR_PLANE_STEP("L-released", DSERVER_PROCESS_CONTROL_OP_ATTACH_LANE);
				if (seenState == DSERVER_PROCESS_CONTROL_DONE &&
				    seenSeq == mine) {
					rc = page->reply_status;
					reject = (uint32_t)page->reply_payload[0];
					{
						uint64_t wakeToken = page->reply_payload[1];
						if (wakeToken != 0) {
							wake_fd = __dserver_fd_courier_receive(wakeToken);
						}
					}
					// A NEGATIVE status is a refusal by the page route, not an attach: nothing was
					// mapped and no lane exists. MEASURED (attempt 2): the server answered -9 (EBADF)
					// while the descriptor half was still in flight on the courier, and treating that as
					// success left the guest believing in a lane it never got -- which is what broke the
					// boot. Every other outcome falls back to the datagram route.
					if (rc < 0) {
						__atomic_add_fetch(&page->attach_route_refused, 1u, __ATOMIC_RELAXED);
						reject = 0;
						wake_fd = -1;
						if (diag) {
							__simple_fprintf(2, "[dring-plane-attach] refused tid=%d rc=%d\n", (int)tid, rc);
						}
					} else {
						plane_route = 1;
						__atomic_add_fetch(&page->attach_route_ok, 1u, __ATOMIC_RELAXED);
						if (diag) {
							__simple_fprintf(2, "[dring-plane-attach] ok tid=%d rc=%d reject=%u wake=%d\n",
								(int)tid, rc, (unsigned)reject, wake_fd);
						}
					}
				} else if (claimed) {
					// The server owns this attach and has not completed it. Do NOT fall back: no second
					// attach, and the thread simply runs on the datagram until the lane exists.
					__atomic_add_fetch(&page->attach_route_claimed_no_lane, 1u, __ATOMIC_RELAXED);
					claimed_no_lane = 1;
					if (diag) {
						__simple_fprintf(2, "[dring-plane-attach] claimed-no-lane tid=%d seq=%u state=%u\n",
							(int)tid, (unsigned)mine,
							(unsigned)__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE));
					}
				} else {
					// never claimed: safe to publish again, bounded
					if (attach_publish_attempts < 3) {
						__simple_fprintf(2, "[dring-plane-attach] republish tid=%d attempt=%d\n",
							(int)tid, attach_publish_attempts);
						goto plane_attach_retry;
					}
					if (diag) {
						__simple_fprintf(2, "[dring-plane-attach] unanswered tid=%d seq=%u state=%u waited_ms=%u\n",
							(int)tid, (unsigned)mine,
							(unsigned)__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE),
							(unsigned)waited_ms);
					}
				}
				}
			}
			}
		}
	}
	if (claimed_no_lane) {
		/* A CLAIMED ATTACH IS A GRANTED ATTACH, JUST NOT YET VISIBLE HERE (dar-4cp9). MEASURED: two tids per run
		 * reached this branch while the server HAD granted their lanes -- [srv-lane-attach ... ] is present for
		 * both, and each of those tids then missed its lane ten to sixteen times afterwards -- because the
		 * accept test requires reply_seq == mine and the single plane reply slot had been reused by another
		 * publisher before this thread read it. The lane is a fact in the guest's own table, so the bounded
		 * thing to do is wait for that fact instead of concluding the thread has no home: the server has
		 * already been told to attach it, and a second attach is exactly what this branch exists to avoid. */
		/* Bound matched to the attach's own claim bound (3 s): a lane the server has claimed is granted on the
		 * server's schedule, and MEASURED that 0.5 s was too short -- eight claimed attaches per run, of which
		 * the ones that did land arrived later. Stated as a count of 1 ms sleeps, like the wait above. */
		for (int w = 0; w < 3000; ++w) {
			/* The lane may have been created BY THE SERVER for this tid while this image's own entry never got
			 * filled -- the process-global directory is the mechanism for exactly that, so ask it too, not only
			 * the local table. MEASURED: eight such attaches per run, each with a server-side srv-lane-attach,
			 * and the guest's table stayed empty; asking only gr_find_lane() therefore could not succeed. */
			gr_lane_t* landed = gr_find_lane(tid);
			if (!landed) {
				landed = gr_adopt_process_lane(tid);
			}
			if (landed && landed->state == 1) {
				__atomic_store_n(&landed->active, 1u, __ATOMIC_RELEASE);
				gr_attach_trace("RING_ATTACH_LANDED_AFTER_CLAIM", tid, 0, 0);
				LINUX_SYSCALL(__NR_munmap, map, total); /* our mapping is not needed once the server holds it */
				LINUX_SYSCALL1(__NR_close, memfd);
				return landed;
			}
			{ /* 1 ms without libc: the kernel's timespec layout, declared inline so no header is needed */
			  struct { long tv_sec; long tv_nsec; } _ts = {0, 1000000L};
			  LINUX_SYSCALL(__NR_nanosleep, &_ts, 0); }
		}
		/* NAME WHAT THE TWO TABLES SAY when the bound expires, so the next read is a measurement rather than a
		 * guess: whether the process-global directory already holds this tid's incarnation (the server granted
		 * it and only this image failed to adopt) or nothing exists at all (the attach never completed). */
		{
			gr_lane_t* local = gr_find_lane(tid);
			{
				struct dserver_process_control* pgT =
					(struct dserver_process_control*)__dserver_process_control_page();
				__simple_fprintf(2, "[dring-plane-attach] claimed-lane-timeout tid=%d local=%d state=%d dir=%d"
					" replyseq=%u replstate=%u\n",
					(int)tid, local ? 1 : 0, local ? (int)local->state : -1,
					(int)(gr_proc_find_active(tid) != 0),
					(unsigned)(pgT ? __atomic_load_n(&pgT->reply_seq, __ATOMIC_ACQUIRE) : 0xffffffffu),
					(unsigned)(pgT ? __atomic_load_n(&pgT->reply_state, __ATOMIC_ACQUIRE) : 0xffffffffu));
			}
		}
		/* THE GRANTED LANE CAN BE UNREACHABLE, SO ASK AGAIN (dar-4cp9). MEASURED with the paired trace: at this
		 * timeout the reply slot holds a DONE belonging to SOMEONE ELSE (replyseq differs from ours) while the
		 * server has been dispatching attaches all along (~200 per run), and neither the local table nor the
		 * process directory has this tid (local=0 dir=0). So the lane exists server-side and the client simply
		 * never learned it. Re-publishing cannot duplicate anything the server did not already do for a
		 * DIFFERENT sequence: the transaction map is keyed by (pid, seq), so a new sequence is a new transaction,
		 * and the guards inside gr_attach_lane and gr_adopt_process_lane still prevent a second attach for an
		 * incarnation that is already there. Bounded, exactly like the unclaimed case. */
		// Still nothing after the bound: the server owns the attach; this image must not create a second one.
		// Release the claimed slot and let the thread run without a lane: correctness first, and the counter
		// says how often this happens.
		LINUX_SYSCALL(__NR_munmap, map, total);
		LINUX_SYSCALL1(__NR_close, memfd);
		__atomic_store_n(&L->active, 0u, __ATOMIC_RELEASE);
		return 0;
	}
	if (!plane_route) {
		/* THE REMOVED TRANSPORT IS NOT AN ANSWER (dar-4cp9). MEASURED: the failing attach of a run is the
		 * [dring-plane-attach] unanswered case -- the plane request was never even claimed -- and this
		 * fallback then called dserver_rpc_ring_attach, whose transport hook declines and whose refusal path
		 * runs __simple_abort(). So a thread that merely failed to get a reply was killed, and the crash
		 * frames read pthread_create -> __darling_thread_create -> sys_bsdthread_create, which points the
		 * reader at thread creation instead of at the transport that no longer exists.
		 * The plane is now the ONLY route for an attach: no reply means no lane for this attempt, reported
		 * by name and handled by the caller (which retries the lookup later), never a process death. */
		__simple_fprintf(2, "[dring-attach-fail] plane-unanswered-no-uds tid=%d\n", tid);
		LINUX_SYSCALL(__NR_munmap, map, total);
		LINUX_SYSCALL1(__NR_close, memfd);
		__atomic_store_n(&L->active, 0u, __ATOMIC_RELEASE);
		return 0;
	}
	gr_attach_trace("RING_ATTACH_END", tid, (long)rc, reject);

	// we no longer need the memfd ourselves (our mapping holds a reference).
	LINUX_SYSCALL1(__NR_close, memfd);

	// perf#28d (round 49): the reply carries a doorbell descriptor ONLY on the process incarnation's first
	// attach. A later attach legitimately has wake_fd == -1, and the descriptor it must use is the one the
	// shared loader already owns -- asked of the owner at every use, never remembered as a number. So a
	// missing wake fd is only a failure when there is no doorbell anywhere in this process.
	/* rc IS NOT AN ERROR CODE ON THE PLANE ROUTE (dar-4cp9). MEASURED: the plane publishes the attach's reply
	 * status in reply_status, which on success is the opened descriptor number -- a run failed with
	 * [dring-attach-fail] reject rc=15 reject=0, i.e. a POSITIVE, perfectly good result read as a failure by a
	 * check written for the old RPC route (where nonzero meant the attach did not happen). The plane route
	 * already decides success by rc < 0 (a refusal) and by reject, so 'rc != 0 means failure' now applies only
	 * to the route it was written for -- which no longer exists. */
	if ((!plane_route && rc != 0) || reject != dserver_ring_ok) {
		__simple_fprintf(2, "[dring-attach-fail] reject tid=%d rc=%d reject=%u plane=%d\n", tid, rc, (unsigned)reject, plane_route);
		LINUX_SYSCALL(__NR_munmap, map, total);
		if (wake_fd >= 0) LINUX_SYSCALL1(__NR_close, wake_fd);
		__atomic_store_n(&L->active, 0u, __ATOMIC_RELEASE); // release the claimed slot on reject
		return 0;
	}
	// ATTRIBUTION (round 49s): the exact case the fifth attempt failed in -- the reply carried no wake fd
	// because the server's one-time rule returned -1. Bounded, and only on this branch.
	if (wake_fd < 0) {
		static int logged = 0;
		if (logged < 8) {
			++logged;
			__simple_fprintf(2, "[dring-attach] no-wake-fd pid=%d tid=%d rc=%d reject=%u owned=%d\n",
				(int)LINUX_SYSCALL(__NR_getpid), (int)LINUX_SYSCALL(__NR_gettid), rc, reject,
				__dserver_ring_doorbell(-1));
		}
	}
	if (wake_fd < 0 && __dserver_ring_doorbell(-1) < 0) {
		__simple_fprintf(2, "[dring-attach-fail] no-doorbell tid=%d rc=%d reject=%u\n", tid, rc, (unsigned)reject);
		LINUX_SYSCALL(__NR_munmap, map, total);
		__atomic_store_n(&L->active, 0u, __ATOMIC_RELEASE);
		return 0;
	}

	// perf#28 (ONE doorbell): the descriptor that just arrived is a dup of the SERVER's single Ring
	// doorbell, not a lane-private wake fd. The shared loader keeps the first dup it is given and
	// closes every later one, returning the canonical fd -- so N lanes, in both images, cost ONE
	// wake fd for the whole Linux process. The lane stores that as a NON-OWNING reference (see the
	// release path): the doorbell outlives every lane because the transport outlives every lane.
	int doorbell_fd = __dserver_ring_doorbell(wake_fd); // consumes wake_fd: adopts it, or closes a dup
	if (doorbell_fd < 0) {
		__simple_fprintf(2, "[dring-attach-fail] doorbell-adopt tid=%d wake=%d\n", tid, wake_fd);
		LINUX_SYSCALL(__NR_munmap, map, total);
		__atomic_store_n(&L->active, 0u, __ATOMIC_RELEASE);
		return 0;
	}
	wake_fd = doorbell_fd;
	// perf#28 (ONE doorbell) PRODUCT evidence: one line per image per process, carrying the fd the
	// shared loader resolved. Both images of the same process print the SAME number, which is the
	// multi-image singleton claim stated as data instead of assumed from the code shape.
	{
		static int doorbell_logged = 0;
		if (!doorbell_logged) {
			doorbell_logged = 1;
		#ifdef VARIANT_DYLD
			__simple_fprintf(2, "[dring-doorbell] pid=%d tid=%d image=dyld fd=%d\n", (int)LINUX_SYSCALL(__NR_getpid), (int)LINUX_SYSCALL(__NR_gettid), doorbell_fd);
		#else
			__simple_fprintf(2, "[dring-doorbell] pid=%d tid=%d image=kernel fd=%d\n", (int)LINUX_SYSCALL(__NR_getpid), (int)LINUX_SYSCALL(__NR_gettid), doorbell_fd);
		#endif
		}
	}

	// INITIALIZE the lane fully, then PUBLISH active=1 LAST (the gate's INVARIANT 1). Bump the generation
	// on this (re)claim so a recycled TID landing on this slot can never match a prior epoch (INVARIANT 2).
	L->map = map;
	L->size = total;
	L->wake_fd = wake_fd;
	L->owner_tid = tid;
	L->seq = 1;
	L->state = 1;
	L->proc_rec = rec;
	L->borrowed = 0;
	if (L->generation != 0) {
		g_stat_lanes_reclaimed++; // D17: this slot held a prior epoch -> it is being REUSED (not first use)
	}
	L->generation = L->generation + 1; // distinct epoch for this claim
	// The attach above used the loader's elfcalls (the process doorbell), so from here on the directory
	// may be resolved safely.
	g_proc_probe_allowed = 1;
	{
		struct gr_proc_lane_rec* existing = gr_proc_find_active(tid);
		if (existing) {
			// Another image incarnated this thread while we were attaching. Keep ONE incarnation: drop
			// ours (the mapping we own) and continue on theirs, borrowed.
			void*    other_map  = (void*)existing->mapping;
			uint64_t other_size = (uint64_t)existing->mapping_size;
			if (other_map && other_size) {
				LINUX_SYSCALL(__NR_munmap, L->map, L->size);
				L->map = other_map;
				L->size = other_size;
				L->wake_fd = -1;
				L->generation = existing->generation;
				L->proc_rec = existing;
				L->borrowed = 1;
				L->seq = __atomic_load_n(&gr_c2s(L)->tail, __ATOMIC_RELAXED) + 1;
				gr_proc_abandon(rec);
				g_stat_proc_attach_yielded++;
				g_stat_lanes_acquired++;
				return L; // still owns a LOCAL slot; the incarnation belongs to the other image
			}
		}
	}
	gr_proc_publish(rec, L->generation, L->map, L->size, L->slot_index); // ACTIVE last, after the mapping
	g_stat_lanes_acquired++; // D17: a successful per-thread attach in this process ("lanes acquired")
	__atomic_store_n(&L->active, 1u, __ATOMIC_RELEASE); // publish LAST: lane now usable + findable
	// perf#26 RING-MACH-MSG: lane-aware diagnostic identity. A lane SLOT is recycled and a re-attach
	// restarts seq at 1, so (tid,seq) alone cannot tell a duplicate request from the same tid+seq on a
	// new lane epoch. The attach is reported by the trace on the FIRST transaction of the lane (the
	// publish line carries lane+gen), deliberately not printed here: gr_attach_lane also runs inside the
	// dyld image's early startup, where the guest's stderr path is not established yet.
	return L;
}

// perf#29 (NON-FD UDS COVERAGE): WHY a call chose UDS. The old census could only say "the server saw
// this callnum over UDS while the thread had a lane" -- it could not say what the GUEST knew at the
// decision point. These reasons are computed from the lane the guest itself looked up, at the moment it
// looked it up, and each miss is emitted once per (callnum, reason) pair so a census can attribute the
// traffic by callnum AND cause without flooding a boot log.
enum {
	GR_URS_NO_LANE_ENTRY = 0,       // no catalog slot for this tid at all
	GR_URS_ATTACH_NOT_STARTED,      // slot claimed, not yet published
	GR_URS_ATTACH_IN_PROGRESS,      // attach in flight on this thread
	GR_URS_ATTACH_FAILED,           // attach was rejected (server said no / lanes exhausted)
	GR_URS_LANE_NOT_ACTIVE,         // slot exists but is not usable (out of service / retired)
	GR_URS_OWNER_TID_MISMATCH,      // slot belongs to another tid
	GR_URS_GENERATION_MISMATCH,     // slot epoch does not match this thread's epoch
	GR_URS_CALL_NOT_RING_ELIGIBLE,  // callnum has no Ring path
	GR_URS_PAYLOAD_NOT_SUPPORTED,   // shape does not fit the slot
	GR_URS_BOOTSTRAP_REQUIRED,      // identity needed before a lane can exist
	GR_URS_TEARDOWN_AFTER_RELEASE,  // lane already released; final lifecycle RPC
	GR_URS_FD_TRANSFER_REQUIRED,    // the operation moves a Linux fd
	GR_URS_IMAGE_LOCAL_STATE,       // this image has no lane table / different image owns the lane
	GR_URS_RING_FULL,               // the c2s ring had no free slot at publish time (backpressure)
	GR_URS_OTHER,
	GR_URS_COUNT
};
static const char* gr_urs_name(int r) {
	static const char* names[GR_URS_COUNT] = {
		"NO_LANE_ENTRY", "ATTACH_NOT_STARTED", "ATTACH_IN_PROGRESS", "ATTACH_FAILED",
		"LANE_NOT_ACTIVE", "OWNER_TID_MISMATCH", "GENERATION_MISMATCH", "CALL_NOT_RING_ELIGIBLE",
		"PAYLOAD_NOT_SUPPORTED", "BOOTSTRAP_REQUIRED", "TEARDOWN_AFTER_LANE_RELEASE",
		"FD_TRANSFER_REQUIRED", "IMAGE_LOCAL_STATE", "RING_FULL", "OTHER"
	};
	return (r >= 0 && r < GR_URS_COUNT) ? names[r] : "OTHER";
}
static uint64_t gr_urs_counts[GR_URS_COUNT];       // per-reason histogram (always maintained)
static int gr_urs_emitted[GR_URS_COUNT][64];       // one emit per (reason, callnum%64) pair
static int gr_urs_diag_cached = -1;
static int gr_urs_diag_enabled(void) {
	static const char key[] = "DARLING_GUEST_LANE_DIAG=1";
	if (gr_urs_diag_cached < 0) {
		gr_urs_diag_cached = gr_environ_has(key, sizeof(key) - 1) ? 1 : 0;
	}
	return gr_urs_diag_cached;
}
// Compute the reason from the guest's OWN view of the lane table (no attach attempt).
static int gr_urs_reason_for_miss(int tid) {
	gr_lane_t* L = gr_find_lane(tid);
	if (!L) {
		return GR_URS_NO_LANE_ENTRY;
	}
	int state = L->state;
	uint32_t active = __atomic_load_n(&L->active, __ATOMIC_RELAXED);
	if (state == 0) return GR_URS_ATTACH_NOT_STARTED;
	if (state == -1) return GR_URS_ATTACH_FAILED;
	if (L->owner_tid != tid) return GR_URS_OWNER_TID_MISMATCH;
	if (active != 1u) return GR_URS_LANE_NOT_ACTIVE;
	return GR_URS_LANE_NOT_ACTIVE; // attached-but-unspecified: treat conservatively
}
static void gr_urs_note(uint32_t callnum, const char* name, int reason, gr_lane_t* lane) {
	if (reason < 0 || reason >= GR_URS_COUNT) reason = GR_URS_OTHER;
	__atomic_fetch_add(&gr_urs_counts[reason], 1u, __ATOMIC_RELAXED);
	int slot = (int)(callnum % 64u);
	// MEASURED (round 49m): gating this on DARLING_GUEST_LANE_DIAG meant the reason never appeared,
	// because the processes that take these fallbacks do not run the code paths that read that hatch.
	// The emission is now UNCONDITIONAL but doubly bounded -- one line per (reason, callnum) pair and
	// at most a handful per process -- so a fallback's reason is always visible somewhere and no boot
	// log is flooded.
	static int gr_urs_emit_count = 0;
	// perf#30 (doc section 206): the budget was 8 per process, and MEASURED: the denial of
	// `semaphore_timedwait` arrived at log line 1356 -- far past the point where eight earlier misses had
	// spent the budget -- so the reason for the miss that mattered was the one line that could not be
	// printed. A bounded budget has to be large enough to cover a whole boot, and the per-(reason,callnum)
	// de-duplication below is what keeps it from flooding.
	if (gr_urs_emitted[reason][slot] || gr_urs_emit_count >= 96) {
		return;
	}
	gr_urs_emitted[reason][slot] = 1;
	++gr_urs_emit_count;
	static const char image_dyld[] = "dyld";
	static const char image_kernel[] = "kernel";
	const char* image = image_kernel;
#ifdef VARIANT_DYLD
	image = image_dyld;
#endif
	__simple_fprintf(2, "[dring-uds-reason] pid=%d tid=%d image=%s callnum=%u name=%s reason=%s "
	                 "lane_slot=%u lane_gen=%u lane_state=%d lane_active=%u\n",
	                 (int)LINUX_SYSCALL(__NR_getpid), (int)LINUX_SYSCALL(__NR_gettid), image,
	                 callnum, name ? name : "?", gr_urs_name(reason),
	                 lane ? lane->slot_index : 0u, lane ? lane->generation : 0u,
	                 lane ? lane->state : 0, lane ? __atomic_load_n(&lane->active, __ATOMIC_RELAXED) : 0u);
}

// Resolve THIS thread's lane: return its already-attached lane, or attach a fresh one, or NULL (no lane
// for this thread -> UDS fallback). This is the per-thread successor to the P3 single-owner gate.
// `callnum`/`name` exist only so a miss can be attributed to the operation that caused it.
// Build this image's BORROWED view of the process-global incarnation: same mapping VA (one Linux address
// space), same control block, same SPSC rings -- and the SAME host thread is the producer, so the
// single-producer rule still holds even though the code executing now lives in a different image. The
// view owns nothing: no attach, no munmap, no doorbell.
static gr_lane_t* gr_adopt_process_lane(int tid) {
	struct gr_proc_lane_rec* r = gr_proc_find_active(tid);
	{
		static int reported = 0;
		if (!reported) {
			reported = 1;
			__simple_fprintf(2, "[dring-adopt] pid=%d tid=%d registry=%p slots=%d found=%d "
			                 "gate=%d doorbell=%d regelfc=%p slotelfc=%d\n",
			                 (int)LINUX_SYSCALL(__NR_getpid), tid, (void*)g_proc_lanes, g_proc_lane_slots,
			                 r ? 1 : 0, g_proc_probe_allowed, __dserver_ring_doorbell(-1),
			                 __dserver_ring_lane_registry(), __dserver_ring_lane_slots());
		}
	}
	if (!r) {
		return 0;
	}
	// Validate liveness BEFORE building a view. A retired incarnation is not a lane: adopting it would
	// hand the caller a control block that immediately reads RETIRED, which is exactly the cycle that
	// made the adopting image spin without ever publishing anything.
	uint32_t ci = __atomic_load_n(&r->creator_image, __ATOMIC_RELAXED);
	{
		void* probe_map = (void*)__atomic_load_n(&r->mapping, __ATOMIC_RELAXED);
		uint32_t srv_state = probe_map ? __atomic_load_n(&((dserver_ring_shm_t*)probe_map)->server_state, __ATOMIC_ACQUIRE) : 0u;
		{
			static int reported_srv = 0;
			if (!reported_srv) {
				reported_srv = 1;
				__simple_fprintf(2, "[dring-adopt] liveness pid=%d tid=%d map=%p server_state=%u gen=%u "
			                 "creator_image=0x%x mode=%u\n",
				                 (int)LINUX_SYSCALL(__NR_getpid), tid, probe_map, srv_state,
				                 (unsigned)__atomic_load_n(&r->generation, __ATOMIC_RELAXED),
			                 (unsigned)__atomic_load_n(&r->creator_image, __ATOMIC_RELAXED),
			                 (unsigned)((ci & 0x100u) ? (ci & 0xffu) : 2u));
			}
		}
		if (probe_map && srv_state == DSERVER_RING_SRV_RETIRED) {
			gr_proc_abandon(r);
			return 0;
		}
	}
	// The adoption mode is published by the LOADER in the record (0x100|mode): the guest must not scan
	// the environment this early -- an env scan in the early Ring path is a known boot hazard.
	int mode = (ci & 0x100u) ? (int)(ci & 0xffu) : 2; // bit 8 set == the loader published this record
	if (mode == 0) {
		return 0; // control arm: never adopt (the sibling keeps its legacy attach)
	}
	void* map = (void*)__atomic_load_n(&r->mapping, __ATOMIC_RELAXED);
	// plain read: the record publishes mapping_size BEFORE its ACTIVE release-store, and we only reach
	// here after an ACQUIRE load of state, so the value is ordered. (A 64-bit atomic load does not exist
	// on the i386 slice of this dylib, and this is the file's convention for multi-word fields anyway.)
	uint64_t size = (uint64_t)r->mapping_size;
	if (!map || !size) {
		return 0;
	}
	{
		static int reported2 = 0;
		if (!reported2) {
			reported2 = 1;
			__simple_fprintf(2, "[dring-adopt] pre-claim pid=%d pages=%p (a claim may mmap: an EMULATED syscall)\n",
			                 (int)LINUX_SYSCALL(__NR_getpid), (void*)g_lane_pages);
		}
	}
	gr_lane_t* L = 0;
	for (int i = 0; i < GR_BORROWED_SLOTS; ++i) {
		uint32_t expected = 0;
		if (__atomic_compare_exchange_n(&g_borrowed[i].active, &expected, 2u, false,
		                                __ATOMIC_ACQ_REL, __ATOMIC_RELAXED)) {
			L = &g_borrowed[i];
			break;
		}
	}
	if (!L) {
		L = gr_claim_lane(tid); // static slots exhausted (unusual): fall back to the catalog
	}
	if (!L) {
		return 0;
	}
	{
		static int reported3 = 0;
		if (!reported3) {
			reported3 = 1;
			__simple_fprintf(2, "[dring-adopt] post-claim pid=%d ok\n", (int)LINUX_SYSCALL(__NR_getpid));
		}
	}
	L->map = map;
	L->size = size;
	// The borrowed view MUST still be able to doorbell the server: gr_wake_server() writes to this
	// descriptor. Setting it to -1 (as this path first did) silently turned every wake into EBADF, so a
	// sibling image that adopted a lane could publish a request and never wake the server -- measured as a
	// boot break in dyld. It is the CANONICAL process doorbell fd, borrowed: the release path still closes
	// nothing, because the descriptor belongs to the transport, not to the lane.
	L->wake_fd = __dserver_ring_doorbell(-1);
	L->owner_tid = tid;
	// seq must continue the SHARED ring's own progression, not restart at 1: the producer index is the
	// authoritative counter and the server correlates replies by seq.
	L->seq = __atomic_load_n(&gr_c2s(L)->tail, __ATOMIC_RELAXED) + 1;
	L->generation = __atomic_load_n(&r->generation, __ATOMIC_RELAXED);
	L->proc_rec = r;
	L->borrowed = 1;
	L->state = 1;
	__atomic_store_n(&L->active, 1u, __ATOMIC_RELEASE);
	g_stat_proc_adopted++;
	if (mode == 1) {
		// BISECTION arm "view": the borrowed view was created (counters, fields, lifetime) but the
		// sibling does NOT use it -- it still takes its own attach path, exactly as before adoption.
		// GREEN here with RED in "full" isolates the defect to USING the borrowed incarnation.
		__atomic_store_n(&L->active, 0u, __ATOMIC_RELEASE);
		L->proc_rec = 0;
		L->borrowed = 0;
		return 0;
	}
	return L;
}

static gr_lane_t* gr_lane_for_this_thread_named(uint32_t callnum, const char* name) {
	int tid = (int)LINUX_SYSCALL(__NR_gettid);
	gr_lane_t* L = gr_find_lane(tid);
	if (L) {
		if (L->state == 1 &&
		    __atomic_load_n(&gr_cb(L)->server_state, __ATOMIC_ACQUIRE) == DSERVER_RING_SRV_RETIRED) {
			// Another image now owns this thread's server-side lane. Keep this
			// image on UDS rather than stealing it back on every image switch --
			// and, perf#27, give the CATALOG slot back: this image will never use
			// this lane again, and holding it would leak a slot per image switch
			// exactly the way thread exit used to leak one per historical thread.
			int was_borrowed = (int)L->borrowed;
			gr_release_lane(L);
			if (was_borrowed) {
				gr_proc_unpublish(tid); // the shared incarnation is dead: nobody may adopt it again
				// Measured (RING_ATTACH_BEGIN with adopted_view=0 right after a successful adoption):
				// a borrowed view whose server-side lane is already retired used to fall through to
				// gr_attach_lane(), which sent a SECOND ring_attach for the SAME tid. That is the
				// Case-A defect: after a successful adoption for tid T this thread must not enter attach
				// again -- the safe action is the datagram path for this call.
				__simple_fprintf(2, "[dring-lane-miss] retired-borrowed tid=%d state=%d borrowed=%d owner=%d\n",
					tid, (int)L->state, (int)L->borrowed, (int)L->owner_tid);
				gr_urs_note(callnum, name, GR_URS_LANE_NOT_ACTIVE, 0);
				return 0;
			}
			// perf#30: the incarnation did NOT die -- another image created it. ADOPT the
			// process-global record instead of falling back to UDS for the rest of this image's
			// life. IMAGE_LOCAL_STATE survives only for the case where there is genuinely nothing
			// to adopt (no directory, or the owner already retired it).
			gr_lane_t* adopted_after_retire = gr_adopt_process_lane(tid);
			if (adopted_after_retire) {
				return adopted_after_retire;
			}
			__simple_fprintf(2, "[dring-lane-miss] retired-unadoptable tid=%d state=%d borrowed=%d owner=%d\n",
				tid, (int)L->state, (int)L->borrowed, (int)L->owner_tid);
			gr_urs_note(callnum, name, GR_URS_IMAGE_LOCAL_STATE, 0);
			return 0;
		}
		if (L->state != 1) {
			/* THE BRANCH THAT PRODUCED THE CONTRADICTION (dar-4cp9). The miss label NO_LANE_ENTRY can only come
			 * from gr_urs_reason_for_miss() finding no slot at all, yet this branch runs only when the OUTER
			 * gr_find_lane() DID find a lane -- so either the lane was released in between, or the two lookups
			 * disagree. Print what the outer lookup holds and what the reason helper recomputes, so the two are
			 * compared instead of inferred. */
			__simple_fprintf(2, "[dring-lane-miss] state-not-1 tid=%d state=%d borrowed=%d owner=%d inner-find=%d\n",
				tid, (int)L->state, (int)L->borrowed, (int)L->owner_tid,
				(int)(gr_find_lane(tid) != 0));
			/* A SLOT THAT EXISTS BUT IS NOT USABLE IS A HOLE, NOT AN ANSWER (dar-4cp9). MEASURED: the residual hangs
			 * are blocking operations on threads whose lane slot reads state=0 -- claimed but never completed -- and
			 * this branch used to note the miss and return, so that thread never attempted an attach again for the
			 * rest of its life. The guards that prevent a second attach (CASE-A, the process-directory arbitration)
			 * live inside gr_attach_lane and gr_adopt_process_lane, so asking them here is safe: they either hand
			 * back the incarnation that already exists or create the one that never was. */
			{
				gr_lane_t* recovered = gr_adopt_process_lane(tid);
				if (!recovered) {
					recovered = gr_attach_lane(tid);
				}
				if (recovered && recovered->state == 1) {
					__simple_fprintf(2, "[dring-lane-recovered] tid=%d was_state=%d\n", tid, (int)L->state);
					return recovered;
				}
			}
			gr_urs_note(callnum, name, gr_urs_reason_for_miss(tid), L);
			return 0;
		}
		return L;
	}
	// perf#30: before attaching a SECOND lane for this thread, ask the process-global directory. This
	// is the arbitration point that makes "one host tid -> one lane incarnation" true; losing the CAS
	// means another image is creating it, so we must not attach.
	gr_lane_t* adopted = gr_adopt_process_lane(tid);
	if (adopted) {
		return adopted;
	}
	gr_lane_t* attached = gr_attach_lane(tid);
	if (!attached) {
		// A REFUSAL TO ATTACH IS AN INVITATION TO ADOPT, NOT A FAILURE. Two of gr_attach_lane's exits are
		// deliberate refusals -- a process-global incarnation already exists for this tid (CASE-A), or another
		// image won the arbitration -- and both mean "use the lane that is already there". MEASURED: the
		// remaining stress_mixed failure was [dring-uds-reason ... callnum=35 name=gr_port_trap reason=
		// ATTACH_FAILED lane_slot=0 lane_gen=0 lane_state=0 lane_active=0] followed by the guest's abort: the
		// refuser returned NULL and this caller reported a failure without ever looking again, even though the
		// incarnation it was told to adopt was right there.
		attached = gr_adopt_process_lane(tid);
	}
	if (!attached) {
		gr_urs_note(callnum, name, GR_URS_ATTACH_FAILED, 0);
	}
	// The hatch is consulted only AFTER this image has already attached several lanes: its first call
	// scans the environment, and doing that during the earliest attach broke boot (measured), the same
	// way an in-band instrumented send hook did.
	return attached;
}

// Back-compat entry point: callers without a callnum identity (attach probes) pass 0.
static gr_lane_t* gr_lane_for_this_thread(void) {
	return gr_lane_for_this_thread_named(0u, "?");
}

// Back-compat bool wrapper (the duplex selftest entry points still call this to gate themselves). True iff
// THIS thread has (or just acquired) a usable lane. The lane itself is resolved again by the caller via
// gr_lane_for_this_thread; this is only the "do I have a ring?" predicate.
bool __dserver_ring_try_attach(void) {
	return gr_lane_for_this_thread() != 0;
}

// perf #18 D17 (dar-1il.12): dump this process's per-thread lane stats to stderr, ONCE, on the clean exit
// path (called from sys_exit, mirroring __darling_rpc_sleep_dump). Default OFF: strict no-op unless the
// env DARLING_GUEST_LANE_STATS=1 is set (scanned libc-free from /proc/self/environ, same discipline as the
// other recon hatches -- set per-command on a warm server, NOT at boot, so daemons don't all dump). This
// surfaces the GUEST-side lane stats the server can't see directly: lanes_exhausted (>0 == reason C),
// lanes_reclaimed (slot reuse), and a live snapshot of how many lanes this process actually holds.
// perf#26 RING-MACH-MSG guest-side transaction counters and the named fallback reasons. The lane-stats
// dump (DARLING_GUEST_LANE_STATS=1) prints them, so the PRODUCT run shows the transaction-scoped
// picture without a second harness.
static uint64_t gr_machmsg_published = 0;
static uint64_t gr_machmsg_final_replies = 0;
static uint64_t gr_machmsg_committed_unknown = 0;
static uint64_t gr_machmsg_fallback_interrupt = 0;
static uint64_t gr_machmsg_fallback_no_lane = 0;
static uint64_t gr_machmsg_fallback_shape = 0;
static uint64_t gr_machmsg_fallback_declined = 0;
static uint64_t gr_duplex_tid_mismatch = 0;   // perf#27 #7: S2C executed on a thread that did not publish
static uint64_t gr_mmap_ok = 0;        // perf#27 #8: anonymous-mmap S2C upcalls that succeeded
static uint64_t gr_mmap_high_addr = 0; // perf#27 #8: of those, how many came back ABOVE 4 GiB (upper32 != 0)
static uint64_t gr_mmap_failures = 0;  // perf#27 #9: anonymous-mmap S2C upcalls that failed (status -1 + errno)
static uint64_t gr_spin_hits = 0;    // perf#27 #2: replies caught inside the spin (no syscall at all)
static uint64_t gr_spin_parks = 0;   // perf#27 #2: waits that fell through to the futex slow path
static uint64_t gr_futex_waits = 0;  // perf#27 #2: FUTEX_WAIT syscalls issued
static uint64_t gr_futex_eagain = 0; // perf#27 #2: FUTEX_WAIT that returned EAGAIN (value already moved)
static uint64_t gr_uds_machmsg_fallbacks = 0; // perf#27 #5: mach_msg ops that rode UDS for the test process

static int g_lane_stats_dumped = 0;
void __dserver_ring_lane_stats_dump(void) {
	if (g_lane_stats_dumped) {
		return;
	}
	g_lane_stats_dumped = 1;
	// env gate (libc-free scan of /proc/self/environ for DARLING_GUEST_LANE_STATS=1).
	long efd = LINUX_SYSCALL(__NR_open, "/proc/self/environ", 0 /*O_RDONLY*/, 0);
	if (efd < 0) {
		return;
	}
	static const char key[] = "DARLING_GUEST_LANE_STATS=1";
	const __SIZE_TYPE__ keylen = sizeof(key) - 1;
	char buf[4096];
	int found = 0;
	for (;;) {
		long n = LINUX_SYSCALL(__NR_read, efd, buf, sizeof(buf));
		if (n <= 0) break;
		__SIZE_TYPE__ start = 0;
		for (__SIZE_TYPE__ i = 0; i < (__SIZE_TYPE__)n; ++i) {
			if (buf[i] == '\0') {
				if (i - start == keylen) {
					int eq = 1;
					for (__SIZE_TYPE__ k = 0; k < keylen; ++k) {
						if (buf[start + k] != key[k]) { eq = 0; break; }
					}
					if (eq) found = 1;
				}
				start = i + 1;
			}
		}
		if (found || (__SIZE_TYPE__)n < (long)sizeof(buf)) break;
	}
	LINUX_SYSCALL1(__NR_close, efd);
	if (!found) {
		return; // env not set -> strict no-op (every normal process)
	}
	// count lanes this process currently holds (active==1) for max-lanes context.
	unsigned held = 0;
	uint32_t pages = 0;
	for (gr_lane_page_t* page = g_lane_pages; page; page = page->next) {
		++pages;
		for (uint32_t i = 0; i < GR_LANE_PAGE_SIZE; ++i) {
			if (__atomic_load_n(&page->lanes[i].active, __ATOMIC_RELAXED) == 1) ++held;
		}
	}
	__simple_fprintf(2, "[dring-lane-stats] pid=%d acquired=%llu released=%llu exhausted=%llu reclaimed=%llu "
	                    "held_now=%u pages=%u capacity=%u max=%u machmsg_ring=%llu machmsg_uds=%llu "
	                    "tid_mismatch=%llu spin_hits=%llu spin_parks=%llu futex_waits=%llu futex_eagain=%llu "
	                    "mmap_ok=%llu mmap_high=%llu mmap_fail=%llu dup_drop=%llu dup_uid=%llu dup_replay=%llu "
	                    "doorbell_fd=%d doorbell_writes=%llu "
	                    "dw_sleeping=%llu dw_active=%llu dw_armed=%llu dw_retired=%llu dw_active_seen=%llu proc_published=%llu proc_adopted=%llu proc_attach_arb=%llu proc_attach_yield=%llu nonfd_uds_violation=%llu "
	                    "courier_attempts=%llu courier_no_address=%llu courier_connect_fail=%llu courier_sent=%llu courier_send_fail=%llu "
	                    "attach_no_page=%llu attach_ready_fail=%llu\n",
		(int)LINUX_SYSCALL(__NR_getpid),
		(unsigned long long)g_stat_lanes_acquired,
		(unsigned long long)g_stat_lanes_released,
		(unsigned long long)g_stat_lanes_exhausted,
		(unsigned long long)g_stat_lanes_reclaimed,
		held, pages, pages * GR_LANE_PAGE_SIZE, (unsigned)(GR_LANE_MAX_PAGES * GR_LANE_PAGE_SIZE),
		(unsigned long long)gr_machmsg_published, (unsigned long long)gr_uds_machmsg_fallbacks,
		(unsigned long long)gr_duplex_tid_mismatch,
		(unsigned long long)gr_spin_hits, (unsigned long long)gr_spin_parks,
		(unsigned long long)gr_futex_waits, (unsigned long long)gr_futex_eagain,
		(unsigned long long)gr_mmap_ok, (unsigned long long)gr_mmap_high_addr,
		(unsigned long long)gr_mmap_failures,
		(unsigned long long)gr_duplex_dropped_replies, (unsigned long long)gr_duplex_uid_mutated,
		(unsigned long long)gr_duplex_dup_replies,
		// perf#28 (ONE doorbell): the canonical process doorbell fd (a -1 query, no side effect) and
		// the number of writes the whole process made on it. doorbell_fd must be IDENTICAL in both
		// guest images: that is the multi-image singleton proof, visible in the product.
		(int)__dserver_ring_doorbell(-1),
		(unsigned long long)__atomic_load_n(&g_stat_doorbell_writes, __ATOMIC_RELAXED),
		(unsigned long long)__atomic_load_n(&g_stat_dw_state[0], __ATOMIC_RELAXED),
		(unsigned long long)__atomic_load_n(&g_stat_dw_state[1], __ATOMIC_RELAXED),
		(unsigned long long)__atomic_load_n(&g_stat_dw_state[2], __ATOMIC_RELAXED),
		(unsigned long long)__atomic_load_n(&g_stat_dw_state[3], __ATOMIC_RELAXED),
		(unsigned long long)__atomic_load_n(&g_stat_dw_active_seen, __ATOMIC_RELAXED),
		// perf#30: the process-global lane directory, as seen by THIS image. adopted>0 with
		// published==0 proves cross-image adoption (this image serves Ring requests on an incarnation
		// it did not create); attach_yielded>0 proves the arbitration refused a duplicate attach.
		(unsigned long long)__atomic_load_n(&g_stat_proc_published, __ATOMIC_RELAXED),
		(unsigned long long)__atomic_load_n(&g_stat_proc_adopted, __ATOMIC_RELAXED),
		(unsigned long long)__atomic_load_n(&g_stat_proc_attach_arbitrated, __ATOMIC_RELAXED),
		(unsigned long long)__atomic_load_n(&g_stat_proc_attach_yielded, __ATOMIC_RELAXED),
		(unsigned long long)__atomic_load_n(&g_stat_nonfd_uds_violation, __ATOMIC_RELAXED),
		(unsigned long long)gr_fd_courier_send_attempts,
		(unsigned long long)gr_fd_courier_no_address,
		(unsigned long long)gr_fd_courier_connect_failures,
		(unsigned long long)gr_fd_courier_sends_ok,
		(unsigned long long)gr_fd_courier_send_failures,
		(unsigned long long)gr_attach_no_page,
		(unsigned long long)gr_attach_ready_fail);

	// perf#29: WHY this process's calls chose UDS, as a histogram (the census that matters). Reasons are
	// computed at the guest's own decision point, never inferred from server state observed later.
	for (unsigned r = 0; r < GR_URS_COUNT; ++r) {
		uint64_t c = __atomic_load_n(&gr_urs_counts[r], __ATOMIC_RELAXED);
		if (c != 0) {
			__simple_fprintf(2, "[dring-uds-reason-hist] pid=%d reason=%s count=%llu\n",
			                 (int)LINUX_SYSCALL(__NR_getpid), gr_urs_name((int)r), (unsigned long long)c);
		}
	}
}

// Wake the server -- CONDITIONALLY (perf #18 P4). We just published a request on lane L. The server only
// needs a doorbell if it is not actively polling its rings; while it spins (ACTIVE_POLLING) it
// will see our new c2s tail on its own, so the eventfd write is pure overhead and we skip it.
// dserver_ring_guest_should_doorbell() reads the server-published state with acquire ordering;
// a stale read at worst costs one redundant write, never a missed wake (an old/0 value ==
// SLEEPING_EPOLL == "doorbell", the safe direction). Each lane has its OWN wake fd (per-thread).
static void gr_wake_server(gr_lane_t* L) {
	uint32_t st = __atomic_load_n(&gr_cb(L)->server_state, __ATOMIC_ACQUIRE);
	if (st == DSERVER_RING_SRV_ACTIVE_POLLING) {
		__atomic_fetch_add(&g_stat_dw_active_seen, 1u, __ATOMIC_RELAXED);
		return; // server is draining rings; it'll pick up our request without a syscall
	}
	if (st < 4) __atomic_fetch_add(&g_stat_dw_state[st], 1u, __ATOMIC_RELAXED);
	uint64_t one = 1;
	LINUX_SYSCALL(__NR_write, L->wake_fd, &one, sizeof(one));
	// perf#28: every lane writes the SAME process doorbell now, so this counts the process's total
	// doorbell syscalls. The hot-path gate is that it stays 0 while the server is ACTIVE_POLLING.
	__atomic_fetch_add(&g_stat_doorbell_writes, 1u, __ATOMIC_RELAXED);
}

// Wait for a reply on s2c after the request has been published. Once published, the server may
// already have performed the operation, so give-up is a committed-or-unknown state, not a UDS retry
// signal. Only pre-publish failures may fall back to UDS.
//
// perf #18 P4: the hot path is the spin -- if the server replies within DARLING_GUEST_RECVSPIN
// iterations we return without touching the kernel AND without ever advertising ourselves as a
// waiter, so the server's conditional FUTEX_WAKE is a no-op too (zero syscalls round-trip). Only
// when we fall through to the slow path do we set s2c_waiters before parking, so the server knows
// to wake us; we clear it on the way out. The bit is set BEFORE the pre-sleep ring recheck, so the
// server can never publish-then-skip-wake while we're committed to sleeping (it reads the bit
// after publishing; if it sees 0 we hadn't slept yet and are still spinning the recheck).
static gr_wait_result_t gr_wait_reply(gr_lane_t* L) {
	dserver_ring_t* s2c = gr_s2c(L);
	for (int i = 0; i < (DARLING_GUEST_RECVSPIN); ++i) {
		dserver_ring_slot_t* rep = dserver_ring_consumer_begin(s2c, GR_SLOT_SIZE, GR_SLOT_COUNT);
		if (rep) return (gr_wait_result_t){ GR_WAIT_COMPLETED, rep };
		gr_relax();
	}
	// Slow path: advertise ourselves as a parked waiter so the server's conditional FUTEX_WAKE
	// fires, then sleep on the futex until it bumps the word. Re-check after each wake.
	uint32_t* word = &gr_cb(L)->s2c_futex;
	uint32_t* waiters = &gr_cb(L)->s2c_waiters;
	__atomic_store_n(waiters, 1u, __ATOMIC_RELEASE);
	dserver_ring_slot_t* result = 0;
	for (int guard = 0; guard < 100000; ++guard) {
		uint32_t observed = __atomic_load_n(word, __ATOMIC_ACQUIRE);
		dserver_ring_slot_t* rep = dserver_ring_consumer_begin(s2c, GR_SLOT_SIZE, GR_SLOT_COUNT);
		if (rep) { result = rep; break; }
		// FUTEX_WAIT(word, observed): sleeps only if *word still == observed (no lost wake).
		long r = LINUX_SYSCALL(__NR_futex, word, GR_FUTEX_WAIT, observed, 0, 0, 0);
		(void)r; // EAGAIN (value changed) / EINTR -> just re-loop and re-check the ring
	}
	// No longer parked -- the server may stop waking us. (Release so the clear is visible before
	// any subsequent request we publish.)
	__atomic_store_n(waiters, 0u, __ATOMIC_RELEASE);
	if (result) {
		return (gr_wait_result_t){ GR_WAIT_COMPLETED, result };
	}
	return (gr_wait_result_t){ GR_WAIT_COMMITTED_UNKNOWN, 0 };
}

// Shared fast path for the no-arg, single-uint32-port-reply traps (task_self_trap,
// mach_reply_port). Publishes an empty-body request for `callnum`, wakes the server, waits for
// the reply, and validates seq+callnum+length before copying out the port. Returns 0 + writes
// *out_port on success; returns negative only on a pre-publish miss where UDS fallback is safe.
// After publish, timeout/bad-reply is committed-unknown and returns KERN_FAILURE without retrying.
// The reply convention ({reply_hdr.code, uint32 port}) is identical for both calls, so one helper covers them.
static int gr_port_trap(uint32_t callnum, uint32_t* out_port) {
	gr_lane_t* L = gr_lane_for_this_thread_named((uint32_t)callnum, "gr_port_trap");
	if (!L) {
		return -1; // no ring for this thread -> UDS fallback (reason counted at the lookup)
	}

#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t0 = gr_rdtsc();
#endif
	dserver_ring_t* c2s = gr_c2s(L);
	dserver_ring_slot_t* req = dserver_ring_producer_begin(c2s, GR_SLOT_SIZE, GR_SLOT_COUNT);
	if (!req) {
		// MEASURED (round 49l): this is a BACKPRESSURE condition, and falling back to UDS here is how a
		// ring-eligible call with a live lane still reached the datagram. It is counted now, because the
		// reason histogram had no name for it and the census could not attribute those instances.
		gr_urs_note(callnum, "gr_port_trap", GR_URS_RING_FULL, L);
		return -1; // ring full -> UDS fallback
	}
	uint32_t seq = gr_next_seq(L);
	req->callnum = callnum;
	req->seq = seq;
	req->length = 0;
	req->arena_off = 0;
	req->arena_len = 0;
	req->flags = 0;
	dserver_ring_producer_publish(c2s);

	gr_wake_server(L);

#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t1 = gr_rdtsc(); // submit done
#endif
	gr_wait_result_t wait = gr_wait_reply(L);
	if (wait.state == GR_WAIT_COMMITTED_UNKNOWN) {
		return KERN_FAILURE; // published: do NOT retry over UDS
	}
	dserver_ring_slot_t* rep = wait.slot;
#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t2 = gr_rdtsc(); // reply observed (roundtrip done)
#endif

	// Validate the reply belongs to our request before trusting it.
	int rc = KERN_FAILURE; // bad/mismatched reply after publish: fail closed, do NOT UDS-retry
	if (rep->seq == seq && rep->callnum == callnum) {
		uint32_t inlineCap = GR_SLOT_SIZE - (uint32_t)sizeof(dserver_ring_slot_t);
		if (rep->length == sizeof(dserver_ring_reply_hdr_t) + sizeof(uint32_t) && rep->length <= inlineCap) {
			char* payload = (char*)rep + sizeof(dserver_ring_slot_t);
			dserver_ring_reply_hdr_t* rh = (dserver_ring_reply_hdr_t*)payload;
			if (rh->code == 0) {
				uint32_t port;
				memcpy(&port, payload + sizeof(dserver_ring_reply_hdr_t), sizeof(port));
				if (out_port) *out_port = port;
				rc = 0;
			} // any server error after publication remains KERN_FAILURE, never a retry sentinel
		}
	}
	dserver_ring_consumer_advance(gr_s2c(L));
#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t3 = gr_rdtsc(); // observe (validate + copy out) done
	gr_ph_submit  += (_t1 - _t0);
	gr_ph_round   += (_t2 - _t1);
	gr_ph_observe += (_t3 - _t2);
	gr_ph_total   += (_t3 - _t0);
	++gr_ph_n;
	gr_phase_maybe_dump();
#endif
	return rc;
}

int __dserver_ring_task_self_trap(uint32_t* out_port) {
	return gr_port_trap((uint32_t)dserver_callnum_task_self_trap, out_port);
}

// perf #18 D10 (dar-1il.5): thread_self_trap + host_self_trap -- the remaining pure-mint self-trap
// family. Byte-identical transport shape to task_self_trap (empty request, single uint32 port reply),
// so they reuse gr_port_trap verbatim. Each routes over the ring iff it is in DSERVER_RING_C2S_OPCODES
// (the shared allowlist the server keys off too); on any transport miss the caller UDS-falls-back.
int __dserver_ring_thread_self_trap(uint32_t* out_port) {
	return gr_port_trap((uint32_t)dserver_callnum_thread_self_trap, out_port);
}

int __dserver_ring_host_self_trap(uint32_t* out_port) {
	return gr_port_trap((uint32_t)dserver_callnum_host_self_trap, out_port);
}

int __dserver_ring_mach_reply_port(uint32_t* out_port) {
	return gr_port_trap((uint32_t)dserver_callnum_mach_reply_port, out_port);
}

// perf #18 P5 (dar-1il): generic fast path for a small-inline-body op whose reply is HEADER-ONLY
// (the meaningful result is the reply hdr's `code` = a kern_return_t, no port/body). Unlike
// gr_port_trap (empty request, port reply), this publishes `bodylen` bytes of request body inline
// in the slot and copies NOTHING out of the reply but the code. Used by mach_port_mod_refs.
//
// Contract mirrors gr_port_trap's transport semantics but splits the result from the
// fall-back signal: returns 0 on a VALID ring round-trip and writes *out_code (the kern_return_t,
// which the caller must use verbatim -- it may be KERN_INVALID_NAME etc.); returns -1 on any
// transport miss BEFORE publish (no ring / ring full), in which case the caller MUST UDS-fall-back.
// Once published, timeout/bad-reply is committed-unknown: return 0 with *out_code=KERN_FAILURE so
// the caller observes failure without re-running the operation over UDS.
// This separation matters because a kern_return_t can be any value and must never be confused
// with the "-1 = retry on UDS" sentinel (gr_port_trap could conflate them only because that op's
// code is always 0).
static int gr_body_trap(uint32_t callnum, const void* body, uint32_t bodylen, int* out_code) {
	gr_lane_t* L = gr_lane_for_this_thread_named((uint32_t)callnum, "gr_body_trap");
	if (!L) {
		return -1; // no ring for this thread -> UDS fallback (reason counted at the lookup)
	}

	uint32_t inlineCap = GR_SLOT_SIZE - (uint32_t)sizeof(dserver_ring_slot_t);
	if (bodylen > inlineCap) {
		return -1; // body won't fit the slot -> UDS fallback (shouldn't happen for our small ops)
	}

#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t0 = gr_rdtsc();
#endif
	dserver_ring_t* c2s = gr_c2s(L);
	dserver_ring_slot_t* req = dserver_ring_producer_begin(c2s, GR_SLOT_SIZE, GR_SLOT_COUNT);
	if (!req) {
		return -1; // ring full -> UDS fallback
	}
	uint32_t seq = gr_next_seq(L);
	req->callnum = callnum;
	req->seq = seq;
	req->length = bodylen;
	req->arena_off = 0;
	req->arena_len = 0;
	req->flags = 0;
	if (bodylen > 0) {
		memcpy((char*)req + sizeof(dserver_ring_slot_t), body, bodylen);
	}
	dserver_ring_producer_publish(c2s);

	gr_wake_server(L);

#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t1 = gr_rdtsc(); // submit done
#endif
	gr_wait_result_t wait = gr_wait_reply(L);
	if (wait.state == GR_WAIT_COMMITTED_UNKNOWN) {
		if (out_code) *out_code = KERN_FAILURE;
		return 0; // published: do NOT retry over UDS
	}
	dserver_ring_slot_t* rep = wait.slot;
#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t2 = gr_rdtsc(); // reply observed (roundtrip done)
#endif

	// Validate the reply belongs to our request before trusting it. The reply is header-only:
	// length must be exactly the reply hdr (no body), and code is the kern_return_t.
	int rc = 0;
	if (out_code) *out_code = KERN_FAILURE; // bad/mismatched reply after publish: no UDS retry
	if (rep->seq == seq && rep->callnum == callnum) {
		if (rep->length == sizeof(dserver_ring_reply_hdr_t) && rep->length <= inlineCap) {
			char* payload = (char*)rep + sizeof(dserver_ring_slot_t);
			dserver_ring_reply_hdr_t* rh = (dserver_ring_reply_hdr_t*)payload;
			if (out_code) *out_code = rh->code;
			rc = 0; // valid ring round-trip
		}
	}
	dserver_ring_consumer_advance(gr_s2c(L));
#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t3 = gr_rdtsc(); // observe done
	gr_ph_submit  += (_t1 - _t0);
	gr_ph_round   += (_t2 - _t1);
	gr_ph_observe += (_t3 - _t2);
	gr_ph_total   += (_t3 - _t0);
	++gr_ph_n;
	gr_phase_maybe_dump();
#endif
	return rc;
}

// perf #18 D11 (dar-1il.6): generic fast path for a CLOSED op with a small inline request body AND a
// small inline reply BODY (not just the kern_return code). Unlike gr_body_trap (header-only reply),
// this also copies `replylen` bytes of reply body out of the slot into `reply_out` -- the over-the-wire
// reply body is byte-identical to the UDS dserver_reply_<op>_t struct, because the server publishes
// everything after the reply hdr onto the ring (_publishReplyToRingLocked). These calls use the
// BSD/internal RPC convention: zero on success, negative Linux errno on failure, not kern_return_t.
// The transport result stays separate: -1 permits a pre-publication UDS fallback; 0 means the
// operation was published. An unknown completion returns 0 with *out_code=-LINUX_EIO, so callers
// observe failure rather than consuming an uninitialized reply or repeating the operation.
// Validate the complete generated wire body, including RPC tail padding, but
// copy only the actual fields. For example a bool body occupies one byte while
// its enclosing RPC reply has three bytes of tail padding.
#define GR_REPLY_WIRE_SIZE(op) (sizeof(dserver_rpc_reply_##op##_t) - sizeof(dserver_rpc_replyhdr_t))
static int gr_full_trap(uint32_t callnum, const void* body, uint32_t bodylen,
                        void* reply_out, uint32_t replylen, uint32_t wirelen, int* out_code) {
	gr_lane_t* L = gr_lane_for_this_thread_named((uint32_t)callnum, "gr_full_trap");
	if (!L) {
		return -1; // no ring for this thread -> UDS fallback (reason counted at the lookup)
	}

	uint32_t inlineCap = GR_SLOT_SIZE - (uint32_t)sizeof(dserver_ring_slot_t);
	if (bodylen > inlineCap || replylen > wirelen || (uint32_t)sizeof(dserver_ring_reply_hdr_t) + wirelen > inlineCap) {
		return -1; // body/reply won't fit the slot -> UDS fallback (shouldn't happen for our small ops)
	}

#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t0 = gr_rdtsc();
#endif
	dserver_ring_t* c2s = gr_c2s(L);
	dserver_ring_slot_t* req = dserver_ring_producer_begin(c2s, GR_SLOT_SIZE, GR_SLOT_COUNT);
	if (!req) {
		return -1; // ring full -> UDS fallback
	}
	uint32_t seq = gr_next_seq(L);
	req->callnum = callnum;
	req->seq = seq;
	req->length = bodylen;
	req->arena_off = 0;
	req->arena_len = 0;
	req->flags = 0;
	if (bodylen > 0) {
		memcpy((char*)req + sizeof(dserver_ring_slot_t), body, bodylen);
	}
	dserver_ring_producer_publish(c2s);

	gr_wake_server(L);

#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t1 = gr_rdtsc(); // submit done
#endif
	gr_wait_result_t wait = gr_wait_reply(L);
	if (wait.state == GR_WAIT_COMMITTED_UNKNOWN) {
		if (out_code) *out_code = -LINUX_EIO;
		return 0; // published: do NOT retry over UDS
	}
	dserver_ring_slot_t* rep = wait.slot;
#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t2 = gr_rdtsc(); // reply observed (roundtrip done)
#endif

	// Validate the reply belongs to our request before trusting it. The reply must be EXACTLY
	// {reply_hdr, wirelen-byte body}; padding is part of the wire shape, not the
	// fields copied to the caller. Malformed replies must never trigger replay.
	int rc = 0;
	if (out_code) *out_code = -LINUX_EIO;
	if (rep->seq == seq && rep->callnum == callnum) {
		if (rep->length == (uint32_t)sizeof(dserver_ring_reply_hdr_t) + wirelen && rep->length <= inlineCap) {
			char* payload = (char*)rep + sizeof(dserver_ring_slot_t);
			dserver_ring_reply_hdr_t* rh = (dserver_ring_reply_hdr_t*)payload;
			if (out_code) *out_code = rh->code;
			if (rh->code == 0 && replylen > 0 && reply_out) {
				memcpy(reply_out, payload + sizeof(dserver_ring_reply_hdr_t), replylen);
			}
			rc = 0; // valid ring round-trip
		}
	}
	dserver_ring_consumer_advance(gr_s2c(L));
#ifdef DARLING_RING_PHASE_PROF
	unsigned long _t3 = gr_rdtsc(); // observe done
	gr_ph_submit  += (_t1 - _t0);
	gr_ph_round   += (_t2 - _t1);
	gr_ph_observe += (_t3 - _t2);
	gr_ph_total   += (_t3 - _t0);
	++gr_ph_n;
	gr_phase_maybe_dump();
#endif
	return rc;
}

// The five closed BSD/internal RPC wrappers. A negative transport return permits UDS fallback;
// zero means published, with the operation status in *out_code. Copy reply fields only on
// successful completion, never on committed-unknown or a server error.
// The request/reply layouts match generated dserver_call_<op>_t / dserver_reply_<op>_t structs,
// so the server rebuilds {callhdr, body} and dispatches via the SAME
// generic Call path / dtape primitive as over UDS -- behavior is byte-identical, only the transport
// differs. None is destroy-capable or caller-S2C (canon rules 1-2 hold); they ride the GENERIC fiber
// doWork() path (Tier-1, NOT the no-fiber inline fast path).

int __dserver_ring_uidgid(int32_t new_uid, int32_t new_gid, int32_t* out_old_uid, int32_t* out_old_gid, int* out_code) {
	// dserver_call_uidgid_t { int32_t new_uid; int32_t new_gid; } -> dserver_reply_uidgid_t { int32_t old_uid; int32_t old_gid; }
	struct { int32_t new_uid; int32_t new_gid; } body = { new_uid, new_gid };
	struct { int32_t old_uid; int32_t old_gid; } reply;
	int rc = gr_full_trap((uint32_t)dserver_callnum_uidgid, &body, (uint32_t)sizeof(body),
	                      &reply, (uint32_t)sizeof(reply), GR_REPLY_WIRE_SIZE(uidgid), out_code);
	if (rc == 0 && out_code && *out_code == 0) {
		if (out_old_uid) *out_old_uid = reply.old_uid;
		if (out_old_gid) *out_old_gid = reply.old_gid;
	}
	return rc;
}

int __dserver_ring_set_thread_handles(uint64_t pthread_handle, uint64_t dispatch_qaddr, int* out_code) {
	// dserver_call_set_thread_handles_t { uint64_t pthread_handle aligned(8); uint64_t dispatch_qaddr aligned(8); } -> header-only reply
	struct {
		uint64_t pthread_handle __attribute__((aligned(8)));
		uint64_t dispatch_qaddr __attribute__((aligned(8)));
	} body = { pthread_handle, dispatch_qaddr };
	return gr_full_trap((uint32_t)dserver_callnum_set_thread_handles, &body, (uint32_t)sizeof(body),
	                    (void*)0, 0, GR_REPLY_WIRE_SIZE(set_thread_handles), out_code);
}

int __dserver_ring_started_suspended(bool* out_suspended, int* out_code) {
	// empty request -> dserver_reply_started_suspended_t { bool suspended; }
	struct { bool suspended; } reply;
	int rc = gr_full_trap((uint32_t)dserver_callnum_started_suspended, (void*)0, 0,
	                      &reply, (uint32_t)sizeof(reply), GR_REPLY_WIRE_SIZE(started_suspended), out_code);
	if (rc == 0 && out_code && *out_code == 0 && out_suspended) *out_suspended = reply.suspended;
	return rc;
}

int __dserver_ring_get_tracer(int32_t* out_tracer, int* out_code) {
	// empty request -> dserver_reply_get_tracer_t { int32_t tracer; }
	struct { int32_t tracer; } reply;
	int rc = gr_full_trap((uint32_t)dserver_callnum_get_tracer, (void*)0, 0,
	                      &reply, (uint32_t)sizeof(reply), GR_REPLY_WIRE_SIZE(get_tracer), out_code);
	if (rc == 0 && out_code && *out_code == 0 && out_tracer) *out_tracer = reply.tracer;
	return rc;
}

int __dserver_ring_task_is_64_bit(int32_t id, bool* out_is_64_bit, int* out_code) {
	// dserver_call_task_is_64_bit_t { int32_t id; } -> dserver_reply_task_is_64_bit_t { bool is_64_bit; }
	struct { int32_t id; } body = { id };
	struct { bool is_64_bit; } reply;
	int rc = gr_full_trap((uint32_t)dserver_callnum_task_is_64_bit, &body, (uint32_t)sizeof(body),
	                      &reply, (uint32_t)sizeof(reply), GR_REPLY_WIRE_SIZE(task_is_64_bit), out_code);
	if (rc == 0 && out_code && *out_code == 0 && out_is_64_bit) *out_is_64_bit = reply.is_64_bit;
	return rc;
}

// perf #18 D13 (dar-1il.8): the two path-op wrappers. Despite the char*/path UDS signature, the path
// bytes do NOT travel in the RPC payload -- `buffer` is the GUEST virtual address into which the server
// writes the path via process_vm_writev (writeMemory) on the guest's /proc/mem, exactly as over UDS.
// So the ring request body is the FIXED 16B { uint64_t buffer; uint64_t buffer_size; } (byte-identical
// to dserver_call_<op>_t) and the reply is the 8B { uint64_t length; } (byte-identical to
// dserver_reply_<op>_t) -- no arena, no variable payload. /proc/mem access is server-side and needs no
// caller-S2C, and the op is a pure read of server config (no destroy), so all 5 canon rules hold. Each
// routes over the ring iff it is in DSERVER_RING_C2S_OPCODES; on any transport miss the caller MUST
// UDS-fall-back. (set_executable_path is NOT migrated: pre-attach mldr-only + NO_REPLY.)

int __dserver_ring_vchroot_path(uint64_t buffer, uint64_t buffer_size, uint64_t* out_length, int* out_code) {
	// dserver_call_vchroot_path_t { uint64_t buffer aligned(8); uint64_t buffer_size aligned(8); }
	//   -> dserver_reply_vchroot_path_t { uint64_t length aligned(8); }
	struct {
		uint64_t buffer __attribute__((aligned(8)));
		uint64_t buffer_size __attribute__((aligned(8)));
	} body = { buffer, buffer_size };
	struct { uint64_t length __attribute__((aligned(8))); } reply;
	int rc = gr_full_trap((uint32_t)dserver_callnum_vchroot_path, &body, (uint32_t)sizeof(body),
	                      &reply, (uint32_t)sizeof(reply), GR_REPLY_WIRE_SIZE(vchroot_path), out_code);
	if (rc == 0 && out_code && *out_code == 0 && out_length) *out_length = reply.length;
	return rc;
}

int __dserver_ring_mldr_path(uint64_t buffer, uint64_t buffer_size, uint64_t* out_length, int* out_code) {
	// dserver_call_mldr_path_t { uint64_t buffer aligned(8); uint64_t buffer_size aligned(8); }
	//   -> dserver_reply_mldr_path_t { uint64_t length aligned(8); }
	struct {
		uint64_t buffer __attribute__((aligned(8)));
		uint64_t buffer_size __attribute__((aligned(8)));
	} body = { buffer, buffer_size };
	struct { uint64_t length __attribute__((aligned(8))); } reply;
	int rc = gr_full_trap((uint32_t)dserver_callnum_mldr_path, &body, (uint32_t)sizeof(body),
	                      &reply, (uint32_t)sizeof(reply), GR_REPLY_WIRE_SIZE(mldr_path), out_code);
	if (rc == 0 && out_code && *out_code == 0 && out_length) *out_length = reply.length;
	return rc;
}

// perf #18 P5-bulk audit (dar-1il.2): mach_port_mod_refs has NO ring helper -- it was removed from
// DSERVER_RING_C2S_OPCODES because it is destroy-capable (negative-delta last-ref destroy -> vm
// munmap S2C upcall to a caller parked on the ring -> deadlock; same class as mach_port_deallocate).
// It routes UDS-only (see mach_traps.c _kernelrpc_mach_port_mod_refs_trap_impl).

// perf #18 P5-bulk (dar-1il.1): two more port/right bookkeeping ops ride the ring. Both are a
// Tier-1 shape: a small inline request body + a HEADER-ONLY reply (the result is the kern_return_t;
// allocate also copies the name out via the existing guest-memory write the generic Call path does).
// Each body layout below is byte-identical to the generated dserver_call_<name>_t, so the server
// rebuilds {callhdr, body} and dispatches via callFromMessage -> the identical dtape primitive as
// over UDS. They ride the GENERIC fiber doWork() path on the server (NOT the no-fiber inline path,
// which off-fiber-corrupts the thread stack; see call.cpp ringFastPathEligible). Each routes over
// the ring iff it is in DSERVER_RING_C2S_OPCODES (rpc-supplement.h), the single shared allowlist the
// server keys off too. (allocate/insert_right are kept; mach_port_deallocate's helper below is dead
// -- deallocate also reverted to UDS-only in dar-1il.1 -- and left only to minimize that diff.)

// perf#30 (directive sections 3, 15): the guest image's GENERIC management-plane publisher. ONE implementation
// for every migrated call, so no transport grows its own copy of the mailbox protocol. Modeled on the loader's
// __mldr_process_control_request and on the slot-ownership rules this work measured: claim the slot with a CAS,
// publish, wake the process doorbell, wait for completion, SNAPSHOT the answer fields, then RELEASE (sections
// 67/70/71 -- releasing before the snapshot lets the next publisher overwrite them).
// Returns the server's status for a COMPLETED request, or -1 meaning "not published / not completed" so the
// caller may fall back without duplicating an operation the server may already have committed.
// perf#30 (directive sections 19-21): the URGENT, SIGNAL-SAFE publisher. interrupt_enter/interrupt_exit run in
// signal context: they may not park, may not wait on a futex, and may not touch lazily-initialised state -- and
// they may not use the thread's own lane either, because the handler can interrupt the thread that holds it.
// So: a bounded pool of slots in the shared page, claimed with a CAS, published with a release store, and
// woken with a raw doorbell write. The caller returns immediately; the reply is informational (the effect it
// needs is the server's flush of a saved reply onto the thread's lane, serviced when the handler returns).
// Reaping a DONE slot back to IDLE is done by the next publisher with a CAS, so no waiter is needed.
// perf#30 (doc section 87): the publish-and-POLL variant, for a signal-context call that needs a completion
// (sigprocess). Polling shared memory is signal-safe -- atomics, no park, no allocation, no lazy state -- and the
// urgent pool is independent of both the lane and the management slot, so a handler that interrupts a thread
// holding either one still makes progress. Bounded: the caller gives up and falls back rather than spinning
// forever.
int __dserver_plane_urgent_publish_wait(uint32_t op, const uint64_t* words, uint32_t wordCount, int32_t* outStatus, uint64_t* outExtra) {
	struct dserver_process_control* page =
		(struct dserver_process_control*)__dserver_process_control_page();
	/* ENTRY TRACE (dar-4cp9). MEASURED contradiction this exists to settle: a boot failure printed
	 * [console-path] plane-status=-1 with this function's refusal macro printing nothing at all, which cannot
	 * both be true if the -1 came from one of the five instrumented exits. A numbered mark at entry and at each
	 * exit tells which one ran, and whether the function was entered at all. */
	if (__plane_steps_enabled()) { __simple_fprintf(2, "[plane-enter] op=%u page=%p\n", (unsigned)op, (void*)page); }
	if (page == NULL) { return -1; }
	if (__atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE) == 0) { return -1; }
	static uint32_t urgentSeqW = 0;
	int chosen = -1;
	for (unsigned u = 0; u < DSERVER_PROCESS_CONTROL_URGENT_SLOTS; ++u) {
		uint32_t doneExpect = DSERVER_PROCESS_CONTROL_URGENT_DONE;
		(void)__atomic_compare_exchange_n(&page->urgent_state[u], &doneExpect,
			DSERVER_PROCESS_CONTROL_URGENT_IDLE, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
		uint32_t expect = DSERVER_PROCESS_CONTROL_URGENT_IDLE;
		if (__atomic_compare_exchange_n(&page->urgent_state[u], &expect,
		        DSERVER_PROCESS_CONTROL_URGENT_PENDING, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
			chosen = (int)u;
			break;
		}
	}
	if (chosen < 0) { return -1; }
	unsigned u = (unsigned)chosen;
	uint32_t mine = __atomic_add_fetch(&urgentSeqW, 1, __ATOMIC_RELAXED);
	page->urgent_op[u] = op;
	page->urgent_seq[u] = mine;
	// The same identity, published the same way: raw syscall, before the release store, because this runs in a
	// signal context where nothing lazily-initialised may be touched.
	page->urgent_tid[u] = (int32_t)LINUX_SYSCALL(__NR_gettid);
	for (uint32_t w = 0; w < wordCount && w < 8u; ++w) {
		page->urgent_payload[u][w] = words[w];
	}
	__atomic_store_n(&page->urgent_state[u], DSERVER_PROCESS_CONTROL_URGENT_PENDING, __ATOMIC_RELEASE);
	if (__plane_steps_enabled()) { __simple_fprintf(2, "[plane-claim-mark] C pre-doorbell op=%u\n", (unsigned)op); }
	int db = __dserver_ring_doorbell(-1);
	if (__plane_steps_enabled()) { __simple_fprintf(2, "[plane-claim-mark] D post-doorbell op=%u db=%d\n", (unsigned)op, db); }
	if (db >= 0) {
		uint64_t one = 1;
		(void)LINUX_SYSCALL3(__NR_write, db, &one, sizeof(one));
	}
	// BOUNDED poll of our own slot: no futex, no park -- signal-safe.
	for (long spins = 0; spins < 20000000L; ++spins) {
		if (__atomic_load_n(&page->urgent_state[u], __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_URGENT_DONE
		    && page->urgent_seq[u] == mine) {
			int32_t st = page->urgent_reply_status[u];
			uint64_t extra = page->urgent_payload[u][6];
			__atomic_store_n(&page->urgent_state[u], DSERVER_PROCESS_CONTROL_URGENT_IDLE, __ATOMIC_RELEASE);
			if (outStatus) { *outStatus = st; }
			if (outExtra) { *outExtra = extra; }
			return (int)st;
		}
		if ((spins & 0x3ff) == 0) {
			(void)LINUX_SYSCALL(__NR_sched_yield);
		}
	}
	// perf#30: name WHY the bounded poll gave up, bounded and gated (this runs in a signal handler, so the
	// print must be one line and only once).
	{
		static int reported = 0;
		if (!reported) {
			reported = 1;
			__simple_fprintf(2, "[urgent-wait-TIMEOUT] op=%u slot=%u mine=%u state=%u seq=%u\n",
				(unsigned)op, (unsigned)u, (unsigned)mine,
				(unsigned)__atomic_load_n(&page->urgent_state[u], __ATOMIC_ACQUIRE),
				(unsigned)page->urgent_seq[u]);
		}
	}
	// perf#30 (directive section 3, doc section 206): PUBLISHED-BUT-NOT-COMPLETED is its OWN answer. MEASURED: the
	// hard oracle's last denial came from this exact path -- the bounded poll gave up, returned -1, and the caller
	// read -1 as "nothing was published" and took the datagram, creating the per-thread socket this work removes.
	// The request HAD been published (the slot is PENDING and the doorbell was rung), so retrying it on another
	// transport would run the operation twice. The slot is released here because this caller abandons the wait; a
	// late completion carries our sequence and is ignored by the seq check, exactly as a late reply is elsewhere.
	__atomic_store_n(&page->urgent_state[u], DSERVER_PROCESS_CONTROL_URGENT_IDLE, __ATOMIC_RELEASE);
	return -2;   // published, completion not observed: NEVER a datagram retry
}

int __dserver_plane_urgent_publish(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3) {
	struct dserver_process_control* page =
		(struct dserver_process_control*)__dserver_process_control_page();
	if (page == NULL) {
		return -1;
	}
	if (__atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE) == 0) {
		return -1;   // no transport: the caller keeps its datagram path
	}
	static uint32_t urgentSeq = 0;
	for (unsigned u = 0; u < DSERVER_PROCESS_CONTROL_URGENT_SLOTS; ++u) {
		// reap a completed slot (its reply is informational) -- a CAS, so two publishers cannot both take it
		uint32_t doneExpect = DSERVER_PROCESS_CONTROL_URGENT_DONE;
		(void)__atomic_compare_exchange_n(&page->urgent_state[u], &doneExpect,
			DSERVER_PROCESS_CONTROL_URGENT_IDLE, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE);
		uint32_t expect = DSERVER_PROCESS_CONTROL_URGENT_IDLE;
		if (__atomic_compare_exchange_n(&page->urgent_state[u], &expect,
		        DSERVER_PROCESS_CONTROL_URGENT_PENDING, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
			page->urgent_op[u] = op;
			page->urgent_seq[u] = __atomic_add_fetch(&urgentSeq, 1, __ATOMIC_RELAXED);
			page->urgent_payload[u][0] = p0;
			page->urgent_payload[u][1] = p1;
			page->urgent_payload[u][2] = p2;
			page->urgent_payload[u][3] = p3;
			__atomic_store_n(&page->urgent_state[u], DSERVER_PROCESS_CONTROL_URGENT_PENDING, __ATOMIC_RELEASE);
			// raw doorbell write: the process doorbell, never a courier byte (directive section 8)
			int db = __dserver_ring_doorbell(-1);
			if (db >= 0) {
				uint64_t one = 1;
				(void)LINUX_SYSCALL3(__NR_write, db, &one, sizeof(one));
			}
			return 0;
		}
	}
	return -1;   // pool full: the caller falls back
}

static const char* __dserver_ring_env_cache = NULL;
/* A BUSY PLANE SLOT IS A TRANSIENT, SO THE RETRY LIVES IN ONE PLACE (dar-4cp9). MEASURED: thirteen call sites
 * in this tree publish to the plane and fall back to the datagram when it answers -1, and each fallback is a
 * correctness cliff under the hard socket gate -- the datagram is denied, the call never runs, and on a boot
 * path the process aborts. The move_member denial that killed launchd was exactly one of those, and the local
 * retry written for it made 30 boots produce zero denials. Rather than repeat that loop thirteen times, the
 * bounded retry now wraps the entry point itself, so every caller inherits it and no caller's contract changes:
 * -1 still means "not published / not completed", just only after the attempts are exhausted. */
int __dserver_plane_request_once(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3,
	uint64_t* outReply0, uint64_t* outReply1);
int __dserver_plane_request_ex(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3,
	uint64_t* outReply0, uint64_t* outReply1) {
	int st = -1;
	for (int attempt = 0; attempt < 9; ++attempt) {
		st = __dserver_plane_request_once(op, p0, p1, p2, p3, outReply0, outReply1);
		if (st != -1) {
			return st;
		}
		if (attempt == 0) {
			/* Named once per process: a retry that happens silently would make a systematic slot problem look
			 * like a slightly slower boot. */
			static int refused_once = 0;
			if (!refused_once) {
				refused_once = 1;
				__simple_fprintf(2, "[plane-retry] first -1 op=%u tid=%d\n",
					(unsigned)op, (int)LINUX_SYSCALL(__NR_gettid));
			}
		}
		LINUX_SYSCALL(__NR_sched_yield);
	}
	/* AN EXHAUSTED RETRY MUST BE NAMEABLE (measured while chasing the mod_refs abort). The first -1 prints
	 * `[plane-retry]`, which says only that a retry HAPPENED; the exit that actually decides the caller's fate
	 * printed nothing, so "the plane refused once and then answered" and "the plane never answered at all" left
	 * the same log -- and the caller's next act is the datagram fallback the hard socket gate denies, i.e. the
	 * abort. Bounded per process, raw, literal and allocation-free, like the rest of this file's instruments. */
	{
		static int exhausted_reported = 0;
		if (exhausted_reported < 8) {
			++exhausted_reported;
			__simple_fprintf(2, "[plane-exhausted] n=%d op=%u tid=%d attempts=9 status=%d\n",
				exhausted_reported, (unsigned)op, (int)LINUX_SYSCALL(__NR_gettid), (int)st);
		}
	}
	return st;
}

int __dserver_plane_request_once(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3,
	uint64_t* outReply0, uint64_t* outReply1) {
	struct dserver_process_control* page =
		(struct dserver_process_control*)__dserver_process_control_page();
	// perf#30 (doc section 232): NAME THE REFUSAL. MEASURED: this function returned -1 with no diagnostic at all, so a
	// caller that fell back to the datagram (`rpc-socket-DENIED ... call=kqchan_mach_port_open`, tid == pid, after the
	// loader had already used the plane successfully for ops 1-8) gave no way to tell "no page" from "transport not
	// ready" from "version mismatch" from "no slot for 2 s". The fallback is a silent correctness cliff under the hard
	// socket hatch (the datagram is denied, so the call never runs), so the reason must be read, not deduced. Bounded
	// and raw: one line per reason per process, written with the ordinary write syscall so it cannot allocate or block.
	/* A REFUSAL MUST BE NAMEABLE MORE THAN ONCE (dar-4cp9). MEASURED defect in this instrument: the guard printed
	 * the FIRST reason per process only, so a later, DIFFERENT reason in the same process was silent -- and the one
	 * observed boot failure (console_open denied, then launchd sig6) had no refuse line at all in its log, which left
	 * 'no slot', 'not ready' and the silent exits indistinguishable exactly where the run was already lost. Bounded
	 * per-process repetition keeps the output finite without hiding the reason that matters; the sequence number says
	 * which refusal in the process's life this is. */
	#define __MLDR_PLANE_REFUSE(why, a, b) do { \
		static int __r_count = 0; \
		if (__r_count < 8) { \
			++__r_count; \
			__simple_fprintf(2, "[plane-refuse] n=%d why=" why " op=%llu a=%llu b=%llu tid=%d\n", \
				__r_count, \
				(unsigned long long)(op), (unsigned long long)(a), (unsigned long long)(b), \
				(int)LINUX_SYSCALL(__NR_gettid)); \
		} \
	} while (0)
	if (page == NULL) {
		if (__plane_steps_enabled()) { __simple_fprintf(2, "[plane-exit] X1\\n"); }
		__MLDR_PLANE_REFUSE("no-page", 0, 0);
		return -1;
	}
	// perf#30 DIAGNOSIS: the plane is a TRANSPORT in front of the same Call, so any behaviour difference between the
	// plane and the datagram for one op must be attributable to the transport, not inferred. MEASURED need:
	// `basic 2`/`basic 3` die (SIGSEGV) inside this function's claim wait while a datagram run of the same workload with
	// the plane unwired is what the A/B has to compare against. This hatch refuses the plane for the whole process
	// BEFORE anything is published, so the caller's ordinary datagram fallback runs -- the only change is the
	// transport. Read with the libc-free scan: this is reached during bootstrap.
	{
		static int __plane_off = -1;
		if (__plane_off < 0) {
			__plane_off = gr_environ_has("DARLING_GUEST_PLANE_OFF=1", sizeof("DARLING_GUEST_PLANE_OFF=1") - 1);
		}
		if (__plane_off) {
			if (__plane_steps_enabled()) { __simple_fprintf(2, "[plane-exit] X2\\n"); }
		__MLDR_PLANE_REFUSE("off", 0, 0);
			return -1;
		}
	}
	for (int waited_ms = 0; __atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE) == 0; ) {
		if (waited_ms >= 200) {
			if (__plane_steps_enabled()) { __simple_fprintf(2, "[plane-exit] X3\\n"); }
		__MLDR_PLANE_REFUSE("not-ready", __atomic_load_n(&page->transport_ready, __ATOMIC_ACQUIRE),
				__atomic_load_n(&page->version, __ATOMIC_ACQUIRE));
			return -1;
		}
		long ts[2] = {0, 1000000L};
		(void)LINUX_SYSCALL6(__NR_nanosleep, ts, 0, 0, 0, 0, 0);
		++waited_ms;
	}
	// A page whose version we do not recognise is NOT the plane: publishing into it would put words at offsets
	// the server does not read. Treated exactly like "no page": the caller's datagram fallback runs.
	__MLDR_PLANE_STEP("D-version", op);
	if (page->version != DSERVER_PROCESS_CONTROL_VERSION) {
		if (__plane_steps_enabled()) { __simple_fprintf(2, "[plane-exit] X4\\n"); }
		__MLDR_PLANE_REFUSE("version", __atomic_load_n(&page->version, __ATOMIC_ACQUIRE), DSERVER_PROCESS_CONTROL_VERSION);
		return -1;
	}
	// perf#30 (doc section 232): a slot left at DONE by the SERVER's completion store is CLAIMABLE -- exactly the
	// remedy doc section 195 applied to the LOADER's copy of this loop and applied to only one of the two copies.
	// MEASURED here: this loop accepted IDLE only, the completion store leaves `request_state = DONE` (server.cpp
	// stores DONE on every reply, and a guest that fell back to the datagram for that request never releases it),
	// and the slot was then unusable for the life of the process: `[plane-refuse] why=no-slot op=9 a=2 b=0x500000004`
	// -- a = DONE (2) and the holder is op 5 seq 4 -- after which every later request timed out at 2 s, took the
	// datagram, and was DENIED by the hard socket hatch, so the call never ran at all (the stall, exactly). The
	// publisher reads its ANSWER from reply_state, never from this flag, so reusing a completed slot is what
	// "the publisher owns the slot until it has read its answer" already means. IDLE and DONE alternate, as in mldr.
	__MLDR_PLANE_STEP("E-claim", op);
	// MEASURED need: the thread died between E-claim and F-claimed, i.e. inside this loop, and the loop's only
	// page access is this load+CAS on `page`. Printing the OBSERVED value first separates "the page mapping the
	// pointer names is gone (the load itself faults)" from "the mapping is fine and the atomic store faults".
	// Bounded: the first three iterations and every 500th after that.
	static int __claim_iters = 0;
	int slot = 0;
	for (int t = 0; t < 2000 && !slot; ++t) {
		uint32_t expect = (t % 2 == 0) ? DSERVER_PROCESS_CONTROL_IDLE : DSERVER_PROCESS_CONTROL_DONE;
		if (__plane_steps_enabled() && (t < 3 || (t % 500) == 0) && __claim_iters < 40) {
			++__claim_iters;
			uint32_t obs = __atomic_load_n(&page->request_state, __ATOMIC_ACQUIRE);
			__simple_fprintf(2, "[plane-claim-iter n=%d t=%d obs=%u expect=%u page=%p op=%u tid=%d]\n",
				__claim_iters, t, (unsigned)obs, (unsigned)expect, (void*)page, (unsigned)(op), __diag_ring_tid());
		}
		if (__atomic_compare_exchange_n(&page->request_state, &expect,
		        DSERVER_PROCESS_CONTROL_PENDING, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) { slot = 1; break; }
		long ts[2] = {0, 1000000L};
		// MEASURED need: the thread died at this syscall and the program's SIGSEGV handler never ran. A
		// synchronously-generated fatal signal that is BLOCKED kills the process at the next return to user mode
		// with the default action and WITHOUT running a handler -- which is indistinguishable from "no handler".
		// Unblocking the fatal set here makes the handler (installed by the workload) run if that is the cause,
		// and changes nothing if it is not. rt_sigprocmask=14, SIG_SETMASK=2, 8-byte sigset.
		if (__plane_steps_enabled()) {
			static int __unblocked = 0;
			if (!__unblocked) {
				__unblocked = 1;
				unsigned long empty_set = 0;
				(void)LINUX_SYSCALL4(14 /*rt_sigprocmask*/, 2 /*SIG_SETMASK*/, (long)&empty_set, 0, 8);
				__simple_fprintf(2, "[plane-step n=unblock op=%u tid=%d]\n", (unsigned)(op), __diag_ring_tid());
			}
		}
		if (__plane_steps_enabled() && (t < 3 || (t % 500) == 0)) { __simple_fprintf(2, "[plane-claim-mark] A pre-sleep op=%u t=%d\n", (unsigned)op, t); }
		__MLDR_PLANE_STEP("N-sleep-enter", op);
		(void)LINUX_SYSCALL6(__NR_nanosleep, ts, 0, 0, 0, 0, 0);
		__MLDR_PLANE_STEP("O-sleep-done", op);
		if (__plane_steps_enabled() && (t < 3 || (t % 500) == 0)) { __simple_fprintf(2, "[plane-claim-mark] B post-sleep op=%u t=%d\n", (unsigned)op, t); }
	}
	if (!slot) {
		// The reason that matters most: the slot is held by another request of THIS process. Its op and sequence are
		// printed with the state, so "another thread is stuck in the plane" is read rather than inferred.
		if (__plane_steps_enabled()) { __simple_fprintf(2, "[plane-exit] X5\\n"); }
		__MLDR_PLANE_REFUSE("no-slot", __atomic_load_n(&page->request_state, __ATOMIC_ACQUIRE),
			((unsigned long long)__atomic_load_n(&page->request_op, __ATOMIC_ACQUIRE) << 32)
			| (unsigned long long)__atomic_load_n(&page->request_seq, __ATOMIC_ACQUIRE));
		return -1;   // no slot within the bound: the caller may fall back
	}
	__MLDR_PLANE_STEP("F-claimed", op);
	static uint32_t seq = 0;
	/* BOUNDED REPUBLISH (dar-4cp9). MEASURED: the reply wait below had NO bound, so a request whose completion
	 * never arrived left the calling thread inside this function forever -- the entry trace showed [plane-enter]
	 * with no exit mark, launchd never reached its fallback, and the boot failed as a shellspawn timeout. Exactly
	 * one boot in about five reproduces it. Re-publishing is safe for the same reason the attach re-publish is
	 * safe: a transaction is keyed by (pid, seq), so a new sequence is a NEW transaction. The label sits before
	 * every state this request owns, so a retry re-initialises them rather than continuing with stale ones, and
	 * the doorbell is re-rung because it is published after the label. */
	int plane_attempts = 0;
	long long plane_wait_started = 0;
	long long plane_wait_elapsed = 0;
plane_request_again:
	++plane_attempts;
	uint32_t mine = __atomic_add_fetch(&seq, 1, __ATOMIC_RELAXED);
	page->reply_state = DSERVER_PROCESS_CONTROL_IDLE;
	// perf#30 (doc section 91): CLEAR the reply payload with the request. MEASURED: a descriptor-bearing
	// request whose server side sent nothing left the PREVIOUS request's token in reply_payload[1], and the
	// guest resolved a token that belonged to an earlier operation. An answer field must be tied to the
	// request that produced it, which starts with not inheriting one.
	page->reply_payload[0] = 0;
	page->reply_payload[1] = 0;
	page->request_op = op;
	page->request_seq = mine;
	// perf#30 (directive D1): the THREAD identity travels with the request. Raw syscall, and published
	// BEFORE the release store below, so the server can never observe a request with a stale tid.
	{
		int32_t realTid = (int32_t)LINUX_SYSCALL(__NR_gettid);
		// perf#30 (directive section 9): TEST-ONLY mutation. A request that publishes a WRONG thread identity
		// must be refused (-ESRCH), and the cancellation state of the real thread must not move: servicing
		// whatever the pid happened to resolve to is exactly the defect this envelope field removes. Kept
		// behind a hatch so it can be exercised on demand and never in an acceptance run.
		const char* mutate = NULL;
		if (__dserver_ring_env_cache_ready == 0) {
			__dserver_ring_env_cache = getenv("DARLING_GUEST_PLANE_TID_MUTATE");
			__dserver_ring_env_cache_ready = 1;
		}
		mutate = __dserver_ring_env_cache;
		page->request_tid = (mutate != NULL && mutate[0] == '1') ? (realTid + 7919) : realTid;
	}
	page->request_payload[0] = p0;
	page->request_payload[1] = p1;
	page->request_payload[2] = p2;
	page->request_payload[3] = p3;
	__atomic_store_n(&page->request_state, DSERVER_PROCESS_CONTROL_PENDING, __ATOMIC_RELEASE);
	// Wake the server on the PROCESS DOORBELL. Never a courier byte (directive section 8): the courier carries
	// SCM_RIGHTS descriptors only, and a zero-fd wake is exactly the temporary architecture being removed.
	int db = __dserver_ring_doorbell(-1);
	if (db >= 0) {
		uint64_t one = 1;
		(void)LINUX_SYSCALL3(__NR_write, db, &one, sizeof(one));
	}
	__MLDR_PLANE_STEP("I-doorbell-done", op);
	int spins = 0, claimed = 0;
	int slow = 0;
	static volatile int planeSlowReported = 0;
	/* A COMPLETION BELONGS TO ITS PUBLISHER (dar-4cp9): MEASURED, the attach loop exited one millisecond
	 * in because a request still in flight when this thread reset the slot wrote its own DONE over the
	 * reset, and the caller then declared the attach unanswered although the page was simply not read
	 * correctly. Wait for OUR sequence; a foreign DONE is a stale answer, not our completion. */
	while (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) != DSERVER_PROCESS_CONTROL_DONE
	       || __atomic_load_n(&page->reply_seq, __ATOMIC_ACQUIRE) != (uint32_t)mine) {
		if (__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_CLAIMED) { claimed = 1; }
		if (++spins < 20000) { continue; }
		{
			uint32_t seen = page->futex;
			long ts[2] = {0, claimed ? 2000000L : 1000000L};
			if (page->reply_state == DSERVER_PROCESS_CONTROL_DONE) { break; }
			(void)LINUX_SYSCALL6(__NR_futex, &page->futex, 0, seen, ts, 0, 0);
			// perf#30 DIAGNOSIS: a published request whose completion never arrives. Without this, the wait is
			// silent (the loop only leaves on DONE), so "the server never serviced op N" and "the server serviced
			// it and the guest cannot see the completion" are indistinguishable from the outside. ONE line per
			// process, after ~5 s of timeouts, from the same raw writer the refusal path uses (no allocation).
			/* The bound that makes this wait finite. Two seconds of timeouts (2000 slow steps) is far
			 * beyond any healthy completion on this path -- the same order the attach route uses -- and three
			 * attempts cover the measured transient. After the last attempt the wait is left as it was, so a
			 * legitimately slow operation behaves exactly as before. */
			/* TIME, NOT ITERATIONS. MEASURED: this bound was written as `slow >= 2000` iterations, and the
			 * futex wait above uses a one-to-two second timeout, so 2000 iterations is twenty to forty minutes --
			 * the republish could never fire, which is exactly what the logs showed (republish=0 while threads sat
			 * in this wait: [plane-enter] with no exit, and plane-slow never reached either for the same reason).
			 * The clock is read with the raw Linux call against CLOCK_MONOTONIC, so the bound is host time and does
			 * not depend on the guest clock. */
			{
				long nowts[2] = {0, 0};
				(void)LINUX_SYSCALL2(__NR_clock_gettime, 1 /*CLOCK_MONOTONIC*/, (long)nowts);
				long long nowms = (long long)nowts[0] * 1000ll + (long long)(nowts[1] / 1000000l);
				if (plane_wait_started == 0) { plane_wait_started = nowms; }
				plane_wait_elapsed = nowms - plane_wait_started;
			}
			if (plane_attempts < 3 && plane_wait_started != 0 && plane_wait_elapsed >= 2000) {
				__simple_fprintf(2, "[plane-republish] op=%u attempt=%d state=%u rseq=%u tid=%d\n",
					(unsigned)op, plane_attempts,
					(unsigned)__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE),
					(unsigned)page->reply_seq, (int)LINUX_SYSCALL(__NR_gettid));
				/* A GIVE-UP MUST NOT TAKE BACK A SLOT THE SERVER OWNS (dar-b5pe), AND IT MUST STILL RETURN A SPENT ONE. MEASURED:
	 * a CAS release from PENDING alone broke the boot twelve times out of twelve, because the plain store it
	 * replaced also returned a slot left at DONE -- and the code's own comment records that leaving a slot at DONE
	 * breaks the boot worse than any other choice. So the only thing this changes is the CLAIMED case: the server
	 * owns the request, and the slot stays as it is until its completion arrives. */
	if (__atomic_load_n(&(page)->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_CLAIMED) {
		static int __release_held = 0;
		if (!__release_held) {
			__release_held = 1;
			__simple_fprintf(2, "[release-held-claimed] op=%u state=%u rstate=%u tid=%d\n",
				(unsigned)__atomic_load_n(&(page)->request_op, __ATOMIC_ACQUIRE),
				(unsigned)__atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE),
				(unsigned)__atomic_load_n(&(page)->reply_state, __ATOMIC_ACQUIRE),
				(int)LINUX_SYSCALL(__NR_gettid));
		}
	} else {
		DSERVER_PROCESS_CONTROL_RELEASE(page);
	}
				goto plane_request_again;
			}
			if (++slow >= 5000 && !planeSlowReported) {
				planeSlowReported = 1;
				__simple_fprintf(2, "[plane-slow op=%u state=%u rseq=%u rs=%d rq=%u tid=%d]\n",
					(unsigned)op, (unsigned)__atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE),
					(unsigned)page->reply_seq, (int)page->reply_status,
					(unsigned)__atomic_load_n(&page->request_state, __ATOMIC_ACQUIRE),
					(int)LINUX_SYSCALL(__NR_gettid));
			}
		}
	}
	uint32_t seenState = __atomic_load_n(&page->reply_state, __ATOMIC_ACQUIRE);
	uint32_t seenSeq = page->reply_seq;
	int32_t seenStatus = page->reply_status;
	// the reply PAYLOAD is part of the answer and must be snapshotted before the release too: the
	// descriptor-bearing routes return a courier token there (the same split ATTACH_LANE uses).
	uint64_t seenReply0 = page->reply_payload[0];
	uint64_t seenReply1 = page->reply_payload[1];
	{ uint32_t _st = __atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE); if (_st == DSERVER_PROCESS_CONTROL_PENDING) { static const char _m[] = "[release-drops-pending] site=dserver-ring.c:2752\n"; long _a = 1, _d = 2, _s = (long)_m, _n = sizeof(_m) - 1; __asm__ volatile("syscall" : "+a"(_a), "+D"(_d), "+S"(_s), "+d"(_n) : : "rcx", "r11", "memory"); } }
	/* A GIVE-UP MUST NOT TAKE BACK A SLOT THE SERVER OWNS (dar-b5pe), AND IT MUST STILL RETURN A SPENT ONE. MEASURED:
	 * a CAS release from PENDING alone broke the boot twelve times out of twelve, because the plain store it
	 * replaced also returned a slot left at DONE -- and the code's own comment records that leaving a slot at DONE
	 * breaks the boot worse than any other choice. So the only thing this changes is the CLAIMED case: the server
	 * owns the request, and the slot stays as it is until its completion arrives. */
	if (__atomic_load_n(&(page)->reply_state, __ATOMIC_ACQUIRE) == DSERVER_PROCESS_CONTROL_CLAIMED) {
		static int __release_held = 0;
		if (!__release_held) {
			__release_held = 1;
			__simple_fprintf(2, "[release-held-claimed] op=%u state=%u rstate=%u tid=%d\n",
				(unsigned)__atomic_load_n(&(page)->request_op, __ATOMIC_ACQUIRE),
				(unsigned)__atomic_load_n(&(page)->request_state, __ATOMIC_ACQUIRE),
				(unsigned)__atomic_load_n(&(page)->reply_state, __ATOMIC_ACQUIRE),
				(int)LINUX_SYSCALL(__NR_gettid));
		}
	} else {
		DSERVER_PROCESS_CONTROL_RELEASE(page);
	}
	if (seenState != DSERVER_PROCESS_CONTROL_DONE || seenSeq != mine) {
		return -1;
	}
	__MLDR_PLANE_STEP("M-returning", op);
	if (outReply0) { *outReply0 = seenReply0; }
	if (outReply1) { *outReply1 = seenReply1; }
	return (int)seenStatus;
}

int __dserver_plane_request(uint32_t op, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3) {
	return __dserver_plane_request_ex(op, p0, p1, p2, p3, 0, 0);
}

static uint64_t gr_generated_ring_ok = 0; // generated wrappers that completed over the Ring

// perf#29 (GENERATED-WRAPPER RING ROUTE): one generic entry point for a generated fixed-shape,
// non-fd, closed request/response RPC. It reuses gr_full_trap -- the SAME publish/wait/validate code
// the hand-written simple-call shims use -- so there is no second SPSC protocol, and it reports the
// tri-state result the generated wrapper needs. The reply BODY is copied into the caller's staging
// struct; the reply CODE is returned separately so the generated code (which owns the reply-header
// type) stores it, keeping this translation unit free of generated-header dependencies.
// perf#30 (directive section 2, doc section 213): the DIAGNOSTIC A/B OPT-OUT for the blocking family. It changes
// the TRANSPORT and nothing else: the same call, the same request, the same semantic timeout travel either way, so
// comparing the two arms on one workload answers "is this the Ring or is it the semantics?" without speculating.
// Default is the Ring (the product path); only a literal `=0` selects the legacy datagram.
static int gr_blocking_ring_enabled(void) {
	static int cached = -1;
	if (cached < 0) {
		cached = gr_environ_has("DARLING_GUEST_RING_BLOCKING=0", sizeof("DARLING_GUEST_RING_BLOCKING=0") - 1) ? 0 : 1;
	}
	return cached;
}

static int gr_callnum_is_blocking_family(uint32_t callnum) {
	switch (callnum) {
		case 11u:  // fork_wait_for_child
		case 60u:  // semaphore_wait
		case 61u:  // semaphore_wait_signal
		case 62u:  // semaphore_timedwait
		case 63u:  // semaphore_timedwait_signal
			return 1;
		default:
			return 0;
	}
}

int __dserver_ring_try_generated_rpc(uint32_t callnum, const void* req_body, uint32_t req_body_len,
                                     void* reply_body, uint32_t reply_body_len, int32_t* out_code) {
	GR_TRACE("RING_TRACE gen ENTER callnum=%u tid=%d\n", (unsigned)callnum, (int)LINUX_SYSCALL(__NR_gettid));
	if (gr_callnum_is_blocking_family(callnum) && !gr_blocking_ring_enabled()) {
		// the diagnostic arm: the caller's datagram path runs exactly as it did before the migration
		return GR_RING_TRY_NOT_TAKEN;
	}
	int code = 0;
	int rc = gr_full_trap(callnum, req_body, req_body_len,
	                      (reply_body_len > 0 ? reply_body : (void*)0), reply_body_len, reply_body_len,
	                      &code);
	/* A BLOCKING OPERATION MAY WAIT FOR ITS LANE (dar-4cp9). MEASURED: the residual stress_mixed hangs are
	 * call=semaphore_timedwait -- a blocking op whose thread has no lane. It has no home for that wait today
	 * (the plane carries signals, ops 24/25, but no wait; the ring needs a lane; the per-thread socket is gone),
	 * so the only correct thing is to acquire the lane the operation needs. A blocking call can afford this by
	 * construction: it was going to wait anyway, and waiting for the transport it is about to use is strictly
	 * cheaper than parking without one. Bounded, and the attach path it drives is now itself correct and fully
	 * instrumented. */
	for (int laneWait = 0; rc == -1 && laneWait < 3000 && gr_callnum_is_blocking_family(callnum); ++laneWait) {
		{ /* 1 ms without libc */
		  struct { long tv_sec; long tv_nsec; } _ts = {0, 1000000L};
		  LINUX_SYSCALL(__NR_nanosleep, &_ts, 0); }
		if (!gr_lane_for_this_thread_named(callnum, "blocking-wait-lane")) {
			continue;
		}
		rc = gr_full_trap(callnum, req_body, req_body_len,
		                  (reply_body_len > 0 ? reply_body : (void*)0), reply_body_len, reply_body_len,
		                  &code);
	}
	if (rc == -1) {
		// Attribute the miss with the SAME reason vocabulary the hand-written helpers use, computed
		// from this thread's own lane state at this instant. Without this the generated route would
		// be invisible: "route exists, census unchanged" cannot be told apart from "never called".
		int vtid = (int)LINUX_SYSCALL(__NR_gettid);
		// NONFD_UDS_VIOLATION: the process-global directory already has an ACTIVE incarnation for this
		// thread, this callnum is non-fd and Ring-capable (we are in the generated route for it), and the
		// client is about to take the datagram path anyway. This is the migration guard: on a warmed
		// process it must stay 0.
		if (gr_proc_find_active(vtid)) {
			__atomic_fetch_add(&g_stat_nonfd_uds_violation, 1u, __ATOMIC_RELAXED);
		}
		gr_urs_note(callnum, "generated", gr_urs_reason_for_miss(vtid), 0);
		return GR_RING_TRY_NOT_TAKEN; // pre-publish miss (no lane / does not fit): datagram is safe
	}
	if (out_code) {
		*out_code = code;
	}
	GR_TRACE("RING_TRACE gen EXIT callnum=%u rc=%d code=%d\n", (unsigned)callnum, rc, (int)code);
	if (rc == 0) {
		__atomic_fetch_add(&gr_generated_ring_ok, 1u, __ATOMIC_RELAXED);
		return GR_RING_TRY_COMPLETED;
	}
	return GR_RING_TRY_COMMITTED_FAILURE;
}


int __dserver_ring_mach_port_deallocate(uint32_t target, uint32_t name, int* out_code) {
	// dserver_call_mach_port_deallocate_t { uint32_t target; uint32_t name; }
	struct {
		uint32_t target;
		uint32_t name;
	} body = { target, name };
	return gr_body_trap((uint32_t)dserver_callnum_mach_port_deallocate, &body, (uint32_t)sizeof(body), out_code);
}

int __dserver_ring_mach_port_allocate(uint32_t target, int32_t right, uint64_t name_ptr, int* out_code) {
	// dserver_call_mach_port_allocate_t { uint32_t target; int32_t right; uint64_t name aligned(8); }
	// `name` is the GUEST virtual address the kernel writes the allocated port name into; we send the
	// pointer value verbatim and the server (generic Call path) performs the same guest-memory write
	// it does over UDS. The reply is header-only (the kern_return_t). The 8-byte alignment of the
	// uint64_t matches the generated struct's __attribute__((aligned(8))) (offset 8, size 16).
	struct {
		uint32_t target;
		int32_t  right;
		uint64_t name __attribute__((aligned(8)));
	} body = { target, right, name_ptr };
	return gr_body_trap((uint32_t)dserver_callnum_mach_port_allocate, &body, (uint32_t)sizeof(body), out_code);
}

// === perf #18 P8 D3 (dar-1il.3.1.1): DUPLEX-lane wait-pump + synthetic selftest =====================
//
// The duplex lane lets the server deliver an S2C upcall to a caller that is PARKED on the ring (not in
// recvmsg) -- the deadlock class that kept deallocate/mod_refs on UDS. The minimal D3 proof drives ONE
// synthetic ECHO upcall end-to-end through the REAL dylib: the guest publishes a sentinel parent
// request, then runs a wait-pump that, while waiting for the parent's final reply, ALSO services the
// S2C upcall mailbox -- handling each upcall ON THIS (caller) thread and replying with matching
// correlation ids. NO real op rides this yet (deallocate is Phase E, blocked on D3 GREEN).
//
// Wake discipline mirrors gr_wait_reply (the proven Lane-1 model): bounded spin watching BOTH the s2c
// reply ring AND the duplex upcall mailbox, then a FUTEX_WAIT on s2c_futex with the waiter bit set
// before the pre-sleep recheck (no lost wake). After publishing an upcall REPLY we doorbell the server
// (it may be epoll-sleeping; the reply is server-inbound work just like a request is), conditionally,
// exactly like gr_wake_server.

// Handle ONE pending duplex S2C upcall on THIS thread, if present. Returns 1 if it handled one (and
// published the correlated reply + doorbelled the server), 0 if none was pending. The ECHO shape is
// the only one the minimal protocol supports; an unknown shape is replied with a nonzero status so the
// server fails the parent rather than hanging.
static int gr_duplex_pump_once(gr_lane_t* L) {
#ifdef DUPLEX_RED_NO_GUEST_PUMP
	(void)L;
	// RED arm for the D3 real-dylib gate: the guest NEVER services the S2C upcall. The parked caller
	// then sleeps through the upcall forever -> the selftest must FAIL/timeout (the parent reply never
	// comes) AND the server must NOT wedge (other clients keep making progress). Proves the gate
	// actually exercises the guest pump + the server's bounded, scoped wait.
	return 0;
#else
	dserver_ring_shm_t* cb = gr_cb(L);
	if (!dserver_ring_duplex_upcall_available(cb)) {
		return 0;
	}
	// perf#27 #7 TID ASSERTION. A lane is strictly SPSC: the thread that published the request is the only
	// thread allowed to execute its caller-S2C upcall. This is a real assertion, not a debug print -- a
	// mismatch is counted and reported, because servicing the mailbox from another thread would run
	// guest-memory effects on the wrong thread (the effect would still happen, but on a stack and a
	// register/errno context the caller never sees).
	{
		int executor = (int)LINUX_SYSCALL(__NR_gettid);
		if (L->publisher_tid != 0 && L->publisher_tid != executor) {
			gr_duplex_tid_mismatch++;
			__simple_printf("RING_TID_MISMATCH lane=%u publisher=%d executor=%d upcall=%u\n",
			                L->slot_index, L->publisher_tid, executor, cb->duplex_upcall_id);
		}
	}
	uint32_t op     = cb->duplex_upcall_op;
	uint32_t parent = cb->duplex_upcall_parent;
	uint32_t uid    = cb->duplex_upcall_id;
	if (op == DSERVER_RING_DUPLEX_UPCALL_MUNMAP) {
		// perf #18 P8 D4: the REAL vm-munmap S2C upcall (mach_port_deallocate of a mapped-region-backed
		// port drives this). Run the SAME munmap(2) the UDS recvmsg S2C path runs (dserver-rpc-defs.h
		// dserver_s2c_msgnum_munmap) ON THIS (caller) thread, so the side effect is byte-identical to UDS
		// -- only the transport differs. The reply carries the kernel return_value + errno verbatim.
		uint64_t addr = cb->duplex_upcall_addr;
		uint64_t len  = cb->duplex_upcall_len;
		long call_ret = LINUX_SYSCALL2(__NR_munmap, addr, len);
		int32_t return_value, errno_result;
		if (call_ret < 0) {
			return_value = -1;
			errno_result = (int32_t)(-call_ret);
		} else {
			return_value = (int32_t)call_ret;
			errno_result = 0;
		}
		gr_duplex_complete(cb, parent, uid, 1, return_value, errno_result, 0);
		gr_wake_server(L);
		return 1;
	}
	if (op == DSERVER_RING_DUPLEX_UPCALL_MMAP) {
		// ABI v6: the ANONYMOUS-mmap S2C -- allocatePages() on the server. Run the same mmap(2) the UDS
		// S2C path runs, ON THIS (caller) thread, with fd = -1 and offset = 0 (the publisher refuses any
		// other shape, so the mailbox never has to carry a descriptor). The reply carries the mapped
		// address in the 64-bit value field; status stays 0/-1 so the shaped readers stay valid.
		uint64_t addr = cb->duplex_upcall_addr;
		uint64_t len  = cb->duplex_upcall_len;
		uint64_t prot = (uint64_t)cb->duplex_upcall_arg;
		uint64_t flags = (uint64_t)cb->duplex_upcall_flags;
		long call_ret = LINUX_SYSCALL6(__NR_mmap, addr, len, prot, flags, (uint64_t)-1, 0);
		// Linux mmap signals failure as -errno in the return value (not via errno).
		if (call_ret < 0 && call_ret >= -4095) {
			// perf#27 #9: the FAILURE arm carries the same (status, errno) pair the UDS S2C path returns, so
			// the two transports can be compared on observable failure semantics, not just on success.
			gr_mmap_failures++;
			GR_TRACE("DUPLEX_MMAP_FAIL addr=0x%llx len=%llu errno=%ld\n",
			         (unsigned long long)addr, (unsigned long long)len, -call_ret);
			gr_duplex_complete(cb, parent, uid, 0, -1, (int32_t)(-call_ret), 0);
		} else {
			// perf#27 #8: the address is a 64-bit value in the reply; the trace is what proves the UPPER
			// HALF is actually carried rather than truncated to the low 32 bits.
			if (((uint64_t)(uintptr_t)call_ret >> 32) != 0) {
				gr_mmap_high_addr++;
			}
			gr_mmap_ok++;
			GR_TRACE("DUPLEX_MMAP_RESULT addr=0x%llx len=%llu upper32=%llu\n",
			         (unsigned long long)(uintptr_t)call_ret, (unsigned long long)len,
			         (unsigned long long)(((uint64_t)(uintptr_t)call_ret) >> 32));
			gr_duplex_complete(cb, parent, uid, 0, 0, 0, (uint64_t)(uintptr_t)call_ret);
		}
		gr_wake_server(L);
		return 1;
	}
	uint32_t a      = cb->duplex_upcall_arg;
	int32_t  status = 0;
	uint32_t result = 0;
	if (op == DSERVER_RING_DUPLEX_UPCALL_ECHO) {
		result = dserver_ring_duplex_echo_transform(a);
	} else {
		status = -1; // unsupported shape -> the server fails the parent
	}
	// publish the reply (this consumes the upcall + advertises the reply, both release-stores).
	dserver_ring_duplex_publish_reply(cb, parent, uid, status, result);
	// the reply is server-inbound work; doorbell conditionally so an epoll-sleeping server drains it.
	gr_wake_server(L);
	return 1;
#endif // DUPLEX_RED_NO_GUEST_PUMP
}

// Wait for the sentinel parent's FINAL reply on the s2c ring while pumping duplex S2C upcalls. This is
// gr_wait_reply's structure with the upcall pump interleaved into BOTH the spin and the parked loop, so
// a parked caller can never sleep through an upcall (the duplex wake model, ring_duplex_wake_gate_test).
// Give-up after publish is committed-unknown, not a UDS retry signal.
static gr_wait_result_t gr_duplex_wait_reply(gr_lane_t* L) {
	dserver_ring_t* s2c = gr_s2c(L);
	for (int i = 0; i < (DARLING_GUEST_RECVSPIN); ++i) {
		// pump first: an upcall must be serviced before the parent reply can ever arrive.
		while (gr_duplex_pump_once(L)) { /* drain all currently-pending upcalls */ }
		dserver_ring_slot_t* rep = dserver_ring_consumer_begin(s2c, GR_SLOT_SIZE, GR_SLOT_COUNT);
		if (rep) return (gr_wait_result_t){ GR_WAIT_COMPLETED, rep };
		gr_relax();
	}
	// Slow path: park on the s2c futex. Set the waiter bit BEFORE the recheck so the server's
	// conditional FUTEX_WAKE fires; re-pump + re-check the ring after every wake.
	//
	// CRITICAL (the lane's whole reason to exist): the FUTEX_WAIT is BOUNDED (a per-wait timespec),
	// NOT an indefinite block. The duplex lane exists to keep a caller-S2C op from DEADLOCKING a parked
	// guest; if an upcall is somehow never serviced (a bug, a torn-down server, or -- in the D3 RED arm
	// -- a deliberately disabled pump), an UNBOUNDED FUTEX_WAIT would re-introduce exactly the wedge we
	// are trying to prevent (and, since the selftest can run in launchd, would wedge boot). So we cap
	// the total parked time and give up -> committed-unknown. The cap is generous (a real reply lands
	// in microseconds) so it never trips in GREEN.
	uint32_t* word = &gr_cb(L)->s2c_futex;
	uint32_t* waiters = &gr_cb(L)->s2c_waiters;
	__atomic_store_n(waiters, 1u, __ATOMIC_RELEASE);
	dserver_ring_slot_t* result = 0;
	// 50ms per FUTEX_WAIT * up to 60 rounds = ~3s total worst-case bound before giving up.
	struct { long tv_sec; long tv_nsec; } ts = { 0, 50L * 1000L * 1000L };
	for (int guard = 0; guard < 60; ++guard) {
		uint32_t observed = __atomic_load_n(word, __ATOMIC_ACQUIRE);
		// pump BEFORE sleeping: an upcall published just before we parked must be drained, not slept
		// through. The waiter bit is already set, so any reply/upcall the server publishes bumps the
		// futex word -> our FUTEX_WAIT returns EAGAIN and we re-pump. (duplex wake model: guest pump.)
#ifndef D6_RED_NO_PUMP
		while (gr_duplex_pump_once(L)) { /* drain */ }
#endif
		// D6_RED_NO_PUMP arm: the guest NEVER pumps the S2C upcall. The server's caller-S2C munmap is then
		// never serviced -> the parent op never completes -> the wait MUST bound out (return NULL) without
		// wedging (proving the cure is real: pumping is what completes it, and the absence is bounded-safe).
		dserver_ring_slot_t* rep = dserver_ring_consumer_begin(s2c, GR_SLOT_SIZE, GR_SLOT_COUNT);
		if (rep) { result = rep; break; }
		long r = LINUX_SYSCALL(__NR_futex, word, GR_FUTEX_WAIT, observed, &ts, 0, 0);
		(void)r; // EAGAIN (word changed) / EINTR / ETIMEDOUT -> re-loop, re-pump, re-check; on a
		         // persistent no-reply we exhaust the guard and return committed-unknown (bounded, no wedge).
	}
	__atomic_store_n(waiters, 0u, __ATOMIC_RELEASE);
	if (result) {
		return (gr_wait_result_t){ GR_WAIT_COMPLETED, result };
	}
	return (gr_wait_result_t){ GR_WAIT_COMMITTED_UNKNOWN, 0 };
}

// perf#26 RING-MACH-MSG: the PRODUCTION duplex-aware wait for a real blocking mach_msg_overwrite. This
// is deliberately NOT gr_duplex_wait_reply: that helper is a BOUNDED proof primitive (50ms * 60 rounds
// ~= 3s before committed-unknown), which is right for a regression proof whose liveness must never wedge
// boot, and WRONG as the semantics of a real blocking receive -- it would turn a slow server (a stopped
// debugger, a contended box) into a fabricated KERN_FAILURE on an op stock would simply have kept
// waiting on.
//
// The Mach timeout is NOT implemented here. The server runs the real mach_msg_overwrite trap with the
// guest's own option/timeout fields, so the server decides MACH_RCV_TIMED_OUT / MACH_SEND_TIMED_OUT and
// publishes the final reply; the transport must therefore out-wait the reply, never race it. There is no
// transport deadline: the loop exits only when the reply slot appears.
//
// Progress is still guaranteed for the S2C mailbox: the timed FUTEX_WAIT below bounds only the PUMP
// LATENCY (a caller-S2C published while we are parked also bumps s2c_futex and wakes us, so the bound is
// a backstop, not the mechanism), never the total wait.
// perf#26: the mach_msg production wait gets its OWN spin budget. The shared DARLING_GUEST_RECVSPIN
// (512 iterations, well under a microsecond) is far too small for a full round trip that has to reach
// the server and come back, so every fast reply missed the spin and paid a futex park + wake. 20000
// relaxed spins cost a few tens of microseconds of CPU at worst -- bounded, once per op -- and are
// irrelevant to a long wait (B3's 5s receive pays it once and then parks), while a short reply is now
// caught without touching the kernel at all.
// perf#27 #2: CHOSEN SPIN BUDGET. A controlled sweep (bench_simple, 20000 ops/arm, spin in
// {512,2000,10000,20000,40000}, latency + guest/server CPU per op from /proc) put the knee between 10k
// and 20k: below it the reply frequently misses the spin and the round trip pays a park+wake (server CPU
// 106.5k ns/op at 512, 55.5k at 2000), above it the spin is pure waste (24.4k ns/op and p95 95us at
// 40000). 10000 was the Pareto point: 23.4k ns/op, p95 66.0k ns/op, server CPU 30.5k ns/op -- as fast as
// 20000 on median/p50 while BETTER on p95 and server CPU. The state-aware policy was measured and
// REJECTED: it was worse on both (31.8k ns/op, p95 106k), so the fixed value stands.
#ifndef GR_MACHMSG_SPIN
#define GR_MACHMSG_SPIN 10000
#endif

// perf#27 #2/#3: the spin budget is a TUNABLE, so a sweep can measure six values without six rebuilds, and
// an optional state-aware policy can be A/B'd against the fixed value. Both are read once (libc-free env
// scan) and are guest-only, ABI-neutral knobs.
//
//   DARLING_GUEST_MACHMSG_SPIN=<n>            fixed spin budget (default GR_MACHMSG_SPIN)
//   DARLING_GUEST_MACHMSG_SPIN_POLICY=state   budget depends on the server's published sleep state:
//                                             ACTIVE_POLLING -> the full budget, else -> GR_MACHMSG_SPIN_LOW
// The hypothesis the policy exists to test: while the server is actively draining rings a reply is
// microseconds away, so spinning pays; while it is parked in epoll the round trip includes a wakeup, so a
// long spin is wasted CPU that only delays the park. It is only used if it is Pareto-superior.
#define GR_MACHMSG_SPIN_LOW 2000
#ifndef GR_MACHMSG_SPIN_MAX
#define GR_MACHMSG_SPIN_MAX 200000
#endif
static uint32_t gr_spin_budget(void) {
	static uint32_t cached = 0;
	if (cached == 0) {
		cached = GR_MACHMSG_SPIN;
		char buf[8];
		static const char key[] = "DARLING_GUEST_MACHMSG_SPIN=";
		__SIZE_TYPE__ kl = sizeof(key) - 1;
		long efd = LINUX_SYSCALL(__NR_open, "/proc/self/environ", 0, 0);
		if (efd >= 0) {
			char env[4096];
			long n;
			while ((n = LINUX_SYSCALL(__NR_read, efd, env, sizeof(env))) > 0) {
				for (long i = 0; i + (long)kl < n; ++i) {
					if (env[i] != key[0] || __builtin_memcmp(env + i, key, kl) != 0) continue;
					long j = i + (long)kl;
					long k = 0;
					while (j < n && env[j] >= '0' && env[j] <= '9' && k < 7) buf[k++] = env[j++];
					if (k > 0) {
						uint32_t v = 0;
						for (long q = 0; q < k; ++q) v = v * 10u + (uint32_t)(buf[q] - '0');
						if (v > 0 && v <= GR_MACHMSG_SPIN_MAX) cached = v;
					}
				}
				if ((__SIZE_TYPE__)n < sizeof(env)) break;
			}
			LINUX_SYSCALL1(__NR_close, efd);
		}
	}
	return cached;
}

static int gr_spin_state_policy(void) {
	static int cached = -1;
	if (cached < 0) {
		cached = gr_environ_has("DARLING_GUEST_MACHMSG_SPIN_POLICY=state",
		                        sizeof("DARLING_GUEST_MACHMSG_SPIN_POLICY=state") - 1) ? 1 : 0;
	}
	return cached;
}

static gr_timespec_t gr_futex_timeout;

static gr_wait_result_t gr_machmsg_wait_reply(gr_lane_t* L) {
	dserver_ring_t* s2c = gr_s2c(L);
	// perf#27 #2/#3: the budget is the tuned value, or the state-aware policy when it is selected.
	uint32_t budget = gr_spin_budget();
	if (gr_spin_state_policy() &&
	    __atomic_load_n(&gr_cb(L)->server_state, __ATOMIC_ACQUIRE) != DSERVER_RING_SRV_ACTIVE_POLLING) {
		budget = GR_MACHMSG_SPIN_LOW;
	}
	for (uint32_t i = 0; i < budget; ++i) {
		dserver_ring_slot_t* rep = dserver_ring_consumer_begin(s2c, GR_SLOT_SIZE, GR_SLOT_COUNT);
		if (rep) {
			gr_spin_hits++;
			return (gr_wait_result_t){ GR_WAIT_COMPLETED, rep };
		}
		// CALLER-S2C FIRST, then the reply check. This is not optional bookkeeping: for an op whose
		// guest-memory effect only WE can perform (mmap/munmap/mprotect), the server cannot produce the
		// reply until this thread has serviced the duplex mailbox, so pumping is part of making progress.
		// Dropping it does not merely delay the reply -- the server's bounded wait for the pump expires and
		// the parent op FAILS (observed: "[D6] duplex S2C upcall TIMED OUT (no guest pump by deadline)").
		gr_duplex_pump_once(L);
		gr_relax();
	}
	// Slow path. The waiter bit is set BEFORE the pre-sleep recheck so the server can never
	// publish-then-skip-wake while we are committed to sleeping.
	gr_spin_parks++;
	uint32_t* word = &gr_cb(L)->s2c_futex;
	uint32_t* waiters = &gr_cb(L)->s2c_waiters;
	__atomic_store_n(waiters, 1u, __ATOMIC_RELEASE);
	dserver_ring_slot_t* result = 0;
	// The wait is bounded PER ITERATION, never as a whole: a single 100ms timed futex sleep bounds how long
	// a needed mailbox pump can be delayed when no wake arrives, and then the loop re-checks the ring and
	// pumps again. There is deliberately NO transport deadline -- the loop exits only when the reply slot
	// appears -- because the Mach timeout is the server's business (it runs the real trap with the guest's
	// option/timeout). B3 proves the difference: a 5s blocking receive still succeeds, which a whole-wait
	// deadline would have to fail.
	for (;;) {
		uint32_t observed = __atomic_load_n(word, __ATOMIC_ACQUIRE);
		dserver_ring_slot_t* rep = dserver_ring_consumer_begin(s2c, GR_SLOT_SIZE, GR_SLOT_COUNT);
		if (rep) { result = rep; break; }
		gr_duplex_pump_once(L); // see the spin loop: the reply may be waiting on OUR guest-memory effect
		gr_futex_waits++;
		gr_futex_timeout.tv_sec = 0;
		gr_futex_timeout.tv_nsec = 100 * 1000 * 1000;
		long r = LINUX_SYSCALL(__NR_futex, word, GR_FUTEX_WAIT, observed, &gr_futex_timeout, 0, 0);
		if (r == -11 /* EAGAIN */) gr_futex_eagain++; // the value moved before we slept: no lost wake
	}
	__atomic_store_n(waiters, 0u, __ATOMIC_RELEASE);
	if (result) {
		return (gr_wait_result_t){ GR_WAIT_COMPLETED, result };
	}
	return (gr_wait_result_t){ GR_WAIT_COMMITTED_UNKNOWN, 0 };
}

int __dserver_ring_duplex_selftest(uint32_t arg, uint32_t* out_result) {
	gr_lane_t* L = gr_lane_for_this_thread();
	if (!L) {
		return -1; // no ring for this thread
	}
	// advertise that we can pump the SELFTEST upcall shape; the server's guard requires this cap.
	// (release so the server sees it before it can observe our sentinel request.)
	__atomic_store_n(&gr_cb(L)->duplex_caps, DSERVER_RING_DUPLEX_CAP_SELFTEST, __ATOMIC_RELEASE);

	dserver_ring_t* c2s = gr_c2s(L);
	dserver_ring_slot_t* req = dserver_ring_producer_begin(c2s, GR_SLOT_SIZE, GR_SLOT_COUNT);
	if (!req) {
		return -2; // ring full
	}
	uint32_t seq = gr_next_seq(L);
	req->callnum = DSERVER_RING_DUPLEX_SELFTEST_CALLNUM;
	req->seq = seq;
	req->length = (uint32_t)sizeof(uint32_t);
	req->arena_off = 0;
	req->arena_len = 0;
	req->flags = 0;
	memcpy((char*)req + sizeof(dserver_ring_slot_t), &arg, sizeof(arg));
	dserver_ring_producer_publish(c2s);
	gr_wake_server(L);

	gr_wait_result_t wait = gr_duplex_wait_reply(L);
	if (wait.state == GR_WAIT_COMMITTED_UNKNOWN) {
		return -3; // published but gave up waiting for the final reply
	}
	dserver_ring_slot_t* rep = wait.slot;
	int rc = -4;
	if (rep->seq == seq && rep->callnum == DSERVER_RING_DUPLEX_SELFTEST_CALLNUM) {
		uint32_t inlineCap = GR_SLOT_SIZE - (uint32_t)sizeof(dserver_ring_slot_t);
		if (rep->length >= sizeof(dserver_ring_reply_hdr_t) && rep->length <= inlineCap) {
			char* payload = (char*)rep + sizeof(dserver_ring_slot_t);
			dserver_ring_reply_hdr_t* rh = (dserver_ring_reply_hdr_t*)payload;
			if (rh->code != 0) {
				rc = -5; // server reported the parent failed (guard declined / mis-correlation)
			} else if (rep->length >= sizeof(dserver_ring_reply_hdr_t) + sizeof(uint32_t)) {
				uint32_t result;
				memcpy(&result, payload + sizeof(dserver_ring_reply_hdr_t), sizeof(result));
				if (out_result) *out_result = result;
				rc = 0;
			}
		}
	}
	dserver_ring_consumer_advance(gr_s2c(L));
	return rc;
}

// The selftest trigger is a MARKER FILE, not an env var. A marker scopes the selftest to the test run
// (absent on a normal boot -> strict no-op) without the env-inheritance hazard of disrupting a specific
// daemon. The TRIGGER is the env var DARLING_GUEST_DUPLEX_SELFTEST=1, scanned libc-free from
// /proc/self/environ. THE DISCIPLINE THAT MAKES IT SAFE: the harness sets the env ONLY on the
// per-command `darling shell` invocation against an ALREADY-WARM server -- it does NOT set it at boot.
// So the long-lived daemons (launchd, shellspawn) were started without it and never run the selftest;
// only the explicitly-targeted leaf process inherits the env and runs it. (An env set at BOOT time IS
// inherited by shellspawn and wedges it -- do not do that.) Belt-and-suspenders: we also skip guest
// pid 1, and the per-process duplex wait is BOUNDED so even an unexpected daemon hit cannot wedge.
static int gr_duplex_selftest_enabled(void) {
	long efd = LINUX_SYSCALL(__NR_open, "/proc/self/environ", 0 /*O_RDONLY*/, 0);
	if (efd < 0) {
		return 0;
	}
	static const char key[] = "DARLING_GUEST_DUPLEX_SELFTEST=1";
	const __SIZE_TYPE__ keylen = sizeof(key) - 1;
	char buf[4096];
	int found = 0;
	for (;;) {
		long n = LINUX_SYSCALL(__NR_read, efd, buf, sizeof(buf));
		if (n <= 0) break;
		__SIZE_TYPE__ start = 0;
		for (__SIZE_TYPE__ i = 0; i < (__SIZE_TYPE__)n; ++i) {
			if (buf[i] == '\0') {
				if (i - start == keylen) {
					int eq = 1;
					for (__SIZE_TYPE__ k = 0; k < keylen; ++k) {
						if (buf[start + k] != key[k]) { eq = 0; break; }
					}
					if (eq) found = 1;
				}
				start = i + 1;
			}
		}
		if (found || (__SIZE_TYPE__)n < (long)sizeof(buf)) break;
	}
	LINUX_SYSCALL1(__NR_close, efd);
	return found;
}

void __dserver_ring_maybe_run_duplex_selftest(void) {
	static int ran = 0;
	if (ran) {
		return; // one-shot per process
	}
	ran = 1;
	if (!gr_duplex_selftest_enabled()) {
		return; // env not set -> strict no-op (every daemon on a normal boot)
	}
	// Skip guest init (launchd == guest-pid 1) defensively.
	if ((int)LINUX_SYSCALL(__NR_getpid) == 1) {
		return;
	}
	uint32_t arg = 0xC0FFEE42u;
	uint32_t result = 0;
	int rc = __dserver_ring_duplex_selftest(arg, &result);
	uint32_t expect = dserver_ring_duplex_echo_transform(arg);
	// stable, greppable single line for the D3 gate. PASS only iff rc==0 AND echo matches.
	if (rc == 0 && result == expect) {
		__simple_fprintf(2, "[dring-duplex-selftest] PASS rc=0 arg=0x%x result=0x%x expect=0x%x\n",
			arg, result, expect);
	} else {
		__simple_fprintf(2, "[dring-duplex-selftest] FAIL rc=%d arg=0x%x result=0x%x expect=0x%x\n",
			rc, arg, result, expect);
	}
}

// === perf #18 P8 D4 (dar-1il.3.2.1): mach_port_deallocate over the DUPLEX lane =====================
//
// deallocate is destroy-capable: dropping the last ref of a mapped-region-backed port drives a
// vm-munmap S2C upcall to THIS caller. Over the SIMPLE ring a futex-parked caller can't service that
// S2C -> deadlock (dar-1il.1), so it stayed UDS-only. The duplex lane fixes exactly this: the caller,
// while parked for the deallocate's final reply, ALSO pumps the munmap S2C on its own thread.
//
// ROUTING (the membership/safety contract): the guest sends deallocate on the duplex lane ONLY behind a
// per-command hatch (DARLING_GUEST_DUPLEX_DEALLOCATE=1, default OFF, warm-server discipline) AND only
// after advertising DSERVER_RING_DUPLEX_CAP_DEALLOCATE at attach. This routing decision is made BEFORE
// the request is published (pre-dispatch) -- so a decline is always pre-mutation and there is never a
// partial-mutation-then-UDS double-effect. On ANY transport miss (no ring / ring full / give-up / a
// server decline reply) the caller returns -1 and the trap impl UDS-falls-back, which is safe because
// the server declines BEFORE dispatching the op (it never began mutating).

// The hatch: scan /proc/self/environ libc-free for DARLING_GUEST_DUPLEX_DEALLOCATE=1. Same discipline
// as the selftest trigger -- set ONLY per-command on a warm server, never at boot (daemons must not
// inherit it). Cached after the first scan (the env is fixed for the process lifetime).
// perf#30 (directive section 26/§81): DEFAULT ON. The route is the call's correct home (destroy-capable ->
// duplex lane) and leaving it off by contract is what forces the per-thread RPC socket into existence for it.
// The hatch is inverted: DARLING_GUEST_DUPLEX_DEALLOCATE_OFF=1 disables the route, which is what a diagnostic
// run wants when it must compare against the datagram path. The warm-server caution the old contract encoded
// is answered by the measurement, not by the default: the socket-disabled boot either passes with the duplex
// parent present during bootstrap or it fails in a way that names the readiness question.
static int gr_dealloc_via_duplex_enabled(void) {
	static int cached = -1;
	if (cached >= 0) {
		return cached;
	}
	cached = 1;
	long efd = LINUX_SYSCALL(__NR_open, "/proc/self/environ", 0 /*O_RDONLY*/, 0);
	if (efd < 0) {
		cached = 1;   // default ON: an unreadable environment does not turn the route off
		return cached;
	}
	static const char key[] = "DARLING_GUEST_DUPLEX_DEALLOCATE_OFF=1";
	const __SIZE_TYPE__ keylen = sizeof(key) - 1;
	char buf[4096];
	int found = 0;
	for (;;) {
		long n = LINUX_SYSCALL(__NR_read, efd, buf, sizeof(buf));
		if (n <= 0) break;
		__SIZE_TYPE__ start = 0;
		for (__SIZE_TYPE__ i = 0; i < (__SIZE_TYPE__)n; ++i) {
			if (buf[i] == '\0') {
				if (i - start == keylen) {
					int eq = 1;
					for (__SIZE_TYPE__ k = 0; k < keylen; ++k) {
						if (buf[start + k] != key[k]) { eq = 0; break; }
					}
					if (eq) found = 1;
				}
				start = i + 1;
			}
		}
		if (found || (__SIZE_TYPE__)n < (long)sizeof(buf)) break;
	}
	LINUX_SYSCALL1(__NR_close, efd);
	cached = found ? 0 : 1;   // the hatch now DISABLES: finding ..._OFF=1 turns the route off
	return cached;
}

// Try mach_port_deallocate over the duplex lane. Returns 0 on a VALID ring round-trip (writes *out_code
// = the kern_return_t, which the caller uses verbatim); returns -1 only on a pre-publish miss or an
// explicit server DECLINE reply (safe, pre-mutation). Once published, timeout/bad-reply is
// committed-unknown and returns 0 with *out_code=KERN_FAILURE so the caller does NOT UDS-retry.
// Mirrors gr_body_trap's transport but waits with gr_duplex_wait_reply (which pumps the munmap S2C)
// and advertises the deallocate duplex cap.
// === GENERIC DUPLEX ROUND TRIP ==========================================================================
//
// ONE transport for every CALLER_S2C operation, so a new callnum costs a body and a name, not a second
// hand-written transport (perf #18 P8 D4/D5 built the transport twice; mach_port_mod_refs would have been
// the third copy).
//
// CONTRACT, unchanged from the deallocate route this is extracted from:
//   * return -1 -> nothing was published, or the server DECLINED before mutation. The caller may use
//     another transport, because no semantic effect has happened.
//   * return  0 -> a valid round trip. *out_code holds the operation's own result. Once the request is
//     published the outcome is committed-unknown at worst, and the caller must NOT retry elsewhere: a
//     retried destructive operation is worse than an error.
//   * the wait pumps caller-S2C upcalls on THIS thread (that is why a duplex op must not be routed on the
//     simple ring: a futex-parked caller cannot service the upcall the server is waiting on).
static int gr_duplex_round_trip(uint32_t callnum, const char* what, uint32_t capBit,
                                const void* body, uint32_t bodyLen, int* out_code) {
	gr_lane_t* L = gr_lane_for_this_thread_named(callnum, what);
	if (!L) {
		return -1; // no ring for this thread -> caller's fallback (reason counted at the lookup)
	}
	// Advertise the capabilities this operation may need BEFORE publishing, release-ordered, so the server
	// can see them when it observes the parent request. MUNMAP_PUMP is what a destroy-capable (or
	// last-reference-dropping) operation requires: the server routes the munmap upcall through the mailbox
	// only to a caller that said it can pump it.
	__atomic_store_n(&gr_cb(L)->duplex_caps,
		DSERVER_RING_DUPLEX_CAP_SELFTEST | capBit, __ATOMIC_RELEASE);

	uint32_t inlineCap = GR_SLOT_SIZE - (uint32_t)sizeof(dserver_ring_slot_t);
	if (bodyLen > inlineCap) {
		return -1;
	}
	dserver_ring_t* c2s = gr_c2s(L);
	dserver_ring_slot_t* req = dserver_ring_producer_begin(c2s, GR_SLOT_SIZE, GR_SLOT_COUNT);
	if (!req) {
		return -1; // ring full -> caller's fallback
	}
	uint32_t seq = gr_next_seq(L);
	req->callnum = callnum;
	req->seq = seq;
	req->length = bodyLen;
	req->arena_off = 0;
	req->arena_len = 0;
	req->flags = 0;
	memcpy((char*)req + sizeof(dserver_ring_slot_t), body, bodyLen);
	dserver_ring_producer_publish(c2s);
	gr_wake_server(L);

	gr_wait_result_t wait = gr_duplex_wait_reply(L);
	if (wait.state == GR_WAIT_COMMITTED_UNKNOWN) {
		if (out_code) *out_code = KERN_FAILURE;
		return 0; // published: never retried on another transport
	}
	dserver_ring_slot_t* rep = wait.slot;
	int rc = 0;
	if (out_code) *out_code = KERN_FAILURE;
	if (rep->seq == seq && rep->callnum == callnum) {
		if (rep->length >= sizeof(dserver_ring_reply_hdr_t) && rep->length <= inlineCap) {
			char* payload = (char*)rep + sizeof(dserver_ring_slot_t);
			dserver_ring_reply_hdr_t* rh = (dserver_ring_reply_hdr_t*)payload;
			if (rh->code == DSERVER_RING_DUPLEX_DECLINE) {
				rc = -1; // declined pre-dispatch, no mutation -> caller may fall back
			} else {
				if (out_code) *out_code = rh->code; // the operation's own result
				rc = 0;
			}
		}
	}
	dserver_ring_consumer_advance(gr_s2c(L));
	return rc;
}

// === mach_port_mod_refs over the DUPLEX lane ===========================================================
//
// WHY IT BELONGS HERE AND NOT ON THE SIMPLE RING: mod_refs(right, delta<0) can drop the last reference of a
// receive right and destroy the port; destroying a port that backs a mapped region drives a vm munmap
// SERVER-TO-CALLER upcall to THIS thread. On the simple ring the caller is parked in the reply futex and
// cannot service it -> the server microthread blocks on the upcall while the guest blocks on the reply.
// That is the identical hazard mach_port_deallocate and mach_vm_deallocate were built for, so it gets the
// identical treatment: the caller pumps the upcall on its own thread while waiting for the final reply.
//
// The routing decision is the server's, and it is pre-mutation by construction: the server dispatches this
// callnum only when the caller advertised MUNMAP_PUMP, and it declines before dispatching otherwise, so
// there is no window in which a semantic effect happened and the client believed otherwise.
int __dserver_ring_mach_port_mod_refs_duplex(uint32_t target, uint32_t name, int right, int delta, int* out_code) {
	// dserver_call_mach_port_mod_refs_t { uint32 target; uint32 name; int32 right; int32 delta; }
	struct { uint32_t target; uint32_t name; int32_t right; int32_t delta; } body = {
		target, name, (int32_t)right, (int32_t)delta,
	};
	return gr_duplex_round_trip((uint32_t)dserver_callnum_mach_port_mod_refs, "mach_port_mod_refs_duplex",
		DSERVER_RING_DUPLEX_CAP_MUNMAP_PUMP, &body, (uint32_t)sizeof(body), out_code);
}

int __dserver_ring_mach_port_deallocate_duplex(uint32_t target, uint32_t name, int* out_code) {
	if (!gr_dealloc_via_duplex_enabled()) {
		return -1; // hatch off -> UDS (the default path)
	}
	gr_lane_t* L = gr_lane_for_this_thread_named((uint32_t)dserver_callnum_mach_port_deallocate, "mach_port_deallocate_duplex");
	if (!L) {
		return -1; // no ring for this thread -> UDS fallback (reason counted at the lookup)
	}
	// advertise that we can pump the munmap S2C upcall shape (the server's duplex guard requires it).
	// release so the server sees the cap before it can observe our deallocate parent request.
	__atomic_store_n(&gr_cb(L)->duplex_caps,
		DSERVER_RING_DUPLEX_CAP_SELFTEST | DSERVER_RING_DUPLEX_CAP_DEALLOCATE, __ATOMIC_RELEASE);

	uint32_t inlineCap = GR_SLOT_SIZE - (uint32_t)sizeof(dserver_ring_slot_t);
	// dserver_call_mach_port_deallocate_t { uint32_t target; uint32_t name; }
	struct { uint32_t target; uint32_t name; } body = { target, name };
	if ((uint32_t)sizeof(body) > inlineCap) {
		return -1;
	}
	dserver_ring_t* c2s = gr_c2s(L);
	dserver_ring_slot_t* req = dserver_ring_producer_begin(c2s, GR_SLOT_SIZE, GR_SLOT_COUNT);
	if (!req) {
		return -1; // ring full -> UDS fallback
	}
	uint32_t seq = gr_next_seq(L);
	req->callnum = (uint32_t)dserver_callnum_mach_port_deallocate;
	req->seq = seq;
	req->length = (uint32_t)sizeof(body);
	req->arena_off = 0;
	req->arena_len = 0;
	req->flags = 0;
	memcpy((char*)req + sizeof(dserver_ring_slot_t), &body, sizeof(body));
	dserver_ring_producer_publish(c2s);
	gr_wake_server(L);

	// wait for the parent's FINAL reply, pumping any munmap S2C upcalls on THIS thread meanwhile.
	gr_wait_result_t wait = gr_duplex_wait_reply(L);
	if (wait.state == GR_WAIT_COMMITTED_UNKNOWN) {
		if (out_code) *out_code = KERN_FAILURE;
		return 0; // published: do NOT retry over UDS
	}
	dserver_ring_slot_t* rep = wait.slot;
	int rc = 0;
	if (out_code) *out_code = KERN_FAILURE; // bad/mismatched reply after publish: no UDS retry
	if (rep->seq == seq && rep->callnum == (uint32_t)dserver_callnum_mach_port_deallocate) {
		if (rep->length >= sizeof(dserver_ring_reply_hdr_t) && rep->length <= inlineCap) {
			char* payload = (char*)rep + sizeof(dserver_ring_slot_t);
			dserver_ring_reply_hdr_t* rh = (dserver_ring_reply_hdr_t*)payload;
			if (rh->code == DSERVER_RING_DUPLEX_DECLINE) {
				// the server declined the duplex deallocate BEFORE dispatching it (no mutation) ->
				// the caller UDS-falls-back. A reserved sentinel code, distinct from any kern_return_t.
				rc = -1;
			} else {
				if (out_code) *out_code = rh->code; // the real kern_return_t
				rc = 0;
			}
		}
	}
	dserver_ring_consumer_advance(gr_s2c(L));
	return rc;
}

// === perf #18 P8 D5 (dar-1il.3.2.2): mach_vm_deallocate over the DUPLEX lane ========================
//
// vm_deallocate is the op that ACTUALLY drives a real caller munmap S2C in Darling: the server's
// vm_map_remove -> dtape_hook_task_free_pages -> Process::freePages -> Thread::_munmap -> _s2cPerform
// sends a munmap upcall back to THIS caller. Over the SIMPLE ring a futex-parked caller can't service
// that S2C -> the dar-1il.1 deadlock. The duplex lane fixes exactly this (the caller pumps the munmap
// S2C on its own thread while parked) -- and unlike D4's mach_port_deallocate (whose S2C is unreachable
// because make_memory_entry is a stub), vm_deallocate REALLY fires it, so this is the live cure proof.
//
// Reuses the D4 duplex munmap transport VERBATIM (gr_duplex_pump_once already handles UPCALL_MUNMAP, the
// mailbox + gr_duplex_wait_reply are unchanged); the only new thing is the routing for THIS callnum
// behind its own cap + per-command hatch. Same safety contract: routing is decided PRE-DISPATCH from the
// negotiated cap (a decline is always pre-mutation, never partial-mutation-then-UDS).

// D5 boot-scoped proof arming (gist 3e928115). Two arming paths, BOTH default OFF; cached after the first
// check (arming is fixed for a process lifetime):
//   (1) a per-command ENV hatch DARLING_GUEST_DUPLEX_VM_DEALLOCATE=1 -- for WARM-server leaf testing, the
//       D4 discipline (never set at boot: a stalling op inherited by a daemon would wedge shellspawn).
//   (2) a boot-scoped MARKER FILE at a fixed guest-visible path -- the ONLY safe way to arm launchd (guest
//       pid 1) at boot WITHOUT inheriting an env into shellspawn/leaf daemons. The harness creates the
//       marker before the proof boot and removes it after, so exactly one boot is armed. A marker is NOT
//       inherited across exec the way env is; each process independently stats it. The server's own
//       conjunction (armed budget + pid==1) is the real gate -- this just lets launchd OFFER the op; the
//       server still declines pre-mutation for every non-pid-1 / unarmed caller, so a stray marker is safe.
// The path lives under the guest's writable runtime dir; the harness maps it to the host prefix.
#define DSERVER_D5_PROOF_MARKER "/private/var/run/darling_d5_vmdealloc_proof"
static int gr_vm_dealloc_via_duplex_enabled(void) {
	static int cached = -1;
	if (cached >= 0) {
		return cached;
	}
	cached = 0;
	// (2) marker file first (cheap stat). If present, arm. Scoped to the proof boot.
	{
		long mfd = LINUX_SYSCALL(__NR_open, DSERVER_D5_PROOF_MARKER, 0 /*O_RDONLY*/, 0);
		if (mfd >= 0) {
			LINUX_SYSCALL1(__NR_close, mfd);
			cached = 1;
			return 1;
		}
	}
	// (1) env hatch (warm leaf testing).
	long efd = LINUX_SYSCALL(__NR_open, "/proc/self/environ", 0 /*O_RDONLY*/, 0);
	if (efd < 0) {
		return 0;
	}
	static const char key[] = "DARLING_GUEST_DUPLEX_VM_DEALLOCATE=1";
	const __SIZE_TYPE__ keylen = sizeof(key) - 1;
	char buf[4096];
	int found = 0;
	for (;;) {
		long n = LINUX_SYSCALL(__NR_read, efd, buf, sizeof(buf));
		if (n <= 0) break;
		__SIZE_TYPE__ start = 0;
		for (__SIZE_TYPE__ i = 0; i < (__SIZE_TYPE__)n; ++i) {
			if (buf[i] == '\0') {
				if (i - start == keylen) {
					int eq = 1;
					for (__SIZE_TYPE__ k = 0; k < keylen; ++k) {
						if (buf[start + k] != key[k]) { eq = 0; break; }
					}
					if (eq) found = 1;
				}
				start = i + 1;
			}
		}
		if (found || (__SIZE_TYPE__)n < (long)sizeof(buf)) break;
	}
	LINUX_SYSCALL1(__NR_close, efd);
	cached = found;
	return found;
}

// Try mach_vm_deallocate over the duplex lane. Returns 0 on a VALID ring round-trip (writes *out_code =
// the kern_return_t verbatim); returns -1 only on a pre-publish miss or an explicit server DECLINE reply
// (safe, pre-mutation). Once published, timeout/bad-reply is committed-unknown and returns 0 with
// *out_code=KERN_FAILURE so the caller does NOT UDS-retry. Mirrors
// __dserver_ring_mach_port_deallocate_duplex but with the vm_deallocate body
// {uint32 target; uint64 address; uint64 size} and the VM_DEALLOCATE cap.
// Set by the D6 proof driver (below) while it intentionally drives ONE vm_deallocate over the duplex lane,
// so the duplex function proceeds without the production hatch (the D6 proof has its own marker gate + the
// server arming + budget bound the blast radius). Default 0 -> the production hatch governs.
static int g_d6_proof_force = 0;
int __dserver_ring_mach_vm_deallocate_duplex(uint32_t target, uint64_t address, uint64_t size, int* out_code) {
	if (!g_d6_proof_force && !gr_vm_dealloc_via_duplex_enabled()) {
		return -1; // hatch off -> UDS (the default path)
	}
	gr_lane_t* L = gr_lane_for_this_thread_named((uint32_t)dserver_callnum_mach_vm_deallocate, "mach_vm_deallocate_duplex");
	if (!L) {
		return -1; // no ring for this thread -> UDS fallback (reason counted at the lookup)
	}
	// advertise that we can pump the munmap S2C upcall shape for a vm_deallocate parent. release so the
	// server sees the cap before it can observe our vm_deallocate parent request. We OR in (not overwrite)
	// so a process that already advertised SELFTEST/DEALLOCATE keeps those.
	uint32_t prev = __atomic_load_n(&gr_cb(L)->duplex_caps, __ATOMIC_ACQUIRE);
	__atomic_store_n(&gr_cb(L)->duplex_caps,
		prev | DSERVER_RING_DUPLEX_CAP_SELFTEST | DSERVER_RING_DUPLEX_CAP_VM_DEALLOCATE, __ATOMIC_RELEASE);

	uint32_t inlineCap = GR_SLOT_SIZE - (uint32_t)sizeof(dserver_ring_slot_t);
	// dserver_call_mach_vm_deallocate_t { uint32_t target; uint64_t address; uint64_t size; } -- the
	// uint64 fields are 8-byte aligned, so the struct is 24 bytes (4 + 4 pad + 8 + 8). Build it with the
	// generated type so the layout matches the server's sizeof(dserver_call_mach_vm_deallocate_t) check.
	dserver_call_mach_vm_deallocate_t body;
	body.target = target;
	body.address = address;
	body.size = size;
	if ((uint32_t)sizeof(body) > inlineCap) {
		return -1;
	}
	dserver_ring_t* c2s = gr_c2s(L);
	dserver_ring_slot_t* req = dserver_ring_producer_begin(c2s, GR_SLOT_SIZE, GR_SLOT_COUNT);
	if (!req) {
		return -1; // ring full -> UDS fallback
	}
	uint32_t seq = gr_next_seq(L);
	req->callnum = (uint32_t)dserver_callnum_mach_vm_deallocate;
	req->seq = seq;
	req->length = (uint32_t)sizeof(body);
	req->arena_off = 0;
	req->arena_len = 0;
	req->flags = 0;
	memcpy((char*)req + sizeof(dserver_ring_slot_t), &body, sizeof(body));
	dserver_ring_producer_publish(c2s);
	gr_wake_server(L);

	// wait for the parent's FINAL reply, pumping any munmap S2C upcalls on THIS thread meanwhile.
	gr_wait_result_t wait = gr_duplex_wait_reply(L);
	if (wait.state == GR_WAIT_COMMITTED_UNKNOWN) {
		// PUBLISHED-then-TIMEOUT (bounded ~3s). CRITICAL: we have ALREADY published the parent onto the
		// duplex lane, so the SERVER owns this op now -- it may have already performed the munmap. We must
		// NOT UDS-fall-back here (that would re-run vm_deallocate = a potential double munmap of a range
		// that may have been re-mapped = the MODEL-B hazard). Report a normal kern_return_t failure without
		// retry; GREEN never trips this branch.
		if (out_code) *out_code = KERN_FAILURE;
		return 0;
	}
	dserver_ring_slot_t* rep = wait.slot;
	int rc = 0;
	if (out_code) *out_code = KERN_FAILURE; // bad/mismatched reply after publish: no UDS retry
	if (rep->seq == seq && rep->callnum == (uint32_t)dserver_callnum_mach_vm_deallocate) {
		if (rep->length >= sizeof(dserver_ring_reply_hdr_t) && rep->length <= inlineCap) {
			char* payload = (char*)rep + sizeof(dserver_ring_slot_t);
			dserver_ring_reply_hdr_t* rh = (dserver_ring_reply_hdr_t*)payload;
			if (rh->code == DSERVER_RING_DUPLEX_DECLINE) {
				// server declined pre-dispatch (no mutation) -> caller UDS-falls-back.
				rc = -1;
			} else {
				if (out_code) *out_code = rh->code; // the real kern_return_t
				rc = 0;
			}
		}
	}
	dserver_ring_consumer_advance(gr_s2c(L));
	return rc;
}

// === perf #18 P8 D6 (caller-S2C sideband): synthetic WARM real-munmap proof ==========================
//
// The conclusive D5/D6 attribution: no guest sends a vm_deallocate RPC, and the only real caller-S2C
// munmap carrier (mach_msg_overwrite) is UDS-only -> no PRODUCTION op drives a caller-S2C over the ring
// today. To prove the duplex caller-S2C sideband LIVE on the REAL transport (not the synthetic D3 echo),
// this test-only driver: (1) mmaps a real page in THIS guest's address space; (2) sends a vm_deallocate
// of that page with target==mach_task_self() OVER the duplex ring, BYPASSING the trap's local-munmap gate
// (__dserver_ring_mach_vm_deallocate_duplex publishes straight to the c2s ring). On the server,
// _kernelrpc_mach_vm_deallocate_trap resolves target to the CURRENT task and frees the page via
// vm_map_remove -> task_free_pages -> a REAL munmap S2C back to THIS (ring-parked) caller, which rides the
// duplex mailbox. We pump it on this thread (real munmap(2)), the server fiber resumes, and the final
// parent reply returns over the ring. This exercises the ENTIRE real mechanism end-to-end.
//
// Triggered by a MARKER FILE (/private/tmp/.dring-d6-munmap-go), one-shot, warm-server discipline (never
// at boot). The server must be ARMED (DARLING_SERVER_D5_VMDEALLOC_PROOF=<budget>) or it declines
// pre-dispatch (the guest then UDS-falls-back harmlessly -- but since we deallocate our OWN just-mapped
// page, a decline means the page stays mapped, which is safe).
static int gr_d6_munmap_proof_enabled(void) {
	// Trigger = the ENV var DARLING_GUEST_D6_MUNMAP_PROOF=1 on the leaf command (the PROVEN D3 selftest
	// pattern: shellspawn is already running WITHOUT it, so only the explicitly-targeted leaf inherits it
	// and runs the proof -- safe per-command on a warm server, never at boot). The per-process duplex wait
	// is BOUNDED so even an unexpected inheritor cannot wedge. Scanned libc-free from /proc/self/environ.
	long efd = LINUX_SYSCALL(__NR_open, "/proc/self/environ", 0 /*O_RDONLY*/, 0);
	if (efd < 0) {
		return 0;
	}
	static const char key[] = "DARLING_GUEST_D6_MUNMAP_PROOF=1";
	const __SIZE_TYPE__ keylen = sizeof(key) - 1;
	char buf[4096];
	int found = 0;
	for (;;) {
		long n = LINUX_SYSCALL(__NR_read, efd, buf, sizeof(buf));
		if (n <= 0) break;
		__SIZE_TYPE__ start = 0;
		for (__SIZE_TYPE__ i = 0; i < (__SIZE_TYPE__)n; ++i) {
			if (buf[i] == '\0') {
				if (i - start == keylen) {
					int eq = 1;
					for (__SIZE_TYPE__ k = 0; k < keylen; ++k) {
						if (buf[start + k] != key[k]) { eq = 0; break; }
					}
					if (eq) found = 1;
				}
				start = i + 1;
			}
		}
		if (found || (__SIZE_TYPE__)n < (long)sizeof(buf)) break;
	}
	LINUX_SYSCALL1(__NR_close, efd);
	return found;
}

void __dserver_ring_maybe_run_d6_munmap_proof(uint32_t self_task_name) {
	static int ran = 0;
	if (ran) return;
	ran = 1;
	if (!gr_d6_munmap_proof_enabled()) {
		return; // env trigger absent -> strict no-op (every normal process)
	}
	// (1) mmap a real anonymous page locally (PROT_READ|WRITE, MAP_PRIVATE|ANON).
	long pg = LINUX_SYSCALL(__NR_mmap, 0, 4096, 0x3 /*RW*/, 0x22 /*PRIVATE|ANON*/, -1, 0);
	if (pg < 0 || pg == 0) {
		__simple_fprintf(2, "[dring-d6-munmap] FAIL mmap rc=%ld\n", pg);
		return;
	}
	*((volatile char*)pg) = 0x5A; // fault the page in so there is something to munmap.
	// (2) send vm_deallocate of THAT page over the duplex ring with our task-self port name (the server
	// trap resolves it to current_task() and frees OUR map -> real munmap S2C back to this ring-parked
	// caller, which rides the duplex mailbox). self_task_name is the caller's mach_task_self() name.
	int code = 0xDEAD;
	g_d6_proof_force = 1; // let the duplex function proceed without the production hatch (proof gate above)
	int rc = __dserver_ring_mach_vm_deallocate_duplex(self_task_name, (uint64_t)pg, 4096, &code);
	g_d6_proof_force = 0;
	if (rc == 0) {
		__simple_fprintf(2, "[dring-d6-munmap] PASS rc=0 code=%d addr=0x%lx (real caller-S2C munmap over duplex)\n", code, (unsigned long)pg);
	} else {
		__simple_fprintf(2, "[dring-d6-munmap] FAIL rc=%d (server declined / transport miss -- not armed?)\n", rc);
	}
}

int __dserver_ring_mach_port_insert_right(uint32_t target, uint32_t name, uint32_t poly, int32_t polyPoly, int* out_code) {
	// dserver_call_mach_port_insert_right_t { uint32_t target; uint32_t name; uint32_t poly; int32_t polyPoly; }
	struct {
		uint32_t target;
		uint32_t name;
		uint32_t poly;
		int32_t  polyPoly;
	} body = { target, name, poly, polyPoly };
	return gr_body_trap((uint32_t)dserver_callnum_mach_port_insert_right, &body, (uint32_t)sizeof(body), out_code);
}


// === perf#26 RING-MACH-MSG: mach_msg_overwrite over the lane =======================================
//
// The first BLOCKING RPC on the ring, and the first real op whose caller-local S2C the lane must
// service. The 40-byte body carries {msg, option, send_size, rcv_size, rcv_name, timeout, priority,
// rcv_msg} with msg/rcv_msg as raw GUEST ADDRESSES -- the Mach bytes never enter the RPC body, so the
// request fits the inline slot and no arena is involved. The reply is header-only: the
// mach_msg_return_t is the reply code.
//
// While the caller waits for the parent reply it MUST pump the duplex mailbox, because the server's OOL
// copyout path can free pages and raise a caller-local munmap S2C on THIS thread, which a parked caller
// can only service from its own wait loop. That is gr_duplex_wait_reply's job.
//
// Approved subset: both interrupt-observing options clear. The UDS path has an EINTR retry protocol
// tied to its receive and the ring wait has no equivalent, so such a shape stays on UDS until that is
// designed. Everything not in the subset falls back pre-publish with a named reason counter.
//
// Returns 0 on a VALID ring round-trip and writes *out_code = the mach_msg_return_t. Returns -1 only on
// a pre-publish miss or an explicit server DECLINE (both safe: the op did not run). After publication a
// timeout or a bad reply is committed-unknown -> returns 0 with *out_code = KERN_FAILURE, never a retry.
static int gr_environ_int(const char* prefix, __SIZE_TYPE__ plen, int defl) {
	long efd = LINUX_SYSCALL(__NR_open, "/proc/self/environ", 0 /*O_RDONLY*/, 0);
	if (efd < 0) return defl;
	char buf[4096];
	int value = defl;
	for (;;) {
		long n = LINUX_SYSCALL(__NR_read, efd, buf, sizeof(buf));
		if (n <= 0) break;
		for (long i = 0; i + (long)plen < n; ++i) {
			if (__builtin_memcmp(buf + i, prefix, plen) != 0) continue;
			long v = 0, j = i + (long)plen, k = 0;
			while (j < n && buf[j] >= '0' && buf[j] <= '9' && k < 9) { v = v * 10 + (buf[j] - '0'); ++j; ++k; }
			if (k > 0 && v > 0) value = (int)v;
		}
		if ((__SIZE_TYPE__)n < sizeof(buf)) break;
	}
	LINUX_SYSCALL1(__NR_close, efd);
	return value;
}

static int gr_environ_has(const char* key, __SIZE_TYPE__ keylen) {
	long efd = LINUX_SYSCALL(__NR_open, "/proc/self/environ", 0 /*O_RDONLY*/, 0);
	if (efd < 0) {
		return 0;
	}
	char buf[4096];
	int found = 0;
	for (;;) {
		long n = LINUX_SYSCALL(__NR_read, efd, buf, sizeof(buf));
		if (n <= 0) break;
		for (long i = 0; i + (long)keylen <= n; ++i) {
			if (buf[i] == key[0] && __builtin_memcmp(buf + i, key, keylen) == 0) { found = 1; break; }
		}
		if (found || (__SIZE_TYPE__)n < (long)sizeof(buf)) break;
	}
	LINUX_SYSCALL1(__NR_close, efd);
	return found;
}

int __dserver_ring_environ_has(const char* key, __SIZE_TYPE__ keylen) {
	return gr_environ_has(key, keylen);
}

// perf#27 #5: PROCESS-SCOPED transport accounting. The server's counters are process-global, so they
// cannot answer "did THIS process use any UDS for mach_msg?" -- bootstrap and every other client are mixed
// in. One line per transport decision, carrying both the process identity and the transport, lets a gate
// count exactly the process under test. Same opt-in gate as the rest of the trace.
void __dserver_ring_note_uds_machmsg(void) {
	gr_uds_machmsg_fallbacks++;
	__dserver_ring_proc_event("request", "UDS", 0, 0, (uint32_t)dserver_callnum_mach_msg_overwrite);
}

void __dserver_ring_proc_event(const char* direction, const char* transport, uint32_t lane, uint32_t seq,
                               uint32_t callnum) {
	if (!gr_trace_enabled()) {
		return;
	}
#ifdef VARIANT_DYLD
	static const char image[] = "dyld";
#else
	static const char image[] = "kernel";
#endif
	__simple_printf("MACHMSG_TRANSPORT process=%d image=%s host_tid=%d lane=%u seq=%u direction=%s "
	                "transport=%s callnum=%u\n",
	                (int)LINUX_SYSCALL(__NR_getpid), image, (int)LINUX_SYSCALL(__NR_gettid),
	                lane, seq, direction, transport, callnum);
}

static int gr_machmsg_enabled(void) {
	static int cached = -1;
	if (cached < 0) {
		// perf#30 (directive section 2, doc section 206): RING IS THE DEFAULT for a supported mach_msg shape.
		// MEASURED before this: the hatch had to be set to 1 by hand, and the hard oracle then denied
		// `mach_msg_overwrite` on its first use -- the product path was reachable only through an environment
		// variable, which is exactly the architecture this work is replacing. The variable is now a DIAGNOSTIC
		// OPT-OUT for a UDS A/B benchmark, not the switch that enables the product: an unset variable means the
		// Ring, and only a literal `=0` selects the legacy datagram.
		static const char key[] = "DARLING_GUEST_RING_MACH_MSG=0";
		cached = gr_environ_has(key, sizeof(key) - 1) ? 0 : 1;
	}
	return cached;
}

int __dserver_ring_mach_msg_overwrite(void* msg, int32_t option, uint32_t send_size, uint32_t rcv_size,
                                      uint32_t rcv_name, uint32_t timeout, uint32_t priority, void* rcv_msg,
                                      int* out_code) {
	if (!gr_machmsg_enabled()) {
		return -1; // hatch off -> UDS (the default path)
	}
	if (((uint32_t)option & GR_MACH_SEND_INTERRUPT) != 0 || ((uint32_t)option & GR_MACH_RCV_INTERRUPT) != 0) {
		gr_machmsg_fallback_interrupt++;
		return -1; // not in the approved subset -> UDS
	}
	gr_lane_t* L = gr_lane_for_this_thread();
	if (!L) {
		// perf#30 (doc section 206): a DECLINED supported shape must be visible, not inferred. MEASURED need: with
		// the Ring as the default for mach_msg the hard oracle still denied `mach_msg_overwrite` at log line 15,
		// and nothing in the log said whether the attempt had been made at all. Bounded to a handful of lines per
		// process so a boot log is never flooded.
		gr_machmsg_fallback_no_lane++;
		if (gr_machmsg_fallback_no_lane <= 4) {
			__simple_fprintf(2, "[dring-machmsg-fallback] pid=%d tid=%d reason=no-lane count=%u\n",
				(int)LINUX_SYSCALL(__NR_getpid), (int)LINUX_SYSCALL(__NR_gettid),
				(unsigned)gr_machmsg_fallback_no_lane);
		}
		return -1;
	}

	uint32_t inlineCap = GR_SLOT_SIZE - (uint32_t)sizeof(dserver_ring_slot_t);
	dserver_call_mach_msg_overwrite_t body;
	body.msg = (uint64_t)(uintptr_t)msg;
	body.option = option;
	body.send_size = send_size;
	body.rcv_size = rcv_size;
	body.rcv_name = rcv_name;
	body.timeout = timeout;
	body.priority = priority;
	body.rcv_msg = (uint64_t)(uintptr_t)rcv_msg;
	if ((uint32_t)sizeof(body) > inlineCap) {
		gr_machmsg_fallback_shape++;
		return -1;
	}

	// Advertise the mach-msg duplex cap BEFORE publishing: the server serves callnum 38 from the ring
	// only for a caller that advertised it, so without this the request would be declined.
	//
	// Also advertise MUNMAP_PUMP (DEALLOCATE|VM_DEALLOCATE, matched as "any bit set"): a real
	// caller-S2C munmap arrives while this caller is parked in gr_machmsg_wait_reply, and the server's
	// duplex guard refuses to route it into the mailbox unless the caller declared it can pump that
	// shape. This caller does -- gr_machmsg_wait_reply pumps gr_duplex_pump_once, which handles
	// DSERVER_RING_DUPLEX_UPCALL_MUNMAP. Without the bit the guard declines, the server falls back to
	// a UDS S2C, and the ring-parked caller can never service it: the op then hangs until the
	// shellspawn timeout (the exact failure this line fixes).
	uint32_t prev = __atomic_load_n(&gr_cb(L)->duplex_caps, __ATOMIC_ACQUIRE);
	__atomic_store_n(&gr_cb(L)->duplex_caps,
	                 prev | DSERVER_RING_DUPLEX_CAP_MACH_MSG | DSERVER_RING_DUPLEX_CAP_DEALLOCATE
	                      | DSERVER_RING_DUPLEX_CAP_VM_DEALLOCATE,
	                 __ATOMIC_RELEASE);

	dserver_ring_t* c2s = gr_c2s(L);
	dserver_ring_slot_t* req = dserver_ring_producer_begin(c2s, GR_SLOT_SIZE, GR_SLOT_COUNT);
	if (!req) {
		return -1; // ring full -> UDS fallback (pre-publish, safe)
	}
	uint32_t seq = gr_next_seq(L);
	// perf#27 #7 RED arm: `DARLING_GUEST_TID_MUTATE=1` records a WRONG publisher so the assert below must
	// fire. It exists because an assertion that has never been observed failing is not evidence that it
	// checks anything; the GREEN arm (mismatch == 0) only means something next to a RED arm that trips it.
	L->publisher_tid = (int)LINUX_SYSCALL(__NR_gettid) + (gr_tid_mutate_enabled() ? 1 : 0);
	req->callnum = (uint32_t)dserver_callnum_mach_msg_overwrite;
	req->seq = seq;
	req->length = (uint32_t)sizeof(body);
	req->arena_off = 0;
	req->arena_len = 0;
	req->flags = 0;
	memcpy((char*)req + sizeof(dserver_ring_slot_t), &body, sizeof(body));
	dserver_ring_producer_publish(c2s);
	gr_machmsg_published++;
	__dserver_ring_proc_event("request", "RING", L->slot_index, seq,
	                          (uint32_t)dserver_callnum_mach_msg_overwrite);
	GR_TRACE("RING_TRACE guest RING_MACHMSG_PUBLISH lane=%u gen=%u seq=%u tid=%d\n",
	         L->slot_index, (unsigned)L->generation, (unsigned)seq,
	         (int)LINUX_SYSCALL(__NR_gettid));
	gr_wake_server(L);

	// Production (unbounded) duplex-aware wait -- NOT the bounded proof helper. See gr_machmsg_wait_reply.
	gr_wait_result_t wait = gr_machmsg_wait_reply(L);
	if (wait.state == GR_WAIT_COMMITTED_UNKNOWN) {
		gr_machmsg_committed_unknown++;
		if (out_code) *out_code = KERN_FAILURE;
		return 0; // published: do NOT retry over UDS
	}
	dserver_ring_slot_t* rep = wait.slot;
	int rc = 0;
	if (out_code) *out_code = KERN_FAILURE; // bad/mismatched reply after publish: no UDS retry
	if (rep->seq == seq && rep->callnum == (uint32_t)dserver_callnum_mach_msg_overwrite) {
		if (rep->length >= sizeof(dserver_ring_reply_hdr_t) && rep->length <= inlineCap) {
			char* payload = (char*)rep + sizeof(dserver_ring_slot_t);
			dserver_ring_reply_hdr_t* rh = (dserver_ring_reply_hdr_t*)payload;
			if (rh->code == DSERVER_RING_DUPLEX_DECLINE) {
				gr_machmsg_fallback_declined++;
				gr_machmsg_published--;
				rc = -1; // declined BEFORE dispatch (no mutation) -> UDS-fallback
			} else {
				if (out_code) *out_code = rh->code;
				gr_machmsg_final_replies++;
				__dserver_ring_proc_event("reply", "RING", L->slot_index, seq,
				                          (uint32_t)dserver_callnum_mach_msg_overwrite);
				GR_TRACE("RING_TRACE guest RING_MACHMSG_REPLY_CONSUME lane=%u gen=%u seq=%u code=%d\n",
				         L->slot_index, (unsigned)L->generation, (unsigned)seq, (int)rh->code);
			}
		}
	}
	dserver_ring_consumer_advance(gr_s2c(L));
	return rc;
}

#endif // DARLING_RING_TRANSPORT
