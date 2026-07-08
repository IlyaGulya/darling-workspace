#pragma once
#include <stdint.h>
typedef struct {
	uint32_t call_number;
	int pid;
	int tid;
	int architecture;
	uint32_t s2c_number;
} dserver_s2c_callhdr_t;
enum {
	dserver_s2c_msgnum_mmap = 1,
	dserver_s2c_msgnum_munmap = 2,
	dserver_s2c_msgnum_mprotect = 3,
	dserver_s2c_msgnum_msync = 4,
};
typedef struct {
	dserver_s2c_callhdr_t header;
	void* address;
	uint64_t length;
	int protection;
	int flags;
	int fd;
	uint64_t offset;
} dserver_s2c_call_mmap_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	void* address;
	int errno_result;
} dserver_s2c_reply_mmap_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	void* address;
	uint64_t length;
} dserver_s2c_call_munmap_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	int return_value;
	int errno_result;
} dserver_s2c_reply_munmap_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	void* address;
	uint64_t length;
	int protection;
} dserver_s2c_call_mprotect_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	int return_value;
	int errno_result;
} dserver_s2c_reply_mprotect_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	void* address;
	uint64_t size;
	int sync_flags;
} dserver_s2c_call_msync_t;
typedef struct {
	dserver_s2c_callhdr_t header;
	int return_value;
	int errno_result;
} dserver_s2c_reply_msync_t;
