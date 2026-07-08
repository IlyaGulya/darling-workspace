#pragma once
#include <stddef.h>
#include <sys/_types/_iovec_t.h>
#define LINUX_MSG_DONTWAIT 0x40
#define LINUX_SOL_SOCKET 1
#define LINUX_CMSG_SPACE(len) (sizeof(struct linux_cmsghdr) + (len))
#define LINUX_CMSG_LEN(len) (sizeof(struct linux_cmsghdr) + (len))
struct linux_cmsghdr {
	size_t cmsg_len;
	int cmsg_level;
	int cmsg_type;
	unsigned char cmsg_data[32];
};
struct linux_msghdr {
	void* msg_name;
	size_t msg_namelen;
	struct iovec* msg_iov;
	size_t msg_iovlen;
	void* msg_control;
	size_t msg_controllen;
	int msg_flags;
};
