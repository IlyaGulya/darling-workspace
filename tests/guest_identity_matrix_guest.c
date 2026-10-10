/*
 * The exported identity constant must be the identity, and it must behave as one.
 *
 * `matrix_identity_float4x4` is declared by the SDK as `extern const simd_float4x4` and is exported from the
 * guest's libSystem. In Darling it used to be a bare, uninitialised definition under a TODO, i.e. an exported
 * all-zero matrix: multiplying by it zeroed the result and nothing reported anything. A wrong exported constant
 * is worse than a missing one, because a missing symbol fails loudly while a zeroed identity produces a
 * plausible-looking wrong answer.
 *
 * Two measurements, because either alone can pass while the symbol is still wrong:
 *   1. COMPONENTS: the four diagonal entries are 1 and every off-diagonal entry is 0.
 *   2. BEHAVIOUR: identity * M == M for a non-trivial M, through the same SDK path a guest would use.
 *
 * Verdict line (`dwdiag verdict` reads the first):
 *   IDENTITY pass=1 diag=1,1,1,1 offdiag-nonzero=0 identity-times-M-equals-M=1
 */
#include <simd/simd.h>
#include <string.h>
#include <stdio.h>

int main(void)
{
	const simd_float4x4 identity = matrix_identity_float4x4;

	float diagonal[4] = {
		identity.columns[0][0], identity.columns[1][1],
		identity.columns[2][2], identity.columns[3][3],
	};
	int diag_wrong = 0;
	for (int i = 0; i < 4; ++i)
		if (diagonal[i] != 1.0f)
			diag_wrong++;

	int offdiag_nonzero = 0;
	for (int c = 0; c < 4; ++c)
		for (int r = 0; r < 4; ++r)
			if (r != c && identity.columns[c][r] != 0.0f)
				offdiag_nonzero++;

	simd_float4x4 m = { .columns = {
		{  1.0f,  2.0f,  3.0f,  4.0f },
		{  5.0f,  6.0f,  7.0f,  8.0f },
		{  9.0f, 10.0f, 11.0f, 12.0f },
		{ 13.0f, 14.0f, 15.0f, 16.0f },
	} };
	simd_float4x4 product = simd_mul(identity, m);
	int product_equals_m = (memcmp(&product, &m, sizeof m) == 0);

	int pass = (diag_wrong == 0 && offdiag_nonzero == 0 && product_equals_m) ? 1 : 0;

	printf("IDENTITY pass=%d diag=%.0f,%.0f,%.0f,%.0f offdiag-nonzero=%d identity-times-M-equals-M=%d\n",
	       pass, diagonal[0], diagonal[1], diagonal[2], diagonal[3], offdiag_nonzero, product_equals_m);
	return pass ? 0 : 1;
}
