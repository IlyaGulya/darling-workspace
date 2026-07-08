#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>

extern void *__darling_bzero_return(void *, size_t) asm("___bzero");

int
main(void)
{
	const size_t size = 0xc50;
	unsigned char *ptr = malloc(size);

	if (ptr == NULL) {
		perror("malloc");
		return 2;
	}

	for (size_t i = 0; i < size; ++i) {
		ptr[i] = 0xa5;
	}

	void *ret = __darling_bzero_return(ptr, size);
	if (ret != ptr) {
		fprintf(stderr, "___bzero returned %p for %p\n", ret, (void *)ptr);
		free(ptr);
		return 1;
	}

	for (size_t i = 0; i < size; ++i) {
		if (ptr[i] != 0) {
			fprintf(stderr, "___bzero left byte %zu as 0x%02x\n", i, ptr[i]);
			free(ptr);
			return 1;
		}
	}

	free(ptr);
	puts("BZERO_RETURN_REGISTER_GUEST_OK");
	return 0;
}
