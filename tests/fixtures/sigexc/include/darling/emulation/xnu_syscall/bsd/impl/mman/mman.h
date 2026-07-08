#pragma once
static inline long sys_mprotect(void* addr, unsigned long len, int prot) { (void)addr; (void)len; (void)prot; return 0; }
static inline long sys_mmap(void* addr, unsigned long len, int prot, int flags, int fd, unsigned long off) { (void)addr; (void)len; (void)prot; (void)flags; (void)fd; (void)off; return 0; }
static inline long sys_munmap(void* addr, unsigned long len) { (void)addr; (void)len; return 0; }
