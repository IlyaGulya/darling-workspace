#pragma once
long sys_fchownat(int fd, const char* path, int uid, int gid, int flag);
