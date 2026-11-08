#pragma once
#include <stdint.h>
uint64_t __dserver_fd_courier_send(int fd, uint32_t kind);
void __dserver_ring_postfork_reset(void);
