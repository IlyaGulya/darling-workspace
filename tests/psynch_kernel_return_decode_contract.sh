#!/usr/bin/env bash
set -euo pipefail

src="${LIBPTHREAD_SRC_ROOT:?set LIBPTHREAD_SRC_ROOT}"
test -f "$src/src/inline_internal.h"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/os" \
	"$tmp/include/mach" \
	"$tmp/include/sys" \
	"$tmp/include/System" \
	"$tmp/include/platform"

cat > "$tmp/include/os/lock_private.h" <<'H_EOF'
#pragma once
typedef struct { int opaque; } os_unfair_lock_s;
typedef os_unfair_lock_s* os_unfair_lock_t;
static inline __attribute__((overloadable)) void os_unfair_lock_lock_no_tsd(os_unfair_lock_t lock) { (void)lock; }
static inline __attribute__((overloadable)) void os_unfair_lock_lock_no_tsd(os_unfair_lock_t lock, unsigned int options, unsigned int mts) { (void)lock; (void)options; (void)mts; }
static inline __attribute__((overloadable)) void os_unfair_lock_unlock_no_tsd(os_unfair_lock_t lock) { (void)lock; }
static inline __attribute__((overloadable)) void os_unfair_lock_unlock_no_tsd(os_unfair_lock_t lock, unsigned int mts) { (void)lock; (void)mts; }
static inline void os_unfair_lock_lock_with_options(os_unfair_lock_t lock, unsigned int options) { (void)lock; (void)options; }
static inline void os_unfair_lock_unlock(os_unfair_lock_t lock) { (void)lock; }
#define OS_UNFAIR_LOCK_INIT ((os_unfair_lock_s){0})
#define OS_UNFAIR_LOCK_DATA_SYNCHRONIZATION 1u
#define OS_UNFAIR_LOCK_ADAPTIVE_SPIN 2u
H_EOF

for header in \
	mach/mach.h \
	mach/mach_time.h \
	sys/codesign.h \
	System/machine/cpu_capabilities.h \
	platform/string.h \
	pthread_machdep.h \
	pthread_spis.h \
	pthread_asm.h \
	internal.h \
	types_internal.h \
	prototypes_internal.h \
	platform_internal.h \
	platform.h \
	os/tsd.h
do
	mkdir -p "$tmp/include/$(dirname "$header")"
	printf '#pragma once\n' > "$tmp/include/$header"
done

cat > "$tmp/include/mach/mach.h" <<'H_EOF'
#pragma once
typedef unsigned int mach_port_t;
typedef int kern_return_t;
#define MACH_PORT_NULL 0
H_EOF

cat > "$tmp/include/pthread.h" <<'H_EOF'
#pragma once
#include <stdint.h>
typedef struct {
	uint32_t sig;
	union {
		uint32_t value;
		struct { unsigned int ulock; } options;
	} mtxopts;
} pthread_mutex_t;
typedef struct {
	uint32_t sig;
} pthread_rwlock_t;
typedef struct _opaque_pthread_t {
	uintptr_t sig;
	void* tsd[256];
	struct _opaque_pthread_t* tl_plist;
} *pthread_t;
#define _PTHREAD_MUTEX_SIG_fast 0x4d555458u
#define _PTHREAD_MUTEX_SIG_MASK 0xffffffffu
#define _PTHREAD_MUTEX_SIG_CMP 0x32aaaba7u
#define _PTHREAD_MUTEX_SIG_init_MASK 0xffffffffu
#define _PTHREAD_MUTEX_SIG_init_CMP 0x32aaaba7u
#define _PTHREAD_RWLOCK_SIG 0x2da8b3b4u
#define _PTHREAD_RWLOCK_SIG_init 0x2da8b3b4u
#define _PTHREAD_TSD_SLOT_MACH_THREAD_SELF 1
#define _PTHREAD_TSD_SLOT_MACH_THREAD_SELF_TYPE mach_port_t
static inline pthread_t _pthread_self_direct(void) { return (pthread_t)0; }
H_EOF

cat > "$tmp/include/types_internal.h" <<'H_EOF'
#pragma once
typedef struct pthread_globals_s* pthread_globals_t;
struct pthread_globals_s { int unused; };
H_EOF

cat > "$tmp/include/prototypes_internal.h" <<'H_EOF'
#pragma once
void* os_alloc_once(unsigned long key, unsigned long size, void* initializer);
extern uintptr_t _pthread_ptr_munge_token;
extern os_unfair_lock_s _pthread_list_lock;
extern pthread_t __pthread_head;
void abort_with_reason(unsigned int reason_namespace, unsigned long long reason_code, const char* reason_string, unsigned long long reason_flags);
#define OS_ALLOC_ONCE_KEY_LIBSYSTEM_PTHREAD 1
#define OS_REASON_LIBSYSTEM 5
#define TAILQ_FOREACH(var, head, field) for ((var) = 0; (var) != 0; (var) = 0)
H_EOF

cat > "$tmp/include/stdatomic.h" <<'H_EOF'
#include_next <stdatomic.h>
H_EOF

clang -std=gnu11 -Wall -Wextra -Werror \
	-Wno-unknown-pragmas \
	-include stdbool.h \
	-include pthread.h \
	-include mach/mach.h \
	-include os/lock_private.h \
	-include types_internal.h \
	-include prototypes_internal.h \
	-DOS_ALWAYS_INLINE= \
	-DOS_OVERLOADABLE='__attribute__((overloadable))' \
	-DOS_CONST= \
	-Dos_unlikely\(x\)='(x)' \
	-I "$tmp/include" \
	-I "$src/src" \
	"$PWD/tests/psynch_kernel_return_decode_contract.c" \
	-o "$tmp/psynch_kernel_return_decode_contract"
"$tmp/psynch_kernel_return_decode_contract"
