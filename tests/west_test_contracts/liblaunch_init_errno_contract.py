#!/usr/bin/env python3
"""Execute the production liblaunch once/error gate in isolated subprocesses."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile


DARLING_SOURCE = Path(os.environ["DARLING_SOURCE"]).resolve()
POLICY_DIR = DARLING_SOURCE / "src/launchd/liblaunch"


PROGRAM = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "launch_client_init_error.h"

struct fixture_globals {
    pthread_once_t once;
    int lc_init_errno;
    void *connection;
};

static struct fixture_globals globals = { PTHREAD_ONCE_INIT, 0, NULL };
static int getsocket_result;
static bool path_nonempty;
static int init_calls;
static int connect_calls;
static int stub_connect_result;

static bool is_connected(void *context) {
    struct fixture_globals *state = context;
    return state->connection != NULL;
}

static int stub_getsocket(bool *nonempty) {
    *nonempty = path_nonempty;
    return getsocket_result;
}

static int stub_connect(void) {
    connect_calls++;
    if (stub_connect_result == 0)
        globals.connection = (void *)(uintptr_t)1;
    return stub_connect_result;
}

/* This is the production initialization shape: pthread_once invokes one
 * getsocket lookup, typed lookup failures never reach connect, and both public
 * entrypoints consume the same sticky result through the production gate. */
static void launch_client_init(void) {
    bool nonempty = false;
    int result;
    init_calls++;
    result = stub_getsocket(&nonempty);
    globals.lc_init_errno = launch_client_getsocket_errno(result, nonempty);
    if (!launch_client_init_allows_connect(globals.lc_init_errno)) {
        errno = globals.lc_init_errno;
        return;
    }
    if (stub_connect() == -1)
        globals.lc_init_errno = launch_client_preserve_init_errno(
            globals.lc_init_errno, errno != 0 ? errno : ENOTCONN);
}

static int launch_get_fd(void) {
    if (launch_client_require_connection(
            &globals.once, launch_client_init, &globals.lc_init_errno,
            is_connected, &globals) == -1)
        return -1;
    return 42;
}

static void *launch_msg_internal(void *message) {
    (void)message;
    if (launch_client_require_connection(
            &globals.once, launch_client_init, &globals.lc_init_errno,
            is_connected, &globals) == -1)
        return NULL;
    return globals.connection;
}

int main(int argc, char **argv) {
    int expected;
    assert(argc == 5);
    getsocket_result = atoi(argv[1]);
    path_nonempty = atoi(argv[2]) != 0;
    stub_connect_result = atoi(argv[3]);
    expected = atoi(argv[4]);

    errno = 0;
    if (expected == 0) {
        assert(launch_get_fd() == 42);
        assert(launch_msg_internal((void *)1) != NULL);
        assert(connect_calls == 1);
    } else {
        assert(launch_get_fd() == -1);
        assert(errno == expected);
        errno = 0;
        assert(launch_msg_internal((void *)1) == NULL);
        assert(errno == expected);
        assert(connect_calls == 0);
    }
    assert(init_calls == 1);
    return 0;
}
'''


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="liblaunch-production-path-") as raw:
        root = Path(raw)
        include = root / "include/servers"
        include.mkdir(parents=True)
        (include / "bootstrap.h").write_text(
            "#define BOOTSTRAP_SUCCESS 0\n"
            "#define BOOTSTRAP_NOT_PRIVILEGED 1100\n",
            encoding="utf-8",
        )
        test = root / "production_path.c"
        test.write_text(PROGRAM, encoding="utf-8")
        binary = root / "production-path"
        subprocess.run(
            [
                os.environ.get("CC", "cc"),
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-pthread",
                f"-I{root / 'include'}",
                f"-I{POLICY_DIR}",
                str(POLICY_DIR / "launch_client_init_error.c"),
                str(test),
                "-o",
                str(binary),
            ],
            check=True,
        )
        cases = (
            (1100, 0, -1, 1),  # EPERM
            (1105, 0, -1, 107),  # ENOTCONN on Linux
            (0, 0, -1, 107),  # successful lookup with empty path
            (0, 1, 0, 0),
        )
        for case in cases:
            subprocess.run([str(binary), *(str(value) for value in case)], check=True)
    print("LIBLAUNCH_PRODUCTION_PATH_VALID pthread_once=1 entrypoints=2")


if __name__ == "__main__":
    main()

