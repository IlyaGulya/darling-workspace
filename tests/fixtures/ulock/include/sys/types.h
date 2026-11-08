#pragma once
/* RPC's public host identity type. Do not import glibc's timespec into the
 * Darwin syscall TU, which declares its Linux wire timeout locally. */
#include <stddef.h>
typedef int pid_t;
