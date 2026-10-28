/* Link the real stack-only formatter used by signal-interrupted production
 * paths. Unused diagnostics/readline sections are discarded by the host link;
 * the harness already owns its diagnostic boundary adapters. No formatting
 * algorithm or masking policy is copied here. */
#define __simple_printf eunion_unused_simple_printf
#define __simple_fprintf eunion_unused_simple_fprintf
#define __simple_kprintf eunion_unused_simple_kprintf
#define __simple_abort eunion_unused_simple_abort
#include "common/simple.c"
