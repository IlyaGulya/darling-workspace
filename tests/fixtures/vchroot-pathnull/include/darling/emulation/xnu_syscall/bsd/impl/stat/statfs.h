#pragma once
struct bsd_statfs64;
long sys_statfs64(const char *path, struct bsd_statfs64 *buf);
