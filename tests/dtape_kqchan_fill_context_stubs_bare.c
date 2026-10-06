/* Stubs whose names collide with XNU macros in the kernel headers; kept in a
 * translation unit that includes no XNU headers. C symbols are not mangled, so
 * the product objects resolve against these. */
unsigned char OSCompareAndSwap(unsigned int oldValue, unsigned int newValue, volatile unsigned int *address) {
	if (*address != oldValue) {
		return 0;
	}
	*address = newValue;
	return 1;
}

int copyout(const void *kaddr, unsigned long udaddr, unsigned long len) {
	(void)kaddr;
	(void)udaddr;
	(void)len;
	return 0;
}
