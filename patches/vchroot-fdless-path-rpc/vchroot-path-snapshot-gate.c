#define _GNU_SOURCE
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

static int fd_path(int fd, char *out, size_t cap) {
    char proc[64];
    snprintf(proc, sizeof proc, "/proc/self/fd/%d", fd);
    ssize_t n = readlink(proc, out, cap - 1);
    if (n < 0) return -1;
    out[n] = 0;
    return 0;
}

int main(void) {
    char root[] = "/tmp/vchroot-path-gate-XXXXXX";
    if (!mkdtemp(root)) return 2;
    char oldp[512], newp[512];
    snprintf(oldp, sizeof oldp, "%s/old", root);
    snprintf(newp, sizeof newp, "%s/new", root);
    if (mkdir(oldp, 0700) || mkdir(newp, 0700)) return 3;

    int oldfd = open(oldp, O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    int newfd = open(newp, O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    int dfd = dup(oldfd);
    if (oldfd < 0 || newfd < 0 || dfd < 0) return 4;

    struct stat oldst, newst;
    fstat(oldfd, &oldst); fstat(newfd, &newst);

    /* Old production order: server receives a dup first, then guest resolves its
       local fd after RPC return. A replacement in between makes them diverge. */
    int server_dup = dup(dfd);
    if (server_dup < 0) return 5;
    if (dup2(newfd, dfd) < 0) return 6;
    char old_server[512], old_guest[512];
    int old_server_ok = fd_path(server_dup, old_server, sizeof old_server) == 0;
    int old_guest_ok = fd_path(dfd, old_guest, sizeof old_guest) == 0;
    int old_diverged = old_server_ok && old_guest_ok && strcmp(old_server, old_guest) != 0;

    /* New order: guest resolves once before RPC and both sides consume exactly
       that byte snapshot. Replacing the fd afterwards cannot change the state
       accepted by the server or published by the guest. */
    if (dup2(oldfd, dfd) < 0) return 7;
    char snapshot[512];
    int snapshot_ok = fd_path(dfd, snapshot, sizeof snapshot) == 0;
    if (dup2(newfd, dfd) < 0) return 8;
    char new_server[512], new_guest[512];
    snprintf(new_server, sizeof new_server, "%s", snapshot);
    snprintf(new_guest, sizeof new_guest, "%s", snapshot);
    struct stat snapst;
    int snap_stat_ok = stat(snapshot, &snapst) == 0;
    int new_same = snapshot_ok && strcmp(new_server, new_guest) == 0;
    int new_is_original = snap_stat_ok && snapst.st_dev == oldst.st_dev && snapst.st_ino == oldst.st_ino;
    int working_fd_now_new = ({ struct stat s; fstat(dfd, &s) == 0 && s.st_dev == newst.st_dev && s.st_ino == newst.st_ino; });

    int invalid = dup(dfd);
    close(invalid);
    char bad[32]; errno = 0;
    int invalid_rejected = fd_path(invalid, bad, sizeof bad) < 0;

    int pass = old_diverged && new_same && new_is_original && working_fd_now_new && invalid_rejected;
    printf("{\"old_order\":{\"server_path\":\"%s\",\"guest_path\":\"%s\",\"diverged_after_dup2\":%s},"
           "\"new_order\":{\"snapshot\":\"%s\",\"server_guest_equal_after_dup2\":%s,\"snapshot_is_original_object\":%s,\"fd_number_now_names_replacement\":%s},"
           "\"invalid_fd_rejected_before_rpc\":%s,\"pass\":%s}\n",
           old_server_ok ? old_server : "", old_guest_ok ? old_guest : "", old_diverged ? "true":"false",
           snapshot_ok ? snapshot : "", new_same ? "true":"false", new_is_original ? "true":"false", working_fd_now_new ? "true":"false",
           invalid_rejected ? "true":"false", pass ? "true":"false");
    return pass ? 0 : 1;
}
