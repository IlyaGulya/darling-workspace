#pragma once
#include <stdint.h>
int dserver_rpc_mldr_path(char *path, uint64_t size, uint64_t *path_length);
int dserver_rpc_interrupt_exit(void);
