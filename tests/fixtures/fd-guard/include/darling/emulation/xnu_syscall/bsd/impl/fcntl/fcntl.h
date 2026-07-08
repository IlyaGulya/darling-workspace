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
long sys_fcntl_nocancel(int fd, int cmd, long arg);
