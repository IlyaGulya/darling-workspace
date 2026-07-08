#pragma once
typedef unsigned long sigset_t;
#define SIG_BLOCK 0
#define SIG_SETMASK 2
static inline long sys_sigprocmask(int how, const sigset_t* set, sigset_t* oldset)
{
	(void)how;
	(void)set;
	if (oldset)
		*oldset = 0;
	return 0;
}
