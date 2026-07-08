#pragma once
struct linux_stat { int st_mode; };
struct linux_statfs64 { int unused; };
struct stat64 { int unused; };
struct linux_timeval { long tv_sec; long tv_usec; };
struct bsd_statfs { char f_mntonname[1024]; char f_fstypename[32]; char f_mntfromname[1024]; };
struct bsd_statfs64 { char f_mntonname[1024]; char f_fstypename[32]; char f_mntfromname[1024]; };
void stat_linux_to_bsd(struct linux_stat *src, struct stat *dst);
void stat_linux_to_bsd64(struct linux_stat *src, struct stat64 *dst);
void statfs_linux_to_bsd64(struct linux_statfs64 *src, struct bsd_statfs64 *dst);
