#include <stdarg.h>
#include <stdio.h>

#include <darling/emulation/common/simple.h>

static int kprintf_calls;
static int side_effects;

void __simple_kprintf(const char *format, ...)
{
	(void)format;
	kprintf_calls++;
}

static int side_effect(void)
{
	side_effects++;
	return 7;
}

int main(void)
{
	hotpath_kdebug("value %d\n", side_effect());
	if (kprintf_calls != 0 || side_effects != 0) {
		fprintf(stderr, "default hotpath_kdebug evaluated: calls=%d side=%d\n", kprintf_calls, side_effects);
		return 1;
	}
	puts("GREEN: default hotpath_kdebug is compiled out");
	return 0;
}
