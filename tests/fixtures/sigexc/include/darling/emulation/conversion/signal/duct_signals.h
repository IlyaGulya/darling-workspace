#pragma once
typedef unsigned int sigset_t;
typedef unsigned long long linux_sigset_t;
struct bsd_siginfo;
typedef void (bsd_sig_handler)(int, struct bsd_siginfo*, void*);
#define SIG_IGN ((bsd_sig_handler*)1)
#define LINUX_SIGKILL 9
#define LINUX_SIGUSR1 10
#define LINUX_SIGSEGV 11
#define LINUX_SIGCHLD 17
#define LINUX_SIGSTOP 19
#define LINUX_SIGCONT 18
#define LINUX_SIGTTOU 22
#define LINUX_SIGURG 23
#define LINUX_SIGWINCH 28
#define LINUX_SA_SIGINFO 0x00000004u
#define LINUX_SA_ONSTACK 0x08000000u
#define LINUX_SA_RESTART 0x10000000u
#define LINUX_SA_NODEFER 0x40000000u
#define LINUX_SA_RESTORER 0x04000000
int signum_linux_to_bsd(int signum);
int signum_bsd_to_linux(int signum);
void sigset_linux_to_bsd(const linux_sigset_t* linux_set, sigset_t* bsd);
void sigset_bsd_to_linux(const sigset_t* bsd, linux_sigset_t* linux_set);
