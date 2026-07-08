#pragma once
#define BSD_AT_FDCWD -2
#define BSD_AT_SYMLINK_NOFOLLOW 0x20
#define BSD_AT_REMOVEDIR 0x80
#define BSD_AT_SYMLINK_FOLLOW 0x40
#define LINUX_AT_INVALID (-1)
int atfd(int fd);
int atflags_bsd_to_linux(int flags);
