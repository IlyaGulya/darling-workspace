#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/event.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

static int infrastructure_error(const char *name, const char *operation, int error)
{
    fprintf(stderr, "KQUEUE_CLOSE_PIPE_EOF_INFRA_ERROR: %s: %s: %s (%d)\n",
            name, operation, strerror(error), error);
    return 2;
}

static int close_owned(const char *name, const char *operation, int *fd)
{
    int owned = *fd;
    *fd = -1;
    if (close(owned)) return infrastructure_error(name, operation, errno);
    return 0;
}

static int run_case(const char *name, int16_t filter, int queue_first)
{
    int descriptors[3] = { -1, -1, -1 };
    int status = 0;
    struct kevent change, event;
    const struct timespec timeout = { 0, 0 };
    char byte;

    printf("CASE %s\n", name);
    if (pipe(descriptors)) {
        status = infrastructure_error(name, "pipe", errno);
        goto cleanup;
    }
    int flags = fcntl(descriptors[0], F_GETFL);
    if (flags < 0 || fcntl(descriptors[0], F_SETFL, flags | O_NONBLOCK) < 0) {
        status = infrastructure_error(name, "nonblocking reader", errno);
        goto cleanup;
    }
    descriptors[2] = kqueue();
    if (descriptors[2] < 0) {
        status = infrastructure_error(name, "kqueue", errno);
        goto cleanup;
    }

    /* Match libuv's temporary select probe, including READ on a write end.
     * A returned readiness event is not the oracle: only subsequent EOF is. */
    EV_SET(&change, descriptors[1], filter, EV_ADD | EV_ENABLE, 0, 0, NULL);
    int count = kevent(descriptors[2], &change, 1, &event, 1, &timeout);
    int error = count < 0 ? errno :
        (count == 1 && (event.flags & EV_ERROR) ? (int)event.data : 0);
    if (error) {
        /* Native runtimes may reject the unsupported READ/write-end pairing.
         * Still check its closure, and never skip the supported WRITE cases. */
        if (filter == EVFILT_READ &&
            (error == EBADF || error == EINVAL || error == ENOTSUP)) {
            printf("UNSUPPORTED %s: EVFILT_READ on pipe writer: %s (%d)\n",
                   name, strerror(error), error);
        } else {
            status = infrastructure_error(name, "kevent registration", error);
            goto cleanup;
        }
    }

    if (queue_first) {
        status = close_owned(name, "close kqueue", &descriptors[2]);
        if (status) goto cleanup;
    }
    status = close_owned(name, "close writer", &descriptors[1]);
    if (status) goto cleanup;

    /* No data was written. EOF must be immediate after the last writer closes,
     * even when its kqueue is still open. O_NONBLOCK makes leaks fail, not hang. */
    ssize_t result = read(descriptors[0], &byte, 1);
    error = result < 0 ? errno : 0;
    if (result != 0) {
        if (result < 0 && error != EAGAIN && error != EWOULDBLOCK) {
            status = infrastructure_error(name, "read after writer close", error);
        } else {
            fprintf(stderr, "%s: read after writer close returned %ld, "
                    "expected 0 (EOF); errno=%d (%s)\n",
                    name, (long)result, error, error ? strerror(error) : "none");
            status = 1;
        }
    } else {
        printf("EOF %s\n", name);
    }

cleanup:
    /* Also release the queue after the writer-first oracle has been observed. */
    for (int i = 2; i >= 0; --i) {
        if (descriptors[i] >= 0 &&
            close_owned(name, "cleanup close", &descriptors[i])) status = 2;
    }
    return status;
}

static int parent_watch_survives_fork(void)
{
    const char *name = "parent watch after child teardown";
    int descriptors[3] = { -1, -1, -1 };
    int status = 0, child_status;
    struct kevent change, event;
    const struct timespec immediate = { 0, 0 }, deadline = { 2, 0 };

    if (pipe(descriptors) || (descriptors[2] = kqueue()) < 0) {
        status = infrastructure_error(name, "create pipe/kqueue", errno);
        goto cleanup;
    }
    EV_SET(&change, descriptors[0], EVFILT_READ, EV_ADD, 0, 0, NULL);
    int count = kevent(descriptors[2], &change, 1, &event, 1, &immediate);
    if (count < 0 || (count == 1 && (event.flags & EV_ERROR))) {
        status = infrastructure_error(name, "register reader",
                                      count < 0 ? errno : (int)event.data);
        goto cleanup;
    }
    pid_t child = fork();
    if (child < 0) {
        status = infrastructure_error(name, "fork", errno);
        goto cleanup;
    }
    if (child == 0) _exit(0);
    pid_t reaped;
    do {
        reaped = waitpid(child, &child_status, 0);
    } while (reaped < 0 && errno == EINTR);
    if (reaped != child || !WIFEXITED(child_status) || WEXITSTATUS(child_status)) {
        status = infrastructure_error(name, "wait for child", reaped < 0 ? errno : EIO);
        goto cleanup;
    }
    if (write(descriptors[1], "x", 1) != 1) {
        status = infrastructure_error(name, "write readiness byte", errno);
        goto cleanup;
    }
    count = kevent(descriptors[2], NULL, 0, &event, 1, &deadline);
    if (count < 0) {
        status = infrastructure_error(name, "wait for parent readiness", errno);
    } else if (count != 1 || event.ident != (uintptr_t)descriptors[0] ||
               event.filter != EVFILT_READ || (event.flags & EV_ERROR)) {
        fprintf(stderr, "%s: child teardown removed the parent's read watch\n", name);
        status = 1;
    } else {
        printf("READY %s\n", name);
    }

cleanup:
    for (int i = 2; i >= 0; --i) {
        if (descriptors[i] >= 0 &&
            close_owned(name, "cleanup close", &descriptors[i])) status = 2;
    }
    return status;
}

int main(void)
{
    int status = run_case("read-filter queue-first", EVFILT_READ, 1);
    int result = run_case("write-filter queue-first", EVFILT_WRITE, 1);
    if (result > status) status = result;
    result = run_case("write-filter writer-first", EVFILT_WRITE, 0);
    if (result > status) status = result;
    result = parent_watch_survives_fork();
    if (result > status) status = result;
    if (status == 0) puts("KQUEUE_CLOSE_PIPE_EOF_OK");
    else if (status == 1) puts("KQUEUE_CLOSE_PIPE_EOF_BROKEN");
    return status;
}
