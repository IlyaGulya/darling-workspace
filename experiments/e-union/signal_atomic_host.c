/* Use the actual native-mask helper through the runner's linux_syscall
 * transport. Only the scalar caller type is adapted; no signal policy or
 * BSD mask conversion lives in this bridge. */
#include <stdint.h>
#include <stdlib.h>
#include <darling/emulation/common/signal_atomic.h>

_Static_assert(sizeof(signal_atomic_mask_t) == 8, "native saved mask must be exactly 64 bits");
_Static_assert(sizeof(uint64_t) == sizeof(signal_atomic_mask_t), "bridge saved mask must be lossless");

void eunion_signal_test_begin(uint64_t* saved) {
    signal_atomic_mask_t native_saved;
    if (signal_atomic_begin(&native_saved) < 0) {
        abort();
    }
    *saved = native_saved;
}

void eunion_signal_test_end(const uint64_t* saved) {
    signal_atomic_mask_t native_saved = *saved;
    if (signal_atomic_end(&native_saved) < 0) {
        abort();
    }
}
