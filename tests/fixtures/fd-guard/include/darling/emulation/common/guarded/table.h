#pragma once
#include <stdbool.h>
enum { guard_flag_prevent_close = 1 };
bool guard_table_check(int fd, int flag);
