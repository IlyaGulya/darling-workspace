#pragma once
#define LINUX_AT_INVALID (-1)
int atfd(int fd);
int atflags_bsd_to_linux(int flags);
