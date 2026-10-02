#!/usr/bin/env python3
"""Deterministic host-level model of the process-control request slot (dar-b5pe).

WHY A MODEL AND NOT A GUEST RUN. The defect this test exists for appeared in roughly a third of twelve-thread
boots and its evidence is a race between two publishers of one shared page, so a guest run can only report that
it happened, never which interleaving caused it. This file encodes the protocol as it is written in
`include/darlingserver/rpc-supplement.h` -- the two-state request word (IDLE/PENDING with CLAIMED written by the
server on ownership transfer and DONE on completion), the separate reply word with its own sequence, and the
client's RELEASE, which is a plain store of IDLE -- and then forces the interleavings by ordering the operations
explicitly instead of by sleeping.

WHAT IT PROVES. Each case below is one interleaving and each assertion is one sentence of the required invariant:
a client that gives up waiting must not hand a slot to the next publisher while an older server transaction can
still publish its completion, and a late completion must never satisfy or mutate a newer generation. Cases A and
B pass on the current protocol; C, D, E and F fail on it, which is the point: this test is the RED proof for the
generation/ownership-safe abandonment the protocol still owes.

Run directly (`python3 tests/process_control_slot_ownership_model.py`) or through the shell contract next to it.
"""
from __future__ import annotations

import sys

IDLE, PENDING, DONE, CLAIMED = 0, 1, 2, 3


class Page:
    """The shared page: one request slot and one reply word, exactly as the struct declares them."""

    def __init__(self) -> None:
        self.request_state = IDLE
        self.request_seq = 0
        self.request_op = 0
        self.reply_state = IDLE
        self.reply_seq = 0
        self.reply_status = 0
        self.owner: int | None = None  # the server's record of which generation it claimed


class Failure(AssertionError):
    pass


def publish(page: Page, generation: int, op: int) -> bool:
    """Client publish: claim the slot (IDLE, or a DONE left by a completed earlier request) and write the request."""
    if page.request_state not in (IDLE, DONE):
        return False
    page.request_state = PENDING
    page.request_seq = generation
    page.request_op = op
    return True


def release(page: Page) -> None:
    """The client's RELEASE today: a plain store of IDLE, with no notion of who else may still own the slot."""
    page.request_state = IDLE
    page.request_seq = 0


def server_claim(page: Page) -> int | None:
    """Ownership transfer. From here the server owns the request whatever the client does."""
    if page.request_state != PENDING:
        return None
    page.request_state = CLAIMED
    page.owner = page.request_seq
    return page.owner


def server_complete(page: Page, generation: int, status: int) -> None:
    """The server publishes its completion. Nothing in the protocol stops a late completion from landing on a slot
    the next publisher already re-used, which is the defect these cases expose."""
    page.reply_seq = generation
    page.reply_status = status
    page.reply_state = DONE
    page.request_state = DONE


def consume(page: Page, generation: int) -> int | None:
    """Client consume: read the reply only if it belongs to this generation."""
    if page.reply_state != DONE or page.reply_seq != generation:
        return None
    return page.reply_status


def case_a_normal() -> None:
    page = Page()
    assert publish(page, 1, op=8), "A: publish failed"
    assert server_claim(page) == 1, "A: server did not claim"
    server_complete(page, 1, status=0)
    if consume(page, 1) != 0:
        raise Failure("A: normal completion was not consumable")


def case_b_abandon_before_claim() -> None:
    page = Page()
    assert publish(page, 1, op=8), "B: publish failed"
    release(page)  # the client gives up before anybody claimed it
    if page.request_state != IDLE:
        raise Failure("B: abandonment before claim left the slot not reusable")
    assert publish(page, 2, op=7), "B: the slot was not reusable after an unclaimed abandonment"
    if server_claim(page) != 2:
        raise Failure("B: the server claimed the wrong generation")


def case_c_abandon_after_claim() -> None:
    page = Page()
    assert publish(page, 1, op=8), "C: publish A failed"
    if server_claim(page) != 1:
        raise Failure("C: the server did not take ownership of A")
    release(page)  # THE DEFECT: the client releases a slot the server still owns
    assert publish(page, 2, op=7), "C: B could not publish into a slot whose owner is still the server"
    server_complete(page, 1, status=0)  # A's completion arrives late, after B published
    if page.reply_seq == 1 and page.request_seq == 2:
        raise Failure(
            "C: a late completion for generation A landed on generation B's slot "
            f"(reply_seq={page.reply_seq}, request_seq={page.request_seq})"
        )


def case_d_late_completion_must_not_satisfy_b() -> None:
    page = Page()
    assert publish(page, 1, op=8) and server_claim(page) == 1
    release(page)
    assert publish(page, 2, op=7), "D: B could not publish"
    server_complete(page, 1, status=0)
    if consume(page, 2) is not None:
        raise Failure("D: B consumed A's reply")


def case_e_stale_done_during_publication() -> None:
    """A's completion is already DONE when B publishes. B must never read it as its own."""
    page = Page()
    assert publish(page, 1, op=8) and server_claim(page) == 1
    server_complete(page, 1, status=0)
    if not publish(page, 2, op=7):
        raise Failure("E: B could not publish over a completed request")
    if consume(page, 2) is not None:
        raise Failure("E: a foreign DONE satisfied generation B")
    if server_claim(page) != 2:
        raise Failure("E: the server could not claim B after A's stale DONE")


def case_f_repeated_generations() -> None:
    """Repeated A/B/C generations under forced scheduling must never wedge the slot nor leak a generation."""
    page = Page()
    for generation in range(1, 40):
        if generation % 3 == 0:
            # abandon before claim
            assert publish(page, generation, op=8), f"F: publish {generation} failed"
            release(page)
            assert page.request_state == IDLE, f"F: slot wedged after abandoning {generation}"
            continue
        assert publish(page, generation, op=8), f"F: publish {generation} failed"
        if server_claim(page) != generation:
            raise Failure(f"F: the server claimed the wrong generation at {generation}")
        if generation % 3 == 1:
            release(page)  # abandonment after claim: the unsafe path
        server_complete(page, generation, status=0)
        if page.reply_state == DONE and page.reply_seq == generation:
            continue
        raise Failure(f"F: generation {generation} left no completion")


def main() -> int:
    cases = [
        ("A normal request", case_a_normal),
        ("B abandonment before claim", case_b_abandon_before_claim),
        ("C abandonment after claim", case_c_abandon_after_claim),
        ("D late completion must not satisfy B", case_d_late_completion_must_not_satisfy_b),
        ("E stale DONE during publication", case_e_stale_done_during_publication),
        ("F repeated generations", case_f_repeated_generations),
    ]
    failures = 0
    for name, fn in cases:
        try:
            fn()
        except Failure as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        else:
            print(f"PASS {name}")
    if failures:
        print(f"SLOT-OWNERSHIP failures={failures} of {len(cases)} -- the current protocol is not ownership-safe")
        return 1
    print(f"SLOT-OWNERSHIP failures=0 of {len(cases)} -- the protocol is ownership-safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
