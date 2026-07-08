#pragma once
typedef unsigned int sigset_t;
struct bsd_siginfo;
typedef void (bsd_sig_handler)(int, struct bsd_siginfo*, void*);
#define SIG_IGN ((bsd_sig_handler*)1)
#define SIGSEGV 11
#define SIGTSTP 20
#define SIGSTOP 19
#define SIGTRAP 5
