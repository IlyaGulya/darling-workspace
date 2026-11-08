#pragma once
#include_next <linux_premigration/vchroot_expand.h>
int vchroot_prepare_write(const char *path);
void vchroot_pre_mkdir(const char *path);
int vchroot_xattr_is_marker(const char *name);
