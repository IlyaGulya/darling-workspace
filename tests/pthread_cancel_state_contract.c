#include <errno.h>
#include <stdio.h>

#include <darlingserver/duct-tape/thread-cancel.h>

static int expect_int(const char* label, int got, int want)
{
	if (got == want)
		return 0;
	fprintf(stderr, "%s: got %d, want %d\n", label, got, want);
	return 1;
}

int main(void)
{
	dtape_thread_cancel_state_t state;
	dtape_thread_cancel_state_init(&state);

	int failed = 0;
	failed |= expect_int("initial disable", state.disable, 0);
	failed |= expect_int("initial pending", state.pending, 0);
	failed |= expect_int("initial canceled", state.canceled, 0);

	failed |= expect_int("action 2 disables", dtape_thread_cancel_state_canceled(&state, 2), 0);
	failed |= expect_int("disabled bit", state.disable, 1);

	failed |= expect_int("markcancel while disabled", dtape_thread_cancel_state_markcancel(&state), 0);
	failed |= expect_int("pending while disabled", state.pending, 1);
	failed |= expect_int("disabled cancel action", dtape_thread_cancel_state_canceled(&state, 0), EINVAL);
	failed |= expect_int("pending survives disabled action", state.pending, 1);
	failed |= expect_int("canceled still clear", state.canceled, 0);

	failed |= expect_int("action 1 enables", dtape_thread_cancel_state_canceled(&state, 1), 0);
	failed |= expect_int("enabled bit", state.disable, 0);
	failed |= expect_int("consume pending", dtape_thread_cancel_state_canceled(&state, 0), 0);
	failed |= expect_int("pending consumed", state.pending, 0);
	failed |= expect_int("canceled set", state.canceled, 1);

	failed |= expect_int("repeat cancel action", dtape_thread_cancel_state_canceled(&state, 0), EINVAL);
	failed |= expect_int("markcancel after canceled", dtape_thread_cancel_state_markcancel(&state), 0);
	failed |= expect_int("pending not rearmed after canceled", state.pending, 0);

	return failed ? 1 : 0;
}
