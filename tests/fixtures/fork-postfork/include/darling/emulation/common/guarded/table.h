#pragma once
typedef struct {
	void (*close)(int fd);
} guard_entry_options_t;
#define guard_flag_prevent_close 1
#define guard_flag_close_on_fork 2
void guard_table_postfork_child(void);
void guard_table_add(int fd, int flags, guard_entry_options_t* options);
