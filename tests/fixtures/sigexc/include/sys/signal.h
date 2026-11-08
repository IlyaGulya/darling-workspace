#pragma once
/*
 * The generated RPC header pulls in host sys/types.h -> sys/select.h.
 * duct_signals.h reserves __sigset_t_defined for the guest sigset_t below,
 * so glibc's bits/types/sigset_t.h cannot supply its internal pselect type.
 * Import only that internal host type, never the host's public sigset_t.
 */
#include <bits/types/__sigset_t.h>

typedef unsigned int sigset_t;
struct bsd_siginfo;
typedef void (bsd_sig_handler)(int, struct bsd_siginfo*, void*);
#define SIG_IGN ((bsd_sig_handler*)1)
#define SIGSEGV 11
#define SIGTSTP 20
#define SIGSTOP 19
#define SIGTRAP 5
