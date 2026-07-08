#pragma once
#define VCHROOT_FOLLOW 1
struct vchroot_expand_args {
	int flags;
	int dfd;
	char path[4096];
};
int vchroot_expand(struct vchroot_expand_args *args);
int vchroot_prepare_write(const char *path);
void vchroot_pre_mkdir(const char *path);
int vchroot_xattr_is_marker(const char *name);
