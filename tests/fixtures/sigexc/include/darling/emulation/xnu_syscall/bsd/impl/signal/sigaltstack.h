#pragma once
#include <stddef.h>
struct linux_stack {
	void* ss_sp;
	int ss_flags;
	size_t ss_size;
};
struct bsd_stack {
	void* ss_sp;
	size_t ss_size;
	int ss_flags;
};
static inline long sys_sigaltstack(const struct bsd_stack* new_stack, struct bsd_stack* old_stack)
{
	(void)new_stack;
	if (old_stack) {
		old_stack->ss_sp = 0;
		old_stack->ss_size = 0;
		old_stack->ss_flags = 0;
	}
	return 0;
}
