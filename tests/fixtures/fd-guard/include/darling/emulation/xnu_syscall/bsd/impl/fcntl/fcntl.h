#pragma once
#ifndef F_CHECK_LV
#define F_CHECK_LV 98
#endif
#ifndef F_GETPATH
#define F_GETPATH 50
#endif
#ifndef F_FULLFSYNC
#define F_FULLFSYNC 51
#endif
/* Darwin commands from XNU bsd/sys/fcntl.h, absent from Linux libc. */
#define F_SETNOSIGPIPE 73
#define F_GETNOSIGPIPE 74
long sys_fcntl_nocancel(int fd, int cmd, long arg);
