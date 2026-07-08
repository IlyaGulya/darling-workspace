#pragma once
struct bsd_flock {
	short l_type;
	short l_whence;
	long long l_start;
	long long l_len;
	int l_pid;
};
struct linux_flock {
	short l_type;
	short l_whence;
	long long l_start;
	long long l_len;
	int l_pid;
};
#define LINUX_F_RDLCK 0
#define LINUX_F_WRLCK 1
#define LINUX_F_UNLCK 2
#define LINUX_F_DUPFD 0
#define LINUX_F_GETFD 1
#define LINUX_F_SETFD 2
#define LINUX_F_GETFL 3
#define LINUX_F_SETFL 4
#define LINUX_F_GETOWN 9
#define LINUX_F_SETOWN 8
#define LINUX_F_SETLK 6
#define LINUX_F_SETLKW 7
#define LINUX_F_GETLK 5
#define LINUX_F_DUPFD_CLOEXEC 1030
