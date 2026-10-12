/*
 Host oracle for the vecLib/BNNS random generator (libBNNS).

 This test compiles the guest library's own source file on the host and drives it, because the two
 things that can silently be wrong here are correctness of the cipher core and the interval the fill
 promises.  It carries its own bad model: for a narrow interval near a common magnitude the UNCLAMPED
 affine map does land on b (measured: 6 of 200000 for a = 1000, b = 1001), and the contract requires
 that arm to still produce at least one such draw -- otherwise the probe has lost the power to detect
 the defect it exists for -- while the implemented function produces none.

 Machine readable lines only; the shell contract asserts them.
*/

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stddef.h>

#include "BNNS.c"

#define SAMPLES 200000

static void make_generator(bnns_random_generator* g)
{
	uint8_t key[16];

	arc4random_buf(key, sizeof(key));
	bnns_aes128_expand_key(key, g->round_keys);
	g->counter = 0;
	g->available = 0;
	pthread_mutex_init(&g->lock, NULL);
}

static void fill_descriptor(BNNSNDArrayDescriptor* d, float* data, size_t n)
{
	memset(d, 0, sizeof(*d));
	d->layout = 0x10000; /* 1-dimensional, per bnns_constants.h */
	d->size[0] = n;
	d->stride[0] = 1;
	d->data = data;
	d->data_type = BNNSDataTypeFloat32;
}

int main(void)
{
	uint8_t key[16] = {0x00,0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,0x09,0x0a,0x0b,0x0c,0x0d,0x0e,0x0f};
	uint8_t plain[16] = {0x00,0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88,0x99,0xaa,0xbb,0xcc,0xdd,0xee,0xff};
	uint8_t round_keys[176];
	uint8_t cipher[16];
	static float buf[SAMPLES];
	BNNSNDArrayDescriptor d;
	bnns_random_generator* g;
	float a, b;
	int i, clamped_outside = 0, unclamped_outside = 0;
	double sum = 0;

	/* FIPS-197 C.1: the cipher core, against the published vector. */
	bnns_aes128_expand_key(key, round_keys);
	bnns_aes128_encrypt_block(round_keys, plain, cipher);
	printf("KAT=");
	for (i = 0; i < 16; i++)
		printf("%02x", cipher[i]);
	printf("\n");

	/* The interval contract, and the bad model: the same draws from the unclamped affine map. */
	a = 1000.0f;
	b = 1001.0f;
	g = calloc(1, sizeof(*g));
	make_generator(g);
	for (i = 0; i < SAMPLES; i++) {
		double u = (double)bnns_uniform_unit(g);
		float unclamped = (float)((double)a + ((double)b - (double)a) * u);

		if (unclamped >= b)
			unclamped_outside++;
	}
	fill_descriptor(&d, buf, SAMPLES);
	if (BNNSRandomFillUniformFloat(g, &d, a, b) != 0)
		return 2;
	for (i = 0; i < SAMPLES; i++) {
		if (buf[i] >= b || buf[i] < a)
			clamped_outside++;
		sum += buf[i];
	}
	printf("UNCLAMPED_OUTSIDE=%d\n", unclamped_outside);
	printf("CLAMPED_OUTSIDE=%d\n", clamped_outside);
	printf("MEAN=%.4f\n", sum / SAMPLES);

	/* Distinctness on a WIDE interval, where binary32 has room: a ramp, a constant or a repeated block
	   would collapse this count.  (Duplicates on a narrow interval are arithmetic, not a defect: a
	   width of 1 at magnitude 1000 holds about 16000 representable values for 2^24 possible draws.) */
	{
		enum { WIDE = 4096 };
		int distinct = 0;

		fill_descriptor(&d, buf, WIDE);
		if (BNNSRandomFillUniformFloat(g, &d, -1.5f, 2.5f) != 0)
			return 3;
		for (i = 0; i < WIDE; i++) {
			int j, seen = 0;

			for (j = 0; j < i; j++)
				if (buf[j] == buf[i])
					seen = 1;
			if (!seen)
				distinct++;
		}
		printf("DISTINCT_WIDE=%d\n", distinct);
	}

	/* a == b returns exactly that value, and a bad descriptor fails instead of succeeding. */
	{
		int identical = 1, nonzero_status = 1;

		fill_descriptor(&d, buf, 64);
		BNNSRandomFillUniformFloat(g, &d, 7.25f, 7.25f);
		for (i = 0; i < 64; i++)
			if (buf[i] != 7.25f)
				identical = 0;
		printf("A_EQ_B_EXACT=%s\n", identical ? "yes" : "no");

		d.data_type = 12345;
		if (BNNSRandomFillUniformFloat(g, &d, 0.0f, 1.0f) == 0)
			nonzero_status = 0;
		d.data_type = BNNSDataTypeFloat32;
		d.layout = 0;
		if (BNNSRandomFillUniformFloat(g, &d, 0.0f, 1.0f) == 0)
			nonzero_status = 0;
		printf("BAD_DESCRIPTOR_FAILS=%s\n", nonzero_status ? "yes" : "no");
	}

	return 0;
}
